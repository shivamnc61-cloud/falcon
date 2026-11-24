import datetime
import io
import json
import logging
import random
import re

import discord
from discord.ext import commands, tasks

from cluster_runtime import bot_owns_guild
from components_v2 import edit_interaction_v2, send_interaction_v2, send_v2
from time_parser import parse_duration


UTC = datetime.timezone.utc
LOGGER = logging.getLogger(__name__)
AFK_NICK_PREFIX = "[AFK] "
SUCCESS_TICK = "<:fp_tick:1556939072646287360>"
DETAIL_ARROW = "<:fp_arrow:1551879040292360225>"


class PollView(discord.ui.View):
    def __init__(self, question, options, owner_id):
        super().__init__(timeout=86400)
        self.question = question
        self.options = options
        self.owner_id = owner_id
        self.votes = {}
        self.first.label = options[0][:80]
        self.second.label = options[1][:80]

    async def _vote(self, interaction, option):
        self.votes[interaction.user.id] = option
        counts = [sum(vote == index for vote in self.votes.values()) for index in range(2)]
        total = sum(counts)
        embed = discord.Embed(title=self.question, color=0xFFDD00)
        for label, count in zip(self.options, counts):
            percentage = (count / total * 100) if total else 0
            embed.add_field(name=label, value=f"{count} vote(s) ({percentage:.1f}%)", inline=False)
        await edit_interaction_v2(interaction, embed=embed, view=self)

    @discord.ui.button(label="Option 1", style=discord.ButtonStyle.secondary)
    async def first(self, interaction, _button):
        await self._vote(interaction, 0)

    @discord.ui.button(label="Option 2", style=discord.ButtonStyle.secondary)
    async def second(self, interaction, _button):
        await self._vote(interaction, 1)


class RPSView(discord.ui.View):
    def __init__(self, owner_id):
        super().__init__(timeout=120)
        self.owner_id = owner_id

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await send_interaction_v2(interaction, "This game belongs to the person who started it.", ephemeral=True)
        return False

    async def _play(self, interaction, choice):
        computer = random.choice(("rock", "paper", "scissors"))
        if choice == computer:
            result = "It's a tie."
        elif (choice, computer) in {("rock", "scissors"), ("paper", "rock"), ("scissors", "paper")}:
            result = "You win!"
        else:
            result = "You lose."
        embed = discord.Embed(
            title="Rock, Paper, Scissors",
            description=f"You chose **{choice}**. I chose **{computer}**.\n{result}",
            color=0xFFDD00,
        )
        await edit_interaction_v2(interaction, embed=embed, view=self)
        self.stop()

    @discord.ui.button(label="Rock", style=discord.ButtonStyle.secondary)
    async def rock(self, interaction, _button):
        await self._play(interaction, "rock")

    @discord.ui.button(label="Paper", style=discord.ButtonStyle.secondary)
    async def paper(self, interaction, _button):
        await self._play(interaction, "paper")

    @discord.ui.button(label="Scissors", style=discord.ButtonStyle.secondary)
    async def scissors(self, interaction, _button):
        await self._play(interaction, "scissors")


class BackupRestoreView(discord.ui.View):
    def __init__(self, cog, guild, owner_id, name, snapshot):
        super().__init__(timeout=120)
        self.cog = cog
        self.guild = guild
        self.owner_id = owner_id
        self.name = name
        self.snapshot = snapshot

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await send_interaction_v2(interaction, "Only the administrator who requested this restore can confirm it.", ephemeral=True)
        return False

    @discord.ui.button(label="Restore Missing Items", style=discord.ButtonStyle.secondary)
    async def confirm(self, interaction, _button):
        await interaction.response.defer(ephemeral=False)
        roles, channels = await self.cog._restore_backup(self.guild, self.snapshot)
        self.stop()
        await interaction.followup.send(
            f"Backup `{self.name}` restored without deleting existing items. Created {roles} role(s) and {channels} channel(s).",
            ephemeral=False,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, _button):
        self.stop()
        await edit_interaction_v2(
            interaction,
            embed=discord.Embed(description="Backup restore cancelled.", color=0xFFDD00),
            view=self,
        )


class ZyreFeatures(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        db = self.bot.db
        await db.execute(
            "CREATE TABLE IF NOT EXISTS zyre_settings ("
            "guild_id INTEGER PRIMARY KEY, join_filter INTEGER NOT NULL DEFAULT 0, "
            "min_age_days INTEGER NOT NULL DEFAULT 1, no_avatar INTEGER NOT NULL DEFAULT 0, autorole_id INTEGER)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS zyre_warnings ("
            "warning_id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, "
            "user_id INTEGER NOT NULL, moderator_id INTEGER NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS zyre_afk ("
            "guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, status TEXT NOT NULL, "
            "created_at TEXT NOT NULL, PRIMARY KEY(guild_id, user_id))"
        )
        async with db.execute("PRAGMA table_info(zyre_afk)") as cursor:
            afk_columns = {row[1] for row in await cursor.fetchall()}
        if "original_nick" not in afk_columns:
            await db.execute("ALTER TABLE zyre_afk ADD COLUMN original_nick TEXT")
        await db.execute(
            "CREATE TABLE IF NOT EXISTS zyre_reminders ("
            "reminder_id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, "
            "user_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, reminder TEXT NOT NULL, due_at TEXT NOT NULL)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS zyre_backups ("
            "guild_id INTEGER NOT NULL, name TEXT NOT NULL, created_by INTEGER NOT NULL, "
            "created_at TEXT NOT NULL, snapshot TEXT NOT NULL, PRIMARY KEY(guild_id, name))"
        )
        await db.commit()
        self.reminder_worker.start()

    def cog_unload(self):
        self.reminder_worker.cancel()

    async def _ensure_settings(self, guild_id):
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO zyre_settings (guild_id) VALUES (?)", (guild_id,)
        )
        await self.bot.db.commit()

    async def _resolve_member(self, ctx, member=None, query=None):
        from main import _resolve_context_member

        return await _resolve_context_member(ctx, member, query)

    async def _set_afk_nickname(self, member: discord.Member) -> bool:
        if member.nick and member.nick.startswith(AFK_NICK_PREFIX):
            return True
        me = member.guild.me
        if (
            me is None
            or not me.guild_permissions.manage_nicknames
            or member.id == member.guild.owner_id
            or me.top_role <= member.top_role
        ):
            return False
        try:
            await member.edit(
                nick=f"{AFK_NICK_PREFIX}{member.display_name}"[:32],
                reason="Member marked themselves AFK",
            )
            return True
        except discord.Forbidden:
            LOGGER.warning(
                "Cannot set AFK nickname for member %s in guild %s due to permissions or role hierarchy.",
                member.id,
                member.guild.id,
            )
        except discord.HTTPException:
            LOGGER.exception(
                "Discord API error setting AFK nickname for member %s in guild %s.",
                member.id,
                member.guild.id,
            )
        return False

    async def _restore_afk_nickname(
        self, member: discord.Member, original_nick: str | None
    ) -> None:
        if not member.nick or not member.nick.startswith(AFK_NICK_PREFIX):
            return
        me = member.guild.me
        if (
            me is None
            or not me.guild_permissions.manage_nicknames
            or member.id == member.guild.owner_id
            or me.top_role <= member.top_role
        ):
            LOGGER.warning(
                "Cannot restore AFK nickname for member %s in guild %s due to permissions or role hierarchy.",
                member.id,
                member.guild.id,
            )
            return
        try:
            await member.edit(nick=original_nick, reason="Member returned from AFK")
        except discord.Forbidden:
            LOGGER.warning(
                "Cannot restore AFK nickname for member %s in guild %s.",
                member.id,
                member.guild.id,
                exc_info=True,
            )
        except discord.HTTPException:
            LOGGER.exception(
                "Discord API error restoring AFK nickname for member %s in guild %s.",
                member.id,
                member.guild.id,
            )

    @tasks.loop(seconds=30)
    async def reminder_worker(self):
        now = datetime.datetime.now(UTC).isoformat()
        async with self.bot.db.execute(
            "SELECT reminder_id, guild_id, user_id, channel_id, reminder "
            "FROM zyre_reminders WHERE due_at <= ? ORDER BY due_at", (now,)
        ) as cursor:
            reminders = await cursor.fetchall()
        for reminder_id, guild_id, user_id, channel_id, text in reminders:
            if not bot_owns_guild(self.bot, guild_id):
                continue
            guild = self.bot.get_guild(guild_id)
            channel = guild.get_channel(channel_id) if guild else None
            try:
                if channel:
                    await send_v2(channel, f"<@{user_id}> Reminder: {text}", allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=user_id)]))
                else:
                    user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                    await user.send(f"Reminder from {guild.name if guild else 'Falcon'}: {text}")
            except discord.HTTPException as error:
                LOGGER.warning(
                    "Could not deliver reminder %s; it will be retried: %s",
                    reminder_id,
                    error,
                )
                continue
            await self.bot.db.execute("DELETE FROM zyre_reminders WHERE reminder_id=?", (reminder_id,))
        if reminders:
            await self.bot.db.commit()

    @reminder_worker.before_loop
    async def before_reminder_worker(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_member_join(self, member):
        await self._ensure_settings(member.guild.id)
        async with self.bot.db.execute(
            "SELECT join_filter, min_age_days, no_avatar, autorole_id FROM zyre_settings WHERE guild_id=?",
            (member.guild.id,),
        ) as cursor:
            settings = await cursor.fetchone()
        if settings[0]:
            age_days = (datetime.datetime.now(UTC) - member.created_at).days
            reasons = []
            if age_days < settings[1]:
                reasons.append(f"Your account is younger than the server's {settings[1]}-day minimum.")
            if settings[2] and member.avatar is None:
                reasons.append("This server requires a profile avatar.")
            if reasons:
                try:
                    await member.send(
                        f"You were removed from **{member.guild.name}** by its account-age/profile filter.\n"
                        + "\n".join(f"• {reason}" for reason in reasons)
                    )
                except discord.Forbidden:
                    LOGGER.info(
                        "Could not DM join-filter notice to member %s in guild %s; DMs are unavailable.",
                        member.id,
                        member.guild.id,
                    )
                except discord.HTTPException:
                    LOGGER.exception(
                        "Discord API error sending join-filter notice to member %s in guild %s.",
                        member.id,
                        member.guild.id,
                    )
                try:
                    await member.kick(reason="Join filter: " + " ".join(reasons))
                except discord.Forbidden:
                    LOGGER.warning(
                        "Could not kick join-filter member %s in guild %s due to permissions or role hierarchy.",
                        member.id,
                        member.guild.id,
                        exc_info=True,
                    )
                except discord.HTTPException:
                    LOGGER.exception(
                        "Discord API error kicking join-filter member %s in guild %s.",
                        member.id,
                        member.guild.id,
                    )
                return
        if settings[3]:
            role = member.guild.get_role(settings[3])
            if role:
                try:
                    await member.add_roles(role, reason="Configured automatic role")
                except discord.HTTPException:
                    pass

    async def process_message(self, message):
        if message.guild is None or message.author.bot:
            return False
        guild_id, user_id = message.guild.id, message.author.id
        await self._ensure_settings(guild_id)
        async with self.bot.db.execute(
            "SELECT status, original_nick FROM zyre_afk WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        ) as cursor:
            afk = await cursor.fetchone()
        if afk:
            await self._restore_afk_nickname(message.author, afk[1])
            await self.bot.db.execute("DELETE FROM zyre_afk WHERE guild_id=? AND user_id=?", (guild_id, user_id))
            await self.bot.db.commit()
            await send_v2(message.channel, f"Welcome back {message.author.mention}; your AFK status was cleared.", delete_after=5)
        for mentioned in message.mentions:
            async with self.bot.db.execute(
                "SELECT status FROM zyre_afk WHERE guild_id=? AND user_id=?", (guild_id, mentioned.id)
            ) as cursor:
                afk = await cursor.fetchone()
            if afk:
                await send_v2(message.channel, f"{mentioned.display_name} is AFK: {afk[0]}", delete_after=8)

        return False

    @commands.hybrid_command(name="warn", description="Record a member warning")
    @commands.has_guild_permissions(manage_messages=True)
    async def warn(self, ctx, member: discord.Member = None, *, reason: str = "No reason provided"):
        member = await self._resolve_member(ctx, member)
        if member is None:
            return await ctx.send("Mention a member or reply to their message to warn them.")
        await self.bot.db.execute(
            "INSERT INTO zyre_warnings (guild_id,user_id,moderator_id,reason,created_at) VALUES (?,?,?,?,?)",
            (ctx.guild.id, member.id, ctx.author.id, reason[:1000], datetime.datetime.now(UTC).isoformat()),
        )
        await self.bot.db.commit()
        async with self.bot.db.execute(
            "SELECT COUNT(*) FROM zyre_warnings WHERE guild_id=? AND user_id=?", (ctx.guild.id, member.id)
        ) as cursor:
            count = (await cursor.fetchone())[0]
        await ctx.send(
            f"{SUCCESS_TICK} **Warning Recorded**\n\n"
            f"{DETAIL_ARROW} {member.mention} has {count} active warning(s)."
        )

    @commands.hybrid_command(name="warnings", description="View a member's active warnings")
    @commands.has_guild_permissions(manage_messages=True)
    async def warnings(self, ctx, member: discord.Member = None):
        member = await self._resolve_member(ctx, member) or ctx.author
        async with self.bot.db.execute(
            "SELECT warning_id, moderator_id, reason, created_at FROM zyre_warnings "
            "WHERE guild_id=? AND user_id=? ORDER BY warning_id DESC LIMIT 20",
            (ctx.guild.id, member.id),
        ) as cursor:
            rows = await cursor.fetchall()
        if not rows:
            return await ctx.send(f"{member.mention} has no active warnings.")

        lines = []
        for warning_id, moderator_id, reason, created_at in rows:
            try:
                timestamp = datetime.datetime.fromisoformat(created_at)
                when = discord.utils.format_dt(timestamp, "R")
            except (TypeError, ValueError):
                when = "time unavailable"
            lines.append(
                f"`{warning_id}` · {when} · Moderator <@{moderator_id}>\n"
                f"{discord.utils.escape_markdown(reason[:300])}"
            )
        embed = discord.Embed(
            title=f"Warnings for {member}",
            description="\n\n".join(lines)[:4000],
            color=0xFFDD00,
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_command(name="clearwarnings", description="Clear all warnings for a member")
    @commands.has_guild_permissions(manage_messages=True)
    async def clearwarnings(self, ctx, member: discord.Member):
        async with self.bot.db.execute(
            "DELETE FROM zyre_warnings WHERE guild_id=? AND user_id=?",
            (ctx.guild.id, member.id),
        ) as cursor:
            removed = cursor.rowcount
        await self.bot.db.commit()
        await ctx.send(
            f"{SUCCESS_TICK} **Warnings Cleared**\n\n"
            f"{DETAIL_ARROW} Cleared {removed} warning(s) for {member.mention}."
        )

    @commands.hybrid_command(name="unwarn", description="Remove a member warning by ID")
    @commands.has_guild_permissions(manage_messages=True)
    async def unwarn(self, ctx, warning_id: int, member: discord.Member = None):
        member = await self._resolve_member(ctx, member)
        if member is None:
            return await ctx.send("Mention a member or reply to their message to remove a warning.")
        await self.bot.db.execute(
            "DELETE FROM zyre_warnings WHERE warning_id=? AND guild_id=? AND user_id=?",
            (warning_id, ctx.guild.id, member.id),
        )
        await self.bot.db.commit()
        await ctx.send(
            f"{SUCCESS_TICK} **Warning Removed**\n\n"
            f"{DETAIL_ARROW} Warning `{warning_id}` removed for {member.mention}, if it existed."
        )

    @commands.hybrid_command(name="slowmode", description="Set channel slowmode")
    @commands.has_guild_permissions(manage_channels=True)
    async def slowmode(self, ctx, delay: int, channel: discord.TextChannel = None):
        if not 0 <= delay <= 21600:
            return await ctx.send("Slowmode must be between 0 and 21600 seconds.")
        channel = channel or ctx.channel
        await channel.edit(slowmode_delay=delay, reason=f"Slowmode changed by {ctx.author}")
        await ctx.send(f"Slowmode set to {delay}s in {channel.mention}.")

    async def _set_channel_lock(self, ctx, channel, locked):
        channel = channel or ctx.channel
        await channel.set_permissions(
            ctx.guild.default_role,
            send_messages=not locked,
            reason=f"Channel {'locked' if locked else 'unlocked'} by {ctx.author}",
        )
        await ctx.send(f"{channel.mention} {'locked' if locked else 'unlocked'}.")

    @commands.hybrid_command(name="channel_lock", description="Lock a text channel")
    @commands.has_guild_permissions(manage_channels=True)
    async def channel_lock(self, ctx, channel: discord.TextChannel = None):
        await self._set_channel_lock(ctx, channel, True)

    @commands.hybrid_command(name="channel_unlock", description="Unlock a text channel")
    @commands.has_guild_permissions(manage_channels=True)
    async def channel_unlock(self, ctx, channel: discord.TextChannel = None):
        await self._set_channel_lock(ctx, channel, False)

    async def _change_role(self, ctx, member, role, add):
        if member is None or role is None:
            return await ctx.send("Provide both a member and role.")
        bot_member = ctx.guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            return await ctx.send("I need Manage Roles permission.")
        if role.is_default() or role >= bot_member.top_role:
            return await ctx.send("That role must be below my top role and cannot be @everyone.")
        if add:
            await member.add_roles(role, reason=f"Role added by {ctx.author}")
        else:
            await member.remove_roles(role, reason=f"Role removed by {ctx.author}")
        action = "added to" if add else "removed from"
        title = "Role Added" if add else "Role Removed"
        await ctx.send(
            f"{SUCCESS_TICK} **{title}**\n\n"
            f"{DETAIL_ARROW} {role.mention} {action} {member.mention}."
        )

    @commands.hybrid_command(name="addrole", description="Add a role to a member")
    @commands.has_guild_permissions(manage_roles=True)
    async def addrole(self, ctx, member: discord.Member, role: discord.Role):
        await self._change_role(ctx, member, role, True)

    @commands.hybrid_command(name="removerole", description="Remove a role from a member")
    @commands.has_guild_permissions(manage_roles=True)
    async def removerole(self, ctx, member: discord.Member, role: discord.Role):
        await self._change_role(ctx, member, role, False)

    @commands.hybrid_command(name="rolename", description="Rename a role")
    @commands.has_guild_permissions(manage_roles=True)
    async def rolename(self, ctx, role: discord.Role, *, name: str):
        if role.is_default() or role >= ctx.guild.me.top_role:
            return await ctx.send("That role cannot be renamed by me.")
        old_name = role.name
        await role.edit(name=name[:100], reason=f"Role renamed by {ctx.author}")
        await ctx.send(f"Renamed **{old_name}** to **{role.name}**.")

    @commands.hybrid_command(name="remind", description="Set a reminder")
    async def remind(self, ctx, duration: str, *, reminder: str):
        await self.remindme(ctx, duration, reminder=reminder)

    @commands.hybrid_command(name="remindme", description="Set a reminder")
    async def remindme(self, ctx, duration: str, *, reminder: str):
        try:
            parsed = parse_duration(duration)
        except (ValueError, OverflowError) as error:
            return await ctx.send(str(error))
        if parsed is None or not reminder.strip():
            return await ctx.send("Provide a finite duration and reminder text.")
        guild_id = ctx.guild.id if ctx.guild else 0
        async with self.bot.db.execute(
            "SELECT COUNT(*) FROM zyre_reminders WHERE guild_id=? AND user_id=?",
            (guild_id, ctx.author.id),
        ) as cursor:
            active_count = (await cursor.fetchone())[0]
        if active_count >= 5:
            return await ctx.send("You can have at most 5 active reminders in this server or DM.")
        due = datetime.datetime.now(UTC) + parsed
        await self.bot.db.execute(
            "INSERT INTO zyre_reminders (guild_id,user_id,channel_id,reminder,due_at) VALUES (?,?,?,?,?)",
            (guild_id, ctx.author.id, ctx.channel.id, reminder.strip()[:1000], due.isoformat()),
        )
        await self.bot.db.commit()
        await ctx.send(f"Reminder set for <t:{int(due.timestamp())}:R>.")

    @commands.hybrid_command(name="reminders_list", description="List your active reminders")
    async def reminders_list(self, ctx):
        guild_id = ctx.guild.id if ctx.guild else 0
        async with self.bot.db.execute(
            "SELECT reminder_id, reminder, due_at FROM zyre_reminders "
            "WHERE guild_id=? AND user_id=? ORDER BY due_at LIMIT 20",
            (guild_id, ctx.author.id),
        ) as cursor:
            rows = await cursor.fetchall()
        if not rows:
            return await ctx.send("You have no active reminders here.")

        lines = []
        for reminder_id, reminder, due_at in rows:
            try:
                timestamp = datetime.datetime.fromisoformat(due_at)
                due = discord.utils.format_dt(timestamp, "R")
            except (TypeError, ValueError):
                due = "time unavailable"
            lines.append(f"`{reminder_id}` · {due}\n{reminder[:250]}")
        embed = discord.Embed(
            title="Your Active Reminders",
            description="\n\n".join(lines)[:4000],
            color=0xFFDD00,
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="reminders_cancel", description="Cancel one of your reminders")
    async def reminders_cancel(self, ctx, reminder_id: int):
        guild_id = ctx.guild.id if ctx.guild else 0
        async with self.bot.db.execute(
            "DELETE FROM zyre_reminders WHERE reminder_id=? AND guild_id=? AND user_id=?",
            (reminder_id, guild_id, ctx.author.id),
        ) as cursor:
            removed = cursor.rowcount
        await self.bot.db.commit()
        if removed:
            await ctx.send(f"Reminder `{reminder_id}` cancelled.")
        else:
            await ctx.send("No active reminder with that ID belongs to you.")

    @commands.hybrid_command(name="backup_create", description="Save a server structure backup")
    @commands.has_guild_permissions(administrator=True)
    async def backup_create(self, ctx, *, name: str):
        name = name.strip()[:80]
        if not name:
            return await ctx.send("Enter a backup name.")
        guild = ctx.guild
        created_at = datetime.datetime.now(UTC).isoformat()
        snapshot = {
            "metadata": {
                "format": "falcon-role-channel-structure-v1",
                "guild_id": guild.id,
                "guild_name": guild.name,
                "backup_name": name,
                "created_at": created_at,
            },
            "roles": [
                {"name": role.name, "color": role.color.value, "permissions": role.permissions.value,
                 "hoist": role.hoist, "mentionable": role.mentionable}
                for role in guild.roles if not role.is_default() and not role.managed
            ],
            "categories": [category.name for category in guild.categories],
            "channels": [
                {"name": channel.name, "type": str(channel.type),
                 "category": channel.category.name if channel.category else None,
                 "topic": getattr(channel, "topic", None), "nsfw": getattr(channel, "nsfw", False),
                 "slowmode": getattr(channel, "slowmode_delay", 0)}
                for channel in guild.channels if not isinstance(channel, discord.CategoryChannel)
                and str(channel.type) in {"text", "news", "voice"}
            ],
        }
        await self.bot.db.execute(
            "INSERT INTO zyre_backups (guild_id,name,created_by,created_at,snapshot) VALUES (?,?,?,?,?) "
            "ON CONFLICT(guild_id,name) DO UPDATE SET created_by=excluded.created_by, "
            "created_at=excluded.created_at, snapshot=excluded.snapshot",
            (guild.id, name, ctx.author.id, created_at, json.dumps(snapshot)),
        )
        await self.bot.db.commit()
        snapshot_bytes = json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8")
        filename_name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")[:40] or "backup"
        try:
            await ctx.author.send(
                content=f"Falcon server structure backup `{name}` for **{guild.name}**.",
                file=discord.File(
                    io.BytesIO(snapshot_bytes),
                    filename=f"falcon-backup-{guild.id}-{filename_name}.json",
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as error:
            await ctx.send(
                f"Backup `{name}` saved, but I couldn't DM the JSON file: {error}"
            )
        else:
            await ctx.send(f"Backup `{name}` saved and the JSON export was sent to your DMs.")

    @commands.hybrid_command(name="backup_list", description="List server structure backups")
    @commands.has_guild_permissions(manage_guild=True)
    async def backup_list(self, ctx):
        async with self.bot.db.execute(
            "SELECT name, created_at FROM zyre_backups WHERE guild_id=? ORDER BY created_at DESC",
            (ctx.guild.id,),
        ) as cursor:
            rows = await cursor.fetchall()
        if not rows:
            return await ctx.send("No server backups have been saved.")
        description = "\n".join(f"`{name}` · <t:{int(datetime.datetime.fromisoformat(date).timestamp())}:R>" for name, date in rows[:25])
        await ctx.send(embed=discord.Embed(title="Server Backups", description=description, color=0xFFDD00))

    @commands.hybrid_command(name="backup_load", description="Restore missing items from a backup")
    @commands.has_guild_permissions(administrator=True)
    async def backup_load(self, ctx, *, name: str):
        async with self.bot.db.execute(
            "SELECT snapshot FROM zyre_backups WHERE guild_id=? AND name=?", (ctx.guild.id, name.strip())
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return await ctx.send(f"Backup `{name}` was not found.")
        snapshot = json.loads(row[0])
        view = BackupRestoreView(self, ctx.guild, ctx.author.id, name, snapshot)
        embed = discord.Embed(
            title="Confirm Non-destructive Restore",
            description="This will create missing roles and channels. Existing server items will not be deleted or overwritten.",
            color=0xFFDD00,
        )
        await ctx.send(embed=embed, view=view, ephemeral=ctx.interaction is not None)

    @commands.hybrid_command(name="backup_restore", description="Preview a JSON structure backup before restoring")
    @commands.has_guild_permissions(administrator=True)
    async def backup_restore(self, ctx, attachment: discord.Attachment):
        if ctx.guild is None:
            return await ctx.send("Backups can only be restored in a server.")
        if attachment.size > 8 * 1024 * 1024:
            return await ctx.send("The backup file must be 8 MiB or smaller.")
        try:
            snapshot = json.loads((await attachment.read()).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, discord.HTTPException) as error:
            return await ctx.send(f"That attachment is not a readable UTF-8 JSON backup: {error}")

        if not isinstance(snapshot, dict):
            return await ctx.send("The backup must contain a JSON object.")
        roles = snapshot.get("roles")
        categories = snapshot.get("categories")
        channels = snapshot.get("channels")
        if not isinstance(roles, list) or not isinstance(categories, list) or not isinstance(channels, list):
            return await ctx.send("The JSON file is missing roles, categories, or channels lists.")
        if any(
            not isinstance(item, dict)
            or not isinstance(item.get("name"), str)
            or not item["name"].strip()
            or not isinstance(item.get("color"), int)
            or isinstance(item.get("color"), bool)
            or not 0 <= item["color"] <= 0xFFFFFF
            or not isinstance(item.get("permissions"), int)
            or isinstance(item.get("permissions"), bool)
            or not 0 <= item["permissions"] < (1 << 53)
            or not isinstance(item.get("hoist"), bool)
            or not isinstance(item.get("mentionable"), bool)
            for item in roles
        ):
            return await ctx.send("The backup contains an invalid role entry.")
        if any(not isinstance(name, str) or not name.strip() for name in categories):
            return await ctx.send("The backup contains an invalid category name.")
        if any(
            not isinstance(item, dict)
            or not isinstance(item.get("name"), str)
            or not item["name"].strip()
            or item.get("type") not in {"text", "news", "voice"}
            or (item.get("category") is not None and not isinstance(item.get("category"), str))
            or (item.get("topic") is not None and not isinstance(item.get("topic"), str))
            or not isinstance(item.get("nsfw", False), bool)
            or not isinstance(item.get("slowmode", 0), int)
            or isinstance(item.get("slowmode", 0), bool)
            or not 0 <= item.get("slowmode", 0) <= 21600
            for item in channels
        ):
            return await ctx.send("The backup contains an invalid channel entry.")

        backup_name = attachment.filename[:80] or "uploaded-backup"
        view = BackupRestoreView(self, ctx.guild, ctx.author.id, backup_name, snapshot)
        embed = discord.Embed(
            title="Confirm Non-destructive Restore",
            description=(
                f"Backup file: `{discord.utils.escape_markdown(backup_name)}`.\n"
                "This will create missing roles and channels only. Existing server items will not be deleted or overwritten."
            ),
            color=0xFFDD00,
        )
        await ctx.send(embed=embed, view=view, ephemeral=ctx.interaction is not None)

    async def _restore_backup(self, guild, snapshot):
        role_names = {role.name.casefold() for role in guild.roles}
        role_count = 0
        for data in snapshot.get("roles", []):
            if data["name"].casefold() in role_names:
                continue
            try:
                await guild.create_role(
                    name=data["name"], color=discord.Color(data["color"]),
                    permissions=discord.Permissions(data["permissions"]),
                    hoist=data["hoist"], mentionable=data["mentionable"], reason="Non-destructive backup restore",
                )
                role_count += 1
            except discord.HTTPException:
                continue

        categories = {category.name.casefold(): category for category in guild.categories}
        for name in snapshot.get("categories", []):
            if name.casefold() not in categories:
                try:
                    categories[name.casefold()] = await guild.create_category(name=name)
                except discord.HTTPException:
                    continue

        existing = {(channel.name.casefold(), str(channel.type), channel.category.name.casefold() if channel.category else None) for channel in guild.channels}
        channel_count = 0
        for data in snapshot.get("channels", []):
            category_key = data.get("category")
            category = categories.get(category_key.casefold()) if category_key else None
            key = (data["name"].casefold(), data["type"], category_key.casefold() if category_key else None)
            if key in existing:
                continue
            try:
                if data["type"] in {"text", "news"}:
                    await guild.create_text_channel(
                        name=data["name"], category=category, topic=data.get("topic"),
                        nsfw=data.get("nsfw", False), slowmode_delay=data.get("slowmode", 0),
                    )
                elif data["type"] == "voice":
                    await guild.create_voice_channel(name=data["name"], category=category)
                else:
                    continue
                existing.add(key)
                channel_count += 1
            except discord.HTTPException:
                continue
        return role_count, channel_count

    @commands.hybrid_command(name="servericon", description="Show this server's icon")
    async def servericon(self, ctx):
        if ctx.guild.icon is None:
            return await ctx.send("This server does not have an icon.")
        embed = discord.Embed(title=f"{ctx.guild.name} icon", color=0xFFDD00)
        embed.set_image(url=ctx.guild.icon.url)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="profile", description="Show a member profile")
    async def profile(self, ctx, member: discord.Member = None):
        member = await self._resolve_member(ctx, member) or ctx.author
        embed = discord.Embed(title=member.display_name, color=0xFFDD00)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Joined", value=discord.utils.format_dt(member.joined_at, "R") if member.joined_at else "Unknown")
        embed.add_field(name="Account created", value=discord.utils.format_dt(member.created_at, "R"))
        embed.add_field(name="Roles", value=str(max(len(member.roles) - 1, 0)))
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="whois", description="Show member information")
    async def whois(self, ctx, member: discord.Member = None):
        member = await self._resolve_member(ctx, member) or ctx.author
        embed = discord.Embed(title=member.display_name, color=0xFFDD00)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="ID", value=str(member.id))
        embed.add_field(name="Joined", value=discord.utils.format_dt(member.joined_at, "R") if member.joined_at else "Unknown")
        embed.add_field(name="Created", value=discord.utils.format_dt(member.created_at, "R"))
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="setnick", description="Set a member nickname by mention or reply")
    @commands.has_guild_permissions(manage_nicknames=True)
    async def setnick(self, ctx, *, arguments: str = ""):
        member = await self._resolve_member(ctx, query=arguments)
        if member is None:
            return await ctx.send("Mention a member or reply to their message to set a nickname.")
        from main import _remove_target_mention

        nickname = _remove_target_mention(arguments, member)[:32]
        if not nickname:
            return await ctx.send("Provide the new nickname after the member mention, or reply with `,setnick <name>`.")
        try:
            await member.edit(nick=nickname, reason=f"Nickname changed by {ctx.author}")
        except discord.Forbidden:
            return await ctx.send("I cannot change that member's nickname. Check role order and permissions.")
        await ctx.send(f"Changed {member.mention}'s nickname to **{nickname}**.")

    @commands.hybrid_command(name="clean", description="Delete recent bot messages")
    @commands.has_guild_permissions(manage_messages=True)
    async def clean(self, ctx, amount: int = 100):
        command = self.bot.get_command("purgebot")
        if command is None:
            return await ctx.send("Bot-message purge command is unavailable.")
        await ctx.invoke(command, amount=amount)

    async def _timeout_from_arguments(self, ctx, arguments):
        member = await self._resolve_member(ctx, query=arguments)
        if member is None:
            return await ctx.send("Mention a member or reply to their message to timeout them.")
        from main import _remove_target_mention

        remaining = _remove_target_mention(arguments, member)
        pieces = remaining.split(maxsplit=1)
        duration_text = pieces[0] if pieces else "10min"
        reason = pieces[1] if len(pieces) > 1 else "No reason provided"
        try:
            duration = parse_duration(duration_text)
        except (ValueError, OverflowError) as error:
            return await ctx.send(str(error))
        if duration is None or duration <= datetime.timedelta(0) or duration > datetime.timedelta(days=28):
            return await ctx.send("Timeout duration must be greater than zero and no more than 28 days.")
        try:
            await member.timeout(datetime.datetime.now(UTC) + duration, reason=reason)
        except discord.HTTPException as error:
            return await ctx.send(f"Could not timeout {member.mention}: {error}")
        await ctx.send(f"{member.mention} timed out for {duration_text}. Reason: {reason}")

    @commands.hybrid_command(name="timeout", description="Timeout a member by mention or reply")
    @commands.has_guild_permissions(moderate_members=True)
    async def timeout(self, ctx, *, arguments: str = ""):
        await self._timeout_from_arguments(ctx, arguments)

    @commands.hybrid_command(name="tempmute", description="Temporarily mute a member")
    @commands.has_guild_permissions(moderate_members=True)
    async def tempmute(self, ctx, *, arguments: str = ""):
        await self._timeout_from_arguments(ctx, arguments)

    @commands.hybrid_command(name="afk", description="Set your AFK status")
    async def afk(self, ctx, *, status: str = "AFK"):
        if ctx.guild is None:
            return await ctx.send("AFK status is available in servers only.")
        await self.bot.db.execute(
            "INSERT INTO zyre_afk (guild_id,user_id,status,created_at,original_nick) VALUES (?,?,?,?,?) "
            "ON CONFLICT(guild_id,user_id) DO UPDATE SET status=excluded.status, created_at=excluded.created_at",
            (
                ctx.guild.id,
                ctx.author.id,
                status[:200],
                datetime.datetime.now(UTC).isoformat(),
                ctx.author.nick,
            ),
        )
        await self.bot.db.commit()
        await self._set_afk_nickname(ctx.author)
        await ctx.send(f"AFK status set: {status[:200]}.")

    @commands.hybrid_command(name="sync", description="Sync slash commands")
    @commands.has_guild_permissions(administrator=True)
    async def sync(self, ctx):
        synced = await self.bot.tree.sync()
        await ctx.send(f"Synced {len(synced)} global slash commands.", ephemeral=ctx.interaction is not None)

    @commands.hybrid_command(name="autorole_setup", description="Configure an automatic member role")
    @commands.has_guild_permissions(manage_guild=True)
    async def autorole_setup(self, ctx, enabled: bool, role: discord.Role = None):
        await self._ensure_settings(ctx.guild.id)
        if enabled and role is None:
            return await ctx.send("Choose a role when enabling autorole.")
        if role and (role.is_default() or role >= ctx.guild.me.top_role):
            return await ctx.send("The role must be below my highest role.")
        await self.bot.db.execute(
            "UPDATE zyre_settings SET autorole_id=? WHERE guild_id=?",
            (role.id if enabled and role else None, ctx.guild.id),
        )
        await self.bot.db.commit()
        await ctx.send(f"Autorole {'enabled for ' + role.mention if enabled and role else 'disabled'}.")

    @commands.hybrid_command(name="joinfilter_setup", description="Configure account-age and avatar join filters")
    @commands.has_guild_permissions(manage_guild=True)
    async def joinfilter_setup(self, ctx, enabled: bool, min_age_days: int = 1, no_avatar: bool = False):
        if not 0 <= min_age_days <= 3650:
            return await ctx.send("Minimum account age must be between 0 and 3650 days.")
        await self._ensure_settings(ctx.guild.id)
        await self.bot.db.execute(
            "UPDATE zyre_settings SET join_filter=?, min_age_days=?, no_avatar=? WHERE guild_id=?",
            (int(enabled), min_age_days, int(no_avatar), ctx.guild.id),
        )
        await self.bot.db.commit()
        await ctx.send(f"Join filter {'enabled' if enabled else 'disabled'} (minimum age {min_age_days} days; require avatar: {no_avatar}).")

    @commands.hybrid_command(name="roll", description="Roll a six-sided die")
    async def roll(self, ctx):
        await ctx.send(f"🎲 You rolled **{random.randint(1, 6)}**.")

    @commands.hybrid_command(name="rps", description="Play rock, paper, scissors")
    async def rps(self, ctx):
        await ctx.send(embed=discord.Embed(title="Rock, Paper, Scissors", description="Choose your move.", color=0xFFDD00), view=RPSView(ctx.author.id))

    @commands.hybrid_command(
        name="poll",
        description="Create a two-option poll, optionally visible only to you",
    )
    async def poll(
        self,
        ctx,
        question: str,
        option_one: str,
        option_two: str,
        private: bool = False,
    ):
        view = PollView(question[:256], [option_one[:80], option_two[:80]], ctx.author.id)
        embed = discord.Embed(title=question[:256], color=0xFFDD00)
        embed.add_field(name=option_one[:80], value="0 votes", inline=False)
        embed.add_field(name=option_two[:80], value="0 votes", inline=False)
        await ctx.send(
            embed=embed,
            view=view,
            ephemeral=bool(private and ctx.interaction is not None),
        )

    @commands.hybrid_command(name="giveaway_start", description="Start a giveaway")
    @commands.has_guild_permissions(manage_guild=True)
    async def giveaway_start(self, ctx, duration: str, winners: int, *, prize: str):
        command = self.bot.get_command("gcreate")
        if command is None:
            return await ctx.send("Falcon giveaway command is unavailable.")
        await ctx.invoke(command, time=duration, winners=winners, prize=prize)

    @commands.hybrid_command(name="giveaway_reroll", description="Reroll the latest giveaway")
    @commands.has_guild_permissions(manage_guild=True)
    async def giveaway_reroll(self, ctx):
        command = self.bot.get_command("greroll")
        if command is None:
            return await ctx.send("Falcon giveaway command is unavailable.")
        await ctx.invoke(command)

    @commands.hybrid_command(name="giveaway_end", description="End the active giveaway")
    @commands.has_guild_permissions(manage_guild=True)
    async def giveaway_end(self, ctx):
        command = self.bot.get_command("gend")
        if command is None:
            return await ctx.send("Falcon giveaway command is unavailable.")
        await ctx.invoke(command)


async def setup(bot):
    await bot.add_cog(ZyreFeatures(bot))
