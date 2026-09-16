import type { Application } from "../server/application";
import type { Draft, Video } from "../server/models";
import type { Issue } from "../shared/types";
export type ClientState = ReturnType<Application["state"]> & {
  videos?: Video[];
  workerError?: string | null;
};
export class HttpError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly issues: Issue[] = [],
    readonly status = 0,
  ) {
    super(message);
  }
}
export async function api<T>(
  path: string,
  method = "GET",
  body?: unknown,
): Promise<T> {
  const response = await fetch("/api" + path, {
    method,
    headers:
      body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
    signal: AbortSignal.timeout(30000),
  }).catch((error: unknown) => {
    throw new HttpError(
      "result_unconfirmed",
      `请求结果尚未确认；请恢复连接并核对保存记录。原始诊断：${error instanceof Error ? error.message : String(error)}`,
    );
  });
  const value = await response.json().catch(() => {
    throw new HttpError(
      "result_unconfirmed",
      "无法读取完整响应，请重新读取保存记录以确认结果。",
    );
  });
  if (!response.ok)
    throw new HttpError(
      value.code ?? "http_error",
      value.message ?? `请求失败（${response.status}）`,
      value.issues ?? [],
      response.status,
    );
  return value as T;
}
export async function readDraft(id: string): Promise<Draft> {
  const state = await api<ClientState>("/state");
  const draft = state.drafts?.find((d) => d.id === id);
  if (!draft) throw new Error("无法读取草稿的保存结果");
  return draft;
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
