/** 直接测：滚轮序列能否被 parseTerminalInput + parseMouseWheel 正确识别。 */
import { parseTerminalInput } from '../src/terminalInput.ts'
import { parseMouseWheel, parseMouseEvent, resolveMouseCaptureMode } from '../src/mouseWheel.ts'

const samples = [
  '\x1b[<64;10;10M',   // SGR wheel up
  '\x1b[<65;10;10M',   // SGR wheel down
  '\x1b[M`!!',          // X10 wheel up (button 0x60 = '`')
  '\x1b[Ma!!',          // X10 wheel down
]

console.log('默认 mouseMode =', resolveMouseCaptureMode({}))
console.log('')
for (const s of samples) {
  const parsed = parseTerminalInput(s)
  const mouseInput = parsed.key.sequence ?? parsed.rawInput
  const wheel = parseMouseWheel(mouseInput)
  const ev = parseMouseEvent(mouseInput)
  console.log(`输入 ${JSON.stringify(s)}`)
  console.log(`  key.name/sequence: ${JSON.stringify(parsed.key.sequence)}  rawInput=${JSON.stringify(parsed.rawInput)}`)
  console.log(`  parseMouseWheel -> ${wheel}   parseMouseEvent -> ${ev ? ev.kind + '/' + (ev as { direction?: string }).direction : 'null'}`)
  console.log('')
}
