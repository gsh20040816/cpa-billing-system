from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class CPAMPSource(Base):
    __tablename__ = "cpamp_sources"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    schema_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    created_at_ms: Mapped[int] = mapped_column(BigInteger)


class SyncCheckpoint(Base):
    __tablename__ = "sync_checkpoints"
    source_id: Mapped[int] = mapped_column(ForeignKey("cpamp_sources.id"), primary_key=True)
    last_event_id: Mapped[int] = mapped_column(BigInteger, default=0)
    last_event_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    last_success_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    backlog: Mapped[int] = mapped_column(BigInteger, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)


class RawUsageEvent(Base):
    __tablename__ = "raw_usage_events"
    __table_args__ = (
        UniqueConstraint("source_id", "source_event_id"),
        UniqueConstraint("source_id", "event_hash"),
        Index("idx_raw_events_time", "occurred_at_ms"),
        Index("idx_raw_events_key_time", "api_key_hash", "occurred_at_ms"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("cpamp_sources.id"))
    source_event_id: Mapped[int] = mapped_column(BigInteger)
    event_hash: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(200))
    occurred_at_ms: Mapped[int] = mapped_column(BigInteger)
    timestamp: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str | None] = mapped_column(String(80))
    executor_type: Mapped[str | None] = mapped_column(String(120))
    model: Mapped[str] = mapped_column(String(160))
    requested_model: Mapped[str | None] = mapped_column(String(160))
    resolved_model: Mapped[str | None] = mapped_column(String(160))
    service_tier: Mapped[str | None] = mapped_column(String(40))
    api_key_hash: Mapped[str | None] = mapped_column(String(64))
    source_hash: Mapped[str | None] = mapped_column(String(64))
    source_label: Mapped[str | None] = mapped_column(String(240))
    account_snapshot: Mapped[str | None] = mapped_column(String(240))
    auth_index: Mapped[str | None] = mapped_column(String(160))
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    reasoning_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cached_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_creation_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    total_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    failed: Mapped[bool] = mapped_column(Boolean, default=False)
    fail_status_code: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(BigInteger)
    ttft_ms: Mapped[int | None] = mapped_column(BigInteger)
    response_metadata_json: Mapped[str | None] = mapped_column(Text)
    quota_used_percent: Mapped[int | None] = mapped_column(Integer)
    quota_recover_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    quota_plan_type: Mapped[str | None] = mapped_column(String(80))
    imported_at_ms: Mapped[int] = mapped_column(BigInteger)


class DeadLetter(Base):
    __tablename__ = "dead_letters"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("cpamp_sources.id"))
    source_event_id: Mapped[int | None] = mapped_column(BigInteger)
    error: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[str] = mapped_column(Text)
    created_at_ms: Mapped[int] = mapped_column(BigInteger)
    resolved_at_ms: Mapped[int | None] = mapped_column(BigInteger)


class TelegramUser(Base):
    __tablename__ = "telegram_users"
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(80))
    first_name: Mapped[str | None] = mapped_column(String(120))
    last_name: Mapped[str | None] = mapped_column(String(120))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    manual_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    registered_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    last_seen_at_ms: Mapped[int] = mapped_column(BigInteger)


class GroupMembership(Base):
    __tablename__ = "group_memberships"
    telegram_user_id: Mapped[int] = mapped_column(ForeignKey("telegram_users.telegram_user_id"), primary_key=True)
    group_chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    status: Mapped[str] = mapped_column(String(40))
    legal: Mapped[bool] = mapped_column(Boolean)
    updated_at_ms: Mapped[int] = mapped_column(BigInteger)


class AllowedChat(Base):
    __tablename__ = "allowed_chats"
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    note: Mapped[str | None] = mapped_column(String(200))
    updated_at_ms: Mapped[int] = mapped_column(BigInteger)


class APIKey(Base):
    __tablename__ = "api_keys"
    __table_args__ = (Index("idx_api_keys_owner_status", "current_owner_id", "status"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cpamp_hash: Mapped[str] = mapped_column(String(64), unique=True)
    login_fingerprint: Mapped[str | None] = mapped_column(String(64), unique=True)
    masked_value: Mapped[str] = mapped_column(String(80))
    display_name: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default="active")
    current_owner_id: Mapped[int | None] = mapped_column(ForeignKey("telegram_users.telegram_user_id"))
    billing_multiplier_ppm: Mapped[int | None] = mapped_column(BigInteger)
    present_in_cpa: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen_in_cpa_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    created_at_ms: Mapped[int] = mapped_column(BigInteger)
    revoked_at_ms: Mapped[int | None] = mapped_column(BigInteger)


class KeyOwnershipPeriod(Base):
    __tablename__ = "key_ownership_periods"
    __table_args__ = (Index("idx_ownership_key_period", "api_key_id", "valid_from_ms", "valid_to_ms"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    api_key_id: Mapped[int] = mapped_column(ForeignKey("api_keys.id"))
    telegram_user_id: Mapped[int] = mapped_column(ForeignKey("telegram_users.telegram_user_id"))
    valid_from_ms: Mapped[int] = mapped_column(BigInteger)
    valid_to_ms: Mapped[int | None] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str | None] = mapped_column(Text)
    operator_user_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at_ms: Mapped[int] = mapped_column(BigInteger)


class KeyActionRequest(Base):
    __tablename__ = "key_action_requests"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(ForeignKey("telegram_users.telegram_user_id"))
    action: Mapped[str] = mapped_column(String(20))
    target_api_key_id: Mapped[int | None] = mapped_column(ForeignKey("api_keys.id"))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at_ms: Mapped[int] = mapped_column(BigInteger)
    expires_at_ms: Mapped[int] = mapped_column(BigInteger)
    confirmed_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    result_masked_key: Mapped[str | None] = mapped_column(String(80))


class PricingVersion(Base):
    __tablename__ = "pricing_versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    source: Mapped[str] = mapped_column(String(80))
    created_at_ms: Mapped[int] = mapped_column(BigInteger)
    activated_at_ms: Mapped[int | None] = mapped_column(BigInteger)


class ModelPriceRule(Base):
    __tablename__ = "model_price_rules"
    pricing_version_id: Mapped[int] = mapped_column(ForeignKey("pricing_versions.id"), primary_key=True)
    model: Mapped[str] = mapped_column(String(160), primary_key=True)
    input_nano_per_token: Mapped[int] = mapped_column(BigInteger)
    output_nano_per_token: Mapped[int] = mapped_column(BigInteger)
    cache_read_nano_per_token: Mapped[int] = mapped_column(BigInteger)
    cache_creation_nano_per_token: Mapped[int] = mapped_column(BigInteger)
    input_configured: Mapped[bool] = mapped_column(Boolean, default=True)
    output_configured: Mapped[bool] = mapped_column(Boolean, default=True)
    cache_read_configured: Mapped[bool] = mapped_column(Boolean, default=False)
    cache_creation_configured: Mapped[bool] = mapped_column(Boolean, default=False)
    priority_input_nano_per_token: Mapped[int | None] = mapped_column(BigInteger)
    priority_output_nano_per_token: Mapped[int | None] = mapped_column(BigInteger)
    priority_cache_read_nano_per_token: Mapped[int | None] = mapped_column(BigInteger)
    priority_cache_creation_nano_per_token: Mapped[int | None] = mapped_column(BigInteger)
    flex_input_nano_per_token: Mapped[int | None] = mapped_column(BigInteger)
    flex_output_nano_per_token: Mapped[int | None] = mapped_column(BigInteger)
    long_threshold_tokens: Mapped[int | None] = mapped_column(BigInteger)
    long_input_multiplier_ppm: Mapped[int] = mapped_column(BigInteger, default=1_000_000)
    long_output_multiplier_ppm: Mapped[int] = mapped_column(BigInteger, default=1_000_000)
    raw_json: Mapped[str | None] = mapped_column(Text)


class ResourcePool(Base):
    __tablename__ = "resource_pools"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at_ms: Mapped[int] = mapped_column(BigInteger)


class PoolAssignmentRule(Base):
    __tablename__ = "pool_assignment_rules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pool_id: Mapped[int] = mapped_column(ForeignKey("resource_pools.id"))
    priority: Mapped[int] = mapped_column(Integer, default=100)
    auth_index_pattern: Mapped[str | None] = mapped_column(String(200))
    model_pattern: Mapped[str | None] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class RatedEvent(Base):
    __tablename__ = "rated_events"
    __table_args__ = (
        UniqueConstraint("raw_event_id", "pricing_version_id"),
        Index("idx_rated_cycle", "occurred_at_ms", "pool_id", "telegram_user_id"),
        Index("idx_rated_user_time", "telegram_user_id", "occurred_at_ms"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    raw_event_id: Mapped[int] = mapped_column(ForeignKey("raw_usage_events.id"))
    pricing_version_id: Mapped[int] = mapped_column(ForeignKey("pricing_versions.id"))
    pool_id: Mapped[int | None] = mapped_column(ForeignKey("resource_pools.id"))
    telegram_user_id: Mapped[int | None] = mapped_column(ForeignKey("telegram_users.telegram_user_id"))
    occurred_at_ms: Mapped[int] = mapped_column(BigInteger)
    rated_weight_nano_usd: Mapped[int] = mapped_column(BigInteger)
    long_context_applied: Mapped[bool] = mapped_column(Boolean)
    service_tier: Mapped[str] = mapped_column(String(40))
    calculation_json: Mapped[str] = mapped_column(Text)
    rated_at_ms: Mapped[int] = mapped_column(BigInteger)


class GradientRule(Base):
    __tablename__ = "gradient_rules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    description: Mapped[str | None] = mapped_column(String(300))
    tiers_json: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at_ms: Mapped[int] = mapped_column(BigInteger)
    updated_at_ms: Mapped[int] = mapped_column(BigInteger)


class BillingCycle(Base):
    __tablename__ = "billing_cycles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    start_at_ms: Mapped[int] = mapped_column(BigInteger)
    end_at_ms: Mapped[int] = mapped_column(BigInteger)
    timezone: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(20), default="open")
    pricing_version_id: Mapped[int] = mapped_column(ForeignKey("pricing_versions.id"))
    gradient_rule_id: Mapped[int] = mapped_column(ForeignKey("gradient_rules.id"))
    tiers_json: Mapped[str] = mapped_column(Text)
    data_quality_waiver: Mapped[str | None] = mapped_column(Text)
    created_at_ms: Mapped[int] = mapped_column(BigInteger)
    closed_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    closed_by: Mapped[int | None] = mapped_column(BigInteger)


class CyclePoolCost(Base):
    __tablename__ = "cycle_pool_costs"
    cycle_id: Mapped[int] = mapped_column(ForeignKey("billing_cycles.id"), primary_key=True)
    pool_id: Mapped[int] = mapped_column(ForeignKey("resource_pools.id"), primary_key=True)
    fixed_cost_cents: Mapped[int] = mapped_column(BigInteger)


class Statement(Base):
    __tablename__ = "statements"
    __table_args__ = (UniqueConstraint("cycle_id", "telegram_user_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("billing_cycles.id"))
    telegram_user_id: Mapped[int] = mapped_column(ForeignKey("telegram_users.telegram_user_id"))
    actual_weight_nano_usd: Mapped[int] = mapped_column(BigInteger)
    billed_weight_nano_usd: Mapped[int] = mapped_column(BigInteger)
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    adjustment_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    generated_at_ms: Mapped[int] = mapped_column(BigInteger)
    final: Mapped[bool] = mapped_column(Boolean, default=False)


class StatementLine(Base):
    __tablename__ = "statement_lines"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    statement_id: Mapped[int] = mapped_column(ForeignKey("statements.id"))
    pool_id: Mapped[int] = mapped_column(ForeignKey("resource_pools.id"))
    actual_weight_nano_usd: Mapped[int] = mapped_column(BigInteger)
    billed_weight_nano_usd: Mapped[int] = mapped_column(BigInteger)
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    api_key_count: Mapped[int] = mapped_column(Integer)


class MeteredKeyCharge(Base):
    __tablename__ = "metered_key_charges"
    __table_args__ = (UniqueConstraint("cycle_id", "pool_id", "api_key_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("billing_cycles.id"))
    pool_id: Mapped[int] = mapped_column(ForeignKey("resource_pools.id"))
    api_key_id: Mapped[int] = mapped_column(ForeignKey("api_keys.id"))
    actual_weight_nano_usd: Mapped[int] = mapped_column(BigInteger)
    multiplier_ppm: Mapped[int] = mapped_column(BigInteger)
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    generated_at_ms: Mapped[int] = mapped_column(BigInteger)
    final: Mapped[bool] = mapped_column(Boolean, default=False)


class Adjustment(Base):
    __tablename__ = "adjustments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("billing_cycles.id"))
    telegram_user_id: Mapped[int] = mapped_column(ForeignKey("telegram_users.telegram_user_id"))
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(Text)
    operator_user_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at_ms: Mapped[int] = mapped_column(BigInteger)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    operator_type: Mapped[str] = mapped_column(String(20))
    operator_id: Mapped[str] = mapped_column(String(120))
    operation: Mapped[str] = mapped_column(String(120))
    target: Mapped[str] = mapped_column(String(240))
    before_json: Mapped[str | None] = mapped_column(Text)
    after_json: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at_ms: Mapped[int] = mapped_column(BigInteger)


class ReconciliationRun(Base):
    __tablename__ = "reconciliation_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cycle_id: Mapped[int | None] = mapped_column(ForeignKey("billing_cycles.id"))
    result_json: Mapped[str] = mapped_column(Text)
    ok: Mapped[bool] = mapped_column(Boolean)
    created_at_ms: Mapped[int] = mapped_column(BigInteger)


class WebSession(Base):
    __tablename__ = "web_sessions"
    session_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(ForeignKey("telegram_users.telegram_user_id"))
    api_key_id: Mapped[int] = mapped_column(ForeignKey("api_keys.id"))
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at_ms: Mapped[int] = mapped_column(BigInteger)
    expires_at_ms: Mapped[int] = mapped_column(BigInteger)
    revoked_at_ms: Mapped[int | None] = mapped_column(BigInteger)


class AdminWebSession(Base):
    __tablename__ = "admin_web_sessions"
    session_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    credential_fingerprint: Mapped[str] = mapped_column(String(64))
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at_ms: Mapped[int] = mapped_column(BigInteger)
    expires_at_ms: Mapped[int] = mapped_column(BigInteger)
    revoked_at_ms: Mapped[int | None] = mapped_column(BigInteger)
