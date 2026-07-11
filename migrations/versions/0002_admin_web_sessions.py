"""Add independent administrator web sessions."""

from alembic import op
import sqlalchemy as sa


revision = "0002_admin_web_sessions"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    if not inspector.has_table("admin_web_sessions"):
        op.create_table(
            "admin_web_sessions",
            sa.Column("session_hash", sa.String(length=64), primary_key=True),
            sa.Column("credential_fingerprint", sa.String(length=64), nullable=False),
            sa.Column("csrf_token", sa.String(length=64), nullable=False),
            sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
            sa.Column("expires_at_ms", sa.BigInteger(), nullable=False),
            sa.Column("revoked_at_ms", sa.BigInteger(), nullable=True),
        )
    adjustment_columns = {column["name"]: column for column in inspector.get_columns("adjustments")}
    if not adjustment_columns["operator_user_id"]["nullable"]:
        with op.batch_alter_table("adjustments") as batch:
            batch.alter_column("operator_user_id", existing_type=sa.BigInteger(), nullable=True)


def downgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    adjustment_columns = {column["name"]: column for column in inspector.get_columns("adjustments")}
    if adjustment_columns["operator_user_id"]["nullable"]:
        with op.batch_alter_table("adjustments") as batch:
            batch.alter_column("operator_user_id", existing_type=sa.BigInteger(), nullable=False)
    if inspector.has_table("admin_web_sessions"):
        op.drop_table("admin_web_sessions")
