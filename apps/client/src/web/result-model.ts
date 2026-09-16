import type {
  ReportAction,
  ReportPlan,
  Output,
  Delivery,
} from "../shared/types";
import type { Video } from "../server/models";
import { mediaGroup } from "../shared/media";
import { isObject } from "../shared/validation";

/** 仅汇总报告明确提供的业务结果，不用动作终态补造逐文件结果。 */
export function resultNotes(
  action: ReportAction,
): Array<{ text: string; error: boolean }> {
  const notes: Array<{ text: string; error: boolean }> = [];
  const result = action.result;
  if (!result) return notes;
  if (Array.isArray(result.failures) && result.failures.length)
    notes.push({
      text: `${result.failures.length} 项取回未成功，已取得的文件仍可查看`,
      error: true,
    });
  if (Array.isArray(result.items)) {
    const items = result.items.filter(isObject);
    const failed = items.filter((i) => i.status === "failed").length;
    const done = items.filter((i) => i.status === "succeeded").length;
    notes.push({
      text: `已报告 ${items.length} 项处理结果，其中 ${done} 项成功${failed ? "、" + failed + " 项失败" : ""}`,
      error: failed > 0,
    });
  }
  if (
    isObject(result.repair) &&
    ["failed", "unconfirmed"].includes(String(result.repair.status))
  )
    notes.push({
      text: "录像修复未成功，原始文件与录像执行结果分别保留",
      error: true,
    });
  if (
    isObject(result.check) &&
    ["failed", "unconfirmed"].includes(String(result.check.check_status))
  )
    notes.push({ text: "媒体检查未通过或无法确认，详见执行记录", error: true });
  if (isObject(result.capture) && result.capture.status === "unconfirmed")
    notes.push({ text: "采集或停止结果无法确认", error: true });
  if (action.type === "report_status" && action.status === "succeeded")
    notes.push({ text: "主机已生成状态报告", error: false });
  return notes;
}

export interface Product {
  id: string;
  name: string;
  output?: Output;
  mediaType?: string;
  group: "image" | "video" | "other";
  state: "ready" | "attention" | "waiting";
  problems: number;
  sourcePlan?: ReportPlan;
  deliveries: Array<{ delivery: Delivery; owner?: ReportPlan; video?: Video }>;
}
export function resultProducts(
  action: ReportAction,
  plans: ReportPlan[],
  videos: Video[],
): Product[] {
  const outputs = new Map(
    plans
      .flatMap((p) => p.actions ?? [])
      .flatMap((a) => a.outputs ?? [])
      .map((o) => [o.output_id, o]),
  );
  for (const o of action.outputs ?? []) outputs.set(o.output_id, o);
  const rows = new Map<string, Product>();
  const add = (id: string, name?: string) => {
    let row = rows.get(id);
    if (row) return row;
    const output = outputs.get(id);
    row = {
      id,
      output,
      name: output?.original_name ?? name ?? "未命名文件",
      mediaType: output?.media_type,
      group: mediaGroup(output?.media_type),
      state: "waiting",
      problems: 0,
      sourcePlan: plans.find((p) =>
        p.actions?.some(
          (a) => a.action_instance_id === output?.source_action_instance_id,
        ),
      ),
      deliveries: [],
    };
    rows.set(id, row);
    return row;
  };
  for (const o of action.outputs ?? []) add(o.output_id);
  // 包含独立动作输入，允许调用者尚未将本次取回加入计划集合。
  const owners: Array<{ owner?: ReportPlan; actions: ReportAction[] }> =
    plans.map((owner) => ({ owner, actions: owner.actions ?? [] }));
  if (
    !owners.some((p) =>
      p.actions.some((a) => a.action_instance_id === action.action_instance_id),
    )
  )
    owners.push({ actions: [action] });
  const files = new Map(videos.map((v) => [v.fileName, v]));
  for (const { owner, actions } of owners)
    for (const a of actions)
      for (const delivery of a.deliveries ?? []) {
        const selected =
          action.type === "obtain_action_outputs"
            ? a.action_instance_id === action.action_instance_id
            : delivery.source_action_instance_id === action.action_instance_id;
        if (selected) {
          const row = add(delivery.output_id, delivery.display_name);
          row.deliveries.push({
            delivery,
            owner,
            video: files.get(delivery.file_name),
          });
        }
      }
  for (const row of rows.values()) {
    row.problems = row.deliveries.filter(
      (d) =>
        d.delivery.status === "failed" ||
        d.video?.status === "mismatch" ||
        d.video?.status === "unavailable",
    ).length;
    row.state = row.deliveries.some((d) => d.video?.status === "verified")
      ? "ready"
      : row.problems
        ? "attention"
        : "waiting";
  }
  return [...rows.values()];
}
