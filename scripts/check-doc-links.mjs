import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { dirname, join, resolve, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const excluded = new Set(['.git', '.local', '.superpowers', 'node_modules', 'dist', 'data', 'tmp', 'test-results', 'playwright-report']);
function markdown(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap(entry => {
    if (excluded.has(entry.name)) return [];
    const file = join(directory, entry.name);
    return entry.isDirectory() ? markdown(file) : entry.name.endsWith('.md') ? [file] : [];
  });
}
let checked = 0;
const failures = [];
for (const file of markdown(root)) {
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
