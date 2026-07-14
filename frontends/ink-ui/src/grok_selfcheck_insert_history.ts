/**
 * 阶段 3 自测：viewport 几何 — 先下移、触底固定（Codex insert_history 数学）。
 * 运行：npx tsx src/grok_selfcheck_insert_history.ts
 */
import {
  advanceViewportForHistory,
  applyDockHeight,
  createViewportState,
  insertHistorySequence,
  isBottomAligned,
} from './insertHistory.js'

function main() {
  let ok = true
  const check = (name: string, cond: boolean, detail = '') => {
    if (cond) console.log(`PASS  ${name}${detail ? ` — ${detail}` : ''}`)
    else {
      ok = false
      console.error(`FAIL  ${name}${detail ? ` — ${detail}` : ''}`)
    }
  }

  console.log('=== phase-3 insertHistory viewport geometry ===')
  let vp = createViewportState({ screenRows: 24, screenCols: 80, dockHeight: 6, areaY: 2 })
  check('starts not bottom-aligned', !isBottomAligned(vp), `y=${vp.areaY} h=${vp.areaHeight}`)

  const a = advanceViewportForHistory(vp, 5)
  check('moves down by history lines', a.scrollAmount === 5 && a.state.areaY === 7, `y=${a.state.areaY}`)
  vp = a.state

  const b = advanceViewportForHistory(vp, 50)
  check('clamps to bottom', isBottomAligned(b.state), `y=${b.state.areaY} bottom=${b.state.areaY + b.state.areaHeight}`)
  vp = b.state

  const c = advanceViewportForHistory(vp, 4)
  check('frozen after bottom-aligned', c.scrollAmount === 0 && c.state.areaY === vp.areaY)

  const grown = applyDockHeight(vp, 10)
  check('dock growth re-sticks bottom', isBottomAligned(grown), `y=${grown.areaY} h=${grown.areaHeight}`)

  const seq = insertHistorySequence(
    createViewportState({ screenRows: 24, screenCols: 80, dockHeight: 6, areaY: 3 }),
    2,
  )
  check('ANSI sequence non-empty when shifting', seq.sequence.length > 0 && seq.scrollAmount === 2)
  check('sequence uses CSI', seq.sequence.includes('\x1b['))

  if (ok) console.log('\nSELFCHECK PASS: phase-3 viewport geometry (Codex insert_history math)')
  else {
    process.exitCode = 1
    console.error('\nSELFCHECK FAIL')
  }
}

main()
