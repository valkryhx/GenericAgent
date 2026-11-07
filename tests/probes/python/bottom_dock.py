"""把 inline 的 band 钉成满高，让输入框从启动时就在底部（对标 CC）。

两部分：
1. messageViewportPlan：有历史/无历史都返回满高 band（room），不再随内容增长
2. MessageViewport：内容底部对齐（justifyContent flex-end），让文本贴着输入框

改完必须真机验收；不通过就 off 回滚。
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"D:\git_repos\GenericAgent\frontends\ink\ui\src")
PLAN = ROOT / "messageViewportPlan.ts"
VIEW = ROOT / "App.tsx"

# --- 1) plan：满高 ---
PLAN_ORIG = """  if (liveCount > 0) {
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

PLAN_FIX = """  // GA_BOTTOM_DOCK: the band is always full height so the composer sits at the
  // bottom from the very first frame (Claude Code parity) instead of starting at
  // the top and sliding down as output grows. A fixed band also makes the frame
  // height constant, so no turn boundary or stream tick can shift the picture.
  //
  // The old "always messageRows" overfix was reverted because the composer was
  // pinned to the *terminal* bottom with a large gap that read as a bug. Here the
  // gap is the message area itself and content hugs its bottom edge
  // (MessageViewport justifyContent=flex-end), which is what CC looks like.
  void liveCount
  void maxLive
  void input.keepLivePlaceholder
  void input.hasStaticMessages
  return { kind: 'live', height: room }"""

# --- 2) view：底部对齐 ---
VIEW_ORIG = """      <Box flexDirection="column" paddingLeft={1} paddingRight={1} height={height} width={messageColumns} overflow="hidden">
        {ready ? <Text color={theme.muted}>Ready.</Text> : lines.map(line => <TranscriptLineView key={line.id} line={line} />)}
      </Box>"""

VIEW_FIX = """      <Box flexDirection="column" justifyContent="flex-end" paddingLeft={1} paddingRight={1} height={height} width={messageColumns} overflow="hidden">
        {ready ? <Text color={theme.muted}>Ready.</Text> : lines.map(line => <TranscriptLineView key={line.id} line={line} />)}
      </Box>"""


def apply(action: str) -> int:
    plan_src = PLAN.read_text(encoding="utf-8")
    view_src = VIEW.read_text(encoding="utf-8")

    if action == "on":
        if "GA_BOTTOM_DOCK" in plan_src:
            print("already on")
            return 0
        assert PLAN_ORIG in plan_src, "plan: 找不到要替换的分支"
        assert VIEW_ORIG in view_src, "view: 找不到 MessageViewport 内容 Box"
        PLAN.write_text(plan_src.replace(PLAN_ORIG, PLAN_FIX), encoding="utf-8", newline="\n")
        VIEW.write_text(view_src.replace(VIEW_ORIG, VIEW_FIX), encoding="utf-8", newline="\n")
        print("patched: band 满高 + 内容底部对齐")
    elif action == "off":
        if "GA_BOTTOM_DOCK" not in plan_src:
            print("already off")
            return 0
        PLAN.write_text(plan_src.replace(PLAN_FIX, PLAN_ORIG), encoding="utf-8", newline="\n")
        VIEW.write_text(view_src.replace(VIEW_FIX, VIEW_ORIG), encoding="utf-8", newline="\n")
        print("reverted")
    else:
        print("usage: on|off")
        return 2

    out = subprocess.run(["git", "diff", "--stat", "--", "frontends/ink/ui/src"],
                         cwd=r"D:\git_repos\GenericAgent", capture_output=True, text=True)
    print(out.stdout.strip() or "(no diff)")
    return 0


sys.exit(apply(sys.argv[1] if len(sys.argv) > 1 else "on"))
