import { expect, it, vi, afterEach } from "vitest";
import { DraftSession } from "../../src/web/session";
import { HttpError } from "../../src/web/api";
import type { Draft, DraftContent } from "../../src/server/models";
const draft = (revision: number, name: string): Draft => ({id: "d", revision, content: {text: JSON.stringify({name, actions: []}, null, 2)}, createdAt: "2026-10-12 00:00:00", updatedAt: "2026-10-12 00:00:01"});
afterEach(() => vi.useRealTimers());

// 编辑代次不等于尚未保存的完整输入；恢复基线时错误发送PUT将违反此用例。
it("恢复已确认完整基线后450毫秒不再写相同内容且保存事实成立", async () => {
  vi.useFakeTimers(); let writes = 0;
  const base = draft(1, "base");
  const session = new DraftSession(base, {save: async () => {writes++; return draft(2, "base");}, read: async () => base}, () => {});
  session.edit(draft(1, "temporary").content); session.schedule();
  session.edit(base.content); session.schedule();
  await vi.advanceTimersByTimeAsync(449); expect(writes).toBe(0);
  await vi.advanceTimersByTimeAsync(1); expect(writes).toBe(0);
  expect(session.saved).toBe(true); expect(session.revision).toBe(1);
});

const parts = ["text", "pending-number", "pending-json", "actionVariants", "parameterVariants", "objectVariants", "automaticPreviews"] as const;
function input(part: typeof parts[number], tag: string): DraftContent {
  const text = '{"name":"base","actions":[{"name":"camera","type":"camera_record","params":{"type":"standard"}}]}';
  switch (part) {
    case "text": return {text: text.replace('"base"', `"${tag}"`)};
    case "pending-number": return {text, pending:{"/amount":{kind:"number",text:tag === "B" ? "-" : "+"}}};
    case "pending-json": return {text, pending:{"/extra":{kind:"json",text:tag === "B" ? "{" : "["}}};
    case "actionVariants": return {text,actionVariants:{"0":[{type:"camera_take_photo",fields:{tag},pending:{}}]}};
    case "parameterVariants": return {text,parameterVariants:{"0":[{paramsText:`{"params":{"type":"bitrate","tag":"${tag}"}}`,pending:{}}]}};
    case "objectVariants": return {text,objectVariants:{"0":{"/exposure":{discriminator:"mode",variants:[{valueText:`{"value":{"mode":"manual","tag":"${tag}"}}`,pending:{}}]}}}};
    case "automaticPreviews": return {text,automaticPreviews:{namespace:tag,next:1,intent:tag === "B" ? "unset" : "enabled",actions:[{id:tag}]}};
  }
}
it.each(parts)("只修改并恢复%s，保存事实按完整内容成立而非编辑代次", async part => {
  vi.useFakeTimers(); let writes = 0;
  const base = {...draft(1,"base"),content:input(part,"B")};
  const session = new DraftSession(base,{save:async () => {writes++; throw Error("不应写入");},read:async () => base},() => {});
  session.edit(input(part,"L")); session.schedule();
  expect(session.saved).toBe(false); expect(session.needsLeaveWarning).toBe(true);
  session.edit(base.content); session.schedule();
  expect(session.version).toBe(2); expect(session.confirmedVersion).toBe(0);
  expect(session.saved).toBe(true); expect(session.saveStatus).toBe("saved"); expect(session.needsLeaveWarning).toBe(false);
  await vi.advanceTimersByTimeAsync(450); await session.flush(); expect(writes).toBe(0);
});
it("Number投影相同但token不同仍是未保存输入并在450毫秒尝试写入", async () => {
  vi.useFakeTimers(); let writes = 0;
  const base = {...draft(1,"base"),content:{text:'{"name":"base","n":1.0000000000000001,"actions":[]}'}};
  const session = new DraftSession(base,{save:async () => {writes++; throw new HttpError("rejected","拒绝",[],400);},read:async () => base},() => {});
  session.edit({text:'{"name":"base","n":1.00000000000000001,"actions":[]}'}); session.schedule();
  expect(Number("1.0000000000000001")).toBe(Number("1.00000000000000001"));
  expect(session.saved).toBe(false); expect(session.needsLeaveWarning).toBe(true);
  await vi.advanceTimersByTimeAsync(449); expect(writes).toBe(0);
  await vi.advanceTimersByTimeAsync(1); expect(writes).toBe(1);
});
it.each(["unavailable","invalid"] as const)("恢复B不授权%s观察接纳或保存", kind => {
  const base = draft(1,"base"); const session = new DraftSession(base,{save:async () => base,read:async () => base},() => {});
  session.edit(draft(1,"temporary").content); session.edit(base.content);
  session.observeTarget(kind === "unavailable" ? {kind,message:"offline"} : {kind:"available",draft:{...draft(2,"remote"),id:"other"}});
  expect(session.saved).toBe(false); expect(session.content).toEqual(base.content); expect(session.revision).toBe(1);
  expect(session.saveStatus).toBe(kind);
});
it("固定unknown期间恢复B仍保留原写入核实责任，显式核实只读零PUT", async () => {
  const base = draft(1,"base"); let writes = 0, offline = true, reject!: (error:Error) => void;
  const session = new DraftSession(base,{save:async () => {writes++; return new Promise<Draft>((_,no) => {reject=no;});},read:async () => {if(offline) throw Error("offline"); return base;}},() => {});
  session.edit(draft(1,"temporary").content); const pending=session.flush(); session.edit(base.content);
  reject(Error("reply lost")); await expect(pending).rejects.toThrow();
  expect(session.saved).toBe(false); expect(session.needsLeaveWarning).toBe(true); expect(writes).toBe(1);
  offline=false; await session.verifySaveResult(); expect(writes).toBe(1);
  expect(session.saved).toBe(true); expect(session.needsLeaveWarning).toBe(false);
});

// 可靠普通拒绝不能把现已恢复的L=B强行变成三方冲突。
it("普通保存被可靠拒绝时本页已恢复B，按清洁分类接纳完整新R", async () => {
  const base = draft(1, "base"), remote = draft(2, "remote");
  let reject!: (error: Error) => void;
  const session = new DraftSession(base, {save: async () => new Promise<Draft>((_, no) => {reject = no;}), read: async () => remote}, () => {});
  session.edit(draft(1, "temporary").content);
  const pending = session.flush();
  session.edit(base.content);
  reject(new HttpError("draft_changed", "版本已更新", [], 409));
  await expect(pending).rejects.toThrow();
  expect(session.content, JSON.stringify({decision:session.decision,error:session.error,diagnostic:session.saveDiagnostic})).toEqual(remote.content); expect(session.revision).toBe(2);
  expect(session.conflict).toBeUndefined(); expect(session.saved).toBe(true);
});
