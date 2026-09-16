import { expect, it } from 'vitest';
import { validatePlan } from '../../src/shared/plan';
import { validateReport, validateReportAgainstHistory } from '../../src/domain/reports';
import { deviceOptions } from '../../src/web/device-options';
import type { Capabilities, StatusReport } from '../../src/shared/types';

const capabilities: Capabilities = {devices: ['camera_take_photo', 'camera_timelapse'].map((type, i) => ({device_id: `cam${i}`, driver_id:'demo', actions:[{type,parameter_types:[{type:'fixed',name:'固定',description:'样例',schema:{$schema:'https://json-schema.org/draft/2020-12/schema',type:'object',properties:{type:{const:'fixed'}},required:['type'],additionalProperties:false}}]}]}))};
const action = (type = 'camera_take_photo') => ({name:'拍摄',type,device_id:type==='camera_take_photo'?'cam0':'cam1',scheduled_at:'2026-09-16 00:00:00',policy:{max_delay_ms:0},params:{type:'fixed'}});
const plan = (actions: unknown[]) => ({request_id:'r1',name:'计划',created_at:'2026-09-16 00:00:00',actions});
const report = (status = 'canceled'): unknown => ({report_id:1,from_wm:0,to_wm:10,plans:[{plan_instance_id:'p1',request_id:'r1',plan_seq:1,name:'计划',created_at:'2026-09-16 00:00:00',status:'completed',actions:[{...action(),params:undefined,action_instance_id:'a1',input_params:{type:'fixed'},effective_params:{type:'fixed'},status,execution:{started:true},result:{capture:{status:status==='succeeded'?'completed':'canceled',captured_count:1}},outputs:[{output_id:'o1',source_action_instance_id:'a1',kind:'original',media_type:'image/png',availability:'available',cleanup:{status:'not_requested'},checksum:{status:'not_obtained'},media:{check_status:'not_performed',duration:{status:'unknown'}}}]}]}]});

it.each(['camera_take_photo','camera_timelapse'])('接受设备支持的 %s 及本计划取回', type => {
 expect(validatePlan(plan([action(type),{name:'取回',type:'obtain_action_outputs',scheduled_at:'2026-09-16 01:00:00',params:{source:{action_name:'拍摄'}}}]),capabilities)).toEqual([]);
});
it('动作选择只列出已部署拍摄能力',()=>{
 expect(deviceOptions(capabilities,'camera_take_photo').devices.map(d=>d.device_id)).toEqual(['cam0']);
 expect(deviceOptions(capabilities,'camera_record').actions).not.toContain('camera_record');
});
it('取消单张拍摄可报告已保留图片',()=> expect(()=>validateReport(JSON.parse(JSON.stringify(report())))).not.toThrow());
it('成功拍摄必须已完成采集',()=>{
 const r=report('succeeded') as StatusReport;
 (r.plans![0].actions![0].result as any).capture.status='running';
 expect(()=>validateReport(JSON.parse(JSON.stringify(r)))).toThrow();
});
it('新拍摄的启动尝试编号必须连续',()=>{
 const r=report('succeeded') as StatusReport;
 (r.plans![0].actions![0].result as any).start={max_attempts:2,attempts:[{attempt_no:2,status:'succeeded'}]};
 expect(()=>validateReport(JSON.parse(JSON.stringify(r)))).toThrow();
});
it('新拍摄终态的采集结果不可改写',()=>{
 const before=JSON.parse(JSON.stringify(report())) as StatusReport;
 const after=structuredClone(before);after.report_id=2;after.from_wm=10;after.to_wm=20;
 (after.plans![0].actions![0].result as any).capture.captured_count=2;
 expect(()=>validateReportAgainstHistory(before,after)).toThrow();
});
