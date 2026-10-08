import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFile, readdir } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { basename, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

// 规格文件检查，复用客户端已锁定的依赖，不是 camctl 的受理实现。
const root = fileURLToPath(new URL('../', import.meta.url));
const require = createRequire(join(root, 'apps/client/package.json'));
const Ajv = require('ajv/dist/2020').default;
const { parseTree, findNodeAtLocation } = require('jsonc-parser');
const ajv = new Ajv({ strict: false, allErrors: true, validateSchema: true });
const json = async path => JSON.parse(await readFile(path, 'utf8'));
const schemaDir = join(root, 'protocol/schemas');
const schemaFiles = (await readdir(schemaDir)).filter(f => f.endsWith('.json'));
for (const file of schemaFiles) ajv.addSchema(await json(join(schemaDir, file)), file);
for (const file of schemaFiles) ajv.getSchema(file);
const validate = (schema, value, label) => {
  const check = ajv.getSchema(schema) ?? ajv.compile({ $ref: schema });
  assert(check(value), `${label}: ${ajv.errorsText(check.errors, { separator: '\n' })}`);
};

// 原始 token 的精确十进制分类仅适配本项目的有界整数规则；JSON 语法由已有解析库处理。
function boundedInteger(token, minimum, maximum) {
  const match = /^(-?)(0|[1-9][0-9]*)(?:\.([0-9]+))?(?:[eE]([+-]?[0-9]+))?$/.exec(token);
  if (!match) return undefined;
  let digits = (match[2] + (match[3] ?? '')).replace(/^0+/, '');
  if (!digits) return 0;
  const shift = BigInt(match[4] ?? '0') - BigInt((match[3] ?? '').length);
  if (shift < 0n) {
    const removed = -shift;
    if (removed >= BigInt(digits.length)) return undefined;
    const count = Number(removed);
    if (!/^0+$/.test(digits.slice(-count))) return undefined;
    digits = digits.slice(0, -count);
  } else {
    const limit = Math.max(String(minimum).replace('-', '').length, String(maximum).length);
    if (BigInt(digits.length) + shift > BigInt(limit)) return undefined;
    digits += '0'.repeat(Number(shift));
  }
  const integer = BigInt((match[1] ? '-' : '') + digits);
  if (integer < BigInt(minimum) || integer > BigInt(maximum)) return undefined;
  return Number(integer); // 已精确证明处于 safe integer 范围之后才转换。
}

function scalarString(text) {
  for (const character of text) {
    const point = character.codePointAt(0);
    if (point >= 0xd800 && point <= 0xdfff) return false;
  }
  return true;
}

function unambiguousTree(node) {
  if (node.type === 'object') {
    const names = new Set();
    for (const property of node.children ?? []) {
      const name = property.children[0].value;
      if (names.has(name) || !scalarString(name)) return false;
      names.add(name);
    }
  }
  if (node.type === 'string' && !scalarString(node.value)) return false;
  return (node.children ?? []).every(unambiguousTree);
}

export function checkMotorFixture(kind, text) {
  const errors = [];
  const tree = parseTree(text, errors, { disallowComments: true, allowTrailingComma: false, allowEmptyContent: false });
  if (!tree || errors.length || !unambiguousTree(tree)) return { valid: false, admission: 'plan_rejected' };
  let value;
  try { value = JSON.parse(text); } catch { return { valid: false, admission: 'plan_rejected' }; }
  function normalize(path, low, high) {
    const node = findNodeAtLocation(tree, path);
    if (!node || node.type !== 'number') return false;
    const integer = boundedInteger(text.slice(node.offset, node.offset + node.length), low, high);
    if (integer === undefined) return false;
    let owner = value;
    for (const part of path.slice(0, -1)) owner = owner[part];
    owner[path.at(-1)] = integer;
    return true;
  }
  const motorNumbers = prefix => normalize([...prefix, 'params', 'position'], -2147483648, 2147483647)
    && normalize([...prefix, 'policy', 'max_delay_ms'], 0, Number.MAX_SAFE_INTEGER);
  if (kind === 'notification') {
    const exact = normalize(['params', 'position'], -2147483648, 2147483647);
    return { valid: Boolean(ajv.getSchema('host-notification.schema.json')(value)) && exact, value };
  }
  if (kind === 'plan') {
    const structure = ajv.compile({ $ref: 'plan.schema.json#/$defs/plan_structure' });
    if (!structure(value)) return { valid: false, admission: 'plan_rejected', value };
    const exact = value.actions.every((action, index) => action.type !== 'motor_control' || motorNumbers(['actions', index]));
    const valid = Boolean(ajv.getSchema('plan.schema.json')(value)) && exact;
    return { valid, admission: valid ? 'accepted' : 'action_failed', value };
  }
  if (kind === 'report') {
    const admissionFailure = value?.status === 'failed' && value?.error?.stage === 'admission';
    const exact = value?.type !== 'motor_control' || admissionFailure
      || (normalize(['input_params', 'position'], -2147483648, 2147483647)
        && normalize(['policy', 'max_delay_ms'], 0, Number.MAX_SAFE_INTEGER));
    return { valid: Boolean(ajv.compile({ $ref: 'status-report.schema.json#/$defs/action' })(value)) && exact, value };
  }
  throw new Error(`未知电机夹具类别: ${kind}`);
}

const registry = await json(join(root, 'protocol/errors/workflow-codes.json'));
ajv.addSchema(registry, 'workflow-codes.json');
const actionErrorIds = new Set();
for (const [code, rule] of Object.entries(registry.codes)) {
  if (rule.action_error_id === undefined) continue;
  assert(Number.isSafeInteger(rule.action_error_id) && rule.action_error_id > 0, `${code}: 动作错误编号必须为正整数`);
  assert(!actionErrorIds.has(rule.action_error_id), `${code}: 动作错误编号重复`);
  assert(['admission', 'device_start', 'device_stop', 'execution'].includes(rule.stage), `${code}: 动作错误阶段无效`);
  actionErrorIds.add(rule.action_error_id);
}
const errorChecks = new Map(Object.entries(registry.codes).map(([code, rule]) =>
  [code, { stage: rule.stage, check: ajv.compile(rule.details_schema) }]));
for (const [code, value, valid] of [
  ['recording_too_short', {}, false],
  ['recording_too_short', { activity_id: '1' }, true],
  ['device_binding_unavailable', { device_id: 'camera', expected_driver_id: 'driver', reason: 'mismatch' }, false],
  ['device_binding_unavailable', { device_id: 'camera', expected_driver_id: 'driver', actual_driver_id: 'other', reason: 'mismatch' }, true],
  ['action_validation_failed', { issues: [] }, false],
  ['action_validation_failed', { issues: [{ field: 'params', reason: 'required' }] }, true],
  ['duplicate_auto_preview', { source_action_name:'shoot', obtain_action_names:['a','b'], issues:[{field:'actions[1].scheduled_at', reason:'range', value:'2026-02-30 09:00:00'}] }, true],
  ['duplicate_auto_preview', { source_action_name:'shoot', obtain_action_names:['a','b'], issues:[] }, false],
]) {
  const { check } = errorChecks.get(code);
  assert.equal(Boolean(check(value)), valid, `${code}: 错误详情结构：${ajv.errorsText(check.errors)}`);
}
function checkErrors(value, label) {
  if (!value || typeof value !== 'object') return;
  if (typeof value.code === 'string' && errorChecks.has(value.code)) {
    const { stage, check } = errorChecks.get(value.code);
    assert.equal(value.stage, stage, `${label}: ${value.code} 的阶段`);
    assert(check(value.details), `${label}: ${value.code}: ${ajv.errorsText(check.errors)}`);
  }
  for (const child of Object.values(value)) checkErrors(child, label);
}
async function main() {
  const examples = join(root, 'protocol/examples');
  let reports = 0, plans = 0, capabilities = 0;
  for (const file of (await readdir(examples, { recursive: true })).filter(f => f.endsWith('.json'))) {
    const absolute = join(examples, file);
    const bytes = await readFile(absolute);
    const value = JSON.parse(bytes.toString('utf8'));
    if (basename(file).startsWith('status-report-')) {
      validate('status-report.schema.json', value, file);
      const digest = createHash('sha256').update(bytes).digest('hex');
      assert.equal(basename(file), `status-report-${value.report_id}-${digest}.json`, `${file}: 报告原始字节摘要`);
      checkErrors(value, file);
      reports++;
    } else if (Array.isArray(value.devices)) {
      validate('capabilities.schema.json', value, file);
      const deviceIds = new Set();
      for (const device of value.devices) {
        assert(!deviceIds.has(device.device_id), `${file}: 设备 ID 重复`);
        deviceIds.add(device.device_id);
        const actionTypes = new Set();
        for (const action of device.actions) {
          assert(!actionTypes.has(action.type), `${file}: 拍摄动作重复`);
          actionTypes.add(action.type);
          const parameterTypes = new Set();
          for (const parameter of action.parameter_types) {
            assert(!parameterTypes.has(parameter.type), `${file}: 参数类型重复`);
            parameterTypes.add(parameter.type);
            assert.equal(parameter.schema.$schema, 'https://json-schema.org/draft/2020-12/schema');
            assert.equal(parameter.schema.type, 'object');
            assert(parameter.schema.required.includes('type'));
            assert.equal(parameter.schema.properties.type.const, parameter.type);
            new Ajv({ strict: false, allErrors: true }).compile(parameter.schema);
          }
        }
      }
      capabilities++;
    } else if (value.request_id && Array.isArray(value.actions) && !value.plan_instance_id) {
      validate('plan.schema.json', value, file);
      plans++;
    }
  }
  const workflows = join(examples, 'workflows');
  const workflowCapabilities = await json(join(workflows, 'capabilities.json'));
  for (const file of ['plan.json', 'duplicate-auto-plan.json']) {
    for (const action of (await json(join(workflows, file))).actions) {
      if (!action.device_id) continue;
      const device = workflowCapabilities.devices.find(d => d.device_id === action.device_id);
      const capability = device?.actions.find(a => a.type === action.type);
      const parameter = capability?.parameter_types.find(p => p.type === action.params.type);
      assert(parameter, `${file}: 演示设备参数类型未定义`);
      const check = new Ajv({ strict: false, allErrors: true }).compile(parameter.schema);
      assert(check(action.params), `${file}: ${ajv.errorsText(check.errors)}`);
    }
  }
  const cases = await json(join(workflows, 'schema-cases.json'));
  for (const entry of cases) {
    const check = ajv.getSchema(entry.schema) ?? ajv.compile({ $ref: entry.schema });
    assert.equal(Boolean(check(entry.value)), entry.valid, `${entry.name}: ${ajv.errorsText(check.errors)}`);
  }
  const reportFiles = await json(join(workflows, 'reports.json'));
  for (const file of Object.values(reportFiles)) await readFile(resolve(workflows, file));
  console.log(`协议规格校验通过：${schemaFiles.length} 份 Schema，${reports} 份报告及摘要，${plans} 份计划，${capabilities} 份能力说明，${cases.length} 个结构正反例；${actionErrorIds.size} 个动作错误编号及已登记错误详情通过。`);
  console.log('边界：未运行生产受理、客户端合并或设备联调；跨实体语义仍按样例说明及行为规格验收。');

  const motorCases = await json(join(examples, "host-notifications/cases.json"));
  for (const entry of motorCases) {
    const result = checkMotorFixture(entry.kind, entry.json);
    assert.equal(result.valid, entry.valid, `${entry.name}: 电机夹具合法性`);
    if (entry.admission) assert.equal(result.admission, entry.admission, `${entry.name}: 受理层次`);
    if (entry.valid && entry.kind === "report") checkErrors(result.value, entry.name);
    if (entry.wire) {
      const canonical = checkMotorFixture("notification", entry.wire);
      assert(canonical.valid && entry.wire.endsWith("\n"), `${entry.name}: 完整规范通知`);
      assert.equal(String(canonical.value.params.position), entry.position);
      assert.deepEqual(canonical.value, result.value);
      assert.equal(entry.wire, JSON.stringify(canonical.value) + "\n");
    }
  }
  console.log(`电机共同夹具通过：${motorCases.length} 个精确输入、受理层次与报告正反例。`);
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await main();
