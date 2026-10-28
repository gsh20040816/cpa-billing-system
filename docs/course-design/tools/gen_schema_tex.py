"""Generate the data-dictionary and trigger tables of the course report from the live SQLAlchemy models.

    python docs/course-design/tools/gen_schema_tex.py docs/course-design/generated

Every table and column must have a description here; the script fails when the schema gains an
undocumented column, so the report cannot silently drift from the code.
"""
from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.dialects import postgresql

from cpa_billing.integrity import trigger_definitions
from cpa_billing.models import Base

# table -> (Chinese name, group, column descriptions)
TABLES: dict[str, tuple[str, str, dict[str, str]]] = {
    "telegram_users": ("用户", "用户与密钥", {
        "telegram_user_id": "用户编号（Telegram 用户 ID）", "username": "用户名", "first_name": "名", "last_name": "姓",
        "is_admin": "是否管理员", "manual_allowed": "是否被管理员手工授权注册", "registered_at_ms": "注册时间，空表示未注册",
        "last_seen_at_ms": "最近活跃时间"}),
    "api_keys": ("API 密钥", "用户与密钥", {
        "id": "密钥编号", "cpamp_hash": "密钥 SHA-256 摘要，用于与调用记录匹配", "login_fingerprint": "带服务端密钥的 HMAC 指纹，用于登录查找",
        "masked_value": "脱敏显示值", "display_name": "备注名", "status": "状态：active/unowned/revoked/retired",
        "current_owner_id": "当前归属用户", "billing_multiplier_ppm": "未绑定密钥的按量计费倍率（×10⁶）",
        "present_in_cpa": "是否仍在网关中启用", "last_seen_in_cpa_at_ms": "最近一次在网关中出现的时间",
        "created_at_ms": "创建时间", "revoked_at_ms": "吊销时间"}),
    "key_ownership_periods": ("密钥归属期", "用户与密钥", {
        "id": "归属期编号", "api_key_id": "密钥", "telegram_user_id": "归属用户", "valid_from_ms": "生效时间（含）",
        "valid_to_ms": "失效时间（不含），空表示至今", "source": "来源：注册/新增/转移等", "reason": "变更原因",
        "operator_user_id": "操作人", "created_at_ms": "记录时间"}),
    "key_action_requests": ("密钥操作请求", "用户与密钥", {
        "token_hash": "一次性确认令牌的 HMAC", "telegram_user_id": "发起用户", "action": "操作：add/reset/revoke",
        "target_api_key_id": "目标密钥", "status": "状态：pending/processing/completed", "created_at_ms": "创建时间",
        "expires_at_ms": "过期时间", "confirmed_at_ms": "确认时间", "result_masked_key": "结果密钥的脱敏值"}),
    "group_memberships": ("群组成员资格", "用户与密钥", {
        "telegram_user_id": "用户", "group_chat_id": "授权群组", "status": "成员状态", "legal": "是否具有注册资格",
        "updated_at_ms": "缓存更新时间"}),
    "allowed_chats": ("允许的会话", "用户与密钥", {
        "chat_id": "会话编号", "note": "备注", "updated_at_ms": "更新时间"}),
    "web_sessions": ("用户 Web 会话", "安全", {
        "session_hash": "会话令牌的 HMAC", "telegram_user_id": "用户，空表示未绑定密钥的只读访客", "api_key_id": "登录所用密钥",
        "csrf_token": "CSRF 令牌", "created_at_ms": "创建时间", "expires_at_ms": "过期时间", "revoked_at_ms": "撤销时间"}),
    "admin_web_sessions": ("管理员 Web 会话", "安全", {
        "session_hash": "会话令牌的 HMAC", "credential_fingerprint": "管理令牌指纹，令牌轮换后会话失效", "csrf_token": "CSRF 令牌",
        "created_at_ms": "创建时间", "expires_at_ms": "过期时间", "revoked_at_ms": "撤销时间"}),
    "cpamp_sources": ("用量数据源", "调用记录", {
        "id": "数据源编号", "name": "数据源名称", "schema_fingerprint": "源表结构指纹", "created_at_ms": "创建时间"}),
    "sync_checkpoints": ("同步检查点", "调用记录", {
        "source_id": "数据源", "last_event_id": "已同步的最大源记录号", "last_event_at_ms": "最近记录的发生时间",
        "last_success_at_ms": "最近一次成功同步时间", "backlog": "待同步记录数", "last_error": "最近错误"}),
    "raw_usage_events": ("原始调用记录", "调用记录", {
        "id": "记录编号", "source_id": "数据源", "source_event_id": "源记录号", "event_hash": "事件摘要（去重）",
        "request_id": "请求编号", "occurred_at_ms": "调用时间", "timestamp": "源时间字符串", "provider": "上游服务商",
        "executor_type": "执行器类型", "model": "模型名", "requested_model": "请求的模型名", "resolved_model": "实际解析的模型名",
        "reasoning_effort": "推理强度", "service_tier": "服务档位", "request_service_tier": "请求声明的档位",
        "response_service_tier": "响应返回的档位", "api_key_hash": "调用所用密钥摘要", "source_hash": "来源摘要",
        "source_label": "来源标签", "account_snapshot": "上游账号快照", "auth_index": "上游账号索引",
        "input_tokens": "输入 Token", "output_tokens": "输出 Token", "reasoning_tokens": "推理 Token",
        "cached_tokens": "缓存命中 Token", "cache_tokens": "缓存 Token（兼容字段）", "cache_read_tokens": "缓存读取 Token",
        "cache_creation_tokens": "缓存写入 Token", "total_tokens": "总 Token", "failed": "是否失败",
        "fail_status_code": "失败状态码", "latency_ms": "总耗时", "ttft_ms": "首 Token 时间",
        "response_metadata_json": "响应元数据", "quota_used_percent": "上游额度使用率（×10⁶）",
        "quota_recover_at_ms": "额度恢复时间", "quota_plan_type": "上游套餐类型", "imported_at_ms": "导入时间"}),
    "dead_letters": ("死信记录", "调用记录", {
        "id": "编号", "source_id": "数据源", "source_event_id": "源记录号", "error": "解析错误", "payload_json": "原始负载",
        "created_at_ms": "创建时间", "resolved_at_ms": "解决时间"}),
    "pricing_versions": ("价格版本", "价格", {
        "id": "版本编号", "name": "版本名", "status": "状态：draft/active/retired", "source": "来源",
        "created_at_ms": "创建时间", "activated_at_ms": "生效时间"}),
    "model_price_rules": ("模型价格", "价格", {
        "pricing_version_id": "价格版本", "model": "模型名", "input_nano_per_token": "输入单价（纳美元/Token）",
        "output_nano_per_token": "输出单价", "cache_read_nano_per_token": "缓存读取单价",
        "cache_creation_nano_per_token": "缓存写入单价", "input_configured": "输入价是否配置",
        "output_configured": "输出价是否配置", "cache_read_configured": "缓存读取价是否配置",
        "cache_creation_configured": "缓存写入价是否配置", "priority_input_nano_per_token": "Priority 档输入单价",
        "priority_output_nano_per_token": "Priority 档输出单价", "priority_cache_read_nano_per_token": "Priority 档缓存读取单价",
        "priority_cache_creation_nano_per_token": "Priority 档缓存写入单价", "flex_input_nano_per_token": "Flex 档输入单价",
        "flex_output_nano_per_token": "Flex 档输出单价", "flex_cache_read_nano_per_token": "Flex 档缓存读取单价",
        "flex_cache_creation_nano_per_token": "Flex 档缓存写入单价", "context_tiers_json": "上下文长度分段价（JSON）",
        "long_threshold_tokens": "历史长上下文阈值（兼容）", "long_input_multiplier_ppm": "历史长上下文输入倍率（兼容）",
        "long_output_multiplier_ppm": "历史长上下文输出倍率（兼容）", "raw_json": "上游原始价格数据"}),
    "pricing_rerate_scopes": ("重新计价范围", "价格", {
        "pricing_version_id": "价格版本", "ranges_json": "需重算的时间区间", "models_json": "价格变化的模型",
        "max_raw_event_id": "版本生效时的最大调用记录号", "created_at_ms": "创建时间"}),
    "resource_pools": ("资源池", "价格", {
        "id": "资源池编号", "name": "名称", "active": "是否启用", "created_at_ms": "创建时间"}),
    "pool_assignment_rules": ("资源池分配规则", "价格", {
        "id": "规则编号", "pool_id": "资源池", "priority": "优先级（小者优先）", "auth_index_pattern": "上游账号正则",
        "model_pattern": "模型名正则", "active": "是否启用"}),
    "rated_events": ("计价记录", "调用记录", {
        "id": "编号", "raw_event_id": "原始调用记录", "pricing_version_id": "价格版本", "pool_id": "资源池",
        "telegram_user_id": "调用时刻的归属用户", "occurred_at_ms": "调用时间（冗余，便于按期聚合）",
        "rated_weight_nano_usd": "等效费用（纳美元）", "long_context_applied": "是否使用长上下文价",
        "service_tier": "计费档位", "calculation_json": "计算明细", "rated_at_ms": "计价时间"}),
    "gradient_rules": ("梯度分摊规则", "分摊", {
        "id": "规则编号", "name": "名称", "description": "说明", "tiers_json": "分段与倍率（JSON）", "active": "是否启用",
        "created_at_ms": "创建时间", "updated_at_ms": "更新时间"}),
    "upstream_account_groups": ("上游账号分组", "分摊", {
        "id": "分组编号", "name": "名称", "is_default": "是否默认分组", "gradient_rule_id": "采用的梯度规则",
        "created_at_ms": "创建时间", "updated_at_ms": "更新时间"}),
    "upstream_account_configs": ("上游账号成本配置", "分摊", {
        "account_id": "上游账号", "group_id": "所属分组", "subscription_mode": "订阅方式：一次性/周期",
        "period_start_at_ms": "订阅开始", "period_end_at_ms": "订阅结束", "recurring_unit": "周期单位",
        "recurring_interval": "周期长度", "period_cost_cents": "每期费用（分）", "rate_ppm": "按量渠道汇率（元/美元×10⁶）",
        "updated_at_ms": "更新时间"}),
    "billing_cycles": ("账期", "账单", {
        "id": "账期编号", "name": "账期名", "start_at_ms": "开始时间（含）", "end_at_ms": "结束时间（不含）", "timezone": "时区",
        "status": "状态：open/preview/closed", "pricing_version_id": "采用的价格版本", "gradient_rule_id": "采用的梯度规则",
        "tiers_json": "梯度规则快照", "data_quality_waiver": "数据质量豁免说明", "created_at_ms": "创建时间",
        "closed_at_ms": "关账时间", "closed_by": "关账人"}),
    "cycle_pool_costs": ("账期资源池成本", "账单", {
        "cycle_id": "账期", "pool_id": "资源池", "fixed_cost_cents": "固定成本（分）"}),
    "cycle_upstream_costs": ("账期上游成本快照", "账单", {
        "cycle_id": "账期", "account_id": "上游账号", "auth_index": "上游账号索引", "account_name": "账号名",
        "auth_type": "账号类型：oauth/api_key", "fixed_cost_cents": "固定成本（分）", "rate_ppm": "按量汇率快照",
        "request_count": "请求数", "token_count": "Token 数", "actual_weight_nano_usd": "实际用量（纳美元）",
        "amount_cents": "成本金额（分）", "group_id": "分组快照", "group_name": "分组名快照",
        "subscription_mode": "订阅方式快照", "period_start_at_ms": "订阅开始快照", "period_end_at_ms": "订阅结束快照",
        "recurring_unit": "周期单位快照", "recurring_interval": "周期长度快照", "period_cost_cents": "每期费用快照"}),
    "cycle_groups": ("账期分组结算", "账单", {
        "cycle_id": "账期", "group_id": "分组", "group_name": "分组名快照", "gradient_rule_id": "梯度规则",
        "tiers_json": "梯度快照", "is_default": "是否默认分组", "fixed_cost_cents": "固定成本", "dynamic_cost_cents": "按量成本",
        "metered_amount_cents": "按量密钥收费", "residual_cost_cents": "待成员分摊成本", "member_amount_cents": "成员分摊合计",
        "surplus_cents": "按量收费超出成本部分", "unallocated_cents": "无法分摊部分"}),
    "statements": ("账单", "账单", {
        "id": "账单编号", "cycle_id": "账期", "telegram_user_id": "用户", "actual_weight_nano_usd": "实际用量（纳美元）",
        "billed_weight_nano_usd": "梯度加权用量", "amount_cents": "应付金额（分）", "adjustment_cents": "调整金额（分）",
        "generated_at_ms": "生成时间", "final": "是否已定稿（关账）"}),
    "statement_lines": ("账单明细", "账单", {
        "id": "明细编号", "statement_id": "账单", "pool_id": "资源池", "group_id": "分组",
        "actual_weight_nano_usd": "实际用量", "billed_weight_nano_usd": "加权用量", "amount_cents": "金额（分）",
        "api_key_count": "密钥数"}),
    "metered_key_charges": ("按量密钥收费", "账单", {
        "id": "编号", "cycle_id": "账期", "pool_id": "资源池", "api_key_id": "密钥", "actual_weight_nano_usd": "实际用量",
        "multiplier_ppm": "倍率（×10⁶）", "amount_cents": "金额（分）", "generated_at_ms": "生成时间", "final": "是否定稿"}),
    "adjustments": ("账单调整", "账单", {
        "id": "编号", "cycle_id": "账期", "telegram_user_id": "用户", "amount_cents": "调整金额（分，可为负）",
        "reason": "原因", "operator_user_id": "操作人", "created_at_ms": "创建时间"}),
    "manual_usage_adjustments": ("手工用量", "账单", {
        "id": "编号", "cycle_id": "账期", "pool_id": "资源池", "group_id": "分组", "telegram_user_id": "用户",
        "amount_nano_usd": "等效用量（纳美元，非零）", "reason": "原因", "operator_user_id": "操作人",
        "created_at_ms": "创建时间", "updated_at_ms": "更新时间"}),
    "audit_logs": ("审计日志", "安全", {
        "id": "编号", "operator_type": "操作者类型", "operator_id": "操作者", "operation": "操作", "target": "对象",
        "before_json": "修改前", "after_json": "修改后", "reason": "原因", "created_at_ms": "时间"}),
    "reconciliation_runs": ("对账记录", "安全", {
        "id": "编号", "cycle_id": "账期", "result_json": "对账结果", "ok": "是否通过", "created_at_ms": "时间"}),
}

GROUP_ORDER = ("用户与密钥", "调用记录", "价格", "分摊", "账单", "安全")


def tex(value: str) -> str:
    return (value.replace("\\", r"\textbackslash{}").replace("_", r"\_").replace("%", r"\%").replace("&", r"\&")
            .replace("#", r"\#").replace("{", r"\{").replace("}", r"\}"))


def column_type(column) -> str:
    return str(column.type.compile(dialect=postgresql.dialect())).replace("VARCHAR", "VARCHAR")


def keys(table, column) -> str:
    marks = []
    if column.primary_key:
        marks.append("PK")
    for foreign in column.foreign_keys:
        marks.append(f"FK→{foreign.column.table.name}")
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint) and column.name in constraint.columns:
            marks.append("UQ" if len(constraint.columns) == 1 else "UQ*")
    if column.unique:
        marks.append("UQ")
    return tex(", ".join(dict.fromkeys(marks)))


def dictionary() -> str:
    tables = {table.name: table for table in Base.metadata.sorted_tables}
    missing = set(tables) ^ set(TABLES)
    if missing:
        raise SystemExit(f"tables without descriptions: {sorted(missing)}")
    out = []
    for group in GROUP_ORDER:
        out.append(f"\\subsubsection*{{{group}}}")
        for name, (title, table_group, docs) in TABLES.items():
            if table_group != group:
                continue
            table = tables[name]
            undocumented = {column.name for column in table.columns} ^ set(docs)
            if undocumented:
                raise SystemExit(f"{name}: columns without descriptions: {sorted(undocumented)}")
            out.append(f"\\paragraph{{{tex(name)}（{title}）}}\\mbox{{}}\n")
            out.append("\\begin{longtable}{@{}p{0.27\\textwidth}p{0.15\\textwidth}c p{0.15\\textwidth}p{0.33\\textwidth}@{}}")
            out.append("\\toprule 字段 & 类型 & 空 & 键 & 说明\\\\ \\midrule\\endhead")
            for column in table.columns:
                null = "√" if column.nullable and not column.primary_key else ""
                out.append(f"\\texttt{{{tex(column.name)}}} & \\texttt{{\\small {tex(column_type(column))}}} & {null} & "
                           f"\\small {keys(table, column)} & {tex(docs[column.name])}\\\\")
            checks = [constraint for constraint in table.constraints if isinstance(constraint, CheckConstraint)]
            composite = [constraint for constraint in table.constraints
                         if isinstance(constraint, UniqueConstraint) and len(constraint.columns) > 1]
            notes = [f"唯一（{', '.join(c.name for c in constraint.columns)}）" for constraint in composite]
            notes += [f"CHECK {constraint.sqltext}" for constraint in checks]
            if notes:
                out.append("\\midrule\\multicolumn{5}{@{}p{0.98\\textwidth}@{}}{\\small 表级约束：" +
                           "；".join(f"\\texttt{{{tex(note)}}}" for note in notes) + "}\\\\")
            out.append("\\bottomrule\\end{longtable}\n")
    return "\n".join(out)


def triggers() -> str:
    """Rows of the trigger table: name, table, events, and the condition/action in one readable cell."""
    rows = []
    for name, body in trigger_definitions("postgresql").items():
        timing, rest = body.split(" ON ", 1)
        table = rest.split()[0]
        events = " / ".join(timing.replace("BEFORE ", "").split(" OR "))
        action = body.rsplit("EXECUTE FUNCTION ", 1)[1]
        when = body.split("WHEN (", 1)[1].rsplit(") EXECUTE", 1)[0] if "WHEN (" in body else ""
        if len(when) > 60:
            condition = "计费相关列被修改时"
        elif when:
            condition = f"当 \\texttt{{{tex(when)}}} 时"
        else:
            condition = "任何时候"
        if action.startswith("billing_reject("):
            effect = f"拒绝：\\texttt{{{tex(action[len('billing_reject(') + 1:-2])}}}"
        else:
            effect = f"调用 \\texttt{{{tex(action)}}} 检查"
        rows.append(f"\\texttt{{{tex(name)}}} & \\texttt{{{tex(table)}}} & {events} & {condition}{effect}\\\\")
    return "\n".join(rows) + "\n\\bottomrule"


SCHEMA_TABLES = (
    "telegram_users", "api_keys", "key_ownership_periods", "raw_usage_events", "rated_events", "pricing_versions",
    "model_price_rules", "resource_pools", "gradient_rules", "upstream_account_groups", "upstream_account_configs",
    "billing_cycles", "cycle_upstream_costs", "cycle_groups", "statements", "statement_lines", "adjustments",
)


def schema_dot() -> str:
    """Relational schema of the core tables: keys only, foreign keys as edges."""
    tables = {table.name: table for table in Base.metadata.sorted_tables}
    lines = ['digraph schema {', '  graph [rankdir=LR, fontname="PingFang SC", nodesep=0.2, ranksep=0.55, concentrate=true];',
             '  node [shape=plaintext, fontname="Menlo", fontsize=8];', '  edge [color="#5b6b88", arrowsize=0.6];']
    for name in SCHEMA_TABLES:
        table = tables[name]
        rows = []
        for column in table.columns:
            foreign = [f.column.table.name for f in column.foreign_keys if f.column.table.name in SCHEMA_TABLES]
            if not (column.primary_key or foreign):
                continue
            mark = "PK" if column.primary_key else "FK"
            rows.append(f'<tr><td align="left" port="{column.name}">{mark} {column.name}</td></tr>')
        others = len([c for c in table.columns if not c.primary_key and not c.foreign_keys])
        rows.append(f'<tr><td align="left"><font color="#6b7280">… {others} 个属性</font></td></tr>')
        lines.append(f'  {name} [label=<<table border="1" cellborder="0" cellspacing="0" cellpadding="2" color="#3d4f75">'
                     f'<tr><td bgcolor="#e6edf9"><b>{name}</b><br/><font face="PingFang SC">{TABLES[name][0]}</font></td></tr>'
                     + "".join(rows) + "</table>>];")
    for name in SCHEMA_TABLES:
        for column in tables[name].columns:
            for foreign in column.foreign_keys:
                if foreign.column.table.name in SCHEMA_TABLES:
                    lines.append(f"  {name}:{column.name} -> {foreign.column.table.name};")
    lines.append("}")
    return "\n".join(lines)


def main() -> None:
    target = Path(sys.argv[1])
    target.mkdir(parents=True, exist_ok=True)
    (target / "data-dictionary.tex").write_text(dictionary(), encoding="utf-8")
    (target / "triggers.tex").write_text(triggers(), encoding="utf-8")
    (target / "schema.dot").write_text(schema_dot(), encoding="utf-8")
    print("generated", target)


if __name__ == "__main__":
    main()
