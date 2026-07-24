import { mkdirSync, writeFileSync, existsSync, readFileSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { spawnSync } from 'node:child_process'
import { tmpdir } from 'node:os'
import { fileURLToPath } from 'node:url'
import {
  captureClipboardImage,
  validateCapturedImageFile,
  MIN_CLIPBOARD_IMAGE_DIMENSION,
} from '../src/clipboardImage.ts'

const dir = join(tmpdir(), 'ga-clip-selftest')
mkdirSync(dir, { recursive: true })

// 64x64 真实体积夹具（不是 1x1；Grok 拒 <8px）
const fixturePath = join(dirname(fileURLToPath(import.meta.url)), 'fixture_clip_64.png')
const pngPath = join(dir, 'clip64.png')
writeFileSync(pngPath, readFileSync(fixturePath))

// 1x1 坏图：捕获后应被 validate 拒绝
const tinyPath = join(dir, 'tiny1.png')
writeFileSync(
  tinyPath,
  Buffer.from(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
    'base64',
  ),
)

const rejectTiny = validateCapturedImageFile(tinyPath)
if (rejectTiny.ok) {
  console.error('FAIL expected 1x1 reject, got ok', rejectTiny)
  process.exit(1)
}
console.log('OK reject 1x1:', rejectTiny.error)

const setScript = [
  'Add-Type -AssemblyName System.Windows.Forms',
  'Add-Type -AssemblyName System.Drawing',
  `$p = $env:GA_CLIP_TEST_PNG`,
  `$img = [System.Drawing.Image]::FromFile($p)`,
  `[System.Windows.Forms.Clipboard]::SetImage($img)`,
  `$img.Dispose()`,
  `if ([System.Windows.Forms.Clipboard]::ContainsImage()) { 'HAS'; exit 0 } else { 'NO'; exit 2 }`,
].join('; ')

const set = spawnSync(
  'powershell.exe',
  ['-STA', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', setScript],
  {
    encoding: 'utf-8',
    windowsHide: true,
    env: { ...process.env, GA_CLIP_TEST_PNG: pngPath },
  },
)
console.log('set_clipboard', set.status, (set.stdout || '').trim(), (set.stderr || '').slice(0, 300))

const cap = captureClipboardImage()
console.log('capture', cap)
if (!cap.ok || !existsSync(cap.path)) {
  console.error('FAIL capture')
  process.exitCode = 1
} else {
  const dimsOk =
    (cap.width ?? 0) >= MIN_CLIPBOARD_IMAGE_DIMENSION &&
    (cap.height ?? 0) >= MIN_CLIPBOARD_IMAGE_DIMENSION
  if (!dimsOk && cap.width !== undefined) {
    console.error('FAIL dims', cap)
    process.exitCode = 1
  } else {
    console.log('OK', cap.path, cap.width, cap.height)
  }
}
