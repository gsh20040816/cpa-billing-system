from __future__ import annotations

import asyncio

from cpa_billing.bot import BillingBot


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
