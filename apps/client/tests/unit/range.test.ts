import { expect, it } from 'vitest';
import { byteRange } from '../../src/server/range';
it.each([['bytes=0-2',10,{start:0,end:2}],['bytes=3-',10,{start:3,end:9}],['bytes=-3',10,{start:7,end:9}],['bytes=1-100',10,{start:1,end:9}]])('计算实际媒体范围 %s',(input,size,want)=>{expect(byteRange(input as string,size as number)).toEqual(want);});
it.each(['bytes=20-30','bytes=3-1','bytes=-0','bytes=a-b','bytes=1-2,4-5'])('拒绝不可满足范围 %s',input=>{expect(()=>byteRange(input,10)).toThrow();});
