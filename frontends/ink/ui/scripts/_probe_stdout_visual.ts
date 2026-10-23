import React from 'react'
import { render } from 'ink'
import { EventEmitter } from 'node:events'
import { App } from '../src/App.js'
import { createCursorParkStdout } from '../src/stdoutCursorPark.js'

class W extends EventEmitter { isTTY=true; chunks:string[]=[]; constructor(public columns:number, public rows:number){super()}
  write(s:string){this.chunks.push(String(s));return true} }
class R extends EventEmitter { isTTY=true; setRawMode(){return this} setEncoding(){return this} ref(){return this} unref(){return this} read(){return null} resume(){return this} pause(){return this} }
const delay=(ms:number)=>new Promise(r=>setTimeout(r,ms))
const queue:any[]=[]
const sbc=(_p:any,_b:any,onEvent:any)=>{ setTimeout(()=>onEvent({type:'ready',version:1}),0); const t=setInterval(()=>{const n=queue.shift(); if(n) onEvent(n)},15); return {send(){},stop(){clearInterval(t)}} }

// naive last-frame extractor: strip ANSI, take text after final clear-home
const w=new W(110,30); const park=createCursorParkStdout(w as any)
const inst=render(React.createElement(App,{python:'python',bridgeScript:'x',startBridgeClient:sbc,cursorPark:park} as any),{stdout:park.stdout as any,stdin:new R() as any,exitOnCtrlC:false} as any)
await delay(200)
queue.push({type:'user',taskId:1,text:'跑一下'})
queue.push({type:'status',status:'running',taskId:1})
const body=['我先执行一个脚本。','````text','{','  "script": "print(1)",','}','````','`````','[Action] Running python in GenericAgent: print(1)','[Status] Exit Code: 0','[Stdout]','60e4340 feat(packaging): ship installable GA wheel','dc07912 feat(hooks): implement Claude Code compatible hook runtime','[omitted long output]','tail line','`````'].join('\n')
queue.push({type:'assistant_delta',taskId:1,text:body})
queue.push({type:'assistant_done',taskId:1,text:body})
queue.push({type:'status',status:'idle',taskId:1})
await delay(1500); inst.unmount(); await delay(100)
const all=w.chunks.join('')
const clean=all.replace(/\x1b\[[0-9;?]*[A-Za-z]/g,'').replace(/\x1b\][^\x07]*\x07/g,'')
console.log(clean.split('\n').slice(-45).join('\n'))
