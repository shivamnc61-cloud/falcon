import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
from pathlib import Path

import discord

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from community_features import CommunityFeatures
import main as bot_module


def _role_context():
    role = SimpleNamespace(id=12345678901234567, name="Community", mention="<@&12345678901234567>")
    guild = SimpleNamespace(
        roles=[role],
        get_role=lambda role_id: role if role_id == role.id else None,
    )
    ctx = SimpleNamespace(guild=guild, interaction=None, send=AsyncMock())
    return ctx, role


def test_autorole_add_accepts_human_and_bot_prefixes(monkeypatch) -> None:
    async def verify() -> None:
        configured = AsyncMock()
        monkeypatch.setattr(bot_module, "setautorole", configured)
        ctx, role = _role_context()

        await bot_module.autorole_add_subcommand.callback(ctx, arguments=f"human {role.mention}")
        configured.assert_awaited_once_with(ctx, role, "humans")

        configured.reset_mock()
        await bot_module.autorole_add_subcommand.callback(ctx, arguments=f"bot {role.mention}")
        configured.assert_awaited_once_with(ctx, role, "bots")

    asyncio.run(verify())


def test_autorole_add_keeps_all_member_form() -> None:
    async def verify() -> None:
        configured = AsyncMock()
        original = bot_module.setautorole
        bot_module.setautorole = configured
        ctx, role = _role_context()
        try:
            await bot_module.autorole_add_subcommand.callback(ctx, arguments=role.mention)
            configured.assert_awaited_once_with(ctx, role, "all")
        finally:
            bot_module.setautorole = original

    asyncio.run(verify())


def test_roleall_targets_requested_member_type() -> None:
    async def verify() -> None:
        bot = SimpleNamespace()
        cog = CommunityFeatures(bot)
        cog._bulk_assign_role = AsyncMock()
        ctx, role = _role_context()

        await cog.roleall.callback(cog, ctx, arguments=f"human {role.mention}")
        cog._bulk_assign_role.assert_awaited_once_with(ctx, role, bots=False)

        cog._bulk_assign_role.reset_mock()
        await cog.roleall.callback(cog, ctx, arguments=f"bot {role.mention}")
        cog._bulk_assign_role.assert_awaited_once_with(ctx, role, bots=True)

        cog._bulk_assign_role.reset_mock()
        await cog.roleall.callback(cog, ctx, arguments=role.mention)
        cog._bulk_assign_role.assert_awaited_once_with(ctx, role)

    asyncio.run(verify())


def test_roleall_nested_add_and_remove_targets() -> None:
    async def verify() -> None:
        cog = CommunityFeatures(SimpleNamespace())
        cog._bulk_assign_role = AsyncMock()
        ctx, role = _role_context()

        await cog.roleall_human_add.callback(cog, ctx, role)
        cog._bulk_assign_role.assert_awaited_once_with(ctx, role, bots=False, add=True)

        cog._bulk_assign_role.reset_mock()
        await cog.roleall_human_remove.callback(cog, ctx, role)
        cog._bulk_assign_role.assert_awaited_once_with(ctx, role, bots=False, add=False)

        cog._bulk_assign_role.reset_mock()
        await cog.roleall_bot_add.callback(cog, ctx, role)
        cog._bulk_assign_role.assert_awaited_once_with(ctx, role, bots=True, add=True)

        cog._bulk_assign_role.reset_mock()
        await cog.roleall_bot_remove.callback(cog, ctx, role)
        cog._bulk_assign_role.assert_awaited_once_with(ctx, role, bots=True, add=False)

    asyncio.run(verify())


def test_bulk_role_helper_changes_only_selected_member_type() -> None:
    async def verify() -> None:
        cog = CommunityFeatures(SimpleNamespace())
        cog._check_role_hierarchy = AsyncMock()
        role = SimpleNamespace(mention="<@&12345678901234567>")
        human = SimpleNamespace(
            bot=False,
            roles=[],
            add_roles=AsyncMock(),
            remove_roles=AsyncMock(),
        )
        bot_member = SimpleNamespace(
            bot=True,
            roles=[role],
            add_roles=AsyncMock(),
            remove_roles=AsyncMock(),
        )
        status = SimpleNamespace(
            flags=SimpleNamespace(is_components_v2=True),
            edit=AsyncMock(),
        )
        ctx = SimpleNamespace(
            guild=SimpleNamespace(chunked=True, members=[human, bot_member]),
            author="Moderator",
            interaction=None,
            send=AsyncMock(return_value=status),
        )

        await cog._bulk_assign_role(ctx, role, bots=False, add=True)
        human.add_roles.assert_awaited_once()
        bot_member.add_roles.assert_not_awaited()

        ctx.send.reset_mock()
        status.edit.reset_mock()
        await cog._bulk_assign_role(ctx, role, bots=True, add=False)
        bot_member.remove_roles.assert_awaited_once()
        human.remove_roles.assert_not_awaited()
        status.edit.assert_awaited_once()
        edit_kwargs = status.edit.await_args.kwargs
        assert "content" not in edit_kwargs
        assert isinstance(edit_kwargs["view"], discord.ui.LayoutView)
        assert any(
            isinstance(child, discord.ui.TextDisplay)
            for container in edit_kwargs["view"].children
            for child in container.children
        )

    asyncio.run(verify())