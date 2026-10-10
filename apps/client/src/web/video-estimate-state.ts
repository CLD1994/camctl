import type { DraftContent } from "../server/models";
import type { CapabilityState } from "../shared/automatic-previews";
import { estimateVideoAction } from "../shared/video-size-estimate";
import type { EstimateReason, VideoEstimate } from "../shared/types";
import { isObject } from "../shared/validation";
import { ACTION_TYPES } from "../shared/plan";
import { decodeJsonPointer } from "../shared/json-pointer";
import { parseDraft, resolveField } from "./editing";
export type EstimateReloadPhase = "idle" | "updating" | "unconfirmed";
const unavailable = (reason: EstimateReason): VideoEstimate => ({
  kind: "unavailable",
  reason,
});

/** 按实际数组结构判定范围；对象中的数字成员名保留原义。 */
function pendingScope(
  root: unknown,
  path: string,
  index: number,
): "related" | "unrelated" | "unknown" {
  let parts: string[];
  try {
    parts = decodeJsonPointer(path);
  } catch {
    return "unknown";
  }
  let current = root;
  for (const part of parts) {
    if (
      Array.isArray(current) &&
      (!/^(0|[1-9]\d*)$/.test(part) ||
        !Number.isSafeInteger(Number(part)) ||
        !Object.hasOwn(current, part))
    )
      return "unknown";
    current =
      (isObject(current) || Array.isArray(current)) &&
      Object.hasOwn(current, part)
        ? current[part as keyof typeof current]
        : undefined;
  }
  if (!parts.length || (parts[0] === "actions" && parts.length === 1))
    return "related";
  if (parts[0] !== "actions") return "unrelated";
  if (
    !isObject(root) ||
    !Array.isArray(root.actions) ||
    !isObject(root.actions[Number(parts[1])])
  )
    return "unknown";
  if (Number(parts[1]) !== index) return "unrelated";
  return parts.length === 2 ||
    ["device_id", "type", "params"].includes(parts[2])
    ? "related"
    : "unrelated";
}

export function estimateDraftAction(
  content: DraftContent,
  actionIndex: number,
  state: CapabilityState,
  phase: EstimateReloadPhase,
): VideoEstimate {
  let root: ReturnType<typeof parseDraft> | undefined;
  let parseError: string | undefined;
  try {
    root = parseDraft(content);
  } catch (error) {
    parseError = (error as Error).message;
  }
  const action =
    Number.isSafeInteger(actionIndex) && actionIndex >= 0
      ? root?.actions[actionIndex]
      : undefined;
  if (
    isObject(action) &&
    ACTION_TYPES.some((type) => type === action.type) &&
    action.type !== "camera_record" &&
    action.type !== "camera_timelapse"
  )
    return { kind: "hidden" };
  if (!state.active)
    return {
      kind: "unavailable",
      reason: "capabilities_unavailable",
      ...(state.error ? { diagnostic: state.error } : {}),
    };
  if (phase !== "idle")
    return unavailable(
      phase === "updating" ? "rules_updating" : "reload_unconfirmed",
    );
  if (!root)
    return {
      kind: "unavailable",
      reason: "input_unfinished",
      diagnostic: parseError,
    };
  const scopes = Object.keys(content.pending ?? {}).map((path) =>
    pendingScope(root, path, actionIndex),
  );
  if (scopes.includes("related")) return unavailable("input_unfinished");
  if (scopes.includes("unknown")) return unavailable("pending_scope_unknown");
  if (
    !isObject(action) ||
    (action.type !== "camera_record" && action.type !== "camera_timelapse")
  )
    return unavailable("selection_invalid");
  const result = estimateVideoAction(action, state.active);
  if (result.kind === "hidden") return result;
  const warning = state.error
    ? "能力说明加载失败，仍使用此前启用的说明。"
    : undefined;
  let label: string | undefined;
  if (result.kind === "unavailable" && result.path && isObject(action.params)) {
    const params = action.params;
    const parameter = state.active.devices
      .find((d) => d.device_id === action.device_id)
      ?.actions.find((a) => a.type === action.type)
      ?.parameter_types.find((p) => p.type === params.type);
    let field: unknown = parameter?.schema;
    for (const part of decodeJsonPointer(result.path)) {
      field = resolveField(field, parameter?.schema ?? {});
      field =
        isObject(field) &&
        isObject(field.properties) &&
        Object.hasOwn(field.properties, part)
          ? field.properties[part]
          : isObject(field) &&
              isObject(field.items) &&
              /^(0|[1-9]\d*)$/.test(part)
            ? field.items
            : undefined;
    }
    field = resolveField(field, parameter?.schema ?? {});
    if (isObject(field) && typeof field.title === "string") label = field.title;
  }
  return {
    ...result,
    ...(warning ? { warning } : {}),
    ...(label ? { label } : {}),
  };
}
