import { beforeEach, expect, it, vi } from "vitest";
import { defaultObtainParams, receivedOutputIds } from "../../src/web/obtain";
import type { ReportPlan } from "../../src/shared/types";
const { api } = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("../../src/web/api", () => ({ api }));
beforeEach(() => {
  api.mockReset();
});
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
it("默认动作取回只排除本地核对后确实可读的副本", async () => {
  api.mockResolvedValueOnce({ received_output_ids: [] });
  expect(
    await defaultObtainParams({ action_instance_id: "12" }, plans),
  ).toEqual({ source: { action_instance_id: "12" } });
  expect(api).toHaveBeenLastCalledWith("/media/availability", "POST", {
    output_ids: ["101"],
  });
});

// 来源计划只有拍摄明细，本地副本可以由另一份计划中的取回交付提供。
const scopedPlans = [
  {
    plan_instance_id: "1",
    actions: [
      {
        action_instance_id: "12",
        outputs: [
          { output_id: "100", kind: "original" },
          { output_id: "101", kind: "preview" },
          { output_id: "102", kind: "repaired" },
        ],
      },
    ],
  },
  {
    plan_instance_id: "2",
    actions: [
      {
        action_instance_id: "21",
        outputs: [{ output_id: "200", kind: "original" }],
      },
      {
        action_instance_id: "22",
        deliveries: [
          { output_id: "100", source_action_instance_id: "12" },
          { output_id: "101", source_action_instance_id: "12" },
          { output_id: "102", source_action_instance_id: "12" },
          { output_id: "200", source_action_instance_id: "21" },
        ],
      },
    ],
  },
] as ReportPlan[];
function allCopiesReadable() {
  api.mockImplementation(async (_path, _method, body) => ({
    received_output_ids: body.output_ids,
  }));
}
it("计划默认取回按产物来源排除其他计划，保留跨计划交付的原片与修复成品", async () => {
  allCopiesReadable();
  expect(
    await defaultObtainParams({ plan_instance_id: "1" }, scopedPlans),
  ).toEqual({
    source: { plan_instance_id: "1" },
    exclude_output_ids: ["100", "102"],
  });
});
it("动作默认取回不把同一来源的已知预览加入排除项", async () => {
  allCopiesReadable();
  expect(
    await defaultObtainParams({ action_instance_id: "12" }, scopedPlans),
  ).toEqual({
    source: { action_instance_id: "12" },
    exclude_output_ids: ["100", "102"],
  });
});
it.each([{ plan_instance_id: "1" }, { action_instance_id: "12" }])(
  "本地只有当前来源的预览和其他计划原片时，默认取回省略排除列表 %j",
  async (source) => {
    const previewsOnly = structuredClone(scopedPlans);
    previewsOnly[1].actions![1].deliveries =
      previewsOnly[1].actions![1].deliveries!.filter((delivery) =>
        ["101", "200"].includes(delivery.output_id),
      );
    allCopiesReadable();
    expect(await defaultObtainParams(source, previewsOnly)).toEqual({ source });
  },
);
it("当前计划只负责交付其他计划产物时，不把这些产物当作自己的来源", async () => {
  const onlyObtain = [
    scopedPlans[0],
    {
      plan_instance_id: "3",
      actions: [
        {
          action_instance_id: "31",
          deliveries: scopedPlans[1].actions![1].deliveries!.filter(
            (delivery) => delivery.source_action_instance_id === "12",
          ),
        },
      ],
    },
  ] as ReportPlan[];
  allCopiesReadable();
  expect(
    await defaultObtainParams({ plan_instance_id: "3" }, onlyObtain),
  ).toEqual({ source: { plan_instance_id: "3" } });
});
it("来源动作明细尚未到达时，保留未知产物的排除项和原计划引用", async () => {
  const partial = structuredClone(scopedPlans);
  partial[0].actions = undefined;
  allCopiesReadable();
  expect(await defaultObtainParams({ plan_instance_id: "1" }, partial)).toEqual(
    {
      source: { plan_instance_id: "1" },
      exclude_output_ids: ["100", "101", "102"],
    },
  );
});
it("重复交付按产物去重，且仅排除可读取性核对通过的副本", async () => {
  const repeated = structuredClone(scopedPlans);
  repeated[1].actions![1].deliveries!.push(
    ...repeated[1].actions![1].deliveries!,
  );
  api.mockResolvedValueOnce({ received_output_ids: ["102"] });
  expect(
    await defaultObtainParams({ plan_instance_id: "1" }, repeated),
  ).toEqual({
    source: { plan_instance_id: "1" },
    exclude_output_ids: ["102"],
  });
  expect(api).toHaveBeenCalledWith("/media/availability", "POST", {
    output_ids: ["100", "102"],
  });
});
it("本地副本核对失败时保留错误，不生成未经核对的排除参数", async () => {
  const error = new Error("无法核对本地副本");
  api.mockRejectedValueOnce(error);
  await expect(
    defaultObtainParams({ plan_instance_id: "1" }, scopedPlans),
  ).rejects.toBe(error);
});
it("精确取回仍可按所选预览产物核对本地副本", async () => {
  allCopiesReadable();
  expect(await receivedOutputIds(["101", "101"])).toEqual(new Set(["101"]));
  expect(api).toHaveBeenCalledWith("/media/availability", "POST", {
    output_ids: ["101"],
  });
});
