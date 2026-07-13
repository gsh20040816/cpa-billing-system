from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from cpa_billing.bot import BillingBot, html_chunks, parse_fixed_cost_cents, telegram_length


def test_group_messages_without_command_prefix_are_ignored(settings, service, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(service, "upsert_user", lambda user: calls.append(user))

    async def scenario() -> str:
        bot = BillingBot(settings, service)
        try:
            return await bot.dispatch({
                "chat": {"id": -100, "type": "supergroup"},
                "from": {"id": 2, "username": "member", "is_bot": False},
                "text": "普通群聊消息",
            })
        finally:
            await bot.tg.client.aclose()

    assert asyncio.run(scenario()) == ""
    assert calls == []


def test_commands_for_other_bots_are_ignored(settings, service, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(service, "upsert_user", lambda user: calls.append(user))

    async def scenario() -> str:
        bot = BillingBot(settings, service)
        bot.bot_username = "cpa_bot"
        try:
            return await bot.dispatch({
                "chat": {"id": 2, "type": "private"},
                "from": {"id": 2, "username": "member", "is_bot": False},
                "text": "/help@other_bot",
            })
        finally:
            await bot.tg.client.aclose()

    assert asyncio.run(scenario()) == ""
    assert calls == []


def test_command_menu_uses_private_and_admin_scopes(settings, service, monkeypatch) -> None:
    calls = []

    async def fake_call(method, payload=None):
        calls.append((method, payload))
        return None

    async def scenario() -> None:
        bot = BillingBot(settings, service)
        monkeypatch.setattr(bot.tg, "call", fake_call)
        try:
            await bot.configure_commands()
        finally:
            await bot.tg.client.aclose()

    asyncio.run(scenario())
    assert [payload["scope"]["type"] for _, payload in calls] == ["default", "all_private_chats", "chat"]
    default_commands = {item["command"] for item in calls[0][1]["commands"]}
    private_commands = {item["command"] for item in calls[1][1]["commands"]}
    admin_commands = {item["command"] for item in calls[2][1]["commands"]}
    assert "register" not in default_commands
    assert {"register", "resetkey", "revoke", "confirm"} <= private_commands
    assert {"billconfig", "users", "listchats"} <= admin_commands


def test_chat_member_updates_refresh_membership_cache(settings, service, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(service, "set_membership", lambda user, group_id, status, legal: calls.append((user, group_id, status, legal)))

    async def scenario() -> None:
        bot = BillingBot(settings, service)
        try:
            await bot.handle({
                "chat_member": {
                    "chat": {"id": -100, "type": "supergroup"},
                    "new_chat_member": {
                        "user": {"id": 2, "username": "member", "is_bot": False},
                        "status": "member",
                    },
                },
            })
        finally:
            await bot.tg.client.aclose()

    asyncio.run(scenario())
    assert calls == [({"id": 2, "username": "member", "is_bot": False}, -100, "member", True)]


def test_html_chunks_keep_each_chunk_valid_and_below_limit() -> None:
    chunks = html_chunks("<b>标题</b>\n<code>" + ("x" * 5000) + "</code>", limit=100)

    assert len(chunks) > 1
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert all(chunk.count("<code>") == chunk.count("</code>") for chunk in chunks)

    emoji_chunks = html_chunks("😀" * 300, limit=100)
    assert all(telegram_length(chunk) <= 100 for chunk in emoji_chunks)


def test_fixed_cost_parser_rounds_cents_and_rejects_invalid_values() -> None:
    assert parse_fixed_cost_cents("1.995") == 200
    assert parse_fixed_cost_cents("NaN") is None
    assert parse_fixed_cost_cents("-1") is None


def test_select_active_sub2_cycle_returns_selected_dates() -> None:
    now = datetime(2026, 7, 13, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    cycles = {
        "older": {"start_at": "2026-07-01T00:00:00", "end_at": "2026-08-01T00:00:00"},
        "newer": {"start_at": "2026-07-10T00:00:00", "end_at": "2026-07-20T00:00:00"},
    }

    selected = BillingBot._select_active_sub2_cycle(cycles, now, ZoneInfo("Asia/Shanghai"))

    assert selected is not None
    start, end, name, cycle = selected
    assert name == "newer"
    assert cycle is cycles["newer"]
    assert start.isoformat() == "2026-07-10T00:00:00+08:00"
    assert end.isoformat() == "2026-07-20T00:00:00+08:00"
