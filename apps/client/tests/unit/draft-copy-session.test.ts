import { expect, it, vi } from "vitest";
import { DraftSession } from "../../src/web/session";
import { creation, record } from "../helpers/draft-conflicts";
import {
  cloneClientJson,
  parseClientJson,
  stringifyJson,
} from "../../src/shared/json";
import { HttpError } from "../../src/web/api";
import { prepareCopyTarget } from "../../src/web/draft-copy-target";
import { DraftCopyOperation } from "../../src/web/draft-copy";
import type {
  Draft,
  DraftContent,
  DraftCreationResult,
} from "../../src/server/models";
import type { DraftObservation } from "../../src/web/draft-conflicts";
const local: DraftContent = {
  text: "{",
  pending: { "": { kind: "json", text: "{" } },
};
async function m1RejectReentry(
  session: DraftSession,
  transport: {
    create: (id: string, input: DraftContent) => Promise<DraftCreationResult>;
    read: (id: string) => Promise<DraftCreationResult>;
  },
  previous = session.copyDecision!,
) {
  const current = session.copyDecision!,
    recovery = session.copyRecovery!;
  expect(current.requestRunning).toBe(true);
  expect(current.canRetry).toBe(false);
  expect(current.canAbandon).toBe(false);
  const rejected = [previous, current].map((view) => {
    expect(() => session.abandonCopy(view)).toThrow();
    return expect(session.retryCopy(view, () => () => {})).rejects.toThrow();
  });
  rejected.push(
    expect(session.retryCopyTransfer(() => () => {})).rejects.toThrow(),
  );
  expect(() => session.beginCopy(transport, "extra")).toThrow();
  expect(() => session.beginNewCopy(transport, "extra")).toThrow();
  expect(() =>
    session.beginRecoveryCopy(recovery, transport, "extra"),
  ).toThrow();
  expect(() => session.discardCopyRecovery(recovery)).toThrow();
  await Promise.all(rejected);
}
it.each(["editable", "deleted", "exported", "unavailable", "invalid"] as const)(
  "M1 转交来源 GET 在途 %s 观察使固定资格失效并保持零共同提交",
  async (kind) => {
    const input = fullCopyInput(),
      source = deferredValue<Draft>(),
      entered = deferredValue<void>();
    const session = new DraftSession(
      record(1, "base"),
      {
        save: async () => record(2, "remote"),
        read: () => {
          entered.resolve();
          return source.promise;
        },
      },
      () => {},
    );
    session.edit(input);
    session.observe(record(2, "remote"));
    const result = copyOutcome("m1-first", input, "editable"),
      transport = {
        create: async () => cloneClientJson(result),
        read: async () => cloneClientJson(result),
      };
    const op = session.beginCopy(transport, "m1-first");
    await op.advance();
    const recovery = session.copyRecovery!,
      fact = op.result;
    let commits = 0,
      preparations = 0;
    session.bindWholeView(() => {
      preparations++;
      return () => {
        commits++;
      };
    });
    const run = session
      .retryCopy(session.copyDecision!, () => {
        preparations++;
        return () => {
          commits++;
        };
      })
      .catch((error) => error);
    await entered.promise;
    if (kind === "deleted") session.observeTarget({ kind });
    else if (kind === "unavailable")
      session.observeTarget({ kind, message: "source lost" });
    else
      session.observe({
        ...record(3, "latest"),
        ...(kind === "exported"
          ? { exportedRequestId: "42" }
          : kind === "invalid"
            ? { id: "foreign" }
            : {}),
      });
    // 返回同一个最新完整版本也不能越过当次固定观察变化。
    source.resolve(
      kind === "editable"
        ? record(3, "latest")
        : kind === "exported"
          ? { ...record(3, "latest"), exportedRequestId: "42" }
          : record(2, "remote"),
    );
    expect(await run).toBeInstanceOf(Error);
    expect([commits, preparations]).toEqual([0, 0]);
    expect(session.copyDecision!.requestRunning).toBe(false);
    expect(session.copyPendingDecision).toBe(true);
    expect(recovery.status).toBe("pending");
    expect(op.result).toEqual(fact);
    expect(stringifyJson(session.content)).toBe(stringifyJson(input));
  },
);
it.each(["export", "delete", "append", "write"] as const)(
  "M1 在途拒绝取消后 %s 及独立输入责任保持，失败只释放所有权",
  async (responsibility) => {
    const { session, transport, create } = setup(),
      input = fullCopyInput();
    session.edit(input);
    const op = session.beginCopy(transport, "m1-first");
    await op.advance();
    const independent = await independentRecovery(
        session,
        "independent",
        input,
        true,
      ),
      recovery = session.copyRecovery!,
      fact = op.result;
    const owner = session as any,
      reentries: Promise<void>[] = [];
    let commits = 0,
      original: unknown;
    await expect(
      session.retryCopy(session.copyDecision!, () => ({
        validate: () => {
          if (responsibility === "export") {
            session.exportState = "unknown";
            owner.exportSnapshot = {
              revision: 1,
              content: cloneClientJson(input),
            };
          }
          if (responsibility === "delete") session.deletionState = "unknown";
          if (responsibility === "append") session.appendLocked = true;
          if (responsibility === "write")
            owner.pendingWrite = {
              revision: 2,
              content: cloneClientJson(input),
              version: session.version,
              baseline: record(1, "base").content,
              baselineDraft: record(1, "base"),
              requestEnded: false,
              generation: 0,
              reason: "save",
            };
          original = {
            export: session.exportState,
            snapshot: owner.exportSnapshot,
            delete: session.deletionState,
            append: session.appendLocked,
            write: owner.pendingWrite,
          };
          reentries.push(m1RejectReentry(session, transport));
          expect(() =>
            session.discardCopyRecovery(independent.recovery),
          ).toThrow();
        },
        commit: () => {
          commits++;
        },
      })),
    ).rejects.toThrow();
    await Promise.all(reentries);
    expect({
      export: session.exportState,
      snapshot: owner.exportSnapshot,
      delete: session.deletionState,
      append: session.appendLocked,
      write: owner.pendingWrite,
    }).toEqual(original);
    expect(session.copyDecision!.requestRunning).toBe(false);
    expect(session.copyPendingDecision).toBe(true);
    expect(recovery.status).toBe("pending");
    expect(independent.recovery.status).toBe("pending");
    expect(independent.op.abandoned).toBe(false);
    expect(op.result).toEqual(fact);
    expect(stringifyJson(session.content)).toBe(stringifyJson(input));
    expect(commits).toBe(0);
    expect(create).toHaveBeenCalledTimes(1);
  },
);
it.each(["create", "target-get", "source-transfer"] as const)(
  "M1 %s 请求挂起时全部外部入口先拒绝，原执行随后成功",
  async (stage) => {
    const input = fullCopyInput(),
      created = copyOutcome("m1-first", input, "editable"),
      result = deferredValue<DraftCreationResult>(),
      source = deferredValue<Draft>(),
      entered = deferredValue<void>();
    const sourceRead = vi.fn(() => {
      entered.resolve();
      return stage === "source-transfer"
        ? source.promise
        : Promise.resolve(record(2, "remote"));
    });
    const session = new DraftSession(
      record(1, "base"),
      { save: async () => record(2, "remote"), read: sourceRead },
      () => {},
    );
    session.edit(input);
    session.observe(record(2, "remote"));
    let active = false;
    const create = vi.fn(async () => {
      if (active && stage === "create") {
        entered.resolve();
        return result.promise;
      }
      return cloneClientJson(created);
    });
    const targetRead = vi.fn(async () => {
      if (stage === "target-get") {
        entered.resolve();
        return result.promise;
      }
      return cloneClientJson(created);
    });
    const transport = { create, read: targetRead },
      op = session.beginCopy(transport, "m1-first");
    if (stage !== "create") await op.advance();
    const independent = await independentRecovery(session, "independent", {
      ...input,
      pending: { "": { kind: "json", text: "[" } },
    });
    const before = session.copyDecision!,
      map = new Map<string, DraftSession>();
    let sourceCommits = 0,
      targetCommits = 0,
      navCommits = 0;
    session.bindWholeView(() => () => {
      sourceCommits++;
    });
    active = true;
    const run = session.retryCopy(before, (actual) => {
      const prepared = prepareCopyTarget(
        map,
        actual,
        (draft) =>
          new DraftSession(
            draft,
            { save: async () => draft, read: async () => draft },
            () => {},
          ),
      );
      return {
        validate: prepared.validate,
        commit: () => {
          prepared.commit();
          targetCommits++;
          navCommits++;
        },
      };
    });
    await entered.promise;
    try {
      await m1RejectReentry(session, transport, before);
      expect(() => session.discardCopyRecovery(independent.recovery)).toThrow();
      expect(independent.recovery.status).toBe("pending");
      expect(independent.op.abandoned).toBe(false);
      expect([sourceCommits, targetCommits, navCommits, map.size]).toEqual([
        0, 0, 0, 0,
      ]);
      expect(stringifyJson(session.content)).toBe(stringifyJson(input));
      expect(create).toHaveBeenCalledTimes(1);
      expect(targetRead).toHaveBeenCalledTimes(stage === "create" ? 0 : 1);
      expect(sourceRead).toHaveBeenCalledTimes(
        stage === "source-transfer" ? 1 : 0,
      );
    } finally {
      result.resolve(cloneClientJson(created));
      source.resolve(record(2, "remote"));
      await run;
    }
    expect(await run).toBe("transferred");
    expect([sourceCommits, targetCommits, navCommits, map.size]).toEqual([
      1, 1, 1, 1,
    ]);
    expect(op.phase).toBe("transferred");
    expect(session.copyPendingDecision).toBe(false);
    expect(session.pendingCopyRecoveries).toEqual([independent.recovery]);
  },
);
it.each([
  "new",
  "not_created",
  "unknown-not-created",
  "editable",
  "terminal",
] as const)(
  "M1 %s 全部 changed 通知及请求衔接持续忙碌，不自锁合法下一阶段",
  async (initial) => {
    const input = fullCopyInput();
    let observing = false,
      session!: DraftSession,
      points = 0,
      quietOperationPoints = 0;
    const reentries: Promise<void>[] = [];
    const create = vi.fn(
      async (
        id: string,
        content: DraftContent,
      ): Promise<DraftCreationResult> => {
        if (!observing && initial === "unknown-not-created")
          throw Error("lost");
        if (!observing && initial === "not_created")
          return { kind: "not_created", attemptId: id };
        return copyOutcome(
          id,
          content,
          !observing && initial === "terminal" ? "deleted" : "editable",
        );
      },
    );
    const targetRead = vi.fn(
      async (id: string): Promise<DraftCreationResult> =>
        initial === "unknown-not-created"
          ? { kind: "not_created", attemptId: id }
          : copyOutcome(id, input, "editable"),
    );
    const transport = { create, read: targetRead };
    session = new DraftSession(
      record(1, "base"),
      {
        save: async () => record(2, "remote"),
        read: async () => record(2, "remote"),
      },
      () => {
        const view = session.copyDecision;
        if (!observing || !view || !view.requestRunning) return;
        points++;
        if (!session.copyOperation!.requestRunning) quietOperationPoints++;
        reentries.push(m1RejectReentry(session, transport, view));
      },
    );
    session.edit(input);
    session.observe(record(2, "remote"));
    const first = session.beginCopy(transport, "m1-first");
    if (initial !== "new") await first.advance();
    const oldFact = first.result,
      recovery = session.copyRecovery!;
    observing = true;
    expect(
      await session.retryCopy(
        session.copyDecision!,
        () => () => {},
        "m1-second",
      ),
    ).toBe("transferred");
    observing = false;
    await Promise.all(reentries);
    expect(points).toBeGreaterThan(2);
    expect(quietOperationPoints).toBeGreaterThan(1);
    expect(session.copyPendingDecision).toBe(false);
    expect(recovery.status).toBe("transferred");
    expect(stringifyJson(recovery.input)).toBe(stringifyJson(input));
    expect(create).toHaveBeenCalledTimes(
      initial === "new" || initial === "editable" ? 1 : 2,
    );
    expect(targetRead).toHaveBeenCalledTimes(
      initial === "editable" || initial === "unknown-not-created" ? 1 : 0,
    );
    if (initial === "terminal") {
      expect(first.result).toEqual(oldFact);
      expect(first.phase).toBe("created_untransferred");
      expect(first.abandoned).toBe(false);
      expect(recovery.attemptIds).toEqual(["m1-first", "m1-second"]);
    }
  },
);
it.each(["source", "target", "validate"] as const)(
  "M1 %s 回调中的所有取消和新入口拒绝后保留整个执行与原责任",
  async (point) => {
    const { session, transport, create } = setup(),
      input = fullCopyInput();
    session.edit(input);
    const op = session.beginCopy(transport, "m1-first");
    await op.advance();
    const recovery = session.copyRecovery!,
      fact = op.result,
      before = session.copyDecision!;
    const reentries: Promise<void>[] = [];
    let sourceCommits = 0,
      targetCommits = 0,
      navCommits = 0;
    const check = () => {
      reentries.push(m1RejectReentry(session, transport, before));
    };
    session.bindWholeView(() => {
      if (point === "source") check();
      return () => {
        sourceCommits++;
      };
    });
    await expect(
      session.retryCopy(before, () => {
        if (point === "target") check();
        return {
          validate: () => {
            if (point === "validate") check();
            throw Error("external validation failed");
          },
          commit: () => {
            targetCommits++;
            navCommits++;
          },
        };
      }),
    ).rejects.toThrow();
    await Promise.all(reentries);
    expect([sourceCommits, targetCommits, navCommits]).toEqual([0, 0, 0]);
    expect(session.copyDecision!.requestRunning).toBe(false);
    expect(session.copyDecision!.canAbandon).toBe(true);
    expect(session.copyPendingDecision).toBe(true);
    expect(recovery.status).toBe("pending");
    expect(op.result).toEqual(fact);
    expect(stringifyJson(session.content)).toBe(stringifyJson(input));
    expect(create).toHaveBeenCalledTimes(1);
    session.bindWholeView(() => () => {
      sourceCommits++;
    });
    expect(
      await session.retryCopy(session.copyDecision!, () => () => {
        targetCommits++;
        navCommits++;
      }),
    ).toBe("transferred");
    expect([sourceCommits, targetCommits, navCommits]).toEqual([1, 1, 1]);
  },
);
const m1SourceCases = (["unavailable", "invalid"] as const).flatMap((source) =>
  (
    [
      "new",
      "not_created",
      "unknown",
      "invalid",
      "editable",
      "deleted",
      "exported",
    ] as const
  ).map((phase) => [source, phase] as const),
);
type M1Phase = (typeof m1SourceCases)[number][1];
function deferredValue<T>() {
  let resolve!: (value: T) => void, reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}
async function m1SourceSetup(
  source: "unavailable" | "invalid",
  phase: M1Phase,
) {
  const input = fullCopyInput(),
    sourceResult = deferredValue<DraftObservation>(),
    entered = deferredValue<void>();
  const sourceRead = vi.fn(() => {
    entered.resolve();
    return sourceResult.promise;
  });
  const session = new DraftSession(
    record(1, "base"),
    {
      save: async () => record(2, "remote"),
      read: async () => record(2, "remote"),
      readObservation: sourceRead,
    },
    () => {},
  );
  session.edit(input);
  session.observe(record(2, "remote"));
  const create = vi.fn(
    async (id: string, content: DraftContent): Promise<DraftCreationResult> => {
      if (phase === "unknown") throw Error("lost");
      if (phase === "invalid")
        return { kind: "not_created", attemptId: "wrong" };
      if (phase === "not_created")
        return { kind: "not_created", attemptId: id };
      return copyOutcome(id, content, phase === "new" ? "editable" : phase);
    },
  );
  const targetRead = vi.fn(async (id: string): Promise<DraftCreationResult> =>
    phase === "not_created" || phase === "unknown" || phase === "invalid"
      ? { kind: "not_created", attemptId: id }
      : copyOutcome(id, input, phase === "new" ? "editable" : phase),
  );
  const transport = { create, read: targetRead },
    op = session.beginCopy(transport, "m1-first");
  if (phase !== "new") await op.advance();
  if (source === "unavailable")
    session.observeTarget({ kind: source, message: "offline" });
  else session.observe({ ...record(2, "remote"), id: "foreign-source" });
  let commits = 0;
  const before = session.copyDecision!,
    run = session
      .retryCopy(
        before,
        () => () => {
          commits++;
        },
        "must-not-create",
      )
      .catch((error) => error);
  await entered.promise;
  return {
    session,
    input,
    before,
    run,
    sourceResult,
    sourceRead,
    create,
    targetRead,
    transport,
    op,
    commits: () => commits,
  };
}
it.each(m1SourceCases)(
  "M1 来源 %s 恢复期间 %s 阶段统一忙碌且内部核实连续推进",
  async (source, phase) => {
    const f = await m1SourceSetup(source, phase),
      fact = f.op.result,
      recovery = f.session.copyRecovery!;
    try {
      const current = f.session.copyDecision!;
      expect(current.requestRunning).toBe(true);
      expect(current.canRetry).toBe(false);
      expect(current.canAbandon).toBe(false);
      for (const view of [f.before, current]) {
        await expect(
          f.session.retryCopy(view, () => () => {}),
        ).rejects.toThrow();
        expect(() => f.session.abandonCopy(view)).toThrow();
      }
      expect(() => f.session.beginCopy(f.transport, "extra")).toThrow();
      expect(() => f.session.beginNewCopy(f.transport, "extra")).toThrow();
      expect(() =>
        f.session.beginRecoveryCopy(recovery, f.transport, "extra"),
      ).toThrow();
      expect(() => f.session.discardCopyRecovery(recovery)).toThrow();
      await expect(
        f.session.retryCopyTransfer(() => () => {}),
      ).rejects.toThrow();
      expect(f.sourceRead).toHaveBeenCalledTimes(1);
      expect(f.targetRead).toHaveBeenCalledTimes(
        ["new", "deleted", "exported"].includes(phase) ? 0 : 1,
      );
      expect(f.create).toHaveBeenCalledTimes(phase === "new" ? 0 : 1);
      expect(recovery.attemptIds).toEqual(["m1-first"]);
      expect(f.op.requestRunning).toBe(false);
      expect(f.commits()).toBe(0);
      expect(stringifyJson(f.session.content)).toBe(stringifyJson(f.input));
    } finally {
      f.sourceResult.resolve({ kind: "available", draft: record(2, "remote") });
      await f.run;
    }
    expect(await f.run).toBe("pending");
    expect(f.session.copyDecision!.requestRunning).toBe(false);
    expect(f.session.copyPendingDecision).toBe(true);
    expect(recovery.status).toBe("pending");
    expect(f.op.result).toEqual(fact);
  },
);
it.each(["editable", "deleted", "exported", "unavailable", "invalid"] as const)(
  "M1 来源 GET 在途普通 %s 观察登记事实而不释放所有权",
  async (kind) => {
    const f = await m1SourceSetup("unavailable", "deleted"),
      recovery = f.session.copyRecovery!,
      fact = f.op.result;
    try {
      if (kind === "deleted") f.session.observeTarget({ kind });
      else if (kind === "unavailable")
        f.session.observeTarget({ kind, message: "again offline" });
      else
        f.session.observe({
          ...record(3, "later remote"),
          ...(kind === "invalid"
            ? { id: "foreign-source" }
            : kind === "exported"
              ? { exportedRequestId: "42" }
              : {}),
        });
      expect(f.session.copyDecision!.requestRunning).toBe(true);
      expect(f.session.copyDecision!.canRetry).toBe(false);
      expect(f.session.copyDecision!.canAbandon).toBe(false);
      expect(() => f.session.abandonCopy(f.session.copyDecision!)).toThrow();
      await expect(
        f.session.retryCopy(f.session.copyDecision!, () => () => {}),
      ).rejects.toThrow();
      expect(f.session.observationEpoch).toBeGreaterThan(
        f.before.observationEpoch,
      );
      expect(stringifyJson(f.session.content)).toBe(stringifyJson(f.input));
    } finally {
      f.sourceResult.resolve({ kind: "available", draft: record(2, "remote") });
      await f.run;
    }
    expect(await f.run).toBeInstanceOf(Error);
    expect(f.session.copyDecision!.requestRunning).toBe(false);
    expect(f.session.copyPendingDecision).toBe(true);
    expect(f.session.targetObservation.kind).toBe(
      kind === "editable" || kind === "exported" || kind === "invalid"
        ? "available"
        : kind,
    );
    expect(recovery.status).toBe("pending");
    expect(f.op.result).toEqual(fact);
    expect(f.commits()).toBe(0);
    expect(f.sourceRead).toHaveBeenCalledTimes(1);
    expect(f.create).toHaveBeenCalledTimes(1);
  },
);
it.each(["failed-read", "generation", "input"] as const)(
  "M1 来源恢复 %s 后仅释放当次执行，不清责任或提交",
  async (change) => {
    const f = await m1SourceSetup("invalid", "exported"),
      recovery = f.session.copyRecovery!,
      fact = f.op.result;
    if (change === "generation")
      (f.session as any).recoveryRecords[0].generation++;
    if (change === "input") f.session.content = { text: "callback input" };
    if (change === "failed-read")
      f.sourceResult.reject(Error("source offline"));
    else
      f.sourceResult.resolve({ kind: "available", draft: record(2, "remote") });
    expect(await f.run).toEqual(
      change === "failed-read" ? "pending" : expect.any(Error),
    );
    expect(f.session.copyDecision!.requestRunning).toBe(false);
    expect(f.session.copyPendingDecision).toBe(true);
    expect(recovery.status).toBe("pending");
    expect(stringifyJson(recovery.input)).toBe(stringifyJson(f.input));
    expect(f.op.result).toEqual(fact);
    expect(f.commits()).toBe(0);
    if (change === "failed-read") {
      expect(f.session.targetObservation.kind).toBe("unavailable");
      expect(f.session.copyDecision!.canAbandon).toBe(false);
    }
  },
);
it.each(["busy", "repeat", "abandon"] as const)(
  "M1 来源 GET 在途保持整个另存执行所有权：%s",
  async (branch) => {
    const input = fullCopyInput(),
      expected = stringifyJson(input);
    let resolve!: (draft: Draft) => void;
    const sourceResult = new Promise<Draft>((done) => {
      resolve = done;
    });
    const sourceRead = vi.fn(() => sourceResult);
    const session = new DraftSession(
      record(1, "base"),
      {
        save: async () => record(2, "remote"),
        read: sourceRead,
      },
      () => {},
    );
    session.edit(input);
    session.observe(record(2, "remote"));
    const create = vi.fn(async (id: string, content: DraftContent) =>
      copyOutcome(id, content, "deleted"),
    );
    const targetRead = vi.fn();
    const op = session.beginCopy({ create, read: targetRead }, "m1-first");
    await op.advance();
    const fact = op.result,
      recovery = session.copyRecovery!;
    session.observeTarget({ kind: "unavailable", message: "offline" });
    const before = session.copyDecision!;
    let commits = 0;
    const first = session
      .retryCopy(before, () => () => {
        commits++;
      })
      .catch((error) => error);
    const other: Promise<unknown>[] = [];
    try {
      expect(sourceRead).toHaveBeenCalledTimes(1);
      if (branch === "busy") {
        expect(session.copyDecision!.requestRunning).toBe(true);
        expect(session.copyDecision!.canRetry).toBe(false);
        expect(session.copyDecision!.canAbandon).toBe(false);
      } else if (branch === "repeat") {
        // 第一项调用是否拒绝属于防止重复请求的公开行为，不能只测快照。
        const second = session
          .retryCopy(session.copyDecision!, () => () => {
            commits++;
          })
          .catch((error) => error);
        other.push(second);
        expect(sourceRead).toHaveBeenCalledTimes(1);
      } else {
        session.observe(record(2, "remote"));
        expect(() => session.abandonCopy(session.copyDecision!)).toThrow();
        expect(session.copyDecision!.requestRunning).toBe(true);
        expect(recovery.status).toBe("pending");
      }
      expect(stringifyJson(session.content)).toBe(expected);
      expect(expected).toContain("1.0000000000000001");
      expect(expected).toContain("1e-999");
      expect(session.copyPendingDecision).toBe(true);
      expect(op.result).toEqual(fact);
      expect(create).toHaveBeenCalledTimes(1);
      expect(targetRead).not.toHaveBeenCalled();
      expect(commits).toBe(0);
    } finally {
      resolve(record(2, "remote"));
      await first;
      await Promise.all(other);
    }
  },
);
const completeCopyCases = (
  [
    "text",
    "pending",
    "actionVariants",
    "parameterVariants",
    "objectVariants",
    "automaticPreviews",
    "all",
  ] as const
).flatMap((part) =>
  (["not_created", "editable", "deleted", "exported"] as const).map(
    (kind) => [part, kind] as const,
  ),
);
function copyInputPart(
  part: (typeof completeCopyCases)[number][0],
): DraftContent {
  const full = fullCopyInput();
  return part === "all"
    ? full
    : part === "text"
      ? { text: full.text }
      : { text: full.text, [part]: full[part] };
}
it.each(completeCopyCases)(
  "放弃保留 %s 的精确输入，目标分区 %s",
  async (part, kind) => {
    const { session } = setup(),
      input = copyInputPart(part),
      expected = stringifyJson(input);
    session.edit(input);
    const create = vi.fn(
      async (id: string, actual: DraftContent): Promise<DraftCreationResult> =>
        kind === "not_created"
          ? { kind, attemptId: id }
          : copyOutcome(id, actual, kind),
    );
    const op = session.beginCopy(
      {
        create,
        read: async () => {
          throw Error("unused");
        },
      },
      "modal",
    );
    await op.advance();
    const fact = op.result,
      decision = session.copyDecision!,
      recovery = session.copyRecovery!;
    decision.input.text = "public mutation";
    recovery.input.text = "public mutation";
    session.abandonCopy(decision);
    expect(stringifyJson(session.content)).toBe(expected);
    expect(stringifyJson(op.input)).toBe(expected);
    expect(op.result).toEqual(fact);
    expect(recovery.status).toBe("returned");
    expect(session.pendingCopyRecoveries).toHaveLength(0);
    expect(create).toHaveBeenCalledTimes(1);
    expect(session.canSave).toBe(false);
    if (part === "all") {
      expect(expected).toContain("1.0000000000000001");
      expect(expected).toContain("1e-999");
    }
  },
);
it.each(completeCopyCases)(
  "重试保留 %s 的精确输入和责任关联，目标分区 %s",
  async (part, kind) => {
    const { session } = setup(),
      input = copyInputPart(part),
      expected = stringifyJson(input);
    session.edit(input);
    let created: DraftCreationResult;
    let count = 0;
    const create = vi.fn(
      async (
        id: string,
        actual: DraftContent,
      ): Promise<DraftCreationResult> => {
        count++;
        created =
          count === 1 && kind === "not_created"
            ? { kind: "not_created", attemptId: id }
            : copyOutcome(
                id,
                actual,
                count === 1 && kind !== "not_created" ? kind : "editable",
              );
        return cloneClientJson(created);
      },
    );
    const read = vi.fn(async () => cloneClientJson(created));
    const op = session.beginCopy({ create, read }, "first");
    await op.advance();
    const recovery = session.copyRecovery!,
      fact = op.result;
    await session.retryCopy(session.copyDecision!, () => () => {}, "second");
    expect(stringifyJson(recovery.input)).toBe(expected);
    expect(create.mock.calls.map((call) => stringifyJson(call[1]))).toEqual(
      kind === "editable" ? [expected] : [expected, expected],
    );
    expect(create.mock.calls.map((call) => call[0])).toEqual(
      kind === "editable"
        ? ["first"]
        : kind === "not_created"
          ? ["first", "first"]
          : ["first", "second"],
    );
    expect(recovery.status).toBe("transferred");
    expect(session.copyPendingDecision).toBe(false);
    if (kind === "deleted" || kind === "exported") {
      expect(op.result).toEqual(fact);
      expect(op.abandoned).toBe(false);
    }
    if (part === "all") {
      expect(expected).toContain("1.0000000000000001");
      expect(expected).toContain("1e-999");
    }
  },
);
it("统一重试读取较新 editable 当前目标，只做本地转交而不创建", async () => {
  const { session } = setup();
  let current = copyOutcome("modal", local, "editable");
  const create = vi.fn(async () => cloneClientJson(current)),
    read = vi.fn(async () => cloneClientJson(current));
  const op = session.beginCopy({ create, read }, "modal");
  await op.advance();
  const fact = op.result;
  await expect(
    session.retryCopy(session.copyDecision!, () => {
      throw Error("navigation prepare");
    }),
  ).rejects.toThrow();
  if (current.kind !== "created" || current.target.kind !== "editable")
    throw Error("fixture");
  current.target.draft.revision = 2;
  current.target.draft.content = record(2, "changed target").content;
  const map = new Map<string, DraftSession>();
  await session.retryCopy(session.copyDecision!, (actual) =>
    prepareCopyTarget(
      map,
      actual,
      (d) =>
        new DraftSession(
          d,
          { save: async () => d, read: async () => d },
          () => {},
        ),
    ),
  );
  expect(map.get("modal-target")!.content).toEqual(
    current.target.draft.content,
  );
  expect(map.get("modal-target")!.revision).toBe(2);
  expect(create).toHaveBeenCalledTimes(1);
  expect(read).toHaveBeenCalledTimes(2);
  expect(op.result).toEqual(current);
  expect(current.creation).toEqual(fact!.kind === "created" && fact!.creation);
  expect(op.input).toEqual(local);
});
it("已可靠创建的当前目标读取失败保留事实和责任，放弃暂不可用", async () => {
  const { session } = setup(),
    fact = copyOutcome("modal", local, "editable");
  let failed = true;
  const create = vi.fn(async () => fact),
    read = vi.fn(async () => {
      if (failed) throw Error("offline");
      return fact;
    });
  const op = session.beginCopy({ create, read }, "modal");
  await op.advance();
  let commits = 0;
  expect(
    await session.retryCopy(session.copyDecision!, () => () => {
      commits++;
    }),
  ).toBe("pending");
  expect(commits).toBe(0);
  expect(op.phase).toBe("unknown");
  expect(op.result).toEqual(fact);
  expect(session.content).toEqual(local);
  expect(session.copyPendingDecision).toBe(true);
  expect(session.pendingCopyRecoveries).toHaveLength(1);
  expect(session.copyDecision!.canAbandon).toBe(false);
  expect(() => session.abandonCopy(session.copyDecision!)).toThrow();
  failed = false;
  await session.retryCopy(session.copyDecision!, () => () => {
    commits++;
  });
  expect(commits).toBe(1);
  expect(create).toHaveBeenCalledTimes(1);
  expect(read).toHaveBeenCalledTimes(2);
});
it("在途请求禁止重复重试和放弃，结束后只有新资格快照可用", async () => {
  const { session } = setup();
  let resolve!: (result: DraftCreationResult) => void;
  const create = vi.fn(
      () =>
        new Promise<DraftCreationResult>((r) => {
          resolve = r;
        }),
    ),
    read = vi.fn();
  const op = session.beginCopy({ create, read }, "modal"),
    request = op.advance(),
    inFlight = session.copyDecision!;
  expect(inFlight.requestRunning).toBe(true);
  expect(inFlight.canRetry).toBe(false);
  expect(inFlight.canAbandon).toBe(false);
  await expect(session.retryCopy(inFlight, () => () => {})).rejects.toThrow();
  expect(() => session.abandonCopy(inFlight)).toThrow();
  resolve({ kind: "not_created", attemptId: "modal" });
  await request;
  expect(() => session.abandonCopy(inFlight)).toThrow();
  session.abandonCopy(session.copyDecision!);
  expect(create).toHaveBeenCalledTimes(1);
  expect(read).not.toHaveBeenCalled();
  expect(session.content).toEqual(local);
});
it("旧 preparedEdit、按钮、核实及导航回调在放弃后不能复活选择", async () => {
  const { session } = setup(),
    prepared = session.prepareEdit({ text: "B" });
  const create = vi.fn(async (id: string): Promise<DraftCreationResult> => ({
      kind: "not_created",
      attemptId: id,
    })),
    read = vi.fn();
  const op = session.beginCopy({ create, read }, "modal");
  await op.advance();
  const decision = session.copyDecision!,
    recovery = session.copyRecovery!,
    version = session.version;
  session.abandonCopy(decision);
  expect(() => session.commitEdit(prepared)).toThrow();
  let commits = 0;
  await expect(
    session.retryCopy(decision, () => () => {
      commits++;
    }),
  ).rejects.toThrow();
  expect(() => session.abandonCopy(decision)).toThrow();
  expect(() =>
    session.beginRecoveryCopy(
      recovery,
      copyTransport("editable"),
      "old-navigation",
    ),
  ).toThrow();
  await op.verify();
  await op.advance();
  expect(commits).toBe(0);
  expect(create).toHaveBeenCalledTimes(1);
  expect(read).not.toHaveBeenCalled();
  expect(session.version).toBe(version);
  expect(session.content).toEqual(local);
  expect(session.copyPendingDecision).toBe(false);
  session.edit({ text: "B" });
  expect(session.content).toEqual({ text: "B" });
});
it("选择前的旧 timer 在交回输入后不能执行普通保存", async () => {
  vi.useFakeTimers();
  const timer = vi.spyOn(globalThis, "setTimeout");
  try {
    const save = vi.fn(async () => record(1, "base")),
      session = new DraftSession(
        record(1, "base"),
        { save, read: async () => record(1, "base") },
        () => {},
      );
    session.edit(local);
    session.schedule();
    const oldTimer = timer.mock.calls.at(-1)![0] as () => void;
    const op = session.beginCopy(
      {
        create: async () => ({ kind: "not_created", attemptId: "modal" }),
        read: async () => ({ kind: "not_created", attemptId: "modal" }),
      },
      "modal",
    );
    await op.advance();
    session.abandonCopy(session.copyDecision!);
    expect(session.canSave).toBe(true);
    oldTimer();
    await Promise.resolve();
    expect(save).not.toHaveBeenCalled();
    expect(session.content).toEqual(local);
  } finally {
    timer.mockRestore();
    vi.useRealTimers();
  }
});
it("普通观察只记录最新事实并使旧决定快照失效，源完整输入不被接纳覆盖", async () => {
  const { session } = setup(),
    op = session.beginCopy(copyTransport("deleted"), "modal");
  await op.advance();
  const old = session.copyDecision!,
    version = session.version;
  session.observe(record(3, "remote after choice"));
  expect(session.lastReliableDraft.revision).toBe(3);
  expect(session.content).toEqual(local);
  expect(session.version).toBe(version);
  expect(session.canEdit).toBe(false);
  expect(() => session.abandonCopy(old)).toThrow();
  await expect(
    session.retryCopy(old, () => () => {}, "obsolete"),
  ).rejects.toThrow();
  session.abandonCopy(session.copyDecision!);
  expect(session.content).toEqual(local);
  expect(session.canSave).toBe(false);
});
it("代次变化的新尝试失败仍持有同一 A，旧终态重试按钮不能创建第三份", async () => {
  const { session } = setup();
  const create = vi.fn(async (id: string, input: DraftContent) => {
    if (id !== "first") throw Error("lost");
    return copyOutcome(id, input, "deleted");
  });
  const first = session.beginCopy(
    {
      create,
      read: async () => {
        throw Error("offline");
      },
    },
    "first",
  );
  await first.advance();
  const original = session.copyDecision!,
    fact = first.result,
    recovery = session.copyRecovery!;
  expect(await session.retryCopy(original, () => () => {}, "second")).toBe(
    "pending",
  );
  expect(session.copyOperation!.phase).toBe("unknown");
  expect(session.copyPendingDecision).toBe(true);
  expect(session.copyDecision!.canAbandon).toBe(false);
  expect(recovery.input).toEqual(local);
  expect(session.content).toEqual(local);
  await expect(
    session.retryCopy(original, () => () => {}, "third"),
  ).rejects.toThrow();
  expect(create.mock.calls.map((call) => call[0])).toEqual(["first", "second"]);
  expect(first.result).toEqual(fact);
  expect(first.abandoned).toBe(false);
  expect(recovery.status).toBe("pending");
});
it.each(["export", "delete", "append", "write"] as const)(
  "放弃所选安排不清除独立 %s 责任",
  async (responsibility) => {
    const { session } = setup(),
      op = session.beginCopy(copyTransport("deleted"), "modal");
    await op.advance();
    const owner = session as any;
    if (responsibility === "export") {
      session.exportState = "unknown";
      owner.exportSnapshot = { revision: 1, content: cloneClientJson(local) };
    }
    if (responsibility === "delete") session.deletionState = "unknown";
    if (responsibility === "append") session.appendLocked = true;
    if (responsibility === "write")
      owner.pendingWrite = {
        revision: 1,
        content: cloneClientJson(local),
        version: session.version,
        baseline: record(1, "base").content,
        baselineDraft: record(1, "base"),
        requestEnded: true,
        generation: 0,
        reason: "save",
      };
    const original = {
      exportState: session.exportState,
      exported: owner.exportSnapshot,
      deletionState: session.deletionState,
      append: session.appendLocked,
      pending: owner.pendingWrite,
    };
    session.abandonCopy(session.copyDecision!);
    expect({
      exportState: session.exportState,
      exported: owner.exportSnapshot,
      deletionState: session.deletionState,
      append: session.appendLocked,
      pending: owner.pendingWrite,
    }).toEqual(original);
    expect(session.copyRecovery!.status).toBe("returned");
    expect(session.content).toEqual(local);
    expect(session.canSave).toBe(false);
  },
);
it.each([false, true])(
  "正文相同的独立输入在所选放弃后保持 pending，unknown=%s",
  async (unknown) => {
    const { session } = setup(),
      op = session.beginCopy(copyTransport("deleted"), "modal");
    await op.advance();
    const separate: DraftContent = {
      text: local.text,
      pending: { "": { kind: "json", text: "[" } },
    };
    const { recovery: independent, op: other } = await independentRecovery(
        session,
        "independent",
        separate,
        unknown,
      ),
      fact = other.result;
    session.abandonCopy(session.copyDecision!);
    expect(session.pendingCopyRecoveries).toEqual([independent]);
    expect(independent.input).toEqual(separate);
    expect(other.result).toEqual(fact);
    expect(other.abandoned).toBe(false);
    expect(session.content).toEqual(local);
  },
);
it("决定快照的公开副本、伪造代次和他会话不能取得重试或放弃资格", async () => {
  const { session } = setup(),
    op = session.beginCopy(copyTransport("deleted"), "modal");
  await op.advance();
  const decision = session.copyDecision!;
  expect(Object.isFrozen(decision)).toBe(true);
  decision.input.text = "changed";
  const foreign = setup().session,
    other = foreign.beginCopy(copyTransport("deleted"), "foreign");
  await other.advance();
  for (const counterfeit of [
    { ...decision, generation: decision.generation + 1 },
    { ...decision },
    foreign.copyDecision!,
  ]) {
    expect(() => session.abandonCopy(counterfeit)).toThrow();
    await expect(
      session.retryCopy(counterfeit, () => () => {}, "fake"),
    ).rejects.toThrow();
  }
  expect(session.content).toEqual(local);
  expect(session.copyRecovery!.input).toEqual(local);
  session.abandonCopy(decision);
  expect(session.copyPendingDecision).toBe(false);
});
it.each(["deleted", "exported"] as const)(
  "目标 %s 核实已结束但待决定期间源输入仍固定",
  async (kind) => {
    const { session } = setup(),
      op = session.beginCopy(copyTransport(kind), "modal");
    await op.advance();
    expect(op.terminalTarget).toBe(true);
    expect(session.canEdit).toBe(false);
    expect(() => session.edit({ text: "later input" })).toThrow();
    expect(session.content).toEqual(local);
  },
);
it("可靠未创建的放弃把输入交回源并结束选中独立恢复责任", async () => {
  const { session } = setup();
  const op = session.beginCopy(
    {
      create: async () => ({ kind: "not_created", attemptId: "modal" }),
      read: async () => ({ kind: "not_created", attemptId: "modal" }),
    },
    "modal",
  );
  await op.advance();
  const recovery = session.copyRecovery!;
  session.abandonCopy();
  expect(session.content).toEqual(local);
  expect(session.pendingCopyRecoveries).toHaveLength(0);
  expect(recovery.status).toBe("returned");
  expect(session.canEdit).toBe(true);
  expect(session.canSave).toBe(false);
});
it("已创建可编辑目标的放弃保留目标并将完整输入交回源", async () => {
  const { session, transport, create } = setup(),
    op = session.beginCopy(transport, "modal");
  await op.advance();
  const fact = op.result;
  await expect(
    session.retryCopyTransfer(() => {
      throw Error("view");
    }),
  ).rejects.toThrow();
  expect(() => session.abandonCopy()).not.toThrow();
  expect(session.content).toEqual(local);
  expect(session.pendingCopyRecoveries).toHaveLength(0);
  expect(op.result).toEqual(fact);
  expect(create).toHaveBeenCalledTimes(1);
});
function setup() {
  let actual = record(2, "remote");
  let created: DraftCreationResult | undefined;
  const save = vi.fn(async () => actual),
    session = new DraftSession(
      record(1, "base"),
      { save, read: async () => actual },
      () => {},
    );
  session.edit(local);
  session.observe(actual);
  const create = vi.fn(
    async (
      attemptId: string,
      input: DraftContent,
    ): Promise<DraftCreationResult> => {
      const fact = creation(input);
      fact.attemptId = attemptId;
      created = {
        kind: "created",
        creation: fact,
        target: { kind: "editable", draft: cloneClientJson(fact.created) },
      };
      return cloneClientJson(created);
    },
  );
  return {
    session,
    save,
    create,
    transport: {
      create,
      read: async (id: string) => {
        if (
          !created ||
          created.kind !== "created" ||
          created.creation.attemptId !== id
        )
          throw Error("not observed");
        return cloneClientJson(created);
      },
    },
    setActual: (value: Draft) => (actual = value),
  };
}
it("另存固定完整 L 而不先保存原冲突输入", async () => {
  const { session, save, transport } = setup(),
    op = session.beginCopy(transport, "copy-1");
  expect(session.copyOperation).toBe(op);
  expect(session.canEdit).toBe(false);
  await op.advance();
  expect(op.input).toEqual(local);
  expect(save).not.toHaveBeenCalled();
});
it("目标准备失败不会提前清源输入或提交显示", async () => {
  const { session, transport, create } = setup(),
    op = session.beginCopy(transport, "copy-1");
  await op.advance();
  let keys = "old";
  session.bindWholeView(() => () => {
    keys = "new";
  });
  await expect(
    session.retryCopyTransfer(() => {
      throw Error("map dirty");
    }),
  ).rejects.toThrow();
  expect(session.content).toEqual(local);
  expect(keys).toBe("old");
  expect(op.phase).toBe("created_untransferred");
  await session.retryCopyTransfer(() => () => {});
  expect(session.content).toEqual(record(2, "remote").content);
  expect(keys).toBe("new");
  expect(op.phase).toBe("transferred");
  expect(create).toHaveBeenCalledTimes(1);
});
it("源显示准备失败只重试本地转交", async () => {
  const { session, transport, create } = setup(),
    op = session.beginCopy(transport, "copy-1");
  await op.advance();
  session.bindWholeView(() => {
    throw Error("view");
  });
  await expect(session.retryCopyTransfer(() => () => {})).rejects.toThrow();
  expect(session.content).toEqual(local);
  session.bindWholeView(() => () => {});
  await session.retryCopyTransfer(() => () => {});
  expect(create).toHaveBeenCalledTimes(1);
});
it("源可靠读取不可用时保留输入和创建事实", async () => {
  const { transport } = setup(),
    s = new DraftSession(
      record(1, "base"),
      {
        save: async () => record(1, "base"),
        read: async () => {
          throw Error("offline");
        },
      },
      () => {},
    );
  s.edit(local);
  const op = s.beginCopy(transport, "copy-1");
  await op.advance();
  await expect(s.retryCopyTransfer(() => () => {})).rejects.toThrow();
  expect(s.content).toEqual(local);
  expect(op.phase).toBe("created_untransferred");
});
it("目标已删除结束创建核实并保持待决定限制，明确新决定才继续", async () => {
  const { session } = setup();
  const fact = creation(local);
  const transport = {
    create: async (): Promise<DraftCreationResult> => ({
      kind: "created",
      creation: fact,
      target: { kind: "deleted" },
    }),
    read: async (): Promise<DraftCreationResult> => ({
      kind: "created",
      creation: fact,
      target: { kind: "deleted" },
    }),
  };
  const old = session.beginCopy(transport, "copy-1");
  await old.advance();
  expect(session.canEdit).toBe(false);
  const next = session.beginNewCopy(transport, "copy-2");
  expect(next.attemptId).toBe("copy-2");
  expect(next.input).toEqual(local);
  expect(old.phase).toBe("created_untransferred");
});
it("未知创建期间源终态不会清除独立核实责任", async () => {
  const { session } = setup(),
    fact = creation(local);
  (session as any).transport.readObservation = async () => ({
    kind: "deleted",
  });
  const op = session.beginCopy(
    {
      create: async () => {
        throw Error("lost");
      },
      read: async () => ({
        kind: "created",
        creation: fact,
        target: { kind: "editable", draft: fact.created },
      }),
    },
    "copy-1",
  );
  await op.advance();
  session.observeTarget({ kind: "deleted" });
  expect(op.phase).toBe("unknown");
  expect(session.content).toEqual(local);
  await op.verify();
  await session.retryCopyTransfer(() => () => {});
  expect(op.phase).toBe("transferred");
  expect(op.input).toEqual(local);
  expect(session.decision.kind).toBe("deleted");
  expect(session.saved).toBe(true);
});
it("源观察在准备期间改变会撤销共同提交", async () => {
  const { session, transport } = setup(),
    op = session.beginCopy(transport, "copy-1");
  await op.advance();
  let committed = false;
  await expect(
    session.retryCopyTransfer(() => {
      session.observeTarget({ kind: "unavailable", message: "offline" });
      return () => {
        committed = true;
      };
    }),
  ).rejects.toThrow();
  expect(session.content).toEqual(local);
  expect(op.phase).toBe("created_untransferred");
  expect(committed).toBe(false);
  await session.retryCopyTransfer(() => () => {});
  expect(op.phase).toBe("transferred");
});
it("源读取旧版本不能清除输入或完成转交", async () => {
  const { session, transport, setActual } = setup(),
    op = session.beginCopy(transport, "copy-1");
  await op.advance();
  setActual(record(1, "base"));
  await expect(session.retryCopyTransfer(() => () => {})).rejects.toThrow();
  expect(session.content).toEqual(local);
  expect(op.phase).toBe("created_untransferred");
});
it("源已导出转交仍先接纳实际完整只读记录", async () => {
  const { session, transport, setActual } = setup(),
    op = session.beginCopy(transport, "copy-1");
  await op.advance();
  const exported = { ...record(3, "fixed"), exportedRequestId: "42" };
  setActual(exported);
  await session.retryCopyTransfer(() => () => {});
  expect(session.content).toEqual(exported.content);
  expect(session.exportState).toBe("exported");
  expect(session.recoveryContent).toBeUndefined();
  expect(op.input).toEqual(local);
});
it("未知另存禁止新的尝试但同次固定核实可继续", async () => {
  const { session, transport } = setup(),
    op = session.beginCopy(
      {
        create: async () => {
          throw Error("lost");
        },
        read: async () => ({ kind: "not_created", attemptId: "copy-1" }),
      },
      "copy-1",
    );
  await op.advance();
  expect(() => session.beginCopy(transport, "copy-2")).toThrow();
  expect(() => session.abandonCopy()).toThrow();
  expect(session.canSave).toBe(false);
  await op.verify();
  expect(op.phase).toBe("not_created");
  session.abandonCopy();
  expect(session.copyLocked).toBe(false);
  expect(session.content).toEqual(local);
});
it("保存请求在途禁止另存", async () => {
  const { transport } = setup();
  let reject!: (error: Error) => void;
  const s = new DraftSession(
    record(1, "base"),
    {
      save: () => new Promise<Draft>((_, r) => (reject = r)),
      read: async () => {
        throw Error("offline");
      },
    },
    () => {},
  );
  s.edit(local);
  const promise = s.flush();
  expect(() => s.beginCopy(transport, "copy-1")).toThrow();
  reject(Error("lost"));
  await promise.catch(() => {});
  expect(() => s.beginCopy(transport, "copy-1")).toThrow();
});
it("旧保存已被更高版本隔离时转交结束旧调度", async () => {
  const { transport } = setup();
  const save = vi.fn(async () => {
    throw new HttpError("result_unconfirmed", "lost");
  });
  let available = false;
  const s = new DraftSession(
    record(1, "base"),
    {
      save,
      read: async () => {
        if (!available) throw Error("offline");
        return record(3, "remote");
      },
    },
    () => {},
  );
  s.edit(local);
  await s.flush().catch(() => {});
  available = true;
  s.observe(record(3, "remote"));
  const op = s.beginCopy(transport, "copy-1");
  await op.advance();
  await s.retryCopyTransfer(() => () => {});
  expect(s.saved).toBe(true);
  expect(s.saveDiagnostic).toContain("旧保存");
  await s.flush();
  expect(save).toHaveBeenCalledTimes(1);
});
it("重复转交只提交一次", async () => {
  const { session, transport } = setup(),
    op = session.beginCopy(transport, "copy-1");
  await op.advance();
  const prepare = vi.fn(() => vi.fn());
  await session.retryCopyTransfer(prepare);
  await session.retryCopyTransfer(prepare);
  expect(prepare).toHaveBeenCalledTimes(1);
});
it.each(["dirty", "unknown", "export", "delete", "append"])(
  "目标 Map 的 %s 会话不能被替换",
  async (kind) => {
    const { session, transport } = setup(),
      op = session.beginCopy(transport, "copy-1");
    await op.advance();
    const result = op.result!;
    if (result.kind !== "created" || result.target.kind !== "editable")
      throw Error();
    const target = result.target.draft,
      existing = new DraftSession(
        target,
        { save: async () => target, read: async () => target },
        () => {},
      );
    if (kind === "dirty") existing.edit({ text: "target dirty" });
    if (kind === "unknown") existing.exportState = "unknown";
    if (kind === "export") existing.exportState = "exporting";
    if (kind === "delete") existing.deletionState = "unknown";
    if (kind === "append") existing.lockAppend();
    const map = new Map([[target.id, existing]]),
      old = cloneClientJson(existing.content);
    await expect(
      session.retryCopyTransfer(
        (d) =>
          prepareCopyTarget(map, d, () => {
            throw Error("must reuse");
          }).commit,
      ),
    ).rejects.toThrow();
    expect(map.get(target.id)).toBe(existing);
    expect(existing.content).toEqual(old);
    expect(session.content).toEqual(local);
  },
);
it("目标 Map、源显示和导航只在所有准备成功后提交", async () => {
  const { session, transport } = setup(),
    op = session.beginCopy(transport, "copy-1");
  await op.advance();
  const map = new Map<string, DraftSession>();
  let active = "source",
    fail = true;
  const prepare = (draft: Draft) => {
    const target = prepareCopyTarget(
      map,
      draft,
      (d) =>
        new DraftSession(
          d,
          { save: async () => d, read: async () => d },
          () => {},
        ),
    );
    if (fail) throw Error("navigation preparation");
    return () => {
      target.commit();
      active = target.session.draft.id;
    };
  };
  await expect(session.retryCopyTransfer(prepare)).rejects.toThrow();
  expect(map.size).toBe(0);
  expect(active).toBe("source");
  expect(session.content).toEqual(local);
  fail = false;
  await session.retryCopyTransfer(prepare);
  expect(map.size).toBe(1);
  expect(active).toBe("copy-target");
  expect(session.saved).toBe(true);
});
it("导航准备期间目标出现新输入撤销整次转交", async () => {
  const { session, transport } = setup(),
    op = session.beginCopy(transport, "copy-1");
  await op.advance();
  const map = new Map<string, DraftSession>();
  let target!: DraftSession;
  await expect(
    session.retryCopyTransfer((d) => {
      const prepared = prepareCopyTarget(
        map,
        d,
        (actual) =>
          new DraftSession(
            actual,
            { save: async () => actual, read: async () => actual },
            () => {},
          ),
      );
      target = prepared.session;
      target.edit({ text: "new target input" });
      return prepared;
    }),
  ).rejects.toThrow();
  expect(session.content).toEqual(local);
  expect(target.content).toEqual({ text: "new target input" });
  expect(map.size).toBe(0);
  expect(op.phase).toBe("created_untransferred");
});
it("未知明确替换被源终态隔离后允许独立另存", async () => {
  const { transport } = setup();
  let readFault = false,
    deleted = false;
  const s = new DraftSession(
    record(1, "base"),
    {
      save: async () => {
        throw new HttpError("result_unconfirmed", "lost");
      },
      read: async () => record(2, "remote"),
      readObservation: async () =>
        readFault
          ? { kind: "unavailable", message: "offline" }
          : deleted
            ? { kind: "deleted" }
            : { kind: "available", draft: record(2, "remote") },
    },
    () => {},
  );
  s.edit(local);
  s.observe(record(2, "remote"));
  const selection = s.prepareConflictSelection();
  readFault = true;
  await s.replaceSavedVersion(selection).catch(() => {});
  deleted = true;
  readFault = false;
  s.observeTarget({ kind: "deleted" });
  expect(() => s.beginCopy(transport, "copy-1")).toThrow();
  expect(s.selectionPendingDecision).toBe(true);
  expect(await s.retrySelection(s.selectionDecision)).toBe("confirmation");
  expect(s.content).toEqual(local);
  const op = s.beginCopy(transport, "copy-1");
  await op.advance();
  await s.retryCopyTransfer(() => () => {});
  expect(op.phase).toBe("transferred");
});
it.each(["unknown-kind", "identity-time", "terminal-regression"])(
  "源转交拒绝不可解释的实际观察：%s",
  async (kind) => {
    const { session, transport, setActual } = setup(),
      op = session.beginCopy(transport, "copy-1");
    await op.advance();
    if (kind === "unknown-kind")
      (session as any).transport.readObservation = async () => ({
        kind: "unrecognized",
      });
    if (kind === "identity-time")
      setActual({ ...record(3, "remote"), createdAt: "2026-10-11 09:00:00" });
    if (kind === "terminal-regression") {
      session.observe({ ...record(3, "fixed"), exportedRequestId: "42" });
      setActual(record(4, "resurrected"));
    }
    await expect(session.retryCopyTransfer(() => () => {})).rejects.toThrow();
    expect(session.content).toEqual(local);
    expect(op.phase).toBe("created_untransferred");
  },
);

function copyOutcome(
  attemptId: string,
  content: DraftContent,
  kind: "editable" | "deleted" | "exported",
): DraftCreationResult {
  const fact = creation(content);
  fact.attemptId = attemptId;
  fact.created.id = `${attemptId}-target`;
  return {
    kind: "created",
    creation: fact,
    target:
      kind === "deleted"
        ? { kind: "deleted" }
        : {
            kind,
            draft:
              kind === "exported"
                ? { ...fact.created, revision: 2, exportedRequestId: "42" }
                : cloneClientJson(fact.created),
          },
  };
}
function copyTransport(kind: "editable" | "deleted" | "exported") {
  return {
    create: async (attemptId: string, content: DraftContent) =>
      copyOutcome(attemptId, content, kind),
    read: async () => {
      throw Error("unused read");
    },
  };
}
// 独立责任是既存所有者状态；夹具只登记它，不把关闭/放弃所选安排当作产生独立输入。
async function independentRecovery(
  session: DraftSession,
  id: string,
  input = local,
  unknown = false,
) {
  const op = new DraftCopyOperation(
    session.draft.id,
    session.version,
    input,
    unknown
      ? {
          create: async () => {
            throw Error("lost");
          },
          read: async () => {
            throw Error("offline");
          },
        }
      : copyTransport("deleted"),
    id,
  );
  await op.advance();
  (session as any).registerCopyRecovery(op);
  (session as any).priorCopies.push(op);
  return { op, recovery: session.copyRecoveries.at(-1)! };
}
it.each(["deleted", "exported"] as const)(
  "纯 A 新决定成功后结束原 %s 目标的输入责任并保留真实创建事实",
  async (kind) => {
    const { session } = setup();
    const original = session.beginCopy(copyTransport(kind), "first");
    await original.advance();
    const originalFact = original.result;
    const next = session.beginNewCopy(copyTransport("editable"), "second");
    await next.advance();
    await session.retryCopyTransfer(() => () => {});
    expect(session.pendingCopyRecoveries).toHaveLength(0);
    expect(session.copyRecovery!.status).toBe("transferred");
    expect(() => session.discardCopyRecovery(session.copyRecovery!)).toThrow();
    expect(original.phase).toBe("created_untransferred");
    expect(original.abandoned).toBe(false);
    expect(original.result).toEqual(originalFact);
    expect(original.attemptId).toBe("first");
    expect(next.phase).toBe("transferred");
    expect(next.attemptId).toBe("second");
    expect(next.input).toEqual(local);
  },
);
it("两次目标终态后的第三次转交只结束同一 A 的责任", async () => {
  const { session } = setup(),
    first = session.beginCopy(copyTransport("deleted"), "first");
  await first.advance();
  const second = session.beginNewCopy(copyTransport("exported"), "second");
  await second.advance();
  expect(session.pendingCopyRecoveries).toHaveLength(1);
  const third = session.beginNewCopy(copyTransport("editable"), "third");
  await third.advance();
  await session.retryCopyTransfer(() => () => {});
  expect(session.pendingCopyRecoveries).toHaveLength(0);
  expect(first.phase).toBe("created_untransferred");
  expect(second.phase).toBe("created_untransferred");
  expect(third.phase).toBe("transferred");
});
it("第二次可靠未创建的放弃交回原 A 并保留旧终态事实", async () => {
  const { session } = setup(),
    first = session.beginCopy(copyTransport("deleted"), "first");
  await first.advance();
  const second = session.beginNewCopy(
    {
      create: async () => ({ kind: "not_created", attemptId: "second" }),
      read: async () => ({ kind: "not_created", attemptId: "second" }),
    },
    "second",
  );
  await second.advance();
  expect(session.pendingCopyRecoveries).toHaveLength(1);
  session.abandonCopy();
  expect(second.abandoned).toBe(true);
  expect(session.pendingCopyRecoveries).toHaveLength(0);
  expect(session.copyRecovery!.status).toBe("returned");
  expect(session.content).toEqual(local);
  expect(first.abandoned).toBe(false);
});
it("第二次所有本地准备拒绝保留关联，成功转交后才结束 A", async () => {
  const { session } = setup(),
    first = session.beginCopy(copyTransport("deleted"), "first");
  await first.advance();
  const next = session.beginNewCopy(copyTransport("editable"), "second");
  await next.advance();
  await expect(
    session.retryCopyTransfer(() => {
      throw Error("navigation");
    }),
  ).rejects.toThrow();
  expect(session.pendingCopyRecoveries).toHaveLength(1);
  expect(session.content).toEqual(local);
  await session.retryCopyTransfer(() => () => {});
  expect(session.pendingCopyRecoveries).toHaveLength(0);
  expect(first.phase).toBe("created_untransferred");
  expect(next.phase).toBe("transferred");
});
it.each([
  "new",
  "in-flight",
  "unknown",
  "invalid",
  "not_created",
  "deleted",
  "exported",
] as const)("明确新决定的 %s 分区保持同一 A 的恢复责任", async (phase) => {
  const { session } = setup(),
    first = session.beginCopy(copyTransport("deleted"), "first");
  await first.advance();
  const fact = first.result,
    recovery = session.pendingCopyRecoveries[0];
  let resolve!: (value: DraftCreationResult) => void;
  const transport = {
    create: async (): Promise<DraftCreationResult> => {
      if (phase === "in-flight") return new Promise((r) => (resolve = r));
      if (phase === "unknown") throw Error("lost");
      if (phase === "invalid")
        return { kind: "not_created", attemptId: "wrong" };
      if (phase === "not_created")
        return { kind: "not_created", attemptId: "second" };
      return copyOutcome(
        "second",
        local,
        phase === "deleted" || phase === "exported" ? phase : "editable",
      );
    },
    read: async () => {
      throw Error("offline");
    },
  };
  const next = session.beginNewCopy(transport, "second"),
    pending = phase === "new" ? undefined : next.advance();
  if (phase !== "in-flight") await pending;
  expect(session.pendingCopyRecoveries).toEqual([recovery]);
  expect(recovery.status).toBe("pending");
  expect(recovery.attemptIds).toEqual(["first", "second"]);
  expect(recovery.input).toEqual(local);
  expect(first.result).toEqual(fact);
  if (["new", "in-flight", "unknown", "invalid"].includes(phase))
    expect(() => session.discardCopyRecovery(recovery)).toThrow();
  if (phase === "unknown") {
    await next.verify();
    expect(next.phase).toBe("unknown");
    expect(recovery.status).toBe("pending");
  }
  if (phase === "in-flight") {
    resolve({ kind: "not_created", attemptId: "second" });
    await pending;
  }
});
it.each([
  "source-read",
  "source-display",
  "target-map",
  "target-display",
  "navigation",
  "source-epoch",
  "target-validate",
] as const)(
  "关联 A 在 %s 拒绝后保留，只由重试共同成功结束",
  async (failure) => {
    const { session } = setup(),
      first = session.beginCopy(copyTransport("deleted"), "first");
    await first.advance();
    const fact = first.result;
    const next = session.beginNewCopy(copyTransport("editable"), "second");
    await next.advance();
    const recovery = session.copyRecovery!;
    let unbind = () => {};
    if (failure === "source-read")
      (session as any).transport.readObservation = async () => ({
        kind: "unavailable",
        message: "offline",
      });
    if (failure === "source-display")
      unbind = session.bindWholeView(() => {
        throw Error("source view");
      });
    const prepare = (actual: Draft) => {
      if (failure === "navigation") throw Error("navigation");
      if (failure === "source-epoch") {
        session.observeTarget({
          kind: "unavailable",
          message: "lost after preparation",
        });
        return () => {};
      }
      if (failure === "target-validate")
        return {
          validate: () => {
            throw Error("changed");
          },
          commit: () => {},
        };
      if (failure === "target-map" || failure === "target-display") {
        const target = new DraftSession(
          actual,
          { save: async () => actual, read: async () => actual },
          () => {},
        );
        if (failure === "target-map") target.edit({ text: "target dirty" });
        else
          target.bindWholeView(() => {
            throw Error("target view");
          });
        return prepareCopyTarget(new Map([[actual.id, target]]), actual, () => {
          throw Error("must reuse");
        });
      }
      return () => {};
    };
    await expect(session.retryCopyTransfer(prepare)).rejects.toThrow();
    expect(recovery.status).toBe("pending");
    expect(session.pendingCopyRecoveries).toEqual([recovery]);
    expect(session.content).toEqual(local);
    expect(first.result).toEqual(fact);
    unbind();
    (session as any).transport.readObservation = undefined;
    await session.retryCopyTransfer(() => () => {});
    expect(recovery.status).toBe("transferred");
    expect(recovery.transferredBy).toBe("second");
    expect(session.pendingCopyRecoveries).toHaveLength(0);
  },
);
it("放弃第二次未创建安排后输入归源，后续另存登记新的责任", async () => {
  const { session } = setup(),
    first = session.beginCopy(copyTransport("deleted"), "first");
  await first.advance();
  const recovery = session.copyRecovery!;
  const second = session.beginNewCopy(
    {
      create: async () => ({ kind: "not_created", attemptId: "second" }),
      read: async () => ({ kind: "not_created", attemptId: "second" }),
    },
    "second",
  );
  await second.advance();
  session.abandonCopy();
  expect(() =>
    session.beginRecoveryCopy(recovery, copyTransport("editable"), "third"),
  ).toThrow();
  const third = session.beginCopy(copyTransport("editable"), "third");
  await third.advance();
  await session.retryCopyTransfer(() => () => {});
  expect(recovery.status).toBe("returned");
  expect(recovery.attemptIds).toEqual(["first", "second"]);
  expect(session.copyRecovery!.status).toBe("transferred");
  expect(second.abandoned).toBe(true);
  expect(first.abandoned).toBe(false);
  expect(first.phase).toBe("created_untransferred");
});
it("完整内容相同的独立责任不能由另一条明确关联链清除", async () => {
  const { session } = setup(),
    { op: first, recovery: independent } = await independentRecovery(
      session,
      "independent",
    );
  const fact = first.result;
  const second = session.beginCopy(copyTransport("deleted"), "chain-root");
  await second.advance();
  const linked = session.copyRecovery!;
  const third = session.beginNewCopy(
    copyTransport("editable"),
    "chain-success",
  );
  await third.advance();
  await session.retryCopyTransfer(() => () => {});
  await session.retryCopyTransfer(() => {
    throw Error("must not repeat");
  });
  expect(linked.status).toBe("transferred");
  expect(independent.status).toBe("pending");
  expect(session.pendingCopyRecoveries).toEqual([independent]);
  expect(independent.attemptIds).toEqual(["independent"]);
  expect(first.result).toEqual(fact);
});
it("恢复视图不能由克隆、其他会话或过期资格伪造放弃", async () => {
  const { session } = setup(),
    first = session.beginCopy(copyTransport("deleted"), "first");
  await first.advance();
  const recovery = session.copyRecovery!;
  const other = setup().session,
    foreign = other.beginCopy(copyTransport("deleted"), "foreign");
  await foreign.advance();
  expect(() => session.discardCopyRecovery({ ...recovery })).toThrow();
  expect(() => session.discardCopyRecovery(other.copyRecovery!)).toThrow();
  expect(recovery.status).toBe("pending");
  expect(() => session.discardCopyRecovery(recovery)).toThrow();
  const decision = session.copyDecision!;
  expect(() => session.abandonCopy({ ...decision })).toThrow();
  expect(() => session.abandonCopy(other.copyDecision!)).toThrow();
  session.abandonCopy(decision);
  expect(recovery.status).toBe("returned");
  expect(() => session.abandonCopy(decision)).toThrow();
  expect(() => session.discardCopyRecovery(recovery)).toThrow();
  expect(other.copyRecovery!.status).toBe("pending");
});
it("明确放弃选中责任不能连带清除独立未知责任", async () => {
  const { session } = setup(),
    first = session.beginCopy(copyTransport("deleted"), "first");
  await first.advance();
  const original = session.copyRecovery!;
  const { op: second, recovery: independent } = await independentRecovery(
    session,
    "independent",
    local,
    true,
  );
  expect(() => session.discardCopyRecovery(independent)).toThrow();
  session.abandonCopy(session.copyDecision!);
  expect(original.status).toBe("returned");
  expect(independent.status).toBe("pending");
  expect(second.phase).toBe("unknown");
  expect(session.pendingCopyRecoveries).toEqual([independent]);
});
it("待决定禁止产生 B，交回 A 后编辑 B 不复活旧安排", async () => {
  const { session } = setup(),
    first = session.beginCopy(copyTransport("deleted"), "first");
  await first.advance();
  const recovery = session.copyRecovery!,
    other = { text: "B" };
  expect(() => session.edit(other)).toThrow();
  expect(session.content).toEqual(local);
  session.abandonCopy(session.copyDecision!);
  session.edit(other);
  expect(() =>
    session.beginRecoveryCopy(recovery, copyTransport("editable"), "second"),
  ).toThrow();
  expect(recovery.input).toEqual(local);
  expect(session.content).toEqual(other);
  expect(recovery.status).toBe("returned");
  expect(session.pendingCopyRecoveries).toHaveLength(0);
});
function fullCopyInput(): DraftContent {
  return parseClientJson(
    '{"text":"{","pending":{"":{"kind":"json","text":"{"}},"actionVariants":{"0":[{"type":"camera_record","fields":{"amount":1.0000000000000001},"fieldsText":"{\\"amount\\":1.0000000000000001}","pending":{}}]},"parameterVariants":{"0":[{"paramsText":"{\\"params\\":{\\"type\\":\\"photo\\",\\"amount\\":1.0000000000000001}}","pending":{"/amount":{"kind":"number","text":"-"}}}]},"objectVariants":{"0":{"/exposure":{"discriminator":"mode","variants":[{"valueText":"{\\"value\\":{\\"mode\\":\\"manual\\",\\"iso\\":1e-999}}","pending":{}}]}}},"automaticPreviews":{"namespace":"source","next":1,"intent":"unset","actions":[{"id":"source-id"}]}}',
  ) as DraftContent;
}
it("恢复责任链接保留全部私有资料与数字词元，公开输入修改不改权威", async () => {
  const full = fullCopyInput();
  const { session } = setup();
  session.edit(full);
  const first = session.beginCopy(copyTransport("deleted"), "first");
  await first.advance();
  const recovery = session.copyRecovery!,
    expected = stringifyJson(full);
  recovery.input.text = "mutated";
  const next = session.beginNewCopy(copyTransport("editable"), "second");
  await next.advance();
  await session.retryCopyTransfer(() => () => {});
  expect(stringifyJson(recovery.input)).toBe(expected);
  expect(stringifyJson(next.input)).toBe(expected);
  expect(recovery.status).toBe("transferred");
  expect(expected).toContain("1.0000000000000001");
  expect(expected).toContain("1e-999");
});

it("validate 启动同身份核实后必须零共同提交，恢复 editable 后仅本地重试", async () => {
  const { session } = setup(),
    first = session.beginCopy(copyTransport("deleted"), "first");
  await first.advance();
  const firstFact = first.result;
  const create = vi.fn(async (attemptId: string, content: DraftContent) =>
    copyOutcome(attemptId, content, "editable"),
  );
  let resolve!: (value: DraftCreationResult) => void;
  const read = vi.fn(
    () => new Promise<DraftCreationResult>((r) => (resolve = r)),
  );
  const next = session.beginNewCopy({ create, read }, "second");
  await next.advance();
  const fact = next.result,
    recovery = session.copyRecovery!,
    version = session.version;
  const map = new Map<string, DraftSession>(),
    commits = { source: 0, target: 0, navigation: 0 };
  let verification!: Promise<void>;
  session.bindWholeView(() => () => {
    commits.source++;
  });
  const prepare = (actual: Draft, verify: boolean) => {
    const target = prepareCopyTarget(map, actual, (d) => {
      const targetSession = new DraftSession(
        d,
        { save: async () => d, read: async () => d },
        () => {},
      );
      targetSession.bindWholeView(() => () => {
        commits.target++;
      });
      return targetSession;
    });
    return {
      validate: () => {
        target.validate();
        if (verify) verification = next.verify();
      },
      commit: () => {
        target.commit();
        commits.navigation++;
      },
    };
  };
  await expect(
    session.retryCopyTransfer((d) => prepare(d, true)),
  ).rejects.toThrow();
  const rejected = {
    input: cloneClientJson(session.content),
    version: session.version,
    mapSize: map.size,
    commits: { ...commits },
    firstFact: first.result,
    fact: next.result,
    recovery: recovery.status,
    phase: next.phase,
  };
  resolve(copyOutcome("second", local, "editable"));
  await verification;
  expect(rejected).toEqual({
    input: local,
    version,
    mapSize: 0,
    commits: { source: 0, target: 0, navigation: 0 },
    firstFact,
    fact,
    recovery: "pending",
    phase: "submitting",
  });
  await session.retryCopyTransfer((d) => prepare(d, false));
  expect(commits).toEqual({ source: 1, target: 1, navigation: 1 });
  expect(map.size).toBe(1);
  expect(recovery.status).toBe("transferred");
  expect(create).toHaveBeenCalledTimes(1);
});

async function preparedLinkedTransfer() {
  const { session } = setup(),
    first = session.beginCopy(copyTransport("deleted"), "first");
  await first.advance();
  const create = vi.fn(async (id: string, input: DraftContent) =>
    copyOutcome(id, input, "editable"),
  );
  let reply: () => Promise<DraftCreationResult> = async () =>
    copyOutcome("second", local, "editable");
  const read = vi.fn(() => reply());
  const next = session.beginNewCopy({ create, read }, "second");
  await next.advance();
  const recovery = session.copyRecovery!,
    firstFact = first.result,
    fact = next.result,
    version = session.version,
    map = new Map<string, DraftSession>(),
    commits = { source: 0, target: 0, navigation: 0 };
  session.bindWholeView(() => () => {
    commits.source++;
  });
  const transfer = (validate: () => void = () => {}) =>
    session.retryCopyTransfer((actual) => {
      const target = prepareCopyTarget(map, actual, (d) => {
        const s = new DraftSession(
          d,
          { read: async () => d, save: async () => d },
          () => {},
        );
        s.bindWholeView(() => () => {
          commits.target++;
        });
        return s;
      });
      return {
        validate: () => {
          target.validate();
          validate();
        },
        commit: () => {
          target.commit();
          commits.navigation++;
        },
      };
    });
  const expectUncommitted = (
    currentInput = local,
    currentVersion = version,
  ) => {
    expect(commits).toEqual({ source: 0, target: 0, navigation: 0 });
    expect(map.size).toBe(0);
    expect(session.content).toEqual(currentInput);
    expect(session.version).toBe(currentVersion);
    expect(recovery.input).toEqual(local);
    expect(first.result).toEqual(firstFact);
    expect(recovery.transferredBy).toBeUndefined();
  };
  return {
    session,
    first,
    next,
    recovery,
    fact,
    version,
    create,
    map,
    transfer,
    expectUncommitted,
    setReply: (value: typeof reply) => {
      reply = value;
    },
  };
}

it.each([
  "throws",
  "observation",
  "identity",
  "createdAt",
  "version",
  "revision",
  "confirmed-version",
  "input",
  "private-input",
  "append",
  "saving",
  "export",
  "exported-id",
  "deletion",
  "operation",
  "creation",
  "recovery-generation",
  "recovery-link",
  "recovery-status",
  "recovery-input",
  "recovery-version",
] as const)(
  "validate 后 %s 改变资格时零共同提交并保留固定 A",
  async (change) => {
    const f = await preparedLinkedTransfer();
    const { session, next, first, recovery } = f;
    const localB: DraftContent =
      change === "private-input"
        ? { text: local.text, pending: { "": { kind: "json", text: "[" } } }
        : { text: "later B" };
    let restore = () => {};
    // 对私有责任资格做受控故障注入，验证最终所有者复核而非假定外部视图可改写。
    const owner = session as any;
    const record = owner.recoveryRecords[0],
      generation = record.generation;
    await expect(
      f.transfer(() => {
        switch (change) {
          case "throws":
            throw Error("navigation validation");
          case "observation":
            session.observe(recordDraftForValidation());
            break;
          case "identity":
            session.draft.id = "changed-source";
            break;
          case "createdAt":
            session.draft.createdAt = "2026-10-11 11:00:00";
            break;
          case "version":
            session.version++;
            break;
          case "revision":
            session.revision++;
            break;
          case "confirmed-version":
            session.confirmedVersion++;
            break;
          case "input":
          case "private-input":
            session.content = localB;
            break;
          case "append":
            session.appendLocked = true;
            break;
          case "saving":
            session.saving = true;
            break;
          case "export":
            session.exportState = "unknown";
            break;
          case "exported-id":
            session.exportedRequestId = "42";
            break;
          case "deletion":
            session.deletionState = "unknown";
            break;
          case "operation": {
            const spy = vi
              .spyOn(session, "copyOperation", "get")
              .mockReturnValue(first);
            restore = () => spy.mockRestore();
            break;
          }
          case "creation": {
            const changed = cloneClientJson(f.fact!);
            if (changed.kind === "created" && changed.target.kind !== "deleted")
              changed.target.draft.revision++;
            const spy = vi
              .spyOn(next, "result", "get")
              .mockReturnValue(changed);
            restore = () => spy.mockRestore();
            break;
          }
          case "recovery-generation":
            record.generation++;
            break;
          case "recovery-link":
            owner.operationRecoveries.delete(next);
            restore = () => owner.operationRecoveries.set(next, record);
            break;
          case "recovery-status":
            record.status = "discarded";
            break;
          case "recovery-input":
            record.input = localB;
            restore = () => {
              record.input = cloneClientJson(local);
            };
            break;
          case "recovery-version":
            record.sourceVersion++;
            restore = () => {
              record.sourceVersion = next.sourceVersion;
            };
            break;
        }
      }),
    ).rejects.toThrow();
    restore();
    f.expectUncommitted(
      change === "input" || change === "private-input" ? localB : local,
      change === "version" ? f.version + 1 : f.version,
    );
    expect(next.result).toEqual(f.fact);
    expect(next.phase).toBe("created_untransferred");
    // 被注入的取消事实由测试恢复；共同提交本身不得把它改成成功。
    expect(recovery.status).toBe(
      change === "recovery-status" ? "discarded" : "pending",
    );
    record.status = "pending";
    record.generation = generation;
    expect(session.pendingCopyRecoveries).toEqual([recovery]);
  },
);
function recordDraftForValidation() {
  return record(3, "observed later");
}

it.each(["unknown", "invalid", "deleted", "exported"] as const)(
  "validate 启动核实后变为 %s 时保留 A，不重复创建目标",
  async (kind) => {
    const f = await preparedLinkedTransfer();
    let resolve!: (value: DraftCreationResult) => void,
      reject!: (error: Error) => void;
    f.setReply(
      () =>
        new Promise((yes, no) => {
          resolve = yes;
          reject = no;
        }),
    );
    let verification!: Promise<void>;
    await expect(
      f.transfer(() => {
        verification = f.next.verify();
      }),
    ).rejects.toThrow();
    f.expectUncommitted();
    expect(f.next.phase).toBe("submitting");
    if (kind === "unknown") reject(Error("read unavailable"));
    else if (kind === "invalid")
      resolve({ kind: "not_created", attemptId: "wrong" });
    else resolve(copyOutcome("second", local, kind));
    await verification;
    expect(f.next.phase).toBe(
      kind === "unknown" || kind === "invalid" ? kind : "created_untransferred",
    );
    await expect(f.transfer()).rejects.toThrow();
    f.expectUncommitted();
    expect(f.recovery.status).toBe("pending");
    expect(f.create).toHaveBeenCalledTimes(1);
    if (kind === "unknown" || kind === "invalid") {
      f.setReply(async () => copyOutcome("second", local, "editable"));
      await f.next.verify();
      await f.transfer();
      expect(f.recovery.status).toBe("transferred");
      expect(f.create).toHaveBeenCalledTimes(1);
    } else {
      expect(f.next.terminalTarget).toBe(true);
      expect(f.next.abandoned).toBe(false);
      const terminal = f.next.result;
      const third = f.session.beginRecoveryCopy(
        f.recovery,
        copyTransport("editable"),
        "third",
      );
      await third.advance();
      await f.transfer();
      expect(f.recovery.transferredBy).toBe("third");
      expect(f.next.result).toEqual(terminal);
      expect(f.next.phase).toBe("created_untransferred");
    }
  },
);

it("validate 不能取消正在共同准备的恢复责任或启动关联新决定", async () => {
  const f = await preparedLinkedTransfer();
  await expect(
    f.transfer(() => f.session.discardCopyRecovery(f.recovery)),
  ).rejects.toThrow();
  f.expectUncommitted();
  await expect(
    f.transfer(() =>
      f.session.beginRecoveryCopy(
        f.recovery,
        copyTransport("editable"),
        "third",
      ),
    ),
  ).rejects.toThrow();
  f.expectUncommitted();
  expect(f.recovery.status).toBe("pending");
  expect(f.recovery.attemptIds).toEqual(["first", "second"]);
  await f.transfer();
  expect(f.recovery.status).toBe("transferred");
});
it("validate 后同身份核实得到较新 editable 目标时仅接纳当前记录", async () => {
  const f = await preparedLinkedTransfer();
  let resolve!: (result: DraftCreationResult) => void;
  f.setReply(
    () =>
      new Promise((r) => {
        resolve = r;
      }),
  );
  let verification!: Promise<void>;
  await expect(
    f.transfer(() => {
      verification = f.next.verify();
    }),
  ).rejects.toThrow();
  f.expectUncommitted();
  const current = copyOutcome("second", local, "editable");
  if (current.kind !== "created" || current.target.kind !== "editable")
    throw Error("fixture");
  current.target.draft.revision = 2;
  current.target.draft.content = record(2, "target edited elsewhere").content;
  resolve(current);
  await verification;
  f.expectUncommitted();
  await f.transfer();
  expect(f.map.get("second-target")!.content).toEqual(
    current.target.draft.content,
  );
  expect(f.next.result).toEqual(current);
  expect(current.creation).toEqual(
    f.fact!.kind === "created" && f.fact!.creation,
  );
  expect(f.recovery.input).toEqual(local);
  expect(f.recovery.status).toBe("transferred");
  expect(f.create).toHaveBeenCalledTimes(1);
});

it.each(["unknown", "invalid"] as const)(
  "弹窗 %s 重试先按固定身份核实，可靠 editable 后当次共同转交",
  async (phase) => {
    const { session } = setup();
    const create = vi.fn(async (): Promise<DraftCreationResult> => {
      if (phase === "unknown") throw Error("lost");
      return { kind: "not_created", attemptId: "wrong" };
    });
    const read = vi.fn(async (id: string) =>
      copyOutcome(id, local, "editable"),
    );
    const op = session.beginCopy({ create, read }, "modal");
    await op.advance();
    const decision = session.copyDecision!;
    expect(decision.canRetry).toBe(true);
    expect(decision.canAbandon).toBe(false);
    expect(() => session.abandonCopy(decision)).toThrow();
    expect(await session.retryCopy(decision, () => () => {})).toBe(
      "transferred",
    );
    expect(create).toHaveBeenCalledTimes(1);
    expect(read).toHaveBeenCalledExactlyOnceWith("modal");
    expect(op.input).toEqual(local);
    expect(session.copyPendingDecision).toBe(false);
    expect(session.pendingCopyRecoveries).toHaveLength(0);
  },
);
it("可靠未创建重试沿用同一身份与全部输入，然后当次转交", async () => {
  const { session } = setup();
  const create = vi.fn(
    async (id: string, input: DraftContent): Promise<DraftCreationResult> =>
      create.mock.calls.length === 1
        ? { kind: "not_created", attemptId: id }
        : copyOutcome(id, input, "editable"),
  );
  const read = vi.fn(async (id: string): Promise<DraftCreationResult> => ({
    kind: "not_created",
    attemptId: id,
  }));
  const op = session.beginCopy({ create, read }, "modal");
  await op.advance();
  expect(session.copyDecision!.canAbandon).toBe(true);
  await session.retryCopy(
    session.copyDecision!,
    () => () => {},
    "ignored-new-id",
  );
  expect(create.mock.calls).toEqual([
    ["modal", local],
    ["modal", local],
  ]);
  expect(read).not.toHaveBeenCalled();
  expect(op.phase).toBe("transferred");
});
it("unknown 核实发现终态只更新事实，下次明确新决定才另存", async () => {
  const { session } = setup();
  const create = vi.fn(async (id: string, input: DraftContent) => {
    if (id === "first") throw Error("lost");
    return copyOutcome(id, input, "editable");
  });
  const read = vi.fn(async (id: string) => copyOutcome(id, local, "deleted"));
  const first = session.beginCopy({ create, read }, "first");
  await first.advance();
  expect(
    await session.retryCopy(session.copyDecision!, () => () => {}, "second"),
  ).toBe("pending");
  expect(create).toHaveBeenCalledTimes(1);
  expect(session.copyDecision!.retryCreatesNewAttempt).toBe(true);
  const fact = first.result,
    recovery = session.copyRecovery!;
  expect(
    await session.retryCopy(session.copyDecision!, () => () => {}, "second"),
  ).toBe("transferred");
  expect(create.mock.calls).toEqual([
    ["first", local],
    ["second", local],
  ]);
  expect(recovery.attemptIds).toEqual(["first", "second"]);
  expect(recovery.transferredBy).toBe("second");
  expect(first.result).toEqual(fact);
  expect(first.phase).toBe("created_untransferred");
  expect(first.abandoned).toBe(false);
});
it("来源读取资格失效时重试只恢复观察，不授权新身份或放弃", async () => {
  const { session } = setup();
  const create = vi.fn(async (id: string, input: DraftContent) =>
      copyOutcome(id, input, "deleted"),
    ),
    read = vi.fn();
  const first = session.beginCopy({ create, read }, "first");
  await first.advance();
  session.observeTarget({ kind: "unavailable", message: "offline" });
  const decision = session.copyDecision!;
  expect(decision.canAbandon).toBe(false);
  expect(decision.retryCreatesNewAttempt).toBe(false);
  expect(() => session.abandonCopy(decision)).toThrow();
  expect(await session.retryCopy(decision, () => () => {}, "second")).toBe(
    "pending",
  );
  expect(create).toHaveBeenCalledTimes(1);
  expect(read).not.toHaveBeenCalled();
  expect(session.copyDecision!.canAbandon).toBe(true);
  expect(session.content).toEqual(local);
});
it.each(["source", "target", "validate"] as const)(
  "editable 在 %s 准备拒绝后只重试本地转交或交回输入",
  async (failure) => {
    const { session, transport, create } = setup(),
      op = session.beginCopy(transport, "modal");
    await op.advance();
    let commits = 0;
    const unbind = session.bindWholeView(() => {
      if (failure === "source") throw Error("source view");
      return () => {
        commits++;
      };
    });
    const prepare = () => {
      if (failure === "target") throw Error("target map");
      return {
        validate: () => {
          throw Error("changed");
        },
        commit: () => {
          commits++;
        },
      };
    };
    await expect(
      session.retryCopy(session.copyDecision!, prepare),
    ).rejects.toThrow();
    expect(commits).toBe(0);
    expect(session.content).toEqual(local);
    expect(session.copyPendingDecision).toBe(true);
    expect(session.copyDecision!.canAbandon).toBe(true);
    expect(create).toHaveBeenCalledTimes(1);
    const fact = op.result;
    session.abandonCopy(session.copyDecision!);
    expect(session.content).toEqual(local);
    expect(session.copyRecovery!.status).toBe("returned");
    expect(session.canSave).toBe(false);
    expect(op.result).toEqual(fact);
    unbind();
  },
);
const terminalSourceCases = (["deleted", "exported"] as const).flatMap(
  (target) =>
    (["conflict", "deleted", "exported"] as const).map(
      (source) => [target, source] as const,
    ),
);
it.each(terminalSourceCases)(
  "目标 %s 与来源 %s 的放弃保留完整输入和真实事实",
  async (targetKind, source) => {
    const { session, setActual } = setup();
    if (source === "deleted") {
      (session as any).transport.readObservation = async () => ({
        kind: "deleted",
      });
      session.observeTarget({ kind: "deleted" });
    }
    if (source === "exported") {
      const actual = { ...record(3, "remote"), exportedRequestId: "42" };
      setActual(actual);
      session.observe(actual);
    }
    const create = vi.fn(async (id: string, input: DraftContent) =>
        copyOutcome(id, input, targetKind),
      ),
      read = vi.fn();
    const op = session.beginCopy({ create, read }, `modal-${source}`);
    await op.advance();
    const fact = op.result,
      recovery = session.copyRecovery!,
      version = session.version;
    expect(session.copyPendingDecision).toBe(true);
    expect(session.canEdit).toBe(false);
    await op.verify();
    expect(read).not.toHaveBeenCalled();
    session.abandonCopy(session.copyDecision!);
    expect(session.content).toEqual(local);
    expect(session.version).toBe(version);
    expect(recovery.status).toBe("returned");
    expect(session.pendingCopyRecoveries).toHaveLength(0);
    expect(op.result).toEqual(fact);
    expect(op.phase).toBe("created_untransferred");
    expect(session.canEdit).toBe(source === "conflict");
    expect(session.canSave).toBe(false);
    expect(session.saved).toBe(false);
    expect(create).toHaveBeenCalledTimes(1);
  },
);
it.each(terminalSourceCases)(
  "目标 %s 的明确新决定沿用 transport 与保留输入，来源 %s 仍可转交",
  async (kind, source) => {
    const { session, setActual } = setup();
    if (source === "deleted") {
      (session as any).transport.readObservation = async () => ({
        kind: "deleted",
      });
      session.observeTarget({ kind: "deleted" });
    }
    if (source === "exported") {
      const actual = { ...record(3, "remote"), exportedRequestId: "42" };
      setActual(actual);
      session.observe(actual);
    }
    const create = vi.fn(async (id: string, input: DraftContent) =>
      copyOutcome(id, input, id === "first" ? kind : "editable"),
    );
    const op = session.beginCopy(
      {
        create,
        read: async () => {
          throw Error("terminal lookup ended");
        },
      },
      "first",
    );
    await op.advance();
    const fact = op.result,
      recovery = session.copyRecovery!;
    await session.retryCopy(session.copyDecision!, () => () => {}, "second");
    expect(create.mock.calls).toEqual([
      ["first", local],
      ["second", local],
    ]);
    expect(recovery.status).toBe("transferred");
    expect(op.result).toEqual(fact);
    expect(op.abandoned).toBe(false);
    expect(session.canEdit).toBe(source === "conflict");
  },
);
