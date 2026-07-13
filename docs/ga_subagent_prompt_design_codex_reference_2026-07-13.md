# GA subagent 提示词优化：直接借鉴 Codex 的委派提示

## 结论

Codex 调 subagent 也有 prompt，但 Codex 的优势不是 `message` 字段本身，而是它把“什么时候委派、怎么写子任务、委派后父 agent 做什么、怎么等待”写进了工具提示和 usage hint。

GA 之前的 `spawn_agent` 提示过弱，只说“适合独立且边界清晰的子任务”，这不足以稳定驱动模型做正确调度。模型容易出现三类问题：

- 任务一复杂就 spawn，没判断关键路径。
- 子任务 prompt 写得太泛，例如“调研一下 X”，没有结果契约。
- spawn 后父 agent 反复查 `output.txt` 或重复做子任务。

这次先做提示词层面的低风险优化：不改运行时协议，先把 Codex 的委派策略压进 GA 的工具 schema 和 subagent SOP。

## Codex 值得直接抄的部分

Codex 的 `spawn_agent` 工具描述中最有价值的是这些规则：

- 只有用户明确要求 subagent、委派、并行工作时才使用。
- 深度、全面、调研、复杂本身不代表可以 spawn。
- spawn 前先分析整个任务，识别 critical path。
- 紧急阻塞下一步的任务不要派出去，父 agent 本地做。
- 子任务必须 concrete、bounded、self-contained。
- 不要让父 agent 和 subagent 重复做同一件事。
- `wait_agent` 要 sparingly 调用，不要条件反射式等待。
- subagent 运行时，父 agent 应做非重叠工作。
- coding/artifact 任务要拆 disjoint write set。
- 子任务 prompt 要窄，只写下一步真正需要的输出。

这些规则对 GA 同样适用，可以直接拿来用。

## GA 需要做的本地化

Codex 的规则不能 100% 原样照搬，有两个地方需要适配 GA：

1. GA 有自己的 SOP 体系。除了“用户明确要求 subagent”，还要允许“当前 GA SOP 明确要求验证/探索 subagent”的场景。
2. GA 当前还没有完整 root/subagent usage hint 注入机制，所以第一阶段把强约束放到 `assets/tools_schema*.json` 和 `memory/subagent.md`。

因此 GA 的第一版提示词策略是：

```text
工具 schema 负责影响模型每次看到 spawn_agent 时的选择。
memory/subagent.md 负责当模型主动读取 SOP 时提供更完整操作规则。
运行时协议暂不扩大改动面。
```

## 这次实施范围

### 1. `assets/tools_schema.json`

增强 `spawn_agent` 描述，直接加入 Codex 式委派策略：

- use only when user explicitly asks / GA SOP requires；
- critical path；
- concrete, bounded, self-contained；
- do not redo delegated subagent tasks；
- call `wait_agent` sparingly；
- final answer contract；
- coding/artifact write scope。

同时增强：

- `message` 参数：要求自包含任务契约；
- `wait_agent`：强调它只是事件等待，不读最终输出；
- `read_agent_result`：强调读取权威 final result，不把 raw output 当答案。

### 2. `assets/tools_schema_cn.json`

同步中文提示，避免中文模式下仍使用弱提示。

中文 schema 不是英文直译，而是直接面向 GA 使用习惯：

- “关键路径”；
- “具体、有边界、自包含”；
- “不要重复执行已经委派给子智能体的任务”；
- “谨慎调用 wait_agent”；
- “最终结果契约”。

### 3. `memory/subagent.md`

重写为更接近 Codex 的父端调度 SOP：

- 先判断是否真的需要 subagent；
- 子任务 prompt 契约模板；
- spawn → wait event → read final result；
- Map 模式；
- 测试/监察模式；
- 内部 plan_mode；
- 反模式。

保留 GA 原有文件协议，包括：

- `temp/{task_name}/`；
- `input.txt` / `output.txt` / `reply.txt`；
- `state.json` / `events.jsonl`；
- `temp/subagents/inbox.jsonl`；
- `_stop` / `_keyinfo` / `_intervene`。

## 新的 subagent message 契约

以后父 agent 写给 subagent 的 `message` 应该包含这些字段，但不需要机械套模板：

```text
任务目标：你要完成什么。
范围/非目标：只做哪些事，不做哪些事。
上下文/路径：需要读取的文件、目录、日志或用户输入；路径尽量给绝对路径。
约束：语言、格式、工具、联网、不要修改哪些文件、写入范围等。
最终结果契约：最终回答必须包含什么；如果生成文件，必须列出文件路径。
完成/失败判定：什么算完成；无法完成时返回失败原因和已验证事实，不要空 completed。
```

好的例子：

```text
读取 D:\git_codes\GenericAgent\README.md，用 3 条简短中文要点总结项目。只读，不修改文件。最终只返回 3 条要点；如果文件不存在，返回明确错误和你检查过的路径。
```

坏的例子：

```text
总结一下 README。
```

文件生成任务尤其要写清 artifact 契约：

```text
使用 office-mcp skill 生成一个包含两句春天诗句的 docx，保存到 D:\git_codes\GenericAgent\temp\spring_poem.docx。只负责生成该文件，不做其他调研。最终回答必须列出生成文件的绝对路径、文件是否存在、大小；失败时说明在哪一步失败。
```

## 这次不做的事

这次只优化提示词，不扩运行时：

- 不新增 root/subagent usage hint 注入机制；
- 不改 fork history 的过滤逻辑；
- 不改 parent inbox 自动注入上下文；
- 不改 `final_text` / `final_artifacts` 状态协议；
- 不改 interrupt 语义。

这些已经在事件驱动设计文档里单独规划。提示词优化可以先独立落地，风险低、收益直接。

## 后续更完整的方案

下一步可以把 Codex 的提示体系拆成三层：

### root agent usage hint

注入给父 agent，用来约束调度：

```text
只在用户明确要求 subagent/委派/并行，或当前 SOP 明确要求时 spawn。
先判断关键路径；阻塞下一步的任务本地做。
只委派可并行、具体、有边界、自包含的任务。
不要重复做已委派任务。
wait_agent 只在需要事件更新时调用。
```

### subagent usage hint

注入给子 agent，用来约束执行：

```text
你是被委派的子智能体，只负责当前 message 中的任务。
不要扩大范围，不要向父 agent 反问已给定的信息。
最终回答必须满足 final answer contract。
如果生成/修改文件，列出绝对路径。
如果无法完成，返回失败原因、已验证事实和阻塞点。
```

### task envelope

由 GA 在 `spawn_agent` 时自动附加，减少父 agent 每次手写协议：

```json
{
  "role": "subagent_task_contract",
  "agent_path": "/root/readme_summary",
  "parent_path": "/root",
  "expected_final": {
    "kind": "text",
    "required": true
  },
  "artifact_policy": {
    "list_created_files": true,
    "verify_exists": true
  }
}
```

这才是更接近 Codex 的完整形态：工具描述教父 agent 怎么委派，usage hint 区分父/子角色，task envelope 强制结果契约。

## 验证策略

这次加了一个轻量回归测试：检查中英文 schema 的 `spawn_agent` 提示必须包含以下关键语义：

- 英文：`critical path`、`concrete, bounded, and self-contained`、`Do not redo delegated subagent tasks yourself`、`Call wait_agent sparingly`、`final answer contract`。
- 中文：`关键路径`、`具体、有边界、自包含`、`不要重复执行已经委派给子智能体的任务`、`谨慎调用 wait_agent`、`最终结果契约`。

这个测试不是为了锁死完整文案，而是防止后续把提示词又简化回弱提示。

## 判断标准

这次优化成功后，GA 父 agent 在看到 `spawn_agent` 时应该更倾向于：

- 先拆 critical path；
- 只委派真正可并行任务；
- 给 subagent 写清边界和 final answer contract；
- spawn 后不重复干同一件事；
- 少调用 `wait_agent`；
- completed 后通过 `read_agent_result` 收权威结果。

这不会解决所有 subagent 运行时问题，但会显著降低“子任务 prompt 太弱”和“父 agent 调度不稳”的概率。
