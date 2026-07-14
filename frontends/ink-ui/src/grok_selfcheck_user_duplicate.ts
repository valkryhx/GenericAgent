/**
 * 独立自测探针：Running 可见性 P0-A + 单通道。
 * 运行：npx tsx src/grok_selfcheck_user_duplicate.ts
 *
 * 期望：
 * - running 中 user 仅 live（无过早 Static）
 * - 有 assistant 流时 live 可见
 * - assistant_done 后 user+assistant 进 Static，live 不再含 user
 */
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from './App.js'
import type { BridgeClient } from './bridgeClient.js'
import type { BridgeEvent } from './protocol.js'
import { splitStaticAndActiveMessages } from './messagePartition.js'
import type { ChatMessage } from './protocol.js'
import { applyBridgeEvent, initialState } from './state.js'

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


function isLiveChromeFrame(chunk: string): boolean {
  return chunk.includes('GenericAgent')
    || chunk.includes('Enter send')
    || chunk.includes('Running ·')
    || chunk.includes('✻')
    || /─{8,}/.test(chunk)
}

async function main() {
  console.log('=== 1) state/partition：open user live-only ===')
  let st = applyBridgeEvent(initialState, { type: 'ready', version: 1 })
  st = applyBridgeEvent(st, { type: 'user', taskId: 1, text: PROBE })
  const open = splitStaticAndActiveMessages(st.messages, { keepLatestTaskActive: true })
  console.log('open active ids:', open.activeMessages.map(m => m.id))
  console.log('open static ids:', open.staticMessages.map(m => m.id))
  if (open.activeMessages.some(m => m.role === 'user') && !open.staticMessages.some(m => m.role === 'user')) {
    console.log('PASS partition open-turn: user only in active')
  } else {
    console.error('FAIL open-turn partition')
    process.exitCode = 1
  }

  st = applyBridgeEvent(st, { type: 'assistant_done', taskId: 1, text: 'done' })
  const fin = splitStaticAndActiveMessages(st.messages)
  console.log('finalized static ids:', fin.staticMessages.map(m => m.id))
  if (fin.staticMessages.some(m => m.id === 'u-1') && fin.activeMessages.length === 0) {
    console.log('PASS partition finalize: user in static only')
  } else {
    console.error('FAIL finalize partition')
    process.exitCode = 1
  }

  console.log('\n=== 2) App: running 中 user 在 live、无过早 Static；stream 可见 ===')
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
    await delay(80)
    sink({ type: 'status', status: 'running', taskId: 99 })
    await delay(120)

    // Before any assistant content: user must stay live-only (no premature Static).
    let plain = stdout.chunks.map(stripAnsi)
    let staticHits = plain.filter(c => c.includes(PROBE) && !isLiveChromeFrame(c)).length
    let liveHits = plain.filter(c => isLiveChromeFrame(c) && c.includes(PROBE)).length
    console.log('pre-delta static-only user chunks:', staticHits)
    console.log('pre-delta live frames with user:', liveHits)
    let ok = true
    if (staticHits !== 0) {
      console.error('FAIL: assistant 前 Static 不应写入 user')
      ok = false
    }
    if (liveHits < 1) {
      console.error('FAIL: running 中 live 未见 user')
      ok = false
    }

    sink({ type: 'assistant_delta', taskId: 99, text: '助手流式片段-selfcheck' })
    await delay(150)

    plain = stdout.chunks.map(stripAnsi)
    staticHits = plain.filter(c => c.includes(PROBE) && !isLiveChromeFrame(c)).length
    liveHits = plain.filter(c => isLiveChromeFrame(c) && c.includes(PROBE)).length
    const liveHasAssistant = plain.filter(c => isLiveChromeFrame(c) && c.includes('助手流式片段-selfcheck')).length

    console.log('post-delta static-only user chunks:', staticHits)
    console.log('post-delta live frames with user:', liveHits)
    console.log('live frames containing assistant delta:', liveHasAssistant)

    const lastLive = plain.filter(isLiveChromeFrame).at(-1) ?? ''
    console.log('--- last live frame (stripped) ---')
    console.log(lastLive.slice(0, 600))
    console.log('--- end ---')

    // After first delta, user is finalized into Static (chronological order before stream commits).
    if (staticHits < 1) {
      console.error('FAIL: 首包 assistant 后 Static 应写入 user（保证问在答前）')
      ok = false
    }
    if (liveHasAssistant < 1) {
      console.error('FAIL: live 未见 assistant 流')
      ok = false
    }

    sink({ type: 'assistant_done', taskId: 99, text: '助手流式片段-selfcheck' })
    sink({ type: 'status', status: 'idle' })
    await delay(150)

    const plainAfter = stdout.chunks.map(stripAnsi)
    const staticAfter = plainAfter.filter(c => c.includes(PROBE) && !isLiveChromeFrame(c)).length
    if (staticAfter < 1) {
      console.error('FAIL: finalize 后 Static 未保留 user')
      ok = false
    } else {
      console.log('static-only chunks after finalize:', staticAfter)
    }

    if (ok && process.exitCode !== 1) {
      console.log('\nSELFCHECK PASS: pre-delta user live-only; post-delta user Static before assistant; stream visible')
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
