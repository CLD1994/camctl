import { expect, it } from "vitest";
import { DraftSession } from "../../src/web/session";
import { bindSessionWholeView } from "../../src/web/draft-session-view";
import { completeDraftInput, record } from "../helpers/draft-conflicts";
import { parseClientJson, stringifyJson } from "../../src/shared/json";

// 自动同步丢任一完整资料或把接纳重新写入后端时失败；预期来自独立完整输入字面量。
it.each([false, true])("清洁或恢复完整B后普通观察接纳六部分R且零写入，恢复=%s", async restore => {
  const base = { ...record(1, "base"), content: completeDraftInput() };
  const remote = { ...record(2, "remote"), content: parseClientJson(
    '{"text":"{\\"name\\":\\"remote\\",\\"actions\\":[{\\"name\\":\\"R\\",\\"type\\":\\"report_status\\",\\"params\\":{\\"scope\\":\\"full\\"}}]}","pending":{"/amount":{"kind":"number","text":"1e"}},"actionVariants":{"0":[{"type":"camera_record","fields":{"n":1.00000000000000001},"pending":{}}]},"parameterVariants":{"0":[{"paramsText":"{\\"params\\":{\\"type\\":\\"photo\\",\\"n\\":1e-998}}","pending":{}}]},"objectVariants":{"0":{"/x":{"discriminator":"mode","variants":[{"valueText":"{\\"value\\":{\\"mode\\":\\"auto\\"}}","pending":{}}]}}},"automaticPreviews":{"namespace":"remote","next":1,"intent":"disabled","actions":[{"id":"remote:0"}]}}',
  ) as typeof base.content };
  let writes = 0;
  const session = new DraftSession(base, { save: async () => { writes++; return remote; }, read: async () => remote }, () => {});
  bindSessionWholeView(session);
  if (restore) { session.edit({ ...base.content, pending: { "/amount": { kind: "number", text: "+" } } }); session.edit(base.content); }
  session.observe(remote);
  for (const part of ["text", "pending", "actionVariants", "parameterVariants", "objectVariants", "automaticPreviews"] as const)
    expect(stringifyJson(session.content[part])).toBe(stringifyJson(remote.content[part]));
  expect(session.confirmedDraft).toEqual(remote); expect(session.revision).toBe(2);
  expect(session.saved).toBe(true); expect(session.needsLeaveWarning).toBe(false);
  await session.flush(); expect(writes).toBe(0);
});
