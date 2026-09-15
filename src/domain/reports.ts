import { createHash } from 'node:crypto';
import { isDeepStrictEqual } from 'node:util';
import schema from '../../docs/superpowers/specs/camctl/schemas/status-report.schema.json';
import { parseJson } from '../shared/json';
import { validateBuiltinParams } from '../shared/action-params';
import { createValidator, isId, isName, isObject, isPositive, isTimestamp, isUint, schemaIssues } from '../shared/validation';
import type { StatusReport, ReportAction, ReportPlan, Output, Delivery } from '../shared/types';
import type { Attempt, Copy, CameraResult, ObtainResult, DeleteResult, CancelResult } from '../shared/status-report.generated';

const validate = createValidator().compile<StatusReport>(schema);
const terminal = (status: ReportAction['status']) => status !== 'pending' && status !== 'running';
function requireFact(condition: unknown, message: string): asserts condition { if (!condition) throw new Error(message); }
function attempts(values: Attempt[], maximum: number, path: string) {
  requireFact(values.length <= maximum, `${path} 尝试次数超过上限`);
  values.forEach((a, i) => requireFact(a.attempt_no === i + 1, `${path} 尝试编号不连续`));
  requireFact(values.filter(a => a.status === 'running').length <= 1, `${path} 同一流程有多个运行尝试`);
  const running = values.findIndex(a => a.status === 'running');
  requireFact(running === -1 || running === values.length - 1, `${path} 运行中的尝试必须是最新尝试`);
}
function copyFacts(copy: Copy, path: string) {
  attempts(copy.read_attempts, copy.max_read_attempts, path);
  requireFact(copy.recopies_used <= copy.max_recopies && copy.round === copy.recopies_used + 1, `${path} 重拷轮次与预算不一致`);
  requireFact(copy.source_size === undefined || copy.committed_bytes <= copy.source_size, `${path} 拷贝进度超过源长度`);
}
interface Index {
  plans: Map<string, ReportPlan>;
  actions: Map<string, { parent: string; value: ReportAction }>;
  outputs: Map<string, { parent: string; value: Output }>;
  deliveries: Map<string, { parent: string; value: Delivery }>;
}
function indexReport(report: StatusReport): Index {
  const index: Index = { plans: new Map(), actions: new Map(), outputs: new Map(), deliveries: new Map() };
  const requests = new Set<string>(); const sequences = new Set<number>(); const files = new Set<string>();
  for (const plan of report.plans ?? []) {
    requireFact(!index.plans.has(plan.plan_instance_id) && !requests.has(plan.request_id) && !sequences.has(plan.plan_seq), '计划身份、请求关联或受理序号重复');
    index.plans.set(plan.plan_instance_id, plan); requests.add(plan.request_id); sequences.add(plan.plan_seq);
    const names = new Set<string>();
    for (const action of plan.actions ?? []) {
      requireFact(!index.actions.has(action.action_instance_id) && !names.has(action.name), '动作身份或计划内名称重复');
      index.actions.set(action.action_instance_id, { parent: plan.plan_instance_id, value: action }); names.add(action.name);
      for (const output of action.outputs ?? []) {
        requireFact(!index.outputs.has(output.output_id), '产物身份重复');
        requireFact(output.source_action_instance_id === action.action_instance_id, '产物来源与父动作不一致');
        index.outputs.set(output.output_id, { parent: action.action_instance_id, value: output });
      }
      for (const delivery of action.deliveries ?? []) {
        requireFact(!index.deliveries.has(delivery.delivery_id) && !files.has(delivery.file_name), '交付身份或文件名重复');
        index.deliveries.set(delivery.delivery_id, { parent: action.action_instance_id, value: delivery }); files.add(delivery.file_name);
      }
    }
  }
  const diagnostics = new Set<string>();
  for (const d of report.plan_file_diagnostics ?? []) { requireFact(!diagnostics.has(d.diagnostic_id), '诊断身份重复'); diagnostics.add(d.diagnostic_id); }
  return index;
}
function associations(index: Index) {
  for (const { value: output } of index.outputs.values()) {
    if (output.derived_from_output_id !== undefined) {
      requireFact(output.derived_from_output_id !== output.output_id, '产物不能由自身派生');
      const source = index.outputs.get(output.derived_from_output_id)?.value;
      if (source) requireFact(source.kind === 'original' && source.source_action_instance_id === output.source_action_instance_id, '修复产物必须派生自同一动作的原片');
    }
  }
  for (const { value: delivery } of index.deliveries.values()) {
    const source = index.actions.get(delivery.source_action_instance_id)?.value;
    if (source) requireFact(source.type === 'camera_record', '交付来源动作不产生产物');
    const output = index.outputs.get(delivery.output_id)?.value;
    if (output) {
      requireFact(output.source_action_instance_id === delivery.source_action_instance_id, '交付与产物来源不一致');
      if (delivery.size !== undefined && output.size !== undefined) requireFact(delivery.size === output.size, '交付长度与正式产物不一致');
      if (delivery.sha256 !== undefined && output.checksum.status === 'available') requireFact(delivery.sha256 === output.checksum.sha256, '交付摘要与正式产物不一致');
    }
  }
  for (const { value: action } of index.actions.values()) {
    if (action.type === 'obtain_action_outputs' && action.result) {
      const seen = new Set<string>();
      for (const failure of (action.result as unknown as ObtainResult).failures) {
        const identity = `${failure.source_action_instance_id}/${failure.output_id ?? ''}/${failure.delivery_id ?? ''}`;
        requireFact(!seen.has(identity), '取回失败项重复'); seen.add(identity);
        const output = failure.output_id ? index.outputs.get(failure.output_id)?.value : undefined;
        if (output) requireFact(output.source_action_instance_id === failure.source_action_instance_id, '失败项的产物来源不一致');
        const delivery = failure.delivery_id ? index.deliveries.get(failure.delivery_id) : undefined;
        if (delivery) requireFact(delivery.parent === action.action_instance_id && delivery.value.output_id === failure.output_id && delivery.value.source_action_instance_id === failure.source_action_instance_id, '失败项交付关联不一致');
      }
    }
    if (action.type === 'cancel_task' && action.result) for (const item of (action.result as unknown as CancelResult).items) for (const withdrawal of item.withdrawals ?? []) {
      const delivery = index.deliveries.get(withdrawal.delivery_id);
      if (delivery) requireFact(delivery.parent === item.action_instance_id, '撤回交付不属于取消目标动作');
    }
  }
}
function ownFacts(report: StatusReport) {
  requireFact(report.from_wm <= report.to_wm, '报告水位顺序不合法');
  const index = indexReport(report);
  for (const plan of report.plans ?? []) {
    requireFact(isName(plan.name) && isTimestamp(plan.created_at), '计划名称或真实日期不合法');
    for (const action of plan.actions ?? []) {
      requireFact(isName(action.name), '动作名称不合法');
      const admission = action.status === 'failed' && !action.execution.started && action.error?.stage === 'admission';
      if (!admission) {
        if (action.scheduled_at !== undefined) requireFact(isTimestamp(action.scheduled_at), '动作真实执行日期不合法');
        if (action.group !== undefined) requireFact(isName(action.group), '动作组名称不合法');
        if (action.type === 'camera_record') {
          requireFact(isObject(action.input_params) && typeof action.input_params.type === 'string' && action.input_params.type.length > 0 && action.input_params.type === action.effective_params?.type, '录像原参数与生效类型不一致');
          requireFact(isId(action.device_id) && isObject(action.policy) && isUint(action.policy.max_delay_ms) && Object.keys(action.policy).length === 1, '录像公共输入不合法');
        } else {
          requireFact(validateBuiltinParams(action.type, action.input_params, Object.hasOwn(action, 'input_params')).length === 0, '动作原始参数不符合已受理契约');
        }
      }
      if (!action.execution.started) requireFact(action.result === undefined && !(action.outputs?.length) && !(action.deliveries?.length), '未执行动作不能携带执行结果或产物交付');
      if (plan.status === 'completed') requireFact(terminal(action.status), '已完成计划包含未终态动作');
      if (plan.status === 'pending') requireFact(!action.execution.started, '未执行计划包含已开始动作');
      if (action.expiration_reason === 'window_missed') requireFact(!action.execution.started, '错过启动窗口不应已有执行事实');
      if (action.expiration_reason === 'window_exhausted') requireFact(action.execution.started, '启动窗口耗尽须已有执行事实');
      if (action.type === 'camera_record' && action.result) {
        const result = action.result as CameraResult;
        if (result.recording) {
          const r = result.recording; attempts(r.start.attempts, r.start.max_attempts, '录像启动'); attempts(r.stop.attempts, r.stop.max_attempts, '录像停止');
          for (const stop of r.followup_stops ?? []) attempts(stop.attempts, stop.max_attempts, '后续停止');
          for (const stop of r.emergency_stops ?? []) if (stop.max_attempts !== undefined) requireFact(stop.attempts_used <= stop.max_attempts, '应急停止超过预算');
          for (const collection of [r.followup_stops, r.emergency_stops]) if (collection) requireFact(new Set(collection.map(s => s.flow_id)).size === collection.length, '停止流程身份重复');
        }
        if (result.source_copy) copyFacts(result.source_copy, '原片工作副本');
        if (action.status === 'succeeded' && result.repair) requireFact(!['undetermined', 'pending', 'running'].includes(result.repair.status), '录像成功时修复仍未结束');
      }
      if (action.type === 'delete_action_outputs' && action.result) {
        const items = (action.result as unknown as DeleteResult).items;
        requireFact(isObject(action.input_params) && Array.isArray(action.input_params.output_ids), '清理输入缺少目标');
        const ids = action.input_params.output_ids;
        requireFact(items.length <= ids.length && items.every((item, i) => item.output_id === ids[i]), '清理逐项结果顺序与输入不一致');
        if (action.status === 'succeeded') requireFact(items.length === ids.length && items.length > 0 && items.every(i => i.status === 'succeeded'), '清理成功缺少全部目标的成功事实');
      }
      if (action.type === 'cancel_task' && action.result) {
        const items = (action.result as unknown as CancelResult).items;
        requireFact(new Set(items.map(i => i.action_instance_id)).size === items.length, '取消结果目标重复');
        if (action.status === 'succeeded') requireFact(items.length > 0 && items.every(i => i.status === 'succeeded' && (i.withdrawals ?? []).every(w => w.status === 'withdrawn' || w.status === 'not_retractable')), '取消成功但有限处理未全部完成');
        for (const item of items) requireFact(new Set((item.withdrawals ?? []).map(w => w.delivery_id)).size === (item.withdrawals ?? []).length, '撤回结果重复');
      }
      if (action.type === 'obtain_action_outputs' && action.status === 'succeeded') requireFact((action.result as unknown as ObtainResult).failures.length === 0, '取回成功仍包含最终失败');
      for (const output of action.outputs ?? []) {
        const restricted = ['pending', 'running', 'incomplete'].includes(output.cleanup.status);
        requireFact((output.availability === 'restricted') === restricted, '删除限制与产物可用状态不一致');
      }
      for (const delivery of action.deliveries ?? []) {
        const suffix = delivery.file_name.slice(delivery.delivery_id.length + 1);
        requireFact(delivery.file_name.startsWith(`${delivery.delivery_id}.`) && /^[A-Za-z0-9]+$/.test(suffix), '交付文件名必须使用完整交付 ID 和安全扩展名');
        copyFacts(delivery.copy, '交付拷贝');
        if (['prepared', 'publishing', 'published', 'withdrawn'].includes(delivery.status)) {
          requireFact(delivery.copy.committed_bytes === delivery.size, '准备完成的字节进度不等于完整长度');
          if (delivery.copy.source_size !== undefined) requireFact(delivery.copy.source_size === delivery.size, '完整源长度与交付长度不一致');
        }
      }
    }
  }
  associations(index);
}
function validateReport(value: unknown): asserts value is StatusReport {
  if (!validate(value)) throw new Error(schemaIssues(validate.errors).map(e => e.message).join('；'));
  ownFacts(value);
}
export function parseReport(fileName: string, bytes: Uint8Array): StatusReport {
  const match = /^status-report-([1-9][0-9]*)-([0-9a-f]{64})\.json$/.exec(fileName);
  requireFact(match && !/[\r\n]/.test(fileName) && isPositive(Number(match[1])), '报告文件名不合法');
  requireFact(createHash('sha256').update(bytes).digest('hex') === match[2], '报告原字节摘要不一致');
  const text = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes);
  const value = parseJson(text);
  validateReport(value);
  requireFact(value.report_id === Number(match[1]), '报告文件名与正文身份不一致');
  return value;
}
function unchanged(old: object, next: object, keys: string[], label: string) {
  for (const key of keys) requireFact(Object.hasOwn(old, key) === Object.hasOwn(next, key) && isDeepStrictEqual(Reflect.get(old, key), Reflect.get(next, key)), `${label} 的不可变字段 ${key} 改变`);
}
function attemptHistory(old: Attempt[], next: Attempt[]) {
  requireFact(next.length >= old.length, '已登记尝试不能消失');
  old.forEach((before, i) => {
    requireFact(before.attempt_no === next[i].attempt_no, '尝试编号不可改变');
    if (before.status === 'succeeded' || before.status === 'failed') requireFact(isDeepStrictEqual(before, next[i]), '已确定尝试结果不可改变');
  });
}
function copyHistory(old: Copy, next: Copy) {
  unchanged(old, next, ['max_read_attempts', 'read_idle_timeout_s', 'max_recopies'], '拷贝预算');
  attemptHistory(old.read_attempts, next.read_attempts);
  requireFact(next.recopies_used >= old.recopies_used, '额外重拷计数不可回退');
  if (next.round === old.round) requireFact(next.committed_bytes >= old.committed_bytes, '同轮拷贝进度不可回退');
  if (old.source_size !== undefined) requireFact(next.source_size === old.source_size, '已知源长度不可改变');
}
function historicalIdentity(old: Index, next: Index, advancing: boolean) {
  for (const [id, plan] of next.plans) {
    const before = old.plans.get(id);
    if (before) {
      unchanged(before, plan, ['request_id', 'plan_seq', 'created_at', 'name'], '计划');
      if (advancing) requireFact(before.status !== 'completed' || plan.status === 'completed', '计划完成状态不能回退');
    }
    for (const existing of old.plans.values()) requireFact(existing.plan_instance_id === id || (existing.request_id !== plan.request_id && existing.plan_seq !== plan.plan_seq), '计划请求或受理序号关联冲突');
  }
  for (const [id, action] of next.actions) {
    const before = old.actions.get(id);
    if (!before) continue;
    requireFact(before.parent === action.parent, '动作所属计划改变');
    unchanged(before.value, action.value, ['name', 'type', 'device_id', 'scheduled_at', 'group', 'policy', 'input_params', 'extra_input_fields', 'effective_params'], '动作原始输入');
    if (advancing) {
      requireFact(!terminal(before.value.status) || before.value.status === action.value.status, '动作终态不可改变');
      requireFact(!before.value.execution.started || action.value.execution.started, '已开始执行的事实不可消失');
      if (before.value.type === 'camera_record') {
        const oldResult = before.value.result as CameraResult | undefined; const nextResult = action.value.result as CameraResult | undefined;
        if (oldResult?.recording) {
          requireFact(nextResult?.recording, '已登记录像流程不能消失');
          for (const key of ['start', 'stop'] as const) { requireFact(oldResult.recording[key].max_attempts === nextResult.recording[key].max_attempts, '录像尝试预算不可改变'); attemptHistory(oldResult.recording[key].attempts, nextResult.recording[key].attempts); }
        }
        if (oldResult?.source_copy) { requireFact(nextResult?.source_copy, '已登记原片拷贝不能消失'); copyHistory(oldResult.source_copy, nextResult.source_copy); }
      }
    }
  }
  for (const [id, output] of next.outputs) {
    const before = old.outputs.get(id);
    if (before) {
      requireFact(before.parent === output.parent, '产物所属动作改变'); unchanged(before.value, output.value, ['source_action_instance_id', 'kind', 'derived_from_output_id'], '产物');
      if (advancing) {
        for (const key of ['size', 'original_name', 'media_type'] as const) if (before.value[key] !== undefined) requireFact(before.value[key] === output.value[key], '已知产物事实不可改变');
        if (before.value.checksum.status === 'available') requireFact(isDeepStrictEqual(before.value.checksum, output.value.checksum), '已知产物摘要不可改变');
        if (before.value.cleanup.status === 'completed') requireFact(output.value.cleanup.status === 'completed', '已完成清理不可回退');
      }
    }
  }
  for (const [id, delivery] of next.deliveries) {
    const before = old.deliveries.get(id);
    for (const existing of old.deliveries.values()) requireFact(existing.value.delivery_id === id || existing.value.file_name !== delivery.value.file_name, '交付文件名被复用');
    if (before) {
      requireFact(before.parent === delivery.parent, '交付所属取回动作改变');
      unchanged(before.value, delivery.value, ['output_id', 'source_action_instance_id', 'file_name', 'display_name'], '交付');
      if (advancing) {
        copyHistory(before.value.copy, delivery.value.copy);
        const status = before.value.status;
        if (['failed', 'canceled', 'withdrawn'].includes(status)) requireFact(status === delivery.value.status, '已结束交付不能恢复');
        if (status === 'published') requireFact(['published', 'withdrawn'].includes(delivery.value.status), '已发布交付只能保留或撤回');
        for (const key of ['size', 'sha256'] as const) if (before.value[key] !== undefined) requireFact(isDeepStrictEqual(before.value[key], delivery.value[key]), '已确定的交付内容改变');
      }
    }
  }
}
function members<T>(old: T[] | undefined, next: T[] | undefined, id: (item: T) => string, merge: (before: T | undefined, value: T) => T): T[] | undefined {
  if (old === undefined && next === undefined) return undefined;
  const map = new Map((old ?? []).map(item => [id(item), item]));
  for (const value of next ?? []) map.set(id(value), merge(map.get(id(value)), value));
  return [...map.values()];
}
export function mergeReport(current: StatusReport, incoming: StatusReport): StatusReport {
  validateReport(incoming);
  const decision = reportDecision(current.to_wm, incoming.from_wm, incoming.to_wm);
  requireFact(decision !== 'gap', '报告覆盖存在缺口');
  historicalIdentity(indexReport(current), indexReport(incoming), decision === 'apply');
  for (const d of incoming.plan_file_diagnostics ?? []) {
    const before = current.plan_file_diagnostics?.find(v => v.diagnostic_id === d.diagnostic_id);
    if (before) requireFact(isDeepStrictEqual(before, d), '计划文件诊断不可改变');
  }
  if (decision === 'covered') return structuredClone(current);
  const combined: StatusReport = { ...incoming, from_wm: current.from_wm };
  const plans = members(current.plans, incoming.plans, p => p.plan_instance_id, (oldPlan, plan) => {
    const result = { ...plan };
    const actions = members(oldPlan?.actions, plan.actions, a => a.action_instance_id, (oldAction, action) => {
      const result = { ...action };
      const outputs = members(oldAction?.outputs, action.outputs, o => o.output_id, (_o, n) => n);
      const deliveries = members(oldAction?.deliveries, action.deliveries, d => d.delivery_id, (_o, n) => n);
      if (outputs !== undefined) result.outputs = outputs;
      if (deliveries !== undefined) result.deliveries = deliveries;
      return result;
    });
    if (actions !== undefined) result.actions = actions;
    return result;
  });
  if (plans !== undefined) combined.plans = plans;
  const diagnostics = members(current.plan_file_diagnostics, incoming.plan_file_diagnostics, d => d.diagnostic_id, (_o, n) => n);
  if (diagnostics !== undefined) combined.plan_file_diagnostics = diagnostics;
  associations(indexReport(combined));
  return structuredClone(combined);
}
export function reportDecision(coverage: number, from: number, to: number): 'covered' | 'apply' | 'gap' {
  requireFact(isUint(coverage) && isUint(from) && isUint(to) && from <= to, '报告覆盖水位不合法');
  return to <= coverage ? 'covered' : from <= coverage ? 'apply' : 'gap';
}
export function selectSyncReport(reports: Array<{ report_id: number; to_wm: number }>, coverage: number): number | null {
  requireFact(isUint(coverage), '本地完整覆盖水位不合法');
  let selected: { report_id: number; to_wm: number } | undefined;
  for (const report of reports) {
    requireFact(isPositive(report.report_id) && isUint(report.to_wm), '已保存报告的身份或水位不合法');
    if (report.to_wm <= coverage && (!selected || report.to_wm > selected.to_wm)) selected = report;
  }
  return selected?.report_id ?? null;
}
