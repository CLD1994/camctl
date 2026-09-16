import { afterEach, expect, it } from "vitest";
import { mkdtempSync, rmSync, existsSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Application } from "../../src/server/application";
import { mappedReport, reportInput } from "./fixtures";
const apps: Application[] = [];
afterEach(() => {
  for (const app of apps.splice(0)) {
    app.store.close();
    rmSync(app.store.directory, { recursive: true, force: true });
  }
});
function setup(initialized = true) {
  const app = new Application(mkdtempSync(join(tmpdir(), "camctl-state-")));
  apps.push(app);
  if (initialized) app.store.initialize();
  return app;
}
it("初始化明确保存零水位与空投影", () => {
  const app = setup();
  expect(app.store.get("state", "coverage")).toBe(0);
  expect(app.store.get("state", "snapshot")).toEqual({
    report_id: 1,
    from_wm: 0,
    to_wm: 0,
  });
});
it.each(["coverage", "snapshot"])(
  "已有历史缺少必要记录 %s 时进入故障",
  (key) => {
    const app = setup();
    app.applyReports([reportInput(mappedReport(Buffer.from("video")))]);
    app.store.close();
    const db = new DatabaseSync(app.store.filename);
    db.prepare("DELETE FROM records WHERE namespace=? AND id=?").run(
      "state",
      key,
    );
    db.close();
    expect(app.store.status().state).toBe("fault");
    expect(() => app.syncParams(true)).toThrow();
  },
);
it.each([
  ["coverage", '"20"'],
  ["coverage", "0"],
  ["snapshot", "null"],
] as const)("已有历史的 %s 类型或关系错误时不能使用", (key, value) => {
  const app = setup();
  app.applyReports([reportInput(mappedReport(Buffer.from("video")))]);
  app.store.close();
  const db = new DatabaseSync(app.store.filename);
  db.prepare("UPDATE records SET value=? WHERE namespace=? AND id=?").run(
    value,
    "state",
    key,
  );
  db.close();
  expect(app.store.status().state).toBe("fault");
  expect(() => app.snapshot()).toThrow();
});
it("合法终点零报告有可靠接收依据", () => {
  const app = setup();
  app.applyReports([reportInput({ report_id: 1, from_wm: 0, to_wm: 0 })]);
  expect(app.coverage()).toBe(0);
  expect(app.ackId()).toBe(1);
  expect(app.store.status().state).toBe("ready");
});
it.each([false, true])("未初始化不因同步 full=%s 返回范围或建库", (full) => {
  const app = setup(false);
  expect(() => app.syncParams(full)).toThrow();
  expect(existsSync(app.store.filename)).toBe(false);
});
