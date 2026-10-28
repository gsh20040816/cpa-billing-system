"""Enforce billing invariants with triggers, single-active indexes, and reporting views."""
import logging

from alembic import op
import sqlalchemy as sa

from cpa_billing.integrity import drop_integrity_objects, install_integrity_objects

revision = "0017_integrity_triggers_views"
down_revision = "0016_worker_hot_path"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

# (index, table, column, predicate, duplicate probe)
PARTIAL_UNIQUE_INDEXES = (
    ("uq_pricing_versions_single_active", "pricing_versions", "status", "status = 'active'",
     "SELECT count(*) FROM pricing_versions WHERE status = 'active'"),
    ("uq_upstream_account_groups_single_default", "upstream_account_groups", "is_default", "is_default",
     "SELECT count(*) FROM upstream_account_groups WHERE is_default"),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for name, table, column, predicate, probe in PARTIAL_UNIQUE_INDEXES:
        if name in {index["name"] for index in inspector.get_indexes(table)}:
            continue
        # Existing deployments keep running if historical rows already break the rule; the operator is told.
        if bind.execute(sa.text(probe)).scalar_one() > 1:
            log.warning("skipping %s: %s already has more than one matching row", name, table)
            continue
        op.create_index(name, table, [column], unique=True,
                        sqlite_where=sa.text(predicate), postgresql_where=sa.text(predicate))
    for name in install_integrity_objects(bind):
        log.info("created %s", name)


def downgrade() -> None:
    bind = op.get_bind()
    drop_integrity_objects(bind)
    inspector = sa.inspect(bind)
    for name, table, *_ in PARTIAL_UNIQUE_INDEXES:
        if name in {index["name"] for index in inspector.get_indexes(table)}:
            op.drop_index(name, table_name=table)
