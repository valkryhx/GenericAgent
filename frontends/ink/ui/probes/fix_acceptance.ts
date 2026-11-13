/**
 * 修复验收探针：把设计文档 §3 的六项判据全部量化，和修复前的基线对照。
 *
 * 判据：
 *  1. 一次 onRender 突发的 base.write 次数（基线 3 → 目标 1）
 *  2. BSU/ESU 配对完整且 > 0
 *  3. 擦除序列是否 100% 落在 BSU…ESU 内
 *  4. 流式中 HIDE 次数（基线 8.4 次/秒 → 目标 0）
 *  5. 光标是否仍落在输入框内（不得退化）
 *  6. park 列是否与该帧内容一致（不得退化）
 *
 * 运行： cd frontends/ink/ui && npx tsx probes/fix_acceptance.ts
 * 对照基线： GA_UI_SYNC_OUTPUT=0 npx tsx probes/fix_acceptance.ts
 */
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from '../src/App.js'
import { createCursorParkStdout } from '../src/stdoutCursorPark.js'
import { applyToScreen, createScreen, type Screen } from '../src/virtualScreen.js'
import type { BridgeClient } from '../src/bridgeClient.js'
import type { BridgeEvent } from '../src/protocol.js'

const NL = '\n'
const COLUMNS = Number(process.env.PROBE_COLUMNS ?? 100)
const ROWS = Number(process.env.PROBE_ROWS ?? 30)
const delay = (ms: number) => new Promise<void>(r => setTimeout(r, ms))

class CaptureWriteStream extends EventEmitter {
  columns = COLUMNS
  rows = ROWS
  isTTY = true
  chunks: string[] = []
  write(chunk: unknown): boolean { this.chunks.push(String(chunk)); return true }
}
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
  send(t: string) { this.queue.push(t); this.emit('readable') }
}

const stripAnsi = (t: string) => t
  .replace(/\u001B\][^\u0007]*(?:\u0007|\u001B\\)/g, '')
  .replace(/\u001B\[[0-?]*[ -/]*[@-~]/g, '')
  .replace(/\u001B[=>]/g, '')

const BSU = '\u001b[?2026h'
const ESU = '\u001b[?2026l'
const cnt = (s: string, needle: string) => s.split(needle).length - 1

async function main() {
  const stdout = new CaptureWriteStream()
  const stderr = new CaptureWriteStream()
  const stdin = new FakeReadStream()
  const park = createCursorParkStdout(stdout as never)

  // ── 度量结构 ────────────────────────────────────────────────────────────
  const screen: Screen = createScreen(ROWS + 80, COLUMNS)
  let frameTop = 0
  let pending: { expectCol: number; promptRow: number; contentBottomRow: number } | null = null
  const parkVerdicts: Array<'inside' | 'outside' | 'no-prompt'> = []
  const colVerdicts: Array<boolean> = []

  const baseWrite = stdout.write.bind(stdout)
  ;(stdout as unknown as { write: (c: unknown) => boolean }).write = (chunk: unknown) => {
    const raw = String(chunk)
    // 拆掉事务包络，按内容分析（包络本身在别处统计）。
    const inner = raw.split(BSU).join('').split(ESU).join('')

    if (inner.includes('\u001b[2K')) {
      const eraseCount = cnt(inner, '\u001b[2K')
      const body = inner.slice(inner.lastIndexOf('\u001b[0G') + 4)
      const rows = Math.max(0, body.split(NL).length - 1)
      frameTop = Math.max(0, screen.row - eraseCount + 1)
      screen.row = frameTop
      screen.col = 0
      applyToScreen(screen, body, COLUMNS)

      // 找输入行 + 期望列（跳过 `>` 后的 gutter 空格）。
      const lines = stripAnsi(body).split(NL)
      let promptRow = -1
      for (let i = lines.length - 1; i >= 0; i -= 1) {
        if (lines[i]!.trimStart().startsWith('>')) { promptRow = i; break }
      }
      const promptLine = promptRow >= 0 ? lines[promptRow]! : ''
      const gt = promptLine.indexOf('>')
      const text = gt >= 0 ? promptLine.slice(gt + 2).replace(/\s+$/, '') : ''
      pending = promptRow >= 0
        ? { expectCol: gt + 3 + text.length, promptRow: frameTop + promptRow, contentBottomRow: frameTop + rows }
        : null
    }

    // park 段：现在不再每帧发 SHOW，所以不能靠 `?25h` 定位；直接找「上移 + 定列」组合。
    // park 的形态是 ESC[<n>A + ESC[<m>G（n 可为 0 而省略）在事务尾部。
    const parkM = /\u001b\[(\d+)A?\u001b\[(\d+)G/.exec(inner)
    if (pending && parkM) {
      const up = Number(parkM[1] ?? 0) || 0
      colVerdicts.push(Number(parkM[2]) === pending.expectCol)
      const parkAbs = pending.contentBottomRow - up
      const top = findBorder(screen, pending.promptRow, -1)
      const bottom = findBorder(screen, pending.promptRow, +1)
      parkVerdicts.push(
        top < 0 || bottom < 0 ? 'no-prompt'
          : parkAbs > top && parkAbs < bottom ? 'inside' : 'outside',
      )
      pending = null
    }
    return baseWrite(chunk)
  }

  let emit: ((e: BridgeEvent) => void) | null = null
  const instance = render(
    React.createElement(App, {
      python: 'python', bridgeScript: 'x', cursorPark: park,
      startBridgeClient: (_p: string, _b: string, onEvent: (e: BridgeEvent) => void): BridgeClient => {
        emit = onEvent
        setTimeout(() => onEvent({ type: 'ready', version: 1 }), 0)
        return { send() {}, stop() {} }
      },
    } as never),
    {
      stdout: park.stdout,
      stderr: stderr as unknown as NodeJS.WriteStream,
      stdin: stdin as unknown as NodeJS.ReadStream,
      patchConsole: false,
      exitOnCtrlC: false,
    } as never,
  )
  const e = () => emit as unknown as (ev: BridgeEvent) => void

  const measure = (from: number, elapsedMs: number) => {
    const chunkList = stdout.chunks.slice(from)
    const all = chunkList.join('')
    const secs = Math.max(0.001, elapsedMs / 1000)
    const bsu = cnt(all, BSU)
    const esu = cnt(all, ESU)
    const totalErase = cnt(all, '\u001b[2K')
    // 事务外的擦除 = 位于「ESU 之后、下一个 BSU 之前」的那些。
    // 把每个事务整体删掉，剩下的就是裸写内容。
    let naked = all
    while (true) {
      const a = naked.indexOf(BSU)
      if (a < 0) break
      const b = naked.indexOf(ESU, a)
      if (b < 0) { naked = naked.slice(0, a); break }
      naked = naked.slice(0, a) + naked.slice(b + ESU.length)
    }
    const eraseOutside = cnt(naked, '\u001b[2K')
    return {
      elapsedMs,
      writes: chunkList.length,
      frames: totalErase > 0 ? chunkList.filter(c => c.includes('\u001b[2K')).length : 0,
      bsu, esu,
      hide: cnt(all, '\u001b[?25l'),
      show: cnt(all, '\u001b[?25h'),
      hidePerSec: cnt(all, '\u001b[?25l') / secs,
      totalErase,
      eraseOutside,
      eraseInside: totalErase - eraseOutside,
      bytes: all.length,
      bytesPerSec: all.length / 1024 / secs,
    }
  }

  try {
    await delay(400)
    e()({ type: 'status', status: 'running', taskId: 1 })
    e()({ type: 'user', taskId: 1, text: 'ok' })
    await delay(200)

    // 流式 2 秒
    let from = stdout.chunks.length
    let t = Date.now()
    for (let i = 0; i < 60; i += 1) {
      e()({ type: 'assistant_delta', taskId: 1, text: `line ${i} ` + 'k'.repeat(20) + NL })
      await delay(20)
    }
    await delay(150)
    const streaming = measure(from, Date.now() - t)

    // 流式中打字 + 回退
    from = stdout.chunks.length
    t = Date.now()
    const pump = (async () => {
      for (let i = 0; i < 30; i += 1) {
        e()({ type: 'assistant_delta', taskId: 1, text: `more ${i}` + NL })
        await delay(20)
      }
    })()
    for (const ch of 'hello') { stdin.send(ch); await delay(90) }
    for (let i = 0; i < 3; i += 1) { stdin.send('\u007f'); await delay(90) }
    await pump
    await delay(150)
    const typing = measure(from, Date.now() - t)

    const mode = park.syncOutput ? 'SYNC ON（修复后）' : 'SYNC OFF（基线）'
    console.log(`\n══════ ${mode} · 终端 ${COLUMNS}x${ROWS} ══════`)
    console.log(`\n【流式 ${streaming.elapsedMs}ms】`)
    console.log(`  1. base.write 次数        = ${streaming.writes}   (其中带擦除的帧 ${streaming.frames})`)
    console.log(`  2. BSU / ESU              = ${streaming.bsu} / ${streaming.esu}   ${streaming.bsu > 0 && streaming.bsu === streaming.esu ? '✅配对' : '❌'}`)
    console.log(`  3. 事务内擦除             = ${streaming.eraseInside} / ${streaming.totalErase}   ${streaming.totalErase > 0 && streaming.eraseInside === streaming.totalErase ? '✅100%' : '❌'}`)
    console.log(`  4. HIDE 次数              = ${streaming.hide}   (${streaming.hidePerSec.toFixed(1)} 次/秒)  ${streaming.hide === 0 ? '✅0' : '❌'}`)
    console.log(`     SHOW 次数              = ${streaming.show}`)
    console.log(`     字节                    = ${(streaming.bytes / 1024).toFixed(1)} KB   (${streaming.bytesPerSec.toFixed(1)} KB/s)`)

    console.log(`\n【流式中打字/回退 ${typing.elapsedMs}ms】`)
    console.log(`  1. base.write 次数        = ${typing.writes}`)
    console.log(`  2. BSU / ESU              = ${typing.bsu} / ${typing.esu}`)
    console.log(`  3. 事务内擦除             = ${typing.eraseInside} / ${typing.totalErase}`)
    console.log(`  4. HIDE 次数              = ${typing.hide}  (${typing.hidePerSec.toFixed(1)} 次/秒)`)

    const inside = parkVerdicts.filter(v => v === 'inside').length
    const outside2 = parkVerdicts.filter(v => v === 'outside').length
    const colOk = colVerdicts.filter(Boolean).length
    console.log(`\n【几何不退化】`)
    console.log(`  5. 光标落在输入框内       = ${inside} / ${parkVerdicts.length}   ${outside2 === 0 ? '✅未退化' : `❌${outside2} 帧越界`}`)
    console.log(`  6. park 列与内容一致      = ${colOk} / ${colVerdicts.length}   ${colOk === colVerdicts.length ? '✅未退化' : '❌'}`)
  } finally {
    instance.unmount()
    await delay(200)
  }
}

function findBorder(screen: Screen, fromRow: number, dir: 1 | -1): number {
  for (let r = fromRow + dir; r >= 0 && r < screen.rows.length; r += dir) {
    const t = (screen.rows[r] ?? '').trim()
    if (t.length > 0 && /^[─═━-]+$/.test(t)) return r
    if (t.length > 0 && dir === -1) return -1
  }
  return -1
}

main().catch(err => { console.error(err); process.exit(1) })
