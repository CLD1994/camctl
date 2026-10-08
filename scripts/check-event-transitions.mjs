import assert from 'node:assert/strict';
import { readFileSync, writeFileSync, readdirSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';
import { validateEventRegistration, generatedEventIndex, generatedEventSql } from './event-transitions.mjs';
import { validateRegistration } from './report-dependencies.mjs';

assert(process.argv.slice(2).every(arg => arg === '--write'), '只接受 --write，默认只检查');
const write = process.argv.includes('--write');
const directory = new URL('../docs/camctl/database/', import.meta.url);
const json = path => JSON.parse(readFileSync(new URL(path,directory),'utf8'));
const registry = json('event-transitions.json');
const reports = json(registry.report_dependencies), enums = json('enum-registry.json');
const schemaDirectory = new URL('../protocol/schemas/',import.meta.url);
const schemas = Object.fromEntries(readdirSync(schemaDirectory)
  .filter(name => name.endsWith('.schema.json') && name !== 'status-report.schema.json')
  .map(name => [name, JSON.parse(readFileSync(new URL(name,schemaDirectory),'utf8'))]));
const db = new DatabaseSync(':memory:');
try {
  for (const file of readdirSync(new URL('schema/',directory)).filter(f=>f.endsWith('.sql')).sort()) db.exec(readFileSync(new URL(`schema/${file}`,directory),'utf8'));
  const tables = Object.fromEntries(db.prepare("SELECT name FROM sqlite_schema WHERE type='table'").all()
    .map(({name})=>[name,db.prepare(`PRAGMA table_xinfo(${name})`).all().map(c=>c.name)]));
  const foreignKeys = Object.fromEntries(Object.keys(tables).flatMap(table=>db.prepare(`PRAGMA foreign_key_list(${table})`).all().map(k=>[`${table}.${k.from}`,`${k.table}.${k.to}`])));
  const context = {tables,foreignKeys,enums,reports};
  const result = validateEventRegistration(registry,context);
  const reportResult = validateRegistration(reports,json(reports.public_schema),{...context,errors:json(reports.error_registry),schemas});
  assert.deepEqual(result.reportColumns,reportResult.columns.sort(),'报告用途分析与字段登记的实际来源不一致');
  for (const item of [{contract: registry.history_contract},...Object.values(registry.guards)]) {
    const [file] = item.contract.split('#'); readFileSync(new URL(file,directory),'utf8');
  }
  for (const [file,start,end,body] of [
    ['history-formats.md','<!-- 事件登记生成开始 -->','<!-- 事件登记生成结束 -->',generatedEventIndex(registry)],
    ['schema/history.sql','-- 事件登记生成开始','-- 事件登记生成结束',generatedEventSql(registry)],
  ]) {
    const url = new URL(file,directory), original = readFileSync(url,'utf8');
    assert.equal(original.split(start).length,2,`生成起点缺失或重复：${file}`);
    assert.equal(original.split(end).length,2,`生成终点缺失或重复：${file}`);
    const from = original.indexOf(start), to = original.indexOf(end);
    assert(to > from,`生成区段顺序错误：${file}`);
    const next = original.slice(0,from) + `${start}\n${body}\n${end}` + original.slice(to+end.length);
    if (write) { if(next !== original) writeFileSync(url,next); }
    else assert.equal(original,next,`事件生成区段需要同步：${file}；运行本脚本 --write`);
  }
  console.log(`事件登记检查通过：${result.events} 类事件、${result.branches} 个分支、${Object.keys(registry.tables).length} 张业务表、${result.columns} 列、${result.states} 个状态模型。`);
  console.log(`报告依赖来源 ${result.reportColumns.length} 列；事件列分类与实际 SQL 一致。`);
  console.log(`历史字段规则：${result.businessReferences} 个业务引用、${result.derivedHistory} 个派生列、${result.structuralReferences} 个基础表引用；已核对分类、计算方式及实际 SQL 外键。`);
  console.log('边界：检查规则文件、行权限与状态覆盖；具体引用值、具名业务校验、生产事务和完整历史恢复尚未执行。');
} finally { db.close(); }
