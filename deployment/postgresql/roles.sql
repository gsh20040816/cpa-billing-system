-- Least-privilege roles for a PostgreSQL billing database.
--
-- Run once as a superuser after `alembic upgrade head`, connected to the billing database:
--   psql -v ON_ERROR_STOP=1 -v app_password="'...'" -v report_password="'...'" -v auditor_password="'...'" \
--        -d billing -f deployment/postgresql/roles.sql
-- Re-running is safe; it re-applies the grants.
--
-- billing_owner    owns the schema; migrations and maintenance run as this role (NOLOGIN, use SET ROLE).
-- billing_app      the web, worker and bot services: data changes only, no DDL.
-- billing_report   analysts and dashboards: reporting views and non-secret tables, read-only.
-- billing_auditor  reviewers: everything billing_report sees plus audit and reconciliation history.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'billing_owner') THEN CREATE ROLE billing_owner NOLOGIN; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'billing_app') THEN CREATE ROLE billing_app LOGIN; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'billing_report') THEN CREATE ROLE billing_report LOGIN; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'billing_auditor') THEN CREATE ROLE billing_auditor LOGIN; END IF;
END $$;

ALTER ROLE billing_app PASSWORD :app_password CONNECTION LIMIT 80;
ALTER ROLE billing_report PASSWORD :report_password CONNECTION LIMIT 10;
ALTER ROLE billing_auditor PASSWORD :auditor_password CONNECTION LIMIT 5;
GRANT billing_report TO billing_auditor;

-- Nobody gets implicit access through PUBLIC.
REVOKE ALL ON DATABASE :"DBNAME" FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT CONNECT ON DATABASE :"DBNAME" TO billing_app, billing_report, billing_auditor;
GRANT USAGE ON SCHEMA public TO billing_app, billing_report, billing_auditor;

-- Ownership: tables, views, sequences and trigger functions belong to billing_owner.
ALTER SCHEMA public OWNER TO billing_owner;
DO $$
DECLARE item record;
BEGIN
    FOR item IN SELECT format('ALTER TABLE %I OWNER TO billing_owner', tablename) AS ddl FROM pg_tables WHERE schemaname = 'public'
        UNION ALL SELECT format('ALTER VIEW %I OWNER TO billing_owner', viewname) FROM pg_views WHERE schemaname = 'public'
        UNION ALL SELECT format('ALTER FUNCTION %s OWNER TO billing_owner', p.oid::regprocedure)
                  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                  WHERE n.nspname = 'public' AND p.proname LIKE 'billing\_%'
    LOOP
        EXECUTE item.ddl;
    END LOOP;
END $$;

-- Application: read and write billing data.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO billing_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO billing_app;
-- Privileges back up the triggers: history is append-only even if a trigger were dropped.
REVOKE UPDATE, DELETE ON audit_logs, reconciliation_runs FROM billing_app;
REVOKE DELETE ON raw_usage_events, rated_events, key_ownership_periods FROM billing_app;
REVOKE ALL ON alembic_version FROM billing_app;
GRANT SELECT ON alembic_version TO billing_app;

-- Reporting: views and descriptive tables; no key hashes, login fingerprints, sessions or raw payloads.
GRANT SELECT ON v_user_cycle_bills, v_statement_line_details, v_cycle_group_summary, v_daily_user_model_usage,
    v_api_key_owners TO billing_report;
GRANT SELECT ON billing_cycles, statements, statement_lines, cycle_groups, cycle_upstream_costs, cycle_pool_costs,
    adjustments, manual_usage_adjustments, metered_key_charges, pricing_versions, model_price_rules, gradient_rules,
    resource_pools, upstream_account_groups TO billing_report;
GRANT SELECT (telegram_user_id, username, registered_at_ms) ON telegram_users TO billing_report;

-- Auditing: the change history and reconciliation results.
GRANT SELECT ON audit_logs, reconciliation_runs, dead_letters, sync_checkpoints TO billing_auditor;

-- Objects created by future migrations (run as billing_owner) receive the same grants.
ALTER DEFAULT PRIVILEGES FOR ROLE billing_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO billing_app;
ALTER DEFAULT PRIVILEGES FOR ROLE billing_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO billing_app;
