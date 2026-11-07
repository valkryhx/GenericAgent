"""回滚 writeCoalescer 实验：删除 3 个未跟踪文件（逐个 os.remove，不递归）。"""
import os
import subprocess

base = r"D:\git_repos\GenericAgent\frontends\ink\ui\src"
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
                     cwd=r"D:\git_repos\GenericAgent", capture_output=True, text=True))
