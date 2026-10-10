export type PointerRead =
  | { found: false }
  | { found: true; parent: object; key: string; value: unknown };
/** 编辑资料允许空根路径；其他路径只解码 JSON Pointer 的标准转义。 */
export function decodeJsonPointer(pointer: string): string[] {
  if (pointer === "") return [];
  if (!pointer.startsWith("/") || /~(?:[^01]|$)/.test(pointer))
    throw new Error("输入路径不是有效的 JSON Pointer，请保留原文核对");
  return pointer
    .slice(1)
    .split("/")
    .map((part) => part.replace(/~1/g, "/").replace(/~0/g, "~"));
}
/** 估算路径必须指向成员；返回父容器以供数字原始词元检查。 */
export function readJsonPointer(root: unknown, pointer: string): PointerRead {
  if (pointer === "") throw new Error("估算路径不能指向空根路径");
  const segments = decodeJsonPointer(pointer);
  let current: unknown = root;
  for (const [index, key] of segments.entries()) {
    if (current === null || typeof current !== "object")
      return { found: false };
    if (
      Array.isArray(current) &&
      (!/^(0|[1-9][0-9]*)$/.test(key) ||
        !Number.isSafeInteger(Number(key)) ||
        Number(key) >= current.length)
    )
      return { found: false };
    if (!Object.hasOwn(current, key)) return { found: false };
    const value: unknown = Reflect.get(current, key);
    if (index === segments.length - 1)
      return { found: true, parent: current, key, value };
    current = value;
  }
  return { found: false };
}
