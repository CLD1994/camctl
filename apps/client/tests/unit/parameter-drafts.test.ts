import { expect, it } from "vitest";
import type { DraftContent } from "../../src/server/models";
import {
  switchParameterType,
  canSwitchParameterType,
} from "../../src/web/parameter-drafts";
import {
  parseDraft,
  editValue,
  setValue,
  editPlanText,
} from "../../src/web/editing";
import { switchActionType } from "../../src/web/action-drafts";
import {
  copyDraftAction,
  removeDraftActions,
  appendContentAction,
  sameContent,
} from "../../src/shared/automatic-previews";
import { parseClientJson, stringifyJson } from "../../src/shared/json";

const initial = (): DraftContent => ({
  text: '{"name":"计划","actions":[{"name":"A","type":"camera_record","device_id":"cam0","scheduled_at":"2026-10-10 01:00:00","params":{"type":"A","duration_s":60,"extra":{"tiny":1e-999,"long":1.0000000000000001}},"policy":{"max_delay_ms":0}}]}',
  pending: { "/actions/0/params/duration_s": { kind: "number", text: "60e" } },
});

it("首次参数类型不承接字段，切回恢复完整参数和成员原文", () => {
  const original = initial();
  const b = switchParameterType(original, 0, "B");
  expect(parseDraft(b).actions[0].params).toEqual({ type: "B" });
  expect(b.pending).toEqual({});
  expect(parseDraft(b).actions[0].policy).toEqual({ max_delay_ms: 0 });
  const a = switchParameterType(b, 0, "A");
  expect(a.pending).toEqual(original.pending);
  expect(a.text).toContain("1e-999");
  expect(a.text).toContain("1.0000000000000001");
  expect(parseDraft(a).actions).toEqual(parseDraft(original).actions);
  expect(switchParameterType(a, 0, "A")).toBe(a);
});

it("独立修改B后A和B分别恢复，不按设备拆分", () => {
  const original = initial();
  let next = switchParameterType(original, 0, "B");
  next = setValue(next, ["actions", 0, "params", "bitrate_mbps"], 70);
  next = setValue(next, ["actions", 0, "device_id"], "cam1");
  next = switchParameterType(next, 0, "A");
  expect(parseDraft(next).actions[0].device_id).toBe("cam1");
  expect(parseDraft(next).actions[0].params.duration_s).toBe(60);
  next = switchParameterType(next, 0, "B");
  expect(parseDraft(next).actions[0].params).toEqual({
    type: "B",
    bitrate_mbps: 70,
  });
});

it.each([undefined, null, [], false, 0, "raw", { extra: true }])(
  "未选择类型的完整形状%j可恢复",
  (params) => {
    const root = parseDraft(initial());
    if (params === undefined) delete root.actions[0].params;
    else root.actions[0].params = params;
    const original = { text: stringifyJson(root) };
    const restored = switchParameterType(
      switchParameterType(original, 0, "B"),
      0,
      undefined,
    );
    expect(Object.hasOwn(parseDraft(restored).actions[0], "params")).toBe(
      params !== undefined,
    );
    expect(parseDraft(restored).actions[0].params).toEqual(params);
  },
);

it.each(["future", null, 42, { kind: [0] }])(
  "未知实际类型%j仍独立恢复",
  (type) => {
    const original = {
      text: stringifyJson({ actions: [{ params: { type, extra: 7 } }] }),
    };
    expect(
      parseDraft(
        switchParameterType(switchParameterType(original, 0, "B"), 0, type),
      ).actions[0].params,
    ).toEqual({ type, extra: 7 });
  },
);

it.each([
  "",
  "/actions",
  "/actions/0",
  "/actions/0/type",
  "/actions/0/params",
  "/actions/0/params/type",
  "/actions/0/params/type/future",
  "/actions/01/params/value",
  "/actions/1/params/value",
  "/actions/0/params/~3",
])("边界或无法归属原文%s阻止切换且保留", (path) => {
  const input = {
    ...initial(),
    pending: { [path]: { kind: "json" as const, text: "{" } },
  };
  const before = stringifyJson(input);
  expect(canSwitchParameterType(input, 0)).toBe(false);
  expect(() => switchParameterType(input, 0, "B")).toThrow();
  expect(stringifyJson(input)).toBe(before);
});

it.each(["{", "[]", '{"params":{},"unexpected":true}'])(
  "损坏权威原文%s阻止转换",
  (paramsText) => {
    const input = {
      ...initial(),
      parameterVariants: { "0": [{ paramsText, pending: {} }] },
    };
    const before = stringifyJson(input);
    expect(canSwitchParameterType(input, 0)).toBe(false);
    expect(() => switchParameterType(input, 0, "B")).toThrow();
    expect(stringifyJson(input)).toBe(before);
  },
);

it("数学等价停用候选重复时拒绝而不任选", () => {
  const input = {
    ...initial(),
    parameterVariants: {
      "0": [
        { paramsText: '{"params":{"type":1}}', pending: {} },
        { paramsText: '{"params":{"type":1.0}}', pending: {} },
      ],
    },
  };
  const before = stringifyJson(input);
  expect(() => switchParameterType(input, 0, 1)).toThrow();
  expect(stringifyJson(input)).toBe(before);
});

it.each(["1e-999", "1.0000000000000001"])(
  "标量参数类型%s依据原事实选择恢复",
  (token) => {
    const input = {
      text: `{"actions":[{"params":{"type":${token},"value":3}}]}`,
    };
    const b = switchParameterType(input, 0, 0);
    expect(parseDraft(b).actions[0].params).toEqual({ type: 0 });
    const restored = editValue(
      b,
      ["actions", 0, "params", "type"],
      token,
      "json",
    );
    expect(restored.text).toContain(token);
    expect(parseDraft(restored).actions[0].params.value).toBe(3);
  },
);

it("嵌套类型数字与值搬移新父对象时保存原事实", () => {
  const input = {
    text: '{"actions":[{"params":{"type":{"future":[1e-999]},"long":1.0000000000000001}}]}',
  };
  const b = switchParameterType(input, 0, { future: [0] });
  expect(parseDraft(b).actions[0].params).toEqual({ type: { future: [0] } });
  const restored = editValue(
    b,
    ["actions", 0, "params", "type"],
    '{"future":[1e-999]}',
    "json",
  );
  expect(restored.text).toContain("1e-999");
  expect(restored.text).toContain("1.0000000000000001");
});

it.each(["1", "1.0", "1e0"])("数学等价当前类型%s不修改内容", (token) => {
  const input = {
    text: `{"actions":[{"params":{"type":${token},"value":3}}]}`,
  };
  expect(switchParameterType(input, 0, 1)).toBe(input);
});

it("完整参数JSON跨类型保存旧型且显式内容优先于已有缓存", () => {
  let next = switchParameterType({ ...initial(), pending: {} }, 0, "B");
  next = setValue(next, ["actions", 0, "params", "value"], 5);
  next = switchParameterType(next, 0, "A");
  next = editValue(
    next,
    ["actions", 0, "params"],
    '{"type":"B","value":8}',
    "json",
  );
  expect(parseDraft(next).actions[0].params).toEqual({ type: "B", value: 8 });
  expect(next.parameterVariants?.["0"]).toHaveLength(1);
  const a = switchParameterType(next, 0, "A");
  expect(parseDraft(a).actions[0].params.duration_s).toBe(60);
  expect(
    parseDraft(switchParameterType(a, 0, "B")).actions[0].params.value,
  ).toBe(8);
});

it("同类型全文只替换当前参数，其他类型资料保持", () => {
  const b = switchParameterType({ ...initial(), pending: {} }, 0, "B");
  const next = setValue(
    b,
    ["actions", 0, "params"],
    { type: "B", replacement: 7 },
    false,
    true,
  );
  expect(next.parameterVariants).toEqual(b.parameterVariants);
  expect(parseDraft(next).actions[0].params).toEqual({
    type: "B",
    replacement: 7,
  });
});

it("整体参数自己未完成原文成功应用时不反存旧型", () => {
  let next = editValue(
    { ...initial(), pending: {} },
    ["actions", 0, "params"],
    '{"type":"B",',
    "json",
  );
  next = editValue(
    next,
    ["actions", 0, "params"],
    '{"type":"B","value":3}',
    "json",
  );
  expect(next.pending).toEqual({});
  expect(next.parameterVariants?.["0"]?.[0].pending).toEqual({});
  expect(
    parseDraft(switchParameterType(next, 0, "A")).actions[0].params.duration_s,
  ).toBe(60);
});

it("参数类型自己的原文成功应用时原子消解后恢复内容", () => {
  let next = switchParameterType({ ...initial(), pending: {} }, 0, "B");
  next = editValue(next, ["actions", 0, "params", "type"], '"A', "json");
  expect(canSwitchParameterType(next, 0)).toBe(false);
  next = editValue(next, ["actions", 0, "params", "type"], '"A"', "json");
  expect(next.pending).toEqual({});
  expect(parseDraft(next).actions[0].params.duration_s).toBe(60);
});

it.each(["{", '{"type":"B","type":"A"}'])(
  "无效JSON%s仅保存原文不转换",
  (text) => {
    const input = { ...initial(), pending: {} };
    const next = editValue(input, ["actions", 0, "params"], text, "json");
    expect(next.text).toBe(input.text);
    expect(next.parameterVariants).toBeUndefined();
    expect(next.pending?.["/actions/0/params"]?.text).toBe(text);
  },
);

it.each(["null", "[]", "1e-999"])(
  "显式参数全文%s保留提供形状，不从未选择缓存恢复",
  (text) => {
    let next = switchParameterType(
      { text: '{"actions":[{"params":false}]}' },
      0,
      "A",
    );
    next = editValue(next, ["actions", 0, "params"], text, "json");
    expect(next.text).toContain(`"params": ${text}`);
    expect(next.parameterVariants?.["0"]).toHaveLength(1);
  },
);

it("明确移除params与恢复未选择缓存不同，最终字段不存在", () => {
  let next = switchParameterType(
    { text: '{"actions":[{"params":null}]}' },
    0,
    "A",
  );
  next = editValue(next, ["actions", 0, "params"], "{", "json");
  next = setValue(next, ["actions", 0, "params"], undefined, true);
  expect(Object.hasOwn(parseDraft(next).actions[0], "params")).toBe(false);
  expect(next.pending).toEqual({});
  expect(
    parseDraft(switchParameterType(next, 0, "A")).actions[0].params,
  ).toEqual({ type: "A" });
});

it("外层分支各自保存相同参数类型的独立内容与缓存", () => {
  let next = switchParameterType(initial(), 0, "B");
  next = switchActionType(next, 0, "camera_timelapse");
  expect(next.parameterVariants?.["0"]).toBeUndefined();
  next = setValue(next, ["actions", 0, "params"], { type: "A", interval: 5 });
  next = switchParameterType(next, 0, "B");
  next = switchActionType(next, 0, "camera_record");
  next = switchParameterType(next, 0, "A");
  expect(parseDraft(next).actions[0].params.duration_s).toBe(60);
  expect(next.pending).toEqual(initial().pending);
  next = switchActionType(next, 0, "camera_timelapse");
  expect(
    parseDraft(switchParameterType(next, 0, "A")).actions[0].params,
  ).toEqual({ type: "A", interval: 5 });
});

it("外层type自己的成功原文消解后共同恢复内层缓存", () => {
  let next = switchParameterType({ ...initial(), pending: {} }, 0, "B");
  next = switchActionType(next, 0, "report_status");
  next = setValue(next, ["actions", 0, "params"], { scope: "full" });
  next = editValue(next, ["actions", 0, "type"], '"camera_record', "json");
  next = editValue(next, ["actions", 0, "type"], '"camera_record"', "json");
  expect(next.pending).toEqual({});
  expect(
    parseDraft(switchParameterType(next, 0, "A")).actions[0].params.duration_s,
  ).toBe(60);
  expect(
    next.actionVariants?.["0"]?.find((v) => v.type === "report_status")
      ?.pending,
  ).toEqual({});
});

it("整段动作跨外层目标全文优先，目标旧params成为独立内容", () => {
  let next = switchParameterType({ ...initial(), pending: {} }, 0, "B");
  next = setValue(next, ["actions", 0, "params", "value"], 4);
  next = switchActionType(next, 0, "camera_timelapse");
  next = setValue(next, ["actions", 0, "params"], { type: "A", interval: 5 });
  next = editValue(next, ["actions", 0], "{", "json");
  next = editValue(
    next,
    ["actions", 0],
    '{"name":"explicit","type":"camera_record","params":{"type":"C","value":9}}',
    "json",
  );
  expect(parseDraft(next).actions[0]).toEqual({
    name: "explicit",
    type: "camera_record",
    params: { type: "C", value: 9 },
  });
  expect(
    parseDraft(switchParameterType(next, 0, "B")).actions[0].params.value,
  ).toBe(4);
  expect(
    parseDraft(switchParameterType(next, 0, "A")).actions[0].params.duration_s,
  ).toBe(60);
  const outer = switchActionType(next, 0, "camera_timelapse");
  expect(parseDraft(outer).actions[0].params.interval).toBe(5);
  expect(outer.pending).toEqual({});
});

it("整段动作省略params明确清空，旧参数缓存仍可恢复", () => {
  const next = editValue(
    { ...initial(), pending: {} },
    ["actions", 0],
    '{"type":"camera_record","name":"new"}',
    "json",
  );
  expect(Object.hasOwn(parseDraft(next).actions[0], "params")).toBe(false);
  expect(
    parseDraft(switchParameterType(next, 0, "A")).actions[0].params.duration_s,
  ).toBe(60);
});

it.each(["null", "5", "[]"])(
  "整段非对象动作%s暂存可解释旧分支及内缓存",
  (text) => {
    const b = switchParameterType({ ...initial(), pending: {} }, 0, "B");
    const next = editValue(b, ["actions", 0], text, "json");
    expect(parseDraft(next).actions[0]).toEqual(parseClientJson(text));
    expect(next.parameterVariants?.["0"]).toBeUndefined();
    const restored = editValue(
      next,
      ["actions", 0],
      '{"type":"camera_record","params":{"type":"C"}}',
      "json",
    );
    expect(
      parseDraft(switchParameterType(restored, 0, "A")).actions[0].params
        .duration_s,
    ).toBe(60);
  },
);

it("非对象动作带活动缓存时拒绝无法归属的修正且完整保留", () => {
  const input = {
    ...switchParameterType({ ...initial(), pending: {} }, 0, "B"),
    text: '{"actions":[null]}',
  };
  const before = stringifyJson(input);
  expect(() =>
    editValue(input, ["actions", 0], '{"type":"camera_record"}', "json"),
  ).toThrow();
  expect(stringifyJson(input)).toBe(before);
});

it("复制与删除前项共同搬移参数内容，追加没有继承", () => {
  const b = switchParameterType(initial(), 0, "B");
  const copy = copyDraftAction(b, 0);
  const edited = setValue(
    switchParameterType(copy, 1, "A"),
    ["actions", 1, "params", "duration_s"],
    10,
  );
  expect(
    parseDraft(switchParameterType(edited, 0, "A")).actions[0].params
      .duration_s,
  ).toBe(60);
  const removed = removeDraftActions(edited, new Set([0]));
  expect(parseDraft(removed).actions[0].params.duration_s).toBe(10);
  expect(removed.parameterVariants?.["1"]).toBeUndefined();
  const appended = appendContentAction(removed, {
    name: "new",
    type: "camera_record",
  });
  expect(appended.parameterVariants?.["1"]).toBeUndefined();
});

it("仅缓存差异参与完整内容比较", () => {
  const b = switchParameterType(initial(), 0, "B");
  expect(sameContent(b, { ...b, parameterVariants: {} })).toBe(false);
});

it("只存在内层资料时整份替换仍需确认，确认清除", () => {
  const b = switchParameterType(initial(), 0, "B");
  expect(() => editPlanText(b, '{"actions":[]}')).toThrow();
  expect(editPlanText(b, '{"actions":[]}', true)).toEqual({
    text: '{"actions":[]}',
    pending: {},
  });
});

it("整组动作确认替换清除当前与外层停用分支的内缓存", () => {
  const b = switchActionType(
    switchParameterType({ ...initial(), pending: {} }, 0, "B"),
    0,
    "report_status",
  );
  expect(() => setValue(b, ["actions"], [])).toThrow();
  const next = setValue(b, ["actions"], [], false, false, undefined, true);
  expect(next.parameterVariants).toBeUndefined();
  expect(next.actionVariants).toBeUndefined();
});

it("参数身份对象后代修正回A恢复完整分支而不生成第二权威", () => {
  const original = {
    text: '{"actions":[{"params":{"type":{"code":"A"},"value":7}}]}',
  };
  let next = switchParameterType(original, 0, { code: "B" });
  next = setValue(next, ["actions", 0, "params", "value"], 9);
  next = editValue(
    next,
    ["actions", 0, "params", "type", "code"],
    '"A"',
    "json",
  );
  expect(parseDraft(next).actions[0].params.value).toBe(7);
  expect(canSwitchParameterType(next, 0)).toBe(true);
  expect(
    parseDraft(switchParameterType(next, 0, { code: "B" })).actions[0].params
      .value,
  ).toBe(9);
});

it("外层身份后代恢复其专属内容与内层缓存并经第三分支仍完整", () => {
  const original = {
    text: '{"actions":[{"name":"A","type":{"code":"A"},"params":{"type":"P","value":7}}]}',
  };
  let next = switchParameterType(original, 0, "Q");
  next = setValue(next, ["actions", 0, "params", "value"], 8);
  next = switchActionType(next, 0, { code: "B" });
  next = editValue(next, ["actions", 0, "type", "code"], '"A"', "json");
  expect(parseDraft(next).actions[0].params).toEqual({ type: "Q", value: 8 });
  expect(
    parseDraft(switchParameterType(next, 0, "P")).actions[0].params.value,
  ).toBe(7);
  next = switchActionType(switchActionType(next, 0, { code: "C" }), 0, {
    code: "A",
  });
  expect(
    parseDraft(switchParameterType(next, 0, "P")).actions[0].params.value,
  ).toBe(7);
});

it.each(["parameter", "action"])(
  "%s身份数组现存位置修改和省略都使用完整身份",
  (scope) => {
    const outer = scope === "action";
    const path = outer
      ? ["actions", 0, "type", 1]
      : ["actions", 0, "params", "type", 1];
    const original = {
      text: stringifyJson({
        actions: [
          outer
            ? { type: ["A", null], params: { type: "P", value: 7 } }
            : { params: { type: ["A", null], value: 7 } },
        ],
      }),
    };
    const b = outer
      ? switchActionType(original, 0, ["A", "B"])
      : switchParameterType(original, 0, ["A", "B"]);
    const restored = setValue(b, path, undefined, true);
    expect(parseDraft(restored).actions[0].params.value).toBe(7);
    const changed = editValue(b, path, "null", "json");
    expect(parseDraft(changed).actions[0].params.value).toBe(7);
  },
);

it.each(["parameter", "action"])(
  "%s身份对象成员省略恢复既有完整身份",
  (scope) => {
    const outer = scope === "action";
    const original = {
      text: stringifyJson({
        actions: [
          outer
            ? { type: { code: "A" }, params: { value: 7 } }
            : { params: { type: { code: "A" }, value: 7 } },
        ],
      }),
    };
    const b = outer
      ? switchActionType(original, 0, { code: "A", extra: true })
      : switchParameterType(original, 0, { code: "A", extra: true });
    const path = outer
      ? ["actions", 0, "type", "extra"]
      : ["actions", 0, "params", "type", "extra"];
    expect(
      parseDraft(setValue(b, path, undefined, true)).actions[0].params.value,
    ).toBe(7);
  },
);

it.each(["parameter", "action"])(
  "%s身份后代自身未完成原文成功消费后恢复",
  (scope) => {
    const outer = scope === "action";
    const original = {
      text: stringifyJson({
        actions: [
          outer
            ? { type: { code: "A" }, params: { value: 7 } }
            : { params: { type: { code: "A" }, value: 7 } },
        ],
      }),
    };
    let b = outer
      ? switchActionType(original, 0, { code: "B" })
      : switchParameterType(original, 0, { code: "B" });
    const path = outer
      ? ["actions", 0, "type", "code"]
      : ["actions", 0, "params", "type", "code"];
    b = editValue(b, path, '"A', "json");
    const restored = editValue(b, path, '"A"', "json");
    expect(restored.pending).toEqual({});
    expect(parseDraft(restored).actions[0].params.value).toBe(7);
  },
);

it.each(["parameter", "action"])(
  "%s身份兄弟原文未完成阻止修改与省略且完整保留",
  (scope) => {
    const outer = scope === "action";
    const base = outer
      ? ["actions", 0, "type"]
      : ["actions", 0, "params", "type"];
    const original = {
      text: stringifyJson({
        actions: [
          outer ? { type: { code: "A" } } : { params: { type: { code: "A" } } },
        ],
      }),
      pending: {
        ["/" + [...base, "extra"].join("/")]: {
          kind: "json" as const,
          text: "{",
        },
      },
    };
    const before = stringifyJson(original);
    expect(() => setValue(original, [...base, "code"], "B")).toThrow();
    expect(() =>
      setValue(original, [...base, "code"], undefined, true),
    ).toThrow();
    expect(stringifyJson(original)).toBe(before);
  },
);

it.each(["parameter", "action"])(
  "%s身份后代数字依据原父容器事实恢复",
  (scope) => {
    const outer = scope === "action";
    for (const token of ["1e-999", "1.0000000000000001"]) {
      const original = {
        text: outer
          ? `{"actions":[{"type":{"code":${token}},"params":{"value":7}}]}`
          : `{"actions":[{"params":{"type":{"code":${token}},"value":7}}]}`,
      };
      const b = outer
        ? switchActionType(original, 0, { code: Number(token) })
        : switchParameterType(original, 0, { code: Number(token) });
      const path = outer
        ? ["actions", 0, "type", "code"]
        : ["actions", 0, "params", "type", "code"];
      const restored = editValue(b, path, token, "json");
      expect(parseDraft(restored).actions[0].params.value).toBe(7);
      expect(restored.text).toContain(token);
    }
  },
);

it.each(["parameter", "action"])(
  "%s身份数学等价后代输入只消费自身原文",
  (scope) => {
    const outer = scope === "action";
    const original = {
      text: outer
        ? '{"actions":[{"type":{"code":1e0},"params":{"value":7}}]}'
        : '{"actions":[{"params":{"type":{"code":1e0},"value":7}}]}',
    };
    const path = outer
      ? ["actions", 0, "type", "code"]
      : ["actions", 0, "params", "type", "code"];
    const b = editValue(original, path, "1e", "json");
    const restored = editValue(b, path, "1.0", "json");
    expect(restored.text).toBe(original.text);
    expect(restored.pending).toEqual({});
    expect(restored.parameterVariants).toBeUndefined();
    expect(restored.actionVariants).toBeUndefined();
  },
);

it.each(["switch", "copy", "remove"])(
  "%s搬移当前同型参数双权威时失败保留",
  (operation) => {
    const input = {
      text: '{"actions":[{"type":"camera_record","params":{"type":"A","value":7}}]}',
      parameterVariants: {
        "0": [{ paramsText: '{"params":{"type":"A","value":8}}', pending: {} }],
      },
    };
    const before = stringifyJson(input);
    expect(() =>
      operation === "switch"
        ? switchActionType(input, 0, "report_status")
        : operation === "copy"
          ? copyDraftAction(input, 0)
          : removeDraftActions(input, new Set([0])),
    ).toThrow();
    expect(stringifyJson(input)).toBe(before);
  },
);

it.each(["switch", "copy", "remove"])(
  "%s搬移停用外层中的同型参数双权威时失败保留",
  (operation) => {
    const input = {
      text: '{"actions":[{"type":"report_status"}]}',
      actionVariants: {
        "0": [
          {
            type: "camera_record",
            fields: { params: { type: "A", value: 7 } },
            pending: {},
            parameterVariants: [
              { paramsText: '{"params":{"type":"A","value":8}}', pending: {} },
            ],
          },
        ],
      },
    };
    const before = stringifyJson(input);
    expect(() =>
      operation === "switch"
        ? switchActionType(input, 0, "camera_record")
        : operation === "copy"
          ? copyDraftAction(input, 0)
          : removeDraftActions(input, new Set([0])),
    ).toThrow();
    expect(stringifyJson(input)).toBe(before);
  },
);

it("整体显式同型参数可以消费目标副本，但跨型不能丢弃损坏旧型权威", () => {
  const input = {
    text: '{"actions":[{"type":"camera_record","params":{"type":"A","value":7}}]}',
    parameterVariants: {
      "0": [{ paramsText: '{"params":{"type":"A","value":8}}', pending: {} }],
    },
  };
  const replaced = setValue(
    input,
    ["actions", 0, "params"],
    { type: "A", value: 9 },
    false,
    true,
  );
  expect(parseDraft(replaced).actions[0].params.value).toBe(9);
  expect(replaced.parameterVariants?.["0"]).toBeUndefined();
  const before = stringifyJson(input);
  expect(() =>
    setValue(input, ["actions", 0, "params"], { type: "B" }, false, true),
  ).toThrow();
  expect(stringifyJson(input)).toBe(before);
});

it("整体显式动作可消费目标外层参数同型副本且provided值优先", () => {
  const input = {
    text: '{"actions":[{"type":"report_status","params":{"scope":"full"}}]}',
    actionVariants: {
      "0": [
        {
          type: "camera_record",
          fields: { params: { type: "A", value: 7 } },
          pending: {},
          parameterVariants: [
            { paramsText: '{"params":{"type":"A","value":8}}', pending: {} },
          ],
        },
      ],
    },
  };
  const replaced = setValue(
    input,
    ["actions", 0],
    { type: "camera_record", params: { type: "A", value: 9 } },
    false,
    true,
  );
  expect(parseDraft(replaced).actions[0].params).toEqual({
    type: "A",
    value: 9,
  });
  expect(replaced.parameterVariants?.["0"]).toBeUndefined();
});
