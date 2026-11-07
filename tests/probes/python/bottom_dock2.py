"""把 inline 的 band 钉成满高，让输入框从启动时就在底部（对标 CC）。

只改 plan：band 恒为 room。内容对齐方式（是否底部对齐）先不动，
先看「满高 + Static 历史」在真机上到底长什么样、有没有大留白。
"""
import subprocess
import sys
from pathlib import Path

PLAN = Path(r"D:\git_repos\GenericAgent\frontends\ink\ui\src\messageViewportPlan.ts")

ORIG = """  if (liveCount > 0) {
    const height = Math.min(liveCount, maxLive, room)
    return { kind: 'live', height }
  }
  if (input.keepLivePlaceholder) {
    // Running with no assistant lines yet: still reserve a short live band so
    // open-turn user / thinking cue stays in the dock (visibility P0-A).
    return { kind: 'live', height: 1 }
  }
  if (!input.hasStaticMessages) {
    return { kind: 'ready', height: 1 }
  }
  // Idle with scrollback history. Keep ONE row instead of collapsing to zero:
  // a `live/1` -> `none/0` transition at turn end shrinks the whole frame by one
  // row, which on a full terminal reads as the picture jumping up one line every
  // turn. One row is enough to hold the height steady without the large empty gap
  // that the old "always messageRows" overfix produced.
  return { kind: 'live', height: 1 }"""

FIX = """  // GA_BOTTOM_DOCK: the band is always full height so the composer sits at the
  // terminal bottom from the very first frame (Claude Code parity) instead of
  // starting high and sliding down as output grows.
  //
  // This also makes the frame height constant, so neither a stream tick nor a turn
  // boundary can shift the picture. The previous "always messageRows" overfix was
  // reverted for a different reason: it left the composer pinned to the bottom with
  // a large gap that read as a bug. Here the gap is gone because content hugs the
  // bottom edge of the band (MessageViewport justifyContent=flex-end).
  const height = room
  void liveCount
  void maxLive
  void input.keepLivePlaceholder
  void input.hasStaticMessages
  return { kind: 'live', height }"""

action = sys.argv[1] if len(sys.argv) > 1 else "on"
text = PLAN.read_text(encoding="utf-8")

if action == "on":
    if "GA_BOTTOM_DOCK" in text:
        print("already on")
        sys.exit(0)
    assert ORIG in text, "找不到要替换的分支"
    PLAN.write_text(text.replace(ORIG, FIX), encoding="utf-8", newline="\n")
    print("patched: band 恒为满高")
elif action == "off":
    if "GA_BOTTOM_DOCK" not in text:
        print("already off")
        sys.exit(0)
    PLAN.write_text(text.replace(FIX, ORIG), encoding="utf-8", newline="\n")
    print("reverted")
else:
    print("usage: on|off")
    sys.exit(2)

out = subprocess.run(["git", "diff", "--stat", "--", "frontends/ink/ui/src"],
                     cwd=r"D:\git_repos\GenericAgent", capture_output=True, text=True)
print(out.stdout.strip() or "(no diff)")
