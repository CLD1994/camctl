import { expect, it } from "vitest";
import { DraftSession } from "../../src/web/session";
import { completeDraftInput, record, creation } from "../helpers/draft-conflicts";
import { stringifyJson } from "../../src/shared/json";
import type { DraftObservation } from "../../src/web/draft-conflicts";

it.each(["updated", "exported", "unavailable", "invalid"] as const)("Task7具体导出核实等待期间%s新观察阻止旧读回降级事实", async kind => {
  const input=completeDraftInput(), base={...record(1,"base"),content:input};
  let release!: (value: DraftObservation) => void;
  const response=new Promise<DraftObservation>(yes => {release=yes;});
  const session=new DraftSession(base,{save:async () => {throw Error("只核实不能写入");},read:async () => base,readObservation:() => response},() => {});
  session.beginExport(); session.exportUnknown();
  const checking=session.checkExport();
  const fresh: DraftObservation=kind==="unavailable" ? {kind:"unavailable",message:"new offline"} : {kind:"available",draft:{...record(3,"newer"),...(kind==="exported" ? {exportedRequestId:"99"} : kind==="invalid" ? {id:"foreign"} : {})}};
  session.observeTarget(fresh);
  const known=session.lastReliableDraft;
  release({kind:"available",draft:{...base,revision:2,exportedRequestId:"42"}});
  await expect(checking).rejects.toThrow();
  expect(session.targetObservation).toEqual(fresh); expect(session.lastReliableDraft).toEqual(known);
  expect(session.exportState).toBe("unknown"); expect(session.exportedRequestId).toBe(kind==="exported" ? "99" : undefined);
  expect(session.content).toEqual(input); expect(session.needsLeaveWarning).toBe(true); expect(session.canSave).toBe(false);
});

it.each((["export","delete","append"] as const).flatMap(operation => (["updated","exported","deleted"] as const).map(observation => ({operation,observation}))))(
  "Task7公开 $operation 待定期间来源 $observation 不越过独立责任",
  async ({operation,observation}) => {
    const input=completeDraftInput(), base={...record(1,"base"),content:input}; let saves=0;
    const session=new DraftSession(base,{save:async () => {saves++; return base;},read:async () => base},() => {});
    if(operation==="export") {session.beginExport(); session.exportUnknown();}
    else if(operation==="delete") await expect(session.delete(async () => {throw Error("reply lost");},async () => {throw Error("offline");})).rejects.toThrow();
    else session.lockAppend();
    if(observation==="deleted") session.observeTarget({kind:"deleted"});
    else session.observe({...record(2,"remote"),...(observation==="exported" ? {exportedRequestId:"1"}: {})});
    expect(stringifyJson(session.content)).toBe(stringifyJson(input));
    expect(session.hasUnfinishedResponsibilities).toBe(true); expect(session.needsLeaveWarning).toBe(true);
    expect(session.canEdit).toBe(false); expect(session.canBeginCopy).toBe(false);
    expect(() => session.prepareConflictSelection()).toThrow(); expect(saves).toBe(0);
    if(operation==="export") expect(session.exportState).toBe("unknown");
    else if(operation==="delete") expect(session.deletionState).toBe("unknown");
    else expect(session.appendLocked).toBe(true);
  },
);

it("Task7公开另存终态重试沿一条责任链，未结束时不能产生第二独立恢复输入", async () => {
  const input=completeDraftInput(), base={...record(1,"base"),content:input};
  const session=new DraftSession(base,{save:async () => base,read:async () => base},() => {});
  const transport={create:async (attemptId:string) => ({kind:"created" as const,creation:{...creation(input),attemptId},target:{kind:"deleted" as const}}),read:async () => {throw Error("正常创建无需核实");}};
  const first=session.beginCopy(transport,"first"); await session.retryCopy(session.copyDecision!,() => {throw Error("终态不得准备转交");});
  const recovery=session.copyRecovery!; expect(session.pendingCopyRecoveries).toEqual([recovery]);
  expect(() => session.beginCopy(transport,"independent")).toThrow();
  await session.retryCopy(session.copyDecision!,() => {throw Error("终态不得准备转交");},"second");
  expect(session.pendingCopyRecoveries).toEqual([recovery]); expect(recovery.attemptIds).toEqual(["first","second"]);
  expect(first.phase).toBe("created_untransferred"); expect(first.result?.kind).toBe("created");
  session.abandonCopy(session.copyDecision!); expect(recovery.status).toBe("returned"); expect(session.pendingCopyRecoveries).toHaveLength(0);
  expect(stringifyJson(session.content)).toBe(stringifyJson(input));
});
