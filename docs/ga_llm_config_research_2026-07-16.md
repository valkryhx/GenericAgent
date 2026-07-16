# GA LLM 接入层重构调研 —— 现状剖析 + Codex/Claude Code 对照 + YAML 方案

**日期：** 2026-07-16
**调研对象：** GA 自身 `llmcore.py` / `mykey.py`；Codex CLI（`D:\git_codes\codex\codex-rs`，Rust）；Claude Code（`D:\git_codes\claude-reviews-claude\claude-code-fork\src`，TypeScript）
**本文定位：** 回答用户两个问题——(1) GA 是否用 litellm 接入？(2) 能否写出更简单优雅的接入代码？——并给出一份面向 GA 的 YAML 配置重构方案。**本文是调研 + 方案文档，不含已落地代码。**

---

## 0. 结论先行

1. **GA 没有用 litellm，也没用任何官方 SDK。** 依赖里只有 `requests`；所有 provider 的 HTTP 请求、SSE 流解析、工具协议转换都在 `llmcore.py`（1198 行）里手写。
2. **GA「杂乱」的根因是配置语义编码在变量名里**：`agentmain.py` 扫描 `mykey.py` 中变量名含 `api`/`config`/`cookie` 的条目，再靠名字里是否含 `native`/`claude`/`oai`/`mixin` 决定实例化哪个 Session 类。加一个 provider = 记住一套命名魔法 + 可能新增一个 Session 子类。
3. **Codex 和 Claude Code 都不用 litellm**，各自手写接入，但配置层都远比 GA 清晰：Codex 用 **TOML + provider 表 + key 引用**，Claude Code 用 **JSON + 环境变量驱动 + 分层合并**。
4. **推荐 GA 走 YAML**（PyYAML 6.0.3、pydantic 2.12.5 已在环境中，无需新增重依赖），吸收三条核心经验：**provider 表 + model 引用**（Codex）、**密钥不入配置只放 env 名 / helper**（两者）、**配置态 vs 运行态分离 + 单一构造入口**（两者）。详见 §5、§6。

---

## 1. GA 现状剖析（已读实源码）

### 1.1 接入方式：纯手写 requests，无 litellm/SDK

`pyproject.toml` 的 `dependencies` 仅 `requests / beautifulsoup4 / bottle / simple-websocket-server`。`llmcore.py` 顶部 `import requests`，全部请求靠它发；SSE 解析是手写的行解析器：

- `_parse_claude_sse` / `_parse_openai_sse`：手写解析两种 provider 的流式响应
- `_parse_claude_json` / `_parse_openai_json`：非流式响应解析
- `_msgs_claude2oai` / `_to_responses_input`：消息格式在 Claude 风格 ↔ OpenAI 风格之间手工转换
- `_stream_with_retry`：手写重试 + 取消 + 超时

**评价：** 手写不是坏事——它让 GA 零重依赖、对每个渠道的怪癖有完全控制力（这正是接大量第三方中转渠道的项目需要的）。问题不在"手写请求"，而在"配置层"。

### 1.2 根本痛点：变量名魔法

`agentmain.py:188-201`（核心分发逻辑）：

```python
for k, cfg in mykeys.items():
    if not any(x in k for x in ['api', 'config', 'cookie']): continue
    if 'mixin' in k: llm_sessions += [{'mixin_cfg': cfg}]
    # ... 再靠 k 里的 native/claude/oai 关键字选 Session 类
```

配置项 `native_claude_config_100x = {...}` 的**行为由变量名决定**：
- 含 `native` + `claude` → `NativeClaudeSession`
- 含 `native` + `oai` → `NativeOAISession`
- 含 `claude`（无 native）→ `ClaudeSession`（文本协议，已废弃）
- 含 `oai`（无 native）→ `LLMSession`（文本协议，已废弃）
- 含 `mixin` → `MixinSession`（故障转移）

`mykey_template.py` 用了整整 60 行注释来解释这套命名规则和优先级陷阱（"`oai_claude_xxx` 会被 `claude` 抢先匹配"）。**这就是"杂乱"的来源：一个本该是显式字段（`provider: anthropic`）的语义，被藏进了变量名的子串匹配里。**

### 1.3 Session 类谱系（按"协议 × 工具方式"分裂）

| 类 | 协议 | 工具方式 | 状态 |
|---|---|---|---|
| `NativeClaudeSession` | Anthropic messages | 原生 tool 字段 | 推荐 |
| `NativeOAISession` | OpenAI chat/responses | 原生 tool 字段 | 推荐 |
| `ClaudeSession` | Anthropic messages | 文本协议工具 | 废弃 |
| `LLMSession` | OpenAI | 文本协议工具 | 废弃 |
| `MixinSession` | 包装多个上面的 | 故障转移 | — |

外层再套 `ToolClient` / `NativeToolClient`。`BaseSession.__init__` 用约 20 个 `cfg.get('field', default)` 手动摊开所有字段（`apikey`/`apibase`/`model`/`context_win`/`max_retries`/`reasoning_effort`/`thinking_type`/`thinking_budget_tokens`/`temperature`/`max_tokens`/`api_mode`/…）。

### 1.4 apibase 自动拼接 + apikey 头自动判断（这部分其实很好）

GA 有两处**值得保留**的聪明设计：
- **apibase 自动补全**：`http://host:2001` → 补 `/v1/chat/completions`；给了 `/v1` 就补 `/chat/completions`；给全了就原样用。降低配置负担。
- **apikey 头自动判断**：`sk-ant-*` 用 `x-api-key`，其它前缀用 `Authorization: Bearer`。

这两点和 Claude Code 的做法同源（见 §3.4），重构时应保留。

### 1.5 mykey.py 现状

42KB 文件，但真正的顶层配置条目只有 5 个（`tui_recent_sessions_limit`、`mixin_config`、`native_claude_config_100x`、`native_oai_config`、`llm_profile_configs`）。其余全是注释文档。已有一个 `llm_profile_configs` 列表 + `expand_llm_profile_configs()` 的雏形（把列表里带 `key` 的 profile 展开成顶层配置）——**说明 GA 自己已经在往"结构化列表"方向挪，只是还半途**。

---

## 2. Codex CLI 的做法（TOML + provider 表 + key 引用）

**格式：TOML**，`~/.codex/config.toml`。核心范式：

```toml
model = "gpt-5.4"              # 顶层：选 model
model_provider = "openai"     # 顶层：选 provider（指向下面表里的 key）
model_reasoning_effort = "high"

[model_providers.azure]        # provider 表：一次定义，多处引用
name = "Azure"
base_url = "https://xxx.openai.azure.com/openai"
env_key = "AZURE_OPENAI_API_KEY"   # ← 存"环境变量名"，不是 key 本身
query_params = { api-version = "2025-04-01-preview" }
http_headers = { "X-Example" = "value" }
env_http_headers = { "X-Env-Header" = "SOME_ENV_VAR" }

[profiles.o3-azure]            # profile：一组命名预设
model = "o3"
model_provider = "azure"
model_reasoning_effort = "high"
```

**六个可借鉴设计：**
1. **provider 表 + key 引用 model**（`model` 和 `model_provider` 两个独立字符串字段）—— 避免把 provider 内联进每个 model，切换只改一个 key。
2. **密钥永不入配置，只放环境变量名**（`env_key = "OPENAI_API_KEY"`）—— 配置文件可放心提交/分享。运行时 `std::env::var(env_key)`，缺失时报错并附 `env_key_instructions`。
3. **配置态结构体 vs 运行态结构体分离**（`ModelProviderInfo` → `Provider`），转换函数 `to_api_provider()` 是唯一的桥。运行态不含任何 secret。
4. **Profiles = 命名预设组**：model + provider + 参数打包成一个命名单元，一键切换。
5. **provider 差异靠数据字段吸收**（`query_params` / `http_headers` / `env_http_headers`），而不是给每家写专用 client。Azure/Bedrock 全靠这几个字段接入。
6. **把所有 provider 规约到同一 wire protocol**：Codex 甚至收敛到只剩 Responses 一种，用自定义 deserialize 对废弃值 `wire_api = "chat"` 返回带迁移链接的友好报错。

**对 GA 的启示（关键差异）：** Codex 是"单一 wire protocol + 数据字段吸收差异"。GA 明确要**同时支持 Anthropic 原生和 OpenAI 两种 wire protocol**，所以不能完全照搬"只留一种协议"，但**"provider 表 + key 引用 + wire_api 字段声明协议"**这个结构完全适用——把 GA 现在藏在变量名里的 `native`/`claude`/`oai` 变成显式的 `wire_api: anthropic|openai` 字段。

---

## 3. Claude Code 的做法（JSON + 环境变量驱动 + 三层解耦）

### 3.1 三层解耦（最值得借鉴的顶层设计）

1. **配置层**（`settings.json` + env）—— 决定"用什么"
2. **Provider/Client 层**（`client.ts` 的 `getAnthropicClient()`）—— 决定"连哪、怎么鉴权"
3. **请求构造层**（`claude.ts` 的 `paramsFromContext()`）—— 决定"发什么 headers/body/beta/cache"

所有 tool schema 过 `toolToAPISchema` 一个 choke point，所有 body 过 `paramsFromContext` 一个函数。**单一构造入口**是关键。

### 3.2 Provider 判定靠环境变量（只有 4 个枚举）

```typescript
type APIProvider = 'firstParty' | 'bedrock' | 'vertex' | 'foundry'
// 靠 CLAUDE_CODE_USE_BEDROCK / _USE_VERTEX / _USE_FOUNDRY 环境变量判定
```

每个 provider 动态 import 对应 SDK（非活跃 provider 不加载）；共享一个 `ARGS` 基础配置对象再叠加专属参数。第三方兼容端点靠 `ANTHROPIC_BASE_URL`，很多 beta 特性 gate 在 `isFirstPartyAnthropicBaseUrl()` 上，防止代理网关（LiteLLM 等）因不认识的字段返回 400。

### 3.3 Model 选择：优先级链 + canonical 归一化

优先级：`/model` session 覆盖 > `--model` flag > `ANTHROPIC_MODEL` env > `settings.json` model 字段 > 内置默认。

**canonical 名归一化**（很值得学）：把 `claude-3-5-haiku-20241022` 和 `us.anthropic.claude-3-5-haiku-20241022-v1:0` 都映射到 `claude-3-5-haiku`，**所有 capability 判断（支不支持 thinking / 1M 上下文）都基于 canonical 名**。加新 provider/model 只改映射表。

### 3.4 鉴权：多来源 + helper 模式

优先级：`ANTHROPIC_API_KEY` env（需在 approved 白名单）> fd 传入 > `apiKeyHelper` 脚本输出 > config 存的 key / keychain > OAuth token。

**x-api-key vs Bearer 的区分**（和 GA §1.4 同源）：OAuth → `Authorization: Bearer`；API key → `x-api-key`；`ANTHROPIC_AUTH_TOKEN` → 显式 Bearer。

**apiKeyHelper 机制**（很值得借鉴）：配一个 shell 命令，跑它取 stdout 当 key，带 **stale-while-revalidate 缓存**（5 分钟 TTL，过期先返回旧值后台刷新），且**来自项目配置的 helper 执行前要过 workspace trust gate**（防恶意 repo 注入命令）。

### 3.5 env 注入统一了"配置文件"和"环境变量"

`settings.json` 的 `env` 字段会被 `Object.assign` 到 `process.env`。所以 JSON 只是环境变量的**持久化前端**，读取侧永远只读 env。**一个反向提醒**：CC 几乎不把端点写进 JSON、全靠 env，是因为要兼容企业 MDM/CI/代理生态。GA 改 YAML 是合理的（更适合单项目、可读性好），但要吸收它的**优先级明确 + env 插值 + 凭据用 helper 而非明文**。

---

## 4. 三方对照表

| 维度 | GA 现状 | Codex | Claude Code | GA 应采纳 |
|---|---|---|---|---|
| 接入库 | 手写 requests | 手写（Rust reqwest） | 官方 SDK + 手写 | 保留手写（零依赖优势） |
| 配置格式 | Python 变量（.py） | TOML | JSON | **YAML** |
| provider 选择 | **变量名子串匹配** ✗ | 显式 `model_provider` key | 环境变量枚举 | **显式字段 + 表引用** |
| 密钥存放 | 明文写在 .py 里 ✗ | 环境变量名 `env_key` | env / helper / keychain | **env 名 + helper，禁明文** |
| 协议差异 | 每种一个 Session 类 | 单一 wire + 数据字段 | 每 provider 动态 SDK | **`wire_api` 字段 + 共享构造** |
| 命名预设 | `llm_profile_configs`（半成品） | `[profiles.x]` | 无（靠 model 别名） | **`profiles:` 表** |
| 配置态/运行态 | 混在 Session 里 | 明确分离 ✓ | 明确分离 ✓ | **分离** |
| model capability | 散落 if + 字符串判断 | model 名前缀 | **canonical 归一化表** ✓ | **canonical 表** |
| 校验 | 无（运行时才炸） | serde deny_unknown | Zod + 优雅降级 | **pydantic extra=forbid** |

---

## 5. 面向 GA 的 YAML 方案（草案）

### 5.0 不可协商的设计约束：三种 wire protocol 全部保留，Chat Completions 是一等默认

在进入方案细节前，先钉死一条**贯穿全文的硬约束**（用户明确要求）：

> **GA 必须同时长期支持三种 wire protocol —— `openai_chat`（Chat Completions）、`openai_responses`、`anthropic`。其中 Chat Completions 是兼容性最强、接入新模型的默认首选，绝不能被移除或降级。**

原因：GA 的定位是"接入各种模型"，而 **Chat Completions 是事实上的通用方言**——绝大多数第三方 / 开源 / 本地 / 中转模型只暴露这个接口（vLLM、Ollama、LM Studio、DeepSeek、Kimi/Moonshot、MiniMax、SiliconFlow、OpenRouter、各类 relay 网关…）。`openai_responses` 和 `anthropic` 是特定官方端点的原生增强，覆盖面窄。

因此 **Codex 的"把所有 provider 收敛到单一 wire protocol（只剩 Responses）"这一条对 GA 反例，明确不照搬**（见 §4 §6）。GA 借鉴 Codex 的是"provider 表 + key 引用 + `wire_api` 字段声明协议"这个**结构**，而不是"只留一种协议"这个**取舍**。下文所有设计都在此约束下展开：wire adapter 的数量可以收敛（2 个 adapter），但**支持的协议种类不收敛（3 种）**，且 Chat Completions 永远是默认路径。

### 5.1 文件：`llm.yaml`（与 mykey 并存，逐步迁移）

```yaml
# ── provider 表：定义"连哪、什么协议、怎么鉴权"，一次定义多处引用 ──
providers:
  anthropic:
    wire_api: anthropic              # anthropic | openai_chat | openai_responses
    base_url: https://api.anthropic.com
    api_key_env: ANTHROPIC_API_KEY   # ← 只放环境变量名，不放 key 本身
    # api_key_helper: "pass show anthropic/key"   # 或跑命令拿 key（带缓存）

  # openai_chat = OpenAI Chat Completions，最具兼容性、覆盖面最广的接口：
  # 绝大多数第三方/开源/本地模型（vLLM、Ollama、SiliconFlow、DeepSeek、Kimi、
  # 各类中转渠道…）都实现它。接入新模型时它是默认首选。
  openai:
    wire_api: openai_chat
    base_url: https://api.openai.com/v1
    api_key_env: OPENAI_API_KEY

  # openai_responses 仅用于确实需要 /v1/responses 的 OpenAI 原生端点；
  # 不确定时一律用 openai_chat。
  openai-responses:
    wire_api: openai_responses
    base_url: https://api.openai.com/v1
    api_key_env: OPENAI_API_KEY

  # 任意 OpenAI 兼容的第三方/本地模型，都走 openai_chat
  deepseek:
    wire_api: openai_chat
    base_url: https://api.deepseek.com/v1
    api_key_env: DEEPSEEK_API_KEY

  local-vllm:
    wire_api: openai_chat
    base_url: http://localhost:8000/v1
    api_key_env: VLLM_API_KEY        # 本地无鉴权时可留空/占位

  my-relay:                          # 第三方中转渠道（反代 Anthropic 协议）
    wire_api: anthropic
    base_url: http://host:2001       # 自动补全 /v1/... 规则保留
    api_key_env: RELAY_KEY
    fake_cc_system_prompt: true      # 反代 CC 协议的渠道专属开关
    extra_headers:                   # 逃生舱：吸收渠道怪癖
      User-Agent: "claude-cli/2.1.113 (external, cli)"

# ── model 表：绑定 provider，声明 capability 与默认参数 ──
models:
  claude-opus-4-8:
    provider: anthropic
    context_window: 400000
    supports: [thinking, prompt_cache, 1m_context]
    thinking_type: adaptive
  gpt-5.4:
    provider: openai
    reasoning_effort: high

# ── profiles：一组命名预设，一键切换（/model 引用） ──
profiles:
  default:
    model: claude-opus-4-8
  fast:
    model: gpt-5.4
    reasoning_effort: minimal

# ── mixin：故障转移（保留现有能力） ──
mixin:
  failover:
    chain: [claude-opus-4-8, gpt-5.4]
    max_retries: 5

# ── 全局默认（被 profile/model 覆盖） ──
defaults:
  max_tokens: 8192
  temperature: 1.0
  stream: true

active_profile: default
```

### 5.2 关键设计点（映射到调研发现）

1. **`providers` 表 + `models.*.provider` 引用**（Codex §2.1）—— 消灭变量名魔法。GA 现在的 `native`/`claude`/`oai` 语义变成 provider 的 `wire_api` 显式字段。
2. **`api_key_env` / `api_key_helper`，禁止明文 key**（Codex + CC §3.4）—— `llm.yaml` 可提交，secret 走环境变量或 helper。加载时 `os.environ[name]`，缺失抛带提示的错误。
3. **`wire_api` 字段声明协议，三种取值全部一等支持**（Codex §2.6 的变体，但**不照搬其收敛**）—— GA 明确保留 **`openai_chat`（Chat Completions）/ `openai_responses` / `anthropic`** 三种 wire，由数据字段选择，不再需要 4 个 Session 子类。**关键约束：`openai_chat`（Chat Completions）是兼容性最强、必须长期保留的一等接口**——它是接入新模型的默认首选，覆盖绝大多数第三方 / 开源 / 本地 / 中转模型（vLLM、Ollama、DeepSeek、Kimi、SiliconFlow、各类 relay…）。`openai_responses` 只是 OpenAI 原生端点的可选增强,**绝不能像 Codex 那样把协议收敛到只剩 Responses**。协议实现收敛成 wire adapter（见 §5.3），但 adapter 数量的收敛 ≠ 支持的协议种类收敛。
4. **`supports: [...]` capability 声明**（CC §3.3 canonical 表的简化）—— thinking/cache/1m 判断查表，不再散落 if。
5. **`extra_headers` / `extra_body` 逃生舱**（CC §3.5）—— 吸收单个渠道怪癖，不污染通用构造逻辑。保留 GA 现有的 `fake_cc_system_prompt` / `user_agent` 等渠道开关。
6. **pydantic schema 校验 + `extra='forbid'`**（CC Zod §6.6）—— 配置拼写错误在加载期就报错，而非运行时才炸。pydantic 2.12.5 已就位。
7. **保留 GA 的 apibase 自动补全 + apikey 头自动判断**（§1.4）—— 这两点本就优秀。
8. **优先级链**（CC §3.3）：`/model` session 覆盖 > 环境变量 > `active_profile` > `defaults`。`${ENV_VAR}` 插值让 YAML 值能引用环境变量，且真实 env 优先级高于文件。

### 5.3 代码结构建议（配置态 / 运行态分离）

```
llm_config.py     # pydantic 模型：ProviderCfg / ModelCfg / ProfileCfg + YAML 加载 + 校验
                  #   （配置态，含 api_key_env、supports、extra_* 等元数据）
llm_wire.py       # wire adapter：AnthropicWire / OpenAIWire
                  #   输入"运行态请求参数"，负责 headers/body/SSE 解析（吃掉 llmcore 手写部分）
                  #   ★ OpenAIWire 内部按 wire_api 分 chat_completions / responses 两种 body 构造
                  #     与解析路径——Chat Completions 是默认且必须保留的兼容路径，Responses 是增强
llm_client.py     # resolve(profile) → RuntimeClient（运行态，只含解析好的 base_url/headers/
                  #   超时/wire adapter，不含 secret）；单一构造入口 build_request()
```

`llmcore.py` 现有的 SSE 解析器、消息转换、重试逻辑**大部分可直接搬进 `llm_wire.py`**（它们本身没问题，只是被埋在 Session 类里）。真正要拆掉的是"配置 → Session 类"的变量名分发，换成"YAML → pydantic → resolve → RuntimeClient"。

---

## 6. 是否引入 litellm？（明确建议：不）

**不建议为 GA 引入 litellm。** 理由：

1. **GA 的核心价值之一是零重依赖 + 对渠道怪癖的完全控制。** GA 大量对接第三方中转/镜像渠道（anyrouter、claude-relay、tabcode…），这些渠道有各种非标准行为（UA 白名单、fake CC system prompt、SSE 被 CDN 截断等）。litellm 的抽象会挡在中间，反而更难 patch 这些怪癖。
2. **Codex 和 Claude Code 都没用 litellm**，各自手写——印证了"认真做 agent 的项目倾向于自己控制 wire 层"。
3. **GA 的痛点不在"请求怎么发"，在"配置怎么组织"。** litellm 解决的是前者（GA 已手写得够好），后者才是本次要重构的。用 YAML + pydantic 重构配置层，比引入 litellm 更对症、更轻。

**唯一值得考虑 litellm 的场景**：如果未来 GA 想低成本支持大量长尾 provider（Cohere/Mistral/Gemini/…各自的原生协议）且不在乎渠道怪癖控制力，litellm 的 100+ provider 映射能省事。但对 GA 当前的定位——**以 Chat Completions 为通用默认吃下绝大多数模型/中转渠道，辅以 OpenAI Responses 与 Anthropic 两种原生增强**——自己的 2 个 wire adapter（其中 OpenAI adapter 内含 chat / responses 双路径）控制力更强、更合适。注意：Chat Completions 本身就是 litellm 覆盖长尾 provider 的主要手段，而 GA 已原生支持它，所以引入 litellm 的边际收益进一步降低。

---

## 7. 迁移路径建议（分阶段，不破坏现有）

1. **阶段 0（本调研）**：产出本文档，确认方向。
2. **阶段 1**：实现 `llm_config.py`（pydantic 模型 + YAML 加载 + 校验），能读 `llm.yaml` 产出配置态对象。**与现有 mykey.py 并存**，`llm.yaml` 不存在时回退老路径。
3. **阶段 2**：实现 `llm_wire.py`（把 `llmcore.py` 的 SSE 解析/消息转换搬过来，收敛成 2 个 wire adapter）+ `llm_client.py`（resolve + 单一构造入口）。
4. **阶段 3**：`agentmain.py` 的分发逻辑改为"读 `llm.yaml` → resolve profile → RuntimeClient"，变量名魔法退役。老 mykey.py 提供一个一次性转换脚本（`mykey.py` → `llm.yaml`）。
5. **阶段 4**：删除废弃的 `ClaudeSession` / `LLMSession`（文本协议工具，已标废弃）。

**每个阶段都可独立验证、可回退。** 建议每阶段配 pydantic 模型的单元测试（校验 YAML 解析、优先级链、env 插值、缺失 key 报错）。

---

## 8. 涉及文件索引

| 文件 | 角色 |
|---|---|
| `llmcore.py` | GA 现状：手写请求 + Session 类 + SSE 解析（1198 行，SSE/转换部分可复用） |
| `mykey.py` / `mykey_template*.py` | GA 现状：变量名魔法配置（待被 `llm.yaml` 取代） |
| `agentmain.py:188-201` | GA 现状：变量名子串匹配分发（待重构为 resolve profile） |
| Codex `model-provider-info/src/lib.rs` | provider 表 + env_key + 配置态/运行态分离的范本 |
| Codex `config/src/profile_toml.rs` | profiles 命名预设的范本 |
| CC `services/api/client.ts` | 三层解耦的 client 构造 + 鉴权头范本 |
| CC `utils/model/model.ts` | model 优先级链 + canonical 归一化范本 |
| CC `utils/auth.ts` | apiKeyHelper（跑命令拿 key + SWR 缓存 + trust gate）范本 |
| CC `utils/settings/types.ts` | Zod schema 校验 + 优雅降级范本（对应 GA 的 pydantic） |

---

## 9. 分析边界（诚实标注）

- 本文的 YAML schema 是**草案**，字段名/结构在阶段 1 实现时可能微调（尤其 `wire_api` 枚举值、capability 名）。
- Codex/Claude Code 的结论来自子代理对源码的调研，关键路径有文件行号佐证；未逐行覆盖两者的全部认证分支（Bedrock SigV4 / Vertex / OAuth device-code），因为 GA 当前不需要这些企业级认证。
- "GA 无 litellm/SDK" 经 `pyproject.toml` + `llmcore.py` import 确认；"变量名魔法" 经 `agentmain.py:188-201` 确认。
- 未评估重构对 GA 现有 session 持久化（`session_transcript.py`）、compaction（`compact_context.py`）、mixin 故障转移的具体耦合面——阶段 1 实现前需补一次耦合面排查。
