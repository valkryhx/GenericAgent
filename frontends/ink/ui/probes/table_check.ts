/** 验证表格渲染：ASCII、CJK、对齐、空表。 */
import { renderMarkdownLines } from '../src/markdownRender.ts'
import stringWidth from 'string-width'

const cases: Array<[string, string]> = [
  ['基础 ASCII', '| Name | Age |\n| --- | --- |\n| Alice | 30 |\n| Bob | 4 |'],
  ['中文 + 对齐', '| 指标 | 说明 | 值 |\n| :--- | :---: | ---: |\n| 延迟 | 平均响应时间 | 120ms |\n| 吞吐 | 每秒请求数 | 5000 |'],
  ['单列', '| A |\n| --- |\n| 1 |\n| 2 |'],
  ['无表体', '| H1 | H2 |\n| --- | --- |'],
  ['行内样式', '| 项 | 状态 |\n| --- | --- |\n| **重要** | `ok` |'],
]

for (const [name, src] of cases) {
  const lines = renderMarkdownLines(src)
  console.log(`\n===== ${name} =====`)
  for (const line of lines) {
    console.log(line.text)
  }
  // 每行宽度应一致（对齐检查）
  const widths = lines.map(l => stringWidth(l.text))
  const uniq = [...new Set(widths)]
  console.log(`  [宽度] ${uniq.join(', ')}${uniq.length === 1 ? '  一致 ✓' : '  不一致 ✗'}`)
}

// 非表格内容不能受影响
console.log('\n===== 回归：非表格 =====')
for (const line of renderMarkdownLines('- a\n- b\n\n普通段落 **粗体**')) {
  console.log(line.text)
}
