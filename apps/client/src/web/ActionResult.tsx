import { useState } from "react";
import { resultProducts, resultNotes } from "./result-model";
import { MediaResults } from "./MediaResults";
import { isCameraAction } from "../shared/actions";
import { isObject } from "../shared/validation";
import { actionLabel, Badge, Facts } from "./common";
import { utcToLocal } from "./editing";
import type { ReportAction, ReportPlan } from "../shared/types";
import type { Video } from "../server/models";
import type { Followup } from "./Records";

export function ActionResult({
  action,
  allPlans,
  videos,
  follow,
  run,
  open,
  active,
}: {
  active: boolean;
  action: ReportAction;
  plan: ReportPlan;
  allPlans: ReportPlan[];
  videos: Video[];
  follow: (f: Followup) => void;
  run: (f: () => Promise<void>) => void;
  open: (id: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const products = resultProducts(action, allPlans, videos);
  const ready = products.filter((p) => p.state === "ready").length;
  const problems = products.reduce((sum, p) => sum + p.problems, 0);
  const result = action.result;
  const capture = isObject(result?.capture) ? result.capture : undefined;
  const counts = [
    ["video", "个视频"],
    ["image", "张图片"],
    ["other", "个其他文件"],
  ].flatMap(([kind, name]) => {
    const n = products.filter((p) => p.group === kind).length;
    return n ? [`${n} ${name}`] : [];
  });
  const issue = action.error;
  const issueText = issue
    ? typeof issue.details?.message === "string"
      ? issue.details.message
      : "执行遇到问题，展开查看原因"
    : action.expiration_reason
      ? "已超过允许启动的时间范围"
      : undefined;
  const syncMissing =
    issue?.code === "sync_report_not_found" ||
    (result && JSON.stringify(result).includes("sync_report_not_found"));
  return (
    <article className="action-card result-card">
      <div className="section-head">
        <div>
          <h3>{action.name}</h3>
          <small>{actionLabel(action.type)}</small>
        </div>
        <Badge value={action.status} />
        <button
          aria-label={`${expanded ? "折叠" : "展开"}动作 ${action.name}`}
          aria-expanded={expanded}
          onClick={() => setExpanded(!expanded)}
        >
          {expanded ? "收起详情" : "展开详情"}
        </button>
      </div>
      <div className="action-summary">
        {counts.length > 0 ? (
          <p>
            已报告 {counts.join("、")} · 本地已核验 {ready} / {products.length}
          </p>
        ) : isCameraAction(action.type) ? (
          <p>
            {action.outputs === undefined
              ? "报告尚未提供产物明细"
              : "报告中没有已登记产物"}
          </p>
        ) : null}
        {capture?.captured_count !== undefined && (
          <p>已确认完成 {String(capture.captured_count)} 次采集</p>
        )}
        {action.waiting?.map((w, i) => (
          <Badge key={i} value={w.code} />
        ))}
        {resultNotes(action).map((note, i) => (
          <p key={i} className={note.error ? "notice error" : ""}>
            {note.text}
          </p>
        ))}
        {issueText && <p className="notice error">{issueText}</p>}
        {problems > 0 && (
          <p className="notice error">{problems} 次交付或文件核验异常</p>
        )}
      </div>
      <div hidden={!expanded}>
        <div className="button-row">
          <span>
            {action.execution.started ? "执行已开始" : "执行尚未开始"}
          </span>
          {typeof action.device_id === "string" && (
            <span>设备：{action.device_id}</span>
          )}
          {typeof action.scheduled_at === "string" && (
            <span>
              计划时间：
              {utcToLocal(action.scheduled_at).replace("T", " ") || "时间无效"}
            </span>
          )}
          <button
            onClick={() =>
              follow({
                action: {
                  name: `取消 ${action.name}`,
                  type: "cancel_task",
                  params: {
                    target: { action_instance_id: action.action_instance_id },
                  },
                },
                summary: `取消动作 ${action.name}`,
              })
            }
          >
            准备取消动作
          </button>
          {isCameraAction(action.type) && (
            <button
              onClick={() =>
                follow({
                  action: {
                    name: `取回 ${action.name}`,
                    type: "obtain_action_outputs",
                    params: {
                      source: { action_instance_id: action.action_instance_id },
                    },
                  },
                  summary: `取回 ${action.name} 的全部产物`,
                })
              }
            >
              准备取回全部产物
            </button>
          )}
        </div>
        {syncMissing && (
          <button
            onClick={() =>
              follow({
                action: {
                  name: "完整状态同步",
                  type: "report_status",
                  params: { scope: "full" },
                },
                summary: "重新获取完整状态",
                sync: true,
              })
            }
          >
            准备完整状态同步
          </button>
        )}
        {products.length > 0 && (
          <MediaResults
            products={products}
            active={active && expanded}
            follow={follow}
            run={run}
            open={open}
          />
        )}
        <details className="advanced">
          <summary>执行技术详情</summary>
          <p className="identifier">{action.action_instance_id}</p>
          {action.error && <Facts value={action.error} />}{" "}
          {action.waiting && <Facts value={action.waiting} />}{" "}
          {action.result && <Facts value={action.result} business />}
        </details>
        <details>
          <summary>输入与生效参数</summary>
          <Facts
            value={{
              输入参数: action.input_params,
              生效参数: action.effective_params,
              业务策略: action.policy,
            }}
          />
        </details>
      </div>
    </article>
  );
}
