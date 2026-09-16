# CPA Billing System

CPAMP-backed monthly cost allocation, global Telegram-user dashboard, API-Key login, and Telegram registration. The Web console uses Vue 3, Vuetify 3, and ECharts; FastAPI exposes JSON APIs and serves the compiled SPA.

New billing cycles snapshot CPA's active upstream authentication channels. OAuth accounts use a configured one-time or recurring subscription; the overlapping portion of that subscription is the cycle's CNY cost. Each upstream API-key channel contributes its rated USD usage multiplied by its configured CNY/USD rate. Administrators assign accounts to billing groups, each with its own gradient rule. Group costs are allocated independently, then summed onto each user. Unbound downstream keys with a configured multiplier reduce the matching group's residual before allocation. Legacy cycles retain their original pool-fixed-cost snapshot until an administrator explicitly migrates an open cycle. Existing deployments keep every upstream account in the default group until regrouped.

Upstream discovery combines CPA OAuth auth files with its API-key management sections (Codex, Claude, Gemini, Vertex, xAI, interactions, and OpenAI-compatible providers). Administrators configure the resulting fixed costs and CNY/USD rates from the upstream-account section of the management page; raw upstream credentials are never returned to the browser or stored in billing snapshots.

## Web authentication

- `/login` accepts only API Keys registered through the Telegram bot. These sessions never receive administrator permissions.
- `/admin/login` accepts only `BILLING_ADMIN_TOKEN`. Administrator sessions are independent from Telegram users and API Keys.
- Rotating `BILLING_ADMIN_TOKEN` invalidates existing administrator sessions on their next request.

Required secrets are `CPA_MANAGEMENT_KEY`, `BILLING_KEY_PEPPER`, `BILLING_SESSION_SECRET`, and `BILLING_ADMIN_TOKEN`.

Upstream accounts and quota windows are read through CPA's management API. The user-facing API only exposes sanitized account IDs and never returns CPA auth indexes, OAuth credentials, or token files. “Refresh quota” performs a new read-only query through CPA's `api-call` proxy: Codex uses ChatGPT usage windows, and xAI OAuth uses the same Grok billing probe as CPA's own quota page. The administrator-only “reset upstream quota” action consumes one Codex `rate-limit-reset-credits` allowance through that proxy, requires the quota-aware confirmation count, and is audited separately; it does not call CPA's local `reset-quota` endpoint.

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

## Docker deployment

Pushes to `main` publish `ghcr.io/gsh20040816/cpa-billing-system:latest`. Version tags matching `v*` also publish a matching image tag, and every published image receives an immutable `sha-<commit>` tag. Pull requests build the image without publishing it.

The Compose services pull the published image instead of building it on the server. To deploy the newest `main` image:

```bash
docker compose pull
docker compose up -d
```

Set `BILLING_IMAGE` in `.env` to deploy a version or commit-specific tag instead of `latest`:

```dotenv
BILLING_IMAGE=ghcr.io/gsh20040816/cpa-billing-system:sha-0123456
```

If the GHCR package is private, log in once on the deployment server with a token that has `read:packages` permission before running Compose.

The service never consumes the CPA usage queue. CPAMP is mounted read-only and mirrored by monotonically increasing `usage_events.id`.

Price synchronization asks CPAMP to refresh its prices, then snapshots the four normalized `model_prices` columns: `prompt_per_1m`, `completion_per_1m`, `cache_read_per_1m`, and `cache_creation_per_1m`. Zero values are preserved without model-specific defaults or cache-price substitutions. For CPAMP rows sourced from `models.dev`, `raw_json.experimental.modes` supplies Priority/Fast and Flex prices, and `raw_json.cost.tiers` supplies the explicit context threshold and prices used to derive long-context multipliers. Fast and long context stack: tier prices are selected first, then multiplied by the context-to-default price ratios. There are no model-name checks or fixed pricing multipliers. Unsupported context tiers fail the import instead of silently discarding prices. Synchronization replaces local overrides with CPAMP rules and queues rerating for open cycles; closed cycles retain their price versions.
