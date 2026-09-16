import type { Draft } from "../server/models";
import { HttpError } from "./api";
import { parseDraft } from "./editing";
import { sameContent } from "./session";
type Phase =
  | "new"
  | "creating"
  | "creation_unknown"
  | "target"
  | "appending"
  | "unknown"
  | "not_appended"
  | "conflict"
  | "done";
export interface FollowTransport {
  create(): Promise<Draft>;
  prepare(id: string): Promise<Draft>;
  append(
    id: string,
    revision: number,
    action: Record<string, unknown>,
  ): Promise<Draft>;
  read(id: string): Promise<Draft>;
}
export function classifyAppend(
  baseline: Draft,
  action: Record<string, unknown>,
  actual: Draft,
): "appended" | "baseline" | "conflict" {
  if (actual.id !== baseline.id || actual.exportedRequestId) return "conflict";
  if (
    actual.revision === baseline.revision &&
    sameContent(actual.content, baseline.content)
  )
    return "baseline";
  try {
    const before = parseDraft(baseline.content),
      after = parseDraft(actual.content),
      last = after.actions.at(-1);
    const { name: requestedName, ...requested } = action,
      { name: actualName, ...appended } = last ?? {};
    if (
      actual.revision === baseline.revision + 1 &&
      after.actions.length === before.actions.length + 1 &&
      typeof actualName === "string" &&
      JSON.stringify({ ...after, actions: after.actions.slice(0, -1) }) ===
        JSON.stringify(before) &&
      JSON.stringify(appended) === JSON.stringify(requested) &&
      sameContent(
        { ...actual.content, text: baseline.content.text },
        baseline.content,
      )
    )
      return "appended";
  } catch {
    /* 无法解析的实际内容不能归因于本次追加。 */
  }
  return "conflict";
}
export class FollowOperation {
  phase: Phase;
  targetId?: string;
  baseline?: Draft;
  actual?: Draft;
  result?: Draft;
  error = "";
  readonly action: Record<string, unknown>;
  private running = false;
  constructor(
    destination: string,
    action: Record<string, unknown>,
    private transport: FollowTransport,
  ) {
    this.phase = destination === "new" ? "new" : "target";
    this.targetId = destination === "new" ? undefined : destination;
    this.action = structuredClone(action);
  }
  get inProgress() {
    return this.running;
  }
  private async verify() {
    try {
      this.actual = structuredClone(await this.transport.read(this.targetId!));
    } catch (error) {
      this.phase = "unknown";
      this.error = `追加结果尚未确认，请恢复连接后重新核实。${message(error)}`;
      return;
    }
    const verdict = classifyAppend(this.baseline!, this.action, this.actual);
    if (verdict === "appended") {
      this.result = this.actual;
      this.phase = "done";
      this.error = "";
    } else if (verdict === "baseline") {
      this.phase = "not_appended";
      this.error = "实际记录确认尚未追加；可以对同一目标重试。";
    } else {
      this.phase = "conflict";
      this.error =
        "实际记录与基线及预期追加均不一致；请查看目标和保留的操作内容。";
    }
  }
  async advance(): Promise<void> {
    if (this.running) return;
    this.running = true;
    try {
      await this.step();
    } finally {
      this.running = false;
    }
  }
  private async step(): Promise<void> {
    if (this.phase === "done" || this.phase === "creation_unknown") return;
    if (this.phase === "unknown" || this.phase === "conflict") {
      await this.verify();
      return;
    }
    if (this.phase === "not_appended") {
      await this.verify();
      if (this.phase !== "not_appended") return;
    }
    if (this.phase === "new") {
      this.phase = "creating";
      this.error = "";
      try {
        const target = await this.transport.create();
        this.targetId = target.id;
        this.phase = "target";
      } catch (error) {
        this.phase =
          error instanceof HttpError &&
          error.status >= 400 &&
          error.status < 500
            ? "new"
            : "creation_unknown";
        this.error =
          this.phase === "creation_unknown"
            ? `创建结果尚未确认。请返回草稿列表核对实际记录；本次流程不会再次创建。${message(error)}`
            : message(error);
        return;
      }
    }
    if (this.phase === "target") {
      try {
        const baseline = await this.transport.prepare(this.targetId!);
        if (baseline.id !== this.targetId || baseline.exportedRequestId)
          throw Error("目标草稿不可追加");
        this.baseline = structuredClone(baseline);
      } catch (error) {
        this.error = `目标草稿已确定，准备基线未完成。${message(error)}`;
        return;
      }
    }
    this.phase = "appending";
    this.error = "";
    try {
      const actual = await this.transport.append(
        this.targetId!,
        this.baseline!.revision,
        this.action,
      );
      if (classifyAppend(this.baseline!, this.action, actual) !== "appended")
        throw Error("追加回执与本次动作不一致");
      this.actual = actual;
      this.result = actual;
      this.phase = "done";
    } catch {
      await this.verify();
    }
  }
}
function message(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}
