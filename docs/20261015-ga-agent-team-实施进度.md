# GA Agent Team 实施进度（对齐 Claude Code 2.1.283）

> 关联方案：`docs/20261014-ga-agent-team-复刻实施方案.md`
> CC 源码证据：`docs/20261014-复刻agent-team指南.md`（§8 四大支柱）
> 记录规则：每完成一个阶段就更新本文件；「已完成」必须是**已跑通测试**的事实。
> 云端不复刻：**【复刻时暂时不需要参考 claudecode 云端会话的实现】**。

## 0. 总览

| 阶段 | 内容 | 状态 | 提交 |
| --- | --- | --- | --- |
| P0 | 共享任务板 `subagent_tasks.py` + 4 工具 | ✅ 完成 | 见本文件首提 |
| P1 | team / lead 身份 | ⬜ 未开始 | |
| P2 | teammate 自主协作（放开 send_message + 任务工具） | ⬜ 未开始 | |
| P3 | 协调者提示词 + teammate 简报 | ⬜ 未开始 | |
| P4 | 通知格式对齐 + 防伪前置 | ⬜ 未开始 | |
| P5 | 协作可靠性（回执 + 防伪 + 已读去重） | ⬜ 未开始 | |
| P6 | 进度自驱 + 发现即通知去重 | ⬜ 未开始 | |

---

## P0：共享任务板 ✅ 完成

**目标**：复刻 CC `TaskCreate/Get/Update/List`，让 lead 与 teammate 共用一份任务清单。

### 交付物

- **`subagent_tasks.py`** —— `SubagentTaskStore`
  - 存储：`<root>/temp/teams/<list_id>/tasks.json` + `tasks.lock`
    （复用 `subagent_state.cross_process_lock` + `atomic_write_json`）。
  - 数据模型（对齐 CC，见 CC 文档 §5）：
    `{id, subject, description, activeForm?, owner?, status, blocks[], blockedBy[], metadata?, createdAt, updatedAt}`
  - API：`create_task / get_task / update_task / list_tasks / claim_task / find_claimable / reclaim_owner`
  - **CC 对齐语义**：
    - `owner` 软锁：已被他人认领 → `already_claimed`（CAS，同 CC `claimTask`）。
    - `blockedBy` 门禁：命中未完成依赖 → `blocked`。
    - `agent_busy`：`claim_task(check_busy=True)`（同 CC `claimTaskWithBusyCheck`）。
    - `already_resolved`：已完成任务拒绝认领。
    - `TaskList` 只列**未完成**的 `blockedBy`；隐藏 `metadata._internal`。
    - id 单调、删除后不复用。
    - `reclaim_owner`（同 CC `N4e`）：agent 终止时回收其未完成任务为 `pending`。
- **工具** `task_create / task_get / task_update / task_list`
  - `ga.py`：新增 `do_task_create / do_task_get / do_task_update / do_task_list`
    + 助手 `_get_task_store` / `_as_list_arg`。
  - `assets/tools_schema.json` / `assets/tools_schema_cn.json`：新增 4 个工具定义（25 → 29）。
  - `subagent_capabilities.py`：4 个任务工具加入 `ORCHESTRATION_TOOLS`
    （P0 仅 lead 使用；P2 再对 teammate 放开）。

### 测试

- `tests/test_subagent_tasks.py`（11 例）：创建 / ID 单调 / CAS / 阻塞 / 忙 / 内部隐藏 /
  双向边 / 删除解边 / `find_claimable` / `reclaim_owner` / **并发认领单赢家**。
- `tests/test_ga_task_tools.py`（2 例）：工具层往返 + 必填校验。
- 回归：`pytest tests/test_subagent_*.py tests/test_ga_subagent_tools.py tests/test_ga_task_tools.py -q`
  → **278 passed, 2 skipped**。

### 复现命令

```bash
python -m pytest tests/test_subagent_tasks.py tests/test_ga_task_tools.py -q
```

---

## 下一步：P1（team / lead 身份）

- 复用 `SubagentRegistry` 给每个会话一个隐式 team（默认 `session-<id[:8]>`）。
- 派生 team 视图 `{teamName, leadAgentId, members[]}`；lead 名字固定 `team-lead`。
- 保留名 `main/team-lead/user/system` 拒绝。
