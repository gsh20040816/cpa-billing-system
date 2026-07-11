# CPA Billing System

CPAMP-backed monthly cost allocation, global Telegram-user dashboard, API-Key login, and Telegram registration. The Web console uses Vue 3, Vuetify 3, and ECharts; FastAPI exposes JSON APIs and serves the compiled SPA.

## Web authentication

- `/login` accepts only API Keys registered through the Telegram bot. These sessions never receive administrator permissions.
- `/admin/login` accepts only `BILLING_ADMIN_TOKEN`. Administrator sessions are independent from Telegram users and API Keys.
- Rotating `BILLING_ADMIN_TOKEN` invalidates existing administrator sessions on their next request.

Required secrets are `CPA_MANAGEMENT_KEY`, `BILLING_KEY_PEPPER`, `BILLING_SESSION_SECRET`, and `BILLING_ADMIN_TOKEN`.

Keeper integration additionally uses `KEEPER_BASE_URL` and `KEEPER_LOGIN_PASSWORD`. The user-facing API only exposes sanitized account IDs and never returns Keeper auth indexes, credential files, or paths. Quota refresh calls Keeper's refresh task API; no quota reset endpoint is exposed.

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
