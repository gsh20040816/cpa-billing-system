"""Add indexes for user request history queries."""

from alembic import op
import sqlalchemy as sa


revision = "0003_request_history_indexes"
down_revision = "0002_admin_web_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    indexes = {item["name"] for item in inspector.get_indexes("rated_events")}
    if "idx_rated_user_time" not in indexes:
        op.create_index(
            "idx_rated_user_time",
            "rated_events",
            ["telegram_user_id", "occurred_at_ms"],
        )


def downgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    indexes = {item["name"] for item in inspector.get_indexes("rated_events")}
    if "idx_rated_user_time" in indexes:
        op.drop_index("idx_rated_user_time", table_name="rated_events")
