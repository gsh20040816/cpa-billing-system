"""Store the request and response service-tier provenance from CPAMP."""

from alembic import op
import sqlalchemy as sa


revision = "0008_service_tier_provenance"
down_revision = "0007_reasoning_effort"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("raw_usage_events")}
    if "request_service_tier" not in columns:
        op.add_column("raw_usage_events", sa.Column("request_service_tier", sa.String(length=40), nullable=True))
    if "response_service_tier" not in columns:
        op.add_column("raw_usage_events", sa.Column("response_service_tier", sa.String(length=40), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("raw_usage_events")}
    with op.batch_alter_table("raw_usage_events") as batch:
        if "response_service_tier" in columns:
            batch.drop_column("response_service_tier")
        if "request_service_tier" in columns:
            batch.drop_column("request_service_tier")
