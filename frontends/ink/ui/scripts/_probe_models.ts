import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { startBridge } from '../src/bridgeClient.js'

const BRIDGE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..', 'bridge.py')
const b = startBridge('python', BRIDGE, (ev: any) => {
  if (ev.type === 'ready') b.send({ type: 'model_status' })
  if (ev.type === 'model_status') {
    for (const m of ev.models) console.log(m.index, JSON.stringify(m.name), m.current ? 'CURRENT' : '')
    b.stop(); process.exit(0)
  }
}, () => {})
setTimeout(() => { console.log('timeout'); process.exit(1) }, 60000)
