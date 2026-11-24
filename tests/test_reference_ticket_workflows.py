import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiosqlite
from denycord_interaction_features import DenycordInteractionFeatures


def test_ticket_claim_assigns_staff_member() -> None:
    async def verify() -> None:
        database = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(rowcount=1)),
            commit=AsyncMock(),
        )
        cog = DenycordInteractionFeatures(SimpleNamespace(db=database))
        cog._ticket_record = AsyncMock(return_value=(3, "open"))
        cog._fetchone = AsyncMock(return_value=(None,))
        cog._ticket_config = AsyncMock(return_value=None)
        author = SimpleNamespace(
            id=9,
            mention="<@9>",
            guild_permissions=SimpleNamespace(manage_channels=True, administrator=False),
        )
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1, get_member=lambda _user_id: None),
            channel=SimpleNamespace(id=2, mention="<#2>"),
            author=author,
            interaction=None,
            send=AsyncMock(),
        )

        await cog.ticket_claim.callback(cog, ctx)

        database.execute.assert_awaited_once()
        database.commit.assert_awaited_once()
        assert "Ticket claimed" in ctx.send.await_args.args[0]

    asyncio.run(verify())


def test_ticket_claim_does_not_overwrite_existing_claim() -> None:
    async def verify() -> None:
        database = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())
        cog = DenycordInteractionFeatures(SimpleNamespace(db=database))
        cog._ticket_record = AsyncMock(return_value=(3, "open"))
        cog._fetchone = AsyncMock(return_value=(12,))
        cog._ticket_config = AsyncMock(return_value=None)
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1, get_member=lambda _user_id: None),
            channel=SimpleNamespace(id=2, mention="<#2>"),
            author=SimpleNamespace(
                id=9,
                mention="<@9>",
                guild_permissions=SimpleNamespace(manage_channels=True, administrator=False),
            ),
            interaction=None,
            send=AsyncMock(),
        )

        await cog.ticket_claim.callback(cog, ctx)

        database.execute.assert_not_awaited()
        database.commit.assert_not_awaited()
        assert "already claimed" in ctx.send.await_args.args[0]

    asyncio.run(verify())


def test_ticket_schema_migrates_panel_and_limit_settings() -> None:
    async def verify() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.execute(
            "CREATE TABLE denycord_ticket_config ("
            "guild_id INTEGER PRIMARY KEY, category_id INTEGER NOT NULL, "
            "panel_channel_id INTEGER NOT NULL, panel_message_id INTEGER NOT NULL, "
            "log_channel_id INTEGER NOT NULL, setup_by INTEGER NOT NULL, created_at TEXT NOT NULL)"
        )
        await database.execute(
            "INSERT INTO denycord_ticket_config VALUES(1, 10, 20, 30, 40, 50, 'now')"
        )
        bot = SimpleNamespace(db=database, add_view=Mock())
        cog = DenycordInteractionFeatures(bot)
        try:
            top_level = {command.name for command in cog.get_commands()}
            assert {
                "ticket", "close", "delete", "rename", "transcript", "ticketinfo",
            } <= top_level
            ticket_commands = {command.name for command in cog.ticket.commands}
            assert {
                "setup", "setup2", "setup_remove", "category_add", "category_remove",
                "category_list", "panel_refresh", "limit", "add", "remove",
            } <= ticket_commands
            slash_ticket_commands = {
                command.name for command in cog.ticket.app_command.commands
            }
            assert {
                "setup2", "setup_remove", "category_add", "category_remove", "add", "remove",
            } <= slash_ticket_commands
            assert cog.ticket_transcript.app_command.name == "transcript"
            assert cog.ticket_info.app_command.name == "ticketinfo"
            for command in cog.walk_commands():
                command.signature
                command.clean_params
            await cog.cog_load()
            async with database.execute("PRAGMA table_info(denycord_ticket_config)") as cursor:
                columns = {row[1] for row in await cursor.fetchall()}
            assert {
                "panel_mode",
                "category_ids",
                "ticket_limit",
                "support_role_id",
                "button_style",
                "button_label",
                "button_emoji",
            } <= columns
            assert bot.add_view.call_count == 5
            assert bot.add_view.call_args.kwargs["message_id"] == 30
            assert len(denycord_interaction_features.TicketActionsView(cog).children) == 4
        finally:
            await database.close()

    import denycord_interaction_features

    asyncio.run(verify())


def test_ticket_panel_wizard_persists_multiple_panel_routes_and_edits() -> None:
    async def verify() -> None:
        database = await aiosqlite.connect(":memory:")
        bot = SimpleNamespace(db=database, add_view=Mock(), get_guild=Mock(return_value=None))
        cog = DenycordInteractionFeatures(bot)
        try:
            await cog.cog_load()
            support_id = await cog._create_ticket_panel(1, "Support")
            billing_id = await cog._create_ticket_panel(1, "Billing")
            assert await cog._add_ticket_panel_category(1, support_id, 10)
            assert await cog._add_ticket_panel_item(
                1,
                support_id,
                {
                    "label": "General help",
                    "description": "Ask a question",
                    "emoji": "🎫",
                    "category_id": 10,
                    "placeholder": "Choose a support topic",
                },
            )
            await cog._update_ticket_panel(
                1,
                support_id,
                mode="button",
                close_category_id=20,
                embed_json={"title": "Help desk", "color": 0x123456},
            )
            assert await cog._rename_ticket_panel(1, "Support", "Help")
            assert await cog._remove_ticket_panel_category(1, support_id, 0)

            support = await cog._ticket_panel(1, support_id)
            billing = await cog._ticket_panel(1, billing_id)
            assert support[1] == "Help"
            assert support[3] == "[]"
            assert support[4] == 20
            assert support[8] == "button"
            assert cog._panel_embed(support)["title"] == "Help desk"
            assert cog._panel_items(support) == []
            assert billing[1] == "Billing"
            assert await cog._delete_ticket_panel(1, "Billing")
            assert await cog._ticket_panel(1, billing_id) is None
        finally:
            await database.close()

    asyncio.run(verify())


def test_ticket_setup_command_opens_guided_panel_builder() -> None:
    async def verify() -> None:
        database = await aiosqlite.connect(":memory:")
        guild = SimpleNamespace(id=1, name="Example", categories=[], text_channels=[])
        bot = SimpleNamespace(
            db=database,
            add_view=Mock(),
            get_guild=Mock(return_value=guild),
        )
        cog = DenycordInteractionFeatures(bot)
        await cog.cog_load()
        ctx = SimpleNamespace(
            guild=guild,
            author=SimpleNamespace(id=9),
            interaction=None,
            send=AsyncMock(),
        )
        try:
            await cog.ticket_setup.callback(cog, ctx)
            kwargs = ctx.send.await_args.kwargs
            assert "Ticket setup" in kwargs["embed"].title
            assert isinstance(kwargs["view"], denycord_interaction_features.TicketSetupWizard)
            assert kwargs["ephemeral"] is False
        finally:
            await database.close()

    import denycord_interaction_features

    asyncio.run(verify())


def test_ticket_wizard_has_searchable_panel_controls_and_single_mode_options() -> None:
    import discord
    import denycord_interaction_features

    async def verify() -> None:
        database = await aiosqlite.connect(":memory:")
        guild = SimpleNamespace(
            id=1,
            name="Example",
            categories=[],
            text_channels=[],
            get_channel=lambda _channel_id: SimpleNamespace(name="Support Tickets"),
        )
        bot = SimpleNamespace(db=database, add_view=Mock(), get_guild=Mock(return_value=guild))
        cog = DenycordInteractionFeatures(bot)
        try:
            await cog.cog_load()
            panel_id = await cog._create_ticket_panel(1, "Support")
            await cog._add_ticket_panel_category(1, panel_id, 10)
            await cog._add_ticket_panel_category(1, panel_id, 11)
            await cog._update_ticket_panel(1, panel_id, mode="button")
            await cog._create_ticket_panel(1, "Billing")
            category = SimpleNamespace(id=10, name="Support Tickets", position=0)
            cog._guild_categories = lambda _guild: [category]
            wizard = denycord_interaction_features.TicketSetupWizard(cog, guild, 9)
            wizard.panel_id = panel_id

            _, page_one = await wizard.render()
            panel_select = next(
                item for item in page_one.children
                if isinstance(item, discord.ui.Select)
            )
            assert panel_select.placeholder == "Choose a panel"
            wizard.search_queries["panel"] = "bill"
            _, filtered_page = await wizard.render()
            panel_select = next(
                item for item in filtered_page.children
                if isinstance(item, discord.ui.Select)
            )
            assert [option.label for option in panel_select.options] == [
                "Billing",
                "Search panel names...",
            ]
            wizard.search_queries["panel"] = ""
            _, page_one = await wizard.render()
            panel_select = next(
                item for item in page_one.children
                if isinstance(item, discord.ui.Select)
            )
            panel_select._values = [denycord_interaction_features._PANEL_SEARCH_VALUE]
            search_interaction = SimpleNamespace(
                user=SimpleNamespace(id=9),
                response=SimpleNamespace(send_modal=AsyncMock()),
            )
            await panel_select.callback(search_interaction)
            search_modal = search_interaction.response.send_modal.await_args.args[0]
            assert isinstance(search_modal, denycord_interaction_features._TicketWizardSearchModal)
            assert search_modal.title == "Search panels"

            wizard.page = 1
            _, page_two = await wizard.render()
            panel_selects = [item for item in page_two.children if isinstance(item, discord.ui.Select)]
            channel_selects = [
                item for item in page_two.children
                if isinstance(item, discord.ui.ChannelSelect)
            ]
            assert panel_selects == []
            assert [item.placeholder for item in channel_selects] == ["Choose ticket category"]
            assert channel_selects[0].channel_types == [discord.ChannelType.category]
            assert any(
                isinstance(item, discord.ui.Button)
                and item.label == "Use selected as close category"
                for item in page_two.children
            )

            wizard.page = 2
            _, page_three = await wizard.render()
            mode_selects = [item for item in page_three.children if isinstance(item, discord.ui.Select)]
            assert len(mode_selects) == 1
            assert mode_selects[0].placeholder == "Choose panel type"

            wizard.page = 3
            _, page_four = await wizard.render()
            assert not any(isinstance(item, discord.ui.Select) for item in page_four.children)
            assert [item.label for item in page_four.children if isinstance(item, discord.ui.Button)] == [
                "Add option",
                "Previous",
                "Next",
            ]
            option_modal = denycord_interaction_features._TicketPanelItemModal(wizard)
            assert len(option_modal.children) == 2

            wizard.page = 4
            _, page_five = await wizard.render()
            assert not any(isinstance(item, discord.ui.Select) for item in page_five.children)
            assert [item.label for item in page_five.children if isinstance(item, discord.ui.Button)] == [
                "Edit Basic Information",
                "Edit Author",
                "Edit Footer",
                "Edit Images",
                "Previous",
                "Next",
            ]
            assert not any(
                isinstance(item, discord.ui.Button) and "server" in (item.label or "").casefold()
                for item in page_five.children
            )
            assert len(denycord_interaction_features._TicketEmbedAuthorModal(wizard, {}).children) == 2
            assert len(denycord_interaction_features._TicketEmbedFooterModal(wizard, {}).children) == 3
            assert len(denycord_interaction_features._TicketEmbedImagesModal(wizard, {}).children) == 2

            wizard.page = 5
            _, page_six = await wizard.render()
            assert not any(isinstance(item, discord.ui.Select) for item in page_six.children)
            assert not any(
                isinstance(item, discord.ui.Button) and "search" in (item.label or "").casefold()
                for item in page_six.children
            )
            channel_select = wizard._options_select(
                "panel-channel",
                "Choose panel channel",
                [],
            )
            assert channel_select.channel_types == [
                discord.ChannelType.text,
                discord.ChannelType.news,
            ]

            await cog._update_ticket_panel(1, panel_id, mode="dropdown")
            wizard.page = 3
            _, dropdown_page = await wizard.render()
            dropdown_buttons = [
                item.label for item in dropdown_page.children
                if isinstance(item, discord.ui.Button)
            ]
            assert dropdown_buttons == ["Add option", "Previous", "Next"]
            assert not any(isinstance(item, discord.ui.Select) for item in dropdown_page.children)
            option_modal = denycord_interaction_features._TicketDropdownOptionModal(wizard)
            assert len(option_modal.children) == 4
            assert all(isinstance(item, discord.ui.TextInput) for item in option_modal.children)
            interaction = SimpleNamespace(
                user=SimpleNamespace(id=9),
                type=discord.InteractionType.component,
                response=SimpleNamespace(
                    is_done=Mock(return_value=False),
                    edit_message=AsyncMock(),
                    send_modal=AsyncMock(),
                ),
                message=SimpleNamespace(flags=SimpleNamespace(is_components_v2=True)),
            )
            await wizard._button_callback(interaction, "add-dropdown-option")
            modal = interaction.response.send_modal.await_args.args[0]
            assert isinstance(modal, denycord_interaction_features._TicketDropdownOptionModal)

            await wizard._add_dropdown_option(
                interaction,
                "Billing help",
                "Billing questions",
                "Choose billing support",
                "💳",
            )
            saved_panel = await cog._ticket_panel(1, panel_id)
            assert cog._panel_items(saved_panel)[0] == {
                "label": "Billing help",
                "description": "Billing questions",
                "emoji": "💳",
                "category_id": 10,
            }
            assert cog._panel_embed(saved_panel)["placeholder"] == "Choose billing support"
            assert wizard._available_dropdown_categories(saved_panel) == [(11, "Support Tickets")]
            assert not await cog._add_ticket_panel_item(
                1,
                panel_id,
                {
                    "label": "Duplicate category",
                    "description": "",
                    "emoji": "",
                    "category_id": 10,
                },
            )
            await wizard._add_dropdown_option(
                interaction,
                "Sales",
                "Sales questions",
                "Choose billing support",
                "",
            )
            saved_panel = await cog._ticket_panel(1, panel_id)
            assert cog._panel_items(saved_panel)[1]["category_id"] == 11
            assert wizard._available_dropdown_categories(saved_panel) == []
            _, completed_options_page = await wizard.render()
            completed_option_buttons = [
                item.label for item in completed_options_page.children
                if isinstance(item, discord.ui.Button)
            ]
            assert "Add option" not in completed_option_buttons
            assert "Remove option number" in completed_option_buttons
            assert interaction.response.edit_message.await_count == 2
            public_view = denycord_interaction_features.ConfiguredTicketPanelView(
                cog,
                panel_id,
                "dropdown",
                cog._panel_items(saved_panel),
                cog._panel_embed(saved_panel)["placeholder"],
            )
            public_select = public_view.children[0]
            assert public_select.placeholder == "Choose billing support"
            assert public_select.options[0].label == "Billing help"
            assert public_select.options[0].description == "Billing questions"
            assert str(public_select.options[0].emoji) == "💳"
            button_view = denycord_interaction_features.ConfiguredTicketPanelView(
                cog,
                panel_id,
                "button",
                cog._panel_items(saved_panel),
                "",
            )
            assert not any(isinstance(item, discord.ui.Select) for item in button_view.children)
            assert button_view.children[0].custom_id.startswith(
                denycord_interaction_features.TICKET_PANEL_BUTTON_PREFIX
            )

            embed_data = {
                "title": "Support desk",
                "description": "Open a ticket",
                "color": 0x123456,
                "author": "Falcon",
                "author_icon_url": "https://example.com/author.png",
                "thumbnail_url": "https://example.com/thumb.png",
                "image_url": "https://example.com/image.png",
                "footer": "Need help?",
                "footer_icon_url": "https://example.com/footer.png",
                "fields": [{"name": "Hours", "value": "24/7", "inline": True}],
                "buttons": [{"label": "Documentation", "url": "https://example.com/docs"}],
                "timestamp": True,
            }
            await cog._update_ticket_panel(1, panel_id, embed_json=embed_data)
            saved_panel = await cog._ticket_panel(1, panel_id)
            preview = cog._ticket_panel_embed_object(saved_panel)
            assert preview.title == "Support desk"
            assert preview.author.icon_url == "https://example.com/author.png"
            assert preview.thumbnail.url == "https://example.com/thumb.png"
            assert preview.image.url == "https://example.com/image.png"
            assert preview.footer.icon_url == "https://example.com/footer.png"
            assert preview.fields[0].name == "Hours"
            assert preview.timestamp is not None
            public_view = denycord_interaction_features.ConfiguredTicketPanelView(
                cog,
                panel_id,
                "dropdown",
                cog._panel_items(saved_panel),
                cog._panel_embed(saved_panel).get("placeholder", "Choose a ticket type"),
                cog._panel_embed(saved_panel)["buttons"],
            )
            assert public_view.children[-1].label == "Documentation"
            assert public_view.children[-1].url == "https://example.com/docs"
        finally:
            await database.close()

    asyncio.run(verify())


def test_dropdown_add_option_button_explains_missing_categories() -> None:
    import discord
    import denycord_interaction_features

    async def verify() -> None:
        database = await aiosqlite.connect(":memory:")
        guild = SimpleNamespace(
            id=1,
            name="Example",
            categories=[],
            text_channels=[],
            get_channel=lambda _channel_id: None,
        )
        bot = SimpleNamespace(db=database, add_view=Mock(), get_guild=Mock(return_value=guild))
        cog = DenycordInteractionFeatures(bot)
        try:
            await cog.cog_load()
            panel_id = await cog._create_ticket_panel(1, "Support")
            await cog._update_ticket_panel(1, panel_id, mode="dropdown")
            wizard = denycord_interaction_features.TicketSetupWizard(cog, guild, 9)
            wizard.panel_id = panel_id
            wizard.page = 3

            _, page = await wizard.render()
            assert "Add option" in [
                item.label for item in page.children
                if isinstance(item, discord.ui.Button)
            ]

            interaction = SimpleNamespace(
                user=SimpleNamespace(id=9),
                response=SimpleNamespace(
                    is_done=Mock(return_value=False),
                    send_message=AsyncMock(),
                ),
            )
            await wizard._button_callback(interaction, "add-dropdown-option")

            interaction.response.send_message.assert_awaited_once_with(
                "Add a ticket category on page 2 before creating dropdown options.",
                ephemeral=True,
            )
        finally:
            await database.close()

    asyncio.run(verify())


def test_ticket_wizard_refresh_edits_v2_messages_without_legacy_embeds() -> None:
    import discord
    import denycord_interaction_features

    async def verify() -> None:
        database = await aiosqlite.connect(":memory:")
        guild = SimpleNamespace(id=1, name="Example", categories=[], text_channels=[])
        bot = SimpleNamespace(
            db=database,
            add_view=Mock(),
            get_guild=Mock(return_value=guild),
        )
        cog = DenycordInteractionFeatures(bot)
        await cog.cog_load()
        wizard = denycord_interaction_features.TicketSetupWizard(cog, guild, 9)

        component_message = SimpleNamespace(
            flags=SimpleNamespace(is_components_v2=True),
            edit=AsyncMock(),
        )
        component_response = SimpleNamespace(
            is_done=Mock(return_value=False),
            edit_message=AsyncMock(),
        )
        component_interaction = SimpleNamespace(
            type=discord.InteractionType.component,
            response=component_response,
            message=component_message,
        )

        modal_message = SimpleNamespace(
            flags=SimpleNamespace(is_components_v2=True),
            edit=AsyncMock(),
        )
        wizard.message = modal_message
        modal_response = SimpleNamespace(
            is_done=Mock(return_value=True),
            defer=AsyncMock(),
        )
        modal_interaction = SimpleNamespace(
            type=discord.InteractionType.modal_submit,
            response=modal_response,
            followup=SimpleNamespace(send=AsyncMock()),
        )
        try:
            await wizard.refresh(component_interaction)
            await wizard.refresh(modal_interaction)

            component_response.edit_message.assert_awaited_once()
            assert "embed" not in component_response.edit_message.await_args.kwargs
            assert isinstance(
                component_response.edit_message.await_args.kwargs["view"],
                discord.ui.LayoutView,
            )
            modal_message.edit.assert_awaited_once()
            assert "embed" not in modal_message.edit.await_args.kwargs
            assert isinstance(modal_message.edit.await_args.kwargs["view"], discord.ui.LayoutView)
        finally:
            await database.close()

    asyncio.run(verify())


def test_publish_uses_selected_channels_after_wizard_refresh() -> None:
    import discord
    import denycord_interaction_features

    async def verify() -> None:
        database = await aiosqlite.connect(":memory:")
        guild = SimpleNamespace(
            id=1,
            name="Example",
            categories=[],
            text_channels=[],
            get_channel=lambda _channel_id: None,
        )
        bot = SimpleNamespace(db=database, add_view=Mock(), get_guild=Mock(return_value=guild))
        cog = DenycordInteractionFeatures(bot)
        try:
            await cog.cog_load()
            panel_id = await cog._create_ticket_panel(1, "Support")
            wizard = denycord_interaction_features.TicketSetupWizard(cog, guild, 9)
            wizard.panel_id = panel_id
            wizard.page = 5

            async def select_channel(selection, channel_id):
                selector = wizard._options_select(selection, "Choose channel", [])
                interaction = SimpleNamespace(
                    user=SimpleNamespace(id=9),
                    type=discord.InteractionType.component,
                    data={"values": [str(channel_id)]},
                    response=SimpleNamespace(
                        is_done=Mock(return_value=False),
                        edit_message=AsyncMock(),
                    ),
                )
                await selector.callback(interaction)

            await select_channel("panel-channel", 101)
            await select_channel("log-channel", 202)
            assert wizard.panel_channel_id == 101
            assert wizard.log_channel_id == 202

            cog._publish_ticket_panel = AsyncMock(return_value=101)
            interaction = SimpleNamespace(
                user=SimpleNamespace(id=9),
                type=discord.InteractionType.component,
                response=SimpleNamespace(
                    is_done=Mock(return_value=False),
                    edit_message=AsyncMock(),
                ),
            )
            await wizard._button_callback(interaction, "publish")
            cog._publish_ticket_panel.assert_awaited_once_with(
                1,
                panel_id,
                101,
                202,
            )
        finally:
            await database.close()

    asyncio.run(verify())


def test_ticket_publish_validation_updates_the_wizard_without_ephemeral_error() -> None:
    import discord
    import denycord_interaction_features

    async def verify() -> None:
        database = await aiosqlite.connect(":memory:")
        guild = SimpleNamespace(
            id=1,
            name="Example",
            categories=[],
            text_channels=[],
            get_channel=lambda _channel_id: None,
        )
        bot = SimpleNamespace(
            db=database,
            add_view=Mock(),
            get_guild=Mock(return_value=guild),
        )
        cog = DenycordInteractionFeatures(bot)
        try:
            await cog.cog_load()
            panel_id = await cog._create_ticket_panel(1, "Support")
            wizard = denycord_interaction_features.TicketSetupWizard(cog, guild, 9)
            wizard.panel_id = panel_id
            interaction = SimpleNamespace(
                user=SimpleNamespace(id=9),
                type=discord.InteractionType.component,
                response=SimpleNamespace(
                    is_done=Mock(return_value=False),
                    edit_message=AsyncMock(),
                    send_message=AsyncMock(),
                ),
                message=SimpleNamespace(flags=SimpleNamespace(is_components_v2=True)),
            )

            await wizard._button_callback(interaction, "publish")

            interaction.response.edit_message.assert_awaited_once()
            interaction.response.send_message.assert_not_awaited()
            notice = "Could not publish panel: Choose both the panel channel and transcript channel."
            wizard.page = 5
            embed, _ = await wizard.render(notice=notice)
            assert notice in embed.description
            assert await cog._publish_ticket_panel(1, panel_id, None, None) == notice.removeprefix(
                "Could not publish panel: "
            )
        finally:
            await database.close()

    asyncio.run(verify())


def test_ticket_setup_remove_deletes_panel_and_preserves_ticket_channels_and_records() -> None:
    import discord

    async def verify() -> None:
        database = await aiosqlite.connect(":memory:")
        bot = SimpleNamespace(db=database, add_view=Mock())
        cog = DenycordInteractionFeatures(bot)
        await cog.cog_load()
        await database.execute(
            "INSERT INTO denycord_ticket_config "
            "(guild_id, category_id, panel_channel_id, panel_message_id, log_channel_id, setup_by, created_at) "
            "VALUES (1, 2, 3, 4, 5, 6, 'now')"
        )
        await database.execute(
            "INSERT INTO denycord_tickets (channel_id, guild_id, owner_id, opened_at) "
            "VALUES (7, 1, 8, 'now')"
        )
        await database.commit()

        panel_message = SimpleNamespace(delete=AsyncMock())
        panel_channel = Mock(spec=discord.TextChannel)
        panel_channel.fetch_message = AsyncMock(return_value=panel_message)
        guild = SimpleNamespace(id=1, fetch_channel=AsyncMock(return_value=panel_channel))
        ctx = SimpleNamespace(
            guild=guild,
            author=SimpleNamespace(id=9),
            interaction=None,
            send=AsyncMock(),
        )
        try:
            await cog.ticket_setup_remove.callback(cog, ctx)

            panel_channel.fetch_message.assert_awaited_once_with(4)
            panel_message.delete.assert_awaited_once_with()
            async with database.execute(
                "SELECT guild_id FROM denycord_ticket_config WHERE guild_id=1"
            ) as cursor:
                assert await cursor.fetchone() is None
            async with database.execute(
                "SELECT channel_id FROM denycord_tickets WHERE guild_id=1"
            ) as cursor:
                assert await cursor.fetchone() == (7,)
            assert "Existing ticket channels" in ctx.send.await_args.args[0]
        finally:
            await database.close()

    asyncio.run(verify())


def test_ticket_transcript_and_ticketinfo_commands() -> None:
    async def verify() -> None:
        cog = DenycordInteractionFeatures(SimpleNamespace())
        cog._ticket_record = AsyncMock(return_value=(4, "open"))
        cog._build_transcript = AsyncMock(return_value=b"<!doctype html>")
        cog._fetchone = AsyncMock(
            return_value=(4, "open", "2026-11-12T10:00:00+00:00", 9, None, None)
        )
        cog._ticket_config = AsyncMock(return_value=None)
        permissions = SimpleNamespace(manage_channels=True, administrator=False)
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            channel=SimpleNamespace(id=2),
            author=SimpleNamespace(id=9, guild_permissions=permissions),
            interaction=None,
            send=AsyncMock(),
        )

        await cog.ticket_transcript.callback(cog, ctx)
        sent_file = ctx.send.await_args.kwargs["file"]
        assert sent_file.filename == "ticket-2-transcript.html"
        assert sent_file.fp.getvalue() == b"<!doctype html>"
        sent_file.close()

        ctx.send.reset_mock()
        await cog.ticket_info.callback(cog, ctx)
        assert "Owner: <@4>" in ctx.send.await_args.args[0]
        assert "Claimed by: <@9>" in ctx.send.await_args.args[0]
        assert "Status: `open`" in ctx.send.await_args.args[0]

    asyncio.run(verify())


def test_ticket_limit_command_persists_valid_limit_and_rejects_out_of_range() -> None:
    async def verify() -> None:
        database = await aiosqlite.connect(":memory:")
        await database.execute(
            "CREATE TABLE denycord_ticket_config (guild_id INTEGER PRIMARY KEY, ticket_limit INTEGER)"
        )
        await database.execute("INSERT INTO denycord_ticket_config VALUES(1, 1)")
        await database.commit()
        cog = DenycordInteractionFeatures(SimpleNamespace(db=database))
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            interaction=None,
            send=AsyncMock(),
        )
        try:
            await cog.ticket_limit.callback(cog, ctx, 4)
            async with database.execute(
                "SELECT ticket_limit FROM denycord_ticket_config WHERE guild_id=1"
            ) as cursor:
                assert await cursor.fetchone() == (4,)

            ctx.send.reset_mock()
            await cog.ticket_limit.callback(cog, ctx, 11)
            ctx.send.assert_awaited_once()
            assert "between 1 and 10" in ctx.send.await_args.args[0]
        finally:
            await database.close()

    asyncio.run(verify())


def test_ticket_setup_persists_custom_panel_and_support_role() -> None:
    import discord

    async def verify() -> None:
        database = await aiosqlite.connect(":memory:")
        bot = SimpleNamespace(db=database, add_view=Mock())
        cog = DenycordInteractionFeatures(bot)
        await cog.cog_load()

        class FakeEntity:
            pass

        guild = SimpleNamespace(
            id=1,
            name="Example",
            default_role=FakeEntity(),
            roles=[],
        )
        bot_member = FakeEntity()
        bot_member.guild_permissions = SimpleNamespace(
            manage_channels=True,
            send_messages=True,
            embed_links=True,
        )
        guild.me = bot_member
        support_role = FakeEntity()
        support_role.id = 55
        support_role.guild = guild
        category = FakeEntity()
        category.id = 10
        category.mention = "<#10>"
        logs = FakeEntity()
        logs.id = 11
        logs.mention = "<#11>"
        panel = FakeEntity()
        panel.id = 12
        guild.create_category = AsyncMock(return_value=category)
        guild.create_text_channel = AsyncMock(return_value=logs)
        channel = SimpleNamespace(
            id=13,
            permissions_for=Mock(
                return_value=SimpleNamespace(send_messages=True, embed_links=True)
            ),
        )
        ctx = SimpleNamespace(
            guild=guild,
            channel=channel,
            author=SimpleNamespace(id=7),
            interaction=None,
            send=AsyncMock(return_value=panel),
        )

        try:
            await cog._setup_ticket_panel(
                ctx,
                panel_mode="button",
                title="Contact",
                description="Choose a topic",
                author="Support",
                author_icon_url=None,
                thumbnail_url=None,
                image_url=None,
                button_label="Get Help",
                button_emoji="📩",
                button_style="green",
                support_role=support_role,
            )
            config = await cog._ticket_config(guild.id)
            assert config[4] == "button"
            assert config[5] == "[10]"
            assert config[7] == 55
            assert config[8:11] == ("green", "Get Help", "📩")
            panel_view = ctx.send.await_args_list[0].kwargs["view"]
            assert panel_view.children[0].label == "Get Help"
            assert panel_view.children[0].style is discord.ButtonStyle.success
            assert support_role in guild.create_text_channel.await_args.kwargs["overwrites"]
        finally:
            await database.close()

    asyncio.run(verify())


def test_ticket_setup_retries_when_new_category_disappears_before_log_channel_creation() -> None:
    import discord

    async def verify() -> None:
        database = await aiosqlite.connect(":memory:")
        bot = SimpleNamespace(db=database, add_view=Mock())
        cog = DenycordInteractionFeatures(bot)
        await cog.cog_load()

        class FakeEntity:
            pass

        guild = SimpleNamespace(
            id=1,
            name="Example",
            default_role=FakeEntity(),
            roles=[],
        )
        bot_member = FakeEntity()
        bot_member.guild_permissions = SimpleNamespace(
            manage_channels=True,
            send_messages=True,
            embed_links=True,
        )
        guild.me = bot_member
        first_category = SimpleNamespace(id=10, mention="<#10>")
        replacement_category = SimpleNamespace(id=20, mention="<#20>")
        logs = SimpleNamespace(id=21, mention="<#21>")
        panel = SimpleNamespace(id=22)
        guild.create_category = AsyncMock(
            side_effect=[first_category, replacement_category]
        )
        missing_category = discord.HTTPException(
            SimpleNamespace(status=400, reason="Bad Request"),
            "Invalid Form Body",
        )
        missing_category.code = 50035
        missing_category.text = "Invalid Form Body\nIn parent_id: Category does not exist"
        guild.create_text_channel = AsyncMock(side_effect=[missing_category, logs])
        channel = SimpleNamespace(
            id=23,
            permissions_for=Mock(
                return_value=SimpleNamespace(send_messages=True, embed_links=True)
            ),
        )
        ctx = SimpleNamespace(
            guild=guild,
            channel=channel,
            author=SimpleNamespace(id=7),
            interaction=None,
            send=AsyncMock(return_value=panel),
        )
        try:
            await cog._setup_ticket_panel(
                ctx,
                panel_mode="button",
                title="Contact",
                description="Choose a topic",
                author=None,
                author_icon_url=None,
                thumbnail_url=None,
                image_url=None,
                button_label="Get Help",
                button_emoji="📩",
                button_style="green",
                support_role=None,
            )

            assert guild.create_category.await_count == 2
            assert guild.create_text_channel.await_count == 2
            config = await cog._ticket_config(guild.id)
            assert config[0] == replacement_category.id
            assert config[5] == "[20]"
            assert "Ticket setup complete" in ctx.send.await_args.args[0]
        finally:
            await database.close()

    asyncio.run(verify())


def test_ticket_setup_rollback_ignores_resources_already_missing_from_discord() -> None:
    import discord

    async def verify() -> None:
        response = SimpleNamespace(status=404, reason="Not Found")
        missing_channel = discord.NotFound(response, {"code": 10003, "message": "Unknown Channel"})
        category = SimpleNamespace(id=10, delete=AsyncMock(side_effect=missing_channel))
        cog = DenycordInteractionFeatures(SimpleNamespace())

        await cog._rollback_setup(None, None, category)

        category.delete.assert_awaited_once_with(
            reason="Rolling back incomplete ticket setup"
        )

    asyncio.run(verify())


def test_dropdown_ticket_panel_routes_selected_category() -> None:
    import discord
    import denycord_interaction_features

    async def verify() -> None:
        cog = DenycordInteractionFeatures(SimpleNamespace())
        cog.open_ticket = AsyncMock()
        category = SimpleNamespace(id=123, name="Billing")
        view = denycord_interaction_features.TicketCategorySelectView(cog, [category])
        select = view.children[0]
        select._values = ["123"]
        interaction = SimpleNamespace()

        await select.callback(interaction)

        cog.open_ticket.assert_awaited_once_with(interaction, category_id=123)
        button_view = denycord_interaction_features.TicketCategoryButtonsView(
            cog,
            [(123, "Billing"), (456, "Technical")],
            style=discord.ButtonStyle.success,
        )
        assert [button.label for button in button_view.children] == ["Billing", "Technical"]
        await button_view.children[0].callback(interaction)
        assert cog.open_ticket.await_args.kwargs == {"category_id": 123}

    asyncio.run(verify())


def test_configured_ticket_panel_controls_route_to_the_selected_panel() -> None:
    import denycord_interaction_features

    async def verify() -> None:
        cog = DenycordInteractionFeatures(SimpleNamespace())
        cog.open_ticket = AsyncMock()
        item = {"label": "Billing", "description": "Payment help", "emoji": "💳", "category_id": 321}
        interaction = SimpleNamespace()

        button_view = denycord_interaction_features.ConfiguredTicketPanelView(
            cog, 77, "button", [item], "Choose a ticket type"
        )
        await button_view.children[0].callback(interaction)
        cog.open_ticket.assert_awaited_once_with(
            interaction,
            category_id=321,
            panel_id=77,
        )

        cog.open_ticket.reset_mock()
        dropdown_view = denycord_interaction_features.ConfiguredTicketPanelView(
            cog, 88, "dropdown", [item], "Choose a ticket type"
        )
        select = dropdown_view.children[0]
        select._values = ["321"]
        await select.callback(interaction)
        cog.open_ticket.assert_awaited_once_with(
            interaction,
            category_id=321,
            panel_id=88,
        )

    asyncio.run(verify())


def test_ticket_support_role_authorizes_staff_actions_and_panel_button_is_customizable() -> None:
    import denycord_interaction_features

    async def verify() -> None:
        cog = DenycordInteractionFeatures(SimpleNamespace())
        support_role = SimpleNamespace(id=77)
        cog._ticket_config = AsyncMock(return_value=(1, 2, 3, 4, "button", "[1]", 1, 77))
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            author=SimpleNamespace(
                guild_permissions=SimpleNamespace(manage_channels=False, administrator=False),
                roles=[support_role],
            ),
            cog=cog,
        )
        assert await denycord_interaction_features._ticket_staff_check(ctx)
        ctx.author.roles = []
        assert not await denycord_interaction_features._ticket_staff_check(ctx)

        view = denycord_interaction_features.TicketPanelView(
            cog,
            label="Contact Support",
            emoji="📩",
            style=discord.ButtonStyle.success,
        )
        button = view.children[0]
        assert button.label == "Contact Support"
        assert str(button.emoji) == "📩"
        assert button.style is discord.ButtonStyle.success

    import discord

    asyncio.run(verify())