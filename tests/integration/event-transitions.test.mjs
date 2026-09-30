import assert from 'node:assert/strict';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';
import test from 'node:test';

const directory = new URL('../../docs/camctl/database/', import.meta.url);
const json = name => JSON.parse(readFileSync(new URL(name, directory), 'utf8'));
async function fixture() {
  assert(existsSync(new URL('event-transitions.json', directory)), '缺少事件转换机器登记');
  assert(existsSync(new URL('../../scripts/event-transitions.mjs', import.meta.url)), '缺少事件转换检查器');
  const api = await import('../../scripts/event-transitions.mjs');
  const db = new DatabaseSync(':memory:');
  try {
    for (const name of readdirSync(new URL('schema/', directory)).filter(n => n.endsWith('.sql')).sort()) {
      db.exec(readFileSync(new URL(`schema/${name}`, directory), 'utf8'));
    }
    const tables = Object.fromEntries(db.prepare("SELECT name FROM sqlite_schema WHERE type='table'").all()
      .map(({ name }) => [name, db.prepare(`PRAGMA table_xinfo(${name})`).all().map(c => c.name)]));
    const foreignKeys = Object.fromEntries(Object.keys(tables).flatMap(t => db.prepare(`PRAGMA foreign_key_list(${t})`).all()
      .map(k => [`${t}.${k.from}`, `${k.table}.${k.to}`])));
    return { ...api, registry: json('event-transitions.json'), context: {
      tables, foreignKeys, enums: json('enum-registry.json'), reports: json('report-dependencies.json')
    } };
  } finally { db.close(); }
}

test('全部实际投影列有唯一分类，状态转换和报告来源得到覆盖', async () => {
  const f = await fixture();
  const result = f.validateEventRegistration(f.registry, f.context);
  assert(result.branches > 80);
  assert(result.columns > 250);
  assert(result.reportColumns.includes('file_copies.target_sha256'));
  assert(result.internalColumns.includes('operation_runs.attempts_used'));
});

for (const [name, change] of [
  ['新增 SQL 业务列未登记', f => f.context.tables.actions.push('unclassified_fact')],
  ['不可变列被开放更新', f => f.registry.events.ACTION_STARTED.branches.START.rows[0].columns.push('name')],
  ['状态转换没有事件承担', f => delete f.registry.events.RESULT_SET_CONFIRMED.branches.BEGIN],
  ['未知业务校验被引用', f => f.registry.events.ACTION_STARTED.branches.START.guards.push('unknown_guard')],
  ['两个分支复用编号', f => f.registry.events.ACTION_FINISHED.branches.FAIL.reason = 1],
  ['历史归属指向被依赖动作', f => f.registry.tables.action_dependencies.owner.id = 'depends_on_action_id'],
  ['报告来源不在事件业务列内', f => f.registry.tables.file_copies.mutable.splice(f.registry.tables.file_copies.mutable.indexOf('target_sha256'), 1)],
  ['SQL 缺少规则引用的列', f => f.context.tables.actions.splice(f.context.tables.actions.indexOf('status'), 1)],
  ['整个状态模型遗漏', f => delete f.registry.state_models['cleanup_items.status']],
  ['更新条件引用不存在的状态编号', f => f.registry.events.ACTION_STARTED.branches.START.rows[0].before.status = [99]],
  ['单条合法状态边遗漏', f => f.registry.state_models['actions.status'].edges = f.registry.state_models['actions.status'].edges.filter(e=>!(e.from===2 && e.to===3))],
  ['状态边漏掉一个承担分支', f => { const edge=f.registry.state_models['actions.status'].edges.find(e=>e.from===2&&e.to===4); edge.by=edge.by.filter(n=>n!=='ACTION_FINISHED.FAIL'); }],
  ['强制状态变化与前后条件不相容', f => f.registry.events.ACTION_FINISHED.branches.SUCCEED.rows[0].after.status=[2]],
  ['行规则漏掉仍由其承担的状态边', f => { const row=f.registry.events.ACTION_FINISHED.branches.FAIL.rows[0]; row.transitions.status=[]; }],
  ['集合固定校验不适用于已有成员的逐项核实', f => { const b=f.registry.events.READ_PERMISSION_CHANGED.branches.RESOLVE; b.guards=b.guards.filter(g=>g!=='source_selection'); b.guards.push('source_selection'); }],
  ['业务事件引用没有目标规则', f => delete f.registry.history_references['operation_attempts.result_event_id']],
  ['派生历史列没有计算规则', f => delete f.registry.derived_history['reports.last_event_id']],
  ['尝试意图不能被改为派生元数据', f => {
    const table = f.registry.tables.operation_attempts;
    table.immutable = table.immutable.filter(c => c !== 'intent_event_id'); table.derived.push('intent_event_id');
    delete f.registry.history_references['operation_attempts.intent_event_id'];
    f.registry.derived_history['operation_attempts.intent_event_id'] = {method:'first_own_event'};
  }],
  ['同步开始边界不能解释为创建事件', f => f.registry.history_references['state_syncs.started_boundary_event_id'].target = 'creating_event'],
  ['最近变化事件不能采用创建事件的计算方式', f => f.registry.derived_history['reports.last_event_id'].method = 'first_own_event'],
  ['基础表事件引用不得漏掉编码规则', f => delete f.registry.structural_history_references['entity_event_links.event_id']],
  ['基础表引用目标必须与实际外键一致', f => f.registry.structural_history_references['entity_snapshots.boundary_event_id'].target = 'history_events.id'],
]) test(name, async () => {
  const f = await fixture(); change(f);
  assert.throws(() => f.validateEventRegistration(f.registry, f.context));
});

for (const [name, key, before, after, accepted] of [
  ['开始执行', 'ACTION_STARTED.START', {status:1, execution_started:0, cancel_requested:0}, {status:2, execution_started:1, cancel_requested:0}, true],
  ['开始事件不能改写原输入', 'ACTION_STARTED.START', {status:1, execution_started:0, cancel_requested:0,name:'a'}, {status:2, execution_started:1,cancel_requested:0,name:'b'}, false],
  ['终态不能重新开始', 'ACTION_STARTED.START', {status:4, execution_started:1,cancel_requested:0}, {status:2, execution_started:1,cancel_requested:0}, false],
  ['取消生效后不能保存普通成功', 'ACTION_FINISHED.SUCCEED', {status:2,execution_started:1,cancel_requested:1}, {status:3,execution_started:1,cancel_requested:1}, false],
  ['动作成功保持已有执行标记', 'ACTION_FINISHED.SUCCEED', {status:2,execution_started:1,cancel_requested:0}, {status:3,execution_started:1,cancel_requested:0}, true],
]) test(name, async () => {
  const f = await fixture();
  const run = () => f.validateRowChange(f.registry, key, {table:'actions', before, after});
  if (accepted) assert.doesNotThrow(run); else assert.throws(run);
});

for (const [name, table, key, before, after, accepted] of [
  ['结果集合开始核实', 'device_activities','RESULT_SET_CONFIRMED.BEGIN',{result_set_state:1},{result_set_state:2},true],
  ['完整结果集合不能重开', 'device_activities','RESULT_SET_CONFIRMED.BEGIN',{result_set_state:3},{result_set_state:2},false],
  ['重试间隔已结束但仍受阻', 'operation_runs','RETRY_WAIT_CHANGED.COMPLETE',{kind:3,status:2,retry_wait_required:1},{kind:3,status:2,retry_wait_required:0},true],
  ['已经结束的操作不重新等待', 'operation_runs','RETRY_WAIT_CHANGED.REQUIRE',{kind:3,status:4,retry_wait_required:0},{kind:3,status:4,retry_wait_required:1},false],
  ['文件来源从未知变为确认', 'device_files','DEVICE_FILE_OBSERVED.OWNERSHIP',{source_action_id:null,ownership_evidence_json:null},{source_action_id:7,ownership_evidence_json:{method:1}},true],
  ['文件来源不能改指其他动作', 'device_files','DEVICE_FILE_OBSERVED.OWNERSHIP',{source_action_id:7,ownership_evidence_json:{method:1}},{source_action_id:8,ownership_evidence_json:{method:1}},false],
  ['清理终态不能重写', 'cleanup_items','CLEANUP_CHANGED.FAIL',{status:4,restriction_state:4},{status:5,restriction_state:4,error_code:4,error_details_json:{},final_event_id:9},false],
]) test(name, async () => {
  const f = await fixture(); const run = () => f.validateRowChange(f.registry,key,{table,before,after});
  if (accepted) assert.doesNotThrow(run); else assert.throws(run);
});

test('创建必须包含全部业务列，派生元数据不能混入正文', async () => {
  const f = await fixture();
  assert.throws(() => f.validateRowChange(f.registry,'SOURCE_RESOLVED.FIX',{
    table:'action_dependencies',before:null,after:{action_id:7}
  }));
  assert.doesNotThrow(() => f.validateRowChange(f.registry,'SOURCE_RESOLVED.FIX',{
    table:'action_dependencies',before:null,after:{action_id:7,depends_on_action_id:8}
  }));
  assert.throws(() => f.validateRowChange(f.registry,'SOURCE_RESOLVED.FIX',{
    table:'action_dependencies',before:null,after:{id:1,action_id:7,depends_on_action_id:8}
  }));
});

test('未知分支不回退成通用行修改', async () => {
  const f = await fixture();
  assert.throws(() => f.validateRowChange(f.registry,'ACTION_STARTED.UNKNOWN',{
    table:'actions',before:{status:1},after:{status:2}
  }));
});

test('来源解析失败在同一行变化内保存动作失败', async () => {
  const f = await fixture();
  assert.doesNotThrow(() => f.validateRowChange(f.registry,'SOURCE_RESOLVED.FAIL',{
    table:'actions',before:{status:2,cancel_requested:0,source_resolution_state:1,error_code:null,error_details_json:null},
    after:{status:4,cancel_requested:0,source_resolution_state:3,error_code:10,error_details_json:{}}
  }));
});

test('没有预建流程时可以与首次尝试直接建立活动流程', async () => {
  const f = await fixture();
  const after = Object.fromEntries([...f.registry.tables.operation_runs.immutable,...f.registry.tables.operation_runs.mutable].map(c=>[c,null]));
  Object.assign(after,{action_id:1,kind:1,responsibility_key:'start/1',activity_id:1,status:2,attempts_used:1,max_attempts_used:2,retry_wait_required:0});
  assert.doesNotThrow(() => f.validateRowChange(f.registry,'ATTEMPT_STARTED.NEW',{table:'operation_runs',before:null,after}));
});

test('首次创建清理项可以直接保存已有完成结果', async () => {
  const f = await fixture();
  assert.doesNotThrow(() => f.validateRowChange(f.registry,'CLEANUP_CHANGED.SUCCEED',{
    table:'cleanup_items',before:null,after:{action_id:1,requested_output_id:2,output_id:2,status:4,restriction_state:4,outcome:2,final_event_id:3,error_code:null,error_details_json:null}
  }));
});

for (const [state,dispatch,accepted] of [[2,1,true],[3,1,false],[2,2,false],[2,3,false]]) test(`空基准固定：原状态${state}，发送状态${dispatch}`, async () => {
  const f=await fixture();
  const before={ownership_mode:2,baseline_state:state,dispatch_state:dispatch,baseline_first_event_id:40,baseline_last_event_id:41};
  const after={...before,baseline_state:3,baseline_first_event_id:null,baseline_last_event_id:null};
  const run=()=>f.validateRowChange(f.registry,'BASELINE_CHUNK.FIX_EMPTY',{table:'device_activities',before,after});
  if(accepted) assert.doesNotThrow(run); else assert.throws(run);
});

for (const decision of [1,2,3]) test(`检查决定${decision}不阻止首次可靠原片关联`,async()=>{
  const f=await fixture();
  const before={check_decision:decision,check_state:1,source_device_file_id:null};
  assert.doesNotThrow(()=>f.validateRowChange(f.registry,'RECORDING_DECIDED.SOURCE',{table:'recording_processing',before,after:{...before,source_device_file_id:7}}));
});

test('原片已经绑定后不能更换',async()=>{
  const f=await fixture();
  assert.throws(()=>f.validateRowChange(f.registry,'RECORDING_DECIDED.SOURCE',{
    table:'recording_processing',before:{source_device_file_id:7},after:{source_device_file_id:8}
  }));
});

for (const [key,before,after,guard] of [
  ['CLEANUP_CHANGED.SUCCEED',{status:3,restriction_state:4,outcome:null,final_event_id:null},{status:4,restriction_state:4,outcome:1,final_event_id:10},'cleanup_member'],
  ['CLEANUP_CHANGED.CANCEL',{status:3,restriction_state:4,outcome:null,final_event_id:null,error_code:null,error_details_json:null},{status:6,restriction_state:4,outcome:null,final_event_id:10,error_code:4,error_details_json:{output_id:'7'}},'cleanup_member'],
  ['READ_PERMISSION_CHANGED.RESOLVE',{status:1,basis:5,output_id:null},{status:2,basis:5,output_id:7},'obtain_member'],
  ['READ_PERMISSION_CHANGED.REJECT',{status:1,error_code:null,error_details_json:null},{status:4,error_code:1,error_details_json:{}},'obtain_member'],
]) test(`${key} 使用已有成员的校验职责`,async()=>{
  const f=await fixture(),table=key.startsWith('CLEANUP')?'cleanup_items':'obtain_items';
  const result=f.validateRowChange(f.registry,key,{table,before,after});
  assert(result.pendingGuards.includes(guard));
  assert(!result.pendingGuards.includes('target_set'));
  assert(!result.pendingGuards.includes('source_selection'));
});

test('状态不变时仍能保存其他真实变化',async()=>{
  const f=await fixture();
  assert.doesNotThrow(()=>f.validateRowChange(f.registry,'DEVICE_OBSERVED.OBSERVE',{
    table:'device_activities',before:{activity_state:2,last_error_json:null},after:{activity_state:2,last_error_json:{code:'unconfirmed'}}
  }));
});

test('实际历史 SQL 接受全部登记类型并拒绝未登记类型',async()=>{
  const f=await fixture(),db=new DatabaseSync(':memory:');
  try {
    for(const name of readdirSync(new URL('schema/',directory)).filter(n=>n.endsWith('.sql')).sort()) db.exec(readFileSync(new URL(`schema/${name}`,directory),'utf8'));
    const types=Object.values(f.registry.events).map(e=>e.id);
    db.exec('BEGIN');
    db.prepare('INSERT INTO history_transactions(id,operation_key,first_event_id,last_event_id) VALUES(1,?,1,?)').run('a'.repeat(32),types.length);
    const insert=db.prepare("INSERT INTO history_events(id,transaction_id,event_type,event_version,occurred_at,clock_status,body_json) VALUES(?,1,?,1,0,1,'{}')");
    for(const [i,type] of types.entries()) insert.run(i+1,type);
    db.exec('COMMIT');
    assert.throws(()=>insert.run(types.length+1,Math.max(...types)+1));
    assert.equal(db.prepare('SELECT count(*) AS n FROM history_events').get().n,types.length);
  } finally { db.close(); }
});
