import { expect, it } from "vitest";
import { DraftSession } from "../../src/web/session";
import { bindSessionWholeView, identityForSession } from "../../src/web/draft-session-view";
import { FollowOperation } from "../../src/web/followup";
import { initializePreviewMetadata } from "../../src/shared/automatic-previews";
import { cloneClientJson } from "../../src/shared/json";
import { record } from "../helpers/draft-conflicts";
import type { Draft } from "../../src/server/models";

it.each([0, 2, "persistent"] as const)("基础绑定及局部释放恢复维护真实追加身份 %s", async shape => {
  let base = record(1, "base");
  base.content = {text: JSON.stringify({actions: Array.from({length: shape === 0 ? 0 : 2}, (_, i) => ({name: `A${i}`, type: "report_status", params: {scope: "full"}}))})};
  if(shape === "persistent") base.content = initializePreviewMetadata(base.content, "disabled", "n");
  const session = new DraftSession(base, {save: async () => base, read: async () => base}, () => {});
  bindSessionWholeView(session);
  const identity = identityForSession(session);
  for(const lifecycle of ["never-mounted", "mounted", "released"]){
    const keys = [...identity.keys], origin: Draft = {...session.confirmedDraft, content: cloneClientJson(session.content)}, action = {name: lifecycle, type: "report_status", params: {scope: "full"}};
    let actual = origin;
    let release: (() => void) | undefined;
    if(lifecycle !== "never-mounted") {
      const releaseAppend = session.bindAppendView(value => identity.prepareAppend(value));
      const releaseWhole = session.bindWholeView(value => identity.prepareWhole(value));
      release = () => {releaseAppend(); releaseWhole();};
    }
    if(lifecycle === "released") release!();
    const op = new FollowOperation(origin.id, action, {create: async () => origin, prepare: async () => origin, append: async (_id, revision, _action, expected) => {
      if(!expected) throw Error("缺少固定追加期望");
      return actual = {...origin, revision: revision + 1, content: expected.content};
    }, read: async () => actual});
    await op.advance();
    session.lockAppend();
    session.acceptAppend(op.transition!);
    expect(identity.keys.slice(0, keys.length)).toEqual(keys);
    expect(identity.keys).toHaveLength(keys.length + 1);
    expect(session.content).toEqual(actual.content);
    session.acceptAppend(op.transition!);
    expect(identity.keys).toHaveLength(keys.length + 1);
    release?.();
  }
  session.observe({...session.confirmedDraft, revision: session.revision + 1, content: {text: '{"actions":[]}'}});
  expect(identity.keys).toHaveLength(0);
});
