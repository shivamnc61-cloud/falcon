import asyncio
from types import SimpleNamespace

import aiosqlite

from denycord_server_automation import DenycordServerAutomation


class RoleStub:
    def __init__(self, role_id: int):
        self.id = role_id

    def __ge__(self, _other) -> bool:
        return False


async def _make_cog(member_has_role: bool = False):
    db = await aiosqlite.connect(":memory:")
    await db.executescript(
        """
        CREATE TABLE denycord_voice_roles (guild_id INTEGER, role_id INTEGER);
        CREATE TABLE denycord_voice_role_grants (
            guild_id INTEGER, user_id INTEGER, role_id INTEGER,
            PRIMARY KEY (guild_id, user_id, role_id)
        );
        INSERT INTO denycord_voice_roles VALUES (1, 5);
        """
    )
    role = RoleStub(5)
    guild = SimpleNamespace(
        id=1,
        me=SimpleNamespace(
            guild_permissions=SimpleNamespace(manage_roles=True),
            top_role=object(),
        ),
        get_role=lambda role_id: role if role_id == role.id else None,
    )
    member = SimpleNamespace(id=9, guild=guild, roles=[role] if member_has_role else [])

    async def add_roles(target, **_kwargs):
        member.roles.append(target)

    async def remove_roles(target, **_kwargs):
        member.roles.remove(target)

    member.add_roles = add_roles
    member.remove_roles = remove_roles
    return db, DenycordServerAutomation(SimpleNamespace(db=db)), member, role


def test_voice_role_is_added_on_join_and_removed_on_leave() -> None:
    async def verify() -> None:
        db, cog, member, role = await _make_cog()
        try:
            await cog.on_voice_state_update(
                member,
                SimpleNamespace(channel=None),
                SimpleNamespace(channel=object()),
            )
            assert role in member.roles
            async with db.execute("SELECT role_id FROM denycord_voice_role_grants") as cursor:
                assert await cursor.fetchone() == (role.id,)

            await cog.on_voice_state_update(
                member,
                SimpleNamespace(channel=object()),
                SimpleNamespace(channel=None),
            )
            assert role not in member.roles
            async with db.execute("SELECT role_id FROM denycord_voice_role_grants") as cursor:
                assert await cursor.fetchone() is None
        finally:
            await db.close()

    asyncio.run(verify())


def test_voice_role_does_not_remove_role_member_already_had() -> None:
    async def verify() -> None:
        db, cog, member, role = await _make_cog(member_has_role=True)
        try:
            await cog.on_voice_state_update(
                member,
                SimpleNamespace(channel=None),
                SimpleNamespace(channel=object()),
            )
            await cog.on_voice_state_update(
                member,
                SimpleNamespace(channel=object()),
                SimpleNamespace(channel=None),
            )
            assert role in member.roles
            async with db.execute("SELECT role_id FROM denycord_voice_role_grants") as cursor:
                assert await cursor.fetchone() is None
        finally:
            await db.close()

    asyncio.run(verify())