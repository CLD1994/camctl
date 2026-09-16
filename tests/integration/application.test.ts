import { afterEach, describe, expect, it } from 'vitest';
import { mkdtempSync, rmSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Application } from '../../src/server/application';
const apps:Application[]=[]; const dirs:string[]=[];
function setup() {const dir=mkdtempSync(join(tmpdir(),'camctl-app-'));dirs.push(dir);const app=new Application(dir);apps.push(app);app.store.initialize();return app;}
afterEach(()=>{apps.splice(0).forEach(a=>a.store.close());dirs.splice(0).forEach(d=>rmSync(d,{recursive:true,force:true}));});
const content={text:JSON.stringify({name:'同步',actions:[{name:'完整同步',type:'report_status',params:{scope:'full'}}]})};
it("草稿其他类型内容持久化，未完成输入不进入当前请求", () => {
  const app = setup();
  const value = {
    ...content,
    actionVariants: {
      "0": [
        {
          type: "camera_record",
          fields: { device_id: "cam" },
          pending: { "/params": { kind: "json" as const, text: "{" } },
        },
      ],
    },
  };
  const d = app.createDraft(value);
  app.store.close();
  const reopened = new Application(app.store.directory);
  apps.push(reopened);
  expect(reopened.draft(d.id).content).toEqual(value);
  const request = reopened.exportDraft(d.id, d.revision, value);
  expect(request.body.actions).toEqual([
    { name: "完整同步", type: "report_status", params: { scope: "full" } },
  ]);
  expect(request.body).not.toHaveProperty("actionVariants");
});
it("历史追加保留已有动作的各类型内容，新动作独立保存", () => {
  const app = setup();
  const actionVariants = {
    "0": [
      {
        type: "camera_record",
        fields: { device_id: "cam" },
        pending: { "/params": { kind: "json" as const, text: "{" } },
      },
    ],
  };
  const d = app.createDraft({ ...content, actionVariants });
  const updated = app.appendAction(d.id, d.revision, {
    name: "后续报告",
    type: "report_status",
  });
  expect(updated.content.actionVariants).toEqual(actionVariants);
  expect(JSON.parse(updated.content.text).actions).toEqual([
    { name: "完整同步", type: "report_status", params: { scope: "full" } },
    { name: "后续报告", type: "report_status" },
  ]);
});
it.each([
  null,
  [],
  { bad: [] },
  { "0": [{}] },
  {
    "0": [{ fields: {}, pending: { "/params": { kind: "other", text: "{" } } }],
  },
  { "0": [{ fields: { name: "错误共用字段" }, pending: {} }] },
  { "0": [{ fields: {}, pending: { "/name": { kind: "json", text: "{" } } }] },
])("拒绝结构非法的动作类型编辑资料 %j", (actionVariants) => {
  const app = setup();
  expect(() =>
    app.createDraft({ ...content, actionVariants } as any),
  ).toThrowError(expect.objectContaining({ code: "invalid_content" }));
});
describe('草稿及固定请求',()=>{
it('不完整 JSON 保存后原样恢复',()=>{const app=setup();const d=app.createDraft({text:'{"name":'});expect(app.store.get('drafts',d.id)).toMatchObject({content:{text:'{"name":'}});});
it('较早保存不能覆盖新版本',()=>{const app=setup();const d=app.createDraft(content);app.saveDraft(d.id,1,{text:'new'});expect(()=>app.saveDraft(d.id,1,{text:'old'})).toThrow();expect(app.draft(d.id).content.text).toBe('new');});
it('导出最新输入后重试复用固定请求',()=>{const app=setup();const d=app.createDraft({text:'bad'});const r=app.exportDraft(d.id,1,content);const again=app.exportDraft(d.id,1,{text:'bad'});expect(again.id).toBe(r.id);expect(again.body).toEqual(r.body);expect(app.draft(d.id).exportedRequestId).toBe(r.id);});
it('未完成控件输入阻止导出旧合法对象',()=>{const app=setup();const d=app.createDraft(content);expect(()=>app.exportDraft(d.id,1,{...content,pending:{duration:{kind:'number',text:'-'}}})).toThrow();expect(app.draft(d.id).exportedRequestId).toBeUndefined();});
it('非法导出保留可编辑草稿',()=>{const app=setup();const d=app.createDraft({text:'[]'});expect(()=>app.exportDraft(d.id,1,d.content)).toThrow();expect(app.draft(d.id).exportedRequestId).toBeUndefined();});
it('重复人工标记保持首次时间',()=>{const app=setup();const d=app.createDraft(content);const r=app.exportDraft(d.id,1,content);const mark=app.markHandoff(r.id,true);expect(app.markHandoff(r.id,true).handedAt).toBe(mark.handedAt);expect(app.markHandoff(r.id,false).handedAt).toBeNull();expect(app.request(r.id).body).toEqual(r.body);});
it('复制原请求生成独立草稿和新请求',()=>{const app=setup();const d=app.createDraft(content);const r=app.exportDraft(d.id,1,content);const copy=app.copyRequest(r.id);const next=app.exportDraft(copy.id,copy.revision,copy.content);expect(next.id).not.toBe(r.id);expect(next.body.name).toBe(r.body.name);});
it('说明重载失败保留此前完整有效目录',()=>{const app=setup();const path=join(app.store.directory,'device-capabilities.json');writeFileSync(path,readFileSync('docs/superpowers/specs/camctl/examples/capabilities/demo-device.json'));const loaded=app.reloadCapabilities();expect(loaded.active?.devices).toHaveLength(1);writeFileSync(path,'bad JSON');const failed=app.reloadCapabilities();expect(failed.error).not.toBeNull();expect(failed.active).toEqual(loaded.active);});
it('有效空能力目录替换旧目录',()=>{const app=setup();writeFileSync(join(app.store.directory,'device-capabilities.json'),'{"devices":[]}');expect(app.reloadCapabilities()).toMatchObject({active:{devices:[]},error:null});});
it('非法预设更新保留原合法参数',()=>{const app=setup();writeFileSync(join(app.store.directory,'device-capabilities.json'),readFileSync('docs/superpowers/specs/camctl/examples/capabilities/demo-device.json'));app.reloadCapabilities();const first=app.savePreset({name:'常用',deviceId:'demo_cam0',actionType:'camera_record',params:{type:'demo_adjustable',resolution:'4K',frame_rate_fps:30}});expect(()=>app.savePreset({...first,params:{type:'demo_adjustable',resolution:'4K',frame_rate_fps:60}})).toThrow();expect(app.store.get('presets',first.id)).toEqual(first);});
});

it("删除未导出草稿持久化且晚到写入不能重建", () => {
  const app = setup(),
    d = app.createDraft({ text: "{" }),
    other = app.createDraft(content);
  app.deleteDraft(d.id, d.revision);
  app.deleteDraft(d.id, d.revision);
  expect(() => app.saveDraft(d.id, d.revision, content)).toThrowError(
    expect.objectContaining({ code: "not_found" }),
  );
  expect(() => app.exportDraft(d.id, d.revision, content)).toThrowError(
    expect.objectContaining({ code: "not_found" }),
  );
  expect(app.draft(other.id)).toEqual(other);
  app.store.close();
  const reopened = new Application(app.store.directory);
  apps.push(reopened);
  expect(reopened.state().drafts).toEqual([other]);
});
it("删除拒绝过期版本并保留更新内容", () => {
  const app = setup(),
    d = app.createDraft(content);
  const saved = app.saveDraft(d.id, 1, { text: "{" });
  expect(() => app.deleteDraft(d.id, 1)).toThrowError(
    expect.objectContaining({ code: "revision_conflict" }),
  );
  expect(app.draft(d.id)).toEqual(saved);
});
it("删除已导出草稿被拒绝并保留固定请求", () => {
  const app = setup(),
    d = app.createDraft(content),
    r = app.exportDraft(d.id, 1, content);
  expect(() => app.deleteDraft(d.id, 2)).toThrowError(
    expect.objectContaining({ code: "already_exported" }),
  );
  expect(app.request(r.id)).toEqual(r);
  expect(app.draft(d.id).exportedRequestId).toBe(r.id);
});
