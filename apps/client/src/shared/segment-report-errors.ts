import registry from "../../../../protocol/errors/workflow-codes.json";
import { createProtocolValidator } from "./protocol-validation";
import { isObject } from "./validation";
import {
  compareJsonNumberTokens,
  exactJsonValueIdentity,
  originalNumberToken,
} from "./json";
import type { ReportAction } from "./types";

const codes = [
  "capture_segment_gap",
  "default_segment_scope_empty",
  "default_segment_scope_unconfirmed",
] as const;
type SegmentCode = (typeof codes)[number];
const ajv = createProtocolValidator();
const checks = new Map(
  codes.map((code) => [code, ajv.compile(registry.codes[code].details_schema)]),
);

export interface SegmentKey {
  directory_number: number;
  index: number;
}
export interface SegmentNeighbor {
  state: "known" | "absent" | "unknown";
  file_identity?: string;
  output_id?: string;
}
export interface SegmentGap {
  kind: "missing_position" | "zero_byte_non_tail";
  start_key: SegmentKey;
  end_key: SegmentKey;
  left: SegmentNeighbor;
  right: SegmentNeighbor;
  empty_file_identity?: string;
}
/** 仅供客户端内部使用；字段结构及基本值由根错误登记校验。 */
export interface SegmentDetails {
  source_action_instance_id: string;
  description: string;
  gap_count: number | null;
  first_gap?: SegmentGap;
  gaps: SegmentGap[];
  default_scope: {
    state: "unknown" | "empty" | "nonempty";
    member_count: number | null;
    output_ids: string[];
  };
  retained_original_count: number;
  retained_output_ids: string[];
}
export interface SegmentIssue {
  code: SegmentCode;
  details: SegmentDetails;
}

/** 同一来源已固定的业务事实可跨错误码比较，说明文字和可选引用缺省不构成冲突。 */
export function compareSegmentFacts(
  a: SegmentDetails,
  b: SegmentDetails,
): void {
  const number = (parent: object, key: string, value: number | null) =>
    exactJsonValueIdentity(value, true, { parent, key });
  const ids = (values: string[]) => JSON.stringify([...values].sort());
  requireFact(
    number(a, "gap_count", a.gap_count) ===
      number(b, "gap_count", b.gap_count) &&
      number(a, "retained_original_count", a.retained_original_count) ===
        number(b, "retained_original_count", b.retained_original_count),
    "同来源冻结计数矛盾",
  );
  requireFact(
    a.default_scope.state === b.default_scope.state &&
      number(a.default_scope, "member_count", a.default_scope.member_count) ===
        number(b.default_scope, "member_count", b.default_scope.member_count) &&
      ids(a.default_scope.output_ids) === ids(b.default_scope.output_ids) &&
      ids(a.retained_output_ids) === ids(b.retained_output_ids),
    "同来源冻结默认范围或保留原片矛盾",
  );
  const neighbor = (n: SegmentNeighbor) => ({
    state: n.state,
    ...(n.file_identity === undefined
      ? {}
      : { file_identity: n.file_identity }),
  });
  const fact = (g: SegmentGap) => ({
    kind: g.kind,
    start_key: g.start_key,
    end_key: g.end_key,
    left: neighbor(g.left),
    right: neighbor(g.right),
    ...(g.empty_file_identity === undefined
      ? {}
      : { empty_file_identity: g.empty_file_identity }),
  });
  requireFact(
    a.gaps.length === b.gaps.length &&
      a.gaps.every(
        (gap, i) =>
          exactJsonValueIdentity(fact(gap)) ===
          exactJsonValueIdentity(fact(b.gaps[i])),
      ),
    "同来源冻结缺口事实矛盾",
  );
  for (let i = 0; i < a.gaps.length; i++)
    for (const side of ["left", "right"] as const) {
      const left = a.gaps[i][side].output_id,
        right = b.gaps[i][side].output_id;
      requireFact(
        left === undefined || right === undefined || left === right,
        "同来源可靠邻接产物身份矛盾",
      );
    }
}
const requireFact = (condition: unknown, message: string): void => {
  if (!condition) throw new Error(`分段报告：${message}`);
};
const token = (parent: object, key: string, value: number) =>
  originalNumberToken(parent, key, value) ?? String(value);
const countMatches = (
  parent: object,
  key: string,
  value: number,
  length: number,
) => compareJsonNumberTokens(token(parent, key, value), String(length)) === 0;
function compareKeys(a: SegmentKey, b: SegmentKey): number {
  return (
    compareJsonNumberTokens(
      token(a, "directory_number", a.directory_number),
      token(b, "directory_number", b.directory_number),
    ) ||
    compareJsonNumberTokens(
      token(a, "index", a.index),
      token(b, "index", b.index),
    )
  );
}
function segmentCode(value: unknown): value is SegmentCode {
  return typeof value === "string" && codes.some((code) => code === value);
}

/** 完整公开详情与内部有限头的接受边界不同；未知错误仍原样保留。 */
export function segmentIssue(error: unknown): SegmentIssue | undefined {
  if (!isObject(error) || !segmentCode(error.code)) return undefined;
  const code = error.code;
  const check = checks.get(code)!;
  requireFact(
    error.stage === registry.codes[code].stage,
    `${code} 阶段与登记不一致`,
  );
  requireFact(
    check(error.details),
    `${code} 详情不符合公共错误结构：${ajv.errorsText(check.errors)}`,
  );
  const d = error.details as unknown as SegmentDetails;
  requireFact(
    Array.isArray(d.gaps) &&
      Array.isArray(d.retained_output_ids) &&
      Array.isArray(d.default_scope.output_ids),
    "缺口、保留原片与默认范围必须完整展开",
  );
  requireFact(
    countMatches(
      d,
      "retained_original_count",
      d.retained_original_count,
      d.retained_output_ids.length,
    ),
    "保留原片数量与身份列表不一致",
  );
  if (d.gap_count === null) {
    requireFact(
      d.gaps.length === 0 &&
        d.first_gap === undefined &&
        d.default_scope.state === "unknown",
      "缺口分类未知不能声明已知缺口或可靠范围",
    );
  } else {
    requireFact(
      countMatches(d, "gap_count", d.gap_count, d.gaps.length),
      "缺口数量与完整列表不一致",
    );
    requireFact(
      d.gaps.length === 0
        ? d.first_gap === undefined
        : d.first_gap !== undefined &&
            exactJsonValueIdentity(d.first_gap) ===
              exactJsonValueIdentity(d.gaps[0]),
      "首缺口与完整列表首项不一致",
    );
  }
  const retained = new Set(d.retained_output_ids);
  for (const gap of d.gaps) {
    requireFact(compareKeys(gap.start_key, gap.end_key) <= 0, "缺口区间倒置");
    if (gap.kind === "zero_byte_non_tail")
      requireFact(
        compareKeys(gap.start_key, gap.end_key) === 0,
        "零字节原片缺口必须是单个位置",
      );
    for (const side of [gap.left, gap.right])
      if (side.output_id !== undefined)
        requireFact(retained.has(side.output_id), "邻接产物不属于保留原片集合");
  }
  const scope = d.default_scope;
  if (scope.state === "unknown")
    requireFact(scope.output_ids.length === 0, "未知默认范围不能携带已选产物");
  else
    requireFact(
      countMatches(
        scope,
        "member_count",
        scope.member_count!,
        scope.output_ids.length,
      ),
      "默认范围数量与身份列表不一致",
    );
  requireFact(
    scope.output_ids.every((id) => retained.has(id)),
    "默认范围产物不属于保留原片集合",
  );
  return { code, details: d };
}

/** 用于所有不属于分段业务的真实公共错误槽，不递归扫描参数或 details。 */
export function rejectSegmentError(error: unknown): void {
  if (isObject(error) && segmentCode(error.code))
    throw new Error(`分段报告：${error.code} 位于错误的业务位置`);
}
function otherMedia(value: unknown): void {
  if (!isObject(value)) return;
  rejectSegmentError(value.error);
  if (Array.isArray(value.issues)) value.issues.forEach(rejectSegmentError);
}

/** 穷尽动作及其子实体的公共错误槽；返回合法分段详情供已知关联检查。 */
export function segmentReferences(action: ReportAction): SegmentIssue[] {
  const references: SegmentIssue[] = [];
  const issue = segmentIssue(action.error);
  if (issue) {
    requireFact(
      issue.code === "capture_segment_gap" && action.type === "camera_record",
      "分段缺口只属于来源录像动作的最终错误",
    );
    requireFact(
      issue.details.source_action_instance_id === action.action_instance_id,
      "缺口来源与所属录像动作不一致",
    );
    references.push(issue);
  }
  rejectSegmentError(action.device_execution?.error);
  const result = action.result;
  if (isObject(result)) {
    otherMedia(result.check);
    for (const key of ["repair", "discard_cleanup"])
      if (isObject(result[key])) rejectSegmentError(result[key].error);
    if (Array.isArray(result.failures))
      for (const failure of result.failures) {
        if (!isObject(failure)) continue;
        const item = segmentIssue(failure.error);
        if (!item) continue;
        requireFact(
          action.type === "obtain_action_outputs" &&
            item.code !== "capture_segment_gap",
          "默认范围错误只属于取回来源级失败",
        );
        requireFact(
          item.details.source_action_instance_id ===
            failure.source_action_instance_id,
          "范围详情与失败项来源不一致",
        );
        requireFact(
          !Object.hasOwn(failure, "output_id") &&
            !Object.hasOwn(failure, "delivery_id"),
          "来源范围失败不能关联虚构产物或交付",
        );
        references.push(item);
      }
    if (Array.isArray(result.items))
      for (const item of result.items) {
        if (!isObject(item)) continue;
        rejectSegmentError(item.error);
        if (Array.isArray(item.withdrawals))
          for (const withdrawal of item.withdrawals)
            if (isObject(withdrawal)) rejectSegmentError(withdrawal.error);
      }
  }
  for (const output of action.outputs ?? []) {
    rejectSegmentError(output.error);
    rejectSegmentError(output.cleanup.error);
    otherMedia(output.media);
  }
  for (const delivery of action.deliveries ?? [])
    rejectSegmentError(delivery.error);
  return references;
}
