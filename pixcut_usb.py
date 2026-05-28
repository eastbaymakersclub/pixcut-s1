#!/usr/bin/env python3
"""USB tooling for the Liene PixCut S1.

This is based on the official desktop app's native SDK behavior:
command endpoint 0x06/0x86, data endpoint 0x04/0x84, JSON framed as
``cmd json\n`` + compact JSON.
"""

from __future__ import annotations

import argparse
import ctypes
import glob
import json
import os
from pathlib import Path
import struct
import sys
import time
from typing import Any

try:
    import usb.core
    import usb.util
except ImportError as exc:  # pragma: no cover - only hit before setup.
    raise SystemExit("Install dependencies first: python -m pip install -r requirements.txt") from exc


DEFAULT_VID = 0x302C
DEFAULT_PID = 0x3101

PRINTER_INTERFACE = 0
COMMAND_INTERFACE = 2
DATA_INTERFACE = 3

COMMAND_OUT = 0x06
COMMAND_IN = 0x86
DATA_OUT = 0x04
DATA_IN = 0x84

CMD_JSON_HEADER = b"cmd json\n"
CMD_DATA_HEADER = b"cmd data EXTLEN="
SDK_DATA_CHUNK_SIZE = 10215
SDK_USB_WRITE_SIZE = 1024
SDK_ACK_DELAY_SECONDS = 0.02
DEFAULT_JOB_TYPE = 0
DEFAULT_PRINT_CUT_JOB_TYPE = 600

STATUS_PROPS = ["printer-state", "printer-sub-state", "printer-state-alerts"]
JOB_POLL_PROPS = [
    "printer-state",
    "printer-sub-state",
    "printer-state-alerts",
    "job-state",
    "job-state-reasons",
    "transfer-status",
    "transfer-size",
    "media-size",
    "paper-size",
]

MEDIA_SIZES = {
    "3x3": 5009,
    "4x6": 5012,
    "4x7": 5013,
}

MEDIA_TYPES = {
    "photo": 2010,
    "sticker_3x3": 2020,
    "sticker_4x7": 2030,
}


class PixcutError(RuntimeError):
    pass


class PixcutUSB:
    def __init__(self, vid: int = DEFAULT_VID, pid: int = DEFAULT_PID, timeout_ms: int = 5000) -> None:
        self.vid = vid
        self.pid = pid
        self.timeout_ms = timeout_ms
        self.dev: usb.core.Device | None = None
        self.next_id = int(time.monotonic() * 1000) % 900000 + 1000

    def __enter__(self) -> "PixcutUSB":
        self.dev = usb.core.find(idVendor=self.vid, idProduct=self.pid)
        if self.dev is None:
            raise PixcutError(f"PixCut S1 USB device not found at {self.vid:04x}:{self.pid:04x}")

        try:
            self.dev.get_active_configuration()
        except usb.core.USBError:
            self.dev.set_configuration()

        for intf in (COMMAND_INTERFACE, DATA_INTERFACE):
            try:
                if self.dev.is_kernel_driver_active(intf):
                    self.dev.detach_kernel_driver(intf)
            except (NotImplementedError, usb.core.USBError):
                pass
            usb.util.claim_interface(self.dev, intf)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.dev is None:
            return
        for intf in (DATA_INTERFACE, COMMAND_INTERFACE):
            try:
                usb.util.release_interface(self.dev, intf)
            except usb.core.USBError:
                pass
        usb.util.dispose_resources(self.dev)
        self.dev = None

    @property
    def device(self) -> usb.core.Device:
        if self.dev is None:
            raise PixcutError("USB device is not open")
        return self.dev

    def printer_device_id(self) -> str:
        """Read the IEEE-1284 printer device ID from interface 0."""
        data = self.device.ctrl_transfer(0xA1, 0, 0, PRINTER_INTERFACE, 1024, timeout=1000)
        raw = bytes(data)
        if len(raw) >= 2:
            declared_len = int.from_bytes(raw[:2], "big")
            payload = raw[2:declared_len] if declared_len <= len(raw) else raw[2:]
        else:
            payload = raw
        return payload.rstrip(b"\x00").decode("ascii", "replace")

    def send_json(self, method: str, params: Any | None = None) -> Any:
        obj: dict[str, Any] = {"method": method}
        if params is not None:
            obj["params"] = params
        obj["id"] = self.next_id
        self.next_id += 1
        return self.send_json_obj(obj)

    def send_json_obj(self, obj: dict[str, Any]) -> Any:
        payload = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        packet = CMD_JSON_HEADER + payload
        self.device.write(COMMAND_OUT, packet, timeout=self.timeout_ms)
        request_id = obj.get("id")
        deadline = time.monotonic() + self.timeout_ms / 1000
        last_response: Any = None
        while True:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                detail = f"; last response: {last_response}" if last_response is not None else ""
                raise PixcutError(f"Timed out waiting for response id {request_id}{detail}")
            response = bytes(self.device.read(COMMAND_IN, 65536, timeout=min(self.timeout_ms, remaining_ms)))
            if response.startswith(CMD_JSON_HEADER):
                response = response[len(CMD_JSON_HEADER) :]
            text = response.decode("utf-8", "replace").strip()
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                return {"raw": text}

            last_response = decoded
            if isinstance(decoded, dict) and str(decoded.get("method", "")).startswith("event."):
                print(json.dumps({"event": decoded}, sort_keys=True), file=sys.stderr)
                continue
            if request_id is not None and isinstance(decoded, dict) and decoded.get("id") != request_id:
                print(f"skipping stale response while waiting for id {request_id}: {decoded}", file=sys.stderr)
                continue

            error_code = find_error_code(decoded)
            if error_code is not None:
                raise PixcutError(f"Device returned error-code {error_code}: {decoded}")
            return decoded

    def status(self) -> Any:
        return self.send_json("get-prop", STATUS_PROPS)

    def device_info(self) -> Any:
        return self.send_json("get-prop", ["device-info"])

    def get_props(self, props: list[str]) -> Any:
        return self.send_json("get-prop", props)

    def set_prop(self, key: str, value: str) -> Any:
        return self.send_json("set-prop", {key: value})

    def set_prop_obj(self, values: dict[str, Any]) -> Any:
        return self.send_json("set-prop", values)

    def resume(self) -> Any:
        return self.send_json("resume-printer", {})

    def get_job_info(self, job_id: int) -> Any:
        return self.send_json("get-job-info", {"job-id": job_id})

    def create_print_job(
        self,
        image_len: int,
        channel: int,
        copies: int,
        media_size: int,
        media_type: int,
        job_type: int,
    ) -> tuple[int, Any]:
        response = self.send_json(
            "print-job",
            {
                "channel": channel,
                "copies": copies,
                "media-size": media_size,
                "media-type": media_type,
                "job-type": job_type,
                "file-size": image_len,
            },
        )
        return extract_job_id(response), response

    def create_cut_job(
        self,
        plot_len: int,
        channel: int,
        copies: int,
        media_size: int,
        media_type: int,
        job_type: int,
    ) -> tuple[int, Any]:
        response = self.send_json(
            "cut-job",
            {
                "channel": channel,
                "copies": copies,
                "media-size": media_size,
                "media-type": media_type,
                "job-type": job_type,
                "plot-file-size": plot_len,
            },
        )
        return extract_job_id(response), response

    def create_print_cut_job(
        self,
        image_len: int,
        plot_len: int,
        channel: int,
        copies: int,
        media_size: int,
        media_type: int,
        job_type: int,
    ) -> tuple[int, Any]:
        obj = {
            "method": "combo-job",
            "params": [
                {
                    "method": "print-job",
                    "params": {
                        "channel": channel,
                        "copies": copies,
                        "file-size": image_len,
                        "media-size": media_size,
                        "media-type": media_type,
                        "job-type": job_type,
                    },
                },
                {
                    "method": "cut-job",
                    "params": {
                        "file-size": plot_len,
                        "job-type": job_type,
                    },
                },
            ],
            "id": self.next_id,
        }
        self.next_id += 1
        response = self.send_json_obj(obj)
        return extract_job_id(response), response

    def send_job_data(
        self,
        job_id: int,
        data: bytes,
        chunk_size: int = SDK_DATA_CHUNK_SIZE,
        chunk_delay: float = SDK_ACK_DELAY_SECONDS,
        usb_write_size: int = SDK_USB_WRITE_SIZE,
    ) -> None:
        if chunk_size <= 0:
            raise PixcutError(f"chunk_size must be positive, got {chunk_size}")
        if usb_write_size <= 0:
            raise PixcutError(f"usb_write_size must be positive, got {usb_write_size}")

        total = len(data)
        next_report = 0
        for offset in range(0, total, chunk_size):
            chunk = data[offset : offset + chunk_size]
            packet = (
                CMD_DATA_HEADER
                + str(len(chunk)).encode("ascii")
                + b"\n"
                + struct.pack("<I", job_id)
                + chunk
            )
            for packet_offset in range(0, len(packet), usb_write_size):
                self.device.write(DATA_OUT, packet[packet_offset : packet_offset + usb_write_size], timeout=10000)
            ack = bytes(self.device.read(DATA_IN, 4096, timeout=10000))
            if not ack:
                raise PixcutError(f"No ACK for job {job_id} at offset {offset}")
            done = min(offset + len(chunk), total)
            if done >= next_report or done == total:
                print(f"sent {done}/{total} bytes", file=sys.stderr)
                next_report = done + 16384
            if chunk_delay > 0:
                time.sleep(chunk_delay)


def find_error_code(response: Any) -> int | None:
    if isinstance(response, dict):
        for key in ("error-code", "error_code"):
            if key in response:
                try:
                    code = int(response[key])
                except (TypeError, ValueError):
                    continue
                if code != 0:
                    return code
        for child in response.values():
            found = find_error_code(child)
            if found is not None:
                return found
    elif isinstance(response, list):
        for child in response:
            found = find_error_code(child)
            if found is not None:
                return found
    return None


def extract_job_id(response: Any) -> int:
    error_code = find_error_code(response)
    if error_code is not None:
        raise PixcutError(f"Device returned error-code {error_code}: {response}")

    def walk(value: Any) -> int | None:
        if isinstance(value, dict):
            for key in ("job-id", "job_id"):
                if key in value:
                    return int(value[key])
            for child in value.values():
                found = walk(child)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = walk(child)
                if found is not None:
                    return found
        return None

    job_id = walk(response)
    if job_id is None:
        raise PixcutError(f"Could not find job-id in response: {response}")
    return job_id


def resolve_int(value: str, table: dict[str, int], label: str) -> int:
    if value in table:
        return table[value]
    try:
        return int(value, 0)
    except ValueError as exc:
        names = ", ".join(sorted(table))
        raise argparse.ArgumentTypeError(f"Unknown {label} {value!r}; use one of {names} or an integer") from exc


def default_framework_dirs() -> list[Path]:
    dirs: list[Path] = []
    env_dir = os.environ.get("PIXCUT_APP_FRAMEWORKS")
    if env_dir:
        dirs.append(Path(env_dir))
    dirs.append(Path("/Applications/Liene Photo.app/Contents/Frameworks"))
    dirs.extend(Path(p) for p in glob.glob("/Volumes/Liene*/Liene Photo.app/Contents/Frameworks"))
    return dirs


def find_framework_dir(user_path: str | None) -> Path:
    candidates = [Path(user_path)] if user_path else default_framework_dirs()
    for path in candidates:
        if (path / "libplt_generator.dylib").exists() and (path / "liblogger.dylib").exists():
            return path
    searched = "\n".join(str(p) for p in candidates)
    raise PixcutError(f"Could not find Liene app frameworks. Searched:\n{searched}")


def generate_plt(
    image_path: Path,
    output_path: Path,
    frameworks: str | None,
    config_path: Path | None,
    dilation: float,
    angle: float,
    media_type: int,
    kp: float,
) -> None:
    framework_dir = find_framework_dir(frameworks)
    ctypes.CDLL(str(framework_dir / "liblogger.dylib"), mode=ctypes.RTLD_GLOBAL)
    lib = ctypes.CDLL(str(framework_dir / "libplt_generator.dylib"), mode=ctypes.RTLD_GLOBAL)

    lib.plt_generate_from_file.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_double,
        ctypes.c_double,
        ctypes.c_int,
        ctypes.c_double,
    ]
    lib.plt_generate_from_file.restype = ctypes.c_void_p
    lib.plt_free_string.argtypes = [ctypes.c_void_p]
    lib.plt_free_string.restype = None

    config = config_path.read_bytes() if config_path else None
    ptr = lib.plt_generate_from_file(
        str(image_path).encode("utf-8"),
        config,
        ctypes.c_double(dilation),
        ctypes.c_double(angle),
        ctypes.c_int(media_type),
        ctypes.c_double(kp),
    )
    if not ptr:
        raise PixcutError("PLT generation failed")
    try:
        data = ctypes.string_at(ptr)
    finally:
        lib.plt_free_string(ptr)
    output_path.write_bytes(data)
    print(f"wrote {len(data)} bytes to {output_path}")


def print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def run_probe(args: argparse.Namespace) -> None:
    with PixcutUSB(args.vid, args.pid, args.timeout_ms) as pixcut:
        out: dict[str, Any] = {}
        try:
            out["printer_device_id"] = pixcut.printer_device_id()
        except Exception as exc:
            out["printer_device_id_error"] = str(exc)
        out["status"] = pixcut.status()
        out["device_info"] = pixcut.device_info()
        print_json(out)


def run_get_prop(args: argparse.Namespace) -> None:
    with PixcutUSB(args.vid, args.pid, args.timeout_ms) as pixcut:
        print_json(pixcut.get_props(args.props))


def run_set_prop(args: argparse.Namespace) -> None:
    with PixcutUSB(args.vid, args.pid, args.timeout_ms) as pixcut:
        value: Any = args.value
        if args.json_value:
            value = json.loads(args.value)
        print_json(pixcut.set_prop_obj({args.key: value}))


def run_send_json(args: argparse.Namespace) -> None:
    obj = json.loads(args.json)
    if not isinstance(obj, dict):
        raise PixcutError("send-json payload must be a JSON object")
    with PixcutUSB(args.vid, args.pid, args.timeout_ms) as pixcut:
        print_json(pixcut.send_json_obj(obj))


def run_resume(args: argparse.Namespace) -> None:
    with PixcutUSB(args.vid, args.pid, args.timeout_ms) as pixcut:
        print_json(pixcut.resume())


def run_get_job_info(args: argparse.Namespace) -> None:
    with PixcutUSB(args.vid, args.pid, args.timeout_ms) as pixcut:
        for job_id in args.job_ids:
            print_json({"job_id": job_id, "response": pixcut.get_job_info(job_id)})


def run_make_plt(args: argparse.Namespace) -> None:
    generate_plt(
        Path(args.image),
        Path(args.output),
        args.frameworks,
        Path(args.config) if args.config else None,
        args.dilation,
        args.angle,
        resolve_int(args.media_type, MEDIA_TYPES, "media type"),
        args.kp,
    )


def common_job_kwargs(args: argparse.Namespace) -> dict[str, int]:
    return {
        "channel": args.channel,
        "copies": args.copies,
        "media_size": resolve_int(args.media_size, MEDIA_SIZES, "media size"),
        "media_type": resolve_int(args.media_type, MEDIA_TYPES, "media type"),
        "job_type": args.job_type,
    }


def require_execute(args: argparse.Namespace, preview: dict[str, Any]) -> bool:
    if args.execute:
        return True
    print_json(
        {
            "dry_run": True,
            "message": "Add --execute to create the job and send data to the printer.",
            **preview,
        }
    )
    return False


def send_resume_probe(pixcut: PixcutUSB, job_id: int, reason: str) -> None:
    try:
        response = pixcut.resume()
    except Exception as exc:
        print_json({"job_id": job_id, reason: "resume-printer", "resume_error": str(exc)})
    else:
        print_json({"job_id": job_id, reason: "resume-printer", "resume_response": response})


def send_print_enable_probe(pixcut: PixcutUSB, job_id: int, reason: str, enabled: int) -> None:
    try:
        response = pixcut.set_prop_obj({"print-function-enable": enabled})
    except Exception as exc:
        print_json({"job_id": job_id, reason: f"print-function-enable={enabled}", "set_prop_error": str(exc)})
    else:
        print_json({"job_id": job_id, reason: f"print-function-enable={enabled}", "set_prop_response": response})


def maybe_resume_after_upload(pixcut: PixcutUSB, args: argparse.Namespace, job_id: int) -> None:
    if args.resume_after_send:
        send_resume_probe(pixcut, job_id, "post_upload_command")
    if args.enable_print_after_send:
        send_print_enable_probe(pixcut, job_id, "post_upload_command", 1)


def wait_after_send(pixcut: PixcutUSB, args: argparse.Namespace, job_id: int) -> None:
    if args.wait_after_send <= 0:
        return

    deadline = time.monotonic() + args.wait_after_send
    poll_index = 0
    resumed_on_active = False
    enabled_print_on_active = False
    failed_states = {7}
    completed_states = {9}
    while True:
        poll_index += 1
        job_state: int | None = None
        transfer_status: int | None = None
        try:
            response = pixcut.get_props(JOB_POLL_PROPS)
        except Exception as exc:
            print_json({"job_id": job_id, "poll": poll_index, "poll_error": str(exc)})
        else:
            print_json({"job_id": job_id, "poll": poll_index, "props": response})

        try:
            job_info = pixcut.get_job_info(job_id)
        except Exception as exc:
            print_json({"job_id": job_id, "poll": poll_index, "job_info_error": str(exc)})
        else:
            print_json({"job_id": job_id, "poll": poll_index, "job_info": job_info})
            job_state = extract_first_int(job_info, "job-state")
            transfer_status = extract_first_int(job_info, "transfer-status")

        if args.resume_on_active and not resumed_on_active and (job_state == 3 or transfer_status == 3):
            resumed_on_active = True
            send_resume_probe(pixcut, job_id, "active_command")
        if args.enable_print_on_active and not enabled_print_on_active and (job_state == 3 or transfer_status == 3):
            enabled_print_on_active = True
            send_print_enable_probe(pixcut, job_id, "active_command", 1)

        if job_state in failed_states or job_state in completed_states:
            return

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(args.poll_interval, remaining))


def extract_first_int(value: Any, key: str) -> int | None:
    if isinstance(value, dict):
        if key in value:
            try:
                return int(value[key])
            except (TypeError, ValueError):
                return None
        for child in value.values():
            found = extract_first_int(child, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = extract_first_int(child, key)
            if found is not None:
                return found
    return None


def run_print(args: argparse.Namespace) -> None:
    image = Path(args.image).read_bytes()
    preview = {"kind": "print", "image_bytes": len(image), "params": common_job_kwargs(args)}
    if not require_execute(args, preview):
        return
    with PixcutUSB(args.vid, args.pid, args.timeout_ms) as pixcut:
        job_id, response = pixcut.create_print_job(len(image), **common_job_kwargs(args))
        print_json({"job_id": job_id, "create_response": response})
        pixcut.send_job_data(job_id, image, args.chunk_size, args.chunk_delay, args.usb_write_size)
        maybe_resume_after_upload(pixcut, args, job_id)
        wait_after_send(pixcut, args, job_id)


def run_cut(args: argparse.Namespace) -> None:
    plot = Path(args.plt).read_bytes()
    preview = {"kind": "cut", "plot_bytes": len(plot), "params": common_job_kwargs(args)}
    if not require_execute(args, preview):
        return
    with PixcutUSB(args.vid, args.pid, args.timeout_ms) as pixcut:
        job_id, response = pixcut.create_cut_job(len(plot), **common_job_kwargs(args))
        print_json({"job_id": job_id, "create_response": response})
        pixcut.send_job_data(job_id, plot, args.chunk_size, args.chunk_delay, args.usb_write_size)
        maybe_resume_after_upload(pixcut, args, job_id)
        wait_after_send(pixcut, args, job_id)


def run_print_cut(args: argparse.Namespace) -> None:
    image = Path(args.image).read_bytes()
    plot = Path(args.plt).read_bytes()
    preview = {
        "kind": "print-cut",
        "image_bytes": len(image),
        "plot_bytes": len(plot),
        "data_order": args.data_order,
        "params": common_job_kwargs(args),
    }
    if not require_execute(args, preview):
        return
    with PixcutUSB(args.vid, args.pid, args.timeout_ms) as pixcut:
        job_id, response = pixcut.create_print_cut_job(len(image), len(plot), **common_job_kwargs(args))
        print_json({"job_id": job_id, "create_response": response})
        if args.data_order == "image-plot":
            data = image + plot
        else:
            data = plot + image
        pixcut.send_job_data(job_id, data, args.chunk_size, args.chunk_delay, args.usb_write_size)
        maybe_resume_after_upload(pixcut, args, job_id)
        wait_after_send(pixcut, args, job_id)


def add_usb_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--vid", type=lambda s: int(s, 0), default=DEFAULT_VID)
    parser.add_argument("--pid", type=lambda s: int(s, 0), default=DEFAULT_PID)
    parser.add_argument("--timeout-ms", type=int, default=5000)


def add_job_args(parser: argparse.ArgumentParser, default_job_type: int = DEFAULT_JOB_TYPE) -> None:
    add_usb_args(parser)
    parser.add_argument("--execute", action="store_true", help="Actually create the job and send data")
    parser.add_argument("--channel", type=int, default=2)
    parser.add_argument("--copies", type=int, default=1)
    parser.add_argument("--media-size", default="4x7", help="Name or integer, e.g. 3x3, 4x6, 4x7, 5013")
    parser.add_argument("--media-type", default="sticker_4x7", help="Name or integer, e.g. photo, sticker_4x7, 2030")
    parser.add_argument("--job-type", type=int, default=default_job_type)
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=SDK_DATA_CHUNK_SIZE,
        help="Payload bytes per data packet. The SDK uses 10215 for this device.",
    )
    parser.add_argument(
        "--usb-write-size",
        type=int,
        default=SDK_USB_WRITE_SIZE,
        help="USB bulk write size inside each framed data packet. The SDK uses 1024.",
    )
    parser.add_argument(
        "--chunk-delay",
        type=float,
        default=SDK_ACK_DELAY_SECONDS,
        help="Delay in seconds after each ACK. The SDK uses 0.02.",
    )
    parser.add_argument(
        "--wait-after-send",
        type=float,
        default=0.0,
        help="Keep the USB connection open and poll job status for this many seconds after upload.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=2.0,
        help="Seconds between --wait-after-send status polls.",
    )
    parser.add_argument(
        "--resume-after-send",
        action="store_true",
        help="Send resume-printer once immediately after all job data is uploaded.",
    )
    parser.add_argument(
        "--resume-on-active",
        action="store_true",
        help="Send resume-printer once when polling first sees job-state=3 or transfer-status=3.",
    )
    parser.add_argument(
        "--enable-print-after-send",
        action="store_true",
        help="Set print-function-enable=1 once immediately after all job data is uploaded.",
    )
    parser.add_argument(
        "--enable-print-on-active",
        action="store_true",
        help="Set print-function-enable=1 once when polling first sees job-state=3 or transfer-status=3.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Liene PixCut S1 USB utility")
    sub = parser.add_subparsers(dest="command", required=True)

    probe = sub.add_parser("probe", help="Read status, device info, and USB printer ID")
    add_usb_args(probe)
    probe.set_defaults(func=run_probe)

    get_prop = sub.add_parser("get-prop", help="Read one or more device properties")
    add_usb_args(get_prop)
    get_prop.add_argument("props", nargs="+")
    get_prop.set_defaults(func=run_get_prop)

    set_prop = sub.add_parser("set-prop", help="Set a string-valued property")
    add_usb_args(set_prop)
    set_prop.add_argument("key")
    set_prop.add_argument("value")
    set_prop.add_argument("--json-value", action="store_true", help="Parse value as JSON instead of sending a string")
    set_prop.set_defaults(func=run_set_prop)

    send_json = sub.add_parser("send-json", help="Send a raw JSON command object framed for the device")
    add_usb_args(send_json)
    send_json.add_argument("json")
    send_json.set_defaults(func=run_send_json)

    resume = sub.add_parser("resume", help="Send resume-printer")
    add_usb_args(resume)
    resume.set_defaults(func=run_resume)

    get_job_info = sub.add_parser("get-job-info", help="Read retained info for one or more device job IDs")
    add_usb_args(get_job_info)
    get_job_info.add_argument("job_ids", nargs="+", type=int)
    get_job_info.set_defaults(func=run_get_job_info)

    make_plt = sub.add_parser("make-plt", help="Generate a cutter PLT file using the Liene app library")
    make_plt.add_argument("image", help="Input image path; transparent PNG alpha becomes the cut mask")
    make_plt.add_argument("-o", "--output", required=True)
    make_plt.add_argument("--frameworks", help="Path to Liene Photo.app/Contents/Frameworks")
    make_plt.add_argument("--config", help="Optional PLT generator JSON config")
    make_plt.add_argument(
        "--dilation",
        type=float,
        default=1.5,
        help="Cut outline expansion. The official app defaults to 1.5 for 4x7 sticker paper.",
    )
    make_plt.add_argument("--angle", type=float, default=0.0)
    make_plt.add_argument("--media-type", default="sticker_4x7")
    make_plt.add_argument("--kp", type=float, default=42.0, help="PLT KP value. Official app emits KP42.")
    make_plt.set_defaults(func=run_make_plt)

    print_job = sub.add_parser("print", help="Create a print job and send an image file")
    print_job.add_argument("image")
    add_job_args(print_job)
    print_job.set_defaults(func=run_print)

    cut_job = sub.add_parser("cut", help="Create a cut job and send a PLT file")
    cut_job.add_argument("plt")
    add_job_args(cut_job)
    cut_job.set_defaults(func=run_cut, media_size="3x3", media_type="sticker_3x3")

    combo = sub.add_parser("print-cut", help="Create a combined print+cut job and send combined image/PLT data")
    combo.add_argument("image")
    combo.add_argument("plt")
    add_job_args(combo, default_job_type=DEFAULT_PRINT_CUT_JOB_TYPE)
    combo.add_argument(
        "--data-order",
        choices=("plot-image", "image-plot"),
        default="plot-image",
        help="Combined upload byte order. The official SDK sends PLT bytes followed by image bytes.",
    )
    combo.set_defaults(func=run_print_cut)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
        return 0
    except PixcutError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
