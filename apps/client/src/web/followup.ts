import type { Draft, DraftContent } from "../server/models";
import { HttpError } from "./api";
import { cloneClientJson } from "../shared/json";
import {
  appendContentAction,
  coordinatePreviews,
  sameContent,
  type CapabilityState,
} from "../shared/automatic-previews";
import { parseDraft } from "./editing";
type Phase =
  | "new"
  | "creating"
  | "creation_unknown"
  | "target"
  | "appending"
  | "unknown"
  | "not_appended"
  | "conflict"
  | "acceptance_failed"
  | "done";
export interface FollowTransport {
  capabilities?(): CapabilityState | undefined;
  create(): Promise<Draft>;
  prepare(id: string): Promise<Draft>;
  append(
    id: string,
    revision: number,
    action: Record<string, unknown>,
    expected?: AppendExpectation,
  ): Promise<Draft>;
  read(id: string): Promise<Draft>;
}
export interface AppendExpectation {
  content: DraftContent;
  capabilityVersion?: string;
}
/** 客户端 attempt 的明确追加关系，不属于持久化资料或公共协议。 */
export interface AppendAttempt {
  origin: Draft;
  baselineChanges: Array<{ actual: Draft; capabilities?: CapabilityState }>;
  baseline: Draft;
  action: Record<string, unknown>;
  index: number;
  appended: DraftContent;
  expected: AppendExpectation;
  preparedCapabilities?: CapabilityState;
}
export interface VerifiedAppend extends AppendAttempt {
  actual: Draft;
  capabilities?: CapabilityState;
}
export function classifyAppend(
  baseline: Draft,
  action: Record<string, unknown>,
  actual: Draft,
  expected?: AppendExpectation,
  capabilities?: CapabilityState,
): "appended" | "baseline" | "conflict" {
  if (actual.id !== baseline.id || actual.exportedRequestId) return "conflict";
  if (
    actual.revision === baseline.revision &&
    sameContent(actual.content, baseline.content)
  )
    return "baseline";
  if (
    actual.revision > baseline.revision &&
    capabilities &&
    (!actual.lastWrite || actual.lastWrite.revision <= baseline.revision) &&
    sameContent(
      coordinatePreviews(baseline.content, capabilities).content,
      actual.content,
    )
  )
    return "baseline";
  try {
    const content =
      expected?.content ?? appendContentAction(baseline.content, action, true);
    if (
      actual.revision === baseline.revision + 1 &&
      sameContent(actual.content, content)
    )
      return "appended";
    const write = actual.lastWrite;
    if (
      write?.revision === baseline.revision + 1 &&
      actual.revision >= write.revision &&
      sameContent(write.input, content) &&
      (sameContent(write.content, actual.content) ||
        (!!capabilities &&
          sameContent(
            coordinatePreviews(write.content, capabilities).content,
            actual.content,
          )))
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
  expected?: AppendExpectation;
  private fixedAttempt?: AppendAttempt;
  private verified?: VerifiedAppend;
  private origin?: Draft;
  private baselineChanges: AppendAttempt["baselineChanges"] = [];
  private baselineCapabilities?: CapabilityState;
  private running = false;
  constructor(
    destination: string,
    action: Record<string, unknown>,
    private transport: FollowTransport,
  ) {
    this.phase = destination === "new" ? "new" : "target";
    this.targetId = destination === "new" ? undefined : destination;
    this.action = cloneClientJson(action);
  }
  get inProgress() {
    return this.running;
  }
  get attempt() {
    return this.fixedAttempt && cloneClientJson(this.fixedAttempt);
  }
  get transition() {
    return this.verified && cloneClientJson(this.verified);
  }
  acceptanceFailed(error: unknown) {
    if (!this.verified) throw Error("没有可靠追加结果可接纳");
    this.phase = "acceptance_failed";
    this.error = `动作已经追加，界面接纳未完成；仅重试接纳实际结果。${message(error)}`;
  }
  acceptanceSucceeded() {
    this.phase = "done";
    this.error = "";
  }
  private prepareExpectation() {
    const capabilities = this.transport.capabilities?.();
    const appended = appendContentAction(
      this.baseline!.content,
      this.action,
      true,
    );
    this.expected = {
      content: capabilities
        ? coordinatePreviews(appended, capabilities).content
        : appended,
      capabilityVersion: capabilities?.version,
    };
    this.fixedAttempt = cloneClientJson({
      origin: this.origin ?? this.baseline!,
      baselineChanges: this.baselineChanges,
      baseline: this.baseline!,
      action: this.action,
      index: parseDraft(this.baseline!.content).actions.length,
      appended,
      expected: this.expected,
      preparedCapabilities: capabilities,
    });
  }
  private confirm(actual: Draft, capabilities?: CapabilityState) {
    this.actual = cloneClientJson(actual);
    this.result = cloneClientJson(actual);
    this.verified = cloneClientJson({
      ...this.fixedAttempt!,
      actual,
      capabilities,
    });
    this.phase = "done";
    this.error = "";
  }
  private async verify() {
    try {
      this.actual = cloneClientJson(await this.transport.read(this.targetId!));
    } catch (error) {
      this.phase = "unknown";
      this.error = `追加结果尚未确认，请恢复连接后重新核实。${message(error)}`;
      return;
    }
    const capabilities = this.transport.capabilities?.();
    const verdict = classifyAppend(
      this.fixedAttempt!.baseline,
      this.fixedAttempt!.action,
      this.actual,
      this.fixedAttempt!.expected,
      capabilities,
    );
    if (verdict === "appended") {
      this.confirm(this.actual, capabilities);
    } else if (verdict === "baseline") {
      this.baselineCapabilities = capabilities && cloneClientJson(capabilities);
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
    if (
      this.phase === "done" ||
      this.phase === "acceptance_failed" ||
      this.phase === "creation_unknown"
    )
      return;
    if (this.phase === "unknown" || this.phase === "conflict") {
      await this.verify();
      return;
    }
    if (this.phase === "not_appended") {
      await this.verify();
      if (this.phase !== "not_appended") return;
      const previous =
        this.baselineChanges.at(-1)?.actual ?? this.fixedAttempt!.baseline;
      if (
        previous.revision !== this.actual!.revision ||
        !sameContent(previous.content, this.actual!.content)
      )
        this.baselineChanges.push(
          cloneClientJson({
            actual: this.actual!,
            capabilities: this.baselineCapabilities,
          }),
        );
      this.baseline = cloneClientJson(this.actual!);
      try {
        this.prepareExpectation();
      } catch (error) {
        this.error = `已核实尚未追加，新基线的预期准备未完成。${message(error)}`;
        return;
      }
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
        this.baseline = cloneClientJson(baseline);
        this.origin = cloneClientJson(baseline);
        this.prepareExpectation();
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
        this.fixedAttempt!.baseline.revision,
        cloneClientJson(this.fixedAttempt!.action),
        cloneClientJson(this.fixedAttempt!.expected),
      );
      const capabilities = this.transport.capabilities?.();
      if (
        classifyAppend(
          this.fixedAttempt!.baseline,
          this.fixedAttempt!.action,
          actual,
          this.fixedAttempt!.expected,
          capabilities,
        ) !== "appended"
      )
        throw Error("追加回执与本次动作不一致");
      this.confirm(actual, capabilities);
    } catch {
      await this.verify();
    }
  }
}
function message(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}
