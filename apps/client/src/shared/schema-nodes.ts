import { isObject } from "./validation";

/** 已知说明注释只属于 Schema 节点，业务值中的同名成员仍是数据。 */
export const SCHEMA_ANNOTATIONS = [
  "title",
  "description",
  "$comment",
  "default",
  "examples",
  "readOnly",
  "writeOnly",
  "deprecated",
] as const;

/** 只访问实际 Schema 节点；属性与定义的名称、enum/const 数据不是节点。 */
export function visitSchemaNodes(
  schema: Record<string, unknown>,
  visit: (node: Record<string, unknown>, root: boolean) => void,
  root = true,
): void {
  visit(schema, root);
  for (const key of [
    "$defs",
    "properties",
    "patternProperties",
    "dependentSchemas",
  ])
    if (isObject(schema[key]))
      for (const child of Object.values(schema[key]))
        if (isObject(child)) visitSchemaNodes(child, visit, false);
  for (const key of [
    "items",
    "additionalProperties",
    "unevaluatedProperties",
    "contains",
    "not",
    "if",
    "then",
    "else",
    "propertyNames",
    "unevaluatedItems",
    "contentSchema",
  ])
    if (isObject(schema[key])) visitSchemaNodes(schema[key], visit, false);
  for (const key of ["allOf", "anyOf", "oneOf", "prefixItems"])
    if (Array.isArray(schema[key]))
      for (const child of schema[key])
        if (isObject(child)) visitSchemaNodes(child, visit, false);
}
