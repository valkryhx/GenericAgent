"""给 App.test.ts 中依赖 inline 默认的测试显式设置 GA_INK_MOUSE=off。

默认模式已改为 full（备用屏），这 5 个测试验证的是 inline 行为，
仍然有效，但必须显式声明模式，不能依赖默认值。
"""
from pathlib import Path

p = Path(r"D:\git_repos\GenericAgent\frontends\ink\ui\src\App.test.ts")
text = p.read_text(encoding="utf-8")

# 每个目标测试：在 test(...) 之后的第一行插入显式模式设置 + 恢复
TARGETS = [
    "test('App default inline mode does not paint a GenericAgent status header above the transcript', async () => {\n"
    "  // Status belongs in the bottom activity chrome; a live header after <Static>\n"
    "  // looks like \"GenericAgent running\" jammed into the middle of output.\n"
    "  delete process.env.GA_INK_MOUSE\n",

    "test('App does not enter the alternate screen before the first Ink frame is written', async () => {\n",

    "test('App clears the inline live input block before exiting on Ctrl+C', async () => {\n",

    "test('App writes completed transcript to static terminal scrollback instead of the live viewport', async () => {\n",
]

MARKER = "  // Inline-mode behaviour: pin the mode explicitly. The shipped default is now\n"
MARKER += "  // fullscreen (CC 319 parity), where this branch is not taken at all.\n"
MARKER += "  process.env.GA_INK_MOUSE = 'off'\n"

count = 0
for head in TARGETS:
    if head not in text:
        print(f"SKIP (not found): {head.splitlines()[0][:70]}")
        continue
    if "process.env.GA_INK_MOUSE = 'off'" in text[text.index(head):text.index(head) + 600]:
        print(f"SKIP (already patched): {head.splitlines()[0][:70]}")
        continue
    if head == TARGETS[0]:
        new_head = head.replace("  delete process.env.GA_INK_MOUSE\n", MARKER)
    else:
        new_head = head + MARKER
    text = text.replace(head, new_head, 1)
    count += 1

p.write_text(text, encoding="utf-8", newline="\n")
print(f"patched {count} 处")
