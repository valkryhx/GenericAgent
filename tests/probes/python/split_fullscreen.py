"""App.tsx：把 fullscreen 拆成独立开关，并同步 terminalCleanup 的调用。"""
from pathlib import Path

p = Path(r"D:\git_repos\GenericAgent\frontends\ink\ui\src\App.tsx")
text = p.read_text(encoding="utf-8")

# 1) 引入 resolveFullscreenMode
old_import = "  cleanupTerminalForExit,"
assert old_import in text
if "resolveFullscreenMode" not in text:
    text = text.replace(
        "  cleanupTerminalForExit,",
        "  cleanupTerminalForExit,",
        1,
    )

# 2) 替换 fullscreen 定义
old = """  const mouseMode = useMemo(() => resolveMouseCaptureMode(), [])
  // `scroll` and `full` are both fullscreen (alternate screen, pinned root height,
  // in-app scrollback). They differ only in how much mouse traffic is reported:
  // scroll = wheel only, full = wheel + drag. Layout and scroll behaviour must key
  // off this flag, not off `full`, or scroll-only mode would silently fall back to
  // the inline layout.
  const fullscreen = mouseMode !== 'off'"""
new = """  const mouseMode = useMemo(() => resolveMouseCaptureMode(), [])
  // Layout vs mouse capture are separate decisions.
  // - `fullscreen`: alternate screen + pinned root height. What keeps the composer
  //   from sliding down as output grows.
  // - `mouseMode`: how much pointer traffic GA claims. Any capture mode takes
  //   native click-drag selection away from the terminal, so the default is `off`.
  const fullscreen = useMemo(() => resolveFullscreenMode(), [])"""
assert old in text, "fullscreen 定义未找到"
text = text.replace(old, new)

# 3) 退出清理
text = text.replace("cleanupTerminalForExit(stdout, mouseMode)", "cleanupTerminalForExit(stdout, mouseMode, fullscreen)")
# 4) 进入备用屏
text = text.replace(
    "stdout.write(enterMainScreenTerminalSequenceForMode(mouseMode))",
    "stdout.write(enterMainScreenTerminalSequenceForMode(mouseMode, fullscreen))",
)

# 5) useEffect 依赖里补 fullscreen
text = text.replace("  }, [mouseMode, stdout])", "  }, [fullscreen, mouseMode, stdout])")

p.write_text(text, encoding="utf-8", newline="\n")
print("App.tsx patched")
