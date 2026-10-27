# Repository Guidelines

## 项目结构与模块组织

GenericAgent 是一个 Python 项目。核心运行时实现位于 `genericagent/`，按 `core/`、`llm/`、`mcp/`、`agents/`、`workflow/`、`web/`、`media/`、`resources/` 分域。仓库根目录只保留本地配置 `mykey.py`，以及 `ga`、`ga.cmd` 等薄入口；`launch.pyw`、`hub.pyw` 仅作为 deprecated 兼容文件保留，不属于发布面。可安装的 CLI 门面仍在 `ga_cli/`，`ga` 命令入口映射到 `ga_cli.cli:main`。受支持的 Ink 前端位于 `frontends/ink/`；Textual、Qt、Web、桌面宠物和聊天/机器人适配器仍在 `frontends/` 中保留，但均已 deprecated。图片、皮肤和静态资源位于 `frontends/skins/` 与 `assets/`。长期记忆、SOP 和辅助工具位于 `memory/`，反射与自主运行辅助逻辑位于 `reflect/`，可选集成放在 `plugins/`。测试统一放在 `tests/`。

## 构建、测试与本地运行

- `python -m pip install -e .`：安装核心包、`ga` 命令以及 `ga` / `ga ink` 所需依赖。
- `ga` / `ga ink`：受支持的 React/Ink 终端入口；`ga cli` 为轻量排障入口。
- `npm install --prefix frontends/ink/ui`：安装 Ink UI 的 npm 依赖。
- `python -m unittest discover -s tests`：运行当前测试套件。

Textual、Qt、Streamlit、pywebview、桌面宠物、Hub、聊天机器人等旧入口已 deprecated：源码保留但不维护，也不再进入默认依赖。详见 `docs/20261022-GA入口收敛与旧前端退役说明.md`。

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

Ink UI 测试位于 `frontends/ink/ui/src/*.test.ts`，用 Node test runner + tsx 运行。调试 Ink UI bug（光标/IME、布局、换行、重复渲染、流式）时，优先写程序化回归测试而非靠人眼看截图。核心思想：终端 UI 的 bug 最终都表现为「写进 stdout 的字节」或「布局出来的行列几何」，二者都是确定性、可被机器断言的。按现象选手段——相对光标算术用虚拟终端追踪器（`cursorParkModel.ts`），控制序列用字节级 ANSI 断言（`match` + `doesNotMatch` + `indexOf` 切片查顺序），布局用内存终端（`CaptureWriteStream`/`FakeReadStream` + `render(<App/>, {debug:true})`）配帧几何解析器，布局/分区决策抽成纯函数单测，CJK/emoji 换行一律用 `string-width`（绝不用 `.length`），重复渲染用唯一探针文本计数。完整方法论、决策表和可复用工具见 `docs/ga_ink_ui_testing_playbook_2026-07-16.md`，新增或调试 Ink UI 测试前先读它。唯一盲区：跨终端行为（如 IME 锚定可见原生光标）只能靠真机截图发现，之后仍应尽量把它降维回字节级契约测试。

### GA ink 真实 LLM API e2e 自测：先读 SOP

凡是「用 GA ink + 真实 LLM API 做端到端自测」的需求（反复发生），**先读**
`docs/20261015-GA-ink-真实LLM-e2e-自测SOP.md`。核心：真实自测**不靠固定期望输出**判定，
靠**证据（事件流 / 日志 / 产物 / 真实 UI 帧）+ 不变量断言**。分三层：

- **L1 事件级**：`python tests/real_ink_selftest.py`（结构检查，秒级）；真实回合加 `GA_RUN_REAL_API_E2E=1`（走默认模型即可），证据包落 `temp/ink-selftest/<ts>/`。
- **L2 UI 版面级**：`cd frontends/ink/ui && npx tsx scripts/real_ink_team_panel_e2e.ts`（真实 App + 伪 TTY，不需 API，秒级）。
- **L2 agent team 全链路**：`cd frontends/ink/ui && npx tsx scripts/real_ink_team_swarm_e2e.ts`（真实 App + 伪 TTY，从输入框输入硬任务 + 真实 API + 默认模型；断言 team_mode teammate / 共享任务板 / 产物）。
- **L3 交互式手动**：`./ga`（人眼 + 会话产物）。

- **2026-10-16 真实自测记录（MCP 结果预算）**：用 FastMCP 真实 stdio server 声明
  `meta={"maxResultSizeChars": 200}`，返回 35,543 chars；默认 `deepseek-flash` 经真实 bridge 调用后，
  `temp/model_responses/*.txt` 中模型收到的是 `<persisted-output>` + `artifactPath` + `originalChars`，
  完整结果落在 `temp/tool-results/`。证据包：`temp/ink-selftest/20261016-152336/`。
  关键测试坑：自定义 stdio server 冷启动约 6 秒，首轮默认 discovery budget（约 2 秒）下模型看不到工具；
  真实自测需设 `GA_MCP_DISCOVERY_BUDGET=30`。SOP §3.4 已固化完整步骤。

- **2026-10-16 真实 workflow E2E（17K authoring prompt 改造后）**：默认 `deepseek-flash` 完成
  “3 路并行版本探测 -> 1 路串行综合 -> `file_write` 落盘 -> `file_read` 验证”，runId
  `wf_9d31a557da1b42f684c999e3d969dc02`，4/4 jobs succeeded，脚本真实包含 `parallel()`。
  `load_skill` 只返回短元数据，正文通过独立 `<skill_content>` 注入，references 按需读取；
  普通工具 `code_run`/`structured_output`/`file_write`/`file_read` 均真实成功。证据：
  `temp/workflow-selftest/20261016-153345/`；报告：`docs/20261016-GA-workflow-17k-prompt-真实e2e-并行串行.md`。

注意：重型全链路脚本 `real_ink_ui_user_input_e2e.ts` **为 luna profile 硬编码**，用默认 `deepseek-flash` 跑会在子代理环节失败（`subagent child did not reach succeeded`）——默认模型下不要用它判定。

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

回归测试 `frontends/ink/ui/src/resumeTranscriptDuplication.test.ts`（屏幕级断言用
`src/resizeReflowModel.ts`）。复盘见 `docs/20261010-ink-resume-duplication-and-list-latency.md`。

### `/resume` 列表：只走轻量扫描 + `(mtime, size)` 缓存

`frontends/terminal/continue_cmd.py::list_sessions()` 是 `/resume` 的列表入口，Ink bridge 一次 `/resume`
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

不要提交真实 API key 或本地密钥。配置示例应维护在 `genericagent/llm/templates/mykey_template.py`、`genericagent/llm/templates/mykey_template_en.py` 或 `assets/configure_mykey.py`。本地生成状态、日志和凭据应保持在版本控制之外。

严格禁止执行、生成、写入或持久化任何包含可疑 base64 载荷、公益站/公益 token 广告、`token自由` 类推广文本、弹窗广告、自启动项、计划任务、注册表 Run 项、VBS/PowerShell 注入脚本或其他恶意/入侵代码的内容。遇到此类内容时，只能进行只读检查、解码说明、定位和删除，禁止运行其载荷或帮助其建立持久化。

## 参考实现：Claude Code 源码

Claude Code 本地源码位于 `D:\git_codes\claude-reviews-claude\claude-code-fork\src`。它是 React + Ink 终端 UI，并在 `src/ink` 下 fork 了 Ink。处理 GA Ink UI 的光标 / IME / 布局问题时，这是最高价值的参考——它解决了 GA 遇到的同一个原生光标/IME 问题。关键文件：`src/ink/components/CursorDeclarationContext.ts`、`src/ink/hooks/use-declared-cursor.ts`（frame 声明 cursor 模型）、`src/ink/frame.ts`、`src/ink/ink.tsx`、`src/ink/log-update.ts`（单一 stdout writer：diff 帧后统一放 cursor）。GA 侧分析见 `docs/superpowers/specs/2026-07-15-ga-self-managed-terminal-design.md` 与 `docs/ga_claude_code_cursor_handling_2026-07-16.md`。

GA Ink UI 的 IME/光标 bug，权威根因文档是 `docs/ga_ui_ime_visible_native_cursor_root_cause_2026-07-16.md`：Windows Terminal 的输入法候选框锚定在**可见**的原生光标上（DECTCEM `\x1b[?25h`），因此 cursor-park 包裹流（`frontends/ink/ui/src/stdoutCursorPark.ts`）必须在光标停到 caret 后 SHOW、在下一帧写入前 HIDE。2026-07-14/07-15 早期诊断得出的「保持原生光标隐藏、只用反显块」结论是错的，已在该文档中修正。

## 参考实现：Claude Code 2.1.303 hooks

CC 2.1.303 的 hooks 全链路调研与 GA 落地设计已经落盘，后续实现 hooks 前先读：

- CC 源码核验与本机真实 Stop beep 证据：`D:/git_codes/claudecode283_hack/claudecode303/extracted/20261020-hooks入口分析.md`（重点 §15）。
- GA 的配置教程（逐层路径、优先级、验证与排障）：`docs/20261021-GA-hooks配置指南.md`。
- GA 的配置、协议、接入点、阶段计划和测试矩阵：`docs/20261020-GA-hooks设计与实现方案.md`。

必须区分语义：CC 的 `Stop` 是**一个用户请求最终结束**，不是 GA `agent_runner_loop` 的每个内部 `Turn N`。GA 的 Stop beep 必须挂到 `GenericAgent.run()` 的用户回合终态（成功/失败/abort 各一次），不能挂 `turn_end_callback()`，否则复杂任务会重复响铃。

当前落地状态（2026-10-20）：本地 hooks runtime 已实现 `command`、`prompt`、`agent`、`http`、`mcp_tool` 五类 handler；33 个事件全部进入配置合同，其中 20 个已接线、13 个为 contract-only。`Stop` 绑定用户回合终态，`SubagentStop` 与 workflow job 作用域已分流；Ink 提供 `/hooks` 只读面板。配置使用 `.genericagent/settings*.json`，默认不读取 `.claude/`。exec form 优先（`command` + `args`，不拼 shell）；PreToolUse 修改 `updatedInput` 后必须重新跑 capability / permission / PathACL，hook 不能放宽任何硬安全边界。

`PermissionRequest` 已进入 GA 的 ask 阶段接入：先执行 hook，`allow` 可跳过用户审批但必须先应用嵌套 `decision.updatedInput` 并重新跑 PathACL；`deny` 必须发出 `PermissionDenied` 且不执行工具；hook 没有给出 `allow/deny` 时才走原有用户审批。CC 303 的证据与 GA 映射见 `D:/git_codes/claudecode283_hack/claudecode303/extracted/20261020-hooks入口分析.md` §12；实现顺序见 `docs/20261020-GA-hooks设计与实现方案.md`。

实施记录与剩余限制：`docs/20261020-GA-hooks实施记录.md`（本地 async/asyncRewake 与 HTTP URL/env policy 已接入；完整 per-event schema、async 进度批处理、会话退出时主动取消后台 hook、低频事件接线仍待补）。

云端边界：**【复刻 hooks 时暂时不需要参考 Claude Code 云端会话的实现】**；不做 cloud hook 策略、device forwarding 或云端会话同步。

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

1. **实时进度**：`frontends/ink/bridge.py::_run_workflow_runtime` 必须边跑边推 `workflow_progress`（`_watch_workflow_progress()` 轮询 durable 的 `workflow-progress.json`）。只在 runtime 返回后推一次，UI 会全程停在 `0/0 agents done`。
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


## 长文本写入边界（Windows / Codex 实测）

- Windows `CreateProcessW` 命令行硬上限为 **32767 UTF-16 code units**；本机 Git Bash `bash -c` 脚本体实测 32735 OK、32736 报 `WinError 206`。
- 但 Codex tool-call 参数可能远早于 OS 上限被截断；实际曾出现约 3.1 万字符 / 4 万 UTF-8 字节的 heredoc 只写入前半段，末尾 `EOF` 未到达。
- **禁止把整篇长文塞进一次 `exec_command`。** 中文正文每段约 2K-4K 字符；ASCII/代码每段不超过 8KB；单条命令目标总长 ≤ 8KB，绝不超过 12KB。
- 第一段用 `cat > file <<'MDOC_20261021_A7F3'`，后续段用 `cat >> file <<'MDOC_20261021_A7F3'`；每次使用**本次唯一的 delimiter**，正文结束后用完全相同的单独一行闭合。
- **禁止在包裹长文时使用裸 `EOF`。** 引号 heredoc（`<<'EOF'`）只阻止变量展开，不能阻止正文中单独一行的 `EOF` 提前结束 heredoc；写包含 heredoc 示例的 Markdown 时，外层 delimiter 必须不同于内层示例 delimiter。
- 按 Markdown 章节边界拆分，不拆 code fence、JSON object、表格行或源码片段的中间；每段独立闭合 heredoc。
- 看到 `here-document ... delimited by end-of-file (wanted ...)` 时，先判断是命令截断，还是正文包含同名 delimiter 行；检查文件尾部与 delimiter 位置，从最后完整章节继续，换新的唯一 delimiter，并缩小下一段；不得原样重试同一大命令。
- 完成后验证 UTF-8 可解码、无 BOM、无 CRLF，并确认最后一段的尾部标记/章节确实落盘。
- 机制与证据见 `D:/git_codes/claudecode283_hack/docs/20261021-Codex长文本写入边界与AGENTS约束.md`；探针见 `D:/git_codes/claudecode283_hack/docs/probes/20261021-codex-command-length-probe.py`。

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

## Workflow contract / Agent Team duplicate-spawn research (2026-10-19)

Before changing workflow acceptance contracts or Agent Team spawn/task-board behavior, read:

- `docs/20261019-CC-workflow-contract与agent-team防重复spawn调研.md` — CC 2.1.283 source evidence and the exact GA failure chain.
- `docs/20261019-GA-workflow-contract与agent-team防重复spawn实施方案.md` — implementation order, file scope, tests and acceptance criteria.
- 实施状态（2026-10-19）：P0/P1 代码、111 个聚焦 Python 测试、真实 `deepseek-flash` Workflow + Agent Team E2E 均已通过；证据与运行号见实施方案 §11。

Load-bearing conclusions:

1. CC does **not** define GA's `executionContract.requiredToolEvidence`. CC's primary quality path is `agent(..., {schema})` → `StructuredOutput` tool-call validation → adversarial/perspective-diverse verification. GA's `executionContract` remains an optional host extension; E2E must assert actual child tool evidence when the prompt did not explicitly request that extension.
2. Agent Team stability must not rely on prompt text alone. CC uses unique teammate identity reservation, a shared task board, automatic owner assignment on `in_progress`, `task_assignment` notifications, and team-context injection. GA additionally fail-closes unbound `team_mode` spawns against a claimable board task, because GA's weaker models were observed creating a second wave after a read-only role failure.
3. `turn_status=completed` is not proof that the assigned task succeeded. Do not auto-complete a teammate-owned task from that signal; require `task_update(status=completed)` and let `close_agent` reclaim unfinished work as `pending`.
4. Team coordination tools (`task_*`, `send_message`, `team_status`) are control-plane tools, not workspace mutations; they must remain available to read-only teammates. Write/execute intent must be checked against the role's permission profile before spawn.
5. Never reintroduce a JSON planner for this path. Workflow entry stays direct JavaScript; task-board/spawn arguments are ordinary tool-call fields, not a second planning DSL.

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

## Workflow tool access: full connected surface, role/profile are hints

A workflow child receives the **complete connected tool schema**. `role` and
`toolProfile` remain useful planning hints and audit metadata, but they do not
pre-deny tools and do not narrow the schema. Inferring hard capability boundaries
from model-authored roles produced contradictory runs (a `review` + `planner` job
was resolved as `verify`, granted `code_run`, while its advertised schema came
from a different profile), so the unreliable rail was removed.

Rules that must not regress:

1. **Do not derive a restrictive `permissionProfile` from `role`.** The scheduler
   keeps an explicitly configured run profile; otherwise the child inherits the
   caller's current permissions. `role` never silently changes `read_only` /
   `verify`.
2. **Do not filter workflow-child tools by `toolProfile`.** `_load_tools_schema`
   returns the discovered schema unchanged (plus `structured_output` for a schema
   job). Keep the declared profile in audit metadata, not as an enforcement gate.
3. **Path policy defaults to unrestricted; explicit workspace policy remains enforced.** Full tool access also means arbitrary filesystem targets by default: `workspacePath` is only the child cwd and artifact root. When a job sets `pathPolicy: "workspace"`, `workflow/path_acl.py` constrains `file_read`, `file_write`, `file_patch`, and `code_run` to that root. Do not silently make `workspacePath` a hard boundary again.
4. **Schema validation and turn-budget failures stay distinct.** If
   `agent_runner_loop` returns `{"result": "MAX_TURNS_EXCEEDED"}`, the child must
   fail with `code: max_turns_exceeded`; it must never fall through to
   `_build_success_payload` and be reported as `schema_validation_failed`.
5. **Capability discovery is reported, not used to pre-reject.** MCP discovery
   failures remain visible in `capabilityCoverage` / `unavailableCapabilities`;
   the agent chooses among the tools that actually connected.

Evidence: `docs/20261009-workflow-tool-profile-capability-boundary.md` §5.
Regression tests: `tests/test_workflow_child_agent.py`,
`tests/test_workflow_scheduler.py`, `tests/test_workflow_permissions.py`.

## Workflow artifact observation and run workspaces

Workflow artifact observation is **filesystem-based, never tool-name-based**. `workflow_child_agent` snapshots the child workspace before and after each job (`workflow_workspace.snapshot_workspace`/`diff_workspace`) and reports `tool_summary["writtenPaths"]`; the scheduler turns that into `observedArtifacts`/`observedMutations`. Do not reintroduce `file_write`/`file_patch` allowlists — `code_run` (the normal DOCX/HTML path) and any future writer tool are covered by the diff by construction. Comparison logic for "did this job produce something" belongs in `docs/20261009-ga-workflow-stability-stepcode-borrow.md` §13.

Ownership travels with the handoff, never as a tool-name inference. `workspace_writes_with_writer` records the writer once, at diff time; `workflow_store.build_artifact_ownership_index(run)` unions it into a run-level `path -> writer(s)` index that `_record_observed_artifacts()` persists to `run.metadata["artifactOwnership"]` and the progress snapshot. The bridge surfaces it as top-level `artifactOwnership` plus per-stage `intermediateResults[].artifactOwners`, and `artifactCollisions` (same path written twice, last write wins) is reported, never failed. A child process keeps no in-process `handoff` dict — its handoff is written to `workflow-handoffs/<job>.json` — so any consumer must read ownership from run metadata, not from `job.metadata["handoff"]`. Keep `artifactOwners`/`artifactOwnership` in the handoff size-compaction allowlist, or downstream readers lose the ability to tell whose output they are reading. Evidence: `docs/20261009-ga-workflow-stability-stepcode-borrow.md` §15.

`observedArtifacts` entries are `{"path", "writer"}` (the writer is the job label, falling back to `jobId`), recorded at diff time because that is the only moment that knows both facts; `workspace_writes_with_writer()` in `workflow_workspace.py` also accepts legacy bare-path lists so old run state still loads. A path written by more than one job is recorded as `artifactCollisions` plus an `artifact_path_collision` issue and an `artifact_collision` event — reported, not fatal, because a deliberate overwrite is legal.


Every planned/draft/resumed run owns an isolated output workspace at `<base>/workflow-runs/<runId>/`. Two roots must not be confused: `workspacePath` (run-local, where `artifactRefs` resolve) and `workspaceBasePath` (the shared base, where run-internal `resultRef`/`transcriptRef` under `temp/sessions/...` resolve). The Ink bridge resolves artifacts via `_run_workspace_root()` and run-internal refs via `_run_artifact_base_root()`; mixing them makes a correct artifact look missing. `_cache_key()["workspacePathHash"]` hashes the base root so resume still hits cached prefixes.

Every ref published to a reader must open under a root that reader is told about. `resultRef`/`transcriptRef` are relative to the run's internal artifact directory, so the host materialises each job's durable result into the run workspace at `workflow-handoffs/result-<jobId>.json` (`workflow_scheduler._materialize_result_copy`) and every reader-facing surface publishes that workspace-relative copy as `resultRef`, with `resultPath` absolute and `runInternalResultRef` kept for audit: `workflow-progress.json`, the workspace handoff file `workflow-handoffs/<job>.json`, and the post-run handoff in `frontends/ink/bridge.py`. `workflow-handoffs/` is in `HOST_OWNED_WORKSPACE_DIRS`, so writing it while other children run cannot be misattributed to a child diff. A reader that joins the bare internal ref onto `workspacePath` builds a real path that does not exist; that is not a Windows path-separator problem, and it is what made a correct run report its durable result as missing (run `wf_92ad839265dc48f3ac56e5cb86f6e780`). This is not a Windows path-separator issue: a bare `resultRef: agents/agent_1/result.json` joined onto `workspacePath` yields a real, well-formed path that simply does not exist, which is how a correct run reported its durable result as missing.

Path policy is host-side and deterministic. `workflow_child_agent._path_policy(job)` defaults to `unrestricted` and only passes `workspace_root` to `GenericAgentHandler` for explicit `pathPolicy: "workspace"`. In that opt-in mode, `workflow_path_acl.check_tool_call(workspace_root, tool_name, args)` is the single source of truth for "may this path be read/written/executed"; `GenericAgentHandler.dispatch` calls it before any workflow child tool executes and returns a `path_acl` error on violation. Capability (permission profile) decides *whether* a tool may be used; the path policy decides *which paths* it may touch. Declared artifacts are still integrity-checked at run close: a non-optional artifact that is empty (`empty_artifact`) or shrank below the largest size the host observed a child write (`artifact_altered_after_write`) fails the run. Keep `expand_file_refs(..., workspace_root=...)` containment for explicit workspace-policy jobs.

Handoff summaries are bounded by a structure-aware helper (`workflow_child_agent.bounded_structured_summary`, shared with `workflow_scheduler._compact_handoff_summary`): a structured answer is projected into a smaller **valid** JSON value carrying `_truncated`/`_note`, and prose is cut on a line boundary with an explicit `…[truncated by the host; full value: <ref>]` marker. An already-truncated fragment (unbalanced brackets) is flagged the same way instead of passing as data. Never reintroduce a bare `[:N]` cut — that regression made a synthesis child read half of `sources[0]` and report the upstream sources as unrecoverable.

A dependent child cannot read `resultRef`/`transcriptRef`: those are run-internal audit refs outside its path limit. `workflow_scheduler._materialize_upstream_result()` copies each upstream `result.json` into the run workspace as `workflow-handoffs/upstream-<jobId>.json` before the child starts and hands it over as `upstreamResultPath`, and `workflow_child_agent._build_prompt()` states both roots explicitly. Do not "fix" a missing-upstream report by widening the child ACL or by inlining the payload into the prompt.

`workflow_workspace.HOST_OWNED_WORKSPACE_DIRS` (`__pycache__`, `.git`, `workflow-handoffs`) is excluded from `snapshot_workspace`, so host control-plane writes are never attributed to a running child.

## MCP startup resilience: slow is not failed

MCP connection is governed by a per-server budget that must not be tight enough to read a slow handshake as a failure. `_MCP_DISCOVERY_TIMEOUT_DEFAULT` is 30s (mirroring Codex `DEFAULT_STARTUP_TIMEOUT` in `codex-mcp/src/rmcp_client.rs`), overridable per server via `startup_timeout_sec` or globally via `GA_MCP_DISCOVERY_TIMEOUT`. Remote (HTTP/SSE) transports get bounded initialize retries inside that same budget (`_MCP_REMOTE_CONNECT_ATTEMPTS = 3`, backoff `_MCP_REMOTE_CONNECT_RETRY_DELAYS = (0.25, 1.0)`), retrying only retryable errors per `_is_retryable_connect_error`; stdio is never retried (a dead local process will die again). The connect path is three-valued: `pending` = never attempted, `connecting` = attempt/retry in flight, `failed` = retries exhausted. Only `failed` renders as `✕`; `connecting` renders as `◌` (`frontends/ink/ui/src/mcpPanel.ts`).

Invariants: the background discovery pass (`start_background_discovery`, `retry_failed=True` by default) is the self-heal channel and must retry previously failed servers, while the synchronous `discover_mcp_tools`/`discover_mcp_tools_cached` keep `retry_failed=False` so they never hammer a server known to be down. Local stdio servers connect through a bounded pool (`GA_MCP_LOCAL_CONNECT_CONCURRENCY`, default 4), not a `Semaphore(1)`. stdio stderr logs are size-capped (`GA_MCP_STDERR_LOG_MAX_BYTES`, default 256 KiB) so they cannot grow unbounded. Root cause, Codex comparison, and measurements: `docs/20261010-ga-mcp-startup-connect-failures.md`. Regression tests: `tests/test_mcp_runtime.py::McpStartupResilienceTest`.

## MCP cold start is a background observation, never a gate

Startup must not block the first turn on MCP readiness, and the UI must not read as if it does. Measured on this machine (2026-10-10): bridge `ready` at ~2.0s, first token **0.16-0.19s after submit**, while MCP discovery only settled at 9.6-14.8s (worst server `context7`). The per-server budget is 30s, so a slow remote endpoint can legitimately hold `loading` for 30s+; the first turn still answers because `discover_mcp_tools_cached_fast()` returns a cached catalog immediately and otherwise waits at most `GA_MCP_DISCOVERY_BUDGET` (default 2s).

Invariants:

1. Never add a startup-time inline MCP warmup. Both a synchronous prewarm in `main()` and a background-thread prewarm pushed `ready` from 1.9s to ~3.7s (import cost + GIL contention) and were reverted. `ready` must stay ~2s.
2. `npx` stdio servers get `--prefer-offline` injected by `mcp_runtime._prefer_offline_args()` (host-side, because `mcp.json` is gitignored user config). The registry round-trip costs 4-8s per spawn even when the package is cached. The flag still installs a genuinely missing package.
3. The bridge's failed-server self-heal (`frontends/ink/bridge.py::_watch_mcp_status`) reconnects **only the servers in the `failed` list** via `reconnect_mcp_server`. Do not switch it back to a full `start_background_discovery(retry_failed=True)` pass: one flaky server used to double the `initializing` window for every server.
4. Startup rows say the truth: `MCP connecting in the background · N/M connected · X tools · you can type now` (`frontends/ink/ui/src/mcpPanel.ts`). Input is never disabled by MCP state, so the copy must not imply otherwise.

Codex does the same thing (waits only for `required = true` servers, skips the wait when `allows_cached_startup()` has a cached catalog, 30s `DEFAULT_STARTUP_TIMEOUT`, per-server TUI status) — see `codex-rs/codex-mcp/src/connection_manager/required.rs` and `codex-rs/tui/src/chatwidget/mcp_startup.rs`. Measurements, per-server timings and probes: `docs/20261010-ga-ink-mcp-cold-start-latency.md`; probes live in `frontends/ink/ui/scripts/_probe_mcp_startup.ts` and `_probe_mcp_firstturn.ts`.

## Workflow activation: an explicit /workflow request must execute

`/workflow <task>` is an explicit user opt-in, but the ink UI strips the prefix before planning
(`frontends/ink/ui/src/inputController.ts`), so the planner cannot see it in the task text. The bridge
carries it as `context["activation"] = {action: "requested", mode: "explicit"}`
(`frontends/ink/bridge.py::workflow_plan`, `setdefault` so the auto-recommended path keeps its own
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
enter `workflowIssues` or degrade the run**. `frontends/ink/bridge.py` publishes it in the handoff so a
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
### 待办 2026-10-16：GA ink「执行过程 UI」对标 CC（折叠 / 图标 / 排版）

- **用户诉求（2026-10-15 提出，次日执行）**：GA ink 执行过程的 UI——**没有折叠**（工具调用/输出/思考过程收不起来），
  且**图标审美偏低**（emoji / 粗制 glyph）。目标：**对标 Claude Code 源码的 UI 部分**，让执行过程更美观、优雅。
- **第一步（明天先做）**：读 CC 源码 UI 相关模块，提炼「折叠/展开 + 图标字形 + 层级缩进 + 颜色/暗色 + spinner 文案」规则，
  先写调研+设计文档 `docs/20261016-CC-UI-对标调研.md`，再据此改 GA ink（走本文件「中途补充调研的持久化流程」）。
- **CC 源码线索（今日粗筛，仅起点，待细读）**：
  - `m1277_0xd55e81a.js`（2.4KB）：工具结果行的 **`⎿` 前缀 + 缩进 + 「offscreen」锁定高度**（折叠/展开雏形）。
  - `m0696_0xcf4efd9.js`（11KB）：**spinner 动词表**（`spinnerVerbs`，Accomplishing/Ruminating…，replace/append 模式）。
  - 含 `Ctrl+O` / `to expand` 的模块：`m0052`、`m0340`、`m0560`、`m1369`、`m1412`、`m1428`、`m1437`、`m1444`、`m1478`(1.5MB)。
  - 含转义字形（`⎿ ⎿` / `⏺ ⏺` / `✻ ✻`）的模块：`m0709`、`m1277`、`m1437`、`m1442`、`m1444`、`m1448`、`m1478`、`m1919`、`m0326`、`m0052`。
  - ⚠️ CC 的渲染组件多在打包 chunk（`B:/~BUN/root/chunk-*.js`，bytecode），明文模块里只是**引用**；细节可能要在
    `claude.exe` 上 `grep -a` 搜 chunk 名/字符串（同 agent-team 调研做法）。
- **GA 落点**：`frontends/ink/ui/src/`（`App.tsx` 的 `computeLayoutMetrics`/面板、转录区渲染、工具调用块）。
- **交付物**：① 调研+设计文档；② GA ink 改动（折叠/展开 + 图标/排版对齐 CC）；③ 无截图式渲染断言测试（见本文件「Ink UI 测试」）。
- **约束**：CC 仓库只读；GA 改动走「先落盘调研 → 再实施」强制流程。
- **exe 到货后（用户 2026-10-15 说次日把 v2.1.283 exe 拷回）**：落位 `claudecode283_hack/claudecode283/claude.exe`
  （该路径已被 `.gitignore` 忽略，**别** `git add -f`）。自检：`stat -c %s` = **244960928**；
  `sha256sum` = **9DBE16DAFED59DA5CDABBFE11AD0335738C753FAD794989B47F9446ACCD6DE3A**（⚠️ 本机 `.local/bin/claude.exe` 是 v2.1.296，不是同一文件）。
  搜法（Git Bash 无 `strings`，用 `grep -a -o -b`；**别**在 IDA 跑 `db.strings.rebuild()`）：
  `grep -a -o -b 'chunk-x9hkq9y1.js' claude.exe` / `'ctrl+o'` / `'Ruminating'`；图标是真实 UTF-8 字节：`'⎿'`(⎿) `'⏺'`(⏺) `'✻'`(✻)。
  已定位的 UI 关键 chunk：`chunk-x9hkq9y1.js`、`chunk-4rk7cwxz.js`、`chunk-rnj6pcz5.js`(m1277+m0696 共用)、`chunk-qkvevvte.js`、`chunk-bgthmgyc.js`。
  **到货自检（2026-10-15，已通过）**：`claude.exe` 就位，`stat -c %s`=244960928、`sha256sum`=9dbe16da…de3a 与 MANIFEST 一致。
  探测：chunk 名命中在 **~97.36M**；`ctrl+o` ~98.2M；`Ruminating`/`spinnerVerbs` ~100.0M；`u273B` ~201M；`u2722/u273D` ~217M。
  字形两种存法：明文 JS 模块区是**转义写法**（`⎿` 25 处 / `✻` 14 处 / `⏺` 6 处…）；bytecode 区有**真实字节**（`⏺`/`✢`/`❯`/`⏹`/`▪`）。
  ⚠️ exe 含 Bun/JSC 引擎字符串表（~65–88M），`to expand` 等会命中**引擎噪声**（非 CC UI）→ 命中需按区域过滤，CC UI 真正在 **≈97–100M 与 200–217M** 两段。

### 中途调研 2026-10-16：CC 执行期「渲染层」UI（图标 / 折叠 / 排版）

- **触发**：用户 2026-10-15 提出 GA ink 执行过程「没有折叠 + 图标审美偏低」，要求对标 CC 源码 UI。
- **CC 源码坐标**（v2.1.283，`claude.exe` 已到货；权威证据见 `D:\git_codes\claudecode283_hack\docs\20261016-CC-UI渲染对标调研.md`）：
  - 字形常量表：`m0052_0xbfaae5d.js`（exe @201043800 / @201044050）；全量具名字形表 exe @207816961。
  - 工具结果行 `  ⎿  ` + dim + 折叠 `Hj`：`m1277_0xd55e81a.js`（exe @223735644 / @227326544 / @227360357）。
  - spinner 动词表 + store：`m0696_0xcf4efd9.js`；spinner 字形帧 `·✢✳✶✻✻`（回弹）exe @217487017。
  - 折叠提示（动态键位 `(ctrl+o to expand)`）exe @207813262 / @227327898 / @227328557；转录头 exe @228272100。
- **结论**：CC 渲染层有三条硬规则——① 图标是**具名字形表**（`⏺` bullet / `⎿` 结果前缀 / `✻` 星 / `├└│` 树 / `▸` 折叠三角）；
  ② 工具结果行统一 `  ⎿  ` + **dim** + 缩进；③ 折叠 = 渲染后测高锁 `minHeight`，提示走键位表。
- **对 GA 的影响（差距）**：GA 工具行是 `> name(args)`（与 user 的 `>` 撞车）；无 `⎿` 结果前缀；用 emoji（`🛠️ 🔨 📥 ✅`）当图标；
  workflow 详情用 `│`/`|` 两列表 + 固定 `padEnd(18)`（长标题错位）。折叠只有全局 `expandedTools`（ctrl+o）开关，无 per-block。
- **待办 → 本轮实施**：① 新增 `frontends/ink/ui/src/glyphs.ts`（CC 字形子集，语义名）；② 工具调用改 `⏺ name(args)`、结果加 `  ⎿  ` + dim；
  ③ emoji 换字形；④ 工具输出超长折叠 + `(ctrl+o to expand)`；⑤ workflow 详情改树/盒字形 + `string-width` 列宽（去掉固定 pad）。
- **不做**：云端会话 UI（【复刻时暂时不需要参考 claudecode 云端会话的实现】）、完整帧动画 spinner。
- **详细方案**：`docs/20261016-CC-UI-对标调研.md`（本仓库）。
- **状态（2026-10-16）：已实施。** 新增 `src/glyphs.ts`（字形表）+ `glyphs.test.ts`；工具调用行 `> ` → `⏺ `；工具结果行 `[Action]/[Status]/[Stdout]` → `  ⎿  <body>`（dim）+ emoji 归一；超长 `[Stdout]` 折叠 + `(ctrl+o to expand)`；workflow 详情改 CC 风格树（`├ └ │ ─`）+ `string-width` 列宽（去固定 pad）。ink-ui `npm test` 425 passed、`tsc` 通过。
- **补充（2026-10-16，commit 759d991）：源码去 emoji（不是只在 UI 层归一）。** 用户要求 emoji 不规范、要在**源码里**改，让 agent 直接产出 CC 字形：`agent_loop.py`/`continue_cmd.py` 工具头 `🔨/🛠️ Tool: … 📥 args:` → `⏺ Tool: … args:`；`ga.py`/`agentmain.py`/`continue_cmd.py`/`chatapp_common.py`/`btw_cmd.py` 状态哨兵 `✅/❌` → `✓/✗`。**关键坑**：`✅` 是**协议哨兵**（多前端 `startswith("✅")` 判恢复成功），故哨兵判断统一改**元组** `startswith(("✅","✓"))` 向后兼容；`App.tsx` 前缀正则 `^[✅✓]\s*`。pytest 160 passed、ink-ui 427 passed、无 emoji 残留。


### 中途调研 + 实施 2026-10-16：上游 error 被静默吞掉 → 伪装成「LLM 空回复」死循环（commit caa42f1）

- **现象**：workflow 任务里 `load_skill('workflow-authoring')` 之后，模型连续多轮
  `[Warn] LLM returned an empty response. Retrying...`，Turn1..Turn4 后停止，**没有任何错误信息**。
- **根因（实测复现，非猜测）**：provider `superapi.buzz` 的 `deepseek-flash` 渠道，对 `load_skill`
  的 tool_result（≈17.7KB）返回一条**裸 JSON 行**——既没有 `data:` 前缀，也没有顶层 `type`：
  `{"error":{"message":"Upstream access forbidden, please contact administrator","type":"upstream_error",...}}`。
  GA 的 SSE 解析器 `if not line.startswith("data:"): continue` 直接跳过该行；且 responses 分支只认
  `type=="error"` → `content_blocks=[]` → 下游 `do_no_tool` 判为「空回复」→ 静默重试 3 次 → 退出。
- **触发边界（二分实验）**：同一 endpoint，`stream=true`，`max_output_tokens=81920`——
  前 17500 字正常，**完整 17738 字**触发；「完整 + 尾部补一句问话」又正常；17738 个 `x` 也正常；
  非流式同 payload → HTTP 502。→ 是该渠道对「裸的大段文档 payload」的 guardrail，与关键字无关。
- **修复**（`llmcore.py` + `ga.py`）：
  1. `_sse_payload()`：除标准 `data:` 行外，也识别**裸 JSON 错误行**（`{... "error" ...}`）；三个 SSE 解析器
     （claude / openai responses / openai chat_completions）统一改用它。
  2. `_provider_error_message()`：从 `{"error": {...}}` 提取 message（dict/str 兼容）；responses 分支额外处理
     `response.failed` / `response.incomplete`；非流式 `_parse_openai_json` 前置校验。错误统一走既有哨兵 `!!!Error: ...`。
  3. `ga.py::do_no_tool`：把「流异常中断」（可重试）与「provider error」（同一 payload 确定性失败、重试无意义）
     分开——后者不再静默重试，改 `yield "[Error] LLM provider error: ..."` 并结束本轮，让用户看到真实原因。
- **可复用的诊断入口**：`temp/model_responses/model_responses_<pid>.txt` 记着每次请求的 **Prompt / Response 原文**
  （`=== Prompt ===` / `=== Response ===`）。看到 `Response` 是 `[]` 就说明**解析层拿到了空**，须查上游 SSE 原始字节，
  不要只怀疑模型。
- **验证**：真实 endpoint 复现裸错误行 → 三个解析器均产出 `!!!Error: ...`；GA 真实 client（profile=default）端到端确认；
  pytest 233 passed。
- **遗留（另案）**：`load_skill` 一次性把 17KB 正文塞进 tool_result 偏「重」（CC 的 skill 是渐进式披露），
  可作为后续优化项，与本次「错误被吞」修复解耦。


### 待办 2026-10-17：Skill「渐进式披露」（progressive disclosure）对标 CC —— 必做

- **动机（用户拍板）**：现在 GA 的 skill 越来越大（`workflow-authoring/SKILL.md` 已 17.3KB），
  `load_skill` **一次性把整篇正文塞进一个 tool_result**，这既是上文那次「上游 17.7KB 裸 payload 被
  guardrail 拒绝」的直接诱因，也浪费上下文。CC 的 skill 是**渐进式披露**，这是它的核心设计，必须抄。
- **CC 机制（v2.1.283 源码证据）**——三级披露：
  1. **L1 常驻上下文（廉价）**：系统提示里只放一份 **skill 清单 = 名字 + 一行描述**。
     证据：`m0357_0xc63ba04.js` 的 `skill_listing` 提醒模板
     `"The following skills are available for use with the Skill tool:\n\n${content}"`（`isMeta` 提醒），
     以及 `dynamic_skill`（新发现 skill 也只播报名字）。
  2. **L2 调用时加载正文**：`Skill` 工具被调用时才把该 skill 的**指令正文加载进本轮**。
     证据：`m0357` 的 Skill 工具描述 `Nqt`——"Available skills appear in a system-reminder listing
     with **one-line descriptions**… call this tool first — the skill's instructions **load into the turn**"。
  3. **L3 按需读附带文件**：skill 目录可带额外文件（`references/`、脚本等），正文里**让模型需要时再用 Read 读**，
     不预加载。证据：`m1556_0xdcb6d73.js` 的 `propose_skills` / `SKILL.md` 读写（`skillRoot` + `SKILL.md`）。
  补充：`invoked_skills` 提醒（`m0357`）在**压缩后**把"本会话早先调用过的 skill 正文"重新注入，避免遗忘。
- **GA 现状差距**：① 无 L1 清单（要么全量 load，要么模型不知道有什么 skill）；② `load_skill` 返回**全量正文**，
  无分片/摘要；③ skill 目录附带文件没有「按需读」的显式约定；④ 压缩后无 `invoked_skills` 回注。
- **优化方向（初稿，待细化）**：
  1. L1：把已注册 skill 的 `name + description`（+ 可选 `when_to_use`）注入系统提示/提醒，**不注入正文**。
  2. L2：`load_skill(name)` 仍返回正文，但正文本身应按"渐进式"重写——**先给目录/摘要 + 指向附带文件的路径**，
     而不是一次性倾倒；对超长 SKILL.md 提供 `load_skill(name, section=...)` 或让模型自己 `file_read` 分段。
  3. L3：约定 skill 目录内 `references/*.md` 等按需读取（正文里显式写"需要 X 时读 references/x.md"）。
  4. 压缩后回注 `invoked_skills`（对齐 CC）。
  5. 顺带解决上文那个「大 payload 被渠道 guardrail 拒绝」的诱因（正文不再一次性全塞）。
- **验收标准**：① 系统提示里能看到 skill 清单（名字+一行描述）而**不含正文**；② 一次 workflow 任务里
  `load_skill` 的 tool_result 体量显著下降（不再 17KB 级单块）；③ 需要细节时模型能按路径分段读取并完成任务；
  ④ 压缩/续跑后 skill 指南仍在（回注）。
- **关联**：`llmcore.py` 的 `!!!Error:` 修复（commit `caa42f1`）已让"被渠道拒绝"可被看见；本条是**根因侧**的优化。
- **详细方案（已落盘）**：`docs/20261017-Skill渐进式披露对标CC.md`（GA 现状 + 差距 + 4 阶段实施 + 验收）。
- **CC 侧证据（已落盘）**：`D:/git_codes/claudecode283_hack/docs/20261017-CC-skill渐进式披露调研.md`（L1 清单预算/降级、L2 `{alwaysOn,onInvoke}`、L3 附带文件、`/skills` 菜单原文、逐条模块坐标）。
- **关键认知（避免做错方向）**：CC 的 L2 **也是载入全文**，它没把 SKILL.md 切片按需加载；渐进披露的杠杆是 ①清单常驻 ②**正文要瘦**（重内容放 `references/`）。GA 的 L1/L2 已基本对齐，真正缺的是 **L3 正文瘦身 + 压缩回注**。

#### 追加实证 2026-10-17：CC Workflow 的 17K authoring prompt 是“按需 + 分通道”，不是常驻大块

- **源码坐标**：`m1502_0xdb4a986.js` 定义 `t`（Workflow 工具短描述，3,078 字符）、`i`（185 字符 load-skill 指针）、`Anr()`（完整 authoring reference，17,526 字符）；`xnr(e)` 在 skill 可用时返回 `t + i`，不可用时才回退 `t + Anr()`。
- **内置 skill**：`m1503_0xdb4fec6.js` 把 `Anr()` 注册为 `workflow-authoring` 内置 skill 的 `getPromptForCommand()` 返回值。正常路径不在 Workflow 工具 schema 里常驻 17K，而是模型调用 Skill 后才加载。
- **两通道**：`m0357_0xc63ba04.js` 的 `TAt(...)` 取 skill 正文 `w`，构造 `Ce({ content: w, isMeta: true, ... })` 作为**独立 user message**；`m0517_0xcd36592.js` 的 tool_result 仍然只有 `Launching skill: <name>`（约 30-40 字符）。真实 transcript 形态：tool_result 小确认 + `isMeta=true` 正文消息。
- **为什么不出问题**：17.5K 正文是合法 user text，不是嵌套 tool_result JSON；tool_result 保持极小；正文按需、一次加载；重复调用去重；compact 截断后有 re-invocation 提示。**不是因为 Claude 上下文大就无脑常驻**——17.5K 字符约 4.4-5K tokens，关键是按需加载和分通道。
- **GA 对齐要点**：Workflow 工具描述保持短；`load_skill` 的 tool_result 只做“已加载”确认；正文作为下一轮独立注入（GA 可先用 `next_prompt`，不必先改底层 API）；正文瘦身到 `references/`；重复加载去重；compact/续跑后 `invoked_skills` 回注。
- **详细落盘**：CC 侧 `D:/git_codes/claudecode283_hack/docs/20261017-CC-skill渐进式披露调研.md` §7；GA 侧 `docs/20261017-Skill渐进式披露对标CC.md` §1.1。
- **云端边界**：**【复刻时暂时不需要参考 claudecode 云端会话的实现】**。


#### 追加风险结论 2026-10-17：大 tool result 截断会不会影响后续 agent？

- **会影响。** 截断会改变后续轮次/下游 agent 能看到的信息；如果裸切 JSON，还可能让下游拿到不可解析的半截对象。
- **安全方案不是只截断，而是：完整内容持久化 + tool_result 返回有界摘要/preview + 可读引用。**
- **同一会话**：后续轮次可 `file_read` 持久化文件；tool_result 必须带 `truncated`、`artifactPath`、`originalChars`，并提示需要时读取。
- **workflow 下游**：不能只放父进程私有 temp；worktree child 看不到。GA 已有正确机制：
  `workflow_scheduler._build_handoff()` 写 `resultRef`/`readableResultRef`/`resultPath`/`artifactRefs`/`artifactOwners`/`handoffRef`；
  `_materialize_result_copy()` 复制到 `workflow-handoffs/result-<job>.json`；
  `_materialize_upstream_result()` 给下游写 `workflow-handoffs/upstream-<job>.json`；
  child prompt 明确要求下游读 `upstreamResultPath` / `artifactRefs`，不要直接打开内部 `resultRef`。
- **JSON 结果**：必须使用 `workflow_child_agent.bounded_structured_summary()` 的结构化投影（合法 JSON + `_truncated`/`_note` + 完整值 ref），禁止裸 `[:N]`。
- **P1 通用预算的硬约束**：先持久化完整内容，再展示有界摘要；路径必须对下游可读；JSON 不能裸切；敏感内容 redact；resume/cache 要稳定（sha256/size）。
- **详细落盘**：`docs/20261017-Skill渐进式披露对标CC.md` §1.3。


#### 追加调研 2026-10-17：CC 的通用 tool_result 预算（JSON 不是问题，大小才是）

- **结论**：CC 允许结构化 tool_result；问题不是 JSON，而是单个 `content` 字符串过大。
- **统一预算**（`m0297_0xc5de28c.js`）：每个 tool 可声明 `maxResultSizeChars`；`oKe -> V` 对 content 做预算。
  - 未超预算：原样返回；
  - 超预算：`ZW` 持久化到文件，`Ime` 返回 `<persisted-output>...Full output saved to: ... Preview ...</persisted-output>`；
  - 持久化失败：`j$n` 返回 `<truncated-output>...first N...</truncated-output>`；
  - `image` / `document` 不计入字符预算（`F`），文本字符串/text blocks 计入（`D`）。
- **具体工具**：`Write` 返回短确认；`Read` 有自身 `maxSizeBytes`/`maxTokens`/offset/limit；`Bash` 返回文本并支持 `persistedOutputPath`；`Grep` 返回文本；`Skill` 只返回 `Launching skill: <name>`；`Workflow` 返回 async 启动结果（runId/taskId），不返回完整日志。
- **GA 修改方向**：
  1. P0：`load_skill` 的正文从 tool_result 移出，放独立 `next_prompt`/meta 注入；tool_result 只留短元数据；重复加载去重。
  2. P0：`workflow-authoring/SKILL.md` 瘦身到 `<5KB`，细节放 `references/`。
  3. P1：`agent_loop.py` 增加通用 tool_result 预算：小结果保持 JSON/text；超阈值持久化到 `temp/tool-results/` 并返回 preview；无法持久化则截断；对已有自身分页的工具豁免。
  4. 不全局禁用 JSON；只限制大 content。
- **详细落盘**：CC 侧 `docs/20261017-CC-skill渐进式披露调研.md` §8；GA 侧 `docs/20261017-Skill渐进式披露对标CC.md` §1.2。
- **实施状态 2026-10-17（本轮已完成 P0/P0.5/P1/P2 的主体）**：
  - P0：`ga.py::do_load_skill` 的 tool_result 只保留短元数据，正文改走独立 `next_prompt`；重复加载相同正文去重。
  - P0.5：`skills/workflow-authoring/SKILL.md` 从 17,297 B 降到 5,052 B，细节拆到 `references/{api,examples,resume,artifacts,testing}.md`。
  - P1：新增 `tool_result_budget.py` 并接入 `agent_loop.py`；默认 50K 字符阈值、2K preview、500K persist limit；超限先落盘，JSON 用 `bounded_structured_summary()`，落盘失败才 `<truncated-output>`；`file_read`/`code_run` 等自管理工具豁免。
  - P2：`skills_runtime.py` 支持 `on`/`name-only`/`user-invocable-only`/`off`、`disable-model-invocation`、目录作用域命名（`apps/web:deploy`）、`alwaysOnChars`/`onInvokeChars`；Ink 增加 `/skills` 成本视图。
  - compact：`compact_context.py` 回注 `<invoked_skills>`，只带 name/description/base_dir/state/字符数，不重注全文。
  - focused 验证：Python 相关测试通过；`frontends/ink/ui` 的 `tsc --noEmit` 通过；Ink UI 测试通过。
  - MCP per-tool 结果上限：已接入 `_meta.maxResultSizeChars`（GA 规范 key，不使用 Anthropic 私有命名空间），
    经 `McpToolRef` / MCP tools cache 投影到通用 `tool_result_limits` registry；
    `tool_result_budget.budget_tool_result()` 自动查询 registry，值 clamp 到 `1..500000`，旧 cache / 非法值回退默认。
    相关文件：`mcp_runtime.py`、`tool_result_limits.py`、`tool_result_budget.py`；focused tests 11 + 49 + 11 passed。
  - 后续项：`budget` token 预算对象按用户决定暂缓；普通内置工具的 `maxResultSizeChars` 仅预留 registry 接口，本轮不改现有行为。
  - 详细实施记录：`docs/20261017-Skill渐进式披露对标CC.md` §5。
- **云端边界**：**【复刻时暂时不需要参考 claudecode 云端会话的实现】**。


### 中途调研 2026-10-15：agent team 的激活机制（不依赖用户正规输入）

- **触发**：用户指出——真实 e2e 要求用户输入「先 task_create 再 spawn_agent(team_mode=true)」太正规；
  应像 CC 那样自然提问就能用起来，或给个简短斜杠命令。
- **CC 源码坐标**（详见 `claudecode283_hack/docs/20261015-CC-agent-team激活机制调研.md`）：
  - 开关：`m0283_0xc5c1f6e.js` `isAgentSwarmsEnabled` = `--agent-teams` 或
    `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`，且 feature flag `tengu_amber_flint`；
  - **无团队斜杠命令**（只有无关的 `/team-onboarding`）；
  - **真正的激活机制 = 计划批准后 nudge**：`m0503_0xccfe7a3.js @78213` 的 `u6t()` 在用户批准计划后的
    ExitPlanMode tool_result 里追加「可拆就 spawn 具名 teammate 并行」；
  - 模型入口：`ExitPlanMode(launchSwarm,teammateCount)` / `Agent(name,team_name,mode)`（`m0357` `nOn`）。
- **结论 / 对 GA 的影响**：GA **已经能靠自然输入自触发** agent team（实证：3 个 `team_mode=true` teammate
  + 共享任务板 + 正确报告）。但差距有二：① 无「场景化 nudge」（lead hint 常驻但不含"何时组队"）；
  ② **任务板不是事实来源**（teammate 按 spawn message 干活，任务停在 `pending`/无 owner）。
- **待办 → 已完成（2026-10-15 本轮实施，见 P11）**：
  ① 场景化 nudge（对齐 CC `u6t`）：`subagent_team_activation.py` + `ink_bridge.submit()` 在自然并行请求上注入
     `[TEAM ORCHESTRATION OPT-IN]`；
  ② 任务板成为 source of truth：`subagent_tasks.team_task_protocol()` + `ga.do_spawn_agent` 的 `_bind_team_task()`
     预绑定（owner 统一为 agent 路径 `/root/<task_name>`）；
  ③ 会话开关：`/team on|off` + `GA_AGENT_TEAMS=1`（贴 CC `--agent-teams`）。
  不复刻 plan mode / `launchSwarm`。
- **文档**：`docs/20261015-agent-team激活机制-调研补充.md`（§7 实施设计）；
  实施记录 `docs/20261015-ga-agent-team-实施进度.md` P11。


### 中途调研 2026-10-15：teammate 完成不置 completed + close 回收 → 任务板失真（P11 补）

- **触发**：P11 首轮真实 e2e（自然输入，默认 deepseek-flash）证据——3 个 `team_mode` teammate 全部
  `turn_status=completed`、报告落盘，但共享任务板 3 条仍 `pending` / `owner=None`（验收不达标）。
- **证据**：`temp/teams/session-3b37d4ea/tasks.json`（全 pending）；teammate `events.jsonl` 无
  `task_claimed/task_completed`，只有 `turn_completed → agent_waiting_reply → idle_notification`
  （`idleReason=available`、无 `completedTaskId`）。
- **源码事实（GA 侧）**：`SubagentManager.close_agent` → `reclaim_tasks_for` →
  `SubagentTaskStore.reclaim_owner`（对齐 CC `N4e`）会把「未完成」的 owned 任务回收为 `pending`；
  teammate 既没自己 `task_update`，回收就把 spawn 时的绑定抹掉了。
- **结论 / 对 GA 的影响**：CC 靠 teammate 自己 `TaskUpdate`；弱模型不照做 → 任务板不是事实来源。
  当时 GA 曾加**宿主兜底**：`agentmain._teammate_idle_hook` 在 `turn_status=completed` 时
  `store.complete_owned_in_progress('/root/<name>')`（只收自己的 `in_progress`），并在 CC `mat` idle 帧里
  带上 `completedTaskId`/`completedStatus`。这是历史实现，**已于 2026-10-19 移除**：turn completed 不是任务成功证据，会掩盖 BLOCKED teammate。
- **复验**：真实 e2e 复跑 `natural_p11b` → 任务板 **3/3 completed**，owner 各自 `/root/<name>`，报告 8014 B。
- **待办**：无（P11 验收达成）。
- **文档**：`docs/20261015-ga-agent-team-实施进度.md` P11。

### 中途调研 2026-10-15：teammate 团队名解析错位 → 任务板分片（P11 补 2）

- **触发**：复查真实 e2e 产物时发现同一 run 出现「同一 teammate 落在两张板」：主板 `session-26db317d` 3 条 completed，
  但另有 `session-f52c4ef1` / `session-c6c74e42` / `session-9c2e7341` 三张**单条**任务板。
- **证据**：那三份 session 日志（`temp/sessions/session_{f52c4ef1,c6c74e42,9c2e7341}*.jsonl`）的
  `session_start.created_at` 都是 17:49:53 = 三个 teammate 的 spawn 时刻 → 那三张板就是三个 teammate **各自的** session 板。
- **源码事实**：`session_transcript.ensure_agent_session` 给每个 agent（含 teammate 子进程）**新建 session_id**；
  `GenericAgentHandler._team_name()` = `team_name_for(self.parent.session_id)` → teammate 用**自己的** session 解析 team 名，
  与 lead 不同。结果：teammate 模型可见的 `task_list/task_update/task_create` 全打到自己的板（还会触发
  「板上没有就 task_create」的 fallback 产出碎板），**根本读不到 lead 的板**；主板 3/3 completed 完全靠宿主兜底
  （历史 `_bind_team_task` 预绑定；当时的 `complete_owned_in_progress` 自动收口已移除）。
- **修复**：`SubagentManager._child_popen_kwargs` 在子进程 env 注入 `GA_TEAM_NAME=<manager.team_name>`（= lead 的 team 名）；
  `team_name_for` 优先读 `GA_TEAM_NAME` → 全队共用一块板。对齐 CC `m1578` 的 swarm env 传播。
- **复验**：真实 e2e `boardfix_p11c` → 本次 run **只产生 1 张板**（`session-83b71255`，3 条全 completed），
  三张碎板消失；teammate 仍有自己的 session，但都解析到 lead 的板。
- **文档**：`docs/20261015-ga-agent-team-实施进度.md` P11；`docs/20261015-agent-team激活机制-调研补充.md` §7.6。

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
  `npm --prefix frontends/ink/ui run typecheck` OK；`npm test` **405 pass / 0 fail**。
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
  - `frontends/ink/ui/src/messageFormat.ts` 兼容历史 transcript 中已经存在的 `[Workflow] ...````` ` / `[Warn] ...````` ` 内联关闭形式。
  - `frontends/ink/ui/src/streamCommit.ts` 的 fence 状态机改为按 Markdown 语义匹配 opener/closer 长度，并把 legacy 内联关闭视为 closer；否则它会把嵌套的 ````text` 误判为关闭 5 反引号块，从而在 `[Info]` 的真正 closer 前切段，让下一段以 closer 开头并吞掉 Turn 4。
- 详细文档：`docs/20261012-ga-ink-workflow-fence-inline-close-fix.md`。
- 状态（2026-10-12）：已实施；回归覆盖 Python 工具输出 fence、Ink transcript 渲染与 stream-commit fence 状态机。验证：`python -m unittest discover -s tests` 1325 OK（skipped=3），`npm --prefix frontends/ink/ui test` 407 pass，typecheck OK。

### 中途调研 2026-10-12：GA Ink workflow 实测缺陷清点（wait_agent / workspace guard / 并行隔离 / 假成功）

- 触发：用户实测 `/workflow` 并行任务（run `wf_3332887439d547609a91dde829e89a43`）出现两类现象——(1) 编排层 `wait_agent(condition="workflow_terminal", target=<runId>)` 空等 300s timeout；(2) 主代理汇报「qipao.html 未产出」「code_run 子进程被 workspace guard 禁用，只能用 runpy 同进程执行」。
- 结论：本次清点出 5 个独立缺陷（A~E），**仅记录、未分析根因、未改代码**，待次日分析。
  - A（bug，已确认根因）：`wait_agent(condition="workflow_terminal")` 是假实现，等价于 `all_terminal` 且只认 subagent task_name，不读 workflow run 终态；传 runId 时对着合成的孤儿 state 空等到 timeout。坐标 `subagent_wait.py:78`、`subagent_manager.py:33/609`、`ga.py:1098`。
  - B（设计缺陷）：`workflow workspace guard` 无条件禁用 workflow 子代理的所有 `subprocess.*`，子代理无法真实运行/验证产物（如 `python script.py`、`pip`），偏离 CC。坐标 `workflow_workspace_guard.py:74-82`、`assets/code_run_header.py`、`ga.py:64`。
  - C：并行 workflow `artifact_path_collision`——多个 author 写同一文件，`writeScope` 未有效隔离。证据 `wf_3332.../run.json` 的 `workflowIssues`/`artifactCollisions`。
  - D：子代理「未产出却上报成功」——声明 artifact 缺失但 job 计 succeeded，缺产物存在性校验。证据 agent_2（qipao.html）。
  - E：verify 阶段被迫用 `runpy.run_path()` 同进程执行，验证强度打折，且受 guard 波及。
- 真实 run 证据：`run.json` status=`degraded`；`childSummary` 6/6 terminal；degraded 由 artifact 冲突 + qipao.html 缺失触发；run 实际早已结束（非卡死）。
- 待办：见独立文档；A 明确是 bug 可先修，B 需先出改造方案，C/D/E 待分析。
- 详细文档：`docs/20261012-ga-workflow-实测缺陷清点-待分析.md`。
- 状态（2026-10-12）：**仅落盘，未实施。**

### 中途调研 2026-10-13：CC workflow 的多 agent 冲突处理 = worktree 隔离 + 值传递（不靠冲突检测）

- 触发：用户要求研究 CC 2.1.283 dynamic workflow 如何减少多 agent 协作冲突，并出 GA 优化方案。
- 上游证据（仓库 `D:\git_codes\claudecode283_hack`）：
  - CC 作者参考 `claudecode283/extracted/workflow-authoring-reference.md:37`：
    `opts.isolation:'worktree'` 让 agent 在**全新 git worktree** 里跑，`auto-removed if unchanged`，
    **EXPENSIVE (~200-500ms + 磁盘/agent)，仅在并行改文件会冲突时才用**。
  - 源码 `claudecode283/modules/m0869_0xd12567a.js` @57526：`else if(ve?.isolation==="worktree"){let E=g?`${g}-${M}`:`wf-${M}`;Je=await vt(E,{...})}` → `worktreePath:fn` 传给 child；默认 `Je=null`（不开，共享 cwd）；内置 web-fetch agent 强制忽略 worktree。
  - worktree 模块 `m0531_0xcd4d7c8.js`：`.claude/worktrees/`、`worktree_created`/`worktree_kept`/`worktree_removed`。
  - **CC 全仓无 `artifactCollision`/`artifactOwnership` 概念**（grep 命中 0）——冲突检测是 GA 自创。
  - CC 默认形态：`agent()` 返回**值**（文本或 schema 校验对象），`parallel()`/`pipeline()` 传**值**，天然无冲突。
- GA 现状与根因（本次确认）：
  - workflow `agent()` **不支持 `isolation`**（`workflow_runtime.py:476` 只读 label/requiredTools/capabilities）。
  - 并行 child 共享同一 workspace（`workflow_child_agent.py:971 _child_cwd` 返回 run 的共享 `workspacePath`），
    每个 child 对**共享 workspace** 做 before/after diff（`workflow_child_agent.py:733-758`）→ 并行窗口重叠 →
    **别人的文件被算到自己头上** → 假 `artifact_path_collision`（`workflow_scheduler.py:1409 _record_artifact_collisions`）。
  - 实测证据：run `wf_3332887439d547609a91dde829e89a43`，`author:eight_queens` 被归属写了 `newton_method.py`/`primes_1_100.py`（实际没写），
    但文件内容正确（未真覆盖）——证明是**归属误判**而非真覆盖。
  - GA 已有可复用件：`subagent_worktree.create_subagent_worktree/remove_subagent_worktree`（Agent 工具 `isolation:"worktree"` 在用，`ga.py:30 _resolve_subagent_isolation`）；
    `workflow_child_agent._child_cwd` 已有 per-job 目录 fallback（`temp/workflow_child_agents/<jobId>`）；workflow 已有 `schema` 值传递（`workflow_scheduler.py:156 validate_agent_payload_against_schema`）。
- 结论：GA 要抄的不是「更好的冲突检测」，而是 CC 的**从源头避免**——(a) 值传递为默认，(b) 并行写文件时给每个 agent 独立工作目录（worktree/子目录）。
- 待办：见独立方案文档；阶段 0 先修归属误判，阶段 1 给 workflow `agent()` 加 `isolation`，阶段 2 值传递默认化，阶段 3 提示词/文档。
- 详细方案：`docs/20261013-ga-workflow-多agent冲突-对齐CC-worktree隔离与值传递.md`。
- 状态（2026-10-13）：**阶段 1-3 已实施，全量测试通过**（见实施记录）。

#### 实施结果（2026-10-13，阶段 1-3）

- **阶段 1**：workflow `agent()` 支持 `isolation:"worktree"`——runtime 白名单校验（其余抛「not available in this build」）；
  `workflow_child_agent` 新增 `_job_isolation`/`_shared_workspace`/`_isolated_workspace`/`_collect_isolated_artifacts`，
  `_child_cwd` 隔离时返回私有目录 `temp/workflow_child_agents/<runId>/<jobId>`，diff 落在私有目录、产物回收回共享 workspace。
- **阶段 2**：`job.metadata["artifactConfidence"]`（exact/declared/inferred）——隔离或 wave 唯一 job = exact；
  并发且声明路径 = declared；并发且未声明 = inferred。`_record_artifact_collisions` 只对**≥2 可信生产者**报
  `artifactCollisions`；并发未声明同路径降级为**不 degrade** 的 `unverifiedWrites`。
- **阶段 3**：`skills/workflow-authoring/SKILL.md` 增补 `isolation` 选项、`Pass values, don't share files` 模式、边界修订。
- **测试**：`tests.test_workflow_*` 全绿（scheduler/workspace/store 109、child/runtime/integration 等 174、全量 discover 1331 OK）。
- **实施记录**：`docs/20261013-ga-workflow-多agent冲突-实施记录-阶段1-3.md`。
- **未做**：阶段 4（受控 subprocess 解缺陷 B/E）、隔离目录清理、`artifactOwnership` 按置信度过滤、隐式默认隔离 → 均已做；`isolation:"remote"` 不做（**复刻时暂时不需要参考 claudecode 云端会话的实现**）。

#### 补充（2026-10-13，阶段 2b：归属索引按置信度过滤）

- 动机：阶段 2 让**冲突判定**忽略 `inferred`，但 run 级 `artifactOwnership` 仍收录全部 `observedArtifacts` → handoff 仍误标「谁写的」。
- 设计（不误删正确归属）：路径**同时有可信归属**（exact/declared）时才丢弃其 `inferred`；**只有 inferred 的路径保留**（单写者未声明的常见场景不误删）。
- 实现：新增 `workflow_workspace.artifact_ownership_index(jobs)` 为唯一实现；`workflow_store.build_artifact_ownership_index`、
  `workflow_scheduler._artifact_owners_for`、`frontends/ink_bridge._workflow_job_artifact_owners` 均改调它。旧数据无 confidence → 默认 `exact`，向后兼容。
- 测试：workspace 3 例 + scheduler 1 例；全量 discover **1344 OK (skipped=3)**。
- 记录：`docs/20261013-ga-workflow-多agent冲突-实施记录-阶段1-3.md` §5.1。

#### 实施结果（2026-10-13，阶段 4 / 缺陷 B·E：受控 subprocess）

- 触发：缺陷 B（guard 全禁 subprocess）与 E（verify 被迫 runpy）。CC 的做法是 `bashCommandClamp`
  （per-spawn 收窄 shell 命令形式，fail-closed）——证据 `m0869_0xd12567a.js:667/1771-1782/2073-2075`、
  `m0357_0xc63ba04.js:110968-112051`、`m0052_0xbfaae5d.js:189-190`。
- **改动**：`workflow_workspace_guard.py` 重写子进程策略——默认**允许**（cwd 限定 workspace + 灾难命令 denylist）；
  设 `bashCommandClamp` 则只放行匹配形式（带 shell 结构 → 拒绝，fail-closed）；`GA_WORKFLOW_GUARD_STRICT=1` 恢复全禁。
  `ga.py code_run` 新增 `bash_clamp` → 环境变量 `GA_WORKFLOW_BASH_CLAMP`；`do_code_run` 透传；
  `workflow_child_agent._build_handler` 挂 `handler.workflow_bash_clamp`；`workflow_runtime` 校验 `bashCommandClamp` 形状。
  路径守卫不变。`os.system`/`os.popen` 一并纳入策略（旧 guard 其实已被它们绕过）。
- **安全取舍**：允许子进程后，子进程文件访问不再受 in-process 路径守卫约束——guard 是「尽力而为的引导」，
  **不是安全边界**（与 CC 对 VM 沙箱的定位一致）。更严用 clamp 或 strict。
- **测试**：`tests.test_workflow_workspace_guard`（重写 8 例）、runtime/child-agent 新增用例；全量 discover **1340 OK (skipped=3)**。
- **实施记录**：`docs/20261013-ga-workflow-受控subprocess-实施记录-阶段4.md`。
- **未做**：OS 级隔离；verify 真实运行的强制契约。

#### 补充（2026-10-13，阶段 4b：clamp 兼容 CC 的 `Bash(...)` 写法）

- `bashCommandClamp` 现在同时接受 CC 的 `Bash(<命令或前缀>)` 形式（剥壳 + 去尾部 `:*`/`*` 通配）与 GA 裸前缀；
  新增纯函数 `workflow_workspace_guard.normalize_bash_command_clamp(entries)`，在 `workflow_runtime` 的 agent RPC 处
  **校验并归一化**（存回归一化后的前缀），`_build_handler` 复用同一函数。非 `Bash` 工具名 / 空内容 / 内部空白 / inert → 拒绝 spawn（fail-closed）。
- CC 脚本的 `bashCommandClamp` 可原样迁移；resume 缓存键基于归一化值。
- 测试：`tests.test_workflow_workspace_guard` 新增 `NormalizeBashCommandClampTest`（6 例）+ runtime 2 例；全量 discover **见提交**。
- 记录：`docs/20261013-ga-workflow-受控subprocess-实施记录-阶段4.md` §4/§6。

#### 中途调研 2026-10-13：隔离目录清理（对齐 CC「worktree is auto-removed if unchanged」）

- 触发：阶段 1-3 给 GA 加了 `isolation:"worktree"` 私有目录 `temp/workflow_child_agents/<runId>/<jobId>`，
  但**从不删除**（阶段 1 刻意留白）。用户问「隔离目录清理是要怎么做」。
- 上游证据（仓库 `D:\git_codes\claudecode283_hack`）：
  - 工具描述 `claudecode283/modules/m1502_0xdb4a986.js`：`isolation:'worktree' ... the worktree is auto-removed if unchanged`。
  - 实现 `reconstructed/deminified/m0511_0xcd25473.js:855-1013` 的 `getWorktreeResult()`：**无改动→移除**（outcome `removed`）；
    **有改动→保留**并打 `Agent worktree kept at: <path>`、返回 `worktreePath/worktreeBranch`；hook-based / 后台 owner 等边界一律保留。
  - 移除动作 `reconstructed/deminified/m0319_0xc61d343.js:80-170`：`git worktree remove --force`，删前有一串守卫（非主 worktree、非他机、无 reparse point、路径完好）→ fail-closed。
  - CC 承认保留的 worktree 会无限增长（`m0357_0xc63ba04.js:39113`），只提示用户手动 `git worktree remove/prune`。
- GA 差异：GA 已把产物**回收**到共享 workspace（`_collect_isolated_artifacts`），私有目录对功能非必需；保留它的价值是排查（如 agent_2 未产出 qipao.html）。
- 设计（详见方案文档）：
  - env `GA_WORKFLOW_ISOLATION_KEEP` = `changed`(默认，对齐 CC) / `always` / `never`。
  - 带守卫的 `_remove_isolated_workspace`（限定 base 之下、名字匹配 runId/jobId、拒 symlink；失败只 warning）。
  - 清理在 `_collect_isolated_artifacts` **之后**；保留时写 `tool_summary["keptWorkspacePath"]`。
  - 额外 TTL 兜底 `prune_isolated_workspaces()`（env `GA_WORKFLOW_ISOLATION_TTL_HOURS`，默认 168h，0=关）。
  - env `GA_WORKFLOW_CHILD_AGENTS_ROOT` 覆盖 base（测试友好）。
- 详细文档：`docs/20261013-ga-workflow-隔离目录清理方案.md`。
- 状态（2026-10-13）：**已实施**（见下方实施结果）。

#### 实施结果（2026-10-13，隔离目录清理）

- env：`GA_WORKFLOW_ISOLATION_KEEP`（`changed` 默认，对齐 CC / `always` / `never`）；
  `GA_WORKFLOW_ISOLATION_TTL_HOURS`（默认 168h，`0`=关）；`GA_WORKFLOW_CHILD_AGENTS_ROOT` 覆盖 base。
- `workflow_child_agent.py`：`_isolated_workspace_base/_path`、`_remove_isolated_workspace`（四重守卫 fail-closed）、
  `_maybe_cleanup_isolated_workspace`、`_finalize_isolated_workspace`、`_maybe_prune_isolated_workspaces`；
  模块级 `prune_isolated_workspaces()`；`start()` 限频（≥600s）清扫。
- 清理在 `_run_job` 成功路径、产物回收**之后**；保留时写 `tool_summary["keptWorkspacePath"]`（对齐 CC `kept at`）。
- `skills/workflow-authoring/SKILL.md` 增补隔离目录清理语义 + 三个 env。
- 测试：`tests/test_workflow_isolation_cleanup.py`（12 例）；全量 discover 见提交。
- 记录：`docs/20261013-ga-workflow-隔离目录清理方案.md`、`docs/20261013-ga-workflow-多agent冲突-实施记录-阶段1-3.md` §5.2。

#### 中途调研 2026-10-13：隐式默认隔离（implicit default isolation）定义与 GA 落点

- 触发：用户确认「隐式默认隔离可做」。先澄清两个易混概念，再记录 GA 实现落点（先落盘再实施）。
- **隐式默认隔离** = 隔离**何时**生效：作者不写 `isolation` 时 GA 也自动隔离（仅并行写者场景）。
  - CC 对照（仓库 `D:\git_codes\claudecode283_hack`）：`m0357_0xc63ba04.js:34126-34127`（未隔离的后台会话改共享 checkout 被**直接拒绝**，
    可用 `"worktree":{"bgIsolation":"none"}` 关）、`m0357:64417`（EnterWorktree 提示「file edits in the shared checkout are rejected until you isolate」）、
    `m0464_0xcc31217.js:1864`（内置 agent 不隔离，子代理按需）。
  - 结论：CC **不是给所有 agent 建 worktree**，而是「**对并行/后台会话强制先隔离**」。GA 类比 = 并发写者默认隔离。
- **OS 级隔离** = 隔离**靠什么**强制：由 OS/容器内核强制，而非 GA 的进程内 guard。
  - CC 对照：sandbox-runtime（macOS seatbelt / Linux-WSL bubblewrap / Windows 需 Git Bash，`m0357:134643`、`m0357:40474`）
    + 文件系统 deny/mask + 网络 allowlist + 凭据遮蔽 + TLS 终止（`m0052_0xbfaae5d.js` schema）。
  - GA 现状：`workflow_workspace_guard` 是进程内 monkeypatch，**是引导不是安全边界** → 本轮不做。
- **GA 实现落点（本轮）**：
  - 并发信号：`parallel()`/`pipeline()` 在 `workflow_js_worker.js` 里用 `Promise.allSettled(items.map(item=>item()))` **同步**发出整批 `agent()` RPC；
    worker 会带上 `parallel: <batchSize>` 提示（模块级 `parallelGroup`）。
  - 调度：`workflow_scheduler.register_agent`(@260) 记 `metadata["parallelBatch"]`；`_start_queued_jobs`(@524) 起跑前调 `_apply_implicit_isolation`。
  - 规则：作者未声明 `isolation` ∧ 是写者（声明 `writeScope`/`deliverables` 或 `capabilities` 含 `file_write`/`execute`）
    ∧ `parallelBatch >= 2` ∧ `max_concurrent > 1` → 自动 `isolation:"worktree"`，
    记 `metadata["isolationReason"]="implicit-parallel-writer"` + 事件 `agent_isolated`。
  - 逃生口：`isolation:"none"`（显式关；runtime 白名单扩到 `{worktree, none}`）。
  - **竞态事实（关键）**：host 循环是 `tick()`(起跑) → `_next_message()`(读一条)，所以「按瞬时 wave 兄弟数」判定会在 A 起跑之后才读到 B，
    **不可靠**；因此改用 worker 的 `parallel` 批次提示（确定性）。
- 详细文档：`docs/20261013-ga-workflow-隐式默认隔离方案.md`。
- 状态（2026-10-13）：**已实施**。

#### 实施结果（2026-10-13，隐式默认隔离）

- `workflow_js_worker.js`：模块级 `parallelGroup`；`agent()` 的 rpc 带 `parallel: <batchSize>`；`parallel()`/`pipeline()` 在 `items.map` 前后设置/复原。
- `workflow_runtime.py`：agent RPC 读 `params["parallel"]` → `register_agent(..., parallel_batch=...)`；`isolation` 白名单扩到 `{worktree, none}`。
- `workflow_scheduler.py`：`register_agent`/`register_cached_agent` 记 `metadata["parallelBatch"]`；`_apply_implicit_isolation(job)` 在 `_start_queued_jobs` 起跑前调用。
  规则：未声明 `isolation` ∧ 写者（`declares_writes`）∧ `parallelBatch>=2` ∧ `max_concurrent>1` → `isolation:"worktree"` + `metadata["isolationReason"]="implicit-parallel-writer"` + 事件 `agent_isolated`。
- `workflow_tool_profiles.py`：新增 `declares_writes(options)`（单一实现）。
- `skills/workflow-authoring/SKILL.md`：文档化隐式默认隔离 + `isolation:"none"`。
- 测试：`tests/test_workflow_implicit_isolation.py`（13 例，含 2 例真跑 node worker 的端到端）；全量 discover 见提交。
- 记录：`docs/20261013-ga-workflow-隐式默认隔离方案.md`。

#### 中途调研 2026-10-13：verify 的「提示词方法论」路线（对齐 CC）

- 触发：用户决定 GA 也走 CC 的 verify 路线（**prompt 层方法论**，不做宿主强制的「真实运行」契约），并检查提示词是否要改。
- CC 事实（仓库 `D:\git_codes\claudecode283_hack`）：
  - `docs/20261012-复刻dynamic-workflow指南.md` §10.2：CC **没有**独立于 LLM 的验收器；
    「脚本 return 什么，最终结果就是什么」；唯一对抗「看起来对但实际错」的是 **prompt 层方法论**（概率性缓解，非验证器）。
  - `claudecode283/extracted/workflow-authoring-reference.md:112-126` 给出确切配方：
    - **Adversarial verify**：N 个独立 skeptic，每个被要求 **REFUTE**，`Default to refuted=true if uncertain`；≥多数 refute 则 kill。
    - **Perspective-diverse verify**：不同 lens（correctness/security/perf/repro）。
    - **Judge panel** / **loop-until-dry**（**dedup vs `seen`，不是 `confirmed`**，否则不收敛）/ **multi-modal sweep** / **completeness critic** / **no silent caps**。
    - **「DEFAULT TO pipeline()」** + barrier smell test（只有 stage N 需要 stage N-1 **全体**时才用 barrier）。
- GA 现状：`skills/workflow-authoring/SKILL.md` §5 已覆盖大部分质量模式
  （adversarial / perspective-diverse / judge panel / loop-until-dry / multi-modal sweep / completeness critic / no silent caps）。
- **差距（本次补）**：(1) 缺「DEFAULT TO pipeline()」+ barrier smell test；(2) loop-until-dry 缺 dedup-vs-`seen` 陷阱；
  (3) 缺 adversarial-refuter 的**具体配方**（prompt 文本 + 多数阈值 + fail-closed 默认）；(4) 子代理 `verification` role 是「验证」口吻而非「**对抗式反驳**」；
  (5) `review` role 缺「先反驳每个 finding」。
- 详细文档：`docs/20261013-ga-workflow-verify-提示词方法论-对齐CC.md`。
- 状态（2026-10-13）：**已实施**。

#### 实施结果（2026-10-13，verify 提示词方法论）

- `skills/workflow-authoring/SKILL.md`：
  - §4 pipeline 增「**Default to `pipeline()`**」+ barrier smell test（只有 stage N 需要 stage N-1 全体才用 barrier）。
  - §5 Adversarial verify 补 CC 的**具体配方**（`Try to refute: <claim>. Default to refuted=true if uncertain.` + ≥2/3 阈值）。
  - §5 Loop until dry 补「dedup vs `seen`，不是 `confirmed`，否则不收敛」。
- `workflow_child_agent.ROLE_INSTRUCTIONS`：
  - `verification` → 对抗式（主动反驳、不只确认）+ 无证据/不确定即 `verificationPassed=false`。
  - `review` → 报告前先对抗式反驳每个 finding，站不住的丢掉。
- 测试：`tests/test_workflow_child_agent.py::LiveJobTelemetryTest::test_verification_and_review_roles_are_adversarial`（1 例）；全量 discover **1378 OK (skipped=3)**。
- 记录：`docs/20261013-ga-workflow-verify-提示词方法论-对齐CC.md`。

#### 补充 2026-10-13：把 review-changes canonical 示例补进 SKILL

- 触发：用户确认「好」——把 GA 的 `review-changes` 示例升级成 CC 的**完整 canonical 形态**。
- CC 事实（仓库 `D:\git_codes\claudecode283_hack`）：
  - `review-changes` 全仓**只出现在 `modules/m1502_0xdb4a986.js`**（= Workflow 工具描述）——它是**教学模板**，不是内置可跑 workflow。
  - 位置：`claudecode283/extracted/workflow-tool-description.md:16-40`（canonical multi-stage pattern）、`workflow-authoring-reference.md:10`（Review 范式引用）。
  - CC 只在 `.claude/workflows/` 加载**用户保存**的 workflow（`m0868_0xd1234ad.js` `loadWorkflowsDir`）。
- GA 现状：`skills/workflow-authoring/SKILL.md` §3 的 `meta` 示例已叫 `review-changes`；§4 的 pipeline 示例是**简化版**——
  缺 `schema`、`Adversarially verify`、`.then(v => ({...f, verdict:v}))`、`verdict.isReal` 过滤，以及「no wasted wall-clock」说明。
- 改动：把 §4 pipeline 示例升级为 CC 的 canonical 完整形态（补 schema + 对抗式 verify + verdict 过滤 + wall-clock 注释），
  并注明 `FINDINGS_SCHEMA`/`VERDICT_SCHEMA` 由作者定义。
- 详细文档：`docs/20261013-ga-workflow-verify-提示词方法论-对齐CC.md` §6。
- 状态（2026-10-13）：**已实施**。

### 缺陷 F（2026-10-13）：`/new` 后 `workflow` 工具报「launcher unavailable」

- 触发：用户「最近一次测试 我开了 workflow on」，却在 Turn 3 看到
  `The workflow launcher isn't available in this environment (workflow tool launcher unavailable)`。
- 现象坐标：`temp/sessions/session_263293057ec6440999915216adcabd15.jsonl`（2026-10-13 19:14，workflow 未启动）；
  同一进程里更早还有 `session_84c43011de614948af2be86ead5ca0e9.jsonl`（19:13:32，仅 `session_start`）——
  即 19:14:16 又新建了一个 agent（`new_session` 路径）。
- 根因（已最小复现）：`frontends/ink/bridge.py` 里 `workflow_tool_launcher` 只在 `__init__` 绑定到 `self.agent`；
  `new_session()` 用 `self.agent = self.agent_factory()` **换掉了 agent，却没有重新绑定 launcher**。
  而模型可见的 `workflow` 工具由 handler 从 `parent.workflow_tool_launcher` 取（`ga.py:750`），
  于是新 agent 上取不到 → `do_workflow` 返回 `workflow tool launcher unavailable`。
  触发条件：**任何 `/new`（或 `/reset`）之后的会话**。
- 澄清：opt-in（`/workflow on`）与 workflow 工具本身都没问题，坏的是「换 agent 时漏挂 launcher」这一处绑定。
- 修复：抽出 `GenericAgentBridge._bind_agent(agent)`（设 `inc_out` / `verbose` / `workflow_tool_launcher` / permission emit），
  `__init__` 与 `new_session` 都改调它，消除「两处换 agent、只一处挂 hook」的漂移。
- 测试：`tests/test_ink_bridge.py::InkBridgeTest::test_new_session_rebinds_workflow_tool_launcher`（新增）；
  全量 discover **1379 OK (skipped=3)**。
- 状态（2026-10-13）：**已实施**。

### 缺陷 G（2026-10-13）：隐式默认隔离误伤 verify（`execute` 被当成写者）

- 触发：用户实测「workflow 一直卡住，`verify:html_time` 前有 `!`」。
- run：`wf_a56df8bc24af434090851d96d31745d1`（`session_419bf66b10924881a5223e66c4bc71e8`）。
- `!` 语义：`frontends/ink/ui/src/workflowPanel.ts:226` —— `degraded|partial → '!'`（该 job 降级）。
- 根因：`workflow_tool_profiles.declares_writes` 的写者集合含 `execute`（`WRITE_CAPABILITIES={"file_write","execute"}`），
  于是 5 个 verify job（`capabilities:["file_read","execute"]`、无 writeScope）也被判为写者 → 隐式隔离 →
  cwd 落到**私有空目录**（`temp/workflow_child_agents/<runId>/<jobId>/`），看不到 author 在共享 workspace 的产物 →
  满盘找文件（agent_7 烧 9+ 轮）→ 降级 → run `degraded`。
- CC 对照：CC 只隔离**写者**；读/验证者读共享（已合并）工作区。GA 的隔离目录是空目录，隔离读者=致盲。
- 修复：拆 `MUTATING_CAPABILITIES={"file_write","execute"}` 与 `FILE_WRITE_CAPABILITIES={"file_write"}`；
  `declares_writes` 改用后者 —— **裸 `execute`/`code_run` 不再触发隔离**。author 仍隔离，verify 回到共享工作区。
- 测试：`tests/test_workflow_implicit_isolation.py`（改 `test_bare_execute_is_not_a_writer`，新增 `test_parallel_verifier_is_not_isolated`）；
  全量 discover **1381 OK (skipped=3)**。
- 文档：`docs/20261013-ga-workflow-隐式隔离误伤verify-对齐CC.md`。
- 状态（2026-10-13）：**已实施**。

### 缺陷 A（2026-10-13，复现并修复）：`wait_agent(condition="workflow_terminal")` 假实现 →「有失败就一直卡住」

- 触发：用户实测「workflow 有执行失败就一直卡住」。主代理发
  `wait_agent {condition:"workflow_terminal", target:"wf_9af50ac554fd43cc9f9d25cc467cd43b", timeout_seconds:600}`，
  该 run 早已 `status=degraded` 收尾（19:57），wait 却空等到 timeout，反复重发 → 永久卡住。
  证据：`temp/model_responses/model_responses_297593.txt`。
- 根因（缺陷 A 的完整链）：`subagent_wait.py:78` 把 `workflow_terminal` 当 `all_terminal` 别名，只按 **subagent 状态机**判定；
  `subagent_manager.probe_agent()` 把 `wf_<32hex>` 当 task_name，读 `temp/<runId>/state.json` 找不到就**合成**非终态
  （`turn_status=running`/`process_status=exited`），无人推进 → 永不满足。真实终态在
  `temp/sessions/<sid>/workflows/<runId>/state.json` 的 `status`，wait 完全不读。
- CC 对照（`claudecode283/extracted/dynamic-workflow-internals.md` §8.3/8.4）：run 是后台任务，工具立即返回、**不轮询**；
  结束时 `RTn()` 发 `<task-notification>`（`priority:"next"`）唤醒主 agent，`<status>` 只有 completed/failed/killed，
  另带 `<failures>` + `<diagnostics>`（journal.jsonl，「先读再诊断」）。
- 修复：
  - `subagent_wait.py`：新增 `is_workflow_run_id` / `workflow_run_is_terminal` / `workflow_run_turn_status`
    （`degraded/partial → completed` 终态，raw status 存 `completion_status`）。
  - `subagent_manager.py`：`SubagentManager(..., workflow_run_resolver=None)` + `_workflow_target_state`；
    `probe_agent`/`read_agent` 前置解析 runId（命中即返回，**不落盘**，不再造孤儿 state）；默认解析器读
    `temp/sessions/*/workflows/<runId>/state.json`。
  - `ga.py`：`do_wait_agent`/`do_read_agent_result` 注入 `agent.workflow_run_resolver`，未知 runId **立即报错**（带 hint，禁止重试同一 id）。
  - `frontends/ink/bridge.py`：`_bind_agent` 绑 `agent.workflow_run_resolver = self._resolve_workflow_run_info`
    （store 权威；**launcher 线程已死且非终态 → 报 `stale`** 兜底，避免僵尸 run 永久阻塞）；
    handoff 补 `failedJobs`（= CC `<failures>`）+ `journalRef` + 「先读 journal 再诊断」提示。
  - `assets/tools_schema*.json`：`workflow_terminal` 语义 + `workflow` 工具描述补「后台任务/完成唤醒/终态集合」。
- 测试：`tests/test_workflow_wait_terminal.py`（12 例）；**对真实卡住的 run 复验** `satisfied=True, terminal_targets=[wf_9af…]`；
  回归 `test_ink_bridge + test_ga_subagent_tools + test_workflow_wait_terminal` → **168 OK**。
- 文档：`docs/20261013-ga-workflow-等待run终态-对齐CC.md`。
- 云端不复刻：**【复刻时暂时不需要参考 claudecode 云端会话的实现】**。
- 状态（2026-10-13）：**已实施**。

### 缺陷 H（2026-10-13）：verify 的 JSON 答案「尾逗号 + 数组误判」→ schema 假失败

- 触发：run `wf_9af50ac554fd43cc9f9d25cc467cd43b` 的 `verify:queens`（agent_6）**答对了**却报
  `schema_validation_failed: missing required field: file/ok/reason`。
- 根因：(a) 答案 JSON 带**尾逗号** `, }` → `json.loads` 失败；(b) `parse_json_answer` 兜底「取第一个 `[` 到最后一个 `]`」
  抓到 reason 里的数组 `[0,4,7,5,2,6,1,3]` → payload 变 `{"result":[...]}` → 缺必需字段。
- 修复：`workflow_child_agent.py` 新增 `_strip_trailing_commas()` / `loads_json_answer()`（容忍 `}`/`]` 前尾逗号、保留字符串内逗号），
  `parse_json_answer` 与 `workflow_scheduler._coerce_structured_text_payload` 改用它。
- 测试：`tests/test_workflow_child_agent.py::StructuredAnswerExtractionTest`（3 例）。
- 状态（2026-10-13）：**已实施**。

### 缺陷 I（2026-10-13）：verifier 传坏路径直接把 job 打死

- 触发：同一 run 的 `verify:html_text`（agent_7）把**工作区根目录本身**当文件传给 `file_read`，
  `resolve_workspace_child` 抛 `WorkspacePathError: path must name a workspace child`，
  `do_file_read` 把 `_get_abs_path` 放在 try 之外 → 异常冒到子代理主循环 → job `failed`。
- 修复：`ga.py` 顶部 `from workflow_workspace import WorkspacePathError`；`GenericAgentHandler.dispatch` 末尾包
  `try/except WorkspacePathError` → 返回**可恢复**工具错误，子代理可自我纠正而不是整个 job 失败。
- 测试：`tests/test_workflow_child_path_recovery.py`（2 例）。
- 状态（2026-10-13）：**已实施**。

### 中途调研 + 实施 2026-10-13：结构化输出对齐 CC（StructuredOutput 工具，取代文本 JSON 解析）

- 触发：用户质疑「GA 要求 workflow 产物是 JSON 是否科学？CC 也这么核验产物吗？」
- **结论：不是。** CC 从不解析自由文本 JSON：
  - schema 约束的是 **agent 的返回值**，**不是磁盘上的产物文件**（产物就是普通文件，无 JSON 校验）；
  - 有 schema 时，CC 强制子 agent 调用 **`StructuredOutput` 工具**（工具 input schema 就是用户 schema），
    **校验在工具调用层**，形状错 → 模型**当轮重试**；脚本拿到的是**已校验对象，无需解析**；
  - 无 schema 时返回值就是**纯文本字符串**。
- CC 证据（仓库 `D:/git_codes/claudecode283_hack`）：
  - `claudecode283/modules/m0227_0xc482590.js`：`bi="StructuredOutput"`；`pe(...)` 定义（`endsTurn:!0`、
    `inputSchema=inputJSONSchema`、ajv 校验 `"Output does not match required schema: …"`）；lint `ze`（`root_not_object`/`missing_type`/`schema too large`）。
  - `claudecode283/modules/m0869_0xd12567a.js`：有 schema 的 system prompt `Jr`/`Vr`（"MUST call the StructuredOutput tool exactly once …
    the script reads ONLY the tool call"）；无 schema 的 `jr`（"final text returned verbatim"）；结构化值以 `attachment.type==="structured_output"` 回收；
    `structuredOutputAttempts`；retry cap `Kn` + `"StructuredOutput retry cap exceeded"`。
  - `claudecode283/extracted/workflow-authoring-reference.md:37,46`（"no parsing needed" / "validation happens at the tool-call layer so the model retries on mismatch"）。
- GA 现状（改前）：`options.schema` → 提示词要求把 JSON 写进**最终文本** → 宿主 `parse_json_answer` 解析 →
  解析失败即 job `failed`（**缺陷 H 的根因**）。
- 修复（对齐 CC）：
  - `workflow_child_agent.py`：`STRUCTURED_OUTPUT_TOOL` + `_with_structured_output_tool`（object 根 schema 时向子 agent 追加
    `structured_output` 工具，`parameters`=该 schema）；`_build_handler` 设 `handler.workflow_output_schema`；
    `_run_tool_job` 回收 `handler.workflow_structured_output`；`_build_success_payload(..., structured=...)` 优先用工具调用值；
    `_structured_output_contract` 改为 CC 口径（"调用 structured_output 工具恰好一次，脚本只读该工具调用"）。
  - `ga.py`：`do_structured_output`——校验通过则存值并 `next_prompt=None` **结束该轮**，不通过则返回错误 + 重试提示（当轮改）。
  - `workflow_permissions.py`：`structured_output` 进 `ALLOWED_READ_ONLY_STATIC_TOOLS`（read_only/verify 档可用）。
  - `skills/workflow-authoring/SKILL.md`：`schema` 说明改为 CC 口径，并**禁止**写「请返回 JSON」这类提示词。
- 保留：文本解析 + `_apply_schema_contract` 作为**回退**（弱模型不调工具时仍可用）。
- 测试：新增 `tests/test_workflow_structured_output.py`（10 例）；`test_workflow_child_agent` 旧措辞断言同步更新；
  回归 `child_agent+structured_output+permissions+scheduler+runtime+integration+capability_preflight+permission_policy` → **277 OK**。
- 端到端自测（真实 tool loop）：`test_workflow_child_agent.StructuredOutputEndToEndTest`（3 例，真实 `agent_runner_loop` + `StubToolClient`）→ **3 OK**；focused 回归 `child_agent+structured_output+permissions+scheduler+wait_terminal` → **154 OK**。
- 文档：`docs/20261013-ga-workflow-结构化输出-对齐CC-StructuredOutput工具.md`。
- 云端不复刻：**【复刻时暂时不需要参考 claudecode 云端会话的实现】**。
- 状态（2026-10-13）：**已实施**。

### 中途调研 2026-10-13：CC dynamic workflow 是异步的；GA Ink「输入框锁死 / 查看卡顿」根因

- 触发：用户实测 GA Ink 跑 workflow 时「输入框无法输入新内容」且「按回车想看 workflow 执行很卡」，并问
  CC 的 dynamic workflow 是否异步。
- CC 结论（异步 = 是）：
  - `m1569_0xdceb889.js @65948 call()` 立即返回 `{status:"async_launched", taskId, runId, ...}`；
    工具定义 `backgrounding:"never"`（`@61381`）——自己起后台任务，不走普通后台化通道。
  - `m0869_0xd12567a.js @81169 kSt()` 是后台 async IIFE，`Io()` `@78839` 在 Node `vm` 里跑脚本。
  - `@70281` 工具结果文案「Workflow launched in background … You will be notified when it completes」→
    主 agent 的回合**可以结束**，不阻塞。
  - `m0567_0xcdd6311.js @5824 RTn()` 用 `task-notification`（`mode:"task-notification"`、`priority:"next"`）
    作为下一条用户消息唤醒主 agent；`BV()` 原子 claim 去重。
  - `m2000_0xe395211.js` 状态行「Running in background · /workflows to monitor and save」是**旁路提示**，
    composer 不锁；`/workflows`（`m1075`）是独立对话框。
- GA 现状：
  - **异步已对齐**：`workflow_launch_from_tool`（`frontends/ink/bridge.py:861`）起后台线程、立即返回
    `{"status":"async_launched", ...}`（`:982`）。
  - **差异 A（现象 1 根因）**：`_run_workflow_runtime`（`:1164`）emit 全局
    `{"type":"status","status":"running"}`（`:1169`）→ Ink 隐藏光标（`App.tsx:1661`）、
    Enter 不提交（`inputController.ts:362`）；handoff 又让会话从启动到 agent 答完整段保持 running。
  - **差异 B（现象 2 根因）**：`workflow_list/detail/progress` 仍在 JSONL stdin 线程**同步**执行
    （`ink_bridge.py:1063/1078/1099`，派发 `:2392/2394/2396`）；状态条只在 `input.trim()===''` 时
    接管 Enter（`App.tsx:1416`）→ 草稿非空时按 Enter 看 workflow **静默无反应**；
    `_watch_workflow_progress`（`:1247`）0.25s 一轮的 `workflow_live`/`workflow_progress`/`agent_snapshot`
    触发 Ink 全树 re-render，并与子 agent 线程争 GIL。
  - **差异 C**：`append_event`（`workflow_store.py:121`）每次追加整读 `journal.jsonl` 求 max sequence（O(n²)）。
- 对齐方向（待实施）：① workflow 不再占用会话 running，改发独立 background-task 事件；
  ② Enter 永远能打开 workflow 面板；③ 查看路径移出命令线程 + `load_run`/`replay_events` 加 mtime+size 缓存；
  ④ 事件→帧节流（对齐 Codex `schedule_frame()`）。
- 详细文档：`docs/20261013-ga-workflow-异步执行与Ink输入卡顿-对齐CC.md`。
- 云端不复刻：**【复刻时暂时不需要参考 claudecode 云端会话的实现】**。
- 状态（2026-10-13）：**调研已落盘，代码改动待实施**。

### 实施结果 2026-10-14：workflow 对齐 CC「异步 background task」（方案 1–4 全部落地）

依据：`docs/20261013-ga-workflow-异步执行与Ink输入卡顿-对齐CC.md` §5（四项对齐方案）。
CC 依据：`claudecode283/extracted/dynamic-workflow-internals.md` §1/§3.4/§7.5/§8/§9.2；
`m1569 @65948 call()` 返回 `async_launched`；`m2000` 渲染「Running in background · /workflows to monitor and save」（旁路提示，不锁 composer）。

1. **workflow 不再占用会话 running 状态**（方案 1）
   - `frontends/ink/bridge.py::_run_workflow_runtime`：删除开头的全局 `status:running` + `activity` emit；删除 `finally` 里的 `activity:None` + `status:idle`（只留 `watch_stop.set()`）；删除 `handoff_queued` 变量。
   - `_queue_workflow_handoff`：在 `put_task` 之后、`thread.start()` 之前新增 `status:running`（handoff 是真实 agent turn，此时才锁 composer；`_consume_display_queue` 末尾会 emit idle）。
   - 效果：workflow 运行期 Ink composer 不再锁死，输入框可正常输入/提交。
2. **Enter 永远能打开 workflow 面板**（方案 2）
   - `frontends/ink/ui/src/App.tsx`（~1416）状态条按键块：空输入 → Enter 开面板；有草稿且 turn running/stopping → Enter 也开面板；有草稿且 idle → Enter 提交草稿（单字母不抢）。
3. **查看路径移出重活**（方案 3，本会话最大发现）
   - 实测根因：`workflow_detail` 原要 7.5–16.8s、emit 2.3 MB（`agent_snapshot` 1.63 MB 来自扫全仓 629 条记录 + 108 run；`workflow_detail` 687 KB）。
   - `workflow_store.list_runs(*, session_id=None)` 新增按 session 作用域 glob（`f"{scope}/workflows/*/state.json"`）。
   - `agent_control_workflow.WorkflowChildAdapter` 新增 `session_id_provider`，`_runs()` 走 scoped。
   - `ink_bridge`：`workflow_list/detail/progress/launch_from_tool/_run_workflow_runtime` 移除 `_emit_agent_read_model()`；`_watch_workflow_progress` 删除 read-model 与节流常量；`workflow_detail.events` 改为 `replay_events(...)[-_WORKFLOW_DETAIL_EVENT_TAIL:]`（=50）。
   - 实测（108 run 的 store，仅 1 个属该 session）：detail 7565–16853ms/2.3MB → **120–144ms/585KB**；list 1525ms/710KB → **85ms/439KB**；progress → **25ms/44KB**。
4. **提示词侧「启动后结束回合」**（方案 4）
   - `ga.py::do_workflow` yield 文案改为「launched ... in the background ... do not wait ... end your turn」。
   - `ink_bridge.workflow_launch_from_tool` 返回 message 同步 CC 口径（含 do not block waiting (no wait_agent)）。
   - `skills/workflow-authoring/SKILL.md` 新增「Do not block on the run」段落。

- 测试（focused，全绿）：`tests.test_ink_bridge` + `test_workflow_store` + `test_agent_control_workflow` → **159 OK**；`npx tsc --noEmit` exit 0；`workflowList.app.test.ts` → 4 pass。
- 新增测试：`test_background_workflow_never_flips_global_session_status`、`test_workflow_detail_does_not_publish_heavy_agent_read_model`、`test_list_runs_scopes_to_session_when_requested`、`App opens the workflow panel with Enter while a running turn holds a draft`。
- 详细文档：`docs/20261014-ga-workflow-异步background-task-实施记录.md`。
- 云端不复刻：**【复刻时暂时不需要参考 claudecode 云端会话的实现】**。
- 状态（2026-10-14）：**已实施**。

### 中途调研 2026-10-14：CC 的 promptPreview / resultPreview = 400 字头部截断（尾部丢弃）

- 触发：用户问 `_workflow_run_payload` 里 job prompt「截断」对应 CC 的什么、截多少、是否丢尾部。
- CC 证据（`claudecode283_hack`）：
  - 预览助手 `Mt(e)`（`m0869_0xd12567a.js @36742`，见 `reconstructed/renamed/m0869.Mt.36742.js`）：
    `function Mt(e){if(e==null)return;let r=(typeof e==="string"?e:b(e)).trim();if(!r)return;return r.length>Co?re(r,Co)+"\u2026":r}`
  - 常量 `Co=400`（同模块 `Co=400`）。
  - 用途：`workflow_agent` 进度事件里 `promptPreview: Mt(U)`、`resultPreview: Mt(te.result)`。
  - 详情面板 `m1460_0xd973957.js`：`rt = a!=="loading" && a?.prompt ? a.prompt : o.promptPreview ?? ""`
    —— 优先用**按需加载**的完整 prompt（`a.prompt`，来自 agent transcript），拿不到时退回 `promptPreview`，
    并显示 `Loading…` / `Not available yet (agent still running).`。
- 结论：
  - **对应 CC 的 `promptPreview` / `resultPreview` 字段**：事件通道只传**预览**，不传全文；
    全文留在 durable record + 按需读取的 transcript 里。
  - **截断长度 = 400 字符**（`Co=400`），超出则取**头部 400** + 追加 `…`（U+2026）。
  - **尾部丢弃**：`r.length > Co ? re(r,Co)+"…" : r` —— 保留前 400 字，其余丢弃、以单个省略号代替；
    全文不丢（在 store / transcript / draft 里）。
- GA 现状：`workflow_store._preview(value, limit=240)` 已用于进度快照的 `promptPreview`/`resultPreview`（240，无省略号）；
  但 `_workflow_run_payload` 仍带**完整** `job.prompt`（最大 ~440 KB/run），面板 `workflowPanel.ts:392` 首选 `job?.prompt`。
- 对齐动作（本轮实施）：`_workflow_run_payload` 把每个 job 的 `prompt` 截成 **400 + `…`**（CC 口径），
  durable `state.json` 仍保留全文；面板无需改（仍首选 `job.prompt`，只是变成 400 预览）。
- 详细文档：`docs/20261014-CC-workflow-promptPreview截断机制.md`。
- 追加（同日）：**统一**三处预览为同一 CC 口径 —— `workflow_store.PREVIEW_CHARS=400` + `preview_text()`；
  `WorkflowStore._preview`、`ink_bridge` job prompt、`agent_control_workflow.read_result` 全部委托它
  （进度快照 `promptPreview`/`resultPreview` 由 240/无省略号 → 400 + `…`）。测试 228 OK。
- 云端不复刻：**【复刻时暂时不需要参考 claudecode 云端会话的实现】**。

### 实施 2026-10-14：workflow_list 轻量 payload（对齐 CC m1906 历史列表）

- 触发：上一轮遗留项「workflow_list 轻量 payload」。
- CC 证据：
  - §8.6（`m0571_0xcddb07e.js`）：`G7r()` 把 `{runId, timestamp, result, agentCount, logs,
    workflowProgress, totalTokens, totalToolCalls, status, ...}` 写成
    `<projectsDir>/<sessionId>/workflows/<runId>.json` 快照；`psr()` 读回全部 `.json`
    按 `startTime` 倒序，供 `/workflows` 列表展示 —— 即**列表读的是小快照，不是完整 run 记录**。
  - `m1906_0xe19cc02.js`：列表行 = `{label: t.workflowName ?? t.summary ?? t.description,
    icon: Bt[t.status], meta: [时间, t.agentCount + "agent", totalTokens + "tok"]}`。
- GA 现状（改前）：`workflow_list` 对每个 run 发**完整** `_workflow_run_payload`（含 script 之外的全部
  字段：jobs 全量 metadata、artifactDir、resultRef、error 等）。全仓 108 run 的 list payload ≈ **4.5 MB**。
- 对齐动作：`frontends/ink/bridge.py` 新增 `_workflow_run_summary(run)`，`workflow_list` 改用它。摘要只保留
  列表行 + 状态条真正读取的字段：
  - `runId` / `sessionId` / `status`；
  - `metadata` 子集：`workflowName` / `workflowTaskType` / `integrationStatus` /
    `finalAuditStatus` / `launchedBy`；
  - `jobs` 子集：每个 job 仅 `jobId` / `status` / `metadata.{label, tokenUsage}`。
  - 丢弃：`script` / `jobs[].prompt` / `artifactDir` / `resultRef` / `error` / 其它 job metadata。
- 实测（temp/sessions 下 108 run）：完整 4,512,030 B → 摘要 **95,374 B（−97.9%）**；
  最大 session 单 run：232 KB → 2.4 KB。
- 面板无需改：`workflowListRunRow` 只用 `runId/status/metadata.workflowName|workflowTaskType/jobs.length`；
  `workflowStatusBarFromState` 只用 `metadata.workflowName|workflowTaskType|integrationStatus|finalAuditStatus`
  与 `jobs[].{status,metadata.label,metadata.tokenUsage}` —— 摘要全部覆盖。
- 测试：新增 `test_workflow_list_payload_is_lightweight`（断言摘要字段集、不含 prompt/script/model、<600B）；
  `ink_bridge+workflow_store+agent_control_workflow` → **163 OK**；`workflowList.app.test.ts`+`workflowStatusBar.test.ts` → 16 pass。
- 详细文档：`docs/20261014-GA-workflow列表轻量payload对齐CC.md`。
- 云端不复刻：**【复刻时暂时不需要参考 claudecode 云端会话的实现】**。
- 状态（2026-10-14）：**已实施**。

### 测量 2026-10-14：事件→帧节流（Codex schedule_frame()）收益验证 —— 结论：暂不实施

- 触发：遗留项「事件 → 帧节流」，用户要求先量一版验证收益。
- 探针：`frontends/ink/ui/scripts/_probe_workflow_frame_cost.ts`
  （真实 `<App>` + 假 bridge，计时 `emit()`，统计 stdout 写次数=帧数）。
- 关键实测：
  - Ink **每个 `dispatch` 同步写一帧**：同步 burst 20 事件 → 20 次 write（N 事件 = N 帧，未自动合并）。
  - `<Static>` 让 transcript 长度**不影响**单帧成本（20 vs 1500 条几乎一样）；每帧只重绘 live chrome。
  - 单帧 median：状态条 ~1.3–1.8ms（~443B）；面板打开 ~4.2ms（~584B）。
  - 运行期事件速率 ≤ **8/s**（`_watch_workflow_progress` interval=0.25，每 tick ≤2 事件）。
  - 换算：现状最坏 4.2ms×8 ≈ **3.3% CPU**；合并 4:1 只省 ~1–2.5%。
- 结论：**收益可忽略，复杂度/风险 > 收益，暂不实施**；真正解决卡顿的是已修的四条
  （composer 锁、detail/list 大 payload、watcher 的 load_run/json.dumps、list 全量 payload）。
- 重估触发条件：单帧 >20ms、常态事件 >50/s、或出现每帧重绘大 transcript/大表格的新 UI。
- 详细文档：`docs/20261014-GA-事件帧节流收益测量.md`。
- 云端不复刻：**【复刻时暂时不需要参考 claudecode 云端会话的实现】**。
- 状态（2026-10-14）：**已测量，判定不做**。

### 中途调研 2026-10-14：CC「worktree 隔离」= 同相对结构的工作副本 + 显式提示词（不是空目录）

- 触发：用户「开始 而且你需要先调研 CC怎么做的 因为这个机制很关键」——继续深挖 CC 的多 agent 冲突机制
  （worktree 隔离 + 值传递，不靠冲突检测）。
- 本轮新证据（CC 2.1.283，仓库 `D:\git_codes\claudecode283_hack`）：
  - **创建**（`modules/m0869_0xd12567a.js`）：`Je=null` 默认不开；`ve?.isolation==="worktree"` 才 `Je=await vt(...)`；
    命名 `<runId|label>-<agentId>` 或 `wf-<agentId>`；`worktreePath:fn` 透传给 child；内置 web-fetch agent 强制忽略。
  - **提示词（关键）**：CC 给隔离 child 注入原文 ——
    `m0869`：「You are running in an isolated git worktree at `<path>` (a separate working copy of the repo).
    Changes you make here do NOT affect the main working directory ... or other agents. Work normally — the worktree
    will be cleaned up automatically if you made no changes, or preserved for review if you did.」；
    `m0357`：「isolated git worktree at `${n}` — **same repository, same relative file structure, separate working copy**.
    **Paths in the inherited context refer to the parent's working directory; translate them to your worktree root.**
    **Re-read files before editing if the parent may have modified them since they appear in the context.**」
  - **生命周期/守卫**（`m0531` / `m0511` / `m0319`）：`worktree_created`/`kept`/`removed`；`qo()` @4333 数
    `changedFiles`+`commits`；移除 fail-closed（非 owner / 无法验证 / 有未提交或未合并 → 拒绝，要 `discard_changes:true`）；
    `git worktree remove --force`。**`movedToChangeset` 是云端，不复刻。**
  - **值传递默认**（`extracted/workflow-authoring-reference.md`）：无 schema 返回文本，有 schema 返回**已校验对象**；
    `isolation:'worktree'` 是 EXPENSIVE 选项，仅并行改文件时用，unchanged 自动删。
  - **否定证据**：`grep -rl "artifactCollision\|artifactOwnership" claudecode283/modules/` → **0 命中**（CC 无事后冲突检测）。
- GA 差距（本轮开工前）：隔离目录是**空目录**（`_isolated_workspace` 只 mkdir）→ 隔离 child 读不到共享 workspace
  已有文件；提示词只有一句，缺 CC 的「同相对结构 / 路径换算 / 编辑前重读」语义。
- 结论：GA 要补两处 ——（1）隔离目录在**创建时预填一份共享 workspace 的工作副本**（排除 host 自有目录、有上限、幂等）；
  （2）隔离 child 提示词对齐 CC 三条语义。清理/守卫/值传递**已对齐**，不动。
- 云端不复刻：**【复刻时暂时不需要参考 claudecode 云端会话的实现】**。
- 详细文档：`docs/20261014-ga-workflow-worktree隔离-CC机制补充调研.md`。
- 状态（2026-10-14）：调研已落盘；实施见下方小节。

### 实施 2026-10-14：隔离目录预填「工作副本」+ 提示词对齐 CC

- 目标：把 GA 隔离 child 的私有目录从「空目录」改成 CC 的「同相对结构工作副本」，并让提示词讲清路径换算/重读。
- 改动 1（`workflow_child_agent.py`）：`_isolated_workspace(job)` 仅**新建**时调
  `_seed_isolated_workspace(job, target)` —— 用共享 workspace 的**启动快照**按同相对路径预填；
  排除 `HOST_OWNED_WORKSPACE_DIRS`（`__pycache__`/`.git`/`workflow-handoffs`）；入口上限
  `_ISOLATED_SEED_MAX_ENTRIES=20000`；任何 `OSError` 吞掉（最坏空/部分目录，不失败 job）。
  只复制启动那一刻 → 并发 sibling 后续写入不可见，隔离保证不变；diff 仍以隔离目录为准。
- 改动 2（`workflow_child_agent.py` `_build_prompt`）：隔离分支文案改为 CC 口径 ——
  「isolated working copy / same relative file structure / 继承上下文路径要换算到 workspacePath root /
  编辑前重读 / 改动不影响主工作区与其它 agent / 产物按相对路径写会被回收」。
- 改动 3（`skills/workflow-authoring/SKILL.md`）：`isolation` 条目补「working copy、启动时预填、路径换算」。
- 测试（focused，未跑 full）：`tests/test_workflow_child_agent.py` 新增 2 例（工作副本预填 + 提示词语义）；
  `pytest tests/test_workflow_child_agent.py tests/test_workflow_isolation_cleanup.py -q` → **69 passed, 6 subtests**；
  `pytest tests/test_workflow_implicit_isolation.py tests/test_workflow_workspace.py tests/test_workflow_scheduler.py -q`
  → **101 passed, 10 subtests**。
- 云端不复刻：**【复刻时暂时不需要参考 claudecode 云端会话的实现】**。
- 详细文档：`docs/20261014-ga-workflow-worktree隔离-CC机制补充调研.md`、
  `docs/20261014-ga-workflow-隔离工作副本-实施记录.md`。
- 状态（2026-10-14）：**已实施，focused 测试通过**。

### 实施 2026-10-14：workflow 进度计数修复（6/6 显示 3/6）+ /workflows 详情美化

- 触发：用户实测「6 个 agent 都执行完毕，但输入框上方状态仍显示 3/6」，并要求美化 `/workflows` 详情（`|` 前列宽对齐）。
- 排查证据：抽查 `temp/sessions/*/workflows/*/workflow-progress.json` 与 `run.json`，**磁盘快照是对的**（`wf_66e2…` 6/6、`wf_39565…` 6/6）→ 不是后端算错，是 **UI 显示陈旧快照**。
- 根因 A（前端信错来源）：`workflowStatusBar.ts` 优先用 `workflowDetails[runId].progress.workflowProgress`（轮询快照，可能停在 3/6），忽略权威 `run.jobs`。
- 根因 B（后端 emit 竞态）：`ink_bridge._run_workflow_runtime` 的 `watch_stop.set()` **不等待** watcher；`_watch_workflow_progress` 若已越过 `stop.wait()`，会把**旧**快照 emit 在终态 emit **之后**，覆盖回 3/6。
- 修复：
  - 后端 `_stop_watcher()`：`set()` 后 `watcher.join(timeout=2.0)`，让 watcher 最后 emit 严格早于终态 emit（三处 `watch_stop.set()` 全改）。
  - 前端 `workflowStatusBar.mergeAgentStates` + `workflowPanel.moreSettledStatus`：按 jobId 合并快照与 `run.jobs`，**取更 settled 的状态**（settled 3 > running 2 > queued 1 > registered 0）→ 计数单调。
- 美化（`workflowOverviewRows`）：列宽按内容算（`string-width`，CJK 正确）+ 分隔符 `|`→`│` + phase 字形完成 `✓`/未完成 `◌`；`|` 在所有行严格对齐。
- 测试：前端 `npm test` **410 passed**、`tsc --noEmit` 0；后端 `pytest tests/test_ink_bridge.py tests/test_workflow_runtime.py -q` **233 passed, 20 subtests**。
- 云端不复刻：**【复刻时暂时不需要参考 claudecode 云端会话的实现】**。
- 详细文档：`docs/20261014-ga-workflow-进度计数修复-与workflows详情美化.md`。
- 状态（2026-10-14）：**已实施，focused 测试通过**。

### 修复 2026-10-14：会话模式（standing opt-in）不再对寒暄轮自动编排（对齐 CC ultracode）

- 触发：用户开过 `/workflow on` 后只输入 `wondeful!`（口语致谢），GA 却重新启动了一轮 workflow 编排。
- 证据：`temp/sessions/session_23910f598e26450f9683b238a1c54c44.jsonl`（2026-10-14）turn 3 用户实际只打
  `wondeful!`，却被包成 `[WORKFLOW OPT-IN]`（reason=`explicit session workflow mode`）→ turn 4 又一次 `workflow_handoff`。
- 根因：`workflow_activation.py::resolve_workflow_activation()` 的 **session 分支**只要 `session_enabled` 就无条件
  返回 `session/requested`，任何消息（含寒暄）都被注入 `workflow_opt_in_reminder()` → 主代理去直写脚本、启动 workflow。
- CC 对照（`claudecode283/extracted/workflow-authoring-reference.md:16`，**Ultracode** 段）：
  standing opt-in「author and run a workflow for **every substantive task** … **Solo only on conversational
  turns or trivial mechanical edits.**」→ GA 漏了「寒暄轮不编排」这条 gate。
- 修复（`workflow_activation.py`）：session 分支先判 `_is_conversational_turn(text)`，命中则返回 `none/none`；
  新增 `_is_conversational_turn` / `_has_task_signal` / `_CONVERSATIONAL_WORDS`（英文 ack + 中文客套词）/
  `_TASK_TOKENS`（重构/优化/修复/refactor/fix…）/ `_CJK`。判定保守：短 CJK 词一律交给模型，避免误杀「重构」类真任务。
  **只作用于 session 分支**——显式 `/workflow <task>`、自然语言 `用 workflow …`、`_EXPLICIT_FANOUT`、semantic
  分支都在 gate 之前返回，行为不变。
- 测试（focused）：`tests/test_workflow_activation.py` 新增 3 例 → `pytest tests/test_workflow_activation.py -q`
  **7 passed, 6 subtests**；`pytest tests/test_ink_bridge.py -q -k "opt_in or workflow"` **73 passed, 53 deselected**。
- 不变量：**能力 ≠ 授权**；session opt-in 只对 substantive task 生效，寒暄轮 solo 回应。
- 云端不复刻：**【复刻时暂时不需要参考 claudecode 云端会话的实现】**。
- 详细文档：`docs/20261014-ga-workflow-会话模式寒暄误触发-修复.md`。
- 状态（2026-10-14）：**已实施，focused 测试通过**。

### 方案 2026-10-14：GA Agent Team 复刻（对齐 Claude Code 2.1.283）

- 目标：把 CC 的 **Agent Team**（协调者 lead + 具名 teammate + 名字寻址消息 + 共享任务板）复刻进 GA，
  让多 agent 协作从「父代理派活、子代理各干各的」升级为「lead + teammate 在共享任务板上认领、用名字互发消息」。
- **充分借鉴 CC 设计**，不自行发明；云端不复刻：**【复刻时暂时不需要参考 claudecode 云端会话的实现】**。
- CC 源码调研（证据源在 `D:\git_codes\claudecode283_hack`）：`docs/20261014-复刻agent-team指南.md`
  （本仓库也有一份副本）；另见 `claudecode283/extracted/agent-team-internals.md`、
  `reconstructed/deminified-agent-team/`、`claudecode283/extracted/hooks-internals.md`。
- **GA 现状：已有大半**（别重造）—— `spawn_agent`（具名）、`send_message`/`followup_task`、
  `SubagentMailbox`（TRIGGER_TURN/QUEUE_ONLY + 去重 + 回执）、父收件箱、`SubagentRegistry`
  （`registry.json` + lock，≈ CC team 文件）、`SubagentEventBus`、`subagent_worktree`（隔离）、
  `subagent_capabilities`（能力边界）、`subagent_prompts`（root/child hint）。
- **差距（要补的 5 件）**：G1 共享任务板（最高，无 `TaskCreate/Get/Update/List`）；
  G2 teammate 无法自主协作（`ORCHESTRATION_TOOLS` 把 `send_message`/任务工具从子代理剥离）；
  G3 无 team/lead 身份；G4 提示词非 coordinator/teammate 模型；G5 通知格式未对齐。
- **实施顺序（不可颠倒）**：P0 任务板 `subagent_tasks.py` + 4 工具 → P1 team/lead 身份（复用 registry）
  → P2 放开 teammate 的 `send_message` + 任务工具（**仍禁 `spawn_agent` 递归**）→ P3 coordinator/teammate
  提示词 → P4 通知对齐 `<task-notification>` + 防伪前置。
- **不变量**：能力≠授权（teammate 只在显式要求/SOP 下起）；子代理不再递归 fan-out；值传递（teammate 看不到
  lead 对话）；任务「真的完成」才置 completed；`TeamCreate`/`TeamDelete` **不做**（本 build 无定义，隐式单例团队）。
- 详细方案：`docs/20261014-ga-agent-team-复刻实施方案.md`。
- 状态（2026-10-14）：**方案已落盘，待实施**（尚未改代码）。

### 中途调研 2026-10-16：GA 项目目录扁平化 / 对齐 CC 的逻辑分层

- **触发**：GA 根目录现有 73 个 `*.py`（其中 `workflow_*.py` 18 个、`subagent_*.py` 25 个、LLM/API 相关 6 个），
  LLM、subagent、workflow、web、media、runtime 实现全部平铺在仓库根目录，入口、实现和兼容代码混在一起；
  用户要求先调研 Claude Code 2.1.283 源码，再落盘一份可执行的目录重构方案。
- **CC 源码坐标与证据**：
  - `D:\git_codes\claudecode283_hack\claudecode283\extracted\README.md` §“模块地图”：CC 的 workflow 工具、引擎、
    parser、launch 状态机、详情对话框、`/workflows` 历史列表、状态行分别落在 `m1502` / `m1569` / `m0869` /
    `m0862` / `m1372` / `m1460` / `m1906` / `m2000` 等模块。
  - `claudecode283\extracted\dynamic-workflow-internals.md`：工作流入口、执行引擎、VM、持久化、resume、
    UI/交互被明确拆成不同职责层；其中 §9 覆盖 `/workflows`、详情进度树、历史重跑。
  - `claudecode283\extracted\agent-team-internals.md` §5：`spawnTeammate`、Agent 工具、InProcessTeammateTask、
    TaskCreate/Update/List/Get、SendMessage、team 文件持久化分别有独立模块职责。
  - `claudecode283\extracted\hooks-internals.md` §1：hooks 执行引擎集中在共享 chunk，但匹配、spawn、超时、
    异步注册表、结果分发、安全门禁仍是可辨识的逻辑层。
  - `claudecode283\modules\INDEX.tsv`：`mNNNN_0x...js` 是 Bun `--compile` 提取索引，不是原始源码目录名；
    **CC 原始目录树不可恢复**。本方案只复刻 CC 的逻辑分层，不伪造 `claudecode/...` 路径。
- **结论**：GA 应建立 `genericagent/` 主包，按 `core` / `llm` / `mcp` / `agents` / `workflow` / `web` / `media`
  分层；`ga_cli` 作为稳定 CLI 门面保留，根目录只保留薄入口与兼容 shim。迁移必须先做路径解析层和兼容层，
  再按领域分批 `git mv`，不得一次性大爆炸。
- **对 GA 的影响**：所有依赖 `__file__` 定位 `assets/`、`skills/`、`temp/`、`mcp.json`、`llm.yaml`、JS worker
  的代码都必须改走 `genericagent.paths`；subagent 的 `-m genericagent.core.app` 启动路径、`workflow_js_worker.js` /
  `workflow_script_contract.js` 资源路径、`pyproject.toml` 打包配置和测试中的旧模块名都是高风险点。
- **待办**：先实施 P0 盘点与结构检查，再按 `docs/20261016-ga-project-structure-reorg-plan.md` 的 P1–P7
  分批迁移；每批单独提交、单独验证，旧根模块通过兼容 shim 过渡，最后再清理。
- **云端边界**：**【复刻时暂时不需要参考 claudecode 云端会话的实现】**
- **详细方案**：`docs/20261016-ga-project-structure-reorg-plan.md`。
- **实施结果（2026-10-16，P0–P5 主体完成）**：
  - 新增 `genericagent/` 主包与 `genericagent/paths.py`；73 个根级实现 `*.py` 已迁入 `core/llm/mcp/agents/workflow/web/media` 等子包。
  - `workflow_js_worker.js` / `workflow_script_contract.js` 迁到 `genericagent/workflow/js/`；`assets/code_run_header.py` 迁到 `genericagent/resources/`；资源定位统一走 `paths.py`。
  - `pyproject.toml` 改为 `ga_cli* + genericagent*` 包发现并配置 JS/resources/templates package-data；`ga_cli/__init__.py` 改从 `genericagent.core.handler` 重导出稳定符号。
  - 新增 `scripts/check_project_structure.py`，P7 后通过：`project structure ok (root allowlist: mykey.py; canonical frontends present)`。
  - 验证：`compileall`、`python -m genericagent.core.app --help`、`python -m ga_cli list`、关键 Python focused tests 308 OK、p8 provider anomaly + ink bridge 139 OK、Ink UI `npm test` 431 pass、`tsc --noEmit` 通过。
  - 契约修正：p8 旧测试曾要求 workflow 尾部发 `idle`，与 CC 对齐后的后台任务契约冲突；现统一断言**后台 workflow 不得翻转全局 session status**，避免锁住 Ink composer。
  - P6 前端目录整理已完成：Ink、TUI、桌面、Web、Bot、ACP 适配器按运行形态迁入 `frontends/{ink,terminal,desktop,web,bots,acp,common}/`。P7 已完成：旧根级 shim 与 `frontends/` 顶层 shim 已删除，所有内部 import、patch target、subprocess 入口和结构门禁已迁移到 canonical 路径。
  - P6 Ink 前端迁移已完成：`frontends/ink_bridge.py` → `frontends/ink/bridge.py`，`frontends/ink-ui/` → `frontends/ink/ui/`；旧 `frontends/ink_bridge.py` 已删除，Ink 相关调用统一使用 `frontends.ink.bridge`。
  - Ink UI 测试命令改为 `cd frontends/ink/ui && npm test`；`ga_cli/cli.py`、`install_ink_ui.cmd`、scripts/probes、测试与 README 路径已同步。验证：相关 Python 165 tests OK，Ink UI 431 pass，`tsc --noEmit` 通过。
  - 结构门禁检查 canonical Ink/前端路径必须存在，旧 `frontends/ink-ui/`、根 shim 与前端顶层 shim 必须不存在，防止目录再次扁平化。
  - P7 打包验证：`python -m pip wheel . -w temp/wheels --no-deps` 成功；`genericagent-0.1.0-py3-none-any.whl` 含 89 个 Python 模块及 `workflow/js/worker.js`、`script_contract.js`、`resources/code_run_header.py`，required 资源 0 missing。根 shim 已删除，内部调用已全部迁移到 canonical 路径。
  - 真实 LLM E2E（2026-10-16，目录重构后）：默认 `default/deepseek-flash`，显式 `/workflow` 完成 4/4 job（2 路并行版本探测 + 串行写表 + 独立 verify），runId `wf_18a215a93a2942dcad977d81444c411a`，status `succeeded`，产物 `temp/workflow-runs/wf_18a215a93a2942dcad977d81444c411a/workflow-e2e-version-table.md`。证据见 `docs/20261016-GA-目录重构后真实workflow-e2e.md`。
  - P6 其他前端迁移：`frontends/{common,terminal,desktop,web,bots,acp}/` 已建立并迁移实现；旧 `frontends/*.py|*.pyw` 顶层路径保留 alias shim，`launch.pyw`、`assets/configure_mykey.py`、README/AGENTS/CLAUDE 与结构门禁同步更新。验证：全量 Python 1585 tests OK（3 skipped）、Ink UI 431 pass、`tsc --noEmit` 通过、结构门禁通过；L1 真实 API bridge 自测 5/5 PASS（`default/deepseek-flash`，证据包 `temp/ink-selftest/20261016-181824`）；真实 workflow strict-schema E2E 通过（`wf_strict_schema_e2e`，1/1 job succeeded，`schemaValidation: true`）。
  - 详细实施记录：`docs/20261016-ga-project-structure-reorg-plan.md` §13。
