from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _ids(name: str) -> frozenset[int]:
    return frozenset(int(v.strip()) for v in os.getenv(name, "").split(",") if v.strip())


@dataclass(frozen=True)
class Settings:
    database_path: Path
    cpamp_database_path: Path
    cpamp_source_name: str
    cpa_base_url: str
    cpa_management_key: str
    cpamp_base_url: str
    cpamp_admin_key: str
    keeper_base_url: str
    keeper_login_password: str
    key_pepper: str
    session_secret: str
    admin_token: str
    telegram_token: str
    admin_user_ids: frozenset[int]
    allowed_group_ids: frozenset[int]
    public_base_url: str
    api_key_prefix: str
    timezone: str
    worker_interval_seconds: float
    action_ttl_seconds: int
    session_ttl_seconds: int
    sub2_state_file: Path
    sub2_postgres_dsn: str | None

    @classmethod
    def from_env(cls) -> "Settings":
        required = {
            "CPA_MANAGEMENT_KEY": os.getenv("CPA_MANAGEMENT_KEY", "").strip(),
            "BILLING_KEY_PEPPER": os.getenv("BILLING_KEY_PEPPER", "").strip(),
            "BILLING_SESSION_SECRET": os.getenv("BILLING_SESSION_SECRET", "").strip(),
            "BILLING_ADMIN_TOKEN": os.getenv("BILLING_ADMIN_TOKEN", "").strip(),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError("missing required environment variables: " + ", ".join(missing))
        return cls(
            database_path=Path(os.getenv("BILLING_DATABASE_PATH", "/data/billing.sqlite")),
            cpamp_database_path=Path(os.getenv("CPAMP_USAGE_DB", "/cpamp/usage.sqlite")),
            cpamp_source_name=os.getenv("CPAMP_SOURCE_NAME", "netcup-cpamp"),
            cpa_base_url=os.getenv("CPA_BASE_URL", "http://cli-proxy-api:8317").rstrip("/"),
            cpa_management_key=required["CPA_MANAGEMENT_KEY"],
            cpamp_base_url=os.getenv("CPAMP_BASE_URL", "http://cpa-manager-plus:18317").rstrip("/"),
            cpamp_admin_key=os.getenv("CPAMP_ADMIN_KEY", "").strip(),
            keeper_base_url=os.getenv("KEEPER_BASE_URL", "http://cpa-usage-keeper:8080").rstrip("/"),
            keeper_login_password=os.getenv("KEEPER_LOGIN_PASSWORD", "").strip(),
            key_pepper=required["BILLING_KEY_PEPPER"],
            session_secret=required["BILLING_SESSION_SECRET"],
            admin_token=required["BILLING_ADMIN_TOKEN"],
            telegram_token=os.getenv("TG_BOT_TOKEN", "").strip(),
            admin_user_ids=_ids("ADMIN_CHAT_IDS"),
            allowed_group_ids=_ids("ALLOWED_GROUP_CHAT_IDS"),
            public_base_url=os.getenv("BILLING_PUBLIC_URL", "https://billing.shgao.top").rstrip("/"),
            api_key_prefix=os.getenv("CPA_API_KEY_PREFIX", "sk-cpa").strip("-") or "sk-cpa",
            timezone=os.getenv("BILLING_TIMEZONE", "Asia/Shanghai"),
            worker_interval_seconds=float(os.getenv("BILLING_WORKER_INTERVAL_SECONDS", "10")),
            action_ttl_seconds=int(os.getenv("BILLING_ACTION_TTL_SECONDS", "600")),
            session_ttl_seconds=int(os.getenv("BILLING_SESSION_TTL_SECONDS", "43200")),
            sub2_state_file=Path(os.getenv("SUB2API_TG_STATE_FILE", "/legacy-sub2/sub2api-tg.state.json")),
            sub2_postgres_dsn=os.getenv("SUB2API_POSTGRES_DSN") or None,
        )
