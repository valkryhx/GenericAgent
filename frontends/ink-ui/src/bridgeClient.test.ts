import test from 'node:test'
import assert from 'node:assert/strict'
import { buildBridgeEnv, writeBridgeCommand } from './bridgeClient.js'

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
    autoApprove: false,
    args: { value: 1 },
    timeoutSeconds: 30,
  })

  assert.equal(lines.length, 1)
  assert.equal(lines[0].endsWith('\n'), true)
  assert.deepEqual(JSON.parse(lines[0]), {
    type: 'workflow_plan',
    taskText: '规划 UI workflow',
    context: { source: 'test' },
    autoApprove: false,
    args: { value: 1 },
    timeoutSeconds: 30,
  })
})
