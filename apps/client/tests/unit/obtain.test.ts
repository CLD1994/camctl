import { expect, it, vi } from "vitest";
import { defaultObtainParams } from "../../src/web/obtain";
import type { ReportPlan } from "../../src/shared/types";
const { api } = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("../../src/web/api", () => ({ api }));
const plans = [
  { plan_instance_id: "1", actions: [{ action_instance_id: "12" }] },
  {
    plan_instance_id: "2",
    actions: [
      {
        action_instance_id: "20",
        deliveries: [
          { output_id: "100", source_action_instance_id: "11" },
          { output_id: "101", source_action_instance_id: "12" },
        ],
      },
    ],
  },
] as ReportPlan[];
it("默认计划取回不依赖增量报告是否已提供来源动作明细", async () => {
  api.mockResolvedValueOnce({ received_output_ids: ["100", "101"] });
  expect(await defaultObtainParams({ plan_instance_id: "1" }, plans)).toEqual({
    source: { plan_instance_id: "1" },
    exclude_output_ids: ["100", "101"],
  });
  expect(api).toHaveBeenLastCalledWith("/media/availability", "POST", {
    output_ids: ["100", "101"],
  });
});
it("默认动作取回只排除主机核对后确实可读的副本", async () => {
  api.mockResolvedValueOnce({ received_output_ids: [] });
  expect(
    await defaultObtainParams({ action_instance_id: "12" }, plans),
  ).toEqual({ source: { action_instance_id: "12" } });
  expect(api).toHaveBeenLastCalledWith("/media/availability", "POST", {
    output_ids: ["101"],
  });
});
