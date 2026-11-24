import asyncio
import datetime
import io
import json
import logging
import math
import os
import random
import re
import shlex
import unicodedata
from typing import Literal
from urllib.parse import quote, urlsplit
from pathlib import Path

import aiohttp
import discord
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from card_theme import draw_bot_icon, normalize_card_text
from components_v2 import (
    edit_interaction_v2,
    edit_message_v2,
    send_interaction_v2,
    send_v2,
)
from premium_features import utc_now
from time_parser import parse_duration


ALIAS_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
VARIABLE_PATTERN = re.compile(r"\{([a-z_][a-z0-9_.]*)\}", re.IGNORECASE)
AUTORESPONDER_ACTION_PATTERN = re.compile(r"\{(delete|sendto:[^}]+|embed:[^}]+)\}", re.IGNORECASE)
CUSTOM_COMMAND_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
AUTORESPONDER_MATCH_MODES = ("contains", "exact", "startswith", "endswith")
GREETING_MESSAGE_COLUMNS = {
    "join": "join_message",
    "leave": "leave_message",
    "vanity": "vanity_message",
}
GREETING_CHANNEL_COLUMNS = {"join": "join_channel_id", "leave": "leave_channel_id"}
DEFAULT_BOOST_MESSAGE = "Thanks {user.mention} for boosting {server.name}! Boost #{boost_count}."
BOOST_EMOJI = "<:boosters:1424797130677293219>"
COMMUNITY_EVENT_KINDS = ("join", "leave", "boost")
GIPHY_API_BASE_URL = "https://api.giphy.com/v1"
GIPHY_RESULT_LIMIT = 25
GIPHY_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
SUCCESS_TICK = "<:fp_tick:1556939072646287360>"
DETAIL_ARROW = "<:fp_arrow:1551879040292360225>"
try:
    COMMUNITY_EMOJI_MANIFEST = json.loads(
        (Path(__file__).resolve().parent / "emojis.json").read_text(encoding="utf-8")
    )
except (OSError, json.JSONDecodeError):
    COMMUNITY_EMOJI_MANIFEST = {}
SUCCESS_TICK = COMMUNITY_EMOJI_MANIFEST.get("fp_tick", SUCCESS_TICK)
DETAIL_ARROW = COMMUNITY_EMOJI_MANIFEST.get("fp_arrow", DETAIL_ARROW)
ROLE_ALIAS_TICK = "<:fp_tick:1556939072646287360>"
ROLE_ALIAS_CROSS = "<:cross:1559374062281826397>"
ROLE_ALIAS_ARROW = "<:fp_arrow:1551879040292360225>"
ROLE_ALIAS_SETTING = "<:setting:1561336841842593862>"
ROLE_ALIAS_DOT = "<:dot:1551879036022816780>"
DEFAULT_EMBED_CONFIG = {
    "title": "{username}",
    "description": "",
    "color": "#FFDD00",
    "url": "",
    "timestamp": False,
    "thumbnail": "{avatar}",
    "image": "",
    "author": {"name": "", "url": "", "icon_url": ""},
    "footer": {"text": "", "icon_url": ""},
    "fields": [],
}


def _matches_autoresponder_trigger(content, trigger, mode, *, case_sensitive=False):
    if not case_sensitive:
        content = content.casefold()
        trigger = trigger.casefold()
    trigger = trigger.strip()
    if not trigger:
        return False
    if mode == "exact":
        return content.strip() == trigger
    if mode in {"contains", "includes"}:
        return trigger in content
    if mode in {"startswith", "starts_with", "starts"}:
        candidate = content.lstrip()
        return candidate.startswith(trigger) and (
            len(candidate) == len(trigger) or candidate[len(trigger)].isspace()
        )
    if mode in {"endswith", "ends_with", "ends"}:
        candidate = content.rstrip()
        start = len(candidate) - len(trigger)
        return start >= 0 and candidate.endswith(trigger) and (
            start == 0 or candidate[start - 1].isspace()
        )
    return False


def _autoresponder_arguments(content, trigger, mode, *, case_sensitive=False):
    if mode == "exact":
        return []
    search_content = content if case_sensitive else content.casefold()
    search_trigger = trigger if case_sensitive else trigger.casefold()
    if mode in {"startswith", "starts_with", "starts"}:
        remainder = content.lstrip()[len(trigger.strip()):]
    elif mode in {"endswith", "ends_with", "ends"}:
        remainder = content.rstrip()[:-len(trigger.strip())]
    else:
        match_at = search_content.find(search_trigger)
        if match_at < 0:
            return []
        remainder = f"{content[:match_at]} {content[match_at + len(trigger):]}"
    return remainder.split()


def render_member_event_card(
    avatar_bytes,
    username,
    guild_name,
    event_kind,
    detail,
    *,
    guild_icon_bytes=None,
    bot_icon_bytes=None,
):
    themes = {
        "boost": ("THANK YOU FOR THE BOOST", "#A23B5A"),
        "join": ("WELCOME TO THE SERVER", "#A23B5A"),
        "leave": ("WE'LL MISS YOU", "#73727D"),
    }
    headline, accent = themes.get(event_kind, ("FALCON COMMUNITY", "#A23B5A"))
    width, height = 1400, 620
    image = Image.new("RGB", (width, height), "#FFF9F5")
    draw = ImageDraw.Draw(image)
    start = (255, 250, 246)
    end = (249, 229, 234)
    for line_y in range(height):
        blend = line_y / max(1, height - 1)
        color = tuple(
            round(start[index] + (end[index] - start[index]) * blend)
            for index in range(3)
        )
        draw.line((0, line_y, width, line_y), fill=color)
    draw.rounded_rectangle(
        (28, 28, width - 28, height - 28),
        radius=38,
        fill="#FFFEFC",
        outline="#E7C78F",
        width=3,
    )
    draw.rounded_rectangle((53, 53, 69, height - 53), radius=8, fill="#F5C7D1")
    draw_bot_icon(image, (1160, 66, 1310, 205), bot_icon_bytes)

    try:
        title_font = ImageFont.truetype("DejaVuSans-Bold.ttf", 33)
        name_font = ImageFont.truetype("DejaVuSans-Bold.ttf", 59)
        headline_font = ImageFont.truetype("DejaVuSans-Bold.ttf", 49)
        detail_font = ImageFont.truetype("DejaVuSans.ttf", 40)
        small_font = ImageFont.truetype("DejaVuSans.ttf", 31)
    except OSError:
        title_font = ImageFont.load_default()
        name_font = title_font
        headline_font = title_font
        detail_font = title_font
        small_font = title_font

    avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
    avatar = ImageOps.fit(avatar, (238, 238))
    avatar_mask = Image.new("L", (238, 238), 0)
    ImageDraw.Draw(avatar_mask).rounded_rectangle((0, 0, 237, 237), radius=28, fill=255)
    image.paste(avatar, (103, 191), avatar_mask)
    draw.rounded_rectangle((97, 185, 347, 435), radius=34, outline="#E7C78F", width=4)

    if guild_icon_bytes:
        try:
            server_icon = Image.open(io.BytesIO(guild_icon_bytes)).convert("RGBA")
            server_icon = ImageOps.fit(server_icon, (58, 58))
            server_mask = Image.new("L", (58, 58), 0)
            ImageDraw.Draw(server_mask).rounded_rectangle((0, 0, 57, 57), radius=12, fill=255)
            image.paste(server_icon, (1206, 252), server_mask)
        except (OSError, ValueError):
            pass

    draw.text((103, 70), "F A L C O N   /   C O M M U N I T Y", font=title_font, fill="#9A7A55")
    draw.text((103, 132), headline, font=headline_font, fill=accent)

    def draw_fitted(text, position, font, fill, max_width):
        safe_text = normalize_card_text(text, font)
        while safe_text and draw.textlength(safe_text, font=font) > max_width:
            safe_text = safe_text[:-2].rstrip() + "…"
        draw.text(position, safe_text, font=font, fill=fill)

    draw_fitted(username, (390, 215), name_font, "#422C34", 760)
    draw_fitted(guild_name, (393, 319), detail_font, "#806D70", 760)
    draw.rounded_rectangle((390, 394, 1255, 397), radius=2, fill="#F0DDE0")
    draw_fitted(detail, (393, 414), detail_font, accent, 760)
    draw.text((103, 488), "A MOMENT WORTH CELEBRATING", font=small_font, fill="#A88D81")
    draw.text((390, 540), "MADE WITH LOVE BY FALCON", font=small_font, fill="#A88D81")

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def render_ping_card(
    gateway_ms,
    api_ms,
    shard_id,
    shard_count,
    guild_count,
    member_count,
    uptime,
    database_ms=None,
    *,
    bot_icon_bytes=None,
    bot_name="Falcon",
    bot_id=None,
    command_count=0,
    memory_mb=None,
    python_version="Unknown",
):
    width, height = 1400, 860
    image = Image.new("RGB", (width, height), "#FFF9F5")
    draw = ImageDraw.Draw(image)
    start = (255, 250, 246)
    end = (249, 229, 234)
    for line_y in range(height):
        blend = line_y / max(1, height - 1)
        color = tuple(
            round(start[index] + (end[index] - start[index]) * blend)
            for index in range(3)
        )
        draw.line((0, line_y, width, line_y), fill=color)
    draw.rounded_rectangle(
        (28, 28, width - 28, height - 28),
        radius=38,
        fill="#FFFEFC",
        outline="#E7C78F",
        width=3,
    )
    draw.rounded_rectangle((53, 53, 69, height - 53), radius=8, fill="#F5C7D1")
    draw_bot_icon(image, (1120, 58, 1305, 226), bot_icon_bytes)

    try:
        title_font = ImageFont.truetype("DejaVuSans-Bold.ttf", 35)
        hero_font = ImageFont.truetype("DejaVuSans-Bold.ttf", 83)
        label_font = ImageFont.truetype("DejaVuSans-Bold.ttf", 27)
        value_font = ImageFont.truetype("DejaVuSans-Bold.ttf", 33)
        small_font = ImageFont.truetype("DejaVuSans.ttf", 28)
    except OSError:
        title_font = hero_font = label_font = value_font = small_font = ImageFont.load_default()

    draw.text((104, 68), "F A L C O N   /   LIVE CONNECTION", font=title_font, fill="#9A7A55")
    identity = f"{bot_name} · ID {bot_id}" if bot_id else bot_name
    identity = normalize_card_text(identity, small_font)
    while identity and draw.textlength(identity, font=small_font) > 900:
        identity = identity[:-2].rstrip() + "…"
    draw.text((104, 122), identity, font=small_font, fill="#806D70")
    draw.text((104, 168), f"{gateway_ms} ms", font=hero_font, fill="#8F3150")
    draw.text((110, 294), "GATEWAY LATENCY", font=label_font, fill="#806D70")
    draw.text((110, 342), "Heartbeat round-trip", font=small_font, fill="#A88D81")
    draw.rounded_rectangle((104, 390, 1292, 393), radius=2, fill="#F0DDE0")

    def metric(x, y, label, value, width=350, height=104):
        draw.rounded_rectangle(
            (x, y, x + width, y + height),
            radius=20,
            fill="#FFF8F6",
            outline="#F0DDE0",
            width=2,
        )
        label_text = label.upper()
        fitted_label_font = label_font
        font_size = 27
        while (
            draw.textlength(label_text, font=fitted_label_font) > width - 44
            and font_size > 25
        ):
            font_size -= 1
            try:
                fitted_label_font = ImageFont.truetype(
                    "DejaVuSans-Bold.ttf", font_size
                )
            except OSError:
                break
        draw.text(
            (x + 22, y + 2),
            label_text,
            font=fitted_label_font,
            fill="#A27972",
        )
        value_text = str(value)
        fitted_value_font = value_font
        value_size = 33
        while (
            draw.textlength(value_text, font=fitted_value_font) > width - 44
            and value_size > 29
        ):
            value_size -= 1
            try:
                fitted_value_font = ImageFont.truetype(
                    "DejaVuSans-Bold.ttf", value_size
                )
            except OSError:
                break
        draw.text(
            (x + 22, y + 49),
            value_text,
            font=fitted_value_font,
            fill="#422C34",
        )

    shard_label = (
        f"{shard_id + 1} / {shard_count}" if shard_id is not None else f"{shard_count} total"
    )
    metric(104, 400, "API", f"{api_ms:.0f} ms")
    metric(480, 400, "Database", f"{database_ms:.1f} ms" if database_ms is not None else "N/A")
    metric(856, 400, "Shard", shard_label)
    metric(104, 508, "Servers", f"{guild_count:,}")
    metric(480, 508, "Members", f"{member_count:,}")
    metric(856, 508, "Commands", f"{command_count:,}")
    metric(104, 616, "Uptime", uptime)
    metric(480, 616, "Memory", f"{memory_mb:.1f} MB" if memory_mb is not None else "N/A")
    metric(856, 616, "Python", python_version)
    metric(104, 724, "Status", "ONLINE · ALL SYSTEMS OPERATIONAL", width=1188)

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


FUN_ACTIONS = {
    "slap": "gave a playful slap to",
    "hug": "hugged",
    "kiss": "sent a kiss to",
    "pat": "gave a head pat to",
    "punch": "challenged",
    "superpunch": "landed a cartoon super-punch on",
    "kick": "gave a playful kick to",
    "kill": "dramatically defeated",
    "yeet": "cartoonishly yeeted",
    "beat": "gave a playful bonk to",
    "highfive": "high-fived",
    "wave": "waved to",
    "dance": "danced with",
    "love": "sent love to",
    "cuddle": "cuddled",
    "bite": "playfully bit",
    "bonk": "bonked",
    "poke": "poked",
    "tickle": "tickled",
    "comfort": "comforted",
    "cheer": "cheered for",
    "roast": "gave a lighthearted roast to",
    "scare": "playfully scared",
    "cry": "cried with",
    "laugh": "laughed with",
    "smile": "shared a smile with",
    "blush": "blushed at",
    "facepalm": "facepalmed at",
    "shrug": "shrugged with",
    "think": "pondered with",
    "wink": "winked at",
    "dab": "did a dab with",
    "handhold": "held hands with",
    "noseboop": "booped on the nose",
    "sleep": "took a nap beside",
    "pillowfight": "had a pillow fight with",
    "marry": "pretended to marry",
    "divorce": "dramatically divorced",
    "propose": "made a playful proposal to",
    "cheekkiss": "gave a cheek kiss to",
    "foreheadkiss": "gave a forehead kiss to",
    "handkiss": "gave a hand kiss to",
    "carry": "gave a playful carry to",
    "bridalcarry": "swept up in a bridal carry",
    "clap": "clapped for",
    "salute": "saluted",
    "bow": "bowed to",
    "thumbsup": "gave a thumbs-up to",
    "celebrate": "celebrated with",
    "party": "partied with",
    "congratulate": "congratulated",
    "thank": "thanked",
    "welcome": "welcomed",
    "forgive": "forgave",
    "miss": "said they missed",
    "admire": "admired",
    "stare": "stared at",
    "glare": "playfully glared at",
    "pout": "pouted at",
    "surprised": "surprised",
    "confused": "confused",
    "angry": "got playfully angry at",
    "excited": "got excited with",
    "nervous": "got nervous around",
    "yawn": "yawned with",
    "sneeze": "sneezed near",
    "run": "ran with",
    "hide": "hid from",
    "chase": "playfully chased",
    "peek": "peeked at",
    "sing": "sang with",
    "eat": "shared a snack with",
    "firstnight": "shared a sweet newlywed moment with",
    "weddingnight": "shared a romantic wedding-night moment with",
    "date": "went on a cute date with",
    "slowdance": "shared a slow dance with",
    "flirt": "flirted with",
    "confess": "confessed their feelings to",
    "serenade": "serenaded",
    "backhug": "gave a warm back hug to",
    "fistbump": "shared a fist bump with",
    "handshake": "shook hands with",
    "cheerup": "tried to cheer up",
    "support": "showed support for",
    "motivate": "motivated",
    "encourage": "encouraged",
    "apologize": "apologized to",
    "sorry": "said sorry to",
    "gift": "gave a gift to",
    "share": "shared something with",
    "feed": "shared a bite with",
    "cook": "cooked a meal with",
    "study": "studied with",
    "read": "read a book with",
    "watch": "watched a show with",
    "travel": "went on an adventure with",
    "teamup": "teamed up with",
    "victory": "celebrated a victory with",
    "protect": "playfully protected",
    "rescue": "came to the rescue of",
    "snowball": "had a snowball fight with",
    "splash": "had a playful splash fight with",
}
FUN_GIF_SEARCH_FALLBACKS = {
    "superpunch": "punch", "kill": "punch", "beat": "bonk", "comfort": "hug",
    "cheer": "happy", "roast": "baka", "scare": "shocked", "noseboop": "poke",
    "dab": "happy", "bow": "salute", "celebrate": "happy", "party": "happy",
    "pillowfight": "lappillow", "marry": "happy", "divorce": "cry",
    "propose": "blowkiss", "cheekkiss": "blowkiss", "foreheadkiss": "kiss",
    "handkiss": "kiss", "love": "blowkiss", "bridalcarry": "carry", "congratulate": "happy",
    "thank": "happy", "welcome": "wave", "forgive": "hug", "miss": "cry",
    "admire": "smug", "glare": "angry", "surprised": "shocked", "excited": "happy",
    "nervous": "shocked", "sneeze": "yawn", "hide": "lurk", "chase": "run",
    "peek": "lurk", "sing": "happy", "eat": "nom", "firstnight": "cuddle",
    "weddingnight": "cuddle", "date": "dance", "slowdance": "dance",
    "flirt": "blowkiss", "confess": "blowkiss", "serenade": "happy",
    "backhug": "hug", "fistbump": "highfive", "cheerup": "hug", "support": "hug",
    "motivate": "happy", "encourage": "happy", "apologize": "salute", "sorry": "salute",
    "gift": "happy", "share": "feed", "cook": "feed", "study": "think",
    "read": "think", "watch": "happy", "travel": "run", "teamup": "highfive",
    "victory": "happy", "protect": "hug", "rescue": "hug", "snowball": "yeet",
    "splash": "dance",
}


def render_variables(value, member, guild, channel=None, bot_user=None, extra_values=None):
    owner = guild.get_member(guild.owner_id)
    joined_at = getattr(member, "joined_at", None)
    created_at = getattr(member, "created_at", None)
    channel_name = getattr(channel, "name", "")
    channel_id = str(getattr(channel, "id", ""))
    user_id = str(member.id)
    username = member.display_name
    avatar_url = member.display_avatar.url
    server_id = str(guild.id)
    member_count = str(guild.member_count or 0)
    owner_mention = owner.mention if owner else f"<@{guild.owner_id}>"
    bot_name = bot_user.name if bot_user else "Falcon"
    bot_id = str(getattr(bot_user, "id", ""))
    values = {
        "user": str(member),
        "username": username,
        "userid": user_id,
        "mention": member.mention,
        "server": guild.name,
        "guild": guild.name,
        "serverid": server_id,
        "guildid": server_id,
        "membercount": member_count,
        "member_count": member_count,
        "channel": channel_name,
        "channelid": channel_id,
        "owner": owner_mention,
        "ownerid": str(guild.owner_id),
        "created": f"<t:{int(created_at.timestamp())}:F>" if created_at else "Unknown",
        "joined": f"<t:{int(joined_at.timestamp())}:F>" if joined_at else "Unknown",
        "avatar": avatar_url,
        "boostcount": str(getattr(guild, "premium_subscription_count", 0) or 0),
        "boost_count": str(getattr(guild, "premium_subscription_count", 0) or 0),
        "boostlevel": str(getattr(guild, "premium_tier", 0) or 0),
        "boost_level": str(getattr(guild, "premium_tier", 0) or 0),
        "user_name": username,
        "user_id": user_id,
        "user_mention": member.mention,
        "user_avatar": avatar_url,
        "user_created": f"<t:{int(created_at.timestamp())}:F>" if created_at else "Unknown",
        "server_name": guild.name,
        "server_id": server_id,
        "server_icon": guild.icon.url if getattr(guild, "icon", None) else "",
        "server_owner": owner_mention,
        "channel_name": channel_name,
        "channel_id": channel_id,
        "message": "",
        "message_content": "",
        "date": utc_now().strftime("%Y-%m-%d"),
        "time": utc_now().strftime("%H:%M UTC"),
        "timestamp": str(int(utc_now().timestamp())),
        "newline": "\n",
        "user.name": getattr(member, "name", username),
        "user.display_name": username,
        "user.id": user_id,
        "user.mention": member.mention,
        "user.avatar": avatar_url,
        "user.avatar_url": avatar_url,
        "user.created_at": f"<t:{int(created_at.timestamp())}:F>" if created_at else "Unknown",
        "user.joined_at": f"<t:{int(joined_at.timestamp())}:F>" if joined_at else "Unknown",
        "server.name": guild.name,
        "server.id": server_id,
        "server.member_count": member_count,
        "server.owner": owner_mention,
        "server.owner_id": str(guild.owner_id),
        "server.boost_count": str(getattr(guild, "premium_subscription_count", 0) or 0),
        "server.boost_level": str(getattr(guild, "premium_tier", 0) or 0),
        "guild.name": guild.name,
        "guild.id": server_id,
        "guild.member_count": member_count,
        "channel.name": channel_name,
        "channel.id": channel_id,
        "channel.mention": getattr(channel, "mention", ""),
        "bot": bot_name,
        "bot.name": bot_name,
        "bot.id": bot_id,
    }
    if extra_values:
        values.update({key.lower(): str(value) for key, value in extra_values.items()})
    return VARIABLE_PATTERN.sub(lambda match: values.get(match.group(1).lower(), match.group(0)), value)


def _render_config_value(value, member, guild, channel, bot_user, extra_values=None):
    if isinstance(value, str):
        return render_variables(value, member, guild, channel, bot_user, extra_values)
    if isinstance(value, list):
        return [_render_config_value(item, member, guild, channel, bot_user, extra_values) for item in value]
    if isinstance(value, dict):
        return {key: _render_config_value(item, member, guild, channel, bot_user, extra_values) for key, item in value.items()}
    return value


def build_custom_embed(config, member, guild, channel, bot_user, extra_values=None):
    if not isinstance(config, dict):
        raise ValueError("Embed configuration must be a JSON object.")
    rendered = _render_config_value(config, member, guild, channel, bot_user, extra_values)
    color = rendered.get("color", "#FFDD00")
    if isinstance(color, str):
        color_names = {
            "red": "FF0000",
            "blue": "0000FF",
            "green": "00FF00",
            "yellow": "FFFF00",
            "gold": "FFDD00",
            "purple": "800080",
        }
        color_text = color.strip().casefold()
        color = int(color_names.get(color_text, color_text.lstrip("#")), 16)
    if not isinstance(color, int) or not 0 <= color <= 0xFFFFFF:
        raise ValueError("Color must be an RGB hex string such as #FFDD00 or an integer.")

    embed = discord.Embed(
        title=rendered.get("title") or None,
        description=rendered.get("description") or None,
        url=rendered.get("url") or None,
        color=color,
    )
    if rendered.get("timestamp"):
        timestamp_value = rendered["timestamp"]
        if timestamp_value is True:
            embed.timestamp = utc_now()
        else:
            try:
                embed.timestamp = datetime.datetime.fromisoformat(timestamp_value)
            except (TypeError, ValueError) as error:
                raise ValueError("Timestamp must be true or an ISO-8601 datetime.") from error

    author = rendered.get("author") or {}
    if author.get("name"):
        embed.set_author(
            name=author["name"],
            url=author.get("url") or None,
            icon_url=author.get("icon_url") or None,
        )
    footer = rendered.get("footer") or {}
    if footer.get("text"):
        embed.set_footer(text=footer["text"], icon_url=footer.get("icon_url") or None)
    if rendered.get("thumbnail"):
        _validate_embed_url(rendered["thumbnail"], "Thumbnail")
        embed.set_thumbnail(url=rendered["thumbnail"])
    if rendered.get("image"):
        _validate_embed_url(rendered["image"], "Image")
        embed.set_image(url=rendered["image"])

    fields = rendered.get("fields") or []
    if not isinstance(fields, list) or len(fields) > 25:
        raise ValueError("Fields must be a list with no more than 25 entries.")
    for field in fields:
        if not isinstance(field, dict) or not field.get("name") or "value" not in field:
            raise ValueError("Each field needs a non-empty name and a value.")
        if len(str(field["name"])) > 256 or len(str(field["value"])) > 1024:
            raise ValueError("Field names are limited to 256 characters and values to 1024 characters.")
        embed.add_field(name=str(field["name"]), value=str(field["value"]), inline=bool(field.get("inline", False)))
    buttons = rendered.get("buttons") or []
    if not isinstance(buttons, list) or len(buttons) > 25:
        raise ValueError("Buttons must be a list with no more than 25 entries.")
    for button in buttons:
        if not isinstance(button, dict) or not button.get("label") or not button.get("url"):
            raise ValueError("Each button needs a non-empty label and URL.")
        if len(str(button["label"])) > 80 or len(str(button["url"])) > 512:
            raise ValueError("Button labels are limited to 80 characters and URLs to 512 characters.")
        _validate_embed_url(button["url"], "Button")
    if embed.title and len(embed.title) > 256:
        raise ValueError("Embed titles are limited to 256 characters.")
    if embed.description and len(embed.description) > 4096:
        raise ValueError("Embed descriptions are limited to 4096 characters.")
    if embed.footer.text and len(embed.footer.text) > 2048:
        raise ValueError("Embed footers are limited to 2048 characters.")
    if embed.author.name and len(embed.author.name) > 256:
        raise ValueError("Embed author names are limited to 256 characters.")
    if len(embed) > 6000:
        raise ValueError("The total embed text cannot exceed 6000 characters.")
    return embed


def _validate_embed_url(value, label):
    parsed = urlsplit(str(value))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{label} URL must start with http:// or https://.")


def validate_embed_config(config_text):
    try:
        config = json.loads(config_text)
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid JSON at line {error.lineno}, column {error.colno}.") from error
    if not isinstance(config, dict):
        raise ValueError("Embed configuration must be a JSON object.")
    build_custom_embed(config, _SampleMember(), _SampleGuild(), None, None)
    return config


def build_custom_embed_view(config, member, guild, channel, bot_user, extra_values=None):
    rendered = _render_config_value(config, member, guild, channel, bot_user, extra_values)
    buttons = rendered.get("buttons", [])
    if not buttons:
        return None
    view = discord.ui.View(timeout=None)
    for index, button in enumerate(buttons):
        view.add_item(discord.ui.Button(
            label=button["label"],
            url=button["url"],
            style=discord.ButtonStyle.link,
            row=index // 5,
        ))
    return view


def build_embed_creation_intro(name):
    return discord.Embed(
        description=(
            f"<:start:1561337010806071387> **successfully created an embed called: `{name}`**\n"
            "please select from the buttons below for what you'd like to edit!\n"
            "alternatively, you can edit these individually in slash commands with `/embed edit`."
        ),
        color=0xFFDD00,
    )


def embed_creation_announcement(name):
    return (
        f"created an embed with name `{name}`.\n"
        f"- you can now refer to the embed with `{{embed:{name}}}` in your autoresponder or greet/leave/boost message\n"
        "- for example, to attach to a welcome message, use "
        "**/setup greet message** "
        f"`message: welcome {{user}}` `{{embed:{name}}}`"
    )


class _SampleAsset:
    url = "https://example.com/avatar.png"


class _SampleMember:
    id = 123456789012345678
    name = "Sample"
    display_name = "Sample"
    mention = "<@123456789012345678>"
    created_at = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
    joined_at = created_at
    display_avatar = _SampleAsset()

    def __str__(self):
        return self.name


class _SampleGuild:
    id = 123456789012345678
    name = "Sample Guild"
    member_count = 1
    owner_id = 123456789012345678

    @staticmethod
    def get_member(_user_id):
        return _SampleMember()


class EmbedConfigModal(discord.ui.Modal):
    def __init__(self, cog, guild_id, name, config=None):
        super().__init__(title=f"Configure embed: {name[:35]}", timeout=300)
        self.cog = cog
        self.guild_id = guild_id
        self.embed_name = name
        initial = json.dumps(config or DEFAULT_EMBED_CONFIG, indent=2)
        self.configuration = discord.ui.TextInput(
            label="Embed configuration (JSON)",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=4000,
            default=initial[:4000],
        )
        self.add_item(self.configuration)

    async def on_submit(self, interaction):
        try:
            config = validate_embed_config(self.configuration.value)
        except ValueError as error:
            return await send_interaction_v2(interaction, str(error), ephemeral=True)
        await self.cog.save_embed(self.guild_id, self.embed_name, config, interaction.user.id)
        await send_interaction_v2(interaction, f"Embed `{self.embed_name}` saved.", ephemeral=True)


class EmbedFormModal(discord.ui.Modal):
    def __init__(self, title, fields, callback):
        super().__init__(title=title, timeout=300)
        self.callback = callback
        self.inputs = {}
        for field in fields:
            value_input = discord.ui.TextInput(
                label=field["label"],
                style=field.get("style", discord.TextStyle.short),
                required=field.get("required", False),
                max_length=field.get("max_length", 4000),
                default=str(field.get("value", ""))[:field.get("max_length", 4000)],
                placeholder=field.get("placeholder", "Enter value"),
            )
            self.inputs[field["key"]] = value_input
            self.add_item(value_input)

    async def on_submit(self, interaction):
        await self.callback(
            interaction,
            {key: value_input.value.strip() for key, value_input in self.inputs.items()},
        )


class EmbedSupportButton(discord.ui.Button):
    def __init__(self):
        support_url = os.getenv("SUPPORT_SERVER_URL", os.getenv("SUPPORT_SERVER", "")).strip()
        parsed_url = urlsplit(support_url)
        self.support_url = (
            support_url
            if parsed_url.scheme in {"http", "https"} and parsed_url.netloc
            else ""
        )
        if self.support_url:
            super().__init__(
                label="Need help? Join the server",
                style=discord.ButtonStyle.link,
                url=self.support_url,
                row=1,
            )
        else:
            super().__init__(
                label="Need help? Join the server",
                style=discord.ButtonStyle.secondary,
                row=1,
            )

    async def callback(self, interaction):
        await send_interaction_v2(
            interaction,
            "The support server invite has not been configured yet.",
            ephemeral=True,
            allow_ephemeral=True,
        )


class AdvancedEmbedBuilderView(discord.ui.View):
    def __init__(
        self,
        cog,
        guild_id,
        name,
        config=None,
        owner_id=None,
        save_callback=None,
        extra_values=None,
    ):
        super().__init__(timeout=600)
        self.cog = cog
        self.guild_id = guild_id
        self.name = name
        self.owner_id = owner_id
        self.save_callback = save_callback
        self.extra_values = extra_values or {}
        self.message = None
        self.config = dict(json.loads(json.dumps(config or DEFAULT_EMBED_CONFIG)))
        if not isinstance(self.config.get("fields"), list):
            self.config["fields"] = []
        self.custom_button_items = []
        self.add_item(EmbedSupportButton())
        self._sync_custom_buttons()

    def _sync_custom_buttons(self):
        for item in self.custom_button_items:
            self.remove_item(item)
        self.custom_button_items.clear()
        for index, button in enumerate((self.config.get("buttons") or [])[:18]):
            row = 1 if index < 3 else 2 + (index - 3) // 5
            item = discord.ui.Button(
                label=button["label"],
                url=button["url"],
                style=discord.ButtonStyle.link,
                row=row,
            )
            self.add_item(item)
            self.custom_button_items.append(item)

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await send_interaction_v2(interaction, "This embed builder is not yours.", ephemeral=True, allow_ephemeral=True)
        return False

    async def _refresh_preview(self, interaction):
        try:
            embed = build_custom_embed(
                self.config,
                interaction.user,
                interaction.guild,
                interaction.channel,
                self.cog.bot.user,
                self.extra_values,
            )
            if self.save_callback is not None:
                await self.save_callback(self.config)
            else:
                await self.cog.save_embed(
                    self.guild_id,
                    self.name,
                    self.config,
                    interaction.user.id,
                )
        except ValueError as error:
            await send_interaction_v2(
                interaction,
                f"Preview error: {error}",
                ephemeral=True,
                allow_ephemeral=True,
            )
            return

        if interaction.message is not None:
            await edit_interaction_v2(interaction, embed=embed, view=self)
            return

        if self.message is None:
            return await send_interaction_v2(
                interaction,
                "The embed editor expired. Reopen it to continue.",
                ephemeral=True,
                allow_ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True)
        await edit_message_v2(self.message, embed=embed, view=self)
        await interaction.delete_original_response()

    @discord.ui.button(label="Edit Basic Information", style=discord.ButtonStyle.secondary, row=0)
    async def set_basic_information(self, interaction, button):
        async def update(interaction, values):
            self.config.update({
                "title": values["title"],
                "description": values["description"],
                "color": values["color"],
            })
            await self._refresh_preview(interaction)

        await interaction.response.send_modal(EmbedFormModal(
            "Edit Basic Information",
            [
                {"key": "title", "label": "Title", "value": self.config.get("title", ""), "max_length": 256},
                {"key": "description", "label": "Description", "value": self.config.get("description", ""), "style": discord.TextStyle.paragraph, "max_length": 4000},
                {"key": "color", "label": "Color", "value": self.config.get("color", "#FFDD00"), "max_length": 16, "placeholder": "Hex color, e.g. #FFDD00"},
            ],
            update,
        ))

    @discord.ui.button(label="Edit Author", style=discord.ButtonStyle.secondary, row=0)
    async def set_author(self, interaction, button):
        async def update(interaction, values):
            author = dict(self.config.get("author") or {})
            author.update({"name": values["name"], "icon_url": values["icon_url"]})
            self.config["author"] = author
            await self._refresh_preview(interaction)

        author = self.config.get("author") or {}
        await interaction.response.send_modal(EmbedFormModal(
            "Edit Author",
            [
                {"key": "name", "label": "Author Text", "value": author.get("name", ""), "max_length": 256},
                {"key": "icon_url", "label": "Author Image (Optional)", "value": author.get("icon_url", ""), "max_length": 512, "placeholder": "https://example.com/image.png"},
            ],
            update,
        ))

    @discord.ui.button(label="Edit Footer", style=discord.ButtonStyle.secondary, row=0)
    async def set_footer(self, interaction, button):
        async def update(interaction, values):
            timestamp = values["timestamp"].casefold()
            if timestamp not in {"yes", "no"}:
                return await send_interaction_v2(
                    interaction,
                    "Timestamp must be `yes` or `no`.",
                    ephemeral=True,
                    allow_ephemeral=True,
                )
            footer = dict(self.config.get("footer") or {})
            footer.update({"text": values["text"], "icon_url": values["icon_url"]})
            self.config["footer"] = footer
            self.config["timestamp"] = timestamp == "yes"
            await self._refresh_preview(interaction)

        footer = self.config.get("footer") or {}
        await interaction.response.send_modal(EmbedFormModal(
            "Edit Footer",
            [
                {"key": "text", "label": "Footer Text", "value": footer.get("text", ""), "max_length": 2048},
                {"key": "icon_url", "label": "Footer Image (Optional)", "value": footer.get("icon_url", ""), "max_length": 512, "placeholder": "https://example.com/image.png"},
                {"key": "timestamp", "label": "Timestamp (Yes/No)", "value": "yes" if self.config.get("timestamp") else "no", "max_length": 3, "placeholder": "yes or no"},
            ],
            update,
        ))

    @discord.ui.button(label="Edit Images", style=discord.ButtonStyle.secondary, row=0)
    async def set_images(self, interaction, button):
        async def update(interaction, values):
            self.config["image"] = values["image"]
            self.config["thumbnail"] = values["thumbnail"]
            await self._refresh_preview(interaction)

        await interaction.response.send_modal(EmbedFormModal(
            "Edit Images",
            [
                {"key": "image", "label": "Main Image", "value": self.config.get("image", ""), "max_length": 512, "placeholder": "https://example.com/image.png"},
                {"key": "thumbnail", "label": "Thumbnail", "value": self.config.get("thumbnail", ""), "max_length": 512, "placeholder": "https://example.com/thumbnail.png"},
            ],
            update,
        ))

    @discord.ui.button(label="Create Button", style=discord.ButtonStyle.secondary, row=0)
    async def create_button(self, interaction, button):
        async def update(interaction, values):
            buttons = list(self.config.get("buttons") or [])
            if len(buttons) >= 18:
                return await send_interaction_v2(
                    interaction,
                    "This editor has seven controls, so it can show up to 18 custom buttons alongside them.",
                    ephemeral=True,
                    allow_ephemeral=True,
                )
            label = values["label"].strip()
            if any(
                str(existing.get("label", "")).casefold() == label.casefold()
                for existing in buttons
            ):
                return await send_interaction_v2(
                    interaction,
                    f"A button named `{label}` already exists. Choose a unique name.",
                    ephemeral=True,
                    allow_ephemeral=True,
                )
            candidate = dict(self.config)
            candidate["buttons"] = buttons + [{"label": label, "url": values["url"]}]
            try:
                build_custom_embed(
                    candidate,
                    interaction.user,
                    interaction.guild,
                    interaction.channel,
                    self.cog.bot.user,
                    self.extra_values,
                )
            except ValueError as error:
                return await send_interaction_v2(
                    interaction,
                    str(error),
                    ephemeral=True,
                    allow_ephemeral=True,
                )
            self.config["buttons"] = candidate["buttons"]
            self._sync_custom_buttons()
            await self._refresh_preview(interaction)

        await interaction.response.send_modal(EmbedFormModal(
            "Create Link Button",
            [
                {"key": "label", "label": "Button Name", "max_length": 80},
                {"key": "url", "label": "Button Link", "max_length": 512, "placeholder": "https://example.com"},
            ],
            update,
        ))

    @discord.ui.button(label="Remove Button", style=discord.ButtonStyle.secondary, row=1)
    async def remove_button(self, interaction, button):
        async def update(interaction, values):
            name = values["label"].strip()
            buttons = list(self.config.get("buttons") or [])
            index = next(
                (
                    index
                    for index, existing in enumerate(buttons)
                    if str(existing.get("label", "")).casefold() == name.casefold()
                ),
                None,
            )
            if index is None:
                return await send_interaction_v2(
                    interaction,
                    f"No button named `{name}` was found.",
                    ephemeral=True,
                    allow_ephemeral=True,
                )
            buttons.pop(index)
            self.config["buttons"] = buttons
            self._sync_custom_buttons()
            await self._refresh_preview(interaction)

        await interaction.response.send_modal(EmbedFormModal(
            "Remove Link Button",
            [{"key": "label", "label": "Button Name", "max_length": 80}],
            update,
        ))

class GreetingConfigModal(discord.ui.Modal):
    def __init__(self, cog, guild_id, greeting_type):
        super().__init__(title=f"Configure {greeting_type} greeting", timeout=300)
        self.cog = cog
        self.guild_id = guild_id
        self.greeting_type = greeting_type
        initial = json.dumps(DEFAULT_EMBED_CONFIG, indent=2)
        self.configuration = discord.ui.TextInput(
            label="Greeting embed configuration (JSON)",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=4000,
            default=initial[:4000],
        )
        self.add_item(self.configuration)

    async def on_submit(self, interaction):
        try:
            config = validate_embed_config(self.configuration.value)
        except ValueError as error:
            return await send_interaction_v2(interaction, str(error), ephemeral=True)
        await self.cog.save_greeting(self.guild_id, self.greeting_type, config, interaction.user.id)
        await send_interaction_v2(interaction, f"{self.greeting_type.title()} greeting saved.", ephemeral=True)


class GreetingConfigView(discord.ui.View):
    def __init__(self, cog, guild_id, owner_id):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = guild_id
        self.owner_id = owner_id

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await send_interaction_v2(interaction, "Only the administrator who opened this setup can use it.", ephemeral=True)
        return False

    @discord.ui.button(label="Edit Join Embed", style=discord.ButtonStyle.secondary)
    async def edit_join(self, interaction, _button):
        await self.cog.open_greeting_embed_editor(interaction, "join")

    @discord.ui.button(label="Edit Leave Embed", style=discord.ButtonStyle.secondary)
    async def edit_leave(self, interaction, _button):
        await self.cog.open_greeting_embed_editor(interaction, "leave")


class GreetingCardSetupView(discord.ui.View):
    def __init__(self, cog, guild_id, owner_id):
        super().__init__(timeout=600)
        self.cog = cog
        self.guild_id = guild_id
        self.owner_id = owner_id

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await send_interaction_v2(
            interaction,
            "Only the administrator who enabled greeting cards can change these channels.",
            ephemeral=True,
        )
        return False

    async def make_embed(self, guild):
        async with self.cog.bot.db.execute(
            "SELECT channel_id, join_card_channel_id, leave_card_channel_id "
            "FROM greeting_settings WHERE guild_id=?",
            (self.guild_id,),
        ) as cursor:
            row = await cursor.fetchone()
        shared_id, join_id, leave_id = row if row else (None, None, None)

        def channel_label(channel_id, fallback_id=None):
            channel = guild.get_channel(channel_id) if channel_id else None
            if channel:
                return channel.mention
            if fallback_id:
                fallback_channel = guild.get_channel(fallback_id)
                if fallback_channel:
                    return f"Use shared channel ({fallback_channel.mention})"
            return "Select a channel below"

        embed = discord.Embed(
            title="Greeting Card Channels",
            description=(
                "Choose the shared channel, then optionally set separate join and leave channels. "
                "Changes save as soon as you select a channel."
            ),
            color=0xFFDD00,
        )
        embed.add_field(
            name="Shared/default",
            value=channel_label(shared_id),
            inline=False,
        )
        embed.add_field(
            name="Join card",
            value=channel_label(join_id, shared_id),
            inline=False,
        )
        embed.add_field(
            name="Leave card",
            value=channel_label(leave_id, shared_id),
            inline=False,
        )
        return embed

    async def _save_channel(self, interaction, column, channel):
        channel_id = channel.id if channel else None
        if column == "channel_id":
            channel_id = channel_id or 0
        await self.cog.bot.db.execute(
            f"UPDATE greeting_settings SET {column}=? WHERE guild_id=?",
            (channel_id, self.guild_id),
        )
        await self.cog.bot.db.commit()
        await edit_interaction_v2(
            interaction,
            embed=await self.make_embed(interaction.guild),
            view=self,
        )

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text, discord.ChannelType.news],
        placeholder="Shared/default greeting-card channel",
        min_values=0,
        max_values=1,
        row=0,
    )
    async def set_shared_channel(self, interaction, select):
        await self._save_channel(
            interaction,
            "channel_id",
            select.values[0] if select.values else None,
        )

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text, discord.ChannelType.news],
        placeholder="Join-card channel (optional override)",
        min_values=0,
        max_values=1,
        row=1,
    )
    async def set_join_channel(self, interaction, select):
        await self._save_channel(
            interaction,
            "join_card_channel_id",
            select.values[0] if select.values else None,
        )

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text, discord.ChannelType.news],
        placeholder="Leave-card channel (optional override)",
        min_values=0,
        max_values=1,
        row=2,
    )
    async def set_leave_channel(self, interaction, select):
        await self._save_channel(
            interaction,
            "leave_card_channel_id",
            select.values[0] if select.values else None,
        )


class BoostCardSetupView(discord.ui.View):
    def __init__(self, cog, guild_id, owner_id):
        super().__init__(timeout=600)
        self.cog = cog
        self.guild_id = guild_id
        self.owner_id = owner_id

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await send_interaction_v2(
            interaction,
            "Only the administrator who enabled booster cards can change this channel.",
            ephemeral=True,
        )
        return False

    async def make_embed(self, guild):
        async with self.cog.bot.db.execute(
            "SELECT card_channel_id, channel_id FROM boost_settings WHERE guild_id=?",
            (self.guild_id,),
        ) as cursor:
            row = await cursor.fetchone()
        channel_id = (row[0] or row[1]) if row else None
        channel = guild.get_channel(channel_id) if channel_id else None
        embed = discord.Embed(
            title="Booster Card Channel",
            description=(
                "Select where the booster mention and card should be sent. "
                "This channel is separate from the regular boost-message channel."
            ),
            color=0xFFDD00,
        )
        embed.add_field(
            name="Current channel",
            value=channel.mention if channel else "Select a channel below",
            inline=False,
        )
        return embed

    async def _save_channel(self, interaction, channel):
        await self.cog.bot.db.execute(
            "UPDATE boost_settings SET card_channel_id=?, card_enabled=1, "
            "updated_by=?, updated_at=? WHERE guild_id=?",
            (
                channel.id,
                interaction.user.id,
                utc_now().isoformat(),
                self.guild_id,
            ),
        )
        await self.cog.bot.db.commit()
        await edit_interaction_v2(
            interaction,
            embed=await self.make_embed(interaction.guild),
            view=self,
        )

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text, discord.ChannelType.news],
        placeholder="Select the booster-card channel",
        min_values=1,
        max_values=1,
    )
    async def set_channel(self, interaction, select):
        await self._save_channel(interaction, select.values[0])


class TagDurationModal(discord.ui.Modal):
    def __init__(self, cog, setup_view, setup_message):
        super().__init__(title="Set Tag Delete Duration", timeout=300)
        self.cog = cog
        self.setup_view = setup_view
        self.setup_message = setup_message
        self.duration_input = discord.ui.TextInput(
            label="Duration (e.g. 30s, 5min, 1h)",
            placeholder="30s",
            required=True,
            max_length=80,
        )
        self.add_item(self.duration_input)

    async def on_submit(self, interaction):
        try:
            duration = parse_duration(self.duration_input.value)
        except (OverflowError, ValueError) as error:
            return await interaction.response.send_message(str(error), ephemeral=False)
        if duration is None:
            return await interaction.response.send_message(
                "Tag pings need an auto-delete duration; permanent is not supported.",
                ephemeral=False,
            )

        seconds = int(duration.total_seconds())
        await self.cog.bot.db.execute(
            "INSERT INTO tag_settings (guild_id, delete_after_seconds) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET delete_after_seconds=excluded.delete_after_seconds",
            (self.setup_view.guild_id, seconds),
        )
        await self.cog.bot.db.commit()
        await interaction.response.defer(ephemeral=False)
        try:
            embed = await self.cog._build_tag_setup_embed(interaction.guild)
            await edit_message_v2(self.setup_message, embed=embed, view=self.setup_view)
        except discord.HTTPException:
            pass
        await interaction.followup.send("Tag delete duration updated.", ephemeral=False)


class TagSetupView(discord.ui.View):
    def __init__(self, cog, guild_id, owner_id):
        super().__init__(timeout=600)
        self.cog = cog
        self.guild_id = guild_id
        self.owner_id = owner_id

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("Only the admin who opened this setup can change it.", ephemeral=False)
        return False

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text, discord.ChannelType.news],
        placeholder="Add one or more tag channels",
        min_values=1,
        max_values=25,
    )
    async def add_channels(self, interaction, select):
        for channel in select.values:
            await self.cog.bot.db.execute(
                "INSERT OR IGNORE INTO tag_channels (guild_id, channel_id) VALUES (?, ?)",
                (self.guild_id, channel.id),
            )
        await self.cog.bot.db.execute(
            "INSERT OR IGNORE INTO tag_settings (guild_id, delete_after_seconds) VALUES (?, NULL)",
            (self.guild_id,),
        )
        await self.cog.bot.db.commit()
        embed = await self.cog._build_tag_setup_embed(interaction.guild)
        await edit_interaction_v2(interaction, embed=embed, view=self)

    @discord.ui.button(label="Set Delete Duration", style=discord.ButtonStyle.secondary)
    async def set_duration(self, interaction, _button):
        await interaction.response.send_modal(TagDurationModal(self.cog, self, interaction.message))


class DestructiveConfirmationView(discord.ui.View):
    def __init__(self, user_id, action, success_message):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.action = action
        self.success_message = success_message

    async def interaction_check(self, interaction):
        if interaction.user.id == self.user_id:
            return True
        await send_interaction_v2(interaction, "This confirmation is not yours.", ephemeral=True)
        return False

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.secondary)
    async def confirm(self, interaction, _button):
        await self.action()
        await edit_interaction_v2(interaction, content=self.success_message, view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, _button):
        await edit_interaction_v2(interaction, content="Action cancelled.", view=None)
        self.stop()


class GiphyAPIError(Exception):
    def __init__(self, status, message=None):
        super().__init__(message or f"GIPHY returned HTTP {status}.")
        self.status = status


def _giphy_image_url(gif):
    if not isinstance(gif, dict):
        return None
    images = gif.get("images")
    if not isinstance(images, dict):
        return None
    for rendition_name in (
        "original",
        "downsized_medium",
        "fixed_height",
        "fixed_height_small",
    ):
        rendition = images.get(rendition_name)
        if not isinstance(rendition, dict):
            continue
        image_url = rendition.get("url")
        if not isinstance(image_url, str):
            continue
        try:
            parsed_url = urlsplit(image_url)
        except ValueError:
            continue
        if parsed_url.scheme in {"http", "https"} and parsed_url.netloc:
            return image_url
    return None


class GiphyResultsView(discord.ui.View):
    def __init__(self, owner_id, items, title):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.items = items
        self.title = title
        self.index = 0
        self._sync_controls()

    def _sync_controls(self):
        self.previous_page.disabled = self.index <= 0
        self.next_page.disabled = self.index >= len(self.items) - 1

    def _make_embed(self):
        item = self.items[self.index]
        title = item.get("title") or item.get("slug") or self.title
        embed = discord.Embed(title=str(title)[:256], color=0xFFDD00)
        page_url = item.get("url")
        if isinstance(page_url, str):
            try:
                parsed_page_url = urlsplit(page_url)
            except ValueError:
                parsed_page_url = None
            if (
                parsed_page_url
                and parsed_page_url.scheme in {"http", "https"}
                and parsed_page_url.netloc
            ):
                embed.url = page_url
        embed.set_image(url=_giphy_image_url(item))
        embed.set_footer(
            text=f"Powered by GIPHY • Result {self.index + 1}/{len(self.items)}"
        )
        return embed

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "Only the person who requested these GIPHY results can change pages.",
            ephemeral=True,
        )
        return False

    async def _change_page(self, interaction, direction):
        new_index = self.index + direction
        if not 0 <= new_index < len(self.items):
            return await interaction.response.send_message(
                "There are no more results in that direction.",
                ephemeral=True,
            )
        self.index = new_index
        self._sync_controls()
        await edit_interaction_v2(
            interaction,
            embed=self._make_embed(),
            view=self,
        )

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous_page(self, interaction, _button):
        await self._change_page(interaction, -1)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction, _button):
        await self._change_page(interaction, 1)


class ShipAgainView(discord.ui.View):
    def __init__(self, cog, first_member, second_member):
        super().__init__(timeout=300)
        self.cog = cog
        self.first_member = first_member
        self.second_member = second_member

    @discord.ui.button(
        label="Ship Again",
        style=discord.ButtonStyle.secondary,
    )
    async def ship_again(self, interaction, _button):
        clicker = interaction.user
        excluded_ids = {clicker.id, self.second_member.id}
        candidates = await self.cog._ship_random_users(excluded_ids)
        if not candidates:
            return await interaction.response.send_message(
                "I couldn't find another non-bot user to ship you with.",
                ephemeral=True,
            )
        second_member = random.choice(candidates)
        try:
            embed, file = await self.cog._create_ship_result(
                clicker,
                second_member,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
            return await interaction.response.send_message(
                "Could not create the ship card right now. Please try again.",
                ephemeral=True,
            )
        await edit_interaction_v2(
            interaction,
            embed=embed,
            attachments=[file],
            view=self,
        )
        self.first_member = clicker
        self.second_member = second_member


class CommunityFeatures(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._role_alias_commands = {}
        self._fun_gif_cache = {}
        self._fun_gif_indexes = {}
        self._fun_gif_last_urls = {}
        self._fun_gif_retry_after = 0.0
        self._response_cooldowns = {}
        self._cooldown_checks = 0

    @commands.hybrid_group(name="test", invoke_without_command=True, case_insensitive=True)
    async def test_group(self, ctx):
        await ctx.send("Use `/test greet` to preview the configured welcome message.", ephemeral=ctx.interaction is not None)

    @test_group.command(name="greet", description="Send a preview of the configured welcome message")
    @commands.has_guild_permissions(manage_guild=True)
    async def test_greet(self, ctx):
        async with self.bot.db.execute(
            "SELECT join_channel_id, join_message, join_enabled FROM guild_settings WHERE guild_id=?",
            (ctx.guild.id,),
        ) as cursor:
            settings = await cursor.fetchone()
        if not settings or not settings[1]:
            return await ctx.send("No welcome message is configured. Use `/setup greet message` first.", ephemeral=ctx.interaction is not None)
        if not settings[2]:
            return await ctx.send("Welcome messages are disabled for this server.", ephemeral=ctx.interaction is not None)
        channel = ctx.guild.get_channel(settings[0]) if settings[0] else None
        channel = channel or ctx.channel
        sent = await self._send_automated_response(
            getattr(ctx, "message", None),
            settings[1],
            allow_delete=False,
            channel=channel,
            user=ctx.author,
            guild=ctx.guild,
            extra_values={"message": "Welcome preview", "message.content": "Welcome preview"},
        )
        if sent:
            await ctx.send(f"Welcome preview sent to {channel.mention}.", ephemeral=ctx.interaction is not None)
        else:
            await ctx.send("I could not send the welcome preview. Check my channel permissions.", ephemeral=ctx.interaction is not None)

    async def _confirm_destructive_action(self, ctx, prompt, success_message, action):
        view = DestructiveConfirmationView(ctx.author.id, action, success_message)
        if ctx.interaction is not None:
            await send_interaction_v2(ctx.interaction, prompt, view=view, ephemeral=True)
        else:
            await ctx.send(prompt, view=view)

    async def cog_load(self):
        await self.bot.db.execute(
            "CREATE TABLE IF NOT EXISTS role_mappings ("
            "guild_id INTEGER NOT NULL, alias TEXT NOT NULL, role_id INTEGER NOT NULL, "
            "created_by INTEGER NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(guild_id, alias))"
        )
        await self.bot.db.execute(
            "CREATE TABLE IF NOT EXISTS role_alias_settings ("
            "guild_id INTEGER PRIMARY KEY, required_role_id INTEGER NOT NULL, "
            "updated_by INTEGER NOT NULL, updated_at TEXT NOT NULL)"
        )
        await self.bot.db.execute(
            "CREATE TABLE IF NOT EXISTS custom_embeds ("
            "guild_id INTEGER NOT NULL, name TEXT NOT NULL, config TEXT NOT NULL, "
            "created_by INTEGER NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(guild_id, name))"
        )
        await self.bot.db.execute(
            "CREATE TABLE IF NOT EXISTS greeting_settings ("
            "guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, join_config TEXT, "
            "leave_config TEXT, join_enabled INTEGER NOT NULL DEFAULT 1, leave_enabled INTEGER NOT NULL DEFAULT 1, "
            "join_card_channel_id INTEGER, leave_card_channel_id INTEGER, "
            "updated_by INTEGER NOT NULL, updated_at TEXT NOT NULL)"
        )
        await self.bot.db.execute(
            "CREATE TABLE IF NOT EXISTS greeting_delete_settings ("
            "guild_id INTEGER PRIMARY KEY, join_seconds INTEGER, leave_seconds INTEGER)"
        )
        await self.bot.db.execute(
            "CREATE TABLE IF NOT EXISTS community_event_replies ("
            "guild_id INTEGER NOT NULL, event TEXT NOT NULL, response TEXT, channel_id INTEGER, "
            "updated_by INTEGER NOT NULL, updated_at TEXT NOT NULL, "
            "PRIMARY KEY(guild_id, event))"
        )
        async with self.bot.db.execute("PRAGMA table_info(greeting_settings)") as cursor:
            greeting_columns = {row[1] for row in await cursor.fetchall()}
        for column, default in (
            ("join_enabled", 1),
            ("leave_enabled", 1),
            ("join_card_enabled", 0),
            ("leave_card_enabled", 0),
        ):
            if column not in greeting_columns:
                await self.bot.db.execute(
                    f"ALTER TABLE greeting_settings ADD COLUMN {column} INTEGER NOT NULL DEFAULT {default}"
                )
        for column in ("join_card_channel_id", "leave_card_channel_id"):
            if column not in greeting_columns:
                await self.bot.db.execute(
                    f"ALTER TABLE greeting_settings ADD COLUMN {column} INTEGER"
                )
        await self.bot.db.execute(
            "CREATE TABLE IF NOT EXISTS autoresponders ("
            "guild_id INTEGER NOT NULL, trigger TEXT NOT NULL, response TEXT NOT NULL, "
            "updated_by INTEGER NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(guild_id, trigger))"
        )
        async with self.bot.db.execute("PRAGMA table_info(autoresponders)") as cursor:
            autoresponder_columns = {row[1] for row in await cursor.fetchall()}
        for column, definition in (
            ("mode", "TEXT NOT NULL DEFAULT 'contains'"),
            ("channel_id", "INTEGER"),
            ("role_id", "INTEGER"),
            ("cooldown", "INTEGER NOT NULL DEFAULT 0"),
            ("enabled", "INTEGER NOT NULL DEFAULT 1"),
            ("excluded_channel_ids", "TEXT NOT NULL DEFAULT '[]'"),
            ("allowed_role_ids", "TEXT NOT NULL DEFAULT '[]'"),
            ("excluded_role_ids", "TEXT NOT NULL DEFAULT '[]'"),
            ("cooldown_scope", "TEXT NOT NULL DEFAULT 'user'"),
            ("case_sensitive", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in autoresponder_columns:
                await self.bot.db.execute(f"ALTER TABLE autoresponders ADD COLUMN {column} {definition}")
        await self.bot.db.execute(
            "CREATE TABLE IF NOT EXISTS custom_commands ("
            "guild_id INTEGER NOT NULL, name TEXT NOT NULL, response TEXT NOT NULL, "
            "role_id INTEGER, channel_id INTEGER, cooldown INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1, "
            "updated_by INTEGER NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(guild_id, name))"
        )
        async with self.bot.db.execute("PRAGMA table_info(custom_commands)") as cursor:
            custom_command_columns = {row[1] for row in await cursor.fetchall()}
        if "enabled" not in custom_command_columns:
            await self.bot.db.execute(
                "ALTER TABLE custom_commands ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"
            )
        await self.bot.db.execute(
            "CREATE TABLE IF NOT EXISTS boost_settings ("
            "guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, message TEXT NOT NULL, "
            "enabled INTEGER NOT NULL DEFAULT 1, card_enabled INTEGER NOT NULL DEFAULT 0, "
            "card_channel_id INTEGER, updated_by INTEGER NOT NULL, updated_at TEXT NOT NULL)"
        )
        async with self.bot.db.execute("PRAGMA table_info(boost_settings)") as cursor:
            boost_columns = {row[1] for row in await cursor.fetchall()}
        if "enabled" not in boost_columns:
            await self.bot.db.execute(
                "ALTER TABLE boost_settings ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"
            )
        if "embed_name" not in boost_columns:
            await self.bot.db.execute(
                "ALTER TABLE boost_settings ADD COLUMN embed_name TEXT"
            )
        if "card_enabled" not in boost_columns:
            await self.bot.db.execute(
                "ALTER TABLE boost_settings ADD COLUMN card_enabled INTEGER NOT NULL DEFAULT 0"
            )
        if "card_channel_id" not in boost_columns:
            await self.bot.db.execute(
                "ALTER TABLE boost_settings ADD COLUMN card_channel_id INTEGER"
            )
        await self.bot.db.execute(
            "CREATE TABLE IF NOT EXISTS tag_settings ("
            "guild_id INTEGER PRIMARY KEY, delete_after_seconds INTEGER, "
            "enabled INTEGER NOT NULL DEFAULT 1)"
        )
        async with self.bot.db.execute("PRAGMA table_info(tag_settings)") as cursor:
            tag_columns = {row[1] for row in await cursor.fetchall()}
        if "enabled" not in tag_columns:
            await self.bot.db.execute(
                "ALTER TABLE tag_settings ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"
            )
        await self.bot.db.execute(
            "CREATE TABLE IF NOT EXISTS tag_channels ("
            "guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, "
            "PRIMARY KEY(guild_id, channel_id))"
        )
        await self.bot.db.commit()
        await self._register_role_aliases()

    async def _register_role_aliases(self):
        async with self.bot.db.execute("SELECT DISTINCT alias FROM role_mappings") as cursor:
            aliases = [row[0] for row in await cursor.fetchall()]
        for alias in aliases:
            self._add_role_command(alias)

    def _add_role_command(self, alias):
        if self.bot.get_command(alias):
            return

        async def callback(ctx, member: discord.Member = None):
            await self.toggle_member_role(ctx, alias, member)

        command = commands.hybrid_command(name=alias, description=f"Toggle the configured {alias} role")(callback)
        self.bot.add_command(command)
        self._role_alias_commands[alias] = command

    async def _check_role_hierarchy(self, ctx, role, *, check_actor=True):
        guild = ctx.guild
        bot_member = guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            raise PermissionError("I need the Manage Roles permission to change roles.")
        if role.is_default():
            raise PermissionError("The @everyone role cannot be assigned or removed.")
        if role >= bot_member.top_role:
            raise PermissionError("That role must be below my highest role.")
        if check_actor and not ctx.author.guild_permissions.administrator and ctx.author.top_role <= role:
            raise PermissionError("That role must be below your highest role.")

    async def _resolve_member_from_context(self, ctx, member=None, query=None):
        if member is not None:
            return member
        query_text = str(query or "").strip()
        if query_text and ctx.guild is not None:
            from main import UsernameMemberConverter

            try:
                return await UsernameMemberConverter().convert(ctx, query_text)
            except commands.MemberNotFound:
                pass
        if getattr(ctx, "message", None) is not None:
            if ctx.message.mentions:
                return ctx.message.mentions[0]
            ref = ctx.message.reference
            if ref:
                resolved = ref.resolved
                if isinstance(resolved, discord.Member):
                    return resolved
                if isinstance(resolved, discord.Message):
                    if isinstance(resolved.author, discord.Member):
                        return resolved.author
                    for mention in resolved.mentions:
                        if isinstance(mention, discord.Member):
                            return mention
                if ref.message_id:
                    try:
                        replied_message = await ctx.channel.fetch_message(ref.message_id)
                        if isinstance(replied_message.author, discord.Member):
                            return replied_message.author
                    except (discord.Forbidden, discord.HTTPException):
                        pass
        return None

    async def _convert_role_argument(self, ctx, argument):
        from main import UsernameRoleConverter

        return await UsernameRoleConverter().convert(ctx, argument)

    async def toggle_member_role(self, ctx, alias, member):
        member = await self._resolve_member_from_context(ctx, member)
        if member is None:
            return await ctx.send(f"Please mention a user or reply to a message to use `{alias}`.", ephemeral=ctx.interaction is not None)
        required_role_id = await self._get_required_role_id(ctx.guild.id)
        if required_role_id and not ctx.author.guild_permissions.administrator:
            required_role = ctx.guild.get_role(required_role_id)
            if required_role is None:
                return await ctx.send(
                    "The configured required role no longer exists. Set it again with `,setup reqrole @role`.",
                    ephemeral=ctx.interaction is not None,
                )
            if required_role_id not in {role.id for role in ctx.author.roles}:
                return await ctx.send(
                    f"You need {required_role.mention} to use role aliases.",
                    ephemeral=ctx.interaction is not None,
                )
        async with self.bot.db.execute(
            "SELECT role_id FROM role_mappings WHERE guild_id=? AND alias=?",
            (ctx.guild.id, alias),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return await ctx.send(f"No role is configured for `{alias}`.", ephemeral=ctx.interaction is not None)
        role = ctx.guild.get_role(row[0])
        if role is None:
            return await ctx.send("The configured role no longer exists. Reconfigure it with `,setup create <name> @role`.", ephemeral=ctx.interaction is not None)
        action, preposition = "added", "to"
        try:
            if member.id != ctx.author.id and not ctx.author.guild_permissions.manage_roles:
                raise PermissionError("You can only toggle this role for yourself.")
            await self._check_role_hierarchy(ctx, role, check_actor=member.id != ctx.author.id)
            if member.id != ctx.author.id and not ctx.author.guild_permissions.administrator and member.top_role >= ctx.author.top_role:
                raise PermissionError("You cannot modify a member with an equal or higher role.")
            if role in member.roles:
                await member.remove_roles(role, reason=f"{alias} role toggle by {ctx.author}")
                title = "Role Removed"
                action, preposition = "removed", "from"
            else:
                await member.add_roles(role, reason=f"{alias} role toggle by {ctx.author}")
                title = "Role Added"
        except PermissionError as error:
            return await ctx.send(str(error), ephemeral=ctx.interaction is not None)
        except discord.Forbidden:
            return await ctx.send("Discord denied that role change. Check role hierarchy and permissions.", ephemeral=ctx.interaction is not None)
        except discord.HTTPException:
            return await ctx.send("Discord could not complete that role change. Try again shortly.", ephemeral=ctx.interaction is not None)

        title_emoji = ROLE_ALIAS_TICK if title == "Role Added" else ROLE_ALIAS_CROSS
        await ctx.send(
            embed=discord.Embed(
                title=f"{title_emoji}  {title}",
                description=f"{ROLE_ALIAS_ARROW}  Successfully {action} {role.mention} role {preposition} {member.mention}.",
                color=0xFFDD00,
            ),
            ephemeral=ctx.interaction is not None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_group(name="role", invoke_without_command=True, case_insensitive=True)
    async def role_group(self, ctx):
        await ctx.send(
            "Use `,role add @member @role`, `,role remove @member @role`, "
            "`,role all bot|humans @role`, `,role create <name>`, or `,role list`. "
            "Configure role aliases with `,setup create <alias> @role`.",
            ephemeral=ctx.interaction is not None,
        )

    @role_group.command(name="create", description="Create a server role")
    @commands.has_guild_permissions(manage_roles=True)
    async def role_create(self, ctx, *, name: str):
        role_name = name.strip()
        if not role_name or len(role_name) > 100:
            return await ctx.send("Role names must contain between 1 and 100 characters.", ephemeral=ctx.interaction is not None)
        try:
            role = await ctx.guild.create_role(
                name=role_name,
                permissions=discord.Permissions.none(),
                reason=f"Role created by {ctx.author}",
            )
        except discord.Forbidden:
            return await ctx.send("Discord denied role creation. Check Manage Roles and the bot's role hierarchy.", ephemeral=ctx.interaction is not None)
        except discord.HTTPException:
            return await ctx.send("Discord could not create that role. Try again shortly.", ephemeral=ctx.interaction is not None)
        await ctx.send(
            f"{SUCCESS_TICK} Created role {role.mention} with no permissions.",
            ephemeral=ctx.interaction is not None,
        )

    async def _configure_role_alias(self, ctx, name, role):
        alias = name.strip().lower()
        if not ALIAS_PATTERN.fullmatch(alias):
            return await ctx.send("Use a short alias beginning with a letter; letters, digits, `_` and `-` are allowed.", ephemeral=ctx.interaction is not None)
        try:
            await self._check_role_hierarchy(ctx, role)
        except PermissionError as error:
            return await ctx.send(str(error), ephemeral=ctx.interaction is not None)
        await self.bot.db.execute(
            "INSERT INTO role_mappings (guild_id, alias, role_id, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(guild_id, alias) DO UPDATE SET "
            "role_id=excluded.role_id, created_by=excluded.created_by, created_at=excluded.created_at",
            (ctx.guild.id, alias, role.id, ctx.author.id, utc_now().isoformat()),
        )
        await self.bot.db.commit()
        existing_command = self.bot.get_command(alias)
        self._add_role_command(alias)
        direct_command = (
            existing_command is None
            or existing_command is self._role_alias_commands.get(alias)
        )
        usage = (
            f"`{alias} <user>`"
            if direct_command
            else f"`setup toggle {alias} <user>`"
        )
        required_role_id = await self._get_required_role_id(ctx.guild.id)
        required_role_note = (
            f"{ROLE_ALIAS_DOT}  Requires configured required role to use"
            if required_role_id is not None
            else f"{ROLE_ALIAS_DOT}  No required role is configured"
        )
        success = discord.Embed(
            title=f"{ROLE_ALIAS_TICK} Custom Command Created",
            description=(
                f"{ROLE_ALIAS_SETTING}  Usage:\n"
                f"{ROLE_ALIAS_DOT}  Use {usage} to assign/remove the role\n"
                f"{required_role_note}\n\n"
                f"{ROLE_ALIAS_ARROW}  Created command {alias} for {role.mention}"
            ),
            color=0xFFDD00,
        )
        await ctx.send(
            embed=success,
            ephemeral=ctx.interaction is not None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _alias_is_configured(self, guild_id, alias):
        async with self.bot.db.execute(
            "SELECT 1 FROM role_mappings WHERE guild_id=? AND alias=?", (guild_id, alias)
        ) as cursor:
            return await cursor.fetchone() is not None

    async def _get_required_role_id(self, guild_id):
        async with self.bot.db.execute(
            "SELECT required_role_id FROM role_alias_settings WHERE guild_id=?",
            (guild_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else None

    @role_group.command(name="setup", description="Show role alias setup usage")
    @commands.has_guild_permissions(manage_roles=True)
    async def role_setup(self, ctx):
        embed = discord.Embed(
            title="Role Alias Setup",
            description=(
                "Create aliases with `,setup create <name> <role>`, then run `name @user` "
                "to toggle that role. If the name conflicts with a built-in command, use "
                "`,setup toggle <name> @user`. Use `,setup list` to view aliases, "
                "`,setup remove <name>` to remove one, or `,setup reqrole @role` to restrict "
                "alias use (server administrators bypass it)."
            ),
            color=0xFFDD00,
        )
        await ctx.send(embed=embed, ephemeral=ctx.interaction is not None)

    @role_group.command(name="list", description="List this server's role aliases")
    async def role_list(self, ctx):
        await self._send_role_list(ctx)

    async def _change_member_role(self, ctx, member, role, *, add):
        try:
            await self._check_role_hierarchy(ctx, role)
            if member.id != ctx.author.id and not ctx.author.guild_permissions.administrator and member.top_role >= ctx.author.top_role:
                raise PermissionError("You cannot modify a member with an equal or higher role.")
            if add:
                if role in member.roles:
                    return await ctx.send(f"{DETAIL_ARROW} {member.mention} already has {role.mention}.", ephemeral=ctx.interaction is not None)
                await member.add_roles(role, reason=f"Role added by {ctx.author}")
                title, action = "Role Added", "added to"
            else:
                if role not in member.roles:
                    return await ctx.send(f"{DETAIL_ARROW} {member.mention} does not have {role.mention}.", ephemeral=ctx.interaction is not None)
                await member.remove_roles(role, reason=f"Role removed by {ctx.author}")
                title, action = "Role Removed", "removed from"
        except PermissionError as error:
            return await ctx.send(f"{DETAIL_ARROW} {error}", ephemeral=ctx.interaction is not None)
        except discord.Forbidden:
            return await ctx.send(f"{DETAIL_ARROW} Discord denied that role change. Check role hierarchy and permissions.", ephemeral=ctx.interaction is not None)
        except discord.HTTPException:
            return await ctx.send(f"{DETAIL_ARROW} Discord could not complete that role change. Try again shortly.", ephemeral=ctx.interaction is not None)
        await ctx.send(
            f"{SUCCESS_TICK} **{title}**\n\n{DETAIL_ARROW} {role.mention} {action} {member.mention}.",
            ephemeral=ctx.interaction is not None,
        )

    async def _bulk_assign_role(self, ctx, role, *, bots=None, add=True):
        try:
            await self._check_role_hierarchy(ctx, role)
            if not ctx.guild.chunked:
                await ctx.guild.chunk(cache=True)
        except PermissionError as error:
            return await ctx.send(f"{DETAIL_ARROW} {error}", ephemeral=ctx.interaction is not None)
        except discord.HTTPException as error:
            return await ctx.send(f"{DETAIL_ARROW} Could not load the server member list: {error}", ephemeral=ctx.interaction is not None)

        targets = [
            member for member in ctx.guild.members
            if (role not in member.roles if add else role in member.roles)
            and (bots is None or member.bot is bots)
        ]
        action = "Assigning" if add else "Removing"
        participle = "Added" if add else "Removed"
        status = await ctx.send(f"{DETAIL_ARROW} {action} {role.mention} for **{len(targets)}** member(s)...", ephemeral=ctx.interaction is not None)
        added = failed = 0
        for index, member in enumerate(targets, start=1):
            try:
                if add:
                    await member.add_roles(role, reason=f"Bulk role assignment by {ctx.author}")
                else:
                    await member.remove_roles(role, reason=f"Bulk role removal by {ctx.author}")
                added += 1
            except (discord.Forbidden, discord.HTTPException):
                failed += 1
            if index % 25 == 0:
                await asyncio.sleep(0)
        await edit_message_v2(
            status,
            content=(
                f"{SUCCESS_TICK} **Role {'Assignment' if add else 'Removal'} Complete**\n\n"
                f"{DETAIL_ARROW} {participle} {role.mention} for **{added}** member(s); **{failed}** failed."
            ),
        )

    @role_group.command(name="add", description="Add a role to a member")
    @commands.has_guild_permissions(manage_roles=True)
    async def role_add(self, ctx, member: discord.Member, role: discord.Role):
        await self._change_member_role(ctx, member, role, add=True)

    @role_group.command(name="remove", description="Remove a role from a member")
    @commands.has_guild_permissions(manage_roles=True)
    async def role_remove(self, ctx, member: discord.Member, role: discord.Role):
        await self._change_member_role(ctx, member, role, add=False)

    @role_group.group(name="all", invoke_without_command=True, case_insensitive=True)
    @commands.has_guild_permissions(manage_roles=True)
    async def role_all(self, ctx):
        await ctx.send(f"{DETAIL_ARROW} Choose `bot` or `humans`, then mention a role.", ephemeral=ctx.interaction is not None)

    @role_all.command(name="bot", aliases=["bots"], description="Assign a role to all bots")
    async def role_all_bots(self, ctx, role: discord.Role):
        await self._bulk_assign_role(ctx, role, bots=True)

    @role_all.command(name="humans", aliases=["human"], description="Assign a role to all humans")
    async def role_all_humans(self, ctx, role: discord.Role):
        await self._bulk_assign_role(ctx, role, bots=False)

    @commands.group(name="roleall", invoke_without_command=True, case_insensitive=True)
    @commands.has_guild_permissions(manage_roles=True)
    async def roleall(self, ctx, *, arguments: str = ""):
        parts = arguments.split(maxsplit=1)
        if not parts:
            return await ctx.send(
                "Use `,roleall @role` for everyone, or `,roleall human|bot add|remove @role`.",
                ephemeral=ctx.interaction is not None,
            )
        target_type = parts[0].casefold()
        if target_type in {"human", "humans", "bot", "bots"} and len(parts) == 2:
            try:
                role = await self._convert_role_argument(ctx, parts[1])
            except commands.BadArgument as error:
                return await ctx.send(str(error), ephemeral=ctx.interaction is not None)
            return await self._bulk_assign_role(ctx, role, bots=target_type in {"bot", "bots"})
        try:
            role = await self._convert_role_argument(ctx, arguments)
        except commands.BadArgument as error:
            return await ctx.send(str(error), ephemeral=ctx.interaction is not None)
        await self._bulk_assign_role(ctx, role)

    @roleall.command(name="add")
    @commands.has_guild_permissions(manage_roles=True)
    async def roleall_add(self, ctx, role: discord.Role):
        await self._bulk_assign_role(ctx, role, add=True)

    @roleall.command(name="remove")
    @commands.has_guild_permissions(manage_roles=True)
    async def roleall_remove(self, ctx, role: discord.Role):
        await self._bulk_assign_role(ctx, role, add=False)

    @roleall.group(name="human", aliases=["humans"], invoke_without_command=True, case_insensitive=True)
    @commands.has_guild_permissions(manage_roles=True)
    async def roleall_human(self, ctx, *, role_argument: str = ""):
        if not role_argument:
            return await ctx.send(
                "Use `,roleall human add @role` or `,roleall human remove @role`.",
                ephemeral=ctx.interaction is not None,
            )
        try:
            role = await self._convert_role_argument(ctx, role_argument)
        except commands.BadArgument as error:
            return await ctx.send(str(error), ephemeral=ctx.interaction is not None)
        await self._bulk_assign_role(ctx, role, bots=False, add=True)

    @roleall_human.command(name="add")
    @commands.has_guild_permissions(manage_roles=True)
    async def roleall_human_add(self, ctx, role: discord.Role):
        await self._bulk_assign_role(ctx, role, bots=False, add=True)

    @roleall_human.command(name="remove")
    @commands.has_guild_permissions(manage_roles=True)
    async def roleall_human_remove(self, ctx, role: discord.Role):
        await self._bulk_assign_role(ctx, role, bots=False, add=False)

    @roleall.group(name="bot", aliases=["bots"], invoke_without_command=True, case_insensitive=True)
    @commands.has_guild_permissions(manage_roles=True)
    async def roleall_bot(self, ctx, *, role_argument: str = ""):
        if not role_argument:
            return await ctx.send(
                "Use `,roleall bot add @role` or `,roleall bot remove @role`.",
                ephemeral=ctx.interaction is not None,
            )
        try:
            role = await self._convert_role_argument(ctx, role_argument)
        except commands.BadArgument as error:
            return await ctx.send(str(error), ephemeral=ctx.interaction is not None)
        await self._bulk_assign_role(ctx, role, bots=True, add=True)

    @roleall_bot.command(name="add")
    @commands.has_guild_permissions(manage_roles=True)
    async def roleall_bot_add(self, ctx, role: discord.Role):
        await self._bulk_assign_role(ctx, role, bots=True, add=True)

    @roleall_bot.command(name="remove")
    @commands.has_guild_permissions(manage_roles=True)
    async def roleall_bot_remove(self, ctx, role: discord.Role):
        await self._bulk_assign_role(ctx, role, bots=True, add=False)

    async def _send_role_list(self, ctx):
        async with self.bot.db.execute(
            "SELECT alias, role_id FROM role_mappings WHERE guild_id=? ORDER BY alias",
            (ctx.guild.id,),
        ) as cursor:
            rows = await cursor.fetchall()
        required_role_id = await self._get_required_role_id(ctx.guild.id)
        if not rows and required_role_id is None:
            return await ctx.send("No role aliases are configured.", ephemeral=ctx.interaction is not None)
        pages = [rows[index:index + 10] for index in range(0, len(rows), 10)] or [[]]
        page = [0]
        view = self._role_list_view(ctx, pages, page, required_role_id) if len(pages) > 1 else None
        await ctx.send(
            embed=self._role_list_embed(
                ctx.guild,
                pages[0],
                page[0],
                len(pages),
                required_role_id,
            ),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_group(name="set", invoke_without_command=True, case_insensitive=True)
    async def setup_group(self, ctx):
        await ctx.send(
            "Use `/set greet message`, `/set greet channel`, "
            "`,set boost message <message>`, `,set boost channel <channel>`, "
            "or `,set boost test`. Add `{embed:name}` to a boost message to attach an embed.",
            ephemeral=ctx.interaction is not None,
        )

    @setup_group.group(name="greet", invoke_without_command=True, case_insensitive=True)
    async def set_greet_group(self, ctx):
        await ctx.send("Use `/set greet message` or `/set greet channel`.", ephemeral=ctx.interaction is not None)

    @set_greet_group.command(name="message", description="Set the welcome message; use {embed:name} to attach a saved embed")
    @commands.has_guild_permissions(manage_guild=True)
    async def set_greet_message(self, ctx, *, message: str):
        message = message.strip()
        if not message or len(message) > 2000:
            return await ctx.send("Greeting messages must contain 1 to 2000 characters.", ephemeral=ctx.interaction is not None)
        await self.bot.db.execute(
            "INSERT INTO guild_settings (guild_id, join_message, join_enabled) VALUES (?, ?, 1) "
            "ON CONFLICT(guild_id) DO UPDATE SET join_message=excluded.join_message, join_enabled=1",
            (ctx.guild.id, message),
        )
        await self.bot.db.commit()
        await ctx.send("Welcome message updated.", ephemeral=ctx.interaction is not None)

    @set_greet_group.command(name="channel", description="Set the welcome message channel")
    @commands.has_guild_permissions(manage_guild=True)
    async def set_greet_channel(self, ctx, channel: discord.TextChannel):
        await self.bot.db.execute(
            "INSERT INTO guild_settings (guild_id, join_channel_id, join_enabled) VALUES (?, ?, 1) "
            "ON CONFLICT(guild_id) DO UPDATE SET join_channel_id=excluded.join_channel_id, join_enabled=1",
            (ctx.guild.id, channel.id),
        )
        await self.bot.db.commit()
        await ctx.send(f"Welcome channel set to {channel.mention}.", ephemeral=ctx.interaction is not None)

    @setup_group.group(name="boost", invoke_without_command=True, case_insensitive=True)
    async def set_boost_group(self, ctx):
        await ctx.send(
            "Use `,set boost message <message>`, `,set boost channel <channel>`, "
            "or `,set boost test`. Add `{embed:name}` to the message to attach an embed.",
            ephemeral=ctx.interaction is not None,
        )

    @set_boost_group.command(
        name="message",
        help="Set the server boost message; use {embed:name} to attach a saved embed",
    )
    @commands.has_guild_permissions(manage_guild=True)
    async def set_boost_message(self, ctx, *, message: str):
        await self._configure_boost_message(ctx, message)

    @set_boost_group.command(name="channel", help="Set the server boost message channel")
    @commands.has_guild_permissions(manage_guild=True)
    async def set_boost_channel(self, ctx, channel: discord.TextChannel):
        await self._configure_boost_channel(ctx, channel)

    @set_boost_group.command(name="test", help="Preview the configured boost message")
    @commands.has_guild_permissions(manage_guild=True)
    async def set_boost_test(self, ctx):
        await self.boost_test.callback(self, ctx)

    async def _configure_role_alias_requirement(self, ctx, role):
        if role.is_default():
            return await ctx.send(
                "The @everyone role cannot be used as the required role.",
                ephemeral=ctx.interaction is not None,
            )
        await self.bot.db.execute(
            "INSERT INTO role_alias_settings (guild_id, required_role_id, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(guild_id) DO UPDATE SET "
            "required_role_id=excluded.required_role_id, updated_by=excluded.updated_by, "
            "updated_at=excluded.updated_at",
            (ctx.guild.id, role.id, ctx.author.id, utc_now().isoformat()),
        )
        await self.bot.db.commit()
        await ctx.send(
            embed=discord.Embed(
                title=f"{ROLE_ALIAS_TICK}  Required Role Set",
                description=(
                    f"{ROLE_ALIAS_ARROW}  {role.mention} is required to use role aliases. "
                    "Server administrators can always use them."
                ),
                color=0xFFDD00,
            ),
            ephemeral=ctx.interaction is not None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.group(name="setup", invoke_without_command=True, case_insensitive=True)
    async def role_alias_setup_group(self, ctx):
        await ctx.send(
            "Use `,setup create <name> <role>`, `,setup toggle <name> <user>`, "
            "`,setup remove <name>`, `,setup reqrole <role>`, or `,setup list`."
        )

    @role_alias_setup_group.command(name="create", help="Configure a role alias command")
    @commands.has_guild_permissions(manage_roles=True)
    async def role_alias_setup_create(self, ctx, name: str, role: discord.Role):
        await self._configure_role_alias(ctx, name, role)

    @role_alias_setup_group.command(name="toggle", help="Assign or remove a configured role alias")
    async def role_alias_setup_toggle(self, ctx, name: str, member: discord.Member):
        await self.toggle_member_role(ctx, name.strip().lower(), member)

    @role_alias_setup_group.command(name="reqrole", help="Require a role to use role aliases")
    @commands.has_guild_permissions(manage_roles=True)
    async def setup_reqrole_prefix(self, ctx, role: discord.Role):
        await self._configure_role_alias_requirement(ctx, role)

    @role_alias_setup_group.command(name="list", help="List configured role aliases")
    async def role_alias_setup_list(self, ctx):
        await self._send_role_list(ctx)

    @role_alias_setup_group.command(name="remove", help="Remove a configured role alias")
    @commands.has_guild_permissions(manage_roles=True)
    async def role_alias_setup_remove(self, ctx, name: str):
        alias = name.strip().lower()
        if not ALIAS_PATTERN.fullmatch(alias):
            return await ctx.send(
                "Enter the alias name to remove.",
                ephemeral=ctx.interaction is not None,
            )
        if not await self._alias_is_configured(ctx.guild.id, alias):
            return await ctx.send(
                f"No role alias is configured for `{alias}`.",
                ephemeral=ctx.interaction is not None,
            )
        await self.bot.db.execute(
            "DELETE FROM role_mappings WHERE guild_id=? AND alias=?",
            (ctx.guild.id, alias),
        )
        await self.bot.db.commit()
        async with self.bot.db.execute(
            "SELECT 1 FROM role_mappings WHERE alias=? LIMIT 1",
            (alias,),
        ) as cursor:
            still_configured = await cursor.fetchone() is not None
        command = self._role_alias_commands.get(alias)
        if not still_configured and command is not None:
            if self.bot.get_command(alias) is command:
                self.bot.remove_command(alias)
            self._role_alias_commands.pop(alias, None)
        await ctx.send(
            f"{ROLE_ALIAS_TICK} Removed role alias `{alias}`.",
            ephemeral=ctx.interaction is not None,
        )

    def _role_list_embed(self, guild, rows, page, total_pages, required_role_id=None):
        lines = []
        for alias, role_id in rows:
            role = guild.get_role(role_id)
            lines.append(f"`{alias} @user` → {role.mention if role else f'Unavailable role ({role_id})'}")
        embed = discord.Embed(
            title="Configured Role Aliases",
            description="\n".join(lines) or "No role aliases are configured.",
            color=0xFFDD00,
        )
        if required_role_id is not None:
            required_role = guild.get_role(required_role_id)
            embed.add_field(
                name="Required role",
                value=required_role.mention if required_role else f"Unavailable role ({required_role_id})",
                inline=False,
            )
        embed.set_footer(text=f"Page {page + 1}/{total_pages}")
        return embed

    def _role_list_view(self, ctx, pages, page, required_role_id=None):
        cog = self

        class RolePages(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=120)
                self.previous.disabled = True
                self.next.disabled = len(pages) < 2

            async def interaction_check(self, interaction):
                if interaction.user.id == ctx.author.id:
                    return True
                await send_interaction_v2(interaction, "This list is not yours.", ephemeral=True)
                return False

            @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
            async def previous(self, interaction, _button):
                page[0] = max(page[0] - 1, 0)
                self.previous.disabled = page[0] == 0
                self.next.disabled = page[0] == len(pages) - 1
                await edit_interaction_v2(
                    interaction,
                    embed=cog._role_list_embed(
                        ctx.guild, pages[page[0]], page[0], len(pages), required_role_id
                    ),
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

            @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
            async def next(self, interaction, _button):
                page[0] = min(page[0] + 1, len(pages) - 1)
                self.previous.disabled = page[0] == 0
                self.next.disabled = page[0] == len(pages) - 1
                await edit_interaction_v2(
                    interaction,
                    embed=cog._role_list_embed(
                        ctx.guild, pages[page[0]], page[0], len(pages), required_role_id
                    ),
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

        return RolePages()

    async def _send_paged_list(self, ctx, title, lines, *, page_size=10):
        pages = [lines[index:index + page_size] for index in range(0, len(lines), page_size)]
        current_page = [0]

        def page_embed(index):
            embed = discord.Embed(
                title=title,
                description="\n".join(pages[index]),
                color=0xFFDD00,
            )
            embed.set_footer(text=f"Page {index + 1}/{len(pages)}")
            return embed

        class ListPages(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=120)
                self.previous.disabled = True
                self.next.disabled = len(pages) < 2

            async def interaction_check(self, interaction):
                if interaction.user.id == ctx.author.id:
                    return True
                await send_interaction_v2(interaction, "This list is not yours.", ephemeral=True)
                return False

            @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
            async def previous(self, interaction, _button):
                current_page[0] = max(current_page[0] - 1, 0)
                self.previous.disabled = current_page[0] == 0
                self.next.disabled = current_page[0] == len(pages) - 1
                await edit_interaction_v2(interaction, embed=page_embed(current_page[0]), view=self)

            @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
            async def next(self, interaction, _button):
                current_page[0] = min(current_page[0] + 1, len(pages) - 1)
                self.previous.disabled = current_page[0] == 0
                self.next.disabled = current_page[0] == len(pages) - 1
                await edit_interaction_v2(interaction, embed=page_embed(current_page[0]), view=self)

            @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary)
            async def close(self, interaction, _button):
                await edit_interaction_v2(interaction, content="List closed.", embed=None, view=None)
                self.stop()

        await ctx.send(
            embed=page_embed(0),
            view=ListPages(),
            ephemeral=ctx.interaction is not None,
        )

    @commands.hybrid_group(name="embed", aliases=["embeds"], invoke_without_command=True, case_insensitive=True)
    async def embed_group(self, ctx):
        await ctx.send(
            "Use `/embed create`, `/embed import`, `/embed show`, `/embed list`, "
            "`/embed edit`, or `/embed remove`.",
            ephemeral=ctx.interaction is not None,
        )

    @commands.hybrid_group(name="events", invoke_without_command=True, case_insensitive=True)
    async def events_group(self, ctx):
        await ctx.send(
            "Use `/events set`, `/events channel`, `/events show`, `/events list`, "
            "`/events test`, or `/events remove` for join, leave, and boost replies. "
            "Attach saved embeds in a reply with `{embed:name}`.",
            ephemeral=ctx.interaction is not None,
        )

    async def _get_community_event_reply(self, guild_id, event):
        async with self.bot.db.execute(
            "SELECT response, channel_id FROM community_event_replies WHERE guild_id=? AND event=?",
            (guild_id, event),
        ) as cursor:
            return await cursor.fetchone()

    @events_group.command(name="set", description="Set the reply for a server event")
    @commands.has_guild_permissions(manage_guild=True)
    async def event_set(self, ctx, event: Literal["join", "leave", "boost"], *, response: str):
        response = response.strip()
        if not response or len(response) > 2000:
            return await ctx.send("Event replies must contain 1 to 2000 characters.", ephemeral=ctx.interaction is not None)
        await self.bot.db.execute(
            "INSERT INTO community_event_replies (guild_id, event, response, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(guild_id, event) DO UPDATE SET "
            "response=excluded.response, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
            (ctx.guild.id, event, response, ctx.author.id, utc_now().isoformat()),
        )
        await self.bot.db.commit()
        current = await self._get_community_event_reply(ctx.guild.id, event)
        note = " Set its destination with `/events channel` before it can fire." if not current[1] else ""
        await ctx.send(f"The {event} event reply was saved.{note}", ephemeral=ctx.interaction is not None)

    @commands.has_guild_permissions(manage_guild=True)
    async def event_design(self, ctx, event: Literal["join", "leave", "boost"], name: str):
        name = name.strip().casefold()
        if not name or len(name) > 50:
            return await ctx.send("Embed names must contain 1 to 50 characters.", ephemeral=ctx.interaction is not None)
        if ctx.interaction is None:
            return await ctx.send("Set the event reply with `/events set` and attach a saved embed using `{embed:name}`.")

        async with self.bot.db.execute(
            "SELECT response FROM community_event_replies WHERE guild_id=? AND event=?",
            (ctx.guild.id, event),
        ) as cursor:
            current = await cursor.fetchone()
        current_response = current[0] if current and current[0] else ""
        current_embed = re.search(r"\{embed:([^}]+)\}", current_response, re.IGNORECASE)
        source_name = current_embed.group(1).strip().casefold() if current_embed else name
        async with self.bot.db.execute(
            "SELECT config FROM custom_embeds WHERE guild_id=? AND name=?",
            (ctx.guild.id, source_name),
        ) as cursor:
            saved = await cursor.fetchone()
        config = json.loads(saved[0]) if saved else DEFAULT_EMBED_CONFIG
        extra_values = {
            "event": event,
            "message": f"Test {event} event",
            "message.content": f"Test {event} event",
        }
        if event == "boost":
            extra_values.update({
                "booster": ctx.author.mention,
                "boost.user": ctx.author.mention,
                "boost.count": getattr(ctx.guild, "premium_subscription_count", 0) or 0,
                "boost.level": getattr(ctx.guild, "premium_tier", 0) or 0,
            })

        async def save_event_embed(updated_config):
            response = re.sub(r"\{embed:[^}]+\}", "", current_response, flags=re.IGNORECASE).strip()
            response = f"{response} {{embed:{name}}}" if response else f"{{embed:{name}}}"
            if len(response) > 2000:
                raise ValueError("The existing event reply is too long to add an embed action.")
            await self.save_embed(ctx.guild.id, name, updated_config, ctx.author.id)
            await self.bot.db.execute(
                "INSERT INTO community_event_replies "
                "(guild_id, event, response, updated_by, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id, event) DO UPDATE SET response=excluded.response, "
                "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                (ctx.guild.id, event, response, ctx.author.id, utc_now().isoformat()),
            )
            await self.bot.db.commit()

        await self._open_embed_editor(
            ctx.interaction,
            f"{event} event: {name}",
            config,
            owner_id=ctx.author.id,
            save_callback=save_event_embed,
            extra_values=extra_values,
        )

    @events_group.command(name="channel", description="Choose where an event reply is sent")
    @commands.has_guild_permissions(manage_guild=True)
    async def event_channel(
        self,
        ctx,
        event: Literal["join", "leave", "boost"],
        channel: discord.TextChannel,
    ):
        await self.bot.db.execute(
            "INSERT INTO community_event_replies (guild_id, event, channel_id, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(guild_id, event) DO UPDATE SET "
            "channel_id=excluded.channel_id, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
            (ctx.guild.id, event, channel.id, ctx.author.id, utc_now().isoformat()),
        )
        await self.bot.db.commit()
        await ctx.send(f"The {event} event will send replies to {channel.mention}.", ephemeral=ctx.interaction is not None)

    @events_group.command(name="show", description="Show one event reply")
    async def event_show(self, ctx, event: Literal["join", "leave", "boost"]):
        row = await self._get_community_event_reply(ctx.guild.id, event)
        if not row or not row[0]:
            return await ctx.send(f"No {event} event reply is configured.", ephemeral=ctx.interaction is not None)
        channel = ctx.guild.get_channel(row[1]) if row[1] else None
        embed = discord.Embed(title=f"{event.title()} Event Reply", description=row[0][:4000], color=0xFFDD00)
        embed.add_field(name="Channel", value=channel.mention if channel else "Not configured", inline=False)
        await ctx.send(embed=embed, ephemeral=ctx.interaction is not None)

    @events_group.command(name="list", description="List the configured event replies")
    async def event_list(self, ctx):
        async with self.bot.db.execute(
            "SELECT event, response, channel_id FROM community_event_replies WHERE guild_id=? ORDER BY event",
            (ctx.guild.id,),
        ) as cursor:
            rows = await cursor.fetchall()
        settings = {event: (response, channel_id) for event, response, channel_id in rows}
        embed = discord.Embed(title="Event Replies", color=0xFFDD00)
        for event in COMMUNITY_EVENT_KINDS:
            response, channel_id = settings.get(event, (None, None))
            channel = ctx.guild.get_channel(channel_id) if channel_id else None
            status = "Reply set" if response else "No reply"
            embed.add_field(
                name=event.title(),
                value=f"{status} · {channel.mention if channel else 'No channel'}",
                inline=True,
            )
        await ctx.send(embed=embed, ephemeral=ctx.interaction is not None)

    @events_group.command(name="remove", description="Remove an event reply")
    @commands.has_guild_permissions(manage_guild=True)
    async def event_remove(self, ctx, event: Literal["join", "leave", "boost"]):
        await self.bot.db.execute(
            "UPDATE community_event_replies SET response=NULL, updated_by=?, updated_at=? "
            "WHERE guild_id=? AND event=?",
            (ctx.author.id, utc_now().isoformat(), ctx.guild.id, event),
        )
        await self.bot.db.commit()
        await ctx.send(f"The {event} event reply was removed.", ephemeral=ctx.interaction is not None)

    @events_group.command(name="test", description="Send a test event reply")
    @commands.has_guild_permissions(manage_guild=True)
    async def event_test(self, ctx, event: Literal["join", "leave", "boost"]):
        row = await self._get_community_event_reply(ctx.guild.id, event)
        if not row or not row[0]:
            return await ctx.send(f"No {event} event reply is configured.", ephemeral=ctx.interaction is not None)
        channel = ctx.guild.get_channel(row[1]) if row[1] else None
        if channel is None:
            return await ctx.send(f"No channel is configured for {event}. Use `/events channel` first.", ephemeral=ctx.interaction is not None)
        await self._send_automated_response(
            ctx.message,
            row[0],
            allow_delete=False,
            channel=channel,
            user=ctx.author,
            guild=ctx.guild,
            extra_values={"event": event, "message": f"Test {event} event", "message.content": f"Test {event} event"},
        )
        await ctx.send(f"Test {event} event reply sent to {channel.mention}.", ephemeral=ctx.interaction is not None)

    async def send_community_event_reply(self, guild, member, event):
        row = await self._get_community_event_reply(guild.id, event)
        if not row or not row[0]:
            return False
        channel = guild.get_channel(row[1]) if row[1] else None
        if channel is None:
            return False
        boost_count = getattr(guild, "premium_subscription_count", 0) or 0
        boost_level = getattr(guild, "premium_tier", 0) or 0
        return await self._send_automated_response(
            None,
            row[0],
            allow_delete=False,
            channel=channel,
            user=member,
            guild=guild,
            extra_values={
                "event": event,
                "message": event,
                "message.content": event,
                "booster": getattr(member, "mention", ""),
                "boost.user": getattr(member, "mention", ""),
                "boost.count": boost_count,
                "boost_count": boost_count,
                "boost.level": boost_level,
            },
        )

    async def save_embed(self, guild_id, name, config, user_id):
        serialized = json.dumps(config, separators=(",", ":"))
        await self.bot.db.execute(
            "INSERT INTO custom_embeds (guild_id, name, config, created_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(guild_id, name) DO UPDATE SET "
            "config=excluded.config, updated_at=excluded.updated_at",
            (guild_id, name.lower(), serialized, user_id, utc_now().isoformat()),
        )
        await self.bot.db.commit()

    async def _open_embed_modal(self, ctx, name, config=None, *, introduction=False):
        intro_embed = build_embed_creation_intro(name) if introduction else None
        intro_message = embed_creation_announcement(name) if introduction else None
        if ctx.interaction is None:
            builder = AdvancedEmbedBuilderView(
                self,
                ctx.guild.id,
                name,
                config,
                owner_id=ctx.author.id,
            )
            preview = intro_embed or build_custom_embed(
                builder.config, ctx.author, ctx.guild, ctx.channel, self.bot.user
            )
            builder.message = await ctx.channel.send(
                content=intro_message,
                embed=preview,
                view=builder,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return builder
        await self._open_embed_editor(
            ctx.interaction,
            name,
            config,
            owner_id=ctx.author.id,
            intro_embed=intro_embed,
            intro_message=intro_message,
        )

    async def _open_embed_editor(
        self,
        interaction,
        name,
        config=None,
        *,
        owner_id,
        save_callback=None,
        extra_values=None,
        intro_embed=None,
        intro_message=None,
    ):
        builder = AdvancedEmbedBuilderView(
            self,
            interaction.guild.id,
            name,
            config,
            owner_id=owner_id,
            save_callback=save_callback,
            extra_values=extra_values,
        )
        preview = intro_embed or build_custom_embed(
            config or DEFAULT_EMBED_CONFIG,
            interaction.user,
            interaction.guild,
            interaction.channel,
            self.bot.user,
            extra_values,
        )
        response = await interaction.response.send_message(
            content=intro_message,
            embed=preview,
            view=builder,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        builder.message = getattr(response, "resource", None)
        if builder.message is None:
            try:
                builder.message = await interaction.original_response()
            except discord.HTTPException:
                pass

    @embed_group.command(name="create", description="Create a reusable custom embed with the live button editor")
    @commands.has_guild_permissions(manage_guild=True)
    async def embed_create(self, ctx, name: str, config: str = ""):
        name = name.strip().lower()
        if not name or len(name) > 50:
            return await ctx.send("Embed names must contain 1 to 50 characters.", ephemeral=ctx.interaction is not None)
        if config:
            try:
                parsed = validate_embed_config(config)
            except ValueError as error:
                return await ctx.send(str(error), ephemeral=ctx.interaction is not None)
        else:
            parsed = DEFAULT_EMBED_CONFIG
        await self.save_embed(ctx.guild.id, name, parsed, ctx.author.id)
        await self._open_embed_modal(ctx, name, parsed, introduction=True)

    @embed_group.command(name="edit", description="Edit a reusable custom embed")
    @commands.has_guild_permissions(manage_guild=True)
    async def embed_edit(self, ctx, name: str, config: str = ""):
        name = name.strip().lower()
        async with self.bot.db.execute(
            "SELECT config FROM custom_embeds WHERE guild_id=? AND name=?", (ctx.guild.id, name)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return await ctx.send(f"Embed `{name}` was not found.", ephemeral=ctx.interaction is not None)
        parsed = json.loads(row[0])
        if config:
            try:
                parsed = validate_embed_config(config)
            except ValueError as error:
                return await ctx.send(str(error), ephemeral=ctx.interaction is not None)
            await self.save_embed(ctx.guild.id, name, parsed, ctx.author.id)
            return await ctx.send(f"Embed `{name}` updated.", ephemeral=ctx.interaction is not None)
        await self._open_embed_modal(ctx, name, parsed)

    @embed_group.command(name="import", description="Create or replace a reusable embed from JSON")
    @commands.has_guild_permissions(manage_guild=True)
    async def embed_import(self, ctx, name: str, *, config: str):
        name = name.strip().casefold()
        if not name or len(name) > 50:
            return await ctx.send("Embed names must contain 1 to 50 characters.", ephemeral=ctx.interaction is not None)
        try:
            parsed = validate_embed_config(config)
        except ValueError as error:
            return await ctx.send(str(error), ephemeral=ctx.interaction is not None)
        async with self.bot.db.execute(
            "SELECT 1 FROM custom_embeds WHERE guild_id=? AND name=?",
            (ctx.guild.id, name),
        ) as cursor:
            existed = await cursor.fetchone() is not None
        await self.save_embed(ctx.guild.id, name, parsed, ctx.author.id)
        result = "replaced" if existed else "imported"
        await ctx.send(
            f"Embed `{name}` {result} from JSON.",
            ephemeral=ctx.interaction is not None,
        )

    @embed_group.command(name="show", description="Send a saved custom embed")
    async def embed_show(self, ctx, name: str):
        async with self.bot.db.execute(
            "SELECT config FROM custom_embeds WHERE guild_id=? AND name=?", (ctx.guild.id, name.lower())
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return await ctx.send(f"Embed `{name}` was not found.", ephemeral=ctx.interaction is not None)
        try:
            config = json.loads(row[0])
            embed = build_custom_embed(config, ctx.author, ctx.guild, ctx.channel, self.bot.user)
            view = build_custom_embed_view(config, ctx.author, ctx.guild, ctx.channel, self.bot.user)
        except (ValueError, TypeError, discord.DiscordException) as error:
            return await ctx.send(f"Saved embed is invalid: {error}", ephemeral=ctx.interaction is not None)
        await ctx.send(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())

    @embed_group.command(name="preview", description="Preview a saved custom embed")
    async def embed_preview(self, ctx, name: str):
        await self.embed_show.callback(self, ctx, name)

    @embed_group.command(name="test", description="Test a saved custom embed without changing it")
    async def embed_test(self, ctx, name: str):
        await self.embed_show.callback(self, ctx, name)

    @embed_group.command(name="send", description="Send a saved custom embed to a channel")
    @commands.has_guild_permissions(manage_guild=True)
    async def embed_send(self, ctx, name: str, channel: discord.TextChannel):
        async with self.bot.db.execute(
            "SELECT config FROM custom_embeds WHERE guild_id=? AND name=?",
            (ctx.guild.id, name.strip().casefold()),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return await ctx.send(f"Embed `{name}` was not found.", ephemeral=ctx.interaction is not None)
        bot_member = ctx.guild.me
        permissions = channel.permissions_for(bot_member) if bot_member else None
        if permissions is None or not permissions.view_channel or not permissions.send_messages or not permissions.embed_links:
            return await ctx.send(f"I cannot send embeds in {channel.mention}; check View Channel, Send Messages, and Embed Links.")
        try:
            config = json.loads(row[0])
            embed = build_custom_embed(config, ctx.author, ctx.guild, channel, self.bot.user)
            view = build_custom_embed_view(config, ctx.author, ctx.guild, channel, self.bot.user)
        except (ValueError, TypeError, discord.DiscordException) as error:
            return await ctx.send(f"Saved embed is invalid: {error}")
        await send_v2(channel, embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
        await ctx.send(f"Embed `{name}` sent to {channel.mention}.", ephemeral=ctx.interaction is not None)

    @embed_group.command(name="clone", description="Clone a saved embed under a new name")
    @commands.has_guild_permissions(manage_guild=True)
    async def embed_clone(self, ctx, source: str, new_name: str):
        source = source.strip().casefold()
        new_name = new_name.strip().casefold()
        if not new_name or len(new_name) > 50:
            return await ctx.send("Embed names must contain 1 to 50 characters.", ephemeral=ctx.interaction is not None)
        async with self.bot.db.execute(
            "SELECT config FROM custom_embeds WHERE guild_id=? AND name=?",
            (ctx.guild.id, source),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return await ctx.send(f"Embed `{source}` was not found.", ephemeral=ctx.interaction is not None)
        async with self.bot.db.execute(
            "SELECT 1 FROM custom_embeds WHERE guild_id=? AND name=?",
            (ctx.guild.id, new_name),
        ) as cursor:
            if await cursor.fetchone():
                return await ctx.send(f"Embed `{new_name}` already exists.", ephemeral=ctx.interaction is not None)
        await self.save_embed(ctx.guild.id, new_name, json.loads(row[0]), ctx.author.id)
        await ctx.send(f"Embed `{source}` cloned as `{new_name}`.", ephemeral=ctx.interaction is not None)

    @embed_group.command(name="list", description="List saved custom embeds")
    async def embed_list(self, ctx):
        async with self.bot.db.execute(
            "SELECT name, updated_at FROM custom_embeds WHERE guild_id=? ORDER BY name",
            (ctx.guild.id,),
        ) as cursor:
            rows = await cursor.fetchall()
        if not rows:
            return await ctx.send("No custom embeds are saved.", ephemeral=ctx.interaction is not None)
        lines = [
            f"`{name}` • updated <t:{int(datetime.datetime.fromisoformat(updated).timestamp())}:R>"
            for name, updated in rows
        ]
        await self._send_paged_list(ctx, "Saved Embeds", lines)

    @embed_group.command(name="delete", description="Delete a saved custom embed")
    @commands.has_guild_permissions(manage_guild=True)
    async def embed_delete(self, ctx, name: str):
        normalized_name = name.lower()

        async def delete_embed():
            await self.bot.db.execute(
                "DELETE FROM custom_embeds WHERE guild_id=? AND name=?",
                (ctx.guild.id, normalized_name),
            )
            await self.bot.db.commit()

        await self._confirm_destructive_action(
            ctx,
            f"Delete saved embed `{normalized_name}`?",
            f"Embed `{normalized_name}` deleted if it existed.",
            delete_embed,
        )

    @embed_group.command(name="remove", description="Remove a saved custom embed")
    @commands.has_guild_permissions(manage_guild=True)
    async def embed_remove(self, ctx, name: str):
        await self.embed_delete.callback(self, ctx, name)

    @embed_group.group(name="field", invoke_without_command=True, case_insensitive=True)
    async def embed_field_group(self, ctx):
        await ctx.send("Use `/embed field add|edit|delete|list`.", ephemeral=ctx.interaction is not None)

    async def _get_saved_embed_config(self, ctx, name):
        async with self.bot.db.execute(
            "SELECT config FROM custom_embeds WHERE guild_id=? AND name=?",
            (ctx.guild.id, name.strip().casefold()),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        return json.loads(row[0])

    @embed_field_group.command(name="add", description="Add a field to a saved embed")
    @commands.has_guild_permissions(manage_guild=True)
    async def embed_field_add(self, ctx, name: str, field_name: str, value: str, inline: bool = False):
        try:
            config = await self._get_saved_embed_config(ctx, name)
            if config is None:
                return await ctx.send(f"Embed `{name}` was not found.", ephemeral=ctx.interaction is not None)
            fields = config.setdefault("fields", [])
            if len(fields) >= 25:
                return await ctx.send("Embeds can have at most 25 fields.", ephemeral=ctx.interaction is not None)
            fields.append({"name": field_name, "value": value, "inline": inline})
            build_custom_embed(config, ctx.author, ctx.guild, ctx.channel, self.bot.user)
        except (ValueError, TypeError, discord.DiscordException) as error:
            return await ctx.send(str(error), ephemeral=ctx.interaction is not None)
        await self.save_embed(ctx.guild.id, name, config, ctx.author.id)
        await ctx.send(f"Field added to embed `{name}`.", ephemeral=ctx.interaction is not None)

    @embed_field_group.command(name="edit", description="Edit a field in a saved embed")
    @commands.has_guild_permissions(manage_guild=True)
    async def embed_field_edit(self, ctx, name: str, index: int, field_name: str, value: str, inline: bool | None = None):
        try:
            config = await self._get_saved_embed_config(ctx, name)
            if config is None:
                return await ctx.send(f"Embed `{name}` was not found.", ephemeral=ctx.interaction is not None)
            fields = config.get("fields", [])
            if not 1 <= index <= len(fields):
                return await ctx.send("Choose an existing field number.", ephemeral=ctx.interaction is not None)
            fields[index - 1].update(name=field_name, value=value)
            if inline is not None:
                fields[index - 1]["inline"] = inline
            build_custom_embed(config, ctx.author, ctx.guild, ctx.channel, self.bot.user)
        except (ValueError, TypeError, discord.DiscordException) as error:
            return await ctx.send(str(error), ephemeral=ctx.interaction is not None)
        await self.save_embed(ctx.guild.id, name, config, ctx.author.id)
        await ctx.send(f"Field {index} updated in embed `{name}`.", ephemeral=ctx.interaction is not None)

    @embed_field_group.command(name="delete", description="Delete a field from a saved embed")
    @commands.has_guild_permissions(manage_guild=True)
    async def embed_field_delete(self, ctx, name: str, index: int):
        config = await self._get_saved_embed_config(ctx, name)
        if config is None:
            return await ctx.send(f"Embed `{name}` was not found.", ephemeral=ctx.interaction is not None)
        fields = config.get("fields", [])
        if not 1 <= index <= len(fields):
            return await ctx.send("Choose an existing field number.", ephemeral=ctx.interaction is not None)
        fields.pop(index - 1)
        await self.save_embed(ctx.guild.id, name, config, ctx.author.id)
        await ctx.send(f"Field {index} deleted from embed `{name}`.", ephemeral=ctx.interaction is not None)

    @embed_field_group.command(name="list", description="List fields in a saved embed")
    async def embed_field_list(self, ctx, name: str):
        config = await self._get_saved_embed_config(ctx, name)
        if config is None:
            return await ctx.send(f"Embed `{name}` was not found.", ephemeral=ctx.interaction is not None)
        fields = config.get("fields", [])
        lines = [f"{index}. **{field['name']}**: {field['value']}" for index, field in enumerate(fields, 1)]
        await ctx.send("\n".join(lines) if lines else f"Embed `{name}` has no fields.", ephemeral=ctx.interaction is not None)

    @commands.hybrid_group(name="autoresponders", aliases=["autoresponder", "ar"], invoke_without_command=True, case_insensitive=True)
    async def autoresponder_group(self, ctx):
        await ctx.send(
            "Use `/autoresponders add`, `/autoresponders edit`, `/autoresponders matchmode`, "
            "`/autoresponders remove`, `/autoresponders list`, or `/autoresponders show`. "
            "Attach saved embeds in a response with `{embed:name}`.",
            ephemeral=ctx.interaction is not None,
        )

    @autoresponder_group.command(name="add", description="Add a keyword autoresponder")
    @commands.has_guild_permissions(manage_guild=True)
    async def autoresponder_add(self, ctx, trigger: str, *, response: str):
        trigger = trigger.strip().casefold()
        response = response.strip()
        if not trigger or len(trigger) > 100:
            return await ctx.send("Triggers must contain 1 to 100 characters.", ephemeral=ctx.interaction is not None)
        if not response or len(response) > 2000:
            return await ctx.send("Responses must contain 1 to 2000 characters.", ephemeral=ctx.interaction is not None)
        async with self.bot.db.execute(
            "SELECT 1 FROM autoresponders WHERE guild_id=? AND trigger=?", (ctx.guild.id, trigger)
        ) as cursor:
            exists = await cursor.fetchone()
        if exists:
            return await ctx.send("That trigger already exists. Use `/autoresponders edit`.", ephemeral=ctx.interaction is not None)
        await self.bot.db.execute(
            "INSERT INTO autoresponders (guild_id, trigger, response, updated_by, updated_at) VALUES (?, ?, ?, ?, ?)",
            (ctx.guild.id, trigger, response, ctx.author.id, utc_now().isoformat()),
        )
        await self.bot.db.commit()
        await ctx.send(f"Autoresponder added for `{trigger}`.", ephemeral=ctx.interaction is not None)

    @autoresponder_group.command(name="edit", description="Edit a keyword autoresponder")
    @commands.has_guild_permissions(manage_guild=True)
    async def autoresponder_edit(self, ctx, trigger: str, *, response: str):
        trigger = trigger.strip().casefold()
        response = response.strip()
        if not response or len(response) > 2000:
            return await ctx.send("Responses must contain 1 to 2000 characters.", ephemeral=ctx.interaction is not None)
        async with self.bot.db.execute(
            "UPDATE autoresponders SET response=?, updated_by=?, updated_at=? WHERE guild_id=? AND trigger=?",
            (response, ctx.author.id, utc_now().isoformat(), ctx.guild.id, trigger),
        ) as cursor:
            updated = cursor.rowcount
        await self.bot.db.commit()
        if not updated:
            return await ctx.send(f"No autoresponder exists for `{trigger}`.", ephemeral=ctx.interaction is not None)
        await ctx.send(f"Autoresponder `{trigger}` updated.", ephemeral=ctx.interaction is not None)

    @autoresponder_group.command(name="matchmode", description="Change how an autoresponder matches messages")
    @commands.has_guild_permissions(manage_guild=True)
    async def autoresponder_matchmode(
        self,
        ctx,
        trigger: str,
        mode: Literal["exact", "contains", "startswith", "endswith"],
    ):
        trigger = trigger.strip().casefold()
        if not trigger:
            return await ctx.send("Trigger cannot be empty.", ephemeral=ctx.interaction is not None)
        async with self.bot.db.execute(
            "UPDATE autoresponders SET mode=?, updated_by=?, updated_at=? "
            "WHERE guild_id=? AND trigger=?",
            (mode, ctx.author.id, utc_now().isoformat(), ctx.guild.id, trigger),
        ) as cursor:
            updated = cursor.rowcount
        await self.bot.db.commit()
        if not updated:
            return await ctx.send(f"No autoresponder exists for `{trigger}`.", ephemeral=ctx.interaction is not None)
        await ctx.send(
            f"Autoresponder `{trigger}` now matches `{mode}`.",
            ephemeral=ctx.interaction is not None,
        )

    @commands.has_guild_permissions(manage_guild=True)
    async def autoresponder_design(self, ctx, trigger: str, name: str):
        trigger = trigger.strip().casefold()
        name = name.strip().casefold()
        if not trigger or len(trigger) > 100:
            return await ctx.send("Triggers must contain 1 to 100 characters.", ephemeral=ctx.interaction is not None)
        if not name or len(name) > 50:
            return await ctx.send("Embed names must contain 1 to 50 characters.", ephemeral=ctx.interaction is not None)
        if ctx.interaction is None:
            return await ctx.send("Set the response with `/autoresponders add` and attach a saved embed using `{embed:name}`.")

        async with self.bot.db.execute(
            "SELECT config FROM custom_embeds WHERE guild_id=? AND name=?",
            (ctx.guild.id, name),
        ) as cursor:
            saved = await cursor.fetchone()
        config = json.loads(saved[0]) if saved else DEFAULT_EMBED_CONFIG
        extra_values = {
            "message": trigger,
            "message.content": trigger,
        }

        async def save_autoresponder_embed(updated_config):
            async with self.bot.db.execute(
                "SELECT response FROM autoresponders WHERE guild_id=? AND trigger=?",
                (ctx.guild.id, trigger),
            ) as cursor:
                existing = await cursor.fetchone()
            response = existing[0] if existing else ""
            response = re.sub(r"\{embed:[^}]+\}", "", response, flags=re.IGNORECASE).strip()
            response = f"{response} {{embed:{name}}}" if response else f"{{embed:{name}}}"
            if len(response) > 2000:
                raise ValueError("The existing autoresponder text is too long to add an embed action.")

            await self.save_embed(ctx.guild.id, name, updated_config, ctx.author.id)
            await self.bot.db.execute(
                "INSERT INTO autoresponders (guild_id, trigger, response, updated_by, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(guild_id, trigger) DO UPDATE SET "
                "response=excluded.response, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                (ctx.guild.id, trigger, response, ctx.author.id, utc_now().isoformat()),
            )
            await self.bot.db.commit()

        await self._open_embed_editor(
            ctx.interaction,
            f"autoresponder: {trigger}",
            config,
            owner_id=ctx.author.id,
            save_callback=save_autoresponder_embed,
            extra_values=extra_values,
        )

    @autoresponder_group.command(name="remove", description="Remove a keyword autoresponder")
    @commands.has_guild_permissions(manage_guild=True)
    async def autoresponder_remove(self, ctx, trigger: str):
        trigger = trigger.strip().casefold()

        async def delete_autoresponder():
            await self.bot.db.execute(
                "DELETE FROM autoresponders WHERE guild_id=? AND trigger=?",
                (ctx.guild.id, trigger),
            )
            await self.bot.db.commit()

        await self._confirm_destructive_action(
            ctx,
            f"Remove autoresponder `{trigger}`?",
            f"Autoresponder `{trigger}` removed if it existed.",
            delete_autoresponder,
        )

    @autoresponder_group.command(name="delete", description="Delete a keyword autoresponder")
    @commands.has_guild_permissions(manage_guild=True)
    async def autoresponder_delete(self, ctx, trigger: str):
        await self.autoresponder_remove.callback(self, ctx, trigger)

    @autoresponder_group.command(name="list", description="List this server's autoresponders")
    async def autoresponder_list(self, ctx):
        async with self.bot.db.execute(
            "SELECT trigger, mode, channel_id, role_id, cooldown, enabled FROM autoresponders "
            "WHERE guild_id=? ORDER BY trigger",
            (ctx.guild.id,),
        ) as cursor:
            rows = await cursor.fetchall()
        if not rows:
            return await ctx.send("No autoresponders are configured.", ephemeral=ctx.interaction is not None)
        lines = []
        for trigger, mode, channel_id, role_id, cooldown, enabled in rows:
            restrictions = []
            if channel_id:
                channel = ctx.guild.get_channel(channel_id)
                restrictions.append(channel.mention if channel else f"missing channel `{channel_id}`")
            if role_id:
                role = ctx.guild.get_role(role_id)
                restrictions.append(role.mention if role else f"missing role `{role_id}`")
            restrictions.append(f"{cooldown}s cooldown")
            restrictions.append("enabled" if enabled else "disabled")
            lines.append(f"`{trigger}` — {mode}; {', '.join(restrictions)}")
        await self._send_paged_list(ctx, "Autoresponders", lines)

    async def _set_autoresponder_enabled(self, ctx, trigger, enabled):
        trigger = trigger.strip().casefold()
        async with self.bot.db.execute(
            "UPDATE autoresponders SET enabled=?, updated_by=?, updated_at=? "
            "WHERE guild_id=? AND trigger=?",
            (int(enabled), ctx.author.id, utc_now().isoformat(), ctx.guild.id, trigger),
        ) as cursor:
            changed = cursor.rowcount
        await self.bot.db.commit()
        if not changed:
            return await ctx.send(f"No autoresponder exists for `{trigger}`.", ephemeral=ctx.interaction is not None)
        state = "enabled" if enabled else "disabled"
        await ctx.send(f"Autoresponder `{trigger}` {state}.", ephemeral=ctx.interaction is not None)

    @autoresponder_group.command(name="show", description="Show one autoresponder's configuration")
    async def autoresponder_show(self, ctx, trigger: str):
        trigger = trigger.strip().casefold()
        async with self.bot.db.execute(
            "SELECT response, mode, channel_id, role_id, cooldown, enabled, excluded_channel_ids, "
            "allowed_role_ids, excluded_role_ids, cooldown_scope, case_sensitive "
            "FROM autoresponders WHERE guild_id=? AND trigger=?",
            (ctx.guild.id, trigger),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return await ctx.send(f"No autoresponder exists for `{trigger}`.", ephemeral=ctx.interaction is not None)
        response, mode, channel_id, role_id, cooldown, enabled, excluded_channels, allowed_roles, excluded_roles, scope, case_sensitive = row
        embed = discord.Embed(title=f"Autoresponder: {trigger}", color=0xFFDD00)
        embed.add_field(name="Status", value="Enabled" if enabled else "Disabled", inline=True)
        embed.add_field(name="Match", value=f"{mode}; {'case-sensitive' if case_sensitive else 'case-insensitive'}", inline=True)
        embed.add_field(name="Cooldown", value=f"{cooldown}s ({scope})", inline=True)
        embed.add_field(name="Response", value=response[:1024], inline=False)
        channel_ids = set(json.loads(excluded_channels or "[]"))
        if channel_id:
            channel_ids.add(channel_id)
        role_ids = set(json.loads(allowed_roles or "[]"))
        role_ids.update(json.loads(excluded_roles or "[]"))
        if role_id:
            role_ids.add(role_id)
        embed.add_field(
            name="Restrictions",
            value=(
                f"Channels: {', '.join(f'<#{item}>' for item in sorted(channel_ids)) or 'All'}\n"
                f"Roles: {', '.join(f'<@&{item}>' for item in sorted(role_ids)) or 'Everyone'}"
            ),
            inline=False,
        )
        await ctx.send(embed=embed, ephemeral=ctx.interaction is not None)

    @autoresponder_group.command(name="enable", description="Enable an autoresponder")
    @commands.has_guild_permissions(manage_guild=True)
    async def autoresponder_enable(self, ctx, trigger: str):
        await self._set_autoresponder_enabled(ctx, trigger, True)

    @autoresponder_group.command(name="disable", description="Disable an autoresponder")
    @commands.has_guild_permissions(manage_guild=True)
    async def autoresponder_disable(self, ctx, trigger: str):
        await self._set_autoresponder_enabled(ctx, trigger, False)

    @autoresponder_group.command(name="toggle", description="Toggle an autoresponder")
    @commands.has_guild_permissions(manage_guild=True)
    async def autoresponder_toggle(self, ctx, trigger: str):
        normalized = trigger.strip().casefold()
        async with self.bot.db.execute(
            "SELECT enabled FROM autoresponders WHERE guild_id=? AND trigger=?",
            (ctx.guild.id, normalized),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return await ctx.send(f"No autoresponder exists for `{normalized}`.", ephemeral=ctx.interaction is not None)
        await self._set_autoresponder_enabled(ctx, normalized, not bool(row[0]))

    @autoresponder_group.command(name="clear", description="Remove all autoresponders from this server")
    @commands.has_guild_permissions(manage_guild=True)
    async def autoresponder_clear(self, ctx):
        async def clear_autoresponders():
            await self.bot.db.execute("DELETE FROM autoresponders WHERE guild_id=?", (ctx.guild.id,))
            await self.bot.db.commit()

        await self._confirm_destructive_action(
            ctx,
            "Remove every autoresponder configured for this server?",
            "All server autoresponders were removed.",
            clear_autoresponders,
        )

    @autoresponder_group.command(name="options", description="Configure matching, restrictions, and cooldown for a trigger")
    @commands.has_guild_permissions(manage_guild=True)
    async def autoresponder_options(
        self,
        ctx,
        trigger: str,
        mode: Literal["contains", "exact", "starts", "starts_with", "ends", "ends_with"] | None = None,
        channel: discord.TextChannel | None = None,
        role: discord.Role | None = None,
        cooldown: int | None = None,
        enabled: bool | None = None,
        clear_restrictions: bool = False,
    ):
        trigger = trigger.strip().casefold()
        if not trigger or len(trigger) > 100:
            return await ctx.send("Triggers must contain 1 to 100 characters.", ephemeral=ctx.interaction is not None)
        if cooldown is not None and not 0 <= cooldown <= 86400:
            return await ctx.send("Cooldown must be between 0 and 86400 seconds.", ephemeral=ctx.interaction is not None)
        async with self.bot.db.execute(
            "SELECT mode, channel_id, role_id, cooldown, enabled FROM autoresponders "
            "WHERE guild_id=? AND trigger=?",
            (ctx.guild.id, trigger),
        ) as cursor:
            current = await cursor.fetchone()
        if not current:
            return await ctx.send(f"No autoresponder exists for `{trigger}`.", ephemeral=ctx.interaction is not None)
        current_mode, current_channel, current_role, current_cooldown, current_enabled = current
        channel_id = None if clear_restrictions else (channel.id if channel else current_channel)
        role_id = None if clear_restrictions else (role.id if role else current_role)
        async with self.bot.db.execute(
            "UPDATE autoresponders SET mode=?, channel_id=?, role_id=?, cooldown=?, enabled=?, "
            "updated_by=?, updated_at=? WHERE guild_id=? AND trigger=?",
            (
                mode or current_mode,
                channel_id,
                role_id,
                current_cooldown if cooldown is None else cooldown,
                current_enabled if enabled is None else int(enabled),
                ctx.author.id,
                utc_now().isoformat(),
                ctx.guild.id,
                trigger,
            ),
        ) as cursor:
            updated = cursor.rowcount
        await self.bot.db.commit()
        if not updated:
            return await ctx.send(f"No autoresponder exists for `{trigger}`.", ephemeral=ctx.interaction is not None)
        await ctx.send(f"Options updated for autoresponder `{trigger}`.", ephemeral=ctx.interaction is not None)

    @autoresponder_group.command(name="restrictions", description="Set multi-role and channel exclusions")
    @commands.has_guild_permissions(manage_guild=True)
    async def autoresponder_restrictions(
        self,
        ctx,
        trigger: str,
        roles: str = "",
        excluded_roles: str = "",
        excluded_channels: str = "",
        cooldown_scope: Literal["user", "channel", "global"] | None = None,
        case_sensitive: bool | None = None,
    ):
        trigger = trigger.strip().casefold()

        def resolve_ids(raw_value, resolver):
            ids = set()
            for token in raw_value.split():
                match = re.fullmatch(r"<@&?(\d+)>|<#(\d+)>|(\d{15,22})", token.strip(","))
                if not match:
                    raise ValueError(f"Invalid target: `{token}`.")
                target_id = int(next(group for group in match.groups() if group))
                target = resolver(target_id)
                if target is None:
                    raise ValueError(f"Target `{token}` is not part of this server.")
                ids.add(target_id)
            return sorted(ids)

        try:
            allowed_role_ids = resolve_ids(roles, ctx.guild.get_role)
            excluded_role_ids = resolve_ids(excluded_roles, ctx.guild.get_role)
            excluded_channel_ids = resolve_ids(
                excluded_channels,
                lambda channel_id: ctx.guild.get_channel(channel_id)
                if isinstance(ctx.guild.get_channel(channel_id), (discord.TextChannel, discord.Thread))
                else None,
            )
        except (ValueError, StopIteration) as error:
            return await ctx.send(str(error), ephemeral=ctx.interaction is not None)
        if cooldown_scope not in {None, "user", "channel", "global"}:
            return await ctx.send("Cooldown scope must be user, channel, or global.", ephemeral=ctx.interaction is not None)
        async with self.bot.db.execute(
            "UPDATE autoresponders SET allowed_role_ids=?, excluded_role_ids=?, excluded_channel_ids=?, "
            "cooldown_scope=COALESCE(?, cooldown_scope), case_sensitive=COALESCE(?, case_sensitive), "
            "updated_by=?, updated_at=? WHERE guild_id=? AND trigger=?",
            (
                json.dumps(allowed_role_ids),
                json.dumps(excluded_role_ids),
                json.dumps(excluded_channel_ids),
                cooldown_scope,
                int(case_sensitive) if case_sensitive is not None else None,
                ctx.author.id,
                utc_now().isoformat(),
                ctx.guild.id,
                trigger,
            ),
        ) as cursor:
            updated = cursor.rowcount
        await self.bot.db.commit()
        if not updated:
            return await ctx.send(f"No autoresponder exists for `{trigger}`.", ephemeral=ctx.interaction is not None)
        await ctx.send(f"Restrictions updated for autoresponder `{trigger}`.", ephemeral=ctx.interaction is not None)

    @autoresponder_group.command(name="test", description="Preview the response for a configured trigger")
    @commands.has_guild_permissions(manage_guild=True)
    async def autoresponder_test(self, ctx, trigger: str):
        async with self.bot.db.execute(
            "SELECT response FROM autoresponders WHERE guild_id=? AND trigger=?",
            (ctx.guild.id, trigger.strip().casefold()),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return await ctx.send(f"No autoresponder exists for `{trigger}`.", ephemeral=ctx.interaction is not None)
        await self._send_automated_response(
            ctx.message,
            row[0],
            allow_delete=False,
            channel=ctx.channel,
            user=ctx.author,
            guild=ctx.guild,
            extra_values={
                "message": f"Preview for {trigger}",
                "message.content": f"Preview for {trigger}",
            },
        )

    @commands.hybrid_group(name="customcommand", aliases=["customcmd"], invoke_without_command=True, case_insensitive=True)
    async def customcommand_group(self, ctx):
        await ctx.send(
            "Use `/customcommand add`, `/customcommand remove`, or `/customcommand list`.",
            ephemeral=ctx.interaction is not None,
        )

    @customcommand_group.command(name="add", description="Create a per-server prefix command")
    @commands.has_guild_permissions(manage_guild=True)
    async def customcommand_add(
        self,
        ctx,
        name: str,
        response: str,
        cooldown: int = 0,
        role: discord.Role = None,
        channel: discord.TextChannel = None,
    ):
        name = name.strip().casefold()
        response = response.strip()
        if not CUSTOM_COMMAND_PATTERN.fullmatch(name):
            return await ctx.send("Names must start with a letter and contain only letters, numbers, `_` or `-` (max 32).", ephemeral=ctx.interaction is not None)
        if self.bot.get_command(name):
            return await ctx.send("That name is already used by a built-in command.", ephemeral=ctx.interaction is not None)
        if not response or len(response) > 2000:
            return await ctx.send("Responses must contain 1 to 2000 characters.", ephemeral=ctx.interaction is not None)
        if not 0 <= cooldown <= 86400:
            return await ctx.send("Cooldown must be between 0 and 86400 seconds.", ephemeral=ctx.interaction is not None)
        async with self.bot.db.execute(
            "INSERT INTO custom_commands "
            "(guild_id, name, response, role_id, channel_id, cooldown, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(guild_id, name) DO NOTHING",
            (
                ctx.guild.id,
                name,
                response,
                role.id if role else None,
                channel.id if channel else None,
                cooldown,
                ctx.author.id,
                utc_now().isoformat(),
            ),
        ) as cursor:
            inserted = cursor.rowcount
        await self.bot.db.commit()
        if not inserted:
            return await ctx.send(f"Custom command `{name}` already exists.", ephemeral=ctx.interaction is not None)
        await ctx.send(f"Custom command `{name}` created.", ephemeral=ctx.interaction is not None)

    @customcommand_group.command(name="create", description="Create a per-server prefix command")
    @commands.has_guild_permissions(manage_guild=True)
    async def customcommand_create(
        self,
        ctx,
        name: str,
        response: str,
        cooldown: int = 0,
        role: discord.Role = None,
        channel: discord.TextChannel = None,
    ):
        await self.customcommand_add.callback(self, ctx, name, response, cooldown, role, channel)

    @customcommand_group.command(name="edit", description="Edit a per-server prefix command")
    @commands.has_guild_permissions(manage_guild=True)
    async def customcommand_edit(self, ctx, name: str, response: str):
        name = name.strip().casefold()
        response = response.strip()
        if not response or len(response) > 2000:
            return await ctx.send("Responses must contain 1 to 2000 characters.", ephemeral=ctx.interaction is not None)
        async with self.bot.db.execute(
            "UPDATE custom_commands SET response=?, updated_by=?, updated_at=? WHERE guild_id=? AND name=?",
            (response, ctx.author.id, utc_now().isoformat(), ctx.guild.id, name),
        ) as cursor:
            updated = cursor.rowcount
        await self.bot.db.commit()
        if not updated:
            return await ctx.send(f"Custom command `{name}` was not found.", ephemeral=ctx.interaction is not None)
        await ctx.send(f"Custom command `{name}` updated.", ephemeral=ctx.interaction is not None)

    async def _set_custom_command_enabled(self, ctx, name, enabled):
        name = name.strip().casefold()
        async with self.bot.db.execute(
            "UPDATE custom_commands SET enabled=?, updated_by=?, updated_at=? WHERE guild_id=? AND name=?",
            (int(enabled), ctx.author.id, utc_now().isoformat(), ctx.guild.id, name),
        ) as cursor:
            updated = cursor.rowcount
        await self.bot.db.commit()
        if not updated:
            return await ctx.send(f"Custom command `{name}` was not found.", ephemeral=ctx.interaction is not None)
        await ctx.send(
            f"Custom command `{name}` {'enabled' if enabled else 'disabled'}.",
            ephemeral=ctx.interaction is not None,
        )

    @customcommand_group.command(name="enable", description="Enable a per-server prefix command")
    @commands.has_guild_permissions(manage_guild=True)
    async def customcommand_enable(self, ctx, name: str):
        await self._set_custom_command_enabled(ctx, name, True)

    @customcommand_group.command(name="disable", description="Disable a per-server prefix command")
    @commands.has_guild_permissions(manage_guild=True)
    async def customcommand_disable(self, ctx, name: str):
        await self._set_custom_command_enabled(ctx, name, False)

    @customcommand_group.command(name="show", description="Show a per-server prefix command")
    async def customcommand_show(self, ctx, name: str):
        name = name.strip().casefold()
        async with self.bot.db.execute(
            "SELECT response, role_id, channel_id, cooldown, enabled FROM custom_commands "
            "WHERE guild_id=? AND name=?",
            (ctx.guild.id, name),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return await ctx.send(f"Custom command `{name}` was not found.", ephemeral=ctx.interaction is not None)
        response, role_id, channel_id, cooldown, enabled = row
        role = ctx.guild.get_role(role_id) if role_id else None
        channel = ctx.guild.get_channel(channel_id) if channel_id else None
        embed = discord.Embed(title=f"Custom command: {name}", color=0xFFDD00)
        embed.description = response[:3500]
        embed.add_field(name="Status", value="Enabled" if enabled else "Disabled", inline=True)
        embed.add_field(name="Cooldown", value=f"{cooldown}s", inline=True)
        embed.add_field(name="Role", value=role.mention if role else "Everyone", inline=True)
        embed.add_field(name="Channel", value=channel.mention if channel else "All channels", inline=True)
        await ctx.send(embed=embed, ephemeral=ctx.interaction is not None)

    @customcommand_group.command(name="remove", description="Remove a per-server prefix command")
    @commands.has_guild_permissions(manage_guild=True)
    async def customcommand_remove(self, ctx, name: str):
        name = name.strip().casefold()
        async with self.bot.db.execute(
            "SELECT 1 FROM custom_commands WHERE guild_id=? AND name=?",
            (ctx.guild.id, name),
        ) as cursor:
            exists = await cursor.fetchone()
        if not exists:
            return await ctx.send(f"Custom command `{name}` was not found.", ephemeral=ctx.interaction is not None)

        async def delete_custom_command():
            await self.bot.db.execute(
                "DELETE FROM custom_commands WHERE guild_id=? AND name=?",
                (ctx.guild.id, name),
            )
            await self.bot.db.commit()

        await self._confirm_destructive_action(
            ctx,
            f"Remove custom command `{name}`?",
            f"Custom command `{name}` removed.",
            delete_custom_command,
        )

    @customcommand_group.command(name="delete", description="Delete a per-server prefix command")
    @commands.has_guild_permissions(manage_guild=True)
    async def customcommand_delete(self, ctx, name: str):
        await self.customcommand_remove.callback(self, ctx, name)

    @customcommand_group.command(name="list", description="List this server's custom prefix commands")
    async def customcommand_list(self, ctx):
        async with self.bot.db.execute(
            "SELECT name, role_id, channel_id, cooldown, enabled FROM custom_commands WHERE guild_id=? ORDER BY name",
            (ctx.guild.id,),
        ) as cursor:
            rows = await cursor.fetchall()
        if not rows:
            return await ctx.send("No custom commands are configured.", ephemeral=ctx.interaction is not None)
        lines = []
        for name, role_id, channel_id, cooldown, enabled in rows:
            restrictions = [f"{cooldown}s cooldown"]
            restrictions.append("enabled" if enabled else "disabled")
            if role_id:
                role = ctx.guild.get_role(role_id)
                restrictions.append(role.mention if role else f"missing role `{role_id}`")
            if channel_id:
                channel = ctx.guild.get_channel(channel_id)
                restrictions.append(channel.mention if channel else f"missing channel `{channel_id}`")
            lines.append(f"`{name}` — {', '.join(restrictions)}")
        await self._send_paged_list(ctx, "Custom Commands", lines)

    @customcommand_group.command(name="test", description="Preview a custom command response")
    @commands.has_guild_permissions(manage_guild=True)
    async def customcommand_test(self, ctx, name: str, *, args: str = ""):
        async with self.bot.db.execute(
            "SELECT response FROM custom_commands WHERE guild_id=? AND name=?",
            (ctx.guild.id, name.strip().casefold()),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return await ctx.send(f"Custom command `{name}` was not found.", ephemeral=ctx.interaction is not None)
        await self._send_automated_response(
            ctx.message,
            row[0],
            allow_delete=False,
            channel=ctx.channel,
            user=ctx.author,
            guild=ctx.guild,
            extra_values={"args": args},
        )

    @commands.hybrid_group(name="boost", invoke_without_command=True, case_insensitive=True)
    async def boost_group(self, ctx):
        await ctx.send(
            "Boost replies are managed with `/events set`, `/events channel`, and `/events test`.",
            ephemeral=ctx.interaction is not None,
        )

    async def save_boost_setting(self, guild_id, channel_id, message, user_id, embed_name=None):
        await self.bot.db.execute(
            "INSERT INTO boost_settings (guild_id, channel_id, message, embed_name, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(guild_id) DO UPDATE SET "
            "channel_id=excluded.channel_id, message=excluded.message, embed_name=excluded.embed_name, "
            "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
            (guild_id, channel_id, message, embed_name, user_id, utc_now().isoformat()),
        )
        await self.bot.db.commit()

    @boost_group.command(name="setup", description="Set up boost notifications in a channel")
    @commands.has_guild_permissions(manage_guild=True)
    async def boost_setup(self, ctx, channel: discord.TextChannel):
        async with self.bot.db.execute(
            "SELECT message FROM boost_settings WHERE guild_id=?", (ctx.guild.id,)
        ) as cursor:
            row = await cursor.fetchone()
        await self.save_boost_setting(
            ctx.guild.id,
            channel.id,
            row[0] if row else DEFAULT_BOOST_MESSAGE,
            ctx.author.id,
        )
        await ctx.send(f"Boost notifications are set up in {channel.mention}.", ephemeral=ctx.interaction is not None)

    @boost_group.command(name="set", description="Set the server boost message and channel")
    @commands.has_guild_permissions(manage_guild=True)
    async def boost_set(self, ctx, channel: discord.TextChannel, *, message: str):
        message = message.strip()
        if not message or len(message) > 2000:
            return await ctx.send("Boost messages must contain 1 to 2000 characters.", ephemeral=ctx.interaction is not None)
        await self.save_boost_setting(ctx.guild.id, channel.id, message, ctx.author.id)
        await ctx.send(f"Boost messages will be sent to {channel.mention}.", ephemeral=ctx.interaction is not None)

    async def _configure_boost_channel(self, ctx, channel):
        async with self.bot.db.execute(
            "SELECT message, embed_name FROM boost_settings WHERE guild_id=?", (ctx.guild.id,)
        ) as cursor:
            row = await cursor.fetchone()
        await self.save_boost_setting(
            ctx.guild.id,
            channel.id,
            row[0] if row else DEFAULT_BOOST_MESSAGE,
            ctx.author.id,
            row[1] if row else None,
        )
        await ctx.send(f"Boost messages will be sent to {channel.mention}.", ephemeral=ctx.interaction is not None)

    @boost_group.command(name="channel", description="Change the server boost message channel")
    @commands.has_guild_permissions(manage_guild=True)
    async def boost_channel(self, ctx, channel: discord.TextChannel):
        await self._configure_boost_channel(ctx, channel)

    async def _configure_boost_message(self, ctx, message):
        message = message.strip()
        if not message or len(message) > 2000:
            return await ctx.send("Boost messages must contain 1 to 2000 characters.", ephemeral=ctx.interaction is not None)
        async with self.bot.db.execute(
            "SELECT channel_id, embed_name FROM boost_settings WHERE guild_id=?", (ctx.guild.id,)
        ) as cursor:
            row = await cursor.fetchone()
        channel_id = row[0] if row else (ctx.guild.system_channel or ctx.channel).id
        has_embed_action = any(
            action.partition(":")[0].strip().casefold() == "embed"
            for action in AUTORESPONDER_ACTION_PATTERN.findall(message)
        )
        await self.save_boost_setting(
            ctx.guild.id,
            channel_id,
            message,
            ctx.author.id,
            None if has_embed_action else (row[1] if row else None),
        )
        await ctx.send(
            "Boost message updated. Use `{embed:name}` in the message to attach a saved embed.",
            ephemeral=ctx.interaction is not None,
        )

    @boost_group.command(name="message", description="Change the server boost message")
    @commands.has_guild_permissions(manage_guild=True)
    async def boost_message(self, ctx, *, message: str):
        await self._configure_boost_message(ctx, message)

    @staticmethod
    def _boost_response_template(message, legacy_embed_name=None):
        has_embed_action = any(
            action.partition(":")[0].strip().casefold() == "embed"
            for action in AUTORESPONDER_ACTION_PATTERN.findall(message)
        )
        if legacy_embed_name and not has_embed_action:
            return f"{message} {{embed:{legacy_embed_name}}}"
        return message

    @commands.has_guild_permissions(manage_guild=True)
    async def boost_design(self, ctx, name: str):
        name = name.strip().casefold()
        if not name or len(name) > 50:
            return await ctx.send("Embed names must contain 1 to 50 characters.", ephemeral=ctx.interaction is not None)
        if ctx.interaction is None:
            return await ctx.send("Set the boost message with `/boost message` and attach a saved embed using `{embed:name}`.")

        async with self.bot.db.execute(
            "SELECT channel_id, message FROM boost_settings WHERE guild_id=?",
            (ctx.guild.id,),
        ) as cursor:
            settings = await cursor.fetchone()
        channel = ctx.guild.get_channel(settings[0]) if settings else None
        channel = channel or ctx.guild.system_channel or ctx.channel
        message = settings[1] if settings else DEFAULT_BOOST_MESSAGE
        async with self.bot.db.execute(
            "SELECT config FROM custom_embeds WHERE guild_id=? AND name=?",
            (ctx.guild.id, name),
        ) as cursor:
            saved = await cursor.fetchone()
        config = json.loads(saved[0]) if saved else DEFAULT_EMBED_CONFIG
        boost_count = getattr(ctx.guild, "premium_subscription_count", 0) or 0
        boost_level = getattr(ctx.guild, "premium_tier", 0) or 0
        extra_values = {
            "booster": ctx.author.mention,
            "boost.user": ctx.author.mention,
            "boost.count": boost_count,
            "boost_count": boost_count,
            "boost.level": boost_level,
        }

        async def save_boost_embed(updated_config):
            await self.save_embed(ctx.guild.id, name, updated_config, ctx.author.id)
            await self.save_boost_setting(ctx.guild.id, channel.id, message, ctx.author.id, name)

        await self._open_embed_editor(
            ctx.interaction,
            f"boost: {name}",
            config,
            owner_id=ctx.author.id,
            save_callback=save_boost_embed,
            extra_values=extra_values,
        )

    @boost_group.command(name="card", description="Enable, disable, or test booster cards")
    @commands.has_guild_permissions(manage_guild=True)
    async def boost_card(self, ctx, action: Literal["enable", "disable", "test"]):
        if action == "test":
            guild = ctx.guild
            boost_count = getattr(guild, "premium_subscription_count", 0) or 0
            boost_level = getattr(guild, "premium_tier", 0) or 0
            await self._send_member_event_card_preview(
                ctx,
                "boost",
                f"PREVIEW  •  LEVEL {boost_level}  •  BOOST #{boost_count}",
                "Boost card preview",
            )
            return

        if action == "enable":
            await self.bot.db.execute(
                "INSERT INTO boost_settings "
                "(guild_id, channel_id, message, enabled, card_enabled, updated_by, updated_at) "
                "VALUES (?, 0, ?, 1, 1, ?, ?) ON CONFLICT(guild_id) DO NOTHING",
                (
                    ctx.guild.id,
                    DEFAULT_BOOST_MESSAGE,
                    ctx.author.id,
                    utc_now().isoformat(),
                ),
            )
        async with self.bot.db.execute(
            "UPDATE boost_settings SET card_enabled=?, updated_by=?, updated_at=? WHERE guild_id=?",
            (
                int(action == "enable"),
                ctx.author.id,
                utc_now().isoformat(),
                ctx.guild.id,
            ),
        ) as cursor:
            changed = cursor.rowcount
        await self.bot.db.commit()
        if not changed:
            return await ctx.send("Set up boost notifications first with `/boost setup`.", ephemeral=ctx.interaction is not None)
        if action == "enable":
            view = BoostCardSetupView(self, ctx.guild.id, ctx.author.id)
            return await send_v2(
                ctx,
                embed=await view.make_embed(ctx.guild),
                view=view,
                ephemeral=ctx.interaction is not None,
            )
        await ctx.send(
            f"Booster cards {action}d.",
            ephemeral=ctx.interaction is not None,
        )

    @boost_card.error
    async def boost_card_error(self, ctx, error):
        if isinstance(error, commands.BadLiteralArgument):
            return await ctx.send(
                "Choose one action: `enable`, `disable`, or `test` (not `enable/disable`)."
            )
        raise error

    @boost_group.command(name="show", description="Show the configured server boost message")
    async def boost_show(self, ctx):
        async with self.bot.db.execute(
            "SELECT channel_id, message, enabled, embed_name, card_enabled, card_channel_id "
            "FROM boost_settings WHERE guild_id=?", (ctx.guild.id,)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return await ctx.send("No boost message is configured.", ephemeral=ctx.interaction is not None)
        channel = ctx.guild.get_channel(row[0])
        card_channel = ctx.guild.get_channel(row[5] or row[0]) if row[5] or row[0] else None
        response = f"Message: {row[1]}"
        if row[3]:
            response += f"\nAttached embed: `{row[3]}`"
        await ctx.send(
            f"Status: {'Enabled' if row[2] else 'Disabled'}\n"
            f"Booster card: {'Enabled' if row[4] else 'Disabled'}\n"
            f"Booster card channel: {card_channel.mention if card_channel else 'Not set'}\n"
            f"Channel: {channel.mention if channel else 'Unavailable'}\n"
            f"{response}",
            ephemeral=ctx.interaction is not None,
        )

    @boost_group.command(name="config", description="Show boost notification settings")
    async def boost_config(self, ctx):
        await self.boost_show.callback(self, ctx)

    async def _set_boost_enabled(self, ctx, enabled):
        async with self.bot.db.execute(
            "UPDATE boost_settings SET enabled=?, updated_by=?, updated_at=? WHERE guild_id=?",
            (int(enabled), ctx.author.id, utc_now().isoformat(), ctx.guild.id),
        ) as cursor:
            changed = cursor.rowcount
        await self.bot.db.commit()
        if not changed:
            return await ctx.send("Set up a boost channel and message first with `/boost set`.", ephemeral=ctx.interaction is not None)
        await ctx.send(f"Boost notifications {'enabled' if enabled else 'disabled'}.", ephemeral=ctx.interaction is not None)

    @boost_group.command(name="enable", description="Enable boost notifications")
    @commands.has_guild_permissions(manage_guild=True)
    async def boost_enable(self, ctx):
        await self._set_boost_enabled(ctx, True)

    @boost_group.command(name="disable", description="Disable boost notifications")
    @commands.has_guild_permissions(manage_guild=True)
    async def boost_disable(self, ctx):
        await self._set_boost_enabled(ctx, False)

    @boost_group.command(name="reset", description="Remove the server boost message")
    @commands.has_guild_permissions(manage_guild=True)
    async def boost_reset(self, ctx):
        async def reset_boost():
            await self.bot.db.execute("DELETE FROM boost_settings WHERE guild_id=?", (ctx.guild.id,))
            await self.bot.db.commit()

        await self._confirm_destructive_action(
            ctx,
            "Remove this server's boost message configuration?",
            "Boost message configuration removed.",
            reset_boost,
        )

    @boost_group.command(name="test", description="Preview the configured boost message")
    async def boost_test(self, ctx):
        async with self.bot.db.execute(
            "SELECT channel_id, message, embed_name FROM boost_settings WHERE guild_id=?", (ctx.guild.id,)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return await ctx.send("No boost message is configured.", ephemeral=ctx.interaction is not None)
        channel = ctx.guild.get_channel(row[0])
        response = self._boost_response_template(row[1], row[2])
        embed_name = None
        for action in AUTORESPONDER_ACTION_PATTERN.findall(response):
            action_type, _, value = action.partition(":")
            if action_type.strip().casefold() == "embed":
                embed_name = value.strip().casefold()
        rendered = AUTORESPONDER_ACTION_PATTERN.sub("", response).strip()
        extra_values = {
            "booster": ctx.author.mention,
            "boost.user": ctx.author.mention,
            "boost.count": getattr(ctx.guild, "premium_subscription_count", 0) or 0,
            "boost.level": getattr(ctx.guild, "premium_tier", 0) or 0,
        }
        rendered = render_variables(
            rendered,
            ctx.author,
            ctx.guild,
            channel or ctx.channel,
            self.bot.user,
            extra_values,
        )
        if embed_name:
            async with self.bot.db.execute(
                "SELECT config FROM custom_embeds WHERE guild_id=? AND name=?",
                (ctx.guild.id, embed_name),
            ) as cursor:
                embed_row = await cursor.fetchone()
            if not embed_row:
                return await ctx.send(
                    f"Saved boost embed `{embed_name}` was not found.",
                    ephemeral=ctx.interaction is not None,
                )
            try:
                embed_config = json.loads(embed_row[0])
                preview = build_custom_embed(
                    embed_config,
                    ctx.author,
                    ctx.guild,
                    channel or ctx.channel,
                    self.bot.user,
                    extra_values,
                )
                view = build_custom_embed_view(
                    embed_config,
                    ctx.author,
                    ctx.guild,
                    channel or ctx.channel,
                    self.bot.user,
                    extra_values,
                )
            except (ValueError, TypeError, discord.DiscordException) as error:
                return await ctx.send(
                    f"Saved boost embed is invalid: {error}",
                    ephemeral=ctx.interaction is not None,
                )
            return await ctx.send(
                content=rendered or None,
                embed=preview,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
                ephemeral=ctx.interaction is not None,
            )
        await ctx.send(
            f"Boost message preview:\n{rendered}",
            ephemeral=ctx.interaction is not None,
        )

    @boost_group.command(name="preview", description="Preview the configured boost message")
    async def boost_preview(self, ctx):
        await self.boost_test(ctx)

    @boost_group.command(name="variables", description="List variables available in boost messages")
    async def boost_variables(self, ctx):
        await ctx.send(
            "Available: `{user}`, `{user.mention}`, `{user.name}`, `{user.id}`, `{server}`, "
            "`{server.name}`, `{server.id}`, `{server.member_count}`, `{boost_count}`, "
            "`{boost_level}`, `{booster}`, `{boost.user}`, `{boost.count}`, `{boost.level}`, "
            "`{channel.name}`, `{bot.name}`.",
            ephemeral=ctx.interaction is not None,
        )

    @commands.group(name="greet", invoke_without_command=True, case_insensitive=True)
    async def greet_group(self, ctx):
        await ctx.send("Join and leave replies are managed with `/events set` and `/events channel`.", ephemeral=ctx.interaction is not None)

    @greet_group.command(name="delete-after")
    @commands.has_guild_permissions(manage_guild=True)
    async def greet_delete_after(
        self,
        ctx,
        event: Literal["join", "leave", "both"],
        seconds: int,
    ):
        if not 0 <= seconds <= 86400:
            return await ctx.send(
                "Choose a delete delay from `0` to `86400` seconds (`0` disables auto-delete)."
            )
        events = ("join", "leave") if event == "both" else (event,)
        for event_name in events:
            column = f"{event_name}_seconds"
            await self.bot.db.execute(
                "INSERT INTO greeting_delete_settings (guild_id, "
                f"{column}) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET "
                f"{column}=excluded.{column}",
                (ctx.guild.id, seconds or None),
            )
        await self.bot.db.commit()
        delay_text = f"{seconds} seconds" if seconds else "disabled"
        await ctx.send(
            f"Automatic deletion for {event} greeting(s) {delay_text}.",
            ephemeral=ctx.interaction is not None,
        )

    async def _greeting_delete_after(self, guild_id, event):
        column = "join_seconds" if event == "join" else "leave_seconds"
        async with self.bot.db.execute(
            f"SELECT {column} FROM greeting_delete_settings WHERE guild_id=?",
            (guild_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row and row[0] else None

    async def save_greeting(self, guild_id, greeting_type, config, user_id):
        column = "join_config" if greeting_type == "join" else "leave_config"
        enabled_column = "join_enabled" if greeting_type == "join" else "leave_enabled"
        serialized_config = json.dumps(config, separators=(",", ":")) if config is not None else None
        await self.bot.db.execute(
            "INSERT INTO greeting_settings "
            "(guild_id, channel_id, updated_by, updated_at, join_config, leave_config, join_enabled, leave_enabled) "
            "VALUES (?, 0, ?, ?, ?, ?, 1, 1) ON CONFLICT(guild_id) DO UPDATE SET "
            f"{column}=excluded.{column}, {enabled_column}=1, "
            "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
            (
                guild_id,
                user_id,
                utc_now().isoformat(),
                serialized_config if greeting_type == "join" else None,
                serialized_config if greeting_type == "leave" else None,
            ),
        )
        await self.bot.db.commit()

    async def open_greeting_embed_editor(self, interaction, greeting_type):
        async with self.bot.db.execute(
            "SELECT join_config, leave_config FROM greeting_settings WHERE guild_id=?",
            (interaction.guild.id,),
        ) as cursor:
            row = await cursor.fetchone()
        config_text = row[0 if greeting_type == "join" else 1] if row else None
        try:
            config = json.loads(config_text) if config_text else DEFAULT_EMBED_CONFIG
        except (TypeError, json.JSONDecodeError):
            config = DEFAULT_EMBED_CONFIG

        async def save_greeting_config(updated_config):
            await self.save_greeting(
                interaction.guild.id,
                greeting_type,
                updated_config,
                interaction.user.id,
            )

        await self._open_embed_editor(
            interaction,
            f"{greeting_type} greeting",
            config,
            owner_id=interaction.user.id,
            save_callback=save_greeting_config,
        )

    @commands.has_guild_permissions(manage_guild=True)
    async def greet_setup(self, ctx, channel: discord.TextChannel):
        await self.bot.db.execute(
            "INSERT INTO greeting_settings (guild_id, channel_id, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(guild_id) DO UPDATE SET "
            "channel_id=excluded.channel_id, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
            (ctx.guild.id, channel.id, ctx.author.id, utc_now().isoformat()),
        )
        await self.bot.db.commit()
        view = GreetingConfigView(self, ctx.guild.id, ctx.author.id)
        await ctx.send(f"Greeting channel set to {channel.mention}. Configure the join and leave embeds below.", view=view, ephemeral=ctx.interaction is not None)

    @commands.has_guild_permissions(manage_guild=True)
    async def greet_embed(self, ctx, channel: discord.TextChannel | None = None):
        if channel is None:
            async with self.bot.db.execute(
                "SELECT channel_id FROM greeting_settings WHERE guild_id=?", (ctx.guild.id,)
            ) as cursor:
                row = await cursor.fetchone()
            channel = ctx.guild.get_channel(row[0]) if row else None
        if channel is None:
            return await ctx.send("Set an event channel first with `/events channel`.", ephemeral=ctx.interaction is not None)
        await self.greet_setup.callback(self, ctx, channel)

    @greet_group.command(name="card", description="Enable, disable, or test join/leave image cards")
    @commands.has_guild_permissions(manage_guild=True)
    async def greet_card(
        self,
        ctx,
        action: Literal["enable", "disable", "test"],
        event: Literal["join", "leave", "both"] = "both",
    ):
        events = ("join", "leave") if event == "both" else (event,)
        if action == "test":
            member_count = ctx.guild.member_count or 0
            for event_name in events:
                await self._send_member_event_card_preview(
                    ctx,
                    event_name,
                    f"PREVIEW  •  MEMBER #{member_count}",
                    f"{event_name.title()} greeting card preview",
                )
            return

        if action == "enable":
            await self.bot.db.execute(
                "INSERT INTO greeting_settings "
                "(guild_id, channel_id, updated_by, updated_at) VALUES (?, 0, ?, ?) "
                "ON CONFLICT(guild_id) DO NOTHING",
                (ctx.guild.id, ctx.author.id, utc_now().isoformat()),
            )
        for event_name in events:
            await self.bot.db.execute(
                f"UPDATE greeting_settings SET {event_name}_card_enabled=?, "
                f"{event_name}_enabled=?, updated_by=?, updated_at=? WHERE guild_id=?",
                (
                    int(action == "enable"),
                    int(action == "enable"),
                    ctx.author.id,
                    utc_now().isoformat(),
                    ctx.guild.id,
                ),
            )
        await self.bot.db.commit()
        if action == "enable":
            view = GreetingCardSetupView(self, ctx.guild.id, ctx.author.id)
            return await send_v2(
                ctx,
                embed=await view.make_embed(ctx.guild),
                view=view,
                ephemeral=ctx.interaction is not None,
            )
        await ctx.send(
            f"{event.title()} greeting card(s) {action}d.",
            ephemeral=ctx.interaction is not None,
        )

    @greet_card.error
    async def greet_card_error(self, ctx, error):
        if isinstance(error, commands.BadLiteralArgument):
            return await ctx.send(
                "Choose one action: `enable`, `disable`, or `test`."
            )
        raise error

    @greet_group.command(name="reset", description="Remove custom greeting configuration")
    @commands.has_guild_permissions(manage_guild=True)
    async def greet_reset(self, ctx):
        async def reset_greetings():
            await self.bot.db.execute("DELETE FROM greeting_settings WHERE guild_id=?", (ctx.guild.id,))
            await self.bot.db.execute("DELETE FROM greet_channels WHERE guild_id=?", (ctx.guild.id,))
            await self.bot.db.execute(
                "UPDATE guild_settings SET join_channel_id=NULL, leave_channel_id=NULL, "
                "join_message=NULL, leave_message=NULL, vanity_message=NULL WHERE guild_id=?",
                (ctx.guild.id,),
            )
            await self.bot.db.commit()

        await self._confirm_destructive_action(
            ctx,
            "Remove all join, leave, and vanity greeting configuration for this server?",
            "All greeting configuration removed.",
            reset_greetings,
        )

    @greet_group.command(name="channel", description="Set the text greeting channel for join or leave events")
    @commands.has_guild_permissions(manage_guild=True)
    async def greet_channel(self, ctx, event: str, channel: discord.TextChannel):
        event = event.casefold().strip()
        column = GREETING_CHANNEL_COLUMNS.get(event)
        if column is None:
            return await ctx.send("Event must be `join` or `leave`.", ephemeral=ctx.interaction is not None)
        await self.bot.db.execute(
            f"INSERT INTO guild_settings (guild_id, {column}, {event}_enabled) VALUES (?, ?, 1) "
            f"ON CONFLICT(guild_id) DO UPDATE SET {column}=excluded.{column}, {event}_enabled=1",
            (ctx.guild.id, channel.id),
        )
        await self.bot.db.commit()
        await ctx.send(f"{event.title()} messages will be sent to {channel.mention}.", ephemeral=ctx.interaction is not None)

    @greet_group.command(name="message", description="Set a text greeting message")
    @commands.has_guild_permissions(manage_guild=True)
    async def greet_message(self, ctx, event: str, *, message: str):
        event = event.casefold().strip()
        column = GREETING_MESSAGE_COLUMNS.get(event)
        if column is None:
            return await ctx.send("Event must be `join`, `leave`, or `vanity`.", ephemeral=ctx.interaction is not None)
        enabled_event = "join" if event == "vanity" else event
        message = message.strip()
        if not message or len(message) > 2000:
            return await ctx.send("Greeting messages must contain 1 to 2000 characters.", ephemeral=ctx.interaction is not None)
        await self.bot.db.execute(
            f"INSERT INTO guild_settings (guild_id, {column}, {enabled_event}_enabled) VALUES (?, ?, 1) "
            f"ON CONFLICT(guild_id) DO UPDATE SET {column}=excluded.{column}, {enabled_event}_enabled=1",
            (ctx.guild.id, message),
        )
        await self.bot.db.commit()
        await ctx.send(f"{event.title()} message updated.", ephemeral=ctx.interaction is not None)

    @greet_group.command(name="clear", description="Clear a text greeting message")
    @commands.has_guild_permissions(manage_guild=True)
    async def greet_clear(self, ctx, event: str):
        event = event.casefold().strip()
        column = GREETING_MESSAGE_COLUMNS.get(event)
        if column is None:
            return await ctx.send("Event must be `join`, `leave`, or `vanity`.", ephemeral=ctx.interaction is not None)
        await self.bot.db.execute(
            f"UPDATE guild_settings SET {column}=NULL WHERE guild_id=?", (ctx.guild.id,)
        )
        await self.bot.db.commit()
        await ctx.send(f"{event.title()} message cleared.", ephemeral=ctx.interaction is not None)

    @greet_group.command(name="disable", description="Disable join, leave, or all greetings")
    @commands.has_guild_permissions(manage_guild=True)
    async def greet_disable(self, ctx, event: Literal["join", "leave", "both"] = "both"):
        events = ("join", "leave") if event == "both" else (event,)
        for item in events:
            await self.bot.db.execute(
                f"INSERT INTO guild_settings (guild_id, {item}_enabled) VALUES (?, 0) "
                f"ON CONFLICT(guild_id) DO UPDATE SET {item}_enabled=0",
                (ctx.guild.id,),
            )
            await self.bot.db.execute(
                f"UPDATE greeting_settings SET {item}_enabled=0 WHERE guild_id=?",
                (ctx.guild.id,),
            )
        await self.bot.db.commit()
        await ctx.send(f"{event.title()} greeting(s) disabled.", ephemeral=ctx.interaction is not None)

    @greet_group.command(name="enable", description="Enable join, leave, or all greetings")
    @commands.has_guild_permissions(manage_guild=True)
    async def greet_enable(self, ctx, event: Literal["join", "leave", "both"] = "both"):
        events = ("join", "leave") if event == "both" else (event,)
        for item in events:
            await self.bot.db.execute(
                f"INSERT INTO guild_settings (guild_id, {item}_enabled) VALUES (?, 1) "
                f"ON CONFLICT(guild_id) DO UPDATE SET {item}_enabled=1",
                (ctx.guild.id,),
            )
            await self.bot.db.execute(
                f"UPDATE greeting_settings SET {item}_enabled=1 WHERE guild_id=?",
                (ctx.guild.id,),
            )
        await self.bot.db.commit()
        await ctx.send(f"{event.title()} greeting(s) enabled.", ephemeral=ctx.interaction is not None)

    @greet_group.command(name="test", description="Preview a text greeting message")
    async def greet_test(self, ctx, event: str):
        event = event.casefold().strip()
        if event not in GREETING_MESSAGE_COLUMNS:
            return await ctx.send("Event must be `join`, `leave`, or `vanity`.", ephemeral=ctx.interaction is not None)
        async with self.bot.db.execute(
            "SELECT join_message, leave_message, vanity_message FROM guild_settings WHERE guild_id=?",
            (ctx.guild.id,),
        ) as cursor:
            row = await cursor.fetchone()
        columns = {"join": 0, "leave": 1, "vanity": 2}
        message = row[columns[event]] if row else None
        if event == "vanity" and not message and row:
            message = row[0]
        if not message:
            async with self.bot.db.execute(
                "SELECT channel_id, join_config, leave_config FROM greeting_settings WHERE guild_id=?",
                (ctx.guild.id,),
            ) as cursor:
                embed_row = await cursor.fetchone()
            config_text = embed_row[2] if event == "leave" and embed_row else (embed_row[1] if embed_row else None)
            if not config_text:
                return await ctx.send(f"No {event} message or embed is configured.", ephemeral=ctx.interaction is not None)
            channel = ctx.guild.get_channel(embed_row[0]) or ctx.channel
            try:
                preview_embed = build_custom_embed(
                    json.loads(config_text),
                    ctx.author,
                    ctx.guild,
                    channel,
                    self.bot.user,
                    {"inviter": "Unknown", "inviter.name": "Unknown", "inviter.id": ""},
                )
            except (ValueError, TypeError, discord.DiscordException) as error:
                return await ctx.send(f"Saved {event} embed is invalid: {error}", ephemeral=ctx.interaction is not None)
            return await ctx.send(embed=preview_embed, allowed_mentions=discord.AllowedMentions.none())
        preview = render_variables(
            message,
            ctx.author,
            ctx.guild,
            ctx.channel,
            self.bot.user,
            {"inviter": "Unknown", "inviter.name": "Unknown", "inviter.id": ""},
        )
        await ctx.send(preview[:2000], ephemeral=ctx.interaction is not None, allowed_mentions=discord.AllowedMentions.none())

    @greet_group.command(name="variables", description="List variables available in greetings")
    async def greet_variables(self, ctx):
        await ctx.send(
            "Available: `{user}`, `{user.mention}`, `{user.name}`, `{user.id}`, `{user.avatar}`, "
            "`{server}`, `{server.name}`, `{server.id}`, `{server.member_count}`, `{channel.name}`, "
            "`{inviter}`, `{inviter.name}`, `{boost_count}`.",
            ephemeral=ctx.interaction is not None,
        )

    @greet_group.command(name="show", description="Show this server's greeting configuration")
    async def greet_show(self, ctx):
        async with self.bot.db.execute(
            "SELECT channel_id, join_config, leave_config, join_enabled, leave_enabled, "
            "join_card_enabled, leave_card_enabled, join_card_channel_id, leave_card_channel_id "
            "FROM greeting_settings WHERE guild_id=?",
            (ctx.guild.id,),
        ) as cursor:
            row = await cursor.fetchone()
        async with self.bot.db.execute(
            "SELECT join_channel_id, leave_channel_id, join_message, leave_message, vanity_message "
            "FROM guild_settings WHERE guild_id=?",
            (ctx.guild.id,),
        ) as cursor:
            text_settings = await cursor.fetchone()
        async with self.bot.db.execute(
            "SELECT channel_id, greet_type FROM greet_channels WHERE guild_id=? ORDER BY channel_id",
            (ctx.guild.id,),
        ) as cursor:
            channel_settings = await cursor.fetchall()
        if not row and not text_settings and not channel_settings:
            return await ctx.send("No custom greetings are configured.", ephemeral=ctx.interaction is not None)
        embed = discord.Embed(title="Greeting Configuration", color=0xFFDD00)
        if row:
            channel = ctx.guild.get_channel(row[0])
            embed.add_field(name="Embed channel", value=channel.mention if channel else "Unavailable", inline=False)
            join_card_channel = ctx.guild.get_channel(row[7] or row[0]) if row[7] or row[0] else None
            leave_card_channel = ctx.guild.get_channel(row[8] or row[0]) if row[8] or row[0] else None
            embed.add_field(
                name="Shared card channel",
                value=channel.mention if channel else "Not set",
                inline=False,
            )
            embed.add_field(
                name="Join card channel",
                value=join_card_channel.mention if join_card_channel else "Not set",
                inline=True,
            )
            embed.add_field(
                name="Leave card channel",
                value=leave_card_channel.mention if leave_card_channel else "Not set",
                inline=True,
            )
            embed.add_field(name="Join embed", value="Configured" if row[1] else "Not configured", inline=True)
            embed.add_field(name="Leave embed", value="Configured" if row[2] else "Not configured", inline=True)
            embed.add_field(name="Join card", value="Enabled" if row[5] else "Disabled", inline=True)
            embed.add_field(name="Leave card", value="Enabled" if row[6] else "Disabled", inline=True)
        if text_settings:
            join_channel = ctx.guild.get_channel(text_settings[0]) if text_settings[0] else None
            leave_channel = ctx.guild.get_channel(text_settings[1]) if text_settings[1] else None
            embed.add_field(
                name="Text greetings",
                value=(
                    f"Join: {join_channel.mention if join_channel else 'Off'} | {'set' if text_settings[2] else 'default'}\n"
                    f"Leave: {leave_channel.mention if leave_channel else 'Off'} | {'set' if text_settings[3] else 'default'}\n"
                    f"Vanity message: {'set' if text_settings[4] else 'Off'}"
                ),
                inline=False,
            )
        if channel_settings:
            lines = []
            for channel_id, greet_type in channel_settings:
                channel = ctx.guild.get_channel(channel_id)
                lines.append(f"{channel.mention if channel else f'Unavailable ({channel_id})'}: {greet_type}")
            embed.add_field(name="Additional greet channels", value="\n".join(lines), inline=False)
        await ctx.send(embed=embed, ephemeral=ctx.interaction is not None)

    @greet_group.command(name="preview", description="Preview the configured join, leave, or vanity greeting")
    async def greet_preview(self, ctx, event: str = "join"):
        await self.greet_test.callback(self, ctx, event)

    @greet_group.command(name="config", description="Show this server's greeting configuration")
    async def greet_config(self, ctx):
        await self.greet_show.callback(self, ctx)

    @commands.hybrid_group(name="leave", invoke_without_command=True, case_insensitive=True)
    async def leave_group(self, ctx):
        await ctx.send(
            "Leave event replies are managed with `/events set`, `/events channel`, and `/events test`.",
            ephemeral=ctx.interaction is not None,
        )

    @commands.has_guild_permissions(manage_guild=True)
    async def leave_setup(self, ctx, channel: discord.TextChannel):
        await self.bot.db.execute(
            "INSERT INTO guild_settings (guild_id, leave_channel_id, leave_enabled) VALUES (?, ?, 1) "
            "ON CONFLICT(guild_id) DO UPDATE SET leave_channel_id=excluded.leave_channel_id, leave_enabled=1",
            (ctx.guild.id, channel.id),
        )
        await self.bot.db.commit()
        await self.bot.db.execute(
            "INSERT INTO greeting_settings "
            "(guild_id, channel_id, updated_by, updated_at, join_enabled, leave_enabled) "
            "VALUES (?, ?, ?, ?, 1, 1) ON CONFLICT(guild_id) DO UPDATE SET "
            "channel_id=excluded.channel_id, leave_enabled=1, updated_by=excluded.updated_by, "
            "updated_at=excluded.updated_at",
            (ctx.guild.id, channel.id, ctx.author.id, utc_now().isoformat()),
        )
        await self.bot.db.commit()
        view = GreetingConfigView(self, ctx.guild.id, ctx.author.id)
        await ctx.send(
            f"Leave channel set to {channel.mention}. Configure the leave embed below.",
            view=view,
            ephemeral=ctx.interaction is not None,
        )

    @leave_group.command(name="enable", description="Enable leave notifications")
    @commands.has_guild_permissions(manage_guild=True)
    async def leave_enable(self, ctx):
        async with self.bot.db.execute(
            "SELECT leave_channel_id FROM guild_settings WHERE guild_id=?", (ctx.guild.id,)
        ) as cursor:
            row = await cursor.fetchone()
        if not row or not row[0]:
            return await ctx.send("Set the leave event channel first with `/events channel`.", ephemeral=ctx.interaction is not None)
        await self.bot.db.execute(
            "UPDATE guild_settings SET leave_enabled=1 WHERE guild_id=?", (ctx.guild.id,)
        )
        await self.bot.db.execute(
            "UPDATE greeting_settings SET leave_enabled=1 WHERE guild_id=?", (ctx.guild.id,)
        )
        await self.bot.db.commit()
        await ctx.send("Leave notifications enabled.", ephemeral=ctx.interaction is not None)

    @leave_group.command(name="disable", description="Disable leave notifications")
    @commands.has_guild_permissions(manage_guild=True)
    async def leave_disable(self, ctx):
        await self.bot.db.execute(
            "INSERT INTO guild_settings (guild_id, leave_enabled) VALUES (?, 0) "
            "ON CONFLICT(guild_id) DO UPDATE SET leave_enabled=0",
            (ctx.guild.id,),
        )
        await self.bot.db.execute(
            "UPDATE greeting_settings SET leave_enabled=0 WHERE guild_id=?", (ctx.guild.id,)
        )
        await self.bot.db.commit()
        await ctx.send("Leave notifications disabled.", ephemeral=ctx.interaction is not None)

    @leave_group.command(name="channel", description="Set the leave notification channel")
    @commands.has_guild_permissions(manage_guild=True)
    async def leave_channel(self, ctx, channel: discord.TextChannel):
        await self.bot.db.execute(
            "INSERT INTO guild_settings (guild_id, leave_channel_id, leave_enabled) VALUES (?, ?, 1) "
            "ON CONFLICT(guild_id) DO UPDATE SET leave_channel_id=excluded.leave_channel_id, leave_enabled=1",
            (ctx.guild.id, channel.id),
        )
        await self.bot.db.execute(
            "INSERT INTO greeting_settings "
            "(guild_id, channel_id, updated_by, updated_at, leave_enabled) VALUES (?, ?, ?, ?, 1) "
            "ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id, leave_enabled=1, "
            "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
            (ctx.guild.id, channel.id, ctx.author.id, utc_now().isoformat()),
        )
        await self.bot.db.commit()
        await ctx.send(f"Leave notifications will be sent to {channel.mention}.", ephemeral=ctx.interaction is not None)

    @leave_group.command(name="message", description="Set the leave text message")
    @commands.has_guild_permissions(manage_guild=True)
    async def leave_message(self, ctx, *, message: str):
        message = message.strip()
        if not message or len(message) > 2000:
            return await ctx.send("Leave messages must contain 1 to 2000 characters.", ephemeral=ctx.interaction is not None)
        await self.bot.db.execute(
            "INSERT INTO guild_settings (guild_id, leave_message, leave_enabled) VALUES (?, ?, 1) "
            "ON CONFLICT(guild_id) DO UPDATE SET leave_message=excluded.leave_message, leave_enabled=1",
            (ctx.guild.id, message),
        )
        await self.bot.db.commit()
        await ctx.send("Leave message updated.", ephemeral=ctx.interaction is not None)

    @commands.has_guild_permissions(manage_guild=True)
    async def leave_embed(self, ctx, channel: discord.TextChannel = None):
        channel = channel or ctx.guild.get_channel(
            await self._guild_leave_channel_id(ctx.guild.id)
        ) or ctx.channel
        await self.leave_setup(ctx, channel)

    async def _guild_leave_channel_id(self, guild_id):
        async with self.bot.db.execute(
            "SELECT leave_channel_id FROM guild_settings WHERE guild_id=?", (guild_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else None

    @leave_group.command(name="test", description="Preview the leave notification")
    async def leave_test(self, ctx):
        await self._send_leave_preview(ctx, deliver=False)

    @leave_group.command(name="preview", description="Preview the configured leave message or embed")
    async def leave_preview(self, ctx):
        await self._send_leave_preview(ctx, deliver=False)

    async def _send_leave_preview(self, ctx, *, deliver):
        async with self.bot.db.execute(
            "SELECT leave_message FROM guild_settings WHERE guild_id=?", (ctx.guild.id,)
        ) as cursor:
            text_row = await cursor.fetchone()
        if text_row and text_row[0]:
            preview = render_variables(
                text_row[0], ctx.author, ctx.guild, ctx.channel, self.bot.user
            )
            await ctx.send(preview[:2000], allowed_mentions=discord.AllowedMentions.none())
            return
        async with self.bot.db.execute(
            "SELECT leave_config FROM greeting_settings WHERE guild_id=?", (ctx.guild.id,)
        ) as cursor:
            embed_row = await cursor.fetchone()
        if embed_row and embed_row[0]:
            try:
                embed = build_custom_embed(
                    json.loads(embed_row[0]), ctx.author, ctx.guild, ctx.channel, self.bot.user
                )
            except (ValueError, TypeError, discord.DiscordException) as error:
                return await ctx.send(f"Saved leave embed is invalid: {error}")
            await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            return
        await ctx.send("No leave message or embed is configured. Use `/events set` and `/events channel`.")

    @leave_group.command(name="config", description="Show leave notification settings")
    async def leave_config(self, ctx):
        async with self.bot.db.execute(
            "SELECT leave_channel_id, leave_message, leave_enabled FROM guild_settings WHERE guild_id=?",
            (ctx.guild.id,),
        ) as cursor:
            text_config = await cursor.fetchone()
        async with self.bot.db.execute(
            "SELECT channel_id, leave_config, leave_enabled FROM greeting_settings WHERE guild_id=?",
            (ctx.guild.id,),
        ) as cursor:
            embed_config = await cursor.fetchone()
        channel_id = text_config[0] if text_config and text_config[0] else (embed_config[0] if embed_config else None)
        channel = ctx.guild.get_channel(channel_id) if channel_id else None
        enabled = bool(text_config and text_config[2]) or bool(embed_config and embed_config[2])
        embed = discord.Embed(title="Leave Configuration", color=0xFFDD00)
        embed.add_field(name="Status", value="Enabled" if enabled else "Disabled", inline=True)
        embed.add_field(name="Channel", value=channel.mention if channel else "Not configured", inline=True)
        embed.add_field(name="Text", value="Configured" if text_config and text_config[1] else "Not configured", inline=True)
        embed.add_field(name="Embed", value="Configured" if embed_config and embed_config[1] else "Not configured", inline=True)
        await ctx.send(embed=embed)

    @leave_group.command(name="reset", description="Reset leave notification configuration")
    @commands.has_guild_permissions(manage_guild=True)
    async def leave_reset(self, ctx):
        async def reset_leave():
            await self.bot.db.execute(
                "UPDATE guild_settings SET leave_channel_id=NULL, leave_message=NULL, leave_enabled=0 WHERE guild_id=?",
                (ctx.guild.id,),
            )
            await self.bot.db.execute(
                "UPDATE greeting_settings SET leave_config=NULL, leave_enabled=0 WHERE guild_id=?",
                (ctx.guild.id,),
            )
            await self.bot.db.commit()

        await self._confirm_destructive_action(
            ctx,
            "Reset all leave notification settings for this server?",
            "Leave notification settings reset.",
            reset_leave,
        )

    @commands.hybrid_group(name="config", invoke_without_command=True, case_insensitive=True)
    async def config_group(self, ctx):
        await self._send_feature_config(ctx)

    async def _send_feature_config(self, ctx, section=None):
        guild_id = ctx.guild.id
        embed = discord.Embed(title="Falcon Server Configuration", color=0xFFDD00)
        if section in (None, "greet", "leave"):
            async with self.bot.db.execute(
                "SELECT join_channel_id, leave_channel_id, join_enabled, leave_enabled "
                "FROM guild_settings WHERE guild_id=?",
                (guild_id,),
            ) as cursor:
                greeting = await cursor.fetchone()
            async with self.bot.db.execute(
                "SELECT join_config, leave_config FROM greeting_settings WHERE guild_id=?",
                (guild_id,),
            ) as cursor:
                greeting_embeds = await cursor.fetchone()
            if section in (None, "greet"):
                join_channel = ctx.guild.get_channel(greeting[0]) if greeting and greeting[0] else None
                embed.add_field(
                    name="Greet",
                    value=(
                        f"Status: {'Enabled' if not greeting or greeting[2] else 'Disabled'}\n"
                        f"Channel: {join_channel.mention if join_channel else 'Not configured'}\n"
                        f"Embed: {'Configured' if greeting_embeds and greeting_embeds[0] else 'Not configured'}"
                    ),
                    inline=True,
                )
            if section in (None, "leave"):
                leave_channel = ctx.guild.get_channel(greeting[1]) if greeting and greeting[1] else None
                embed.add_field(
                    name="Leave",
                    value=(
                        f"Status: {'Enabled' if not greeting or greeting[3] else 'Disabled'}\n"
                        f"Channel: {leave_channel.mention if leave_channel else 'Not configured'}\n"
                        f"Embed: {'Configured' if greeting_embeds and greeting_embeds[1] else 'Not configured'}"
                    ),
                    inline=True,
                )
        if section in (None, "boost"):
            async with self.bot.db.execute(
                "SELECT channel_id, enabled FROM boost_settings WHERE guild_id=?", (guild_id,)
            ) as cursor:
                boost = await cursor.fetchone()
            channel = ctx.guild.get_channel(boost[0]) if boost else None
            embed.add_field(
                name="Boost",
                value=f"Status: {'Enabled' if boost and boost[1] else 'Disabled'}\nChannel: {channel.mention if channel else 'Not configured'}",
                inline=True,
            )
        if section in (None, "autoresponder", "autoresponders"):
            async with self.bot.db.execute(
                "SELECT COUNT(*), COALESCE(SUM(enabled), 0) FROM autoresponders WHERE guild_id=?",
                (guild_id,),
            ) as cursor:
                total, enabled = await cursor.fetchone()
            embed.add_field(name="Autoresponders", value=f"{enabled} enabled / {total} total", inline=True)
        if section in (None, "customcommands", "customcommand"):
            async with self.bot.db.execute(
                "SELECT COUNT(*), COALESCE(SUM(enabled), 0) FROM custom_commands WHERE guild_id=?",
                (guild_id,),
            ) as cursor:
                total, enabled = await cursor.fetchone()
            embed.add_field(name="Custom Commands", value=f"{enabled} enabled / {total} total", inline=True)
        if section in (None, "embeds"):
            async with self.bot.db.execute(
                "SELECT COUNT(*) FROM custom_embeds WHERE guild_id=?", (guild_id,)
            ) as cursor:
                count = (await cursor.fetchone())[0]
            embed.add_field(name="Saved Embeds", value=str(count), inline=True)
        await ctx.send(embed=embed)

    @config_group.command(name="greet", description="Show greet configuration")
    async def config_greet(self, ctx):
        await self._send_feature_config(ctx, "greet")

    @config_group.command(name="leave", description="Show leave configuration")
    async def config_leave(self, ctx):
        await self._send_feature_config(ctx, "leave")

    @config_group.command(name="boost", description="Show boost configuration")
    async def config_boost(self, ctx):
        await self._send_feature_config(ctx, "boost")

    @config_group.command(name="autoresponder", description="Show autoresponder configuration")
    async def config_autoresponder(self, ctx):
        await self._send_feature_config(ctx, "autoresponder")

    @config_group.command(name="customcommands", description="Show custom command configuration")
    async def config_customcommands(self, ctx):
        await self._send_feature_config(ctx, "customcommands")

    @config_group.command(name="embeds", description="Show saved embed configuration")
    async def config_embeds(self, ctx):
        await self._send_feature_config(ctx, "embeds")

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.guild is None or message.author.bot or message.webhook_id:
            return
        if (await self.bot.get_context(message)).valid:
            return
        content = message.content.casefold()
        if not content:
            return

        prefixes = await self.bot.get_prefix(message)
        if isinstance(prefixes, str):
            prefixes = [prefixes]
        for prefix in sorted((prefix for prefix in prefixes if prefix), key=len, reverse=True):
            if not message.content.startswith(prefix):
                continue
            command_text = message.content[len(prefix):].strip()
            name = command_text.split(maxsplit=1)[0].casefold() if command_text else ""
            if name:
                async with self.bot.db.execute(
                    "SELECT response, role_id, channel_id, cooldown, enabled FROM custom_commands "
                    "WHERE guild_id=? AND name=?",
                    (message.guild.id, name),
                ) as cursor:
                    custom_command = await cursor.fetchone()
                if custom_command:
                    response, role_id, channel_id, cooldown, enabled = custom_command
                    if not enabled:
                        return
                    if (channel_id is None or channel_id == message.channel.id) and (
                        role_id is None or any(role.id == role_id for role in message.author.roles)
                    ):
                        if self._response_cooldown_ready(
                            ("custom", message.guild.id, name, message.author.id), cooldown
                        ):
                            raw_args = command_text[len(name):].strip()
                            try:
                                args = shlex.split(raw_args)
                            except ValueError:
                                args = raw_args.split()
                            await self._send_automated_response(
                                message,
                                response,
                                extra_values={"args": " ".join(args)},
                                command_args=args,
                            )
                    return
            break

        async with self.bot.db.execute(
            "SELECT trigger, response, mode, channel_id, role_id, cooldown, excluded_channel_ids, "
            "allowed_role_ids, excluded_role_ids, cooldown_scope, case_sensitive FROM autoresponders "
            "WHERE guild_id=? AND enabled=1 ORDER BY LENGTH(trigger) DESC",
            (message.guild.id,),
        ) as cursor:
            rows = await cursor.fetchall()
        for trigger, response, mode, channel_id, role_id, cooldown, excluded_channels, allowed_roles, excluded_roles, cooldown_scope, case_sensitive in rows:
            if not _matches_autoresponder_trigger(
                message.content,
                trigger,
                mode,
                case_sensitive=bool(case_sensitive),
            ):
                continue
            if channel_id is not None and channel_id != message.channel.id:
                continue
            try:
                blocked_channels = {int(item) for item in json.loads(excluded_channels or "[]")}
                allowed_role_set = {int(item) for item in json.loads(allowed_roles or "[]")}
                excluded_role_set = {int(item) for item in json.loads(excluded_roles or "[]")}
            except (TypeError, ValueError, json.JSONDecodeError):
                print(f"[community] Invalid restriction data for autoresponder {trigger!r} in guild {message.guild.id}.")
                continue
            if message.channel.id in blocked_channels:
                continue
            author_roles = {role.id for role in message.author.roles}
            admin_bypass = message.author.guild_permissions.administrator
            if not admin_bypass and role_id is not None:
                allowed_role_set.add(role_id)
            if not admin_bypass and allowed_role_set and not author_roles.intersection(allowed_role_set):
                continue
            if not admin_bypass and author_roles.intersection(excluded_role_set):
                continue
            cooldown_key = ("autoresponder", message.guild.id, trigger)
            if cooldown_scope == "channel":
                cooldown_key += (message.channel.id,)
            elif cooldown_scope != "global":
                cooldown_key += (message.author.id,)
            if not self._response_cooldown_ready(
                cooldown_key, cooldown
            ):
                continue
            arguments = _autoresponder_arguments(
                message.content,
                trigger,
                mode,
                case_sensitive=bool(case_sensitive),
            )
            await self._send_automated_response(
                message,
                response,
                extra_values={
                    "message": message.content,
                    "message.content": message.content,
                    "args": " ".join(arguments),
                },
                command_args=arguments,
            )
            return

    def _response_cooldown_ready(self, key, cooldown):
        now = asyncio.get_running_loop().time()
        last_used = self._response_cooldowns.get(key)
        if cooldown and last_used is not None and now < last_used[1]:
            return False
        if cooldown:
            self._response_cooldowns[key] = (now, now + cooldown)
        self._cooldown_checks += 1
        if self._cooldown_checks % 128 == 0:
            self._response_cooldowns = {
                entry: timestamps
                for entry, timestamps in self._response_cooldowns.items()
                if now < timestamps[1]
            }
        return True

    async def _send_automated_response(
        self,
        message,
        response,
        *,
        allow_delete=True,
        channel=None,
        user=None,
        guild=None,
        extra_values=None,
        command_args=None,
        delete_after=None,
    ):
        user = user or getattr(message, "author", None)
        guild = guild or getattr(message, "guild", None)
        source_channel = channel or getattr(message, "channel", None)
        if user is None or guild is None or source_channel is None:
            return False

        actions = AUTORESPONDER_ACTION_PATTERN.findall(response)
        delete_source = allow_delete and any(action.casefold() == "delete" for action in actions)
        destination = source_channel
        embed_name = None
        for action in actions:
            action = action.strip()
            if action.casefold() == "delete":
                continue
            action_type, _, value = action.partition(":")
            value = value.strip()
            if action_type.casefold() == "sendto":
                channel_id = value.strip("<#>")
                if not channel_id.isdecimal():
                    print(f"[community] Invalid sendto channel in automated response for guild {guild.id}.")
                    return False
                target = guild.get_channel_or_thread(int(channel_id))
                if target is None or not hasattr(target, "send"):
                    print(f"[community] sendto channel {channel_id} is unavailable in guild {guild.id}.")
                    return False
                bot_member = guild.me or guild.get_member(self.bot.user.id)
                if bot_member is None:
                    return False
                permissions = target.permissions_for(bot_member)
                if not permissions.view_channel or not permissions.send_messages:
                    print(f"[community] Missing view/send permission for sendto channel {channel_id} in guild {guild.id}.")
                    return False
                destination = target
            elif action_type.casefold() == "embed":
                embed_name = value.casefold()
        rendered = AUTORESPONDER_ACTION_PATTERN.sub("", response).strip()
        rendered = render_variables(
            rendered,
            user,
            guild,
            source_channel,
            self.bot.user,
            extra_values,
        )
        if command_args is not None:
            arguments = list(command_args)

            def substitute_argument(match):
                index = int(match.group(1)) - 1
                if match.group(2):
                    return " ".join(arguments[index:]) if 0 <= index < len(arguments) else ""
                return arguments[index] if 0 <= index < len(arguments) else ""

            rendered = re.sub(r"\$(\d+)(\+?)", substitute_argument, rendered)
        embed = None
        if embed_name:
            async with self.bot.db.execute(
                "SELECT config FROM custom_embeds WHERE guild_id=? AND name=?",
                (guild.id, embed_name),
            ) as cursor:
                saved_embed = await cursor.fetchone()
            if not saved_embed:
                print(f"[community] Automated response references missing embed {embed_name!r} in guild {guild.id}.")
                return False
            try:
                embed_config = json.loads(saved_embed[0])
                embed = build_custom_embed(embed_config, user, guild, source_channel, self.bot.user, extra_values)
                embed_view = build_custom_embed_view(
                    embed_config, user, guild, source_channel, self.bot.user, extra_values
                )
            except (ValueError, TypeError, discord.DiscordException) as error:
                print(f"[community] Invalid saved embed {embed_name!r} for guild {guild.id}: {error}")
                return False
        else:
            embed_view = None
        bot_member = guild.me or guild.get_member(self.bot.user.id)
        if bot_member is None:
            return False
        permissions = destination.permissions_for(bot_member)
        if not permissions.view_channel or not permissions.send_messages:
            print(f"[community] Missing view/send permission in response channel for guild {guild.id}.")
            return False
        if embed is not None and not permissions.embed_links:
            print(f"[community] Missing embed links permission in response channel for guild {guild.id}.")
            return False
        try:
            if rendered or embed is not None:
                deletion_options = (
                    {"delete_after": delete_after}
                    if delete_after is not None
                    else {}
                )
                await send_v2(
                    destination,
                    rendered[:2000] if rendered else None,
                    embed=embed,
                    view=embed_view,
                    **deletion_options,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            if delete_source and hasattr(message, "delete"):
                await message.delete(reason="Automated response requested message deletion")
        except discord.HTTPException as error:
            print(f"[community] Failed to send automated response in guild {guild.id}: {error}")
            return False
        return True

    async def _build_member_event_card(self, member, event_kind, detail):
        timeout = aiohttp.ClientTimeout(total=15, connect=5, sock_read=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            avatar_bytes = await self._download_avatar(session, member)
            bot_icon_bytes = None
            if self.bot.user:
                try:
                    bot_icon_bytes = await self._download_avatar(session, self.bot.user)
                except (
                    aiohttp.ClientError,
                    asyncio.TimeoutError,
                    OSError,
                    discord.HTTPException,
                ) as error:
                    print(
                        f"[event-card] Failed to download bot icon for guild "
                        f"{member.guild.id}: {error}"
                    )
            guild_icon_bytes = None
            if member.guild.icon:
                icon_url = member.guild.icon.replace(format="png", size=128).url
                async with session.get(icon_url) as response:
                    if response.status == 200:
                        guild_icon_bytes = await response.read()
            card_bytes = await asyncio.to_thread(
                render_member_event_card,
                avatar_bytes,
                member.display_name,
                member.guild.name,
                event_kind,
                detail,
                guild_icon_bytes=guild_icon_bytes,
                bot_icon_bytes=bot_icon_bytes,
            )
        return discord.File(
            io.BytesIO(card_bytes),
            filename=f"falcon-{event_kind}-{member.id}.png",
        )

    async def _send_member_event_card_preview(self, ctx, event_kind, detail, title):
        card = await self._build_member_event_card(ctx.author, event_kind, detail)
        embed = discord.Embed(title=title, color=0xFFDD00)
        embed.set_image(url=f"attachment://{card.filename}")
        emoji = f"{BOOST_EMOJI} " if event_kind == "boost" else ""
        await send_v2(
            ctx,
            f"{emoji}{title}. Server settings were not changed.",
            embed=embed,
            file=card,
            allowed_mentions=discord.AllowedMentions.none(),
            ephemeral=ctx.interaction is not None,
        )

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        if before.premium_since is not None or after.premium_since is None:
            return
        async with self.bot.db.execute(
            "SELECT channel_id, message, enabled, embed_name, card_enabled, card_channel_id "
            "FROM boost_settings WHERE guild_id=?",
            (after.guild.id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row and row[4]:
            card_channel = after.guild.get_channel(row[5]) if row[5] else None
            card_channel = card_channel or (
                after.guild.get_channel(row[0]) if row[0] else None
            )
            if card_channel is not None:
                subscription_count = getattr(after.guild, "premium_subscription_count", 0) or 0
                boost_level = getattr(after.guild, "premium_tier", 0) or 0
                try:
                    card = await self._build_member_event_card(
                        after,
                        "boost",
                        f"LEVEL {boost_level}  •  BOOST #{subscription_count}",
                    )
                    card_embed = discord.Embed(color=0xFFDD00)
                    card_embed.set_image(url=f"attachment://{card.filename}")
                    await send_v2(
                        card_channel,
                        f"{BOOST_EMOJI} Thank you {after.mention} for boosting **{after.guild.name}**!",
                        embed=card_embed,
                        file=card,
                        allowed_mentions=discord.AllowedMentions(
                            users=[after],
                            roles=False,
                            everyone=False,
                            replied_user=False,
                        ),
                    )
                    return
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError, discord.HTTPException) as error:
                    print(f"[boost] Card generation failed for member {after.id} in guild {after.guild.id}: {error}")
        if await self.send_community_event_reply(after.guild, after, "boost"):
            return
        if not row or not row[2]:
            return
        channel = after.guild.get_channel(row[0])
        if channel is None:
            return
        subscription_count = getattr(after.guild, "premium_subscription_count", 0) or 0
        boost_level = getattr(after.guild, "premium_tier", 0) or 0
        extra_values = {
            "booster": after.mention,
            "boost.user": after.mention,
            "boost.count": subscription_count,
            "boost_count": subscription_count,
            "boost.level": boost_level,
        }
        response = self._boost_response_template(row[1], row[3])
        await self._send_automated_response(
            None,
            response,
            allow_delete=False,
            channel=channel,
            user=after,
            guild=after.guild,
            extra_values=extra_values,
        )

    @staticmethod
    def _format_tag_duration(seconds):
        for unit_seconds, unit_name in (
            (604800, "week"),
            (86400, "day"),
            (3600, "hour"),
            (60, "minute"),
            (1, "second"),
        ):
            if seconds % unit_seconds == 0:
                amount = seconds // unit_seconds
                return f"{amount} {unit_name}{'' if amount == 1 else 's'}"
        return f"{seconds} seconds"

    async def _build_tag_setup_embed(self, guild):
        async with self.bot.db.execute(
            "SELECT delete_after_seconds, enabled FROM tag_settings WHERE guild_id=?", (guild.id,)
        ) as cursor:
            settings = await cursor.fetchone()
        async with self.bot.db.execute(
            "SELECT channel_id FROM tag_channels WHERE guild_id=? ORDER BY channel_id", (guild.id,)
        ) as cursor:
            channel_ids = [row[0] for row in await cursor.fetchall()]

        embed = discord.Embed(title="Join Tag Setup", color=0xFFDD00)
        channel_mentions = [f"<#{channel_id}>" for channel_id in channel_ids]
        channel_value = ", ".join(channel_mentions) if channel_mentions else "No channels added"
        if len(channel_value) > 1024:
            channel_value = channel_value[:1000].rsplit(", ", 1)[0] + ", and more"
        embed.add_field(name=f"Channels ({len(channel_ids)})", value=channel_value, inline=False)
        delete_after = settings[0] if settings else None
        duration_value = self._format_tag_duration(delete_after) if delete_after else "Not set"
        embed.add_field(name="Delete after", value=duration_value, inline=False)
        enabled = bool(settings and settings[1])
        embed.add_field(name="Status", value="Enabled" if enabled else "Disabled", inline=False)
        return embed

    async def _send_tag_setup(self, ctx):
        view = TagSetupView(self, ctx.guild.id, ctx.author.id)
        embed = await self._build_tag_setup_embed(ctx.guild)
        await ctx.send(embed=embed, view=view, ephemeral=ctx.interaction is not None)

    @commands.hybrid_group(name="tag", invoke_without_command=True, case_insensitive=True)
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def tag(self, ctx):
        await self._send_tag_setup(ctx)

    @tag.command(name="setup", description="Choose tag channels and auto-delete duration")
    async def tag_setup(self, ctx):
        await self._send_tag_setup(ctx)

    @tag.command(name="list", description="Show tag channels, duration, and status")
    async def tag_list(self, ctx):
        embed = await self._build_tag_setup_embed(ctx.guild)
        await ctx.send(embed=embed, ephemeral=ctx.interaction is not None)

    @tag.command(name="enable", description="Enable automatic join tag pings")
    async def tag_enable(self, ctx):
        async with self.bot.db.execute(
            "SELECT delete_after_seconds FROM tag_settings WHERE guild_id=?", (ctx.guild.id,)
        ) as cursor:
            settings = await cursor.fetchone()
        async with self.bot.db.execute(
            "SELECT 1 FROM tag_channels WHERE guild_id=? LIMIT 1", (ctx.guild.id,)
        ) as cursor:
            has_channel = await cursor.fetchone()
        if not settings or not settings[0] or not has_channel:
            return await ctx.send(
                "Run `,tag setup` and choose at least one channel and a delete duration first.",
                ephemeral=ctx.interaction is not None,
            )
        await self.bot.db.execute(
            "UPDATE tag_settings SET enabled=1 WHERE guild_id=?", (ctx.guild.id,)
        )
        await self.bot.db.commit()
        await ctx.send("Automatic join tag pings enabled.", ephemeral=ctx.interaction is not None)

    @tag.command(name="disable", description="Disable automatic join tag pings")
    async def tag_disable(self, ctx):
        await self.bot.db.execute(
            "INSERT INTO tag_settings (guild_id, delete_after_seconds, enabled) VALUES (?, NULL, 0) "
            "ON CONFLICT(guild_id) DO UPDATE SET enabled=0",
            (ctx.guild.id,),
        )
        await self.bot.db.commit()
        await ctx.send("Automatic join tag pings disabled.", ephemeral=ctx.interaction is not None)

    async def send_tag_on_join(self, member):
        async with self.bot.db.execute(
            "SELECT delete_after_seconds, enabled FROM tag_settings WHERE guild_id=?", (member.guild.id,)
        ) as cursor:
            settings = await cursor.fetchone()
        if not settings or not settings[0] or not settings[1]:
            return

        async with self.bot.db.execute(
            "SELECT channel_id FROM tag_channels WHERE guild_id=?", (member.guild.id,)
        ) as cursor:
            channel_ids = [row[0] for row in await cursor.fetchall()]
        allowed_mentions = discord.AllowedMentions(
            users=[member], roles=False, everyone=False, replied_user=False
        )
        for channel_id in channel_ids:
            channel = member.guild.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                continue
            try:
                await channel.send(
                    member.mention,
                    allowed_mentions=allowed_mentions,
                    delete_after=settings[0],
                )
            except discord.HTTPException as error:
                print(f"[tag] Failed to ping new member in channel {channel_id}: {error}")

    @commands.Cog.listener()
    async def on_member_join(self, member):
        await self.send_configured_greeting(member, "join")
        await self.send_tag_on_join(member)

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        await self.send_configured_greeting(member, "leave")

    async def _send_saved_text_greeting(self, member, greeting_type, inviter=None):
        columns = {
            "join": ("join_channel_id", "join_message", "join_enabled"),
            "leave": ("leave_channel_id", "leave_message", "leave_enabled"),
        }
        if greeting_type not in columns:
            return False
        channel_column, message_column, enabled_column = columns[greeting_type]
        async with self.bot.db.execute(
            f"SELECT {channel_column}, {message_column}, {enabled_column} "
            "FROM guild_settings WHERE guild_id=?",
            (member.guild.id,),
        ) as cursor:
            settings = await cursor.fetchone()
        if not settings or not settings[2] or not settings[1]:
            return False
        channel = member.guild.get_channel(settings[0]) if settings[0] else None
        if channel is None:
            return False
        delete_after = await self._greeting_delete_after(member.guild.id, greeting_type)
        return await self._send_automated_response(
            None,
            settings[1],
            allow_delete=False,
            channel=channel,
            user=member,
            guild=member.guild,
            delete_after=delete_after,
            extra_values={
                "inviter": inviter.mention if inviter else "Unknown",
                "inviter.name": inviter.display_name if inviter else "Unknown",
                "inviter.id": inviter.id if inviter else "",
            },
        )

    async def send_configured_greeting(self, member, greeting_type, inviter=None):
        async with self.bot.db.execute(
            "SELECT channel_id, join_config, leave_config, join_enabled, leave_enabled, "
            "join_card_enabled, leave_card_enabled, join_card_channel_id, leave_card_channel_id "
            "FROM greeting_settings WHERE guild_id=?",
            (member.guild.id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            (
                channel_id,
                join_config,
                leave_config,
                join_enabled,
                leave_enabled,
                join_card_enabled,
                leave_card_enabled,
                join_card_channel_id,
                leave_card_channel_id,
            ) = row
            event_enabled = join_enabled if greeting_type == "join" else leave_enabled
            card_enabled = join_card_enabled if greeting_type == "join" else leave_card_enabled
            if event_enabled and card_enabled:
                card_channel_id = (
                    join_card_channel_id
                    if greeting_type == "join"
                    else leave_card_channel_id
                )
                channel = member.guild.get_channel(card_channel_id) if card_channel_id else None
                channel = channel or (
                    member.guild.get_channel(channel_id) if channel_id else None
                )
                if channel is not None:
                    delete_after = await self._greeting_delete_after(
                        member.guild.id, greeting_type
                    )
                    try:
                        detail = f"MEMBER #{member.guild.member_count or 0}"
                        if inviter:
                            detail += f"  •  INVITED BY {inviter.display_name}"
                        card = await self._build_member_event_card(
                            member, greeting_type, detail
                        )
                        card_embed = discord.Embed(color=0xFFDD00)
                        card_embed.set_image(url=f"attachment://{card.filename}")
                        await send_v2(
                            channel,
                            member.mention if greeting_type == "join" else None,
                            embed=card_embed,
                            file=card,
                            delete_after=delete_after,
                            allowed_mentions=(
                                discord.AllowedMentions(
                                    users=[member],
                                    roles=False,
                                    everyone=False,
                                    replied_user=False,
                                )
                                if greeting_type == "join"
                                else discord.AllowedMentions.none()
                            ),
                        )
                        return True
                    except (
                        ValueError,
                        TypeError,
                        discord.DiscordException,
                        aiohttp.ClientError,
                        asyncio.TimeoutError,
                        OSError,
                    ) as error:
                        print(
                            f"[greeting] Failed to send {greeting_type} card for "
                            f"member {member.id} in guild {member.guild.id}: {error}"
                        )
        if await self.send_community_event_reply(member.guild, member, greeting_type):
            return True
        if not row:
            return await self._send_saved_text_greeting(member, greeting_type, inviter)
        channel_id, join_config, leave_config, join_enabled, leave_enabled, join_card_enabled, leave_card_enabled, _, _ = row
        delete_after = await self._greeting_delete_after(member.guild.id, greeting_type)
        if not (join_enabled if greeting_type == "join" else leave_enabled):
            return False
        raw_config = join_config if greeting_type == "join" else leave_config
        card_enabled = join_card_enabled if greeting_type == "join" else leave_card_enabled
        channel = member.guild.get_channel(channel_id)
        if not card_enabled and (not raw_config or channel is None):
            return await self._send_saved_text_greeting(member, greeting_type, inviter)
        if card_enabled and not raw_config:
            return False
        if channel is None:
            return False
        try:
            config = json.loads(raw_config)
            if inviter:
                config = dict(config)
                description = config.get("description", "")
                config["description"] = f"{description}\nInvited by: {inviter.mention}".strip()
            embed = build_custom_embed(
                config,
                member,
                member.guild,
                channel,
                self.bot.user,
                {
                    "inviter": inviter.mention if inviter else "Unknown",
                    "inviter.name": inviter.display_name if inviter else "Unknown",
                    "inviter.id": inviter.id if inviter else "",
                },
            )
            view = build_custom_embed_view(
                config,
                member,
                member.guild,
                channel,
                self.bot.user,
                {
                    "inviter": inviter.mention if inviter else "Unknown",
                    "inviter.name": inviter.display_name if inviter else "Unknown",
                    "inviter.id": inviter.id if inviter else "",
                },
            )
            await send_v2(
                channel,
                embed=embed,
                view=view,
                delete_after=delete_after,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (
            ValueError,
            TypeError,
            discord.DiscordException,
            aiohttp.ClientError,
            asyncio.TimeoutError,
            OSError,
        ) as error:
            print(f"[greeting] Failed to send {greeting_type} embed for guild {member.guild.id}: {error}")
        return True

    def _next_fun_gif(self, action, gif_urls=None):
        if gif_urls:
            cached_urls = self._fun_gif_cache.get(action, [])
            self._fun_gif_cache[action] = list(
                dict.fromkeys([*cached_urls, *gif_urls])
            )[-30:]
        cached_urls = self._fun_gif_cache.get(action, [])
        if not cached_urls:
            return None
        last_url = self._fun_gif_last_urls.get(action)
        available_urls = [url for url in cached_urls if url != last_url] or cached_urls
        index = self._fun_gif_indexes.get(action, 0) % len(available_urls)
        self._fun_gif_indexes[action] = index + 1
        selected_url = available_urls[index]
        self._fun_gif_last_urls[action] = selected_url
        return selected_url

    async def _request_giphy_api(self, endpoint, params=()):
        api_key = os.getenv("GIPHY_API_KEY", "").strip()
        if not api_key:
            raise GiphyAPIError(None, "GIPHY_API_KEY is not configured.")

        timeout = aiohttp.ClientTimeout(total=7, connect=3, sock_read=5)
        request_params = {"api_key": api_key, **dict(params)}
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for attempt in range(2):
                try:
                    async with session.get(
                        f"{GIPHY_API_BASE_URL}/{endpoint.lstrip('/')}",
                        params=request_params,
                    ) as response:
                        if response.status == 200:
                            payload = await response.json()
                            if not isinstance(payload, dict) or "data" not in payload:
                                raise ValueError("GIPHY returned an invalid response.")
                            return payload
                        raise GiphyAPIError(response.status)
                except GiphyAPIError as error:
                    if error.status != 429 and error.status < 500:
                        raise
                    if attempt == 0:
                        await asyncio.sleep(0.5)
                        continue
                    raise
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    if attempt == 0:
                        await asyncio.sleep(0.5)
                        continue
                    raise
        raise RuntimeError("GIPHY request retry loop exited unexpectedly.")

    async def _request_giphy_action_gif_urls(self, category):
        payload = await self._request_giphy_api(
            "gifs/search",
            {
                "q": f"anime {category}",
                "limit": 10,
                "rating": "g",
                "bundle": "messaging_non_clips",
            },
        )
        results = payload["data"]
        if not isinstance(results, list):
            raise ValueError("GIPHY returned an invalid search response.")
        return [
            image_url
            for result in results
            if (image_url := _giphy_image_url(result)) is not None
        ]

    def _log_fun_gif_failure(self, action, errors):
        loop = asyncio.get_running_loop()
        now = loop.time()
        if now >= self._fun_gif_retry_after:
            reasons = []
            for provider, error in errors:
                status = getattr(error, "status", getattr(error, "code", None))
                detail = f"HTTP {status}" if status is not None else type(error).__name__
                reasons.append(f"{provider}: {detail}")
            print(
                f"[fun] GIF provider failed for {action} ({'; '.join(reasons)}); "
                "pausing requests for 5 minutes."
            )
        self._fun_gif_retry_after = now + 300

    async def _fetch_fun_gif(self, action):
        loop = asyncio.get_running_loop()
        if loop.time() < self._fun_gif_retry_after:
            return self._next_fun_gif(action)
        if not os.getenv("GIPHY_API_KEY", "").strip():
            return self._next_fun_gif(action)

        category = FUN_GIF_SEARCH_FALLBACKS.get(action, action)
        try:
            gif_urls = await self._request_giphy_action_gif_urls(category)
        except (GiphyAPIError, aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
            errors: list[tuple[str, Exception]] = [("GIPHY", error)]
        else:
            if gif_urls:
                self._fun_gif_retry_after = 0.0
                return self._next_fun_gif(action, gif_urls)
            errors = [("GIPHY", RuntimeError("no GIF results"))]

        self._log_fun_gif_failure(action, errors)
        return self._next_fun_gif(action)

    @staticmethod
    def _giphy_catalog_description(data):
        records = data if isinstance(data, list) else [data]
        lines = []
        for record in records:
            if not isinstance(record, dict):
                continue
            name = record.get("name") or record.get("name_encoded") or record.get("slug")
            if name:
                lines.append(f"• {str(name)[:160]}")
            subcategories = record.get("subcategories")
            if isinstance(subcategories, list):
                for subcategory in subcategories:
                    if isinstance(subcategory, dict):
                        subcategory_name = (
                            subcategory.get("name")
                            or subcategory.get("name_encoded")
                            or subcategory.get("slug")
                        )
                        if subcategory_name:
                            lines.append(f"  • {str(subcategory_name)[:150]}")
            tags = record.get("tags")
            if isinstance(tags, list):
                for tag in tags:
                    if isinstance(tag, str) and tag.strip():
                        lines.append(f"• {tag.strip()[:160]}")
                    elif isinstance(tag, dict):
                        tag_name = tag.get("name") or tag.get("name_encoded")
                        if tag_name:
                            lines.append(f"• {str(tag_name)[:160]}")
        return "\n".join(lines)[:4000]

    @staticmethod
    def _giphy_error_message(error):
        if isinstance(error, GiphyAPIError):
            if error.status is None:
                return str(error)
            if error.status in {401, 403}:
                return "GIPHY rejected the API key. Check that `GIPHY_API_KEY` is valid."
            if error.status == 404:
                return "That GIPHY endpoint or item was not found."
            if error.status == 429:
                return "GIPHY is rate-limiting requests. Please try again shortly."
            if error.status >= 500:
                return "GIPHY is temporarily unavailable. Please try again shortly."
        if isinstance(error, ValueError):
            return "GIPHY returned an unexpected response. Please try again later."
        return "GIPHY could not be reached. Please try again shortly."

    async def _send_giphy_payload(self, ctx, payload, title):
        data = payload.get("data")
        if isinstance(data, list):
            gif_items = [
                item for item in data if isinstance(item, dict) and _giphy_image_url(item)
            ]
            if gif_items:
                view = GiphyResultsView(ctx.author.id, gif_items, title)
                await ctx.send(
                    embed=view._make_embed(),
                    view=view if len(gif_items) > 1 else None,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
        elif isinstance(data, dict) and _giphy_image_url(data):
            view = GiphyResultsView(ctx.author.id, [data], title)
            await ctx.send(
                embed=view._make_embed(),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if data == [] or data is None:
            return await ctx.send(
                f"No {title.lower()} results were found.",
                ephemeral=ctx.interaction is not None,
            )

        description = self._giphy_catalog_description(data)
        if not description:
            raise ValueError("GIPHY returned results in an unsupported format.")
        embed = discord.Embed(title=title, description=description, color=0xFFDD00)
        embed.set_footer(text="Powered by GIPHY")
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def _send_giphy_endpoint(self, ctx, endpoint, params, title):
        try:
            payload = await self._request_giphy_api(endpoint, params)
            await self._send_giphy_payload(ctx, payload, title)
        except (
            GiphyAPIError,
            aiohttp.ClientError,
            asyncio.TimeoutError,
            ValueError,
        ) as error:
            print(f"[giphy] Request failed for {endpoint}: {error}")
            await ctx.send(
                self._giphy_error_message(error),
                ephemeral=ctx.interaction is not None,
            )

    @staticmethod
    def _giphy_pagination_is_valid(limit, offset):
        return (
            isinstance(limit, int)
            and 1 <= limit <= GIPHY_RESULT_LIMIT
            and isinstance(offset, int)
            and offset >= 0
        )

    @commands.command(name="search", help="Search GIPHY for GIFs")
    async def giphy_search(self, ctx, *, query: str):
        query = query.strip()
        if not query:
            return await ctx.send(
                "Enter a search phrase.",
                ephemeral=ctx.interaction is not None,
            )
        await self._send_giphy_endpoint(
            ctx,
            "gifs/search",
            {"q": query, "limit": GIPHY_RESULT_LIMIT, "offset": 0, "rating": "g"},
            "GIPHY Search",
        )

    @commands.command(name="trending", help="Show trending GIPHY GIFs")
    async def giphy_trending(self, ctx, limit: int = 10, offset: int = 0):
        if not self._giphy_pagination_is_valid(limit, offset):
            return await ctx.send(
                f"Limit must be between 1 and {GIPHY_RESULT_LIMIT}, and offset cannot be negative.",
                ephemeral=ctx.interaction is not None,
            )
        await self._send_giphy_endpoint(
            ctx,
            "gifs/trending",
            {"limit": limit, "offset": offset, "rating": "g"},
            "Trending GIPHY GIFs",
        )

    @commands.command(name="random", help="Show a random GIPHY GIF")
    async def giphy_random(self, ctx, *, tag: str = ""):
        params = {"rating": "g"}
        if tag.strip():
            params["tag"] = tag.strip()
        await self._send_giphy_endpoint(ctx, "gifs/random", params, "Random GIPHY GIF")

    @commands.command(name="translate", help="Translate a phrase into a GIPHY GIF")
    async def giphy_translate(self, ctx, *, phrase: str):
        phrase = phrase.strip()
        if not phrase:
            return await ctx.send(
                "Enter a phrase to translate.",
                ephemeral=ctx.interaction is not None,
            )
        await self._send_giphy_endpoint(
            ctx,
            "gifs/translate",
            {"s": phrase, "rating": "g"},
            "GIPHY Translation",
        )

    @commands.command(name="get", help="Get one or more GIPHY GIFs by ID")
    async def giphy_get(self, ctx, ids: str):
        gif_ids = [gif_id.strip() for gif_id in ids.split(",")]
        if (
            not gif_ids
            or len(gif_ids) > GIPHY_RESULT_LIMIT
            or not all(GIPHY_ID_PATTERN.fullmatch(gif_id) for gif_id in gif_ids)
        ):
            return await ctx.send(
                f"Provide 1 to {GIPHY_RESULT_LIMIT} comma-separated GIPHY IDs using letters, numbers, `_`, or `-`.",
                ephemeral=ctx.interaction is not None,
            )
        if len(gif_ids) == 1:
            endpoint = f"gifs/{quote(gif_ids[0], safe='')}"
            params = {}
        else:
            endpoint = "gifs"
            params = {"ids": ",".join(gif_ids)}
        await self._send_giphy_endpoint(ctx, endpoint, params, "GIPHY GIF")

    @commands.command(name="categories", help="List GIPHY GIF categories")
    async def giphy_categories(self, ctx):
        await self._send_giphy_endpoint(
            ctx,
            "gifs/categories",
            {"limit": GIPHY_RESULT_LIMIT},
            "GIPHY Categories",
        )

    @commands.command(name="category", help="List tags in a GIPHY category")
    async def giphy_category(
        self,
        ctx,
        category: str,
        limit: int = 10,
        offset: int = 0,
    ):
        category = category.strip()
        if not category:
            return await ctx.send(
                "Enter a category slug.",
                ephemeral=ctx.interaction is not None,
            )
        if not self._giphy_pagination_is_valid(limit, offset):
            return await ctx.send(
                f"Limit must be between 1 and {GIPHY_RESULT_LIMIT}, and offset cannot be negative.",
                ephemeral=ctx.interaction is not None,
            )
        endpoint = f"gifs/categories/{quote(category, safe='')}"
        await self._send_giphy_endpoint(
            ctx,
            endpoint,
            {"limit": limit, "offset": offset},
            f"GIPHY {category.title()} Tags",
        )

    @commands.command(name="category-tag", help="Browse GIFs for a category tag")
    async def giphy_category_tag(
        self,
        ctx,
        category: str,
        tag: str,
        limit: int = 10,
        offset: int = 0,
    ):
        category = category.strip()
        tag = tag.strip()
        if not category or not tag:
            return await ctx.send(
                "Enter both a category slug and tag.",
                ephemeral=ctx.interaction is not None,
            )
        if not self._giphy_pagination_is_valid(limit, offset):
            return await ctx.send(
                f"Limit must be between 1 and {GIPHY_RESULT_LIMIT}, and offset cannot be negative.",
                ephemeral=ctx.interaction is not None,
            )
        endpoint = (
            f"gifs/categories/{quote(category, safe='')}/{quote(tag, safe='')}"
        )
        await self._send_giphy_endpoint(
            ctx,
            endpoint,
            {"limit": limit, "offset": offset},
            f"GIPHY {category.title()} · {tag.title()}",
        )

    @commands.command(
        name="sticker",
        aliases=["sticker-search"],
        help="Search GIPHY animated stickers",
    )
    async def giphy_sticker_search(self, ctx, *, query: str):
        query = query.strip()
        if not query:
            return await ctx.send(
                "Enter a sticker search phrase.",
                ephemeral=ctx.interaction is not None,
            )
        await self._send_giphy_endpoint(
            ctx,
            "stickers/search",
            {"q": query, "limit": GIPHY_RESULT_LIMIT, "offset": 0, "rating": "g"},
            "GIPHY Sticker Search",
        )

    @commands.command(name="sticker-trending", help="Show trending GIPHY stickers")
    async def giphy_sticker_trending(self, ctx, limit: int = 10, offset: int = 0):
        if not self._giphy_pagination_is_valid(limit, offset):
            return await ctx.send(
                f"Limit must be between 1 and {GIPHY_RESULT_LIMIT}, and offset cannot be negative.",
                ephemeral=ctx.interaction is not None,
            )
        await self._send_giphy_endpoint(
            ctx,
            "stickers/trending",
            {"limit": limit, "offset": offset, "rating": "g"},
            "Trending GIPHY Stickers",
        )

    @commands.command(name="sticker-random", help="Show a random GIPHY sticker")
    async def giphy_sticker_random(self, ctx, *, tag: str = ""):
        params = {"rating": "g"}
        if tag.strip():
            params["tag"] = tag.strip()
        await self._send_giphy_endpoint(
            ctx,
            "stickers/random",
            params,
            "Random GIPHY Sticker",
        )

    @commands.command(name="sticker-translate", help="Translate a phrase into a GIPHY sticker")
    async def giphy_sticker_translate(self, ctx, *, phrase: str):
        phrase = phrase.strip()
        if not phrase:
            return await ctx.send(
                "Enter a phrase to translate.",
                ephemeral=ctx.interaction is not None,
            )
        await self._send_giphy_endpoint(
            ctx,
            "stickers/translate",
            {"s": phrase, "rating": "g"},
            "GIPHY Sticker Translation",
        )

    async def send_fun_action(self, ctx, action, member, *, spam=False):
        description = FUN_ACTIONS[action]
        embed = discord.Embed(
            title=f"{action.title()} Spam" if spam else None,
            description="\n".join(
                f"{ctx.author.mention} {description} {member.mention}!"
                for _ in range(5 if spam else 1)
            ),
            color=0xFFDD00,
        )
        gif_url = await self._fetch_fun_gif(action)
        if gif_url:
            embed.set_image(url=gif_url)
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_command(
        name="ship",
        description="Ship yourself with a chosen or random user",
    )
    async def ship(self, ctx, user: discord.User = None, second: discord.User = None):
        if user is None:
            user = await self._resolve_member_from_context(ctx)
        if user is None:
            return await self._send_random_ship(ctx)
        first_member, second_member = (ctx.author, user) if second is None else (user, second)
        return await self._send_ship_result(ctx, first_member, second_member)

    @ship.error
    async def ship_error(self, ctx, error):
        original_error = getattr(error, "original", error)
        if (
            ctx.interaction is None
            and isinstance(original_error, commands.UserNotFound)
            and self._is_ship_random_invocation(ctx)
        ):
            return await self._send_random_ship(ctx)
        await self.bot.on_command_error(ctx, original_error)

    @commands.hybrid_command(
        name="ship-random",
        aliases=("ship_random",),
        description="Ship yourself with a random user the bot knows",
    )
    async def ship_random(self, ctx):
        await self._send_random_ship(ctx)

    @staticmethod
    def _is_ship_random_invocation(ctx):
        message = getattr(ctx, "message", None)
        if message is None or ctx.command is None:
            return False
        content = message.content
        prefix = ctx.prefix or ""
        if not content.startswith(prefix):
            return False
        command_and_arguments = content[len(prefix):].strip().split(maxsplit=1)
        return (
            len(command_and_arguments) == 2
            and command_and_arguments[0].casefold() == "ship"
            and command_and_arguments[1].casefold() == "random"
        )

    async def _send_random_ship(self, ctx):
        candidates = await self._ship_random_users({ctx.author.id})
        if not candidates:
            return await ctx.send(
                "I couldn't find another non-bot user in the servers I'm in.",
                ephemeral=ctx.interaction is not None,
            )
        return await self._send_ship_result(
            ctx,
            ctx.author,
            random.choice(candidates),
        )

    async def _ship_random_users(self, excluded_ids=()):
        bot_user_id = getattr(getattr(self.bot, "user", None), "id", None)
        excluded_ids = set(excluded_ids)
        if bot_user_id is not None:
            excluded_ids.add(bot_user_id)

        if getattr(self.bot, "cluster_count", 1) <= 1:
            return [
                user
                for user in getattr(self.bot, "users", ())
                if not user.bot and user.id not in excluded_ids
            ]

        async with self.bot.db.execute(
            "SELECT user_id FROM users GROUP BY user_id ORDER BY RANDOM() LIMIT 50"
        ) as cursor:
            user_ids = [row[0] for row in await cursor.fetchall()]
        cached_users = {
            user.id: user
            for user in getattr(self.bot, "users", ())
            if not user.bot and user.id not in excluded_ids
        }
        user_ids = list(dict.fromkeys([*user_ids, *cached_users]))
        random.shuffle(user_ids)
        candidates = []
        for user_id in user_ids:
            if user_id in excluded_ids:
                continue
            user = cached_users.get(user_id) or self.bot.get_user(user_id)
            if user is None:
                try:
                    user = await self.bot.fetch_user(user_id)
                except discord.HTTPException:
                    logging.getLogger(__name__).warning(
                        "Could not resolve global ship candidate %s",
                        user_id,
                        exc_info=True,
                    )
                    continue
            if not user.bot:
                candidates.append(user)
            if len(candidates) >= 10:
                break
        return candidates

    async def _send_ship_result(self, ctx, first_member, second_member):
        try:
            embed, file = await self._create_ship_result(
                first_member,
                second_member,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as error:
            print(f"[ship] Card generation failed: {error}")
            return await ctx.send(
                "Could not create the ship card right now. Please try again.",
                ephemeral=ctx.interaction is not None,
            )
        await ctx.send(
            embed=embed,
            file=file,
            view=ShipAgainView(
                self,
                first_member,
                second_member,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _create_ship_result(self, first_member, second_member):
        percentage = random.randint(0, 100)
        if percentage >= 90:
            label = "Perfect Match"
        elif percentage >= 75:
            label = "Amazing Chemistry"
        elif percentage >= 60:
            label = "Cute Pair"
        elif percentage >= 40:
            label = "Good Match"
        elif percentage >= 20:
            label = "Maybe"
        else:
            label = "Friend Zone"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session:
            first_bytes, second_bytes = await asyncio.gather(
                self._download_avatar(session, first_member),
                self._download_avatar(session, second_member),
            )
        card = await asyncio.to_thread(
            render_ship_card,
            first_bytes,
            second_bytes,
            first_member.display_name,
            second_member.display_name,
            percentage,
            label,
        )
        file = discord.File(io.BytesIO(card), filename="falcon-ship.png")
        embed = discord.Embed(
            title="💞 FALCON LOVE LAB",
            description=(
                f"{first_member.mention}  **×**  {second_member.mention}\n\n"
                f"### {percentage}% — {label}\n"
                "A playful match score, just for fun."
            ),
            color=0xFFDD00,
        )
        embed.set_image(url="attachment://falcon-ship.png")
        embed.set_footer(text="FALCON • LOVE LAB")
        return embed, file

    async def _download_avatar(self, session, member):
        avatar_url = member.display_avatar.replace(format="png", size=256).url
        async with session.get(avatar_url) as response:
            if response.status != 200:
                raise aiohttp.ClientError(f"Avatar download returned HTTP {response.status}")
            return await response.read()


def render_ship_card(first_avatar, second_avatar, first_name, second_name, percentage, label):
    width, height = 1600, 900
    image = Image.new("RGBA", (width, height), (255, 242, 235, 255))
    draw = ImageDraw.Draw(image)
    background_stops = (
        (0.0, (255, 249, 235)),
        (0.34, (255, 221, 226)),
        (0.68, (250, 199, 215)),
        (1.0, (255, 237, 207)),
    )
    for y in range(height):
        position = y / (height - 1)
        for stop_index in range(len(background_stops) - 1):
            start_at, start_color = background_stops[stop_index]
            end_at, end_color = background_stops[stop_index + 1]
            if position <= end_at:
                blend = (position - start_at) / (end_at - start_at)
                color = tuple(
                    int(start_color[channel] * (1 - blend) + end_color[channel] * blend)
                    for channel in range(3)
                )
                break
        draw.line((0, y, width, y), fill=(*color, 255))

    gold = (188, 132, 48, 255)
    deep_red = (133, 34, 59, 255)
    red = (184, 41, 70, 255)
    rose = (221, 76, 112, 255)
    pink = (246, 139, 167, 255)
    pale_pink = (255, 222, 227, 255)
    white = (255, 252, 246, 255)
    muted = (139, 76, 83, 255)

    def heart_points(center, heart_width, heart_height):
        points = []
        for step in range(241):
            angle = 2 * math.pi * step / 240
            x = 16 * math.sin(angle) ** 3
            y = (
                13 * math.cos(angle)
                - 5 * math.cos(2 * angle)
                - 2 * math.cos(3 * angle)
                - math.cos(4 * angle)
            )
            points.append(
                (
                    center[0] + x * heart_width / 32,
                    center[1] - y * heart_height / 34,
                )
            )
        return points

    def gradient_heart(center, heart_width, heart_height, colors, *, outline=None, shadow=False):
        heart_layer = Image.new(
            "RGBA",
            (heart_width + 60, heart_height + 60),
            (0, 0, 0, 0),
        )
        heart_draw = ImageDraw.Draw(heart_layer)
        local_center = (heart_layer.width // 2, heart_layer.height // 2)
        points = heart_points(local_center, heart_width, heart_height)
        mask = Image.new("L", heart_layer.size, 0)
        ImageDraw.Draw(mask).polygon(points, fill=255)

        if shadow:
            shadow_layer = Image.new("RGBA", heart_layer.size, (157, 36, 68, 0))
            shadow_alpha = mask.filter(ImageFilter.GaussianBlur(22)).point(
                lambda alpha: int(alpha * 0.38)
            )
            shadow_layer.putalpha(shadow_alpha)
            image.alpha_composite(shadow_layer, (center[0] - heart_layer.width // 2 + 5, center[1] + 10 - heart_layer.height // 2))

        gradient = Image.new("RGBA", heart_layer.size, (0, 0, 0, 0))
        gradient_draw = ImageDraw.Draw(gradient)
        for y in range(gradient.height):
            position = y / max(1, gradient.height - 1)
            for stop_index in range(len(colors) - 1):
                start_at, start_color = colors[stop_index]
                end_at, end_color = colors[stop_index + 1]
                if position <= end_at:
                    blend = (position - start_at) / (end_at - start_at)
                    color = tuple(
                        int(start_color[channel] * (1 - blend) + end_color[channel] * blend)
                        for channel in range(3)
                    )
                    break
            gradient_draw.line((0, y, gradient.width, y), fill=(*color, 255))
        gradient.putalpha(mask)
        image.alpha_composite(gradient, (center[0] - heart_layer.width // 2, center[1] - heart_layer.height // 2))
        if outline:
            shifted_points = [
                (point[0] + center[0] - heart_layer.width // 2, point[1] + center[1] - heart_layer.height // 2)
                for point in points
            ]
            ImageDraw.Draw(image).line(
                shifted_points + [shifted_points[0]],
                fill=outline,
                width=5 if heart_width > 100 else 2,
                joint="curve",
            )

    def make_gradient_surface(box, top_color, bottom_color, radius, outline):
        left, top, right, bottom = box
        surface_width, surface_height = right - left, bottom - top
        surface = Image.new("RGBA", (surface_width, surface_height), (0, 0, 0, 0))
        surface_draw = ImageDraw.Draw(surface)
        for y in range(surface_height):
            blend = y / max(1, surface_height - 1)
            color = tuple(
                int(top_color[channel] * (1 - blend) + bottom_color[channel] * blend)
                for channel in range(3)
            )
            surface_draw.line((0, y, surface_width, y), fill=(*color, 255))
        mask = Image.new("L", surface.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, surface_width - 1, surface_height - 1),
            radius=radius,
            fill=255,
        )
        surface.putalpha(mask)
        image.alpha_composite(surface, (left, top))
        ImageDraw.Draw(image).rounded_rectangle(box, radius=radius, outline=outline, width=2)

    font_cache = {}

    def font(size, *, bold=False, serif=False):
        key = (size, bold, serif)
        if key not in font_cache:
            if serif:
                path = "DejaVuSerif-Bold.ttf" if bold else "DejaVuSerif.ttf"
            else:
                path = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
            try:
                font_cache[key] = ImageFont.truetype(path, size)
            except OSError:
                font_cache[key] = ImageFont.load_default()
        return font_cache[key]

    def fitted_text(text, center, max_width, *, size, color, bold=True, serif=False):
        value = unicodedata.normalize("NFKC", str(text or "").strip()) or "Someone special"

        def font_runs(text, font_size):
            primary = font(font_size, bold=bold, serif=serif)
            fallback = font(font_size, bold=bold, serif=False)
            primary_missing = bytes(primary.getmask("\U0010ffff"))
            fallback_missing = bytes(fallback.getmask("\U0010ffff"))
            runs = []
            total_width = 0
            for character in text:
                if unicodedata.category(character) == "Cf" or character in "\ufe0e\ufe0f":
                    continue
                if bytes(primary.getmask(character)) != primary_missing:
                    selected_font = primary
                elif bytes(fallback.getmask(character)) != fallback_missing:
                    selected_font = fallback
                else:
                    character = "?"
                    selected_font = fallback
                if runs and runs[-1][0] is selected_font:
                    runs[-1][1] += character
                else:
                    runs.append([selected_font, character])
                total_width += draw.textlength(character, font=selected_font)
            return runs, total_width

        runs, text_width = font_runs(value, size)
        while text_width > max_width and size > 42:
            size -= 1
            runs, text_width = font_runs(value, size)
        while text_width > max_width and len(value) > 1:
            value = value[:-2].rstrip() + "…"
            runs, text_width = font_runs(value, size)

        cursor = center[0] - text_width / 2
        for selected_font, run in runs:
            draw.text((cursor, center[1]), run, font=selected_font, fill=color, anchor="lm")
            cursor += draw.textlength(run, font=selected_font)

    draw.rounded_rectangle(
        (32, 30, width - 32, height - 30),
        radius=38,
        outline=gold,
        width=4,
    )
    draw.rounded_rectangle(
        (46, 44, width - 46, height - 44),
        radius=30,
        outline=(255, 252, 246, 235),
        width=2,
    )
    draw.line((152, 74, 152, 214), fill=(255, 252, 246, 125), width=2)
    draw.line((width - 152, 74, width - 152, 214), fill=(255, 252, 246, 125), width=2)
    draw.polygon(((91, 124), (101, 114), (111, 124), (101, 134)), fill=gold)
    draw.polygon(((1489, 124), (1499, 114), (1509, 124), (1499, 134)), fill=gold)

    draw.text(
        (width // 2, 60),
        "F A L C O N   /   M A T C H   S T U D I O",
        font=font(34, bold=True),
        fill=deep_red,
        anchor="mm",
    )
    draw.text(
        (width // 2, 125),
        "Love, in the making",
        font=font(53, bold=True, serif=True),
        fill=deep_red,
        anchor="mm",
    )
    draw.text(
        (width // 2, 185),
        "A playful little measure of your chemistry",
        font=font(33),
        fill=muted,
        anchor="mm",
    )
    draw.line((180, 226, 680, 226), fill=(188, 132, 48, 170), width=2)
    draw.line((920, 226, 1420, 226), fill=(188, 132, 48, 170), width=2)
    gradient_heart(
        (800, 226),
        34,
        30,
        ((0.0, (255, 181, 184)), (0.55, (231, 82, 117)), (1.0, (181, 34, 66))),
        outline=gold,
    )

    def draw_profile(data, name, center_x, index, accent):
        left, right = center_x - 226, center_x + 226
        top, bottom = 282, 718
        make_gradient_surface(
            (left, top, right, bottom),
            (255, 253, 247),
            (255, 226, 230),
            28,
            (188, 132, 48, 220),
        )
        draw.rounded_rectangle(
            (left + 11, top + 11, right - 11, bottom - 11),
            radius=21,
            outline=(221, 119, 142, 135),
            width=1,
        )
        draw.text(
            (center_x, top + 43),
            f"CONNECTION  /  {index:02}",
            font=font(31, bold=True),
            fill=accent,
            anchor="mm",
        )
        draw.line(
            (left + 76, top + 76, right - 76, top + 76),
            fill=(214, 172, 125, 185),
            width=1,
        )

        avatar_size = 200
        avatar_box = (
            center_x - avatar_size // 2,
            top + 105,
            center_x + avatar_size // 2,
            top + 105 + avatar_size,
        )
        frame_box = (
            avatar_box[0] - 10,
            avatar_box[1] - 10,
            avatar_box[2] + 10,
            avatar_box[3] + 10,
        )
        draw.rounded_rectangle(
            frame_box,
            radius=38,
            fill=(255, 251, 243, 255),
            outline=gold,
            width=4,
        )
        try:
            avatar = Image.open(io.BytesIO(data)).convert("RGBA")
            avatar = ImageOps.fit(avatar, (avatar_size, avatar_size))
            mask = Image.new("L", (avatar_size, avatar_size), 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                (0, 0, avatar_size - 1, avatar_size - 1),
                radius=30,
                fill=255,
            )
            image.paste(avatar, (avatar_box[0], avatar_box[1]), mask)
        except (OSError, ValueError):
            fallback = Image.new("RGBA", (avatar_size, avatar_size), (255, 229, 232, 255))
            ImageDraw.Draw(fallback).rounded_rectangle(
                (0, 0, avatar_size - 1, avatar_size - 1),
                radius=30,
                fill=(245, 180, 188, 255),
            )
            image.alpha_composite(fallback, (avatar_box[0], avatar_box[1]))

        fitted_text(
            name,
            (center_x, top + 338),
            392,
            size=45,
            color=deep_red,
            serif=True,
        )
        draw.text(
            (center_x, top + 400),
            "SHIP PARTNER",
            font=font(31, bold=True),
            fill=muted,
            anchor="mm",
        )

    draw_profile(first_avatar, first_name, 350, 1, rose)
    draw_profile(second_avatar, second_name, 1250, 2, gold)

    heart_colors = (
        (0.0, (255, 204, 119)),
        (0.28, (255, 142, 159)),
        (0.63, (232, 64, 105)),
        (1.0, (184, 30, 64)),
    )
    gradient_heart(
        (800, 493),
        350,
        330,
        heart_colors,
        outline=(255, 248, 225, 255),
        shadow=True,
    )
    draw.text(
        (800, 390),
        "MATCH SCORE",
        font=font(33, bold=True),
        fill=(255, 240, 228, 255),
        anchor="mm",
    )
    draw.text(
        (800, 516),
        f"{percentage}%",
        font=font(99, bold=True, serif=True),
        fill=white,
        anchor="mm",
        stroke_width=1,
        stroke_fill=(169, 38, 69, 255),
    )
    draw.text(
        (800, 680),
        label.upper(),
        font=font(28, bold=True),
        fill=deep_red,
        anchor="mm",
    )

    make_gradient_surface(
        (144, 728, width - 144, 820),
        (255, 252, 245),
        (255, 229, 232),
        20,
        (188, 132, 48, 200),
    )
    draw.text(
        (184, 748),
        "CHEMISTRY METER",
        font=font(31, bold=True),
        fill=deep_red,
        anchor="lm",
    )
    draw.text(
        (width - 184, 748),
        f"{percentage} / 100",
        font=font(31, bold=True),
        fill=deep_red,
        anchor="rm",
    )
    meter_left, meter_right = 184, width - 184
    draw.rounded_rectangle(
        (meter_left, 790, meter_right, 804),
        radius=5,
        fill=(242, 187, 196, 255),
    )
    meter_end = meter_left + int((meter_right - meter_left) * percentage / 100)
    if meter_end > meter_left:
        meter_gradient = Image.new("RGBA", (meter_end - meter_left, 15), (0, 0, 0, 0))
        meter_draw = ImageDraw.Draw(meter_gradient)
        for x in range(meter_gradient.width):
            blend = x / max(1, meter_gradient.width - 1)
            color = tuple(
                int(rose[channel] * (1 - blend) + gold[channel] * blend)
                for channel in range(3)
            )
            meter_draw.line((x, 0, x, 14), fill=(*color, 255))
        meter_mask = Image.new("L", meter_gradient.size, 0)
        ImageDraw.Draw(meter_mask).rounded_rectangle(
            (0, 0, meter_gradient.width - 1, 14),
            radius=5,
            fill=255,
        )
        meter_gradient.putalpha(meter_mask)
        image.alpha_composite(meter_gradient, (meter_left, 790))

    draw.text(
        (width // 2, 842),
        "FOR FUN ONLY  /  YOUR STORY, YOUR SHIP",
        font=font(36, bold=True),
        fill=deep_red,
        anchor="mm",
    )

    output = io.BytesIO()
    image.convert("RGB").save(output, format="PNG", optimize=True)
    return output.getvalue()


def _make_fun_command(action):
    async def callback(ctx, *, arguments: str = ""):
        cog = ctx.bot.get_cog("CommunityFeatures")
        parts = arguments.strip().split(maxsplit=1)
        spam = bool(parts and parts[0].casefold() == "spam")
        target_query = parts[1] if spam and len(parts) > 1 else ("" if spam else arguments.strip())
        target = await cog._resolve_member_from_context(ctx, query=target_query)
        if target is None:
            return await ctx.send(
                f"Use `,{action} @user`, `,{action} spam @user`, or reply to a user's message.",
                ephemeral=ctx.interaction is not None,
            )
        await cog.send_fun_action(ctx, action, target, spam=spam)

    return commands.command(name=action, help=f"{action.title()} another member")(callback)


def register_fun_commands(bot):
    for action in FUN_ACTIONS:
        if bot.get_command(action) is None:
            bot.add_command(_make_fun_command(action))


async def setup(bot):
    cog = CommunityFeatures(bot)
    await bot.add_cog(cog)
    register_fun_commands(bot)
