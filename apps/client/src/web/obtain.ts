import type { ReportPlan } from "../shared/types";
import { api } from "./api";

/** 排除的是本地仍可读取的副本；默认范围继续由主机决定。 */
export async function receivedOutputIds(
  outputIds: string[],
): Promise<Set<string>> {
  if (!outputIds.length) return new Set();
  const result = await api<{ received_output_ids: string[] }>(
    "/media/availability",
    "POST",
    { output_ids: [...new Set(outputIds)] },
  );
  return new Set(result.received_output_ids);
}
export async function defaultObtainParams(
  source: { plan_instance_id: string } | { action_instance_id: string },
  plans: ReportPlan[],
) {
  const sourcePlans = new Map<string, string>();
  const previews = new Set<string>();
  for (const plan of plans)
    for (const action of plan.actions ?? []) {
      sourcePlans.set(action.action_instance_id, plan.plan_instance_id);
      for (const output of action.outputs ?? [])
        if (output.kind === "preview") previews.add(output.output_id);
    }
  const ids = plans.flatMap((plan) =>
    (plan.actions ?? []).flatMap((action) =>
      (action.deliveries ?? [])
        .filter((delivery) => {
          if (previews.has(delivery.output_id)) return false;
          if ("action_instance_id" in source)
            return (
              delivery.source_action_instance_id === source.action_instance_id
            );
          // 交付可以来自其他取回计划，归属按产物来源动作判断。
          const planId = sourcePlans.get(delivery.source_action_instance_id);
          // 增量报告尚未提供来源明细时，由主机判断该排除项是否适用。
          return planId === undefined || planId === source.plan_instance_id;
        })
        .map((delivery) => delivery.output_id),
    ),
  );
  const excluded = [...(await receivedOutputIds(ids))];
  return {
    source,
    ...(excluded.length ? { exclude_output_ids: excluded } : {}),
  };
}
