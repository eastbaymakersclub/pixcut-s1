#!/usr/bin/env python3
"""Generate East Bay Makers Club sticker sheets for the PixCut S1."""

from __future__ import annotations

from pathlib import Path
import math

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output"
CANVAS_3X3 = 1200
CANVAS_4X7 = (1200, 2100)
STICKER = 485
GAP = 70
MARGIN = 80


COLORS = {
    "ink": (18, 23, 27, 255),
    "ink2": (25, 34, 38, 255),
    "paper": (244, 247, 239, 255),
    "white": (255, 255, 255, 255),
    "muted": (179, 190, 188, 255),
    "teal": (35, 190, 168, 255),
    "green": (145, 222, 98, 255),
    "orange": (255, 139, 72, 255),
    "magenta": (238, 92, 133, 255),
    "blue": (79, 148, 255, 255),
    "hair": (255, 255, 255, 70),
}


def font(candidates: list[str], size: int) -> ImageFont.FreeTypeFont:
    for path in candidates:
        p = Path(path).expanduser()
        if p.exists():
            return ImageFont.truetype(str(p), size=size)
    return ImageFont.load_default(size=size)


MONO_BOLD = [
    "~/Library/Fonts/SpaceMono-Bold.ttf",
    "/System/Library/Fonts/SFNSMono.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]
MONO = [
    "~/Library/Fonts/SpaceMono-Regular.ttf",
    "/System/Library/Fonts/SFNSMono.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]
SANS_BOLD = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]


def text_center(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], text: str, fnt: ImageFont.ImageFont, fill) -> None:
    bbox = draw.textbbox((0, 0), text, font=fnt)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    x = box[0] + (box[2] - box[0] - tw) / 2
    y = box[1] + (box[3] - box[1] - th) / 2 - bbox[1]
    draw.text((x, y), text, font=fnt, fill=fill)


def rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    return mask


def circle_mask(size: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    d.ellipse((0, 0, size - 1, size - 1), fill=255)
    return mask


def hex_mask(size: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    cx = cy = size / 2
    r = size * 0.49
    pts = []
    for i in range(6):
        a = math.radians(30 + 60 * i)
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    d.polygon(pts, fill=255)
    return mask


def paste_sticker(canvas: Image.Image, layer: Image.Image, mask: Image.Image, xy: tuple[int, int]) -> None:
    clipped = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    clipped.paste(layer, (0, 0), mask)
    canvas.alpha_composite(clipped, xy)


def flatten_for_print(canvas: Image.Image) -> Image.Image:
    flattened = Image.new("RGBA", canvas.size, COLORS["white"])
    flattened.alpha_composite(canvas)
    return flattened.convert("RGB")


def cut_source_from_alpha(alpha: Image.Image) -> Image.Image:
    cut_source = Image.new("RGBA", alpha.size, (0, 0, 0, 0))
    cut_source.putalpha(alpha)
    return cut_source


def draw_grid(d: ImageDraw.ImageDraw, size: int, color=(58, 67, 70, 255), step=40) -> None:
    for x in range(0, size, step):
        d.line((x, 0, x, size), fill=color, width=2)
    for y in range(0, size, step):
        d.line((0, y, size, y), fill=color, width=2)


def draw_badge_main() -> tuple[Image.Image, Image.Image]:
    size = STICKER
    mask = rounded_mask(size, 78)
    img = Image.new("RGBA", (size, size), COLORS["ink"])
    d = ImageDraw.Draw(img)
    draw_grid(d, size, step=46)
    d.rounded_rectangle((16, 16, size - 17, size - 17), radius=64, outline=COLORS["teal"], width=8)
    d.rounded_rectangle((32, 32, size - 33, size - 33), radius=50, outline=(83, 97, 99, 255), width=3)
    d.rectangle((0, 0, size, 62), fill=(35, 190, 168, 255))
    text_center(d, (0, 8, size, 58), "EAST BAY MAKERS CLUB", font(MONO_BOLD, 24), COLORS["ink"])

    text_center(d, (0, 125, size, 265), "EBMC", font(SANS_BOLD, 112), COLORS["white"])
    d.line((100, 288, size - 100, 288), fill=COLORS["orange"], width=8)
    text_center(d, (0, 305, size, 360), "BUILD / FIX / SHARE", font(MONO_BOLD, 28), COLORS["green"])
    text_center(d, (0, 370, size, 420), "BERKELEY CA", font(MONO, 24), COLORS["muted"])
    return img, mask


def draw_badge_cnc() -> tuple[Image.Image, Image.Image]:
    size = STICKER
    mask = circle_mask(size)
    img = Image.new("RGBA", (size, size), COLORS["paper"])
    d = ImageDraw.Draw(img)
    d.ellipse((17, 17, size - 18, size - 18), outline=COLORS["ink"], width=8)
    d.ellipse((36, 36, size - 37, size - 37), outline=COLORS["orange"], width=5)

    for i in range(9):
        x = 88 + i * 38
        d.line((x, 145, x + 30, 95), fill=(166, 170, 164, 255), width=8)
    d.rounded_rectangle((120, 155, size - 120, 230), radius=22, fill=COLORS["ink"])
    text_center(d, (120, 155, size - 120, 230), "TOOL CERT", font(MONO_BOLD, 32), COLORS["green"])
    text_center(d, (0, 245, size, 315), "CNC  LASER  3D", font(MONO_BOLD, 34), COLORS["ink"])
    d.line((145, 340, size - 145, 340), fill=COLORS["blue"], width=7)
    text_center(d, (0, 355, size, 405), "EBMC", font(MONO_BOLD, 48), COLORS["magenta"])
    return img, mask


def draw_badge_orbit() -> tuple[Image.Image, Image.Image]:
    size = STICKER
    mask = rounded_mask(size, 120)
    img = Image.new("RGBA", (size, size), (20, 22, 34, 255))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((18, 18, size - 19, size - 19), radius=104, outline=COLORS["green"], width=8)
    for r, col in [(165, COLORS["blue"]), (115, COLORS["magenta"]), (70, COLORS["orange"])]:
        d.ellipse((size / 2 - r, size / 2 - r, size / 2 + r, size / 2 + r), outline=col, width=5)
    for angle, col in [(20, COLORS["orange"]), (155, COLORS["green"]), (280, COLORS["blue"])]:
        a = math.radians(angle)
        x = size / 2 + 165 * math.cos(a)
        y = size / 2 + 165 * math.sin(a)
        d.ellipse((x - 15, y - 15, x + 15, y + 15), fill=col)
    text_center(d, (0, 120, size, 190), "BUILD", font(SANS_BOLD, 74), COLORS["white"])
    text_center(d, (0, 195, size, 260), "NIGHT", font(SANS_BOLD, 66), COLORS["white"])
    text_center(d, (0, 320, size, 375), "ARCHIVE DATA / EBMC", font(MONO_BOLD, 23), COLORS["muted"])
    return img, mask


def draw_badge_address() -> tuple[Image.Image, Image.Image]:
    size = STICKER
    mask = hex_mask(size)
    img = Image.new("RGBA", (size, size), (21, 42, 38, 255))
    d = ImageDraw.Draw(img)
    cx = cy = size // 2
    for offset in range(-260, 280, 46):
        d.line((offset, 0, offset + size, size), fill=(69, 91, 85, 255), width=3)
    d.ellipse((cx - 110, 85, cx + 110, 305), outline=COLORS["teal"], width=9)
    d.polygon([(cx, 335), (cx - 60, 260), (cx + 60, 260)], fill=COLORS["teal"])
    d.ellipse((cx - 42, 153, cx + 42, 237), fill=COLORS["ink"])
    text_center(d, (0, 52, size, 105), "FIND THE SHOP", font(MONO_BOLD, 27), COLORS["green"])
    d.rounded_rectangle((78, 344, size - 78, 438), radius=24, fill=(47, 69, 64, 255))
    text_center(d, (0, 350, size, 395), "1619 SAN PABLO", font(MONO_BOLD, 31), COLORS["white"])
    text_center(d, (0, 395, size, 435), "BERKELEY", font(MONO_BOLD, 30), COLORS["orange"])
    return img, mask


def draw_badge_member_made() -> tuple[Image.Image, Image.Image]:
    size = STICKER
    mask = rounded_mask(size, 88)
    img = Image.new("RGBA", (size, size), (244, 247, 239, 255))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((18, 18, size - 19, size - 19), radius=72, outline=COLORS["ink"], width=8)
    d.rounded_rectangle((38, 38, size - 39, 132), radius=28, fill=COLORS["teal"])
    text_center(d, (0, 42, size, 128), "MEMBER MADE", font(MONO_BOLD, 35), COLORS["ink"])
    d.polygon([(105, 190), (size - 105, 190), (size - 75, 275), (size - 140, 360), (140, 360), (75, 275)], fill=COLORS["ink"])
    text_center(d, (0, 205, size, 270), "TOOLS", font(SANS_BOLD, 62), COLORS["white"])
    text_center(d, (0, 280, size, 335), "IDEAS", font(SANS_BOLD, 54), COLORS["green"])
    text_center(d, (0, 374, size, 420), "EAST BAY MAKERS CLUB", font(MONO_BOLD, 22), COLORS["magenta"])
    return img, mask


def draw_badge_repair() -> tuple[Image.Image, Image.Image]:
    size = STICKER
    mask = circle_mask(size)
    img = Image.new("RGBA", (size, size), (22, 31, 45, 255))
    d = ImageDraw.Draw(img)
    d.ellipse((17, 17, size - 18, size - 18), outline=COLORS["green"], width=8)
    d.ellipse((42, 42, size - 43, size - 43), outline=COLORS["blue"], width=5)
    d.rounded_rectangle((138, 105, size - 138, 330), radius=32, fill=COLORS["paper"])
    d.rectangle((175, 82, size - 175, 118), fill=COLORS["orange"])
    d.rectangle((175, 317, size - 175, 352), fill=COLORS["orange"])
    text_center(d, (0, 122, size, 185), "REPAIR", font(SANS_BOLD, 58), COLORS["ink"])
    text_center(d, (0, 190, size, 245), "CAFE", font(SANS_BOLD, 58), COLORS["ink"])
    text_center(d, (0, 265, size, 320), "FIX IT FORWARD", font(MONO_BOLD, 24), COLORS["magenta"])
    text_center(d, (0, 360, size, 410), "EBMC", font(MONO_BOLD, 50), COLORS["white"])
    return img, mask


def checkerboard(width: int, height: int | None = None, cell: int = 32) -> Image.Image:
    height = width if height is None else height
    img = Image.new("RGB", (width, height), (238, 238, 238))
    d = ImageDraw.Draw(img)
    for y in range(0, height, cell):
        for x in range(0, width, cell):
            if (x // cell + y // cell) % 2:
                d.rectangle((x, y, x + cell - 1, y + cell - 1), fill=(204, 204, 204))
    return img


def render_3x3() -> None:
    global STICKER
    STICKER = 485
    canvas = Image.new("RGBA", (CANVAS_3X3, CANVAS_3X3), (0, 0, 0, 0))
    positions = [
        (MARGIN, MARGIN),
        (MARGIN + STICKER + GAP, MARGIN),
        (MARGIN, MARGIN + STICKER + GAP),
        (MARGIN + STICKER + GAP, MARGIN + STICKER + GAP),
    ]
    for draw_fn, xy in zip(
        [draw_badge_main, draw_badge_cnc, draw_badge_orbit, draw_badge_address],
        positions,
        strict=True,
    ):
        layer, mask = draw_fn()
        paste_sticker(canvas, layer, mask, xy)

    print_path = OUT / "ebmc_sticker_sheet_3x3.png"
    flat_print_path = OUT / "ebmc_sticker_sheet_3x3_print.png"
    flat_print_jpg_path = OUT / "ebmc_sticker_sheet_3x3_print.jpg"
    preview_path = OUT / "ebmc_sticker_sheet_preview.png"
    mask_path = OUT / "ebmc_sticker_sheet_alpha_mask.png"
    cut_source_path = OUT / "ebmc_sticker_sheet_3x3_cut_source.png"

    canvas.save(print_path)
    print_image = flatten_for_print(canvas)
    print_image.save(flat_print_path)
    print_image.save(flat_print_jpg_path, quality=92, subsampling=0)

    preview = checkerboard(CANVAS_3X3)
    preview = preview.convert("RGBA")
    preview.alpha_composite(canvas)
    draw = ImageDraw.Draw(preview)
    draw.rectangle((0, 0, CANVAS_3X3 - 1, CANVAS_3X3 - 1), outline=(255, 0, 0, 190), width=6)
    text_center(draw, (0, 1142, CANVAS_3X3, 1196), "3x3 PRINT ART - RED EDGE IS MEDIA PREVIEW ONLY", font(MONO_BOLD, 28), (255, 255, 255, 230))
    preview.save(preview_path)

    alpha = canvas.getchannel("A")
    alpha.save(mask_path)
    cut_source_from_alpha(alpha).save(cut_source_path)

    print(print_path)
    print(flat_print_path)
    print(flat_print_jpg_path)
    print(preview_path)
    print(mask_path)
    print(cut_source_path)


def render_4x7() -> None:
    global STICKER
    STICKER = 485
    width, height = CANVAS_4X7
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    positions = [
        (80, 95),
        (635, 95),
        (80, 735),
        (635, 735),
        (80, 1375),
        (635, 1375),
    ]
    draw_fns = [
        draw_badge_main,
        draw_badge_cnc,
        draw_badge_orbit,
        draw_badge_address,
        draw_badge_member_made,
        draw_badge_repair,
    ]
    for draw_fn, xy in zip(draw_fns, positions, strict=True):
        layer, mask = draw_fn()
        paste_sticker(canvas, layer, mask, xy)

    print_path = OUT / "ebmc_sticker_sheet_4x7.png"
    flat_print_path = OUT / "ebmc_sticker_sheet_4x7_print.png"
    flat_print_jpg_path = OUT / "ebmc_sticker_sheet_4x7_print.jpg"
    preview_path = OUT / "ebmc_sticker_sheet_4x7_preview.png"
    mask_path = OUT / "ebmc_sticker_sheet_4x7_alpha_mask.png"
    cut_source_path = OUT / "ebmc_sticker_sheet_4x7_cut_source.png"

    canvas.save(print_path)
    print_image = flatten_for_print(canvas)
    print_image.save(flat_print_path)
    print_image.save(flat_print_jpg_path, quality=92, subsampling=0)

    preview = checkerboard(width, height)
    preview = preview.convert("RGBA")
    preview.alpha_composite(canvas)
    draw = ImageDraw.Draw(preview)
    draw.rectangle((0, 0, width - 1, height - 1), outline=(255, 0, 0, 190), width=6)
    text_center(draw, (0, 2038, width, 2095), "4x7 PRINT ART - RED EDGE IS MEDIA PREVIEW ONLY", font(MONO_BOLD, 28), (255, 255, 255, 230))
    preview.save(preview_path)

    alpha = canvas.getchannel("A")
    alpha.save(mask_path)
    cut_source_from_alpha(alpha).save(cut_source_path)

    print(print_path)
    print(flat_print_path)
    print(flat_print_jpg_path)
    print(preview_path)
    print(mask_path)
    print(cut_source_path)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    render_3x3()
    render_4x7()


if __name__ == "__main__":
    main()
