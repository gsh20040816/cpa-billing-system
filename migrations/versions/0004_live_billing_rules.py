"""Add live billing rules, CPA key profiles, and metered charges."""

import json
import time

from alembic import op
import sqlalchemy as sa


revision = "0004_live_billing_rules"
down_revision = "0003_request_history_indexes"
branch_labels = None
depends_on = None


DEFAULT_TIERS = [
    {"left": 0, "right": 300, "multiplier": 1},
    {"left": 300, "right": 800, "multiplier": 0.9},
    {"left": 800, "right": 1400, "multiplier": 0.8},
    {"left": 1400, "right": 2000, "multiplier": 0.7},
    {"left": 2000, "right": None, "multiplier": 0.6},
]


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)

    raw_columns = {column["name"] for column in inspector.get_columns("raw_usage_events")}
    if "cache_tokens" not in raw_columns:
        op.add_column("raw_usage_events", sa.Column("cache_tokens", sa.BigInteger(), nullable=False, server_default="0"))

    key_columns = {column["name"] for column in inspector.get_columns("api_keys")}
    if "billing_multiplier_ppm" not in key_columns:
        op.add_column("api_keys", sa.Column("billing_multiplier_ppm", sa.BigInteger(), nullable=True))
    if "present_in_cpa" not in key_columns:
        op.add_column("api_keys", sa.Column("present_in_cpa", sa.Boolean(), nullable=False, server_default=sa.false()))
    if "last_seen_in_cpa_at_ms" not in key_columns:
        op.add_column("api_keys", sa.Column("last_seen_in_cpa_at_ms", sa.BigInteger(), nullable=True))

    price_columns = {column["name"] for column in inspector.get_columns("model_price_rules")}
    configured_columns = {
        "input_configured": True,
        "output_configured": True,
        "cache_read_configured": False,
        "cache_creation_configured": False,
    }
    for name, default in configured_columns.items():
        if name not in price_columns:
            op.add_column(
                "model_price_rules",
                sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.true() if default else sa.false()),
            )
    connection.execute(sa.text("update model_price_rules set cache_read_configured = 1 where cache_read_nano_per_token != 0"))
    connection.execute(sa.text("update model_price_rules set cache_creation_configured = 1 where cache_creation_nano_per_token != 0"))

    if not inspector.has_table("gradient_rules"):
        op.create_table(
            "gradient_rules",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(length=80), nullable=False, unique=True),
            sa.Column("description", sa.String(length=300), nullable=True),
            sa.Column("tiers_json", sa.Text(), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
            sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        )

    cycle_column_rows = {column["name"]: column for column in inspector.get_columns("billing_cycles")}
    gradient_added = "gradient_rule_id" not in cycle_column_rows
    gradient_nullable = gradient_added or bool(cycle_column_rows["gradient_rule_id"].get("nullable"))
    gradient_fk_exists = any(
        foreign_key.get("referred_table") == "gradient_rules"
        and foreign_key.get("constrained_columns") == ["gradient_rule_id"]
        for foreign_key in inspector.get_foreign_keys("billing_cycles")
    )
    if gradient_added:
        op.add_column("billing_cycles", sa.Column("gradient_rule_id", sa.Integer(), nullable=True))

    now = int(time.time() * 1000)
    default_json = json.dumps(DEFAULT_TIERS, separators=(",", ":"))
    default_id = connection.execute(
        sa.text("select id from gradient_rules where name=:name"),
        {"name": "default-gradient"},
    ).scalar_one_or_none()
    if default_id is None:
        result = connection.execute(
            sa.text(
                "insert into gradient_rules(name,description,tiers_json,active,created_at_ms,updated_at_ms) "
                "values(:name,:description,:tiers,1,:now,:now)"
            ),
            {"name": "default-gradient", "description": "Default progressive allocation rule", "tiers": default_json, "now": now},
        )
        default_id = result.lastrowid
    cycles = connection.execute(sa.text("select id,name,tiers_json from billing_cycles order by id")).mappings().all()
    for cycle in cycles:
        tiers = cycle["tiers_json"] or default_json
        try:
            is_default = json.loads(tiers) == DEFAULT_TIERS
        except (TypeError, json.JSONDecodeError):
            is_default = False
        rule_id = default_id
        if not is_default:
            base_name = "migrated-" + str(cycle["name"])
            name = base_name[:80]
            result = connection.execute(
                sa.text(
                    "insert into gradient_rules(name,description,tiers_json,active,created_at_ms,updated_at_ms) "
                    "values(:name,:description,:tiers,1,:now,:now)"
                ),
                {"name": name, "description": "Imported from legacy billing cycle", "tiers": tiers, "now": now},
            )
            rule_id = result.lastrowid
        connection.execute(
            sa.text("update billing_cycles set gradient_rule_id=:rule_id where id=:cycle_id"),
            {"rule_id": rule_id, "cycle_id": cycle["id"]},
        )
    if gradient_nullable or not gradient_fk_exists:
        with op.batch_alter_table("billing_cycles") as batch:
            if gradient_nullable:
                batch.alter_column("gradient_rule_id", existing_type=sa.Integer(), nullable=False)
            if not gradient_fk_exists:
                batch.create_foreign_key("fk_billing_cycles_gradient_rule", "gradient_rules", ["gradient_rule_id"], ["id"])

    if not inspector.has_table("metered_key_charges"):
        op.create_table(
            "metered_key_charges",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("cycle_id", sa.Integer(), sa.ForeignKey("billing_cycles.id"), nullable=False),
            sa.Column("pool_id", sa.Integer(), sa.ForeignKey("resource_pools.id"), nullable=False),
            sa.Column("api_key_id", sa.Integer(), sa.ForeignKey("api_keys.id"), nullable=False),
            sa.Column("actual_weight_nano_usd", sa.BigInteger(), nullable=False),
            sa.Column("multiplier_ppm", sa.BigInteger(), nullable=False),
            sa.Column("amount_cents", sa.BigInteger(), nullable=False),
            sa.Column("generated_at_ms", sa.BigInteger(), nullable=False),
            sa.Column("final", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.UniqueConstraint("cycle_id", "pool_id", "api_key_id"),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("metered_key_charges"):
        op.drop_table("metered_key_charges")
    cycle_columns = {column["name"] for column in inspector.get_columns("billing_cycles")}
    if "gradient_rule_id" in cycle_columns:
        with op.batch_alter_table("billing_cycles") as batch:
            batch.drop_column("gradient_rule_id")
    if inspector.has_table("gradient_rules"):
        op.drop_table("gradient_rules")
    for name in ("input_configured", "output_configured", "cache_read_configured", "cache_creation_configured"):
        if name in {column["name"] for column in inspector.get_columns("model_price_rules")}:
            op.drop_column("model_price_rules", name)
    for name in ("billing_multiplier_ppm", "present_in_cpa", "last_seen_in_cpa_at_ms"):
        if name in {column["name"] for column in inspector.get_columns("api_keys")}:
            op.drop_column("api_keys", name)
    if "cache_tokens" in {column["name"] for column in inspector.get_columns("raw_usage_events")}:
        op.drop_column("raw_usage_events", "cache_tokens")
