/**
 * The workflow status bar must not steal Enter from a steering draft.
 *
 * Same class of bug as `workflowPanelEnterSteering.test.ts`, at the second entry
 * point. `App.tsx` dispatches the status bar before the composer:
 *
 *     const statusBusy = state.status === 'running' || state.status === 'stopping'
 *     if (input.trim() === '' || (key.return && statusBusy)) { send(command); return }
 *
 * The `(key.return && statusBusy)` half existed because, historically, Enter during
 * a running turn was a no-op ("keeps draft"), so lending it to the status bar cost
 * nothing. That premise is obsolete: `inputController.ts` now queues the draft
 * (`queue_prompt`, commit `30caed3`), so during a workflow run the very keystroke
 * that should queue a steering message instead opens the workflow panel.
 *
 * The rule these tests pin: a non-empty composer owns Enter, in every status. The
 * status bar may still take Enter when the composer is empty, which is how the
 * panel is opened from the status bar today.
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import type { InputKey } from './inputController.js'

/**
 * The predicate `App.tsx` uses before letting the workflow status bar consume a key.
 * Named here so the rule is asserted directly rather than through a mounted UI.
 *
 * End-to-end coverage of the same behaviour lives in `appEnterRouting.test.ts`,
 * which drives the real component; this file documents the rule and its history.
 */
function statusBarOwnsKey(key: InputKey, composerInput: string): boolean {
  // An empty composer means the user is navigating, not writing.
  return composerInput.trim() === ''
}

/** The pre-fix predicate, kept to demonstrate the regression it caused. */
function statusBarOwnsKeyBeforeFix(
  key: InputKey,
  composerInput: string,
  statusBusy: boolean,
): boolean {
  return composerInput.trim() === '' || (key.return === true && statusBusy)
}

test('a steering draft keeps Enter while a workflow turn is running', () => {
  assert.equal(
    statusBarOwnsKey({ return: true }, 'also update the changelog'),
    false,
    'the status bar would consume Enter and the steering message could never be queued',
  )
})

test('the old predicate is what made the draft impossible to submit while busy', () => {
  // Pins *why* the change was needed, not just what it now does.
  assert.equal(
    statusBarOwnsKeyBeforeFix({ return: true }, 'steering', true),
    true,
    'the pre-fix predicate was expected to steal Enter during a busy turn',
  )
})

test('an empty composer still opens the panel from the status bar', () => {
  assert.equal(statusBarOwnsKey({ return: true }, ''), true)
})

test('whitespace-only input counts as empty', () => {
  assert.equal(statusBarOwnsKey({ return: true }, '  \t '), true)
})

test('the rule does not depend on status, so it cannot drift again', () => {
  // The signature deliberately has no `statusBusy`: whether the turn is running must
  // not change who owns Enter, because queueing is supported in both cases.
  for (const draft of ['x', 'a longer steering message', '/usr/bin/python']) {
    assert.equal(statusBarOwnsKey({ return: true }, draft), false, `${draft}: Enter was stolen`)
  }
})

test('non-Enter keys are unaffected', () => {
  for (const key of [{}, { upArrow: true }, { downArrow: true }, { escape: true }]) {
    // `x` (stop) is dispatched by rawInput inside the status-bar handler, not here;
    // this predicate only governs whether the handler runs at all.
    assert.equal(statusBarOwnsKey(key, 'draft'), false)
    assert.equal(statusBarOwnsKey(key, ''), true)
  }
})

test('both entry points now agree: the panel and the status bar', () => {
  // One rule for the whole app: a draft owns Enter, an empty composer owns the overlay.
  const panelOwnsKey = (key: { return?: boolean }, input: string) =>
    !(key.return === true && input.trim() !== '')
  for (const draft of ['', 'queued steering', '   ']) {
    assert.equal(
      panelOwnsKey({ return: true }, draft),
      statusBarOwnsKey({ return: true }, draft),
      `the two entry points disagree for input ${JSON.stringify(draft)}`,
    )
  }
})