import { AppError } from "./models";

/** HTTP 连接结束和处理函数结束是两个边界；异步处理仍可能保存结果。 */
export class RequestLifecycle {
  stopping = false;
  private readonly active = new Set<Promise<unknown>>();

  run<T>(operation: () => Promise<T>): Promise<T> {
    if (this.stopping)
      throw new AppError("server_stopping", "服务正在停止", 503);
    const result = Promise.resolve()
      .then(operation)
      .finally(() => this.active.delete(result));
    this.active.add(result);
    return result;
  }

  async idle(): Promise<void> {
    while (this.active.size) await Promise.allSettled([...this.active]);
  }
}

export function createStop(
  closeHttp: () => Promise<void>,
  requests: RequestLifecycle,
  idleJobs: () => Promise<void>,
  closeStore: () => void,
): () => Promise<void> {
  let completion: Promise<void> | undefined;
  return () => {
    if (!completion) {
      requests.stopping = true;
      completion = (async () => {
        await Promise.all([closeHttp(), requests.idle()]);
        await idleJobs();
        closeStore();
      })();
    }
    return completion;
  };
}
