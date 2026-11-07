/**
 * 诊断：Ink 的 useInput 到底把鼠标序列切成什么样？
 *
 * 真机发现 PgUp/End 有效但滚轮无效。怀疑 ConPTY + Ink 的 useInput
 * 把 ESC[<64;10;10M 拆成了多段（ESC 单独一段 → 被当成 escape）。
 */
import { EventEmitter } from 'node:events'
import React from 'react'
import { Text, render, useInput } from 'ink'
import { parseMouseWheel } from '../src/mouseWheel.ts'

class FakeReadStream extends EventEmitter {
  isTTY = true
  private queue: string[] = []
  setRawMode() { return this }
  setEncoding() { return this }
  ref() { return this }
  unref() { return this }
  read(): string | null { return this.queue.shift() ?? null }
  resume() { return this }
  pause() { return this }
  send(s: string) { this.queue.push(s); this.emit('readable') }
}

class FakeWriteStream extends EventEmitter {
  columns = 100
  rows = 30
  isTTY = true
  write() { return true }
}

const seen: string[] = []

function Probe() {
  useInput((input, key) => {
    seen.push(`input=${JSON.stringify(input)} seq=${JSON.stringify((key as { sequence?: string }).sequence ?? '')}`)
    const wheel = parseMouseWheel((key as { sequence?: string }).sequence ?? input)
    seen.push(`  -> wheel=${wheel}`)
  })
  return React.createElement(Text, null, 'probe')
}

const stdin = new FakeReadStream()
const stdout = new FakeWriteStream()

const inst = render(React.createElement(Probe), {
  stdout: stdout as unknown as NodeJS.WriteStream,
  stderr: stdout as unknown as NodeJS.WriteStream,
  stdin: stdin as unknown as NodeJS.ReadStream,
  patchConsole: false,
})

setTimeout(() => {
  // 一次性整段发送（正常终端行为）
  stdin.send('\x1b[<64;10;10M')
  setTimeout(() => {
    stdin.send('\x1b[5~')
    setTimeout(() => {
      // 分段发送（ConPTY 可能的行为）
      stdin.send('\x1b')
      stdin.send('[')
      stdin.send('<64;10;10M')
      setTimeout(() => {
        console.log('=== useInput 收到的片段 ===')
        for (const s of seen) console.log('  ' + s)
        inst.unmount()
        process.exit(0)
      }, 300)
    }, 200)
  }, 200)
}, 300)
