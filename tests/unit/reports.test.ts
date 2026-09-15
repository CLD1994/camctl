import { createHash } from 'node:crypto';
import { describe, it, expect } from 'vitest';
import { parseReport, mergeReport, reportDecision, selectSyncReport } from '../../src/domain/reports';
import type { StatusReport, ReportAction, Output, Delivery } from '../../src/shared/types';

const error = { code: 'future_code', stage: 'execution', details: {} };
const output = (id = 'o1'): Output => ({ output_id: id, source_action_instance_id: 'a1', kind: 'original', availability: 'available', cleanup: { status: 'not_requested' }, checksum: { status: 'not_obtained' }, media: { check_status: 'not_performed', duration: { status: 'unknown' } } });
const camera = (): ReportAction => ({ action_instance_id: 'a1', name: '录像', type: 'camera_record', device_id: 'cam0', scheduled_at: '2026-01-01 00:00:00', input_params: { type: 'historical' }, effective_params: { type: 'historical' }, policy: { max_delay_ms: 0 }, status: 'succeeded', execution: { started: true }, outputs: [output()] });
const delivery = (): Delivery => ({ delivery_id: 'd1', output_id: 'o1', source_action_instance_id: 'a1', file_name: 'd1.mp4', display_name: '录像.mp4', status: 'published', size: 100, sha256: 'a'.repeat(64), copy: { max_read_attempts: 3, read_idle_timeout_s: 10, max_recopies: 1, recopies_used: 0, round: 1, committed_bytes: 100, source_size: 100, read_attempts: [{ attempt_no: 1, status: 'succeeded' }], verification: { status: 'source_checksum_unavailable' }, work_file_cleanup: { status: 'not_needed' } } });
const obtain = (): ReportAction => ({ action_instance_id: 'a2', name: '取回', type: 'obtain_action_outputs', scheduled_at: '2026-01-01 00:00:00', input_params: { source: { action_instance_id: 'a1' } }, status: 'succeeded', execution: { started: true }, result: { failures: [] }, deliveries: [delivery()] });
const report = (): StatusReport => ({ report_id: 1, from_wm: 0, to_wm: 20, plans: [{ plan_instance_id: 'p1', request_id: 'r1', plan_seq: 1, created_at: '2026-01-01 00:00:00', name: '计划', status: 'completed', actions: [camera(), obtain()] }] });
function parse(value: unknown, text = JSON.stringify(value)) { const bytes = new TextEncoder().encode(text); return parseReport(`status-report-1-${createHash('sha256').update(bytes).digest('hex')}.json`, bytes); }
describe('parseReport', () => {
  it('接受完整报告且不依赖当前能力说明', () => expect(parse(report())).toEqual(report()));
  it('接受只包含诊断的报告及未知错误码', () => expect(parse({ report_id: 1, from_wm: 0, to_wm: 1, plan_file_diagnostics: [{ diagnostic_id: 'x', file_name: '输入.json', errors: [{ ...error, stage: 'admission' }] }] }).plan_file_diagnostics?.length).toBe(1));
  it('先校验原始字节摘要', () => expect(() => parseReport(`status-report-1-${'0'.repeat(64)}.json`, new TextEncoder().encode('{}'))).toThrow());
  it('接受空白不同但摘要对应原文的报告', () => expect(parse({ report_id: 1, from_wm: 0, to_wm: 0 }, ' { "report_id": 1, "from_wm":0, "to_wm":0 }\n').report_id).toBe(1));
  it('拒绝文件名身份与根不一致', () => expect(() => parse({ report_id: 2, from_wm: 0, to_wm: 1 })).toThrow());
  it('拒绝重复转义成员', () => expect(() => parse({}, '{"report_id":1,"from_wm":0,"to_wm":1,"\\u0074o_wm":2}')).toThrow());
  it('拒绝逆序水位', () => expect(() => parse({ report_id: 1, from_wm: 2, to_wm: 1 })).toThrow());
  it('拒绝不存在的日期', () => { const r = report(); r.plans![0].created_at = '2026-02-29 00:00:00'; expect(() => parse(r)).toThrow(); });
  it('拒绝名称首尾 Unicode 空白', () => { const r = report(); r.plans![0].name = '计划\u2003'; expect(() => parse(r)).toThrow(); });
  it('初始失败保留非法原始输入', () => { const r = report(); r.plans![0].actions = [{ action_instance_id: 'a1', name: '录像', type: 'camera_record', device_id: null, group: false, scheduled_at: 5, input_params: null, policy: null, extra_input_fields: { typo: true }, status: 'failed', execution: { started: false }, error: { ...error, stage: 'admission' } }]; expect(parse(r).plans![0].actions![0].input_params).toBeNull(); });
  it('拒绝跨计划重复动作身份', () => { const r = report(); r.plans!.push({ ...r.plans![0], plan_instance_id: 'p2', request_id: 'r2', plan_seq: 2 }); expect(() => parse(r)).toThrow(); });
  it('拒绝产物归属与父动作不符', () => { const r = report(); r.plans![0].actions![0].outputs![0].source_action_instance_id = 'a2'; expect(() => parse(r)).toThrow(); });
  it('拒绝交付的来源与已知产物不符', () => { const r = report(); r.plans![0].actions![1].deliveries![0].source_action_instance_id = 'a2'; expect(() => parse(r)).toThrow(); });
  it('来源尚未出现时保留交付引用', () => { const r = report(); r.plans![0].actions = [obtain()]; expect(parse(r).plans![0].actions![0].deliveries![0].output_id).toBe('o1'); });
  it('拒绝交付文件名截短身份', () => { const r = report(); r.plans![0].actions![1].deliveries![0].file_name = 'd.mp4'; expect(() => parse(r)).toThrow(); });
  it('拒绝已准备交付的进度不等于完整长度', () => { const r = report(); r.plans![0].actions![1].deliveries![0].copy.committed_bytes = 99; expect(() => parse(r)).toThrow(); });
  it('拒绝读取尝试不连续', () => { const r = report(); r.plans![0].actions![1].deliveries![0].copy.read_attempts[0].attempt_no = 2; expect(() => parse(r)).toThrow(); });
  it('拒绝读取尝试超过上限', () => { const r = report(); const c = r.plans![0].actions![1].deliveries![0].copy; c.max_read_attempts = 1; c.read_attempts.push({ attempt_no: 2, status: 'succeeded' }); expect(() => parse(r)).toThrow(); });
  it('拒绝拷贝轮次与重拷计数矛盾', () => { const r = report(); r.plans![0].actions![1].deliveries![0].copy.round = 2; expect(() => parse(r)).toThrow(); });
  it('拒绝未结束删除限制与可取回资格并存', () => { const r = report(); r.plans![0].actions![0].outputs![0].cleanup.status = 'running'; expect(() => parse(r)).toThrow(); });
  it('拒绝已知源摘要与交付摘要不符', () => { const r = report(); r.plans![0].actions![0].outputs![0].checksum = { status: 'available', sha256: 'b'.repeat(64) }; expect(() => parse(r)).toThrow(); });
  it('拒绝合法受理报告的非法取消目标组合', () => { const r = report(); r.plans![0].actions = [{ action_instance_id: 'c', name: '取消', type: 'cancel_task', status: 'pending', execution: { started: false }, input_params: { target: { group: '组' } } }]; r.plans![0].status = 'pending'; expect(() => parse(r)).toThrow(); });
  it('拒绝已开始取回但来源参数缺失', () => { const r = report(); r.plans![0].actions![1].input_params = {}; expect(() => parse(r)).toThrow(); });
  it('拒绝未执行却带有运行结果的取消动作', () => { const r = report(); r.plans![0].actions = [{ action_instance_id: 'c', name: '取消', type: 'cancel_task', status: 'canceled', execution: { started: false }, input_params: { target: { request_id: 'r2' } }, result: { items: [] } }]; expect(() => parse(r)).toThrow(); });
});
describe('reportDecision', () => {
  it.each([[20, 0, 20, 'covered'], [20, 10, 19, 'covered'], [20, 20, 40, 'apply'], [20, 10, 40, 'apply'], [20, 40, 45, 'gap'], [0, 0, 0, 'covered']] as const)('覆盖 C=%s F=%s T=%s', (c, f, t, result) => expect(reportDecision(c, f, t)).toBe(result));
  it('非法水位不能变为缺口或覆盖', () => expect(() => reportDecision(0, 2, 1)).toThrow());
});
describe('selectSyncReport', () => {
  it('按终点选择且排除未覆盖报告', () => expect(selectSyncReport([{ report_id: 100, to_wm: 10 }, { report_id: 2, to_wm: 20 }, { report_id: 3, to_wm: 40 }], 20)).toBe(2));
  it('无可靠基础返回完整同步选择', () => expect(selectSyncReport([], 0)).toBeNull());
});
describe('mergeReport', () => {
  it('替换自身字段同时保留未出现的子实体', () => { const r = report(); const next = report(); next.report_id = 2; next.from_wm = 20; next.to_wm = 30; next.plans![0].actions = [{ ...camera(), outputs: [] }]; expect(mergeReport(r, next).plans![0].actions).toHaveLength(2); expect(mergeReport(r, next).plans![0].actions![0].outputs).toHaveLength(1); });
  it('移除缺席的可选自身字段', () => { const r = report(); r.plans![0].status = 'running'; r.plans![0].actions = [{ action_instance_id: 'a3', name: '报告', type: 'report_status', status: 'running', execution: { started: true }, waiting: [{ code: 'report_publication', details: {} }] }]; const next = structuredClone(r); next.report_id = 2; next.from_wm = 20; next.to_wm = 30; next.plans![0].status = 'completed'; next.plans![0].actions![0] = { ...next.plans![0].actions![0], status: 'succeeded', result: { report_id: 1 } }; delete next.plans![0].actions![0].waiting; expect(mergeReport(r, next).plans![0].actions![0].waiting).toBeUndefined(); });
  it('拒绝跨快照改变请求关联', () => { const r = report(); const n = structuredClone(r); n.report_id = 2; n.to_wm = 30; n.plans![0].request_id = 'other'; expect(() => mergeReport(r, n)).toThrow(); });
  it('旧报告不覆盖当前终态', () => { const r = report(); const old = report(); old.to_wm = 10; old.plans![0].status = 'pending'; old.plans![0].actions = [{ ...camera(), status: 'pending', execution: { started: false } }]; delete old.plans![0].actions[0].outputs; expect(mergeReport(r, old)).toEqual(r); });
  it('旧报告仍不得改变身份关联', () => { const r = report(); const old = report(); old.to_wm = 10; old.plans![0].request_id = 'other'; expect(() => mergeReport(r, old)).toThrow(); });
  it('新报告不得改变既有动作终态', () => { const r = report(); const n = report(); n.report_id = 2; n.to_wm = 30; n.plans![0].actions![0].status = 'failed'; n.plans![0].actions![0].error = error; expect(() => mergeReport(r, n)).toThrow(); });
  it('缺口不能应用部分结果', () => { const r = report(); const n = report(); n.report_id = 2; n.from_wm = 30; n.to_wm = 40; expect(() => mergeReport(r, n)).toThrow(); expect(r.to_wm).toBe(20); });
  it('后续报告不能重置已用读取尝试', () => { const r = report(); const n = report(); n.report_id = 2; n.to_wm = 30; n.plans![0].actions![1].deliveries![0].copy.read_attempts = []; expect(() => mergeReport(r, n)).toThrow(); });
  it('后续报告不能改写固化读取预算', () => { const r = report(); const n = report(); n.report_id = 2; n.to_wm = 30; n.plans![0].actions![1].deliveries![0].copy.max_read_attempts = 5; expect(() => mergeReport(r, n)).toThrow(); });
  it('后续报告不能丢弃已经取得的源摘要', () => { const r = report(); r.plans![0].actions![0].outputs![0].checksum = { status: 'available', sha256: 'a'.repeat(64) }; const n = report(); n.report_id = 2; n.to_wm = 30; expect(() => mergeReport(r, n)).toThrow(); });
});
