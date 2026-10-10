import type { DraftContent } from "../server/models";
import { parseJson } from "./json";
import { isObject } from "./validation";
import { decodeJsonPointer } from "./json-pointer";

export type DraftPath = Array<string | number>;
export type DraftRoot = Record<string, any> & {
  actions: Record<string, any>[];
};
export function requireDraftRoot(value: unknown): DraftRoot {
  if (!isObject(value) || !Array.isArray(value.actions))
    throw new Error("计划必须是对象，并包含 actions 数组。请在 JSON 中修正。");
  return value as DraftRoot;
}
export function parseDraft(content: DraftContent): DraftRoot {
  return requireDraftRoot(parseJson(content.text));
}
export function pointer(path: DraftPath): string {
  return path.length
    ? "/" +
        path
          .map((p) => String(p).replace(/~/g, "~0").replace(/\//g, "~1"))
          .join("/")
    : "";
}

/** 编辑原文只有解码后的规范、现存动作位置才能参与动作搬移。 */
export function inspectActionPending(
  content: DraftContent,
  actions: unknown[],
) {
  return Object.entries(content.pending ?? {}).map(([key, input]) => {
    let path: string[];
    try {
      path = decodeJsonPointer(key);
    } catch {
      throw Error(`未完成输入路径无法解码：${key}`);
    }
    let actionIndex: number | undefined;
    if (path[0] === "actions" && path.length > 1) {
      if (
        !/^(0|[1-9][0-9]*)$/.test(path[1]) ||
        !Number.isSafeInteger(Number(path[1])) ||
        Number(path[1]) >= actions.length ||
        !Object.hasOwn(actions, path[1]) ||
        !isObject(actions[Number(path[1])])
      )
        throw Error(`未完成输入没有可确定的动作位置：${key}`);
      actionIndex = Number(path[1]);
    }
    return { key, input, path, actionIndex };
  });
}

export function requireKnownActionList(
  entries: ReturnType<typeof inspectActionPending>,
) {
  if (
    entries.some(
      ({ path }) =>
        !path.length || (path[0] === "actions" && path.length === 1),
    )
  )
    throw Error("整个计划或动作集合存在未完成输入，无法确定动作位置");
}
