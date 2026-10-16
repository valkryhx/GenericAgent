import { writeFileSync, mkdirSync, readFileSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { tmpdir } from 'node:os'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { createPasteStore } from '../src/paste.ts'
import { createImageAttachmentStore } from '../src/imageAttachments.ts'
import {
  handleInput,
  isImagePasteShortcut,
  applyClipboardImagePaste,
  tryPasteClipboardImage,
} from '../src/inputController.ts'
import { parseTerminalInput } from '../src/terminalInput.ts'

const dir = join(tmpdir(), 'ga-clip-selftest')
mkdirSync(dir, { recursive: true })
const fixturePath = join(dirname(fileURLToPath(import.meta.url)), 'fixture_clip_64.png')
const pngPath = join(dir, 'clip64.png')
writeFileSync(pngPath, readFileSync(fixturePath))

const setScript = [
  'Add-Type -AssemblyName System.Windows.Forms',
  'Add-Type -AssemblyName System.Drawing',
  '$p = $env:GA_CLIP_TEST_PNG',
  '$img = [System.Drawing.Image]::FromFile($p)',
  '[System.Windows.Forms.Clipboard]::SetImage($img)',
  '$img.Dispose()',
  'if ([System.Windows.Forms.Clipboard]::ContainsImage()) { exit 0 } else { exit 2 }',
].join('; ')

const set = spawnSync(
  'powershell.exe',
  ['-STA', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', setScript],
  { env: { ...process.env, GA_CLIP_TEST_PNG: pngPath }, encoding: 'utf-8', windowsHide: true },
)
console.log('clipboard set', set.status)

const pasteStore = createPasteStore()
const imageStore = createImageAttachmentStore()

const alt = parseTerminalInput('\x1bv')
console.log('parsed alt+v', alt)
console.log('isShortcut', isImagePasteShortcut(alt.key, alt.rawInput))
const pending = handleInput('', alt.rawInput, alt.key, 'idle', pasteStore, new Set(), 0, imageStore)
console.log('pending decision', pending)
if (!pending.pendingImagePaste) {
  console.error('FAIL expected pendingImagePaste')
  process.exit(1)
}

const d = await applyClipboardImagePaste('', 0, imageStore)
console.log('async paste', d)
if (!d.ok || !d.value.includes('[Image #1]')) {
  console.error('FAIL no placeholder from async alt+v path')
  process.exit(1)
}
console.log('OK async alt+v ->', d.value)

// re-set clipboard for second paste
spawnSync(
  'powershell.exe',
  ['-STA', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', setScript],
  { env: { ...process.env, GA_CLIP_TEST_PNG: pngPath }, encoding: 'utf-8', windowsHide: true },
)

const ctrl = parseTerminalInput('\x16')
const pending2 = handleInput(d.value, ctrl.rawInput, ctrl.key, 'idle', pasteStore, new Set(), d.value.length, imageStore)
if (!pending2.pendingImagePaste) {
  console.error('FAIL expected pending for ctrl+v')
  process.exit(1)
}
const d2 = await applyClipboardImagePaste(d.value, d.value.length, imageStore)
console.log('ctrl+v second paste value', JSON.stringify(d2.value))
if (!d2.ok || !d2.value.includes('[Image #2]')) {
  // also allow sync fallback for diagnosis
  const sync = tryPasteClipboardImage(d.value, d.value.length, imageStore)
  console.error('async failed, sync=', sync)
  process.exit(1)
}
console.log('OK ctrl+v ->', d2.value)
