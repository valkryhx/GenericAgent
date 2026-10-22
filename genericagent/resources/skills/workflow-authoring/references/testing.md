# Workflow Test Gates

### `runPythonUnittest(workspaceOrSpec, options?)`

GA-specific host gate. Runs Python `unittest` in a workspace and returns a
structured gate result. The workspace must be inside the workflow workspace.

```js
const gate = await runPythonUnittest(workspacePath, {
  startDir: '.',
  pattern: 'test_*.py',
  timeoutMs: 30000,
})
if (!gate.gatePassed) {
  return { ok: false, gate }
}
```

### `repairAndRetest(spec)`

GA-specific bounded repair loop. Runs the test gate, then asks a repair agent to
read `TEST_FAILURES.txt` and fix the workspace, up to `maxAttempts`.

```js
const repaired = await repairAndRetest({
  workspacePath,
  maxAttempts: 2,
  repairPrompt: 'Fix only the failing behavior and keep the change minimal.',
})
```
