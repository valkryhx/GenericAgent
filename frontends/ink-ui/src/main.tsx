import path from 'node:path'
import { fileURLToPath } from 'node:url'
import React from 'react'
import { render } from 'ink'
import { App } from './App.js'
import { createCursorParkStdout } from './stdoutCursorPark.js'

function argValue(name: string, fallback: string): string {
  const idx = process.argv.indexOf(name)
  return idx >= 0 && process.argv[idx + 1] ? process.argv[idx + 1] : fallback
}

const here = path.dirname(fileURLToPath(import.meta.url))
const defaultBridgeScript = path.resolve(here, '..', '..', 'ink_bridge.py')
const python = argValue('--python', process.platform === 'win32' ? 'python' : 'python3')
const bridgeScript = argValue('--bridge', defaultBridgeScript)

// 路径 A 光标协调默认开启。若某终端上相对光标移动表现异常，可设
// GA_UI_CURSOR_PARK=off 关闭：退回「不 park，光标留在帧底（隐藏）」——无 ghost/漂移，
// 仅 IME 不贴 caret。无需重新构建即可回退。
const cursorParkEnabled = (process.env.GA_UI_CURSOR_PARK ?? '').toLowerCase() !== 'off'
if (cursorParkEnabled) {
  const cursorPark = createCursorParkStdout(process.stdout)
  render(<App python={python} bridgeScript={bridgeScript} cursorPark={cursorPark} />, { stdout: cursorPark.stdout })
} else {
  render(<App python={python} bridgeScript={bridgeScript} />)
}
