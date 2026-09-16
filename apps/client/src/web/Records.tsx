import { lazy, Suspense } from "react";
const ActionResult = lazy(() =>
  import("./ActionResult").then((m) => ({ default: m.ActionResult })),
);
import { useState } from "react";
import type { Video } from "../server/models";
import type { ClientState } from "./api";
import { download } from "./api";
import type { PlanRecord } from "./editing";
import { utcToLocal } from "./editing";
import { Badge, Facts, Empty } from "./common";
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
  const [expanded, setExpanded] = useState(true);
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
        <button aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>
          {expanded ? "折叠计划" : "展开计划"}
        </button>
      </div>
      <div hidden={!expanded}>
        <p className="identifier">请求 ID · {record.id}</p>
        <div className="evidence-grid">
          <div>
            <small>本地导出</small>
            <strong>{request ? "已保存原请求" : "没有本地原请求"}</strong>
            {request && (
              <span>{utcToLocal(request.exportedAt).replace("T", " ")}</span>
            )}
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
            {request?.handedAt && (
              <span>
                标记时间：{utcToLocal(request.handedAt).replace("T", " ")}
              </span>
            )}
          </div>
          <div>
            <small>主机报告</small>
            <strong>
              {plan ? "已取得受理与执行依据" : "尚无计划执行依据"}
            </strong>
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
            <Suspense fallback={<p>正在加载动作摘要…</p>}>
              {plan.actions?.map((action) => (
                <ActionResult
                  key={action.action_instance_id}
                  action={action}
                  active={expanded}
                  plan={plan}
                  allPlans={allPlans}
                  videos={state.videos ?? []}
                  follow={follow}
                  run={run}
                  open={open}
                />
              ))}
            </Suspense>
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
      </div>
    </section>
  );
}
