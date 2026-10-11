import { expect, it, vi } from "vitest";
import { DraftSession } from "../../src/web/session";
import { stringifyJson } from "../../src/shared/json";
import { record, creation } from "../helpers/draft-conflicts";
import type { DraftContent } from "../../src/server/models";

const laterAcceptances = (["ordinary", "adopt", "replace", "copy"] as const).flatMap(kind =>
  [false, true].flatMap(sameInput => (["available", "unavailable", "deleted", "exported"] as const).map(state => ({kind, sameInput, state}))));
it.each(laterAcceptances)("旧普通接纳快照不能处理B $kind/$sameInput/$state 责任", async ({kind, sameInput, state}) => {
  let reject = true;
  const save = vi.fn(async (_id: string, rev: number, content: DraftContent) => ({...record(rev + 1, "saved"), content}));
  const read = vi.fn(async () => record(2, "remote")), creates = vi.fn();
  const commits = vi.fn();
  const session = new DraftSession(record(1, "base"), {save, read}, () => {}), prepares = vi.fn(() => { if(reject) throw Error("view"); return commits; });
  session.bindWholeView(prepares);
  session.edit(record(1, "A").content);
  await expect(session.flush()).rejects.toThrow();
  const snapshot = session.wholeAcceptanceSnapshot;
  const old = () => session.retryWholeAcceptance(snapshot);
  reject = false;
  session.retryWholeAcceptance(session.wholeAcceptanceSnapshot);
  if(kind === "ordinary" && sameInput) {
    // 新责任必须从不同的可靠基线发起；仅再次编辑已确认A不产生同内容PUT。
    session.observe(record(3, "different baseline"));
  }
  session.edit(record(1, sameInput ? "A" : "B").content);
  reject = true;
  if(kind === "ordinary") await expect(session.flush()).rejects.toThrow();
  else {
    if(sameInput) {
      reject = false;
      session.observe(record(3, "different baseline"));
      session.edit(record(1, "A").content);
      reject = true;
    }
    session.observe(record(sameInput ? 4 : 3, "remote-B"));
    if(kind === "adopt") expect(() => session.adoptSavedVersion(session.prepareConflictSelection())).toThrow();
    if(kind === "replace") await expect(session.replaceSavedVersion(session.prepareConflictSelection())).rejects.toThrow();
    if(kind === "copy") {
      const fact = creation(session.content);
      const result = {kind: "created" as const, creation: fact, target: {kind: "editable" as const, draft: fact.created}};
      const op = session.beginCopy({create: async () => {creates(); return result;}, read: async () => result}, "copy-1");
      await op.advance();
    }
  }
  if(state === "unavailable") session.observeTarget({kind: state, message: "offline"});
  if(state === "deleted") session.observeTarget({kind: state});
  if(state === "exported") session.observe({...record(9, "ended"), exportedRequestId: "42"});
  const before = {input: stringifyJson(session.content), revision: session.revision, saves: save.mock.calls.length, reads: read.mock.calls.length, creates: creates.mock.calls.length, prepares: prepares.mock.calls.length, commits: commits.mock.calls.length};
  reject = false;
  expect(old).toThrow();
  expect({input: stringifyJson(session.content), revision: session.revision, saves: save.mock.calls.length, reads: read.mock.calls.length, creates: creates.mock.calls.length, prepares: prepares.mock.calls.length, commits: commits.mock.calls.length}).toEqual(before);
  if(kind === "ordinary" && state === "available") {
    session.retryWholeAcceptance(session.wholeAcceptanceSnapshot);
    expect(session.content).toEqual(record(1, sameInput ? "A" : "B").content);
    expect(session.saved).toBe(true); expect(session.wholeAcceptanceSnapshot).toBeUndefined();
    expect(save.mock.calls.length).toBe(before.saves); expect(read.mock.calls.length).toBe(before.reads); expect(creates.mock.calls.length).toBe(before.creates);
    expect(prepares.mock.calls.length).toBe(before.prepares + 1); expect(commits.mock.calls.length).toBe(before.commits + 1);
  }
});
const selectedCompletion = (["adopt", "replace", "copy"] as const).flatMap(kind =>
  (["abandoned", "completed"] as const).map(outcome => ({kind, outcome})));
it.each(selectedCompletion)("旧普通入口在新$kind责任执行及$outcome后仍零副作用，当前owned入口合法完成", async ({kind, outcome}) => {
  let failed = true;
  const save = vi.fn(async (_id: string, revision: number, content: DraftContent) => ({...record(revision + 1, "saved"), content}));
  const read = vi.fn(async () => record(3, "remote"));
  const session = new DraftSession(record(1, "base"), {save, read}, () => {});
  const commits = vi.fn(), prepares = vi.fn(() => {if(failed) throw Error("view"); return commits;});
  session.bindWholeView(prepares);
  session.edit(record(1, "A").content);
  await expect(session.flush()).rejects.toThrow();
  const stale = session.wholeAcceptanceSnapshot;
  failed = false;
  session.retryWholeAcceptance(session.wholeAcceptanceSnapshot);
  session.edit(record(1, "B").content);
  session.observe(record(3, "remote"));
  failed = true;
  const refuseOld = () => {
    const before = [save.mock.calls.length, read.mock.calls.length, prepares.mock.calls.length, commits.mock.calls.length, session.revision, stringifyJson(session.content)];
    expect(() => session.retryWholeAcceptance(stale)).toThrow();
    expect([save.mock.calls.length, read.mock.calls.length, prepares.mock.calls.length, commits.mock.calls.length, session.revision, stringifyJson(session.content)]).toEqual(before);
  };
  if(kind === "copy") {
    const fact = creation(session.content), result = {kind: "created" as const, creation: fact, target: {kind: "editable" as const, draft: fact.created}};
    const create = vi.fn(async () => result), targetRead = vi.fn(async () => result);
    await session.beginCopy({create, read: targetRead}, "copy-1").advance();
    if(outcome === "abandoned") session.abandonCopy(session.copyDecision!);
    else {
      failed = false;
      await session.retryCopy(session.copyDecision!, () => {refuseOld(); return () => {};});
      expect(create).toHaveBeenCalledTimes(1);
    }
  } else {
    if(kind === "adopt") expect(() => session.adoptSavedVersion(session.prepareConflictSelection())).toThrow();
    else await expect(session.replaceSavedVersion(session.prepareConflictSelection())).rejects.toThrow();
    if(outcome === "abandoned") session.abandonSelection(session.selectionDecision);
    else {
      session.bindWholeView(() => {refuseOld(); return commits;});
      expect(await session.retrySelection(session.selectionDecision)).toBe("completed");
    }
  }
  refuseOld();
  expect(session.selectionPendingDecision).toBe(false);
  expect(session.copyPendingDecision).toBe(false);
});
it("普通完整观察的同步显示准备拒绝旧preparedEdit且只接纳可靠转换", () => {
  const session = new DraftSession(record(1, "base"), {save: async () => record(1, "base"), read: async () => record(1, "base")}, () => {});
  const prepared = session.prepareEdit(record(1, "old callback").content);
  let refused = false, commits = 0;
  session.bindWholeView(() => {
    try { session.commitEdit(prepared); } catch { refused = true; }
    return () => {commits++;};
  });
  session.observe(record(2, "remote"));
  expect(refused).toBe(true);
  expect(session.content).toEqual(record(2, "remote").content);
  expect(commits).toBe(1);
});
it.each(["same", "unavailable", "deleted", "exported"] as const)("普通接纳外部准备重入%s观察及旧入口在首commit前被拒绝", async state => {
  const session = new DraftSession(record(1, "base"), {save: async () => record(2, "local"), read: async () => record(2, "local")}, () => {});
  session.edit(record(1, "local").content);
  const release = session.bindWholeView(() => {throw Error("view");});
  await expect(session.flush()).rejects.toThrow();
  const snapshot = session.wholeAcceptanceSnapshot, commits = vi.fn();
  release();
  session.bindWholeView(() => {
    expect(() => session.retryWholeAcceptance(snapshot)).toThrow();
    expect(() => session.retryWholeAcceptance(session.wholeAcceptanceSnapshot)).toThrow();
    if(state === "unavailable") session.observeTarget({kind: state, message: "offline"});
    else if(state === "deleted") session.observeTarget({kind: state});
    else session.observe({...record(3, "latest"), ...(state === "exported" ? {exportedRequestId: "42"} : {})});
    return commits;
  });
  expect(() => session.retryWholeAcceptance(snapshot)).toThrow();
  expect(commits).not.toHaveBeenCalled();
  expect(session.content).toEqual(record(1, "local").content);
  expect(session.confirmedDraft.revision).toBe(1);
});
it.each(["id", "createdAt", "revision", "confirmedVersion"] as const)("普通接纳外部准备改变%s固定依据时零commit", async field => {
  const session = new DraftSession(record(1, "base"), {save: async () => record(2, "local"), read: async () => record(2, "local")}, () => {});
  session.edit(record(1, "local").content);
  const release = session.bindWholeView(() => {throw Error("view");});
  await expect(session.flush()).rejects.toThrow();
  const snapshot = session.wholeAcceptanceSnapshot, commits = vi.fn();
  release();
  session.bindWholeView(() => {
    if(field === "id") session.draft.id = "other";
    if(field === "createdAt") session.draft.createdAt = "2026-10-11 09:00:00";
    if(field === "revision") session.revision++;
    if(field === "confirmedVersion") session.confirmedVersion++;
    return commits;
  });
  expect(() => session.retryWholeAcceptance(snapshot)).toThrow();
  expect(commits).not.toHaveBeenCalled();
  expect(session.content).toEqual(record(1, "local").content);
});
it("普通接纳快照副本及其他会话入口没有私有资格", async () => {
  const base = record(1, "base"), session = new DraftSession(base, {save: async () => record(2, "local"), read: async () => base}, () => {});
  session.edit(record(1, "local").content);
  session.bindWholeView(() => {throw Error("view");});
  await expect(session.flush()).rejects.toThrow();
  const snapshot = session.wholeAcceptanceSnapshot!, other = new DraftSession(base, {save: async () => base, read: async () => base}, () => {}), prepares = vi.fn(() => () => {});
  session.bindWholeView(prepares);
  expect(() => session.retryWholeAcceptance({...snapshot})).toThrow();
  expect(() => other.retryWholeAcceptance(snapshot)).toThrow();
  expect(prepares).not.toHaveBeenCalled();
  session.retryWholeAcceptance(snapshot);
  expect(session.saved).toBe(true);
  expect(() => session.retryWholeAcceptance(snapshot)).toThrow();
  expect(prepares).toHaveBeenCalledTimes(1);
});
