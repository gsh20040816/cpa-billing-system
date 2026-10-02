"""Store explicit context bands and Flex cache prices."""
from alembic import op
import sqlalchemy as sa

revision = "0015_explicit_context_prices"
down_revision = "0014_pricing_rerate_models"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("model_price_rules")}
    for name, kind in (("context_tiers_json", sa.Text()), ("flex_cache_read_nano_per_token", sa.BigInteger()),
                       ("flex_cache_creation_nano_per_token", sa.BigInteger())):
        if name not in existing:
            op.add_column("model_price_rules", sa.Column(name, kind, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("model_price_rules") as batch:
        batch.drop_column("context_tiers_json")
        batch.drop_column("flex_cache_read_nano_per_token")
        batch.drop_column("flex_cache_creation_nano_per_token")
