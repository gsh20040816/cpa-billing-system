"""Add append-only manual usage adjustments."""

from alembic import op
import sqlalchemy as sa


revision = "0005_manual_usage_adjustments"
down_revision = "0004_live_billing_rules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("manual_usage_adjustments"):
        return
    op.create_table(
        "manual_usage_adjustments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cycle_id", sa.Integer(), sa.ForeignKey("billing_cycles.id"), nullable=False),
        sa.Column("pool_id", sa.Integer(), sa.ForeignKey("resource_pools.id"), nullable=False),
        sa.Column(
            "telegram_user_id",
            sa.BigInteger(),
            sa.ForeignKey("telegram_users.telegram_user_id"),
            nullable=False,
        ),
        sa.Column("amount_nano_usd", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("operator_user_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("amount_nano_usd != 0", name="ck_manual_usage_adjustments_nonzero"),
    )
    op.create_index(
        "idx_manual_usage_cycle_pool_user",
        "manual_usage_adjustments",
        ["cycle_id", "pool_id", "telegram_user_id"],
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("manual_usage_adjustments"):
        op.drop_table("manual_usage_adjustments")
