// 设计登记检查；不执行生产事务或外部业务校验。
import assert from 'node:assert/strict';
import { isDeepStrictEqual } from 'node:util';
import { createRequire } from 'node:module';
const require = createRequire(new URL('../apps/client/package.json', import.meta.url));
const Ajv = require('ajv/dist/2020').default;
const string = { type: 'string', minLength: 1 };
const strings = { type: 'array', items: string, uniqueItems: true };
const values = { type: 'object', additionalProperties: { type: 'array', minItems: 1, uniqueItems: true, items: { type: ['integer', 'string', 'boolean', 'null'] } } };
const object = (properties, required = Object.keys(properties)) => ({ type: 'object', properties, required, additionalProperties: false });
const schema = object({
  format_version: { const: 1 }, report_dependencies: { const: 'report-dependencies.json' }, contract: string,
  tables: { type: 'object', additionalProperties: object({ immutable: strings, mutable: strings, derived: strings, write_once: strings, state_columns: strings, owner: { $ref: '#/$defs/owner' } }) },
  guards: { type: 'object', additionalProperties: object({ contract: string, rule: string, scope: { oneOf: [
    { const: 'transaction' }, object({ rows: { type: 'array', minItems: 1, items: object({ table: string, op: { enum: ['create','update'] } }) } })
  ] } }) },
  state_models: { type: 'object', additionalProperties: object({ edges: { type: 'array', minItems: 1, items: object({ id: string, from: { type: 'integer' }, to: { type: 'integer' }, by: { ...strings, minItems: 1 } }) } }) },
  events: { type: 'object', additionalProperties: object({ id: { type: 'integer', minimum: 1 }, version: { const: 1 }, branches: { type: 'object', minProperties: 1, additionalProperties: object({ reason: { type: 'integer', minimum: 1 }, title: string, rows: { type: 'array', minItems: 1, items: { oneOf: [
    object({ table: string, op: { const: 'create' }, after: values }),
    object({ table: string, op: { const: 'update' }, columns: { ...strings, minItems: 1 }, before: values, after: values, required: strings, transitions: { type: 'object', additionalProperties: { ...strings, minItems: 1 } } })
  ] } }, guards: { ...strings, minItems: 1 }, evidence: strings }) } }) },
  history_references: { type: 'object', additionalProperties: object({ target: { enum: ['creating_event','assigning_event','baseline_first_chunk','baseline_last_chunk','prior_complete_boundary','creating_transaction_end'] }, exception: string }, ['target']) },
  history_contract: string,
  derived_history: { type: 'object', additionalProperties: object({ method: { enum: ['first_own_event','last_own_event','own_event_count'] } }) },
  structural_history_references: { type: 'object', additionalProperties: object({ target: string, rule: string }) },
  non_event_tables: { type: 'object', additionalProperties: string },
});
schema.$defs = { owner: { oneOf: [
  object({ entity: string, id: string, via: strings }),
  object({ inherit: string }),
  object({ cases: { type: 'array', minItems: 1, items: object({ when: { type: 'object', minProperties: 1, additionalProperties: { anyOf: [{ const: 'present' }, { type: 'array', minItems: 1, items: { type: 'integer' } }] } }, owner: { $ref: '#/$defs/owner' } }) }, otherwise: { $ref: '#/$defs/owner' } })
] } };
const ajv = new Ajv({ strict: false, allErrors: true });
const shape = ajv.compile(schema);
const sorted = values => [...values].sort();
const branchEntries = registry => Object.entries(registry.events).flatMap(([event, definition]) =>
  Object.entries(definition.branches).map(([name, branch]) => [`${event}.${name}`, branch]));
function references(value, columns = new Set(), isColumn = false) {
  if (isColumn && typeof value === 'string' && /^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$/.test(value)) columns.add(value);
  else if (Array.isArray(value)) value.forEach(v => references(v, columns, isColumn));
  else if (value && typeof value === 'object') Object.entries(value).forEach(([key,v]) =>
    references(v, columns, ['column','code_column','details_column','identity','from','to','order_by','columns'].includes(key)));
  return columns;
}

export function validateEventRegistration(registry, { tables, foreignKeys, enums, reports }) {
  assert(shape(registry), `事件登记格式错误：${ajv.errorsText(shape.errors)}`);
  assert.deepEqual(sorted([...Object.keys(registry.tables), ...Object.keys(registry.non_event_tables)]), sorted(Object.keys(tables)), '实际表的事件职责分类不完整');
  const branches = new Map(branchEntries(registry));
  const usedGuards = new Set(), updated = new Set(), created = new Set(), reportColumns = references(reports);
  const identifiers = new Set();
  function column(table, col) {
    assert(registry.tables[table], `未知业务表：${table}`);
    assert(tables[table].includes(col), `未知列：${table}.${col}`);
  }
  function foreign(table, col) {
    column(table, col);
    const target = foreignKeys[`${table}.${col}`];
    assert(target, `历史归属缺少实际关联：${table}.${col}`);
    return target.split('.')[0];
  }
  function owner(table, rule, stack = []) {
    assert(!stack.includes(table), `历史归属循环：${[...stack, table]}`);
    if (rule.cases) {
      for (const c of rule.cases) {
        Object.keys(c.when).forEach(col => column(table, col));
        owner(table, c.owner, stack);
      }
      owner(table, rule.otherwise, stack);
    } else if (rule.inherit) {
      const target = foreign(table, rule.inherit);
      assert(registry.tables[target], `历史归属引用非业务表：${target}`);
      owner(target, registry.tables[target].owner, [...stack, table]);
    } else {
      let current = table;
      for (const step of rule.via) current = foreign(current, step);
      column(current, rule.id);
      const entity = enums.history_objects[rule.entity];
      assert(entity, `未知历史对象：${rule.entity}`);
      assert(current === entity.table && rule.id === 'id' || foreignKeys[`${current}.${rule.id}`] === `${entity.table}.id`, `历史归属未到目标身份：${table}`);
      // 同时属于自身报告的直接成员，其报告根与历史根必须是同一个稳定身份。
      const route = reports.routes[table];
      if (route?.entity === rule.entity && route.mode === 'public_change') {
        assert.equal(`${current}.${rule.id}`, route.identity, `历史归属与自身报告成员不一致：${table}`);
      }
    }
  }
  for (const [table, definition] of Object.entries(registry.tables)) {
    const all = ['immutable','mutable','derived'].flatMap(k => definition[k]);
    assert.equal(new Set(all).size, all.length, `列重复分类：${table}`);
    assert.deepEqual(sorted(all), sorted(tables[table]), `列分类与 SQL 不一致：${table}`);
    for (const col of definition.write_once) assert(definition.mutable.includes(col), `只写一次列不是业务可变列：${table}.${col}`);
    assert.deepEqual(sorted(definition.state_columns), sorted(Object.keys(registry.state_models).filter(c => c.startsWith(`${table}.`)).map(c => c.split('.')[1])), `状态列缺少完整模型：${table}`);
    owner(table, definition.owner);
  }
  for (const [name, definition] of Object.entries(registry.events)) {
    assert(/^[A-Z][A-Z0-9_]+$/.test(name), `事件标识无效：${name}`);
    assert(!identifiers.has(definition.id), `事件编号重复：${definition.id}`); identifiers.add(definition.id);
    const reasons = new Set();
    for (const [key, branch] of Object.entries(definition.branches)) {
      assert(/^[A-Z][A-Z0-9_]+$/.test(key), `分支标识无效：${key}`);
      assert(!reasons.has(branch.reason), `分支编号重复：${name}`); reasons.add(branch.reason);
      for (const guard of branch.guards) {
        assert(registry.guards[guard], `未定义的业务校验：${guard}`); usedGuards.add(guard);
        const scope = registry.guards[guard].scope;
        assert(scope === 'transaction' || scope.rows.some(selector => branch.rows.some(row => selector.table === row.table && selector.op === row.op)), `业务校验阶段与分支不相容：${name}.${key}/${guard}`);
      }
      for (const mandatory of ['row_shape','transaction','ownership','report_impact']) assert(branch.guards.includes(mandatory), `分支缺少公共校验：${name}.${key}/${mandatory}`);
      for (const row of branch.rows) {
        const t = registry.tables[row.table]; assert(t, `分支使用未知业务表：${row.table}`);
        if (row.op === 'create') created.add(row.table);
        for (const side of [row.before ?? {}, row.after]) for (const [col,values] of Object.entries(side)) {
          column(row.table,col); assert(!t.derived.includes(col), `条件引用非业务列：${row.table}.${col}`);
          const members = enums.enums[`${row.table}.${col}`]?.members;
          if (members) for (const value of values) assert(value === null || Object.values(members).includes(value), `分支条件使用未知编号：${row.table}.${col}/${value}`);
        }
        if (row.op === 'update') {
          for (const col of row.columns) { assert(t.mutable.includes(col), `分支越权修改：${row.table}.${col}`); updated.add(`${row.table}.${col}`); }
          for (const col of row.required) assert(row.columns.includes(col), `必改列不在权限内：${row.table}.${col}`);
          assert.deepEqual(sorted(Object.keys(row.transitions)), sorted(row.columns.filter(c => t.state_columns.includes(c))), `状态列缺少明确转换引用：${name}.${key}/${row.table}`);
          for (const [col, ids] of Object.entries(row.transitions)) {
            const model = registry.state_models[`${row.table}.${col}`];
            for (const id of ids) {
              const edge = model?.edges.find(e => e.id === id);
              assert(edge, `分支引用缺失状态边：${name}.${key}/${row.table}.${col}/${id}`);
              assert(edge.by.includes(`${name}.${key}`), `状态边缺少承担分支：${name}.${key}/${id}`);
              assert((!row.before[col] || row.before[col].includes(edge.from)) && (!row.after[col] || row.after[col].includes(edge.to)), `状态变化与分支条件不相容：${name}.${key}/${id}`);
            }
          }
        }
      }
    }
  }
  assert.deepEqual(sorted(usedGuards), sorted(Object.keys(registry.guards)), '存在未使用的业务校验');
  for (const [table, definition] of Object.entries(registry.tables)) {
    if (table !== 'runtime_state') assert(created.has(table), `业务表缺少创建事件：${table}`);
    for (const col of definition.mutable) assert(updated.has(`${table}.${col}`), `可变列缺少事件：${table}.${col}`);
  }
  for (const [full, model] of Object.entries(registry.state_models)) {
    const [table, col] = full.split('.'); column(table, col);
    assert(registry.tables[table].mutable.includes(col), `状态模型指向非可变列：${full}`);
    const edges = new Set(), edgeIds = new Set();
    for (const edge of model.edges) {
      assert(!edgeIds.has(edge.id), `状态边标识重复：${full}/${edge.id}`); edgeIds.add(edge.id);
      assert.notEqual(edge.from, edge.to, `状态边没有变化：${full}`);
      const id = `${edge.from}/${edge.to}`; assert(!edges.has(id), `重复状态边：${full}/${id}`); edges.add(id);
      if (enums.enums[full]) for (const value of [edge.from,edge.to]) assert(Object.values(enums.enums[full].members).includes(value), `状态编号未登记：${full}/${value}`);
      for (const by of edge.by) {
        const branch = branches.get(by); assert(branch, `状态转换没有事件分支：${full}/${by}`);
        assert(branch.rows.some(row => row.table === table && row.op === 'update' && row.transitions[col]?.includes(edge.id) &&
          (!row.before[col] || row.before[col].includes(edge.from)) && (!row.after[col] || row.after[col].includes(edge.to))), `状态边与分支权限不匹配：${full}/${by}`);
      }
    }
  }
  for (const full of reportColumns) {
    const [table, col] = full.split('.');
    column(table,col);
  }
  // 结合实际外键及明确列名检查覆盖；具体引用含义仍由规则文件决定。
  const historyTargets = new Set(['history_events.id','history_events.change_seq','history_transactions.id','history_transactions.last_event_id','entity_snapshots.id']);
  const referenceTargets = {
    creating_event: 'history_events.id', assigning_event: 'history_events.id',
    baseline_first_chunk: 'history_events.id', baseline_last_chunk: 'history_events.id',
    creating_transaction_end: 'history_transactions.last_event_id', prior_complete_boundary: null,
  };
  const derivedColumns = { first_own_event: 'created_event_id', last_own_event: 'last_event_id', own_event_count: 'change_count' };
  for (const [table, definition] of Object.entries(registry.tables)) {
    assert.deepEqual(sorted(definition.derived.filter(col => col !== 'id')),
      sorted(Object.keys(registry.derived_history).filter(full => full.startsWith(`${table}.`)).map(full => full.split('.')[1])), `派生历史列缺少计算规则：${table}`);
    for (const col of [...definition.immutable,...definition.mutable]) {
      if (col.endsWith('_event_id') || historyTargets.has(foreignKeys[`${table}.${col}`])) assert(registry.history_references[`${table}.${col}`], `业务历史引用缺少编码规则：${table}.${col}`);
    }
  }
  for (const [full, reference] of Object.entries(registry.history_references)) {
    const [table,col] = full.split('.'); column(table,col);
    assert([...registry.tables[table].immutable,...registry.tables[table].mutable].includes(col), `历史引用不是业务值：${full}`);
    assert.equal(foreignKeys[full] ?? null, referenceTargets[reference.target], `业务引用目标与 SQL 外键不一致：${full}`);
    if (reference.target === 'prior_complete_boundary') assert.equal(full, 'reports.frozen_event_id', '只有报告冻结引用允许初始化边界 0，并由业务校验核对边界');
    const fixed = ['creating_event','creating_transaction_end','prior_complete_boundary'].includes(reference.target);
    assert(registry.tables[table][fixed ? 'immutable' : 'mutable'].includes(col), `历史引用的赋值规则与列分类不一致：${full}`);
  }
  for (const [full, rule] of Object.entries(registry.derived_history)) {
    const [table,col] = full.split('.'); column(table,col);
    const definition = registry.tables[table], own = definition.owner;
    assert(definition.derived.includes(col), `计算规则指向非派生列：${full}`);
    assert.equal(col, derivedColumns[rule.method], `派生历史的计算方式与列含义不一致：${full}`);
    assert(own.entity && own.id === 'id' && own.via.length === 0 && enums.history_objects[own.entity].table === table, `派生历史必须属于对象自身：${full}`);
    assert.equal(foreignKeys[full] ?? null, rule.method === 'own_event_count' ? null : 'history_events.id', `派生历史的 SQL 引用不一致：${full}`);
  }
  for (const [full, rule] of Object.entries(registry.structural_history_references)) {
    const [table,col] = full.split('.');
    assert(registry.non_event_tables[table] && tables[table].includes(col), `历史基础表引用位置无效：${full}`);
    assert(historyTargets.has(rule.target), `历史基础表引用目标无效：${full}`);
    assert.equal(foreignKeys[full], rule.target, `历史基础表引用与 SQL 外键不一致：${full}`);
  }
  for (const [full, target] of Object.entries(foreignKeys)) {
    if (!historyTargets.has(target)) continue;
    const categories = [registry.history_references[full], registry.derived_history[full], registry.structural_history_references[full]];
    assert.equal(categories.filter(Boolean).length, 1, `实际历史引用必须有唯一用途和规则：${full}`);
  }
  return { events: identifiers.size, branches: branches.size, columns: Object.values(registry.tables).reduce((sum,t) => sum + t.immutable.length+t.mutable.length+t.derived.length,0),
    businessReferences: Object.keys(registry.history_references).length, derivedHistory: Object.keys(registry.derived_history).length, structuralReferences: Object.keys(registry.structural_history_references).length,
    states: Object.keys(registry.state_models).length, reportColumns: sorted(reportColumns),
    internalColumns: Object.entries(registry.tables).flatMap(([t,d]) => [...d.immutable,...d.mutable].map(c => `${t}.${c}`)).filter(c => !reportColumns.has(c)) };
}

/** 只校验行权限和有限状态；返回仍须执行的业务校验名称，绝不冒充它们已通过。 */
export function validateRowChange(registry, branchKey, { table, before, after }) {
  const branch = new Map(branchEntries(registry)).get(branchKey);
  assert(branch, `未知事件分支：${branchKey}`);
  const definition = registry.tables[table]; assert(definition, `未知业务表：${table}`);
  assert(after && typeof after === 'object' && !Array.isArray(after), '事件后值必须存在');
  const changed = Object.keys({ ...before, ...after }).filter(col => !before || !isDeepStrictEqual(before[col],after[col]));
  assert(changed.length, '空行变化不能生成事件');
  const match = (row, side) => Object.entries(row[side] ?? {}).every(([col, values]) => Object.hasOwn(side === 'before' ? before : after,col) && values.some(v => isDeepStrictEqual(v,(side === 'before' ? before : after)[col])));
  const allowed = branch.rows.filter(row => row.table === table && row.op === (before === null ? 'create' : 'update'));
  assert(allowed.some(row => match(row,'after') && (before === null || match(row,'before') && changed.every(c => row.columns.includes(c)) && row.required.every(c => changed.includes(c)) && changed.every(col => {
    const model = registry.state_models[`${table}.${col}`];
    return !model || model.edges.some(e => e.from === before[col] && e.to === after[col] && row.transitions[col]?.includes(e.id) && e.by.includes(branchKey));
  }))), `不满足分支的行权限或状态：${branchKey}/${table}`);
  if (before === null) assert.deepEqual(sorted(Object.keys(after)), sorted([...definition.immutable,...definition.mutable]), `创建行业务列不完整：${table}`);
  else {
    for (const col of changed) {
      assert(definition.mutable.includes(col), `更新非可变列：${table}.${col}`);
      if (definition.write_once.includes(col)) assert(before[col] == null && after[col] != null, `覆盖只写一次的事实：${table}.${col}`);
    }
  }
  return { pendingGuards: branch.guards.filter(name => {
    const scope = registry.guards[name].scope;
    return scope === 'transaction' || scope.rows.some(row => row.table === table && row.op === (before === null ? 'create' : 'update'));
  }) };
}

export function generatedEventIndex(registry) {
  return ['| 编号及事件 | 分支编号及事实 |','| --- | --- |', ...Object.entries(registry.events).sort((a,b)=>a[1].id-b[1].id).map(([name,event]) =>
    `| ${event.id} \`${name}\` | ${Object.values(event.branches).sort((a,b)=>a.reason-b.reason).map(b=>`${b.reason} ${b.title}`).join('；')} |`)].join('\n');
}
export function generatedEventSql(registry) {
  return `    event_type INTEGER NOT NULL CHECK (event_type IN (${Object.values(registry.events).map(e=>e.id).sort((a,b)=>a-b).join(',')})),`;
}
