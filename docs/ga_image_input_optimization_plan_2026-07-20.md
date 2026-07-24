# GA 图片输入优化方案：对齐 Codex / Claude Code 流程

日期：2026-07-20
状态：方案（未实施）
前置调研：`docs/ga_image_input_research_2026-07-20.md`
参考源码：

- Codex：`D:\git_codes\codex`（`protocol/user_input`、`utils/image`、`tui/clipboard_paste`、`tui/bottom_pane/chat_composer`）
- Claude Code：`D:\git_codes\claude-reviews-claude\claude-code-fork\src`（`imagePaste`、`imageResizer`、`imageStore`、`imageValidation`、`usePasteHandler`、`PromptInput`）

目标：让 **GA 本体（默认 Ink UI / `ga` CLI）** 在 LLM API 支持多模态时，能像 Codex、Claude Code 一样原生识图——剪贴板贴图、路径贴图、`[Image #N]` 附件生命周期、有界编码、标准 vision API。
**不依赖**飞书、Discord、微信等第三方 IM 适配器才能看图。

核心链路（产品主路径）：

```text
用户在 GA Ink/CLI 里贴图或贴路径
  → GA 自己识别并 attach
  → put_task(images=[path...])
  → codec + gate
  → 多模态 LLM API
```

---

## 0. 目标态一句话

| 层 | 目标态（对齐谁） |
|----|------------------|
| 协议 | Codex：`LocalImage{path}` 一等公民；路径与 base64 分离 |
| **原生 UI（主战场）** | CC + Codex：剪贴板/路径 → `[Image #N]` → 删芯片即不发送；**入口是 `ga` / Ink，不是第三方 App** |
| Codec | 两边：最大边 ~2000–2048、base64≤5MB、BMP→PNG、结果缓存 |
| 发请求 | Codex：序列化时再编码；CC：校验后再发 |
| Gate | 按模型 `image_input` 能力，不绑死 `NativeOAISession` |
| 提交 | Ink bridge / CLI **显式** `images[]`；文本路径仅 fallback |

### 成功标准（用户视角）

在 **不打开飞书/Discord** 的前提下，仅使用 `ga`（Ink）：

1. Ctrl+V 贴截图 → 出现 `[Image #1]` → 回车 → vision 模型能描述图片
2. 粘贴本地图片路径 → 同上
3. 当前模型支持 `image_input` 时走多模态；不支持时明确提示，而不是静默当纯文本

### 非目标（本方案不做 / 不优先）

- **不以** Feishu / Discord / 微信 / 企微 等第三方 IM 贯通为交付目标（它们若已有 `images=` 可顺带受益，但 **不排期、不验收**）
- 不引入 litellm
- 不用 `vision_api` 工具冒充用户消息多模态
- 不重写整个 agent loop / transcript 格式（只增量扩展字段）
- 第一期不要求 Qt / Streamlit / 旧 TUI 全对齐；**默认产品面 = Ink + backend + CLI 可选**

---

## 1. 目标端到端流程（必须对齐）

```text
┌─────────────────────────────────────────────────────────────┐
│  GA 原生 UI（Ink = 主路径；对齐 Codex TUI / CC PromptInput）   │
│  1. 剪贴板贴图（Win 优先）/ 粘贴本地图片路径                  │
│  2. 落盘 temp 或保留源路径                                    │
│  3. attachments[] + 插入 [Image #N]                           │
│  4. 删除芯片 → prune attachments                              │
│  5. 提交 { text, images:[{path, placeholder, source}] }       │
│  （可选）CLI：ga / agent 启动参数 --image path                │
└───────────────────────────┬─────────────────────────────────┘
                            │ put_task / ink_bridge（GA 自有协议）
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  Agent 入队（路径真源，不在此步塞巨型 base64 进 transcript）   │
│  task = { query, images:[{path,...}], source }                │
│  transcript 记 path/sha1/placeholder，不记 full data URL      │
└───────────────────────────┬─────────────────────────────────┘
                            │ run()
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  Capability gate                                              │
│  supports_image_input(backend)?                               │
│    no  → 纯文本 + 明确提示「当前模型不支持原生看图」           │
│    yes → 继续                                                 │
└───────────────────────────┬─────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  Codec（对齐 Codex load_for_prompt + CC maybeResize）         │
│  encode_image_for_prompt(path):                               │
│    stat 拦截过大 → decode → 超边缩放 → 体积下采样 →           │
│    BMP→PNG → sha1 LRU 缓存 → EncodedImage                     │
│  build_user_content(text, encoded[]):                         │
│    Claude-style image blocks（内部统一）                      │
└───────────────────────────┬─────────────────────────────────┘
                            │ initial_user_content
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  LLM 出口（已有转换链保留并加固）                             │
│  NativeClaude → Anthropic messages image block                │
│  NativeOAI    → _msgs_claude2oai → image_url / input_image    │
│  发请求前 validate（base64 长度 ≤ 5MB）                       │
│  llm log 脱敏（只 path/sha1/bytes，不打 full base64）          │
└─────────────────────────────────────────────────────────────┘
```

对照：

| 步骤 | Codex | Claude Code | GA 目标 |
|------|-------|-------------|---------|
| 识别 | clipboard file/image + 路径 | paste 状态机 + 空粘贴剪贴板 | 同左（Win 优先 PS） |
| 真源 | `LocalImage.path` | image-cache path + sourcePath | `ImageAttachment.path` |
| UI | placeholder element | `[Image #N]` chip | `[Image #N]` |
| 编码 | 发请求时 resize≤2048 | 贴图时 resize≤2000+5MB | 入模前统一 codec |
| API | input_image | Anthropic image | 保持现有双协议出口 |

---

## 2. 数据模型

### 2.1 `ImageAttachment`（全栈统一）

```python
# 建议：image_input.py（新模块）或 agentmain 旁路小模块
from dataclasses import dataclass, field
from typing import Literal, Optional

ImageSource = Literal["clipboard", "path", "upload", "cli", "fallback_text"]

@dataclass
class ImageAttachment:
    path: str                          # 绝对路径，真源
    placeholder: str = ""              # 如 "[Image #1]"
    source: ImageSource = "path"
    media_type: Optional[str] = None   # 可选，codec 后回填
    width: Optional[int] = None
    height: Optional[int] = None
    sha1: Optional[str] = None          # 文件内容摘要，用于缓存/transcript
```

序列化（JSONL / bridge）：

```json
{
  "path": "D:\\git_codes\\GenericAgent\\temp\\ga-clipboard-abc.png",
  "placeholder": "[Image #1]",
  "source": "clipboard"
}
```

### 2.2 任务队列

扩展现有 `put_task`（兼容旧调用）：

```python
def put_task(self, query, source="user", images=None):
    # images: list[str] | list[dict] | list[ImageAttachment]
    norm = normalize_image_attachments(images)
    self.task_queue.put({
        "query": query,
        "source": source,
        "images": norm,          # list[ImageAttachment] 或 dict 列表
        "output": display_queue,
    })
```

规则：

- `images` 优先；`_extract_image_paths(query)` 仅作 **fallback**（无 Ink 芯片时的兼容），`source="fallback_text"`
- 主路径下 UI 应只插 `[Image #N]` 并把 path 放进 `images[]`，而不是让用户靠「消息里写路径碰运气」
- 文本里的路径 **不必删除**；发送时以 attachments 列表为准去重
- placeholder 在 `query` 中出现时与 path 绑定；**CLI / 无芯片调用**可只传 `images=[{path}]` 不传 placeholder

### 2.3 Transcript（增量，不破坏旧会话）

在 turn / user 事件中增加可选字段：

```json
{
  "type": "turn",
  "user": "...",
  "images": [
    {"path": "...", "placeholder": "[Image #1]", "sha1": "...", "media_type": "image/png"}
  ]
}
```

禁止：把完整 base64 data URL 写入 transcript 作为默认真源。
Resume：path 存在则重编码；不存在则 UI/日志标 `image_missing`，不静默丢失败。

---

## 3. Codec 模块（对齐两边硬限制）

### 3.1 新文件 `image_codec.py`（仓库根，与 `token_meter.py` 同级）

```python
MAX_DIMENSION = 2048          # 取 Codex；也可配置 2000 对齐 CC
MAX_BASE64_CHARS = 5_000_000  # 对齐 Claude Code API_IMAGE_MAX_BASE64_SIZE
MAX_RAW_FILE_BYTES = 40 * 1024 * 1024  # 读盘前硬拦，防 OOM
CACHE_SIZE = 32               # 对齐 Codex LRU

@dataclass
class EncodedImage:
    data_b64: str
    media_type: str           # image/png|jpeg|webp
    width: int
    height: int
    source_path: str
    sha1: str
    resized: bool
```

### 3.2 `encode_image_for_prompt(path) -> EncodedImage`

算法（综合两边）：

1. `Path.resolve()` + `stat()`：不存在 / 非文件 / `st_size > MAX_RAW_FILE_BYTES` → 抛 `ImageCodecError`
2. 读字节 → `sha1`；查 LRU `(sha1, MAX_DIMENSION, MAX_BASE64_CHARS)`
3. 魔数检测格式（勿只信扩展名）；`BM` → 转 PNG（Pillow）
4. 若 **无 Pillow**：
   - 仅允许「已是 png/jpeg/webp 且边长未知时靠文件大小启发式」
   - 超过 `MAX_BASE64_CHARS` 直接失败并提示安装 Pillow
   - 有 Pillow 时：decode → 超 `MAX_DIMENSION` 等比缩放 → JPEG quality~85 或 PNG → 若仍超 5MB base64 再降质量/缩小
5. 返回 `EncodedImage`；写入 LRU

依赖策略：

- **硬依赖不强制 Pillow**（保持轻装）
- `pyproject` 可选 extra：`[image]` → `Pillow`
- 无 Pillow 时行为降级但 **可测、可报错**，禁止静默塞 50MB base64

### 3.3 `build_image_block(encoded, style="claude")`

```python
# Claude / 内部统一
{"type": "image", "source": {"type": "base64", "media_type": ..., "data": ...}}

# 若某出口需要 OAI 原样（一般仍走 _msgs_claude2oai）
{"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{data}"}}
```

### 3.4 `build_user_content_with_images(text, attachments) -> list|None`

替换 `agentmain._build_user_content_with_images`：

1. 规范化 attachments + fallback 抽路径
2. 逐张 `encode_image_for_prompt`
3. 失败张：text 追加可读错误，不中断其它图
4. 全部失败且无有效图 → 返回 None 或仅 text（策略：有附件意图则仍返回 text+错误说明）
5. 成功：`[text_block, *image_blocks]`

### 3.5 单测 `tests/test_image_codec.py`

- 小 PNG 透传
- 超大边缩放（Pillow 可用时）
- 超 base64 限失败
- BMP 魔数转换（Pillow）
- 缓存命中同 sha1
- 缺失文件错误
- `build_user_content` 多图部分失败

---

## 4. Capability Gate（放宽并做对）

### 4.1 替换 `_native_image_input_enabled`

```python
def supports_image_input(llmclient) -> bool:
    backend = getattr(llmclient, "backend", None)
    backend = getattr(backend, "primary", backend)
    # 1) 显式 flag（兼容 mykey / llm.yaml native_image_input）
    if bool(getattr(backend, "native_image_input", False)):
        return True
    # 2) capability 表（llm_config supports("image_input")）
    if bool(getattr(backend, "supports_image_input", False)):
        return True
    # 3) Native Claude 默认支持多模态 image block（与 Anthropic Messages 对齐）
    from llmcore import NativeClaudeSession, NativeOAISession
    if isinstance(backend, NativeClaudeSession) and not isinstance(backend, NativeOAISession):
        return True  # 或仍要求 cfg 显式 image_input，见下方开关策略
    if isinstance(backend, NativeOAISession):
        return bool(getattr(backend, "native_image_input", False))
    return False
```

**推荐开关策略（避免误开不支持 vision 的模型）：**

| Session | 默认 | 说明 |
|---------|------|------|
| `NativeOAISession` | 跟 `native_image_input` / yaml `image_input` | 现状兼容 |
| `NativeClaudeSession` | 跟 yaml `image_input`，**默认 True 若未配置** 或显式 capability | 可直接发 image block |
| 其它 / 文本协议 | False | 降级提示 |

配置层（`llm_config.py` 已有 capability）：

- `supports("image_input")` → 写入 backend `native_image_input` **且** `supports_image_input`
- `/model` 切换时：若队列中有未发送 attachments 且新模型不支持，Ink 提示

### 4.2 不支持时的行为

```text
if attachments and not supports_image_input:
    display: "当前模型不支持原生图片输入；路径已保留在文本中。"
    initial_content = None   # 或 strip 后纯文本
    # 不调用 codec，避免无意义读盘
```

---

## 5. Agent / LLM 出口加固

### 5.1 `agentmain.run`

```python
attachments = normalize_image_attachments(task.get("images")) + fallback_from_text(raw_query)
# 去重 path
if supports_image_input(self.llmclient) and attachments:
    initial_content = build_user_content_with_images(raw_query, attachments)
else:
    initial_content = None
    if attachments and not supports_image_input(...):
        display_queue.put("[image] 当前模型不支持原生看图\n")
```

### 5.2 保留并加固转换链

现有：

- `_msgs_claude2oai`：`image` → `image_url` data URL ✅
- `_to_responses_input`：`image_url` → `input_image` ✅
- `NativeToolClient.chat`：保留非 text block ✅

加固：

1. **`_to_responses_input`**：同时识别 Claude 风格 `type=="image"`（防漏转）
2. **`validate_images_for_api(content)`**：发 `ask` 前检查 base64 长度（对齐 CC `imageValidation.ts`）
3. **`_write_llm_log`**：对 content 做 redaction：

```python
def _redact_for_log(obj):
    # image / image_url data: 只保留 media_type + len(data) + sha1 前缀
```

4. **history 可选策略（P1）**：history 中保留 image block 供多轮看图；compact 时旧图可替换为 `[image omitted: path/sha1]` 文本，避免无限胀（单独任务，本方案先记录接口）

### 5.3 与 `token_meter`

- 继续 `IMAGE_BLOCK_TOKENS = 1500`
- 后续可按 `width*height` 微调（P2）
- 确保 `image_url` 块与 `image` 块 **都不按 base64 字符计费**

---

## 6. Ink UI：对齐 CC PromptInput + Codex composer

### 6.1 新模块（`frontends/ink-ui/src/`）

| 文件 | 职责 | 对齐 |
|------|------|------|
| `imageAttachments.ts` | `Map/list` of attachments；alloc id；placeholder `[Image #N]`；prune by text | Codex attachments + CC pastedContents |
| `clipboardImage.ts` | Windows：PowerShell 剪贴板 → `temp/ga-clipboard-*.png`；可选 file list | Codex clipboard_paste + WSL/Win 现实 |
| `imagePathDetect.ts` | 粘贴文本是否图片路径（去引号、Win 盘符、扩展名） | CC `isImageFilePath` / Codex `handle_paste_image_path` |

Placeholder 格式（与 CC 接近，便于用户识别）：

```text
[Image #1]
[Image #2]
```

### 6.2 输入状态机（贴进 `inputController` 或旁路）

顺序（对齐 `usePasteHandler` 思想）：

1. 若 bracketed paste / 大段输入 **整体是图片路径**（可多行）→ `attachImage(path)`，不插入裸路径（或路径+芯片二选一：**推荐只插芯片**，path 进 attachments）
2. 若粘贴为空或检测为「可能是贴图」→ 调 `clipboardImage.capture()`
3. 成功 → `attachments.add` + 光标处插入 `[Image #N]`
4. 失败 → 通知「剪贴板无图片」
5. 普通文本 paste → 现有 `paste.ts` 折叠逻辑不变

快捷键（第一期）：

- **Ctrl+V**：先走增强逻辑（空/路径/剪贴板图），再退回文本粘贴
- 可选 **Ctrl+Alt+V**：强制剪贴板贴图（对齐 Codex footer / CC `chat:imagePaste`）

### 6.3 提交

```ts
// App.tsx / bridge client
submit({
  text: expandedText,  // 可含 [Image #N]
  images: attachments
    .filter(a => expandedText.includes(a.placeholder) || a.source !== 'ui')
    .map(a => ({ path: a.path, placeholder: a.placeholder, source: a.source })),
})
```

Prune 规则（对齐 CC effect）：

- 每次 input 变更：attachments 中 placeholder 不在 value 内 → 移除
- 提交前再 filter 一次

### 6.4 `ink_bridge.py`

```python
# 现：put_task(text)
# 目标：
images = payload.get("images") or []
display_queue = self.agent.put_task(text, source="user", images=images)
```

JSONL 协议增量：

```json
{"type": "submit", "text": "看看这张图 [Image #1]", "images": [{"path": "...", "placeholder": "[Image #1]", "source": "clipboard"}]}
```

旧客户端不传 `images` → 行为与现网一致（fallback 抽路径）。

### 6.5 Windows 剪贴板参考实现（第一期）

PowerShell（无窗口）保存剪贴板图到路径，由 Node `spawn` 调用；失败再尝试「剪贴板文件列表」。

落盘目录：`temp/ga-images/<session_or_run>/`
前缀：`clipboard-` / `upload-`
权限：正常用户文件；启动或退出时清理超过 N 天的 clipboard 文件。

### 6.6 Ink 测试（无截图，走 playbook）

`frontends/ink-ui/src/imageAttachments.test.ts` 等：

- alloc placeholder 递增
- 删除文本中芯片后 prune
- 路径检测：`D:\a.png`、带空格、引号
- submit payload 形状
- bridge 侧 Python 单测：`images` 传入 `put_task`

---

## 7. 产品入口范围（只做 GA 原生）

本方案的 **P0 入口只有 GA 自己**：

| 入口 | 角色 | 优先级 |
|------|------|--------|
| **`ga` / Ink UI** | 主产品面：剪贴板 + 路径 + `[Image #N]`，对齐 Codex/CC | **P0** |
| **`ink_bridge` JSONL** | Ink ↔ Python 的自有协议，传 `images[]` | **P0** |
| **Backend `put_task(images=)`** | 运行时真源；单测/脚本可直接调 | **P0** |
| **CLI 可选** `--image path`（或等价） | 无 UI 时原生带图启动/提问 | **P1** |
| 旧 Textual TUI / Qt / Streamlit | 非本方案主路径；协议兼容即可，不单独排期 | 非目标 |
| Feishu / Discord / 微信 / 企微 等 | **明确非目标**；不为其做专项贯通 | 非目标 |

说明：

- 第三方 IM 适配器即便将来复用 `images=` + codec，也属于「顺带兼容」，**不写进本方案验收**。
- 用户价值陈述应是：「打开 `ga`，像 Claude Code / Codex 一样贴图就能问」，而不是「在飞书里发图」。

可选 CLI 形态（P1，仍属原生，非第三方）：

```bash
ga --image D:\shots\ui.png -q "描述这张图"
# 或交互会话内已通过 Ink 贴图
```

---

## 8. 实施切片（按 PR 拆分）

### Slice A — Backend codec + gate（原生通道底座）

**交付：**

1. 新增 `image_codec.py` + `tests/test_image_codec.py`
2. `agentmain`：`normalize_image_attachments`、`build_user_content_with_images` 改走 codec
3. `supports_image_input` 替换窄 gate；Claude native 可收图
4. llm log redaction
5. 扩展 `tests/test_native_image_input.py`
6. 单测用 `put_task(..., images=[png])` 验证 **不经过任何 IM** 即可出多模态请求

**验收：**

- 仅通过 Python API / unittest：`images=[local.png]` + vision 配置 → 请求含 `image_url` / `input_image` / Claude `image`
- 大文件被拒或缩小，不 OOM
- 日志无完整 base64
- **不**以飞书/Discord 作为验收环境

**预计改动文件：**
`image_codec.py`（新）、`agentmain.py`、`llmcore.py`（log + 可选 validate）、`tests/test_*.py`、`token_meter.py`（若需认 `image_url`）

---

### Slice B — GA 原生协议（Ink bridge + transcript）

**交付：**

1. `ink_bridge` submit / 相关 JSONL **解析并转发 `images`**（这是 GA 自己的协议，不是第三方）
2. transcript 可选记录 `images` 元数据（path/sha1/placeholder）
3. 模型不支持时的 display 提示（Ink 状态行或回显）
4. （可选同 PR）CLI `--image` 把 path 填进 `put_task`

**验收：**

- bridge 收到带 `images` 的 submit → `agent.put_task(..., images=...)` 非空
- 无 Ink 时，单测模拟 bridge payload 即可
- **不**包含 dcapp/fsapp 改动

---

### Slice C — Ink 原生贴图 UX（对齐 CC/Codex，本方案主交付）

**交付：**

1. `imageAttachments.ts` / `clipboardImage.ts` / `imagePathDetect.ts`
2. Ctrl+V 增强 + 可选强制贴图快捷键（如 Ctrl+Alt+V）
3. `[Image #N]` 插入 / prune / submit
4. 程序化单测（无截图 playbook）

**验收（全部在 `ga` Ink 内完成）：**

1. 剪贴板截图 → 芯片 → 发送 → vision 模型能描述图
2. 粘贴 `D:\shots\a.png` → 芯片 → 同上
3. 删芯片后发送 → 无 image block
4. 纯文本 paste 不回归
5. **全程不打开任何第三方 IM**

---

### Slice D — 原生体验打磨

详见 `docs/ga_image_input_slice_d_plan_2026-07-20.md`。

1. ~~焦点恢复 + 剪贴板有图提示~~ **取消**（Ctrl+V 贴图已落地，提示多余）
2. compact 时旧图省略策略（**做**）
3. `/model` 切换后「是否仍支持识图」提示（可选 P2）
4. `IMAGE_BLOCK_TOKENS` 细化（可选 P2）
5. `temp/ga-images` 临时文件 GC（**做**）

---

## 9. 模块归属与依赖方向

```text
  用户 @ GA 原生
       │
       ├─ Ink UI（剪贴板 / 路径 / [Image #N]）
       │       │
       │       ▼
       │  ink_bridge JSONL {text, images:[{path}]}   ← GA 自有，非第三方 App
       │       │
       └─ CLI --image path（可选）
               │
               ▼
        agentmain.put_task(query, images=...)
               │
               ▼
        image_codec.encode_image_for_prompt
               │
               ▼
        agent_loop initial_user_content
               │
               ▼
        NativeToolClient / Session
               │
       +-------+--------+
       v                v
 NativeClaude      NativeOAI
 image block       image_url / input_image
```

- `image_codec` **不得**依赖 frontends
- Ink **不得**自己 base64 塞进 prompt（只传 path，对齐 Codex `LocalImage`）
- 唯一编码入口：`image_codec`
- 第三方 IM **不在依赖图上**

---

## 10. 风险与缓解

| 风险 | 缓解 |
|------|------|
| Windows 剪贴板 API 不稳 | PS 脚本 + 失败可读错误；保留路径粘贴主路径 |
| 无 Pillow 环境 | 小图透传；大图明确报错；optional extra |
| history 胀 | transcript 不存 base64；后续 compact 省略图 |
| 误开无 vision 模型 | capability 默认保守；OAI 仍要 flag |
| 破坏纯文本 paste | paste 测试全保留；图片逻辑分支在前、失败回退文本 |
| 路径安全 | 仅本地文件；可选限制在 cwd/temp/用户主目录外需确认（P1） |

---

## 11. 明确不做（防 scope creep）

1. **不为飞书 / Discord / 微信等第三方 IM 做专项识图贯通**（非本方案目的）
2. 不在 Slice A/B/C 重做 vision_sop / 桌面截图 agent 工具（那是 agent 主动看屏，不是用户贴图）
3. 不引入 sharp/napi 到 Ink（第一期：Win 剪贴板 + Python codec）
4. 不在终端渲染真实缩略图（`[Image #N]` 文字芯片即可）
5. 不把「文本正则抠路径」继续当主产品路径（仅 fallback）

---

## 12. 验收总清单（全部 Slice 完成后）

全部在 **GA 原生环境**验收（`ga` Ink + unittest），不依赖第三方 App：

1. **正确通道**：vision 模型 + capability 打开时，请求体出现标准多模态块（非纯路径字符串）
2. **Ink 剪贴板贴图**：截图 Ctrl+V → `[Image #N]` → 发送 → 模型能描述
3. **Ink 路径贴图**：粘贴本地 `.png/.jpg...` 路径 → 同上
4. **生命周期**：删除芯片后发送 → 无 image block
5. **有界编码**：超限缩小或拒绝，日志无巨型 base64
6. **Claude native 与 OAI native** 在 capability 打开时均可
7. 测试：`test_image_codec`、`test_native_image_input`、Ink image 单测、ink_bridge `images` 单测全绿

---

## 13. 建议实施顺序

| 顺序 | Slice | 价值 | 依赖 |
|------|-------|------|------|
| 1 | **A** Backend codec + gate | 修对 LLM 的原生多模态通道 | 无 |
| 2 | **B** ink_bridge / transcript（+ 可选 CLI `--image`） | GA 自有协议能传图 | A |
| 3 | **C** Ink 贴图 UX | **主交付**：像 Codex/CC 一样用 | A+B |
| 4 | **D** 打磨 | 提示、GC、compact | C |

推荐：**A → B → C**。C 是用户可感知的「原生识图」完成线；A/B 是底座。
**不要**在 C 之前插入 Feishu/Discord 工作。

---

## 14. 与调研文档的关系

| 文档 | 角色 |
|------|------|
| `docs/ga_image_input_research_2026-07-20.md` | 现状与两边源码对照（Why） |
| **本文** | 目标架构、数据模型、切片、验收（How） |

实施时以本文为准；若实现细节与调研冲突，以 **路径真源 + 有界 codec + 标准多模态出口** 三原则仲裁。

---

## 15. 首个可开写的任务清单

### Slice A（底座）

- [ ] 新增 `image_codec.py`：`EncodedImage`、`ImageCodecError`、`encode_image_for_prompt`、LRU
- [ ] 新增 `tests/test_image_codec.py`
- [ ] `agentmain`：attachments 规范化；codec；gate → `supports_image_input`
- [ ] `llmcore`：Prompt 日志 redaction；可选 `validate_images_for_api`
- [ ] `token_meter`：`image` / `image_url` 均不计 base64 字符
- [ ] `python -m unittest tests.test_image_codec tests.test_native_image_input tests.test_token_meter`

### Slice B → C（原生产品面，紧接 A）

- [ ] `ink_bridge`：`images` 字段 → `put_task`
- [ ] Ink：`clipboardImage` + `imageAttachments` + `[Image #N]` + Ctrl+V
- [ ] 在本机 `ga` 上完成「贴图即识图」手测（vision 模型）

**完成线 = Slice C 在 `ga` Ink 内可用**，而不是「某个第三方 App 能发图」。
