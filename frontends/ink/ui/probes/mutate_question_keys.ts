/**
 * 反向确认：把多选/空格/Other 输入的实现改坏，确认对应测试真的会红。
 *
 * AGENTS.md §8.5 要求：测试变绿不能证明有效，必须把实现改坏再跑。
 * 这里针对的是 2026-11-14 用户实测报的两个 bug：
 *   1. 多选页无法提交（Enter 只 toggle 不推进）
 *   2. Other 行无法输入（读 composer 的 input 而不是 rawInput）
 */
import { readFileSync, writeFileSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const SRC = resolve(here, '..', 'src')
const PANEL = resolve(SRC, 'questionPanel.ts')
const APP = resolve(SRC, 'App.tsx')
const TEST_ACTIONS = 'src/questionPanelActions.test.ts'

type Mutation = {
  name: string
  file: string
  from: string
  to: string
  expectRed: string
}

const MUTATIONS: Mutation[] = [
  {
    name: '多选页 Enter 退回旧行为（只 toggle 不推进）',
    file: PANEL,
    from: `  if (question.multiSelect) {
    // 多选：Enter 只负责前进。勾选交给空格（questionPanelOnSpace）。
    // 未勾选任何项也前进 —— 与确认页允许提交同一个理由。
    return { type: 'advance', next: movePage(panel, 1) }
  }`,
    to: `  if (question.multiSelect) {
    const t = toggleMultiOption(panel, panel.currentIndex, row!.label)
    return { type: 'advance', next: t }
  }`,
    expectRed: 'Enter on a multiSelect page advances to the next page',
  },
  {
    name: '空格不切换多选',
    file: PANEL,
    from: `    const toggled = toggleMultiOption(panel, panel.currentIndex, row.label)`,
    to: `    const toggled = panel`,
    expectRed: 'Space toggles a multiSelect option',
  },
  {
    name: '空格后不移动光标（无法连续勾选）',
    file: PANEL,
    from: `    const next = Math.min(total - 1, toggled.optionIndex + 1)`,
    to: `    const next = toggled.optionIndex`,
    expectRed: 'Space moves down after toggling',
  },
  {
    name: 'textFromRawInput 放行转义序列',
    file: PANEL,
    from: `  if (rawInput.startsWith('\\x1b')) return ''`,
    to: `  if (false) return ''`,
    expectRed: 'textFromRawInput rejects escape sequences',
  },
  {
    name: 'textFromRawInput 放行控制字符',
    file: PANEL,
    from: `  if (rawInput.length === 1 && rawInput.charCodeAt(0) < 0x20) return ''`,
    to: `  if (false) return ''`,
    expectRed: 'textFromRawInput rejects bare control characters',
  },
  {
    name: 'App 退回读 composer 的 input（Other 无法输入）',
    file: APP,
    from: `        const typedChar = textFromRawInput(rawInput, key)`,
    to: `        const typedChar = input && !input.startsWith('\\x1b') ? input : ''`,
    expectRed: 'Other text input reads rawInput',
  },
  {
    name: 'App 不接空格键（多选无法确认）',
    file: APP,
    from: `      if (!questionPanel.editingKey && rawInput === ' ') {`,
    to: `      if (false) {`,
    expectRed: 'the question block handles Space',
  },
  {
    name: 'App 空格分支不排除编辑态（打不出空格）',
    file: APP,
    from: `      if (!questionPanel.editingKey && rawInput === ' ') {`,
    to: `      if (rawInput === ' ') {`,
    expectRed: 'space handling is skipped while typing Other text',
  },
]

function runTests(): { ok: boolean; out: string } {
  try {
    const out = execFileSync('npx', ['tsx', '--test', 'src/questionPanelActions.test.ts', 'src/keyboardPriority.test.ts'], {
      cwd: resolve(here, '..'),
      encoding: 'utf8',
      stdio: 'pipe',
      shell: true,
    })
    return { ok: true, out }
  } catch (err) {
    const e = err as { stdout?: string; stderr?: string }
    return { ok: false, out: `${e.stdout ?? ''}${e.stderr ?? ''}` }
  }
}

const results: string[] = []
let bad = 0

for (const m of MUTATIONS) {
  const original = readFileSync(m.file, 'utf8')
  if (!original.includes(m.from)) {
    results.push(`⚠️  SKIP (anchor missing): ${m.name}`)
    bad++
    continue
  }
  try {
    writeFileSync(m.file, original.replace(m.from, m.to), 'utf8')
    const { ok, out } = runTests()
    const redHit = !ok && out.includes(m.expectRed)
    if (!redHit) bad++
    results.push(`${redHit ? '✅' : '❌'} ${m.name} -> ${m.expectRed}`)
  } finally {
    writeFileSync(m.file, original, 'utf8')
  }
}

console.log('=== reverse confirmation (multiSelect / space / Other) ===')
for (const r of results) console.log(r)
console.log('')
console.log(bad === 0 ? 'ALL MUTATIONS CAUGHT' : `${bad} MUTATION(S) NOT CAUGHT`)
process.exit(bad === 0 ? 0 : 1)
