import { expect, it } from "vitest";
import { validatePlan } from "../../src/shared/plan";
import { validateReport } from "../../src/domain/reports";

const plan = (created_at: unknown) => ({
  request_id: "42",
  created_at,
  name: "时间检查",
  actions: [{ name: "同步", type: "report_status", params: { scope: "full" } }],
});

it.each([
  "2026-10-26 07:08:23.047",
  "2026-10-26 07:08:23.000",
  "2024-02-29 23:59:59.999",
  "0001-01-01 00:00:00.001",
  "9999-12-31 23:59:59.999",
  "2026-10-26 07:08:23",
])("计划与报告接受合法创建时间 %s", (created_at) => {
  expect(validatePlan(plan(created_at), null)).toEqual([]);
  expect(() =>
    validateReport({
      report_id: "1",
      from_wm: 0,
      to_wm: 1,
      plans: [
        {
          plan_instance_id: "1",
          request_id: "42",
          name: "时间检查",
          created_at,
          status: "pending",
        },
      ],
    }),
  ).not.toThrow();
});

it.each([
  "2026-10-26 07:08:23.1",
  "2026-10-26 07:08:23.12",
  "2026-10-26 07:08:23.1234",
  "2026-10-26 07:08:23.123456",
  "2026-02-29 07:08:23.047",
  "0000-01-01 00:00:00.000",
  "2026-10-26 07:08:23.047Z",
  "2026-10-26 07:08:23.047\n",
])("计划拒绝非法创建时间 %s", (created_at) => {
  expect(
    validatePlan(plan(created_at), null).some((i) =>
      i.path.includes("created_at"),
    ),
  ).toBe(true);
});

it("创建时间的毫秒格式不扩大动作执行时间的格式", () => {
  const value = plan("2026-10-26 07:08:23.047");
  Object.assign(value.actions[0], { scheduled_at: "2026-10-26 08:00:00.047" });
  expect(
    validatePlan(value, null).some((i) => i.path.includes("scheduled_at")),
  ).toBe(true);
});
