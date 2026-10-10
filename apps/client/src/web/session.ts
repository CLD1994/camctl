import { cloneClientJson, stringifyJson } from "../shared/json";
import {
  sameContent,
  coordinatePreviews,
  type CapabilityState,
  appendContentAction,
} from "../shared/automatic-previews";
import type { Draft, DraftContent } from "../server/models";
import { classifyAppend, type VerifiedAppend } from "./followup";
import { parseDraft } from "./editing";
export interface DraftTransport {
  save(id: string, revision: number, content: DraftContent): Promise<Draft>;
  read(id: string): Promise<Draft>;
}
export { sameContent } from "../shared/automatic-previews";
type ExportState = "editable" | "exporting" | "unknown" | "exported";
interface PendingWrite {
  revision: number;
  content: DraftContent;
  version: number;
  baseline: DraftContent;
}
export class DraftSession {
  content: DraftContent;
  revision: number;
  version = 0;
  confirmedVersion = 0;
  error = "";
  saving = false;
  exportedRequestId?: string;
  exportState: ExportState = "editable";
  exportToken = 0;
  recoveryContent?: DraftContent;
  conflict?: Draft;
  appendLocked = false;
  deletionState: "idle" | "deleting" | "unknown" | "deleted" = "idle";
  private baseline: DraftContent;
  private pendingWrite?: PendingWrite;
  private recoveredVersion?: number;
  private exportSnapshot?: { revision: number; content: DraftContent };
  private running?: Promise<void>;
  private timer?: ReturnType<typeof setTimeout>;
  private deletionFailure = "";
  private capabilities?: CapabilityState;
  private appendView?: (transition: VerifiedAppend) => () => void;
  private acceptedAppend?: VerifiedAppend;
  constructor(
    readonly draft: Draft,
    private transport: DraftTransport,
    private changed: () => void,
  ) {
    this.content = cloneClientJson(draft.content);
    this.baseline = cloneClientJson(draft.content);
    this.revision = draft.revision;
    this.exportedRequestId = draft.exportedRequestId;
    if (draft.exportedRequestId) this.exportState = "exported";
  }
  get editable() {
    return (
      this.exportState === "editable" &&
      !this.appendLocked &&
      this.deletionState === "idle"
    );
  }
  async delete(
    remove: (revision: number) => Promise<void>,
    read: () => Promise<Draft | undefined>,
  ) {
    await this.flush();
    if (!this.editable || !this.saved) throw new Error("请先核实草稿保存状态");
    this.deletionState = "deleting";
    clearTimeout(this.timer);
    this.changed();
    try {
      await remove(this.revision);
      this.deletionState = "deleted";
      this.changed();
    } catch (error) {
      this.deletionFailure =
        error instanceof Error ? error.message : String(error);
      this.deletionState = "unknown";
      await this.checkDeletion(read);
    }
  }
  async checkDeletion(read: () => Promise<Draft | undefined>) {
    if (this.deletionState !== "unknown")
      throw new Error("没有待核实的删除操作");
    let actual: Draft | undefined;
    try {
      actual = await read();
    } catch {
      this.error = "删除结果尚未确认，请恢复连接后重新核实。";
      this.changed();
      throw new Error(this.error);
    }
    this.deletionState = actual ? "idle" : "deleted";
    this.error = "";
    if (actual) {
      this.observe(actual);
      if (!actual.exportedRequestId && actual.revision !== this.revision)
        this.conflict = cloneClientJson(actual);
      this.changed();
      throw new Error(
        `草稿仍存在，删除未完成；请刷新页面核对后重试。${this.deletionFailure}`,
      );
    }
    this.changed();
  }
  lockAppend() {
    clearTimeout(this.timer);
    this.appendLocked = true;
    this.changed();
  }
  unlockAppend() {
    this.appendLocked = false;
    this.changed();
  }
  get saved() {
    if (this.exportedRequestId) return !this.recoveryContent;
    return (
      this.version === this.confirmedVersion &&
      !this.error &&
      !this.pendingWrite
    );
  }
  edit(content: DraftContent) {
    this.commitEdit(this.prepareEdit(content));
  }
  prepareEdit(content: DraftContent) {
    if (!this.editable) throw new Error("此草稿的导出状态尚未确认或已经只读");
    const actual = cloneClientJson(
      this.capabilities
        ? coordinatePreviews(content, this.capabilities).content
        : content,
    );
    return { content: actual, version: this.version };
  }
  commitEdit(prepared: { content: DraftContent; version: number }) {
    if (!this.editable || prepared.version !== this.version)
      throw Error("草稿状态已改变，不能接纳本次编辑");
    this.content = cloneClientJson(prepared.content);
    this.version++;
    this.error = "";
    this.changed();
  }
  schedule() {
    clearTimeout(this.timer);
    if (this.editable)
      this.timer = setTimeout(() => {
        void this.flush().catch(() => {});
      }, 450);
  }
  async flush(): Promise<void> {
    clearTimeout(this.timer);
    if (!this.editable) throw new Error("请先核实草稿的导出状态");
    if (this.running) return this.running;
    this.running = this.saveLoop();
    try {
      await this.running;
    } finally {
      this.running = undefined;
    }
  }
  private async verifyWrite(): Promise<"saved" | "not_saved"> {
    const pending = this.pendingWrite!;
    let actual: Draft;
    try {
      actual = await this.transport.read(this.draft.id);
    } catch {
      throw new Error("保存结果尚未确认；请恢复连接后重试，当前输入已保留。");
    }
    if (actual.exportedRequestId) {
      this.observe(actual);
      throw new Error("草稿已导出；未保存的输入已保留为可恢复内容");
    }
    if (this.matchesWrite(actual, pending)) {
      this.acceptWrite(actual, pending);
      return "saved";
    }
    if (
      actual.revision === pending.revision &&
      sameContent(actual.content, pending.baseline)
    ) {
      this.pendingWrite = undefined;
      this.conflict = undefined;
      return "not_saved";
    }
    this.conflict = cloneClientJson(actual);
    throw new Error(
      "保存记录与本次已知写入不一致；当前输入与后端记录均已保留，请核对冲突。",
    );
  }
  private matchesWrite(actual: Draft, pending: PendingWrite) {
    if (actual.id !== this.draft.id || actual.exportedRequestId) return false;
    if (
      actual.revision === pending.revision + 1 &&
      sameContent(actual.content, pending.content)
    )
      return true;
    const evidence = actual.lastWrite;
    return (
      !!evidence &&
      evidence.revision === pending.revision + 1 &&
      actual.revision >= evidence.revision &&
      sameContent(evidence.input, pending.content) &&
      (sameContent(evidence.content, actual.content) ||
        (!!this.capabilities &&
          sameContent(
            coordinatePreviews(evidence.content, this.capabilities).content,
            actual.content,
          )))
    );
  }
  private acceptWrite(actual: Draft, pending: PendingWrite) {
    const unchanged = this.version === pending.version;
    this.revision = actual.revision;
    this.baseline = cloneClientJson(actual.content);
    if (unchanged) this.content = cloneClientJson(actual.content);
    else if (this.capabilities)
      this.content = coordinatePreviews(
        this.content,
        this.capabilities,
      ).content;
    this.confirmedVersion = sameContent(this.content, actual.content)
      ? this.version
      : pending.version;
    this.pendingWrite = undefined;
    this.conflict = undefined;
  }
  private async saveLoop() {
    this.error = "";
    this.saving = true;
    this.changed();
    try {
      if (this.pendingWrite) await this.verifyWrite();
      while (this.confirmedVersion !== this.version) {
        if (!this.editable) throw new Error("草稿不可继续保存，请核实导出结果");
        const pending: PendingWrite = {
          revision: this.revision,
          content: cloneClientJson(this.content),
          version: this.version,
          baseline: cloneClientJson(this.baseline),
        };
        this.pendingWrite = pending;
        try {
          const saved = await this.transport.save(
            this.draft.id,
            pending.revision,
            pending.content,
          );
          if (!this.matchesWrite(saved, pending))
            throw new Error("保存回执与提交内容不一致");
          this.acceptWrite(saved, pending);
        } catch (error) {
          const result = await this.verifyWrite();
          if (result === "not_saved") throw error;
        }
        this.changed();
      }
    } catch (error) {
      this.error = error instanceof Error ? error.message : String(error);
      throw error;
    } finally {
      this.saving = false;
      this.changed();
    }
  }
  beginExport(capabilities = this.capabilities) {
    if (!this.editable || !this.saved) throw new Error("请先完成草稿保存");
    clearTimeout(this.timer);
    this.exportState = "exporting";
    this.exportToken++;
    this.exportSnapshot = {
      revision: this.revision,
      content: cloneClientJson(this.content),
    };
    this.changed();
    return {
      ...cloneClientJson(this.exportSnapshot),
      capabilityVersion: capabilities?.version,
    };
  }
  exportUnknown() {
    if (this.exportedRequestId) return;
    this.exportState = "unknown";
    this.error = "导出结果尚未确认；原输入已保留，请重新核实后继续。";
    this.changed();
  }
  exportFailed() {
    if (this.exportedRequestId) return;
    this.exportState = "editable";
    this.exportSnapshot = undefined;
    this.error = "";
    this.changed();
  }
  confirmExport(id: string) {
    this.exportedRequestId = id;
    this.exportState = "exported";
    this.error = "";
    clearTimeout(this.timer);
    this.changed();
  }
  completeRecovery() {
    this.recoveredVersion = this.version;
    this.recoveryContent = undefined;
    this.changed();
  }
  observe(
    actual: Draft,
    unexportedToken?: number,
    capabilities?: CapabilityState,
  ) {
    if (actual.id !== this.draft.id) return;
    if (actual.revision < this.revision) return;
    if (capabilities) this.capabilities = capabilities;
    if (actual.exportedRequestId) {
      if (
        !sameContent(this.content, actual.content) &&
        this.recoveredVersion !== this.version
      )
        this.recoveryContent = cloneClientJson(this.content);
      else this.confirmedVersion = this.version;
      this.pendingWrite = undefined;
      this.confirmExport(actual.exportedRequestId);
      return;
    }
    if (
      this.exportState === "unknown" &&
      unexportedToken === this.exportToken
    ) {
      const expected = this.exportSnapshot;
      if (
        expected &&
        actual.revision === expected.revision &&
        sameContent(actual.content, expected.content)
      )
        this.exportFailed();
      else if (
        expected &&
        capabilities &&
        actual.revision > expected.revision &&
        sameContent(
          coordinatePreviews(expected.content, capabilities).content,
          actual.content,
        )
      ) {
        this.exportFailed();
        this.observe(actual, undefined, capabilities);
      } else {
        this.conflict = cloneClientJson(actual);
        this.error = capabilities
          ? "导出结果与已知草稿基线不一致，请核对后端记录。"
          : "导出核实还需要同次能力与草稿观察，当前输入已保留。";
        this.changed();
      }
      return;
    }
    if (!this.editable || this.saving || this.pendingWrite) return;
    if (
      actual.revision === this.revision &&
      sameContent(actual.content, this.baseline)
    ) {
      this.coordinate(capabilities);
      return;
    }
    if (
      actual.revision > this.revision &&
      capabilities &&
      sameContent(
        coordinatePreviews(this.baseline, capabilities).content,
        actual.content,
      )
    ) {
      const local = cloneClientJson(this.content);
      const clean = sameContent(local, this.baseline);
      this.revision = actual.revision;
      this.baseline = cloneClientJson(actual.content);
      this.content = clean
        ? cloneClientJson(actual.content)
        : coordinatePreviews(local, capabilities).content;
      this.version++;
      if (sameContent(this.content, this.baseline))
        this.confirmedVersion = this.version;
      this.error = "";
      this.conflict = undefined;
      if (!this.saved) this.schedule();
      this.changed();
      return;
    }
    this.conflict = cloneClientJson(actual);
    this.error = "后端草稿与已确认基线不一致；当前输入和后端内容均已保留。";
    this.changed();
  }
  coordinate(capabilities = this.capabilities) {
    if (capabilities) this.capabilities = capabilities;
    if (!capabilities || !this.editable || this.saving || this.pendingWrite)
      return;
    const next = coordinatePreviews(this.content, capabilities).content;
    if (!sameContent(next, this.content)) {
      this.edit(next);
      this.schedule();
    }
  }
  async checkExport() {
    const token = this.exportState === "unknown" ? this.exportToken : undefined;
    const actual = await this.transport.read(this.draft.id);
    this.observe(actual, token);
    return actual;
  }
  accept(draft: Draft) {
    this.content = cloneClientJson(draft.content);
    this.baseline = cloneClientJson(draft.content);
    this.revision = draft.revision;
    this.version++;
    this.confirmedVersion = this.version;
    this.exportedRequestId = draft.exportedRequestId;
    this.exportState = draft.exportedRequestId ? "exported" : "editable";
    this.pendingWrite = undefined;
    this.error = "";
    this.conflict = undefined;
    this.changed();
  }
  bindAppendView(prepare: (transition: VerifiedAppend) => () => void) {
    this.appendView = prepare;
    return () => {
      if (this.appendView === prepare) this.appendView = undefined;
    };
  }
  acceptAppend(input: VerifiedAppend) {
    const transition = cloneClientJson(input),
      { origin, baseline, actual } = transition;
    if (
      this.acceptedAppend &&
      stringifyJson(this.acceptedAppend) === stringifyJson(transition)
    )
      return;
    if (
      origin.id !== this.draft.id ||
      baseline.id !== this.draft.id ||
      actual.id !== this.draft.id ||
      origin.revision !== this.revision ||
      !sameContent(origin.content, this.content) ||
      !sameContent(origin.content, this.baseline)
    )
      throw Error("追加目标或完整基线版本与当前草稿不一致");
    if (
      this.exportState !== "editable" ||
      this.deletionState !== "idle" ||
      this.saving ||
      this.pendingWrite ||
      !this.saved
    )
      throw Error("当前草稿状态不能接纳追加结果");
    let prior = origin;
    for (const change of transition.baselineChanges) {
      if (
        change.actual.revision <= prior.revision ||
        classifyAppend(
          prior,
          transition.action,
          change.actual,
          undefined,
          change.capabilities,
        ) !== "baseline"
      )
        throw Error("追加准备的基线派生版本链不可靠");
      prior = change.actual;
    }
    if (
      prior.revision !== baseline.revision ||
      prior.id !== baseline.id ||
      !sameContent(prior.content, baseline.content)
    )
      throw Error("追加准备的基线版本链未闭合");
    if (
      transition.index !== parseDraft(baseline.content).actions.length ||
      !sameContent(
        appendContentAction(baseline.content, transition.action, true),
        transition.appended,
      ) ||
      transition.expected.capabilityVersion !==
        transition.preparedCapabilities?.version ||
      !sameContent(
        transition.preparedCapabilities
          ? coordinatePreviews(
              transition.appended,
              transition.preparedCapabilities,
            ).content
          : transition.appended,
        transition.expected.content,
      ) ||
      classifyAppend(
        baseline,
        transition.action,
        actual,
        transition.expected,
        transition.capabilities,
      ) !== "appended"
    )
      throw Error("实际记录没有匹配固定追加转换");
    const commitView = this.appendView?.(transition);
    // 所有校验和副本计算均已结束；以下提交不调用可拒绝的映射检查。
    this.content = cloneClientJson(actual.content);
    this.baseline = cloneClientJson(actual.content);
    this.revision = actual.revision;
    this.version++;
    this.confirmedVersion = this.version;
    this.error = "";
    this.conflict = undefined;
    this.acceptedAppend = transition;
    commitView?.();
    this.appendLocked = false;
    this.changed();
  }
}

/** 调用方在此期间阻止新的编辑、导出、追加和删除，再发起能力重载。 */
export async function prepareSessionsForReload(
  sessions: DraftSession[],
  observe: () => Promise<void>,
) {
  try {
    if (sessions.some((s) => s.exportState === "unknown")) await observe();
    for (const session of sessions) {
      if (
        session.deletionState === "deleted" ||
        session.exportState === "exported"
      )
        continue;
      if (!session.editable)
        throw Error(`草稿 ${session.draft.id} 的操作结果尚未核实`);
      await session.flush();
      if (!session.saved) throw Error(`草稿 ${session.draft.id} 尚未可靠保存`);
    }
  } catch (error) {
    throw Error(
      `草稿保存或操作核实未完成，尚未开始能力重载：${(error as Error).message}`,
    );
  }
}
