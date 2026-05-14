# Codex Coding SOP

来源：由 `codex_session_distill.py` 从本机 Codex JSONL 会话生成脱敏证据包，再由 LLM 读取 packet 并通过 `codex_lesson_update` 提议、校验、晋升。本文只保留跨项目编码工作法，不保存原始对话、密钥、私有路径或一次性业务事实。

## 使用原则
- 先按当前仓库规范执行；本 SOP 只提供编码协作习惯和避坑策略。
- 经验应来自 LLM 对脱敏 packet 的分析；规则命中的 seed observation 只作为证据提示，不能单独成为正式经验。
- 经验有独立证据计数，证据越多优先级越高；单次会话经验只作为弱提示。
- 若本 SOP 与项目 AGENTS/CONTRIBUTING/用户指令冲突，以上游明确指令为准。

## debugging
### Recover from failures by adding information
- 做法：On failure, read the error, gather new state, then change strategy; avoid repeating the same command without new evidence.
- 证据: 12 | signals: failure, recovery

## testing
### Verify changed behavior before claiming completion
- 做法：After edits, run the focused test or project verification command and report failures instead of assuming success.
- 证据: 9 | signals: patch, verification, verification_success

## workflow
### Probe repository facts before editing
- 做法：For coding work, inspect project rules, status, and nearby code before making the smallest viable patch.
- 证据: 15 | signals: fast_search, git_status, patch, repo_probe

### Use fast text search to orient in code
- 做法：Reach for precise repository search before broad directory traversal when locating files, symbols, or tests.
- 证据: 12 | signals: fast_search

### Distill pipeline has dual-channel lesson extraction
- 做法：codex distill has two independent pipelines: (1) rule-based extract_session_packet() scans for 4 hardcoded behavior signals, (2) LLM-proposed codex_lesson_update tool records any pattern the agent deems valuable during live execution. Candidates need evidence_count>=2 or confidence>=0.85 to promote to forma ...[cut]
- 证据: 1 | signals: codex_lesson_update_found_in_ga.py, extract_session_packet_4_patterns_confirmed, promote_threshold_verified

### Separate extraction, validation, and publication
- 做法：When a workflow is underperforming, split responsibilities into separate stages: one stage gathers/desensitizes evidence, one stage abstracts reusable lessons, and one stage validates/merges/renders the result. Avoid mixing generation and verification in the same step.
- 证据: 1 | signals: packet shows historical distillation became richer after separating脱 ...[cut], packet summary identifies职责放错是根因 rather than tuning thresholds, packet timeline includes brainstorming, TDD, systematic-debugging, v ...[cut]
