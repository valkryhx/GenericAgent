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
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

WireApi = Literal["openai_chat", "openai_responses", "anthropic"]

# capability 名（models.*.supports 里的取值），供 ResolvedModel.supports() 查表。
KNOWN_CAPABILITIES = frozenset({
    "thinking", "prompt_cache", "1m_context", "image_input", "responses",
})

# 统一思考级别（thinking 字段的合法取值）。三种 wire 写法一致，由配置层按 wire
# 翻译成各自底层字段，屏蔽 Claude(thinking_type+effort) 与 OpenAI/Grok(reasoning_effort)
# 的差异。想精细控制的高级用户仍可直接写 thinking_type / reasoning_effort（优先级更高）。
THINKING_LEVELS = ("off", "low", "medium", "high", "max")

# YAML 的 "Norway problem"：裸写 thinking: off 会被 YAML 解析成布尔 False
# （on/yes/no 同理）。用户自然会写 thinking: off，所以在校验前把布尔还原成字符串，
# 免得逼用户加引号。还原后仍走 THINKING_LEVELS 合法性校验（on→"on" 会被拒）。
_YAML_BOOL_TO_STR = {True: "on", False: "off"}


def _coerce_thinking(v: Any) -> Any:
    """把 YAML 误判成布尔的 thinking 值还原成字符串（off/on）。其它原样返回。"""
    if isinstance(v, bool):
        return _YAML_BOOL_TO_STR[v]
    return v

# 统一级别 → 各 wire 的底层字段翻译表。
#   anthropic：off → 关思考；其余 → adaptive 思考 + output_config.effort（由 reasoning_effort 承载）
#   openai   ：直接映射到 chat/responses 的 reasoning_effort（none/low/medium/high/xhigh）
# 注意：个别模型有硬约束（如 grok-4.5 思考不可关、无 xhigh），那种情况用显式
# reasoning_effort 逃生舱覆盖即可；此表覆盖通用情形。
_THINKING_TO_ANTHROPIC = {
    "off":    {"thinking_type": "disabled"},
    "low":    {"thinking_type": "adaptive", "reasoning_effort": "low"},
    "medium": {"thinking_type": "adaptive", "reasoning_effort": "medium"},
    "high":   {"thinking_type": "adaptive", "reasoning_effort": "high"},
    "max":    {"thinking_type": "adaptive", "reasoning_effort": "xhigh"},
}
_THINKING_TO_OPENAI = {
    "off":    {"reasoning_effort": "none"},
    "low":    {"reasoning_effort": "low"},
    "medium": {"reasoning_effort": "medium"},
    "high":   {"reasoning_effort": "high"},
    "max":    {"reasoning_effort": "xhigh"},
}

def apply_thinking_translation(params: dict[str, Any], wire_api: str) -> None:
    """把合并后的统一 thinking 级别翻译成该 wire 的底层字段，就地写入 params。

    规则：
      - params["thinking"] 为空 → 什么都不做。
      - 否则按 wire_api 选翻译表（anthropic 用 thinking_type+reasoning_effort，
        openai_* 用 reasoning_effort），把级别展开成底层字段。
      - 显式字段优先：若 params 里对应的底层字段已由 model/profile 显式设过
        （非 None），保留显式值，不被翻译覆盖。这样高级用户能精细控制。
    """
    level = params.get("thinking")
    if level is None:
        return
    table = _THINKING_TO_ANTHROPIC if wire_api == "anthropic" else _THINKING_TO_OPENAI
    mapping = table.get(level)
    if not mapping:
        return
    for key, value in mapping.items():
        if params.get(key) is None:
            params[key] = value


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
    """provider 表条目：定义「连哪、什么协议、怎么鉴权」，一次定义多处引用。

    字段分三档，填写时按需取用：

    【必填三件套】—— 任何 provider 都只需要这三个就能跑：
        wire_api   走哪种线协议：openai_chat（默认首选，兼容性最强）/
                   openai_responses / anthropic
        base_url   API 端点。支持自动补全（http://host:2001 会补 /v1/...）
        api_key    明文 API key（一等支持）。或用 api_key_env 放环境变量名。
                   本地无鉴权端点可填空串 ""。

    【通用可选】—— 所有 wire 都可用，不填就用默认：
        api_key_env / proxy / verify / extra_headers / extra_body

    【wire 专属可选】—— 只对某种 wire 有意义，其它 wire 填了会被忽略：
        anthropic 专属：fake_cc_system_prompt、user_agent（反代 CC 协议的中转渠道用）
        openai   专属：native_tools（个别 relay 不支持 tools 字段时设 false）

    注意：模型能力（是否支持图片输入 / thinking / 缓存）不在这里，而在
    对应 model 的 supports 列表里声明——因为那是「模型」的属性，不是「连接」的属性。
    """
    # ── 必填三件套 ───────────────────────────────────────────────
    wire_api: WireApi
    base_url: str
    api_key: Optional[str] = None          # 明文 key（一等）；与 api_key_env 二选一
    # ── 通用可选 ─────────────────────────────────────────────────
    api_key_env: Optional[str] = None      # 只放环境变量名（可选备选）
    proxy: Optional[str] = None
    verify: bool = True
    extra_headers: dict[str, str] = Field(default_factory=dict)  # 逃生舱：吸收渠道怪癖
    extra_body: dict[str, Any] = Field(default_factory=dict)     # 逃生舱：附加 body 字段
    # ── anthropic wire 专属可选 ─────────────────────────────────
    fake_cc_system_prompt: bool = False    # 反代 Claude Code 协议的中转渠道置 true
    user_agent: Optional[str] = None       # 伪装 UA（某些渠道按 UA 白名单校验）
    # ── openai wire 专属可选 ────────────────────────────────────
    native_tools: bool = True              # 端点不支持 tools 字段时设 false

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
    # 两道压缩线相对 context_window 的比率（token 口径，对齐 Codex 90%/95%）。
    #   auto_compact_ratio 软线：摘要式压缩触发（保留信息）
    #   hard_limit_ratio   硬线：裁剪兜底触发（丢最旧消息）
    # 不填则用默认；仅当 context_window 有值时 to_legacy_cfg 才派生出 token 阈值。
    auto_compact_ratio: float = 0.90
    hard_limit_ratio: float = 0.95
    supports: list[str] = Field(default_factory=list)
    # 【推荐】统一思考级别：off/low/medium/high/max，三种 wire 写法一致，
    # 由配置层按 wire 翻译成底层字段。日常只需写这一个。
    thinking: Optional[str] = None
    # 【高级逃生舱】想绕过统一翻译、直接控制底层字段时才填（优先级高于 thinking）。
    thinking_type: Optional[str] = None            # anthropic 专属：adaptive/enabled/disabled
    thinking_budget_tokens: Optional[int] = None   # 仅 thinking_type=enabled 时用
    reasoning_effort: Optional[str] = None          # none/minimal/low/medium/high/xhigh
    # 采样 / 传输默认（可被 profile 覆盖）。
    service_tier: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    stream: Optional[bool] = None
    max_retries: Optional[int] = None
    timeout: Optional[int] = None
    read_timeout: Optional[int] = None
    # 发给 API 的真实 model 字符串；不填则用 model 表的 key 本身。
    api_model: Optional[str] = None

    @field_validator("thinking", mode="before")
    @classmethod
    def _coerce_thinking(cls, v: Any) -> Any:
        return _coerce_thinking(v)

    @model_validator(mode="after")
    def _check_supports(self) -> "ModelCfg":
        unknown = [s for s in self.supports if s not in KNOWN_CAPABILITIES]
        if unknown:
            raise ValueError(
                f"未知 capability {unknown}；合法取值：{sorted(KNOWN_CAPABILITIES)}"
            )
        if self.thinking is not None and self.thinking not in THINKING_LEVELS:
            raise ValueError(
                f"model 的 thinking={self.thinking!r} 非法；合法级别：{list(THINKING_LEVELS)}"
            )
        for name in ("auto_compact_ratio", "hard_limit_ratio"):
            r = getattr(self, name)
            if not (0 < r <= 1):
                raise ValueError(f"model 的 {name}={r} 非法；须在 (0, 1] 区间内")
        if self.auto_compact_ratio >= self.hard_limit_ratio:
            raise ValueError(
                f"auto_compact_ratio({self.auto_compact_ratio}) 必须小于 "
                f"hard_limit_ratio({self.hard_limit_ratio})：软线应早于硬线触发"
            )
        return self


# profile 可覆盖的参数字段（不含 provider/supports/context_window 等结构性字段）。
# thinking 排在最前：它是统一入口，翻译在 resolve() 里展开成底层字段。
_PROFILE_OVERRIDABLE = (
    "thinking", "thinking_type", "thinking_budget_tokens", "reasoning_effort",
    "service_tier", "temperature", "max_tokens", "stream", "max_retries",
    "timeout", "read_timeout",
)


class ProfileCfg(_Strict):
    """命名预设：选一个 model，并可覆盖其参数。/model 命令切换的单位。"""
    model: str
    thinking: Optional[str] = None                  # 统一思考级别，覆盖 model 的同名字段
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

    @field_validator("thinking", mode="before")
    @classmethod
    def _coerce_thinking(cls, v: Any) -> Any:
        return _coerce_thinking(v)

    @model_validator(mode="after")
    def _check_thinking(self) -> "ProfileCfg":
        if self.thinking is not None and self.thinking not in THINKING_LEVELS:
            raise ValueError(
                f"profile 的 thinking={self.thinking!r} 非法；合法级别：{list(THINKING_LEVELS)}"
            )
        return self


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

        window = m.context_window if m.context_window is not None else p.get("context_window")
        put("context_win", window)
        # 两道压缩线：仅当 model 声明了 context_window 时才派生 token 阈值写入 cfg；
        # 未声明则不写，交给 Session 端从 context_win 默认值兜底派生（行为等价）。
        if m.context_window is not None:
            put("auto_compact_tokens", round(m.context_window * m.auto_compact_ratio))
            put("hard_limit_tokens", round(m.context_window * m.hard_limit_ratio))
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
            # 图片输入是「模型能力」，从 model 的 supports 推导，而非 provider 字段。
            cfg["native_image_input"] = self.supports("image_input")
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
        # 统一 thinking 级别 → 该 wire 的底层字段（显式底层字段优先，不被覆盖）。
        apply_thinking_translation(params, provider.wire_api)
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
