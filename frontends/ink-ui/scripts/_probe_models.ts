import { startBridge } from '../src/bridgeClient.js'
const b = startBridge('python', '../../frontends/ink_bridge.py', (ev: any) => {
  if (ev.type === 'ready') b.send({ type: 'model_status' })
  if (ev.type === 'model_status') {
    for (const m of ev.models) console.log(m.index, JSON.stringify(m.name), m.current ? 'CURRENT' : '')
    b.stop(); process.exit(0)
  }
}, () => {})
setTimeout(() => { console.log('timeout'); process.exit(1) }, 60000)
