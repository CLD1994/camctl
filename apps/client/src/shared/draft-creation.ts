import type { Draft, DraftCreationResult } from "../server/models";
import { sameContent } from "./automatic-previews";
import { checkDraftContent } from "./draft-content";
import { requireDraftRecord } from "./draft-record";

/** 创建结果的纯完整边界；API 与操作共同拒绝无法解释的事实。 */
export function requireDraftCreationResult(
  value: unknown,
  attemptId: string,
): DraftCreationResult {
  const object = (input: unknown): input is Record<string, unknown> =>
    typeof input === "object" && input !== null && !Array.isArray(input);
  const content = (input: unknown) => {
    checkDraftContent(input);
    return true;
  };
  const draft = (input: unknown): input is Draft => {
    requireDraftRecord(input);
    return true;
  };
  if (!object(value)) throw Error("创建响应必须包含完整结果封套");
  if (value.kind === "not_created" && value.attemptId === attemptId)
    return value as DraftCreationResult;
  if (
    value.kind !== "created" ||
    !object(value.creation) ||
    value.creation.attemptId !== attemptId ||
    !content(value.creation.input) ||
    !draft(value.creation.created) ||
    value.creation.created.revision !== 1 ||
    value.creation.created.exportedRequestId !== undefined ||
    value.creation.created.lastWrite !== undefined ||
    value.creation.created.createdAt !== value.creation.created.updatedAt ||
    !object(value.target)
  )
    throw Error("创建响应的固定身份、首次记录或目标事实不完整");
  if (value.target.kind === "deleted")
    return value as unknown as DraftCreationResult;
  if (
    !["editable", "exported"].includes(String(value.target.kind)) ||
    !draft(value.target.draft) ||
    value.target.draft.id !== value.creation.created.id ||
    value.target.draft.createdAt !== value.creation.created.createdAt ||
    (value.target.kind === "exported") !==
      (value.target.draft.exportedRequestId !== undefined) ||
    value.target.draft.revision < value.creation.created.revision ||
    (value.target.draft.revision === 1 &&
      (!sameContent(
        value.target.draft.content,
        value.creation.created.content,
      ) ||
        value.target.draft.exportedRequestId !== undefined))
  )
    throw Error("创建响应的固定身份、首次记录或目标事实不完整");
  return value as unknown as DraftCreationResult;
}
