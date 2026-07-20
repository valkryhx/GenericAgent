# GA Slice D 方案：Compact 旧图 · 临时文件 GC

日期：2026-07-20  
状态：方案（未实施；**D1 焦点提示已取消**）  
前置：

- 调研：`docs/ga_image_input_research_2026-07-20.md`
- 主方案 A–C：`docs/ga_image_input_optimization_plan_2026-07-20.md`（**A/B/C 已落地**）
- 选型：compact 旧图主跟 **Codex** + 摘要时 strip 跟 **CC**；GC 目录/上限跟 **CC**，clipboard 短生命周期跟 **Codex**；**焦点提示不做**

目标：在 **不改变 A–C 识图主路径** 的前提下，补齐 **compact 旧图卫生** 与 **临时文件 GC**（长会话/磁盘相关）。  
非目标：飞书/Discord；不重写 compact 算法本身；不引入 sharp/napi；**不做焦点剪贴板提示**（见 §2 决策）。

---

## 0. 为什么还要做 Slice D

| 项 | 没有 D 时 | 有 D 后 |
|----|-----------|---------|
| 识图 | 路径/剪贴板已可 live 识图 | 不变 |
| 长会话 compact | `_content_to_text` 把 **整段 base64 image 块 JSON 序列化进摘要 prompt**，易 **prompt-too-long / 贵 / 慢** | 摘要前 strip 为 `[image]` 标记 |
| 磁盘 | `clipboard-*.png` 落在系统 temp，无统一 GC | 统一目录 + 天数/条数清理 |

**A–C = 能不能看图；D = 会不会在 compact/磁盘上把自己撑死。**  
（「回焦点提醒剪贴板有图」对已实现 Ctrl+V→`[Image #N]` 的 GA 收益低，**明确不做**。）

---

## 1. 现状差距（基于当前代码）

### 1.1 GA

| 区域 | 现状 | 问题 |
|------|------|------|
| `frontends/ink-ui/src/clipboardImage.ts` | 写入 `GA_IMAGE_TEMP` 或 `os.tmpdir()/ga-images`，`clipboard-*.png` | 无 session 子目录；无启动/退出 GC；无条数上限 |
| `compact_context._content_to_text` | `image` 块走 `else: json.dumps(block)` | **整段 base64 进入 compact 源文本** |
| history 压缩结果 | 整段 history → LLM 摘要 → 替换为 summary pair | 摘要 prompt 本身可能因图过大失败 |
| Ink 焦点提示 | 无（且 **决定不做**） | Ctrl+V 贴图已落地，再做 hint 多余 |
| `/model` | 切换后无「是否仍支持识图」提示 | 可选 P2，非本方案主交付 |

### 1.2 Claude Code（可抄行为）

| 机制 | 位置 | 行为 |
|------|------|------|
| 焦点提示 | `hooks/useClipboardImageHint.ts` | 失焦→聚焦；`hasImageInClipboard`；30s 冷却；文案含快捷键 |
| Compact 剥图 | `services/compact/compact.ts` → `stripImagesFromMessages` | 摘要 API 调用前把 user 的 `image`/`document` 换成 `[image]`/`[document]`，**避免 compact 请求本身超长** |
| Image cache | `utils/imageStore.ts` | `~/.claude/image-cache/<sessionId>/`；内存 path map；**MAX 200** 条 evict |

### 1.3 Codex（可抄语义）

| 机制 | 位置 | 行为 |
|------|------|------|
| 路径真源 | `UserInput::LocalImage { path }` | UI/历史可只持 path；发请求再 encode |
| 图体积估算 | `context_manager/history.rs` | base64 data URL 估字节；默认 resized 估常量 |
| Compact | `compact.rs` 等 | 重写 history；对 `InputImage` 有专门分支（不按普通文本无限胀） |
| Clipboard 落盘 | `clipboard_paste.rs` | `codex-clipboard-*.png` tempfile；短生命周期语义 |

### 1.4 选型总表（本方案锁定）

| 机制 | **决策** | 实现蓝本 | 锁定理由 |
|------|----------|----------|----------|
| ~~D1 焦点提示~~ | **不做 / 取消** | — | A–C 已实现 Ctrl+V→`[Image #N]`；再提示「可以 Ctrl+V」多余、吵、实现还吃 PS/focus 复杂度 |
| D2 compact 旧图 | **做** | **CC strip 时机 + Codex path 元数据** | GA compact 会把 base64 塞进摘要源；必须 strip |
| D3 临时文件 GC | **做** | **CC 目录/上限** + Codex 命名 | 可扫、可测、防盘垃圾 |

---

## 2. D1 — 焦点提示：**取消（Won't do）**

### 2.1 决策（2026-07-20）

用户反馈：**焦点提示有点多余。**

在 GA 已具备：

- Ctrl+V → 剪贴板图 → `[Image #N]`
- 粘贴本地路径 → `[Image #N]`

的前提下，「回到窗口再提示剪贴板有图」**几乎不增加能力**，只增加：

- 通知噪音（30s 冷却仍可能烦）
- Win 上额外 PS `ContainsImage` / focus 事件复杂度
- 与「用户已经会贴图」的心智重复

Claude Code 做 hint，是因为其产品要照顾更广的新手路径；**GA 当前阶段不跟这项 UX。**

### 2.2 文档与排期

- **不实现** `useClipboardImageHint` / `clipboardLikelyHasImage` / focus footer  
- 若未来大量反馈「不知道能贴图」，再单独立项，**不默认进 Slice D**  
- CC 源码对照仅作历史参考，不再作为交付蓝本  

### 2.3 验收

无（本项不交付）。

---

## 3. D2 — Compact 旧图策略（CC strip + Codex path）

### 3.1 问题解剖（GA 今天）

`compact_agent_context` 流程：

```text
backend.history
  → _history_to_text → _content_to_text
  → 拼成大字符串喂摘要 LLM
  → 用 summary pair 替换 history
```

`_content_to_text` 对未知 block（含 `type=="image"`）：

```python
else:
    parts.append(json.dumps(block, ...))  # 整段 base64！！
```

后果：

1. Compact **请求体**可能比正常对话还大（CC 文档原话：images can cause compaction API 自身 prompt-too-long）  
2. 摘要模型浪费 token「阅读」base64  
3. 日志 `replace_log_with_compact_history` 也可能间接触发巨内容  

CC 的解法（必须学）：**生成摘要前先 stripImagesFromMessages**。  
Codex 的解法（语义学）：history 侧区分 image；真源可回落到 path。

### 3.2 目标语义

分两层，**不要混成一层**：

#### 层 A — Compact **输入**剥图（必须，跟 CC）

在 `_content_to_text` / 或 compact 专用预处理中：

| block type | 替换为 |
|------------|--------|
| `image` | `[image]` 或更优：`[image path=... sha1=... media=image/png]`（若能从旁路元数据拿到） |
| `image_url` / data URL | `[image]` |
| `input_image` | `[image]` |
| 嵌套在 `tool_result` 里的 image | 同上 |

**原则：** 摘要 LLM **永不**看到 base64。

第一期最小实现即可对齐 CC：

```text
image → 文本 "[image]"
```

第二期增强（Codex path 友好）：

```text
image → "[image | path=D:\\...\\a.png | sha1=abcd1234]"
```

path/sha1 来源优先级：

1. 若 history 旁保存了 attachments 元数据（见 3.3）  
2. 否则仅 `[image]`（与 CC 同）

#### 层 B — Compact **输出** history（跟 Codex path 真源思想）

Compact 成功后，GA 当前用 summary pair **整表替换** history（已有行为），其中 naturally **不再含**旧 image block。这与「丢掉旧 base64」一致。

额外保证：

1. **transcript** `record_compact` 已记 `backend_history_after`；确认不把 strip 前的巨 JSON 再写入  
2. 若用户 resume 后还想引用「压缩前那张图」：只能靠 **磁盘 path 仍在** + 用户再次附上；**不**在 summary 里假装模型仍「看见」像素  
3. 可选（P1）：summary 中强制要求模型写「用户曾附 N 张图」——依赖层 A 的 `[image]` 标记已进入源文本，摘要自然会提到  

### 3.3 可选：history 旁路元数据（便于层 A 增强）

不强制改 wire format。可选在 `put_task` / turn 记录时：

```python
# session_transcript.record_turn 增量
images_meta=[{"path": "...", "sha1": "...", "placeholder": "[Image #1]"}]
```

Compact strip 时若 message 带关联 meta，生成更富 `[image | path=...]`。  
**第一期可不做 meta**，只做 `[image]` strip。

### 3.4 代码落点

| 文件 | 改动 |
|------|------|
| `compact_context.py` | 新增 `_strip_images_in_content(content) -> content`；`_content_to_text` 对 image* 输出标记而非 `json.dumps` |
| `tests/test_compact_context.py` 或新测 | 构造含 500KB base64 的 history → `_history_to_text` **不含**长 base64；含 `[image]` |
| `token_meter.py` | 已处理 image 固定 token；compact 后 history 无图则自然降下来，无需特判 |

**不改：** 摘要 prompt 模板大意、auto_compact 阈值算法（除非测出仍不够再调）。

### 3.5 与 Codex/CC 对照（实现时心里要有数）

```text
CC:  messages --stripImages--> compact API --summary--> 新 messages 树
GA:  history  --_content_to_text(strip)--> 文本 --摘要 LLM--> summary pair 替换 history

Codex: history 项含 LocalImage/Image；compact 重写时按 content 类型处理，估算用 payload 规则
GA:    摘要路径更接近 CC 的 strip；真源叙事接近 Codex path-first
```

### 3.6 验收

1. 单元：含巨大 image block 的 history → compact 源文本长度 ≈ 无图文本级，且含 `[image]`  
2. 单元：纯文本 history compact 行为与现网一致（回归）  
3. 手测：多轮贴图后 `/compact` 成功，不 413；摘要里可提到曾看过图  
4. 贴图识图主路径（A–C）回归仍绿  

---

## 4. D3 — 临时文件 GC（CC 目录 + Codex 命名）

### 4.1 目录布局（按 CC 可扫、按 GA 习惯）

**目标布局：**

```text
{GA_IMAGE_ROOT}/
  ga-images/
    <session_id 或 pid>/
      clipboard-<ts>-<rand>.png
      upload-...
    orphan/          # 可选：无 session 时
```

**`GA_IMAGE_ROOT` 解析顺序：**

1. 环境变量 `GA_IMAGE_TEMP`（已有）  
2. 否则：`{repo}/temp/ga-images`（与 GA `temp/` 一致，**优于**纯系统 tmpdir，便于用户找到、一并 gitignore）  
3. 若 repo 不可写：fallback `os.tmpdir()/ga-images`

> 现状 `clipboardImage.ts` 默认 `tmpdir()/ga-images`。方案要求 **优先改到 `temp/ga-images`**（与仓库 temp 策略一致），仍可用 env 覆盖。

**Session id：**

- Ink bridge ready / agent `session_id` 可用时写入子目录  
- 未就绪时用 `pid` 或 `orphan`  

### 4.2 清理策略

| 触发 | 动作 | 对齐 |
|------|------|------|
| **进程启动**（Python agent 或 Ink main） | 扫 `ga-images/**`，删除 **mtime > RETAIN_DAYS（7）** 的文件；删除空目录 | CC 可扫 cache + 通用 janitor |
| **new_session** | 可选：删上一 session 子目录中 `clipboard-*`（保留 path 用户图不在此目录则不动） | Codex 短生命周期 clipboard |
| **进程正常退出** | best-effort 删本 session 的 `clipboard-*` | Codex tempfile |
| **条数上限** | 单 session 目录内文件数 > **MAX_FILES_PER_SESSION（200）** 时按 mtime 删最旧 | CC `MAX_STORED_IMAGE_PATHS = 200` |
| **总大小上限（P1）** | 例如 500MB / 全 ga-images | 可选 |

**绝不删除：**

- 用户原图路径（如 `截图/图2.png`）— 只管理 **GA 自己写下的** `clipboard-*` / `upload-*`  
- `temp/sessions/*.jsonl`

### 4.3 双端职责

| 端 | 职责 |
|----|------|
| **Ink `clipboardImage.ts`** | 写入正确子目录；export `getGaImageDir()`；可选 `gcLocalClipboardFiles({ maxAgeMs })` |
| **Python `image_gc.py`（新，小模块）** | `gc_ga_images(root, retain_days=7, max_per_dir=200)`；`agentmain` / `ink_bridge` 启动调用一次 |
| **gitignore** | 确认 `temp/ga-images/` 被忽略 |

### 4.4 测试

- `tests/test_image_gc.py`：临时目录造旧文件/新文件 → gc 后旧删新留  
- 条数上限：建 201 个文件 → 剩 ≤200  
- 不碰非 `clipboard-`/`upload-` 前缀（若策略限定前缀）

### 4.5 验收

1. 多次 Ctrl+V 贴图后，`temp/ga-images/` 有文件且可定位  
2. 把某文件 mtime 改成 8 天前，启动 gc → 被删  
3. 用户目录下的 `截图/图2.png` 永不被 gc  
4. 识图路径回归通过  

---

## 5. 方案中顺带可选的 D 项（原优化方案 526–532）

原 Slice D 还列了：

| 项 | 建议 | 蓝本 |
|----|------|------|
| `/model` 切换后提示是否仍支持识图 | **D4 可选**，跟在 model_switch 成功回调 | 产品逻辑，非 CC/Codex 专属 |
| `IMAGE_BLOCK_TOKENS` 细化 | **D5 可选 / 后置**；维持 1500 已够用 | Codex 有更细估算，非必须 |

本 MD **主交付仅 D2+D3**；D4/D5 单列 P2，避免 scope 膨胀。D1 已取消。

---

## 6. 实施切片（可 PR 拆分）

### PR-D3（先做，风险最低）

1. 统一 `temp/ga-images` 布局；改 `clipboardImage.ts` 默认根  
2. `image_gc.py` + 启动调用  
3. 单测 + gitignore  

**依赖：** 无  
**验证：** gc 单测；手测贴图仍识图  

### PR-D2（长会话刚需）

1. `compact_context._content_to_text` strip image*  
2. 单测：巨 base64 不进 compact 源  
3. 手动 `/compact` 多图会话  

**依赖：** 无（不依赖 D3）  
**验证：** compact 单测 + 回归  

### ~~PR-D1~~ 取消

焦点提示不排期、不实现。

**推荐顺序：D3 → D2**（仅此两项）。

---

## 7. 模块与数据流（D 完成后）

```text
[Ink]
  Ctrl+V / path ──► clipboard-*.png under temp/ga-images/<session>/ ──► images[]
                         │
                         └─► gc on start / exit / max files (D3, CC+Codex)
  （无 focus hint）

[Python]
  put_task(images) ──► image_codec ──► history image blocks
  /compact ──► strip images to [image] (D2, CC) ──► summary LLM ──► history 无 base64
  startup ──► image_gc.gc_ga_images() (D3)
```

---

## 8. 风险与缓解

| 风险 | 缓解 |
|------|------|
| strip 过狠导致摘要不知道有过图 | 使用明确标记 `[image]`，摘要 prompt 已要求保留关键事实 |
| GC 误删用户文件 | **仅**管理自有前缀与自有根目录；单测锁定 |
| 改 temp 路径后旧 tmpdir 垃圾残留 | 启动 gc **同时扫**旧 `tmpdir()/ga-images` 一轮（兼容迁移） |

---

## 9. 明确不做

1. Compact 后自动把旧 path 图重新注入 history（需用户再次附上或显式命令）  
2. 在 history 中永久保存 base64 作为真源  
3. **焦点剪贴板提示**（已取消；Ctrl+V 已够）  
4. 第三方 IM 的图片 GC  
5. 复刻 CC 全量 `~/.claude` 目录布局  
6. 为已取消的 hint 引入 focus 监听 / 剪贴板轮询  

---

## 10. 验收总清单（仅 D2+D3）

1. **D2：** 含大图 history 的 compact 源文本无 base64；`/compact` 成功  
2. **D3：** `temp/ga-images` 可定位；过期/超限清理生效；用户原图安全  
3. **回归：** `test_image_codec`、`test_native_image_input`、Ink 贴路径提交、bridge `images` 全绿  
4. **手测：** `图2.png` 路径 + Ctrl+V 仍能被 vision 模型描述  
5. **无** 焦点剪贴板提示相关行为/代码  

---

## 11. 首个可开写任务清单

### D3 GC

- [ ] 定 `resolve_ga_image_root()`（TS + Python 同规则）  
- [ ] `clipboardImage.ts` 改默认根到 `temp/ga-images`  
- [ ] `image_gc.py` + `tests/test_image_gc.py`  
- [ ] `ink_bridge` / `agentmain` 启动调用 gc  
- [ ] 兼容清理 `tmpdir()/ga-images`  

### D2 Compact strip

- [ ] `_content_to_text` 识别 `image` / `image_url` / `input_image` → `[image]`  
- [ ] 单测巨 base64 不出现在 `_history_to_text`  
- [ ] 文档注释指向 CC `stripImagesFromMessages`  

### D1 Focus hint

- [x] **取消** — 不实现（Ctrl+V→`[Image #N]` 已够）

---

## 12. 与主方案文档关系

| 文档 | 角色 |
|------|------|
| `ga_image_input_optimization_plan_2026-07-20.md` | A–C 主路径（多数已实现）；D 仅提纲 |
| **本文** | **Slice D 详细设计、选型、切片、验收** |

实施以本文为准；与 A–C 冲突时仍遵守三原则：  
**路径真源 · 有界 codec · 标准多模态出口**；D 只做体验与卫生，不削弱这三条。
