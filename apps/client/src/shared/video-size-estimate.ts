import { validateParams } from "./capabilities";
import { mathematicalInteger, originalNumberToken } from "./json";
import { readJsonPointer } from "./json-pointer";
import { isObject } from "./validation";
import type {
  Capabilities,
  EstimateQuantity,
  EstimateReason,
  VideoEstimate,
} from "./types";

type Unavailable = Extract<VideoEstimate, { kind: "unavailable" }>;
type QuantityResult = { kind: "value"; value: number } | Unavailable;
const unavailable = (reason: EstimateReason, path?: string): Unavailable => ({
  kind: "unavailable",
  reason,
  ...(path === undefined ? {} : { path }),
});
const positiveFinite = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value) && value > 0;

function checkedNumber(
  value: unknown,
  parent: object,
  key: string,
  integer: boolean,
  path?: string,
): QuantityResult {
  if (!positiveFinite(value)) return unavailable("value_invalid", path);
  if (integer) {
    const token = originalNumberToken(parent, key, value);
    if (
      !Number.isInteger(value) ||
      (token !== undefined && !mathematicalInteger(token))
    )
      return unavailable("value_invalid", path);
  }
  return { kind: "value", value };
}
function quantity(
  source: EstimateQuantity,
  params: object,
  integer = false,
): QuantityResult {
  if (source.source === "constant")
    return checkedNumber(source.value, source, "value", integer);
  const read = readJsonPointer(params, source.path);
  if (!read.found) return unavailable("value_missing", source.path);
  if (source.source === "parameter")
    return checkedNumber(
      read.value,
      read.parent,
      read.key,
      integer,
      source.path,
    );
  if (typeof read.value !== "string")
    return unavailable("value_invalid", source.path);
  if (!Object.hasOwn(source.values, read.value))
    return unavailable("lookup_missing", source.path);
  return checkedNumber(
    source.values[read.value],
    source.values,
    read.value,
    integer,
    source.path,
  );
}

/** 输入目录已经通过 loadCapabilities；计算只读取当前动作，不写入任何编辑资料。 */
export function estimateVideoAction(
  action: unknown,
  capabilities: Capabilities | null,
): VideoEstimate {
  if (
    !isObject(action) ||
    !Object.hasOwn(action, "type") ||
    (action.type !== "camera_record" && action.type !== "camera_timelapse")
  )
    return { kind: "hidden" };
  if (!capabilities) return unavailable("capabilities_unavailable");
  const device = Object.hasOwn(action, "device_id")
    ? capabilities.devices.find(
        (candidate) => candidate.device_id === action.device_id,
      )
    : undefined;
  const entry = device?.actions.find(
    (candidate) => candidate.type === action.type,
  );
  const params = Object.hasOwn(action, "params") ? action.params : undefined;
  if (!entry || !isObject(params) || !Object.hasOwn(params, "type"))
    return unavailable("selection_invalid");
  const parameter = entry.parameter_types.find(
    (candidate) => candidate.type === params.type,
  );
  if (!parameter) return unavailable("selection_invalid");
  const definition = parameter.video_size_estimate;
  if (definition === undefined) return unavailable("not_provided");
  if (
    validateParams(device!.device_id, entry.type, params, capabilities).length
  )
    return unavailable("params_invalid");

  const bitrate = definition.bitrate_mbps;
  const rate = quantity(
    typeof bitrate === "number"
      ? { source: "constant", value: bitrate }
      : "from" in bitrate
        ? { source: "parameter", path: bitrate.from }
        : { source: "lookup", path: bitrate.by, values: bitrate.values },
    params,
  );
  if (rate.kind === "unavailable") return rate;
  const duration = definition.duration;
  let playbackSeconds: number;
  if (duration.method === "direct") {
    const seconds = quantity(duration.seconds, params);
    if (seconds.kind === "unavailable") return seconds;
    playbackSeconds = seconds.value;
  } else {
    const frames =
      duration.method === "timelapse_frames"
        ? quantity(duration.frames, params, true)
        : quantity(duration.capture_seconds, params);
    if (frames.kind === "unavailable") return frames;
    const fps = quantity(duration.playback_fps, params);
    if (fps.kind === "unavailable") return fps;
    let frameCount = frames.value;
    if (duration.method === "timelapse_interval") {
      const interval = quantity(duration.interval_seconds, params);
      if (interval.kind === "unavailable") return interval;
      frameCount /= interval.value;
      if (!positiveFinite(frameCount))
        return unavailable("calculation_invalid");
    }
    playbackSeconds = frameCount / fps.value;
  }
  const mb = (rate.value * playbackSeconds) / 8;
  const sizeBytes = mb * 1_000_000;
  if (![playbackSeconds, mb, sizeBytes].every(positiveFinite))
    return unavailable("calculation_invalid");
  return { kind: "ready", bitrateMbps: rate.value, playbackSeconds, sizeBytes };
}
/** 接受有效估算的正字节数，采用十进制 MB/GB，舍入仅用于展示。 */
export function formatVideoSize(sizeBytes: number): string {
  const mb = sizeBytes / 1_000_000;
  if (mb < 0.01) return "<0.01 MB";
  const value = mb >= 1000 ? mb / 1000 : mb;
  return `${Number(value.toFixed(2))} ${mb >= 1000 ? "GB" : "MB"}`;
}
