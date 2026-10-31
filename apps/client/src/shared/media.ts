import type { StatusReport } from "./types";

const previews: Record<string, "image" | "video"> = {
  "image/jpeg": "image",
  "image/png": "image",
  "image/webp": "image",
  "image/gif": "image",
  "video/mp4": "video",
  "video/webm": "video",
  "video/quicktime": "video",
};
export function previewKind(type?: string): "image" | "video" | undefined {
  return type && Object.hasOwn(previews, type) ? previews[type] : undefined;
}
export function mediaGroup(type?: string): "image" | "video" | "other" {
  if (type?.startsWith("image/")) return "image";
  if (type?.startsWith("video/")) return "video";
  return "other";
}
/** 查看类型只用于分组和媒体响应，不改写报告提供的内容类型。 */
export function playbackMediaType(
  type: string | undefined,
  ...names: Array<string | undefined>
): string | undefined {
  if (
    (type === undefined || type === "application/octet-stream") &&
    names.some((name) => name !== undefined && /\.(mp4|lrf|osv)$/i.test(name))
  )
    return "video/mp4";
  return type;
}
export function fileMediaType(
  snapshot: StatusReport,
  name: string,
): string | undefined {
  const actions = (snapshot.plans ?? []).flatMap((p) => p.actions ?? []);
  const delivery = actions
    .flatMap((a) => a.deliveries ?? [])
    .find((d) => d.file_name === name);
  if (!delivery) return undefined;
  const output = actions
    .flatMap((a) => a.outputs ?? [])
    .find((o) => o.output_id === delivery.output_id);
  return playbackMediaType(
    output?.media_type,
    output?.original_name,
    delivery.file_name,
  );
}

/** 使用实际交付的可读名称；不改变传输与本地文件身份。 */
export function fileDisplayName(snapshot: StatusReport, name: string): string {
  for (const plan of snapshot.plans ?? [])
    for (const action of plan.actions ?? []) {
      const delivery = action.deliveries?.find((d) => d.file_name === name);
      if (delivery) return delivery.display_name;
    }
  return name;
}
