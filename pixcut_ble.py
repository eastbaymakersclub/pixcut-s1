#!/usr/bin/env python3
"""BLE tooling for the Liene PixCut S1.

The working USB protocol frames commands as ``cmd json\n`` plus compact JSON,
and streams job data as ``cmd data EXTLEN=...`` packets. The PixCut also
advertises BLE GATT services. The Android app uses a related Hannto frame
format over Bluetooth; this module can probe GATT characteristics, send small
JSON commands over confirmed BLE data paths, and create/upload print jobs with
an explicit ``--execute`` guard.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Iterable
import json
from pathlib import Path
import re
import struct
import subprocess
import sys
import time
from typing import Any

from bleak import BleakClient, BleakScanner

from pixcut_usb import (
    CMD_DATA_HEADER,
    CMD_JSON_HEADER,
    DEFAULT_JOB_TYPE,
    DEFAULT_PRINT_CUT_JOB_TYPE,
    MEDIA_SIZES,
    MEDIA_TYPES,
    PixcutError,
    STATUS_PROPS,
    add_status_notes,
    extract_job_id,
    find_error_code,
    resolve_int,
)


PIXCUT_NAME_PREFIX = "Liene PixCut S1"
PIXCUT_ADV_SERVICE = "0000fc00-0000-1000-8000-00805f9b34fb"
FE95_WRITE_READ_UUID = "00000013-0000-1000-8000-00805f9b34fb"

NOTIFY_CHARS = {
    "ff00-status": "0000ff03-0000-1000-8000-00805f9b34fb",
    "ff00-data": "0000ff01-0000-1000-8000-00805f9b34fb",
    "ff80-data": "0000ff81-0000-1000-8000-00805f9b34fb",
    "mldp-data": "49535343-1e4d-4bd9-ba61-23c647249616",
    "mldp-control": "49535343-aca3-481c-91ec-d85e28a60318",
    "fe95-0001": "00000001-0000-1000-8000-00805f9b34fb",
    "fe95-0005": "00000005-0000-1000-8000-00805f9b34fb",
    "fe95-0015": "00000015-0000-1000-8000-00805f9b34fb",
    "fe95-0016": "00000016-0000-1000-8000-00805f9b34fb",
    "fee7-fec8": "0000fec8-0000-1000-8000-00805f9b34fb",
}

WRITE_CHARS = {
    "ff00-ff02": "0000ff02-0000-1000-8000-00805f9b34fb",
    "ff80-ff82": "0000ff82-0000-1000-8000-00805f9b34fb",
    "mldp-8841": "49535343-8841-43f4-a8d4-ecbe34729bb3",
    "mldp-aca3": "49535343-aca3-481c-91ec-d85e28a60318",
    "fee7-fec7": "0000fec7-0000-1000-8000-00805f9b34fb",
    "fe95-0001": "00000001-0000-1000-8000-00805f9b34fb",
    "fe95-0005": "00000005-0000-1000-8000-00805f9b34fb",
    "fe95-0010": "00000010-0000-1000-8000-00805f9b34fb",
    "fe95-0013": FE95_WRITE_READ_UUID,
    "fe95-0015": "00000015-0000-1000-8000-00805f9b34fb",
    "fe95-0016": "00000016-0000-1000-8000-00805f9b34fb",
}

PROFILES = {
    # This pair ACKs writes with 0x0101 on ff00-status. It has not yet returned
    # USB-style JSON responses, but it is a real PixCut data path.
    "ff00": {
        "write": "0000ff02-0000-1000-8000-00805f9b34fb",
        "notify": ("ff00-status", "ff00-data"),
        "prefer_response": False,
    },
    # This is the Microchip/ISSC transparent UART service. It returned
    # 000000b901 to a USB JSON frame on the data notify characteristic.
    "mldp": {
        "write": "49535343-8841-43f4-a8d4-ecbe34729bb3",
        "notify": ("mldp-data", "mldp-control"),
        "prefer_response": False,
    },
    # This behaves like an AT/modem channel and returned ASCII ERROR to a USB
    # JSON frame. Kept for controlled probes, not for normal printer commands.
    "ff80": {
        "write": "0000ff82-0000-1000-8000-00805f9b34fb",
        "notify": ("ff80-data",),
        "prefer_response": False,
    },
}

READABLE_CHARS = [
    "0000fec9-0000-1000-8000-00805f9b34fb",
    "00000002-0000-1000-8000-00805f9b34fb",
    "00000004-0000-1000-8000-00805f9b34fb",
    "00000013-0000-1000-8000-00805f9b34fb",
    "00000014-0000-1000-8000-00805f9b34fb",
]

HANNTO_JSON_PROFILES = ("fe95-0013", "ff00-ff02", "mldp-8841")
MAC_RE = re.compile(r"^(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")


class PixcutBLEError(RuntimeError):
    pass


def bytes_summary(data: bytes) -> dict[str, Any]:
    summary: dict[str, Any] = {"len": len(data), "hex": data.hex()}
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return summary
    if all((32 <= ord(ch) < 127) or ch in "\r\n\t" for ch in text):
        summary["ascii"] = text
    return summary


def jsonable_bytes_map(values: dict[Any, bytes]) -> dict[str, str]:
    return {str(key): bytes(value).hex() for key, value in values.items()}


def print_json(value: Any) -> None:
    print(json.dumps(add_status_notes(value), indent=2, sort_keys=True))


def local_bluetooth_mac() -> str | None:
    try:
        result = subprocess.run(
            ["system_profiler", "SPBluetoothDataType"],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    match = re.search(r"Address:\s*((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})", result.stdout)
    return match.group(1).upper() if match else None


def normalize_mac(mac: str) -> str:
    mac = mac.strip().upper().replace("-", ":")
    if not MAC_RE.match(mac):
        raise PixcutBLEError(f"Expected a Bluetooth MAC like AA:BB:CC:DD:EE:FF, got {mac!r}")
    return mac


def hannto_checksum(frame: bytes | bytearray) -> int:
    return sum(frame[1:-2]) & 0xFF


def hannto_attribute(*, splited: bool, encrypt_type: int, body_len: int) -> int:
    if body_len > 0x3FF:
        raise PixcutBLEError(f"Hannto BLE frame body is too large: {body_len} bytes")
    return (0x2000 if splited else 0) | ((encrypt_type & 0x07) << 10) | body_len


def build_hannto_frame(
    body: bytes,
    *,
    version: int = 100,
    reserve: int = 0,
    connect_type: int = 1,
    interactive: int = 6,
    encoding: int = 3,
    terminal_id: int = 0,
    msg_sn: int = 1,
    package_total: int = 1,
    package_num: int = 1,
    splited: bool = False,
    encrypt_type: int = 0,
) -> bytes:
    attr = hannto_attribute(splited=splited, encrypt_type=encrypt_type, body_len=len(body))
    frame = bytearray(len(body) + 22)
    frame[0] = 0x7E
    frame[1] = version & 0xFF
    frame[2] = reserve & 0xFF
    frame[3] = connect_type & 0xFF
    frame[4] = interactive & 0xFF
    frame[5] = encoding & 0xFF
    frame[6:10] = struct.pack("<I", terminal_id)
    frame[10:14] = struct.pack("<I", msg_sn)
    frame[14:16] = struct.pack("<H", package_total)
    frame[16:18] = struct.pack("<H", package_num)
    frame[18:20] = struct.pack("<H", attr)
    frame[20 : 20 + len(body)] = body
    frame[-2] = hannto_checksum(frame)
    frame[-1] = 0x7E
    return bytes(frame)


def build_hannto_packets(
    data: bytes,
    *,
    job_id: int = -1,
    data_chunk_size: int | None = None,
    version: int = 100,
    connect_type: int = 1,
    interactive: int = 6,
    encoding: int = 3,
    splited: bool = False,
    encrypt_type: int = 0,
) -> list[bytes]:
    max_chunk_size = 892 if job_id != -1 else 896
    chunk_size = data_chunk_size or max_chunk_size
    if chunk_size <= 0 or chunk_size > max_chunk_size:
        raise PixcutBLEError(f"data_chunk_size must be 1..{max_chunk_size}, got {chunk_size}")
    if not data:
        chunks = [b""]
    else:
        chunks = [data[offset : offset + chunk_size] for offset in range(0, len(data), chunk_size)]
    package_total = len(chunks)
    frames = []
    for idx, chunk in enumerate(chunks):
        body = struct.pack("<I", job_id) + chunk if job_id != -1 else chunk
        frames.append(
            build_hannto_frame(
                body,
                version=version,
                connect_type=connect_type,
                interactive=interactive,
                encoding=encoding,
                msg_sn=idx + 1,
                package_total=package_total,
                package_num=idx + 1,
                splited=splited,
                encrypt_type=encrypt_type,
            )
        )
    return frames


def hannto_encrypt_type(attr: int) -> int:
    return (attr >> 10) & 0x07


def parse_hannto_frame(frame: bytes) -> dict[str, Any]:
    attr = struct.unpack("<H", frame[18:20])[0]
    body_len = attr & 0x03FF
    body = frame[20 : 20 + body_len]
    parsed: dict[str, Any] = {
        "len": len(frame),
        "version": frame[1],
        "reserve": frame[2],
        "connect_type": frame[3],
        "interactive": frame[4],
        "encoding": frame[5],
        "terminal_id": struct.unpack("<I", frame[6:10])[0],
        "msg_sn": struct.unpack("<I", frame[10:14])[0],
        "package_total": struct.unpack("<H", frame[14:16])[0],
        "package_num": struct.unpack("<H", frame[16:18])[0],
        "splited": bool(attr & 0x2000),
        "encrypt_type": hannto_encrypt_type(attr),
        "body": bytes_summary(body),
        "checksum": frame[-2],
        "checksum_ok": hannto_checksum(frame) == frame[-2],
    }
    decoded = decode_notification_payload(body)
    if decoded is not None:
        parsed["json"] = decoded
    return parsed


def extract_hannto_frames(buffer: bytearray) -> list[dict[str, Any]]:
    frames = []
    while True:
        try:
            start = buffer.index(0x7E)
        except ValueError:
            buffer.clear()
            return frames
        if start:
            del buffer[:start]
        if len(buffer) < 22:
            return frames
        if buffer[1] not in (0x01, 0x64):
            del buffer[0]
            continue
        attr = struct.unpack("<H", buffer[18:20])[0]
        body_len = attr & 0x03FF
        frame_len = body_len + 22
        if body_len > 992:
            del buffer[:3]
            continue
        if len(buffer) < frame_len:
            return frames
        if buffer[frame_len - 1] != 0x7E:
            del buffer[:3]
            continue
        frame = bytes(buffer[:frame_len])
        del buffer[:frame_len]
        frames.append(parse_hannto_frame(frame))


def service_dict(client: BleakClient) -> list[dict[str, Any]]:
    services = []
    for service in client.services:
        characteristics = []
        for char in service.characteristics:
            characteristics.append(
                {
                    "uuid": char.uuid,
                    "handle": getattr(char, "handle", None),
                    "properties": list(char.properties),
                    "description": char.description,
                    "max_write_without_response_size": getattr(char, "max_write_without_response_size", None),
                    "descriptors": [
                        {
                            "uuid": descriptor.uuid,
                            "handle": getattr(descriptor, "handle", None),
                            "description": descriptor.description,
                        }
                        for descriptor in char.descriptors
                    ],
                }
            )
        services.append(
            {
                "uuid": service.uuid,
                "handle": getattr(service, "handle", None),
                "description": service.description,
                "characteristics": characteristics,
            }
        )
    return services


def decode_notification_payload(data: bytes) -> Any | None:
    payload = data
    if payload.startswith(CMD_JSON_HEADER):
        payload = payload[len(CMD_JSON_HEADER) :]
    try:
        text = payload.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


class NotificationLog:
    def __init__(self, *, verbose: bool = False) -> None:
        self.started = time.monotonic()
        self.events: list[dict[str, Any]] = []
        self.hannto_buffers: dict[str, bytearray] = {}
        self.verbose = verbose

    def callback(self, label: str):
        def on_notify(char: Any, data: bytearray) -> None:
            raw = bytes(data)
            char_uuid = str(getattr(char, "uuid", char))
            event = {
                "t": round(time.monotonic() - self.started, 3),
                "label": label,
                "char_uuid": char_uuid,
                "data": bytes_summary(raw),
            }
            decoded = decode_notification_payload(raw)
            if decoded is not None:
                event["json"] = decoded
            buffer = self.hannto_buffers.setdefault(label, bytearray())
            buffer.extend(raw)
            hannto_frames = extract_hannto_frames(buffer)
            if hannto_frames:
                event["hannto_frames"] = hannto_frames
            self.events.append(event)
            if self.verbose:
                print_json({"notification": event})

        return on_notify

    def clear(self) -> None:
        self.events.clear()
        self.hannto_buffers.clear()
        self.started = time.monotonic()

    def decoded_json(self, request_id: int | None = None) -> Any | None:
        for event in self.events:
            decoded_values = [event.get("json")]
            decoded_values.extend(frame.get("json") for frame in event.get("hannto_frames", []))
            for decoded in decoded_values:
                if decoded is None:
                    continue
                if request_id is None or not isinstance(decoded, dict) or decoded.get("id") == request_id:
                    return decoded
        return None


def notification_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    by_label: dict[str, int] = {}
    by_payload: dict[str, int] = {}
    decoded_json: list[Any] = []
    partial_hannto_bodies: list[dict[str, Any]] = []
    for event in events:
        label = str(event.get("label", "unknown"))
        by_label[label] = by_label.get(label, 0) + 1
        data = event.get("data")
        if isinstance(data, dict):
            hex_value = str(data.get("hex", ""))
            if hex_value:
                raw = bytes.fromhex(hex_value)
                if len(raw) > 20 and raw[0] == 0x7E:
                    declared_body_len = struct.unpack("<H", raw[18:20])[0] & 0x03FF
                    body = raw[20:]
                    if body.endswith(b"~"):
                        body = body[:-2]
                    try:
                        text_prefix = body.decode("utf-8", "replace")
                    except UnicodeDecodeError:
                        text_prefix = ""
                    if text_prefix:
                        partial_hannto_bodies.append(
                            {
                                "label": label,
                                "declared_body_len": declared_body_len,
                                "received_body_prefix_len": len(body),
                                "text_prefix": text_prefix[:220],
                            }
                        )
                if len(hex_value) > 96:
                    hex_value = f"{hex_value[:96]}...({len(hex_value) // 2} bytes)"
                key = f"{label}:{hex_value}"
                by_payload[key] = by_payload.get(key, 0) + 1
        if "json" in event:
            decoded_json.append(event["json"])
        for frame in event.get("hannto_frames", []):
            if isinstance(frame, dict) and "json" in frame:
                decoded_json.append(frame["json"])
    return {
        "count": len(events),
        "by_label": by_label,
        "by_payload": by_payload,
        "decoded_json": decoded_json[:5],
        "partial_hannto_bodies": partial_hannto_bodies[:5],
    }


def response_result(value: Any) -> Any | None:
    if isinstance(value, dict):
        return value.get("result")
    return None


def printer_status_tuple(value: Any) -> tuple[str | None, str | None, str | None]:
    result = response_result(value)
    if isinstance(result, list) and len(result) >= 3:
        return (str(result[0]), str(result[1]), str(result[2]))
    return (None, None, None)


async def find_pixcut_address(scan_seconds: float, name: str | None, address: str | None) -> str:
    if address:
        return address
    devices = await BleakScanner.discover(timeout=scan_seconds, return_adv=True)
    candidates: list[dict[str, Any]] = []
    for device, adv in devices.values():
        device_name = device.name or adv.local_name or ""
        has_pixcut_name = device_name.startswith(name or PIXCUT_NAME_PREFIX)
        has_pixcut_service = PIXCUT_ADV_SERVICE in {uuid.lower() for uuid in adv.service_data}
        if has_pixcut_name or has_pixcut_service:
            candidates.append(
                {
                    "address": device.address,
                    "name": device_name,
                    "rssi": adv.rssi,
                    "service_data": jsonable_bytes_map(adv.service_data),
                    "manufacturer_data": jsonable_bytes_map(adv.manufacturer_data),
                }
            )
    if not candidates:
        raise PixcutBLEError(f"PixCut BLE advertisement not found after {scan_seconds:g}s")
    candidates.sort(key=lambda item: item.get("rssi") or -999, reverse=True)
    print_json({"selected": candidates[0], "candidates": candidates})
    return str(candidates[0]["address"])


class PixcutBLE:
    def __init__(
        self,
        *,
        address: str | None = None,
        name: str | None = None,
        scan_seconds: float = 10.0,
        timeout: float = 20.0,
    ) -> None:
        self.address = address
        self.name = name
        self.scan_seconds = scan_seconds
        self.timeout = timeout
        self.client: BleakClient | None = None
        self.next_id = int(time.monotonic() * 1000) % 900000 + 1000
        self.notifications = NotificationLog()

    async def __aenter__(self) -> "PixcutBLE":
        address = await find_pixcut_address(self.scan_seconds, self.name, self.address)
        self.address = address
        self.client = BleakClient(address, timeout=self.timeout)
        await self.client.__aenter__()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.client is not None:
            await self.client.__aexit__(exc_type, exc, tb)
        self.client = None

    @property
    def ble(self) -> BleakClient:
        if self.client is None:
            raise PixcutBLEError("BLE device is not connected")
        return self.client

    async def start_notifications(self, labels: Iterable[str]) -> list[str]:
        subscribed = []
        for label in labels:
            uuid = NOTIFY_CHARS[label]
            try:
                await self.ble.start_notify(uuid, self.notifications.callback(label))
            except Exception as exc:
                print_json({"subscribe_error": {"label": label, "uuid": uuid, "error": str(exc)}})
                continue
            subscribed.append(label)
        return subscribed

    async def stop_notifications(self, labels: Iterable[str]) -> None:
        for label in labels:
            try:
                await self.ble.stop_notify(NOTIFY_CHARS[label])
            except Exception:
                pass

    def characteristic(self, uuid: str) -> Any:
        for service in self.ble.services:
            for char in service.characteristics:
                if char.uuid.lower() == uuid.lower():
                    return char
        raise PixcutBLEError(f"Characteristic not found: {uuid}")

    async def write_frame(self, uuid: str, data: bytes, *, response: bool | None = None) -> None:
        char = self.characteristic(uuid)
        if response is None:
            response = "write-without-response" not in char.properties
        if response:
            # ATT write-with-response handles larger values on macOS, but the
            # current PixCut probes use small JSON frames.
            chunk_size = 512
        else:
            chunk_size = getattr(char, "max_write_without_response_size", None) or 20
        if chunk_size <= 0:
            chunk_size = 20
        for offset in range(0, len(data), chunk_size):
            await self.ble.write_gatt_char(uuid, data[offset : offset + chunk_size], response=response)
            await asyncio.sleep(0.02)

    async def write_label(self, label: str, data: bytes, *, response: bool | None = None) -> None:
        await self.write_frame(WRITE_CHARS[label], data, response=response)

    async def send_handoff(self, phone_mac: str, wait_seconds: float) -> dict[str, Any]:
        payload = f"A_OUT_{normalize_mac(phone_mac)}".encode("ascii")
        subscribed = await self.start_notifications(NOTIFY_CHARS)
        await asyncio.sleep(0.5)
        self.notifications.clear()
        try:
            await self.write_frame(FE95_WRITE_READ_UUID, payload, response=True)
            await asyncio.sleep(wait_seconds)
        finally:
            await self.stop_notifications(subscribed)
        reading: dict[str, Any]
        try:
            reading = bytes_summary(bytes(await self.ble.read_gatt_char(FE95_WRITE_READ_UUID)))
        except Exception as exc:
            reading = {"error": str(exc)}
        return {
            "address": self.address,
            "write_uuid": FE95_WRITE_READ_UUID,
            "payload": bytes_summary(payload),
            "read_after_write": reading,
            "notifications": self.notifications.events,
        }

    async def send_json_probe(self, profile: str, obj: dict[str, Any], wait_seconds: float) -> dict[str, Any]:
        profile_info = PROFILES[profile]
        request_id = obj.get("id")
        if request_id is None:
            request_id = self.next_id
            self.next_id += 1
            obj = {**obj, "id": request_id}
        frame = CMD_JSON_HEADER + json.dumps(obj, separators=(",", ":")).encode("utf-8")

        notify_labels = profile_info["notify"]
        subscribed = await self.start_notifications(notify_labels)
        await asyncio.sleep(0.5)
        self.notifications.clear()
        try:
            await self.write_frame(
                str(profile_info["write"]),
                frame,
                response=bool(profile_info["prefer_response"]),
            )
            await asyncio.sleep(wait_seconds)
        finally:
            await self.stop_notifications(subscribed)

        decoded = self.notifications.decoded_json(int(request_id))
        if decoded is not None:
            error_code = find_error_code(decoded)
            if error_code is not None:
                raise PixcutError(f"Device returned error-code {error_code}: {decoded}")
        return {
            "profile": profile,
            "request": obj,
            "frame_bytes": len(frame),
            "decoded_response": decoded,
            "notifications": self.notifications.events,
        }

    async def send_data_probe(self, profile: str, job_id: int, data: bytes, wait_seconds: float) -> dict[str, Any]:
        profile_info = PROFILES[profile]
        frame = CMD_DATA_HEADER + str(len(data)).encode("ascii") + b"\n" + struct.pack("<I", job_id) + data
        notify_labels = profile_info["notify"]
        subscribed = await self.start_notifications(notify_labels)
        await asyncio.sleep(0.5)
        self.notifications.clear()
        try:
            await self.write_frame(str(profile_info["write"]), frame, response=bool(profile_info["prefer_response"]))
            await asyncio.sleep(wait_seconds)
        finally:
            await self.stop_notifications(subscribed)
        return {
            "profile": profile,
            "job_id": job_id,
            "frame_bytes": len(frame),
            "notifications": self.notifications.events,
        }

    async def send_hannto_json_probe(
        self,
        write_label: str,
        obj: dict[str, Any],
        wait_seconds: float,
        *,
        response: bool | None = None,
    ) -> dict[str, Any]:
        request_id = obj.get("id")
        if request_id is None:
            request_id = self.next_id
            self.next_id += 1
            obj = {**obj, "id": request_id}
        payload = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        frames = build_hannto_packets(payload)
        subscribed = await self.start_notifications(NOTIFY_CHARS)
        await asyncio.sleep(0.5)
        self.notifications.clear()
        try:
            for frame in frames:
                await self.write_label(write_label, frame, response=response)
                await asyncio.sleep(0.1)
            await asyncio.sleep(wait_seconds)
        finally:
            await self.stop_notifications(subscribed)

        decoded = self.notifications.decoded_json(int(request_id))
        return {
            "write_label": write_label,
            "write_uuid": WRITE_CHARS[write_label],
            "request": obj,
            "frame_count": len(frames),
            "frames": [bytes_summary(frame) for frame in frames],
            "decoded_response": decoded,
            "notifications": self.notifications.events,
        }

    async def send_json_obj(self, write_label: str, obj: dict[str, Any], wait_seconds: float = 5.0) -> Any:
        result = await self.send_hannto_json_probe(write_label, obj, wait_seconds)
        decoded = result["decoded_response"]
        if decoded is None:
            detail = {key: value for key, value in result.items() if key != "notifications"}
            detail["notification_summary"] = notification_summary(result.get("notifications", []))
            raise PixcutBLEError(f"Timed out waiting for BLE JSON response id {obj.get('id')}: {detail}")
        error_code = find_error_code(decoded)
        if error_code is not None:
            raise PixcutError(f"Device returned error-code {error_code}: {decoded}")
        return decoded

    async def send_json(self, write_label: str, method: str, params: Any | None = None, wait_seconds: float = 5.0) -> Any:
        obj: dict[str, Any] = {"method": method, "id": self.next_id}
        self.next_id += 1
        if params is not None:
            obj["params"] = params
        return await self.send_json_obj(write_label, obj, wait_seconds)

    async def get_props(self, write_label: str, props: list[str], wait_seconds: float = 5.0) -> Any:
        return await self.send_json(write_label, "get-prop", props, wait_seconds)

    async def set_prop_obj(self, write_label: str, values: dict[str, Any], wait_seconds: float = 5.0) -> Any:
        return await self.send_json(write_label, "set-prop", values, wait_seconds)

    async def resume(self, write_label: str, wait_seconds: float = 5.0) -> Any:
        return await self.send_json(write_label, "resume-printer", {}, wait_seconds)

    async def get_job_info(self, write_label: str, job_id: int, wait_seconds: float = 5.0) -> Any:
        return await self.send_json(write_label, "get-job-info", {"job-id": job_id}, wait_seconds)

    async def create_print_job(
        self,
        write_label: str,
        image_len: int,
        channel: int,
        copies: int,
        media_size: int,
        media_type: int,
        job_type: int,
        wait_seconds: float = 5.0,
    ) -> tuple[int, Any]:
        response = await self.send_json(
            write_label,
            "print-job",
            {
                "channel": channel,
                "copies": copies,
                "media-size": media_size,
                "media-type": media_type,
                "job-type": job_type,
                "file-size": image_len,
            },
            wait_seconds,
        )
        return extract_job_id(response), response

    async def create_cut_job(
        self,
        write_label: str,
        plot_len: int,
        channel: int,
        copies: int,
        media_size: int,
        media_type: int,
        job_type: int,
        wait_seconds: float = 5.0,
    ) -> tuple[int, Any]:
        response = await self.send_json(
            write_label,
            "cut-job",
            {
                "channel": channel,
                "copies": copies,
                "media-size": media_size,
                "media-type": media_type,
                "job-type": job_type,
                "plot-file-size": plot_len,
            },
            wait_seconds,
        )
        return extract_job_id(response), response

    async def create_print_cut_job(
        self,
        write_label: str,
        image_len: int,
        plot_len: int,
        channel: int,
        copies: int,
        media_size: int,
        media_type: int,
        job_type: int,
        wait_seconds: float = 5.0,
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
        response = await self.send_json_obj(write_label, obj, wait_seconds)
        return extract_job_id(response), response

    async def send_job_data(
        self,
        write_label: str,
        job_id: int,
        data: bytes,
        *,
        data_chunk_size: int = 156,
        chunk_delay: float = 0.005,
        wait_seconds: float = 1.0,
    ) -> dict[str, Any]:
        frames = build_hannto_packets(
            data,
            job_id=job_id,
            data_chunk_size=data_chunk_size,
            connect_type=2,
            interactive=6,
            encoding=2,
            splited=True,
        )
        subscribed = await self.start_notifications(NOTIFY_CHARS)
        await asyncio.sleep(0.5)
        self.notifications.clear()
        sent = 0
        next_report = 0
        try:
            for index, frame in enumerate(frames, start=1):
                await self.write_label(write_label, frame)
                sent = min(sent + min(data_chunk_size, max(0, len(data) - sent)), len(data))
                if sent >= next_report or index == len(frames):
                    print(f"sent {sent}/{len(data)} bytes over BLE ({index}/{len(frames)} frames)", file=sys.stderr)
                    next_report = sent + 16384
                if chunk_delay > 0:
                    await asyncio.sleep(chunk_delay)
            await asyncio.sleep(wait_seconds)
        finally:
            await self.stop_notifications(subscribed)
        return {
            "job_id": job_id,
            "write_label": write_label,
            "data_bytes": len(data),
            "frame_count": len(frames),
            "data_chunk_size": data_chunk_size,
            "notification_summary": notification_summary(self.notifications.events),
        }


async def run_scan(args: argparse.Namespace) -> None:
    devices = await BleakScanner.discover(timeout=args.scan_seconds, return_adv=True)
    rows = []
    for device, adv in devices.values():
        device_name = device.name or adv.local_name or ""
        if args.all or device_name.startswith(PIXCUT_NAME_PREFIX) or PIXCUT_ADV_SERVICE in {
            uuid.lower() for uuid in adv.service_data
        }:
            rows.append(
                {
                    "address": device.address,
                    "name": device_name,
                    "rssi": adv.rssi,
                    "service_uuids": adv.service_uuids,
                    "service_data": jsonable_bytes_map(adv.service_data),
                    "manufacturer_data": jsonable_bytes_map(adv.manufacturer_data),
                }
            )
    rows.sort(key=lambda item: item.get("rssi") or -999, reverse=True)
    print_json({"devices": rows})


async def run_services(args: argparse.Namespace) -> None:
    async with PixcutBLE(address=args.address, name=args.name, scan_seconds=args.scan_seconds) as pixcut:
        print_json({"address": pixcut.address, "services": service_dict(pixcut.ble)})


async def run_read(args: argparse.Namespace) -> None:
    async with PixcutBLE(address=args.address, name=args.name, scan_seconds=args.scan_seconds) as pixcut:
        readings = {}
        for uuid in args.characteristics:
            try:
                readings[uuid] = bytes_summary(bytes(await pixcut.ble.read_gatt_char(uuid)))
            except Exception as exc:
                readings[uuid] = {"error": str(exc)}
        print_json({"address": pixcut.address, "readings": readings})


async def run_probe_json(args: argparse.Namespace) -> None:
    request = json.loads(args.json) if args.json else None
    if request is None:
        request = {"method": "get-prop", "params": args.props}
    if not isinstance(request, dict):
        raise PixcutBLEError("--json must be a JSON object")

    async with PixcutBLE(address=args.address, name=args.name, scan_seconds=args.scan_seconds) as pixcut:
        if args.subscribe_all:
            subscribed = await pixcut.start_notifications(NOTIFY_CHARS)
            await asyncio.sleep(1.0)
            await pixcut.stop_notifications(subscribed)
        for profile in args.profile:
            pixcut.notifications.clear()
            try:
                result = await pixcut.send_json_probe(profile, dict(request), args.wait_seconds)
            except Exception as exc:
                result = {"profile": profile, "error": str(exc), "notifications": pixcut.notifications.events}
            print_json(result)


async def run_probe_data(args: argparse.Namespace) -> None:
    data = bytes.fromhex(args.hex) if args.hex else args.data.encode("utf-8")
    async with PixcutBLE(address=args.address, name=args.name, scan_seconds=args.scan_seconds) as pixcut:
        result = await pixcut.send_data_probe(args.profile, args.job_id, data, args.wait_seconds)
        print_json(result)


async def run_handoff(args: argparse.Namespace) -> None:
    phone_mac = args.phone_mac or local_bluetooth_mac()
    if not phone_mac:
        raise PixcutBLEError("Could not determine local Bluetooth MAC; pass --phone-mac AA:BB:CC:DD:EE:FF")
    async with PixcutBLE(address=args.address, name=args.name, scan_seconds=args.scan_seconds) as pixcut:
        result = await pixcut.send_handoff(phone_mac, args.wait_seconds)
        print_json(result)


async def run_probe_hannto_json(args: argparse.Namespace) -> None:
    request = json.loads(args.json) if args.json else None
    if request is None:
        request = {"method": "get-prop", "params": args.props}
    if not isinstance(request, dict):
        raise PixcutBLEError("--json must be a JSON object")

    async with PixcutBLE(address=args.address, name=args.name, scan_seconds=args.scan_seconds) as pixcut:
        for write_label in args.write:
            pixcut.notifications.clear()
            try:
                result = await pixcut.send_hannto_json_probe(
                    write_label,
                    dict(request),
                    args.wait_seconds,
                    response=True if args.response else None,
                )
            except Exception as exc:
                result = {"write_label": write_label, "error": str(exc), "notifications": pixcut.notifications.events}
            print_json(result)


def common_ble_job_kwargs(args: argparse.Namespace) -> dict[str, int]:
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
            "message": "Add --execute to create the BLE job and send data to the printer.",
            **preview,
        }
    )
    return False


async def maybe_resume_after_upload(pixcut: PixcutBLE, args: argparse.Namespace, job_id: int) -> None:
    if args.resume_after_send:
        try:
            response = await pixcut.resume(args.write)
        except Exception as exc:
            print_json({"job_id": job_id, "resume_error": str(exc)})
        else:
            print_json({"job_id": job_id, "resume_response": response})
    if args.enable_print_after_send:
        try:
            response = await pixcut.set_prop_obj(args.write, {"print-function-enable": 1})
        except Exception as exc:
            print_json({"job_id": job_id, "set_prop_error": str(exc)})
        else:
            print_json({"job_id": job_id, "set_prop_response": response})


async def wait_after_send(pixcut: PixcutBLE, args: argparse.Namespace, job_id: int) -> None:
    if args.wait_after_send <= 0:
        return

    deadline = time.monotonic() + args.wait_after_send
    poll_index = 0
    saw_active = False
    while True:
        poll_index += 1
        try:
            response = await pixcut.get_props(args.write, STATUS_PROPS)
        except Exception as exc:
            print_json({"job_id": job_id, "poll": poll_index, "poll_error": str(exc)})
        else:
            printer_state, printer_sub_state, alerts = printer_status_tuple(response)
            if printer_state not in (None, "20"):
                saw_active = True
            print_json(
                {
                    "job_id": job_id,
                    "poll": poll_index,
                    "printer_state": printer_state,
                    "printer_sub_state": printer_sub_state,
                    "printer_state_alerts": alerts,
                    "props": response,
                }
            )
            if saw_active and printer_state == "20" and alerts in (None, "::0"):
                print_json({"job_id": job_id, "status": "idle_after_active"})
                return
            if alerts not in (None, "::0"):
                print_json({"job_id": job_id, "status": "alert", "printer_state_alerts": alerts})
                return

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(args.poll_interval, remaining))


async def run_get_prop(args: argparse.Namespace) -> None:
    async with PixcutBLE(address=args.address, name=args.name, scan_seconds=args.scan_seconds) as pixcut:
        print_json(await pixcut.get_props(args.write, args.props, args.wait_seconds))


async def run_get_job_info(args: argparse.Namespace) -> None:
    async with PixcutBLE(address=args.address, name=args.name, scan_seconds=args.scan_seconds) as pixcut:
        print_json(await pixcut.get_job_info(args.write, args.job_id, args.wait_seconds))


async def run_send_json(args: argparse.Namespace) -> None:
    obj = json.loads(args.json)
    if not isinstance(obj, dict):
        raise PixcutBLEError("json must be a JSON object")
    async with PixcutBLE(address=args.address, name=args.name, scan_seconds=args.scan_seconds) as pixcut:
        print_json(await pixcut.send_json_obj(args.write, obj, args.wait_seconds))


async def run_print(args: argparse.Namespace) -> None:
    image = Path(args.image).read_bytes()
    preview = {"kind": "print", "image_bytes": len(image), "write": args.write, "params": common_ble_job_kwargs(args)}
    if not require_execute(args, preview):
        return
    async with PixcutBLE(address=args.address, name=args.name, scan_seconds=args.scan_seconds) as pixcut:
        job_id, response = await pixcut.create_print_job(
            args.write,
            len(image),
            **common_ble_job_kwargs(args),
            wait_seconds=args.wait_seconds,
        )
        print_json({"job_id": job_id, "create_response": response})
        result = await pixcut.send_job_data(
            args.write,
            job_id,
            image,
            data_chunk_size=args.ble_data_chunk_size,
            chunk_delay=args.chunk_delay,
            wait_seconds=args.data_wait_seconds,
        )
        print_json({"job_id": job_id, "upload": result})
        await maybe_resume_after_upload(pixcut, args, job_id)
        await wait_after_send(pixcut, args, job_id)


async def run_cut(args: argparse.Namespace) -> None:
    plot = Path(args.plt).read_bytes()
    preview = {"kind": "cut", "plot_bytes": len(plot), "write": args.write, "params": common_ble_job_kwargs(args)}
    if not require_execute(args, preview):
        return
    async with PixcutBLE(address=args.address, name=args.name, scan_seconds=args.scan_seconds) as pixcut:
        job_id, response = await pixcut.create_cut_job(
            args.write,
            len(plot),
            **common_ble_job_kwargs(args),
            wait_seconds=args.wait_seconds,
        )
        print_json({"job_id": job_id, "create_response": response})
        result = await pixcut.send_job_data(
            args.write,
            job_id,
            plot,
            data_chunk_size=args.ble_data_chunk_size,
            chunk_delay=args.chunk_delay,
            wait_seconds=args.data_wait_seconds,
        )
        print_json({"job_id": job_id, "upload": result})
        await maybe_resume_after_upload(pixcut, args, job_id)
        await wait_after_send(pixcut, args, job_id)


async def run_print_cut(args: argparse.Namespace) -> None:
    image = Path(args.image).read_bytes()
    plot = Path(args.plt).read_bytes()
    preview = {
        "kind": "print-cut",
        "image_bytes": len(image),
        "plot_bytes": len(plot),
        "data_order": args.data_order,
        "write": args.write,
        "params": common_ble_job_kwargs(args),
    }
    if not require_execute(args, preview):
        return
    data = image + plot if args.data_order == "image-plot" else plot + image
    async with PixcutBLE(address=args.address, name=args.name, scan_seconds=args.scan_seconds) as pixcut:
        job_id, response = await pixcut.create_print_cut_job(
            args.write,
            len(image),
            len(plot),
            **common_ble_job_kwargs(args),
            wait_seconds=args.wait_seconds,
        )
        print_json({"job_id": job_id, "create_response": response})
        result = await pixcut.send_job_data(
            args.write,
            job_id,
            data,
            data_chunk_size=args.ble_data_chunk_size,
            chunk_delay=args.chunk_delay,
            wait_seconds=args.data_wait_seconds,
        )
        print_json({"job_id": job_id, "upload": result})
        await maybe_resume_after_upload(pixcut, args, job_id)
        await wait_after_send(pixcut, args, job_id)


def add_connect_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--address", help="macOS BLE peripheral UUID from a previous scan")
    parser.add_argument("--name", help=f"BLE device name prefix; default {PIXCUT_NAME_PREFIX!r}")
    parser.add_argument("--scan-seconds", type=float, default=10.0)


def add_hannto_args(parser: argparse.ArgumentParser) -> None:
    add_connect_args(parser)
    parser.add_argument("--write", choices=sorted(WRITE_CHARS), default="ff00-ff02")
    parser.add_argument("--wait-seconds", type=float, default=5.0)


def add_job_args(parser: argparse.ArgumentParser, default_job_type: int = DEFAULT_JOB_TYPE) -> None:
    add_hannto_args(parser)
    parser.add_argument("--execute", action="store_true", help="Actually create the job and send data")
    parser.add_argument("--channel", type=int, default=2)
    parser.add_argument("--copies", type=int, default=1)
    parser.add_argument("--media-size", default="4x7", help="Name or integer, e.g. 3x3, 4x6, 4x7, 5013")
    parser.add_argument("--media-type", default="sticker_4x7", help="Name or integer, e.g. photo, sticker_4x7, 2030")
    parser.add_argument("--job-type", type=int, default=default_job_type)
    parser.add_argument(
        "--ble-data-chunk-size",
        type=int,
        default=156,
        help="Job payload bytes per Hannto BLE frame. 156 keeps frames <=182-byte GATT writes.",
    )
    parser.add_argument("--chunk-delay", type=float, default=0.005)
    parser.add_argument("--data-wait-seconds", type=float, default=1.0)
    parser.add_argument("--wait-after-send", type=float, default=0.0)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--resume-after-send", action="store_true")
    parser.add_argument("--enable-print-after-send", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="Scan for PixCut BLE advertisements")
    scan.add_argument("--scan-seconds", type=float, default=10.0)
    scan.add_argument("--all", action="store_true", help="Show all discovered BLE devices")
    scan.set_defaults(func=run_scan)

    services = sub.add_parser("services", help="Connect and enumerate GATT services/characteristics")
    add_connect_args(services)
    services.set_defaults(func=run_services)

    read = sub.add_parser("read", help="Read safe/readable characteristics")
    add_connect_args(read)
    read.add_argument("characteristics", nargs="*", default=READABLE_CHARS)
    read.set_defaults(func=run_read)

    get_prop = sub.add_parser("get-prop", help="Read one or more device properties over Hannto BLE")
    add_hannto_args(get_prop)
    get_prop.add_argument("props", nargs="+")
    get_prop.set_defaults(func=run_get_prop)

    get_job_info = sub.add_parser("get-job-info", help="Read one job record over Hannto BLE")
    add_hannto_args(get_job_info)
    get_job_info.add_argument("job_id", type=int)
    get_job_info.set_defaults(func=run_get_job_info)

    send_json = sub.add_parser("send-json", help="Send a raw JSON command object over Hannto BLE")
    add_hannto_args(send_json)
    send_json.add_argument("json")
    send_json.set_defaults(func=run_send_json)

    probe_json = sub.add_parser("probe-json", help="Send USB-style JSON probes over BLE profiles")
    add_connect_args(probe_json)
    probe_json.add_argument("--profile", action="append", choices=sorted(PROFILES), default=None)
    probe_json.add_argument("--props", nargs="+", default=STATUS_PROPS)
    probe_json.add_argument("--json", help="Raw JSON command object to frame as USB-style cmd json")
    probe_json.add_argument("--wait-seconds", type=float, default=3.0)
    probe_json.add_argument("--subscribe-all", action="store_true", help="Briefly subscribe to every known notify char first")
    probe_json.set_defaults(func=run_probe_json)

    probe_data = sub.add_parser("probe-data", help="Send one USB-style data frame over a BLE profile")
    add_connect_args(probe_data)
    probe_data.add_argument("--profile", choices=sorted(PROFILES), default="ff00")
    probe_data.add_argument("--job-id", type=int, default=0)
    probe_data.add_argument("--data", default="")
    probe_data.add_argument("--hex", help="Data bytes as hex instead of --data text")
    probe_data.add_argument("--wait-seconds", type=float, default=3.0)
    probe_data.set_defaults(func=run_probe_data)

    handoff = sub.add_parser("handoff", help="Write the Android app's FE95 A_OUT_<phone-mac> BLE handoff")
    add_connect_args(handoff)
    handoff.add_argument("--phone-mac", help="Bluetooth MAC to advertise in A_OUT_; defaults to local Mac Bluetooth address")
    handoff.add_argument("--wait-seconds", type=float, default=5.0)
    handoff.set_defaults(func=run_handoff)

    probe_hannto = sub.add_parser("probe-hannto-json", help="Send Android-style Hannto framed JSON over BLE write chars")
    add_connect_args(probe_hannto)
    probe_hannto.add_argument("--write", action="append", choices=sorted(WRITE_CHARS), default=None)
    probe_hannto.add_argument("--props", nargs="+", default=STATUS_PROPS)
    probe_hannto.add_argument("--json", help="Raw JSON command object to frame as Hannto payload")
    probe_hannto.add_argument("--wait-seconds", type=float, default=3.0)
    probe_hannto.add_argument("--response", action="store_true", help="Force write-with-response")
    probe_hannto.set_defaults(func=run_probe_hannto_json)

    print_job = sub.add_parser("print", help="Create a BLE print job and send an image file")
    print_job.add_argument("image")
    add_job_args(print_job)
    print_job.set_defaults(func=run_print)

    cut_job = sub.add_parser("cut", help="Create a BLE cut job and send a PLT file")
    cut_job.add_argument("plt")
    add_job_args(cut_job)
    cut_job.set_defaults(func=run_cut, media_size="3x3", media_type="sticker_3x3")

    combo = sub.add_parser("print-cut", help="Create a BLE print+cut job and send combined image/PLT data")
    combo.add_argument("image")
    combo.add_argument("plt")
    add_job_args(combo, default_job_type=DEFAULT_PRINT_CUT_JOB_TYPE)
    combo.add_argument(
        "--data-order",
        choices=("plot-image", "image-plot"),
        default="plot-image",
        help="Combined upload byte order. The official app sends PLT bytes followed by image bytes.",
    )
    combo.set_defaults(func=run_print_cut)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args, "profile", None) is None:
        args.profile = ["ff00", "mldp"]
    if getattr(args, "write", None) is None:
        args.write = list(HANNTO_JSON_PROFILES)
    try:
        asyncio.run(args.func(args))
    except (PixcutBLEError, PixcutError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
