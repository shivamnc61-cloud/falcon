import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
from discord.ext.commands.view import StringView

import main


class FakeGuild:
    def __init__(self, *, members=(), roles=()):
        self.id = 1
        self.members = list(members)
        self.roles = list(roles)
        self._roles = {role.id: role for role in self.roles}
        self._state = SimpleNamespace(
            member_cache_flags=SimpleNamespace(joined=True)
        )

    def get_member(self, member_id):
        return next((member for member in self.members if member.id == member_id), None)

    def get_member_named(self, name):
        return None

    async def query_members(self, _query, *, limit, cache):
        query = _query.casefold()
        return [
            member for member in self.members
            if query in {
                member.name.casefold(),
                member.display_name.casefold(),
                member.global_name.casefold(),
            }
        ][:limit]

    def get_role(self, role_id):
        return self._roles.get(role_id)


def _member(guild, member_id=123456789012345678, name="alice"):
    member = Mock(spec=discord.Member)
    member.id = member_id
    member.name = name
    member.display_name = name.title()
    member.global_name = name.title()
    member.nick = None
    member.guild = guild
    return member


def test_member_and_user_converters_accept_at_name_id_and_mentions() -> None:
    async def verify() -> None:
        guild = FakeGuild()
        member = _member(guild)
        guild.members.append(member)
        ctx = SimpleNamespace(
            guild=guild,
            bot=main.bot,
            message=SimpleNamespace(mentions=[member]),
            _state=SimpleNamespace(_users={}),
        )

        converter = main.UsernameMemberConverter()
        assert await converter.convert(ctx, "@alice") is member
        assert await converter.convert(ctx, f"@{member.id}") is member
        assert await converter.convert(ctx, f"<@{member.id}>") is member
        assert await converter.convert(ctx, f"<@!{member.id}>") is member
        user_converter = main.UsernameUserConverter()
        assert await user_converter.convert(ctx, "@alice") is member
        assert await user_converter.convert(ctx, f"@{member.id}") is member
        assert await user_converter.convert(ctx, f"<@{member.id}>") is member

    asyncio.run(verify())


def test_premium_user_lookup_accepts_at_username_and_at_id() -> None:
    from premium_features import PremiumFeatures

    async def verify() -> None:
        guild = FakeGuild()
        member = _member(guild)
        guild.members.append(member)
        bot = SimpleNamespace(
            owner_ids=set(),
            users=[member],
            guilds=[guild],
            get_user=lambda user_id: member if user_id == member.id else None,
        )
        cog = PremiumFeatures(bot)

        assert await cog._resolve_user("@alice") is member
        assert await cog._resolve_user(f"@{member.id}") is member
        assert await cog._resolve_user(member) is member

    asyncio.run(verify())


def test_role_converter_accepts_at_name_id_and_role_mentions() -> None:
    async def verify() -> None:
        role = SimpleNamespace(id=234567890123456789, name="Family")
        guild = FakeGuild(roles=[role])
        ctx = SimpleNamespace(guild=guild)
        converter = main.UsernameRoleConverter()

        assert await converter.convert(ctx, "@Family") is role
        assert await converter.convert(ctx, f"@{role.id}") is role
        assert await converter.convert(ctx, f"<@&{role.id}>") is role

    asyncio.run(verify())


def test_reply_target_is_injected_for_prefix_commands_without_overriding_explicit_target() -> None:
    async def verify() -> None:
        guild = FakeGuild()
        member = _member(guild)
        guild.members.append(member)
        message = SimpleNamespace(
            reference=SimpleNamespace(resolved=SimpleNamespace(author=member)),
            mentions=[member],
        )
        command = main.bot.get_command("mute")
        ctx = SimpleNamespace(
            bot=main.bot,
            guild=guild,
            message=message,
            channel=SimpleNamespace(fetch_message=AsyncMock()),
            command=command,
            view=StringView(",mute 10m"),
        )
        ctx.view.index = len(",mute")

        await main.bot._inject_replied_member(ctx)
        assert ctx.view.buffer == f",mute <@{member.id}> 10m"
        assert ctx.view.end == len(ctx.view.buffer)

        explicit_ctx = SimpleNamespace(
            bot=main.bot,
            guild=guild,
            message=message,
            channel=ctx.channel,
            command=command,
            view=StringView(f",mute <@{member.id}>"),
        )
        explicit_ctx.view.index = len(",mute")
        await main.bot._inject_replied_member(explicit_ctx)
        assert explicit_ctx.view.buffer == f",mute <@{member.id}>"

    asyncio.run(verify())


def test_reply_target_is_injected_after_nested_prefix_subcommand() -> None:
    from denycord_interaction_features import DenycordInteractionFeatures

    async def verify() -> None:
        guild = FakeGuild()
        member = _member(guild)
        guild.members.append(member)
        cog = DenycordInteractionFeatures(SimpleNamespace())
        ctx = SimpleNamespace(
            bot=main.bot,
            guild=guild,
            message=SimpleNamespace(
                reference=SimpleNamespace(resolved=SimpleNamespace(author=member)),
                mentions=[],
            ),
            channel=SimpleNamespace(fetch_message=AsyncMock()),
            command=cog.ticket,
            view=StringView(",ticket add"),
        )
        ctx.view.index = len(",ticket")

        await main.bot._inject_replied_member(ctx)

        assert ctx.view.buffer == f",ticket add <@{member.id}>"

    asyncio.run(verify())


def test_reply_target_fills_the_next_missing_member_argument() -> None:
    from community_features import CommunityFeatures

    async def verify() -> None:
        guild = FakeGuild()
        explicit_member = _member(guild, member_id=123456789012345678, name="alice")
        replied_member = _member(guild, member_id=223456789012345678, name="bob")
        guild.members.extend((explicit_member, replied_member))
        cog = CommunityFeatures(SimpleNamespace())
        command = next(command for command in cog.get_commands() if command.name == "ship")
        ctx = SimpleNamespace(
            bot=main.bot,
            guild=guild,
            message=SimpleNamespace(
                reference=SimpleNamespace(
                    resolved=SimpleNamespace(author=replied_member)
                ),
                mentions=[explicit_member, replied_member],
            ),
            channel=SimpleNamespace(fetch_message=AsyncMock()),
            command=command,
            view=StringView(f",ship <@{explicit_member.id}>"),
            _state=SimpleNamespace(_users={}),
        )
        ctx.view.index = len(",ship")

        await main.bot._inject_replied_member(ctx)

        assert ctx.view.buffer == (
            f",ship <@{explicit_member.id}> <@{replied_member.id}>"
        )

    asyncio.run(verify())


def test_reply_target_is_injected_after_non_member_arguments() -> None:
    from zyre_features import ZyreFeatures

    async def verify() -> None:
        guild = FakeGuild()
        member = _member(guild)
        guild.members.append(member)
        ctx = SimpleNamespace(
            bot=main.bot,
            guild=guild,
            message=SimpleNamespace(
                reference=SimpleNamespace(resolved=SimpleNamespace(author=member)),
                mentions=[],
            ),
            channel=SimpleNamespace(fetch_message=AsyncMock()),
            command=ZyreFeatures.unwarn,
            view=StringView(",unwarn 42"),
        )
        ctx.view.index = len(",unwarn")

        await main.bot._inject_replied_member(ctx)

        assert ctx.view.buffer == f",unwarn 42 <@{member.id}>"

    asyncio.run(verify())


def test_tagreply_pings_an_explicit_member() -> None:
    async def verify() -> None:
        guild = FakeGuild()
        member = _member(guild)
        guild.members.append(member)
        ctx = SimpleNamespace(
            guild=guild,
            message=SimpleNamespace(
                reference=SimpleNamespace(resolved=SimpleNamespace(author=member)),
                mentions=[],
            ),
            channel=SimpleNamespace(fetch_message=AsyncMock()),
            send=AsyncMock(),
        )

        await main.tagreply.callback(ctx)

        ctx.send.assert_awaited_once()
        assert ctx.send.await_args.args == (member.mention,)
        assert ctx.send.await_args.kwargs["allowed_mentions"].users == [member]

        ctx.send.reset_mock()
        await main.tagreply.callback(ctx, member)
        ctx.send.assert_awaited_once()
        assert ctx.send.await_args.args == (member.mention,)
        assert ctx.send.await_args.kwargs["allowed_mentions"].users == [member]

    asyncio.run(verify())
