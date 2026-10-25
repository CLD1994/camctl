import { useState } from "react";
import {
  resultProducts,
  resultNotes,
  actionIssueText,
  executionText,
  automaticResult,
} from "./result-model";
import { MediaResults } from "./MediaResults";
import { isCameraAction } from "../shared/actions";
import { actionLabel, Badge, Facts } from "./common";
import { utcToLocal } from "./editing";
import { cleanupEntryVisible } from "./device-options";
import type { Capabilities, ReportAction, ReportPlan } from "../shared/types";
import type { Video } from "../server/models";
import type { Followup } from "./Records";
import { defaultObtainParams } from "./obtain";
import { StatusTimeline, statusTimeText } from "./StatusTimes";

export type ActionView = { expanded: boolean; selected: Set<string> };
export type ActionViews = {
  views: Record<string, ActionView>;
  updateView: (id: string, update: (view: ActionView) => ActionView) => void;
};
const initialActionView: ActionView = { expanded: false, selected: new Set() };

export function ActionResult({
  action,
  plan,
  allPlans,
  videos,
  follow,
  run,
  open,
  active,
  capabilities,
  motorInputText,
  source,
  merged,
  association,
  automaticActions = [],
  views,
  updateView,
}: {
  active: boolean;
  capabilities: Capabilities | null;
  motorInputText?: string;
  source?: ReportAction;
  merged?: boolean;
  association?: string;
  automaticActions?: ReportAction[];
  action: ReportAction;
  plan: ReportPlan;
  allPlans: ReportPlan[];
  videos: Video[];
  follow: (f: Followup) => void;
  run: (f: () => Promise<void>) => void;
  open: (id: string) => void;
} & ActionViews) {
  const view = views[action.action_instance_id] ?? initialActionView;
  const { expanded, selected } = view;
  const [technicalOpen, setTechnicalOpen] = useState(false);
  const previews = automaticActions.map((preview) => ({
    action: preview,
    summary: automaticResult(preview, allPlans, videos),
  }));
  const automatic =
    action.automation?.purpose === "auto_preview"
      ? automaticResult(action, allPlans, videos)
      : undefined;
  const products =
    automatic?.products ?? resultProducts(action, allPlans, videos);
  const sourceActions = [
    ...allPlans.flatMap((owner) => owner.actions ?? []),
    ...(source ? [source] : []),
  ];
  const sourceDevices = new Map(
    [plan, ...allPlans].flatMap((owner) =>
      (owner.actions ?? [])
        .filter((candidate) => isCameraAction(candidate.type))
        .map(
          (candidate) =>
            [
              candidate.action_instance_id,
              typeof candidate.device_id === "string"
                ? candidate.device_id
                : undefined,
            ] as const,
        ),
    ),
  );
  const ready = products.filter((p) => p.state === "ready").length;
  const problems =
    automatic?.problems ?? products.reduce((sum, p) => sum + p.problems, 0);
  const result = action.result;
  const counts = [
    ["video", "个视频"],
    ["image", "张图片"],
    ["other", "个其他文件"],
  ].flatMap(([kind, name]) => {
    const n = products.filter((p) => p.group === kind).length;
    return n ? [`${n} ${name}`] : [];
  });
  const issue = action.error;
  const issueText = actionIssueText(action);
  const syncMissing =
    issue?.code === "sync_report_not_found" ||
    (result && JSON.stringify(result).includes("sync_report_not_found"));
  return (
    <article
      className={`action-card result-card${merged ? " automatic-result" : ""}`}
      data-action-id={action.action_instance_id}
    >
      {source && (
        <p className="automatic-source">
          {merged ? "拍摄自动取回" : "自动取回来源"} · {source.name} ·{" "}
          {source.action_instance_id}
        </p>
      )}
      {association && <p className="notice warning">{association}</p>}
      <div className="section-head">
        <div>
          <h3>{action.name}</h3>
          <small>{actionLabel(action.type)}</small>
        </div>
        <Badge value={action.status} />
        <button
          aria-label={`${expanded ? "折叠" : "展开"}动作 ${action.name}`}
          aria-expanded={expanded}
          onClick={() =>
            updateView(action.action_instance_id, (old) => ({
              ...old,
              expanded: !old.expanded,
            }))
          }
        >
          {expanded ? "收起详情" : "展开详情"}
        </button>
      </div>
      <div className="action-summary">
        <p className="muted">
          当前状态时间：{statusTimeText(action.status_history?.at(-1))}
        </p>
        {automatic && (
          <>
            <p>{executionText(action)}</p>
            <p>
              本次自动取回已收到并核验 {automatic.ready} 个文件
              {automatic.repaired
                ? `；修复成品已收到 ${automatic.repaired} 个`
                : ""}
            </p>
            <div className="button-row">
              {automatic.stages.map((s) => (
                <span key={s.status}>
                  <Badge value={s.status} /> {s.count} 项
                </span>
              ))}
            </div>
            {automatic.waitingPublished > 0 && (
              <p>主机已发布，等待接收 {automatic.waitingPublished} 项</p>
            )}
            <div className="button-row">
              {automatic.local.map((s) => (
                <span
                  key={s.status}
                  className={
                    ["mismatch", "unavailable"].includes(s.status)
                      ? "notice error"
                      : ""
                  }
                >
                  本地副本 <Badge value={s.status} /> {s.count} 项
                </span>
              ))}
            </div>
          </>
        )}
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
        {(automatic?.notes ?? resultNotes(action, sourceActions)).map(
          (note, i) => (
            <p key={i} className={note.error ? "notice error" : ""}>
              {note.text}
            </p>
          ),
        )}
        {issueText && <p className="notice error">{issueText}</p>}
        {problems > 0 && (
          <p className="notice error">{problems} 次交付或文件核验异常</p>
        )}
        {previews
          .filter(
            ({ action: preview, summary }) =>
              ["failed", "canceled", "expired"].includes(preview.status) ||
              summary.problems > 0 ||
              summary.notes.some((note) => note.error),
          )
          .map(({ action: preview, summary }) => (
            <p key={preview.action_instance_id} className="notice error">
              自动预览异常 · {preview.name} <Badge value={preview.status} />
              {summary.problems > 0 &&
                ` · ${summary.problems} 次交付或文件核验异常`}
              {" · 原因见执行技术详情"}
            </p>
          ))}
      </div>
      <div hidden={!expanded}>
        <StatusTimeline history={action.status_history} />
        <div className="button-row">
          <span>{executionText(action)}</span>
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
            取消动作
          </button>
          {isCameraAction(action.type) && (
            <>
              <button
                onClick={(event) => {
                  const entry = event.currentTarget;
                  run(async () =>
                    follow({
                      entry,
                      action: {
                        name: `取回 ${action.name}`,
                        type: "obtain_action_outputs",
                        params: await defaultObtainParams(
                          { action_instance_id: action.action_instance_id },
                          allPlans,
                        ),
                      },
                      summary: `按 ${action.name} 已保存的默认范围取回：排除预览，并优先对应修复成品`,
                    }),
                  );
                }}
              >
                取回默认产物
              </button>
              {cleanupEntryVisible(capabilities, action.device_id) && (
                <button
                  onClick={() =>
                    follow({
                      action: {
                        name: `清理 ${action.name}`,
                        type: "delete_action_outputs",
                        params: {
                          source: {
                            action_instance_id: action.action_instance_id,
                          },
                        },
                      },
                      summary: `清理 ${action.name} 的全部正式源产物，包括原文件、预览和修复成品；不清理客户端副本`,
                    })
                  }
                >
                  清理全部产物
                </button>
              )}
            </>
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
        {previews.length > 0 && (
          <div className="automatic-preview-status">
            {previews.map(({ action: preview, summary }) => (
              <p key={preview.action_instance_id}>
                自动预览 <Badge value={preview.status} />
                {summary.products.length > 0 &&
                  ` · 已核验 ${summary.ready} / ${summary.products.length} 个文件`}
                {summary.waitingPublished > 0 &&
                  ` · 等待接收 ${summary.waitingPublished} 个文件`}
              </p>
            ))}
          </div>
        )}
        {products.length > 0 && (
          <MediaResults
            products={products}
            capabilities={capabilities}
            sourceDevices={sourceDevices}
            active={active && expanded}
            follow={follow}
            run={run}
            open={open}
            selected={selected}
            setSelected={(update) =>
              updateView(action.action_instance_id, (old) => ({
                ...old,
                selected: update(old.selected),
              }))
            }
          />
        )}
        <details
          className="advanced"
          onToggle={(event) => setTechnicalOpen(event.currentTarget.open)}
        >
          <summary>执行技术详情</summary>
          <p className="identifier">{action.action_instance_id}</p>
          {action.automation && <Facts value={action.automation} />}
          {action.error && <Facts value={action.error} />}{" "}
          {action.result && <Facts value={action.result} business />}
          {automaticActions.length > 0 && <h4>自动预览取回详情</h4>}
          {automaticActions.map((preview) => (
            <ActionResult
              key={preview.action_instance_id}
              action={preview}
              source={action}
              merged
              active={active && expanded && technicalOpen}
              {...{
                plan,
                allPlans,
                videos,
                follow,
                run,
                open,
                capabilities,
                views,
                updateView,
              }}
            />
          ))}
        </details>
        <details>
          <summary>输入与生效参数</summary>
          {action.type === "motor_control" && motorInputText !== undefined && (
            <>
              <p>输入参数</p>
              <pre data-testid="motor-input-params">{motorInputText}</pre>
            </>
          )}
          <Facts
            value={{
              ...(action.type === "motor_control" &&
              motorInputText !== undefined
                ? {}
                : { 输入参数: action.input_params }),
              生效参数: action.effective_params,
              业务策略: action.policy,
            }}
          />
        </details>
      </div>
    </article>
  );
}
