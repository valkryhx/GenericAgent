"""真机实测：bridge 发来的 assistant_delta 一次带多少字符、多少换行。

目的：验证或推翻「帧高跳变 ≈ delta 行数」这一根因假设。
已知对侧证据：genericagent/core/app.py:97 `STREAM_FLUSH_CHARS = 16`，
即 app 层每 16 个**字符**才 flush 一次 delta。若成立，单次 delta 通常 < 1 行，
那么「一次 delta 跳 2~3 行」就不可能是 delta 粒度造成的，假设被推翻。

做法：直接跑 frontends/ink/bridge.py 的 JSONL 循环（真 LLM、真 MCP），
逐行读 stdout 事件，对每个 assistant_delta 记录 len(text) 与 text.count("\n")。
"""
from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _workspace_guard import guarded_main  # noqa: E402

# 原写法 `Path(__file__).parent` 指向 tests/probes/python，
# 于是 BRIDGE 被拼成 .../tests/probes/python/frontends/ink/bridge.py（不存在），
# PYTHONPATH 也指向探针目录 —— 这个探针从探针归入 probes/ 起就跑不起来。
REPO = Path(__file__).resolve().parents[3]
BRIDGE = REPO / "frontends" / "ink" / "bridge.py"
PROMPT = "请写一段 200 字左右的中文，介绍终端 UI 渲染的基本原理。直接给正文，不要用工具。"


@guarded_main(str(REPO))
def main() -> int:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = str(REPO) + os.pathsep + str(REPO / "frontends") + os.pathsep + env.get("PYTHONPATH", "")

    proc = subprocess.Popen(
        [sys.executable, "-u", str(BRIDGE)],
        cwd=str(REPO),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
    )
    assert proc.stdin and proc.stdout

    def send(obj: dict) -> None:
        proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    deltas: list[dict] = []
    done_text = None
    ready = False
    idle = False
    deadline = time.time() + 180

    # 等 ready，再提交
    submit_at = None
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = ev.get("type")
        if t == "ready":
            ready = True
            submit_at = time.time()
            send({"type": "submit", "text": PROMPT})
            continue
        if t == "assistant_delta":
            deltas.append({
                "at": time.time(),
                "chars": len(ev.get("text") or ""),
                "nl": (ev.get("text") or "").count("\n"),
            })
            continue
        if t == "assistant_done":
            done_text = ev.get("text") or ""
            continue
        if t == "status" and ev.get("status") == "idle" and deltas:
            idle = True
            break

    try:
        send({"type": "shutdown"})
    except Exception:
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()

    print(f"ready={ready} idle={idle} deltas={len(deltas)}")
    if not deltas:
        print("FAIL: 没有收到任何 assistant_delta，无法判定")
        return 1

    chars = [d["chars"] for d in deltas]
    nls = [d["nl"] for d in deltas]
    gaps = [round((deltas[i]["at"] - deltas[i - 1]["at"]) * 1000) for i in range(1, len(deltas))]

    print()
    print("=== 每次 delta 的字符数 ===")
    print(f"  min={min(chars)} median={int(statistics.median(chars))} "
          f"mean={statistics.mean(chars):.1f} max={max(chars)}")
    print(f"  ==16 的占比: {sum(1 for c in chars if c == 16) / len(chars):.0%}")
    print()
    print("=== 每次 delta 携带的换行数 ===")
    print(f"  min={min(nls)} median={int(statistics.median(nls))} max={max(nls)}")
    from collections import Counter
    print(f"  分布: {dict(sorted(Counter(nls).items()))}")
    print()
    print("=== delta 间隔(ms) ===")
    if gaps:
        print(f"  min={min(gaps)} median={int(statistics.median(gaps))} max={max(gaps)}")
    print()
    print("=== 判定 ===")
    big = sum(1 for n in nls if n >= 2)
    print(f"  delta 携带 >=2 换行的次数: {big} / {len(nls)} ({big / len(nls):.0%})")
    if big / len(nls) >= 0.5:
        print("  => 根因假设【成立】：delta 粒度确实 >=2 行")
    else:
        print("  => 根因假设【被推翻】：delta 粒度大多 <2 行，帧高跳变另有来源")
    if done_text is not None:
        print(f"  最终正文 {len(done_text)} 字，渲染前 {done_text.count(chr(10)) + 1} 文本行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
