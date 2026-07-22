import {
  DEFAULT_PRINT_CUT_OPTIONS,
  PixcutWebBluetoothClient,
  buildCombinedPrintCutPayload,
  buildHanntoPackets,
  printerStatusTuple,
} from "./pixcut-web-bluetooth.mjs";

const elements = {
  connect: document.querySelector("#connect"),
  readStatus: document.querySelector("#read-status"),
  dryRun: document.querySelector("#dry-run"),
  printCut: document.querySelector("#print-cut"),
  armExecute: document.querySelector("#arm-execute"),
  printFile: document.querySelector("#print-file"),
  pltFile: document.querySelector("#plt-file"),
  preview: document.querySelector("#preview"),
  previewEmpty: document.querySelector("#preview-empty"),
  deviceStatus: document.querySelector("#device-status"),
  printerStatus: document.querySelector("#printer-status"),
  fileStatus: document.querySelector("#file-status"),
  progress: document.querySelector("#progress"),
  progressText: document.querySelector("#progress-text"),
  jobType: document.querySelector("#job-type"),
  chunkSize: document.querySelector("#chunk-size"),
  waitSeconds: document.querySelector("#wait-seconds"),
  pollSeconds: document.querySelector("#poll-seconds"),
  log: document.querySelector("#log"),
};

let client = null;
let printBytes = null;
let plotBytes = null;

function formatBytes(bytes) {
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`;
  }
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
}

function setStatus(element, text, kind = "neutral") {
  element.textContent = text;
  element.dataset.kind = kind;
}

function log(text, detail = null) {
  const line = document.createElement("div");
  const timestamp = new Date().toLocaleTimeString();
  line.textContent = `[${timestamp}] ${text}`;
  elements.log.prepend(line);
  if (detail !== null) {
    const details = document.createElement("pre");
    details.textContent = typeof detail === "string" ? detail : JSON.stringify(detail, null, 2);
    elements.log.prepend(details);
  }
}

function optionsFromForm() {
  return {
    ...DEFAULT_PRINT_CUT_OPTIONS,
    jobType: Number.parseInt(elements.jobType.value, 10),
    dataChunkSize: Number.parseInt(elements.chunkSize.value, 10),
    waitAfterSendMs: Number.parseFloat(elements.waitSeconds.value) * 1000,
    pollIntervalMs: Number.parseFloat(elements.pollSeconds.value) * 1000,
  };
}

function updateButtons() {
  const hasFiles = Boolean(printBytes?.length && plotBytes?.length);
  const connected = Boolean(client?.connected);
  elements.readStatus.disabled = !connected;
  elements.dryRun.disabled = !hasFiles;
  elements.printCut.disabled = !(connected && hasFiles && elements.armExecute.checked);
}

function updateFileStatus() {
  const printText = printBytes ? `print ${formatBytes(printBytes.length)}` : "print none";
  const plotText = plotBytes ? `plt ${formatBytes(plotBytes.length)}` : "plt none";
  setStatus(elements.fileStatus, `${printText} / ${plotText}`, printBytes && plotBytes ? "ok" : "neutral");
}

async function readFileBytes(input) {
  const file = input.files?.[0];
  if (!file) {
    return null;
  }
  return new Uint8Array(await file.arrayBuffer());
}

elements.printFile.addEventListener("change", async () => {
  printBytes = await readFileBytes(elements.printFile);
  const file = elements.printFile.files?.[0];
  if (file) {
    elements.preview.src = URL.createObjectURL(file);
    elements.preview.hidden = false;
    elements.previewEmpty.hidden = true;
  } else {
    elements.preview.removeAttribute("src");
    elements.preview.hidden = true;
    elements.previewEmpty.hidden = false;
  }
  updateFileStatus();
  updateButtons();
});

elements.pltFile.addEventListener("change", async () => {
  plotBytes = await readFileBytes(elements.pltFile);
  updateFileStatus();
  updateButtons();
});

elements.armExecute.addEventListener("change", updateButtons);

elements.connect.addEventListener("click", async () => {
  try {
    client = new PixcutWebBluetoothClient({ log });
    await client.requestAndConnect();
    setStatus(elements.deviceStatus, client.device?.name || "connected", "ok");
    updateButtons();
  } catch (error) {
    setStatus(elements.deviceStatus, "not connected", "bad");
    log("Connect failed", String(error));
  }
});

elements.readStatus.addEventListener("click", async () => {
  try {
    const response = await client.getStatus();
    const status = printerStatusTuple(response);
    setStatus(
      elements.printerStatus,
      `${status.printerState} / ${status.printerSubState} / ${status.printerStateAlerts}`,
      status.printerStateAlerts === "::0" ? "ok" : "bad"
    );
    log("Status", response);
  } catch (error) {
    setStatus(elements.printerStatus, "status error", "bad");
    log("Status failed", String(error));
  }
});

elements.dryRun.addEventListener("click", () => {
  try {
    const options = optionsFromForm();
    const payload = buildCombinedPrintCutPayload(printBytes, plotBytes, options.dataOrder);
    const frames = buildHanntoPackets(payload, {
      jobId: 1,
      dataChunkSize: options.dataChunkSize,
      connectType: 2,
      interactive: 6,
      encoding: 2,
      splited: true,
    });
    log("Dry run", {
      imageBytes: printBytes.length,
      plotBytes: plotBytes.length,
      uploadBytes: payload.length,
      dataOrder: options.dataOrder,
      frameCount: frames.length,
      firstFrameBytes: frames[0]?.length,
      lastFrameBytes: frames.at(-1)?.length,
      mediaSize: options.mediaSize,
      mediaType: options.mediaType,
      jobType: options.jobType,
    });
  } catch (error) {
    log("Dry run failed", String(error));
  }
});

elements.printCut.addEventListener("click", async () => {
  try {
    const options = {
      ...optionsFromForm(),
      onProgress: ({ sent, total, frame, frames }) => {
        const value = Math.round((sent / total) * 100);
        elements.progress.value = value;
        elements.progressText.textContent = `${formatBytes(sent)} / ${formatBytes(total)} (${frame}/${frames})`;
      },
      onPoll: ({ printerState, printerSubState, printerStateAlerts }) => {
        setStatus(
          elements.printerStatus,
          `${printerState} / ${printerSubState} / ${printerStateAlerts}`,
          printerStateAlerts === "::0" ? "ok" : "bad"
        );
      },
    };
    elements.printCut.disabled = true;
    elements.progress.value = 0;
    elements.progressText.textContent = "starting";
    const result = await client.printCut(printBytes, plotBytes, options);
    elements.progress.value = 100;
    elements.progressText.textContent = "complete";
    elements.armExecute.checked = false;
    updateButtons();
    log("Print-cut complete", result);
  } catch (error) {
    elements.progressText.textContent = "failed";
    log("Print-cut failed", String(error));
  } finally {
    updateButtons();
  }
});

function init() {
  elements.jobType.value = String(DEFAULT_PRINT_CUT_OPTIONS.jobType);
  elements.chunkSize.value = String(DEFAULT_PRINT_CUT_OPTIONS.dataChunkSize);
  elements.waitSeconds.value = String(DEFAULT_PRINT_CUT_OPTIONS.waitAfterSendMs / 1000);
  elements.pollSeconds.value = String(DEFAULT_PRINT_CUT_OPTIONS.pollIntervalMs / 1000);
  if (!globalThis.isSecureContext || !navigator.bluetooth) {
    setStatus(elements.deviceStatus, "web bluetooth unavailable", "bad");
    elements.connect.disabled = true;
  }
  updateFileStatus();
  updateButtons();
}

init();
