---
name: pixcut-s1-usb
description: Generate, preview, trace, and send Liene PixCut S1 sticker print/cut jobs over direct USB from macOS using the reverse-engineered Python tooling.
---

# PixCut S1 USB

Use this skill when the user wants to design stickers, generate cutlines, probe
printer status, print, cut, or continue USB reverse engineering for the Liene
PixCut S1 from macOS.

## Repository

Work from:

```sh
cd /Users/johnboiles/Developer/repos/eastbaymakersclub/pixcut-s1
```

Setup:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
brew install libusb
```

If PyUSB cannot find libusb on Apple Silicon:

```sh
export LIBUSB_PATH=/opt/homebrew/opt/libusb/lib/libusb-1.0.dylib
```

## Known Good Defaults

- USB VID/PID: `302c:3101`.
- Combined print/cut media: `--media-size 4x7 --media-type sticker_4x7`.
- The working 4x7 raster is `1200x2100` px. Treat it as `4 x 7 inches` at
  `300dpi`, which is `101.6 x 177.8mm` and `11.81102362` dots/mm. The sticker
  printing spec is about `100 x 170mm`; that is the advertised printable area
  inside the full raster, not the raster scale.
- The current local printer profile is in `pixcut_s1_profile.json`. For
  dimensionally accurate local layouts, use print scale correction
  `0.9891938513 x 0.9843598616` and safe print margins top `1.3mm`, right
  `1.2mm`, bottom `1.3mm`, left `0.6mm`.
- Leave at least a `2mm` top feed/print guard. The top about `1.5mm` may not
  image if art starts at `y=0`.
- Do not use a `1200x2400` canvas with the current 4x7 path; it printed and
  cut with distorted physical dimensions.
- Combined print/cut jobs require `--job-type 600`.
- Combined upload byte order is PLT bytes followed by image bytes. This is the
  `print-cut` default: `--data-order plot-image`.
- SDK-style data framing uses `10215` byte payload chunks, `1024` byte USB
  sub-writes, and a `20ms` delay after each ACK. These are the script defaults.
- The printer may report physical `media-size: 5009` / `paper_size: 3x3` even
  when 4x7 jobs work.

## Status Checks

Quit `Liene Photo.app` before using direct USB if it may own the interface.

```sh
python pixcut_usb.py probe
python pixcut_usb.py get-prop printer-state printer-sub-state printer-state-alerts
python pixcut_usb.py get-prop device-info media-size paper-size
```

Idle status usually includes:

```json
{"result":["20","2000","::0"]}
```

If the device is not idle, poll again or ask the user to check paper, ribbon,
cover, and LEDs.

## Design And Cutline Workflow

1. Generate print art and a review preview before printing.
2. Use an RGB/JPEG print image for the job.
3. Use a transparent PNG as the cut source; opaque alpha is the cut region.
4. Convert the cut source to PLT with the official app library:

```sh
python pixcut_usb.py make-plt output/cut_source.png \
  -o output/cut.plt --media-type sticker_4x7 --dilation 0 --kp 42
```

Use `--dilation 0` when the cutline should sit at the artwork edge. Use the
tool default `--dilation 1.5` when intentionally expanding around opaque art.
For simple rectangle calibration targets, `generate_calibration_sheet.py`
writes a native `_native_rects.plt` file directly, so the app dylib is not
needed for those rectangle-only cuts. Native rectangles include dylib-style
lead-in/lead-out strokes at the lower-left starting corner. PLT coordinates are
rotated HPGL-like integer plotter units, about `40.52` units/mm for 4x7
`1200x2100` masks; the native rectangle transform is fitted against
`libplt_generator.dylib`.

For custom sheets, inspect the preview for clipping, overlap, edge crowding,
and mismatched print/cut geometry before sending the job. Ask for user review
before printing when the user requested review or when the design changed.

## Calibration Workflow

Use this before precision layouts or when a user wants printable area, scale, or
print/cut registration data.

```sh
python generate_calibration_sheet.py
python pixcut_usb.py print-cut output/calibration/pixcut_s1_calibration_4x7_300dpi_print.jpg \
  output/calibration/pixcut_s1_calibration_4x7_300dpi_native_rects.plt \
  --media-size 4x7 --media-type sticker_4x7 \
  --execute --wait-after-send 360 --poll-interval 5
```

Review `output/calibration/pixcut_s1_calibration_4x7_300dpi_preview.png` before
printing. After printing, use the generated measurements Markdown file to
record edge/inset visibility, H/V print scale, and cut target offsets. For cut
targets, measure from the printed blue center cross to each physical cut edge;
expected distance is half the cut target size.

`generate_calibration_sheet.py` defaults to the current local print scale
correction from `pixcut_s1_profile.json`. To override it explicitly:

```sh
python generate_calibration_sheet.py \
  --basename pixcut_s1_calibration_local_residual \
  --print-scale-correction 0.9891938513x0.9843598616
python pixcut_usb.py print-cut output/calibration/pixcut_s1_calibration_local_residual_print.jpg \
  output/calibration/pixcut_s1_calibration_local_residual_native_rects.plt \
  --media-size 4x7 --media-type sticker_4x7 \
  --execute --wait-after-send 360 --poll-interval 5
```

Residual scale correction is applied around the center of the 4x7 raster. The
latest X factor came from job `53`, where `H80` measured `80.35mm`; `V150`
measured on target, so the previous Y factor stayed unchanged. The older
`0.9778755653x0.9411764706` correction belonged to the obsolete
`100x170mm`-as-full-raster model and should not be used.

## Print And Cut

Dry-run first:

```sh
python pixcut_usb.py print-cut output/print.jpg output/cut.plt \
  --media-size 4x7 --media-type sticker_4x7
```

Send only after review:

```sh
python pixcut_usb.py print-cut output/print.jpg output/cut.plt \
  --media-size 4x7 --media-type sticker_4x7 \
  --execute --wait-after-send 360 --poll-interval 5
```

Watch stdout/stderr for `job-state: 9` completion, `cut-contours`, and
`cutting-progress`. If a job uploads but never starts, re-check `job-type=600`,
PLT+image order, canvas size, and whether the Liene app is open.

## Example Generator

For a known working local example:

```sh
python design_ebmc_stickers.py
python pixcut_usb.py make-plt output/ebmc_sticker_sheet_4x7_cut_source.png \
  -o output/ebmc_sticker_sheet_4x7.plt --media-type sticker_4x7 --dilation 0 --kp 42
```

Review `output/ebmc_sticker_sheet_4x7_preview.png` before printing
`output/ebmc_sticker_sheet_4x7_print.jpg`.

## Tracing

Use tracing only when reverse engineering official app behavior. Traces may
contain account or local metadata, so do not paste full logs into final answers.

```sh
.frida-venv/bin/python trace_liene_usb.py --spawn \
  --log-file output/frida_trace_$(date +%Y%m%d_%H%M%S).jsonl
```

Start a print in `Liene Photo.app`, then stop the tracer after the print starts
or fails. To attach to an already-open app:

```sh
.frida-venv/bin/python trace_liene_usb.py --attach "Liene Photo"
```
