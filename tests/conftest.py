from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from cpa_billing.config import Settings
from cpa_billing.database import Database
from cpa_billing.services import BillingService


def create_cpamp(path: Path) -> None:
    db = sqlite3.connect(path)
    db.executescript("""
    create table usage_events(
      id integer primary key autoincrement,event_hash text unique,request_id text,timestamp_ms integer,timestamp text,
      provider text,executor_type text,model text,requested_model text,resolved_model text,reasoning_effort text,service_tier text,
      api_key_hash text,source_hash text,source text,account_snapshot text,auth_index text,
      input_tokens integer,output_tokens integer,reasoning_tokens integer,cached_tokens integer,cache_tokens integer,
      cache_read_tokens integer,cache_creation_tokens integer,total_tokens integer,failed integer,
      fail_status_code integer,latency_ms integer,ttft_ms integer);
    alter table usage_events add column response_metadata_json text;
    alter table usage_events add column header_quota_used_percent real;
    alter table usage_events add column header_quota_recover_at_ms integer;
    alter table usage_events add column header_quota_plan_type text;
    create table model_prices(model text primary key,prompt_per_1m real,completion_per_1m real,cache_per_1m real,
      cache_read_per_1m real,cache_creation_per_1m real,source text,source_model_id text,raw_json text,
      updated_at_ms integer,synced_at_ms integer);
    """)
    raw = {"input_cost_per_token_priority": 0.000002, "output_cost_per_token_priority": 0.000012,
           "cache_read_input_token_cost_priority": 0.0000002, "input_cost_per_token_above_272k_tokens": 0.000002,
           "output_cost_per_token_above_272k_tokens": 0.000009}
    db.execute("insert into model_prices values(?,?,?,?,?,?,?,?,?,?,?)", ("gpt-test", 1, 6, .1, .1, 1.25, "test", "gpt-test", json.dumps(raw), 1, 1))
    db.execute("insert into model_prices values(?,?,?,?,?,?,?,?,?,?,?)", ("gpt-5.6-luna", 1, 6, .1, .1, 1.25, "test", "gpt-5.6-luna", json.dumps(raw), 1, 1))
    db.execute("insert into model_prices values(?,?,?,?,?,?,?,?,?,?,?)", ("gpt-5.3-codex-spark", 1, 6, .1, .1, 1.25, "test", "gpt-5.3-codex-spark", json.dumps(raw), 1, 1))
    db.commit(); db.close()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    cpamp = tmp_path / "usage.sqlite"
    create_cpamp(cpamp)
    return Settings(database_path=tmp_path / "billing.sqlite", cpamp_database_path=cpamp, cpamp_source_name="test",
                    cpa_base_url="http://cpa", cpa_management_key="management", cpamp_base_url="http://cpamp",
                    cpamp_admin_key="",
                    key_pepper="pepper", session_secret="session", admin_token="admin-secret-token", telegram_token="token",
                    admin_user_ids=frozenset({1}), allowed_group_ids=frozenset({-100}), public_base_url="https://billing.example",
                    api_key_prefix="sk-cpa", timezone="Asia/Shanghai", worker_interval_seconds=1,
                    action_ttl_seconds=600, session_ttl_seconds=3600, sub2_state_file=tmp_path / "sub2.json", sub2_postgres_dsn=None)


@pytest.fixture
def service(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> BillingService:
    result = BillingService(settings, Database(settings.database_path))
    monkeypatch.setattr(result.cpa, "list_keys", lambda: [])
    monkeypatch.setattr(result.cpa, "add_key", lambda _: None)
    monkeypatch.setattr(result.cpa, "remove_key_hash", lambda _: None)
    result.bootstrap()
    return result
