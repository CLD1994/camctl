import type { ReactNode } from "react";
import type { Issue } from "../shared/types";
export const actionNames: Record<string, string> = {
  camera_record: "录像",
  obtain_action_outputs: "取回产物",
  delete_action_outputs: "清理源产物",
  cancel_task: "取消任务",
  report_status: "状态报告",
};
const labels: Record<string, string> = {
  pending: "等待执行",
  running: "执行中",
  completed: "已结束",
  succeeded: "成功",
  failed: "失败",
  expired: "已过期",
  canceled: "已取消",
  uploading: "上传中",
  received: "已完整接收",
  processing: "处理中",
  accepted: "报告已应用",
  covered: "报告已接受，已覆盖",
  duplicate: "已导入，复用记录",
  gap: "缺少历史",
  waiting_report: "已保存，等待报告",
  verifying: "核验中",
  verified: "核验通过",
  mismatch: "核验不符",
  conflict: "内容冲突",
  interrupted: "已中断",
  unavailable: "文件不可用",
  available: "可用",
  restricted: "受限",
  cleaned: "已清理",
  missing: "缺失",
  unknown: "未知",
  not_requested: "未请求清理",
  incomplete: "未完成",
  not_obtained: "尚未取得",
  not_performed: "未执行",
  unconfirmed: "无法确认",
  preparing: "准备中",
  prepared: "已准备",
  publishing: "发布中",
  published: "主机已发布",
  withdrawn: "已撤回",
  matched: "一致",
  mismatched: "不一致",
  not_needed: "无需执行",
  source_checksum_unavailable: "来源摘要不可用",
  scheduled_time: "等待执行时间",
  device_busy: "设备忙",
  device_reserved: "设备已保留",
  retry_delay: "等待重试",
  source_actions: "等待来源动作",
  copy_slot: "等待复制资源",
  readers: "等待读取结束",
  clock_untrusted: "主机时钟不可信",
  report_publication: "等待报告发布",
};
const own = (dictionary: Record<string, string>, key: string) =>
  Object.hasOwn(dictionary, key) ? dictionary[key] : undefined;
export const actionLabel = (value: string) => own(actionNames, value) ?? value;
export const label = (value: unknown) =>
  typeof value === "string"
    ? (own(labels, value) ?? own(actionNames, value) ?? value)
    : String(value);
export function Badge({ value }: { value: unknown }) {
  return (
    <span
      className={`badge ${["failed", "mismatch", "gap", "unavailable", "conflict", "expired"].includes(String(value)) ? "danger" : ["verified", "succeeded", "accepted"].includes(String(value)) ? "good" : ""}`}
    >
      {label(value)}
    </span>
  );
}
export function ErrorBox({ error }: { error: unknown }) {
  if (!error) return null;
  return (
    <div role="alert" className="notice error">
      {error instanceof Error ? error.message : String(error)}
    </div>
  );
}
export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}
const issueHints: Record<string, string> = {
  schema_required: "缺少必填字段",
  schema_enum: "请选择规则允许的值",
  schema_const: "参数组合或固定值不符合规则",
  schema_type: "值类型不符合规则",
  schema_if: "参数组合不符合规则",
  schema_additionalProperties: "包含未开放的参数",
  schema_minimum: "小于允许的最小值",
  schema_maximum: "超过允许的最大值",
};
export function Issues({ issues }: { issues: Issue[] }) {
  return (
    <div
      data-testid="validation-issues"
      className={issues.length ? "validation" : "valid"}
      aria-live="polite"
    >
      {issues.length ? (
        <>
          <strong>请检查以下输入</strong>
          <ul>
            {issues.map((issue, i) => (
              <li key={i}>
                <code>{issue.path || "计划"}</code> ·{" "}
                {own(issueHints, issue.code) ?? issue.message}
                <small>
                  {issue.code}
                  {own(issueHints, issue.code) ? ` · ${issue.message}` : ""}
                </small>
              </li>
            ))}
          </ul>
        </>
      ) : (
        <span>当前输入检查通过；导出时后端将检查完整计划。</span>
      )}
    </div>
  );
}
const statusFields = new Set([
  "status",
  "availability",
  "outcome",
  "check_status",
]);
const resultContainers = new Set([
  "recording",
  "start",
  "stop",
  "repair",
  "check",
  "source_copy",
  "cleanup",
  "checksum",
  "media",
  "duration",
  "verification",
  "work_file_cleanup",
  "attempts",
  "read_attempts",
  "failures",
  "items",
  "result",
]);
export function Facts({
  value,
  business = false,
}: {
  value: unknown;
  business?: boolean;
}) {
  if (value === null) return <span>null</span>;
  if (Array.isArray(value))
    return value.length ? (
      <ol className="facts-array">
        {value.map((v, i) => (
          <li key={i}>
            <Facts value={v} business={business} />
          </li>
        ))}
      </ol>
    ) : (
      <span>[]</span>
    );
  if (typeof value === "object" && value)
    return (
      <dl className="facts">
        {Object.entries(value).map(([k, v]) => (
          <div key={k}>
            <dt>{own(fieldNames, k) ?? k}</dt>
            <dd>
              {business && statusFields.has(k) && typeof v === "string" ? (
                <span>{label(v)}</span>
              ) : (
                <Facts
                  value={v}
                  business={business && resultContainers.has(k)}
                />
              )}
            </dd>
          </div>
        ))}
      </dl>
    );
  return <span>{value === undefined ? "未提供" : String(value)}</span>;
}
const fieldNames: Record<string, string> = {
  status: "状态",
  code: "错误码",
  stage: "阶段",
  details: "详细原因",
  started: "已开始执行",
  output_id: "产物 ID",
  source_action_instance_id: "来源动作 ID",
  file_name: "交付文件名",
  display_name: "显示名称",
  size: "大小（字节）",
  availability: "可用性",
  cleanup: "清理",
  checksum: "摘要",
  media: "媒体检查",
  error: "错误",
  duration: "时长",
  seconds: "秒",
  sha256: "SHA-256",
  verification: "复制核验",
  work_file_cleanup: "工作文件清理",
  committed_bytes: "已提交字节",
  attempts: "尝试记录",
  read_attempts: "读取尝试",
  attempt_no: "尝试次数",
  failures: "失败项",
  items: "各项结果",
  outcome: "结果",
  recording: "录像执行",
  start: "启动",
  stop: "停止",
  repair: "修复",
  check: "检查",
  source_copy: "源文件复制",
  report_id: "报告编号",
  waiting: "等待条件",
  kind: "类型",
  original_name: "源文件名称",
  media_type: "媒体类型",
  derived_from_output_id: "派生产物来源",
  control_elapsed_s: "控制计时（秒）",
  result: "结果",
};
