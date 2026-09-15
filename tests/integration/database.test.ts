import { afterEach, describe, expect, it } from 'vitest';
import { mkdtempSync, rmSync, writeFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Store } from '../../src/server/database';

const dirs: string[] = [];
const stores: Store[] = [];
function setup() { const dir = mkdtempSync(join(tmpdir(), 'camctl-db-')); dirs.push(dir); const store = new Store(dir); stores.push(store); return {dir,store}; }
afterEach(() => { stores.splice(0).forEach(s=>s.close()); dirs.splice(0).forEach(p=>rmSync(p,{recursive:true,force:true})); });
describe('客户端数据库启动与事务', () => {
  it('普通启动不创建数据库', () => { const {dir,store}=setup(); expect(store.status().state).toBe('uninitialized'); expect(existsSync(join(dir,'client.db'))).toBe(false); });
  it('显式初始化保存完成事实并可以重开', () => { const {dir,store}=setup(); store.initialize(); store.set('drafts','one',{text:'{"name":'}); store.close(); const next=new Store(dir); stores.push(next); expect(next.status().state).toBe('ready'); expect(next.get('drafts','one')).toEqual({text:'{"name":'}); });
  it('重复初始化不覆盖已保存内容', () => { const {store}=setup(); store.initialize(); store.set('drafts','one',{name:'草稿'}); store.initialize(); expect(store.get('drafts','one')).toEqual({name:'草稿'}); });
  it('业务文件存在而库缺失时拒绝初始化', () => { const {dir,store}=setup(); writeFileSync(join(dir,'video.mp4'),'video'); expect(store.status().state).toBe('fault'); expect(()=>store.initialize()).toThrow(); expect(existsSync(join(dir,'client.db'))).toBe(false); });
  it('损坏库保留原文件且拒绝初始化', () => { const {dir,store}=setup(); writeFileSync(join(dir,'client.db'),'invalid'); expect(store.status().state).toBe('fault'); expect(()=>store.initialize()).toThrow(); });
  it('能力说明不阻止显式初始化', () => { const {dir,store}=setup(); writeFileSync(join(dir,'device-capabilities.json'),'bad'); store.initialize(); expect(store.status().state).toBe('ready'); });
  it('失败事务不留下部分报告及水位', () => { const {store}=setup(); store.initialize(); expect(()=>store.transaction(()=>{store.set('state','coverage',20); store.saveReport(1,'original.json',Buffer.from('original'),0,20); throw Error('写入故障');})).toThrow(); expect(store.get('state','coverage')).toBeUndefined(); expect(store.report(1)).toBeUndefined(); });
  it('成功事务保留原始字节并与状态共同恢复', () => { const {store}=setup(); store.initialize(); const bytes=Buffer.from('{\r\n "a": 1\r\n}'); store.transaction(()=>{store.saveReport(1,'original.json',bytes,0,20); store.set('state','coverage',20);}); expect(Buffer.from(store.report(1)!.bytes)).toEqual(bytes); expect(store.get('state','coverage')).toBe(20); });
});
