import { expect, it } from "vitest";
import { DraftSession, prepareSessionsForReload } from "../../src/web/session";
import { StateObservations } from "../../src/web/state-observations";
import { completeDraftInput, record } from "../helpers/draft-conflicts";
import { cloneClientJson, stringifyJson } from "../../src/shared/json";
import type { DraftObservation } from "../../src/web/draft-conflicts";

const capabilities = { active: { devices: [] }, error: null, generation: 1, version: "k1" };
function unknown() {
  const base = { ...record(1, "base"), content: completeDraftInput() };
  let observation: DraftObservation = { kind: "available", draft: base, capabilities };
  let writes = 0, reads = 0;
  const session = new DraftSession(base, {
    save: async () => { writes++; return base; },
    read: async () => base,
    readObservation: async () => { reads++; return cloneClientJson(observation); },
  }, () => {});
  const fixed = session.beginExport(); session.exportUnknown();
  return { session, base, fixed, writes: () => writes, reads: () => reads, set: (value: DraftObservation) => { observation = value; } };
}

// 普通观察携带捕获 token 也不能取得具体导出核实权；错误清责会令这些断言失败。
it.each(["baseline", "derived", "updated", "exported", "deleted"] as const)("普通%s观察保留未知导出固定输入及核实责任", kind => {
  const f = unknown(), token = f.session.exportToken;
  const observation: DraftObservation = kind === "deleted" ? { kind } : {
    kind: "available", capabilities,
    draft: kind === "baseline" ? f.base : kind === "derived" ? { ...f.base, revision: 2 } : {
      ...record(3, "remote"), ...(kind === "exported" ? { exportedRequestId: "42" } : {}),
    },
  };
  f.session.observeTarget(observation, token);
  expect(f.session.exportState).toBe("unknown");
  expect(f.session.content).toEqual(f.fixed.content);
  expect(f.session.canEdit).toBe(false); expect(f.session.needsLeaveWarning).toBe(true);
  expect(f.session.hasUnfinishedResponsibilities).toBe(true); expect(f.writes()).toBe(0);
  expect(f.session.targetObservation).toEqual(observation);
});

it("另一个会话owned读回对本会话仍是普通观察，不结束未知导出", async () => {
  const f = unknown(), other = { ...record(1, "other"), id: "other" };
  const context = { consumerId: other.id, tokens: new Map([[f.base.id, f.session.exportToken]]) };
  const reads = new StateObservations(async () => [f.base, other], () => context, (drafts, ctx) => {
    for (const actual of drafts) if (actual.id !== ctx.consumerId) f.session.observe(actual, ctx.tokens.get(actual.id), capabilities);
  }, () => {});
  await reads.read();
  expect(f.session.exportState).toBe("unknown"); expect(f.session.content).toEqual(f.fixed.content);
  expect(f.session.needsLeaveWarning).toBe(true); expect(f.session.canEdit).toBe(false);
});

it.each(["baseline", "derived", "exported", "updated", "deleted", "unavailable", "invalid"] as const)("本会话具体核实%s只读并按固定依据完成或保留责任", async kind => {
  const f = unknown();
  const observation: DraftObservation = kind === "deleted" ? { kind } : kind === "unavailable" ? { kind, message: "offline" } : {
    kind: "available", capabilities,
    draft: kind === "baseline" ? f.base : kind === "derived" ? { ...f.base, revision: 2 } : kind === "exported" ? { ...f.base, revision: 2, exportedRequestId: "42" } : {
      ...record(3, "remote"), ...(kind === "invalid" ? { id: "foreign" } : {}),
    },
  };
  f.set(observation);
  if (["deleted", "unavailable", "invalid"].includes(kind)) await expect(f.session.checkExport()).rejects.toThrow();
  else await f.session.checkExport();
  const completed = ["baseline", "derived", "exported"].includes(kind);
  expect(f.session.exportState).toBe(kind === "exported" ? "exported" : completed ? "editable" : "unknown");
  expect(f.session.needsLeaveWarning).toBe(!completed); expect(f.writes()).toBe(0); expect(f.reads()).toBe(1);
  expect(stringifyJson(f.session.content)).toBe(stringifyJson(f.fixed.content));
  expect(f.session.targetObservation).toEqual(observation);
});

it("能力重载准备使用各会话具体导出核实，普通refresh不授予核实权", async () => {
  const f = unknown(); let refreshes = 0;
  await prepareSessionsForReload([f.session], async () => { refreshes++; f.session.observe(f.base, f.session.exportToken); });
  expect(f.reads()).toBe(1); expect(refreshes).toBe(0); expect(f.writes()).toBe(0);
  expect(f.session.exportState).toBe("editable"); expect(f.session.needsLeaveWarning).toBe(false);
});

it("能力重载具体核实读到不同版本时保留未知责任并拒绝前置准备", async () => {
  const f = unknown(); f.set({ kind: "available", draft: record(3, "remote"), capabilities });
  await expect(prepareSessionsForReload([f.session], async () => { throw Error("不应普通refresh"); })).rejects.toThrow();
  expect(f.reads()).toBe(1); expect(f.writes()).toBe(0); expect(f.session.exportState).toBe("unknown");
  expect(f.session.content).toEqual(f.fixed.content);
});
