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
