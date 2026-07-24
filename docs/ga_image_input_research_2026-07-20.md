# GA 用户图片输入：Codex / Claude Code 对照调研与优化建议

日期：2026-07-20
范围：用户消息里的图片识别、粘贴、落盘、编码、进模型请求、上下文计量
参考源码：

- Codex：`D:\git_codes\codex`（重点 `codex-rs/tui`、`codex-rs/utils/image`、`codex-rs/protocol`、`codex-rs/core`）
- Claude Code：`D:\git_codes\claude-reviews-claude\claude-code-fork\src`（重点 `utils/imagePaste.ts`、`utils/imageResizer.ts`、`utils/imageStore.ts`、`utils/imageValidation.ts`、`hooks/usePasteHandler.ts`、`components/PromptInput`）
- GA：`agentmain.py`、`llmcore.py`、`token_meter.py`、`frontends/ink-ui`、`frontends/ink_bridge.py`、`frontends/fsapp.py` 等

本文只做调研与方案，不直接改代码。

---

## 0. 结论先行

| 维度 | Codex | Claude Code | GA 现状 | 建议优先级 |
|------|-------|-------------|---------|------------|
| UI 剪贴板贴图 | 有：`arboard` + 临时 PNG，WSL 走 PowerShell | 有：跨平台剪贴板命令 / native module，Ctrl+V / 专用快捷键 | **几乎无**：Ink 只折叠文本 paste，不识别图片 | **P0** |
| UI 拖拽/路径贴图 | 有：粘贴路径 → `attach_image`，composer 占位符 | 有：路径解析 + 空粘贴回退剪贴板 + `[Image #N]` 芯片 | **仅后端文本扫路径**；Ink/TUI/CLI 不独立传 `images` | **P0** |
| 附件模型 | 结构化 `UserInput::LocalImage { path }` / `Image { image_url }` | `PastedContent` + 磁盘 `image-cache` + placeholder 引用 | 弱：`images=[]` 参数 + 文本里抠路径 | **P0** |
| 编码前处理 | 解码后按 `MAX_DIMENSION=2048` 缩放；PNG/JPEG/WebP 可保源；SHA1 LRU 缓存 | `2000×2000` + base64 ≤5MB 下采样；BMP 转 PNG；API 边界再校验 | **原样 base64 整文件**，无缩放、无体积上限 | **P0** |
| 请求构造时机 | 序列化时才 `LocalImage`→data URL | 粘贴时已 resize，提交时拼 content block | 入队时读盘编码；flag 关则整条多模态失效 | **P1** |
| 能力开关 | 模型/能力侧约束更完整 | 始终按 Anthropic 多模态块 | **仅** `NativeOAISession` + `native_image_input`；Claude native 不走此路 | **P1** |
| 前端贯通 | TUI 原生贯通 | PromptInput 原生贯通 | **仅 Feishu `fsapp` 显式传 `images`**；Ink/TUI/Discord 等多数只塞路径文本到 query | **P0** |
| Token 计量 | 按 base64/payload 估算，区分 detail | 图片块固定估算 + 校验 | 固定 `IMAGE_BLOCK_TOKENS=1500`（方向正确） | **P2** |
| 历史/回滚 | 保留 `local_image_paths`，composer 可回填 | session 级 image-cache 可恢复 | transcript 是否稳妥存图块未形成一等公民 | **P2** |

**一句话：**
GA 已有「路径 → Claude-style image block → OAI/Responses 转换」的后端半成品，但缺 Codex/Claude Code 那套 **UI 识别 + 附件生命周期 + 编码前处理 + 多前端贯通**。优先补 Ink/TUI 贴图与路径附件、统一 resize/校验管线，再放宽能力开关与协议形状。

---

## 1. Codex：用户图片怎么走

### 1.1 协议层：图片是一等输入

`codex-rs/protocol/src/user_input.rs`：

```text
UserInput::Text { text, text_elements }
UserInput::Image { image_url, detail? }          # 已是 data URL / 远程 URL
UserInput::LocalImage { path, detail? }          # 本地路径，序列化时再编码
```

要点：

- **路径与已编码 URL 分离**：composer / 历史里长期拿路径，真正发请求再转 data URL，避免草稿阶段塞巨量 base64。
- `text_elements` 用字节区间标记占位符（含图片 pill），UI 与 resume 不必改字面文本。
- `detail`（`ImageDetail`）可影响 vision 计费/精度（core history 估算会用到）。

### 1.2 TUI：识别与附件

关键文件：

- `codex-rs/tui/src/clipboard_paste.rs`
- `codex-rs/tui/src/bottom_pane/chat_composer.rs`
- `codex-rs/tui/src/chatwidget.rs`

流程：

1. **剪贴板贴图** `paste_image_to_temp_png()`
   - 优先 `arboard` 的 **file_list**（Finder/资源管理器复制文件）
   - 否则 `get_image()` 取 RGBA → 编 PNG
   - 写入 `codex-clipboard-*.png` 临时文件并 `keep`
   - Linux/WSL：`arboard` 失败时用 **PowerShell 把 Windows 剪贴板图落盘**，再映射成 WSL 路径

2. **粘贴文本像图片路径**
   - `handle_paste` / `handle_paste_image_path` 检测到图片路径 → `attach_image(path)`

3. **Composer 附件**
   - 插入 **placeholder element**（如 `[Image #N]` 一类）
   - `LocalImageAttachment` 跟踪 path + placeholder
   - 提交前：展开大段文本 paste，**按仍存在的 placeholder 裁剪附件列表**（删掉 pill 就不发图）

4. **回退/历史**
   - `local_image_paths` 随 cell 保存，backtrack/prefill composer 可恢复附件，而不只是纯文本。

### 1.3 编码与缓存

`codex-rs/utils/image/src/lib.rs`：

- `MAX_DIMENSION = 2048`
- `PromptImageMode::ResizeToFit | Original`
- 流程：`guess_format` → decode → 超边则 `resize` → 优先保 PNG/JPEG/WebP 源字节，否则重编码
- JPEG 质量约 85；WebP lossless；GIF 不保源字节
- **SHA1(file_bytes)+mode 的 LRU 缓存（32）**，同图重复提交不重复处理
- 输出 `EncodedImage { bytes, mime, width, height }` → `into_data_url()`

工具侧 `view_image` 也走同一套 `load_for_prompt_bytes`，用户贴图与工具读图共用管线。

### 1.4 请求与上下文

- 序列化阶段 `LocalImage` → data URL → Responses/API 的 `input_image`
- history 对 base64 image 有 **payload 长度估算**，非 base64 URL 另算
- analytics 统计 `num_input_images`

**Codex 可借鉴的工程形状：**

1. 路径优先、延迟 base64
2. 剪贴板：file list → image buffer →（WSL）OS 桥
3. UI placeholder 与附件表双向绑定
4. 统一 resize + 格式透传 + 结果缓存

---

## 2. Claude Code：用户图片怎么走

### 2.1 粘贴识别（比 Codex 更“终端现实”）

关键文件：

- `src/hooks/usePasteHandler.ts`
- `src/utils/imagePaste.ts`
- `src/hooks/useClipboardImageHint.ts`
- `src/components/PromptInput/PromptInput.tsx`

识别路径：

1. **Bracketed paste / 粘贴缓冲**
   - 粘贴结束后解析文本
   - 按「空格+绝对路径」或换行拆多路径（兼容 Finder 多选拖拽、终端路径转义）
   - `isImageFilePath`：去引号、剥 shell 反斜杠转义、扩展名 regex `png|jpe?g|gif|webp`

2. **路径读图** `tryReadImageFromPath`
   - 绝对路径直接读
   - 相对/仅文件名：尝试与 **剪贴板路径 basename** 对齐（截图临时文件常见）
   - BMP 魔数 `BM` → sharp 转 PNG（WSL/Windows 剪贴板常见）
   - 再 `maybeResizeAndDownsampleImageBuffer`

3. **空粘贴 + macOS**
   - 粘贴文本为空时查剪贴板图（Cmd+V 贴图常如此）
   - 临时截图路径已失效时回退剪贴板

4. **专用快捷键** `chat:imagePaste`
   - 明确从剪贴板取图；SSH 时提示 scp 等

5. **焦点提示** `useClipboardImageHint`
   - 终端重新获得焦点且剪贴板有图时，提示可用快捷键贴图（30s 冷却）

### 2.2 UI 附件生命周期

`PromptInput`：

- `onImagePaste` → 分配 `pasteId`，写入 `pastedContents`
- 插入 **`[Image #N]` 芯片**
- `cacheImagePath` 立刻给可点路径；`storeImage` 后台写到
  `~/.claude/image-cache/<sessionId>/<id>.<ext>`（权限 0o600，上限约 200 条 LRU）
- **删除芯片即 prune 附件**（effect 扫描 input 中仍引用的 id）
- 可无文字、仅图提交

### 2.3 Resize / 校验硬限制

`src/constants/apiLimits.ts` + `imageResizer.ts` + `imageValidation.ts`：

| 常量 | 值 | 含义 |
|------|-----|------|
| `IMAGE_MAX_WIDTH/HEIGHT` | 2000 | 边长上限 |
| `API_IMAGE_MAX_BASE64_SIZE` | 5 MB | **base64 字符串**长度硬限（不是解码后字节） |
| `IMAGE_TARGET_RAW_SIZE` | 3.75 MB | 对应 base64 5MB 的 raw 目标 |
| `API_MAX_MEDIA_PER_REQUEST` | 100 | 单请求媒体数 |

`maybeResizeAndDownsampleImageBuffer`：超边缩放 + 必要时降采样/改格式，保证过 API。
`validateImagesForAPI` 在发请求前再扫一遍，超限抛 `ImageSizeError` 给用户可读错误。

### 2.4 进模型

- Anthropic 原生：`{ type: "image", source: { type: "base64", media_type, data } }`
- 粘贴阶段已 resize，请求侧主要是组装 + 校验
- 工具读图（`FileReadTool/imageProcessor`）共用 sharp / image-processor-napi

**Claude Code 可借鉴的工程形状：**

1. 终端粘贴的完整状态机（路径 / 空粘贴 / 截图临时文件 / BMP）
2. Placeholder 芯片 + 磁盘 image-cache（会话级）
3. **API 级硬限制 + 边界校验**
4. UX：剪贴板有图时提示快捷键

---

## 3. GA 现状

### 3.1 后端：半成品但可用

`agentmain.py`：

```text
_extract_image_paths(text)     # 引号路径 / 绝对路径 / 裸文件名
_image_block(path)             # 整文件 base64 → Claude-style image block
_build_user_content_with_images(text, images)
_native_image_input_enabled()  # 仅 NativeOAISession + native_image_input
```

`run()`：

```python
initial_content = _build_user_content_with_images(raw_query, images) \
    if _native_image_input_enabled(self.llmclient) else None
# agent_runner_loop(..., initial_user_content=initial_content)
```

`llmcore.py`：

- Claude-style `image` → OAI `image_url` data URL（`_msgs_claude2oai`）
- OAI → Responses `input_image`（`_to_responses_input`）
- `NativeToolClient` 会保留非 text block

`token_meter.py`：图片块固定 `IMAGE_BLOCK_TOKENS = 1500`，避免 base64 按字符爆炸——方向正确。

测试：`tests/test_native_image_input.py` 覆盖路径抽取（含空格）、转换链、flag 开关。

### 3.2 前端：严重不贯通

| 前端 | 是否传 `images=` | 行为 |
|------|------------------|------|
| `fsapp`（飞书） | **是** | 下载媒体 → `put_task(..., images=image_paths)` |
| `ink_bridge` / Ink UI | 否 | 只 `put_task(text)`；paste 仅文本折叠 |
| TUI / Streamlit / Qt / Telegram / Discord / WeChat 等 | 否（多数） | 路径写进文本，靠 `_extract_image_paths` 碰运气；Discord 附件下载后也是拼进文本 |

Ink 的 `paste.ts` / `inputController.ts` 只有 **`[Copied text #N +k lines]`**，**没有图片 paste / 剪贴板图**。

### 3.3 能力开关过窄

```python
return isinstance(backend, NativeOAISession) and bool(getattr(backend, 'native_image_input', False))
```

- 非 OAI native、Claude native、代理会话：即使用户给了图路径，**也不会建多模态 content**
- 配置上 `llm_config` 有 `image_input` capability → `native_image_input`，但运行时 gate 仍绑死 `NativeOAISession`

### 3.4 编码与安全缺口

- 无边长限制、无 base64 大小限制
- 无格式魔数校验（扩展名即信任）
- 无 BMP→PNG
- 无处理结果缓存
- 读失败只在 text 后追加中文错误串，无结构化错误
- 大图会：撑请求体、拖慢 JSON log、污染 history、误触发压缩逻辑

### 3.5 旁路：Vision SOP

`memory/vision_sop.md` + `vision_api.template.py` 是 **agent 主动截图/问 vision** 的工具链，不是用户消息多模态入口。两者应并存：

- 用户贴图 → 原生多模态 user content
- agent 看桌面/窗口 → vision 工具（有窗口枚举、禁止全屏等 SOP）

不要用 vision 工具替代「用户消息带图」。

---

## 4. 端到端对照（用户贴一张图）

```text
                Codex                              Claude Code                         GA (现状)
识别      剪贴板 file/image / 路径粘贴      路径粘贴 / 空粘贴剪贴板 / 快捷键      文本里正则抠路径
落盘      临时 PNG 或保留源路径           image-cache/<session>/id.ext         无统一落盘；飞书另存 media
UI        [Image #N] element + 附件表     [Image #N] chip + pastedContents    无芯片；文本路径裸奔
提交      LocalImage{path}                PastedContent → image blocks        images[] 或路径字符串
编码      load_for_prompt (≤2048, cache)  resize ≤2000 & ≤5MB b64            整文件 base64
API       input_image / provider map      Anthropic image source              Claude block → OAI/Responses
Gate      模型能力                        默认多模态                          仅 NativeOAI + flag
计量      payload 估算                    固定块 + 校验                       固定 1500 token/图
```

---

## 5. GA 优化建议（按优先级）

### P0 — 用户可感知、立刻拉开差距

#### 5.1 统一 `ImageAttachment` 与提交协议

建议在 agent / bridge 层固定：

```text
UserTurn = {
  text: str,
  images: list[{
    path: str,              # 本地绝对路径（首选）
    media_type?: str,
    source?: "clipboard"|"path"|"upload"|"chat",
    placeholder?: str,      # 如 "[Image #1]"
  }]
}
```

规则：

- **路径优先**，history / transcript 默认存 path + placeholder，不默认存巨型 base64
- 发请求前再 `encode_image_for_prompt(path) → image block`
- 与 Codex `LocalImage` / Claude `image-cache` 同构

`put_task(query, images=...)` 已有雏形，补齐：

- Ink bridge / TUI / Discord / Telegram 全部显式传 `images`
- 文本路径抽取降级为兼容层，不再是主路径

#### 5.2 Ink UI：剪贴板贴图 + 路径贴图

对齐 Claude Code / Codex：

1. **快捷键**（建议 `Ctrl+V` 增强 + 可选 `Ctrl+Alt+V` 专贴图，Windows/WSL 注意冲突）
2. 剪贴板读取策略（Windows 优先）：
   - PowerShell / .NET 剪贴板图 → `temp/ga-clipboard-*.png`
   - 或 Node 侧 `clipboardy` / 原生模块（若引入依赖需评估）
   - 有 file list 时直接 attach 路径
3. 粘贴文本若是图片路径 → attach，不插进纯文本
4. Composer 显示 **`[Image #N]`** 芯片；删除芯片即从 attachments 移除
5. 提交 JSONL：`{ type: "user_turn", text, images: [{path, placeholder}] }`

实现落点建议：

- `frontends/ink-ui/src/` 新增 `clipboardImage.ts`、`imageAttachments.ts`
- `paste.ts` 扩展或并列，勿把图塞进 text paste store
- `ink_bridge.py` 解析 images 并 `put_task(..., images=paths)`

#### 5.3 编码管线 `image_codec.py`（或 `ga_image.py`）

对齐两边的硬约束，建议默认：

| 参数 | 建议值 | 来源 |
|------|--------|------|
| 最大边 | 2048（或 2000） | Codex 2048 / CC 2000 |
| base64 上限 | 5_000_000 字符 | Claude Code API 限 |
| 格式 | png/jpeg/webp 透传；gif 可选；bmp→png | 两边共识 |
| 缓存 | 按文件 sha1 + 参数 LRU（内存 32） | Codex |
| 失败 | 结构化错误，不静默丢图 | CC ImageSizeError |

API 草图：

```python
@dataclass
class EncodedImage:
    data_b64: str
    media_type: str
    width: int
    height: int
    source_path: str

def encode_image_for_prompt(path: str, *, max_dim=2048, max_b64=5_000_000) -> EncodedImage: ...
def build_image_content_block(encoded: EncodedImage, *, style="claude"|"oai") -> dict: ...
```

依赖：优先 **stdlib + 可选 Pillow**；无 Pillow 时至少做体积/扩展名拒绝并提示安装。

#### 5.4 关掉「只有 OAI native 才能看图」

- Gate 改为：`backend.supports_image_input` / config capability `image_input`
- `NativeClaudeSession` 直接吃 Claude image block
- 文本协议模型：降级为「路径说明 + 可选 vision 工具」，并在 UI 标明「当前模型不支持原生看图」
- `/model` 切换时若有未发送附件，提示能力变化

### P1 — 稳妥与跨前端一致

#### 5.5 全前端附件入口

| 前端 | 改法 |
|------|------|
| Feishu | 已有 `images=`，改为走统一 encode |
| Discord | `_download_attachments` 已有路径 → **传 `images=`**，勿只拼文本 |
| Telegram / WeChat / WeCom | 下载后 `images=` |
| Qt / Streamlit | 文件选择器 / 拖拽 → `images=` |
| CLI | 支持 `--image path` 或 stdin 旁路文件列表 |
| ACP bridge | 协议层透传 image block / path |

#### 5.6 Placeholder 与 transcript

- transcript 事件记录：`images: [{path, placeholder, sha1, media_type}]`
- compact 时：**不要把 base64 当文本压缩**；可保留「本轮含 N 张图」摘要，或按策略 drop 旧图仅留路径说明
- resume：路径仍在则重编码；路径丢失则 UI 标失效

#### 5.7 与 vision 工具边界

文档化：

- 用户消息图 → 多模态 user content（本调研）
- agent 截 UI → `vision_api` / OCR（现有 SOP）
- 禁止：用户贴图却只把路径当字符串让模型「想象」

### P2 — 体验与计量

#### 5.8 Token 估算细化

- 短图/已缩放图可按 `max(1500, f(width,height,detail))`
- 或对 base64 长度分段估算（对齐 Codex history）
- 压缩触发阈值必须用「去 base64 后的计量」——`token_meter` 已在做，保持测试锁住

#### 5.9 UX 细节（抄 Claude Code）

- 焦点恢复 + 剪贴板有图 → 一次性提示
- 贴图失败区分：无图 / 过大 / 格式不支持 / 模型不支持
- 仅图无字允许提交（若模型支持）
- 多图粘贴批量 attach，编号连续

#### 5.10 安全

- 限制可读根（工作区 / temp / 用户显式路径）
- 拒绝超大文件在 base64 前就 `stat` 拦截
- 临时剪贴板文件放 `temp/` 并定期清理
- 日志：`_write_llm_log` **禁止完整打印 base64**（只打 media_type、path、sha1、字节数）

---

## 6. 建议落地顺序（可执行切片）

### Slice A — 后端编码与 gate（无 UI 也能受益）

1. 新增 `encode_image_for_prompt` + 单测（缩放、超限、bmp、缓存）
2. `_image_block` 改走 encoder
3. `_native_image_input_enabled` → capability 判断，覆盖 Claude native
4. llm log 脱敏

### Slice B — 协议贯通

1. `put_task` / transcript / ink bridge 字段对齐
2. Discord / Feishu 统一 `images=`
3. 文本路径抽取保留为 fallback，并加「已抽取 N 张图」的 display 提示

### Slice C — Ink 贴图 UX

1. 剪贴板 → temp png（Windows PowerShell 优先，最贴合本仓库环境）
2. `[Image #N]` 芯片 + attachments store
3. 提交带 images；删除芯片不同步发送
4. 程序化测试：路径粘贴识别、附件 prune、bridge JSON 形状（参考 `docs/ga_ink_ui_testing_playbook_2026-07-16.md`）

### Slice D — 体验打磨

1. 模型不支持时的明确报错
2. 剪贴板提示
3. compact / resume 策略

---

## 7. 明确不建议做的事

1. **不要**为了贴图引入完整 litellm 或重型浏览器自动化。
2. **不要**在 history 里长期存未截断的 data URL 字符串当「真源」；真源用 path + 必要时 session image-cache。
3. **不要**只在 Ink 做、后端仍无 resize——大图会在非 Ink 前端（飞书）同样炸。
4. **不要**用 `vision_api` 包装层假装原生多模态：工具往返多一轮、系统提示更脏、且依赖 agent 自觉调工具。
5. **不要**在 Windows 上假设 `xclip`/`osascript`；GA 主战场是 win32，剪贴板方案应 **PowerShell / 本地 API 优先**，再考虑跨平台。

---

## 8. 关键文件索引

### Codex

- `codex-rs/protocol/src/user_input.rs` — `UserInput::Image|LocalImage`
- `codex-rs/utils/image/src/lib.rs` — resize / encode / LRU
- `codex-rs/tui/src/clipboard_paste.rs` — 剪贴板与 WSL fallback
- `codex-rs/tui/src/bottom_pane/chat_composer.rs` — attach / placeholder / submit
- `codex-rs/core/src/tools/handlers/view_image.rs` — 工具读图共用 codec
- `codex-rs/core/src/context_manager/history.rs` — image token 估算

### Claude Code

- `src/utils/imagePaste.ts` — 剪贴板 / 路径 / BMP
- `src/utils/imageResizer.ts` — 2000px + 5MB
- `src/utils/imageStore.ts` — session image-cache
- `src/utils/imageValidation.ts` — API 边界校验
- `src/hooks/usePasteHandler.ts` — 粘贴状态机
- `src/hooks/useClipboardImageHint.ts` — 焦点提示
- `src/components/PromptInput/PromptInput.tsx` — `[Image #N]` 生命周期
- `src/constants/apiLimits.ts` — 硬限制常量

### GA

- `agentmain.py` — `_extract_image_paths` / `_build_user_content_with_images` / gate
- `llmcore.py` — `_msgs_claude2oai` / `_to_responses_input` / `native_image_input`
- `token_meter.py` — `IMAGE_BLOCK_TOKENS`
- `tests/test_native_image_input.py`
- `frontends/fsapp.py` — 唯一认真传 `images=` 的前端
- `frontends/ink_bridge.py` / `frontends/ink-ui/src/paste.ts` — 当前无图
- `memory/vision_sop.md` — agent 侧 vision，非用户贴图

---

## 9. 验收标准（做完怎么算好）

1. Ink 下 Ctrl+V 贴截图 → composer 出现 `[Image #1]` → 发送后模型能描述图内容（`native_image_input`/支持 vision 的模型）。
2. 粘贴 `D:\shots\a.png` 或拖路径 → 同上，不依赖手打引号。
3. 删除 `[Image #1]` 后发送 → 请求中无 image block。
4. 10MB / 8000px 大图 → 自动缩小或明确拒绝，不 413、不把 base64 打满日志。
5. Feishu / Discord 附件与 Ink 走同一 encoder。
6. Claude native 与 OAI native 都能收图（capability 打开时）。
7. `python -m unittest tests.test_native_image_input` 及新增 codec/UI 测试全绿。

---

## 10. 总结

Codex 与 Claude Code 在用户图片上的共同点不是「会 base64」，而是：

1. **UI 把图当成附件对象**（placeholder + 生命周期）
2. **真源尽量是路径，编码尽量延迟且有界**
3. **剪贴板 / 路径 / 拖拽多入口**
4. **发请求前统一 codec + 校验**

GA 已有转换链和 Feishu 附件参数，缺的是把这条链提成产品级输入通路。按 Slice A→B→C 推进，可以在不大改 agent loop 的前提下，把「能看用户的图」从半成品做成默认能力。
