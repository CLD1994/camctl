import type { DraftContent, DraftCreationResult } from "../server/models";
import { cloneClientJson, stringifyJson } from "../shared/json";
import { sameContent } from "../shared/automatic-previews";
import { checkDraftContent } from "../shared/draft-content";
import { requireDraftCreationResult } from "../shared/draft-creation";
import { HttpError } from "../shared/http-error";
export interface DraftCopyTransport {
  create(attemptId: string, input: DraftContent): Promise<DraftCreationResult>;
  read(attemptId: string): Promise<DraftCreationResult>;
}
export type DraftCopyPhase =
  | "new"
  | "submitting"
  | "unknown"
  | "not_created"
  | "created_untransferred"
  | "invalid"
  | "transferred";
export function requireMatchingCreationResult(
  result: Extract<DraftCreationResult, { kind: "created" }>,
  attemptId: string,
  input: DraftContent,
): void {
  requireDraftCreationResult(result, attemptId);
  if (!sameContent(result.creation.input, input))
    throw Error("创建事实的完整原输入与固定输入不一致");
}
/** 创建与本地转交分别完成；固定依据只保存在私有副本中。 */
export class DraftCopyOperation {
  private readonly fixedSourceId: string;
  private readonly fixedSourceVersion: number;
  private readonly fixedInput: DraftContent;
  private readonly fixedAttemptId: string;
  private currentPhase: DraftCopyPhase = "new";
  private currentResult?: DraftCreationResult;
  private request?: Promise<void>;
  private discarded = false;
  diagnostic = "";
  constructor(
    sourceId: string,
    sourceVersion: number,
    input: DraftContent,
    private transport: DraftCopyTransport,
    attemptId: string = crypto.randomUUID(),
    private changed: () => void = () => {},
  ) {
    checkDraftContent(input);
    if (
      !attemptId ||
      !sourceId ||
      !Number.isSafeInteger(sourceVersion) ||
      sourceVersion < 0
    )
      throw Error("另存缺少固定身份或本页版本");
    this.fixedSourceId = sourceId;
    this.fixedSourceVersion = sourceVersion;
    this.fixedInput = cloneClientJson(input);
    this.fixedAttemptId = attemptId;
  }
  get sourceId() {
    return this.fixedSourceId;
  }
  get sourceVersion() {
    return this.fixedSourceVersion;
  }
  get attemptId() {
    return this.fixedAttemptId;
  }
  get input() {
    return cloneClientJson(this.fixedInput);
  }
  get phase() {
    return this.currentPhase;
  }
  get result() {
    return this.currentResult && cloneClientJson(this.currentResult);
  }
  get abandoned() {
    return this.discarded;
  }
  get terminalTarget() {
    return (
      this.currentPhase === "created_untransferred" &&
      this.currentResult?.kind === "created" &&
      this.currentResult.target.kind !== "editable"
    );
  }
  get locksSource() {
    return !this.discarded && this.phase !== "transferred";
  }
  get requestRunning() {
    return !!this.request || this.phase === "submitting";
  }
  get canAbandon() {
    return (
      !this.requestRunning &&
      !this.discarded &&
      (this.phase === "not_created" || this.phase === "created_untransferred")
    );
  }
  async advance(): Promise<void> {
    if (
      this.request ||
      this.phase === "submitting" ||
      this.discarded ||
      this.phase === "transferred" ||
      this.phase === "created_untransferred"
    )
      return;
    if (this.phase === "invalid")
      throw Error("创建依据不可解释，请按原身份核实");
    await this.run(() =>
      this.transport.create(
        this.fixedAttemptId,
        cloneClientJson(this.fixedInput),
      ),
    );
  }
  async verify(): Promise<void> {
    if (
      this.request ||
      this.phase === "submitting" ||
      this.discarded ||
      this.phase === "transferred" ||
      this.terminalTarget
    )
      return;
    if (this.phase === "new") throw Error("尚未提交另存尝试");
    await this.run(() => this.transport.read(this.fixedAttemptId));
  }
  private async run(send: () => Promise<DraftCreationResult>) {
    this.currentPhase = "submitting";
    this.changed();
    const request = (async () => {
      let raw: DraftCreationResult;
      try {
        raw = await send();
      } catch (error) {
        this.currentPhase =
          error instanceof HttpError &&
          ["creation_conflict", "invalid_content"].includes(error.code)
            ? "invalid"
            : "unknown";
        this.diagnostic =
          error instanceof Error ? error.message : String(error);
        return;
      }
      try {
        const next = requireDraftCreationResult(raw, this.fixedAttemptId);
        if (next.kind === "created") {
          requireMatchingCreationResult(
            next,
            this.fixedAttemptId,
            this.fixedInput,
          );
          if (next.creation.created.id === this.sourceId)
            throw Error("另存目标必须是独立草稿");
        }
        const previous = this.currentResult;
        if (previous?.kind === "created") {
          if (
            next.kind !== "created" ||
            stringifyJson(next.creation) !== stringifyJson(previous.creation)
          )
            throw Error("首次创建事实已改变或消失");
          if (
            previous.target.kind !== "deleted" &&
            next.target.kind !== "deleted"
          ) {
            if (
              next.target.draft.revision < previous.target.draft.revision ||
              (next.target.draft.revision === previous.target.draft.revision &&
                stringifyJson(next.target.draft) !==
                  stringifyJson(previous.target.draft))
            )
              throw Error("目标当前记录倒退或同版本事实矛盾");
          }
        }
        this.currentResult = cloneClientJson(next);
        this.currentPhase =
          next.kind === "created" ? "created_untransferred" : "not_created";
        this.diagnostic = "";
      } catch (error) {
        this.currentPhase = "invalid";
        this.diagnostic =
          error instanceof Error ? error.message : String(error);
      }
    })();
    this.request = request;
    try {
      await request;
    } finally {
      this.request = undefined;
      this.changed();
    }
  }
  completeTransfer() {
    if (this.phase === "transferred") return;
    this.prepareTransferCompletion()();
  }
  /** 资格检查必须在共同提交前完成；返回值只应用已准备的阶段。 */
  prepareTransferCompletion(): () => void {
    if (
      this.phase !== "created_untransferred" ||
      this.terminalTarget ||
      this.discarded ||
      this.request
    )
      throw Error("目标当前不可编辑，不能完成输入转交");
    return () => {
      this.currentPhase = "transferred";
    };
  }
  abandon() {
    this.prepareAbandonment()();
    this.changed();
  }
  /** 放弃安排只结束本地责任，不改变创建事实或清除固定输入。 */
  prepareAbandonment(): () => void {
    if (!this.canAbandon) throw Error("尚未核实的另存责任不能放弃");
    return () => {
      this.discarded = true;
    };
  }
}
