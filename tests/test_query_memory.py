from __future__ import annotations

import pytest
from sqlalchemy import select

from cpa_billing.models import APIKey, CPAMPSource, KeyOwnershipPeriod, RawUsageEvent, TelegramUser
from cpa_billing.security import cpamp_key_hash, mask_hash


def add_events(service, events):
    with service.db.session() as session:
        source_id = session.scalar(select(CPAMPSource.id))
        session.add_all([
            RawUsageEvent(source_id=source_id, source_event_id=index, event_hash=f"memory-{index}",
                          occurred_at_ms=timestamp, timestamp="test", model="test", api_key_hash=key,
                          total_tokens=tokens, imported_at_ms=timestamp)
            for index, (timestamp, key, tokens) in enumerate(events, 1)
        ])


def test_hourly_sql_preserves_historical_ownership_and_bucket_boundaries(service, monkeypatch):
    now = 100 * 3_600_000
    start = now - 2 * 3_600_000
    monkeypatch.setattr("cpa_billing.services.now_ms", lambda: now)
    key_hash = cpamp_key_hash("changing-owner")
    with service.db.session() as session:
        session.add_all([
            TelegramUser(telegram_user_id=2, username="old", last_seen_at_ms=now),
            TelegramUser(telegram_user_id=3, username="new", last_seen_at_ms=now),
        ])
        session.flush()
        key = APIKey(cpamp_hash=key_hash, masked_value="masked", display_name="external", current_owner_id=3,
                     created_at_ms=start, status="active")
        session.add(key)
        session.flush()
        session.add_all([
            KeyOwnershipPeriod(api_key_id=key.id, telegram_user_id=2, valid_from_ms=start,
                               valid_to_ms=start + 3_600_000, source="test", created_at_ms=start),
            KeyOwnershipPeriod(api_key_id=key.id, telegram_user_id=3, valid_from_ms=start + 3_600_000,
                               valid_to_ms=now, source="test", created_at_ms=start),
        ])
    add_events(service, [(start - 1, key_hash, 999), (start, key_hash, 10),
                         (start + 3_600_000 - 1, key_hash, 20), (start + 3_600_000, key_hash, 40),
                         (now, key_hash, 5), (now + 1000, key_hash, 7),
                         (start, "unknown-hash", 3), (start, None, 2), (start, "", 1)])
    labels, series = service.hourly_usage(2)
    by_name = {item["name"]: item for item in series}
    assert [item["total"] for item in series] == [40, 30, 12, 3, 3]
    assert by_name["@old"]["values"] == {labels[0]: 30}
    assert by_name["@new"]["values"] == {labels[1]: 40}
    assert by_name["external"]["values"] == {labels[2]: 12}
    assert by_name[mask_hash("unknown-hash")]["total"] == 3
    assert by_name["未知 API Key"]["total"] == 3


def test_hourly_top_series_uses_whole_window_totals_and_bounds_results(service, monkeypatch):
    now = 100 * 3_600_000
    monkeypatch.setattr("cpa_billing.services.now_ms", lambda: now)
    start = now - 24 * 3_600_000
    # The winning key is never the largest single request or hourly bucket.
    events = [(start + i * 3_600_000, "consistent", 10) for i in range(24)]
    events += [(start, f"spike-{i}", 200 - i) for i in range(20)]
    add_events(service, events)
    labels, series = service.hourly_usage(24, limit=1)
    assert len(labels) == 25
    assert len(series) == 1
    assert series[0]["name"] == mask_hash("consistent")
    assert series[0]["total"] == 240
    assert len(series[0]["values"]) == 24
    assert len(service.hourly_usage()[1]) == 12


def test_hourly_empty_window_and_invalid_bounds(service):
    assert service.hourly_usage()[1] == []
    for hours, limit in [(0, 12), (169, 12), (24, 0), (24, 101)]:
        with pytest.raises(ValueError):
            service.hourly_usage(hours, limit)


def test_user_queries_count_active_keys_and_limit_in_sql(service):
    with service.db.session() as session:
        session.add_all([
            TelegramUser(telegram_user_id=1, username="older", registered_at_ms=1, last_seen_at_ms=1),
            TelegramUser(telegram_user_id=2, username="newer", registered_at_ms=0, last_seen_at_ms=2),
            TelegramUser(telegram_user_id=3, last_seen_at_ms=3),
        ])
        session.flush()
        session.add_all([
            APIKey(cpamp_hash=f"key-{i}", masked_value="masked", status=status, current_owner_id=2,
                   created_at_ms=1)
            for i, status in enumerate(["active", "active", "revoked"])
        ])
    assert service.user_stats() == {"users": 3, "registered": 1}
    assert service.list_users(2) == [
        {"id": 3, "username": "-", "registered": False, "keys": 0},
        {"id": 2, "username": "newer", "registered": False, "keys": 2},
    ]


def test_bot_database_pool_and_disk_temporaries(settings):
    from sqlalchemy.exc import TimeoutError
    from cpa_billing.database import Database

    db = Database(settings.database_path, pool_size=1, max_overflow=0, pool_timeout=0)
    try:
        with db.engine.connect() as connection:
            assert connection.exec_driver_sql("pragma temp_store").scalar_one() == 1
            with pytest.raises(TimeoutError):
                # With a connection already in use, another must wait rather
                # than silently create an overflow connection.
                db.engine.connect()
    finally:
        db.engine.dispose()
