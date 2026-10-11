import type { Draft } from "../server/models";
import { cloneClientJson, stringifyJson } from "../shared/json";
import { requireDraftRecord } from "../shared/draft-record";
import type { DraftSession } from "./session";

/** 先准备目标会话及其显示；调用者再准备导航，最后共同提交。 */
export function prepareCopyTarget(
  sessions: Map<string, DraftSession>,
  actual: Draft,
  createSession: (draft: Draft) => DraftSession,
): { session: DraftSession; validate: () => void; commit: () => void } {
  requireDraftRecord(actual);
  if (actual.exportedRequestId) throw Error("另存目标已经导出");
  const existing = sessions.get(actual.id);
  const session = existing ?? createSession(cloneClientJson(actual));
  const version = session.version,
    epoch = session.observationEpoch,
    content = stringifyJson(session.content);
  const commitSession = session.prepareCopyTarget(cloneClientJson(actual));
  if (sessions.get(actual.id) !== existing)
    throw Error("目标会话集合在准备期间改变");
  return {
    session,
    validate: () => {
      if (
        sessions.get(actual.id) !== existing ||
        session.version !== version ||
        session.observationEpoch !== epoch ||
        stringifyJson(session.content) !== content ||
        !session.canSave ||
        !session.saved ||
        session.saving
      )
        throw Error("目标会话或集合在转交准备期间改变");
    },
    commit: () => {
      commitSession();
      if (!existing) sessions.set(actual.id, session);
    },
  };
}
