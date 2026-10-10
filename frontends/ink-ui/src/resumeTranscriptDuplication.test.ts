import test from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from './App.js'
import { ReflowTerminal, countVisibleRows } from './resizeReflowModel.js'
import type { BridgeClient } from './bridgeClient.js'
import type { BridgeEvent } from './protocol.js'

const ESC = String.fromCharCode(27)

class CaptureWriteStream extends EventEmitter {
  columns = 80
  rows = 24
  chunks: string[] = []
  isTTY = true

  write(chunk: unknown): boolean {
    this.chunks.push(String(chunk))
    return true
  }
}

class FakeReadStream extends EventEmitter {
  isTTY = true
  private readonly queue: string[] = []

  setRawMode(): this {
    return this
  }

  setEncoding(): this {
    return this
  }

  ref(): this {
    return this
  }

  unref(): this {
    return this
  }

  read(): string | null {
    return this.queue.shift() ?? null
  }

  resume(): this {
    return this
  }

  pause(): this {
    return this
  }

  send(text: string): void {
    this.queue.push(text)
    this.emit('readable')
  }
}

function delay(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms))
}

// 只保留可见字符：ESC 之后的单个字符（CSI/SS3 引导）与 CSI 参数一并丢弃。
function stripAnsi(text: string): string {
  let out = ''
  for (let index = 0; index < text.length; index += 1) {
    if (text[index] === ESC) {
      index += 1
      if (text[index] === '[') {
        index += 1
        while (index < text.length && !(text[index]! >= '@' && text[index]! <= '~')) index += 1
      }
      continue
    }
    out += text[index]
  }
  return out
}

function occurrences(haystack: string, needle: string): number {
  return haystack.split(needle).length - 1
}

function renderHarness(onBridge: (emit: (event: BridgeEvent) => void) => void): {
  stdout: CaptureWriteStream
  unmount: () => void
} {
  const stdout = new CaptureWriteStream()
  const stderr = new CaptureWriteStream()
  const stdin = new FakeReadStream()
  const startBridgeClient = (
    _python: string,
    _bridgeScript: string,
    onEvent: (event: BridgeEvent) => void,
  ): BridgeClient => {
    setTimeout(() => onEvent({ type: 'ready', version: 1 }), 0)
    onBridge(onEvent)
    return { send() {}, stop() {} }
  }
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
  return { stdout, unmount: () => instance.unmount() }
}

// resume 的恢复历史比当前会话更长时，<Static> 的旧实现会先按「追加尾部」写一次，
// 再因重挂 key 全量重印一次 —— 尾部内容在终端里出现两份（用户截图 resume_bug.png）。
test('resume prints each restored transcript row exactly once when restored history is longer', async () => {
  const { stdout, unmount } = renderHarness(emit => {
    setTimeout(() => {
      emit({ type: 'status', status: 'running', taskId: 1 })
      emit({ type: 'user', taskId: 1, text: 'CURRENT-TURN-ALPHA' })
      emit({ type: 'assistant_done', taskId: 1, text: 'CURRENT-ANSWER-ALPHA' })
      emit({ type: 'status', status: 'idle', taskId: 1 })
    }, 30)
    setTimeout(() => {
      emit({
        type: 'history_replace',
        messages: [
          { role: 'user', taskId: 1, text: 'RESTORED-USER-1' },
          { role: 'assistant', taskId: 1, text: 'RESTORED-ASSISTANT-1' },
          { role: 'user', taskId: 2, text: 'RESTORED-USER-2' },
          { role: 'assistant', taskId: 2, text: 'RESTORED-ASSISTANT-2' },
        ],
      } as unknown as BridgeEvent)
      emit({ type: 'system', text: 'RESUME-OK 恢复完成：2 轮历史 · session_xyz.jsonl' })
    }, 120)
  })

  try {
    const deadline = Date.now() + 2000
    let merged = ''
    while (Date.now() < deadline) {
      merged = stdout.chunks.map(stripAnsi).join('')
      if (merged.includes('RESUME-OK')) break
      await delay(20)
    }
    await delay(120)

    const output = stdout.chunks.map(stripAnsi).join('')
    for (const probe of ['RESTORED-USER-1', 'RESTORED-ASSISTANT-1', 'RESTORED-USER-2', 'RESTORED-ASSISTANT-2']) {
      assert.equal(occurrences(output, probe), 1, `${probe} should be printed exactly once, got ${occurrences(output, probe)}`)
    }
    assert.match(output, /RESUME-OK/)
  } finally {
    unmount()
  }
})

// /rewind 让历史变短，也必须只重印一次（不得出现「旧的全量 + 重印的裁剪历史」叠加）。
// 用 reflow 虚拟终端而不是裸字节计数：rewind 前会先整屏重置（清屏 + 清 scrollback），
// 被擦掉的行不该再算数。
test('rewind leaves exactly one copy of the trimmed transcript on screen', async () => {
  const { stdout, unmount } = renderHarness(emit => {
    setTimeout(() => {
      for (let index = 1; index <= 2; index += 1) {
        emit({ type: 'status', status: 'running', taskId: index })
        emit({ type: 'user', taskId: index, text: `ORIGINAL-USER-${index}` })
        emit({ type: 'assistant_done', taskId: index, text: `ORIGINAL-ASSISTANT-${index}` })
        emit({ type: 'status', status: 'idle', taskId: index })
      }
    }, 30)
    setTimeout(() => {
      emit({ type: 'rewind_done', taskId: 2, text: 'ORIGINAL-USER-2' } as unknown as BridgeEvent)
    }, 150)
  })

  try {
    await delay(400)
    const term = new ReflowTerminal(80, 24)
    for (const chunk of stdout.chunks) term.write(chunk)
    const screen = term.screen()

    assert.equal(countVisibleRows(screen, 'ORIGINAL-USER-1'), 1, `kept turn must appear once on screen:\n${screen.join('\n')}`)
    assert.equal(countVisibleRows(screen, 'ORIGINAL-ASSISTANT-1'), 1, `kept answer must appear once on screen:\n${screen.join('\n')}`)
    assert.equal(countVisibleRows(screen, 'ORIGINAL-ASSISTANT-2'), 0, 'rewound answer must be gone from the screen')
  } finally {
    unmount()
  }
})
