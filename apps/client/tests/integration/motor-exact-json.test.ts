import { afterEach, expect, it, vi } from "vitest";
import { createHash } from "node:crypto";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Application } from "../../src/server/application";
import type { ImportFile } from "../../src/server/models";
import { motorReport } from "../helpers/motor";
import { reportInput } from "./fixtures";
import { parseJson } from "../../src/shared/json";
import { Files } from "../../src/server/files";
import { createHttpApp } from "../../src/server/http";
import { RequestLifecycle } from "../../src/server/lifecycle";
import { api } from "../../src/web/api";

const cleanup: Array<() => void> = [];
function setup() {
  const directory = mkdtempSync(join(tmpdir(), "camctl-exact-"));
  let app = new Application(directory);
  app.store.initialize();
  cleanup.push(() => {
    app.store.close();
    rmSync(directory, { recursive: true, force: true });
  });
  return {
    get app() {
      return app;
    },
    restart() {
      app.store.close();
      app = new Application(directory);
      return app;
    },
  };
}
afterEach(() => cleanup.splice(0).forEach((f) => f()));
function originalInput(
  params: string | undefined,
  id = "1",
  from = 0,
  to = 2,
  policy?: string,
) {
  const report = motorReport();
  report.report_id = id;
  report.from_wm = from;
  report.to_wm = to;
  report.plans![0].status = "pending";
  const action = report.plans![0].actions![0];
  action.status = "failed";
  action.error = { code: "invalid_params", stage: "admission", details: {} };
  if (params === undefined) delete action.input_params;
  let text = JSON.stringify(report);
  if (params !== undefined)
    text = text.replace(
      '"input_params":{"position":0}',
      `"input_params":${params}`,
    );
  if (policy !== undefined)
    text = text.replace('"policy":{"max_delay_ms":1000}', `"policy":${policy}`);
  const bytes = Buffer.from(text);
  const input = reportInput(report);
  return {
    bytes,
    file: {
      ...input.file,
      fileName: `status-report-${id}-${createHash("sha256").update(bytes).digest("hex")}.json`,
      expectedSize: bytes.length,
      bytesReceived: bytes.length,
    },
  };
}
const changed: Array<[string | undefined, string]> = [
  ["1.0000000000000000001", "1"],
  ['{"position":{"n":1.0000000000000000001}}', '{"position":{"n":1}}'],
  ['{"position":1,"extra":1.0000000000000000001}', '{"position":1,"extra":1}'],
  ["[1.0000000000000000001]", "[1]"],
  ['{"position":[1,2]}', '{"position":[2,1]}'],
  [undefined, "null"],
  ["true", "1"],
  ["1e999", "1e998"],
];
const equivalent: Array<[string, string]> = [
  ["1.0", "1e0"],
  ['{"position":-0}', '{"position":0}'],
  [
    '{"z":[1.0,null],"position":{"x":1.0000000000000000001}}',
    '{"position":{"x":10000000000000000001e-19},"z":[1e0,null]}',
  ],
  ["1e999", "10e998"],
];
for (const boundary of [
  "advancing",
  "same",
  "earlier",
  "covered-first",
  "restart",
] as const) {
  it.each(changed)(
    `${boundary}：原输入 %s 变成 %s 时拒绝并保持覆盖`,
    (first, next) => {
      const env = setup();
      if (boundary === "covered-first")
        env.app.applyReports([
          reportInput({ report_id: "9", from_wm: 0, to_wm: 2 }),
        ]);
      const initial = originalInput(
        first,
        "1",
        0,
        boundary === "covered-first" ? 1 : 2,
      );
      env.app.applyReports([initial]);
      expect(
        env.app.store.get<ImportFile>("imports", initial.file.id)?.status,
      ).toBe(boundary === "covered-first" ? "covered" : "accepted");
      if (boundary === "restart") env.restart();
      const incoming = originalInput(
        next,
        "2",
        boundary === "same" || boundary === "earlier" ? 0 : 2,
        boundary === "same" ? 2 : boundary === "earlier" ? 1 : 3,
      );
      env.app.applyReports([incoming]);
      expect(
        env.app.store.get<ImportFile>("imports", incoming.file.id)?.status,
      ).toBe("failed");
      expect(env.app.coverage()).toBe(2);
      expect(env.app.store.report("2")).toBeUndefined();
    },
  );
  it.each(equivalent)(
    `${boundary}：数学等价的 %s 与 %s 保留首次原文`,
    (first, next) => {
      const env = setup();
      if (boundary === "covered-first")
        env.app.applyReports([
          reportInput({ report_id: "9", from_wm: 0, to_wm: 2 }),
        ]);
      env.app.applyReports([
        originalInput(first, "1", 0, boundary === "covered-first" ? 1 : 2),
      ]);
      if (boundary === "restart") env.restart();
      const incoming = originalInput(
        next,
        "2",
        boundary === "same" || boundary === "earlier" ? 0 : 2,
        boundary === "same" ? 2 : boundary === "earlier" ? 1 : 3,
      );
      env.app.applyReports([incoming]);
      expect(
        env.app.store.get<ImportFile>("imports", incoming.file.id)?.status,
      ).toBe(
        boundary === "same" || boundary === "earlier" ? "covered" : "accepted",
      );
      expect(
        env.app.store.get<{ text: string }>("motor_input_texts", "1")?.text,
      ).toBe(first);
    },
  );
}
it.each(["1e999", "-1e999", "9".repeat(400), '[1e999,{"position":-1e999}]'])(
  "超 Number 范围原数 %s 经SQLite和重启仍保留数字字面量",
  (token) => {
    const env = setup();
    const input = originalInput(token);
    env.app.applyReports([input]);
    expect(env.app.coverage()).toBe(2);
    const text = JSON.stringify(env.app.snapshot());
    expect(text).toContain(`"input_params":${token}`);
    expect(JSON.stringify(env.app.store.get("state", "snapshot"))).toContain(
      `"input_params":${token}`,
    );
    env.restart();
    expect(JSON.stringify(env.app.snapshot())).toContain(
      `"input_params":${token}`,
    );
    expect(
      JSON.stringify(parseJson(JSON.stringify(env.app.state()))),
    ).toContain(`"input_params":${token}`);
  },
);
it.each(["missing", "old-identity", "wrong-identity", "missing-text"])(
  "从原报告恢复%s派生信息，不把它当成缺省输入",
  (mode) => {
    const env = setup();
    const first = originalInput("1.0000000000000000001");
    env.app.applyReports([first]);
    if (mode === "missing") env.app.store.remove("motor_input_texts", "1");
    else if (mode === "old-identity")
      env.app.store.set("motor_input_texts", "1", {
        text: "1.0000000000000000001",
      });
    else if (mode === "wrong-identity")
      env.app.store.set("motor_input_texts", "1", {
        text: "1",
        inputIdentity: "incorrect-derived-identity",
      });
    else {
      const saved = env.app.store.get<{ inputIdentity: string }>(
        "motor_input_texts",
        "1",
      )!;
      env.app.store.set("motor_input_texts", "1", {
        inputIdentity: saved.inputIdentity,
      });
    }
    const lossy = env.app.snapshot();
    lossy.plans![0].actions![0].input_params = 1;
    env.app.store.set("state", "snapshot", lossy);
    env.restart();
    expect(JSON.stringify(env.app.snapshot())).toContain(
      '"input_params":1.0000000000000000001',
    );
    expect(env.app.state().motorInputTexts["1"]).toBe("1.0000000000000000001");
    expect(JSON.stringify(env.app.snapshot())).toContain(
      '"input_params":1.0000000000000000001',
    );
    const next = originalInput("1", "2", 2, 3);
    env.app.applyReports([next]);
    expect(env.app.store.get<ImportFile>("imports", next.file.id)?.status).toBe(
      "failed",
    );
  },
);
it("HTTP 与网页 API 都以原 JSON 数字传递超范围原输入", async () => {
  const env = setup();
  env.app.applyReports([originalInput('{"position":1e999,"nested":[-1e999]}')]);
  const files = new Files(env.app);
  const server = createHttpApp(env.app, files, new RequestLifecycle()).listen(
    0,
    "127.0.0.1",
  );
  await new Promise<void>((r) => server.once("listening", r));
  const base = `http://127.0.0.1:${(server.address() as { port: number }).port}`;
  const fetchReal = globalThis.fetch;
  const fetchSpy = vi
    .spyOn(globalThis, "fetch")
    .mockImplementation((input, init) =>
      fetchReal(
        typeof input === "string" && input.startsWith("/")
          ? base + input
          : input,
        init,
      ),
    );
  try {
    const response = await fetchReal(base + "/api/state");
    expect(await response.text()).toContain(
      '"input_params":{"position":1e999,"nested":[-1e999]}',
    );
    const state = await api("/state");
    expect(JSON.stringify(state)).toContain(
      '"input_params":{"position":1e999,"nested":[-1e999]}',
    );
  } finally {
    fetchSpy.mockRestore();
    await files.idle();
    await new Promise<void>((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
  }
});
it("含大数的失败动作和另一正常动作共同应用", () => {
  const env = setup();
  const input = originalInput('{"position":1e999}');
  const report = motorReport();
  report.plans![0].actions![0].action_instance_id = "2";
  report.plans![0].actions![0].name = "第二位置";
  let text = input.bytes.toString();
  text = text
    .replace(
      '"actions":[',
      `"actions":[${JSON.stringify(report.plans![0].actions![0])},`,
    )
    .replace('"status":"pending","actions"', '"status":"completed","actions"');
  const bytes = Buffer.from(text);
  env.app.applyReports([
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
  expect(env.app.coverage()).toBe(2);
  expect(env.app.snapshot().plans![0].actions!.map((a) => a.status)).toEqual([
    "succeeded",
    "failed",
  ]);
});
it.each(["NaN", "Infinity", '{"position":1,"position":2}', "{"])(
  "错误 JSON %s 仍拒绝",
  (token) => {
    const env = setup();
    const input = originalInput(token);
    env.app.applyReports([input]);
    expect(
      env.app.store.get<ImportFile>("imports", input.file.id)?.status,
    ).toBe("failed");
    expect(env.app.coverage()).toBe(0);
  },
);
it.each(["report", "motor_input_texts", "snapshot", "coverage", "imports"])(
  "大数原输入在 %s 提交失败时所有接收事实回滚",
  (step) => {
    const env = setup();
    const input = originalInput("1e999");
    const set = env.app.store.set.bind(env.app.store);
    const spy = vi
      .spyOn(env.app.store, "set")
      .mockImplementation((namespace, id, value) => {
        if (namespace === step || (namespace === "state" && id === step))
          throw new Error("受控写失败");
        set(namespace, id, value);
      });
    const reportSpy =
      step === "report"
        ? vi.spyOn(env.app.store, "saveReport").mockImplementation(() => {
            throw new Error("受控报告写失败");
          })
        : undefined;
    try {
      expect(() => env.app.applyReports([input])).toThrow();
    } finally {
      spy.mockRestore();
      reportSpy?.mockRestore();
    }
    expect(env.app.coverage()).toBe(0);
    expect(env.app.store.reports()).toEqual([]);
    expect(env.app.store.all("motor_input_texts")).toEqual([]);
    expect(env.app.snapshot().plans ?? []).toEqual([]);
    expect(env.app.store.get("imports", input.file.id)).toBeUndefined();
  },
);
it("超范围数字的重复身份、字节冲突及缺口分别处理", () => {
  const env = setup();
  const first = originalInput("1e999");
  env.app.applyReports([first]);
  const same = { ...first, file: { ...first.file, id: "duplicate" } };
  env.app.applyReports([same]);
  expect(env.app.store.get<ImportFile>("imports", "duplicate")?.status).toBe(
    "duplicate",
  );
  const conflict = originalInput("10e998");
  env.app.applyReports([conflict]);
  expect(
    env.app.store.get<ImportFile>("imports", conflict.file.id)?.status,
  ).toBe("conflict");
  const gap = originalInput("1e999", "2", 4, 5);
  env.app.applyReports([gap]);
  expect(env.app.store.get<ImportFile>("imports", gap.file.id)?.status).toBe(
    "gap",
  );
  expect(env.app.store.report("2")).toBeUndefined();
  expect(env.app.coverage()).toBe(2);
  expect(env.app.state().motorInputTexts["1"]).toBe("1e999");
});
it.each([
  "1.0000000000000000001",
  "1e-999",
  "9007199254740991.0000001",
  "1e999",
])("受理失败策略原数 %s 保留，正常报告拒绝", (token) => {
  const env = setup();
  const input = originalInput(
    '{"position":0}',
    "1",
    0,
    2,
    `{"max_delay_ms":${token}}`,
  );
  env.app.applyReports([input]);
  expect(env.app.coverage()).toBe(2);
  expect(JSON.stringify(env.app.snapshot())).toContain(
    `"policy":{"max_delay_ms":${token}}`,
  );
  const normal = motorReport();
  const raw = reportInput(normal);
  const bytes = Buffer.from(
    raw.bytes
      .toString()
      .replace('"max_delay_ms":1000', `"max_delay_ms":${token}`),
  );
  const file = {
    ...raw.file,
    id: "normal",
    fileName: `status-report-1-${createHash("sha256").update(bytes).digest("hex")}.json`,
    expectedSize: bytes.length,
    bytesReceived: bytes.length,
  };
  const other = setup();
  other.app.applyReports([{ bytes, file }]);
  expect(other.app.store.get<ImportFile>("imports", "normal")?.status).toBe(
    "failed",
  );
  expect(other.app.coverage()).toBe(0);
});
it.each(["1e999", "-1e999", "9".repeat(400)])(
  "正常电机位置 %s 仍受表示范围约束",
  (token) => {
    const env = setup();
    const input = reportInput(motorReport());
    const bytes = Buffer.from(
      input.bytes.toString().replace('"position":0', `"position":${token}`),
    );
    env.app.applyReports([
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
    expect(env.app.coverage()).toBe(0);
  },
);
