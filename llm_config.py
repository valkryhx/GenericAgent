"""
GA LLM 接入层 —— YAML + pydantic 配置层（阶段 1）。

背景与设计依据见 docs/ga_llm_config_research_2026-07-16.md。本模块只负责
「配置态」：读 llm.yaml → pydantic 校验 → 解析出一个扁平化的运行参数对象，
并生成与现有 llmcore.Session 类兼容的 cfg dict。它不 import llmcore（保持
可独立单测、无重依赖）；把配置喂进 Session 类的薄封装在 build_client() 里
惰性 import llmcore。

核心取代的东西：agentmain.py:188-201 的「变量名魔法」——过去靠 mykey.py 里
变量名是否含 native/claude/oai 决定实例化哪个 Session 类。现在由 provider 的
显式 wire_api 字段决定。

三种 wire protocol 全部一等支持（不照搬 Codex 的单协议收敛）：
  - openai_chat       OpenAI Chat Completions —— 兼容性最强、接入新模型的默认首选，
                      覆盖绝大多数第三方/开源/本地/中转模型（vLLM/Ollama/DeepSeek/
                      Kimi/SiliconFlow/各类 relay…）。
  - openai_responses  OpenAI /v1/responses —— OpenAI 原生端点的可选增强。
  - anthropic         Anthropic messages —— Claude 原生端点 / 反代 CC 协议的渠道。

密钥策略（按用户明确要求）：api_key 明文写在 llm.yaml 里是一等支持方式，
无需藏进环境变量。api_key_env（只放环境变量名）作为可选备选，两者都可选，
但每个 provider 至少要能解析出一个 key（本地无鉴权端点可留空字符串）。
所有字符串值支持 ${ENV_VAR} 插值。
"""

from __future__ import annotations

import os
import re
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

WireApi = Literal["openai_chat", "openai_responses", "anthropic"]

# capability 名（models.*.supports 里的取值），供 ResolvedModel.supports() 查表。
KNOWN_CAPABILITIES = frozenset({
    "thinking", "prompt_cache", "1m_context", "image_input", "responses",
})

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _interpolate_env(value: Any) -> Any:
    """把字符串里的 ${ENV_VAR} 替换成环境变量值；递归处理 dict/list。

    未定义的环境变量替换为空串（本地无鉴权端点常见）。非字符串原样返回。
    """
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _interpolate_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate_env(v) for v in value]
    return value


class _Strict(BaseModel):
    """所有配置模型的基类：拒绝未知字段，让拼写错误在加载期就报错。"""
    model_config = ConfigDict(extra="forbid")


class ProviderCfg(_Strict):
    """provider 表条目：定义「连哪、什么协议、怎么鉴权」，一次定义多处引用。"""
    wire_api: WireApi
    base_url: str
    # 密钥：api_key 明文一等支持；api_key_env 只放环境变量名，可选备选。
    api_key: Optional[str] = None
    api_key_env: Optional[str] = None
    # 渠道专属开关（保留 GA 现有能力）。
    fake_cc_system_prompt: bool = False
    user_agent: Optional[str] = None
    # 逃生舱：吸收单个渠道怪癖，不污染通用构造逻辑。
    extra_headers: dict[str, str] = Field(default_factory=dict)
    extra_body: dict[str, Any] = Field(default_factory=dict)
    proxy: Optional[str] = None
    verify: bool = True
    # OpenAI 系专属（对 anthropic wire 无意义，留默认即可）。
    native_tools: bool = True
    native_image_input: bool = False

    def resolve_api_key(self) -> str:
        """解析实际 key：优先明文 api_key，其次 api_key_env 指向的环境变量。

        两者都没有时返回空串（允许本地无鉴权端点）。
        """
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            key = os.environ.get(self.api_key_env)
            if key:
                return key
            raise ValueError(
                f"provider 的 api_key_env='{self.api_key_env}' 指向的环境变量未设置，"
                f"且未提供明文 api_key。请设置该环境变量或直接在 llm.yaml 写 api_key。"
            )
        return ""


class ModelCfg(_Strict):
    """model 表条目：绑定 provider，声明 capability 与默认参数。"""
    provider: str
    context_window: Optional[int] = None
    supports: list[str] = Field(default_factory=list)
    # 采样 / 推理默认（可被 profile 覆盖）。
    thinking_type: Optional[str] = None
    thinking_budget_tokens: Optional[int] = None
    reasoning_effort: Optional[str] = None
    service_tier: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    stream: Optional[bool] = None
    max_retries: Optional[int] = None
    timeout: Optional[int] = None
    read_timeout: Optional[int] = None
    # 发给 API 的真实 model 字符串；不填则用 model 表的 key 本身。
    api_model: Optional[str] = None

    @model_validator(mode="after")
    def _check_supports(self) -> "ModelCfg":
        unknown = [s for s in self.supports if s not in KNOWN_CAPABILITIES]
        if unknown:
            raise ValueError(
                f"未知 capability {unknown}；合法取值：{sorted(KNOWN_CAPABILITIES)}"
            )
        return self


# profile 可覆盖的参数字段（不含 provider/supports/context_window 等结构性字段）。
_PROFILE_OVERRIDABLE = (
    "thinking_type", "thinking_budget_tokens", "reasoning_effort", "service_tier",
    "temperature", "max_tokens", "stream", "max_retries", "timeout", "read_timeout",
)


class ProfileCfg(_Strict):
    """命名预设：选一个 model，并可覆盖其参数。/model 命令切换的单位。"""
    model: str
    thinking_type: Optional[str] = None
    thinking_budget_tokens: Optional[int] = None
    reasoning_effort: Optional[str] = None
    service_tier: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    stream: Optional[bool] = None
    max_retries: Optional[int] = None
    timeout: Optional[int] = None
    read_timeout: Optional[int] = None


class MixinCfg(_Strict):
    """故障转移：按 chain 里的 model key 顺序尝试，失败切下一个。"""
    chain: list[str]
    max_retries: int = 3
    base_delay: float = 1.5
    spring_back: int = 300


class DefaultsCfg(_Strict):
    """全局默认，被 model/profile 覆盖。"""
    max_tokens: Optional[int] = 8192
    temperature: Optional[float] = 1.0
    stream: bool = True
    max_retries: Optional[int] = None
    timeout: Optional[int] = None
    read_timeout: Optional[int] = None
    context_window: Optional[int] = None


class ResolvedModel:
    """运行态：一个 profile 解析后的扁平结果。

    含选定 model 的所有参数（defaults < model < profile 合并后）+ 其 provider。
    提供 to_legacy_cfg() 生成兼容现有 llmcore.Session 的 cfg dict，以及
    supports() 查 capability。不含任何构造逻辑，纯数据。
    """

    def __init__(self, name: str, model: ModelCfg, provider: ProviderCfg,
                 provider_key: str, params: dict[str, Any]):
        self.name = name                  # profile 名（也用作 session 展示名）
        self.model = model
        self.provider = provider
        self.provider_key = provider_key
        self.params = params              # 合并后的最终参数
        self.wire_api: WireApi = provider.wire_api

    def supports(self, capability: str) -> bool:
        return capability in self.model.supports

    def to_legacy_cfg(self) -> dict[str, Any]:
        """生成兼容 llmcore.BaseSession.__init__ 的 cfg dict。

        wire_api → api_mode 映射：
          openai_chat      → api_mode='chat_completions'
          openai_responses → api_mode='responses'
          anthropic        → api_mode 无意义（NativeClaudeSession 不读）
        """
        p, m, prov = self.params, self.model, self.provider
        cfg: dict[str, Any] = {
            "name": self.name,
            "apikey": prov.resolve_api_key(),
            "apibase": prov.base_url,
            "model": m.api_model or self.params.get("_model_key", self.name),
        }
        # 可选字段仅在有值时写入，保持 cfg 精简、让 Session 用自己的默认。
        def put(key: str, value: Any) -> None:
            if value is not None:
                cfg[key] = value

        put("context_win", m.context_window if m.context_window is not None else p.get("context_window"))
        put("max_retries", p.get("max_retries"))
        put("timeout", p.get("timeout"))
        put("read_timeout", p.get("read_timeout"))
        put("reasoning_effort", p.get("reasoning_effort"))
        put("service_tier", p.get("service_tier"))
        put("thinking_type", p.get("thinking_type"))
        put("thinking_budget_tokens", p.get("thinking_budget_tokens"))
        put("temperature", p.get("temperature"))
        put("max_tokens", p.get("max_tokens"))
        put("stream", p.get("stream"))
        put("proxy", prov.proxy)
        cfg["verify"] = prov.verify

        if self.wire_api == "openai_responses":
            cfg["api_mode"] = "responses"
        elif self.wire_api == "openai_chat":
            cfg["api_mode"] = "chat_completions"

        # 渠道专属 / 逃生舱字段。
        if prov.fake_cc_system_prompt:
            cfg["fake_cc_system_prompt"] = True
        if prov.user_agent:
            cfg["user_agent"] = prov.user_agent
        if self.wire_api in ("openai_chat", "openai_responses"):
            cfg["native_tools"] = prov.native_tools
            cfg["native_image_input"] = prov.native_image_input
        if prov.extra_headers:
            cfg["extra_headers"] = dict(prov.extra_headers)
        if prov.extra_body:
            cfg["extra_body"] = dict(prov.extra_body)
        return cfg


class LLMConfig(_Strict):
    """llm.yaml 的根结构。"""
    providers: dict[str, ProviderCfg]
    models: dict[str, ModelCfg]
    profiles: dict[str, ProfileCfg] = Field(default_factory=dict)
    mixin: dict[str, MixinCfg] = Field(default_factory=dict)
    defaults: DefaultsCfg = Field(default_factory=DefaultsCfg)
    active_profile: Optional[str] = None
    # 允许非 LLM 的杂项键（如 tui_recent_sessions_limit）从同一文件读取。
    settings: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_refs(self) -> "LLMConfig":
        # 每个 model 的 provider 必须存在。
        for mk, m in self.models.items():
            if m.provider not in self.providers:
                raise ValueError(
                    f"model '{mk}' 引用了不存在的 provider '{m.provider}'；"
                    f"已定义 providers：{sorted(self.providers)}"
                )
        # 每个 profile 的 model 必须存在。
        for pk, prof in self.profiles.items():
            if prof.model not in self.models:
                raise ValueError(
                    f"profile '{pk}' 引用了不存在的 model '{prof.model}'；"
                    f"已定义 models：{sorted(self.models)}"
                )
        # active_profile 必须存在。
        if self.active_profile is not None and self.active_profile not in self.profiles:
            raise ValueError(
                f"active_profile '{self.active_profile}' 不在 profiles 中：{sorted(self.profiles)}"
            )
        # mixin.chain 里的每个 model 必须存在。
        for mixk, mx in self.mixin.items():
            for ref in mx.chain:
                if ref not in self.models:
                    raise ValueError(
                        f"mixin '{mixk}' 的 chain 引用了不存在的 model '{ref}'；"
                        f"已定义 models：{sorted(self.models)}"
                    )
        return self

    def resolve(self, profile_name: Optional[str] = None) -> ResolvedModel:
        """解析一个 profile → ResolvedModel（defaults < model < profile 合并）。

        profile_name 为空时用 active_profile；仍为空且只有一个 profile 时用它。
        """
        name = profile_name or self.active_profile
        if name is None:
            if len(self.profiles) == 1:
                name = next(iter(self.profiles))
            else:
                raise ValueError(
                    "未指定 profile 且无 active_profile，无法确定用哪个。"
                    f"可选 profiles：{sorted(self.profiles)}"
                )
        if name not in self.profiles:
            raise ValueError(f"profile '{name}' 不存在：{sorted(self.profiles)}")

        prof = self.profiles[name]
        model = self.models[prof.model]
        provider = self.providers[model.provider]

        # 合并优先级：defaults < model < profile。
        params: dict[str, Any] = {}
        d = self.defaults
        for key in _PROFILE_OVERRIDABLE:
            params[key] = getattr(d, key, None)
        params["context_window"] = d.context_window
        for key in _PROFILE_OVERRIDABLE:
            mv = getattr(model, key, None)
            if mv is not None:
                params[key] = mv
        for key in _PROFILE_OVERRIDABLE:
            pv = getattr(prof, key, None)
            if pv is not None:
                params[key] = pv
        params["_model_key"] = prof.model
        return ResolvedModel(name, model, provider, model.provider, params)


def load_llm_config(path: str) -> LLMConfig:
    """读取并校验 llm.yaml，返回 LLMConfig。字符串值做 ${ENV_VAR} 插值。"""
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} 顶层必须是 mapping，实际为 {type(raw).__name__}")
    raw = _interpolate_env(raw)
    return LLMConfig.model_validate(raw)


def find_llm_config(start_dir: Optional[str] = None) -> Optional[str]:
    """在 start_dir（默认本模块目录）找 llm.yaml / llm.yml，找到返回路径，否则 None。"""
    base = start_dir or os.path.dirname(os.path.abspath(__file__))
    for name in ("llm.yaml", "llm.yml"):
        p = os.path.join(base, name)
        if os.path.exists(p):
            return p
    return None
