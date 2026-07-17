"""真实 API 自测：自动压缩（软线/硬线）+ 手动 /compact + 总开关。

用 llm.yaml 的真实 provider 打真实 LLM API。为了容易触发，运行时把阈值调小
（软线 700、硬线 1000；不改 llm.yaml，不写任何 key）。软线场景在判断 True
后会真正调用 compact_agent_context 做摘要（对齐 ink 自动压缩路径）。

用法：
  python tests/real_compact_selftest.py            # active_profile
  python tests/real_compact_selftest.py grok       # 指定 profile
  python tests/real_compact_selftest.py glm
  python tests/real_compact_selftest.py default
  python tests/real_compact_selftest.py gpt-mini

依赖：仓库根目录有 llm.yaml（gitignored，含真实 key）。本脚本本身不含任何密钥。
"""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# Windows 控制台默认 GBK，强制 stdout 走 UTF-8，中文才不乱码。
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import compact_context  # noqa: E402
import token_meter  # noqa: E402
from llm_client import build_client  # noqa: E402
from llm_config import find_llm_config, load_llm_config  # noqa: E402


class MiniAgent:
    """compact 核心只需要 agent.llmclient.backend / agent.history / handler。"""

    def __init__(self, client):
        self.llmclient = client
        self.history = []
        self.handler = object()
        self.log_path = None
        self.session_path = None
        self.session_id = ""


def _user(text: str) -> dict:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def run_turn(backend, text: str):
    """跑一轮真实对话，返回助手 MockResponse（若有）。"""
    gen = backend.ask(_user(text))
    try:
        while True:
            next(gen)
    except StopIteration as e:
        return e.value


def sep(title: str) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def main() -> int:
    profile = sys.argv[1] if len(sys.argv) > 1 else None
    path = find_llm_config(str(REPO))
    if not path:
        print("ERROR: 未找到 llm.yaml（仓库根目录）。本脚本不含 key，需本地配置。")
        return 2
    cfg = load_llm_config(path)
    client = build_client(cfg, profile)
    backend = client.backend
    agent = MiniAgent(client)
    print(
        f"profile={profile or cfg.active_profile}  "
        f"backend={type(backend).__name__}  model={backend.model}"
    )

    # 软线阈值调小，确保多轮后真实 token 会越过；硬线抬高，避免本场景被裁剪抢走。
    # 对齐 ink_bridge._auto_compact_if_needed：判断 True → 真正调用 compact_agent_context。
    SOFT = 700
    HARD = 50_000

    # ── 场景 A：软线自动压缩（判断 + 真实执行摘要）──────────────────────
    sep(f"场景 A：软线自动压缩 should_auto_compact → compact_agent_context（软线={SOFT}）")
    backend.history = []
    backend.last_usage_tokens = None
    backend.auto_compact_tokens = SOFT
    backend.hard_limit_tokens = HARD
    backend.auto_compact_enabled = True
    before = compact_context.should_auto_compact_agent(agent, pending_text="hi")
    print(f"  空历史 + 短输入 → should_auto_compact = {before}（期望 False）")

    # 多轮真实对话，塞入可被摘要记住的事实 + 长文本，把真实 token 推过软线。
    soft_prompts = [
        "我叫小明，项目代号是 Falcon，住在杭州。请用一句话确认你记住了。",
        "请写一段大约200字的关于海洋的科普。",
        "请写一段大约200字的关于森林的科普。",
        "请写一段大约200字的关于沙漠的科普。",
        "请写一段大约200字的关于极地的科普。",
        "请写一段大约200字的关于火山的科普。",
    ]
    triggered = False
    for i, p in enumerate(soft_prompts, 1):
        run_turn(backend, p)
        real = token_meter.real_total_tokens(backend.last_usage_tokens)
        est = token_meter.estimate_context_tokens(backend.history, backend.last_usage_tokens)
        hit = compact_context.should_auto_compact_agent(agent, pending_text="")
        print(
            f"  轮 {i}: history={len(backend.history)}  real_total={real}  "
            f"estimate={est}  soft={SOFT}  should_auto={hit}"
        )
        if hit:
            triggered = True
            n_before = len(backend.history)
            # 对齐 ink 自动压缩：判断通过后真正执行摘要压缩。
            result_auto = compact_context.compact_agent_context(
                agent,
                instructions="Automatic compact before the next user request. 保留用户姓名、项目代号、城市。",
            )
            print(
                f"  ★ 软线触发：compact ok={result_auto.ok}  "
                f"history {n_before}→{len(backend.history)}  message={result_auto.message}"
            )
            if result_auto.ok:
                print(f"  摘要片段（前250字）：\n    {result_auto.summary[:250]}")
                follow = run_turn(backend, "根据压缩后的上下文：我叫什么？项目代号？住哪？")
                ans = getattr(follow, "content", "") if follow else ""
                print(f"  压缩后追问回答：{ans[:220]}")
            break
    if not triggered:
        print(f"  ⚠ 跑完 {len(soft_prompts)} 轮仍未越过软线 {SOFT}，请再降阈值或加长 prompt。")

    # ── 场景 B：硬线裁剪（trim_messages_history）多轮真实对话触发 ──────────
    sep("场景 B：硬线裁剪 trim_messages_history（多轮真实对话，丢最旧消息）")
    backend.history = []
    backend.last_usage_tokens = None
    backend.auto_compact_tokens = 100_000
    backend.hard_limit_tokens = 1000  # 硬线设小，几轮后必超
    backend.auto_compact_enabled = True
    prompts = [
        "请写一段大约200字的关于海洋的科普。",
        "再写一段大约200字关于森林的科普。",
        "再写一段大约200字关于沙漠的科普。",
        "再写一段大约200字关于极地的科普。",
        "再写一段大约200字关于火山的科普。",
    ]
    for i, p in enumerate(prompts, 1):
        n_before = len(backend.history)
        run_turn(backend, p)
        n_after = len(backend.history)
        est = token_meter.estimate_context_tokens(backend.history, backend.last_usage_tokens)
        real = token_meter.real_total_tokens(backend.last_usage_tokens)
        print(
            f"  轮 {i}: history {n_before}→{n_after} 条  "
            f"real_total={real}  estimate={est}  硬线=1000"
        )
    print(
        f"  最终 history 条数 = {len(backend.history)}"
        f"（硬线裁剪应把最旧消息丢掉，不会无限增长）"
    )

    # ── 场景 C：总开关关闭时不裁剪 ─────────────────────────────────────
    sep("场景 C：总开关 auto_compact_enabled=False → 不裁剪（history 持续增长）")
    backend.history = []
    backend.last_usage_tokens = None
    backend.hard_limit_tokens = 1000
    backend.auto_compact_enabled = False
    for i, p in enumerate(prompts[:3], 1):
        run_turn(backend, p)
        print(f"  轮 {i}: history = {len(backend.history)} 条（开关关闭，不裁剪）")
    off = compact_context.should_auto_compact_agent(agent, pending_text="x" * 100000)
    print(f"  should_auto_compact（开关关闭）= {off}（期望 False，即便 pending 很大）")

    # ── 场景 D：手动压缩（compact_agent_context 真实 LLM 摘要）────────────
    sep("场景 D：手动 /compact（compact_agent_context，真实 LLM 摘要替换长历史）")
    backend.auto_compact_enabled = True
    backend.hard_limit_tokens = 100_000
    backend.history = []
    backend.last_usage_tokens = None
    run_turn(backend, "我叫小明，我最喜欢的颜色是蓝色，我的项目代号是 Falcon。记住这些。")
    run_turn(backend, "另外我住在杭州，用 Python 写后端。")
    run_turn(backend, "再补充一点：我的猫叫咪咪。")
    n_before = len(backend.history)
    print(f"  压缩前 history = {n_before} 条")
    result = compact_context.compact_agent_context(
        agent, instructions="务必保留用户的名字、项目代号、城市、猫名。"
    )
    print(f"  compact 结果 ok={result.ok}  message={result.message}")
    print(f"  压缩后 history = {len(backend.history)} 条（应为摘要 user+assistant 一对 = 2 条）")
    if result.ok:
        print(f"  摘要片段（前300字）：\n    {result.summary[:300]}")
        follow = run_turn(backend, "根据前面的上下文，我的项目代号是什么？我的猫叫什么？")
        ans = getattr(follow, "content", "") if follow else ""
        print(f"  压缩后追问回答：{ans[:200]}")

    print("\n" + "=" * 70)
    print("自测完成。")
    print("=" * 70)
    return 0 if result.ok else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n中断。")
        raise SystemExit(130)
