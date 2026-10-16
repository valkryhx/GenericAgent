import test from 'node:test'
import assert from 'node:assert/strict'
import {
  movePermissionSelection,
  panelFromPermissionStatus,
  permissionPanelOnEnter,
  permissionPanelOnEscape,
  permissionPanelRows,
  requiresConfirmation,
  shouldApplyPermissionStatus,
  type PermissionPanelState,
} from './permissionPanel.js'

function statusEvent(mode: string) {
  return {
    type: 'permission_status' as const,
    mode,
    default: 'full_access',
    modes: ['read_only', 'ask', 'full_access'],
  }
}

test('panelFromPermissionStatus selects the current mode and fills three options', () => {
  const panel = panelFromPermissionStatus(statusEvent('ask'))
  assert.equal(panel.options.length, 3)
  assert.deepEqual(panel.options.map(o => o.mode), ['read_only', 'ask', 'full_access'])
  assert.equal(panel.current, 'ask')
  assert.equal(panel.selected, 1)
  assert.equal(panel.confirming, null)
})

test('panelFromPermissionStatus falls back to default modes when server sends none', () => {
  const panel = panelFromPermissionStatus({
    type: 'permission_status',
    mode: 'full_access',
    default: 'full_access',
    modes: [],
  })
  assert.deepEqual(panel.options.map(o => o.mode), ['read_only', 'ask', 'full_access'])
  assert.equal(panel.selected, 2)
})

test('movePermissionSelection clamps within bounds', () => {
  assert.equal(movePermissionSelection(0, -1, 3), 0)
  assert.equal(movePermissionSelection(2, 1, 3), 2)
  assert.equal(movePermissionSelection(1, 1, 3), 2)
})

test('requiresConfirmation only for entering full_access from another mode', () => {
  assert.equal(requiresConfirmation('full_access', 'ask'), true)
  assert.equal(requiresConfirmation('full_access', 'read_only'), true)
  assert.equal(requiresConfirmation('full_access', 'full_access'), false)
  assert.equal(requiresConfirmation('read_only', 'full_access'), false)
  assert.equal(requiresConfirmation('ask', 'full_access'), false)
})

test('Enter on a non-full-access target applies immediately', () => {
  const panel = panelFromPermissionStatus(statusEvent('full_access'))
  const readOnly: PermissionPanelState = { ...panel, selected: 0 }
  assert.deepEqual(permissionPanelOnEnter(readOnly), { type: 'apply', mode: 'read_only' })
})

test('Enter on full_access from another mode requests confirmation first', () => {
  const panel = panelFromPermissionStatus(statusEvent('read_only'))
  const full: PermissionPanelState = { ...panel, selected: 2 }
  assert.deepEqual(permissionPanelOnEnter(full), { type: 'confirm', mode: 'full_access' })
})

test('Enter while confirming applies the confirmed mode', () => {
  const panel = panelFromPermissionStatus(statusEvent('read_only'))
  const confirming: PermissionPanelState = { ...panel, selected: 2, confirming: 'full_access' }
  assert.deepEqual(permissionPanelOnEnter(confirming), { type: 'apply', mode: 'full_access' })
})

test('Enter on the already-current mode is a noop', () => {
  const panel = panelFromPermissionStatus(statusEvent('ask'))
  assert.deepEqual(permissionPanelOnEnter(panel), { type: 'noop' })
})

test('Escape from confirm returns to list, escape from list closes', () => {
  const panel = panelFromPermissionStatus(statusEvent('read_only'))
  const confirming: PermissionPanelState = { ...panel, confirming: 'full_access' }
  const back = permissionPanelOnEscape(confirming)
  assert.ok(back)
  assert.equal(back?.confirming, null)
  assert.equal(permissionPanelOnEscape(panel), null)
})

test('permissionPanelRows grows by one row while confirming', () => {
  const panel = panelFromPermissionStatus(statusEvent('read_only'))
  const listRows = permissionPanelRows(panel)
  const confirmRows = permissionPanelRows({ ...panel, confirming: 'full_access' })
  assert.equal(confirmRows, listRows + 1)
})

test('shouldApplyPermissionStatus mirrors requested-or-open gate', () => {
  assert.equal(shouldApplyPermissionStatus(true, false), true)
  assert.equal(shouldApplyPermissionStatus(false, true), true)
  assert.equal(shouldApplyPermissionStatus(false, false), false)
})
