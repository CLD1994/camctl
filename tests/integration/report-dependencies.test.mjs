import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';
import { test } from 'node:test';
import { validateRegistration } from '../../scripts/report-dependencies.mjs';

const root = new URL('../../', import.meta.url);
const json = path => JSON.parse(readFileSync(new URL(path, root), 'utf8'));
const registry = json('docs/camctl/database/report-dependencies.json');
const schema = json('protocol/schemas/status-report.schema.json');
const enums = json('docs/camctl/database/enum-registry.json');
const errors = json('protocol/errors/workflow-codes.json');
const db = new DatabaseSync(':memory:');
for (const file of readdirSync(new URL('docs/camctl/database/schema/', root)).sort()) {
  if (file.endsWith('.sql')) db.exec(readFileSync(new URL(`docs/camctl/database/schema/${file}`, root), 'utf8'));
}
const tables = Object.fromEntries(db.prepare("SELECT name FROM sqlite_schema WHERE type = 'table'").all()
  .map(({ name }) => [name, db.prepare(`PRAGMA table_info(${name})`).all().map(column => column.name)]));
const foreignKeys = Object.fromEntries(Object.keys(tables).flatMap(table => db.prepare(`PRAGMA foreign_key_list(${table})`).all()
  .map(key => [`${table}.${key.from}`, `${key.table}.${key.to}`])));
db.close();
const check = (value = registry, publicSchema = schema) => validateRegistration(value, publicSchema, { enums, errors, tables, foreignKeys });
const rejected = mutate => { const value = structuredClone(registry); mutate(value); assert.throws(() => check(value)); };

test('正式登记覆盖公开字段，来源列均已落实到 SQL', () => {
  const result = check();
  assert(result.columns.includes('device_activities.activity_state'));
  assert(result.columns.includes('actions.execution_started'));
  assert(!result.columns.includes('operation_attempts.attempt_no'));
  assert.deepEqual(result.pendingSql, []);
});
test('遗漏已有公开字段被拒绝', () => rejected(value => { delete value.projections.action.fields.status; }));
test('公共 Schema 增加字段后要求补齐登记', () => {
  const changed = structuredClone(schema); changed.$defs.delivery.properties.receipt = { type: 'string' };
  assert.throws(() => check(registry, changed));
});
test('登记引用已移除字段被拒绝', () => rejected(value => { value.projections.action.fields.waiting = { when: { op:'literal', value:true }, value:{ op:'literal',value:{} } }; }));
test('拼错事实来源列被拒绝', () => rejected(value => { value.projections.plan.fields.name.value.column = 'plans.nmae'; }));
test('缺少关联路径被拒绝', () => rejected(value => { value.projections.delivery.relations = []; }));
test('未知内部枚举成员被拒绝', () => rejected(value => { value.projections.action.fields.status.value.map.FINISHED = 'succeeded'; }));
test('无效公共枚举值被拒绝', () => rejected(value => { value.projections.action.fields.status.value.map.SUCCEEDED = 'complete'; }));
test('未知条件读取也被拒绝', () => rejected(value => { value.projections.automation.when.table = 'automatic_links'; }));
test('内部文件不能成为报告目标', () => rejected(value => { value.entities.device_file = {table:'device_files',identity:'id',target:true}; }));
test('字段到自身投影的递归被拒绝', () => rejected(value => { value.projections.action.fields.result.value = {op:'project',projection:'action'}; }));
test('错误编号必须引用既有权威登记', () => rejected(value => { value.projections.item_failure.fields.error.value.registry_key = 'item_error_ids.nonexistent'; }));
test('SQL 待同步名单不能掩盖任意列拼写错误', () => rejected(value => { value.sql_pending.plans = {columns:['nmae'], specification:'plans-actions.md'}; }));
test('修复与废弃清理投影不能互换', () => rejected(value => {
  const fields = value.projections.camera_result.fields;
  [fields.repair.value, fields.discard_cleanup.value] = [fields.discard_cleanup.value, fields.repair.value];
}));
test('保存的媒体 JSON 不能按错误结构输出', () => rejected(value => { value.projections.output.fields.media.value.schema = '#/$defs/error'; }));
for (const [projection, field] of [['output', 'media'], ['delivery', 'error'], ['action', 'effective_params'], ['diagnostic', 'errors']]) {
  test(`${projection}.${field} 的结构化值必须解码 JSON`, () => rejected(value => {
    value.projections[projection].fields[field].value.encoding = 'identity';
  }));
}
test('删除结构引用不能绕过 JSON 解码要求', () => rejected(value => {
  const read = value.projections.output.fields.media.value;
  read.encoding = 'identity'; delete read.schema;
}));
test('父对象关联列必须存在', () => rejected(value => { value.entities.delivery.parent.foreign_key = 'actoin_id'; }));
test('交付必须登记取回父对象', () => rejected(value => { delete value.entities.delivery.parent; }));
test('关联不能用同表其他列代替外键', () => rejected(value => { value.relations.action_deliveries.to = 'deliveries.output_id'; }));
test('错误整数编号不能套用另一种明细的登记', () => rejected(value => { value.projections.item_failure.fields.error.value.registry_key = 'item_error_ids.cleanup_items'; }));
test('合法内部状态不能漏掉公开映射', () => rejected(value => { delete value.projections.action.fields.status.value.map.SUCCEEDED; }));
test('新公共枚举要求扩展映射', () => {
  const changed = structuredClone(schema); changed.$defs.action_status.enum.push('paused');
  assert.throws(() => check(registry, changed));
});
test('设备执行提示必须归原动作', () => rejected(value => { value.projections.device_execution.entity = 'plan'; }));
test('历史对象登记的报告资格须与公共实体一致', () => {
  const changed = structuredClone(enums); changed.history_objects.device_file.report_target = true;
  assert.throws(() => validateRegistration(registry, schema, { enums: changed, errors, tables, foreignKeys }));
});
test('报告实体须引用历史对象登记的同一主表', () => {
  const changed = structuredClone(enums); changed.history_objects.delivery.table = 'actions';
  assert.throws(() => validateRegistration(registry, schema, { enums: changed, errors, tables, foreignKeys }));
});
