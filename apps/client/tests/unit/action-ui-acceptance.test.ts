import { expect, it } from "vitest";
import { ActionUiIdentity } from "../../src/web/action-ui-identity";
import { DraftSession } from "../../src/web/session";
import type { Draft } from "../../src/server/models";
import { FollowOperation } from "../../src/web/followup";
import {
  appendContentAction,
  initializePreviewMetadata,
  coordinatePreviews,
} from "../../src/shared/automatic-previews";
import { loadCapabilities } from "../../src/shared/capabilities";

async function verified() {
  const baseline = draft(),
    action = { name: "C", type: "report_status", params: { scope: "full" } };
  const actual = {
    ...baseline,
    revision: 2,
    content: appendContentAction(baseline.content, action, true),
  };
  const op = new FollowOperation(baseline.id, action, {
    create: async () => baseline,
    prepare: async () => baseline,
    append: async () => actual,
    read: async () => actual,
  });
  await op.advance();
  return op.transition!;
}
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
it("已核实共同追加在session提交前准备最终UI映射，并幂等接纳", async () => {
  const { session, identity } = setup(),
    keys = [...identity.keys],
    transition = await verified();
  session.bindAppendView((value) => identity.prepareAppend(value));
  session.lockAppend();
  session.acceptAppend(transition);
  expect(identity.keys.slice(0, 2)).toEqual(keys);
  expect(identity.keys).toHaveLength(3);
  expect(session.revision).toBe(2);
  expect(session.appendLocked).toBe(false);
  const finalKeys = [...identity.keys],
    version = session.version;
  session.acceptAppend(transition);
  session.observe(transition.actual);
  expect(identity.keys).toEqual(finalKeys);
  expect(session.version).toBe(version);
});
it.each([
  "target",
  "baseline",
  "revision",
  "position",
  "actual",
  "view",
  "locked",
])("共同追加%s拒绝时正文版本与UI保持", async (difference) => {
  const { session, identity } = setup(),
    keys = [...identity.keys],
    transition = await verified(),
    content = session.content;
  session.bindAppendView((value) => identity.prepareAppend(value));
  session.lockAppend();
  if (difference === "target") transition.actual.id = "other";
  if (difference === "baseline")
    transition.baseline.content.pending = {
      "/name": { kind: "json", text: "{" },
    };
  if (difference === "revision") transition.baseline.revision = 3;
  if (difference === "position") transition.index = 0;
  if (difference === "actual")
    transition.actual.content.text = '{"actions":[]}';
  if (difference === "view")
    identity.update({ text: '{"actions":[{"name":"X"},{"name":"Y"}]}' });
  if (difference === "locked") session.exportState = "exporting";
  expect(() => session.acceptAppend(transition)).toThrow();
  expect(session.content).toBe(content);
  expect(session.revision).toBe(1);
  expect(session.version).toBe(0);
  expect(session.appendLocked).toBe(true);
  expect(identity.keys).toEqual(keys);
});
it("没有挂载视图的目标独立接纳，新生命周期不使用其他草稿keys", async () => {
  const { session } = setup(),
    transition = await verified();
  session.lockAppend();
  session.acceptAppend(transition);
  expect(session.content).toEqual(transition.actual.content);
  const newView = new ActionUiIdentity(session.content);
  expect(newView.keys).toHaveLength(3);
  expect(session.revision).toBe(2);
});
it("挂载视图预检拒绝时保留已提交事实，仅本地重试接纳", async () => {
  const { session, identity } = setup(),
    transition = await verified(),
    old = session.content;
  const release = session.bindAppendView(() => {
    throw Error("视图尚未准备");
  });
  session.lockAppend();
  expect(() => session.acceptAppend(transition)).toThrow();
  expect(session.content).toBe(old);
  expect(session.revision).toBe(1);
  release();
  session.bindAppendView((value) => identity.prepareAppend(value));
  session.acceptAppend(transition);
  expect(session.revision).toBe(2);
  expect(identity.keys).toHaveLength(3);
});
const capture = (name: string) => ({
  name,
  type: "camera_record",
  device_id: "cam",
  scheduled_at: "2026-10-10 01:00:00",
  params: { type: "photo" },
  policy: { max_delay_ms: 0 },
});
const catalog = (support: boolean) => ({
  active: loadCapabilities({
    devices: [
      {
        device_id: "cam",
        driver_id: "test",
        actions: [
          {
            type: "camera_record",
            parameter_types: [
              {
                type: "photo",
                name: "照片",
                description: "照片",
                preview_supported: support,
                schema: {
                  $schema: "https://json-schema.org/draft/2020-12/schema",
                  type: "object",
                  required: ["type"],
                  properties: { type: { const: "photo" } },
                  additionalProperties: false,
                },
              },
            ],
          },
        ],
      },
    ],
  }),
  error: null,
  generation: support ? 1 : 2,
  version: support ? "v1" : "v2",
});
async function derivedTransition(beforeAppend: boolean) {
  const origin = {
    ...draft(),
    content: coordinatePreviews(
      initializePreviewMetadata(
        { text: JSON.stringify({ actions: [capture("A"), capture("B")] }) },
        "enabled",
        "n",
      ),
      catalog(true),
    ).content,
  };
  const action = {
    name: "C",
    type: "report_status",
    params: { scope: "full" },
  };
  let capabilities = catalog(true);
  const base = {
    ...origin,
    revision: 2,
    content: coordinatePreviews(origin.content, catalog(false)).content,
  };
  const expected = appendContentAction(origin.content, action, true);
  const actual = beforeAppend
    ? {
        ...base,
        revision: 3,
        content: appendContentAction(base.content, action, true),
      }
    : {
        ...origin,
        revision: 3,
        content: coordinatePreviews(expected, catalog(false)).content,
        lastWrite: { revision: 2, input: expected, content: expected },
      };
  let calls = 0;
  const op = new FollowOperation(origin.id, action, {
    capabilities: () => capabilities,
    create: async () => origin,
    prepare: async () => origin,
    append: async () => {
      calls++;
      capabilities = catalog(false);
      if (beforeAppend && calls === 1) throw Error("未提交");
      return actual;
    },
    read: async () => base,
  });
  await op.advance();
  if (beforeAppend) await op.advance();
  const session = new DraftSession(
    origin,
    { save: async () => origin, read: async () => actual },
    () => {},
  );
  const identity = new ActionUiIdentity(session.content);
  session.bindAppendView((value) => identity.prepareAppend(value));
  session.lockAppend();
  return { session, identity, transition: op.transition!, op };
}
it.each([false, true])(
  "可靠ID解释%s阶段的派生删除，存续动作保留自己的键",
  async (beforeAppend) => {
    const { session, identity, transition } =
        await derivedTransition(beforeAppend),
      keys = [...identity.keys];
    session.acceptAppend(transition);
    expect(keys).toHaveLength(4);
    expect(identity.keys.slice(0, 2)).toEqual([keys[0], keys[1]]);
    expect(keys).not.toContain(identity.keys[2]);
    expect(session.revision).toBe(3);
  },
);
it.each(["target", "revision", "break", "repeat"])(
  "基线派生链%s错误在任何提交前拒绝",
  async (difference) => {
    const { session, identity, transition } = await derivedTransition(true),
      keys = [...identity.keys],
      content = session.content;
    if (difference === "target")
      transition.baselineChanges[0].actual.id = "other";
    if (difference === "revision")
      transition.baselineChanges[0].actual.revision = 1;
    if (difference === "break") transition.baselineChanges = [];
    if (difference === "repeat")
      transition.baselineChanges.push(transition.baselineChanges[0]);
    expect(() => session.acceptAppend(transition)).toThrow();
    expect(session.content).toBe(content);
    expect(session.revision).toBe(1);
    expect(session.version).toBe(0);
    expect(identity.keys).toEqual(keys);
  },
);
it("已证明追加但后续实际结构没有ID映射时保留结果且不重发", async () => {
  const base = draft(),
    action = { name: "C", type: "report_status", params: { scope: "full" } },
    expected = appendContentAction(base.content, action, true);
  const actual = {
    ...base,
    revision: 3,
    content: {
      text: '{"actions":[{"name":"A"},{"name":"B"},{"name":"C"},{"name":"派生"}]}',
    },
    lastWrite: {
      revision: 2,
      input: expected,
      content: {
        text: '{"actions":[{"name":"A"},{"name":"B"},{"name":"C"},{"name":"派生"}]}',
      },
    },
  };
  let calls = 0;
  const op = new FollowOperation(base.id, action, {
    create: async () => base,
    prepare: async () => base,
    append: async () => {
      calls++;
      return actual;
    },
    read: async () => actual,
  });
  await op.advance();
  const { session, identity } = setup(),
    keys = [...identity.keys],
    content = session.content;
  session.bindAppendView((value) => identity.prepareAppend(value));
  session.lockAppend();
  expect(() => session.acceptAppend(op.transition!)).toThrow();
  op.acceptanceFailed(Error("派生结构缺少身份映射"));
  await op.advance();
  expect(op.result).toEqual(actual);
  expect(calls).toBe(1);
  expect(session.content).toBe(content);
  expect(session.revision).toBe(1);
  expect(identity.keys).toEqual(keys);
});
it("最终实际映射不可解释时，局部编辑也在session提交前拒绝", () => {
  const { session, identity } = setup(),
    keys = [...identity.keys];
  const next = { text: '{"actions":[{"name":"A"},{"name":"B"},{"name":"C"}]}' };
  const actual = {
    text: '{"actions":[{"name":"A"},{"name":"B"},{"name":"C"},{"name":"D"}]}',
  };
  let accepted = false;
  expect(() =>
    identity.update(
      next,
      "append",
      () => {
        accepted = true;
        session.edit(actual);
      },
      undefined,
      actual,
    ),
  ).toThrow();
  expect(accepted).toBe(false);
  expect(session.version).toBe(0);
  expect(identity.keys).toEqual(keys);
});
it("编辑准备后session状态改变，同步拒绝仍保留正文和UI", () => {
  const { session, identity } = setup(),
    keys = [...identity.keys];
  const next = { text: '{"actions":[{"name":"X"}]}' },
    prepared = session.prepareEdit(next);
  session.lockAppend();
  expect(() =>
    identity.update(
      next,
      "reset",
      () => session.commitEdit(prepared),
      undefined,
      prepared.content,
    ),
  ).toThrow();
  expect(session.content).toEqual(draft().content);
  expect(session.version).toBe(0);
  expect(identity.keys).toEqual(keys);
});
it("UI解除挂载之后不再接纳前一个生命周期的keys", async () => {
  const { session, identity } = setup(),
    keys = [...identity.keys],
    transition = await verified();
  const unmount = session.bindAppendView((value) =>
    identity.prepareAppend(value),
  );
  unmount();
  session.lockAppend();
  session.acceptAppend(transition);
  expect(identity.keys).toEqual(keys);
  expect(session.content).toEqual(transition.actual.content);
  expect(new ActionUiIdentity(session.content).keys).toHaveLength(3);
});
it("已消费转换重复回调不能回退后续本地输入或复用旧键", async () => {
  const { session, identity } = setup(),
    transition = await verified();
  session.bindAppendView((value) => identity.prepareAppend(value));
  session.lockAppend();
  session.acceptAppend(transition);
  const next = { text: '{"actions":[{"name":"X"}]}' },
    prepared = session.prepareEdit(next);
  identity.update(
    next,
    "reset",
    () => session.commitEdit(prepared),
    undefined,
    prepared.content,
  );
  const keys = [...identity.keys],
    version = session.version;
  session.acceptAppend(transition);
  session.observe(transition.baseline);
  expect(session.content).toEqual(next);
  expect(session.version).toBe(version);
  expect(identity.keys).toEqual(keys);
});
