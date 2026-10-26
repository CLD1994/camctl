import { afterEach, expect, it, vi } from "vitest";
import { DraftSession } from "../../src/web/session";
import type { Draft, DraftContent } from "../../src/server/models";
import { record } from "../helpers/draft-conflicts";
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
afterEach(() => vi.useRealTimers());
function setup(first?: Promise<Draft>) {
  const save = vi.fn(
    async (_id: string, revision: number, content: DraftContent) => {
      if (save.mock.calls.length === 1 && first) return first;
      return { ...record(revision + 1, ""), content };
    },
  );
  const session = new DraftSession(
    record(1, "base"),
    {
      save,
      read: async () => record(1, "base"),
    },
    () => {},
  );
  return { session, save };
}
it("停止输入一秒才自动保存，继续输入重新计时", async () => {
  vi.useFakeTimers();
  const { session, save } = setup();
  session.edit(record(1, "one").content);
  session.schedule();
  await vi.advanceTimersByTimeAsync(999);
  expect(save).not.toHaveBeenCalled();
  session.edit(record(1, "two").content);
  session.schedule();
  await vi.advanceTimersByTimeAsync(999);
  expect(save).not.toHaveBeenCalled();
  await vi.advanceTimersByTimeAsync(1);
  expect(save).toHaveBeenCalledTimes(1);
  expect(session.saved).toBe(true);
});
it("自动保存期间的新输入也等待自己的静默窗口", async () => {
  vi.useFakeTimers();
  const first = deferred<Draft>();
  const { session, save } = setup(first.promise);
  session.edit(record(1, "one").content);
  session.schedule();
  await vi.advanceTimersByTimeAsync(1000);
  session.edit(record(1, "two").content);
  session.schedule();
  first.resolve({ ...record(2, "one") });
  await vi.advanceTimersByTimeAsync(999);
  expect(save).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(1);
  expect(save).toHaveBeenCalledTimes(2);
  expect(session.saved).toBe(true);
});
it("显式完成保存立即写入，并等待在途自动保存后的全部新输入", async () => {
  vi.useFakeTimers();
  const first = deferred<Draft>();
  const { session, save } = setup(first.promise);
  session.edit(record(1, "one").content);
  session.schedule();
  await vi.advanceTimersByTimeAsync(1000);
  session.edit(record(1, "two").content);
  session.schedule();
  const flushing = session.flush();
  first.resolve(record(2, "one"));
  await flushing;
  expect(save).toHaveBeenCalledTimes(2);
  expect(session.saved).toBe(true);
  await vi.advanceTimersByTimeAsync(1000);
  expect(save).toHaveBeenCalledTimes(2);
});
