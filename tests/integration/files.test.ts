import { afterEach, expect, it } from 'vitest';
import { mkdtempSync, rmSync, readFileSync, unlinkSync, cpSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { Readable } from 'node:stream';
import { createHash } from 'node:crypto';
import { Application } from '../../src/server/application';
import { Files } from '../../src/server/files';
import type { Video } from '../../src/server/models';
const dirs:string[]=[];const contexts:Array<{app:Application;files:Files}>=[];
function setup(){const dir=mkdtempSync(join(tmpdir(),'camctl-files-'));dirs.push(dir);const app=new Application(dir);app.store.initialize();const files=new Files(app);contexts.push({app,files});return {app,files,dir};}
afterEach(async()=>{for(const c of contexts.splice(0)){await c.files.idle();c.app.store.close();}for(const d of dirs.splice(0))rmSync(d,{recursive:true,force:true});});
async function upload(files:Files,name:string,bytes:Buffer){const batch=files.createBatch([{fileName:name,size:bytes.length,kind:'video'}]);await files.upload(batch.files[0].id,Readable.from([bytes]));await files.idle();return batch.files[0];}
function mapping(app:Application,bytes:Buffer){const r=JSON.parse(readFileSync('docs/superpowers/specs/camctl/examples/client-protocol/01-success/status-report-1-6a2d8346b79be33790f613d183707116fb16ec40908850b7e9963195bf4f9b62.json','utf8'));const delivery=r.plans[0].actions.find((a:{type:string})=>a.type==='obtain_action_outputs').deliveries[0];delivery.size=bytes.length;delivery.sha256=createHash('sha256').update(bytes).digest('hex');app.store.set('state','snapshot',r);return delivery.file_name as string;}
it('完整视频无报告时保存并拒绝下载',async()=>{const {files}=setup();await upload(files,'d-test.mp4',Buffer.from('video'));expect(files.videos()[0].status).toBe('waiting_report');await expect(files.openVideo(files.videos()[0].id)).rejects.toThrow();});
it('报告映射到达后后台核验已保存视频',async()=>{const {app,files}=setup();const bytes=Buffer.from('video');const name=mapping(app,bytes);await upload(files,name,bytes);const video=files.videos()[0];expect(video.status).toBe('verified');expect(readFileSync((await files.openVideo(video.id)).path)).toEqual(bytes);});
it('重复相同内容只保留一个当前副本',async()=>{const {files}=setup();await upload(files,'d-test.mp4',Buffer.from('same'));await upload(files,'d-test.mp4',Buffer.from('same'));expect(files.videos()).toHaveLength(1);});
it('同名不同内容无报告时保留先前副本',async()=>{const {files}=setup();await upload(files,'d-test.mp4',Buffer.from('first'));await upload(files,'d-test.mp4',Buffer.from('second'));const v=files.videos()[0];expect(readFileSync(v.path).toString()).toBe('first');});
it('正确补发替换已确认错误的副本',async()=>{const {app,files}=setup();const good=Buffer.from('correct');const name=mapping(app,good);await upload(files,name,Buffer.from('bad'));expect(files.videos()[0].status).toBe('mismatch');await upload(files,name,good);expect(files.videos()[0].status).toBe('verified');expect(readFileSync(files.videos()[0].path)).toEqual(good);});
it('未完整上传不得登记完整副本',async()=>{const {files}=setup();const batch=files.createBatch([{fileName:'d-test.mp4',kind:'video',size:10}]);await expect(files.upload(batch.files[0].id,Readable.from([Buffer.from('short')]))).rejects.toThrow();expect(files.videos()).toHaveLength(0);});
it('已核验文件丢失时拒绝读取',async()=>{const {app,files}=setup();const bytes=Buffer.from('video');await upload(files,mapping(app,bytes),bytes);const video=files.videos()[0];unlinkSync(video.path);await expect(files.openVideo(video.id)).rejects.toThrow();});
it('重启恢复等待报告文件，不要求重新上传',async()=>{const {app,files}=setup();await upload(files,'d-001.mp4',Buffer.from('video'));mapping(app,Buffer.from('video'));const next=new Files(app);await next.recover();await next.idle();expect(next.videos()[0].status).toBe('verified');});
it('完整目录恢复到其他位置仍能读取原视频',async()=>{const {app,files,dir}=setup();const bytes=Buffer.from('video');await upload(files,mapping(app,bytes),bytes);await files.idle();app.store.close();const restored=mkdtempSync(join(tmpdir(),'camctl-restored-'));dirs.push(restored);cpSync(dir,restored,{recursive:true});rmSync(dir,{recursive:true,force:true});const next=new Application(restored);const nextFiles=new Files(next);contexts.push({app:next,files:nextFiles});await nextFiles.recover();await nextFiles.idle();const v=nextFiles.videos()[0];expect(v.status).toBe('verified');expect(readFileSync((await nextFiles.openVideo(v.id)).path)).toEqual(bytes);});
