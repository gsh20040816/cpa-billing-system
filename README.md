# CPA Billing System

CPAMP-backed monthly cost allocation, global Telegram-user dashboard, API-Key login, and Telegram registration. The Web console uses Vue 3, Vuetify 3, and ECharts; FastAPI exposes JSON APIs and serves the compiled SPA.

## Web authentication

- `/login` accepts only API Keys registered through the Telegram bot. These sessions never receive administrator permissions.
- `/admin/login` accepts only `BILLING_ADMIN_TOKEN`. Administrator sessions are independent from Telegram users and API Keys.
- Rotating `BILLING_ADMIN_TOKEN` invalidates existing administrator sessions on their next request.

Required secrets are `CPA_MANAGEMENT_KEY`, `BILLING_KEY_PEPPER`, `BILLING_SESSION_SECRET`, and `BILLING_ADMIN_TOKEN`.

Upstream accounts and quota windows are read through CPA's management API. The user-facing API only exposes sanitized account IDs and never returns CPA auth indexes, OAuth credentials, or token files. “Refresh quota” performs a new read-only upstream query; the administrator-only “reset local state” action calls CPA's local `reset-quota` endpoint and is audited separately.

The administrator console can add manual raw equivalent usage in USD to a registered Telegram user and a configured cycle resource pool. These entries are applied before gradient billing and allocation and do not create request or token records. While the source and target cycles remain open, administrators may update every business field; each update preserves creation metadata, records before/after audit values, and recalculates both affected cycles. Negative values cannot make a cycle, pool, and user manual balance fall below zero.

## Web development

```bash
cd frontend
npm ci
npm test
npm run build
```

The backend serves `frontend/dist`. In development, Vite proxies `/api` and `/auth` to `127.0.0.1:18417`.

## Commands

```bash
cpa-billing init
cpa-billing migrate-legacy /path/to/cpa-tg-bot.sqlite3 --dry-run
cpa-billing sync --once
cpa-billing preview cycle0
cpa-billing reconcile
cpa-billing serve
cpa-billing bot
```

Apply database migrations before starting a new application version:

```bash
alembic upgrade head
```

The service never consumes the CPA usage queue. CPAMP is mounted read-only and mirrored by monotonically increasing `usage_events.id`.
