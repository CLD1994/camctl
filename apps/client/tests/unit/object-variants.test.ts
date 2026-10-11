import { expect, it } from "vitest";
import type { DraftContent } from "../../src/server/models";
import {
  sameContent,
  copyDraftAction,
  removeDraftActions,
} from "../../src/shared/automatic-previews";
import { parseClientJson, stringifyJson } from "../../src/shared/json";
import { setValue, editPlanText } from "../../src/web/editing";
import { switchActionType } from "../../src/web/action-drafts";
import { switchParameterType } from "../../src/shared/parameter-variants";
import {
  switchObjectBranch,
  canSwitchObjectBranch,
  replaceObjectValue,
} from "../../src/shared/object-variants";

const source = (): DraftContent =>
  ({
    text: '{"actions":[{"type":"camera_record","params":{"type":"standard","exposure":{"mode":"auto","compensation_ev":0},"other":8}}]}',
    objectVariants: {
      "0": {
        "/exposure": {
          discriminator: "mode",
          variants: [
            {
              valueText:
                '{"value":{"mode":"manual","iso":100,"exact":1.0000000000000001}}',
              pending: { "/iso": { kind: "number", text: "1e" } },
            },
          ],
        },
      },
    },
  }) as DraftContent;
const exposure = (content: DraftContent) =>
  (parseClientJson(content.text) as any).actions[0].params.exposure;

it("完整保存比较发现仅停用对象分支发生变化", () => {
  const before = source();
  const after = JSON.parse(JSON.stringify(before));
  after.objectVariants["0"]["/exposure"].variants[0].pending["/iso"].text =
    "2e";
  expect(sameContent(before, after)).toBe(false);
});

it("判别字段切回恢复所选模式的对象及未完成成员", () => {
  const next = setValue(
    source(),
    ["actions", 0, "params", "exposure", "mode"],
    "manual",
  );
  expect(exposure(next)).toEqual({ mode: "manual", iso: 100, exact: 1 });
  expect(next.text).toContain("1.0000000000000001");
  expect(next.pending).toEqual({
    "/actions/0/params/exposure/iso": { kind: "number", text: "1e" },
  });
});

it("复制动作共同复制停用对象分支且两份输入独立", () => {
  const next = copyDraftAction(source(), 0) as any;
  expect(next.objectVariants["1"]).toEqual(next.objectVariants["0"]);
  next.objectVariants["1"]["/exposure"].variants[0].pending["/iso"].text = "2e";
  expect(
    next.objectVariants["0"]["/exposure"].variants[0].pending["/iso"].text,
  ).toBe("1e");
});

it("删除前项后对象资料随原动作移动", () => {
  const initial = source() as any;
  const root = parseClientJson(initial.text) as any;
  root.actions.unshift({ type: "report_status" });
  initial.text = stringifyJson(root);
  initial.objectVariants = { "1": initial.objectVariants["0"] };
  const next = removeDraftActions(initial, new Set([0])) as any;
  expect(Object.keys(next.objectVariants)).toEqual(["0"]);
  expect(
    next.objectVariants["0"]["/exposure"].variants[0].pending["/iso"].text,
  ).toBe("1e");
});

it("仅有对象停用资料时整份替换仍要求清除确认", () => {
  expect(() => editPlanText(source(), '{"actions":[]}')).toThrow();
});

it("切换参数类型后对象资料随原参数分支保存和恢复", () => {
  const original = source();
  const next = switchParameterType(original, 0, "other");
  expect(next.objectVariants?.["0"]).toBeUndefined();
  expect(next.parameterVariants?.["0"]?.[0].objectVariants).toEqual(
    original.objectVariants?.["0"],
  );
  expect(
    switchParameterType(next, 0, "standard").objectVariants?.["0"],
  ).toEqual(original.objectVariants?.["0"]);
});
it("切换动作类型后对象资料随原动作分支保存和恢复", () => {
  const original = source();
  const next = switchActionType(original, 0, "report_status");
  expect(next.objectVariants?.["0"]).toBeUndefined();
  expect(next.actionVariants?.["0"]?.[0].objectVariants).toEqual(
    original.objectVariants?.["0"],
  );
  expect(
    switchActionType(next, 0, "camera_record").objectVariants?.["0"],
  ).toEqual(original.objectVariants?.["0"]);
});
it("明确替换整个参数时清除当前对象分支资料", () => {
  const next = setValue(source(), ["actions", 0, "params"], {
    type: "standard",
    exposure: { mode: "manual", iso: 400 },
  });
  expect(next.objectVariants?.["0"]).toBeUndefined();
});

it("首次模式选择仅写判别字段，切换当前模式不产生版本", () => {
  const initial: DraftContent = {
    text: '{"actions":[{"params":{"type":"standard","other":8}}]}',
  };
  const next = switchObjectBranch(initial, 0, ["exposure"], "mode", "manual");
  expect(exposure(next)).toEqual({ mode: "manual" });
  expect(switchObjectBranch(next, 0, ["exposure"], "mode", "manual")).toBe(
    next,
  );
});

it.each([
  "",
  "/actions",
  "/actions/0",
  "/actions/0/params",
  "/actions/0/params/exposure",
  "/actions/0/params/exposure/mode",
  "/actions/0/device_id",
  "/actions/0/type",
  "/actions/0/params/type",
])("身份未完成%s阻止模式选择且保留全部原文", (path) => {
  const initial = {
    ...source(),
    pending: { [path]: { kind: "json" as const, text: "{" } },
  };
  const before = stringifyJson(initial);
  expect(canSwitchObjectBranch(initial, 0, ["exposure"], "mode")).toBe(false);
  expect(() =>
    switchObjectBranch(initial, 0, ["exposure"], "mode", "manual"),
  ).toThrow();
  expect(stringifyJson(initial)).toBe(before);
});

it("明确重填只移除目标对象及其成员原文和分支", () => {
  const initial = {
    ...source(),
    pending: {
      "/actions/0/params/exposure": { kind: "json" as const, text: "auto" },
      "/actions/0/params/other": { kind: "number" as const, text: "1e" },
    },
  };
  const next = replaceObjectValue(initial, 0, ["exposure"], undefined, {
    omit: true,
    resetBranches: true,
  });
  expect(exposure(next)).toBeUndefined();
  expect(next.objectVariants?.["0"]).toBeUndefined();
  expect(next.pending).toEqual({
    "/actions/0/params/other": { kind: "number", text: "1e" },
  });
});

it("整体对象新值优先且离开模式可恢复数字与额外字段", () => {
  const initial = source();
  const next = replaceObjectValue(
    initial,
    0,
    ["exposure"],
    parseClientJson('{"mode":"manual","iso":400,"exact":1e-999}'),
  );
  expect(exposure(next)).toEqual({ mode: "manual", iso: 400, exact: 0 });
  expect(next.text).toContain("1e-999");
  expect(
    switchObjectBranch(next, 0, ["exposure"], "mode", "auto").text,
  ).toContain('"compensation_ev": 0');
});

it("整段动作替换清当前对象资料但保留停用动作分支", () => {
  const off = switchActionType(source(), 0, "report_status");
  const next = setValue(off, ["actions", 0], { type: "report_status" });
  expect(next.objectVariants?.["0"]).toBeUndefined();
  expect(next.actionVariants?.["0"]?.[0].objectVariants).toEqual(
    source().objectVariants?.["0"],
  );
});

it("明确替换嵌套对象祖先清除该范围后代分支并保持其他对象资料", () => {
  const initial = source();
  const root = parseClientJson(initial.text) as any;
  root.actions[0].params.container = {
    exposure: root.actions[0].params.exposure,
  };
  initial.text = stringifyJson(root);
  initial.objectVariants!["0"]["/container/exposure"] = structuredClone(
    initial.objectVariants!["0"]["/exposure"],
  );
  const next = setValue(initial, ["actions", 0, "params", "container"], {});
  expect(next.objectVariants!["0"]["/container/exposure"]).toBeUndefined();
  expect(next.objectVariants!["0"]["/exposure"]).toEqual(
    initial.objectVariants!["0"]["/exposure"],
  );
});

it("首次整体JSON替换使用已证明判别字段保存导入的原模式", () => {
  const initial: DraftContent = {
    text: '{"actions":[{"params":{"type":"standard","exposure":{"mode":"manual","iso":100,"exact":1.0000000000000001}}}]}',
  };
  const next = replaceObjectValue(initial, 0, ["exposure"], { mode: "auto" }, {
    discriminator: "mode",
  } as any);
  const restored = switchObjectBranch(next, 0, ["exposure"], "mode", "manual");
  expect(exposure(restored)).toEqual({ mode: "manual", iso: 100, exact: 1 });
  expect(restored.text).toContain("1.0000000000000001");
});
