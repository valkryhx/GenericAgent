import path from 'node:path'
import { fileURLToPath } from 'node:url'
import React from 'react'
import { render } from 'ink'
import { App } from './App.js'

function argValue(name: string, fallback: string): string {
  const idx = process.argv.indexOf(name)
  return idx >= 0 && process.argv[idx + 1] ? process.argv[idx + 1] : fallback
}

const here = path.dirname(fileURLToPath(import.meta.url))
const defaultBridgeScript = path.resolve(here, '..', '..', 'ink_bridge.py')
const python = argValue('--python', process.platform === 'win32' ? 'python' : 'python3')
const bridgeScript = argValue('--bridge', defaultBridgeScript)

render(<App python={python} bridgeScript={bridgeScript} />)
