import path from 'node:path'
import { fileURLToPath } from 'node:url'
import React from 'react'
import { render } from 'ink'
import { App } from './App.js'
import { createCursorParkStdout } from './stdoutCursorPark.js'
import { inkRenderOptions } from './inkRenderOptions.js'

function argValue(name: string, fallback: string): string {
  const idx = process.argv.indexOf(name)
  return idx >= 0 && process.argv[idx + 1] ? process.argv[idx + 1] : fallback
}

const here = path.dirname(fileURLToPath(import.meta.url))
const defaultBridgeScript = path.resolve(here, '..', '..', 'bridge.py')
const python = argValue('--python', process.platform === 'win32' ? 'python' : 'python3')
const bridgeScript = argValue('--bridge', defaultBridgeScript)

// 路径 A 光标协调默认开启。若某终端上相对光标移动表现异常，可设
// GA_UI_CURSOR_PARK=off 关闭：退回「不 park，光标留在帧底（隐藏）」——无 ghost/漂移，
// 仅 IME 不贴 caret。无需重新构建即可回退。
const cursorParkEnabled = (process.env.GA_UI_CURSOR_PARK ?? '').toLowerCase() !== 'off'

// Ink 默认 `exitOnCtrlC: true`（ink/build/render.js:14）：它在 stdin 上自己挂一个
// Ctrl+C 监听并直接 unmount，事件根本到不了 App 的 handleTerminalInput。那样有
// 两个后果：二次确认永远没机会执行；有选区时也无法「先复制、不退出」。
// Claude Code 319 两种语义都有（m2124:8688 复制并吞掉事件；m1072 无选区时要求
// 二次确认），所以这里关掉 Ink 的默认行为，全部交给 App 判断。
// 选项与测试共用 `inkRenderOptions`，避免两边行为不一致。
const inkOptions = inkRenderOptions

if (cursorParkEnabled) {
  const cursorPark = createCursorParkStdout(process.stdout)
  render(<App python={python} bridgeScript={bridgeScript} cursorPark={cursorPark} />, {
    stdout: cursorPark.stdout,
    ...inkOptions,
  })
} else {
  render(<App python={python} bridgeScript={bridgeScript} />, inkOptions)
}
