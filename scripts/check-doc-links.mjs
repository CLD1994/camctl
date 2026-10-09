import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join, resolve, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
// Git 的文件范围包含已跟踪文档和待加入的新文档，忽略构建中间产物。
const markdown = [...new Set(execFileSync('git', [
  'ls-files', '--cached', '--others', '--exclude-standard', '-z', '--', '*.md',
], { cwd: root, encoding: 'utf8' }).split('\0').filter(Boolean))]
  .map(file => join(root, file)).filter(existsSync);
let checked = 0;
const failures = [];
for (const file of markdown) {
  const text = readFileSync(file, 'utf8').replace(/```[\s\S]*?```/g, '');
  for (const match of text.matchAll(/\]\((?:<([^>]+)>|([^\s)]+))(?:\s+"[^"]*")?\)/g)) {
    const url = match[1] ?? match[2];
    if (/^(?:[a-z][a-z\d+.-]*:|#|\/)/i.test(url)) continue;
    const target = decodeURIComponent(url.split('#')[0]);
    if (!target) continue;
    checked++;
    if (!existsSync(resolve(dirname(file), target))) failures.push(`${relative(root, file)}: ${url}`);
  }
}
if (failures.length) {
  console.error(failures.join('\n'));
  process.exitCode = 1;
} else console.log(`文档本地文件链接检查通过：${checked} 个链接（不校验标题锚点）。`);
