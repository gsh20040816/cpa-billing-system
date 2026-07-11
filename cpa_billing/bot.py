from __future__ import annotations

import asyncio
import html
import json
import logging
import tempfile
from datetime import datetime
from decimal import Decimal
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
        for offset in range(0, max(1, len(text)), 3900):
            await self.call("sendMessage", {"chat_id": chat_id, "text": text[offset:offset + 3900], "parse_mode": "HTML", "disable_web_page_preview": True})

    async def member(self, chat_id: int, user_id: int) -> dict[str, Any]:
        return dict(await self.call("getChatMember", {"chat_id": chat_id, "user_id": user_id}) or {})

    async def photo(self, chat_id: int, path: Path, caption: str) -> None:
        with path.open("rb") as image:
            response = await self.client.post(f"{self.url}/sendPhoto", data={"chat_id": str(chat_id), "caption": caption}, files={"photo": image})
        response.raise_for_status()


class BillingBot:
    def __init__(self, settings: Settings, service: BillingService) -> None:
        if not settings.telegram_token:
            raise RuntimeError("TG_BOT_TOKEN is required")
        self.settings, self.service = settings, service
        self.tg = TelegramAPI(settings.telegram_token)

    async def eligible(self, user: dict[str, Any]) -> bool:
        user_id = int(user["id"])
        if user_id in self.settings.admin_user_ids or self.service.user_is_eligible_cached(user_id):
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

    async def dispatch(self, message: dict[str, Any]) -> str:
        chat, user = message.get("chat") or {}, message.get("from") or {}
        if not user or user.get("is_bot"):
            return ""
        await asyncio.to_thread(self.service.upsert_user, user)
        text = str(message.get("text") or "").strip()
        command, _, args = text.partition(" ")
        command = command.split("@", 1)[0].lower()
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
            if not (self.service.chat_is_allowed(int(chat.get("id", 0))) or await self.eligible(user)):
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
            if not args.strip().split():
                return f"用法：<code>{command} USER_ID</code>"
            target = int(args.strip().split()[0])
            await asyncio.to_thread(self.service.set_manual_allowed, target, True)
            return f"已授权用户 <code>{target}</code>。"
        if command == "/revokeuser":
            if not args.strip().split():
                return "用法：<code>/revokeuser USER_ID</code>"
            target = int(args.strip().split()[0])
            count = await asyncio.to_thread(self.service.revoke_user, target)
            return f"已吊销用户 <code>{target}</code> 的 API Key <code>{count}</code> 个，并移除手动授权。"
        if command == "/checkuser":
            target = int(args.strip().split()[0])
            return f"用户 <code>{target}</code> cached_legal=<code>{self.service.user_is_eligible_cached(target)}</code>"
        if command == "/billconfig":
            parts = args.split()
            if len(parts) != 5:
                return "用法：<code>/billconfig NAME START END FIXED TIERS</code>；Web 管理页将提供完整向导。"
            name, start, end, fixed, _ = parts
            await asyncio.to_thread(self.service.create_cycle, name, start, end, int(Decimal(fixed) * 100), None)
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
            await asyncio.to_thread(self.service.set_allowed_chat, int(parts[0]), parts[1] if len(parts) > 1 else "manual")
            return f"已添加可查询 chat_id：<code>{parts[0]}</code>"
        if command == "/delchat":
            if not args.strip(): return "用法：<code>/delchat CHAT_ID</code>"
            await asyncio.to_thread(self.service.remove_allowed_chat, int(args.split()[0]))
            return f"已删除可查询 chat_id：<code>{args.split()[0]}</code>"
        if command == "/listchats":
            chats = await asyncio.to_thread(self.service.list_allowed_chats)
            return "<b>可查询 chat_id</b>\n" + "\n".join(f"<code>{c['chat_id']}</code> {esc(c['note'] or '-')}" for c in chats)
        return "未知命令。发送 /help 查看可用命令。"

    def help(self, is_admin: bool) -> str:
        lines = ["CPA 自助 API Key Bot", "", "用户命令：", "/register - 首次注册或新增 API Key", "/mykey - 查看已绑定的掩码 Key",
                 "/confirm TOKEN - 确认 Web Key 操作", "/usage /models /ranking /chart /billing /sub2billing /accounts", "/id /help", "", f"Web：<code>{esc(self.settings.public_base_url)}</code>"]
        if is_admin:
            lines += ["", "管理员命令：", "/billconfig /billcycle /billcycles", "/stats /users /allowuser /revokeuser /checkuser", "/namekey /allowchat /delchat /listchats"]
        return "\n".join(lines)

    def sub2billing(self) -> str:
        if not self.settings.sub2_state_file.exists():
            return "<b>Sub2API 当前计费周期费用</b>\n\n找不到 Sub2API 历史周期文件。"
        data = json.loads(self.settings.sub2_state_file.read_text())
        cycles = data.get("billing_cycles", {})
        zone, now = ZoneInfo(self.settings.timezone), datetime.now(ZoneInfo(self.settings.timezone))
        active = []
        for name, cycle in cycles.items():
            start, end = datetime.fromisoformat(cycle["start_at"]), datetime.fromisoformat(cycle["end_at"])
            if start.tzinfo is None: start = start.replace(tzinfo=zone)
            if end.tzinfo is None: end = end.replace(tzinfo=zone)
            if start <= now < end: active.append((start, name, cycle))
        if not active:
            return "<b>Sub2API 当前计费周期费用</b>\n\n当前没有活跃 Sub2API 计费周期。"
        _, name, cycle = sorted(active, reverse=True)[0]
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

    async def send_chart(self, chat_id: int) -> None:
        labels, series = await asyncio.to_thread(self.service.hourly_usage, 24)
        if not series:
            await self.tg.send(chat_id, "最近 24 小时没有可绘制的用量。")
            return
        fig, ax = plt.subplots(figsize=(12, 6), dpi=130)
        for item in series[:12]:
            ax.plot(labels, [item["values"].get(label, 0) for label in labels], label=item["name"], linewidth=2)
        ax.set_title("Recent CPA Usage", loc="left")
        ax.grid(True, color="#e5e7eb")
        ax.tick_params(axis="x", labelsize=7, rotation=25)
        ax.legend(fontsize=8, ncol=3)
        fig.tight_layout()
        handle = tempfile.NamedTemporaryFile(prefix="cpa-chart-", suffix=".png", delete=False); handle.close()
        path = Path(handle.name)
        try:
            fig.savefig(path)
            await self.tg.photo(chat_id, path, "最近 24 小时 CPA 使用（按 Telegram 用户聚合）")
        finally:
            plt.close(fig); path.unlink(missing_ok=True)

    async def handle(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not message:
            return
        try:
            reply = await self.dispatch(message)
        except Exception as exc:
            LOG.exception("command failed")
            reply = f"执行失败：<code>{esc(exc)}</code>"
        if reply:
            await self.tg.send(int((message.get("chat") or {}).get("id")), reply)

    async def run(self) -> None:
        commands = [{"command": cmd, "description": desc} for cmd, desc in [
            ("usage", "查看 CPA 全局用量"), ("models", "查看模型用量"), ("ranking", "查看全局排行"), ("chart", "查看最近排行"),
            ("billing", "查看当前计费周期"), ("sub2billing", "查看 Sub2API 当前周期"), ("accounts", "查看账号用量"),
            ("register", "注册或新增 CPA API Key"), ("mykey", "查看已绑定 Key"), ("confirm", "确认 Web Key 操作"), ("id", "显示 Telegram id"), ("help", "查看帮助")]]
        await self.tg.call("setMyCommands", {"commands": commands})
        offset = None
        while True:
            try:
                updates = await self.tg.call("getUpdates", {"offset": offset, "timeout": 30, "allowed_updates": ["message", "chat_member"]}) or []
                tasks = []
                for update in updates:
                    offset = int(update["update_id"]) + 1
                    tasks.append(asyncio.create_task(self.handle(update)))
                if tasks:
                    await asyncio.gather(*tasks)
            except Exception:
                LOG.exception("polling failed")
                await asyncio.sleep(3)


async def run_bot() -> None:
    settings = Settings.from_env()
    db = Database(settings.database_path)
    service = BillingService(settings, db)
    service.bootstrap()
    await BillingBot(settings, service).run()
