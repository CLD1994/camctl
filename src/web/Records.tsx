import { useState } from "react";
import type {
  ReportPlan,
  ReportAction,
  Delivery,
  Output,
} from "../shared/types";
import type { Video } from "../server/models";
import type { ClientState } from "./api";
import { download } from "./api";
import type { PlanRecord } from "./editing";
import { Badge, Facts, actionNames, Empty } from "./common";
export type Followup = {
  action: Record<string, unknown>;
  summary: string;
  sync?: boolean;
};
interface Props {
  record: PlanRecord;
  state: ClientState;
  busy: boolean;
  run: (fn: () => Promise<void>) => void;
  copy: (id: string) => void;
  handoff: (id: string, marked: boolean) => void;
  follow: (input: Followup) => void;
  open: (id: string) => void;
}
export function RecordDetail({
  record,
  state,
  busy,
  run,
  copy,
  handoff,
  follow,
  open,
}: Props) {
  const { request, plan } = record;
  const allPlans = state.snapshot?.plans ?? [];
  const diagnostics =
    state.snapshot?.plan_file_diagnostics?.filter(
      (d) => d.request_id === record.id,
    ) ?? [];
  return (
    <section className="panel record-detail">
      <p className="eyebrow">已保存的计划记录</p>
      <div className="section-head">
        <h2>{plan?.name ?? String(request?.body.name ?? record.id)}</h2>
        {plan && <Badge value={plan.status} />}
      </div>
      <p className="identifier">请求 ID · {record.id}</p>
      <div className="evidence-grid">
        <div>
          <small>本地导出</small>
          <strong>{request ? "已保存原请求" : "没有本地原请求"}</strong>
          {request && <span>{request.exportedAt} UTC</span>}
        </div>
        <div>
          <small>人工递交记录</small>
          <strong>
            {request
              ? request.handedAt
                ? "已标记递交"
                : "未标记递交"
              : "无本地记录"}
          </strong>
          {request?.handedAt && <span>标记时间：{request.handedAt} UTC</span>}
        </div>
        <div>
          <small>主机报告</small>
          <strong>{plan ? "已取得受理与执行依据" : "尚无计划执行依据"}</strong>
          {plan && <span>{plan.plan_instance_id}</span>}
        </div>
      </div>
      {request && (
        <div className="button-row">
          <button
            data-testid="download-request-button"
            disabled={busy}
            onClick={() =>
              run(() =>
                download(
                  `/requests/${record.id}/download`,
                  `plan-${record.id}.json`,
                ),
              )
            }
          >
            再次下载原请求
          </button>
          <button
            data-testid="copy-request-button"
            disabled={busy}
            onClick={() => copy(record.id)}
          >
            复制为新草稿
          </button>
          <button
            disabled={busy}
            onClick={() => handoff(record.id, !request.handedAt)}
          >
            {request.handedAt ? "清除递交标记" : "标记已递交"}
          </button>
        </div>
      )}
      <p className="muted">
        人工标记说明记录交接的时间。主机是否受理、动作是否成功，依据已应用报告分别展示。
      </p>
      {diagnostics.map((d) => (
        <section key={d.diagnostic_id} className="notice error">
          <h3>主机输入诊断 · {d.file_name}</h3>
          <Facts value={d.errors} />
        </section>
      ))}
      {plan ? (
        <>
          <div className="section-head">
            <h3>执行与结果</h3>
            <button
              disabled={busy}
              onClick={() =>
                follow({
                  action: {
                    name: `取消 ${plan.name}`,
                    type: "cancel_task",
                    params: {
                      target: { plan_instance_id: plan.plan_instance_id },
                    },
                  },
                  summary: `取消计划 ${plan.name} · ${plan.plan_instance_id}`,
                })
              }
            >
              准备取消计划
            </button>
          </div>
          {plan.status === "completed" && (
            <p className="notice">
              计划已结束，各动作可能成功、失败、取消或过期，请分别查看。
            </p>
          )}
          {plan.actions?.map((action) => (
            <ActionResult
              key={action.action_instance_id}
              action={action}
              plan={plan}
              allPlans={allPlans}
              videos={state.videos ?? []}
              follow={follow}
              run={run}
              open={open}
            />
          ))}
          {!plan.actions?.length && <Empty>报告尚未提供动作明细。</Empty>}
        </>
      ) : (
        <Empty>等待状态报告。导出和人工递交标记不表示主机已开始执行。</Empty>
      )}
      {request && (
        <details className="advanced">
          <summary>查看固定原请求 JSON</summary>
          <pre>{JSON.stringify(request.body, null, 2)}</pre>
        </details>
      )}
      {plan && (
        <details className="advanced">
          <summary>查看合并后的报告计划 JSON</summary>
          <pre>{JSON.stringify(plan, null, 2)}</pre>
        </details>
      )}
    </section>
  );
}
function ActionResult({
  action,
  plan,
  allPlans,
  videos,
  follow,
  run,
  open,
}: {
  action: ReportAction;
  plan: ReportPlan;
  allPlans: ReportPlan[];
  videos: Video[];
  follow: (f: Followup) => void;
  run: (f: () => Promise<void>) => void;
  open: (id: string) => void;
}) {
  const outputIds = new Set(action.outputs?.map((o) => o.output_id));
  const deliveries = new Map<
    string,
    { delivery: Delivery; owner: ReportPlan }
  >();
  for (const owner of allPlans)
    for (const item of owner.actions ?? [])
      for (const delivery of item.deliveries ?? [])
        if (
          item.action_instance_id === action.action_instance_id ||
          delivery.source_action_instance_id === action.action_instance_id ||
          outputIds.has(delivery.output_id)
        )
          deliveries.set(delivery.delivery_id, { delivery, owner });
  return (
    <article className="action-card result-card">
      <div className="section-head">
        <div>
          <h3>{action.name}</h3>
          <small>
            {actionNames[action.type]} · {action.action_instance_id}
          </small>
        </div>
        <Badge value={action.status} />
      </div>
      <div className="button-row">
        <span className="muted">
          {action.execution.started ? "执行已开始" : "执行尚未开始"}
        </span>
        {action.scheduled_at !== undefined && (
          <span className="muted">
            计划时间：{String(action.scheduled_at)} UTC
          </span>
        )}
        <button
          className="inline"
          onClick={() =>
            follow({
              action: {
                name: `取消 ${action.name}`,
                type: "cancel_task",
                params: {
                  target: { action_instance_id: action.action_instance_id },
                },
              },
              summary: `取消动作 ${action.name} · ${action.action_instance_id}`,
            })
          }
        >
          准备取消动作
        </button>
      </div>
      {action.waiting && (
        <div className="notice">
          <h4>等待条件</h4>
          <Facts value={action.waiting} />
        </div>
      )}
      {action.expiration_reason && <p>过期原因：{action.expiration_reason}</p>}
      {action.error && (
        <div className="notice error">
          <Facts value={action.error} />
        </div>
      )}
      {action.result && (
        <section>
          <h4>动作结果</h4>
          <Facts value={action.result} />
          {JSON.stringify(action.result).includes("sync_report_not_found") && (
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
        </section>
      )}
      {action.error?.code === "sync_report_not_found" && (
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
      {action.outputs?.map((output) => (
        <OutputCard key={output.output_id} output={output} follow={follow} />
      ))}
      {[...deliveries.values()].map(({ delivery, owner }) => {
        const source = allPlans.find((p) =>
          p.actions?.some(
            (a) => a.action_instance_id === delivery.source_action_instance_id,
          ),
        );
        return (
          <section className="delivery" key={delivery.delivery_id}>
            <div className="section-head">
              <h4>{delivery.display_name}</h4>
              <Badge value={delivery.status} />
            </div>
            <p className="identifier">
              {delivery.file_name} · 交付 {delivery.delivery_id}
            </p>
            <p className="muted">
              产物 {delivery.output_id} · 来源动作{" "}
              {delivery.source_action_instance_id}
            </p>
            {source && source.request_id !== plan.request_id && (
              <button
                className="inline"
                onClick={() => open(source.request_id)}
              >
                查看来源计划：{source.name}
              </button>
            )}
            {owner.request_id !== plan.request_id && (
              <button className="inline" onClick={() => open(owner.request_id)}>
                查看取回计划：{owner.name}
              </button>
            )}
            {delivery.error && (
              <div className="notice error">
                <Facts value={delivery.error} />
              </div>
            )}
            <details>
              <summary>复制进度与核验</summary>
              <Facts value={delivery.copy} />
            </details>
            <VideoPanel
              video={videos.find((v) => v.fileName === delivery.file_name)}
              published={delivery.status === "published"}
              run={run}
            />
          </section>
        );
      })}
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
    </article>
  );
}
function OutputCard({
  output,
  follow,
}: {
  output: Output;
  follow: (f: Followup) => void;
}) {
  return (
    <section className="output">
      <div className="section-head">
        <h4>产物 · {output.original_name ?? output.output_id}</h4>
        <Badge value={output.availability} />
      </div>
      <p className="identifier">{output.output_id}</p>
      <Facts
        value={{
          类型: output.kind,
          字节数: output.size,
          清理: output.cleanup,
          摘要: output.checksum,
          媒体: output.media,
          ...(output.error ? { 错误: output.error } : {}),
        }}
      />
      <div className="button-row">
        <button
          onClick={() =>
            follow({
              action: {
                name: "取回产物",
                type: "obtain_action_outputs",
                params: {
                  source: {
                    action_instance_id: output.source_action_instance_id,
                  },
                  output_ids: [output.output_id],
                },
              },
              summary: `取回产物 ${output.output_id}`,
            })
          }
        >
          准备取回
        </button>
        <button
          onClick={() =>
            follow({
              action: {
                name: "清理源产物",
                type: "delete_action_outputs",
                params: { output_ids: [output.output_id] },
              },
              summary: `清理相机源产物 ${output.output_id}`,
            })
          }
        >
          准备清理源产物
        </button>
      </div>
    </section>
  );
}
function VideoPanel({
  video,
  published,
  run,
}: {
  video?: Video;
  published: boolean;
  run: (f: () => Promise<void>) => void;
}) {
  const [playbackError, setPlaybackError] = useState(false);
  if (!video)
    return (
      <p className="notice">
        本地视频：{published ? "等待接收" : "尚无本地副本"}
      </p>
    );
  return (
    <div className="video-panel">
      <div className="section-head">
        <strong>本地视频</strong>
        <Badge value={video.status} />
      </div>
      {video.message && <p>{video.message}</p>}
      {video.status === "verified" && (
        <>
          <video
            controls
            preload="metadata"
            src={`/api/videos/${video.id}/content`}
            onError={() => setPlaybackError(true)}
          />
          {playbackError && (
            <p role="alert" className="notice">
              无法在浏览器播放。编码可能不受支持；可以下载原文件使用其他播放器。文件可用性以当前核验状态为准。
            </p>
          )}
          <button
            onClick={() =>
              run(() =>
                download(
                  `/videos/${video.id}/content?download=true`,
                  video.fileName,
                ),
              )
            }
          >
            下载原视频
          </button>
        </>
      )}
    </div>
  );
}
