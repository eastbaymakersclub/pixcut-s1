#!/usr/bin/env python3
"""Call Liene's native libdevice_sdk directly for PixCut S1 experiments."""

from __future__ import annotations

import argparse
import ctypes
from pathlib import Path
import sys


DEFAULT_VID = 0x302C
DEFAULT_PID = 0x3101
DEFAULT_FRAMEWORKS = Path(__file__).with_name("trace-app") / "Liene Photo.app" / "Contents" / "Frameworks"
DEFAULT_PRINT_CUT_JOB_TYPE = 600

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


class PrintCutJobParams(ctypes.Structure):
    _fields_ = [
        ("channel", ctypes.c_int),
        ("copies", ctypes.c_int),
        ("media_size", ctypes.c_int),
        ("media_type", ctypes.c_int),
        ("job_type", ctypes.c_int),
        ("image_size", ctypes.c_int),
        ("plot_size", ctypes.c_int),
    ]


ProgressCallback = ctypes.CFUNCTYPE(None, ctypes.c_uint, ctypes.c_int, ctypes.c_void_p)


class PixcutSdkError(RuntimeError):
    pass


def resolve_int(value: str, table: dict[str, int], label: str) -> int:
    if value in table:
        return table[value]
    try:
        return int(value, 0)
    except ValueError as exc:
        names = ", ".join(sorted(table))
        raise argparse.ArgumentTypeError(f"Unknown {label} {value!r}; use one of {names} or an integer") from exc


def load_sdk(frameworks: Path) -> ctypes.CDLL:
    logger_path = frameworks / "liblogger.dylib"
    sdk_path = frameworks / "libdevice_sdk.dylib"
    if not logger_path.exists() or not sdk_path.exists():
        raise PixcutSdkError(f"Could not find liblogger.dylib and libdevice_sdk.dylib under {frameworks}")

    ctypes.CDLL(str(logger_path), mode=ctypes.RTLD_GLOBAL)
    sdk = ctypes.CDLL(str(sdk_path), mode=ctypes.RTLD_GLOBAL)

    sdk.device_sdk_create_instance.argtypes = []
    sdk.device_sdk_create_instance.restype = ctypes.c_void_p
    sdk.device_sdk_destroy_instance.argtypes = [ctypes.c_void_p]
    sdk.device_sdk_destroy_instance.restype = None
    sdk.device_sdk_connect.argtypes = [ctypes.c_void_p, ctypes.c_ushort, ctypes.c_ushort]
    sdk.device_sdk_connect.restype = ctypes.c_int
    sdk.device_sdk_disconnect.argtypes = [ctypes.c_void_p]
    sdk.device_sdk_disconnect.restype = ctypes.c_int
    sdk.device_sdk_set_log_level.argtypes = [ctypes.c_int]
    sdk.device_sdk_set_log_level.restype = None
    sdk.device_sdk_create_print_cut_job.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(PrintCutJobParams),
        ctypes.POINTER(ctypes.c_ubyte),
        ctypes.POINTER(ctypes.c_ubyte),
        ctypes.POINTER(ctypes.c_uint),
        ProgressCallback,
        ctypes.c_void_p,
    ]
    sdk.device_sdk_create_print_cut_job.restype = ctypes.c_int
    return sdk


def run_print_cut(args: argparse.Namespace) -> None:
    image = Path(args.image).read_bytes()
    plot = Path(args.plt).read_bytes()
    params = PrintCutJobParams(
        channel=args.channel,
        copies=args.copies,
        media_size=resolve_int(args.media_size, MEDIA_SIZES, "media size"),
        media_type=resolve_int(args.media_type, MEDIA_TYPES, "media type"),
        job_type=args.job_type,
        image_size=len(image),
        plot_size=len(plot),
    )

    preview = {
        "kind": "sdk-print-cut",
        "image_bytes": len(image),
        "plot_bytes": len(plot),
        "params": {
            "channel": params.channel,
            "copies": params.copies,
            "media_size": params.media_size,
            "media_type": params.media_type,
            "job_type": params.job_type,
            "image_size": params.image_size,
            "plot_size": params.plot_size,
        },
    }
    if not args.execute:
        print(preview)
        return

    sdk = load_sdk(Path(args.frameworks))
    sdk.device_sdk_set_log_level(args.log_level)

    image_buf = (ctypes.c_ubyte * len(image)).from_buffer_copy(image)
    plot_buf = (ctypes.c_ubyte * len(plot)).from_buffer_copy(plot)

    @ProgressCallback
    def progress(job_id: int, progress_value: int, user_data: ctypes.c_void_p) -> None:
        print({"job_id": int(job_id), "progress": int(progress_value)}, file=sys.stderr, flush=True)

    instance = sdk.device_sdk_create_instance()
    if not instance:
        raise PixcutSdkError("device_sdk_create_instance returned NULL")
    try:
        rc = sdk.device_sdk_connect(instance, args.vid, args.pid)
        print({"connect_rc": rc}, flush=True)
        if rc != 0:
            raise PixcutSdkError(f"device_sdk_connect failed: {rc}")

        job_id = ctypes.c_uint(0)
        rc = sdk.device_sdk_create_print_cut_job(
            instance,
            ctypes.byref(params),
            image_buf,
            plot_buf,
            ctypes.byref(job_id),
            progress,
            None,
        )
        print({"create_print_cut_rc": rc, "job_id": int(job_id.value)}, flush=True)
        if rc != 0:
            raise PixcutSdkError(f"device_sdk_create_print_cut_job failed: {rc}")
    finally:
        try:
            sdk.device_sdk_disconnect(instance)
        finally:
            sdk.device_sdk_destroy_instance(instance)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    print_cut = sub.add_parser("print-cut", help="Create a print+cut job through libdevice_sdk")
    print_cut.add_argument("image")
    print_cut.add_argument("plt")
    print_cut.add_argument("--frameworks", default=str(DEFAULT_FRAMEWORKS))
    print_cut.add_argument("--vid", type=lambda s: int(s, 0), default=DEFAULT_VID)
    print_cut.add_argument("--pid", type=lambda s: int(s, 0), default=DEFAULT_PID)
    print_cut.add_argument("--execute", action="store_true")
    print_cut.add_argument("--channel", type=int, default=2)
    print_cut.add_argument("--copies", type=int, default=1)
    print_cut.add_argument("--media-size", default="4x7")
    print_cut.add_argument("--media-type", default="sticker_4x7")
    print_cut.add_argument("--job-type", type=int, default=DEFAULT_PRINT_CUT_JOB_TYPE)
    print_cut.add_argument("--log-level", type=int, default=0)
    print_cut.set_defaults(func=run_print_cut)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
        return 0
    except PixcutSdkError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
