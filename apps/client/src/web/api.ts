import type { Application } from "../server/application";
import type {
  Draft,
  DraftContent,
  DraftCreationResult,
  Video,
} from "../server/models";
import { parseClientJson, stringifyJson } from "../shared/json";
import { sameContent } from "../shared/automatic-previews";
import { requireDraftCreationResult as validateDraftCreationResult } from "../shared/draft-creation";
import { requireDraftRecord } from "../shared/draft-record";
import type { DraftObservation } from "./draft-conflicts";
import { SupersededStateReadError } from "./state-observations";
export type ClientState = ReturnType<Application["state"]> & {
  videos?: Video[];
  workerError?: string | null;
};
export { HttpError } from "../shared/http-error";
import { HttpError } from "../shared/http-error";
export async function api<T>(
  path: string,
  method = "GET",
  body?: unknown,
): Promise<T> {
  const response = await fetch("/api" + path, {
    method,
    headers:
      body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : stringifyJson(body),
    cache: "no-store",
    signal: AbortSignal.timeout(30000),
  }).catch((error: unknown) => {
    throw new HttpError(
      "result_unconfirmed",
      `请求结果尚未确认；请恢复连接并核对保存记录。原始诊断：${error instanceof Error ? error.message : String(error)}`,
    );
  });
  const value = await response
    .text()
    .then((text) => parseClientJson(text) as any)
    .catch(() => {
      throw new HttpError(
        "result_unconfirmed",
        "无法读取完整响应，请重新读取保存记录以确认结果。",
      );
    });
  if (!response.ok) {
    if (
      typeof value !== "object" ||
      value === null ||
      Array.isArray(value) ||
      typeof value.code !== "string" ||
      !value.code.length ||
      typeof value.message !== "string" ||
      (value.issues !== undefined && !Array.isArray(value.issues))
    )
      throw new HttpError(
        "result_unconfirmed",
        "错误响应不完整，请重新读取保存记录以确认结果。",
      );
    throw new HttpError(
      value.code,
      value.message,
      value.issues ?? [],
      response.status,
    );
  }
  return value as T;
}
export async function createDraftCopy(
  attemptId: string,
  content: DraftContent,
): Promise<DraftCreationResult> {
  const result = requireDraftCreationResult(
    await api(`/draft-creations/${encodeURIComponent(attemptId)}`, "PUT", {
      content,
    }),
    attemptId,
  );
  if (result.kind === "created" && !sameContent(result.creation.input, content))
    throw new HttpError(
      "result_unconfirmed",
      "创建回执的固定输入与本次输入不一致。",
    );
  return result;
}
export async function readDraftCreation(
  attemptId: string,
): Promise<DraftCreationResult> {
  return requireDraftCreationResult(
    await api(`/draft-creations/${encodeURIComponent(attemptId)}`),
    attemptId,
  );
}
function requireDraftCreationResult(
  value: unknown,
  attemptId: string,
): DraftCreationResult {
  try {
    return validateDraftCreationResult(value, attemptId);
  } catch {
    throw new HttpError(
      "result_unconfirmed",
      "无法读取完整创建结果，请按原尝试身份重新核实。",
    );
  }
}
export async function readDraft(
  id: string,
  readState: () => Promise<ClientState>,
): Promise<Draft> {
  const observation = await readDraftObservation(id, readState);
  if (observation.kind === "available") return observation.draft;
  throw new Error(
    observation.kind === "unavailable"
      ? observation.message
      : "草稿已删除，无法读取保存结果",
  );
}
/** 可靠存在和可靠删除共同要求启动正常且整个集合满足完整记录契约。 */
export function observeDraft(id: string, state: ClientState): DraftObservation {
  try {
    if (
      !state ||
      state.startup?.state !== "ready" ||
      !Array.isArray(state.drafts)
    )
      throw Error("客户端启动状态或草稿集合不可用");
    const ids = new Set<string>();
    let target: Draft | undefined;
    for (const item of state.drafts) {
      const draft = requireDraftRecord(item);
      if (ids.has(draft.id)) throw Error(`草稿集合存在重复身份：${draft.id}`);
      ids.add(draft.id);
      if (draft.id === id) target = draft;
    }
    return target
      ? { kind: "available", draft: target, capabilities: state.capabilities }
      : { kind: "deleted" };
  } catch (error) {
    return {
      kind: "unavailable",
      message: `无法可靠读取草稿：${error instanceof Error ? error.message : String(error)}`,
    };
  }
}
export async function readDraftObservation(
  id: string,
  readState: () => Promise<ClientState>,
): Promise<DraftObservation> {
  try {
    return observeDraft(id, await readState());
  } catch (error) {
    if (error instanceof SupersededStateReadError) throw error;
    return {
      kind: "unavailable",
      message: `无法读取草稿观察：${error instanceof Error ? error.message : String(error)}`,
    };
  }
}
export async function download(path: string, name: string) {
  const response = await fetch("/api" + path, { cache: "no-store" });
  if (!response.ok) {
    const value = await response.json();
    throw new HttpError(
      value.code,
      value.message,
      value.issues,
      response.status,
    );
  }
  const blob = await response.blob(),
    url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}
export function upload(
  id: string,
  file: File,
  progress: (bytes: number) => void,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", `/api/imports/${encodeURIComponent(id)}/content`);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (event) => progress(event.loaded);
    xhr.onerror = () => reject(new Error("上传连接中断；正在核对后端保存结果"));
    xhr.onabort = () => reject(new Error("文件上传已中断"));
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve();
      else {
        try {
          const result = JSON.parse(xhr.responseText);
          reject(
            new HttpError(
              result.code,
              result.message,
              result.issues,
              xhr.status,
            ),
          );
        } catch {
          reject(new Error(`文件上传失败（${xhr.status}）`));
        }
      }
    };
    try {
      xhr.send(file);
    } catch (error) {
      reject(error);
    }
  });
}
