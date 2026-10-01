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

## 测试指南

测试使用标准库 `unittest`。测试文件命名为 `test_*.py`，放在 `tests/`。新增前端或适配器行为时，应 stub 外部服务，避免依赖真实 API 凭据。提交前运行 `python -m unittest discover -s tests`，修复 bug 时补充聚焦的回归测试。

### Ink UI 测试：优先「无截图」的程序化手段

Ink UI 测试位于 `frontends/ink-ui/src/*.test.ts`，用 Node test runner + tsx 运行。调试 Ink UI bug（光标/IME、布局、换行、重复渲染、流式）时，优先写程序化回归测试而非靠人眼看截图。核心思想：终端 UI 的 bug 最终都表现为「写进 stdout 的字节」或「布局出来的行列几何」，二者都是确定性、可被机器断言的。按现象选手段——相对光标算术用虚拟终端追踪器（`cursorParkModel.ts`），控制序列用字节级 ANSI 断言（`match` + `doesNotMatch` + `indexOf` 切片查顺序），布局用内存终端（`CaptureWriteStream`/`FakeReadStream` + `render(<App/>, {debug:true})`）配帧几何解析器，布局/分区决策抽成纯函数单测，CJK/emoji 换行一律用 `string-width`（绝不用 `.length`），重复渲染用唯一探针文本计数。完整方法论、决策表和可复用工具见 `docs/ga_ink_ui_testing_playbook_2026-07-16.md`，新增或调试 Ink UI 测试前先读它。唯一盲区：跨终端行为（如 IME 锚定可见原生光标）只能靠真机截图发现，之后仍应尽量把它降维回字节级契约测试。

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
