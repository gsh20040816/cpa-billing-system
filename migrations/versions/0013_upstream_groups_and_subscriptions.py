"""Add upstream account groups, subscription configs, and cycle group snapshots."""

from alembic import op
import sqlalchemy as sa


revision = "0013_upstream_groups_and_subscriptions"
down_revision = "0012_pricing_rerate_scopes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    now = 0

    if "upstream_account_groups" not in tables:
        op.create_table(
            "upstream_account_groups",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(length=80), nullable=False, unique=True),
            sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("gradient_rule_id", sa.Integer(), sa.ForeignKey("gradient_rules.id"), nullable=False),
            sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
            sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        )

    if "upstream_account_configs" not in tables:
        op.create_table(
            "upstream_account_configs",
            sa.Column("account_id", sa.String(length=200), primary_key=True),
            sa.Column("group_id", sa.Integer(), sa.ForeignKey("upstream_account_groups.id"), nullable=False),
            sa.Column("subscription_mode", sa.String(length=20), nullable=True),
            sa.Column("period_start_at_ms", sa.BigInteger(), nullable=True),
            sa.Column("period_end_at_ms", sa.BigInteger(), nullable=True),
            sa.Column("recurring_unit", sa.String(length=20), nullable=True),
            sa.Column("recurring_interval", sa.Integer(), nullable=True),
            sa.Column("period_cost_cents", sa.BigInteger(), nullable=True),
            sa.Column("rate_ppm", sa.BigInteger(), nullable=True),
            sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        )

    if "cycle_groups" not in tables:
        op.create_table(
            "cycle_groups",
            sa.Column("cycle_id", sa.Integer(), sa.ForeignKey("billing_cycles.id"), primary_key=True),
            sa.Column("group_id", sa.Integer(), primary_key=True),
            sa.Column("group_name", sa.String(length=80), nullable=False),
            sa.Column("gradient_rule_id", sa.Integer(), sa.ForeignKey("gradient_rules.id"), nullable=False),
            sa.Column("tiers_json", sa.Text(), nullable=False),
            sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("fixed_cost_cents", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("dynamic_cost_cents", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("metered_amount_cents", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("residual_cost_cents", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("member_amount_cents", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("surplus_cents", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("unallocated_cents", sa.BigInteger(), nullable=False, server_default="0"),
        )

    upstream_columns = {column["name"] for column in inspector.get_columns("cycle_upstream_costs")} if "cycle_upstream_costs" in tables else set()
    if "cycle_upstream_costs" in tables:
        with op.batch_alter_table("cycle_upstream_costs") as batch:
            if "group_id" not in upstream_columns:
                batch.add_column(sa.Column("group_id", sa.Integer(), nullable=True))
            if "group_name" not in upstream_columns:
                batch.add_column(sa.Column("group_name", sa.String(length=80), nullable=True))
            if "subscription_mode" not in upstream_columns:
                batch.add_column(sa.Column("subscription_mode", sa.String(length=20), nullable=True))
            if "period_start_at_ms" not in upstream_columns:
                batch.add_column(sa.Column("period_start_at_ms", sa.BigInteger(), nullable=True))
            if "period_end_at_ms" not in upstream_columns:
                batch.add_column(sa.Column("period_end_at_ms", sa.BigInteger(), nullable=True))
            if "recurring_unit" not in upstream_columns:
                batch.add_column(sa.Column("recurring_unit", sa.String(length=20), nullable=True))
            if "recurring_interval" not in upstream_columns:
                batch.add_column(sa.Column("recurring_interval", sa.Integer(), nullable=True))
            if "period_cost_cents" not in upstream_columns:
                batch.add_column(sa.Column("period_cost_cents", sa.BigInteger(), nullable=True))

    statement_columns = {column["name"] for column in inspector.get_columns("statement_lines")} if "statement_lines" in tables else set()
    if "statement_lines" in tables and "group_id" not in statement_columns:
        with op.batch_alter_table("statement_lines") as batch:
            batch.add_column(sa.Column("group_id", sa.Integer(), nullable=True))

    manual_columns = {column["name"] for column in inspector.get_columns("manual_usage_adjustments")} if "manual_usage_adjustments" in tables else set()
    if "manual_usage_adjustments" in tables and "group_id" not in manual_columns:
        with op.batch_alter_table("manual_usage_adjustments") as batch:
            batch.add_column(sa.Column("group_id", sa.Integer(), sa.ForeignKey("upstream_account_groups.id"), nullable=True))
        op.create_index(
            "idx_manual_usage_cycle_group_pool_user",
            "manual_usage_adjustments",
            ["cycle_id", "group_id", "pool_id", "telegram_user_id"],
        )

    gradient_id = bind.execute(
        sa.text("select id from gradient_rules where active = 1 order by id limit 1")
    ).scalar()
    if gradient_id is None:
        gradient_id = bind.execute(sa.text("select id from gradient_rules order by id limit 1")).scalar()
    if gradient_id is None:
        return

    existing_default = bind.execute(
        sa.text("select id from upstream_account_groups where is_default = 1 order by id limit 1")
    ).scalar()
    if existing_default is None:
        bind.execute(
            sa.text(
                "insert into upstream_account_groups "
                "(name, is_default, gradient_rule_id, created_at_ms, updated_at_ms) "
                "values ('default', 1, :gradient_id, :now, :now)"
            ),
            {"gradient_id": int(gradient_id), "now": now},
        )
        existing_default = bind.execute(
            sa.text("select id from upstream_account_groups where is_default = 1 order by id limit 1")
        ).scalar()
    default_id = int(existing_default)
    default_name = bind.execute(
        sa.text("select name from upstream_account_groups where id = :id"),
        {"id": default_id},
    ).scalar() or "default"

    if "cycle_upstream_costs" in set(sa.inspect(bind).get_table_names()):
        bind.execute(
            sa.text(
                "update cycle_upstream_costs set group_id = :group_id, group_name = :group_name "
                "where group_id is null"
            ),
            {"group_id": default_id, "group_name": default_name},
        )

    if "manual_usage_adjustments" in set(sa.inspect(bind).get_table_names()):
        bind.execute(
            sa.text("update manual_usage_adjustments set group_id = :group_id where group_id is null"),
            {"group_id": default_id},
        )

    existing_configs = {
        str(row["account_id"])
        for row in bind.execute(sa.text("select account_id from upstream_account_configs")).mappings()
    }
    if "cycle_upstream_costs" in set(sa.inspect(bind).get_table_names()):
        snapshots = bind.execute(
            sa.text(
                "select cuc.account_id, cuc.auth_type, cuc.rate_ppm "
                "from cycle_upstream_costs cuc "
                "join billing_cycles bc on bc.id = cuc.cycle_id "
                "order by bc.start_at_ms desc, cuc.account_id"
            )
        ).mappings().all()
        seen: set[str] = set()
        for row in snapshots:
            account_id = str(row["account_id"] or "")
            if not account_id or account_id in seen or account_id in existing_configs:
                continue
            seen.add(account_id)
            bind.execute(
                sa.text(
                    "insert into upstream_account_configs ("
                    "account_id, group_id, subscription_mode, period_start_at_ms, period_end_at_ms, "
                    "recurring_unit, recurring_interval, period_cost_cents, rate_ppm, updated_at_ms"
                    ") values ("
                    ":account_id, :group_id, null, null, null, null, null, null, :rate_ppm, :now)"
                ),
                {
                    "account_id": account_id,
                    "group_id": default_id,
                    "rate_ppm": row["rate_ppm"] if row["auth_type"] == "api_key" else None,
                    "now": now,
                },
            )

    cycles = bind.execute(
        sa.text("select id, gradient_rule_id, tiers_json from billing_cycles order by id")
    ).mappings().all()
    existing_cycle_groups = {
        (int(row["cycle_id"]), int(row["group_id"]))
        for row in bind.execute(sa.text("select cycle_id, group_id from cycle_groups")).mappings()
    }
    for cycle in cycles:
        key = (int(cycle["id"]), default_id)
        if key in existing_cycle_groups:
            continue
        has_upstream = bind.execute(
            sa.text("select 1 from cycle_upstream_costs where cycle_id = :cycle_id limit 1"),
            {"cycle_id": int(cycle["id"])},
        ).scalar()
        if not has_upstream:
            continue
        bind.execute(
            sa.text(
                "insert into cycle_groups ("
                "cycle_id, group_id, group_name, gradient_rule_id, tiers_json, is_default, "
                "fixed_cost_cents, dynamic_cost_cents, metered_amount_cents, residual_cost_cents, "
                "member_amount_cents, surplus_cents, unallocated_cents"
                ") values ("
                ":cycle_id, :group_id, :group_name, :gradient_rule_id, :tiers_json, 1, "
                "0, 0, 0, 0, 0, 0, 0)"
            ),
            {
                "cycle_id": int(cycle["id"]),
                "group_id": default_id,
                "group_name": default_name,
                "gradient_rule_id": int(cycle["gradient_rule_id"]),
                "tiers_json": cycle["tiers_json"],
            },
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "manual_usage_adjustments" in tables:
        indexes = {index["name"] for index in inspector.get_indexes("manual_usage_adjustments")}
        if "idx_manual_usage_cycle_group_pool_user" in indexes:
            op.drop_index("idx_manual_usage_cycle_group_pool_user", table_name="manual_usage_adjustments")
        columns = {column["name"] for column in inspector.get_columns("manual_usage_adjustments")}
        if "group_id" in columns:
            with op.batch_alter_table("manual_usage_adjustments") as batch:
                batch.drop_column("group_id")
    if "statement_lines" in tables:
        columns = {column["name"] for column in inspector.get_columns("statement_lines")}
        if "group_id" in columns:
            with op.batch_alter_table("statement_lines") as batch:
                batch.drop_column("group_id")
    if "cycle_upstream_costs" in tables:
        columns = {column["name"] for column in inspector.get_columns("cycle_upstream_costs")}
        with op.batch_alter_table("cycle_upstream_costs") as batch:
            for name in (
                "period_cost_cents",
                "recurring_interval",
                "recurring_unit",
                "period_end_at_ms",
                "period_start_at_ms",
                "subscription_mode",
                "group_name",
                "group_id",
            ):
                if name in columns:
                    batch.drop_column(name)
    if "cycle_groups" in tables:
        op.drop_table("cycle_groups")
    if "upstream_account_configs" in tables:
        op.drop_table("upstream_account_configs")
    if "upstream_account_groups" in tables:
        op.drop_table("upstream_account_groups")
