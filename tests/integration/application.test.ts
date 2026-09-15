import { afterEach, describe, expect, it } from 'vitest';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Application } from '../../src/server/application';
const apps:Application[]=[]; const dirs:string[]=[];
function setup() {const dir=mkdtempSync(join(tmpdir(),'camctl-app-'));dirs.push(dir);const app=new Application(dir);apps.push(app);app.store.initialize();return app;}
afterEach(()=>{apps.splice(0).forEach(a=>a.store.close());dirs.splice(0).forEach(d=>rmSync(d,{recursive:true,force:true}));});
const content={text:JSON.stringify({name:'同步',actions:[{name:'完整同步',type:'report_status',params:{scope:'full'}}]})};
describe('草稿及固定请求',()=>{
it('不完整 JSON 保存后原样恢复',()=>{const app=setup();const d=app.createDraft({text:'{"name":'});expect(app.store.get('drafts',d.id)).toMatchObject({content:{text:'{"name":'}});});
it('较早保存不能覆盖新版本',()=>{const app=setup();const d=app.createDraft(content);app.saveDraft(d.id,1,{text:'new'});expect(()=>app.saveDraft(d.id,1,{text:'old'})).toThrow();expect(app.draft(d.id).content.text).toBe('new');});
it('导出最新输入后重试复用固定请求',()=>{const app=setup();const d=app.createDraft({text:'bad'});const r=app.exportDraft(d.id,1,content);const again=app.exportDraft(d.id,1,{text:'bad'});expect(again.id).toBe(r.id);expect(again.body).toEqual(r.body);expect(app.draft(d.id).exportedRequestId).toBe(r.id);});
it('未完成控件输入阻止导出旧合法对象',()=>{const app=setup();const d=app.createDraft(content);expect(()=>app.exportDraft(d.id,1,{...content,pending:{duration:{kind:'number',text:'-'}}})).toThrow();expect(app.draft(d.id).exportedRequestId).toBeUndefined();});
it('非法导出保留可编辑草稿',()=>{const app=setup();const d=app.createDraft({text:'[]'});expect(()=>app.exportDraft(d.id,1,d.content)).toThrow();expect(app.draft(d.id).exportedRequestId).toBeUndefined();});
it('重复人工标记保持首次时间',()=>{const app=setup();const d=app.createDraft(content);const r=app.exportDraft(d.id,1,content);const mark=app.markHandoff(r.id,true);expect(app.markHandoff(r.id,true).handedAt).toBe(mark.handedAt);expect(app.markHandoff(r.id,false).handedAt).toBeNull();expect(app.request(r.id).body).toEqual(r.body);});
it('复制原请求生成独立草稿和新请求',()=>{const app=setup();const d=app.createDraft(content);const r=app.exportDraft(d.id,1,content);const copy=app.copyRequest(r.id);const next=app.exportDraft(copy.id,copy.revision,copy.content);expect(next.id).not.toBe(r.id);expect(next.body.name).toBe(r.body.name);});
});
