"""
GA LLM 接入层 —— 客户端构造桥（阶段 2）。

把 llm_config 的「配置态」（ResolvedModel）转成可用的 llmcore ToolClient/
NativeToolClient「运行态」。这是取代 agentmain.py:188-201 变量名魔法的单一
构造入口：过去靠 cfg_name 里的 native/claude/oai 子串选 Session 类，现在完全
由 provider 的显式 wire_api 字段决定。

wire_api → Session 类映射（三种 wire 全部一等支持）：
  anthropic         → NativeClaudeSession  （Anthropic messages 原生协议）
  openai_chat       → NativeOAISession，api_mode=chat_completions（默认兼容路径）
  openai_responses  → NativeOAISession，api_mode=responses（OpenAI 原生增强）

三者都用「原生 tool 字段」协议（function calling），对齐 CC/Codex 的做法；
已废弃的文本协议 Session（ClaudeSession/LLMSession）不在此路径提供。

本模块惰性 import llmcore，使 llm_config 可脱离 llmcore 独立单测。
"""

from __future__ import annotations

from typing import Any, Optional

from llm_config import LLMConfig, ResolvedModel, load_llm_config, find_llm_config


def _make_session(resolved: ResolvedModel):
    """按 wire_api 实例化对应的 Session 类，喂入 to_legacy_cfg() 生成的 cfg。"""
    import llmcore

    cfg = resolved.to_legacy_cfg()
    if resolved.wire_api == "anthropic":
        return llmcore.NativeClaudeSession(cfg=cfg)
    if resolved.wire_api in ("openai_chat", "openai_responses"):
        # api_mode 已由 to_legacy_cfg() 按 wire_api 写好。
        return llmcore.NativeOAISession(cfg=cfg)
    raise ValueError(f"未知 wire_api：{resolved.wire_api!r}")


def _wrap_client(session):
    """套 NativeToolClient（三种 wire 都是 Native 系列）。"""
    import llmcore

    return llmcore.NativeToolClient(session)


def build_session(config: LLMConfig, profile_name: Optional[str] = None):
    """解析 profile → 实例化 Session（不套 ToolClient，供 mixin/内部使用）。"""
    resolved = config.resolve(profile_name)
    return _make_session(resolved)


def build_client(config: LLMConfig, profile_name: Optional[str] = None):
    """解析 profile → NativeToolClient（agentmain 用的完整客户端）。

    这是取代 resolve_client(cfg_name) 的显式入口：不再猜变量名，profile →
    model → provider.wire_api 一路显式解析。
    """
    return _wrap_client(build_session(config, profile_name))


def build_mixin_client(config: LLMConfig, mixin_name: str):
    """按 mixin.chain 构造 MixinSession → NativeToolClient（故障转移）。

    chain 里的每个 model key 各解析成一个 Session，MixinSession 按现有语义
    做故障转移 + spring-back。要求 chain 里的 model 全走 Native 系列（本层
    产出的 Session 都是 Native，天然满足 MixinSession 的同组约束）。
    """
    import llmcore

    if mixin_name not in config.mixin:
        raise ValueError(f"mixin '{mixin_name}' 不存在：{sorted(config.mixin)}")
    mx = config.mixin[mixin_name]

    # 为 chain 里每个 model 造一个 Session。MixinSession 通过 name 引用它们，
    # 因此把每个 Session 的 name 设为对应的 model key。
    sessions: list[Any] = []
    for model_key in mx.chain:
        # 用一个临时 profile 语义解析该 model：构造一个只含该 model 的 ResolvedModel。
        resolved = _resolve_model_directly(config, model_key)
        sess = _make_session(resolved)
        sess.name = model_key
        sessions.append(sess)

    mixin_cfg = {
        "llm_nos": list(mx.chain),
        "max_retries": mx.max_retries,
        "base_delay": mx.base_delay,
        "spring_back": mx.spring_back,
    }
    # MixinSession 期望 all_sessions 里的元素有 .backend 属性（ToolClient 包装）
    # 或是 dict；这里传包装好的 NativeToolClient，与 agentmain 现有用法一致。
    wrapped = [llmcore.NativeToolClient(s) for s in sessions]
    mixin = llmcore.MixinSession(wrapped, mixin_cfg)
    return llmcore.NativeToolClient(mixin)


def _resolve_model_directly(config: LLMConfig, model_key: str) -> ResolvedModel:
    """不经 profile，直接把一个 model key 解析成 ResolvedModel（defaults < model）。

    供 mixin.chain 使用——chain 引用的是 model key 而非 profile。
    """
    if model_key not in config.models:
        raise ValueError(f"model '{model_key}' 不存在：{sorted(config.models)}")
    model = config.models[model_key]
    provider = config.providers[model.provider]

    from llm_config import _PROFILE_OVERRIDABLE

    params: dict[str, Any] = {}
    d = config.defaults
    for key in _PROFILE_OVERRIDABLE:
        params[key] = getattr(d, key, None)
    params["context_window"] = d.context_window
    for key in _PROFILE_OVERRIDABLE:
        mv = getattr(model, key, None)
        if mv is not None:
            params[key] = mv
    params["_model_key"] = model_key
    return ResolvedModel(model_key, model, provider, model.provider, params)


def try_build_default_client(start_dir: Optional[str] = None):
    """便捷入口：找到 llm.yaml 就构造 active_profile 的 client，否则返回 None。

    agentmain 可先试这个；返回 None 时回退到现有 mykey.py 路径。
    """
    path = find_llm_config(start_dir)
    if not path:
        return None
    config = load_llm_config(path)
    return build_client(config)
