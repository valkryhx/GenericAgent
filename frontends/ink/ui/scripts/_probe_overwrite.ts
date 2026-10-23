
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from '../src/App.js'
import { createCursorParkStdout } from '../src/stdoutCursorPark.js'

class W extends EventEmitter { isTTY=true; chunks=[]; constructor(c,r){super();this.columns=c;this.rows=r}
  write(s){this.chunks.push(String(s));return true} }
class R extends EventEmitter { isTTY=true; setRawMode(){return this} setEncoding(){return this} ref(){return this} unref(){return this} read(){return null} resume(){return this} pause(){return this} }
const delay=(ms)=>new Promise(r=>setTimeout(r,ms))

// ---- virtual terminal: growable buffer, absolute cursor ----
class VT {
  rows: string[][] = [[]]
  r = 0; c = 0
  write(s: string) {
    for (let i = 0; i < s.length; i++) {
      const ch = s[i]
      if (ch === '\x1b') {
        const m = /^\x1b\[([0-9;?]*)([A-Za-z])/.exec(s.slice(i))
        if (m) {
          const n = m[1] === '' ? 1 : parseInt(m[1].replace(/[?]/g, ''), 10) || 1
          const cmd = m[2]
          if (cmd === 'A') this.r = Math.max(0, this.r - n)
          else if (cmd === 'B') { this.r += n; this.ensure() }
          else if (cmd === 'C') this.c += n
          else if (cmd === 'D') this.c = Math.max(0, this.c - n)
          else if (cmd === 'G') this.c = Math.max(0, n - 1)
          else if (cmd === 'H') { this.r = 0; this.c = 0 }
          else if (cmd === 'K') { this.rows[this.r] = [] }
          else if (cmd === 'J') { const k = parseInt(m[1] || '0', 10); if (k === 2 || k === 3) { this.rows = [[]]; this.r = 0; this.c = 0 } }
          i += m[0].length - 1
          continue
        }
        const m2 = /^\x1b\][^\x07]*\x07/.exec(s.slice(i))
        if (m2) { i += m2[0].length - 1; continue }
        continue
      }
      if (ch === '\n') { this.r += 1; this.c = 0; this.ensure(); continue }
      if (ch === '\r') { this.c = 0; continue }
      this.ensure()
      const line = this.rows[this.r]
      while (line.length < this.c) line.push(' ')
      line[this.c] = ch
      this.c += 1
    }
  }
  ensure() { while (this.rows.length <= this.r) this.rows.push([]) }
  text() { return this.rows.map(l => l.join('').trimEnd()) }
}

const queue: any[] = []
const sbc=(_p:any,_b:any,onEvent:any)=>{ setTimeout(()=>onEvent({type:'ready',version:1}),0); const t=setInterval(()=>{const n=queue.shift(); if(n) onEvent(n)},15); return {send(){},stop(){clearInterval(t)}} }
const w=new W(100,24); const park=createCursorParkStdout(w as any)
const vt=new VT()
const origWrite = w.write.bind(w)
;(w as any).write=(s:any)=>{ vt.write(String(s)); return origWrite(s) }
const inst=render(React.createElement(App,{python:'python',bridgeScript:'x',startBridgeClient:sbc,cursorPark:park} as any),{stdout:park.stdout as any,stdin:new R() as any,exitOnCtrlC:false} as any)
await delay(200)
queue.push({type:'user',taskId:1,text:'跑大量输出'})
queue.push({type:'status',status:'running',taskId:1})
const body=['我先执行一个脚本。','[Status] Exit Code: 0','[Stdout]','']
for(let i=0;i<120;i++) body.push('L'+String(i).padStart(3,'0')+' stdout payload ........')
const text=body.join('\n'); const size=Math.ceil(text.length/40)
for(let o=0;o<text.length;o+=size) queue.push({type:'assistant_delta',taskId:1,text:text.slice(o,o+size)})
queue.push({type:'assistant_done',taskId:1,text})
queue.push({type:'status',status:'idle',taskId:1})
await delay(3000); inst.unmount(); await delay(100)

const lines = vt.text()
const joined = lines.join('\n')
const missing:string[]=[]; const dup:string[]=[]
for(let i=0;i<120;i++){
  const tag='L'+String(i).padStart(3,'0')+' stdout payload'
  const n=lines.filter(l=>l.includes(tag)).length
  if(n===0) missing.push(tag.slice(0,3))
  if(n>1) dup.push(tag.slice(0,3)+'x'+n)
}
console.log('TOTAL visual rows:', lines.length)
console.log('missing:', missing.length, missing.slice(0,40).join(','))
console.log('duplicated:', dup.length, dup.slice(0,40).join(','))
console.log('ExitCode line present:', joined.includes('Exit Code: 0'))
console.log('omitted marker:', joined.includes('omitted long output'))
console.log('card header count:', lines.filter(l=>l.includes('code_run')).length)
console.log('--- first 6 non-empty ---')
lines.filter(l=>l.trim()).slice(0,6).forEach(l=>console.log('|'+l))
console.log('--- last 8 non-empty ---')
lines.filter(l=>l.trim()).slice(-8).forEach(l=>console.log('|'+l))
