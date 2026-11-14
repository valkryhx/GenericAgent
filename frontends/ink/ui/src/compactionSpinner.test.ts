/**
 * 压缩行用 CC 的「思考帧」`∴ ∷ ∵ ∷`，而不是普通 spinner。
 *
 * ## 用户实测报的缺陷（2026-11-14）
 *
 * 「ga 的自动压缩目前没有像 cc319 那样的 ux 上指示，而是突然卡住不动」
 *
 * 上一轮已经把普通运行行做成跳动图标，但压缩是一个**不同**的状态：
 * CC 在压缩时换的是另一套帧表（`tLr = [∴, ∷, ∵, ∷]`，三角形点阵来回摆动），
 * 帧间隔也从 100ms 改成 150ms。用户能一眼看出「这不是普通运行，是在压缩」。
 *
 * GA 的 `GLYPHS.thinkingFrames` 早就抄了这套（glyphs.ts:20），但全仓库零引用 ——
 * 和上次 `spinnerFrames` 一样的坑。这批测试锁住「压缩行真的用思考帧」。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import {
  compactionFrameAt,
  compactionFrameCount,
  compactionStatusSegments,
} from './activityStatus.js'
import { GLYPHS } from './glyphs.js'

test('compaction frames are the CC thinking frames, not the spinner frames', () => {
  for (let i = 0; i < compactionFrameCount(); i += 1) {
    assert.ok(
      (GLYPHS.thinkingFrames as readonly string[]).includes(compactionFrameAt(i)),
      `第 ${i} 帧不在 GLYPHS.thinkingFrames 里：${compactionFrameAt(i)}`,
    )
  }
  // 两套帧表必须真的不同，否则「压缩看起来不一样」这个目的就落空了
  const compaction = new Set(Array.from({ length: compactionFrameCount() }, (_, i) => compactionFrameAt(i)))
  const spinner = new Set(GLYPHS.spinnerFrames as readonly string[])
  const overlap = [...compaction].filter(glyph => spinner.has(glyph))
  assert.deepEqual(overlap, [], `压缩帧与普通帧重了：${overlap.join(' ')}`)
})

test('compaction frames wrap around', () => {
  const n = compactionFrameCount()
  assert.equal(compactionFrameAt(0), compactionFrameAt(n))
  assert.equal(compactionFrameAt(1), compactionFrameAt(n + 1))
  assert.equal(compactionFrameAt(-1), compactionFrameAt(n - 1))
})

test('the compaction line animates its leading glyph', () => {
  const a = compactionStatusSegments(3, 'Compacting conversation', 0)
  const b = compactionStatusSegments(3, 'Compacting conversation', 1)
  assert.notEqual(a[0]!.text[0], b[0]!.text[0], '不同帧的开头图标必须不同')
})

test('the compaction line keeps its label and elapsed time', () => {
  const seg = compactionStatusSegments(42, 'Compacting conversation', 0)
  const text = seg.map(s => s.text).join('')
  assert.ok(text.includes('Compacting conversation'), `标签丢失：${text}`)
  assert.ok(text.includes('42s'), `计时丢失：${text}`)
})

test('a reduced-motion run holds one frame', () => {
  // CC: `reducedMotion ? " " : tLr[frame % len]`，且帧间隔传 null 不起定时器。
  // GA 这里只保证「固定同一个字」，不动定时器。
  const a = compactionStatusSegments(3, 'Compacting conversation', 0, { reducedMotion: true })
  const b = compactionStatusSegments(3, 'Compacting conversation', 3, { reducedMotion: true })
  assert.equal(a[0]!.text[0], b[0]!.text[0], 'reducedMotion 下图标不该跳')
  assert.ok((GLYPHS.thinkingFrames as readonly string[]).includes(a[0]!.text[0]),
    'reducedMotion 也要用思考帧里的字')
})
