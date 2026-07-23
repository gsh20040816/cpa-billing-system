"""Add the composite index used by dashboard aggregates."""

from alembic import op
import sqlalchemy as sa


revision = "0010_dashboard_query_indexes"
down_revision = "0009_read_only_key_sessions"
branch_labels = None
depends_on = None


INDEX_NAME = "idx_rated_version_time_owner_pool"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    indexes = {item["name"] for item in inspector.get_indexes("rated_events")}
    if INDEX_NAME not in indexes:
        op.create_index(
            INDEX_NAME,
            "rated_events",
            ["pricing_version_id", "occurred_at_ms", "telegram_user_id", "pool_id"],
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    indexes = {item["name"] for item in inspector.get_indexes("rated_events")}
    if INDEX_NAME in indexes:
        op.drop_index(INDEX_NAME, table_name="rated_events")
