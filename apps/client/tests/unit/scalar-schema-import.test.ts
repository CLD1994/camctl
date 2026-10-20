import { expect, it, vi } from "vitest";

it("共同标量资格可先于校验器独立导入", async () => {
  vi.resetModules();
  const { isLocalScalarSchema } =
    await import("../../src/shared/scalar-schema");
  expect(
    isLocalScalarSchema({
      type: "string",
      oneOf: [{ const: "value", type: "string" }],
    }),
  ).toBe(true);
});
it("Schema 节点遍历可先于校验器独立导入", async () => {
  vi.resetModules();
  const { visitSchemaNodes } = await import("../../src/shared/schema-nodes");
  const nodes: unknown[] = [];
  visitSchemaNodes(
    { type: "object", properties: { value: { type: "string" } } },
    (node) => nodes.push(node.type),
  );
  expect(nodes).toEqual(["object", "string"]);
});
