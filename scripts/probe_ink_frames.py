#!/usr/bin/env python3
"""Drive the real Ink UI with scripted keystrokes and capture every stdout frame.

Why this exists
---------------
Some Ink UI defects only exist as *transient frames*: a panel paints for one render
and is gone before a human can screenshot it or read it. Eyeballing the terminal
cannot reproduce that, and unit tests that call pure functions never see it, because
the bug lives in the ordering of state updates across a key sequence.

This script takes the opposite approach: it launches the SAME entry point the user
launches, feeds a scripted key sequence, and keeps the complete byte stream. The
frames are then parsed offline, so a one-frame flash is as visible as a persistent
panel.

It is the concrete form of the technique in
``docs/ga_ink_transient_frame_debug_2026-11-11.md``.

Two modes
---------
**Drive mode** (default). Launches the UI itself with a scripted key sequence and keeps
the whole byte stream. Use it when you can reproduce the bug yourself.

    python scripts/probe_ink_frames.py                     # default sequence
    python scripts/probe_ink_frames.py --keys "nnn\\r\\x1b" --wait 8
    python scripts/probe_ink_frames.py --grep "working"    # only frames matching

**Attach mode** (``--attach``). The user drives a real ``ga`` session and you record it.
This is the mode to use when the bug needs a live agent-team/workflow context that you
cannot reconstruct, or when the UI that matters is the *installed* wheel rather than
this checkout. It prints the exact command to paste in another terminal; a preload
module (``scripts/ga_frame_trace.mjs``) tees stdout without touching the UI source.

    python scripts/probe_ink_frames.py --attach
    python scripts/probe_ink_frames.py --attach --grep Waiting --idle-exit 60

Key notation: literal text is typed as-is; ``\\r`` is Enter, ``\\x1b`` is Esc,
``\\t`` is Tab. A ``|`` character separates *groups* -- keys are grouped so each
group is sent as one write, then the script sleeps ``--gap`` seconds, which is what
lets a panel paint between keystrokes.

Output
------
Drive mode -- ``temp/ink_frames/<stamp>/``:
  * ``stdout.bin`` -- the raw byte stream, ANSI intact
  * ``frames.txt`` -- ANSI-stripped, one screen update per block
  * ``frames.txt`` is what you grep for the text a user reported seeing.

Attach mode -- ``temp/ink_trace/frames-<pid>.log`` (written by the UI process), parsed
and printed here.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

# The Ink entry point, resolved the same way `ga` resolves it: prefer the installed
# wheel (that is what `ga` runs), fall back to this checkout.
UI_CANDIDATES = (
    Path(sys.prefix) / "Lib" / "site-packages" / "frontends" / "ink" / "ui",
    REPO_ROOT / "frontends" / "ink" / "ui",
)

ANSI_RE = re.compile(
    r"\x1b\][^\x07]*(?:\x07|\x1b\\)"   # OSC
    r"|\x1b\[[0-?]*[ -/]*[@-~]"        # CSI
    r"|\x1b[=>]"                        # misc
)


def resolve_ui_dir(explicit: str | None = None) -> Path:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        # An explicit path still has to be a real UI tree. Accepting a typo here would
        # launch node against a missing entry and report "0 frames", which reads like
        # "the bug did not reproduce" -- the exact silent failure this script exists to
        # rule out.
        if not (path / "src" / "main.tsx").is_file():
            raise FileNotFoundError(
                f"No Ink UI entry at {path / 'src' / 'main.tsx'}. "
                f"Pass --ui-dir pointing at a directory containing src/main.tsx."
            )
        return path
    for candidate in UI_CANDIDATES:
        if (candidate / "src" / "main.tsx").is_file():
            return candidate
    raise FileNotFoundError(
        "Cannot find the Ink UI. Looked in:\n  "
        + "\n  ".join(str(c) for c in UI_CANDIDATES)
        + "\nPass --ui-dir <path>."
    )


def decode_keys(spec: str) -> list[str]:
    """Split a ``a|\\r|\\x1b`` spec into groups of raw key bytes.

    Escapes follow the usual convention, decoded left to right in ONE pass:

        ``\\\\`` -> ``\\``    ``\\r`` -> CR    ``\\n`` -> LF    ``\\t`` -> TAB    ``\\x1b`` -> ESC

    A single pass matters. Replacing ``\\r`` over the whole string first would turn a
    Windows path like ``C:\\repo`` into ``C:<CR>epo`` -- sending Enter in the middle of
    the text a user meant to type. With ``\\\\`` as the escape for a literal backslash,
    ``C:\\\\repo`` keeps its separator and ``\\r`` unambiguously means Enter.
    An unrecognised ``\\x`` is left as-is rather than dropped.
    """
    groups = [group for group in spec.split("|") if group]
    decoded: list[str] = []
    for group in groups:
        out: list[str] = []
        index = 0
        while index < len(group):
            char = group[index]
            if char != "\\" or index + 1 >= len(group):
                out.append(char)
                index += 1
                continue
            nxt = group[index + 1]
            if nxt == "\\":
                out.append("\\")
                index += 2
            elif nxt == "r":
                out.append("\r")
                index += 2
            elif nxt == "n":
                out.append("\n")
                index += 2
            elif nxt == "t":
                out.append("\t")
                index += 2
            elif group.startswith("\\x1b", index):
                out.append("\x1b")
                index += 4
            else:
                # Unknown escape: keep both characters so nothing is silently lost.
                out.append(char)
                index += 1
        decoded.append("".join(out))
    return decoded


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text.replace("\r", ""))


def split_frames(stream: str) -> list[str]:
    """Split the raw stream into per-update blocks.

    Ink erases the previous frame with cursor-up + erase-line bursts before painting
    the next one. Those bursts are the only reliable frame boundary in the byte
    stream, so split on the longest erase run rather than on newlines (a single frame
    contains many).
    """
    plain = strip_ansi(stream)
    return [block for block in re.split(r"\n{3,}", plain) if block.strip()]


# --- attach mode: parse traces written by scripts/ga_frame_trace.mjs ---------------
#
# The trace file is byte-length framed (see that module's docstring), so parsing is
# exact rather than pattern-guessing: read the header, take `bytes=N` bytes verbatim.

FRAME_HEAD_RE = re.compile(
    rb"\n=== GA_FRAME (?P<iso>\S+) #(?P<seq>\d+) cols=(?P<cols>\d+) rows=(?P<rows>\d+) "
    rb"bytes=(?P<bytes>\d+) ===\n"
)


def read_trace_file(path: Path) -> list[dict]:
    """Parse one ``frames-<pid>.log`` into ``[{seq, iso, cols, rows, raw}]``.

    Works in bytes, not str: the header carries a byte count, and searching a decoded
    string would make that count meaningless as soon as a chunk contains multi-byte
    UTF-8 (every Chinese label does). ``raw`` is decoded only for display.
    """
    data = path.read_bytes()
    frames: list[dict] = []
    cursor = 0
    while True:
        match = FRAME_HEAD_RE.search(data, cursor)
        if not match:
            break
        start = match.end()
        size = int(match.group("bytes"))
        chunk = data[start:start + size]
        frames.append(
            {
                "seq": int(match.group("seq")),
                "iso": match.group("iso").decode("ascii", "replace"),
                "cols": int(match.group("cols")),
                "rows": int(match.group("rows")),
                "raw": chunk.decode("utf-8", "replace"),
                "truncated": len(chunk) < size,
            }
        )
        cursor = start + size
    return frames


def render_frame(path: Path, frame: dict, grep: str | None) -> str:
    """One frame -> one printable block, ANSI stripped. Empty if it fails ``grep``."""
    text = strip_ansi(frame["raw"])
    if grep and grep not in text:
        return ""
    return (
        f"\n##### {path.name} #{frame['seq']} {frame['iso']} cols={frame['cols']} "
        f"rows={frame['rows']}{' TRUNCATED' if frame['truncated'] else ''} #####\n{text}"
    )


def find_traces(directory: Path) -> list[Path]:
    return sorted(directory.glob("frames-*.log"), key=lambda p: p.stat().st_mtime)


def attach(out_dir: Path, grep: str | None, watch: bool = True, idle_exit: float = 30.0) -> int:
    """Watch a live trace directory -- the 'you drive, I record' mode.

    Polls rather than using a file watcher: the trace file is appended by whichever
    node process is the UI, and a plain mtime/size poll is enough at 0.5s and cannot
    miss the race where the watch is set up before the file exists.

    Exits after ``idle_exit`` seconds with no file growth, so the same command works
    both interactively (Ctrl+C) and when driven from another shell/script.
    """
    trace_script = REPO_ROOT / "scripts" / "ga_frame_trace.mjs"
    # Node's --import needs a file URL, not a Windows path: backslashes get eaten by
    # the loader, so always hand it the forward-slash form behind file:///.
    url = f"file:///{trace_script.as_posix()}"
    print("=" * 78)
    print("在【另一个终端】里粘贴对应的一行启动 ga，然后照常操作复现 bug：")
    print()
    print("  PowerShell:")
    print(f'    $env:GA_UI_TRACE="{out_dir}"; $env:NODE_OPTIONS=\'--import "{url}"\'; ga')
    print()
    print("  bash / Git Bash:")
    print(f"    GA_UI_TRACE='{out_dir}' NODE_OPTIONS='--import \"{url}\"' ga")
    print()
    print("  cmd.exe:")
    print(f'    set GA_UI_TRACE={out_dir}')
    print(f'    set NODE_OPTIONS=--import "{url}"')
    print("    ga")
    print()
    print(f"记录目录: {out_dir}")
    print(f"复现完退出 ga（或 Ctrl+C）；无新帧 {idle_exit:.0f}s 后本进程自动退出并汇总。")
    print("=" * 78)

    out_dir.mkdir(parents=True, exist_ok=True)
    # Per-file cursor into the frame list: on each poll only frames beyond the count we
    # already printed are emitted. Re-rendering the whole file every 0.5s would flood
    # the terminal with thousands of duplicate blocks and bury the one flash we want.
    emitted: dict[Path, int] = {}
    last_growth = time.time()
    try:
        while True:
            grew = False
            for trace in find_traces(out_dir):
                frames = read_trace_file(trace)
                done = emitted.get(trace, 0)
                if len(frames) == done:
                    continue
                grew = True
                for frame in frames[done:]:
                    text = render_frame(trace, frame, grep)
                    if text:
                        print(text)
                emitted[trace] = len(frames)
                print(f"[attach] {trace.name}: {len(frames)} frame(s) (+{len(frames) - done})")
            if grew:
                last_growth = time.time()
            if not watch:
                break
            if idle_exit and time.time() - last_growth > idle_exit:
                print(f"[attach] {idle_exit:.0f}s 无新帧，退出。")
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default stdout to GBK; every message here is Chinese, so without
    # this the operator sees mojibake instead of the command they are meant to paste.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ui-dir", default=None, help="Ink UI directory (default: installed, else checkout)")
    parser.add_argument(
        "--keys",
        default="nnndadadad|\\r|\\x1b",
        help='key groups separated by "|"; \\r Enter, \\x1b Esc (default: the reported repro)',
    )
    parser.add_argument("--wait", type=float, default=6.0, help="seconds to let the UI boot before typing")
    parser.add_argument("--gap", type=float, default=0.35, help="seconds between key groups")
    parser.add_argument("--settle", type=float, default=1.2, help="seconds to keep capturing after the last key")
    parser.add_argument("--grep", default=None, help="only print frames containing this text")
    parser.add_argument("--out", default=None, help="output directory (default: temp/ink_frames/<stamp>)")
    parser.add_argument(
        "--attach",
        action="store_true",
        help="do not drive the UI: record a live ga session YOU drive, and parse the trace",
    )
    parser.add_argument(
        "--idle-exit",
        type=float,
        default=30.0,
        help="with --attach: exit after this many idle seconds (0 = wait forever)",
    )
    args = parser.parse_args(argv)

    if args.attach:
        out_dir = Path(args.out) if args.out else REPO_ROOT / "temp" / "ink_trace"
        # Absolute matters: the printed command is pasted into a *different* shell whose
        # cwd is not necessarily this one, and a relative GA_UI_TRACE would then land
        # somewhere the watcher never looks.
        return attach(out_dir.expanduser().resolve(), args.grep, idle_exit=args.idle_exit)

    ui_dir = resolve_ui_dir(args.ui_dir)
    cli = ui_dir / "node_modules" / "tsx" / "dist" / "cli.mjs"
    entry = ui_dir / "src" / "main.tsx"
    if not cli.is_file():
        print(f"[ERROR] tsx not installed in {ui_dir}. Run: npm install --prefix \"{ui_dir}\"", file=sys.stderr)
        return 2

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out) if args.out else REPO_ROOT / "temp" / "ink_frames" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    env = {
        **os.environ,
        # Keep the child on the same project/data roots `ga` would use, so the frames
        # show the user's real session rather than an empty one.
        "GA_PROJECT_ROOT": os.environ.get("GA_PROJECT_ROOT", str(REPO_ROOT)),
        "GA_DATA_DIR": os.environ.get("GA_DATA_DIR", ""),
    }
    if not env["GA_DATA_DIR"]:
        env.pop("GA_DATA_DIR")

    print(f"UI     : {ui_dir}")
    print(f"keys   : {args.keys!r}")
    print(f"output : {out_dir}")

    proc = subprocess.Popen(
        ["node", str(cli), str(entry)],
        cwd=str(ui_dir),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        time.sleep(args.wait)
        for group in decode_keys(args.keys):
            if proc.poll() is not None:
                print("[WARN] UI exited early; capturing what it produced", file=sys.stderr)
                break
            try:
                proc.stdin.write(group.encode("utf-8"))
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                print(f"[WARN] stdin closed ({exc})", file=sys.stderr)
                break
            time.sleep(args.gap)
        time.sleep(args.settle)
    finally:
        proc.kill()
        out, err = proc.communicate()

    (out_dir / "stdout.bin").write_bytes(out)
    (out_dir / "stderr.txt").write_bytes(err)

    frames = split_frames(out.decode("utf-8", "replace"))
    (out_dir / "frames.txt").write_text(
        "\n\n===== FRAME =====\n\n".join(frames), encoding="utf-8", newline="\n"
    )

    print(f"stdout : {len(out)} bytes, {len(frames)} frame(s)")
    if err.strip():
        print("stderr :", err.decode("utf-8", "replace").strip()[:400])

    if args.grep:
        hits = [f for f in frames if args.grep in f]
        print(f"\nmatching {args.grep!r}: {len(hits)} frame(s)")
        for hit in hits[:5]:
            print("-----")
            print(hit[:1200])
        return 0 if hits else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())