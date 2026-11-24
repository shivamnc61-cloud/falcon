import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import aiosqlite

from reference_utility_features import (
    ReferenceUtilityFeatures,
    _convert_text,
    calculate,
)


def test_calculate_supports_arithmetic_and_rejects_code() -> None:
    assert calculate("2 + 3 * (4 - 1)") == 11
    with pytest.raises(ValueError):
        calculate("__import__('os').system('true')")
    with pytest.raises(ValueError):
        calculate("(-1) ** 0.5")
    with pytest.raises(ValueError):
        calculate("1e309")


def test_text_encoding_round_trips() -> None:
    text = "Falcon Sakura 42"
    for mode in ("base32", "hex", "binary"):
        assert _convert_text(mode, _convert_text(mode, text), decode=True) == text
    assert _convert_text("rot13", _convert_text("rot13", text)) == text


def test_length_conversion_formats_result() -> None:
    async def verify() -> None:
        cog = ReferenceUtilityFeatures(None)
        ctx = SimpleNamespace(send=AsyncMock())
        await cog.convert_length.callback(cog, ctx, 1, "mi", "km")
        assert "1 mi` = `1.609344 km" in ctx.send.await_args.args[0]

    asyncio.run(verify())


def test_profile_bio_persists_and_renders() -> None:
    async def verify() -> None:
        db = await aiosqlite.connect(":memory:")
        cog = ReferenceUtilityFeatures(SimpleNamespace(db=db))
        await cog.cog_load()
        user = SimpleNamespace(
            id=5,
            display_name="Falcon User",
            display_avatar=SimpleNamespace(url="https://example.test/avatar.png"),
        )
        ctx = SimpleNamespace(author=user, send=AsyncMock())
        try:
            await cog.profileedit_bio.callback(cog, ctx, bio="Builder and community helper.")
            await cog.profilecard.callback(cog, ctx, user)
            embed = ctx.send.await_args.kwargs["embed"]
            assert embed.description == "Builder and community helper."
            assert embed.thumbnail.url == user.display_avatar.url
        finally:
            await db.close()

    asyncio.run(verify())


def test_profile_url_commands_reject_malformed_https_urls() -> None:
    async def verify() -> None:
        cog = ReferenceUtilityFeatures(SimpleNamespace())
        ctx = SimpleNamespace(author=SimpleNamespace(id=5), send=AsyncMock())

        await cog.profileedit_background.callback(cog, ctx, "https://[")
        ctx.send.assert_awaited_once_with("Use a valid HTTPS image URL.")

        ctx.send.reset_mock()
        await cog.profileedit_social.callback(cog, ctx, "example", "https://[")
        ctx.send.assert_awaited_once_with("Use a valid HTTPS profile URL.")

    asyncio.run(verify())


def test_todo_list_splits_output_under_discord_message_limit() -> None:
    async def verify() -> None:
        db = await aiosqlite.connect(":memory:")
        cog = ReferenceUtilityFeatures(SimpleNamespace(db=db))
        await cog.cog_load()
        await db.executemany(
            "INSERT INTO reference_todos (user_id, guild_id, task, created_at) VALUES (?, ?, ?, ?)",
            [(5, 0, f"task-{index}-" + ("x" * 480), "now") for index in range(10)],
        )
        ctx = SimpleNamespace(
            author=SimpleNamespace(id=5),
            guild=None,
            send=AsyncMock(),
        )
        try:
            await cog.todo_list.callback(cog, ctx)
            messages = [call.args[0] for call in ctx.send.await_args_list]
            assert len(messages) > 1
            assert all(len(message) <= 2000 for message in messages)
            assert sum(message.count("task-") for message in messages) == 10
        finally:
            await db.close()

    asyncio.run(verify())


def test_export_bans_collects_discord_async_iterator() -> None:
    async def verify() -> None:
        class FakeUser:
            id = 42

            def __str__(self):
                return "banned-user"

        entry = SimpleNamespace(
            user=FakeUser(),
            reason="spam",
        )

        async def bans():
            yield entry

        cog = ReferenceUtilityFeatures(None)
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1, bans=bans),
            send=AsyncMock(),
        )

        await cog.export_data.callback(cog, ctx, "bans")

        csv_file = ctx.send.await_args.kwargs["file"]
        output = csv_file.fp.getvalue().decode("utf-8-sig")
        assert "user_id,username,reason" in output
        assert "42,banned-user,spam" in output
        csv_file.close()

    asyncio.run(verify())


def test_globalboard_shows_only_current_server_message_totals() -> None:
    async def verify() -> None:
        db = await aiosqlite.connect(":memory:")
        await db.execute(
            "CREATE TABLE users (guild_id INTEGER, user_id INTEGER, total_msgs INTEGER, "
            "joins INTEGER, left INTEGER, bonus INTEGER)"
        )
        await db.executemany(
            "INSERT INTO users VALUES (?, ?, ?, ?, ?, ?)",
            [(31, 10, 40, 0, 0, 0), (31, 11, 90, 0, 0, 0), (32, 12, 150, 0, 0, 0)],
        )
        await db.commit()
        bot = SimpleNamespace(db=db, get_user=lambda _user_id: None)
        cog = ReferenceUtilityFeatures(bot)
        ctx = SimpleNamespace(guild=SimpleNamespace(id=31), send=AsyncMock())
        try:
            await cog.globalboard.callback(cog, ctx, "messages")
            embed = ctx.send.await_args.kwargs["embed"]
            assert embed.title == "Server Messages Leaderboard"
            assert embed.description.index("<@11>") < embed.description.index("<@10>")
            assert "<@12>" not in embed.description
        finally:
            await db.close()

    asyncio.run(verify())