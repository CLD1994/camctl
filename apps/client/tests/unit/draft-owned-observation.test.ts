import { expect, it, vi } from "vitest";
import { DraftSession } from "../../src/web/session";
import { stringifyJson } from "../../src/shared/json";
import { record, creation, completeDraftInput } from "../helpers/draft-conflicts";
import type { DraftObservation } from "../../src/web/draft-conflicts";
import { StateObservations, SupersededStateReadError } from "../../src/web/state-observations";
import { readDraftObservation, type ClientState } from "../../src/web/api";

const input = completeDraftInput();
it.each(["foreign", "terminal-regression"] as const)("导出自有核实%s非法记录不能确认导出或成为可靠历史", async kind => {
  const prior = {...record(3, "known"), ...(kind === "terminal-regression" ? {exportedRequestId: "42"} : {})};
  const returned = {...record(4, "invalid"), ...(kind === "foreign" ? {id: "foreign", exportedRequestId: "99"} : {})};
  const session = new DraftSession(record(1, "base"), {save: async () => prior, read: async () => returned}, () => {});
  session.edit(input);
  session.observe(prior);
  await expect(session.checkExport()).rejects.toThrow();
  expect(session.decision.kind).toBe("invalid");
  expect(session.lastReliableDraft).toEqual(prior);
  expect(session.exportedRequestId).toBe(kind === "terminal-regression" ? "42" : undefined);
  expect(session.content).toEqual(input);
});
const capabilities = { active: { devices: [] }, error: null, generation: 8, version: "v8" };
function observation(kind: "editable" | "deleted" | "exported" | "unavailable" | "invalid"): DraftObservation {
  if (kind === "deleted") return { kind };
  if (kind === "unavailable") return { kind, message: "offline" };
  return { kind: "available", capabilities, draft: {
    ...record(3, "owned"),
    ...(kind === "exported" ? { exportedRequestId: "42" } : kind === "invalid" ? { id: "foreign" } : {}),
  } };
}
async function copy(read: () => Promise<DraftObservation>) {
  const session = new DraftSession(record(1, "base"), {
    save: vi.fn(async () => record(2, "saved")), read: async () => record(2, "remote"), readObservation: read,
  }, () => {});
  session.edit(input);
  session.observe(record(2, "remote"));
  const fact = creation(input), result = { kind: "created" as const, creation: fact, target: { kind: "editable" as const, draft: fact.created } };
  const create = vi.fn(async () => result), targetRead = vi.fn(async () => result);
  const op = session.beginCopy({ create, read: targetRead }, "copy-1");
  await op.advance();
  return { session, op, create, targetRead };
}
const reliableFailures: Array<{kind: "editable" | "exported" | "deleted"; stage: "source" | "target" | "validate"}> = [
  ...(["editable", "exported"] as const).flatMap(kind => (["source", "target", "validate"] as const).map(stage => ({kind, stage}))),
  ...(["target", "validate"] as const).map(stage => ({kind: "deleted" as const, stage})),
];
it.each(reliableFailures)("操作自有来源读取 $kind 在 $stage 拒绝前登记事实且零共同提交", async ({kind, stage}) => {
  const observed = observation(kind), { session, op, create } = await copy(async () => observed), before = stringifyJson(session.content), fact = op.result;
  const commits = vi.fn();
  session.bindWholeView(() => { if (stage === "source") throw Error("source prepare"); return commits; });
  await expect(session.retryCopy(session.copyDecision!, () => {
    if (stage === "target") throw Error("target prepare");
    return { validate: () => { throw Error("validate"); }, commit: commits };
  })).rejects.toThrow();
  expect(session.targetObservation).toEqual(observed);
  expect(session.lastReliableDraft.revision).toBe(kind === "deleted" ? 2 : 3);
  expect(session.decision.kind === "deleted").toBe(kind === "deleted");
  expect(session.exportedRequestId).toBe(kind === "exported" ? "42" : undefined);
  expect(stringifyJson(session.content)).toBe(before);
  expect(session.confirmedDraft.revision).toBe(1);
  expect(op.result).toEqual(fact);
  expect(session.copyRecovery!.status).toBe("pending");
  expect(session.copyDecision!.canAbandon).toBe(true);
  expect(session.needsLeaveWarning).toBe(true);
  expect(commits).not.toHaveBeenCalled();
  expect(create).toHaveBeenCalledTimes(1);
});
it.each(["success", "failure"] as const)("能力重载取代操作自有来源读取 %s结果不降级来源资格", async outcome => {
  let resolve!: (state: ClientState) => void, reject!: (error: Error) => void;
  const request = new Promise<ClientState>((yes, no) => {resolve = yes; reject = no;});
  let started!: () => void;
  const entered = new Promise<void>(yes => { started = yes; });
  const model = new StateObservations(() => {started(); return request;}, () => "poll", () => {}, () => {});
  const {session} = await copy(() => readDraftObservation("d1", async () => {
    const result = await model.read("d1");
    if(result.kind === "superseded") throw new SupersededStateReadError();
    return result.value;
  }));
  const prior = session.targetObservation, prepare = vi.fn(() => () => {});
  const run = session.retryCopy(session.copyDecision!, prepare);
  await entered;
  model.beginReload();
  if(outcome === "failure") reject(Error("offline"));
  else resolve({startup: {state: "ready"}, drafts: [record(3, "old")]} as ClientState);
  await expect(run).rejects.toThrow();
  expect(session.targetObservation).toEqual(prior);
  expect(session.lastReliableDraft).toEqual(record(2, "remote"));
  expect(prepare).not.toHaveBeenCalled();
  expect(session.content).toEqual(input);
});
it.each(["unavailable", "invalid"] as const)("操作自有来源读取 %s 更新资格并保留创建和输入", async kind => {
  const observed = observation(kind), {session, op} = await copy(async () => observed), prepares = vi.fn(() => () => {}), fact = op.result;
  await expect(session.retryCopy(session.copyDecision!, prepares)).rejects.toThrow();
  if (kind === "unavailable") expect(session.targetObservation).toEqual(observed);
  expect(session.lastReliableDraft).toEqual(record(2, "remote"));
  expect(session.decision.kind).toBe(kind);
  expect(session.error).not.toBe("");
  expect(session.canEdit).toBe(false);
  expect(session.copyDecision!.canAbandon).toBe(false);
  expect(session.content).toEqual(input);
  expect(op.result).toEqual(fact);
  expect(prepares).not.toHaveBeenCalled();
});
const changedDuringPreparation = (["source", "target", "validate"] as const).flatMap(stage =>
  (["editable", "deleted", "exported", "unavailable", "invalid"] as const).map(kind => ({stage, kind})));
it.each(changedDuringPreparation)("$stage 准备期间$kind新观察拒绝共同提交并保持最新事实", async ({stage, kind}) => {
  const owned = observation("editable"), latest = observation(kind), {session} = await copy(async () => owned), commits = vi.fn();
  if (latest.kind === "available") latest.draft.revision = 4;
  session.bindWholeView(() => { if (stage === "source") session.observeTarget(latest); return commits; });
  await expect(session.retryCopy(session.copyDecision!, () => {
    if (stage === "target") session.observeTarget(latest);
    return {validate: () => { if (stage === "validate") session.observeTarget(latest); }, commit: commits};
  })).rejects.toThrow();
  expect(session.targetObservation).toEqual(latest);
  expect(session.content).toEqual(input);
  expect(session.lastReliableDraft.revision).toBe(kind === "editable" || kind === "exported" ? 4 : 3);
  expect(session.copyDecision!.canAbandon).toBe(kind !== "unavailable" && kind !== "invalid");
  expect(session.copyRecovery!.status).toBe("pending");
  expect(commits).not.toHaveBeenCalled();
});
it("来源导出后非法恢复editable的自有记录不能成为可靠历史或放弃资格", async () => {
  const {session} = await copy(async () => ({kind: "available", draft: record(5, "resurrected")}));
  const terminal = {...record(4, "ended"), exportedRequestId: "42"};
  session.observe(terminal);
  const prepare = vi.fn(() => () => {});
  await expect(session.retryCopy(session.copyDecision!, prepare)).rejects.toThrow();
  expect(session.lastReliableDraft).toEqual(terminal);
  expect(session.decision.kind).toBe("invalid");
  expect(session.copyDecision!.canAbandon).toBe(false);
  expect(session.exportedRequestId).toBe("42");
  expect(session.content).toEqual(input);
  expect(prepare).not.toHaveBeenCalled();
});
it("未定义来源分类按不可用诊断拒绝所有决定", async () => {
  const {session} = await copy(async () => ({kind: "unknown"} as unknown as DraftObservation));
  const prepare = vi.fn(() => () => {});
  await expect(session.retryCopy(session.copyDecision!, prepare)).rejects.toThrow();
  expect(session.decision.kind).toBe("unavailable");
  expect(session.lastReliableDraft).toEqual(record(2, "remote"));
  expect(session.copyDecision!.canAbandon).toBe(false);
  expect(prepare).not.toHaveBeenCalled();
});
it.each(["editable", "deleted", "exported", "unavailable", "invalid"] as const)("固定保存自有核实等待期间%s观察不能被旧读回覆盖", async kind => {
  let entered!: () => void, release!: (value: DraftObservation) => void;
  const started = new Promise<void>(yes => {entered = yes;}), response = new Promise<DraftObservation>(yes => {release = yes;});
  const session = new DraftSession(record(1, "base"), {save: async () => {throw Error("lost");}, read: async () => record(1, "base"), readObservation: () => {entered(); return response;}}, () => {});
  session.edit(input);
  const flushing = session.flush();
  await started;
  const fresh = observation(kind);
  session.observeTarget(fresh);
  release({kind: "available", draft: {...record(2, "saved"), content: input}});
  await expect(flushing).rejects.toThrow();
  expect(session.targetObservation).toEqual(fresh);
  expect(session.content).toEqual(input);
  expect(session.confirmedDraft.revision).toBe(1);
  expect(session.hasUnfinishedResponsibilities).toBe(true);
});
