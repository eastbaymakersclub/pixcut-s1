"""BLE transport regressions; all connections and notifications are simulated."""

import argparse
import asyncio
import contextlib
import io
import itertools
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pixcut_ble as ble


class FakeClient:
    def __init__(self, *, write_label="ff00-ff02", max_write_size=182, missing=()):
        self.char = SimpleNamespace(
            uuid=ble.WRITE_CHARS[write_label],
            properties=["write-without-response"],
            max_write_without_response_size=max_write_size,
        )
        self.services = [SimpleNamespace(characteristics=[self.char])]
        self.callbacks = {}
        self.missing = set(missing)
        self.writes = []
        self.on_write = lambda data: None

    async def start_notify(self, uuid, callback):
        if uuid in self.missing:
            raise RuntimeError("notification unavailable")
        self.callbacks[uuid] = callback

    async def stop_notify(self, uuid):
        self.callbacks.pop(uuid, None)

    async def write_gatt_char(self, uuid, data, response):
        self.writes.append(bytes(data))
        self.on_write(bytes(data))

    def notify(self, label, data):
        self.callbacks[ble.NOTIFY_CHARS[label]](self.char, bytearray(data))

    def ack(self):
        self.notify("ff00-status", b"\x01\x01")

    def json_notification(self, value):
        for frame in ble.build_hannto_packets(json.dumps(value).encode()):
            self.notify("ff00-data", frame)


def device_error(log):
    frame = ble.build_hannto_packets(json.dumps({
        "method": "event.rpt_err", "params": {"code": -8013, "message": "job timeout"},
    }).encode())[0]
    log.callback("ff00-data")("fake", bytearray(frame))


class UploadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.pixcut = ble.PixcutBLE(address="fake")
        self.client = FakeClient()
        self.pixcut.client = self.client

    async def upload(self, data=b"x" * 400, **kwargs):
        with contextlib.redirect_stderr(io.StringIO()):
            return await self.pixcut.send_job_data(
                "ff00-ff02", 20, data, chunk_delay=0, wait_seconds=0, **kwargs,
            )

    async def test_delayed_ack_gates_each_att_write(self):
        # Force multiple ATT writes per Hannto packet as well as multiple packets.
        self.client.char.max_write_without_response_size = 80
        unacknowledged = False

        def on_write(data):
            nonlocal unacknowledged
            self.assertFalse(unacknowledged, "sent the next ATT write before its ACK")
            unacknowledged = True

            def acknowledge():
                nonlocal unacknowledged
                unacknowledged = False
                self.client.ack()

            asyncio.get_running_loop().call_later(0.002, acknowledge)

        self.client.on_write = on_write
        result = await self.upload()
        self.assertEqual(result["data_bytes"], 400)
        self.assertEqual(result["frame_count"], 3)
        self.assertEqual([len(x) for x in self.client.writes], [80, 80, 22, 80, 80, 22, 80, 34])
        self.assertFalse(unacknowledged)
        self.assertFalse(self.client.callbacks)

    async def test_inline_ack_during_write_is_not_lost(self):
        self.client.on_write = lambda data: self.client.ack()
        await self.upload()
        self.assertEqual(len(self.client.writes), 3)
        self.assertIsNone(self.pixcut.notifications.pending_ack)

    async def test_wrong_ack_does_not_advance_or_resend(self):
        self.client.on_write = lambda data: self.client.notify("ff00-data", b"\x01\x01")
        with self.assertRaisesRegex(ble.PixcutBLEError, "without resending"):
            await self.upload(ack_timeout=0.01)
        self.assertEqual(len(self.client.writes), 1)
        self.assertFalse(self.client.callbacks)
        self.assertIsNone(self.pixcut.notifications.pending_ack)

    async def test_device_error_interrupts_pending_ack_and_survives_clear(self):
        self.client.on_write = lambda data: device_error(self.pixcut.notifications)
        with self.assertRaisesRegex(ble.PixcutBLEDeviceError, "-8013"):
            await asyncio.wait_for(self.upload(), timeout=1)
        self.assertEqual(len(self.client.writes), 1)
        self.pixcut.notifications.clear()
        with self.assertRaises(ble.PixcutBLEDeviceError):
            await self.pixcut.get_props("ff00-ff02", ble.STATUS_PROPS, wait_seconds=0)
        self.assertEqual(len(self.client.writes), 1)

    async def test_missing_subscription_prevents_upload(self):
        for label in ("ff00-status", "ff00-data"):
            with self.subTest(label=label):
                self.client.missing = {ble.NOTIFY_CHARS[label]}
                with contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(ble.PixcutBLEError, "requires ff00-status and ff00-data"):
                        await self.upload()
                self.assertEqual(self.client.writes, [])
                self.assertFalse(self.client.callbacks)

    async def test_explicit_experimental_profile_does_not_wait_for_ff00_ack(self):
        self.client = FakeClient(write_label="mldp-8841")
        self.pixcut.client = self.client
        with contextlib.redirect_stderr(io.StringIO()):
            result = await self.pixcut.send_job_data(
                "mldp-8841", 20, b"x", chunk_delay=0, wait_seconds=0, ack_timeout=0.001,
            )
        self.assertEqual(result["data_bytes"], 1)
        self.assertEqual(len(self.client.writes), 1)

    async def test_json_command_does_not_require_write_ack(self):
        def respond(data):
            request = ble.parse_hannto_frame(data)["json"]
            self.client.json_notification({"id": request["id"], "result": ["20", "2000", "::0"]})

        self.client.on_write = respond
        result = await self.pixcut.get_props("ff00-ff02", ble.STATUS_PROPS, wait_seconds=0)
        self.assertEqual(result["result"], ["20", "2000", "::0"])
        self.assertEqual(len(self.client.writes), 1)

    async def test_dry_run_never_opens_a_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            image, plot = Path(directory) / "print.jpg", Path(directory) / "cut.plt"
            image.write_bytes(b"image")
            plot.write_bytes(b"plot")
            args = ble.build_parser().parse_args(["print-cut", str(image), str(plot)])
            with patch.object(ble, "PixcutBLE", side_effect=AssertionError("dry run connected")):
                with patch.object(ble, "print_json") as output:
                    await args.func(args)
            self.assertTrue(output.call_args.args[0]["dry_run"])


class StatusTests(unittest.IsolatedAsyncioTestCase):
    async def poll(self, responses, *, wait_after_send=1):
        log = ble.NotificationLog()
        calls = []
        outcomes = iter(responses)

        async def get_props(write_label, props):
            calls.append((write_label, props))
            value = next(outcomes)
            if isinstance(value, Exception):
                raise value
            if callable(value):
                value = value(log)
            return {"result": value}

        pixcut = SimpleNamespace(notifications=log, get_props=get_props)
        args = argparse.Namespace(write="ff00-ff02", wait_after_send=wait_after_send, poll_interval=0)
        with patch.object(ble, "print_json") as output:
            await ble.wait_after_send(pixcut, args, job_id=20)
        return calls, [call.args[0] for call in output.call_args_list]

    async def test_status_loss_after_active_recovers_to_confirmed_idle(self):
        calls, output = await self.poll([
            ["40", "3015", "::0"], ble.PixcutBLEError("status response missing"),
            ["40", "3030", "::0"], ["20", "2000", "::0"],
        ])
        self.assertEqual(len(calls), 4)
        self.assertTrue(any("poll_error" in item for item in output))
        self.assertEqual(output[-1]["status"], "idle_after_active")
        self.assertTrue(all(item["job_id"] == 20 for item in output))

    async def test_paper_load_resumes_polling_same_job(self):
        calls, output = await self.poll([
            ["60", "3015", "::5401"], ["60", "3015", "::5401"],
            ["40", "3021", "::0"], ["20", "2000", "::0"],
        ])
        self.assertEqual(len(calls), 4)
        self.assertEqual(sum(item.get("status") == "waiting_for_media" for item in output), 2)
        self.assertEqual(output[-1]["status"], "idle_after_active")
        self.assertTrue(all(item["job_id"] == 20 for item in output))

    async def test_device_error_followed_by_idle_is_not_success(self):
        def error_then_idle(log):
            device_error(log)
            log.clear()
            return ["20", "2000", "::0"]

        with self.assertRaises(ble.PixcutBLEDeviceError):
            await self.poll([["40", "3015", "::0"], error_then_idle])

    async def test_paper_wait_then_idle_without_observed_resume_is_not_complete(self):
        calls, output = await self.poll(itertools.chain(
            [["40", "3015", "::0"], ["60", "3015", "::5401"]],
            itertools.repeat(["20", "2000", "::0"]),
        ), wait_after_send=0.01)
        self.assertGreater(len(calls), 2)
        self.assertFalse(any(item.get("status") == "idle_after_active" for item in output))
        self.assertEqual(output[-1]["status"], "timeout")
        self.assertFalse(output[-1]["saw_active"])

    async def test_repeated_poll_loss_reaches_inconclusive_deadline(self):
        calls = 0

        async def get_props(write_label, props):
            nonlocal calls
            calls += 1
            if calls == 1:
                return {"result": ["40", "3015", "::0"]}
            raise ble.PixcutBLEError("status response missing")

        pixcut = SimpleNamespace(notifications=ble.NotificationLog(), get_props=get_props)
        args = argparse.Namespace(write="ff00-ff02", wait_after_send=0.015, poll_interval=0.002)
        with patch.object(ble, "print_json") as output:
            await ble.wait_after_send(pixcut, args, job_id=20)
        self.assertGreater(calls, 2)
        self.assertEqual(output.call_args.args[0]["status"], "timeout")
        self.assertTrue(output.call_args.args[0]["saw_active"])


if __name__ == "__main__":
    unittest.main()
