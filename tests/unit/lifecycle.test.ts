import { expect, it, vi } from "vitest";
import { RequestLifecycle, createStop } from "../../src/server/lifecycle";

function barrier() {
  let release!: () => void;
  const promise = new Promise<void>((resolve) => {
    release = resolve;
  });
  return { promise, release };
}
it("停止等待活动处理及其产生的后台工作后才关闭数据库", async () => {
  const requests = new RequestLifecycle();
  const request = barrier();
  const network = barrier();
  const jobs = barrier();
  const active = requests.run(() => request.promise);
  const close = vi.fn();
  const idle = vi.fn(() => jobs.promise);
  const stop = createStop(() => network.promise, requests, idle, close);
  let done = false;
  const stopped = stop().then(() => {
    done = true;
  });
  network.release();
  await Promise.resolve();
  expect(idle).not.toHaveBeenCalled();
  expect(close).not.toHaveBeenCalled();
  request.release();
  await active;
  await Promise.resolve();
  expect(done).toBe(false);
  jobs.release();
  await stopped;
  expect(idle).toHaveBeenCalledOnce();
  expect(close).toHaveBeenCalledOnce();
});
it("重复停止返回同一完成结果并拒绝新的处理", async () => {
  const requests = new RequestLifecycle();
  const network = barrier();
  const stop = createStop(
    () => network.promise,
    requests,
    async () => {},
    () => {},
  );
  const first = stop();
  expect(stop()).toBe(first);
  expect(() => requests.run(async () => {})).toThrow();
  network.release();
  await first;
});
it("活动处理失败仍会释放等待边界", async () => {
  const requests = new RequestLifecycle();
  const gate = barrier();
  const active = requests.run(async () => {
    await gate.promise;
    throw new Error("断连");
  });
  const failed = expect(active).rejects.toThrow("断连");
  const close = vi.fn();
  const stop = createStop(
    async () => {},
    requests,
    async () => {},
    close,
  );
  const stopped = stop();
  gate.release();
  await failed;
  await stopped;
  expect(close).toHaveBeenCalledOnce();
});
it("关闭监听失败时停止失败且不提前关闭数据库", async () => {
  const requests = new RequestLifecycle();
  const close = vi.fn();
  const stop = createStop(
    async () => {
      throw new Error("关闭失败");
    },
    requests,
    async () => {},
    close,
  );
  await expect(stop()).rejects.toThrow("关闭失败");
  expect(close).not.toHaveBeenCalled();
});
it("后台等待失败时保留数据库并返回故障", async () => {
  const requests = new RequestLifecycle();
  const close = vi.fn();
  const stop = createStop(
    async () => {},
    requests,
    async () => {
      throw new Error("等待失败");
    },
    close,
  );
  await expect(stop()).rejects.toThrow("等待失败");
  expect(close).not.toHaveBeenCalled();
});
