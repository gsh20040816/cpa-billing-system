"""Load synthetic usage into an empty PostgreSQL database and time the billing queries with and without indexes.

    python scripts/benchmark_postgresql.py postgresql+psycopg://user@host/billing_bench --events 1000000 \
        --report benchmark.md

The target database must be empty; it is filled through Database.initialize() so it carries the production
schema, constraints, triggers and views. Index-free timings drop the index inside a transaction that is
rolled back, so the database is left unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import statistics
import time
from dataclasses import dataclass
from pathlib import Path

import psycopg
from sqlalchemy import inspect
from sqlalchemy.engine import make_url

from cpa_billing.database import Database

DAY_MS = 86_400_000
START_MS = 1_767_196_800_000  # 2026-01-01T00:00:00+08:00
MODELS = (
    ("gpt-5.5", 1250, 10000), ("gpt-5.5-mini", 250, 2000), ("gpt-5.3-codex", 1250, 10000), ("claude-opus-5-5", 5000, 25000),
    ("claude-sonnet-5-5", 3000, 15000), ("claude-haiku-5-5", 1000, 5000), ("gemini-3-pro", 2000, 12000),
    ("gemini-3-flash", 300, 2500), ("grok-5", 3000, 15000), ("deepseek-v4", 270, 1100), ("qwen-4-max", 1200, 6000),
    ("kimi-k3", 600, 2500),
)


@dataclass(frozen=True)
class Query:
    name: str
    purpose: str
    sql: str
    indexes: tuple[str, ...]  # dropped for the index-free run


def parameters(users: int, days: int) -> dict[str, object]:
    month_start = START_MS + (days - 30) * DAY_MS
    return {"user": users // 2, "key_hash": key_hash(7), "from": month_start, "to": month_start + 30 * DAY_MS, "at": month_start}


QUERIES = (
    Query(
        "Q1 用户月度按模型用量",
        "用户详情页：某用户本期各模型请求数、Token 与费用",
        """SELECT e.model, count(*), sum(e.total_tokens), sum(r.rated_weight_nano_usd)
           FROM rated_events r JOIN raw_usage_events e ON e.id = r.raw_event_id
           WHERE r.pricing_version_id = %(version)s AND r.telegram_user_id = %(user)s
             AND r.occurred_at_ms >= %(from)s AND r.occurred_at_ms < %(to)s
           GROUP BY e.model ORDER BY 4 DESC""",
        ("idx_rated_user_time", "idx_rated_version_time_owner_pool", "idx_rated_cycle"),
    ),
    Query(
        "Q2 账期分摊汇总",
        "账务总览/出账：按用户、资源池汇总一个账期的计价用量",
        """SELECT telegram_user_id, pool_id, count(*), sum(rated_weight_nano_usd)
           FROM rated_events
           WHERE pricing_version_id = %(version)s AND occurred_at_ms >= %(from)s AND occurred_at_ms < %(to)s
           GROUP BY telegram_user_id, pool_id""",
        ("idx_rated_version_time_owner_pool", "idx_rated_cycle", "idx_rated_user_time"),
    ),
    Query(
        "Q3 密钥请求明细分页",
        "请求明细页：某 API Key 最近 50 条调用",
        """SELECT id, occurred_at_ms, model, input_tokens, output_tokens, latency_ms
           FROM raw_usage_events WHERE api_key_hash = %(key_hash)s
           ORDER BY occurred_at_ms DESC LIMIT 50""",
        ("idx_raw_events_key_time",),
    ),
    Query(
        "Q4 时间窗站点状态",
        "站点状态页：最近 24 小时按模型统计请求、失败与平均延迟",
        """SELECT model, count(*), sum(CASE WHEN failed THEN 1 ELSE 0 END), avg(latency_ms)
           FROM raw_usage_events WHERE occurred_at_ms >= %(to)s - 86400000 AND occurred_at_ms < %(to)s
           GROUP BY model""",
        ("idx_raw_events_time",),
    ),
    Query(
        "Q5 计价幂等去重",
        "计价任务：查找当前价格版本下尚未计价的原始事件",
        """SELECT count(*) FROM raw_usage_events e
           WHERE NOT EXISTS (SELECT 1 FROM rated_events r WHERE r.raw_event_id = e.id AND r.pricing_version_id = %(version)s)""",
        ("rated_events_raw_event_id_pricing_version_id_key",),
    ),
    Query(
        "Q6 密钥历史归属",
        "按时间归属：某时刻每个 Key 的归属用户",
        """SELECT k.id, p.telegram_user_id FROM api_keys k
           JOIN LATERAL (SELECT telegram_user_id FROM key_ownership_periods p
                         WHERE p.api_key_id = k.id AND p.valid_from_ms <= %(at)s
                           AND (p.valid_to_ms IS NULL OR p.valid_to_ms > %(at)s)
                         ORDER BY p.valid_from_ms DESC LIMIT 1) p ON true""",
        ("idx_ownership_key_period",),
    ),
)

# The same report written two ways: the view's day expression cannot use an index; a range predicate can.
REWRITE = (
    ("改写前：在视图日期表达式上过滤",
     """SELECT usage_date, sum(cost_usd) FROM v_daily_user_model_usage
        WHERE telegram_user_id = %(user)s AND usage_date >= %(from_day)s AND usage_date < %(to_day)s
        GROUP BY usage_date ORDER BY usage_date"""),
    ("改写后：先按时间范围过滤再分组",
     """SELECT to_char(to_timestamp(r.occurred_at_ms / 1000.0) AT TIME ZONE 'Asia/Shanghai', 'YYYY-MM-DD') AS usage_date,
               sum(r.rated_weight_nano_usd) / 1e9
        FROM rated_events r
        WHERE r.pricing_version_id = %(version)s AND r.telegram_user_id = %(user)s
          AND r.occurred_at_ms >= %(from)s AND r.occurred_at_ms < %(to)s
        GROUP BY 1 ORDER BY 1"""),
)


def key_hash(index: int) -> str:
    return hashlib.sha256(f"bench-key-{index}".encode()).hexdigest()


def load(dsn: str, users: int, keys_per_user: int, events: int, days: int, seed: int) -> dict[str, int]:
    rng = random.Random(seed)
    keys = users * keys_per_user + 20  # the last 20 keys are unowned
    weights = [1 / (rank + 1) ** 1.1 for rank in range(len(MODELS))]
    user_weights = [1 / (rank + 1) ** 0.9 for rank in range(users)]
    with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute("INSERT INTO cpamp_sources(name, schema_fingerprint, created_at_ms) VALUES ('bench', '', %s) RETURNING id",
                       (START_MS,))
        source_id = cursor.fetchone()[0]
        cursor.execute("INSERT INTO resource_pools(name, active, created_at_ms) VALUES ('default', true, %s) RETURNING id", (START_MS,))
        pool_id = cursor.fetchone()[0]
        versions = []
        for name, status in (("bench-v1", "retired"), ("bench-v2", "active")):
            cursor.execute("INSERT INTO pricing_versions(name, status, source, created_at_ms, activated_at_ms) "
                           "VALUES (%s, %s, 'bench', %s, %s) RETURNING id", (name, status, START_MS, START_MS))
            versions.append(cursor.fetchone()[0])
        with cursor.copy("COPY model_price_rules(pricing_version_id, model, input_nano_per_token, output_nano_per_token, "
                         "cache_read_nano_per_token, cache_creation_nano_per_token, input_configured, output_configured, "
                         "cache_read_configured, cache_creation_configured, long_input_multiplier_ppm, long_output_multiplier_ppm) "
                         "FROM STDIN") as copy:
            for version in versions:
                for model, input_price, output_price in MODELS:
                    copy.write_row((version, model, input_price, output_price, input_price // 10, input_price, True, True,
                                    True, True, 1_000_000, 1_000_000))
        with cursor.copy("COPY telegram_users(telegram_user_id, username, is_admin, manual_allowed, registered_at_ms, "
                         "last_seen_at_ms) FROM STDIN") as copy:
            for user in range(1, users + 1):
                copy.write_row((user, f"user{user}", user == 1, False, START_MS, START_MS))
        with cursor.copy("COPY api_keys(id, cpamp_hash, login_fingerprint, masked_value, status, current_owner_id, "
                         "present_in_cpa, created_at_ms) FROM STDIN") as copy:
            for index in range(1, keys + 1):
                owner = (index - 1) // keys_per_user + 1 if index <= users * keys_per_user else None
                copy.write_row((index, key_hash(index), hashlib.sha256(f"fp{index}".encode()).hexdigest(), f"sk-cpa-…{index:04d}",
                                "active" if owner else "unowned", owner, True, START_MS))
        owners: dict[int, list[tuple[int, int | None, int]]] = {}
        with cursor.copy("COPY key_ownership_periods(api_key_id, telegram_user_id, valid_from_ms, valid_to_ms, source, "
                         "created_at_ms) FROM STDIN") as copy:
            for index in range(1, users * keys_per_user + 1):
                owner = (index - 1) // keys_per_user + 1
                if index % 10 == 0:  # every tenth key changed hands halfway through
                    middle = START_MS + days // 2 * DAY_MS
                    previous = owner % users + 1
                    periods = [(START_MS, middle, previous), (middle, None, owner)]
                else:
                    periods = [(START_MS, None, owner)]
                owners[index] = [(start, end, user) for start, end, user in periods]
                for start, end, user in periods:
                    copy.write_row((index, user, start, end, "bench", START_MS))
        span = days * DAY_MS
        with cursor.copy("COPY raw_usage_events(id, source_id, source_event_id, event_hash, request_id, occurred_at_ms, "
                         "timestamp, provider, model, service_tier, api_key_hash, auth_index, input_tokens, output_tokens, "
                         "reasoning_tokens, cached_tokens, cache_tokens, cache_read_tokens, cache_creation_tokens, total_tokens, "
                         "failed, latency_ms, ttft_ms, imported_at_ms) FROM STDIN") as raw_copy:
            rated_rows = []
            for event in range(1, events + 1):
                model_index = rng.choices(range(len(MODELS)), weights)[0]
                model, input_price, output_price = MODELS[model_index]
                user = rng.choices(range(1, users + 1), user_weights)[0]
                key = (user - 1) * keys_per_user + rng.randrange(keys_per_user) + 1 if rng.random() > 0.03 \
                    else users * keys_per_user + rng.randrange(20) + 1
                occurred = START_MS + int(span * (event / events) ** 0.8) + rng.randrange(60_000)
                input_tokens = int(rng.lognormvariate(8.5, 1.2))
                cached = int(input_tokens * rng.random() * 0.6)
                output_tokens = int(rng.lognormvariate(6, 1))
                failed = rng.random() < 0.02
                raw_copy.write_row((event, source_id, event, f"bench-{event}", f"req-{event}", occurred, "", "bench", model,
                                    "default", key_hash(key), f"auth-{model_index % 4}", input_tokens, output_tokens, 0, cached,
                                    0, 0, 0, input_tokens + output_tokens, failed, rng.randrange(300, 30_000),
                                    rng.randrange(100, 3000), occurred))
                cost = 0 if failed else (input_tokens - cached) * input_price + cached * (input_price // 10) + output_tokens * output_price
                owner = next((user for start, end, user in owners.get(key, []) if start <= occurred and (end is None or occurred < end)),
                             None)
                detail = json.dumps({"price_model": model})
                rated_rows.append((event, versions[1], pool_id, owner, occurred, cost, False, "default", detail, occurred))
                if event % 2 == 0:  # half the history was also rated by the retired version
                    rated_rows.append((event, versions[0], pool_id, owner, occurred, cost, False, "default", detail, occurred))
        with cursor.copy("COPY rated_events(raw_event_id, pricing_version_id, pool_id, telegram_user_id, occurred_at_ms, "
                         "rated_weight_nano_usd, long_context_applied, service_tier, calculation_json, rated_at_ms) FROM STDIN") as copy:
            for row in rated_rows:
                copy.write_row(row)
        for table in ("raw_usage_events", "api_keys"):
            cursor.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), (SELECT max(id) FROM {table}))")
        connection.commit()
        connection.autocommit = True
        cursor.execute("VACUUM ANALYZE")
    return {"users": users, "api_keys": keys, "raw_usage_events": events, "rated_events": len(rated_rows), "active_version": versions[1]}


def explain(cursor: psycopg.Cursor, sql: str, params: dict[str, object], runs: int) -> tuple[float, str]:
    timings, plan = [], ""
    for _ in range(runs):
        cursor.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params)
        document = cursor.fetchone()[0][0]
        timings.append(document["Execution Time"])
        plan = summarize(document["Plan"])
    return statistics.median(timings), plan


def summarize(node: dict, depth: int = 0) -> str:
    label = node["Node Type"]
    if "Index Name" in node:
        label += f" using {node['Index Name']}"
    elif "Relation Name" in node:
        label += f" on {node['Relation Name']}"
    lines = ["  " * depth + label]
    for child in node.get("Plans", []):
        lines.append(summarize(child, depth + 1))
    return "\n".join(lines)


def benchmark(dsn: str, params: dict[str, object], runs: int) -> list[dict[str, object]]:
    results = []
    with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
        for query in QUERIES:
            with_index = explain(cursor, query.sql, params, runs)
            connection.rollback()
            for index in query.indexes:
                cursor.execute(f"DROP INDEX IF EXISTS {index}" if not index.endswith("_key")
                               else f"ALTER TABLE rated_events DROP CONSTRAINT {index}")
            without_index = explain(cursor, query.sql, params, runs)
            connection.rollback()  # restores the dropped indexes
            results.append({"query": query, "with": with_index, "without": without_index})
        for label, sql in REWRITE:
            results.append({"rewrite": label, "sql": sql, "with": explain(cursor, sql, params, runs)})
            connection.rollback()
    return results


def report(meta: dict[str, int], results: list[dict[str, object]], load_seconds: float) -> str:
    lines = [
        "# PostgreSQL 查询性能测试结果", "",
        f"- 数据规模：用户 {meta['users']}，API Key {meta['api_keys']}，原始调用记录 {meta['raw_usage_events']:,}，"
        f"计价记录 {meta['rated_events']:,}；装载耗时 {load_seconds:.1f} s",
        "- 每条查询执行 EXPLAIN (ANALYZE, BUFFERS) 多次取中位数；无索引组在事务内删除索引后测量并回滚。", "",
        "| 查询 | 业务场景 | 有索引 (ms) | 无索引 (ms) | 加速比 |", "|---|---|---:|---:|---:|",
    ]
    for item in results:
        if "query" in item:
            query: Query = item["query"]
            fast, slow = item["with"][0], item["without"][0]
            lines.append(f"| {query.name} | {query.purpose} | {fast:.2f} | {slow:.2f} | {slow / fast:.1f}× |")
    lines += ["", "## 执行计划对比", ""]
    for item in results:
        if "query" in item:
            query = item["query"]
            lines += [f"### {query.name}", "", "```sql", " ".join(query.sql.split()), "```", "",
                      "有索引：", "```", item["with"][1], "```", "无索引：", "```", item["without"][1], "```", ""]
    lines += ["## SQL 改写对比", "", "| 写法 | 耗时 (ms) |", "|---|---:|"]
    for item in results:
        if "rewrite" in item:
            lines.append(f"| {item['rewrite']} | {item['with'][0]:.2f} |")
    for item in results:
        if "rewrite" in item:
            lines += ["", f"**{item['rewrite']}**", "```sql", " ".join(item["sql"].split()), "```", "```", item["with"][1], "```"]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", help="SQLAlchemy URL of an empty PostgreSQL database")
    parser.add_argument("--users", type=int, default=120)
    parser.add_argument("--keys-per-user", type=int, default=3)
    parser.add_argument("--events", type=int, default=1_000_000)
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20261028)
    parser.add_argument("--report", type=Path, default=Path("benchmark.md"))
    args = parser.parse_args()

    database = Database(args.url, pool_size=1, max_overflow=0)
    if database.dialect != "postgresql":
        raise SystemExit("the benchmark targets PostgreSQL")
    with database.engine.connect() as connection:
        if inspect(connection).get_table_names():
            raise SystemExit("target database must be empty")
    database.initialize()
    database.engine.dispose()
    dsn = make_url(args.url).set(drivername="postgresql").render_as_string(hide_password=False)

    started = time.perf_counter()
    meta = load(dsn, args.users, args.keys_per_user, args.events, args.days, args.seed)
    load_seconds = time.perf_counter() - started
    params = parameters(args.users, args.days) | {"version": meta["active_version"]}
    params |= {
        "from_day": time.strftime("%Y-%m-%d", time.gmtime((params["from"] + 8 * 3_600_000) / 1000)),
        "to_day": time.strftime("%Y-%m-%d", time.gmtime((params["to"] + 8 * 3_600_000) / 1000)),
    }
    args.report.write_text(report(meta, benchmark(dsn, params, args.runs), load_seconds), encoding="utf-8")
    print("report written", args.report)


if __name__ == "__main__":
    main()
