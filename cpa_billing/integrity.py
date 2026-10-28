"""Database-enforced billing invariants (triggers) and reporting views.

The application already enforces these rules; the triggers keep them true for any client of the database,
including manual SQL. Installation is idempotent and only creates missing objects, so it runs on every start.
SQLite table rebuilds (Alembic batch mode) drop a table's triggers; the next start reinstalls them, and a
migration that rebuilds a table referenced by a view must call drop_integrity_objects() first.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text

from .models import TOKEN_COLUMNS

# Columns a pricing rerate or backfill never changes once CPAMP usage is imported.
RAW_EVENT_IMMUTABLE_COLUMNS = (
    "source_id", "source_event_id", "event_hash", "occurred_at_ms", "model", "api_key_hash", "auth_index", "failed",
    *TOKEN_COLUMNS,
)

# Tables whose rows belong to one billing cycle and are frozen once it closes.
CYCLE_CHILD_TABLES = ("adjustments", "manual_usage_adjustments", "cycle_pool_costs", "cycle_upstream_costs", "cycle_groups")

VIEW_NAMES = (
    "v_user_cycle_bills", "v_statement_line_details", "v_cycle_group_summary", "v_daily_user_model_usage", "v_api_key_owners",
)

ADVISORY_LOCK_ID = 7_202_610_170  # Arbitrary constant shared by all billing processes.


def _views(day_expression: str, false: str) -> dict[str, str]:
    return {
        # One row per user per cycle: the monthly bill.
        "v_user_cycle_bills": f"""
            SELECT s.id AS statement_id, c.id AS cycle_id, c.name AS cycle_name, c.status AS cycle_status,
                   c.start_at_ms, c.end_at_ms, s.telegram_user_id, u.username,
                   s.actual_weight_nano_usd / 1000000000.0 AS actual_usd,
                   s.billed_weight_nano_usd / 1000000000.0 AS billed_usd,
                   s.adjustment_cents / 100.0 AS adjustment_yuan,
                   s.amount_cents / 100.0 AS amount_yuan,
                   s.final, s.generated_at_ms
            FROM statements s
            JOIN billing_cycles c ON c.id = s.cycle_id
            JOIN telegram_users u ON u.telegram_user_id = s.telegram_user_id""",
        # Bill lines with the pool and group they were allocated from.
        "v_statement_line_details": """
            SELECT l.id AS line_id, s.cycle_id, c.name AS cycle_name, s.telegram_user_id,
                   l.pool_id, p.name AS pool_name, l.group_id, g.group_name,
                   l.actual_weight_nano_usd / 1000000000.0 AS actual_usd,
                   l.billed_weight_nano_usd / 1000000000.0 AS billed_usd,
                   l.amount_cents / 100.0 AS amount_yuan, l.api_key_count
            FROM statement_lines l
            JOIN statements s ON s.id = l.statement_id
            JOIN billing_cycles c ON c.id = s.cycle_id
            JOIN resource_pools p ON p.id = l.pool_id
            LEFT JOIN cycle_groups g ON g.cycle_id = s.cycle_id AND g.group_id = l.group_id""",
        # Cost reconciliation per upstream account group and cycle.
        "v_cycle_group_summary": """
            SELECT c.id AS cycle_id, c.name AS cycle_name, c.status AS cycle_status, g.group_id, g.group_name, g.is_default,
                   (g.fixed_cost_cents + g.dynamic_cost_cents) / 100.0 AS total_cost_yuan,
                   g.metered_amount_cents / 100.0 AS metered_yuan,
                   g.residual_cost_cents / 100.0 AS residual_yuan,
                   g.member_amount_cents / 100.0 AS member_yuan,
                   g.surplus_cents / 100.0 AS surplus_yuan,
                   g.unallocated_cents / 100.0 AS unallocated_yuan
            FROM cycle_groups g
            JOIN billing_cycles c ON c.id = g.cycle_id""",
        # Usage statistics by day (Asia/Shanghai), user and model under the active price version.
        "v_daily_user_model_usage": f"""
            SELECT {day_expression} AS usage_date, r.telegram_user_id, e.model,
                   COUNT(*) AS request_count,
                   SUM(CASE WHEN e.failed = {false} THEN 0 ELSE 1 END) AS failed_count,
                   SUM(e.input_tokens) AS input_tokens, SUM(e.output_tokens) AS output_tokens,
                   SUM(e.total_tokens) AS total_tokens,
                   SUM(r.rated_weight_nano_usd) / 1000000000.0 AS cost_usd
            FROM rated_events r
            JOIN raw_usage_events e ON e.id = r.raw_event_id
            JOIN pricing_versions v ON v.id = r.pricing_version_id AND v.status = 'active'
            GROUP BY {day_expression}, r.telegram_user_id, e.model""",
        # Key ownership without hashes or login fingerprints, for read-only reporting roles.
        "v_api_key_owners": """
            SELECT k.id AS api_key_id, k.masked_value, k.display_name, k.status, k.current_owner_id, u.username,
                   k.billing_multiplier_ppm / 1000000.0 AS billing_multiplier, k.created_at_ms, k.revoked_at_ms
            FROM api_keys k
            LEFT JOIN telegram_users u ON u.telegram_user_id = k.current_owner_id""",
    }


def _sqlite_triggers() -> dict[str, str]:
    def reject(message: str) -> str:
        return f"SELECT RAISE(ABORT, 'integrity: {message}');"

    triggers = {
        "trg_audit_logs_no_update": f"BEFORE UPDATE ON audit_logs BEGIN {reject('audit_logs is append-only')} END",
        "trg_audit_logs_no_delete": f"BEFORE DELETE ON audit_logs BEGIN {reject('audit_logs is append-only')} END",
        "trg_billing_cycles_closed_update": (
            f"BEFORE UPDATE ON billing_cycles WHEN OLD.status = 'closed' BEGIN {reject('closed billing cycle is frozen')} END"
        ),
        "trg_billing_cycles_closed_delete": (
            f"BEFORE DELETE ON billing_cycles WHEN OLD.status = 'closed' BEGIN {reject('closed billing cycle is frozen')} END"
        ),
        "trg_pricing_versions_retired": (
            "BEFORE UPDATE OF status ON pricing_versions WHEN OLD.status = 'retired' AND NEW.status <> 'retired' "
            f"BEGIN {reject('retired pricing version cannot be reactivated')} END"
        ),
        **{
            f"trg_model_price_rules_closed_{operation.lower()}": (
                f"BEFORE {operation} ON model_price_rules WHEN EXISTS (SELECT 1 FROM billing_cycles "
                "WHERE pricing_version_id = OLD.pricing_version_id AND status = 'closed') "
                f"BEGIN {reject('prices of a closed billing cycle are frozen')} END"
            )
            for operation in ("UPDATE", "DELETE")
        },
        "trg_rated_events_no_update": f"BEFORE UPDATE ON rated_events BEGIN {reject('rated events are immutable')} END",
        "trg_raw_usage_events_no_delete": f"BEFORE DELETE ON raw_usage_events BEGIN {reject('raw usage events are immutable')} END",
        "trg_raw_usage_events_no_update": (
            "BEFORE UPDATE ON raw_usage_events WHEN "
            + " OR ".join(f"OLD.{column} IS NOT NEW.{column}" for column in RAW_EVENT_IMMUTABLE_COLUMNS)
            + f" BEGIN {reject('raw usage events are immutable')} END"
        ),
        "trg_key_ownership_no_overlap": (
            "BEFORE INSERT ON key_ownership_periods WHEN EXISTS ("
            "SELECT 1 FROM key_ownership_periods p WHERE p.api_key_id = NEW.api_key_id "
            "AND (p.valid_to_ms IS NULL OR p.valid_to_ms > NEW.valid_from_ms) "
            "AND (NEW.valid_to_ms IS NULL OR NEW.valid_to_ms > p.valid_from_ms)) "
            f"BEGIN {reject('key ownership periods overlap')} END"
        ),
    }
    for table in ("statements", "metered_key_charges"):
        for operation in ("UPDATE", "DELETE"):
            triggers[f"trg_{table}_final_{operation.lower()}"] = (
                f"BEFORE {operation} ON {table} WHEN OLD.final BEGIN {reject('final statement is frozen')} END"
            )
    final_parent = "(SELECT final FROM statements WHERE id = {row}.statement_id)"
    triggers["trg_statement_lines_final_insert"] = (
        f"BEFORE INSERT ON statement_lines WHEN {final_parent.format(row='NEW')} "
        f"BEGIN {reject('final statement is frozen')} END"
    )
    for operation in ("UPDATE", "DELETE"):
        triggers[f"trg_statement_lines_final_{operation.lower()}"] = (
            f"BEFORE {operation} ON statement_lines WHEN {final_parent.format(row='OLD')} "
            f"BEGIN {reject('final statement is frozen')} END"
        )
    closed = "EXISTS (SELECT 1 FROM billing_cycles WHERE id = {row}.cycle_id AND status = 'closed')"
    for table in CYCLE_CHILD_TABLES:
        triggers[f"trg_{table}_closed_insert"] = (
            f"BEFORE INSERT ON {table} WHEN {closed.format(row='NEW')} BEGIN {reject('closed billing cycle is frozen')} END"
        )
        triggers[f"trg_{table}_closed_update"] = (
            f"BEFORE UPDATE ON {table} WHEN {closed.format(row='OLD')} OR {closed.format(row='NEW')} "
            f"BEGIN {reject('closed billing cycle is frozen')} END"
        )
        triggers[f"trg_{table}_closed_delete"] = (
            f"BEFORE DELETE ON {table} WHEN {closed.format(row='OLD')} BEGIN {reject('closed billing cycle is frozen')} END"
        )
    return triggers


POSTGRESQL_FUNCTIONS = {
    "billing_reject": """
        CREATE OR REPLACE FUNCTION billing_reject() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'integrity: %', TG_ARGV[0] USING ERRCODE = 'integrity_constraint_violation';
        END $$""",
    "billing_statement_line_guard": """
        CREATE OR REPLACE FUNCTION billing_statement_line_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF (TG_OP <> 'INSERT' AND (SELECT final FROM statements WHERE id = OLD.statement_id))
               OR (TG_OP <> 'DELETE' AND (SELECT final FROM statements WHERE id = NEW.statement_id)) THEN
                RAISE EXCEPTION 'integrity: final statement is frozen' USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END $$""",
    "billing_cycle_child_guard": """
        CREATE OR REPLACE FUNCTION billing_cycle_child_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF (TG_OP <> 'INSERT' AND EXISTS (SELECT 1 FROM billing_cycles WHERE id = OLD.cycle_id AND status = 'closed'))
               OR (TG_OP <> 'DELETE' AND EXISTS (SELECT 1 FROM billing_cycles WHERE id = NEW.cycle_id AND status = 'closed')) THEN
                RAISE EXCEPTION 'integrity: closed billing cycle is frozen' USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END $$""",
    "billing_price_rule_guard": """
        CREATE OR REPLACE FUNCTION billing_price_rule_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM billing_cycles WHERE pricing_version_id = OLD.pricing_version_id AND status = 'closed') THEN
                RAISE EXCEPTION 'integrity: prices of a closed billing cycle are frozen' USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END $$""",
    "billing_key_ownership_guard": """
        CREATE OR REPLACE FUNCTION billing_key_ownership_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            -- Serialize inserts per key so two concurrent transfers cannot both pass the overlap check.
            PERFORM 1 FROM api_keys WHERE id = NEW.api_key_id FOR UPDATE;
            IF EXISTS (
                SELECT 1 FROM key_ownership_periods p
                WHERE p.api_key_id = NEW.api_key_id
                  AND (p.valid_to_ms IS NULL OR p.valid_to_ms > NEW.valid_from_ms)
                  AND (NEW.valid_to_ms IS NULL OR NEW.valid_to_ms > p.valid_from_ms)
            ) THEN
                RAISE EXCEPTION 'integrity: key ownership periods overlap' USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END $$""",
}


def _postgresql_triggers() -> dict[str, str]:
    def reject(message: str) -> str:
        return f"EXECUTE FUNCTION billing_reject('{message}')"

    raw_changed = " OR ".join(f"OLD.{column} IS DISTINCT FROM NEW.{column}" for column in RAW_EVENT_IMMUTABLE_COLUMNS)
    triggers = {
        "trg_audit_logs_append_only": f"BEFORE UPDATE OR DELETE ON audit_logs FOR EACH ROW {reject('audit_logs is append-only')}",
        "trg_billing_cycles_closed": (
            "BEFORE UPDATE OR DELETE ON billing_cycles FOR EACH ROW WHEN (OLD.status = 'closed') "
            + reject("closed billing cycle is frozen")
        ),
        "trg_pricing_versions_retired": (
            "BEFORE UPDATE ON pricing_versions FOR EACH ROW WHEN (OLD.status = 'retired' AND NEW.status <> 'retired') "
            + reject("retired pricing version cannot be reactivated")
        ),
        "trg_model_price_rules_closed": (
            "BEFORE UPDATE OR DELETE ON model_price_rules FOR EACH ROW EXECUTE FUNCTION billing_price_rule_guard()"
        ),
        "trg_rated_events_no_update": f"BEFORE UPDATE ON rated_events FOR EACH ROW {reject('rated events are immutable')}",
        "trg_raw_usage_events_no_delete": f"BEFORE DELETE ON raw_usage_events FOR EACH ROW {reject('raw usage events are immutable')}",
        "trg_raw_usage_events_no_update": (
            f"BEFORE UPDATE ON raw_usage_events FOR EACH ROW WHEN ({raw_changed}) {reject('raw usage events are immutable')}"
        ),
        "trg_key_ownership_no_overlap": (
            "BEFORE INSERT ON key_ownership_periods FOR EACH ROW EXECUTE FUNCTION billing_key_ownership_guard()"
        ),
        "trg_statement_lines_final": (
            "BEFORE INSERT OR UPDATE OR DELETE ON statement_lines FOR EACH ROW EXECUTE FUNCTION billing_statement_line_guard()"
        ),
    }
    for table in ("statements", "metered_key_charges"):
        triggers[f"trg_{table}_final"] = (
            f"BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW WHEN (OLD.final) {reject('final statement is frozen')}"
        )
    for table in CYCLE_CHILD_TABLES:
        triggers[f"trg_{table}_closed"] = (
            f"BEFORE INSERT OR UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION billing_cycle_child_guard()"
        )
    return triggers


def trigger_definitions(dialect: str) -> dict[str, str]:
    return _postgresql_triggers() if dialect == "postgresql" else _sqlite_triggers()


def view_definitions(dialect: str) -> dict[str, str]:
    if dialect == "postgresql":
        day = "to_char(to_timestamp(r.occurred_at_ms / 1000.0) AT TIME ZONE 'Asia/Shanghai', 'YYYY-MM-DD')"
        return _views(day, "false")
    return _views("date(r.occurred_at_ms / 1000, 'unixepoch', '+8 hours')", "0")


def _existing(connection: Any, dialect: str) -> tuple[set[str], set[str]]:
    if dialect == "postgresql":
        triggers = set(connection.execute(text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")).scalars())
        views = set(connection.execute(text("SELECT viewname FROM pg_views WHERE schemaname = current_schema()")).scalars())
    else:
        triggers = set(connection.execute(text("SELECT name FROM sqlite_master WHERE type = 'trigger'")).scalars())
        views = set(connection.execute(text("SELECT name FROM sqlite_master WHERE type = 'view'")).scalars())
    return triggers, views


def install_integrity_objects(connection: Any) -> list[str]:
    """Create missing triggers and views on an open connection (inside its transaction); returns their names."""
    dialect = connection.dialect.name
    triggers, views = trigger_definitions(dialect), view_definitions(dialect)
    existing_triggers, existing_views = _existing(connection, dialect)
    if set(triggers) <= existing_triggers and set(views) <= existing_views:
        return []
    if dialect == "postgresql":
        connection.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": ADVISORY_LOCK_ID})
        existing_triggers, existing_views = _existing(connection, dialect)
        for body in POSTGRESQL_FUNCTIONS.values():
            connection.execute(text(body))
    # PostgreSQL installs under the advisory lock; SQLite processes starting together rely on IF NOT EXISTS.
    if_missing = "" if dialect == "postgresql" else "IF NOT EXISTS "
    created = []
    for name, body in triggers.items():
        if name not in existing_triggers:
            connection.execute(text(f"CREATE TRIGGER {if_missing}{name} {body}"))
            created.append(name)
    for name, body in views.items():
        if name not in existing_views:
            connection.execute(text(f"CREATE VIEW {if_missing}{name} AS {body}"))
            created.append(name)
    return created


def drop_integrity_objects(connection: Any) -> None:
    dialect = connection.dialect.name
    for name in view_definitions(dialect):
        connection.execute(text(f"DROP VIEW IF EXISTS {name}"))
    for name, body in trigger_definitions(dialect).items():
        if dialect == "postgresql":
            table = body.split(" ON ", 1)[1].split()[0]
            connection.execute(text(f"DROP TRIGGER IF EXISTS {name} ON {table}"))
        else:
            connection.execute(text(f"DROP TRIGGER IF EXISTS {name}"))
    if dialect == "postgresql":
        for name in POSTGRESQL_FUNCTIONS:
            connection.execute(text(f"DROP FUNCTION IF EXISTS {name}()"))
