import { afterEach, expect, it } from 'vitest';
import { mkdtempSync,rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { Server } from 'node:http';
import { Application } from '../../src/server/application';
import { Files } from '../../src/server/files';
import { createHttpApp } from '../../src/server/http';
const clean:Array<()=>Promise<void>>=[];
afterEach(async()=>{for(const c of clean.splice(0))await c();});
async function setup(){const dir=mkdtempSync(join(tmpdir(),'camctl-http-'));const app=new Application(dir);const files=new Files(app);const server=await new Promise<Server>(resolve=>{const s=createHttpApp(app,files).listen(0,'127.0.0.1',()=>resolve(s));});const address=server.address() as {port:number};clean.push(async()=>{await files.idle();await new Promise<void>((r,j)=>server.close(e=>e?j(e):r()));app.store.close();rmSync(dir,{recursive:true,force:true});});return `http://127.0.0.1:${address.port}`;}
it('HTTP 显式初始化后保存并导出计划',async()=>{const base=await setup();expect((await (await fetch(base+'/api/state')).json()).startup.state).toBe('uninitialized');await fetch(base+'/api/initialize',{method:'POST'});const content={text:JSON.stringify({name:'状态',actions:[{name:'同步',type:'report_status',params:{scope:'full'}}]})};const created=await fetch(base+'/api/drafts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content})});const d=await created.json();expect(created.status).toBe(201);const exported=await fetch(`${base}/api/drafts/${d.id}/export`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content,revision:d.revision})});expect(exported.status).toBe(200);const r=await exported.json();const downloaded=await(await fetch(`${base}/api/requests/${r.id}/download`)).json();expect(downloaded.request_id).toBe(r.id);});
it('跨来源写入被拒绝且数据库未初始化',async()=>{const base=await setup();const result=await fetch(base+'/api/initialize',{method:'POST',headers:{Origin:'https://other.example'}});expect(result.status).toBe(403);expect((await(await fetch(base+'/api/state')).json()).startup.state).toBe('uninitialized');});
