import {
  cloneClientJson,
  exactJsonValueIdentity,
  originalNumberToken,
  rememberNumberToken,
} from "./json";
import { SCHEMA_ANNOTATIONS, visitSchemaNodes } from "./schema-nodes";

const annotations = new Set<string>(SCHEMA_ANNOTATIONS);
/** Ajv 的 oneOf 代码生成逐分支嵌套。大纯常量目录改为等价交集，原规则保持。 */
export function compactConstantOneOf(schema: unknown): unknown {
  if (typeof schema !== "object" || schema === null || Array.isArray(schema))
    return schema;
  let branchReference = false;
  visitSchemaNodes(schema as Record<string, unknown>, (node) => {
    for (const keyword of ["$ref", "$dynamicRef"]) {
      if (typeof node[keyword] !== "string") continue;
      const fragment = node[keyword].split("#")[1];
      if (fragment === undefined) continue;
      try {
        if (decodeURIComponent(fragment).split("/").includes("oneOf"))
          branchReference = true;
      } catch {
        branchReference = true;
      }
    }
  });
  // 引用可能寻址原 oneOf 的成员；不改变无法保持寻址的规则布局。
  if (branchReference) return schema;
  let result: Record<string, unknown> | undefined;
  const eligible = (node: Record<string, unknown>) =>
    Array.isArray(node.oneOf) &&
    node.oneOf.length >= 256 &&
    node.oneOf.every(
      (branch) =>
        typeof branch === "object" &&
        branch !== null &&
        !Array.isArray(branch) &&
        Object.hasOwn(branch, "const") &&
        Object.keys(branch).every(
          (key) => key === "const" || annotations.has(key),
        ),
    );
  visitSchemaNodes(schema as Record<string, unknown>, (node) => {
    if (eligible(node))
      result ??= cloneClientJson(schema as Record<string, unknown>);
  });
  if (!result) return schema;
  visitSchemaNodes(result, (node) => {
    if (!eligible(node)) return;
    const branches = node.oneOf as Record<string, unknown>[];
    const counts = new Map<string, number>();
    const identities = branches.map((branch) =>
      exactJsonValueIdentity(branch.const, true, {
        parent: branch,
        key: "const",
      }),
    );
    for (const identity of identities)
      counts.set(identity, (counts.get(identity) ?? 0) + 1);
    const values: unknown[] = [];
    branches.forEach((branch, index) => {
      // oneOf 恰好满足一次；重复常量因此没有合法值，不能直接并入 enum。
      if (counts.get(identities[index]) !== 1) return;
      values.push(cloneClientJson(branch.const));
      rememberNumberToken(
        values,
        values.length - 1,
        originalNumberToken(branch, "const", branch.const),
      );
    });
    delete node.oneOf;
    const constraint = values.length ? { enum: values } : false;
    node.allOf = [...(Array.isArray(node.allOf) ? node.allOf : []), constraint];
  });
  return result;
}
