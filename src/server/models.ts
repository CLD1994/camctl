export interface DraftContent {
  text:string;
  /** 尚不能形成 JSON 值的输入，随草稿保存，存在时禁止导出。 */
  pending?:Record<string,{kind:'number'|'json';text:string}>;
}
export interface Draft { id:string; revision:number; content:DraftContent; createdAt:string; updatedAt:string; exportedRequestId?:string }
export interface ExportedRequest { id:string; draftId:string; body:Record<string,unknown>; exportedAt:string; handedAt:string|null }
export interface Preset { id:string; name:string; deviceId:string; actionType:string; params:unknown; updatedAt:string }
export type ImportStatus='uploading'|'received'|'processing'|'accepted'|'covered'|'duplicate'|'gap'|'waiting_report'|'verifying'|'verified'|'mismatch'|'conflict'|'failed'|'interrupted'|'unavailable';
export interface ImportFile { id:string; batchId:string; fileName:string; kind:'report'|'video'; expectedSize:number; status:ImportStatus; message?:string; bytesReceived:number; reportId?:number; fromWm?:number; toWm?:number; videoId?:string; createdAt:string }
export interface ImportBatch { id:string; fileIds:string[]; createdAt:string; reportsDone:boolean }
export interface Video { id:string; fileName:string; path:string; size:number; sha256?:string; status:'waiting_report'|'verifying'|'verified'|'mismatch'|'unavailable'; message?:string; importId:string; verifiedAgainst?:{size:number;sha256:string}; previousId?:string }
export class AppError extends Error { constructor(readonly code:string,message:string,readonly status=400,readonly issues?:unknown) {super(message);} }
export function errorMessage(error:unknown):string {return error instanceof Error?error.message:String(error);}
