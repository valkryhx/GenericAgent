/**
 * 独立自测探针：模拟 ink_bridge 生产顺序，打印 Static / live 证据。
 * 运行：npx tsx src/grok_selfcheck_user_duplicate.ts
 */
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from './App.js'
import type { BridgeClient } from './bridgeClient.js'
import type { BridgeEvent } from './protocol.js'
import { splitStaticAndActiveMessages } from './messagePartition.js'
import type { ChatMessage } from './protocol.js'

const PROBE = `自测探针-user-dup-${Date.now()}`

class CaptureWriteStream extends EventEmitter {
  columns = 80
  rows = 24
  chunks: string[] = []
  write(chunk: unknown): boolean {
    this.chunks.push(String(chunk))
    return true
  }
}

class FakeReadStream extends EventEmitter {
  isTTY = true
  setRawMode() { return this }
  setEncoding() { return this }
  ref() { return this }
  unref() { return this }
  read() { return null }
  resume() { return this }
  pause() { return this }
}

function stripAnsi(text: string): string {
  return text
    .replace(/\][^]*(?:|\\)/g, '')
    .replace(/\[[0-?]*[ -/]*[@-~]/g, '')
    .replace(/[=>]/g, '')
}

function delay(ms: number) {
  return new Promise(resolve => setTimeout(resolve, ms))
}

function count(haystack: string, needle: string): number {
  let n = 0
  let from = 0
  while (true) {
    const i = haystack.indexOf(needle, from)
    if (i === -1) return n
    n += 1
    from = i + needle.length
  }
}

/** 旧实现：整 task 切片（会把 done user 拉进 active）——用于对照 */
function oldSplit(messages: ChatMessage[], keepLatestTaskActive: boolean) {
  let latestTaskId: number | undefined
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i]?.taskId !== undefined) {
      latestTaskId = messages[i]!.taskId
      break
    }
  }
  if (latestTaskId === undefined) {
    return { staticMessages: messages.filter(m => m.done), activeMessages: [] as ChatMessage[] }
  }
  const hasPending = messages.some(m => m.taskId === latestTaskId && !m.done)
  if (!hasPending && !keepLatestTaskActive) {
    return { staticMessages: messages.filter(m => m.done), activeMessages: [] as ChatMessage[] }
  }
  const activeStart = messages.findIndex(m => m.taskId === latestTaskId)
  return {
    staticMessages: messages.slice(0, activeStart).filter(m => m.done),
    activeMessages: messages.slice(activeStart),
  }
}

async function main() {
  console.log('=== 1) partition 新旧对照（生产序 running 后）===')
  const msgs: ChatMessage[] = [
    { id: 'u-1', role: 'user', text: PROBE, done: true, taskId: 1 },
  ]
  const oldP = oldSplit(msgs, true)
  const newP = splitStaticAndActiveMessages(msgs, { keepLatestTaskActive: true })
  console.log('OLD active ids:', oldP.activeMessages.map(m => m.id))
  console.log('NEW active ids:', newP.activeMessages.map(m => m.id))
  console.log('NEW static ids:', newP.staticMessages.map(m => m.id))
  if (oldP.activeMessages.some(m => m.role === 'user') && !newP.activeMessages.some(m => m.role === 'user')) {
    console.log('PASS partition: 旧实现会把 user 放进 active，新实现不会')
  } else {
    console.error('FAIL partition 对照未体现修复')
    process.exitCode = 1
  }

  console.log('\n=== 2) App 渲染探针 user → status:running ===')
  delete process.env.GA_INK_MOUSE
  let emit: ((e: BridgeEvent) => void) | null = null
  const startBridgeClient = (
    _p: string,
    _s: string,
    onEvent: (e: BridgeEvent) => void,
  ): BridgeClient => {
    emit = onEvent
    setTimeout(() => onEvent({ type: 'ready', version: 1 }), 0)
    return { send() {}, stop() {} }
  }

  const stdout = new CaptureWriteStream()
  const stderr = new CaptureWriteStream()
  const stdin = new FakeReadStream()
  const instance = render(React.createElement(App, {
    python: 'python',
    bridgeScript: 'bridge.py',
    startBridgeClient,
  }), {
    stdout: stdout as unknown as NodeJS.WriteStream,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    patchConsole: false,
  })

  try {
    await delay(80)
    const sink = emit as (e: BridgeEvent) => void
    sink({ type: 'user', taskId: 99, text: PROBE })
    await delay(100)
    sink({ type: 'status', status: 'running', taskId: 99 })
    await delay(150)
    sink({ type: 'assistant_delta', taskId: 99, text: '助手流式片段-selfcheck' })
    await delay(150)

    const plain = stdout.chunks.map(stripAnsi)
    const combined = plain.join('')
    const staticHits = plain
      .filter(c => c.includes(PROBE) && !c.includes('GenericAgent'))
      .length
    const liveHits = plain
      .filter(c => c.includes('GenericAgent') && c.includes(PROBE))
      .length
    const liveHasAssistant = plain
      .filter(c => c.includes('GenericAgent') && c.includes('助手流式片段-selfcheck'))
      .length

    console.log('static-only chunks containing user:', staticHits)
    console.log('live frames containing user:', liveHits)
    console.log('live frames containing assistant delta:', liveHasAssistant)
    console.log('total user text occurrences in stdout:', count(combined, PROBE))

    const lastLive = plain.filter(c => c.includes('GenericAgent')).at(-1) ?? ''
    console.log('--- last live frame (stripped) ---')
    console.log(lastLive.slice(0, 600))
    console.log('--- end ---')

    let ok = true
    if (staticHits < 1) {
      console.error('FAIL: Static 未写入 user')
      ok = false
    }
    if (liveHits !== 0) {
      console.error('FAIL: live 仍含 user（双显未消）')
      ok = false
    }
    if (liveHasAssistant < 1) {
      console.error('FAIL: live 未出现 assistant 流（过度修坏？）')
      ok = false
    }
    if (ok) {
      console.log('\nSELFCHECK PASS: user 仅 Static；live 无 user、有 assistant')
    } else {
      process.exitCode = 1
      console.error('\nSELFCHECK FAIL')
    }
  } finally {
    instance.unmount()
  }
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
