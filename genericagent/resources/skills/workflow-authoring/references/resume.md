# Workflow Resume

## 6. Resume

The tool result includes `runId`. To resume after a pause, kill, or script edit,
call `workflow` again with `resumeFromRunId` and, when iterating on a saved
script, `scriptPath`. The longest unchanged prefix of `agent()` calls returns
cached results; the first changed or new call and everything after it runs live.

Before diagnosing an empty or unexpected result, read the run's journal and
agent result files under the run transcript directory. Do not assume a cached
result is non-empty.
