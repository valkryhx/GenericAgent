/** 验证默认配置：全屏布局开 + 鼠标捕获关（不写任何 1000 系列）。 */
import { enterMainScreenTerminalSequenceForMode, exitTerminalCleanupSequenceForMode } from '../src/terminalCleanup.ts'
import { resolveFullscreenMode, resolveMouseCaptureMode, mouseTrackingOn } from '../src/mouseWheel.ts'

const env = {}
const fullscreen = resolveFullscreenMode(env)
const mouse = resolveMouseCaptureMode(env)

console.log('=== 默认配置（无环境变量）===')
console.log(`  fullscreen        = ${fullscreen}`)
console.log(`  mouseCaptureMode  = ${mouse}`)
console.log(`  tracking sequence = ${JSON.stringify(mouseTrackingOn(mouse))}`)

const enter = enterMainScreenTerminalSequenceForMode(mouse, fullscreen)
const exit = exitTerminalCleanupSequenceForMode(mouse, fullscreen)

console.log('\n=== 启动序列 ===')
console.log(`  ${JSON.stringify(enter)}`)
console.log(`  含 1049h（备用屏）: ${enter.includes('\u001B[?1049h')}`)
console.log(`  含 1000h（鼠标）:   ${enter.includes('\u001B[?1000h')}`)
console.log(`  → 左键选中应可用:   ${!enter.includes('\u001B[?100')}`)

console.log('\n=== 退出序列 ===')
console.log(`  ${JSON.stringify(exit)}`)
console.log(`  含 1049l（退备用屏）: ${exit.includes('\u001B[?1049l')}`)
