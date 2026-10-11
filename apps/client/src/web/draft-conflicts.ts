import type { Draft, DraftContent } from "../server/models";
import {
  coordinatePreviews,
  sameContent,
  type CapabilityState,
} from "../shared/automatic-previews";
import { checkDraftContent } from "../shared/draft-content";
import { requireDraftRecord } from "../shared/draft-record";

export type DraftObservation =
  | { kind: "available"; draft: Draft; capabilities?: CapabilityState }
  | { kind: "deleted" }
  | { kind: "unavailable"; message: string };
export type PendingDraftOperation =
  | { kind: "none" }
  | { kind: "save"; revision: number; requestEnded: boolean }
  | { kind: "other" };
export type DraftDecision = {
  kind:
    | "ignore"
    | "invalid"
    | "unavailable"
    | "blocked"
    | "exported"
    | "deleted"
    | "derived"
    | "saved_content"
    | "same_baseline"
    | "remote"
    | "conflict"
    | "superseded_write";
};
export interface ConflictSelectionSnapshot {
  draftId: string;
  localVersion: number;
  local: DraftContent;
  remote: Draft;
  observationEpoch: number;
}

/** 只分类当前可靠事实；不认领具体请求成功，不接纳输入或安排写入。 */
export function classifyDraftObservation(
  baseline: Draft,
  local: DraftContent,
  observation: DraftObservation,
  pending: PendingDraftOperation,
): DraftDecision {
  if (observation.kind === "unavailable") return { kind: "unavailable" };
  if (observation.kind === "deleted") return { kind: "deleted" };
  try {
    const actual = requireDraftRecord(observation.draft);
    requireDraftRecord(baseline);
    checkDraftContent(local);
    if (actual.id !== baseline.id || actual.createdAt !== baseline.createdAt)
      return { kind: "invalid" };
    if (actual.revision < baseline.revision) return { kind: "ignore" };
    if (
      actual.revision === baseline.revision &&
      !sameContent(actual.content, baseline.content)
    )
      return { kind: "invalid" };
    if (actual.exportedRequestId !== undefined) return { kind: "exported" };
    if (
      pending.kind === "other" ||
      (pending.kind === "save" && !pending.requestEnded)
    )
      return { kind: "blocked" };
    if (pending.kind === "save")
      return {
        kind:
          actual.revision > pending.revision ? "superseded_write" : "blocked",
      };
    const capabilities = observation.capabilities;
    const retainedWrite =
      !baseline.lastWrite ||
      (!!actual.lastWrite &&
        actual.lastWrite.revision === baseline.lastWrite.revision &&
        sameContent(actual.lastWrite.input, baseline.lastWrite.input) &&
        sameContent(actual.lastWrite.content, baseline.lastWrite.content));
    if (
      actual.revision > baseline.revision &&
      capabilities &&
      retainedWrite &&
      (!actual.lastWrite || actual.lastWrite.revision <= baseline.revision)
    ) {
      const derived = coordinatePreviews(
        baseline.content,
        capabilities,
      ).content;
      if (
        !sameContent(derived, baseline.content) &&
        sameContent(derived, actual.content)
      )
        return { kind: "derived" };
    }
    if (sameContent(actual.content, local)) return { kind: "saved_content" };
    if (sameContent(actual.content, baseline.content))
      return { kind: "same_baseline" };
    if (sameContent(local, baseline.content)) return { kind: "remote" };
    return { kind: "conflict" };
  } catch {
    return { kind: "invalid" };
  }
}

/** epoch 由会话在读取失效或可靠内容变化时推进，防止旧确认在恢复后复活。 */
export function selectionMatches(
  snapshot: ConflictSelectionSnapshot,
  draftId: string,
  localVersion: number,
  local: DraftContent,
  observation: DraftObservation,
  observationEpoch: number,
): boolean {
  if (
    observation.kind !== "available" ||
    snapshot.draftId !== draftId ||
    snapshot.localVersion !== localVersion ||
    snapshot.observationEpoch !== observationEpoch
  )
    return false;
  try {
    const actual = requireDraftRecord(observation.draft),
      remote = requireDraftRecord(snapshot.remote);
    checkDraftContent(snapshot.local);
    checkDraftContent(local);
    return (
      remote.id === draftId &&
      actual.id === draftId &&
      remote.exportedRequestId === undefined &&
      actual.exportedRequestId === undefined &&
      actual.createdAt === remote.createdAt &&
      actual.revision === remote.revision &&
      sameContent(snapshot.local, local) &&
      sameContent(remote.content, actual.content)
    );
  } catch {
    return false;
  }
}
