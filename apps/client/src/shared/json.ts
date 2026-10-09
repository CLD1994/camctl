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
export function mathematicalInteger(token: string): boolean {
  const match = /^-?(\d+)(?:\.(\d+))?(?:[eE]([+-]?\d+))?$/.exec(token)!;
  const digits = match[1] + (match[2] ?? "");
  if (!/[1-9]/.test(digits)) return true;
  const trailingZeros = digits.length - digits.replace(/0+$/, "").length;
  const fractionalPlaces =
    BigInt((match[2] ?? "").length) - BigInt(match[3] ?? "0");
  return fractionalPlaces <= BigInt(trailingZeros);
}

const numberTokens = new WeakMap<
  object,
  Map<string, { token: string; value: number }>
>();
/** 词元随实际父容器保存；显式编辑同值时也必须清除旧证据。 */
export function rememberNumberToken(
  parent: object,
  key: string | number,
  token?: string,
) {
  let tokens = numberTokens.get(parent);
  if (!tokens) numberTokens.set(parent, (tokens = new Map()));
  if (token === undefined) tokens.delete(String(key));
  else tokens.set(String(key), { token, value: Number(token) });
}
export function originalNumberToken(
  parent: unknown,
  key: string | number,
  value: unknown,
): string | undefined {
  if (typeof parent !== "object" || parent === null) return undefined;
  const saved = numberTokens.get(parent)?.get(String(key));
  return saved && Object.is(saved.value, value) ? saved.token : undefined;
}
export function displayNumber(
  parent: unknown,
  key: string | number,
  value: number,
): string {
  const token = originalNumberToken(parent, key, value);
  return token !== undefined &&
    decimalIdentity(token) !== decimalIdentity(String(value))
    ? token
    : String(value);
}
/** 普通字段仍为 number；仅序列化阶段用原词元避免编辑改写数值事实。 */
export function stringifyJson(value: unknown, space?: number): string {
  return JSON.stringify(
    value,
    function (key, current) {
      const token = originalNumberToken(this, key, current);
      return token === undefined ? current : rawJson.rawJSON(token);
    },
    space,
  );
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
  if (node.type === "array" || node.type === "object") {
    const entries: Array<[string, Node]> = (node.children ?? []).map(
      (child, index) =>
        node.type === "array"
          ? [String(index), child]
          : [child.children![0].value, child.children![1]],
    );
    const result =
      node.type === "array"
        ? entries.map(([, child]) =>
            exactNodeValue(child, text, preserved, roots),
          )
        : Object.fromEntries(
            entries.map(([key, child]) => [
              key,
              exactNodeValue(child, text, preserved, roots),
            ]),
          );
    for (const [key, child] of entries)
      if (child.type === "number" && !preserved && !roots.has(child))
        rememberNumberToken(
          result,
          key,
          text.slice(child.offset, child.offset + child.length),
        );
    return result;
  }
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
  return parseDocument(text, "protocol", integerPaths);
}
/** 私有编辑资料可包含非法或未完成原文；公开正文须另经 parseJson 校验。 */
export function parseClientJson(text: string): unknown {
  return parseDocument(text, "client", []);
}
function parseDocument(
  text: string,
  boundary: "protocol" | "client",
  integerPaths: JsonPath[],
): unknown {
  const scopes: Set<string>[] = [];
  let problem: string | undefined;
  const scalar = (value: string, offset: number) => {
    if (boundary === "protocol" && /[\uD800-\uDFFF]/u.test(value))
      problem = `位置 ${offset} 的字符串含未配对代理码点：${JSON.stringify(value)}`;
  };
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
        scalar(name, offset);
        const scope = scopes.at(-1)!;
        if (scope.has(name))
          problem = `位置 ${offset} 存在重复成员 ${JSON.stringify(name)}`;
        scope.add(name);
      },
      onLiteralValue: (value, offset) => {
        if (typeof value === "string") scalar(value, offset);
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
  if (root && boundary === "protocol")
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
  return value === undefined ? value : (parseJson(stringifyJson(value)) as T);
}
/** 草稿字段的复制只维护 JSON 结构与原词元，不执行公共业务资格校验。 */
export function cloneClientJson<T>(value: T): T {
  return value === undefined
    ? value
    : (parseClientJson(stringifyJson(value)) as T);
}
