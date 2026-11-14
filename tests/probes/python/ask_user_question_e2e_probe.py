# -*- coding: utf-8 -*-
"""PTY 驱动真实 `./ga ink`，按下发输入，抓整段屏幕字节。

用途：验证 ask_user_question 端到端 ——
卡片真的弹出来了吗？按键真的被卡片接住了吗？用户提交的答案真的
传到 LLM 那边了吗？

用法：
  python tests/probes/python/ask_user_question_e2e_probe.py <秒数> <prompt文件>

按键脚本从环境变量 __KEYS__ 读，格式 "S:<秒>,K:<文本>"，用 ;; 分隔。
文本用 python 转义（\r = Enter）。

产出：tests/probes/captures/ask_q_e2e.txt（原始字节，ANSI 保留）
      tests/probes/captures/ask_q_e2e.clean.txt（去 ANSI 的可见文本）
"""
import os
import re
import sys
import time

from winpty import PtyProcess

ROOT = r"D:\git_repos\GenericAgent"
OUT_DIR = os.path.join(ROOT, "tests", "probes", "captures")

SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0

env = dict(os.environ)
# 清掉嵌套标记：本进程可能跑在别的 agent 里，那些环境变量会让被测进程走降级路径
for k in [k for k in env if k.upper().startswith(("CLAUDE", "ANTHROPIC"))]:
    env.pop(k, None)
env["PYTHONPATH"] = ROOT
env["PYTHONIOENCODING"] = "utf-8"
env["PYTHONUTF8"] = "1"
# 不想要全屏模式干扰抓包（全屏会重绘，diff 更难读）
env.setdefault("GA_TUI_FULLSCREEN", "0")

p = PtyProcess.spawn(
    ["bash", "-lc", "cd /d/git_repos/GenericAgent && ./ga ink"],
    dimensions=(40, 120),
    env=env,
    cwd=ROOT,
)

chunks = []
t0 = time.time()


def reader_until(deadline):
    while time.time() < deadline:
        try:
            raw = p.read(65536)
        except EOFError:
            chunks.append((time.time() - t0, "<EOF>"))
            return False
        if raw:
            chunks.append((time.time() - t0, raw))
        else:
            time.sleep(0.02)
    return True


# 第一阶段：等界面起来
reader_until(t0 + 12.0)

# 第二阶段：按脚本注入
#
# 两个坑（本探针实测踩过）：
# 1. `raw_text.encode("utf-8").decode("unicode_escape")` 会把 UTF-8 字节
#    当成 latin-1 逐字节再转义 → 中文变 mojibake。正确做法是**直接**用
#    原字符串，只在需要 `\r` 这类转义时手工替换。
# 2. 单独写 `\r` 会被 Ink 的输入层当成「复制」之类的组合键。发送整行时
#    必须把文本和回车**一次 write**，让 Ink 当成 paste+enter。
script = os.environ.get("__KEYS__", "")
if script:
    for item in script.split(";;"):
        item = item.strip()
        if not item:
            continue
        at_s, raw_text = item.split(":", 1)
        at = float(at_s)
        delay = at - (time.time() - t0)
        if delay > 0:
            time.sleep(delay)
        # 只解释显式写出的 \r / \n 转义，其余字符原样保留
        text = raw_text.replace("\\r", "\r").replace("\\n", "\n")
        chunks.append((time.time() - t0, "\n<<<INJECT %r>>>\n" % text))
        try:
            p.write(text)
        except Exception as exc:
            chunks.append((time.time() - t0, "<<<WRITE FAILED %r>>>" % exc))

# 第三阶段：继续收尾
reader_until(t0 + SECONDS)

try:
    p.terminate(force=True)
except Exception:
    pass

text = "".join(c for _, c in chunks if c != "<EOF>")
os.makedirs(OUT_DIR, exist_ok=True)
raw_path = os.path.join(OUT_DIR, "ask_q_e2e.txt")
with open(raw_path, "w", encoding="utf-8", newline="\n") as f:
    f.write(text)

# 去 ANSI 留可见文本：注意 [0-9;?]* —— 光标隐藏/显示序列带 ?，
# 漏掉它会把多帧粘成一行，读起来像 bug。
clean = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)
clean = re.sub(r"\x1b\][^\x07]*\x07", "", clean)  # OSC
clean_path = os.path.join(OUT_DIR, "ask_q_e2e.clean.txt")
with open(clean_path, "w", encoding="utf-8", newline="\n") as f:
    f.write(clean)

print("chunks      :", len(chunks))
print("chars raw   :", len(text))
print("chars clean :", len(clean))
print("raw   ->", raw_path)
print("clean ->", clean_path)
