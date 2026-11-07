"""inline 专用测试改用 GA_INK_FULLSCREEN=off（GA_INK_MOUSE=off 现在只关鼠标捕获）。"""
from pathlib import Path

UI = Path(r"D:\git_repos\GenericAgent\frontends\ink\ui\src")

OLD_MARKER = """  // Inline-mode behaviour: pin the mode explicitly. The shipped default is now
  // fullscreen (CC 319 parity), where this branch is not taken at all.
  process.env.GA_INK_MOUSE = 'off'"""
NEW_MARKER = """  // Inline-mode behaviour: pin the layout explicitly. The shipped default is the
  // alternate screen (CC 319 parity), where this branch is not taken at all. Note
  // this is the LAYOUT switch — GA_INK_MOUSE only controls mouse capture.
  process.env.GA_INK_FULLSCREEN = 'off'"""

for name in ["App.test.ts", "resizeReflow.test.ts", "resumeTranscriptDuplication.test.ts"]:
    p = UI / name
    text = p.read_text(encoding="utf-8")
    changed = False

    if OLD_MARKER in text:
        text = text.replace(OLD_MARKER, NEW_MARKER)
        changed = True

    # resizeReflow / resumeTranscript 用的是精简注释
    simple_old = "  process.env.GA_INK_MOUSE = 'off'\n"
    simple_new = "  process.env.GA_INK_FULLSCREEN = 'off'\n"
    if simple_old in text:
        text = text.replace(simple_old, simple_new)
        changed = True

    if changed:
        p.write_text(text, encoding="utf-8", newline="\n")
        print(f"patched {name}")
    else:
        print(f"no change {name}")
