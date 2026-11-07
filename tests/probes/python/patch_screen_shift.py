import re

path = r"D:\git_repos\GenericAgent\.tmp_screen_shift.py"
with open(path, encoding="utf-8", newline="") as f:
    text = f.read()

old = """def snapshot():
    disp = screen.display
    lines = [l.rstrip() for l in disp]
    first = None
    last = None
    for y, l in enumerate(lines):
        if l.strip():
            if first is None:
                first = y
            last = y
    fp = hashlib.md5("\\n".join(lines).encode("utf-8", "replace")).hexdigest()[:8]
    return first, last, fp"""

new = """def snapshot():
    disp = screen.display
    lines = [l.rstrip() for l in disp]
    first = -1
    last = -1
    for y, l in enumerate(lines):
        if l.strip():
            if first < 0:
                first = y
            last = y
    fp = hashlib.md5("\\n".join(lines).encode("utf-8", "replace")).hexdigest()[:8]
    return first, last, fp"""

assert old in text, "snapshot block not found"
text = text.replace(old, new)

# 过滤空屏快照
old2 = """with lock:
    got = list(snaps)"""
new2 = """with lock:
    got = [s for s in snaps if s[1] >= 0]"""
assert old2 in text
text = text.replace(old2, new2)

with open(path, "w", encoding="utf-8", newline="\n") as f:
    f.write(text)
print("patched")
