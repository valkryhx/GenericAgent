import test from 'node:test'
import assert from 'node:assert/strict'
import { CursorParkWriter } from './stdoutCursorPark.js'
import { CursorTracker } from './cursorParkModel.js'

// 手动调度器：把 microtask 收集起来，测试里显式 flush，模拟「一轮同步写入后触发」。
function manualScheduler() {
  const queue: Array<() => void> = []
  const schedule = (cb: () => void) => { queue.push(cb) }
  const flush = () => {
    const pending = queue.splice(0)
    for (const cb of pending) cb()
  }
  return { schedule, flush }
}

test('no park spec: write passes through unchanged, cursor never parked', () => {
  const out: string[] = []
  const { schedule, flush } = manualScheduler()
  const w = new CursorParkWriter((c) => out.push(c), schedule)
  w.write('frame-a')
  flush()
  assert.deepEqual(out, ['frame-a'])
  assert.equal(w.parkedUp, null)
})

test('park after a frame emits relative up + column, once per onRender burst', () => {
  const out: string[] = []
  const { schedule, flush } = manualScheduler()
  const w = new CursorParkWriter((c) => out.push(c), schedule)
  w.setPark({ up: 2, col: 5 })
  // 模拟 ink 一轮 onRender 的 3 次同步写入（clear / static / 主帧）。
  w.write('clear')
  w.write('static')
  w.write('main-frame')
  // 同步阶段内不 park。
  assert.equal(w.parkedUp, null)
  flush()
  // burst 结束后只 park 一次：上移 2 行 + 到第 6 列 + SHOW（原生光标锚定 IME）。
  assert.deepEqual(out, ['clear', 'static', 'main-frame', '\x1b[2A\x1b[6G\x1b[?25h'])
  assert.equal(w.parkedUp, 2)
})

test('unpark before next write returns cursor to frame bottom (relative down + CR)', () => {
  const out: string[] = []
  const { schedule, flush } = manualScheduler()
  const w = new CursorParkWriter((c) => out.push(c), schedule)
  w.setPark({ up: 3, col: 4 })
  w.write('frame-1')
  flush()                       // park: up 3
  assert.equal(w.parkedUp, 3)
  w.write('frame-2')            // 写入前应先 unpark：HIDE + 下移 3 + CR
  assert.equal(out[out.length - 2], '\x1b[?25l\x1b[3B\r')
  assert.equal(out[out.length - 1], 'frame-2')
  assert.equal(w.parkedUp, null)
})

test('up=0 park (caret on frame-bottom row) uses bare CR to unpark', () => {
  const out: string[] = []
  const { schedule, flush } = manualScheduler()
  const w = new CursorParkWriter((c) => out.push(c), schedule)
  w.setPark({ up: 0, col: 7 })
  w.write('f1')
  flush()
  assert.deepEqual(out.slice(-1), ['\x1b[8G\x1b[?25h'])   // 无上移，仅定列 + SHOW
  w.write('f2')
  // unpark: parkedUp===0 → HIDE + '\r'
  assert.equal(out[out.length - 2], '\x1b[?25l\r')
})

test('dispose stops parking and cancels pending microtask', () => {
  const out: string[] = []
  const { schedule, flush } = manualScheduler()
  const w = new CursorParkWriter((c) => out.push(c), schedule)
  w.setPark({ up: 1, col: 2 })
  w.write('f1')
  w.dispose()
  flush()                       // microtask 触发时已 disposed → 不 park
  assert.deepEqual(out, ['f1'])
  assert.equal(w.parkedUp, null)
})

// 端到端不变量：用虚拟终端追踪器验证连续多帧下 eraseLines 始终从帧底往上擦。
test('end-to-end: eraseLines always erases from frame bottom across parked frames', () => {
  const BODY = ['hint', '────────', '> caret']  // 3 行，caret 在末行
  const caretUpFromBottom = 1                    // 帧底(内容后一行) → caret 行 上移 1
  const caretCol = 2
  const { schedule, flush } = manualScheduler()
  const t = new CursorTracker()
  const w = new CursorParkWriter((c) => t.feed(c), schedule)

  const eraseLines = (n: number) => {
    let s = ''
    for (let i = 0; i < n; i++) s += '\x1b[2K' + (i < n - 1 ? '\x1b[1A' : '')
    if (n) s += '\x1b[G'
    return s
  }

  let prevLineCount = 0
  let prevContentTop = 0
  for (let f = 0; f < 6; f++) {
    w.setPark({ up: caretUpFromBottom, col: caretCol })
    const contentTop = t.cursor.row
    t.erasedRows = []
    const output = BODY.join('\n') + '\n'
    w.write(eraseLines(prevLineCount) + output)   // ink 一帧
    if (f > 0) {
      const erasedTop = Math.min(...t.erasedRows)
      const erasedBottom = Math.max(...t.erasedRows)
      assert.ok(
        erasedTop <= prevContentTop && erasedBottom >= prevContentTop,
        `frame ${f}: erased[${erasedTop}..${erasedBottom}] must cover prevContentTop=${prevContentTop}`,
      )
    }
    flush()                                        // park 到 caret
    prevLineCount = output.split('\n').length
    prevContentTop = contentTop
  }
})
