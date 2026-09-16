import { isCameraAction } from "./actions";
import type { ValidateFunction } from "ajv";
import type { Capabilities, Issue, ParameterType } from "./types";
import { createValidator, DIALECT, isObject, schemaIssues } from "./validation";

const compiled = new WeakMap<object, ValidateFunction>();
function exact(
  value: unknown,
  fields: string[],
  path: string,
): asserts value is Record<string, unknown> {
  if (
    !isObject(value) ||
    fields.some((f) => !Object.hasOwn(value, f)) ||
    Object.keys(value).some((f) => !fields.includes(f))
  )
    throw new Error(`${path} 字段不完整或包含未知字段`);
}
function nonempty(value: unknown, path: string): asserts value is string {
  if (typeof value !== "string" || !value.length)
    throw new Error(`${path} 必须是非空字符串`);
}
function unique(values: Set<string>, value: string, path: string) {
  if (values.has(value)) throw new Error(`${path} 标识重复：${value}`);
  values.add(value);
}
function validateSchemaResources(
  schema: Record<string, unknown>,
  validator: ReturnType<typeof createValidator>,
  root = true,
) {
  // 使用校验器已注册词汇，包含仅作说明的标准关键词，不另维护完整清单。
  for (const keyword of Object.keys(schema))
    if (!Object.hasOwn(validator.RULES.keywords, keyword))
      throw new Error(`Schema 关键词不支持：${keyword}`);
  if (
    typeof schema.format === "string" &&
    !Object.hasOwn(validator.formats, schema.format)
  )
    throw new Error(`Schema 格式不支持：${schema.format}`);
  if ((root || Object.hasOwn(schema, "$id")) && schema.$schema !== DIALECT)
    throw new Error("Schema 资源必须声明 Draft 2020-12");
  if (Object.hasOwn(schema, "$schema") && schema.$schema !== DIALECT)
    throw new Error("Schema 版本不支持");
  const single = [
    "$defs",
    "properties",
    "patternProperties",
    "dependentSchemas",
  ];
  for (const key of single)
    if (isObject(schema[key]))
      for (const child of Object.values(schema[key]))
        if (isObject(child)) validateSchemaResources(child, validator, false);
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
    if (isObject(schema[key]))
      validateSchemaResources(schema[key], validator, false);
  for (const key of ["allOf", "anyOf", "oneOf", "prefixItems"])
    if (Array.isArray(schema[key]))
      for (const child of schema[key])
        if (isObject(child)) validateSchemaResources(child, validator, false);
}
function compile(parameter: ParameterType): ValidateFunction {
  const validator = createValidator();
  validateSchemaResources(parameter.schema, validator);
  const s = parameter.schema;
  if (
    s.type !== "object" ||
    !Array.isArray(s.required) ||
    !s.required.includes("type") ||
    !isObject(s.properties) ||
    !isObject(s.properties.type) ||
    s.properties.type.const !== parameter.type
  )
    throw new Error("参数类型与 Schema 根约束不一致");
  const validate = validator.compile(s);
  compiled.set(parameter, validate);
  return validate;
}
export function loadCapabilities(value: unknown): Capabilities {
  value = structuredClone(value);
  exact(value, ["devices"], "能力说明");
  if (!Array.isArray(value.devices)) throw new Error("devices 必须是数组");
  const deviceIds = new Set<string>();
  for (const device of value.devices) {
    exact(device, ["device_id", "driver_id", "actions"], "设备");
    nonempty(device.device_id, "device_id");
    nonempty(device.driver_id, "driver_id");
    unique(deviceIds, device.device_id, "设备");
    if (!Array.isArray(device.actions)) throw new Error("actions 必须是数组");
    const actions = new Set<string>();
    for (const action of device.actions) {
      exact(action, ["type", "parameter_types"], "拍摄动作");
      nonempty(action.type, "动作 type");
      unique(actions, action.type, "动作");
      if (!isCameraAction(action.type))
        throw new Error(`本版不支持拍摄动作 ${action.type}`);
      if (
        !Array.isArray(action.parameter_types) ||
        !action.parameter_types.length
      )
        throw new Error("parameter_types 必须非空");
      const types = new Set<string>();
      for (const parameter of action.parameter_types) {
        exact(parameter, ["type", "name", "description", "schema"], "参数类型");
        for (const field of ["type", "name", "description"])
          nonempty(parameter[field], field);
        unique(types, parameter.type as string, "参数类型");
        if (!isObject(parameter.schema)) throw new Error("schema 必须是对象");
        compile(parameter as unknown as ParameterType);
      }
    }
  }
  return value as unknown as Capabilities;
}
export function validateParams(
  deviceId: string,
  actionType: string,
  params: unknown,
  capabilities: Capabilities | null,
): Issue[] {
  const fail = (path: string, code: string, message: string): Issue[] => [
    { path, code, message },
  ];
  if (!capabilities)
    return fail("params", "capabilities_unavailable", "设备说明不可用");
  const device = capabilities.devices.find((d) => d.device_id === deviceId);
  if (!device)
    return fail(
      "device_id",
      "device_not_supported",
      `设备说明中没有 ${deviceId}`,
    );
  const action = device.actions.find((a) => a.type === actionType);
  if (!action)
    return fail("type", "action_not_supported", `设备不支持 ${actionType}`);
  if (!isObject(params))
    return fail("params", "params_invalid", "拍摄参数必须是对象");
  const parameter = action.parameter_types.find((p) => p.type === params.type);
  if (!parameter)
    return fail(
      "params.type",
      "parameter_type_not_supported",
      "参数类型缺失或不受支持",
    );
  const validate = compiled.get(parameter) ?? compile(parameter);
  return validate(params) ? [] : schemaIssues(validate.errors, "params");
}
