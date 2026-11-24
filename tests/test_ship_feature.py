import asyncio
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
from PIL import Image

import community_features
from discord.ext import commands


def test_ship_random_uses_bot_wide_users_and_excludes_bots_and_requester(monkeypatch) -> None:
    async def verify() -> None:
        requester = SimpleNamespace(id=1, bot=False)
        bot_user = SimpleNamespace(id=2, bot=True)
        other_bot = SimpleNamespace(id=3, bot=True)
        candidate = SimpleNamespace(id=4, bot=False)
        bot = SimpleNamespace(
            user=bot_user,
            users=[requester, bot_user, other_bot, candidate],
        )
        cog = community_features.CommunityFeatures(bot)
        cog._send_ship_result = AsyncMock()
        ctx = SimpleNamespace(
            author=requester,
            interaction=None,
            send=AsyncMock(),
        )
        monkeypatch.setattr(community_features.random, "choice", lambda users: users[0])

        await cog._send_random_ship(ctx)

        cog._send_ship_result.assert_awaited_once_with(ctx, requester, candidate)
        ctx.send.assert_not_awaited()

    asyncio.run(verify())


def test_ship_random_handles_empty_global_user_pool() -> None:
    async def verify() -> None:
        requester = SimpleNamespace(id=1, bot=False)
        cog = community_features.CommunityFeatures(
            SimpleNamespace(user=SimpleNamespace(id=2), users=[requester])
        )
        ctx = SimpleNamespace(
            author=requester,
            interaction=None,
            send=AsyncMock(),
        )

        await cog._send_random_ship(ctx)

        ctx.send.assert_awaited_once_with(
            "I couldn't find another non-bot user in the servers I'm in.",
            ephemeral=False,
        )

    asyncio.run(verify())


def test_ship_without_a_target_uses_the_random_global_picker() -> None:
    async def verify() -> None:
        cog = community_features.CommunityFeatures(SimpleNamespace())
        cog._resolve_member_from_context = AsyncMock(return_value=None)
        cog._send_random_ship = AsyncMock()
        ctx = SimpleNamespace(
            author=SimpleNamespace(id=1),
            interaction=None,
        )

        await community_features.CommunityFeatures.ship.callback(cog, ctx)

        cog._resolve_member_from_context.assert_awaited_once_with(ctx)
        cog._send_random_ship.assert_awaited_once_with(ctx)

    asyncio.run(verify())


def test_ship_random_prefix_fallback_only_matches_exact_random_argument() -> None:
    cog = community_features.CommunityFeatures(SimpleNamespace())

    def make_context(content: str):
        return SimpleNamespace(
            message=SimpleNamespace(content=content),
            prefix=",",
            command=SimpleNamespace(name="ship"),
        )

    assert cog._is_ship_random_invocation(make_context(",ship random"))
    assert cog._is_ship_random_invocation(make_context(",ship RANDOM"))
    assert not cog._is_ship_random_invocation(make_context(",ship random-user"))
    assert not cog._is_ship_random_invocation(make_context(",ship random @user"))


def test_ship_command_routes_random_argument_to_global_picker() -> None:
    async def verify() -> None:
        cog = community_features.CommunityFeatures(SimpleNamespace())
        cog._send_random_ship = AsyncMock()
        ctx = SimpleNamespace(
            interaction=None,
            message=SimpleNamespace(content=",ship random"),
            prefix=",",
            command=SimpleNamespace(name="ship"),
        )

        await cog.ship_error(ctx, commands.UserNotFound("random"))

        cog._send_random_ship.assert_awaited_once_with(ctx)

    asyncio.run(verify())


def test_ship_again_button_rerolls_the_card(monkeypatch) -> None:
    async def verify() -> None:
        embed = discord.Embed(title="Updated ship")
        image = discord.File(io.BytesIO(b"image"), filename="falcon-ship.png")
        first_member = SimpleNamespace(id=10)
        second_member = SimpleNamespace(id=20)
        clicker = SimpleNamespace(id=30)
        new_partner = SimpleNamespace(id=40)
        edit_interaction_v2 = AsyncMock()
        monkeypatch.setattr(
            community_features.random,
            "choice",
            lambda users: users[0],
        )
        monkeypatch.setattr(
            community_features,
            "edit_interaction_v2",
            edit_interaction_v2,
        )
        cog = SimpleNamespace(
            _create_ship_result=AsyncMock(return_value=(embed, image)),
            _ship_random_users=AsyncMock(return_value=[new_partner]),
        )
        view = community_features.ShipAgainView(
            cog,
            first_member=first_member,
            second_member=second_member,
        )
        interaction = SimpleNamespace(
            user=clicker,
            response=SimpleNamespace()
        )

        assert view.children[0].label == "Ship Again"
        assert view.children[0].emoji is None
        assert view.children[0].style is discord.ButtonStyle.secondary
        await view.children[0].callback(interaction)

        cog._ship_random_users.assert_awaited_once_with({clicker.id, second_member.id})
        cog._create_ship_result.assert_awaited_once_with(
            clicker,
            new_partner,
        )
        edit_interaction_v2.assert_awaited_once_with(
            interaction,
            embed=embed,
            attachments=[image],
            view=view,
        )
        assert view.first_member is clicker
        assert view.second_member is new_partner

    asyncio.run(verify())


def test_ship_result_embed_uses_gold_accent(monkeypatch) -> None:
    async def verify() -> None:
        cog = community_features.CommunityFeatures(SimpleNamespace())
        cog._download_avatar = AsyncMock(return_value=b"avatar")
        monkeypatch.setattr(asyncio, "to_thread", AsyncMock(return_value=b"card"))
        first_member = SimpleNamespace(display_name="First", mention="<@1>")
        second_member = SimpleNamespace(display_name="Second", mention="<@2>")

        embed, file = await cog._create_ship_result(first_member, second_member)

        assert embed.color.value == 0xFFDD00
        assert file.filename == "falcon-ship.png"

    asyncio.run(verify())


def test_ship_again_requires_another_global_partner() -> None:
    async def verify() -> None:
        first_member = SimpleNamespace(id=10)
        second_member = SimpleNamespace(id=20)
        clicker = SimpleNamespace(id=30)
        cog = SimpleNamespace(
            _ship_random_users=AsyncMock(return_value=[]),
            _create_ship_result=AsyncMock(),
        )
        view = community_features.ShipAgainView(
            cog,
            first_member=first_member,
            second_member=second_member,
        )
        interaction = SimpleNamespace(
            user=clicker,
            response=SimpleNamespace(send_message=AsyncMock())
        )

        await view.children[0].callback(interaction)

        interaction.response.send_message.assert_awaited_once_with(
            "I couldn't find another non-bot user to ship you with.",
            ephemeral=True,
        )
        cog._create_ship_result.assert_not_awaited()

    asyncio.run(verify())


def test_clustered_ship_random_resolves_candidates_from_shared_database() -> None:
    class CandidateCursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def fetchall(self):
            return [(100,)]

    class CandidateDatabase:
        def execute(self, query):
            assert "GROUP BY user_id" in query
            return CandidateCursor()

    candidate = SimpleNamespace(id=100, bot=False)
    bot = SimpleNamespace(
        cluster_count=11,
        user=SimpleNamespace(id=2),
        users=[],
        db=CandidateDatabase(),
        get_user=Mock(return_value=None),
        fetch_user=AsyncMock(return_value=candidate),
    )
    cog = community_features.CommunityFeatures(bot)

    candidates = asyncio.run(cog._ship_random_users({1}))

    assert candidates == [candidate]
    bot.fetch_user.assert_awaited_once_with(100)


def test_ship_card_renders_at_premium_layout_dimensions() -> None:
    avatar_buffer = io.BytesIO()
    Image.new("RGBA", (64, 64), "#d86b91").save(avatar_buffer, format="PNG")
    avatar = avatar_buffer.getvalue()

    card_bytes = community_features.render_ship_card(
        avatar,
        avatar,
        "A Very Long First User Name That Needs Fitting",
        "Second User",
        87,
        "Amazing Chemistry",
    )

    with Image.open(io.BytesIO(card_bytes)) as card:
        assert card.format == "PNG"
        assert card.size == (1600, 900)
        assert min(card.convert("RGB").getpixel((80, 80))) > 150


def test_ship_card_normalizes_stylized_partner_names() -> None:
    avatar_buffer = io.BytesIO()
    Image.new("RGBA", (64, 64), "#d86b91").save(avatar_buffer, format="PNG")
    avatar = avatar_buffer.getvalue()

    stylized_name_card = community_features.render_ship_card(
        avatar,
        avatar,
        "𝕊𝕠𝕡𝕙𝕚𝕒",
        "Jordan Moon",
        87,
        "Amazing Chemistry",
    )
    plain_name_card = community_features.render_ship_card(
        avatar,
        avatar,
        "Sophia",
        "Jordan Moon",
        87,
        "Amazing Chemistry",
    )

    assert stylized_name_card == plain_name_card
