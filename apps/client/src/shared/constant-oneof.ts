import {
  cloneClientJson,
  exactJsonValueIdentity,
  originalNumberToken,
  rememberNumberToken,
} from "./json";
import { SCHEMA_ANNOTATIONS, visitSchemaNodes } from "./schema-nodes";
import { isLocalScalarAssertions } from "./scalar-schema";

const annotations = new Set<string>(SCHEMA_ANNOTATIONS);
/** 按各常量满足自身局部断言的数量生成等价交集，原 Schema 保持。 */
export function compactConstantOneOf(
  schema: unknown,
  branchMatches: (branch: Record<string, unknown>) => boolean,
): unknown {
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
        (isLocalScalarAssertions(branch) ||
          Object.keys(branch).every(
            (key) => key === "const" || annotations.has(key),
          )),
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
    const matches = branches.map(branchMatches);
    identities.forEach((identity, index) => {
      if (matches[index]) counts.set(identity, (counts.get(identity) ?? 0) + 1);
    });
    const values: unknown[] = [];
    branches.forEach((branch, index) => {
      // 同一精确身份恰好一个分支满足自身断言时，才保留该值。
      if (!matches[index] || counts.get(identities[index]) !== 1) return;
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
