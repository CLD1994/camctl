import { AppError } from './models';
export function byteRange(header:string,size:number):{start:number;end:number} {
  const match=/^bytes=(\d*)-(\d*)$/.exec(header);
  const invalid=()=>new AppError('range_not_satisfiable','请求的视频范围不可满足',416);
  if(!match||(!match[1]&&!match[2])||size===0)throw invalid();
  let start:number,end:number;
  if(!match[1]){const suffix=Number(match[2]);if(!Number.isSafeInteger(suffix)||suffix<=0)throw invalid();start=Math.max(0,size-suffix);end=size-1;}
  else {start=Number(match[1]);end=match[2]?Number(match[2]):size-1;}
  if(!Number.isSafeInteger(start)||!Number.isSafeInteger(end)||start>=size||start>end)throw invalid();
  return {start,end:Math.min(end,size-1)};
}
