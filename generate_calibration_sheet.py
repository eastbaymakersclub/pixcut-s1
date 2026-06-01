#!/usr/bin/env python3
"""Generate a repeatable PixCut S1 print/cut calibration sheet."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent
DEFAULT_OUT = ROOT / "output" / "calibration"
PLOT_UNIT_SCALE = 1643 / 480
PLOT_X_ORIGIN = 7153.75
PLOT_Y_ORIGIN = -24.75
PLOT_TRAILER_X = 6476
PLOT_TRAILER_Y = 0


@dataclass(frozen=True)
class CutTarget:
    name: str
    x_mm: float
    y_mm: float
    width_mm: float
    height_mm: float

    @property
    def center_mm(self) -> tuple[float, float]:
        return (self.x_mm + self.width_mm / 2, self.y_mm + self.height_mm / 2)


@dataclass(frozen=True)
class CalibrationConfig:
    canvas_px: tuple[int, int]
    nominal_media_mm: tuple[float, float]
    basename: str
    cut_target_mm: tuple[float, float]
    horizontal_scale_bar_mm: tuple[float, float, float]
    vertical_scale_bar_mm: tuple[float, float, float]
    print_scale_correction: tuple[float, float]
    print_offset_mm: tuple[float, float]
    transform_origin: str

    @property
    def px_per_mm_x(self) -> float:
        return self.canvas_px[0] / self.nominal_media_mm[0]

    @property
    def px_per_mm_y(self) -> float:
        return self.canvas_px[1] / self.nominal_media_mm[1]

    def transformed_mm(self, x_mm: float, y_mm: float) -> tuple[float, float]:
        scale_x, scale_y = self.print_scale_correction
        offset_x, offset_y = self.print_offset_mm
        if self.transform_origin == "center":
            center_x = self.nominal_media_mm[0] / 2
            center_y = self.nominal_media_mm[1] / 2
            return (
                center_x + (x_mm - center_x) * scale_x + offset_x,
                center_y + (y_mm - center_y) * scale_y + offset_y,
            )
        return (x_mm * scale_x + offset_x, y_mm * scale_y + offset_y)

    def x_px(self, mm: float) -> int:
        x_mm, _ = self.transformed_mm(mm, 0)
        return round(x_mm * self.px_per_mm_x)

    def y_px(self, mm: float) -> int:
        _, y_mm = self.transformed_mm(0, mm)
        return round(y_mm * self.px_per_mm_y)

    def box_px(self, x_mm: float, y_mm: float, w_mm: float, h_mm: float) -> tuple[int, int, int, int]:
        x0 = self.x_px(x_mm)
        y0 = self.y_px(y_mm)
        x1 = self.x_px(x_mm + w_mm) - 1
        y1 = self.y_px(y_mm + h_mm) - 1
        return (x0, y0, x1, y1)


def parse_pair(value: str, sep: str, cast: type) -> tuple:
    parts = value.lower().split(sep)
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"Expected A{sep}B, got {value!r}")
    return (cast(parts[0]), cast(parts[1]))


def font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    ]
    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default(size=size)


def draw_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    size: int = 18,
    fill: tuple[int, int, int] = (20, 20, 20),
    bold: bool = False,
    anchor: str | None = None,
) -> None:
    draw.text(xy, text, font=font(size, bold=bold), fill=fill, anchor=anchor)


def draw_centered_text(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    text: str,
    size: int,
    fill: tuple[int, int, int] = (20, 20, 20),
    bold: bool = False,
) -> None:
    fnt = font(size, bold=bold)
    bbox = draw.textbbox((0, 0), text, font=fnt)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    x = box[0] + (box[2] - box[0] - tw) / 2
    y = box[1] + (box[3] - box[1] - th) / 2 - bbox[1]
    draw.text((x, y), text, font=fnt, fill=fill)


def line_mm(
    draw: ImageDraw.ImageDraw,
    cfg: CalibrationConfig,
    start_mm: tuple[float, float],
    end_mm: tuple[float, float],
    fill: tuple[int, int, int],
    width_px: int = 1,
) -> None:
    draw.line(
        (cfg.x_px(start_mm[0]), cfg.y_px(start_mm[1]), cfg.x_px(end_mm[0]), cfg.y_px(end_mm[1])),
        fill=fill,
        width=width_px,
    )


def rectangle_mm(
    draw: ImageDraw.ImageDraw,
    cfg: CalibrationConfig,
    x_mm: float,
    y_mm: float,
    w_mm: float,
    h_mm: float,
    outline: tuple[int, int, int],
    width_px: int = 1,
) -> None:
    draw.rectangle(cfg.box_px(x_mm, y_mm, w_mm, h_mm), outline=outline, width=width_px)


def draw_crosshair(
    draw: ImageDraw.ImageDraw,
    cfg: CalibrationConfig,
    center_mm: tuple[float, float],
    arm_mm: float,
    fill: tuple[int, int, int],
    width_px: int = 2,
) -> None:
    cx, cy = center_mm
    line_mm(draw, cfg, (cx - arm_mm, cy), (cx + arm_mm, cy), fill, width_px)
    line_mm(draw, cfg, (cx, cy - arm_mm), (cx, cy + arm_mm), fill, width_px)
    r = max(2, round(width_px * 1.5))
    x = cfg.x_px(cx)
    y = cfg.y_px(cy)
    draw.ellipse((x - r, y - r, x + r, y + r), fill=fill)


def draw_grid(draw: ImageDraw.ImageDraw, cfg: CalibrationConfig) -> None:
    width_mm, height_mm = cfg.nominal_media_mm
    light = (226, 229, 232)
    mid = (188, 193, 199)
    dark = (112, 118, 125)

    for x in range(0, int(width_mm) + 1, 5):
        is_major = x % 10 == 0
        line_mm(draw, cfg, (x, 0), (x, height_mm), mid if is_major else light, 2 if is_major else 1)
    for y in range(0, int(height_mm) + 1, 5):
        is_major = y % 10 == 0
        line_mm(draw, cfg, (0, y), (width_mm, y), mid if is_major else light, 2 if is_major else 1)

    for x in range(0, int(width_mm) + 1, 10):
        draw_text(draw, (cfg.x_px(x) + 3, cfg.y_px(8.3)), f"{x}", 14, dark)
        draw_text(draw, (cfg.x_px(x) + 3, cfg.y_px(height_mm - 4)), f"{x}", 14, dark)
    for y in range(0, int(height_mm) + 1, 10):
        draw_text(draw, (cfg.x_px(1.2), cfg.y_px(y) + 2), f"{y}", 14, dark)
        draw_text(draw, (cfg.x_px(width_mm - 7.0), cfg.y_px(y) + 2), f"{y}", 14, dark)


def draw_edge_printability_marks(draw: ImageDraw.ImageDraw, cfg: CalibrationConfig) -> None:
    width_mm, height_mm = cfg.nominal_media_mm
    colors = {
        0: (15, 15, 15),
        1: (210, 30, 30),
        2: (30, 120, 220),
        3: (20, 150, 80),
        5: (190, 120, 20),
        10: (90, 90, 90),
    }
    for inset, color in colors.items():
        rectangle_mm(draw, cfg, inset, inset, width_mm - 2 * inset, height_mm - 2 * inset, color, 2)

    draw_text(draw, (cfg.x_px(4), cfg.y_px(6.1)), "inset print test: 0,1,2,3,5,10 mm", 15, (20, 20, 20), bold=True)


def draw_scale_bars(draw: ImageDraw.ImageDraw, cfg: CalibrationConfig) -> dict[str, dict[str, float]]:
    hx0, hx1, hy = cfg.horizontal_scale_bar_mm
    vx, vy0, vy1 = cfg.vertical_scale_bar_mm

    black = (0, 0, 0)
    blue = (0, 90, 210)
    orange = (210, 100, 0)

    line_mm(draw, cfg, (hx0, hy), (hx1, hy), black, 4)
    for x in (hx0, hx1):
        line_mm(draw, cfg, (x, hy - 3), (x, hy + 3), black, 4)
        draw_crosshair(draw, cfg, (x, hy), 2.0, blue, 2)
    draw_centered_text(
        draw,
        (cfg.x_px(hx0), cfg.y_px(hy + 2.0), cfg.x_px(hx1), cfg.y_px(hy + 7.0)),
        f"H{hx1 - hx0:.2f} mm print scale",
        18,
        black,
        bold=True,
    )

    line_mm(draw, cfg, (vx, vy0), (vx, vy1), black, 4)
    for y in (vy0, vy1):
        line_mm(draw, cfg, (vx - 3, y), (vx + 3, y), black, 4)
        draw_crosshair(draw, cfg, (vx, y), 2.0, orange, 2)
    draw_text(draw, (cfg.x_px(vx + 2.0), cfg.y_px((vy0 + vy1) / 2 - 2.0)), f"V{vy1 - vy0:.2f} mm", 18, black, bold=True)

    return {
        "horizontal": {"x0_mm": hx0, "x1_mm": hx1, "y_mm": hy, "expected_length_mm": hx1 - hx0},
        "vertical": {"x_mm": vx, "y0_mm": vy0, "y1_mm": vy1, "expected_length_mm": vy1 - vy0},
    }


def default_cut_targets(cfg: CalibrationConfig) -> list[CutTarget]:
    w, h = cfg.cut_target_mm
    return [
        CutTarget("C1", 8, 14, w, h),
        CutTarget("C2", 78, 14, w, h),
        CutTarget("C3", 8, 78, w, h),
        CutTarget("C4", 43, 78, w, h),
        CutTarget("C5", 78, 78, w, h),
        CutTarget("C6", 8, 146, w, h),
        CutTarget("C7", 78, 146, w, h),
    ]


def draw_cut_targets(
    print_draw: ImageDraw.ImageDraw,
    cut_mask_draw: ImageDraw.ImageDraw,
    cfg: CalibrationConfig,
    targets: Iterable[CutTarget],
) -> None:
    red = (214, 38, 38)
    blue = (0, 92, 210)
    green = (0, 160, 80)
    grey = (70, 70, 70)

    for target in targets:
        box_px = cfg.box_px(target.x_mm, target.y_mm, target.width_mm, target.height_mm)
        cut_mask_draw.rectangle(box_px, fill=255)

        rectangle_mm(print_draw, cfg, target.x_mm, target.y_mm, target.width_mm, target.height_mm, red, 3)
        rectangle_mm(print_draw, cfg, target.x_mm + 1, target.y_mm + 1, target.width_mm - 2, target.height_mm - 2, green, 1)
        draw_crosshair(print_draw, cfg, target.center_mm, 4.0, blue, 3)
        draw_centered_text(print_draw, box_px, target.name, 22, grey, bold=True)
        draw_text(
            print_draw,
            (box_px[0] + 5, box_px[3] - 24),
            f"{target.width_mm:g}x{target.height_mm:g}",
            14,
            grey,
        )


def build_preview(print_image: Image.Image, cut_mask: Image.Image) -> Image.Image:
    preview = print_image.convert("RGBA")
    overlay = Image.new("RGBA", print_image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    alpha = cut_mask.load()
    width, height = cut_mask.size

    for y in range(1, height - 1):
        for x in range(1, width - 1):
            if alpha[x, y] == 0:
                continue
            if alpha[x - 1, y] == 0 or alpha[x + 1, y] == 0 or alpha[x, y - 1] == 0 or alpha[x, y + 1] == 0:
                draw.point((x, y), fill=(0, 230, 80, 230))

    preview.alpha_composite(overlay)
    return preview.convert("RGB")


def image_point_to_plot(x_px: int, y_px: int) -> tuple[int, int]:
    return (
        round(PLOT_X_ORIGIN - y_px * PLOT_UNIT_SCALE),
        round(PLOT_Y_ORIGIN + x_px * PLOT_UNIT_SCALE),
    )


def rect_to_plot_path(box_px: tuple[int, int, int, int]) -> list[tuple[int, int]]:
    x0, y0, x1, y1 = box_px
    top_left = image_point_to_plot(x0, y0)
    top_right = image_point_to_plot(x1, y0)
    bottom_right = image_point_to_plot(x1, y1)
    bottom_left = image_point_to_plot(x0, y1)
    return [top_left, top_right, bottom_right, bottom_left, top_left]


def write_native_rect_plt(
    path: Path,
    boxes_px: Iterable[tuple[int, int, int, int]],
    kp: int = 42,
) -> None:
    """Write simple rectangular PixCut PLT paths without libplt_generator.dylib.

    The coordinate transform is derived from official PLT output for rectangular
    masks on the PixCut S1 1200x2100 4x7 sticker raster.
    """

    parts = [f"IN VER0.1.0 KP{kp} "]
    for box in boxes_px:
        points = rect_to_plot_path(box)
        first_x, first_y = points[0]
        parts.append(f"U{first_x},{first_y} ")
        for x, y in points:
            parts.append(f"D{x},{y} ")
    parts.append(f"U{PLOT_TRAILER_X},{PLOT_TRAILER_Y}  @ ")
    path.write_text("".join(parts))


def write_measurement_sheet(
    path: Path,
    cfg: CalibrationConfig,
    scale_bars: dict[str, dict[str, float]],
    targets: list[CutTarget],
) -> None:
    rows = []
    for target in targets:
        expected_x = target.width_mm / 2
        expected_y = target.height_mm / 2
        rows.append(f"| {target.name} | {expected_x:.2f} | {expected_x:.2f} | {expected_y:.2f} | {expected_y:.2f} |  |  |  |  |")

    content = f"""# PixCut S1 Calibration Measurements

Generated for nominal media `{cfg.nominal_media_mm[0]:g} x {cfg.nominal_media_mm[1]:g} mm` on raster `{cfg.canvas_px[0]} x {cfg.canvas_px[1]} px`.

Print scale correction: X `{cfg.print_scale_correction[0]:.7f}`, Y `{cfg.print_scale_correction[1]:.7f}`, origin `{cfg.transform_origin}`.

## Print Area

Record the first fully visible inset line from each physical sheet edge.

| Edge | First fully visible inset mm | Notes |
| --- | ---: | --- |
| Top |  |  |
| Left |  |  |
| Right |  |  |
| Bottom |  |  |

## Print Scale

Measure printed endpoint-to-endpoint distances.

| Mark | Expected mm | Measured mm | Notes |
| --- | ---: | ---: | --- |
| H scale bar | {scale_bars['horizontal']['expected_length_mm']:.2f} |  |  |
| V scale bar | {scale_bars['vertical']['expected_length_mm']:.2f} |  |  |

## Cut Registration

For each cut target, measure from the printed blue center cross to the physical cut edge.
Expected values are half the target size. Positive/negative correction can be derived from left-vs-right and top-vs-bottom asymmetry.

| Target | Expected left mm | Expected right mm | Expected top mm | Expected bottom mm | Measured left | Measured right | Measured top | Measured bottom |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
{chr(10).join(rows)}

## Optional Cut Size

Measure the physical cutout width/height for any targets that are easy to handle.

| Target | Expected width mm | Expected height mm | Measured width | Measured height |
| --- | ---: | ---: | ---: | ---: |
{chr(10).join(f'| {target.name} | {target.width_mm:.2f} | {target.height_mm:.2f} |  |  |' for target in targets)}
"""
    path.write_text(content)


def generate(cfg: CalibrationConfig, output_dir: Path, jpg_quality: int) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)

    print_image = Image.new("RGB", cfg.canvas_px, (255, 255, 255))
    print_draw = ImageDraw.Draw(print_image)
    cut_mask = Image.new("L", cfg.canvas_px, 0)
    cut_mask_draw = ImageDraw.Draw(cut_mask)

    draw_grid(print_draw, cfg)
    draw_edge_printability_marks(print_draw, cfg)
    scale_bars = draw_scale_bars(print_draw, cfg)
    targets = default_cut_targets(cfg)
    draw_cut_targets(print_draw, cut_mask_draw, cfg, targets)
    target_boxes_px = [cfg.box_px(target.x_mm, target.y_mm, target.width_mm, target.height_mm) for target in targets]

    draw_text(
        print_draw,
        (cfg.x_px(24), cfg.y_px(3.1)),
        f"PixCut S1 calibration | nominal {cfg.nominal_media_mm[0]:g}x{cfg.nominal_media_mm[1]:g}mm | canvas {cfg.canvas_px[0]}x{cfg.canvas_px[1]}",
        18,
        (20, 20, 20),
        bold=True,
    )

    cut_source = Image.new("RGBA", cfg.canvas_px, (0, 0, 0, 0))
    cut_source.putalpha(cut_mask)
    preview = build_preview(print_image, cut_mask)

    print_png = output_dir / f"{cfg.basename}_print.png"
    print_jpg = output_dir / f"{cfg.basename}_print.jpg"
    cut_source_png = output_dir / f"{cfg.basename}_cut_source.png"
    cut_mask_png = output_dir / f"{cfg.basename}_cut_mask.png"
    preview_png = output_dir / f"{cfg.basename}_preview.png"
    native_plt = output_dir / f"{cfg.basename}_native_rects.plt"
    metadata_json = output_dir / f"{cfg.basename}_metadata.json"
    measurements_md = output_dir / f"{cfg.basename}_measurements.md"

    print_image.save(print_png)
    print_image.save(print_jpg, quality=jpg_quality, subsampling=0)
    cut_source.save(cut_source_png)
    cut_mask.save(cut_mask_png)
    preview.save(preview_png)
    write_native_rect_plt(native_plt, target_boxes_px)
    write_measurement_sheet(measurements_md, cfg, scale_bars, targets)

    metadata = {
        "config": asdict(cfg),
        "px_per_mm_x": cfg.px_per_mm_x,
        "px_per_mm_y": cfg.px_per_mm_y,
        "scale_bars": scale_bars,
        "cut_targets": [
            {
                **asdict(target),
                "center_mm": target.center_mm,
                "box_px": cfg.box_px(target.x_mm, target.y_mm, target.width_mm, target.height_mm),
            }
            for target in targets
        ],
        "outputs": {
            "print_png": str(print_png),
            "print_jpg": str(print_jpg),
            "cut_source_png": str(cut_source_png),
            "cut_mask_png": str(cut_mask_png),
            "preview_png": str(preview_png),
            "native_rects_plt": str(native_plt),
            "measurements_md": str(measurements_md),
        },
    }
    metadata_json.write_text(json.dumps(metadata, indent=2) + "\n")

    return {
        "print_png": print_png,
        "print_jpg": print_jpg,
        "cut_source_png": cut_source_png,
        "cut_mask_png": cut_mask_png,
        "preview_png": preview_png,
        "native_rects_plt": native_plt,
        "metadata_json": metadata_json,
        "measurements_md": measurements_md,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--basename", default="pixcut_s1_calibration_100x170")
    parser.add_argument("--canvas-px", type=lambda value: parse_pair(value, "x", int), default=(1200, 2100))
    parser.add_argument("--nominal-media-mm", type=lambda value: parse_pair(value, "x", float), default=(100.0, 170.0))
    parser.add_argument("--cut-target-mm", type=lambda value: parse_pair(value, "x", float), default=(14.0, 14.0))
    parser.add_argument("--print-scale-correction", type=lambda value: parse_pair(value, "x", float), default=(1.0, 1.0))
    parser.add_argument("--print-offset-mm", type=lambda value: parse_pair(value, "x", float), default=(0.0, 0.0))
    parser.add_argument("--transform-origin", choices=("origin", "center"), default="origin")
    parser.add_argument("--jpg-quality", type=int, default=95)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    cfg = CalibrationConfig(
        canvas_px=args.canvas_px,
        nominal_media_mm=args.nominal_media_mm,
        basename=args.basename,
        cut_target_mm=args.cut_target_mm,
        horizontal_scale_bar_mm=(10.0, 90.0, 62.0),
        vertical_scale_bar_mm=(62.0, 10.0, 160.0),
        print_scale_correction=args.print_scale_correction,
        print_offset_mm=args.print_offset_mm,
        transform_origin=args.transform_origin,
    )
    outputs = generate(cfg, args.output_dir, args.jpg_quality)
    for path in outputs.values():
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
