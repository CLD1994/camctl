import { randomUUID, createHash } from 'node:crypto';
import { createReadStream, createWriteStream } from 'node:fs';
import { mkdir, open, readFile, stat, unlink } from 'node:fs/promises';
import { join, relative } from 'node:path';
import { Readable, Transform } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import type { Application } from './application';
import { AppError, errorMessage, type ImportBatch, type ImportFile, type Video } from './models';

interface CompleteFile {id:string;path:string;size:number;fileName:string;importId:string;processed:boolean}
interface Mapping {size:number;sha256:string}
async function fingerprint(path:string):Promise<Mapping> {const hash=createHash('sha256');let size=0;for await(const chunk of createReadStream(path)){size+=chunk.length;hash.update(chunk);}return {size,sha256:hash.digest('hex')};}
function same(a:Mapping,b:Mapping) {return a.size===b.size&&a.sha256===b.sha256;}

export class Files {
  private queue:Promise<void>=Promise.resolve();
  private reportQueue:Promise<void>=Promise.resolve();
  private activeUploads=new Set<string>();
  private processingBatches=new Set<string>();
  workerError:string|null=null;
  constructor(readonly app:Application) {}
  private get store(){return this.app.store;}
  private complete(id:string):CompleteFile|undefined {const row=this.store.get<CompleteFile>('complete_files',id);return row?{...row,path:join(this.store.directory,row.path)}:undefined;}
  private saveComplete(row:CompleteFile) {this.store.set('complete_files',row.id,{...row,path:relative(this.store.directory,row.path)});}
  private saveVideo(row:Video) {this.store.set('videos',row.fileName,{...row,path:relative(this.store.directory,row.path)});}
  private file(id:string):ImportFile {const f=this.store.get<ImportFile>('imports',id);if(!f)throw new AppError('not_found','导入文件不存在',404);return f;}
  private update(id:string,patch:Partial<ImportFile>) {this.store.set('imports',id,{...this.file(id),...patch});}
  createBatch(input:Array<{fileName:string;size:number;kind:'report'|'video'}>) {
    if(!Array.isArray(input)||!input.length)throw new AppError('invalid_batch','请选择文件');
    for(const f of input){if(!f||typeof f.fileName!=='string'||!f.fileName||/[\x00-\x1f/\\]/.test(f.fileName)||!Number.isSafeInteger(f.size)||f.size<0||!['report','video'].includes(f.kind))throw new AppError('invalid_file','文件名、类型或大小无效');}
    return this.store.transaction(()=>{const id=randomUUID();const now=new Date().toISOString();const files:ImportFile[]=input.map(f=>({id:randomUUID(),batchId:id,fileName:f.fileName,expectedSize:f.size,kind:f.kind,status:'uploading',bytesReceived:0,createdAt:now}));const batch:ImportBatch={id,fileIds:files.map(f=>f.id),createdAt:now,reportsDone:false};this.store.set('batches',id,batch);for(const f of files)this.store.set('imports',f.id,f);return {...batch,files};});
  }
  async upload(id:string,stream:Readable):Promise<ImportFile> {
    const file=this.file(id);
    if(file.status!=='uploading'||this.activeUploads.has(id))throw new AppError('upload_state','该上传已开始或结束，请查询实际结果',409);
    this.activeUploads.add(id);const path=join(this.store.directory,'uploads',`${id}.part`);
    try {
      await mkdir(join(this.store.directory,'uploads'),{recursive:true});
      let size=0;let saved=0;let last=Date.now();
      const count=new Transform({transform:(chunk,encoding,callback)=>{size+=chunk.length;if(size>file.expectedSize){callback(new AppError('upload_size','上传字节超过所声明大小'));return;}if(Date.now()-last>250){try{this.update(id,{bytesReceived:size});saved=size;last=Date.now();}catch(error){callback(error as Error);return;}}callback(null,chunk);}});
      await pipeline(stream,count,createWriteStream(path,{flags:'wx'}));
      if(size!==file.expectedSize)throw new AppError('upload_incomplete',`上传未完成：${size}/${file.expectedSize} 字节`);
      const fd=await open(path,'r+');try{await fd.sync();}finally{await fd.close();}
      this.store.transaction(()=>{this.saveComplete({id,path,size,fileName:file.fileName,importId:id,processed:false});this.update(id,{status:'received',bytesReceived:size});});
      if(file.kind==='video')this.enqueue(()=>this.processVideo(id));
      else this.enqueueReport(()=>this.processBatch(file.batchId));
    } catch(error) {
      try{this.update(id,{status:'interrupted',message:errorMessage(error)});}catch(writeError){this.workerError=`${errorMessage(error)}；结果保存失败：${errorMessage(writeError)}`;}
      this.enqueueReport(()=>this.processBatch(file.batchId));throw error;
    } finally {this.activeUploads.delete(id);}
    return this.file(id);
  }
  async failUpload(id:string,message:string) {if(this.activeUploads.has(id))throw new AppError('upload_active','文件仍在上传，请等待实际结果',409);const file=this.file(id);if(file.status==='uploading'){this.update(id,{status:'interrupted',message});this.enqueueReport(()=>this.processBatch(file.batchId));}return this.file(id);}
  private enqueue(job:()=>Promise<void>) {this.queue=this.queue.then(job).catch(error=>{this.workerError=errorMessage(error);});}
  private enqueueReport(job:()=>Promise<void>) {this.reportQueue=this.reportQueue.then(job).catch(error=>{this.workerError=errorMessage(error);});}
  async idle() {await this.reportQueue;await this.queue;}
  private mapping(name:string):Mapping|undefined {
    for(const plan of this.app.snapshot().plans??[])for(const action of plan.actions??[])if('deliveries' in action)for(const delivery of action.deliveries??[]){if(delivery.file_name===name&&typeof delivery.size==='number'&&typeof delivery.sha256==='string')return {size:delivery.size,sha256:delivery.sha256};}
    return undefined;
  }
  private async processBatch(id:string) {
    const batch=this.store.get<ImportBatch>('batches',id);if(!batch||batch.reportsDone||this.processingBatches.has(id))return;
    const files=batch.fileIds.map(fid=>this.file(fid)).filter(f=>f.kind==='report');
    if(files.some(f=>f.status==='uploading'))return;
    this.processingBatches.add(id);
    try {
      const inputs:Array<{file:ImportFile;bytes:Uint8Array}>=[];
      for(const f of files.filter(f=>f.status==='received')){const complete=this.complete(f.id);if(!complete){this.update(f.id,{status:'failed',message:'缺少完整文件保存记录'});continue;}try{inputs.push({file:f,bytes:await readFile(complete.path)});}catch(error){this.update(f.id,{status:'failed',message:errorMessage(error)});}}
      this.app.applyReports(inputs);
      this.store.set('batches',id,{...batch,reportsDone:true});
      for(const f of files){const complete=this.complete(f.id);if(complete){this.saveComplete({...complete,processed:true});await this.removeTemporary(complete.path,f.id);}}
      for(const video of this.videos())if(video.status==='waiting_report'||video.status==='unavailable')this.enqueue(()=>this.verifyCurrent(video));
    } finally {this.processingBatches.delete(id);}
  }
  private async removeTemporary(path:string,id:string) {try{await unlink(path);}catch(error){if((error as NodeJS.ErrnoException).code!=='ENOENT'){this.store.set('file_diagnostics',randomUUID(),{id,path,message:`临时文件整理失败：${errorMessage(error)}`});}}}
  async processVideo(id:string) {
    const file=this.file(id);const complete=this.complete(id);if(!complete||complete.processed)return;
    this.update(id,{status:'verifying'});
    try {
      const newHash=await fingerprint(complete.path);
      if(newHash.size!==complete.size)throw new AppError('file_changed','文件大小与完整保存记录不一致');
      const mapping=this.mapping(file.fileName);
      const previous=this.videos().find(v=>v.fileName===file.fileName);
      if(previous){
        const oldHash=await fingerprint(previous.path);
        if(oldHash.size!==previous.size || (previous.sha256!==undefined&&oldHash.sha256!==previous.sha256))throw new AppError('file_changed','已有副本的实际内容与保存记录矛盾，无法决定替换');
        if(same(oldHash,newHash)){this.update(id,{status:'duplicate',videoId:previous.id,message:'相同内容已导入，复用已有副本'});this.saveComplete({...complete,processed:true});await this.removeTemporary(complete.path,id);await this.verifyCurrent(previous);return;}
        if(!mapping||!same(newHash,mapping)||same(oldHash,mapping)){this.update(id,{status:'conflict',message:!mapping?'同名文件内容冲突，等待报告核对':!same(newHash,mapping)?'新副本不符合报告，已有副本保留':'已有副本符合报告，不可覆盖'});this.saveComplete({...complete,processed:true});await this.removeTemporary(complete.path,id);return;}
      }
      const verified=mapping?same(newHash,mapping):false;
      const video:Video={id,fileName:file.fileName,path:complete.path,size:newHash.size,sha256:newHash.sha256,status:mapping?(verified?'verified':'mismatch'):'waiting_report',importId:id,...(verified?{verifiedAgainst:mapping}:{}),...(previous?{previousId:previous.id}:{})};
      this.store.transaction(()=>{this.saveVideo(video);this.saveComplete({...complete,processed:true});this.update(id,{status:video.status,videoId:id,message:verified?'大小与 SHA-256 均匹配':mapping?'文件内容与报告不符':'文件已保存，等待对应报告'});});
      if(previous)await this.removeTemporary(previous.path,previous.importId);
    } catch(error){this.update(id,{status:'unavailable',message:errorMessage(error)});}
  }
  private async verifyCurrent(video:Video) {
    try {
      const stats=await stat(video.path);if(!stats.isFile()||stats.size!==video.size)throw new AppError('file_changed','当前文件与完整保存记录不符');
      const fd=await open(video.path,'r');await fd.close();
      if(video.status==='verified'||video.status==='mismatch')return;
      const mapping=this.mapping(video.fileName);if(!mapping){this.saveVideo({...video,status:'waiting_report',message:undefined});return;}
      this.saveVideo({...video,status:'verifying'});this.update(video.importId,{status:'verifying'});
      const hash=await fingerprint(video.path);
      const matched=same(hash,mapping);const next:Video={...video,sha256:hash.sha256,status:matched?'verified':'mismatch',message:matched?undefined:'大小或摘要与报告不符',...(matched?{verifiedAgainst:mapping}:{})};
      this.store.transaction(()=>{this.saveVideo(next);this.update(video.importId,{status:next.status,message:next.message});});
    }catch(error){const message=errorMessage(error);this.saveVideo({...video,status:'unavailable',message});this.update(video.importId,{status:'unavailable',message});}
  }
  videos():Video[] {return this.store.all<Video>('videos').map(v=>({...v,path:join(this.store.directory,v.path)}));}
  async openVideo(id:string):Promise<Video> {
    const video=this.videos().find(v=>v.id===id);if(!video)throw new AppError('not_found','视频不存在',404);
    if(video.status!=='verified'||!video.verifiedAgainst)throw new AppError('video_not_verified','视频尚未满足播放和下载条件',409);
    const mapping=this.mapping(video.fileName);if(!mapping||!same(mapping,video.verifiedAgainst))throw new AppError('video_mapping','当前报告映射不满足已保存核验结果',409);
    try{const stats=await stat(video.path);if(!stats.isFile()||stats.size!==video.size)throw Error('文件大小已变化');const fd=await open(video.path,'r');await fd.close();}catch(error){this.saveVideo({...video,status:'unavailable',message:errorMessage(error)});throw new AppError('video_unavailable','已保存的视频当前不可读取',409);}
    return video;
  }
  async recover() {
    if(this.store.status().state!=='ready')return;
    for(const file of this.store.all<ImportFile>('imports')){
      const complete=this.complete(file.id);
      if(file.kind==='report'&&['uploading','received','processing'].includes(file.status)){this.update(file.id,{status:'interrupted',message:'后端处理中断，请重新选择原批报告'});continue;}
      if(file.kind==='video'&&complete&&!complete.processed)this.enqueue(()=>this.processVideo(file.id));
      else if(file.status==='uploading')this.update(file.id,{status:'interrupted',message:'上传未完成，请重新选择文件从头上传'});
    }
    for(const v of this.videos())this.enqueue(()=>this.verifyCurrent(v));
  }
}
