# CPA Billing System

CPAMP-backed monthly cost allocation, global Telegram-user dashboard, API-Key login, and Telegram registration.

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

The service never consumes the CPA usage queue. CPAMP is mounted read-only and mirrored by monotonically increasing `usage_events.id`.
