"""回滚 writeCoalescer 实验：删除 3 个未跟踪文件（逐个 os.remove，不递归）。"""
import os
import subprocess
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[3])

base = str(Path(REPO) / "frontends" / "ink" / "ui" / "src")
targets = [
    "writeCoalescer.ts",
    "writeCoalescerTypes.ts",
    "writeCoalescer.test.ts",
]
for name in targets:
    p = os.path.join(base, name)
    if os.path.exists(p):
        os.remove(p)
        print("removed", name)
    else:
        print("absent ", name)

print(subprocess.run(["git", "checkout", "--", "frontends/ink/ui/src/main.tsx"],
                     cwd=REPO, capture_output=True, text=True))
