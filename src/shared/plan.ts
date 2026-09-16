import { ACTION_TYPES, isCameraAction } from './actions';
export { ACTION_TYPES } from './actions';
import type { ActionType, Capabilities, Issue, ValidationContext } from './types';
import { validateParams } from './capabilities';
import { validateBuiltinParams, isSyncBasis } from './action-params';
import { isId, isName, isObject, isPositive, isTimestamp, isUint } from './validation';

export function validatePlan(plan: unknown, capabilities: Capabilities | null, context: ValidationContext = {}): Issue[] {
  const issues: Issue[] = [];
  const issue = (path: string, code: string, message: string) => { issues.push({ path, code, message }); };
  const check = (condition: boolean, path: string, message: string, code = 'invalid_value') => { if (!condition) issue(path, code, message); };
  const fields = (value: Record<string, unknown>, allowed: string[], path: string) => { for (const key of Object.keys(value)) check(allowed.includes(key), path ? `${path}.${key}` : key, '不接受此字段', 'unknown_field'); };
  if (!isObject(plan)) return [{ path: '', code: 'invalid_plan', message: '计划必须是对象' }];
  fields(plan, ['request_id', 'created_at', 'name', 'actions', 'last_report_id'], '');
  check(isId(plan.request_id), 'request_id', '请求 ID 不合法');
  check(isName(plan.name), 'name', '计划名称不合法');
  check(isTimestamp(plan.created_at), 'created_at', '创建时间必须是真实 UTC 时间');
  if (Object.hasOwn(plan, 'last_report_id')) check(isPositive(plan.last_report_id), 'last_report_id', '报告编号必须是正安全整数');
  if (!Array.isArray(plan.actions) || !plan.actions.length) { issue('actions', 'invalid_actions', '至少需要一个动作'); return issues; }
  const actions = plan.actions;
  const names = new Set<string>();
  actions.forEach((action, index) => {
    const path = `actions[${index}]`;
    if (!isObject(action)) { issue(path, 'invalid_action', '动作必须是对象'); return; }
    fields(action, ['name', 'type', 'device_id', 'scheduled_at', 'group', 'params', 'policy'], path);
    check(isName(action.name), `${path}.name`, '动作名称不合法');
    if (typeof action.name === 'string') { check(!names.has(action.name), `${path}.name`, '动作名称重复', 'duplicate_name'); names.add(action.name); }
    if (!ACTION_TYPES.includes(action.type as ActionType)) { issue(`${path}.type`, 'unsupported_action', '本版不支持此动作类型'); return; }
    const type = action.type as ActionType;
    const requiresTime = isCameraAction(type) || ['obtain_action_outputs', 'delete_action_outputs'].includes(type);
    if (requiresTime || Object.hasOwn(action, 'scheduled_at')) check(isTimestamp(action.scheduled_at), `${path}.scheduled_at`, '执行时间必须是真实 UTC 时间');
    if (Object.hasOwn(action, 'group')) check(type !== 'obtain_action_outputs' && isName(action.group), `${path}.group`, '动作所属组不合法');
    if (isCameraAction(type)) {
      check(isId(action.device_id), `${path}.device_id`, '设备 ID 不合法');
      if (!isObject(action.policy)) issue(`${path}.policy`, 'invalid_policy', '拍摄必须提供策略');
      else { fields(action.policy, ['max_delay_ms'], `${path}.policy`); check(isUint(action.policy.max_delay_ms), `${path}.policy.max_delay_ms`, '最大延迟必须是非负安全整数'); }
      issues.push(...validateParams(typeof action.device_id === 'string' ? action.device_id : '', type, action.params, capabilities).map(i => ({ ...i, path: `${path}.${i.path}` })));
      return;
    }
    check(!Object.hasOwn(action, 'device_id'), `${path}.device_id`, '此动作必须省略设备字段');
    if (Object.hasOwn(action, 'policy')) check(isObject(action.policy) && !Object.keys(action.policy).length, `${path}.policy`, '此动作仅接受空策略对象');
    const parameterIssues = validateBuiltinParams(type, action.params, Object.hasOwn(action, 'params'));
    issues.push(...parameterIssues.map(i => ({ ...i, path: `${path}.${i.path}` })));
    if (parameterIssues.length || !isObject(action.params)) return;
    const params = action.params;
    const p = `${path}.params`;
    if (type === 'report_status') {
      if (params.scope === 'since') check(!!context.reports?.some(r => r.report_id === params.after_report_id && isSyncBasis(r, context.coverage)), `${p}.after_report_id`, '同步起点没有已保存且完整覆盖的报告依据', 'sync_basis_unavailable');
    } else if (type === 'obtain_action_outputs') {
      if (isObject(params.source)) {
        const source = params.source;
        if (Object.hasOwn(source, 'action_name')) check(actions.some(a => isObject(a) && a.name === source.action_name && isCameraAction(a.type)), `${p}.source.action_name`, '本计划内没有该名称的产物来源', 'source_not_found');
        if (Object.hasOwn(source, 'group') && !Object.hasOwn(source, 'plan_instance_id')) check(actions.some(a => isObject(a) && a.group === source.group && isName(a.group) && isCameraAction(a.type)), `${p}.source.group`, '本计划组中没有产物来源', 'source_not_found');
      }
    }
  });
  return issues;
}
