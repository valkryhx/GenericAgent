/**
 * 键盘拦截优先级：审批卡片必须优先于提问卡片。
 *
 * 真机跑通后才注意到的问题（2026-11-14）。
 *
 * 为什么审批必须优先：
 * 审批是**安全门禁** —— 它决定某个写/执行操作能不能发生。提问只影响
 * 一次信息收集。如果提问卡片抢走按键，而审批卡片还挂在屏幕上，
 * 用户会被困住：看得见审批卡片但按不动它。
 *
 * 触发场景是真实存在的：模型先发起一个需要审批的写操作（审批卡片挂起），
 * 然后 /stop 或另一条路径又弹出一个提问 —— 两个 overlay 同时存在。
 *
 * 检查方式是 AST 级的：确认 `if (approvalPanel)` 在源码里出现在
 * `if (questionPanel)` 之前。纯文本匹配会被注释里的名字蒙混过去，
 * 所以这里取的是两个 `if` 语句在键盘处理函数内的相对位置。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const appSource = readFileSync(resolve(here, 'App.tsx'), 'utf8')

test('approval panel keyboard block comes before the question panel block', () => {
  const approvalIdx = appSource.indexOf('if (approvalPanel) {')
  const questionIdx = appSource.indexOf('if (questionPanel) {')
  assert.ok(approvalIdx > 0, '没找到 approvalPanel 的键盘块')
  assert.ok(questionIdx > 0, '没找到 questionPanel 的键盘块')
  assert.ok(
    approvalIdx < questionIdx,
    `审批卡片必须优先处理按键（安全门禁）。实际 approvalPanel@${approvalIdx} > questionPanel@${questionIdx}`,
  )
})

test('the question panel block still exists and handles escape', () => {
  const start = appSource.indexOf('if (questionPanel) {')
  // 提问块现在在审批块之后，下一个块是 permissionPanel
  const end = appSource.indexOf('if (permissionPanel) {', start)
  assert.ok(start > 0 && end > start, 'questionPanel 块的位置不对')
  const block = appSource.slice(start, end)
  assert.match(block, /key\.escape/, '提问卡片必须有 Esc 处理')
  assert.match(block, /key\.return/, '提问卡片必须有 Enter 处理')
})

// ---- 真机 bug 回归（2026-11-14）----
//
// 用户实测：「用 enter 表示选中 那如何 submit 呢 尤其是可以多选 我似乎
// 没法提交了」「other 那一行我无法输入」。两个都是接线层的问题，
// 逻辑层单测覆盖不到，所以这里对 App.tsx 做 AST 级检查。

test('the question block handles Space for multiSelect confirmation', () => {
  const start = appSource.indexOf('if (questionPanel) {')
  const end = appSource.indexOf('if (permissionPanel) {', start)
  const block = appSource.slice(start, end)
  assert.match(block, /questionPanelOnSpace/, '提问卡片必须接空格键（多选勾选）')
  assert.match(block, /rawInput === ' '/, '空格要用 rawInput 判定')
})

test('space handling is skipped while typing Other text', () => {
  // 编辑态的空格必须能打进文本，否则 Other 行永远打不出空格
  const start = appSource.indexOf('if (questionPanel) {')
  const end = appSource.indexOf('if (permissionPanel) {', start)
  const block = appSource.slice(start, end)
  const spaceIdx = block.indexOf("rawInput === ' '")
  assert.ok(spaceIdx > 0, '找不到空格分支')
  const before = block.slice(Math.max(0, spaceIdx - 200), spaceIdx)
  assert.match(before, /!questionPanel\.editingKey/, '空格分支必须排除编辑态')
})

test('Other text input reads rawInput, not the composer state', () => {
  // 根因：编辑态读 `input`（composer 的 useState），而提问卡打开时字符
  // 进不到 composer，input 恒为空串 → Other 行打字无效。
  const start = appSource.indexOf('if (questionPanel.editingKey)')
  assert.ok(start > 0, '找不到编辑态分支')
  // 编辑态分支到「离开编辑态后的 Esc 处理」为止
  const end = appSource.indexOf("rawInput === ' '", start)
  assert.ok(end > start, '找不到编辑态分支的结尾')
  const block = appSource.slice(start, end)
  assert.match(block, /textFromRawInput\(\s*rawInput/, 'Other 输入必须取自 rawInput')
  assert.doesNotMatch(
    block,
    /const typedChar = input\b/,
    '不能用 composer 的 input —— 提问卡打开时它恒为空串',
  )
})
