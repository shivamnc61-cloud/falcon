import io
import unicodedata

from PIL import Image, ImageDraw, ImageOps


def normalize_card_text(text: str, font, *, fallback: str = "Member") -> str:
    value = unicodedata.normalize("NFKC", str(text or "").strip()) or fallback
    missing_glyph = bytes(font.getmask("\U0010ffff"))
    characters = []
    for character in value:
        if unicodedata.category(character) == "Cf" or character in "\ufe0e\ufe0f":
            continue
        if bytes(font.getmask(character)) == missing_glyph:
            character = "?"
        characters.append(character)
    return "".join(characters) or fallback


def draw_bot_icon(
    image: Image.Image,
    box: tuple[int, int, int, int],
    icon_bytes: bytes | None = None,
) -> None:
    left, top, right, bottom = box
    diameter = min(right - left, bottom - top)
    icon_left = left + (right - left - diameter) // 2
    icon_top = top + (bottom - top - diameter) // 2
    draw = ImageDraw.Draw(image)
    draw.ellipse(
        (icon_left, icon_top, icon_left + diameter, icon_top + diameter),
        fill="#FFF8F6",
        outline="#E7C78F",
        width=max(3, diameter // 32),
    )
    inset = max(6, diameter // 14)
    inner_box = (
        icon_left + inset,
        icon_top + inset,
        icon_left + diameter - inset,
        icon_top + diameter - inset,
    )
    inner_size = inner_box[2] - inner_box[0]
    if icon_bytes:
        icon = Image.open(io.BytesIO(icon_bytes)).convert("RGBA")
        icon = ImageOps.fit(icon, (inner_size, inner_size))
        mask = Image.new("L", (inner_size, inner_size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, inner_size - 1, inner_size - 1), fill=255)
        image.paste(icon, (inner_box[0], inner_box[1]), mask)
        return

    center_x = (inner_box[0] + inner_box[2]) // 2
    draw.line(
        (center_x, inner_box[1], center_x, inner_box[1] + inner_size // 7),
        fill="#A23B5A",
        width=max(3, diameter // 32),
    )
    antenna_radius = max(3, diameter // 18)
    draw.ellipse(
        (
            center_x - antenna_radius,
            inner_box[1] - antenna_radius,
            center_x + antenna_radius,
            inner_box[1] + antenna_radius,
        ),
        fill="#F5C7D1",
        outline="#A23B5A",
        width=max(2, diameter // 48),
    )
    head_top = inner_box[1] + inner_size // 6
    head_bottom = inner_box[3] - inner_size // 9
    draw.rounded_rectangle(
        (inner_box[0], head_top, inner_box[2], head_bottom),
        radius=diameter // 7,
        fill="#F5C7D1",
        outline="#A23B5A",
        width=max(3, diameter // 32),
    )
    eye_radius = max(3, diameter // 22)
    eye_y = head_top + (head_bottom - head_top) // 2
    eye_offset = inner_size // 4
    draw.ellipse(
        (center_x - eye_offset - eye_radius, eye_y - eye_radius,
         center_x - eye_offset + eye_radius, eye_y + eye_radius),
        fill="#422C34",
    )
    draw.ellipse(
        (center_x + eye_offset - eye_radius, eye_y - eye_radius,
         center_x + eye_offset + eye_radius, eye_y + eye_radius),
        fill="#422C34",
    )
    mouth_width = inner_size // 3
    draw.rounded_rectangle(
        (center_x - mouth_width // 2, head_bottom - inner_size // 5,
         center_x + mouth_width // 2, head_bottom - inner_size // 8),
        radius=max(2, diameter // 48),
        fill="#A23B5A",
    )
