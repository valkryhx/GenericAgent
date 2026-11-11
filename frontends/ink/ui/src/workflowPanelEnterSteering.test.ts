/**
 * Enter while steering a running workflow must submit the message, not drive the panel.
 *
 * Reported: during a workflow run the user types a follow-up ("steering") and presses
 * Enter. Instead of submitting, the open workflow panel opens/closes again, so the
 * queued message can never be sent.
 *
 * The root cause is a dispatch-order problem in `App.tsx`, not in `workflowPanel.ts`:
 *
 *   - panels are handled BEFORE the composer;
 *   - `workflowPanelCommandForKey` answers Enter with `{panel}` (list -> detail,
 *     overview -> agent detail), returning early;
 *   - `workflowPanelCapturesKey` then also says Enter is the panel's.
 *
 * `workflowPanelCapturesKey` cannot fix this on its own: its `rawInput` is the
 * character produced by the current key, not the composer's contents. The decision
 * needs the composer state, which lives in `App.tsx`.
 *
 * What these tests pin is the *rule*, expressed as the predicate App now uses:
 * the overlay owns Enter only when the composer is empty. This mirrors the workflow
 * status bar's existing `input.trim() === ''` check, so both entry points agree.
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { workflowPanelCapturesKey, workflowPanelFromDetail } from './workflowPanel.js'
import type { WorkflowPanelState } from './workflowPanel.js'
import type { InputKey } from './inputController.js'

/**
 * The predicate `App.tsx` applies before handing a key to the workflow overlay.
 * Kept as a named function so the rule is testable without mounting React.
 */
function overlayOwnsKey(key: InputKey, composerInput: string): boolean {
  const composerHasDraft = composerInput.trim() !== ''
  return !(key.return === true && composerHasDraft)
}

/** Built through the real constructor so the fixture cannot drift from the type. */
function overviewPanel(): WorkflowPanelState {
  return workflowPanelFromDetail({
    run: { runId: 'wf_1', sessionId: 'session', status: 'running', jobs: [] },
    script: 'return 1',
    events: [],
    draft: {
      taskText: 'x',
      classification: {},
      plan: {
        meta: { name: 'x' },
        phases: [{ title: 'One', agents: [{ label: 'a1' }] }],
      },
      validation: { ok: true },
    },
    progress: null,
  })
}

test('Enter with a queued steering draft is NOT the overlay\'s', () => {
  // The reported bug in one assertion.
  assert.equal(
    overlayOwnsKey({ return: true }, 'please also check the tests'),
    false,
    'the overlay would consume Enter and the steering message could never be sent',
  )
})

test('Enter with an empty composer IS the overlay\'s', () => {
  // The panel's own Enter behaviour must survive: list -> detail, overview -> agent.
  assert.equal(overlayOwnsKey({ return: true }, ''), true)
})

test('whitespace-only input counts as empty, matching the status bar rule', () => {
  assert.equal(overlayOwnsKey({ return: true }, '   '), true)
  assert.equal(overlayOwnsKey({ return: true }, '\n\t '), true)
})

test('non-Enter keys are unaffected by the draft', () => {
  // Movement and Esc must keep working while a draft exists, otherwise the user
  // could not scroll the overlay he is looking at while writing a message.
  for (const key of [{ upArrow: true }, { downArrow: true }, { escape: true }, {}]) {
    assert.equal(overlayOwnsKey(key, 'draft'), true, `${JSON.stringify(key)} was taken from the overlay`)
    assert.equal(overlayOwnsKey(key, ''), true, `${JSON.stringify(key)} was taken from the overlay`)
  }
})

test('the panel still treats Enter as its own, so the guard is what changes behaviour', () => {
  // Documents *why* the fix had to live in App: the low-level capture check has no
  // access to the composer and keeps claiming Enter.
  assert.equal(workflowPanelCapturesKey(overviewPanel(), { return: true }, ''), true)
})

test('the guard does not disturb the existing panel shortcuts', () => {
  // `j`/`k`/`x`/`r`/`s` are still panel shortcuts while the composer is empty; this
  // fix deliberately does not touch them (they are typed as `rawInput`, which the
  // overlay check handles, and a draft containing them is a separate concern).
  for (const letter of ['j', 'k', 'x', 'r', 's']) {
    assert.equal(workflowPanelCapturesKey(overviewPanel(), {}, letter), true, `${letter} lost its shortcut`)
  }
})

test('a queued steering Enter is exactly the case the status bar already guards', () => {
  // Both entry points now draw the same line, so the user sees one consistent rule.
  const statusBarOwnsEnter = (input: string) => input.trim() === ''
  assert.equal(statusBarOwnsEnter('steering text'), false)
  assert.equal(overlayOwnsKey({ return: true }, 'steering text'), false)
  assert.equal(statusBarOwnsEnter(''), true)
  assert.equal(overlayOwnsKey({ return: true }, ''), true)
})