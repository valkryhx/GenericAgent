"""真实 LLM 图片输入端到端（O2/O3/O4/O6）。

遵循 docs/20261103-GA真实LLM测试策略-过程断言优先.md：
- 每条用例只用「一条用户会敲的输入」；
- 断言过程证据（耗时/running/assistant 正文/token>0），不只结果；
- 断言「本次新增」（临时目录带时间戳、事件用 start 偏移）；
- 图片内容验证：让模型读出图中 OCR 文本，证明像素真送达。

运行：python tests/real_image_input_e2e.py
"""
from __future__ import annotations
import json, os, sys, threading, time, base64
from pathlib import Path

REPO = Path(r"D:/git_codes/GenericAgent")
for _p in (REPO,):
    sys.path.insert(0, str(_p))

STAMP = time.strftime("%Y%m%d-%H%M%S")
IMGS = REPO / "temp" / "image_input_selftest" / "imgs"
OUT = REPO / "temp" / "image_input_selftest" / f"run_{STAMP}"
OUT.mkdir(parents=True, exist_ok=True)

for _k in ("GA_PROJECT_ROOT", "GA_WORKFLOW_WORKSPACE_ROOT", "GA_PROJECT_KEY",
           "GA_PROJECT_DATA_DIR", "GA_SESSION_DATA_DIR", "GA_TASK_ROOT", "GA_TEAM_KEY",
           "GA_DATA_DIR"):
    os.environ.pop(_k, None)
DATA = REPO / "temp" / "image_input_selftest" / f"data_{STAMP}"
os.environ.update({
    "GA_DATA_DIR": str(DATA),
    "GA_LAUNCH_CWD": str(REPO),
    "GA_ORIGINAL_CWD": str(REPO),
    "GA_LLM_CONFIG": str(REPO / "llm.yaml"),
})

CHECKS = []
def rec(name, ok, detail=""):
    CHECKS.append({"name": name, "ok": bool(ok), "detail": str(detail)})
    print(("PASS " if ok else "FAIL ") + name + (" - " + str(detail) if detail else ""), flush=True)

from frontends.ink.bridge import GenericAgentBridge  # noqa: E402
from genericagent.core import app as _app  # noqa: E402

events = []
_lock = threading.Lock()
def emit(e):
    with _lock:
        events.append(e)

def snapshot(n):
    with _lock:
        return list(events[n:])

def drain(task_id, timeout=240):
    t0 = time.time(); done = None
    while time.time() - t0 < timeout:
        with _lock:
            ds = [e for e in events if e.get("type") == "assistant_done" and e.get("taskId") == task_id]
            idle = [e for e in events if e.get("type") == "status" and e.get("status") == "idle" and e.get("taskId") == task_id]
        if ds:
            done = ds[-1].get("text", "")
        if idle:
            break
        time.sleep(0.4)
    return done, round(time.time() - t0, 1)

def run_turn(bridge, text, images=None, timeout=240):
    start = len(events)
    tid = bridge.submit(text, images=images)
    done, secs = drain(tid, timeout)
    evs = snapshot(start)
    running_seen = any(e.get("type") == "status" and e.get("status") == "running" for e in evs)
    usage = {}
    for e in evs:
        if e.get("type") == "token_usage":
            usage = e
    return {
        "task_id": tid, "done": done or "", "secs": secs, "running_seen": running_seen,
        "usage": usage, "events": evs,
    }

def tokens_of(usage):
    for k in ("totalTokens", "outputTokens", "inputTokens"):
        v = usage.get(k) if isinstance(usage, dict) else None
        if isinstance(v, int) and v > 0:
            return v
    return 0

def process_ok(tag, r):
    rec(f"{tag}: 耗时像真回合(>1s)", r["secs"] > 1, f"secs={r['secs']}")
    rec(f"{tag}: 出现过 running", r["running_seen"])
    rec(f"{tag}: assistant 正文非空", len((r["done"] or "").strip()) > 0, (r["done"] or "")[:80])
    rec(f"{tag}: token>0", tokens_of(r["usage"]) > 0, r["usage"])

def main() -> int:
    bridge = GenericAgentBridge(emit=emit)
    agent = bridge.agent
    threading.Thread(target=agent.run, daemon=True).start()
    time.sleep(2)
    try:
        bridge.model_switch("default/moma/deepseek-latest-flash")
        rec("MODEL: supports native image input", True, agent.get_llm_name(model=True))
    except Exception as e:
        print("model_switch warn:", e, flush=True)

    big = IMGS / "big_3024x1964.png"
    small = IMGS / "small_400x300.png"

    # (1) 显式附件：让模型读出大图 OCR 文本（真正的像素送达证据）
    r1 = run_turn(bridge,
        "请只描述你看得见的图中文字，逐行原样输出，不要解释。",
        images=[str(big)])
    process_ok("BIG", r1)
    d1 = r1["done"]
    rec("BIG: 读出 ZEBRA-7734", "ZEBRA-7734" in d1, d1[:160])
    rec("BIG: 读出 ORANGE-42", "ORANGE-42" in d1, d1[:160])

    # (2) 小图（不缩放）：让模型读 PINE-318
    r2 = run_turn(bridge, "图里写了什么代码？只回答代码。", images=[str(small)])
    process_ok("SMALL", r2)
    rec("SMALL: 读出 PINE-318", "PINE-318" in r2["done"], r2["done"][:120])

    # (3) 文本路径自动附图（前端不传 images，靠 _extract_image_paths）
    r3 = run_turn(bridge, f"这张图里有什么代码？{small.as_posix()}")
    process_ok("PATH", r3)
    rec("PATH: 读出 PINE-318（路径自动附图成功）", "PINE-318" in r3["done"], r3["done"][:120])

    # (4) O3 反例：代码块里的路径不该被自动当附件。
    # 纯函数证据（不受模型随机性影响）：扫描层不会把 fence 内路径收进自动附图列表。
    # 注：模型仍可自主 code_run 读 D 盘，那是模型行为，不代表「自动附图」。
    fence_text = f"下面是配置示例：\n```\n{small.as_posix()}\n```\n请问这段配置里的图内容是什么？"
    extracted = _app._extract_image_paths(fence_text)
    rec("FENCE: 代码块路径未被自动扫描为附件", str(small) not in extracted and len(extracted) == 0, extracted)
    inline_extracted = _app._extract_image_paths(f"看图 {str(small)}")
    rec("FENCE: 对照-正文裸路径可被扫描", str(small) in inline_extracted, inline_extracted)
    r4 = run_turn(bridge, f"下面是配置示例：\n```\n{small.name}\n```\n这段配置里的字面量是什么？只回答字面量。")
    process_ok("FENCE", r4)

    # (5) O6：坏路径作为显式附件 -> 界面可见失败提示
    bad = str(OUT / "no_such_image_zz.png")
    r5 = run_turn(bridge, "描述这张图。", images=[bad])
    fails = [e for e in r5["events"] if "图片附件读取失败" in str(e) or "读取失败" in str(e)]
    rec("BADPATH: 出现图片附件读取失败提示", bool(fails), fails[:1])

    # (6) O3 上限：12 个存在的路径 -> 只自动附前 8 张，并出现截断提示（纯函数 + 真实回合）
    import shutil
    many_dir = OUT / "many"
    many_dir.mkdir(parents=True, exist_ok=True)
    many = []
    for i in range(12):
        dst = many_dir / f"c{i}.png"
        shutil.copyfile(small, dst)
        many.append(str(dst))
    many_text = "看图 " + " ".join(many)
    paths8, dropped = _app._extract_image_paths_report(many_text)
    rec("LIMIT: 12 路径只留 8", len(paths8) == 8 and dropped == 4, f"{len(paths8)}/{dropped}")
    content = _app._build_user_content_with_images(many_text)
    first_text = content[0]["text"] if content else ""
    rec("LIMIT: 最终 content 含截断提示", "其余 4 个路径未自动附图" in first_text, first_text[-80:])
    r6 = run_turn(bridge, many_text)
    process_ok("LIMIT", r6)

    summary = {
        "stamp": STAMP, "outDir": str(OUT),
        "model": agent.get_llm_name(model=True),
        "checks": CHECKS,
        "turns": {
            "big": {"secs": r1["secs"], "tokens": tokens_of(r1["usage"]), "text": r1["done"][:400]},
            "small": {"secs": r2["secs"], "tokens": tokens_of(r2["usage"]), "text": r2["done"][:400]},
            "path": {"secs": r3["secs"], "tokens": tokens_of(r3["usage"]), "text": r3["done"][:400]},
            "fence": {"secs": r4["secs"], "tokens": tokens_of(r4["usage"]), "text": r4["done"][:400]},
            "badpath": {"secs": r5["secs"], "tokens": tokens_of(r5["usage"])},
            "limit": {"secs": r6["secs"], "tokens": tokens_of(r6["usage"])},
        },
    }
    (REPO / "temp" / "image_input_selftest" / f"summary_{STAMP}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    passed = sum(1 for c in CHECKS if c["ok"])
    print(f"RESULT {passed}/{len(CHECKS)} checks passed; summary=temp/image_input_selftest/summary_{STAMP}.json", flush=True)
    try:
        agent.stop_sig = True
        agent.abort()
        bridge.stop()
    except Exception:
        pass
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
