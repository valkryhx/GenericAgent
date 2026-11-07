"""重复跑 resizeReflow 测试，量化在当前改动下的失败率。"""
import subprocess
import sys
from pathlib import Path

UI = Path(r"D:\git_repos\GenericAgent\frontends\ink\ui")
runs = int(sys.argv[1]) if len(sys.argv) > 1 else 5

passes = 0
fails = 0
for i in range(runs):
    r = subprocess.run(
        ["npx", "tsx", "--test", "src/resizeReflow.test.ts"],
        cwd=str(UI), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    out = r.stdout + r.stderr
    ok = "fail 0" in out
    if ok:
        passes += 1
    else:
        fails += 1
        for line in out.splitlines():
            if "AssertionError" in line:
                print(f"  run {i + 1}: {line.strip()}")
    print(f"  run {i + 1}: {'PASS' if ok else 'FAIL'}")

print(f"\n{passes} pass / {fails} fail  (共 {runs} 次)")
