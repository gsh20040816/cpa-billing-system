from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from cpa_billing.models import APIKey, KeyOwnershipPeriod, TelegramUser
from cpa_billing.security import cpamp_key_hash, login_fingerprint, mask_api_key
from cpa_billing.web import create_app


def add_user(app, settings, user_id: int, raw_key: str) -> None:
    service = app.state.service
    with service.db.session() as session:
        session.add(TelegramUser(telegram_user_id=user_id, username=f"user{user_id}", registered_at_ms=1, last_seen_at_ms=1))
        session.flush()
        key = APIKey(cpamp_hash=cpamp_key_hash(raw_key), login_fingerprint=login_fingerprint(raw_key, settings.key_pepper),
                     masked_value=mask_api_key(raw_key), status="active", current_owner_id=user_id, created_at_ms=1)
        session.add(key); session.flush()
        session.add(KeyOwnershipPeriod(api_key_id=key.id, telegram_user_id=user_id, valid_from_ms=1, source="test", created_at_ms=1))


def test_web_has_no_registration_and_hides_other_users_keys(settings, monkeypatch) -> None:
    app = create_app(settings)
    add_user(app, settings, 2, "sk-cpa-user-two-secret")
    add_user(app, settings, 3, "sk-cpa-user-three-secret")
    monkeypatch.setattr(app.state.service.cpa, "list_keys", lambda: ["sk-cpa-user-two-secret", "sk-cpa-user-three-secret"])
    client = TestClient(app, base_url="https://billing.example")
    unauthenticated = client.get("/", follow_redirects=False)
    assert unauthenticated.status_code == 303
    assert unauthenticated.headers["location"] == "/login"
    assert client.get("/register").status_code == 404
    response = client.post("/auth/api-key/login", data={"api_key": "sk-cpa-user-two-secret"}, follow_redirects=False)
    assert response.status_code == 303
    client.cookies.update(response.cookies)
    own = client.get("/api/users/2/summary").json()
    other = client.get("/api/users/3/summary").json()
    assert "keys" in own
    assert "keys" not in other
    assert "sk-cpa-user-three-secret" not in str(other)
