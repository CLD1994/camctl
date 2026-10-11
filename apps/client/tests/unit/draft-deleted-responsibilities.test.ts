import { expect, it } from "vitest";
import { DraftSession } from "../../src/web/session";
import { completeDraftInput, record, creation } from "../helpers/draft-conflicts";
import { stringifyJson } from "../../src/shared/json";

// 终态导航只消费完整离页责任：若getter丢失任一责任，清洁结束路径会丢输入或入口。
it.each(["clean", "dirty", "recovery", "save", "export", "delete", "append", "selection", "copy"] as const)("可靠deleted后%s输入及责任决定是否可以结束Editor", async kind => {
  const base = { ...record(1, "base"), content: completeDraftInput() };
  const session = new DraftSession(base, { save: async () => { throw Error("lost write"); }, read: async () => { throw Error("offline"); } }, () => {});
  if (kind === "dirty") session.edit({ ...base.content, text: '{"name":"dirty","actions":[]}', actionVariants: {}, parameterVariants: {}, objectVariants: {}, automaticPreviews: undefined });
  if (kind === "recovery") session.recoveryContent = completeDraftInput();
  if (kind === "save") { session.edit({ ...base.content, pending: { "/amount": { kind: "number", text: "+" } } }); await expect(session.flush()).rejects.toThrow(); }
  if (kind === "export") { session.beginExport(); session.exportUnknown(); }
  if (kind === "delete") await expect(session.delete(async () => { throw Error("lost delete"); }, async () => { throw Error("offline"); })).rejects.toThrow();
  if (kind === "append") session.lockAppend();
  if (kind === "selection") {
    session.edit({ ...base.content, pending: { "/amount": { kind: "number", text: "+" } } });
    session.observe({ ...base, revision: 2, content: { ...base.content, pending: { "/amount": { kind: "number", text: "1e" } } } });
    session.bindWholeView(() => { throw Error("display failure"); });
    expect(() => session.adoptSavedVersion(session.prepareConflictSelection())).toThrow();
  }
  if (kind === "copy") {
    session.beginCopy({ create: async attemptId => ({ kind: "created", creation: { ...creation(base.content), attemptId }, target: { kind: "deleted" } }), read: async () => { throw Error("unexpected"); } }, "copy-owned");
    await session.retryCopy(session.copyDecision!, () => { throw Error("deleted target cannot transfer"); });
  }
  const input = stringifyJson(session.content), recovery = stringifyJson(session.recoveryContent);
  session.observeTarget({ kind: "deleted" });
  expect(session.targetObservation.kind).toBe("deleted"); expect(session.canEdit).toBe(false); expect(stringifyJson(session.content)).toBe(input);
  if (kind === "clean") { expect(session.needsLeaveWarning).toBe(false); expect(session.hasUnfinishedResponsibilities).toBe(false); }
  else { expect(session.needsLeaveWarning).toBe(true); if (!["dirty", "recovery"].includes(kind)) expect(session.hasUnfinishedResponsibilities).toBe(true); }
  if (kind === "recovery") expect(stringifyJson(session.recoveryContent)).toBe(recovery);
  if (kind === "copy") expect(session.copyDecision).toBeDefined();
  if (kind === "selection") expect(session.selectionDecision).toBeDefined();
  if (kind === "save") expect(session.saveVerification).toBeDefined();
});
