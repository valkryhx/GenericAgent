"""临时把 streamCommit 的 live tail 调到极大（= 永不提交），跑 A/B。

用途：验证「撑满终端后的 ±1 来回摆动」是否由 Static 提交造成。
改完必须立刻还原 —— 本脚本会自己写回。
"""
import subprocess
import sys
from pathlib import Path

SRC = Path(r"D:\git_repos\GenericAgent\frontends\ink\ui\src\streamCommit.ts")
ORIG = "export const DEFAULT_STREAM_LIVE_TAIL_LINES = 8"
TEST = "export const DEFAULT_STREAM_LIVE_TAIL_LINES = 100000"
MARK = "GA_AB_NOCOMMIT"

action = sys.argv[1] if len(sys.argv) > 1 else "on"

text = SRC.read_text(encoding="utf-8")
if action == "on":
    if TEST in text:
        print("already on")
        sys.exit(0)
    assert ORIG in text, "找不到原始常量行"
    SRC.write_text(text.replace(ORIG, TEST + f" // {MARK}"), encoding="utf-8", newline="\n")
    print("patched: tail=100000 (commit disabled)")
elif action == "off":
    if TEST not in text:
        print("already off")
        sys.exit(0)
    SRC.write_text(
        text.replace(TEST + f" // {MARK}", ORIG),
        encoding="utf-8", newline="\n",
    )
    print("reverted: tail=8")
else:
    print("usage: on|off")
    sys.exit(2)

out = subprocess.run(["git", "diff", "--stat", "--", str(SRC)],
                     cwd=r"D:\git_repos\GenericAgent",
                     capture_output=True, text=True)
print(out.stdout.strip() or "(no diff)")
