import test from 'node:test'
import assert from 'node:assert/strict'
import { readClipboardText } from './clipboardText.js'

test('readClipboardText honors GA_CLIPBOARD_TEXT_FALLBACK=0', () => {
  const old = process.env.GA_CLIPBOARD_TEXT_FALLBACK
  process.env.GA_CLIPBOARD_TEXT_FALLBACK = '0'
  try {
    assert.equal(readClipboardText(), '')
  } finally {
    if (old === undefined) delete process.env.GA_CLIPBOARD_TEXT_FALLBACK
    else process.env.GA_CLIPBOARD_TEXT_FALLBACK = old
  }
})

test('readClipboardText returns string (never throws) when clipboard empty', () => {
  // 只断言类型与不抛错；具体内容依赖真实剪贴板，不在单测里断言。
  const text = readClipboardText(200)
  assert.equal(typeof text, 'string')
})
