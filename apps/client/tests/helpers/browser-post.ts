import type { Page, Request, Response } from "@playwright/test";

/** 先观察点击可能同步发出的请求，再等待这次点击后的请求与响应。 */
export function observePost(
  page: Pick<Page, "on" | "off">,
  url: string,
  timeout = 5000,
) {
  let request: Request | undefined;
  let result: { response: Response } | { error: Error } | undefined;
  let waiting: Promise<Response> | undefined;
  let deliver: ((value: NonNullable<typeof result>) => void) | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const remove = () => {
    page.off("request", onRequest);
    page.off("response", onResponse);
    page.off("requestfailed", onFailure);
    page.off("close", onClose);
    if (timer !== undefined) clearTimeout(timer);
  };
  const finish = (value: NonNullable<typeof result>) => {
    if (result !== undefined) return;
    result = value;
    remove();
    deliver?.(value);
  };
  const onRequest = (value: Request) => {
    if (value.method() === "POST" && value.url() === url) request ??= value;
  };
  const onResponse = (value: Response) => {
    if (request !== undefined && value.request() === request)
      finish({ response: value });
  };
  const onFailure = (value: Request) => {
    if (value === request)
      finish({ error: Error(`本次 POST 失败：${value.failure()?.errorText}`) });
  };
  const onClose = () =>
    finish({ error: Error("页面已关闭，无法取得本次 POST 响应") });
  const expire = () =>
    finish({
      error: Error(
        request === undefined
          ? `点击完成后 ${timeout}ms 内没有观察到本次 POST`
          : `点击完成后 ${timeout}ms 内没有取得本次 POST 响应`,
      ),
    });
  page.on("request", onRequest);
  page.on("response", onResponse);
  page.on("requestfailed", onFailure);
  page.on("close", onClose);
  return {
    wait: () =>
      (waiting ??= new Promise<Response>((resolve, reject) => {
        deliver = (value) =>
          "error" in value ? reject(value.error) : resolve(value.response);
        if (result !== undefined) deliver(result);
        else timer = setTimeout(expire, timeout);
      })),
    dispose: () => finish({ error: Error("本次 POST 观察已结束") }),
  };
}
