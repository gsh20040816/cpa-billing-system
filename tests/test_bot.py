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


def test_unknown_admin_chat_does_not_prevent_bot_startup(settings, service, monkeypatch):
    import httpx
    import pytest

    async def scenario(description):
        bot = BillingBot(settings, service)
        async def fake_call(method, payload=None):
            if payload["scope"]["type"] == "chat":
                response = httpx.Response(400, json={"description": description}, request=httpx.Request("POST", "https://example.invalid"))
                response.raise_for_status()
        monkeypatch.setattr(bot.tg, "call", fake_call)
        try:
            await bot.configure_commands()
        finally:
            await bot.tg.client.aclose()
    asyncio.run(scenario("Bad Request: chat not found"))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(scenario("Bad Request: unrelated configuration error"))


def test_update_queue_applies_backpressure_and_processes_serially(settings, service, monkeypatch):
    import cpa_billing.bot as bot_module

    async def scenario():
        monkeypatch.setattr(bot_module, "UPDATE_QUEUE_SIZE", 2)
        bot = BillingBot(settings, service)
        started, release, drained = asyncio.Event(), asyncio.Event(), asyncio.Event()
        handled, polling, queues = [], [], []
        active = peak = 0
        real_queue = asyncio.Queue

        class ObservedQueue(real_queue):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.peak = 0
                queues.append(self)

            async def put(self, item):
                await super().put(item)
                self.peak = max(self.peak, self.qsize())

        monkeypatch.setattr(bot_module.asyncio, "Queue", ObservedQueue)

        async def handle(update):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            started.set()
            try:
                await release.wait()
                handled.append(update["update_id"])
                if len(handled) == 6:
                    drained.set()
            finally:
                active -= 1

        async def call(method, payload=None):
            if method == "getMe":
                return {"username": "cpa_bot"}
            if method != "getUpdates":
                return None
            polling.append(payload.copy())
            offset = payload["offset"] or 0
            assert payload["limit"] == 2
            if offset >= 6:
                await asyncio.Event().wait()
            return [{"update_id": offset + i} for i in range(2)]

        monkeypatch.setattr(bot, "handle", handle)
        monkeypatch.setattr(bot.tg, "call", call)
        task = asyncio.create_task(bot.run())
        try:
            await asyncio.wait_for(started.wait(), 2)
            for _ in range(10):
                await asyncio.sleep(0)
            assert peak == 1
            assert queues[0].qsize() == 2
            assert len(polling) == 2  # Polling stopped on a full queue.
            release.set()
            await asyncio.wait_for(drained.wait(), 2)
            assert handled == list(range(6))
            assert peak == 1
            assert queues[0].peak <= 2
            assert [payload["offset"] for payload in polling[:3]] == [None, 2, 4]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert bot.tg.client.is_closed
        assert active == 0

    asyncio.run(scenario())


def test_help_and_id_do_not_write_users_and_membership_checks_run_concurrently(settings, service, monkeypatch) -> None:
    from dataclasses import replace

    upserts, memberships = [], []
    monkeypatch.setattr(service, "upsert_user", lambda user: upserts.append(user))
    monkeypatch.setattr(service, "set_membership", lambda user, group_id, status, legal: memberships.append((group_id, legal)))
    settings = replace(settings, allowed_group_ids=frozenset({-100, -200}))

    async def scenario():
        bot = BillingBot(settings, service)
        in_flight = peak = 0

        async def member(group_id, user_id):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return {"status": "member" if group_id == -200 else "left"}

        monkeypatch.setattr(bot.tg, "member", member)
        try:
            user = {"id": 5, "username": "member", "is_bot": False}
            for text in ("/help", "/id", "/start"):
                await bot.dispatch({"chat": {"id": 5, "type": "private"}, "from": user, "text": text})
            assert upserts == []
            assert await bot.eligible(user) is True
            return peak
        finally:
            await bot.tg.client.aclose()

    assert asyncio.run(scenario()) == 2
    assert sorted(memberships) == [(-200, True), (-100, False)]


def test_deferred_admin_menu_is_configured_on_admin_start(settings, service, monkeypatch) -> None:
    import httpx

    calls = []

    async def scenario():
        bot = BillingBot(settings, service)
        chat_exists = False

        async def fake_call(method, payload=None):
            calls.append((method, payload["scope"]["type"]))
            if payload["scope"]["type"] == "chat" and not chat_exists:
                response = httpx.Response(400, json={"description": "Bad Request: chat not found"},
                                          request=httpx.Request("POST", "https://example.invalid"))
                response.raise_for_status()

        async def send(chat_id, text):
            return None

        monkeypatch.setattr(bot.tg, "call", fake_call)
        monkeypatch.setattr(bot.tg, "send", send)
        message = {"message": {"chat": {"id": 1, "type": "private"}, "from": {"id": 1, "is_bot": False}, "text": "/start"}}
        try:
            await bot.configure_commands()
            assert bot.deferred_admin_menus == {1}
            chat_exists = True
            calls.clear()
            await bot.handle(message)
            assert calls == [("setMyCommands", "chat")]
            assert bot.deferred_admin_menus == set()
            calls.clear()
            await bot.handle(message)
            assert calls == []
        finally:
            await bot.tg.client.aclose()

    asyncio.run(scenario())
