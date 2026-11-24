import discord


TEXT_DISPLAY_LIMIT = 1900
GOLD_ACCENT_COLOR = 0xFFDD00
SELECT_COMPONENT_TYPES = (
    discord.ui.Select,
    discord.ui.ChannelSelect,
    discord.ui.RoleSelect,
    discord.ui.UserSelect,
    discord.ui.MentionableSelect,
)


def _split_text(value):
    remaining = str(value)
    while len(remaining) > TEXT_DISPLAY_LIMIT:
        split_at = remaining.rfind("\n", 0, TEXT_DISPLAY_LIMIT)
        if split_at <= 0:
            split_at = TEXT_DISPLAY_LIMIT
        yield remaining[:split_at]
        remaining = remaining[split_at:].lstrip("\n")
    if remaining:
        yield remaining


def _add_text(container, value):
    for chunk in _split_text(value):
        container.add_item(discord.ui.TextDisplay(chunk))


def _embed_container(embed):
    embed_color = embed.to_dict().get("color", GOLD_ACCENT_COLOR)
    container = discord.ui.Container(accent_color=embed_color)
    body_parts = []
    author = embed.author
    if author.name:
        if author.icon_url:
            author_text = f"**[{author.name}]({author.url})**" if author.url else f"**{author.name}**"
            container.add_item(
                discord.ui.Section(
                    discord.ui.TextDisplay(author_text),
                    accessory=discord.ui.Thumbnail(media=author.icon_url),
                )
            )
        else:
            author_text = f"-# [{author.name}]({author.url})" if author.url else f"-# {author.name}"
            body_parts.append(author_text)
    if embed.title:
        title = f"**[{embed.title}]({embed.url})**" if embed.url else f"**{embed.title}**"
        body_parts.append(title)
    if embed.description:
        body_parts.append(embed.description)
    for field in embed.fields:
        body_parts.append(f"**{field.name}**\n{field.value}" if field.name else field.value)
    thumbnail_url = embed.thumbnail.url
    if body_parts:
        text_chunks = list(_split_text("\n\n".join(body_parts)))
        if thumbnail_url:
            section_chunks, remaining_chunks = text_chunks[:3], text_chunks[3:]
            container.add_item(
                discord.ui.Section(
                    *(discord.ui.TextDisplay(chunk) for chunk in section_chunks),
                    accessory=discord.ui.Thumbnail(media=thumbnail_url),
                )
            )
            for chunk in remaining_chunks:
                container.add_item(discord.ui.TextDisplay(chunk))
        else:
            _add_text(container, "\n\n".join(body_parts))
    elif thumbnail_url:
        container.add_item(
            discord.ui.Section(
                discord.ui.TextDisplay("** **"),
                accessory=discord.ui.Thumbnail(media=thumbnail_url),
            )
        )

    image_urls = []
    for url in (embed.image.url,):
        if not url:
            continue
        image_urls.append(url)
    if image_urls:
        gallery = discord.ui.MediaGallery()
        for url in image_urls:
            gallery.add_item(media=url)
        container.add_item(gallery)

    footer_parts = []
    if embed.footer.text:
        footer_parts.append(embed.footer.text)
    if embed.timestamp:
        footer_parts.append(f"<t:{int(embed.timestamp.timestamp())}:f>")
    if footer_parts:
        _add_text(container, f"-# {' | '.join(footer_parts)}")
    if not container.children:
        container.add_item(discord.ui.TextDisplay("** **"))
    return container


def _add_legacy_view(legacy_view, layout):
    pending_buttons = []
    component_rows = []

    def flush_buttons():
        if pending_buttons:
            component_rows.append(list(pending_buttons))
            pending_buttons.clear()

    for item in legacy_view.children:
        if isinstance(item, discord.ui.Button):
            pending_buttons.append(item)
            if len(pending_buttons) == 5:
                flush_buttons()
        elif isinstance(item, SELECT_COMPONENT_TYPES):
            flush_buttons()
            component_rows.append([item])
    flush_buttons()

    bridged_items = []
    for source_row in component_rows:
        copied_row = []
        for source in source_row:
            if isinstance(source, discord.ui.Button):
                copied = discord.ui.Button.from_component(source._underlying)
            else:
                copied = type(source).from_component(source._underlying)

            if not (isinstance(source, discord.ui.Button) and source.url):
                async def callback(interaction, original=source, component=copied):
                    check = getattr(legacy_view, "interaction_check", None)
                    if check and not await check(interaction):
                        return
                    item_check = getattr(original, "interaction_check", None)
                    if item_check and not await item_check(interaction):
                        return
                    if isinstance(original, SELECT_COMPONENT_TYPES):
                        original._values = getattr(component, "_values", [])
                        if hasattr(original, "_resolved"):
                            original._resolved = getattr(component, "_resolved", None)
                    await original.callback(interaction)
                    if legacy_view.is_finished():
                        current_layout = getattr(legacy_view, "_v2_layout", layout)
                        for bridged in current_layout.walk_children():
                            if hasattr(bridged, "disabled"):
                                bridged.disabled = True
                        try:
                            await interaction.message.edit(view=current_layout)
                        except discord.HTTPException:
                            pass
                        current_layout.stop()

                copied.callback = callback
            copied_row.append(copied)
            bridged_items.append((source, copied))
        layout.add_item(discord.ui.ActionRow(*copied_row))
    return bridged_items


def _bind_layout_message(layout, message):
    if message is not None:
        layout._v2_message = message


def _message_uses_components_v2(message):
    return getattr(getattr(message, "flags", None), "is_components_v2", False)


async def _bind_interaction_layout_message(interaction, response, layout):
    message = getattr(response, "resource", None)
    if message is None and layout.timeout is not None:
        try:
            message = await interaction.original_response()
        except discord.HTTPException:
            pass
    _bind_layout_message(layout, message)


def make_layout_view(
    content=None,
    embed=None,
    embeds=None,
    legacy_view=None,
    file=None,
    files=None,
):
    timeout = legacy_view.timeout if legacy_view is not None else None
    layout = discord.ui.LayoutView(timeout=timeout)
    if content:
        container = discord.ui.Container()
        _add_text(container, content)
        layout.add_item(container)

    all_embeds = list(embeds or ())
    if embed is not None:
        all_embeds.insert(0, embed)
    for item in all_embeds:
        layout.add_item(_embed_container(item))

    uploaded_files = list(files or ())
    if file is not None:
        uploaded_files.insert(0, file)
    if uploaded_files:
        referenced = {
            url.removeprefix("attachment://")
            for item in all_embeds
            for url in (item.thumbnail.url, item.image.url)
            if url and url.startswith("attachment://")
        }
        container = next(
            (item for item in reversed(layout.children) if isinstance(item, discord.ui.Container)),
            None,
        )
        if container is None:
            container = discord.ui.Container()
            layout.add_item(container)
        for uploaded in uploaded_files:
            filename = getattr(uploaded, "filename", None)
            if filename and filename not in referenced:
                container.add_item(discord.ui.File(media=f"attachment://{filename}"))

    if not layout.children:
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("** **"))
        layout.add_item(container)
    if legacy_view is not None:
        legacy_view._v2_layout = layout
        _add_legacy_view(legacy_view, layout)

        async def on_timeout():
            try:
                await legacy_view.on_timeout()
            finally:
                current_layout = getattr(legacy_view, "_v2_layout", layout)
                for bridged in current_layout.walk_children():
                    if hasattr(bridged, "disabled"):
                        bridged.disabled = True
                try:
                    message = getattr(current_layout, "_v2_message", None)
                    if message is not None:
                        await message.edit(view=current_layout)
                except discord.HTTPException:
                    pass

        layout.on_timeout = on_timeout
    return layout


async def send_v2(
    destination,
    content=None,
    *,
    embed=None,
    embeds=None,
    view=None,
    ephemeral=False,
    **kwargs,
):
    ephemeral = False
    send_kwargs = dict(kwargs)
    if ephemeral:
        send_kwargs["ephemeral"] = True
    if kwargs.get("poll") is not None or kwargs.get("stickers") or kwargs.get("tts", False):
        return await destination.send(
            content,
            embed=embed,
            embeds=embeds,
            view=view,
            **send_kwargs,
        )
    if isinstance(view, discord.ui.LayoutView):
        message = await destination.send(view=view, **send_kwargs)
        _bind_layout_message(view, message)
        return message
    if content is not None or embed is not None or embeds is not None or view is not None:
        layout = make_layout_view(
            content,
            embed,
            embeds,
            legacy_view=view,
            file=kwargs.get("file"),
            files=kwargs.get("files"),
        )
        message = await destination.send(view=layout, **send_kwargs)
        _bind_layout_message(layout, message)
        return message
    return await destination.send(content, **send_kwargs)


async def send_interaction_v2(
    interaction,
    content=None,
    *,
    embed=None,
    embeds=None,
    view=None,
    ephemeral=False,
    allow_ephemeral=False,
    **kwargs,
):
    if not allow_ephemeral:
        ephemeral = False
    response = interaction.response
    if kwargs.get("poll") is not None or kwargs.get("stickers") or kwargs.get("tts", False):
        return await response.send_message(
            content,
            embed=embed,
            embeds=embeds,
            view=view,
            ephemeral=ephemeral,
            **kwargs,
        )
    if isinstance(view, discord.ui.LayoutView):
        result = await response.send_message(view=view, ephemeral=ephemeral, **kwargs)
        await _bind_interaction_layout_message(interaction, result, view)
        return result
    if content is not None or embed is not None or embeds is not None or view is not None:
        layout = make_layout_view(
            content,
            embed,
            embeds,
            legacy_view=view,
            file=kwargs.get("file"),
            files=kwargs.get("files"),
        )
        result = await response.send_message(
            view=layout,
            ephemeral=ephemeral,
            **kwargs,
        )
        await _bind_interaction_layout_message(interaction, result, layout)
        return result
    return await response.send_message(content, ephemeral=ephemeral, **kwargs)


async def edit_interaction_v2(
    interaction,
    content=None,
    *,
    embed=None,
    embeds=None,
    view=None,
    **kwargs,
):
    if isinstance(view, discord.ui.LayoutView):
        if _message_uses_components_v2(getattr(interaction, "message", None)):
            return await interaction.response.edit_message(view=view, **kwargs)
        return await interaction.response.edit_message(
            content=None,
            embeds=[],
            view=view,
            **kwargs,
        )
    message = getattr(interaction, "message", None)
    is_components_v2 = _message_uses_components_v2(message)
    if content is not None or embed is not None or embeds is not None:
        layout = make_layout_view(content, embed, embeds, legacy_view=view)
        if is_components_v2:
            result = await interaction.response.edit_message(view=layout, **kwargs)
        else:
            result = await interaction.response.edit_message(
                content=None,
                embeds=[],
                view=layout,
                **kwargs,
            )
        _bind_layout_message(layout, message)
        return result
    if view is not None:
        layout = make_layout_view(legacy_view=view)
        if is_components_v2:
            result = await interaction.response.edit_message(view=layout, **kwargs)
        else:
            result = await interaction.response.edit_message(
                content=None,
                embeds=[],
                view=layout,
                **kwargs,
            )
        _bind_layout_message(layout, message)
        return result
    return await interaction.response.edit_message(content=content, **kwargs)


async def edit_message_v2(
    message,
    content=None,
    *,
    embed=None,
    embeds=None,
    view=None,
    **kwargs,
):
    if isinstance(view, discord.ui.LayoutView):
        if _message_uses_components_v2(message):
            return await message.edit(view=view, **kwargs)
        result = await message.edit(content=None, embeds=[], view=view, **kwargs)
        _bind_layout_message(view, message)
        return result
    if content is not None or embed is not None or embeds is not None:
        layout = make_layout_view(content, embed, embeds, legacy_view=view)
        if _message_uses_components_v2(message):
            result = await message.edit(view=layout, **kwargs)
            _bind_layout_message(layout, message)
            return result
        result = await message.edit(
            content=None,
            embeds=[],
            view=layout,
            **kwargs,
        )
        _bind_layout_message(layout, message)
        return result
    if view is not None:
        layout = make_layout_view(legacy_view=view)
        if _message_uses_components_v2(message):
            result = await message.edit(view=layout, **kwargs)
            _bind_layout_message(layout, message)
            return result
        result = await message.edit(
            content=None,
            embeds=[],
            view=layout,
            **kwargs,
        )
        _bind_layout_message(layout, message)
        return result
    return await message.edit(content=content, **kwargs)
