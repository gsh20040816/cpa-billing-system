"""Track changed models for scoped pricing rerates."""

from alembic import op
import sqlalchemy as sa


revision = "0014_pricing_rerate_models"
down_revision = "0013_upstream_groups_and_subscriptions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("pricing_rerate_scopes"):
        return
    columns = {column["name"] for column in inspector.get_columns("pricing_rerate_scopes")}
    if "models_json" in columns:
        return
    with op.batch_alter_table("pricing_rerate_scopes") as batch_op:
        batch_op.add_column(sa.Column("models_json", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("pricing_rerate_scopes"):
        return
    columns = {column["name"] for column in inspector.get_columns("pricing_rerate_scopes")}
    if "models_json" not in columns:
        return
    with op.batch_alter_table("pricing_rerate_scopes") as batch_op:
        batch_op.drop_column("models_json")
