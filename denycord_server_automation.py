import datetime
import logging

import discord
from discord.ext import commands, tasks

from cluster_runtime import bot_owns_guild

log = logging.getLogger(__name__)
UTC = datetime.timezone.utc
STAT_LABELS = {
    "members": "Members",
    "humans": "Humans",
    "bots": "Bots",
    "boosts": "Boosts",
}


class DenycordServerAutomation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        db = self.bot.db
        await db.execute(
            """CREATE TABLE IF NOT EXISTS denycord_birthdays (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                month INTEGER NOT NULL,
                day INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            )"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS denycord_birthday_settings (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                role_id INTEGER,
                last_announcement_date TEXT
            )"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS denycord_birthday_role_grants (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id, role_id)
            )"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS denycord_live_stats (
                guild_id INTEGER NOT NULL,
                stat_key TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, stat_key)
            )"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS denycord_voice_roles (
                guild_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, role_id)
            )"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS denycord_voice_role_grants (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_id, role_id)
            )"""
        )
        await db.commit()
        if not self.birthday_worker.is_running():
            self.birthday_worker.start()
        if not self.stats_worker.is_running():
            self.stats_worker.start()

    async def cog_unload(self) -> None:
        self.birthday_worker.cancel()
        self.stats_worker.cancel()

    @staticmethod
    def _valid_birthday(month: int, day: int) -> bool:
        try:
            datetime.date(2000, month, day)
        except ValueError:
            return False
        return True

    @staticmethod
    def _today_utc() -> datetime.date:
        return datetime.datetime.now(UTC).date()

    async def _expire_birthday_roles(self) -> None:
        now = datetime.datetime.now(UTC).isoformat()
        async with self.bot.db.execute(
            "SELECT guild_id, user_id, role_id FROM denycord_birthday_role_grants "
            "WHERE expires_at<=?",
            (now,),
        ) as cursor:
            expired = await cursor.fetchall()
        for guild_id, user_id, role_id in expired:
            if not bot_owns_guild(self.bot, guild_id):
                continue
            guild = self.bot.get_guild(guild_id)
            member = guild.get_member(user_id) if guild else None
            role = guild.get_role(role_id) if guild else None
            if guild is None or role is None:
                await self.bot.db.execute(
                    "DELETE FROM denycord_birthday_role_grants WHERE guild_id=? AND user_id=? AND role_id=?",
                    (guild_id, user_id, role_id),
                )
                continue
            if member is None:
                try:
                    member = await guild.fetch_member(user_id)
                except discord.NotFound:
                    await self.bot.db.execute(
                        "DELETE FROM denycord_birthday_role_grants WHERE guild_id=? AND user_id=? AND role_id=?",
                        (guild_id, user_id, role_id),
                    )
                    continue
                except discord.Forbidden:
                    log.warning("Cannot fetch birthday-role member %s in guild %s", user_id, guild_id, exc_info=True)
                    continue
                except discord.HTTPException:
                    log.exception("Discord API error fetching birthday-role member %s", user_id)
                    continue
            if role in member.roles:
                try:
                    await member.remove_roles(role, reason="Temporary birthday role expired")
                except discord.Forbidden:
                    log.warning("Cannot remove expired birthday role %s from %s", role_id, user_id, exc_info=True)
                    continue
                except discord.HTTPException:
                    log.exception("Discord API error removing birthday role %s from %s", role_id, user_id)
                    continue
            await self.bot.db.execute(
                "DELETE FROM denycord_birthday_role_grants WHERE guild_id=? AND user_id=? AND role_id=?",
                (guild_id, user_id, role_id),
            )
        if expired:
            await self.bot.db.commit()

    async def _send_birthday_announcement(
        self,
        guild_id: int,
        channel_id: int,
        role_id: int | None,
        on_date: datetime.date,
    ) -> bool:
        async with self.bot.db.execute(
            "SELECT user_id FROM denycord_birthdays "
            "WHERE guild_id=? AND month=? AND day=? ORDER BY user_id",
            (guild_id, on_date.month, on_date.day),
        ) as cursor:
            user_ids = [row[0] for row in await cursor.fetchall()]
        if not user_ids:
            return False

        guild = self.bot.get_guild(guild_id)
        channel = self.bot.get_channel(channel_id)
        if channel is None and guild is not None:
            channel = guild.get_channel(channel_id)
        if guild is None or not isinstance(channel, discord.TextChannel):
            log.warning(
                "Cannot send birthday announcement for guild %s: "
                "guild or configured text channel is unavailable",
                guild_id,
            )
            return False

        role = guild.get_role(role_id) if role_id is not None else None
        bot_member = guild.me
        role_is_assignable = (
            role is not None
            and not role.managed
            and bot_member is not None
            and bot_member.guild_permissions.manage_roles
            and role < bot_member.top_role
        )
        if role is not None and not role_is_assignable:
            log.warning("Configured birthday role %s is not assignable in guild %s", role.id, guild_id)
            role = None

        if role is not None:
            expiry = (datetime.datetime.now(UTC) + datetime.timedelta(days=1)).isoformat()
            for user_id in user_ids:
                member = guild.get_member(user_id)
                if member is None:
                    try:
                        member = await guild.fetch_member(user_id)
                    except discord.NotFound:
                        continue
                    except discord.Forbidden:
                        log.warning("Cannot fetch birthday member %s in guild %s", user_id, guild_id, exc_info=True)
                        continue
                    except discord.HTTPException:
                        log.exception("Discord API error fetching birthday member %s", user_id)
                        continue
                if role in member.roles:
                    continue
                try:
                    await member.add_roles(role, reason="Temporary birthday role (expires in 24 hours)")
                except discord.Forbidden:
                    log.warning("Cannot grant birthday role %s to %s", role.id, user_id, exc_info=True)
                    continue
                except discord.HTTPException:
                    log.exception("Discord API error granting birthday role %s to %s", role.id, user_id)
                    continue
                await self.bot.db.execute(
                    "INSERT INTO denycord_birthday_role_grants (guild_id,user_id,role_id,expires_at) "
                    "VALUES (?,?,?,?) ON CONFLICT(guild_id,user_id,role_id) "
                    "DO UPDATE SET expires_at=excluded.expires_at",
                    (guild_id, user_id, role.id, expiry),
                )
            await self.bot.db.commit()

        mentions = [f"<@{user_id}>" for user_id in user_ids]
        prefix = f"{role.mention} " if role is not None else ""
        content = f"{prefix}Happy birthday to " + ", ".join(mentions) + "!"
        if len(content) > 1950:
            included: list[str] = []
            for mention in mentions:
                candidate = f"{prefix}Happy birthday to " + ", ".join(included + [mention])
                if len(candidate) > 1850:
                    break
                included.append(mention)
            remaining = len(mentions) - len(included)
            content = (
                f"{prefix}Happy birthday to "
                + ", ".join(included)
                + (f" and {remaining} more member(s)!" if remaining else "!")
            )
        allowed_mentions = discord.AllowedMentions(
            users=True,
            roles=[role] if role is not None else False,
            everyone=False,
            replied_user=False,
        )
        try:
            await channel.send(content, allowed_mentions=allowed_mentions)
        except discord.Forbidden:
            log.warning("Discord denied birthday announcement in guild %s", guild_id, exc_info=True)
            return False
        except discord.HTTPException:
            log.exception("Failed to send birthday announcement in guild %s", guild_id)
            return False
        return True

    @tasks.loop(minutes=1)
    async def birthday_worker(self) -> None:
        db = self.bot.db
        await self._expire_birthday_roles()
        async with db.execute(
            "SELECT guild_id, channel_id, role_id FROM denycord_birthday_settings"
        ) as cursor:
            settings = await cursor.fetchall()

        today = self._today_utc()
        date_key = today.isoformat()
        for guild_id, channel_id, role_id in settings:
            if not bot_owns_guild(self.bot, guild_id):
                continue
            # Claim this server/date before sending: the committed claim prevents
            # duplicate announcements if the process restarts after the send.
            async with db.execute(
                "UPDATE denycord_birthday_settings SET last_announcement_date=? "
                "WHERE guild_id=? AND (last_announcement_date IS NULL "
                "OR last_announcement_date<>?)",
                (date_key, guild_id, date_key),
            ) as cursor:
                claimed = cursor.rowcount == 1
            await db.commit()
            if not claimed:
                continue

            sent = await self._send_birthday_announcement(guild_id, channel_id, role_id, today)
            if not sent:
                await db.execute(
                    "UPDATE denycord_birthday_settings SET last_announcement_date=NULL "
                    "WHERE guild_id=? AND last_announcement_date=?",
                    (guild_id, date_key),
                )
                await db.commit()

    @birthday_worker.before_loop
    async def before_birthday_worker(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=5)
    async def stats_worker(self) -> None:
        for guild in self.bot.guilds:
            await self._update_guild_stats(guild)

    @stats_worker.before_loop
    async def before_stats_worker(self) -> None:
        await self.bot.wait_until_ready()

    async def _update_guild_stats(self, guild: discord.Guild) -> None:
        async with self.bot.db.execute(
            "SELECT stat_key, channel_id FROM denycord_live_stats WHERE guild_id=?",
            (guild.id,),
        ) as cursor:
            configured = await cursor.fetchall()
        members = guild.members
        values = {
            "members": guild.member_count if guild.member_count is not None else len(members),
            "humans": sum(not member.bot for member in members),
            "bots": sum(member.bot for member in members),
            "boosts": guild.premium_subscription_count or 0,
        }
        for stat_key, channel_id in configured:
            label = STAT_LABELS.get(stat_key)
            if label is None:
                log.warning("Unknown live-stat key %r for guild %s", stat_key, guild.id)
                continue
            channel = guild.get_channel(channel_id)
            if not isinstance(channel, discord.VoiceChannel):
                log.warning(
                    "Configured live-stat voice channel %s is unavailable in guild %s",
                    channel_id,
                    guild.id,
                )
                continue
            name = f"{label}: {values[stat_key]}"
            if channel.name == name:
                continue
            try:
                await channel.edit(name=name, reason="Refresh Falcon live server statistics")
            except discord.HTTPException:
                log.exception(
                    "Failed to update %s live-stat channel in guild %s",
                    stat_key,
                    guild.id,
                )

    @commands.hybrid_group(name="birthday", invoke_without_command=True)
    async def birthday(self, ctx: commands.Context) -> None:
        await ctx.send("Birthday commands: `birthday setup`, `birthday set`, `birthday remove`, and `birthday list`.")

    @birthday.command(
        name="setup",
        description="Set or disable this server's birthday announcements",
    )
    @commands.has_guild_permissions(manage_guild=True)
    async def birthday_setup(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel | None = None,
        role: discord.Role | None = None,
    ) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        if channel is None:
            await self.bot.db.execute(
                "DELETE FROM denycord_birthday_settings WHERE guild_id=?",
                (ctx.guild.id,),
            )
            await self.bot.db.commit()
            await ctx.send("Birthday announcements are disabled for this server.")
            return

        bot_member = ctx.guild.me
        permissions = channel.permissions_for(bot_member) if bot_member is not None else None
        if permissions is None or not permissions.view_channel or not permissions.send_messages:
            await ctx.send("I need permission to view and send messages in that channel.")
            return
        if role is not None and role.guild.id != ctx.guild.id:
            await ctx.send("Choose a birthday role from this server.")
            return
        if role is not None and (
            role.managed
            or bot_member is None
            or not bot_member.guild_permissions.manage_roles
            or role >= bot_member.top_role
        ):
            await ctx.send("I need Manage Roles, and my highest role must be above the birthday role.")
            return

        await self.bot.db.execute(
            "INSERT INTO denycord_birthday_settings (guild_id, channel_id, role_id) "
            "VALUES (?, ?, ?) ON CONFLICT(guild_id) DO UPDATE SET "
            "channel_id=excluded.channel_id, role_id=excluded.role_id",
            (ctx.guild.id, channel.id, role.id if role else None),
        )
        await self.bot.db.commit()
        role_text = f" and {role.mention}" if role else " (no role)"
        await ctx.send(f"Birthday announcements will be sent to {channel.mention}{role_text}.")

    @birthday.command(name="set", description="Set your birthday in this server")
    async def birthday_set(self, ctx: commands.Context, month: int, day: int) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        if not self._valid_birthday(month, day):
            await ctx.send("Enter a valid month and day (for example, `2 29` is valid).")
            return
        await self.bot.db.execute(
            "INSERT INTO denycord_birthdays (guild_id, user_id, month, day) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(guild_id, user_id) DO UPDATE SET "
            "month=excluded.month, day=excluded.day",
            (ctx.guild.id, ctx.author.id, month, day),
        )
        await self.bot.db.commit()
        await ctx.send(f"Your birthday is set to {month:02d}-{day:02d}.")

    @birthday.command(name="remove", description="Remove your birthday from this server")
    async def birthday_remove(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        await self.bot.db.execute(
            "DELETE FROM denycord_birthdays WHERE guild_id=? AND user_id=?",
            (ctx.guild.id, ctx.author.id),
        )
        await self.bot.db.commit()
        await ctx.send("Your birthday has been removed.")

    @birthday.command(name="list", description="List birthdays set in this server")
    async def birthday_list(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        async with self.bot.db.execute(
            "SELECT user_id, month, day FROM denycord_birthdays "
            "WHERE guild_id=? ORDER BY month, day, user_id",
            (ctx.guild.id,),
        ) as cursor:
            birthdays = await cursor.fetchall()
        if not birthdays:
            await ctx.send("No birthdays have been set in this server.")
            return

        lines = [
            f"• <@{user_id}> — {datetime.date(2000, month, day).strftime('%b %d')}"
            for user_id, month, day in birthdays
        ]
        chunks: list[str] = []
        current = "Server birthdays:\n"
        for line in lines:
            if len(current) + len(line) + 1 > 1900:
                chunks.append(current)
                current = ""
            current += line + "\n"
        if current:
            chunks.append(current)
        for chunk in chunks:
            await ctx.send(
                chunk,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @birthday.command(name="check", description="Manually announce today's birthdays")
    @commands.has_guild_permissions(manage_guild=True)
    async def birthday_check(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        async with self.bot.db.execute(
            "SELECT channel_id, role_id FROM denycord_birthday_settings WHERE guild_id=?",
            (ctx.guild.id,),
        ) as cursor:
            settings = await cursor.fetchone()
        if settings is None:
            await ctx.send("Set up birthday announcements first with `birthday setup #channel [@role]`.")
            return
        today = self._today_utc()
        async with self.bot.db.execute(
            "SELECT COUNT(*) FROM denycord_birthdays WHERE guild_id=? AND month=? AND day=?",
            (ctx.guild.id, today.month, today.day),
        ) as cursor:
            count = (await cursor.fetchone())[0]
        if not count:
            await ctx.send("No birthdays are registered for today.")
            return
        sent = await self._send_birthday_announcement(
            ctx.guild.id, settings[0], settings[1], today
        )
        await ctx.send(
            "Today's birthday announcement was sent."
            if sent
            else "I couldn't send today's birthday announcement. Check the configured channel and bot permissions."
        )

    @commands.hybrid_group(name="livestats", invoke_without_command=True)
    async def livestats(self, ctx: commands.Context) -> None:
        await ctx.send("Live stats commands: `livestats setup` and `livestats remove`.")

    @livestats.command(
        name="setup",
        description="Create or refresh live member and boost voice-channel stats",
    )
    @commands.has_guild_permissions(manage_channels=True)
    async def stats_setup(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        guild = ctx.guild
        async with self.bot.db.execute(
            "SELECT stat_key, channel_id FROM denycord_live_stats WHERE guild_id=?",
            (guild.id,),
        ) as cursor:
            existing = dict(await cursor.fetchall())

        created = 0
        for stat_key, label in STAT_LABELS.items():
            channel = guild.get_channel(existing.get(stat_key)) if stat_key in existing else None
            if isinstance(channel, discord.VoiceChannel):
                continue
            try:
                channel = await guild.create_voice_channel(
                    name=f"{label}: 0",
                    reason=f"Set up Falcon live {label.lower()} statistics",
                )
            except discord.HTTPException:
                log.exception("Failed to create %s live-stat channel in guild %s", stat_key, guild.id)
                await ctx.send(
                    f"Could not create the {label.lower()} voice channel. "
                    "Check my Manage Channels permission; any channels already created remain configured."
                )
                return
            await self.bot.db.execute(
                "INSERT INTO denycord_live_stats (guild_id, stat_key, channel_id) "
                "VALUES (?, ?, ?) ON CONFLICT(guild_id, stat_key) DO UPDATE SET "
                "channel_id=excluded.channel_id",
                (guild.id, stat_key, channel.id),
            )
            await self.bot.db.commit()
            created += 1

        await self._update_guild_stats(guild)
        if created:
            await ctx.send(f"Live stats are configured. Created {created} voice channel(s).")
        else:
            await ctx.send("Live stats are already configured and have been refreshed.")

    @livestats.command(
        name="remove",
        description="Remove this server's live-stat voice channels",
    )
    @commands.has_guild_permissions(manage_channels=True)
    async def stats_remove(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        async with self.bot.db.execute(
            "SELECT stat_key, channel_id FROM denycord_live_stats WHERE guild_id=?",
            (ctx.guild.id,),
        ) as cursor:
            configured = await cursor.fetchall()
        removed = 0
        for stat_key, channel_id in configured:
            channel = ctx.guild.get_channel(channel_id)
            if channel is not None:
                try:
                    await channel.delete(reason="Remove Falcon live server statistics")
                except discord.HTTPException:
                    log.exception(
                        "Failed to delete %s live-stat channel in guild %s",
                        stat_key,
                        ctx.guild.id,
                    )
                    continue
            await self.bot.db.execute(
                "DELETE FROM denycord_live_stats WHERE guild_id=? AND stat_key=?",
                (ctx.guild.id, stat_key),
            )
            await self.bot.db.commit()
            removed += 1
        await ctx.send(f"Removed {removed} live-stat voice channel(s).")

    @commands.group(name="voicerole", invoke_without_command=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def voicerole(self, ctx: commands.Context) -> None:
        await ctx.send("Voice-role commands: `voicerole add @role`, `voicerole remove @role`, `voicerole list`.")

    @voicerole.command(name="add")
    @commands.has_guild_permissions(manage_guild=True)
    async def voicerole_add(self, ctx: commands.Context, role: discord.Role) -> None:
        guild = ctx.guild
        bot_member = guild.me if guild else None
        if (
            guild is None
            or role.is_default()
            or role.managed
            or bot_member is None
            or not bot_member.guild_permissions.manage_roles
            or role >= bot_member.top_role
        ):
            await ctx.send("I need Manage Roles and a role hierarchy above the voice role.")
            return
        cursor = await self.bot.db.execute(
            "INSERT OR IGNORE INTO denycord_voice_roles (guild_id, role_id) VALUES (?, ?)",
            (guild.id, role.id),
        )
        await self.bot.db.commit()
        await ctx.send(
            f"Voice role {role.mention} added; members receive it when joining voice and lose it when leaving."
            if cursor.rowcount
            else f"{role.mention} is already configured as a voice role."
        )

    @voicerole.command(name="remove")
    @commands.has_guild_permissions(manage_guild=True)
    async def voicerole_remove(self, ctx: commands.Context, role: discord.Role) -> None:
        cursor = await self.bot.db.execute(
            "DELETE FROM denycord_voice_roles WHERE guild_id=? AND role_id=?",
            (ctx.guild.id, role.id),
        )
        await self.bot.db.commit()
        await ctx.send("Voice role removed." if cursor.rowcount else "That voice role was not configured.")

    @voicerole.command(name="list")
    @commands.has_guild_permissions(manage_guild=True)
    async def voicerole_list(self, ctx: commands.Context) -> None:
        async with self.bot.db.execute(
            "SELECT role_id FROM denycord_voice_roles WHERE guild_id=? ORDER BY role_id",
            (ctx.guild.id,),
        ) as cursor:
            role_ids = [row[0] for row in await cursor.fetchall()]
        roles = [f"<@&{role_id}>" for role_id in role_ids]
        await ctx.send("Configured voice roles: " + ", ".join(roles) if roles else "No voice roles configured.")

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if before.channel is None and after.channel is not None:
            async with self.bot.db.execute(
                "SELECT role_id FROM denycord_voice_roles WHERE guild_id=?",
                (member.guild.id,),
            ) as cursor:
                role_ids = [row[0] for row in await cursor.fetchall()]
            bot_member = member.guild.me
            if bot_member is None or not bot_member.guild_permissions.manage_roles:
                return
            for role_id in role_ids:
                role = member.guild.get_role(role_id)
                if role is None or role in member.roles or role >= bot_member.top_role:
                    continue
                await self.bot.db.execute(
                    "INSERT OR IGNORE INTO denycord_voice_role_grants (guild_id,user_id,role_id) "
                    "VALUES (?,?,?)",
                    (member.guild.id, member.id, role_id),
                )
                await self.bot.db.commit()
                try:
                    await member.add_roles(role, reason="Joined a voice channel")
                except discord.HTTPException:
                    log.warning("Could not assign voice role %s to member %s", role_id, member.id, exc_info=True)
                    await self.bot.db.execute(
                        "DELETE FROM denycord_voice_role_grants WHERE guild_id=? AND user_id=? AND role_id=?",
                        (member.guild.id, member.id, role_id),
                    )
                    await self.bot.db.commit()
        elif before.channel is not None and after.channel is None:
            async with self.bot.db.execute(
                "SELECT role_id FROM denycord_voice_role_grants WHERE guild_id=? AND user_id=?",
                (member.guild.id, member.id),
            ) as cursor:
                granted_roles = [row[0] for row in await cursor.fetchall()]
            for role_id in granted_roles:
                role = member.guild.get_role(role_id)
                if role is not None and role in member.roles:
                    try:
                        await member.remove_roles(role, reason="Left voice channel")
                    except discord.HTTPException:
                        log.warning("Could not remove voice role %s from member %s", role_id, member.id, exc_info=True)
                        continue
                await self.bot.db.execute(
                    "DELETE FROM denycord_voice_role_grants WHERE guild_id=? AND user_id=? AND role_id=?",
                    (member.guild.id, member.id, role_id),
                )
            if granted_roles:
                await self.bot.db.commit()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DenycordServerAutomation(bot))
