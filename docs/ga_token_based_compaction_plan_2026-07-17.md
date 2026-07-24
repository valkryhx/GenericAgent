# GA 压缩机制 token 化重构 —— 分步实现方案

> 2026-07-17。目标:把 GA 的两套压缩机制从「字符数 ×3 魔法估算」彻底改到
> **真实 token 方向**,参照 Codex / Claude Code 源码,不沿用 GA 旧的字符估算与
> pop 截断口径。
>
> 前置调研:`docs/ga_context_window_autocompact_research_2026-07-16.md`(三家机制对比)。
> 本文只讲「怎么改」。

---

## 0. 决定性事实(来自 Codex / CC 源码,是整个设计的地基)

**两者都没有本地 tokenizer(全仓无 tiktoken)。** token 计数是同一模式:
**真实 API token 为基准 + 未发送增量的本地字节/字符估算。**

| | 基准(真实) | 增量(估算) | 压缩后 |
|---|---|---|---|
| **Codex** | `last_token_usage.total_tokens`(API usage) | 最后一条模型生成消息之后的新条目,`bytes/4`(`APPROX_BYTES_PER_TOKEN=4`) | `recompute_token_usage` 纯本地估算刷新,下次 API 覆盖 |
| **Claude Code** | 上次 API 响应 usage(input+cache+output) | 其后新增消息,`chars/4`(JSON `chars/2`、图片 2000) | 摘要为新消息、无 usage,估算到下次 API |

**推论(为什么这样做):** 权威 token 只在 API 响应回来时才有;而压缩/裁剪发生在**两次调用之间**,此刻精确 token 物理上拿不到。正解不是找 tokenizer,而是:真实 token 占整段历史(准、且是绝大多数),只对最近一小段还没发出的增量做廉价估算,总误差可控。

**GA 的错误不是"用了估算",而是"只有估算、从不用真实 token"** —— `_record_usage` 已把真实 token 存进 `sess.last_usage_tokens`(三种 wire 都归一化好了,`normalize_usage_tokens` llmcore.py:320),但两套压缩判断都在用 `len(json.dumps)×3`,把手里的真值扔了。

---

## 1. 两个需求的本质

### 需求 1:`trim_messages_history`(硬裁剪,llmcore.py:107-119)token 化

一个函数干两件事,对真实 token 的可用性不同:

| 子步骤 | 旧做法 | token 化后 |
|---|---|---|
| **触发判断** | `chars > context_win×3` | `估算当前token > hard_limit_tokens`,当前 token = **真实基准 + 增量估算** |
| **收敛裁剪** | `while chars>target: pop; 重算chars` | 已 mutate,真实基准失效 → 用**从真实数据校准出的** chars/token 比率把每条消息折成 token,pop 到低于目标 |

关键约束:裁剪过程中每 pop 一条**没有新 API 调用**,真实 token 不会更新。所以收敛只能靠本地估算——但把旧的固定 `×3` 换成**本会话实测校准**的比率 `chars_per_token = 本次历史总字符 / 上次真实total_token`(中文会话可能实测 1.6,代码 3.5,自适应)。这正是 Codex `recompute_token_usage`「mutate 后用本地估算」的等价做法。

### 需求 2:拆分 `context_win` 一值两义

现状 `context_win` 同时是「模型窗口」和「裁剪阈值」,且三个魔法数散落(摘要 `×0.75`、硬裁剪 `×1.0`、目标 `×0.6`)。GA 其实已是 Codex 式两道线(软线摘要在前、硬线裁剪兜底),只是没命名、不可配、用字符。

拆成语义清晰、有默认、可派生的字段(对齐 Codex 90%/95%,用户已确认):

```yaml
models:
  claude-opus-4-8:
    context_window: 1000000      # 模型物理窗口(token)。唯一"事实",下面从它派生
    # ── 以下可不填,不填按比率派生 ──
    auto_compact_ratio: 0.90     # 软线:摘要式压缩触发 = 窗口 × 此值(信息保留)
    hard_limit_ratio:  0.95      # 硬线:裁剪兜底触发 = 窗口 × 此值(丢信息)
```

---

## 2. 目标架构

新增 **`token_meter.py`**(零重依赖,可独立单测),对标 CC `utils/tokens.ts` / Codex `history.rs` 的 token 方法,作为**唯一** token 计量入口。llmcore 与 compact_context 都用它,消灭重复的字符估算。

```
token_meter.py  ← 唯一 token 计量
  ├─ 常量 CHARS_PER_TOKEN_FALLBACK = 4        # 冷启动无真值时用,对齐 CC/Codex 的 4
  ├─ real_total_tokens(last_usage)            # 读 last_usage_tokens 的真实基准
  ├─ estimate_msg_tokens(msg, cpt)            # 单条消息 → token(按 chars/cpt)
  ├─ calibrated_cpt(history, last_usage)      # 从真实数据校准 chars/token,无真值回退 4
  └─ estimate_context_tokens(history, last_usage)
         = 真实基准(last_usage.total) + 最后一条 assistant 之后新增消息的估算
         (冷启动无 usage → 全量本地估算,对齐 Codex/CC 回退)

llmcore.py
  ├─ BaseSession 读 cfg: context_win / auto_compact_tokens / hard_limit_tokens
  └─ trim_messages_history(history, hard_limit_tokens, last_usage)  ← 签名改造
        触发: estimate_context_tokens > hard_limit_tokens
        收敛: 用 calibrated_cpt 把每条折 token,pop 到 < 目标

compact_context.py
  └─ should_auto_compact_agent  ← 用 token_meter + 软线 auto_compact_tokens

llm_config.py
  ├─ ModelCfg: + auto_compact_ratio(0.90) / hard_limit_ratio(0.95)
  └─ to_legacy_cfg: 派生 auto_compact_tokens / hard_limit_tokens 写入 cfg
```

**保留的正确设计**:两道线结构(软摘要在前、硬裁剪兜底)不动;非 ink 前端(CLI/TUI)只有硬裁剪这一条安全网,所以硬裁剪不能删,只能 token 化。

---

## 3. 分步实现(每步可独立测试、独立提交)

### 步骤 1:新建 `token_meter.py` + 单测(纯函数,不碰现有代码)

- 实现上述 5 个函数。`estimate_context_tokens` 的「最后一条 assistant 之后」边界:从后往前找最后一个 `role=="assistant"` 的消息,其后的消息(新 user、tool_result)算增量。找不到(冷启动)→ 全量估算。
- `calibrated_cpt`:`total_chars / real_total` ,real_total 缺失或 ≤0 → 返回 `CHARS_PER_TOKEN_FALLBACK`。
- 图片块估算对齐 CC:固定计一个常量(如 1500),不把 base64 字符数算进去(否则爆炸)。
- `tests/test_token_meter.py`:真实基准优先、冷启动回退、增量边界(assistant 后)、校准比率、图片块不爆炸、CJK 场景比率合理。

### 步骤 2:`trim_messages_history` token 化(llmcore.py)

- 签名 `trim_messages_history(history, hard_limit_tokens, last_usage=None)`。
- 触发:`estimate_context_tokens(history, last_usage) > hard_limit_tokens`。
- 收敛:`cpt = calibrated_cpt(history, last_usage)`;目标 `target_tokens = hard_limit_tokens × RECOVER_RATIO`(如 0.6,保留旧的"压到 6 成"手感但以 token 计);pop 循环用 `sum(estimate_msg_tokens(m, cpt)) > target_tokens` 收敛。
- 两个调用点(llmcore.py:695、816)改传 `self.hard_limit_tokens, self.last_usage_tokens`。
- `compress_history_tags` 保留(它是省 token 的正交手段)。
- 现有 llmcore 相关测试跑通。

### 步骤 3:`BaseSession` 读新 cfg 字段(llmcore.py:624-653)

- 读 `auto_compact_tokens` / `hard_limit_tokens`;缺省时从 `context_win` 按 0.90 / 0.95 兜底派生(保证不经新配置层、直接喂 cfg 的老调用也有合理值)。
- `context_win` 语义收窄为「模型窗口」,不再直接当阈值。

### 步骤 4:`should_auto_compact_agent` token 化(compact_context.py)

- 改用 `token_meter.estimate_context_tokens`;阈值读 `backend.auto_compact_tokens`(软线),缺省从 `context_win×0.90` 派生。
- 删掉本文件里的 `_CHARS_PER_TOKEN` / `estimate_history_chars` 旧字符路径(迁移到 token_meter);保留冷启动回退(在 token_meter 内)。
- 更新 `tests/test_compact_context.py`:阈值口径从字符改 token,复用 FakeBackend 加 `last_usage_tokens` / `auto_compact_tokens`。

### 步骤 5:配置层拆分(llm_config.py + llm.yaml.example)

- `ModelCfg` + `DefaultsCfg`:加 `auto_compact_ratio: float = 0.90`、`hard_limit_ratio: float = 0.95`(0<ratio≤1 校验;软 < 硬 校验)。
- `to_legacy_cfg()`:`context_window` 存在时派生 `auto_compact_tokens = round(window×auto_compact_ratio)`、`hard_limit_tokens = round(window×hard_limit_ratio)` 写入 cfg;window 未填则不写(由步骤 3 的 Session 兜底)。
- `llm.yaml.example`:补两个 ratio 字段注释 + 「context_window 是模型窗口、两道线从它派生」的说明(顺带回答"context_window 怎么定位")。
- `tests/test_llm_config.py`:派生正确、ratio 校验、软<硬 校验。

### 步骤 6(可选,低成本):自动压缩熔断器(ink_bridge.py:774)

- 连续摘要失败 N 次(如 3)后本 session 停自动压缩并提示,抄 CC 的 `MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES`。避免摘要模型宕机时反复空打。
- 与前 5 步解耦,可最后单独做。

---

## 4. 落地顺序与风险

1. 步骤 1(token_meter + 测)—— 纯新增,零风险,是后面所有步的地基。
2. 步骤 2+3(硬裁剪 token 化)—— 改 llmcore,非 ink 前端的安全网,优先保证正确。
3. 步骤 4(软线 token 化)—— 改 compact_context。
4. 步骤 5(配置拆分)—— 改 llm_config,把魔法数变可配。
5. 步骤 6(熔断器)—— 可选收尾。

**关键取舍(回应"完全 token 化"):** 真实 token 无法在两次调用间获得是物理约束,Codex/CC 都接受并用「真实基准 + 廉价增量估算」应对——这不是妥协,是业界正解。本方案据此:**阈值单位全部是 token、触发判断用真实 token、只有 mutate 后的收敛用从真实数据校准的比率(非固定魔法数)**。这才是真正的 token 方向,而非 GA 旧的「纯字符 ×3」。

**向后兼容:** 直接喂 legacy cfg(不经新配置层)的老路径,靠步骤 3 的 Session 端派生兜底,行为等价且不炸。
