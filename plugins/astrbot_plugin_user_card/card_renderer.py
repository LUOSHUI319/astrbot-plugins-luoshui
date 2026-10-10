"""用户卡片渲染器：把字段数据画成一张图片。"""

from __future__ import annotations

import os
import re
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

FONT_DIRS = [
    r"C:\Windows\Fonts",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\Fonts"),
    "/usr/share/fonts/truetype/wqy",
    "/usr/share/fonts/opentype/noto",
]

FONT_CANDIDATES = {
    "regular": ["simyou.ttf", "msyh.ttc", "simhei.ttf", "simsun.ttc", "wqy-microhei.ttc"],
    "bold": ["msyhbd.ttc", "msyh.ttc", "simhei.ttf", "wqy-microhei.ttc"],
    "handwrite": ["LXGWWenKai-Regular.ttf", "simyou.ttf", "simkai.ttf", "msyh.ttc"],
    "emoji": ["seguiemj.ttf", "NotoColorEmoji.ttf"],
}

EMOJI_RE = re.compile(
    r"[\U0001F000-\U0001FAFF\u2190-\u21FF\u2300-\u23FF\u2460-\u24FF"
    r"\u25A0-\u27BF\u2B00-\u2BFF\uFE0F\u200D\u20E3]+"
)

THEMES = {
    "cute": {
        "bg_top": (252, 231, 245),
        "bg_bottom": (219, 226, 252),
        "card": (255, 255, 255),
        "border": (255, 197, 227),
        "border_width": 4,
        "value": (74, 68, 88),
        "footer": (176, 168, 188),
        "font": "regular",
        "label_colors": [
            (155, 89, 182),
            (52, 152, 219),
            (46, 204, 113),
            (230, 126, 34),
            (231, 76, 120),
        ],
    },
    "classic": {
        "bg_top": (255, 255, 255),
        "bg_bottom": (243, 244, 250),
        "card": (255, 255, 255),
        "border": (206, 212, 224),
        "border_width": 3,
        "value": (48, 52, 64),
        "footer": (150, 155, 168),
        "font": "regular",
        "label_colors": [
            (60, 132, 96),
            (196, 94, 148),
            (58, 116, 188),
            (196, 128, 44),
            (120, 96, 200),
        ],
    },
    "handwrite": {
        "bg_top": (245, 245, 247),
        "bg_bottom": (236, 237, 242),
        "card": (255, 255, 255),
        "border": (43, 43, 43),
        "border_width": 3,
        "value": (68, 68, 68),
        "footer": (170, 170, 176),
        "font": "handwrite",
        "label_colors": [
            (63, 164, 91),
            (224, 104, 154),
            (74, 134, 200),
            (142, 124, 195),
            (215, 154, 70),
        ],
    },
}

CARD_W = 1020
MARGIN = 16
AVATAR_D = 336
LINE_HEIGHT = 36
TOP_EXTRA = 46
BOTTOM_EXTRA = 62
TEXT_LEFT = 472
FONT_SIZE = 30
FOOTER_INSET = 42

_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def _find_font_file(key: str) -> str | None:
    for name in FONT_CANDIDATES.get(key, []):
        for folder in FONT_DIRS:
            path = os.path.join(folder, name)
            if os.path.exists(path):
                return path
    return None


def _load_font(size: int, key: str = "regular") -> ImageFont.FreeTypeFont:
    cache_key = (key, size)
    if cache_key in _font_cache:
        return _font_cache[cache_key]
    path = _find_font_file(key)
    font = None
    if path:
        try:
            font = ImageFont.truetype(path, size)
        except Exception:
            font = None
    if font is None:
        font = ImageFont.load_default()
    _font_cache[cache_key] = font
    return font


def _segments(text: str):
    out = []
    last = 0
    for match in EMOJI_RE.finditer(text):
        if match.start() > last:
            out.append((text[last : match.start()], False))
        out.append((match.group(0), True))
        last = match.end()
    if last < len(text):
        out.append((text[last:], False))
    return out


def _draw_mixed(draw, x, y, text, font, emoji_font, fill):
    for chunk, is_emoji in _segments(text):
        use = emoji_font if (is_emoji and emoji_font) else font
        try:
            draw.text((x, y), chunk, font=use, fill=fill, embedded_color=bool(is_emoji and emoji_font))
        except TypeError:
            draw.text((x, y), chunk, font=use, fill=fill)
        try:
            x += draw.textlength(chunk, font=use)
        except Exception:
            x += len(chunk) * 12
    return x


def _measure_mixed(draw, text, font, emoji_font):
    total = 0.0
    for chunk, is_emoji in _segments(text):
        use = emoji_font if (is_emoji and emoji_font) else font
        try:
            total += draw.textlength(chunk, font=use)
        except Exception:
            total += len(chunk) * 12
    return total


def _vertical_gradient(width: int, height: int, top, bottom) -> Image.Image:
    if top == bottom:
        return Image.new("RGB", (width, height), top)
    strip = Image.new("RGB", (1, max(2, height)))
    px = strip.load()
    for y in range(strip.height):
        t = y / (strip.height - 1)
        px[0, y] = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
    return strip.resize((width, height), Image.BILINEAR)


def _circular(image: Image.Image, diameter: int) -> Image.Image:
    image = image.convert("RGB")
    side = min(image.size)
    left = (image.width - side) // 2
    top = (image.height - side) // 2
    image = image.crop((left, top, left + side, top + side)).resize(
        (diameter, diameter), Image.LANCZOS
    )
    mask = Image.new("L", (diameter, diameter), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, diameter - 1, diameter - 1], fill=255)
    out = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
    out.paste(image, (0, 0), mask)
    return out


def _placeholder_avatar(diameter: int, text: str = "?") -> Image.Image:
    tile = _vertical_gradient(diameter, diameter, (238, 220, 248), (214, 226, 250))
    out = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
    mask = Image.new("L", (diameter, diameter), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, diameter - 1, diameter - 1], fill=255)
    out.paste(tile, (0, 0), mask)
    draw = ImageDraw.Draw(out)
    font = _load_font(int(diameter * 0.42), "bold")
    bbox = draw.textbbox((0, 0), text, font=font)
    draw.text(
        (
            (diameter - (bbox[2] - bbox[0])) / 2 - bbox[0],
            (diameter - (bbox[3] - bbox[1])) / 2 - bbox[1],
        ),
        text,
        font=font,
        fill=(255, 255, 255),
    )
    return out


def render_card(
    fields: list[tuple[str, str]],
    avatar: Image.Image | None = None,
    theme: str = "cute",
    footer: str = "",
) -> Image.Image:
    """把字段列表渲染成资料卡图片，高度随字段数量自适应。"""
    palette = THEMES.get(theme, THEMES["cute"])
    font_key = palette.get("font", "regular")
    count = max(1, len(fields))
    text_block_h = count * LINE_HEIGHT
    card_h = int(max(text_block_h + TOP_EXTRA + BOTTOM_EXTRA, AVATAR_D + 120) + MARGIN * 2)

    canvas = _vertical_gradient(CARD_W, card_h, palette["bg_top"], palette["bg_bottom"]).convert(
        "RGBA"
    )

    card = Image.new("RGBA", (CARD_W - MARGIN * 2, card_h - MARGIN * 2), (0, 0, 0, 0))
    card_draw = ImageDraw.Draw(card)
    card_draw.rounded_rectangle(
        [0, 0, card.width - 1, card.height - 1],
        radius=28,
        fill=palette["card"] + (255,),
        outline=palette["border"] + (255,),
        width=palette.get("border_width", 4),
    )
    canvas.alpha_composite(card, (MARGIN, MARGIN))
    draw = ImageDraw.Draw(canvas)

    avatar_x = MARGIN + 52
    avatar_y = (card_h - AVATAR_D) // 2
    inner = avatar if avatar is not None else _placeholder_avatar(AVATAR_D)
    ring = Image.new("RGBA", (AVATAR_D + 14, AVATAR_D + 14), (0, 0, 0, 0))
    ImageDraw.Draw(ring).ellipse(
        [0, 0, ring.width - 1, ring.height - 1], fill=palette["border"] + (255,)
    )
    canvas.alpha_composite(ring, (avatar_x - 7, avatar_y - 7))
    canvas.alpha_composite(_circular(inner, AVATAR_D), (avatar_x, avatar_y))

    label_font = _load_font(FONT_SIZE, font_key)
    value_font = _load_font(FONT_SIZE, font_key)
    emoji_font = _load_font(int(FONT_SIZE * 0.95), "emoji")
    if emoji_font.__class__.__name__ == "ImageFont":
        emoji_font = None

    label_colors = palette["label_colors"]
    text_top = (card_h - text_block_h) // 2
    y = text_top
    for index, (label, value) in enumerate(fields):
        color = label_colors[index % len(label_colors)]
        head = f"{label}："
        _draw_mixed(draw, TEXT_LEFT, y, head, label_font, None, color)
        head_width = _measure_mixed(draw, head, label_font, None)
        text = str(value)
        max_width = CARD_W - MARGIN - 36 - (TEXT_LEFT + head_width)
        if _measure_mixed(draw, text, value_font, emoji_font) > max_width:
            while text and _measure_mixed(draw, text + "…", value_font, emoji_font) > max_width:
                text = text[:-1]
            text = text + "…"
        _draw_mixed(draw, TEXT_LEFT + head_width, y, text, value_font, emoji_font, palette["value"])
        y += LINE_HEIGHT

    if footer:
        footer_font = _load_font(19, font_key)
        fw = _measure_mixed(draw, footer, footer_font, None)
        draw.text(
            (CARD_W - MARGIN - FOOTER_INSET - fw, card_h - MARGIN - FOOTER_INSET),
            footer,
            font=footer_font,
            fill=palette["footer"],
        )

    return canvas.convert("RGB")


def _load_font_file(path: str, size: int) -> ImageFont.FreeTypeFont:
    key = ("FILE:" + path, size)
    if key in _font_cache:
        return _font_cache[key]
    try:
        font = ImageFont.truetype(path, size)
    except Exception:
        font = _load_font(size, "regular")
    _font_cache[key] = font
    return font


BOX_FONT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "box.ttf")
BOX_EMOJI_FONT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fonts", "NotoColorEmoji.ttf"
)


def _random_deep_color(rng) -> tuple[int, int, int, int]:
    return (
        rng.randint(0, 128),
        rng.randint(0, 128),
        rng.randint(0, 128),
        rng.randint(240, 255),
    )


def render_box_card(
    fields: list[tuple[str, str]],
    avatar: Image.Image | None = None,
    rng=None,
    font_size: int = 35,
    line_height: int = 40,
    padding: int = 10,
    corner_radius: int = 30,
    border_thickness: int = 10,
) -> Image.Image:
    """复刻 astrbot_plugin_box 的画法：白底、随机深色文字、随机色粗边框、可爱手写字体。"""
    import random as _random

    rng = rng or _random.Random()
    lines = [f"{label}： {value}" for label, value in fields]

    cute_font = _load_font_file(BOX_FONT, font_size) if os.path.exists(BOX_FONT) else _load_font(
        font_size, "handwrite"
    )
    emoji_font = (
        _load_font_file(BOX_EMOJI_FONT, font_size)
        if os.path.exists(BOX_EMOJI_FONT)
        else None
    )

    probe = Image.new("RGBA", (1, 1))
    probe_draw = ImageDraw.Draw(probe)

    def line_width(line: str) -> float:
        width = 0.0
        for chunk, is_emoji in _segments(line):
            font = emoji_font if (is_emoji and emoji_font) else cute_font
            try:
                width += probe_draw.textlength(chunk, font=font)
            except Exception:
                width += len(chunk) * font_size * 0.6
        return width

    text_width = int(max((line_width(line) for line in lines), default=font_size))
    text_height = line_height * len(lines)
    padding = int(padding)
    img_height = text_height + padding * 2

    avatar_img = None
    avatar_size = text_height
    if avatar is not None:
        avatar_img = avatar.convert("RGBA")
        avatar_img = avatar_img.resize((avatar_size, avatar_size), Image.LANCZOS)
        mask = Image.new("L", (avatar_size, avatar_size), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, avatar_size - 1, avatar_size - 1], fill=255)
        avatar_img.putalpha(mask)

    content_width = (avatar_size if avatar_img else 0) + text_width + padding * 2
    canvas = Image.new("RGBA", (content_width, img_height), (255, 255, 255, 255))
    draw = ImageDraw.Draw(canvas)

    if avatar_img is not None:
        canvas.alpha_composite(avatar_img, (0, (img_height - avatar_size) // 2))

    left = (avatar_size if avatar_img else 0) + padding
    current_y = padding
    for line in lines:
        color = _random_deep_color(rng)
        current_x = left
        for chunk, is_emoji in _segments(line):
            font = emoji_font if (is_emoji and emoji_font) else cute_font
            offset = 10 if (is_emoji and emoji_font) else 0
            try:
                draw.text(
                    (current_x, current_y + offset),
                    chunk,
                    font=font,
                    fill=color,
                    embedded_color=bool(is_emoji and emoji_font),
                )
            except TypeError:
                draw.text((current_x, current_y + offset), chunk, font=font, fill=color)
            try:
                advance = probe_draw.textlength(chunk, font=font)
            except Exception:
                advance = len(chunk) * font_size * 0.6
            current_x += advance
        current_y += line_height

    border_color = tuple(rng.randint(64, 255) for _ in range(3))
    border = Image.new(
        "RGBA",
        (content_width + border_thickness * 2, img_height + border_thickness * 2),
        border_color + (255,),
    )
    border.paste(canvas, (border_thickness, border_thickness))
    return border.convert("RGB")


def save_card(image: Image.Image, out_dir: str, name: str | None = None) -> str:
    os.makedirs(out_dir, exist_ok=True)
    if not name:
        name = f"card_{datetime.now().strftime('%Y%m%d%H%M%S%f')}.png"
    path = os.path.join(out_dir, name)
    image.save(path, "PNG")
    return path
