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

Usage
-----
    python scripts/probe_ink_frames.py                     # default sequence
    python scripts/probe_ink_frames.py --keys "nnn\\r\\x1b" --wait 8
    python scripts/probe_ink_frames.py --grep "working"     # only frames matching

Key notation: literal text is typed as-is; ``\\r`` is Enter, ``\\x1b`` is Esc,
``\\t`` is Tab. A ``|`` character separates *groups* -- keys are grouped so each
group is sent as one write, then the script sleeps ``--gap`` seconds, which is what
lets a panel paint between keystrokes.

Output
------
``temp/ink_frames/<stamp>/`` receives:
  * ``stdout.bin`` -- the raw byte stream, ANSI intact
  * ``frames.txt`` -- ANSI-stripped, one screen update per block
  * ``frames.txt`` is what you grep for the text a user reported seeing.
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


def main(argv: list[str] | None = None) -> int:
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
    args = parser.parse_args(argv)

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