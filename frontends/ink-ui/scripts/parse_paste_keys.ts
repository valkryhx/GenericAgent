import { parseTerminalInput } from './src/terminalInput.ts'

const cases = [
  ['ctrl-v \\x16', '\x16'],
  ['alt-v esc+v', '\x1bv'],
  ['plain v', 'v'],
  ['esc+ctrl-v', '\x1b\x16'],
  ['ctrl-V uppercase via shift not typical', '\x16'],
]
for (const [name, seq] of cases) {
  console.log(name, JSON.stringify(parseTerminalInput(seq)))
}
