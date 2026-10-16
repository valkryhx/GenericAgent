import test from 'node:test'
import assert from 'node:assert/strict'
import {
  moveTeamSelection,
  teamMemberGlyph,
  teamPanelCapturesKey,
  teamPanelCommandForKey,
  teamPanelFromRoster,
  teamPanelLines,
  teamTaskGlyph,
} from './teamPanel.js'

const rosterEvent = {
  team: { teamName: 'session-abc12345', leadAgentId: '/root', leadName: 'team-lead', memberCount: 2 },
  members: [
    { agentId: '/root/agent_1', name: 'agent_1', status: 'running', taskCount: 2, currentTask: 'Add parser' },
    { agentId: '/root/agent_2', name: 'agent_2', status: 'idle', taskCount: 0, currentTask: null },
  ],
  tasks: [
    { id: '1', subject: 'Design', status: 'completed', owner: '/root/agent_1' },
    { id: '2', subject: 'Add parser', status: 'in_progress', owner: '/root/agent_1' },
    { id: '3', subject: 'Write tests', status: 'pending', owner: null, blockedBy: ['2'] },
  ],
}

test('teamPanelFromRoster normalizes a payload with missing arrays', () => {
  const state = teamPanelFromRoster({ team: rosterEvent.team })
  assert.deepEqual(state.members, [])
  assert.deepEqual(state.tasks, [])
  assert.equal(state.selected, 0)
})

test('teamPanelLines renders the header, members and task board', () => {
  const lines = teamPanelLines(teamPanelFromRoster(rosterEvent))
  assert.match(lines[0]!, /Team session-abc12345/)
  assert.match(lines[0]!, /2 member\(s\)/)
  assert.match(lines[0]!, /3 task\(s\)/)
  const body = lines.join('\n')
  assert.match(body, /agent_1/)
  assert.match(body, /running/)
  assert.match(body, /Add parser/)
  assert.match(body, /Task board/)
  assert.match(body, /\[x\] #1 Design\s+owner=agent_1/)
  assert.match(body, /\[~\] #2 Add parser\s+owner=agent_1/)
  assert.match(body, /\[ \] #3 Write tests\s+owner=- \(blocked by 2\)/)
  assert.match(body, /Esc close/)
})

test('the selected member row carries the selection marker', () => {
  const state = { ...teamPanelFromRoster(rosterEvent), selected: 1 }
  const lines = teamPanelLines(state)
  const agent2Row = lines.find(line => line.includes('agent_2'))!
  assert.ok(agent2Row.startsWith('\u25b8'))
})

test('empty roster renders placeholders', () => {
  const lines = teamPanelLines(teamPanelFromRoster({ team: rosterEvent.team, members: [], tasks: [] }))
  assert.ok(lines.includes('  (no teammates yet)'))
  assert.ok(lines.includes('  (no tasks)'))
})

test('glyphs fall back to the neutral marker', () => {
  assert.equal(teamMemberGlyph('running'), '\u25cf')
  assert.equal(teamMemberGlyph('nonsense'), '\u25cb')
  assert.equal(teamMemberGlyph(undefined), '\u25cb')
  assert.equal(teamTaskGlyph('in_progress'), '[~]')
  assert.equal(teamTaskGlyph('nonsense'), '[ ]')
})

test('moveTeamSelection wraps in both directions', () => {
  const state = teamPanelFromRoster(rosterEvent)
  assert.equal(moveTeamSelection(state, 1).selected, 1)
  assert.equal(moveTeamSelection({ ...state, selected: 1 }, 1).selected, 0)
  assert.equal(moveTeamSelection(state, -1).selected, 1)
})

test('moveTeamSelection is a no-op with no members', () => {
  const state = teamPanelFromRoster({ team: rosterEvent.team, members: [], tasks: [] })
  assert.equal(moveTeamSelection(state, 1), state)
})

test('teamPanelCommandForKey maps keys to selection or refresh', () => {
  const state = teamPanelFromRoster(rosterEvent)
  assert.deepEqual(teamPanelCommandForKey(state, { downArrow: true }, ''), {
    panel: { ...state, selected: 1 },
  })
  assert.deepEqual(teamPanelCommandForKey(state, { upArrow: true }, ''), {
    panel: { ...state, selected: 1 },
  })
  assert.deepEqual(teamPanelCommandForKey(state, {}, 'r'), { command: { type: 'team_list' } })
  assert.equal(teamPanelCommandForKey(state, {}, 'x'), null)
})

test('teamPanelCapturesKey owns navigation and refresh keys only', () => {
  const state = teamPanelFromRoster(rosterEvent)
  assert.equal(teamPanelCapturesKey(state, { upArrow: true }, ''), true)
  assert.equal(teamPanelCapturesKey(state, {}, 'r'), true)
  assert.equal(teamPanelCapturesKey(state, {}, 'x'), false)
})
