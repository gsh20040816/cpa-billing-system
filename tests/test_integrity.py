from __future__ import annotations

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError

from cpa_billing.integrity import VIEW_NAMES, install_integrity_objects
from cpa_billing.models import (
    Adjustment,
    APIKey,
    AuditLog,
    BillingCycle,
    KeyOwnershipPeriod,
    ModelPriceRule,
    PricingVersion,
    RatedEvent,
    RawUsageEvent,
    Statement,
    StatementLine,
)
from cpa_billing.security import cpamp_key_hash
from test_services import create_owner, insert_event


def closed_cycle(service, settings) -> int:
    create_owner(service, "key", 2, 0)
    insert_event(settings, cpamp_key_hash("key"), 1000)
    service.sync_cpamp(); service.rate_events()
    service.create_cycle("cycle", "1970-01-01T08:00", "1970-01-02T08:00", 10000)
    service.close_cycle("cycle", 1, False)
    with service.db.session() as session:
        return session.scalar(select(BillingCycle.id).where(BillingCycle.name == "cycle"))


def rejected(service, statement) -> str:
    with pytest.raises(IntegrityError) as caught:
        with service.db.session() as session:
            session.execute(statement)
    return str(caught.value)


def test_audit_log_is_append_only(service) -> None:
    with service.db.session() as session:
        session.add(AuditLog(operator_type="test", operator_id="1", operation="op", target="t", created_at_ms=1))
    assert "append-only" in rejected(service, update(AuditLog).values(reason="rewritten"))
    assert "append-only" in rejected(service, delete(AuditLog))


def test_closed_cycle_bills_and_children_are_frozen(service, settings) -> None:
    cycle_id = closed_cycle(service, settings)
    assert "closed billing cycle is frozen" in rejected(
        service, update(BillingCycle).where(BillingCycle.id == cycle_id).values(status="open"))
    assert "final statement is frozen" in rejected(service, update(Statement).values(amount_cents=0))
    assert "final statement is frozen" in rejected(service, delete(StatementLine))
    assert "closed billing cycle is frozen" in rejected(service, Adjustment.__table__.insert().values(
        cycle_id=cycle_id, telegram_user_id=2, amount_cents=1, reason="late", created_at_ms=1))
    with service.db.session() as session:
        version_id = session.scalar(select(BillingCycle.pricing_version_id).where(BillingCycle.id == cycle_id))
    assert "prices of a closed billing cycle are frozen" in rejected(
        service, update(ModelPriceRule).where(ModelPriceRule.pricing_version_id == version_id).values(input_nano_per_token=0))
    # The service still reads the frozen bill.
    assert service.preview_cycle("cycle")[0].final is True


def test_usage_records_are_immutable(service, settings) -> None:
    create_owner(service, "key", 2, 0)
    insert_event(settings, cpamp_key_hash("key"), 1000)
    service.sync_cpamp(); service.rate_events()
    assert "raw usage events are immutable" in rejected(service, update(RawUsageEvent).values(input_tokens=1))
    assert "raw usage events are immutable" in rejected(service, delete(RawUsageEvent))
    assert "rated events are immutable" in rejected(service, update(RatedEvent).values(rated_weight_nano_usd=0))
    # CPAMP backfills of optional provenance columns remain allowed.
    with service.db.session() as session:
        session.execute(update(RawUsageEvent).values(response_service_tier="default"))


def test_key_ownership_periods_cannot_overlap(service) -> None:
    create_owner(service, "key", 2, 0)
    with service.db.session() as session:
        key_id = session.scalar(select(APIKey.id))
    overlapping = KeyOwnershipPeriod.__table__.insert().values(
        api_key_id=key_id, telegram_user_id=2, valid_from_ms=500, source="test", created_at_ms=1)
    assert "overlap" in rejected(service, overlapping)
    reversed_period = KeyOwnershipPeriod.__table__.insert().values(
        api_key_id=key_id, telegram_user_id=2, valid_from_ms=-10, valid_to_ms=-20, source="test", created_at_ms=1)
    assert "ck_key_ownership_periods_order" in rejected(service, reversed_period)
    # Closing the open period at t and opening a new one at t is a valid hand-over.
    with service.db.session() as session:
        session.execute(update(KeyOwnershipPeriod).values(valid_to_ms=700))
        session.add(KeyOwnershipPeriod(api_key_id=key_id, telegram_user_id=2, valid_from_ms=700, source="test", created_at_ms=1))


def test_check_constraints_and_single_active_price_version(service) -> None:
    create_owner(service, "key", 2, 0)
    assert "ck_api_keys_status" in rejected(service, update(APIKey).values(status="deleted"))
    assert "ck_billing_cycles_period_order" in rejected(service, BillingCycle.__table__.insert().values(
        name="bad", start_at_ms=10, end_at_ms=10, timezone="UTC", status="open", pricing_version_id=1,
        gradient_rule_id=1, tiers_json="[]", created_at_ms=1))
    message = rejected(service, PricingVersion.__table__.insert().values(name="second", status="active", source="test", created_at_ms=1))
    assert "uq_pricing_versions_single_active" in message or "UNIQUE" in message.upper()


def test_reporting_views_expose_bills_without_secrets(service, settings) -> None:
    closed_cycle(service, settings)
    with service.db.session() as session:
        bills = session.execute(text("SELECT cycle_name, telegram_user_id, amount_yuan, final FROM v_user_cycle_bills")).all()
        assert [(row[0], row[1], float(row[2]), bool(row[3])) for row in bills] == [("cycle", 2, 100.0, True)]
        usage = session.execute(text("SELECT usage_date, request_count, total_tokens FROM v_daily_user_model_usage")).all()
        assert [(row[0], row[1], row[2]) for row in usage] == [("1970-01-01", 1, 1100)]
        columns = session.execute(text("SELECT * FROM v_api_key_owners")).keys()
        assert "cpamp_hash" not in columns and "login_fingerprint" not in columns
        for name in VIEW_NAMES:
            session.execute(text(f"SELECT * FROM {name}")).all()


def test_installation_is_idempotent(service) -> None:
    with service.db.engine.begin() as connection:
        assert install_integrity_objects(connection) == []
