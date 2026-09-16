import type { Draft, DraftContent } from "../server/models";
export interface DraftTransport {
  save(id: string, revision: number, content: DraftContent): Promise<Draft>;
  read(id: string): Promise<Draft>;
}
export const sameContent = (a: DraftContent, b: DraftContent) =>
  a.text === b.text &&
  JSON.stringify(a.pending ?? {}) === JSON.stringify(b.pending ?? {});
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
  constructor(
    readonly draft: Draft,
    private transport: DraftTransport,
    private changed: () => void,
  ) {
    this.content = structuredClone(draft.content);
    this.baseline = structuredClone(draft.content);
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
        this.conflict = structuredClone(actual);
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
    if (!this.editable) throw new Error("此草稿的导出状态尚未确认或已经只读");
    this.content = structuredClone(content);
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
    if (
      actual.revision === pending.revision + 1 &&
      sameContent(actual.content, pending.content)
    ) {
      this.revision = actual.revision;
      this.baseline = structuredClone(actual.content);
      this.confirmedVersion = pending.version;
      this.pendingWrite = undefined;
      this.conflict = undefined;
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
    this.conflict = structuredClone(actual);
    throw new Error(
      "保存记录与本次已知写入不一致；当前输入与后端记录均已保留，请核对冲突。",
    );
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
          content: structuredClone(this.content),
          version: this.version,
          baseline: structuredClone(this.baseline),
        };
        this.pendingWrite = pending;
        try {
          const saved = await this.transport.save(
            this.draft.id,
            pending.revision,
            pending.content,
          );
          if (
            saved.revision !== pending.revision + 1 ||
            !sameContent(saved.content, pending.content)
          )
            throw new Error("保存回执与提交内容不一致");
          this.revision = saved.revision;
          this.baseline = structuredClone(saved.content);
          this.confirmedVersion = pending.version;
          this.pendingWrite = undefined;
          this.conflict = undefined;
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
  beginExport() {
    if (!this.editable || !this.saved) throw new Error("请先完成草稿保存");
    clearTimeout(this.timer);
    this.exportState = "exporting";
    this.exportToken++;
    this.exportSnapshot = {
      revision: this.revision,
      content: structuredClone(this.content),
    };
    this.changed();
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
  observe(actual: Draft, unexportedToken?: number) {
    if (actual.id !== this.draft.id) return;
    if (actual.exportedRequestId) {
      if (
        !sameContent(this.content, actual.content) &&
        this.recoveredVersion !== this.version
      )
        this.recoveryContent = structuredClone(this.content);
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
      else {
        this.conflict = structuredClone(actual);
        this.error = "导出结果与已知草稿基线不一致，请核对后端记录。";
        this.changed();
      }
    }
  }
  async checkExport() {
    const token = this.exportState === "unknown" ? this.exportToken : undefined;
    const actual = await this.transport.read(this.draft.id);
    this.observe(actual, token);
    return actual;
  }
  accept(draft: Draft) {
    this.content = structuredClone(draft.content);
    this.baseline = structuredClone(draft.content);
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
}
