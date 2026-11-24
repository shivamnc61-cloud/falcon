import datetime
import logging

import discord
from discord.ext import commands, tasks

from cluster_runtime import bot_owns_guild
from time_parser import parse_duration


log = logging.getLogger(__name__)
UTC = datetime.timezone.utc
MAX_INTERVAL_SECONDS = 365 * 24 * 60 * 60


def _interval_seconds(value: str) -> int:
    duration = parse_duration(value)
    if duration is None:
        raise ValueError("An interval is required; permanent durations are not supported.")
    seconds = int(duration.total_seconds())
    if not 60 <= seconds <= MAX_INTERVAL_SECONDS:
        raise ValueError("Choose an interval between 1 minute and 365 days.")
    return seconds


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(UTC)


class ReferenceAutomation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        await self.bot.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS reference_autoposts (
                post_id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                interval_seconds INTEGER NOT NULL,
                message TEXT NOT NULL,
                next_run_at TEXT NOT NULL,
                created_by INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reference_bump_reminders (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                interval_seconds INTEGER NOT NULL,
                next_run_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reference_autoreacts (
                rule_id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER,
                trigger TEXT NOT NULL,
                emoji TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reference_vanity_roles (
                rule_id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                keyword TEXT NOT NULL,
                role_id INTEGER NOT NULL,
                UNIQUE(guild_id, keyword, role_id)
            );
            """
        )
        await self.bot.db.commit()
        if not self.scheduler.is_running():
            self.scheduler.start()

    async def cog_unload(self) -> None:
        self.scheduler.cancel()

    @tasks.loop(seconds=30)
    async def scheduler(self) -> None:
        await self._run_due_autoposts()
        await self._run_due_bump_reminders()

    @scheduler.before_loop
    async def before_scheduler(self) -> None:
        await self.bot.wait_until_ready()

    async def _run_due_autoposts(self) -> None:
        now = _utc_now()
        async with self.bot.db.execute(
            "SELECT post_id, guild_id, channel_id, interval_seconds, message, next_run_at "
            "FROM reference_autoposts WHERE next_run_at<=? ORDER BY next_run_at LIMIT 25",
            (now.isoformat(),),
        ) as cursor:
            due_posts = await cursor.fetchall()

        for post_id, guild_id, channel_id, interval_seconds, message, due_at in due_posts:
            if not bot_owns_guild(self.bot, guild_id):
                continue
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                guild = self.bot.get_guild(guild_id)
                channel = guild.get_channel(channel_id) if guild else None
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                await self.bot.db.execute(
                    "DELETE FROM reference_autoposts WHERE post_id=?", (post_id,)
                )
                continue
            try:
                await channel.send(message, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                log.exception("Autopost %s failed in guild %s", post_id, guild_id)
            next_run = now + datetime.timedelta(seconds=interval_seconds)
            await self.bot.db.execute(
                "UPDATE reference_autoposts SET next_run_at=? WHERE post_id=? AND next_run_at=?",
                (next_run.isoformat(), post_id, due_at),
            )
        if due_posts:
            await self.bot.db.commit()

    async def _run_due_bump_reminders(self) -> None:
        now = _utc_now()
        async with self.bot.db.execute(
            "SELECT guild_id, channel_id, interval_seconds, next_run_at "
            "FROM reference_bump_reminders WHERE next_run_at<=?",
            (now.isoformat(),),
        ) as cursor:
            due_reminders = await cursor.fetchall()

        for guild_id, channel_id, interval_seconds, due_at in due_reminders:
            if not bot_owns_guild(self.bot, guild_id):
                continue
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                guild = self.bot.get_guild(guild_id)
                channel = guild.get_channel(channel_id) if guild else None
            if isinstance(channel, (discord.TextChannel, discord.Thread)):
                try:
                    await channel.send(
                        "Your server bump is ready. Run `/bump` in this channel.",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    log.exception("Bump reminder failed in guild %s", guild_id)
            next_run = now + datetime.timedelta(seconds=interval_seconds)
            await self.bot.db.execute(
                "UPDATE reference_bump_reminders SET next_run_at=? "
                "WHERE guild_id=? AND next_run_at=?",
                (next_run.isoformat(), guild_id, due_at),
            )
        if due_reminders:
            await self.bot.db.commit()

    @commands.group(name="autopost", invoke_without_command=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def autopost(self, ctx: commands.Context) -> None:
        await ctx.send("Use `autopost add`, `autopost list`, or `autopost remove`.")

    @autopost.command(name="add")
    @commands.has_guild_permissions(manage_guild=True)
    async def autopost_add(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel,
        interval: str,
        *,
        message: str,
    ) -> None:
        try:
            seconds = _interval_seconds(interval)
        except ValueError as error:
            return await ctx.send(str(error))
        if len(message) > 2000:
            return await ctx.send("Autopost messages must be 2000 characters or fewer.")
        next_run = (_utc_now() + datetime.timedelta(seconds=seconds)).isoformat()
        cursor = await self.bot.db.execute(
            "INSERT INTO reference_autoposts "
            "(guild_id, channel_id, interval_seconds, message, next_run_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ctx.guild.id, channel.id, seconds, message, next_run, ctx.author.id),
        )
        await self.bot.db.commit()
        await ctx.send(f"Autopost `{cursor.lastrowid}` scheduled in {channel.mention} every {interval}.")

    @autopost.command(name="list")
    @commands.has_guild_permissions(manage_guild=True)
    async def autopost_list(self, ctx: commands.Context) -> None:
        async with self.bot.db.execute(
            "SELECT post_id, channel_id, interval_seconds, next_run_at "
            "FROM reference_autoposts WHERE guild_id=? ORDER BY post_id LIMIT 20",
            (ctx.guild.id,),
        ) as cursor:
            rows = await cursor.fetchall()
        if not rows:
            return await ctx.send("No autopost schedules are configured.")
        lines = []
        for post_id, channel_id, seconds, next_run in rows:
            channel = ctx.guild.get_channel(channel_id)
            channel_label = channel.mention if channel else f"deleted channel `{channel_id}`"
            lines.append(f"`{post_id}` {channel_label} every `{seconds}s`, next <t:{int(datetime.datetime.fromisoformat(next_run).timestamp())}:R>")
        await ctx.send("\n".join(lines))

    @autopost.command(name="remove")
    @commands.has_guild_permissions(manage_guild=True)
    async def autopost_remove(self, ctx: commands.Context, post_id: int) -> None:
        cursor = await self.bot.db.execute(
            "DELETE FROM reference_autoposts WHERE guild_id=? AND post_id=?",
            (ctx.guild.id, post_id),
        )
        await self.bot.db.commit()
        await ctx.send("Autopost schedule removed." if cursor.rowcount else "Schedule not found.")

    @commands.group(name="autobump", invoke_without_command=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def autobump(self, ctx: commands.Context) -> None:
        await ctx.send("Use `autobump set`, `autobump status`, or `autobump disable`.")

    @autobump.command(name="set")
    @commands.has_guild_permissions(manage_guild=True)
    async def autobump_set(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel,
        interval: str = "2h",
    ) -> None:
        try:
            seconds = _interval_seconds(interval)
        except ValueError as error:
            return await ctx.send(str(error))
        next_run = (_utc_now() + datetime.timedelta(seconds=seconds)).isoformat()
        await self.bot.db.execute(
            "INSERT INTO reference_bump_reminders (guild_id, channel_id, interval_seconds, next_run_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(guild_id) DO UPDATE SET "
            "channel_id=excluded.channel_id, interval_seconds=excluded.interval_seconds, "
            "next_run_at=excluded.next_run_at",
            (ctx.guild.id, channel.id, seconds, next_run),
        )
        await self.bot.db.commit()
        await ctx.send(f"Bump reminders enabled in {channel.mention} every {interval}.")

    @autobump.command(name="status")
    @commands.has_guild_permissions(manage_guild=True)
    async def autobump_status(self, ctx: commands.Context) -> None:
        async with self.bot.db.execute(
            "SELECT channel_id, interval_seconds, next_run_at FROM reference_bump_reminders WHERE guild_id=?",
            (ctx.guild.id,),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return await ctx.send("Bump reminders are disabled.")
        channel = ctx.guild.get_channel(row[0])
        target = channel.mention if channel else f"deleted channel `{row[0]}`"
        timestamp = int(datetime.datetime.fromisoformat(row[2]).timestamp())
        await ctx.send(f"Bump reminder: {target}, every `{row[1]}s`, next <t:{timestamp}:R>.")

    @autobump.command(name="disable")
    @commands.has_guild_permissions(manage_guild=True)
    async def autobump_disable(self, ctx: commands.Context) -> None:
        await self.bot.db.execute(
            "DELETE FROM reference_bump_reminders WHERE guild_id=?", (ctx.guild.id,)
        )
        await self.bot.db.commit()
        await ctx.send("Bump reminders disabled.")

    @commands.group(name="autoreact", invoke_without_command=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def autoreact(self, ctx: commands.Context) -> None:
        await ctx.send("Use `autoreact add`, `autoreact list`, or `autoreact remove`.")

    @autoreact.command(name="add")
    @commands.has_guild_permissions(manage_guild=True)
    async def autoreact_add(
        self,
        ctx: commands.Context,
        trigger: str,
        emoji: str,
        channel: discord.TextChannel | None = None,
    ) -> None:
        if not trigger.strip() or len(trigger) > 100 or len(emoji) > 100:
            return await ctx.send("Trigger and emoji must be between 1 and 100 characters.")
        channel_id = channel.id if channel else None
        await self.bot.db.execute(
            "INSERT INTO reference_autoreacts (guild_id, channel_id, trigger, emoji) VALUES (?, ?, ?, ?)",
            (ctx.guild.id, channel_id, trigger.casefold(), emoji),
        )
        await self.bot.db.commit()
        scope = channel.mention if channel else "all channels"
        await ctx.send(f"Auto-reaction added for `{trigger}` in {scope}.")

    @autoreact.command(name="list")
    @commands.has_guild_permissions(manage_guild=True)
    async def autoreact_list(self, ctx: commands.Context) -> None:
        async with self.bot.db.execute(
            "SELECT rule_id, channel_id, trigger, emoji FROM reference_autoreacts "
            "WHERE guild_id=? ORDER BY rule_id LIMIT 20",
            (ctx.guild.id,),
        ) as cursor:
            rows = await cursor.fetchall()
        if not rows:
            return await ctx.send("No auto-reaction rules are configured.")
        lines = []
        for rule_id, channel_id, trigger, emoji in rows:
            scope = f" in <#{channel_id}>" if channel_id else " in all channels"
            lines.append(f"`{rule_id}` `{trigger}` -> {emoji}{scope}")
        await ctx.send("\n".join(lines))

    @autoreact.command(name="remove")
    @commands.has_guild_permissions(manage_guild=True)
    async def autoreact_remove(self, ctx: commands.Context, rule_id: int) -> None:
        cursor = await self.bot.db.execute(
            "DELETE FROM reference_autoreacts WHERE guild_id=? AND rule_id=?",
            (ctx.guild.id, rule_id),
        )
        await self.bot.db.commit()
        await ctx.send("Auto-reaction rule removed." if cursor.rowcount else "Rule not found.")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot or not message.content:
            return
        async with self.bot.db.execute(
            "SELECT channel_id, trigger, emoji FROM reference_autoreacts WHERE guild_id=?",
            (message.guild.id,),
        ) as cursor:
            rules = await cursor.fetchall()
        for channel_id, trigger, emoji in rules:
            if channel_id is not None and channel_id != message.channel.id:
                continue
            if trigger not in message.content.casefold():
                continue
            try:
                await message.add_reaction(emoji)
            except discord.HTTPException:
                log.warning("Auto-reaction failed for guild %s rule %r", message.guild.id, trigger)

    @commands.group(name="vanityroles", invoke_without_command=True)
    @commands.has_guild_permissions(manage_roles=True)
    async def vanityroles(self, ctx: commands.Context) -> None:
        await ctx.send("Use `vanityroles add`, `vanityroles list`, or `vanityroles remove`.")

    @vanityroles.command(name="add")
    @commands.has_guild_permissions(manage_roles=True)
    async def vanityroles_add(
        self,
        ctx: commands.Context,
        keyword: str,
        role: discord.Role,
    ) -> None:
        keyword = keyword.strip()
        if not keyword:
            return await ctx.send("Vanity-role keywords cannot be empty.")
        bot_member = ctx.guild.me
        if (
            role.is_default()
            or bot_member is None
            or not bot_member.guild_permissions.manage_roles
            or role >= bot_member.top_role
        ):
            return await ctx.send("I need Manage Roles and a role hierarchy above the configured role.")
        cursor = await self.bot.db.execute(
            "INSERT OR IGNORE INTO reference_vanity_roles (guild_id, keyword, role_id) VALUES (?, ?, ?)",
            (ctx.guild.id, keyword.casefold(), role.id),
        )
        await self.bot.db.commit()
        if cursor.rowcount == 0:
            return await ctx.send("That keyword and role rule already exists.")
        await ctx.send(f"Members with `{keyword}` in their custom status receive {role.mention}.")

    @vanityroles.command(name="list")
    @commands.has_guild_permissions(manage_roles=True)
    async def vanityroles_list(self, ctx: commands.Context) -> None:
        async with self.bot.db.execute(
            "SELECT rule_id, keyword, role_id FROM reference_vanity_roles "
            "WHERE guild_id=? ORDER BY rule_id LIMIT 20",
            (ctx.guild.id,),
        ) as cursor:
            rows = await cursor.fetchall()
        if not rows:
            return await ctx.send("No vanity-role rules are configured.")
        await ctx.send("\n".join(
            f"`{rule_id}` `{keyword}` -> <@&{role_id}>" for rule_id, keyword, role_id in rows
        ))

    @vanityroles.command(name="remove")
    @commands.has_guild_permissions(manage_roles=True)
    async def vanityroles_remove(self, ctx: commands.Context, rule_id: int) -> None:
        cursor = await self.bot.db.execute(
            "DELETE FROM reference_vanity_roles WHERE guild_id=? AND rule_id=?",
            (ctx.guild.id, rule_id),
        )
        await self.bot.db.commit()
        await ctx.send("Vanity-role rule removed." if cursor.rowcount else "Rule not found.")

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.activities == after.activities or after.bot:
            return
        bot_member = after.guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            return
        custom_status = next(
            (activity.name for activity in after.activities if isinstance(activity, discord.CustomActivity)),
            "",
        ).casefold()
        async with self.bot.db.execute(
            "SELECT keyword, role_id FROM reference_vanity_roles WHERE guild_id=?",
            (after.guild.id,),
        ) as cursor:
            rules = await cursor.fetchall()
        for keyword, role_id in rules:
            role = after.guild.get_role(role_id)
            if role is None or role >= bot_member.top_role or role.is_default():
                continue
            has_role = role in after.roles
            should_have_role = bool(custom_status and keyword in custom_status)
            if should_have_role == has_role:
                continue
            try:
                if should_have_role:
                    await after.add_roles(role, reason=f"Custom status contains {keyword!r}")
                else:
                    await after.remove_roles(role, reason=f"Custom status no longer contains {keyword!r}")
            except discord.HTTPException:
                log.warning("Vanity-role update failed for member %s in guild %s", after.id, after.guild.id)