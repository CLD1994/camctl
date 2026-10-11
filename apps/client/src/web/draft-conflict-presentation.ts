import type { DraftContent } from "../server/models";
import {
  cloneClientJson,
  parseClientJson,
  stringifyJson,
} from "../shared/json";
import { sameContent } from "../shared/automatic-previews";
import { isObject } from "../shared/validation";
import type { ConflictSelectionSnapshot } from "./draft-conflicts";
export interface DraftSideSummary {
  title?: string;
  actions?: Array<{ index: number; name?: string; type?: string }>;
  raw: string;
  parseError?: string;
}
export const draftContentSections = [
  "text",
  "pending",
  "actionVariants",
  "parameterVariants",
  "objectVariants",
  "automaticPreviews",
] as const;
export type DraftContentSection = (typeof draftContentSections)[number];
export const draftSectionLabels: Record<DraftContentSection, string> = {
  text: "计划正文",
  pending: "未完成输入",
  actionVariants: "停用动作类型",
  parameterVariants: "停用参数类型",
  objectVariants: "停用对象模式",
  automaticPreviews: "自动预览、动作身份和关联",
};
export interface DraftConflictPresentation {
  local: DraftSideSummary;
  remote: DraftSideSummary;
  savedAt: string;
  differences: Array<{ section: DraftContentSection; different: boolean }>;
  localDetails: string;
  remoteDetails: string;
}
function summarize(content: DraftContent): DraftSideSummary {
  const raw = content.text;
  try {
    const plan = parseClientJson(raw);
    if (!isObject(plan))
      return { raw, parseError: "计划正文不是对象，请查看原文。" };
    return {
      raw,
      title: typeof plan.name === "string" ? plan.name : undefined,
      ...(Array.isArray(plan.actions)
        ? {
            actions: plan.actions.map((action, index) => ({
              index,
              name:
                isObject(action) && typeof action.name === "string"
                  ? action.name
                  : undefined,
              type:
                isObject(action) && typeof action.type === "string"
                  ? action.type
                  : undefined,
            })),
          }
        : { parseError: "动作集合无法解释，请查看原文。" }),
    };
  } catch (error) {
    return {
      raw,
      parseError: error instanceof Error ? error.message : String(error),
    };
  }
}
export function presentDraftConflict(
  snapshot: ConflictSelectionSnapshot,
): DraftConflictPresentation {
  const local = cloneClientJson(snapshot.local),
    remote = cloneClientJson(snapshot.remote.content);
  return {
    local: summarize(local),
    remote: summarize(remote),
    savedAt: snapshot.remote.updatedAt,
    differences: draftContentSections.map((section) => ({
      section,
      different: !sameContent(
        { text: "", [section]: local[section] },
        { text: "", [section]: remote[section] },
      ),
    })),
    localDetails: stringifyJson(local, 2),
    remoteDetails: stringifyJson(remote, 2),
  };
}
