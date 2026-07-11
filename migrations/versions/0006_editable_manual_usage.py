"""Track manual usage updates."""

from alembic import op
import sqlalchemy as sa


revision = "0006_editable_manual_usage"
down_revision = "0005_manual_usage_adjustments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("manual_usage_adjustments")}
    if "updated_at_ms" not in columns:
        op.add_column("manual_usage_adjustments", sa.Column("updated_at_ms", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("manual_usage_adjustments")}
    if "updated_at_ms" in columns:
        with op.batch_alter_table("manual_usage_adjustments") as batch:
            batch.drop_column("updated_at_ms")
