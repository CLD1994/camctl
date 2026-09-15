import { beforeAll, afterAll, afterEach, expect, it } from 'vitest';
import { chromium, expect as browserExpect, type Browser } from '@playwright/test';
import { mkdtempSync, rmSync, copyFileSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { Server } from 'node:http';
import { Application } from '../../src/server/application';
import { Files } from '../../src/server/files';
import { createHttpApp } from '../../src/server/http';
import { makeMediaFixture } from '../helpers/media';

let browser:Browser;let fixtures:Awaited<ReturnType<typeof makeMediaFixture>>;let fixtureDirectory:string;
const clean:Array<()=>Promise<void>>=[];
beforeAll(async()=>{browser=await chromium.launch({headless:true});fixtureDirectory=mkdtempSync(join(tmpdir(),'camctl-browser-media-'));fixtures=await makeMediaFixture(fixtureDirectory);},20000);
afterAll(async()=>{await browser?.close();if(fixtureDirectory)rmSync(fixtureDirectory,{recursive:true,force:true});});
afterEach(async()=>{for(const c of clean.splice(0))await c();});
async function setup(){
  const directory=mkdtempSync(join(tmpdir(),'camctl-browser-'));copyFileSync('docs/superpowers/specs/camctl/examples/capabilities/demo-device.json',join(directory,'device-capabilities.json'));
  const application=new Application(directory);const files=new Files(application);
  const server=await new Promise<Server>(resolve=>{const s=createHttpApp(application,files).listen(0,'127.0.0.1',()=>resolve(s));});
  const base=`http://127.0.0.1:${(server.address() as {port:number}).port}`;const context=await browser.newContext({acceptDownloads:true});const page=await context.newPage();
  clean.push(async()=>{await context.close();await files.idle();await new Promise<void>((resolve,reject)=>server.close(e=>e?reject(e):resolve()));application.store.close();rmSync(directory,{recursive:true,force:true});});
  const response=await page.goto(base);expect(response?.status(), '构建后的客户端首页应可访问').toBe(200);return {application,files,page,base};
}
it('网页初始化并恢复未完成的草稿输入',async()=>{
  const {page,application}=await setup();
  await page.getByTestId('initialize-button').click();await page.getByTestId('new-draft-button').click();
  await page.getByTestId('draft-json-toggle').click();await page.getByTestId('draft-json-input').fill('{"name":"未完成",');
  await browserExpect(page.getByTestId('save-status')).toContainText('已保存');
  expect(application.store.all<{content:{text:string}}>('drafts')[0].content.text).toBe('{"name":"未完成",');
  await page.reload();await page.getByTestId('draft-open-button').first().click();await page.getByTestId('draft-json-toggle').click();
  await browserExpect(page.getByTestId('draft-json-input')).toHaveValue('{"name":"未完成",');
},20000);
it('网页导出与再次下载保持同一请求，复制后产生新请求',async()=>{
  const {page,application}=await setup();await page.getByTestId('initialize-button').click();await page.getByTestId('new-draft-button').click();
  await page.getByTestId('draft-json-toggle').click();await page.getByTestId('draft-json-input').fill(JSON.stringify({name:'网页同步',actions:[{name:'完整同步',type:'report_status',params:{scope:'full'}}]}));
  const firstEvent=page.waitForEvent('download');await page.getByTestId('export-button').click();const first=JSON.parse(readFileSync((await(await firstEvent).path())!,'utf8'));
  const nextEvent=page.waitForEvent('download');await page.getByTestId('download-request-button').click();const next=JSON.parse(readFileSync((await(await nextEvent).path())!,'utf8'));expect(next.request_id).toBe(first.request_id);
  await page.getByTestId('copy-request-button').click();const copyEvent=page.waitForEvent('download');await page.getByTestId('export-button').click();const copy=JSON.parse(readFileSync((await(await copyEvent).path())!,'utf8'));expect(copy.request_id).not.toBe(first.request_id);expect(application.store.all('requests')).toHaveLength(2);
},20000);
it('网页混合导入真实报告与视频，核验后播放并按范围下载',async()=>{
  const {page,application,base}=await setup();await page.getByTestId('initialize-button').click();await page.getByTestId('nav-import').click();
  await page.getByTestId('import-files').setInputFiles([fixtures.videoPath,fixtures.reportPath]);
  await browserExpect.poll(()=>application.coverage()).toBe(20);
  await page.getByTestId('nav-plans').click();await page.getByTestId('tab-records').click();await page.getByTestId('record-open-button').first().click();
  const video=page.locator('video').first();await browserExpect(video).toBeVisible();await browserExpect.poll(()=>video.evaluate((v:HTMLVideoElement)=>v.readyState)).toBeGreaterThanOrEqual(2);
  await video.evaluate((v:HTMLVideoElement)=>v.play());await browserExpect.poll(()=>video.evaluate((v:HTMLVideoElement)=>v.currentTime)).toBeGreaterThan(0);
  const current=application.store.all<{id:string}>('videos')[0];const ranged=await fetch(`${base}/api/videos/${current.id}/content`,{headers:{Range:'bytes=0-15'}});expect(ranged.status).toBe(206);expect(Buffer.from(await ranged.arrayBuffer())).toEqual(fixtures.video.subarray(0,16));
},20000);
