/**
 * 帧模型必须包含**所有**屏幕上的文本行。
 *
 * ## 用户实测报的缺陷（2026-11-14）
 *
 * 「目前 ga 的这一行似乎不能复制，而 claudecode 的 ux 界面所有元素都可以复制，
 *  这一点也是 ga 必须优化的，不然这些细微之处会让用户觉得非常难受，
 *  居然有些地方不能复制？？？」
 *
 * 根因：`buildFramePlan` 支持 `bandLines` / `belowLines`，但 App.tsx 调用时
 * **一个都没传**。于是 transcript 与 composer 之间的整段（活动行
 * 「✻ Hyperspacing (12s · ↓ 1.2k tokens)」、goal 卡、工作流状态条、重试横幅、
 * 子代理通知）在帧模型里全是空串。
 *
 * 复制走的是 `selectedFrameText(range, frameRowsRef.current)` —— 帧里是空串，
 * 取出来自然什么都没有。用户拖过去只看到高亮、粘贴出来是空的。
 *
 * 这批测试锁两件事：
 * 1. `buildFramePlan` 拿到 bandLines/belowLines 时必须把它们放进行
 * 2. App.tsx 必须真的把这两组内容传进去（AST 级检查，纯文本匹配会被注释骗过）
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { buildFramePlan } from './framePlan.js'

const here = dirname(fileURLToPath(import.meta.url))
const appSource = readFileSync(resolve(here, 'App.tsx'), 'utf8')

test('band lines land in the rows between transcript and composer', () => {
  const rows = buildFramePlan({
    canvasColumns: 40,
    rows: 12,
    headerRows: 1,
    header: 'GenericAgent running',
    transcriptRows: 4,
    transcriptLines: ['a', 'b', 'c', 'd'],
    composerRow: 8,
    composerLines: [{ gutter: '> ', text: 'hello' }],
    bandLines: ['* Hyperspacing (12s)', 'goal: do the thing'],
  })
  assert.equal(rows[5]!.text, '* Hyperspacing (12s)', '活动行必须进帧模型')
  assert.equal(rows[6]!.text, 'goal: do the thing', 'goal 卡必须进帧模型')
})

test('an activity line is actually selectable out of the frame', () => {
  // 端到端意味的检查：行在帧里，选中它就能取到文字
  const rows = buildFramePlan({
    canvasColumns: 40,
    rows: 10,
    headerRows: 0,
    transcriptRows: 3,
    transcriptLines: ['x', 'y', 'z'],
    composerRow: 5,
    composerLines: [{ gutter: '> ', text: '' }],
    bandLines: ['* Nucleating... (2m 48s)'],
  })
  assert.equal(rows[3]!.text, '* Nucleating... (2m 48s)')
  assert.ok(rows[3]!.text.includes('Nucleating'), '取得到文字才谈得上复制')
})

test('below lines land under the composer', () => {
  const rows = buildFramePlan({
    canvasColumns: 40,
    rows: 10,
    headerRows: 0,
    transcriptRows: 2,
    transcriptLines: ['a', 'b'],
    composerRow: 3,
    composerLines: [{ gutter: '> ', text: 'hi' }],
    belowLines: ['Enter to send', 'esc to interrupt'],
  })
  assert.equal(rows[4]!.text, 'Enter to send')
  assert.equal(rows[5]!.text, 'esc to interrupt')
})

// ---- 接线：App.tsx 必须把内容真的传进去 ----

test('App passes bandLines to buildFramePlan', () => {
  const start = appSource.indexOf('buildFramePlan({')
  assert.ok(start > 0, '找不到 buildFramePlan 调用')
  const end = appSource.indexOf('})', start)
  const call = appSource.slice(start, end)
  assert.match(
    call,
    /bandLines/,
    '不传 bandLines 的话，活动行/goal 卡在帧里是空串 → 复制为空',
  )
})

test('App passes belowLines to buildFramePlan', () => {
  const start = appSource.indexOf('buildFramePlan({')
  const end = appSource.indexOf('})', start)
  const call = appSource.slice(start, end)
  assert.match(call, /belowLines/, 'composer 下方的提示行也要可复制')
})

test('the band row count stays consistent with the composer anchor', () => {
  // band 的内容可以 best-effort，但**行数**必须与 composerRow 对齐，
  // 否则 composer 的地址会漂 → 选区落错行。
  const rows = buildFramePlan({
    canvasColumns: 40,
    rows: 14,
    headerRows: 1,
    transcriptRows: 4,
    transcriptLines: ['a', 'b', 'c', 'd'],
    composerRow: 9,
    composerLines: [{ gutter: '> ', text: 'x' }],
    // 只给 2 行内容，但 band 区有 4 行空间（1+4=5 .. 9）
    bandLines: ['line1', 'line2'],
  })
  assert.equal(rows[5]!.text, 'line1')
  assert.equal(rows[6]!.text, 'line2')
  assert.equal(rows[7]!.text, '', '不足的部分留空白，不能挤动 composer')
  assert.equal(rows[8]!.text, '')
  assert.equal(rows[9]!.text, '> x', 'composer 必须还在锚点行上')
})

// ---- 选区跨视口（用户实测：拖不出屏幕就选不到屏幕外内容）----

test('copy reads the content frame, not the visible one', () => {
  // 只在可见帧里取文本，滚出去的行就复制不到。
  // 用户原话：「无法选出屏幕外需要滑动的区域」。
  const start = appSource.indexOf('const copyCurrentSelection')
  const end = appSource.indexOf('const handleTerminalInput', start)
  const block = appSource.slice(start, end)
  assert.match(block, /contentFrameRowsRef\.current/, '复制必须读内容帧')
  assert.doesNotMatch(
    block,
    /const frame = frameRowsRef\.current/,
    '不能再用可见帧取文本 —— 屏幕外的行不在里面',
  )
})

test('dragging past the viewport scrolls and extends the selection', () => {
  const start = appSource.indexOf('const handleMouseEvent')
  const end = appSource.indexOf('const handleTerminalInput', start)
  const block = appSource.slice(start, end)
  assert.match(block, /autoScrollDirection/, '拖到边缘必须触自动滚动')
  assert.match(block, /setTranscriptScrollOffset/, '必须真的滚')
  assert.match(block, /focusContentRowAtEdge/, '焦点要落在滚完之后的边缘行')
})

test('selection points are stored in content coordinates', () => {
  const start = appSource.indexOf('const contentPointFromMouse')
  assert.ok(start > 0, '找不到内容坐标换算')
  const block = appSource.slice(start, start + 600)
  assert.match(block, /screenRowToContentRow/, '鼠标屏幕点必须换算成内容行')
})

test('the highlight is projected back to screen coordinates', () => {
  const start = appSource.indexOf('const selectionRange = useMemo')
  const end = appSource.indexOf('const frameRowSpans = useMemo', start)
  const block = appSource.slice(start, end)
  assert.match(
    block,
    /projectRangeToScreen/,
    '高亮必须投影回屏幕坐标，否则滚出视口的部分会画到 composer 上',
  )
})
