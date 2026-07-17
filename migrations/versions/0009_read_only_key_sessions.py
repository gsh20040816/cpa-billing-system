"""Allow sessions for API Keys without Telegram ownership."""

from alembic import op
import sqlalchemy as sa


revision = "0009_read_only_key_sessions"
down_revision = "0008_service_tier_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("web_sessions") as batch:
        batch.alter_column(
            "telegram_user_id",
            existing_type=sa.BigInteger(),
            nullable=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("select count(*) from web_sessions where telegram_user_id is null")).scalar_one():
        raise RuntimeError("cannot restore non-null Telegram ownership while read-only sessions exist")
    with op.batch_alter_table("web_sessions") as batch:
        batch.alter_column(
            "telegram_user_id",
            existing_type=sa.BigInteger(),
            nullable=False,
        )
