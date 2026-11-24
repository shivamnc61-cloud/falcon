import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from reference_automation import ReferenceAutomation, _interval_seconds


def test_interval_seconds_parses_and_enforces_bounds() -> None:
    assert _interval_seconds("2h") == 7200
    with pytest.raises(ValueError):
        _interval_seconds("30s")
    with pytest.raises(ValueError):
        _interval_seconds("permanent")


def test_autoreact_respects_channel_scope_and_casefolds_trigger() -> None:
    async def verify() -> None:
        db = await aiosqlite.connect(":memory:")
        await db.execute(
            "CREATE TABLE reference_autoreacts ("
            "rule_id INTEGER PRIMARY KEY, guild_id INTEGER, channel_id INTEGER, "
            "trigger TEXT, emoji TEXT)"
        )
        await db.executemany(
            "INSERT INTO reference_autoreacts (guild_id, channel_id, trigger, emoji) "
            "VALUES (?, ?, ?, ?)",
            [(7, None, "hello", "🌸"), (7, 99, "hello", "⭐")],
        )
        bot = SimpleNamespace(db=db)
        cog = ReferenceAutomation(bot)
        message = SimpleNamespace(
            guild=SimpleNamespace(id=7),
            author=SimpleNamespace(bot=False),
            content="Well, HELLO there",
            channel=SimpleNamespace(id=42),
            add_reaction=AsyncMock(),
        )

        try:
            await cog.on_message(message)
            message.add_reaction.assert_awaited_once_with("🌸")
        finally:
            await db.close()

    asyncio.run(verify())


def test_vanity_role_rejects_blank_keyword() -> None:
    async def verify() -> None:
        cog = ReferenceAutomation(SimpleNamespace())
        ctx = SimpleNamespace(send=AsyncMock())
        await cog.vanityroles_add.callback(cog, ctx, "   ", SimpleNamespace())
        ctx.send.assert_awaited_once_with("Vanity-role keywords cannot be empty.")

    asyncio.run(verify())