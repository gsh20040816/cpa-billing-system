from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from cpa_billing.models import APIKey, KeyOwnershipPeriod, TelegramUser
from cpa_billing.security import cpamp_key_hash, login_fingerprint, mask_api_key
from cpa_billing.web import LoginLimiter, create_app


def add_user(app, settings, user_id: int, raw_key: str, *, is_admin: bool = False) -> None:
    service = app.state.service
    with service.db.session() as session:
        session.add(TelegramUser(
            telegram_user_id=user_id,
            username=f"user{user_id}",
            is_admin=is_admin,
            registered_at_ms=1,
            last_seen_at_ms=1,
        ))
        session.flush()
        key = APIKey(
            cpamp_hash=cpamp_key_hash(raw_key),
            login_fingerprint=login_fingerprint(raw_key, settings.key_pepper),
            masked_value=mask_api_key(raw_key),
            status="active",
            current_owner_id=user_id,
            created_at_ms=1,
        )
        session.add(key)
        session.flush()
        session.add(KeyOwnershipPeriod(
            api_key_id=key.id,
            telegram_user_id=user_id,
            valid_from_ms=1,
            source="test",
            created_at_ms=1,
        ))


def login_user(client: TestClient, raw_key: str) -> dict:
    response = client.post("/auth/api-key/login", json={"api_key": raw_key})
    assert response.status_code == 200
    return response.json()


def test_web_has_no_registration_and_hides_other_users_keys(settings, monkeypatch) -> None:
    app = create_app(settings)
    add_user(app, settings, 2, "sk-cpa-user-two-secret")
    add_user(app, settings, 3, "sk-cpa-user-three-secret")
    app.state.service.create_cycle("cycle", "1970-01-01T08:00", "1970-01-02T08:00", 0)
    monkeypatch.setattr(app.state.service.cpa, "list_keys", lambda: [
        "sk-cpa-user-two-secret", "sk-cpa-user-three-secret",
    ])
    client = TestClient(app, base_url="https://billing.example")

    unauthenticated = client.get("/", follow_redirects=False)
    assert unauthenticated.status_code == 303
    assert unauthenticated.headers["location"] == "/login"
    assert client.get("/favicon.ico").status_code == 200
    assert client.get("/favicon.svg").status_code == 200
    assert client.get("/register").status_code == 404

    login = login_user(client, "sk-cpa-user-two-secret")
    assert login["telegram_user_id"] == 2
    own = client.get("/api/users/2/summary").json()
    other = client.get("/api/users/3/summary").json()
    assert "keys" not in own
    assert "keys" not in other
    assert len(client.get("/api/me/keys").json()["keys"]) == 1
    assert "sk-cpa-user-three-secret" not in str(other)
    assert client.get("/api/me/usage/events").status_code == 200
    admin_page = client.get("/admin", follow_redirects=False)
    assert admin_page.status_code == 303
    assert admin_page.headers["location"] == "/"

    logout = client.post("/auth/logout", headers={"X-CSRF-Token": login["csrf_token"]})
    assert logout.status_code == 204
    assert client.get("/api/session").status_code == 401


def test_api_key_admin_flag_grants_web_admin(settings, monkeypatch) -> None:
    app = create_app(settings)
    add_user(app, settings, 1, "sk-cpa-telegram-admin-secret", is_admin=True)
    monkeypatch.setattr(app.state.service.cpa, "list_keys", lambda: ["sk-cpa-telegram-admin-secret"])
    client = TestClient(app, base_url="https://billing.example")

    login_user(client, "sk-cpa-telegram-admin-secret")
    assert client.get("/api/session").json()["is_admin"] is True
    assert client.get("/admin", follow_redirects=False).status_code == 200
    assert client.get("/api/admin/session").status_code == 200


def test_admin_token_uses_the_unified_session_and_csrf(settings) -> None:
    app = create_app(settings)
    client = TestClient(app, base_url="https://billing.example")

    assert client.get("/api/admin/usage/filter-options").status_code == 401
    assert client.get("/api/admin/usage/events").status_code == 401
    invalid = client.post("/auth/admin/login", json={"management_token": "wrong"})
    assert invalid.status_code == 401
    assert invalid.json()["detail"] == "管理 token 无效。"

    response = client.post("/auth/admin/login", json={"management_token": settings.admin_token})
    assert response.status_code == 200
    csrf = response.json()["csrf_token"]
    assert client.get("/api/admin/session").json()["is_admin"] is True
    unified = client.get("/api/session")
    assert unified.status_code == 200
    assert unified.json()["is_admin"] is True
    assert unified.json()["telegram_user_id"] is None
    assert unified.json()["management_session"] is True
    assert client.get("/api/dashboard").status_code == 200
    assert client.get("/api/me/usage/filter-options").status_code == 200
    assert client.get("/api/admin/usage/filter-options").status_code == 200
    assert client.get("/api/admin/usage/events").status_code == 200

    missing_csrf = client.post("/api/admin/cycles/cycle0/preview", json={})
    assert missing_csrf.status_code == 403
    assert "CSRF" in missing_csrf.json()["detail"]
    logout = client.post("/auth/logout", headers={"X-CSRF-Token": csrf})
    assert logout.status_code == 204
    assert client.get("/api/session").status_code == 401
    assert client.get("/api/admin/session").status_code == 401


def test_admin_can_grant_and_revoke_web_access_for_telegram_user(settings, monkeypatch) -> None:
    app = create_app(settings)
    add_user(app, settings, 2, "sk-cpa-user-two-secret")
    monkeypatch.setattr(app.state.service.cpa, "list_keys", lambda: ["sk-cpa-user-two-secret"])
    admin_client = TestClient(app, base_url="https://billing.example")
    login = admin_client.post("/auth/admin/login", json={"management_token": settings.admin_token})
    csrf = login.json()["csrf_token"]
    headers = {"X-CSRF-Token": csrf}

    granted = admin_client.patch(
        "/api/admin/users/2/admin",
        headers=headers,
        json={"is_admin": True, "reason": "授予 Web 管理权限"},
    )
    assert granted.status_code == 200
    assert granted.json()["is_admin"] is True

    user_client = TestClient(app, base_url="https://billing.example")
    user_login = login_user(user_client, "sk-cpa-user-two-secret")
    assert user_client.get("/api/session").json()["is_admin"] is True
    assert user_client.get("/admin", follow_redirects=False).status_code == 200
    assert user_client.get("/api/admin/session").status_code == 200

    revoked = admin_client.patch(
        "/api/admin/users/2/admin",
        headers=headers,
        json={"is_admin": False, "reason": "取消测试权限"},
    )
    assert revoked.status_code == 200
    assert user_client.get("/api/session").json()["is_admin"] is False
    assert user_client.get("/api/admin/session").status_code == 403
    user_client.post("/auth/logout", headers={"X-CSRF-Token": user_login["csrf_token"]})


def test_site_mutations_require_user_csrf_and_admin_quota_reset(settings, monkeypatch) -> None:
    app = create_app(settings)
    add_user(app, settings, 2, "sk-cpa-user-two-secret")
    monkeypatch.setattr(app.state.service.cpa, "list_keys", lambda: ["sk-cpa-user-two-secret"])
    monkeypatch.setattr(app.state.service, "refresh_account_quotas", lambda account_ids: {"tasks": [], "accepted": 0})
    client = TestClient(app, base_url="https://billing.example")
    login = login_user(client, "sk-cpa-user-two-secret")

    assert client.post("/api/site/accounts/refresh", json={"account_ids": []}).status_code == 403
    response = client.post(
        "/api/site/accounts/refresh",
        json={"account_ids": []},
        headers={"X-CSRF-Token": login["csrf_token"]},
    )
    assert response.status_code == 200
    throttled = client.post(
        "/api/site/accounts/refresh",
        json={"account_ids": []},
        headers={"X-CSRF-Token": login["csrf_token"]},
    )
    assert throttled.status_code == 429
    assert client.post("/api/admin/accounts/account-1/reset-upstream-quota", json={}).status_code == 403

    admin_client = TestClient(app, base_url="https://billing.example")
    admin_login = admin_client.post("/auth/admin/login", json={"management_token": settings.admin_token})
    admin_csrf = admin_login.json()["csrf_token"]
    monkeypatch.setattr(
        app.state.service,
        "reset_account_quota",
        lambda account_id, reason, confirmations, operator_id, operator_type: {"ok": True, "account_id": account_id, "status": "reset", "required_confirmations": confirmations},
    )
    missing_reason = admin_client.post(
        "/api/admin/accounts/account-1/reset-upstream-quota",
        headers={"X-CSRF-Token": admin_csrf},
        json={"reason": ""},
    )
    assert missing_reason.status_code == 422
    reset = admin_client.post(
        "/api/admin/accounts/account-1/reset-upstream-quota",
        headers={"X-CSRF-Token": admin_csrf},
        json={"reason": "测试上游额度重置", "confirmations": 3},
    )
    assert reset.status_code == 200
    assert reset.json()["status"] == "reset"


def test_login_limiter_counts_failures_only() -> None:
    limiter = LoginLimiter(maximum=2, window_seconds=60)
    limiter.check("user:one")
    limiter.failure("user:one")
    limiter.failure("user:one")
    with pytest.raises(HTTPException) as blocked:
        limiter.check("user:one")
    assert getattr(blocked.value, "status_code", None) == 429
    limiter.success("user:one")
    limiter.check("user:one")
    limiter.check("user:two")


def test_admin_billing_rule_and_key_profile_endpoints(settings, monkeypatch) -> None:
    app = create_app(settings)
    monkeypatch.setattr(app.state.service, "accounts_snapshot", lambda: {"accounts": [], "inspection": {}})
    client = TestClient(app, base_url="https://billing.example")
    login = client.post("/auth/admin/login", json={"management_token": settings.admin_token})
    csrf = login.json()["csrf_token"]
    headers = {"X-CSRF-Token": csrf}

    created = client.post(
        "/api/admin/gradient-rules",
        headers=headers,
        json={
            "name": "web-gradient",
            "description": "created by test",
            "tiers": [
                {"left": 0, "right": 10, "multiplier": 1},
                {"left": 10, "right": None, "multiplier": 0.5},
            ],
            "reason": "web test",
        },
    )
    assert created.status_code == 200
    rule_id = created.json()["id"]
    snapshot = client.get("/api/admin/snapshot").json()["admin"]
    pool_id = snapshot["pools"][0]["id"]

    cycle = client.post(
        "/api/admin/cycles",
        headers=headers,
        json={
            "name": "web-cycle",
            "start": "1970-01-01T08:00",
            "end": "1970-01-02T08:00",
            "fixed_cost": "0",
            "gradient_rule_id": rule_id,
            "pool_costs": [{"pool_id": pool_id, "fixed_cost": "12.34"}],
        },
    )
    assert cycle.status_code == 200
    configured = client.put(
        "/api/admin/cycles/web-cycle/configuration",
        headers=headers,
        json={
            "gradient_rule_id": rule_id,
            "pool_costs": [{"pool_id": pool_id, "fixed_cost": "10.00"}],
            "reason": "adjust cost",
        },
    )
    assert configured.status_code == 200

    with app.state.service.db.session() as session:
        key = APIKey(
            cpamp_hash="f" * 64,
            login_fingerprint=None,
            masked_value="sk-cpa-****test",
            status="unowned",
            current_owner_id=None,
            present_in_cpa=True,
            created_at_ms=1,
        )
        session.add(key)
        session.flush()
        key_id = key.id
    profile = client.patch(
        f"/api/admin/keys/{key_id}/billing-profile",
        headers=headers,
        json={"name": "external", "multiplier": "7"},
    )
    assert profile.status_code == 200
    assert profile.json()["multiplier"] == "7"

    monkeypatch.setattr(app.state.service, "sync_cpa_keys", lambda: {
        "created": 0, "updated": 1, "retired": 0, "current": 1,
    })
    key_sync = client.post("/api/admin/cpa-keys/sync", headers=headers)
    assert key_sync.status_code == 200
    assert key_sync.json()["current"] == 1

    monkeypatch.setattr(app.state.service, "sync_upstream_prices", lambda *args, **kwargs: {
        "version_id": 2, "name": "upstream-test", "rated_events": 0, "rating_status": "queued",
    })
    pricing = client.post(
        "/api/admin/pricing-versions/sync",
        headers=headers,
        json={"name": "upstream-test", "reason": "refresh prices"},
    )
    assert pricing.status_code == 200
    assert pricing.json()["rating_status"] == "queued"

    manual = client.put(
        "/api/admin/pricing-rules",
        headers=headers,
        json={
            "model": "gpt-test",
            "input_usd_per_million": "2",
            "output_usd_per_million": "12",
            "cache_read_usd_per_million": "0.2",
            "cache_creation_usd_per_million": "2.5",
            "priority_input_usd_per_million": None,
            "priority_output_usd_per_million": None,
            "priority_cache_read_usd_per_million": None,
            "priority_cache_creation_usd_per_million": None,
            "flex_input_usd_per_million": None,
            "flex_output_usd_per_million": None,
            "long_context_threshold_tokens": None,
            "long_context_input_multiplier": "1",
            "long_context_output_multiplier": "1",
            "version_name": "manual-web",
            "reason": "手动修正测试价格",
        },
    )
    assert manual.status_code == 200
    assert manual.json()["name"] == "manual-web"
    admin_snapshot = client.get("/api/admin/snapshot").json()["admin"]
    assert admin_snapshot["pricing_rules"]["active_version"]["name"] == "manual-web"
    assert next(item for item in admin_snapshot["pricing_rules"]["models"] if item["model"] == "gpt-test")["default"]["input"]["usd_per_million"] == "2"


def test_admin_can_add_exact_manual_usage_with_independent_auth_and_csrf(settings, monkeypatch) -> None:
    app = create_app(settings)
    monkeypatch.setattr(app.state.service, "accounts_snapshot", lambda: {"accounts": [], "inspection": {}})
    add_user(app, settings, 2, "sk-cpa-user-two-secret")
    app.state.service.create_cycle("manual-web", "1970-01-01T08:00", "1970-01-02T08:00", 1000)
    client = TestClient(app, base_url="https://billing.example")
    payload = {
        "cycle": "manual-web",
        "pool_id": 1,
        "telegram_user_id": 2,
        "amount_usd": "1.234567891",
        "reason": "补录测试用量",
    }

    assert client.post("/api/admin/manual-usage-adjustments", json=payload).status_code == 401
    login = client.post("/auth/admin/login", json={"management_token": settings.admin_token})
    csrf = login.json()["csrf_token"]
    assert client.post("/api/admin/manual-usage-adjustments", json=payload).status_code == 403
    created = client.post(
        "/api/admin/manual-usage-adjustments",
        headers={"X-CSRF-Token": csrf},
        json=payload,
    )
    assert created.status_code == 200
    adjustment_id = created.json()["id"]
    assert adjustment_id >= 1

    snapshot = client.get("/api/admin/snapshot").json()["admin"]
    row = snapshot["manual_usage_adjustments"][0]
    assert row["cycle"] == "manual-web"
    assert row["pool"] == "default-cpa"
    assert row["user_id"] == 2
    assert row["amount_usd"] == "1.234567891"
    assert row["reason"] == "补录测试用量"
    assert row["editable"] is True
    assert row["updated_at"] is None
    assert snapshot["audits"][0]["operation"] == "manual-usage.create"

    updated_payload = {**payload, "amount_usd": "2.500000001", "reason": "更新后的补录说明"}
    assert client.put(f"/api/admin/manual-usage-adjustments/{adjustment_id}", json=updated_payload).status_code == 403
    updated = client.put(
        f"/api/admin/manual-usage-adjustments/{adjustment_id}",
        headers={"X-CSRF-Token": csrf},
        json=updated_payload,
    )
    assert updated.status_code == 200
    snapshot = client.get("/api/admin/snapshot").json()["admin"]
    row = snapshot["manual_usage_adjustments"][0]
    assert row["amount_usd"] == "2.500000001"
    assert row["reason"] == "更新后的补录说明"
    assert row["updated_at"] is not None
    assert snapshot["audits"][0]["operation"] == "manual-usage.update"

    invalid_precision = client.post(
        "/api/admin/manual-usage-adjustments",
        headers={"X-CSRF-Token": csrf},
        json={**payload, "amount_usd": "0.0000000001"},
    )
    assert invalid_precision.status_code == 400
    assert "九位小数" in invalid_precision.json()["error"]
