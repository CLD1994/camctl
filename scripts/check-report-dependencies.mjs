import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';
import { validateRegistration } from './report-dependencies.mjs';

const directory = new URL('../docs/camctl/database/', import.meta.url);
const json = url => JSON.parse(readFileSync(url, 'utf8'));
const registry = json(new URL('report-dependencies.json', directory));
const schema = json(new URL(registry.public_schema, directory));
const enums = json(new URL(registry.enum_registry, directory));
const errors = json(new URL(registry.error_registry, directory));
const db = new DatabaseSync(':memory:');
try {
  for (const file of readdirSync(new URL('schema/', directory)).sort()) {
    if (file.endsWith('.sql')) db.exec(readFileSync(new URL(`schema/${file}`, directory), 'utf8'));
  }
  const tables = Object.fromEntries(db.prepare("SELECT name FROM sqlite_schema WHERE type = 'table'").all()
    .map(({ name }) => [name, db.prepare(`PRAGMA table_info(${name})`).all().map(column => column.name)]));
  const foreignKeys = Object.fromEntries(Object.keys(tables).flatMap(table => db.prepare(`PRAGMA foreign_key_list(${table})`).all()
    .map(key => [`${table}.${key.from}`, `${key.table}.${key.to}`])));
  const result = validateRegistration(registry, schema, { enums, errors, tables, foreignKeys });
  for (const [table, pending] of Object.entries(registry.sql_pending)) {
    const document = readFileSync(new URL(pending.specification.split('#')[0], directory), 'utf8');
    for (const column of pending.columns) assert(
      document.includes(`\`${table}.${column}\``) || document.includes(`\`${column}\``),
      `SQL 待同步列缺少字段规格依据：${table}.${column}`
    );
  }
  console.log(`报告依赖登记检查通过：${result.projections} 个投影，${result.fields} 条字段映射，${result.columns.length} 个来源及关联列。`);
  console.log(result.pendingSql.length ? `SQL 待同步（不计为已实现）：${result.pendingSql.join('、')}。` : '全部登记来源列及关联已在实际 SQL 中核对，无待同步项。');
  console.log('边界：验证机器登记与公共结构的接缝；未运行生产事件应用、历史恢复或报告生成。');
} finally {
  db.close();
}
