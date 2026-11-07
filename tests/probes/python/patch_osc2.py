import re

path = r"D:\git_repos\GenericAgent\.tmp_bottom_oscillation.py"
with open(path, encoding="utf-8", newline="") as f:
    text = f.read()

old = """def composer_row():
    disp = screen.display
    for y in range(len(disp) - 1, -1, -1):
        if re.fullmatch(r"─{8,}", disp[y].strip()):
            return y
    return -1


def content_rows():
    disp = screen.display
    n = 0
    for line in disp:
        s = line.strip()
        if s and not re.fullmatch(r"─{8,}", s):
            n += 1
    return n"""

new = """_last = {"row": -1, "content": 0}


def sample():
    \"\"\"取一帧屏幕快照。pyte 在宽字符边界上会 IndexError，取不到就沿用上次值。\"\"\"
    try:
        disp = screen.display
    except Exception:
        return _last["row"], _last["content"]
    row = -1
    content = 0
    for y in range(len(disp) - 1, -1, -1):
        s = disp[y].strip()
        if row < 0 and re.fullmatch(r"─{8,}", s):
            row = y
    for line in disp:
        s = line.strip()
        if s and not re.fullmatch(r"─{8,}", s):
            content += 1
    _last["row"] = row
    _last["content"] = content
    return row, content"""

assert old in text, "block not found"
text = text.replace(old, new)

old2 = """        with lock:
            series.append((time.time() - state["t0"], composer_row(), content_rows()))"""
new2 = """        r, c = sample()
        with lock:
            series.append((time.time() - state["t0"], r, c))"""
assert old2 in text
text = text.replace(old2, new2)

with open(path, "w", encoding="utf-8", newline="\n") as f:
    f.write(text)
print("patched")
