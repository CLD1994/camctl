import { visit } from 'jsonc-parser';

/** 完整 JSON 解析；在标准解析前检查解码后的对象成员是否重复。 */
export function parseJson(text: string): unknown {
  const scopes: Set<string>[] = [];
  let problem: string | undefined;
  visit(text, {
    onObjectBegin: () => { scopes.push(new Set()); },
    onObjectEnd: () => { scopes.pop(); },
    onObjectProperty: (name, offset) => {
      const scope = scopes.at(-1)!;
      if (scope.has(name)) problem = `位置 ${offset} 存在重复成员 ${name}`;
      scope.add(name);
    },
    onLiteralValue: (value, offset) => { if (typeof value === 'number' && !Number.isFinite(value)) problem = `位置 ${offset} 的数字不是有限值`; },
    onError: (_error, offset) => { problem = `位置 ${offset} 的 JSON 语法错误`; },
  }, { disallowComments: true, allowTrailingComma: false, allowEmptyContent: false });
  if (problem) throw new Error(problem);
  return JSON.parse(text) as unknown;
}
