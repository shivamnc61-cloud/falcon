"""VoiceMaster rooms and scheduled, non-destructive server-structure backups."""

import asyncio
import datetime
import io
import json
import logging
import os
import random
import re
from pathlib import Path

import aiohttp
import aiosqlite
import discord
import yaml
from discord import app_commands
from discord.ext import commands, tasks
from PIL import Image, ImageDraw, ImageFont

from cluster_runtime import bot_owns_guild
from components_v2 import edit_interaction_v2, edit_message_v2


log = logging.getLogger(__name__)
UTC = datetime.timezone.utc
BACKUP_INTERVAL = datetime.timedelta(hours=1)
MAX_BACKUP_BYTES = 8 * 1024 * 1024
RTC_REGIONS = (
    "auto", "us-east", "us-west", "us-central", "us-south", "europe",
    "singapore", "sydney", "brazil", "hongkong", "japan", "india",
)
ROOM_NAME_EMOJIS = ("🎧", "🌿", "✨", "🌙", "🌊", "🌸", "🍀", "⭐")
ROOM_JOIN_LEAVE_MESSAGES = os.getenv("ROOM_JOIN_LEAVE_MESSAGES", "false").strip().lower() in {
    "1", "true", "yes", "on",
}
TEMPVOICE_CREATION_COOLDOWN = 5.0
MAX_TEMP_ROOMS_PER_GUILD = 50
ROOM_PANEL_BUTTONS_PER_ROW = 5
ROOM_PANEL_BUTTONS = (
    ("lock", "Lock Room", "lock", discord.ButtonStyle.secondary, False),
    ("unlock", "Unlock Room", "unlock", discord.ButtonStyle.secondary, False),
    ("secure", "Secure Lock", "lock", discord.ButtonStyle.secondary, False),
    ("private", "Private Room", "hide", discord.ButtonStyle.secondary, False),
    ("hide", "Hide Room", "hide", discord.ButtonStyle.secondary, False),
    ("unhide", "Unhide Room", "unhide", discord.ButtonStyle.secondary, False),
    ("unban", "Unblock Member", "unblock", discord.ButtonStyle.secondary, True),
    ("mute", "Mute Member", "mute", discord.ButtonStyle.secondary, True),
    ("unmute", "Unmute Member", "unmute", discord.ButtonStyle.secondary, True),
    ("block", "Block Member", "block", discord.ButtonStyle.secondary, True),
    ("ban", "Ban Member", "ban", discord.ButtonStyle.secondary, True),
    ("permit", "Permit Member", "permit", discord.ButtonStyle.secondary, True),
    ("claim", "Claim Ownership", "claim", discord.ButtonStyle.secondary, False),
    ("transfer", "Transfer Ownership", "transfer", discord.ButtonStyle.secondary, True),
    ("rename", "Rename Room", "rename", discord.ButtonStyle.secondary, True),
    ("limit", "Set User Limit", "limit", discord.ButtonStyle.secondary, True),
    ("delete", "Delete Room", "delete", discord.ButtonStyle.secondary, False),
    ("setting", "Reset Room Settings", "reset", discord.ButtonStyle.secondary, False),
    ("invite", "Invite Member", "invite", discord.ButtonStyle.secondary, True),
    ("kick", "Kick Member", "kick", discord.ButtonStyle.secondary, True),
    ("chat", "Open Room Chat", "openchat", discord.ButtonStyle.secondary, False),
    ("announce", "Create Room Invite", "invite", discord.ButtonStyle.secondary, True),
    ("info", "Room Status", "status", discord.ButtonStyle.secondary, False),
    ("premium", "Trust Member", "trust", discord.ButtonStyle.secondary, True),
    ("fast", "Refresh Room Panel", "refresh", discord.ButtonStyle.secondary, False),
    ("active", "Close Room Chat", "closechat", discord.ButtonStyle.secondary, False),
    ("voice_support", "Room Information", "status", discord.ButtonStyle.secondary, False),
    ("unlimited", "Set Unlimited User Limit", "limit", discord.ButtonStyle.secondary, True),
    ("love", "Untrust Member", "untrust", discord.ButtonStyle.secondary, True),
    ("start", "Start Room Chat", "openchat", discord.ButtonStyle.secondary, False),
)


def _load_voice_button_emojis():
    manifest_path = Path(__file__).resolve().parent / "emojis.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not load TempVoice emoji manifest: {manifest_path}") from error
    configured = manifest.get("voice_buttons") if isinstance(manifest, dict) else None
    if not isinstance(configured, dict):
        raise RuntimeError("emojis.json must contain a voice_buttons mapping.")

    emojis = {}
    for key in {button[0] for button in ROOM_PANEL_BUTTONS}:
        value = configured.get(key)
        emoji = discord.PartialEmoji.from_str(value) if isinstance(value, str) else None
        if emoji is None or emoji.id is None:
            raise RuntimeError(f"emojis.json is missing the custom TempVoice emoji {key!r}.")
        emojis[key] = emoji
    return emojis


VOICE_BUTTON_EMOJIS = _load_voice_button_emojis()


def _placeholder_emoji_bytes(label: str = "?") -> bytes:
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((4, 4, 59, 59), radius=14, fill=(32, 38, 47, 255), outline=(146, 154, 168, 255), width=2)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 22)
    except OSError:
        font = ImageFont.load_default()
    text = str(label or "?")[:1]
    bbox = draw.textbbox((0, 0), text, font=font)
    text_x = (image.width - (bbox[2] - bbox[0])) // 2
    text_y = (image.height - (bbox[3] - bbox[1])) // 2
    draw.text((text_x, text_y), text, font=font, fill=(245, 247, 250, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _render_room_panel_card(emoji_images, heading, subtitle):
    width = 1600
    padding = 44
    columns = 3
    column_gap = 16
    tile_width = (width - 2 * padding - (columns - 1) * column_gap) // columns
    tile_height = 92
    row_gap = 14
    rows = (len(ROOM_PANEL_BUTTONS) + columns - 1) // columns
    content_top = 150
    height = content_top + rows * tile_height + max(0, rows - 1) * row_gap + padding
    image = Image.new("RGB", (width, height), "#FFF9F5")
    draw = ImageDraw.Draw(image)
    background_stops = (
        (0.0, (255, 249, 235)),
        (0.34, (255, 221, 226)),
        (0.68, (250, 199, 215)),
        (1.0, (255, 237, 207)),
    )
    for y in range(height):
        position = y / max(1, height - 1)
        for stop_index in range(len(background_stops) - 1):
            start_at, start_color = background_stops[stop_index]
            end_at, end_color = background_stops[stop_index + 1]
            if position <= end_at:
                blend = (position - start_at) / (end_at - start_at)
                color = tuple(
                    round(
                        start_color[channel] * (1 - blend)
                        + end_color[channel] * blend
                    )
                    for channel in range(3)
                )
                break
        draw.line((0, y, width, y), fill=color)

    draw.rounded_rectangle(
        (24, 20, width - 24, height - 20),
        radius=38,
        fill="#FFFEFC",
        outline="#BC8430",
        width=4,
    )
    draw.rounded_rectangle(
        (38, 34, width - 38, height - 34),
        radius=29,
        outline="#F2D8C8",
        width=2,
    )

    try:
        title_font = ImageFont.truetype("DejaVuSans-Bold.ttf", 35)
        command_font = ImageFont.truetype("DejaVuSans-Bold.ttf", 20)
        subtitle_font = ImageFont.truetype("DejaVuSans.ttf", 18)
    except OSError:
        title_font = ImageFont.load_default()
        command_font = title_font
        subtitle_font = title_font

    def draw_centered(text, y, font, fill, max_width):
        safe_text = str(text or "").strip()
        while safe_text and draw.textbbox((0, 0), safe_text, font=font)[2] > max_width:
            safe_text = safe_text[:-2].rstrip() + "…"
        text_width = draw.textbbox((0, 0), safe_text, font=font)[2]
        draw.text(((width - text_width) // 2, y), safe_text, font=font, fill=fill)

    draw_centered(heading, 25, title_font, "#85223B", width - 2 * padding)
    draw_centered(subtitle, 80, subtitle_font, "#8B5C5B", width - 2 * padding)
    draw.line((padding, 128, width - padding, 128), fill="#E7C78F", width=2)

    for button_index, (emoji_key, name, *_) in enumerate(ROOM_PANEL_BUTTONS):
        row, column = divmod(button_index, columns)
        left = padding + column * (tile_width + column_gap)
        top = content_top + row * (tile_height + row_gap)
        draw.rounded_rectangle(
            (left, top, left + tile_width, top + tile_height),
            radius=18,
            fill="#FFF9F2",
            outline="#E7C78F",
            width=2,
        )
        draw.rounded_rectangle(
            (left + 1, top + 1, left + 9, top + tile_height - 1),
            radius=4,
            fill="#E98BA4",
        )
        icon_badge_left = left + 16
        icon_badge_top = top + (tile_height - 56) // 2
        draw.ellipse(
            (
                icon_badge_left,
                icon_badge_top,
                icon_badge_left + 56,
                icon_badge_top + 56,
            ),
            fill="#FFE7E8",
            outline="#D5A252",
            width=2,
        )
        with Image.open(io.BytesIO(emoji_images[emoji_key])) as emoji_source:
            emoji_image = emoji_source.convert("RGBA")
        emoji_image.thumbnail((44, 44), Image.Resampling.LANCZOS)
        emoji_y = top + (tile_height - emoji_image.height) // 2
        image.paste(emoji_image, (left + 22, emoji_y), emoji_image)

        fitted_name = name
        max_label_width = tile_width - 92
        while (
            fitted_name
            and draw.textbbox((0, 0), fitted_name, font=command_font)[2]
            > max_label_width
        ):
            fitted_name = fitted_name[:-2].rstrip() + "…"
        label_bbox = draw.textbbox((0, 0), fitted_name, font=command_font)
        label_y = top + (tile_height - (label_bbox[3] - label_bbox[1])) // 2 - label_bbox[1]
        draw.text(
            (left + 82, label_y),
            fitted_name,
            font=command_font,
            fill="#5D2636",
        )

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _format_room_member_mentions(mentions):
    if not mentions:
        return "None"

    displayed = []
    for mention in mentions:
        candidate = ", ".join((*displayed, mention))
        remaining = len(mentions) - len(displayed) - 1
        suffix = f" … and {remaining} more" if remaining else ""
        if len(candidate) + len(suffix) > 1024:
            break
        displayed.append(mention)

    if len(displayed) == len(mentions):
        return ", ".join(displayed)

    suffix = f" … and {len(mentions) - len(displayed)} more"
    if displayed:
        return f"{', '.join(displayed)}{suffix}"
    return f"{len(mentions)} members (list shortened)"


DEFAULT_TEMPVOICE_CONFIG = {
    "tempvoice": {
        "default_channel_name": "{display_name}'s room",
        "default_user_limit": 0,
        "default_bitrate_kbps": 64,
        "default_region": "auto",
        "auto_delete_empty": True,
        "ownership_transfer_policy": "claim_after_owner_leaves",
        "claim_timeout_seconds": 0,
        "auto_transfer_owner": False,
        "staff_role_ids": [],
        "allowed_controls": [
            "lock", "unlock", "hide", "unhide", "rename", "limit", "bitrate", "region",
            "status", "reset", "delete", "refresh", "permit", "allow", "reject", "trust",
            "untrust", "block", "unblock", "kick", "mute", "unmute", "deafen", "undeafen",
            "transfer", "claim", "invite", "summon", "openchat", "closechat", "clear",
        ],
        "creation_cooldown_seconds": TEMPVOICE_CREATION_COOLDOWN,
        "max_temp_channels_per_guild": MAX_TEMP_ROOMS_PER_GUILD,
        "audit_logging": True,
        "theme": {"embed_color": "ffdd00"},
        "localization": "en",
    }
}


def load_tempvoice_config(path=None):
    config_path = Path(path or Path(__file__).resolve().parent / "config.yml")
    try:
        with config_path.open("r", encoding="utf-8") as config_file:
            loaded = yaml.safe_load(config_file) or {}
    except FileNotFoundError:
        loaded = {}
    except (OSError, yaml.YAMLError):
        log.exception("Could not load TempVoice config from %s; using defaults", config_path)
        loaded = {}
    if not isinstance(loaded, dict):
        log.warning("TempVoice config root must be a mapping; using defaults")
        loaded = {}

    merged = json.loads(json.dumps(DEFAULT_TEMPVOICE_CONFIG))
    supplied = loaded.get("tempvoice", {})
    if not isinstance(supplied, dict):
        log.warning("TempVoice config section must be a mapping; using defaults")
        supplied = {}
    for key, value in supplied.items():
        if key == "theme":
            if isinstance(value, dict):
                merged["tempvoice"]["theme"].update(value)
            else:
                log.warning("TempVoice theme must be a mapping; using defaults")
        elif key in merged["tempvoice"]:
            merged["tempvoice"][key] = value

    settings = merged["tempvoice"]
    integer_settings = (
        ("default_user_limit", 0, 99),
        ("default_bitrate_kbps", 8, 384),
        ("creation_cooldown_seconds", 0, 60),
        ("max_temp_channels_per_guild", 1, 500),
        ("claim_timeout_seconds", 0, 86400),
    )
    for key, minimum, maximum in integer_settings:
        value = settings[key]
        try:
            parsed = int(value) if not isinstance(value, bool) else None
        except (TypeError, ValueError):
            parsed = None
        if parsed is None:
            log.warning("Invalid TempVoice setting %s; using its default", key)
            parsed = DEFAULT_TEMPVOICE_CONFIG["tempvoice"][key]
        settings[key] = max(minimum, min(maximum, parsed))
    settings["default_region"] = str(settings["default_region"]).casefold()
    if settings["default_region"] not in RTC_REGIONS:
        log.warning("Invalid TempVoice default_region; using auto")
        settings["default_region"] = "auto"
    for key in ("auto_delete_empty", "auto_transfer_owner", "audit_logging"):
        if not isinstance(settings[key], bool):
            log.warning("Invalid TempVoice setting %s; using its default", key)
            settings[key] = DEFAULT_TEMPVOICE_CONFIG["tempvoice"][key]
    staff_role_ids = settings["staff_role_ids"]
    if not isinstance(staff_role_ids, list):
        log.warning("TempVoice staff_role_ids must be a list; using no global staff roles")
        staff_role_ids = []
    settings["staff_role_ids"] = [
        int(role_id)
        for role_id in staff_role_ids
        if (isinstance(role_id, int) and not isinstance(role_id, bool) and role_id > 0)
        or (isinstance(role_id, str) and role_id.isdigit() and int(role_id) > 0)
    ]
    supported_controls = {
        "lock", "unlock", "hide", "unhide", "rename", "limit", "bitrate", "region",
        "status", "reset", "delete", "refresh", "permit", "allow", "reject", "trust",
        "untrust", "block", "unblock", "kick", "mute", "unmute", "deafen", "undeafen",
        "transfer", "claim", "invite", "summon", "openchat", "closechat", "clear",
    }
    controls = settings["allowed_controls"]
    if not isinstance(controls, list):
        log.warning("TempVoice allowed_controls must be a list; using defaults")
        controls = DEFAULT_TEMPVOICE_CONFIG["tempvoice"]["allowed_controls"]
    settings["allowed_controls"] = list(dict.fromkeys(
        action for action in controls
        if isinstance(action, str) and action in supported_controls
    ))
    if not isinstance(settings["ownership_transfer_policy"], str) or settings[
        "ownership_transfer_policy"
    ] not in {"claim_after_owner_leaves", "claim_after_timeout"}:
        settings["ownership_transfer_policy"] = "claim_after_owner_leaves"
    if not isinstance(settings["default_channel_name"], str) or not settings["default_channel_name"].strip():
        settings["default_channel_name"] = DEFAULT_TEMPVOICE_CONFIG["tempvoice"]["default_channel_name"]
    color = str(settings["theme"].get("embed_color", "ffdd00")).strip().removeprefix("#")
    try:
        parsed_color = int(color, 16)
        if not 0 <= parsed_color <= 0xFFFFFF:
            raise ValueError("Color must be a 24-bit RGB value.")
        settings["theme"]["embed_color"] = parsed_color
    except ValueError:
        settings["theme"]["embed_color"] = 0xFFDD00
    return merged


TEMPVOICE_CONFIG = load_tempvoice_config()["tempvoice"]
TEMPVOICE_CREATION_COOLDOWN = TEMPVOICE_CONFIG["creation_cooldown_seconds"]
MAX_TEMP_ROOMS_PER_GUILD = TEMPVOICE_CONFIG["max_temp_channels_per_guild"]


def _now():
    return datetime.datetime.now(UTC)


def _as_utc(value):
    parsed = datetime.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _has_text_channel_permissions(channel, member):
    permissions = channel.permissions_for(member)
    return (
        permissions.view_channel
        and permissions.send_messages
        and permissions.embed_links
        and permissions.attach_files
    )


def _format_room_name(name):
    cleaned = re.sub(r"[\r\n]+", " ", str(name or "")).strip()
    if not cleaned:
        cleaned = "Temporary room"
    prefix, separator, remainder = cleaned.partition(" ")
    if prefix in ROOM_NAME_EMOJIS and separator:
        cleaned = remainder.strip() or "Temporary room"
    return f"{random.choice(ROOM_NAME_EMOJIS)} {cleaned}"[:100]


class RoomModal(discord.ui.Modal):
    def __init__(self, cog, action):
        super().__init__(
            title={
                "rename": "Rename your room",
                "limit": "Set room user limit",
                "bitrate": "Set room bitrate",
                "region": "Set voice region",
                "ban": "Ban a member from your room",
                "unblock": "Unblock a room member",
                "reject": "Reject a member from your room",
                "trust": "Trust a room member",
                "untrust": "Untrust a room member",
                "mute": "Mute a room member",
                "unmute": "Unmute a room member",
                "deafen": "Deafen a room member",
                "undeafen": "Undeafen a room member",
                "summon": "Summon a member",
                "allow": "Allow a member",
                "block": "Block a member",
                "transfer": "Transfer room ownership",
                "permit": "Permit a member",
                "kick": "Remove a member",
                "invite": "Invite a room member",
                "clear": "Clear room access overrides",
            }[action]
        )
        self.cog = cog
        self.action = action
        if action == "rename":
            self.value = discord.ui.TextInput(label="New room name", max_length=100)
        elif action == "limit":
            self.value = discord.ui.TextInput(label="User limit (0-99; 0 means unlimited)", max_length=2)
        elif action == "bitrate":
            self.value = discord.ui.TextInput(
                label="Bitrate in kbps",
                max_length=3,
                placeholder="8-384, based on server boost level",
            )
        elif action == "region":
            self.value = discord.ui.TextInput(
                label="RTC region",
                max_length=20,
                placeholder="auto, us-east, europe, ...",
            )
        elif action == "clear":
            self.value = discord.ui.TextInput(
                label="Member ID or mention (leave blank for all)",
                max_length=30,
                required=False,
                placeholder="Leave blank to clear every room override",
            )
        else:
            self.value = discord.ui.TextInput(
                label="Member ID or mention",
                max_length=30,
                placeholder="For example: 123456789012345678",
            )
        self.add_item(self.value)

    async def on_submit(self, interaction):
        await self.cog._handle_room_interaction(interaction, self.action, str(self.value.value))


class VoiceRoomPanel(discord.ui.LayoutView):
    """Persistent controls resolve the member's current managed room on each click."""

    def __init__(self, cog, card_image=None, description=None):
        super().__init__(timeout=None)
        self.cog = cog
        self.card_image = card_image
        self.card_filename = "tempvoice-room-panel.png"
        container = discord.ui.Container(accent_color=0xFFDD00)
        container.add_item(discord.ui.TextDisplay(
            "# FALCON / VOICE ROOM STUDIO\n"
            "Use the controls below to manage your temporary voice room. "
            "More options are available with **/voice** commands."
        ))
        if card_image is not None:
            gallery = discord.ui.MediaGallery()
            gallery.add_item(
                media=f"attachment://{self.card_filename}",
                description=description,
            )
            container.add_item(gallery)
        self.add_item(container)

        for start in range(0, len(ROOM_PANEL_BUTTONS), ROOM_PANEL_BUTTONS_PER_ROW):
            buttons = []
            for emoji_key, _, action, _, needs_modal in ROOM_PANEL_BUTTONS[
                start:start + ROOM_PANEL_BUTTONS_PER_ROW
            ]:
                button = discord.ui.Button(
                    emoji=VOICE_BUTTON_EMOJIS[emoji_key],
                    style=discord.ButtonStyle.secondary,
                    custom_id=f"denycord:room:{action}:{emoji_key}",
                )
                button.style = discord.ButtonStyle.secondary

                async def callback(
                    interaction,
                    selected_action=action,
                    use_modal=needs_modal,
                ):
                    if use_modal:
                        await interaction.response.send_modal(
                            RoomModal(self.cog, selected_action)
                        )
                    else:
                        await self.cog._handle_room_interaction(
                            interaction, selected_action
                        )
                button.callback = callback
                buttons.append(button)
            self.add_item(discord.ui.ActionRow(*buttons))


class DeleteRoomConfirmationView(discord.ui.View):
    def __init__(self, cog, requester_id):
        super().__init__(timeout=45)
        self.cog = cog
        self.requester_id = requester_id

    @discord.ui.button(style=discord.ButtonStyle.secondary, emoji=VOICE_BUTTON_EMOJIS["delete"])
    async def confirm(self, interaction, _):
        if interaction.user.id != self.requester_id:
            return await interaction.response.send_message(
                "Only the person who requested deletion can confirm it.", ephemeral=True
            )
        await self.cog._handle_room_interaction(interaction, "delete", "confirmed")
        self.stop()

    @discord.ui.button(style=discord.ButtonStyle.secondary, emoji=VOICE_BUTTON_EMOJIS["voice_support"])
    async def cancel(self, interaction, _):
        await edit_interaction_v2(
            interaction,
            content="Room deletion cancelled.",
            embed=None,
            view=None,
        )
        self.stop()


class DenycordVoiceBackup(commands.Cog):
    voice = app_commands.Group(
        name="voice",
        description="Manage your temporary voice room",
    )
    voice_settings = app_commands.Group(
        name="settings",
        description="Configure advanced temporary voice room settings",
        parent=voice,
    )
    tempvoice = app_commands.Group(
        name="tempvoice",
        description="Configure temporary voice rooms for this server",
    )

    def __init__(self, bot):
        self.bot = bot
        self._voice_emoji_cache = {}
        self._voice_emoji_cache_lock = asyncio.Lock()
        self.room_panel = VoiceRoomPanel(self)
        self._creation_locks = {}
        self._creation_cooldowns = {}
        self._rooms_being_created = set()
        self._room_panels_reconciled = False

    async def _run_voice_app_action(self, interaction, action, value=None):
        await self._handle_room_interaction(interaction, action, value)

    @voice.command(name="name", description="Rename your temporary voice room")
    async def voice_name(self, interaction: discord.Interaction, name: str):
        await self._run_voice_app_action(interaction, "rename", name)

    @voice.command(name="limit", description="Set the room's user limit; use zero for unlimited")
    async def voice_limit(self, interaction: discord.Interaction, limit: app_commands.Range[int, 0, 99]):
        await self._run_voice_app_action(interaction, "limit", limit)

    @voice_settings.command(name="bitrate", description="Set your room's bitrate in kbps")
    async def voice_bitrate(self, interaction: discord.Interaction, bitrate_kbps: app_commands.Range[int, 8, 384]):
        await self._run_voice_app_action(interaction, "bitrate", bitrate_kbps)

    @voice.command(name="lock", description="Prevent other members from joining your room")
    async def voice_lock(self, interaction: discord.Interaction):
        await self._run_voice_app_action(interaction, "lock")

    @voice.command(name="unlock", description="Allow members to join your room")
    async def voice_unlock(self, interaction: discord.Interaction):
        await self._run_voice_app_action(interaction, "unlock")

    @voice.command(name="hide", description="Hide your room from other members")
    async def voice_hide(self, interaction: discord.Interaction):
        await self._run_voice_app_action(interaction, "hide")

    @voice.command(name="unhide", description="Make your room visible again")
    async def voice_unhide(self, interaction: discord.Interaction):
        await self._run_voice_app_action(interaction, "unhide")

    @voice_settings.command(name="region", description="Set your room's RTC region")
    async def voice_region(self, interaction: discord.Interaction, region: str):
        await self._run_voice_app_action(interaction, "region", region)

    @voice.command(name="status", description="Show your room's current settings and access rules")
    async def voice_status(self, interaction: discord.Interaction):
        await self._run_voice_app_action(interaction, "status")

    @voice_settings.command(name="textchat", description="Open or close text chat in your voice room")
    async def voice_textchat(self, interaction: discord.Interaction, enabled: bool):
        await self._run_voice_app_action(interaction, "openchat" if enabled else "closechat")

    @voice.command(name="delete", description="Delete your temporary voice room")
    async def voice_delete(self, interaction: discord.Interaction):
        await self._run_voice_app_action(interaction, "delete")

    @voice.command(name="reset", description="Reset your room's settings and member overrides")
    async def voice_reset(self, interaction: discord.Interaction):
        await self._run_voice_app_action(interaction, "reset")

    @voice.command(name="interface", description="Refresh your room's control panel")
    async def voice_interface(self, interaction: discord.Interaction):
        await self._run_voice_app_action(interaction, "refresh")

    @voice.command(name="permit", description="Allow a member to join your room")
    async def voice_member_permit(self, interaction: discord.Interaction, member: discord.Member):
        await self._run_voice_app_action(interaction, "permit", member.id)

    @voice.command(name="reject", description="Reject a member from your room")
    async def voice_member_reject(self, interaction: discord.Interaction, member: discord.Member):
        await self._run_voice_app_action(interaction, "reject", member.id)

    @voice.command(name="trust", description="Trust a member in your room")
    async def voice_member_trust(self, interaction: discord.Interaction, member: discord.Member):
        await self._run_voice_app_action(interaction, "trust", member.id)

    @voice.command(name="untrust", description="Remove a member's trusted status")
    async def voice_member_untrust(self, interaction: discord.Interaction, member: discord.Member):
        await self._run_voice_app_action(interaction, "untrust", member.id)

    @voice.command(name="block", description="Block a member from your room")
    async def voice_member_block(self, interaction: discord.Interaction, member: discord.Member):
        await self._run_voice_app_action(interaction, "block", member.id)

    @voice.command(name="unblock", description="Remove a member's room block")
    async def voice_member_unblock(self, interaction: discord.Interaction, member: discord.Member):
        await self._run_voice_app_action(interaction, "unblock", member.id)

    @voice.command(name="kick", description="Disconnect a member from your room")
    async def voice_member_kick(self, interaction: discord.Interaction, member: discord.Member):
        await self._run_voice_app_action(interaction, "kick", member.id)

    @voice.command(name="mute", description="Mute a member in your room")
    async def voice_member_mute(self, interaction: discord.Interaction, member: discord.Member):
        await self._run_voice_app_action(interaction, "mute", member.id)

    @voice.command(name="unmute", description="Unmute a member in your room")
    async def voice_member_unmute(self, interaction: discord.Interaction, member: discord.Member):
        await self._run_voice_app_action(interaction, "unmute", member.id)

    @voice.command(name="deafen", description="Server-deafen a member in your room")
    async def voice_member_deafen(self, interaction: discord.Interaction, member: discord.Member):
        await self._run_voice_app_action(interaction, "deafen", member.id)

    @voice.command(name="undeafen", description="Remove server-deafen from a member")
    async def voice_member_undeafen(self, interaction: discord.Interaction, member: discord.Member):
        await self._run_voice_app_action(interaction, "undeafen", member.id)

    @voice.command(name="transfer", description="Transfer room ownership to a current member")
    async def voice_member_transfer(self, interaction: discord.Interaction, member: discord.Member):
        await self._run_voice_app_action(interaction, "transfer", member.id)

    @voice.command(name="claim", description="Claim a room when its claim policy allows")
    async def voice_member_claim(self, interaction: discord.Interaction):
        await self._run_voice_app_action(interaction, "claim")

    @voice.command(name="invite", description="Send a one-use room invite to a member")
    async def voice_member_invite(self, interaction: discord.Interaction, member: discord.Member):
        await self._run_voice_app_action(interaction, "invite", member.id)

    async def _tempvoice_admin_context(self, interaction):
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "TempVoice administration is only available in a server.",
                ephemeral=True,
            )
            return None, None
        if not member.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "You need Manage Server permission to configure TempVoice.",
                ephemeral=True,
            )
            return None, None
        return guild, member

    def _tempvoice_generator_is_ready(self, guild, channel):
        bot_member = guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_channels:
            return False
        if not bot_member.guild_permissions.move_members:
            return False
        permissions = channel.permissions_for(bot_member)
        return permissions.view_channel and permissions.connect and permissions.move_members

    @tempvoice.command(name="setup", description="Configure the generator, control panel, and category")
    async def tempvoice_setup(
        self,
        interaction: discord.Interaction,
        generator: discord.VoiceChannel,
        panel: discord.TextChannel,
        category: discord.CategoryChannel = None,
    ):
        guild, member = await self._tempvoice_admin_context(interaction)
        if guild is None:
            return
        category = category or generator.category
        if not self._tempvoice_generator_is_ready(guild, generator):
            return await interaction.response.send_message(
                "I need Manage Channels, Move Members, and View/Connect/Move Members access to the generator.",
                ephemeral=True,
            )
        if category is not None and category.guild.id != guild.id:
            return await interaction.response.send_message(
                "Choose a category from this server.",
                ephemeral=True,
            )
        panel_permissions = panel.permissions_for(guild.me)
        if not (
            panel_permissions.view_channel
            and panel_permissions.send_messages
            and panel_permissions.attach_files
        ):
            return await interaction.response.send_message(
                "I need View Channel, Send Messages, and Attach Files in the panel channel.",
                ephemeral=True,
            )
        async with self.bot.db.execute(
            "SELECT panel_channel_id, interface_message_id FROM denycord_voice_settings WHERE guild_id=?",
            (guild.id,),
        ) as cursor:
            previous = await cursor.fetchone()
        previous_message_id = (
            previous[1] if previous and previous[0] == panel.id else None
        )
        try:
            interface_message_id = await self._ensure_voice_interface(
                guild, panel, previous_message_id
            )
            await self.bot.db.execute(
                "INSERT INTO denycord_voice_settings "
                "(guild_id, hub_channel_id, panel_channel_id, category_id, interface_message_id, enabled) "
                "VALUES (?,?,?,?,?,1) ON CONFLICT(guild_id) DO UPDATE SET "
                "hub_channel_id=excluded.hub_channel_id, panel_channel_id=excluded.panel_channel_id, "
                "category_id=excluded.category_id, interface_message_id=excluded.interface_message_id, enabled=1",
                (guild.id, generator.id, panel.id, category.id if category else None, interface_message_id),
            )
            await self.bot.db.execute(
                "INSERT INTO denycord_voice_generators "
                "(guild_id, channel_id, category_id, created_by, created_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(guild_id, channel_id) DO UPDATE SET category_id=excluded.category_id",
                (guild.id, generator.id, category.id if category else None, member.id, _now().isoformat()),
            )
            await self.bot.db.commit()
        except (aiosqlite.Error, discord.HTTPException):
            await self.bot.db.rollback()
            log.exception("Could not configure TempVoice in guild %s", guild.id)
            return await interaction.response.send_message(
                "TempVoice setup failed. Check my channel permissions and try again.",
                ephemeral=True,
            )
        await interaction.response.send_message(
            f"TempVoice is ready. Join {generator.mention} to create a room; the control panel is in {panel.mention}.",
            ephemeral=True,
        )

    @tempvoice.command(name="set-generator", description="Add or update a join-to-create voice channel")
    async def tempvoice_set_generator(
        self,
        interaction: discord.Interaction,
        generator: discord.VoiceChannel,
        category: discord.CategoryChannel = None,
    ):
        guild, member = await self._tempvoice_admin_context(interaction)
        if guild is None:
            return
        if not self._tempvoice_generator_is_ready(guild, generator):
            return await interaction.response.send_message(
                "I need Manage Channels, Move Members, and View/Connect/Move Members access to that generator.",
                ephemeral=True,
            )
        async with self.bot.db.execute(
            "SELECT hub_channel_id FROM denycord_voice_settings WHERE guild_id=?",
            (guild.id,),
        ) as cursor:
            settings = await cursor.fetchone()
        if not settings:
            return await interaction.response.send_message(
                "Run `/tempvoice setup` first to configure a panel and initial generator.",
                ephemeral=True,
            )
        category_id = category.id if category else (
            generator.category.id if generator.category else None
        )
        await self.bot.db.execute(
            "INSERT INTO denycord_voice_generators "
            "(guild_id, channel_id, category_id, created_by, created_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(guild_id, channel_id) DO UPDATE SET category_id=excluded.category_id",
            (guild.id, generator.id, category_id, member.id, _now().isoformat()),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            f"{generator.mention} now creates temporary rooms.",
            ephemeral=True,
        )

    @tempvoice.command(name="set-category", description="Set or clear a generator's room category")
    async def tempvoice_set_category(
        self,
        interaction: discord.Interaction,
        generator: discord.VoiceChannel,
        category: discord.CategoryChannel = None,
    ):
        guild, _ = await self._tempvoice_admin_context(interaction)
        if guild is None:
            return
        async with self.bot.db.execute(
            "SELECT 1 FROM denycord_voice_generators WHERE guild_id=? AND channel_id=?",
            (guild.id, generator.id),
        ) as cursor:
            exists = await cursor.fetchone()
        if not exists:
            return await interaction.response.send_message(
                "That voice channel is not a configured generator.",
                ephemeral=True,
            )
        await self.bot.db.execute(
            "UPDATE denycord_voice_generators SET category_id=? WHERE guild_id=? AND channel_id=?",
            (category.id if category else None, guild.id, generator.id),
        )
        await self.bot.db.execute(
            "UPDATE denycord_voice_settings SET category_id=? "
            "WHERE guild_id=? AND hub_channel_id=?",
            (category.id if category else None, guild.id, generator.id),
        )
        await self.bot.db.commit()
        message = f"Rooms from {generator.mention} will be created in {category.name}." if category else (
            f"The room category for {generator.mention} has been cleared."
        )
        await interaction.response.send_message(message, ephemeral=True)

    @tempvoice.command(name="set-panel", description="Move or refresh the persistent TempVoice panel")
    async def tempvoice_set_panel(self, interaction: discord.Interaction, panel: discord.TextChannel):
        guild, _ = await self._tempvoice_admin_context(interaction)
        if guild is None:
            return
        bot_member = guild.me
        if bot_member is None:
            return await interaction.response.send_message(
                "I could not resolve my server member. Try again after I reconnect.",
                ephemeral=True,
            )
        permissions = panel.permissions_for(bot_member)
        if not (
            permissions.view_channel
            and permissions.send_messages
            and permissions.attach_files
        ):
            return await interaction.response.send_message(
                "I need View Channel, Send Messages, and Attach Files in that panel channel.",
                ephemeral=True,
            )
        async with self.bot.db.execute(
            "SELECT hub_channel_id, category_id, panel_channel_id, interface_message_id "
            "FROM denycord_voice_settings WHERE guild_id=?",
            (guild.id,),
        ) as cursor:
            settings = await cursor.fetchone()
        if not settings:
            return await interaction.response.send_message(
                "Run `/tempvoice setup` before setting the panel channel.",
                ephemeral=True,
            )
        try:
            interface_message_id = await self._ensure_voice_interface(
                guild,
                panel,
                settings[3] if settings[2] == panel.id else None,
            )
        except discord.HTTPException:
            log.exception("Could not post TempVoice panel in guild %s", guild.id)
            return await interaction.response.send_message(
                "I could not post the panel. Check channel permissions and try again.",
                ephemeral=True,
            )
        await self.bot.db.execute(
            "UPDATE denycord_voice_settings SET panel_channel_id=?, interface_message_id=? WHERE guild_id=?",
            (panel.id, interface_message_id, guild.id),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            f"TempVoice's persistent panel is set to {panel.mention}.",
            ephemeral=True,
        )

    @tempvoice.command(name="set-staff", description="Add or remove a TempVoice staff role")
    async def tempvoice_set_staff(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        enabled: bool = True,
    ):
        guild, member = await self._tempvoice_admin_context(interaction)
        if guild is None:
            return
        if enabled:
            await self.bot.db.execute(
                "INSERT OR IGNORE INTO denycord_voice_staff_roles "
                "(guild_id, role_id, created_by, created_at) VALUES (?,?,?,?)",
                (guild.id, role.id, member.id, _now().isoformat()),
            )
            message = f"{role.mention} can now override TempVoice owner restrictions."
        else:
            await self.bot.db.execute(
                "DELETE FROM denycord_voice_staff_roles WHERE guild_id=? AND role_id=?",
                (guild.id, role.id),
            )
            message = f"{role.mention} was removed from TempVoice staff."
        await self.bot.db.commit()
        await interaction.response.send_message(message, ephemeral=True)

    @tempvoice.command(name="config", description="Show this server's TempVoice configuration")
    async def tempvoice_config(self, interaction: discord.Interaction):
        guild, _ = await self._tempvoice_admin_context(interaction)
        if guild is None:
            return
        async with self.bot.db.execute(
            "SELECT hub_channel_id, panel_channel_id, category_id, enabled "
            "FROM denycord_voice_settings WHERE guild_id=?",
            (guild.id,),
        ) as cursor:
            settings = await cursor.fetchone()
        async with self.bot.db.execute(
            "SELECT channel_id, category_id FROM denycord_voice_generators "
            "WHERE guild_id=? ORDER BY channel_id",
            (guild.id,),
        ) as cursor:
            generators = await cursor.fetchall()
        if not settings:
            return await interaction.response.send_message(
                "TempVoice is not configured. Run `/tempvoice setup`.",
                ephemeral=True,
            )
        generator_text = "\n".join(
            f"{guild.get_channel(channel_id).mention if guild.get_channel(channel_id) else f'`{channel_id}`'}"
            f" → {guild.get_channel(category_id).name if category_id and guild.get_channel(category_id) else 'No category'}"
            for channel_id, category_id in generators
        ) or "None"
        embed = discord.Embed(title="TempVoice Configuration", color=TEMPVOICE_CONFIG["theme"]["embed_color"])
        embed.add_field(name="Enabled", value="Yes" if settings[3] else "No", inline=True)
        embed.add_field(
            name="Panel",
            value=guild.get_channel(settings[1]).mention if guild.get_channel(settings[1]) else "Missing",
            inline=True,
        )
        embed.add_field(name="Generators", value=generator_text[:1024], inline=False)
        embed.add_field(
            name="Claim policy",
            value=f"{TEMPVOICE_CONFIG['ownership_transfer_policy']} · {TEMPVOICE_CONFIG['claim_timeout_seconds']}s",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tempvoice.command(name="stats", description="Show this server's TempVoice channel counts")
    async def tempvoice_stats(self, interaction: discord.Interaction):
        guild, _ = await self._tempvoice_admin_context(interaction)
        if guild is None:
            return
        async with self.bot.db.execute(
            "SELECT COUNT(*) FROM denycord_voice_generators WHERE guild_id=?",
            (guild.id,),
        ) as cursor:
            generator_count = (await cursor.fetchone())[0]
        async with self.bot.db.execute(
            "SELECT COUNT(*) FROM denycord_voice_rooms WHERE guild_id=?",
            (guild.id,),
        ) as cursor:
            room_count = (await cursor.fetchone())[0]
        async with self.bot.db.execute(
            "SELECT enabled FROM denycord_voice_settings WHERE guild_id=?",
            (guild.id,),
        ) as cursor:
            settings = await cursor.fetchone()
        await interaction.response.send_message(
            f"TempVoice is {'enabled' if settings and settings[0] else 'disabled or unconfigured'}; "
            f"{generator_count} generator(s), {room_count} tracked room(s).",
            ephemeral=True,
        )

    @tempvoice.command(name="cleanup", description="Remove stale records and empty temporary rooms")
    async def tempvoice_cleanup(self, interaction: discord.Interaction):
        guild, _ = await self._tempvoice_admin_context(interaction)
        if guild is None:
            return
        async with self.bot.db.execute(
            "SELECT COUNT(*) FROM denycord_voice_rooms WHERE guild_id=?",
            (guild.id,),
        ) as cursor:
            before_count = (await cursor.fetchone())[0]
        await self._cleanup_empty_rooms(guild.id)
        async with self.bot.db.execute(
            "SELECT COUNT(*) FROM denycord_voice_rooms WHERE guild_id=?",
            (guild.id,),
        ) as cursor:
            after_count = (await cursor.fetchone())[0]
        await interaction.response.send_message(
            f"TempVoice cleanup complete; removed {before_count - after_count} empty or stale room record(s).",
            ephemeral=True,
        )

    async def _tempvoice_backup_payload(self, guild_id):
        async with self.bot.db.execute(
            "SELECT hub_channel_id, panel_channel_id, category_id, interface_message_id, enabled "
            "FROM denycord_voice_settings WHERE guild_id=?",
            (guild_id,),
        ) as cursor:
            settings_row = await cursor.fetchone()
        async with self.bot.db.execute(
            "SELECT channel_id, category_id, created_by, created_at FROM denycord_voice_generators "
            "WHERE guild_id=? ORDER BY channel_id",
            (guild_id,),
        ) as cursor:
            generators = await cursor.fetchall()
        async with self.bot.db.execute(
            "SELECT role_id, created_by, created_at FROM denycord_voice_staff_roles "
            "WHERE guild_id=? ORDER BY role_id",
            (guild_id,),
        ) as cursor:
            staff_roles = await cursor.fetchall()
        async with self.bot.db.execute(
            "SELECT channel_id, owner_id, created_at, locked, user_limit, rtc_region, chat_open, hidden, panel_message_id "
            "FROM denycord_voice_rooms WHERE guild_id=? ORDER BY channel_id",
            (guild_id,),
        ) as cursor:
            rooms = await cursor.fetchall()
        async with self.bot.db.execute(
            "SELECT channel_id, user_id, allow_connect, allow_chat FROM denycord_voice_overrides "
            "WHERE guild_id=? ORDER BY channel_id, user_id",
            (guild_id,),
        ) as cursor:
            overrides = await cursor.fetchall()
        return {
            "format": "falcon-tempvoice-v1",
            "guild_id": guild_id,
            "created_at": _now().isoformat(),
            "settings": list(settings_row) if settings_row else None,
            "generators": [list(row) for row in generators],
            "staff_roles": [list(row) for row in staff_roles],
            "rooms": [list(row) for row in rooms],
            "overrides": [list(row) for row in overrides],
        }

    @tempvoice.command(name="backup", description="Export this server's TempVoice database records")
    async def tempvoice_backup(self, interaction: discord.Interaction):
        guild, _ = await self._tempvoice_admin_context(interaction)
        if guild is None:
            return
        payload = await self._tempvoice_backup_payload(guild.id)
        contents = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        if len(contents) > MAX_BACKUP_BYTES:
            return await interaction.response.send_message(
                "The TempVoice backup exceeds the 8 MiB export limit.",
                ephemeral=True,
            )
        backup_file = discord.File(
            io.BytesIO(contents),
            filename=f"falcon-tempvoice-{guild.id}.json",
        )
        await interaction.response.send_message(
            content="TempVoice data backup (guild configuration and room records).",
            file=backup_file,
            ephemeral=True,
        )

    @tempvoice.command(name="restore", description="Restore TempVoice data from a backup file")
    async def tempvoice_restore(self, interaction: discord.Interaction, backup: discord.Attachment):
        guild, _ = await self._tempvoice_admin_context(interaction)
        if guild is None:
            return
        if backup.size > MAX_BACKUP_BYTES:
            return await interaction.response.send_message(
                "The backup file exceeds the 8 MiB restore limit.",
                ephemeral=True,
            )
        try:
            payload = json.loads((await backup.read()).decode("utf-8"))
            if (
                not isinstance(payload, dict)
                or payload.get("format") != "falcon-tempvoice-v1"
                or payload.get("guild_id") != guild.id
                or not isinstance(payload.get("generators"), list)
                or not isinstance(payload.get("staff_roles"), list)
                or not isinstance(payload.get("rooms"), list)
                or not isinstance(payload.get("overrides"), list)
            ):
                raise ValueError("Backup schema or guild did not match.")
            settings_row = payload.get("settings")
            if settings_row is not None and (
                not isinstance(settings_row, list)
                or len(settings_row) != 5
                or not all(
                    isinstance(value, int) and not isinstance(value, bool) and value > 0
                    for value in settings_row[:2]
                )
                or (
                    settings_row[2] is not None
                    and (
                        not isinstance(settings_row[2], int)
                        or isinstance(settings_row[2], bool)
                        or settings_row[2] <= 0
                    )
                )
                or (
                    settings_row[3] is not None
                    and (
                        not isinstance(settings_row[3], int)
                        or isinstance(settings_row[3], bool)
                        or settings_row[3] <= 0
                    )
                )
                or not isinstance(settings_row[4], int)
                or isinstance(settings_row[4], bool)
                or settings_row[4] not in {0, 1}
            ):
                raise ValueError("Backup settings are invalid.")
            generators = payload["generators"]
            staff_roles = payload["staff_roles"]
            rooms = payload["rooms"]
            overrides = payload["overrides"]
            if not all(isinstance(row, list) and len(row) == 4 for row in generators):
                raise ValueError("Backup generators are invalid.")
            if not all(isinstance(row, list) and len(row) == 3 for row in staff_roles):
                raise ValueError("Backup staff roles are invalid.")
            if not all(isinstance(row, list) and len(row) == 9 for row in rooms):
                raise ValueError("Backup rooms are invalid.")
            if not all(isinstance(row, list) and len(row) == 4 for row in overrides):
                raise ValueError("Backup overrides are invalid.")

            valid_generators = []
            for channel_id, category_id, created_by, created_at in generators:
                if (
                    not isinstance(channel_id, int)
                    or isinstance(channel_id, bool)
                    or channel_id <= 0
                    or (category_id is not None and (
                        not isinstance(category_id, int)
                        or isinstance(category_id, bool)
                        or category_id <= 0
                    ))
                    or (created_by is not None and (
                        not isinstance(created_by, int)
                        or isinstance(created_by, bool)
                        or created_by <= 0
                    ))
                    or not isinstance(created_at, str)
                ):
                    raise ValueError("Backup generator data is invalid.")
                channel = guild.get_channel(channel_id)
                category = guild.get_channel(category_id) if category_id is not None else None
                if not isinstance(channel, discord.VoiceChannel):
                    raise ValueError("A generator channel in the backup no longer exists.")
                if category_id is not None and not isinstance(category, discord.CategoryChannel):
                    raise ValueError("A generator category in the backup no longer exists.")
                valid_generators.append((guild.id, channel_id, category_id, created_by, created_at))
            valid_roles = []
            for role_id, created_by, created_at in staff_roles:
                if (
                    not isinstance(role_id, int)
                    or isinstance(role_id, bool)
                    or role_id <= 0
                    or not isinstance(created_by, int)
                    or isinstance(created_by, bool)
                    or created_by <= 0
                    or not isinstance(created_at, str)
                    or guild.get_role(role_id) is None
                ):
                    raise ValueError("A staff role in the backup no longer exists.")
                valid_roles.append((guild.id, role_id, created_by, created_at))
            valid_rooms = []
            room_ids = set()
            for row in rooms:
                channel_id, owner_id, created_at, locked, user_limit, rtc_region, chat_open, hidden, panel_message_id = row
                if (
                    not isinstance(channel_id, int)
                    or isinstance(channel_id, bool)
                    or channel_id <= 0
                    or not isinstance(owner_id, int)
                    or isinstance(owner_id, bool)
                    or owner_id <= 0
                    or not isinstance(created_at, str)
                    or not isinstance(locked, int)
                    or isinstance(locked, bool)
                    or locked not in {0, 1}
                    or not isinstance(user_limit, int)
                    or isinstance(user_limit, bool)
                    or user_limit not in range(100)
                    or (
                        rtc_region is not None
                        and (not isinstance(rtc_region, str) or rtc_region not in RTC_REGIONS)
                    )
                    or not isinstance(chat_open, int)
                    or isinstance(chat_open, bool)
                    or chat_open not in {0, 1}
                    or not isinstance(hidden, int)
                    or isinstance(hidden, bool)
                    or hidden not in {0, 1}
                    or (
                        panel_message_id is not None
                        and (
                            not isinstance(panel_message_id, int)
                            or isinstance(panel_message_id, bool)
                            or panel_message_id <= 0
                        )
                    )
                ):
                    raise ValueError("Backup room data is invalid.")
                channel = guild.get_channel(channel_id)
                if not isinstance(channel, discord.VoiceChannel):
                    continue
                room_ids.add(channel_id)
                valid_rooms.append(
                    (guild.id, channel_id, owner_id, created_at, locked, user_limit, rtc_region, chat_open, hidden, panel_message_id)
                )
            valid_overrides = []
            for channel_id, user_id, allow_connect, allow_chat in overrides:
                if (
                    not isinstance(channel_id, int)
                    or isinstance(channel_id, bool)
                    or not isinstance(user_id, int)
                    or isinstance(user_id, bool)
                    or user_id <= 0
                    or (
                        allow_connect is not None
                        and (
                            not isinstance(allow_connect, int)
                            or isinstance(allow_connect, bool)
                        )
                    )
                    or (
                        allow_chat is not None
                        and (
                            not isinstance(allow_chat, int)
                            or isinstance(allow_chat, bool)
                        )
                    )
                    or allow_connect not in {None, 0, 1}
                    or allow_chat not in {None, 0, 1}
                ):
                    raise ValueError("Backup member override data is invalid.")
                if channel_id in room_ids:
                    valid_overrides.append((guild.id, channel_id, user_id, allow_connect, allow_chat))
            if settings_row is not None:
                hub = guild.get_channel(settings_row[0])
                panel = guild.get_channel(settings_row[1])
                category = guild.get_channel(settings_row[2]) if settings_row[2] is not None else None
                if not isinstance(hub, discord.VoiceChannel) or not isinstance(panel, discord.TextChannel):
                    raise ValueError("The configured generator or panel in the backup no longer exists.")
                if settings_row[2] is not None and not isinstance(category, discord.CategoryChannel):
                    raise ValueError("The configured category in the backup no longer exists.")
                if not any(row[1] == hub.id for row in valid_generators):
                    raise ValueError("The primary generator is missing from the backup generator list.")
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            return await interaction.response.send_message(
                f"Backup not restored: {error}",
                ephemeral=True,
            )

        try:
            await self.bot.db.execute("BEGIN IMMEDIATE")
            for table in (
                "denycord_voice_settings",
                "denycord_voice_generators",
                "denycord_voice_staff_roles",
                "denycord_voice_rooms",
                "denycord_voice_overrides",
            ):
                await self.bot.db.execute(
                    f"DELETE FROM {table} WHERE guild_id=?",
                    (guild.id,),
                )
            if settings_row is not None:
                await self.bot.db.execute(
                    "INSERT INTO denycord_voice_settings "
                    "(guild_id, hub_channel_id, panel_channel_id, category_id, interface_message_id, enabled) "
                    "VALUES (?,?,?,?,?,?)",
                    (guild.id, *settings_row),
                )
            await self.bot.db.executemany(
                "INSERT INTO denycord_voice_generators "
                "(guild_id, channel_id, category_id, created_by, created_at) VALUES (?,?,?,?,?)",
                valid_generators,
            )
            await self.bot.db.executemany(
                "INSERT INTO denycord_voice_staff_roles "
                "(guild_id, role_id, created_by, created_at) VALUES (?,?,?,?)",
                valid_roles,
            )
            await self.bot.db.executemany(
                "INSERT INTO denycord_voice_rooms "
                "(guild_id, channel_id, owner_id, created_at, locked, user_limit, rtc_region, chat_open, hidden, panel_message_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                valid_rooms,
            )
            await self.bot.db.executemany(
                "INSERT INTO denycord_voice_overrides "
                "(guild_id, channel_id, user_id, allow_connect, allow_chat) VALUES (?,?,?,?,?)",
                valid_overrides,
            )
            await self.bot.db.commit()
        except aiosqlite.Error:
            await self.bot.db.rollback()
            log.exception("Could not restore TempVoice backup in guild %s", guild.id)
            return await interaction.response.send_message(
                "TempVoice restore failed; no partial database changes were kept.",
                ephemeral=True,
            )
        await interaction.response.send_message(
            f"TempVoice backup restored: {len(valid_generators)} generator(s), "
            f"{len(valid_rooms)} existing room record(s), and {len(valid_roles)} staff role(s).",
            ephemeral=True,
        )

    async def _set_tempvoice_enabled(self, interaction, enabled):
        guild, _ = await self._tempvoice_admin_context(interaction)
        if guild is None:
            return
        async with self.bot.db.execute(
            "SELECT 1 FROM denycord_voice_settings WHERE guild_id=?",
            (guild.id,),
        ) as cursor:
            configured = await cursor.fetchone()
        if not configured:
            return await interaction.response.send_message(
                "Run `/tempvoice setup` before enabling TempVoice.",
                ephemeral=True,
            )
        await self.bot.db.execute(
            "UPDATE denycord_voice_settings SET enabled=? WHERE guild_id=?",
            (int(enabled), guild.id),
        )
        await self.bot.db.commit()
        state = "enabled" if enabled else "disabled"
        await interaction.response.send_message(
            f"TempVoice creation is now {state}. Existing rooms are unchanged.",
            ephemeral=True,
        )

    @tempvoice.command(name="disable", description="Disable new temporary room creation")
    async def tempvoice_disable(self, interaction: discord.Interaction):
        await self._set_tempvoice_enabled(interaction, False)

    @tempvoice.command(name="enable", description="Enable temporary room creation")
    async def tempvoice_enable(self, interaction: discord.Interaction):
        await self._set_tempvoice_enabled(interaction, True)

    async def cog_load(self):
        if self.bot.db is None:
            raise RuntimeError("DenycordVoiceBackup requires the bot's open aiosqlite database.")
        await self.bot.db.execute(
            "CREATE TABLE IF NOT EXISTS denycord_voice_settings ("
            "guild_id INTEGER PRIMARY KEY, hub_channel_id INTEGER NOT NULL, "
            "panel_channel_id INTEGER NOT NULL, category_id INTEGER, interface_message_id INTEGER)"
        )
        async with self.bot.db.execute("PRAGMA table_info(denycord_voice_settings)") as cursor:
            settings_columns = {row[1] for row in await cursor.fetchall()}
        if "interface_message_id" not in settings_columns:
            await self.bot.db.execute(
                "ALTER TABLE denycord_voice_settings ADD COLUMN interface_message_id INTEGER"
            )
        if "enabled" not in settings_columns:
            await self.bot.db.execute(
                "ALTER TABLE denycord_voice_settings ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"
            )
        await self.bot.db.execute(
            "CREATE TABLE IF NOT EXISTS denycord_voice_generators ("
            "guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, category_id INTEGER, "
            "created_by INTEGER, created_at TEXT NOT NULL, "
            "PRIMARY KEY(guild_id, channel_id))"
        )
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO denycord_voice_generators "
            "(guild_id, channel_id, category_id, created_by, created_at) "
            "SELECT guild_id, hub_channel_id, category_id, NULL, datetime('now') "
            "FROM denycord_voice_settings"
        )
        await self.bot.db.execute(
            "CREATE INDEX IF NOT EXISTS denycord_voice_generators_guild_idx "
            "ON denycord_voice_generators(guild_id)"
        )
        await self.bot.db.execute(
            "CREATE TABLE IF NOT EXISTS denycord_voice_staff_roles ("
            "guild_id INTEGER NOT NULL, role_id INTEGER NOT NULL, created_by INTEGER NOT NULL, "
            "created_at TEXT NOT NULL, PRIMARY KEY(guild_id, role_id))"
        )
        await self.bot.db.execute(
            "CREATE TABLE IF NOT EXISTS denycord_voice_audit ("
            "audit_id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, "
            "channel_id INTEGER, actor_id INTEGER, action TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}', "
            "created_at TEXT NOT NULL)"
        )
        await self.bot.db.execute(
            "CREATE INDEX IF NOT EXISTS denycord_voice_audit_guild_time_idx "
            "ON denycord_voice_audit(guild_id, created_at DESC)"
        )
        await self.bot.db.execute(
            "CREATE TABLE IF NOT EXISTS denycord_voice_rooms ("
            "guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, owner_id INTEGER NOT NULL, "
            "created_at TEXT NOT NULL, locked INTEGER NOT NULL DEFAULT 1, "
            "user_limit INTEGER NOT NULL DEFAULT 0, rtc_region TEXT, chat_open INTEGER NOT NULL DEFAULT 1, "
            "hidden INTEGER NOT NULL DEFAULT 0, panel_message_id INTEGER, "
            "PRIMARY KEY(guild_id, channel_id))"
        )
        async with self.bot.db.execute("PRAGMA table_info(denycord_voice_rooms)") as cursor:
            room_columns = {row[1] for row in await cursor.fetchall()}
        for column, declaration in (
            ("user_limit", "INTEGER NOT NULL DEFAULT 0"),
            ("rtc_region", "TEXT"),
            ("chat_open", "INTEGER NOT NULL DEFAULT 1"),
            ("hidden", "INTEGER NOT NULL DEFAULT 0"),
            ("panel_message_id", "INTEGER"),
        ):
            if column not in room_columns:
                await self.bot.db.execute(
                    f"ALTER TABLE denycord_voice_rooms ADD COLUMN {column} {declaration}"
                )
        await self.bot.db.execute(
            "CREATE TABLE IF NOT EXISTS denycord_voice_overrides ("
            "guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, user_id INTEGER NOT NULL, "
            "allow_connect INTEGER, allow_chat INTEGER, "
            "PRIMARY KEY(guild_id, channel_id, user_id))"
        )
        await self.bot.db.execute(
            "CREATE TABLE IF NOT EXISTS denycord_backup_settings ("
            "guild_id INTEGER PRIMARY KEY, channel_id INTEGER, enabled INTEGER NOT NULL DEFAULT 0, "
            "next_run_at TEXT, last_backup_at TEXT, last_backup_id INTEGER)"
        )
        await self.bot.db.execute(
            "CREATE TABLE IF NOT EXISTS denycord_backups ("
            "backup_id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, "
            "created_at TEXT NOT NULL, trigger TEXT NOT NULL, channel_id INTEGER NOT NULL, "
            "message_id INTEGER, snapshot TEXT NOT NULL)"
        )
        await self.bot.db.commit()
        self.bot.add_view(self.room_panel)
        self.maintenance_worker.start()

    def cog_unload(self):
        self.maintenance_worker.cancel()

    async def cog_app_command_error(self, interaction, error):
        log.error(
            "TempVoice application command failed in guild %s: %r",
            interaction.guild_id,
            error,
        )
        message = "TempVoice could not complete that command. Check the bot's permissions and try again."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    @tasks.loop(seconds=60)
    async def maintenance_worker(self):
        try:
            if not self._room_panels_reconciled:
                self._room_panels_reconciled = await self._refresh_saved_room_panels()
            await self._cleanup_empty_rooms()
            await self._run_due_backups()
        except aiosqlite.Error:
            await self.bot.db.rollback()
            log.exception("TempVoice maintenance could not access the database")

    @maintenance_worker.before_loop
    async def before_maintenance_worker(self):
        await self.bot.wait_until_ready()

    async def _room_schema_columns(self):
        try:
            async with self.bot.db.execute("PRAGMA table_info(denycord_voice_rooms)") as cursor:
                return {row[1] for row in await cursor.fetchall()}
        except aiosqlite.Error:
            return set()

    async def _is_managed_room(self, guild_id, channel_id):
        columns = await self._room_schema_columns()
        selected = []
        for column in ("owner_id", "locked", "user_limit", "rtc_region", "chat_open", "hidden", "panel_message_id"):
            if column in columns:
                selected.append(column)
            else:
                selected.append(f"NULL AS {column}")
        query = (
            f"SELECT {', '.join(selected)} FROM denycord_voice_rooms WHERE guild_id=? AND channel_id=?"
        )
        async with self.bot.db.execute(query, (guild_id, channel_id)) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        if len(row) < 7:
            row = tuple(row) + (None,) * (7 - len(row))
        return row[:7]

    async def _room_overrides(self, guild_id, channel_id):
        async with self.bot.db.execute(
            "SELECT user_id, allow_connect, allow_chat FROM denycord_voice_overrides "
            "WHERE guild_id=? AND channel_id=?",
            (guild_id, channel_id),
        ) as cursor:
            return await cursor.fetchall()

    async def _save_room_override(self, guild_id, channel_id, user_id, *, allow_connect, allow_chat):
        await self.bot.db.execute(
            "INSERT INTO denycord_voice_overrides "
            "(guild_id, channel_id, user_id, allow_connect, allow_chat) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id, channel_id, user_id) DO UPDATE SET "
            "allow_connect=excluded.allow_connect, allow_chat=excluded.allow_chat",
            (guild_id, channel_id, user_id, allow_connect, allow_chat),
        )
        await self.bot.db.commit()

    async def _apply_room_overrides(self, channel, member=None):
        rows = await self._room_overrides(channel.guild.id, channel.id)
        for user_id, allow_connect, allow_chat in rows:
            if member is not None and member.id != user_id:
                continue
            target = member if member is not None else channel.guild.get_member(user_id)
            if target is None:
                continue
            changes = {}
            if allow_connect is not None:
                changes.update(view_channel=bool(allow_connect), connect=bool(allow_connect))
            if allow_chat is not None:
                changes["send_messages"] = bool(allow_chat)
            if changes:
                await channel.set_permissions(target, **changes)
            if allow_connect == 0 and target in channel.members:
                try:
                    await target.move_to(None, reason="Enforce a blocked managed-room override")
                except discord.Forbidden:
                    log.warning("Cannot disconnect blocked member %s from room %s", target.id, channel.id)
                except discord.HTTPException:
                    log.exception("Could not disconnect blocked member %s from room %s", target.id, channel.id)

    async def _get_member_room(self, guild, member):
        voice = getattr(member, "voice", None)
        channel = voice.channel if voice else None
        if channel is None:
            return None, None
        row = await self._is_managed_room(guild.id, channel.id)
        return (channel, row) if row else (None, None)

    async def _is_room_staff(self, guild, member):
        permissions = getattr(member, "guild_permissions", discord.Permissions.none())
        if member.id == guild.owner_id or permissions.administrator:
            return True
        if permissions.manage_channels:
            return True
        configured_roles = {int(role_id) for role_id in TEMPVOICE_CONFIG["staff_role_ids"]}
        try:
            async with self.bot.db.execute(
                "SELECT role_id FROM denycord_voice_staff_roles WHERE guild_id=?",
                (guild.id,),
            ) as cursor:
                configured_roles.update(row[0] for row in await cursor.fetchall())
        except aiosqlite.Error:
            log.warning("Voice staff roles are unavailable for guild %s", guild.id)
        return bool(configured_roles.intersection(role.id for role in getattr(member, "roles", ())))

    async def _audit_room_action(self, guild_id, channel_id, actor_id, action, result):
        if not TEMPVOICE_CONFIG["audit_logging"]:
            return
        try:
            await self.bot.db.execute(
                "INSERT INTO denycord_voice_audit "
                "(guild_id, channel_id, actor_id, action, details, created_at) VALUES (?,?,?,?,?,?)",
                (
                    guild_id,
                    channel_id,
                    actor_id,
                    action,
                    json.dumps({"result": str(result)[:300]}, ensure_ascii=True),
                    _now().isoformat(),
                ),
            )
            await self.bot.db.commit()
        except aiosqlite.Error:
            log.exception("Could not audit voice-room action %s in guild %s", action, guild_id)

    async def _require_owner_room(self, guild, member):
        channel, row = await self._get_member_room(guild, member)
        if channel is None:
            return None, None, "Join your managed voice room first."
        if row[0] != member.id:
            return None, None, "Only this room's owner can manage it. Use Claim if its owner has left."
        bot_member = guild.me
        if bot_member is None or not channel.permissions_for(bot_member).manage_channels:
            return None, None, "I need Manage Channels permission in your room."
        return channel, row, None

    async def _room_overwrites(self, guild, owner):
        bot_member = guild.me
        if bot_member is None:
            raise RuntimeError("The bot's guild member is unavailable.")
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=True, connect=True, send_messages=True
            ),
            owner: discord.PermissionOverwrite(
                view_channel=True, connect=True, send_messages=True, embed_links=True
            ),
            bot_member: discord.PermissionOverwrite(
                view_channel=True,
                connect=True,
                send_messages=True,
                embed_links=True,
                attach_files=True,
                manage_channels=True,
                move_members=True,
            ),
        }
        return overwrites

    async def _voice_state_changed(self, member, before, after):
        if member.guild is None:
            return
        if member.bot:
            if before.channel is not None and before.channel.id != getattr(after.channel, "id", None):
                await self._delete_if_empty(member.guild, before.channel)
            return
        if after.channel is not None:
            async with self.bot.db.execute(
                "SELECT enabled "
                "FROM denycord_voice_settings WHERE guild_id=?", (member.guild.id,),
            ) as cursor:
                settings = await cursor.fetchone()
            if settings and settings[0]:
                async with self.bot.db.execute(
                    "SELECT category_id FROM denycord_voice_generators "
                    "WHERE guild_id=? AND channel_id=?",
                    (member.guild.id, after.channel.id),
                ) as cursor:
                    generator = await cursor.fetchone()
            else:
                generator = None
            if generator:
                key = (member.guild.id, member.id)
                lock = self._creation_locks.setdefault(member.guild.id, asyncio.Lock())
                async with lock:
                    voice = getattr(member, "voice", None)
                    if voice is None or voice.channel is None or voice.channel.id != after.channel.id:
                        return
                    now = asyncio.get_running_loop().time()
                    last_created = self._creation_cooldowns.get(key)
                    if (
                        last_created is not None
                        and now - last_created < TEMPVOICE_CREATION_COOLDOWN
                    ):
                        return
                    self._creation_cooldowns[key] = now
                    await self._create_temp_room(member, generator[0], after.channel.id)
                if ROOM_JOIN_LEAVE_MESSAGES:
                    await self._send_voice_notice(
                        after.channel, f"🔊 **{member.display_name}** joined the voice channel."
                    )
            if await self._is_managed_room(member.guild.id, after.channel.id):
                await self._apply_room_overrides(after.channel, member)
                if ROOM_JOIN_LEAVE_MESSAGES:
                    await self._send_voice_notice(
                        after.channel, f"🔊 **{member.display_name}** joined the voice channel."
                    )
        if before.channel is not None and before.channel.id != getattr(after.channel, "id", None):
            if TEMPVOICE_CONFIG["auto_transfer_owner"]:
                await self._transfer_owner_after_leave(member, before.channel)
            if ROOM_JOIN_LEAVE_MESSAGES:
                await self._send_voice_notice(
                    before.channel, f"👋 **{member.display_name}** left the voice channel."
                )
            await self._delete_if_empty(member.guild, before.channel)

    async def _transfer_owner_after_leave(self, former_owner, channel):
        row = await self._is_managed_room(former_owner.guild.id, channel.id)
        if row is None or row[0] != former_owner.id:
            return
        candidates = [candidate for candidate in channel.members if not candidate.bot]
        if not candidates:
            return

        def seniority(candidate):
            joined_at = getattr(candidate, "joined_at", None)
            if not isinstance(joined_at, datetime.datetime):
                joined_at = datetime.datetime.max.replace(tzinfo=UTC)
            elif joined_at.tzinfo is None:
                joined_at = joined_at.replace(tzinfo=UTC)
            else:
                joined_at = joined_at.astimezone(UTC)
            return joined_at, candidate.id

        successor = min(
            candidates,
            key=seniority,
        )
        try:
            await channel.set_permissions(
                successor,
                view_channel=True,
                connect=True,
                reason=f"Automatic room ownership transfer after {former_owner} left",
            )
            await self.bot.db.execute(
                "UPDATE denycord_voice_rooms SET owner_id=? WHERE guild_id=? AND channel_id=?",
                (successor.id, former_owner.guild.id, channel.id),
            )
            await self.bot.db.commit()
        except discord.Forbidden:
            log.warning(
                "Discord denied automatic ownership transfer for room %s",
                channel.id,
                exc_info=True,
            )
            return
        except discord.HTTPException:
            log.exception("Discord API error transferring ownership of room %s", channel.id)
            return
        except aiosqlite.Error:
            await self.bot.db.rollback()
            log.exception("Could not save automatic ownership transfer for room %s", channel.id)
            return
        await self._audit_room_action(
            former_owner.guild.id,
            channel.id,
            former_owner.id,
            "auto_transfer",
            f"ownership transferred to {successor.id}",
        )

    async def _send_voice_notice(self, channel, content):
        try:
            await channel.send(content)
        except discord.Forbidden:
            log.debug("Cannot send voice-room notice in channel %s", channel.id)
        except discord.HTTPException:
            log.debug("Discord API rejected voice-room notice in channel %s", channel.id, exc_info=True)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        try:
            await self._voice_state_changed(member, before, after)
        except aiosqlite.Error:
            await self.bot.db.rollback()
            log.exception("TempVoice voice-state handling failed in guild %s", member.guild.id)

    async def _create_temp_room(self, member, category_id, generator_channel_id=None):
        guild = member.guild
        bot_member = guild.me
        hub_permissions = None
        if (
            bot_member is None
            or not bot_member.guild_permissions.manage_channels
            or not bot_member.guild_permissions.move_members
        ):
            log.warning("Missing guild-level voice-room permissions in guild %s", guild.id)
            return
        hub = guild.get_channel(generator_channel_id) if generator_channel_id else None
        if hub is None:
            async with self.bot.db.execute(
                "SELECT hub_channel_id FROM denycord_voice_settings WHERE guild_id=?", (guild.id,)
            ) as cursor:
                row = await cursor.fetchone()
            hub = guild.get_channel(row[0]) if row else None
        if hub is not None:
            hub_permissions = hub.permissions_for(bot_member)
        if hub_permissions is not None and not (
            hub_permissions.view_channel
            and hub_permissions.connect
            and hub_permissions.move_members
        ):
            log.warning("Missing voice-room permissions in guild %s", guild.id)
            return
        async with self.bot.db.execute(
            "SELECT COUNT(*) FROM denycord_voice_rooms WHERE guild_id=?",
            (guild.id,),
        ) as cursor:
            room_count = (await cursor.fetchone())[0]
        if room_count >= MAX_TEMP_ROOMS_PER_GUILD:
            log.info("Voice room cap reached in guild %s; not creating another room", guild.id)
            return
        category = guild.get_channel(category_id) if category_id else None
        if category_id and not isinstance(category, discord.CategoryChannel):
            category = None
        name_template = str(TEMPVOICE_CONFIG["default_channel_name"])
        try:
            base_name = name_template.format(
                display_name=member.display_name,
                username=getattr(member, "name", member.display_name),
            )
        except (KeyError, ValueError):
            base_name = f"{member.display_name}'s room"
        name = _format_room_name(base_name)
        user_limit = TEMPVOICE_CONFIG["default_user_limit"]
        region_name = str(TEMPVOICE_CONFIG["default_region"]).casefold()
        rtc_region = None if region_name == "auto" else region_name
        bitrate_kbps = TEMPVOICE_CONFIG["default_bitrate_kbps"]
        max_bitrate_kbps = {0: 96, 1: 128, 2: 256, 3: 384}.get(
            getattr(guild, "premium_tier", 0), 96
        )
        bitrate = min(bitrate_kbps, max_bitrate_kbps) * 1000
        channel = None
        try:
            overwrites = await self._room_overwrites(guild, member)
            channel = await guild.create_voice_channel(
                name=name, category=category, overwrites=overwrites,
                user_limit=user_limit,
                bitrate=bitrate,
                rtc_region=rtc_region,
                reason=f"VoiceMaster room for {member} ({member.id})",
            )
            self._rooms_being_created.add((guild.id, channel.id))
            await self.bot.db.execute(
                "INSERT OR REPLACE INTO denycord_voice_rooms "
                "(guild_id, channel_id, owner_id, created_at, locked, user_limit, rtc_region, chat_open, hidden, panel_message_id) "
                "VALUES (?, ?, ?, ?, 0, ?, ?, 1, 0, NULL)",
                (guild.id, channel.id, member.id, _now().isoformat(), user_limit, rtc_region),
            )
            await self.bot.db.commit()
            await member.move_to(channel, reason="Joined the VoiceMaster create channel")
        except ValueError as error:
            log.warning("Could not create temporary room in guild %s: %s", guild.id, error)
            if channel is not None:
                await self._remove_room_after_failed_create(channel)
            return
        except discord.Forbidden:
            log.warning("Discord denied VoiceMaster room setup in guild %s", guild.id, exc_info=True)
            if channel is not None:
                await self._remove_room_after_failed_create(channel)
            return
        except discord.HTTPException:
            log.exception("Discord API error creating a VoiceMaster room in guild %s", guild.id)
            if channel is not None:
                await self._remove_room_after_failed_create(channel)
            return
        except aiosqlite.Error:
            await self.bot.db.rollback()
            log.exception("Could not save VoiceMaster room metadata in guild %s", guild.id)
            if channel is not None:
                await self._remove_room_after_failed_create(channel)
            return

        try:
            panel_view = await self._room_panel_view(channel, member)
            panel_message = await channel.send(
                view=panel_view,
                file=self._panel_image_file(panel_view),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await self.bot.db.execute(
                "UPDATE denycord_voice_rooms SET panel_message_id=? WHERE guild_id=? AND channel_id=?",
                (panel_message.id, guild.id, channel.id),
            )
            await self.bot.db.commit()
        except discord.NotFound:
            log.info(
                "VoiceMaster room %s disappeared before its interface could be posted",
                channel.id,
            )
            await self._remove_room_after_failed_create(channel, delete_channel=False)
        except discord.Forbidden:
            log.warning("Cannot post VoiceMaster interface in room %s", channel.id, exc_info=True)
        except discord.HTTPException:
            log.exception("Discord API error posting VoiceMaster interface in room %s", channel.id)
        except RuntimeError:
            log.exception("Could not render VoiceMaster interface in room %s", channel.id)
        finally:
            self._rooms_being_created.discard((guild.id, channel.id))

    async def _load_room_panel_emoji_images(self):
        async with self._voice_emoji_cache_lock:
            missing = [
                key for key in VOICE_BUTTON_EMOJIS
                if key not in self._voice_emoji_cache
            ]
            if missing:
                timeout = aiohttp.ClientTimeout(total=30)
                fetched = []
                try:
                    async with aiohttp.ClientSession(
                        timeout=timeout,
                        connector=aiohttp.TCPConnector(limit=8),
                    ) as session:
                        async def fetch_emoji(key):
                            emoji = VOICE_BUTTON_EMOJIS[key]
                            for attempt in range(2):
                                try:
                                    async with session.get(str(emoji.url)) as response:
                                        response.raise_for_status()
                                        if (
                                            response.content_length
                                            and response.content_length > 2 * 1024 * 1024
                                        ):
                                            raise ValueError(
                                                f"TempVoice emoji {key!r} image exceeds 2 MiB."
                                            )
                                        image_bytes = await response.read()
                                        if len(image_bytes) > 2 * 1024 * 1024:
                                            raise ValueError(
                                                f"TempVoice emoji {key!r} image exceeds 2 MiB."
                                            )
                                    with Image.open(io.BytesIO(image_bytes)) as image:
                                        image.load()
                                    return key, image_bytes
                                except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
                                    if attempt == 0:
                                        log.warning(
                                            "Retrying failed TempVoice emoji download for %s",
                                            key,
                                            exc_info=True,
                                        )
                                        continue
                                    log.warning(
                                        "Using a fallback icon for TempVoice emoji %s because the downloaded image is invalid or truncated.",
                                        key,
                                        exc_info=True,
                                    )
                                    return key, _placeholder_emoji_bytes(key[:1].upper() or "?")
                            return key, _placeholder_emoji_bytes(key[:1].upper() or "?")

                        fetched = [
                            item for item in await asyncio.gather(
                                *(fetch_emoji(key) for key in missing)
                            )
                            if item is not None
                        ]
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as error:
                    log.warning(
                        "Could not download TempVoice panel emoji images; falling back to generated icons.",
                        exc_info=error,
                    )
                    fetched = [
                        (key, _placeholder_emoji_bytes(key[:1].upper() or "?"))
                        for key in missing
                    ]
                self._voice_emoji_cache.update(fetched)
            return dict(self._voice_emoji_cache)

    def _panel_card_description(self, heading, subtitle):
        description = (
            f"{heading}. {subtitle}. "
            f"Illustrated card with {len(ROOM_PANEL_BUTTONS)} voice controls."
        )
        if len(description) > 256:
            return f"{description[:253].rstrip()}..."
        return description

    async def _room_panel_view(self, channel=None, owner=None):
        if channel is not None and owner is not None:
            owner_name = (
                getattr(owner, "display_name", None)
                or getattr(owner, "name", None)
                or f"Member {owner.id}"
            )
            room_name = getattr(channel, "name", None) or f"Room {channel.id}"
            subtitle = f"Room: {room_name} · Owner: {owner_name}"
        else:
            subtitle = "Join the Join to Create voice channel to open your temporary room"
            room_name = "Voice Room"
            owner_name = "Owner"
        heading = "FALCON VOICE ROOM"
        emoji_images = await self._load_room_panel_emoji_images()
        card_image = _render_room_panel_card(
            emoji_images,
            heading,
            subtitle,
        )
        return VoiceRoomPanel(
            self,
            card_image,
            self._panel_card_description(heading, subtitle),
        )

    async def _voice_interface_view(self, guild):
        heading = "FALCON VOICE ROOM"
        subtitle = (
            f"{guild.name} · Join the Join to Create voice channel to open a room"
        )
        emoji_images = await self._load_room_panel_emoji_images()
        card_image = _render_room_panel_card(emoji_images, heading, subtitle)
        return VoiceRoomPanel(
            self,
            card_image,
            self._panel_card_description(heading, subtitle),
        )

    def _panel_image_file(self, panel_view):
        return discord.File(
            io.BytesIO(panel_view.card_image),
            filename=panel_view.card_filename,
        )

    async def _ensure_voice_interface(self, guild, panel_channel, message_id=None):
        message = None
        if message_id:
            try:
                message = await panel_channel.fetch_message(message_id)
            except discord.NotFound:
                pass
        panel_view = await self._voice_interface_view(guild)
        if message is not None:
            await edit_message_v2(
                message,
                view=panel_view,
                attachments=[self._panel_image_file(panel_view)],
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            message = await panel_channel.send(
                view=panel_view,
                file=self._panel_image_file(panel_view),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        return message.id

    async def _refresh_saved_room_panels(self):
        async with self.bot.db.execute(
            "SELECT guild_id, channel_id, owner_id, panel_message_id "
            "FROM denycord_voice_rooms WHERE panel_message_id IS NOT NULL"
        ) as cursor:
            rows = await cursor.fetchall()
        all_guilds_ready = True
        for guild_id, channel_id, owner_id, message_id in rows:
            if not bot_owns_guild(self.bot, guild_id):
                continue
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                all_guilds_ready = False
                continue
            channel = guild.get_channel(channel_id)
            if not isinstance(channel, discord.VoiceChannel):
                continue
            try:
                message = await channel.fetch_message(message_id)
                panel_view = await self._room_panel_view(
                    channel, discord.Object(id=owner_id)
                )
                await edit_message_v2(
                    message,
                    view=panel_view,
                    attachments=[self._panel_image_file(panel_view)],
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.NotFound:
                log.info("Saved TempVoice panel %s no longer exists in room %s", message_id, channel_id)
            except discord.Forbidden:
                log.warning("Cannot refresh saved TempVoice panel in room %s", channel_id, exc_info=True)
            except discord.HTTPException:
                log.exception("Discord API error refreshing TempVoice panel in room %s", channel_id)
            except RuntimeError:
                log.exception("Could not render saved TempVoice panel in room %s", channel_id)
                all_guilds_ready = False
        return all_guilds_ready

    async def _remove_room_after_failed_create(self, channel, *, delete_channel=True):
        try:
            if delete_channel:
                try:
                    await channel.delete(reason="Cleaning up failed VoiceMaster room setup")
                except discord.NotFound:
                    pass
                except discord.Forbidden:
                    log.warning("Could not clean up failed managed room %s", channel.id, exc_info=True)
                except discord.HTTPException:
                    log.exception("Discord API error cleaning up room %s", channel.id)
            await self.bot.db.execute(
                "DELETE FROM denycord_voice_rooms WHERE guild_id=? AND channel_id=?",
                (channel.guild.id, channel.id),
            )
            await self.bot.db.execute(
                "DELETE FROM denycord_voice_overrides WHERE guild_id=? AND channel_id=?",
                (channel.guild.id, channel.id),
            )
            await self.bot.db.commit()
        finally:
            self._rooms_being_created.discard((channel.guild.id, channel.id))

    async def _delete_if_empty(self, guild, channel):
        if not TEMPVOICE_CONFIG["auto_delete_empty"]:
            return
        if (guild.id, channel.id) in self._rooms_being_created:
            return
        row = await self._is_managed_room(guild.id, channel.id)
        if row is None or channel.members:
            return
        try:
            await channel.delete(reason="Auto-delete empty VoiceMaster room")
        except discord.NotFound:
            pass
        except discord.Forbidden:
            log.warning("Cannot delete empty managed room %s in guild %s", channel.id, guild.id, exc_info=True)
            return
        except discord.HTTPException:
            log.exception("Discord API error deleting empty managed room %s", channel.id)
            return
        await self.bot.db.execute(
            "DELETE FROM denycord_voice_rooms WHERE guild_id=? AND channel_id=?",
            (guild.id, channel.id),
        )
        await self.bot.db.execute(
            "DELETE FROM denycord_voice_overrides WHERE guild_id=? AND channel_id=?",
            (guild.id, channel.id),
        )
        await self.bot.db.commit()

    async def _cleanup_empty_rooms(self, guild_id=None):
        query = "SELECT guild_id, channel_id FROM denycord_voice_rooms"
        parameters = ()
        if guild_id is not None:
            query += " WHERE guild_id=?"
            parameters = (guild_id,)
        async with self.bot.db.execute(query, parameters) as cursor:
            rows = await cursor.fetchall()
        for guild_id, channel_id in rows:
            if not bot_owns_guild(self.bot, guild_id):
                continue
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            channel = guild.get_channel(channel_id)
            if channel is None:
                await self.bot.db.execute(
                    "DELETE FROM denycord_voice_rooms WHERE guild_id=? AND channel_id=?",
                    (guild_id, channel_id),
                )
                await self.bot.db.execute(
                    "DELETE FROM denycord_voice_overrides WHERE guild_id=? AND channel_id=?",
                    (guild_id, channel_id),
                )
                await self.bot.db.commit()
            elif isinstance(channel, discord.VoiceChannel):
                await self._delete_if_empty(guild, channel)

    async def _authorize_room_action(self, guild, member, action):
        channel, row, error = await self._current_room_member_for(guild, member)
        if error:
            return None, None, error
        owner_id = row[0]
        is_staff = await self._is_room_staff(guild, member)
        if action == "claim":
            if owner_id == member.id:
                return None, None, "You already own this room."
            owner_present = any(voice_member.id == owner_id for voice_member in channel.members)
            policy = TEMPVOICE_CONFIG["ownership_transfer_policy"]
            timeout = TEMPVOICE_CONFIG["claim_timeout_seconds"]
            try:
                created_at = _as_utc(await self._fetch_room_created_at(guild.id, channel.id))
            except (TypeError, ValueError):
                created_at = _now()
            claim_ready = timeout > 0 and _now() >= created_at + datetime.timedelta(seconds=timeout)
            if not is_staff and policy == "claim_after_owner_leaves" and owner_present:
                return None, None, "The current owner is still in the room; it cannot be claimed."
            if not is_staff and policy == "claim_after_timeout" and not claim_ready:
                return None, None, f"This room can be claimed <t:{int((created_at + datetime.timedelta(seconds=timeout)).timestamp())}:R>."
            old_owner = guild.get_member(owner_id)
            if old_owner is None:
                try:
                    old_owner = await guild.fetch_member(owner_id)
                except discord.NotFound:
                    old_owner = None
                except discord.Forbidden:
                    log.warning("Cannot resolve former room owner %s in guild %s", owner_id, guild.id, exc_info=True)
                    return None, None, "I could not verify the previous owner. Ask an administrator for help."
                except discord.HTTPException:
                    log.exception("Discord API error resolving former room owner %s", owner_id)
                    return None, None, "Discord could not verify the previous owner. Try again."
            try:
                await channel.set_permissions(
                    member, view_channel=True, connect=True, reason=f"Room claimed by {member}"
                )
                if old_owner is not None:
                    await channel.set_permissions(
                        old_owner, view_channel=False, connect=False,
                        reason=f"Room claimed by {member}",
                    )
            except discord.Forbidden:
                log.warning("Discord denied room claim permission update for %s", channel.id, exc_info=True)
                return None, None, "Discord would not update the room permissions. Ask an administrator."
            except discord.HTTPException:
                log.exception("Discord API error applying room claim for %s", channel.id)
                return None, None, "Discord could not update the room permissions. Try again."
            await self.bot.db.execute(
                "UPDATE denycord_voice_rooms SET owner_id=? WHERE guild_id=? AND channel_id=?",
                (member.id, guild.id, channel.id),
            )
            await self.bot.db.commit()
            return channel, (member.id, row[1]), None
        if owner_id != member.id and not is_staff:
            return None, None, "Only this room's owner can manage it. Use Claim if its owner has left."
        return channel, row, None

    async def _fetch_room_created_at(self, guild_id, channel_id):
        async with self.bot.db.execute(
            "SELECT created_at FROM denycord_voice_rooms WHERE guild_id=? AND channel_id=?",
            (guild_id, channel_id),
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else None

    async def _current_room_member_for(self, guild, member):
        voice = getattr(member, "voice", None)
        channel = voice.channel if voice else None
        if channel is None:
            return None, None, "Join your managed voice room first."
        row = await self._is_managed_room(guild.id, channel.id)
        if row is None:
            return None, None, "You are not in a Falcon-managed voice room."
        bot_member = guild.me
        if bot_member is None:
            return None, None, "The bot's server member is unavailable."
        if not channel.permissions_for(bot_member).manage_channels:
            return None, None, "I need Manage Channels permission in your room."
        return channel, row, None

    async def _perform_room_action(self, guild, member, action, value=None):
        if action not in TEMPVOICE_CONFIG["allowed_controls"]:
            return f"The `{action}` control is disabled in TempVoice configuration."
        channel, room_row, error = await self._authorize_room_action(guild, member, action)
        if error:
            return error
        try:
            if action in {"lock", "unlock"}:
                await self._set_room_locked(
                    channel, guild, action == "lock", member, hidden=bool(room_row[5])
                )
                await self.bot.db.execute(
                    "UPDATE denycord_voice_rooms SET locked=? WHERE guild_id=? AND channel_id=?",
                    (int(action == "lock"), guild.id, channel.id),
                )
                await self.bot.db.commit()
                return f"Room {action}ed." if action == "lock" else "Room unlocked."
            if action in {"hide", "unhide"}:
                hidden = action == "hide"
                locked = bool(room_row[1])
                can_connect = not hidden and not locked
                await channel.set_permissions(
                    guild.default_role,
                    view_channel=not hidden,
                    connect=can_connect,
                    reason=f"Room {action}d by {member}",
                )
                for target, overwrite in list(channel.overwrites.items()):
                    if target in {guild.default_role, member, guild.me} or isinstance(target, discord.Member):
                        continue
                    overwrite.view_channel = not hidden
                    overwrite.connect = can_connect
                    await channel.set_permissions(
                        target, overwrite=overwrite, reason=f"Room {action}d by {member}"
                    )
                await channel.set_permissions(
                    member, view_channel=True, connect=True,
                    reason=f"Room {action}d by {member}",
                )
                if not hidden:
                    await self._apply_room_overrides(channel)
                await self.bot.db.execute(
                    "UPDATE denycord_voice_rooms SET hidden=? WHERE guild_id=? AND channel_id=?",
                    (int(hidden), guild.id, channel.id),
                )
                await self.bot.db.commit()
                return "Room hidden from other members." if hidden else "Room is visible again."
            if action == "rename":
                name = _format_room_name(value)
                await channel.edit(name=name, reason=f"Room renamed by {member}")
                return f"Room renamed to **{discord.utils.escape_markdown(name)}**."
            if action == "limit":
                try:
                    limit = int(value)
                except (TypeError, ValueError):
                    return "Set a whole-number limit from 0 to 99 (0 means unlimited)."
                if not 0 <= limit <= 99:
                    return "The room limit must be between 0 and 99."
                await channel.edit(user_limit=limit, reason=f"Room limit changed by {member}")
                await self.bot.db.execute(
                    "UPDATE denycord_voice_rooms SET user_limit=? WHERE guild_id=? AND channel_id=?",
                    (limit, guild.id, channel.id),
                )
                await self.bot.db.commit()
                return "Room user limit set to unlimited." if limit == 0 else f"Room user limit set to {limit}."
            if action == "permit":
                target = await self._resolve_room_member(guild, value)
                if target is None:
                    return "Provide a member mention or ID from this server."
                if target.id == member.id:
                    return "You already have access to your room."
                await channel.set_permissions(
                    target, view_channel=True, connect=True, send_messages=True,
                    reason=f"Permitted by room owner {member}"
                )
                await self._save_room_override(guild.id, channel.id, target.id, allow_connect=1, allow_chat=1)
                return f"Permitted {target.mention} to join your room."
            if action in {"allow", "permit", "trust", "block", "ban", "reject"}:
                target = await self._resolve_room_member(guild, value)
                if target is None:
                    return "Provide a member mention or ID from this server."
                if target.id == member.id or target.id == guild.owner_id:
                    return "You cannot change access for yourself or the server owner."
                allowed = action in {"allow", "permit", "trust"}
                denied_action = not allowed
                await channel.set_permissions(
                    target,
                    view_channel=allowed,
                    connect=allowed,
                    send_messages=allowed,
                    reason=f"Room {action} by {member}",
                )
                await self._save_room_override(
                    guild.id, channel.id, target.id,
                    allow_connect=int(allowed), allow_chat=int(allowed),
                )
                if denied_action and target in channel.members:
                    await target.move_to(None, reason=f"Blocked from managed room by {member}")
                verb = "trusted" if action == "trust" else "permitted" if action == "permit" else (
                    "allowed" if allowed else "rejected" if action == "reject" else
                    "banned" if action == "ban" else "blocked"
                )
                return f"{target.mention} is now {verb} for this room."
            if action in {"unblock", "untrust"}:
                target = await self._resolve_room_member(guild, value)
                if target is None:
                    return "Provide a member mention or ID from this server."
                await channel.set_permissions(
                    target, overwrite=None, reason=f"Room override cleared by {member}"
                )
                await self.bot.db.execute(
                    "DELETE FROM denycord_voice_overrides WHERE guild_id=? AND channel_id=? AND user_id=?",
                    (guild.id, channel.id, target.id),
                )
                await self.bot.db.commit()
                return f"{target.mention} no longer has a saved room override."
            if action == "clear":
                target = await self._resolve_room_member(guild, value) if value else None
                if value and target is None:
                    return "Provide a member mention or ID from this server."
                rows = await self._room_overrides(guild.id, channel.id)
                targets = [target] if target else [
                    guild.get_member(user_id) or discord.Object(id=user_id)
                    for user_id, _, _ in rows
                ]
                for override_target in targets:
                    if override_target is None:
                        continue
                    overwrite = channel.overwrites_for(override_target)
                    overwrite.view_channel = None
                    overwrite.connect = None
                    overwrite.send_messages = None
                    if overwrite.is_empty():
                        await channel.set_permissions(override_target, overwrite=None)
                    else:
                        await channel.set_permissions(override_target, overwrite=overwrite)
                    await self.bot.db.execute(
                        "DELETE FROM denycord_voice_overrides WHERE guild_id=? AND channel_id=? AND user_id=?",
                        (guild.id, channel.id, override_target.id),
                    )
                if not target:
                    await self.bot.db.execute(
                        "DELETE FROM denycord_voice_overrides WHERE guild_id=? AND channel_id=?",
                        (guild.id, channel.id),
                    )
                await self.bot.db.commit()
                return "Room access overrides cleared."
            if action in {"openchat", "closechat"}:
                chat_open = action == "openchat"
                await channel.set_permissions(guild.default_role, send_messages=chat_open)
                await channel.set_permissions(member, send_messages=True if not chat_open else None)
                await self.bot.db.execute(
                    "UPDATE denycord_voice_rooms SET chat_open=? WHERE guild_id=? AND channel_id=?",
                    (int(chat_open), guild.id, channel.id),
                )
                await self.bot.db.commit()
                await self._apply_room_overrides(channel)
                return "Room chat opened." if chat_open else "Room chat closed for other members."
            if action == "region":
                requested = str(value or "").strip().casefold()
                if not requested:
                    return f"Available regions: {', '.join(RTC_REGIONS)}. Use `room region <name>`."
                if requested not in RTC_REGIONS:
                    return f"Choose a supported region: {', '.join(RTC_REGIONS)}."
                rtc_region = None if requested == "auto" else requested
                await channel.edit(rtc_region=rtc_region, reason=f"Voice region changed by {member}")
                await self.bot.db.execute(
                    "UPDATE denycord_voice_rooms SET rtc_region=? WHERE guild_id=? AND channel_id=?",
                    (rtc_region, guild.id, channel.id),
                )
                await self.bot.db.commit()
                return f"Voice region set to **{requested}**."
            if action == "bitrate":
                try:
                    bitrate_kbps = int(value)
                except (TypeError, ValueError):
                    return "Enter a bitrate in kbps, such as 64 or 96."
                maximum_kbps = {0: 96, 1: 128, 2: 256, 3: 384}.get(
                    getattr(guild, "premium_tier", 0), 96
                )
                if not 8 <= bitrate_kbps <= maximum_kbps:
                    return f"Bitrate must be between 8 and {maximum_kbps} kbps for this server."
                await channel.edit(
                    bitrate=bitrate_kbps * 1000,
                    reason=f"Room bitrate changed by {member}",
                )
                return f"Room bitrate set to **{bitrate_kbps} kbps**."
            if action == "status":
                overrides = await self._room_overrides(guild.id, channel.id)
                allowed = [f"<@{user_id}>" for user_id, can_connect, _ in overrides if can_connect == 1]
                blocked = [f"<@{user_id}>" for user_id, can_connect, _ in overrides if can_connect == 0]
                owner = guild.get_member(room_row[0])
                embed = discord.Embed(
                    title=f"Temporary Room · {channel.name}",
                    description=f"Owner: {owner.mention if owner else f'<@{room_row[0]}>'}",
                    color=0xFFDD00,
                )
                embed.add_field(name="Access", value="Locked" if room_row[1] else "Unlocked", inline=True)
                embed.add_field(name="User limit", value=str(room_row[2] or "Unlimited"), inline=True)
                embed.add_field(name="Region", value=room_row[3] or "Auto", inline=True)
                embed.add_field(name="Chat", value="Open" if room_row[4] else "Closed", inline=True)
                embed.add_field(name="Visibility", value="Hidden" if room_row[5] else "Visible", inline=True)
                embed.add_field(
                    name="Allowed",
                    value=_format_room_member_mentions(allowed),
                    inline=False,
                )
                embed.add_field(
                    name="Blocked",
                    value=_format_room_member_mentions(blocked),
                    inline=False,
                )
                return embed
            if action == "reset":
                overwrites = await self._room_overwrites(guild, member)
                await channel.edit(
                    overwrites=overwrites,
                    user_limit=0,
                    rtc_region=None,
                    reason=f"Room settings reset by {member}",
                )
                await self.bot.db.execute(
                    "DELETE FROM denycord_voice_overrides WHERE guild_id=? AND channel_id=?",
                    (guild.id, channel.id),
                )
                await self.bot.db.execute(
                    "UPDATE denycord_voice_rooms SET locked=0, user_limit=0, rtc_region=NULL, chat_open=1, hidden=0 "
                    "WHERE guild_id=? AND channel_id=?",
                    (guild.id, channel.id),
                )
                await self.bot.db.commit()
                return "Room permissions, access rules, region, and user limit have been reset."
            if action == "transfer":
                target = await self._resolve_room_member(guild, value)
                if target is None or target not in channel.members:
                    return "The new owner must be a member currently in your room."
                if target.id == member.id:
                    return "You already own this room."
                await channel.set_permissions(
                    target, view_channel=True, connect=True,
                    reason=f"Room ownership transferred by {member}",
                )
                await self.bot.db.execute(
                    "UPDATE denycord_voice_rooms SET owner_id=? WHERE guild_id=? AND channel_id=?",
                    (target.id, guild.id, channel.id),
                )
                await self.bot.db.commit()
                return f"Room ownership transferred to {target.mention}."
            if action in {"mute", "unmute", "deafen", "undeafen", "kick", "summon", "invite"}:
                target = await self._resolve_room_member(guild, value)
                if target is None:
                    return "Provide a member mention or ID from this server."
                bot_member = guild.me
                if target.id in {member.id, guild.owner_id}:
                    return "You cannot use this action on yourself or the server owner."
                if target.top_role >= member.top_role and member.id != guild.owner_id:
                    return "You can only manage members whose highest role is below yours."
                if bot_member is None or target.top_role >= bot_member.top_role:
                    return "I can only manage members below my highest role."
                if action == "summon":
                    if not bot_member.guild_permissions.move_members:
                        return "I need Move Members permission to summon users."
                    await target.move_to(channel, reason=f"Summoned by room owner {member}")
                    return f"Summoned {target.mention} to your room."
                if action == "invite":
                    if not bot_member.guild_permissions.create_instant_invite:
                        return "I need Create Invite permission to invite someone to this room."
                    invite = await channel.create_invite(
                        max_age=3600,
                        max_uses=1,
                        unique=True,
                        reason=f"Temporary room invite created by {member}",
                    )
                    try:
                        await target.send(f"You were invited to {channel.mention}: {invite.url}")
                    except discord.HTTPException:
                        return f"Invite created, but I couldn't DM {target.mention}: {invite.url}"
                    return f"A one-hour, single-use invite was sent to {target.mention}."
                if target not in channel.members:
                    return "That member is not in your room."
                if action == "kick":
                    await target.move_to(None, reason=f"Removed from managed room by {member}")
                    return f"Removed {target.mention} from your room."
                if action in {"deafen", "undeafen"}:
                    if not bot_member.guild_permissions.deafen_members:
                        return "I need Deafen Members permission to change server deafen state."
                    await target.edit(
                        deafen=action == "deafen",
                        reason=f"Room {action} by {member}",
                    )
                    verb = "deafened" if action == "deafen" else "undeafened"
                    return f"{target.mention} was {verb}."
                await channel.set_permissions(
                    target,
                    speak=action == "unmute",
                    reason=f"Room {action} by {member}",
                )
                return f"{target.mention} has been {action}d."
            if action == "kick":
                target = await self._resolve_room_member(guild, value)
                if target is None:
                    return "Provide a member mention or ID from this server."
                if target.id == member.id or target.id == guild.owner_id:
                    return "You cannot remove yourself or the server owner from a room."
                bot_member = guild.me
                if target.top_role >= member.top_role and member.id != guild.owner_id:
                    return "You can only remove members whose highest role is below yours."
                if bot_member is None or target.top_role >= bot_member.top_role:
                    return "I can only disconnect members whose highest role is below my highest role."
                if target not in channel.members:
                    return "That member is not in your room."
                await channel.set_permissions(
                    target, view_channel=False, connect=False,
                    reason=f"Removed from managed room by {member}",
                )
                await target.move_to(None, reason=f"Removed from managed room by {member}")
                return f"Removed {target.mention} from your room."
            if action == "claim":
                return f"You now own {channel.mention}."
            if action == "refresh":
                if not room_row[6]:
                    return "No saved room panel was found to refresh."
                panel_message = await channel.fetch_message(room_row[6])
                panel_view = await self._room_panel_view(channel, member)
                await edit_message_v2(
                    panel_message,
                    view=panel_view,
                    attachments=[self._panel_image_file(panel_view)],
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return "Room panel refreshed."
            if action == "delete":
                if value != "confirmed":
                    return DeleteRoomConfirmationView(self, member.id)
                await channel.delete(reason=f"Temporary room deleted by {member} ({member.id})")
                await self.bot.db.execute(
                    "DELETE FROM denycord_voice_overrides WHERE guild_id=? AND channel_id=?",
                    (guild.id, channel.id),
                )
                await self.bot.db.execute(
                    "DELETE FROM denycord_voice_rooms WHERE guild_id=? AND channel_id=?",
                    (guild.id, channel.id),
                )
                await self.bot.db.commit()
                return "Temporary voice room deleted."
        except discord.Forbidden:
            log.warning("Discord denied room action %s in channel %s", action, channel.id, exc_info=True)
            return "Discord denied that room change. Check my permissions and role order."
        except discord.HTTPException:
            log.exception("Discord API error performing room action %s in channel %s", action, channel.id)
            return "Discord could not apply that room change. Please try again."
        return "Unknown room action."

    async def _set_room_locked(self, channel, guild, locked, owner, *, hidden=False):
        bot_member = guild.me
        can_view = not hidden
        can_connect = not hidden and not locked
        default_overwrite = channel.overwrites_for(guild.default_role)
        default_overwrite.view_channel = can_view
        default_overwrite.connect = can_connect
        await channel.set_permissions(
            guild.default_role,
            overwrite=default_overwrite,
            reason="Managed room locked" if locked else "Managed room unlocked",
        )
        for target, overwrite in list(channel.overwrites.items()):
            if target in {guild.default_role, owner, bot_member} or isinstance(target, discord.Member):
                continue
            overwrite.view_channel = can_view if (locked or hidden) else None
            overwrite.connect = can_connect if (locked or hidden) else None
            if overwrite.is_empty():
                await channel.set_permissions(target, overwrite=None, reason="Managed room access updated")
            else:
                await channel.set_permissions(target, overwrite=overwrite, reason="Managed room access updated")
        if not locked:
            await self._apply_room_overrides(channel)

    async def _resolve_room_member(self, guild, value):
        text = str(value or "").strip()
        match = re.fullmatch(r"<@!?(\d+)>", text)
        member_id = int(match.group(1)) if match else int(text) if text.isdigit() else None
        if not member_id:
            return None
        member = guild.get_member(member_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(member_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    async def _handle_room_interaction(self, interaction, action, value=None):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("These controls only work in a server.", ephemeral=True)
        try:
            result = await self._perform_room_action(interaction.guild, interaction.user, action, value)
        except aiosqlite.Error:
            await self.bot.db.rollback()
            log.exception("TempVoice interaction database operation failed for %s", action)
            result = "TempVoice could not save or read this room's settings. Try again."
        channel = interaction.user.voice.channel if interaction.user.voice else None
        await self._audit_room_action(
            interaction.guild.id,
            channel.id if channel else None,
            interaction.user.id,
            action,
            result,
        )
        if isinstance(result, discord.ui.View):
            response_kwargs = {
                "content": "Confirm deleting your temporary voice room.",
                "view": result,
            }
        elif isinstance(result, discord.Embed):
            response_kwargs = {"embed": result}
        else:
            response_kwargs = {"content": result}
        if interaction.response.is_done():
            await interaction.followup.send(**response_kwargs, ephemeral=True)
        else:
            await interaction.response.send_message(**response_kwargs, ephemeral=True)

    async def _run_ctx_room_action(self, ctx, action, value=None):
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            return await ctx.send("This command only works in a server.")
        try:
            result = await self._perform_room_action(ctx.guild, ctx.author, action, value)
        except aiosqlite.Error:
            await self.bot.db.rollback()
            log.exception("TempVoice command database operation failed for %s", action)
            result = "TempVoice could not save or read this room's settings. Try again."
        channel = ctx.author.voice.channel if ctx.author.voice else None
        await self._audit_room_action(
            ctx.guild.id,
            channel.id if channel else None,
            ctx.author.id,
            action,
            result,
        )
        if isinstance(result, discord.ui.View):
            await ctx.send(
                "Confirm deleting your temporary voice room.",
                view=result,
                ephemeral=ctx.interaction is not None,
            )
        elif isinstance(result, discord.Embed):
            await ctx.send(embed=result, ephemeral=ctx.interaction is not None)
        else:
            await ctx.send(result, ephemeral=ctx.interaction is not None)

    async def _cleanup_created_hub(self, hub, category, panel=None):
        if panel is not None:
            try:
                await panel.delete(reason="Rolling back incomplete VoiceMaster setup")
            except discord.NotFound:
                pass
            except discord.HTTPException:
                log.exception("Could not remove incomplete VoiceMaster interface %s", panel.id)
        if hub is not None:
            try:
                await hub.delete(reason="Rolling back incomplete VoiceMaster setup")
            except discord.NotFound:
                pass
            except discord.HTTPException:
                log.exception("Could not remove incomplete VoiceMaster hub %s", hub.id)
        if category is not None:
            try:
                await category.delete(reason="Rolling back incomplete VoiceMaster setup")
            except discord.NotFound:
                pass
            except discord.HTTPException:
                log.exception("Could not remove incomplete VoiceMaster category %s", category.id)

    @commands.hybrid_command(name="voicehub", description="Set up or inspect the temporary voice-room hub")
    @commands.has_guild_permissions(manage_guild=True)
    async def voicehub(
        self,
        ctx,
        action: str = "status",
        hub: discord.VoiceChannel = None,
        panel: discord.TextChannel = None,
        category: discord.CategoryChannel = None,
    ):
        if ctx.guild is None:
            return await ctx.send("Voice hubs can only be configured in a server.")
        action = action.casefold()
        if action == "setup":
            bot_member = ctx.guild.me
            if bot_member is None or not bot_member.guild_permissions.manage_channels:
                return await ctx.send("I need Manage Channels permission to create managed rooms.")

            async with self.bot.db.execute(
                "SELECT hub_channel_id, panel_channel_id, category_id, interface_message_id "
                "FROM denycord_voice_settings WHERE guild_id=?",
                (ctx.guild.id,),
            ) as cursor:
                existing = await cursor.fetchone()
            existing_hub = ctx.guild.get_channel(existing[0]) if existing else None
            existing_panel = ctx.guild.get_channel(existing[1]) if existing else None
            existing_category = ctx.guild.get_channel(existing[2]) if existing and existing[2] else None
            if hub is None and isinstance(existing_hub, discord.VoiceChannel):
                hub = existing_hub
            if panel is None and isinstance(existing_panel, discord.TextChannel):
                panel = existing_panel
            if category is None and isinstance(existing_category, discord.CategoryChannel):
                category = existing_category
            if category is None and hub is not None and isinstance(hub.category, discord.CategoryChannel):
                category = hub.category

            created_category = None
            created_hub = None
            created_panel = None
            try:
                if hub is None:
                    if category is None:
                        category = await ctx.guild.create_category(
                            name="VoiceMaster",
                            reason=f"VoiceMaster setup by {ctx.author}",
                        )
                        created_category = category
                    hub = await ctx.guild.create_voice_channel(
                        name="Join to Create",
                        category=category,
                        reason=f"VoiceMaster setup by {ctx.author}",
                    )
                    created_hub = hub
                if panel is None:
                    panel = await ctx.guild.create_text_channel(
                        name="voice-interface",
                        category=category,
                        overwrites={
                            ctx.guild.default_role: discord.PermissionOverwrite(
                                view_channel=True, send_messages=False, read_message_history=True
                            ),
                            bot_member: discord.PermissionOverwrite(
                                view_channel=True, send_messages=True, embed_links=True,
                                read_message_history=True, manage_channels=True,
                            ),
                        },
                        reason=f"VoiceMaster interface setup by {ctx.author}",
                    )
                    created_panel = panel
            except discord.Forbidden:
                log.warning("Discord denied VoiceMaster channel creation in guild %s", ctx.guild.id, exc_info=True)
                await self._cleanup_created_hub(created_hub, created_category, created_panel)
                return await ctx.send("Discord denied VoiceMaster setup. Check my Manage Channels permission.")
            except discord.HTTPException:
                log.exception("Discord API error creating VoiceMaster channels in guild %s", ctx.guild.id)
                await self._cleanup_created_hub(created_hub, created_category, created_panel)
                return await ctx.send("Discord could not create the VoiceMaster channels. Please try again.")

            voice_permissions = hub.permissions_for(bot_member)
            if (
                not voice_permissions.view_channel
                or not voice_permissions.connect
                or not voice_permissions.move_members
                or not bot_member.guild_permissions.manage_channels
            ):
                await self._cleanup_created_hub(created_hub, created_category, created_panel)
                return await ctx.send(
                    "I need View Channel, Connect, Move Members, and Manage Channels for the voice hub."
                )
            panel_permissions = panel.permissions_for(bot_member)
            if not (
                panel_permissions.view_channel
                and panel_permissions.send_messages
                and panel_permissions.attach_files
            ):
                await self._cleanup_created_hub(created_hub, created_category, created_panel)
                return await ctx.send(
                    "I need View Channel, Send Messages, and Attach Files in the voice interface channel."
                )
            try:
                interface_message_id = await self._ensure_voice_interface(
                    ctx.guild,
                    panel,
                    existing[3] if existing and panel.id == existing[1] else None,
                )
                await self.bot.db.execute(
                    "INSERT INTO denycord_voice_settings "
                    "(guild_id, hub_channel_id, panel_channel_id, category_id, interface_message_id, enabled) "
                    "VALUES (?,?,?,?,?,1) ON CONFLICT(guild_id) DO UPDATE SET "
                    "hub_channel_id=excluded.hub_channel_id, panel_channel_id=excluded.panel_channel_id, "
                    "category_id=excluded.category_id, interface_message_id=excluded.interface_message_id, enabled=1",
                    (ctx.guild.id, hub.id, panel.id, category.id if category else None, interface_message_id),
                )
                await self.bot.db.execute(
                    "INSERT INTO denycord_voice_generators "
                    "(guild_id, channel_id, category_id, created_by, created_at) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(guild_id, channel_id) DO UPDATE SET category_id=excluded.category_id",
                    (ctx.guild.id, hub.id, category.id if category else None, ctx.author.id, _now().isoformat()),
                )
                await self.bot.db.commit()
            except aiosqlite.Error:
                await self.bot.db.rollback()
                log.exception("Could not save VoiceMaster configuration for guild %s", ctx.guild.id)
                await self._cleanup_created_hub(created_hub, created_category, created_panel)
                return await ctx.send("I couldn't save the VoiceMaster settings. Any new hub channels were cleaned up where possible.")
            except discord.HTTPException:
                log.exception("Could not post the VoiceMaster interface in guild %s", ctx.guild.id)
                await self._cleanup_created_hub(created_hub, created_category, created_panel)
                return await ctx.send("I couldn't post the voice interface. Check its channel permissions.")
            return await ctx.send(
                f"Voice interface ready in {panel.mention}. Join {hub.mention} to create a temporary room."
            )
        if action in {"disable", "off", "remove"}:
            await self.bot.db.execute(
                "UPDATE denycord_voice_settings SET enabled=0 WHERE guild_id=?", (ctx.guild.id,)
            )
            await self.bot.db.commit()
            return await ctx.send("VoiceMaster creation disabled. Existing rooms and configuration remain available.")
        if action != "status":
            return await ctx.send("Use `setup`, `status`, or `disable`.")
        async with self.bot.db.execute(
            "SELECT hub_channel_id, panel_channel_id, category_id "
            "FROM denycord_voice_settings WHERE guild_id=?", (ctx.guild.id,)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return await ctx.send("No VoiceMaster hub is set up. Use `voicehub setup [hub] [panel] [category]`.")
        hub_channel = ctx.guild.get_channel(row[0])
        panel_channel = ctx.guild.get_channel(row[1])
        category_channel = ctx.guild.get_channel(row[2]) if row[2] else None
        await ctx.send(
            f"Voice hub: {hub_channel.mention if hub_channel else 'missing'} · "
            f"Control panel: {panel_channel.mention if panel_channel else 'missing'} · "
            f"Category: {category_channel.name if category_channel else 'none'}."
        )

    @commands.hybrid_group(
        name="room",
        aliases=("temp_vc", "temp", "tv", "temp_voice"),
        invoke_without_command=True,
    )
    async def room(self, ctx):
        if ctx.guild is None:
            return await ctx.send("Temporary voice rooms can only be configured in a server.")
        async with self.bot.db.execute(
            "SELECT hub_channel_id, category_id FROM denycord_voice_settings WHERE guild_id=?",
            (ctx.guild.id,),
        ) as cursor:
            settings = await cursor.fetchone()
        hub = ctx.guild.get_channel(settings[0]) if settings else None
        category = ctx.guild.get_channel(settings[1]) if settings and settings[1] else None
        embed = discord.Embed(
            title="Temporary Voice Rooms",
            description=(
                f"Join {hub.mention} to create an open temporary room."
                if hub else "Join-to-create is not configured in this server yet."
            ),
            color=0xFFDD00,
        )
        embed.add_field(
            name="Admin setup · Manage Server",
            value="`room set_channel <voice-channel>` · `room set_category <category>`\n"
            "Or use `voicehub setup [hub] [panel] [category]`.",
            inline=False,
        )
        embed.add_field(
            name="Room controls",
            value="`room info` shows the complete owner command list.",
            inline=False,
        )
        if settings:
            embed.add_field(
                name="Category",
                value=category.name if category else "None",
                inline=True,
            )
        await ctx.send(embed=embed, ephemeral=ctx.interaction is not None)

    @room.command(
        name="info",
        description="Show the temporary voice-room command reference",
        with_app_command=False,
    )
    async def room_info(self, ctx):
        embed = discord.Embed(
            title="Temporary Voice Room Commands",
            description=(
                "Join the configured voice hub to create a room. Room-owner commands "
                "require you to be connected to a managed room."
            ),
            color=0xFFDD00,
        )
        embed.add_field(
            name="Admin setup · Manage Server",
            value=(
                "`/tempvoice setup` · `/tempvoice set-generator` · `/tempvoice set-category` · "
                "`/tempvoice set-panel` · `/tempvoice set-staff`\n"
                "`/tempvoice config` · `/tempvoice stats` · `/tempvoice cleanup` · "
                "`/tempvoice backup` · `/tempvoice restore` · `/tempvoice disable` · `/tempvoice enable`\n"
                "Prefix setup remains available through `room set_channel`, `room set_category`, and `voicehub setup`."
            ),
            inline=False,
        )
        embed.add_field(
            name="Room controls",
            value=(
                "`/voice name` · `/voice limit` · `/voice lock` · `/voice unlock` · `/voice hide` · `/voice unhide` · "
                "`/voice status` · `/voice reset` · `/voice delete` · `/voice interface`\n"
                "`/voice permit` · `/voice reject` · `/voice trust` · `/voice untrust` · `/voice block` · "
                "`/voice unblock` · `/voice kick` · `/voice mute` · `/voice unmute` · `/voice deafen` · "
                "`/voice undeafen` · `/voice transfer` · `/voice claim` · `/voice invite`\n"
                "`/voice settings bitrate` · `/voice settings region` · `/voice settings textchat`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Discord limitation",
            value="Voice channels do not support slowmode; no slowmode command is registered.",
            inline=False,
        )
        await ctx.send(embed=embed, ephemeral=ctx.interaction is not None)

    @room.command(name="set_channel", description="Set the join-to-create voice channel")
    @commands.has_guild_permissions(manage_guild=True)
    async def room_set_channel(self, ctx, hub: discord.VoiceChannel):
        if ctx.guild is None:
            return await ctx.send("Voice hubs can only be configured in a server.")
        bot_member = ctx.guild.me
        panel = None
        category_id = None
        async with self.bot.db.execute(
            "SELECT panel_channel_id, category_id FROM denycord_voice_settings WHERE guild_id=?",
            (ctx.guild.id,),
        ) as cursor:
            settings = await cursor.fetchone()
        if settings:
            panel = ctx.guild.get_channel(settings[0])
            category_id = settings[1]
        if not isinstance(panel, discord.TextChannel) and isinstance(ctx.channel, discord.TextChannel):
            panel = ctx.channel
        if not isinstance(panel, discord.TextChannel):
            return await ctx.send(
                "Choose a text control-panel channel with `voicehub setup [hub] [panel] [category]` first."
            )
        if bot_member is None or not bot_member.guild_permissions.manage_channels:
            return await ctx.send("I need Manage Channels permission to create managed rooms.")
        hub_permissions = hub.permissions_for(bot_member)
        panel_permissions = panel.permissions_for(bot_member)
        if not (
            hub_permissions.view_channel
            and hub_permissions.connect
            and hub_permissions.move_members
            and bot_member.guild_permissions.move_members
        ):
            return await ctx.send("I need View Channel, Connect, and Move Members permissions in the voice hub.")
        if not (
            panel_permissions.view_channel
            and panel_permissions.send_messages
            and panel_permissions.attach_files
        ):
            return await ctx.send(
                "I need View Channel, Send Messages, and Attach Files in the control-panel channel."
            )
        await self.bot.db.execute(
            "INSERT INTO denycord_voice_settings (guild_id, hub_channel_id, panel_channel_id, category_id) "
            "VALUES (?,?,?,?) ON CONFLICT(guild_id) DO UPDATE SET "
            "hub_channel_id=excluded.hub_channel_id, panel_channel_id=excluded.panel_channel_id",
            (ctx.guild.id, hub.id, panel.id, category_id),
        )
        await self.bot.db.execute(
            "INSERT INTO denycord_voice_generators "
            "(guild_id, channel_id, category_id, created_by, created_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(guild_id, channel_id) DO UPDATE SET category_id=excluded.category_id",
            (ctx.guild.id, hub.id, category_id, ctx.author.id, _now().isoformat()),
        )
        await self.bot.db.execute(
            "UPDATE denycord_voice_settings SET enabled=1 WHERE guild_id=?", (ctx.guild.id,)
        )
        await self.bot.db.commit()
        await ctx.send(f"Join-to-create channel set to {hub.mention}.")

    @room.command(name="set_category", description="Set the category for new temporary voice rooms")
    @commands.has_guild_permissions(manage_guild=True)
    async def room_set_category(self, ctx, category: discord.CategoryChannel = None):
        if ctx.guild is None:
            return await ctx.send("Voice hubs can only be configured in a server.")
        async with self.bot.db.execute(
            "SELECT hub_channel_id FROM denycord_voice_settings WHERE guild_id=?",
            (ctx.guild.id,),
        ) as cursor:
            settings = await cursor.fetchone()
        if not settings:
            return await ctx.send("Set a join-to-create channel first with `room set_channel #voice-channel`.")
        await self.bot.db.execute(
            "UPDATE denycord_voice_settings SET category_id=? WHERE guild_id=?",
            (category.id if category else None, ctx.guild.id),
        )
        async with self.bot.db.execute(
            "SELECT hub_channel_id FROM denycord_voice_settings WHERE guild_id=?",
            (ctx.guild.id,),
        ) as cursor:
            primary_hub = await cursor.fetchone()
        if primary_hub:
            await self.bot.db.execute(
                "UPDATE denycord_voice_generators SET category_id=? "
                "WHERE guild_id=? AND channel_id=?",
                (category.id if category else None, ctx.guild.id, primary_hub[0]),
            )
        await self.bot.db.commit()
        if category:
            await ctx.send(f"New temporary rooms will be created in **{category.name}**.")
        else:
            await ctx.send("The temporary-room category has been cleared.")

    @room.command(name="lock", description="Lock your managed voice room")
    async def room_lock(self, ctx):
        await self._run_ctx_room_action(ctx, "lock")

    @room.command(name="unlock", description="Unlock your managed voice room")
    async def room_unlock(self, ctx):
        await self._run_ctx_room_action(ctx, "unlock")

    @room.command(name="hide", description="Hide your voice room from other members")
    async def room_hide(self, ctx):
        await self._run_ctx_room_action(ctx, "hide")

    @room.command(name="unhide", description="Make your voice room visible again")
    async def room_unhide(self, ctx):
        await self._run_ctx_room_action(ctx, "unhide")

    @room.command(name="rename", description="Rename your managed voice room")
    async def room_rename(self, ctx, *, name: str):
        await self._run_ctx_room_action(ctx, "rename", name)

    @room.command(name="limit", description="Set your managed voice-room user limit")
    async def room_limit(self, ctx, limit: int):
        await self._run_ctx_room_action(ctx, "limit", limit)

    @room.command(name="bitrate", description="Set your managed voice-room bitrate")
    async def room_bitrate(self, ctx, bitrate_kbps: int):
        await self._run_ctx_room_action(ctx, "bitrate", bitrate_kbps)

    @room.command(name="permit", description="Permit a member in your managed room")
    async def room_permit(self, ctx, member: discord.Member):
        await self._run_ctx_room_action(ctx, "permit", member.id)

    @room.command(name="kick", description="Disconnect a member from your managed room")
    async def room_kick(self, ctx, member: discord.Member):
        await self._run_ctx_room_action(ctx, "kick", member.id)

    @room.command(name="claim", description="Claim an unowned managed room")
    async def room_claim(self, ctx):
        await self._run_ctx_room_action(ctx, "claim")

    @room.command(name="transfer", description="Transfer room ownership to a member inside the room")
    async def room_transfer(self, ctx, member: discord.Member):
        await self._run_ctx_room_action(ctx, "transfer", member.id)

    @room.command(name="allow", description="Allow a member to join and chat in your room")
    async def room_allow(self, ctx, member: discord.Member):
        await self._run_ctx_room_action(ctx, "allow", member.id)

    @room.command(name="block", description="Block a member from joining and chatting in your room")
    async def room_block(self, ctx, member: discord.Member):
        await self._run_ctx_room_action(ctx, "block", member.id)

    @room.command(name="ban", description="Ban a member from your managed voice room")
    async def room_ban(self, ctx, member: discord.Member):
        await self._run_ctx_room_action(ctx, "ban", member.id)

    @room.command(name="clear", description="Clear one or all saved member access overrides")
    async def room_clear(self, ctx, member: discord.Member = None):
        await self._run_ctx_room_action(ctx, "clear", member.id if member else None)

    @room.command(name="openchat", description="Open text-in-voice chat")
    async def room_openchat(self, ctx):
        await self._run_ctx_room_action(ctx, "openchat")

    @room.command(name="closechat", description="Close text-in-voice chat for other members")
    async def room_closechat(self, ctx):
        await self._run_ctx_room_action(ctx, "closechat")

    @room.command(name="mute", description="Mute a member in your room")
    async def room_mute(self, ctx, member: discord.Member):
        await self._run_ctx_room_action(ctx, "mute", member.id)

    @room.command(name="unmute", description="Unmute a member in your room")
    async def room_unmute(self, ctx, member: discord.Member):
        await self._run_ctx_room_action(ctx, "unmute", member.id)

    @room.command(name="summon", description="Move a member to your room")
    async def room_summon(self, ctx, member: discord.Member):
        await self._run_ctx_room_action(ctx, "summon", member.id)

    @room.command(name="region", description="Set or inspect your room's voice region")
    async def room_region(self, ctx, *, region: str = ""):
        await self._run_ctx_room_action(ctx, "region", region)

    @room.command(name="status", description="Show room settings and access overrides")
    async def room_status(self, ctx):
        await self._run_ctx_room_action(ctx, "status")

    @room.command(name="reset", description="Reset room settings and saved overrides")
    async def room_reset(self, ctx):
        await self._run_ctx_room_action(ctx, "reset")

    def _snapshot(self, guild, trigger, schedule_enabled, next_run_at):
        created_at = _now().isoformat()
        return {
            "metadata": {
                "format": "falcon-role-channel-structure-v1",
                "guild_id": guild.id,
                "guild_name": guild.name,
                "created_at": created_at,
                "trigger": trigger,
                "schedule": {
                    "enabled": bool(schedule_enabled),
                    "interval_seconds": 3600,
                    "next_run_at": next_run_at,
                },
            },
            "roles": [
                {
                    "name": role.name,
                    "color": role.color.value,
                    "permissions": role.permissions.value,
                    "hoist": role.hoist,
                    "mentionable": role.mentionable,
                }
                for role in guild.roles if not role.is_default() and not role.managed
            ],
            "categories": [category.name for category in guild.categories],
            "channels": [
                {
                    "name": channel.name,
                    "type": str(channel.type),
                    "category": channel.category.name if channel.category else None,
                    "topic": getattr(channel, "topic", None),
                    "nsfw": getattr(channel, "nsfw", False),
                    "slowmode": getattr(channel, "slowmode_delay", 0),
                }
                for channel in guild.channels
                if not isinstance(channel, discord.CategoryChannel)
                and str(channel.type) in {"text", "news", "voice"}
            ],
        }

    async def _create_and_deliver_backup(self, guild, channel, trigger):
        async with self.bot.db.execute(
            "SELECT enabled, next_run_at FROM denycord_backup_settings WHERE guild_id=?",
            (guild.id,),
        ) as cursor:
            schedule = await cursor.fetchone()
        snapshot = self._snapshot(
            guild,
            trigger,
            schedule[0] if schedule else False,
            schedule[1] if schedule else None,
        )
        snapshot_text = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
        snapshot_bytes = snapshot_text.encode("utf-8")
        if len(snapshot_bytes) > MAX_BACKUP_BYTES:
            log.warning("Structure backup too large for delivery in guild %s", guild.id)
            return None, "The structure backup exceeds the 8 MiB delivery limit; no backup was recorded."
        created_at = snapshot["metadata"]["created_at"]
        cursor = await self.bot.db.execute(
            "INSERT INTO denycord_backups "
            "(guild_id, created_at, trigger, channel_id, snapshot) VALUES (?,?,?,?,?)",
            (guild.id, created_at, trigger, channel.id, snapshot_text),
        )
        backup_id = cursor.lastrowid
        await self.bot.db.execute(
            "UPDATE denycord_backup_settings SET last_backup_at=?, last_backup_id=? WHERE guild_id=?",
            (created_at, backup_id, guild.id),
        )
        await self.bot.db.commit()
        embed = discord.Embed(
            title="Falcon Server Structure Backup",
            description=(
                f"Backup `{backup_id}` for **{discord.utils.escape_markdown(guild.name)}**. "
                "This file is a record only; Falcon will never restore it automatically."
            ),
            color=0xFFDD00,
            timestamp=_as_utc(created_at),
        )
        embed.add_field(name="Roles", value=str(len(snapshot["roles"])))
        embed.add_field(name="Categories", value=str(len(snapshot["categories"])))
        embed.add_field(name="Channels", value=str(len(snapshot["channels"])))
        embed.add_field(name="Trigger", value=trigger.title())
        file = discord.File(io.BytesIO(snapshot_bytes), filename=f"falcon-structure-{guild.id}-{backup_id}.json")
        try:
            message = await channel.send(
                embed=embed, file=file, allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.Forbidden:
            log.warning("Discord denied scheduled backup delivery for guild %s", guild.id, exc_info=True)
            return backup_id, "The snapshot is saved in Falcon's database, but Discord denied channel delivery."
        except discord.HTTPException:
            log.exception("Discord API error delivering structure backup for guild %s", guild.id)
            return backup_id, "The snapshot is saved in Falcon's database, but Discord could not deliver the file."
        await self.bot.db.execute(
            "UPDATE denycord_backups SET message_id=? WHERE backup_id=?", (message.id, backup_id)
        )
        await self.bot.db.commit()
        return backup_id, f"Backup `{backup_id}` delivered to {channel.mention}."

    async def _run_due_backups(self):
        now = _now()
        async with self.bot.db.execute(
            "SELECT guild_id, channel_id, next_run_at FROM denycord_backup_settings "
            "WHERE enabled=1 AND channel_id IS NOT NULL"
        ) as cursor:
            rows = await cursor.fetchall()
        for guild_id, channel_id, next_run_at in rows:
            if not bot_owns_guild(self.bot, guild_id):
                continue
            if not next_run_at:
                due = True
            else:
                try:
                    due = _as_utc(next_run_at) <= now
                except (TypeError, ValueError):
                    log.warning("Invalid backup schedule timestamp for guild %s", guild_id)
                    due = True
            if not due:
                continue
            next_run = (now + BACKUP_INTERVAL).isoformat()
            await self.bot.db.execute(
                "UPDATE denycord_backup_settings SET next_run_at=? WHERE guild_id=?",
                (next_run, guild_id),
            )
            await self.bot.db.commit()
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            channel = guild.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await guild.fetch_channel(channel_id)
                except discord.NotFound:
                    log.warning("Configured backup channel %s was deleted in guild %s", channel_id, guild_id)
                    continue
                except discord.Forbidden:
                    log.warning("Cannot access backup channel %s in guild %s", channel_id, guild_id, exc_info=True)
                    continue
                except discord.HTTPException:
                    log.exception("Discord API error fetching backup channel %s", channel_id)
                    continue
            if not isinstance(channel, discord.TextChannel):
                log.warning("Configured backup channel %s is not a text channel", channel_id)
                continue
            if not _has_text_channel_permissions(channel, guild.me):
                log.warning("Missing backup delivery permissions in channel %s", channel_id)
                continue
            await self._create_and_deliver_backup(guild, channel, "scheduled")

    @commands.hybrid_command(name="backup_schedule", description="Schedule hourly server-structure backups to a channel")
    @commands.has_guild_permissions(manage_guild=True)
    async def backup_schedule(self, ctx, channel: discord.TextChannel):
        if channel is None:
            return await ctx.send("Choose a text channel: `backup_schedule #channel`.")
        return await self.serverbackup(ctx, "channel", channel)

    @commands.hybrid_command(name="backup_unschedule", description="Disable hourly server-structure backups")
    @commands.has_guild_permissions(manage_guild=True)
    async def backup_unschedule(self, ctx):
        return await self.serverbackup(ctx, "disable")

    @commands.hybrid_command(name="serverbackup", description="Configure hourly non-destructive structure backups")
    @commands.has_guild_permissions(manage_guild=True)
    async def serverbackup(self, ctx, action: str = "status", channel: discord.TextChannel = None):
        if ctx.guild is None:
            return await ctx.send("Backups can only be configured in a server.")
        action = action.casefold()
        guild_id = ctx.guild.id
        if action == "channel":
            if channel is None:
                return await ctx.send("Choose a text channel: `serverbackup channel #channel`.")
            if not _has_text_channel_permissions(channel, ctx.guild.me):
                return await ctx.send(
                    "I need View Channel, Send Messages, Embed Links, and Attach Files in that channel."
                )
            await self.bot.db.execute(
                "INSERT INTO denycord_backup_settings (guild_id, channel_id) VALUES (?,?) "
                "ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id",
                (guild_id, channel.id),
            )
            await self.bot.db.commit()
            return await ctx.send(f"Backup delivery channel set to {channel.mention}.")
        if action in {"enable", "on"}:
            async with self.bot.db.execute(
                "SELECT channel_id FROM denycord_backup_settings WHERE guild_id=?", (guild_id,)
            ) as cursor:
                row = await cursor.fetchone()
            if not row or row[0] is None:
                return await ctx.send("Choose a channel first with `serverbackup channel #channel`.")
            target = ctx.guild.get_channel(row[0])
            if not isinstance(target, discord.TextChannel) or not _has_text_channel_permissions(target, ctx.guild.me):
                return await ctx.send("The configured channel is unavailable or I lack delivery permissions.")
            next_run = (_now() + BACKUP_INTERVAL).isoformat()
            await self.bot.db.execute(
                "UPDATE denycord_backup_settings SET enabled=1, next_run_at=? WHERE guild_id=?",
                (next_run, guild_id),
            )
            await self.bot.db.commit()
            return await ctx.send(f"Hourly backups enabled. The first backup is due <t:{int(_as_utc(next_run).timestamp())}:R>.")
        if action in {"disable", "off"}:
            await self.bot.db.execute(
                "UPDATE denycord_backup_settings SET enabled=0, next_run_at=NULL WHERE guild_id=?",
                (guild_id,),
            )
            await self.bot.db.commit()
            return await ctx.send("Hourly backups disabled. Existing snapshots are unchanged.")
        if action == "now":
            async with self.bot.db.execute(
                "SELECT channel_id FROM denycord_backup_settings WHERE guild_id=?", (guild_id,)
            ) as cursor:
                row = await cursor.fetchone()
            if not row or row[0] is None:
                return await ctx.send("Choose a channel first with `serverbackup channel #channel`.")
            target = ctx.guild.get_channel(row[0])
            if not isinstance(target, discord.TextChannel) or not _has_text_channel_permissions(target, ctx.guild.me):
                return await ctx.send("The configured channel is unavailable or I lack delivery permissions.")
            _, result = await self._create_and_deliver_backup(ctx.guild, target, "manual")
            return await ctx.send(result)
        if action != "status":
            return await ctx.send("Use `channel`, `enable`, `disable`, `now`, or `status`.")
        async with self.bot.db.execute(
            "SELECT channel_id, enabled, next_run_at, last_backup_at, last_backup_id "
            "FROM denycord_backup_settings WHERE guild_id=?", (guild_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return await ctx.send("No backup channel is configured. Use `serverbackup channel #channel`.")
        channel_id, enabled, next_run_at, last_backup_at, last_backup_id = row
        target = ctx.guild.get_channel(channel_id) if channel_id else None
        schedule = f"Enabled; next run <t:{int(_as_utc(next_run_at).timestamp())}:R>." if enabled and next_run_at else (
            "Enabled; next run will be scheduled shortly." if enabled else "Disabled."
        )
        last = (
            f"Backup `{last_backup_id}` · <t:{int(_as_utc(last_backup_at).timestamp())}:R>"
            if last_backup_at and last_backup_id else "None yet"
        )
        embed = discord.Embed(title="Hourly Server Backups", description=schedule, color=0xFFDD00)
        embed.add_field(name="Channel", value=target.mention if target else "Not configured or unavailable")
        embed.add_field(name="Last backup", value=last)
        embed.set_footer(text="Backups contain role/channel structure and are never restored automatically.")
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(DenycordVoiceBackup(bot))
