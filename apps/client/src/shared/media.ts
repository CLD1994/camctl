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
export function fileMediaType(
  snapshot: StatusReport,
  name: string,
): string | undefined {
  const actions = (snapshot.plans ?? []).flatMap((p) => p.actions ?? []);
  const delivery = actions
    .flatMap((a) => a.deliveries ?? [])
    .find((d) => d.file_name === name);
  return delivery
    ? actions
        .flatMap((a) => a.outputs ?? [])
        .find((o) => o.output_id === delivery.output_id)?.media_type
    : undefined;
}
