'use strict';

const installed = new Set();
const dataPacketCounts = {};
let lastPrintCutParams = null;

function emit(kind, fields) {
  send(Object.assign({ kind }, fields || {}));
}

function findExportsContaining(needle) {
  const matches = [];
  for (const module of Process.enumerateModules()) {
    let exports;
    try {
      exports = module.enumerateExports();
    } catch (_) {
      continue;
    }
    for (const exp of exports) {
      if (exp.name.indexOf(needle) !== -1) {
        matches.push({ module: module.name, name: exp.name, address: exp.address });
      }
    }
  }
  return matches;
}

function hexBytes(ptr, length) {
  if (ptr.isNull() || length <= 0) {
    return '';
  }
  const size = Math.min(length, 32);
  const bytes = new Uint8Array(ptr.readByteArray(size));
  return Array.from(bytes, b => b.toString(16).padStart(2, '0')).join(' ');
}

function asciiPrefix(ptr, length) {
  if (ptr.isNull() || length <= 0) {
    return '';
  }
  const size = Math.min(length, 64);
  const bytes = new Uint8Array(ptr.readByteArray(size));
  return Array.from(bytes, b => (b >= 32 && b <= 126) ? String.fromCharCode(b) : '.').join('');
}

function textPrefix(ptr, length, maxLength) {
  if (ptr.isNull() || length <= 0) {
    return '';
  }
  const size = Math.min(length, maxLength);
  const bytes = new Uint8Array(ptr.readByteArray(size));
  return Array.from(bytes, b => (b >= 32 && b <= 126) || b === 10 ? String.fromCharCode(b) : '.').join('');
}

function commandSummary(ptr, length) {
  const text = textPrefix(ptr, length, 4096);
  const summary = { text_prefix: text };
  if (text.indexOf('cmd json\n') === 0) {
    const body = text.slice('cmd json\n'.length);
    try {
      const decoded = JSON.parse(body);
      summary.json = decoded;
      summary.method = decoded.method || null;
    } catch (_) {
    }
  }
  return summary;
}

function responseSummary(ptr, lengthPtr) {
  if (ptr.isNull() || lengthPtr.isNull()) {
    return null;
  }
  let length = 0;
  try {
    length = lengthPtr.readS32();
  } catch (_) {
    return null;
  }
  if (length <= 0) {
    return { length };
  }
  return Object.assign(pointerSummary(ptr, length), commandSummary(ptr, length));
}

function fnv1aSample(ptr, length) {
  if (ptr.isNull() || length <= 0) {
    return '00000000';
  }
  const sample = Math.min(length, 4096);
  const bytes = new Uint8Array(ptr.readByteArray(sample));
  let hash = 0x811c9dc5;
  for (const b of bytes) {
    hash ^= b;
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash.toString(16).padStart(8, '0');
}

function pointerSummary(ptr, length) {
  return {
    address: ptr.toString(),
    length,
    first_hex: hexBytes(ptr, length),
    first_ascii: asciiPrefix(ptr, length),
    first_4k_fnv1a: fnv1aSample(ptr, length),
  };
}

function readPrintCutParams(params) {
  return {
    address: params.toString(),
    raw_64_hex: hexBytes(params, 64),
    channel: params.add(0).readS32(),
    copies: params.add(4).readS32(),
    media_size: params.add(8).readS32(),
    media_type: params.add(12).readS32(),
    job_type: params.add(16).readS32(),
    image_size: params.add(20).readS32(),
    plot_size: params.add(24).readS32(),
  };
}

function hookFirstExportContaining(needle, installer) {
  if (installed.has(needle)) {
    return true;
  }
  const matches = findExportsContaining(needle);
  if (matches.length === 0) {
    return false;
  }
  const match = matches[0];
  installer(match);
  installed.add(needle);
  emit('hooked', match);
  return true;
}

function installHooks() {
  hookFirstExportContaining('createPrintCutJob', match => {
    Interceptor.attach(match.address, {
      onEnter(args) {
        const params = readPrintCutParams(args[1]);
        lastPrintCutParams = params;
        this.jobIdPtr = args[4];
        emit('create_print_cut_enter', {
          symbol: match.name,
          instance: args[0].toString(),
          params,
          arg2_summary_using_image_size: pointerSummary(args[2], params.image_size),
          arg3_summary_using_plot_size: pointerSummary(args[3], params.plot_size),
          job_id_ptr: args[4].toString(),
        });
      },
      onLeave(retval) {
        let jobId = null;
        try {
          if (this.jobIdPtr && !this.jobIdPtr.isNull()) {
            jobId = this.jobIdPtr.readU32();
          }
        } catch (_) {
        }
        emit('create_print_cut_leave', {
          symbol: match.name,
          retval: retval.toInt32(),
          job_id: jobId,
        });
      },
    });
  });

  hookFirstExportContaining('sendJobData', match => {
    Interceptor.attach(match.address, {
      onEnter(args) {
        const jobId = args[1].toUInt32();
        const length = args[3].toInt32();
        const payload = {
          symbol: match.name,
          job_id: jobId,
          length,
          data: pointerSummary(args[2], length),
        };
        if (lastPrintCutParams) {
          payload.last_print_cut_params = lastPrintCutParams;
          if (lastPrintCutParams.plot_size > 0 && length > lastPrintCutParams.plot_size) {
            payload.after_plot_prefix = pointerSummary(args[2].add(lastPrintCutParams.plot_size), Math.min(64, length - lastPrintCutParams.plot_size));
          }
          if (lastPrintCutParams.image_size > 0 && length > lastPrintCutParams.image_size) {
            payload.after_image_prefix = pointerSummary(args[2].add(lastPrintCutParams.image_size), Math.min(64, length - lastPrintCutParams.image_size));
          }
        }
        emit('send_job_data_enter', payload);
      },
      onLeave(retval) {
        emit('send_job_data_leave', {
          symbol: match.name,
          retval: retval.toInt32(),
        });
      },
    });
  });

  hookFirstExportContaining('transferCommand', match => {
    Interceptor.attach(match.address, {
      onEnter(args) {
        const length = args[2].toInt32();
        this.responsePtr = args[3];
        this.responseLengthPtr = args[4];
        emit('transfer_command', {
          symbol: match.name,
          length,
          out: pointerSummary(args[1], length),
          command: commandSummary(args[1], length),
        });
      },
      onLeave(retval) {
        emit('transfer_command_leave', {
          symbol: match.name,
          retval: retval.toInt32(),
          response: responseSummary(this.responsePtr, this.responseLengthPtr),
        });
      },
    });
  });

  hookFirstExportContaining('transferData', match => {
    Interceptor.attach(match.address, {
      onEnter(args) {
        const length = args[2].toInt32();
        const ptr = args[1];
        this.responsePtr = args[3];
        this.responseLengthPtr = args[4];
        let extlen = null;
        let jobId = null;
        let payloadPrefix = null;
        try {
          const text = asciiPrefix(ptr, Math.min(length, 96));
          const matchExt = text.match(/^cmd data EXTLEN=([0-9]+)\n/);
          if (matchExt) {
            extlen = parseInt(matchExt[1], 10);
            const headerLen = matchExt[0].length;
            jobId = ptr.add(headerLen).readU32();
            payloadPrefix = pointerSummary(ptr.add(headerLen + 4), Math.min(extlen, 128));
          }
        } catch (_) {
        }
        const key = jobId === null ? 'unknown' : String(jobId);
        dataPacketCounts[key] = (dataPacketCounts[key] || 0) + 1;
        emit('transfer_data', {
          symbol: match.name,
          packet_index_for_job: dataPacketCounts[key],
          length,
          extlen,
          job_id: jobId,
          out: pointerSummary(ptr, length),
          payload_prefix: payloadPrefix,
        });
      },
      onLeave(retval) {
        emit('transfer_data_leave', {
          symbol: match.name,
          retval: retval.toInt32(),
          response: responseSummary(this.responsePtr, this.responseLengthPtr),
        });
      },
    });
  });
}

const timer = setInterval(() => {
  installHooks();
  if (installed.has('createPrintCutJob') && installed.has('sendJobData') &&
      installed.has('transferCommand') && installed.has('transferData')) {
    clearInterval(timer);
  }
}, 250);

emit('trace_ready', { pid: Process.id });
