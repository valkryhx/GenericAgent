import test from 'node:test'
import assert from 'node:assert/strict'
import {
  BSU,
  ESU,
  isSynchronizedOutputSupported,
  wrapSynchronizedUpdate,
} from './syncOutput.js'

const baseEnv = (extra: NodeJS.ProcessEnv = {}): NodeJS.ProcessEnv => ({ ...extra })

test('explicit override beats every heuristic', () => {
  // 强制关闭：即使终端本身支持，也必须返回 false（真机排查用的逃生口）。
  assert.equal(
    isSynchronizedOutputSupported({ env: baseEnv({ WT_SESSION: 'x', GA_UI_SYNC_OUTPUT: '0' }) }),
    false,
  )
  // 强制开启：即使终端不在白名单里，也必须返回 true。
  assert.equal(
    isSynchronizedOutputSupported({ env: baseEnv({ TERM: 'dumb', GA_UI_SYNC_OUTPUT: '1' }) }),
    true,
  )
  // 大小写与空白都要容忍，否则用户写 `Off ` 就静默失效。
  assert.equal(isSynchronizedOutputSupported({ env: baseEnv({ GA_UI_SYNC_OUTPUT: ' Off ' }) }), false)
  assert.equal(isSynchronizedOutputSupported({ env: baseEnv({ GA_UI_SYNC_OUTPUT: 'TRUE' }) }), true)
})

test('tmux is always skipped, even from a supporting outer terminal', () => {
  // tmux 转发字节但不实现 DEC 2026，包装只会打碎原子性。
  // 这条断言防止将来有人「顺手」把 TMUX 从排除表里去掉。
  assert.equal(
    isSynchronizedOutputSupported({
      env: baseEnv({ TMUX: '/tmp/tmux-1000/default,123,0', TERM_PROGRAM: 'iTerm.app' }),
    }),
    false,
  )
})

test('known supporting terminals are detected', () => {
  const cases: NodeJS.ProcessEnv[] = [
    { TERM_PROGRAM: 'iTerm.app' },
    { TERM_PROGRAM: 'WezTerm' },
    { TERM_PROGRAM: 'vscode' },
    { TERM_PROGRAM: 'ghostty' },
    { TERM: 'xterm-kitty' },
    { KITTY_WINDOW_ID: '1' },
    { TERM: 'xterm-ghostty' },
    { TERM: 'foot' },
    { TERM: 'alacritty' },
    { ZED_TERM: 'true' },
    { WT_SESSION: 'guid' },
    { VTE_VERSION: '6800' },
  ]
  for (const env of cases) {
    assert.equal(
      isSynchronizedOutputSupported({ env: baseEnv(env) }),
      true,
      `expected support for ${JSON.stringify(env)}`,
    )
  }
})

test('VTE below 0.68 is not claimed', () => {
  assert.equal(isSynchronizedOutputSupported({ env: baseEnv({ VTE_VERSION: '6003' }) }), false)
})

test('unknown terminals are not claimed', () => {
  assert.equal(isSynchronizedOutputSupported({ env: baseEnv({ TERM: 'xterm-256color' }) }), false)
  assert.equal(isSynchronizedOutputSupported({ env: baseEnv({}) }), false)
})

test('legacy Windows console is excluded even when TERM is set', () => {
  // conhost 不支持 DEC 2026；写出无人消费的序列只会增加每帧字节。
  assert.equal(
    isSynchronizedOutputSupported({
      platform: 'win32',
      env: baseEnv({ TERM: 'xterm-256color' }),
    }),
    false,
  )
  // 但 Windows Terminal 里（WT_SESSION 存在）必须判为支持。
  assert.equal(
    isSynchronizedOutputSupported({
      platform: 'win32',
      env: baseEnv({ TERM: 'xterm-256color', WT_SESSION: 'guid' }),
    }),
    true,
  )
})

test('wrapSynchronizedUpdate brackets the chunk exactly once', () => {
  const wrapped = wrapSynchronizedUpdate('FRAME', true)
  assert.equal(wrapped, `${BSU}FRAME${ESU}`)
  // 配对完整：一次写入里 BSU/ESU 各一个。
  assert.equal(wrapped.split(BSU).length - 1, 1)
  assert.equal(wrapped.split(ESU).length - 1, 1)
})

test('wrapSynchronizedUpdate is identity when disabled or empty', () => {
  assert.equal(wrapSynchronizedUpdate('FRAME', false), 'FRAME')
  // 空事务（BSU 紧跟 ESU）没有意义，不该写出去。
  assert.equal(wrapSynchronizedUpdate('', true), '')
})
