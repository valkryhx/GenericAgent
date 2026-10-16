import test from 'node:test'
import assert from 'node:assert/strict'
import { createPasteStore, expandPastedTextRefs, foldPastedText } from './paste.js'

test('foldPastedText keeps single-line text literal', () => {
  const store = createPasteStore()

  assert.equal(foldPastedText('hello', store), 'hello')
  assert.equal(store.size, 0)
})

test('foldPastedText folds multiline text into copied placeholder', () => {
  const store = createPasteStore()
  const text = 'a\nb\nc'

  const folded = foldPastedText(text, store)

  assert.equal(folded, '[Copied text #1 +2 lines]')
  assert.equal(expandPastedTextRefs(`${folded} 请检查`, store), 'a\nb\nc 请检查')
})

test('foldPastedText normalizes bracketed paste control sequences', () => {
  const store = createPasteStore()
  const text = '\u001b[200~a\r\nb\tc\u001b[201~'

  const folded = foldPastedText(text, store)

  assert.equal(folded, '[Copied text #1 +1 lines]')
  assert.equal(expandPastedTextRefs(folded, store), 'a\nb    c')
})
