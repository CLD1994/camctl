// 规格登记检查；生产历史查询、事件应用和报告生成另行接入。
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(new URL('../apps/client/package.json', import.meta.url));
const Ajv = require('ajv/dist/2020').default;
const expression = { $ref: '#/$defs/expression' };
const text = { type: 'string', minLength: 1 };
const strings = { type: 'array', items: text, uniqueItems: true };
const column = { type: 'string', pattern: '^[a-z_][a-z0-9_]*\\.[a-z_][a-z0-9_]*$' };
const object = (properties, required = Object.keys(properties)) => ({ type: 'object', properties, required, additionalProperties: false });
const operation = (op, properties = {}, required = Object.keys(properties)) => object({ op: { const: op }, ...properties }, ['op', ...required]);
const grammar = {
  $defs: {
    expression: { discriminator: { propertyName: 'op' }, oneOf: [
      operation('literal', { value: {} }), operation('omit'), operation('error', { code: { const: 'state_database_error' } }),
      operation('read', { column, encoding: { enum: ['identity', 'id', 'utc_seconds', 'json'] }, schema: text }, ['column', 'encoding']),
      operation('enum', { column, map: { type: 'object', minProperties: 1, additionalProperties: text } }),
      operation('enum_is', { column, members: { ...strings, minItems: 1 } }),
      operation('not_null', { column }), operation('exists', { table: text }),
      ...['json_member', 'json_has'].map(op => operation(op, { column, pointer: { type: 'string', pattern: '^/' } })),
      ...['all', 'any'].map(op => operation(op, { args: { type: 'array', minItems: 1, items: expression } })),
      operation('eq', { left: expression, right: expression }), operation('not', { arg: expression }),
      operation('cases', { branches: { type: 'array', minItems: 1, items: object({ when: expression, value: expression }) }, otherwise: expression }),
      operation('project', { projection: text }),
      operation('rows', { projection: text, relations: strings, order_by: { type: 'array', minItems: 1, items: column } }),
      operation('entities', { entity: text, selection: { const: 'report_targets_and_parents' }, order_by: column, empty: { const: 'omit' } }),
      operation('extra_input', { column, exclude_schema: text, empty: { const: 'omit' } }),
      operation('registered_error', { code_column: column, details_column: column, registry_key: text, schema: { const: '#/$defs/error' } }),
      operation('failure_union', {
        branches: { type: 'array', minItems: 1, items: object({ projection: text, relations: strings, identity: column }) },
        order_by: strings, duplicate: { const: 'state_database_error' }, empty: { const: 'array' }
      })
    ] }
  },
  ...object({
    format_version: { const: 1 }, public_schema: text, enum_registry: text, error_registry: text, contract: text,
    boundary: object({ history: { const: 'same_frozen_h' }, impact: { const: 'event_before_and_after' }, missing_required: { const: 'state_database_error' }, compare: { const: 'public_value_and_presence' }, internal_only: { const: 'no_report_target' }, parent_fill: { const: 'no_additional_change_seq' } }),
    entities: { type: 'object', additionalProperties: object({ table: text, identity: text, target: { type: 'boolean' }, parent: object({ entity: text, foreign_key: text }) }, ['table', 'identity', 'target']) },
    relations: { type: 'object', additionalProperties: object({ from: column, to: column, cardinality: { enum: ['one', 'optional_one', 'many'] } }) },
    projections: { type: 'object', additionalProperties: object({
      schema: text, root_table: text, entity: text, relations: strings, when: expression,
      fields: { type: 'object', minProperties: 1, additionalProperties: object({ when: expression, value: expression }) },
      assertions: strings, empty: { const: 'omit' }
    }, ['schema', 'root_table', 'entity', 'relations', 'when', 'fields']) },
    assertions: { type: 'object', additionalProperties: object({ columns: { type: 'array', minItems: 1, items: column, uniqueItems: true }, contract: text }) },
    routes: { type: 'object', additionalProperties: { type: 'object' } },
    sql_pending: { type: 'object', additionalProperties: object({ columns: { ...strings, minItems: 1 }, specification: text,
      foreign_keys: { type: 'array', items: object({ column: text, references: column }) }
    }, ['columns', 'specification']) },
    notes: { type: 'object', additionalProperties: text }
  })
};
const ajv = new Ajv({ strict: false, allErrors: true, discriminator: true });
const validateShape = ajv.compile(grammar);

export function pointer(document, path) {
  assert(path === '#' || path.startsWith('#/'), `仅允许文档内 JSON Pointer：${path}`);
  return path === '#' ? document : path.slice(2).split('/').reduce((node, key) => {
    key = key.replaceAll('~1', '/').replaceAll('~0', '~');
    assert(node && Object.hasOwn(node, key), `无效 JSON Pointer：${path}`);
    return node[key];
  }, document);
}

function propertiesOf(node, schema) {
  if (!node || typeof node !== 'object') return {};
  const result = { ...(node.$ref ? propertiesOf(pointer(schema, node.$ref), schema) : {}), ...node.properties };
  for (const branch of node.allOf ?? []) Object.assign(result, propertiesOf(branch, schema));
  for (const branch of [...node.oneOf ?? [], ...node.anyOf ?? []]) {
    for (const [key, value] of Object.entries(propertiesOf(branch, schema))) {
      result[key] = result[key] ? { anyOf: [result[key], value] } : value;
    }
  }
  return result;
}

function literalValues(node, schema) {
  if (!node || typeof node !== 'object') return undefined;
  if (node.$ref) return literalValues(pointer(schema, node.$ref), schema);
  if (node.enum) return node.enum;
  if (Object.hasOwn(node, 'const')) return [node.const];
  const branches = node.oneOf ?? node.anyOf;
  if (!branches) return undefined;
  const values = branches.map(branch => literalValues(branch, schema));
  return values.every(Boolean) ? values.flat() : undefined;
}

function structuredValue(node, schema) {
  if (!node || typeof node !== 'object') return false;
  if (node.$ref && structuredValue(pointer(schema, node.$ref), schema)) return true;
  const types = Array.isArray(node.type) ? node.type : [node.type];
  return types.some(type => ['object', 'array'].includes(type)) ||
    [...node.allOf ?? [], ...node.oneOf ?? [], ...node.anyOf ?? []].some(branch => structuredValue(branch, schema));
}

function generatedLiterals(node) {
  if (node.op === 'literal') return [node.value];
  if (node.op === 'enum') return Object.values(node.map);
  if (['omit', 'error'].includes(node.op)) return [];
  if (node.op !== 'cases') return undefined;
  const values = [...node.branches.map(branch => generatedLiterals(branch.value)), generatedLiterals(node.otherwise)];
  return values.every(Boolean) ? values.flat() : undefined;
}

/** 检查真实规格之间的结构接缝；返回可供覆盖审计使用的列集合。 */
export function validateRegistration(registry, schema, { enums, errors, tables, foreignKeys }) {
  assert(validateShape(registry), `登记格式错误：${ajv.errorsText(validateShape.errors)}`);
  const usedColumns = new Set(), pendingSql = new Set(), usedPending = new Set();
  const usedEntities = new Set(['report']), referencedProjections = new Set(['report']);
  const schemaCoverage = new Set(), projectionEdges = new Map();
  const expectedParents = new Map();
  const localAjv = new Ajv({ strict: false, allErrors: true });
  localAjv.addSchema(schema, 'report');
  const availableTables = new Set([...Object.keys(tables), ...Object.keys(registry.sql_pending)]);
  assert(enums.history_objects, '缺少统一历史对象登记');
  const reportTargets = Object.entries(enums.history_objects).filter(([, rule]) => rule.report_target).map(([name]) => name);
  assert.deepEqual(Object.entries(registry.entities).filter(([, rule]) => rule.target).map(([name]) => name).sort(),
    reportTargets.sort(), '报告实体与历史对象登记的报告资格不一致');
  for (const [name, entity] of Object.entries(registry.entities)) {
    assert.equal(entity.table, enums.history_objects[name]?.table, `报告实体与历史对象主表不一致：${name}`);
  }
  const keys = new Map(Object.entries(foreignKeys));
  for (const [table, pending] of Object.entries(registry.sql_pending)) for (const key of pending.foreign_keys ?? []) {
    assert(pending.columns.includes(key.column), `待同步外键不在待同步列中：${table}.${key.column}`);
    assert(!keys.has(`${table}.${key.column}`), `待同步外键已存在：${table}.${key.column}`);
    keys.set(`${table}.${key.column}`, key.references);
  }
  function checkColumn(value, scope) {
    const [table, name] = value.split('.');
    assert(availableTables.has(table), `来源表不存在：${value}`);
    assert(!scope || scope.has(table), `缺少来源关联：${value}`);
    if (!tables[table]?.includes(name)) {
      assert(registry.sql_pending[table]?.columns.includes(name), `来源列不存在：${value}`);
      pendingSql.add(value); usedPending.add(value);
    }
    usedColumns.add(value);
  }
  for (const [name, relation] of Object.entries(registry.relations)) {
    checkColumn(relation.from); checkColumn(relation.to);
    assert(keys.get(relation.from) === relation.to || keys.get(relation.to) === relation.from, `关联不是已定义外键：${name}`);
  }
  function joinedTables(root, relationNames) {
    const scope = new Set([root]);
    const remaining = new Set(relationNames);
    while (remaining.size) {
      let progressed = false;
      for (const name of remaining) {
        const relation = registry.relations[name];
        assert(relation, `未知关联：${name}`);
        assert(keys.get(relation.from) === relation.to || keys.get(relation.to) === relation.from, `关联不是已定义外键：${name}`);
        const a = relation.from.split('.')[0], b = relation.to.split('.')[0];
        if (!scope.has(a) && !scope.has(b)) continue;
        checkColumn(relation.from); checkColumn(relation.to);
        scope.add(a); scope.add(b); remaining.delete(name); progressed = true;
      }
      assert(progressed, `关联链不连通：${root} → ${[...remaining].join(', ')}`);
    }
    return scope;
  }
  function cover(pointerValue) {
    schemaCoverage.add(pointerValue);
    // 直接保存的公共 JSON 结构整体复用 Schema，不另抄媒体与错误成员。
    function refs(node) {
      if (!node || typeof node !== 'object') return;
      if (node.$ref && !schemaCoverage.has(node.$ref)) cover(node.$ref);
      for (const [key, value] of Object.entries(node)) if (key !== '$defs') refs(value);
    }
    refs(pointer(schema, pointerValue));
  }
  function validateValue(value, fieldSchema, label) {
    if (!fieldSchema) return;
    const validator = localAjv.compile({ ...fieldSchema, $defs: schema.$defs });
    assert(validator(value), `${label} 的公共值无效：${JSON.stringify(value)}`);
  }
  function compatible(reference, fieldSchema, label) {
    if (!fieldSchema) return;
    const refs = [];
    function rootRefs(node) {
      if (!node || typeof node !== 'object') return;
      if (node.$ref) refs.push(node.$ref);
      for (const branch of [...node.allOf ?? [], ...node.oneOf ?? [], ...node.anyOf ?? []]) rootRefs(branch);
    }
    rootRefs(fieldSchema);
    if (refs.length) assert(refs.includes(reference), `${label} 的输出结构不匹配：${reference}`);
    else if (Object.keys(fieldSchema).length > 1 || fieldSchema.type !== 'object') assert.deepEqual(pointer(schema, reference), fieldSchema, `${label} 的直接 JSON 结构不匹配`);
  }
  function visit(node, scope, parent, fieldSchema) {
    const edge = child => {
      assert(registry.projections[child], `未知投影：${child}`);
      referencedProjections.add(child);
      projectionEdges.get(parent).add(child);
    };
    if (node.column) checkColumn(node.column, scope);
    if (node.schema) { pointer(schema, node.schema); cover(node.schema); compatible(node.schema, fieldSchema, parent); }
    switch (node.op) {
      case 'read':
        assert(node.encoding !== 'json' || node.schema, `${node.column} 的 JSON 缺少公共结构引用`);
        assert(!(node.schema || structuredValue(fieldSchema, schema)) || node.encoding === 'json', `${node.column} 的结构化值必须解码 JSON`);
        break;
      case 'literal': validateValue(node.value, fieldSchema, parent); break;
      case 'enum': case 'enum_is': {
        const members = enums.enums[node.column]?.members;
        assert(members, `未登记内部枚举：${node.column}`);
        for (const member of node.members ?? Object.keys(node.map)) assert(Object.hasOwn(members, member), `未知枚举成员：${node.column}.${member}`);
        if (node.map) {
          const guard = registry.projections[parent].when;
          const requiredMembers = guard.op === 'enum_is' && guard.column === node.column ? guard.members : Object.keys(members);
          assert.deepEqual(Object.keys(node.map).sort(), [...requiredMembers].sort(), `内部枚举映射不完整：${node.column}`);
          for (const value of Object.values(node.map)) validateValue(value, fieldSchema, node.column);
        }
        break;
      }
      case 'exists': assert(scope.has(node.table), `存在性条件缺少关联：${node.table}`); break;
      case 'all': case 'any': for (const arg of node.args) visit(arg, scope, parent); break;
      case 'eq': visit(node.left, scope, parent); visit(node.right, scope, parent); break;
      case 'not': visit(node.arg, scope, parent); break;
      case 'cases':
        for (const branch of node.branches) { visit(branch.when, scope, parent); visit(branch.value, scope, parent, fieldSchema); }
        visit(node.otherwise, scope, parent, fieldSchema); break;
      case 'project':
        edge(node.projection);
        compatible(registry.projections[node.projection].schema, fieldSchema, parent);
        assert(scope.has(registry.projections[node.projection].root_table), `投影根缺少关联：${parent} → ${node.projection}`);
        break;
      case 'rows': {
        edge(node.projection);
        compatible(registry.projections[node.projection].schema, fieldSchema?.items, parent);
        const joined = joinedTables(registry.projections[parent].root_table, [...registry.projections[parent].relations, ...node.relations]);
        assert(joined.has(registry.projections[node.projection].root_table), `集合根缺少关联：${node.projection}`);
        for (const col of node.order_by) checkColumn(col, joined);
        break;
      }
      case 'failure_union':
        for (const branch of node.branches) {
          edge(branch.projection);
          compatible(registry.projections[branch.projection].schema, fieldSchema?.items, parent);
          const joined = joinedTables(registry.projections[parent].root_table, branch.relations);
          assert(joined.has(registry.projections[branch.projection].root_table), `失败项根缺少关联：${branch.projection}`);
          checkColumn(branch.identity, joined);
        }
        for (const col of node.order_by.filter(x => x.includes('.'))) checkColumn(col);
        break;
      case 'entities':
        assert(registry.entities[node.entity]?.target, `非法报告实体：${node.entity}`);
        usedEntities.add(node.entity); edge(node.entity); checkColumn(node.order_by);
        compatible(registry.projections[node.entity].schema, fieldSchema?.items, parent);
        if (parent !== 'report') {
          const owner = registry.projections[parent].entity;
          assert(!expectedParents.has(node.entity) || expectedParents.get(node.entity) === owner, `实体父归属矛盾：${node.entity}`);
          expectedParents.set(node.entity, owner);
        }
        break;
      case 'registered_error': {
        checkColumn(node.code_column, scope); checkColumn(node.details_column, scope);
        const table = node.code_column.split('.')[0];
        assert.equal(node.code_column, `${table}.error_code`, '错误编号须读取所属错误列');
        assert.equal(node.details_column, `${table}.error_details_json`, '错误详情须与编号属于同一记录');
        assert.equal(node.registry_key, table === 'actions' ? 'action_error_id' : `item_error_ids.${table}`, '错误编号登记与所属记录不一致');
        const get = rule => node.registry_key.split('.').reduce((value, key) => value?.[key], rule);
        assert(Object.values(errors.codes).some(rule => Number.isInteger(get(rule))), `无效错误编号引用：${node.registry_key}`);
        break;
      }
      case 'extra_input':
        assert(node.exclude_schema.endsWith('plan.schema.json#/$defs/action/properties'), '原输入字段须从计划公共结构读取');
        break;
      case 'not_null': case 'json_has': case 'json_member': case 'omit': case 'error': break;
      default: assert.fail(`未知登记操作：${node.op}`);
    }
  }
  let fields = 0;
  for (const [name, projection] of Object.entries(registry.projections)) {
    const node = pointer(schema, projection.schema);
    schemaCoverage.add(projection.schema);
    const expected = propertiesOf(node, schema);
    assert.deepEqual(Object.keys(projection.fields).sort(), Object.keys(expected).sort(), `${name} 的公开字段覆盖不一致`);
    assert(registry.entities[projection.entity], `未知报告归属：${name}`);
    assert.equal(registry.routes[projection.root_table]?.entity, projection.entity, `投影与历史对象的报告归属不一致：${name}`);
    assert(availableTables.has(projection.root_table), `投影根表不存在：${name}`);
    const scope = joinedTables(projection.root_table, projection.relations);
    projectionEdges.set(name, new Set());
    visit(projection.when, scope, name);
    for (const assertionName of projection.assertions ?? []) {
      const assertion = registry.assertions[assertionName];
      assert(assertion, `未知前置校验：${assertionName}`);
      for (const col of assertion.columns) checkColumn(col, scope);
    }
    for (const [field, definition] of Object.entries(projection.fields)) {
      visit(definition.when, scope, name); visit(definition.value, scope, name, expected[field]); fields++;
      const allowed = literalValues(expected[field], schema), generated = generatedLiterals(definition.value);
      if (allowed && generated) assert.deepEqual([...new Set(generated)].sort(), [...new Set(allowed)].sort(), `${name}.${field} 的公共枚举覆盖不完整`);
    }
  }
  assert.deepEqual([...usedEntities].sort(), Object.keys(registry.entities).sort(), '存在未对应公开实体的报告目标');
  for (const [name, entity] of Object.entries(registry.entities)) {
    assert.equal(registry.projections[name]?.root_table, entity.table, `实体主投影不匹配：${name}`);
    assert.equal(entity.parent?.entity, expectedParents.get(name), `实体父归属缺失或错误：${name}`);
    if (entity.parent) {
      const foreignKey = `${entity.table}.${entity.parent.foreign_key}`;
      checkColumn(foreignKey);
      const target = registry.entities[entity.parent.entity];
      assert.equal(keys.get(foreignKey), `${target.table}.${target.identity}`, `父对象外键错误：${name}`);
    }
  }
  assert.deepEqual([...referencedProjections].sort(), Object.keys(registry.projections).sort(), '存在无法从根报告到达的投影');
  const visited = new Set();
  function acyclic(name, stack = new Set()) {
    assert(!stack.has(name), `投影递归：${name}`);
    if (visited.has(name)) return;
    for (const child of projectionEdges.get(name)) acyclic(child, new Set([...stack, name]));
    visited.add(name);
  }
  acyclic('report');
  // 遍历公开输出可用分支；if/not 是约束谓词，不是新增输出结构。
  const requiredRefs = new Set();
  function required(node) {
    if (!node || typeof node !== 'object') return;
    if (node.$ref && !requiredRefs.has(node.$ref)) { requiredRefs.add(node.$ref); required(pointer(schema, node.$ref)); }
    for (const [key, value] of Object.entries(node)) if (!['$defs', 'if', 'not'].includes(key)) required(value);
  }
  required(schema);
  for (const ref of requiredRefs) if (Object.keys(propertiesOf(pointer(schema, ref), schema)).length) assert(schemaCoverage.has(ref), `缺少公开结构映射：${ref}`);
  for (const [table, route] of Object.entries(registry.routes)) {
    assert(availableTables.has(table), `路由来源不存在：${table}`);
    const choices = route.mode === 'per_relation' ? route.targets : [route];
    assert(['public_change', 'frozen_metadata', 'per_relation'].includes(route.mode), `未知路由方式：${table}`);
    for (const choice of choices) {
      const entity = registry.entities[choice.entity];
      assert(entity && (entity.target || route.mode === 'frozen_metadata'), `非法报告目标：${table}`);
      const scope = joinedTables(table, choice.relations ?? []);
      checkColumn(choice.identity, scope);
      const primaryKey = `${entity.table}.${entity.identity}`;
      assert(choice.identity === primaryKey || Object.values(registry.relations).some(relation =>
        (relation.from === primaryKey && relation.to === choice.identity) || (relation.to === primaryKey && relation.from === choice.identity)), `目标身份未指向所属实体：${table} → ${choice.identity}`);
      if (choice.when) visit(choice.when, scope, 'report');
    }
  }
  for (const col of usedColumns) assert(registry.routes[col.split('.')[0]], `依赖缺少报告路由：${col}`);
  for (const [table, pending] of Object.entries(registry.sql_pending)) for (const name of pending.columns) {
    assert(usedPending.has(`${table}.${name}`), `SQL 待同步声明已过期或未使用：${table}.${name}`);
  }
  return { fields, projections: Object.keys(registry.projections).length, columns: [...usedColumns].sort(), pendingSql: [...pendingSql].sort() };
}
