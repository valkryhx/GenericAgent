/**
 * 运行中状态行要有**跳动**的图标。
 *
 * ## 用户实测报的缺陷（2026-11-14）
 *
 * 「claudecode 的 `✽ Nucleating… (2m 48s · ↓ 12.3k tokens · thinking more with
 *  xhigh effort)` 很有动态性，尤其是最前面那个跳动的图标，很明显用户知道
 *  claudecode 正在运行。我希望你也能在 ga 中实现这种细微但有明显作用的动态提示」
 *
 * 现状：`glyphs.ts` 里**已经**有 `spinnerFrames`（CC 的 `lt = · ✢ * ✶ ✻ ✽`），
 * 但全仓库零引用 —— `runningStatusSegments` 把 `✻` 写死了。
 *
 * 也就是说：帧表早就抄来了，只是从来没用。这批测试锁住「真的动起来」。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import {
  spinnerFrameAt,
  spinnerFrameCount,
  runningStatusSegments,
} from './activityStatus.js'
import { GLYPHS } from './glyphs.js'

test('spinner has more than one frame', () => {
  assert.ok(spinnerFrameCount() > 1, '只有一帧就等于不动')
})

test('frame index wraps around', () => {
  const n = spinnerFrameCount()
  assert.equal(spinnerFrameAt(0), spinnerFrameAt(n))
  assert.equal(spinnerFrameAt(1), spinnerFrameAt(n + 1))
})

test('every frame is a different glyph', () => {
  const seen = new Set<string>()
  for (let i = 0; i < spinnerFrameCount(); i += 1) seen.add(spinnerFrameAt(i))
  assert.equal(seen.size, spinnerFrameCount(), '帧表里有重复字形，动画会卡顿')
})

test('frames come from the CC-derived glyph table', () => {
  // 不能自己另起一套：glyphs.ts 里的 spinnerFrames 是 CC 319 的实测抄本
  for (let i = 0; i < spinnerFrameCount(); i += 1) {
    assert.ok(
      (GLYPHS.spinnerFrames as readonly string[]).includes(spinnerFrameAt(i)),
      `第 ${i} 帧不在 GLYPHS.spinnerFrames 里`,
    )
  }
})

test('the running status line animates its leading glyph', () => {
  const a = runningStatusSegments(12, 'Nucleating', null, 0)
  const b = runningStatusSegments(12, 'Nucleating', null, 1)
  assert.notEqual(a[0]!.text[0], b[0]!.text[0], '不同帧的开头图标必须不同')
})

test('the animated glyph replaces the hardcoded star', () => {
  // 帧 4 是 ✻，与旧写死值一致 —— 保证换了实现观感不变、只是动起来
  const seg = runningStatusSegments(12, 'Nucleating', null, 4)
  assert.ok(seg[0]!.text.startsWith('✻'), `帧 4 应是 ✻，实际 ${seg[0]!.text[0]}`)
})

test('the rest of the line is unchanged by the frame index', () => {
  const a = runningStatusSegments(12, 'Nucleating', null, 0)
  const b = runningStatusSegments(12, 'Nucleating', null, 5)
  assert.equal(a.slice(1).map(s => s.text).join(''), b.slice(1).map(s => s.text).join(''))
  assert.equal(a[0]!.text.slice(1), b[0]!.text.slice(1), '只有图标在变，文字不动')
})

test('an omitted frame index falls back to a stable glyph', () => {
  // 不传帧索引时不能崩，也不该随机跳（会让测试和快照不稳定）
  const seg = runningStatusSegments(12, 'Nucleating')
  assert.ok(seg[0]!.text.length > 0)
  assert.equal(seg[0]!.text, runningStatusSegments(12, 'Nucleating')[0]!.text)
})
