# -*- coding: utf-8 -*-
"""用 JSONL bridge 驱动真实 GA，验证 ask_user_question 端到端。

为什么用 JSONL 而不是 PTY：
PTY 里用 `\\r` 模拟 Enter 会被 Ink 的粘贴层吞掉（变成
`[Copied text #1 +1 lines]`），按键注入不可靠。JSONL 是 UI 自己的
协议，走的是同一条 bridge 链路，而且是纯文本、可精确控制。

这个探针验证的是用户真正关心的问题：
1. 卡片真的弹出来了吗（收到 question_request）
2. 提交之后，答案真的到 LLM 那边了吗（模型复述出答案）
3. 全流程有没有报错

用法：
  python tests/probes/python/ask_q_jsonl_probe.py

产出：tests/probes/captures/ask_q_jsonl.jsonl（完整事件流）
      tests/probes/captures/ask_q_jsonl.txt（可读摘要）
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time

ROOT = r"D:\git_repos\GenericAgent"
OUT_DIR = os.path.join(ROOT, "tests", "probes", "captures")
TIMEOUT_S = 180.0

PROMPT = os.environ.get("__PROMPT__") or (
    "Call the ask_user_question tool right now, before doing anything else. "
    "Ask exactly one single-select question: key 'task', question text "
    "'Which should I do first?', options labeled exactly 'write tests' and "
    "'write docs'. Do not pick for me. After you receive my answer, your very "
    "next output must contain the literal line: YOU PICKED: <the answer text>"
)

# 用户作答：__ANSWER_MODE__ = first | all | partial | other
ANSWER_MODE = os.environ.get("__ANSWER_MODE__", "first")


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    env = dict(os.environ)
    for k in [k for k in env if k.upper().startswith(("CLAUDE", "ANTHROPIC"))]:
        env.pop(k, None)
    env["PYTHONPATH"] = ROOT
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    proc = subprocess.Popen(
        [sys.executable, "-m", "frontends.ink.bridge"],
        cwd=ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )

    events: list[dict] = []
    raw_lines: list[str] = []
    q: "queue.Queue[str]" = queue.Queue()
    stderr_buf: list[str] = []

    def pump_stdout():
        for line in proc.stdout:  # type: ignore[union-attr]
            raw_lines.append(line.rstrip("\n"))
            q.put(line.rstrip("\n"))
        q.put("<<<EOF>>>")

    def pump_stderr():
        for line in proc.stderr:  # type: ignore[union-attr]
            stderr_buf.append(line.rstrip("\n"))

    threading.Thread(target=pump_stdout, daemon=True).start()
    threading.Thread(target=pump_stderr, daemon=True).start()

    def send(obj: dict) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    def wait_for(pred, timeout: float):
        """收事件直到 pred 成立或超时。返回命中的事件或 None。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                line = q.get(timeout=max(0.05, min(1.0, deadline - time.time())))
            except queue.Empty:
                continue
            if line == "<<<EOF>>>":
                return None
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                events.append({"__unparsed__": line})
                continue
            events.append(ev)
            if pred(ev):
                return ev
        return None

    summary: list[str] = []
    try:
        ready = wait_for(lambda e: e.get("type") == "ready", 30)
        summary.append("ready: %s" % ("yes" if ready else "NO"))

        send({"type": "submit", "text": PROMPT})
        summary.append("submitted prompt")

        # 等卡片
        asked = wait_for(lambda e: e.get("type") == "question_request", 150)
        if asked is None:
            summary.append("question_request: NEVER ARRIVED")
            return _finish(summary, events, raw_lines, stderr_buf)

        questions = asked.get("questions") or []
        summary.append("question_request: arrived, %d question(s)" % len(questions))
        for qq in questions:
            summary.append(
                "  q key=%r text=%r options=%r multiSelect=%r"
                % (
                    qq.get("key"),
                    qq.get("question"),
                    [o.get("label") for o in (qq.get("options") or [])],
                    qq.get("multiSelect"),
                )
            )

        request_id = asked.get("requestId")

        def label_of(qi: int, oi: int) -> str:
            qs = questions[qi] if qi < len(questions) else {}
            opts = qs.get("options") or []
            if oi < len(opts):
                return str(opts[oi].get("label"))
            return ""

        answers: dict = {}
        if ANSWER_MODE == "first":
            answers = {(questions[0].get("key") if questions else "task") or "task": label_of(0, 0)}
        elif ANSWER_MODE == "all":
            for i, qq in enumerate(questions):
                key = str(qq.get("key") or ("q%d" % i))
                answers[key] = label_of(i, 0)
        elif ANSWER_MODE == "partial":
            # 只答第一题，故意留空第二题
            key = str(questions[0].get("key") or "q0")
            answers = {key: label_of(0, 0)}
        elif ANSWER_MODE == "multi":
            key = str(questions[0].get("key") or "q0")
            answers = {key: [label_of(0, 0), label_of(0, 1)]}
        elif ANSWER_MODE == "other":
            # 用一句自造文本代替选项
            key = str(questions[0].get("key") or "q0")
            answers = {key: "neither, do the refactor first"}

        summary.append("ANSWER_MODE=%s" % ANSWER_MODE)
        summary.append("user answers: %r" % (answers,))

        if ANSWER_MODE == "cancel":
            send({"type": "question_response", "requestId": request_id, "answers": {}, "cancelled": True})
            summary.append("question_response sent (cancelled)")
        elif ANSWER_MODE == "chat":
            send({"type": "question_response", "requestId": request_id, "answers": {}, "cancelled": True, "reason": "chat"})
            summary.append("question_response sent (chat)")
        else:
            send({"type": "question_response", "requestId": request_id, "answers": answers})
            summary.append("question_response sent")

        # 等模型复述
        deadline = time.time() + 150
        picked_seen = False
        while time.time() < deadline:
            try:
                line = q.get(timeout=1.0)
            except queue.Empty:
                continue
            if line == "<<<EOF>>>":
                break
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            events.append(ev)
            blob = json.dumps(ev, ensure_ascii=False)
            if "YOU PICKED:" in blob:
                picked_seen = True
                summary.append("model echoed: YES")
                summary.append("  " + blob[:400])
                break
            if ev.get("type") == "error":
                summary.append("ERROR event: " + blob[:300])
        if not picked_seen:
            summary.append("model echoed: NO (answer may not have reached the LLM)")
    finally:
        try:
            send({"type": "shutdown"})
        except Exception:
            pass
        time.sleep(1.0)
        try:
            proc.terminate()
        except Exception:
            pass
        time.sleep(0.5)

    return _finish(summary, events, raw_lines, stderr_buf)


def _finish(summary, events, raw_lines, stderr_buf) -> int:
    jpath = os.path.join(OUT_DIR, "ask_q_jsonl.jsonl")
    with open(jpath, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(raw_lines))
    tpath = os.path.join(OUT_DIR, "ask_q_jsonl.txt")
    with open(tpath, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(summary))
        f.write("\n\n---- stderr (tail) ----\n")
        f.write("\n".join(stderr_buf[-40:]))
    sys.stdout.buffer.write(("\n".join(summary) + "\n").encode("utf-8"))
    sys.stdout.buffer.write(("\njsonl   -> %s\nevents  : %d\n" % (jpath, len(events))).encode("utf-8"))
    sys.stdout.buffer.write(("stderr  : %d lines\n" % len(stderr_buf)).encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
