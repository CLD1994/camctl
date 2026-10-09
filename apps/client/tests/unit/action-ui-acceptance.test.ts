import { expect, it } from "vitest";
import { ActionUiIdentity } from "../../src/web/action-ui-identity";
import { DraftSession } from "../../src/web/session";
import type { Draft } from "../../src/server/models";
const draft = (): Draft => ({
  id: "test",
  revision: 1,
  createdAt: "2026-10-10 00:00:00",
  updatedAt: "2026-10-10 00:00:00",
  content: { text: '{"actions":[{"name":"A"},{"name":"B"}]}' },
});
function setup() {
  const d = draft();
  const s = new DraftSession(
    d,
    {
      save: async () => {
        throw Error("本测试不发送保存");
      },
      read: async () => d,
    },
    () => {},
  );
  return { session: s, identity: new ActionUiIdentity(s.content) };
}
it.each(["append", "export", "delete"])(
  "session因%s同步拒绝时UI结构和正文都不变",
  (stage) => {
    const { session, identity } = setup(),
      keys = [...identity.keys],
      before = session.content;
    if (stage === "append") session.appendLocked = true;
    if (stage === "export") session.exportState = "exporting";
    if (stage === "delete") session.deletionState = "deleting";
    expect(() =>
      identity.update({ text: '{"actions":[{"name":"X"}]}' }, "reset", () => {
        session.edit({ text: '{"actions":[{"name":"X"}]}' });
        return session.content;
      }),
    ).toThrow();
    expect(identity.keys).toEqual(keys);
    expect(session.content).toBe(before);
  },
);
it("身份映射未知时在session接纳之前报错，正文版本保持", () => {
  const { session, identity } = setup(),
    keys = [...identity.keys];
  const next = { text: '{"actions":[{"name":"A"},{"name":"B"},{"name":"C"}]}' };
  expect(() =>
    identity.update(next, undefined, () => {
      session.edit(next);
      return session.content;
    }),
  ).toThrow();
  expect(session.version).toBe(0);
  expect(session.content.text).toBe(draft().content.text);
  expect(identity.keys).toEqual(keys);
});
it("session可靠接纳后才提交新UI结构，旧键不交给新动作", () => {
  const { session, identity } = setup(),
    keys = [...identity.keys];
  const next = { text: '{"actions":[{"name":"X"}]}' };
  identity.update(next, "reset", () => {
    expect(identity.keys).toEqual(keys);
    session.edit(next);
    return session.content;
  });
  expect(session.content).toEqual(next);
  expect(identity.keys).toHaveLength(1);
  expect(keys).not.toContain(identity.keys[0]);
});
