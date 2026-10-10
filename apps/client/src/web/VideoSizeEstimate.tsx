import type { EstimateReason, VideoEstimate } from "../shared/types";
import { formatVideoSize } from "../shared/video-size-estimate";

const reasons: Record<EstimateReason, string> = {
  capabilities_unavailable: "能力说明不可用。",
  rules_updating: "规则正在更新，请等待新的状态观察。",
  reload_unconfirmed: "能力重载结果尚待核实。",
  input_unfinished: "当前输入尚未完成，请修正保留的原文。",
  pending_scope_unknown: "未完成输入的影响范围无法确定，请核对原文和路径。",
  selection_invalid: "请完成或修正设备、动作和参数类型选择。",
  not_provided: "此参数类型未提供视频大小估算信息。",
  params_invalid: "请完成或修正当前拍摄参数。",
  value_missing: "所需参数缺失，请填写对应字段。",
  value_invalid: "所需参数不符合估算要求，请核对对应字段。",
  lookup_missing: "当前选项未提供估算依据。",
  calculation_invalid: "计算未能取得正的有限结果。",
};
const displayNumber = (value: number) =>
  value < 0.01 ? "<0.01" : String(Number(value.toFixed(2)));
/** 只呈现同次输入派生的结果；不拥有编辑、保存或预览操作。 */
export function VideoSizeEstimate({ result }: { result: VideoEstimate }) {
  if (result.kind === "hidden") return null;
  return (
    <div
      className="video-size-estimate"
      data-testid="video-size-estimate"
      role="status"
    >
      {result.kind === "ready" ? (
        <>
          <div data-testid="video-size-value">
            预计视频大小：约 {formatVideoSize(result.sizeBytes)}
          </div>
          <div>预计成片时长：约 {displayNumber(result.playbackSeconds)} 秒</div>
          <div>参考码率：{displayNumber(result.bitrateMbps)} Mbps</div>
          <p>根据参考码率估算，实际大小可能随拍摄内容和编码结果变化。</p>
        </>
      ) : (
        <>
          <div>暂无法估算视频大小：{reasons[result.reason]}</div>
          {result.path && (
            <p>
              {result.label && <span>{result.label} · </span>}
              <code>{result.path}</code>
            </p>
          )}
          {result.diagnostic && <p>{result.diagnostic}</p>}
        </>
      )}
      {result.warning && <p>{result.warning}</p>}
    </div>
  );
}
