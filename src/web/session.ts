import type { Draft, DraftContent } from "../server/models";
export interface DraftTransport {
  save(id: string, revision: number, content: DraftContent): Promise<Draft>;
  read(id: string): Promise<Draft>;
}
const same = (a: DraftContent, b: DraftContent) =>
  a.text === b.text &&
  JSON.stringify(a.pending ?? {}) === JSON.stringify(b.pending ?? {});
export class DraftSession {
  content: DraftContent;
  revision: number;
  version = 0;
  confirmedVersion = 0;
  error = "";
  saving = false;
  exportedRequestId?: string;
  private running?: Promise<void>;
  private timer?: ReturnType<typeof setTimeout>;
  constructor(
    readonly draft: Draft,
    private transport: DraftTransport,
    private changed: () => void,
  ) {
    this.content = structuredClone(draft.content);
    this.revision = draft.revision;
    this.exportedRequestId = draft.exportedRequestId;
  }
  get saved() {
    return this.version === this.confirmedVersion && !this.error;
  }
  edit(content: DraftContent) {
    if (this.exportedRequestId) throw new Error("已导出请求只读");
    this.content = structuredClone(content);
    this.version++;
    this.error = "";
    this.changed();
  }
  schedule() {
    clearTimeout(this.timer);
    this.timer = setTimeout(() => {
      void this.flush().catch(() => {});
    }, 450);
  }
  async flush(): Promise<void> {
    clearTimeout(this.timer);
    if (this.running) return this.running;
    this.running = this.saveLoop();
    try {
      await this.running;
    } finally {
      this.running = undefined;
    }
  }
  private async saveLoop() {
    this.error = "";
    this.saving = true;
    this.changed();
    try {
      while (this.confirmedVersion !== this.version) {
        if (this.exportedRequestId)
          throw new Error("草稿已导出，请打开计划记录");
        const version = this.version,
          content = structuredClone(this.content),
          revision = this.revision;
        let saved: Draft;
        try {
          saved = await this.transport.save(this.draft.id, revision, content);
        } catch (error) {
          let actual: Draft;
          try {
            actual = await this.transport.read(this.draft.id);
          } catch {
            throw new Error(
              "保存结果尚未确认；请恢复连接后重试，当前输入已保留。",
            );
          }
          if (actual.exportedRequestId) {
            this.exportedRequestId = actual.exportedRequestId;
            throw new Error("草稿已导出，请打开计划记录");
          }
          if (actual.revision > revision && same(actual.content, content))
            saved = actual;
          else throw error;
        }
        this.revision = saved.revision;
        this.confirmedVersion = version;
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
  accept(draft: Draft) {
    this.content = structuredClone(draft.content);
    this.revision = draft.revision;
    this.version++;
    this.confirmedVersion = this.version;
    this.exportedRequestId = draft.exportedRequestId;
    this.error = "";
    this.changed();
  }
}
