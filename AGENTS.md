# Repository Guidelines

## 项目结构与模块组织

GenericAgent 是一个紧凑的 Python 项目。核心运行时代码位于仓库根目录，包括 `agentmain.py`、`agent_loop.py`、`ga.py`、`llmcore.py` 和 `simphtml.py`。可安装的 CLI 包在 `ga_cli/`，`ga` 命令入口映射到 `ga_cli.cli:main`。各类界面和聊天/机器人适配器位于 `frontends/`；图片、皮肤和静态资源位于 `frontends/skins/` 与 `assets/`。长期记忆、SOP 和辅助工具位于 `memory/`，反射与自主运行辅助逻辑位于 `reflect/`，可选集成放在 `plugins/`。测试统一放在 `tests/`。

## 构建、测试与本地运行

- `python -m pip install -e .`：以 editable 模式安装核心包和 `ga` 命令。
- `python -m pip install -e ".[ui]"`：安装核心依赖和桌面/TUI UI 依赖。
- `python launch.pyw`：启动默认桌面界面。
- `python frontends/tuiapp.py`：启动终端 UI。
- `streamlit run frontends/stapp2.py`：启动 Streamlit 前端。
- `python -m unittest discover -s tests`：运行当前测试套件。

只安装正在修改的前端或机器人适配器所需的可选依赖。

## 编码风格与命名规范

使用 Python 3.10-3.13。代码应保持紧凑、可读，并贴合现有文件风格。优先使用自解释的函数和变量，少写解释性注释。避免过宽的 `try/except`，重要错误应清晰暴露。模块、函数和变量使用 `snake_case`，类名使用 `PascalCase`。新增模块应靠近功能边界，例如 UI 适配放在 `frontends/`，可选集成放在 `plugins/`。

## 行尾与文件编码（统一 LF）

仓库文本文件统一 **LF**（入库 + 检出），由仓库内 `.gitattributes`（`* text=auto eol=lf`）声明、`.editorconfig` 兜底；只影响本仓库。

- **编辑/生成任何文本文件都直接写成 LF（`\n`），不要 CRLF。**
- 例外：`.bat` / `.cmd` / `.ps1` 保持 CRLF；`.sh` 保持 LF；二进制（`.png` / `.jpg` / `.gif` / `.pdf` 等）不做转换。
- **看到 `git status` 报 ` M` 但 `git diff` 为空时，先怀疑行尾**（stat 缓存假象）——`git checkout -- <file>` 或 `git add` 一下即可，**不要展开排查、不要反复重写**，那是纯 token 浪费。
- 程序化写文件的坑（实测）：**Python `open(p,"w")` 在 Windows 会把 `\n` 翻成 `\r\n`** → 用 `open(p,"w",newline="\n")`（或写二进制）；Node `fs.writeFileSync` 默认原样写，确保字符串里是 `\n`。
- 快速校验（期望 0）：`grep -c $'\r' <file>`

## 测试指南

测试使用标准库 `unittest`。测试文件命名为 `test_*.py`，放在 `tests/`。新增前端或适配器行为时，应 stub 外部服务，避免依赖真实 API 凭据。提交前运行 `python -m unittest discover -s tests`，修复 bug 时补充聚焦的回归测试。

### Ink UI 测试：优先「无截图」的程序化手段

Ink UI 测试位于 `frontends/ink-ui/src/*.test.ts`，用 Node test runner + tsx 运行。调试 Ink UI bug（光标/IME、布局、换行、重复渲染、流式）时，优先写程序化回归测试而非靠人眼看截图。核心思想：终端 UI 的 bug 最终都表现为「写进 stdout 的字节」或「布局出来的行列几何」，二者都是确定性、可被机器断言的。按现象选手段——相对光标算术用虚拟终端追踪器（`cursorParkModel.ts`），控制序列用字节级 ANSI 断言（`match` + `doesNotMatch` + `indexOf` 切片查顺序），布局用内存终端（`CaptureWriteStream`/`FakeReadStream` + `render(<App/>, {debug:true})`）配帧几何解析器，布局/分区决策抽成纯函数单测，CJK/emoji 换行一律用 `string-width`（绝不用 `.length`），重复渲染用唯一探针文本计数。完整方法论、决策表和可复用工具见 `docs/ga_ink_ui_testing_playbook_2026-07-16.md`，新增或调试 Ink UI 测试前先读它。唯一盲区：跨终端行为（如 IME 锚定可见原生光标）只能靠真机截图发现，之后仍应尽量把它降维回字节级契约测试。

### Ink 终端 resize（reflow）：禁止依赖 ink 的相对 eraseLines

终端**宽度**变化会让终端对已写出的缓冲区 reflow（长行重新折行），ink 的 `log-update` 下一帧仍按
「改尺寸前」的 `previousLineCount` 做相对擦除，擦不干净就会残留旧帧顶行 —— 真机表现是放大/缩小终端后
底部信息重复叠加。处理方式固定为「整屏重置 + 全量重绘」，不要改成去猜 reflow 之后的几何：

- `terminalCleanup.resetViewportSequence()` 产出清屏 + 清 scrollback + 归位序列；
- `cursorPark.resetViewport()` 写出该序列并丢弃 park 记账（含作废未决的 park microtask）；
- `App.tsx` 的 resize 监听先提交新尺寸，再重置，最后重挂 `<Static>` 让 ink 重发历史并按新宽度重画。

根因、Claude Code / Codex 对照、复现模型与红/绿证据见
`docs/20261010-ink-resize-reflow-duplication-fix.md`；回归测试 `src/resizeReflow.test.ts`（配套
reflow 感知虚拟终端 `src/resizeReflowModel.ts`）。高度变化不触发重置（不 reflow，且可能因帧内容未变而空屏）。

### Ink 转录区重印：`/resume`、`/rewind` 必须一次 commit 完成

Ink v5 用 **legacy root**（`ink/build/ink.js` 里 `createContainer(..., 0, ...)`），React 事件之外的
多次 state 更新**不会**被自动批处理。`<Static>` 只能追加、无法撤销已经写进 scrollback 的行，所以
「历史被替换/裁剪后重印」必须满足两条：

1. 重印代数由 reducer 原子 `+1`（`AppState.staticGeneration`，`history_replace` / `rewind_done`
   各自在同一个 action 里改 messages 与代数），一次 `dispatch` 就是一次 commit、一次重印；
   不要在 bridge 事件处理里再单独调 `resetStaticTranscriptOutput()` —— 那会先「追加尾部」再
   「全量重印」，恢复的历史比当前会话长时尾部会重复（截图 `resume_bug.png`）。
   `resetStaticTranscriptOutput()` 只用于「历史没变、纯重画」的终端 resize。
2. 历史变短的路径（`/rewind`）必须先整屏重置（`resetTranscriptViewport()`）再重印，否则留在
   scrollback 上的旧行会与新印的行叠加。

回归测试 `frontends/ink-ui/src/resumeTranscriptDuplication.test.ts`（屏幕级断言用
`src/resizeReflowModel.ts`）。复盘见 `docs/20261010-ink-resume-duplication-and-list-latency.md`。

### `/resume` 列表：只走轻量扫描 + `(mtime, size)` 缓存

`frontends/continue_cmd.py::list_sessions()` 是 `/resume` 的列表入口，Ink bridge 一次 `/resume`
会调它两次（列列表、按序号恢复）。它**不得**调用 `session_transcript.load_session()`：
`session_transcript.list_sessions()` 返回 `SessionSummary`（只含 session_id / preview / rounds /
last_seq / 用户消息文本），turn 簿记与 `load_session` 逐条对齐。legacy 日志的用户消息文本从
`_pairs()` 结果里取（`_user_text`），不要再整读一遍重建整段对话。

列表字段按 `(mtime, size)` 缓存在被扫描目录的 `.listing_cache.json`（`listing_cache.py`，原子写、
可损坏自愈、失败只退化为重扫）；日志都是纯追加，所以这个失效依据成立。本机 2471 个文件实测：
修复前 9–17 s，修复后冷启 3.0 s / 热启 0.09–0.15 s，输出与旧实现逐条一致。

## 提交与 PR 规范

近期历史使用 Conventional Commits，例如 `feat(tui): ...`、`fix(tgapp): ...`、`docs: ...` 和 `refactor: ...`。提交应小而聚焦。PR 应说明背景、概述行为变化、列出验证命令；只有可见 UI 变化才附截图。避免不必要的新依赖和大范围重构。

## 安全与配置提示

不要提交真实 API key 或本地密钥。配置示例应维护在 `mykey_template.py`、`mykey_template_en.py` 或 `assets/configure_mykey.py`。本地生成状态、日志和凭据应保持在版本控制之外。

严格禁止执行、生成、写入或持久化任何包含可疑 base64 载荷、公益站/公益 token 广告、`token自由` 类推广文本、弹窗广告、自启动项、计划任务、注册表 Run 项、VBS/PowerShell 注入脚本或其他恶意/入侵代码的内容。遇到此类内容时，只能进行只读检查、解码说明、定位和删除，禁止运行其载荷或帮助其建立持久化。

## 参考实现：Claude Code 源码

Claude Code 本地源码位于 `D:\git_codes\claude-reviews-claude\claude-code-fork\src`。它是 React + Ink 终端 UI，并在 `src/ink` 下 fork 了 Ink。处理 GA Ink UI 的光标 / IME / 布局问题时，这是最高价值的参考——它解决了 GA 遇到的同一个原生光标/IME 问题。关键文件：`src/ink/components/CursorDeclarationContext.ts`、`src/ink/hooks/use-declared-cursor.ts`（frame 声明 cursor 模型）、`src/ink/frame.ts`、`src/ink/ink.tsx`、`src/ink/log-update.ts`（单一 stdout writer：diff 帧后统一放 cursor）。GA 侧分析见 `docs/superpowers/specs/2026-07-15-ga-self-managed-terminal-design.md` 与 `docs/ga_claude_code_cursor_handling_2026-07-16.md`。

GA Ink UI 的 IME/光标 bug，权威根因文档是 `docs/ga_ui_ime_visible_native_cursor_root_cause_2026-07-16.md`：Windows Terminal 的输入法候选框锚定在**可见**的原生光标上（DECTCEM `\x1b[?25h`），因此 cursor-park 包裹流（`frontends/ink-ui/src/stdoutCursorPark.ts`）必须在光标停到 caret 后 SHOW、在下一帧写入前 HIDE。2026-07-14/07-15 早期诊断得出的「保持原生光标隐藏、只用反显块」结论是错的，已在该文档中修正。

## 参考实现：Pi agent（提速参考）

Pi agent 本地源码位于 `D:\git_codes\pi`。它是 TypeScript monorepo（agent 循环、多 provider LLM、TUI、codemode、MCP 分包），以启动快、首 token 快著称，比 Codex 响应更快。需要优化 GA 的启动延迟或响应速度时，先看它：核心手法是 MCP 连接异步化（首个 prompt 只等 `direct` 工具，其余后台连接、按需等待）、重依赖 lazy load、启动期工作全部推迟到首帧之后、prompt cache 主动预热与 miss 统计、流式 + 默认并行工具执行。

完整机制分析、关键文件与 commit 清单、以及 GA 侧对应慢点（`agentmain.py:821` 每次提问同步等 MCP 发现，实测冷启约 16s / 热 0.016s）见 `docs/20261001-pi-agent-speed-reference.md`。

## 参考实现：Codex 源码

Codex CLI 的最新本地源码位于 `D:\git_codes\codex`，是 GenericAgent 实现和修复相关能力时的首选参考实现。凡涉及 subagent 生命周期、agent team 协作、动态工作流、任务调度与恢复、控制面/事件流、TUI 与 UI 显示、stdout 所有权、scrollback/选区、终端 draw/cursor 等特性，都应优先直接查阅该目录中的实际源码和测试，不要只依赖二手笔记或记忆。

- TUI、终端渲染、光标、滚动和选区行为：优先查看 `D:\git_codes\codex\codex-rs\tui`。
- agent/runtime、subagent、团队协作、动态工作流、控制面、事件和任务生命周期：查看 `D:\git_codes\codex\codex-rs` 其余相关模块，并同时阅读对应测试。
- 需要确认 Codex 当前行为或设计取舍时，以本地源码和测试为准；不要把旧版本笔记当作现行实现。

GA 侧已有分析笔记：`docs/ga_subagent_codex_reference_2026-07-13.md`、`docs/ga_codex_vs_ink_stdout_ownership_2026-07-15.md`、`docs/ga_ink_ui_text_selection_copy_diagnosis_2026-07-14.md`。

## 参考实现：Step-Code 的 ultracode / workflow

Step-Code 本地源码位于 `D:\git_codes\Step-Code`（stepfun-ai 的 Code CLI）。它的 workflow 是我们做 GA 动态 workflow 时最直接的对标实现：同一类"模型生成编排脚本 → 宿主执行 → 多 agent fan-out"的架构，但把稳定性问题收敛得比 GA 更好。开发 GA workflow（planner、runtime、scheduler、progress、UI）前先读这套代码。

关键文件：

- 设计文档：`docs/orchestration-lifecycle.md`（opt-in 语义、生命周期、fan-out 边界、预算、超时与 journal 契约）
- 编排运行时：`packages/coding-agent/src/features/workflow/runtime.ts`（agent 调用、并发槽、schema 重试、budget、timeout、iterate 循环）
- 宿主工具与 opt-in：`step-workflow.ts`（workflow 工具本体与系统提示）、`ultraloop-opt-in.ts`（`ultracode`/`ultraloop` 关键字与 session 级开关）
- 沙箱与契约：`vm.ts`（QuickJS/WASM 隔离）、`types.ts`（JSON-only 契约）、`schema.ts`（结构化输出校验）、`tool-profile.ts`（readOnly/writable 工具与路径 ACL）
- 恢复与观测：`journal.ts`（可重放 journal）、`progress.ts`、`rendering.ts`、`budget.ts`、`registration-gate.ts`、`acl-extension.ts`、`agent-runner.ts`
- HoH 迭代：`hoh.ts`（Planner → Developer → 独立 QA 的结构化 schema 与 coverage/stagnation 停止条件）

值得直接借鉴的机制：

1. **opt-in 与能力分离**。workflow 工具注册只是环境能力，是否使用由 per-turn 关键字（`ultracode`/`ultraloop`/显式要求）或 session 级 `/ultraloop on` 决定，`agent_settled` 时清理；模型不得从"任务看起来大/可并行"推断 opt-in。GA 的 approval gate 与 mode 路由可以对齐这个"能力 ≠ 授权"的分层。
2. **fan-out 只有一层**。子进程带 `STEP_CLI_SUBAGENT_CHILD=1`、`STEP_DISABLE_WORKFLOW=1` 等标记，不能再次 fan-out，也不会持有 cron/goal 调度权。GA 的 workflow child 与 subagent 递归边界可参照此约束。
3. **JSON-only 契约 + 结构化输出强制**。`agent(prompt, {schema})` 让下游消费的结果必须是 JSON，schema 不匹配带校验错误重试最多 3 次，失败即失败，不降级成自然语言。GA 的 strict verification schema 思路一致，但应保证"哪些检查该跑"由计划显式声明，而不是由任务类型字符串隐式推断（GA 曾因研究型计划被塞入 `python_unittest` 而在无测试文件时误判 `NO TESTS RAN`）。
4. **预算与超时 fail-closed**。token budget 是 input+output 累计，超额对整个 run 报 `budget_exceeded`，即使脚本 catch 了异常；agent 有默认 30 分钟超时；并发有硬上限。GA 的 retry/delegation 上限应保持同样"有界且不可被脚本绕过"的语义。
5. **journal 可重放 + resumeFromRunId**。相同 script+args 可重放 cached journal 前缀，首个变化点之后才重新执行。GA 的 run store/journal 可对齐这一可恢复性。
6. **显式停止条件**。`iterate()` 用 coverage 目标、stagnation 上限、max iterations、empty objective 四类停止条件；`parallel()` 是 barrier 且抛错的 task 变 null、`pipeline()` 无 barrier 逐项并发。GA 的 wave 调度可借用这套"barrier vs pipeline"区分。
7. **progress 快照独立于后续变更**。每次 `onUpdate` 都带一份完整 `WorkflowProgress` 快照，tool row 在 settled 后保留最后快照并忽略迟到的 child 清理事件。GA 的 Ink UI workflow 面板可对齐该快照语义。

**重要方向性判断**：Step-Code 的 workflow 契约里没有 `taskType`（research/coding/review）这一概念，校验与门禁由计划显式声明的 schema、toolProfile、ACL 和 budget 决定。GA 目前用 `taskType` 分支推导硬门禁（如 coding → `python_unittest` + strict verification schema），这是研究型/混合型任务被错误套上代码型约束的根源。后续优化 GA workflow 时，应把门禁来源从"任务类型"迁移到"计划显式声明的检查项"，任务类型降级为提示信息而非门禁开关。

### 结构化输出契约（workflow schema 子代理）

声明了 `schemaRef` 的 agent 必须得到三件事，缺一个就会让整条 run 因 `schema_validation_failed` 失败：

1. `workflow_child_agent.py::_structured_output_contract()` 追加的 `<workflow-structured-output>` 输出契约（对齐 Step-Code `agent-runner.ts::buildAgentPrompt`，32 KiB 截断）。只把 schema 放在 `options` dict 里不算契约，模型不会知道自己的回答会被机器校验。
2. `agent_loop.py::_clean_content()` 不得压缩 JSON / `json` 代码块；该函数只服务于人类可读的终端输出，不能作用于机器校验的产物。
3. `workflow_scheduler.py`：重试必须通过 `retryFeedback` 带上一次校验 issues（`_schema_retry_feedback`），repair 必须携带原 prompt + schema；`downstream_result()` 必须把通过校验的 schema 字段暴露给脚本（Step-Code 语义是 `agent()` 直接返回校验后的值）。

真实回归证据见 `docs/20261009-ga-workflow-stability-stepcode-borrow.md` §11 与 `tests/real_workflow_strict_schema_child_e2e.py`。

### Ink 工作流实时进度与产物引用

两个反复踩到的坑，改 workflow UI/handoff 前先确认：

1. **实时进度**：`frontends/ink_bridge.py::_run_workflow_runtime` 必须边跑边推 `workflow_progress`（`_watch_workflow_progress()` 轮询 durable 的 `workflow-progress.json`）。只在 runtime 返回后推一次，UI 会全程停在 `0/0 agents done`。
2. **产物引用**：计划里的 `artifacts`/`deliverables` 是语义标签，不是路径。真实路径来自子代理自己的 `file_write`/`file_patch` 调用（`workflow_scheduler._record_observed_artifacts()` → `job.metadata["observedArtifacts"]` → `workflow-progress.json` → handoff `intermediateResults[].artifactRefs`）。handoff 里必须说明这些是 **workspace 相对路径**，基准是 GA workspace 根（默认 `temp/`），而不是 run 的内部 artifact 目录。不要把"计划标签"当路径交给 LLM 去猜。

证据见 `docs/20261009-ga-workflow-stability-stepcode-borrow.md` §12。

## Ink workflow progress: planning off the command loop, live telemetry, panel keys

`/workflow` responsiveness is a three-part contract. Do not regress any of them; the analysis,
Codex comparison and real-run measurements are in `docs/20261010-ga-ink-workflow-progress-ui.md`.

1. **The planner never runs on the JSONL stdin loop.** `run_jsonl_loop` dispatches `workflow_plan`
   to `InkBridge.workflow_plan_async`, which flips `_workflow_planning`, emits
   `status: running` + `activity: Planning workflow` immediately, and plans on a worker thread.
   Running the planner inline made the composer lag and delayed the first workflow UI update.
   Real measurement: 1 ms from `workflow_plan` to `status/activity`, and a second command answered
   10 ms later while planning was still in flight.
2. **Polling is signature-gated and rate-limited.** `_watch_workflow_progress` reads the run once
   for `artifact_dir` and afterwards only `os.stat`s `workflow-progress.json`
   (`_file_signature`), skipping the tick when nothing changed; `_emit_agent_read_model()` is
   throttled by `_AGENT_READ_MODEL_MIN_INTERVAL`. The old version called
   `workflow_store.load_run` (~12 ms) plus a full `json.dumps` four times a second.
3. **Live child telemetry is a UI-only observation channel.** A running child writes
   `agents/<jobId>/live.json` (`workflow_store.write_job_live_telemetry`, throttled <=1 s) with
   `turn`/`toolCalls`/`lastToolName`/`elapsedSeconds`/`tokenUsage`; the bridge turns it into
   `{"type": "workflow_live", "runId", "jobs": [...]}`. `workflow-progress.json` remains the single
   durable snapshot and nothing in this channel may feed an acceptance check. The writer is
   attached as `handler.live_telemetry` (via `LiveJobTelemetry`), never as an extra
   `_build_handler` parameter, because test doubles replace that method with the old signature.
4. **The workflow panel owns its keys while it is open.** `workflowPanelCapturesKey(panel, key, raw)`
   claims `j k g x r s`, arrows, paging, `Enter` and `Esc`, and `App.tsx` must `return` on it so the
   key never lands in the composer. Panel height follows the terminal
   (`WORKFLOW_PANEL_MAX_ROWS`), not a hard-coded 8 rows.
5. **After the terminal workflow event the UI may stay `running` on purpose.** The host hands the
   workflow result to the agent loop, which answers with it, so `idle` arrives after
   `workflow_final`. Real Ink E2E harnesses must `waitFor` idle (see
   `real_ink_ui_workflow_autonomous_e2e.ts`) instead of asserting on the `workflow_final` frame.

## 文档命名约定

新增测试记录、故障复盘、验收报告和技术调研文档时，文件名统一使用 `YYYYMMDD-xxxx.md` 格式，例如 `20260930-gpt6-luna-generic-agent-capability-evaluation.md`。日期使用 Asia/Shanghai 当前日期，`xxxx` 使用简洁、可检索的英文小写短语。

## LLM 配置与本地验证约定

LLM provider/model/profile 的主配置通过根目录的 `llm.yaml` 加载，模板见 `llm.yaml.example`；`llm.yaml` 已被 `.gitignore` 忽略，可以存放本机真实密钥，但真实 API key 禁止提交到 Git。新增第三方 OpenAI 兼容模型时，应同时检查 `llm_config.py`、`llm_client.py` 和 `llmcore.py` 的 `wire_api` 映射。

截至 2026-09-30，本机 `llm.yaml` 已加入 Xem8k5 端点 `https://ai.xem8k5.top/v1` 的两个本地测试模型：`gpt-6-luna` 和 `deepseek-v4.1-flash`。两者通过 `openai_responses` 配置验证；同一端点若需走 Chat Completions，应使用 `openai_chat` provider。对应真实 key 仅存在于本机被忽略的 `llm.yaml`，模板只保留 `api_key_env` 示例。

项目当前已经支持 OpenAI Responses API：请求构造位于 `llmcore.py:_openai_stream()`，输入转换为 `_to_responses_input()`，SSE/JSON 解析分别由 `_parse_openai_sse(..., api_mode="responses")` 和 `_parse_openai_json(..., api_mode="responses")` 完成。新增或修复 Responses 兼容性时，优先补充脱敏的协议单测，再用本机 `llm.yaml` 做真实端到端验证；不要把真实响应、Authorization header 或 API key 写入仓库。

### Xem8k5 validation note (2026-09-30)

The local `gpt-6-luna` and `deepseek-v4.1-flash` profiles both passed the OpenAI Responses and Chat Completions paths for a development-oriented prompt and for image input from `截图/图2.png`; both image runs returned the required `IMAGE_DEV_OK` marker and produced a programmatic UI-layout assertion. An earlier deepseek 401 was caused by a local configuration typo (`...KlsdUMa1` instead of the supplied `...KlsdUM1`); after correcting the ignored local `llm.yaml`, `/models` returned 200 and both model paths passed. Do not copy keys, Authorization headers, or raw responses into the repository.
### GenericAgent capability evaluation note (2026-09-30)

真实 `gpt-6-luna` 与 `deepseek-v4.1-flash` 的 skills/TDD、双 subagent、动态 workflow、真实 Tavily MCP 验收记录见 `docs/20260930-gpt6-luna-generic-agent-capability-evaluation.md`。后续修改 agent runtime 或新增能力时，优先复用该报告中的最小真实 E2E 结构；复杂 planner E2E 的 timeout 不能替代基础链路验收。

### Pi subagent reference and GA hardening

Pi subagent design evidence and the GA hardening mapping are documented in `docs/20261001-pi-subagent-design-reference.md`; the implementation plan is `docs/superpowers/plans/2026-10-01-ga-subagent-hardening.md`. For subagent, multi-agent, agent team, and workflow children, enforce isolated context by default (`fork_turns=none`), remove orchestration tools from child schemas unless explicitly enabled, pass parent context only through explicit messages/artifacts/chains, and never let business allowlists deny the internal `no_tool` sentinel. Do not rely on prompts as the only control for identity, recursion, or tool boundaries.

## Workflow autonomous activation hardening

GA workflow activation, explicit execution contracts, host MCP/tool preflight, run-scoped workspace/resume behavior, terminal events, and the DeepSeek + Tavily Ink UI E2E are tracked in `docs/20261002-ga-workflow-autonomous-activation-hardening-reference.md` and the implementation progress in `docs/20261002-ga-workflow-autonomous-activation-hardening-progress.md`. Read these before changing workflow planner/runtime/Ink routing. Preserve the invariant that `taskType` is advisory only: declared actions, tools, scopes, artifacts, and machine-checkable evidence determine execution gates.

## Workflow degradation and terminal semantics

Workflow terminal states are three-valued, not "failed vs succeeded": `completed`, `degraded` (a declared optional contract fell back; core delivery still usable), and `failed` (schema/artifact/path/budget/execution contracts not met). The Step-Code comparison, root causes, and P0–P3 implementation record live in `docs/20261009-ga-workflow-stability-stepcode-borrow.md`. Read it before touching schema fallback, artifact acceptance, planner fallback, or run status projection. Invariants to preserve: a declared schema is fail-closed unless the plan writes `schemaPolicy: "optional"`; a declared non-optional `executionContract.artifacts[]` entry is existence-checked by the host regardless of `requiredChecks`; a text-schema fallback or deterministic planner fallback terminates the job/run as `degraded` and never as accepted/passed; `failurePolicy=continue` affects scheduling only, never final acceptance. A planner reply that cannot be decoded as JSON is a *repairable* defect, not a provider outage: `workflow_planner.PlannerResponseError` routes it into the repair loop as `planner_response_unparsable` so the model gets one chance to fix its own syntax. Only a genuine provider/transport failure may go straight to `_fallback_draft`. Real case: a trailing comma at line 190 of the planner reply degraded `wf_a076100a22ef4e799fe2cd8dd81a027a` even though all three children succeeded.

`workflowIssues` is a gate of *unresolved* problems, not a history log: `workflow_scheduler._apply_schema_contract()` must call `_clear_schema_issue(job)` when the schema finally validates, otherwise a retry that recovered leaves a stale `schema_validation_failed` and `workflow_runtime` degrades a run whose every job ended `succeeded` (real case: `wf_6e481417f6e344cc80249b7f50fd3418`). Only that job's schema issue is cleared — a `fallback: text` degradation and a job that failed every attempt keep theirs. See §16 of `docs/20261009-ga-workflow-stability-stepcode-borrow.md`. Real E2E: `GA_RUN_REAL_WORKFLOW_DEGRADED_E2E=1 python tests/real_workflow_degraded_semantics_e2e.py` (uses `profiles.default`, currently cc-deepseek-v4.1-flash-chat).

## Runtime prompt layers: base prompt vs GA_AGENTS.md

GA splits its runtime prompt the way Codex splits `gpt-5.2-codex_prompt.md` from `AGENTS.md`:

- `assets/sys_prompt.txt` (+ `sys_prompt_en.txt` for `GA_LANG=en`) is the **base prompt**: identity,
  capability framing, working style, verification discipline, general tool/search/path rules, and
  communication style. It applies to every workspace.
- `GA_AGENTS.md` / `GA_AGENTS.override.md` is the **project layer**: workspace paths, project map, test
  commands, safety, layering semantics. `ga_agents_runtime.build_ga_project_instructions()` injects it as
  `[GA_PROJECT_INSTRUCTIONS]`.
- `ga_agents_runtime.load_base_system_prompt()` is the single reader for the base file. Both
  `agentmain.get_system_prompt()` (root agent) and `NativeGPTChildAgentRunner._build_system_prompt()`
  (workflow child) go through it, so the two cannot drift.

Do not re-add general working-style or tool-discipline sections to `GA_AGENTS.md`: they belong in the
base prompt so that runs in *other* workspaces (which have no GA_AGENTS.md) still get them. Regression
coverage lives in `tests/test_ga_agents_runtime.py::RepoPromptLayersTest`.

## Prompt layers: base prompt, role hints, planner playbook

GA's runtime prompt is layered, and each layer has a job. Read
`docs/20261010-ga-prompt-layer-benchmark-and-plan.md` before editing any of them; it benchmarks GA
against pi (`packages/coding-agent/src/core/system-prompt.ts`, per-tool `snippets`/`guidelines`, the
`examples/extensions/subagent/agents/*.md` role prompts), Codex (`core/gpt_5_2_prompt.md`,
`prompts/src/model_messages/multi_agent.rs`, `core/templates/collab/experimental_prompt.md`) and
Step-Code (`features/workflow/step-workflow.ts` tool description, `agent-runner.ts`,
`step-subagent-agents.ts::formatBuiltinAgentGuidance`).

- `assets/sys_prompt.txt` / `_en`: identity, boundaries, working style (planning, change discipline,
  review and deliverable quality), verification, tools, communication. Keep the two languages in sync.
- `subagent_prompts.py`: root vs subagent role hints. A child shares the parent's workspace and gets
  an isolated context, so the root hint must tell it to say "you are not alone in this workspace" and
  to assign disjoint write paths; the subagent hint must say its final answer goes back to the parent
  and that orchestration tools were removed.
- `workflow_planner.py::_planner_prompt`: `orchestrationPolicy` is contract bookkeeping
  (schemas, capability classes, acceptance ids) and `orchestrationPlaybook` is strategy (named
  patterns, verification spend, sizing, no silent caps). Do not collapse one into the other: the
  contract list alone produced two-or-three-agent chains for work that needed adversarial verification.
- `workflow_child_agent.py`: `role_instructions` covers every canonical role in
  `workflow_planner.CODING_AGENT_ROLES`. A declared role with no instruction is a role the child
  invents; keep one entry per role, and keep the `sharedWorkspace:` / `handoff:` lines.

All three P1 follow-ups from `docs/20261010-ga-prompt-layer-benchmark-and-plan.md` §3 are now in:
built-in read-only roles published on `spawn_agent`, tool-profile wording generated from the host
tables, and the sectioned system prompt (see the next three sections).

## System prompt sections: named, diffable, one renderer

`agentmain.build_system_prompt_sections()` returns the system prompt as a named, ordered dict
(`SYSTEM_PROMPT_SECTION_ORDER` = base / date / project / notifications / memory / skills / role_hint /
permission_mode) and `render_system_prompt()` is the only renderer; `get_system_prompt()` is just
`render(build(...))`, so the rendered bytes are unchanged. The point is pi's
`diffSystemPromptSections`: the prompt is rebuilt every turn, so `note_system_prompt_sections(agent,
sections)` records the previous snapshot and the loop prints
`[Prompt] system prompt sections changed: ...` only when a section actually changed. That turns "why
did the prompt cache miss this turn" into a one-line answer. Do not re-inline the concatenation in the
turn loop, and do not add a section without adding it to `SYSTEM_PROMPT_SECTION_ORDER` — an unordered
section is a section `render_system_prompt` silently drops.

## Workflow prompt text is generated from the host tables

Never hand-copy "what a toolProfile means" into a prompt: the prompt would keep advertising a
capability the host denies the moment someone edits the table. `workflow_tool_profiles.py` owns the
wording — `allowed_capabilities_for_profile()` derives a profile's allowed classes from
`WORKFLOW_TOOL_PROFILES` (`*` = everything except orchestration), and
`format_tool_profile_guidance()` / `format_role_profile_guidance()` / `format_capability_classes()`
render it. `workflow_planner._planner_prompt` calls those instead of listing
`planner=.../research=.../authoring=...` by hand.

`workflow_child_agent.ROLE_INSTRUCTIONS` is the module-level single source for per-role guidance; it
must stay in two-way sync with `workflow_planner.CODING_AGENT_ROLES` (a declared role with no
instruction is a role the child invents). Drift guards live in
`tests/test_workflow_prompt_single_source.py`: they patch the tables and assert the new text reaches
the prompt, so a table edit cannot silently miss the prompt.

## Built-in subagent roles and the published catalog

`subagent_roles.py::BUILTIN_ROLES` defines `explore`, `plan` and `review`, all `READ_ONLY`, so a fresh
workspace with no `.ga/subagents` directory still offers named roles instead of forcing every task
through a generic subagent. `SubagentRoleRegistry.get()` falls back to a builtin only after a file
lookup misses, and `list_roles()` puts project roles first and appends the builtins they do not
override — a project role of the same name always wins.

`agentmain._apply_subagent_role_schema()` publishes that catalog inside the `agent_type` description:
`format_role_catalog()` renders `explore (read-only) — ...` per role, followed by a "prefer a read-only
role for review, audit or exploration work" line. Listing bare names is not enough, because the choice
happens before the model can see the directory — the same reason Step-Code renders
`formatBuiltinAgentGuidance()` into its subagent tool. `role_capability_note()` derives "read-only" /
"can write" from `permission_profile`, so a role added later is annotated by behaviour rather than by a
hand-maintained list.

## Workflow tool boundary: host-owned profiles, never model-declared tool names

A workflow child's tool boundary is decided by the **host**, not by the plan. The plan declares a
named `toolProfile` (a closed set: `planner` = read-only + search, `research` = + write, `authoring` =
everything, `verify` = read-only + execute, `*` = unrestricted) and/or capability classes
(`web_search`, `web_fetch`, `file_read`, `file_write`, `execute`). `workflow_tool_profiles.py` resolves
that to the child's actual tool schema and to a dispatch-time gate. When a plan names no profile, the
host derives one from the declared role, exactly like `resolve_job_permission_profile` derives the
`verify` permission profile for evidence roles.

Rules that must not regress:

1. **Never let the model name a concrete tool.** `requiredTools`/`requiredToolEvidence` are parsed only
   for backward compatibility with older saved plans; deterministic plans and the planner prompt must
   use `capabilities` + `requiredCapabilityEvidence`. Guessing `mcp__tavily__tavily_search` from the
   substring "tavily" in the task text is exactly the bug this replaces.
2. **Profiles are subtraction only.** They restrict which *classes* of tool a child may use; they never
   require the model to call a specific tool. This mirrors Step-Code's `WORKFLOW_TOOL_PROFILES` and
   Codex's `ToolPolicy.allowed_tools` ("a startup ceiling on tool selection ... only restricts tools").
3. **Orchestration tools are always denied to workflow children** (`spawn_agent`, `wait_agent`, ...).
   They used to leak in through `assets/tools_schema.json`; the profile filter is now the single gate.
4. **A missing capability degrades, it never kills the run and never passes silently.** MCP discovery
   failures are reported in the capability snapshot (`capabilityCoverage` / `unavailableCapabilities`),
   surfaced to the child prompt, and recorded as a `capability_unavailable` workflow issue so the run
   terminates `degraded`. Codex emits `McpStartupUpdateEvent` with a reason and keeps the servers that
   did start; Step-Code simply never constructs the missing tool. GA's old
   `raise RuntimeError("capability_unavailable: ...")` fail-fast turned one dead search server into a
   10-minute scraping loop.
5. **Capability classes are predicates, not name lists.** `tool_capabilities()` classifies by behaviour
   so a tool added later lands in the right class; the same lesson as the artifact-observation fix.

Evidence: `docs/20261009-workflow-tool-profile-capability-boundary.md`. Regression tests:
`tests/test_workflow_capability_preflight.py`, `tests/test_workflow_planner_execution_intent.py`.

## Workflow artifact observation and run workspaces

Workflow artifact observation is **filesystem-based, never tool-name-based**. `workflow_child_agent` snapshots the child workspace before and after each job (`workflow_workspace.snapshot_workspace`/`diff_workspace`) and reports `tool_summary["writtenPaths"]`; the scheduler turns that into `observedArtifacts`/`observedMutations`. Do not reintroduce `file_write`/`file_patch` allowlists — `code_run` (the normal DOCX/HTML path) and any future writer tool are covered by the diff by construction. Comparison logic for "did this job produce something" belongs in `docs/20261009-ga-workflow-stability-stepcode-borrow.md` §13.

Ownership travels with the handoff, never as a tool-name inference. `workspace_writes_with_writer` records the writer once, at diff time; `workflow_store.build_artifact_ownership_index(run)` unions it into a run-level `path -> writer(s)` index that `_record_observed_artifacts()` persists to `run.metadata["artifactOwnership"]` and the progress snapshot. The bridge surfaces it as top-level `artifactOwnership` plus per-stage `intermediateResults[].artifactOwners`, and `artifactCollisions` (same path written twice, last write wins) is reported, never failed. A child process keeps no in-process `handoff` dict — its handoff is written to `workflow-handoffs/<job>.json` — so any consumer must read ownership from run metadata, not from `job.metadata["handoff"]`. Keep `artifactOwners`/`artifactOwnership` in the handoff size-compaction allowlist, or downstream readers lose the ability to tell whose output they are reading. Evidence: `docs/20261009-ga-workflow-stability-stepcode-borrow.md` §15.

`observedArtifacts` entries are `{"path", "writer"}` (the writer is the job label, falling back to `jobId`), recorded at diff time because that is the only moment that knows both facts; `workspace_writes_with_writer()` in `workflow_workspace.py` also accepts legacy bare-path lists so old run state still loads. A path written by more than one job is recorded as `artifactCollisions` plus an `artifact_path_collision` issue and an `artifact_collision` event — reported, not fatal, because a deliberate overwrite is legal.


Every planned/draft/resumed run owns an isolated output workspace at `<base>/workflow-runs/<runId>/`. Two roots must not be confused: `workspacePath` (run-local, where `artifactRefs` resolve) and `workspaceBasePath` (the shared base, where run-internal `resultRef`/`transcriptRef` under `temp/sessions/...` resolve). The Ink bridge resolves artifacts via `_run_workspace_root()` and run-internal refs via `_run_artifact_base_root()`; mixing them makes a correct artifact look missing. `_cache_key()["workspacePathHash"]` hashes the base root so resume still hits cached prefixes.

Every ref published to a reader must open under a root that reader is told about. `resultRef`/`transcriptRef` are relative to the run's internal artifact directory, so the host materialises each job's durable result into the run workspace at `workflow-handoffs/result-<jobId>.json` (`workflow_scheduler._materialize_result_copy`) and every reader-facing surface publishes that workspace-relative copy as `resultRef`, with `resultPath` absolute and `runInternalResultRef` kept for audit: `workflow-progress.json`, the workspace handoff file `workflow-handoffs/<job>.json`, and the post-run handoff in `frontends/ink_bridge.py`. `workflow-handoffs/` is in `HOST_OWNED_WORKSPACE_DIRS`, so writing it while other children run cannot be misattributed to a child diff. A reader that joins the bare internal ref onto `workspacePath` builds a real path that does not exist; that is not a Windows path-separator problem, and it is what made a correct run report its durable result as missing (run `wf_92ad839265dc48f3ac56e5cb86f6e780`). This is not a Windows path-separator issue: a bare `resultRef: agents/agent_1/result.json` joined onto `workspacePath` yields a real, well-formed path that simply does not exist, which is how a correct run reported its durable result as missing.

Path policy is host-side and deterministic. `workflow_path_acl.check_tool_call(workspace_root, tool_name, args)` is the single source of truth for "may this path be read/written/executed"; `GenericAgentHandler.dispatch` calls it before any workflow child tool executes and returns a `path_acl` error on violation. Capability (permission profile) decides *whether* a tool may be used; the ACL decides *which paths* it may touch. Declared artifacts are additionally integrity-checked at run close: a non-optional artifact that is empty (`empty_artifact`) or shrank below the largest size the host observed a child write (`artifact_altered_after_write`) fails the run, which is what the 2026-10-09 cleanup-probe failure required. Keep `expand_file_refs(..., workspace_root=...)` containment when touching `file_write`/`file_patch`.

Handoff summaries are bounded by a structure-aware helper (`workflow_child_agent.bounded_structured_summary`, shared with `workflow_scheduler._compact_handoff_summary`): a structured answer is projected into a smaller **valid** JSON value carrying `_truncated`/`_note`, and prose is cut on a line boundary with an explicit `…[truncated by the host; full value: <ref>]` marker. An already-truncated fragment (unbalanced brackets) is flagged the same way instead of passing as data. Never reintroduce a bare `[:N]` cut — that regression made a synthesis child read half of `sources[0]` and report the upstream sources as unrecoverable.

A dependent child cannot read `resultRef`/`transcriptRef`: those are run-internal audit refs outside its path limit. `workflow_scheduler._materialize_upstream_result()` copies each upstream `result.json` into the run workspace as `workflow-handoffs/upstream-<jobId>.json` before the child starts and hands it over as `upstreamResultPath`, and `workflow_child_agent._build_prompt()` states both roots explicitly. Do not "fix" a missing-upstream report by widening the child ACL or by inlining the payload into the prompt.

`workflow_workspace.HOST_OWNED_WORKSPACE_DIRS` (`__pycache__`, `.git`, `workflow-handoffs`) is excluded from `snapshot_workspace`, so host control-plane writes are never attributed to a running child.

## MCP startup resilience: slow is not failed

MCP connection is governed by a per-server budget that must not be tight enough to read a slow handshake as a failure. `_MCP_DISCOVERY_TIMEOUT_DEFAULT` is 30s (mirroring Codex `DEFAULT_STARTUP_TIMEOUT` in `codex-mcp/src/rmcp_client.rs`), overridable per server via `startup_timeout_sec` or globally via `GA_MCP_DISCOVERY_TIMEOUT`. Remote (HTTP/SSE) transports get bounded initialize retries inside that same budget (`_MCP_REMOTE_CONNECT_ATTEMPTS = 3`, backoff `_MCP_REMOTE_CONNECT_RETRY_DELAYS = (0.25, 1.0)`), retrying only retryable errors per `_is_retryable_connect_error`; stdio is never retried (a dead local process will die again). The connect path is three-valued: `pending` = never attempted, `connecting` = attempt/retry in flight, `failed` = retries exhausted. Only `failed` renders as `✕`; `connecting` renders as `◌` (`frontends/ink-ui/src/mcpPanel.ts`).

Invariants: the background discovery pass (`start_background_discovery`, `retry_failed=True` by default) is the self-heal channel and must retry previously failed servers, while the synchronous `discover_mcp_tools`/`discover_mcp_tools_cached` keep `retry_failed=False` so they never hammer a server known to be down. Local stdio servers connect through a bounded pool (`GA_MCP_LOCAL_CONNECT_CONCURRENCY`, default 4), not a `Semaphore(1)`. stdio stderr logs are size-capped (`GA_MCP_STDERR_LOG_MAX_BYTES`, default 256 KiB) so they cannot grow unbounded. Root cause, Codex comparison, and measurements: `docs/20261010-ga-mcp-startup-connect-failures.md`. Regression tests: `tests/test_mcp_runtime.py::McpStartupResilienceTest`.

## MCP cold start is a background observation, never a gate

Startup must not block the first turn on MCP readiness, and the UI must not read as if it does. Measured on this machine (2026-10-10): bridge `ready` at ~2.0s, first token **0.16-0.19s after submit**, while MCP discovery only settled at 9.6-14.8s (worst server `context7`). The per-server budget is 30s, so a slow remote endpoint can legitimately hold `loading` for 30s+; the first turn still answers because `discover_mcp_tools_cached_fast()` returns a cached catalog immediately and otherwise waits at most `GA_MCP_DISCOVERY_BUDGET` (default 2s).

Invariants:

1. Never add a startup-time inline MCP warmup. Both a synchronous prewarm in `main()` and a background-thread prewarm pushed `ready` from 1.9s to ~3.7s (import cost + GIL contention) and were reverted. `ready` must stay ~2s.
2. `npx` stdio servers get `--prefer-offline` injected by `mcp_runtime._prefer_offline_args()` (host-side, because `mcp.json` is gitignored user config). The registry round-trip costs 4-8s per spawn even when the package is cached. The flag still installs a genuinely missing package.
3. The bridge's failed-server self-heal (`frontends/ink_bridge.py::_watch_mcp_status`) reconnects **only the servers in the `failed` list** via `reconnect_mcp_server`. Do not switch it back to a full `start_background_discovery(retry_failed=True)` pass: one flaky server used to double the `initializing` window for every server.
4. Startup rows say the truth: `MCP connecting in the background · N/M connected · X tools · you can type now` (`frontends/ink-ui/src/mcpPanel.ts`). Input is never disabled by MCP state, so the copy must not imply otherwise.

Codex does the same thing (waits only for `required = true` servers, skips the wait when `allows_cached_startup()` has a cached catalog, 30s `DEFAULT_STARTUP_TIMEOUT`, per-server TUI status) — see `codex-rs/codex-mcp/src/connection_manager/required.rs` and `codex-rs/tui/src/chatwidget/mcp_startup.rs`. Measurements, per-server timings and probes: `docs/20261010-ga-ink-mcp-cold-start-latency.md`; probes live in `frontends/ink-ui/scripts/_probe_mcp_startup.ts` and `_probe_mcp_firstturn.ts`.

## Workflow activation: an explicit /workflow request must execute

`/workflow <task>` is an explicit user opt-in, but the ink UI strips the prefix before planning
(`frontends/ink-ui/src/inputController.ts`), so the planner cannot see it in the task text. The bridge
carries it as `context["activation"] = {action: "requested", mode: "explicit"}`
(`frontends/ink_bridge.py::workflow_plan`, `setdefault` so the auto-recommended path keeps its own
activation), and `WorkflowPlanner.classify()` must treat that as an execution request: an unrecognised
task shape resolves to `taskType: "general"` and the `general` template (execute-task -> verify-result),
never to the planner-only template. The planner-only shape is reserved for genuine plan-only requests
(`只规划` / `不要执行`) and for text that never opted in.

Why this is load-bearing: before it, `/workflow 写3个python demo并检验` classified as `planning` (the
coding keyword list only knows 实现/修复/开发/修改/implement/fix/code), produced a single `planner` job,
and the run reported `succeeded` having written nothing but PLAN.md. The `0/1 agents done` the user saw
was truthful — the plan contained no execution step at all. Details, per-run evidence and the real E2E:
`docs/20261010-ga-workflow-coding-task-planned-only.md`.

The plan is **model-authored, always**. `build_workflow_planner_from_env()` defaults to
`prompt_guided` (`LLMWorkflowPlanner`), and `GA_WORKFLOW_PLANNER_MODE=deterministic` is **removed** — it
raises instead of being silently ignored, because a fixed template set can only pick one of N shapes and
is therefore not a dynamic workflow (Step-Code has no plan template at all: the model writes the
orchestration script itself). `WorkflowPlanner` survives only as the internal fallback for a planner-model
error, and that path marks the run `fallback_deterministic` + `degraded`. `run.metadata["plannerMode"]` is
`prompt_guided` on the production path, and `unknown` when a directly-built draft declares no mode.

Test scripts and probes set `GA_WORKFLOW_PLANNER_MODE=real|prompt_guided` explicitly; that is not the
path a user gets from `./ga` + `/workflow`, so verify workflow behaviour on the default path before
claiming a fix.

## Verification writes are side effects, not artifact collisions

A verification job that *runs* the artifact under test will rewrite it. `verify` denies
`file_write`/`file_patch` but allows `execute` (`workflow_tool_profiles.WORKFLOW_TOOL_PROFILES["verify"]`),
and `code_run` is arbitrary Python that `workflow_path_acl.check_tool_call` only containment-checks — so
"read-only verifier" and "execute the artifact" are in tension by construction. This is **not** a prompt
defect: the verification role instruction actively tells the child to run the checks, and no runtime gate
can stop an `execute` tool from writing.

`workflow_scheduler._record_artifact_collisions()` therefore classifies a shared path by its number of
**producing** writers (`_is_verification_job()`: `permissionProfile == "verify"`, `toolProfile ==
"verify"`, or `role` in `{verification, review}`). More than one producer is still `artifactCollisions` +
an `artifact_path_collision` issue (a real overwrite: reported, never fatal). One producer plus a verifier
— or two verifiers — becomes `verificationSideEffects` + a `verification_side_effect` event and **must not
enter `workflowIssues` or degrade the run**. `frontends/ink_bridge.py` publishes it in the handoff so a
downstream reader does not mistake the re-generated deliverable for a second output. Real case:
`wf_9d097fb199824a1eb4bb37776ef8a2f1`, where a correct run was reported `degraded`. Regression tests:
`tests/test_workflow_scheduler.py::test_a_verifier_running_the_artifact_is_a_side_effect_not_a_collision`
and `::test_a_verification_only_path_is_never_reported_as_a_collision`.

## 中途补充调研的持久化流程（强制）

实施过程中发现新的上游源码事实、设计差异或复刻决策时，必须按以下顺序操作，不能直接改代码：

1. 先在本文件追加/更新一条「中途调研」记录：日期、触发点、源码坐标、结论、对 GA 的影响、待办。
2. 再写独立调研文档 `docs/YYYYMMDD-<主题>-调研补充.md`，展开证据、原文摘录、方案与验收。
3. 最后按该独立文档实施代码，并在实施记录中回链本文件与调研文档。

目的：让中途研究可持久化、可复核，避免只存在于会话上下文。

### 中途调研 2026-10-12：CC Workflow 工具入口 + G8/G9

- 触发：用户要求 GA 的 workflow 入口必须按 CC 设计，不能只停留在 `/workflow` 的 JSON plan 路径；除 B1 `budget` 外，CC 能对齐的能力都要对齐。
- CC 证据：
  - `m1569_0xdceb889.js @61628 inputSchema`：`script` / `scriptPath` / `name` / `args` / `resumeFromRunId` / `runId` / `remote`。
  - `@62439 validateInput`：feature gate、named-only、scriptPath 可读性、resume 校验；`Fy()` 解析 meta；`LKt()` 确定性闸门。
  - `@64578 checkPermissions`：默认 `ask`（"Review dynamic workflow before running"）。
  - `@65948 call`：编译/落盘后注册后台任务，立即返回 `{status:"async_launched", taskId, runId, workflowName, summary, transcriptDir, scriptPath}`；完成走 task notification。
  - `m1502_0xdb4a986.js xnr(e)`：有 skill 系统时只给短提示让模型 `load_skill` authoring reference；无 skill 系统时把约 17K 的 reference 全文塞进 tool description。GA 有 `load_skill` + `skills_runtime`，走 `e=true` 分支。
  - CC 子 agent 的 `disallowedTools` 包含 Workflow 工具（`Fd`），禁止递归起 workflow。
- GA 结论：
  - 新增模型可见的 `workflow` 工具；`/workflow <task>` 保留为兼容的 planner 路径，但 CC 风格主入口是工具。
  - 新增 `skills/workflow-authoring/SKILL.md`，并让 `get_default_skill_roots()` 发现仓库内 `skills/`。
  - tool description 用 CC 原文的本地化版 + “先 load_skill('workflow-authoring')”短提示。
  - bridge 增加 `workflow_launch_from_tool()`：绕过主 agent busy 检查（工具调用发生在主 agent 运行中），校验后创建 run、标记 running、追加 `workflow_started`、启动既有 `_run_workflow_runtime` watcher，立即返回 `async_launched`；完成后既有 `_queue_workflow_handoff` 把 `<workflow_handoff>` 排给主 agent。
  - `workflow` 加入 `ORCHESTRATION_TOOLS`，workflow 子 agent 不可见/不可调用。
- G8 结论：
  - CC 允许脚本内 `await workflow(nameOrRef, args)` 且只允许一层；二层拒绝。
  - GA 实现：worker 注入 `workflow()` RPC；runtime 新增 `workflow` RPC 处理，解析 named workflow / inline script / scriptPath；用嵌套 `WorkflowRuntime` 执行，`workflow_depth=1` 拒绝二层；嵌套 run 记录 `parentRunId`，父 run 停止时子 run 也停止。
  - 嵌套 run 与外层共享 workspace；为避免共用 runner 的 `job_id` 状态冲突，scheduler 增加 `job_id_prefix`，嵌套 run 用 `nested_<runId>_` 前缀。
- 不在范围：
  - B1 `budget`（用户点名暂缓）。
  - CC 云端会话 `isolation:"remote"` / `Ho()`：**【复刻时暂时不需要参考 claudecode 云端会话的实现】**。
- 详细文档：`docs/20261012-ga-dynamic-workflow-C3-G8-调研补充.md`。
- 实施记录：`docs/20261012-ga-dynamic-workflow-实施记录-C3-G8.md`（**已创建**）。
- 状态（2026-10-12）：C3/G8 **已完成**。`workflow` 工具入口 + `workflow-authoring` skill + 一层
  `workflow()` 嵌套均已落地；工具启动/嵌套 run 复用 `/workflows` 全部 UI 能力；子 agent 隔离
  与父 run 停止传播已实现；`NestedWorkflowRuntimeTest`(7) / `WorkflowToolEntryTest`(4) /
  `test_subagent_capabilities`(9) / `test_skills_runtime`(7) 全绿，合并 246 tests OK。B1 `budget` 仍暂缓。

### 中途调研 2026-10-12：CC Dynamic Workflow 剩余可对齐能力清单（H1~H28）

- 触发：用户提问“继续还有哪些可以抄 CC 的 dynamic workflow 的？”，要求把答案持久化后再实施。
- 结论：A1~A4 / B2 / C1~C2 / C3 / G8 已完成；对照 CC v2.1.283 全链路后，还有 20 项可对齐能力（编号 H1~H28）。
- 高价值四组：
  1. **通知/可观测性**（H17 `<usage>` / H18 `<failures>`+`<diagnostics>` / H19 外部 artifact 注入告警）——GA 的 `<workflow_handoff>` 缺这三块。
  2. **启动前批准**（H5）——CC 默认 `ask`（"Review dynamic workflow before running"），GA 的 `workflow` 工具直接跑。
  3. **单 agent 能力**（H7 per-agent `model`/`effort`）——GA 声明接受但 child runner 不用，且未进缓存键。
  4. **引擎一致性**（H12 嵌套共享并发/计数 / H13 并发上限 `min(16,CPU-2)` / H15 限流等待 reset）。
- 其余：H1 `ultracode` 关键词、H2 banner+忽略键、H3 feature gate、H4 `/effort ultracode`、H6 `stallMs`、H8 `agentType`（语义映射）、H9 `disallowedTools`（已等价）、H10 `bashCommandClamp`、H16 auto 安全分类器、H20 进度批量上报、H22 详情面板下钻+过滤、H23 单 agent skip/retry、H24 pause、H25 `/tasks`、H26 本地跨会话 adopt、H27 JSON Schema lint、H28 沙箱边界克隆器。
- 已覆盖无需再抄：H21 `agent_cached` 进度（GA 已有 `agent_cached` 事件）。
- 不复刻：云端 `remote_launched`/`isolation:"remote"`/`Ho()`（**【复刻时暂时不需要参考 claudecode 云端会话的实现】**）、工具层 `runId` run-op（本 build provider 不存在）、**H14 `budget`（用户点名暂缓 B1）**。
- 建议实施批次：① 可观测性(H17/H18/H19) → ② 入口(H5/H1/H2) → ③ 单 agent(H7) → ④ 引擎(H12/H13/H15) → ⑤ UI(H22/H23/H24/H26) → ⑥ 校验(H27/H28)。
- 详细文档：`docs/20261012-ga-dynamic-workflow-可继续对齐CC能力清单-调研补充.md`。
- 实施记录：待创建（按批次落地后补 `docs/20261012-ga-dynamic-workflow-实施记录-*.md`）。

### 中途调研 2026-10-12：去掉 JSON Planner，改走 CC 直写 JS 主路径

- 触发：用户实测 `/workflow` planner 路径两次产出坏 JSON（`planner_response_unparsable`，run `wf_44b2bc5ff8c9407782a64983d57a5712` → `failed` / `workflow_plan_rejected`）；用户决定**按 CC 改**：模型直写 JS，不再用 JSON 计划，也不兼容现有 GA planner。
- 结论：GA 有两条入口，**只有 `workflow` 工具（C3）是 CC 主路径**（模型直写 JS）；`/workflow <task>` + semantic activation 走的 **JSON planner 是 GA 特有脆点**，应整体移除。
- 结构性原因（换模型治不了根）：
  1. CC 从不要求模型产大 JSON —— 模型直接写 JS，只解析开头一小段纯字面量 `meta`（`Fy()`），其余交给 acorn/VM；
  2. GA LLM 层无结构化输出（全仓无 `response_format` / json mode）；
  3. `parse_json_object()` 太简陋（只去 ``` 围栏 + 找第一个 `{`，不修尾逗号/注释/截断）；
  4. 无截断检测（不看 `finish_reason`）；
  5. repair 重试看不到原文（`previousPlan={}`，`raw_outputs` 只在内存）；
  6. planner 另起一套 LLM 调用，和主 agent 脚本作者不是同一上下文。
- 目标架构（对齐 CC）：`opt-in（/workflow <task> | 显式请求 | activation 命中）→ 注入 opt-in 提醒 → 主 agent 直写 JS → 调 workflow 工具 → 后台跑`。**opt-in 只负责"告知模型可用 workflow"，脚本作者永远是主 agent。**
- 删除：`workflow_planner.py` 的 planner/plan/render 部分（`WorkflowPlanner`/`LLMWorkflowPlanner`/`NativeWorkflowPlannerClient`/`validate_workflow_plan`/`render_workflow_plan`/`_planner_prompt`/`parse_json_object` 等）、`workflow_controller.create_planned_run`、`ink_bridge.workflow_plan*`/`_make_workflow_planner`/`_submit_recommended_workflow`、UI 的 `workflow_plan` 命令、`workflow_planned`/`workflow_plan_rejected` 事件。
- 保留（不要删）：`validate_rendered_workflow_script` + `_workflow_script_contract_preflight`（C3/嵌套在用，建议迁到独立模块）、`workflow_script_contract.js`、`workflow_determinism.py`、`workflow_launch_from_tool`、`create_draft`/`workflow_draft`、runtime/scheduler/store、`/workflows` UI、`workflow_activation.py`、`skills/workflow-authoring/SKILL.md`、权限/路径收口。
- 实施顺序（不可颠倒）：批次0 迁出脚本契约 → 批次1 新入口（`submit_workflow_opt_in` + 提醒）→ 批次2 删 planner → 批次3 收尾（skill/文档/全量测试）。
- 验收：`/workflow <task>` 不再产生 `workflow_planned`/`plan.md`；主 agent 收到提醒并能调 `workflow` 工具（`launchedBy == "workflow_tool"`）；弱模型（deepseek-flash）实测不再因 JSON 解析失败而 failed。
- 云端不复刻：**【复刻时暂时不需要参考 claudecode 云端会话的实现】**；B1 `budget` 仍暂缓。
- 详细方案：`docs/20261012-ga-dynamic-workflow-直写JS入口改造方案.md`。
- **状态（2026-10-12）：已全部实施完成。** 批次 0~3 全部落地：脚本契约迁到
  `workflow_script_validation.py`；`submit()` 注入 `[WORKFLOW OPT-IN]` 且不再 hijack；
  `workflow_planner.py` / `create_planned_run` / `ink_bridge.workflow_plan*` / UI 的
  `workflow_plan` 命令 / `workflow_planned`+`workflow_plan_rejected` 事件全部删除；
  `/workflow on|off` 作为 session opt-in 保留，`/workflows` 列表/详情/控制保留。
  验证：`python -m unittest discover -s tests` **1324 OK (skipped=3)**；
  `npm --prefix frontends/ink-ui run typecheck` OK；`npm test` **405 pass / 0 fail**。
- 实施记录：`docs/20261012-ga-dynamic-workflow-实施记录-直写JS入口.md`。
### 中途调研 2026-10-12：GA Ink workflow 内联 fence 关闭导致 Turn 3/4 字面星号

- 触发：用户实测 `/workflow` 启动后，Ink UI 中 Turn 1/2 的 `LLM Running` 正常加粗，Turn 3/4 变成字面量 `**LLM Running ...**`，并与 `🔨 Tool:` 行混在同一段。
- 证据：
  - `temp/sessions/session_a0df2e7356e54111b99aa8eccf4ada56.jsonl` 第 2 条 `turn` 的 `assistant_text` 中，`workflow` 工具结果被包在 5 反引号 status fence 里，但结束 fence 被拼到了正文行尾：
    `[Workflow] launched ... use /workflows to monitor.`````
  - `ga.py:767` 的 `do_workflow()` 输出没有结尾 `\n`；`agent_loop.py::_run_single_tool()` 随后直接追加 `"`````\n"`，于是形成上述畸形 fence。
  - 用真实 transcript 按 16 字符 delta 重放 `applyBridgeEvent`：Turn 2 为 bold，Turn 3/4 为 code/gray 且 `**` 原样显示；只在正文和结束 fence 之间插入换行后，Turn 3/4 恢复 bold。
- 结论：这是服务端 fence 生成缺陷，不是 `marked` 解析器随机故障。stream-commit 可以从 ` ````` ` 这一行开始新 segment；`formatAssistantText()` 把 4/5 反引号压成 3 后，Markdown 只看到 opener、看不到独立闭合行，后续 Turn 和 Tool 行全部落入 code block。
- 修复：
  - `agent_loop.py` 在关闭工具输出 fence 前保证最后一个 chunk 以换行结尾（顺序工具和并行工具路径都覆盖）。
  - `ga.py::do_workflow()` 的 `[Workflow]` / `[Warn]` 输出显式补 `\n`。
  - `frontends/ink-ui/src/messageFormat.ts` 兼容历史 transcript 中已经存在的 `[Workflow] ...````` ` / `[Warn] ...````` ` 内联关闭形式。
  - `frontends/ink-ui/src/streamCommit.ts` 的 fence 状态机改为按 Markdown 语义匹配 opener/closer 长度，并把 legacy 内联关闭视为 closer；否则它会把嵌套的 ````text` 误判为关闭 5 反引号块，从而在 `[Info]` 的真正 closer 前切段，让下一段以 closer 开头并吞掉 Turn 4。
- 详细文档：`docs/20261012-ga-ink-workflow-fence-inline-close-fix.md`。
- 状态（2026-10-12）：已实施；回归覆盖 Python 工具输出 fence、Ink transcript 渲染与 stream-commit fence 状态机。验证：`python -m unittest discover -s tests` 1325 OK（skipped=3），`npm --prefix frontends/ink-ui test` 407 pass，typecheck OK。
