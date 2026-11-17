/**
 * 探针：工具调用行「有时黑、有时青」的根因 —— header 折叠是否成功。
 *
 * 现象（bug/1234.png）：两个 MCP 工具 chip 是深色，紧跟的两行是主题青色，
 * 看起来像「同一个工具调用，一次黑一次绿」。
 *
 * 根因（本探针验证的就是这一条）：
 *   formatAssistantText 只把**完整**的 GA 工具 header 折叠成 `● name(args)`：
 *       /…Tool:…`([^`]+)`…args:\n(`{3,})text\n([\s\S]*?)\n\2/      ← 带 args 围栏
 *       /…Tool:…`([^`]+)`…args:$/gm                                 ← header 单独成行
 *   两者都要求行尾出现 `args:`。header 一旦**没走完**（流式中途、
 *   stream-commit 切在行内、或 args fence 被拆到另一段），这两条都不匹配，
 *   原始协议文本 `🔨 Tool: \`mcp__exa__web_fetch_exa\`` 就交给了 Markdown。
 *   Markdown 把反引号内容当 inline code，用 `theme.code`（默认 cyan）上色。
 *
 * 于是同一个工具调用：
 *   - 折叠成功 → `● mcp__…(args)`，默认前景色（深色）
 *   - 折叠失败 → `🔨 Tool: <cyan>mcp__…</cyan>`，青色
 * 用户看到的「一个黑色一个绿色」就是这两种状态同时出现在屏幕上。
 *
 * 本探针是**对照实验**（硬规则 2），修复前后都成立：
 *   组 A（对照，完整 header）       → 必须无青色
 *   组 B（失败条件，header 截断）   → 修复前**出现**青色，修复后必须无青色
 * 组 B 是判定修复是否真的生效的那一组：它构造了「`args:` 还没到」的中间态。
 * 只跑组 A 会得到假绿（完整 header 本来就走折叠），只跑组 B 则没有对照。
 *
 * 修复位置：src/messageFormat.ts 的两条 header 折叠正则放宽为
 *   - `args:` 可选（`📥 args:` 尚未到达时也能折叠）
 *   - 闭反引号缺失时兜底，绝不把 `Tool:` 协议行交给 Markdown
 *
 * 为什么必须保留：这是「协议文本泄露到 Markdown 渲染层」的唯一可复现证据。
 * 任何一次改动只要让半截 header 重新掉回 Markdown，组 B 立刻亮红。
 *
 * 跑法：cd frontends/ink/ui && npx tsx probes/tool_header_fold_race.ts
 */
import { formatAssistantText } from '../src/messageFormat.js'
import { transcriptLines } from '../src/messageWindow.js'
import { getInkTheme } from '../src/theme.js'
import type { ChatMessage } from '../src/protocol.js'

const theme = getInkTheme('default')
const CYAN = theme.code!

const ARGS = ['````text', '{', '  "maxCharacters": 4000', '}', '````'].join('\n')
const RESULT = ['`````', '[Action] Calling MCP tool: mcp__exa__web_fetch_exa', '[Status] MCP success', '`````'].join('\n')

/** 组 A：完整 header，args 围栏紧随其后。 */
const COMPLETE = [
  '⏺ Tool: `mcp__exa__web_fetch_exa` args:',
  ARGS,
  RESULT,
].join('\n')

/** 组 B：header 尚未写完（流式中途 / 段被切开），行尾没有 `args:`。 */
const TRUNCATED = [
  '⏺ Tool: `mcp__exa__web_fetch_exa`',
  ARGS,
  RESULT,
].join('\n')

/**
 * 组 D：并行 MCP 批（用户实测「分别单独搜都正常，并行就一黑一青」）。
 *
 * 输入按 loop.py:_run_parallel_tools 的真实 yield 序列还原，工具名与参数取自
 * 用户报障那次的真实会话：
 *   %LOCALAPPDATA%\GenericAgent\projects\falcomm_new_mcp-.../
 *   session_e803a39a...jsonl（"mcp 搜搜 忽必烈"）
 * 结构特征：每个 header 后面都跟一个 close fence，于是折叠后留下
 * `● chip1 / fence / ● chip2 / fence`，第一个 fence 没有自己的 body，
 * 原来的 unwrap 规则（要求 body 含 [Action]/[Status]）匹配不到它，
 * 围栏存活并开启代码块，把第二个 chip 染成 theme.code。
 */
const PARALLEL = [
  '⏺ Tool: `mcp__tavily__tavily_search` args:',
  '````text',
  '{',
  '  "query": "忽必烈",',
  '  "max_results": 8,',
  '  "search_depth": "basic"',
  '}',
  '````',
  '`````',
  '⏺ Tool: `mcp__exa__web_search_exa` args:',
  '````text',
  '{',
  '  "query": "忽必烈 生平 元世祖 蒙古帝国 元朝建立者",',
  '  "numResults": 6',
  '}',
  '````',
  '`````',
  '[Action] Calling MCP tool: mcp__tavily__tavily_search',
  '[Status] MCP success',
  '[Action] Calling MCP tool: mcp__exa__web_search_exa',
  '[Status] MCP success',
].join('\n')

type Row = { text: string; colors: string[] }

function render(raw: string, expandedTools = false): Row[] {
  const formatted = formatAssistantText(raw, { expanded: expandedTools, streaming: false })
  const rows = transcriptLines(
    [{ id: 'p', role: 'assistant', text: formatted, done: true } as ChatMessage],
    { expandedTools, theme },
  )
  return rows.map(row => ({
    text: row.text,
    colors: (row.parts ?? []).map(part => `${part.color ?? '-'}${part.dimColor ? '+dim' : ''}`),
  }))
}

function cyanRows(rows: Row[]): Row[] {
  return rows.filter(row => row.colors.some(color => color.startsWith(CYAN)))
}

let failures = 0

for (const [label, raw] of [
  ['A 对照：完整 header', COMPLETE],
  ['B 失败条件：header 截断（`args:` 尚未到达）', TRUNCATED],
  ['D 并行 MCP 批（每个 header 后多一个 close fence）', PARALLEL],
] as const) {
  const rows = render(raw)
  const cyan = cyanRows(rows)
  const hit = cyan.length > 0
  const ok = !hit
  if (!ok) failures++
  console.log(`\n=== ${label} ===`)
  for (const row of rows) {
    const flag = cyan.includes(row) ? '  ← CYAN' : ''
    console.log(`  ${JSON.stringify(row.text).padEnd(72)} [${row.colors.join(',')}]${flag}`)
  }
  const chips = rows.filter(row => row.text.trimStart().startsWith('● ')).length
  console.log(`  ${ok ? '✔' : '✖'} 期望无青色，实际${hit ? '有' : '无'}（折叠出 ${chips} 个 chip）`)
}

// 展开态（Ctrl+O）：args JSON 里缩进 >=4 空格的行会被 Markdown 当代码块，
// 这是与折叠无关的第二条青色来源，一并锁住。
{
  const rows = render(COMPLETE, true)
  const cyan = cyanRows(rows)
  console.log('\n=== C 展开态（Ctrl+O）===')
  for (const row of rows) {
    const flag = cyan.includes(row) ? '  ← CYAN' : ''
    console.log(`  ${JSON.stringify(row.text).padEnd(72)} [${row.colors.join(',')}]${flag}`)
  }
  console.log(`  展开态青色行 ${cyan.length} 条（args JSON 缩进行）`)
}

console.log('\n=== 判决 ===')
if (failures > 0) {
  console.log(`✖ ${failures} 个对照组出现了青色行：半截 header 又掉回 Markdown（${CYAN}）`)
  process.exitCode = 1
} else {
  console.log('✔ 完整 header 与截断 header 都折叠成 ● chip，均无青色')
  console.log('  修复前：截断 header（`args:` 未到达）落到 Markdown inline code 染成 cyan，')
  console.log('  同一个工具调用因此「有时黑、有时青」—— 取决于流被切在哪儿，与是否 MCP 无关。')
}