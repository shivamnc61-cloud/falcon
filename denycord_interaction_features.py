"""Persistent ticket and reaction-role features for Falcon (discord.py 2.x)."""

import asyncio
import datetime
import html
import io
import json
import logging
import re
from typing import Literal
from urllib.parse import urlsplit

import aiosqlite
import discord
from discord.ext import commands

from components_v2 import edit_interaction_v2, edit_message_v2, make_layout_view


LOGGER = logging.getLogger(__name__)
UTC = datetime.timezone.utc
TEMPLATE_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
TICKET_OPEN_CUSTOM_ID = "denycord_ticket_open:v1"
TICKET_CLOSE_CUSTOM_ID = "denycord_ticket_close:v1"
TICKET_CATEGORY_CUSTOM_ID = "denycord_ticket_category:v1"
TICKET_CATEGORY_BUTTON_PREFIX = "denycord_ticket_category_button:v1"
TICKET_CLAIM_CUSTOM_ID = "denycord_ticket_claim:v1"
TICKET_REOPEN_CUSTOM_ID = "denycord_ticket_reopen:v1"
TICKET_DELETE_CUSTOM_ID = "denycord_ticket_delete:v1"
TICKET_PANEL_SELECT_PREFIX = "falcon:ticket-panel:select:"
TICKET_PANEL_BUTTON_PREFIX = "falcon:ticket-panel:button:"
_PANEL_SEARCH_VALUE = "__search_panel_names__"
TICKET_BUTTON_STYLES = {
    "blue": discord.ButtonStyle.primary,
    "grey": discord.ButtonStyle.secondary,
    "gray": discord.ButtonStyle.secondary,
    "green": discord.ButtonStyle.success,
    "red": discord.ButtonStyle.danger,
}


async def _ticket_staff_check(ctx: commands.Context) -> bool:
    if ctx.guild is None:
        return False
    permissions = ctx.author.guild_permissions
    if permissions.manage_channels or permissions.administrator:
        return True
    config = await ctx.cog._ticket_config(ctx.guild.id)
    support_role_id = config[7] if config and len(config) > 7 else None
    return bool(
        support_role_id
        and any(role.id == support_role_id for role in ctx.author.roles)
    )


def _now() -> str:
    return datetime.datetime.now(UTC).isoformat()


def _ticket_filename(value: str) -> str:
    value = re.sub(r"[^a-z0-9-]+", "-", value.casefold()).strip("-")
    return (value or "member")[:32]


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


class TicketPanelView(discord.ui.View):
    def __init__(
        self,
        cog,
        *,
        label: str = "Open a Ticket",
        emoji: str = "🎫",
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    ):
        super().__init__(timeout=None)
        self.cog = cog
        self.open_ticket.label = label
        self.open_ticket.emoji = emoji
        self.open_ticket.style = style

    @discord.ui.button(
        label="Open a Ticket",
        style=discord.ButtonStyle.secondary,
        emoji="🎫",
        custom_id=TICKET_OPEN_CUSTOM_ID,
    )
    async def open_ticket(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.open_ticket(interaction)


class TicketCategoryButton(discord.ui.Button):
    def __init__(
        self,
        cog,
        category_id: int,
        label: str,
        style: discord.ButtonStyle,
        emoji: str | None,
    ):
        super().__init__(
            label=label[:80],
            emoji=emoji,
            style=style,
            custom_id=f"{TICKET_CATEGORY_BUTTON_PREFIX}:{category_id}",
        )
        self.cog = cog
        self.category_id = category_id

    async def callback(self, interaction: discord.Interaction):
        await self.cog.open_ticket(interaction, category_id=self.category_id)


class TicketCategoryButtonsView(discord.ui.View):
    def __init__(
        self,
        cog,
        categories: list[tuple[int, str]],
        *,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        emoji: str | None = None,
    ):
        super().__init__(timeout=None)
        for category_id, label in categories[:25]:
            self.add_item(TicketCategoryButton(cog, category_id, label, style, emoji))


class TicketCloseView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Close Ticket",
        style=discord.ButtonStyle.secondary,
        emoji="🔒",
        custom_id=TICKET_CLOSE_CUSTOM_ID,
    )
    async def close_ticket(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.close_ticket(interaction)


class TicketCategorySelectView(discord.ui.View):
    def __init__(self, cog, categories: list[discord.CategoryChannel] | None = None):
        super().__init__(timeout=None)
        options = [
            discord.SelectOption(
                label=category.name[:100],
                value=str(category.id),
            )
            for category in (categories or [])
        ]
        if not options:
            options = [discord.SelectOption(label="Support", value="0")]
        self.add_item(TicketCategorySelect(cog, options[:25]))


class TicketCategorySelect(discord.ui.Select):
    def __init__(self, cog, options: list[discord.SelectOption]):
        super().__init__(
            placeholder="Choose a ticket category",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=TICKET_CATEGORY_CUSTOM_ID,
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        try:
            category_id = int(self.values[0])
        except (ValueError, IndexError):
            return await self.cog._send_interaction(interaction, "Choose a valid ticket category.")
        await self.cog.open_ticket(interaction, category_id=category_id)


class TicketActionsView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Claim",
        style=discord.ButtonStyle.secondary,
        emoji="🙋",
        custom_id=TICKET_CLAIM_CUSTOM_ID,
    )
    async def claim_ticket(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.ticket_action(interaction, "claim")

    @discord.ui.button(
        label="Close Ticket",
        style=discord.ButtonStyle.secondary,
        emoji="🔒",
        custom_id=TICKET_CLOSE_CUSTOM_ID,
    )
    async def close_ticket(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.close_ticket(interaction)

    @discord.ui.button(
        label="Reopen",
        style=discord.ButtonStyle.secondary,
        emoji="🔓",
        custom_id=TICKET_REOPEN_CUSTOM_ID,
    )
    async def reopen_ticket(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.ticket_action(interaction, "reopen")

    @discord.ui.button(
        label="Delete",
        style=discord.ButtonStyle.secondary,
        emoji="🗑️",
        custom_id=TICKET_DELETE_CUSTOM_ID,
    )
    async def delete_ticket(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.ticket_action(interaction, "delete")


class TicketPanelItemSelect(discord.ui.Select):
    def __init__(self, cog, panel_id: int, items: list[dict], placeholder: str):
        options = [
            discord.SelectOption(
                label=item["label"][:100],
                value=str(item["category_id"]),
                description=(item.get("description") or None),
                emoji=item.get("emoji") or None,
            )
            for item in items[:25]
        ]
        super().__init__(
            placeholder=(placeholder or "Choose a ticket type")[:150],
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"{TICKET_PANEL_SELECT_PREFIX}{panel_id}",
        )
        self.cog = cog
        self.panel_id = panel_id

    async def callback(self, interaction: discord.Interaction):
        await self.cog.open_ticket(
            interaction,
            category_id=int(self.values[0]),
            panel_id=self.panel_id,
        )


class TicketPanelItemButton(discord.ui.Button):
    def __init__(self, cog, panel_id: int, item: dict, index: int):
        super().__init__(
            label=item["label"][:80],
            emoji=item.get("emoji") or None,
            style=discord.ButtonStyle.primary,
            custom_id=f"{TICKET_PANEL_BUTTON_PREFIX}{panel_id}:{index}",
        )
        self.cog = cog
        self.panel_id = panel_id
        self.category_id = int(item["category_id"])

    async def callback(self, interaction: discord.Interaction):
        await self.cog.open_ticket(
            interaction,
            category_id=self.category_id,
            panel_id=self.panel_id,
        )


class ConfiguredTicketPanelView(discord.ui.View):
    def __init__(
        self,
        cog,
        panel_id: int,
        mode: str,
        items: list[dict],
        placeholder: str,
        link_buttons: list[dict] | None = None,
    ):
        super().__init__(timeout=None)
        if mode == "dropdown":
            self.add_item(TicketPanelItemSelect(cog, panel_id, items, placeholder))
        else:
            for index, item in enumerate(items[:25]):
                self.add_item(TicketPanelItemButton(cog, panel_id, item, index))
        remaining_slots = 25 - len(self.children)
        for button in (link_buttons or [])[:remaining_slots]:
            label = button.get("label") if isinstance(button, dict) else None
            url = button.get("url") if isinstance(button, dict) else None
            if not isinstance(label, str) or not label.strip() or not isinstance(url, str):
                continue
            try:
                parsed = urlsplit(url)
            except ValueError:
                LOGGER.warning("Skipping invalid saved ticket panel button URL for panel %s", panel_id)
                continue
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                LOGGER.warning("Skipping invalid saved ticket panel button URL for panel %s", panel_id)
                continue
            self.add_item(
                discord.ui.Button(
                    label=label[:80],
                    url=url,
                    style=discord.ButtonStyle.link,
                )
            )


class _TicketWizardSelect(discord.ui.Select):
    def __init__(self, wizard, custom_id: str, placeholder: str, options):
        super().__init__(
            custom_id=f"ticket-wizard:{wizard.actor_id}:{custom_id}",
            placeholder=placeholder[:150],
            min_values=1,
            max_values=1,
            options=options[:25],
        )
        self.wizard = wizard
        self.selection = custom_id

    async def callback(self, interaction: discord.Interaction):
        if not await self.wizard._authorized(interaction):
            return
        value = self.values[0]
        if self.selection == "panel":
            if value == _PANEL_SEARCH_VALUE:
                return await interaction.response.send_modal(
                    _TicketWizardSearchModal(self.wizard, "panel", "panels")
                )
            new_panel_id = int(value)
            if new_panel_id != self.wizard.panel_id:
                self.wizard.category_id = None
                self.wizard.panel_channel_id = None
                self.wizard.log_channel_id = None
            self.wizard.panel_id = new_panel_id
        elif self.selection == "category":
            self.wizard.category_id = int(value)
        elif self.selection == "mode":
            await self.wizard.cog._update_ticket_panel(
                self.wizard.guild_id, self.wizard.panel_id, mode=value
            )
        elif self.selection == "panel-channel":
            self.wizard.panel_channel_id = int(value)
        elif self.selection == "log-channel":
            self.wizard.log_channel_id = int(value)
        await self.wizard.refresh(interaction)


class _TicketWizardChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, wizard, selection: str, placeholder: str, channel_types, *, default=None):
        default_values = [discord.Object(id=default)] if default is not None else []
        super().__init__(
            custom_id=f"ticket-wizard:{wizard.actor_id}:{selection}",
            placeholder=placeholder[:150],
            channel_types=channel_types,
            default_values=default_values,
        )
        self.wizard = wizard
        self.selection = selection

    async def callback(self, interaction: discord.Interaction):
        if not await self.wizard._authorized(interaction):
            return
        value = int(interaction.data["values"][0])
        if self.selection == "category":
            self.wizard.category_id = value
        elif self.selection == "panel-channel":
            self.wizard.panel_channel_id = value
        elif self.selection == "log-channel":
            self.wizard.log_channel_id = value
        await self.wizard.refresh(interaction)


class _TicketWizardSearchModal(discord.ui.Modal):
    query = discord.ui.TextInput(
        label="Panel name (blank clears search)",
        max_length=100,
        required=False,
    )

    def __init__(self, wizard, selection: str, label: str):
        super().__init__(title=f"Search {label}"[:45])
        self.wizard = wizard
        self.selection = selection
        self.query.default = wizard.search_queries.get(selection, "")[:100]

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.wizard._authorized(interaction):
            return
        query = str(self.query.value).strip()
        self.wizard.search_queries[self.selection] = query
        await self.wizard._defer_modal(interaction)
        await self.wizard.refresh(
            interaction,
            notice=f"{self.selection.replace('-', ' ').title()} search "
            + (f"set to **{query}**." if query else "cleared."),
        )


class _CreateTicketPanelModal(discord.ui.Modal, title="Create ticket panel"):
    name = discord.ui.TextInput(label="Panel name", max_length=80, placeholder="For example: Support")

    def __init__(self, wizard):
        super().__init__()
        self.wizard = wizard

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.wizard._authorized(interaction):
            return
        await self.wizard._defer_modal(interaction)
        name = str(self.name.value).strip()
        if not name:
            return await self.wizard._respond(interaction, "Panel name cannot be empty.")
        try:
            panel_id = await self.wizard.cog._create_ticket_panel(self.wizard.guild_id, name)
        except ValueError as error:
            return await self.wizard._respond(interaction, str(error))
        self.wizard.panel_id = panel_id
        await self.wizard.refresh(interaction)


class _RenameTicketPanelModal(discord.ui.Modal, title="Rename ticket panel"):
    def __init__(self, wizard, current_name: str):
        super().__init__()
        self.wizard = wizard
        self.old_name = discord.ui.TextInput(
            label="Existing panel name",
            max_length=80,
            default=current_name,
        )
        self.new_name = discord.ui.TextInput(
            label="New panel name",
            max_length=80,
            placeholder=current_name,
        )
        self.add_item(self.old_name)
        self.add_item(self.new_name)

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.wizard._authorized(interaction):
            return
        await self.wizard._defer_modal(interaction)
        changed = await self.wizard.cog._rename_ticket_panel(
            self.wizard.guild_id,
            str(self.old_name.value).strip(),
            str(self.new_name.value).strip(),
        )
        if not changed:
            return await self.wizard._respond(interaction, "That panel name was not found or the new name is invalid.")
        await self.wizard.refresh(interaction)


class _DeleteTicketPanelModal(discord.ui.Modal, title="Delete ticket panel"):
    def __init__(self, wizard, current_name: str):
        super().__init__()
        self.wizard = wizard
        self.name = discord.ui.TextInput(
            label="Panel name to delete",
            max_length=80,
            default=current_name,
        )
        self.add_item(self.name)

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.wizard._authorized(interaction):
            return
        await self.wizard._defer_modal(interaction)
        deleted = await self.wizard.cog._delete_ticket_panel(
            self.wizard.guild_id, str(self.name.value).strip()
        )
        if deleted:
            self.wizard.panel_id = None
        await self.wizard.refresh(interaction, notice="Panel deleted." if deleted else "Panel name was not found.")


class _RemoveTicketCategoryModal(discord.ui.Modal, title="Remove ticket category"):
    number = discord.ui.TextInput(label="Category number to remove", max_length=2, placeholder="1")

    def __init__(self, wizard):
        super().__init__()
        self.wizard = wizard

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.wizard._authorized(interaction):
            return
        await self.wizard._defer_modal(interaction)
        try:
            index = int(self.number.value) - 1
        except ValueError:
            return await self.wizard._respond(interaction, "Enter a valid category number.")
        removed = await self.wizard.cog._remove_ticket_panel_category(
            self.wizard.guild_id, self.wizard.panel_id, index
        )
        await self.wizard.refresh(interaction, notice="Category removed." if removed else "That category number is not set.")


class _TicketPanelItemModal(discord.ui.Modal, title="Add ticket button"):
    label = discord.ui.TextInput(label="Button name", max_length=80)
    emoji = discord.ui.TextInput(label="Emoji (optional)", max_length=100, required=False)

    def __init__(self, wizard):
        super().__init__()
        self.wizard = wizard

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.wizard._authorized(interaction):
            return
        await self.wizard._defer_modal(interaction)
        emoji = str(self.emoji.value).strip()
        if emoji:
            try:
                emoji = str(discord.PartialEmoji.from_str(emoji))
            except (TypeError, ValueError):
                return await self.wizard._respond(interaction, "Enter a valid Unicode or custom emoji.")
        panel = await self.wizard.cog._ticket_panel(
            self.wizard.guild_id, self.wizard.panel_id
        )
        if panel is None:
            return await self.wizard._respond(interaction, "Select a valid panel first.")
        if panel[8] != "button":
            return await self.wizard._respond(interaction, "Choose button mode before adding buttons.")
        category_ids = self.wizard.cog._panel_category_ids(panel)
        items = self.wizard.cog._panel_items(panel)
        if not category_ids:
            return await self.wizard._respond(
                interaction,
                "Add a ticket category on page 2 before creating buttons.",
            )
        category_id = min(
            category_ids,
            key=lambda value: sum(item.get("category_id") == value for item in items),
        )
        added = await self.wizard.cog._add_ticket_panel_item(
            self.wizard.guild_id,
            self.wizard.panel_id,
            {
                "label": str(self.label.value).strip(),
                "description": "",
                "emoji": emoji,
                "category_id": category_id,
            },
        )
        if not added:
            return await self.wizard._respond(interaction, "A panel needs a category and can contain at most 25 options.")
        await self.wizard.refresh(interaction)


class _TicketDropdownOptionModal(discord.ui.Modal, title="Add dropdown option"):
    label = discord.ui.TextInput(label="Option name", max_length=100)
    description = discord.ui.TextInput(
        label="Option description (optional)",
        max_length=100,
        required=False,
    )
    placeholder = discord.ui.TextInput(
        label="Dropdown placeholder",
        max_length=150,
        placeholder="Choose a ticket type",
    )
    emoji = discord.ui.TextInput(
        label="Option emoji (optional)",
        max_length=100,
        required=False,
        placeholder="🎫",
    )

    def __init__(self, wizard, placeholder: str = ""):
        super().__init__()
        self.wizard = wizard
        self.placeholder.default = placeholder[:150]

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.wizard._authorized(interaction):
            return
        emoji = str(self.emoji.value).strip()
        if emoji:
            try:
                emoji = str(discord.PartialEmoji.from_str(emoji))
            except (TypeError, ValueError):
                return await self.wizard._respond(interaction, "Enter a valid Unicode or custom emoji.")
        await self.wizard._defer_modal(interaction)
        await self.wizard._add_dropdown_option(
            interaction,
            str(self.label.value).strip(),
            str(self.description.value).strip(),
            str(self.placeholder.value).strip(),
            emoji,
        )


class _TicketEmbedModal(discord.ui.Modal, title="Edit ticket panel embed"):
    def __init__(self, wizard, data: dict):
        super().__init__()
        self.wizard = wizard
        try:
            color = f"#{int(data.get('color', 0xFFDD00)):06X}"
        except (TypeError, ValueError):
            color = "#FFDD00"
        self.title_input = discord.ui.TextInput(
            label="Embed title",
            max_length=256,
            default=str(data.get("title") or "Open a ticket")[:256],
        )
        self.description = discord.ui.TextInput(
            label="Embed description",
            style=discord.TextStyle.paragraph,
            max_length=4000,
            default=str(data.get("description") or "Choose an option below to create a private ticket.")[:4000],
        )
        self.color = discord.ui.TextInput(
            label="Color hex (optional)",
            max_length=7,
            required=False,
            default=color,
            placeholder="#FFDD00",
        )
        for item in (self.title_input, self.description, self.color):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.wizard._authorized(interaction):
            return
        await self.wizard._defer_modal(interaction)
        color_text = str(self.color.value).strip().removeprefix("#")
        try:
            color = int(color_text, 16) if color_text else 0xFFDD00
            if not 0 <= color <= 0xFFFFFF:
                raise ValueError
        except ValueError:
            return await self.wizard._respond(interaction, "Enter a valid 6-digit hexadecimal color.")
        panel = await self.wizard.cog._ticket_panel(
            self.wizard.guild_id, self.wizard.panel_id
        )
        data = self.wizard.cog._panel_embed(panel) if panel else {}
        data.update({
            "title": str(self.title_input.value).strip(),
            "description": str(self.description.value).strip(),
            "color": color,
        })
        await self.wizard.cog._update_ticket_panel(
            self.wizard.guild_id, self.wizard.panel_id, embed_json=data
        )
        await self.wizard.refresh(interaction)


class _TicketEmbedAuthorModal(discord.ui.Modal, title="Edit embed author"):
    name = discord.ui.TextInput(label="Author text", max_length=256, required=False)
    icon_url = discord.ui.TextInput(
        label="Author image URL (optional)",
        max_length=512,
        required=False,
        placeholder="https://example.com/image.png",
    )

    def __init__(self, wizard, data: dict):
        super().__init__()
        self.wizard = wizard
        self.name.default = str(data.get("author") or "")[:256]
        self.icon_url.default = str(data.get("author_icon_url") or "")[:512]

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.wizard._authorized(interaction):
            return
        values = {
            "author": str(self.name.value).strip(),
            "author_icon_url": str(self.icon_url.value).strip(),
        }
        icon_url = values["author_icon_url"]
        if icon_url:
            try:
                parsed = urlsplit(icon_url)
            except ValueError:
                return await self.wizard._respond(interaction, "Author image URL must be a valid HTTP or HTTPS URL.")
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                return await self.wizard._respond(interaction, "Author image URL must be a valid HTTP or HTTPS URL.")
        await self.wizard._defer_modal(interaction)
        panel = await self.wizard.cog._ticket_panel(self.wizard.guild_id, self.wizard.panel_id)
        if panel is None:
            return await self.wizard._respond(interaction, "Select a valid panel first.")
        data = self.wizard.cog._panel_embed(panel)
        data.update(values)
        await self.wizard.cog._update_ticket_panel(
            self.wizard.guild_id, self.wizard.panel_id, embed_json=data
        )
        await self.wizard.refresh(interaction, notice="Embed author saved.")


class _TicketEmbedFooterModal(discord.ui.Modal, title="Edit embed footer"):
    text = discord.ui.TextInput(label="Footer text", max_length=2048, required=False)
    icon_url = discord.ui.TextInput(
        label="Footer image URL (optional)",
        max_length=512,
        required=False,
        placeholder="https://example.com/image.png",
    )
    timestamp = discord.ui.TextInput(
        label="Timestamp (yes or no)",
        max_length=3,
        required=False,
        default="no",
        placeholder="yes or no",
    )

    def __init__(self, wizard, data: dict):
        super().__init__()
        self.wizard = wizard
        self.text.default = str(data.get("footer") or "")[:2048]
        self.icon_url.default = str(data.get("footer_icon_url") or "")[:512]
        self.timestamp.default = "yes" if data.get("timestamp") else "no"

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.wizard._authorized(interaction):
            return
        timestamp = str(self.timestamp.value).strip().casefold()
        if timestamp not in {"yes", "no"}:
            return await self.wizard._respond(interaction, "Timestamp must be yes or no.")
        icon_url = str(self.icon_url.value).strip()
        if icon_url:
            try:
                parsed = urlsplit(icon_url)
            except ValueError:
                return await self.wizard._respond(interaction, "Footer image URL must be a valid HTTP or HTTPS URL.")
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                return await self.wizard._respond(interaction, "Footer image URL must be a valid HTTP or HTTPS URL.")
        await self.wizard._defer_modal(interaction)
        panel = await self.wizard.cog._ticket_panel(self.wizard.guild_id, self.wizard.panel_id)
        if panel is None:
            return await self.wizard._respond(interaction, "Select a valid panel first.")
        data = self.wizard.cog._panel_embed(panel)
        data.update({
            "footer": str(self.text.value).strip(),
            "footer_icon_url": icon_url,
            "timestamp": timestamp == "yes",
        })
        await self.wizard.cog._update_ticket_panel(
            self.wizard.guild_id, self.wizard.panel_id, embed_json=data
        )
        await self.wizard.refresh(interaction, notice="Embed footer saved.")


class _TicketEmbedImagesModal(discord.ui.Modal, title="Edit embed images"):
    def __init__(self, wizard, data: dict):
        super().__init__()
        self.wizard = wizard
        self.image_url = discord.ui.TextInput(
            label="Main image URL (optional)",
            max_length=512,
            required=False,
            default=str(data.get("image_url") or "")[:512],
        )
        self.thumbnail_url = discord.ui.TextInput(
            label="Thumbnail URL (optional)",
            max_length=512,
            required=False,
            default=str(data.get("thumbnail_url") or "")[:512],
        )
        for item in (self.image_url, self.thumbnail_url):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.wizard._authorized(interaction):
            return
        await self.wizard._defer_modal(interaction)
        values = {
            "image_url": str(self.image_url.value).strip(),
            "thumbnail_url": str(self.thumbnail_url.value).strip(),
        }
        for label, value in values.items():
            if not value:
                continue
            try:
                parsed = urlsplit(value)
            except ValueError:
                return await self.wizard._respond(interaction, f"Enter a valid HTTP or HTTPS {label.replace('_', ' ')}.")
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                return await self.wizard._respond(interaction, f"Enter a valid HTTP or HTTPS {label.replace('_', ' ')}.")
        panel = await self.wizard.cog._ticket_panel(self.wizard.guild_id, self.wizard.panel_id)
        if panel is None:
            return await self.wizard._respond(interaction, "Select a valid panel first.")
        data = self.wizard.cog._panel_embed(panel)
        data.update(values)
        await self.wizard.cog._update_ticket_panel(
            self.wizard.guild_id, self.wizard.panel_id, embed_json=data
        )
        await self.wizard.refresh(interaction, notice="Embed images saved.")


class _TicketEmbedButtonModal(discord.ui.Modal, title="Create embed button"):
    label = discord.ui.TextInput(label="Button name", max_length=80)
    url = discord.ui.TextInput(
        label="Button link",
        max_length=512,
        placeholder="https://example.com",
    )

    def __init__(self, wizard):
        super().__init__()
        self.wizard = wizard

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.wizard._authorized(interaction):
            return
        label = str(self.label.value).strip()
        url = str(self.url.value).strip()
        try:
            parsed = urlsplit(url)
        except ValueError:
            return await self.wizard._respond(interaction, "Button link must be a valid HTTP or HTTPS URL.")
        if not label or parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return await self.wizard._respond(interaction, "Enter a button name and a valid HTTP or HTTPS link.")
        await self.wizard._defer_modal(interaction)
        panel = await self.wizard.cog._ticket_panel(self.wizard.guild_id, self.wizard.panel_id)
        if panel is None:
            return await self.wizard._respond(interaction, "Select a valid panel first.")
        data = self.wizard.cog._panel_embed(panel)
        buttons = data.get("buttons")
        if not isinstance(buttons, list):
            buttons = []
        if len(buttons) >= 18:
            return await self.wizard._respond(interaction, "A panel can have at most 18 custom link buttons.")
        if any(str(button.get("label", "")).casefold() == label.casefold() for button in buttons if isinstance(button, dict)):
            return await self.wizard._respond(interaction, f"A button named `{label}` already exists.")
        category_ids = self.wizard.cog._panel_category_ids(panel)
        ticket_items = [
            item for item in self.wizard.cog._panel_items(panel)
            if item.get("category_id") in category_ids
        ]
        ticket_control_count = (
            1 if panel[8] == "dropdown"
            else len(ticket_items) or len(category_ids)
        )
        if ticket_control_count + len(buttons) >= 25:
            return await self.wizard._respond(interaction, "There is no room for another button on the published panel.")
        buttons.append({"label": label, "url": url})
        data["buttons"] = buttons
        await self.wizard.cog._update_ticket_panel(
            self.wizard.guild_id, self.wizard.panel_id, embed_json=data
        )
        await self.wizard.refresh(interaction, notice="Embed button created.")


class _TicketRemoveEmbedButtonModal(discord.ui.Modal, title="Remove embed button"):
    label = discord.ui.TextInput(label="Button name", max_length=80)

    def __init__(self, wizard):
        super().__init__()
        self.wizard = wizard

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.wizard._authorized(interaction):
            return
        label = str(self.label.value).strip()
        await self.wizard._defer_modal(interaction)
        panel = await self.wizard.cog._ticket_panel(self.wizard.guild_id, self.wizard.panel_id)
        if panel is None:
            return await self.wizard._respond(interaction, "Select a valid panel first.")
        data = self.wizard.cog._panel_embed(panel)
        buttons = data.get("buttons")
        if not isinstance(buttons, list):
            buttons = []
        index = next(
            (
                i for i, button in enumerate(buttons)
                if isinstance(button, dict)
                and str(button.get("label", "")).casefold() == label.casefold()
            ),
            None,
        )
        if index is None:
            return await self.wizard._respond(interaction, f"No button named `{label}` was found.")
        buttons.pop(index)
        data["buttons"] = buttons
        await self.wizard.cog._update_ticket_panel(
            self.wizard.guild_id, self.wizard.panel_id, embed_json=data
        )
        await self.wizard.refresh(interaction, notice="Embed button removed.")


class _TicketEmbedFieldModal(discord.ui.Modal, title="Add embed field"):
    name = discord.ui.TextInput(label="Field name", max_length=256)
    value = discord.ui.TextInput(label="Field value", max_length=1024)
    inline = discord.ui.TextInput(
        label="Inline? (yes or no)",
        max_length=3,
        required=False,
        default="no",
    )

    def __init__(self, wizard):
        super().__init__()
        self.wizard = wizard

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.wizard._authorized(interaction):
            return
        name = str(self.name.value).strip()
        value = str(self.value.value).strip()
        inline_text = str(self.inline.value).strip().casefold()
        if not name or not value:
            return await self.wizard._respond(interaction, "Embed field name and value cannot be empty.")
        if inline_text not in {"", "yes", "no", "true", "false"}:
            return await self.wizard._respond(interaction, "Inline must be yes or no.")
        await self.wizard._defer_modal(interaction)
        panel = await self.wizard.cog._ticket_panel(self.wizard.guild_id, self.wizard.panel_id)
        if panel is None:
            return await self.wizard._respond(interaction, "Select a valid panel first.")
        data = self.wizard.cog._panel_embed(panel)
        fields = data.get("fields")
        if not isinstance(fields, list):
            fields = []
        if len(fields) >= 24:
            return await self.wizard._respond(
                interaction,
                "The panel name uses one embed field, so up to 24 custom fields are available.",
            )
        fields.append({"name": name, "value": value, "inline": inline_text in {"yes", "true"}})
        data["fields"] = fields
        await self.wizard.cog._update_ticket_panel(
            self.wizard.guild_id, self.wizard.panel_id, embed_json=data
        )
        await self.wizard.refresh(interaction, notice="Embed field added.")


class _RemoveTicketEmbedFieldModal(discord.ui.Modal, title="Remove embed field"):
    number = discord.ui.TextInput(label="Field number to remove", max_length=2, placeholder="1")

    def __init__(self, wizard):
        super().__init__()
        self.wizard = wizard

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.wizard._authorized(interaction):
            return
        try:
            index = int(self.number.value) - 1
        except ValueError:
            return await self.wizard._respond(interaction, "Enter a valid field number.")
        await self.wizard._defer_modal(interaction)
        panel = await self.wizard.cog._ticket_panel(self.wizard.guild_id, self.wizard.panel_id)
        if panel is None:
            return await self.wizard._respond(interaction, "Select a valid panel first.")
        data = self.wizard.cog._panel_embed(panel)
        fields = data.get("fields")
        if not isinstance(fields, list) or not 0 <= index < len(fields):
            return await self.wizard._respond(interaction, "That embed field number is not set.")
        fields.pop(index)
        data["fields"] = fields
        await self.wizard.cog._update_ticket_panel(
            self.wizard.guild_id, self.wizard.panel_id, embed_json=data
        )
        await self.wizard.refresh(interaction, notice="Embed field removed.")


class _RemoveTicketPanelItemModal(discord.ui.Modal, title="Remove panel option"):
    number = discord.ui.TextInput(label="Option number to remove", max_length=2, placeholder="1")

    def __init__(self, wizard):
        super().__init__()
        self.wizard = wizard

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.wizard._authorized(interaction):
            return
        await self.wizard._defer_modal(interaction)
        try:
            index = int(self.number.value) - 1
        except ValueError:
            return await self.wizard._respond(interaction, "Enter a valid option number.")
        removed = await self.wizard.cog._remove_ticket_panel_item(
            self.wizard.guild_id, self.wizard.panel_id, index
        )
        await self.wizard.refresh(interaction, notice="Option removed." if removed else "That option number is not set.")


class TicketSetupWizard(discord.ui.View):
    """Author-scoped guided ticket panel builder; each edit is persisted immediately."""

    def __init__(self, cog, guild: discord.Guild, actor_id: int, *, page: int = 0):
        super().__init__(timeout=900)
        self.cog = cog
        self.guild_id = guild.id
        self.actor_id = actor_id
        self.page = page
        self.panel_id = None
        self.category_id = None
        self.panel_channel_id = None
        self.log_channel_id = None
        self.message = None
        self.search_queries: dict[str, str] = {}

    async def _authorized(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.actor_id:
            return True
        await self.cog._send_interaction(interaction, "Only the administrator who opened this ticket setup can use it.")
        return False

    async def _respond(self, interaction: discord.Interaction, content: str):
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)

    async def _defer_modal(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

    async def refresh(self, interaction: discord.Interaction, *, notice: str | None = None):
        embed, view = await self.render(notice=notice)
        if interaction.type == discord.InteractionType.modal_submit:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            if self.message is not None:
                await edit_message_v2(self.message, embed=embed, view=view)
            else:
                layout = make_layout_view(embed=embed, legacy_view=view)
                await interaction.followup.send(view=layout, ephemeral=True)
            return
        if interaction.response.is_done():
            message = getattr(interaction, "message", None) or self.message
            if message is not None:
                await edit_message_v2(message, embed=embed, view=view)
            else:
                await interaction.edit_original_response(
                    view=make_layout_view(embed=embed, legacy_view=view)
                )
        else:
            await edit_interaction_v2(interaction, embed=embed, view=view)

    async def render(self, *, notice: str | None = None):
        self.clear_items()
        guild = self.cog.bot.get_guild(self.guild_id)
        panel_rows = await self.cog._ticket_panels(self.guild_id)
        if self.panel_id not in {row[0] for row in panel_rows}:
            self.panel_id = panel_rows[0][0] if panel_rows else None
        page_titles = (
            "1 · Ticket panels",
            "2 · Ticket categories",
            "3 · Panel type",
            "4 · Button and dropdown options",
            "5 · Panel embed",
            "6 · Publish panel",
        )
        embed = discord.Embed(title=f"Ticket setup · {page_titles[self.page]}", color=0xFFDD00)
        if self.page == 0:
            embed.add_field(
                name="Saved panels",
                value="\n".join(
                    f"{index}. **{row[1]}**" + (" · selected" if row[0] == self.panel_id else "")
                    for index, row in enumerate(panel_rows, 1)
                ) or "No panels yet. Create one to begin.",
                inline=False,
            )
            if panel_rows:
                self.add_item(self._panel_select(panel_rows))
            self.add_item(self._button("Create panel", "create", discord.ButtonStyle.success))
            self.add_item(self._button("Rename panel", "rename"))
            self.add_item(self._button("Delete panel", "delete", discord.ButtonStyle.danger))
        else:
            row = await self.cog._ticket_panel(self.guild_id, self.panel_id) if self.panel_id else None
            if row is None:
                embed.description = "Create a panel on the first page before configuring it."
            else:
                if self.panel_channel_id is None:
                    self.panel_channel_id = row[5]
                if self.log_channel_id is None:
                    self.log_channel_id = row[7]
                if self.page == 1:
                    categories = self.cog._guild_categories(guild)
                    category_ids = self.cog._panel_category_ids(row)
                    embed.description = (
                        "Add ticket categories to the selected panel; closed tickets "
                        "can be moved to an optional close category."
                    )
                    embed.add_field(
                        name="Added categories",
                        value="\n".join(
                            f"{i}. {guild.get_channel(category_id).name}"
                            for i, category_id in enumerate(category_ids, 1)
                            if guild.get_channel(category_id)
                        ) or "None",
                        inline=False,
                    )
                    close_name = getattr(guild.get_channel(row[4]), "name", None) if row[4] else None
                    embed.add_field(
                        name="Close category",
                        value=close_name or "No close category",
                        inline=False,
                    )
                    if categories:
                        self.add_item(self._options_select(
                            "category",
                            "Choose ticket category",
                            categories,
                            default=self.category_id,
                        ))
                        self.add_item(self._button("Add category", "add-category", discord.ButtonStyle.success))
                        self.add_item(self._button("Remove category number", "remove-category", discord.ButtonStyle.danger))
                        self.add_item(self._button("Use selected as close category", "set-close-category"))
                        if row[4]:
                            self.add_item(self._button("Clear close category", "clear-close-category"))
                    else:
                        embed.description = "Select a panel. Create a Discord category before adding ticket categories."
                elif self.page == 2:
                    mode = row[8]
                    embed.description = f"Current panel type: **{mode}**. Choose how members open tickets."
                    self.add_item(self._raw_select(
                        "mode",
                        "Choose panel type",
                        [("dropdown", "Dropdown menu"), ("button", "Buttons")],
                        default=mode,
                    ))
                elif self.page == 3:
                    category_ids = self.cog._panel_category_ids(row)
                    configured_items = self.cog._panel_items(row)
                    available_categories = self._available_dropdown_categories(row)
                    items = configured_items
                    if row[8] == "dropdown" and not items:
                        items = [
                            {
                                "label": getattr(guild.get_channel(category_id), "name", "Open Ticket"),
                                "emoji": "",
                                "category_id": category_id,
                            }
                            for category_id in category_ids
                        ]
                    embed.description = (
                        "Add dropdown options with a name, optional description and emoji, and a menu placeholder. "
                        "Each option automatically uses the next unused page-2 category."
                        if row[8] == "dropdown"
                        else "Add buttons using only a label and emoji. Buttons are routed to this panel's categories automatically."
                    )
                    embed.add_field(
                        name="Current buttons" if row[8] == "button" else "Dropdown options",
                        value="\n".join(
                            f"{i}. {item.get('emoji', '')} **{item['label']}**"
                            + (f" — {item['description']}" if item.get("description") else "")
                            + " → "
                            f"{getattr(guild.get_channel(int(item['category_id'])), 'name', 'missing category')}"
                            for i, item in enumerate(items, 1)
                        ) or "None",
                        inline=False,
                    )
                    if row[8] == "button":
                        self.add_item(self._button("Add option", "add-item", discord.ButtonStyle.success))
                        if items:
                            self.add_item(self._button("Remove option number", "remove-item", discord.ButtonStyle.danger))
                    else:
                        if available_categories or not category_ids:
                            self.add_item(self._button("Add option", "add-dropdown-option", discord.ButtonStyle.success))
                        elif category_ids:
                            embed.description += " All configured categories already have an option."
                        if configured_items:
                            self.add_item(self._button("Remove option number", "remove-item", discord.ButtonStyle.danger))
                elif self.page == 4:
                    embed = self.cog._ticket_panel_embed_object(row)
                    embed.add_field(
                        name="Panel embed preview · step 5 of 6",
                        value="Edit text, color, and images. Saved changes update a published panel immediately.",
                        inline=False,
                    )
                    self.add_item(self._button("Edit Basic Information", "edit-embed", discord.ButtonStyle.primary))
                    self.add_item(self._button("Edit Author", "edit-embed-author"))
                    self.add_item(self._button("Edit Footer", "edit-embed-footer"))
                    self.add_item(self._button("Edit Images", "edit-embed-images"))
                    if self.cog._panel_embed(row).get("fields"):
                        self.add_item(self._button("Remove field", "remove-embed-field", discord.ButtonStyle.danger))
                elif self.page == 5:
                    embed.description = "Choose where the public panel and ticket transcripts will be posted."
                    text_channels = [
                        channel for channel in getattr(guild, "text_channels", [])
                        if isinstance(channel, discord.TextChannel)
                    ]
                    if text_channels:
                        self.add_item(self._options_select(
                            "panel-channel",
                            "Choose panel channel",
                            text_channels,
                            default=self.panel_channel_id,
                        ))
                        self.add_item(self._options_select(
                            "log-channel",
                            "Choose transcript channel",
                            text_channels,
                            default=self.log_channel_id,
                        ))
                        self.add_item(self._button("Send panel", "publish", discord.ButtonStyle.success))

        self.add_item(self._button("Previous", "previous"))
        self.add_item(self._button("Next", "next", discord.ButtonStyle.primary))
        if notice:
            description = embed.description or ""
            embed.description = f"{description}\n\n{notice}".strip()
        return embed, self

    def _panel_select(self, rows):
        return self._raw_select(
            "panel",
            "Choose a panel",
            [(str(row[0]), row[1]) for row in rows],
            default=self.panel_id,
        )

    def _available_dropdown_categories(self, panel):
        used_category_ids = {
            int(item["category_id"])
            for item in self.cog._panel_items(panel)
            if item.get("category_id") is not None
        }
        guild = self.cog.bot.get_guild(self.guild_id)
        categories = []
        for category_id in self.cog._panel_category_ids(panel):
            if category_id in used_category_ids:
                continue
            category = guild.get_channel(category_id) if guild else None
            if category is not None:
                categories.append((category_id, category.name))
        return categories

    def _options_select(self, selection, placeholder, channels, *, default=None):
        if selection == "category":
            channel_types = [discord.ChannelType.category]
        else:
            channel_types = [discord.ChannelType.text, discord.ChannelType.news]
        return _TicketWizardChannelSelect(
            self,
            selection,
            placeholder,
            channel_types,
            default=default,
        )

    def _raw_select(self, selection, placeholder, options, *, default=None):
        query = self.search_queries.get(selection, "").casefold()
        matching_options = [
            (value, label)
            for value, label in options
            if not query or query in label.casefold()
        ][:24 if selection == "panel" else 25]
        if selection == "panel":
            matching_options.append((_PANEL_SEARCH_VALUE, "Search panel names..."))
        disabled = not matching_options
        if disabled:
            matching_options = [("__no_matches__", "No matches; change search")]
        select = _TicketWizardSelect(
            self,
            selection,
            placeholder,
            [
                discord.SelectOption(
                    label=label[:100],
                    value=value,
                    default=value == str(default),
                )
                for value, label in matching_options
            ],
        )
        select.disabled = disabled
        return select

    def _button(self, label, action, style=discord.ButtonStyle.secondary):
        button = discord.ui.Button(
            label=label,
            style=style,
            custom_id=f"ticket-wizard:{self.actor_id}:button:{action}",
        )
        button.callback = lambda interaction: self._button_callback(interaction, action)
        return button

    async def _button_callback(self, interaction: discord.Interaction, action: str):
        if not await self._authorized(interaction):
            return
        if action == "previous":
            self.page = max(0, self.page - 1)
        elif action == "next":
            self.page = min(5, self.page + 1)
        elif action == "create":
            return await interaction.response.send_modal(_CreateTicketPanelModal(self))
        elif action == "rename":
            if not self.panel_id:
                return await self._respond(interaction, "Create or select a panel first.")
            panel = await self.cog._ticket_panel(self.guild_id, self.panel_id)
            if panel is None:
                return await self._respond(interaction, "That panel is no longer available.")
            return await interaction.response.send_modal(
                _RenameTicketPanelModal(self, panel[1])
            )
        elif action == "delete":
            if not self.panel_id:
                return await self._respond(interaction, "Create or select a panel first.")
            panel = await self.cog._ticket_panel(self.guild_id, self.panel_id)
            if panel is None:
                return await self._respond(interaction, "That panel is no longer available.")
            return await interaction.response.send_modal(
                _DeleteTicketPanelModal(self, panel[1])
            )
        elif action == "add-category":
            if self.category_id is None:
                return await self._respond(interaction, "Choose a ticket category first.")
            added = await self.cog._add_ticket_panel_category(
                self.guild_id, self.panel_id, self.category_id
            )
            await self.refresh(
                interaction,
                notice="Category added." if added else "Could not add that category; the panel may already have 25 categories.",
            )
            return
        elif action == "set-close-category":
            if self.category_id is None:
                return await self._respond(interaction, "Choose a category first.")
            await self.cog._update_ticket_panel(
                self.guild_id,
                self.panel_id,
                close_category_id=self.category_id,
            )
            await self.refresh(interaction, notice="Close category saved.")
            return
        elif action == "clear-close-category":
            await self.cog._update_ticket_panel(
                self.guild_id,
                self.panel_id,
                close_category_id=None,
            )
            await self.refresh(interaction, notice="Close category cleared.")
            return
        elif action == "remove-category":
            return await interaction.response.send_modal(_RemoveTicketCategoryModal(self))
        elif action == "add-item":
            return await interaction.response.send_modal(_TicketPanelItemModal(self))
        elif action == "add-dropdown-option":
            panel = await self.cog._ticket_panel(self.guild_id, self.panel_id)
            if panel is None or panel[8] != "dropdown":
                return await self._respond(interaction, "Select a dropdown panel first.")
            if not self.cog._panel_category_ids(panel):
                return await self._respond(
                    interaction,
                    "Add a ticket category on page 2 before creating dropdown options.",
                )
            categories = self._available_dropdown_categories(panel)
            if not categories:
                return await self._respond(
                    interaction,
                    "Every configured category already has an option. Add a category on page 2 or remove an option first.",
                )
            placeholder = self.cog._panel_embed(panel).get("placeholder", "")
            return await interaction.response.send_modal(
                _TicketDropdownOptionModal(self, placeholder)
            )
        elif action == "remove-item":
            return await interaction.response.send_modal(_RemoveTicketPanelItemModal(self))
        elif action == "edit-embed":
            panel = await self.cog._ticket_panel(self.guild_id, self.panel_id)
            return await interaction.response.send_modal(
                _TicketEmbedModal(self, self.cog._panel_embed(panel) if panel else {})
            )
        elif action == "edit-embed-author":
            panel = await self.cog._ticket_panel(self.guild_id, self.panel_id)
            return await interaction.response.send_modal(
                _TicketEmbedAuthorModal(self, self.cog._panel_embed(panel) if panel else {})
            )
        elif action == "edit-embed-footer":
            panel = await self.cog._ticket_panel(self.guild_id, self.panel_id)
            return await interaction.response.send_modal(
                _TicketEmbedFooterModal(self, self.cog._panel_embed(panel) if panel else {})
            )
        elif action == "edit-embed-images":
            panel = await self.cog._ticket_panel(self.guild_id, self.panel_id)
            return await interaction.response.send_modal(
                _TicketEmbedImagesModal(self, self.cog._panel_embed(panel) if panel else {})
            )
        elif action == "create-embed-button":
            return await interaction.response.send_modal(_TicketEmbedButtonModal(self))
        elif action == "remove-embed-button":
            return await interaction.response.send_modal(_TicketRemoveEmbedButtonModal(self))
        elif action == "add-embed-field":
            return await interaction.response.send_modal(_TicketEmbedFieldModal(self))
        elif action == "remove-embed-field":
            return await interaction.response.send_modal(_RemoveTicketEmbedFieldModal(self))
        elif action == "publish":
            result = await self.cog._publish_ticket_panel(
                self.guild_id,
                self.panel_id,
                self.panel_channel_id,
                self.log_channel_id,
            )
            if isinstance(result, str):
                return await self.refresh(interaction, notice=f"Could not publish panel: {result}")
            return await self.refresh(interaction, notice=f"Panel posted in <#{result}> and saved.")
        await self.refresh(interaction)

    async def _add_dropdown_option(
        self,
        interaction: discord.Interaction,
        label: str,
        description: str,
        placeholder: str,
        emoji: str,
    ):
        if not label:
            return await self._respond(interaction, "Option name cannot be empty.")
        if not placeholder:
            return await self._respond(interaction, "Dropdown placeholder cannot be empty.")
        panel = await self.cog._ticket_panel(self.guild_id, self.panel_id)
        if panel is None or panel[8] != "dropdown":
            return await self._respond(interaction, "Select a dropdown panel first.")
        category_ids = self.cog._panel_category_ids(panel)
        if not category_ids:
            return await self._respond(interaction, "Add a ticket category on page 2 first.")
        available_categories = self._available_dropdown_categories(panel)
        if not available_categories:
            return await self._respond(
                interaction,
                "Every configured category already has an option. Add a category on page 2 or remove an option first.",
            )
        category_id = available_categories[0][0]
        added = await self.cog._add_ticket_panel_item(
            self.guild_id,
            self.panel_id,
            {
                "label": label,
                "description": description,
                "emoji": emoji,
                "category_id": category_id,
                "placeholder": placeholder,
            },
        )
        if not added:
            return await self._respond(
                interaction,
                "Could not add that option; a dropdown can contain at most 25 options.",
            )
        await self.refresh(interaction, notice="Dropdown option added.")


class DenycordInteractionFeatures(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._setup_lock = asyncio.Lock()
        self._ticket_open_lock = asyncio.Lock()

    async def cog_load(self):
        db = self.bot.db
        await db.execute(
            "CREATE TABLE IF NOT EXISTS denycord_ticket_config ("
            "guild_id INTEGER PRIMARY KEY, category_id INTEGER NOT NULL, "
            "panel_channel_id INTEGER NOT NULL, panel_message_id INTEGER NOT NULL, "
            "log_channel_id INTEGER NOT NULL, setup_by INTEGER NOT NULL, created_at TEXT NOT NULL)"
        )
        async with db.execute("PRAGMA table_info(denycord_ticket_config)") as cursor:
            config_columns = {row[1] for row in await cursor.fetchall()}
        for column, definition in (
            ("panel_mode", "TEXT NOT NULL DEFAULT 'button'"),
            ("category_ids", "TEXT NOT NULL DEFAULT '[]'"),
            ("ticket_limit", "INTEGER NOT NULL DEFAULT 1"),
            ("support_role_id", "INTEGER"),
            ("button_style", "TEXT NOT NULL DEFAULT 'grey'"),
            ("button_label", "TEXT NOT NULL DEFAULT 'Open a Ticket'"),
            ("button_emoji", "TEXT NOT NULL DEFAULT '🎫'"),
        ):
            if column not in config_columns:
                await db.execute(
                    f"ALTER TABLE denycord_ticket_config ADD COLUMN {column} {definition}"
                )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS denycord_tickets ("
            "channel_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, owner_id INTEGER NOT NULL, "
            "opened_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', claimed_by INTEGER, "
            "closed_at TEXT, closed_by INTEGER)"
        )
        async with db.execute("PRAGMA table_info(denycord_tickets)") as cursor:
            ticket_columns = {row[1] for row in await cursor.fetchall()}
        if "claimed_by" not in ticket_columns:
            await db.execute("ALTER TABLE denycord_tickets ADD COLUMN claimed_by INTEGER")
        if "panel_id" not in ticket_columns:
            await db.execute("ALTER TABLE denycord_tickets ADD COLUMN panel_id INTEGER")
        await db.execute(
            "CREATE TABLE IF NOT EXISTS denycord_ticket_panels ("
            "panel_id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, "
            "name TEXT NOT NULL, category_ids TEXT NOT NULL DEFAULT '[]', "
            "close_category_id INTEGER, panel_channel_id INTEGER, panel_message_id INTEGER, "
            "log_channel_id INTEGER, mode TEXT NOT NULL DEFAULT 'dropdown', "
            "embed_json TEXT NOT NULL DEFAULT '{}', items_json TEXT NOT NULL DEFAULT '[]', "
            "created_at TEXT NOT NULL)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS denycord_ticket_panels_guild "
            "ON denycord_ticket_panels(guild_id, panel_id)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS denycord_ticket_panel_migrations ("
            "guild_id INTEGER NOT NULL, legacy_panel_message_id INTEGER NOT NULL, "
            "PRIMARY KEY(guild_id, legacy_panel_message_id))"
        )
        async with db.execute(
            "SELECT guild_id, category_id, panel_channel_id, panel_message_id, log_channel_id, "
            "panel_mode, category_ids FROM denycord_ticket_config"
        ) as cursor:
            legacy_panels = await cursor.fetchall()
        for guild_id, category_id, panel_channel_id, panel_message_id, log_channel_id, mode, raw_ids in legacy_panels:
            existing_panel = await self._fetchone(
                "SELECT 1 FROM denycord_ticket_panel_migrations "
                "WHERE guild_id=? AND legacy_panel_message_id=?",
                (guild_id, panel_message_id),
            )
            if existing_panel:
                continue
            try:
                category_ids = [int(value) for value in json.loads(raw_ids or "[]")]
            except (json.JSONDecodeError, TypeError, ValueError):
                category_ids = []
            if category_id not in category_ids:
                category_ids.insert(0, category_id)
            items = [
                {
                    "label": "Open Ticket",
                    "description": "",
                    "emoji": "",
                    "category_id": value,
                }
                for value in category_ids[:25]
            ]
            await db.execute(
                "INSERT INTO denycord_ticket_panels "
                "(guild_id, name, category_ids, panel_channel_id, panel_message_id, log_channel_id, "
                "mode, embed_json, items_json, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    guild_id,
                    "Support",
                    json.dumps(category_ids),
                    panel_channel_id,
                    panel_message_id,
                    log_channel_id,
                    mode if mode in {"button", "dropdown"} else "button",
                    json.dumps({
                        "title": "Support Tickets",
                        "description": "Choose a category below to create a private support channel.",
                        "author": "",
                        "footer": "",
                        "color": 0xFFDD00,
                        "placeholder": "Choose a ticket type",
                    }),
                    json.dumps(items),
                    _now(),
                ),
            )
            await db.execute(
                "INSERT OR IGNORE INTO denycord_ticket_panel_migrations "
                "(guild_id, legacy_panel_message_id) VALUES(?,?)",
                (guild_id, panel_message_id),
            )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS denycord_tickets_guild_status "
            "ON denycord_tickets(guild_id, status)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS denycord_ticket_templates ("
            "guild_id INTEGER NOT NULL, name TEXT NOT NULL, content TEXT NOT NULL, "
            "updated_by INTEGER NOT NULL, updated_at TEXT NOT NULL, "
            "PRIMARY KEY(guild_id, name))"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS denycord_reaction_roles ("
            "guild_id INTEGER NOT NULL, message_id INTEGER NOT NULL, emoji TEXT NOT NULL, "
            "role_id INTEGER NOT NULL, created_by INTEGER NOT NULL, created_at TEXT NOT NULL, "
            "PRIMARY KEY(guild_id, message_id, emoji))"
        )
        await db.commit()
        self.bot.add_view(TicketPanelView(self))
        self.bot.add_view(TicketCategorySelectView(self))
        self.bot.add_view(TicketActionsView(self))
        await self._restore_ticket_panel_views()
        await self._restore_configured_ticket_panel_views()

    async def _restore_ticket_panel_views(self):
        async with self.bot.db.execute(
            "SELECT panel_message_id, category_id, panel_mode, category_ids, "
            "button_style, button_label, button_emoji FROM denycord_ticket_config"
        ) as cursor:
            rows = await cursor.fetchall()
        for panel_message_id, category_id, mode, raw_category_ids, button_style, button_label, button_emoji in rows:
            try:
                category_ids = [int(value) for value in json.loads(raw_category_ids or "[]")]
            except (json.JSONDecodeError, TypeError, ValueError):
                LOGGER.warning("Invalid ticket category configuration for panel %s", panel_message_id)
                category_ids = []
            if category_id not in category_ids:
                category_ids.insert(0, category_id)
            if mode == "dropdown":
                view = TicketCategorySelectView(self)
            elif len(category_ids) > 1:
                style = TICKET_BUTTON_STYLES.get(button_style, discord.ButtonStyle.secondary)
                view = TicketCategoryButtonsView(
                    self,
                    [(value, "Open Ticket") for value in category_ids],
                    style=style,
                    emoji=button_emoji,
                )
            else:
                style = TICKET_BUTTON_STYLES.get(button_style, discord.ButtonStyle.secondary)
                view = TicketPanelView(
                    self,
                    label=button_label,
                    emoji=button_emoji,
                    style=style,
                )
            self.bot.add_view(view, message_id=panel_message_id)

    async def _restore_configured_ticket_panel_views(self):
        async with self.bot.db.execute(
            "SELECT panel_id, panel_message_id, mode, embed_json, items_json "
            "FROM denycord_ticket_panels WHERE panel_message_id IS NOT NULL"
        ) as cursor:
            rows = await cursor.fetchall()
        for panel_id, message_id, mode, raw_embed, raw_items in rows:
            try:
                embed_data = json.loads(raw_embed or "{}")
                items = json.loads(raw_items or "[]")
                if not isinstance(embed_data, dict) or not isinstance(items, list):
                    raise ValueError("Invalid panel data")
                self.bot.add_view(
                    ConfiguredTicketPanelView(
                        self,
                        panel_id,
                        mode,
                        items,
                        embed_data.get("placeholder", "Choose a ticket type"),
                        embed_data.get("buttons"),
                    ),
                    message_id=message_id,
                )
            except (json.JSONDecodeError, TypeError, ValueError):
                LOGGER.warning("Invalid saved ticket panel %s; persistent controls were not restored", panel_id)

    async def _fetchone(self, query, parameters=()):
        async with self.bot.db.execute(query, parameters) as cursor:
            return await cursor.fetchone()

    async def _send_ctx(self, ctx: commands.Context, content: str):
        await ctx.send(content, ephemeral=ctx.interaction is not None)

    async def _send_interaction(self, interaction: discord.Interaction, content: str, *, ephemeral=True):
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(content, ephemeral=ephemeral)

    def _bot_member(self, guild: discord.Guild):
        return guild.me or guild.get_member(self.bot.user.id)

    def _staff_overwrites(self, guild: discord.Guild, *, send_messages: bool):
        permissions = discord.PermissionOverwrite(
            view_channel=True,
            read_message_history=True,
            send_messages=send_messages,
        )
        staff_roles = sorted(
            (
                role for role in guild.roles
                if not role.is_default()
                and (role.permissions.administrator or role.permissions.manage_channels)
            ),
            key=lambda role: role.position,
            reverse=True,
        )
        overwrites = {role: permissions for role in staff_roles[:95]}
        return overwrites

    def _ticket_staff_overwrites(
        self,
        guild: discord.Guild,
        *,
        send_messages: bool,
        support_role: discord.Role | None,
    ):
        overwrites = self._staff_overwrites(guild, send_messages=send_messages)
        if support_role is not None and support_role.guild.id == guild.id:
            overwrites[support_role] = discord.PermissionOverwrite(
                view_channel=True,
                read_message_history=True,
                send_messages=send_messages,
            )
        return overwrites

    async def _ticket_record(self, guild_id: int, channel_id: int):
        return await self._fetchone(
            "SELECT owner_id, status FROM denycord_tickets WHERE guild_id=? AND channel_id=?",
            (guild_id, channel_id),
        )

    async def _ticket_config(self, guild_id: int):
        return await self._fetchone(
            "SELECT category_id, panel_channel_id, panel_message_id, log_channel_id, "
            "panel_mode, category_ids, ticket_limit, support_role_id, "
            "button_style, button_label, button_emoji "
            "FROM denycord_ticket_config WHERE guild_id=?",
            (guild_id,),
        )

    async def _ticket_panels(self, guild_id: int):
        async with self.bot.db.execute(
            "SELECT panel_id, name FROM denycord_ticket_panels "
            "WHERE guild_id=? ORDER BY panel_id",
            (guild_id,),
        ) as cursor:
            return await cursor.fetchall()

    async def _ticket_panel(self, guild_id: int, panel_id: int | None):
        if panel_id is None:
            return None
        return await self._fetchone(
            "SELECT panel_id, name, guild_id, category_ids, close_category_id, "
            "panel_channel_id, panel_message_id, log_channel_id, mode, embed_json, items_json "
            "FROM denycord_ticket_panels WHERE guild_id=? AND panel_id=?",
            (guild_id, panel_id),
        )

    @staticmethod
    def _panel_category_ids(panel) -> list[int]:
        try:
            values = json.loads(panel[3] or "[]")
        except (json.JSONDecodeError, TypeError):
            return []
        return [value for value in values if isinstance(value, int) and not isinstance(value, bool)]

    @staticmethod
    def _panel_items(panel) -> list[dict]:
        try:
            values = json.loads(panel[10] or "[]")
        except (json.JSONDecodeError, TypeError):
            return []
        return [
            item for item in values
            if isinstance(item, dict)
            and isinstance(item.get("label"), str)
            and isinstance(item.get("category_id"), int)
        ]

    @staticmethod
    def _panel_embed(panel) -> dict:
        try:
            value = json.loads(panel[9] or "{}")
        except (json.JSONDecodeError, TypeError):
            value = {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _guild_categories(guild):
        return sorted(
            (
                channel for channel in getattr(guild, "categories", [])
                if isinstance(channel, discord.CategoryChannel)
            ),
            key=lambda category: category.position,
        )

    async def _create_ticket_panel(self, guild_id: int, name: str):
        if not name or len(name) > 80:
            raise ValueError("Panel names must contain 1-80 characters.")
        if await self._fetchone(
            "SELECT 1 FROM denycord_ticket_panels WHERE guild_id=? AND lower(name)=lower(?)",
            (guild_id, name),
        ):
            raise ValueError("A panel with that name already exists.")
        cursor = await self.bot.db.execute(
            "INSERT INTO denycord_ticket_panels "
            "(guild_id, name, created_at) VALUES(?,?,?)",
            (guild_id, name, _now()),
        )
        await self.bot.db.commit()
        return cursor.lastrowid

    async def _rename_ticket_panel(self, guild_id: int, old_name: str, new_name: str) -> bool:
        if not old_name or not new_name or len(new_name) > 80:
            return False
        collision = await self._fetchone(
            "SELECT panel_id FROM denycord_ticket_panels "
            "WHERE guild_id=? AND lower(name)=lower(?) AND lower(name)!=lower(?)",
            (guild_id, new_name, old_name),
        )
        if collision:
            return False
        cursor = await self.bot.db.execute(
            "UPDATE denycord_ticket_panels SET name=? "
            "WHERE guild_id=? AND lower(name)=lower(?)",
            (new_name, guild_id, old_name),
        )
        await self.bot.db.commit()
        if cursor.rowcount:
            panel = await self._fetchone(
                "SELECT panel_id FROM denycord_ticket_panels "
                "WHERE guild_id=? AND lower(name)=lower(?)",
                (guild_id, new_name),
            )
            if panel:
                await self._refresh_configured_ticket_panel(guild_id, panel[0])
        return cursor.rowcount > 0

    async def _delete_ticket_panel(self, guild_id: int, name: str) -> bool:
        panel = await self._fetchone(
            "SELECT panel_id, panel_channel_id, panel_message_id FROM denycord_ticket_panels "
            "WHERE guild_id=? AND lower(name)=lower(?)",
            (guild_id, name),
        )
        if not panel:
            return False
        guild = self.bot.get_guild(guild_id)
        if guild and panel[1] and panel[2]:
            channel = guild.get_channel(panel[1])
            if isinstance(channel, discord.TextChannel):
                try:
                    message = await channel.fetch_message(panel[2])
                    await message.delete()
                except discord.NotFound:
                    pass
                except discord.HTTPException as error:
                    LOGGER.warning("Could not delete ticket panel message %s: %s", panel[2], error)
                    raise
        await self.bot.db.execute(
            "DELETE FROM denycord_ticket_panels WHERE guild_id=? AND panel_id=?",
            (guild_id, panel[0]),
        )
        await self.bot.db.commit()
        return True

    async def _update_ticket_panel(
        self,
        guild_id: int,
        panel_id: int | None,
        *,
        sync: bool = True,
        **changes,
    ):
        if panel_id is None:
            return False
        allowed = {"category_ids", "close_category_id", "panel_channel_id", "panel_message_id",
                   "log_channel_id", "mode", "embed_json", "items_json"}
        assignments = []
        values = []
        for key, value in changes.items():
            if key not in allowed:
                raise ValueError(f"Unsupported ticket panel property: {key}")
            if key in {"category_ids", "embed_json", "items_json"}:
                value = json.dumps(value, ensure_ascii=False)
            assignments.append(f"{key}=?")
            values.append(value)
        if not assignments:
            return False
        cursor = await self.bot.db.execute(
            f"UPDATE denycord_ticket_panels SET {', '.join(assignments)} "
            "WHERE guild_id=? AND panel_id=?",
            (*values, guild_id, panel_id),
        )
        await self.bot.db.commit()
        if cursor.rowcount and sync and {"category_ids", "mode", "embed_json", "items_json"} & changes.keys():
            await self._refresh_configured_ticket_panel(guild_id, panel_id)
        return cursor.rowcount > 0

    async def _refresh_configured_ticket_panel(self, guild_id: int, panel_id: int):
        panel = await self._ticket_panel(guild_id, panel_id)
        if not panel or not panel[5] or not panel[6]:
            return False
        guild = self.bot.get_guild(guild_id)
        channel = guild.get_channel(panel[5]) if guild else None
        if not isinstance(channel, discord.TextChannel):
            return False
        category_ids = [
            category_id for category_id in self._panel_category_ids(panel)
            if isinstance(guild.get_channel(category_id), discord.CategoryChannel)
        ]
        items = [
            item for item in self._panel_items(panel)
            if item.get("category_id") in category_ids
        ]
        if not items:
            items = [
                {
                    "label": getattr(guild.get_channel(category_id), "name", "Open Ticket")[:80],
                    "description": "",
                    "emoji": "",
                    "category_id": category_id,
                }
                for category_id in category_ids
            ]
        if not items:
            return False
        embed_data = self._panel_embed(panel)
        view = ConfiguredTicketPanelView(
            self,
            panel_id,
            panel[8],
            items,
            embed_data.get("placeholder", "Choose a ticket type"),
            embed_data.get("buttons"),
        )
        try:
            message = await channel.fetch_message(panel[6])
            await message.edit(embed=self._ticket_panel_embed_object(panel), view=view)
        except discord.NotFound:
            return False
        except discord.HTTPException as error:
            LOGGER.warning("Could not update saved ticket panel %s: %s", panel_id, error)
            return False
        self.bot.add_view(view, message_id=message.id)
        return True

    async def _add_ticket_panel_category(self, guild_id: int, panel_id: int, category_id: int):
        panel = await self._ticket_panel(guild_id, panel_id)
        if not panel:
            return False
        category_ids = self._panel_category_ids(panel)
        if category_id in category_ids:
            return True
        if len(category_ids) >= 25:
            return False
        category_ids.append(category_id)
        await self._update_ticket_panel(guild_id, panel_id, category_ids=category_ids)
        return True

    async def _remove_ticket_panel_category(self, guild_id: int, panel_id: int | None, index: int):
        panel = await self._ticket_panel(guild_id, panel_id)
        if not panel:
            return False
        category_ids = self._panel_category_ids(panel)
        if not 0 <= index < len(category_ids):
            return False
        removed_id = category_ids.pop(index)
        items = [item for item in self._panel_items(panel) if item["category_id"] != removed_id]
        close_category_id = None if panel[4] == removed_id else panel[4]
        await self._update_ticket_panel(
            guild_id,
            panel_id,
            category_ids=category_ids,
            items_json=items,
            close_category_id=close_category_id,
        )
        return True

    async def _add_ticket_panel_item(self, guild_id: int, panel_id: int | None, item: dict):
        panel = await self._ticket_panel(guild_id, panel_id)
        if not panel or not item["label"]:
            return False
        max_label_length = 100 if panel[8] == "dropdown" else 80
        if len(item["label"]) > max_label_length:
            return False
        category_ids = self._panel_category_ids(panel)
        items = self._panel_items(panel)
        if item["category_id"] not in category_ids or len(items) >= 25:
            return False
        if panel[8] == "dropdown" and any(
            existing.get("category_id") == item["category_id"]
            for existing in items
        ):
            return False
        if panel[8] == "button" and len(items) >= 25:
            return False
        items.append({
            "label": item["label"],
            "description": item["description"][:100],
            "emoji": item["emoji"],
            "category_id": item["category_id"],
        })
        embed_data = self._panel_embed(panel)
        if item.get("placeholder"):
            embed_data["placeholder"] = item["placeholder"][:150]
        await self._update_ticket_panel(
            guild_id,
            panel_id,
            items_json=items,
            embed_json=embed_data,
        )
        return True

    async def _remove_ticket_panel_item(self, guild_id: int, panel_id: int | None, index: int):
        panel = await self._ticket_panel(guild_id, panel_id)
        if not panel:
            return False
        items = self._panel_items(panel)
        if not 0 <= index < len(items):
            return False
        items.pop(index)
        await self._update_ticket_panel(
            guild_id, panel_id, items_json=items
        )
        return True

    def _ticket_panel_embed_object(self, panel):
        data = self._panel_embed(panel)
        try:
            color = int(data.get("color", 0xFFDD00))
        except (TypeError, ValueError):
            color = 0xFFDD00
        embed = discord.Embed(
            title=data.get("title") or "Open a ticket",
            description=data.get("description") or "Choose an option below to create a private ticket.",
            color=discord.Color(max(0, min(0xFFFFFF, color))),
        )
        if data.get("author") or data.get("author_icon_url"):
            embed.set_author(
                name=str(data.get("author") or panel[1])[:256],
                url=data.get("author_url") or None,
                icon_url=data.get("author_icon_url") or None,
            )
        if data.get("footer") or data.get("footer_icon_url"):
            embed.set_footer(
                text=str(data.get("footer") or "")[:2048] or None,
                icon_url=data.get("footer_icon_url") or None,
            )
        if data.get("timestamp"):
            embed.timestamp = datetime.datetime.now(UTC)
        if data.get("thumbnail_url"):
            embed.set_thumbnail(url=data["thumbnail_url"])
        if data.get("image_url"):
            embed.set_image(url=data["image_url"])
        fields = data.get("fields")
        if isinstance(fields, list):
            for field in fields[:24]:
                if not isinstance(field, dict):
                    continue
                name = field.get("name")
                value = field.get("value")
                if isinstance(name, str) and name.strip() and isinstance(value, str) and value.strip():
                    embed.add_field(
                        name=name[:256],
                        value=value[:1024],
                        inline=field.get("inline") is True,
                    )
        embed.add_field(name="Panel", value=panel[1][:256], inline=False)
        return embed

    async def _publish_ticket_panel(
        self,
        guild_id: int,
        panel_id: int | None,
        panel_channel_id: int | None,
        log_channel_id: int | None,
    ) -> int | str:
        panel = await self._ticket_panel(guild_id, panel_id)
        if not panel:
            return "Create or select a ticket panel first."
        if not panel_channel_id or not log_channel_id:
            return "Choose both the panel channel and transcript channel."
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return "This server is unavailable. Reopen ticket setup and try again."
        panel_channel = guild.get_channel(panel_channel_id)
        log_channel = guild.get_channel(log_channel_id)
        if not isinstance(panel_channel, discord.TextChannel) or not isinstance(log_channel, discord.TextChannel):
            return "One of the selected channels is unavailable or is not a text channel."
        category_ids = [
            value for value in self._panel_category_ids(panel)
            if isinstance(guild.get_channel(value), discord.CategoryChannel)
        ]
        if not category_ids:
            return "Add at least one valid ticket category to this panel on page 2."
        items = [
            item for item in self._panel_items(panel)
            if item.get("category_id") in category_ids
        ]
        if not items:
            items = [
                {
                    "label": guild.get_channel(value).name[:80],
                    "description": "",
                    "emoji": "",
                    "category_id": value,
                }
                for value in category_ids
            ]
        embed_data = self._panel_embed(panel)
        embed = self._ticket_panel_embed_object(panel)
        view = ConfiguredTicketPanelView(
            self,
            panel_id,
            panel[8],
            items,
            embed_data.get("placeholder", "Choose a ticket type"),
            embed_data.get("buttons"),
        )
        bot_member = self._bot_member(guild)
        if bot_member is None:
            return "I could not find my member in this server."
        permissions = panel_channel.permissions_for(bot_member)
        if not (
            permissions.view_channel
            and permissions.send_messages
            and permissions.embed_links
        ):
            return "I need View Channel, Send Messages, and Embed Links in the panel channel."
        log_permissions = log_channel.permissions_for(bot_member)
        if not (
            log_permissions.view_channel
            and log_permissions.send_messages
            and log_permissions.attach_files
        ):
            return "I need View Channel, Send Messages, and Attach Files in the transcript channel."
        message = None
        old_channel_id, old_message_id = panel[5], panel[6]
        if old_channel_id == panel_channel.id and old_message_id:
            try:
                message = await panel_channel.fetch_message(old_message_id)
                await message.edit(embed=embed, view=view)
            except discord.NotFound:
                message = None
            except discord.HTTPException as error:
                LOGGER.warning("Could not refresh saved ticket panel %s: %s", panel_id, error)
                return "Discord could not update the existing panel. Check the bot's channel permissions."
        if message is None:
            try:
                message = await panel_channel.send(
                    embed=embed,
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException as error:
                LOGGER.warning("Could not publish ticket panel %s: %s", panel_id, error)
                return "Discord could not send the panel. Check the bot's channel permissions."
        await self._update_ticket_panel(
            guild_id,
            panel_id,
            sync=False,
            category_ids=category_ids,
            items_json=items,
            panel_channel_id=panel_channel.id,
            panel_message_id=message.id,
            log_channel_id=log_channel.id,
        )
        self.bot.add_view(view, message_id=message.id)
        if old_message_id and old_channel_id and old_channel_id != panel_channel.id:
            old_channel = guild.get_channel(old_channel_id)
            if isinstance(old_channel, discord.TextChannel):
                try:
                    old_message = await old_channel.fetch_message(old_message_id)
                    await old_message.delete()
                except discord.NotFound:
                    pass
                except discord.HTTPException as error:
                    LOGGER.warning("New ticket panel was published but old panel could not be removed: %s", error)
        first_category = category_ids[0]
        try:
            await self.bot.db.execute(
                "INSERT OR IGNORE INTO denycord_ticket_config "
                "(guild_id, category_id, panel_channel_id, panel_message_id, log_channel_id, setup_by, created_at, "
                "panel_mode, category_ids, ticket_limit, support_role_id, button_style, button_label, button_emoji) "
                "VALUES(?,?,?,?,?,?,?,?,?,1,NULL,'grey','Open a Ticket','🎫')",
                (
                    guild_id,
                    first_category,
                    panel_channel.id,
                    message.id,
                    log_channel.id,
                    0,
                    _now(),
                    panel[8],
                    json.dumps(category_ids),
                ),
            )
            await self.bot.db.commit()
        except aiosqlite.Error:
            await self.bot.db.rollback()
            LOGGER.exception("Ticket panel was sent but legacy ticket defaults could not be saved for guild %s", guild_id)
        return panel_channel.id

    async def _ticket_log_channel(self, guild, channel_id: int):
        panel_id_row = await self._fetchone(
            "SELECT panel_id FROM denycord_tickets WHERE guild_id=? AND channel_id=?",
            (guild.id, channel_id),
        )
        panel = await self._ticket_panel(guild.id, panel_id_row[0]) if panel_id_row and panel_id_row[0] else None
        if panel and panel[7]:
            channel = guild.get_channel(panel[7])
            if isinstance(channel, discord.TextChannel):
                return channel, panel
        config = await self._ticket_config(guild.id)
        logs = guild.get_channel(config[3]) if config else None
        return (logs if isinstance(logs, discord.TextChannel) else None), panel

    async def _log_ticket_event(
        self,
        guild: discord.Guild,
        content: str,
        *,
        ticket_channel_id: int | None = None,
    ) -> None:
        if ticket_channel_id is not None:
            logs, _ = await self._ticket_log_channel(guild, ticket_channel_id)
        else:
            config = await self._ticket_config(guild.id)
            logs = guild.get_channel(config[3]) if config else None
        if not isinstance(logs, discord.TextChannel):
            LOGGER.warning("Ticket event could not be logged in guild %s: log channel is unavailable", guild.id)
            return
        try:
            await logs.send(content, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as error:
            LOGGER.warning("Could not log ticket event in guild %s: %s", guild.id, error)

    async def open_ticket(
        self,
        interaction: discord.Interaction,
        *,
        category_id: int | None = None,
        panel_id: int | None = None,
    ):
        if not interaction.response.is_done():
            try:
                await interaction.response.defer(ephemeral=True, thinking=True)
            except discord.NotFound as error:
                LOGGER.warning("Ticket-open interaction expired before it could be deferred: %s", error)
                return
        async with self._ticket_open_lock:
            if panel_id is None:
                return await self._open_ticket_locked(
                    interaction,
                    category_id=category_id,
                )
            return await self._open_ticket_locked(
                interaction,
                category_id=category_id,
                panel_id=panel_id,
            )

    async def _open_ticket_locked(
        self,
        interaction: discord.Interaction,
        *,
        category_id: int | None = None,
        panel_id: int | None = None,
    ):
        if not interaction.response.is_done():
            try:
                await interaction.response.defer(ephemeral=True, thinking=True)
            except discord.NotFound as error:
                LOGGER.warning("Ticket-open interaction expired before it could be deferred: %s", error)
                return
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            return await self._send_interaction(interaction, "Tickets can only be opened in a server.")
        config = await self._ticket_config(guild.id)
        panel = await self._ticket_panel(guild.id, panel_id) if panel_id is not None else None
        if panel_id is not None and panel is None:
            return await self._send_interaction(interaction, "That ticket panel was deleted or is no longer available.")
        if not config and panel is None:
            return await self._send_interaction(interaction, "Tickets have not been set up in this server yet.")
        if panel is not None:
            configured_category_ids = set(self._panel_category_ids(panel))
            selected_category_id = category_id if category_id is not None else (
                next(iter(configured_category_ids), None)
            )
        else:
            try:
                configured_category_ids = {
                    int(value) for value in json.loads(config[5] or "[]")
                }
            except (json.JSONDecodeError, TypeError, ValueError):
                LOGGER.warning("Invalid ticket category configuration in guild %s", guild.id)
                configured_category_ids = set()
            configured_category_ids.add(config[0])
            selected_category_id = category_id if category_id is not None else config[0]
        if selected_category_id not in configured_category_ids:
            return await self._send_interaction(interaction, "That ticket category is not configured.")
        category = guild.get_channel(selected_category_id)
        bot_member = self._bot_member(guild)
        if not isinstance(category, discord.CategoryChannel) or bot_member is None:
            return await self._send_interaction(
                interaction,
                "The ticket category or bot member is unavailable. Ask an administrator to run `ticket_setup` again.",
            )

        async with self.bot.db.execute(
            "SELECT channel_id FROM denycord_tickets "
            "WHERE guild_id=? AND owner_id=? AND status='open'",
            (guild.id, interaction.user.id),
        ) as cursor:
            existing = await cursor.fetchall()
        live_tickets = []
        for (existing_channel_id,) in existing:
            existing_channel = guild.get_channel(existing_channel_id)
            if existing_channel is None:
                await self.bot.db.execute(
                    "UPDATE denycord_tickets SET status='missing' WHERE channel_id=?",
                    (existing_channel_id,),
                )
            else:
                live_tickets.append(existing_channel)
        ticket_limit = max(1, int(config[6] or 1)) if config else 1
        if live_tickets and ticket_limit == 1:
            await self.bot.db.commit()
            return await self._send_interaction(
                interaction,
                f"You already have an open ticket: {live_tickets[0].mention}",
            )
        if len(live_tickets) >= ticket_limit:
            await self.bot.db.commit()
            return await self._send_interaction(
                interaction,
                f"You have reached the open ticket limit ({ticket_limit}).",
            )

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            bot_member: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
                attach_files=True,
                embed_links=True,
            ),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
            ),
        }
        support_role = guild.get_role(config[7]) if config and config[7] else None
        overwrites.update(
            self._ticket_staff_overwrites(
                guild,
                send_messages=True,
                support_role=support_role,
            )
        )
        try:
            channel = await guild.create_text_channel(
                name=f"ticket-{_ticket_filename(interaction.user.name)}-{interaction.user.id % 10000:04d}",
                category=category,
                overwrites=overwrites,
                reason=f"Ticket opened by {interaction.user} ({interaction.user.id})",
            )
        except discord.HTTPException as error:
            LOGGER.warning("Could not create a ticket channel in guild %s: %s", guild.id, error)
            return await self._send_interaction(interaction, "I couldn't create your ticket channel. Check my channel permissions.")

        try:
            await channel.send(
                f"{interaction.user.mention}, describe the help you need. A staff member will respond here.",
                embed=discord.Embed(
                    title="Ticket Opened",
                    description="Use the button below when your issue is resolved. Closing creates an HTML transcript.",
                    color=0xFFDD00,
                ),
                view=TicketActionsView(self),
                allowed_mentions=discord.AllowedMentions(users=[interaction.user]),
            )
            await self.bot.db.execute(
                "INSERT INTO denycord_tickets(guild_id, channel_id, owner_id, opened_at, status, panel_id) "
                "VALUES(?,?,?,?, 'open', ?)",
                (guild.id, channel.id, interaction.user.id, _now(), panel_id),
            )
            await self.bot.db.commit()
        except discord.HTTPException as error:
            LOGGER.warning("Could not initialize ticket %s: %s", channel.id, error)
            try:
                await channel.delete(reason="Ticket initialization failed")
            except discord.HTTPException as delete_error:
                LOGGER.warning("Could not clean up incomplete ticket %s: %s", channel.id, delete_error)
            return await self._send_interaction(interaction, "I couldn't initialize the ticket. Please try again or contact an administrator.")
        except aiosqlite.Error as error:
            await self.bot.db.rollback()
            LOGGER.exception("Could not save ticket %s: %s", channel.id, error)
            try:
                await channel.delete(reason="Ticket database write failed")
            except discord.HTTPException as delete_error:
                LOGGER.warning("Could not clean up unsaved ticket %s: %s", channel.id, delete_error)
            return await self._send_interaction(interaction, "I couldn't save the ticket. Please try again later.")
        await self._log_ticket_event(
            guild,
            f"Ticket opened: {channel.mention} by {interaction.user.mention}.",
            ticket_channel_id=channel.id,
        )
        await self._send_interaction(interaction, f"Your private ticket is ready: {channel.mention}")

    async def _build_transcript(self, channel: discord.TextChannel):
        messages = []
        async for message in channel.history(limit=500, oldest_first=True):
            parts = [f'<div class="message"><header>{_escape(message.author)} '
                     f'&lt;{message.author.id}&gt; · {_escape(message.created_at.isoformat())}</header>']
            content = message.clean_content or message.content
            if content:
                parts.append(f'<pre>{_escape(content)}</pre>')
            for embed in message.embeds:
                embed_text = " · ".join(value for value in (embed.title, embed.description) if value)
                if embed_text:
                    parts.append(f'<blockquote>{_escape(embed_text[:6000])}</blockquote>')
            for attachment in message.attachments:
                url = _escape(attachment.url)
                parts.append(
                    f'<p>Attachment: <a href="{url}">{_escape(attachment.filename)}</a> '
                    f'({attachment.size} bytes)</p>'
                )
            parts.append("</div>")
            messages.append("".join(parts))

        document = (
            "<!doctype html><html><head><meta charset=\"utf-8\">"
            f"<title>Ticket {channel.id} transcript</title>"
            "<style>body{font:15px system-ui,sans-serif;background:#1e1f22;color:#dbdee1;"
            "max-width:1000px;margin:2rem auto;padding:0 1rem}.message{padding:1rem;"
            "border-bottom:1px solid #3f4147}header{color:#aeb2b8;font-size:.85rem}"
            "pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}a{color:#00a8fc}"
            "blockquote{border-left:3px solid #5865f2;padding-left:.8rem}</style></head><body>"
            f"<h1>Ticket transcript · {_escape(channel.name)}</h1>"
            f"<p>Channel ID: {channel.id} · Guild: {_escape(channel.guild.name)} "
            f"· Exported: {_escape(_now())}</p>"
        )
        footer = "</body></html>"
        truncated = False
        max_size = max(64_000, getattr(channel.guild, "filesize_limit", 8_000_000))
        body = "".join(messages)
        rendered = (document + body + footer).encode("utf-8")
        while len(rendered) > max_size and messages:
            messages.pop(0)
            truncated = True
            note = '<p><em>Earlier messages were omitted to fit the upload limit.</em></p>' if truncated else ""
            rendered = (document + note + "".join(messages) + footer).encode("utf-8")
        return rendered

    async def _close_ticket_for_actor(self, guild, channel, actor, respond):
        if guild is None or not isinstance(channel, discord.TextChannel):
            return await respond("This is not a ticket channel.")
        record = await self._ticket_record(guild.id, channel.id)
        if not record or record[1] != "open":
            return await respond("This ticket is already closed or was not created by Falcon.")
        owner_id = record[0]
        config = await self._ticket_config(guild.id)
        support_role_id = config[7] if config and len(config) > 7 else None
        is_staff = isinstance(actor, discord.Member) and self._is_ticket_staff(
            actor,
            support_role_id,
        )
        if actor.id != owner_id and not is_staff:
            return await respond("Only the ticket owner or a channel manager can close this ticket.")
        logs, panel = await self._ticket_log_channel(guild, channel.id)
        if not isinstance(logs, discord.TextChannel):
            return await respond("The configured ticket log channel is unavailable; this ticket was not closed.")
        try:
            transcript = await self._build_transcript(channel)
            file = discord.File(io.BytesIO(transcript), filename=f"ticket-{channel.id}-transcript.html")
            await logs.send(
                content=f"Transcript for ticket #{channel.name} (ID `{channel.id}`), closed by {actor.mention}.",
                file=file,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as error:
            LOGGER.warning("Could not export ticket %s: %s", channel.id, error)
            return await respond("I couldn't upload the transcript, so this ticket remains open.")

        owner = guild.get_member(owner_id)
        owner_transcript_sent = False
        try:
            owner_user = owner or self.bot.get_user(owner_id) or await self.bot.fetch_user(owner_id)
            await owner_user.send(
                content=f"Transcript for your ticket in **{guild.name}**.",
                file=discord.File(
                    io.BytesIO(transcript),
                    filename=f"ticket-{channel.id}-transcript.html",
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            owner_transcript_sent = True
        except discord.HTTPException as error:
            LOGGER.warning("Could not DM transcript for ticket %s to owner %s: %s", channel.id, owner_id, error)

        try:
            if owner is not None:
                await channel.set_permissions(
                    owner,
                    overwrite=discord.PermissionOverwrite(
                        view_channel=False,
                        send_messages=False,
                        read_message_history=False,
                    ),
                    reason=f"Ticket closed by {actor} ({actor.id})",
                )
            edit_kwargs = {
                "name": f"closed-{channel.id}",
                "reason": f"Ticket closed by {actor} ({actor.id})",
            }
            close_category = guild.get_channel(panel[4]) if panel and panel[4] else None
            if isinstance(close_category, discord.CategoryChannel):
                edit_kwargs.update(category=close_category, sync_permissions=False)
            await channel.edit(
                **edit_kwargs,
            )
        except discord.HTTPException as error:
            LOGGER.warning("Transcript was saved, but ticket %s could not be locked: %s", channel.id, error)
            return await respond(
                "The transcript was saved, but I couldn't lock the channel. Check my permissions and close it again.",
            )
        try:
            await self.bot.db.execute(
                "UPDATE denycord_tickets SET status='closed', closed_at=?, closed_by=? "
                "WHERE guild_id=? AND channel_id=? AND status='open'",
                (_now(), actor.id, guild.id, channel.id),
            )
            await self.bot.db.commit()
        except aiosqlite.Error as error:
            await self.bot.db.rollback()
            LOGGER.exception("Could not record closure of ticket %s: %s", channel.id, error)
            return await respond("The transcript is saved and channel locked, but I couldn't record the closure.")
        owner_delivery = " The owner also received a DM copy." if owner_transcript_sent else " The owner could not be sent a DM copy."
        await respond(f"Ticket closed and HTML transcript sent to the configured logs.{owner_delivery}")

    async def close_ticket(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            try:
                await interaction.response.defer(ephemeral=True, thinking=True)
            except discord.NotFound as error:
                LOGGER.warning("Ticket-close interaction expired before it could be deferred: %s", error)
                return

        async def respond(content):
            await self._send_interaction(interaction, content, ephemeral=True)

        await self._close_ticket_for_actor(
            interaction.guild,
            interaction.channel,
            interaction.user,
            respond,
        )

    async def ticket_action(self, interaction: discord.Interaction, action: str) -> None:
        if not interaction.response.is_done():
            try:
                await interaction.response.defer(ephemeral=True, thinking=True)
            except discord.NotFound as error:
                LOGGER.warning("Ticket action interaction expired: %s", error)
                return

        async def respond(content):
            await self._send_interaction(interaction, content, ephemeral=True)

        guild = interaction.guild
        channel = interaction.channel
        if guild is None or not isinstance(channel, discord.TextChannel):
            return await respond("This is not a Falcon ticket channel.")
        if action == "claim":
            return await self._claim_ticket_for_actor(
                guild, channel, interaction.user, respond
            )
        if action == "reopen":
            return await self._reopen_ticket_for_actor(
                guild, channel, interaction.user, respond
            )
        if action == "delete":
            return await self._delete_ticket_for_actor(
                guild, channel, interaction.user, respond
            )
        return await respond("That ticket action is not supported.")

    @commands.hybrid_group(name="ticket", invoke_without_command=True)
    async def ticket(self, ctx: commands.Context):
        await self._send_ctx(
            ctx,
            "Ticket panel setup: `,ticket setup` · `,ticket edit` · `,ticket delete`. "
            "Use the guided panel editor to create multiple panels, configure categories and controls, "
            "customize embeds, and publish panels. Use `,close` to close an open ticket.",
        )

    @commands.hybrid_command(name="close", description="Close this ticket and export its transcript")
    @commands.guild_only()
    async def close_ticket_command(self, ctx: commands.Context):
        async def respond(content):
            await self._send_ctx(ctx, content)

        await self._close_ticket_for_actor(ctx.guild, ctx.channel, ctx.author, respond)

    @commands.hybrid_command(name="transcript", description="Export an HTML transcript without closing the ticket")
    @commands.guild_only()
    @commands.check(_ticket_staff_check)
    async def ticket_transcript(self, ctx: commands.Context):
        record = await self._ticket_record(ctx.guild.id, ctx.channel.id)
        if not record or record[1] not in {"open", "closed"}:
            return await self._send_ctx(ctx, "Use this command in an open or closed Falcon ticket.")
        try:
            transcript = await self._build_transcript(ctx.channel)
            await ctx.send(
                content="Ticket transcript:",
                file=discord.File(
                    io.BytesIO(transcript),
                    filename=f"ticket-{ctx.channel.id}-transcript.html",
                ),
                ephemeral=ctx.interaction is not None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as error:
            LOGGER.warning("Could not send transcript for ticket %s: %s", ctx.channel.id, error)
            return await self._send_ctx(ctx, "I couldn't send the transcript. Check my upload permissions.")

    @commands.hybrid_command(name="ticketinfo", description="View owner, status, claim, and timestamps for a ticket")
    @commands.guild_only()
    async def ticket_info(self, ctx: commands.Context):
        row = await self._fetchone(
            "SELECT owner_id, status, opened_at, claimed_by, closed_at, closed_by "
            "FROM denycord_tickets WHERE guild_id=? AND channel_id=?",
            (ctx.guild.id, ctx.channel.id),
        )
        if not row:
            return await self._send_ctx(ctx, "Use this command in a Falcon ticket.")
        config = await self._ticket_config(ctx.guild.id)
        support_role_id = config[7] if config and len(config) > 7 else None
        is_staff = self._is_ticket_staff(ctx.author, support_role_id)
        if ctx.author.id != row[0] and not is_staff:
            return await self._send_ctx(ctx, "Only the ticket owner or a channel manager can view ticket details.")
        claimed = f"<@{row[3]}>" if row[3] is not None else "Unclaimed"
        closed = row[4] or "Not closed"
        closed_by = f"<@{row[5]}>" if row[5] is not None else "—"
        await ctx.send(
            f"**Ticket information**\n"
            f"Owner: <@{row[0]}>\nStatus: `{row[1]}`\nClaimed by: {claimed}\n"
            f"Opened: `{row[2]}`\nClosed: `{closed}`\nClosed by: {closed_by}",
            ephemeral=ctx.interaction is not None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_command(name="delete", description="Delete a closed Falcon ticket")
    @commands.guild_only()
    @commands.check(_ticket_staff_check)
    async def delete_ticket_command(self, ctx: commands.Context):
        async def respond(content):
            await self._send_ctx(ctx, content)

        await self._delete_ticket_for_actor(ctx.guild, ctx.channel, ctx.author, respond)

    @staticmethod
    def _is_ticket_staff(actor, support_role_id: int | None = None) -> bool:
        permissions = getattr(actor, "guild_permissions", None)
        has_management_permissions = bool(
            permissions
            and (permissions.manage_channels or permissions.administrator)
        )
        return has_management_permissions or bool(
            support_role_id
            and any(
                role.id == support_role_id
                for role in getattr(actor, "roles", ())
            )
        )

    async def _delete_ticket_for_actor(self, guild, channel, actor, respond):
        record = await self._ticket_record(guild.id, channel.id)
        if not record:
            return await respond("Use this command in a Falcon ticket.")
        if record[1] != "closed":
            return await respond("Close the ticket first with `,close` before deleting it.")
        config = await self._ticket_config(guild.id)
        support_role_id = config[7] if config and len(config) > 7 else None
        if not self._is_ticket_staff(actor, support_role_id):
            return await respond("Only a channel manager can delete a closed ticket.")
        try:
            await channel.delete(reason=f"Closed ticket deleted by {actor} ({actor.id})")
        except discord.HTTPException as error:
            LOGGER.warning("Could not delete closed ticket %s: %s", channel.id, error)
            return await respond("I couldn't delete this ticket. Check my Manage Channels permission.")
        await self._log_ticket_event(
            guild,
            f"Ticket deleted: #{channel.name} (`{channel.id}`) by {actor.mention}.",
            ticket_channel_id=channel.id,
        )
        try:
            await self.bot.db.execute(
                "UPDATE denycord_tickets SET status='deleted' WHERE guild_id=? AND channel_id=?",
                (guild.id, channel.id),
            )
            await self.bot.db.commit()
        except aiosqlite.Error as error:
            await self.bot.db.rollback()
            LOGGER.exception("Ticket %s was deleted but its database status was not updated: %s", channel.id, error)
            return await respond("The channel was deleted, but I couldn't update its saved ticket status.")
        await respond("Ticket deleted.")

    @commands.hybrid_command(name="rename", description="Rename a Falcon ticket channel")
    @commands.guild_only()
    async def rename_ticket_command(self, ctx: commands.Context, *, name: str):
        record = await self._ticket_record(ctx.guild.id, ctx.channel.id)
        if not record or record[1] not in {"open", "closed"}:
            return await self._send_ctx(ctx, "Use this command in an open or closed Falcon ticket.")
        config = await self._ticket_config(ctx.guild.id)
        support_role_id = config[7] if config and len(config) > 7 else None
        is_staff = self._is_ticket_staff(ctx.author, support_role_id)
        if ctx.author.id != record[0] and not is_staff:
            return await self._send_ctx(ctx, "Only the ticket owner or a channel manager can rename this ticket.")
        safe_name = _ticket_filename(name)
        if safe_name == "member" and not name.strip():
            return await self._send_ctx(ctx, "Provide a ticket name containing letters or numbers.")
        prefix = "closed-ticket" if record[1] == "closed" else "ticket"
        new_name = f"{prefix}-{safe_name}"[:100]
        try:
            await ctx.channel.edit(
                name=new_name,
                reason=f"Ticket renamed by {ctx.author} ({ctx.author.id})",
            )
        except discord.HTTPException as error:
            LOGGER.warning("Could not rename ticket %s: %s", ctx.channel.id, error)
            return await self._send_ctx(ctx, "I couldn't rename this ticket. Check my Manage Channels permission.")
        await self._send_ctx(ctx, f"Ticket renamed to `{new_name}`.")

    @ticket.command(name="claim", description="Claim an open ticket for staff follow-up")
    @commands.guild_only()
    @commands.check(_ticket_staff_check)
    async def ticket_claim(self, ctx: commands.Context):
        async def respond(content):
            await self._send_ctx(ctx, content)

        await self._claim_ticket_for_actor(ctx.guild, ctx.channel, ctx.author, respond)

    async def _claim_ticket_for_actor(self, guild, channel, actor, respond):
        config = await self._ticket_config(guild.id)
        support_role_id = config[7] if config and len(config) > 7 else None
        if not self._is_ticket_staff(actor, support_role_id):
            return await respond("Only a channel manager can claim a ticket.")
        record = await self._ticket_record(guild.id, channel.id)
        if not record or record[1] != "open":
            return await respond("Use this command in an open Falcon ticket.")
        claim = await self._fetchone(
            "SELECT claimed_by FROM denycord_tickets WHERE guild_id=? AND channel_id=?",
            (guild.id, channel.id),
        )
        if claim and claim[0] is not None:
            claimant = guild.get_member(claim[0])
            owner_label = claimant.mention if claimant else f"<@{claim[0]}>"
            return await respond(f"This ticket is already claimed by {owner_label}.")
        cursor = await self.bot.db.execute(
            "UPDATE denycord_tickets SET claimed_by=? "
            "WHERE guild_id=? AND channel_id=? AND status='open' AND claimed_by IS NULL",
            (actor.id, guild.id, channel.id),
        )
        if cursor.rowcount == 0:
            claim = await self._fetchone(
                "SELECT claimed_by FROM denycord_tickets WHERE guild_id=? AND channel_id=?",
                (guild.id, channel.id),
            )
            claimant_id = claim[0] if claim else None
            if claimant_id is None:
                return await respond("This ticket is no longer open for claims.")
            claimant = guild.get_member(claimant_id) if claimant_id else None
            owner_label = claimant.mention if claimant else f"<@{claimant_id}>"
            return await respond(f"This ticket is already claimed by {owner_label}.")
        await self.bot.db.commit()
        await self._log_ticket_event(
            guild,
            f"Ticket claimed: {channel.mention} by {actor.mention}.",
            ticket_channel_id=channel.id,
        )
        await respond(f"Ticket claimed by {actor.mention}.")

    @ticket.command(name="transfer", description="Transfer ticket ownership to another member")
    @commands.guild_only()
    @commands.check(_ticket_staff_check)
    async def ticket_transfer(self, ctx: commands.Context, member: discord.Member):
        record = await self._ticket_record(ctx.guild.id, ctx.channel.id)
        if not record or record[1] != "open":
            return await self._send_ctx(ctx, "Use this command in an open Falcon ticket.")
        owner_id = record[0]
        if member.bot or member.id == owner_id:
            return await self._send_ctx(ctx, "Choose a different human member as the new ticket owner.")
        old_owner = ctx.guild.get_member(owner_id)
        try:
            await ctx.channel.set_permissions(
                member,
                overwrite=discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                    embed_links=True,
                ),
                reason=f"Ticket ownership transferred by {ctx.author} ({ctx.author.id})",
            )
            if old_owner is not None:
                await ctx.channel.set_permissions(
                    old_owner,
                    overwrite=discord.PermissionOverwrite(
                        view_channel=False,
                        send_messages=False,
                        read_message_history=False,
                    ),
                    reason=f"Ticket ownership transferred by {ctx.author} ({ctx.author.id})",
                )
        except discord.HTTPException as error:
            LOGGER.warning("Could not transfer ticket %s to %s: %s", ctx.channel.id, member.id, error)
            return await self._send_ctx(ctx, "I couldn't update ticket access. Check my Manage Channels permission.")
        try:
            await self.bot.db.execute(
                "UPDATE denycord_tickets SET owner_id=?, claimed_by=NULL "
                "WHERE guild_id=? AND channel_id=? AND status='open'",
                (member.id, ctx.guild.id, ctx.channel.id),
            )
            await self.bot.db.commit()
        except aiosqlite.Error as error:
            await self.bot.db.rollback()
            LOGGER.exception("Could not save ticket ownership transfer for %s: %s", ctx.channel.id, error)
            return await self._send_ctx(ctx, "Ticket permissions changed, but I couldn't save the new owner.")
        await self._send_ctx(ctx, f"Ticket ownership transferred to {member.mention}.")

    @ticket.command(name="reopen", description="Reopen a closed ticket")
    @commands.guild_only()
    @commands.check(_ticket_staff_check)
    async def ticket_reopen(self, ctx: commands.Context):
        async def respond(content):
            await self._send_ctx(ctx, content)

        await self._reopen_ticket_for_actor(ctx.guild, ctx.channel, ctx.author, respond)

    async def _reopen_ticket_for_actor(self, guild, channel, actor, respond):
        config = await self._ticket_config(guild.id)
        support_role_id = config[7] if config and len(config) > 7 else None
        if not self._is_ticket_staff(actor, support_role_id):
            return await respond("Only a channel manager can reopen a ticket.")
        record = await self._ticket_record(guild.id, channel.id)
        if not record or record[1] != "closed":
            return await respond("Use this command in a closed Falcon ticket.")
        owner = guild.get_member(record[0])
        if owner is None:
            try:
                owner = await guild.fetch_member(record[0])
            except discord.HTTPException:
                return await respond("The ticket owner is no longer available in this server.")
        name = f"ticket-{_ticket_filename(owner.name)}-{owner.id % 10000:04d}"[:100]
        try:
            await channel.set_permissions(
                owner,
                overwrite=discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                    embed_links=True,
                ),
                reason=f"Ticket reopened by {actor} ({actor.id})",
            )
            await channel.edit(name=name, reason=f"Ticket reopened by {actor} ({actor.id})")
            await self.bot.db.execute(
                "UPDATE denycord_tickets SET status='open', claimed_by=NULL, closed_at=NULL, closed_by=NULL "
                "WHERE guild_id=? AND channel_id=? AND status='closed'",
                (guild.id, channel.id),
            )
            await self.bot.db.commit()
        except (discord.HTTPException, aiosqlite.Error) as error:
            await self.bot.db.rollback()
            LOGGER.warning("Could not reopen ticket %s: %s", channel.id, error)
            return await respond("I couldn't reopen this ticket. Check channel permissions and try again.")
        await self._log_ticket_event(
            guild,
            f"Ticket reopened: {channel.mention} for {owner.mention} by {actor.mention}.",
            ticket_channel_id=channel.id,
        )
        await respond(f"Ticket reopened for {owner.mention}.")

    async def _setup_ticket_panel(
        self,
        ctx: commands.Context,
        *,
        panel_mode: Literal["button", "dropdown"],
        title: str | None,
        description: str | None,
        author: str | None,
        author_icon_url: str | None,
        thumbnail_url: str | None,
        image_url: str | None,
        button_label: str,
        button_emoji: str,
        button_style: str,
        support_role: discord.Role | None,
    ):
        guild = ctx.guild
        title = (title or f"{guild.name} Support").strip()
        description = (
            description
            or (
                "Choose a category below to create a private support channel."
                if panel_mode == "dropdown"
                else "Press the button below to create a private support channel."
            )
        ).strip()
        if not title or len(title) > 256 or len(description) > 4000:
            return await self._send_ctx(ctx, "Panel titles must be 1-256 characters and descriptions at most 4000.")
        if panel_mode == "button" and (not button_label.strip() or len(button_label) > 80):
            return await self._send_ctx(ctx, "The button label must contain 1-80 characters.")
        if support_role is not None and support_role.guild.id != guild.id:
            return await self._send_ctx(ctx, "Choose a support role from this server.")
        button_style = button_style.casefold()
        style = TICKET_BUTTON_STYLES.get(button_style)
        if style is None:
            return await self._send_ctx(ctx, "Button style must be blue, grey, green, or red.")
        try:
            emoji_value = button_emoji.strip()
            if panel_mode == "button" and not emoji_value:
                raise ValueError("Button emoji cannot be empty.")
            button_emoji = str(discord.PartialEmoji.from_str(emoji_value))
        except (TypeError, ValueError):
            return await self._send_ctx(ctx, "Enter a valid Unicode or custom button emoji.")
        for label, url in (
            ("author icon", author_icon_url),
            ("thumbnail", thumbnail_url),
            ("image", image_url),
        ):
            if url is not None:
                try:
                    parsed_url = urlsplit(url)
                except ValueError:
                    return await self._send_ctx(ctx, f"The {label} must be a valid HTTP or HTTPS URL.")
                if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
                    return await self._send_ctx(ctx, f"The {label} must be a valid HTTP or HTTPS URL.")

        embed = discord.Embed(title=title, description=description, color=0xFFDD00)
        if author or author_icon_url:
            embed.set_author(name=(author or guild.name)[:256], icon_url=author_icon_url)
        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)
        if image_url:
            embed.set_image(url=image_url)

        bot_member = self._bot_member(guild)
        if bot_member is None or not bot_member.guild_permissions.manage_channels:
            return await self._send_ctx(ctx, "I need Manage Channels to set up tickets.")
        if not bot_member.guild_permissions.send_messages or not bot_member.guild_permissions.embed_links:
            return await self._send_ctx(ctx, "I need Send Messages and Embed Links permissions to post the ticket panel.")
        channel_permissions = ctx.channel.permissions_for(bot_member)
        if not channel_permissions.send_messages or not channel_permissions.embed_links:
            return await self._send_ctx(ctx, "I need Send Messages and Embed Links in this channel for the ticket panel.")

        async with self._setup_lock:
            current = await self._ticket_config(guild.id)
            if current:
                panel_channel = guild.get_channel(current[1])
                panel_message_id = current[2]
                suffix = f" Panel: {panel_channel.mention} (message `{panel_message_id}`)." if panel_channel else ""
                return await self._send_ctx(ctx, f"Ticket setup already exists. Existing channels and panel were left unchanged.{suffix}")

            category = None
            logs = None
            panel_message = None
            try:
                category_overwrites = {
                    guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    bot_member: discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=True,
                        read_message_history=True,
                        manage_channels=True,
                    ),
                }
                category = await guild.create_category(
                    name="Support Tickets",
                    overwrites=category_overwrites,
                    reason=f"Ticket setup by {ctx.author} ({ctx.author.id})",
                )
                log_overwrites = {
                    guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    bot_member: discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=True,
                        read_message_history=True,
                        attach_files=True,
                    ),
                }
                log_overwrites.update(
                    self._ticket_staff_overwrites(
                        guild,
                        send_messages=True,
                        support_role=support_role,
                    )
                )

                async def create_ticket_log_channel():
                    return await guild.create_text_channel(
                        name="ticket-logs",
                        category=category,
                        overwrites=log_overwrites,
                        reason=f"Ticket setup by {ctx.author} ({ctx.author.id})",
                    )

                try:
                    logs = await create_ticket_log_channel()
                except discord.HTTPException as error:
                    if error.code != 50035 or "Category does not exist" not in error.text:
                        raise
                    LOGGER.warning(
                        "Ticket category %s disappeared during setup in guild %s; retrying with a new category",
                        category.id,
                        guild.id,
                    )
                    category = await guild.create_category(
                        name="Support Tickets",
                        overwrites=category_overwrites,
                        reason=f"Ticket setup retry by {ctx.author} ({ctx.author.id})",
                    )
                    logs = await create_ticket_log_channel()
                if panel_mode == "dropdown":
                    view = TicketCategorySelectView(self, [category])
                else:
                    view = TicketPanelView(
                        self,
                        label=button_label.strip(),
                        emoji=button_emoji,
                        style=style,
                    )
                panel_message = await ctx.send(
                    embed=embed,
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                await self.bot.db.execute(
                    "INSERT INTO denycord_ticket_config"
                    "(guild_id, category_id, panel_channel_id, panel_message_id, log_channel_id, "
                    "setup_by, created_at, panel_mode, category_ids, support_role_id, "
                    "button_style, button_label, button_emoji) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        guild.id, category.id, ctx.channel.id, panel_message.id,
                        logs.id, ctx.author.id, _now(), panel_mode, json.dumps([category.id]),
                        support_role.id if support_role else None,
                        button_style, button_label.strip(), button_emoji,
                    ),
                )
                await self.bot.db.commit()
            except discord.HTTPException as error:
                await self.bot.db.rollback()
                LOGGER.warning("Ticket setup API operation failed in guild %s: %s", guild.id, error)
                await self._rollback_setup(panel_message, logs, category)
                return await self._send_ctx(ctx, "Ticket setup failed because Discord rejected an operation. Check permissions and try again.")
            except aiosqlite.Error as error:
                await self.bot.db.rollback()
                LOGGER.exception("Could not save ticket setup in guild %s: %s", guild.id, error)
                await self._rollback_setup(panel_message, logs, category)
                return await self._send_ctx(ctx, "Ticket setup could not be saved. Newly created setup resources were removed when possible.")
        await self._send_ctx(ctx, f"Ticket setup complete. Category: {category.mention} · Logs: {logs.mention} · Panel posted here.")

    @ticket.command(name="setup", description="Create a customizable button ticket panel")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_channels=True)
    async def ticket_setup(
        self,
        ctx: commands.Context,
    ):
        await self._start_ticket_wizard(ctx)

    @ticket.command(name="edit", description="Edit ticket panels in the guided setup")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_channels=True)
    async def ticket_edit(self, ctx: commands.Context):
        await self._start_ticket_wizard(ctx, notice="Select a panel to edit, or create a new panel.")

    @ticket.command(name="delete", description="Delete a ticket panel in the guided setup")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_channels=True)
    async def ticket_delete_panel(self, ctx: commands.Context):
        await self._start_ticket_wizard(ctx, notice="Select a panel, then click Delete panel.")

    async def _start_ticket_wizard(self, ctx: commands.Context, *, notice: str | None = None):
        if ctx.guild is None:
            return await self._send_ctx(ctx, "Ticket setup is only available in a server.")
        wizard = TicketSetupWizard(self, ctx.guild, ctx.author.id)
        embed, view = await wizard.render(notice=notice)
        wizard.message = await ctx.send(
            embed=embed,
            view=view,
            ephemeral=ctx.interaction is not None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @ticket.command(name="setup2", description="Create a customizable dropdown ticket panel")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_channels=True)
    async def ticket_setup2(
        self,
        ctx: commands.Context,
        title: str | None = None,
        description: str | None = None,
        author: str | None = None,
        author_icon_url: str | None = None,
        thumbnail_url: str | None = None,
        image_url: str | None = None,
        support_role: discord.Role | None = None,
    ):
        await self._setup_ticket_panel(
            ctx,
            panel_mode="dropdown",
            title=title,
            description=description,
            author=author,
            author_icon_url=author_icon_url,
            thumbnail_url=thumbnail_url,
            image_url=image_url,
            button_label="Open a Ticket",
            button_emoji="🎫",
            button_style="grey",
            support_role=support_role,
        )

    @ticket.command(
        name="setup_remove",
        aliases=["remove_setup"],
        description="Remove this server's ticket panel setup",
    )
    @commands.guild_only()
    @commands.has_guild_permissions(manage_channels=True)
    async def ticket_setup_remove(self, ctx: commands.Context):
        async with self._setup_lock:
            config = await self._ticket_config(ctx.guild.id)
            if not config:
                return await self._send_ctx(ctx, "This server does not have a ticket setup.")

            try:
                panel_channel = await ctx.guild.fetch_channel(config[1])
            except discord.NotFound:
                panel_channel = None
            except discord.HTTPException as error:
                LOGGER.warning("Could not find ticket panel in guild %s: %s", ctx.guild.id, error)
                return await self._send_ctx(ctx, "I couldn't access the ticket panel. The setup was left unchanged.")

            if isinstance(panel_channel, (discord.TextChannel, discord.Thread)):
                try:
                    panel_message = await panel_channel.fetch_message(config[2])
                    await panel_message.delete()
                except discord.NotFound:
                    pass
                except discord.HTTPException as error:
                    LOGGER.warning(
                        "Could not remove ticket panel in guild %s: %s",
                        ctx.guild.id,
                        error,
                    )
                    return await self._send_ctx(ctx, "I couldn't remove the ticket panel. The setup was left unchanged.")

            try:
                await self.bot.db.execute(
                    "DELETE FROM denycord_ticket_config WHERE guild_id=?",
                    (ctx.guild.id,),
                )
                await self.bot.db.commit()
            except aiosqlite.Error as error:
                await self.bot.db.rollback()
                LOGGER.exception("Could not remove ticket setup in guild %s: %s", ctx.guild.id, error)
                return await self._send_ctx(
                    ctx,
                    "The panel was removed, but I couldn't clear the saved setup. Run this command again to retry.",
                )

        await self._send_ctx(
            ctx,
            "Ticket setup removed. Existing ticket channels, categories, logs, records, and templates were kept.",
        )

    async def _rollback_setup(self, panel_message, logs, category):
        if panel_message is not None:
            try:
                await panel_message.delete()
            except discord.NotFound:
                pass
            except discord.HTTPException as error:
                LOGGER.warning("Could not remove failed ticket panel %s: %s", panel_message.id, error)
        for channel in (logs, category):
            if channel is not None:
                try:
                    await channel.delete(reason="Rolling back incomplete ticket setup")
                except discord.NotFound:
                    pass
                except discord.HTTPException as error:
                    LOGGER.warning("Could not remove incomplete ticket setup channel %s: %s", channel.id, error)

    def _configured_ticket_categories(
        self,
        guild: discord.Guild,
        config,
    ) -> list[discord.CategoryChannel]:
        try:
            category_ids = [int(value) for value in json.loads(config[5] or "[]")]
        except (json.JSONDecodeError, TypeError, ValueError):
            LOGGER.warning("Invalid ticket category configuration in guild %s", guild.id)
            category_ids = []
        if config[0] not in category_ids:
            category_ids.insert(0, config[0])
        categories = []
        for category_id in dict.fromkeys(category_ids):
            category = guild.get_channel(category_id)
            if isinstance(category, discord.CategoryChannel):
                categories.append(category)
        return categories

    async def _refresh_ticket_panel(self, guild: discord.Guild) -> bool:
        config = await self._ticket_config(guild.id)
        if not config:
            return False
        panel_channel = guild.get_channel(config[1])
        if not isinstance(panel_channel, discord.TextChannel):
            return False
        categories = self._configured_ticket_categories(guild, config)
        if config[4] == "dropdown":
            view = TicketCategorySelectView(self, categories)
        elif len(categories) > 1:
            style = TICKET_BUTTON_STYLES.get(config[8], discord.ButtonStyle.secondary)
            view = TicketCategoryButtonsView(
                self,
                [(category.id, category.name) for category in categories],
                style=style,
                emoji=config[10],
            )
        else:
            style = TICKET_BUTTON_STYLES.get(config[8], discord.ButtonStyle.secondary)
            view = TicketPanelView(
                self,
                label=config[9],
                emoji=config[10],
                style=style,
            )
        try:
            panel_message = await panel_channel.fetch_message(config[2])
            await panel_message.edit(view=view)
            self.bot.add_view(view, message_id=panel_message.id)
        except discord.HTTPException as error:
            LOGGER.warning("Could not refresh ticket panel in guild %s: %s", guild.id, error)
            return False
        return True

    @ticket.command(name="panel_refresh", description="Refresh category buttons or dropdown options")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_channels=True)
    async def ticket_panel_refresh(self, ctx: commands.Context):
        config = await self._ticket_config(ctx.guild.id)
        if not config:
            return await self._send_ctx(ctx, "This server does not have a ticket panel.")
        if not await self._refresh_ticket_panel(ctx.guild):
            return await self._send_ctx(ctx, "I couldn't refresh the ticket panel. Check the panel channel and message.")
        await self._send_ctx(ctx, "Ticket panel options refreshed.")

    @ticket.command(name="category_add", description="Add a category button or dropdown option")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_channels=True)
    async def ticket_category_add(self, ctx: commands.Context, *, name: str):
        config = await self._ticket_config(ctx.guild.id)
        if not config:
            return await self._send_ctx(ctx, "Run `,ticket setup` or `,ticket setup2` before adding categories.")
        name = name.strip()
        if not name or len(name) > 100:
            return await self._send_ctx(ctx, "Category names must contain 1-100 characters.")
        categories = self._configured_ticket_categories(ctx.guild, config)
        if len(categories) >= 25:
            return await self._send_ctx(ctx, "A ticket panel can contain at most 25 categories.")
        bot_member = self._bot_member(ctx.guild)
        if bot_member is None:
            return await self._send_ctx(ctx, "I could not find my member record in this server.")
        overwrites = {
            ctx.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            bot_member: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
            ),
        }
        support_role = ctx.guild.get_role(config[7]) if config[7] else None
        overwrites.update(
            self._ticket_staff_overwrites(
                ctx.guild,
                send_messages=True,
                support_role=support_role,
            )
        )
        category = None
        try:
            category = await ctx.guild.create_category(
                name=name,
                overwrites=overwrites,
                reason=f"Ticket category created by {ctx.author} ({ctx.author.id})",
            )
            category_ids = [item.id for item in categories] + [category.id]
            await self.bot.db.execute(
                "UPDATE denycord_ticket_config SET category_ids=? WHERE guild_id=?",
                (json.dumps(category_ids), ctx.guild.id),
            )
            await self.bot.db.commit()
        except discord.HTTPException as error:
            await self.bot.db.rollback()
            LOGGER.warning("Could not create ticket category in guild %s: %s", ctx.guild.id, error)
            if category is not None:
                try:
                    await category.delete(reason="Rolling back failed ticket category setup")
                except discord.HTTPException as delete_error:
                    LOGGER.warning("Could not remove failed ticket category %s: %s", category.id, delete_error)
            return await self._send_ctx(ctx, "I couldn't create that ticket category. Check my Manage Channels permission.")
        except aiosqlite.Error as error:
            await self.bot.db.rollback()
            LOGGER.exception("Could not save ticket category in guild %s: %s", ctx.guild.id, error)
            if category is not None:
                try:
                    await category.delete(reason="Rolling back failed ticket category setup")
                except discord.HTTPException as delete_error:
                    LOGGER.warning("Could not remove failed ticket category %s: %s", category.id, delete_error)
            return await self._send_ctx(ctx, "The category was created but could not be saved; cleanup was attempted.")
        if not await self._refresh_ticket_panel(ctx.guild):
            return await self._send_ctx(ctx, f"Added {category.mention}, but I couldn't refresh the ticket panel.")
        await self._send_ctx(ctx, f"Added {category.mention} to the ticket panel.")

    @ticket.command(name="category_remove", description="Remove a category button or dropdown option")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_channels=True)
    async def ticket_category_remove(
        self,
        ctx: commands.Context,
        category: discord.CategoryChannel,
    ):
        config = await self._ticket_config(ctx.guild.id)
        if not config:
            return await self._send_ctx(ctx, "This server does not have a ticket panel.")
        categories = self._configured_ticket_categories(ctx.guild, config)
        category_ids = [item.id for item in categories]
        if category.id not in category_ids:
            return await self._send_ctx(ctx, "That category is not in the ticket panel.")
        if len(category_ids) <= 1:
            return await self._send_ctx(ctx, "A ticket panel must keep at least one category.")
        category_ids.remove(category.id)
        try:
            await self.bot.db.execute(
                "UPDATE denycord_ticket_config SET category_id=?, category_ids=? WHERE guild_id=?",
                (category_ids[0], json.dumps(category_ids), ctx.guild.id),
            )
            await self.bot.db.commit()
        except aiosqlite.Error as error:
            await self.bot.db.rollback()
            LOGGER.exception("Could not remove ticket category %s in guild %s: %s", category.id, ctx.guild.id, error)
            return await self._send_ctx(ctx, "I couldn't update the ticket panel.")
        if not await self._refresh_ticket_panel(ctx.guild):
            return await self._send_ctx(ctx, "Removed the category from configuration, but I couldn't refresh the panel.")
        await self._send_ctx(ctx, f"Removed {category.mention} from the panel. The category itself was not deleted.")

    @ticket.command(name="category_list", description="List ticket panel categories")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_channels=True)
    async def ticket_category_list(self, ctx: commands.Context):
        config = await self._ticket_config(ctx.guild.id)
        if not config:
            return await self._send_ctx(ctx, "This server does not have a ticket panel.")
        categories = self._configured_ticket_categories(ctx.guild, config)
        if not categories:
            return await self._send_ctx(ctx, "No configured ticket categories are available.")
        await self._send_ctx(ctx, "Ticket categories: " + ", ".join(category.mention for category in categories))

    @ticket.command(name="limit", description="Set a member's open ticket limit")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_channels=True)
    async def ticket_limit(self, ctx: commands.Context, amount: int):
        if not 1 <= amount <= 10:
            return await self._send_ctx(ctx, "The open ticket limit must be between 1 and 10.")
        try:
            cursor = await self.bot.db.execute(
                "UPDATE denycord_ticket_config SET ticket_limit=? WHERE guild_id=?",
                (amount, ctx.guild.id),
            )
            await self.bot.db.commit()
        except aiosqlite.Error as error:
            await self.bot.db.rollback()
            LOGGER.exception("Could not set ticket limit in guild %s: %s", ctx.guild.id, error)
            return await self._send_ctx(ctx, "I couldn't update the ticket limit.")
        if cursor.rowcount == 0:
            return await self._send_ctx(ctx, "Set up tickets before changing the open ticket limit.")
        await self._send_ctx(ctx, f"Members can now have up to **{amount}** open ticket(s).")

    @ticket.command(name="user_add", description="Add a member to this ticket")
    @commands.guild_only()
    @commands.check(_ticket_staff_check)
    async def ticket_user_add(self, ctx: commands.Context, member: discord.Member):
        record = await self._ticket_record(ctx.guild.id, ctx.channel.id)
        if not record or record[1] != "open":
            return await self._send_ctx(ctx, "Use this command in an open Falcon ticket.")
        if member.bot:
            return await self._send_ctx(ctx, "Bots cannot be added as ticket participants.")
        try:
            await ctx.channel.set_permissions(
                member,
                overwrite=discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                    embed_links=True,
                ),
                reason=f"Ticket participant added by {ctx.author} ({ctx.author.id})",
            )
        except discord.HTTPException as error:
            LOGGER.warning("Could not add %s to ticket %s: %s", member.id, ctx.channel.id, error)
            return await self._send_ctx(ctx, "I couldn't update this ticket's permissions. Check my Manage Channels permission.")
        await self._send_ctx(ctx, f"Added {member.mention} to this ticket.")

    @ticket.command(name="user_remove", description="Remove a member from this ticket")
    @commands.guild_only()
    @commands.check(_ticket_staff_check)
    async def ticket_user_remove(self, ctx: commands.Context, member: discord.Member):
        record = await self._ticket_record(ctx.guild.id, ctx.channel.id)
        if not record or record[1] != "open":
            return await self._send_ctx(ctx, "Use this command in an open Falcon ticket.")
        if member.id == record[0]:
            return await self._send_ctx(ctx, "The ticket opener cannot be removed. Close the ticket instead.")
        try:
            await ctx.channel.set_permissions(
                member,
                overwrite=discord.PermissionOverwrite(
                    view_channel=False,
                    send_messages=False,
                    read_message_history=False,
                ),
                reason=f"Ticket participant removed by {ctx.author} ({ctx.author.id})",
            )
        except discord.HTTPException as error:
            LOGGER.warning("Could not remove %s from ticket %s: %s", member.id, ctx.channel.id, error)
            return await self._send_ctx(ctx, "I couldn't update this ticket's permissions. Check my Manage Channels permission.")
        await self._send_ctx(ctx, f"Removed {member.mention} from this ticket.")

    @ticket.command(name="add", description="Add a member to the current ticket")
    @commands.guild_only()
    @commands.check(_ticket_staff_check)
    async def ticket_add_command(self, ctx: commands.Context, member: discord.Member):
        await self.ticket_user_add.callback(self, ctx, member)

    @ticket.command(name="remove", description="Remove a member from the current ticket")
    @commands.guild_only()
    @commands.check(_ticket_staff_check)
    async def ticket_remove_command(self, ctx: commands.Context, member: discord.Member):
        await self.ticket_user_remove.callback(self, ctx, member)

    @ticket.command(name="template_set", description="Create or update a canned ticket template")
    @commands.guild_only()
    @commands.check(_ticket_staff_check)
    async def ticket_template_set(self, ctx: commands.Context, name: str, *, content: str):
        name = name.strip().casefold()
        content = content.strip()
        if not TEMPLATE_NAME_PATTERN.fullmatch(name):
            return await self._send_ctx(ctx, "Template names must be 1-32 letters, numbers, underscores, or hyphens.")
        if not content or len(content) > 2000:
            return await self._send_ctx(ctx, "Template content must contain 1-2000 characters.")
        try:
            await self.bot.db.execute(
                "INSERT INTO denycord_ticket_templates(guild_id,name,content,updated_by,updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(guild_id,name) DO UPDATE SET "
                "content=excluded.content, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                (ctx.guild.id, name, content, ctx.author.id, _now()),
            )
            await self.bot.db.commit()
        except aiosqlite.Error as error:
            await self.bot.db.rollback()
            LOGGER.exception("Could not save ticket template %s in guild %s: %s", name, ctx.guild.id, error)
            return await self._send_ctx(ctx, "I couldn't save that ticket template.")
        await self._send_ctx(ctx, f"Saved ticket template `{name}`.")

    @ticket.command(name="template_list", description="List this server's canned ticket templates")
    @commands.guild_only()
    @commands.check(_ticket_staff_check)
    async def ticket_template_list(self, ctx: commands.Context):
        async with self.bot.db.execute(
            "SELECT name FROM denycord_ticket_templates WHERE guild_id=? ORDER BY name LIMIT 50",
            (ctx.guild.id,),
        ) as cursor:
            rows = await cursor.fetchall()
        if not rows:
            return await self._send_ctx(ctx, "This server has no ticket templates yet.")
        await self._send_ctx(ctx, "Ticket templates: " + ", ".join(f"`{row[0]}`" for row in rows))

    @ticket.command(name="template_delete", description="Delete a canned ticket template")
    @commands.guild_only()
    @commands.check(_ticket_staff_check)
    async def ticket_template_delete(self, ctx: commands.Context, name: str):
        name = name.strip().casefold()
        try:
            cursor = await self.bot.db.execute(
                "DELETE FROM denycord_ticket_templates WHERE guild_id=? AND name=?",
                (ctx.guild.id, name),
            )
            await self.bot.db.commit()
        except aiosqlite.Error as error:
            await self.bot.db.rollback()
            LOGGER.exception("Could not delete ticket template %s in guild %s: %s", name, ctx.guild.id, error)
            return await self._send_ctx(ctx, "I couldn't delete that ticket template.")
        await self._send_ctx(ctx, f"Deleted ticket template `{name}`." if cursor.rowcount else f"No template named `{name}` exists.")

    @ticket.command(name="template_post", description="Post a canned template in this ticket")
    @commands.guild_only()
    @commands.check(_ticket_staff_check)
    async def ticket_template_post(self, ctx: commands.Context, name: str):
        record = await self._ticket_record(ctx.guild.id, ctx.channel.id)
        if not record or record[1] != "open":
            return await self._send_ctx(ctx, "Use this command in an open Falcon ticket.")
        row = await self._fetchone(
            "SELECT content FROM denycord_ticket_templates WHERE guild_id=? AND name=?",
            (ctx.guild.id, name.strip().casefold()),
        )
        if not row:
            return await self._send_ctx(ctx, f"No ticket template named `{name}` exists.")
        try:
            await ctx.channel.send(row[0], allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as error:
            LOGGER.warning("Could not post ticket template in channel %s: %s", ctx.channel.id, error)
            return await self._send_ctx(ctx, "I couldn't post that template. Check my Send Messages permission.")
        await self._send_ctx(ctx, f"Posted ticket template `{name}`.")

    @ticket.command(name="template_add", description="Alias for template_set")
    @commands.guild_only()
    @commands.check(_ticket_staff_check)
    async def ticket_template_add(self, ctx: commands.Context, name: str, *, content: str):
        await self.ticket_template_set.callback(self, ctx, name, content=content)

    @ticket.command(name="template_remove", description="Alias for template_delete")
    @commands.guild_only()
    @commands.check(_ticket_staff_check)
    async def ticket_template_remove(self, ctx: commands.Context, name: str):
        await self.ticket_template_delete(ctx, name)

    @commands.hybrid_command(name="rr_add", description="Alias for reactionrole add")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    async def rr_add(self, ctx: commands.Context, message_id: int, emoji: str, role: discord.Role):
        await self.reactionrole_add(ctx, message_id, emoji, role)

    @commands.hybrid_command(name="rr_remove", description="Alias for reactionrole remove")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    async def rr_remove(self, ctx: commands.Context, message_id: int, emoji: str):
        await self.reactionrole_remove(ctx, message_id, emoji)

    @commands.hybrid_command(name="rr_list", description="Alias for reactionrole list")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    async def rr_list(self, ctx: commands.Context):
        await self.reactionrole_list(ctx)

    def _role_is_assignable(self, guild: discord.Guild, role: discord.Role):
        bot_member = self._bot_member(guild)
        if role.is_default() or role.managed:
            return False
        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            return False
        return role < bot_member.top_role

    @commands.hybrid_group(name="reactionrole", invoke_without_command=True)
    async def reactionrole(self, ctx: commands.Context):
        await self._send_ctx(ctx, "Reaction-role commands: `reactionrole add`, `reactionrole list`, and `reactionrole remove`.")

    @reactionrole.command(name="add", description="Map a message reaction to a server role")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    async def reactionrole_add(self, ctx: commands.Context, message_id: int, emoji: str, role: discord.Role):
        guild = ctx.guild
        if message_id <= 0:
            return await self._send_ctx(ctx, "Enter a valid Discord message ID.")
        if role.guild.id != guild.id:
            return await self._send_ctx(ctx, "Choose a role from this server.")
        if not self._role_is_assignable(guild, role):
            return await self._send_ctx(ctx, "I need Manage Roles, and my highest role must be above the selected role.")
        try:
            partial_emoji = discord.PartialEmoji.from_str(emoji.strip())
        except (TypeError, ValueError):
            return await self._send_ctx(ctx, "Enter a valid Unicode or custom emoji.")
        emoji_key = str(partial_emoji)
        if not emoji_key:
            return await self._send_ctx(ctx, "Enter a valid Unicode or custom emoji.")
        existing = await self._fetchone(
            "SELECT role_id FROM denycord_reaction_roles WHERE guild_id=? AND message_id=? AND emoji=?",
            (guild.id, message_id, emoji_key),
        )
        if existing:
            return await self._send_ctx(ctx, "That message and emoji already have a role mapping. Remove it before adding another.")
        try:
            await self.bot.db.execute(
                "INSERT INTO denycord_reaction_roles"
                "(guild_id,message_id,emoji,role_id,created_by,created_at) VALUES(?,?,?,?,?,?)",
                (guild.id, message_id, emoji_key, role.id, ctx.author.id, _now()),
            )
            await self.bot.db.commit()
        except aiosqlite.Error as error:
            await self.bot.db.rollback()
            LOGGER.exception("Could not save reaction role mapping for message %s: %s", message_id, error)
            return await self._send_ctx(ctx, "I couldn't save that reaction-role mapping.")
        await self._send_ctx(
            ctx,
            f"Mapped {emoji_key} on message `{message_id}` to {role.mention}. "
            "Add that reaction to the message; Falcon will assign or remove the role when users react.",
        )

    @reactionrole.command(name="list", description="List this server's reaction-role mappings")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    async def reactionrole_list(self, ctx: commands.Context):
        async with self.bot.db.execute(
            "SELECT message_id, emoji, role_id FROM denycord_reaction_roles "
            "WHERE guild_id=? ORDER BY message_id, emoji LIMIT 50",
            (ctx.guild.id,),
        ) as cursor:
            rows = await cursor.fetchall()
        if not rows:
            return await self._send_ctx(ctx, "This server has no reaction-role mappings.")
        lines = []
        for message_id, emoji, role_id in rows:
            role = ctx.guild.get_role(role_id)
            role_label = role.mention if role else f"missing role `{role_id}`"
            lines.append(f"Message `{message_id}` · {emoji} → {role_label}")
        await self._send_ctx(ctx, "Reaction-role mappings:\n" + "\n".join(lines))

    @reactionrole.command(name="remove", description="Remove a reaction-role mapping")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    async def reactionrole_remove(self, ctx: commands.Context, message_id: int, emoji: str):
        try:
            emoji_key = str(discord.PartialEmoji.from_str(emoji.strip()))
        except (TypeError, ValueError):
            return await self._send_ctx(ctx, "Enter a valid Unicode or custom emoji.")
        try:
            cursor = await self.bot.db.execute(
                "DELETE FROM denycord_reaction_roles WHERE guild_id=? AND message_id=? AND emoji=?",
                (ctx.guild.id, message_id, emoji_key),
            )
            await self.bot.db.commit()
        except aiosqlite.Error as error:
            await self.bot.db.rollback()
            LOGGER.exception("Could not remove reaction role mapping for message %s: %s", message_id, error)
            return await self._send_ctx(ctx, "I couldn't remove that reaction-role mapping.")
        if cursor.rowcount:
            await self._send_ctx(ctx, f"Removed {emoji_key} mapping from message `{message_id}`.")
        else:
            await self._send_ctx(ctx, "No mapping for that message and emoji was found.")

    async def _handle_raw_reaction(self, payload: discord.RawReactionActionEvent, *, adding: bool):
        guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        if guild is None or payload.user_id == self.bot.user.id:
            return
        mapping = await self._fetchone(
            "SELECT role_id FROM denycord_reaction_roles WHERE guild_id=? AND message_id=? AND emoji=?",
            (guild.id, payload.message_id, str(payload.emoji)),
        )
        if mapping is None:
            return
        role = guild.get_role(mapping[0])
        if role is None or not self._role_is_assignable(guild, role):
            return
        member = payload.member if adding else guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except discord.NotFound:
                return
            except discord.Forbidden as error:
                LOGGER.warning("Cannot fetch reaction user %s in guild %s: %s", payload.user_id, guild.id, error)
                return
            except discord.HTTPException as error:
                LOGGER.warning("Cannot fetch reaction user %s in guild %s: %s", payload.user_id, guild.id, error)
                return
        if member.bot:
            return
        try:
            if adding:
                await member.add_roles(role, reason=f"Reaction role on message {payload.message_id}")
            else:
                await member.remove_roles(role, reason=f"Reaction role removed from message {payload.message_id}")
        except discord.Forbidden as error:
            LOGGER.warning("Role operation forbidden for %s in guild %s: %s", member.id, guild.id, error)
        except discord.HTTPException as error:
            LOGGER.warning("Role operation failed for %s in guild %s: %s", member.id, guild.id, error)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        await self._handle_raw_reaction(payload, adding=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        await self._handle_raw_reaction(payload, adding=False)


async def setup(bot):
    await bot.add_cog(DenycordInteractionFeatures(bot))
