# GA 外部目录 123 子目录深度 E2E 与 file_write 换行修复

日期：2026-10-29
模型：`default/deepseek-v4.1-flash`（`llm.yaml` → `profiles.default`；`defaults.max_tokens: 81920`）
被测目标目录：`D:\test20260416\123`（空目录，用户要求保留全部生成物、不清空）
启动方式：**源码启动**（`pip show genericagent` 为空；显式把仓库根与 `frontends/` 加入 `PYTHONPATH`）
驱动脚本：`work/_e2e_dtest123.py`（真实 LLM，非 mock）
证据 bundle：`temp/dtest123/run_20261029-143115/`（`summary.json` / `events.jsonl` / `driver.log`）

---

## 1. 结论先行

- 在 `D:\test20260416\123` 上，普通对话、代码生成、中文 UTF-8、嵌套子目录读写、workflow、agent team、external workspace 七类任务**全部成功**：`passed=true`，**43/43 检查项 PASS**。
- 所有写入都真实落在目标目录（`tool_cwd` / `workspace_root` 均为 `D:\test20260416\123`），顶层**没有** `assets/skills/memory` 之类 GA 运行时污染。
- 本轮真实运行暴露并修复了一个此前未覆盖的缺陷：`file_write` / `file_patch` 在 Windows 文本模式下把模型给的 `\n` 写成 `\r\n`。修复提交 `b37021f`。
- 修复后复核：**由 `file_write` / `file_patch` 产出的文件全部为纯 LF（CRLF=0）**。仍有 CRLF 的极少数文件来自**模型自己在 `code_run` 里写的 Python 代码**（它用文本模式 `open(...)`），属于被执行的用户代码行为，不是 GA 文件工具的问题（见 §6）。

---

## 2. 环境变量设置（本轮实测）

驱动脚本 `setup_env()` 设置三个变量并清空其余项目/会话级覆盖：

| 变量 | 值 | 实测落点 |
| --- | --- | --- |
| `GA_PROJECT_ROOT` | `D:\test20260416\123` | `agent.project_root`；projectKey 之一段为 `123-4e916e8d1a106296` |
| `GA_DATA_DIR` | `temp/dtest123/data_20261029-143115` | `agent.data_root`；项目桶=`<data>/projects/123-4e916e8d1a106296` |
| `GA_WORKFLOW_WORKSPACE_ROOT` | `D:\test20260416\123` | `bridge.workspace_root`；workflow run 落在 `D:\test20260416\123\workflow-runs\<id>` |

- `agent.tool_cwd` = `D:\test20260416\123`（PASS）
- `bridge.workspace_root` = `D:\test20260416\123`（PASS）
- GA 自身状态（子代理任务、workflow journal 等）留在 `GA_DATA_DIR` 项目桶，**不污染**目标目录。

---

## 3. 测试矩阵与结果（43/43 PASS）

### A. 主 agent 直接读写（file_write / file_read / code_run）

| 编号 | 任务 | 落地物 | 结果 |
| --- | --- | --- | --- |
| A1 | `file_write` 生成 `src/calc.py`（add/sub/mul/div，div 除零抛异常） | `src/calc.py` 237B，UTF-8，`ast.parse` 通过，4 个函数齐全 | PASS |
| A2 | `file_write` 写中文文件（中文 + emoji + 符号） | `docs/说明.md` 93B，UTF-8 无 BOM，三行逐字匹配 | PASS |
| A3 | `code_run`(python) 建嵌套目录 `data/raw/nested/`、写 sample.txt 三行整数、读回求和写 result.txt | `data/raw/sample.txt`(10/20/30)、`data/raw/nested/result.txt`=`60` | PASS |
| A4 | `file_read` 回读自产文件并回答 | 回答含「4 个 def」与中文首行「第一行：中文编码测试」 | PASS |

### B. workflow 与 agent team

| 编号 | 任务 | 结果 |
| --- | --- | --- |
| B1 | 动态 workflow：builder 子任务建 `reports/q1/` 写 summary.md，checker 子任务读回第二行 | `status=succeeded`；`workflow-runs/wf_7d6b7965.../reports/q1/summary.md` 首行 `# Q1 Summary`；journal.jsonl 含 builder+checker（4 处引用） |
| B2 | agent team：子代理建 `team_out/sub/` 写中文 notes.txt，完成后回报任务板 | 子代理 `turn_status=completed`；`team_out/sub/notes.txt` 内容「子代理写入：团队协作测试成功」；任务 `1` 状态 completed；roster 含 `dtest123_child` |

### C. 显式外部 workspacePath

| 编号 | 任务 | 结果 |
| --- | --- | --- |
| C1 | 直接 `WorkflowRuntime`，显式 `args.workspacePath=D:\test20260416\123\ext_ws` | `status=succeeded`；`ext_ws/out/hello.txt`=`EXT123_OK`；workspacePath 精确等于指定子目录 |

### D. 目录纪律

- 目标顶层仅 `_MANIFEST.txt / data / docs / ext_ws / src / team_out / workflow-runs`，**无** `assets/skills/memory`（PASS）。
- 子代理任务状态、workflow journal 均在 `GA_DATA_DIR` 项目桶（PASS）。

---

## 4. 缺陷：file_write / file_patch 在 Windows 注入 CRLF

### 现象

第一轮（`run_20261029-141741`）产物里，由 `file_write` 写出的 `src/calc.py`、`docs/说明.md` 出现 CRLF：
模型给的是 `\n`，落盘却成了 `\r\n`。严格 LF 校验因此失败。

### 根因

`genericagent/core/handler.py` 的 `do_file_write` 与 `file_patch` 使用文本模式
`open(path, "w", encoding="utf-8")`。按 PEP 3116 文本模式写，Windows 会把 `\n` 翻译成 `\r\n`。
`code_run` 写出的文件不受影响（那不带 `newline` 翻译，是模型脚本自己决定的字节）。

### 修复（提交 `b37021f`）

- `file_patch`：`open(path, "w", encoding="utf-8", newline="")` 写出；
- `do_file_write`：
  - `prepend` 分支读旧内容加 `newline=""`，写新内容加 `newline=""`；
  - `append` / `overwrite` 分支统一加 `newline=""`。
- 新增 `tests/test_file_tools_newline.py`（3 条：overwrite / append / patch 均断言 `b"\r" not in raw`）。

回归范围：`test_file_tools_newline.py` + `test_ga_subagent_tools.py` + `test_hooks_tool_runtime.py`
+ `test_agent_loop_parallel_tools.py` + `test_ink_bridge.py` = **228 passed, 9 subtests passed**。

---

## 5. 修复后复核（关键差异）

对比两轮同一任务、同一模型的换行结果：

| 文件 | 产出方式 | 修复前 | 修复后 | 结论 |
| --- | --- | --- | --- | --- |
| `src/calc.py` | `file_write` | CRLF>0 | **CRLF=0（LF）** | 修复生效 |
| `docs/说明.md` | `file_write` | CRLF>0 | **CRLF=0（LF）** | 修复生效 |
| `data/raw/sample.txt` | `code_run` | CRLF | **CRLF=0（LF）** | 本轮模型脚本用了 `newline="\n"`/二进制，未带 CRLF |
| `data/raw/nested/result.txt` | `code_run` | - | **CRLF=0（LF）** | 同上 |
| `team_out/sub/notes.txt` | 子代理 `code_run` | CRLF | **CRLF=0（LF）** | 本轮子代理脚本未带 CRLF |
| `ext_ws/out/hello.txt` | 子代理 `code_run` | - | 无换行（单行） | - |
| `workflow .../summary.md` | 子代理 `code_run` | CRLF | **仍有 CRLF=2** | 见 §6 |

> 说明：`src/__pycache__/calc.cpython-312.pyc` 的 CRLF 是 Python 字节码的二进制内容，**不是文本换行**，不计入。

---

## 6. 关于 code_run 产物仍可能带 CRLF 的归因

`code_run` 执行的是**模型自己生成的 Python 代码**。若模型脚本用文本模式
`open(path, "w", encoding="utf-8")` 写文件，Windows 就会把 `\n` 翻成 `\r\n` —— 这是**被执行的用户代码**的行为，
与 GA 的 `file_write` / `file_patch` 工具无关。本轮 `workflow` 的 `summary.md` 就是这种情况（子任务用 `code_run` 写）。

### 建议（明确操作约定）

要产出**严格 LF** 的文本文件，按下面优先级执行：

1. **首选 GA 的 `file_write`**：它现在硬保证保留模型给的 `\n`（修复 `b37021f`），Windows 上不再注入 CRLF。
2. 若必须在 `code_run` 的 Python 里写，显式关闭换行翻译：`open(path, "w", encoding="utf-8", newline="")`（或直接写二进制 `open(path, "wb")`）。
3. **不要**在 Windows 上用默认文本模式 `open(path, "w", encoding="utf-8")` 写需要严格 LF 的文件——那会把 `\n` 翻成 `\r\n`。

为让模型自然遵守这条约定，`code_run` 的工具描述（`assets/tools_schema.json` 与 `assets/tools_schema_cn.json`，含
`genericagent/resources/assets/` 镜像）已各加一句软提示：需要严格 LF 时优先 `file_write`，或在 Python 里用
`newline=''` / 写二进制。**`code_run` 的执行逻辑本身未改**——它是通用 Python 执行器，不替用户代码强行改写换行，
以免破坏二进制写入或用户预期。

---

## 7. 生成物清单（`D:\test20260416\123`，保留不删）

两轮测试的产物都在，用户要求全部保留：

- `_MANIFEST.txt`：全部生成物的清单（路径 / BOM / 编码 / 字节数）
- `src/calc.py`、`src/__pycache__/calc.cpython-312.pyc`
- `docs/说明.md`（中文 UTF-8）
- `data/raw/sample.txt`、`data/raw/nested/result.txt`
- `team_out/sub/notes.txt`（子代理中文）
- `ext_ws/out/hello.txt`、`ext_ws/workflow-handoffs/agent_1.json`、`ext_ws/workflow-handoffs/result-agent_1.json`
- `workflow-runs/wf_642a09…`（第一轮）、`wf_bd264d…`（第二轮）、`wf_7d6b79…`（本轮）
  每个 run 下含 `reports/q1/summary.md` 与 `workflow-handoffs/{agent_1,agent_2,result-agent_1,result-agent_2}.json`

---

## 8. 复现方法

```bash
cd D:/git_codes/GenericAgent
export PYTHONPATH="D:/git_codes/GenericAgent;D:/git_codes/GenericAgent/frontends"
python work/_e2e_dtest123.py
# 结果写到 temp/dtest123/run_<时间戳>/summary.json 与 events.jsonl
```

> 注意：脚本每次会新建 `data_<时间戳>` / `run_<时间戳>` 目录，**不会清空** `D:\test20260416\123`；
> 再次运行会追加新的 `workflow-runs/<新runId>` 等，不会覆盖旧产物。

---

## 9. 与本轮相关的仓库提交

- `b37021f` fix(file-tools): preserve LF newlines in file_write/file_patch on Windows
- 本文件（docs）：记录本轮 `D:\test20260416\123` 深度 E2E 与换行修复复核结论
- 本文件（docs）：§6 新增“严格 LF 优先用 file_write”的明确操作约定
- `code_run` 工具描述（英/中 × assets + genericagent/resources/assets 共 4 份镜像）：加入一句软提示，不改执行逻辑

---

## 10. 软提示生效复核（提交 c273618 后重跑）

同一个驱动脚本、同一模型（`default/deepseek-v4.1-flash`）在 `D:\test20260416\123` 再跑一轮：

- 证据 bundle：`temp/dtest123/run_20261029-144458`，仍是 **43/43 检查项 PASS**。
- 新 workflow run `wf_51ec91155cc140d1ba96a6a3c2417c07` 的 `reports/q1/summary.md`：**CRLF=0（纯 LF）**。
- 对照上一轮（加提示前）`wf_7d6b79...` 的同名文件：CRLF=2；两轮正文内容逐字节一致（仅行尾不同）。
- 十六进制对照：新文件行尾为 `0x0a`，旧文件为 `0x0d 0x0a`。
- 结论：`code_run` 工具描述里的那句软提示生效，子任务改用了 LF 安全的写法；旧产物按用户要求保留、未删除。
