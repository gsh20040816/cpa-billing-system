"""Limit new pricing rerates to open billing cycles."""

from alembic import op
import sqlalchemy as sa


revision = "0012_pricing_rerate_scopes"
down_revision = "0011_upstream_channel_costs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("pricing_rerate_scopes"):
        return
    op.create_table(
        "pricing_rerate_scopes",
        sa.Column(
            "pricing_version_id",
            sa.Integer(),
            sa.ForeignKey("pricing_versions.id"),
            primary_key=True,
        ),
        sa.Column("ranges_json", sa.Text(), nullable=False),
        sa.Column("max_raw_event_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("pricing_rerate_scopes"):
        op.drop_table("pricing_rerate_scopes")
