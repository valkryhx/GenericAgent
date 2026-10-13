# GA workflow 结构化输出对齐 CC：StructuredOutput 工具（取代文本 JSON 解析）

- 日期：2026-10-13
- 状态：**已实施**
- 触发：用户质疑「GA 要求 workflow 产物是 JSON 是否科学？CC 也是这么核验产物的吗？」
- CC 证据仓库（只读）：`D:/git_codes/claudecode283_hack`
- 关联：`docs/20261013-ga-workflow-等待run终态-对齐CC.md`（缺陷 H 的临时修复）、`AGENTS.md`

> **复刻时暂时不需要参考 claudecode 云端会话的实现**。

---

## 0. 一句话结论

**不是。** CC 从不用「解析自由文本 JSON」来核验产物：

1. **schema 约束的是 agent 的「返回值」，不是磁盘上的产物文件**；产物文件（代码/HTML/报告）在 CC 里就是普通文件，没有任何 JSON 校验。
2. 有 schema 时，CC **强制子 agent 调用一个 `StructuredOutput` 工具**（该工具的 input schema 就是用户给的 JSON Schema），
   **校验发生在工具调用层**，形状不对模型**在同一轮内自己重试**；脚本读到的就是**已校验的对象**，无需解析。
3. 没有 schema 时，子 agent 的返回值就是**纯文本字符串**（system prompt 明确「你的最终文本会被原样返回」）。

GA 之前是「提示词让子 agent 把 JSON 写在最终文本里，宿主再解析」——这正是缺陷 H 的根源，也是用户觉得「不科学」的地方。

---

## 1. CC 的确切机制（源码证据）

### 1.1 StructuredOutput 工具（`m0227_0xc482590.js`）

```js
bi="StructuredOutput";
function pe(e={},n=()=>{}){return Ft({...e, isMcp:!1, isEnabled(){return!0}, isConcurrencySafe(){return!0},
  isReadOnly(){return!0}, isOpenWorld(){return!1}, name:bi,
  searchHint:"return the final response as structured JSON", backgrounding:"never",
  async description(){return"Return structured output in the requested format"},
  async prompt(){return"Use this tool to return your final response in the requested structured format. You MUST call this tool exactly once at the end of your response to provide the structured output."},
  get inputSchema(){return Ke()}, ...
  create(){return{async call(r){return n(r),{data:"Structured output provided successfully",structured_output:r,endsTurn:!0}}}}})}
```

要点：

- 工具名 `StructuredOutput`；`endsTurn:true` —— 调用即结束这一轮。
- `inputSchema` = **用户给的 JSON Schema**（`pe({inputJSONSchema:e, strictInputJSONSchema:l}, ...)`）。
- 校验用 ajv：`if(!E(p)){ throw new P("Output does not match required schema: " + errors, "StructuredOutput schema mismatch: " + keywords) }`
  —— **校验在工具调用层**，失败即抛出，模型看到错误后重试。
- schema lint（`ze`/`r3e`）：`root_not_object`（根必须是 object）、`missing_type`、`schema too large`、`$async` 不支持
  → 不可满足的 schema **直接拒绝 spawn**，不会跑到一半才炸。

### 1.2 子 agent 的 system prompt（`m0869_0xd12567a.js`）

有 schema（`Jr` / 追加块 `Vr`）：

> You are a subagent spawned by a workflow orchestration script. ...
> CRITICAL: You MUST call the StructuredOutput tool **exactly once** to return your final answer. The tool's input schema defines the required shape.
> - Do your work (Read files, run commands, etc.), then call StructuredOutput with your answer.
> - Do NOT put your answer in a text response. **The script reads ONLY the StructuredOutput tool call.**
> - If the schema validation fails, read the error and call StructuredOutput again with a corrected shape.
> - After calling StructuredOutput successfully, end your turn. No acknowledgment needed.

无 schema（`jr`）：

> CRITICAL: Your final text response is returned **verbatim** as a string to the calling script — it is your return value, not a message to a human. ... If asked for JSON, return ONLY the raw JSON ...

### 1.3 结果回收与重试上限（`m0869`）

- 成功：`if(le.type==="attachment" && le.attachment.type==="structured_output"){_t=le.attachment.data; continue}` —— 结构化值作为附件回收。
- 计数：`if(kt.name===bi){ Dt++; Bt=kt.input; ... if(_t!==void 0 && Dt>2){Tt.abort("stalled"); break} }` —— 已有值后重复调用 >2 次判 stalled。
- 重试上限：`Ht` 计**失败的** StructuredOutput 结果，`if(Ht>0 && Ht>=Kn && _t===void 0) throw ... "StructuredOutput retry cap (Kn) exceeded"`。
- 结果对象带 `structuredOutputAttempts` / `lastStructuredOutputInput` 供诊断。

### 1.4 官方文档口径

- `workflow-authoring-reference.md:37`：「With schema (a JSON Schema), the subagent is forced to call a StructuredOutput tool and `agent()` returns the validated object — **no parsing needed**.」
- `workflow-authoring-reference.md:46`：「For structured output, use the schema option — **validation happens at the tool-call layer so the model retries on mismatch**.」

---

## 2. GA 现状与差距

| 维度 | CC | GA（改前） |
| --- | --- | --- |
| schema 约束对象 | agent **返回值** | agent 返回值（同样，但实现方式不同） |
| 实现方式 | `StructuredOutput` **工具调用**（input schema = 用户 schema） | 提示词让子 agent **把 JSON 写进最终文本**，宿主 `parse_json_answer` 解析 |
| 校验位置 | 工具调用层（模型当轮重试） | 宿主解析后校验（`_apply_schema_contract`），失败 → 整个 job `failed` → job 级重试 |
| 失败模式 | 形状错 → 工具报错 → 模型当轮改 | 尾逗号 / 夹带数组 / 夹带散文 → **解析失败 = 假失败**（缺陷 H） |
| 产物文件 | 不校验（就是文件） | 不校验（一致） |

即：GA 的 `schema` 语义没错，**错在把「结构化输出」实现成了「文本解析」**。

---

## 3. 实施（GA 对齐 CC）

| 文件 | 改动 |
| --- | --- |
| `workflow_child_agent.py` | 新增常量 `STRUCTURED_OUTPUT_TOOL = "structured_output"`；新增 `_with_structured_output_tool(tools, options)`——当 `options.schema` 是 **object 根**时，向子 agent 的工具表追加 `structured_output`（`parameters` = 该 schema，description 抄 CC「exactly once」）；`_load_tools_schema` 末尾调用它 |
| 同上 | `_build_handler` 设 `handler.workflow_output_schema = schema`；`_run_tool_job` 在子循环结束后把 `handler.workflow_structured_output` 写入 `state["structuredOutput"]`；`_build_success_payload(job, answer, structured=None)` **优先用工具调用值**，无则回退文本解析 |
| 同上 | `_structured_output_contract` 措辞改为 CC 的 `Vr`：**必须调用 `structured_output` 工具恰好一次；脚本只读该工具调用**（并保留 schema JSON 便于弱模型） |
| `ga.py` | 新增 `GenericAgentHandler.do_structured_output`：剥离 `_index/_tool_num` → 用 `validate_agent_payload_against_schema` 校验；**通过 → 存值并 `next_prompt=None` 结束该轮**（`agent_runner_loop` 以 `CURRENT_TASK_DONE` 收尾）；**不通过 → 返回错误 + 重试提示**，模型当轮改 |
| `workflow_permissions.py` | `structured_output` 加入 `ALLOWED_READ_ONLY_STATIC_TOOLS`（纯「交答案」工具，read-only/verify 档也允许，否则 verify job 会被权限拒） |
| `skills/workflow-authoring/SKILL.md` | §4 `schema` 说明改为 CC 口径：object 根、子 agent 调 `structured_output` 工具、工具调用层校验；**不要写「请返回 JSON」这类提示词** |

保留（有意）：文本解析 `parse_json_answer` + `_apply_schema_contract` 作为**回退**——弱模型不调工具时仍能工作，且不破坏既有测试。

**为何不用 `response_format`/json mode**：GA 的 LLM 层没有 json mode；但 GA 有 **function calling**，而 CC 恰恰也是用工具调用实现的，
所以「注入一个 schema 即其参数的函数工具」是与 CC 等价、且 GA 现成可用的路线。

---

## 4. 验证

- 新增 `tests/test_workflow_structured_output.py`（10 例）：object schema 注入工具 / 无 schema 不注入 / 非 object 根不注入 / 不重复注入；
  `do_structured_output` 合法值结束该轮并落值、非法值返回重试提示且不落值、剥离 `_index`；`_build_success_payload` 结构化值优先、文本回退仍可用；read_only/verify 档允许该工具。
- `_load_tools_schema` 集成复验：object schema job 工具数 +1 且含 `structured_output`；无 schema job 不含。
- 回归：`test_workflow_child_agent + test_workflow_structured_output + test_workflow_permissions + test_workflow_scheduler` → **139 OK**；
  `test_workflow_child_agent/permissions/scheduler/runtime/integration/capability_preflight/structured_output/permission_policy` → **277 OK**（改前 1 例旧措辞断言已同步更新）。
- **端到端自测（真实 tool loop）**：`tests/test_workflow_child_agent.py` 新增 `StructuredOutputEndToEndTest`（3 例），用真实 `agent_runner_loop` + `StubToolClient` 驱动：模型调 `structured_output` 时 payload 直接是该对象、工具表含该工具；非法调用当轮被拒（tool_result status=error）→ 第二轮改对即 succeeded；纯文本 JSON 仍可回退。实测 `python -m unittest tests.test_workflow_child_agent.StructuredOutputEndToEndTest` → **3 OK**。

---

## 5. 已知限制 / 下一步

- 只对 **object 根** schema 注入工具（provider 只接受 object 根工具参数；CC 亦如此）。非 object 根 schema 仍走文本契约 + 回退（数组根 payload 目前是 `{"result":[...]}` 形态，宿主校验未解包——如需支持应单独做）。
- 未实现 CC 的 `structuredOutputAttempts` / retry-cap 计数与埋点（GA 的 job 级重试已能兜底）。
- **复刻时暂时不需要参考 claudecode 云端会话的实现**。
