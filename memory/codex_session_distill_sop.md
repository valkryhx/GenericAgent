# Codex Session Distill SOP

**触发**：用户要求“学习 Codex 历史 / 蒸馏 Codex 会话 / 提升编码经验”，或自主行动需要低风险学习任务。  
**目标**：从本机 Codex JSONL 会话中提炼跨项目编码工作法，更新 `codex_coding_sop.md`。不保存原始对话、密钥、私有路径或一次性业务事实。

## 快速流程

1. 查看状态：
   ```powershell
   python ../memory/codex_session_distill.py status
   ```

2. 小批量蒸馏：
   ```powershell
   python ../memory/codex_session_distill.py run --limit 3
   ```
   未指定目录时，工具会自动发现本机 Codex sessions 目录，优先检查 `$CODEX_SESSIONS_DIR`、`$CODEX_HOME\sessions`、用户目录下的 `.codex\sessions`、AppData 常见位置，以及当前工作区附近的 `.codex\sessions`。

3. 如需指定目录：
   ```powershell
   python ../memory/codex_session_distill.py run "$env:USERPROFILE\.codex\sessions\2026\05" --limit 3
   ```

4. 蒸馏完成后读取：
   ```text
   ../memory/codex_coding_sop.md
   ```

## LLM 参与的深度蒸馏

当用户要求“深入学习 Codex 经验”或你发现规则蒸馏过于保守时，走工具闭环，不要直接 patch 正式 SOP：

1. 生成脱敏 packet：
   ```powershell
   python ../memory/codex_session_distill.py prepare --limit 1
   ```

2. 读取 `../memory/codex_distill/queue/*.md` 中最新 packet。只基于 packet 内容判断，禁止读取原始 JSONL。

3. 若发现可复用经验，调用 `codex_lesson_update` 工具写入候选。每次只写一条经验，字段要短：
   - `title`: 简短标题
   - `guidance`: 可复用做法，不写项目私有事实
   - `category`: workflow/debugging/testing/git/security/frontend/planning/communication/quality
   - `evidence`: packet 中的短证据信号
   - `source_hash`: packet 标题或内容里的 hash 前缀
   - `confidence`: 0.0-1.0

4. 晋升候选并渲染 SOP：
   ```powershell
   python ../memory/codex_session_distill.py promote
   ```

这个流程中，LLM 负责发现和表达经验；`codex_lesson_update` / `promote` 负责校验、去重、晋升和写文件。

## 写入边界

- 允许写入：`../memory/codex_distill/`、`../memory/codex_coding_sop.md`
- 不要读取或复制密钥文件。
- 不要把 JSONL 原文写入长期记忆。
- 不要把项目私有事实、绝对用户路径、token、cookie、临时错误细节写入 SOP。

## 进度规则

- `codex_distill/progress.json` 用 session hash 记录处理状态。
- 已 `prepared` / `learned` / `skipped` 且 size 未变的 JSONL 默认跳过。
- 重复出现的好经验只增加 `lessons.jsonl` 中的 `evidence_count`，不重复扩写 SOP。
- 扫描不是随机采样：按文件路径稳定排序，取前 N 个未处理且达到质量门槛的 JSONL。传入月份目录时只处理该月份目录；不传目录时处理自动发现到的 sessions 根目录。

## 质量门槛

优先学习有这些信号的会话：

- 先搜索/读取仓库，再改文件
- 有 `apply_patch` 或文件修改
- 修改后运行测试、lint、类型检查或其他验证
- 失败后读取错误并切换策略，最终恢复成功

跳过：

- 纯闲聊或短问答
- 无验证的猜测
- 只包含一次性路径、业务状态、日志粘贴的会话
- 脱敏风险高的会话
