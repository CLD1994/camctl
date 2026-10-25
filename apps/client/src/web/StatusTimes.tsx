import type { ReportAction, ReportPlan } from "../shared/types";
import { utcToLocal } from "./editing";
import { Badge } from "./common";

type History = Array<
  | NonNullable<ReportAction["status_history"]>[number]
  | NonNullable<ReportPlan["status_history"]>[number]
>;
export function statusTimeText(entry: History[number] | undefined): string {
  if (!entry) return "报告未提供";
  if (entry.changed_at === null) return "未记录";
  const time = utcToLocal(entry.changed_at).replace("T", " ");
  return entry.clock_status === "trusted"
    ? time
    : `${time}（${entry.clock_status === "invalid" ? "主机时钟无效" : "主机时钟未核验"}）`;
}
export function StatusTimeline({ history }: { history: History | undefined }) {
  return (
    <div className="status-timeline">
      <h4>状态变化时间</h4>
      {history ? (
        <ol>
          {history.map((entry, index) => (
            <li key={index}>
              <Badge value={entry.status} />{" "}
              <span>{statusTimeText(entry)}</span>
            </li>
          ))}
        </ol>
      ) : (
        <p className="muted">报告未提供</p>
      )}
    </div>
  );
}
