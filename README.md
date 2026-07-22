# Liene PixCut S1 reverse-engineering notes

This folder contains Python utilities for the Liene PixCut S1 sticker printer/cutter over USB and BLE.

## What is known

- USB device: `302c:3101`, manufacturer `Liene`, product `PixCut S1`.
- Model reported over the protocol: `DHP700`.
- USB printer device ID reports `CMD:DC_RASTER` and `CID:HANNTODHP51X`.
- USB interfaces:
  - interface `0`: printer class, endpoints `0x01` out and `0x81` in
  - interface `2`: command channel, endpoints `0x06` out and `0x86` in
  - interface `3`: job data channel, endpoints `0x04` out and `0x84` in
- JSON command frame: `b"cmd json\n" + compact_json`.
- Data frame: `b"cmd data EXTLEN=" + decimal_chunk_len + b"\n" + little_endian_u32_job_id + chunk`.
- The official SDK uses `10215` bytes of job payload per data frame. It wraps each chunk in the data frame above, splits the framed packet into `1024` byte USB bulk writes, waits for an ACK on `0x84`, then sleeps for about `20ms` before the next frame.
- BLE exposes multiple writable GATT pipes. The confirmed direct BLE command path is Android-style Hannto framing over `0000ff00` service, write characteristic `0000ff02`, status notify `0000ff03`, and data notify `0000ff01`. The Microchip/ISSC MLDP service also accepts the same framed status request.
- The Android app also writes `A_OUT_<phone-mac>` to `0000fe95` / `00000013` as a BLE handoff before using Bluetooth Classic SPP (`00001101-0000-1000-8000-00805f9b34fb`). Direct BLE framing means WebBluetooth is plausible for the GATT path, but the handoff/SPP path itself is not WebBluetooth-compatible.
- A direct BLE print/cut smoke job has been validated on the local PixCut S1. The job used `combo-job`, `job-type=600`, PLT bytes followed by image bytes, and `156` job bytes per BLE Hannto frame. The printer assigned job id `66`, moved through active status (`40/3015`, `40/3021`, `40/3030`), then returned to idle (`20/2000/::0`) after printing and cutting. A later partial `get-job-info` response showed `job-state=9`, `job-sub-state=9000`, and `job-state-reason=90001`.
- A browser-originated Album Cards print/cut job failed with `event.rpt_err` code `-8013` / `job timeout` when job data frames were written without flow control. Waiting for each `ff00-status` notification payload `0101` as a per-write ACK fixed that Web Bluetooth path; the LED went pink during transfer and the sheet printed/cut successfully.
- A later successful browser print/cut produced the physical output but one post-upload `get-prop` status poll timed out. Treat status-poll loss after any active printer state as nonfatal (`status_poll_timeout_after_active`); continue to fail immediately on actual `event.rpt_err` device errors.

The read-only USB probe, combined USB print/cut flow, small BLE JSON request/response flow, and a small BLE print/cut flow have been tested against the local printer. USB and BLE job commands require `--execute` before they can start print or cutter movement.

## Setup

```bash
cd /Users/johnboiles/Developer/repos/eastbaymakersclub/pixcut-s1
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
brew install libusb
```

On Apple Silicon Macs, if PyUSB cannot find libusb:

```bash
export LIBUSB_PATH=/opt/homebrew/opt/libusb/lib/libusb-1.0.dylib
```

## Probe

```bash
python pixcut_usb.py probe
python pixcut_usb.py get-prop printer-state printer-sub-state printer-state-alerts
python pixcut_usb.py get-prop device-info
python pixcut_usb.py get-prop media-size paper-size
```

BLE status over the confirmed `ff00` GATT path:

```bash
python pixcut_ble.py services --address 4133B034-D252-CEE5-A0DC-150BCE8D4DBF
python pixcut_ble.py get-prop --address 4133B034-D252-CEE5-A0DC-150BCE8D4DBF \
  printer-state printer-sub-state printer-state-alerts
python pixcut_ble.py probe-hannto-json --address 4133B034-D252-CEE5-A0DC-150BCE8D4DBF
```

Known good local response:

```json
{
  "result": [
    "20",
    "2000",
    "::0"
  ]
}
```

Device info returned firmware `1.0.15_0054`, model `DHP700`, MAC `F0:13:C1:62:F3:9A`.

BLE note: small JSON responses decode cleanly. Larger responses, such as `device-info` and `get-job-info`, currently arrive on macOS as a single 182-byte notification fragment without the continuation needed to complete the Hannto frame. Use short status polling (`printer-state`, `printer-sub-state`, `printer-state-alerts`) for job progress until response fragmentation or larger MTU handling is understood.

Known fault/status observations:

- Out of paper or missing media during device job `56` reported printer state `60`, sub-state `3015`, alert `::5401`, with job state `5`, job reason `50000`, and job sub-state `5000`. After loading new sticker paper, the printer resumed the same uploaded job without resending it. When this alert appears, load media and keep polling before creating a duplicate job.

The official app can claim the USB interface. If PyUSB reports access denied, quit `Liene Photo.app` before probing.

## Cutter PLT generation

The official app includes `libplt_generator.dylib`. The tool can call it if the Liene Photo app is installed under `/Applications`, the app DMG is mounted, or `PIXCUT_APP_FRAMEWORKS` points to `Liene Photo.app/Contents/Frameworks`.

Transparent PNGs work well as cutter masks: transparent pixels become background and opaque pixels become the cut region.

```bash
python pixcut_usb.py make-plt sticker.png -o cut.plt --media-type sticker_4x7
```

For 4x7 sticker paper, the official app's default preview settings generate PLT with `dilation=1.5` and `KP42`. Those are the tool defaults now. The earlier `dilation=0` and `KP1` output produced valid-looking paths but did not match the successful official app job.

If the requested cutline should sit exactly on the alpha edge of a design, use `--dilation 0`.

For simple rectangular calibration targets, `generate_calibration_sheet.py` also writes a native PixCut PLT file directly. That path does not require `libplt_generator.dylib`; it is currently only intended for axis-aligned rectangles on the validated `1200x2100` 4x7 sticker raster. Native rectangles include dylib-style lead-in/lead-out strokes at the lower-left starting corner so the blade enters and exits outside the meaningful cut edge.

PixCut PLT files use HPGL-like integer plotter units, not raster pixels. For `1200x2100` 4x7 masks, the dylib maps raster pixels into rotated PLT coordinates approximately as:

```text
plot_x = 7153.0320854 - image_y * 3.4308105
plot_y =  -24.9285172 + image_x * 3.4315980
```

At the 300dpi raster scale this is about `40.52` PLT units/mm, or `0.0247mm` per PLT unit. The native rectangle writer is fitted against `libplt_generator.dylib` using that transform.

The validated C signatures are:

```c
char *plt_generate_from_file(const char *image_path,
                             const char *config_json,
                             double dilation,
                             double angle,
                             int media_type,
                             double kp);
int plt_get_default_config(char *buffer, int buffer_size, int *bytes_written);
void plt_free_string(char *ptr);
```

## Example sticker design workflow

`design_ebmc_stickers.py` is a reusable example of generating print art, a transparent cut source, and a preview from PIL. It writes generated files under ignored `output/`.

```bash
python design_ebmc_stickers.py
python pixcut_usb.py make-plt output/ebmc_sticker_sheet_4x7_cut_source.png \
  -o output/ebmc_sticker_sheet_4x7.plt --media-type sticker_4x7 --dilation 0 --kp 42
python pixcut_usb.py print-cut output/ebmc_sticker_sheet_4x7_print.jpg \
  output/ebmc_sticker_sheet_4x7.plt --media-size 4x7 --media-type sticker_4x7 \
  --execute --wait-after-send 360 --poll-interval 5
```

## Calibration workflow

The working raster for 4x7 sticker jobs is `1200x2100`. At the advertised `300dpi`, that is exactly `4 x 7 inches` / `101.6 x 177.8mm`, or `11.81102362` dots/mm. `generate_calibration_sheet.py` uses that 300dpi full-raster coordinate system by default. The sticker-mode printable-area spec is still `3.9 x 6.7 inches` / about `100 x 170mm`, so treat that as the advertised printable region inside the 4x7 raster, not as the raster's physical scale.

The current local printer/media profile is recorded in `pixcut_s1_profile.json`. For dimensionally accurate local layouts, use:

- print scale correction: `0.9891938513 x 0.9843598616`
- safe print margins: top `1.3mm`, right `1.2mm`, bottom `1.3mm`, left `0.6mm`

`generate_calibration_sheet.py` defaults to that current local print scale correction. Override `--print-scale-correction` only when testing a new profile.

Generate a repeatable calibration print/cut sheet:

```bash
python generate_calibration_sheet.py
python pixcut_usb.py print-cut output/calibration/pixcut_s1_calibration_4x7_300dpi_print.jpg \
  output/calibration/pixcut_s1_calibration_4x7_300dpi_native_rects.plt \
  --media-size 4x7 --media-type sticker_4x7 \
  --execute --wait-after-send 360 --poll-interval 5
```

The generator writes:

- print PNG/JPEG
- transparent cut-source PNG
- cut mask PNG
- preview PNG with the cutlines overlaid
- native rectangle PLT for the cut targets, including lower-left lead-in/lead-out strokes
- metadata JSON
- a measurement worksheet Markdown file

The calibration sheet includes edge/inset printability rectangles, H/V print scale bars, and seven rectangular cut registration targets. For each cut target, measure from the printed blue center cross to the physical cut edge; the expected distance is half the target size. Earlier direct USB calibration jobs verified the seven-target workflow: job `51` printed/cut successfully, and job `52` verified generated native rectangle PLT output.

To regenerate using an explicit residual correction:

```bash
python generate_calibration_sheet.py \
  --basename pixcut_s1_calibration_local_residual \
  --print-scale-correction 0.9891938513x0.9843598616
```

Residual scale correction is applied around the center of the 4x7 raster. The latest X factor came from job `53`, where `H80` measured `80.35mm`; `V150` measured on target, so the previous Y factor stayed unchanged. The older `0.9778755653 x 0.9411764706` correction should not be used because most of that correction came from the wrong `100x170mm`-as-full-raster model.

## Job sending

Dry run:

```bash
python pixcut_usb.py cut cut.plt
python pixcut_usb.py print sticker.jpg --media-size 4x7 --media-type sticker_4x7
python pixcut_usb.py print-cut sticker.jpg cut.plt --media-size 4x7 --media-type sticker_4x7
```

Actually send to the printer/cutter:

```bash
python pixcut_usb.py cut cut.plt --media-size 3x3 --media-type sticker_3x3 --execute
```

For a combined print-and-cut job, the current working path is SDK-style upload framing, `job-type=600`, and PLT bytes before JPEG bytes. Those are now the `print-cut` defaults and match both the SDK wrapper disassembly and the successful Liene app jobs:

```bash
python pixcut_usb.py print-cut sticker.jpg cut.plt --media-size 4x7 --media-type sticker_4x7 --execute
```

BLE dry run:

```bash
python pixcut_ble.py print-cut sticker.jpg cut.plt \
  --address 4133B034-D252-CEE5-A0DC-150BCE8D4DBF \
  --media-size 4x7 --media-type sticker_4x7
```

BLE send:

```bash
python pixcut_ble.py print-cut sticker.jpg cut.plt \
  --address 4133B034-D252-CEE5-A0DC-150BCE8D4DBF \
  --media-size 4x7 --media-type sticker_4x7 \
  --execute --wait-after-send 360 --poll-interval 5
```

The BLE job path uses the same `combo-job` JSON and PLT-then-image upload order as USB, but wraps JSON and job bytes in Android-style Hannto frames instead of USB `cmd json` / `cmd data` frames. The default BLE upload chunk is `156` job bytes per frame so each full Hannto frame stays within the observed `182` byte GATT write-without-response size. Increase `--ble-data-chunk-size` only after validating larger fragmented writes. The `ff00-status` notification payload `0101` acts as a per-write ACK; Web Bluetooth uploads should wait for it after every job-data frame rather than relying on a fixed delay. `--wait-after-send` polls only short status fields because larger BLE JSON responses currently truncate on macOS; a dropped status response after the printer has reported an active state is not by itself a failed job.

## Web Bluetooth browser client

`web-bluetooth/` contains a dependency-free local browser client for the same
confirmed BLE path. It implements:

- Web Bluetooth device selection by PixCut name prefix
- GATT connection to service `0000ff00`
- JSON commands over write `0000ff02` and data notify `0000ff01`
- short status polling with `printer-state`, `printer-sub-state`, and `printer-state-alerts`
- guarded 4x7 `print-cut` uploads using `combo-job`, `job-type=600`, PLT bytes followed by image bytes, `156` job bytes per BLE frame, and `ff00-status` `0101` ACK pacing
- direct handling of `event.rpt_err` notifications, including `-8013` / `job timeout`

Run it from localhost:

```bash
python3 -m http.server 8765 --directory web-bluetooth
open http://localhost:8765/
```

The browser still has to show its Bluetooth device picker, so the hardware
connect/send path is a manual browser step. Automated coverage currently tests
the protocol code and static browser load:

```bash
node --test test/pixcut-web-bluetooth.test.mjs
node --check web-bluetooth/pixcut-web-bluetooth.mjs
node --check web-bluetooth/app.mjs
```

Known media values from the app assets:

- `3x3`: media size `5009`; currently reported by the local printer
- `4x6`: media size `5012`; photo media type `2010`
- `4x7`: media size `5013`; sticker-back media type `2030`

The `job-type` field is not fully mapped yet. Direct USB combined print/cut jobs require `600`; earlier `job-type=0` jobs uploaded but did not start. Print-only and cut-only commands still default to `0` until those paths are verified.

Successful official app comparison jobs:

- Official app version: `Liene Photo.app` `2.1.012`.
- Printer status moved `Printing` -> `Cutting` -> `completed`.
- App task type in logs: `HtJobType.generalPrintCutTask`.
- App rendered `1200x2100` print data to JPEG before sending.
- App generated a separate transparent `1200x2100` cut-mask PNG and converted it to `cut.plt`.
- Re-running `pixcut_usb.py make-plt` on that transparent PNG with `--dilation 1.5 --kp 42` exactly reproduced the official `cut.plt`.
- Standalone `cut-job` uses `plot-file-size`, but the nested `cut-job` inside `combo-job` uses plain `file-size`. This matches `RawProtocolManager::buildCreatePrintCutJobCommand` in the official SDK.
- Native SDK disassembly and live SDK logging show `10215` byte data payloads, `1024` byte USB sub-writes, and a `20ms` delay after each ACK.
- Native SDK disassembly confirms the combined `combo-job` JSON is ordered as nested `print-job` then nested `cut-job`. The upload buffer is ordered separately: the wrapper copies the plot pointer/`plot_size` first, then the image pointer/`image_size`.
- A traced successful Liene app job confirmed the data order at the raw USB layer: the later data packets and the final short packet match `cut.plt + jpg`, not `jpg + cut.plt`.

Captured successful-job artifacts are in:

```text
pixcut-s1/output/official_app_job_21/
pixcut-s1/output/official_app_job_31/
pixcut-s1/output/official_app_job_42_stina/
pixcut-s1/output/official_app_job_46_stina/
```

`official_app_job_31` came from a successful user-created 4x7 sheet in `Liene Photo.app`: device job id `31`, JPEG `731496` bytes, `cut.plt` `9804` bytes, total combo payload `741300` bytes. The app log took about nine seconds from combo-job creation to assigned device job id, which is consistent with SDK-style chunking and 1024-byte USB sub-writes.

`official_app_job_42_stina` came from a successful traced 4x7 sheet in `Liene Photo.app`: device job id `42`, JPEG `638131` bytes, `cut.plt` `8643` bytes, total combo payload `646774` bytes. The final official data packet had payload length `3229`, exactly what `10215` byte chunks produce for that total.

`official_app_job_46_stina` came from a successful 4x7 sheet in `Liene Photo.app`: device job id `46`, JPEG `654755` bytes, `cut.plt` `8173` bytes, total combo payload `662928` bytes. Resending the same artifacts directly over USB with `job-type=600` succeeded as device job id `47`: the printer produced one page, then reported `cut-contours: 9` with `cutting-progress` advancing from `1` through `9`, and finally returned to idle with `job-state: 9`.

4x7 layout calibration:

- Use a `1200x2100` canvas for the working `4x7` media path. Model it as `4 x 7 inches` at `300dpi`, which is `101.6 x 177.8mm` and `11.81102362` dots/mm. The advertised sticker printable area is about `100x170mm` inside that raster. A `1200x2400` test printed/cut, but the output was distorted: the print measured about `41mm x 36mm` while cuts measured about `36mm x 36mm`.
- The top approximately `1.5mm` of the sheet may not image if artwork starts at physical `y=0`. Future layouts should reserve at least a `2mm` top feed/print guard, about `24px` at `300dpi`, and start the first cut/art row at `y>=24px`.

To resend those exact known-good official artifacts through the Python sender:

```bash
python pixcut_usb.py print-cut output/official_app_job_31/1779678228882.jpg output/official_app_job_31/cut.plt --media-size 4x7 --media-type sticker_4x7 --execute
python pixcut_usb.py print-cut output/official_app_job_42_stina/1779900496812.jpg output/official_app_job_42_stina/cut.plt --media-size 4x7 --media-type sticker_4x7 --execute
python pixcut_usb.py print-cut output/official_app_job_46_stina/1779916716262.jpg output/official_app_job_46_stina/cut.plt --media-size 4x7 --media-type sticker_4x7 --execute
```

Earlier 2026-05-27 direct-USB hardware results:

- `PLT + JPG` official artifacts became device jobs `32`, `33`, and `35`, but each went straight to `job-state: 7`, `job-sub-state: 7000`, `job-state-reason: 70000`. This conflicts with the later successful app trace, so there is still at least one missing condition or the printer was in a different state.
- `JPG + PLT` official artifacts became device jobs `36`, `37`, `38`, and `39`, reached printer state `40`, sub-state `3016`, `transfer-status: 3`, and the full transfer size, then timed out with `event.rpt_err` code `-8013` / `job timeout`.
- `resume-printer` sent during that active/transfer-complete state is accepted with `error-code: 0` but does not start motion.
- `print-function-enable` is writable only as numeric JSON. Setting it to `1` before job creation makes the next `combo-job` fail immediately with error `8001`; setting it to `1` after transfer-complete is accepted and changes the reported property, but the job still times out.
- The printer reports physical media as `media-size: 5009`, `paper_size: 3x3`, and `print-function-enable: 0` even when creating a 4x7 job. Attempts to set `paper-size`, `paper_size`, or `media-size` over `set-prop` return error `6002` or are ignored when combined with writable properties.
- A 3x3 EBMC job matching reported `media-size: 5009` also reached transfer-complete and timed out, so the failure is not just a 4x7-vs-3x3 metadata mismatch.
- Native SDK disassembly confirms the `combo-job` JSON field offsets. It also shows the exported `device_sdk_create_print_cut_job` combines data as `arg3` bytes first, then `arg2` bytes. Passing normal `(image, plot)` pointers therefore produces `PLT + JPG`, which the later successful app trace confirmed.

## Official app tracing

A Frida tracing environment is installed under `.frida-venv` for the next successful Liene app print. The tracer hooks `device_sdk_create_print_cut_job`, `DeviceSDK::sendJobData`, and the lower-level USB transfer methods so we can capture actual argument order, combined payload prefix, and raw `cmd json` / `cmd data` frames from the app.

```bash
cd /Users/johnboiles/Developer/repos/eastbaymakersclub/pixcut-s1
.frida-venv/bin/python trace_liene_usb.py --spawn --log-file output/frida_trace_$(date +%Y%m%d_%H%M%S).jsonl
```

Once the app opens, start a print from `Liene Photo.app` and leave this tracer running until the print either starts or fails. If the app is already open, use:

```bash
.frida-venv/bin/python trace_liene_usb.py --attach "Liene Photo"
```

## Sources inspected

- Liene PixCut S1 help/spec page: <https://www.liene-life.com/pages/pixcut-help-center>
- Liene app download page: <https://www.liene-life.com/pages/pixcut-s1-app-download>
- Liene PC app download page: <https://www.liene-life.com/pages/pcdownload?platform=mac>
- Official macOS app `Liene Photo.app` version `2.1.012`, native libraries `libdevice_sdk.dylib` and `libplt_generator.dylib`.
