#!/usr/bin/env node
'use strict';

const readline = require('readline');
const vm = require('vm');

let nextRpcId = 1;
const pending = new Map();
let nextRepairGroup = 1;

function emit(message) {
  process.stdout.write(JSON.stringify(message) + '\n');
}

function rpc(method, params) {
  const id = nextRpcId++;
  emit({ type: 'rpc', id, method, params: params || {} });
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
  });
}

function event(method, params) {
  emit({ type: 'event', method, params: params || {} });
}

function transformScript(script) {
  return String(script || '').replace(/\bexport\s+const\s+meta\s*=/, 'const meta =');
}

function normalizeAgentOptions(options) {
  if (!options) return {};
  if (
    typeof options !== 'object' ||
    Array.isArray(options) ||
    Object.prototype.toString.call(options) !== '[object Object]'
  ) {
    throw new TypeError('agent options must be a plain object');
  }
  return options;
}

function normalizeTestGateParams(workspaceOrSpec, options) {
  let spec;
  if (
    workspaceOrSpec &&
    typeof workspaceOrSpec === 'object' &&
    !Array.isArray(workspaceOrSpec) &&
    options === undefined
  ) {
    spec = { ...workspaceOrSpec };
  } else {
    spec = { ...(options || {}), workspacePath: workspaceOrSpec };
  }
  if (typeof spec.workspacePath !== 'string' || !spec.workspacePath.trim()) {
    throw new TypeError('runPythonUnittest requires a workspace path');
  }
  return spec;
}

function normalizeRepairParams(spec) {
  if (!spec || typeof spec !== 'object' || Array.isArray(spec)) {
    throw new TypeError('repairAndRetest requires a plain object');
  }
  const maxAttempts = spec.maxAttempts === undefined ? 2 : Number(spec.maxAttempts);
  if (!Number.isInteger(maxAttempts) || maxAttempts < 0 || maxAttempts > 3) {
    throw new TypeError('repairAndRetest maxAttempts must be an integer between 0 and 3');
  }
  const repairPrompt = spec.repairPrompt;
  if (typeof repairPrompt !== 'string' || !repairPrompt.trim()) {
    throw new TypeError('repairAndRetest requires repairPrompt');
  }
  const labelPrefix = spec.labelPrefix === undefined ? 'repair' : spec.labelPrefix;
  if (typeof labelPrefix !== 'string' || !/^[A-Za-z0-9_-]{1,32}$/.test(labelPrefix)) {
    throw new TypeError('repairAndRetest labelPrefix is invalid');
  }
  const gateKey = spec.gateKey === undefined ? `repair-loop-${nextRepairGroup++}` : spec.gateKey;
  if (typeof gateKey !== 'string' || !/^[A-Za-z0-9._-]{1,64}$/.test(gateKey)) {
    throw new TypeError('repairAndRetest gateKey is invalid');
  }
  const gateSpec = { ...spec, gateKey };
  delete gateSpec.maxAttempts;
  delete gateSpec.repairPrompt;
  delete gateSpec.labelPrefix;
  return { gateSpec, maxAttempts, repairPrompt, labelPrefix, gateKey };
}

async function executeScript(script, args, options) {
  const sandbox = {
    args,
    agent: (prompt, options) => rpc('agent', { prompt, options: normalizeAgentOptions(options) }),
    runPythonUnittest: (workspaceOrSpec, options) =>
      rpc('runPythonUnittest', normalizeTestGateParams(workspaceOrSpec, options)),
    repairAndRetest: async (spec) => {
      const normalized = normalizeRepairParams(spec);
      let gate = await rpc('runPythonUnittest', normalizeTestGateParams(normalized.gateSpec));
      let repairAttempts = 0;
      const repairs = [];
      while (!gate.gatePassed && repairAttempts < normalized.maxAttempts) {
        repairAttempts += 1;
        const repair = await rpc('agent', {
          prompt:
            `${normalized.repairPrompt}\n` +
            `Read ${normalized.gateSpec.workspacePath}/TEST_FAILURES.txt and the workspace files, ` +
            'make the smallest repair, and return a concise result.',
          options: normalizeAgentOptions({
            label: `${normalized.labelPrefix}-${repairAttempts}`,
            phase: 'Repair',
          }),
        });
        repairs.push({ attempt: repairAttempts, result: repair });
        gate = await rpc('runPythonUnittest', normalizeTestGateParams(normalized.gateSpec));
      }
      return {
        type: 'repair_retest_result',
        passed: Boolean(gate.passed),
        gatePassed: Boolean(gate.gatePassed),
        repairAttempts,
        maxAttempts: normalized.maxAttempts,
        gateKey: normalized.gateKey,
        finalGate: gate,
        repairs,
      };
    },
    phase: (name) => event('phase', { name }),
    log: (message) => event('log', { message }),
    parallel: async (items) => Promise.all((items || []).map((item) => (typeof item === 'function' ? item() : item))),
    pipeline: async (items, ...stages) => {
      let values = items || [];
      for (const stage of stages) {
        values = await Promise.all(values.map((value, index) => stage(value, (items || [])[index], index)));
      }
      return values;
    },
    console: {
      log: (...parts) => event('log', { message: parts.map(String).join(' ') }),
      error: (...parts) => event('log', { message: parts.map(String).join(' ') }),
    },
    setTimeout: undefined,
    setInterval: undefined,
    require: undefined,
    process: undefined,
    fetch: undefined,
    XMLHttpRequest: undefined,
    WebSocket: undefined,
    Deno: undefined,
    Bun: undefined,
  };
  const context = vm.createContext(sandbox, {
    codeGeneration: { strings: false, wasm: false },
  });
  const wrapped = `(async () => {\n${transformScript(script)}\n})()`;
  const compiled = new vm.Script(wrapped, { filename: 'workflow-script.js' });
  const timeout = Math.max(1, Number(options && options.timeoutMs) || 1000);
  return await compiled.runInContext(context, { timeout });
}

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });

rl.on('line', async (line) => {
  if (!line.trim()) return;
  let message;
  try {
    message = JSON.parse(line);
  } catch (error) {
    emit({ type: 'error', error: `invalid json: ${error.message}` });
    return;
  }

  if (message.type === 'rpc_result') {
    const waiter = pending.get(message.id);
    if (!waiter) return;
    pending.delete(message.id);
    if (message.ok) waiter.resolve(message.value);
    else waiter.reject(new Error(message.error || 'workflow rpc failed'));
    return;
  }

  if (message.type !== 'start') return;

  try {
    const result = await executeScript(message.script || '', message.args, { timeoutMs: message.timeoutMs });
    emit({ type: 'done', result });
  } catch (error) {
    emit({ type: 'error', error: error && error.stack ? error.stack : String(error) });
  }
});

emit({ type: 'ready' });
