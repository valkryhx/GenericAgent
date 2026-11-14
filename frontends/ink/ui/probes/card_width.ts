/**
 * 探针：验证卡片在窄终端下是否真的降级。
 *
 * 前一个探针（question_card.ts）用 debug:true + 假 stream，结果 @60 与 @120
 * 渲染完全相同 —— 怀疑假 stream 没让 Ink 拿到真实宽度。
 * 这个探针直接问 Ink：给定 columns，它算出来的输出有多少列宽。
 */
import React from 'react'
import { EventEmitter } from 'node:events'
import { render, Box, Text } from 'ink'

const NL = String.fromCharCode(10)

class CaptureStream extends EventEmitter {
  readonly chunks: string[] = []
  isTTY = true
  constructor(public columns: number, public rows: number) {
    super()
  }
  write(s: string): boolean {
    this.chunks.push(s)
    return true
  }
  get text() {
    return this.chunks.join('')
  }
}

const LONG = 'Which database should the migration target?'

async function measure(columns: number, wrap: 'truncate-end' | 'wrap' | undefined) {
  const stdout = new CaptureStream(columns, 40)
  const inst = render(
    React.createElement(
      Box,
      { flexDirection: 'column', paddingX: 1 },
      React.createElement(Text, { wrap, color: 'cyan' }, LONG),
    ),
    { stdout: stdout as unknown as NodeJS.WriteStream, debug: true, patchConsole: false },
  )
  await new Promise(r => setTimeout(r, 100))
  inst.unmount()
  await new Promise(r => setTimeout(r, 60))
  const clean = stdout.text.replace(/\x1b\[[0-9;]*[A-Za-z]/g, '')
  const lines = clean.split(/\r?\n/).filter(l => l.trim() !== '')
  const widest = Math.max(...lines.map(l => l.length), 0)
  return { columns, wrap: String(wrap), width: widest, first: lines[0] ?? '' }
}

async function main() {
  const rows: string[] = []
  for (const columns of [120, 80, 60, 40]) {
    for (const wrap of ['truncate-end', 'wrap', undefined] as const) {
      const r = await measure(columns, wrap)
      rows.push(
        `columns=${r.columns} wrap=${r.wrap.padEnd(12)} widest=${String(r.width).padStart(3)}  | ${r.first}`,
      )
    }
  }
  process.stdout.write(rows.join(NL) + NL)
}

main().catch(e => {
  console.error(e)
  process.exit(1)
})
