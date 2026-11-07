"""把 App.tsx 里「是否全屏」判断从 mouseMode==='full' 换成 fullscreen。

mouseMode 现在有三档：off / scroll / full。scroll 与 full 都是全屏，
只有鼠标上报量不同。因此布局/滚动相关判断必须用 fullscreen。

排除：真正与「鼠标上报档位」相关的分支（parseMouseWheel/parseMouseEvent 的处理）
保持 mouseMode==='full' —— 滚轮在 scroll 档也要处理，按键在 scroll 档不需要。
"""
import re
from pathlib import Path

p = Path(r"D:\git_repos\GenericAgent\frontends\ink\ui\src\App.tsx")
text = p.read_text(encoding="utf-8")

before = text

# 这些是布局/滚动/全屏判断 -> fullscreen
replacements = [
    ("if (mouseMode !== 'full') {\n      return\n    }", "if (!fullscreen) {\n      return\n    }"),
    ("if (mouseMode !== 'full') setTranscriptScrollOffset(0)", "if (!fullscreen) setTranscriptScrollOffset(0)"),
    ("const mcpBannerReserved = mouseMode === 'full' &&", "const mcpBannerReserved = fullscreen &&"),
    ("headerRows: mouseMode === 'full' ? 1 : 0,", "headerRows: fullscreen ? 1 : 0,"),
    ("const messageRowsForCursor = mouseMode === 'full' ? metrics.messageRows : inlineMessageRows",
     "const messageRowsForCursor = fullscreen ? metrics.messageRows : inlineMessageRows"),
    ("const historyReplacementScrollOffset = mouseMode === 'full' &&",
     "const historyReplacementScrollOffset = fullscreen &&"),
    ("transcriptRowsRef.current = mouseMode === 'full'", "transcriptRowsRef.current = fullscreen"),
    ("const liveViewportRows = mouseMode === 'full' ? metrics.rows :",
     "const liveViewportRows = fullscreen ? metrics.rows :"),
    ("if (mouseMode !== 'full') return", "if (!fullscreen) return"),
    ("{mouseMode === 'full' ? null : (", "{fullscreen ? null : ("),
]

for old, new in replacements:
    if old not in text:
        print(f"SKIP (not found): {old[:60]}")
        continue
    text = text.replace(old, new)

# 渲染消息视口：if (mouseMode === 'full') {
text = text.replace(
    "  const renderMessageViewport = () => {\n    if (mouseMode === 'full') {",
    "  const renderMessageViewport = () => {\n    if (fullscreen) {",
)

# 滚动/滚轮/按键处理：滚轮在 scroll 档也要生效，因此用 mouseMode !== 'off'
text = text.replace(
    "  const handleTerminalInput = (rawInput: string, key: InputKey) => {\n    if (mouseMode === 'full') {",
    "  const handleTerminalInput = (rawInput: string, key: InputKey) => {\n    // Wheel works in every fullscreen mode; button/drag events only exist in `full`.\n    if (fullscreen) {",
)

text = text.replace("if (mouseMode === 'full' && key.pageUp) {", "if (fullscreen && key.pageUp) {")
text = text.replace("if (mouseMode === 'full' && key.pageDown) {", "if (fullscreen && key.pageDown) {")
text = text.replace("if (mouseMode === 'full' && key.end) {", "if (fullscreen && key.end) {")

# static transcript 与 active 计算
text = text.replace(
    "  const staticTranscript = useMemo(() => (\n    mouseMode === 'full'",
    "  const staticTranscript = useMemo(() => (\n    fullscreen",
)
text = text.replace(
    "  const activeTranscriptRows = useMemo(() => (\n    mouseMode === 'full'",
    "  const activeTranscriptRows = useMemo(() => (\n    fullscreen",
)
text = text.replace("  ), [expandedTools, messagePartition.staticMessages, metrics.canvasColumns, mouseMode, theme])",
                  "  ), [expandedTools, messagePartition.staticMessages, metrics.canvasColumns, fullscreen, theme])")
text = text.replace("  ), [expandedTools, messagePartition.activeMessages, metrics.canvasColumns, mouseMode, staticTranscript.channel, theme])",
                  "  ), [expandedTools, messagePartition.activeMessages, metrics.canvasColumns, fullscreen, staticTranscript.channel, theme])")

p.write_text(text, encoding="utf-8", newline="\n")
print(f"changed: {before != text}")

# 检查残留
leftover = [m.start() for m in re.finditer(r"mouseMode === 'full'|mouseMode !== 'full'", text)]
print(f"剩余 mouseMode==='full' 判断: {len(leftover)} 处")
for idx in leftover:
    line_no = text[:idx].count("\n") + 1
    line = text.splitlines()[line_no - 1].strip()
    print(f"  L{line_no}: {line[:100]}")
