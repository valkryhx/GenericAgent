/**
 * Time fixed offline fixture lines at the Node stdout boundary, before ConPTY.
 * App.tsx:1572 is the first JS scheduling boundary. Screen reads alone cannot
 * identify whether the app wrote late or the pseudo-terminal merged writes.
 * The Python probe sends six numbered lines and checks their order against
 * ConPTY. Keep this preloader to preserve that comparison without changing GA.
 */
const { writeFileSync, renameSync } = require('node:fs')
const { stripVTControlCharacters } = require('node:util')

const trace = process.env.GA_CONPTY_WRITE_TRACE
if (trace && process.env.GA_CONPTY_LATENCY_REAL === '0') {
  const write = process.stdout.write
  const seen = new Set()
  const rows = []
  process.stdout.write = function (chunk, ...args) {
    const at = process.hrtime.bigint()
    const result = write.call(this, chunk, ...args)
    if (seen.size === 6 || typeof chunk !== 'string' || !chunk.includes('R0')) return result
    const labels = []
    for (const [, label] of stripVTControlCharacters(chunk).matchAll(/\bR0([1-6]) +[A-Za-z]/g)) {
      if (seen.has(label)) continue
      seen.add(label)
      labels.push(label)
    }
    if (labels.length > 0) rows.push(JSON.stringify({ ns: String(at), labels }))
    if (seen.size === 6) {
      // Persist only once, after the last measured stdout call.
      writeFileSync(`${trace}.tmp`, rows.join('\n') + '\n')
      renameSync(`${trace}.tmp`, trace)
    }
    return result
  }
}
