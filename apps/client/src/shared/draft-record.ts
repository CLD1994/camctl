import type { Draft } from "../server/models";
import { formatProtocolTime } from "../domain/protocol-time";
import { checkDraftContent } from "./draft-content";
import { isCanonicalId } from "./validation";

/** 完整持久草稿的读回结构；不执行计划业务校验或修改输入。 */
export function requireDraftRecord(input: unknown): Draft {
  const object = (value: unknown): value is Record<string, unknown> =>
    typeof value === "object" && value !== null && !Array.isArray(value);
  if (
    !object(input) ||
    typeof input.id !== "string" ||
    !input.id.length ||
    !Number.isSafeInteger(input.revision) ||
    (input.revision as number) < 1 ||
    typeof input.createdAt !== "string" ||
    typeof input.updatedAt !== "string"
  )
    throw Error("草稿记录缺少完整身份、版本或时间");
  checkDraftContent(input.content);
  formatProtocolTime(input.createdAt);
  formatProtocolTime(input.updatedAt);
  if (
    input.updatedAt < input.createdAt ||
    (input.exportedRequestId !== undefined &&
      (!isCanonicalId(input.exportedRequestId) || input.revision === 1))
  )
    throw Error("草稿记录的时间或导出状态无效");
  if (input.lastWrite !== undefined) {
    if (
      !object(input.lastWrite) ||
      !Number.isSafeInteger(input.lastWrite.revision) ||
      (input.lastWrite.revision as number) < 2 ||
      (input.lastWrite.revision as number) > (input.revision as number)
    )
      throw Error("草稿记录的写入依据版本无效");
    checkDraftContent(input.lastWrite.input);
    checkDraftContent(input.lastWrite.content);
  }
  return input as unknown as Draft;
}
