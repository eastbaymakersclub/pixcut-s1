import assert from "node:assert/strict";
import test from "node:test";

import {
  DEFAULT_PRINT_CUT_OPTIONS,
  PixcutWebBluetoothClient,
  buildComboJobRequest,
  buildCombinedPrintCutPayload,
  buildHanntoFrame,
  buildHanntoPackets,
  extractHanntoFrames,
  extractJobId,
  hanntoChecksum,
  notificationSummary,
  parseHanntoFrame,
  printerStatusTuple,
} from "../web-bluetooth/pixcut-web-bluetooth.mjs";

const encoder = new TextEncoder();

test("builds Hannto JSON frames with little-endian fields and checksum", () => {
  const body = encoder.encode(JSON.stringify({ method: "get-prop", params: ["printer-state"], id: 123 }));
  const frame = buildHanntoFrame(body, { msgSn: 7, packageTotal: 1, packageNum: 1 });
  const parsed = parseHanntoFrame(frame);

  assert.equal(frame[0], 0x7e);
  assert.equal(frame.at(-1), 0x7e);
  assert.equal(parsed.version, 100);
  assert.equal(parsed.connectType, 1);
  assert.equal(parsed.interactive, 6);
  assert.equal(parsed.encoding, 3);
  assert.equal(parsed.msgSn, 7);
  assert.equal(parsed.body.length, body.length);
  assert.equal(parsed.checksum, hanntoChecksum(frame));
  assert.equal(parsed.checksumOk, true);
  assert.deepEqual(parsed.json, { method: "get-prop", params: ["printer-state"], id: 123 });
});

test("keeps job data frames inside the observed 182-byte BLE write size", () => {
  const payload = new Uint8Array(312);
  payload.fill(0xaa);
  const frames = buildHanntoPackets(payload, {
    jobId: 66,
    dataChunkSize: 156,
    connectType: 2,
    interactive: 6,
    encoding: 2,
    splited: true,
  });

  assert.equal(frames.length, 2);
  for (const frame of frames) {
    assert.equal(frame.length, 182);
    const parsed = parseHanntoFrame(frame);
    assert.equal(parsed.connectType, 2);
    assert.equal(parsed.encoding, 2);
    assert.equal(parsed.splited, true);
    assert.equal(new DataView(parsed.body.buffer, parsed.body.byteOffset, parsed.body.byteLength).getInt32(0, true), 66);
    assert.equal(parsed.body.length, 160);
  }
});

test("parses complete frames from a noisy notification buffer", () => {
  const first = buildHanntoFrame(encoder.encode(JSON.stringify({ result: ["20", "2000", "::0"], id: 11 })));
  const second = buildHanntoFrame(encoder.encode(JSON.stringify({ result: { "job-id": 66 }, id: 12 })));
  const noisy = new Uint8Array(3 + first.length + 2 + second.length);
  noisy.set([0, 1, 2], 0);
  noisy.set(first, 3);
  noisy.set([3, 4], 3 + first.length);
  noisy.set(second, 3 + first.length + 2);

  const { frames, remainder } = extractHanntoFrames(noisy);
  assert.equal(remainder.length, 0);
  assert.equal(frames.length, 2);
  assert.deepEqual(frames[0].json.result, ["20", "2000", "::0"]);
  assert.equal(frames[1].json.result["job-id"], 66);
});

test("builds combo job JSON using PixCut S1 defaults", () => {
  const request = buildComboJobRequest({ imageBytes: 64180, plotBytes: 160, id: 777 });

  assert.equal(DEFAULT_PRINT_CUT_OPTIONS.ackDataWrites, true);
  assert.equal(DEFAULT_PRINT_CUT_OPTIONS.chunkDelayMs, 0);
  assert.equal(request.method, "combo-job");
  assert.equal(request.id, 777);
  assert.equal(request.params[0].method, "print-job");
  assert.equal(request.params[0].params["file-size"], 64180);
  assert.equal(request.params[0].params["media-size"], 5013);
  assert.equal(request.params[0].params["media-type"], 2030);
  assert.equal(request.params[0].params["job-type"], 600);
  assert.equal(request.params[1].method, "cut-job");
  assert.equal(request.params[1].params["file-size"], 160);
  assert.equal(request.params[1].params["job-type"], 600);
});

test("orders combined print-cut upload as PLT bytes followed by image bytes", () => {
  const image = new Uint8Array([0xff, 0xd8, 0xff, 0x00]);
  const plot = encoder.encode("IN VER0.1.0 KP42 @ ");
  const payload = buildCombinedPrintCutPayload(image, plot, "plot-image");

  assert.deepEqual(Array.from(payload.slice(0, plot.length)), Array.from(plot));
  assert.deepEqual(Array.from(payload.slice(plot.length)), Array.from(image));
});

test("extracts job ids and printer status responses", () => {
  assert.equal(extractJobId({ result: { "job-id": 66 }, id: 1 }), 66);
  assert.deepEqual(printerStatusTuple({ result: ["40", "3030", "::0"], id: 2 }), {
    printerState: "40",
    printerSubState: "3030",
    printerStateAlerts: "::0",
  });
});

test("treats ff00 status 0101 notifications as write ACKs", async () => {
  const client = new PixcutWebBluetoothClient({ bluetooth: {}, log: () => {} });
  const ack = client.waitForStatusAck(1000);

  client.handleNotification("ff00-status", new Uint8Array([0x01, 0x01]));

  await ack;
});

test("surfaces PixCut event.rpt_err notifications as device errors", async () => {
  const client = new PixcutWebBluetoothClient({ bluetooth: {}, log: () => {} });
  const errorFrame = buildHanntoFrame(
    encoder.encode(JSON.stringify({ method: "event.rpt_err", params: { code: -8013, message: "job timeout" } }))
  );

  client.handleNotification("ff00-data", errorFrame);

  await assert.rejects(
    () => client.waitForStatusAck(1),
    /PixCut reported code -8013: job timeout/
  );
});

test("caps notification summary payload fingerprints", () => {
  const events = Array.from({ length: 20 }, (_, index) => ({
    label: "ff00-data",
    raw: new Uint8Array([index]),
    hex: index.toString(16).padStart(2, "0"),
  }));
  const summary = notificationSummary(events);

  assert.equal(Object.keys(summary.byPayload).length, 16);
  assert.equal(summary.byPayloadOmitted, 4);
});

test("does not fail a print wait when status polling times out after active state", async () => {
  const client = new PixcutWebBluetoothClient({ bluetooth: {}, log: () => {} });
  let calls = 0;

  client.getStatus = async () => {
    calls += 1;
    if (calls === 1) {
      return { result: ["40", "3016", "::0"], id: 1 };
    }
    throw new Error("status request lost");
  };

  const result = await client.waitForIdleAfterActive(70, {
    waitAfterSendMs: 1000,
    pollIntervalMs: 1,
  });

  assert.equal(result.status, "status_poll_timeout_after_active");
  assert.equal(result.error, "status request lost");
  assert.deepEqual(result.response, { result: ["40", "3016", "::0"], id: 1 });
});
