import re

path = r"D:\git_repos\GenericAgent\.tmp_bottom_oscillation.py"
with open(path, encoding="utf-8", newline="") as f:
    text = f.read()

old = "    _, pc = tail[i - 1]"
new = "    pc = tail[i - 1][2]"
assert old in text
text = text.replace(old, new)

# 采样数太少（3.3s）：把观测时间拆成「等流式结束」+ 更长观测
old2 = """# 先等撑满，再观测 25 秒摆动
time.sleep(55)"""
new2 = """# 先等撑满，再继续观测
time.sleep(80)"""
assert old2 in text
text = text.replace(old2, new2)

with open(path, "w", encoding="utf-8", newline="\n") as f:
    f.write(text)
print("patched")
