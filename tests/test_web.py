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

    logout = client.post("/auth/logout", headers={"X-CSRF-Token": login["csrf_token"]})
    assert logout.status_code == 204
    assert client.get("/api/session").status_code == 401


def test_api_key_admin_flag_does_not_grant_web_admin(settings, monkeypatch) -> None:
    app = create_app(settings)
    add_user(app, settings, 1, "sk-cpa-telegram-admin-secret", is_admin=True)
    monkeypatch.setattr(app.state.service.cpa, "list_keys", lambda: ["sk-cpa-telegram-admin-secret"])
    client = TestClient(app, base_url="https://billing.example")

    login_user(client, "sk-cpa-telegram-admin-secret")
    assert client.get("/api/session").json()["is_admin"] is False
    admin_page = client.get("/admin", follow_redirects=False)
    assert admin_page.status_code == 303
    assert admin_page.headers["location"] == "/admin/login"
    assert client.get("/api/admin/session").status_code == 401


def test_admin_uses_independent_token_and_csrf(settings) -> None:
    app = create_app(settings)
    client = TestClient(app, base_url="https://billing.example")

    invalid = client.post("/auth/admin/login", json={"management_token": "wrong"})
    assert invalid.status_code == 401
    assert invalid.json()["detail"] == "管理 token 无效。"

    response = client.post("/auth/admin/login", json={"management_token": settings.admin_token})
    assert response.status_code == 200
    csrf = response.json()["csrf_token"]
    assert client.get("/api/admin/session").json()["is_admin"] is True
    assert client.get("/api/session").status_code == 401

    missing_csrf = client.post("/api/admin/cycles/cycle0/preview", json={})
    assert missing_csrf.status_code == 403
    assert "CSRF" in missing_csrf.json()["detail"]
    logout = client.post("/auth/admin/logout", headers={"X-CSRF-Token": csrf})
    assert logout.status_code == 204
    assert client.get("/api/admin/session").status_code == 401


def test_site_mutations_require_user_csrf_and_expose_no_quota_reset(settings, monkeypatch) -> None:
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
    assert client.post("/api/site/accounts/reset", json={}).status_code in {404, 405}


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
