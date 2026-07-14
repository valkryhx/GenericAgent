# GA Ink UI selection/copy bug implementation progress

**Target:** Goal 2, Codex-style inline scrollback by default.

## Progress

- [x] Rechecked the fix plan document.
- [x] Confirmed the plan does not need a rewrite before implementation.
- [x] Selected implementation strategy: use Ink `Static` for finalized transcript scrollback, keep active tail/composer/panels in the live viewport.
- [x] RED tests for inline startup and static scrollback.
- [x] Terminal mode implementation.
- [x] App inline scrollback implementation.
- [x] Update affected tests.
- [x] Fix stale live input block left in terminal scrollback on Ctrl+C / restart.
- [x] Fix duplicate-looking input box caused by native cursor parking before Ink redraw.
- [x] Typecheck and test verification.

## Notes

- Default mode must not enter alternate screen or enable DEC mouse capture.
- `GA_INK_MOUSE=full` can remain as a legacy path for old app-handled mouse behavior.
- History replacement and clear actions need scrollback reset/replay handling because static terminal output cannot be removed by ordinary React reconciliation.
- Inline mode must clear the whole live viewport on exit. Clearing only the current line leaves the previous input frame in terminal scrollback after Ctrl+C.
- Native cursor parking for IME support must not leave the terminal cursor inside the input frame when Ink/log-update starts the next redraw. The cursor is now saved before parking and restored before redraw/cleanup, otherwise `eraseLines()` clears from the wrong row and leaves a stale input frame above the current one.
- RED command: `npx tsx --test src/terminalCleanup.test.ts src/App.test.ts`.
- RED result: 4 expected failures. The current code still enters `1049h`, still enables `1000/1002/1003/1006`, reasserts mouse tracking after stdin resume, and renders completed transcript inside the live Ink viewport.
- GREEN focused command: `npx tsx --test src/App.test.ts src/messagePartition.test.ts src/messageViewportPlan.test.ts`.
- GREEN focused result: 18/18 pass after default inline scrollback implementation and destructive history replacement replay fix.
- Second RED command: `npx tsx --test src/terminalCleanup.test.ts src/App.test.ts`.
- Second RED result: expected failures for missing multiline live viewport cleanup and missing cursor restore before Ink redraw.
- Second GREEN focused result: `terminalCleanup.test.ts` 6/6 pass; `App.test.ts` 12/12 pass.
- Verification command: `npm test` in `frontends/ink-ui`.
- Verification result: 239/239 pass.
- Typecheck command: `npm run typecheck` in `frontends/ink-ui`.
- Typecheck result: pass.
- Repository test command: `python -m unittest discover -s tests`.
- Latest repository test result: 466 tests ran, 1 skipped, 1 failure in `test_workflow_runtime.WorkflowRuntimeTest.test_runtime_observes_external_kill_state`: expected text containing `killed`, got `workflow runtime deadline exceeded`. This is outside `frontends/ink-ui` and did not reproduce through the TS UI verification path.
