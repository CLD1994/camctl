import { expect, it } from "vitest";
import { StateObservations } from "../../src/web/state-observations";
function gate<T>() {
  let resolve!: (value: T) => void, reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}
function setup() {
  const pending: ReturnType<typeof gate<string>>[] = [],
    accepted: Array<[string, number]> = [];
  let token = 1;
  const observations = new StateObservations(
    () => {
      const request = gate<string>();
      pending.push(request);
      return request.promise;
    },
    () => token,
    (value, captured) => accepted.push([value, captured]),
    () => {},
  );
  return { observations, pending, accepted, changeToken: () => token++ };
}
it.each([false, true])(
  "准备失败恢复原未核实责任 %s 且之后新观察可以核实",
  async (prior) => {
    const { observations: model, pending } = setup();
    if (prior) {
      model.beginReload();
      model.postStarted();
      model.postEnded();
      model.reloadFailed();
    }
    model.beginReload();
    expect(model.phase).toBe("updating");
    model.preparationFailed();
    expect(model.phase).toBe(prior ? "unconfirmed" : "idle");
    const read = model.read();
    pending[0].resolve("actual");
    await read;
    expect(model.phase).toBe("idle");
  },
);
it.each(["old", "preparing", "posting"])(
  "%s 发起的观察晚到不能解锁",
  async (timing) => {
    const { observations: model, pending } = setup();
    let read: Promise<string>;
    if (timing === "old") read = model.read();
    model.beginReload();
    if (timing === "preparing") read = model.read();
    model.postStarted();
    if (timing === "posting") read = model.read();
    model.postEnded();
    model.reloadFailed();
    pending[0].resolve("old");
    await read!;
    expect(model.phase).toBe("unconfirmed");
    const actual = model.read();
    pending[1].resolve("actual");
    await actual;
    expect(model.phase).toBe("idle");
  },
);
it("更早代次结束后发起的观察不能核实新尝试", async () => {
  const { observations: model, pending } = setup();
  model.beginReload();
  model.postStarted();
  model.postEnded();
  model.reloadFailed();
  const previous = model.read();
  model.beginReload();
  model.postStarted();
  model.postEnded();
  model.reloadFailed();
  pending[0].resolve("previous");
  await previous;
  expect(model.phase).toBe("unconfirmed");
});
it("准备期间成功观察不能确认前次责任，准备失败仍待核实", async () => {
  const { observations: model, pending } = setup();
  model.beginReload();
  model.postStarted();
  model.postEnded();
  model.reloadFailed();
  model.beginReload();
  const preparation = model.read();
  pending[0].resolve("actual");
  await preparation;
  expect(model.phase).toBe("updating");
  model.preparationFailed();
  expect(model.phase).toBe("unconfirmed");
});
it("结束后唯一成功观察接纳原快照和发起token并恢复", async () => {
  const { observations: model, pending, accepted, changeToken } = setup();
  model.beginReload();
  model.postStarted();
  model.postEnded();
  const read = model.read();
  changeToken();
  pending[0].resolve("same-version-active-and-error");
  expect(await read).toBe("same-version-active-and-error");
  expect(accepted).toEqual([["same-version-active-and-error", 1]]);
  expect(model.phase).toBe("idle");
});
it("较旧成功观察不覆盖已经接纳的新快照，但调用者仍得到自己的事实", async () => {
  const { observations: model, pending, accepted } = setup();
  const old = model.read(),
    fresh = model.read();
  pending[1].resolve("fresh");
  await fresh;
  pending[0].resolve("old");
  expect(await old).toBe("old");
  expect(accepted).toEqual([["fresh", 1]]);
});
it("较新失败不妨碍较旧成功快照接纳", async () => {
  const { observations: model, pending, accepted } = setup();
  const old = model.read(),
    fresh = model.read();
  pending[1].reject(new Error("lost"));
  await expect(fresh).rejects.toThrow("lost");
  pending[0].resolve("old-success");
  await old;
  expect(accepted).toEqual([["old-success", 1]]);
});
it("合格观察已确认后晚到失败不重新制造责任", async () => {
  const { observations: model, pending } = setup();
  model.beginReload();
  model.postStarted();
  model.postEnded();
  const one = model.read(),
    two = model.read();
  pending[1].resolve("confirmed");
  await two;
  pending[0].reject(new Error("lost"));
  await expect(one).rejects.toThrow("lost");
  model.reloadFailed();
  expect(model.phase).toBe("idle");
});
it("观察失败保留未核实阶段且不接纳部分数据", async () => {
  const { observations: model, pending, accepted } = setup();
  model.beginReload();
  model.postStarted();
  model.postEnded();
  model.reloadFailed();
  const read = model.read();
  pending[0].reject(new Error("offline"));
  await expect(read).rejects.toThrow("offline");
  expect(accepted).toEqual([]);
  expect(model.phase).toBe("unconfirmed");
});
it("settle结束全部已发观察后才返回失败，不追加读取", async () => {
  const { observations: model, pending } = setup();
  const one = model.read(),
    two = model.read();
  const settled = model.settle();
  let ended = false;
  void settled.catch(() => {
    ended = true;
  });
  pending[0].reject(new Error("offline"));
  await expect(one).rejects.toThrow();
  expect(ended).toBe(false);
  expect(pending).toHaveLength(2);
  pending[1].resolve("actual");
  await two;
  await expect(settled).rejects.toThrow("offline");
});
