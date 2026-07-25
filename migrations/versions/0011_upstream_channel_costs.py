"""Add per-cycle upstream channel cost snapshots."""

from alembic import op
import sqlalchemy as sa


revision = "0011_upstream_channel_costs"
down_revision = "0010_dashboard_query_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("cycle_upstream_costs"):
        return
    op.create_table(
        "cycle_upstream_costs",
        sa.Column("cycle_id", sa.Integer(), sa.ForeignKey("billing_cycles.id"), primary_key=True),
        sa.Column("account_id", sa.String(length=200), primary_key=True),
        sa.Column("auth_index", sa.String(length=160), nullable=False),
        sa.Column("account_name", sa.String(length=200), nullable=False),
        sa.Column("auth_type", sa.String(length=20), nullable=False),
        sa.Column("fixed_cost_cents", sa.BigInteger(), nullable=True),
        sa.Column("rate_ppm", sa.BigInteger(), nullable=True),
        sa.Column("request_count", sa.BigInteger(), nullable=True),
        sa.Column("token_count", sa.BigInteger(), nullable=True),
        sa.Column("actual_weight_nano_usd", sa.BigInteger(), nullable=True),
        sa.Column("amount_cents", sa.BigInteger(), nullable=True),
        sa.UniqueConstraint("cycle_id", "auth_index"),
        sa.CheckConstraint(
            "(auth_type = 'oauth' and fixed_cost_cents is not null and rate_ppm is null) or "
            "(auth_type = 'api_key' and fixed_cost_cents is null and rate_ppm is not null)",
            name="ck_cycle_upstream_cost_type",
        ),
        sa.CheckConstraint(
            "coalesce(fixed_cost_cents, 0) >= 0 and coalesce(rate_ppm, 0) >= 0",
            name="ck_cycle_upstream_cost_nonnegative",
        ),
    )


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("cycle_upstream_costs"):
        op.drop_table("cycle_upstream_costs")
