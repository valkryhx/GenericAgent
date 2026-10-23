import React from 'react'
import { render } from 'ink'
import { EventEmitter } from 'node:events'
import { App } from '../src/App.js'
import { createCursorParkStdout } from '../src/stdoutCursorPark.js'
class W extends EventEmitter { isTTY=true; chunks:string[]=[]; constructor(public columns:number, public rows:number){super()} write(s:string){this.chunks.push(String(s));return true} }
class R extends EventEmitter { isTTY=true; setRawMode(){return this} setEncoding(){return this} ref(){return this} unref(){return this} read(){return null} resume(){return this} pause(){return this} }
const delay=(ms:number)=>new Promise(r=>setTimeout(r,ms))
const queue:any[]=[]
const sbc=(_p:any,_b:any,onEvent:any)=>{ setTimeout(()=>onEvent({type:'ready',version:1}),0); const t=setInterval(()=>{const n=queue.shift(); if(n) onEvent(n)},15); return {send(){},stop(){clearInterval(t)}} }
class VT { rows:string[][]=[[]]; r=0;c=0
  write(s:string){ for(let i=0;i<s.length;i++){const ch=s[i]; if(ch==='\x1b'){const m=/^\x1b\[([0-9;?]*)([A-Za-z])/.exec(s.slice(i)); if(m){const n=m[1]===''?1:parseInt(m[1].replace(/[?]/g,''),10)||1; const cmd=m[2]; if(cmd==='A')this.r=Math.max(0,this.r-n); else if(cmd==='B'){this.r+=n;this.ensure()} else if(cmd==='C')this.c+=n; else if(cmd==='D')this.c=Math.max(0,this.c-n); else if(cmd==='G')this.c=Math.max(0,n-1); else if(cmd==='H'){this.r=0;this.c=0} else if(cmd==='K'){this.rows[this.r]=[]} else if(cmd==='J'){const k=parseInt(m[1]||'0',10); if(k===2||k===3){this.rows=[[]];this.r=0;this.c=0}} i+=m[0].length-1; continue} const m2=/^\x1b\][^\x07]*\x07/.exec(s.slice(i)); if(m2){i+=m2[0].length-1;continue} continue} if(ch==='\n'){this.r+=1;this.c=0;this.ensure();continue} if(ch==='\r'){this.c=0;continue} this.ensure(); const line=this.rows[this.r]; while(line.length<this.c)line.push(' '); line[this.c]=ch; this.c+=1 } }
  ensure(){while(this.rows.length<=this.r)this.rows.push([])} text(){return this.rows.map(l=>l.join('').trimEnd())} }
const w=new W(110,30); const park=createCursorParkStdout(w as any); const vt=new VT(); const orig=w.write.bind(w); (w as any).write=(s:any)=>{vt.write(String(s)); return orig(s)}
const inst=render(React.createElement(App,{python:'python',bridgeScript:'x',startBridgeClient:sbc,cursorPark:park} as any),{stdout:park.stdout as any,stdin:new R() as any,exitOnCtrlC:false} as any)
await delay(200)
queue.push({type:'user',taskId:1,text:'跑一下'})
queue.push({type:'status',status:'running',taskId:1})
const text=['我先执行一个脚本。','⏺ Tool: `code_run` args:','````text','{"script":"print(1)"}','````','`````','[Action] Running python in GenericAgent: print(1)','[Status] Exit Code: 0','[Stdout]','60e4340 feat(packaging): ship installable GA wheel','dc07912 feat(hooks): implement Claude Code compatible hook runtime','`````'].join('\n')
queue.push({type:'assistant_delta',taskId:1,text})
queue.push({type:'assistant_done',taskId:1,text})
queue.push({type:'status',status:'idle',taskId:1})
await delay(1500); inst.unmount(); await delay(100)
const lines=vt.text(); console.log(lines.filter(l=>l.trim()).slice(-30).join('\n'))
