import { expect, it } from "vitest";
import type { DraftContent } from "../../src/server/models";
import { switchActionType } from "../../src/web/action-drafts";
import {
  copyDraftAction,
  removeDraftActions,
} from "../../src/shared/automatic-previews";
import { parseDraft } from "../../src/web/editing";
import { stringifyJson } from "../../src/shared/json";

const original = (): DraftContent => ({
  text: '{"actions":[{"name":"A","type":"camera_record","params":{"type":"P","value":7}},{"name":"B","type":"camera_record","params":{"type":"Q"}}]}',
});
const operations = [
  ["switch", (c: DraftContent) => switchActionType(c, 0, "report_status")],
  ["copy", (c: DraftContent) => copyDraftAction(c, 0)],
  ["remove", (c: DraftContent) => removeDraftActions(c, new Set([0]))],
] as const;
for (const [name, run] of operations) {
  it.each([
    "/actions/01/params/ghost",
    "/actions/0/params/ghost~2",
    "/actions/9007199254740993/params/ghost",
    "/actions/2/params/ghost",
    "",
    "/actions",
  ])(`${name}对无法证明的路径%s失败且完整保留`, (path) => {
    const input = {
      ...original(),
      pending: { [path]: { kind: "json" as const, text: "{" } },
    };
    const before = stringifyJson(input);
    expect(() => run(input)).toThrow();
    expect(stringifyJson(input)).toBe(before);
  });
  it(`${name}不能为非对象动作的输入分配归属`, () => {
    const input = {
      text: '{"actions":[{"name":"A","type":"camera_record"},null]}',
      pending: {
        "/actions/1/params/value": { kind: "json" as const, text: "{" },
      },
    };
    const before = stringifyJson(input);
    expect(() => run(input)).toThrow();
    expect(stringifyJson(input)).toBe(before);
  });
}

it("删除只清已删除输入，合法其他动作和转义成员按真实表移位", () => {
  const input = {
    ...original(),
    pending: {
      "/actions/0/params/gone": { kind: "json" as const, text: "gone" },
      "/actions/1/params/a~1b~0c": { kind: "number" as const, text: "1e" },
      "/name": { kind: "json" as const, text: "plan" },
    },
  };
  const next = removeDraftActions(input, new Set([0]));
  expect(next.pending).toEqual({
    "/actions/0/params/a~1b~0c": { kind: "number", text: "1e" },
    "/name": { kind: "json", text: "plan" },
  });
  expect(parseDraft(next).actions[0].name).toBe("B");
});
it("复制只复制来源动作合法输入，其他范围保持", () => {
  const input = {
    ...original(),
    pending: {
      "/actions/0/params/a~1b~0c": { kind: "number" as const, text: "1e" },
      "/actions/1/params/other": { kind: "json" as const, text: "{" },
      "/name": { kind: "json" as const, text: "plan" },
    },
  };
  const next = copyDraftAction(input, 0);
  expect(next.pending).toEqual({
    ...input.pending,
    "/actions/2/params/a~1b~0c": { kind: "number", text: "1e" },
  });
});
it("外层切换合法转义成员往返保留，共用和其他范围输入不搬移", () => {
  const input = {
    ...original(),
    pending: {
      "/actions/0/params/a~1b~0c": { kind: "number" as const, text: "1e" },
      "/actions/0/name": { kind: "json" as const, text: "name" },
      "/actions/1/params/other": { kind: "json" as const, text: "{" },
      "/name": { kind: "json" as const, text: "plan" },
    },
  };
  const next = switchActionType(input, 0, "report_status");
  expect(next.actionVariants?.["0"]?.[0].pending).toEqual({
    "/params/a~1b~0c": { kind: "number", text: "1e" },
  });
  expect(switchActionType(next, 0, "camera_record").pending).toEqual(
    input.pending,
  );
});
