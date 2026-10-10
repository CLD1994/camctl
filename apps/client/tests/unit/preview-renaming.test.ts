import { expect, it } from "vitest";
import { editValue, setValue } from "../../src/web/editing";
import { coordinatePreviews } from "../../src/shared/automatic-previews";
import { linked } from "../helpers/preview-renaming";
it.each([0, "0"])("名称恢复的数字及Pointer路径等价 %s", (index) => {
  const c = linked();
  c.pending = { "/actions/0/name": { kind: "json", text: '"unfinished' } };
  const result = editValue(c, ["actions", index, "name"], '"C"', "json");
  expect(JSON.parse(result.text).actions[2].params.source.action_name).toBe(
    "C",
  );
  expect(result.automaticPreviews!.actions[2]).toMatchObject({
    id: "n:2",
    sourceId: "n:0",
  });
  expect(result.pending).toEqual({});
});
it.each(["B", "", 42, null, undefined])(
  "暂时非法名称可恢复原归属 %s",
  (value) => {
    const temporary = setValue(
      linked(),
      ["actions", 0, "name"],
      value,
      value === undefined,
    );
    const result = setValue(temporary, ["actions", 0, "name"], "C");
    expect(
      JSON.parse(result.text)
        .actions.slice(2)
        .map((a: any) => a.params.source.action_name),
    ).toEqual(["C", "B", "A"]);
    expect(
      result.automaticPreviews!.actions.map((a) => [a.id, a.sourceId]),
    ).toEqual([
      ["n:0", undefined],
      ["n:1", undefined],
      ["n:2", "n:0"],
      ["n:3", "n:1"],
      ["n:4", undefined],
    ]);
  },
);
it.each(["00", "01", "-1", "1.0", "1e0", " 0", -1, 0.5, 99])(
  "拒绝非法数组索引且原输入不变 %s",
  (index) => {
    const c = linked(),
      before = JSON.stringify(c);
    expect(() => setValue(c, ["actions", index, "name"], "C")).toThrow();
    expect(JSON.stringify(c)).toBe(before);
  },
);
it("两位数动作索引与参数数字对象键按实际容器识别", () => {
  const c = linked(),
    root = JSON.parse(c.text);
  for (let i = 5; i <= 12; i++) {
    root.actions.push({ name: `额外${i}`, type: "report_status" });
    c.automaticPreviews!.actions.push({ id: `n:${i}` });
  }
  root.actions[12] = root.actions[0];
  root.actions[0] = { name: "占位", type: "report_status" };
  c.automaticPreviews!.actions[2].sourceId = "n:12";
  c.text = JSON.stringify(root);
  const result = setValue(c, ["actions", "12", "name"], "C");
  expect(JSON.parse(result.text).actions[2].params.source.action_name).toBe(
    "C",
  );
  const keyed = setValue(result, ["actions", "12", "params", "01"], "数字键");
  expect(JSON.parse(keyed.text).actions[12].params["01"]).toBe("数字键");
});
it("未完成名称保留恢复依据后按Pointer恢复", () => {
  const pending = editValue(
    linked(),
    ["actions", 0, "name"],
    '"unfinished',
    "json",
  );
  const result = editValue(pending, ["actions", "0", "name"], '"C"', "json");
  expect(JSON.parse(result.text).actions[2].params.source.action_name).toBe(
    "C",
  );
});
it.each(["B", "", 42, undefined])(
  "暂时无效名称保持最后可靠投影 %s",
  (value) => {
    const c = setValue(
      linked(),
      ["actions", 0, "name"],
      value,
      value === undefined,
    );
    expect(JSON.parse(c.text).actions[2].params.source.action_name).toBe("A");
    expect(
      coordinatePreviews(c, { active: null, error: "缺目录", generation: 0 })
        .content,
    ).toEqual(c);
  },
);
it.each(["source", "pending", "duplicate", "missing", "external"])(
  "无法证明的关联不由改名覆盖 %s",
  (kind) => {
    let c = setValue(linked(), ["actions", 0, "name"], "B"),
      root = JSON.parse(c.text);
    if (kind === "source") root.actions[2].params.source.action_name = "外部";
    if (kind === "pending")
      c.pending = { "/actions/2/params/source": { kind: "json", text: "{" } };
    if (kind === "duplicate") c.automaticPreviews!.actions[3].sourceId = "n:0";
    if (kind === "missing") c.automaticPreviews!.actions[0].id = "missing";
    if (kind === "external") {
      c = linked();
      root = JSON.parse(c.text);
      root.actions[0].name = "B";
    }
    c.text = JSON.stringify(root);
    const before = root.actions
      .slice(2)
      .map((a: any) => a.params.source.action_name);
    const result = setValue(c, ["actions", 0, "name"], "C");
    expect(
      JSON.parse(result.text)
        .actions.slice(2)
        .map((a: any) => a.params.source.action_name),
    ).toEqual(before);
    expect(
      coordinatePreviews(result, { active: null, error: null, generation: 0 })
        .issues.length,
    ).toBeGreaterThan(0);
  },
);
it("无关动作业务错误不阻止可靠旧图建立局部改名依据", () => {
  const c = linked(),
    root = JSON.parse(c.text);
  root.actions.push({ type: "report_status" });
  c.text = JSON.stringify(root);
  c.automaticPreviews!.actions.push({ id: "n:5" });
  const temporary = setValue(c, ["actions", 0, "name"], "B");
  const result = setValue(temporary, ["actions", 0, "name"], "C");
  expect(JSON.parse(result.text).actions[2].params.source.action_name).toBe(
    "C",
  );
});
it("已保存投影不能被改动的sourceId或自动id重新解释", () => {
  const c = setValue(linked(), ["actions", 0, "name"], "B");
  c.automaticPreviews!.actions[2].sourceId = "n:1";
  c.automaticPreviews!.actions[3].sourceId = "n:0";
  const result = setValue(c, ["actions", 0, "name"], "C");
  expect(JSON.parse(result.text).actions[3].params.source.action_name).toBe(
    "B",
  );
});
it.each(["enabled", "disabled", "unset"] as const)(
  "三态名称编辑只维护开启的可靠关联 %s",
  (intent) => {
    const c = linked();
    c.automaticPreviews!.intent = intent;
    const result = setValue(c, ["actions", 0, "name"], "C");
    expect(JSON.parse(result.text).actions[2].params.source.action_name).toBe(
      intent === "enabled" ? "C" : "A",
    );
  },
);
it("完整版本比较区分同正文的恢复依据", async () => {
  const { sameContent } = await import("../../src/shared/automatic-previews");
  const c = setValue(linked(), ["actions", 0, "name"], "B"),
    other = structuredClone(c);
  other.automaticPreviews!.actions[2].rename!.pending = false;
  expect(sameContent(c, other)).toBe(false);
});
it("复制草稿在待恢复状态重映射两个身份，删除来源也移除原自动项", async () => {
  const { copyDraftContent } =
    await import("../../src/shared/automatic-previews");
  const { removeAction } = await import("../../src/web/editing");
  const c = setValue(linked(), ["actions", 0, "name"], "B");
  const copied = copyDraftContent(c, "copy");
  const result = setValue(copied, ["actions", "0", "name"], "C");
  expect(JSON.parse(result.text).actions[2].params.source.action_name).toBe(
    "C",
  );
  expect(result.automaticPreviews!.actions[2].rename).toEqual({
    sourceId: "copy:0",
    automaticId: "copy:2",
    actionName: "C",
    pending: false,
  });
  const removed = removeAction(c, 0);
  expect(JSON.parse(removed.text).actions.map((a: any) => a.name)).toEqual([
    "B",
    "自动B",
    "手动",
  ]);
});
it("改名的解析和协调保留原数字事实", () => {
  const c = linked();
  c.text = c.text.replace(
    '"name":"计划"',
    '"name":"计划","extra":1.0000000000000001',
  );
  const result = setValue(
    setValue(c, ["actions", 0, "name"], "B"),
    ["actions", "0", "name"],
    "C",
  );
  expect(result.text).toContain("1.0000000000000001");
});
it("自动id变化也不能沿旧依据更新投影", () => {
  const c = setValue(linked(), ["actions", 0, "name"], "B");
  c.automaticPreviews!.actions[2].id = "different";
  const result = setValue(c, ["actions", 0, "name"], "C");
  expect(JSON.parse(result.text).actions[2].params.source.action_name).toBe(
    "A",
  );
});
it("Pointer电机整数恢复与数字路径使用同一分类", () => {
  const c = {
    text: JSON.stringify({
      actions: [{ type: "motor_control", params: { position: 0 } }],
    }),
  };
  const result = editValue(
    c,
    ["actions", "0", "params", "position"],
    "9007199254740993",
    "number",
  );
  expect(result.text).toContain("9007199254740993");
});
it("畸形动作只能显式替换整个值，不能通过名称路径隐式重建", () => {
  const c = { text: '{"actions":[null]}' };
  expect(() => setValue(c, ["actions", "0", "name"], "C")).toThrow();
  const replaced = setValue(c, ["actions", "0"], {
    name: "C",
    type: "report_status",
  });
  expect(JSON.parse(replaced.text).actions).toEqual([
    { name: "C", type: "report_status" },
  ]);
});
it("修改的pending只清除对应路径", () => {
  const c = linked();
  c.pending = {
    "/actions/0/name": { kind: "json", text: '"C"' },
    "/name": { kind: "json", text: '"待完成' },
  };
  const result = editValue(c, ["actions", "0", "name"], '"C"', "json");
  expect(result.pending).toEqual({
    "/name": { kind: "json", text: '"待完成' },
  });
});
