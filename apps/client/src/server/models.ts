export const DRAFT_COMMON_ACTION_FIELDS = ["name", "scheduled_at"] as const;
export type PreviewIntent = "enabled" | "disabled" | "unset";
export interface PreviewMetadata {
  intent: PreviewIntent;
  namespace: string;
  next: number;
  actions: Array<{
    id: string;
    sourceId?: string;
    /** 此自动身份与 sourceId 的可靠协议投影；pending 只由局部名称编辑产生。 */
    rename?: {
      sourceId: string;
      automaticId: string;
      actionName: string;
      pending: boolean;
    };
  }>;
}
export interface DraftContent {
  text: string;
  automaticPreviews?: PreviewMetadata;
  /** 尚不能形成 JSON 值的输入，随草稿保存，存在时禁止导出。 */
  pending?: Record<string, { kind: "number" | "json"; text: string }>;
  /** 非当前动作类型的编辑内容；键为动作下标，不进入执行协议。 */
  actionVariants?: Record<string, ActionVariant[]>;
  /** 当前外层动作分支的停用参数类型内容；键为动作下标。 */
  parameterVariants?: Record<string, ParameterVariant[]>;
}
export interface ParameterVariant {
  /** 仅含可选 params 成员的权威 JSON 原文；缺省与显式值分别保存。 */
  paramsText: string;
  /** 相对于 params 的成员 JSON Pointer，不含整个参数或类型身份输入。 */
  pending: NonNullable<DraftContent["pending"]>;
}
export interface ActionVariant {
  type?: unknown;
  fields: Record<string, unknown>;
  /** 存在时为字段的权威 JSON 原文，fields 为兼容投影；不得失败回退。 */
  fieldsText?: string;
  /** 相对于动作对象的 JSON Pointer。 */
  pending: NonNullable<DraftContent["pending"]>;
  /** 此停用外层动作分支拥有的参数类型内容。 */
  parameterVariants?: ParameterVariant[];
}
export interface Draft {
  id: string;
  revision: number;
  content: DraftContent;
  createdAt: string;
  updatedAt: string;
  exportedRequestId?: string;
  /** 最近用户写入的原始完整依据；后续能力派生不覆盖它。 */
  lastWrite?: { revision: number; input: DraftContent; content: DraftContent };
}
export interface ExportedRequest {
  id: string;
  draftId: string;
  body: Record<string, unknown>;
  exportedAt: string;
  handedAt: string | null;
  copyContent?: DraftContent;
}
export interface Preset {
  id: string;
  name: string;
  deviceId: string;
  actionType: string;
  params: unknown;
  updatedAt: string;
}
export type ImportStatus =
  | "uploading"
  | "received"
  | "processing"
  | "accepted"
  | "covered"
  | "duplicate"
  | "gap"
  | "waiting_report"
  | "verifying"
  | "verified"
  | "mismatch"
  | "conflict"
  | "failed"
  | "interrupted"
  | "unavailable";
export interface ImportFile {
  id: string;
  batchId: string;
  fileName: string;
  kind: "report" | "video" | "media";
  expectedSize: number;
  status: ImportStatus;
  message?: string;
  bytesReceived: number;
  reportId?: string;
  fromWm?: number;
  toWm?: number;
  videoId?: string;
  createdAt: string;
}
export interface ImportBatch {
  id: string;
  fileIds: string[];
  createdAt: string;
  reportsDone: boolean;
}
export interface Video {
  id: string;
  fileName: string;
  path: string;
  size: number;
  sha256?: string;
  status:
    "waiting_report" | "verifying" | "verified" | "mismatch" | "unavailable";
  message?: string;
  importId: string;
  verifiedAgainst?: { size: number; sha256: string };
  verification?: {
    status: "verified" | "mismatch";
    against: { size: number; sha256: string };
  };
  previousId?: string;
}
export class AppError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly status = 400,
    readonly issues?: unknown,
  ) {
    super(message);
  }
}
export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
