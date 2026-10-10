import { cloneClientJson } from "./json";
import { createProtocolValidator } from "./protocol-validation";
import type { ValidateFunction } from "ajv";
import type { Capabilities, Issue, ParameterType } from "./types";
import { createValidator, DIALECT, isObject, schemaIssues } from "./validation";
import { visitSchemaNodes } from "./schema-nodes";

const compiled = new WeakMap<object, ValidateFunction>();
function unique(values: Set<string>, value: string, path: string) {
  if (values.has(value)) throw new Error(`${path} 标识重复：${value}`);
  values.add(value);
}
function validateSchemaResources(
  schema: Record<string, unknown>,
  validator: ReturnType<typeof createValidator>,
) {
  visitSchemaNodes(schema, (schema, root) => {
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
  });
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
  const copy = cloneClientJson(value);
  const validator = createProtocolValidator();
  const check = validator.getSchema("capabilities.schema.json")!;
  if (!check(copy)) throw new Error(validator.errorsText(check.errors));
  const capabilities = copy as Capabilities;
  const deviceIds = new Set<string>();
  for (const device of capabilities.devices) {
    unique(deviceIds, device.device_id, "设备");
    const actions = new Set<string>();
    for (const action of device.actions) {
      unique(actions, action.type, "动作");
      const types = new Set<string>();
      for (const parameter of action.parameter_types) {
        unique(types, parameter.type, "参数类型");
        compile(parameter);
      }
    }
  }
  return capabilities;
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
