import type {
  ReportAction,
  ReportPlan,
  Output,
  Delivery,
} from "../shared/types";
import type { Video } from "../server/models";
import { mediaGroup, playbackMediaType } from "../shared/media";
import { isObject } from "../shared/validation";
import { isCameraAction } from "../shared/actions";
import {
  segmentIssue,
  type SegmentIssue,
  type SegmentKey,
} from "../shared/segment-report-errors";
import { displayNumber } from "../shared/json";
import type { VideoCoverage } from "../shared/status-report.generated";

function segmentIssueText({ details: d }: SegmentIssue): string {
  const key = (value: SegmentKey) =>
    `目录 ${displayNumber(value, "directory_number", value.directory_number)}/索引 ${displayNumber(value, "index", value.index)}`;
  const gap =
    d.gap_count === null
      ? "缺口分类未确认"
      : `已确认 ${displayNumber(d, "gap_count", d.gap_count)} 处缺口`;
  const first = d.first_gap;
  const neighbors = first
    ? [first.left, first.right].flatMap((side) =>
        side.state === "known" ? [side.file_identity!] : [],
      )
    : [];
  const scope = d.default_scope;
  const range =
    scope.state === "unknown"
      ? "默认范围未确认"
      : scope.state === "empty"
        ? "默认范围为空，没有可默认交付的连续部分"
        : `默认连续前缀 ${displayNumber(scope, "member_count", scope.member_count!)} 份原片`;
  return `来源动作 ${d.source_action_instance_id}：${gap}${first ? `，首个缺口 ${key(first.start_key)} 至 ${key(first.end_key)}` : ""}${neighbors.length ? `；可靠邻接文件：${neighbors.join("、")}` : ""}；${range}；已保留 ${displayNumber(d, "retained_original_count", d.retained_original_count)} 份原片，逐文件身份见产物和技术详情。`;
}

function videoCoverageNote(coverage: VideoCoverage) {
  const unit = coverage.metric === "duration_s" ? "秒" : "帧";
  const target = displayNumber(coverage, "target", coverage.target);
  const status = {
    not_performed: "未执行实际内容检查",
    reached: "已达到覆盖目标",
    confirmed_short: "已确认完整内容不足",
    unconfirmed: "内容覆盖未确认",
  }[coverage.status];
  const amount =
    coverage.status === "not_performed"
      ? "实际量未检查"
      : coverage.confirmed === null
        ? "实际累计量未知"
        : `已确认累计 ${displayNumber(coverage, "confirmed", coverage.confirmed)}${unit}`;
  const scope =
    coverage.default_scope.state === "unknown"
      ? "默认视频范围未确认"
      : coverage.default_scope.state === "empty"
        ? "默认视频范围为空"
        : `默认视频范围 ${displayNumber(coverage.default_scope, "member_count", coverage.default_scope.member_count!)} 段`;
  const reason = coverage.reason
    ? `；原因：${coverage.reason.message ?? coverage.reason.code}${coverage.reason.output_id ? `（产物 ${coverage.reason.output_id}）` : ""}`
    : "";
  return {
    text: `视频内容覆盖：${status}，目标 ${target}${unit}，${amount}；${scope}${reason}。`,
    error:
      coverage.status === "confirmed_short" ||
      coverage.status === "unconfirmed",
  };
}

export interface ResultActionRow {
  action: ReportAction;
  automaticActions: ReportAction[];
  association?: string;
}
/** 只按报告中的可靠关系，将自动取回归入拍摄详情。 */
export function resultActionRows(plan: ReportPlan): ResultActionRow[] {
  const actions = plan.actions ?? [];
  const index = new Map(
    actions.map((action) => [action.action_instance_id, action]),
  );
  const rows = actions.map(
    (action): ResultActionRow & { source?: ReportAction } => {
      if (action.automation?.purpose !== "auto_preview")
        return {
          action,
          automaticActions: [],
          ...(isObject(action.input_params) &&
          action.input_params.purpose === "auto_preview"
            ? { association: "自动取回关联尚未确认" }
            : {}),
        };
      const id = action.automation.source_action_instance_id;
      if (!id)
        return {
          action,
          automaticActions: [],
          association: "自动取回关联尚未确认",
        };
      const source = index.get(id);
      if (!source)
        return {
          action,
          automaticActions: [],
          association: `等待来源动作 ${id} 的报告明细`,
        };
      if (!isCameraAction(source.type))
        throw Error("自动预览报告来源不是拍摄动作");
      return { action, automaticActions: [], source };
    },
  );
  const associated = new Map<string, ReportAction[]>();
  for (const row of rows)
    if (row.source) {
      const id = row.source.action_instance_id;
      const group = associated.get(id) ?? [];
      group.push(row.action);
      associated.set(id, group);
    }
  return rows
    .filter((r) => !r.source)
    .map(({ action, association }) => ({
      action,
      association,
      automaticActions: associated.get(action.action_instance_id) ?? [],
    }));
}

export function automaticResult(
  action: ReportAction,
  plans: ReportPlan[],
  videos: Video[],
) {
  const products = resultProducts(action, plans, videos);
  const stages = new Map<Delivery["status"], number>();
  const local = new Map<Video["status"], number>();
  let waitingPublished = 0;
  for (const { deliveries } of products)
    for (const { delivery, video } of deliveries) {
      stages.set(delivery.status, (stages.get(delivery.status) ?? 0) + 1);
      if (video) local.set(video.status, (local.get(video.status) ?? 0) + 1);
      else if (delivery.status === "published") waitingPublished++;
    }
  return {
    products,
    ready: products.filter((p) => p.state === "ready").length,
    repaired: products.filter(
      (p) => p.state === "ready" && p.output?.kind === "repaired",
    ).length,
    problems: products.reduce((sum, p) => sum + p.problems, 0),
    stages: [...stages].map(([status, count]) => ({ status, count })),
    local: [...local].map(([status, count]) => ({ status, count })),
    waitingPublished,
    notes: resultNotes(
      action,
      plans.flatMap((plan) => plan.actions ?? []),
    ),
  };
}

/** 展示报告给出的处理状态和阶段，不从终态推断设备调用。 */
export function executionText(action: ReportAction): string {
  if (action.status === "pending") return "动作尚未进入执行";
  if (action.status === "running")
    return "动作已开始处理；具体进度以报告结果为准";
  if (action.status === "failed")
    return action.error?.stage === "admission"
      ? "受理校验失败，动作未执行"
      : `动作失败，报告阶段：${action.error?.stage ?? "未提供"}；执行经历以具体结果为准`;
  if (action.status === "succeeded")
    return "动作已成功结束；完成依据见具体结果";
  const status = action.status === "canceled" ? "动作已取消" : "动作已过期";
  return action.result ||
    action.outputs?.length ||
    action.deliveries?.length ||
    action.device_execution
    ? `${status}；已报告的结果、产物和设备执行情况分别保留`
    : `${status}；报告未提供开始经历`;
}

export function actionIssueText(action: ReportAction): string | undefined {
  const issue = action.error;
  const segment = segmentIssue(issue);
  if (segment) return segmentIssueText(segment);
  if (issue?.stage === "admission")
    return `受理校验失败，未执行${typeof issue.details?.message === "string" ? `：${issue.details.message}` : "，展开查看输入问题"}`;
  if (action.type === "motor_control") {
    if (issue?.code === "motor_channel_unavailable")
      return "通知通道不可用，未开始发送控制通知";
    if (issue?.code === "motor_notification_failed")
      return "控制通知未完整发送，本次发送已结束";
    if (issue?.code === "motor_notification_unconfirmed")
      return "控制通知发送结果不确定，不会再次发送";
  }
  return issue
    ? typeof issue.details?.message === "string"
      ? issue.details.message
      : `动作失败（${issue.code}，阶段：${issue.stage}），展开查看原因`
    : action.expiration_reason
      ? "已超过允许启动的时间范围"
      : undefined;
}

/** 仅汇总报告明确提供的业务结果，不用动作终态补造逐文件结果。 */
export function resultNotes(
  action: ReportAction,
  sourceActions: readonly ReportAction[] = [],
): Array<{ text: string; error: boolean }> {
  const notes: Array<{ text: string; error: boolean }> = [];
  const result = action.result;
  if (action.type === "motor_control" && action.status === "succeeded")
    notes.push({ text: "控制通知已发送", error: false });
  // 设备执行提示不属于动作 result，没有 result 的动作也要提示。
  if (action.device_execution)
    notes.push({
      text:
        action.device_execution.status === "still_running"
          ? "动作已有结果，设备仍在执行该动作要求的工作"
          : "设备是否已结束该动作的工作尚未确认",
      error: true,
    });
  if (!result) return notes;
  if (isObject(result) && isObject(result.coverage))
    notes.push(videoCoverageNote(result.coverage as unknown as VideoCoverage));
  if (action.type === "obtain_action_outputs" && Array.isArray(result.failures))
    for (const failure of result.failures) {
      if (!isObject(failure)) continue;
      const segment = segmentIssue(failure.error);
      if (segment) notes.push({ text: segmentIssueText(segment), error: true });
      if (
        isObject(failure.error) &&
        failure.error.code === "source_action_failed"
      ) {
        const sourceId =
          typeof failure.source_action_instance_id === "string"
            ? failure.source_action_instance_id
            : typeof failure.error.details === "object" &&
                failure.error.details !== null &&
                "source_action_instance_id" in failure.error.details &&
                typeof failure.error.details.source_action_instance_id ===
                  "string"
              ? failure.error.details.source_action_instance_id
              : undefined;
        const source = sourceId
          ? sourceActions.find(
              (candidate) => candidate.action_instance_id === sourceId,
            )
          : undefined;
        const identity = source
          ? `${source.name} · 动作 ID ${source.action_instance_id}`
          : sourceId
            ? `动作 ID ${sourceId}`
            : "来源动作身份未提供";
        const cause = source?.error;
        const reason =
          typeof cause?.details?.message === "string"
            ? `；来源失败原因：${cause.details.message}`
            : cause?.code
              ? `；来源错误码：${cause.code}`
              : "";
        notes.push({
          text: `来源动作失败，未生成可取回产物或预览；来源：${identity}${reason}`,
          error: true,
        });
      }
    }
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
  if (action.type === "report_status" && action.status === "succeeded")
    notes.push({ text: "主机已生成状态报告", error: false });
  return notes;
}

export interface Product {
  localCopy?: Video & { downloadName: string };
  id: string;
  name: string;
  output?: Output;
  original?: { id: string; output?: Output };
  hasRepaired?: boolean;
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
  const repairedOriginals = new Set(
    [...outputs.values()]
      .filter((o) => o.kind === "repaired")
      .map((o) => o.derived_from_output_id),
  );
  const rows = new Map<string, Product>();
  const add = (id: string, name?: string) => {
    let row = rows.get(id);
    if (row) return row;
    const output = outputs.get(id);
    const originalId =
      output?.kind === "preview"
        ? output.preview_of_output_id
        : output?.kind === "repaired"
          ? output.derived_from_output_id
          : undefined;
    row = {
      id,
      output,
      original: originalId
        ? { id: originalId, output: outputs.get(originalId) }
        : undefined,
      hasRepaired: !!originalId && repairedOriginals.has(originalId),
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
  const copies = new Map<string, NonNullable<Product["localCopy"]>>();
  for (const { actions } of owners)
    for (const candidate of actions)
      for (const delivery of candidate.deliveries ?? []) {
        const copy = files.get(delivery.file_name);
        if (copy?.status === "verified")
          copies.set(delivery.output_id, {
            ...copy,
            downloadName: delivery.display_name,
          });
      }
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
    row.localCopy = copies.get(row.id);
    row.mediaType = playbackMediaType(
      row.mediaType,
      row.output?.original_name,
      ...row.deliveries.map((d) => d.delivery.file_name),
    );
    row.group = mediaGroup(row.mediaType);
    row.problems = row.deliveries.filter(
      (d) =>
        ["failed", "canceled", "withdrawn"].includes(d.delivery.status) ||
        d.video?.status === "mismatch" ||
        d.video?.status === "unavailable",
    ).length;
    row.state = row.deliveries.some((d) => d.video?.status === "verified")
      ? "ready"
      : row.deliveries.some(
            (d) =>
              d.delivery.status === "failed" ||
              d.video?.status === "mismatch" ||
              d.video?.status === "unavailable",
          )
        ? "attention"
        : "waiting";
  }
  return [...rows.values()];
}
