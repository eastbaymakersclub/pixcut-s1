const textEncoder = new TextEncoder();
const textDecoder = new TextDecoder();

export const PIXCUT_NAME_PREFIX = "Liene PixCut S1";

export const UUIDS = Object.freeze({
  advService: "0000fc00-0000-1000-8000-00805f9b34fb",
  ff00Service: "0000ff00-0000-1000-8000-00805f9b34fb",
  ff00Write: "0000ff02-0000-1000-8000-00805f9b34fb",
  ff00StatusNotify: "0000ff03-0000-1000-8000-00805f9b34fb",
  ff00DataNotify: "0000ff01-0000-1000-8000-00805f9b34fb",
});

export const MEDIA_SIZES = Object.freeze({
  "3x3": 5009,
  "4x6": 5012,
  "4x7": 5013,
});

export const MEDIA_TYPES = Object.freeze({
  photo: 2010,
  sticker_3x3: 2020,
  sticker_4x7: 2030,
});

export const STATUS_PROPS = Object.freeze(["printer-state", "printer-sub-state", "printer-state-alerts"]);

export const DEFAULT_PRINT_CUT_OPTIONS = Object.freeze({
  channel: 2,
  copies: 1,
  mediaSize: MEDIA_SIZES["4x7"],
  mediaType: MEDIA_TYPES.sticker_4x7,
  jobType: 600,
  dataChunkSize: 156,
  chunkDelayMs: 5,
  dataOrder: "plot-image",
  waitAfterSendMs: 360000,
  pollIntervalMs: 2000,
});

export class PixcutWebBluetoothError extends Error {}

export function toUint8Array(value) {
  if (value instanceof Uint8Array) {
    return value;
  }
  if (value instanceof ArrayBuffer) {
    return new Uint8Array(value);
  }
  if (ArrayBuffer.isView(value)) {
    return new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
  }
  throw new TypeError("Expected Uint8Array, ArrayBuffer, or ArrayBuffer view");
}

export function concatUint8Arrays(parts) {
  const arrays = parts.map(toUint8Array);
  const total = arrays.reduce((sum, part) => sum + part.length, 0);
  const result = new Uint8Array(total);
  let offset = 0;
  for (const part of arrays) {
    result.set(part, offset);
    offset += part.length;
  }
  return result;
}

export function hex(bytes, limit = Number.POSITIVE_INFINITY) {
  const view = toUint8Array(bytes);
  const end = Math.min(view.length, limit);
  let value = "";
  for (let index = 0; index < end; index += 1) {
    value += view[index].toString(16).padStart(2, "0");
  }
  return end < view.length ? `${value}...(${view.length} bytes)` : value;
}

export function hanntoChecksum(frame) {
  const bytes = toUint8Array(frame);
  let sum = 0;
  for (let index = 1; index < bytes.length - 2; index += 1) {
    sum = (sum + bytes[index]) & 0xff;
  }
  return sum;
}

export function hanntoAttribute({ splited = false, encryptType = 0, bodyLen }) {
  if (!Number.isInteger(bodyLen) || bodyLen < 0 || bodyLen > 0x3ff) {
    throw new PixcutWebBluetoothError(`Hannto BLE frame body is too large: ${bodyLen} bytes`);
  }
  return (splited ? 0x2000 : 0) | ((encryptType & 0x07) << 10) | bodyLen;
}

export function buildHanntoFrame(bodyBytes, options = {}) {
  const body = toUint8Array(bodyBytes);
  const {
    version = 100,
    reserve = 0,
    connectType = 1,
    interactive = 6,
    encoding = 3,
    terminalId = 0,
    msgSn = 1,
    packageTotal = 1,
    packageNum = 1,
    splited = false,
    encryptType = 0,
  } = options;

  const frame = new Uint8Array(body.length + 22);
  const view = new DataView(frame.buffer);
  const attr = hanntoAttribute({ splited, encryptType, bodyLen: body.length });

  frame[0] = 0x7e;
  frame[1] = version & 0xff;
  frame[2] = reserve & 0xff;
  frame[3] = connectType & 0xff;
  frame[4] = interactive & 0xff;
  frame[5] = encoding & 0xff;
  view.setUint32(6, terminalId >>> 0, true);
  view.setUint32(10, msgSn >>> 0, true);
  view.setUint16(14, packageTotal & 0xffff, true);
  view.setUint16(16, packageNum & 0xffff, true);
  view.setUint16(18, attr, true);
  frame.set(body, 20);
  frame[frame.length - 2] = hanntoChecksum(frame);
  frame[frame.length - 1] = 0x7e;
  return frame;
}

export function buildHanntoPackets(dataBytes, options = {}) {
  const data = toUint8Array(dataBytes);
  const {
    jobId = -1,
    dataChunkSize = undefined,
    version = 100,
    connectType = 1,
    interactive = 6,
    encoding = 3,
    splited = false,
    encryptType = 0,
  } = options;

  const maxChunkSize = jobId !== -1 ? 892 : 896;
  const chunkSize = dataChunkSize ?? maxChunkSize;
  if (!Number.isInteger(chunkSize) || chunkSize <= 0 || chunkSize > maxChunkSize) {
    throw new PixcutWebBluetoothError(`dataChunkSize must be 1..${maxChunkSize}, got ${chunkSize}`);
  }

  const chunks = [];
  if (data.length === 0) {
    chunks.push(new Uint8Array(0));
  } else {
    for (let offset = 0; offset < data.length; offset += chunkSize) {
      chunks.push(data.slice(offset, offset + chunkSize));
    }
  }

  return chunks.map((chunk, index) => {
    let body = chunk;
    if (jobId !== -1) {
      const withJobId = new Uint8Array(chunk.length + 4);
      new DataView(withJobId.buffer).setInt32(0, jobId, true);
      withJobId.set(chunk, 4);
      body = withJobId;
    }
    return buildHanntoFrame(body, {
      version,
      connectType,
      interactive,
      encoding,
      msgSn: index + 1,
      packageTotal: chunks.length,
      packageNum: index + 1,
      splited,
      encryptType,
    });
  });
}

export function buildJsonRequestFrame(request) {
  return buildHanntoPackets(textEncoder.encode(JSON.stringify(request)))[0];
}

export function parseJsonBytes(bytes) {
  const text = textDecoder.decode(toUint8Array(bytes)).trim();
  if (!text) {
    return null;
  }
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

export function parseHanntoFrame(frameBytes) {
  const frame = toUint8Array(frameBytes);
  if (frame.length < 22 || frame[0] !== 0x7e || frame[frame.length - 1] !== 0x7e) {
    throw new PixcutWebBluetoothError("Invalid Hannto frame boundary");
  }
  const view = new DataView(frame.buffer, frame.byteOffset, frame.byteLength);
  const attr = view.getUint16(18, true);
  const bodyLen = attr & 0x03ff;
  const body = frame.slice(20, 20 + bodyLen);
  const parsed = {
    len: frame.length,
    version: frame[1],
    reserve: frame[2],
    connectType: frame[3],
    interactive: frame[4],
    encoding: frame[5],
    terminalId: view.getUint32(6, true),
    msgSn: view.getUint32(10, true),
    packageTotal: view.getUint16(14, true),
    packageNum: view.getUint16(16, true),
    splited: Boolean(attr & 0x2000),
    encryptType: (attr >> 10) & 0x07,
    body,
    bodyHex: hex(body, 80),
    checksum: frame[frame.length - 2],
    checksumOk: hanntoChecksum(frame) === frame[frame.length - 2],
  };
  const json = parseJsonBytes(body);
  if (json !== null) {
    parsed.json = json;
  }
  return parsed;
}

export function extractHanntoFrames(bufferBytes) {
  let buffer = toUint8Array(bufferBytes);
  const frames = [];

  while (buffer.length > 0) {
    const start = buffer.indexOf(0x7e);
    if (start < 0) {
      return { frames, remainder: new Uint8Array(0) };
    }
    if (start > 0) {
      buffer = buffer.slice(start);
    }
    if (buffer.length < 22) {
      return { frames, remainder: buffer };
    }
    if (buffer[1] !== 0x01 && buffer[1] !== 0x64) {
      buffer = buffer.slice(1);
      continue;
    }
    const attr = new DataView(buffer.buffer, buffer.byteOffset, buffer.byteLength).getUint16(18, true);
    const bodyLen = attr & 0x03ff;
    const frameLen = bodyLen + 22;
    if (bodyLen > 992) {
      buffer = buffer.slice(3);
      continue;
    }
    if (buffer.length < frameLen) {
      return { frames, remainder: buffer };
    }
    if (buffer[frameLen - 1] !== 0x7e) {
      buffer = buffer.slice(3);
      continue;
    }
    frames.push(parseHanntoFrame(buffer.slice(0, frameLen)));
    buffer = buffer.slice(frameLen);
  }

  return { frames, remainder: buffer };
}

export function partialHanntoBodyPrefix(bytes) {
  const raw = toUint8Array(bytes);
  if (raw.length <= 20 || raw[0] !== 0x7e) {
    return null;
  }
  const declaredBodyLen = new DataView(raw.buffer, raw.byteOffset, raw.byteLength).getUint16(18, true) & 0x03ff;
  let body = raw.slice(20);
  if (body.length >= 2 && body[body.length - 1] === 0x7e) {
    body = body.slice(0, -2);
  }
  return {
    declaredBodyLen,
    receivedBodyPrefixLen: body.length,
    textPrefix: textDecoder.decode(body).slice(0, 220),
  };
}

export function notificationSummary(events) {
  const byLabel = {};
  const byPayload = {};
  const decodedJson = [];
  const partialHanntoBodies = [];
  for (const event of events) {
    byLabel[event.label] = (byLabel[event.label] ?? 0) + 1;
    if (event.hex) {
      const payloadKey = `${event.label}:${event.hex.length > 96 ? `${event.hex.slice(0, 96)}...` : event.hex}`;
      byPayload[payloadKey] = (byPayload[payloadKey] ?? 0) + 1;
    }
    for (const frame of event.frames ?? []) {
      if (frame.json) {
        decodedJson.push(frame.json);
      }
    }
    const partial = partialHanntoBodyPrefix(event.raw);
    if (partial?.textPrefix) {
      partialHanntoBodies.push({ label: event.label, ...partial });
    }
  }
  return {
    count: events.length,
    byLabel,
    byPayload,
    decodedJson: decodedJson.slice(0, 5),
    partialHanntoBodies: partialHanntoBodies.slice(0, 5),
  };
}

export function responseResult(value) {
  return value && typeof value === "object" ? value.result : undefined;
}

export function printerStatusTuple(value) {
  const result = responseResult(value);
  if (Array.isArray(result) && result.length >= 3) {
    return {
      printerState: String(result[0]),
      printerSubState: String(result[1]),
      printerStateAlerts: String(result[2]),
    };
  }
  return {
    printerState: null,
    printerSubState: null,
    printerStateAlerts: null,
  };
}

export function extractJobId(response) {
  const result = responseResult(response);
  if (result && typeof result === "object" && !Array.isArray(result) && Number.isInteger(result["job-id"])) {
    return result["job-id"];
  }
  if (Array.isArray(result)) {
    for (const item of result) {
      if (item && typeof item === "object" && Number.isInteger(item["job-id"])) {
        return item["job-id"];
      }
    }
  }
  throw new PixcutWebBluetoothError(`Could not find job-id in response: ${JSON.stringify(response)}`);
}

export function buildComboJobRequest({ imageBytes, plotBytes, id, options = {} }) {
  const merged = { ...DEFAULT_PRINT_CUT_OPTIONS, ...options };
  return {
    method: "combo-job",
    params: [
      {
        method: "print-job",
        params: {
          channel: merged.channel,
          copies: merged.copies,
          "file-size": imageBytes,
          "media-size": merged.mediaSize,
          "media-type": merged.mediaType,
          "job-type": merged.jobType,
        },
      },
      {
        method: "cut-job",
        params: {
          "file-size": plotBytes,
          "job-type": merged.jobType,
        },
      },
    ],
    id,
  };
}

export function buildCombinedPrintCutPayload(imageBytes, plotBytes, dataOrder = "plot-image") {
  if (dataOrder === "image-plot") {
    return concatUint8Arrays([imageBytes, plotBytes]);
  }
  if (dataOrder !== "plot-image") {
    throw new PixcutWebBluetoothError(`Unknown dataOrder: ${dataOrder}`);
  }
  return concatUint8Arrays([plotBytes, imageBytes]);
}

export function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export class PixcutWebBluetoothClient {
  constructor({ bluetooth = globalThis.navigator?.bluetooth, log = () => {} } = {}) {
    this.bluetooth = bluetooth;
    this.log = log;
    this.device = null;
    this.server = null;
    this.service = null;
    this.writeCharacteristic = null;
    this.statusCharacteristic = null;
    this.dataCharacteristic = null;
    this.nextId = Math.floor((globalThis.performance?.now?.() ?? Date.now()) * 1000) % 900000 + 1000;
    this.pending = new Map();
    this.events = [];
    this.dataBuffer = new Uint8Array(0);
  }

  get connected() {
    return Boolean(this.device?.gatt?.connected && this.writeCharacteristic);
  }

  async requestDevice() {
    if (!this.bluetooth) {
      throw new PixcutWebBluetoothError("Web Bluetooth is not available in this browser context");
    }
    this.device = await this.bluetooth.requestDevice({
      filters: [{ namePrefix: PIXCUT_NAME_PREFIX }],
      optionalServices: [UUIDS.ff00Service],
    });
    this.device.addEventListener?.("gattserverdisconnected", () => {
      this.log("Disconnected");
    });
    return this.device;
  }

  async connect() {
    if (!this.device) {
      await this.requestDevice();
    }
    this.server = await this.device.gatt.connect();
    this.service = await this.server.getPrimaryService(UUIDS.ff00Service);
    this.writeCharacteristic = await this.service.getCharacteristic(UUIDS.ff00Write);
    this.statusCharacteristic = await this.service.getCharacteristic(UUIDS.ff00StatusNotify);
    this.dataCharacteristic = await this.service.getCharacteristic(UUIDS.ff00DataNotify);

    await this.statusCharacteristic.startNotifications();
    await this.dataCharacteristic.startNotifications();
    this.statusCharacteristic.addEventListener("characteristicvaluechanged", (event) =>
      this.handleNotification("ff00-status", event.target.value)
    );
    this.dataCharacteristic.addEventListener("characteristicvaluechanged", (event) =>
      this.handleNotification("ff00-data", event.target.value)
    );

    this.log(`Connected to ${this.device.name || "PixCut S1"}`);
    return this;
  }

  async requestAndConnect() {
    await this.requestDevice();
    return this.connect();
  }

  handleNotification(label, value) {
    const raw = toUint8Array(value);
    const event = { label, raw, hex: hex(raw) };
    if (label === "ff00-data") {
      this.dataBuffer = concatUint8Arrays([this.dataBuffer, raw]);
      const parsed = extractHanntoFrames(this.dataBuffer);
      this.dataBuffer = parsed.remainder;
      event.frames = parsed.frames;
      for (const frame of parsed.frames) {
        if (frame.json && typeof frame.json === "object") {
          this.resolvePending(frame.json);
        }
      }
    }
    this.events.push(event);
    if (label === "ff00-data" && !event.frames?.length) {
      const partial = partialHanntoBodyPrefix(raw);
      if (partial?.textPrefix) {
        this.log(`BLE ${label} fragment: ${partial.textPrefix}`);
      }
    }
  }

  resolvePending(response) {
    const requestId = response.id;
    if (!this.pending.has(requestId)) {
      return;
    }
    const pending = this.pending.get(requestId);
    clearTimeout(pending.timer);
    this.pending.delete(requestId);
    pending.resolve(response);
  }

  async writeFrame(frame) {
    if (!this.writeCharacteristic) {
      throw new PixcutWebBluetoothError("Not connected");
    }
    if (typeof this.writeCharacteristic.writeValueWithoutResponse === "function") {
      await this.writeCharacteristic.writeValueWithoutResponse(frame);
      return;
    }
    await this.writeCharacteristic.writeValue(frame);
  }

  async sendJsonObj(request, waitSeconds = 5) {
    const obj = { ...request };
    if (obj.id === undefined || obj.id === null) {
      obj.id = this.nextId;
      this.nextId += 1;
    }
    const frame = buildJsonRequestFrame(obj);
    const responsePromise = new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(obj.id);
        reject(
          new PixcutWebBluetoothError(
            `Timed out waiting for BLE JSON response id ${obj.id}: ${JSON.stringify(
              notificationSummary(this.events)
            )}`
          )
        );
      }, waitSeconds * 1000);
      this.pending.set(obj.id, { resolve, reject, timer });
    });
    await this.writeFrame(frame);
    const response = await responsePromise;
    if (response.error || response.result?.["error-code"] !== undefined) {
      throw new PixcutWebBluetoothError(`Device returned error: ${JSON.stringify(response)}`);
    }
    return response;
  }

  async sendJson(method, params = undefined, waitSeconds = 5) {
    const request = { method };
    if (params !== undefined) {
      request.params = params;
    }
    return this.sendJsonObj(request, waitSeconds);
  }

  async getStatus(waitSeconds = 5) {
    return this.sendJson("get-prop", STATUS_PROPS, waitSeconds);
  }

  async createPrintCutJob(imageBytes, plotBytes, options = {}) {
    const request = buildComboJobRequest({
      imageBytes,
      plotBytes,
      id: this.nextId,
      options,
    });
    this.nextId += 1;
    const response = await this.sendJsonObj(request, options.waitSeconds ?? 5);
    return { jobId: extractJobId(response), response };
  }

  async sendJobData(jobId, dataBytes, options = {}) {
    const merged = { ...DEFAULT_PRINT_CUT_OPTIONS, ...options };
    const data = toUint8Array(dataBytes);
    const frames = buildHanntoPackets(data, {
      jobId,
      dataChunkSize: merged.dataChunkSize,
      connectType: 2,
      interactive: 6,
      encoding: 2,
      splited: true,
    });
    let sent = 0;
    for (let index = 0; index < frames.length; index += 1) {
      await this.writeFrame(frames[index]);
      sent = Math.min(sent + Math.min(merged.dataChunkSize, data.length - sent), data.length);
      merged.onProgress?.({ sent, total: data.length, frame: index + 1, frames: frames.length });
      if (merged.chunkDelayMs > 0) {
        await sleep(merged.chunkDelayMs);
      }
    }
    return {
      jobId,
      dataBytes: data.length,
      frameCount: frames.length,
      dataChunkSize: merged.dataChunkSize,
      notificationSummary: notificationSummary(this.events),
    };
  }

  async waitForIdleAfterActive(jobId, options = {}) {
    const merged = { ...DEFAULT_PRINT_CUT_OPTIONS, ...options };
    const deadline = Date.now() + merged.waitAfterSendMs;
    let sawActive = false;
    let poll = 0;
    while (Date.now() < deadline) {
      poll += 1;
      const response = await this.getStatus(merged.waitSeconds ?? 5);
      const status = printerStatusTuple(response);
      if (status.printerState && status.printerState !== "20") {
        sawActive = true;
      }
      merged.onPoll?.({ jobId, poll, response, ...status });
      if (sawActive && status.printerState === "20" && status.printerStateAlerts === "::0") {
        return { jobId, poll, status: "idle_after_active", response };
      }
      if (status.printerStateAlerts && status.printerStateAlerts !== "::0") {
        return { jobId, poll, status: "alert", response, ...status };
      }
      await sleep(Math.min(merged.pollIntervalMs, Math.max(0, deadline - Date.now())));
    }
    return { jobId, poll, status: "timeout" };
  }

  async printCut(imageBytes, plotBytes, options = {}) {
    const merged = { ...DEFAULT_PRINT_CUT_OPTIONS, ...options };
    const create = await this.createPrintCutJob(imageBytes.length, plotBytes.length, merged);
    const payload = buildCombinedPrintCutPayload(imageBytes, plotBytes, merged.dataOrder);
    const upload = await this.sendJobData(create.jobId, payload, merged);
    let wait = null;
    if (merged.waitAfterSendMs > 0) {
      wait = await this.waitForIdleAfterActive(create.jobId, merged);
    }
    return { jobId: create.jobId, createResponse: create.response, upload, wait };
  }
}
