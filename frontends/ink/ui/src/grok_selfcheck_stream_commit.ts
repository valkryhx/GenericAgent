/**
 * 阶段 2 自测：长流式边流边 commit，done 不二次整段重复。
 * 运行：npx tsx src/grok_selfcheck_stream_commit.ts
 */
import { applyBridgeEvent, initialState } from './state.js'
import { splitStaticAndActiveMessages } from './messagePartition.js'
import { DEFAULT_STREAM_LIVE_TAIL_LINES } from './streamCommit.js'

function countLine(haystack: string, line: string): number {
  return haystack.split('\n').filter(l => l === line).length
}

async function main() {
  let ok = true
  const check = (name: string, cond: boolean, detail = '') => {
    if (cond) console.log(`PASS  ${name}${detail ? ` — ${detail}` : ''}`)
    else {
      ok = false
      console.error(`FAIL  ${name}${detail ? ` — ${detail}` : ''}`)
    }
  }

  console.log('=== stream commit phase-2 selfcheck ===')
  let state = applyBridgeEvent(initialState, { type: 'ready', version: 1 })
  state = applyBridgeEvent(state, { type: 'user', taskId: 1, text: '长流探针' })
  state = applyBridgeEvent(state, { type: 'status', status: 'running', taskId: 1 })

  // Grow stream past tail cap in several deltas
  for (let batch = 0; batch < 4; batch++) {
    const chunk = Array.from({ length: 5 }, (_, i) => `BLK${batch}-L${i}`).join('\n') + '\n'
    state = applyBridgeEvent(state, { type: 'assistant_delta', taskId: 1, text: chunk })
  }

  const mid = splitStaticAndActiveMessages(state.messages, { keepLatestTaskActive: true })
  const commits = mid.staticMessages.filter(m => m.id.startsWith('a-1-c'))
  const live = mid.activeMessages.find(m => m.id === 'a-1')
  console.log('mid commits:', commits.map(m => m.id))
  console.log('mid live lines:', live?.text.split('\n').length)
  check('mid-stream has Static commits', commits.length >= 1)
  check('mid-stream live is short tail', (live?.text.split('\n').length ?? 99) <= DEFAULT_STREAM_LIVE_TAIL_LINES)
  check('user finalized static once stream starts', mid.staticMessages.some(m => m.id === 'u-1'))
  check('user not live mid-stream', !mid.activeMessages.some(m => m.id === 'u-1'))
  const ids = mid.staticMessages.map(m => m.id)
  const uIdx = ids.indexOf('u-1')
  const cIdx = ids.findIndex(id => id.startsWith('a-1-c'))
  check('static chronological user before commits', uIdx >= 0 && (cIdx === -1 || uIdx < cIdx))

  const full = Array.from({ length: 20 }, (_, i) => {
    const batch = Math.floor(i / 5)
    const li = i % 5
    return `BLK${batch}-L${li}`
  }).join('\n') + '\n'

  state = applyBridgeEvent(state, { type: 'assistant_done', taskId: 1, text: full })
  state = applyBridgeEvent(state, { type: 'status', status: 'idle' })

  const end = splitStaticAndActiveMessages(state.messages)
  const allAssistant = end.staticMessages.filter(m => m.role === 'assistant').map(m => m.text).join('\n')
  check('idle active empty', end.activeMessages.length === 0)
  check('user finalized static', end.staticMessages.some(m => m.id === 'u-1' && m.done))
  for (const line of full.trim().split('\n')) {
    check(`line once: ${line}`, countLine(allAssistant, line) === 1)
  }

  if (ok) console.log('\nSELFCHECK PASS: phase-2 incremental commit, no duplicate finalize')
  else {
    process.exitCode = 1
    console.error('\nSELFCHECK FAIL')
  }
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
