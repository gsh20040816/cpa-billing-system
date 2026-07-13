from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import tempfile
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import psycopg

from .config import Settings
from .database import Database
from .domain import NANO_USD, format_cents, format_usd_nano, largest_remainder, parse_tiers, tiered_weight
from .services import BillingError, BillingService, DEFAULT_TIERS


LOG = logging.getLogger("cpa_billing.bot")
MEMBER = {"creator", "administrator", "member"}
COMMAND_MESSAGE_LIMIT = 3900
MEMBERSHIP_CACHE_TTL_MS = 5 * 60_000
HTML_TAG = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9-]*)(?:\s[^>]*)?>")


def esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


def compact(value: int) -> str:
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.2f}K"
    return str(value)


def telegram_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def html_chunks(text: str, limit: int = COMMAND_MESSAGE_LIMIT) -> list[str]:
    """Split Telegram HTML messages without leaving an unclosed tag in a chunk."""
    if telegram_length(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    open_tags: list[tuple[str, str]] = []

    def closing_tags() -> str:
        return "".join(f"</{name}>" for name, _ in reversed(open_tags))

    def flush() -> None:
        nonlocal current
        if not current:
            return
        chunks.append(current + closing_tags())
        current = "".join(tag for _, tag in open_tags)

    def next_tags(part: str) -> list[tuple[str, str]]:
        updated = list(open_tags)
        match = HTML_TAG.fullmatch(part)
        if not match:
            return updated
        closing, name = match.groups()
        name = name.lower()
        if closing:
            for index in range(len(updated) - 1, -1, -1):
                if updated[index][0] == name:
                    del updated[index]
                    break
        elif not part.endswith("/>"):
            updated.append((name, part))
        return updated

    def take_prefix(value: str, available: int) -> str:
        used = 0
        index = 0
        for index, character in enumerate(value):
            units = telegram_length(character)
            if used + units > available:
                break
            used += units
        else:
            return value
        return value[:index]

    for part in re.split(r"(<(?:/?)[A-Za-z][^>]*>)", text):
        if not part:
            continue
        match = HTML_TAG.fullmatch(part)
        if match:
            updated = next_tags(part)
            closing_after = "".join(f"</{name}>" for name, _ in reversed(updated))
            if current and telegram_length(current) + telegram_length(part) + telegram_length(closing_after) > limit:
                flush()
            current += part
            open_tags[:] = updated
            continue

        remaining = part
        while remaining:
            available = limit - telegram_length(current) - telegram_length(closing_tags())
            if available <= 0:
                flush()
                continue
            prefix = take_prefix(remaining, available)
            if not prefix:
                flush()
                continue
            current += prefix
            remaining = remaining[len(prefix):]
            if remaining:
                flush()

    if current:
        flush()
    return chunks or [""]


def parse_user_id(args: str) -> int | None:
    token = args.strip().split(maxsplit=1)
    if not token:
        return None
    try:
        return int(token[0])
    except ValueError:
        return None


def parse_fixed_cost_cents(value: str) -> int | None:
    try:
        amount = Decimal(value.strip())
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return int((amount * 100).to_integral_value(rounding=ROUND_HALF_UP))


class TelegramAPI:
    def __init__(self, token: str) -> None:
        self.url = f"https://api.telegram.org/bot{token}"
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(15, read=45))

    async def call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        response = await self.client.post(f"{self.url}/{method}", json=payload or {})
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise BillingError(f"Telegram {method} failed")
        return data.get("result")

    async def send(self, chat_id: int, text: str) -> None:
        for chunk in html_chunks(text):
            await self.call("sendMessage", {"chat_id": chat_id, "text": chunk, "parse_mode": "HTML", "disable_web_page_preview": True})

    async def member(self, chat_id: int, user_id: int) -> dict[str, Any]:
        return dict(await self.call("getChatMember", {"chat_id": chat_id, "user_id": user_id}) or {})

    async def me(self) -> dict[str, Any]:
        return dict(await self.call("getMe") or {})

    async def photo(self, chat_id: int, path: Path, caption: str) -> None:
        with path.open("rb") as image:
            response = await self.client.post(f"{self.url}/sendPhoto", data={"chat_id": str(chat_id), "caption": caption}, files={"photo": image})
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise BillingError("Telegram sendPhoto failed")


class BillingBot:
    def __init__(self, settings: Settings, service: BillingService) -> None:
        if not settings.telegram_token:
            raise RuntimeError("TG_BOT_TOKEN is required")
        self.settings, self.service = settings, service
        self.tg = TelegramAPI(settings.telegram_token)
        self.bot_username: str | None = None

    async def eligible(self, user: dict[str, Any]) -> bool:
        user_id = int(user["id"])
        if user_id in self.settings.admin_user_ids:
            return True
        if await asyncio.to_thread(self.service.user_is_eligible_cached, user_id, MEMBERSHIP_CACHE_TTL_MS):
            return True
        for group_id in self.settings.allowed_group_ids:
            try:
                member = await self.tg.member(group_id, user_id)
                status = str(member.get("status", ""))
                legal = status in MEMBER or (status == "restricted" and bool(member.get("is_member")))
                await asyncio.to_thread(self.service.set_membership, user, group_id, status, legal)
                if legal:
                    return True
            except Exception:
                LOG.exception("membership check failed user=%s group=%s", user_id, group_id)
        return False

    def is_admin(self, user: dict[str, Any]) -> bool:
        return int(user.get("id", 0)) in self.settings.admin_user_ids

    async def handle_chat_member(self, update: dict[str, Any]) -> None:
        change = update.get("chat_member") or {}
        chat = change.get("chat") or {}
        chat_id = int(chat.get("id", 0))
        if chat_id not in self.settings.allowed_group_ids:
            return
        member = change.get("new_chat_member") or {}
        user = member.get("user") or {}
        if not user or user.get("is_bot"):
            return
        status = str(member.get("status", ""))
        legal = status in MEMBER or (status == "restricted" and bool(member.get("is_member")))
        await asyncio.to_thread(self.service.set_membership, user, chat_id, status, legal)

    async def dispatch(self, message: dict[str, Any]) -> str:
        chat, user = message.get("chat") or {}, message.get("from") or {}
        if not user or user.get("is_bot"):
            return ""
        text = str(message.get("text") or "").strip()
        if not text.startswith("/"):
            return ""
        command_token, *arg_parts = text.split(maxsplit=1)
        args = arg_parts[0] if arg_parts else ""
        command_name, mention_separator, mention = command_token.partition("@")
        if mention_separator and (not self.bot_username or mention.casefold() != self.bot_username.casefold()):
            return ""
        await asyncio.to_thread(self.service.upsert_user, user)
        command = command_name.lower()
        private = chat.get("type") == "private"
        if command in {"/start", "/help"}:
            return self.help(self.is_admin(user))
        if command == "/id":
            return f"chat_id: <code>{chat.get('id')}</code>\nuser_id: <code>{user.get('id')}</code>\ntype: <code>{esc(chat.get('type'))}</code>"
        if command in {"/register", "/mykey", "/resetkey", "/revoke", "/confirm"} and not private:
            return "这个命令只能私聊 bot 使用，避免 API Key 发到群里。"
        if command == "/register":
            if not await self.eligible(user):
                return "你不在合法用户组内，不能自助注册。"
            key = await asyncio.to_thread(self.service.register_key, user)
            return f"注册完成，已新增并绑定到你的 Telegram 用户：\n\n<code>{esc(key)}</code>\n\n请立即保存。系统不会再次显示完整 Key。\nBase URL：<code>https://cpa.shgao.top/v1</code>"
        if command == "/mykey":
            keys = await asyncio.to_thread(self.service.active_keys, int(user["id"]))
            if not keys:
                return "你还没有注册。发送 /register 注册。"
            return "你当前绑定的 API Key：\n" + "\n".join(f"KEY_ID=<code>{key.id}</code> <code>{esc(key.masked_value)}</code>" for key in keys)
        if command == "/confirm":
            if not args.strip():
                return "用法：<code>/confirm TOKEN</code>"
            raw = await asyncio.to_thread(self.service.confirm_key_action, int(user["id"]), args.strip())
            if raw:
                return f"操作已确认。新 API Key：\n\n<code>{esc(raw)}</code>\n\n请立即保存，系统不会再次显示完整 Key。"
            return "操作已确认并完成。"
        if command in {"/resetkey", "/revoke"}:
            keys = await asyncio.to_thread(self.service.active_keys, int(user["id"]))
            selector = args.strip()
            if not keys:
                return "你没有正在使用的 API Key。"
            if not selector and len(keys) != 1:
                return "你有多个 API Key，请指定 KEY_ID。"
            target = keys[0] if not selector else next((key for key in keys if str(key.id) == selector), None)
            if target is None:
                return "找不到指定 KEY_ID。"
            raw = await asyncio.to_thread(self.service.telegram_key_action, int(user["id"]), "reset" if command == "/resetkey" else "revoke", target.id)
            if raw:
                return f"已重置指定 API Key：\n\n<code>{esc(raw)}</code>\n\n请立即保存，系统不会再次显示完整 Key。"
            return "已吊销指定 API Key。"
        if command in {"/createuser", "/bindemail"}:
            return "CPA 版不再按邮箱创建/绑定用户。请使用 /register 自助注册并绑定 Telegram 用户。"
        if command == "/image":
            return "CPA 版暂未迁移旧 Sub2API 的 /image 生图入口。当前请直接使用你的 CPA API Key 调用模型。"
        if command == "/cancel":
            return "没有正在进行的交互操作。"
        if command in {"/usage", "/models", "/ranking", "/chart", "/accounts", "/billing", "/sub2billing"}:
            chat_allowed = await asyncio.to_thread(self.service.chat_is_allowed, int(chat.get("id", 0)))
            if not (chat_allowed or await self.eligible(user)):
                return "没有权限查询。"
            if command == "/usage":
                data = await asyncio.to_thread(self.service.usage_summary)
                return "\n".join(["<b>CPA Usage</b>", "", f"近 24h 请求：<code>{data['recent_requests']:,}</code>",
                                  f"近 24h Tokens：<code>{compact(data['recent_tokens'])}</code>", f"近 24h 估算成本：<code>{data['recent_cost']}</code>",
                                  f"累计请求：<code>{data['total_requests']:,}</code>", f"累计 Tokens：<code>{compact(data['total_tokens'])}</code>",
                                  f"累计估算成本：<code>{data['total_cost']}</code>"])
            if command == "/models":
                rows = await asyncio.to_thread(self.service.model_usage, 10)
                return "<b>模型用量 Top 10</b>\n\n" + "\n".join(f"{i}. <b>{esc(r['model'])}</b> req=<code>{r['requests']:,}</code> tokens=<code>{compact(r['tokens'])}</code> cost=<code>{r['cost']}</code>" for i, r in enumerate(rows, 1))
            if command == "/ranking":
                recent = await asyncio.to_thread(self.service.rankings, int(datetime.now().timestamp() * 1000) - 86_400_000)
                total = await asyncio.to_thread(self.service.rankings, None)
                def section(title: str, rows: list[dict[str, Any]]) -> str:
                    return f"<b>{title}</b>\n\n" + "\n".join(f"{i}. <b>{esc(r['name'])}</b> req=<code>{r['requests']:,}</code> tokens=<code>{compact(r['tokens'])}</code> cost=<code>{r['cost']}</code> API Key=<code>{r['key_count']}</code>" for i, r in enumerate(rows, 1))
                return section("近 24h Telegram 用户用量排行（未绑定 key 单独列出）", recent) + "\n\n" + section("全部 Telegram 用户用量排行（未绑定 key 单独列出）", total)
            if command == "/chart":
                await self.send_chart(int(chat["id"]))
                return ""
            if command == "/accounts":
                rows = await asyncio.to_thread(self.service.account_usage, 20)
                return "<b>账号/来源用量 Top 20</b>\n\n" + "\n".join(f"{i}. <b>{esc(r['name'])}</b> req=<code>{r['requests']:,}</code> tokens=<code>{compact(r['tokens'])}</code> cost=<code>{r['cost']}</code>\n额度 {esc(r['quota'])}" for i, r in enumerate(rows, 1))
            if command == "/billing":
                data = await asyncio.to_thread(self.service.dashboard, args.strip() or None)
                if not data["cycle"]:
                    return "<b>计费周期费用</b>\n\n当前没有活跃计费周期。"
                lines = ["<b>计费周期费用</b>", "", f"名称：<code>{esc(data['cycle']['name'])}</code>", f"状态：<code>{esc(data['cycle']['status'])}</code>",
                         f"合计 实际=<code>{data['totals']['actual']}</code> 计费=<code>{data['totals']['billed']}</code>", f"分摊总额=<code>{data['totals']['amount']}</code>"]
                if data["cycle"].get("waiver"):
                    lines += ["", f"数据质量说明：{esc(data['cycle']['waiver'])}"]
                lines.append("")
                for i, row in enumerate(data["rows"], 1):
                    lines.append(f"{i}. <b>{esc(row['name'])}</b>\n实际=<code>{row['actual']}</code> 计费=<code>{row['billed']}</code> 预估付费=<code>{row['amount']}</code> API Key=<code>{row['key_count']}</code>")
                return "\n".join(lines)
            if command == "/sub2billing":
                return await asyncio.to_thread(self.sub2billing)
        if not self.is_admin(user):
            return "没有权限。"
        if command == "/users":
            rows = await asyncio.to_thread(self.service.list_users)
            return "最近注册用户：\n" + "\n".join(f"<code>{r['id']}</code> {esc(r['username'])} API Key=<code>{r['keys']}</code>" for r in rows[:30])
        if command == "/stats":
            rows = await asyncio.to_thread(self.service.list_users)
            return f"当前状态：\n用户：<code>{len(rows)}</code>\n有效注册：<code>{sum(1 for r in rows if r['registered'])}</code>"
        if command == "/allowuser":
            target = parse_user_id(args)
            if target is None:
                return f"用法：<code>{command} USER_ID</code>"
            await asyncio.to_thread(self.service.set_manual_allowed, target, True)
            return f"已授权用户 <code>{target}</code>。"
        if command == "/revokeuser":
            target = parse_user_id(args)
            if target is None:
                return "用法：<code>/revokeuser USER_ID</code>"
            count = await asyncio.to_thread(self.service.revoke_user, target)
            return f"已吊销用户 <code>{target}</code> 的 API Key <code>{count}</code> 个，并移除手动授权。"
        if command == "/checkuser":
            target = parse_user_id(args)
            if target is None:
                return "用法：<code>/checkuser USER_ID</code>"
            cached = await asyncio.to_thread(self.service.user_is_eligible_cached, target)
            return f"用户 <code>{target}</code> cached_legal=<code>{cached}</code>"
        if command == "/billconfig":
            parts = args.split()
            if not parts:
                snapshot = await asyncio.to_thread(self.service.admin_snapshot)
                rules = "\n".join(
                    f"<code>{rule['id']}</code> {esc(rule['name'])}"
                    for rule in snapshot.get("gradients", []) if rule.get("active")
                ) or "暂无可用规则"
                return (
                    "<b>创建计费周期</b>\n\n"
                    "用法：<code>/billconfig NAME START END FIXED [RULE_ID]</code>\n"
                    "示例：<code>/billconfig 2026-08 2026-08-01T00:00 2026-09-01T00:00 1090 1</code>\n\n"
                    "可用梯度规则：\n" + rules
                )
            if len(parts) not in {4, 5}:
                return "参数数量不正确。发送 <code>/billconfig</code> 查看示例和可用规则。"
            name, start, end, fixed = parts[:4]
            fixed_cents = parse_fixed_cost_cents(fixed)
            rule_id = parse_user_id(parts[4]) if len(parts) == 5 else None
            if fixed_cents is None or (len(parts) == 5 and rule_id is None):
                return "固定成本必须是非负金额，规则 ID 必须是整数。"
            await asyncio.to_thread(
                self.service.create_cycle,
                name,
                start,
                end,
                fixed_cents,
                None,
                rule_id,
            )
            return f"计费周期 <code>{esc(name)}</code> 已创建。"
        if command == "/billcycles":
            cycles = await asyncio.to_thread(self.service.list_cycles)
            return "<b>计费周期配置</b>\n\n" + "\n".join(f"<b>{esc(c['name'])}</b> status=<code>{esc(c['status'])}</code>" for c in cycles)
        if command == "/billcycle":
            parts = args.split()
            if len(parts) != 3: return "用法：<code>/billcycle NAME START END</code>"
            await asyncio.to_thread(self.service.update_cycle_time, *parts)
            return f"计费周期 <code>{esc(parts[0])}</code> 时间已更新。"
        if command == "/namekey":
            parts = args.split(maxsplit=1)
            if len(parts) != 2: return "用法：<code>/namekey API_KEY_OR_HASH NAME</code>"
            await asyncio.to_thread(self.service.name_unowned_key, parts[0], parts[1])
            return f"已命名未绑定 API Key：<b>{esc(parts[1])}</b>"
        if command == "/allowchat":
            parts = args.split(maxsplit=1)
            if not parts: return "用法：<code>/allowchat CHAT_ID [NOTE]</code>"
            chat_id = parse_user_id(parts[0])
            if chat_id is None:
                return "用法：<code>/allowchat CHAT_ID [NOTE]</code>"
            await asyncio.to_thread(self.service.set_allowed_chat, chat_id, parts[1] if len(parts) > 1 else "manual")
            return f"已添加可查询 chat_id：<code>{parts[0]}</code>"
        if command == "/delchat":
            chat_id = parse_user_id(args)
            if chat_id is None: return "用法：<code>/delchat CHAT_ID</code>"
            await asyncio.to_thread(self.service.remove_allowed_chat, chat_id)
            return f"已删除可查询 chat_id：<code>{chat_id}</code>"
        if command == "/listchats":
            chats = await asyncio.to_thread(self.service.list_allowed_chats)
            return "<b>可查询 chat_id</b>\n" + "\n".join(f"<code>{c['chat_id']}</code> {esc(c['note'] or '-')}" for c in chats)
        return "未知命令。发送 /help 查看可用命令。"

    def help(self, is_admin: bool) -> str:
        lines = ["CPA 自助 API Key Bot", "", "用户命令：", "/register - 首次注册或新增 API Key", "/mykey - 查看已绑定的掩码 Key",
                 "/resetkey /revoke - 重置或吊销指定 Key", "/confirm - 确认待处理操作", "/cancel - 取消当前操作",
                 "/usage /models /ranking /chart /billing /sub2billing /accounts", "/id /help", "", f"Web：<code>{esc(self.settings.public_base_url)}</code>"]
        if is_admin:
            lines += ["", "管理员命令：", "/billconfig /billcycle /billcycles", "/stats /users /allowuser /revokeuser /checkuser", "/namekey /allowchat /delchat /listchats"]
        return "\n".join(lines)

    @staticmethod
    def _select_active_sub2_cycle(
        cycles: dict[str, Any], now: datetime, zone: ZoneInfo,
    ) -> tuple[datetime, datetime, str, dict[str, Any]] | None:
        active: list[tuple[datetime, datetime, str, dict[str, Any]]] = []
        for name, cycle in cycles.items():
            start, end = datetime.fromisoformat(cycle["start_at"]), datetime.fromisoformat(cycle["end_at"])
            if start.tzinfo is None:
                start = start.replace(tzinfo=zone)
            if end.tzinfo is None:
                end = end.replace(tzinfo=zone)
            if start <= now < end:
                active.append((start, end, name, cycle))
        return max(active, key=lambda item: (item[0], item[2])) if active else None

    def sub2billing(self) -> str:
        if not self.settings.sub2_state_file.exists():
            return "<b>Sub2API 当前计费周期费用</b>\n\n找不到 Sub2API 历史周期文件。"
        data = json.loads(self.settings.sub2_state_file.read_text())
        cycles = data.get("billing_cycles", {})
        zone, now = ZoneInfo(self.settings.timezone), datetime.now(ZoneInfo(self.settings.timezone))
        selected = self._select_active_sub2_cycle(cycles, now, zone)
        if selected is None:
            return "<b>Sub2API 当前计费周期费用</b>\n\n当前没有活跃 Sub2API 计费周期。"
        start, end, name, cycle = selected
        if not self.settings.sub2_postgres_dsn:
            raise BillingError("SUB2API_POSTGRES_DSN is not configured")
        with psycopg.connect(self.settings.sub2_postgres_dsn) as connection:
            rows = connection.execute("""select u.id,coalesce(nullif(u.username,''),nullif(u.email,''),'user-'||u.id::text),
                coalesce(sum(l.actual_cost),0) from usage_logs l join users u on u.id=l.user_id
                where l.group_id=%s and l.created_at >= %s and l.created_at < %s group by u.id,u.username,u.email
                having coalesce(sum(l.actual_cost),0) <> 0 order by 3 desc""",
                (int(cycle["group_id"]), start, end)).fetchall()
        tiers = parse_tiers(cycle.get("tiers") or DEFAULT_TIERS)
        actual = {int(row[0]): int((Decimal(str(row[2])) * NANO_USD).to_integral_value()) for row in rows}
        billed = {uid: tiered_weight(weight, tiers) for uid, weight in actual.items()}
        fixed_cents = int((Decimal(str(cycle["fixed_cost"])) * 100).to_integral_value())
        allocated = largest_remainder(fixed_cents, billed)
        names = {int(row[0]): str(row[1]) for row in rows}
        lines = ["<b>Sub2API 当前计费周期费用</b>", "", f"周期：<code>{esc(name)}</code>",
                 f"分组：<code>{esc(cycle.get('group_name'))}</code> id=<code>{cycle.get('group_id')}</code>",
                 f"固定成本：<code>{format_cents(fixed_cents)}</code>", "",
                 f"合计 实际=<code>{format_usd_nano(sum(actual.values()))}</code> 计费=<code>{format_usd_nano(sum(billed.values()))}</code> 用户=<code>{len(rows)}</code>", ""]
        for index, uid in enumerate(sorted(actual, key=actual.get, reverse=True), 1):
            lines.append(f"{index}. <b>{esc(names[uid])}</b>\n实际=<code>{format_usd_nano(actual[uid])}</code> 计费=<code>{format_usd_nano(billed[uid])}</code> 预估付费=<code>{format_cents(allocated[uid])}</code>")
        return "\n".join(lines)

    @staticmethod
    def _render_chart(labels: list[str], series: list[dict[str, Any]]) -> Path:
        fig, ax = plt.subplots(figsize=(12, 6), dpi=130)
        handle = tempfile.NamedTemporaryFile(prefix="cpa-chart-", suffix=".png", delete=False); handle.close()
        path = Path(handle.name)
        try:
            for item in series[:12]:
                ax.plot(labels, [item["values"].get(label, 0) for label in labels], label=item["name"], linewidth=2)
            ax.set_title("Recent CPA Usage", loc="left")
            ax.grid(True, color="#e5e7eb")
            ax.tick_params(axis="x", labelsize=7, rotation=25)
            ax.legend(fontsize=8, ncol=3)
            fig.tight_layout()
            fig.savefig(path)
            return path
        except Exception:
            path.unlink(missing_ok=True)
            raise
        finally:
            plt.close(fig)

    async def send_chart(self, chat_id: int) -> None:
        labels, series = await asyncio.to_thread(self.service.hourly_usage, 24)
        if not series:
            await self.tg.send(chat_id, "最近 24 小时没有可绘制的用量。")
            return
        path = await asyncio.to_thread(self._render_chart, labels, series)
        try:
            await self.tg.photo(chat_id, path, "最近 24 小时 CPA 使用（按 Telegram 用户聚合）")
        finally:
            path.unlink(missing_ok=True)

    async def handle(self, update: dict[str, Any]) -> None:
        if update.get("chat_member"):
            try:
                await self.handle_chat_member(update)
            except Exception:
                LOG.exception("membership update failed")
            return

        message = update.get("message")
        if not message:
            return
        try:
            reply = await self.dispatch(message)
        except BillingError as exc:
            LOG.warning("command rejected: %s", exc)
            reply = f"执行失败：<code>{esc(exc)}</code>"
        except Exception:
            LOG.exception("command failed")
            reply = "执行失败，请稍后重试。"
        if not reply:
            return
        try:
            await self.tg.send(int((message.get("chat") or {}).get("id")), reply)
        except Exception:
            LOG.exception("reply failed")

    async def configure_commands(self) -> None:
        common = [
            {"command": "start", "description": "打开帮助"},
            {"command": "help", "description": "查看帮助"},
            {"command": "usage", "description": "查看 CPA 全局用量"},
            {"command": "models", "description": "查看模型用量"},
            {"command": "ranking", "description": "查看全局排行"},
            {"command": "chart", "description": "查看最近用量图"},
            {"command": "billing", "description": "查看当前计费周期"},
            {"command": "sub2billing", "description": "查看 Sub2API 当前周期"},
            {"command": "accounts", "description": "查看账号用量"},
            {"command": "id", "description": "显示 Telegram ID"},
        ]
        private = common + [
            {"command": "register", "description": "注册或新增 CPA API Key"},
            {"command": "mykey", "description": "查看已绑定 Key"},
            {"command": "resetkey", "description": "重置指定 API Key"},
            {"command": "revoke", "description": "吊销指定 API Key"},
            {"command": "confirm", "description": "确认待处理操作"},
            {"command": "cancel", "description": "取消当前操作"},
        ]
        admin = private + [
            {"command": "billconfig", "description": "创建计费周期"},
            {"command": "billcycle", "description": "修改计费周期时间"},
            {"command": "billcycles", "description": "查看计费周期配置"},
            {"command": "stats", "description": "查看用户统计"},
            {"command": "users", "description": "查看用户列表"},
            {"command": "allowuser", "description": "手动授权用户"},
            {"command": "revokeuser", "description": "吊销用户 Key"},
            {"command": "checkuser", "description": "检查用户资格"},
            {"command": "namekey", "description": "命名未绑定 Key"},
            {"command": "allowchat", "description": "添加可查询群组"},
            {"command": "delchat", "description": "删除可查询群组"},
            {"command": "listchats", "description": "查看可查询群组"},
        ]
        await self.tg.call("setMyCommands", {"commands": common, "scope": {"type": "default"}})
        await self.tg.call("setMyCommands", {"commands": private, "scope": {"type": "all_private_chats"}})
        for user_id in sorted(self.settings.admin_user_ids):
            await self.tg.call("setMyCommands", {
                "commands": admin,
                "scope": {"type": "chat", "chat_id": user_id},
            })

    async def run(self) -> None:
        me = await self.tg.me()
        self.bot_username = str(me.get("username") or "").strip()
        if not self.bot_username:
            raise BillingError("Telegram getMe 未返回 bot username")
        await self.configure_commands()
        offset = None
        tasks: set[asyncio.Task[None]] = set()
        try:
            while True:
                try:
                    updates = await self.tg.call("getUpdates", {"offset": offset, "timeout": 30, "allowed_updates": ["message", "chat_member"]}) or []
                    for update in updates:
                        offset = int(update["update_id"]) + 1
                        task = asyncio.create_task(self.handle(update))
                        tasks.add(task)
                        task.add_done_callback(tasks.discard)
                except Exception:
                    LOG.exception("polling failed")
                    await asyncio.sleep(3)
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await self.tg.client.aclose()


async def run_bot() -> None:
    settings = Settings.from_env()
    db = Database(settings.database_path)
    service = BillingService(settings, db)
    service.bootstrap()
    await BillingBot(settings, service).run()
