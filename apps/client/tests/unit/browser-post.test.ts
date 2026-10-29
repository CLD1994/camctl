import { EventEmitter } from "node:events";
import type { Page, Request } from "@playwright/test";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { observePost } from "../helpers/browser-post";

const url = "http://127.0.0.1:4310/api/capabilities/reload";
const request = (method = "POST", path = url) =>
  ({
    method: () => method,
    url: () => path,
    failure: () => ({ errorText: "connection closed" }),
  }) satisfies Pick<Request, "method" | "url" | "failure">;
function world() {
  const page = new EventEmitter();
  const observer = observePost(
    page as unknown as Pick<Page, "on" | "off">,
    url,
  );
  return { page, observer };
}
const outcome = <T,>(promise: Promise<T>) =>
  promise.then(
    (value) => ({ value }),
    (error: Error) => ({ error }),
  );
function closed(page: EventEmitter) {
  expect(page.eventNames()).toEqual([]);
  expect(vi.getTimerCount()).toBe(0);
}
beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

it("点击与请求响应各自使用五秒预算，点击等待不消耗响应预算", async () => {
  const { page, observer } = world();
  await vi.advanceTimersByTimeAsync(4000);
  const observed = outcome(observer.wait());
  const post = request();
  page.emit("request", post);
  await vi.advanceTimersByTimeAsync(3000);
  const response = { request: () => post };
  page.emit("response", response);
  expect(await observed).toEqual({ value: response });
  closed(page);
});

it("保留点击尚未返回时已经到达的同请求响应", async () => {
  const { page, observer } = world();
  const post = request();
  const response = { request: () => post };
  page.emit("request", post);
  page.emit("response", response);
  await vi.advanceTimersByTimeAsync(4000);
  expect(await observer.wait()).toBe(response);
  closed(page);
});

it("忽略其他方法、地址与请求身份的响应", async () => {
  const { page, observer } = world();
  const observed = outcome(observer.wait());
  for (const unrelated of [request("GET"), request("POST", url + "/other")]) {
    page.emit("request", unrelated);
    page.emit("response", { request: () => unrelated });
  }
  const post = request();
  page.emit("request", post);
  page.emit("response", { request: () => request() });
  await vi.advanceTimersByTimeAsync(5000);
  expect(((await observed) as { error: Error }).error.message).toContain(
    "没有取得本次 POST 响应",
  );
  closed(page);
});

it.each(["requestfailed", "close", "dispose"])(
  "%s 立即收束观察器",
  async (event) => {
    const { page, observer } = world();
    const post = request();
    page.emit("request", post);
    const observed = outcome(observer.wait());
    if (event === "dispose") observer.dispose();
    else page.emit(event, post);
    expect(await observed).toHaveProperty("error");
    closed(page);
  },
);

it("点击失败后取消尚未等待的观察器，不遗留监听或计时器", () => {
  const { page, observer } = world();
  observer.dispose();
  closed(page);
});

it("没有发出 POST 时耗尽点击后的原预算并清除观察器", async () => {
  const { page, observer } = world();
  const observed = outcome(observer.wait());
  await vi.advanceTimersByTimeAsync(5000);
  expect(((await observed) as { error: Error }).error.message).toContain(
    "没有观察到本次 POST",
  );
  closed(page);
});
