import { visit, parseTree, findNodeAtLocation, type Node } from "jsonc-parser";

type JsonPath = Array<string | number>;
export const MOTOR_ORIGINAL_INPUT_FIELDS = [
  "input_params",
  "policy",
  "device_id",
  "scheduled_at",
  "group",
  "extra_input_fields",
] as const;
function mathematicalInteger(token: string): boolean {
  const match = /^-?(\d+)(?:\.(\d+))?(?:[eE]([+-]?\d+))?$/.exec(token)!;
  const digits = match[1] + (match[2] ?? "");
  if (!/[1-9]/.test(digits)) return true;
  const trailingZeros = digits.length - digits.replace(/0+$/, "").length;
  const fractionalPlaces =
    BigInt((match[2] ?? "").length) - BigInt(match[3] ?? "0");
  return fractionalPlaces <= BigInt(trailingZeros);
}

/** 只保护电机整数契约；其他参数继续使用既有数值解析。 */
function motorIntegerPaths(root: Node): JsonPath[] {
  const paths: JsonPath[] = [];
  const actions = (base: JsonPath, report = false) => {
    const list = findNodeAtLocation(root, [...base, "actions"]);
    if (list?.type !== "array") return;
    list.children?.forEach((_action, index) => {
      const path = [...base, "actions", index];
      if (
        findNodeAtLocation(root, [...path, "type"])?.value !== "motor_control"
      )
        return;
      if (
        report &&
        findNodeAtLocation(root, [...path, "status"])?.value === "failed" &&
        findNodeAtLocation(root, [...path, "error", "stage"])?.value ===
          "admission"
      )
        return;
      for (const field of ["params", "input_params"])
        paths.push([...path, field, "position"]);
      paths.push([...path, "policy", "max_delay_ms"]);
    });
  };
  actions([]);
  const plans = findNodeAtLocation(root, ["plans"]);
  if (plans?.type === "array")
    plans.children?.forEach((_plan, index) => actions(["plans", index], true));
  return paths;
}

function decimalIdentity(token: string): string {
  const match = /^(-?)(\d+)(?:\.(\d+))?(?:[eE]([+-]?\d+))?$/.exec(token)!;
  const coefficient = (match[2] + (match[3] ?? "")).replace(/^0+/, "");
  if (!coefficient) return "0";
  const significant = coefficient.replace(/0+$/, "");
  const exponent =
    BigInt(match[4] ?? "0") -
    BigInt((match[3] ?? "").length) +
    BigInt(coefficient.length - significant.length);
  return `${match[1]}${significant}e${exponent}`;
}
export interface MotorInputText {
  text?: string;
  inputIdentity: string;
}
type RawNumber = { readonly rawJSON: string };
const rawJson = JSON as typeof JSON & {
  rawJSON(text: string): RawNumber;
  isRawJSON(value: unknown): value is RawNumber;
};
export const isRawNumber = (value: unknown): value is RawNumber =>
  rawJson.isRawJSON(value);
/** 身份表达 JSON 类型、数组次序和精确数学值；对象成员顺序不参与身份。 */
export function exactJsonIdentity(value: unknown, present = true): string {
  const identity = (v: unknown): unknown => {
    if (isRawNumber(v)) return ["number", decimalIdentity(v.rawJSON)];
    if (v === null) return ["null"];
    if (typeof v === "number") return ["number", decimalIdentity(String(v))];
    if (typeof v === "string" || typeof v === "boolean") return [typeof v, v];
    if (Array.isArray(v)) return ["array", v.map(identity)];
    if (v !== null && typeof v === "object")
      return [
        "object",
        Object.keys(v)
          .sort()
          .map((key) => [key, identity(Reflect.get(v, key))]),
      ];
    throw new Error("值不属于 JSON 类型");
  };
  return JSON.stringify(present ? identity(value) : ["missing"]);
}
function exactNodeValue(
  node: Node,
  text: string,
  preserved = false,
  roots = new Set<Node>(),
): unknown {
  preserved ||= roots.has(node);
  if (node.type === "number") {
    if (preserved)
      return rawJson.rawJSON(
        text.slice(node.offset, node.offset + node.length),
      );
    if (!Number.isFinite(node.value))
      throw new Error(`位置 ${node.offset} 的数字超出当前字段的数值表示范围`);
    return node.value;
  }
  if (node.type === "array")
    return (node.children ?? []).map((child) =>
      exactNodeValue(child, text, preserved, roots),
    );
  if (node.type === "object")
    return Object.fromEntries(
      (node.children ?? []).map((property) => [
        property.children![0].value,
        exactNodeValue(property.children![1], text, preserved, roots),
      ]),
    );
  return node.value;
}
function admissionRoots(root: Node): Set<Node> {
  const roots = new Set<Node>();
  for (const base of [[], ["snapshot"]]) {
    const plans = findNodeAtLocation(root, [...base, "plans"]);
    if (plans?.type !== "array") continue;
    plans.children?.forEach((_plan, index) => {
      const actions = findNodeAtLocation(root, [
        ...base,
        "plans",
        index,
        "actions",
      ]);
      if (actions?.type !== "array") return;
      actions.children?.forEach((_action, actionIndex) => {
        const path = [...base, "plans", index, "actions", actionIndex];
        if (
          findNodeAtLocation(root, [...path, "type"])?.value !==
            "motor_control" ||
          findNodeAtLocation(root, [...path, "status"])?.value !== "failed" ||
          findNodeAtLocation(root, [...path, "error", "stage"])?.value !==
            "admission"
        )
          return;
        for (const field of MOTOR_ORIGINAL_INPUT_FIELDS) {
          const value = findNodeAtLocation(root, [...path, field]);
          if (value) roots.add(value);
        }
      });
    });
  }
  return roots;
}
/** 从报告原字节派生展示信息；序列化时保留 JSON 数字类型。 */
export function motorInputTexts(text: string): Record<string, MotorInputText> {
  const root = parseTree(text);
  const result: Record<string, MotorInputText> = {};
  if (!root) return result;
  const plans = findNodeAtLocation(root, ["plans"]);
  if (plans?.type !== "array") return result;
  plans.children?.forEach((_plan, index) => {
    const actions = findNodeAtLocation(root, ["plans", index, "actions"]);
    actions?.children?.forEach((_action, actionIndex) => {
      const path = ["plans", index, "actions", actionIndex];
      if (
        findNodeAtLocation(root, [...path, "type"])?.value !== "motor_control"
      )
        return;
      const id = findNodeAtLocation(root, [
        ...path,
        "action_instance_id",
      ])?.value;
      const params = findNodeAtLocation(root, [...path, "input_params"]);
      if (typeof id !== "string") return;
      if (!params) {
        result[id] = { inputIdentity: exactJsonIdentity(undefined, false) };
        return;
      }
      result[id] = {
        text: text.slice(params.offset, params.offset + params.length),
        inputIdentity: exactJsonIdentity(exactNodeValue(params, text, true)),
      };
    });
  });
  return result;
}

/** 完整 JSON 解析；在标准解析前检查解码后的对象成员是否重复。 */
export function parseJson(
  text: string,
  integerPaths: JsonPath[] = [],
): unknown {
  const scopes: Set<string>[] = [];
  let problem: string | undefined;
  visit(
    text,
    {
      onObjectBegin: () => {
        scopes.push(new Set());
      },
      onObjectEnd: () => {
        scopes.pop();
      },
      onObjectProperty: (name, offset) => {
        const scope = scopes.at(-1)!;
        if (scope.has(name)) problem = `位置 ${offset} 存在重复成员 ${name}`;
        scope.add(name);
      },
      onError: (_error, offset) => {
        problem = `位置 ${offset} 的 JSON 语法错误`;
      },
    },
    {
      disallowComments: true,
      allowTrailingComma: false,
      allowEmptyContent: false,
    },
  );
  if (problem) throw new Error(problem);
  const root = parseTree(text);
  if (root)
    for (const path of [...motorIntegerPaths(root), ...integerPaths]) {
      const node = findNodeAtLocation(root, path);
      if (
        node?.type === "number" &&
        Number.isInteger(node.value) &&
        !mathematicalInteger(text.slice(node.offset, node.offset + node.length))
      )
        throw new Error(
          `位置 ${node.offset} 的电机参数数学值不是整数，不能按浮点舍入为整数`,
        );
    }
  if (!root) throw new Error("JSON 为空");
  return exactNodeValue(root, text, false, admissionRoots(root));
}
/** 原始数字记录不能 structuredClone；JSON 往返恢复同一范围的精确表示。 */
export function cloneProtocolJson<T>(value: T): T {
  return parseJson(JSON.stringify(value)) as T;
}
