import asyncio
import io
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiosqlite
import discord
from PIL import Image
from discord.ext import commands

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _stub_room_panel_emoji_download(cog, voice_module):
    image_buffer = io.BytesIO()
    Image.new("RGBA", (48, 48), "#F0F2F5").save(image_buffer, format="PNG")
    image_bytes = image_buffer.getvalue()
    cog._load_room_panel_emoji_images = AsyncMock(
        return_value={
            key: image_bytes for key in voice_module.VOICE_BUTTON_EMOJIS
        }
    )


def test_is_bot_owner_returns_boolean(monkeypatch) -> None:
    import main as bot_module

    monkeypatch.setattr(bot_module, "PRIMARY_OWNER_IDS", [123])

    async def verify() -> None:
        assert await bot_module.is_bot_owner(123) is True
        assert await bot_module.is_bot_owner(456) is False

    asyncio.run(verify())


def test_empty_bot_staff_list_is_silent_and_staff_name_is_available(
    monkeypatch,
) -> None:
    import main as bot_module

    assert bot_module.staff_cmd.name == "botstaff"
    assert "stafflist" in bot_module.staff_cmd.aliases
    assert bot_module.bot.get_command("staff") is None

    async def verify() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.execute("CREATE TABLE bot_staff (user_id INTEGER)")
        monkeypatch.setattr(bot_module.bot, "db", database, raising=False)
        ctx = SimpleNamespace(send=AsyncMock())
        try:
            await bot_module.staff_cmd.callback(ctx)
            ctx.send.assert_not_awaited()
        finally:
            await database.close()

    asyncio.run(verify())


def test_slash_registration_skips_reply_based_giveaway_commands() -> None:
    import main as bot_module

    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    excluded_names = ("gend", "greroll", "setwin", "finish", "reroll")

    for name in (*excluded_names, "slash_probe"):
        async def command_callback(ctx):
            pass

        bot.add_command(commands.Command(command_callback, name=name))

    bot_module.register_prefix_slash_commands(bot)

    registered_subcommands = {
        child.name
        for group in bot.tree.get_commands()
        if isinstance(group, discord.app_commands.Group)
        for child in group.commands
    }

    for name in excluded_names:
        assert name not in registered_subcommands
    assert "slash_probe" in registered_subcommands


def test_env_ids_returns_all_valid_comma_separated_ids(monkeypatch) -> None:
    import main as bot_module

    monkeypatch.setenv("OWNER_IDS", "123, invalid, 456,")

    assert bot_module._env_ids("OWNER_IDS") == [123, 456]


def test_live_leaderboard_refresh_skips_unchanged_embeds() -> None:
    import main as bot_module

    current = discord.Embed(title="Leaderboard", description="Top members")
    message = SimpleNamespace(embeds=[current])

    assert not bot_module._live_leaderboard_embed_changed(
        message, discord.Embed.from_dict(current.to_dict())
    )
    assert bot_module._live_leaderboard_embed_changed(
        message, discord.Embed(title="Leaderboard", description="Updated members")
    )


def test_member_statistics_are_isolated_by_guild_and_keep_legacy_rows() -> None:
    import main as bot_module

    async def verify() -> None:
        original_bot = bot_module.bot
        database = await aiosqlite.connect(":memory:")
        await database.execute(
            """
            CREATE TABLE users (
                user_id INTEGER PRIMARY KEY, joins INTEGER DEFAULT 0,
                left INTEGER DEFAULT 0, bonus INTEGER DEFAULT 0,
                total_msgs INTEGER DEFAULT 0, weekly_msgs INTEGER DEFAULT 0, today_msgs INTEGER DEFAULT 0,
                voice_alltime INTEGER DEFAULT 0, voice_weekly INTEGER DEFAULT 0, voice_daily INTEGER DEFAULT 0,
                fake_invites INTEGER NOT NULL DEFAULT 0, rejoins INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        await database.execute("INSERT INTO users (user_id, total_msgs) VALUES (9, 15)")
        await bot_module._ensure_guild_scoped_table(
            database,
            "users",
            """
            CREATE TABLE IF NOT EXISTS users (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, joins INTEGER DEFAULT 0,
                left INTEGER DEFAULT 0, bonus INTEGER DEFAULT 0,
                total_msgs INTEGER DEFAULT 0, weekly_msgs INTEGER DEFAULT 0, today_msgs INTEGER DEFAULT 0,
                voice_alltime INTEGER DEFAULT 0, voice_weekly INTEGER DEFAULT 0, voice_daily INTEGER DEFAULT 0,
                fake_invites INTEGER NOT NULL DEFAULT 0, rejoins INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )
            """,
        )
        bot_module.bot = SimpleNamespace(db=database)
        try:
            async with database.execute(
                "SELECT total_msgs FROM users_legacy WHERE user_id=9"
            ) as cursor:
                assert await cursor.fetchone() == (15,)

            await bot_module.update_user(1, 9, total_msgs=4)
            await bot_module.update_user(2, 9, total_msgs=10)
            assert (await bot_module.get_user_data(1, 9))[3] == 4
            assert (await bot_module.get_user_data(2, 9))[3] == 10
        finally:
            bot_module.bot = original_bot
            await database.close()

    asyncio.run(verify())


def test_message_counters_increment_atomically() -> None:
    import main as bot_module

    async def verify() -> None:
        original_bot = bot_module.bot
        database = await aiosqlite.connect(":memory:")
        await database.execute(
            """
            CREATE TABLE users (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                total_msgs INTEGER DEFAULT 0,
                weekly_msgs INTEGER DEFAULT 0,
                today_msgs INTEGER DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )
        bot_module.bot = SimpleNamespace(db=database)
        try:
            assert await bot_module.increment_message_counters(1, 42) == 1
            assert await bot_module.increment_message_counters(1, 42) == 2
            async with database.execute(
                "SELECT total_msgs, weekly_msgs, today_msgs FROM users "
                "WHERE guild_id = 1 AND user_id = 42"
            ) as cursor:
                assert await cursor.fetchone() == (2, 2, 2)
        finally:
            bot_module.bot = original_bot
            await database.close()

    asyncio.run(verify())


def test_server_prefix_can_be_set_and_restored_to_default() -> None:
    import main as bot_module

    async def verify_prefix_commands() -> None:
        original_database = bot_module.bot.db
        database = await aiosqlite.connect(":memory:")
        await database.execute(
            "CREATE TABLE guild_prefixes (guild_id INTEGER PRIMARY KEY, prefix TEXT NOT NULL)"
        )
        await database.execute(
            "CREATE TABLE noprefix (user_id INTEGER PRIMARY KEY, expiry TEXT, permanent INTEGER DEFAULT 0)"
        )
        bot_module.bot.db = database
        context = SimpleNamespace(
            guild=SimpleNamespace(id=71),
            send=AsyncMock(),
        )
        message = SimpleNamespace(
            guild=context.guild,
            author=SimpleNamespace(id=700),
        )
        try:
            await bot_module.setprefix.callback(context, prefix="?")
            assert await bot_module.bot.get_prefix(message) == ["?", bot_module.BOT_PREFIX]
            await bot_module.deleteprefix.callback(context)
            assert await bot_module.bot.get_prefix(message) == [bot_module.BOT_PREFIX]
        finally:
            bot_module.bot.db = original_database
            await database.close()

    asyncio.run(verify_prefix_commands())


def test_no_prefix_grant_applies_globally_across_guilds() -> None:
    import main as bot_module

    async def verify_global_no_prefix() -> None:
        original_database = bot_module.bot.db
        database = await aiosqlite.connect(":memory:")
        await database.executescript(
            """
            CREATE TABLE guild_prefixes (guild_id INTEGER PRIMARY KEY, prefix TEXT NOT NULL);
            CREATE TABLE noprefix (
                user_id INTEGER PRIMARY KEY, expiry TEXT, permanent INTEGER DEFAULT 0
            );
            INSERT INTO guild_prefixes VALUES (71, '?'), (72, '!');
            INSERT INTO noprefix VALUES (700, '9999-12-31T00:00:00+00:00', 1);
            """
        )
        bot_module.bot.db = database
        try:
            for guild_id, custom_prefix in ((71, "?"), (72, "!")):
                message = SimpleNamespace(
                    guild=SimpleNamespace(id=guild_id),
                    author=SimpleNamespace(id=700),
                )
                assert await bot_module.bot.get_prefix(message) == [
                    custom_prefix,
                    bot_module.BOT_PREFIX,
                    "",
                ]
        finally:
            bot_module.bot.db = original_database
            await database.close()

    asyncio.run(verify_global_no_prefix())


def test_nonpremium_guild_join_notifies_owner_and_leaves(monkeypatch) -> None:
    import main as bot_module

    async def verify_nonpremium_join() -> None:
        channel = SimpleNamespace(
            permissions_for=lambda _member: SimpleNamespace(send_messages=True),
            send=AsyncMock(),
        )
        owner = SimpleNamespace(send=AsyncMock())
        inviter = SimpleNamespace(id=801, send=AsyncMock())

        async def audit_logs(*, limit, action):
            assert limit == 10
            assert action == discord.AuditLogAction.bot_add
            yield SimpleNamespace(
                target=SimpleNamespace(id=99),
                user=inviter,
                created_at=bot_module.datetime.datetime.now(bot_module.datetime.timezone.utc),
            )

        guild = SimpleNamespace(
            id=88,
            owner_id=800,
            owner=owner,
            me=object(),
            system_channel=channel,
            text_channels=[channel],
            audit_logs=audit_logs,
            leave=AsyncMock(),
            invites=AsyncMock(return_value=[]),
        )
        monkeypatch.setattr(
            bot_module,
            "bot",
            SimpleNamespace(
                user=SimpleNamespace(id=99),
                fetch_user=AsyncMock(),
                inv_cache={},
            ),
        )
        premium_check = AsyncMock(side_effect=lambda user_id: user_id == guild.owner_id)
        monkeypatch.setattr(bot_module, "is_user_premium", premium_check)
        monkeypatch.setattr(bot_module, "SUPPORT_SERVER_URL", "https://discord.gg/falcon")
        style_guild = AsyncMock()
        monkeypatch.setattr(bot_module, "apply_golden_neon_name_style", style_guild)

        await bot_module.on_guild_join(guild)

        channel.send.assert_awaited_once()
        inviter.send.assert_awaited_once()
        owner.send.assert_not_awaited()
        premium_check.assert_awaited_once_with(inviter.id)
        for send_call in (channel.send.await_args, inviter.send.await_args):
            assert send_call.kwargs["embed"].title == "Premium Required"
            support_button = send_call.kwargs["view"].children[0]
            assert support_button.url == "https://discord.gg/falcon"
        guild.leave.assert_awaited_once()
        guild.invites.assert_not_awaited()
        style_guild.assert_not_awaited()

    asyncio.run(verify_nonpremium_join())


def test_nonpremium_guild_join_without_support_url_uses_no_empty_view(monkeypatch) -> None:
    import main as bot_module

    async def verify_missing_support_url() -> None:
        channel = SimpleNamespace(
            permissions_for=lambda _member: SimpleNamespace(send_messages=True),
            send=AsyncMock(),
        )
        owner = SimpleNamespace(send=AsyncMock())
        guild = SimpleNamespace(
            id=89,
            owner_id=801,
            owner=owner,
            me=object(),
            system_channel=channel,
            text_channels=[],
            leave=AsyncMock(),
        )
        monkeypatch.setattr(
            bot_module,
            "bot",
            SimpleNamespace(user=None, fetch_user=AsyncMock()),
        )
        monkeypatch.setattr(bot_module, "is_user_premium", AsyncMock(return_value=False))
        monkeypatch.setattr(bot_module, "SUPPORT_SERVER_URL", "")

        await bot_module.on_guild_join(guild)

        for send_call in (channel.send.await_args, owner.send.await_args):
            assert send_call.kwargs["view"] is None
            assert "currently unavailable" in send_call.kwargs["embed"].description
        guild.leave.assert_awaited_once()

    asyncio.run(verify_missing_support_url())


def test_invite_tracking_counts_rejoins_and_classifies_fake_accounts() -> None:
    import datetime
    import main as bot_module

    async def verify_invite_tracking() -> None:
        original_bot = bot_module.bot
        database = await aiosqlite.connect(":memory:")
        await database.executescript(
            """
            CREATE TABLE users (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                joins INTEGER DEFAULT 0, left INTEGER DEFAULT 0,
                bonus INTEGER DEFAULT 0, total_msgs INTEGER DEFAULT 0, weekly_msgs INTEGER DEFAULT 0,
                today_msgs INTEGER DEFAULT 0, voice_alltime INTEGER DEFAULT 0,
                voice_weekly INTEGER DEFAULT 0, voice_daily INTEGER DEFAULT 0,
                fake_invites INTEGER NOT NULL DEFAULT 0, rejoins INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            );
            CREATE TABLE autoroles (guild_id INTEGER, role_id INTEGER, role_type TEXT);
            CREATE TABLE guild_settings (guild_id INTEGER PRIMARY KEY, alt_threshold INTEGER);
            CREATE TABLE invite_roles (guild_id INTEGER, role_id INTEGER, required_invites INTEGER);
            CREATE TABLE invite_tracker (
                guild_id INTEGER, invitee_id INTEGER, inviter_id INTEGER, joined_at TEXT,
                PRIMARY KEY (guild_id, invitee_id)
            );
            CREATE TABLE invite_member_tracker (
                guild_id INTEGER NOT NULL, invitee_id INTEGER NOT NULL, inviter_id INTEGER,
                joined_at TEXT NOT NULL, is_fake INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1, PRIMARY KEY (guild_id, invitee_id)
            );
            INSERT INTO guild_settings VALUES (71, 0);
            """
        )
        inviter = SimpleNamespace(
            id=700,
            display_name="Inviter",
            mention="<@700>",
            roles=[],
            add_roles=AsyncMock(),
        )
        invite_states = [
            [SimpleNamespace(code="abc", uses=1, inviter=inviter)],
            [SimpleNamespace(code="abc", uses=2, inviter=inviter)],
            [SimpleNamespace(code="abc", uses=3, inviter=inviter)],
        ]
        guild = SimpleNamespace(
            id=71,
            invites=AsyncMock(side_effect=invite_states),
            get_role=lambda _role_id: None,
            get_member=lambda user_id: inviter if user_id == inviter.id else None,
        )
        member = SimpleNamespace(
            id=701,
            bot=False,
            created_at=datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=30),
            guild=guild,
            roles=[],
            add_roles=AsyncMock(),
        )
        bot_module.bot = SimpleNamespace(
            db=database,
            inv_cache={71: {"abc": 0}},
        )
        try:
            await bot_module.on_member_join(member)
            await bot_module.on_member_remove(member)
            await bot_module.on_member_join(member)
            async with database.execute(
                "SELECT joins, left, fake_invites, rejoins FROM users WHERE guild_id=? AND user_id=?",
                (guild.id, inviter.id),
            ) as cursor:
                assert await cursor.fetchone() == (2, 1, 0, 1)

            await database.execute(
                "UPDATE guild_settings SET alt_threshold=7 WHERE guild_id=71"
            )
            fake_member = SimpleNamespace(
                id=702,
                bot=False,
                created_at=datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(days=1),
                guild=guild,
                roles=[],
                add_roles=AsyncMock(),
            )
            await bot_module.on_member_join(fake_member)
            async with database.execute(
                "SELECT joins, left, fake_invites, rejoins FROM users WHERE guild_id=? AND user_id=?",
                (guild.id, inviter.id),
            ) as cursor:
                assert await cursor.fetchone() == (2, 1, 1, 1)
            await bot_module.resetinvites.callback(
                SimpleNamespace(guild=guild, send=AsyncMock()), inviter, "fakes"
            )
            async with database.execute(
                "SELECT joins, left, fake_invites, rejoins FROM users WHERE guild_id=? AND user_id=?",
                (guild.id, inviter.id),
            ) as cursor:
                assert await cursor.fetchone() == (2, 1, 0, 1)

            young_member = SimpleNamespace(
                bot=False,
                created_at=datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(days=1),
            )
            bot_member = SimpleNamespace(bot=True, created_at=young_member.created_at)
            assert bot_module.is_fake_invite(young_member, 7)
            assert not bot_module.is_fake_invite(young_member, 0)
            assert bot_module.is_fake_invite(bot_member, 0)
        finally:
            bot_module.bot = original_bot
            await database.close()

    asyncio.run(verify_invite_tracking())
































def test_channel_delete_requires_owner_or_administrator() -> None:
    import main as bot_module

    async def verify_channel_delete_access() -> None:
        channel = SimpleNamespace(id=20, mention="<#20>", delete=AsyncMock())
        bot_member = SimpleNamespace(
            guild_permissions=discord.Permissions(manage_channels=True)
        )
        guild = SimpleNamespace(id=7, owner_id=3, me=bot_member)
        denied_context = SimpleNamespace(
            guild=guild,
            author=SimpleNamespace(
                id=4,
                guild_permissions=discord.Permissions(manage_channels=True),
            ),
            send=AsyncMock(),
        )
        await bot_module.channel_delete.callback(denied_context, channel)
        assert "Only the server owner or an Administrator" in denied_context.send.await_args.args[0]
        channel.delete.assert_not_awaited()

        owner_context = SimpleNamespace(
            guild=guild,
            author=SimpleNamespace(
                id=3,
                mention="<@3>",
                guild_permissions=discord.Permissions.none(),
            ),
            send=AsyncMock(),
        )
        await bot_module.channel_delete.callback(owner_context, channel, reason="cleanup")
        assert isinstance(owner_context.send.await_args.kwargs["view"], bot_module.ChannelDeleteConfirmationView)
        channel.delete.assert_not_awaited()

    asyncio.run(verify_channel_delete_access())


def test_channel_delete_picker_requires_selection_and_deletes_on_confirm(monkeypatch) -> None:
    import main as bot_module

    async def verify_picker() -> None:
        channels = [
            SimpleNamespace(id=20, name="old-channel", mention="<#20>", delete=AsyncMock()),
            SimpleNamespace(id=22, name="second-channel", mention="<#22>", delete=AsyncMock()),
        ]
        guild = SimpleNamespace(
            id=7,
            owner_id=3,
            me=SimpleNamespace(
                guild_permissions=discord.Permissions(manage_channels=True)
            ),
            get_channel=lambda channel_id: next(
                (channel for channel in channels if channel.id == channel_id), None
            ),
        )
        context = SimpleNamespace(
            guild=guild,
            author=SimpleNamespace(
                id=3,
                mention="<@3>",
                guild_permissions=discord.Permissions.none(),
            ),
            send=AsyncMock(),
        )
        monkeypatch.setattr(bot_module, "edit_interaction_v2", AsyncMock())
        await bot_module.channel_delete.callback(context)
        view = context.send.await_args.kwargs["view"]
        assert view.channels == []
        assert view.channel_select.max_values == 25
        delete_button = next(item for item in view.children if getattr(item, "label", None) == "Delete Channels")
        cancel_button = next(item for item in view.children if getattr(item, "label", None) == "Cancel")
        assert delete_button.emoji.name == "delete"
        assert cancel_button.emoji.name == "cross"
        assert delete_button.disabled

        view.channel_select._values = [SimpleNamespace(id=20), SimpleNamespace(id=22)]
        await view.select_channel(SimpleNamespace())
        assert view.channels == channels
        assert not delete_button.disabled
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            message=SimpleNamespace(
                flags=SimpleNamespace(is_components_v2=True),
                edit=AsyncMock(),
            ),
        )
        await delete_button.callback(interaction)
        for channel in channels:
            channel.delete.assert_awaited_once()
        interaction.response.defer.assert_awaited_once()
        status_layout = interaction.message.edit.await_args.kwargs["view"]
        assert "old-channel" in status_layout.children[0].children[0].content
        assert "second-channel" in status_layout.children[0].children[0].content

        cancel_channel = SimpleNamespace(
            id=21,
            name="keep-channel",
            mention="<#21>",
            delete=AsyncMock(),
        )
        cancel_view = bot_module.ChannelDeleteConfirmationView(context, cancel_channel, "cleanup")
        cancel_interaction = SimpleNamespace()
        cancel_button = next(
            item for item in cancel_view.children if getattr(item, "label", None) == "Cancel"
        )
        await cancel_button.callback(cancel_interaction)
        cancel_channel.delete.assert_not_awaited()

    asyncio.run(verify_picker())


def test_bulk_ban_prompts_and_excludes_owner_admin_and_invoker() -> None:
    import main as bot_module

    async def verify_bulk_targets() -> None:
        def member(user_id, *, bot=False, admin=False):
            return SimpleNamespace(
                id=user_id,
                bot=bot,
                guild_permissions=discord.Permissions(administrator=admin),
            )

        targets = [
            member(1),
            member(2),
            member(3, admin=True),
            member(4),
            member(5, bot=True),
            member(99, bot=True),
        ]
        guild = SimpleNamespace(
            owner_id=1,
            members=targets,
            chunked=True,
            me=SimpleNamespace(
                id=99,
                guild_permissions=discord.Permissions(
                    administrator=True,
                    ban_members=True,
                    manage_guild=True,
                ),
            ),
        )
        ctx = SimpleNamespace(
            guild=guild,
            author=SimpleNamespace(id=2),
            interaction=None,
            send=AsyncMock(),
        )
        await bot_module.banall.callback(ctx, scope="all", reason="test")
        view = ctx.send.await_args.kwargs["view"]
        assert {member.id for member in view.targets} == {4, 5}
        assert "2" in ctx.send.await_args.kwargs["embed"].description
        confirm_button = next(button for button in view.children if button.label == "Confirm")
        assert confirm_button.emoji.name == "fp_tick"
        assert confirm_button.emoji.id == 1556939072646287360
        unban_view = bot_module.BulkModerationConfirmView(ctx, "unban", [], "test")
        unban_confirm_button = next(
            button for button in unban_view.children if button.label == "Confirm"
        )
        assert unban_confirm_button.emoji.id == 1556939072646287360

    asyncio.run(verify_bulk_targets())


def test_warn_and_ban_success_messages_start_with_manifest_tick(monkeypatch) -> None:
    import main as bot_module
    import zyre_features

    async def verify_success_messages() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.execute(
            "CREATE TABLE zyre_warnings (warning_id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, "
            "user_id INTEGER, moderator_id INTEGER, reason TEXT, created_at TEXT)"
        )
        await database.commit()
        warn_ctx = SimpleNamespace(
            guild=SimpleNamespace(id=7),
            author=SimpleNamespace(id=3),
            send=AsyncMock(),
        )
        member = SimpleNamespace(id=4, mention="<@4>")
        zyre = zyre_features.ZyreFeatures(SimpleNamespace(db=database))
        zyre._resolve_member = AsyncMock(return_value=member)
        try:
            await zyre_features.ZyreFeatures.warn.callback(zyre, warn_ctx, member, reason="test")
            warning_message = warn_ctx.send.await_args.args[0]
            assert warning_message.startswith(f"{zyre_features.SUCCESS_TICK} **Warning Recorded**")
            assert f"{zyre_features.DETAIL_ARROW} <@4> has 1 active warning(s)." in warning_message

            member.ban = AsyncMock()
            ctx = SimpleNamespace(author=SimpleNamespace(id=3))
            view = bot_module.ModerationConfirmView(ctx, "ban", member, "test")
            edit = AsyncMock()
            monkeypatch.setattr(bot_module, "edit_interaction_v2", edit)
            await view._finish(SimpleNamespace(), True)
            member.ban.assert_awaited_once_with(reason="test")
            result_embed = edit.await_args.kwargs["embed"]
            assert result_embed.description.startswith(f"{bot_module.FP_TICK} **Member Banned**")
            assert f"{bot_module.ARROW} <@4> was banned." in result_embed.description
        finally:
            await database.close()

    asyncio.run(verify_success_messages())


def test_afk_stays_active_when_nickname_cannot_be_changed(monkeypatch) -> None:
    import zyre_features

    async def verify_afk_without_nickname() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.executescript(
            """
            CREATE TABLE zyre_afk (
                guild_id INTEGER, user_id INTEGER, status TEXT, created_at TEXT,
                original_nick TEXT, PRIMARY KEY(guild_id, user_id)
            );
            CREATE TABLE zyre_settings (guild_id INTEGER PRIMARY KEY);
            INSERT INTO zyre_settings VALUES (7);
            """
        )
        member = SimpleNamespace(id=2, nick=None, display_name="User", mention="<@2>")
        context = SimpleNamespace(
            guild=SimpleNamespace(id=7),
            author=member,
            send=AsyncMock(),
        )
        cog = zyre_features.ZyreFeatures(SimpleNamespace(db=database))
        cog._set_afk_nickname = AsyncMock(return_value=False)
        cog._ensure_settings = AsyncMock()
        send = AsyncMock()
        monkeypatch.setattr(zyre_features, "send_v2", send)
        try:
            await zyre_features.ZyreFeatures.afk.callback(cog, context, status="sojao")
            assert context.send.await_args.args == ("AFK status set: sojao.",)
            async with database.execute(
                "SELECT status FROM zyre_afk WHERE guild_id=7 AND user_id=2"
            ) as cursor:
                assert await cursor.fetchone() == ("sojao",)

            message = SimpleNamespace(
                guild=context.guild,
                author=SimpleNamespace(id=9, bot=False),
                content="",
                mentions=[member],
                channel=SimpleNamespace(),
            )
            await cog.process_message(message)
            send.assert_awaited_once()
            assert "is AFK: sojao" in send.await_args.args[1]
        finally:
            await database.close()

    asyncio.run(verify_afk_without_nickname())


def test_nick_success_message_uses_fp_tick(monkeypatch) -> None:
    import main as bot_module

    async def verify_nick_success_message() -> None:
        member = SimpleNamespace(
            id=4,
            mention="<@4>",
            display_name="User",
            name="User",
            edit=AsyncMock(),
        )
        ctx = SimpleNamespace(author=SimpleNamespace(name="Moderator"), send=AsyncMock())
        monkeypatch.setattr(bot_module, "_resolve_context_member", AsyncMock(return_value=member))

        await bot_module.nick.callback(ctx, arguments="<@4> moon")

        member.edit.assert_awaited_once()
        assert member.edit.await_args.kwargs["nick"] == "moon"
        description = ctx.send.await_args.kwargs["embed"].description
        assert description == "<:fp_tick:1556939072646287360> Changed <@4>'s nickname to **moon**."

    asyncio.run(verify_nick_success_message())


def test_giphy_action_search_uses_gif_results_and_safe_rating(monkeypatch) -> None:
    import community_features

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def json(self):
            return {
                "data": [
                    {"images": {"original": {"url": "https://gif.test/hug.gif"}}},
                    {"images": {"original": {"url": "javascript:alert(1)"}}},
                    {"images": None},
                ]
            }

    class Session:
        calls = []

        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def get(self, url, *, params):
            self.calls.append((url, params, self.timeout.total))
            return Response()

    async def verify_giphy_request() -> None:
        cog = community_features.CommunityFeatures(SimpleNamespace(user=None))
        gif_urls = await cog._request_giphy_action_gif_urls("hug")
        assert gif_urls == ["https://gif.test/hug.gif"]
        assert Session.calls == [
            (
                "https://api.giphy.com/v1/gifs/search",
                {
                    "api_key": "test-key",
                    "q": "anime hug",
                    "limit": 10,
                    "rating": "g",
                    "bundle": "messaging_non_clips",
                },
                7,
            )
        ]

    monkeypatch.setenv("GIPHY_API_KEY", "test-key")
    monkeypatch.setattr(community_features.aiohttp, "ClientSession", Session)
    asyncio.run(verify_giphy_request())


def test_giphy_commands_cover_all_legacy_client_endpoints() -> None:
    import community_features

    async def verify_endpoints() -> None:
        cog = community_features.CommunityFeatures(SimpleNamespace(user=None))
        ctx = SimpleNamespace(interaction=None, send=AsyncMock())
        routes = []

        async def capture_endpoint(_ctx, endpoint, params, title):
            routes.append((endpoint, params, title))

        cog._send_giphy_endpoint = capture_endpoint
        await community_features.CommunityFeatures.giphy_search.callback(
            cog, ctx, query="anime hug"
        )
        await community_features.CommunityFeatures.giphy_trending.callback(
            cog, ctx, limit=8, offset=4
        )
        await community_features.CommunityFeatures.giphy_random.callback(
            cog, ctx, tag="cats"
        )
        await community_features.CommunityFeatures.giphy_translate.callback(
            cog, ctx, phrase="feeling happy"
        )
        await community_features.CommunityFeatures.giphy_get.callback(
            cog, ctx, ids="gif123"
        )
        await community_features.CommunityFeatures.giphy_get.callback(
            cog, ctx, ids="gif123,gif456"
        )
        await community_features.CommunityFeatures.giphy_categories.callback(cog, ctx)
        await community_features.CommunityFeatures.giphy_category.callback(
            cog, ctx, category="reactions", limit=5, offset=10
        )
        await community_features.CommunityFeatures.giphy_category_tag.callback(
            cog, ctx, category="reactions", tag="wave", limit=5, offset=10
        )
        await community_features.CommunityFeatures.giphy_sticker_search.callback(
            cog, ctx, query="anime hug"
        )
        await community_features.CommunityFeatures.giphy_sticker_trending.callback(
            cog, ctx, limit=7, offset=3
        )
        await community_features.CommunityFeatures.giphy_sticker_random.callback(
            cog, ctx, tag="cats"
        )
        await community_features.CommunityFeatures.giphy_sticker_translate.callback(
            cog, ctx, phrase="feeling happy"
        )

        assert [(endpoint, params) for endpoint, params, _title in routes] == [
            (
                "gifs/search",
                {"q": "anime hug", "limit": 25, "offset": 0, "rating": "g"},
            ),
            ("gifs/trending", {"limit": 8, "offset": 4, "rating": "g"}),
            ("gifs/random", {"rating": "g", "tag": "cats"}),
            ("gifs/translate", {"s": "feeling happy", "rating": "g"}),
            ("gifs/gif123", {}),
            ("gifs", {"ids": "gif123,gif456"}),
            ("gifs/categories", {"limit": 25}),
            (
                "gifs/categories/reactions",
                {"limit": 5, "offset": 10},
            ),
            (
                "gifs/categories/reactions/wave",
                {"limit": 5, "offset": 10},
            ),
            (
                "stickers/search",
                {"q": "anime hug", "limit": 25, "offset": 0, "rating": "g"},
            ),
            ("stickers/trending", {"limit": 7, "offset": 3, "rating": "g"}),
            ("stickers/random", {"rating": "g", "tag": "cats"}),
            ("stickers/translate", {"s": "feeling happy", "rating": "g"}),
        ]

    asyncio.run(verify_endpoints())


def test_giphy_commands_are_direct_and_sticker_search_has_simple_alias() -> None:
    import community_features

    commands = [
        community_features.CommunityFeatures.giphy_search,
        community_features.CommunityFeatures.giphy_trending,
        community_features.CommunityFeatures.giphy_random,
        community_features.CommunityFeatures.giphy_translate,
        community_features.CommunityFeatures.giphy_get,
        community_features.CommunityFeatures.giphy_categories,
        community_features.CommunityFeatures.giphy_category,
        community_features.CommunityFeatures.giphy_category_tag,
        community_features.CommunityFeatures.giphy_sticker_search,
        community_features.CommunityFeatures.giphy_sticker_trending,
        community_features.CommunityFeatures.giphy_sticker_random,
        community_features.CommunityFeatures.giphy_sticker_translate,
    ]

    assert [command.name for command in commands] == [
        "search",
        "trending",
        "random",
        "translate",
        "get",
        "categories",
        "category",
        "category-tag",
        "sticker",
        "sticker-trending",
        "sticker-random",
        "sticker-translate",
    ]
    assert "sticker-search" in community_features.CommunityFeatures.giphy_sticker_search.aliases
    assert all(not hasattr(command, "app_command") for command in commands)
    assert not hasattr(community_features.CommunityFeatures, "giphy_group")


def test_giphy_results_show_animated_gifs_and_category_lists(monkeypatch) -> None:
    import community_features

    async def verify_result_rendering() -> None:
        cog = community_features.CommunityFeatures(SimpleNamespace(user=None))
        edit_calls = []

        async def edit_interaction(interaction, **kwargs):
            edit_calls.append((interaction, kwargs))

        monkeypatch.setattr(
            community_features,
            "edit_interaction_v2",
            edit_interaction,
        )
        ctx = SimpleNamespace(
            author=SimpleNamespace(id=9),
            interaction=None,
            send=AsyncMock(),
        )
        await cog._send_giphy_payload(
            ctx,
            {
                "data": [
                    {
                        "title": "Hug 1",
                        "url": "https://giphy.com/gifs/hug-one",
                        "images": {"original": {"url": "https://gif.test/hug-1.gif"}},
                    },
                    {
                        "title": "Hug 2",
                        "url": "https://giphy.com/gifs/hug-two",
                        "images": {"original": {"url": "https://gif.test/hug-2.gif"}},
                    },
                ]
            },
            "GIPHY Search",
        )
        first_response = ctx.send.await_args.kwargs
        assert first_response["embed"].image.url == "https://gif.test/hug-1.gif"
        assert "Powered by GIPHY" in first_response["embed"].footer.text
        view = first_response["view"]
        assert view.next_page.disabled is False

        interaction = SimpleNamespace(
            response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock())
        )
        await view._change_page(interaction, 1)
        assert edit_calls[0][0] is interaction
        assert edit_calls[0][1]["embed"].image.url == "https://gif.test/hug-2.gif"
        assert edit_calls[0][1]["view"] is view

        ctx.send.reset_mock()
        await cog._send_giphy_payload(
            ctx,
            {
                "data": [
                    {
                        "name": "Reactions",
                        "subcategories": [{"name": "Wave"}],
                    }
                ]
            },
            "GIPHY Categories",
        )
        category_embed = ctx.send.await_args.kwargs["embed"]
        assert "Reactions" in category_embed.description
        assert "Wave" in category_embed.description
        assert "Powered by GIPHY" in category_embed.footer.text

    asyncio.run(verify_result_rendering())


def test_fun_gif_uses_giphy_when_configured(monkeypatch) -> None:
    import community_features

    async def verify_giphy_provider() -> None:
        cog = community_features.CommunityFeatures(SimpleNamespace(user=None))
        cog._request_giphy_action_gif_urls = AsyncMock(
            return_value=["https://gif.test/giphy-hug.gif"]
        )

        giphy_url = await cog._fetch_fun_gif("hug")
        assert giphy_url == "https://gif.test/giphy-hug.gif"
        cog._request_giphy_action_gif_urls.assert_awaited_once_with("hug")

    monkeypatch.setenv("GIPHY_API_KEY", "test-key")
    asyncio.run(verify_giphy_provider())


def test_fun_gif_rotation_avoids_immediate_repeats(monkeypatch) -> None:
    import community_features

    async def verify_fun_gif_rotation() -> None:
        cog = community_features.CommunityFeatures(SimpleNamespace(user=None))
        requests = []

        async def request_gifs(category):
            requests.append(category)
            return ["https://gif.test/slap-1.gif", "https://gif.test/slap-2.gif"]

        cog._request_giphy_action_gif_urls = request_gifs
        gif_urls = [await cog._fetch_fun_gif("slap") for _ in range(3)]
        assert gif_urls == [
            "https://gif.test/slap-1.gif",
            "https://gif.test/slap-2.gif",
            "https://gif.test/slap-1.gif",
        ]
        assert requests == ["slap", "slap", "slap"]

    monkeypatch.setenv("GIPHY_API_KEY", "test-key")
    asyncio.run(verify_fun_gif_rotation())


def test_fun_gif_returns_no_gif_without_giphy_key(monkeypatch) -> None:
    import community_features

    async def verify_missing_key() -> None:
        cog = community_features.CommunityFeatures(SimpleNamespace(user=None))
        cog._request_giphy_action_gif_urls = AsyncMock()
        assert await cog._fetch_fun_gif("slap") is None
        cog._request_giphy_action_gif_urls.assert_not_awaited()

    monkeypatch.delenv("GIPHY_API_KEY", raising=False)
    asyncio.run(verify_missing_key())


def test_fun_spam_commands_share_one_response() -> None:
    import community_features

    async def verify_fun_spam() -> None:
        member_id = 123456789012345678
        member = SimpleNamespace(id=member_id, mention=f"<@{member_id}>")
        cog = community_features.CommunityFeatures(SimpleNamespace(user=None))
        cog.send_fun_action = AsyncMock()
        guild = SimpleNamespace(get_member=lambda user_id: member if user_id == member.id else None)
        ctx = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda _name: cog),
            guild=guild,
            message=SimpleNamespace(mentions=[member], reference=None),
            send=AsyncMock(),
            interaction=None,
        )
        command = community_features._make_fun_command("hug")
        await command.callback(ctx, arguments="spam")
        cog.send_fun_action.assert_awaited_once_with(ctx, "hug", member, spam=True)

        class Cog:
            async def _fetch_fun_gif(self, _action):
                return "https://gif.test/hug.gif"

            async def send(self, ctx, action, target, *, spam=False):
                await community_features.CommunityFeatures.send_fun_action(
                    self, ctx, action, target, spam=spam
                )

        ctx.author = SimpleNamespace(mention="<@1>")
        ctx.send = AsyncMock()
        await Cog().send(ctx, "hug", member, spam=True)
        ctx.send.assert_awaited_once()
        response_embed = ctx.send.await_args.kwargs["embed"]
        assert response_embed.title == "Hug Spam"
        assert response_embed.description.count(f"hugged {member.mention}") == 5
        assert response_embed.image.url == "https://gif.test/hug.gif"

    asyncio.run(verify_fun_spam())


def test_role_alias_authorization() -> None:
    from community_features import CommunityFeatures

    class Role:
        def __init__(self, role_id, position):
            self.id = role_id
            self.position = position
            self.mention = f"<@&{role_id}>"

        def is_default(self):
            return False

        def __ge__(self, other):
            return self.position >= other.position

    async def verify_role_toggle() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.execute(
            "CREATE TABLE role_mappings (guild_id INTEGER, alias TEXT, role_id INTEGER)"
        )
        await database.execute(
            "CREATE TABLE role_alias_settings "
            "(guild_id INTEGER PRIMARY KEY, required_role_id INTEGER, updated_by INTEGER, updated_at TEXT)"
        )
        await database.execute("INSERT INTO role_mappings VALUES (1, 'fam', 5)")
        await database.commit()
        bot = SimpleNamespace(db=database)
        cog = CommunityFeatures(bot)
        role = Role(5, 10)
        member = SimpleNamespace(
            id=10,
            mention="<@10>",
            top_role=Role(6, 1),
            roles=[],
            add_roles=AsyncMock(),
            remove_roles=AsyncMock(),
        )
        author = SimpleNamespace(
            id=10,
            guild_permissions=discord.Permissions.none(),
            top_role=Role(6, 1),
        )
        guild = SimpleNamespace(
            id=1,
            me=SimpleNamespace(
                guild_permissions=discord.Permissions(manage_roles=True),
                top_role=Role(7, 100),
            ),
            get_role=lambda role_id: role if role_id == role.id else None,
        )
        ctx = SimpleNamespace(
            guild=guild,
            author=author,
            interaction=None,
            send=AsyncMock(),
        )
        try:
            await cog.toggle_member_role(ctx, "fam", member)
            member.add_roles.assert_awaited_once()
            assert member.add_roles.await_args.args == (role,)
            assert member.add_roles.await_args.kwargs["reason"].startswith("fam role toggle by")
            success_embed = ctx.send.await_args_list[0].kwargs["embed"]
            assert success_embed.title == "<:fp_tick:1556939072646287360>  Role Added"
            assert success_embed.description == (
                "<:fp_arrow:1551879040292360225>  Successfully added <@&5> role to <@10>."
            )
            allowed_mentions = ctx.send.await_args.kwargs["allowed_mentions"]
            assert not allowed_mentions.everyone
            assert not allowed_mentions.roles
            assert not allowed_mentions.users

            member.roles.append(role)
            ctx.send.reset_mock()
            await cog.toggle_member_role(ctx, "fam", member)
            member.remove_roles.assert_awaited_once()
            remove_embed = ctx.send.await_args.kwargs["embed"]
            assert remove_embed.title == "<:cross:1559374062281826397>  Role Removed"
            assert remove_embed.description == (
                "<:fp_arrow:1551879040292360225>  Successfully removed <@&5> role from <@10>."
            )
            allowed_mentions = ctx.send.await_args.kwargs["allowed_mentions"]
            assert not allowed_mentions.everyone
            assert not allowed_mentions.roles
            assert not allowed_mentions.users

            other_member = SimpleNamespace(id=11, top_role=Role(8, 2))
            await cog.toggle_member_role(ctx, "fam", other_member)
            assert "only toggle this role for yourself" in ctx.send.await_args.args[0]
        finally:
            await database.close()

    asyncio.run(verify_role_toggle())


def test_role_create_and_setup_create_are_distinct() -> None:
    import community_features

    setup_commands = {
        command.name
        for command in community_features.CommunityFeatures.role_alias_setup_group.commands
    }
    assert {"create", "list", "remove", "toggle"} <= setup_commands

    created_role = SimpleNamespace(mention="<@&55>")
    create_role = AsyncMock(return_value=created_role)
    configure_alias = AsyncMock()
    cog = SimpleNamespace(_configure_role_alias=configure_alias)
    ctx = SimpleNamespace(
        guild=SimpleNamespace(create_role=create_role),
        author=SimpleNamespace(id=3),
        interaction=None,
        send=AsyncMock(),
    )
    existing_role = SimpleNamespace(id=88)

    async def verify_commands() -> None:
        assert community_features.CommunityFeatures.setup_group.name == "set"
        assert community_features.CommunityFeatures.role_alias_setup_group.name == "setup"
        await community_features.CommunityFeatures.role_create.callback(
            cog, ctx, name="Moderators"
        )
        create_role.assert_awaited_once()
        assert create_role.await_args.kwargs["name"] == "Moderators"
        assert create_role.await_args.kwargs["permissions"] == discord.Permissions.none()

        await community_features.CommunityFeatures.role_alias_setup_create.callback(
            cog, ctx, "fem", existing_role
        )
        configure_alias.assert_awaited_once_with(ctx, "fem", existing_role)

    asyncio.run(verify_commands())


def test_role_alias_creation_uses_custom_command_embed() -> None:
    import community_features

    class Role:
        def __init__(self, role_id, position):
            self.id = role_id
            self.position = position
            self.mention = f"<@&{role_id}>"

        def is_default(self):
            return False

        def __ge__(self, other):
            return self.position >= other.position

    async def verify_creation_embed() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.executescript(
            """
            CREATE TABLE role_mappings (
                guild_id INTEGER, alias TEXT, role_id INTEGER,
                created_by INTEGER, created_at TEXT, PRIMARY KEY(guild_id, alias)
            );
            CREATE TABLE role_alias_settings (
                guild_id INTEGER PRIMARY KEY, required_role_id INTEGER,
                updated_by INTEGER, updated_at TEXT
            );
            INSERT INTO role_alias_settings VALUES (1, 12, 3, '2026-01-01');
            """
        )
        bot = SimpleNamespace(
            db=database,
            get_command=lambda _name: None,
            add_command=Mock(),
        )
        cog = community_features.CommunityFeatures(bot)
        role = Role(55, 10)
        ctx = SimpleNamespace(
            guild=SimpleNamespace(
                id=1,
                me=SimpleNamespace(
                    guild_permissions=discord.Permissions(manage_roles=True),
                    top_role=Role(99, 100),
                ),
            ),
            author=SimpleNamespace(
                id=3,
                guild_permissions=discord.Permissions(administrator=True),
            ),
            interaction=None,
            send=AsyncMock(),
        )
        try:
            await cog._configure_role_alias(ctx, "staff", role)

            embed = ctx.send.await_args.kwargs["embed"]
            assert embed.title == "<:fp_tick:1556939072646287360> Custom Command Created"
            assert embed.description == (
                "<:setting:1561336841842593862>  Usage:\n"
                "<:dot:1551879036022816780>  Use `staff <user>` to assign/remove the role\n"
                "<:dot:1551879036022816780>  Requires configured required role to use\n\n"
                "<:fp_arrow:1551879040292360225>  Created command staff for <@&55>"
            )
            bot.add_command.assert_called_once()
            staff_alias_command = bot.add_command.call_args.args[0]
            assert staff_alias_command.name == "staff"
            member = SimpleNamespace(id=4)
            cog.toggle_member_role = AsyncMock()
            await staff_alias_command.callback(ctx, member)
            cog.toggle_member_role.assert_awaited_once_with(ctx, "staff", member)
            allowed_mentions = ctx.send.await_args.kwargs["allowed_mentions"]
            assert not allowed_mentions.everyone
            assert not allowed_mentions.roles
            assert not allowed_mentions.users
        finally:
            await database.close()

    asyncio.run(verify_creation_embed())


def test_setup_create_allows_builtin_name_and_setup_remove_preserves_builtin() -> None:
    import community_features

    class Role:
        id = 55
        position = 10
        mention = "<@&55>"

        def is_default(self):
            return False

        def __ge__(self, other):
            return self.position >= other.position

    async def verify_setup_create_and_remove() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.executescript(
            """
            CREATE TABLE role_mappings (
                guild_id INTEGER, alias TEXT, role_id INTEGER,
                created_by INTEGER, created_at TEXT, PRIMARY KEY(guild_id, alias)
            );
            CREATE TABLE role_alias_settings (
                guild_id INTEGER PRIMARY KEY, required_role_id INTEGER,
                updated_by INTEGER, updated_at TEXT
            );
            """
        )
        staff_command = object()
        bot = SimpleNamespace(
            db=database,
            get_command=lambda name: staff_command if name == "staff" else None,
            add_command=Mock(),
            remove_command=Mock(),
        )
        cog = community_features.CommunityFeatures(bot)
        ctx = SimpleNamespace(
            guild=SimpleNamespace(
                id=1,
                me=SimpleNamespace(
                    guild_permissions=discord.Permissions(manage_roles=True),
                    top_role=SimpleNamespace(position=100),
                ),
            ),
            author=SimpleNamespace(
                id=3,
                guild_permissions=discord.Permissions(administrator=True),
            ),
            interaction=None,
            send=AsyncMock(),
        )
        try:
            await cog._configure_role_alias(ctx, "staff", Role())
            embed = ctx.send.await_args.kwargs["embed"]
            assert "`setup toggle staff <user>`" in embed.description
            allowed_mentions = ctx.send.await_args.kwargs["allowed_mentions"]
            assert not allowed_mentions.roles
            assert not allowed_mentions.users

            await community_features.CommunityFeatures.role_alias_setup_remove.callback(
                cog, ctx, "staff"
            )
            async with database.execute(
                "SELECT 1 FROM role_mappings WHERE guild_id=1 AND alias='staff'"
            ) as cursor:
                assert await cursor.fetchone() is None
            bot.remove_command.assert_not_called()
        finally:
            await database.close()

    asyncio.run(verify_setup_create_and_remove())


def test_serverinfo_embed_does_not_ping_owner_or_afk_channel() -> None:
    import datetime
    import main as bot_module

    guild = SimpleNamespace(
        name="Example",
        id=1,
        owner=SimpleNamespace(mention="<@2>"),
        owner_id=2,
        verification_level=discord.VerificationLevel.medium,
        afk_timeout=300,
        afk_channel=SimpleNamespace(mention="<#3>"),
        description=None,
        created_at=datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc),
        icon=None,
        channels=[],
        member_count=5,
        roles=[object()],
        emojis=[],
        premium_subscription_count=0,
        premium_tier=0,
    )
    ctx = SimpleNamespace(
        guild=guild,
        author="Requester",
        send=AsyncMock(),
    )

    asyncio.run(bot_module.serverinfo.callback(ctx))

    embed = ctx.send.await_args.kwargs["embed"]
    assert "<@2>" in embed.fields[0].value
    assert "<#3>" in embed.fields[2].value
    allowed_mentions = ctx.send.await_args.kwargs["allowed_mentions"]
    assert not allowed_mentions.everyone
    assert not allowed_mentions.roles
    assert not allowed_mentions.users


def test_setup_reqrole_gates_aliases_and_admins_bypass() -> None:
    import community_features

    class Role:
        def __init__(self, role_id, position):
            self.id = role_id
            self.position = position
            self.mention = f"<@&{role_id}>"

        def is_default(self):
            return False

        def __ge__(self, other):
            return self.position >= other.position

    async def verify_required_role() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.executescript(
            """
            CREATE TABLE role_mappings (
                guild_id INTEGER, alias TEXT, role_id INTEGER
            );
            CREATE TABLE role_alias_settings (
                guild_id INTEGER PRIMARY KEY, required_role_id INTEGER,
                updated_by INTEGER, updated_at TEXT
            );
            INSERT INTO role_mappings VALUES (1, 'fam', 5);
            """
        )
        bot = SimpleNamespace(db=database)
        cog = community_features.CommunityFeatures(bot)
        assigned_role = Role(5, 10)
        required_role = Role(6, 20)
        member = SimpleNamespace(
            id=10,
            mention="<@10>",
            top_role=Role(7, 30),
            roles=[],
            guild_permissions=discord.Permissions.none(),
            add_roles=AsyncMock(),
            remove_roles=AsyncMock(),
        )
        guild = SimpleNamespace(
            id=1,
            me=SimpleNamespace(
                guild_permissions=discord.Permissions(manage_roles=True),
                top_role=Role(8, 100),
            ),
            get_role=lambda role_id: {
                assigned_role.id: assigned_role,
                required_role.id: required_role,
            }.get(role_id),
        )
        ctx = SimpleNamespace(
            guild=guild,
            author=member,
            interaction=None,
            send=AsyncMock(),
        )
        try:
            await community_features.CommunityFeatures.setup_reqrole_prefix.callback(
                cog,
                ctx,
                required_role,
            )
            assert await cog._get_required_role_id(guild.id) == required_role.id
            ctx.send.reset_mock()

            await cog.toggle_member_role(ctx, "fam", member)
            assert "You need <@&6>" in ctx.send.await_args.args[0]
            member.add_roles.assert_not_awaited()

            ctx.author.guild_permissions = discord.Permissions(administrator=True)
            ctx.send.reset_mock()
            await cog.toggle_member_role(ctx, "fam", member)
            member.add_roles.assert_awaited_once()
            assert member.add_roles.await_args.args == (assigned_role,)
            assert member.add_roles.await_args.kwargs["reason"].startswith("fam role toggle by")
            success_embed = ctx.send.await_args.kwargs["embed"]
            assert "Role Added" in success_embed.title
            assert "Successfully added <@&5> role to <@10>." in success_embed.description

            ctx.send.reset_mock()
            await community_features.CommunityFeatures.role_alias_setup_list.callback(cog, ctx)
            list_embed = ctx.send.await_args.kwargs["embed"]
            assert "`fam @user`" in list_embed.description
            assert list_embed.fields[0].name == "Required role"
            assert list_embed.fields[0].value == required_role.mention
            allowed_mentions = ctx.send.await_args.kwargs["allowed_mentions"]
            assert not allowed_mentions.everyone
            assert not allowed_mentions.roles
            assert not allowed_mentions.users
        finally:
            await database.close()

    asyncio.run(verify_required_role())


def test_voice_room_access_override_persists_and_status_is_embed() -> None:
    import denycord_voice_backup

    async def verify_voice_room() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.executescript(
            """
            CREATE TABLE denycord_voice_rooms (
                guild_id INTEGER, channel_id INTEGER, owner_id INTEGER, locked INTEGER,
                user_limit INTEGER, rtc_region TEXT, chat_open INTEGER, hidden INTEGER DEFAULT 0
            );
            CREATE TABLE denycord_voice_overrides (
                guild_id INTEGER, channel_id INTEGER, user_id INTEGER,
                allow_connect INTEGER, allow_chat INTEGER,
                PRIMARY KEY(guild_id, channel_id, user_id)
            );
            INSERT INTO denycord_voice_rooms VALUES (1, 10, 2, 1, 0, NULL, 1, 0);
            """
        )
        owner = SimpleNamespace(id=2, mention="<@2>", top_role=SimpleNamespace(position=10))
        blocked = SimpleNamespace(
            id=3,
            mention="<@3>",
            top_role=SimpleNamespace(position=1),
            move_to=AsyncMock(),
        )
        bot_member = SimpleNamespace(
            id=99,
            guild_permissions=discord.Permissions(move_members=True),
        )
        permissions = SimpleNamespace(manage_channels=True)
        channel = SimpleNamespace(
            id=10,
            name="Room",
            members=[owner, blocked],
            overwrites={},
            overwrites_for=lambda _target: discord.PermissionOverwrite(),
            permissions_for=lambda _member: permissions,
            set_permissions=AsyncMock(),
            edit=AsyncMock(),
        )
        owner.voice = SimpleNamespace(channel=channel)
        guild = SimpleNamespace(
            id=1,
            owner_id=1,
                premium_tier=2,
            me=bot_member,
            default_role=object(),
            get_member=lambda member_id: {2: owner, 3: blocked}.get(member_id),
        )
        channel.guild = guild
        bot = SimpleNamespace(db=database)
        cog = denycord_voice_backup.DenycordVoiceBackup(bot)
        try:
            result = await cog._perform_room_action(guild, owner, "block", blocked.id)
            assert "blocked" in result
            blocked.move_to.assert_awaited_once()
            assert blocked.move_to.await_args.args == (None,)
            assert blocked.move_to.await_args.kwargs["reason"].startswith(
                "Blocked from managed room by"
            )
            async with database.execute(
                "SELECT allow_connect, allow_chat FROM denycord_voice_overrides "
                "WHERE guild_id=1 AND channel_id=10 AND user_id=3"
            ) as cursor:
                assert await cursor.fetchone() == (0, 0)

            await cog._apply_room_overrides(channel)
            assert channel.set_permissions.await_args.kwargs == {
                "view_channel": False,
                "connect": False,
                "send_messages": False,
            }

            status = await cog._perform_room_action(guild, owner, "status")
            assert isinstance(status, discord.Embed)
            assert status.fields[0].value == "Locked"
            assert status.fields[-1].value == "<@3>"
            await database.executemany(
                "INSERT INTO denycord_voice_overrides "
                "(guild_id, channel_id, user_id, allow_connect, allow_chat) "
                "VALUES (1, 10, ?, ?, ?)",
                [
                    (user_id, int(user_id % 2 == 0), int(user_id % 2 == 0))
                    for user_id in range(1000, 1600)
                ],
            )
            await database.commit()
            status = await cog._perform_room_action(guild, owner, "status")
            allowed_field = next(field for field in status.fields if field.name == "Allowed")
            blocked_field = next(field for field in status.fields if field.name == "Blocked")
            assert len(allowed_field.value) <= 1024
            assert len(blocked_field.value) <= 1024
            assert "more" in allowed_field.value
            assert "more" in blocked_field.value

            assert "hidden" in await cog._perform_room_action(guild, owner, "hide")
            await cog._perform_room_action(guild, owner, "lock")
            await cog._perform_room_action(guild, owner, "unlock")
            async with database.execute(
                "SELECT locked, hidden FROM denycord_voice_rooms WHERE guild_id=1 AND channel_id=10"
            ) as cursor:
                assert await cursor.fetchone() == (0, 1)

            assert "128 kbps" in await cog._perform_room_action(guild, owner, "bitrate", 128)
            channel.edit.assert_awaited_once_with(
                bitrate=128_000, reason=f"Room bitrate changed by {owner}"
            )
        finally:
            await database.close()

    asyncio.run(verify_voice_room())


def test_voice_room_card_shows_emoji_legend_and_open_text_permissions() -> None:
    import denycord_voice_backup

    class Target:
        def __init__(self, target_id, mention=None):
            self.id = target_id
            self.mention = mention

        __hash__ = object.__hash__

    cog = denycord_voice_backup.DenycordVoiceBackup(SimpleNamespace(user=None))
    owner = Target(2, "<@2>")
    bot_member = Target(99)
    everyone = Target(1)
    guild = SimpleNamespace(
        me=bot_member,
        default_role=everyone,
        roles=[],
    )
    channel = SimpleNamespace(name="Room", mention="<#10>")

    _stub_room_panel_emoji_download(cog, denycord_voice_backup)
    panel = asyncio.run(cog._room_panel_view(channel, owner))
    overwrites = asyncio.run(cog._room_overwrites(guild, owner))

    with Image.open(io.BytesIO(panel.card_image)) as card_image:
        assert card_image.format == "PNG"
        assert card_image.width == 1600
        assert card_image.height > 650
        assert min(card_image.getpixel((0, 0))) > 220
    assert isinstance(panel.children[0], discord.ui.Container)
    card = panel.children[0]
    assert card.accent_color == 0xFFDD00
    assert isinstance(card.children[0], discord.ui.TextDisplay)
    assert card.children[0].content == (
        "# FALCON / VOICE ROOM STUDIO\n"
        "Use the controls below to manage your temporary voice room. "
        "More options are available with **/voice** commands."
    )
    assert isinstance(card.children[1], discord.ui.MediaGallery)
    image_item = card.children[1].items[0]
    assert image_item.media.url == f"attachment://{panel.card_filename}"
    assert len(image_item.description) <= 256
    assert "Room: Room" in image_item.description
    assert "30 voice controls" in image_item.description
    assert all(
        isinstance(child, discord.ui.ActionRow)
        for child in panel.children[1:]
    )
    panel_buttons = [
        child for child in panel.walk_children()
        if isinstance(child, discord.ui.Button)
    ]
    assert all(button.style is discord.ButtonStyle.secondary for button in panel_buttons)
    assert len(panel_buttons) == 30
    assert len([
        child for child in panel.walk_children()
        if isinstance(child, discord.ui.Button)
    ]) == len(denycord_voice_backup.ROOM_PANEL_BUTTONS)
    assert not any(
        isinstance(child, discord.ui.Select)
        for child in panel.walk_children()
    )
    assert overwrites[everyone].view_channel is True
    assert overwrites[everyone].connect is True
    assert overwrites[everyone].send_messages is True
    assert overwrites[owner].send_messages is True
    assert overwrites[owner].embed_links is True
    assert overwrites[bot_member].send_messages is True
    assert overwrites[bot_member].embed_links is True
    assert overwrites[bot_member].attach_files is True


def test_tempvoice_startup_refreshes_saved_room_panel(monkeypatch) -> None:
    import denycord_voice_backup

    class Cursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def fetchall(self):
            return [(1, 10, 2, 99)]

    class Room:
        id = 10
        name = "Room"
        mention = "<#10>"

        def __init__(self, message):
            self.fetch_message = AsyncMock(return_value=message)

    class Message:
        edit = AsyncMock()

    message = Message()
    room = Room(message)
    guild = SimpleNamespace(get_channel=lambda channel_id: room)
    database = SimpleNamespace(execute=Mock(return_value=Cursor()))
    bot = SimpleNamespace(
        db=database,
        get_guild=Mock(return_value=guild),
    )
    monkeypatch.setattr(denycord_voice_backup.discord, "VoiceChannel", Room)
    cog = denycord_voice_backup.DenycordVoiceBackup(bot)
    _stub_room_panel_emoji_download(cog, denycord_voice_backup)

    assert asyncio.run(cog._refresh_saved_room_panels())
    room.fetch_message.assert_awaited_once_with(99)
    kwargs = message.edit.await_args.kwargs
    assert "embed" not in kwargs
    assert isinstance(kwargs["view"], discord.ui.LayoutView)
    assert len(kwargs["attachments"]) == 1
    assert kwargs["view"].children[0].children[1].items[0].media.url == (
        f"attachment://{kwargs['view'].card_filename}"
    )
    assert kwargs["view"].children[0].children[1].items[0].description.startswith(
        "FALCON VOICE ROOM. Room: Room · Owner:"
    )
    all_panel_buttons = [
        child for child in kwargs["view"].walk_children()
        if isinstance(child, discord.ui.Button)
    ]
    assert len(all_panel_buttons) == len(denycord_voice_backup.ROOM_PANEL_BUTTONS)
    assert all(button.label is None for button in all_panel_buttons)
    assert {button.emoji.id for button in all_panel_buttons} == {
        emoji.id for emoji in denycord_voice_backup.VOICE_BUTTON_EMOJIS.values()
    }
    action_rows = [
        child for child in kwargs["view"].walk_children()
        if isinstance(child, discord.ui.ActionRow)
    ]
    assert len(action_rows) == 6
    assert all(len(row.children) <= 5 for row in action_rows)


def test_voice_room_names_use_one_emoji_prefix(monkeypatch) -> None:
    import denycord_voice_backup

    monkeypatch.setattr(denycord_voice_backup.random, "choice", lambda values: values[0])

    assert denycord_voice_backup._format_room_name("Owner's room") == "🎧 Owner's room"
    assert denycord_voice_backup._format_room_name("🌿 Garden") == "🎧 Garden"
    assert denycord_voice_backup._format_room_name("\n") == "🎧 Temporary room"


def test_voice_room_upstream_aliases_and_category_setup() -> None:
    import denycord_voice_backup

    async def verify_setup_commands() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.execute(
            "CREATE TABLE denycord_voice_settings ("
            "guild_id INTEGER PRIMARY KEY, hub_channel_id INTEGER NOT NULL, "
            "panel_channel_id INTEGER NOT NULL, category_id INTEGER, enabled INTEGER NOT NULL DEFAULT 1)"
        )
        await database.execute(
            "INSERT INTO denycord_voice_settings VALUES (1, 10, 20, NULL, 1)"
        )
        await database.execute(
            "CREATE TABLE denycord_voice_generators ("
            "guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, category_id INTEGER, "
            "created_by INTEGER, created_at TEXT NOT NULL, PRIMARY KEY(guild_id, channel_id))"
        )
        await database.execute(
            "INSERT INTO denycord_voice_generators VALUES (1, 10, NULL, 2, '2026-10-17T00:00:00+00:00')"
        )
        await database.commit()
        cog = denycord_voice_backup.DenycordVoiceBackup(SimpleNamespace(db=database))
        hub = SimpleNamespace(mention="<#10>")
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1, get_channel=lambda channel_id: hub if channel_id == 10 else None),
            interaction=None,
            send=AsyncMock(),
        )
        category = SimpleNamespace(id=30, name="Temporary Rooms")
        try:
            assert set(denycord_voice_backup.DenycordVoiceBackup.room.aliases) >= {
                "temp_vc", "temp", "tv", "temp_voice"
            }
            assert {"info", "set_channel", "set_category"} <= set(
                denycord_voice_backup.DenycordVoiceBackup.room.all_commands
            )
            assert {"hide", "unhide", "ban", "bitrate"} <= set(
                denycord_voice_backup.DenycordVoiceBackup.room.all_commands
            )

            await denycord_voice_backup.DenycordVoiceBackup.room_set_category.callback(
                cog, ctx, category
            )
            async with database.execute(
                "SELECT category_id FROM denycord_voice_settings WHERE guild_id=1"
            ) as cursor:
                assert await cursor.fetchone() == (30,)
            async with database.execute(
                "SELECT category_id FROM denycord_voice_generators WHERE guild_id=1 AND channel_id=10"
            ) as cursor:
                assert await cursor.fetchone() == (30,)

            await denycord_voice_backup.DenycordVoiceBackup.room_set_category.callback(
                cog, ctx, None
            )
            async with database.execute(
                "SELECT category_id FROM denycord_voice_settings WHERE guild_id=1"
            ) as cursor:
                assert await cursor.fetchone() == (None,)
            async with database.execute(
                "SELECT category_id FROM denycord_voice_generators WHERE guild_id=1 AND channel_id=10"
            ) as cursor:
                assert await cursor.fetchone() == (None,)

            await denycord_voice_backup.DenycordVoiceBackup.room.callback(cog, ctx)
            ctx.send.assert_awaited()
            assert "<#10>" in ctx.send.await_args.kwargs["embed"].description
        finally:
            await database.close()

    asyncio.run(verify_setup_commands())


def test_voicehub_setup_creates_persistent_shared_interface() -> None:
    import denycord_voice_backup

    class FakeMember:
        def __init__(self, member_id, *, permissions=None):
            self.id = member_id
            self.mention = f"<@{member_id}>"
            self.guild_permissions = permissions or discord.Permissions.none()

        def __str__(self):
            return f"member-{self.id}"

    class FakeChannel:
        def __init__(self, channel_id, name, permissions):
            self.id = channel_id
            self.name = name
            self.mention = f"<#${channel_id}>"
            self.permissions = permissions
            self.messages = []

        def permissions_for(self, _member):
            return self.permissions

        async def send(self, **kwargs):
            self.messages.append(kwargs)
            return SimpleNamespace(id=900 + self.id)

    async def verify_setup() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.execute(
            "CREATE TABLE denycord_voice_settings ("
            "guild_id INTEGER PRIMARY KEY, hub_channel_id INTEGER NOT NULL, "
            "panel_channel_id INTEGER NOT NULL, category_id INTEGER, interface_message_id INTEGER, "
            "enabled INTEGER NOT NULL DEFAULT 1)"
        )
        await database.execute(
            "CREATE TABLE denycord_voice_generators ("
            "guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, category_id INTEGER, "
            "created_by INTEGER, created_at TEXT NOT NULL, PRIMARY KEY(guild_id, channel_id))"
        )
        await database.commit()

        bot_member = FakeMember(
            99,
            permissions=discord.Permissions(manage_channels=True, move_members=True),
        )
        everyone = FakeMember(1)
        everyone.mention = "@everyone"
        channel_permissions = SimpleNamespace(
            view_channel=True,
            connect=True,
            move_members=True,
            send_messages=True,
            embed_links=True,
            attach_files=True,
        )

        class FakeGuild:
            id = 5
            name = "Test Guild"
            icon = None
            default_role = everyone
            me = bot_member

            def __init__(self):
                self.created = []

            async def create_category(self, name, **_kwargs):
                category = SimpleNamespace(id=10, name=name)
                self.created.append(("category", category))
                return category

            async def create_voice_channel(self, name, **_kwargs):
                channel = FakeChannel(11, name, channel_permissions)
                self.created.append(("voice", channel))
                return channel

            async def create_text_channel(self, name, **_kwargs):
                channel = FakeChannel(12, name, channel_permissions)
                self.created.append(("text", channel))
                return channel

        guild = FakeGuild()
        ctx = SimpleNamespace(
            guild=guild,
            author=FakeMember(2),
            send=AsyncMock(),
        )
        cog = denycord_voice_backup.DenycordVoiceBackup(
            SimpleNamespace(db=database, user=None)
        )
        _stub_room_panel_emoji_download(cog, denycord_voice_backup)
        try:
            await denycord_voice_backup.DenycordVoiceBackup.voicehub.callback(
                cog, ctx, "setup", None, None, None
            )
            assert [kind for kind, _channel in guild.created] == ["category", "voice", "text"]
            panel = guild.created[-1][1]
            assert panel.name == "voice-interface"
            assert len(panel.messages) == 1
            panel_view = panel.messages[0]["view"]
            assert isinstance(panel_view, discord.ui.LayoutView)
            assert "embed" not in panel.messages[0]
            assert panel.messages[0]["file"].filename == panel_view.card_filename
            with Image.open(io.BytesIO(panel_view.card_image)) as card_image:
                assert card_image.format == "PNG"
            assert "FALCON VOICE ROOM" in (
                    panel_view.children[0].children[1].items[0].description
            )
            assert sum(
                isinstance(child, discord.ui.Button)
                for child in panel_view.walk_children()
            ) == len(denycord_voice_backup.ROOM_PANEL_BUTTONS)
            async with database.execute(
                "SELECT hub_channel_id, panel_channel_id, interface_message_id "
                "FROM denycord_voice_settings WHERE guild_id=5"
            ) as cursor:
                assert await cursor.fetchone() == (11, 12, 912)
            async with database.execute(
                "SELECT channel_id, category_id, created_by FROM denycord_voice_generators WHERE guild_id=5"
            ) as cursor:
                assert await cursor.fetchone() == (11, 10, 2)
        finally:
            await database.close()

    asyncio.run(verify_setup())


def test_voice_interface_setup_edits_saved_card_instead_of_sending_duplicate() -> None:
    import denycord_voice_backup

    async def verify_update() -> None:
        cog = denycord_voice_backup.DenycordVoiceBackup(SimpleNamespace(user=None))
        existing_message = SimpleNamespace(id=123, edit=AsyncMock())
        panel = SimpleNamespace(
            fetch_message=AsyncMock(return_value=existing_message),
            send=AsyncMock(),
        )
        guild = SimpleNamespace(name="Test Guild", icon=None)
        _stub_room_panel_emoji_download(cog, denycord_voice_backup)

        message_id = await cog._ensure_voice_interface(guild, panel, 123)

        assert message_id == 123
        panel.fetch_message.assert_awaited_once_with(123)
        panel.send.assert_not_awaited()
        existing_message.edit.assert_awaited_once()
        assert "embed" not in existing_message.edit.await_args.kwargs
        assert len(existing_message.edit.await_args.kwargs["attachments"]) == 1
        panel_view = existing_message.edit.await_args.kwargs["view"]
        assert isinstance(panel_view, discord.ui.LayoutView)
        assert "Test Guild" in (
            panel_view.children[0].children[1].items[0].description
        )

    asyncio.run(verify_update())


def test_created_room_interface_is_posted_only_in_voice_chat() -> None:
    import denycord_voice_backup

    class FakeMember:
        def __init__(self, member_id, *, display_name="Member", permissions=None):
            self.id = member_id
            self.display_name = display_name
            self.guild_permissions = permissions or discord.Permissions.none()
            self.voice = None
            self.move_to = AsyncMock()

    async def verify_room_creation() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.executescript(
            """
            CREATE TABLE denycord_voice_settings (
                guild_id INTEGER PRIMARY KEY, hub_channel_id INTEGER NOT NULL,
                panel_channel_id INTEGER NOT NULL, category_id INTEGER, interface_message_id INTEGER
            );
            CREATE TABLE denycord_voice_rooms (
                guild_id INTEGER, channel_id INTEGER, owner_id INTEGER, created_at TEXT,
                locked INTEGER DEFAULT 0, user_limit INTEGER DEFAULT 0, rtc_region TEXT,
                    chat_open INTEGER DEFAULT 1, hidden INTEGER DEFAULT 0, panel_message_id INTEGER,
                PRIMARY KEY(guild_id, channel_id)
            );
            INSERT INTO denycord_voice_settings VALUES (5, 20, 30, NULL, 930);
            """
        )
        bot_member = FakeMember(
            99,
            permissions=discord.Permissions(manage_channels=True, move_members=True),
        )
        everyone = FakeMember(1)
        hub_permissions = SimpleNamespace(
            view_channel=True,
            connect=True,
            move_members=True,
        )
        hub = SimpleNamespace(id=20, permissions_for=lambda _member: hub_permissions)
        room = SimpleNamespace(
            id=40,
            name="Ada's room",
            mention="<#40>",
            guild=None,
            send=AsyncMock(return_value=SimpleNamespace(id=41)),
        )

        class FakeGuild:
            id = 5
            me = bot_member
            default_role = everyone

            def get_channel(self, channel_id):
                return hub if channel_id == 20 else None

            create_voice_channel = AsyncMock(return_value=room)

        guild = FakeGuild()
        room.guild = guild
        member = FakeMember(7, display_name="Ada")
        member.guild = guild
        member.mention = "<@7>"
        bot = SimpleNamespace(db=database, user=None)
        cog = denycord_voice_backup.DenycordVoiceBackup(bot)
        _stub_room_panel_emoji_download(cog, denycord_voice_backup)
        try:
            await cog._create_temp_room(member, None)
            member.move_to.assert_awaited_once_with(
                room, reason="Joined the VoiceMaster create channel"
            )
            room.send.assert_awaited_once()
            panel_view = room.send.await_args.kwargs["view"]
            assert isinstance(panel_view, discord.ui.LayoutView)
            assert room.send.await_args.kwargs["file"].filename == panel_view.card_filename
            assert "Room: Ada's room" in (
                panel_view.children[0].children[1].items[0].description
            )
            assert guild.create_voice_channel.await_count == 1
            assert not cog._rooms_being_created
        finally:
            await database.close()

    asyncio.run(verify_room_creation())


def test_noprefix_success_uses_public_premium_message() -> None:
    import premium_features

    async def verify_noprefix_message() -> None:
        import main as bot_module

        database = await aiosqlite.connect(":memory:")
        await database.execute(
            "CREATE TABLE noprefix (user_id INTEGER PRIMARY KEY, expiry TEXT, granted_by INTEGER, "
            "started_at TEXT, permanent INTEGER)"
        )
        bot = SimpleNamespace(db=database, is_owner=AsyncMock(return_value=True))
        cog = premium_features.PremiumFeatures(bot)
        ctx = SimpleNamespace(
            author=SimpleNamespace(id=1),
            interaction=None,
            send=AsyncMock(),
        )
        user = SimpleNamespace(id=2, mention="<@2>")
        original_parser = premium_features.parse_duration
        premium_features.parse_duration = lambda _duration: None
        try:
            await premium_features.PremiumFeatures.noprefix_add.callback(
                cog, ctx, user, "permanent"
            )
            message = ctx.send.await_args.args[0]
            assert f"{bot_module.FP_TICK}  __**No-Prefix Enabled**__" in message
            assert f"{bot_module.ARROW}  **No-prefix enabled for <@2>**" in message
            assert "> Duration                 Status\n> Permanent          Active" in message
            assert message.endswith("-# Falcon Premium • No-Prefix")
            assert "embed" not in ctx.send.await_args.kwargs
        finally:
            premium_features.parse_duration = original_parser
            await database.close()

    asyncio.run(verify_noprefix_message())


def test_giveaway_heading_is_a_standalone_message() -> None:
    import main as bot_module

    async def verify_giveaway_messages() -> None:
        original_database = bot_module.bot.db
        database = await aiosqlite.connect(":memory:")
        bot_module.bot.db = database
        await database.execute(
            "CREATE TABLE giveaway_settings (guild_id INTEGER PRIMARY KEY, required_role_id INTEGER, "
            "embed_color INTEGER, embed_image_url TEXT, reaction_emoji TEXT)"
        )
        await database.execute(
            "INSERT INTO giveaway_settings VALUES (1, NULL, NULL, 'https://assets.test/banner.gif', '<:wave:111111111111111111>')"
        )
        await database.execute(
            "CREATE TABLE giveaways (msg_id INTEGER, prize TEXT, host_id INTEGER, winners INTEGER, "
            "guild_id INTEGER, channel_id INTEGER, ends_at TEXT, active INTEGER, announcement_msg_id INTEGER)"
        )
        await database.commit()
        giveaway_message = SimpleNamespace(id=123, add_reaction=AsyncMock())
        announcement_message = SimpleNamespace(id=456)
        channel = SimpleNamespace(id=2, send=AsyncMock(return_value=announcement_message))
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1, get_role=lambda _role_id: None),
            channel=channel,
            author=SimpleNamespace(id=3, mention="<@3>"),
            send=AsyncMock(return_value=giveaway_message),
        )
        try:
            await bot_module.gcreate.callback(ctx, "10m", 1, prize="Premium")
            channel.send.assert_awaited_once_with(
                f"{bot_module.GIVEAWAY_EMOJI} **New Giveaway** {bot_module.GIVEAWAY_EMOJI}"
            )
            ctx.send.assert_awaited_once()
            assert ctx.send.await_args.args == ()
            assert ctx.send.await_args.kwargs["embed"].color.value == 0xFFDD00
            assert ctx.send.await_args.kwargs["embed"].image.url == "https://assets.test/banner.gif"
            giveaway_message.add_reaction.assert_awaited_once()
            assert giveaway_message.add_reaction.await_args.args == ("<:wave:111111111111111111>",)
            async with database.execute(
                "SELECT announcement_msg_id FROM giveaways WHERE msg_id=123"
            ) as cursor:
                assert await cursor.fetchone() == (announcement_message.id,)
        finally:
            await database.close()
            bot_module.bot.db = original_database

    asyncio.run(verify_giveaway_messages())


def test_giveaway_expiry_worker_uses_one_second_poll(monkeypatch) -> None:
    import main as bot_module

    async def verify_poll_interval() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.execute(
            "CREATE TABLE giveaways (msg_id INTEGER, channel_id INTEGER, guild_id INTEGER, "
            "active INTEGER, ends_at TEXT)"
        )
        await database.commit()
        state = {"closed": False}
        delays = []

        async def capture_sleep(delay):
            delays.append(delay)
            state["closed"] = True

        client = SimpleNamespace(
            db=database,
            wait_until_ready=AsyncMock(return_value=None),
            is_closed=lambda: state["closed"],
        )
        monkeypatch.setattr(bot_module.asyncio, "sleep", capture_sleep)
        try:
            await bot_module.FalconBot.giveaway_expiry_task(client)
            assert delays == [bot_module.GIVEAWAY_EXPIRY_POLL_INTERVAL]
            assert delays == [1.0]
        finally:
            await database.close()

    asyncio.run(verify_poll_interval())


def test_setgiveaway_saves_emoji_and_uploaded_banner(monkeypatch) -> None:
    import main as bot_module

    async def verify_settings() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.execute(
            "CREATE TABLE giveaway_settings (guild_id INTEGER PRIMARY KEY, winner_role_id INTEGER, "
            "bypass_role_id INTEGER, required_role_id INTEGER, embed_title TEXT, embed_color INTEGER, "
            "embed_image_url TEXT, reaction_emoji TEXT)"
        )
        await database.commit()
        original_database = bot_module.bot.db
        bot_module.bot.db = database
        channel = SimpleNamespace(id=20)
        guild = SimpleNamespace(id=7, get_emoji=lambda _emoji_id: None)
        author = SimpleNamespace(id=3)
        attachment = SimpleNamespace(
            filename="giveaway-banner.gif",
            content_type="image/gif",
            url="https://assets.test/giveaway-banner.gif",
        )
        send_interaction = AsyncMock()
        monkeypatch.setattr(bot_module, "send_interaction_v2", send_interaction)
        ctx = SimpleNamespace(
            guild=guild,
            channel=channel,
            author=author,
            message=SimpleNamespace(attachments=[], role_mentions=[]),
            send=AsyncMock(),
        )
        try:
            await bot_module.setgiveaway.callback(
                ctx, "emoji", value="<:wave:111111111111111111>"
            )
            await bot_module.setgiveaway.callback(ctx, "image")
            prompt_embed = ctx.send.await_args_list[1].kwargs["embed"]
            upload_view = ctx.send.await_args_list[1].kwargs["view"]
            assert prompt_embed.title == "Giveaway Banner"
            assert any(button.label == "Upload Banner" for button in upload_view.children)
            remove_button = next(
                button for button in upload_view.children
                if button.label == "Remove Banner"
            )
            modal = bot_module.GiveawayBannerModal(guild.id, author.id)
            modal_payload = modal.to_components()
            assert modal_payload[0]["type"] == discord.ComponentType.label.value
            assert modal_payload[0]["component"]["type"] == discord.ComponentType.file_upload.value
            assert modal_payload[0]["component"]["required"] is False
            assert modal_payload[1]["type"] == 1
            assert modal_payload[1]["components"][0]["type"] == discord.ComponentType.text_input.value
            modal.banner_upload._values = [attachment]
            await modal.on_submit(SimpleNamespace(user=author))
            send_interaction.assert_awaited_once()
            assert send_interaction.await_args.kwargs["embed"].image.url == attachment.url
            modal.banner_upload._values = []
            modal.banner_url._value = "https://assets.test/from-link.png"
            await modal.on_submit(SimpleNamespace(user=author))
            assert send_interaction.await_count == 2
            assert send_interaction.await_args.kwargs["embed"].image.url == modal.banner_url.value
            modal.banner_url._value = "https://klipy.com/gifs/marody"
            await modal.on_submit(SimpleNamespace(user=author))
            assert send_interaction.await_count == 3
            assert "direct image link" in send_interaction.await_args.args[1]
            assert bot_module._is_direct_giveaway_image_url(
                "https://assets.test/from-link.png?size=large"
            )
            assert not bot_module._is_direct_giveaway_image_url(
                "https://klipy.com/gifs/marody"
            )
            async with database.execute(
                "SELECT embed_image_url, reaction_emoji FROM giveaway_settings WHERE guild_id=7"
            ) as cursor:
                assert await cursor.fetchone() == (
                    "https://assets.test/from-link.png",
                    "<:wave:111111111111111111>",
                )
            await remove_button.callback(SimpleNamespace(user=author))
            assert send_interaction.await_count == 4
            async with database.execute(
                "SELECT embed_image_url FROM giveaway_settings WHERE guild_id=7"
            ) as cursor:
                assert await cursor.fetchone() == (None,)
            await bot_module.setgiveaway.callback(ctx, "emoji")
            assert "Usage: `,setgiveaway emoji <emoji/emoji id>`" in ctx.send.await_args.args[0]
            await bot_module.setgiveaway.callback(ctx, "bypassrole")
            assert "Usage: `,setgiveaway bypassrole <role id>`" in ctx.send.await_args.args[0]
            await bot_module.setgiveaway.callback(ctx, "winnerrole")
            assert "Usage: `,setgiveaway winnerrole <role id>`" in ctx.send.await_args.args[0]
        finally:
            bot_module.bot.db = original_database
            await database.close()

    asyncio.run(verify_settings())


def test_timer_heading_is_plain_message_above_timer_embed(monkeypatch) -> None:
    import main as bot_module

    async def verify_timer_messages() -> None:
        original_bot = bot_module.bot
        previous_timer = bot_module.active_timers.pop(987, None)
        database = await aiosqlite.connect(":memory:")
        await database.execute(
            "CREATE TABLE timers (guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, "
            "label TEXT NOT NULL, end_at TEXT, remaining_seconds INTEGER NOT NULL DEFAULT 0, "
            "paused INTEGER NOT NULL DEFAULT 0)"
        )
        await database.commit()
        task = SimpleNamespace(cancel=lambda: None)

        def schedule(coroutine):
            coroutine.close()
            return task

        bot_module.bot = SimpleNamespace(
            loop=SimpleNamespace(create_task=schedule),
            db=database,
        )
        channel = SimpleNamespace(id=5, send=AsyncMock())
        context = SimpleNamespace(
            guild=SimpleNamespace(id=987),
            channel=channel,
            send=AsyncMock(),
        )
        try:
            await bot_module.tstart.callback(context, "30s", label="Tea")
            timer_emoji = bot_module._manifest_emoji("timer", "⏱️")
            channel.send.assert_awaited_once_with(f"{timer_emoji} **Timer** {timer_emoji}")
            context.send.assert_awaited_once()
            assert context.send.await_args.args == ()
            assert context.send.await_args.kwargs["embed"].description.startswith("**Tea**")
            async with database.execute(
                "SELECT channel_id, label, remaining_seconds, paused FROM timers WHERE guild_id=987"
            ) as cursor:
                assert await cursor.fetchone() == (5, "Tea", 30, 0)
        finally:
            bot_module.active_timers.pop(987, None)
            if previous_timer is not None:
                bot_module.active_timers[987] = previous_timer
            bot_module.bot = original_bot
            await database.close()

    asyncio.run(verify_timer_messages())


def test_active_timers_are_restored_from_persistent_state() -> None:
    import datetime
    import main as bot_module

    async def verify_restore() -> None:
        original_bot = bot_module.bot
        previous_timers = {
            guild_id: bot_module.active_timers.pop(guild_id, None)
            for guild_id in (987, 988)
        }
        database = await aiosqlite.connect(":memory:")
        await database.execute(
            "CREATE TABLE timers (guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, "
            "label TEXT NOT NULL, end_at TEXT, remaining_seconds INTEGER NOT NULL DEFAULT 0, "
            "paused INTEGER NOT NULL DEFAULT 0)"
        )
        deadline = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=90)
        await database.executemany(
            "INSERT INTO timers VALUES (?, ?, ?, ?, ?, ?)",
            [
                (987, 5, "Restored", deadline.isoformat(), 90, 0),
                (988, 6, "Paused", None, 45, 1),
            ],
        )
        await database.commit()
        scheduled = []

        def schedule(coroutine):
            coroutine.close()
            task = SimpleNamespace(cancel=Mock())
            scheduled.append(task)
            return task

        bot_module.bot = SimpleNamespace(
            loop=SimpleNamespace(create_task=schedule),
            db=database,
        )
        try:
            await bot_module.restore_active_timers()
            assert bot_module.active_timers[987]["label"] == "Restored"
            assert bot_module.active_timers[987]["task"] is scheduled[0]
            assert bot_module.active_timers[988]["label"] == "Paused"
            assert bot_module.active_timers[988]["paused"] is True
            assert bot_module.active_timers[988]["remaining"] == 45
            assert bot_module.active_timers[988]["task"] is None
        finally:
            for guild_id, previous in previous_timers.items():
                bot_module.active_timers.pop(guild_id, None)
                if previous is not None:
                    bot_module.active_timers[guild_id] = previous
            bot_module.bot = original_bot
            await database.close()

    asyncio.run(verify_restore())


def test_timer_pause_resume_and_end_update_persistent_state() -> None:
    import datetime
    import main as bot_module

    async def verify_lifecycle() -> None:
        original_bot = bot_module.bot
        previous_timer = bot_module.active_timers.pop(989, None)
        database = await aiosqlite.connect(":memory:")
        await database.execute(
            "CREATE TABLE timers (guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, "
            "label TEXT NOT NULL, end_at TEXT, remaining_seconds INTEGER NOT NULL DEFAULT 0, "
            "paused INTEGER NOT NULL DEFAULT 0)"
        )
        await database.execute(
            "INSERT INTO timers VALUES (989, 5, 'Tea', ?, 60, 0)",
            (
                (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=60))
                .isoformat(),
            ),
        )
        await database.commit()

        def schedule(coroutine):
            coroutine.close()
            return SimpleNamespace(cancel=Mock())

        bot_module.bot = SimpleNamespace(
            loop=SimpleNamespace(create_task=schedule),
            db=database,
        )
        bot_module.active_timers[989] = {
            "task": SimpleNamespace(cancel=Mock()),
            "end_time": datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(seconds=60),
            "paused": False,
            "remaining": 60,
            "channel_id": 5,
            "label": "Tea",
        }
        context = SimpleNamespace(guild=SimpleNamespace(id=989), send=AsyncMock())
        try:
            await bot_module.tpause.callback(context)
            async with database.execute(
                "SELECT end_at, remaining_seconds, paused FROM timers WHERE guild_id=989"
            ) as cursor:
                paused_state = await cursor.fetchone()
            assert paused_state[0] is None
            assert 0 < paused_state[1] <= 60
            assert paused_state[2] == 1

            await bot_module.tresume.callback(context)
            async with database.execute(
                "SELECT end_at, remaining_seconds, paused FROM timers WHERE guild_id=989"
            ) as cursor:
                resumed_state = await cursor.fetchone()
            assert resumed_state[0] is not None
            assert resumed_state[1] == paused_state[1]
            assert resumed_state[2] == 0

            await bot_module.tend.callback(context)
            async with database.execute(
                "SELECT guild_id FROM timers WHERE guild_id=989"
            ) as cursor:
                assert await cursor.fetchone() is None
        finally:
            bot_module.active_timers.pop(989, None)
            if previous_timer is not None:
                bot_module.active_timers[989] = previous_timer
            bot_module.bot = original_bot
            await database.close()

    asyncio.run(verify_lifecycle())


def test_giveaway_end_updates_components_v2_message(monkeypatch) -> None:
    import main as bot_module

    async def verify_giveaway_end_messages() -> None:
        original_bot = bot_module.bot
        database = await aiosqlite.connect(":memory:")
        await database.executescript(
            """
            CREATE TABLE giveaways (
                msg_id INTEGER, forced_winner INTEGER, prize TEXT, host_id INTEGER,
                winners INTEGER, guild_id INTEGER, active INTEGER, announcement_msg_id INTEGER
            );
            CREATE TABLE giveaway_settings (
                guild_id INTEGER, winner_role_id INTEGER, bypass_role_id INTEGER,
                required_role_id INTEGER, embed_title TEXT, embed_color INTEGER,
                embed_image_url TEXT, reaction_emoji TEXT
            );
            INSERT INTO giveaways VALUES (123, NULL, 'Premium', 3, 1, 1, 1, 456);
            """
        )
        message = SimpleNamespace(
            reactions=[],
            jump_url="https://discord.test/giveaway/123",
        )
        announcement = SimpleNamespace(id=456, edit=AsyncMock())

        async def fetch_message(message_id):
            return message if message_id == 123 else announcement

        guild = SimpleNamespace(id=1, get_role=lambda _role_id: None)
        channel = SimpleNamespace(
            id=2,
            guild=guild,
            fetch_message=AsyncMock(side_effect=fetch_message),
            send=AsyncMock(),
        )
        bot_module.bot = SimpleNamespace(db=database, get_channel=lambda _channel_id: channel)
        edit_message_v2 = AsyncMock()
        monkeypatch.setattr(bot_module, "edit_message_v2", edit_message_v2)
        try:
            await asyncio.gather(
                bot_module.resolve_giveaway(123, 2),
                bot_module.resolve_giveaway(123, 2),
            )
            channel.send.assert_not_awaited()
            announcement.edit.assert_awaited_once_with(
                content=(
                    f"{bot_module.GIVEAWAY_ENDED_EMOJI} "
                    f"**Giveaway Ended** {bot_module.GIVEAWAY_ENDED_EMOJI}"
                )
            )
            edit_message_v2.assert_awaited_once()
            assert edit_message_v2.await_args.args == (message,)
            description = edit_message_v2.await_args.kwargs["embed"].description
            assert "Giveaway Ended" not in description
            assert f"{bot_module.DOT} Hosted by:" in description
            assert f"{bot_module.FP_TICK} **Winner(s):**" in description
        finally:
            await database.close()
            bot_module.bot = original_bot

    asyncio.run(verify_giveaway_end_messages())


def test_winner_notification_replies_to_giveaway(monkeypatch) -> None:
    import main as bot_module

    async def verify_winner_reply() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.executescript(
            """
            CREATE TABLE giveaways (
                msg_id INTEGER, forced_winner INTEGER, prize TEXT, host_id INTEGER,
                winners INTEGER, guild_id INTEGER, active INTEGER, announcement_msg_id INTEGER
            );
            CREATE TABLE giveaway_settings (
                guild_id INTEGER, winner_role_id INTEGER, bypass_role_id INTEGER,
                required_role_id INTEGER, embed_title TEXT, embed_color INTEGER,
                embed_image_url TEXT, reaction_emoji TEXT
            );
            INSERT INTO giveaways VALUES (123, 7, '100', 7, 1, 1, 1, 456);
            """
        )
        winner = SimpleNamespace(id=7, mention="<@7>")
        giveaway_message = SimpleNamespace(reactions=[], jump_url="https://discord.test/giveaway/123")
        announcement = SimpleNamespace(id=456, edit=AsyncMock())

        async def fetch_message(message_id):
            return giveaway_message if message_id == 123 else announcement

        channel = SimpleNamespace(
            guild=SimpleNamespace(id=1, get_role=lambda _role_id: None, get_member=lambda _user_id: winner),
            fetch_message=AsyncMock(side_effect=fetch_message),
            send=AsyncMock(),
        )
        original_bot = bot_module.bot
        bot_module.bot = SimpleNamespace(
            db=database,
            user=SimpleNamespace(id=99),
            get_channel=lambda _channel_id: channel,
        )
        send = AsyncMock()
        monkeypatch.setattr(bot_module, "send_v2", send)
        monkeypatch.setattr(bot_module, "edit_message_v2", AsyncMock())
        try:
            await bot_module.resolve_giveaway(123, 2)
            send.assert_awaited_once()
            assert send.await_args.args[0] is channel
            assert send.await_args.args[1] == "Congrats, <@7> you have won **100**, hosted by <@7>"
            assert send.await_args.kwargs["reference"] is giveaway_message
            assert send.await_args.kwargs["mention_author"] is False
            button = send.await_args.kwargs["view"].children[0]
            assert button.label == "Giveaway Link"
            assert button.url == giveaway_message.jump_url
        finally:
            bot_module.bot = original_bot
            await database.close()

    asyncio.run(verify_winner_reply())


def test_legacy_giveaway_end_updates_previous_header(monkeypatch) -> None:
    import main as bot_module

    async def verify_legacy_reply() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.executescript(
            """
            CREATE TABLE giveaways (
                msg_id INTEGER, forced_winner INTEGER, prize TEXT, host_id INTEGER,
                winners INTEGER, guild_id INTEGER, active INTEGER, announcement_msg_id INTEGER
            );
            CREATE TABLE giveaway_settings (
                guild_id INTEGER, winner_role_id INTEGER, bypass_role_id INTEGER,
                required_role_id INTEGER, embed_title TEXT, embed_color INTEGER,
                embed_image_url TEXT, reaction_emoji TEXT
            );
            INSERT INTO giveaways VALUES (321, NULL, 'Prize', 3, 1, 1, 1, NULL);
            """
        )
        giveaway_message = SimpleNamespace(reactions=[], jump_url="https://discord.test/giveaway/321")
        header = SimpleNamespace(
            content=f"{bot_module.GIVEAWAY_EMOJI} **New Giveaway** {bot_module.GIVEAWAY_EMOJI}",
            author=SimpleNamespace(id=99),
            edit=AsyncMock(),
        )

        async def history(*, limit, before):
            assert limit == 5
            assert before is giveaway_message
            yield header

        async def fetch_message(message_id):
            assert message_id == 321
            return giveaway_message

        guild = SimpleNamespace(id=1, get_role=lambda _role_id: None)
        channel = SimpleNamespace(
            id=2,
            guild=guild,
            history=history,
            fetch_message=AsyncMock(side_effect=fetch_message),
            send=AsyncMock(),
        )
        original_bot = bot_module.bot
        bot_module.bot = SimpleNamespace(
            db=database,
            user=SimpleNamespace(id=99),
            get_channel=lambda _channel_id: channel,
        )
        monkeypatch.setattr(bot_module, "edit_message_v2", AsyncMock())
        try:
            await bot_module.resolve_giveaway(321, 2)
            channel.send.assert_not_awaited()
            header.edit.assert_awaited_once_with(
                content=(
                    f"{bot_module.GIVEAWAY_ENDED_EMOJI} "
                    f"**Giveaway Ended** {bot_module.GIVEAWAY_ENDED_EMOJI}"
                )
            )
        finally:
            bot_module.bot = original_bot
            await database.close()

    asyncio.run(verify_legacy_reply())


def test_edit_message_v2_does_not_send_legacy_embeds_for_v2_messages() -> None:
    from components_v2 import edit_message_v2

    message = SimpleNamespace(
        flags=SimpleNamespace(is_components_v2=True),
        edit=AsyncMock(),
    )

    asyncio.run(edit_message_v2(message, embed=discord.Embed(description="Updated")))

    message.edit.assert_awaited_once()
    assert "embeds" not in message.edit.await_args.kwargs
    assert isinstance(message.edit.await_args.kwargs["view"], discord.ui.LayoutView)


def test_edit_message_v2_disables_view_without_legacy_embeds() -> None:
    from components_v2 import edit_message_v2

    message = SimpleNamespace(
        flags=SimpleNamespace(is_components_v2=True),
        edit=AsyncMock(),
    )

    asyncio.run(edit_message_v2(message, view=discord.ui.View()))

    message.edit.assert_awaited_once()
    assert "embeds" not in message.edit.await_args.kwargs
    assert isinstance(message.edit.await_args.kwargs["view"], discord.ui.LayoutView)


def test_edit_message_v2_layout_view_omits_legacy_fields_for_v2_message() -> None:
    from components_v2 import edit_message_v2

    message = SimpleNamespace(
        flags=SimpleNamespace(is_components_v2=True),
        edit=AsyncMock(),
    )
    layout = discord.ui.LayoutView()

    asyncio.run(edit_message_v2(message, view=layout))

    message.edit.assert_awaited_once_with(view=layout)


def test_edit_message_v2_layout_view_clears_legacy_fields_for_legacy_message() -> None:
    from components_v2 import edit_message_v2

    message = SimpleNamespace(
        flags=SimpleNamespace(is_components_v2=False),
        edit=AsyncMock(),
    )
    layout = discord.ui.LayoutView()

    asyncio.run(edit_message_v2(message, view=layout))

    message.edit.assert_awaited_once_with(content=None, embeds=[], view=layout)
    assert layout._v2_message is message


def test_edit_interaction_v2_omits_legacy_fields_for_v2_message() -> None:
    from components_v2 import edit_interaction_v2

    message = SimpleNamespace(flags=SimpleNamespace(is_components_v2=True))
    response = SimpleNamespace(edit_message=AsyncMock())
    interaction = SimpleNamespace(message=message, response=response)

    asyncio.run(
        edit_interaction_v2(
            interaction,
            embed=discord.Embed(description="Updated"),
            view=discord.ui.View(),
        )
    )

    response.edit_message.assert_awaited_once()
    kwargs = response.edit_message.await_args.kwargs
    assert "embed" not in kwargs
    assert "embeds" not in kwargs
    assert "content" not in kwargs
    assert isinstance(kwargs["view"], discord.ui.LayoutView)
    assert kwargs["view"]._v2_message is message


def test_components_v2_places_embed_thumbnail_on_right() -> None:
    from components_v2 import make_layout_view

    embed = discord.Embed(title="Security event", description="Details")
    embed.set_thumbnail(url="https://assets.test/bot-avatar.png")
    layout = make_layout_view(embed=embed)
    section = layout.children[0].children[0]

    assert isinstance(section, discord.ui.Section)
    assert isinstance(section.accessory, discord.ui.Thumbnail)
    assert section.accessory.media.url == "https://assets.test/bot-avatar.png"
    assert not any(isinstance(item, discord.ui.MediaGallery) for item in layout.children[0].children)


def test_components_v2_places_buttons_outside_embed_container() -> None:
    from components_v2 import make_layout_view

    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Open", style=discord.ButtonStyle.secondary))
    layout = make_layout_view(embed=discord.Embed(title="Example"), legacy_view=view)

    assert isinstance(layout.children[0], discord.ui.Container)
    assert isinstance(layout.children[1], discord.ui.ActionRow)
    assert layout.children[1].children[0].label == "Open"


def test_steal_menu_extracts_and_processes_five_modes(monkeypatch) -> None:
    import main as bot_module

    source = SimpleNamespace(
        content="<:wave:111111111111111111> <:heart:222222222222222222>",
        stickers=[
            SimpleNamespace(name="sticker_one", url="https://assets.test/one.png", format=SimpleNamespace(name="png"), description="", emoji="🙂"),
            SimpleNamespace(name="sticker_two", url="https://assets.test/two.png", format=SimpleNamespace(name="png"), description="", emoji="🙂"),
        ],
        attachments=[
            SimpleNamespace(filename="image.png", url="https://assets.test/image.png", content_type="image/png")
        ],
        embeds=[
            SimpleNamespace(
                image=SimpleNamespace(url="https://assets.test/preview.gif?width=400"),
                thumbnail=None,
                video=None,
            )
        ],
        author=SimpleNamespace(id=1),
    )

    async def verify_modes() -> None:
        monkeypatch.setattr(bot_module, "_download_steal_asset", AsyncMock(return_value=b"image"))
        assets = bot_module._steal_message_assets(source)
        assert tuple(map(len, assets)) == (2, 2, 2, 2)

        expected_counts = {
            "emoji_one": (1, 0),
            "sticker_one": (0, 1),
            "all_emoji": (4, 0),
            "all_stickers": (0, 4),
            "both": (4, 4),
        }
        for mode, expected in expected_counts.items():
            guild = SimpleNamespace(
                emojis=[],
                stickers=[],
                filesize_limit=1024,
                create_custom_emoji=AsyncMock(),
                create_sticker=AsyncMock(),
            )
            added, failures, created_assets = await bot_module._steal_selected_assets(guild, source, mode)
            assert (guild.create_custom_emoji.await_count, guild.create_sticker.await_count) == expected
            assert added == sum(expected)
            assert failures == []
            assert len(created_assets) == added

        class FakeEmoji:
            name = "wave"
            url = "https://assets.test/wave.gif"

            def __str__(self):
                return "<:wave:123>"

        result_embed = bot_module._build_steal_result_embed(
            [("emoji", FakeEmoji()),
             ("sticker", SimpleNamespace(name="dance", url="https://assets.test/dance.gif"))],
            [],
        )
        assert "<" in result_embed.fields[0].value
        assert result_embed.fields[1].name == "Sticker"
        assert result_embed.image.url == "https://assets.test/wave.gif"

    asyncio.run(verify_modes())


def test_oversized_gif_is_compressed_and_stays_animated() -> None:
    import io

    from PIL import Image

    import main as bot_module

    frames = [Image.effect_noise((384, 384), 100).convert("RGB") for _ in range(4)]
    source = io.BytesIO()
    frames[0].save(
        source,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=100,
        loop=0,
    )
    for frame in frames:
        frame.close()
    original = source.getvalue()
    compressed = bot_module._compress_steal_gif(original, max(1, len(original) // 3))

    assert len(compressed) <= len(original) // 3
    with Image.open(io.BytesIO(compressed)) as optimized:
        assert optimized.n_frames > 1


def test_oversized_png_is_resized_and_remains_png() -> None:
    import io

    from PIL import Image

    import main as bot_module

    image = Image.effect_noise((1024, 1024), 100).convert("RGB")
    source = io.BytesIO()
    image.save(source, format="PNG")
    image.close()
    compressed = bot_module._compress_steal_image(source.getvalue(), 128 * 1024, ".png")

    assert len(compressed) <= 128 * 1024
    with Image.open(io.BytesIO(compressed)) as optimized:
        assert optimized.format == "PNG"
        assert optimized.width < 1024


def test_steal_upload_limits_account_for_emoji_base64() -> None:
    import main as bot_module

    guild = SimpleNamespace(filesize_limit=100 * 1024 * 1024)

    assert bot_module._steal_upload_limit(guild) == 23 * 1024 * 1024
    assert bot_module._steal_upload_limit(guild, for_sticker=True) == 31 * 1024 * 1024
    guild.filesize_limit = 8 * 1024 * 1024
    assert bot_module._steal_upload_limit(guild) == 8 * 1024 * 1024


def test_steal_command_has_five_menu_options() -> None:
    import main as bot_module

    source = SimpleNamespace(author=SimpleNamespace(mention="<@1>"))
    view = bot_module.StealMenuView(source, invoker_id=1)
    select = view.children[0]

    assert [option.value for option in select.options] == [
        "emoji_one", "sticker_one", "all_emoji", "all_stickers", "both"
    ]
    assert bot_module.bot.get_command("steal") is not None
    assert "steal" not in bot_module.bot.get_command("emoji").aliases


def test_steal_source_resolves_reply_and_mentioned_user() -> None:
    import main as bot_module

    async def verify_sources() -> None:
        reply_source = SimpleNamespace(
            content="",
            stickers=[],
            attachments=[],
            embeds=[
                SimpleNamespace(
                    image=SimpleNamespace(url="https://assets.test/reply.gif"),
                    thumbnail=None,
                    video=None,
                )
            ],
        )
        reply_context = SimpleNamespace(
            message=SimpleNamespace(
                reference=SimpleNamespace(resolved=reply_source), mentions=[]
            ),
            channel=SimpleNamespace(),
        )
        assert await bot_module._find_steal_source(reply_context) is reply_source

        mentioned_user = SimpleNamespace(id=7)
        mentioned_source = SimpleNamespace(
            content="<:wave:111111111111111111>", stickers=[], attachments=[], author=mentioned_user
        )

        async def history(limit):
            assert limit == 50
            yield mentioned_source

        mention_context = SimpleNamespace(
            message=SimpleNamespace(reference=None, mentions=[mentioned_user]),
            channel=SimpleNamespace(history=history),
        )
        assert await bot_module._find_steal_source(mention_context) is mentioned_source

    asyncio.run(verify_sources())


def test_premium_gift_requires_existing_premium() -> None:
    from premium_features import PremiumFeatures

    async def verify_denied_gift() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.executescript(
            """
            CREATE TABLE premium_allocations (
                allocation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL, granted_by INTEGER NOT NULL,
                started_at TEXT NOT NULL, expires_at TEXT,
                guild_limit INTEGER NOT NULL, status TEXT NOT NULL
            );
            CREATE TABLE premium_activations (
                allocation_id INTEGER NOT NULL, guild_id INTEGER NOT NULL,
                owner_id INTEGER NOT NULL, status TEXT NOT NULL
            );
            CREATE TABLE premium_guilds (
                guild_id INTEGER PRIMARY KEY, allocation_id INTEGER, owner_id INTEGER
            );
            CREATE TABLE premium_users (user_id INTEGER PRIMARY KEY, expiry TEXT);
            """
        )
        feature = PremiumFeatures(SimpleNamespace(db=database, owner_ids={1}))
        feature._is_manager = lambda _user_id: True
        target = SimpleNamespace(id=123456789012345, mention="<@123456789012345>")
        feature._resolve_user = AsyncMock(return_value=target)
        context = SimpleNamespace(
            author=SimpleNamespace(id=1),
            interaction=None,
            send=AsyncMock(),
        )
        try:
            await PremiumFeatures.premium_gift.callback(feature, context, str(target.id))
            async with database.execute(
                "SELECT COUNT(*) FROM premium_allocations"
            ) as cursor:
                assert await cursor.fetchone() == (0,)
            assert "active Premium" in context.send.await_args.args[0]
            context.send.assert_awaited_once()
        finally:
            await database.close()

    asyncio.run(verify_denied_gift())


def test_premium_user_gift_transfers_all_allocations_and_removes_access() -> None:
    import datetime

    from premium_features import PremiumFeatures, utc_now

    async def verify_gift_transfer() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.executescript(
            """
            CREATE TABLE premium_allocations (
                allocation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL, granted_by INTEGER NOT NULL,
                started_at TEXT NOT NULL, expires_at TEXT,
                guild_limit INTEGER NOT NULL, status TEXT NOT NULL
            );
            CREATE TABLE premium_activations (
                allocation_id INTEGER NOT NULL, guild_id INTEGER NOT NULL,
                owner_id INTEGER NOT NULL, status TEXT NOT NULL
            );
            CREATE TABLE premium_guilds (
                guild_id INTEGER PRIMARY KEY, allocation_id INTEGER, owner_id INTEGER
            );
            CREATE TABLE premium_users (user_id INTEGER PRIMARY KEY, expiry TEXT);
            """
        )
        expiry = (utc_now() + datetime.timedelta(days=45)).isoformat()
        later_expiry = (utc_now() + datetime.timedelta(days=90)).isoformat()
        await database.execute(
            "INSERT INTO premium_allocations "
            "(user_id, granted_by, started_at, expires_at, guild_limit, status) "
            "VALUES (1, 9, ?, ?, 2, 'active')",
            (utc_now().isoformat(), expiry),
        )
        await database.execute(
            "INSERT INTO premium_allocations "
            "(user_id, granted_by, started_at, expires_at, guild_limit, status) "
            "VALUES (1, 9, ?, ?, 1, 'active')",
            (utc_now().isoformat(), later_expiry),
        )
        await database.execute(
            "INSERT INTO premium_activations VALUES (2, 71, 1, 'active')"
        )
        await database.execute(
            "INSERT INTO premium_guilds VALUES (71, 2, 1)"
        )
        await database.execute(
            "INSERT INTO premium_users (user_id, expiry) VALUES (1, ?)",
            (later_expiry,),
        )
        await database.commit()
        feature = PremiumFeatures(SimpleNamespace(db=database, owner_ids=set()))
        target = SimpleNamespace(id=2, mention="<@2>")
        feature._resolve_user = AsyncMock(return_value=target)
        context = SimpleNamespace(
            author=SimpleNamespace(id=1),
            interaction=None,
            send=AsyncMock(),
        )
        try:
            await PremiumFeatures.premium_gift.callback(feature, context, target)
            async with database.execute(
                "SELECT allocation_id, user_id, granted_by "
                "FROM premium_allocations ORDER BY allocation_id"
            ) as cursor:
                assert await cursor.fetchall() == [(1, 2, 1), (2, 2, 1)]
            async with database.execute(
                "SELECT allocation_id, owner_id FROM premium_activations"
            ) as cursor:
                assert await cursor.fetchall() == [(2, 2)]
            async with database.execute(
                "SELECT guild_id, owner_id FROM premium_guilds"
            ) as cursor:
                assert await cursor.fetchall() == [(71, 2)]
            async with database.execute(
                "SELECT user_id, expiry FROM premium_users ORDER BY user_id"
            ) as cursor:
                assert await cursor.fetchall() == [(2, later_expiry)]
            context.send.assert_awaited_once()
            description = context.send.await_args.kwargs["embed"].description
            assert "2** active Premium allocation(s)" in description
            assert "Your Premium access has been removed." in description
        finally:
            await database.close()

    asyncio.run(verify_gift_transfer())


def test_legacy_premium_entitlement_can_be_gifted() -> None:
    import datetime

    from premium_features import PremiumFeatures, utc_now

    async def verify_legacy_transfer() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.executescript(
            """
            CREATE TABLE premium_allocations (
                allocation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL, granted_by INTEGER NOT NULL,
                started_at TEXT NOT NULL, expires_at TEXT,
                guild_limit INTEGER NOT NULL, status TEXT NOT NULL
            );
            CREATE TABLE premium_activations (
                allocation_id INTEGER NOT NULL, guild_id INTEGER NOT NULL,
                owner_id INTEGER NOT NULL, status TEXT NOT NULL
            );
            CREATE TABLE premium_guilds (
                guild_id INTEGER PRIMARY KEY, allocation_id INTEGER, owner_id INTEGER
            );
            CREATE TABLE premium_users (user_id INTEGER PRIMARY KEY, expiry TEXT);
            """
        )
        expiry = (utc_now() + datetime.timedelta(days=30)).isoformat()
        await database.execute(
            "INSERT INTO premium_users (user_id, expiry) VALUES (1, ?)",
            (expiry,),
        )
        await database.commit()
        feature = PremiumFeatures(SimpleNamespace(db=database, owner_ids=set()))
        target = SimpleNamespace(id=2, mention="<@2>")
        feature._resolve_user = AsyncMock(return_value=target)
        context = SimpleNamespace(
            author=SimpleNamespace(id=1),
            interaction=None,
            send=AsyncMock(),
        )
        try:
            await PremiumFeatures.premium_gift.callback(feature, context, target)
            async with database.execute(
                "SELECT user_id, granted_by, expires_at FROM premium_allocations"
            ) as cursor:
                assert await cursor.fetchone() == (2, 1, expiry)
            async with database.execute(
                "SELECT user_id, expiry FROM premium_users ORDER BY user_id"
            ) as cursor:
                assert await cursor.fetchall() == [(2, expiry)]
            assert "Your Premium access has been removed" in (
                context.send.await_args.kwargs["embed"].description
            )
        finally:
            await database.close()

    asyncio.run(verify_legacy_transfer())


def test_nonpremium_user_cannot_gift_premium() -> None:
    from premium_features import PremiumFeatures

    async def verify_gift_denied() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.executescript(
            """
            CREATE TABLE premium_allocations (
                allocation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL, granted_by INTEGER NOT NULL,
                started_at TEXT NOT NULL, expires_at TEXT,
                guild_limit INTEGER NOT NULL, status TEXT NOT NULL
            );
            CREATE TABLE premium_activations (
                allocation_id INTEGER NOT NULL, guild_id INTEGER NOT NULL,
                owner_id INTEGER NOT NULL, status TEXT NOT NULL
            );
            CREATE TABLE premium_guilds (
                guild_id INTEGER PRIMARY KEY, allocation_id INTEGER, owner_id INTEGER
            );
            CREATE TABLE premium_users (user_id INTEGER PRIMARY KEY, expiry TEXT);
            """
        )
        feature = PremiumFeatures(SimpleNamespace(db=database, owner_ids=set()))
        target = SimpleNamespace(id=2, mention="<@2>")
        feature._resolve_user = AsyncMock(return_value=target)
        context = SimpleNamespace(
            author=SimpleNamespace(id=1),
            interaction=None,
            send=AsyncMock(),
        )
        try:
            await PremiumFeatures.premium_gift.callback(feature, context, target)
            async with database.execute(
                "SELECT COUNT(*) FROM premium_allocations"
            ) as cursor:
                assert await cursor.fetchone() == (0,)
            assert "active Premium" in context.send.await_args.args[0]
        finally:
            await database.close()

    asyncio.run(verify_gift_denied())


def test_interaction_responses_are_public() -> None:
    from components_v2 import send_interaction_v2, send_v2

    async def verify_visibility() -> None:
        destination = SimpleNamespace(send=AsyncMock())
        await send_v2(destination, "public", ephemeral=True)
        assert destination.send.call_args.kwargs.get("ephemeral", False) is False

        response = SimpleNamespace(send_message=AsyncMock(), is_done=lambda: False)
        interaction = SimpleNamespace(response=response)
        await send_interaction_v2(interaction, "public", ephemeral=True)
        assert response.send_message.call_args.kwargs["ephemeral"] is False
        await send_interaction_v2(interaction, "private", ephemeral=True, allow_ephemeral=True)
        assert response.send_message.call_args.kwargs["ephemeral"] is True

    asyncio.run(verify_visibility())


def test_embed_builder_refreshes_existing_preview(monkeypatch) -> None:
    import community_features

    monkeypatch.setattr(
        community_features,
        "build_custom_embed",
        lambda *_args: discord.Embed(title="Updated preview"),
    )

    async def verify_refresh() -> None:
        cog = SimpleNamespace(
            bot=SimpleNamespace(user=None),
            save_embed=AsyncMock(),
        )
        view = community_features.AdvancedEmbedBuilderView(
            cog,
            1,
            "sample",
            owner_id=2,
        )
        assert [item.label for item in view.children] == [
            "Edit Basic Information",
            "Edit Author",
            "Edit Footer",
            "Edit Images",
            "Create Button",
            "Remove Button",
            "Need help? Join the server",
        ]
        assert all(item.emoji is None for item in view.children)
        assert all(item.style is discord.ButtonStyle.secondary for item in view.children[:5])
        component_interaction = SimpleNamespace(
            user=SimpleNamespace(id=2),
            guild=SimpleNamespace(),
            channel=SimpleNamespace(),
            message=SimpleNamespace(flags=SimpleNamespace(is_components_v2=True)),
            response=SimpleNamespace(edit_message=AsyncMock()),
        )
        await view._refresh_preview(component_interaction)
        cog.save_embed.assert_awaited_once_with(1, "sample", view.config, 2)
        component_interaction.response.edit_message.assert_awaited_once()
        interaction_kwargs = component_interaction.response.edit_message.await_args.kwargs
        assert "embeds" not in interaction_kwargs
        assert isinstance(interaction_kwargs["view"], discord.ui.LayoutView)

        preview_message = SimpleNamespace(
            flags=SimpleNamespace(is_components_v2=True),
            edit=AsyncMock(),
        )
        view.message = preview_message
        modal_interaction = SimpleNamespace(
            user=SimpleNamespace(id=2),
            guild=SimpleNamespace(),
            channel=SimpleNamespace(),
            message=None,
            response=SimpleNamespace(defer=AsyncMock()),
            delete_original_response=AsyncMock(),
        )
        await view._refresh_preview(modal_interaction)
        assert cog.save_embed.await_count == 2
        modal_interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        preview_message.edit.assert_awaited_once()
        message_kwargs = preview_message.edit.await_args.kwargs
        assert "embeds" not in message_kwargs
        assert isinstance(message_kwargs["view"], discord.ui.LayoutView)
        modal_interaction.delete_original_response.assert_awaited_once()

    asyncio.run(verify_refresh())


def test_embed_builder_buttons_open_grouped_forms() -> None:
    import community_features

    async def verify_forms() -> None:
        view = community_features.AdvancedEmbedBuilderView(
            SimpleNamespace(bot=SimpleNamespace(user=None)),
            1,
            "sample",
            owner_id=2,
        )
        interaction = SimpleNamespace(response=SimpleNamespace(send_modal=AsyncMock()))
        expected = [
            ("Edit Basic Information", ["title", "description", "color"]),
            ("Edit Author", ["name", "icon_url"]),
            ("Edit Footer", ["text", "icon_url", "timestamp"]),
            ("Edit Images", ["image", "thumbnail"]),
            ("Create Link Button", ["label", "url"]),
        ]

        for button, (title, input_keys) in zip(view.children[:5], expected):
            await button.callback(interaction)
            modal = interaction.response.send_modal.await_args.args[0]
            assert modal.title == title
            assert list(modal.inputs) == input_keys
            interaction.response.send_modal.reset_mock()

    asyncio.run(verify_forms())


def test_prefix_embed_create_shows_only_success_embed(monkeypatch) -> None:
    import community_features

    message = object()
    cog = community_features.CommunityFeatures(SimpleNamespace(user=None))
    channel = SimpleNamespace(send=AsyncMock(return_value=message))
    ctx = SimpleNamespace(
        guild=SimpleNamespace(id=1),
        author=SimpleNamespace(id=42),
        channel=channel,
        interaction=None,
    )

    async def verify_intro() -> None:
        builder = await cog._open_embed_modal(ctx, "shivay", introduction=True)
        sent = channel.send.await_args.kwargs
        assert sent["content"] == community_features.embed_creation_announcement("shivay")
        assert sent["embed"].description == (
            "<:start:1561337010806071387> **successfully created an embed called: `shivay`**\n"
            "please select from the buttons below for what you'd like to edit!\n"
            "alternatively, you can edit these individually in slash commands with `/embed edit`."
        )
        assert sent["embed"].color.value == 0xFFDD00
        assert sent["view"] is builder
        assert builder.message is message

    asyncio.run(verify_intro())


def test_embed_builder_shows_custom_links_below_controls() -> None:
    import community_features

    config = dict(community_features.DEFAULT_EMBED_CONFIG)
    config["buttons"] = [
        {"label": "Website", "url": "https://example.com"},
        {"label": "Rules", "url": "https://example.com/rules"},
    ]
    view = community_features.AdvancedEmbedBuilderView(
        SimpleNamespace(bot=SimpleNamespace(user=None)),
        1,
        "sample",
        config,
        owner_id=2,
    )

    assert len(view.children) == 9
    assert [item.label for item in view.custom_button_items] == ["Website", "Rules"]
    assert all(item.emoji is None for item in view.custom_button_items)
    assert [item.row for item in view.custom_button_items] == [1, 1]
    assert [item.url for item in view.custom_button_items] == [
        "https://example.com",
        "https://example.com/rules",
    ]

    full_config = dict(community_features.DEFAULT_EMBED_CONFIG)
    full_config["buttons"] = [
        {"label": f"Button {index}", "url": f"https://example.com/{index}"}
        for index in range(18)
    ]
    full_view = community_features.AdvancedEmbedBuilderView(
        SimpleNamespace(bot=SimpleNamespace(user=None)),
        1,
        "full",
        full_config,
        owner_id=2,
    )
    assert len(full_view.children) == 25
    assert [sum(item.row == row for item in full_view.children) for row in range(5)] == [
        5, 5, 5, 5, 5
    ]


def test_embed_builder_removes_button_by_name(monkeypatch) -> None:
    import community_features

    config = dict(community_features.DEFAULT_EMBED_CONFIG)
    config["buttons"] = [
        {"label": "Website", "url": "https://example.com"},
        {"label": "Rules", "url": "https://example.com/rules"},
    ]
    cog = SimpleNamespace(
        bot=SimpleNamespace(user=None),
        save_embed=AsyncMock(),
    )
    view = community_features.AdvancedEmbedBuilderView(cog, 1, "sample", config, owner_id=42)
    monkeypatch.setattr(
        community_features,
        "build_custom_embed",
        lambda *_args: discord.Embed(title="Updated preview"),
    )
    edit_preview = AsyncMock()
    monkeypatch.setattr(community_features, "edit_interaction_v2", edit_preview)
    send_error = AsyncMock()
    monkeypatch.setattr(community_features, "send_interaction_v2", send_error)
    interaction = SimpleNamespace(
        response=SimpleNamespace(send_modal=AsyncMock()),
        user=community_features._SampleMember(),
        guild=community_features._SampleGuild(),
        channel=None,
        message=object(),
    )

    async def verify_remove() -> None:
        button = next(item for item in view.children if item.label == "Remove Button")
        await button.callback(interaction)
        modal = interaction.response.send_modal.await_args.args[0]
        assert modal.title == "Remove Link Button"
        modal.inputs["label"]._value = "missing"
        await modal.on_submit(interaction)
        send_error.assert_awaited_once()
        assert [item.label for item in view.custom_button_items] == ["Website", "Rules"]

        modal.inputs["label"]._value = "rules"
        await modal.on_submit(interaction)
        assert [item.label for item in view.custom_button_items] == ["Website"]
        assert view.config["buttons"] == [
            {"label": "Website", "url": "https://example.com"}
        ]
        cog.save_embed.assert_awaited_once()
        edit_preview.assert_awaited_once()

    asyncio.run(verify_remove())


def test_slash_embed_create_sends_success_embed_with_separate_controls(monkeypatch) -> None:
    import community_features

    message = object()
    response = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(resource=message))
    )
    followup = SimpleNamespace(send=AsyncMock(return_value=message))
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=1),
        user=SimpleNamespace(id=42),
        channel=SimpleNamespace(),
        response=response,
        followup=followup,
    )
    ctx = SimpleNamespace(
        guild=interaction.guild,
        author=interaction.user,
        channel=interaction.channel,
        interaction=interaction,
    )
    monkeypatch.setattr(
        community_features,
        "build_custom_embed",
        lambda *_args: discord.Embed(title="Preview"),
    )
    cog = community_features.CommunityFeatures(SimpleNamespace(user=None))

    async def verify_slash_flow() -> None:
        await cog._open_embed_modal(ctx, "shivvay", introduction=True)

        followup.send.assert_not_awaited()
        sent = response.send_message.await_args.kwargs
        assert sent["content"] == community_features.embed_creation_announcement("shivvay")
        assert sent["embed"].description.startswith(
            "<:start:1561337010806071387> **successfully created an embed called: `shivvay`**"
        )
        assert isinstance(sent["view"], community_features.AdvancedEmbedBuilderView)
        assert sent["view"].message is message
        assert sent["ephemeral"] is True

    asyncio.run(verify_slash_flow())


def test_embed_color_change_updates_preview_accent_immediately(monkeypatch) -> None:
    import community_features

    cog = community_features.CommunityFeatures(SimpleNamespace(user=None))
    cog.save_embed = AsyncMock()
    view = community_features.AdvancedEmbedBuilderView(
        cog,
        1,
        "sample",
        owner_id=42,
    )
    open_interaction = SimpleNamespace(response=SimpleNamespace(send_modal=AsyncMock()))
    edit_message = AsyncMock()

    async def verify_color_update() -> None:
        button = next(item for item in view.children if item.label == "Edit Basic Information")
        await button.callback(open_interaction)
        modal = open_interaction.response.send_modal.await_args.args[0]
        modal.inputs["title"]._value = "Color test"
        modal.inputs["color"]._value = "#12AB34"
        submit_interaction = SimpleNamespace(
            user=community_features._SampleMember(),
            guild=community_features._SampleGuild(),
            channel=None,
            message=object(),
            response=SimpleNamespace(edit_message=edit_message),
        )

        await modal.on_submit(submit_interaction)

        updated_layout = edit_message.await_args.kwargs["view"]
        assert view.config["color"] == "#12AB34"
        assert isinstance(updated_layout, discord.ui.LayoutView)
        assert updated_layout.children[0].accent_color == 0x12AB34
        cog.save_embed.assert_awaited_once()

    asyncio.run(verify_color_update())


def test_embed_create_command_saves_default_and_opens_intro() -> None:
    import community_features

    async def verify_create() -> None:
        cog = community_features.CommunityFeatures(SimpleNamespace(user=None))
        cog.save_embed = AsyncMock()
        cog._open_embed_modal = AsyncMock()
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            author=SimpleNamespace(id=42),
            interaction=None,
            send=AsyncMock(),
        )

        await community_features.CommunityFeatures.embed_create.callback(cog, ctx, "shivay")

        cog.save_embed.assert_awaited_once_with(
            1,
            "shivay",
            community_features.DEFAULT_EMBED_CONFIG,
            42,
        )
        cog._open_embed_modal.assert_awaited_once_with(
            ctx,
            "shivay",
            community_features.DEFAULT_EMBED_CONFIG,
            introduction=True,
        )

    asyncio.run(verify_create())


def test_embed_support_button_uses_configured_invite(monkeypatch) -> None:
    import community_features

    monkeypatch.delenv("SUPPORT_SERVER_URL", raising=False)
    monkeypatch.setenv("SUPPORT_SERVER", "https://discord.gg/example")
    button = community_features.EmbedSupportButton()

    assert button.label == "Need help? Join the server"
    assert button.url == "https://discord.gg/example"
    assert button.style is discord.ButtonStyle.link


def test_prefix_embed_create_opens_button_editor(monkeypatch) -> None:
    import community_features

    message = object()
    monkeypatch.setattr(
        community_features,
        "build_custom_embed",
        lambda *_args: discord.Embed(title="Editor preview", description="Editable description"),
    )

    async def verify_prefix_editor() -> None:
        cog = community_features.CommunityFeatures(SimpleNamespace(user=None))
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            author=SimpleNamespace(id=42),
            channel=SimpleNamespace(send=AsyncMock(return_value=message)),
            interaction=None,
        )
        builder = await cog._open_embed_modal(ctx, "welcome")
        assert isinstance(builder, community_features.AdvancedEmbedBuilderView)
        assert builder.message is message
        sent = ctx.channel.send.await_args.kwargs
        assert sent["view"] is builder
        assert sent["embed"].description == "Editable description"

    asyncio.run(verify_prefix_editor())


def test_custom_embed_link_buttons_are_validated_and_rendered() -> None:
    import json

    import community_features
    import pytest

    config = dict(community_features.DEFAULT_EMBED_CONFIG)
    config["buttons"] = [
        {"label": "Read more", "url": "https://example.com/guide"},
    ]
    validated = community_features.validate_embed_config(json.dumps(config))
    view = community_features.build_custom_embed_view(
        validated,
        community_features._SampleMember(),
        community_features._SampleGuild(),
        None,
        None,
    )

    assert len(view.children) == 1
    assert view.children[0].label == "Read more"
    assert view.children[0].url == "https://example.com/guide"
    assert view.children[0].style is discord.ButtonStyle.link

    config["buttons"] = [{"label": "Bad URL", "url": "javascript:alert(1)"}]
    with pytest.raises(ValueError, match="Button URL"):
        community_features.validate_embed_config(json.dumps(config))


def test_reusable_embed_persists_and_event_reply_references_it(monkeypatch) -> None:
    import json

    import community_features

    async def verify_feature_persistence() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.executescript(
            """
            CREATE TABLE greeting_settings (
                guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL,
                join_config TEXT, leave_config TEXT,
                join_enabled INTEGER NOT NULL DEFAULT 1, leave_enabled INTEGER NOT NULL DEFAULT 1,
                updated_by INTEGER NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE custom_embeds (
                guild_id INTEGER NOT NULL, name TEXT NOT NULL, config TEXT NOT NULL,
                created_by INTEGER NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(guild_id, name)
            );
            CREATE TABLE boost_settings (
                guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, message TEXT NOT NULL,
                embed_name TEXT, updated_by INTEGER NOT NULL, updated_at TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE community_event_replies (
                guild_id INTEGER NOT NULL, event TEXT NOT NULL, response TEXT, channel_id INTEGER,
                updated_by INTEGER NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(guild_id, event)
            );
            CREATE TABLE autoresponders (
                guild_id INTEGER NOT NULL, trigger TEXT NOT NULL, response TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'contains',
                updated_by INTEGER NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(guild_id, trigger)
            );
            INSERT INTO autoresponders (guild_id, trigger, response, updated_by, updated_at) VALUES
                (7, 'hello', 'Welcome {user.mention}! {delete} {sendto:55} {embed:old}', 8, 'now');
            """
        )
        bot = SimpleNamespace(db=database, user=SimpleNamespace(id=99, name="Falcon"))
        cog = community_features.CommunityFeatures(bot)
        config = {"title": "{username}", "description": "A saved feature embed", "color": "#FFDD00"}
        try:
            await cog.save_embed(7, "shared", config, 8)

            channel = SimpleNamespace(id=22)
            guild = SimpleNamespace(
                id=7,
                system_channel=channel,
                premium_subscription_count=3,
                premium_tier=1,
                get_channel=lambda _channel_id: None,
            )
            ctx = SimpleNamespace(
                guild=guild,
                channel=channel,
                author=SimpleNamespace(id=8, mention="<@8>"),
                interaction=object(),
                send=AsyncMock(),
            )

            await community_features.CommunityFeatures.event_set.callback(
                cog,
                ctx,
                "join",
                response="Welcome {user.mention}! {embed:shared}",
            )
            async with database.execute(
                "SELECT response FROM community_event_replies WHERE guild_id=7 AND event='join'"
            ) as cursor:
                event_row = await cursor.fetchone()
            assert event_row == ("Welcome {user.mention}! {embed:shared}",)

            await community_features.CommunityFeatures.embed_import.callback(
                cog, ctx, "imported-card", config=json.dumps(config)
            )
            async with database.execute(
                "SELECT config FROM custom_embeds WHERE guild_id=7 AND name='imported-card'"
            ) as cursor:
                imported_config = await cursor.fetchone()
            assert json.loads(imported_config[0]) == config

            await community_features.CommunityFeatures.autoresponder_matchmode.callback(
                cog, ctx, "hello", "startswith"
            )
            async with database.execute(
                "SELECT mode FROM autoresponders WHERE guild_id=7 AND trigger='hello'"
            ) as cursor:
                assert await cursor.fetchone() == ("startswith",)
        finally:
            await database.close()

    asyncio.run(verify_feature_persistence())


def test_autoresponder_matches_whole_words_and_extracts_arguments() -> None:
    import community_features

    assert community_features._matches_autoresponder_trigger(
        "  Hello there", "hello", "startswith"
    )
    assert not community_features._matches_autoresponder_trigger(
        "hellothere", "hello", "startswith"
    )
    assert community_features._matches_autoresponder_trigger(
        "Say hello", "hello", "endswith"
    )
    assert not community_features._matches_autoresponder_trigger(
        "Sayhello", "hello", "endswith"
    )
    assert community_features._autoresponder_arguments(
        "hello world today", "hello", "startswith"
    ) == ["world", "today"]
    assert community_features._autoresponder_arguments(
        "today hello world", "hello", "contains"
    ) == ["today", "world"]


def test_help_panels_keep_original_layout_and_manifest_role_emoji() -> None:
    import main as bot_module

    home = bot_module.build_main_help_embed()
    assert home.title is None
    assert home.footer.text is None
    assert "**Auto Roles**" in home.description
    assert "Falcon • Command Center" not in home.description
    assert "<:fp_invite:1551879046722359417> • **Voice Rooms**" in home.description
    assert "<:mods:1551879061171736587> • **Automation**" in home.description
    assert "<:fp_invite:1551879046722359417> • **More Utilities**" in home.description
    assert "<:polls:1551879063248044104> • **Profiles**" in home.description

    moderation = bot_module.build_section_embed("moderation")
    assert moderation.title == "Moderation"
    assert moderation.footer.text is None
    assert "`,channel delete #channel`" in moderation.description
    assert bot_module.HELP_SECTIONS["autoroles"]["emoji"] == bot_module.E_ROLE


def test_invite_command_and_help_link_request_administrator_for_this_bot(monkeypatch) -> None:
    from urllib.parse import parse_qs, urlsplit
    import main as bot_module

    monkeypatch.setattr(bot_module, "BOT_APPLICATION_ID", "111111111111111111")
    monkeypatch.setattr(
        type(bot_module.bot),
        "user",
        property(lambda _bot: SimpleNamespace(id=123456789012345678)),
    )
    invite_url = bot_module._bot_admin_invite_url()
    assert invite_url is not None
    parsed_url = urlsplit(invite_url)
    query = parse_qs(parsed_url.query)
    assert query == {
        "client_id": ["123456789012345678"],
        "permissions": ["8"],
        "scope": ["bot applications.commands"],
    }
    assert "scope=bot%20applications.commands" in parsed_url.query
    assert "+" not in parsed_url.query

    help_view = bot_module.HelpView()
    invite_button = next(
        button for button in help_view.children
        if isinstance(button, discord.ui.Button) and button.label == "Invite me"
    )
    assert invite_button.url == invite_url

    ctx = SimpleNamespace(author="Requester", send=AsyncMock())
    asyncio.run(bot_module.invite_cmd.callback(ctx))
    description = ctx.send.await_args.kwargs["embed"].description
    assert "Administrator permissions" in description
    assert invite_url in description


def test_full_command_registry() -> None:
    import main as bot_module

    async def verify_registry() -> None:
        bot = bot_module.bot
        original_database_path = bot_module.DATABASE_PATH
        original_database_url = bot_module.DATABASE_URL
        original_wait_until_ready = bot.wait_until_ready
        bot_module.DATABASE_PATH = ":memory:"
        bot_module.DATABASE_URL = ""
        bot.wait_until_ready = AsyncMock(return_value=None)
        try:
            await bot.setup_hook()
            commands = list(bot.walk_commands())
            assert bot.shard_count == 10
            assert bot.get_command("premium gift") is not None
            assert bot_module.DOT == bot_module.EMOJI_MANIFEST["dot"]
            for room_action in (
                "info", "set_channel", "set_category", "rename", "limit", "lock", "unlock",
                "allow", "block", "clear", "permit", "openchat", "closechat", "kick", "mute",
                "unmute", "summon", "claim", "transfer", "region", "status", "reset",
            ):
                assert bot.get_command(f"room {room_action}") is not None
            assert bot.get_command("set create") is None
            assert bot.get_command("set role") is None
            assert bot.get_command("set list") is None
            for name in ("setup create", "setup reqrole", "setup list"):
                assert bot.get_command(name) is not None
            assert bot.get_command("embed create") is not None
            assert bot.get_command("embed add") is None
            assert bot.get_command("set boost embed") is None
            for name in ("set boost message", "set boost channel"):
                assert bot.get_command(name) is not None
            for name in (
                "autorole add", "autorole remove", "autorole list",
                "voicerole add", "voicerole remove", "voicerole list",
            ):
                assert bot.get_command(name) is not None, f"Role automation command missing: {name}"
            for name in ("close", "delete", "rename"):
                assert bot.get_command(name) is not None, f"Ticket command missing: {name}"
            for name in ("lock", "unlock", "hide", "unhide"):
                assert bot.get_command(name) is not None, f"Channel command missing: {name}"
            for emoji_name in ("delete", "cross"):
                emoji_value = bot_module.EMOJI_MANIFEST[emoji_name]
                assert emoji_value.startswith(("<:", "<a:")) and emoji_value.endswith(">")
            for name in ("online", "offline", "dnd", "idle"):
                assert bot_module.EMOJI_MANIFEST[name].startswith("<:")
                assert bot_module.EMOJI_MANIFEST[name].endswith(">")
            assert len(commands) >= 250, f"Expected full command registry, found {len(commands)} commands"
            slash_roots = {command.name: command for command in bot.tree.get_commands()}
            assert len(slash_roots) <= 100, "Slash command tree exceeds Discord's global cap"
            assert bot.get_command("set") is not None, "Nested greeting set command was not registered on the bot"
            assert bot.get_command("setup") is not None
            for name in ("setup create", "setup reqrole", "setup list"):
                assert bot.get_command(name) is not None
            for name in ("embed", "events", "leave", "autoresponders", "set", "test"):
                assert name in slash_roots, (
                    f"Community command group missing from slash registry: {name}; "
                    f"registered roots: {sorted(slash_roots)}"
                )
            assert slash_roots["embed"].get_command("create") is not None
            assert slash_roots["embed"].get_command("add") is None
            set_greet = slash_roots["set"].get_command("greet")
            assert set_greet is not None
            assert set_greet.get_command("message") is not None
            assert set_greet.get_command("channel") is not None
            set_boost = slash_roots["set"].get_command("boost")
            assert set_boost is not None
            assert set_boost.get_command("message") is not None
            assert set_boost.get_command("channel") is not None
            assert set_boost.get_command("embed") is None
            assert set_boost.get_command("test") is not None
            assert slash_roots["test"].get_command("greet") is not None
            assert "greet" not in slash_roots
            assert "boost" not in slash_roots, "Boost slash commands should use the shared events group"
            assert bot.get_command("boost setup") is not None, "Prefix boost command compatibility was lost"
            for name in (
                "greet embed", "greet setup", "leave embed", "leave setup",
                "boost embed", "boost design", "events design", "autoresponder design",
            ):
                assert bot.get_command(name) is None, f"Feature-specific embed command is still registered: {name}"

            removed_commands = {
                "play", "lplay", "lavalinkstatus", "lavalinkstart", "lavalinkstop",
                "search", "skip", "pause", "resume", "stop", "queue", "shuffle",
                "volume", "history", "lyrics", "nowplaying", "join",
            }
            assert not (removed_commands - {"search"}).intersection(bot.all_commands), (
                "Removed music commands are still registered"
            )
            search_command = bot.get_command("search")
            assert search_command is not None
            assert search_command.callback.__name__ == "giphy_search"
            for name in (
                "setjoinchannel", "unsetwelcomechannel", "setleave", "unsetleavechannel",
                "setjoinmessage", "unsetjoinmessage", "setgreet", "disablegreet",
                "greetchannels", "greetvariables",
            ):
                assert name not in bot.all_commands, f"Legacy greeting command is still registered: {name}"
            for name in ("np", "tag", "afk", "rps", "rr_add", "rr_remove", "rr_list", "sync"):
                assert name not in slash_roots, f"Short command is exposed as a slash command: {name}"
                assert name in bot.all_commands, f"Prefix-only command was lost: {name}"
            for name in ("moderation", "utility"):
                assert name in slash_roots, f"Full command group missing from slash registry: {name}"
            assert slash_roots["voice"].get_command("permit") is not None
            for name in (
                "antinuke", "automod", "automodwhitelist", "antispam", "antiswear",
                "antiraid", "securityowner", "extraowner", "security", "wl",
                "tempvoice", "channel_lock",
                "channel_unlock",
            ):
                assert name not in slash_roots, f"Slash command should be hidden: {name}"
            for name in (
                "antinuke", "automod", "automodwhitelist", "antiraid", "securityowner",
                "extraowner", "security", "wl", "antispam", "antiswear",
            ):
                assert bot.get_command(name) is None, f"Security command is still registered: {name}"
            assert bot.get_command("channel_lock") is not None
            assert bot.get_command("channel_unlock") is not None
            assert slash_roots["utility"].get_command("serverinfo") is not None
            assert slash_roots["role"].get_command("all").get_command("bot") is not None
            assert "roleall" in bot.all_commands, "Bulk role command missing"
            for name in (
                "roleall human add", "roleall human remove",
                "roleall bot add", "roleall bot remove",
            ):
                assert bot.get_command(name) is not None, f"Bulk role command missing: {name}"
            assert bot.get_command("embed create") is not None, "Interactive embed create alias is missing"
            for name in ("banall", "unbanall", "unball", "unroleall"):
                assert name in bot.all_commands, f"Bulk command missing: {name}"
            assert "purgeall" in bot.all_commands, "Purge-all command missing"
            for name in (
                "role add", "role remove", "role all bot", "role all humans", "role create",
                "warn", "warnings", "clearwarnings", "unwarn", "slowmode",
                "leave enable", "leave test", "leave reset",
                "greet enable", "greet message", "greet channel", "greet preview", "greet config",
                "boost setup", "boost preview", "boost config",
                    "autoresponder enable", "autoresponder restrictions", "autoresponder delete",
                    "customcommand create", "customcommand edit", "customcommand enable",
                    "customcommand delete", "embed test",
                "help greet", "help leave", "help boost", "help autoresponder",
                    "help customcommand", "help embed", "help config",
            ):
                assert bot.get_command(name) is not None, f"Command missing: {name}"
            for name in ("jail", "unjail", "setjail"):
                assert bot.get_command(name) is not None, f"Command missing: {name}"

            for command in commands:
                assert callable(command.callback), f"Command has no callback: {command.qualified_name}"
                command.signature
                command.clean_params
        finally:
            pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for cog_name in reversed(tuple(bot.cogs)):
                await bot.remove_cog(cog_name)
            await bot.close()
            bot.wait_until_ready = original_wait_until_ready
            bot_module.DATABASE_PATH = original_database_path
            bot_module.DATABASE_URL = original_database_url

    asyncio.run(verify_registry())


def test_database_connection_migrates_legacy_file_without_deleting_it(tmp_path) -> None:
    import main as bot_module

    async def verify_migration() -> None:
        original_database_url = bot_module.DATABASE_URL
        bot_module.DATABASE_URL = ""
        legacy_path = tmp_path / "falcon_data.db"
        source = await aiosqlite.connect(legacy_path)
        await source.execute("CREATE TABLE preserved_data (value TEXT NOT NULL)")
        await source.execute("INSERT INTO preserved_data VALUES ('kept')")
        await source.commit()
        await source.close()

        database = await bot_module._connect_database("falcon_data.db", project_dir=tmp_path)
        try:
            async with database.execute("SELECT value FROM preserved_data") as cursor:
                assert await cursor.fetchone() == ("kept",)
        finally:
            await database.close()
            bot_module.DATABASE_URL = original_database_url

        assert legacy_path.exists()
        assert (tmp_path / "data" / "falcon_data.db").exists()

    asyncio.run(verify_migration())


def test_shard_connection_log_filter_only_keeps_first_connect_record() -> None:
    import main as bot_module

    log_filter = bot_module._SingleShardConnectLogFilter()
    first = logging.LogRecord(
        "discord.gateway", logging.INFO, __file__, 1,
        "Shard ID 1 has connected to Gateway", (), None,
    )
    second = logging.LogRecord(
        "discord.gateway", logging.INFO, __file__, 2,
        "Shard ID 2 has connected to Gateway", (), None,
    )
    unrelated = logging.LogRecord(
        "discord.gateway", logging.INFO, __file__, 3,
        "Shard ID 2 resumed its Gateway connection", (), None,
    )

    assert log_filter.filter(first) is True
    assert log_filter.filter(second) is False
    assert log_filter.filter(unrelated) is True


def test_rotating_presence_includes_counts_help_shards_and_owner() -> None:
    import main as bot_module

    activities = bot_module._build_presence_activities(
        member_count=12345,
        server_count=67,
        shard_count=10,
        owner_name="Falcon Owner",
    )

    assert len(activities) == 3
    assert activities[0].type is discord.ActivityType.streaming
    assert activities[0].name == "12,345 users | 67 servers"
    assert activities[0].url == bot_module.STREAM_STATUS_URL
    assert activities[1].type is discord.ActivityType.streaming
    assert activities[1].name == "/help | 10 shards"
    assert activities[1].url == bot_module.STREAM_STATUS_URL
    assert activities[2].type is discord.ActivityType.streaming
    assert activities[2].name == "Owner: Falcon Owner"
    assert activities[2].url == bot_module.STREAM_STATUS_URL
def test_shards_launch_in_sequence() -> None:
    import main as bot_module

    async def verify_launch() -> None:
        client = bot_module.FalconBot()
        client.shard_count = 3
        client.shard_ids = None
        client._connection.shard_count = None
        active_launches = 0
        max_active_launches = 0

        async def launch_shard(*_args, **_kwargs):
            nonlocal active_launches, max_active_launches
            active_launches += 1
            max_active_launches = max(max_active_launches, active_launches)
            await asyncio.sleep(0)
            active_launches -= 1

        launch_shard = AsyncMock(side_effect=launch_shard)
        client.launch_shard = launch_shard
        try:
            await client.launch_shards()
            assert launch_shard.await_count == 3
            assert [call.args[1] for call in launch_shard.await_args_list] == [0, 1, 2]
            assert [call.kwargs["initial"] for call in launch_shard.await_args_list] == [True, False, False]
            assert max_active_launches == 1
        finally:
            await client.close()

    asyncio.run(verify_launch())




def test_identify_hook_waits_only_within_same_discord_bucket(monkeypatch) -> None:
    import main as bot_module

    waits = []

    async def capture_sleep(delay):
        waits.append(delay)

    monkeypatch.setattr(bot_module.asyncio, "sleep", capture_sleep)
    client = SimpleNamespace(
        identify_max_concurrency=2,
        _identify_bucket_locks={},
        _identify_bucket_last_started={},
    )

    async def verify_buckets() -> None:
        await bot_module.FalconBot.before_identify_hook(client, 0, initial=True)
        await bot_module.FalconBot.before_identify_hook(client, 1)
        await bot_module.FalconBot.before_identify_hook(client, 2)
        assert len(waits) == 1
        assert 5.0 <= waits[0] <= 5.1

    asyncio.run(verify_buckets())


def test_on_ready_schedules_startup_work_without_blocking(monkeypatch) -> None:
    import main as bot_module

    class PendingTask:
        def done(self):
            return False

        def cancel(self):
            return None

    scheduled = []

    def schedule(coroutine):
        coroutine.close()
        task = PendingTask()
        scheduled.append(task)
        return task

    monkeypatch.setattr(
        bot_module,
        "bot",
        SimpleNamespace(
            status_rotation_task=None,
            post_ready_task=None,
            golden_neon_sync_task=None,
        ),
    )
    monkeypatch.setattr(bot_module.asyncio, "create_task", schedule)

    async def verify_ready_handler():
        await bot_module.on_ready()
        assert len(scheduled) == 3
        await bot_module.on_ready()
        assert len(scheduled) == 3

    asyncio.run(verify_ready_handler())


def test_post_ready_initialization_runs_parallel_startup_tasks(monkeypatch) -> None:
    import main as bot_module

    started = []
    release = [asyncio.Event(), asyncio.Event(), asyncio.Event()]

    async def fake_tree_sync():
        started.append("tree")
        await release[0].wait()
        return []

    async def fake_sync_emojis():
        started.append("emoji")
        await release[1].wait()
        return True

    async def fake_warm_guilds(*_args, **_kwargs):
        started.append("warm")
        await release[2].wait()
        return None

    monkeypatch.setattr(bot_module.bot, "slash_commands_synced", False)
    monkeypatch.setattr(bot_module.bot.tree, "sync", AsyncMock(side_effect=fake_tree_sync))
    monkeypatch.setattr(bot_module, "sync_application_emojis", AsyncMock(side_effect=fake_sync_emojis))
    monkeypatch.setattr(bot_module, "_warm_guild_runtime_state", AsyncMock(side_effect=fake_warm_guilds))
    monkeypatch.setattr(type(bot_module.bot), "guilds", property(lambda _self: [SimpleNamespace()]), raising=False)

    async def verify_parallel():
        task = asyncio.create_task(bot_module._post_ready_initialization())
        await asyncio.sleep(0.05)
        assert set(started) == {"tree", "emoji", "warm"}
        for gate in release:
            gate.set()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(verify_parallel())


def test_apply_golden_neon_to_existing_guilds_runs_concurrently(monkeypatch) -> None:
    import main as bot_module

    calls = []
    start = asyncio.Event()
    released = asyncio.Event()

    async def fake_apply(guild):
        calls.append(guild.id)
        await start.wait()
        await released.wait()
        return True

    guilds = [SimpleNamespace(id=10), SimpleNamespace(id=20), SimpleNamespace(id=30)]
    monkeypatch.setattr(type(bot_module.bot), "guilds", property(lambda _self: guilds), raising=False)
    monkeypatch.setattr(bot_module, "apply_golden_neon_name_style", fake_apply)

    async def verify_parallel():
        task = asyncio.create_task(bot_module.apply_golden_neon_to_existing_guilds())
        await asyncio.sleep(0.05)
        assert len(calls) == len(guilds)
        start.set()
        released.set()
        await asyncio.wait_for(task, timeout=1)
        assert set(calls) == {10, 20, 30}

    asyncio.run(verify_parallel())


def test_boost_settings_add_embed_migration() -> None:
    from community_features import CommunityFeatures

    async def verify_migration() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.execute(
            "CREATE TABLE boost_settings (guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, "
            "message TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, updated_by INTEGER NOT NULL, "
            "updated_at TEXT NOT NULL)"
        )
        await database.execute(
            "INSERT INTO boost_settings VALUES (1, 5, 'Thanks {user.mention}', 1, 7, '2026-01-01')"
        )
        await database.execute(
            "CREATE TABLE greeting_settings (guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, "
            "join_config TEXT, leave_config TEXT, join_enabled INTEGER NOT NULL DEFAULT 1, "
            "leave_enabled INTEGER NOT NULL DEFAULT 1, updated_by INTEGER NOT NULL, updated_at TEXT NOT NULL)"
        )
        await database.commit()
        cog = CommunityFeatures(SimpleNamespace(db=database))
        try:
            await cog.cog_load()
            async with database.execute("PRAGMA table_info(boost_settings)") as cursor:
                columns = {row[1] for row in await cursor.fetchall()}
            assert "embed_name" in columns
            assert "card_enabled" in columns
            assert "card_channel_id" in columns
            async with database.execute("PRAGMA table_info(greeting_settings)") as cursor:
                greeting_columns = {row[1] for row in await cursor.fetchall()}
            assert {"join_card_channel_id", "leave_card_channel_id"} <= greeting_columns
            assert {"join_card_enabled", "leave_card_enabled"} <= greeting_columns
            async with database.execute(
                "SELECT channel_id, message, enabled, embed_name, card_enabled, card_channel_id "
                "FROM boost_settings WHERE guild_id=1"
            ) as cursor:
                assert await cursor.fetchone() == (5, "Thanks {user.mention}", 1, None, 0, None)
        finally:
            await database.close()

    asyncio.run(verify_migration())


def test_set_boost_commands_preserve_and_attach_saved_embed(monkeypatch) -> None:
    import community_features

    async def verify_set_boost_commands() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.execute(
            "CREATE TABLE boost_settings (guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, "
            "message TEXT NOT NULL, embed_name TEXT, updated_by INTEGER NOT NULL, updated_at TEXT NOT NULL)"
        )
        await database.execute(
            "CREATE TABLE custom_embeds (guild_id INTEGER, name TEXT, config TEXT)"
        )
        await database.execute(
            "INSERT INTO boost_settings VALUES (1, 5, 'Old message', 'thanks', 7, '2026-01-01')"
        )
        await database.execute(
            "INSERT INTO custom_embeds VALUES (1, 'welcome', '{\"title\":\"Boosted!\"}')"
        )
        await database.commit()
        cog = community_features.CommunityFeatures(
            SimpleNamespace(db=database, user=SimpleNamespace(name="Falcon", id=99))
        )
        channel = SimpleNamespace(id=9, mention="<#9>")
        ctx = SimpleNamespace(
            guild=SimpleNamespace(
                id=1,
                system_channel=SimpleNamespace(id=4),
                premium_subscription_count=2,
                premium_tier=1,
                get_channel=lambda _channel_id: channel,
            ),
            channel=channel,
            author=SimpleNamespace(id=10, mention="<@10>"),
            interaction=None,
            send=AsyncMock(),
        )
        preview = discord.Embed(title="Boosted preview")
        monkeypatch.setattr(community_features, "build_custom_embed", lambda *_args: preview)
        monkeypatch.setattr(
            community_features,
            "render_variables",
            lambda value, *_args: value.replace("{user.mention}", "<@10>"),
        )
        try:
            await community_features.CommunityFeatures.set_boost_message.callback(
                cog,
                ctx,
                message="  New {user.mention} message  ",
            )
            async with database.execute(
                "SELECT channel_id, message, embed_name FROM boost_settings WHERE guild_id=1"
            ) as cursor:
                assert await cursor.fetchone() == (5, "New {user.mention} message", "thanks")

            await community_features.CommunityFeatures.set_boost_channel.callback(
                cog,
                ctx,
                channel,
            )
            async with database.execute(
                "SELECT channel_id, message, embed_name FROM boost_settings WHERE guild_id=1"
            ) as cursor:
                assert await cursor.fetchone() == (9, "New {user.mention} message", "thanks")

            await community_features.CommunityFeatures.set_boost_message.callback(
                cog,
                ctx,
                message="  New {user.mention} message {embed:welcome}  ",
            )
            async with database.execute(
                "SELECT channel_id, message, embed_name FROM boost_settings WHERE guild_id=1"
            ) as cursor:
                assert await cursor.fetchone() == (
                    9,
                    "New {user.mention} message {embed:welcome}",
                    None,
                )

            await community_features.CommunityFeatures.set_boost_test.callback(cog, ctx)
            assert ctx.send.await_args.kwargs["embed"] is preview
            assert ctx.send.await_args.kwargs["content"] == "New <@10> message"
        finally:
            await database.close()

    asyncio.run(verify_set_boost_commands())


def test_boost_event_renders_saved_embed(monkeypatch) -> None:
    import community_features

    async def verify_boost_embed() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.executescript(
            "CREATE TABLE community_event_replies (guild_id INTEGER, event TEXT, response TEXT, channel_id INTEGER, "
            "updated_by INTEGER, updated_at TEXT, PRIMARY KEY(guild_id, event));"
            "CREATE TABLE boost_settings (guild_id INTEGER, channel_id INTEGER, message TEXT, "
            "enabled INTEGER, embed_name TEXT, card_enabled INTEGER DEFAULT 0, "
            "card_channel_id INTEGER);"
            "CREATE TABLE custom_embeds (guild_id INTEGER, name TEXT, config TEXT);"
            "INSERT INTO boost_settings VALUES (1, 5, 'Thanks {user.mention}', 1, 'thanks', 0, NULL);"
            "INSERT INTO custom_embeds VALUES (1, 'thanks', '{\"title\":\"Thanks {user.name}\","
            "\"description\":\"Boost #{boost_count}\",\"color\":\"#FFDD00\"}');"
        )
        bot_member = object()
        permissions = SimpleNamespace(view_channel=True, send_messages=True, embed_links=True)
        channel = SimpleNamespace(
            id=5,
            name="boosts",
            mention="#boosts",
            permissions_for=lambda _member: permissions,
        )
        guild = SimpleNamespace(
            id=1,
            name="Test Server",
            owner_id=99,
            member_count=10,
            premium_subscription_count=4,
            premium_tier=2,
            icon=None,
            me=bot_member,
            get_member=lambda _user_id: None,
            get_channel=lambda channel_id: channel if channel_id == channel.id else None,
        )
        member = SimpleNamespace(
            id=2,
            name="Booster",
            display_name="Booster",
            mention="<@2>",
            display_avatar=SimpleNamespace(url="https://cdn.test/avatar.png"),
            created_at=None,
            joined_at=None,
            guild=guild,
        )
        sender = AsyncMock()
        monkeypatch.setattr(community_features, "send_v2", sender)
        cog = community_features.CommunityFeatures(
            SimpleNamespace(db=database, user=SimpleNamespace(name="Falcon", id=99))
        )
        try:
            await cog.on_member_update(
                SimpleNamespace(premium_since=None),
                SimpleNamespace(premium_since=object(), **member.__dict__),
            )
            sender.assert_awaited_once()
            sent_embed = sender.await_args.kwargs["embed"]
            assert sent_embed.title == "Thanks Booster"
            assert sent_embed.description == "Boost #4"
            assert sender.await_args.args[1] == "Thanks <@2>"
        finally:
            await database.close()

    asyncio.run(verify_boost_embed())


def test_boost_card_keeps_thanks_outside_image(monkeypatch) -> None:
    import io

    import community_features

    async def verify_boost_card() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.executescript(
            "CREATE TABLE community_event_replies (guild_id INTEGER, event TEXT, response TEXT, "
            "channel_id INTEGER, updated_by INTEGER, updated_at TEXT, PRIMARY KEY(guild_id, event));"
            "CREATE TABLE boost_settings (guild_id INTEGER, channel_id INTEGER, message TEXT, "
            "enabled INTEGER, embed_name TEXT, card_enabled INTEGER, card_channel_id INTEGER);"
            "INSERT INTO boost_settings VALUES (1, 5, 'old text', 1, NULL, 1, 6);"
        )
        channel = SimpleNamespace(id=5, mention="#boosts")
        card_channel = SimpleNamespace(id=6, mention="#booster-cards")
        guild = SimpleNamespace(
            id=1,
            name="Example Server",
            premium_subscription_count=8,
            premium_tier=2,
            get_channel=lambda channel_id: {5: channel, 6: card_channel}.get(channel_id),
        )
        member = SimpleNamespace(
            id=2,
            display_name="Booster",
            mention="<@2>",
            guild=guild,
            premium_since=object(),
        )
        send = AsyncMock()
        monkeypatch.setattr(community_features, "send_v2", send)
        cog = community_features.CommunityFeatures(SimpleNamespace(db=database, user=None))
        card = discord.File(io.BytesIO(b"card"), filename="booster.png")
        cog._build_member_event_card = AsyncMock(return_value=card)
        try:
            await cog.on_member_update(SimpleNamespace(premium_since=None), member)
            cog._build_member_event_card.assert_awaited_once_with(
                member,
                "boost",
                "LEVEL 2  •  BOOST #8",
            )
            send.assert_awaited_once()
            assert (
                f"{community_features.BOOST_EMOJI} Thank you <@2> for boosting **Example Server**!"
                == send.await_args.args[1]
            )
            assert send.await_args.args[0] is card_channel
            assert send.await_args.kwargs["file"] is card
            assert send.await_args.kwargs["embed"].image.url == "attachment://booster.png"
            assert send.await_args.kwargs["allowed_mentions"].users == [member]
        finally:
            await database.close()

    asyncio.run(verify_boost_card())


def test_greeting_card_uses_configured_channel(monkeypatch) -> None:
    import io

    import community_features

    async def verify_greeting_card() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.execute(
            "CREATE TABLE greeting_settings (guild_id INTEGER PRIMARY KEY, channel_id INTEGER, "
            "join_config TEXT, leave_config TEXT, join_enabled INTEGER, leave_enabled INTEGER, "
            "join_card_enabled INTEGER, leave_card_enabled INTEGER, "
            "join_card_channel_id INTEGER, leave_card_channel_id INTEGER)"
        )
        await database.execute(
            "CREATE TABLE community_event_replies (guild_id INTEGER, event TEXT, response TEXT, channel_id INTEGER, "
            "updated_by INTEGER, updated_at TEXT, PRIMARY KEY(guild_id, event))"
        )
        await database.execute(
            "CREATE TABLE greeting_delete_settings (guild_id INTEGER PRIMARY KEY, "
            "join_seconds INTEGER, leave_seconds INTEGER)"
        )
        await database.execute(
            "INSERT INTO greeting_settings VALUES (1, 5, NULL, NULL, 1, 1, 1, 1, 6, 7)"
        )
        await database.commit()
        default_channel = SimpleNamespace(id=5)
        join_channel = SimpleNamespace(id=6)
        leave_channel = SimpleNamespace(id=7)
        guild = SimpleNamespace(
            id=1,
            name="Example Server",
            member_count=4,
            icon=None,
            get_channel=lambda channel_id: {
                5: default_channel,
                6: join_channel,
                7: leave_channel,
            }.get(channel_id),
        )
        member = SimpleNamespace(id=2, display_name="New Member", mention="<@2>", guild=guild)
        send = AsyncMock()
        monkeypatch.setattr(community_features, "send_v2", send)
        cog = community_features.CommunityFeatures(SimpleNamespace(db=database, user=None))
        join_card = discord.File(io.BytesIO(b"join-card"), filename="welcome.png")
        leave_card = discord.File(io.BytesIO(b"leave-card"), filename="goodbye.png")
        cog._build_member_event_card = AsyncMock(side_effect=[join_card, leave_card])
        try:
            assert await cog.send_configured_greeting(member, "join") is True
            cog._build_member_event_card.assert_awaited_once_with(member, "join", "MEMBER #4")
            send.assert_awaited_once()
            assert send.await_args.args[0] is join_channel
            assert send.await_args.args[1] == member.mention
            assert send.await_args.kwargs["file"] is join_card
            assert send.await_args.kwargs["embed"].image.url == "attachment://welcome.png"

            assert await cog.send_configured_greeting(member, "leave") is True
            assert send.await_count == 2
            assert send.await_args_list[1].args[0] is leave_channel
            assert send.await_args_list[1].args[1] is None
            assert send.await_args_list[1].kwargs["file"] is leave_card
            assert send.await_args_list[1].kwargs["allowed_mentions"].users is False
        finally:
            await database.close()

    asyncio.run(verify_greeting_card())


def test_event_reply_replaces_legacy_join_greeting(monkeypatch) -> None:
    import community_features

    async def verify_event_precedence() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.executescript(
            "CREATE TABLE community_event_replies (guild_id INTEGER, event TEXT, response TEXT, "
            "channel_id INTEGER, updated_by INTEGER, updated_at TEXT, PRIMARY KEY(guild_id, event));"
            "INSERT INTO community_event_replies VALUES (1, 'join', 'Hello {user.mention}', 5, 7, 'now');"
            "CREATE TABLE greeting_settings (guild_id INTEGER PRIMARY KEY, channel_id INTEGER, "
            "join_config TEXT, leave_config TEXT, join_enabled INTEGER, leave_enabled INTEGER, "
            "join_card_enabled INTEGER, leave_card_enabled INTEGER, "
            "join_card_channel_id INTEGER, leave_card_channel_id INTEGER);"
            "CREATE TABLE greeting_delete_settings (guild_id INTEGER PRIMARY KEY, "
            "join_seconds INTEGER, leave_seconds INTEGER);"
            "INSERT INTO greeting_settings VALUES "
            "(1, 5, '{\"description\":\"legacy\"}', NULL, 1, 1, 0, 0, NULL, NULL);"
        )
        channel = SimpleNamespace(id=5)
        guild = SimpleNamespace(id=1, get_channel=lambda channel_id: channel if channel_id == 5 else None)
        member = SimpleNamespace(id=10, mention="<@10>", guild=guild)
        send_response = AsyncMock(return_value=True)
        legacy_send = AsyncMock()
        monkeypatch.setattr(community_features, "send_v2", legacy_send)
        cog = community_features.CommunityFeatures(SimpleNamespace(db=database, user=None))
        cog._send_automated_response = send_response
        try:
            assert await cog.send_configured_greeting(member, "join") is True
            send_response.assert_awaited_once()
            assert send_response.await_args.args[1] == "Hello {user.mention}"
            assert send_response.await_args.kwargs["channel"] is channel
            legacy_send.assert_not_awaited()
        finally:
            await database.close()

    asyncio.run(verify_event_precedence())


def test_saved_greet_message_resolves_reusable_embed_action() -> None:
    import community_features

    async def verify_saved_greeting() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.executescript(
            "CREATE TABLE community_event_replies (guild_id INTEGER, event TEXT, response TEXT, "
            "channel_id INTEGER, updated_by INTEGER, updated_at TEXT, PRIMARY KEY(guild_id, event));"
            "CREATE TABLE greeting_settings (guild_id INTEGER PRIMARY KEY, channel_id INTEGER, "
            "join_config TEXT, leave_config TEXT, join_enabled INTEGER, leave_enabled INTEGER, "
            "join_card_enabled INTEGER, leave_card_enabled INTEGER, "
            "join_card_channel_id INTEGER, leave_card_channel_id INTEGER);"
            "CREATE TABLE greeting_delete_settings (guild_id INTEGER PRIMARY KEY, "
            "join_seconds INTEGER, leave_seconds INTEGER);"
            "INSERT INTO greeting_delete_settings VALUES (1, 30, NULL);"
            "CREATE TABLE guild_settings (guild_id INTEGER PRIMARY KEY, join_channel_id INTEGER, "
            "join_message TEXT, join_enabled INTEGER, leave_channel_id INTEGER, leave_message TEXT, leave_enabled INTEGER);"
            "INSERT INTO guild_settings VALUES (1, 5, 'Welcome {user.mention} {embed:shivay}', 1, NULL, NULL, 1);"
        )
        channel = SimpleNamespace(id=5, mention="#welcome")
        guild = SimpleNamespace(id=1, get_channel=lambda channel_id: channel if channel_id == 5 else None)
        member = SimpleNamespace(id=2, mention="<@2>", guild=guild)
        cog = community_features.CommunityFeatures(SimpleNamespace(db=database, user=None))
        sender = AsyncMock(return_value=True)
        cog._send_automated_response = sender
        try:
            assert await cog.send_configured_greeting(member, "join") is True
            sender.assert_awaited_once()
            assert sender.await_args.args[1] == "Welcome {user.mention} {embed:shivay}"
            assert sender.await_args.kwargs["channel"] is channel
            assert sender.await_args.kwargs["user"] is member
            assert sender.await_args.kwargs["allow_delete"] is False
            assert sender.await_args.kwargs["delete_after"] == 30
        finally:
            await database.close()

    asyncio.run(verify_saved_greeting())


def test_boost_and_greeting_card_toggles_are_independent(monkeypatch) -> None:
    import community_features

    async def verify_card_toggles() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.executescript(
            "CREATE TABLE boost_settings (guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, "
            "message TEXT NOT NULL, enabled INTEGER DEFAULT 1, card_enabled INTEGER DEFAULT 0, "
            "card_channel_id INTEGER, updated_by INTEGER NOT NULL, updated_at TEXT NOT NULL);"
            "INSERT INTO boost_settings VALUES (1, 5, 'Thanks', 1, 0, NULL, 2, 'now');"
            "CREATE TABLE greeting_settings (guild_id INTEGER PRIMARY KEY, channel_id INTEGER, "
            "join_config TEXT, leave_config TEXT, join_enabled INTEGER DEFAULT 1, leave_enabled INTEGER DEFAULT 1, "
            "join_card_enabled INTEGER DEFAULT 0, leave_card_enabled INTEGER DEFAULT 0, "
            "join_card_channel_id INTEGER, leave_card_channel_id INTEGER, "
            "updated_by INTEGER NOT NULL, updated_at TEXT NOT NULL);"
            "INSERT INTO greeting_settings VALUES (1, 5, NULL, NULL, 1, 1, 0, 0, NULL, NULL, 2, 'now');"
        )
        monkeypatch.setattr(community_features, "send_v2", AsyncMock())
        cog = community_features.CommunityFeatures(SimpleNamespace(db=database))
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1, get_channel=lambda _channel_id: None),
            author=SimpleNamespace(id=2),
            interaction=None,
            send=AsyncMock(),
        )
        try:
            await community_features.CommunityFeatures.boost_card.callback(cog, ctx, "enable")
            async with database.execute("SELECT card_enabled FROM boost_settings WHERE guild_id=1") as cursor:
                assert await cursor.fetchone() == (1,)
            boost_panel = community_features.send_v2.await_args.kwargs["view"]
            assert len(boost_panel.children) == 1
            await community_features.CommunityFeatures.greet_card.callback(cog, ctx, "enable", "both")
            greeting_panel = community_features.send_v2.await_args.kwargs["view"]
            assert len(greeting_panel.children) == 3
            async with database.execute(
                "SELECT join_card_enabled, leave_card_enabled FROM greeting_settings WHERE guild_id=1"
            ) as cursor:
                assert await cursor.fetchone() == (1, 1)
            await community_features.CommunityFeatures.greet_card.callback(cog, ctx, "disable", "leave")
            async with database.execute(
                "SELECT join_card_enabled, leave_card_enabled FROM greeting_settings WHERE guild_id=1"
            ) as cursor:
                assert await cursor.fetchone() == (1, 0)
            await community_features.CommunityFeatures.boost_card.callback(cog, ctx, "disable")
            async with database.execute("SELECT card_enabled FROM boost_settings WHERE guild_id=1") as cursor:
                assert await cursor.fetchone() == (0,)
        finally:
            await database.close()

    asyncio.run(verify_card_toggles())


def test_greeting_and_boost_card_channel_selectors_persist(monkeypatch) -> None:
    import community_features

    async def verify_channel_selectors() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.executescript(
            "CREATE TABLE greeting_settings (guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, "
            "join_config TEXT, leave_config TEXT, join_enabled INTEGER DEFAULT 1, "
            "leave_enabled INTEGER DEFAULT 1, join_card_enabled INTEGER DEFAULT 1, "
            "leave_card_enabled INTEGER DEFAULT 1, join_card_channel_id INTEGER, "
            "leave_card_channel_id INTEGER, updated_by INTEGER NOT NULL, updated_at TEXT NOT NULL);"
            "INSERT INTO greeting_settings VALUES (1, 5, NULL, NULL, 1, 1, 1, 1, NULL, NULL, 2, 'now');"
            "CREATE TABLE boost_settings (guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, "
            "message TEXT NOT NULL, enabled INTEGER DEFAULT 1, card_enabled INTEGER DEFAULT 1, "
            "card_channel_id INTEGER, updated_by INTEGER NOT NULL, updated_at TEXT NOT NULL);"
            "INSERT INTO boost_settings VALUES (1, 5, 'Thanks', 1, 1, NULL, 2, 'now');"
        )
        shared = SimpleNamespace(id=5, mention="#shared")
        join = SimpleNamespace(id=6, mention="#join-cards")
        boost = SimpleNamespace(id=7, mention="#boost-cards")
        guild = SimpleNamespace(
            get_channel=lambda channel_id: {
                5: shared,
                6: join,
                7: boost,
            }.get(channel_id)
        )
        interaction = SimpleNamespace(
            guild=guild,
            user=SimpleNamespace(id=2),
        )
        monkeypatch.setattr(community_features, "edit_interaction_v2", AsyncMock())
        cog = community_features.CommunityFeatures(SimpleNamespace(db=database, user=None))
        greeting_view = community_features.GreetingCardSetupView(cog, 1, 2)
        boost_view = community_features.BoostCardSetupView(cog, 1, 2)
        try:
            await greeting_view._save_channel(interaction, "join_card_channel_id", join)
            await greeting_view._save_channel(interaction, "channel_id", shared)
            async with database.execute(
                "SELECT channel_id, join_card_channel_id, leave_card_channel_id "
                "FROM greeting_settings WHERE guild_id=1"
            ) as cursor:
                assert await cursor.fetchone() == (5, 6, None)

            await boost_view._save_channel(interaction, boost)
            async with database.execute(
                "SELECT card_channel_id, card_enabled, updated_by "
                "FROM boost_settings WHERE guild_id=1"
            ) as cursor:
                assert (await cursor.fetchone()) == (7, 1, 2)
        finally:
            await database.close()

    asyncio.run(verify_channel_selectors())


def test_greet_and_boost_card_test_actions_preview_images_without_settings(monkeypatch) -> None:
    import community_features

    async def verify_card_previews() -> None:
        cog = community_features.CommunityFeatures(SimpleNamespace(db=None, user=None))
        ctx = SimpleNamespace(
            guild=SimpleNamespace(
                member_count=14,
                premium_subscription_count=6,
                premium_tier=2,
            ),
            author=SimpleNamespace(id=2),
            interaction=None,
        )
        cog._build_member_event_card = AsyncMock(
            side_effect=[
                discord.File(io.BytesIO(b"boost"), filename="boost.png"),
                discord.File(io.BytesIO(b"join"), filename="join.png"),
                discord.File(io.BytesIO(b"leave"), filename="leave.png"),
            ]
        )
        send = AsyncMock()
        monkeypatch.setattr(community_features, "send_v2", send)

        await community_features.CommunityFeatures.boost_card.callback(cog, ctx, "test")
        await community_features.CommunityFeatures.greet_card.callback(
            cog,
            ctx,
            "test",
            "both",
        )

        assert [
            (call.args[1], call.kwargs["embed"].title)
            for call in send.await_args_list
        ] == [
            (
                f"{community_features.BOOST_EMOJI} Boost card preview. Server settings were not changed.",
                "Boost card preview",
            ),
            ("Join greeting card preview. Server settings were not changed.", "Join greeting card preview"),
            ("Leave greeting card preview. Server settings were not changed.", "Leave greeting card preview"),
        ]
        assert [
            call.args[:2] for call in cog._build_member_event_card.await_args_list
        ] == [
            (ctx.author, "boost"),
            (ctx.author, "join"),
            (ctx.author, "leave"),
        ]

    asyncio.run(verify_card_previews())


def test_card_text_replaces_unsupported_glyphs_instead_of_showing_boxes() -> None:
    from PIL import ImageFont

    from card_theme import normalize_card_text

    font = ImageFont.truetype("DejaVuSans.ttf", 20)
    assert normalize_card_text("Example 漢🫶", font) == "Example ??"


def test_boost_card_invalid_action_shows_one_action_usage() -> None:
    import community_features
    from discord.ext import commands

    async def verify_invalid_action() -> None:
        command = community_features.CommunityFeatures.boost_card
        parameter = command.params["action"]
        error = commands.BadLiteralArgument(
            parameter,
            ("enable", "disable", "test"),
            [],
            "enable/disable",
        )
        ctx = SimpleNamespace(send=AsyncMock())
        cog = community_features.CommunityFeatures(SimpleNamespace())

        await command.on_error(cog, ctx, error)

        ctx.send.assert_awaited_once_with(
            "Choose one action: `enable`, `disable`, or `test` (not `enable/disable`)."
        )

    asyncio.run(verify_invalid_action())


def test_greeting_auto_delete_can_be_configured_per_event() -> None:
    import community_features

    async def verify_delete_settings() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.execute(
            "CREATE TABLE greeting_delete_settings ("
            "guild_id INTEGER PRIMARY KEY, join_seconds INTEGER, leave_seconds INTEGER)"
        )
        cog = community_features.CommunityFeatures(SimpleNamespace(db=database))
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            interaction=None,
            send=AsyncMock(),
        )
        try:
            await community_features.CommunityFeatures.greet_delete_after.callback(
                cog, ctx, "join", 30
            )
            assert await cog._greeting_delete_after(1, "join") == 30
            assert await cog._greeting_delete_after(1, "leave") is None

            await community_features.CommunityFeatures.greet_delete_after.callback(
                cog, ctx, "both", 0
            )
            assert await cog._greeting_delete_after(1, "join") is None
            assert await cog._greeting_delete_after(1, "leave") is None
        finally:
            await database.close()

    asyncio.run(verify_delete_settings())


def test_poll_supports_private_interaction_responses() -> None:
    import zyre_features

    async def verify_poll_visibility() -> None:
        cog = zyre_features.ZyreFeatures(SimpleNamespace())
        context = SimpleNamespace(
            author=SimpleNamespace(id=1),
            interaction=object(),
            send=AsyncMock(),
        )
        await zyre_features.ZyreFeatures.poll.callback(
            cog, context, "Question", "Yes", "No", True
        )
        assert context.send.await_args.kwargs["ephemeral"] is True
        assert isinstance(context.send.await_args.kwargs["view"], zyre_features.PollView)

        context.interaction = None
        context.send.reset_mock()
        await zyre_features.ZyreFeatures.poll.callback(
            cog, context, "Question", "Yes", "No", True
        )
        assert context.send.await_args.kwargs["ephemeral"] is False

    asyncio.run(verify_poll_visibility())


def test_member_event_cards_render_png_with_optional_server_icon() -> None:
    import io

    from PIL import Image

    from community_features import render_member_event_card

    def png(color):
        output = io.BytesIO()
        Image.new("RGB", (32, 32), color).save(output, format="PNG")
        return output.getvalue()

    avatar = png("#ff00ff")
    server_icon = png("#00ffff")
    bot_icon = png("#00ffff")
    for event_kind in ("boost", "join", "leave"):
        card_bytes = render_member_event_card(
            avatar,
            "A very long member display name that needs fitting",
            "Example Server",
            event_kind,
            "MEMBER #52",
            guild_icon_bytes=server_icon,
            bot_icon_bytes=bot_icon,
        )
        assert card_bytes.startswith(b"\x89PNG\r\n\x1a\n")
        with Image.open(io.BytesIO(card_bytes)) as card:
            assert card.size == (1400, 620)
            assert min(card.getpixel((0, 0))) > 220
            assert card.getpixel((1235, 135)) == (0, 255, 255)


def test_rank_and_ping_cards_use_the_light_premium_theme() -> None:
    import io

    from PIL import Image

    import leveling_features
    from community_features import render_ping_card

    avatar_output = io.BytesIO()
    Image.new("RGB", (32, 32), "#ff00ff").save(avatar_output, format="PNG")
    avatar = avatar_output.getvalue()
    bot_icon_output = io.BytesIO()
    Image.new("RGB", (32, 32), "#00ffff").save(bot_icon_output, format="PNG")
    bot_icon = bot_icon_output.getvalue()

    rank_card = leveling_features.render_rank_card(
        avatar,
        "A very long rank-card display name that must be fitted into the available layout",
        level=4,
        xp=1_750,
        rank=2,
        streak=6,
        voice_seconds=3_660,
        bot_icon_bytes=bot_icon,
    )
    with Image.open(io.BytesIO(rank_card)) as card:
        assert card.size == (1200, 560)
        assert min(card.getpixel((0, 0))) > 220
        assert card.getpixel((1084, 120)) == (0, 255, 255)

    ping_card = render_ping_card(
        gateway_ms=42,
        api_ms=31.5,
        shard_id=0,
        shard_count=2,
        guild_count=12,
        member_count=4_500,
        uptime="1 day, 2:03:04",
        database_ms=3.2,
        bot_icon_bytes=bot_icon,
        bot_name="Falcon Test Bot",
        bot_id=123_456,
        command_count=85,
        memory_mb=128.5,
        python_version="3.14.2",
    )
    with Image.open(io.BytesIO(ping_card)) as card:
        assert card.size == (1400, 860)
        assert min(card.getpixel((0, 0))) > 220
        assert card.getpixel((1212, 142)) == (0, 255, 255)


def test_ping_command_sends_connection_diagnostics_card(monkeypatch) -> None:
    import main as bot_module
    import community_features

    class AvatarAsset:
        def with_format(self, _format):
            return self

        def with_size(self, _size):
            return self

        async def read(self):
            output = io.BytesIO()
            Image.new("RGB", (32, 32), "#00ffff").save(output, format="PNG")
            return output.getvalue()

    async def verify_ping_card() -> None:
        database = await aiosqlite.connect(":memory:")
        message = object()
        send = AsyncMock(return_value=message)
        edit = AsyncMock()
        render_call = {}
        actual_render = community_features.render_ping_card

        def capture_render(*args, **kwargs):
            render_call["args"] = args
            render_call["kwargs"] = kwargs
            return actual_render(*args, **kwargs)

        monkeypatch.setattr(community_features, "render_ping_card", capture_render)
        fake_bot = SimpleNamespace(
            latency=0.042,
            shard_count=2,
            shards={0: None, 1: None},
            guilds=[SimpleNamespace(member_count=12)],
            db=database,
            commands=[object(), object(), object()],
            user=SimpleNamespace(
                id=123,
                name="Falcon Test Bot",
                display_avatar=AvatarAsset(),
            ),
        )
        monkeypatch.setattr(bot_module, "bot", fake_bot)
        monkeypatch.setattr(bot_module, "send_v2", send)
        monkeypatch.setattr(bot_module, "edit_message_v2", edit)
        ctx = SimpleNamespace(guild=SimpleNamespace(shard_id=0))
        try:
            await bot_module.ping.callback(ctx)
            send.assert_awaited_once_with(ctx, "Measuring Falcon's live connection...")
            edit.assert_awaited_once()
            edited_message = edit.await_args.args[0]
            embed = edit.await_args.kwargs["embed"]
            card_file = edit.await_args.kwargs["attachments"][0]
            assert edited_message is message
            assert embed.title is None
            assert embed.description is None
            assert embed.color.value == 0xFFDD00
            assert embed.image.url == "attachment://falcon-ping.png"
            assert card_file.filename == "falcon-ping.png"
            assert render_call["kwargs"]["bot_name"] == "Falcon Test Bot"
            assert render_call["kwargs"]["bot_id"] == 123
            assert render_call["kwargs"]["command_count"] == 3
            assert render_call["kwargs"]["memory_mb"] > 0
            assert render_call["kwargs"]["python_version"] != "Unknown"
        finally:
            await database.close()

    asyncio.run(verify_ping_card())


def test_leveling_backup_is_owner_only_and_exports_scoped_json() -> None:
    import json
    import pytest
    from discord.ext import commands

    import leveling_features

    async def verify_leveling_backup() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.executescript(
            """
            CREATE TABLE leveling_settings (
                guild_id INTEGER PRIMARY KEY, enabled INTEGER, xp_min INTEGER, xp_max INTEGER,
                cooldown_seconds INTEGER, multiplier REAL, voice_xp INTEGER,
                voice_interval_seconds INTEGER, levelup_card_enabled INTEGER, daily_reward INTEGER
            );
            INSERT INTO leveling_settings VALUES
                (1, 1, 15, 25, 60, 1.0, 10, 60, 1, 100),
                (2, 0, 10, 20, 45, 2.0, 5, 30, 0, 50);
            CREATE TABLE leveling_rewards (guild_id INTEGER, level INTEGER, role_id INTEGER);
            INSERT INTO leveling_rewards VALUES (1, 5, 101), (2, 3, 202);
            CREATE TABLE leveling_blacklist (guild_id INTEGER, user_id INTEGER);
            INSERT INTO leveling_blacklist VALUES (1, 11), (2, 22);
            CREATE TABLE leveling_users (
                guild_id INTEGER, user_id INTEGER, xp INTEGER, streak INTEGER,
                last_claim_date TEXT, last_message_xp TEXT, updated_at TEXT, voice_seconds INTEGER
            );
            INSERT INTO leveling_users VALUES
                (1, 11, 500, 4, '2026-11-12', '2026-11-12T10:00:00+00:00', 'now', 360),
                (2, 22, 900, 2, '2026-11-11', '2026-11-11T10:00:00+00:00', 'now', 120);
            """
        )
        owner = SimpleNamespace(id=1, send=AsyncMock())
        bot = SimpleNamespace(
            db=database,
            can_run=AsyncMock(return_value=True),
            is_owner=AsyncMock(return_value=False),
        )
        cog = leveling_features.LevelingFeatures(bot)
        ctx = SimpleNamespace(
            bot=bot,
            author=owner,
            command=None,
            guild=None,
            interaction=None,
            send=AsyncMock(),
        )
        try:
            with pytest.raises(commands.NotOwner):
                await cog.levelbackup.can_run(ctx)

            bot.is_owner.return_value = True
            assert await cog.levelbackup.can_run(ctx)
            await cog.levelbackup.callback(cog, ctx)

            all_file = owner.send.await_args_list[0].kwargs["file"]
            all_backup = json.loads(all_file.fp.getvalue().decode("utf-8"))
            all_file.close()
            assert all_backup["format_version"] == 1
            assert all_backup["data"]["leveling_settings"][0]["guild_id"] == 1
            assert all_backup["data"]["leveling_rewards"][1]["role_id"] == 202
            assert all_backup["data"]["leveling_blacklist"][0]["user_id"] == 11
            assert all_backup["data"]["leveling_users"][0]["streak"] == 4
            assert len(all_backup["data"]["leveling_users"]) == 2

            await cog.levelbackup.callback(cog, ctx, 1)
            guild_file = owner.send.await_args_list[1].kwargs["file"]
            guild_backup = json.loads(guild_file.fp.getvalue().decode("utf-8"))
            guild_file.close()
            assert guild_backup["scope"] == {"guild_id": 1}
            assert all(
                row["guild_id"] == 1
                for rows in guild_backup["data"].values()
                for row in rows
            )
        finally:
            await database.close()

    asyncio.run(verify_leveling_backup())


def test_levelup_card_posts_rank_image_when_enabled() -> None:
    import io

    import leveling_features

    async def verify_levelup_card() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.executescript(
            """
            CREATE TABLE leveling_settings (
                guild_id INTEGER PRIMARY KEY, enabled INTEGER, xp_min INTEGER, xp_max INTEGER,
                cooldown_seconds INTEGER, multiplier REAL, voice_xp INTEGER,
                voice_interval_seconds INTEGER, daily_reward INTEGER, levelup_card_enabled INTEGER
            );
            INSERT INTO leveling_settings VALUES (1, 1, 15, 25, 60, 1.0, 10, 60, 100, 1);
            CREATE TABLE leveling_users (
                guild_id INTEGER, user_id INTEGER, xp INTEGER, streak INTEGER DEFAULT 0,
                last_claim_date TEXT, last_message_xp TEXT, updated_at TEXT, voice_seconds INTEGER DEFAULT 0,
                PRIMARY KEY(guild_id, user_id)
            );
            INSERT INTO leveling_users VALUES (1, 2, 250, 0, NULL, NULL, 'now', 30);
            CREATE TABLE leveling_rewards (guild_id INTEGER, level INTEGER, role_id INTEGER);
            """
        )
        member = SimpleNamespace(
            id=2,
            display_name="Member",
            mention="<@2>",
            roles=[],
            add_roles=AsyncMock(),
        )
        me = SimpleNamespace(top_role=SimpleNamespace())
        guild = SimpleNamespace(
            id=1,
            name="Example Server",
            me=me,
            system_channel=None,
            get_member=lambda _user_id: member,
            get_role=lambda _role_id: None,
        )
        member.guild = guild
        destination = SimpleNamespace(send=AsyncMock())
        cog = leveling_features.LevelingFeatures(SimpleNamespace(db=database))
        card = discord.File(io.BytesIO(b"rank card"), filename="rank.png")
        cog._rank_card = AsyncMock(return_value=card)
        try:
            await cog._grant_level_rewards(guild, member.id, 0, 1, channel=destination)
            cog._rank_card.assert_awaited_once()
            destination.send.assert_awaited_once()
            assert destination.send.await_args.kwargs["content"] == "<@2> leveled up!"
            assert destination.send.await_args.kwargs["embed"].title == "Level Up! Member reached Level 1"
            assert destination.send.await_args.kwargs["file"] is card

            ctx = SimpleNamespace(guild=guild, interaction=None, send=AsyncMock())
            await cog.levelup_card.callback(cog, ctx, "disable")
            async with database.execute(
                "SELECT levelup_card_enabled FROM leveling_settings WHERE guild_id=1"
            ) as cursor:
                assert await cursor.fetchone() == (0,)
        finally:
            await database.close()

    asyncio.run(verify_levelup_card())


def test_leveling_settings_migrate_levelup_card_flag() -> None:
    import leveling_features

    async def verify_migration() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.execute(
            "CREATE TABLE leveling_settings (guild_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0, "
            "xp_min INTEGER NOT NULL DEFAULT 15, xp_max INTEGER NOT NULL DEFAULT 25, "
            "cooldown_seconds INTEGER NOT NULL DEFAULT 60, multiplier REAL NOT NULL DEFAULT 1.0, "
            "voice_xp INTEGER NOT NULL DEFAULT 10, voice_interval_seconds INTEGER NOT NULL DEFAULT 60, "
            "daily_reward INTEGER NOT NULL DEFAULT 100)"
        )
        await database.execute("INSERT INTO leveling_settings (guild_id) VALUES (1)")
        await database.commit()
        cog = leveling_features.LevelingFeatures(SimpleNamespace(db=database))
        cog.voice_worker.start = lambda: None
        try:
            await cog.cog_load()
            settings = await cog._settings(1)
            assert settings["levelup_card_enabled"] == 0
            async with database.execute("PRAGMA table_info(leveling_settings)") as cursor:
                assert "levelup_card_enabled" in {row[1] for row in await cursor.fetchall()}
        finally:
            await cog.cog_unload()
            await database.close()

    asyncio.run(verify_migration())


def test_leveling_settings_default_read_does_not_write() -> None:
    import leveling_features

    async def verify_read_only_defaults() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.execute(
            "CREATE TABLE leveling_settings ("
            "guild_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0, "
            "xp_min INTEGER NOT NULL DEFAULT 15, xp_max INTEGER NOT NULL DEFAULT 25, "
            "cooldown_seconds INTEGER NOT NULL DEFAULT 60, multiplier REAL NOT NULL DEFAULT 1.0, "
            "voice_xp INTEGER NOT NULL DEFAULT 10, voice_interval_seconds INTEGER NOT NULL DEFAULT 60, "
            "daily_reward INTEGER NOT NULL DEFAULT 100, levelup_card_enabled INTEGER NOT NULL DEFAULT 0)"
        )
        cog = leveling_features.LevelingFeatures(SimpleNamespace(db=database))
        try:
            settings = await cog._settings(123)
            assert settings["enabled"] == 0
            assert settings["xp_min"] == 15
            async with database.execute("SELECT COUNT(*) FROM leveling_settings") as cursor:
                assert await cursor.fetchone() == (0,)
        finally:
            await database.close()

    asyncio.run(verify_read_only_defaults())


def test_leveling_voice_worker_survives_database_errors(caplog) -> None:
    import leveling_features

    async def verify_worker_error_handling() -> None:
        cog = object.__new__(leveling_features.LevelingFeatures)
        cog._process_voice_worker_iteration = AsyncMock(
            side_effect=aiosqlite.OperationalError("attempt to write a readonly database")
        )
        await leveling_features.LevelingFeatures.voice_worker.coro(cog)
        cog._process_voice_worker_iteration.assert_awaited_once()

    asyncio.run(verify_worker_error_handling())
    assert "retrying next interval" in caplog.text


def test_ticket_close_defers_button_interaction_and_uses_private_followup() -> None:
    import denycord_interaction_features

    async def verify_deferred_close() -> None:
        cog = object.__new__(denycord_interaction_features.DenycordInteractionFeatures)
        state = {"done": False}

        async def defer(**_kwargs):
            state["done"] = True

        response = SimpleNamespace(
            is_done=lambda: state["done"],
            defer=AsyncMock(side_effect=defer),
            send_message=AsyncMock(),
        )
        followup = SimpleNamespace(send=AsyncMock())
        interaction = SimpleNamespace(
            response=response,
            followup=followup,
            guild=SimpleNamespace(id=1),
            channel=SimpleNamespace(id=2),
            user=SimpleNamespace(id=3),
        )

        async def close_after_deferring(_guild, _channel, _actor, respond):
            assert state["done"]
            await respond("Ticket closed")

        cog._close_ticket_for_actor = AsyncMock(side_effect=close_after_deferring)
        await cog.close_ticket(interaction)

        response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        response.send_message.assert_not_awaited()
        followup.send.assert_awaited_once_with("Ticket closed", ephemeral=True)

    asyncio.run(verify_deferred_close())


def test_ticket_delete_refuses_to_delete_open_ticket() -> None:
    import denycord_interaction_features

    async def verify_open_ticket_is_preserved() -> None:
        cog = object.__new__(denycord_interaction_features.DenycordInteractionFeatures)
        cog._ticket_record = AsyncMock(return_value=(2, "open"))
        channel = SimpleNamespace(id=10, delete=AsyncMock())
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            channel=channel,
            author=SimpleNamespace(id=9),
            interaction=None,
            send=AsyncMock(),
        )

        await denycord_interaction_features.DenycordInteractionFeatures.delete_ticket_command.callback(
            cog, ctx
        )

        channel.delete.assert_not_awaited()
        assert "Close the ticket first" in ctx.send.await_args.args[0]

    asyncio.run(verify_open_ticket_is_preserved())


def test_general_channel_lock_and_visibility_preserve_other_overwrites() -> None:
    import main as bot_module

    class FakeChannel:
        def __init__(self, channel_id, channel_type, overwrite):
            self.id = channel_id
            self.type = channel_type
            self.mention = f"<#${channel_id}>"
            self.overwrite = overwrite
            self.guild = SimpleNamespace(id=1)
            self.edits = []

        def overwrites_for(self, _target):
            return self.overwrite

        async def set_permissions(self, target, *, overwrite, reason):
            self.edits.append((target, overwrite, reason))
            self.overwrite = overwrite or discord.PermissionOverwrite()

    async def verify_overwrites() -> None:
        default_role = object()
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1, default_role=default_role),
            author=SimpleNamespace(id=7),
            send=AsyncMock(),
        )
        text_channel = FakeChannel(
            10,
            discord.ChannelType.text,
            discord.PermissionOverwrite(view_channel=True, connect=True),
        )
        await bot_module._set_channel_access_state(ctx, text_channel, "lock")
        assert text_channel.overwrite.send_messages is False
        assert text_channel.overwrite.connect is True
        assert text_channel.overwrite.view_channel is True

        await bot_module._set_channel_access_state(ctx, text_channel, "unlock")
        assert text_channel.overwrite.send_messages is None
        assert text_channel.overwrite.connect is True
        await bot_module._set_channel_access_state(ctx, text_channel, "hide")
        assert text_channel.overwrite.view_channel is False
        assert text_channel.overwrite.connect is True
        await bot_module._set_channel_access_state(ctx, text_channel, "unhide")
        assert text_channel.overwrite.view_channel is None
        assert text_channel.overwrite.connect is True

        voice_channel = FakeChannel(
            20,
            discord.ChannelType.voice,
            discord.PermissionOverwrite(view_channel=True, send_messages=True),
        )
        await bot_module._set_channel_access_state(ctx, voice_channel, "lock")
        assert voice_channel.overwrite.connect is False
        assert voice_channel.overwrite.send_messages is True
        assert voice_channel.overwrite.view_channel is True

    asyncio.run(verify_overwrites())


def test_saved_item_lists_use_pagination_controls() -> None:
    from community_features import CommunityFeatures

    async def verify_pagination() -> None:
        cog = CommunityFeatures(SimpleNamespace())
        ctx = SimpleNamespace(
            author=SimpleNamespace(id=1),
            interaction=None,
            send=AsyncMock(),
        )
        await cog._send_paged_list(ctx, "Saved Items", [f"Item {index}" for index in range(11)])
        sent_embed = ctx.send.await_args.kwargs["embed"]
        view = ctx.send.await_args.kwargs["view"]
        buttons = {button.label: button for button in view.children}
        assert sent_embed.footer.text == "Page 1/2"
        assert set(buttons) == {"Previous", "Next", "Close"}
        assert buttons["Previous"].disabled
        assert not buttons["Next"].disabled

    asyncio.run(verify_pagination())


def test_tempvoice_slash_groups_and_panel_cover_supported_controls() -> None:
    import denycord_voice_backup

    voice_commands = {command.name for command in denycord_voice_backup.DenycordVoiceBackup.voice.commands}
    settings_commands = {
        command.name for command in denycord_voice_backup.DenycordVoiceBackup.voice_settings.commands
    }
    admin_commands = {
        command.name for command in denycord_voice_backup.DenycordVoiceBackup.tempvoice.commands
    }
    assert voice_commands == {
        "settings", "name", "limit", "lock", "unlock", "hide", "unhide", "status",
        "delete", "reset", "interface", "permit", "reject", "trust", "untrust",
        "block", "unblock", "kick", "mute", "unmute", "deafen", "undeafen",
        "transfer", "claim", "invite",
    }
    assert settings_commands == {"bitrate", "region", "textchat"}
    assert admin_commands == {
        "setup", "set-generator", "set-category", "set-panel", "set-staff",
        "config", "stats", "cleanup", "backup", "restore", "disable", "enable",
    }

    cog = denycord_voice_backup.DenycordVoiceBackup(SimpleNamespace(user=None))
    view = cog.room_panel
    buttons = [
        child for child in view.walk_children()
        if isinstance(child, discord.ui.Button)
    ]
    button_actions = {
        button.custom_id.removeprefix("denycord:room:").partition(":")[0]
        for button in buttons
    }
    assert button_actions >= {
        "lock", "unlock", "hide", "unhide", "rename", "limit", "permit",
        "unblock", "trust", "untrust", "transfer", "claim", "kick", "delete",
        "refresh", "openchat", "closechat", "status", "reset", "invite",
    }
    assert len(buttons) == len(denycord_voice_backup.ROOM_PANEL_BUTTONS)
    assert all(button.label is None and button.emoji.id for button in buttons)
    assert len({button.emoji.id for button in buttons}) == len(buttons)
    for emoji_key, _name, _action, _style, _needs_modal in denycord_voice_backup.ROOM_PANEL_BUTTONS:
        button = next(
            button for button in buttons
            if button.custom_id.endswith(f":{emoji_key}")
        )
        assert button.style is discord.ButtonStyle.secondary
    assert not any(
        isinstance(child, discord.ui.Select)
        for child in view.walk_children()
    )
    assert len(list(view.walk_children())) <= 40
    assert all(
        len(row.children) <= denycord_voice_backup.ROOM_PANEL_BUTTONS_PER_ROW
        for row in view.children[0].children
        if isinstance(row, discord.ui.ActionRow)
    )
    assert isinstance(view.children[0], discord.ui.Container)
    assert len(view.children) >= 2
    assert all(
        isinstance(child, discord.ui.ActionRow)
        for child in view.children[1:]
    )
    assert {button.style for button in buttons} == {discord.ButtonStyle.secondary}


def test_tempvoice_button_emoji_manifest_contains_all_supplied_icons() -> None:
    import denycord_voice_backup

    assert set(denycord_voice_backup.VOICE_BUTTON_EMOJIS) == {
        "lock", "unlock", "secure", "private", "hide", "unhide", "unban",
        "mute", "unmute", "block", "permit", "claim", "transfer", "rename",
        "ban",
        "limit", "delete", "setting", "invite", "kick", "chat", "announce",
        "info", "premium", "fast", "active", "voice_support", "unlimited",
        "love", "start",
    }
    assert all(emoji.id is not None for emoji in denycord_voice_backup.VOICE_BUTTON_EMOJIS.values())


def test_tempvoice_placeholder_emoji_assets_are_valid_pngs() -> None:
    import denycord_voice_backup

    fallback = denycord_voice_backup._placeholder_emoji_bytes("L")
    with Image.open(io.BytesIO(fallback)) as image:
        image.load()
    assert image.size == (64, 64)


def test_tempvoice_custom_emoji_buttons_dispatch_actions() -> None:
    import denycord_voice_backup

    async def verify_callbacks() -> None:
        cog = denycord_voice_backup.DenycordVoiceBackup(SimpleNamespace(user=None))
        cog._handle_room_interaction = AsyncMock()
        response = SimpleNamespace(
            send_modal=AsyncMock(),
        )
        interaction = SimpleNamespace(response=response)
        buttons_by_key = {
            emoji_key: button
            for button in cog.room_panel.walk_children()
            if isinstance(button, discord.ui.Button)
            for emoji_key, _name, _action, _style, _needs_modal
            in denycord_voice_backup.ROOM_PANEL_BUTTONS
            if button.custom_id.endswith(f":{emoji_key}")
        }

        for emoji_key, _name, action, _style, needs_modal in denycord_voice_backup.ROOM_PANEL_BUTTONS:
            cog._handle_room_interaction.reset_mock()
            response.send_modal.reset_mock()
            await buttons_by_key[emoji_key].callback(interaction)
            if needs_modal:
                response.send_modal.assert_awaited_once()
                assert response.send_modal.await_args.args[0].action == action
                cog._handle_room_interaction.assert_not_awaited()
            else:
                cog._handle_room_interaction.assert_awaited_once_with(interaction, action)
                response.send_modal.assert_not_awaited()

    asyncio.run(verify_callbacks())


def test_tempvoice_config_rejects_invalid_values_without_crashing(tmp_path) -> None:
    import denycord_voice_backup

    config_path = tmp_path / "config.yml"
    config_path.write_text(
        "tempvoice:\n"
        "  default_user_limit: invalid\n"
        "  default_bitrate_kbps: 1000\n"
        "  default_region: moon\n"
        "  creation_cooldown_seconds: never\n"
        "  max_temp_channels_per_guild: 0\n"
        "  claim_timeout_seconds: forever\n"
        "  auto_delete_empty: yes\n"
        "  allowed_controls: [slowmode, lock]\n"
        "  theme: broken\n",
        encoding="utf-8",
    )
    settings = denycord_voice_backup.load_tempvoice_config(config_path)["tempvoice"]
    assert settings["default_user_limit"] == 0
    assert settings["default_bitrate_kbps"] == 384
    assert settings["default_region"] == "auto"
    assert settings["creation_cooldown_seconds"] == 5
    assert settings["max_temp_channels_per_guild"] == 1
    assert settings["claim_timeout_seconds"] == 0
    assert settings["auto_delete_empty"] is True
    assert settings["allowed_controls"] == ["lock"]
    assert settings["theme"]["embed_color"] == 0xFFDD00


def test_tempvoice_delete_confirmation_view_is_sent_ephemerally() -> None:
    import denycord_voice_backup

    async def verify_delete_confirmation() -> None:
        user = Mock(spec=discord.Member)
        user.id = 24
        user.voice = None
        guild = SimpleNamespace(id=10)
        response = SimpleNamespace(
            is_done=lambda: False,
            send_message=AsyncMock(),
        )
        interaction = SimpleNamespace(
            guild=guild,
            user=user,
            response=response,
            followup=SimpleNamespace(send=AsyncMock()),
        )
        cog = denycord_voice_backup.DenycordVoiceBackup(SimpleNamespace())
        confirmation = discord.ui.View(timeout=10)
        cog._perform_room_action = AsyncMock(return_value=confirmation)
        cog._audit_room_action = AsyncMock()

        await cog._handle_room_interaction(interaction, "delete")

        response.send_message.assert_awaited_once()
        kwargs = response.send_message.await_args.kwargs
        assert kwargs["ephemeral"] is True
        assert kwargs["view"] is confirmation
        assert "Confirm deleting" in kwargs["content"]

    asyncio.run(verify_delete_confirmation())


def test_tempvoice_restore_validates_and_replaces_only_current_guild_data() -> None:
    import json
    import denycord_voice_backup

    async def verify_restore() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.executescript(
            """
            CREATE TABLE denycord_voice_settings (
                guild_id INTEGER PRIMARY KEY, hub_channel_id INTEGER NOT NULL,
                panel_channel_id INTEGER NOT NULL, category_id INTEGER,
                interface_message_id INTEGER, enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE denycord_voice_generators (
                guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, category_id INTEGER,
                created_by INTEGER, created_at TEXT NOT NULL,
                PRIMARY KEY(guild_id, channel_id)
            );
            CREATE TABLE denycord_voice_staff_roles (
                guild_id INTEGER NOT NULL, role_id INTEGER NOT NULL, created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL, PRIMARY KEY(guild_id, role_id)
            );
            CREATE TABLE denycord_voice_rooms (
                guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, owner_id INTEGER NOT NULL,
                created_at TEXT NOT NULL, locked INTEGER NOT NULL DEFAULT 1,
                user_limit INTEGER NOT NULL DEFAULT 0, rtc_region TEXT,
                chat_open INTEGER NOT NULL DEFAULT 1, hidden INTEGER NOT NULL DEFAULT 0,
                panel_message_id INTEGER, PRIMARY KEY(guild_id, channel_id)
            );
            CREATE TABLE denycord_voice_overrides (
                guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                allow_connect INTEGER, allow_chat INTEGER,
                PRIMARY KEY(guild_id, channel_id, user_id)
            );
            INSERT INTO denycord_voice_settings VALUES (5, 1, 2, NULL, NULL, 0);
            INSERT INTO denycord_voice_settings VALUES (6, 11, 12, NULL, NULL, 1);
            """
        )
        generator = Mock(spec=discord.VoiceChannel)
        panel = Mock(spec=discord.TextChannel)
        room = Mock(spec=discord.VoiceChannel)
        generator.id = 10
        panel.id = 20
        room.id = 30
        channels = {10: generator, 20: panel, 30: room}
        role = SimpleNamespace(id=40)
        guild = SimpleNamespace(
            id=5,
            get_channel=lambda channel_id: channels.get(channel_id),
            get_role=lambda role_id: role if role_id == 40 else None,
        )
        user = Mock(spec=discord.Member)
        user.guild_permissions = SimpleNamespace(manage_guild=True)
        response = SimpleNamespace(send_message=AsyncMock())
        interaction = SimpleNamespace(
            guild=guild,
            user=user,
            response=response,
        )
        payload = {
            "format": "falcon-tempvoice-v1",
            "guild_id": 5,
            "settings": [10, 20, None, 700, 1],
            "generators": [[10, None, 9, "2026-01-01T00:00:00+00:00"]],
            "staff_roles": [[40, 9, "2026-01-01T00:00:00+00:00"]],
            "rooms": [[30, 11, "2026-01-01T00:00:00+00:00", 0, 0, None, 1, 0, None]],
            "overrides": [[30, 12, 0, 0]],
        }
        attachment = SimpleNamespace(
            size=len(json.dumps(payload).encode("utf-8")),
            read=AsyncMock(return_value=json.dumps(payload).encode("utf-8")),
        )
        cog = denycord_voice_backup.DenycordVoiceBackup(SimpleNamespace(db=database))
        try:
            await denycord_voice_backup.DenycordVoiceBackup.tempvoice_restore.callback(
                cog,
                interaction,
                attachment,
            )
            async with database.execute(
                "SELECT hub_channel_id, panel_channel_id, enabled "
                "FROM denycord_voice_settings WHERE guild_id=5"
            ) as cursor:
                assert await cursor.fetchone() == (10, 20, 1)
            async with database.execute(
                "SELECT hub_channel_id FROM denycord_voice_settings WHERE guild_id=6"
            ) as cursor:
                assert await cursor.fetchone() == (11,)
            async with database.execute(
                "SELECT owner_id FROM denycord_voice_rooms WHERE guild_id=5 AND channel_id=30"
            ) as cursor:
                assert await cursor.fetchone() == (11,)
            async with database.execute(
                "SELECT allow_connect, allow_chat FROM denycord_voice_overrides "
                "WHERE guild_id=5 AND channel_id=30 AND user_id=12"
            ) as cursor:
                assert await cursor.fetchone() == (0, 0)
            response.send_message.assert_awaited_once()
            assert response.send_message.await_args.kwargs["ephemeral"] is True
        finally:
            await database.close()

    asyncio.run(verify_restore())


def test_tempvoice_setup_persists_generator_panel_and_category() -> None:
    import denycord_voice_backup

    async def verify_setup() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.executescript(
            """
            CREATE TABLE denycord_voice_settings (
                guild_id INTEGER PRIMARY KEY, hub_channel_id INTEGER NOT NULL,
                panel_channel_id INTEGER NOT NULL, category_id INTEGER,
                interface_message_id INTEGER, enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE denycord_voice_generators (
                guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, category_id INTEGER,
                created_by INTEGER, created_at TEXT NOT NULL,
                PRIMARY KEY(guild_id, channel_id)
            );
            """
        )
        category = SimpleNamespace(id=30, name="Voice")
        generator = Mock(spec=discord.VoiceChannel)
        generator.id = 10
        generator.category = category
        generator.mention = "<#10>"
        generator.permissions_for = Mock(
            return_value=SimpleNamespace(
                view_channel=True,
                connect=True,
                move_members=True,
            )
        )
        panel = Mock(spec=discord.TextChannel)
        panel.id = 20
        panel.mention = "<#20>"
        panel.permissions_for = Mock(
            return_value=SimpleNamespace(
                view_channel=True,
                send_messages=True,
                embed_links=True,
                attach_files=True,
            )
        )
        bot_member = SimpleNamespace(
            guild_permissions=discord.Permissions(manage_channels=True, move_members=True)
        )
        guild = SimpleNamespace(id=5, me=bot_member)
        category.guild = guild
        member = Mock(spec=discord.Member)
        member.id = 7
        member.guild_permissions = SimpleNamespace(manage_guild=True)
        interaction = SimpleNamespace(
            guild=guild,
            user=member,
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        cog = denycord_voice_backup.DenycordVoiceBackup(SimpleNamespace(db=database))
        cog._ensure_voice_interface = AsyncMock(return_value=500)
        try:
            await denycord_voice_backup.DenycordVoiceBackup.tempvoice_setup.callback(
                cog,
                interaction,
                generator,
                panel,
            )
            async with database.execute(
                "SELECT hub_channel_id, panel_channel_id, category_id, interface_message_id, enabled "
                "FROM denycord_voice_settings WHERE guild_id=5"
            ) as cursor:
                assert await cursor.fetchone() == (10, 20, 30, 500, 1)
            async with database.execute(
                "SELECT channel_id, category_id, created_by FROM denycord_voice_generators "
                "WHERE guild_id=5"
            ) as cursor:
                assert await cursor.fetchone() == (10, 30, 7)
            cog._ensure_voice_interface.assert_awaited_once()
            interaction.response.send_message.assert_awaited_once()
            assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True
        finally:
            await database.close()

    asyncio.run(verify_setup())


def test_tempvoice_claim_transfer_and_oldest_member_auto_transfer(monkeypatch) -> None:
    import datetime
    import denycord_voice_backup

    async def verify_ownership() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.executescript(
            """
            CREATE TABLE denycord_voice_rooms (
                guild_id INTEGER, channel_id INTEGER, owner_id INTEGER, created_at TEXT,
                locked INTEGER, user_limit INTEGER, rtc_region TEXT, chat_open INTEGER,
                hidden INTEGER, panel_message_id INTEGER,
                PRIMARY KEY(guild_id, channel_id)
            );
            CREATE TABLE denycord_voice_staff_roles (
                guild_id INTEGER, role_id INTEGER, created_by INTEGER, created_at TEXT,
                PRIMARY KEY(guild_id, role_id)
            );
            INSERT INTO denycord_voice_rooms VALUES
                (5, 30, 2, '2026-01-01T00:00:00+00:00', 0, 0, NULL, 1, 0, NULL);
            """
        )
        old_owner = SimpleNamespace(id=2, bot=False, roles=[], guild_permissions=discord.Permissions.none())
        claimant = SimpleNamespace(id=3, bot=False, roles=[], guild_permissions=discord.Permissions.none())
        recipient = SimpleNamespace(id=4, bot=False, roles=[], guild_permissions=discord.Permissions.none())
        oldest = SimpleNamespace(
            id=5,
            bot=False,
            joined_at=datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc),
        )
        newer = SimpleNamespace(
            id=6,
            bot=False,
            joined_at=datetime.datetime(2022, 1, 1, tzinfo=datetime.timezone.utc),
        )
        bot_member = SimpleNamespace(
            id=99,
            guild_permissions=discord.Permissions(manage_channels=True),
        )
        permissions = SimpleNamespace(manage_channels=True)
        channel = SimpleNamespace(
            id=30,
            name="Room",
            mention="<#30>",
            members=[old_owner, claimant, recipient],
            overwrites={},
            permissions_for=lambda _member: permissions,
            set_permissions=AsyncMock(),
        )
        guild = SimpleNamespace(
            id=5,
            owner_id=1,
            me=bot_member,
            default_role=object(),
            get_member=lambda user_id: {
                2: old_owner,
                3: claimant,
                4: recipient,
                5: oldest,
                6: newer,
            }.get(user_id),
        )
        channel.guild = guild
        for member in (old_owner, claimant, recipient):
            member.voice = SimpleNamespace(channel=channel)
            member.mention = f"<@{member.id}>"
            member.guild = guild
        bot = SimpleNamespace(db=database)
        cog = denycord_voice_backup.DenycordVoiceBackup(bot)
        cog._audit_room_action = AsyncMock()
        monkeypatch.setitem(
            denycord_voice_backup.TEMPVOICE_CONFIG,
            "ownership_transfer_policy",
            "claim_after_owner_leaves",
        )
        try:
            blocked_claim = await cog._perform_room_action(guild, claimant, "claim")
            assert "still in the room" in blocked_claim
            channel.members.remove(old_owner)
            claimed = await cog._perform_room_action(guild, claimant, "claim")
            assert "now own" in claimed
            async with database.execute(
                "SELECT owner_id FROM denycord_voice_rooms WHERE guild_id=5 AND channel_id=30"
            ) as cursor:
                assert await cursor.fetchone() == (3,)

            transferred = await cog._perform_room_action(guild, claimant, "transfer", recipient.id)
            assert "transferred" in transferred
            async with database.execute(
                "SELECT owner_id FROM denycord_voice_rooms WHERE guild_id=5 AND channel_id=30"
            ) as cursor:
                assert await cursor.fetchone() == (4,)

            channel.members = [recipient, oldest, newer]
            await cog._transfer_owner_after_leave(recipient, channel)
            async with database.execute(
                "SELECT owner_id FROM denycord_voice_rooms WHERE guild_id=5 AND channel_id=30"
            ) as cursor:
                assert await cursor.fetchone() == (5,)
            channel.set_permissions.assert_awaited()
        finally:
            await database.close()

    asyncio.run(verify_ownership())


def test_tempvoice_duplicate_generator_events_are_serialized_per_guild() -> None:
    import denycord_voice_backup

    async def verify_duplicate_events() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.executescript(
            """
            CREATE TABLE denycord_voice_settings (
                guild_id INTEGER PRIMARY KEY, hub_channel_id INTEGER NOT NULL,
                panel_channel_id INTEGER NOT NULL, category_id INTEGER,
                interface_message_id INTEGER, enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE denycord_voice_generators (
                guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, category_id INTEGER,
                created_by INTEGER, created_at TEXT NOT NULL,
                PRIMARY KEY(guild_id, channel_id)
            );
            CREATE TABLE denycord_voice_rooms (
                guild_id INTEGER, channel_id INTEGER, owner_id INTEGER, created_at TEXT,
                locked INTEGER, user_limit INTEGER, rtc_region TEXT, chat_open INTEGER,
                hidden INTEGER, panel_message_id INTEGER,
                PRIMARY KEY(guild_id, channel_id)
            );
            INSERT INTO denycord_voice_settings VALUES (5, 20, 30, NULL, NULL, 1);
            INSERT INTO denycord_voice_generators VALUES
                (5, 20, NULL, 7, '2026-01-01T00:00:00+00:00');
            """
        )
        generator = SimpleNamespace(id=20)
        guild = SimpleNamespace(id=5)
        member = SimpleNamespace(
            id=7,
            bot=False,
            guild=guild,
            voice=SimpleNamespace(channel=generator),
        )
        cog = denycord_voice_backup.DenycordVoiceBackup(SimpleNamespace(db=database))

        async def create_room(*_args, **_kwargs):
            await asyncio.sleep(0.01)

        cog._create_temp_room = AsyncMock(side_effect=create_room)
        before = SimpleNamespace(channel=None)
        after = SimpleNamespace(channel=generator)
        try:
            await asyncio.gather(
                cog._voice_state_changed(member, before, after),
                cog._voice_state_changed(member, before, after),
            )
            cog._create_temp_room.assert_awaited_once_with(member, None, 20)
        finally:
            await database.close()

    asyncio.run(verify_duplicate_events())


def test_tempvoice_cleanup_removes_empty_rooms_and_stale_records() -> None:
    import denycord_voice_backup

    async def verify_cleanup() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.executescript(
            """
            CREATE TABLE denycord_voice_rooms (
                guild_id INTEGER, channel_id INTEGER, owner_id INTEGER, created_at TEXT,
                locked INTEGER, user_limit INTEGER, rtc_region TEXT, chat_open INTEGER,
                hidden INTEGER, panel_message_id INTEGER,
                PRIMARY KEY(guild_id, channel_id)
            );
            CREATE TABLE denycord_voice_overrides (
                guild_id INTEGER, channel_id INTEGER, user_id INTEGER,
                allow_connect INTEGER, allow_chat INTEGER,
                PRIMARY KEY(guild_id, channel_id, user_id)
            );
            INSERT INTO denycord_voice_rooms VALUES
                (5, 30, 7, '2026-01-01T00:00:00+00:00', 0, 0, NULL, 1, 0, NULL),
                (5, 31, 8, '2026-01-01T00:00:00+00:00', 0, 0, NULL, 1, 0, NULL),
                (6, 60, 9, '2026-01-01T00:00:00+00:00', 0, 0, NULL, 1, 0, NULL);
            INSERT INTO denycord_voice_overrides VALUES (5, 30, 9, 1, 1);
            INSERT INTO denycord_voice_overrides VALUES (5, 31, 10, 0, 0);
            """
        )
        channel = Mock(spec=discord.VoiceChannel)
        channel.id = 30
        channel.members = []
        channel.delete = AsyncMock()
        guild = SimpleNamespace(
            id=5,
            get_channel=lambda channel_id: channel if channel_id == 30 else None,
        )
        other_channel = Mock(spec=discord.VoiceChannel)
        other_channel.id = 60
        other_channel.members = []
        other_channel.delete = AsyncMock()
        other_guild = SimpleNamespace(
            id=6,
            get_channel=lambda channel_id: other_channel if channel_id == 60 else None,
        )
        other_channel.guild = other_guild
        channel.guild = guild
        cog = denycord_voice_backup.DenycordVoiceBackup(
            SimpleNamespace(
                db=database,
                get_guild=lambda guild_id: {5: guild, 6: other_guild}.get(guild_id),
            )
        )
        try:
            cog._rooms_being_created.add((5, 30))
            await cog._cleanup_empty_rooms(5)
            channel.delete.assert_not_awaited()
            other_channel.delete.assert_not_awaited()
            async with database.execute(
                "SELECT COUNT(*) FROM denycord_voice_rooms WHERE guild_id=5"
            ) as cursor:
                assert await cursor.fetchone() == (1,)
            async with database.execute(
                "SELECT COUNT(*) FROM denycord_voice_overrides WHERE guild_id=5"
            ) as cursor:
                assert await cursor.fetchone() == (1,)
            cog._rooms_being_created.discard((5, 30))
            await cog._cleanup_empty_rooms(5)
            channel.delete.assert_awaited_once()
            async with database.execute(
                "SELECT COUNT(*) FROM denycord_voice_rooms WHERE guild_id=5"
            ) as cursor:
                assert await cursor.fetchone() == (0,)
            async with database.execute(
                "SELECT COUNT(*) FROM denycord_voice_overrides WHERE guild_id=5"
            ) as cursor:
                assert await cursor.fetchone() == (0,)
            async with database.execute(
                "SELECT COUNT(*) FROM denycord_voice_rooms WHERE guild_id=6"
            ) as cursor:
                assert await cursor.fetchone() == (1,)
            await cog._cleanup_empty_rooms()
            other_channel.delete.assert_awaited_once()
            async with database.execute(
                "SELECT COUNT(*) FROM denycord_voice_rooms WHERE guild_id=6"
            ) as cursor:
                assert await cursor.fetchone() == (0,)
        finally:
            await database.close()

    asyncio.run(verify_cleanup())


def test_tempvoice_missing_channel_cleanup_skips_delete_request() -> None:
    import denycord_voice_backup

    async def verify_cleanup() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.executescript(
            """
            CREATE TABLE denycord_voice_rooms (
                guild_id INTEGER, channel_id INTEGER, owner_id INTEGER, created_at TEXT,
                locked INTEGER, user_limit INTEGER, rtc_region TEXT, chat_open INTEGER,
                hidden INTEGER, panel_message_id INTEGER,
                PRIMARY KEY(guild_id, channel_id)
            );
            CREATE TABLE denycord_voice_overrides (
                guild_id INTEGER, channel_id INTEGER, user_id INTEGER,
                allow_connect INTEGER, allow_chat INTEGER,
                PRIMARY KEY(guild_id, channel_id, user_id)
            );
            INSERT INTO denycord_voice_rooms VALUES
                (5, 40, 7, '2026-01-01T00:00:00+00:00', 0, 0, NULL, 1, 0, NULL);
            """
        )
        guild = SimpleNamespace(id=5)
        channel = SimpleNamespace(id=40, guild=guild, delete=AsyncMock())
        cog = denycord_voice_backup.DenycordVoiceBackup(SimpleNamespace(db=database))
        cog._rooms_being_created.add((guild.id, channel.id))
        try:
            await cog._remove_room_after_failed_create(channel, delete_channel=False)
            channel.delete.assert_not_awaited()
            assert (guild.id, channel.id) not in cog._rooms_being_created
            async with database.execute(
                "SELECT COUNT(*) FROM denycord_voice_rooms"
            ) as cursor:
                assert await cursor.fetchone() == (0,)
        finally:
            await database.close()

    asyncio.run(verify_cleanup())


def test_accage_shows_calendar_duration_and_user_avatar(monkeypatch) -> None:
    import datetime
    from dateutil.relativedelta import relativedelta
    import main as bot_module

    async def verify_accage() -> None:
        now = datetime.datetime(2025, 11, 11, 12, 30, 45, tzinfo=datetime.timezone.utc)

        class FrozenDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return now if tz is None else now.astimezone(tz)

        user = SimpleNamespace(
            name="shivay001991",
            id=42,
            created_at=now - relativedelta(
                years=1, months=6, weeks=2, days=5, hours=6, minutes=7
            ),
            display_avatar=SimpleNamespace(url="https://example.test/avatar.png"),
        )
        monkeypatch.setattr(bot_module.datetime, "datetime", FrozenDateTime)
        ctx = SimpleNamespace(author=user, send=AsyncMock())

        await bot_module.accage.callback(ctx, user)

        embed = ctx.send.await_args.kwargs["embed"]
        assert embed.title == "shivay001991's account age"
        assert embed.thumbnail.url == user.display_avatar.url
        assert "1 years, 6 months, 2 weeks, 6 days" in embed.description
        assert "hours, " in embed.description
        assert "minutes and " in embed.description
        assert embed.description.endswith(" seconds")

    asyncio.run(verify_accage())


def test_tempvoice_channel_move_failure_cleans_up_created_channel() -> None:
    import denycord_voice_backup

    class FakeMember:
        def __init__(self, member_id, permissions=None):
            self.id = member_id
            self.display_name = "Test"
            self.name = "Test"
            self.guild_permissions = permissions or discord.Permissions.none()
            error_response = SimpleNamespace(
                status=403,
                reason="Forbidden",
                headers={},
            )
            self.move_to = AsyncMock(
                side_effect=discord.Forbidden(error_response, "move denied")
            )

    async def verify_failure_cleanup() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.executescript(
            """
            CREATE TABLE denycord_voice_settings (
                guild_id INTEGER PRIMARY KEY, hub_channel_id INTEGER NOT NULL,
                panel_channel_id INTEGER NOT NULL, category_id INTEGER,
                interface_message_id INTEGER, enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE denycord_voice_rooms (
                guild_id INTEGER, channel_id INTEGER, owner_id INTEGER, created_at TEXT,
                locked INTEGER, user_limit INTEGER, rtc_region TEXT, chat_open INTEGER,
                hidden INTEGER, panel_message_id INTEGER,
                PRIMARY KEY(guild_id, channel_id)
            );
            CREATE TABLE denycord_voice_overrides (
                guild_id INTEGER, channel_id INTEGER, user_id INTEGER,
                allow_connect INTEGER, allow_chat INTEGER,
                PRIMARY KEY(guild_id, channel_id, user_id)
            );
            INSERT INTO denycord_voice_settings VALUES (5, 20, 30, NULL, NULL, 1);
            """
        )
        bot_member = FakeMember(
            99,
            discord.Permissions(manage_channels=True, move_members=True),
        )
        everyone = FakeMember(1)
        hub = SimpleNamespace(
            id=20,
            permissions_for=lambda _member: SimpleNamespace(
                view_channel=True,
                connect=True,
                move_members=True,
            ),
        )
        guild = SimpleNamespace(
            id=5,
            me=bot_member,
            default_role=everyone,
            premium_tier=0,
            get_channel=lambda channel_id: hub if channel_id == 20 else None,
        )
        room = SimpleNamespace(
            id=40,
            guild=guild,
            delete=AsyncMock(),
        )
        guild.create_voice_channel = AsyncMock(return_value=room)
        member = FakeMember(7)
        member.guild = guild
        cog = denycord_voice_backup.DenycordVoiceBackup(SimpleNamespace(db=database, user=None))
        try:
            await cog._create_temp_room(member, None, 20)
            room.delete.assert_awaited_once()
            async with database.execute(
                "SELECT COUNT(*) FROM denycord_voice_rooms WHERE guild_id=5 AND channel_id=40"
            ) as cursor:
                assert await cursor.fetchone() == (0,)
        finally:
            await database.close()

    asyncio.run(verify_failure_cleanup())


def test_is_bot_blacklisted() -> None:
    import main as bot_module

    async def verify_blacklist() -> None:
        bot = bot_module.bot
        original_database = bot.db
        database = await aiosqlite.connect(":memory:")
        bot.db = database
        await database.executescript(
            """
            CREATE TABLE user_blacklist (
                user_id INTEGER PRIMARY KEY, reason TEXT, by_user INTEGER, at TEXT
            );
            INSERT INTO user_blacklist VALUES (42, 'test', 1, '2026-01-01');
            """
        )
        try:
            assert await bot_module.is_bot_blacklisted(42) is True
            assert await bot_module.is_bot_blacklisted(43) is False
            for owner_id in bot_module.PRIMARY_OWNER_IDS:
                await database.execute(
                    "INSERT OR REPLACE INTO user_blacklist VALUES (?, 'test', 1, '2026-01-01')",
                    (owner_id,),
                )
                assert await bot_module.is_bot_blacklisted(owner_id) is False
        finally:
            bot.db = original_database
            await database.close()

    asyncio.run(verify_blacklist())
