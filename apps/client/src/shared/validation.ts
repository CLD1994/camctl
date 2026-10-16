import Ajv2020 from "ajv/dist/2020";
import addFormats from "ajv-formats";
import type { ErrorObject } from "ajv";
import type { Issue } from "./types";
import {
  compareJsonNumberTokens,
  jsonNumberIsMultipleOf,
  mathematicalInteger,
  originalNumberToken,
  rememberNumberToken,
  cloneClientJson,
  exactJsonValueIdentity,
} from "./json";

export const DIALECT = "https://json-schema.org/draft/2020-12/schema";
export const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
export const isId = (value: unknown): value is string =>
  typeof value === "string" &&
  /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(value) &&
  !/[\r\n]/.test(value);
export const isCanonicalId = (value: unknown): value is string =>
  typeof value === "string" && /^[1-9][0-9]*$/.test(value);
export const isUint = (value: unknown): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
export const isPositive = (value: unknown): value is number =>
  isUint(value) && value > 0;
export const isName = (value: unknown): value is string =>
  typeof value === "string" &&
  [...value].length >= 1 &&
  [...value].length <= 128 &&
  !/^[\p{White_Space}\uFEFF]|[\p{White_Space}\uFEFF]$/u.test(value) &&
  !/[\u0000-\u001f\u007f-\u009f]/.test(value);
export function isTimestamp(value: unknown): value is string {
  if (
    typeof value !== "string" ||
    !/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(value) ||
    /[\r\n]/.test(value)
  )
    return false;
  const [year, month, day, hour, minute, second] = value
    .slice(0, 19)
    .split(/[- :]/)
    .map(Number);
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  return (
    year >= 1 &&
    month >= 1 &&
    month <= 12 &&
    day >= 1 &&
    day <=
      [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1] &&
    hour <= 23 &&
    minute <= 59 &&
    second <= 59
  );
}
export function createValidator() {
  const ajv = new Ajv2020({
    strict: true,
    strictSchema: false,
    strictRequired: false,
    strictTypes: false,
    strictTuples: false,
    allowMatchingProperties: true,
    allErrors: true,
    coerceTypes: false,
    removeAdditional: false,
    useDefaults: false,
    passContext: true,
  });
  // Ajv 继续负责原生类型与 Schema 分支；此关键词只追加原词元整数性。
  ajv.removeKeyword("type");
  ajv.addKeyword({
    keyword: "type",
    schemaType: ["string", "array"],
    errors: false,
    validate(
      this: { numberToken?: string } | undefined,
      schema: string | string[],
      data: unknown,
      _parentSchema: unknown,
      context?: { parentData?: unknown; parentDataProperty?: string | number },
    ) {
      const types = Array.isArray(schema) ? schema : [schema];
      if (
        typeof data !== "number" ||
        !Number.isInteger(data) ||
        !types.includes("integer") ||
        types.includes("number")
      )
        return true;
      const token =
        context?.parentData === undefined
          ? this?.numberToken
          : originalNumberToken(
              context.parentData,
              context.parentDataProperty!,
              data,
            );
      return token === undefined || mathematicalInteger(token);
    },
  });
  // 数值范围消费输入与规则的原词元；浏览器和后端使用相同的精确判定。
  for (const [keyword, comparison] of [
    ["minimum", ">="],
    ["maximum", "<="],
    ["exclusiveMinimum", ">"],
    ["exclusiveMaximum", "<"],
  ] as const) {
    ajv.removeKeyword(keyword);
    const check = Object.assign(
      function (
        this: { numberToken?: string } | undefined,
        limit: number,
        data: number,
        parentSchema?: Record<string, unknown>,
        context?: {
          parentData?: unknown;
          parentDataProperty?: string | number;
        },
      ) {
        const dataToken =
          context?.parentData === undefined
            ? this?.numberToken
            : originalNumberToken(
                context.parentData,
                context.parentDataProperty!,
                data,
              );
        const schemaLimit = parentSchema?.[keyword];
        const limitToken =
          originalNumberToken(parentSchema, keyword, schemaLimit) ??
          String(limit);
        // 非有限 Number 仍由 Ajv 的原生数字类型规则拒绝。
        if (!Number.isFinite(data) || !Number.isFinite(limit)) return true;
        const order = compareJsonNumberTokens(
          dataToken ?? String(data),
          limitToken,
        );
        const valid =
          keyword === "minimum"
            ? order >= 0
            : keyword === "maximum"
              ? order <= 0
              : keyword === "exclusiveMinimum"
                ? order > 0
                : order < 0;
        const params = {
          comparison,
          limit: typeof schemaLimit === "number" ? schemaLimit : limit,
        };
        rememberNumberToken(params, "limit", limitToken);
        check.errors = valid
          ? undefined
          : [
              {
                keyword,
                params,
                message: `must be ${comparison} ${limitToken}`,
              },
            ];
        return valid;
      },
      { errors: undefined as Array<Partial<ErrorObject>> | undefined },
    );
    ajv.addKeyword({
      keyword,
      type: "number",
      schemaType: "number",
      errors: true,
      validate: check,
    });
  }
  ajv.removeKeyword("multipleOf");
  const checkMultiple = Object.assign(
    function (
      this: { numberToken?: string } | undefined,
      step: number,
      data: number,
      parentSchema?: Record<string, unknown>,
      context?: { parentData?: unknown; parentDataProperty?: string | number },
    ) {
      const dataToken =
        context?.parentData === undefined
          ? this?.numberToken
          : originalNumberToken(
              context.parentData,
              context.parentDataProperty!,
              data,
            );
      const schemaStep = parentSchema?.multipleOf;
      const stepToken =
        originalNumberToken(parentSchema, "multipleOf", schemaStep) ??
        String(step);
      if (!Number.isFinite(data) || !Number.isFinite(step)) return true;
      const valid = jsonNumberIsMultipleOf(
        dataToken ?? String(data),
        stepToken,
      );
      const params = {
        multipleOf: typeof schemaStep === "number" ? schemaStep : step,
      };
      rememberNumberToken(params, "multipleOf", stepToken);
      checkMultiple.errors = valid
        ? undefined
        : [
            {
              keyword: "multipleOf",
              params,
              message: `must be multiple of ${stepToken}`,
            },
          ];
      return valid;
    },
    { errors: undefined as Array<Partial<ErrorObject>> | undefined },
  );
  ajv.addKeyword({
    keyword: "multipleOf",
    type: "number",
    schemaType: "number",
    errors: true,
    validate: checkMultiple,
  });
  // 关键字的 metaSchema 会立即编译；必须先登记全部精确数值规则。
  // 枚举、常量及条件分支共用 JSON 身份，不按浮点投影比较数字。
  for (const keyword of ["enum", "const"] as const) {
    ajv.removeKeyword(keyword);
    const check = Object.assign(
      function (
        this: { numberToken?: string } | undefined,
        allowed: unknown,
        data: unknown,
        parentSchema?: Record<string, unknown>,
        context?: {
          parentData?: unknown;
          parentDataProperty?: string | number;
        },
      ) {
        const root = { value: data };
        if (typeof data === "number")
          rememberNumberToken(root, "value", this?.numberToken);
        const source =
          context?.parentData === undefined
            ? { parent: root, key: "value" }
            : { parent: context.parentData, key: context.parentDataProperty! };
        const identity =
          data === undefined ||
          (typeof data === "number" && !Number.isFinite(data))
            ? undefined
            : exactJsonValueIdentity(data, true, source);
        const valid =
          identity !== undefined &&
          (keyword === "enum"
            ? (allowed as unknown[]).some(
                (value, index) =>
                  exactJsonValueIdentity(value, true, {
                    parent: allowed,
                    key: index,
                  }) === identity,
              )
            : exactJsonValueIdentity(allowed, true, {
                parent: parentSchema,
                key: keyword,
              }) === identity);
        const param = keyword === "enum" ? "allowedValues" : "allowedValue";
        const params = { [param]: allowed };
        if (keyword === "const")
          rememberNumberToken(
            params,
            param,
            originalNumberToken(parentSchema, keyword, allowed),
          );
        check.errors = valid
          ? undefined
          : [
              {
                keyword,
                params,
                message:
                  keyword === "enum"
                    ? "must be equal to one of the allowed values"
                    : "must be equal to constant",
              },
            ];
        return valid;
      },
      { errors: undefined as Array<Partial<ErrorObject>> | undefined },
    );
    ajv.addKeyword({
      keyword,
      ...(keyword === "enum"
        ? {
            schemaType: "array" as const,
            metaSchema: { type: "array", minItems: 1 },
          }
        : {}),
      errors: true,
      validate: check,
    });
  }
  addFormats(ajv);
  return ajv;
}
export function schemaIssues(
  errors: ErrorObject[] | null | undefined,
  prefix = "",
  pointerPrefix = prefix ? `/${prefix}` : "",
): Issue[] {
  return (errors ?? []).map((error) => {
    const segments = error.instancePath
      .split("/")
      .slice(1)
      .map((s) => s.replace(/~1/g, "/").replace(/~0/g, "~"));
    const missing =
      error.keyword === "required"
        ? error.params.missingProperty
        : error.keyword === "additionalProperties"
          ? error.params.additionalProperty
          : undefined;
    if (typeof missing === "string") segments.push(missing);
    const path = segments.reduce(
      (p, s) => (/^\d+$/.test(s) ? `${p}[${s}]` : p ? `${p}.${s}` : s),
      prefix,
    );
    return {
      path,
      code: `schema_${error.keyword}`,
      message: `${path || "输入"}：${error.message ?? "不符合规则"}`,
      pointer:
        pointerPrefix +
        segments
          .map((s) => `/${s.replace(/~/g, "~0").replace(/\//g, "~1")}`)
          .join(""),
      schema: {
        instancePath: pointerPrefix + error.instancePath,
        schemaPath: error.schemaPath,
        keyword: error.keyword,
        params: cloneClientJson(error.params),
      },
    };
  });
}
