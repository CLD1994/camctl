import type { ActionType, Issue } from './types';
import { isId, isName, isObject, isPositive } from './validation';

/** 非拍摄动作的静态参数契约；引用存在性由调用方已有完整资料判断。 */
export function validateBuiltinParams(type: Exclude<ActionType, 'camera_record'>, params: unknown, present: boolean): Issue[] {
  const issues: Issue[] = [];
  const check = (ok: boolean, path: string, message: string) => { if (!ok) issues.push({ path, code: 'invalid_params', message }); };
  const fields = (value: Record<string, unknown>, allowed: string[], path: string) => { for (const key of Object.keys(value)) check(allowed.includes(key), `${path}.${key}`, '不接受此字段'); };
  const ids = (value: unknown) => {
    check(Array.isArray(value) && value.length > 0, 'params.output_ids', '必须填写非空产物 ID 数组');
    if (Array.isArray(value)) { const seen = new Set<unknown>(); value.forEach((id, i) => { check(isId(id) && !seen.has(id), `params.output_ids[${i}]`, '产物 ID 必须合法且不重复'); seen.add(id); }); }
  };
  const reference = (value: unknown, combinations: string[][], path: string) => {
    if (!isObject(value)) { check(false, path, '引用必须是对象'); return; }
    const keys = Object.keys(value);
    check(combinations.some(combo => combo.length === keys.length && combo.every(k => keys.includes(k))), path, '引用字段组合不合法');
    for (const [key, v] of Object.entries(value)) check(key === 'group' || key === 'action_name' ? isName(v) : isId(v), `${path}.${key}`, '引用值不合法');
  };
  if (type === 'report_status' && !present) return issues;
  if (!isObject(params)) return [{ path: 'params', code: 'invalid_params', message: '参数必须是对象' }];
  switch (type) {
    case 'report_status': {
      const keys = Object.keys(params);
      check(!keys.length || (keys.length === 1 && params.scope === 'full') || (keys.length === 2 && keys.includes('scope') && keys.includes('after_report_id') && params.scope === 'since' && isPositive(params.after_report_id)), 'params', '报告范围参数组合不合法');
      break;
    }
    case 'delete_action_outputs': fields(params, ['output_ids'], 'params'); ids(params.output_ids); break;
    case 'cancel_task': fields(params, ['target'], 'params'); reference(params.target, [['request_id'], ['plan_instance_id'], ['action_instance_id'], ['plan_instance_id', 'group']], 'params.target'); break;
    case 'obtain_action_outputs':
      fields(params, ['source', 'output_ids'], 'params');
      reference(params.source, [['action_instance_id'], ['plan_instance_id', 'group'], ['action_name'], ['group']], 'params.source');
      if (Object.hasOwn(params, 'output_ids')) { ids(params.output_ids); check(!isObject(params.source) || !Object.hasOwn(params.source, 'group'), 'params.output_ids', '产物筛选只适用于动作级引用'); }
      break;
  }
  return issues;
}
