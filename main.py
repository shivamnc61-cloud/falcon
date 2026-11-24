import os
import base64
import json
from pathlib import Path
from dotenv import load_dotenv, dotenv_values
import discord
from discord.ext import commands
from discord.ext.commands.view import StringView
import datetime
import asyncio
import logging
import re
import random
import aiosqlite
import io
import aiohttp
from urllib.parse import quote, urlencode, urlsplit
from discord import app_commands
from collections import defaultdict
from typing import Any, get_args
from cluster_runtime import bot_owns_guild, cluster_shard_ids
from database import connect_database
from components_v2 import (
    edit_interaction_v2,
    edit_message_v2,
    make_layout_view,
    send_interaction_v2,
    send_v2,
)

load_dotenv()


class _SingleShardConnectLogFilter(logging.Filter):
    def __init__(self):
        super().__init__()
        self._logged_connection = False

    def filter(self, record):
        if "has connected to Gateway" not in record.getMessage():
            return True
        if self._logged_connection:
            return False
        self._logged_connection = True
        return True


logging.getLogger("discord.gateway").addFilter(_SingleShardConnectLogFilter())


def _env_ids(name: str) -> list[int]:
    values = []
    for raw_value in os.getenv(name, "").split(","):
        raw_value = raw_value.strip()
        if raw_value.isdigit():
            values.append(int(raw_value))
    return values


def _get_effective_discord_token():
    env_token = (os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN") or "").strip()
    env_file = Path(__file__).resolve().parent / ".env"
    file_cfg = dotenv_values(env_file)
    file_token = (file_cfg.get("DISCORD_TOKEN") or file_cfg.get("BOT_TOKEN") or "").strip()

    def looks_valid(token):
        if not token:
            return False
        if token.lower() in {"paste_your_real_token_here", "your_token_here", "placeholder", "changeme"}:
            return False
        parts = token.split(".")
        return len(parts) == 3 and all(part for part in parts) and all(
            ch.isalnum() or ch in "._-" for ch in token
        )

    if looks_valid(env_token):
        return env_token
    if looks_valid(file_token):
        return file_token
    return env_token or file_token


BOT_PREFIX = os.getenv("PREFIX", ",").strip() or ","
BOT_STATUS = os.getenv("STATUS_TEXT", ",help").strip() or ",help"
STREAM_STATUS_URL = os.getenv("STREAM_STATUS_URL", "https://www.twitch.tv/").strip() or "https://www.twitch.tv/"
CLUSTER_COUNT = int(os.getenv("CLUSTER_COUNT", "1"))
CLUSTER_ID = int(os.getenv("CLUSTER_ID", "0"))
SHARD_COUNT = int(
    os.getenv("SHARD_COUNT", str(CLUSTER_COUNT if CLUSTER_COUNT > 1 else 10))
)
CLUSTER_SHARD_IDS = cluster_shard_ids(CLUSTER_ID, CLUSTER_COUNT, SHARD_COUNT)
IDENTIFY_BUCKET_INTERVAL = 5.1
CLUSTER_MIGRATION_LOCK_ID = 0x46414C434F4E
CLUSTER_IDENTIFY_LOCK_ID = 0x46414C434F51
GIVEAWAY_EXPIRY_POLL_INTERVAL = 1.0
BOT_COLOR = 0xFFDD00
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/falcon_data.db").strip() or "data/falcon_data.db"
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
BOT_APPLICATION_ID = os.getenv("BOT_APPLICATION_ID", "").strip()
INVITE_URL = os.getenv("INVITE_URL", "").strip()
INVITE_URL_LIMITED = os.getenv("INVITE_URL_LIMITED", INVITE_URL).strip() or INVITE_URL
INVITE_URL_ADMIN = os.getenv("INVITE_URL_ADMIN", INVITE_URL).strip() or INVITE_URL
PRIMARY_OWNER_IDS = _env_ids("OWNER_IDS")
if not PRIMARY_OWNER_IDS:
    configured_owner = os.getenv("PRIMARY_OWNER_ID", os.getenv("OWNER_ID", "")).strip()
    if configured_owner.isdigit():
        PRIMARY_OWNER_IDS = [int(configured_owner)]
PRIMARY_OWNER_ID = PRIMARY_OWNER_IDS[0] if PRIMARY_OWNER_IDS else 0
SUPPORT_SERVER_URL = os.getenv("SUPPORT_SERVER_URL", os.getenv("SUPPORT_SERVER", "")).strip()


def _bot_admin_invite_url():
    application_id = getattr(getattr(bot, "user", None), "id", None)
    if not application_id:
        application_id = BOT_APPLICATION_ID or getattr(bot, "application_id", None)
    if not application_id:
        return None
    query = urlencode(
        {
            "client_id": str(application_id),
            "permissions": 8,
            "scope": "bot applications.commands",
        },
        quote_via=quote,
    )
    return f"https://discord.com/oauth2/authorize?{query}"


async def _connect_database(database_path, *, project_dir=None):
    return await connect_database(DATABASE_URL, database_path, project_dir=project_dir)


async def _ensure_guild_scoped_table(db, table_name, create_statement):
    async with db.execute(f"PRAGMA table_info({table_name})") as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    if columns and "guild_id" not in columns:
        await db.execute(f"ALTER TABLE {table_name} RENAME TO {table_name}_legacy")
    await db.execute(create_statement)


class PremiumSupportView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        if SUPPORT_SERVER_URL:
            self.add_item(discord.ui.Button(
                label="Join Support Server",
                url=SUPPORT_SERVER_URL,
                style=discord.ButtonStyle.link,
            ))


def premium_required_embed():
    return discord.Embed(
        title="Premium Required",
        description="This server does not have an active Falcon Premium subscription.",
        color=0xFFDD00,
    )


async def is_guild_premium(guild_id):
    if bot.db is None:
        return False
    async with bot.db.execute(
        "SELECT expiry FROM premium_guilds WHERE guild_id=?", (guild_id,)
    ) as cursor:
        row = await cursor.fetchone()
    if not row:
        return False
    if row[0] is None:
        return True
    try:
        expiry = datetime.datetime.fromisoformat(row[0])
    except (TypeError, ValueError):
        return False
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=datetime.timezone.utc)
    return expiry > datetime.datetime.now(datetime.timezone.utc)


PREFIX_ONLY_SLASH_NAMES = {
    "afk", "np", "rps", "tag", "rr_add", "rr_remove", "rr_list", "sync",
    "premium_legacy", "np_legacy",
}
HIDDEN_SLASH_COMMAND_NAMES = {
    "tempvoice", "channel_lock",
    "channel_unlock",
}
GLOBAL_SLASH_COMMAND_LIMIT = 100
PRIORITY_SLASH_GROUP_RESERVE = 6


def _prefix_only_slash_name(name):
    normalized = name.casefold()
    compact = normalized.replace("_", "").replace("-", "")
    return normalized in PREFIX_ONLY_SLASH_NAMES or (len(compact) <= 3 and normalized != "set")


def _remove_short_group_commands(group, prefix=""):
    for child in tuple(group.commands):
        qualified_name = f"{prefix} {child.name}".strip()
        if child.name.casefold() in HIDDEN_SLASH_COMMAND_NAMES:
            group.remove_command(child.name)
            continue
        if isinstance(child, app_commands.Group):
            _remove_short_group_commands(child, qualified_name)
            if not child.commands:
                group.remove_command(child.name)
        elif _prefix_only_slash_name(child.name) and len(
            qualified_name.replace(" ", "").replace("_", "").replace("-", "")
        ) <= 3:
            group.remove_command(child.name)


class FalconCommandTree(app_commands.CommandTree):
    def add_command(self, command, /, *, override=False, guild=None, guilds=discord.utils.MISSING):
        if command.name.casefold() in HIDDEN_SLASH_COMMAND_NAMES:
            return
        if _prefix_only_slash_name(command.name):
            return
        if (
            not isinstance(command, app_commands.Group)
            and len(self.get_commands()) >= GLOBAL_SLASH_COMMAND_LIMIT - PRIORITY_SLASH_GROUP_RESERVE
        ):
            return
        if isinstance(command, app_commands.Group):
            _remove_short_group_commands(command, command.name)
        return super().add_command(
            command,
            override=override,
            guild=guild,
            guilds=guilds,
        )

    async def interaction_check(self, interaction):
        if interaction.guild_id is None:
            return True
        command = interaction.command
        qualified_name = command.qualified_name if command else ""
        if qualified_name == "premium" or qualified_name.startswith("premium "):
            return True
        if await is_guild_premium(interaction.guild_id):
            return True
        await send_interaction_v2(
            interaction,
            embed=premium_required_embed(),
            view=PremiumSupportView(),
            ephemeral=True,
        )
        return False


def _load_emoji_manifest():
    manifest_path = Path(__file__).resolve().parent / "emojis.json"
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


EMOJI_MANIFEST = _load_emoji_manifest()


def _manifest_emoji(name, fallback):
    value = EMOJI_MANIFEST.get(name)
    return value if isinstance(value, str) else fallback


GLOBAL_HELP_EMOJI_KEYS = [
    "fp_premium", "fp_invite", "ms", "vc", "fp_premium", "lb", "tada",
    "timer", "wlcm", "mods", "fp_invite", "polls", "utlity", "support",
]


def get_global_help_emojis():
    return [_manifest_emoji(name, "") for name in GLOBAL_HELP_EMOJI_KEYS]

# Required for tracking invites, reading messages, and force-fetching reactions
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.invites = True
intents.presences = True

class FalconContext(commands.Context):
    async def send(self, content=None, *, embed=None, embeds=None, view=discord.utils.MISSING, ephemeral=False, **kwargs):
        ephemeral = False
        has_legacy_options = kwargs.get("poll") is not None or kwargs.get("stickers") or kwargs.get("tts", False)
        if isinstance(view, discord.ui.LayoutView):
            message = await super().send(view=view, ephemeral=ephemeral, **kwargs)
            view._v2_message = message
            return message
        if not has_legacy_options and (
            content is not None or embed is not None or embeds is not None or view is not discord.utils.MISSING
        ):
            layout = make_layout_view(
                content,
                embed,
                embeds,
                legacy_view=None if view is discord.utils.MISSING else view,
                file=kwargs.get("file"),
                files=kwargs.get("files"),
            )
            message = await super().send(view=layout, ephemeral=ephemeral, **kwargs)
            layout._v2_message = message
            return message
        return await super().send(
            content,
            embed=embed,
            embeds=embeds,
            view=None if view is discord.utils.MISSING else view,
            ephemeral=ephemeral,
            **kwargs,
        )


class FalconBot(commands.AutoShardedBot):
    def __init__(self):
        super().__init__(
            command_prefix=self.get_prefix,
            intents=intents,
            shard_count=SHARD_COUNT,
            shard_ids=CLUSTER_SHARD_IDS if CLUSTER_COUNT > 1 else None,
            help_command=None,
            case_insensitive=True,
            tree_cls=FalconCommandTree,
        )
        self.cluster_id = CLUSTER_ID
        self.cluster_count = CLUSTER_COUNT
        self.inv_cache = {}
        self.db = None
        self.owner_id = PRIMARY_OWNER_ID
        self.owner_ids = set(PRIMARY_OWNER_IDS)
        self.slash_commands_synced = False
        self.timers_restored = False
        self.status_rotation_task = None
        self.post_ready_task = None
        self.identify_max_concurrency = 1
        self._identify_bucket_locks = {}
        self._identify_bucket_last_started = {}
        # Color scheme
        self.color = BOT_COLOR
        self.golden_neon_guilds = set()
        self.golden_neon_guilds_pending = set()
        self.golden_neon_sync_task = None
        self.cluster_metrics_task = None

    def owns_guild(self, guild_id):
        return bot_owns_guild(self, guild_id)

    def add_command(self, command):
        try:
            super().add_command(command)
        except discord.app_commands.errors.CommandLimitReached:
            # The prefix command is registered before its slash copy is added.
            return

    async def get_context(self, origin, /, *, cls=None):
        return await super().get_context(origin, cls=cls or FalconContext)

    async def process_commands(self, message):
        if message.author.bot:
            return
        ctx = await self.get_context(message)
        if ctx.command is not None:
            await self._inject_replied_member(ctx)
        await self.invoke(ctx)

    async def _inject_replied_member(self, ctx):
        command = ctx.command
        if command is None:
            return

        view = StringView(ctx.view.buffer)
        view.index = ctx.view.index
        while isinstance(command, commands.Group):
            view.skip_ws()
            if view.eof:
                break
            command_start = view.index
            subcommand_name = view.get_word()
            subcommand = command.get_command(subcommand_name)
            if subcommand is None:
                view.index = command_start
                break
            command = subcommand

        parameters = list(command.clean_params.values())
        target_parameter = None
        target_position = None
        for parameter in parameters:
            view.skip_ws()
            argument_position = view.index
            try:
                token = view.get_quoted_word()
            except commands.CommandError:
                return

            if _is_user_target_parameter(parameter):
                if token:
                    try:
                        await commands.converter.run_converters(
                            ctx, parameter.converter, token, parameter
                        )
                        continue
                    except commands.BadArgument:
                        pass
                target_parameter = parameter
                target_position = argument_position
                break

            if parameter.kind.name == "KEYWORD_ONLY":
                return
            if token is None:
                return

        if target_parameter is None or target_position is None:
            return

        if _is_user_parameter(target_parameter):
            replied_target = await _resolve_replied_user(ctx)
        else:
            replied_target = await _resolve_replied_member(ctx)
        if replied_target is None:
            return

        buffer = ctx.view.buffer
        mention = f"<@{replied_target.id}>"
        before = buffer[:target_position]
        after = buffer[target_position:]
        separator_before = "" if not before or before[-1].isspace() else " "
        separator_after = "" if not after or after[0].isspace() else " "
        ctx.view.buffer = (
            before + separator_before + mention + separator_after + after
        )
        ctx.view.end = len(ctx.view.buffer)

    async def before_identify_hook(self, shard_id, *, initial=False):
        if shard_id is None:
            return
        if getattr(self, "cluster_count", 1) > 1:
            async with self.db.execute(
                "SELECT pg_advisory_lock(?)", (CLUSTER_IDENTIFY_LOCK_ID,)
            ) as cursor:
                await cursor.fetchone()
            try:
                await asyncio.sleep(IDENTIFY_BUCKET_INTERVAL)
            finally:
                async with self.db.execute(
                    "SELECT pg_advisory_unlock(?)", (CLUSTER_IDENTIFY_LOCK_ID,)
                ) as cursor:
                    await cursor.fetchone()
            return
        bucket = shard_id % self.identify_max_concurrency
        lock = self._identify_bucket_locks.setdefault(bucket, asyncio.Lock())
        async with lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            previous = self._identify_bucket_last_started.get(bucket)
            if previous is not None and not initial:
                wait_for = IDENTIFY_BUCKET_INTERVAL - (now - previous)
                if wait_for > 0:
                    await asyncio.sleep(wait_for)
            self._identify_bucket_last_started[bucket] = loop.time()

    async def launch_shards(self):
        await super().launch_shards()

    async def _configure_identify_concurrency(self):
        if self.cluster_count > 1:
            self.identify_max_concurrency = 1
            print(
                f"[gateway] Starting {self.shard_count} shards with "
                "PostgreSQL-coordinated identify starts."
            )
            return
        try:
            limits = await self.fetch_session_start_limits()
            self.identify_max_concurrency = max(1, int(limits.max_concurrency))
            print(
                f"[gateway] Starting {self.shard_count} shards with "
                f"{self.identify_max_concurrency} identify bucket(s)."
            )
        except Exception:
            logging.getLogger(__name__).warning(
                "Could not read Discord identify concurrency; using conservative one-at-a-time startup.",
                exc_info=True,
            )

    async def close(self):
        background_tasks = [
            task for task in (
                self.status_rotation_task,
                self.post_ready_task,
                self.cluster_metrics_task,
            )
            if task is not None and not task.done()
        ]
        for task in background_tasks:
            task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)
        if self.db is not None:
            await self.db.close()
            self.db = None
        if not hasattr(self, "_AutoShardedClient__queue"):
            return await discord.Client.close(self)
        await super().close()

    async def get_prefix(self, message):
        prefix = BOT_PREFIX
        if self.db:
            if message.guild is not None:
                async with self.db.execute(
                    "SELECT prefix FROM guild_prefixes WHERE guild_id = ?",
                    (message.guild.id,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row and row[0]:
                    prefix = row[0]
            async with self.db.execute(
                "SELECT expiry, permanent FROM noprefix WHERE user_id = ?",
                (message.author.id,),
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    expiry = None
                    if row[0]:
                        try:
                            expiry = datetime.datetime.fromisoformat(row[0])
                        except ValueError:
                            expiry = None
                    if expiry and expiry.tzinfo is None:
                        expiry = expiry.replace(tzinfo=datetime.timezone.utc)
                    if row[1] or (
                        expiry
                        and (
                            expiry.year >= 9999
                            or expiry > datetime.datetime.now(datetime.timezone.utc)
                        )
                    ):
                        return [prefix, BOT_PREFIX, ""]
                    await self.db.execute(
                        "DELETE FROM noprefix WHERE user_id = ?", (message.author.id,)
                    )
                    await self.db.commit()
        return [prefix] if prefix == BOT_PREFIX else [prefix, BOT_PREFIX]

    async def setup_hook(self):
        if self.cluster_count > 1 and not DATABASE_URL:
            raise RuntimeError(
                "Multi-process clustering requires PostgreSQL; set DATABASE_URL "
                "to the shared PostgreSQL database."
            )
        await self._configure_identify_concurrency()
        self.db = await _connect_database(DATABASE_PATH)
        if self.cluster_count > 1:
            async with self.db.execute(
                "SELECT pg_advisory_lock(?)", (CLUSTER_MIGRATION_LOCK_ID,)
            ) as cursor:
                await cursor.fetchone()
        await self.db.execute(
            "CREATE TABLE IF NOT EXISTS cluster_runtime ("
            "cluster_id INTEGER PRIMARY KEY, server_count INTEGER NOT NULL, "
            "member_count INTEGER NOT NULL, updated_at TEXT NOT NULL)"
        )
        await _ensure_guild_scoped_table(self.db, "users", """
            CREATE TABLE IF NOT EXISTS users (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                joins INTEGER DEFAULT 0,
                left INTEGER DEFAULT 0, bonus INTEGER DEFAULT 0,
                total_msgs INTEGER DEFAULT 0, weekly_msgs INTEGER DEFAULT 0, today_msgs INTEGER DEFAULT 0,
                voice_alltime INTEGER DEFAULT 0, voice_weekly INTEGER DEFAULT 0, voice_daily INTEGER DEFAULT 0,
                fake_invites INTEGER NOT NULL DEFAULT 0, rejoins INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )""")
        async with self.db.execute("PRAGMA table_info(users)") as cursor:
            user_columns = {row[1] for row in await cursor.fetchall()}
        for column, declaration in (
            ("voice_alltime", "INTEGER DEFAULT 0"),
            ("voice_weekly", "INTEGER DEFAULT 0"),
            ("voice_daily", "INTEGER DEFAULT 0"),
            ("fake_invites", "INTEGER NOT NULL DEFAULT 0"),
            ("rejoins", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in user_columns:
                await self.db.execute(f"ALTER TABLE users ADD COLUMN {column} {declaration}")

        await self.db.execute("CREATE TABLE IF NOT EXISTS giveaways (msg_id INTEGER PRIMARY KEY, forced_winner INTEGER, prize TEXT, host_id INTEGER, winners INTEGER DEFAULT 1)")
        try:
            await self.db.execute("ALTER TABLE giveaways ADD COLUMN winners INTEGER DEFAULT 1")
            await self.db.commit()
        except Exception:
            pass
        for column, declaration in (
            ("guild_id", "INTEGER"),
            ("channel_id", "INTEGER"),
            ("announcement_msg_id", "INTEGER"),
            ("ends_at", "TEXT"),
            ("active", "INTEGER NOT NULL DEFAULT 1"),
        ):
            try:
                await self.db.execute(f"ALTER TABLE giveaways ADD COLUMN {column} {declaration}")
                await self.db.commit()
            except Exception:
                pass
        await self.db.execute(
            "CREATE TABLE IF NOT EXISTS giveaway_settings ("
            "guild_id INTEGER PRIMARY KEY, winner_role_id INTEGER, bypass_role_id INTEGER, "
            "required_role_id INTEGER, embed_title TEXT, embed_color INTEGER, "
            "embed_image_url TEXT, reaction_emoji TEXT)"
        )
        async with self.db.execute("PRAGMA table_info(giveaway_settings)") as cursor:
            giveaway_setting_columns = {row[1] for row in await cursor.fetchall()}
        for column in ("embed_image_url", "reaction_emoji"):
            if column not in giveaway_setting_columns:
                await self.db.execute(f"ALTER TABLE giveaway_settings ADD COLUMN {column} TEXT")
        await self.db.execute(
            "CREATE TABLE IF NOT EXISTS giveaway_website_settings ("
            "guild_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0, "
            "invite_url TEXT, notes TEXT)"
        )
        await self.db.execute("CREATE TABLE IF NOT EXISTS noprefix (user_id INTEGER PRIMARY KEY, expiry TEXT)")
        await self.db.execute(
            "CREATE TABLE IF NOT EXISTS guild_prefixes ("
            "guild_id INTEGER PRIMARY KEY, prefix TEXT NOT NULL)"
        )
        await _ensure_guild_scoped_table(self.db, "invite_tracker", """
            CREATE TABLE IF NOT EXISTS invite_tracker (
                guild_id INTEGER NOT NULL,
                invitee_id INTEGER NOT NULL,
                inviter_id INTEGER,
                joined_at TEXT,
                PRIMARY KEY (guild_id, invitee_id)
            )""")
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS invite_member_tracker (
                guild_id INTEGER NOT NULL,
                invitee_id INTEGER NOT NULL,
                inviter_id INTEGER,
                joined_at TEXT NOT NULL,
                is_fake INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (guild_id, invitee_id)
            )""")
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS timers (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                label TEXT NOT NULL,
                end_at TEXT,
                remaining_seconds INTEGER NOT NULL DEFAULT 0,
                paused INTEGER NOT NULL DEFAULT 0
            )""")
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                join_channel_id INTEGER,
                leave_channel_id INTEGER,
                join_message TEXT,
                leave_message TEXT,
                vanity_message TEXT,
                alt_threshold INTEGER,
                msg_weekly_reset TEXT,
                msg_daily_reset TEXT,
                voice_daily_reset TEXT,
                voice_weekly_reset TEXT,
                jail_role_id INTEGER,
                jail_channel_id INTEGER,
                join_enabled INTEGER NOT NULL DEFAULT 1,
                leave_enabled INTEGER NOT NULL DEFAULT 1
            )""")
        async with self.db.execute("PRAGMA table_info(guild_settings)") as cursor:
            guild_columns = {row[1] for row in await cursor.fetchall()}
        for column, declaration in (
            ("jail_role_id", "INTEGER"),
            ("jail_channel_id", "INTEGER"),
            ("join_enabled", "INTEGER NOT NULL DEFAULT 1"),
            ("leave_enabled", "INTEGER NOT NULL DEFAULT 1"),
        ):
            if column not in guild_columns:
                await self.db.execute(f"ALTER TABLE guild_settings ADD COLUMN {column} {declaration}")
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS jail_members (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                role_ids TEXT NOT NULL,
                until TEXT,
                PRIMARY KEY (guild_id, user_id)
            )""")
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS invite_roles (
                guild_id INTEGER,
                role_id INTEGER,
                required_invites INTEGER,
                PRIMARY KEY (guild_id, role_id)
            )""")
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS message_roles (
                guild_id INTEGER,
                role_id INTEGER,
                required_msgs INTEGER,
                PRIMARY KEY (guild_id, role_id)
            )""")
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS blacklisted_channels (
                guild_id INTEGER,
                channel_id INTEGER,
                PRIMARY KEY (guild_id, channel_id)
            )""")
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS blacklisted_categories (
                guild_id INTEGER,
                category_id INTEGER,
                PRIMARY KEY (guild_id, category_id)
            )""")
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS greet_channels (
                guild_id INTEGER,
                channel_id INTEGER,
                greet_type TEXT DEFAULT 'embed',
                PRIMARY KEY (guild_id, channel_id)
            )""")
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS autoroles (
                guild_id INTEGER,
                role_id INTEGER,
                role_type TEXT DEFAULT 'all',
                PRIMARY KEY (guild_id, role_id)
            )""")
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS live_leaderboards (
                guild_id INTEGER,
                channel_id INTEGER,
                message_id INTEGER,
                lb_type TEXT,
                PRIMARY KEY (guild_id, lb_type)
            )""")
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS disabled_commands (
                guild_id INTEGER,
                command_name TEXT,
                channel_id INTEGER,
                PRIMARY KEY (guild_id, command_name, channel_id)
            )""")
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS disabled_modules (
                guild_id INTEGER,
                module_name TEXT,
                channel_id INTEGER,
                PRIMARY KEY (guild_id, module_name, channel_id)
            )""")
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS polls (
                msg_id INTEGER PRIMARY KEY,
                question TEXT,
                options TEXT,
                channel_id INTEGER,
                active INTEGER DEFAULT 1
            )""")
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS premium_guilds (
                guild_id INTEGER PRIMARY KEY,
                expiry TEXT
            )""")
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS premium_users (
                user_id INTEGER PRIMARY KEY,
                expiry TEXT
            )""")
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS guild_help_emojis (
                guild_id INTEGER PRIMARY KEY,
                emojis TEXT
            )""")
        await self.db.execute("CREATE TABLE IF NOT EXISTS bot_owners (user_id INTEGER PRIMARY KEY)")
        await self.db.execute("CREATE TABLE IF NOT EXISTS bot_staff (user_id INTEGER PRIMARY KEY)")
        await self.db.execute("CREATE TABLE IF NOT EXISTS user_blacklist (user_id INTEGER PRIMARY KEY, reason TEXT, by_user INTEGER, at TEXT)")
        await self.db.execute("CREATE TABLE IF NOT EXISTS leaderboard_preferences (guild_id INTEGER PRIMARY KEY, use_mentions INTEGER DEFAULT 0)")
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS guild_event_log_settings (
                guild_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                channel_id INTEGER
            )""")
        await self.db.commit()
        from premium_features import PremiumFeatures
        from community_features import CommunityFeatures, register_fun_commands
        from leveling_features import LevelingFeatures
        from zyre_features import ZyreFeatures
        from denycord_interaction_features import DenycordInteractionFeatures
        from denycord_server_automation import DenycordServerAutomation
        from denycord_voice_backup import DenycordVoiceBackup
        from reference_automation import ReferenceAutomation
        from reference_utility_features import ReferenceUtilityFeatures
        await self.add_cog(PremiumFeatures(self))
        await self.add_cog(CommunityFeatures(self))
        await self.add_cog(ZyreFeatures(self))
        await self.add_cog(LevelingFeatures(self))
        await self.add_cog(DenycordInteractionFeatures(self))
        await self.add_cog(DenycordServerAutomation(self))
        await self.add_cog(DenycordVoiceBackup(self))
        await self.add_cog(ReferenceAutomation(self))
        await self.add_cog(ReferenceUtilityFeatures(self))
        legacy_community_commands = (
            "setjoinchannel", "unsetwelcomechannel",
            "setleave", "unsetleavechannel", "setjoinmessage", "unsetjoinmessage",
            "setwelcome", "setwelcomemsg", "setleavemessage", "unsetleavemessage",
            "setvanitymessage", "unsetvanitymessage", "testmsg", "setgreet",
            "disablegreet", "greetchannels", "greetvariables",
        )
        for legacy_command in legacy_community_commands:
            self.remove_command(legacy_command)
            self.tree.remove_command(legacy_command)
        self.tree.remove_command("boost")
        register_fun_commands(self)
        register_prefix_slash_commands(self)
        set_group = self.get_command("set")
        if set_group is not None and self.tree.get_command("set") is None:
            for optional_root in ("roles", "about"):
                if len(self.tree.get_commands()) < 100:
                    break
                self.tree.remove_command(optional_root)
            if len(self.tree.get_commands()) < 100:
                self.tree.add_command(set_group.app_command)
        if self.cluster_count > 1:
            async with self.db.execute(
                "SELECT pg_advisory_unlock(?)", (CLUSTER_MIGRATION_LOCK_ID,)
            ) as cursor:
                await cursor.fetchone()
        if self.cluster_id == 0:
            asyncio.create_task(self.daily_reset_task())
        asyncio.create_task(self.live_lb_update_task())
        asyncio.create_task(self.giveaway_expiry_task())

    async def daily_reset_task(self):
        while True:
            now = datetime.datetime.now()
            tomorrow = datetime.datetime.combine(now.date() + datetime.timedelta(days=1), datetime.time(0, 0))
            await asyncio.sleep((tomorrow - now).total_seconds())
            await self.db.execute("UPDATE users SET today_msgs = 0, voice_daily = 0")
            await self.db.commit()

    async def giveaway_expiry_task(self):
        await self.wait_until_ready()
        while not self.is_closed():
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            async with self.db.execute(
                "SELECT msg_id, channel_id, guild_id FROM giveaways "
                "WHERE active=1 AND ends_at IS NOT NULL AND ends_at<=?",
                (now,),
            ) as cursor:
                expired = await cursor.fetchall()

            semaphore = asyncio.Semaphore(SHARD_COUNT)

            async def resolve_expired(message_id, channel_id):
                if channel_id:
                    try:
                        async with semaphore:
                            await resolve_giveaway(message_id, channel_id)
                    except (discord.HTTPException, aiosqlite.Error) as error:
                        print(f"[giveaway] Failed to resolve giveaway {message_id}: {error}")
            await asyncio.gather(
                *(
                    resolve_expired(message_id, channel_id)
                    for message_id, channel_id, guild_id in expired
                    if (
                        self.owns_guild(guild_id)
                        if guild_id is not None
                        else self.cluster_id == 0
                    )
                )
            )
            await asyncio.sleep(GIVEAWAY_EXPIRY_POLL_INTERVAL)

    async def live_lb_update_task(self):
        while True:
            await asyncio.sleep(30)
            try:
                async with self.db.execute(
                    "SELECT guild_id, channel_id, message_id, lb_type FROM live_leaderboards"
                ) as cursor:
                    rows = await cursor.fetchall()
                last_channel_edit = {}
                loop = asyncio.get_running_loop()
                for guild_id, channel_id, message_id, lb_type in rows:
                    guild = self.get_guild(guild_id)
                    if guild is None:
                        continue
                    channel = guild.get_channel(channel_id)
                    if channel is None:
                        continue
                    try:
                        message = await channel.fetch_message(message_id)
                        embed = await build_lb_embed(guild, lb_type)
                        if not _live_leaderboard_embed_changed(message, embed):
                            continue
                        last_edit = last_channel_edit.get(channel_id)
                        if last_edit is not None:
                            delay = 1.1 - (loop.time() - last_edit)
                            if delay > 0:
                                await asyncio.sleep(delay)
                        await edit_message_v2(message, embed=embed, content=None)
                        last_channel_edit[channel_id] = loop.time()
                    except (discord.HTTPException, aiosqlite.Error):
                        logging.getLogger(__name__).exception(
                            "Failed to update live leaderboard %s in channel %s",
                            message_id,
                            channel_id,
                        )
            except aiosqlite.Error:
                logging.getLogger(__name__).exception("Failed to load live leaderboards")

def _is_user_target_parameter(parameter):
    annotations = (parameter.annotation, *get_args(parameter.annotation))
    return any(annotation in (discord.Member, discord.User) for annotation in annotations)


def _is_user_parameter(parameter):
    annotations = (parameter.annotation, *get_args(parameter.annotation))
    return discord.User in annotations


async def _resolve_replied_user(ctx):
    message = getattr(ctx, "message", None)
    reference = getattr(message, "reference", None)
    if reference is None:
        return None

    resolved = getattr(reference, "resolved", None)
    candidate = (
        resolved
        if isinstance(resolved, (discord.Member, discord.User))
        else getattr(resolved, "author", None)
    )
    if candidate is None:
        candidate = next(
            (
                mention
                for mention in getattr(message, "mentions", ())
                if isinstance(mention, discord.User)
            ),
            None,
        )
    if candidate is None and getattr(reference, "message_id", None):
        try:
            replied_message = await ctx.channel.fetch_message(reference.message_id)
        except (discord.Forbidden, discord.HTTPException):
            return None
        candidate = replied_message.author

    return candidate if isinstance(candidate, (discord.Member, discord.User)) else None


async def _resolve_replied_member(ctx):
    guild = getattr(ctx, "guild", None)
    if guild is None:
        return None
    user = await _resolve_replied_user(ctx)
    if isinstance(user, discord.Member):
        return user if user.guild.id == guild.id else None
    if user is not None:
        member = guild.get_member(user.id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(user.id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
    return None


class UsernameMemberConverter(commands.MemberConverter):
    async def convert(self, ctx, argument):
        query = argument.strip()
        if query.startswith("@") and not query.startswith("<@"):
            query = query[1:]
        if query.isdecimal() or query.startswith("<@") or ctx.guild is None:
            return await super().convert(ctx, query)
        if not query:
            raise commands.MemberNotFound(argument)

        guild = ctx.guild
        lookup_error = None
        candidates = list(guild.members)
        try:
            candidates.extend(await guild.query_members(query, limit=100, cache=True))
        except (discord.Forbidden, discord.HTTPException) as error:
            lookup_error = error

        normalized = query.casefold()
        matches = {}
        for candidate in candidates:
            names = (
                str(candidate),
                candidate.name,
                candidate.display_name,
                getattr(candidate, "global_name", None),
            )
            if any(name and name.casefold() == normalized for name in names):
                matches[candidate.id] = candidate
        if len(matches) == 1:
            return next(iter(matches.values()))
        if len(matches) > 1:
            raise commands.BadArgument(
                f"Multiple members match {argument!r}; use a mention or ID."
            )
        if lookup_error is not None:
            raise lookup_error
        raise commands.MemberNotFound(argument)


class UsernameUserConverter(commands.UserConverter):
    async def convert(self, ctx, argument):
        query = argument.strip()
        if query.startswith("@") and not query.startswith("<@"):
            query = query[1:]
        try:
            return await super().convert(ctx, query)
        except commands.UserNotFound as conversion_error:
            if ctx.guild is not None:
                try:
                    return await UsernameMemberConverter().convert(ctx, query)
                except commands.MemberNotFound:
                    pass
            raise conversion_error


class UsernameRoleConverter(commands.RoleConverter):
    async def convert(self, ctx, argument):
        query = argument.strip()
        if query.startswith("@") and not query.startswith("<@"):
            query = query[1:]
        try:
            return await super().convert(ctx, query)
        except commands.RoleNotFound as conversion_error:
            guild = ctx.guild
            if guild is None or not query:
                raise conversion_error
            matches = [
                role for role in guild.roles
                if role.name.casefold() == query.casefold()
            ]
            if len(matches) == 1:
                return matches[0]
            raise conversion_error


commands.converter.CONVERTER_MAPPING[discord.Member] = UsernameMemberConverter
commands.converter.CONVERTER_MAPPING[discord.User] = UsernameUserConverter
commands.converter.CONVERTER_MAPPING[discord.Role] = UsernameRoleConverter


async def _resolve_prefix_member_argument_with_rest(ctx, argument=None):
    text = (argument or "").strip()
    if text:
        arguments = text.split(maxsplit=1)
        token = arguments[0]
        try:
            member = await UsernameMemberConverter().convert(ctx, token)
        except commands.MemberNotFound:
            return await _resolve_replied_member(ctx), text
        return member, arguments[1] if len(arguments) > 1 else ""
    return await _resolve_replied_member(ctx), ""


async def _resolve_prefix_member_argument(ctx, argument=None):
    member, _remainder = await _resolve_prefix_member_argument_with_rest(ctx, argument)
    return member


bot = FalconBot()
BOT_STARTED_AT = datetime.datetime.now(datetime.timezone.utc)


def register_prefix_slash_commands(command_bot):
    set_group = command_bot.get_command("set")
    if getattr(command_bot, "prefix_slash_groups_registered", False):
        return

    categories = {
        "invite-tools": {
            "invites", "i", "inviter", "invited", "inviteinfo", "addinvites",
            "removeinvites", "clearinvites", "resetmyinvites", "resetinvites",
            "setaltthreshold", "unsetaltthreshold", "setinviterole",
            "unsetinviterole", "viewinviteroles",
        },
        "message-tools": {
            "messages", "m", "addmessages", "removemessages", "blacklistchannel",
            "unblacklistchannel", "blacklistedchannels", "blacklistcategory",
            "unblacklistcategory", "blacklistedcategories", "setmessagerole",
            "unsetmessagerole", "viewmessageroles", "clearmsgs",
            "resetmymessages", "setresettime",
        },
        "voice": {"vc", "addvctime", "reducevctime", "clearvoice", "resetmyvoice", "vcinfo"},
        "leaderboards": {"leaderboard"},
        "roles": {"autorole", "setautorole", "unsetautorole", "viewautoroles"},
        "giveaways": {
            "gcreate", "gstart", "greroll", "gend", "setgiveaway", "unsetgiveaway",
            "viewwinnerrole", "viewbypassrole", "giveawayconfig", "gwstart", "setwin", "finish", "reroll",
        },
        "timers": {"tstart", "tpause", "tresume", "tend"},
        "greetings": {
            "setjoinchannel", "unsetwelcomechannel", "setleave", "unsetleavechannel",
            "setjoinmessage", "unsetjoinmessage", "setleavemessage", "unsetleavemessage",
            "setvanitymessage", "unsetvanitymessage", "variables", "testmsg", "setgreet",
            "disablegreet", "greetchannels", "greetvariables",
        },
        "moderation": {"kick", "ban", "unban", "mute", "unmute", "erase", "disable", "enable"},
        "polls": {"createpoll", "epoll"},
        "music": {"play", "skip", "stop", "queue", "nowplaying"},
        "administration": {
            "editfalconpremium", "togglemention", "premium_legacy", "np_legacy", "bonus",
            "blacklist", "premiumadd", "premiumremove", "configscreening",
        },
        "utility": {
            "ping", "serverinfo", "userinfo", "avatar", "banner", "guildbanner", "roleinfo",
            "support", "mc", "stats", "perms", "accage", "sponsor", "uptime", "botinfo",
            "timedifference", "help", "credits", "c", "emoji", "crypto",
            "lock", "unlock", "hide", "unhide", "setprefix", "deleteprefix",
        },
    }

    groups = {}
    skipped_categories = set()

    def get_group(category):
        group = groups.get(category)
        if group is not None and len(group.commands) < 25:
            return group

        suffix = 1
        while True:
            group_name = category if suffix == 1 else f"{category}-{suffix}"
            existing = command_bot.tree.get_command(group_name)
            if existing is None:
                break
            suffix += 1
        if len(command_bot.tree.get_commands()) >= 100:
            skipped_categories.add(category)
            return None
        group = app_commands.Group(name=group_name, description=f"{category.title()} commands")
        command_bot.tree.add_command(group)
        groups[category] = group
        return group

    def make_callback(prefix_command):
        async def invoke(interaction: discord.Interaction, arguments: str = ""):
            ctx = await FalconContext.from_interaction(interaction)
            ctx.command = prefix_command
            ctx.view = StringView(arguments)
            try:
                await prefix_command.invoke(ctx)
            except commands.CommandError as error:
                await command_bot.on_command_error(ctx, error)

        return invoke

    category_priority = {
        "moderation": 1,
        "utility": 2,
        "roles": 3,
        "invite-tools": 4,
        "message-tools": 5,
        "giveaways": 6,
        "timers": 7,
        "greetings": 8,
        "polls": 9,
        "administration": 10,
        "leaderboards": 11,
        "voice": 12,
        "community": 13,
    }

    def command_category(prefix_command):
        if prefix_command.qualified_name.lower().split()[0] == "leaderboard":
            return "leaderboards"
        return next(
            (category for category, names in categories.items() if prefix_command.name.lower() in names),
            "community",
        )

    prefix_commands = list(command_bot.walk_commands())
    prefix_commands.sort(key=lambda command: (
        category_priority[command_category(command)],
        command.qualified_name,
    ))
    for prefix_command in prefix_commands:
        if isinstance(prefix_command, commands.HybridCommand):
            continue
        if prefix_command.name.casefold() in HIDDEN_SLASH_COMMAND_NAMES:
            continue
        if prefix_command.name.casefold() in {
            "gend",
            "greroll",
            "setwin",
            "finish",
            "reroll",
        }:
            continue

        qualified_parts = prefix_command.qualified_name.lower().split()
        category = next(
            (name for name, command_names in categories.items() if prefix_command.name.lower() in command_names),
            "community",
        )
        if qualified_parts[0] == "leaderboard":
            category = "leaderboards"
        description = str(prefix_command.short_doc or f"Run {prefix_command.qualified_name}")
        description = description.splitlines()[0][:100] or f"Run {prefix_command.name}"
        name = prefix_command.name
        if _prefix_only_slash_name(name):
            continue
        for name in (name,):
            name_parts = [*qualified_parts[:-1], name.lower()]
            command_name = "-".join(name_parts)
            if len(command_name) > 32 or not re.fullmatch(r"[a-z0-9_-]+", command_name):
                continue
            group = get_group(category)
            if group is None:
                continue
            if group.get_command(command_name) is not None:
                continue
            group.add_command(app_commands.Command(
                name=command_name,
                description=description,
                callback=make_callback(prefix_command),
            ))

    if skipped_categories:
        print(
            "[slash commands] Skipped optional prefix-command slash shortcut groups "
            f"for {', '.join(sorted(skipped_categories))}: Discord allows at most 100 global commands."
        )
    command_bot.prefix_slash_groups_registered = True

@bot.event
async def on_command_error(ctx, error):
    error = getattr(error, 'original', error)
    
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        permissions = ", ".join(permission.replace("_", " ") for permission in error.missing_permissions)
        return await ctx.send(f"{DOT} You need the following permission(s): **{permissions}**.")
    if isinstance(error, commands.CheckFailure):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        embed = discord.Embed(color=0xffdd00, title="<:cross:1559374062281826397> Missing Arguments")
        command_name = ctx.command.qualified_name
        embed.description = f"**Command:** `{ctx.prefix}{command_name}`\n\n**Missing:** `{error.param.name}`"
        embed.add_field(name="Usage:", value=f"`{ctx.prefix}{command_name} {ctx.command.signature}`", inline=False)
        embed.add_field(name="Help:", value=f"{ctx.command.help or 'No description available.'}", inline=False)
        return await ctx.send(embed=embed)
    if isinstance(error, commands.BadArgument):
        embed = discord.Embed(color=0xffdd00, title="<:cross:1559374062281826397> Invalid Argument")
        command_name = ctx.command.qualified_name
        embed.description = f"**Command:** `{ctx.prefix}{command_name}`\n\n**Error:** {str(error)}"
        embed.add_field(name="Usage:", value=f"`{ctx.prefix}{command_name} {ctx.command.signature}`", inline=False)
        return await ctx.send(embed=embed)
    print(f"[command-error] {ctx.command.qualified_name if ctx.command else 'unknown'}: {error!r}")
    try:
        await ctx.send(f"{DOT} Something went wrong while running that command.", delete_after=8)
    except discord.HTTPException:
        pass

# ─── Emoji Constants ───────────────────────────────────────────────
GIVEAWAY_EMOJI = _manifest_emoji("Falcon_giveaway", "<a:Falcon_giveaway:1551883585957404823>")
GIVEAWAY_ENDED_EMOJI = "<a:Falcon_giveaway:1542103448986583113>"
TADA_GIF    = _manifest_emoji("tada", "<:tada:1551879034424655942>")
FP_TICK     = _manifest_emoji("fp_tick", "<:fp_tick:1556939072646287360>")
ARROW       = _manifest_emoji("fp_arrow", "<:fp_arrow:1551879040292360225>")
DOT         = _manifest_emoji("dot", "<:dot:1551879036022816780>")
GIFT        = _manifest_emoji("wvay", "<:wvay:1551879038317105263>")
BLACK_TADA  = _manifest_emoji("tada", "<:tada:1551879034424655942>")

# Category emojis
E_NEWS      = _manifest_emoji("news", "<:news:1551879042381250600>")
E_PREMIUM   = _manifest_emoji("fp_premium", "<:fp_premium:1551879044650246164>")
E_INVITE    = _manifest_emoji("fp_invite", "<:fp_invite:1551879046722359417>")
E_MSG       = _manifest_emoji("ms", "<:ms:1551879048890687518>")
E_VOICE     = _manifest_emoji("vc", "<:vc:1551879052481011793>")
E_ROLE      = _manifest_emoji("fp_premium", "<:fp_premium:1551879044650246164>")
E_LB        = _manifest_emoji("lb", "<:lb:1551879054540677184>")
LB_FIRST    = _manifest_emoji("fp_lb1st", "<:fp_lb1st:1551893166108712991>")
LB_SECOND   = _manifest_emoji("fp_lb2nd", "<:fp_lb2nd:1551893169522737196>")
LB_THIRD    = _manifest_emoji("fp_lb3rd", "<:fp_lb3rd:1551893173947863072>")
LB_DOTS     = _manifest_emoji("dots", "<:dots:1551893178397757491>")
E_GW        = _manifest_emoji("tada", "<:tada:1551879034424655942>")
E_TIMER     = _manifest_emoji("timer", "<:timer:1551879056767582328>")
E_GREET     = _manifest_emoji("wlcm", "<:wlcm:1551879058583715943>")
E_MOD       = _manifest_emoji("mods", "<:mods:1551879061171736587>")
E_MANAGE    = _manifest_emoji("fp_invite", "<:fp_invite:1551879046722359417>")
E_POLL      = _manifest_emoji("polls", "<:polls:1551879063248044104>")
E_UTILITY   = _manifest_emoji("utlity", "<:utlity:1551879065755983912>")
E_CONTACT   = _manifest_emoji("support", "<:support:1551879067488231527>")


def _refresh_emoji_constants():
    global GIVEAWAY_EMOJI, TADA_GIF, DOT, FP_TICK, GIFT, BLACK_TADA, ARROW
    global E_NEWS, E_PREMIUM, E_INVITE, E_MSG, E_VOICE, E_ROLE, E_LB, LB_FIRST, LB_SECOND, LB_THIRD, LB_DOTS, E_GW
    global E_TIMER, E_GREET, E_MOD, E_MANAGE, E_POLL, E_UTILITY, E_CONTACT
    global ONLINE, OFFLINE, DND, IDLE
    GIVEAWAY_EMOJI = _manifest_emoji("Falcon_giveaway", "<a:Falcon_giveaway:1551883585957404823>")
    TADA_GIF = _manifest_emoji("tada", "<:tada:1551879034424655942>")
    FP_TICK = _manifest_emoji("fp_tick", "<:fp_tick:1556939072646287360>")
    ARROW = _manifest_emoji("fp_arrow", "<:fp_arrow:1551879040292360225>")
    DOT = _manifest_emoji("dot", "<:dot:1551879036022816780>")
    GIFT = _manifest_emoji("wvay", "<:wvay:1551879038317105263>")
    BLACK_TADA = _manifest_emoji("tada", "<:tada:1551879034424655942>")
    E_NEWS = _manifest_emoji("news", "<:news:1551879042381250600>")
    E_PREMIUM = _manifest_emoji("fp_premium", "<:fp_premium:1551879044650246164>")
    E_INVITE = _manifest_emoji("fp_invite", "<:fp_invite:1551879046722359417>")
    E_MSG = _manifest_emoji("ms", "<:ms:1551879048890687518>")
    E_VOICE = _manifest_emoji("vc", "<:vc:1551879052481011793>")
    E_ROLE = _manifest_emoji("fp_premium", "<:fp_premium:1551879044650246164>")
    E_LB = _manifest_emoji("lb", "<:lb:1551879054540677184>")
    LB_FIRST = _manifest_emoji("fp_lb1st", "<:fp_lb1st:1551893166108712991>")
    LB_SECOND = _manifest_emoji("fp_lb2nd", "<:fp_lb2nd:1551893169522737196>")
    LB_THIRD = _manifest_emoji("fp_lb3rd", "<:fp_lb3rd:1551893173947863072>")
    LB_DOTS = _manifest_emoji("dots", "<:dots:1551893178397757491>")
    E_GW = _manifest_emoji("tada", "<:tada:1551879034424655942>")
    E_TIMER = _manifest_emoji("timer", "<:timer:1551879056767582328>")
    E_GREET = _manifest_emoji("wlcm", "<:wlcm:1551879058583715943>")
    E_MOD = _manifest_emoji("mods", "<:mods:1551879061171736587>")
    E_MANAGE = _manifest_emoji("fp_invite", "<:fp_invite:1551879046722359417>")
    E_POLL = _manifest_emoji("polls", "<:polls:1551879063248044104>")
    E_UTILITY = _manifest_emoji("utlity", "<:utlity:1551879065755983912>")
    E_CONTACT = _manifest_emoji("support", "<:support:1551879067488231527>")
    ONLINE = _manifest_emoji("online", "<:fp_tick:1556939072646287360>")
    OFFLINE = _manifest_emoji("offline", "<:dot:1551879036022816780>")
    DND = _manifest_emoji("dnd", "<:cross:1559374062281826397>")
    IDLE = _manifest_emoji("idle", "<:fp_timerclock:1556945096266616853>")

# Status Emojis
ONLINE  = _manifest_emoji("online", "<:fp_tick:1556939072646287360>")
OFFLINE = _manifest_emoji("offline", "<:dot:1551879036022816780>")
DND     = _manifest_emoji("dnd", "<:cross:1559374062281826397>")
IDLE    = _manifest_emoji("idle", "<:fp_timerclock:1556945096266616853>")

def get_status_emoji(member):
    if member is None: return OFFLINE
    s = member.status
    if s == discord.Status.online: return ONLINE
    elif s == discord.Status.dnd: return DND
    elif s == discord.Status.idle: return IDLE
    else: return OFFLINE

# ─── DB Helpers ────────────────────────────────────────────────────
async def update_user(guild_id, user_id, **kwargs):
    cols = ", ".join([f"{k} = {k} + ?" for k in kwargs.keys()])
    vals = list(kwargs.values())
    await bot.db.execute(
        "INSERT OR IGNORE INTO users (guild_id, user_id) VALUES (?, ?)",
        (guild_id, user_id),
    )
    await bot.db.execute(
        f"UPDATE users SET {cols} WHERE guild_id = ? AND user_id = ?",
        vals + [guild_id, user_id],
    )
    await bot.db.commit()


async def increment_message_counters(guild_id, user_id):
    async with bot.db.execute(
        """
        INSERT INTO users (guild_id, user_id, total_msgs, weekly_msgs, today_msgs)
        VALUES (?, ?, 1, 1, 1)
        ON CONFLICT (guild_id, user_id) DO UPDATE SET
            total_msgs = COALESCE(users.total_msgs, 0) + 1,
            weekly_msgs = COALESCE(users.weekly_msgs, 0) + 1,
            today_msgs = COALESCE(users.today_msgs, 0) + 1
        RETURNING total_msgs
        """,
        (guild_id, user_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise RuntimeError("Incrementing message counters returned no user row.")
    await bot.db.commit()
    return row[0]


async def get_user_data(guild_id, user_id):
    async with bot.db.execute(
        "SELECT joins, left, bonus, total_msgs, weekly_msgs, today_msgs, voice_alltime, voice_weekly, voice_daily "
        "FROM users WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id)
    ) as cursor:
        row = await cursor.fetchone()
        return row if row else (0, 0, 0, 0, 0, 0, 0, 0, 0)


def is_fake_invite(member, alt_threshold_days):
    if member.bot:
        return True
    if not alt_threshold_days or alt_threshold_days < 0:
        return False
    created_at = member.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=datetime.timezone.utc)
    return (
        datetime.datetime.now(datetime.timezone.utc) - created_at.astimezone(datetime.timezone.utc)
        < datetime.timedelta(days=alt_threshold_days)
    )


async def get_invite_extra_counts(guild_id, user_id):
    async with bot.db.execute(
        "SELECT fake_invites, rejoins FROM users WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ) as cursor:
        row = await cursor.fetchone()
    return row or (0, 0)


async def build_lb_embed(guild, lb_type, page=0):
    members_ids = [m.id for m in guild.members]
    if not members_ids:
        embed = discord.Embed(color=0xffdd00, title="Leaderboard")
        embed.description = f"{DOT} No data yet."
        embed.set_footer(text="Page 1/1")
        return embed
    placeholders = ",".join("?" * len(members_ids))
    col_map = {
        "invites": ("joins - left + bonus", "🏆 Invite Leaderboard"),
        "messages": ("total_msgs", "💬 Message Leaderboard"),
        "weeklymessages": ("weekly_msgs", "📅 Weekly Message Leaderboard"),
        "dailymessages": ("today_msgs", "☀️ Daily Message Leaderboard"),
        "voice": ("voice_alltime", "🎙️ Voice Leaderboard"),
        "weeklyvoice": ("voice_weekly", "📅 Weekly Voice Leaderboard"),
        "dailyvoice": ("voice_daily", "☀️ Daily Voice Leaderboard"),
    }
    col, title = col_map.get(lb_type, ("total_msgs", "Leaderboard"))
    if lb_type == "invites":
        async with bot.db.execute(
            f"SELECT user_id, joins, left, bonus FROM users "
            f"WHERE guild_id = ? AND user_id IN ({placeholders}) "
            f"ORDER BY (joins - left + bonus) DESC LIMIT 10 OFFSET ?",
            [guild.id] + members_ids + [page * 10]
        ) as cur:
            rows = await cur.fetchall()
        async with bot.db.execute(
            f"SELECT COUNT(*) FROM users WHERE guild_id = ? AND user_id IN ({placeholders})",
            [guild.id] + members_ids
        ) as cur:
            total_rows = (await cur.fetchone())[0]
    else:
        async with bot.db.execute(
            f"SELECT user_id, {col} FROM users WHERE guild_id = ? AND user_id IN ({placeholders}) "
            f"ORDER BY {col} DESC LIMIT 10 OFFSET ?",
            [guild.id] + members_ids + [page * 10]
        ) as cur:
            rows = await cur.fetchall()
        async with bot.db.execute(
            f"SELECT COUNT(*) FROM users WHERE guild_id = ? AND user_id IN ({placeholders})",
            [guild.id] + members_ids
        ) as cur:
            total_rows = (await cur.fetchone())[0]
    async with bot.db.execute("SELECT use_mentions FROM leaderboard_preferences WHERE guild_id=?", (guild.id,)) as cur:
        preference = await cur.fetchone()
    use_mentions = bool(preference and preference[0])
    embed = discord.Embed(color=0xffdd00, title=title)
    if not rows:
        embed.description = f"{DOT} No data yet."
    else:
        lines = []
        for index, row in enumerate(rows, start=page * 10 + 1):
            uid = row[0]
            m = guild.get_member(uid)
            name = m.mention if use_mentions and m else (m.name if m else f"Unknown ({uid})")
            if lb_type == "invites":
                joins, left, bonus = row[1:]
                total = joins - left + bonus
                rank_prefix = {1: LB_FIRST, 2: LB_SECOND, 3: LB_THIRD}.get(index, f"**{index}** {LB_DOTS}")
                lines.append(
                    f"{rank_prefix} **|** {name} • **{total}** Invites "
                    f"(**{joins}** Joins, **{left}** Leaves, **0** Fakes, **0** Rejoins)"
                )
            else:
                value = row[1]
                if lb_type in {"voice", "weeklyvoice", "dailyvoice"}:
                    value = f"{value // 3600}h {(value % 3600) // 60}m {value % 60}s"
                    label = "Voice"
                else:
                    label = "Messages"
                rank_prefix = {1: LB_FIRST, 2: LB_SECOND, 3: LB_THIRD}.get(index, f"**{index}** {LB_DOTS}")
                lines.append(f"{rank_prefix} **|** {name} • **{value}** {label}")
        embed.description = "\n".join(lines)
    total_pages = max(1, (total_rows + 9) // 10)
    embed.set_footer(text=f"Page {page + 1}/{total_pages}")
    return embed


def _live_leaderboard_embed_changed(message, embed):
    return not message.embeds or message.embeds[0].to_dict() != embed.to_dict()

# ─── Owner / Staff Permission Helpers ──────────────────────────────
async def is_bot_owner(uid) -> bool:
    return uid in PRIMARY_OWNER_IDS

async def is_bot_blacklisted(uid):
    if uid in PRIMARY_OWNER_IDS:
        return False
    async with bot.db.execute("SELECT 1 FROM user_blacklist WHERE user_id = ?", (uid,)) as cur:
        return (await cur.fetchone()) is not None

async def is_user_premium(uid):
    if uid in PRIMARY_OWNER_IDS:
        return True
    try:
        async with bot.db.execute(
            "SELECT expires_at FROM premium_allocations WHERE user_id=? AND status='active'",
            (uid,),
        ) as cur:
            allocations = await cur.fetchall()
        now = datetime.datetime.now(datetime.timezone.utc)
        for (expiry_value,) in allocations:
            if expiry_value is None:
                return True
            expiry = datetime.datetime.fromisoformat(expiry_value)
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=datetime.timezone.utc)
            if expiry > now:
                return True
    except (aiosqlite.OperationalError, TypeError, ValueError):
        pass
    async with bot.db.execute("SELECT expiry FROM premium_users WHERE user_id = ?", (uid,)) as cur:
        row = await cur.fetchone()
    if not row:
        return False
    try:
        expiry = datetime.datetime.fromisoformat(row[0])
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=datetime.timezone.utc)
        return expiry > datetime.datetime.now(datetime.timezone.utc)
    except (TypeError, ValueError):
        return False

async def is_bot_staff(uid):
    if await is_bot_owner(uid):
        return True
    async with bot.db.execute("SELECT 1 FROM bot_staff WHERE user_id = ?", (uid,)) as cur:
        return (await cur.fetchone()) is not None

# ─── Owner-only Auto Emoji Feature ─────────────────────────────────
async def get_guild_dropdown_emojis(guild_id):
    global_emojis = get_global_help_emojis()
    if any(global_emojis):
        return global_emojis
    return None

SECTION_KEYWORDS = [
    # premium
    ["premium", "vip", "crown", "diamond", "gem", "star", "sparkle", "gold", "rich"],
    # invites
    ["invite", "invites", "envelope", "mail", "letter", "join", "member", "add", "plus", "people"],
    # messages
    ["message", "msg", "chat", "talk", "speech", "bubble", "comment", "text", "say", "type"],
    # voice
    ["voice", "vc", "mic", "microphone", "audio", "sound", "speak", "headphone", "music"],
    # autoroles
    ["role", "autorole", "tag", "badge", "label", "rank"],
    # liveleaderboards
    ["leaderboard", "lb", "trophy", "top", "winner", "first", "medal", "rank", "podium"],
    # giveaway
    ["giveaway", "gift", "gw", "present", "tada", "party", "celebrate", "confetti"],
    # timer
    ["timer", "time", "clock", "alarm", "hourglass", "stopwatch", "watch"],
    # greet
    ["greet", "welcome", "wave", "hello", "hi", "hey", "handshake"],
    # moderation
    ["mod", "ban", "hammer", "shield", "gavel", "kick", "lock", "staff", "police", "guard", "admin"],
    # managecommands
    ["gear", "setting", "settings", "manage", "config", "tool", "wrench", "cog", "system"],
    # poll
    ["poll", "vote", "chart", "bar", "stats", "graph", "ballot", "survey"],
    # utility
    ["util", "utility", "tool", "wrench", "ping", "info", "search", "lens", "magnify"],
    # credits
    ["contact", "support", "credit", "credits", "link", "owner", "heart", "team", "crew", "thanks", "love"],
]

def _smart_pick_emojis(guild_emojis):
    chosen = []
    used_ids = set()
    available = list(guild_emojis)
    for keywords in SECTION_KEYWORDS:
        match = None
        for kw in keywords:
            for e in available:
                if e.id in used_ids:
                    continue
                if kw in e.name.lower():
                    match = e
                    break
            if match:
                break
        if match:
            used_ids.add(match.id)
            chosen.append(str(match))
        else:
            chosen.append("")  # fallback to default later
    # Fill any empty slots with leftover unused emojis (in order)
    leftovers = [e for e in available if e.id not in used_ids]
    li = 0
    for i in range(len(chosen)):
        if not chosen[i] and li < len(leftovers):
            chosen[i] = str(leftovers[li])
            used_ids.add(leftovers[li].id)
            li += 1
    return chosen

async def store_guild_dropdown_emojis(guild):
    pool = [e for e in guild.emojis if not e.animated]
    if not pool:
        pool = list(guild.emojis)
    if not pool:
        return False
    chosen = _smart_pick_emojis(pool)
    if not any(chosen):
        return False
    await bot.db.execute(
        "INSERT OR REPLACE INTO guild_help_emojis (guild_id, emojis) VALUES (?, ?)",
        (guild.id, "|".join(chosen))
    )
    await bot.db.commit()
    return True


def _emoji_folder():
    folder = Path(os.getenv("EMOJI_FOLDER", "emojis"))
    return folder if folder.is_absolute() else Path(__file__).resolve().parent / folder


def _coerce_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _decode_application_id(token):
    if not token:
        return None
    try:
        header = token.split(".", 1)[0]
        padded = header + "=" * (-len(header) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")
        match = re.search(r"(\d{17,20})", decoded)
        if match:
            return match.group(1)
    except Exception:
        pass
    return None


async def sync_application_emojis():
    enabled = _coerce_bool(os.getenv("AUTO_UPLOAD_EMOJIS", "false"), False)
    if not enabled:
        print("[emoji] AUTO_UPLOAD_EMOJIS disabled for application emoji sync")
        return False

    token = os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN")
    if not token:
        print("[emoji] DISCORD_TOKEN/BOT_TOKEN is not set")
        return False

    app_id = str(getattr(bot, "application_id", None) or _decode_application_id(token) or "")
    if not app_id:
        print("[emoji] Could not decode Discord application ID from token")
        return False

    folder = _emoji_folder()
    manifest_path = Path(__file__).resolve().parent / "emojis.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[emoji] Could not read {manifest_path.name}: {exc}")
        return False

    allowed = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    sources = []
    if folder.exists() and folder.is_dir():
        sources.extend(
            (path.stem.strip().lower().replace(" ", "_"), path, None, path.suffix.lower())
            for path in sorted(folder.iterdir())
            if path.is_file() and path.suffix.lower() in allowed and path.stem.lower() in manifest
        )
    if not sources:
        for emoji_name, emoji_value in manifest.items():
            if not isinstance(emoji_value, str):
                continue
            match = EMOJI_RE.fullmatch(emoji_value)
            if not match:
                continue
            suffix = ".gif" if match.group(1) else ".png"
            sources.append((emoji_name, None, f"https://cdn.discordapp.com/emojis/{match.group(3)}.{suffix[1:]}?size=512&quality=lossless", suffix))

    if not sources:
        print("[emoji] No supported image files or manifest emoji sources found")
        return False

    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
    }
    endpoint = f"https://discord.com/api/v10/applications/{app_id}/emojis"

    async with aiohttp.ClientSession() as session:
        async with session.get(endpoint, headers=headers) as resp:
            if resp.status != 200:
                text = await resp.text()
                print(f"[emoji] Failed to fetch application emojis: {resp.status} {text[:300]}")
                return False
            payload = await resp.json()
            if isinstance(payload, dict):
                app_emojis = payload.get("items", [])
            else:
                app_emojis = payload

        existing_by_name = {
            emoji["name"]: emoji
            for emoji in app_emojis
            if isinstance(emoji, dict) and emoji.get("name") and emoji.get("id")
        }

        uploaded = 0
        skipped = 0
        failed = 0
        manifest_changed = False

        for emoji_name, file_path, source_url, suffix in sources:
            if not emoji_name:
                continue
            if emoji_name in existing_by_name:
                existing = existing_by_name[emoji_name]
                animated = bool(existing.get("animated"))
                manifest_value = f"<a:{emoji_name}:{existing['id']}>" if animated else f"<:{emoji_name}:{existing['id']}>"
                if manifest.get(emoji_name) != manifest_value:
                    manifest[emoji_name] = manifest_value
                    manifest_changed = True
                skipped += 1
                print(f"[emoji] Skipped existing app emoji: {emoji_name}")
                continue

            mime_type = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".webp": "image/webp",
                ".gif": "image/gif",
            }.get(suffix, "image/png")
            try:
                if file_path is not None:
                    image_bytes = file_path.read_bytes()
                else:
                    async with session.get(source_url) as source_response:
                        if source_response.status != 200:
                            print(f"[emoji] Could not download {emoji_name}: HTTP {source_response.status}")
                            failed += 1
                            continue
                        image_bytes = await source_response.read()
                image_b64 = base64.b64encode(image_bytes).decode("ascii")
                image_data = f"data:{mime_type};base64,{image_b64}"
            except Exception as exc:
                source_name = file_path.name if file_path is not None else (source_url or emoji_name)
                print(f"[emoji] Could not read {source_name}: {exc}")
                failed += 1
                continue

            try:
                async with session.post(endpoint, headers=headers, json={"name": emoji_name, "image": image_data}) as resp:
                    if resp.status in {200, 201}:
                        created = await resp.json()
                        manifest[emoji_name] = (
                            f"<a:{emoji_name}:{created['id']}>"
                            if suffix == ".gif"
                            else f"<:{emoji_name}:{created['id']}>"
                        )
                        uploaded += 1
                        manifest_changed = True
                        print(f"[emoji] Uploaded app emoji: {emoji_name}")
                    else:
                        body = await resp.text()
                        print(f"[emoji] Upload failed for {emoji_name}: {resp.status} {body[:300]}")
                        failed += 1
            except Exception as exc:
                print(f"[emoji] Upload request error for {emoji_name}: {exc}")
                failed += 1

    if manifest_changed:
        try:
            manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            EMOJI_MANIFEST.clear()
            EMOJI_MANIFEST.update(manifest)
            _refresh_emoji_constants()
            print(f"[emoji] Updated {manifest_path.name} with new application emoji IDs")
        except Exception as exc:
            print(f"[emoji] Could not update {manifest_path.name}: {exc}")

    if uploaded == 0 and skipped == 0 and failed == 0:
        print(f"[emoji] No emoji changes were needed in {folder}")
    elif uploaded == 0 and failed == 0:
        print(f"[emoji] All app emojis already existed; skipped={skipped}")
    else:
        print(f"[emoji] Sync complete: uploaded={uploaded}, skipped={skipped}, failed={failed}")

    return uploaded > 0 or skipped > 0

async def upload_custom_emojis_for_guild(guild):
    enabled = os.getenv("AUTO_UPLOAD_EMOJIS", "false").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        print(f"[emoji] AUTO_UPLOAD_EMOJIS disabled for guild {guild.id}")
        return False

    if guild.me is None or not guild.me.guild_permissions.manage_emojis_and_stickers:
        print(f"[emoji] Missing MANAGE_EMOJIS_AND_STICKERS permission in guild {guild.id}")
        return False

    folder = _emoji_folder()
    allowed = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    sources = []
    if folder.exists() and folder.is_dir():
        sources.extend(
            (file_path.stem.strip().lower().replace(" ", "_"), file_path, None)
            for file_path in sorted(folder.iterdir())
            if file_path.is_file() and file_path.suffix.lower() in allowed
        )

    if not sources:
        manifest_path = Path(__file__).resolve().parent / "emojis.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for emoji_name, emoji_value in manifest.items():
                match = EMOJI_RE.fullmatch(str(emoji_value))
                if not match:
                    continue
                animated = bool(match.group(1))
                extension = "gif" if animated else "png"
                sources.append((emoji_name, None, f"https://cdn.discordapp.com/emojis/{match.group(3)}.{extension}?size=512&quality=lossless"))
        except Exception as exc:
            print(f"[emoji] Could not read {manifest_path.name}: {exc}")

    if not sources:
        print(f"[emoji] No emoji assets found in {folder} or emojis.json")
        return False

    uploaded = 0
    async with aiohttp.ClientSession() as session:
        for emoji_name, file_path, source_url in sources:
            if not emoji_name or any(existing.name == emoji_name for existing in guild.emojis):
                continue

            try:
                if file_path is not None:
                    image_bytes = file_path.read_bytes()
                else:
                    async with session.get(source_url) as response:
                        if response.status != 200:
                            print(f"[emoji] Download failed for {emoji_name}: HTTP {response.status}")
                            continue
                        image_bytes = await response.read()
                await guild.create_custom_emoji(name=emoji_name, image=image_bytes)
                uploaded += 1
                print(f"[emoji] Uploaded: {emoji_name}")
            except Exception as exc:
                source_name = file_path.name if file_path is not None else source_url
                print(f"[emoji] Upload failed for {source_name}: {exc}")

    if uploaded == 0:
        print(f"[emoji] No new emojis uploaded to guild {guild.id}")
    return uploaded > 0

# ─── Events ────────────────────────────────────────────────────────
async def apply_golden_neon_name_style(guild):
    if guild.id in bot.golden_neon_guilds or guild.id in bot.golden_neon_guilds_pending:
        return False
    bot_member = guild.me
    if bot_member is None or not bot_member.guild_permissions.change_nickname:
        print(f"Cannot apply the golden neon name style in guild {guild.id}: Change Nickname permission is missing.")
        return False

    bot.golden_neon_guilds_pending.add(guild.id)
    try:
        await bot.http.request(
            discord.http.Route(
                "PATCH",
                "/guilds/{guild_id}/members/@me",
                guild_id=guild.id,
            ),
            json={
                "display_name_font_id": 3,
                "display_name_effect_id": 3,
                "display_name_colors": [0xFFD700],
            },
        )
    except discord.HTTPException as error:
        print(f"Could not apply the golden neon name style in guild {guild.id}: {error}")
    else:
        bot.golden_neon_guilds.add(guild.id)
        print(f"Applied golden neon name style in guild {guild.id}.")
    finally:
        bot.golden_neon_guilds_pending.discard(guild.id)
    return True


async def apply_golden_neon_to_existing_guilds():
    if not bot.guilds:
        return
    semaphore = asyncio.Semaphore(8)

    async def apply_for_guild(guild):
        async with semaphore:
            await apply_golden_neon_name_style(guild)

    await asyncio.gather(*(apply_for_guild(guild) for guild in bot.guilds))


sniped_messages: dict[int, dict[str, Any]] = {}


async def _log_guild_event(guild, title, description):
    if guild is None:
        return
    async with bot.db.execute(
        "SELECT enabled, channel_id FROM guild_event_log_settings WHERE guild_id=?",
        (guild.id,),
    ) as cursor:
        row = await cursor.fetchone()
    if not row or not row[0] or not row[1]:
        return
    channel = guild.get_channel(row[1])
    if not isinstance(channel, discord.TextChannel):
        return
    embed = discord.Embed(
        title=title,
        description=description[:3900],
        color=0xffdd00,
        timestamp=discord.utils.utcnow(),
    )
    try:
        await send_v2(
            channel,
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.Forbidden as error:
        print(f"[event-log] Missing permission in guild {guild.id}, channel {channel.id}: {error}")
    except discord.HTTPException as error:
        print(f"[event-log] Could not send event log in guild {guild.id}: {error}")


async def _event_log_audit_actor(
    guild, action, target_id, *, expected_change=None
):
    async with bot.db.execute(
        "SELECT enabled, channel_id FROM guild_event_log_settings WHERE guild_id=?",
        (guild.id,),
    ) as cursor:
        settings = await cursor.fetchone()
    if not settings or not settings[0] or not settings[1]:
        return None
    me = guild.me
    if me is None or not me.guild_permissions.view_audit_log:
        return None
    expected_key, expected_value = expected_change or (None, None)
    missing = object()
    try:
        async for entry in guild.audit_logs(action=action, limit=5):
            if getattr(entry.target, "id", None) != target_id:
                continue
            if expected_key is not None and getattr(
                getattr(entry.changes, "after", None), expected_key, missing
            ) != expected_value:
                continue
            age = (discord.utils.utcnow() - entry.created_at).total_seconds()
            if 0 <= age <= 15:
                return entry
    except discord.Forbidden as error:
        print(f"[event-log] Missing View Audit Log permission in guild {guild.id}: {error}")
    except discord.HTTPException as error:
        print(f"[event-log] Could not read audit log in guild {guild.id}: {error}")
    return None


@bot.listen("on_member_join")
async def event_log_member_join(member):
    created_days = (discord.utils.utcnow() - member.created_at).days
    await _log_guild_event(
        member.guild,
        "Member Joined",
        f"**Member:** {member.mention} (`{member.id}`)\n"
        f"**Account created:** <t:{int(member.created_at.timestamp())}:R>\n"
        f"**Account age:** {created_days} day(s)\n"
        f"**Member count:** {member.guild.member_count or 'Unknown'}",
    )


@bot.listen("on_member_remove")
async def event_log_member_remove(member):
    audit_entry = await _event_log_audit_actor(
        member.guild, discord.AuditLogAction.kick, member.id
    )
    if audit_entry:
        executor = audit_entry.user.mention if audit_entry.user else "Unknown"
        reason = audit_entry.reason or "No reason provided"
        return await _log_guild_event(
            member.guild,
            "Member Kicked",
            f"**Member:** {member} (`{member.id}`)\n"
            f"**Moderator:** {executor}\n"
            f"**Reason:** {reason[:700]}",
        )
    await _log_guild_event(
        member.guild,
        "Member Left",
        f"**Member:** {member.mention} (`{member.id}`)\n"
        f"**Joined server:** "
        f"{discord.utils.format_dt(member.joined_at, style='R') if member.joined_at else 'Unknown'}",
    )


@bot.listen("on_member_ban")
async def event_log_member_ban(guild, user):
    audit_entry = await _event_log_audit_actor(
        guild, discord.AuditLogAction.ban, user.id
    )
    executor = (
        audit_entry.user.mention
        if audit_entry and audit_entry.user
        else "Unknown"
    )
    reason = audit_entry.reason if audit_entry else None
    await _log_guild_event(
        guild,
        "Member Banned",
        f"**Member:** {user} (`{user.id}`)\n"
        f"**Moderator:** {executor}\n"
        f"**Reason:** {(reason or 'No reason provided')[:700]}",
    )


@bot.listen("on_member_unban")
async def event_log_member_unban(guild, user):
    audit_entry = await _event_log_audit_actor(
        guild, discord.AuditLogAction.unban, user.id
    )
    executor = (
        audit_entry.user.mention
        if audit_entry and audit_entry.user
        else "Unknown"
    )
    reason = audit_entry.reason if audit_entry else None
    await _log_guild_event(
        guild,
        "Member Unbanned",
        f"**Member:** {user} (`{user.id}`)\n"
        f"**Moderator:** {executor}\n"
        f"**Reason:** {(reason or 'No reason provided')[:700]}",
    )


@bot.listen("on_member_update")
async def event_log_member_timeout(before, after):
    if before.timed_out_until == after.timed_out_until:
        return
    timed_out_until = after.timed_out_until
    if timed_out_until is None:
        title = "Member Timeout Removed"
        status = "Timeout removed"
    else:
        title = "Member Timed Out"
        status = f"Timed out until {discord.utils.format_dt(timed_out_until, style='F')}"
    audit_entry = await _event_log_audit_actor(
        after.guild,
        discord.AuditLogAction.member_update,
        after.id,
        expected_change=("timed_out_until", timed_out_until),
    )
    executor = (
        audit_entry.user.mention
        if audit_entry and audit_entry.user
        else "Unknown"
    )
    reason = audit_entry.reason if audit_entry else None
    await _log_guild_event(
        after.guild,
        title,
        f"**Member:** {after.mention} (`{after.id}`)\n"
        f"**Change:** {status}\n"
        f"**Moderator:** {executor}\n"
        f"**Reason:** {(reason or 'No reason provided')[:700]}",
    )


@bot.listen("on_voice_state_update")
async def event_log_voice_state(member, before, after):
    if member.bot:
        return
    if before.channel == after.channel and before.mute == after.mute and before.deaf == after.deaf:
        return
    if before.channel is None and after.channel is not None:
        action = f"joined {after.channel.mention}"
    elif before.channel is not None and after.channel is None:
        action = f"left {before.channel.mention}"
    elif before.channel != after.channel:
        action = f"moved from {before.channel.mention} to {after.channel.mention}"
    else:
        changes = []
        if before.mute != after.mute:
            changes.append(f"server mute {'enabled' if after.mute else 'disabled'}")
        if before.deaf != after.deaf:
            changes.append(f"server deaf {'enabled' if after.deaf else 'disabled'}")
        action = ", ".join(changes)
    await _log_guild_event(
        member.guild,
        "Voice State Changed",
        f"**Member:** {member.mention} (`{member.id}`)\n**Change:** {action}",
    )


@bot.listen("on_guild_channel_create")
async def event_log_channel_create(channel):
    await _log_guild_event(
        channel.guild,
        "Channel Created",
        f"**Channel:** {channel.mention if hasattr(channel, 'mention') else channel.name}\n"
        f"**Type:** {channel.type}",
    )


@bot.listen("on_guild_channel_delete")
async def event_log_channel_delete(channel):
    await _log_guild_event(
        channel.guild,
        "Channel Deleted",
        f"**Channel:** #{channel.name} (`{channel.id}`)\n**Type:** {channel.type}",
    )


@bot.listen("on_guild_channel_update")
async def event_log_channel_update(before, after):
    changes = []
    for attribute, label in (
        ("name", "name"),
        ("topic", "topic"),
        ("category_id", "category"),
        ("slowmode_delay", "slowmode"),
        ("nsfw", "NSFW"),
        ("bitrate", "bitrate"),
        ("user_limit", "user limit"),
    ):
        old_value = getattr(before, attribute, None)
        new_value = getattr(after, attribute, None)
        if old_value != new_value:
            changes.append(f"**{label}:** `{old_value}` → `{new_value}`")
    if before.overwrites != after.overwrites:
        changes.append("**Permission overwrites:** changed")
    if not changes:
        return
    await _log_guild_event(
        after.guild,
        "Channel Updated",
        f"**Channel:** {after.mention}\n" + "\n".join(changes),
    )


@bot.listen("on_guild_role_create")
async def event_log_role_create(role):
    await _log_guild_event(
        role.guild,
        "Role Created",
        f"**Role:** {role.mention} (`{role.id}`)",
    )


@bot.listen("on_guild_role_delete")
async def event_log_role_delete(role):
    await _log_guild_event(
        role.guild,
        "Role Deleted",
        f"**Role:** {role.name} (`{role.id}`)",
    )


@bot.listen("on_guild_role_update")
async def event_log_role_update(before, after):
    changes = []
    for attribute, label in (
        ("name", "name"),
        ("colour", "colour"),
        ("hoist", "hoisted"),
        ("mentionable", "mentionable"),
        ("permissions", "permissions"),
        ("position", "position"),
    ):
        old_value = getattr(before, attribute, None)
        new_value = getattr(after, attribute, None)
        if old_value != new_value:
            changes.append(f"**{label}:** `{old_value}` → `{new_value}`")
    if changes:
        await _log_guild_event(
            after.guild,
            "Role Updated",
            f"**Role:** {after.mention}\n" + "\n".join(changes),
        )


def _cache_deleted_message(message):
    if message.guild is None or message.author.bot:
        return
    embed_text = "\n".join(
        part
        for item in message.embeds
        for part in (item.title, item.description)
        if part
    )
    sniped_messages[message.channel.id] = {
        "author": message.author.display_name,
        "avatar_url": message.author.display_avatar.url,
        "content": message.content or embed_text,
        "attachments": [(attachment.filename, attachment.url) for attachment in message.attachments],
        "created_at": message.created_at,
        "channel": message.channel.name,
    }


@bot.event
async def on_message_delete(message):
    _cache_deleted_message(message)
    await _log_guild_event(
        message.guild,
        "Message Deleted",
        f"**Author:** {message.author.mention} (`{message.author.id}`)\n"
        f"**Channel:** {message.channel.mention}\n"
        f"**Content:** {(message.content or '[no text content]')[:900]}",
    )


@bot.event
async def on_bulk_message_delete(messages):
    for message in sorted(messages, key=lambda deleted: deleted.created_at):
        _cache_deleted_message(message)
    if messages:
        channel = messages[0].channel
        await _log_guild_event(
            channel.guild,
            "Bulk Messages Deleted",
            f"**Channel:** {channel.mention}\n"
            f"**Messages removed:** {len(messages)}",
        )


def _build_presence_activities(member_count, server_count, shard_count, owner_name):
    return (
        discord.Streaming(
            name=f"{member_count:,} users | {server_count:,} servers",
            url=STREAM_STATUS_URL,
        ),
        discord.Streaming(
            name=f"/help | {shard_count} shards",
            url=STREAM_STATUS_URL,
        ),
        discord.Streaming(
            name=f"Owner: {owner_name}"[:128],
            url=STREAM_STATUS_URL,
        ),
    )


async def _cluster_totals():
    if getattr(bot, "cluster_count", 1) <= 1:
        return (
            len(bot.guilds),
            sum(
                guild.member_count or 0
                for guild in bot.guilds
            ),
        )

    cutoff = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(seconds=60)
    ).isoformat()
    async with bot.db.execute(
        "SELECT COALESCE(SUM(server_count), 0), COALESCE(SUM(member_count), 0) "
        "FROM cluster_runtime WHERE updated_at>=?",
        (cutoff,),
    ) as cursor:
        row = await cursor.fetchone()
    return int(row[0]), int(row[1])


async def _publish_cluster_metrics():
    while not bot.is_closed():
        try:
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            await bot.db.execute(
                "INSERT INTO cluster_runtime "
                "(cluster_id, server_count, member_count, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(cluster_id) DO UPDATE SET "
                "server_count=excluded.server_count, "
                "member_count=excluded.member_count, updated_at=excluded.updated_at",
                (
                    bot.cluster_id,
                    len(bot.guilds),
                    sum(guild.member_count or 0 for guild in bot.guilds),
                    now,
                ),
            )
            await bot.db.commit()
        except aiosqlite.Error:
            logging.getLogger(__name__).exception(
                "Could not publish cluster %s metrics", bot.cluster_id
            )
        await asyncio.sleep(10)


async def _resolve_presence_owner_name():
    owner_ids = PRIMARY_OWNER_IDS or ([PRIMARY_OWNER_ID] if PRIMARY_OWNER_ID else [])
    names = []
    for owner_id in owner_ids[:2]:
        owner = bot.get_user(owner_id)
        if owner is None:
            try:
                owner = await bot.fetch_user(owner_id)
            except discord.HTTPException:
                continue
        names.append(getattr(owner, "global_name", None) or owner.name)
    return ", ".join(names) if names else "Not configured"


async def _rotate_bot_presence():
    owner_name = await _resolve_presence_owner_name()
    activity_index = 0
    while not bot.is_closed():
        if bot.is_ready():
            server_count, member_count = await _cluster_totals()
            shard_count = bot.shard_count or SHARD_COUNT
            activities = _build_presence_activities(
                member_count,
                server_count,
                shard_count,
                owner_name,
            )
            try:
                await bot.change_presence(
                    activity=activities[activity_index],
                    status=discord.Status.online,
                )
                activity_index = (activity_index + 1) % len(activities)
            except discord.HTTPException:
                logging.getLogger(__name__).warning(
                    "Could not update rotating bot presence", exc_info=True
                )
        await asyncio.sleep(25)


async def _warm_guild_runtime_state(guild, semaphore):
    async with semaphore:
        try:
            invites = await guild.invites()
            bot.inv_cache[guild.id] = {invite.code: invite.uses for invite in invites}
        except (discord.Forbidden, discord.HTTPException):
            pass


async def _post_ready_initialization():
    async def sync_commands():
        if getattr(bot, "cluster_id", 0) != 0:
            return
        if bot.slash_commands_synced:
            return
        try:
            synced = await bot.tree.sync()
            bot.slash_commands_synced = True
            print(f"Synced {len(synced)} slash commands.")
        except discord.HTTPException as error:
            print(f"Slash command sync failed: {error}")

    async def sync_emojis():
        if getattr(bot, "cluster_id", 0) != 0:
            return
        try:
            await sync_application_emojis()
        except Exception as exc:
            print(f"Application emoji sync failed: {exc}")

    async def warm_guilds():
        if not bot.guilds:
            return
        semaphore = asyncio.Semaphore(5)
        await asyncio.gather(
            *(_warm_guild_runtime_state(guild, semaphore) for guild in bot.guilds)
        )

    await asyncio.gather(
        sync_commands(),
        sync_emojis(),
        warm_guilds(),
    )


@bot.event
async def on_ready():
    if not getattr(bot, "timers_restored", False) and getattr(bot, "db", None) is not None:
        await restore_active_timers()
        bot.timers_restored = True
    if (
        getattr(bot, "cluster_count", 1) > 1
        and (bot.cluster_metrics_task is None or bot.cluster_metrics_task.done())
    ):
        bot.cluster_metrics_task = asyncio.create_task(_publish_cluster_metrics())
    if (
        getattr(bot, "cluster_id", 0) == 0
        and (bot.status_rotation_task is None or bot.status_rotation_task.done())
    ):
        bot.status_rotation_task = asyncio.create_task(_rotate_bot_presence())
    if bot.post_ready_task is None or bot.post_ready_task.done():
        bot.post_ready_task = asyncio.create_task(_post_ready_initialization())
    if bot.golden_neon_sync_task is None or bot.golden_neon_sync_task.done():
        bot.golden_neon_sync_task = asyncio.create_task(apply_golden_neon_to_existing_guilds())
    print("Falcon™ Premium is live and tracking.")

@bot.event
async def on_guild_join(guild):
    inviter = None
    if bot.user is not None:
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            async for entry in guild.audit_logs(
                limit=10, action=discord.AuditLogAction.bot_add
            ):
                target = entry.target
                if (
                    target is not None
                    and target.id == bot.user.id
                    and entry.user is not None
                    and now - entry.created_at <= datetime.timedelta(minutes=5)
                ):
                    inviter = entry.user
                    break
        except discord.HTTPException as error:
            logging.getLogger(__name__).warning(
                "Could not identify inviter for guild %s: %s", guild.id, error
            )

    premium_account_id = inviter.id if inviter is not None else guild.owner_id
    if not await is_user_premium(premium_account_id):
        support_button_available = bool(SUPPORT_SERVER_URL)
        notice = discord.Embed(
            title="Premium Required",
            description=(
                "An active Falcon Premium account is required to add this bot. "
                + (
                    "Use the button below to join the support server and get help."
                    if support_button_available
                    else "The support invite is currently unavailable."
                )
            ),
            color=0xFFDD00,
        )

        bot_member = guild.me
        if bot_member is not None:
            channels = [guild.system_channel, *guild.text_channels]
            seen_channels = set()
            for channel in channels:
                if channel is None:
                    continue
                channel_id = getattr(channel, "id", id(channel))
                if channel_id in seen_channels:
                    continue
                seen_channels.add(channel_id)
                if not channel.permissions_for(bot_member).send_messages:
                    continue
                try:
                    await channel.send(
                        embed=notice,
                        view=PremiumSupportView() if support_button_available else None,
                    )
                except discord.HTTPException as error:
                    logging.getLogger(__name__).warning(
                        "Could not notify non-premium guild %s: %s", guild.id, error
                    )
                    continue
                break

        recipient = inviter or guild.owner
        if recipient is None:
            try:
                recipient = await bot.fetch_user(guild.owner_id)
            except discord.HTTPException as error:
                logging.getLogger(__name__).warning(
                    "Could not fetch owner for non-premium guild %s: %s", guild.id, error
                )
        if recipient is not None:
            try:
                await recipient.send(
                    embed=notice,
                    view=PremiumSupportView() if support_button_available else None,
                )
            except discord.HTTPException as error:
                logging.getLogger(__name__).warning(
                    "Could not DM inviter or owner of non-premium guild %s: %s",
                    guild.id,
                    error,
                )

        await guild.leave()
        return

    try:
        invs = await guild.invites()
        bot.inv_cache[guild.id] = {i.code: i.uses for i in invs}
    except: pass
    await apply_golden_neon_name_style(guild)

@bot.event
async def on_message(message):
    if message.author.bot: return

    zyre_features = bot.get_cog("ZyreFeatures")
    if zyre_features and await zyre_features.process_message(message):
        return
    
    if bot.user.mentioned_in(message) and message.content.startswith(bot.user.mention):
        prefixes = await bot.get_prefix(message)
        current_prefix = next(
            (prefix for prefix in prefixes if prefix and prefix != ""),
            BOT_PREFIX,
        )
        return await send_v2(
            message.channel,
            f"My prefix here is `{current_prefix}`.",
            reference=message,
            mention_author=False,
        )
    if await is_bot_blacklisted(message.author.id):
        return
    async with bot.db.execute("SELECT channel_id FROM blacklisted_channels WHERE guild_id = ? AND channel_id = ?",
                              (message.guild.id if message.guild else 0, message.channel.id)) as cur:
        if await cur.fetchone():
            return await bot.process_commands(message)
    if message.guild and message.channel.category_id:
        async with bot.db.execute("SELECT category_id FROM blacklisted_categories WHERE guild_id = ? AND category_id = ?",
                                  (message.guild.id, message.channel.category_id)) as cur:
            if await cur.fetchone():
                return await bot.process_commands(message)
    guild_id = message.guild.id if message.guild else 0
    total_msgs = await increment_message_counters(guild_id, message.author.id)
    if message.guild:
        async with bot.db.execute("SELECT role_id, required_msgs FROM message_roles WHERE guild_id = ?",
                                  (message.guild.id,)) as cur:
            rows = await cur.fetchall()
        for role_id, req in rows:
            if total_msgs >= req:
                role = message.guild.get_role(role_id)
                if role and role not in message.author.roles:
                    try: await message.author.add_roles(role)
                    except: pass
    await bot.process_commands(message)


@bot.listen("on_message_edit")
async def event_log_message_edit(before, after):
    if after.guild is None or after.author.bot or before.content == after.content:
        return
    await _log_guild_event(
        after.guild,
        "Message Edited",
        f"**Author:** {after.author.mention} (`{after.author.id}`)\n"
        f"**Channel:** {after.channel.mention}\n"
        f"**Before:** {(before.content or '[empty]')[:700]}\n"
        f"**After:** {(after.content or '[empty]')[:700]}\n"
        f"[Jump to message]({after.jump_url})",
    )

async def replace_message_variables(text, member, inviter, guild, channel=None):
    from dateutil.relativedelta import relativedelta
    from community_features import render_variables

    text = render_variables(
        text,
        member,
        guild,
        channel,
        bot.user,
        {
            "user": member.mention,
            "username": member.display_name,
            "inviter": inviter.mention if inviter else "Unknown",
            "inviter.name": inviter.display_name if inviter else "Unknown",
            "inviter.id": inviter.id if inviter else "",
        },
    )
        
    text = text.replace("{member_count}", str(guild.member_count))
        
    ordinal = {1: "st", 2: "nd", 3: "rd"}.get(guild.member_count % 10 if guild.member_count % 100 not in [11, 12, 13] else 0, "th")
    text = text.replace("{ordinal_member_count}", f"{guild.member_count}{ordinal}")
        
    text = text.replace("{member_name}", member.name)
    text = text.replace("{member}", f"{member.name}#{member.discriminator}")
    text = text.replace("{member_mention}", member.mention)
    text = text.replace("{member_avatar}", member.display_avatar.url)
    text = text.replace("{member_banner}", member.banner.url if member.banner else "None")
    text = text.replace("{member_created_at}", member.created_at.strftime("%Y-%m-%d %H:%M:%S"))
        
    now = datetime.datetime.now(datetime.timezone.utc)
    created_ago = relativedelta(now, member.created_at.replace(tzinfo=datetime.timezone.utc))
    ago_text = f"{created_ago.years}y {created_ago.months}m {created_ago.days}d ago" if created_ago.years else f"{created_ago.months}m {created_ago.days}d ago"
    text = text.replace("{member_created_ago}", ago_text)
        
    text = text.replace("{join_time}", member.joined_at.strftime("%Y-%m-%d %H:%M:%S") if member.joined_at else "Unknown")
        
    text = text.replace("{guild_name}", guild.name)
    text = text.replace("{guild_icon}", guild.icon.url if guild.icon else "None")
    text = text.replace("{guild_banner}", guild.banner.url if guild.banner else "None")
        
    if inviter:
        text = text.replace("{inviter_name}", inviter.name)
        text = text.replace("{inviter_mention}", inviter.mention)
        text = text.replace("{inviter_avatar}", inviter.display_avatar.url)
        text = text.replace("{inviter_banner}", inviter.banner.url if inviter.banner else "None")
        text = text.replace("{inviter_created_at}", inviter.created_at.strftime("%Y-%m-%d %H:%M:%S"))
            
        inviter_created_ago = relativedelta(now, inviter.created_at.replace(tzinfo=datetime.timezone.utc))
        inviter_ago_text = f"{inviter_created_ago.years}y {inviter_created_ago.months}m {inviter_created_ago.days}d ago" if inviter_created_ago.years else f"{inviter_created_ago.months}m {inviter_created_ago.days}d ago"
        text = text.replace("{inviter_created_ago}", inviter_ago_text)
            
        inviter_in_guild = guild.get_member(inviter.id)
        if inviter_in_guild and inviter_in_guild.joined_at:
            text = text.replace("{inviter_joined_at}", inviter_in_guild.joined_at.strftime("%Y-%m-%d %H:%M:%S"))
        else:
            text = text.replace("{inviter_joined_at}", "Unknown")
            
        joins, left, bonus, _, _, _, _, _, _ = await get_user_data(
            guild.id, inviter.id
        )
        total_invites = joins - left + bonus
        text = text.replace("{invites}", str(total_invites))
        text = text.replace("{inviter_reg_invites}", str(joins))
        fake_invites, rejoins = await get_invite_extra_counts(guild.id, inviter.id)
        text = text.replace("{fake_invites}", str(fake_invites))
        text = text.replace("{rejoins}", str(rejoins))
        text = text.replace("{left_invites}", str(left))
    return text
    
@bot.event
async def on_member_join(member):
    async with bot.db.execute(
        "SELECT role_id, role_type FROM autoroles WHERE guild_id=?",
        (member.guild.id,),
    ) as cur:
        ar_rows = await cur.fetchall()
    for role_id, role_type in ar_rows:
        if role_type == "bots" and not member.bot:
            continue
        if role_type == "humans" and member.bot:
            continue
        role = member.guild.get_role(role_id)
        if role:
            try:
                await member.add_roles(role)
            except (discord.Forbidden, discord.HTTPException):
                logging.getLogger(__name__).warning(
                    "Could not apply join role %s to member %s in guild %s",
                    role_id,
                    member.id,
                    member.guild.id,
                    exc_info=True,
                )

    invite_cache_initialized = member.guild.id in bot.inv_cache
    old_invites = bot.inv_cache.get(member.guild.id, {})
    inviter = None
    try:
        invites_after = await member.guild.invites()
    except (discord.Forbidden, discord.HTTPException):
        logging.getLogger(__name__).warning(
            "Could not inspect invites after member %s joined guild %s",
            member.id,
            member.guild.id,
            exc_info=True,
        )
    else:
        if invite_cache_initialized:
            for invite in invites_after:
                if invite.uses > old_invites.get(invite.code, 0):
                    inviter = invite.inviter
                    break
        bot.inv_cache[member.guild.id] = {
            invite.code: invite.uses for invite in invites_after
        }

    if inviter is not None:
        async with bot.db.execute(
            "SELECT is_fake, active FROM invite_member_tracker "
            "WHERE guild_id=? AND invitee_id=?",
            (member.guild.id, member.id),
        ) as cursor:
            previous = await cursor.fetchone()
        is_rejoin = previous is not None and not previous[1]
        was_fake = bool(previous and previous[0])
        async with bot.db.execute(
            "SELECT alt_threshold FROM guild_settings WHERE guild_id=?",
            (member.guild.id,),
        ) as cursor:
            threshold_row = await cursor.fetchone()
        threshold = threshold_row[0] if threshold_row else None
        fake = is_fake_invite(member, threshold)

        updates = {"rejoins": int(is_rejoin)}
        if fake:
            if not was_fake:
                updates["fake_invites"] = 1
        else:
            updates["joins"] = 1
        await update_user(member.guild.id, inviter.id, **updates)

        joined_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        await bot.db.execute(
            "INSERT INTO invite_member_tracker "
            "(guild_id, invitee_id, inviter_id, joined_at, is_fake, active) "
            "VALUES (?, ?, ?, ?, ?, 1) "
            "ON CONFLICT(guild_id, invitee_id) DO UPDATE SET "
            "inviter_id=excluded.inviter_id, joined_at=excluded.joined_at, "
            "is_fake=excluded.is_fake, active=1",
            (member.guild.id, member.id, inviter.id, joined_at, int(fake)),
        )
        await bot.db.execute(
            "INSERT INTO invite_tracker (guild_id, invitee_id, inviter_id, joined_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(guild_id, invitee_id) DO UPDATE SET "
            "inviter_id=excluded.inviter_id, joined_at=excluded.joined_at",
            (member.guild.id, member.id, inviter.id, joined_at),
        )
        await bot.db.commit()

        if not fake:
            joins, left, bonus, _, _, _, _, _, _ = await get_user_data(
                member.guild.id, inviter.id
            )
            net = joins - left + bonus
            async with bot.db.execute(
                "SELECT role_id, required_invites FROM invite_roles WHERE guild_id = ?",
                (member.guild.id,),
            ) as cur:
                role_rows = await cur.fetchall()
            inviter_member = member.guild.get_member(inviter.id)
            if inviter_member:
                for role_id, required_invites in role_rows:
                    role = member.guild.get_role(role_id)
                    if net >= required_invites and role and role not in inviter_member.roles:
                        try:
                            await inviter_member.add_roles(role)
                        except (discord.Forbidden, discord.HTTPException):
                            logging.getLogger(__name__).warning(
                                "Could not apply invite milestone role %s to member %s",
                                role_id,
                                inviter.id,
                                exc_info=True,
                            )


@bot.event
async def on_member_remove(member):
    async with bot.db.execute(
        "SELECT inviter_id, is_fake, active FROM invite_member_tracker "
        "WHERE guild_id=? AND invitee_id=?",
        (member.guild.id, member.id),
    ) as cursor:
        tracked = await cursor.fetchone()
    if tracked and tracked[2]:
        inviter_id, is_fake, _active = tracked
        if inviter_id is not None and not is_fake:
            await update_user(member.guild.id, inviter_id, left=1)
        await bot.db.execute(
            "UPDATE invite_member_tracker SET active=0 WHERE guild_id=? AND invitee_id=?",
            (member.guild.id, member.id),
        )
        await bot.db.commit()

# Voice state tracking
voice_join_times: dict[tuple[int, int], datetime.datetime] = {}

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot: return
    uid = member.id
    member_key = (member.guild.id, uid)
    if before.channel is None and after.channel is not None:
        voice_join_times[member_key] = datetime.datetime.now()
    elif before.channel is not None and after.channel is None:
        if member_key in voice_join_times:
            duration = int(
                (datetime.datetime.now() - voice_join_times.pop(member_key)).total_seconds()
            )
            await update_user(
                member.guild.id,
                uid,
                voice_alltime=duration,
                voice_weekly=duration,
                voice_daily=duration,
            )

# ─── INVITE COMMANDS ──────────────────────────────────────────────

@bot.command(name="invites")
async def invites_cmd(ctx, user: discord.Member = None):
    user = user or ctx.author
    joins, left, bonus, _, _, _, _, _, _ = await get_user_data(
        ctx.guild.id if ctx.guild else 0, user.id
    )
    fake_invites, rejoins = await get_invite_extra_counts(
        ctx.guild.id if ctx.guild else 0, user.id
    )
    total = joins - left + bonus
    embed = discord.Embed(color=0xffdd00)
    embed.set_author(name=f"{user.name}'s Invites Stats", icon_url=user.display_avatar.url)
    embed.description = f"{user.name} currently has **{total}** invites."
    embed.add_field(name="Total:", value=f"{ARROW} ・ **Joins : {joins}**\n{ARROW} ・ **Left : {left}**\n{ARROW} ・ **Bonus : {bonus}**", inline=False)
    embed.add_field(
        name="Other:",
        value=(
            f"{ARROW} ・ **Fake : {fake_invites}**\n"
            f"{ARROW} ・ **Rejoins : {rejoins}**"
        ),
        inline=False,
    )
    embed.set_footer(text=f"Requested by {ctx.author.name} • Today at {datetime.datetime.now().strftime('%I:%M %p')}", icon_url=bot.user.display_avatar.url)
    await ctx.send(embed=embed)

@bot.command(name="i")
async def i_cmd(ctx, user: discord.Member = None):
    await invites_cmd(ctx, user)

@bot.command(name="inviter")
async def inviter_cmd(ctx, user: discord.Member = None):
    user = user or ctx.author
    async with bot.db.execute(
        "SELECT inviter_id, joined_at FROM invite_tracker WHERE guild_id = ? AND invitee_id = ?",
        (ctx.guild.id if ctx.guild else 0, user.id),
    ) as cursor:
        row = await cursor.fetchone()
    embed = discord.Embed(color=0xffdd00)
    embed.set_author(name="Inviter information")
    if row:
        inviter_id, joined_at_str = row
        inviter_mention = f"<@!{inviter_id}>"
        try:
            joined_dt = datetime.datetime.fromisoformat(joined_at_str)
            delta = datetime.datetime.now() - joined_dt
            days = delta.days
            joined_str = "Today" if days == 0 else ("1 day ago" if days == 1 else f"{days} days ago")
        except:
            joined_str = "Unknown"
        embed.description = (f"**! {user.name}** was invited by {inviter_mention}\nJoined : {joined_str}")
    else:
        embed.description = f"**! {user.name}** — No inviter data found.\nThey may have joined before tracking started."
    embed.set_footer(text=f"Today at {datetime.datetime.now().strftime('%I:%M %p')}", icon_url=bot.user.display_avatar.url)
    await ctx.send(embed=embed)

@bot.command(name="invited")
async def invited_cmd(ctx, user: discord.Member = None):
    user = user or ctx.author
    async with bot.db.execute(
        "SELECT invitee_id, joined_at FROM invite_tracker WHERE guild_id = ? AND inviter_id = ?",
        (ctx.guild.id if ctx.guild else 0, user.id),
    ) as cursor:
        rows = await cursor.fetchall()
    if not rows:
        embed = discord.Embed(color=0xffdd00)
        embed.set_author(name=f"Invited list of ! {user.name}")
        embed.description = "This user has not invited anyone yet."
        embed.set_footer(text="Page 1/1", icon_url=bot.user.display_avatar.url)
        return await ctx.send(embed=embed)
    entries = []
    for idx, (invitee_id, _) in enumerate(rows, start=1):
        member = ctx.guild.get_member(invitee_id)
        name = member.name if member else f"Unknown ({invitee_id})"
        status_emoji = get_status_emoji(member)
        entries.append((idx, name, status_emoji))
    page_size = 10
    pages = [entries[i:i+page_size] for i in range(0, len(entries), page_size)]
    total_pages = len(pages)
    current_page = [0]

    def build_embed(page_idx):
        embed = discord.Embed(color=0xffdd00)
        embed.set_author(name=f"Invited list of ! {user.name}", icon_url=user.display_avatar.url)
        lines = [f"**#{idx}** •! **{name}** • {status}" for idx, name, status in pages[page_idx]]
        embed.description = "\n".join(lines)
        embed.set_footer(text=f"Page {page_idx + 1}/{total_pages}", icon_url=bot.user.display_avatar.url)
        return embed

    class PaginationView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)
            self.update_buttons()
        def update_buttons(self):
            self.first_btn.disabled = current_page[0] == 0
            self.prev_btn.disabled = current_page[0] == 0
            self.next_btn.disabled = current_page[0] == total_pages - 1
            self.last_btn.disabled = current_page[0] == total_pages - 1
        @discord.ui.button(emoji=_manifest_emoji("lb_back", "<:lb_back:1556944301823369216>"), style=discord.ButtonStyle.secondary)
        async def first_btn(self, interaction, button):
            if interaction.user != ctx.author: return await send_interaction_v2(interaction, "Not your command!", ephemeral=True)
            current_page[0] = 0; self.update_buttons()
            await edit_interaction_v2(interaction, embed=build_embed(current_page[0]), view=self)
        @discord.ui.button(emoji=_manifest_emoji("lb_prev", "<:lb_prev:1556944305237663784>"), style=discord.ButtonStyle.secondary)
        async def prev_btn(self, interaction, button):
            if interaction.user != ctx.author: return await send_interaction_v2(interaction, "Not your command!", ephemeral=True)
            current_page[0] = max(0, current_page[0] - 1); self.update_buttons()
            await edit_interaction_v2(interaction, embed=build_embed(current_page[0]), view=self)
        @discord.ui.button(emoji=_manifest_emoji("lb_stop", "<:lb_stop:1556944308530319380>"), style=discord.ButtonStyle.secondary)
        async def stop_btn(self, interaction, button):
            if interaction.user != ctx.author: return await send_interaction_v2(interaction, "Not your command!", ephemeral=True)
            for child in self.children: child.disabled = True
            await edit_interaction_v2(interaction, view=self); self.stop()
        @discord.ui.button(emoji=_manifest_emoji("lb_next", "<:lb_next:1556944315526283305>"), style=discord.ButtonStyle.secondary)
        async def next_btn(self, interaction, button):
            if interaction.user != ctx.author: return await send_interaction_v2(interaction, "Not your command!", ephemeral=True)
            current_page[0] = min(total_pages - 1, current_page[0] + 1); self.update_buttons()
            await edit_interaction_v2(interaction, embed=build_embed(current_page[0]), view=self)
        @discord.ui.button(emoji=_manifest_emoji("lb_forward", "<:lb_forward:1556944320215388252>"), style=discord.ButtonStyle.secondary)
        async def last_btn(self, interaction, button):
            if interaction.user != ctx.author: return await send_interaction_v2(interaction, "Not your command!", ephemeral=True)
            current_page[0] = total_pages - 1; self.update_buttons()
            await edit_interaction_v2(interaction, embed=build_embed(current_page[0]), view=self)

    view = PaginationView() if total_pages > 1 else discord.ui.View()
    await ctx.send(embed=build_embed(0), view=view)

@bot.command(name="inviteinfo", aliases=["ii"])
async def inviteinfo(ctx, user: discord.Member = None):
    user = user or ctx.author
    try: guild_invites = await ctx.guild.invites()
    except: return await ctx.send("<:cross:1559374062281826397> I don't have permission to view invites.")
    user_invites = [inv for inv in guild_invites if inv.inviter and inv.inviter.id == user.id]
    embed = discord.Embed(color=0xffdd00)
    embed.set_author(name=f"Invite codes for {user.name}", icon_url=user.display_avatar.url)
    if not user_invites:
        embed.description = f"{DOT} {user.mention} has no active invite codes."
    else:
        lines = []
        for inv in user_invites:
            exp = f"<t:{int(inv.expires_at.timestamp())}:R>" if inv.expires_at else "Never"
            lines.append(f"{DOT} `{inv.code}` — **{inv.uses}** uses — Expires: {exp}")
        embed.description = "\n".join(lines)
    embed.set_footer(text=f"Today at {datetime.datetime.now().strftime('%I:%M %p')}", icon_url=bot.user.display_avatar.url)
    await ctx.send(embed=embed)

@bot.command(name="setjoinchannel")
@commands.has_permissions(manage_guild=True)
async def setjoinchannel(ctx, channel: discord.TextChannel = None):
    channel = channel or ctx.channel
    await bot.db.execute("INSERT INTO guild_settings (guild_id, join_channel_id, join_enabled) VALUES (?, ?, 1) ON CONFLICT(guild_id) DO UPDATE SET join_channel_id=excluded.join_channel_id, join_enabled=1",
                         (ctx.guild.id, channel.id))
    await bot.db.commit()
    embed = discord.Embed(color=0xffdd00, description=f"{DOT} Welcome channel set to {channel.mention}.")
    await ctx.send(embed=embed)

@bot.command(name="unsetwelcomechannel")
@commands.has_permissions(manage_guild=True)
async def unsetwelcomechannel(ctx):
    await bot.db.execute("UPDATE guild_settings SET join_channel_id=NULL, join_enabled=0 WHERE guild_id=?", (ctx.guild.id,))
    await bot.db.commit()
    embed = discord.Embed(color=0xffdd00, description=f"{DOT} Welcome channel unset. Join messages are now **disabled**.")
    await ctx.send(embed=embed)

@bot.command(name="setleave")
@commands.has_permissions(manage_guild=True)
async def setleave(ctx, channel: discord.TextChannel = None):
    channel = channel or ctx.channel
    await bot.db.execute("INSERT INTO guild_settings (guild_id, leave_channel_id, leave_enabled) VALUES (?, ?, 1) ON CONFLICT(guild_id) DO UPDATE SET leave_channel_id=excluded.leave_channel_id, leave_enabled=1",
                         (ctx.guild.id, channel.id))
    await bot.db.commit()
    embed = discord.Embed(color=0xffdd00, description=f"{DOT} Leave channel set to {channel.mention}.")
    await ctx.send(embed=embed)

@bot.command(name="unsetleavechannel")
@commands.has_permissions(manage_guild=True)
async def unsetleavechannel(ctx):
    await bot.db.execute("UPDATE guild_settings SET leave_channel_id=NULL, leave_enabled=0 WHERE guild_id=?", (ctx.guild.id,))
    await bot.db.commit()
    embed = discord.Embed(color=0xffdd00, description=f"{DOT} Leave channel unset. Leave messages are now **disabled**.")
    await ctx.send(embed=embed)

@bot.command(name="addinvites")
@commands.has_permissions(administrator=True)
async def addinvites(ctx, user: discord.Member, amount: int):
    """Add invites to a user
    
    Usage: ,addinvites <user> <amount>
    Example: ,addinvites @John 5"""
    await update_user(ctx.guild.id if ctx.guild else 0, user.id, bonus=amount)
    embed = discord.Embed(color=0xffdd00, description=f"{DOT} Added **{amount}** invites to {user.mention}.")
    await ctx.send(embed=embed)

@bot.command(name="removeinvites")
@commands.has_permissions(administrator=True)
async def removeinvites(ctx, user: discord.Member, amount: int):
    """Remove invites from a user
    
    Usage: ,removeinvites <user> <amount>
    Example: ,removeinvites @John 3"""
    await update_user(ctx.guild.id if ctx.guild else 0, user.id, bonus=-amount)
    embed = discord.Embed(color=0xffdd00, description=f"{DOT} Removed **{amount}** invites from {user.mention}.")
    await ctx.send(embed=embed)

@bot.command(name="clearinvites")
@commands.has_permissions(administrator=True)
async def clearinvites(ctx, user: discord.Member = None):
    if user:
        await bot.db.execute(
            "UPDATE users SET joins=0, left=0, bonus=0, fake_invites=0, rejoins=0 "
            "WHERE guild_id=? AND user_id=?",
            (ctx.guild.id, user.id),
        )
        await bot.db.commit()
        embed = discord.Embed(color=0xffdd00, description=f"{FP_TICK} Successfully reset invites for {user.mention}.")
    else:
        await bot.db.execute(
            "UPDATE users SET joins=0, left=0, bonus=0, fake_invites=0, rejoins=0 "
            "WHERE guild_id=?",
            (ctx.guild.id,),
        )
        await bot.db.commit()
        embed = discord.Embed(color=0xffdd00, description=f"{FP_TICK} Successfully reset **all** members' invites in this guild.")
    await ctx.send(embed=embed)

@bot.command(name="resetmyinvites", aliases=["rmi", "resetmyinvite"])
async def resetmyinvites(ctx):
    await bot.db.execute(
        "UPDATE users SET joins=0, left=0, bonus=0, fake_invites=0, rejoins=0 "
        "WHERE guild_id=? AND user_id=?",
        (ctx.guild.id if ctx.guild else 0, ctx.author.id),
    )
    await bot.db.commit()
    embed = discord.Embed(color=0xffdd00, description=f"{FP_TICK} Your invite stats have been reset, {ctx.author.mention}.")
    await ctx.send(embed=embed)

@bot.command(name="resetinvites")
@commands.has_permissions(administrator=True)
async def resetinvites(ctx, user: discord.Member, counter: str = "all"):
    """Reset a member's joins, bonus/votes, leaves, or all invite counters."""
    counter = counter.lower().strip()
    if counter in {"all", "invites", "invite"}:
        assignments = "joins=0, left=0, bonus=0, fake_invites=0, rejoins=0"
        label = "invites"
    elif counter in {"joins", "join"}:
        assignments = "joins=0"
        label = "joins"
    elif counter in {"votes", "vote", "bonus"}:
        assignments = "bonus=0"
        label = "votes"
    elif counter in {"left", "leaves", "leave"}:
        assignments = "left=0"
        label = "leaves"
    elif counter in {"fake", "fakes"}:
        assignments = "fake_invites=0"
        label = "fake invites"
    elif counter in {"rejoins", "rejoin"}:
        assignments = "rejoins=0"
        label = "rejoins"
    else:
        return await ctx.send(
            f"{DOT} Use `,resetinvites @user all|joins|votes|leaves|fakes|rejoins`."
        )
    await bot.db.execute(
        f"UPDATE users SET {assignments} WHERE guild_id=? AND user_id=?",
        (ctx.guild.id, user.id),
    )
    await bot.db.commit()
    await ctx.send(f"{FP_TICK} **| I have successfully reset {user.display_name}'s {label}.**")

@bot.command(name="setaltthreshold")
@commands.has_permissions(manage_guild=True)
async def setaltthreshold(ctx, days: int):
    if not 0 <= days <= 3650:
        return await ctx.send("Choose an account-age threshold from `0` to `3650` days.")
    await bot.db.execute("INSERT INTO guild_settings (guild_id, alt_threshold) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET alt_threshold=excluded.alt_threshold",
                         (ctx.guild.id, days))
    await bot.db.commit()
    embed = discord.Embed(color=0xffdd00, description=f"{DOT} Alt threshold set to **{days} days**.")
    await ctx.send(embed=embed)

@bot.command(name="unsetaltthreshold")
@commands.has_permissions(manage_guild=True)
async def unsetaltthreshold(ctx):
    await bot.db.execute("UPDATE guild_settings SET alt_threshold=NULL WHERE guild_id=?", (ctx.guild.id,))
    await bot.db.commit()
    embed = discord.Embed(color=0xffdd00, description=f"{DOT} Alt threshold disabled.")
    await ctx.send(embed=embed)

@bot.command(name="setinviterole")
@commands.has_permissions(manage_guild=True)
async def setinviterole(ctx, role: discord.Role, invites: int):
    """Set role reward at invite milestone
    
    Usage: ,setinviterole <role> <invites>
    Example: ,setinviterole @Gold 10"""
    await bot.db.execute("INSERT OR REPLACE INTO invite_roles (guild_id, role_id, required_invites) VALUES (?, ?, ?)",
                         (ctx.guild.id, role.id, invites))
    await bot.db.commit()
    embed = discord.Embed(color=0xffdd00, description=f"{DOT} Members will receive {role.mention} when they reach **{invites}** invites.")
    await ctx.send(embed=embed)

@bot.command(name="unsetinviterole")
@commands.has_permissions(manage_guild=True)
async def unsetinviterole(ctx, role: discord.Role):
    await bot.db.execute("DELETE FROM invite_roles WHERE guild_id=? AND role_id=?", (ctx.guild.id, role.id))
    await bot.db.commit()
    embed = discord.Embed(color=0xffdd00, description=f"{DOT} Invite role reward for {role.mention} removed.")
    await ctx.send(embed=embed)

@bot.command(name="viewinviteroles")
async def viewinviteroles(ctx):
    async with bot.db.execute("SELECT role_id, required_invites FROM invite_roles WHERE guild_id=? ORDER BY required_invites ASC",
                              (ctx.guild.id,)) as cur:
        rows = await cur.fetchall()
    embed = discord.Embed(color=0xffdd00, title="🏆 Invite Role Rewards")
    if not rows:
        embed.description = f"{DOT} No invite role rewards configured."
    else:
        embed.description = "\n".join([f"{DOT} <@&{r[0]}> — **{r[1]}** invites required" for r in rows])
    await ctx.send(embed=embed)

@bot.command(name="setjoinmessage")
@commands.has_permissions(manage_guild=True)
async def setjoinmessage(ctx, *, message: str):
    await bot.db.execute("INSERT INTO guild_settings (guild_id, join_message, join_enabled) VALUES (?, ?, 1) ON CONFLICT(guild_id) DO UPDATE SET join_message=excluded.join_message, join_enabled=1",
                         (ctx.guild.id, message))
    await bot.db.commit()
    embed = discord.Embed(color=0xffdd00, description=f"{DOT} Custom join message set!\n\n**Preview:**\n{message}")
    await ctx.send(embed=embed)

@bot.command(name="unsetjoinmessage")
@commands.has_permissions(manage_guild=True)
async def unsetjoinmessage(ctx):
    await bot.db.execute("UPDATE guild_settings SET join_message=NULL WHERE guild_id=?", (ctx.guild.id,))
    await bot.db.commit()
    embed = discord.Embed(color=0xffdd00, description=f"{DOT} Custom join message deleted.")
    await ctx.send(embed=embed)

@bot.command(name="setwelcome")
@commands.has_permissions(manage_guild=True)
async def setwelcome(ctx, channel: discord.TextChannel = None):
    await setjoinchannel(ctx, channel)

@bot.command(name="setwelcomemsg")
@commands.has_permissions(manage_guild=True)
async def setwelcomemsg(ctx, *, message: str):
    await setjoinmessage(ctx, message=message)

@bot.command(name="setleavemessage")
@commands.has_permissions(manage_guild=True)
async def setleavemessage(ctx, *, message: str):
    await bot.db.execute("INSERT INTO guild_settings (guild_id, leave_message, leave_enabled) VALUES (?, ?, 1) ON CONFLICT(guild_id) DO UPDATE SET leave_message=excluded.leave_message, leave_enabled=1",
                         (ctx.guild.id, message))
    await bot.db.commit()
    embed = discord.Embed(color=0xffdd00, description=f"{DOT} Custom leave message set!\n\n**Preview:**\n{message}")
    await ctx.send(embed=embed)

@bot.command(name="unsetleavemessage")
@commands.has_permissions(manage_guild=True)
async def unsetleavemessage(ctx):
    await bot.db.execute("UPDATE guild_settings SET leave_message=NULL WHERE guild_id=?", (ctx.guild.id,))
    await bot.db.commit()
    embed = discord.Embed(color=0xffdd00, description=f"{DOT} Custom leave message deleted.")
    await ctx.send(embed=embed)

@bot.command(name="setvanitymessage")
@commands.has_permissions(manage_guild=True)
async def setvanitymessage(ctx, *, message: str):
    await bot.db.execute("INSERT INTO guild_settings (guild_id, vanity_message) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET vanity_message=excluded.vanity_message",
                         (ctx.guild.id, message))
    await bot.db.commit()
    embed = discord.Embed(color=0xffdd00, description=f"{DOT} Custom vanity message set!\n\n**Preview:**\n{message}")
    await ctx.send(embed=embed)

@bot.command(name="unsetvanitymessage")
@commands.has_permissions(manage_guild=True)
async def unsetvanitymessage(ctx):
    await bot.db.execute("UPDATE guild_settings SET vanity_message=NULL WHERE guild_id=?", (ctx.guild.id,))
    await bot.db.commit()
    embed = discord.Embed(color=0xffdd00, description=f"{DOT} Custom vanity message deleted.")
    await ctx.send(embed=embed)

@bot.command(name="variables")
async def variables(ctx):
    embed = discord.Embed(color=0xffdd00, title="📋 Message Variables")
    embed.description = (
        f"{DOT} `{{user}}` — Mentions the user\n"
        f"{DOT} `{{username}}` — User's name\n"
        f"{DOT} `{{server}}` — Server name\n"
        f"{DOT} `{{membercount}}` — Total member count\n"
        f"{DOT} `{{inviter}}` — Who invited the user\n"
        f"{DOT} `{{fake_invites}}` — Fake invite count for the inviter\n"
        f"{DOT} `{{rejoins}}` — Rejoin count for the inviter\n"
    )
    await ctx.send(embed=embed)

@bot.command(name="testmsg")
@commands.has_permissions(manage_guild=True)
async def testmsg(ctx, *, message: str):
    text = message.replace("{user}", ctx.author.mention)\
                  .replace("{username}", ctx.author.name)\
                  .replace("{server}", ctx.guild.name)\
                  .replace("{membercount}", str(ctx.guild.member_count))\
                  .replace("{inviter}", ctx.author.mention)
    embed = discord.Embed(color=0xffdd00, title="🧪 Message Preview")
    embed.description = text
    await ctx.send(embed=embed)

# ─── MESSAGES COMMANDS ─────────────────────────────────────────────

@bot.command(name="messages")
async def messages_cmd(ctx, user: discord.Member = None):
    user = user or ctx.author
    _, _, _, total, weekly, today, _, _, _ = await get_user_data(
        ctx.guild.id if ctx.guild else 0, user.id
    )
    embed = discord.Embed(color=0xffdd00, title=f"{user.name}'s Messages")
    embed.description = f"**All time • {total}** messages\n**Weekly • {weekly}** messages\n**Today • {today}** messages\n\n*Updated in real-time*"
    await ctx.send(embed=embed)

@bot.command(name="m")
async def m_cmd(ctx, user: discord.Member = None):
    await messages_cmd(ctx, user)

@bot.command(name="addmessages", aliases=["addmsg"])
@commands.has_permissions(administrator=True)
async def addmessages(ctx, user: discord.Member, amount: int):
    await update_user(ctx.guild.id if ctx.guild else 0, user.id, total_msgs=amount)
    embed = discord.Embed(color=0xffdd00, description=f"{DOT} Added **{amount}** messages to {user.mention}.")
    await ctx.send(embed=embed)

@bot.command(name="removemessages", aliases=["removemsg"])
@commands.has_permissions(administrator=True)
async def removemessages(ctx, user: discord.Member, amount: int):
    await update_user(ctx.guild.id if ctx.guild else 0, user.id, total_msgs=-amount)
    embed = discord.Embed(color=0xffdd00, description=f"{DOT} Removed **{amount}** messages from {user.mention}.")
    await ctx.send(embed=embed)

@bot.command(name="blacklistchannel", aliases=["blchannel"])
@commands.has_permissions(manage_guild=True)
async def blacklistchannel(ctx, channel: discord.TextChannel = None):
    channel = channel or ctx.channel
    await bot.db.execute("INSERT OR IGNORE INTO blacklisted_channels (guild_id, channel_id) VALUES (?, ?)", (ctx.guild.id, channel.id))
    await bot.db.commit()
    embed = discord.Embed(color=0xffdd00, description=f"{DOT} {channel.mention} is now blacklisted.")
    await ctx.send(embed=embed)

@bot.command(name="unblacklistchannel", aliases=["unblchannel"])
@commands.has_permissions(manage_guild=True)
async def unblacklistchannel(ctx, channel: discord.TextChannel = None):
    channel = channel or ctx.channel
    await bot.db.execute("DELETE FROM blacklisted_channels WHERE guild_id=? AND channel_id=?", (ctx.guild.id, channel.id))
    await bot.db.commit()
    embed = discord.Embed(color=0xffdd00, description=f"{DOT} {channel.mention} has been unblacklisted.")
    await ctx.send(embed=embed)

@bot.command(name="blacklistedchannels", aliases=["blchannels"])
async def blacklistedchannels(ctx):
    async with bot.db.execute("SELECT channel_id FROM blacklisted_channels WHERE guild_id=?", (ctx.guild.id,)) as cur:
        rows = await cur.fetchall()
    embed = discord.Embed(color=0xffdd00, title="🚫 Blacklisted Channels")
    embed.description = "\n".join([f"{DOT} <#{r[0]}>" for r in rows]) if rows else f"{DOT} No blacklisted channels."
    await ctx.send(embed=embed)

@bot.command(name="blacklistcategory")
@commands.has_permissions(manage_guild=True)
async def blacklistcategory(ctx, *, category_name: str):
    cat = discord.utils.get(ctx.guild.categories, name=category_name)
    if not cat: return await ctx.send(f"<:cross:1559374062281826397> Category `{category_name}` not found.")
    await bot.db.execute("INSERT OR IGNORE INTO blacklisted_categories (guild_id, category_id) VALUES (?, ?)", (ctx.guild.id, cat.id))
    await bot.db.commit()
    embed = discord.Embed(color=0xffdd00, description=f"{DOT} Category **{cat.name}** is now blacklisted.")
    await ctx.send(embed=embed)

@bot.command(name="unblacklistcategory")
@commands.has_permissions(manage_guild=True)
async def unblacklistcategory(ctx, *, category_name: str):
    cat = discord.utils.get(ctx.guild.categories, name=category_name)
    if not cat: return await ctx.send(f"<:cross:1559374062281826397> Category `{category_name}` not found.")
    await bot.db.execute("DELETE FROM blacklisted_categories WHERE guild_id=? AND category_id=?", (ctx.guild.id, cat.id))
    await bot.db.commit()
    embed = discord.Embed(color=0xffdd00, description=f"{DOT} Category **{cat.name}** has been unblacklisted.")
    await ctx.send(embed=embed)

@bot.command(name="blacklistedcategories")
async def blacklistedcategories(ctx):
    async with bot.db.execute("SELECT category_id FROM blacklisted_categories WHERE guild_id=?", (ctx.guild.id,)) as cur:
        rows = await cur.fetchall()
    embed = discord.Embed(color=0xffdd00, title="🚫 Blacklisted Categories")
    if not rows:
        embed.description = f"{DOT} No blacklisted categories."
    else:
        lines = []
        for (cat_id,) in rows:
            cat = ctx.guild.get_channel(cat_id)
            lines.append(f"{DOT} **{cat.name if cat else f'Unknown ({cat_id})'}**")
        embed.description = "\n".join(lines)
    await ctx.send(embed=embed)

@bot.command(name="setmessagerole")
@commands.has_permissions(manage_guild=True)
async def setmessagerole(ctx, role: discord.Role, count: int):
    await bot.db.execute("INSERT OR REPLACE INTO message_roles (guild_id, role_id, required_msgs) VALUES (?, ?, ?)",
                         (ctx.guild.id, role.id, count))
    await bot.db.commit()
    embed = discord.Embed(color=0xffdd00, description=f"{DOT} Members will receive {role.mention} when they reach **{count}** messages.")
    await ctx.send(embed=embed)

@bot.command(name="unsetmessagerole")
@commands.has_permissions(manage_guild=True)
async def unsetmessagerole(ctx, role: discord.Role):
    await bot.db.execute("DELETE FROM message_roles WHERE guild_id=? AND role_id=?", (ctx.guild.id, role.id))
    await bot.db.commit()
    embed = discord.Embed(color=0xffdd00, description=f"{DOT} Message role reward for {role.mention} removed.")
    await ctx.send(embed=embed)

@bot.command(name="viewmessageroles")
async def viewmessageroles(ctx):
    async with bot.db.execute("SELECT role_id, required_msgs FROM message_roles WHERE guild_id=? ORDER BY required_msgs ASC",
                              (ctx.guild.id,)) as cur:
        rows = await cur.fetchall()
    embed = discord.Embed(color=0xffdd00, title="📨 Message Role Rewards")
    embed.description = "\n".join([f"{DOT} <@&{r[0]}> — **{r[1]}** messages required" for r in rows]) if rows else f"{DOT} No message role rewards configured."
    await ctx.send(embed=embed)

@bot.command(name="clearmsgs", aliases=["clearmsg", "resetmessages"])
@commands.has_permissions(administrator=True)
async def clearmsgs(ctx, user: discord.Member = None):
    if user:
        await bot.db.execute(
            "UPDATE users SET total_msgs=0, weekly_msgs=0, today_msgs=0 "
            "WHERE guild_id=? AND user_id=?",
            (ctx.guild.id, user.id),
        )
        embed = discord.Embed(color=0xffdd00, description=f"{FP_TICK} Cleared messages for {user.mention}.")
    else:
        await bot.db.execute(
            "UPDATE users SET total_msgs=0, weekly_msgs=0, today_msgs=0 WHERE guild_id=?",
            (ctx.guild.id,),
        )
        embed = discord.Embed(color=0xffdd00, description=f"{FP_TICK} Cleared **all** members' messages in this guild.")
    await bot.db.commit()
    await ctx.send(embed=embed)

@bot.command(name="resetmymessages", aliases=["resetmymsgs"])
async def resetmymessages(ctx):
    await bot.db.execute(
        "UPDATE users SET total_msgs=0, weekly_msgs=0, today_msgs=0 "
        "WHERE guild_id=? AND user_id=?",
        (ctx.guild.id if ctx.guild else 0, ctx.author.id),
    )
    await bot.db.commit()
    embed = discord.Embed(color=0xffdd00, description=f"{FP_TICK} Your message stats have been reset, {ctx.author.mention}.")
    await ctx.send(embed=embed)

@bot.command(name="setresettime")
@commands.has_permissions(manage_guild=True)
async def setresettime(ctx, mode: str, *, time: str):
    """Set weekly/daily message or voice reset time. e.g. ,setresettime weeklymessage 00:00"""
    col_map = {
        "weeklymessage": "msg_weekly_reset",
        "dailymessage": "msg_daily_reset",
        "dailyvoice": "voice_daily_reset",
        "weeklyvoice": "voice_weekly_reset",
    }
    col = col_map.get(mode.lower())
    if not col: return await ctx.send("<:cross:1559374062281826397> Mode must be one of: `weeklymessage`, `dailymessage`, `dailyvoice`, `weeklyvoice`")
    await bot.db.execute(f"INSERT INTO guild_settings (guild_id, {col}) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET {col}=excluded.{col}",
                         (ctx.guild.id, time))
    await bot.db.commit()
    embed = discord.Embed(color=0xffdd00, description=f"{DOT} Reset time for **{mode}** set to `{time}`.")
    await ctx.send(embed=embed)

# ─── VOICE COMMANDS ────────────────────────────────────────────────

@bot.command(name="vc")
async def vc_cmd(ctx, user: discord.Member = None):
    user = user or ctx.author
    _, _, _, _, _, _, alltime, weekly, daily = await get_user_data(
        ctx.guild.id if ctx.guild else 0, user.id
    )
    def fmt(s): return f"{s//3600}h {(s%3600)//60}m {s%60}s"
    embed = discord.Embed(color=0xffdd00, title=f"{user.name}'s Voice Stats")
    embed.description = f"**All time • {fmt(alltime)}**\n**Weekly • {fmt(weekly)}**\n**Daily • {fmt(daily)}**\n\n*Updated in real-time*"
    await ctx.send(embed=embed)

@bot.command(name="addvctime")
@commands.has_permissions(administrator=True)
async def addvctime(ctx, user: discord.Member, amount: int):
    """Add voice time in seconds to a user's all time, daily and weekly fields."""
    await update_user(
        ctx.guild.id if ctx.guild else 0,
        user.id,
        voice_alltime=amount,
        voice_weekly=amount,
        voice_daily=amount,
    )
    embed = discord.Embed(color=0xffdd00, description=f"{DOT} Added **{amount}s** of voice time to {user.mention}.")
    await ctx.send(embed=embed)

@bot.command(name="reducevctime")
@commands.has_permissions(administrator=True)
async def reducevctime(ctx, user: discord.Member, amount: int):
    """Reduce voice time from all time, daily and weekly fields."""
    await update_user(
        ctx.guild.id if ctx.guild else 0,
        user.id,
        voice_alltime=-amount,
        voice_weekly=-amount,
        voice_daily=-amount,
    )
    embed = discord.Embed(color=0xffdd00, description=f"{DOT} Reduced **{amount}s** of voice time from {user.mention}.")
    await ctx.send(embed=embed)

@bot.command(name="clearvoice")
@commands.has_permissions(administrator=True)
async def clearvoice(ctx, user: discord.Member = None):
    if user:
        await bot.db.execute(
            "UPDATE users SET voice_alltime=0, voice_weekly=0, voice_daily=0 "
            "WHERE guild_id=? AND user_id=?",
            (ctx.guild.id, user.id),
        )
        embed = discord.Embed(color=0xffdd00, description=f"{DOT} Cleared voice stats for {user.mention}.")
    else:
        await bot.db.execute(
            "UPDATE users SET voice_alltime=0, voice_weekly=0, voice_daily=0 WHERE guild_id=?",
            (ctx.guild.id,),
        )
        embed = discord.Embed(color=0xffdd00, description=f"{DOT} Cleared **all** members' voice stats in this guild.")
    await bot.db.commit()
    await ctx.send(embed=embed)

@bot.command(name="resetmyvoice")
async def resetmyvoice(ctx):
    await bot.db.execute(
        "UPDATE users SET voice_alltime=0, voice_weekly=0, voice_daily=0 "
        "WHERE guild_id=? AND user_id=?",
        (ctx.guild.id if ctx.guild else 0, ctx.author.id),
    )
    await bot.db.commit()
    embed = discord.Embed(color=0xffdd00, description=f"{DOT} Your voice stats have been reset, {ctx.author.mention}.")
    await ctx.send(embed=embed)

# ─── LEADERBOARDS ──────────────────────────────────────────────────

@bot.hybrid_group(name="leaderboard", aliases=["lb"], invoke_without_command=True, case_insensitive=True)
async def leaderboard(ctx):
    embed = discord.Embed(color=0xffdd00, title="📊 Leaderboard Commands")
    embed.description = (
        f"{DOT} `,leaderboard invites` — Top 10 inviters\n"
        f"{DOT} `,leaderboard messages` — Top 10 all-time messengers\n"
        f"{DOT} `,leaderboard weeklymessages` — Top 10 weekly\n"
        f"{DOT} `,leaderboard dailymessages` — Top 10 daily\n"
        f"{DOT} `,leaderboard voice` — Top 10 voice (all time)\n"
        f"{DOT} `,leaderboard weeklyvoice` — Top 10 voice (weekly)\n"
        f"{DOT} `,leaderboard dailyvoice` — Top 10 voice (daily)\n"
    )
    await ctx.send(embed=embed)

async def send_paginated_leaderboard(ctx, lb_type):
    async def make_embed(page):
        return await build_lb_embed(ctx.guild, lb_type, page=page)

    member_ids = [member.id for member in ctx.guild.members]
    if member_ids:
        placeholders = ",".join("?" * len(member_ids))
        async with bot.db.execute(
            f"SELECT COUNT(*) FROM users WHERE guild_id=? AND user_id IN ({placeholders})",
            [ctx.guild.id] + member_ids
        ) as cur:
            tracked_users = (await cur.fetchone())[0]
    else:
        tracked_users = 0
    total_pages = max(1, (tracked_users + 9) // 10)
    page = [0]

    class LeaderboardView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=120)
            self.update_buttons()

        def update_buttons(self):
            self.back.disabled = page[0] < 2
            self.previous.disabled = page[0] == 0
            self.next.disabled = page[0] >= total_pages - 1
            self.forward.disabled = page[0] + 2 >= total_pages

        async def move_page(self, interaction, offset):
            if interaction.user.id != ctx.author.id:
                return await send_interaction_v2(interaction, "This leaderboard is not yours.", ephemeral=True)
            page[0] = max(0, min(total_pages - 1, page[0] + offset))
            self.update_buttons()
            await edit_interaction_v2(interaction, embed=await make_embed(page[0]), view=self)

        @discord.ui.button(emoji=_manifest_emoji("lb_back", "⏮️"), style=discord.ButtonStyle.secondary)
        async def back(self, interaction, button):
            await self.move_page(interaction, -2)

        @discord.ui.button(emoji=_manifest_emoji("lb_prev", "◀️"), style=discord.ButtonStyle.secondary)
        async def previous(self, interaction, button):
            await self.move_page(interaction, -1)

        @discord.ui.button(emoji=_manifest_emoji("lb_stop", "⏹️"), style=discord.ButtonStyle.secondary)
        async def stop_button(self, interaction, button):
            if interaction.user.id != ctx.author.id:
                return await send_interaction_v2(interaction, "This leaderboard is not yours.", ephemeral=True)
            await interaction.response.defer()
            await interaction.message.delete()
            self.stop()

        @discord.ui.button(emoji=_manifest_emoji("lb_next", "▶️"), style=discord.ButtonStyle.secondary)
        async def next(self, interaction, button):
            await self.move_page(interaction, 1)

        @discord.ui.button(emoji=_manifest_emoji("lb_forward", "⏭️"), style=discord.ButtonStyle.secondary)
        async def forward(self, interaction, button):
            await self.move_page(interaction, 2)

    view = LeaderboardView()
    await ctx.send(embed=await make_embed(0), view=view)

@leaderboard.command(name="invites", aliases=["i"])
async def lb_invites(ctx):
    await send_paginated_leaderboard(ctx, "invites")

@leaderboard.command(name="messages", aliases=["m"])
async def lb_messages(ctx):
    await send_paginated_leaderboard(ctx, "messages")

@leaderboard.command(name="weeklymessages")
async def lb_weekly(ctx):
    await send_paginated_leaderboard(ctx, "weeklymessages")

@leaderboard.command(name="dailymessages")
async def lb_daily(ctx):
    await send_paginated_leaderboard(ctx, "dailymessages")

@leaderboard.command(name="voice", aliases=["vc"])
async def lb_voice(ctx):
    await send_paginated_leaderboard(ctx, "voice")

@leaderboard.command(name="weeklyvoice")
async def lb_weeklyvoice(ctx):
    await send_paginated_leaderboard(ctx, "weeklyvoice")

@leaderboard.command(name="dailyvoice")
async def lb_dailyvoice(ctx):
    await send_paginated_leaderboard(ctx, "dailyvoice")

# ─── AUTO ROLES ────────────────────────────────────────────────────

@bot.group(name="autorole", invoke_without_command=True)
@commands.has_permissions(manage_guild=True)
async def autorole(ctx):
    embed = discord.Embed(color=0xffdd00, title=f"{E_ROLE} Auto Roles", description="Automatically assign roles to new users\n\n**Commands**")
    embed.description = (embed.description or "") + (
        f"\n{ARROW} `,autorole add [human|bot] @role` — Assign roles by member type"
        f"\n{ARROW} `,autorole remove @role` — Remove an autorole"
        f"\n{ARROW} `,autorole list` — Show configured autoroles"
        f"\n{ARROW} `,setautorole` — Set autorole for humans and bots"
        f"\n{ARROW} `,unsetautorole` — Delete an autorole"
        f"\n{ARROW} `,configscreening` — Add role after membership screening"
        f"\n{ARROW} `,viewautoroles` — Displays a guild's autoroles"
    )
    await ctx.send(embed=embed)

@autorole.command(name="add")
@commands.has_permissions(manage_guild=True)
async def autorole_add_subcommand(ctx, *, arguments: str):
    parts = arguments.strip().split(maxsplit=1)
    if not parts:
        return await ctx.send("Use `,autorole add [human|bot] @role`.")
    role_types = {"all": "all", "human": "humans", "humans": "humans", "bot": "bots", "bots": "bots"}
    if len(parts) == 1:
        role_text, role_type = parts[0], "all"
    elif parts[0].casefold() in role_types:
        role_text, role_type = parts[1], role_types[parts[0].casefold()]
    elif parts[1].casefold() in role_types:
        role_text, role_type = parts[0], role_types[parts[1].casefold()]
    else:
        role_text, role_type = arguments.strip(), "all"
    try:
        role = await UsernameRoleConverter().convert(ctx, role_text)
    except commands.BadArgument as error:
        return await ctx.send(str(error))
    await setautorole(ctx, role, role_type)

@autorole.command(name="remove")
@commands.has_permissions(manage_guild=True)
async def autorole_remove_subcommand(ctx, role: discord.Role):
    await unsetautorole(ctx, role)

@autorole.command(name="list")
@commands.has_permissions(manage_guild=True)
async def autorole_list_subcommand(ctx):
    await viewautoroles(ctx)

@bot.command(name="autorole_add")
@commands.has_permissions(manage_guild=True)
async def autorole_add(ctx, role: discord.Role):
    await setautorole(ctx, role, "all")

@bot.command(name="autorole_remove")
@commands.has_permissions(manage_guild=True)
async def autorole_remove(ctx, role: discord.Role):
    await unsetautorole(ctx, role)

@bot.command(name="autorole_list")
@commands.has_permissions(manage_guild=True)
async def autorole_list(ctx):
    await viewautoroles(ctx)

@bot.command(name="setautorole")
@commands.has_permissions(manage_guild=True)
async def setautorole(ctx, role: discord.Role, role_type: str = "all"):
    """Set an autorole. role_type: all, humans, bots"""
    if role_type not in ("all", "humans", "bots"):
        return await ctx.send("<:cross:1559374062281826397> role_type must be `all`, `humans`, or `bots`.")
    bot_member = ctx.guild.me
    if (
        role.is_default()
        or role.managed
        or bot_member is None
        or not bot_member.guild_permissions.manage_roles
        or role >= bot_member.top_role
    ):
        return await ctx.send("I need Manage Roles and a role hierarchy above the autorole.")
    await bot.db.execute("INSERT OR REPLACE INTO autoroles (guild_id, role_id, role_type) VALUES (?, ?, ?)",
                         (ctx.guild.id, role.id, role_type))
    await bot.db.commit()
    embed = discord.Embed(color=0xffdd00, description=f"{DOT} AutoRole set: {role.mention} for **{role_type}**.")
    await ctx.send(embed=embed)

@bot.command(name="unsetautorole")
@commands.has_permissions(manage_guild=True)
async def unsetautorole(ctx, role: discord.Role):
    await bot.db.execute("DELETE FROM autoroles WHERE guild_id=? AND role_id=?", (ctx.guild.id, role.id))
    await bot.db.commit()
    embed = discord.Embed(color=0xffdd00, description=f"{DOT} AutoRole for {role.mention} removed.")
    await ctx.send(embed=embed)

@bot.command(name="configscreening")
@commands.has_permissions(manage_guild=True)
async def configscreening(ctx, role: discord.Role):
    """This feature will add role to users after they complete membership screening."""
    embed = discord.Embed(color=0xffdd00, description=f"{DOT} Screening role set to {role.mention}. Note: Discord API handles this natively via membership screening.")
    await ctx.send(embed=embed)

@bot.command(name="viewautoroles")
async def viewautoroles(ctx):
    async with bot.db.execute("SELECT role_id, role_type FROM autoroles WHERE guild_id=?", (ctx.guild.id,)) as cur:
        rows = await cur.fetchall()
    embed = discord.Embed(color=0xffdd00, title=f"{E_ROLE} Auto Roles")
    embed.description = "\n".join([f"{DOT} <@&{r[0]}> — `{r[1]}`" for r in rows]) if rows else f"{DOT} No autoroles configured."
    await ctx.send(embed=embed)

# ─── LIVE LEADERBOARDS ─────────────────────────────────────────────

@bot.command(name="createlivelb")
@commands.has_permissions(manage_guild=True)
async def createlivelb(ctx, lb_type: str = "invites"):
    """Create a live leaderboard that updates every 30 seconds. Types: invites, messages, voice"""
    embed = await build_lb_embed(ctx.guild, lb_type)
    embed.set_footer(text="Live • Updates every 30 seconds")
    msg = await ctx.send(embed=embed)
    await bot.db.execute(
        "INSERT OR REPLACE INTO live_leaderboards (guild_id, channel_id, message_id, lb_type) VALUES (?, ?, ?, ?)",
        (ctx.guild.id, ctx.channel.id, msg.id, lb_type)
    )
    await bot.db.commit()
    confirm = discord.Embed(color=0xffdd00, description=f"{DOT} Live leaderboard (`{lb_type}`) created! It will update every 30 seconds.")
    await ctx.send(embed=confirm, delete_after=5)

@bot.command(name="deletelivelb")
@commands.has_permissions(manage_guild=True)
async def deletelivelb(ctx, lb_type: str = "invites"):
    await bot.db.execute("DELETE FROM live_leaderboards WHERE guild_id=? AND lb_type=?", (ctx.guild.id, lb_type))
    await bot.db.commit()
    embed = discord.Embed(color=0xffdd00, description=f"{DOT} Live leaderboard (`{lb_type}`) deleted.")
    await ctx.send(embed=embed)

@bot.command(name="showlivelbs")
async def showlivelbs(ctx):
    async with bot.db.execute("SELECT lb_type, channel_id, message_id FROM live_leaderboards WHERE guild_id=?", (ctx.guild.id,)) as cur:
        rows = await cur.fetchall()
    embed = discord.Embed(color=0xffdd00, title="📊 Live Leaderboards")
    if not rows:
        embed.description = f"{DOT} No live leaderboards configured."
    else:
        lines = [f"{DOT} `{lb_type}` — <#{ch_id}> ([Jump](https://discord.com/channels/{ctx.guild.id}/{ch_id}/{msg_id}))" for lb_type, ch_id, msg_id in rows]
        embed.description = "\n".join(lines)
    await ctx.send(embed=embed)

# ─── GIVEAWAY ──────────────────────────────────────────────────────

_giveaway_resolution_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

async def resolve_giveaway(msg_id, channel_id, reroll=False):
    async with _giveaway_resolution_locks[msg_id]:
        await _resolve_giveaway_locked(msg_id, channel_id, reroll)


async def _resolve_giveaway_locked(msg_id, channel_id, reroll=False):
    async with bot.db.execute(
        "SELECT forced_winner, prize, host_id, winners, guild_id, active, announcement_msg_id "
        "FROM giveaways WHERE msg_id = ?",
        (msg_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if not row:
        return
    forced_id, prize, host_id, winner_count, guild_id, active, announcement_msg_id = row
    if (reroll and active) or (not reroll and not active):
        return
    winner_count = max(1, int(winner_count or 1))
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException:
            return
    if channel is None:
        return
    guild = channel.guild
    guild_id = guild_id or guild.id
    async with bot.db.execute(
        "SELECT winner_role_id, bypass_role_id, required_role_id, embed_title, embed_color, "
        "embed_image_url, reaction_emoji "
        "FROM giveaway_settings WHERE guild_id=?",
        (guild_id,),
    ) as cursor:
        settings = await cursor.fetchone()
    (winner_role_id, bypass_role_id, required_role_id, embed_title, embed_color,
     embed_image_url, reaction_emoji) = settings or (None, None, None, None, None, None, None)

    try: msg = await channel.fetch_message(msg_id)
    except discord.HTTPException: return
    eligible_users = []
    seen_user_ids = set()
    target_emoji = reaction_emoji or BLACK_TADA
    for reaction in msg.reactions:
        legacy_giveaway_emoji = (
            not reaction_emoji
            and isinstance(reaction.emoji, discord.Emoji)
            and reaction.emoji.id == 1473964312107421697
        )
        if str(reaction.emoji) == target_emoji or legacy_giveaway_emoji:
            async for user in reaction.users():
                if user.bot or user.id in seen_user_ids:
                    continue
                seen_user_ids.add(user.id)
                member = user if isinstance(user, discord.Member) else guild.get_member(user.id)
                if member is None:
                    try:
                        member = await guild.fetch_member(user.id)
                    except discord.HTTPException:
                        continue
                role_ids = {role.id for role in member.roles}
                if required_role_id and required_role_id not in role_ids and bypass_role_id not in role_ids:
                    continue
                eligible_users.append(member)
    winners = []
    if forced_id and not reroll:
        forced_winner = guild.get_member(forced_id)
        if forced_winner:
            winners.append(forced_winner)
    remaining_users = [user for user in eligible_users if user.id not in {winner.id for winner in winners}]
    winners.extend(random.sample(remaining_users, min(winner_count - len(winners), len(remaining_users))))
    winner_mentions = ", ".join(winner.mention for winner in winners) if winners else "No one"
    winner_role = guild.get_role(winner_role_id) if winner_role_id else None
    if winner_role:
        for winner in winners:
            try:
                await winner.add_roles(winner_role, reason=f"Won giveaway {msg_id}")
            except discord.HTTPException:
                pass
    e = discord.Embed(
        title=embed_title or None,
        description=f"**{GIFT} {prize} {GIFT}**\n\n{DOT} Hosted by: <@{host_id}>\n{DOT} Valid participant(s): {len(eligible_users)}\n\n{FP_TICK} **Winner(s):**\n{winner_mentions}",
        color=embed_color or 0xffdd00,
    )
    if embed_image_url and _is_direct_giveaway_image_url(embed_image_url):
        e.set_image(url=embed_image_url)
    ended_at = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    e.set_footer(text=f"Ended • <t:{ended_at}:F>")
    if not reroll:
        announcement = None
        if announcement_msg_id:
            try:
                announcement = await channel.fetch_message(announcement_msg_id)
            except discord.HTTPException:
                pass
        ended_message = f"{GIVEAWAY_ENDED_EMOJI} **Giveaway Ended** {GIVEAWAY_ENDED_EMOJI}"
        if announcement is None:
            expected_header = f"{GIVEAWAY_EMOJI} **New Giveaway** {GIVEAWAY_EMOJI}"
            bot_user_id = getattr(getattr(bot, "user", None), "id", None)
            try:
                async for candidate in channel.history(limit=5, before=msg):
                    if (
                        candidate.content == expected_header
                        and (bot_user_id is None or candidate.author.id == bot_user_id)
                    ):
                        announcement = candidate
                        break
            except discord.HTTPException:
                pass
        if announcement is not None:
            try:
                await announcement.edit(content=ended_message)
            except discord.HTTPException as error:
                logging.getLogger(__name__).warning(
                    "Could not update giveaway header %s: %s", msg_id, error
                )
                try:
                    await channel.send(
                        ended_message,
                        reference=announcement,
                        mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    pass
        else:
            try:
                await channel.send(
                    ended_message,
                    reference=msg,
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException as error:
                logging.getLogger(__name__).warning(
                    "Could not post ended reply for giveaway %s: %s", msg_id, error
                )
    await edit_message_v2(msg, embed=e)
    if winners:
        v = discord.ui.View().add_item(discord.ui.Button(label="Giveaway Link", url=msg.jump_url))
        notification = (
            f"{FP_TICK} New Winner! {winner_mentions} you have won **{prize}**, hosted by <@{host_id}>"
            if reroll
            else f"Congrats, {winner_mentions} you have won **{prize}**, hosted by <@{host_id}>"
        )
        await send_v2(
            channel,
            notification,
            view=v,
            reference=msg,
            mention_author=False,
        )
    if not reroll:
        await bot.db.execute("UPDATE giveaways SET active=0 WHERE msg_id = ?", (msg_id,))
        await bot.db.commit()

@bot.command(name="gcreate")
@commands.has_permissions(administrator=True)
async def gcreate(ctx, time: str, winners: int, *, prize: str):
    """Create giveaway with requirements. Usage: ,gcreate 10m 1 Prize Name"""
    if not 1 <= winners <= 100:
        return await ctx.send(f"{DOT} Winners must be between `1` and `100`.")
    match = re.fullmatch(r'(\d+)([smhd])', time.strip().lower())
    if not match: return await ctx.send("<:cross:1559374062281826397> Invalid time format. Use formats like `30s`, `5m`, `3h`, `1d`.")
    amount, unit = match.groups()
    sec = int(amount) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    if not 1 <= sec <= 365 * 86400:
        return await ctx.send("Giveaway duration must be between 1 second and 365 days.")
    end = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=sec)
    async with bot.db.execute(
        "SELECT required_role_id, embed_color, embed_image_url, reaction_emoji "
        "FROM giveaway_settings WHERE guild_id=?",
        (ctx.guild.id,),
    ) as cursor:
        giveaway_settings = await cursor.fetchone()
    required_role_id, embed_color, embed_image_url, reaction_emoji = giveaway_settings or (None, None, None, None)
    reaction_emoji = reaction_emoji or BLACK_TADA
    required_role = ctx.guild.get_role(required_role_id) if required_role_id else None
    requirement_text = f"\n{DOT} Required role: {required_role.mention}" if required_role else ""
    e = discord.Embed(
        description=f"**{GIFT} {prize} {GIFT}**\n\n{DOT} Winners: **{winners}**\n{DOT} Ends: <t:{int(end.timestamp())}:R> ( <t:{int(end.timestamp())}:F> )\n{DOT} Hosted by: {ctx.author.mention}{requirement_text}\n\n{DOT} React with {reaction_emoji} to participate!",
        color=embed_color or 0xffdd00,
    )
    if embed_image_url and _is_direct_giveaway_image_url(embed_image_url):
        e.set_image(url=embed_image_url)
    e.set_footer(text=f"Ends at • {end.strftime('%m/%d/%y')}")
    announcement = await ctx.channel.send(f"{GIVEAWAY_EMOJI} **New Giveaway** {GIVEAWAY_EMOJI}")
    msg = await ctx.send(embed=e)
    await msg.add_reaction(reaction_emoji)
    await bot.db.execute(
        "INSERT INTO giveaways (msg_id, prize, host_id, winners, guild_id, channel_id, "
        "announcement_msg_id, ends_at, active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
        (msg.id, prize, ctx.author.id, winners, ctx.guild.id, ctx.channel.id, announcement.id, end.isoformat()),
    )
    await bot.db.commit()

@bot.command(name="gstart")
@commands.has_permissions(administrator=True)
async def gstart(ctx, time: str, winners: int, *, prize: str):
    """Creates a giveaway. Alias for gcreate."""
    await gcreate(ctx, time, winners, prize=prize)

@bot.command(name="greroll")
@commands.has_permissions(administrator=True)
async def greroll(ctx):
    """Rerolls an ended giveaway."""
    if ctx.message.reference:
        msg_id = ctx.message.reference.message_id
        async with bot.db.execute("SELECT active FROM giveaways WHERE msg_id=?", (msg_id,)) as cursor:
            row = await cursor.fetchone()
        if not row:
            return await ctx.send("No stored giveaway found. Reply to an ended giveaway message.")
        if row[0]:
            return await ctx.send("End the giveaway before rerolling it.")
        await resolve_giveaway(msg_id, ctx.channel.id, reroll=True)
    else:
        await ctx.send("<:cross:1559374062281826397> Reply to a giveaway message to reroll.")

@bot.command(name="gend")
@commands.has_permissions(administrator=True)
async def gend(ctx):
    """Ends an active giveaway."""
    if ctx.message.reference:
        msg_id = ctx.message.reference.message_id
        async with bot.db.execute("SELECT active FROM giveaways WHERE msg_id=?", (msg_id,)) as cursor:
            row = await cursor.fetchone()
        if not row or not row[0]:
            return await ctx.send("No active giveaway found for that message.")
        await resolve_giveaway(msg_id, ctx.channel.id)
    else:
        await ctx.send("<:cross:1559374062281826397> Reply to a giveaway message to end it.")


async def _save_giveaway_banner(guild_id, image_url):
    await bot.db.execute("INSERT OR IGNORE INTO giveaway_settings (guild_id) VALUES (?)", (guild_id,))
    await bot.db.execute(
        "UPDATE giveaway_settings SET embed_image_url=? WHERE guild_id=?",
        (image_url, guild_id),
    )
    await bot.db.commit()


GIVEAWAY_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".apng"}


def _is_direct_giveaway_image_url(image_url):
    try:
        parsed_url = urlsplit(image_url.strip())
    except (AttributeError, ValueError):
        return False
    return (
        parsed_url.scheme in {"http", "https"}
        and bool(parsed_url.netloc)
        and Path(parsed_url.path).suffix.lower() in GIVEAWAY_IMAGE_EXTENSIONS
    )


def _is_supported_giveaway_image(attachment):
    return (
        (getattr(attachment, "content_type", None) or "").startswith("image/")
        or Path(getattr(attachment, "filename", "")).suffix.lower() in GIVEAWAY_IMAGE_EXTENSIONS
    )


class GiveawayBannerModal(discord.ui.Modal):
    def __init__(self, guild_id, owner_id):
        super().__init__(title="Upload Giveaway Banner", timeout=120)
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.banner_upload = discord.ui.FileUpload(
            custom_id="giveaway_banner_upload",
            min_values=1,
            max_values=1,
            required=False,
        )
        self.add_item(discord.ui.Label(
            text="Choose an image from your device",
            description="PNG, JPG, WEBP, GIF, or APNG. This takes priority over a link.",
            component=self.banner_upload,
        ))
        self.banner_url = discord.ui.TextInput(
            label="Or paste an image URL",
            placeholder="https://example.com/banner.png",
            required=False,
            max_length=400,
        )
        self.add_item(self.banner_url)

    async def on_submit(self, interaction):
        if interaction.user.id != self.owner_id:
            return await send_interaction_v2(
                interaction,
                "Only the administrator who opened this upload can use it.",
                ephemeral=True,
                allow_ephemeral=True,
            )
        attachment = next(
            (item for item in self.banner_upload.values if _is_supported_giveaway_image(item)),
            None,
        )
        image_url = attachment.url if attachment is not None else self.banner_url.value.strip()
        if not image_url:
            return await send_interaction_v2(
                interaction,
                "Choose an image from your device or provide an image URL.",
                ephemeral=True,
                allow_ephemeral=True,
            )
        if attachment is None:
            if not _is_direct_giveaway_image_url(image_url):
                return await send_interaction_v2(
                    interaction,
                    "Use a direct image link ending in `.png`, `.jpg`, `.webp`, `.gif`, or `.apng`. Webpage links do not work.",
                    ephemeral=True,
                    allow_ephemeral=True,
                )
        await _save_giveaway_banner(self.guild_id, image_url)
        embed = discord.Embed(
            title="Giveaway Banner Saved",
            description="The selected image will appear on new giveaways in this server.",
            color=0xFFDD00,
        )
        embed.set_image(url=image_url)
        await send_interaction_v2(
            interaction,
            embed=embed,
            ephemeral=True,
            allow_ephemeral=True,
        )


class GiveawayBannerUploadView(discord.ui.View):
    def __init__(self, guild_id, owner_id):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.owner_id = owner_id

    @discord.ui.button(label="Upload Banner", style=discord.ButtonStyle.secondary)
    async def upload_banner(self, interaction, button):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message(
                "Only the administrator who opened this upload can use it.",
                ephemeral=True,
            )
        await interaction.response.send_modal(
            GiveawayBannerModal(self.guild_id, self.owner_id)
        )

    @discord.ui.button(label="Remove Banner", style=discord.ButtonStyle.secondary)
    async def remove_banner(self, interaction, button):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message(
                "Only the administrator who opened these controls can remove the banner.",
                ephemeral=True,
            )
        await _save_giveaway_banner(self.guild_id, None)
        await send_interaction_v2(
            interaction,
            "Giveaway banner removed. New giveaways will no longer show a banner.",
            ephemeral=True,
            allow_ephemeral=True,
        )


@bot.command(name="setgiveaway")
@commands.has_permissions(administrator=True)
async def setgiveaway(ctx, option: str | None = None, *, value: str = ""):
    """Configure giveaway roles or embed styling. Example: ,setgiveaway requiredrole @role"""
    option = (option or "").casefold()
    options = {
        "winnerrole": ("winner_role_id", "winner role"),
        "bypassrole": ("bypass_role_id", "bypass role"),
        "requiredrole": ("required_role_id", "required role"),
        "title": ("embed_title", "embed title"),
        "color": ("embed_color", "embed color"),
    }
    if option == "image":
        attachment = next(
            (item for item in ctx.message.attachments if _is_supported_giveaway_image(item)),
            None,
        )
        if attachment is not None:
            await _save_giveaway_banner(ctx.guild.id, attachment.url)
            return await ctx.send(f"{DOT} Giveaway banner saved.")
        else:
            prompt = discord.Embed(
                title="Giveaway Banner",
                description=(
                    "Click **Upload Banner** to choose an image from your device or paste a direct image link.\n"
                    "Supported formats: PNG, JPG, WEBP, GIF, APNG. Webpage links do not work."
                ),
                color=0xFFDD00,
            )
            return await ctx.send(
                embed=prompt,
                view=GiveawayBannerUploadView(ctx.guild.id, ctx.author.id),
            )

    if option == "emoji":
        emoji_setting_value = value.strip()
        if not emoji_setting_value:
            return await ctx.send(
                "⚠️ Missing required argument(s).\n"
                "Usage: `,setgiveaway emoji <emoji/emoji id>`"
            )
        emoji_match = EMOJI_RE.fullmatch(emoji_setting_value)
        if emoji_setting_value.isdigit():
            emoji = ctx.guild.get_emoji(int(emoji_setting_value)) or bot.get_emoji(int(emoji_setting_value))
            if emoji is None:
                return await ctx.send(f"{DOT} I could not find that custom emoji.")
            emoji_setting_value = str(emoji)
        elif emoji_match:
            emoji_id = int(emoji_match.group(3))
            emoji = ctx.guild.get_emoji(emoji_id) or bot.get_emoji(emoji_id)
            if emoji is not None:
                emoji_setting_value = str(emoji)
        elif any(character.isspace() for character in emoji_setting_value):
            return await ctx.send("Provide one emoji or a custom emoji ID.")
        await bot.db.execute("INSERT OR IGNORE INTO giveaway_settings (guild_id) VALUES (?)", (ctx.guild.id,))
        await bot.db.execute(
            "UPDATE giveaway_settings SET reaction_emoji=? WHERE guild_id=?",
            (emoji_setting_value, ctx.guild.id),
        )
        await bot.db.commit()
        return await ctx.send(f"{DOT} Giveaway reaction emoji set to {emoji_setting_value}.")

    if option not in options:
        help_embed = discord.Embed(
            title="Customize Giveaway Embed",
            description=(
                "Use the following subcommands:\n"
                f"{ARROW} `setgiveaway emoji <emoji/emoji id>`\n"
                f"{ARROW} `setgiveaway bypassrole <role id>`\n"
                f"{ARROW} `setgiveaway image`\n"
                f"{ARROW} `setgiveaway winnerrole <role id>`\n"
                f"{ARROW} `setgiveaway requiredrole <role id>`\n"
                f"{ARROW} `setgiveaway title <text>`\n"
                f"{ARROW} `setgiveaway color <#RRGGBB>`"
            ),
            color=0xFFDD00,
        )
        return await ctx.send(embed=help_embed)
    column, label = options[option]
    setting_text = value.strip()
    embed_setting_value: str | int = setting_text
    role: discord.Role | None = None
    if column.endswith("_role_id"):
        if not setting_text:
            return await ctx.send(
                "⚠️ Missing required argument(s).\n"
                f"Usage: `,setgiveaway {option} <role id>`"
            )
        try:
            role = await UsernameRoleConverter().convert(ctx, setting_text)
        except commands.RoleNotFound:
            return await ctx.send("Mention a role that belongs to this server.")
        if role.is_default() or role.managed:
            return await ctx.send("Choose a normal, assignable server role.")
        embed_setting_value = role.id
    elif column == "embed_title":
        if not setting_text or len(setting_text) > 256:
            return await ctx.send("Giveaway title must contain 1 to 256 characters.")
    else:
        color_text = setting_text.removeprefix("#").removeprefix("0x")
        if not re.fullmatch(r"[0-9a-fA-F]{6}", color_text):
            return await ctx.send("Use a six-digit color such as `#FFDD00`.")
        embed_setting_value = int(color_text, 16)

    await bot.db.execute("INSERT OR IGNORE INTO giveaway_settings (guild_id) VALUES (?)", (ctx.guild.id,))
    await bot.db.execute(
        f"UPDATE giveaway_settings SET {column}=? WHERE guild_id=?",
        (embed_setting_value, ctx.guild.id),
    )
    await bot.db.commit()
    if column.endswith("_role_id") and role is not None:
        display_value = role.mention
    elif column == "embed_color":
        display_value = f"#{int(embed_setting_value):06X}"
    else:
        display_value = str(embed_setting_value)
    await ctx.send(f"Giveaway {label} set to {display_value}.")

@bot.command(name="unsetgiveaway")
@commands.has_permissions(administrator=True)
async def unsetgiveaway(ctx, option: str = "all"):
    columns = {
        "winnerrole": "winner_role_id",
        "bypassrole": "bypass_role_id",
        "requiredrole": "required_role_id",
        "title": "embed_title",
        "color": "embed_color",
        "emoji": "reaction_emoji",
        "image": "embed_image_url",
    }
    option = option.casefold()
    if option == "all":
        await bot.db.execute("DELETE FROM giveaway_settings WHERE guild_id=?", (ctx.guild.id,))
        message = "Giveaway customization and role requirements reset."
    elif option in columns:
        await bot.db.execute("UPDATE giveaway_settings SET {}=NULL WHERE guild_id=?".format(columns[option]), (ctx.guild.id,))
        message = f"Giveaway {option} setting cleared."
    else:
        return await ctx.send("Choose `winnerrole`, `bypassrole`, `requiredrole`, `emoji`, `image`, `title`, `color`, or `all`.")
    await bot.db.commit()
    await ctx.send(embed=discord.Embed(color=0xffdd00, description=f"{DOT} {message}"))

@bot.command(name="viewwinnerrole")
async def viewwinnerrole(ctx):
    async with bot.db.execute("SELECT winner_role_id FROM giveaway_settings WHERE guild_id=?", (ctx.guild.id,)) as cursor:
        row = await cursor.fetchone()
    role = ctx.guild.get_role(row[0]) if row and row[0] else None
    description = f"{DOT} Winner role: {role.mention}" if role else f"{DOT} No custom giveaway winner role configured."
    embed = discord.Embed(color=0xffdd00, description=description)
    await ctx.send(embed=embed)

@bot.command(name="viewbypassrole")
async def viewbypassrole(ctx):
    async with bot.db.execute("SELECT bypass_role_id FROM giveaway_settings WHERE guild_id=?", (ctx.guild.id,)) as cursor:
        row = await cursor.fetchone()
    role = ctx.guild.get_role(row[0]) if row and row[0] else None
    description = f"{DOT} Bypass role: {role.mention}" if role else f"{DOT} No custom giveaway bypass role configured."
    embed = discord.Embed(color=0xffdd00, description=description)
    await ctx.send(embed=embed)


@bot.command(name="giveawayconfig")
@commands.has_permissions(manage_guild=True)
async def giveawayconfig(ctx, action: str = "status", *, value: str = ""):
    """Configure giveaway website visibility without using the website dashboard."""
    action = action.casefold()
    if action == "status":
        async with bot.db.execute(
            "SELECT enabled, invite_url, notes FROM giveaway_website_settings WHERE guild_id=?",
            (ctx.guild.id,),
        ) as cursor:
            row = await cursor.fetchone()
        enabled, invite_url, notes = row or (0, None, None)
        embed = discord.Embed(title="Giveaway Website Visibility", color=0xffdd00)
        embed.add_field(name="Current Status", value="Enabled" if enabled else "Disabled", inline=False)
        embed.add_field(name="Server Invite", value=invite_url or "Not set", inline=False)
        embed.add_field(name="Notes", value=notes or "None", inline=False)
        embed.set_footer(text="Use ,giveawayconfig enable <permanent invite> or ,giveawayconfig disable")
        return await ctx.send(embed=embed)

    if action in {"enable", "invite"}:
        if not value.strip():
            return await ctx.send("Provide an invite URL with unlimited uses and no expiration.")
        try:
            invite = await bot.fetch_invite(value.strip())
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return await ctx.send("That invite could not be checked. Make sure it is valid and not expired.")
        if invite.guild is None or invite.guild.id != ctx.guild.id:
            return await ctx.send("The invite must point to this server.")
        if invite.max_uses not in (None, 0) or invite.max_age not in (None, 0):
            return await ctx.send("The invite must have unlimited uses and never expire.")
        await bot.db.execute(
            "INSERT INTO giveaway_website_settings (guild_id, enabled, invite_url) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET enabled=excluded.enabled, invite_url=excluded.invite_url",
            (ctx.guild.id, int(action == "enable"), invite.url),
        )
        await bot.db.commit()
        status = "enabled" if action == "enable" else "invite updated"
        return await ctx.send(f"Giveaway website visibility {status}.")

    if action == "disable":
        await bot.db.execute(
            "INSERT INTO giveaway_website_settings (guild_id, enabled) VALUES (?, 0) "
            "ON CONFLICT(guild_id) DO UPDATE SET enabled=0",
            (ctx.guild.id,),
        )
        await bot.db.commit()
        return await ctx.send("Giveaway website visibility disabled.")

    if action == "notes":
        if len(value) > 500:
            return await ctx.send("Notes must be 500 characters or fewer.")
        await bot.db.execute(
            "INSERT INTO giveaway_website_settings (guild_id, notes) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET notes=excluded.notes",
            (ctx.guild.id, value.strip() or None),
        )
        await bot.db.commit()
        return await ctx.send("Giveaway website notes updated.")

    await ctx.send("Use `,giveawayconfig`, `enable <invite>`, `invite <invite>`, `disable`, or `notes <text>`.")

# Legacy aliases
@bot.command(name="gwstart")
@commands.has_permissions(administrator=True)
async def gwstart(ctx, time: str, winners: int, *, prize: str):
    await gcreate(ctx, time, winners, prize=prize)

@bot.command(name="setwin")
@commands.has_permissions(administrator=True)
async def setwin(ctx, user: discord.Member):
    await ctx.message.delete()
    if ctx.message.reference:
        await bot.db.execute("UPDATE giveaways SET forced_winner = ? WHERE msg_id = ?", (user.id, ctx.message.reference.message_id))
        await bot.db.commit()

@bot.command(name="finish")
@commands.has_permissions(administrator=True)
async def finish(ctx):
    await ctx.message.delete()
    if ctx.message.reference:
        await resolve_giveaway(ctx.message.reference.message_id, ctx.channel.id)

@bot.command(name="reroll")
@commands.has_permissions(administrator=True)
async def reroll(ctx):
    await ctx.message.delete()
    if ctx.message.reference:
        await resolve_giveaway(ctx.message.reference.message_id, ctx.channel.id, reroll=True)

# ─── TIMER ─────────────────────────────────────────────────────────

active_timers: dict[int, dict[str, Any]] = {}

def parse_duration(time_str: str):
    total = 0.0
    matches = re.findall(r'(\d+(?:\.\d+)?)([smhd])', time_str.lower())
    if not matches: return None
    for amount, unit in matches:
        total += float(amount) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return int(total)


async def restore_active_timers():
    async with bot.db.execute(
        "SELECT guild_id, channel_id, label, end_at, remaining_seconds, paused FROM timers"
    ) as cursor:
        rows = await cursor.fetchall()
    now = datetime.datetime.now(datetime.timezone.utc)
    for guild_id, channel_id, label, end_at, remaining_seconds, paused in rows:
        if not bot_owns_guild(bot, guild_id):
            continue
        if guild_id in active_timers:
            continue
        deadline = None
        if end_at:
            try:
                deadline = datetime.datetime.fromisoformat(end_at)
            except (TypeError, ValueError):
                logging.getLogger(__name__).error(
                    "Discarding timer with invalid end time for guild %s",
                    guild_id,
                )
                await bot.db.execute("DELETE FROM timers WHERE guild_id=?", (guild_id,))
                continue
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=datetime.timezone.utc)
            deadline = deadline.astimezone(datetime.timezone.utc)
        remaining = (
            max(0, int((deadline - now).total_seconds()))
            if deadline is not None and not paused
            else remaining_seconds
        )
        data = {
            "task": None,
            "end_time": deadline,
            "paused": bool(paused),
            "remaining": remaining,
            "channel_id": channel_id,
            "label": label,
        }
        if not paused:
            data["end_time"] = now + datetime.timedelta(seconds=remaining)
            data["task"] = bot.loop.create_task(run_timer(guild_id, remaining))
        active_timers[guild_id] = data


async def run_timer(guild_id: int, seconds: int):
    try:
        await asyncio.sleep(seconds)
        data = active_timers.get(guild_id)
        if not data:
            return
        channel = bot.get_channel(data["channel_id"])
        label = data.get("label", "Timer")
        if channel:
            try:
                await send_v2(
                    channel,
                    f"{FP_TICK} **Timer Ended**\n\n{ARROW} ⏰ **{label}** has ended!",
                )
            except discord.HTTPException:
                logging.getLogger(__name__).exception(
                    "Could not post completion for timer in guild %s",
                    guild_id,
                )
        else:
            logging.getLogger(__name__).warning(
                "Could not post completion for timer in guild %s: channel %s is unavailable",
                guild_id,
                data["channel_id"],
            )
        active_timers.pop(guild_id, None)
        await bot.db.execute("DELETE FROM timers WHERE guild_id=?", (guild_id,))
        await bot.db.commit()
    except asyncio.CancelledError:
        return

@bot.command(name="tstart")
@commands.has_permissions(manage_guild=True)
async def tstart(ctx, time_str: str, *, label: str = "Timer"):
    if ctx.guild.id in active_timers:
        return await ctx.send("<:cross:1559374062281826397> A timer is already running! Use `,tend` to end it first.")
    seconds = parse_duration(time_str)
    if not seconds: return await ctx.send("<:cross:1559374062281826397> Invalid format. Use `30s`, `5m`, `3h`, `1d`.")
    end_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds)
    await bot.db.execute(
        "INSERT INTO timers "
        "(guild_id, channel_id, label, end_at, remaining_seconds, paused) "
        "VALUES (?, ?, ?, ?, ?, 0)",
        (ctx.guild.id, ctx.channel.id, label, end_time.isoformat(), seconds),
    )
    await bot.db.commit()
    task = bot.loop.create_task(run_timer(ctx.guild.id, seconds))
    active_timers[ctx.guild.id] = {"task": task, "end_time": end_time, "paused": False, "remaining": seconds, "channel_id": ctx.channel.id, "label": label}
    embed = discord.Embed(color=0xffdd00)
    end_timestamp = int(end_time.timestamp())
    timer_clock = _manifest_emoji("fp_timerclock", "⏱️")
    timer_arrow = _manifest_emoji("fp_timer_arrow", ARROW)
    support_link = f"[here]({SUPPORT_SERVER_URL})" if SUPPORT_SERVER_URL else "here"
    embed.description = (
        f"**{label}**\n\n"
        f"{timer_clock} Ends : <t:{end_timestamp}:R> (<t:{end_timestamp}:f>) {timer_clock}\n\n"
        f"{timer_arrow} Discover new server events {support_link}"
    )
    timer_emoji = _manifest_emoji("timer", "⏱️")
    await ctx.channel.send(f"{timer_emoji} **Timer** {timer_emoji}")
    await ctx.send(embed=embed)

@bot.command(name="tpause")
@commands.has_permissions(manage_guild=True)
async def tpause(ctx):
    data = active_timers.get(ctx.guild.id)
    if not data: return await ctx.send(f"{ARROW} No active timer to pause.")
    if data["paused"]: return await ctx.send(f"{ARROW} Timer is already paused.")
    remaining = int(
        (data["end_time"] - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    )
    if remaining <= 0: return await ctx.send(f"{ARROW} Timer has already ended.")
    data["task"].cancel(); data["paused"] = True; data["remaining"] = remaining
    await bot.db.execute(
        "UPDATE timers SET end_at=NULL, remaining_seconds=?, paused=1 WHERE guild_id=?",
        (remaining, ctx.guild.id),
    )
    await bot.db.commit()
    await ctx.send(f"{FP_TICK} **Timer Paused**\n\n{ARROW} **{data['label']}** paused with **{remaining}s** remaining.")

@bot.command(name="tresume")
@commands.has_permissions(manage_guild=True)
async def tresume(ctx):
    data = active_timers.get(ctx.guild.id)
    if not data: return await ctx.send(f"{ARROW} No timer found.")
    if not data["paused"]: return await ctx.send(f"{ARROW} Timer is not paused.")
    remaining = data["remaining"]
    new_end = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=remaining)
    await bot.db.execute(
        "UPDATE timers SET end_at=?, remaining_seconds=?, paused=0 WHERE guild_id=?",
        (new_end.isoformat(), remaining, ctx.guild.id),
    )
    await bot.db.commit()
    task = bot.loop.create_task(run_timer(ctx.guild.id, remaining))
    data["task"] = task; data["end_time"] = new_end; data["paused"] = False
    await ctx.send(
        f"{FP_TICK} **Timer Resumed**\n\n{ARROW} **{data['label']}** resumed.\n"
        f"{ARROW} **Ends:** <t:{int(new_end.timestamp())}:R>"
    )

@bot.command(name="tend")
@commands.has_permissions(manage_guild=True)
async def tend(ctx):
    data = active_timers.pop(ctx.guild.id, None)
    if not data: return await ctx.send(f"{ARROW} No active timer to end.")
    if data["task"] is not None:
        data["task"].cancel()
    await bot.db.execute("DELETE FROM timers WHERE guild_id=?", (ctx.guild.id,))
    await bot.db.commit()
    await ctx.send(f"{FP_TICK} **Timer Ended**\n\n{ARROW} **{data['label']}** ended early.")

# ─── GREET ─────────────────────────────────────────────────────────

@bot.command(name="setgreet")
@commands.has_permissions(manage_guild=True)
async def setgreet(ctx, mode: str = "embed", channel: discord.TextChannel = None):
    channel = channel or ctx.channel
    async with bot.db.execute("SELECT COUNT(*) FROM greet_channels WHERE guild_id=?", (ctx.guild.id,)) as cur:
        count = (await cur.fetchone())[0]
    if count >= 3: return await ctx.send("<:cross:1559374062281826397> You can only have up to **3** greet channels.")
    if mode not in ("embed", "message"): return await ctx.send("<:cross:1559374062281826397> Mode must be `embed` or `message`.")
    await bot.db.execute("INSERT OR REPLACE INTO greet_channels (guild_id, channel_id, greet_type) VALUES (?, ?, ?)",
                         (ctx.guild.id, channel.id, mode))
    await bot.db.commit()
    embed = discord.Embed(color=0xffdd00, description=f"{DOT} Greet channel set to {channel.mention} (`{mode}` mode).")
    await ctx.send(embed=embed)

@bot.command(name="disablegreet")
@commands.has_permissions(manage_guild=True)
async def disablegreet(ctx, channel: discord.TextChannel = None):
    channel = channel or ctx.channel
    await bot.db.execute("DELETE FROM greet_channels WHERE guild_id=? AND channel_id=?", (ctx.guild.id, channel.id))
    await bot.db.commit()
    embed = discord.Embed(color=0xffdd00, description=f"{DOT} Greet system disabled for {channel.mention}.")
    await ctx.send(embed=embed)

@bot.command(name="greetchannels")
async def greetchannels(ctx):
    async with bot.db.execute("SELECT channel_id, greet_type FROM greet_channels WHERE guild_id=?", (ctx.guild.id,)) as cur:
        rows = await cur.fetchall()
    embed = discord.Embed(color=0xffdd00, title="👋 Greet Channels")
    embed.description = "\n".join([f"{DOT} <#{r[0]}> — `{r[1]}` mode" for r in rows]) if rows else f"{DOT} No greet channels configured."
    await ctx.send(embed=embed)

@bot.command(name="greetvariables")
async def greetvariables(ctx):
    embed = discord.Embed(color=0xffdd00, title="📋 Greet Variables")
    embed.description = (
        f"{DOT} `{{user}}` — Mentions the user\n"
        f"{DOT} `{{username}}` — User's name\n"
        f"{DOT} `{{server}}` — Server name\n"
        f"{DOT} `{{membercount}}` — Total members\n"
    )
    await ctx.send(embed=embed)

# ─── MODERATION ────────────────────────────────────────────────────

async def _resolve_context_member(ctx, member=None, query=None):
    if ctx.guild is None:
        return None
    if isinstance(member, discord.Member):
        return member
    target = member if isinstance(member, discord.User) else None
    message = getattr(ctx, "message", None)
    query_text = str(query or "").strip()
    if target is None and query_text:
        id_match = re.match(r"^(?:<@!?(\d+)>|@?(\d+))(?:\s|$)", query_text)
        if id_match:
            user_id = int(id_match.group(1) or id_match.group(2))
            target = ctx.guild.get_member(user_id)
            if target is None:
                try:
                    target = await ctx.guild.fetch_member(user_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    return None
        else:
            normalized = query_text.casefold()
            matches = []
            for candidate in ctx.guild.members:
                names = {
                    str(candidate),
                    candidate.name,
                    candidate.display_name,
                    getattr(candidate, "global_name", None) or "",
                }
                for name in names:
                    name = name.strip()
                    if name and (normalized == name.casefold() or normalized.startswith(name.casefold() + " ")):
                        matches.append((len(name), candidate))
            if matches:
                target = max(matches, key=lambda item: item[0])[1]
            else:
                token = query_text.split(maxsplit=1)[0]
                try:
                    target = await UsernameMemberConverter().convert(ctx, token)
                except commands.BadArgument:
                    target = None
    if target is None and message is not None and message.mentions:
        target = message.mentions[0]
    reference = getattr(message, "reference", None)
    if target is None and reference is not None:
        resolved = reference.resolved
        resolved_author = getattr(resolved, "author", None)
        if resolved_author is not None:
            target = resolved_author
        elif isinstance(resolved, (discord.Member, discord.User)):
            target = resolved
        if target is None and reference.message_id:
            try:
                target = (await ctx.channel.fetch_message(reference.message_id)).author
            except (discord.Forbidden, discord.HTTPException):
                pass
    if isinstance(target, discord.Member):
        return target
    if isinstance(target, discord.User):
        member = ctx.guild.get_member(target.id)
        if member is not None:
            return member
        try:
            return await ctx.guild.fetch_member(target.id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
    return None


def _remove_target_mention(arguments, member):
    if member is None:
        return arguments.strip()
    text = arguments.strip()
    identifiers = [
        f"<@{member.id}>", f"<@!{member.id}>", str(member.id),
        member.display_name, member.name, getattr(member, "global_name", None) or "", str(member),
    ]
    for identifier in sorted((item for item in identifiers if item), key=len, reverse=True):
        if text.casefold().startswith(identifier.casefold()):
            end = len(identifier)
            if end == len(text) or text[end].isspace() or text[end] in ",:|":
                return text[end:].lstrip(" \t,:|")
    return text


def _snipe_embed(data):
    embed = discord.Embed(
        title="Deleted Message",
        description=(data["content"] or "*No text content.*")[:4096],
        color=0xFFDD00,
        timestamp=data["created_at"],
    )
    embed.set_author(name=data["author"], icon_url=data["avatar_url"])
    if data["attachments"]:
        attachment_text = "\n".join(
            f"[{name}]({url})" for name, url in data["attachments"]
        )
        embed.add_field(name="Attachments", value=attachment_text[:1024], inline=False)
    embed.set_footer(text=f"#{data['channel']}")
    return embed


@bot.hybrid_command(name="snipe", description="Show the latest deleted message in this channel")
async def snipe(ctx):
    data = sniped_messages.get(ctx.channel.id)
    if data is None:
        return await ctx.send("There is no recently deleted message in this channel.")
    await ctx.send(embed=_snipe_embed(data))


class ModerationConfirmView(discord.ui.View):
    def __init__(self, ctx, action, member, reason):
        super().__init__(timeout=60)
        self.ctx = ctx
        self.action = action
        self.member = member
        self.reason = reason

    async def interaction_check(self, interaction):
        if interaction.user.id == self.ctx.author.id:
            return True
        await send_interaction_v2(interaction, "Only the moderator who started this action can confirm it.", ephemeral=True)
        return False

    async def _finish(self, interaction, accepted):
        if accepted:
            try:
                if self.action == "ban":
                    await self.member.ban(reason=self.reason)
                    result = f"{FP_TICK} **Member Banned**\n\n{ARROW} {self.member.mention} was banned."
                else:
                    await self.member.kick(reason=self.reason)
                    result = f"{FP_TICK} **Member Kicked**\n\n{ARROW} {self.member.mention} was kicked."
            except discord.HTTPException as error:
                result = f"{DOT} **Could not {self.action} {self.member.mention}**\n\n{ARROW} {error}"
        else:
            result = f"{ARROW} {self.action.title()} cancelled for {self.member.mention}."
        await edit_interaction_v2(
            interaction,
            embed=discord.Embed(description=result, color=0xFFDD00),
            view=self,
        )
        self.stop()

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.secondary)
    async def confirm(self, interaction, _button):
        await self._finish(interaction, True)

    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, _button):
        await self._finish(interaction, False)


async def _confirm_member_action(ctx, action, member, reason):
    embed = discord.Embed(
        title=f"Confirm {action.title()}",
        description=f"Are you sure you want to {action} {member.mention}?",
        color=0xFFDD00,
    )
    embed.add_field(name="Reason", value=reason[:1024], inline=False)
    await ctx.send(embed=embed, view=ModerationConfirmView(ctx, action, member, reason))


class BulkModerationConfirmView(discord.ui.View):
    def __init__(self, ctx, action, targets, reason):
        super().__init__(timeout=120)
        self.ctx = ctx
        self.action = action
        self.targets = targets
        self.reason = reason

    async def interaction_check(self, interaction):
        if interaction.user.id == self.ctx.author.id:
            return True
        await send_interaction_v2(
            interaction,
            "Only the administrator who started this action can confirm it.",
            ephemeral=True,
            allow_ephemeral=True,
        )
        return False

    @discord.ui.button(
        label="Confirm",
        style=discord.ButtonStyle.secondary,
        emoji=discord.PartialEmoji.from_str("<:fp_tick:1556939072646287360>"),
    )
    async def confirm(self, interaction, _button):
        for child in self.children:
            child.disabled = True
        await edit_interaction_v2(
            interaction,
            embed=discord.Embed(
                color=0xFFDD00,
                title=f"Bulk {self.action.title()} in progress",
                description=f"{ARROW} Processing **{len(self.targets)}** accounts...",
            ),
            view=self,
        )
        succeeded = failed = 0
        try:
            if self.action == "ban":
                for offset in range(0, len(self.targets), 200):
                    result = await self.ctx.guild.bulk_ban(
                        self.targets[offset:offset + 200],
                        reason=self.reason[:512],
                        delete_message_seconds=0,
                    )
                    succeeded += len(result.banned)
                    failed += len(result.failed)
            else:
                for index, user in enumerate(self.targets, start=1):
                    try:
                        await self.ctx.guild.unban(user, reason=self.reason[:512])
                        succeeded += 1
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        failed += 1
                    if index % 25 == 0:
                        await asyncio.sleep(0)
            result_embed = discord.Embed(
                color=0xFFDD00,
                title=f"{FP_TICK} Bulk {self.action.title()} Complete",
                description=(
                    f"{ARROW} Completed: **{succeeded}**\n"
                    f"{ARROW} Failed or skipped: **{failed}**"
                ),
            )
        except (discord.Forbidden, discord.HTTPException) as error:
            result_embed = discord.Embed(
                color=0xFFDD00,
                title=f"{DOT} Bulk {self.action.title()} incomplete",
                description=f"{ARROW} Completed: **{succeeded}**\n{ARROW} Error: {error}",
            )
        await edit_message_v2(interaction.message, embed=result_embed, view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji=_manifest_emoji("cross", "<:cross:1559374062281826397>"))
    async def cancel(self, interaction, _button):
        for child in self.children:
            child.disabled = True
        await edit_interaction_v2(
            interaction,
            embed=discord.Embed(
                color=0xFFDD00,
                description=f"{ARROW} Bulk {self.action} cancelled.",
            ),
            view=self,
        )
        self.stop()


class BulkRoleRemovalConfirmView(discord.ui.View):
    def __init__(self, ctx, role, targets):
        super().__init__(timeout=120)
        self.ctx = ctx
        self.role = role
        self.targets = targets

    async def interaction_check(self, interaction):
        if interaction.user.id == self.ctx.author.id:
            return True
        await send_interaction_v2(
            interaction,
            "Only the administrator who started this action can confirm it.",
            ephemeral=True,
            allow_ephemeral=True,
        )
        return False

    @discord.ui.button(label="Remove Role", style=discord.ButtonStyle.secondary, emoji=_manifest_emoji("cross", "<:cross:1559374062281826397>"))
    async def confirm(self, interaction, _button):
        for child in self.children:
            child.disabled = True
        await edit_interaction_v2(
            interaction,
            embed=discord.Embed(
                color=0xFFDD00,
                title="Bulk Role Removal in progress",
                description=f"{ARROW} Processing **{len(self.targets)}** members...",
            ),
            view=self,
        )
        removed = failed = 0
        for index, member in enumerate(self.targets, start=1):
            try:
                await member.remove_roles(self.role, reason=f"Bulk role removal by {self.ctx.author}")
                removed += 1
            except (discord.Forbidden, discord.HTTPException):
                failed += 1
            if index % 25 == 0:
                await asyncio.sleep(0)
        result = discord.Embed(
            color=0xFFDD00,
            title=f"{FP_TICK} Bulk Role Removal Complete",
            description=(
                f"{ARROW} Removed {self.role.mention} from **{removed}** member(s).\n"
                f"{ARROW} Failed: **{failed}**"
            ),
        )
        await edit_message_v2(interaction.message, embed=result, view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji=_manifest_emoji("cross", "<:cross:1559374062281826397>"))
    async def cancel(self, interaction, _button):
        for child in self.children:
            child.disabled = True
        await edit_interaction_v2(
            interaction,
            embed=discord.Embed(description=f"{ARROW} Bulk role removal cancelled.", color=0xFFDD00),
            view=self,
        )
        self.stop()


@bot.hybrid_command(name="kick")
@commands.has_permissions(kick_members=True)
async def kick(ctx, *, arguments: str = ""):
    """Kick a member after confirmation; target may be mentioned or replied to."""
    member = await _resolve_context_member(ctx, query=arguments)
    if member is None:
        return await ctx.send("Mention a member or reply to their message to kick them.")
    reason = _remove_target_mention(arguments, member) or "No reason provided"
    await _confirm_member_action(ctx, "kick", member, reason)

@bot.hybrid_command(name="ban")
@commands.has_permissions(ban_members=True)
async def ban(ctx, *, arguments: str = ""):
    """Ban a member after confirmation; target may be mentioned or replied to."""
    member = await _resolve_context_member(ctx, query=arguments)
    if member is None:
        return await ctx.send("Mention a member or reply to their message to ban them.")
    reason = _remove_target_mention(arguments, member) or "No reason provided"
    await _confirm_member_action(ctx, "ban", member, reason)

@bot.hybrid_command(name="unban")
@commands.has_permissions(ban_members=True)
@commands.guild_only()
async def unban(ctx, user_argument: str | None = None):
    if user_argument:
        try:
            user = await UsernameUserConverter().convert(ctx, user_argument)
        except commands.UserNotFound:
            return await ctx.send("<:cross:1559374062281826397> User not found. Use a user mention, ID, or exact username.")
    else:
        user = await _resolve_replied_user(ctx)
        if user is None:
            return await ctx.send("<:cross:1559374062281826397> Mention a user, provide an ID/username, or reply to their message.")
    try:
        await ctx.guild.unban(user)
        embed = discord.Embed(
            color=0xffdd00,
            description=f"{FP_TICK} **Member Unbanned**\n\n{ARROW} {user.name} has been unbanned.",
        )
        await ctx.send(embed=embed)
    except discord.NotFound:
        await ctx.send("<:cross:1559374062281826397> User not found or not banned.")
    except discord.Forbidden:
        await ctx.send("<:cross:1559374062281826397> I need Ban Members permission to unban users.")
    except discord.HTTPException as error:
        await ctx.send(f"<:cross:1559374062281826397> Unban failed: {error}")


@bot.command(name="banall", help="Review and ban all eligible members in a server")
@commands.guild_only()
@commands.has_guild_permissions(administrator=True, ban_members=True, manage_guild=True)
async def banall(ctx, scope: str = "humans", *, reason: str = "Bulk ban requested by administrator"):
    scope = scope.casefold()
    if scope not in {"humans", "bots", "all"}:
        return await ctx.send("Choose a ban scope: `humans`, `bots`, or `all`.")
    bot_member = ctx.guild.me
    if bot_member is None or not (
        bot_member.guild_permissions.ban_members
        and bot_member.guild_permissions.manage_guild
    ):
        return await ctx.send(f"{DOT} I need Ban Members and Manage Server permissions for bulk bans.")
    try:
        if not ctx.guild.chunked:
            await ctx.guild.chunk(cache=True)
    except (discord.Forbidden, discord.HTTPException) as error:
        return await ctx.send(f"{DOT} Could not load the complete member list: {error}")

    excluded_ids = {ctx.guild.owner_id, ctx.author.id, bot_member.id}
    targets = [
        member for member in ctx.guild.members
        if member.id not in excluded_ids
        and not member.guild_permissions.administrator
        and (scope == "all" or member.bot is (scope == "bots"))
    ]
    if not targets:
        return await ctx.send(f"{DOT} No eligible {scope} members found to ban.")
    embed = discord.Embed(
        color=0xFFDD00,
        title="Confirm Bulk Ban",
        description=(
            f"This will ban **{len(targets)}** eligible **{scope}** member(s).\n\n"
            "The guild owner, you, the bot, and Administrators are excluded. "
            "No message history will be deleted."
        ),
    )
    embed.add_field(name="Reason", value=reason[:1024], inline=False)
    await ctx.send(
        embed=embed,
        view=BulkModerationConfirmView(ctx, "ban", targets, reason),
        ephemeral=ctx.interaction is not None,
    )


@bot.command(name="unbanall", aliases=["unball"], help="Review and unban all currently banned accounts")
@commands.guild_only()
@commands.has_guild_permissions(administrator=True, ban_members=True)
async def unbanall(ctx, *, reason: str = "Bulk unban requested by administrator"):
    try:
        targets = [entry.user async for entry in ctx.guild.bans(limit=None)]
    except (discord.Forbidden, discord.HTTPException) as error:
        return await ctx.send(f"{DOT} Could not fetch this server's ban list: {error}")
    if not targets:
        return await ctx.send(f"{DOT} This server has no banned accounts.")
    embed = discord.Embed(
        color=0xFFDD00,
        title="Confirm Bulk Unban",
        description=f"This will unban **{len(targets)}** account(s) from this server.",
    )
    embed.add_field(name="Reason", value=reason[:1024], inline=False)
    await ctx.send(
        embed=embed,
        view=BulkModerationConfirmView(ctx, "unban", targets, reason),
        ephemeral=ctx.interaction is not None,
    )


@bot.command(name="unroleall")
@commands.guild_only()
@commands.has_guild_permissions(administrator=True, manage_roles=True)
async def unroleall(ctx, role: discord.Role, scope: str = "all"):
    scope = scope.casefold()
    if scope not in {"all", "bots", "humans"}:
        return await ctx.send("Choose a role-removal scope: `all`, `bots`, or `humans`.")
    try:
        if not ctx.guild.chunked:
            await ctx.guild.chunk(cache=True)
        bot_member = ctx.guild.me
        if bot_member is None or role.is_default() or role >= bot_member.top_role:
            raise PermissionError("The role must be below Falcon's highest role and cannot be @everyone.")
    except PermissionError as error:
        return await ctx.send(f"{DOT} {error}")
    except (discord.Forbidden, discord.HTTPException) as error:
        return await ctx.send(f"{DOT} Could not load the complete member list: {error}")

    targets = [
        member for member in ctx.guild.members
        if role in member.roles
        and (scope == "all" or member.bot is (scope == "bots"))
    ]
    if not targets:
        return await ctx.send(f"{DOT} No {scope} members currently have {role.mention}.")
    embed = discord.Embed(
        color=0xFFDD00,
        title="Confirm Bulk Role Removal",
        description=f"Remove {role.mention} from **{len(targets)}** {scope} member(s)?",
    )
    await ctx.send(embed=embed, view=BulkRoleRemovalConfirmView(ctx, role, targets))

@bot.hybrid_command(name="mute")
@commands.has_permissions(moderate_members=True)
async def mute(ctx, member: discord.Member, time_str: str = "10m", *, reason: str = "No reason provided"):
    seconds = parse_duration(time_str)
    if not seconds: return await ctx.send("<:cross:1559374062281826397> Invalid time format.")
    until = discord.utils.utcnow() + datetime.timedelta(seconds=seconds)
    await member.timeout(until, reason=reason)
    embed = discord.Embed(color=0xffdd00, description=f"{DOT} **{member.name}** has been muted for `{time_str}`.\n{DOT} Reason: {reason}")
    await ctx.send(embed=embed)

@bot.hybrid_command(name="unmute")
@commands.has_permissions(moderate_members=True)
async def unmute(ctx, member: discord.Member):
    await member.timeout(None)
    embed = discord.Embed(color=0xffdd00, description=f"{DOT} **{member.name}** has been unmuted.")
    await ctx.send(embed=embed)

async def _ensure_jail_role(guild):
    async with bot.db.execute(
        "SELECT jail_role_id, jail_channel_id FROM guild_settings WHERE guild_id=?",
        (guild.id,),
    ) as cursor:
        row = await cursor.fetchone()

    jail_role_id = row[0] if row else None
    jail_channel_id = row[1] if row else None

    jail_role = guild.get_role(jail_role_id) if jail_role_id else None
    if jail_role is None:
        jail_role = discord.utils.get(guild.roles, name="Jail")
    if jail_role is None:
        jail_role = await guild.create_role(name="Jail", reason="Falcon jail role")
    await bot.db.execute(
        "INSERT INTO guild_settings (guild_id, jail_role_id) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET jail_role_id=excluded.jail_role_id",
        (guild.id, jail_role.id),
    )
    await bot.db.commit()

    for channel in guild.channels:
        try:
            overwrites = dict(channel.overwrites)
            if jail_role not in overwrites:
                overwrites[jail_role] = discord.PermissionOverwrite()
            overwrite = overwrites[jail_role]
            overwrite.view_channel = False
            overwrite.send_messages = False
            overwrite.send_messages_in_threads = False
            overwrite.add_reactions = False
            overwrite.speak = False
            overwrite.connect = False
            overwrites[jail_role] = overwrite
            await channel.edit(overwrites=overwrites)
        except (discord.Forbidden, discord.HTTPException):
            pass

    jail_channel = guild.get_channel(jail_channel_id) if jail_channel_id else None
    if jail_channel is None:
        jail_channel = discord.utils.get(guild.text_channels, name="jail")
    if jail_channel is None:
        jail_channel = await guild.create_text_channel("jail", reason="Falcon jail channel")
    try:
        await jail_channel.set_permissions(jail_role, view_channel=True, send_messages=True, send_messages_in_threads=True)
    except (discord.Forbidden, discord.HTTPException):
        pass
    await bot.db.execute(
        "INSERT INTO guild_settings (guild_id, jail_channel_id) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET jail_channel_id=excluded.jail_channel_id",
        (guild.id, jail_channel.id),
    )
    await bot.db.commit()
    return jail_role

async def _restore_jail_roles(guild_id, user_id):
    guild = bot.get_guild(guild_id)
    if guild is None:
        return False
    async with bot.db.execute(
        "SELECT role_ids, jail_role_id FROM jail_members JOIN guild_settings ON guild_settings.guild_id = jail_members.guild_id WHERE jail_members.guild_id=? AND jail_members.user_id=?",
        (guild_id, user_id),
    ) as cursor:
        row = await cursor.fetchone()

    if row is None:
        return False

    saved_role_ids = json.loads(row[0]) if row[0] else []
    jail_role_id = row[1]

    member: discord.Member | None
    try:
        member = await guild.fetch_member(user_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        member = guild.get_member(user_id)
    if member is None:
        await bot.db.execute("DELETE FROM jail_members WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        await bot.db.commit()
        return False

    if jail_role_id:
        jail_role = guild.get_role(jail_role_id)
        if jail_role is not None and jail_role in member.roles:
            await member.remove_roles(jail_role, reason="Unjail")

    valid_roles = []
    for role_id in saved_role_ids:
        role = guild.get_role(role_id)
        if role is not None and role not in member.roles:
            valid_roles.append(role)
    if valid_roles:
        await member.add_roles(*valid_roles, reason="Unjail")

    await bot.db.execute("DELETE FROM jail_members WHERE guild_id=? AND user_id=?", (guild_id, user_id))
    await bot.db.commit()
    return True

async def _schedule_jail_expiration(guild_id, user_id, ends_at):
    if ends_at is None:
        return
    now = datetime.datetime.now(datetime.timezone.utc)
    expiry = datetime.datetime.fromisoformat(ends_at)
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=datetime.timezone.utc)
    delay = (expiry - now).total_seconds()
    if delay <= 0:
        await _restore_jail_roles(guild_id, user_id)
        return

    async def _expire_later():
        try:
            await discord.utils.sleep_until(expiry)
            await _restore_jail_roles(guild_id, user_id)
        except asyncio.CancelledError:
            pass

    asyncio.create_task(_expire_later())

@bot.hybrid_command(name="jail")
@commands.has_permissions(manage_roles=True)
async def jail(ctx, member: discord.Member, duration: str = "p", *, reason: str = "No reason provided"):
    if member.id == ctx.author.id:
        return await ctx.send(f"{DOT} You cannot jail yourself.")
    if member.guild_permissions.administrator and not ctx.author.guild_permissions.administrator:
        return await ctx.send(f"{DOT} I cannot jail an administrator with that role hierarchy.")

    jail_role = await _ensure_jail_role(ctx.guild)
    if jail_role in member.roles:
        return await ctx.send(f"{DOT} {member.mention} is already jailed.")

    try:
        parsed_duration = parse_duration(duration)
    except ValueError as error:
        return await ctx.send(f"{DOT} {error}")

    saved_roles = [role for role in member.roles if role != jail_role and not role.is_default()]
    restored_role_ids = [role.id for role in saved_roles]
    expires_at = None
    if parsed_duration is not None:
        expires_at = (datetime.datetime.now(datetime.timezone.utc) + parsed_duration).isoformat()

    await bot.db.execute(
        "INSERT INTO jail_members (guild_id, user_id, role_ids, until) VALUES (?, ?, ?, ?) ON CONFLICT(guild_id, user_id) DO UPDATE SET role_ids=excluded.role_ids, until=excluded.until",
        (ctx.guild.id, member.id, json.dumps(restored_role_ids), expires_at),
    )
    await bot.db.commit()

    updated_roles = [role for role in member.roles if role not in saved_roles] + [jail_role]
    await member.edit(roles=updated_roles, reason=f"Jail: {reason or 'No reason provided'} by {ctx.author}")
    if expires_at is not None:
        await _schedule_jail_expiration(ctx.guild.id, member.id, expires_at)

    duration_label = "permanent" if parsed_duration is None else str(parsed_duration).replace("days", "d").replace("seconds", "s")
    embed = discord.Embed(color=0xFFDD00, description=f"{DOT} **{member.mention}** has been jailed for **{duration_label}**.\n{DOT} Reason: {reason}")
    await ctx.send(embed=embed)

@bot.hybrid_command(name="unjail")
@commands.has_permissions(manage_roles=True)
async def unjail(ctx, member: discord.Member):
    if member.id == ctx.author.id:
        return await ctx.send(f"{DOT} You cannot unjail yourself from this command.")
    jail_role = await _ensure_jail_role(ctx.guild)
    async with bot.db.execute(
        "SELECT role_ids FROM jail_members WHERE guild_id=? AND user_id=?",
        (ctx.guild.id, member.id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        if jail_role in member.roles:
            await member.remove_roles(jail_role, reason=f"Unjail by {ctx.author}")
            embed = discord.Embed(color=0xFFDD00, description=f"{DOT} **{member.mention}** was removed from the jail role.")
            return await ctx.send(embed=embed)
        return await ctx.send(f"{DOT} {member.mention} is not jailed.")

    await _restore_jail_roles(ctx.guild.id, member.id)
    embed = discord.Embed(color=0xFFDD00, description=f"{DOT} **{member.mention}** has been unjailed.")
    await ctx.send(embed=embed)

@bot.hybrid_command(name="setjail")
@commands.has_permissions(manage_roles=True)
async def setjail(ctx, role: discord.Role):
    await bot.db.execute(
        "INSERT INTO guild_settings (guild_id, jail_role_id) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET jail_role_id=excluded.jail_role_id",
        (ctx.guild.id, role.id),
    )
    await bot.db.commit()
    embed = discord.Embed(color=0xFFDD00, description=f"{DOT} Jail role set to **{role.mention}**.")
    await ctx.send(embed=embed)

@bot.hybrid_command(name="nick", aliases=["nickname"])
@commands.has_permissions(manage_nicknames=True)
async def nick(ctx, *, arguments: str = ""):
    member = await _resolve_context_member(ctx)
    if member is None:
        return await ctx.send("Mention a member or reply to their message, then provide the new nickname.")
    nickname = _remove_target_mention(arguments, member)
    if not nickname:
        return await ctx.send("Usage: `,nick @user <name>` or reply to their message with `,nick <name>`.")
    nickname = nickname[:32]
    try:
        await member.edit(nick=nickname, reason=f"Nickname changed by {ctx.author}")
    except discord.Forbidden:
        return await ctx.send("I cannot change that member's nickname. Check role order and permissions.")
    embed = discord.Embed(
        description=f"<:fp_tick:1556939072646287360> Changed {member.mention}'s nickname to **{nickname}**.",
        color=0xFFDD00,
    )
    await ctx.send(embed=embed)


@bot.hybrid_command(name="purge", aliases=["erase"])
@commands.has_permissions(manage_messages=True)
async def purge(ctx, *, arguments: str = "10"):
    target = await _resolve_context_member(ctx, query=arguments)
    remaining = _remove_target_mention(arguments, target)
    amount_text = remaining.split()[0] if remaining else "10"
    try:
        amount = int(amount_text)
    except ValueError:
        return await ctx.send(f"{ARROW} Usage: `,purge [@user] <amount>` or reply to a user with `,purge <amount>`.")
    if not 1 <= amount <= 1000:
        return await ctx.send(f"{ARROW} Purge amount must be between 1 and 1000.")

    deleted_count = 0
    invocation_id = ctx.message.id if ctx.message else None

    def matches_purge(message):
        nonlocal deleted_count
        if message.id == invocation_id or (target is not None and message.author.id != target.id):
            return False
        if deleted_count >= amount:
            return False
        deleted_count += 1
        return True

    try:
        await ctx.channel.purge(limit=10000, check=matches_purge)
    except discord.Forbidden:
        return await ctx.send(f"{ARROW} I need Manage Messages to purge this channel.")
    except discord.HTTPException as error:
        return await ctx.send(f"{ARROW} Channel purge failed: {error}")
    subject = f" from {target.mention}" if target is not None else ""
    embed = discord.Embed(
        description=f"{ARROW} Deleted **{deleted_count}** messages{subject}.",
        color=0xFFDD00,
    )
    await ctx.send(embed=embed, delete_after=5)


@bot.hybrid_command(name="purgeall")
@commands.has_permissions(manage_messages=True)
async def purgeall(ctx):
    invocation_id = ctx.message.id if ctx.message else None
    try:
        deleted = await ctx.channel.purge(
            limit=None,
            check=lambda message: message.id != invocation_id,
        )
    except discord.Forbidden:
        return await ctx.send(f"{ARROW} I need Manage Messages to purge this channel.")
    except discord.HTTPException as error:
        return await ctx.send(f"{ARROW} Channel purge failed: {error}")
    embed = discord.Embed(
        color=0xFFDD00,
        description=f"{ARROW} Purged **{len(deleted)}** messages from this channel.",
    )
    await ctx.send(embed=embed, delete_after=8)


@bot.hybrid_command(name="purgebot", aliases=["pb"])
@commands.has_permissions(manage_messages=True)
async def purgebot(ctx, amount: int = 5):
    if not 1 <= amount <= 1000:
        return await ctx.send(f"{ARROW} Bot purge amount must be between 1 and 1000.")
    deleted_count = 0
    invocation_id = ctx.message.id if ctx.message else None

    def matches_bot(message):
        nonlocal deleted_count
        if message.id == invocation_id or not message.author.bot or deleted_count >= amount:
            return False
        deleted_count += 1
        return True

    try:
        await ctx.channel.purge(limit=10000, check=matches_bot)
    except discord.Forbidden:
        return await ctx.send(f"{ARROW} I need Manage Messages to purge bot messages.")
    except discord.HTTPException as error:
        return await ctx.send(f"{ARROW} Bot-message purge failed: {error}")
    embed = discord.Embed(
        description=f"{ARROW} Deleted **{deleted_count}** bot messages.",
        color=0xFFDD00,
    )
    await ctx.send(embed=embed, delete_after=5)


# ─── MANAGE COMMANDS ───────────────────────────────────────────────

@bot.command(name="disable")
@commands.has_permissions(manage_guild=True)
async def disable(ctx, mode: str, *, target: str):
    """Disable a command or module. Usage: ,disable command <name> | ,disable module <name>"""
    if mode == "command":
        await bot.db.execute(
            "INSERT OR IGNORE INTO disabled_commands (guild_id, command_name, channel_id) VALUES (?, ?, ?)",
            (ctx.guild.id, target.lower(), 0)
        )
        await bot.db.commit()
        embed = discord.Embed(color=0xffdd00, description=f"{DOT} Command `{target}` has been **disabled** in this server.")
    elif mode == "module":
        valid_modules = set(HELP_SECTIONS) | set(COMMAND_MODULES)
        if target.lower() not in valid_modules:
            return await ctx.send(f"{DOT} Unknown module. Choose from: {', '.join(sorted(valid_modules))}.")
        await bot.db.execute(
            "INSERT OR IGNORE INTO disabled_modules (guild_id, module_name, channel_id) VALUES (?, ?, ?)",
            (ctx.guild.id, target.lower(), 0)
        )
        await bot.db.commit()
        embed = discord.Embed(color=0xffdd00, description=f"{DOT} Module `{target}` has been **disabled** in this server.")
    else:
        embed = discord.Embed(color=0xffdd00, description="<:cross:1559374062281826397> Usage: `,disable command <name>` or `,disable module <name>`")
    await ctx.send(embed=embed)

@bot.command(name="enable")
@commands.has_permissions(manage_guild=True)
async def enable(ctx, mode: str, *, target: str):
    """Enable a command or module. Usage: ,enable command <name> | ,enable module <name>"""
    if mode == "command":
        await bot.db.execute("DELETE FROM disabled_commands WHERE guild_id=? AND command_name=?", (ctx.guild.id, target.lower()))
        await bot.db.commit()
        embed = discord.Embed(color=0xffdd00, description=f"{DOT} Command `{target}` has been **enabled**.")
    elif mode == "module":
        await bot.db.execute("DELETE FROM disabled_modules WHERE guild_id=? AND module_name=?", (ctx.guild.id, target.lower()))
        await bot.db.commit()
        embed = discord.Embed(color=0xffdd00, description=f"{DOT} Module `{target}` has been **enabled**.")
    else:
        embed = discord.Embed(color=0xffdd00, description="<:cross:1559374062281826397> Usage: `,enable command <name>` or `,enable module <name>`")
    await ctx.send(embed=embed)

# ─── POLLS ─────────────────────────────────────────────────────────

@bot.command(name="createpoll")
@commands.has_permissions(manage_guild=True)
async def createpoll(ctx, *, args: str):
    """Create a poll. Usage: ,createpoll Question | Option1 | Option2 | Option3"""
    parts = [p.strip() for p in args.split("|")]
    if len(parts) < 2:
        return await ctx.send("<:cross:1559374062281826397> Usage: `,createpoll Question | Option1 | Option2`")
    question = parts[0]
    options = parts[1:]
    emojis = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    desc = "\n".join([f"{emojis[i]} {opt}" for i, opt in enumerate(options[:10])])
    embed = discord.Embed(color=0xffdd00, title=f"📊 {question}", description=desc)
    embed.set_footer(text=f"Poll by {ctx.author.name}", icon_url=ctx.author.display_avatar.url)
    msg = await ctx.send(embed=embed)
    for i in range(min(len(options), 10)):
        await msg.add_reaction(emojis[i])
    import json
    await bot.db.execute(
        "INSERT INTO polls (msg_id, question, options, channel_id, active) VALUES (?, ?, ?, ?, 1)",
        (msg.id, question, json.dumps(options), ctx.channel.id)
    )
    await bot.db.commit()

@bot.command(name="epoll")
@commands.has_permissions(manage_guild=True)
async def epoll(ctx):
    """Ends an active poll. Reply to the poll message."""
    if not ctx.message.reference:
        return await ctx.send("<:cross:1559374062281826397> Reply to a poll message to end it.")
    msg_id = ctx.message.reference.message_id
    async with bot.db.execute("SELECT question, options, channel_id FROM polls WHERE msg_id=? AND active=1", (msg_id,)) as cur:
        row = await cur.fetchone()
    if not row:
        return await ctx.send("<:cross:1559374062281826397> No active poll found.")
    import json
    question, options_json, ch_id = row
    options = json.loads(options_json)
    channel = ctx.guild.get_channel(ch_id)
    try:
        poll_msg = await channel.fetch_message(msg_id)
    except:
        return await ctx.send("<:cross:1559374062281826397> Could not fetch poll message.")
    emojis = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    results = []
    for i, opt in enumerate(options[:10]):
        for r in poll_msg.reactions:
            if str(r.emoji) == emojis[i]:
                results.append((opt, r.count - 1))
                break
        else:
            results.append((opt, 0))
    results.sort(key=lambda x: x[1], reverse=True)
    desc = "\n".join([f"{DOT} **{opt}** — {votes} vote(s)" for opt, votes in results])
    embed = discord.Embed(color=0xffdd00, title=f"📊 Poll Ended: {question}", description=desc)
    embed.set_footer(text="Poll ended")
    await edit_message_v2(poll_msg, embed=embed)
    await bot.db.execute("UPDATE polls SET active=0 WHERE msg_id=?", (msg_id,))
    await bot.db.commit()
    await ctx.send(embed=discord.Embed(color=0xffdd00, description=f"{DOT} Poll ended successfully."), delete_after=5)

# ─── UTILITY ───────────────────────────────────────────────────────

@bot.command(name="setprefix")
@commands.has_guild_permissions(manage_guild=True)
async def setprefix(ctx, *, prefix: str):
    """Set this server's message-command prefix (1-5 non-space characters)."""
    prefix = prefix.strip()
    if not prefix or len(prefix) > 5 or any(character.isspace() for character in prefix):
        return await ctx.send("Choose a prefix containing 1-5 non-space characters.")
    if prefix.startswith("<@"):
        return await ctx.send("A member mention cannot be used as the server prefix.")
    await bot.db.execute(
        "INSERT INTO guild_prefixes (guild_id, prefix) VALUES (?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET prefix=excluded.prefix",
        (ctx.guild.id, prefix),
    )
    await bot.db.commit()
    await ctx.send(
        f"Server prefix set to `{prefix}`. The default `{BOT_PREFIX}` prefix will also continue to work."
    )


@bot.command(name="deleteprefix")
@commands.has_guild_permissions(manage_guild=True)
async def deleteprefix(ctx):
    """Restore the bot's default message-command prefix for this server."""
    await bot.db.execute(
        "DELETE FROM guild_prefixes WHERE guild_id = ?",
        (ctx.guild.id,),
    )
    await bot.db.commit()
    await ctx.send(f"Server prefix removed. Use `{BOT_PREFIX}` again.")


@bot.command(name="ping", aliases=["latency", "ms", "pong"])
async def ping(ctx):
    import resource

    from community_features import render_ping_card

    loop = asyncio.get_running_loop()
    request_started = loop.time()
    message = await send_v2(ctx, "Measuring Falcon's live connection...")
    api_ms = (loop.time() - request_started) * 1000
    database_started = loop.time()
    async with bot.db.execute("SELECT 1") as cursor:
        await cursor.fetchone()
    database_ms = (loop.time() - database_started) * 1000

    uptime = datetime.datetime.now(datetime.timezone.utc) - BOT_STARTED_AT
    uptime_text = str(uptime).split(".")[0]
    memory_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    python_version = __import__("platform").python_version()
    shard_count = getattr(bot, "shard_count", None) or len(getattr(bot, "shards", {})) or 1
    shard_id = getattr(ctx.guild, "shard_id", None)
    server_count, member_count = await _cluster_totals()
    bot_name = bot.user.name if bot.user else "Falcon"
    bot_id = getattr(bot.user, "id", None)
    command_count = len(bot.commands)
    bot_icon_bytes = None
    if bot.user:
        try:
            bot_icon_bytes = (
                await bot.user.display_avatar.with_format("png").with_size(128).read()
            )
        except (discord.HTTPException, OSError, asyncio.TimeoutError) as error:
            logging.getLogger(__name__).warning(
                "Failed to download bot icon for ping card: %s", error
            )
    card_bytes = await asyncio.to_thread(
        render_ping_card,
        round(bot.latency * 1000),
        api_ms,
        shard_id,
        shard_count,
        server_count,
        member_count,
        uptime_text,
        database_ms,
        bot_icon_bytes=bot_icon_bytes,
        bot_name=bot_name,
        bot_id=bot_id,
        command_count=command_count,
        memory_mb=memory_mb,
        python_version=python_version,
    )
    card = discord.File(io.BytesIO(card_bytes), filename="falcon-ping.png")
    embed = discord.Embed(color=0xFFDD00)
    embed.set_image(url="attachment://falcon-ping.png")
    await edit_message_v2(message, embed=embed, attachments=[card])


@bot.hybrid_command(name="tagreply", description="Ping a member or the author of a replied message")
async def tagreply(ctx, member: discord.Member = None):
    member = await _resolve_context_member(ctx, member)
    if member is None:
        return await ctx.send("Mention a member or reply to their message to ping them.")
    await ctx.send(
        member.mention,
        allowed_mentions=discord.AllowedMentions(
            users=[member],
            roles=False,
            everyone=False,
            replied_user=False,
        ),
    )


@bot.command(name="serverinfo", aliases=["si"])
async def serverinfo(ctx):
    g = ctx.guild
    text_channels = sum(isinstance(channel, discord.TextChannel) for channel in g.channels)
    voice_channels = sum(isinstance(channel, discord.VoiceChannel) for channel in g.channels)
    categories = sum(isinstance(channel, discord.CategoryChannel) for channel in g.channels)
    owner = g.owner.mention if g.owner else f"<@{g.owner_id}>"
    verification = str(g.verification_level).replace("VerificationLevel.", "").lower()
    afk_timeout = f"{g.afk_timeout // 60} mins"
    afk_channel = g.afk_channel.mention if g.afk_channel else "None"
    embed = discord.Embed(
        color=BOT_COLOR,
        title=f"Server Information - {g.name}",
        description=g.description or "No server description.",
    )
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    embed.add_field(
        name=f"{E_NEWS} General Info",
        value=(
            f"**ID:** `{g.id}`\n"
            f"**Owner:** {owner}\n"
            f"**Created:** <t:{int(g.created_at.timestamp())}:R> (<t:{int(g.created_at.timestamp())}:F>)\n"
            f"**Description:** {g.description or 'None'}"
        ),
        inline=False,
    )
    embed.add_field(
        name="👥 Members & Roles",
        value=(
            f"**Members:** `{g.member_count or 0}`\n"
            f"**Roles:** `{max(len(g.roles) - 1, 0)}`\n"
            f"**Emojis:** `{len(g.emojis)}`\n"
            f"**Verification Level:** `{verification}`"
        ),
        inline=True,
    )
    embed.add_field(
        name="📺 Channels",
        value=(
            f"**Text:** `{text_channels}`\n"
            f"**Voice:** `{voice_channels}`\n"
            f"**Categories:** `{categories}`\n"
            f"**AFK Timeout:** `{afk_timeout}`\n"
            f"**AFK Channel:** {afk_channel}"
        ),
        inline=True,
    )
    embed.add_field(
        name="🚀 Boost Info",
        value=(
            f"**Boosts:** `{g.premium_subscription_count or 0}`\n"
            f"**Boost Level:** `{g.premium_tier}`"
        ),
        inline=False,
    )
    embed.set_footer(text=f"Requested by {ctx.author} • {g.name}")
    await ctx.send(
        embed=embed,
        allowed_mentions=discord.AllowedMentions.none(),
    )

@bot.hybrid_command(name="userinfo", aliases=["ui"])
async def userinfo(ctx, user: discord.Member = None):
    if ctx.guild is None:
        return await ctx.send("Use this command in a server.")
    user = await _resolve_context_member(ctx, user) or ctx.author
    created = user.created_at
    created_text = (
        f"{discord.utils.format_dt(created, 'R')} "
        f"({created.strftime('%B')} {created.day}, {created.year} at {created.strftime('%I:%M %p').lstrip('0')})"
    )
    joined_text = "Not in this server"
    if user.joined_at:
        joined = user.joined_at
        joined_text = (
            f"{discord.utils.format_dt(joined, 'R')} "
            f"({joined.strftime('%B')} {joined.day}, {joined.year} at {joined.strftime('%I:%M %p').lstrip('0')})"
        )
    roles = [role for role in user.roles if not role.is_default()]
    role_text = " ".join(role.mention for role in reversed(roles)) or "No roles"
    if len(role_text) > 1024:
        role_text = role_text[:1000].rsplit(" ", 1)[0] + " …"
    embed = discord.Embed(color=0xffdd00, title=f"User Info - {user.display_name}")
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(
        name="General Info",
        value=(
            f"**ID:** `{user.id}`\n**Name:** `{user.name}`\n"
            f"**Display Name:** `{user.display_name}`\n**Created:** {created_text}\n"
            f"**Bot:** {'Yes' if user.bot else 'No'}"
        ),
        inline=False,
    )
    embed.add_field(
        name="Server Info",
        value=(
            f"**Joined {ctx.guild.name}:** {joined_text}\n"
            f"**Boosted Server:** {'Yes' if user.premium_since else 'No'}"
        ),
        inline=False,
    )
    embed.add_field(name=f"Roles [{len(roles)}]", value=role_text, inline=False)
    embed.add_field(name="Top Role", value=user.top_role.mention if roles else "@everyone", inline=False)
    embed.set_footer(text=f"Requested by {ctx.author} • Today at {datetime.datetime.now().strftime('%I:%M %p')}")
    await ctx.send(embed=embed)

@bot.hybrid_command(name="avatar", aliases=["av", "pfp"])
async def avatar(ctx, user: discord.Member = None):
    user = await _resolve_context_member(ctx, user) or ctx.author
    embed = discord.Embed(color=0xffdd00, title=f"{user.name}'s Avatar")
    embed.set_image(url=user.display_avatar.url)
    await ctx.send(embed=embed)

@bot.command(name="banner", aliases=["userbanner"])
async def banner(ctx, user: discord.Member = None):
    user = user or ctx.author
    fetched = await bot.fetch_user(user.id)
    if not fetched.banner:
        return await ctx.send(f"{DOT} {user.mention} does not have a banner.")
    embed = discord.Embed(color=BOT_COLOR, title=f"{user.name}'s Banner")
    embed.set_image(url=fetched.banner.url)
    await ctx.send(embed=embed)

@bot.command(name="guildbanner", aliases=["serverbanner", "gbanner"])
async def guildbanner(ctx):
    if not ctx.guild.banner:
        return await ctx.send(f"{DOT} This server does not have a banner.")
    embed = discord.Embed(color=BOT_COLOR, title=f"{ctx.guild.name}'s Banner")
    embed.set_image(url=ctx.guild.banner.url)
    await ctx.send(embed=embed)

@bot.command(name="roleinfo", aliases=["ri"])
async def roleinfo(ctx, role: discord.Role):
    permissions = [name.replace("_", " ").title() for name, enabled in role.permissions if enabled]
    embed = discord.Embed(color=BOT_COLOR, title=f"Role Information - {role.name}")
    embed.add_field(name="General", value=f"**ID:** `{role.id}`\n**Mention:** {role.mention}\n**Position:** `{role.position}`\n**Color:** `{role.colour}`", inline=False)
    embed.add_field(name="Members", value=f"`{len(role.members)}`", inline=True)
    embed.add_field(name="Permissions", value=", ".join(permissions[:20]) or "None", inline=False)
    await ctx.send(embed=embed)

@bot.command(name="vcinfo", aliases=["voiceinfo"])
async def vcinfo(ctx, channel: discord.VoiceChannel | None = None):
    selected_channel = channel or (
        ctx.author.voice.channel if ctx.author.voice else None
    )
    if not selected_channel:
        return await ctx.send(f"{DOT} Mention a voice channel or join one first.")
    embed = discord.Embed(
        color=BOT_COLOR,
        title=f"Voice Channel Information - {selected_channel.name}",
    )
    embed.description = (
        f"**ID:** `{selected_channel.id}`\n"
        f"**Category:** {selected_channel.category.mention if selected_channel.category else 'None'}\n"
        f"**Bitrate:** `{selected_channel.bitrate // 1000} kbps`\n"
        f"**User Limit:** `{selected_channel.user_limit or 'Unlimited'}`\n"
        f"**Members:** `{len(selected_channel.members)}`"
    )
    await ctx.send(embed=embed)

@bot.command(name="support", aliases=["help-server", "discord", "server"])
async def support(ctx):
    url = os.getenv("SUPPORT_SERVER", "").strip() or "#"
    await ctx.send(embed=discord.Embed(color=BOT_COLOR, title="Falcon Support Server", description=f"Join the support server: [Click here]({url})"))

@bot.command(name="mc", aliases=["membercount"])
async def membercount(ctx):
    humans = sum(not member.bot for member in ctx.guild.members)
    bots = sum(member.bot for member in ctx.guild.members)
    await ctx.send(embed=discord.Embed(color=BOT_COLOR, title=f"Member Count - {ctx.guild.name}", description=f"**Total:** `{ctx.guild.member_count or 0}`\n**Humans:** `{humans}`\n**Bots:** `{bots}`"))

@bot.command(name="stats", aliases=["botstats", "status"])
async def stats(ctx):
    import resource
    memory_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    uptime = datetime.datetime.now(datetime.timezone.utc) - BOT_STARTED_AT
    server_count, member_count = await _cluster_totals()
    embed = discord.Embed(color=BOT_COLOR, title="Falcon Bot Statistics")
    embed.description = f"**Servers:** `{server_count}`\n**Users:** `{member_count}`\n**Latency:** `{round(bot.latency * 1000)}ms`\n**Uptime:** `{str(uptime).split('.')[0]}`\n**Memory:** `{memory_mb:.1f} MB`\n**Python:** `{__import__('platform').python_version()}`"
    await ctx.send(embed=embed)

@bot.command(name="perms", aliases=["botperms"])
async def perms(ctx):
    me = ctx.guild.me
    required = ["view_channel", "send_messages", "embed_links", "read_message_history", "manage_messages", "manage_roles", "manage_channels", "manage_emojis_and_stickers", "view_audit_log"]
    lines = [f"{'✅' if getattr(me.guild_permissions, permission, False) else '<:cross:1559374062281826397>'} `{permission.replace('_', ' ').title()}`" for permission in required]
    await ctx.send(embed=discord.Embed(color=BOT_COLOR, title="Falcon Permissions", description="\n".join(lines)))

@bot.command(name="accage", aliases=["age", "accountage"])
async def accage(ctx, user: discord.Member = None):
    from dateutil.relativedelta import relativedelta

    user = user or ctx.author
    created = user.created_at
    age = relativedelta(datetime.datetime.now(datetime.timezone.utc), created)
    weeks, days = divmod(age.days, 7)
    age_text = (
        f"{age.years} years, {age.months} months, {weeks} weeks, {days} days, "
        f"{age.hours} hours, {age.minutes} minutes and {age.seconds} seconds"
    )
    embed = discord.Embed(
        color=BOT_COLOR,
        title=f"{user.name}'s account age",
        description=age_text,
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    await ctx.send(embed=embed)

@bot.command(name="sponsor", aliases=["sponsors"])
async def sponsor(ctx):
    await ctx.send(embed=discord.Embed(color=BOT_COLOR, title="Falcon Sponsors", description=os.getenv("SPONSOR_TEXT", "No sponsors have been configured yet.")))

@bot.command(name="uptime")
async def uptime(ctx):
    elapsed = datetime.datetime.now(datetime.timezone.utc) - BOT_STARTED_AT
    await ctx.send(embed=discord.Embed(color=BOT_COLOR, description=f"{DOT} Bot uptime: **{str(elapsed).split('.')[0]}**"))

@bot.command(name="botinfo", aliases=["bi"])
async def botinfo(ctx):
    server_count, member_count = await _cluster_totals()
    embed = discord.Embed(color=BOT_COLOR, title="Falcon Premium")
    embed.description = f"**Servers:** `{server_count}`\n**Users:** `{member_count}`\n**Commands:** `{len(bot.commands)}`\n**Latency:** `{round(bot.latency * 1000)}ms`\n**Uptime:** `{str(datetime.datetime.now(datetime.timezone.utc) - BOT_STARTED_AT).split('.')[0]}`"
    if bot.user:
        embed.set_thumbnail(url=bot.user.display_avatar.url)
    await ctx.send(embed=embed)

@bot.command(name="timedifference")
async def timedifference(ctx, first_id: int, second_id: int):
    first = discord.utils.snowflake_time(first_id)
    second = discord.utils.snowflake_time(second_id)
    difference = abs(first - second)
    await ctx.send(embed=discord.Embed(color=BOT_COLOR, title="Time Difference", description=f"`{difference}` between <t:{int(first.timestamp())}:F> and <t:{int(second.timestamp())}:F>."))

@bot.command(name="nuke")
@commands.has_permissions(manage_channels=True)
async def nuke(ctx):
    embed = discord.Embed(
        color=0xffdd00,
        title="Channel Nuke Warning",
        description=(
            f"{ARROW} Click **Yes** to replace this channel or **No** to cancel.\n\n"
            f"{ARROW} This action cannot be reversed."
        ),
    )

    class NukeConfirmationView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)
            self.confirmed = False

        async def interaction_check(self, interaction: discord.Interaction):
            if interaction.user.id != ctx.author.id:
                await send_interaction_v2(
                    interaction,
                    "Only the user who started this command can choose an option.",
                    ephemeral=True,
                )
                return False
            return True

        @discord.ui.button(label="Yes", style=discord.ButtonStyle.secondary)
        async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
            self.confirmed = True
            for child in self.children:
                child.disabled = True
            await edit_interaction_v2(interaction, content=f"{ARROW} Nuking channel...", view=self)

            channel = ctx.channel
            try:
                new_channel = await channel.clone(reason=f"Nuked by {ctx.author}")
                await new_channel.edit(position=channel.position)
                await channel.delete(reason=f"Nuked by {ctx.author}")
                await send_v2(new_channel, f"{FP_TICK} This channel was nuked by {ctx.author.display_name}.")
            except discord.Forbidden:
                await send_v2(interaction.followup,
                    f"{ARROW} I do not have enough permissions to nuke this channel.",
                    ephemeral=True,
                )
            except discord.HTTPException as error:
                await send_v2(interaction.followup, f"{ARROW} Channel nuke failed: {error}", ephemeral=True)

        @discord.ui.button(label="No", style=discord.ButtonStyle.secondary)
        async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
            for child in self.children:
                child.disabled = True
            await edit_interaction_v2(
                interaction,
                content=f"{ARROW} Action cancelled.",
                embed=None,
                view=self,
            )
            self.stop()

        async def on_timeout(self):
            if not self.confirmed:
                for child in self.children:
                    child.disabled = True

    await ctx.send(embed=embed, view=NukeConfirmationView())


class ChannelDeleteConfirmationView(discord.ui.View):
    def __init__(self, ctx, channel, reason):
        super().__init__(timeout=60)
        self.ctx = ctx
        self.channels = [channel] if channel is not None else []
        self.reason = reason
        self.channel_select = discord.ui.ChannelSelect(
            placeholder="Choose channel(s) to delete",
            min_values=1,
            max_values=25,
            row=0,
        )
        self.channel_select.callback = self.select_channel
        self.add_item(self.channel_select)
        self._set_delete_disabled(channel is None)

    def _set_delete_disabled(self, disabled):
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.label == "Delete Channels":
                item.disabled = disabled

    def _prompt_embed(self):
        selected = "\n".join(channel.mention for channel in self.channels)
        selected = selected or "No channels selected"
        return discord.Embed(
            color=0xFFDD00,
            title="Confirm Channel Deletion",
            description=(
                f"Choose one or more channels from the menu, then click **Delete Channels**.\n\n"
                f"**Selected:** {selected}\n"
                f"**Reason:** {discord.utils.escape_markdown(self.reason[:500], as_needed=True)}"
            ),
        )

    async def interaction_check(self, interaction):
        if interaction.user.id == self.ctx.author.id:
            return True
        await send_interaction_v2(
            interaction,
            "Only the administrator who started this deletion can confirm it.",
            ephemeral=True,
            allow_ephemeral=True,
        )
        return False

    async def select_channel(self, interaction):
        self.channels = []
        for selected in self.channel_select.values:
            channel_id = getattr(selected, "id", None)
            if channel_id is None:
                continue
            channel = self.ctx.guild.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.ctx.guild.fetch_channel(channel_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    continue
            if hasattr(channel, "delete"):
                self.channels.append(channel)
        self._set_delete_disabled(not self.channels)
        await edit_interaction_v2(interaction, embed=self._prompt_embed(), view=self)

    @discord.ui.button(
        label="Delete Channels",
        style=discord.ButtonStyle.secondary,
        emoji=_manifest_emoji("delete", "<:delete:1559373984636739644>"),
        row=1,
    )
    async def confirm(self, interaction, _button):
        if not self.channels:
            return await send_interaction_v2(
                interaction,
                "Choose at least one channel before deleting.",
                ephemeral=True,
                allow_ephemeral=True,
            )
        for item in self.children:
            item.disabled = True
        await interaction.response.defer()
        deleted = []
        failures = []
        for channel in self.channels:
            try:
                await channel.delete(
                    reason=f"Deleted by {self.ctx.author}: {self.reason}"[:512]
                )
                deleted.append(f"#{channel.name}")
            except discord.HTTPException as error:
                failures.append(f"#{channel.name}: {error}")
        result_lines = []
        if deleted:
            result_lines.append(f"{FP_TICK} Deleted: {', '.join(deleted)}")
        if failures:
            result_lines.append(f"{DOT} Could not delete: {'; '.join(failures)}")
        result = "\n".join(result_lines)
        try:
            await edit_message_v2(interaction.message, content=result)
        except discord.HTTPException:
            try:
                await interaction.followup.send(result, ephemeral=True)
            except discord.HTTPException as error:
                print(f"[channel-delete] Could not post completion status: {error}")
        self.stop()

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.secondary,
        emoji=_manifest_emoji("cross", "<:cross:1559374062281826397>"),
        row=1,
    )
    async def cancel(self, interaction, _button):
        for item in self.children:
            item.disabled = True
        await edit_interaction_v2(
            interaction,
            content=f"{ARROW} Channel deletion cancelled.",
            embed=None,
            view=self,
        )
        self.stop()


def _channel_overwrite_flags(channel):
    channel_type = getattr(channel, "type", None)
    if channel_type == discord.ChannelType.category:
        return ("send_messages", "connect")
    if channel_type in {
        discord.ChannelType.text,
        discord.ChannelType.news,
        discord.ChannelType.forum,
        discord.ChannelType.media,
    }:
        return ("send_messages",)
    if channel_type in {discord.ChannelType.voice, discord.ChannelType.stage_voice}:
        return ("connect",)
    return None


async def _set_channel_access_state(ctx, channel, action):
    if channel.guild.id != ctx.guild.id:
        return await ctx.send(f"{DOT} Choose a channel from this server.")

    if isinstance(channel, discord.Thread):
        if action in {"hide", "unhide"}:
            return await ctx.send(
                f"{DOT} Discord doesn't support hiding one thread independently; use the parent channel."
            )
        try:
            await channel.edit(
                locked=action == "lock",
                reason=f"{action.title()}ed by {ctx.author} ({ctx.author.id})",
            )
        except discord.Forbidden:
            return await ctx.send(f"{DOT} I need Manage Threads to change this thread's lock state.")
        except discord.HTTPException as error:
            logging.getLogger(__name__).warning("Could not update thread %s: %s", channel.id, error)
            return await ctx.send(f"{DOT} Discord couldn't update that thread.")
        return await ctx.send(f"{ARROW} {channel.mention} {action}ed.")

    flags = _channel_overwrite_flags(channel)
    if flags is None:
        return await ctx.send(f"{DOT} That channel type doesn't support lock or visibility overwrites.")

    overwrite = channel.overwrites_for(ctx.guild.default_role)
    if action in {"hide", "unhide"}:
        overwrite.view_channel = False if action == "hide" else None
    else:
        for flag in flags:
            setattr(overwrite, flag, False if action == "lock" else None)

    try:
        await channel.set_permissions(
            ctx.guild.default_role,
            overwrite=None if overwrite.is_empty() else overwrite,
            reason=f"Channel {action} by {ctx.author} ({ctx.author.id})",
        )
    except discord.Forbidden:
        return await ctx.send(f"{DOT} I need Manage Channels permission for {channel.mention}.")
    except discord.HTTPException as error:
        logging.getLogger(__name__).warning("Could not %s channel %s: %s", action, channel.id, error)
        return await ctx.send(f"{DOT} Discord couldn't {action} {channel.mention}.")

    return await ctx.send(f"{ARROW} {channel.mention} {action}ed.")


@bot.command(name="lock", help="Lock a channel or the current channel")
@commands.guild_only()
@commands.has_guild_permissions(manage_channels=True)
async def lock_channel(ctx, channel: discord.abc.GuildChannel = None):
    await _set_channel_access_state(ctx, channel or ctx.channel, "lock")


@bot.command(name="unlock", help="Unlock a channel or the current channel")
@commands.guild_only()
@commands.has_guild_permissions(manage_channels=True)
async def unlock_channel(ctx, channel: discord.abc.GuildChannel = None):
    await _set_channel_access_state(ctx, channel or ctx.channel, "unlock")


@bot.command(name="hide", help="Hide a channel from @everyone")
@commands.guild_only()
@commands.has_guild_permissions(manage_channels=True)
async def hide_channel(ctx, channel: discord.abc.GuildChannel = None):
    await _set_channel_access_state(ctx, channel or ctx.channel, "hide")


@bot.command(name="unhide", help="Restore a channel's inherited visibility")
@commands.guild_only()
@commands.has_guild_permissions(manage_channels=True)
async def unhide_channel(ctx, channel: discord.abc.GuildChannel = None):
    await _set_channel_access_state(ctx, channel or ctx.channel, "unhide")


@bot.group(name="channel", invoke_without_command=True)
@commands.guild_only()
async def channel_group(ctx):
    await ctx.send(
        f"{DOT} Usage: `,channel delete [#channel] [reason]`. "
        "Omit the channel to choose one or more channels from the dropdown."
    )


@channel_group.command(name="delete")
async def channel_delete(ctx, channel: discord.TextChannel = None, *, reason: str = "No reason provided"):
    if ctx.author.id != ctx.guild.owner_id and not ctx.author.guild_permissions.administrator:
        return await ctx.send(f"{DOT} Only the server owner or an Administrator can delete channels.")
    bot_member = ctx.guild.me
    if bot_member is None or not bot_member.guild_permissions.manage_channels:
        return await ctx.send(f"{DOT} I need Manage Channels permission to delete channels.")
    view = ChannelDeleteConfirmationView(ctx, channel, reason)
    await ctx.send(embed=view._prompt_embed(), view=view)


@bot.command(name="editfalconpremium")
@commands.has_permissions(manage_guild=True)
async def editfalconpremium(ctx, action: str | None = None, *, value: str | None = None):
    success_tick = "<:fp_tick:1556939072646287360>"
    if not action:
        class EditFalconModal(discord.ui.Modal):
            def __init__(self, edit_action):
                self.edit_action = edit_action
                labels = {
                    "name": ("Bot Nickname", "Enter the bot nickname for this server"),
                    "avatar": ("Bot Guild Avatar URL", "Paste the bot avatar image URL for this server"),
                    "banner": ("Bot Guild Banner URL", "Paste the bot banner image URL for this server"),
                    "bio": ("Bot Guild Bio", "Enter the bot bio for this server"),
                }
                label, placeholder = labels[edit_action]
                super().__init__(title=f"Edit Server {edit_action.title()}")
                self.value = discord.ui.TextInput(
                    label=label,
                    placeholder=placeholder,
                    required=True,
                    style=discord.TextStyle.paragraph if edit_action == "bio" else discord.TextStyle.short,
                    max_length=4000 if edit_action == "bio" else 1000,
                )
                self.add_item(self.value)

            async def on_submit(self, interaction: discord.Interaction):
                submitted = self.value.value.strip()
                try:
                    bot_member = ctx.guild.me
                    if bot_member is None:
                        return await send_interaction_v2(interaction, f"{DOT} Bot member is unavailable.", ephemeral=True)
                    if self.edit_action == "remove_avatar":
                        await bot_member.edit(avatar=None, reason=f"Edited by {interaction.user}")
                        success_message = "Bot guild avatar removed successfully."
                    elif self.edit_action == "remove_banner":
                        await bot_member.edit(banner=None, reason=f"Edited by {interaction.user}")
                        success_message = "Bot guild banner removed successfully."
                    elif self.edit_action == "remove_bio":
                        await bot_member.edit(bio=None, reason=f"Edited by {interaction.user}")
                        success_message = "Bot guild bio removed successfully."
                    elif self.edit_action == "remove_name":
                        await bot_member.edit(nick=None, reason=f"Edited by {interaction.user}")
                        success_message = "Bot guild name reset to default successfully."
                    elif self.edit_action == "name":
                        await bot_member.edit(nick=submitted or None, reason=f"Edited by {interaction.user}")
                        success_message = "Bot guild nickname updated successfully."
                    elif self.edit_action == "bio":
                        await bot_member.edit(bio=submitted, reason=f"Edited by {interaction.user}")
                        success_message = "Bot guild bio updated successfully."
                    else:
                        async with aiohttp.ClientSession() as session:
                            async with session.get(submitted) as response:
                                if response.status != 200:
                                    return await send_interaction_v2(
                                        interaction,
                                        f"{DOT} Could not download the image (HTTP {response.status}).",
                                        ephemeral=True,
                                    )
                                image = await response.read()
                        await bot_member.edit(**{self.edit_action: image}, reason=f"Edited by {interaction.user}")
                        success_message = f"Bot guild {self.edit_action} updated successfully."
                    await send_interaction_v2(
                        interaction,
                        f"{success_tick} {success_message}",
                        ephemeral=True,
                    )
                except discord.Forbidden:
                    await send_interaction_v2(
                        interaction,
                        f"{DOT} I do not have permission to edit the server.",
                        ephemeral=True,
                    )
                except discord.HTTPException as error:
                    await send_interaction_v2(interaction, f"{DOT} Update failed: {error}", ephemeral=True)

        class EditFalconView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=180)

            async def clear_setting(self, interaction, setting):
                try:
                    bot_member = ctx.guild.me
                    if bot_member is None:
                        return await send_interaction_v2(interaction, f"{DOT} Bot member is unavailable.", ephemeral=True)
                    if setting == "avatar":
                        await bot_member.edit(avatar=None, reason=f"Edited by {interaction.user}")
                        message = "Bot guild avatar removed successfully."
                    elif setting == "banner":
                        await bot_member.edit(banner=None, reason=f"Edited by {interaction.user}")
                        message = "Bot guild banner removed successfully."
                    elif setting == "name":
                        await bot_member.edit(nick=None, reason=f"Edited by {interaction.user}")
                        message = "Bot guild name reset to default successfully."
                    else:
                        await bot_member.edit(bio=None, reason=f"Edited by {interaction.user}")
                        message = "Bot guild bio removed successfully."
                    await send_interaction_v2(
                        interaction,
                        f"{success_tick} {message}",
                        ephemeral=True,
                    )
                except discord.Forbidden:
                    await send_interaction_v2(interaction, f"{DOT} I do not have permission to make that change.", ephemeral=True)
                except discord.HTTPException as error:
                    await send_interaction_v2(interaction, f"{DOT} Remove failed: {error}", ephemeral=True)

            async def interaction_check(self, interaction: discord.Interaction):
                if interaction.user.id != ctx.author.id:
                    await send_interaction_v2(
                        interaction,
                        "Only the user who opened this panel can use it.",
                        ephemeral=True,
                    )
                    return False
                return True

            @discord.ui.button(label="Avatar", style=discord.ButtonStyle.secondary)
            async def avatar_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                await interaction.response.send_modal(EditFalconModal("avatar"))

            @discord.ui.button(label="Banner", style=discord.ButtonStyle.secondary)
            async def banner_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                await interaction.response.send_modal(EditFalconModal("banner"))

            @discord.ui.button(label="Bio", style=discord.ButtonStyle.secondary)
            async def bio_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                await interaction.response.send_modal(EditFalconModal("bio"))

            @discord.ui.button(label="Name", style=discord.ButtonStyle.secondary)
            async def name_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                await interaction.response.send_modal(EditFalconModal("name"))

            @discord.ui.button(label="Remove Avatar", style=discord.ButtonStyle.secondary)
            async def remove_avatar_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                await self.clear_setting(interaction, "avatar")

            @discord.ui.button(label="Remove Banner", style=discord.ButtonStyle.secondary)
            async def remove_banner_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                await self.clear_setting(interaction, "banner")

            @discord.ui.button(label="Remove Name", style=discord.ButtonStyle.secondary)
            async def remove_name_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                await self.clear_setting(interaction, "name")

            @discord.ui.button(label="Remove Bio", style=discord.ButtonStyle.secondary)
            async def remove_bio_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                await self.clear_setting(interaction, "bio")

        embed = discord.Embed(
            color=BOT_COLOR,
            description="Choose a section below to update Falcon Premium in this server.",
        )
        embed.add_field(
            name="Avatar",
            value="Change Falcon Premium's bot avatar in this server.",
            inline=False,
        )
        embed.add_field(
            name="Banner",
            value="Change Falcon Premium's bot banner in this server.",
            inline=False,
        )
        embed.add_field(
            name="Bio",
            value="Change Falcon Premium's bot bio in this server.",
            inline=False,
        )
        embed.add_field(
            name="Name",
            value="Set Falcon Premium's bot nickname in this server.",
            inline=False,
        )
        if ctx.guild.me:
            embed.set_author(name="Edit Falcon Premium", icon_url=ctx.guild.me.display_avatar.url)
        return await ctx.send(embed=embed, view=EditFalconView())
    action = action.lower()
    if action == "name":
        if ctx.guild.me is None:
            return await ctx.send(f"{DOT} Bot member is unavailable.")
        await ctx.guild.me.edit(nick=value or None, reason=f"Edited by {ctx.author}")
        await ctx.send(f"{success_tick} Bot guild nickname updated successfully.")
        return
    elif action == "bio":
        await ctx.guild.me.edit(bio=value, reason=f"Edited by {ctx.author}")
        await ctx.send(f"{success_tick} Bot guild bio updated successfully.")
        return
    elif action in {"avatar", "banner"}:
        image_url = value or (ctx.message.attachments[0].url if ctx.message.attachments else None)
        if not image_url:
            return await ctx.send(f"{DOT} Provide an image URL or attachment.")
        async with aiohttp.ClientSession() as session:
            async with session.get(image_url) as response:
                if response.status != 200:
                    return await ctx.send(f"{DOT} Could not download the image.")
                image = await response.read()
        edit_field = "avatar" if action == "avatar" else "banner"
        await ctx.guild.me.edit(**{edit_field: image}, reason=f"Edited by {ctx.author}")
        await ctx.send(f"{success_tick} Bot guild {action} updated successfully.")
        return
    else:
        return await ctx.send(f"{DOT} Action must be `name`, `bio`, `avatar`, or `banner`.")
@bot.command(name="togglemention")
async def togglemention(ctx):
    async with bot.db.execute("SELECT use_mentions FROM leaderboard_preferences WHERE guild_id=?", (ctx.guild.id,)) as cur:
        row = await cur.fetchone()
    value = 0 if row and row[0] else 1
    await bot.db.execute("INSERT INTO leaderboard_preferences (guild_id, use_mentions) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET use_mentions=excluded.use_mentions", (ctx.guild.id, value))
    await bot.db.commit()
    state = "mentions" if value else "usernames"
    await ctx.send(f"{DOT} Leaderboard entries will now use **{state}**.")

# ─── PREMIUM COMMANDS ──────────────────────────────────────────────

@bot.command(name="premium_legacy")
async def premium_cmd(ctx, action: str | None = None, *, target: str | None = None):
    embed = discord.Embed(color=0xffdd00, title="Premium", description="Displays premium command usage\n\n**Commands**")
    embed.description = (embed.description or "") + (
        f"\n{ARROW} `,premium activate`"
        f"\n{ARROW} `,premium gift`"
        f"\n{ARROW} `,premium view user`"
        f"\n{ARROW} `,premium view guild`"
    )
    if action == "activate":
        embed = discord.Embed(color=0xffdd00, description=f"{DOT} To activate premium, please visit our dashboard or support server.")
    elif action == "gift":
        embed = discord.Embed(color=0xffdd00, description=f"{DOT} Use `,premium gift <user id>` to gift 30 days of Premium.")
    elif action == "view":
        if target == "user":
            async with bot.db.execute("SELECT expiry FROM premium_users WHERE user_id=?", (ctx.author.id,)) as cur:
                row = await cur.fetchone()
            if row:
                embed = discord.Embed(color=0xffdd00, description=f"{DOT} You have premium until `{row[0][:10]}`.")
            else:
                embed = discord.Embed(color=0xffdd00, description=f"{DOT} You do not have a premium subscription.")
        elif target == "guild":
            async with bot.db.execute("SELECT expiry FROM premium_guilds WHERE guild_id=?", (ctx.guild.id,)) as cur:
                row = await cur.fetchone()
            if row:
                embed = discord.Embed(color=0xffdd00, description=f"{DOT} This guild has premium until `{row[0][:10]}`.")
            else:
                embed = discord.Embed(color=0xffdd00, description=f"{DOT} This guild does not have premium.")
        else:
            pass
    await ctx.send(embed=embed)

# ─── NO-PREFIX MANAGEMENT ──────────────────────────────────────────

NP_DURATIONS = [
    ("10 Minutes",  "Trial for 10 minutes",       datetime.timedelta(minutes=10),  "10m"),
    ("1 Week",      "No prefix for 1 week",       datetime.timedelta(weeks=1),     "1w"),
    ("3 Weeks",     "No prefix for 3 weeks",      datetime.timedelta(weeks=3),     "3w"),
    ("1 Month",     "No prefix for 1 month",      datetime.timedelta(days=30),     "1mo"),
    ("3 Months",    "No prefix for 3 months.",    datetime.timedelta(days=90),     "3mo"),
    ("6 Months",    "No prefix for 6 months.",    datetime.timedelta(days=180),    "6mo"),
    ("1 Year",      "No prefix for 1 year.",      datetime.timedelta(days=365),    "1y"),
    ("3 Years",     "No prefix for 3 years.",     datetime.timedelta(days=365*3),  "3y"),
    ("Lifetime",    "No prefix permanently.",     None,                            "life"),
]

class NpDurationSelect(discord.ui.Select):
    def __init__(self, target: discord.Member, invoker_id: int):
        self.target = target
        self.invoker_id = invoker_id
        options = [
            discord.SelectOption(label=label, description=desc, value=code)
            for (label, desc, _td, code) in NP_DURATIONS
        ]
        super().__init__(placeholder="Select the duration", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.invoker_id:
            embed = discord.Embed(
                title="No-Prefix Access Denied",
                description="Only the command invoker can select this duration.",
                color=0xFFDD00,
            )
            embed.set_footer(text="Falcon Premium • No-Prefix")
            return await send_interaction_v2(interaction, embed=embed, ephemeral=True)
        code = self.values[0]
        match = next((d for d in NP_DURATIONS if d[3] == code), None)
        if not match:
            embed = discord.Embed(
                title="Invalid Duration",
                description="Select one of the available no-prefix durations.",
                color=0xFFDD00,
            )
            embed.set_footer(text="Falcon Premium • No-Prefix")
            return await send_interaction_v2(interaction, embed=embed, ephemeral=True)
        label, _desc, td, _code = match
        if td is None:
            expiry_dt = datetime.datetime(9999, 12, 31)
            human = "**Lifetime**"
        else:
            expiry_dt = datetime.datetime.now() + td
            human = f"**{label}** (until <t:{int(expiry_dt.timestamp())}:F>)"
        await bot.db.execute(
            "INSERT OR REPLACE INTO noprefix (user_id, expiry) VALUES (?, ?)",
            (self.target.id, expiry_dt.isoformat())
        )
        await bot.db.commit()
        embed = discord.Embed(
            color=0xffdd00,
            description=f"{DOT} Added {self.target.mention} to **No-Prefix** for {human}."
        )
        for child in self.view.children:
            child.disabled = True
        embed.title = "No-Prefix Enabled"
        embed.set_footer(text="Falcon Premium • No-Prefix")
        await edit_interaction_v2(interaction, embed=embed, view=self.view)

class NpDurationView(discord.ui.View):
    def __init__(self, target: discord.Member, invoker_id: int):
        super().__init__(timeout=120)
        self.add_item(NpDurationSelect(target, invoker_id))

@bot.command(name="np_legacy")
async def noprefix_cmd(ctx, action: str | None = None, *, rest: str | None = None):
    if not (await is_bot_owner(ctx.author.id) or await is_bot_staff(ctx.author.id)):
        return
    action = (action or "").lower()

    # ── ADD: ,np add @user  →  duration dropdown ──
    if action == "add":
        user = await _resolve_prefix_member_argument(ctx, rest)
        if not user:
            return await ctx.send(embed=discord.Embed(
                color=0xffdd00,
                description=f"{DOT} Usage: `,np add @user`"
            ))
        embed = discord.Embed(
            color=0xffdd00,
            title="Select No Prefix Duration",
            description=f"Choose the duration for how long no-prefix should be enabled for {user.mention}:"
        )
        view = NpDurationView(user, ctx.author.id)
        await ctx.send(embed=embed, view=view)
        return

    # ── REMOVE: ,np remove @user ──
    if action == "remove":
        user = await _resolve_prefix_member_argument(ctx, rest)
        if not user:
            return await ctx.send(embed=discord.Embed(
                color=0xffdd00, description=f"{DOT} Usage: `,np remove @user`"
            ))
        await bot.db.execute("DELETE FROM noprefix WHERE user_id = ?", (user.id,))
        await bot.db.commit()
        await ctx.send(embed=discord.Embed(
            color=0xffdd00, description=f"{DOT} Removed {user.mention} from **No-Prefix**."
        ))
        return

    # ── LIST: ,np list ──
    if action == "list":
        async with bot.db.execute("SELECT user_id, expiry FROM noprefix") as cursor:
            rows = await cursor.fetchall()
        if not rows:
            return await ctx.send(embed=discord.Embed(
                color=0xffdd00, description=f"{DOT} No no-prefix users found."
            ))
        lines = []
        for uid, expiry in rows:
            try:
                exp_dt = datetime.datetime.fromisoformat(expiry)
                if exp_dt.year >= 9999:
                    when = "Lifetime"
                else:
                    when = f"<t:{int(exp_dt.timestamp())}:R>"
            except Exception:
                when = expiry[:10]
            lines.append(f"• <@{uid}> — {when}")
        embed = discord.Embed(
            color=0xffdd00,
            title="No-Prefix Users",
            description="\n".join(lines)
        )
        return await ctx.send(embed=embed)

    # ── CHECK: ,np check @user ──
    if action == "check":
        user = await _resolve_prefix_member_argument(ctx, rest)
        if not user:
            return await ctx.send(embed=discord.Embed(
                color=0xffdd00, description=f"{DOT} Usage: `,np check @user`"
            ))
        async with bot.db.execute("SELECT expiry FROM noprefix WHERE user_id = ?", (user.id,)) as cursor:
            row = await cursor.fetchone()
        if not row:
            return await ctx.send(embed=discord.Embed(
                color=0xffdd00, description=f"{DOT} {user.mention} does not have no-prefix."
            ))
        try:
            exp_dt = datetime.datetime.fromisoformat(row[0])
            if exp_dt.year >= 9999:
                when = "**Lifetime**"
            elif exp_dt < datetime.datetime.now():
                when = "expired"
            else:
                when = f"<t:{int(exp_dt.timestamp())}:F> (<t:{int(exp_dt.timestamp())}:R>)"
        except Exception:
            when = row[0]
        return await ctx.send(embed=discord.Embed(
            color=0xffdd00, description=f"{DOT} {user.mention} no-prefix: {when}"
        ))

    # ── HELP / fallback ──
    embed = discord.Embed(
        color=0xffdd00,
        title="Np Add Command",
        description=(
            "Add user to no-prefix with time options\n\n"
            "**Alt cmd**\nNo Alt cmd\n\n"
            "**Usage**\n`<user>`\n\n"
            "<[] = optional | <> = required • Use Prefix Before Commands."
        )
    )
    embed.add_field(
        name="Subcommands",
        value="`,np add @user`\n`,np remove @user`\n`,np check @user`\n`,np list`",
        inline=False
    )
    await ctx.send(embed=embed)

@bot.command(name="bonus")
@commands.has_permissions(administrator=True)
async def bonus(ctx, user: discord.Member, amount: int):
    await update_user(ctx.guild.id if ctx.guild else 0, user.id, bonus=amount)

# ─── HELP COMMAND (1:1 Dropdown Clone) ────────────────────────────

HELP_SECTIONS = {
    "premium": {
        "emoji": E_PREMIUM,
        "label": "Premium utility",
        "title": "Premium",
        "description": "Displays premium command usage",
        "commands": [
            (",premium activate", None),
            (",premium gift <user id>", None),
            (",premium view user", None),
            (",premium view guild", None),
        ]
    },
    "invites": {
        "emoji": E_INVITE,
        "label": "Invite tracking",
        "title": "Invite logger/ Invite tracker",
        "description": "Tracks server invites",
        "commands": [
            (",invites", "Displays the invites stats of a user in this guild"),
            (",inviter", "Displays the inviter of a server member"),
            (",invited", "Displays the invited list of a member"),
            (",inviteinfo", "Displays the active invite code(s) of a user in this guild"),
            (",setjoinchannel", "Set the welcome channel! All invites welcome message will appear there"),
            (",addinvites", "Adds a number of invites to a user !"),
            (",removeinvites", "Removes a number of invites from a user !"),
            (",unsetwelcomechannel", "Unset the welcome channel! Note that this command will disable the welcome messages"),
            (",setleave", "Sets the member leave channel"),
            (",unsetleavechannel", "Unsets the member leave channel! Note that this will disable member leave messages"),
            (",setaltthreshold", "Customize fake invites count duration"),
            (",unsetaltthreshold", "Disable custom alt threshold"),
            (",setinviterole", "Setup invite role reward, the bot will give roles to inviters when reaching specified invites count"),
            (",unsetinviterole", "Remove your customized invite role configuration"),
            (",viewinviteroles", "View you customized invite role configuration"),
            (",clearinvites", "Resets everyone's invites or a user's invites statistics in this guild"),
            (",resetmyinvites", "Clears your own invites in this guild (no permissions required)"),
            (",setjoinmessage", "Set a custom join message"),
            (",setleavemessage", "Set a custom leave message"),
            (",setvanitymessage", "Set a custom vanity join message"),
            (",unsetjoinmessage", "Deletes your custom join message"),
            (",unsetleavemessage", "Deletes your custom leave message"),
            (",unsetvanitymessage", "Deletes your custom vanity message"),
            (",variables", "Displays the variables so that you can use them in custom welcome message"),
            (",testmsg", "Displays your custom join message which you enter as an argument!"),
            (",leaderboard invites", "Displays the top 10 inviters in this guild"),
        ]
    },
    "messages": {
        "emoji": E_MSG,
        "label": "Messages",
        "title": "Messages",
        "description": "Tracks user message counts",
        "commands": [
            (",messages", "Displays the number of messages sent by you or a user"),
            (",addmessages", "Adds the specified number of messages to a user"),
            (",removemessages", "Removes the specified number of messages to a user"),
            (",blacklistchannel", "Blacklists a channel , I wont count messages from that channel"),
            (",unblacklistchannel", "Unblacklists a channel"),
            (",blacklistedchannels", "Displays the blacklisted channels of a guild"),
            (",blacklistcategory", "Blacklists a category, I wont count messages sent in that category"),
            (",unblacklistcategory", "Unblacklists a category"),
            (",blacklistedcategories", "Displays the blacklisted categories of a guild"),
            (",setmessagerole", "Setup messages role, the bot will give roles to members when reaching specified messages count"),
            (",unsetmessagerole", "Remove your customized message role configuration"),
            (",viewmessageroles", "View you customized message role configuration"),
            (",clearmsgs", "Resets everyone's messages or a user's messages in this guild"),
            (",resetmymessages", "Resets your own messages that you sent in this guild (no permissions required)"),
            (",setresettime weeklymessage", "Customize your weekly messages reset time"),
            (",setresettime dailymessage", "Customize your daily messages reset time"),
            (",leaderboard weeklymessages", "Displays the top 10 active messengers in this guild"),
            (",leaderboard messages", "Displays the top 10 messengers in this guild"),
            (",leaderboard dailymessages", "Displays the top 10 active messengers in this guild"),
        ]
    },
    "voice": {
        "emoji": E_VOICE,
        "label": "Voice",
        "title": "Voice state tracking",
        "description": "Logs voice channel durations",
        "commands": [
            (",vc", "Displays the voice state stats of a user"),
            (",addvctime", "Add voice time to All time, daily and weekly fields of a user"),
            (",reducevctime", "Reduce voice time from All time, daily and weekly fields to a user"),
            (",clearvoice", "Resets everyone's voice state stats or a user's voice state stats in this guild"),
            (",resetmyvoice", "Resets your own voice state record in this guild (no permissions required)"),
            (",setresettime dailyvoice", "Customize your daily voice state reset time"),
            (",setresettime weeklyvoice", "Customize your weekly voice state reset time"),
            (",leaderboard weeklyvoice", "Displays the top 10 voice state members in this guild"),
            (",leaderboard voice", "Displays the top 10 voice state members in this guild"),
            (",leaderboard dailyvoice", "Displays the top 10 voice state members in this guild"),
        ]
    },
    "autoroles": {
        "emoji": E_ROLE,
        "label": "Auto Roles",
        "title": "Auto Roles",
        "description": "Automatically assign roles to new users",
        "commands_with_header": True,
        "commands": [
            (",autorole add [human|bot] @role", "Assign a role to humans, bots, or everyone"),
            (",autorole remove @role", "Remove a configured autorole"),
            (",autorole list", "List configured autoroles"),
            (",setautorole", "Set autorole for humans and bots"),
            (",unsetautorole", "Delete an autorole"),
            (",configscreening", "This feature will add role to users after they complete membership screening"),
            (",viewautoroles", "Displays a guild's autoroles"),
        ]
    },
    "liveleaderboards": {
        "emoji": E_LB,
        "label": "Live Leaderboards",
        "title": "Live leaderboards",
        "description": None,
        "commands": [
            (",createlivelb", "Create a live leaderboard of invites, messages, voice updated every 30 seconds"),
            (",deletelivelb", "Delete a live leaderboard"),
            (",showlivelbs", "Shows the live leaderboards information of your server"),
        ]
    },
    "giveaway": {
        "emoji": E_GW,
        "label": "Giveaways",
        "title": "Giveaway",
        "description": "Create giveaways",
        "commands_with_header": True,
        "commands": [
            (",gcreate", "Create giveaways with requirements"),
            (",gstart", "Creates a giveaway"),
            (",greroll", "Rerolls an ended giveaway"),
            (",gend", "Ends an active giveaway"),
            (",setgiveaway", "customize giveaway embed"),
            (",unsetgiveaway", "Disable premium giveaway features"),
            (",viewwinnerrole", "Shows your custom giveaway winner role"),
            (",viewbypassrole", "Shows your custom giveaway bypass role"),
            (",giveawayconfig", "Configure giveaway website visibility and invite without dashboard"),
        ]
    },
    "timer": {
        "emoji": E_TIMER,
        "label": "Timer",
        "title": "Timer",
        "description": "Timers are saved and restored after a bot restart.",
        "commands": [
            (",tstart <time> [label]", "Start a timer (30s, 5m, 3h, 1d)"),
            (",tpause", "Pause the active timer"),
            (",tresume", "Resume a paused timer"),
            (",tend", "End the active timer early"),
        ]
    },
    "greet": {
        "emoji": E_GREET,
        "label": "Greet",
        "title": "Greet",
        "description": "Configure welcome text and attach reusable saved embeds.",
        "commands_with_header": True,
        "commands": [
            ("/set greet message", "Set welcome text; use {embed:name} to attach a saved embed"),
            ("/set greet channel", "Choose where welcome messages are sent"),
            ("/test greet", "Preview the configured welcome message"),
            (",greet card enable|disable [join|leave|both]", "Toggle image greeting cards"),
            (",greet delete-after <join|leave|both> <seconds>", "Auto-delete greeting messages; 0 disables it"),
        ]
    },
    "leave": {
        "emoji": E_GREET,
        "label": "Leave",
        "title": "Leave",
        "description": "Configure member departure messages.",
        "commands": [
            ("/leave message", "Set a text goodbye"),
            ("/leave test", "Preview the configured goodbye"),
            ("/leave config", "Show leave settings"),
            ("/leave reset", "Reset leave settings"),
        ],
    },
    "boost": {
        "emoji": E_GW,
        "label": "Boost",
        "title": "Boost",
        "description": "Thank members for boosting your server.",
        "commands": [
            ("/boost setup", "Choose a boost notification channel"),
            ("/boost card enable|disable", "Toggle Nitro-style booster cards"),
            ("/boost message", "Set a text boost response; use {embed:name} for a saved embed"),
            ("/boost preview", "Preview the current boost response"),
            ("/boost config", "Show boost settings"),
            ("/boost reset", "Reset boost settings"),
        ],
    },
    "autoresponder": {
        "emoji": E_TIMER,
        "label": "Autoresponder",
        "title": "Autoresponder",
        "description": "Configure guild-scoped automated replies.",
        "commands": [
            ("/autoresponder add", "Add a trigger and response"),
            ("/autoresponder options", "Configure matching, channels, roles, and cooldowns"),
            ("/autoresponder restrictions", "Set role/channel exclusions and cooldown scope"),
            ("/autoresponder list", "Browse configured triggers"),
            ("/autoresponder test", "Preview a response"),
            ("/autoresponder remove", "Remove a trigger"),
        ],
    },
    "customcommands": {
        "emoji": E_MANAGE,
        "label": "Custom Commands",
        "title": "Custom Commands",
        "description": "Create safe, per-server prefix commands.",
        "commands": [
            ("/customcommand create", "Create a command"),
            ("/customcommand edit", "Edit a response"),
            ("/customcommand list", "Browse configured commands"),
            ("/customcommand test", "Preview a response"),
            ("/customcommand remove", "Remove a command"),
        ],
    },
    "embeds": {
        "emoji": E_POLL,
        "label": "Embeds",
        "title": "Embeds",
        "description": "Build and reuse custom embeds.",
        "commands": [
            (",embed create <name>", "Open the button editor to create and save an embed"),
            (",embed edit <name>", "Edit a saved embed in the button editor"),
            (",embed show <name>", "Send a saved embed"),
            ("/embed field add", "Add a field"),
            (",embed send <name> #channel", "Send a saved embed to a channel"),
            (",embed clone <name> <new-name>", "Clone a saved embed"),
            (",embed list", "Browse saved embeds"),
        ],
    },
    "configuration": {
        "emoji": E_MANAGE,
        "label": "Configuration",
        "title": "Configuration",
        "description": "Review this server's feature settings.",
        "commands": [
            ("/config", "Show the full server configuration"),
            ("/config greet", "Show greeting settings"),
            ("/config leave", "Show leave settings"),
            ("/config boost", "Show boost settings"),
            ("/config autoresponder", "Show autoresponder counts"),
            ("/config customcommands", "Show custom-command counts"),
            ("/config embeds", "Show saved-embed count"),
        ],
    },
    "moderation": {
        "emoji": E_MOD,
        "label": "Moderation",
        "title": "Moderation",
        "description": None,
        "commands": [
            (",kick", "Kicks a user from a guild"),
            (",erase", "Delete a number of messages from a channel"),
            (",ban", "Bans a user from a guild"),
            (",banall [humans|bots|all]", "Review and ban eligible members in bulk"),
            (",unban", "Unbans a banned user from a Discord server"),
            (",unbanall", "Review and unban the current server ban list"),
            (",unroleall @role [all|bots|humans]", "Review and remove a role in bulk"),
            (",roleall human|bot add|remove @role", "Add or remove a role for human or bot members"),
            (",roleall @role", "Add a role to everyone; legacy `human @role` and `bot @role` forms also work"),
            (",mute", "Mutes a server member for a specified amount of time"),
            (",unmute", "Unmutes a server member"),
            (",channel delete #channel", "Delete a channel (owner or Administrator only)"),
        ]
    },
    "managecommands": {
        "emoji": E_MANAGE,
        "label": "Manage Commands",
        "title": "Manage Commands",
        "description": None,
        "commands": [
            (",disable command", "Disables a specific command"),
            (",disable module", "Disable a module in a channel or in the server"),
            (",enable module", "Remove module from the blacklist"),
            (",enable command", "Remove command from the blacklist"),
        ]
    },
    "poll": {
        "emoji": E_POLL,
        "label": "Poll",
        "title": "Polls",
        "description": "Creates a poll",
        "commands_with_header": True,
        "commands": [
            (",createpoll", "Create a poll"),
            (",poll <question> <option1> <option2> [private]", "Create a public or private two-option poll"),
            (",epoll", "Ends an active poll"),
        ]
    },
    "utility": {
        "emoji": E_UTILITY,
        "label": "Utility",
        "title": "Utility",
        "description": None,
        "commands": [
            (",setprefix <prefix>", "Set a server-specific message prefix"),
            (",deleteprefix", "Restore the default message prefix"),
            (",editfalconpremium", "Edit Falcon Premium's server name, bio, avatar, or banner"),
            (",lock [#channel]", "Stop members from sending or connecting in a channel"),
            (",unlock [#channel]", "Restore inherited send/connect permissions"),
            (",hide [#channel]", "Hide a channel from @everyone"),
            (",unhide [#channel]", "Restore inherited channel visibility"),
            (",timedifference", "Show the difference between two Discord object creation times"),
            (",nuke", "Nuke a text channel with confirmation"),
            (",serverinfo", "Display detailed server information"),
            (",si", "Short command for serverinfo"),
            (",userinfo", "Display member information"),
            (",ui", "Short command for userinfo"),
            (",roleinfo", "Display guild role information"),
            (",vcinfo", "Display voice channel information"),
            (",avatar", "Display a user's avatar"),
            (",banner", "Display a user's banner"),
            (",guildbanner", "Display the server banner"),
            (",support", "Display the support server invite link"),
            (",mc", "Display the server member count"),
            (",stats", "Display bot and VPS statistics"),
            (",perms", "Display the bot permissions required to function"),
            (",accage", "Display an account's age"),
            (",invite", "Display bot invite links"),
            (",sponsor", "Display sponsor information"),
            (",uptime", "Display bot uptime"),
            (",botinfo", "Display bot information"),
            ("/levelupcard enable|disable", "Toggle rank cards when a member levels up"),
            (",ping", "Display API latency"),
            (",togglemention", "Toggle usernames or mentions in leaderboards"),
            (",setresettime", "Customize message and voice reset times"),
            (",leaderboard", "Display the top users in a category"),
            (",lb i", "Invite leaderboard"),
            (",lb m", "Message leaderboard"),
            (",lb vc", "Voice leaderboard"),
            (",rmi", "Reset your invite stats"),
            (",resetinvites", "Reset a member's joins, leaves, bonus, fake, rejoin, or all invite stats"),
        ]
    },
    "credits": {
        "emoji": E_CONTACT,
        "label": "Credits",
        "title": "Credits",
        "description": None,
        "contact": True,
    },
    "voice_rooms": {
        "emoji": _manifest_emoji("help_voice_rooms", "<:fp_invite:1551879046722359417>"),
        "label": "Voice Rooms",
        "title": "Temporary Voice Rooms",
        "description": (
            "Create temporary rooms by joining a configured voice hub. "
            "Room owners can control access, chat, members, and voice settings."
        ),
        "commands": [
            (",voicehub setup [hub] [panel] [category]", "Configure a hub and persistent room-control panel"),
            (",voicehub status", "Show the configured hub, panel, and category"),
            (",voicehub disable", "Disable room creation without removing existing rooms"),
            (",room", "Show the room setup summary"),
            (",room info", "Show the full room command reference"),
            (",room set_channel <voice-channel>", "Set the join-to-create channel"),
            (",room set_category [category]", "Choose or clear the new-room category"),
            (",room rename <name>", "Rename your room"),
            (",room limit <0-99>", "Set the room user limit; 0 is unlimited"),
            (",room bitrate <kbps>", "Set bitrate within the server's boost-tier limit"),
            (",room region [name]", "Set the RTC region or list available regions"),
            (",room lock / unlock", "Close or reopen room access"),
            (",room hide / unhide", "Hide or show your room to other members"),
            (",room allow / block / ban <member>", "Persistently allow or deny a member"),
            (",room permit <member>", "Allow a member to join and chat"),
            (",room clear [member]", "Clear one or all member access overrides"),
            (",room openchat / closechat", "Open or close text-in-voice chat"),
            (",room kick / mute / unmute <member>", "Moderate a member in your room"),
            (",room summon <member>", "Move a member into your room"),
            (",room claim", "Claim a room after its owner leaves"),
            (",room transfer <member>", "Transfer ownership to a current room member"),
            (",room status / reset", "View or reset room settings and overrides"),
        ],
    },
}

HELP_SECTIONS.update({
    "reference_automation": {
        "emoji": _manifest_emoji("help_automation", "<:mods:1551879061171736587>"),
        "label": "Automation",
        "title": "Server Automation",
        "description": "Schedule messages, reactions, bump reminders, and custom-status roles.",
        "commands": [
            (",autopost add #channel <interval> <message>", "Schedule a recurring message"),
            (",autopost list / remove <id>", "Review or remove scheduled posts"),
            (",autobump set #channel [interval]", "Schedule a bump reminder"),
            (",autobump status / disable", "Review or disable bump reminders"),
            (",autoreact add <trigger> <emoji> [#channel]", "React to matching messages"),
            (",autoreact list / remove <id>", "Review or remove reaction rules"),
            (",voicerole add @role", "Grant a role when members join voice; remove it when they leave"),
            (",voicerole remove @role / list", "Remove or list voice roles"),
            (",vanityroles add <keyword> @role", "Grant a role for a custom-status keyword"),
            (",vanityroles list / remove <id>", "Review or remove vanity-role rules"),
        ],
    },
    "reference_utilities": {
        "emoji": _manifest_emoji("help_more_utilities", "<:fp_invite:1551879046722359417>"),
        "label": "More Utilities",
        "title": "More Utilities",
        "description": "Conversions, search, data exports, and community commands.",
        "commands": [
            (",convert length|weight <amount> <from> <to>", "Convert common units"),
            (",convert encode|decode <base32|hex|rot13|binary> <text>", "Convert text formats"),
            (",calculate <expression>", "Evaluate basic arithmetic safely"),
            (",wiki <query>", "Find a Wikipedia article summary"),
            (",github <query> / ,youtube <query>", "Search GitHub or YouTube"),
            (",globalboard [messages|invites]", "View global message or invite rankings"),
            (",export <members|roles|bans|messages>", "Export server data to CSV (Administrator)"),
            (",todo add|list|remove", "Manage your personal todo list"),
            (",animal [cat|dog|fox] / ,meme", "Show an animal fact or wholesome meme"),
            (",fakehack [member]", "Run a fictional hack roleplay"),
        ],
    },
    "reference_profiles": {
        "emoji": _manifest_emoji("help_profiles", "<:polls:1551879063248044104>"),
        "label": "Profiles",
        "title": "Member Profiles",
        "description": "Personalize a profile with a bio, background, and social links.",
        "commands": [
            (",profilecard [member]", "View a member's custom profile"),
            (",profileedit bio <text>", "Set your profile bio"),
            (",profileedit background <https-url>", "Set your profile background image"),
            (",profileedit social <platform> <https-url>", "Add or update a social link"),
            (",profileedit clear", "Clear your custom profile"),
        ],
    },
})


COMMAND_MODULES = {
    "admin": {"setowner", "unsetowner", "owners", "setstaff", "unsetstaff", "staff", "blacklist", "c"},
    "premium": {"premium", "premiumadd", "premiumremove", "np"},
}


def _command_module_names(command):
    module_names = {command.qualified_name.split(" ", 1)[0].lower()}
    root_command = command
    while root_command.parent:
        root_command = root_command.parent
    root_names = {root_command.name.lower(), *(alias.lower() for alias in root_command.aliases)}
    for module_name, command_roots in COMMAND_MODULES.items():
        if root_names & command_roots:
            module_names.add(module_name)

    for module_name, section in HELP_SECTIONS.items():
        for item in section.get("commands", []):
            command_text = item[0] if isinstance(item, tuple) else item
            words = command_text.lstrip(",!").split()
            while words:
                listed_command = bot.get_command(" ".join(words))
                if listed_command:
                    qualified_name = listed_command.qualified_name.lower()
                    command_name = command.qualified_name.lower()
                    if command_name == qualified_name or (
                        isinstance(listed_command, commands.Group)
                        and command_name.startswith(f"{qualified_name} ")
                    ):
                        module_names.add(module_name)
                    break
                words.pop()
    return module_names


@bot.check
async def check_command_enabled(ctx):
    if not ctx.guild or not ctx.command:
        return True
    command_root = ctx.command.qualified_name.split(" ", 1)[0].lower()
    if command_root not in {"premium", "premiumadd", "premiumremove"}:
        if not await is_guild_premium(ctx.guild.id):
            await ctx.send(
                embed=premium_required_embed(),
                view=PremiumSupportView(),
            )
            return False
    if ctx.command.qualified_name in {"help", "enable", "disable"}:
        return True

    command_names = {ctx.command.name.lower(), ctx.command.qualified_name.lower()}
    command_names.update(alias.lower() for alias in ctx.command.aliases)
    parent = ctx.command.parent
    while parent:
        command_names.add(parent.qualified_name.lower())
        command_names.update(f"{alias.lower()} {ctx.command.name.lower()}" for alias in parent.aliases)
        parent = parent.parent

    async with bot.db.execute(
        "SELECT command_name FROM disabled_commands "
        "WHERE guild_id=? AND channel_id IN (0, ?)",
        (ctx.guild.id, ctx.channel.id),
    ) as cursor:
        disabled_commands = {row[0].lower() for row in await cursor.fetchall()}
    if command_names & disabled_commands:
        await ctx.send(f"{DOT} This command is disabled in this server or channel.")
        return False

    async with bot.db.execute(
        "SELECT module_name FROM disabled_modules "
        "WHERE guild_id=? AND channel_id IN (0, ?)",
        (ctx.guild.id, ctx.channel.id),
    ) as cursor:
        disabled_modules = {row[0].lower() for row in await cursor.fetchall()}
    if _command_module_names(ctx.command) & disabled_modules:
        await ctx.send(f"{DOT} This command's module is disabled in this server or channel.")
        return False
    return True


def build_main_help_embed(custom_emojis=None):
    # Map section index -> emoji (custom takes precedence, else section default)
    keys = list(HELP_SECTIONS.keys())
    def em(i):
        if custom_emojis and i < len(custom_emojis) and custom_emojis[i]:
            return custom_emojis[i]
        return HELP_SECTIONS[keys[i]]["emoji"]
    def section_em(key):
        return HELP_SECTIONS[key]["emoji"]
    embed = discord.Embed(color=0xffdd00)
    embed.description = (
        f"Hey there, my prefix is `,`\n\n"
        f"{E_NEWS} **News:**\n"
        f"> {ARROW} Access the new Falcon Premium Dashboard to configure your server!\n"
        f"> {ARROW} Upgrade your Discord server with Falcon Premium now to enjoy feature rich Discord bot!\n\n"
        f"I can do invite tracking, message tracking, voice state tracking, can manage your server events with giveaways, greet system ,timers, polls and much more! "
        f"You can checkout my other commands in the context menu!\n\n"
        f"{em(0)} • **Premium utility**\n"
        f"{em(1)} • **Invite tracking**\n"
        f"{em(2)} • **Messages**\n"
        f"{em(3)} • **Voice**\n"
        f"{em(4)} • **Auto Roles**\n"
        f"{em(5)} • **Live Leaderboards**\n"
        f"{em(6)} • **Giveaways**\n"
        f"{em(7)} • **Timer**\n"
        f"{em(8)} • **Greet**\n"
        f"{em(9)} • **Moderation**\n"
        f"{em(10)} • **Manage Commands**\n"
        f"{em(11)} • **Poll**\n"
        f"{em(12)} • **Utility**\n"
        f"{em(13)} • **Credits**\n"
        f"{section_em('voice_rooms')} • **Voice Rooms**\n"
        f"{section_em('reference_automation')} • **Automation**\n"
        f"{section_em('reference_utilities')} • **More Utilities**\n"
        f"{section_em('reference_profiles')} • **Profiles**"
    )
    return embed

def build_section_embed(section_key: str):
    s = HELP_SECTIONS[section_key]
    embed = discord.Embed(color=0xffdd00)
    embed.title = s["title"]

    if s.get("contact"):
        support_server = os.getenv("SUPPORT_SERVER", "").strip() or "#"
        embed.description = (
            f"{ARROW} **Support server:** [Join here]({support_server})\n"
            f"{ARROW} **Owner:** <@{PRIMARY_OWNER_ID}>\n"
            f"{ARROW} **Owner ID:** `{PRIMARY_OWNER_ID}`"
        )
        return embed

    if s.get("description"):
        embed.description = s["description"] + "\n"
    else:
        embed.description = ""

    has_header = s.get("commands_with_header", False)
    if has_header:
        embed.description += "\n**Commands**"

    lines = []
    for item in s["commands"]:
        if isinstance(item, tuple):
            cmd, desc = item
        else:
            cmd, desc = item, None
        if desc:
            lines.append(f"{ARROW} `{cmd}` - {desc}")
        else:
            lines.append(f"{ARROW} `{cmd}`")

    embed.description += "\n" + "\n".join(lines)
    return embed

class HelpDropdown(discord.ui.Select):
    def __init__(self, custom_emojis=None):
        options = [
            discord.SelectOption(label="Select a category...", value="main", default=True),
        ]
        keys = list(HELP_SECTIONS.keys())
        for i, key in enumerate(keys):
            data = HELP_SECTIONS[key]
            emoji_str = data["emoji"]
            if custom_emojis and i < len(custom_emojis) and custom_emojis[i]:
                emoji_str = custom_emojis[i]
            emoji_obj = None
            try:
                emoji_obj = discord.PartialEmoji.from_str(emoji_str)
            except Exception:
                emoji_obj = None
            try:
                options.append(discord.SelectOption(label=data["label"], value=key, emoji=emoji_obj or emoji_str))
            except Exception:
                options.append(discord.SelectOption(label=data["label"], value=key))
        super().__init__(placeholder="Select a category...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        val = self.values[0]
        gid = interaction.guild.id if interaction.guild else None
        custom = await get_guild_dropdown_emojis(gid)
        if val == "main":
            embed = build_main_help_embed(custom_emojis=custom)
        else:
            embed = build_section_embed(val)
        for opt in self.options:
            opt.default = (opt.value == val)
        await edit_interaction_v2(interaction, embed=embed, view=self.view)

class HelpView(discord.ui.View):
    def __init__(self, custom_emojis=None):
        super().__init__(timeout=120)
        self.add_item(HelpDropdown(custom_emojis=custom_emojis))

        invite_url = _bot_admin_invite_url()
        support_url = os.getenv("SUPPORT_SERVER", "").strip()

        if invite_url:
            self.add_item(discord.ui.Button(label="Invite me", url=invite_url, style=discord.ButtonStyle.url))
        if support_url:
            self.add_item(discord.ui.Button(label="Support server", url=support_url, style=discord.ButtonStyle.url))

async def _send_help_section(ctx, section_key):
    custom = await get_guild_dropdown_emojis(ctx.guild.id if ctx.guild else None)
    embed = build_section_embed(section_key)
    view = HelpView(custom_emojis=custom)
    for option in view.children[0].options:
        option.default = option.value == section_key
    await ctx.send(embed=embed, view=view)


@bot.hybrid_group(name="help", invoke_without_command=True, aliases=["h", "cmds", "commands"])
async def help_cmd(ctx):
    custom = await get_guild_dropdown_emojis(ctx.guild.id if ctx.guild else None)
    embed = build_main_help_embed(custom_emojis=custom)
    view = HelpView(custom_emojis=custom)
    await ctx.send(embed=embed, view=view)


@help_cmd.command(name="greet", description="Show greet and welcome commands")
async def help_greet(ctx):
    await _send_help_section(ctx, "greet")


@help_cmd.command(name="leave", description="Show leave notification commands")
async def help_leave(ctx):
    await _send_help_section(ctx, "leave")


@help_cmd.command(name="boost", description="Show server boost notification commands")
async def help_boost(ctx):
    await _send_help_section(ctx, "boost")


@help_cmd.command(name="autoresponder", description="Show autoresponder commands")
async def help_autoresponder(ctx):
    await _send_help_section(ctx, "autoresponder")


@help_cmd.command(name="customcommand", description="Show custom-command commands")
async def help_customcommand(ctx):
    await _send_help_section(ctx, "customcommands")


@help_cmd.command(name="embed", description="Show custom embed commands")
async def help_embed(ctx):
    await _send_help_section(ctx, "embeds")


@help_cmd.command(name="config", description="Show server configuration commands")
async def help_config(ctx):
    await _send_help_section(ctx, "configuration")


@help_cmd.command(name="voice_rooms", description="Show temporary voice-room commands")
async def help_voice_rooms(ctx):
    await _send_help_section(ctx, "voice_rooms")

@bot.command(name="credits")
async def credits_cmd(ctx):
    custom = await get_guild_dropdown_emojis(ctx.guild.id if ctx.guild else None)
    embed = build_section_embed("credits")
    view = HelpView(custom_emojis=custom)
    for opt in view.children[0].options:
        opt.default = (opt.value == "credits")
    await ctx.send(embed=embed, view=view)

@bot.hybrid_command(name="refreshemojis", description="(Owner) Refresh global application emojis")
async def refresh_emojis(ctx):
    if not await is_bot_owner(ctx.author.id):
        return
    if not ctx.guild:
        return await ctx.send(f"{DOT} Run this inside a server.")
    await sync_application_emojis()
    await ctx.send(f"{DOT} Global application emojis refreshed. Try `,help`.")

@bot.hybrid_command(name="resetemojis", description="(Owner) Reset help dropdown emojis to defaults")
async def reset_emojis(ctx):
    if not await is_bot_owner(ctx.author.id):
        return
    if not ctx.guild:
        return
    await bot.db.execute("DELETE FROM guild_help_emojis WHERE guild_id = ?", (ctx.guild.id,))
    await bot.db.commit()
    await ctx.send(f"{DOT} Dropdown emojis reset to defaults.")

# ─── Owner / Staff Management ──────────────────────────────────────
@bot.hybrid_command(name="setowner", description="(Primary owner) Promote a user to bot owner")
async def setowner_cmd(ctx, user: discord.User):
    if ctx.author.id != PRIMARY_OWNER_ID:
        return await ctx.send(f"{DOT} Only the primary owner can do that.")
    if user.id == PRIMARY_OWNER_ID:
        return await ctx.send(f"{DOT} That user is already the primary owner.")
    await bot.db.execute("INSERT OR IGNORE INTO bot_owners (user_id) VALUES (?)", (user.id,))
    await bot.db.commit()
    await ctx.send(f"{DOT} **{user}** is now a bot owner.")

@bot.hybrid_command(name="unsetowner", description="(Primary owner) Remove an owner")
async def unsetowner_cmd(ctx, user: discord.User):
    if ctx.author.id not in PRIMARY_OWNER_IDS:
        return await ctx.send(f"{DOT} Only a primary owner can do that.")
    if user.id in PRIMARY_OWNER_IDS:
        return await ctx.send(f"{DOT} A primary owner cannot be removed.")
    await bot.db.execute("DELETE FROM bot_owners WHERE user_id = ?", (user.id,))
    await bot.db.commit()
    await ctx.send(f"{DOT} **{user}** is no longer a bot owner.")

@bot.hybrid_command(name="owners", description="List all bot owners")
async def owners_cmd(ctx):
    async with bot.db.execute("SELECT user_id FROM bot_owners") as cur:
        rows = await cur.fetchall()
    lines = [f"{ARROW} <@{oid}> — `{oid}` (Primary)" for oid in PRIMARY_OWNER_IDS]
    for (uid,) in rows:
        if uid in PRIMARY_OWNER_IDS: continue
        lines.append(f"{ARROW} <@{uid}> — `{uid}`")
    embed = discord.Embed(color=0xffdd00, title="Bot Owners", description="\n".join(lines))
    await ctx.send(embed=embed)

@bot.hybrid_command(name="setstaff", description="(Owner) Promote a user to bot staff")
async def setstaff_cmd(ctx, user: discord.User):
    if not await is_bot_owner(ctx.author.id):
        return await ctx.send(f"{DOT} Owner only.")
    await bot.db.execute("INSERT OR IGNORE INTO bot_staff (user_id) VALUES (?)", (user.id,))
    await bot.db.commit()
    await ctx.send(f"{DOT} **{user}** is now bot staff.")

@bot.hybrid_command(name="unsetstaff", description="(Owner) Remove a staff member")
async def unsetstaff_cmd(ctx, user: discord.User):
    if not await is_bot_owner(ctx.author.id):
        return await ctx.send(f"{DOT} Owner only.")
    await bot.db.execute("DELETE FROM bot_staff WHERE user_id = ?", (user.id,))
    await bot.db.commit()
    await ctx.send(f"{DOT} **{user}** is no longer bot staff.")

@bot.hybrid_command(
    name="botstaff",
    aliases=["stafflist"],
    description="List all bot staff",
)
async def staff_cmd(ctx):
    async with bot.db.execute("SELECT user_id FROM bot_staff") as cur:
        rows = await cur.fetchall()
    if not rows:
        return
    desc = "\n".join([f"{ARROW} <@{uid}> — `{uid}`" for (uid,) in rows])
    embed = discord.Embed(color=0xffdd00, title="Bot Staff", description=desc)
    await ctx.send(embed=embed)

# ═══════════════════════════════════════════════════════════════════
# ─── ABOUT ─────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════
@bot.hybrid_command(name="about", description="About Falcon")
async def about_cmd(ctx):
    async with bot.db.execute("SELECT user_id FROM bot_owners") as cur:
        extra_owners = [r[0] for r in await cur.fetchall() if r[0] not in PRIMARY_OWNER_IDS]
    async with bot.db.execute("SELECT user_id FROM bot_staff") as cur:
        staff = [r[0] for r in await cur.fetchall()]
    server_count, member_count = await _cluster_totals()

    primary_lines = "\n".join([f"{ARROW} <@{oid}> (`{oid}`)" for oid in PRIMARY_OWNER_IDS])
    owner_lines = "\n".join([f"{ARROW} <@{oid}>" for oid in extra_owners]) if extra_owners else f"{DOT} *(none)*"
    staff_lines = "\n".join([f"{ARROW} <@{sid}>" for sid in staff]) if staff else f"{DOT} *(none)*"

    embed = discord.Embed(color=0xffdd00, title="About Falcon Premium")
    embed.description = (
        "A multipurpose Discord bot featuring moderation, "
        "crypto tools, utility commands, and more.\n"
    )
    embed.add_field(name="Primary Owners", value=primary_lines, inline=False)
    embed.add_field(name="Owners", value=owner_lines, inline=False)
    embed.add_field(name="Staff", value=staff_lines, inline=False)
    embed.add_field(name="Servers", value=str(server_count), inline=True)
    embed.add_field(name="Users", value=str(member_count), inline=True)
    embed.add_field(name="Latency", value=f"{round(bot.latency*1000)} ms", inline=True)
    embed.set_footer(text="Falcon Premium • Multi-purpose")
    await ctx.send(embed=embed)

# ═══════════════════════════════════════════════════════════════════
# ─── INVITE ────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════
@bot.hybrid_command(name="invite", aliases=["inv", "add"], description="Get Falcon Premium invite links")
async def invite_cmd(ctx):
    invite_url = _bot_admin_invite_url()
    if invite_url is None:
        return await ctx.send("The bot invite link is unavailable. Configure BOT_APPLICATION_ID.")
    embed = discord.Embed(
        color=0xffdd00,
        title="Falcon Premium bot Invite",
        description=(
            f"Invite this bot with Administrator permissions:\n"
            f"[Click here to invite]({invite_url})"
        )
    )
    embed.set_footer(text=f"Requested by - {ctx.author}")
    await ctx.send(embed=embed)

# ═══════════════════════════════════════════════════════════════════
# ─── CUSTOMIZATION (Owner only): ,c a <url>  ,c b <url> ────────────
# ═══════════════════════════════════════════════════════════════════
@bot.command(name="c")
async def customize_cmd(ctx, action: str | None = None, *, url: str | None = None):
    if not await is_bot_owner(ctx.author.id):
        return
    if not action:
        return await ctx.send(embed=discord.Embed(
            color=0xffdd00,
            description=(f"{DOT} Customization\n"
                         f"{ARROW} `,c a <url|attachment>` — change bot avatar\n"
                         f"{ARROW} `,c b <url|attachment>` — change bot banner")
        ))
    action = action.lower()
    if action not in ("a", "avatar", "b", "banner"):
        return await ctx.send(f"{DOT} Use `a` (avatar) or `b` (banner).")
    # Get image bytes from URL or attachment
    img_url = url
    if not img_url and ctx.message.attachments:
        img_url = ctx.message.attachments[0].url
    if not img_url:
        return await ctx.send(f"{DOT} Provide an image URL or attach an image.")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(img_url) as resp:
                if resp.status != 200:
                    return await ctx.send(f"{DOT} Failed to download image (HTTP {resp.status}).")
                data = await resp.read()
        if action in ("a", "avatar"):
            await bot.user.edit(avatar=data)
            kind = "avatar"
        else:
            await bot.user.edit(banner=data)
            kind = "banner"
        await ctx.send(embed=discord.Embed(
            color=0xffdd00, description=f"{DOT} Bot {kind} updated successfully."
        ))
    except discord.HTTPException as e:
        await ctx.send(f"{DOT} Discord error: {e}")
    except Exception as e:
        await ctx.send(f"{DOT} Error: {e}")

# ═══════════════════════════════════════════════════════════════════
# ─── EMOJI STEAL / INFO ────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════
EMOJI_RE = re.compile(r"<(a)?:(\w+):(\d+)>")

def _steal_message_assets(message):
    emoji_assets = []
    seen_emoji_ids = set()
    for match in EMOJI_RE.finditer(getattr(message, "content", "") or ""):
        animated, name, emoji_id = match.groups()
        if emoji_id in seen_emoji_ids:
            continue
        seen_emoji_ids.add(emoji_id)
        extension = "gif" if animated else "png"
        emoji_assets.append({
            "name": name,
            "url": f"https://cdn.discordapp.com/emojis/{emoji_id}.{extension}?size=512&quality=lossless",
            "filename": f"{name}.{extension}",
        })

    sticker_assets = []
    for sticker in getattr(message, "stickers", ()):
        sticker_name = getattr(sticker, "name", "sticker")
        sticker_format = getattr(getattr(sticker, "format", None), "name", "png").lower()
        sticker_assets.append({
            "name": sticker_name,
            "url": str(sticker.url),
            "filename": f"{sticker_name}.{sticker_format}",
            "description": getattr(sticker, "description", None) or "",
            "emoji": getattr(sticker, "emoji", None) or "🙂",
        })

    image_assets = []
    sticker_image_assets = []
    seen_image_urls = set()
    image_extensions = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".apng"}

    def add_image_asset(url, filename, content_type=""):
        mime_extension = content_type.partition(";")[0].removeprefix("image/").lower()
        if mime_extension == "jpeg":
            mime_extension = "jpg"
        extension = f".{mime_extension}" if f".{mime_extension}" in image_extensions else Path(filename).suffix.lower()
        if extension not in image_extensions:
            extension = Path(str(url).split("?", 1)[0]).suffix.lower()
        if extension not in image_extensions or url in seen_image_urls:
            return
        seen_image_urls.add(url)
        name = Path(filename).stem or "image"
        asset = {"name": name, "url": url, "filename": f"{name}{extension}"}
        image_assets.append(asset)
        sticker_image_assets.append({**asset, "description": "", "emoji": "🙂"})

    for attachment in getattr(message, "attachments", ()):
        filename = getattr(attachment, "filename", "image.png")
        content_type = getattr(attachment, "content_type", None) or ""
        if content_type.startswith("image/") or Path(filename).suffix.lower() in image_extensions:
            add_image_asset(attachment.url, filename, content_type)

    for embed in getattr(message, "embeds", ()):
        for field in ("image", "thumbnail", "video"):
            media_url = getattr(getattr(embed, field, None), "url", None)
            if not media_url:
                continue
            media_path = str(media_url).split("?", 1)[0]
            extension = Path(media_path).suffix.lower()
            if extension in image_extensions:
                add_image_asset(media_url, Path(media_path).name)
    return emoji_assets, sticker_assets, image_assets, sticker_image_assets


def _has_stealable_assets(message):
    return any(_steal_message_assets(message))


async def _find_steal_source(ctx):
    reference = getattr(ctx.message, "reference", None)
    if reference is not None:
        source = getattr(reference, "resolved", None)
        if source is None and getattr(reference, "message_id", None):
            try:
                source = await ctx.channel.fetch_message(reference.message_id)
            except discord.HTTPException:
                source = None
        if source is not None and _has_stealable_assets(source):
            return source

    mentioned_ids = {user.id for user in getattr(ctx.message, "mentions", ())}
    history = getattr(ctx.channel, "history", None)
    if mentioned_ids and history is not None:
        async for candidate in ctx.channel.history(limit=50):
            if candidate.author.id in mentioned_ids and _has_stealable_assets(candidate):
                return candidate

    return ctx.message if _has_stealable_assets(ctx.message) else None


def _safe_stolen_name(value, fallback, existing, limit):
    name = re.sub(r"[^A-Za-z0-9_]", "_", str(value)).strip("_").lower()[:limit]
    if len(name) < 2:
        name = fallback
    base = name
    suffix = 2
    while name in existing:
        ending = f"_{suffix}"
        name = f"{base[:limit - len(ending)]}{ending}"
        suffix += 1
    existing.add(name)
    return name


def _steal_upload_limit(guild, *, for_sticker=False):
    hard_limit = (31 if for_sticker else 23) * 1024 * 1024
    guild_limit = getattr(guild, "filesize_limit", None)
    return min(guild_limit, hard_limit) if guild_limit else hard_limit


async def _download_steal_asset(session, guild, asset, *, for_sticker=False):
    async with session.get(asset["url"]) as response:
        if response.status != 200:
            raise ValueError(f"download returned HTTP {response.status}")
        data = await response.read()
    upload_limit = _steal_upload_limit(guild, for_sticker=for_sticker)
    extension = Path(asset["filename"]).suffix.lower()
    if len(data) > upload_limit:
        if extension == ".gif":
            data = _compress_steal_gif(data, upload_limit)
        else:
            target_extension = ".png" if for_sticker and extension in {".jpg", ".jpeg", ".webp"} else extension
            data = _compress_steal_image(data, upload_limit, target_extension)
    elif (for_sticker and extension in {".jpg", ".jpeg", ".webp"}) or extension == ".webp":
        data = _compress_steal_image(data, upload_limit, ".png")
    if len(data) > upload_limit:
        limit_mib = upload_limit / (1024 * 1024)
        raise ValueError(f"asset exceeds the {limit_mib:.0f} MiB upload limit after optimization")
    return data


def _compress_steal_image(data, max_bytes, output_extension):
    from PIL import Image

    format_by_extension = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}
    output_format = format_by_extension.get(output_extension)
    if output_format is None:
        raise ValueError(f"unsupported image format: {output_extension}")
    try:
        with Image.open(io.BytesIO(data)) as source:
            if getattr(source, "n_frames", 1) > 1:
                raise ValueError("animated images other than GIF cannot be resized safely")
            for max_dimension in (4096, 3072, 2048, 1536, 1024, 768, 512, 384, 256, 128):
                image = source.copy()
                image.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
                if output_format == "JPEG" and image.mode not in {"RGB", "L"}:
                    image = image.convert("RGB")
                qualities = (88, 76, 64, 52, 40) if output_format in {"JPEG", "WEBP"} else (None,)
                for quality in qualities:
                    output = io.BytesIO()
                    save_options = {"format": output_format}
                    if output_format == "PNG":
                        save_options.update(optimize=True, compress_level=9)
                    elif output_format == "JPEG":
                        save_options.update(quality=quality, optimize=True, progressive=True)
                    else:
                        save_options.update(quality=quality, method=6)
                    image.save(output, **save_options)
                    compressed = output.getvalue()
                    if len(compressed) <= max_bytes:
                        image.close()
                        return compressed
                image.close()
    except (OSError, Image.DecompressionBombError) as error:
        raise ValueError(f"image could not be optimized: {error}") from error
    raise ValueError("image is still too large after resizing and re-encoding")


def _compress_steal_gif(data, max_bytes):
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as source:
            frame_count = getattr(source, "n_frames", 1)
            if frame_count < 2:
                raise ValueError("GIF is too large and cannot be optimized as an animation")

            frame_limit = 60
            stride = max(1, (frame_count + frame_limit - 1) // frame_limit)
            frame_indexes = list(range(0, frame_count, stride))
            durations = []
            for start in frame_indexes:
                duration = 0
                for index in range(start, min(start + stride, frame_count)):
                    source.seek(index)
                    duration += int(source.info.get("duration", 100))
                durations.append(max(20, duration))

            loop = source.info.get("loop", 0)
            for max_dimension in (720, 576, 448, 352, 256, 192, 128):
                frames = []
                for index in frame_indexes:
                    source.seek(index)
                    frame = source.convert("RGBA")
                    frame.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
                    frames.append(frame)
                output = io.BytesIO()
                frames[0].save(
                    output,
                    format="GIF",
                    save_all=True,
                    append_images=frames[1:],
                    optimize=True,
                    loop=loop,
                    duration=durations,
                    disposal=2,
                )
                for frame in frames:
                    frame.close()
                compressed = output.getvalue()
                if len(compressed) <= max_bytes:
                    return compressed
    except (OSError, Image.DecompressionBombError) as error:
        raise ValueError(f"GIF could not be optimized: {error}") from error
    raise ValueError("GIF is still too large after reducing animation frames and dimensions")


async def _steal_selected_assets(guild, source, mode):
    emojis, stickers, images, sticker_images = _steal_message_assets(source)
    emoji_sources = emojis + images
    sticker_sources = stickers + sticker_images
    if mode == "emoji_one":
        emoji_sources, sticker_sources = emoji_sources[:1], []
    elif mode == "sticker_one":
        emoji_sources, sticker_sources = [], sticker_sources[:1]
    elif mode == "all_emoji":
        sticker_sources = []
    elif mode == "all_stickers":
        emoji_sources = []

    created_assets = []
    failures = []
    existing_emojis = {emoji.name.casefold() for emoji in guild.emojis}
    existing_stickers = {sticker.name.casefold() for sticker in guild.stickers}
    async with aiohttp.ClientSession() as session:
        for asset in emoji_sources:
            try:
                data = await _download_steal_asset(session, guild, asset)
                name = _safe_stolen_name(asset["name"], "stolen_emoji", existing_emojis, 32)
                created = await guild.create_custom_emoji(
                    name=name,
                    image=data,
                    reason=f"Emoji/sticker steal requested by {source.author}",
                )
                created_assets.append(("emoji", created))
            except (aiohttp.ClientError, discord.HTTPException, OSError, ValueError) as error:
                failures.append(f"{asset['name']}: {error}")

        for asset in sticker_sources:
            try:
                data = await _download_steal_asset(session, guild, asset, for_sticker=True)
                name = _safe_stolen_name(asset["name"], "stolen_sticker", existing_stickers, 30)
                extension = Path(asset["filename"]).suffix or ".png"
                if extension.lower() in {".jpg", ".jpeg", ".webp"}:
                    extension = ".png"
                sticker_file = discord.File(io.BytesIO(data), filename=f"{name}{extension}")
                try:
                    created = await guild.create_sticker(
                        name=name,
                        description=asset.get("description", "")[:100],
                        emoji=asset.get("emoji", "🙂"),
                        file=sticker_file,
                        reason=f"Emoji/sticker steal requested by {source.author}",
                    )
                finally:
                    sticker_file.close()
                created_assets.append(("sticker", created))
            except (aiohttp.ClientError, discord.HTTPException, OSError, ValueError) as error:
                failures.append(f"{asset['name']}: {error}")
    return len(created_assets), failures, created_assets


def _build_steal_result_embed(created_assets, failures):
    emoji_lines = []
    sticker_lines = []
    preview_url = None
    for kind, asset in created_assets:
        name = getattr(asset, "name", "Asset")
        url = str(getattr(asset, "url", ""))
        if preview_url is None and url:
            preview_url = url
        if kind == "emoji":
            emoji_lines.append(f"{asset} `{name}`")
        else:
            sticker_lines.append(f"**{name}**\n{url}" if url else f"**{name}**")

    embed = discord.Embed(
        title="Steal Complete" if created_assets else "Nothing Added",
        description=f"Added **{len(created_assets)}** asset(s).",
        color=0xFFDD00,
    )
    if emoji_lines:
        embed.add_field(name="Emoji", value="\n".join(emoji_lines)[:1024], inline=False)
    if sticker_lines:
        embed.add_field(name="Sticker", value="\n".join(sticker_lines)[:1024], inline=False)
    if failures:
        embed.add_field(name="Could not add", value="\n".join(failures[:5])[:1024], inline=False)
    if preview_url:
        embed.set_image(url=preview_url)
    return embed


STEAL_MODES = (
    ("emoji_one", "Steal one emoji", "Add the first emoji or image"),
    ("sticker_one", "Steal one sticker", "Add the first sticker or image"),
    ("all_emoji", "Steal all emoji", "Add every emoji and image"),
    ("all_stickers", "Steal all stickers", "Add every sticker and compatible image"),
    ("both", "Steal emoji and sticker", "Add all available emoji, stickers, and images"),
)


class StealMenuSelect(discord.ui.Select):
    def __init__(self, source, invoker_id):
        self.source = source
        self.invoker_id = invoker_id
        options = [
            discord.SelectOption(label=label, description=description, value=value)
            for value, label, description in STEAL_MODES
        ]
        super().__init__(placeholder="Choose what to steal", min_values=1, max_values=1, options=options)

    async def callback(self, interaction):
        if interaction.user.id != self.invoker_id:
            return await interaction.response.send_message(
                "Only the command user can use this menu.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        _added, failures, created_assets = await _steal_selected_assets(
            interaction.guild, self.source, self.values[0]
        )
        self.disabled = True
        if interaction.message is not None:
            await edit_message_v2(interaction.message, view=self.view)
        await interaction.followup.send(
            embed=_build_steal_result_embed(created_assets, failures),
            ephemeral=True,
        )


class StealMenuView(discord.ui.View):
    def __init__(self, source, invoker_id):
        super().__init__(timeout=120)
        self.add_item(StealMenuSelect(source, invoker_id))


@bot.command(name="steal")
@commands.guild_only()
@commands.has_guild_permissions(manage_emojis_and_stickers=True)
async def steal_cmd(ctx):
    bot_member = ctx.guild.me
    if bot_member is None or not bot_member.guild_permissions.manage_emojis_and_stickers:
        return await ctx.send(f"{DOT} I need Manage Expressions to add emoji or stickers.")
    source = await _find_steal_source(ctx)
    if source is None:
        return await ctx.send(
            f"{DOT} Reply to a message with emoji, stickers, or images, use `,steal @user`, "
            "or attach an image with the command."
        )
    embed = discord.Embed(
        title="Steal Emoji or Sticker",
        description=f"Choose one of the five options for assets from {source.author.mention}.",
        color=0xFFDD00,
    )
    emoji_assets, sticker_assets, image_assets, _sticker_image_assets = _steal_message_assets(source)
    preview = next(iter(image_assets or sticker_assets or emoji_assets), None)
    if preview is not None:
        embed.set_image(url=preview["url"])
    await ctx.send(embed=embed, view=StealMenuView(source, ctx.author.id))


@bot.command(name="emoji", aliases=["einfo"])
async def emoji_cmd(ctx, *, arg: str | None = None):
    if not arg and not ctx.message.attachments:
        return await ctx.send(f"{DOT} Send an emoji: `,emoji <emoji>`")
    target = arg or ""
    m = EMOJI_RE.search(target)
    if m:
        animated = m.group(1) == "a"
        name = m.group(2)
        eid = m.group(3)
        ext = "gif" if animated else "png"
        url = f"https://cdn.discordapp.com/emojis/{eid}.{ext}?size=512&quality=lossless"
        embed = discord.Embed(color=0xffdd00, title=f":{name}:")
        embed.add_field(name="Name", value=f"`{name}`", inline=True)
        embed.add_field(name="ID",   value=f"`{eid}`",  inline=True)
        embed.add_field(name="Animated", value="Yes" if animated else "No", inline=True)
        embed.add_field(name="URL", value=f"[Click]({url})", inline=False)
        embed.set_image(url=url)
        return await ctx.send(embed=embed)
    # Plain unicode emoji or unknown
    embed = discord.Embed(color=0xffdd00, title="Emoji Info")
    embed.description = (
        f"{DOT} Raw: `{target}`\n"
        f"{DOT} Codepoints: `{' '.join(f'U+{ord(c):04X}' for c in target if c.strip())}`"
    )
    await ctx.send(embed=embed)

# ═══════════════════════════════════════════════════════════════════
# ─── BOT-WIDE USER BLACKLIST (Staff+) ──────────────────────────────
# ═══════════════════════════════════════════════════════════════════
@bot.command(name="blacklist", aliases=["bl"])
async def blacklist_user_cmd(ctx, action: str | None = None, *, rest: str | None = None):
    if not await is_bot_staff(ctx.author.id):
        return
    action = (action or "").lower()
    if action == "add":
        user, reason = await _resolve_prefix_member_argument_with_rest(ctx, rest)
        if not user:
            return await ctx.send(f"{DOT} Usage: `,blacklist add @user [reason]`")
        if user.id in PRIMARY_OWNER_IDS:
            return await ctx.send(f"{DOT} Cannot blacklist a primary owner.")
        reason = reason or "No reason"
        await bot.db.execute(
            "INSERT OR REPLACE INTO user_blacklist (user_id, reason, by_user, at) VALUES (?, ?, ?, ?)",
            (user.id, reason, ctx.author.id, datetime.datetime.now().isoformat())
        )
        await bot.db.commit()
        await ctx.send(embed=discord.Embed(
            color=0xffdd00,
            description=f"{DOT} {user.mention} has been **blacklisted** from the bot.\n{ARROW} Reason: {reason}"
        ))
    elif action == "remove":
        user = await _resolve_prefix_member_argument(ctx, rest)
        if not user:
            return await ctx.send(f"{DOT} Usage: `,blacklist remove @user`")
        await bot.db.execute("DELETE FROM user_blacklist WHERE user_id = ?", (user.id,))
        await bot.db.commit()
        await ctx.send(embed=discord.Embed(
            color=0xffdd00, description=f"{DOT} {user.mention} has been removed from the blacklist."
        ))
    elif action == "list":
        async with bot.db.execute("SELECT user_id, reason FROM user_blacklist") as cur:
            rows = await cur.fetchall()
        if not rows:
            return await ctx.send(f"{DOT} No blacklisted users.")
        desc = "\n".join([f"{ARROW} <@{uid}> — {reason}" for uid, reason in rows])
        await ctx.send(embed=discord.Embed(color=0xffdd00, title="Bot Blacklist", description=desc))
    else:
        await ctx.send(embed=discord.Embed(
            color=0xffdd00,
            description=f"{DOT} Usage:\n{ARROW} `,blacklist add @user [reason]`\n{ARROW} `,blacklist remove @user`\n{ARROW} `,blacklist list`"
        ))

# ═══════════════════════════════════════════════════════════════════
# ─── PREMIUM ADD/REMOVE (Owner) ────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════
@bot.command(name="premiumadd", aliases=["padd"])
async def premium_add_cmd(ctx, user: discord.Member = None, days: int = 30):
    if not await is_bot_owner(ctx.author.id):
        return
    if not user:
        return await ctx.send(f"{DOT} Usage: `,premiumadd @user [days=30]`")
    expiry = datetime.datetime.now() + datetime.timedelta(days=days)
    await bot.db.execute(
        "INSERT OR REPLACE INTO premium_users (user_id, expiry) VALUES (?, ?)",
        (user.id, expiry.isoformat())
    )
    await bot.db.commit()
    await ctx.send(embed=discord.Embed(
        color=0xffdd00,
        description=f"{DOT} {user.mention} now has **Premium** for **{days} days** (until <t:{int(expiry.timestamp())}:F>)."
    ))

@bot.command(name="premiumremove", aliases=["premrm"])
async def premium_remove_cmd(ctx, user: discord.Member = None):
    if not await is_bot_owner(ctx.author.id):
        return
    if not user:
        return await ctx.send(f"{DOT} Usage: `,premiumremove @user`")
    await bot.db.execute("DELETE FROM premium_users WHERE user_id = ?", (user.id,))
    await bot.db.commit()
    await ctx.send(embed=discord.Embed(
        color=0xffdd00, description=f"{DOT} Removed Premium from {user.mention}."
    ))

# ═══════════════════════════════════════════════════════════════════
# ─── CRYPTO TOOLS (CoinGecko, no key) ──────────────────────────────
# ═══════════════════════════════════════════════════════════════════
COIN_ALIAS = {
    "btc":"bitcoin","eth":"ethereum","sol":"solana","doge":"dogecoin",
    "xrp":"ripple","ada":"cardano","bnb":"binancecoin","ltc":"litecoin",
    "matic":"matic-network","dot":"polkadot","trx":"tron","shib":"shiba-inu",
    "avax":"avalanche-2","link":"chainlink","atom":"cosmos","ton":"the-open-network",
}

@bot.command(name="crypto", aliases=["price"])
async def crypto_cmd(ctx, symbol: str | None = None):
    if not symbol:
        return await ctx.send(f"{DOT} Usage: `,crypto <symbol>`  e.g. `,crypto btc`")
    coin_id = COIN_ALIAS.get(symbol.lower(), symbol.lower())
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}?localization=false&tickers=false&community_data=false&developer_data=false"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=15) as resp:
                if resp.status != 200:
                    return await ctx.send(f"{DOT} Coin `{symbol}` not found.")
                data = await resp.json()
        md = data.get("market_data", {})
        price = md.get("current_price", {}).get("usd", "?")
        change_24h = md.get("price_change_percentage_24h", 0) or 0
        mcap = md.get("market_cap", {}).get("usd", 0) or 0
        vol = md.get("total_volume", {}).get("usd", 0) or 0
        embed = discord.Embed(
            color=0xffdd00,
            title=f"{data.get('name','?')} ({data.get('symbol','').upper()})",
            url=f"https://www.coingecko.com/en/coins/{coin_id}"
        )
        thumb = (data.get("image") or {}).get("large")
        if thumb: embed.set_thumbnail(url=thumb)
        sign = "▲" if change_24h >= 0 else "▼"
        embed.add_field(name="Price (USD)", value=f"${price:,}", inline=True)
        embed.add_field(name="24h Change", value=f"{sign} {change_24h:.2f}%", inline=True)
        embed.add_field(name="Market Cap", value=f"${mcap:,.0f}", inline=True)
        embed.add_field(name="24h Volume", value=f"${vol:,.0f}", inline=True)
        rank = md.get("market_cap_rank")
        if rank: embed.add_field(name="Rank", value=f"#{rank}", inline=True)
        await ctx.send(embed=embed)
    except asyncio.TimeoutError:
        await ctx.send(f"{DOT} CoinGecko timeout.")
    except Exception as e:
        await ctx.send(f"{DOT} Error: {e}")

# ═══════════════════════════════════════════════════════════════════
@bot.command(name="eventlog", aliases=["eventlogs", "setup_logs"])
@commands.guild_only()
@commands.has_guild_permissions(manage_guild=True)
async def eventlog_cmd(ctx, action: str = "status", channel: discord.TextChannel = None):
    action = action.lower()
    if action == "status":
        async with bot.db.execute(
            "SELECT enabled, channel_id FROM guild_event_log_settings WHERE guild_id=?",
            (ctx.guild.id,),
        ) as cursor:
            settings = await cursor.fetchone()
        log_channel = ctx.guild.get_channel(settings[1]) if settings and settings[1] else None
        enabled = bool(settings and settings[0] and log_channel)
        return await ctx.send(
            f"{DOT} Event logging is **{'enabled' if enabled else 'disabled'}**"
            f"{f' in {log_channel.mention}' if log_channel else ''}.\n"
            "Events: member join/leave/kick/ban/unban/timeout, message edit/delete, "
            "voice changes, and channel/role create/update/delete."
        )
    if action in {"enable", "on"}:
        if channel is None:
            try:
                channel = await ctx.guild.create_text_channel(
                    "falcon-event-logs",
                    reason="Enable Falcon guild event logging",
                )
            except (discord.Forbidden, discord.HTTPException) as error:
                return await ctx.send(
                    f"{DOT} Mention a text channel or grant Manage Channels to create the event log channel: {error}"
                )
        await bot.db.execute(
            "INSERT INTO guild_event_log_settings (guild_id, enabled, channel_id) VALUES (?, 1, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET enabled=1, channel_id=excluded.channel_id",
            (ctx.guild.id, channel.id),
        )
        await bot.db.commit()
        return await ctx.send(f"{DOT} Event logging enabled in {channel.mention}.")
    if action == "channel":
        if channel is None:
            return await ctx.send(f"{DOT} Usage: `,eventlog channel #text-channel`.")
        await bot.db.execute(
            "INSERT INTO guild_event_log_settings (guild_id, enabled, channel_id) VALUES (?, 1, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET enabled=1, channel_id=excluded.channel_id",
            (ctx.guild.id, channel.id),
        )
        await bot.db.commit()
        return await ctx.send(f"{DOT} Event logs will now be sent to {channel.mention}.")
    if action in {"disable", "off"}:
        await bot.db.execute(
            "INSERT INTO guild_event_log_settings (guild_id, enabled) VALUES (?, 0) "
            "ON CONFLICT(guild_id) DO UPDATE SET enabled=0",
            (ctx.guild.id,),
        )
        await bot.db.commit()
        return await ctx.send(f"{DOT} Event logging disabled.")
    return await ctx.send(
        f"{DOT} Usage: `,eventlog enable [#channel]`, `,eventlog channel #channel`, "
        "`,eventlog disable`, or `,eventlog status`."
    )


# ─── RUN ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    TOKEN = _get_effective_discord_token()
    if not TOKEN:
        raise RuntimeError("Missing Discord token. Set DISCORD_TOKEN or BOT_TOKEN in your environment before starting the bot.")
    if not all(part for part in TOKEN.split(".")) or len(TOKEN.split(".")) != 3:
        raise RuntimeError(
            "DISCORD_TOKEN looks invalid. Copy the exact bot token from the Discord Developer Portal "
            "(Application -> Bot -> Reset Token or Copy) and paste it into .env."
        )
    bot.run(TOKEN)
