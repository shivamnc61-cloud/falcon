import asyncio
import datetime
import os
import re
from contextlib import asynccontextmanager

import aiosqlite
import discord
from discord.ext import commands, tasks

from cluster_runtime import shared_advisory_lock
from components_v2 import send_v2
from time_parser import parse_duration


UTC = datetime.timezone.utc
LIFETIME_EXPIRY = "9999-12-31T00:00:00+00:00"
USER_ID_PATTERN = re.compile(r"^(?:<@!?(\d{15,22})>|(\d{15,22}))$")
PREMIUM_OPERATION_LOCK_ID = 0x5052454D49554D


def utc_now():
    return datetime.datetime.now(UTC)


def parse_stored_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def format_expiry(value):
    if value is None:
        return "Permanent"
    expiry = parse_stored_datetime(value)
    if expiry.year >= 9999:
        return "Permanent"
    return f"<t:{int(expiry.timestamp())}:F> (<t:{int(expiry.timestamp())}:R>)"


async def ensure_column(db, table, column, declaration):
    async with db.execute(f"PRAGMA table_info({table})") as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    if column not in columns:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


class PremiumFeatures(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.premium_lock = asyncio.Lock()

    @asynccontextmanager
    async def _premium_operation_lock(self):
        async with self.premium_lock:
            async with shared_advisory_lock(self.bot, PREMIUM_OPERATION_LOCK_ID):
                yield

    async def _fetch_guild(self, guild_id):
        guild = self.bot.get_guild(guild_id)
        if guild is not None:
            return guild
        try:
            return await self.bot.fetch_guild(guild_id)
        except (discord.NotFound, discord.Forbidden):
            return None

    async def cog_load(self):
        db = self.bot.db
        await db.execute(
            """CREATE TABLE IF NOT EXISTS premium_allocations (
                allocation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                granted_by INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                expires_at TEXT,
                guild_limit INTEGER NOT NULL CHECK(guild_limit > 0),
                status TEXT NOT NULL DEFAULT 'active'
            )"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS premium_activations (
                activation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                allocation_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                owner_id INTEGER NOT NULL,
                activated_at TEXT NOT NULL,
                revoked_at TEXT,
                status TEXT NOT NULL DEFAULT 'active'
            )"""
        )
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS one_active_premium_per_guild "
            "ON premium_activations(guild_id) WHERE status='active'"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS premium_allocations_by_user "
            "ON premium_allocations(user_id, status, expires_at)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS premium_activations_by_allocation "
            "ON premium_activations(allocation_id, status)"
        )
        for column, declaration in (
            ("owner_id", "INTEGER"),
            ("allocation_id", "INTEGER"),
            ("activated_at", "TEXT"),
        ):
            await ensure_column(db, "premium_guilds", column, declaration)
        for column, declaration in (
            ("granted_by", "INTEGER"),
            ("started_at", "TEXT"),
            ("permanent", "INTEGER NOT NULL DEFAULT 0"),
        ):
            await ensure_column(db, "noprefix", column, declaration)
        await db.commit()
        await self._migrate_legacy_premium()
        if (
            getattr(self.bot, "cluster_id", 0) == 0
            and not self.expiration_loop.is_running()
        ):
            self.expiration_loop.start()

    async def cog_unload(self):
        self.expiration_loop.cancel()

    async def _migrate_legacy_premium(self):
        db = self.bot.db
        async with db.execute("SELECT user_id, expiry FROM premium_users") as cursor:
            legacy_rows = await cursor.fetchall()
        now = utc_now()
        for user_id, expiry in legacy_rows:
            async with db.execute(
                "SELECT 1 FROM premium_allocations WHERE user_id=? LIMIT 1", (user_id,)
            ) as cursor:
                if await cursor.fetchone():
                    continue
            parsed = parse_stored_datetime(expiry)
            status = "active" if parsed and parsed > now else "expired"
            await db.execute(
                "INSERT INTO premium_allocations "
                "(user_id, granted_by, started_at, expires_at, guild_limit, status) "
                "VALUES (?, ?, ?, ?, 1, ?)",
                (user_id, user_id, now.isoformat(), expiry, status),
            )
        await db.commit()

    def _is_manager(self, user_id):
        configured = {
            int(value.strip())
            for value in os.getenv("PREMIUM_MANAGER_IDS", "").split(",")
            if value.strip().isdigit()
        }
        return user_id in self.bot.owner_ids or user_id in configured

    async def _resolve_user(self, value):
        if isinstance(value, (discord.Member, discord.User)):
            return value

        target = value.strip()
        if target.startswith("@") and not target.startswith("<@"):
            target = target[1:]
        match = USER_ID_PATTERN.fullmatch(target)
        if match:
            user_id = int(match.group(1) or match.group(2))
            user = self.bot.get_user(user_id)
            return user or await self.bot.fetch_user(user_id)

        target = target.casefold()
        matches = {
            user.id: user
            for user in self.bot.users
            if target in {user.name.casefold(), user.global_name.casefold() if user.global_name else ""}
        }
        for guild in self.bot.guilds:
            for member in guild.members:
                if target in {member.name.casefold(), member.display_name.casefold(), member.global_name.casefold() if member.global_name else ""}:
                    matches[member.id] = member
        if len(matches) == 1:
            return next(iter(matches.values()))
        if len(matches) > 1:
            raise ValueError("That username matches multiple users. Use a mention or user ID.")
        raise LookupError("User not found. Use a mention, user ID, or a cached exact username.")

    @tasks.loop(minutes=1)
    async def expiration_loop(self):
        async with self._premium_operation_lock():
            await self._expire_allocations()

    async def _expire_allocations(self):
        db = self.bot.db
        now = utc_now()
        async with db.execute(
            "SELECT allocation_id, user_id FROM premium_allocations "
            "WHERE status='active' AND expires_at IS NOT NULL"
        ) as cursor:
            rows = await cursor.fetchall()
        expired = []
        for allocation_id, user_id in rows:
            async with db.execute(
                "SELECT expires_at FROM premium_allocations WHERE allocation_id=?", (allocation_id,)
            ) as cursor:
                row = await cursor.fetchone()
            if row and parse_stored_datetime(row[0]) <= now:
                expired.append((allocation_id, user_id))
        for allocation_id, user_id in expired:
            async with db.execute(
                "SELECT guild_id, owner_id FROM premium_activations "
                "WHERE allocation_id=? AND status='active'",
                (allocation_id,),
            ) as cursor:
                guild_ids = await cursor.fetchall()
            await db.execute(
                "UPDATE premium_allocations SET status='expired' WHERE allocation_id=?",
                (allocation_id,),
            )
            await db.execute(
                "UPDATE premium_activations SET status='expired', revoked_at=? "
                "WHERE allocation_id=? AND status='active'",
                (now.isoformat(), allocation_id),
            )
            await db.execute(
                "DELETE FROM premium_guilds WHERE allocation_id=?", (allocation_id,)
            )
            user = self.bot.get_user(user_id)
            if user:
                try:
                    await send_v2(user, "Your Falcon Premium allocation has expired. Its guild activations are now inactive.")
                except discord.HTTPException:
                    pass
            for guild_id, owner_id in guild_ids:
                owner = self.bot.get_user(owner_id)
                if owner is None:
                    try:
                        owner = await self.bot.fetch_user(owner_id)
                    except discord.HTTPException:
                        continue
                try:
                    await send_v2(owner, "Falcon Premium for your server has expired.")
                except discord.HTTPException:
                    pass
        if expired:
            await db.commit()

    @expiration_loop.before_loop
    async def before_expiration_loop(self):
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:
            return

    @commands.hybrid_group(name="premium", invoke_without_command=True, case_insensitive=True)
    async def premium_group(self, ctx):
        embed = discord.Embed(
            title="Falcon Premium",
            description="Use `/premium gift`, `/premium add`, `/premium active`, `/premium revoke`, or `/premium view`.",
            color=0xFFDD00,
        )
        await ctx.send(embed=embed, ephemeral=ctx.interaction is not None)

    async def _grant_premium(self, ctx, user, duration, count):
        if not self._is_manager(ctx.author.id):
            return await ctx.send("Only configured bot owners or premium managers can grant premium.", ephemeral=ctx.interaction is not None)
        try:
            target = await self._resolve_user(user)
            delta = parse_duration(duration)
        except (LookupError, ValueError, discord.NotFound) as error:
            return await ctx.send(str(error), ephemeral=ctx.interaction is not None)
        if target.id == ctx.author.id and not self._is_manager(ctx.author.id):
            return await ctx.send("You cannot grant a premium allocation to yourself.", ephemeral=ctx.interaction is not None)
        if count < 1 or count > 100:
            return await ctx.send("Guild count must be between 1 and 100.", ephemeral=ctx.interaction is not None)

        now = utc_now()
        expiry = (now + delta).isoformat() if delta is not None else None
        async with self._premium_operation_lock():
            async with self.bot.db.execute(
                "INSERT INTO premium_allocations "
                "(user_id, granted_by, started_at, expires_at, guild_limit, status) "
                "VALUES (?, ?, ?, ?, ?, 'active') RETURNING allocation_id",
                (target.id, ctx.author.id, now.isoformat(), expiry, count),
            ) as cursor:
                allocation_id = (await cursor.fetchone())[0]
            await self.bot.db.execute(
                "INSERT OR REPLACE INTO premium_users (user_id, expiry) VALUES (?, ?)",
                (target.id, expiry or LIFETIME_EXPIRY),
            )
            await self.bot.db.commit()
        embed = discord.Embed(title="Premium Granted", color=0xFFDD00)
        embed.description = (
            f"{target.mention} received allocation **#{allocation_id}**.\n"
            f"Duration: {format_expiry(expiry)}\n"
            f"Guild activations: **{count}**."
        )
        await ctx.send(embed=embed, ephemeral=ctx.interaction is not None)

    async def _sync_premium_user_mirror(self, user_id):
        db = self.bot.db
        now = utc_now()
        expiries = []
        permanent = False
        async with db.execute(
            "SELECT expires_at FROM premium_allocations "
            "WHERE user_id=? AND status='active'",
            (user_id,),
        ) as cursor:
            allocations = await cursor.fetchall()
        for (expiry_value,) in allocations:
            if expiry_value is None:
                permanent = True
                continue
            expiry = parse_stored_datetime(expiry_value)
            if expiry is not None and expiry > now:
                expiries.append(expiry)

        async with db.execute(
            "SELECT expiry FROM premium_users WHERE user_id=?", (user_id,)
        ) as cursor:
            legacy = await cursor.fetchone()
        if legacy and legacy[0]:
            legacy_expiry = parse_stored_datetime(legacy[0])
            if legacy_expiry is not None and legacy_expiry > now:
                expiries.append(legacy_expiry)

        if permanent or any(expiry.year >= 9999 for expiry in expiries):
            mirror_expiry = LIFETIME_EXPIRY
        elif expiries:
            mirror_expiry = max(expiries).isoformat()
        else:
            await db.execute("DELETE FROM premium_users WHERE user_id=?", (user_id,))
            return
        await db.execute(
            "INSERT OR REPLACE INTO premium_users (user_id, expiry) VALUES (?, ?)",
            (user_id, mirror_expiry),
        )

    async def _transfer_premium_gift(self, ctx, user):
        try:
            target = await self._resolve_user(user)
        except (LookupError, ValueError, discord.NotFound) as error:
            return await ctx.send(str(error), ephemeral=ctx.interaction is not None)
        if target.id == ctx.author.id:
            return await ctx.send(
                "You cannot gift a premium allocation to yourself.",
                ephemeral=ctx.interaction is not None,
            )

        now = utc_now()
        db = self.bot.db
        async with self._premium_operation_lock():
            async with db.execute(
                "SELECT allocation_id, expires_at, guild_limit "
                "FROM premium_allocations WHERE user_id=? AND status='active' "
                "ORDER BY allocation_id",
                (ctx.author.id,),
            ) as cursor:
                rows = await cursor.fetchall()

            allocations = []
            for allocation_id, expiry, guild_limit in rows:
                parsed_expiry = parse_stored_datetime(expiry)
                if expiry is None or (parsed_expiry is not None and parsed_expiry > now):
                    allocations.append((allocation_id, expiry, guild_limit))

            if not allocations:
                async with db.execute(
                    "SELECT expiry FROM premium_users WHERE user_id=?",
                    (ctx.author.id,),
                ) as cursor:
                    legacy = await cursor.fetchone()
                parsed_legacy_expiry = (
                    parse_stored_datetime(legacy[0]) if legacy and legacy[0] else None
                )
                if parsed_legacy_expiry is not None and parsed_legacy_expiry > now:
                    async with db.execute(
                        "INSERT INTO premium_allocations "
                        "(user_id, granted_by, started_at, expires_at, guild_limit, status) "
                        "VALUES (?, ?, ?, ?, 1, 'active') RETURNING allocation_id",
                        (ctx.author.id, ctx.author.id, now.isoformat(), legacy[0]),
                    ) as cursor:
                        legacy_allocation_id = (await cursor.fetchone())[0]
                    allocations.append((legacy_allocation_id, legacy[0], 1))

            if not allocations:
                return await ctx.send(
                    "You need active Premium to gift it. No active Premium entitlement was found.",
                    ephemeral=ctx.interaction is not None,
                )

            for allocation in allocations:
                allocation_id = allocation[0]
                await db.execute(
                    "UPDATE premium_allocations SET user_id=?, granted_by=? "
                    "WHERE allocation_id=? AND user_id=? AND status='active'",
                    (target.id, ctx.author.id, allocation_id, ctx.author.id),
                )
                await db.execute(
                    "UPDATE premium_activations SET owner_id=? "
                    "WHERE allocation_id=? AND status='active'",
                    (target.id, allocation_id),
                )
                await db.execute(
                    "UPDATE premium_guilds SET owner_id=? WHERE allocation_id=?",
                    (target.id, allocation_id),
                )
            await db.execute("DELETE FROM premium_users WHERE user_id=?", (ctx.author.id,))
            await self._sync_premium_user_mirror(ctx.author.id)
            await self._sync_premium_user_mirror(target.id)
            await db.commit()

        embed = discord.Embed(title="Premium Gift Sent", color=0xFFDD00)
        embed.description = (
            f"{target.mention} received all **{len(allocations)}** active Premium "
            "allocation(s) from you.\n"
            "Your Premium access has been removed."
        )
        await ctx.send(embed=embed, ephemeral=ctx.interaction is not None)

    @premium_group.command(name="add", description="Grant a user a Falcon Premium allocation")
    async def premium_add(self, ctx, user: discord.User, duration: str, count: int):
        await self._grant_premium(ctx, user, duration, count)

    @premium_group.command(name="gift", description="Transfer all your Premium to another user")
    async def premium_gift(self, ctx, user: discord.User):
        await self._transfer_premium_gift(ctx, user)

    @premium_group.command(name="active", description="Activate your allocation in a guild the bot is in")
    async def premium_active(self, ctx, guild_id: int):
        user_id = ctx.author.id
        guild = await self._fetch_guild(guild_id)
        if guild is None:
            return await ctx.send("The bot is not in that guild, or the guild ID is invalid.", ephemeral=ctx.interaction is not None)
        now = utc_now()
        async with self._premium_operation_lock():
            async with self.bot.db.execute(
                "SELECT guild_id, expiry FROM premium_guilds WHERE guild_id=?", (guild_id,)
            ) as cursor:
                existing = await cursor.fetchone()
            if existing:
                existing_expiry = parse_stored_datetime(existing[1])
                if existing_expiry is None or existing_expiry > now:
                    return await ctx.send("This guild already has active premium.", ephemeral=ctx.interaction is not None)
                await self.bot.db.execute("DELETE FROM premium_guilds WHERE guild_id=?", (guild_id,))

            async with self.bot.db.execute(
                "SELECT allocation_id, expires_at, guild_limit FROM premium_allocations "
                "WHERE user_id=? AND status='active' ORDER BY allocation_id",
                (user_id,),
            ) as cursor:
                allocations = await cursor.fetchall()
            selected = None
            for allocation in allocations:
                allocation_expiry = parse_stored_datetime(allocation[1])
                if allocation_expiry is not None and allocation_expiry <= now:
                    continue
                async with self.bot.db.execute(
                    "SELECT COUNT(*) FROM premium_activations "
                    "WHERE allocation_id=? AND status='active'", (allocation[0],)
                ) as cursor:
                    used = (await cursor.fetchone())[0]
                if used < allocation[2]:
                    selected = allocation
                    break
            if selected is None:
                await self.bot.db.commit()
                return await ctx.send(
                    "You have no active allocation with a remaining guild slot.",
                    ephemeral=ctx.interaction is not None,
                )

            allocation_id, expiry, guild_limit = selected
            try:
                await self.bot.db.execute(
                    "INSERT INTO premium_activations "
                    "(allocation_id, guild_id, owner_id, activated_at, status) "
                    "VALUES (?, ?, ?, ?, 'active')",
                    (allocation_id, guild_id, user_id, now.isoformat()),
                )
            except aiosqlite.IntegrityError:
                await self.bot.db.rollback()
                return await ctx.send("This guild already has an active premium allocation.", ephemeral=ctx.interaction is not None)
            await self.bot.db.execute(
                "INSERT OR REPLACE INTO premium_guilds "
                "(guild_id, expiry, owner_id, allocation_id, activated_at) VALUES (?, ?, ?, ?, ?)",
                (guild_id, expiry, user_id, allocation_id, now.isoformat()),
            )
            await self.bot.db.commit()
            async with self.bot.db.execute(
                "SELECT COUNT(*) FROM premium_activations "
                "WHERE allocation_id=? AND status='active'", (allocation_id,)
            ) as cursor:
                used = (await cursor.fetchone())[0]
        remaining = max(0, guild_limit - used)
        embed = discord.Embed(title="Premium Activated", color=0xFFDD00)
        embed.description = (
            f"**Guild:** {guild.name}\n**Guild ID:** `{guild.id}`\n"
            f"**Premium owner:** {ctx.author.mention}\n**Expiry:** {format_expiry(expiry)}\n"
            f"**Remaining slots:** {remaining}\n**Status:** Active"
        )
        await ctx.send(embed=embed, ephemeral=ctx.interaction is not None)

    @premium_group.command(name="revoke", description="Revoke your premium activation from a guild")
    async def premium_revoke(self, ctx, guild_id: int):
        now = utc_now().isoformat()
        async with self._premium_operation_lock():
            async with self.bot.db.execute(
                "SELECT owner_id, allocation_id FROM premium_guilds WHERE guild_id=?", (guild_id,)
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                return await ctx.send("That guild has no active premium allocation.", ephemeral=ctx.interaction is not None)
            owner_id, allocation_id = row
            if owner_id not in (None, ctx.author.id) and not self._is_manager(ctx.author.id):
                return await ctx.send("Only the allocation owner or a premium manager can revoke this guild.", ephemeral=ctx.interaction is not None)
            if allocation_id is not None:
                await self.bot.db.execute(
                    "UPDATE premium_activations SET status='revoked', revoked_at=? "
                    "WHERE allocation_id=? AND guild_id=? AND status='active'",
                    (now, allocation_id, guild_id),
                )
            await self.bot.db.execute("DELETE FROM premium_guilds WHERE guild_id=?", (guild_id,))
            await self.bot.db.commit()
        guild = self.bot.get_guild(guild_id)
        name = guild.name if guild else f"Guild {guild_id}"
        await ctx.send(f"Premium has been revoked for **{name}**. Your allocation remains available.", ephemeral=ctx.interaction is not None)

    @premium_group.command(name="view", description="View your premium allocations and activations")
    async def premium_view(self, ctx):
        now = utc_now()
        async with self.bot.db.execute(
            "SELECT allocation_id, expires_at, guild_limit, status FROM premium_allocations "
            "WHERE user_id=? ORDER BY allocation_id DESC", (ctx.author.id,)
        ) as cursor:
            allocations = await cursor.fetchall()
        active_allocations = [
            row for row in allocations
            if row[3] == "active" and (parse_stored_datetime(row[1]) is None or parse_stored_datetime(row[1]) > now)
        ]
        total_slots = sum(row[2] for row in active_allocations)
        async with self.bot.db.execute(
            "SELECT a.guild_id, a.activated_at, a.status "
            "FROM premium_activations a JOIN premium_allocations p ON p.allocation_id=a.allocation_id "
            "WHERE a.owner_id=? ORDER BY a.activated_at DESC", (ctx.author.id,)
        ) as cursor:
            activations = await cursor.fetchall()
        active_count = sum(row[2] == "active" for row in activations)
        allocation_lines = [
            f"`#{row[0]}`: {format_expiry(row[1])}, {row[2]} guild slot(s), {row[3]}"
            for row in allocations[:10]
        ]
        guild_lines = []
        for guild_id, activated_at, status in activations[:20]:
            guild = self.bot.get_guild(guild_id)
            guild_name = guild.name if guild else "Unknown guild"
            activated = parse_stored_datetime(activated_at)
            if activated is None:
                continue
            stamp = int(activated.timestamp())
            guild_lines.append(f"{guild_name} (`{guild_id}`) • {status} • <t:{stamp}:D>")
        guild_history = []
        for line in guild_lines:
            candidate = "\n".join((*guild_history, line))
            if len(candidate) > 990:
                guild_history.append("… More history not shown.")
                break
            guild_history.append(line)
        embed = discord.Embed(title="Your Falcon Premium", color=0xFFDD00)
        embed.add_field(
            name="Allocations",
            value="\n".join(allocation_lines) if allocation_lines else "No premium allocation found.",
            inline=False,
        )
        embed.add_field(
            name="Capacity",
            value=f"Allowed: **{total_slots}** • Active guilds: **{active_count}** • Remaining: **{max(0, total_slots - active_count)}**",
            inline=False,
        )
        embed.add_field(
            name="Guild history",
            value="\n".join(guild_history) if guild_history else "No guild activations.",
            inline=False,
        )
        await ctx.send(embed=embed, ephemeral=ctx.interaction is not None)

    @commands.hybrid_group(name="np", invoke_without_command=True, case_insensitive=True)
    async def noprefix_group(self, ctx):
        embed = self._noprefix_embed(
            "Premium No-Prefix",
            "Use `/np add`, `/np remove`, or `/np list` to manage no-prefix access.",
        )
        await ctx.send(embed=embed, ephemeral=ctx.interaction is not None)

    def _noprefix_embed(self, title, description):
        embed = discord.Embed(title=title, description=description, color=0xFFDD00)
        embed.set_footer(text="Falcon Premium • No-Prefix")
        return embed

    async def _authorize_noprefix(self, ctx):
        if await self.bot.is_owner(ctx.author) or await self._is_bot_staff(ctx.author.id):
            return True
        await ctx.send(
            embed=self._noprefix_embed(
                "Access Denied", "Only bot owners or staff can manage no-prefix users."
            ),
            ephemeral=ctx.interaction is not None,
        )
        return False

    async def _is_bot_staff(self, user_id):
        if user_id in self.bot.owner_ids:
            return True
        async with self.bot.db.execute("SELECT 1 FROM bot_staff WHERE user_id=?", (user_id,)) as cursor:
            return await cursor.fetchone() is not None

    @noprefix_group.command(name="add", description="Grant no-prefix to a user")
    async def noprefix_add(self, ctx, user: discord.User, duration: str = "1m"):
        if not await self._authorize_noprefix(ctx):
            return
        try:
            delta = parse_duration(duration)
        except ValueError as error:
            return await ctx.send(
                embed=self._noprefix_embed("Invalid Duration", str(error)),
                ephemeral=ctx.interaction is not None,
            )
        now = utc_now()
        expiry = (now + delta).isoformat() if delta is not None else None
        await self.bot.db.execute(
            "INSERT OR REPLACE INTO noprefix "
            "(user_id, expiry, granted_by, started_at, permanent) VALUES (?, ?, ?, ?, ?)",
            (user.id, expiry, ctx.author.id, now.isoformat(), int(delta is None)),
        )
        await self.bot.db.commit()
        from main import ARROW, FP_TICK

        await ctx.send(
            f"{FP_TICK}  __**No-Prefix Enabled**__\n\n"
            f"{ARROW}  **No-prefix enabled for {user.mention}**\n\n"
            f"> Duration                 Status\n"
            f"> {format_expiry(expiry)}          Active\n\n"
            "-# Falcon Premium • No-Prefix",
            ephemeral=ctx.interaction is not None,
        )

    @noprefix_group.command(name="remove", description="Remove no-prefix from a user")
    async def noprefix_remove(self, ctx, user: discord.User):
        if not await self._authorize_noprefix(ctx):
            return
        await self.bot.db.execute("DELETE FROM noprefix WHERE user_id=?", (user.id,))
        await self.bot.db.commit()
        await ctx.send(
            embed=self._noprefix_embed("No-Prefix Removed", f"No-prefix access removed from {user.mention}."),
            ephemeral=ctx.interaction is not None,
        )

    @noprefix_group.command(name="list", description="List active no-prefix grants")
    async def noprefix_list(self, ctx):
        if not await self._authorize_noprefix(ctx):
            return
        async with self.bot.db.execute(
            "SELECT user_id, expiry, permanent FROM noprefix ORDER BY started_at DESC"
        ) as cursor:
            rows = await cursor.fetchall()
        now = utc_now()
        active_rows = []
        expired_ids = []
        for user_id, expiry, permanent in rows:
            parsed = parse_stored_datetime(expiry)
            if permanent or parsed is None or parsed > now:
                active_rows.append((user_id, expiry))
            else:
                expired_ids.append((user_id,))
        if expired_ids:
            await self.bot.db.executemany("DELETE FROM noprefix WHERE user_id=?", expired_ids)
            await self.bot.db.commit()
        if not active_rows:
            return await ctx.send(
                embed=self._noprefix_embed("Active No-Prefix Grants", "No active no-prefix grants found."),
                ephemeral=ctx.interaction is not None,
            )
        lines = [f"<@{user_id}> • {format_expiry(expiry)}" for user_id, expiry in active_rows[:20]]
        embed = self._noprefix_embed("Active No-Prefix Grants", "\n".join(lines))
        embed.set_footer(text=f"Falcon Premium • {len(active_rows)} active grants")
        await ctx.send(embed=embed, ephemeral=ctx.interaction is not None)


async def setup(bot):
    await bot.add_cog(PremiumFeatures(bot))
