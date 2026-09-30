import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

// 解释登记中的纯取值与条件，用独立事实验证映射；不模拟 SQL 或历史恢复。
const root = new URL('../../', import.meta.url);
const registry = JSON.parse(readFileSync(new URL('docs/camctl/database/report-dependencies.json', root), 'utf8'));
const enums = JSON.parse(readFileSync(new URL('docs/camctl/database/enum-registry.json', root), 'utf8'));
const omitted = Symbol('omitted');
function evaluate(node, rows) {
  const column = path => {
    const [table, key] = path.split('.');
    assert(Object.hasOwn(rows, table), `场景缺少表：${table}`);
    if (rows[table] === null) return null;
    assert(Object.hasOwn(rows[table], key), `场景缺少列：${path}`);
    return rows[table][key];
  };
  const jsonMember = () => {
    const value = JSON.parse(column(node.column));
    assert.equal(node.pointer.split('/').length, 2);
    return [value, node.pointer.slice(1)];
  };
  switch (node.op) {
    case 'literal': return node.value;
    case 'omit': return omitted;
    case 'read': {
      const value = column(node.column);
      if (node.encoding === 'id') return String(value);
      if (node.encoding === 'json') return JSON.parse(value);
      assert.equal(node.encoding, 'identity'); return value;
    }
    case 'not_null': return column(node.column) !== null;
    case 'exists': assert(Object.hasOwn(rows, node.table)); return rows[node.table] !== null;
    case 'enum_is': return node.members.some(member => enums.enums[node.column].members[member] === column(node.column));
    case 'enum': {
      const member = Object.entries(enums.enums[node.column].members).find(([, value]) => value === column(node.column))?.[0];
      assert(Object.hasOwn(node.map, member)); return node.map[member];
    }
    case 'all': return node.args.every(arg => evaluate(arg, rows));
    case 'any': return node.args.some(arg => evaluate(arg, rows));
    case 'eq': return evaluate(node.left, rows) === evaluate(node.right, rows);
    case 'not': return !evaluate(node.arg, rows);
    case 'json_has': { const [value, key] = jsonMember(); return Object.hasOwn(value, key); }
    case 'json_member': { const [value, key] = jsonMember(); assert(Object.hasOwn(value, key)); return value[key]; }
    case 'cases': return evaluate(node.branches.find(branch => evaluate(branch.when, rows))?.value ?? node.otherwise, rows);
    case 'project': return node.projection;
    case 'error': throw new Error(node.code);
    default: throw new Error(`场景解释器不处理：${node.op}`);
  }
}
function field(projectionName, fieldName, rows) {
  const projection = registry.projections[projectionName];
  if (!evaluate(projection.when, rows)) return omitted;
  const definition = projection.fields[fieldName];
  return evaluate(definition.when, rows) ? evaluate(definition.value, rows) : omitted;
}
const hintRows = (action, activity) => ({
  actions: {type:2,status:4,...action},
  device_activities: activity === null ? null : {dispatch_state:3,activity_state:1,completion_basis:null,...activity}
});

for (const [name, action, activity, expected] of [
  ['运行中的动作', {status:2}, {}, omitted],
  ['未建立活动', {}, null, omitted],
  ['可靠确认未派发', {}, {dispatch_state:1}, omitted],
  ['可靠确认无启动效果', {}, {dispatch_state:4}, omitted],
  ['仍在执行', {}, {activity_state:2}, 'still_running'],
  ['可能已派发且结束未知', {}, {dispatch_state:2}, 'end_unconfirmed'],
  ['已经确认结束', {}, {activity_state:3}, omitted],
  ['单张拍摄已有适用完成依据', {type:1}, {completion_basis:3}, omitted],
]) test(`设备执行提示：${name}`, () => assert.equal(field('device_execution','status',hintRows(action,activity)), expected));

test('提示清除时原动作失败状态保持', () => {
  const rows = hintRows({}, {activity_state:3});
  assert.equal(field('device_execution','status',rows), omitted);
  assert.equal(field('action','status',rows), 'failed');
});
for (const [name, input, expected] of [
  ['省略', '{}', omitted], ['显式 null', '{"device_id":null}', null],
  ['错误类型', '{"device_id":42}', 42]
]) test(`原始输入：${name}`, () => assert.equal(field('action','device_id',{
  actions:{device_id:null,input_fields_json:input}
}), expected));
test('合法设备字段从原普通列读取', () => assert.equal(field('action','device_id',{
  actions:{device_id:'cam0',input_fields_json:'{}'}
}), 'cam0'));
test('内部读取重试不会形成新的取回最终失败项', () => {
  const p = registry.projections.delivery_failure;
  assert.equal(evaluate(p.when,{deliveries:{status:2}}),false);
  assert.equal(evaluate(p.when,{deliveries:{status:6}}),true);
  assert.equal(p.entity,'delivery');
});
test('交付长度使用该次拷贝依据，源文件后续变化不替换它', () => {
  const rows={file_copies:{source_size:100},device_files:{size_bytes:200},intermediate_files:{size_bytes:null}};
  assert.equal(field('delivery','size',rows),100);
});
test('未取得产物摘要时不填空摘要', () => {
  const rows={outputs:{device_file_id:1,intermediate_file_id:null},device_files:{sha256:null},intermediate_files:null};
  assert.equal(field('checksum','status',rows),'not_obtained');
  assert.equal(field('checksum','sha256',rows),omitted);
});
for (const [type, started, status, expected] of [
  [1,1,3,omitted], [3,1,3,omitted], [4,0,6,omitted], [4,1,6,'obtain_result'],
  [5,1,2,'delete_result'], [6,1,2,'cancel_result'], [7,1,2,omitted], [7,1,3,'report_result'], [2,1,2,'camera_result']
]) test(`动作结果分支：type=${type}, started=${started}, status=${status}`, () => assert.equal(
  field('action','result',{actions:{type,execution_started:started,status}}),expected
));
