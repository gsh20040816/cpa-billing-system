"""Index pending CPAMP backfills and clamp negative token counts."""
from alembic import op
import sqlalchemy as sa

revision = "0016_worker_hot_path"
down_revision = "0015_explicit_context_prices"
branch_labels = None
depends_on = None

BACKFILL_COLUMNS = ("reasoning_effort", "request_service_tier", "response_service_tier")
TOKEN_COLUMNS = (
    "input_tokens", "output_tokens", "reasoning_tokens", "cached_tokens",
    "cache_tokens", "cache_read_tokens", "cache_creation_tokens", "total_tokens",
)


def upgrade() -> None:
    existing = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("raw_usage_events")}
    for column in BACKFILL_COLUMNS:
        name = f"idx_raw_events_missing_{column}"
        if name not in existing:
            op.create_index(name, "raw_usage_events", ["source_id", "source_event_id"],
                            sqlite_where=sa.text(f"{column} IS NULL"))
    # Imports now clamp negative CPAMP token counts; rating already treated them as zero.
    op.execute(
        "UPDATE raw_usage_events SET "
        + ", ".join(f"{column} = max({column}, 0)" for column in TOKEN_COLUMNS)
        + " WHERE " + " OR ".join(f"{column} < 0" for column in TOKEN_COLUMNS)
    )


def downgrade() -> None:
    existing = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("raw_usage_events")}
    for column in BACKFILL_COLUMNS:
        name = f"idx_raw_events_missing_{column}"
        if name in existing:
            op.drop_index(name, table_name="raw_usage_events")
