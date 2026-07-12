"""Store CPAMP reasoning effort on mirrored usage events."""

from alembic import op
import sqlalchemy as sa


revision = "0007_reasoning_effort"
down_revision = "0006_editable_manual_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("raw_usage_events")}
    if "reasoning_effort" not in columns:
        op.add_column("raw_usage_events", sa.Column("reasoning_effort", sa.String(length=40), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("raw_usage_events")}
    if "reasoning_effort" in columns:
        with op.batch_alter_table("raw_usage_events") as batch:
            batch.drop_column("reasoning_effort")
