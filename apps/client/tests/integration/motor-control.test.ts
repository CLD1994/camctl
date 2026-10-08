import { afterEach, expect, it, vi } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Application } from "../../src/server/application";
import { parseReport } from "../../src/domain/reports";
import type { ImportFile } from "../../src/server/models";
import {
  motorDraft,
  motorPositions,
  motorReport,
  roundedMotorFractions,
} from "../helpers/motor";
import { reportInput } from "./fixtures";
import { createHash } from "node:crypto";
const clean: Array<() => void> = [];
function setup() {
  const directory = mkdtempSync(join(tmpdir(), "camctl-motor-"));
  const app = new Application(directory, { next: () => 17n });
  app.store.initialize();
  clean.push(() => {
    app.store.close();
    rmSync(directory, { recursive: true, force: true });
  });
  return { app, directory };
}
afterEach(() => clean.splice(0).forEach((fn) => fn()));
it.each(["1.0000000000000000001", "1e-999", "9007199254740991.0000001"])(
  "电机延迟 %s 不能在真实导出入口舍入",
  (token) => {
    const { app } = setup();
    const content = {
      text: motorDraft().text.replace(
        '"max_delay_ms":1000',
        `"max_delay_ms":${token}`,
      ),
    };
    const draft = app.createDraft(content);
    expect(() => app.exportDraft(draft.id, 1, content)).toThrow();
    expect(app.draft(draft.id).content).toEqual(content);
  },
);
it.each(["-1", "9007199254740992", "true", '"1"'])(
  "非法电机延迟 %s 拒绝导出并保存草稿文本",
  (token) => {
    const { app } = setup();
    const content = {
      text: motorDraft().text.replace(
        '"max_delay_ms":1000',
        `"max_delay_ms":${token}`,
      ),
    };
    const draft = app.createDraft(content);
    expect(() => app.exportDraft(draft.id, 1, content)).toThrow();
    expect(app.draft(draft.id).content.text).toBe(content.text);
    expect(app.store.all("requests")).toEqual([]);
  },
);
it.each([
  ["0", 0],
  ["1.0", 1],
  ["1e2", 100],
  ["9007199254740991", 9007199254740991],
] as const)("合法电机延迟 %s 能导出和导入正常报告", (token, expected) => {
  const { app } = setup();
  const content = {
    text: motorDraft().text.replace(
      '"max_delay_ms":1000',
      `"max_delay_ms":${token}`,
    ),
  };
  const request = app.exportDraft(app.createDraft(content).id, 1, content);
  expect(
    (request.body.actions as Array<{ policy: unknown }>)[0].policy,
  ).toEqual({ max_delay_ms: expected });
  const input = reportInput(motorReport(request.id));
  const bytes = Buffer.from(
    input.bytes
      .toString()
      .replace('"max_delay_ms":1000', `"max_delay_ms":${token}`),
  );
  app.applyReports([
    {
      bytes,
      file: {
        ...input.file,
        fileName: `status-report-1-${createHash("sha256").update(bytes).digest("hex")}.json`,
        expectedSize: bytes.length,
        bytesReceived: bytes.length,
      },
    },
  ]);
  expect(app.coverage()).toBe(1);
});
function admissionInput(token: string, reportId = "1", from = 0, to = 1) {
  const report = motorReport();
  report.report_id = reportId;
  report.from_wm = from;
  report.to_wm = to;
  report.plans![0].status = "pending";
  Object.assign(report.plans![0].actions![0], {
    status: "failed",
    error: {
      code: "invalid_params",
      stage: "admission",
      details: { parameter_error: "position_not_integer" },
    },
  });
  const input = reportInput(report);
  const bytes = Buffer.from(
    input.bytes.toString().replace('"position":0', `"position":${token}`),
  );
  const fileName = `status-report-${reportId}-${createHash("sha256").update(bytes).digest("hex")}.json`;
  return {
    file: {
      ...input.file,
      fileName,
      expectedSize: bytes.length,
      bytesReceived: bytes.length,
    },
    bytes,
  };
}
it("受理失败报告保留精确位置原文，重启后仍可展示", () => {
  const { app, directory } = setup();
  const input = admissionInput("1.0000000000000000001");
  app.applyReports([input]);
  expect(app.store.get<ImportFile>("imports", input.file.id)?.status).toBe(
    "accepted",
  );
  expect(app.state().motorInputTexts["1"]).toBe(
    '{"position":1.0000000000000000001}',
  );
  app.store.close();
  const reopened = new Application(directory);
  try {
    expect(reopened.state().motorInputTexts["1"]).toBe(
      '{"position":1.0000000000000000001}',
    );
  } finally {
    reopened.store.close();
  }
});
it("后续报告不能用舍入后相同的整数改写原始数学值", () => {
  const { app } = setup();
  const first = admissionInput("1.0000000000000000001");
  const changed = admissionInput("1", "2", 1, 2);
  app.applyReports([first, changed]);
  expect(app.store.get<ImportFile>("imports", changed.file.id)?.status).toBe(
    "failed",
  );
  expect(app.coverage()).toBe(1);
});
it("位置数学值相同的不同数值字面量保留首次输入原文", () => {
  const { app } = setup();
  app.applyReports([
    admissionInput("1.0000000000000000001"),
    admissionInput("10000000000000000001e-19", "2", 1, 2),
  ]);
  expect(app.coverage()).toBe(2);
  expect(app.state().motorInputTexts["1"]).toBe(
    '{"position":1.0000000000000000001}',
  );
});
it.each([undefined, null, true, "1", {}, { position: false, extra: 3 }])(
  "受理失败报告保留非数字原输入 %j",
  (params) => {
    const { app } = setup();
    const report = motorReport();
    report.plans![0].status = "pending";
    const action = report.plans![0].actions![0];
    Object.assign(action, {
      status: "failed",
      error: { code: "invalid_params", stage: "admission", details: {} },
    });
    if (params === undefined) delete action.input_params;
    else action.input_params = params;
    const input = reportInput(report);
    app.applyReports([input]);
    expect(app.store.get<ImportFile>("imports", input.file.id)?.status).toBe(
      "accepted",
    );
    if (params !== undefined)
      expect(app.state().motorInputTexts["1"]).toBe(JSON.stringify(params));
  },
);
it("原文派生记录写入失败时报告与覆盖也回滚", () => {
  const { app } = setup();
  const input = admissionInput("1.0000000000000000001");
  const set = app.store.set.bind(app.store);
  const spy = vi
    .spyOn(app.store, "set")
    .mockImplementation((namespace, id, value) => {
      if (namespace === "motor_input_texts") throw new Error("受控写入失败");
      set(namespace, id, value);
    });
  try {
    expect(() => app.applyReports([input])).toThrow(/受控写入/);
  } finally {
    spy.mockRestore();
  }
  expect(app.coverage()).toBe(0);
  expect(app.store.reports()).toEqual([]);
  expect(app.store.all("motor_input_texts")).toEqual([]);
});
it("重复和旧报告不覆盖位置原文展示", () => {
  const { app } = setup();
  const first = admissionInput("1.0000000000000000001");
  app.applyReports([
    first,
    admissionInput("10000000000000000001e-19", "2", 1, 2),
    admissionInput("1.00000000000000000010", "3", 0, 1),
    first,
  ]);
  expect(app.coverage()).toBe(2);
  expect(app.state().motorInputTexts["1"]).toBe(
    '{"position":1.0000000000000000001}',
  );
});
it("先接受旧报告中的原输入，再收到新报告时仍核对精确数学值", () => {
  const { app } = setup();
  app.applyReports([reportInput({ report_id: "9", from_wm: 0, to_wm: 2 })]);
  const older = admissionInput("1.0000000000000000001");
  app.applyReports([older]);
  expect(app.store.get<ImportFile>("imports", older.file.id)?.status).toBe(
    "covered",
  );
  const changed = admissionInput("1", "2", 2, 3);
  app.applyReports([changed]);
  expect(app.store.get<ImportFile>("imports", changed.file.id)?.status).toBe(
    "failed",
  );
  expect(app.coverage()).toBe(2);
});
it("旧报告首次取得的非数字位置在新报告中也保持原类型", () => {
  const { app } = setup();
  app.applyReports([reportInput({ report_id: "9", from_wm: 0, to_wm: 2 })]);
  const older = admissionInput("false");
  app.applyReports([older]);
  const changed = admissionInput('"false"', "2", 2, 3);
  app.applyReports([changed]);
  expect(app.store.get<ImportFile>("imports", changed.file.id)?.status).toBe(
    "failed",
  );
});
it.each(motorPositions)(
  "位置 %s 草稿保存、导出及重送保留同一正文",
  (position) => {
    const { app, directory } = setup();
    const content = motorDraft(position);
    const draft = app.createDraft(content);
    expect(app.draft(draft.id).content).toEqual(content);
    const request = app.exportDraft(draft.id, 1, content);
    expect(request.body.actions).toEqual([
      {
        name: "位置控制",
        type: "motor_control",
        scheduled_at: "2026-10-08 12:00:00",
        params: { position },
        policy: { max_delay_ms: 1000 },
      },
    ]);
    expect(app.downloadRequest(request.id)).toEqual(request.body);
    expect(app.exportDraft(draft.id, 1, { text: "bad" }).body).toEqual(
      request.body,
    );
    app.store.close();
    const reopened = new Application(directory);
    try {
      expect(reopened.downloadRequest(request.id)).toEqual(request.body);
    } finally {
      reopened.store.close();
    }
  },
);
it.each(roundedMotorFractions)(
  "原始位置 %s 不可经导出舍入为整数",
  (position) => {
    const { app } = setup();
    const content = {
      text: motorDraft().text.replace('"position":0', `"position":${position}`),
    };
    const draft = app.createDraft(content);
    expect(() => app.exportDraft(draft.id, 1, content)).toThrow();
    expect(app.draft(draft.id).content.text).toBe(content.text);
    expect(app.store.all("requests")).toEqual([]);
  },
);
it("电机报告真实导入关联原请求，重复导入保持事实", () => {
  const { app } = setup();
  const content = motorDraft(-2147483648);
  const request = app.exportDraft(app.createDraft(content).id, 1, content);
  const input = reportInput(motorReport(request.id, -2147483648));
  expect(
    parseReport(input.file.fileName, input.bytes).plans![0].actions![0]
      .input_params,
  ).toEqual({ position: -2147483648 });
  app.applyReports([input]);
  expect(app.store.get<ImportFile>("imports", input.file.id)?.status).toBe(
    "accepted",
  );
  expect(app.snapshot().plans![0].actions![0]).toMatchObject({
    type: "motor_control",
    status: "succeeded",
    input_params: { position: -2147483648 },
  });
  app.applyReports([reportInput(motorReport(request.id, -2147483648))]);
  expect(app.snapshot().plans).toHaveLength(1);
  expect(app.downloadRequest(request.id).actions).toEqual(request.body.actions);
});
it.each([
  "effective_params",
  "outputs",
  "deliveries",
  "device_execution",
  "result",
])("电机报告不能补造 %s", (field) => {
  const report = motorReport();
  Object.assign(report.plans![0].actions![0], {
    [field]: field === "outputs" || field === "deliveries" ? [] : {},
  });
  const input = reportInput(report);
  expect(() => parseReport(input.file.fileName, input.bytes)).toThrow();
});
it("已执行电机报告不能用舍入隐藏非整数位置", () => {
  const input = reportInput(motorReport());
  const bytes = Buffer.from(
    input.bytes
      .toString()
      .replace('"position":0', '"position":1.0000000000000000001'),
  );
  const name = `status-report-1-${createHash("sha256").update(bytes).digest("hex")}.json`;
  expect(() => parseReport(name, bytes)).toThrow();
});
