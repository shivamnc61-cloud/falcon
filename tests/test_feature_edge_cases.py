import asyncio
import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiosqlite
import discord

from community_features import CommunityFeatures
from denycord_interaction_features import DenycordInteractionFeatures
from denycord_server_automation import DenycordServerAutomation
from denycord_voice_backup import DenycordVoiceBackup
from leveling_features import LevelingFeatures
from premium_features import PremiumFeatures
from zyre_features import ZyreFeatures


def test_community_event_without_destination_falls_back_to_saved_greeting() -> None:
    async def verify() -> None:
        db = await aiosqlite.connect(":memory:")
        await db.executescript(
            "CREATE TABLE community_event_replies (guild_id INTEGER, event TEXT, response TEXT, "
            "channel_id INTEGER, updated_by INTEGER, updated_at TEXT, PRIMARY KEY(guild_id, event));"
            "INSERT INTO community_event_replies VALUES (1, 'join', 'Event greeting', 99, 7, 'now');"
            "CREATE TABLE greeting_settings (guild_id INTEGER PRIMARY KEY, channel_id INTEGER, "
            "join_config TEXT, leave_config TEXT, join_enabled INTEGER, leave_enabled INTEGER, "
            "join_card_enabled INTEGER, leave_card_enabled INTEGER, "
            "join_card_channel_id INTEGER, leave_card_channel_id INTEGER);"
        )
        guild = SimpleNamespace(id=1, get_channel=lambda _channel_id: None)
        member = SimpleNamespace(id=10, guild=guild)
        cog = CommunityFeatures(SimpleNamespace(db=db, user=None))
        cog._send_saved_text_greeting = AsyncMock(return_value=True)
        try:
            assert await cog.send_configured_greeting(member, "join") is True
            cog._send_saved_text_greeting.assert_awaited_once_with(member, "join", None)
        finally:
            await db.close()

    asyncio.run(verify())


def test_leveling_interaction_helpers_keep_requested_responses_ephemeral() -> None:
    async def verify() -> None:
        response = SimpleNamespace(
            is_done=Mock(return_value=False),
            defer=AsyncMock(),
            send_message=AsyncMock(),
        )
        interaction = SimpleNamespace(
            response=response,
            followup=SimpleNamespace(send=AsyncMock()),
        )
        ctx = SimpleNamespace(interaction=interaction, send=AsyncMock())

        await LevelingFeatures._defer(ctx, ephemeral=True)
        response.defer.assert_awaited_once_with(ephemeral=True)

        await LevelingFeatures._reply(ctx, "Private response", ephemeral=True)
        response.send_message.assert_awaited_once()
        assert response.send_message.await_args.kwargs["ephemeral"] is True

        response.is_done.return_value = True
        await LevelingFeatures._reply(ctx, "Private followup", ephemeral=True)
        interaction.followup.send.assert_awaited_once()
        assert interaction.followup.send.await_args.kwargs["ephemeral"] is True

    asyncio.run(verify())


def test_failed_reminder_delivery_keeps_reminder_for_retry(monkeypatch) -> None:
    import zyre_features

    async def verify() -> None:
        db = await aiosqlite.connect(":memory:")
        await db.execute(
            "CREATE TABLE zyre_reminders (reminder_id INTEGER PRIMARY KEY, guild_id INTEGER, "
            "user_id INTEGER, channel_id INTEGER, reminder TEXT, due_at TEXT)"
        )
        await db.execute(
            "INSERT INTO zyre_reminders VALUES (1, 2, 3, 4, 'test', ?)",
            ((datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1)).isoformat(),),
        )
        await db.commit()
        channel = SimpleNamespace(id=4)
        guild = SimpleNamespace(get_channel=lambda _channel_id: channel)
        cog = ZyreFeatures(SimpleNamespace(db=db, get_guild=lambda _guild_id: guild))
        response = SimpleNamespace(status=500, reason="test failure")
        delivery_error = discord.HTTPException(response, "send failed")
        monkeypatch.setattr(
            zyre_features,
            "send_v2",
            AsyncMock(side_effect=delivery_error),
        )
        try:
            await cog.reminder_worker.coro(cog)
            async with db.execute("SELECT reminder_id FROM zyre_reminders") as cursor:
                assert await cursor.fetchone() == (1,)
        finally:
            await db.close()

    asyncio.run(verify())


def test_premium_guild_history_stays_within_embed_field_limit() -> None:
    async def verify() -> None:
        db = await aiosqlite.connect(":memory:")
        await db.executescript(
            "CREATE TABLE premium_allocations (allocation_id INTEGER PRIMARY KEY, user_id INTEGER, "
            "expires_at TEXT, guild_limit INTEGER, status TEXT);"
            "CREATE TABLE premium_activations (allocation_id INTEGER, guild_id INTEGER, owner_id INTEGER, "
            "activated_at TEXT, status TEXT);"
            "INSERT INTO premium_allocations VALUES (1, 5, NULL, 20, 'active');"
        )
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        await db.executemany(
            "INSERT INTO premium_activations VALUES (1, ?, 5, ?, 'active')",
            [(guild_id, now) for guild_id in range(20)],
        )
        await db.commit()
        bot = SimpleNamespace(
            db=db,
            get_guild=lambda _guild_id: SimpleNamespace(name="G" * 100),
        )
        cog = PremiumFeatures(bot)
        ctx = SimpleNamespace(author=SimpleNamespace(id=5), interaction=None, send=AsyncMock())
        try:
            await cog.premium_view.callback(cog, ctx)
            history = ctx.send.await_args.kwargs["embed"].fields[-1].value
            assert len(history) <= 1024
            assert "More history not shown" in history
        finally:
            await db.close()

    asyncio.run(verify())


def test_disabled_room_claim_is_rejected_before_authorization(monkeypatch) -> None:
    import denycord_voice_backup

    async def verify() -> None:
        cog = DenycordVoiceBackup(SimpleNamespace())
        cog._authorize_room_action = AsyncMock()
        monkeypatch.setattr(
            denycord_voice_backup,
            "TEMPVOICE_CONFIG",
            {"allowed_controls": []},
        )

        result = await cog._perform_room_action(None, None, "claim")

        assert "disabled" in result
        cog._authorize_room_action.assert_not_awaited()

    asyncio.run(verify())


def test_clear_room_overrides_removes_permissions_for_uncached_users() -> None:
    async def verify() -> None:
        db = await aiosqlite.connect(":memory:")
        await db.execute(
            "CREATE TABLE denycord_voice_overrides (guild_id INTEGER, channel_id INTEGER, user_id INTEGER, "
            "allow_connect INTEGER, allow_chat INTEGER, PRIMARY KEY(guild_id, channel_id, user_id))"
        )
        await db.execute(
            "INSERT INTO denycord_voice_overrides VALUES (1, 2, 33, 0, 0)"
        )
        await db.commit()
        guild = SimpleNamespace(id=1, get_member=lambda _user_id: None)
        channel = SimpleNamespace(
            id=2,
            guild=guild,
            overwrites_for=Mock(
                return_value=discord.PermissionOverwrite(
                    view_channel=False,
                    connect=False,
                    send_messages=False,
                )
            ),
            set_permissions=AsyncMock(),
        )
        member = SimpleNamespace(id=44)
        cog = DenycordVoiceBackup(SimpleNamespace(db=db))
        cog._authorize_room_action = AsyncMock(return_value=(channel, (), None))
        try:
            result = await cog._perform_room_action(guild, member, "clear")
            assert result == "Room access overrides cleared."
            permission_target = channel.set_permissions.await_args.args[0]
            assert permission_target.id == 33
            assert channel.set_permissions.await_args.kwargs["overwrite"] is None
            async with db.execute("SELECT COUNT(*) FROM denycord_voice_overrides") as cursor:
                assert await cursor.fetchone() == (0,)
        finally:
            await db.close()

    asyncio.run(verify())


def test_ticket_open_requests_are_serialized() -> None:
    async def verify() -> None:
        cog = DenycordInteractionFeatures(SimpleNamespace())
        active = 0
        maximum_active = 0

        async def open_locked(_interaction, *, category_id=None):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0)
            active -= 1

        cog._open_ticket_locked = open_locked
        interactions = [
            SimpleNamespace(response=SimpleNamespace(is_done=Mock(return_value=True)))
            for _ in range(2)
        ]
        await asyncio.gather(*(cog.open_ticket(interaction) for interaction in interactions))

        assert maximum_active == 1

    asyncio.run(verify())


def test_ticket_template_add_passes_keyword_only_content() -> None:
    async def verify() -> None:
        db = await aiosqlite.connect(":memory:")
        await db.execute(
            "CREATE TABLE denycord_ticket_templates (guild_id INTEGER, name TEXT, content TEXT, "
            "updated_by INTEGER, updated_at TEXT, PRIMARY KEY(guild_id, name))"
        )
        await db.commit()
        cog = DenycordInteractionFeatures(SimpleNamespace(db=db))
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            author=SimpleNamespace(id=7),
            interaction=None,
            send=AsyncMock(),
        )
        try:
            await cog.ticket_template_add.callback(cog, ctx, "welcome", content="Welcome to the ticket.")
            async with db.execute(
                "SELECT content FROM denycord_ticket_templates WHERE guild_id=1 AND name='welcome'"
            ) as cursor:
                assert await cursor.fetchone() == ("Welcome to the ticket.",)
        finally:
            await db.close()

    asyncio.run(verify())


def test_birthday_announcement_retries_after_send_failure() -> None:
    async def verify() -> None:
        db = await aiosqlite.connect(":memory:")
        await db.execute(
            "CREATE TABLE denycord_birthday_settings (guild_id INTEGER PRIMARY KEY, channel_id INTEGER, "
            "role_id INTEGER, last_announcement_date TEXT)"
        )
        await db.execute(
            "INSERT INTO denycord_birthday_settings VALUES (1, 2, NULL, NULL)"
        )
        await db.commit()
        cog = DenycordServerAutomation(SimpleNamespace(db=db))
        cog._expire_birthday_roles = AsyncMock()
        cog._today_utc = lambda: datetime.date(2026, 11, 12)
        cog._send_birthday_announcement = AsyncMock(return_value=False)
        try:
            await cog.birthday_worker.coro(cog)
            async with db.execute(
                "SELECT last_announcement_date FROM denycord_birthday_settings WHERE guild_id=1"
            ) as cursor:
                assert await cursor.fetchone() == (None,)
            await cog.birthday_worker.coro(cog)
            assert cog._send_birthday_announcement.await_count == 2
        finally:
            await db.close()

    asyncio.run(verify())
