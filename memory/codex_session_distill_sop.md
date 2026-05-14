# Codex Session Distill SOP

**触发**：用户要求“学习 Codex 历史 / 蒸馏 Codex 会话 / 提升编码经验”，或自主行动需要低风险学习任务。  
**目标**：从本机 Codex JSONL 会话中提炼跨项目编码工作法，更新 `codex_coding_sop.md`。不保存原始对话、密钥、私有路径或一次性业务事实。

## 快速流程

1. 查看状态：
   ```powershell
   python ../memory/codex_session_distill.py status
   ```

2. 小批量准备 LLM 深度蒸馏包：
   ```powershell
   python ../memory/codex_session_distill.py deep --limit 3
   ```
   未指定目录时，工具会自动发现本机 Codex sessions 目录，优先检查 `$CODEX_SESSIONS_DIR`、`$CODEX_HOME\sessions`、用户目录下的 `.codex\sessions`、AppData 常见位置，以及当前工作区附近的 `.codex\sessions`。这个命令只生成脱敏证据包和 LLM 审阅说明，不会把规则命中的 seed observation 直接当成正式经验。

3. 如需指定目录：
   ```powershell
   python ../memory/codex_session_distill.py deep "$env:USERPROFILE\.codex\sessions\2026\05" --limit 3
   ```

4. 读取 `../memory/codex_distill/queue/*.md`。基于 packet 中的目标、timeline、验证命令、失败恢复和 seed observations 判断是否有可复用经验；有则调用 `codex_lesson_update` 写入候选。

5. 晋升候选并渲染 SOP：
   ```powershell
   python ../memory/codex_session_distill.py promote
   ```

6. 蒸馏完成后读取：
   ```text
   ../memory/codex_coding_sop.md
   ```

## LLM 参与的深度蒸馏

当用户要求“深入学习 Codex 经验”或你发现规则蒸馏过于保守时，走工具闭环，不要直接 patch 正式 SOP：

1. 生成脱敏 packet：
   ```powershell
   python ../memory/codex_session_distill.py deep --limit 1
   ```

2. 读取 `../memory/codex_distill/queue/*.md` 中最新 packet。只基于 packet 内容判断，禁止读取原始 JSONL。packet 中的 `Seed Observations` 是确定性证据提示，不是正式 lesson；必须由 LLM 结合完整轨迹判断是否存在更丰富、可迁移的经验。

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

这个流程中，LLM 是蒸馏核心，负责发现和表达经验；程序只负责脱敏、证据包生成、安全校验、去重、晋升和写文件。不要用简单规则匹配替代 LLM 判断。

## 写入边界

- 允许写入：`../memory/codex_distill/`、`../memory/codex_coding_sop.md`
- 不要读取或复制密钥文件。
- 不要把 JSONL 原文写入长期记忆。
- 不要把项目私有事实、绝对用户路径、token、cookie、临时错误细节写入 SOP。

## 进度规则

- `codex_distill/progress.json` 用 session hash 记录处理状态。
- 已 `prepared` / `learned` / `skipped` 且 size 未变的 JSONL 默认跳过。
- `codex_lesson_update` 的 `evidence_count` 按独立 `source_hash` 计数；同一 packet 反复调用不会刷高晋升证据。
- 重复出现的好经验只增加 `lessons.jsonl` 中的独立证据计数，不重复扩写 SOP。
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
