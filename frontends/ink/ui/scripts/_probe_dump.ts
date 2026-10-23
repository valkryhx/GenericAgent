
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from '../src/App.js'
import { createCursorParkStdout } from '../src/stdoutCursorPark.js'
class W extends EventEmitter { isTTY=true; chunks=[]; constructor(c,r){super();this.columns=c;this.rows=r}
  write(s){this.chunks.push(String(s));return true} }
class R extends EventEmitter { isTTY=true; setRawMode(){return this} setEncoding(){return this} ref(){return this} unref(){return this} read(){return null} resume(){return this} pause(){return this} }
const delay=(ms)=>new Promise(r=>setTimeout(r,ms))
const queue=[]
const sbc=(_p,_b,onEvent)=>{ setTimeout(()=>onEvent({type:'ready',version:1}),0); const t=setInterval(()=>{const n=queue.shift(); if(n) onEvent(n)},15); return {send(){},stop(){clearInterval(t)}} }
const w=new W(100,24); const park=createCursorParkStdout(w); const inst=render(React.createElement(App,{python:'python',bridgeScript:'x',startBridgeClient:sbc,cursorPark:park}),{stdout:park.stdout,stdin:new R(),exitOnCtrlC:false})
await delay(200)
queue.push({type:'user',taskId:1,text:'跑大量输出'})
queue.push({type:'status',status:'running',taskId:1})
const body=['我先执行一个脚本。','']; for(let i=0;i<120;i++) body.push('L'+String(i).padStart(3,'0')+' stdout payload ........')
const text=body.join('\n'); const size=Math.ceil(text.length/40)
for(let o=0;o<text.length;o+=size) queue.push({type:'assistant_delta',taskId:1,text:text.slice(o,o+size)})
queue.push({type:'assistant_done',taskId:1,text})
queue.push({type:'status',status:'idle',taskId:1})
await delay(3000); inst.unmount(); await delay(100)
console.log('WRITES',w.chunks.length)
w.chunks.forEach((c,i)=>{
  const esc = c.replace(/\x1b/g,'<ESC>').replace(/\n/g,'<LF>\n')
  console.log('=== write#'+i+' len='+c.length+' ===')
  console.log(esc.length>1200? esc.slice(0,600)+'\n...[snip]...\n'+esc.slice(-600) : esc)
})
