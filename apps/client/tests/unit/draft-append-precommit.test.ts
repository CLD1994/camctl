import { expect, it } from "vitest";
import { DraftSession } from "../../src/web/session";
import { FollowOperation } from "../../src/web/followup";
import { bindSessionWholeView, identityForSession } from "../../src/web/draft-session-view";
import { completeDraftInput, record } from "../helpers/draft-conflicts";
import { cloneClientJson, stringifyJson } from "../../src/shared/json";
import type { DraftObservation } from "../../src/web/draft-conflicts";

async function ready() {
  const origin = { ...record(1, "base"), content: completeDraftInput() };
  let actual = origin, posts = 0;
  const session = new DraftSession(origin, { read: async () => actual, save: async () => { throw Error("追加接纳不能保存"); } }, () => {});
  bindSessionWholeView(session);
  const identity = identityForSession(session);
  const operation = new FollowOperation(origin.id, { name: "new", type: "report_status", params: { scope: "full" } }, {
    create: async () => origin, prepare: async () => origin, read: async () => actual,
    append: async (_id, revision, _action, expected) => {
      if (!expected) throw Error("缺少固定追加依据");
      posts++; return actual = { ...origin, revision: revision + 1, content: expected.content };
    },
  });
  await operation.advance();
  expect(operation.transition).toBeDefined(); expect(operation.result?.revision).toBe(2);
  session.lockAppend();
  return { session, identity, operation, origin, posts: () => posts };
}

// 外部prepare期间新观察必须令首commit前复核拒绝；缺少复核会改写内容、键和最新事实。
it.each(["unavailable", "deleted", "updated", "exported", "invalid", "lost-restored"] as const)("真实追加转换prepare期间%s拒绝旧准备并保留本地输入及追加责任", async kind => {
  const f = await ready(), before = stringifyJson(f.session.content), keys = [...f.identity.keys];
  let commits = 0;
  const observation: DraftObservation = kind === "unavailable" || kind === "lost-restored" ? { kind: "unavailable", message: "offline" } : kind === "deleted" ? { kind } : {
    kind: "available", draft: { ...record(3, "newer"), ...(kind === "exported" ? { exportedRequestId: "42" } : kind === "invalid" ? { id: "foreign" } : {}) },
  };
  f.session.bindAppendView(transition => {
    const commit = f.identity.prepareAppend(transition);
    f.session.observeTarget(observation);
    if (kind === "lost-restored") f.session.observeTarget({ kind: "available", draft: f.origin });
    return () => { commits++; commit(); };
  });
  expect(() => f.session.acceptAppend(f.operation.transition!)).toThrow();
  expect(commits).toBe(0); expect(stringifyJson(f.session.content)).toBe(before); expect(f.identity.keys).toEqual(keys);
  expect(f.session.revision).toBe(1); expect(f.session.version).toBe(0); expect(f.session.appendLocked).toBe(true);
  expect(f.session.targetObservation).toEqual(kind === "lost-restored" ? { kind: "available", draft: f.origin } : observation);
  expect(f.operation.result?.revision).toBe(2); expect(f.posts()).toBe(1);
  f.operation.acceptanceFailed(Error("local prepare stale")); await f.operation.advance();
  expect(f.posts()).toBe(1); expect(f.operation.transition).toBeDefined();
});

it("重复相同可靠观察不令合法追加prepare失效，已接纳转换幂等", async () => {
  const f = await ready(), keys = [...f.identity.keys], epoch = f.session.observationEpoch; let commits = 0;
  f.session.bindAppendView(transition => {
    const commit = f.identity.prepareAppend(transition);
    f.session.observeTarget({ kind: "available", draft: f.origin });
    return () => { commits++; commit(); };
  });
  f.session.acceptAppend(f.operation.transition!);
  expect(f.session.observationEpoch).toBe(epoch); expect(commits).toBe(1);
  expect(f.identity.keys.slice(0, keys.length)).toEqual(keys); expect(f.identity.keys).toHaveLength(keys.length + 1);
  expect(f.session.content).toEqual(f.operation.result!.content); expect(f.session.appendLocked).toBe(false);
  f.session.acceptAppend(f.operation.transition!); expect(commits).toBe(1); expect(f.posts()).toBe(1);
});

it.each(["identity", "createdAt", "revision", "version", "confirmedVersion", "text", "pending", "actionVariants", "parameterVariants", "objectVariants", "automaticPreviews", "lock", "export", "delete", "recovery"] as const)("追加prepare期间%s依据改变，旧显示commit不得接管新状态", async part => {
  const f = await ready(), keys = [...f.identity.keys]; let commits = 0, latestInput = "";
  f.session.bindAppendView(transition => {
    const commit = f.identity.prepareAppend(transition);
    if (part === "identity") f.session.draft.id = "other";
    else if (part === "createdAt") f.session.draft.createdAt = "2026-10-12 00:00:00";
    else if (part === "revision") f.session.revision++;
    else if (part === "version") f.session.version++;
    else if (part === "confirmedVersion") f.session.confirmedVersion++;
    else if (part === "lock") { f.session.unlockAppend(); f.session.lockAppend(); }
    else if (part === "export") f.session.exportState = "unknown";
    else if (part === "delete") f.session.deletionState = "unknown";
    else if (part === "recovery") f.session.recoveryContent = cloneClientJson(f.session.content);
    else {
      const next = cloneClientJson(f.session.content);
      if (part === "text") next.text = '{"name":"changed","actions":[]}';
      else if (part === "pending") next.pending!["/amount"].text = "+";
      else if (part === "actionVariants") next.actionVariants!["0"][0].fields = { n: 2 };
      else if (part === "parameterVariants") next.parameterVariants!["0"][0].paramsText = '{"params":{"type":"photo","n":2}}';
      else if (part === "objectVariants") next.objectVariants!["0"]["/x"].variants[0].valueText = '{"value":{"mode":"auto"}}';
      else next.automaticPreviews!.intent = "disabled";
      f.session.content = next;
    }
    latestInput = stringifyJson(f.session.content);
    return () => { commits++; commit(); };
  });
  expect(() => f.session.acceptAppend(f.operation.transition!)).toThrow();
  expect(commits).toBe(0); expect(f.identity.keys).toEqual(keys); expect(stringifyJson(f.session.content)).toBe(latestInput);
  expect(f.session.appendLocked).toBe(true); expect(f.posts()).toBe(1); expect(f.operation.result?.revision).toBe(2);
});

it("追加prepare拒绝后只重试本地接纳，释放局部绑定后基础显示与会话共同提交", async () => {
  const f = await ready(), keys = [...f.identity.keys], input = stringifyJson(f.session.content);
  const release = f.session.bindAppendView(() => { throw Error("display unavailable"); });
  expect(() => f.session.acceptAppend(f.operation.transition!)).toThrow();
  f.operation.acceptanceFailed(Error("display unavailable"));
  expect(f.operation.phase).toBe("acceptance_failed");
  expect(stringifyJson(f.session.content)).toBe(input); expect(f.identity.keys).toEqual(keys); expect(f.session.appendLocked).toBe(true);
  release(); await f.operation.advance(); f.session.acceptAppend(f.operation.transition!); f.operation.acceptanceSucceeded();
  expect(f.operation.phase).toBe("done"); expect(f.posts()).toBe(1); expect(f.session.appendLocked).toBe(false);
  expect(f.session.content).toEqual(f.operation.result!.content); expect(f.identity.keys.slice(0, keys.length)).toEqual(keys);
  expect(f.identity.keys).toHaveLength(keys.length + 1);
});

it("追加prepare改变当前显示绑定后拒绝旧commit，当前新绑定仍能接纳同一后端结果", async () => {
  const f = await ready(), keys = [...f.identity.keys]; let commits = 0;
  const release = f.session.bindAppendView(transition => {
    const commit = f.identity.prepareAppend(transition); release();
    return () => { commits++; commit(); };
  });
  expect(() => f.session.acceptAppend(f.operation.transition!)).toThrow();
  expect(commits).toBe(0); expect(f.identity.keys).toEqual(keys); expect(f.session.appendLocked).toBe(true);
  f.session.acceptAppend(f.operation.transition!);
  expect(f.posts()).toBe(1); expect(f.identity.keys).toHaveLength(keys.length + 1);
});

it.each(["origin", "baseline", "actual"] as const)("追加转换%s的createdAt不属于本会话身份时首prepare前拒绝", async part => {
  const f = await ready(), input = stringifyJson(f.session.content), keys = [...f.identity.keys]; let prepares = 0;
  const transition = f.operation.transition!; transition[part].createdAt = "2026-01-01 00:00:00";
  f.session.bindAppendView(value => { prepares++; return f.identity.prepareAppend(value); });
  expect(() => f.session.acceptAppend(transition)).toThrow();
  expect(prepares).toBe(0); expect(f.identity.keys).toEqual(keys); expect(stringifyJson(f.session.content)).toBe(input);
  expect(f.session.appendLocked).toBe(true); expect(f.posts()).toBe(1);
});
