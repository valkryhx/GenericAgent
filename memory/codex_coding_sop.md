# Codex Coding SOP

来源：由 `codex_session_distill.py` 从本机 Codex JSONL 会话离线蒸馏。本文只保留跨项目编码工作法，不保存原始对话、密钥、私有路径或一次性业务事实。

## 使用原则
- 先按当前仓库规范执行；本 SOP 只提供编码协作习惯和避坑策略。
- 经验有证据计数，证据越多优先级越高；单次会话经验只作为弱提示。
- 若本 SOP 与项目 AGENTS/CONTRIBUTING/用户指令冲突，以上游明确指令为准。

## debugging
### Recover from failures by adding information
- 做法：On failure, read the error, gather new state, then change strategy; avoid repeating the same command without new evidence.
- 证据: 8 | signals: failure, recovery

## git
### 保护用户未提交改动
- 做法：改代码前检查 git status，遇到无关改动只绕开，不重置或覆盖。
- 证据: 1 | signals: git_status, patch

## testing
### Verify changed behavior before claiming completion
- 做法：After edits, run the focused test or project verification command and report failures instead of assuming success.
- 证据: 7 | signals: patch, verification, verification_success

## workflow
### Use fast text search to orient in code
- 做法：Reach for precise repository search before broad directory traversal when locating files, symbols, or tests.
- 证据: 8 | signals: fast_search

### Probe repository facts before editing
- 做法：For coding work, inspect project rules, status, and nearby code before making the smallest viable patch.
- 证据: 7 | signals: fast_search, git_status, patch, repo_probe

### Ground documentation in project commit history
- 做法：When generating contributor documentation (AGENTS.md/CONTRIBUTING.md), first review recent commit history to understand actual team conventions, workflow patterns, and practices, grounding documentation in real project activity rather than assumptions.
- 证据: 1 | signals: fast_search, patch, repo_probe, session requested AGENTS.md after commit history review
