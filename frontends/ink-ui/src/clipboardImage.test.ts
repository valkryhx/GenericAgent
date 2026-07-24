import test from 'node:test'
import assert from 'node:assert/strict'
import { writeFileSync, mkdirSync, readFileSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { randomBytes } from 'node:crypto'
import {
  readPngDimensions,
  validateCapturedImageFile,
  MIN_CLIPBOARD_IMAGE_DIMENSION,
} from './clipboardImage.js'

const dir = join(tmpdir(), 'ga-clip-unit')
mkdirSync(dir, { recursive: true })

test('readPngDimensions reads IHDR', () => {
  const png = Buffer.from(
    'iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAFElEQVR4nGM8YWTEgA0wYRUdtBIA76YBPGvKGPUAAAAASUVORK5CYII=',
    'base64',
  )
  const p = join(dir, `d-${randomBytes(3).toString('hex')}.png`)
  writeFileSync(p, png)
  assert.deepEqual(readPngDimensions(p), { width: 8, height: 8 })
})

test('validateCapturedImageFile rejects 1x1', () => {
  const png = Buffer.from(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
    'base64',
  )
  const p = join(dir, `tiny-${randomBytes(3).toString('hex')}.png`)
  writeFileSync(p, png)
  const r = validateCapturedImageFile(p)
  assert.equal(r.ok, false)
  if (!r.ok) assert.match(r.error, /too small|too tiny/i)
})

test('validateCapturedImageFile accepts 64x64 fixture', () => {
  const candidates = [
    join(process.cwd(), 'scripts', 'fixture_clip_64.png'),
    join(process.cwd(), 'frontends', 'ink-ui', 'scripts', 'fixture_clip_64.png'),
  ]
  const src = candidates.find((c) => existsSync(c))
  if (!src) {
    assert.fail('missing scripts/fixture_clip_64.png')
  }
  const p = join(dir, `ok-${randomBytes(3).toString('hex')}.png`)
  writeFileSync(p, readFileSync(src))
  const r = validateCapturedImageFile(p)
  assert.equal(r.ok, true)
  if (r.ok) {
    assert.ok((r.width ?? 0) >= MIN_CLIPBOARD_IMAGE_DIMENSION)
    assert.ok((r.height ?? 0) >= MIN_CLIPBOARD_IMAGE_DIMENSION)
  }
})
