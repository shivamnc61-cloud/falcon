"""Guild-scoped XP and leveling features for Falcon (discord.py 2.x)."""

import asyncio
import datetime
import io
import json
import logging
import math
import random
from typing import Literal

import aiohttp
import aiosqlite
import discord
from PIL import Image, ImageDraw, ImageFont, ImageOps
from discord.ext import commands, tasks

from card_theme import draw_bot_icon, normalize_card_text


UTC = datetime.timezone.utc
MAX_XP = 1_000_000
MAX_LEVELING_BACKUP_BYTES = 8 * 1024 * 1024
log = logging.getLogger(__name__)


def level_for_xp(xp: int) -> int:
    """Return the level for cumulative XP; each level requires a larger square threshold."""
    return math.isqrt(max(0, int(xp)) // 100)


def xp_for_level(level: int) -> int:
    """Return the cumulative XP threshold for a level."""
    return max(0, int(level)) ** 2 * 100


def xp_progress(xp: int) -> tuple[int, int, int]:
    """Return current level, XP earned within it, and XP needed for the next level."""
    xp = max(0, int(xp))
    level = level_for_xp(xp)
    current = xp - xp_for_level(level)
    return level, current, xp_for_level(level + 1) - xp_for_level(level)


def format_voice_time(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    return f"{hours:,}h {minutes:02d}m"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(UTC)


def _timestamp(value: datetime.datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _parse_timestamp(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def render_rank_card(
    avatar_bytes: bytes,
    username: str,
    level: int,
    xp: int,
    rank: int,
    streak: int | None = None,
    voice_seconds: int = 0,
    bot_icon_bytes: bytes | None = None,
) -> bytes:
    width, height = 1200, 560
    image = Image.new("RGB", (width, height), "#FFF9F5")
    draw = ImageDraw.Draw(image)
    for y in range(height):
        blend = y / max(1, height - 1)
        color = (
            round(255 - 6 * blend),
            round(250 - 19 * blend),
            round(246 - 12 * blend),
        )
        draw.line((0, y, width, y), fill=color)
    draw.rounded_rectangle(
        (24, 24, width - 24, height - 24),
        radius=34,
        fill="#FFFEFC",
        outline="#E7C78F",
        width=3,
    )
    draw.rounded_rectangle((45, 45, 60, height - 45), radius=7, fill="#F5C7D1")
    draw_bot_icon(image, (1010, 52, 1158, 188), bot_icon_bytes)

    try:
        title_font = ImageFont.truetype("DejaVuSans-Bold.ttf", 49)
        stat_font = ImageFont.truetype("DejaVuSans-Bold.ttf", 43)
        label_font = ImageFont.truetype("DejaVuSans.ttf", 31)
        small_font = ImageFont.truetype("DejaVuSans.ttf", 29)
        eyebrow_font = ImageFont.truetype("DejaVuSans-Bold.ttf", 29)
    except OSError:
        title_font = ImageFont.load_default()
        stat_font = title_font
        label_font = title_font
        small_font = title_font
        eyebrow_font = title_font

    avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
    avatar = ImageOps.fit(avatar, (216, 216))
    mask = Image.new("L", (216, 216), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, 215, 215), radius=28, fill=255)
    image.paste(avatar, (82, 174), mask)
    draw.rounded_rectangle((76, 168, 304, 396), radius=34, outline="#E7C78F", width=4)

    safe_name = normalize_card_text(username, title_font)
    while safe_name and draw.textlength(safe_name, font=title_font) > 620:
        safe_name = safe_name[:-2].rstrip() + "…"
    draw.text((352, 60), "F A L C O N   /   L E V E L I N G", font=eyebrow_font, fill="#9A7A55")
    draw.text((352, 106), safe_name, font=title_font, fill="#422C34")
    draw.text(
        (354, 185),
        f"LEVEL {level}",
        font=stat_font,
        fill="#A23B5A",
    )
    draw.text(
        (640, 190),
        f"SERVER RANK  #{rank}",
        font=label_font,
        fill="#806D70",
    )
    current_xp = xp - xp_for_level(level)
    required_xp = xp_for_level(level + 1) - xp_for_level(level)
    progress = min(1.0, max(0.0, current_xp / max(1, required_xp)))
    draw.text(
        (354, 255),
        f"{current_xp:,} / {required_xp:,} XP TO NEXT LEVEL",
        font=label_font,
        fill="#806D70",
    )
    draw.rounded_rectangle((352, 305, 1112, 339), radius=17, fill="#F2E4E3")
    progress_width = int(760 * progress)
    if progress_width:
        draw.rounded_rectangle(
            (352, 305, 352 + progress_width, 339),
            radius=17,
            fill="#E56B88",
        )
    draw.text(
        (354, 354),
        f"{xp:,} TOTAL XP",
        font=label_font,
        fill="#A23B5A",
    )
    draw.text(
        (354, 407),
        f"VOICE TIME   {format_voice_time(voice_seconds)}",
        font=label_font,
        fill="#806D70",
    )
    if streak is not None:
        draw.text(
            (1110, 407),
            f"{streak} DAY STREAK",
            font=label_font,
            fill="#A23B5A",
            anchor="ra",
        )
    draw.text(
        (82, 431),
        "PROGRESS",
        font=small_font,
        fill="#A88D81",
    )

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


class LevelingFeatures(commands.Cog):
    """Persistent XP, ranks, daily streaks, voice XP, and configurable role rewards."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._xp_lock = asyncio.Lock()
        self._voice_seen: dict[tuple[int, int], float] = {}
        self._voice_activity_seen: dict[tuple[int, int], float] = {}
        self._last_event_cleanup = 0.0

    async def cog_load(self) -> None:
        db = self.bot.db
        await db.execute(
            "CREATE TABLE IF NOT EXISTS leveling_settings ("
            "guild_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0, "
            "xp_min INTEGER NOT NULL DEFAULT 15, xp_max INTEGER NOT NULL DEFAULT 25, "
            "cooldown_seconds INTEGER NOT NULL DEFAULT 60, multiplier REAL NOT NULL DEFAULT 1.0, "
            "voice_xp INTEGER NOT NULL DEFAULT 10, voice_interval_seconds INTEGER NOT NULL DEFAULT 60, "
            "levelup_card_enabled INTEGER NOT NULL DEFAULT 0, "
            "daily_reward INTEGER NOT NULL DEFAULT 100)"
        )
        async with db.execute("PRAGMA table_info(leveling_settings)") as cursor:
            setting_columns = {row[1] for row in await cursor.fetchall()}
        if "levelup_card_enabled" not in setting_columns:
            await db.execute(
                "ALTER TABLE leveling_settings ADD COLUMN levelup_card_enabled INTEGER NOT NULL DEFAULT 0"
            )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS leveling_users ("
            "guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, xp INTEGER NOT NULL DEFAULT 0, "
            "streak INTEGER NOT NULL DEFAULT 0, last_claim_date TEXT, last_message_xp TEXT, "
            "updated_at TEXT NOT NULL, voice_seconds INTEGER NOT NULL DEFAULT 0, "
            "PRIMARY KEY(guild_id, user_id))"
        )
        async with db.execute("PRAGMA table_info(leveling_users)") as cursor:
            user_columns = {row[1] for row in await cursor.fetchall()}
        if "voice_seconds" not in user_columns:
            await db.execute(
                "ALTER TABLE leveling_users ADD COLUMN voice_seconds INTEGER NOT NULL DEFAULT 0"
            )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS leveling_blacklist ("
            "guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, "
            "PRIMARY KEY(guild_id, user_id))"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS leveling_rewards ("
            "guild_id INTEGER NOT NULL, level INTEGER NOT NULL, role_id INTEGER NOT NULL, "
            "PRIMARY KEY(guild_id, level))"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS leveling_message_events ("
            "guild_id INTEGER NOT NULL, message_id INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT '', "
            "PRIMARY KEY(guild_id, message_id))"
        )
        async with db.execute("PRAGMA table_info(leveling_message_events)") as cursor:
            event_columns = {row[1] for row in await cursor.fetchall()}
        if "created_at" not in event_columns:
            await db.execute(
                "ALTER TABLE leveling_message_events ADD COLUMN created_at TEXT NOT NULL DEFAULT ''"
            )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS leveling_message_events_created_idx "
            "ON leveling_message_events(created_at)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS leveling_users_rank_idx "
            "ON leveling_users(guild_id, xp DESC, user_id)"
        )
        await db.commit()
        self.voice_worker.start()

    async def cog_unload(self) -> None:
        self.voice_worker.cancel()

    async def _ensure_settings(self, guild_id: int) -> None:
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO leveling_settings (guild_id) VALUES (?)", (guild_id,)
        )
        await self.bot.db.commit()

    async def _settings(self, guild_id: int) -> dict:
        async with self.bot.db.execute(
            "SELECT enabled, xp_min, xp_max, cooldown_seconds, multiplier, "
            "voice_xp, voice_interval_seconds, daily_reward, levelup_card_enabled "
            "FROM leveling_settings WHERE guild_id=?",
            (guild_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return {
                "enabled": 0,
                "xp_min": 15,
                "xp_max": 25,
                "cooldown_seconds": 60,
                "multiplier": 1.0,
                "voice_xp": 10,
                "voice_interval_seconds": 60,
                "daily_reward": 100,
                "levelup_card_enabled": 0,
            }
        return dict(
            zip(
                (
                    "enabled",
                    "xp_min",
                    "xp_max",
                    "cooldown_seconds",
                    "multiplier",
                    "voice_xp",
                    "voice_interval_seconds",
                    "daily_reward",
                    "levelup_card_enabled",
                ),
                row,
            )
        )

    async def _user(self, guild_id: int, user_id: int) -> tuple[int, int, str | None, str | None]:
        async with self.bot.db.execute(
            "SELECT xp, streak, last_claim_date, last_message_xp "
            "FROM leveling_users WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        ) as cursor:
            row = await cursor.fetchone()
        return row if row is not None else (0, 0, None, None)

    async def _add_xp_locked(
        self, guild: discord.Guild, user_id: int, amount: int
    ) -> tuple[int, int]:
        """Add XP while _xp_lock is held; return old and new derived levels."""
        amount = max(0, int(amount))
        old_xp, _, _, _ = await self._user(guild.id, user_id)
        now = _timestamp(_utcnow())
        await self.bot.db.execute(
            "INSERT INTO leveling_users (guild_id, user_id, xp, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
            "xp=MIN(?, leveling_users.xp + excluded.xp), updated_at=excluded.updated_at",
            (guild.id, user_id, amount, now, MAX_XP),
        )
        async with self.bot.db.execute(
            "SELECT xp FROM leveling_users WHERE guild_id=? AND user_id=?",
            (guild.id, user_id),
        ) as cursor:
            row = await cursor.fetchone()
        await self.bot.db.commit()
        new_xp = int(row[0])
        old_level, new_level = level_for_xp(old_xp), level_for_xp(new_xp)
        if new_level > old_level:
            await self._grant_level_rewards(guild, user_id, old_level, new_level)
        return old_level, new_level

    async def _grant_level_rewards(
        self,
        guild: discord.Guild,
        user_id: int,
        old_level: int,
        new_level: int,
        channel: discord.abc.Messageable | None = None,
    ) -> None:
        member = guild.get_member(user_id)
        me = guild.me
        if member is None or me is None:
            return
        async with self.bot.db.execute(
            "SELECT role_id FROM leveling_rewards WHERE guild_id=? AND level>? AND level<=?",
            (guild.id, old_level, new_level),
        ) as cursor:
            rows = await cursor.fetchall()
        for (role_id,) in rows:
            role = guild.get_role(role_id)
            if (
                role is None
                or role.is_default()
                or role.managed
                or role >= me.top_role
                or role in member.roles
            ):
                continue
            try:
                await member.add_roles(role, reason=f"Reached leveling level {new_level}")
            except (discord.Forbidden, discord.HTTPException) as error:
                print(
                    f"[leveling] Could not grant role {role.id} to user {user_id} "
                    f"in guild {guild.id}: {error}"
                )
                continue

        settings = await self._settings(guild.id)
        destination = channel or guild.system_channel
        if not settings["levelup_card_enabled"] or destination is None:
            return
        try:
            xp, _, _, _ = await self._user(guild.id, user_id)
            rank = await self._rank_position(guild.id, user_id, xp)
            voice_seconds = await self._voice_seconds(guild.id, user_id)
            card = await self._rank_card(
                member,
                xp,
                rank,
                voice_seconds=voice_seconds,
            )
            embed = self._rank_embed(guild, member, xp, rank, card.filename)
            embed.title = f"Level Up! {member.display_name} reached Level {new_level}"
            await destination.send(
                content=f"{member.mention} leveled up!",
                embed=embed,
                file=card,
                allowed_mentions=discord.AllowedMentions(users=[member]),
            )
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, discord.HTTPException) as error:
            print(f"[leveling] Could not send level-up card for {user_id} in guild {guild.id}: {error}")

    async def _award_xp(
        self, guild: discord.Guild, user_id: int, amount: int
    ) -> tuple[int, int]:
        async with self._xp_lock:
            return await self._add_xp_locked(guild, user_id, amount)

    async def _is_blacklisted(self, guild_id: int, user_id: int) -> bool:
        async with self.bot.db.execute(
            "SELECT 1 FROM leveling_blacklist WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def _voice_seconds(self, guild_id: int, user_id: int) -> int:
        async with self.bot.db.execute(
            "SELECT voice_seconds FROM leveling_users WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def _record_voice_seconds(
        self, guild: discord.Guild, user_id: int, seconds: int
    ) -> None:
        seconds = max(0, int(seconds))
        if seconds == 0:
            return
        async with self._xp_lock:
            await self.bot.db.execute(
                "INSERT INTO leveling_users (guild_id, user_id, updated_at, voice_seconds) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(guild_id, user_id) DO UPDATE SET "
                "voice_seconds=leveling_users.voice_seconds + excluded.voice_seconds, "
                "updated_at=excluded.updated_at",
                (guild.id, user_id, _timestamp(_utcnow()), seconds),
            )
            await self.bot.db.commit()

    @staticmethod
    def _voice_eligible(
        guild: discord.Guild, member: discord.Member, voice: discord.VoiceState | None
    ) -> bool:
        return bool(
            not member.bot
            and voice is not None
            and voice.channel is not None
            and not voice.self_deaf
            and not voice.deaf
            and not (guild.afk_channel and voice.channel.id == guild.afk_channel.id)
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if (
            message.guild is None
            or message.author.bot
            or not message.content.strip()
        ):
            return
        guild = message.guild
        settings = await self._settings(guild.id)
        if not settings["enabled"]:
            return
        async with self._xp_lock:
            now = _utcnow()
            loop_now = asyncio.get_running_loop().time()
            if loop_now - self._last_event_cleanup >= 3600:
                cutoff = _timestamp(now - datetime.timedelta(days=7))
                await self.bot.db.execute(
                    "DELETE FROM leveling_message_events WHERE created_at='' OR created_at < ?",
                    (cutoff,),
                )
                self._last_event_cleanup = loop_now
            inserted = await self.bot.db.execute(
                "INSERT OR IGNORE INTO leveling_message_events "
                "(guild_id, message_id, created_at) VALUES (?, ?, ?)",
                (guild.id, message.id, _timestamp(now)),
            )
            if inserted.rowcount == 0:
                await self.bot.db.commit()
                return
            if await self._is_blacklisted(guild.id, message.author.id):
                await self.bot.db.commit()
                return
            old_xp, _, _, last_message = await self._user(guild.id, message.author.id)
            last = _parse_timestamp(last_message)
            if last and (now - last).total_seconds() < settings["cooldown_seconds"]:
                await self.bot.db.commit()
                return
            raw_award = random.randint(settings["xp_min"], settings["xp_max"])
            award = max(0, min(MAX_XP, round(raw_award * settings["multiplier"])))
            await self.bot.db.execute(
                "INSERT INTO leveling_users "
                "(guild_id, user_id, xp, last_message_xp, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
                "xp=MIN(?, leveling_users.xp + excluded.xp), "
                "last_message_xp=excluded.last_message_xp, updated_at=excluded.updated_at",
                (guild.id, message.author.id, award, _timestamp(now), _timestamp(now), MAX_XP),
            )
            async with self.bot.db.execute(
                "SELECT xp FROM leveling_users WHERE guild_id=? AND user_id=?",
                (guild.id, message.author.id),
            ) as cursor:
                updated = await cursor.fetchone()
            await self.bot.db.commit()
            before, after = level_for_xp(old_xp), level_for_xp(updated[0])
            if after > before:
                await self._grant_level_rewards(
                    guild,
                    message.author.id,
                    before,
                    after,
                    channel=message.channel,
                )

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        key = (member.guild.id, member.id)
        before_eligible = self._voice_eligible(member.guild, member, before)
        after_eligible = self._voice_eligible(member.guild, member, after)
        if not after_eligible:
            self._voice_seen.pop(key, None)
            activity_started = self._voice_activity_seen.pop(key, None)
            if activity_started is not None:
                elapsed = int(asyncio.get_running_loop().time() - activity_started)
                await self._record_voice_seconds(member.guild, member.id, elapsed)
        elif not before_eligible:
            settings = await self._settings(member.guild.id)
            if settings["enabled"]:
                now = asyncio.get_running_loop().time()
                self._voice_seen[key] = now
                self._voice_activity_seen[key] = now

    @tasks.loop(seconds=60)
    async def voice_worker(self) -> None:
        try:
            await self._process_voice_worker_iteration()
        except aiosqlite.Error:
            log.exception("Leveling voice worker database operation failed; retrying next interval")

    async def _process_voice_worker_iteration(self) -> None:
        now = asyncio.get_running_loop().time()
        eligible: dict[tuple[int, int], tuple[discord.Member, dict]] = {}
        for guild in self.bot.guilds:
            settings = await self._settings(guild.id)
            if not settings["enabled"]:
                continue
            for channel in guild.voice_channels:
                for member in channel.members:
                    voice = member.voice
                    if not self._voice_eligible(guild, member, voice):
                        continue
                    eligible[(guild.id, member.id)] = (member, settings)
        eligible_ids = set(eligible)
        for key in tuple(self._voice_seen):
            if key not in eligible_ids:
                self._voice_seen.pop(key, None)
        for key in tuple(self._voice_activity_seen):
            if key not in eligible_ids:
                started = self._voice_activity_seen.pop(key)
                guild_id, user_id = key
                guild = self.bot.get_guild(guild_id)
                if guild is not None:
                    elapsed = int(now - started)
                    await self._record_voice_seconds(guild, user_id, elapsed)
        for key, (member, settings) in eligible.items():
            activity_started = self._voice_activity_seen.setdefault(key, now)
            elapsed = int(now - activity_started)
            if elapsed > 0:
                await self._record_voice_seconds(member.guild, member.id, elapsed)
                self._voice_activity_seen[key] = activity_started + elapsed
            last_seen = self._voice_seen.setdefault(key, now)
            if now - last_seen < settings["voice_interval_seconds"]:
                continue
            self._voice_seen[key] = now
            if settings["voice_xp"] <= 0 or await self._is_blacklisted(member.guild.id, member.id):
                continue
            await self._award_xp(member.guild, member.id, settings["voice_xp"])

    @voice_worker.before_loop
    async def before_voice_worker(self) -> None:
        await self.bot.wait_until_ready()

    @staticmethod
    async def _defer(ctx: commands.Context, ephemeral: bool = False) -> None:
        if ctx.interaction and not ctx.interaction.response.is_done():
            await ctx.interaction.response.defer(ephemeral=ephemeral)

    @staticmethod
    async def _reply(
        ctx: commands.Context,
        content: str | None = None,
        *,
        embed=None,
        ephemeral=False,
        file: discord.File | None = None,
    ):
        send_kwargs = {"embed": embed}
        if file is not None:
            send_kwargs["file"] = file
        if ctx.interaction:
            if ctx.interaction.response.is_done():
                return await ctx.interaction.followup.send(
                    content, ephemeral=ephemeral, **send_kwargs
                )
            return await ctx.interaction.response.send_message(
                content, ephemeral=ephemeral, **send_kwargs
            )
        return await ctx.send(content, **send_kwargs)

    async def _rank_position(self, guild_id: int, user_id: int, xp: int) -> int:
        async with self.bot.db.execute(
            "SELECT COUNT(*) + 1 FROM leveling_users "
            "WHERE guild_id=? AND (xp>? OR (xp=? AND user_id<?))",
            (guild_id, xp, xp, user_id),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0])

    async def _rank_card(
        self,
        member: discord.Member,
        xp: int,
        rank: int,
        *,
        streak: int | None = None,
        voice_seconds: int = 0,
    ) -> discord.File:
        avatar_bytes = await member.display_avatar.with_format("png").with_size(256).read()
        bot_icon_bytes = None
        if self.bot.user:
            try:
                bot_icon = self.bot.user.display_avatar.with_format("png").with_size(128)
                bot_icon_bytes = await bot_icon.read()
            except (discord.HTTPException, OSError, asyncio.TimeoutError) as error:
                log.warning("Failed to download bot icon for rank card: %s", error)
        card_bytes = await asyncio.to_thread(
            render_rank_card,
            avatar_bytes,
            member.display_name,
            level_for_xp(xp),
            xp,
            rank,
            streak,
            voice_seconds,
            bot_icon_bytes,
        )
        return discord.File(
            io.BytesIO(card_bytes),
            filename=f"falcon-rank-{member.id}.png",
        )

    @staticmethod
    def _rank_embed(
        guild: discord.Guild,
        user: discord.abc.User,
        xp: int,
        rank: int,
        filename: str,
    ) -> discord.Embed:
        level, in_level, needed = xp_progress(xp)
        embed = discord.Embed(
            title=f"{user.display_name}'s Rank",
            color=discord.Color.gold(),
            description=f"**Level {level}** · **Server Rank #{rank}** · {xp:,} total XP",
        )
        embed.add_field(name="Progress", value=f"{in_level:,} / {needed:,} XP", inline=False)
        embed.set_footer(text=guild.name)
        embed.set_image(url=f"attachment://{filename}")
        return embed

    @commands.hybrid_command(name="rank", description="Show a member's server rank")
    @commands.guild_only()
    async def rank(self, ctx: commands.Context, member: discord.Member | None = None):
        await self._defer(ctx)
        member = member or ctx.author
        xp, _, _, _ = await self._user(ctx.guild.id, member.id)
        rank = await self._rank_position(ctx.guild.id, member.id, xp)
        voice_seconds = await self._voice_seconds(ctx.guild.id, member.id)
        filename = f"falcon-rank-{member.id}.png"
        file = await self._rank_card(
            member, xp, rank, voice_seconds=voice_seconds
        )
        await self._reply(
            ctx,
            embed=self._rank_embed(ctx.guild, member, xp, rank, filename),
            file=file,
        )

    @commands.hybrid_command(name="levelprofile", description="Show a member's leveling profile")
    @commands.guild_only()
    async def profile(self, ctx: commands.Context, member: discord.Member | None = None):
        await self._defer(ctx)
        member = member or ctx.author
        xp, streak, last_claim, _ = await self._user(ctx.guild.id, member.id)
        rank = await self._rank_position(ctx.guild.id, member.id, xp)
        voice_seconds = await self._voice_seconds(ctx.guild.id, member.id)
        filename = f"falcon-rank-{member.id}.png"
        file = await self._rank_card(
            member,
            xp,
            rank,
            streak=streak,
            voice_seconds=voice_seconds,
        )
        embed = self._rank_embed(ctx.guild, member, xp, rank, filename)
        embed.title = f"{member.display_name}'s Leveling Profile"
        embed.add_field(name="Current streak", value=f"{streak} day(s)", inline=True)
        embed.add_field(
            name="Voice activity",
            value=format_voice_time(voice_seconds),
            inline=True,
        )
        embed.add_field(name="Daily claim", value=last_claim or "Not claimed yet", inline=True)
        await self._reply(ctx, embed=embed, file=file)

    @commands.hybrid_command(name="xpleaderboard", description="Show this server's XP leaderboard")
    @commands.guild_only()
    async def leaderboard(self, ctx: commands.Context, limit: int = 10):
        await self._defer(ctx)
        if not 1 <= limit <= 25:
            return await self._reply(ctx, "Choose a leaderboard size from 1 to 25.")
        async with self.bot.db.execute(
            "SELECT user_id, xp FROM leveling_users WHERE guild_id=? "
            "ORDER BY xp DESC, user_id ASC LIMIT ?",
            (ctx.guild.id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        lines = []
        for index, (user_id, xp) in enumerate(rows, 1):
            member = ctx.guild.get_member(user_id)
            label = member.display_name if member else f"Member {user_id}"
            lines.append(f"**{index}.** {label} — Level {level_for_xp(xp)} ({xp:,} XP)")
        embed = discord.Embed(
            title=f"{ctx.guild.name} XP Leaderboard",
            description="\n".join(lines) if lines else "No XP has been earned yet.",
            color=discord.Color.gold(),
        )
        await self._reply(ctx, embed=embed)

    @commands.hybrid_command(name="streak", description="Show your or another member's claim streak")
    @commands.guild_only()
    async def streak(self, ctx: commands.Context, member: discord.Member | None = None):
        member = member or ctx.author
        _, days, last_claim, _ = await self._user(ctx.guild.id, member.id)
        claim_date = _utcnow().date()
        today = claim_date.isoformat()
        status = "Claim available today" if last_claim != today else "Already claimed today"
        await self._reply(
            ctx,
            f"**{member.display_name}** has a **{days}-day** streak. {status}.",
        )

    @commands.hybrid_command(name="claimreward", description="Claim your daily XP and extend your streak")
    @commands.guild_only()
    async def claimreward(self, ctx: commands.Context):
        await self._defer(ctx)
        claim_date = _utcnow().date()
        today = claim_date.isoformat()
        async with self._xp_lock:
            settings = await self._settings(ctx.guild.id)
            if not settings["enabled"]:
                return await self._reply(ctx, "Leveling is not enabled in this server.")
            if await self._is_blacklisted(ctx.guild.id, ctx.author.id):
                return await self._reply(ctx, "You are excluded from earning leveling XP in this server.")
            xp, streak, last_claim, _ = await self._user(ctx.guild.id, ctx.author.id)
            if last_claim == today:
                return await self._reply(ctx, "You have already claimed today's reward.")
            yesterday = (claim_date - datetime.timedelta(days=1)).isoformat()
            streak = streak + 1 if last_claim == yesterday else 1
            award = settings["daily_reward"] + min(streak - 1, 30) * 10
            granted = min(award, MAX_XP - xp)
            updated_xp = xp + granted
            now = _timestamp(_utcnow())
            await self.bot.db.execute(
                "INSERT INTO leveling_users "
                "(guild_id, user_id, xp, streak, last_claim_date, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET xp=excluded.xp, "
                "streak=excluded.streak, last_claim_date=excluded.last_claim_date, "
                "updated_at=excluded.updated_at",
                (ctx.guild.id, ctx.author.id, updated_xp, streak, today, now),
            )
            await self.bot.db.commit()
            old_level, new_level = level_for_xp(xp), level_for_xp(updated_xp)
            if new_level > old_level:
                await self._grant_level_rewards(ctx.guild, ctx.author.id, old_level, new_level)
        await self._reply(ctx, f"Daily reward claimed: **{granted:,} XP**. Streak: **{streak} day(s)**.")

    @commands.hybrid_command(name="rewards", description="List this server's level role rewards")
    @commands.guild_only()
    async def rewards(self, ctx: commands.Context):
        async with self.bot.db.execute(
            "SELECT level, role_id FROM leveling_rewards WHERE guild_id=? ORDER BY level",
            (ctx.guild.id,),
        ) as cursor:
            rows = await cursor.fetchall()
        if not rows:
            text = "No level role rewards are configured."
        else:
            text = "\n".join(
                f"Level **{level}** — {ctx.guild.get_role(role_id).mention if ctx.guild.get_role(role_id) else f'role {role_id}'}"
                for level, role_id in rows
            )
        await self._reply(ctx, embed=discord.Embed(title="Level Rewards", description=text, color=discord.Color.gold()))

    @commands.hybrid_command(name="serverstats", description="Show this server's leveling statistics")
    @commands.guild_only()
    async def serverstats(self, ctx: commands.Context):
        await self._defer(ctx)
        settings = await self._settings(ctx.guild.id)
        async with self.bot.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(xp), 0), COALESCE(MAX(xp), 0) "
            "FROM leveling_users WHERE guild_id=?",
            (ctx.guild.id,),
        ) as cursor:
            users, total_xp, top_xp = await cursor.fetchone()
        embed = discord.Embed(title=f"{ctx.guild.name} Leveling Stats", color=discord.Color.gold())
        embed.add_field(name="Status", value="Enabled" if settings["enabled"] else "Disabled")
        embed.add_field(name="Tracked members", value=f"{users:,}")
        embed.add_field(name="Total XP", value=f"{total_xp:,}")
        embed.add_field(name="Highest level", value=str(level_for_xp(top_xp)))
        await self._reply(ctx, embed=embed)

    @commands.hybrid_command(name="leveling", description="Enable or disable leveling and configure a level reward")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def setup(
        self,
        ctx: commands.Context,
        enabled: bool = True,
        reward_level: int | None = None,
        reward_role: discord.Role | None = None,
    ):
        await self._defer(ctx, ephemeral=True)
        await self._ensure_settings(ctx.guild.id)
        if reward_level is not None and reward_level < 1:
            return await self._reply(ctx, "Reward level must be at least 1.", ephemeral=True)
        if reward_role is not None:
            if reward_level is None:
                return await self._reply(ctx, "Provide a reward level when assigning a role.", ephemeral=True)
            me = ctx.guild.me
            if (
                reward_role.is_default()
                or reward_role.managed
                or me is None
                or reward_role >= me.top_role
            ):
                return await self._reply(
                    ctx, "That role is not assignable: it must be below the bot's highest role.", ephemeral=True
                )
        await self.bot.db.execute(
            "UPDATE leveling_settings SET enabled=? WHERE guild_id=?",
            (int(enabled), ctx.guild.id),
        )
        if reward_level is not None:
            if reward_role is None:
                await self.bot.db.execute(
                    "DELETE FROM leveling_rewards WHERE guild_id=? AND level=?",
                    (ctx.guild.id, reward_level),
                )
            else:
                await self.bot.db.execute(
                    "INSERT INTO leveling_rewards (guild_id, level, role_id) VALUES (?, ?, ?) "
                    "ON CONFLICT(guild_id, level) DO UPDATE SET role_id=excluded.role_id",
                    (ctx.guild.id, reward_level, reward_role.id),
                )
        await self.bot.db.commit()
        state = "enabled" if enabled else "disabled"
        detail = ""
        if reward_level is not None:
            detail = (
                f" Role reward for level {reward_level} set to {reward_role.mention}."
                if reward_role
                else f" Role reward for level {reward_level} removed."
            )
        await self._reply(ctx, f"Leveling {state}.{detail}", ephemeral=True)

    @commands.command(name="levelupcard", help="Enable or disable rank cards on level-up")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def levelup_card(self, ctx: commands.Context, action: Literal["enable", "disable"]):
        await self._ensure_settings(ctx.guild.id)
        enabled = action == "enable"
        await self.bot.db.execute(
            "UPDATE leveling_settings SET levelup_card_enabled=? WHERE guild_id=?",
            (int(enabled), ctx.guild.id),
        )
        await self.bot.db.commit()
        await self._reply(
            ctx,
            f"Rank card level-up announcements {action}d.",
            ephemeral=ctx.interaction is not None,
        )

    @commands.hybrid_command(name="multiplier", description="Set this server's XP multiplier")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def multiplier(self, ctx: commands.Context, value: float):
        if not math.isfinite(value) or not 0.1 <= value <= 10:
            return await self._reply(ctx, "Multiplier must be between 0.1 and 10.")
        await self._ensure_settings(ctx.guild.id)
        await self.bot.db.execute(
            "UPDATE leveling_settings SET multiplier=? WHERE guild_id=?", (value, ctx.guild.id)
        )
        await self.bot.db.commit()
        await self._reply(ctx, f"XP multiplier set to **{value:g}×**.")

    @commands.hybrid_command(name="xpblacklist", description="Add or remove a member from XP earning")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def xpblacklist(
        self, ctx: commands.Context, action: str, member: discord.Member | None = None
    ):
        action = action.lower()
        if action == "list":
            async with self.bot.db.execute(
                "SELECT user_id FROM leveling_blacklist WHERE guild_id=? ORDER BY user_id",
                (ctx.guild.id,),
            ) as cursor:
                rows = await cursor.fetchall()
            return await self._reply(
                ctx, "XP blacklist: " + (", ".join(f"<@{row[0]}>" for row in rows) if rows else "empty")
            )
        if action not in {"add", "remove"} or member is None:
            return await self._reply(ctx, "Usage: `xpblacklist add|remove @member` or `xpblacklist list`.")
        if action == "add":
            await self.bot.db.execute(
                "INSERT OR IGNORE INTO leveling_blacklist (guild_id, user_id) VALUES (?, ?)",
                (ctx.guild.id, member.id),
            )
        else:
            await self.bot.db.execute(
                "DELETE FROM leveling_blacklist WHERE guild_id=? AND user_id=?",
                (ctx.guild.id, member.id),
            )
        await self.bot.db.commit()
        await self._reply(ctx, f"{member.mention} {'added to' if action == 'add' else 'removed from'} the XP blacklist.")

    @commands.hybrid_command(name="voicexp", description="Set voice XP per eligible voice interval")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def voicexp(self, ctx: commands.Context, amount: int, interval_minutes: int = 1):
        if not 0 <= amount <= 1000 or not 1 <= interval_minutes <= 60:
            return await self._reply(ctx, "XP must be 0–1000 and interval must be 1–60 minutes.")
        await self._ensure_settings(ctx.guild.id)
        await self.bot.db.execute(
            "UPDATE leveling_settings SET voice_xp=?, voice_interval_seconds=? WHERE guild_id=?",
            (amount, interval_minutes * 60, ctx.guild.id),
        )
        await self.bot.db.commit()
        await self._reply(
            ctx, f"Voice XP set to **{amount} XP** every **{interval_minutes} minute(s)**. Use 0 to disable."
        )

    @commands.hybrid_command(name="xpconfig", description="View leveling settings or set the daily reward")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def xpconfig(self, ctx: commands.Context, daily_reward: int | None = None):
        if daily_reward is not None:
            if not 0 <= daily_reward <= MAX_XP:
                return await self._reply(ctx, f"Daily reward must be between 0 and {MAX_XP:,} XP.")
            await self._ensure_settings(ctx.guild.id)
            await self.bot.db.execute(
                "UPDATE leveling_settings SET daily_reward=? WHERE guild_id=?",
                (daily_reward, ctx.guild.id),
            )
            await self.bot.db.commit()
        settings = await self._settings(ctx.guild.id)
        embed = discord.Embed(title="Leveling Configuration", color=discord.Color.gold())
        embed.description = (
            f"Status: **{'Enabled' if settings['enabled'] else 'Disabled'}**\n"
            f"Message XP: **{settings['xp_min']}–{settings['xp_max']}** every "
            f"**{settings['cooldown_seconds']}s**\n"
            f"Multiplier: **{settings['multiplier']:g}×**\n"
            f"Voice XP: **{settings['voice_xp']}** every **{settings['voice_interval_seconds']}s**\n"
            f"Daily base reward: **{settings['daily_reward']} XP** (+ streak bonus)"
        )
        await self._reply(ctx, embed=embed)

    @commands.hybrid_command(name="xpcooldown", description="Set the message XP cooldown in seconds")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def xpcooldown(self, ctx: commands.Context, seconds: int):
        if not 0 <= seconds <= 3600:
            return await self._reply(ctx, "Cooldown must be between 0 and 3600 seconds.")
        await self._ensure_settings(ctx.guild.id)
        await self.bot.db.execute(
            "UPDATE leveling_settings SET cooldown_seconds=? WHERE guild_id=?",
            (seconds, ctx.guild.id),
        )
        await self.bot.db.commit()
        await self._reply(ctx, f"Message XP cooldown set to **{seconds} second(s)**.")

    @commands.hybrid_command(name="xprange", description="Set the random message XP range")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def xprange(self, ctx: commands.Context, minimum: int, maximum: int):
        if not 0 <= minimum <= maximum <= 1000:
            return await self._reply(ctx, "XP range must satisfy 0 ≤ minimum ≤ maximum ≤ 1000.")
        await self._ensure_settings(ctx.guild.id)
        await self.bot.db.execute(
            "UPDATE leveling_settings SET xp_min=?, xp_max=? WHERE guild_id=?",
            (minimum, maximum, ctx.guild.id),
        )
        await self.bot.db.commit()
        await self._reply(ctx, f"Message XP range set to **{minimum}–{maximum}**.")

    @commands.hybrid_command(name="givexp", description="Give a member XP")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def givexp(self, ctx: commands.Context, member: discord.Member, amount: int):
        if not 1 <= amount <= MAX_XP:
            return await self._reply(ctx, f"XP amount must be between 1 and {MAX_XP:,}.")
        await self._award_xp(ctx.guild, member.id, amount)
        await self._reply(ctx, f"Gave **{amount:,} XP** to {member.mention}.")

    @commands.hybrid_command(name="setlevel", description="Set a member's level (XP progress is reset)")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def setlevel(self, ctx: commands.Context, member: discord.Member, level: int):
        if not 0 <= level <= 100:
            return await self._reply(ctx, "Level must be between 0 and 100.")
        amount = xp_for_level(level)
        async with self._xp_lock:
            now = _timestamp(_utcnow())
            old_xp, _, _, _ = await self._user(ctx.guild.id, member.id)
            await self.bot.db.execute(
                "INSERT INTO leveling_users (guild_id, user_id, xp, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET xp=excluded.xp, updated_at=excluded.updated_at",
                (ctx.guild.id, member.id, amount, now),
            )
            await self.bot.db.commit()
            old_level = level_for_xp(old_xp)
            if level > old_level:
                await self._grant_level_rewards(ctx.guild, member.id, old_level, level)
        await self._reply(ctx, f"Set {member.mention} to **level {level}** ({amount:,} XP).")

    @commands.hybrid_command(name="resetxp", description="Reset a member's XP, streak, and claim history")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def resetxp(self, ctx: commands.Context, member: discord.Member):
        await self.bot.db.execute(
            "DELETE FROM leveling_users WHERE guild_id=? AND user_id=?",
            (ctx.guild.id, member.id),
        )
        await self.bot.db.commit()
        await self._reply(ctx, f"Reset leveling data for {member.mention}.")

    @commands.command(
        name="levelbackup",
        aliases=["backup"],
        help="Send an owner-only JSON export of leveling data by DM.",
    )
    @commands.is_owner()
    async def levelbackup(
        self,
        ctx: commands.Context,
        guild_id: int | None = None,
    ):
        if guild_id is not None and guild_id <= 0:
            return await self._reply(ctx, "Guild ID must be a positive integer.")

        tables = {
            "leveling_settings": (
                ("guild_id", "enabled", "xp_min", "xp_max", "cooldown_seconds",
                 "multiplier", "voice_xp", "voice_interval_seconds",
                 "levelup_card_enabled", "daily_reward"),
                ("guild_id",),
            ),
            "leveling_rewards": (
                ("guild_id", "level", "role_id"),
                ("guild_id", "level"),
            ),
            "leveling_blacklist": (
                ("guild_id", "user_id"),
                ("guild_id", "user_id"),
            ),
            "leveling_users": (
                ("guild_id", "user_id", "xp", "streak", "last_claim_date",
                 "last_message_xp", "updated_at", "voice_seconds"),
                ("guild_id", "user_id"),
            ),
        }
        data = {}
        async with self._xp_lock:
            for table, (columns, sort_columns) in tables.items():
                column_sql = ", ".join(columns)
                query = f"SELECT {column_sql} FROM {table}"
                parameters = ()
                if guild_id is not None:
                    query += " WHERE guild_id=?"
                    parameters = (guild_id,)
                query += f" ORDER BY {', '.join(sort_columns)}"
                async with self.bot.db.execute(query, parameters) as cursor:
                    rows = await cursor.fetchall()
                data[table] = [
                    dict(zip(columns, row, strict=True)) for row in rows
                ]

        backup = {
            "format_version": 1,
            "created_at": _timestamp(_utcnow()),
            "scope": {"guild_id": guild_id},
            "data": data,
        }
        payload = json.dumps(
            backup,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        if len(payload) > MAX_LEVELING_BACKUP_BYTES:
            return await self._reply(
                ctx,
                "The leveling export exceeds 8 MiB. Retry with a specific guild ID "
                "to export a smaller snapshot.",
            )

        scope_name = str(guild_id) if guild_id is not None else "all-guilds"
        filename = f"falcon-leveling-backup-{scope_name}-{_utcnow():%Y%m%dT%H%M%SZ}.json"
        try:
            await ctx.author.send(
                file=discord.File(io.BytesIO(payload), filename=filename)
            )
        except discord.Forbidden:
            return await self._reply(
                ctx,
                "I could not DM the backup. Enable direct messages from this server and try again.",
            )
        except discord.HTTPException as error:
            log.exception("Could not deliver leveling backup to owner %s", ctx.author.id)
            return await self._reply(ctx, f"Discord could not deliver the backup: {error}")
        return await self._reply(
            ctx,
            f"Sent `{filename}` to your DMs.",
        )


async def setup(bot: commands.Bot) -> None:
    """Load as an extension without replacing Falcon's existing profile command."""
    await bot.add_cog(LevelingFeatures(bot))
