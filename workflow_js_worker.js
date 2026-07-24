#!/usr/bin/env node
'use strict';

const readline = require('readline');
const vm = require('vm');

let nextRpcId = 1;
const pending = new Map();

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

async function executeScript(script, args, options) {
  const sandbox = {
    args,
    agent: (prompt, options) => rpc('agent', { prompt, options: normalizeAgentOptions(options) }),
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
