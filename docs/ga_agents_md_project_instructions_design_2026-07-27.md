# GA_AGENTS.md 项目说明层设计（借鉴 Codex AGENTS.md）

日期：2026-07-27

## 1. 背景

GA 当前已经有三类上下文输入：

1. `assets/sys_prompt*.txt`：基础系统提示。
2. `memory/global_mem_insight.txt`：长期记忆/经验摘要，由 `get_global_memory()` 注入。
3. Skill index：由 `skills_runtime.build_skill_prompt()` 注入，模型需要时再调用 `load_skill`。

但 GA 还没有 Codex 那种“项目说明层”：按当前项目目录结构加载说明文件，把仓库级、子目录级约束稳定注入模型上下文。仓库里已有 `AGENTS.md`，但它是给外部 agent / Claude Code / Codex 风格工具看的，不应该被 GA runtime 直接占用为自己的协议文件。

因此新增 GA 专用项目说明层：

- 默认文件名：`GA_AGENTS.md`
- 本地同层覆盖文件名：`GA_AGENTS.override.md`

## 2. Codex 参考实现

Codex 的核心实现位于：

```text
D:\git_codes\codex\codex-rs\core\src\agents_md.rs
```

关键语义来自源码注释：

```rust
//! 2.  Collect every `AGENTS.md` found from the project root down to the
//!     current working directory (inclusive) and concatenate their contents in
//!     that order.
//! 3.  We do **not** walk past the project root.
```

同层文件候选顺序是：

```rust
names.push(LOCAL_AGENTS_MD_FILENAME);    // AGENTS.override.md
names.push(DEFAULT_AGENTS_MD_FILENAME);  // AGENTS.md
```

目录扫描时，每层只取第一个存在的候选文件：

```rust
for d in search_dirs {
    for name in &candidate_filenames {
        let candidate = d.join(name);
        match fs.get_metadata(&candidate, /*sandbox*/ None).await {
            Ok(md) if md.is_file => {
                found.push(candidate);
                break;
            }
            ...
        }
    }
}
```

因此 Codex 的合并规则是：

1. 不同目录层级：从 project root 到 cwd 依次追加。
2. 同一目录内：`AGENTS.override.md` 优先于 `AGENTS.md`。
3. 不越过 project root。
4. 有总字节预算，预算为 0 时禁用。
5. 保留 instruction source paths，供 UI / 诊断 / 引用使用。

## 3. GA 采用的语义

GA v1 采用 Codex 的主体语义，但文件名改为 GA 专用名，避免与外部 agent 的 `AGENTS.md` 混淆：

```text
GA_AGENTS.override.md
GA_AGENTS.md
```

### 3.1 层级合并

假设目录结构为：

```text
repo/
  GA_AGENTS.md
  frontends/
    GA_AGENTS.md
    ink-ui/
      GA_AGENTS.override.md
      GA_AGENTS.md
```

当前 cwd 为：

```text
repo/frontends/ink-ui
```

GA 加载：

```text
repo/GA_AGENTS.md
repo/frontends/GA_AGENTS.md
repo/frontends/ink-ui/GA_AGENTS.override.md
```

不会加载：

```text
repo/frontends/ink-ui/GA_AGENTS.md
```

最终注入顺序仍是 root → cwd：

```text
root instructions

--- ga-project-doc: frontends ---

frontends instructions

--- ga-project-doc: frontends/ink-ui ---

ink-ui local override instructions
```

### 3.2 冲突处理

机制上不做字段级合并，也不自动删除父层规则。所有命中的层级文档按顺序追加。

当父层和子层内容冲突时，模型应遵守更具体、更靠近当前 cwd 的后置说明。为了降低歧义，渲染块会显式提示：

```text
When instructions conflict, follow the later and more specific source.
```

### 3.3 同层 override

同一目录内，候选顺序固定为：

1. `GA_AGENTS.override.md`
2. `GA_AGENTS.md`

只加载第一个存在的文件。`GA_AGENTS.override.md` 是“本层覆盖本层默认说明”，不是“覆盖所有父层说明”。

### 3.4 项目根

GA 主 runtime 已知道仓库根 `script_dir`，因此接入 `agentmain.get_system_prompt()` 时直接以 `script_dir` 作为 workspace root。

独立 runtime 函数仍支持传入任意 `workspace_root` 和 `current_dir`，便于测试和未来子目录工作流扩展。

### 3.5 预算

新增总字节预算，默认值建议为 20000 bytes，可通过环境变量覆盖：

```text
GA_PROJECT_DOC_MAX_BYTES=20000
```

预算为 0 时禁用项目说明层：

```text
GA_PROJECT_DOC_MAX_BYTES=0
```

v1 与 Codex 保持简单一致：按 root → cwd 的加载顺序消耗预算；如果某个文件超过剩余预算，则截断该文件，后续层级不再加载。后续如果发现根文档过大遮蔽子目录说明，再演进为“近 cwd 优先保留”的预算策略。

### 3.6 fallback filenames

v1 只内置两个文件名：

```text
GA_AGENTS.override.md
GA_AGENTS.md
```

为避免复杂化，暂不提供配置化 fallback filenames。未来如需要兼容别名，可在 runtime 函数参数中加 `fallback_filenames`，但默认不暴露给模型。

## 4. 注入位置

GA 当前 `agentmain.get_system_prompt()` 结构为：

```python
base sys_prompt
+ Today
+ get_global_memory()
+ build_skill_prompt()
+ role usage hint
+ permission mode hint
```

新增后调整为：

```python
base sys_prompt
+ Today
+ build_ga_project_instructions(...)
+ get_global_memory()
+ build_skill_prompt()
+ role usage hint
+ permission mode hint
```

理由：

1. 项目说明比 memory 更像“当前项目合同”，应靠前。
2. memory 是长期经验，不应覆盖当前项目局部约束。
3. skill index 是工具/流程导航，应在项目约束之后。

Workflow child agent 的 `_build_system_prompt()` 也应追加同一项目说明层，顺序为：

```python
base child system prompt
+ build_ga_project_instructions(...)
+ build_skill_prompt()
```

## 5. Runtime API 设计

新增模块：

```text
ga_agents_runtime.py
```

核心常量：

```python
DEFAULT_GA_AGENTS_FILENAME = "GA_AGENTS.md"
LOCAL_GA_AGENTS_FILENAME = "GA_AGENTS.override.md"
DEFAULT_PROJECT_DOC_MAX_BYTES = 20000
```

核心数据结构：

```python
@dataclass(frozen=True)
class LoadedGaAgentsDoc:
    path: Path
    rel_path: str
    content: str
    truncated: bool = False

@dataclass(frozen=True)
class GaProjectInstructions:
    docs: tuple[LoadedGaAgentsDoc, ...]
    max_bytes: int
    truncated: bool = False
```

核心函数：

```python
def discover_ga_agents_paths(workspace_root, current_dir=None):
    ...

def load_ga_project_instructions(workspace_root=None, current_dir=None, max_bytes=None):
    ...

def build_ga_project_instructions(workspace_root=None, current_dir=None, max_bytes=None):
    ...
```

错误处理原则：

- 文件不存在：静默跳过。
- 目录同名：跳过。
- 非 UTF-8：`errors="replace"` 读取，不让 prompt 构造失败。
- 任意异常：构造一个短 warning block 或返回空字符串；不能阻断 agent 启动。

## 6. 根 GA_AGENTS.md 内容策略

仓库根可以新增一个简洁的 `GA_AGENTS.md`，作为 GA runtime 自己可读的项目合同。

它不应复制外部 `AGENTS.md` 全文，只保留运行期对模型最重要的稳定约束：

- 代码结构入口。
- 测试入口。
- 真实 API 测试必须显式 opt-in，且当前只使用 terra / `gpt-5.6-terra` / `hhhl`。
- 不提交密钥和本地配置。
- 遇到恶意/可疑载荷只做只读检查和删除。

## 7. TDD 计划

先写失败测试：

1. `tests/test_ga_agents_runtime.py`
   - root → cwd 层级追加。
   - 同层 `GA_AGENTS.override.md` 覆盖同层 `GA_AGENTS.md`。
   - `max_bytes=0` 禁用。
   - 总预算截断。
   - 渲染 block 包含 source path，且保留 root → cwd 顺序。

2. `tests/test_agentmain_role_prompts.py`
   - `get_system_prompt()` 在 memory / skills 前注入 GA project instructions。

3. `tests/test_workflow_child_agent.py`
   - workflow child system prompt 同样注入 GA project instructions，且在 skill index 之前。

然后实现 runtime 和接入。

## 8. 自测计划

逐步验证：

```bash
python -m unittest tests.test_ga_agents_runtime
python -m unittest tests.test_agentmain_role_prompts
python -m unittest tests.test_workflow_child_agent.NativeGPTChildAgentRunnerTest.test_child_agent_system_prompt_includes_optional_skill_listing
python -m unittest discover -s tests
```

最后真实 LLM API 自测只允许使用用户指定配置：

```bash
GA_RUN_REAL_PROMPT_PLANNER_E2E=1 \
GA_WORKFLOW_LLM_PROFILE=terra \
GA_REAL_API_PROFILE=terra \
GA_REAL_API_CONFIG=terra \
GA_REAL_API_EXPECTED_MODEL=gpt-5.6-terra \
GA_REAL_API_EXPECTED_NAME=terra \
python tests/prompt_guided_planner_real_e2e.py
```

不得尝试其他 API / model / provider。
