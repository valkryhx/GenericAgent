# GA ink 真实 LLM API e2e 自测 SOP

> 目的：把「用 GA ink + 真实 LLM API 做端到端自测」这件**反复发生**的事固化成可复用流程。
> 真实自测**不能**靠预先固定的期望输出来判定（模型输出天然不确定），必须靠
> **证据（事件流 / 日志 / 产物 / 真实 UI 帧）+ 不变量断言**来判断。
>
> 适用对象：后续任何 agent / 人在改动 GA ink、bridge、workflow、agent team、UI 后自测。

---

## 0. 环境铁律（先读）

- **Windows 上所有命令走 Git Bash**：`exec_command` 默认是 PowerShell，必须显式 `shell="bash"`、`login=false`。
- 中文/UTF-8：命令前 `export LANG=C.UTF-8; export LC_ALL=C.UTF-8`。
- Python 用 `python`（不是 `python3`）。
- **不要**用 `python -c "中文"` 往返中文（会坏编码）；写文件用 `cat > f << 'EOF'`。
- **heredoc 会吃反斜杠**：源码里需要 `\` 时用 `chr(92)` 构造，或事后用 Python 补回。
- 单条 heredoc 过长会被截断：长文件分块 `cat > f` / `cat >> f` 写。
- 删除单文件用 `python -c "import os; os.remove('x')"`，绝不 `rm -rf`。

---

## 1. 三层自测模型（按成本从低到高）

| 层 | 名字 | 驱动什么 | 需真实 LLM？ | 判定对象 | 入口 |
| --- | --- | --- | --- | --- | --- |
| **L1** | 事件级（bridge） | 真实 `GenericAgentBridge` + 真实 agent | 可选 | 事件流 / token / 产物文件 | `tests/real_ink_selftest.py` |
| **L2** | UI 版面级 | 真实 `App`（伪 TTY）+ 真实 bridge | 否（布局）/ 可选 | 渲染帧（宽度/边框/面板文本） | `frontends/ink-ui/scripts/real_ink_*.ts` |
| **L3** | 交互式手动 | 真实终端里跑 `./ga` | 是 | 人眼 + 会话产物 | `./ga` |

**推荐顺序**：先 L2 快速验证 UI 改动没把版面搞坏 → 再 L1 跑一次真实 LLM 回合确认链路 →
需要体感时 L3。三者互补：L1 看「结果/日志对不对」，L2 看「版面对不对」，L3 看「整体手感」。

---

## 2. 凭据与模型（不改动、不外泄）

- 模型/密钥来自仓库内 `mykey.py`（真实配置文件，**不要提交、不要打印内容**）。
- 列出现有模型（不泄露密钥）：

  ```bash
  cd frontends/ink-ui && timeout 120 npx tsx scripts/_probe_models.ts
  ```

  输出形如 `14 "cc-deepseek-v4.1-flash-chat/deepseek-flash"`，`CURRENT` 标注当前模型。
- 真实 API 自测一律**显式 opt-in**：`GA_RUN_REAL_API_E2E=1`（不加则只跑不需要模型的结构检查）。
- 选模型：L1 用 `GA_SELFTEST_MODEL=<selector>`；L3 在 UI 里 `/model <selector>`。

> 安全：自测产物落在 `temp/`（gitignored）；不要读取/写入受保护文件；报告里不要贴密钥。

---

## 3. L1：事件级真实自测（`tests/real_ink_selftest.py`）

### 3.1 跑法

```bash
# 结构检查（不调模型，秒级）：bridge 起来 + 模型表 + /team 事件
python tests/real_ink_selftest.py

# 真实 LLM 回合（opt-in）：提交一个极小 prompt 并等待 assistant_done
GA_RUN_REAL_API_E2E=1 GA_SELFTEST_TIMEOUT=240 python tests/real_ink_selftest.py

# 指定模型 / prompt
GA_RUN_REAL_API_E2E=1 GA_SELFTEST_MODEL=deepseek-v4.1-flash \
  GA_SELFTEST_PROMPT='Reply with exactly one word: PONG' python tests/real_ink_selftest.py
```

### 3.2 它检查什么（不变量，非固定输出）

1. `model_status` 能列出模型，且有 `current`（证明配置/凭据可用）。
2. `team_list`（`/team`、`/agents` 的后端）emit `team_roster`，含 `team/members/tasks` 三键。
3. （opt-in）真实回合产出非空 `assistant_done` 文本；`token_usage` 有值；无 `error` 事件。

### 3.3 证据包（每次运行落一份）

```
temp/ink-selftest/<YYYYMMDD-HHMMSS>/
  events.jsonl          # 完整 bridge 事件流（已 sanitize）
  backend-log-tail.txt  # temp/ink_bridge_backend.log 末 80 行
  summary.json          # 每项检查 PASS/FAIL + 耗时 + eventCounts + turn 摘要
```

**判读**：先看 `summary.json` 的 checks；再看 `events.jsonl` 里 `assistant_done.text` /
`token_usage` / `error`；异常时看 `backend-log-tail.txt`（栈、MCP、重试）。

> 注意：GA agent 会**劫持 `sys.stdout`**，所以脚本用 `sys.__stdout__` 打印；证据一律落文件，
> 不要依赖终端回显（否则会出现「命令 exit 0 但什么都没打印」的假象）。

---

## 4. L2：UI 版面级真实自测（真实 App + 伪 TTY）

这类脚本用 `ink.render(<App/>)` 渲染**真实 App**，配 `CaptureWriteStream`（伪 TTY，80×24）+
`FakeReadStream`（注入按键），把渲染帧截下来断言。**不需要真实 LLM**，秒级、确定性。

### 4.1 现有脚本

```bash
cd frontends/ink-ui
npx tsx scripts/real_ink_team_panel_e2e.ts     # /team 面板：头部 + 任务板 + 页脚 + 宽度
npx tsx scripts/real_ink_ui_user_input_e2e.ts  # 输入/光标/子代理/workflow 全链路（需真实 API）
npx tsx scripts/real_ink_team_swarm_e2e.ts     # agent team 全链路：输入框驱动 + 真实 API + 默认模型
```

`real_ink_team_panel_e2e.ts` 的检查（`GA_TEAM_E2E_DEBUG=1` 可打印帧）：

- `/team` 命令发出后 bridge 返回 `team_roster`；
- 渲染帧含 `Team … lead team-lead`、`Task board`、`Esc close`；
- 任意一行宽度 `< 80`（不溢出）。

> ⚠️ **模型绑定（实测教训）**：`real_ink_ui_user_input_e2e.ts` **为 luna profile 硬编码**——
> `GA_UI_EXPECTED_PROFILE` 默认 `luna`，且注入给子代理的 prompt 里**写死** “luna index N”。
> **用默认 `deepseek-flash` 跑会在子代理环节失败**：报
> `AssertionError: subagent child did not reach succeeded in N read-model refreshes`，
> 且 `temp/subagents/registry.json` 里该子代理**停在 `running`**（未进 `succeeded`）。
> 结论：**默认模型下的真实 UI 自测用 L1（`real_ink_selftest.py`，走默认模型）+ L2（`real_ink_team_panel_e2e.ts`，仅版面）**；
> 只有在显式切到 luna 时才跑这个重型全链路脚本。

> ℹ️ **agent team e2e（`real_ink_team_swarm_e2e.ts`）实测结论（默认 `deepseek-flash`）**：
> - 从**输入框**输入一个硬任务（先 `task_create` 建任务板，再 `spawn_agent(team_mode=true)` 起 3 个 teammate），
>   脚本断言：≥2 个 teammate 的 `state.json` 里 `capability_options.team_mode=true` 且 `turn_status=completed`、
>   共享任务板 `temp/teams/<list_id>/tasks.json` 有 ≥2 条任务、`report.md` 已写。
> - **`/team` 名册只列「活跃」成员**（`include_closed=False`）：lead 读完结果就 `close_agent` 释放槽位，
>   所以**跑完后名册 memberCount=0 属正常**；持久的是共享任务板（`/team` 仍会显示 tasks）。别把它当 bug。
> - 实测团队正确产出（70 / 107 / 28）并**自纠**：首轮误用 `agent_type=explore` 被裁成只读档导致阻塞，
>   lead 关闭旧 teammate、任务重置 pending、改用 generic 档重启后成功——这正是 agent team 的价值。

### 4.2 判读帧的关键坑（必读）

- **Ink 是差分渲染**：`stdout.chunks` 是逐帧写入，不是「最后一块 = 整屏」。
  取帧要用「最后一个 chrome 帧」启发式（含 `Enter send`/`GenericAgent` 的块），见脚本。
- **面板必须有行预算**：`App` 里 `computeLayoutMetrics` 的 `panelRows` 决定 bottom chrome 高度；
  新面板若只加进 `activePanel` 而**没加进 `panelRows` 分支**，面板会被裁成 0 行 → 版面乱。
  （本次 P10 就踩了这个坑：`/team` 面板被裁成一行，加了 `teamPanelRows.length` 才正常。）
- 断言只查**结构不变量**（宽度、边框、关键文本），不要断言整屏像素级内容。

---

## 5. L3：交互式手动自测（真实终端）

```bash
cd /d/git_codes/GenericAgent
./ga                 # Git Bash；Windows 上也可 ga.cmd
```

清单（每项都看**真实产出**，不看「感觉」）：

- [ ] `/model` 列出模型并能切换；切换后 `model_status` 的 `current` 变了。
- [ ] 普通回合：输入一句 → 有流式输出 → 结束后状态回 `idle`，底部有 token 统计。
- [ ] `/team`（或 `/agents`）：面板出现，名册 + 任务板正确；`↑/↓` 选成员、`r` 刷新、`Esc` 关闭。
- [ ] `/workflows`：列表 + 详情；状态 X/Y 计数正确。
- [ ] 触发子代理 / workflow：看 `temp/subagents/`、`temp/sessions/` 产物是否落盘、内容是否合理。
- [ ] 输入框在面板打开/关闭、运行中是否仍可输入（不卡、不串行）。

---

## 6. 证据位置速查（判读时的「真凭实据」）

| 证据 | 路径 | 说明 |
| --- | --- | --- |
| 后端日志 | `temp/ink_bridge_backend.log` | bridge/agent 的 stdout/stderr（栈、MCP、重试） |
| L1 证据包 | `temp/ink-selftest/<ts>/` | events.jsonl + summary.json + 日志尾 |
| 子代理注册表 | `temp/subagents/` | `registry`、`inbox.jsonl`、`events.jsonl`、`mailbox.jsonl` |
| 任务板 | `temp/teams/<list_id>/tasks.json` | `task_create/task_update` 的共享任务板（list_id = `session-<sid[:8]>`） |
| 会话/workflow | `temp/sessions/`、workflow store | 运行记录、产物引用 |
| 每 agent 目录 | `temp/<name>/` | `output.txt`、`state.json`、`reply.txt`、`handoff` |

---

## 7. 判读原则（无固定期望值时怎么下结论）

1. **结构不变量优先**：字段存在、类型正确、状态机单调（queued→delivered→consumed）、
   计数自洽（X/Y、memberCount、job 数）。
2. **横向对比**：同一 prompt 跑两次，看是否稳定；改动前后对比事件序列。
3. **产物验证**：模型说「已写入 X」→ 去磁盘**打开 X 看字节**，不看模型自述。
4. **日志交叉**：事件流说成功但日志有异常 → 以日志为准。
5. **失败要可复现**：记录 runId / 证据包路径 / 命令，写进缺陷 md。

---

## 8. 常见故障签名 → 定位

| 现象 | 可能原因 | 先看 |
| --- | --- | --- |
| 命令 exit 0 但无输出 | agent 劫持 `sys.stdout` | 用 `sys.__stdout__`；看落盘证据 |
| 面板内容错乱/挤成一行 | `panelRows` 未加新面板分支 | `App.tsx` `computeLayoutMetrics` 调用处 |
| 面板文本断言失败 | Ink 差分渲染，取错帧 | 取「最后一个 chrome 帧」 |
| `wait_agent` 一直等 | 子代理终态未写 / 条件不匹配 | `temp/subagents/*/events.jsonl` |
| 重型 UI e2e 报 `subagent … did not reach succeeded` | 脚本为 luna 硬编码，用默认模型跑 | 改用 L1+L2；或显式切 luna |
| `/team` 跑完后 memberCount=0 但 taskCount>0 | 正常：名册只列活跃成员，teammate 已被 lead close | 看共享任务板，非 bug |
| 中文乱码 | 没设 `LANG/LC_ALL` 或走了 PowerShell | 重跑，显式 `shell=bash` |
| 临时目录删除失败（Windows） | 句柄未释放（日志被占用） | 让 bridge 写日志到真实 `PROJECT_DIR/temp`，不要 patch 到临时目录 |
| heredoc 内容被截断/吃反斜杠 | 单条太长 / 经过 shell 层 | 分块写；`chr(92)` 构造 |

---

## 9. 一键命令清单（复制即用）

```bash
export LANG=C.UTF-8; export LC_ALL=C.UTF-8

# 列模型
cd /d/git_codes/GenericAgent/frontends/ink-ui && timeout 120 npx tsx scripts/_probe_models.ts

# L2 UI 版面（快）
cd /d/git_codes/GenericAgent/frontends/ink-ui && npx tsx scripts/real_ink_team_panel_e2e.ts

# L1 结构 + 真实回合
cd /d/git_codes/GenericAgent
python tests/real_ink_selftest.py
GA_RUN_REAL_API_E2E=1 python tests/real_ink_selftest.py

# 回归（不调真实模型）
cd frontends/ink-ui && npm test && npm run typecheck
cd ../.. && python -m pytest tests/test_ink_bridge.py -q
```

---

## 10. 维护约定

- 每次给 GA ink 加**新命令/新面板**，都补一个 `scripts/real_ink_<feature>_e2e.ts`（L2）+ 在
  `tests/real_ink_selftest.py` 的 L1 检查里加一条结构断言。
- 每个真实 e2e 的**证据包路径**写进对应的缺陷/进度 md，便于回查。
- 本 SOP 是活文档：踩到新坑就补第 8 节。

---

## 附：本 SOP 的诞生案例（P10 `/team` UI）

用 L2（`real_ink_team_panel_e2e.ts`）首次跑就发现：`/team` 面板被裁成一行（`(no tasks)000…`），
根因是 `App.tsx` 的 `panelRows` 没有 `teamPanel` 分支 → bottom chrome 只给面板 0 行。
加 `teamPanelRows.length` 后帧变为：

```
Team session-session_ · lead team-lead · 1 member(s) · 0 task(s)
Task board
Up/Down select · r refresh · Esc close
```

这正是「真实 UI 版面自测」的价值：**单元测试全绿也挡不住的版面缺陷，L2 一次就抓到。**
