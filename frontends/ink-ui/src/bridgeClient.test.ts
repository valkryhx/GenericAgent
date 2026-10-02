import test from 'node:test'
import assert from 'node:assert/strict'
import { Writable } from 'node:stream'
import { buildBridgeEnv, guardBridgeStdin, writeBridgeCommand } from './bridgeClient.js'

test('buildBridgeEnv forces Python stdio to UTF-8', () => {
  const env = buildBridgeEnv({ PATH: 'x' })

  assert.equal(env.PYTHONIOENCODING, 'utf-8')
  assert.equal(env.PYTHONUTF8, '1')
  assert.equal(env.PATH, 'x')
})

test('writeBridgeCommand serializes workflow_plan as one JSON line', () => {
  const lines: string[] = []
  const stdin = {
    write(chunk: string) {
      lines.push(chunk)
      return true
    },
  }

  writeBridgeCommand(stdin, {
    type: 'workflow_plan',
    taskText: '规划 UI workflow',
    context: { source: 'test' },
    autoApprove: true,
    args: { value: 1 },
    timeoutSeconds: 30,
  })

  assert.equal(lines.length, 1)
  assert.equal(lines[0].endsWith('\n'), true)
  assert.deepEqual(JSON.parse(lines[0]), {
    type: 'workflow_plan',
    taskText: '规划 UI workflow',
    context: { source: 'test' },
    autoApprove: true,
    args: { value: 1 },
    timeoutSeconds: 30,
  })
})

test('writeBridgeCommand serializes workflow_progress as one JSON line', () => {
  const lines: string[] = []
  const stdin = {
    write(chunk: string) {
      lines.push(chunk)
      return true
    },
  }

  writeBridgeCommand(stdin, {
    type: 'workflow_progress',
    runId: 'wf_demo',
  })

  assert.equal(lines.length, 1)
  assert.equal(lines[0].endsWith('\n'), true)
  assert.deepEqual(JSON.parse(lines[0]), {
    type: 'workflow_progress',
    runId: 'wf_demo',
  })
})

test('writeBridgeCommand serializes set_permission_mode as one JSON line', () => {
  const lines: string[] = []
  const stdin = {
    write(chunk: string) {
      lines.push(chunk)
      return true
    },
  }

  writeBridgeCommand(stdin, {
    type: 'set_permission_mode',
    mode: 'read_only',
    persist: false,
  })

  assert.equal(lines.length, 1)
  assert.equal(lines[0].endsWith('\n'), true)
  assert.deepEqual(JSON.parse(lines[0]), {
    type: 'set_permission_mode',
    mode: 'read_only',
    persist: false,
  })
})

test('writeBridgeCommand serializes permission_status as one JSON line', () => {
  const lines: string[] = []
  const stdin = {
    write(chunk: string) {
      lines.push(chunk)
      return true
    },
  }

  writeBridgeCommand(stdin, { type: 'permission_status' })

  assert.equal(lines.length, 1)
  assert.deepEqual(JSON.parse(lines[0]), { type: 'permission_status' })
})

test('guardBridgeStdin absorbs asynchronous EPIPE write errors', async () => {
  const stream = new Writable({
    write(_chunk, _encoding, callback) {
      callback(Object.assign(new Error('write EPIPE'), { code: 'EPIPE' }))
    },
  })
  guardBridgeStdin(stream)

  writeBridgeCommand(stream, { type: 'permission_status' })

  // Without the guard, the 'error' event is unhandled and throws.
  await new Promise(resolve => setTimeout(resolve, 50))
})

test('writeBridgeCommand skips a destroyed bridge stdin', () => {
  const writes: string[] = []
  const stdin = {
    destroyed: true,
    writableEnded: false,
    write(chunk: string) {
      writes.push(chunk)
      return true
    },
  }

  writeBridgeCommand(stdin, { type: 'permission_status' })

  assert.equal(writes.length, 0)
})

test('writeBridgeCommand skips a closed bridge stdin', () => {
  const writes: string[] = []
  const stdin = {
    destroyed: false,
    writableEnded: true,
    write(chunk: string) {
      writes.push(chunk)
      return true
    },
  }

  writeBridgeCommand(stdin, { type: 'permission_status' })

  assert.equal(writes.length, 0)
})
