import assert from 'node:assert/strict'
import test from 'node:test'
import {
  asImageFilePath,
  extractImagePathCandidates,
  isImageFilePath,
} from './imagePathDetect.js'
import {
  attachImage,
  attachmentsForSubmit,
  createImageAttachmentStore,
  formatImagePlaceholder,
  pruneImageAttachments,
} from './imageAttachments.js'

test('isImageFilePath accepts windows and quoted paths', () => {
  assert.equal(isImageFilePath(String.raw`D:\shots\a.png`), true)
  assert.equal(isImageFilePath('"D:\\shots\\a.png"'), true)
  assert.equal(isImageFilePath('/tmp/x.JPG'), true)
  assert.equal(isImageFilePath('readme.md'), false)
  assert.equal(isImageFilePath('not an image'), false)
})

test('asImageFilePath strips quotes', () => {
  assert.equal(asImageFilePath('"C:\\\\a\\\\b.webp"'), String.raw`C:\\a\\b.webp`)
})

test('extractImagePathCandidates finds absolute paths', () => {
  const paths = extractImagePathCandidates(String.raw`D:\a.png
D:\b.jpg and text`)
  assert.ok(paths.some(p => p.toLowerCase().endsWith('a.png')))
})

test('image attachment placeholders prune and submit order', () => {
  const store = createImageAttachmentStore()
  const a = attachImage(store, String.raw`D:\a.png`, 'path')
  const b = attachImage(store, String.raw`D:\b.png`, 'clipboard')
  assert.equal(a.placeholder, formatImagePlaceholder(1))
  assert.equal(b.placeholder, formatImagePlaceholder(2))

  let text = `${a.placeholder} then ${b.placeholder}`
  assert.deepEqual(attachmentsForSubmit(store, text), [
    { path: String.raw`D:\a.png`, placeholder: '[Image #1]', source: 'path' },
    { path: String.raw`D:\b.png`, placeholder: '[Image #2]', source: 'clipboard' },
  ])

  text = a.placeholder
  pruneImageAttachments(store, text)
  assert.equal(store.byId.has(2), false)
  assert.equal(store.byId.has(1), true)
})
