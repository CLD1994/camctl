import { chromium } from '@playwright/test';
import { createHash } from 'node:crypto';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { join } from 'node:path';

/** 生成实际可播放的浏览器视频及匹配报告；不把任意媒体冒充规格中的占位摘要。 */
export async function makeMediaFixture(directory:string) {
  await mkdir(directory,{recursive:true});
  const browser=await chromium.launch({headless:true});
  let video:Buffer;
  try {
    const page=await browser.newPage();
    const data=await page.evaluate(async()=>{
      const canvas=document.createElement('canvas');canvas.width=480;canvas.height=270;
      const ctx=canvas.getContext('2d')!;const stream=canvas.captureStream(12);
      const recorder=new MediaRecorder(stream,{mimeType:'video/webm;codecs=vp8'});
      const chunks:Blob[]=[];
      const done=new Promise<Blob>(resolve=>{recorder.ondataavailable=e=>chunks.push(e.data);recorder.onstop=()=>resolve(new Blob(chunks,{type:'video/webm'}));});
      recorder.start();
      for(let frame=0;frame<18;frame++){ctx.fillStyle='#143d35';ctx.fillRect(0,0,480,270);ctx.fillStyle='#d8e7b8';ctx.font='30px sans-serif';ctx.fillText('camctl • local verification',30,110);ctx.fillStyle='#ffffff';ctx.fillRect(30+frame*15,160,40,8);await new Promise(r=>setTimeout(r,85));}
      recorder.stop();const blob=await done;stream.getTracks().forEach(t=>t.stop());return Array.from(new Uint8Array(await blob.arrayBuffer()));
    });
    video=Buffer.from(data);
  } finally {await browser.close();}
  const checksum=createHash('sha256').update(video).digest('hex');
  const report=JSON.parse(await readFile('../../protocol/examples/client-protocol/01-success/status-report-1-6a2d8346b79be33790f613d183707116fb16ec40908850b7e9963195bf4f9b62.json','utf8'));
  const output=report.plans[0].actions[0].outputs[0];output.size=video.length;output.checksum.sha256=checksum;output.original_name='本地验证.webm';output.media_type='video/webm';
  const delivery=report.plans[0].actions[1].deliveries[0];delivery.size=video.length;delivery.sha256=checksum;delivery.file_name='d-001.webm';delivery.display_name='本地验证-正常采集-主录像.webm';delivery.copy.committed_bytes=video.length;
  const bytes=Buffer.from(JSON.stringify(report,null,2)+'\n');const reportName=`status-report-1-${createHash('sha256').update(bytes).digest('hex')}.json`;
  const videoPath=join(directory,delivery.file_name);const reportPath=join(directory,reportName);
  await writeFile(videoPath,video);await writeFile(reportPath,bytes);
  return {videoPath,reportPath,videoName:delivery.file_name as string,reportName,video,reportBytes:bytes};
}
