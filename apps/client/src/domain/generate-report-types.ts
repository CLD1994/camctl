/** 执行 npx tsx src/domain/generate-report-types.ts，从公共 Schema 刷新报告类型。 */
import { compileFromFile } from 'json-schema-to-typescript';
import { writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';

const root = resolve(import.meta.dirname, '../..');
const source = resolve(root, '../../protocol/schemas/status-report.schema.json');
const target = resolve(root, 'src/shared/status-report.generated.ts');
const declaration = await compileFromFile(source, {
  bannerComment: '/* 从公共 status-report.schema.json 生成；请勿手工修改。 */',
  unreachableDefinitions: true,
  unknownAny: true,
});
await writeFile(target, declaration, 'utf8');
