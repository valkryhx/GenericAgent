# GA 上下文窗口与自动压缩机制调研 —— 对标 Codex / Claude Code

> 2026-07-16。目的:搞清 Codex 的 `model_context_window` / `model_auto_compact_token_limit`
> 和 Claude Code 的 `CLAUDE_CODE_AUTO_COMPACT_WINDOW` 各自怎么作用,再回头看 GA 现有机制的
> 短板,给出新 YAML 配置层(`llm_config.py`)的优化建议。
>
> 证据来源:Codex `D:\git_codes\codex\codex-rs`、Claude Code `D:\git_codes\claude-reviews-claude\claude-code-fork\src`、
> GA `llmcore.py` / `compact_context.py`。所有结论附文件:行号。

---

## 0. 一句话结论(先看这个)

三家都是「**两道线 + 一个基准窗口**」的模型:一个基准上下文窗口,一条**早触发的压缩线**(摘要式,保留信息),一条**兜底的硬上限**(超了就截断/拒绝)。差别在:

| | 基准窗口 | 压缩触发线 | 硬上限 | **计量单位** |
|---|---|---|---|---|
| **Codex** | `model_context_window`(默认 models.json,如 272k) | 窗口 × **90%**(`auto_compact_token_limit`) | 窗口 × **95%**(`effective_context_window_percent`) | **真实 token**(API usage) |
| **Claude Code** | `getContextWindowForModel`(默认 200k,`[1m]`→1M) | `有效窗口 − 13k`(`AUTOCOMPACT_BUFFER_TOKENS`) | `有效窗口 − 3k`(blocking) | **真实 token**(计数+估算) |
| **GA(现状)** | `context_win`(默认 400k) | `context_win × 3 × 0.75`(摘要式) | `context_win × 3`(硬裁剪) | **字符数 × 3 猜 token** ⚠️ |

**GA 最大的短板:用 `len(json.dumps(msg))` 字符数 × 3 来估 token,而不是用模型实际返回的 token 数**——尽管 GA 已经在 `_record_usage` 里存了 `sess.last_usage_tokens`,压缩判断却没用它。这是首要优化点。

---

## 1. Codex:`model_context_window` 与 `model_auto_compact_token_limit`

### 1.1 两个配置项的定义

TOML 层 `codex-rs/config/src/config_toml.rs`:
```rust
pub model_context_window: Option<i64>,              // :145  上下文窗口(token)
pub model_auto_compact_token_limit: Option<i64>,    // :148  触发自动压缩的 token 阈值
pub model_auto_compact_token_limit_scope: ...       // :150  阈值作用域(Total / BodyAfterPrefix)
```
都是 `Option<i64>`,**用户不填则为 `None`**,走模型元数据 `models-manager/models.json` 里的默认。

### 1.2 默认值怎么来的(关键:都是派生的,不是写死的)

- `model_context_window` 不填 → 用 `models.json` 里该模型的 `context_window`(如 gpt-5.5 = 272000),回退 `max_context_window`。用户填了会被 **clamp 到 `max_context_window`**(`model_info.rs:29-37`)——即你不能把窗口吹大过模型物理上限。
- **有效窗口** = 名义窗口 × `effective_context_window_percent`(默认 **95%**,`turn_context.rs:140-147` + `model_info.rs:97`)。留 5% 给系统提示/工具开销/输出。
- **auto-compact 阈值**默认 = `resolved_context_window × 90%`(`openai_models.rs:328-339`)。`models.json` 里所有条目的 `auto_compact_token_limit` 都是 `null`,所以恒走 90% 派生。用户填了会被 clamp:`min(用户值, 窗口×90%)`。

于是两道线:**90% 触发摘要压缩,95% 是硬上限兜底**。

### 1.3 触发判断(绝对 token 比较)

`core/src/session/turn.rs:677-678`:
```rust
let token_limit_reached =
    auto_compact_scope_tokens >= auto_compact_scope_limit    // 到 90%
    || full_context_window_limit_reached;                    // 或到 95%
```
`active_context_tokens = sess.get_total_token_usage()` —— **真实 token 累计**,来自模型 usage 上报。触发时机有三个:采样前(`turn.rs:692`)、采样后要续轮时(`turn.rs:282`)、切换到更小窗口模型时用旧模型先压一次(`turn.rs:739`,ModelDownshift)。

### 1.4 `scope` 的巧思

`AutoCompactTokenLimitScope`(`config_types.rs:31-37`,默认 `Total`):
- `Total`:全量 active context 都计入阈值。
- `BodyAfterPrefix`:只算「压缩窗口前缀之后的增量」,减去 prefill 基线——这样固定的系统前缀(能命中 prompt cache 的部分)不反复触发压缩。

### 1.5 压缩执行(`compact.rs:171-295`)

摘要 prompt 让模型总结历史 → 取最后一条 assistant 消息作摘要 → `build_compacted_history` 用「**摘要 + 最近若干条 user 消息(≤ `COMPACT_USER_MESSAGE_MAX_TOKENS`=20k)**」替换旧历史。若压缩途中还超窗,从头删最旧 item 保留前缀缓存。

---

## 2. Claude Code:`CLAUDE_CODE_AUTO_COMPACT_WINDOW` 及阈值体系

### 2.1 那个环境变量的作用

`src/services/compact/autoCompact.ts:40-46`:
```ts
let contextWindow = getContextWindowForModel(model, getSdkBetas())
const autoCompactWindow = process.env.CLAUDE_CODE_AUTO_COMPACT_WINDOW
if (autoCompactWindow) {
  const parsed = parseInt(autoCompactWindow, 10)
  if (!isNaN(parsed) && parsed > 0) contextWindow = Math.min(contextWindow, parsed)  // 只能调小
}
return contextWindow - reservedTokensForSummary
```
- **无默认值**,不设置就整段跳过。
- 作用:用 `Math.min` **把有效窗口向下钳制**(只能调小),让压缩更早触发。它不是开关,而是「以多大窗口为基准算阈值」。

### 2.2 窗口解析(优先级链,`context.ts:51-98`)

`CLAUDE_CODE_MAX_CONTEXT_TOKENS`(ant-only)> 模型名带 `[1m]`→1M > 动态能力表 `max_input_tokens` > 1M beta > … > 默认 **200k**(`MODEL_CONTEXT_WINDOW_DEFAULT`,`context.ts:9`)。
> 注意:本次运行模型 `claude-opus-4-8[1m]` 命中 `[1m]` 分支 = 1M 窗口。

### 2.3 阈值全是绝对 token(减固定 buffer)

`autoCompact.ts`:
```ts
effectiveWindow   = contextWindow − min(maxOutputTokens, 20_000)   // :33-49 留输出空间
autoCompactThreshold = effectiveWindow − 13_000                    // :72   (AUTOCOMPACT_BUFFER_TOKENS)
warningThreshold  = threshold − 20_000                             // 警告
blockingLimit     = effectiveWindow − 3_000                        // 硬上限(手动压缩 buffer 更小)
```
判断 `tokenUsage >= autoCompactThreshold` 即触发(`:238`)。**默认是绝对 token 数,不是百分比**;UI 上「X% until auto-compact」是从绝对阈值反算的展示值。只有测试用环境变量 `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` 才切百分比模式。

### 2.4 工程细节(值得抄)

- **熔断器**:连续压缩失败 3 次(`MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES`)后本 session 不再重试,防止无可挽回超限时反复空打 API。
- **开关分层**:`DISABLE_COMPACT`(关全部)/ `DISABLE_AUTO_COMPACT`(只关自动、留手动 `/compact`)/ 配置 `autoCompactEnabled`(默认 true)。
- 手动 `/compact` 用更小的 buffer(3k vs 13k),能压到更满;支持自定义指令。

---

## 3. GA 现状:两套压缩 + 字符估算

GA 有**两个独立**的压缩机制,分别在不同层、不同时机、不同阈值触发,但**共用同一个 `context_win`**(默认 `DEFAULT_CONTEXT_WIN = 400_000`,`llmcore.py:5` / `compact_context.py:14`)。

### 3.1 机制 A:硬裁剪 `trim_messages_history`(llmcore.py:107-119)

每次 `ask()` 内部调用(`llmcore.py:695`、`816`),同步、无摘要:
```python
cost = sum(len(json.dumps(m, ensure_ascii=False)) for m in history)   # 字符数
if cost > context_win * 3:                        # 超 100%(以 3 char/token 折算)
    compress_history_tags(history, keep_recent=4, force=True)
    target = context_win * 3 * 0.6                # 压到 60%
    while len(history) > 5 and cost > target:
        history.pop(0)                            # 从头丢消息(丢信息!)
        ...
```

### 3.2 机制 B:摘要式 `compact_agent_context`(compact_context.py)

由 ink_bridge 在**下一次用户请求前**调用(`ink_bridge.py:157/774-796`),用 LLM 生成摘要替换历史:
```python
def should_auto_compact_agent(agent, pending_text="", threshold=0.75):   # :31
    context_win = getattr(backend, "context_win", 400_000)
    return estimate_history_chars(history, pending_text) > context_win * 3 * threshold  # 超 75%
```
`estimate_history_chars` = `sum(len(json.dumps(m)))`(`:27-28`)—— **又是字符数**。摘要 prompt 见 `compact_context.py:206`,重建为「摘要 user 消息 + 一条 assistant 确认」(`:177-187`)。

### 3.3 把 GA 折算成「窗口百分比」看,其实是两道线

GA 用 `× 3` 把「token 窗口」换成「字符预算」(隐含假设 1 token ≈ 3 字符):

| 机制 | 触发条件(字符) | 折算成窗口占比 | 行为 |
|---|---|---|---|
| B 摘要式(先触发) | `chars > context_win×3×0.75` | **~75%** | LLM 摘要,**保留信息** |
| A 硬裁剪(兜底) | `chars > context_win×3` | **~100%** | 丢最旧消息,**丢信息** |

**这个两道线结构本身和 Codex 的 90%/95% 是同构的**——摘要线在前、硬线在后。GA 不是没有设计,而是:(1) 阈值散落成魔法数 `× 3`、`× 0.75`、`× 0.6`,没有命名、不可配;(2) 用字符估算而非真实 token;(3) `context_win` 一个字段身兼「模型窗口」和「裁剪阈值」两义,语义混淆(mykey 模板注释自己都说"仅作为历史裁剪阈值")。

---

## 4. 三家对比小结

| 维度 | Codex | Claude Code | GA 现状 |
|---|---|---|---|
| 基准窗口来源 | models.json 派生 + 用户 override(clamp) | 模型解析链,默认 200k | `context_win` 默认 400k |
| 计量单位 | **真实 token** | **真实 token** | **字符 × 3 估算** ⚠️ |
| 压缩触发线 | 窗口 90% | 有效窗口 − 13k | 字符 75%(摘要)|
| 硬上限 | 窗口 95% | 有效窗口 − 3k | 字符 100%(硬裁剪)|
| 留输出空间 | ×95% headroom | − min(输出上限,20k) | 无(未扣输出)|
| 压缩方式 | 摘要 + 最近 user 消息 | 摘要 | 摘要(B)/ 丢消息(A)|
| 熔断器 | —— | 连续失败 3 次停 | 无 |
| 阈值可配 | 3 个 TOML 键 | 环境变量若干 | 全是魔法数,不可配 |
| 作用域优化 | Total / BodyAfterPrefix | —— | 无 |

---

## 5. 对 GA 新配置层(`llm_config.py`)的优化建议

按「投入产出比」排序,不必一次全做。

### 建议 1(最高优先):压缩判断改用真实 token,字符估算作回退

GA 已经在 `_record_usage` 里存了 `sess.last_usage_tokens`(input/output/total),但 `should_auto_compact_agent` 和 `trim_messages_history` 都没用它。字符 × 3 对中文/代码/JSON 结构误差很大(中文常 1 token ≈ 1.5 字符,`json.dumps` 的括号引号又虚增字符),导致压缩时机偏差。

改法:优先读 `backend.last_usage_tokens["total_tokens"]`(模型上报的真实累计),没有时(首轮、未记录)再回退到现有字符估算。这一步不改配置格式,纯改判断逻辑,收益最大。

### 建议 2:配置项语义拆分,对齐 Codex 命名

当前 `context_window` 一词两义。建议在 `ModelCfg` / `DefaultsCfg` 引入**独立、命名清晰、有默认值**的三个字段(全部可选,不填走派生):

```yaml
models:
  claude-opus-4-8:
    context_window: 1000000          # 模型物理窗口(token)。仅此为"事实",下面两个从它派生
    # ── 以下都可不填,不填按比例自动派生 ──
    auto_compact_ratio: 0.75         # 摘要式压缩触发线 = 窗口 × 此值(对标 Codex 90%)
    effective_context_ratio: 0.95    # 硬上限 = 窗口 × 此值,留 headroom(对标 Codex 95%)
```

派生逻辑(在 `resolve()` / `to_legacy_cfg()` 里算好,传给 Session):
- 摘要触发线 `auto_compact_tokens = context_window × auto_compact_ratio`
- 硬上限 `effective_window_tokens = context_window × effective_context_ratio`
- 两者都不填时用默认 0.75 / 0.95,**行为与现状(75%/100%)基本一致但可调**。

这样用户加新模型只需填一个 `context_window`(甚至不填吃 400k 默认),高级用户想调压缩激进程度就改 ratio。消灭 `× 3`、`× 0.75`、`× 0.6` 魔法数。

> 注:GA 内部若仍以字符预算运行(建议 1 未落地前),`× 3` 可保留为一个命名常量 `CHARS_PER_TOKEN = 3`,集中一处,便于将来切真实 token 时删除。

### 建议 3:扣除输出预留(对标 Codex 95% / CC 的 −20k)

现在 GA 的硬线是满窗口 100%,没给模型输出留空间——长回复 + 满历史可能真超窗被 API 拒。建议硬上限用 `effective_context_ratio`(0.95)或显式减去 `max_tokens`,二选一。上面建议 2 的 0.95 已隐含此意。

### 建议 4:加熔断器(抄 Claude Code)

`compact_agent_context` 连续失败时(如摘要模型也宕机),ink_bridge 目前会每次请求都重试。加一个「连续失败 N 次(如 3)后本 session 停用自动压缩并提示用户」的计数器,避免反复空打 API。实现成本低,`ink_bridge.py:774` 附近加个计数即可。

### 建议 5(可选):压缩作用域优化

若将来 GA 引入稳定的系统前缀 + prompt cache,可参考 Codex 的 `BodyAfterPrefix`,让固定前缀不计入压缩阈值。当前 GA 无此需求,**暂不做**,仅记录。

---

## 6. 建议落地顺序

1. **建议 1**(真实 token 判断,回退字符)—— 改 `compact_context.py` + `llmcore.py`,不动配置格式,收益最大、风险最低。
2. **建议 2 + 3**(配置项拆分 + headroom)—— 在 `llm_config.py` 加 `auto_compact_ratio` / `effective_context_ratio`,`to_legacy_cfg()` 派生出阈值传给 Session;Session 端把魔法数换成传入值。同步更新 `llm.yaml.example` 注释。
3. **建议 4**(熔断器)—— `ink_bridge.py` 加失败计数。
4. **建议 5** —— 记录备用,不实现。

> 关键取舍:GA 的两道线设计(摘要在前、硬裁剪兜底)本身是对的,不要推倒。优化的核心是**把估算换成真实 token、把魔法数变成命名可配项、给输出留 headroom**——即"把 Codex/CC 已经验证过的参数化方式搬过来",而不是重写压缩算法。

---

## 附:关键行号速查

**Codex**
- `model_context_window` 定义:`config_toml.rs:145`、`config/mod.rs:564`
- `model_auto_compact_token_limit` 定义:`config_toml.rs:148`、`config/mod.rs:567`
- scope 枚举(默认 Total):`config_types.rs:31-37`
- override + clamp:`model_info.rs:29-40`;fallback 默认(272k/95%):`model_info.rs:94-97`
- 有效窗口 ×95%:`turn_context.rs:140-147`;auto_compact ×90%:`openai_models.rs:328-339`
- 触发比较:`turn.rs:677-678`;压缩执行:`compact.rs:171-295`

**Claude Code**
- `CLAUDE_CODE_AUTO_COMPACT_WINDOW`:`autoCompact.ts:40-46`
- 窗口默认 200k:`context.ts:9`;解析链:`context.ts:51-98`
- buffer 常量 / 阈值:`autoCompact.ts:30,62-91`;熔断器:`autoCompact.ts:70,260-265`
- 开关分层:`autoCompact.ts:147-158`

**GA**
- `DEFAULT_CONTEXT_WIN=400k`:`llmcore.py:5`、`compact_context.py:14`
- 硬裁剪:`llmcore.py:107-119`,调用点 `695`/`816`
- 摘要触发(0.75):`compact_context.py:31-35`;字符估算:`compact_context.py:27-28`
- 摘要执行:`compact_context.py:38-99,206`;bridge 调用:`ink_bridge.py:157,774-796`
- 已存但未用于压缩判断的真实 token:`sess.last_usage_tokens`(`llmcore.py:_record_usage`)
