#!/usr/bin/env python3
"""Run or attach to Liene Photo.app and trace PixCut USB SDK calls with Frida."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import sys
from typing import Any, TextIO

import frida


DEFAULT_APP = "/Applications/Liene Photo.app/Contents/MacOS/Liene Photo"
DEFAULT_SCRIPT = Path(__file__).with_name("frida_trace_pixcut.js")
LOG_HANDLE: TextIO | None = None


def on_message(message: dict[str, Any], data: bytes | None) -> None:
    if message.get("type") == "send":
        line = json.dumps(message.get("payload"), sort_keys=True)
        print(line, flush=True)
        if LOG_HANDLE is not None:
            print(line, file=LOG_HANDLE, flush=True)
        return
    line = json.dumps(message, sort_keys=True)
    print(line, file=sys.stderr, flush=True)
    if LOG_HANDLE is not None:
        print(line, file=LOG_HANDLE, flush=True)
    if data:
        data_line = json.dumps({"kind": "message_data", "hex": data.hex()}, sort_keys=True)
        print(data_line, file=sys.stderr, flush=True)
        if LOG_HANDLE is not None:
            print(data_line, file=LOG_HANDLE, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--spawn", action="store_true", help="Launch Liene Photo.app under Frida")
    mode.add_argument("--attach", metavar="PROCESS", help="Attach to an existing process name or PID")
    parser.add_argument("--app", default=DEFAULT_APP, help="Executable path used with --spawn")
    parser.add_argument("--script", default=str(DEFAULT_SCRIPT), help="Frida JavaScript hook file")
    parser.add_argument("--log-file", help="Optional JSONL trace file. Useful because stdout can be truncated.")
    args = parser.parse_args()

    global LOG_HANDLE
    if args.log_file:
        log_path = Path(args.log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        LOG_HANDLE = log_path.open("a", encoding="utf-8")

    script_source = Path(args.script).read_text()
    device = frida.get_local_device()

    pid: int | None = None
    if args.spawn:
        pid = device.spawn([args.app])
        session = device.attach(pid)
    else:
        target: str | int
        target = int(args.attach) if args.attach.isdigit() else args.attach
        session = device.attach(target)

    script = session.create_script(script_source)
    script.on("message", on_message)
    script.load()

    if pid is not None:
        device.resume(pid)
        print(json.dumps({"kind": "spawned", "pid": pid, "app": args.app}, sort_keys=True), flush=True)
    else:
        print(json.dumps({"kind": "attached", "target": args.attach}, sort_keys=True), flush=True)

    print("Tracing. Start a print in Liene Photo.app, then press Ctrl-C here when complete.", flush=True)
    try:
        signal.pause()
    except KeyboardInterrupt:
        pass
    finally:
        session.detach()
        if LOG_HANDLE is not None:
            LOG_HANDLE.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
