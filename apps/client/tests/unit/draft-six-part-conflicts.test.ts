import { expect, it } from "vitest";
import type { Draft, DraftContent } from "../../src/server/models";
import { classifyDraftObservation } from "../../src/web/draft-conflicts";
import { DraftSession } from "../../src/web/session";
import { bindSessionWholeView } from "../../src/web/draft-session-view";
import { stringifyJson } from "../../src/shared/json";

const text = '{"name":"same","actions":[{"name":"camera","type":"camera_record","params":{"type":"standard","exposure":{"mode":"auto"}}},{"name":"preview","type":"obtain_action_outputs","params":{"source":{"action_name":"camera"},"filter":"preview","purpose":"auto_preview"}}]}';
const sections = ["text", "pending-number", "pending-json", "actionVariants", "parameterVariants", "objectVariants", "automaticPreviews", "combined"] as const;
function piece(section: typeof sections[number], tag: string): DraftContent {
  const facts: DraftContent = {
    text: text.replace('"same"', `"${tag}"`),
    pending: { "/amount": { kind: "number", text: tag === "B" ? "-" : tag === "L" ? "+" : "1e" } },
    actionVariants: { "0": [{ type: "camera_take_photo", fields: { tag }, pending: {} }] },
    parameterVariants: { "0": [{ paramsText: `{"params":{"type":"bitrate","tag":"${tag}"}}`, pending: {} }] },
    objectVariants: { "0": { "/exposure": { discriminator: "mode", variants: [{ valueText: `{"value":{"mode":"manual","tag":"${tag}"}}`, pending: {} }] } } },
    automaticPreviews: { namespace: tag, next: 2, intent: tag === "B" ? "unset" : tag === "L" ? "enabled" : "disabled", actions: [{ id: `${tag}:source` }, { id: `${tag}:preview`, sourceId: `${tag}:source` }] },
  };
  if (section === "combined") return facts;
  if (section === "pending-json") return { text, pending: { "/extra": { kind: "json", text: tag === "B" ? "{" : tag === "L" ? "[" : '"' } } };
  if (section === "pending-number") return { text, pending: facts.pending };
  if (section === "text") return { text: facts.text };
  return { text, [section]: facts[section] };
}
function record(revision: number, content: DraftContent): Draft {
  return { id: "source", revision, content, createdAt: "2026-10-12 00:00:00", updatedAt: "2026-10-12 00:00:01" };
}

// 若三方分类只比较正文或因无关 pending 才检测停用资料，此测试的单一维度反例失败。
it.each(sections)("只有%s独立不同仍为完整三方冲突", section => {
  const base = piece(section, "B"), local = piece(section, "L"), remote = piece(section, "R");
  if (section !== "text" && section !== "combined") expect([base.text, local.text, remote.text]).toEqual([text, text, text]);
  if (section !== "pending-number" && section !== "pending-json" && section !== "combined") expect([base.pending, local.pending, remote.pending]).toEqual([undefined, undefined, undefined]);
  expect(classifyDraftObservation(record(1, base), local, { kind: "available", draft: record(2, remote) }, { kind: "none" }).kind).toBe("conflict");
});

// 若完整选择混合两侧、遗漏当前私有资料或采用额外保存，此测试失败。
it.each(sections.flatMap(section => (["adopt", "replace", "copy"] as const).map(operation => [section, operation] as const)))(
  "%s整份%s保留所选事实及写入边界", async (section, operation) => {
    const base = record(1, piece(section, "B")), local = piece(section, "L"), remote = record(2, piece(section, "R"));
    let writes = 0, creations = 0, transferred: Draft | undefined;
    const session = new DraftSession(base, { read: async () => remote, save: async (id, revision, content) => { expect([id, revision]).toEqual(["source", 2]); writes++; return record(3, content); } }, () => {});
    session.edit(local); bindSessionWholeView(session); session.observe(remote);
    const snapshot = session.prepareConflictSelection();
    expect(stringifyJson(snapshot.local)).toBe(stringifyJson(local));
    expect(stringifyJson(snapshot.remote.content)).toBe(stringifyJson(remote.content));
    if (operation === "adopt") session.adoptSavedVersion(snapshot);
    else if (operation === "replace") await session.replaceSavedVersion(snapshot);
    else {
      session.beginCopy({ create: async (attemptId, input) => { creations++; const target = { ...record(1, input), id: "target", updatedAt: "2026-10-12 00:00:00" }; return { kind: "created", creation: { attemptId, input, created: target }, target: { kind: "editable", draft: target } }; }, read: async () => { throw Error("正常创建无核实"); } });
      await session.retryCopy(session.copyDecision!, draft => ({ validate: () => {}, commit: () => { transferred = draft; } }));
      expect(transferred, session.copyOperation?.diagnostic).toBeDefined();
    }
    expect(writes).toBe(operation === "replace" ? 1 : 0);
    expect(creations).toBe(operation === "copy" ? 1 : 0);
    expect(stringifyJson(operation === "copy" ? transferred!.content : session.content)).toBe(stringifyJson(operation === "adopt" ? remote.content : local));
    expect(session.copyPendingDecision).toBe(false);
    expect(session.selectionPendingDecision).toBe(false);
  },
);
