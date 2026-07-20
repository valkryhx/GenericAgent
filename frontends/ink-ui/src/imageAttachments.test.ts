import assert from 'node:assert/strict'
import test from 'node:test'
import {
  asImageFilePath,
  completeAbsoluteImagePathAtCursor,
  extractImagePathCandidates,
  isImageFilePath,
  replaceImagePathsInText,
} from './imagePathDetect.js'
import {
  attachImage,
  attachmentsForSubmit,
  createImageAttachmentStore,
  formatImagePlaceholder,
  pruneImageAttachments,
} from './imageAttachments.js'

const REPO = String.raw`d:\git_codes\GenericAgent`
const SPACED = String.raw`${REPO}\截图\屏幕截图 2026-07-15 124017.png`
const WECHAT = String.raw`${REPO}\截图\微信图片_20260720144105_2048_89.jpg`

test('isImageFilePath accepts windows and quoted absolute paths only', () => {
  assert.equal(isImageFilePath(String.raw`D:\shots\a.png`), true)
  assert.equal(isImageFilePath('"D:\\shots\\a.png"'), true)
  assert.equal(isImageFilePath('/tmp/x.JPG'), true)
  assert.equal(isImageFilePath(SPACED), true)
  assert.equal(isImageFilePath(`"${SPACED}"`), true)
  assert.equal(isImageFilePath('readme.md'), false)
  assert.equal(isImageFilePath('not an image'), false)
  // 相对裸名：终端分片粘贴时绝不能当图（会误伤完整路径子串）
  assert.equal(isImageFilePath('048_89.jpg'), false)
  assert.equal(isImageFilePath('a.png'), false)
  assert.equal(isImageFilePath('屏幕截图 2026-07-15 124017.png'), false)
})

test('asImageFilePath strips quotes on absolute paths including spaces', () => {
  assert.equal(asImageFilePath('"C:\\\\a\\\\b.webp"'), String.raw`C:\\a\\b.webp`)
  assert.equal(asImageFilePath(`"${SPACED}"`), SPACED)
  assert.equal(asImageFilePath(SPACED), SPACED)
  assert.equal(asImageFilePath('048_89.jpg'), null)
})

test('extractImagePathCandidates: wechat path + trailing Chinese', () => {
  assert.deepEqual(extractImagePathCandidates(`${WECHAT} 这图是什么`), [WECHAT])
  assert.deepEqual(extractImagePathCandidates('048_89.jpg'), [])
})

test('extractImagePathCandidates: spaced Windows screenshot path', () => {
  assert.deepEqual(extractImagePathCandidates(SPACED), [SPACED])
  assert.deepEqual(extractImagePathCandidates(`${SPACED} 描述一下`), [SPACED])
  assert.deepEqual(extractImagePathCandidates(`"${SPACED}" 描述一下`), [SPACED])
  // 路径内双空格
  const dbl = String.raw`${REPO}\截图\屏幕截图  2026-07-15  124017.png`
  assert.deepEqual(extractImagePathCandidates(`${dbl} ok`), [dbl])
})

test('extractImagePathCandidates: weird symbols and unicode', () => {
  const weird = String.raw`D:\pics\foo(1)[bar]#baz+qux~1.png`
  assert.deepEqual(extractImagePathCandidates(`${weird} x`), [weird])
  const cjk = String.raw`D:\图 库\测试-图_片.JPEG`
  assert.deepEqual(extractImagePathCandidates(cjk), [cjk])
  // 两个绝对路径
  const a = String.raw`D:\a.png`
  const b = String.raw`D:\b 2.jpg`
  const multi = extractImagePathCandidates(`${a} ${b}`)
  assert.ok(multi.includes(a))
  assert.ok(multi.includes(b))
})

test('replaceImagePathsInText does not replace path substring mid-filename', () => {
  const text = `${WECHAT} 这图是什么`
  const out = replaceImagePathsInText(text, [
    { path: '048_89.jpg', placeholder: '[Image #2]' },
  ])
  assert.equal(out, text)
  assert.ok(!out.includes('[Image #2]'))

  const ok = replaceImagePathsInText(text, [
    { path: WECHAT, placeholder: '[Image #1]' },
  ])
  assert.equal(ok, '[Image #1] 这图是什么')

  const spacedOk = replaceImagePathsInText(`${SPACED} 这图是什么`, [
    { path: SPACED, placeholder: '[Image #3]' },
  ])
  assert.equal(spacedOk, '[Image #3] 这图是什么')
})

test('replaceImagePathsInText strips PowerShell call-operator wrapper', () => {
  // 资源管理器 / PS 粘贴带空格路径时常是：& 'D:\...\屏幕截图 ....png'
  const wrapped = `& '${SPACED}'`
  const out = replaceImagePathsInText(wrapped, [
    { path: SPACED, placeholder: '[Image #4]' },
  ])
  assert.equal(out, '[Image #4]')
  assert.ok(!out.includes('&'))
  assert.ok(!out.includes("'"))

  const dbl = `. "${SPACED}" 看看`
  assert.equal(
    replaceImagePathsInText(dbl, [{ path: SPACED, placeholder: '[Image #1]' }]),
    '[Image #1] 看看',
  )
})

test('asImageFilePath unwraps PowerShell & quoted path', () => {
  assert.equal(asImageFilePath(`& '${SPACED}'`), SPACED)
  assert.equal(asImageFilePath(`& "${SPACED}"`), SPACED)
})

test('completeAbsoluteImagePathAtCursor joins split paste chunks', () => {
  const head = String.raw`${REPO}\截图\微信图片_20260720144105_2`
  const tail = String.raw`048_89.jpg 这图是什么`
  const hit = completeAbsoluteImagePathAtCursor(head, head.length, tail)
  assert.ok(hit)
  assert.equal(hit!.path, WECHAT)
  assert.equal(hit!.start, 0)
  assert.equal(hit!.end, WECHAT.length)
})

test('completeAbsoluteImagePathAtCursor joins spaced path split after space', () => {
  const head = String.raw`${REPO}\截图\屏幕截图 `
  const tail = String.raw`2026-07-15 124017.png 看看`
  const hit = completeAbsoluteImagePathAtCursor(head, head.length, tail)
  assert.ok(hit)
  assert.equal(hit!.path, SPACED)
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
