import { expect, it } from "vitest";
import { editValue, setValue } from "../../src/web/editing";
import {
  initializePreviewMetadata,
  previewIntent,
} from "../../src/shared/automatic-previews";
import { stringifyJson, parseClientJson } from "../../src/shared/json";
import type { DraftContent } from "../../src/server/models";
function content(intent?: "enabled" | "disabled"): DraftContent {
  const c: DraftContent = {
    text: '{"name":"旧","measurement":1.0000000000000001,"actions":[{"name":"A"},{"name":"B"}]}',
    pending: {
      "/actions": { kind: "json", text: "[" },
      "/actions/0/name": { kind: "json", text: '"未完成' },
      "/name": { kind: "json", text: '"保留' },
      "/actions_extra": { kind: "json", text: "[" },
      "/actions~1other/0": { kind: "json", text: "[" },
      "/actions/~2bad": { kind: "json", text: "[" },
      bad: { kind: "json", text: "[" },
    },
    actionVariants: {
      "1": [
        {
          type: "report_status",
          fields: { params: { scope: "full" } },
          pending: {},
        },
      ],
    },
  };
  return intent ? initializePreviewMetadata(c, intent, "stable") : c;
}
it.each([undefined, "enabled", "disabled"] as const)(
  "整组替换未经确认保持%s资料和所有原文",
  (intent) => {
    const c = content(intent),
      before = stringifyJson(c);
    expect(() =>
      editValue(c, ["actions"], '[{"name":"X"},{"name":"Y"}]', "json"),
    ).toThrow();
    expect(() => setValue(c, ["actions"], [])).toThrow();
    expect(() => setValue(c, ["actions"], undefined, true)).toThrow();
    expect(stringifyJson(c)).toBe(before);
  },
);
it.each([
  {
    intent: undefined,
    text: '[{"name":"X"},{"name":"Y"}]',
    expected: [{ name: "X" }, { name: "Y" }],
  },
  { intent: "enabled", text: '[{"name":"X"}]', expected: [{ name: "X" }] },
  { intent: "disabled", text: "[]", expected: [] },
  { intent: "enabled", text: '[null,3,"未知"]', expected: [null, 3, "未知"] },
] as const)(
  "确认整组替换清理资料并保存精确新数组 $text",
  ({ intent, text, expected }) => {
    const c = content(intent);
    const next = editValue(c, ["actions"], text, "json", true);
    expect((parseClientJson(next.text) as any).actions).toEqual(expected);
    expect(next.text).toContain("1.0000000000000001");
    expect(next.automaticPreviews).toBeUndefined();
    expect(next.actionVariants).toBeUndefined();
    expect(previewIntent(next)).toBe("unset");
    expect(next.pending).toEqual({
      "/name": c.pending!["/name"],
      "/actions_extra": c.pending!["/actions_extra"],
      "/actions~1other/0": c.pending!["/actions~1other/0"],
      "/actions/~2bad": c.pending!["/actions/~2bad"],
      bad: c.pending!.bad,
    });
  },
);
it("确认整个字段移除不补造空数组，同时清理资料和集合内pending", () => {
  const c = content("disabled");
  const next = setValue(
    c,
    ["actions"],
    undefined,
    true,
    false,
    undefined,
    true,
  );
  expect(Object.hasOwn(parseClientJson(next.text) as object, "actions")).toBe(
    false,
  );
  expect(next.automaticPreviews).toBeUndefined();
  expect(next.actionVariants).toBeUndefined();
  expect(next.pending?.["/name"]).toEqual(c.pending!["/name"]);
  expect(next.pending?.["/actions"]).toBeUndefined();
  expect(next.text).toContain("1.0000000000000001");
});
it.each(["[", '[{"x":1,"x":2}]', "null", "{}", "3", '"数组"'])(
  "非法整组候选%s不改正文或任何资料",
  (text) => {
    const c = content("enabled");
    c.pending = { "/actions": { kind: "json", text } };
    const before = stringifyJson(c);
    expect(() => editValue(c, ["actions"], text, "json", true)).toThrow();
    expect(stringifyJson(c)).toBe(before);
  },
);
it.each(['{"name":', "null", "[]", "3"])(
  "未知根正文%s不能由候选数组补造计划",
  (text) => {
    const c = {
        ...content("enabled"),
        text,
        pending: { "/actions": { kind: "json" as const, text: "[]" } },
      },
      before = stringifyJson(c);
    expect(() => editValue(c, ["actions"], "[]", "json", true)).toThrow();
    expect(() =>
      setValue(c, ["actions"], undefined, true, false, undefined, true),
    ).toThrow();
    expect(stringifyJson(c)).toBe(before);
  },
);
it("根对象祖先pending阻止整数组替换和移除，保留全部输入", () => {
  const c = content("enabled");
  c.pending![""] = { kind: "json", text: '{"name":' };
  const before = stringifyJson(c);
  expect(() => editValue(c, ["actions"], "[]", "json", true)).toThrow();
  expect(() =>
    setValue(c, ["actions"], undefined, true, false, undefined, true),
  ).toThrow();
  expect(stringifyJson(c)).toBe(before);
});
it("直接设置未定义值不解释为移除actions字段", () => {
  const c = { text: '{"name":"旧","actions":[]}' };
  expect(() =>
    setValue(c, ["actions"], undefined, false, false, undefined, true),
  ).toThrow();
  expect(c.text).toBe('{"name":"旧","actions":[]}');
});
it("结构替换可保存业务非法Unicode和电机数值，公共校验资格不阻止私有输入", () => {
  const c = { text: '{"name":"旧","extra":"\\ud800","actions":[]}' };
  const next = editValue(
    c,
    ["actions"],
    '[{"name":"\\ud800","type":"motor_control","params":{"position":1.0000000000000001}}]',
    "json",
    true,
  );
  expect(next.text).toContain("\\ud800");
  expect(next.text).toContain("1.0000000000000001");
  expect((parseClientJson(next.text) as any).extra).toBe("\ud800");
  expect(next.automaticPreviews).toBeUndefined();
});
it("当前对象缺少actions时确认数组仍能修正，精确保留数组内数字词元", () => {
  const next = editValue(
    {
      text: '{"name":"修正"}',
      pending: { "/actions": { kind: "json", text: "[" } },
    },
    ["actions"],
    '[{"params":{"value":1.0000000000000001}}]',
    "json",
    true,
  );
  expect(next.text).toContain("1.0000000000000001");
  expect(next.pending).toEqual({});
});
