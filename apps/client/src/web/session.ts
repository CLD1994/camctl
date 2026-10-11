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
import { HttpError } from "./api";
import { SupersededStateReadError } from "./state-observations";
import {
  DraftCopyOperation,
  type DraftCopyTransport,
  type DraftCopyPhase,
} from "./draft-copy";
import { checkDraftContent } from "../shared/draft-content";
import { requireDraftRecord } from "../shared/draft-record";
import {
  classifyDraftObservation,
  selectionMatches,
  type DraftObservation,
  type DraftDecision,
  type ConflictSelectionSnapshot,
} from "./draft-conflicts";
export interface DraftTransport {
  save(id: string, revision: number, content: DraftContent): Promise<Draft>;
  read(id: string): Promise<Draft>;
  readObservation?(id: string): Promise<DraftObservation>;
}
export interface WholeDraftTransition {
  draftId: string;
  localVersion: number;
  origin: DraftContent;
  next: DraftContent;
  baseline: Draft;
  actual: Draft;
  reason: "save" | "derived" | "remote" | "adopt" | "replace" | "copy";
}
export type DraftSaveStatus =
  | "copying"
  | "saved"
  | "dirty"
  | "saving"
  | "replacing"
  | "selecting"
  | "accepting"
  | "unknown"
  | "conflict"
  | "superseded_write"
  | "invalid"
  | "unavailable"
  | "blocked"
  | "exported"
  | "deleted";
export { sameContent } from "../shared/automatic-previews";
type ExportState = "editable" | "exporting" | "unknown" | "exported";
interface PendingWrite {
  revision: number;
  content: DraftContent;
  version: number;
  baseline: DraftContent;
  baselineDraft: Draft;
  requestEnded: boolean;
  generation: number;
  reason: "save" | "replace";
}
export interface DraftCopyRecovery {
  readonly id: string;
  readonly sourceId: string;
  readonly sourceVersion: number;
  readonly input: DraftContent;
  readonly status: "pending" | "transferred" | "returned" | "discarded";
  readonly attemptIds: readonly string[];
  readonly transferredBy?: string;
}
interface CopyRecoveryRecord {
  view: DraftCopyRecovery;
  input: DraftContent;
  sourceVersion: number;
  status: DraftCopyRecovery["status"];
  operations: DraftCopyOperation[];
  generation: number;
  transferredBy?: string;
}
export interface DraftCopyDecision {
  readonly attemptId: string;
  readonly recoveryId: string;
  readonly generation: number;
  readonly sourceId: string;
  readonly localVersion: number;
  readonly input: DraftContent;
  readonly observationEpoch: number;
  readonly phase: DraftCopyPhase;
  readonly targetKind?: "editable" | "deleted" | "exported";
  readonly requestRunning: boolean;
  readonly canRetry: boolean;
  readonly canAbandon: boolean;
  readonly retryCreatesNewAttempt: boolean;
}
interface CopySelection {
  operation: DraftCopyOperation;
  recovery: CopyRecoveryRecord;
  transport: DraftCopyTransport;
}
interface CopyDecisionBasis {
  selection: CopySelection;
  generation: number;
  version: number;
  sourceId: string;
  sourceCreatedAt: string;
  input: DraftContent;
  epoch: number;
  observation: string;
  phase: DraftCopyPhase;
  result: string;
}
export interface SaveVerification {
  readonly generation: number;
  readonly localVersion: number;
  readonly input: DraftContent;
  readonly observationEpoch: number;
}
export interface WholeAcceptanceSnapshot {
  readonly generation: number;
  readonly localVersion: number;
  readonly origin: DraftContent;
  readonly observationEpoch: number;
}
export interface DraftSelectionDecision {
  readonly generation: number;
  readonly sourceId: string;
  readonly kind: "adopt" | "replace";
  readonly phase: "running" | "unknown" | "confirmation" | "acceptance";
  readonly input: DraftContent;
  readonly written?: Draft;
  readonly requestRunning: boolean;
  readonly canRetry: boolean;
  readonly canAbandon: boolean;
}
interface SelectionOperation {
  kind: "adopt" | "replace";
  generation: number;
  version: number;
  input: DraftContent;
  snapshot: ConflictSelectionSnapshot;
  epoch: number;
  written?: Draft;
  phase: "unknown" | "confirmation" | "acceptance";
}
export class DraftSession {
  readonly draft: Draft;
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
  private failedWrite?: PendingWrite;
  private automaticSaveBlocked = false;
  private lastEditAt = 0;
  private recoveredVersion?: number;
  private exportSnapshot?: { revision: number; content: DraftContent };
  private running?: Promise<void>;
  private timer?: ReturnType<typeof setTimeout>;
  private deletionFailure = "";
  private capabilities?: CapabilityState;
  private appendView?: (transition: VerifiedAppend) => () => void;
  private appendGeneration = 0;
  private appendPreparing = false;
  private acceptedAppend?: VerifiedAppend;
  private confirmed: Draft;
  private observation: DraftObservation;
  private latestReliable: Draft;
  observationEpoch = 0;
  decision: DraftDecision = { kind: "same_baseline" };
  private supersededWriteDiagnostic = false;
  private lastSaveDiagnostic = "";
  get saveDiagnostic() {
    return [
      this.supersededWriteDiagnostic ? "旧保存结果不可确认" : "",
      this.lastSaveDiagnostic,
    ]
      .filter(Boolean)
      .join("；");
  }
  private selectionLocked = false;
  private operationGeneration = 0;
  private abandonedWrites: PendingWrite[] = [];
  private selections = new WeakMap<
    ConflictSelectionSnapshot,
    ConflictSelectionSnapshot
  >();
  private wholeView?: (input: WholeDraftTransition) => () => void;
  private wholeAcceptance?: {
    transition: WholeDraftTransition;
    finish: () => void;
    observationEpoch: number;
    generation: number;
  };
  private wholePreparationGeneration = 0;
  private wholePreparing = 0;
  private wholeExecution?: object;
  private ordinaryAcceptances = new WeakMap<WholeAcceptanceSnapshot, {
    acceptance: NonNullable<DraftSession["wholeAcceptance"]>;
    editGeneration: number;
    operationGeneration: number;
    observation: string;
    origin: DraftContent;
    sourceId: string;
    sourceCreatedAt: string;
    revision: number;
    confirmedVersion: number;
  }>();
  private targetDeleted = false;
  private retryableRejected = false;
  private selectionOperation?: SelectionOperation;
  private selectionGeneration = 0;
  private selectionExecution?: object;
  private selectionDecisions = new WeakMap<
    DraftSelectionDecision,
    {
      operation: SelectionOperation;
      epoch: number;
      observation: string;
      phase: DraftSelectionDecision["phase"];
      written: string;
    }
  >();
  private verificationSnapshots = new WeakMap<SaveVerification, PendingWrite>();
  get saveVerification(): SaveVerification | undefined {
    const pending = this.pendingWrite;
    if (
      !pending ||
      !pending.requestEnded ||
      this.wholeAcceptance ||
      this.saving ||
      this.running ||
      this.selectionExecution
    )
      return;
    const view = Object.freeze({
      generation: pending.generation,
      localVersion: this.version,
      input: cloneClientJson(this.content),
      observationEpoch: this.observationEpoch,
    });
    this.verificationSnapshots.set(view, pending);
    return view;
  }
  async verifySaveResult(
    view: SaveVerification | undefined = this.saveVerification,
  ) {
    const pending = view && this.verificationSnapshots.get(view);
    if (
      !view ||
      !pending ||
      this.pendingWrite !== pending ||
      this.running ||
      this.saving ||
      this.wholeAcceptance ||
      this.copyLocked ||
      this.selectionExecution ||
      !pending.requestEnded ||
      pending.generation !== this.operationGeneration ||
      view.localVersion !== this.version ||
      !sameContent(view.input, this.content) ||
      view.observationEpoch !== this.observationEpoch
    )
      throw Error("保存核实资格已改变，请重新取得当前责任");
    this.cancelScheduledSave();
    this.saving = true;
    this.changed();
    try {
      return await this.verifyWrite(view.observationEpoch, view);
    } finally {
      this.saving = false;
      this.changed();
    }
  }
  get selectionPendingDecision() {
    return !!this.selectionOperation;
  }
  get selectionDecision(): DraftSelectionDecision | undefined {
    const operation = this.selectionOperation;
    if (!operation) return;
    const busy = !!this.selectionExecution || this.saving || !!this.running;
    const reliable =
      this.observation.kind !== "unavailable" &&
      this.decision.kind !== "invalid";
    const owns =
      operation.version === this.version &&
      sameContent(operation.input, this.content);
    const phase = busy
      ? "running"
      : !operation.written && this.decision.kind === "superseded_write"
        ? "confirmation"
        : operation.phase;
    const view: DraftSelectionDecision = Object.freeze({
      generation: operation.generation,
      sourceId: this.draft.id,
      kind: operation.kind,
      phase,
      get input() {
        return cloneClientJson(operation.input);
      },
      get written() {
        return operation.written && cloneClientJson(operation.written);
      },
      requestRunning: busy,
      canRetry: !busy && owns,
      canAbandon:
        !busy &&
        owns &&
        reliable &&
        (!!operation.written ||
          !this.pendingWrite ||
          this.decision.kind === "superseded_write" ||
          this.observation.kind === "deleted" ||
          (this.observation.kind === "available" && !!this.observation.draft.exportedRequestId)),
    });
    this.selectionDecisions.set(view, {
      operation,
      epoch: this.observationEpoch,
      observation: stringifyJson(this.observation),
      phase,
      written: stringifyJson(operation.written),
    });
    return view;
  }
  private requireSelectionDecision(view: DraftSelectionDecision | undefined) {
    const fixed = view && this.selectionDecisions.get(view),
      operation = this.selectionOperation;
    if (
      !fixed ||
      !operation ||
      fixed.operation !== operation ||
      fixed.epoch !== this.observationEpoch ||
      fixed.observation !== stringifyJson(this.observation) ||
      fixed.phase !== this.selectionDecision?.phase ||
      fixed.written !== stringifyJson(operation.written) ||
      operation.version !== this.version ||
      !sameContent(operation.input, this.content) ||
      this.selectionExecution ||
      this.saving ||
      this.running
    )
      throw Error("所选操作的输入、观察或责任代次已改变");
    return operation;
  }
  private selectionStillConfirmed(operation: SelectionOperation) {
    return (
      operation.epoch === this.observationEpoch &&
      selectionMatches(
        operation.snapshot,
        this.draft.id,
        this.version,
        this.content,
        this.observation,
        operation.snapshot.observationEpoch,
      )
    );
  }
  private stopSelectionLocal(operation: SelectionOperation) {
    if (this.selectionOperation !== operation) throw Error("所选操作已结束");
    this.cancelScheduledSave();
    this.editGeneration++;
    this.wholeAcceptance = undefined;
    if (operation.kind === "replace" && operation.written) {
      this.pendingWrite = undefined;
      this.confirmed = cloneClientJson(operation.written);
      this.baseline = cloneClientJson(operation.written.content);
      this.revision = operation.written.revision;
      if (operation.written.revision > this.latestReliable.revision)
        this.latestReliable = cloneClientJson(operation.written);
      this.confirmedVersion = sameContent(
        this.content,
        operation.written.content,
      )
        ? this.version
        : this.confirmedVersion;
    }
    this.selectionLocked = false;
    this.automaticSaveBlocked = true;
    const observation = cloneClientJson(this.observation);
    if (
      operation.written &&
      observation.kind === "available" &&
      observation.draft.revision < operation.written.revision
    )
      this.observation = {
        kind: "available",
        draft: cloneClientJson(operation.written),
        capabilities: this.capabilities,
      };
    this.observeTarget(this.observation);
    this.selectionOperation = undefined;
    // 停止所选本地接纳只交回输入。普通观察不能在放弃或重新确认时采用另一份内容。
    if (
      this.observation.kind === "available" &&
      !this.observation.draft.exportedRequestId &&
      this.decision.kind !== "invalid"
    ) {
      this.decision = { kind: "conflict" };
      this.conflict = cloneClientJson(this.observation.draft);
      this.error = "当前输入已保留，请重新选择完整版本。";
    }
    this.changed();
  }
  abandonSelection(
    view: DraftSelectionDecision | undefined = this.selectionDecision,
  ) {
    const operation = this.requireSelectionDecision(view);
    if (!view!.canAbandon) throw Error("请先核实所选操作，当前不能放弃");
    this.stopSelectionLocal(operation);
  }
  async retrySelection(
    view: DraftSelectionDecision | undefined = this.selectionDecision,
  ): Promise<"completed" | "confirmation" | "pending"> {
    const operation = this.requireSelectionDecision(view);
    if (!view!.canRetry) throw Error("所选操作正在执行");
    const owner = {};
    this.selectionExecution = owner;
    this.changed();
    try {
      if (
        this.observation.kind === "unavailable" ||
        this.decision.kind === "invalid" ||
        (operation.phase === "unknown" &&
          this.decision.kind !== "superseded_write" &&
          this.observation.kind === "available" &&
          !this.observation.draft.exportedRequestId)
      ) {
        const epoch = this.observationEpoch;
        if (this.pendingWrite && !operation.written)
          await this.verifyWrite(epoch);
        else {
          const observed = await this.readObservation();
          if (
            epoch !== this.observationEpoch ||
            this.selectionOperation !== operation
          )
            throw Error("核实期间所选资格已改变");
          this.observeTarget(observed);
        }
      }
      if (this.selectionOperation !== operation) return "completed";
      if (
        this.observation.kind === "unavailable" ||
        this.decision.kind === "invalid"
      )
        return "pending";
      if (
        (operation.kind === "adopt" &&
          !this.selectionStillConfirmed(operation)) ||
        !this.wholeAcceptance
      ) {
        this.stopSelectionLocal(operation);
        return "confirmation";
      }
      if (
        operation.kind === "replace" &&
        operation.written &&
        this.observation.kind === "available" &&
        this.observation.draft.revision > operation.written.revision
      ) {
        this.wholeAcceptance!.transition.actual = cloneClientJson(
          this.observation.draft,
        );
        this.wholeAcceptance!.transition.next = cloneClientJson(
          this.observation.draft.content,
        );
      }
      if (
        this.observation.kind === "deleted" ||
        (this.observation.kind === "available" &&
          this.observation.draft.exportedRequestId)
      ) {
        this.stopSelectionLocal(operation);
        return "confirmation";
      }
      this.retryWholeAcceptanceOwned(owner);
      return "completed";
    } finally {
      if (this.selectionExecution === owner)
        this.selectionExecution = undefined;
      this.changed();
    }
  }
  get hasUnfinishedResponsibilities() {
    return (
      !!this.pendingWrite ||
      !!this.wholeAcceptance ||
      !!this.selectionOperation ||
      this.abandonedWrites.length > 0 ||
      this.copyPendingDecision ||
      this.pendingCopyRecoveries.length > 0 ||
      this.exportState === "unknown" ||
      this.exportState === "exporting" ||
      this.deletionState === "unknown" ||
      this.deletionState === "deleting" ||
      this.appendLocked
    );
  }
  get needsLeaveWarning() {
    return (
      !sameContent(this.content, this.confirmed.content) ||
      !!this.recoveryContent ||
      this.hasUnfinishedResponsibilities
    );
  }
  constructor(
    draft: Draft,
    private transport: DraftTransport,
    private changed: () => void,
  ) {
    this.draft = cloneClientJson(draft);
    this.content = cloneClientJson(draft.content);
    this.baseline = cloneClientJson(draft.content);
    this.revision = draft.revision;
    this.confirmed = cloneClientJson(draft);
    this.observation = { kind: "available", draft: cloneClientJson(draft) };
    this.latestReliable = cloneClientJson(draft);
    this.exportedRequestId = draft.exportedRequestId;
    if (draft.exportedRequestId) this.exportState = "exported";
  }
  private currentCopyOperation?: DraftCopyOperation;
  get copyOperation() {
    return this.currentCopyOperation;
  }
  private priorCopies: DraftCopyOperation[] = [];
  private copyExecution?: object;
  private selectedCopy?: CopySelection;
  private copyDecisions = new WeakMap<DraftCopyDecision, CopyDecisionBasis>();
  private editGeneration = 0;
  get copyPendingDecision() {
    return !!this.selectedCopy;
  }
  get copyDecision(): DraftCopyDecision | undefined {
    const selection = this.selectedCopy;
    if (!selection) return;
    const { operation, recovery } = selection,
      result = operation.result;
    const busy = this.copyRequestRunning;
    const reliableSource =
      this.observation.kind !== "unavailable" &&
      this.decision.kind !== "invalid";
    const input = operation.input;
    const ownsInput =
      operation.sourceId === this.draft.id &&
      this.draft.createdAt === this.confirmed.createdAt &&
      operation.sourceVersion === this.version &&
      recovery.sourceVersion === this.version &&
      sameContent(this.content, input) &&
      sameContent(recovery.input, input);
    const view: DraftCopyDecision = Object.freeze({
      attemptId: operation.attemptId,
      recoveryId: recovery.view.id,
      generation: recovery.generation,
      sourceId: operation.sourceId,
      localVersion: operation.sourceVersion,
      get input() {
        return cloneClientJson(input);
      },
      observationEpoch: this.observationEpoch,
      phase: operation.phase,
      targetKind: result?.kind === "created" ? result.target.kind : undefined,
      requestRunning: busy,
      canRetry:
        !busy &&
        ownsInput &&
        !operation.abandoned &&
        operation.phase !== "transferred" &&
        recovery.status === "pending",
      canAbandon:
        !busy &&
        ownsInput &&
        reliableSource &&
        operation.canAbandon &&
        recovery.status === "pending",
      retryCreatesNewAttempt: reliableSource && operation.terminalTarget,
    });
    this.copyDecisions.set(view, {
      selection,
      generation: recovery.generation,
      version: this.version,
      sourceId: this.draft.id,
      sourceCreatedAt: this.draft.createdAt,
      input: cloneClientJson(input),
      epoch: this.observationEpoch,
      observation: stringifyJson(this.observation),
      phase: operation.phase,
      result: stringifyJson(result),
    });
    return view;
  }
  private requireCopyBasis(fixed: CopyDecisionBasis) {
    const { selection } = fixed,
      { operation, recovery } = selection;
    if (
      this.selectedCopy !== selection ||
      this.copyOperation !== operation ||
      this.operationRecoveries.get(operation) !== recovery ||
      !this.recoveryRecords.includes(recovery) ||
      recovery.status !== "pending" ||
      operation.abandoned ||
      recovery.generation !== fixed.generation ||
      recovery.operations.at(-1) !== operation ||
      recovery.sourceVersion !== fixed.version ||
      this.version !== fixed.version ||
      this.draft.id !== fixed.sourceId ||
      this.draft.createdAt !== fixed.sourceCreatedAt ||
      operation.sourceId !== fixed.sourceId ||
      operation.sourceVersion !== fixed.version ||
      !sameContent(recovery.input, fixed.input) ||
      !sameContent(this.content, fixed.input) ||
      fixed.epoch !== this.observationEpoch ||
      fixed.observation !== stringifyJson(this.observation)
    )
      throw Error("所选另存的固定输入、观察或责任代次已经改变");
  }
  private get copyRequestRunning() {
    return !!this.copyExecution || !!this.copyOperation?.requestRunning;
  }
  private requireCopyIdle() {
    if (this.copyRequestRunning)
      throw Error("另存正在执行，请等待当次核实或转交结束");
  }
  private requireCopyOwner(owner: object) {
    if (this.copyExecution !== owner) throw Error("另存执行所有权已经改变");
  }
  private async executeCopy<T>(run: (owner: object) => Promise<T>): Promise<T> {
    this.requireCopyIdle();
    const owner = {};
    this.copyExecution = owner;
    try {
      this.changed();
      return await run(owner);
    } finally {
      if (this.copyExecution === owner) {
        this.copyExecution = undefined;
        this.changed();
      }
    }
  }
  private requireCopyDecision(
    view: DraftCopyDecision | undefined,
    owner?: object,
  ) {
    const fixed = view && this.copyDecisions.get(view);
    if (!fixed) throw Error("所选另存决定不属于此会话");
    this.requireCopyBasis(fixed);
    if (
      fixed.selection.operation.phase !== fixed.phase ||
      stringifyJson(fixed.selection.operation.result) !== fixed.result ||
      (this.copyExecution && this.copyExecution !== owner) ||
      fixed.selection.operation.requestRunning
    )
      throw Error("所选另存的阶段已经改变或正在执行");
    return fixed;
  }
  async retryCopy(
    view: DraftCopyDecision,
    prepareTarget: Parameters<DraftSession["retryCopyTransfer"]>[0],
    attemptId?: string,
  ): Promise<"transferred" | "pending"> {
    const fixed = this.requireCopyDecision(view);
    if (!view.canRetry) throw Error("当前另存不能重试");
    return this.executeCopy((owner) =>
      this.retryCopyOwned(fixed, prepareTarget, attemptId, owner),
    );
  }
  private async retryCopyOwned(
    fixed: CopyDecisionBasis,
    prepareTarget: Parameters<DraftSession["retryCopyTransfer"]>[0],
    attemptId: string | undefined,
    owner: object,
  ): Promise<"transferred" | "pending"> {
    this.requireCopyOwner(owner);
    this.requireCopyBasis(fixed);
    const { selection } = fixed;
    let operation = selection.operation;
    let actionBasis = fixed;
    if (
      this.observation.kind === "unavailable" ||
      this.decision.kind === "invalid"
    ) {
      // 独立创建仍按原身份核实；来源观察失效时先恢复资格，不授权新身份。
      if (operation.phase !== "new" && !operation.terminalTarget)
        await operation.verify();
      this.requireCopyOwner(owner);
      this.requireCopyBasis(fixed);
      const observation = await this.readObservation();
      this.requireCopyOwner(owner);
      this.requireCopyBasis(fixed);
      this.observeTarget(observation);
      return "pending";
    }
    if (operation.terminalTarget) {
      operation = this.startRecoveryCopy(
        selection.recovery.view,
        selection.transport,
        attemptId,
      );
      actionBasis = this.copyDecisions.get(this.copyDecision!)!;
      await operation.advance();
    } else {
      if (
        ["unknown", "invalid", "created_untransferred"].includes(
          operation.phase,
        )
      ) {
        await operation.verify();
        this.requireCopyOwner(owner);
        this.requireCopyBasis(fixed);
        // 核实首次发现终态时，下一次显示新决定说明后再点击，不能自动分配新身份。
        if (operation.terminalTarget) return "pending";
      }
      if (["new", "not_created"].includes(operation.phase))
        await operation.advance();
    }
    this.requireCopyOwner(owner);
    this.requireCopyBasis(actionBasis);
    if (operation.phase !== "created_untransferred" || operation.terminalTarget)
      return "pending";
    // 请求仅更新创建事实；下一步使用重新取得的固定资格准备本地转交。
    const current = this.copyDecision;
    this.requireCopyDecision(current, owner);
    await this.transferCopyOwned(prepareTarget, owner);
    return "transferred";
  }
  get retainedCopies(): readonly DraftCopyOperation[] {
    return [
      ...this.priorCopies,
      ...(this.copyOperation ? [this.copyOperation] : []),
    ];
  }
  private recoveryRecords: CopyRecoveryRecord[] = [];
  private operationRecoveries = new WeakMap<
    DraftCopyOperation,
    CopyRecoveryRecord
  >();
  get copyRecoveries(): readonly DraftCopyRecovery[] {
    return this.recoveryRecords.map((record) => record.view);
  }
  get pendingCopyRecoveries(): readonly DraftCopyRecovery[] {
    return this.recoveryRecords
      .filter((record) => record.status === "pending")
      .map((record) => record.view);
  }
  get copyRecovery(): DraftCopyRecovery | undefined {
    return (
      this.copyOperation &&
      this.operationRecoveries.get(this.copyOperation)?.view
    );
  }
  private registerCopyRecovery(
    operation: DraftCopyOperation,
    linked?: CopyRecoveryRecord,
  ) {
    let record = linked;
    if (!record) {
      record = {
        view: undefined!,
        input: operation.input,
        sourceVersion: operation.sourceVersion,
        status: "pending",
        operations: [],
        generation: 0,
      };
      const fixed = record;
      fixed.view = Object.freeze({
        id: operation.attemptId,
        sourceId: operation.sourceId,
        sourceVersion: operation.sourceVersion,
        get input() {
          return cloneClientJson(fixed.input);
        },
        get status() {
          return fixed.status;
        },
        get attemptIds() {
          return fixed.operations.map((op) => op.attemptId);
        },
        get transferredBy() {
          return fixed.transferredBy;
        },
      });
      this.recoveryRecords.push(fixed);
    }
    record.operations.push(operation);
    record.generation++;
    this.operationRecoveries.set(operation, record);
  }
  private requireRecovery(view: DraftCopyRecovery): CopyRecoveryRecord {
    const record = this.recoveryRecords.find((record) => record.view === view);
    if (!record || record.status !== "pending")
      throw Error("恢复输入的责任已改变或不属于此会话");
    return record;
  }
  get copyLocked() {
    return !!this.copyExecution || !!this.selectedCopy;
  }
  get canBeginCopy() {
    if (
      this.copyLocked ||
      (this.copyOperation &&
        !this.copyOperation.abandoned &&
        this.copyOperation.phase !== "transferred")
    )
      return false;
    try {
      this.requireCopyEligibility();
      return true;
    } catch {
      return false;
    }
  }
  private requireCopyEligibility(
    observation = this.observation,
    transferring = false,
  ) {
    const writeIsolated =
      this.pendingWrite?.requestEnded &&
      (observation.kind === "deleted" ||
        (observation.kind === "available" &&
          (observation.draft.exportedRequestId ||
            observation.draft.revision > this.pendingWrite.revision)));
    if (
      this.saving ||
      this.wholePreparing ||
      this.selectionOperation ||
      this.running ||
      this.wholeAcceptance ||
      this.appendLocked ||
      ["exporting", "unknown"].includes(this.exportState) ||
      ["deleting", "unknown"].includes(this.deletionState) ||
      (this.selectionLocked && !writeIsolated) ||
      (!transferring && this.decision.kind === "invalid") ||
      observation.kind === "unavailable"
    )
      throw Error("请先核实原草稿的操作与完整记录");
    if (
      this.pendingWrite &&
      (!this.pendingWrite.requestEnded ||
        (observation.kind !== "deleted" &&
          !(
            observation.kind === "available" &&
            (observation.draft.exportedRequestId ||
              observation.draft.revision > this.pendingWrite.revision)
          )))
    )
      throw Error("旧保存仍可能改变原草稿，请先按固定依据核实");
  }
  beginCopy(
    transport: DraftCopyTransport,
    attemptId?: string,
  ): DraftCopyOperation {
    this.requireCopyIdle();
    this.requireCopyEligibility();
    if (this.selectedCopy) throw Error("请先处理所选另存决定");
    if (
      this.copyOperation &&
      !this.copyOperation.abandoned &&
      this.copyOperation.phase !== "transferred"
    )
      throw Error("已有另存尝试，请核实或重试同一次操作");
    return this.startCopy(this.content, transport, attemptId);
  }
  private startCopy(
    input: DraftContent,
    transport: DraftCopyTransport,
    attemptId?: string,
    recovery?: CopyRecoveryRecord,
  ) {
    if (
      attemptId &&
      this.retainedCopies.some((op) => op.attemptId === attemptId)
    )
      throw Error("另存尝试身份不能复用");
    const op = new DraftCopyOperation(
      this.draft.id,
      this.version,
      input,
      transport,
      attemptId,
      this.changed,
    );
    if (this.copyOperation) this.priorCopies.push(this.copyOperation);
    this.currentCopyOperation = op;
    this.registerCopyRecovery(op, recovery);
    this.selectedCopy = {
      operation: op,
      recovery: this.operationRecoveries.get(op)!,
      transport,
    };
    this.editGeneration++;
    this.cancelScheduledSave();
    this.changed();
    return op;
  }
  beginNewCopy(
    transport: DraftCopyTransport,
    attemptId?: string,
  ): DraftCopyOperation {
    this.requireCopyIdle();
    const recovery = this.copyRecovery;
    if (!recovery) throw Error("没有待处理的另存恢复输入");
    return this.beginRecoveryCopy(recovery, transport, attemptId);
  }
  beginRecoveryCopy(
    recovery: DraftCopyRecovery,
    transport: DraftCopyTransport,
    attemptId?: string,
  ): DraftCopyOperation {
    this.requireCopyIdle();
    return this.startRecoveryCopy(recovery, transport, attemptId);
  }
  private startRecoveryCopy(
    recovery: DraftCopyRecovery,
    transport: DraftCopyTransport,
    attemptId?: string,
  ): DraftCopyOperation {
    this.requireCopyEligibility();
    const record = this.requireRecovery(recovery);
    const previous = record.operations.at(-1)!;
    if (
      (this.selectedCopy && this.selectedCopy.recovery !== record) ||
      !previous.terminalTarget ||
      previous.abandoned
    )
      throw Error("新另存决定要求原创建已确认且目标已终结");
    if (
      record.sourceVersion !== this.version ||
      !sameContent(record.input, this.content)
    )
      throw Error("本页已有新的输入，请先处理原另存保留资料");
    return this.startCopy(record.input, transport, attemptId, record);
  }
  /** 明确放弃选中的恢复输入，与仅取消当前创建尝试分别处理。 */
  discardCopyRecovery(recovery: DraftCopyRecovery) {
    this.requireCopyIdle();
    const record = this.requireRecovery(recovery);
    if (
      this.selectedCopy?.recovery === record ||
      record.operations.some(
        (op) =>
          !op.abandoned && op.phase !== "not_created" && !op.terminalTarget,
      )
    )
      throw Error("尚未核实的另存责任不能放弃");
    for (const op of record.operations) if (!op.abandoned) op.abandon();
    record.status = "discarded";
    record.generation++;
    this.changed();
  }
  abandonCopy(view: DraftCopyDecision | undefined = this.copyDecision) {
    const fixed = this.requireCopyDecision(view);
    if (!view!.canAbandon || !this.copyDecision?.canAbandon)
      throw Error("请先可靠核实所选另存及来源状态");
    const { selection } = fixed,
      { operation, recovery } = selection;
    // 所选安排结束，输入仍归源会话；不调用丢弃输入或结束其他保存责任。
    const commitOperation = operation.prepareAbandonment();
    let decision = classifyDraftObservation(
      this.confirmed,
      this.content,
      this.observation,
      this.pendingWrite
        ? {
            kind: "save",
            revision: this.pendingWrite.revision,
            requestEnded: this.pendingWrite.requestEnded,
          }
        : this.exportState === "unknown" ||
            this.exportState === "exporting" ||
            this.deletionState !== "idle" ||
            this.appendLocked ||
            this.selectionLocked ||
            this.wholeAcceptance
          ? { kind: "other" }
          : { kind: "none" },
    );
    if (["remote", "derived"].includes(decision.kind))
      decision = { kind: "conflict" };
    commitOperation();
    recovery.status = "returned";
    recovery.generation++;
    this.selectedCopy = undefined;
    this.editGeneration++;
    this.cancelScheduledSave();
    this.decision = decision;
    this.changed();
  }
  /** prepareTarget 先准备目标 Map、目标显示及导航，返回不会再拒绝的同步提交。 */
  async retryCopyTransfer(
    prepareTarget: (
      draft: Draft,
    ) => (() => void) | { validate: () => void; commit: () => void },
  ): Promise<void> {
    this.requireCopyIdle();
    if (this.copyOperation?.phase === "transferred") return;
    return this.executeCopy((owner) =>
      this.transferCopyOwned(prepareTarget, owner),
    );
  }
  private async transferCopyOwned(
    prepareTarget: Parameters<DraftSession["retryCopyTransfer"]>[0],
    owner: object,
  ): Promise<void> {
    this.requireCopyOwner(owner);
    const operation = this.copyOperation;
    if (operation?.phase === "transferred") return;
    if (
      !operation ||
      this.selectedCopy?.operation !== operation ||
      operation.phase !== "created_untransferred" ||
      operation.terminalTarget ||
      operation.abandoned
    )
      throw Error("尚未取得可编辑目标的完整创建结果");
    const recovery = this.operationRecoveries.get(operation);
    if (!recovery || recovery.status !== "pending")
      throw Error("另存恢复责任已经改变");
    const recoveryGeneration = recovery.generation;
    const selection = this.selectedCopy;
    const sourceBasis = this.copyDecisions.get(this.copyDecision!)!;
    try {
      const observation = await this.readObservation();
      this.requireCopyOwner(owner);
      this.requireCopyBasis(sourceBasis);
      if (!["available", "deleted", "unavailable"].includes(observation.kind)) {
        this.observeTarget({ kind: "unavailable", message: "来源观察分类无效" });
        throw Error("来源观察分类无效");
      }
      if (
        this.copyOperation !== operation ||
        operation.sourceVersion !== this.version ||
        !sameContent(this.content, operation.input)
      )
        throw Error("来源完整输入已经改变");
      // 自有读回先登记可靠事实和读取失效；copy 锁使登记不接纳固定输入。
      this.observeTarget(observation);
      if (stringifyJson(this.observation) !== stringifyJson(observation))
        throw Error("来源读回已被较新事实取代");
      if (this.decision.kind === "invalid")
        throw Error("来源可靠记录不能解释");
      this.requireCopyEligibility(observation, true);
      if (observation.kind === "unavailable") throw Error(observation.message);
      if (observation.kind === "available") {
        const actual = requireDraftRecord(observation.draft);
        if (
          this.targetDeleted ||
          actual.id !== this.draft.id ||
          actual.createdAt !== this.draft.createdAt ||
          (this.exportedRequestId !== undefined &&
            actual.exportedRequestId !== this.exportedRequestId) ||
          actual.revision < this.latestReliable.revision ||
          (actual.revision === this.latestReliable.revision &&
            (!sameContent(actual.content, this.latestReliable.content) ||
              actual.exportedRequestId !==
                this.latestReliable.exportedRequestId))
        )
          throw Error("来源可靠记录倒退或不能解释");
      }
      const epoch = this.observationEpoch,
        version = this.version;
      const sourceId = this.draft.id,
        sourceCreatedAt = this.draft.createdAt,
        sourceRevision = this.revision,
        sourceConfirmedVersion = this.confirmedVersion,
        sourceExportedRequestId = this.exportedRequestId,
        sourceExportState = this.exportState,
        sourceDeletionState = this.deletionState,
        input = operation.input;
      const result = operation.result!;
      if (result.kind !== "created" || result.target.kind !== "editable")
        throw Error("目标当前不可编辑");
      // 所有校验、副本与显示映射均先准备。源删除时没有可接纳的记录。
      let commitSource: () => void;
      if (observation.kind === "available") {
        const actual = cloneClientJson(observation.draft);
        const transition: WholeDraftTransition = {
          draftId: this.draft.id,
          localVersion: version,
          origin: cloneClientJson(this.content),
          next: cloneClientJson(actual.content),
          baseline: this.confirmedDraft,
          actual,
          reason: "copy",
        };
        const commitView = this.wholeView?.(cloneClientJson(transition));
        commitSource = this.prepareWholeCommit(
          transition,
          () => {
            this.finishSelection();
            this.recoveryContent = undefined;
            if (actual.exportedRequestId) {
              this.exportedRequestId = actual.exportedRequestId;
              this.exportState = "exported";
            }
          },
          commitView,
          false,
        );
      } else {
        const endedContent = cloneClientJson(this.baseline);
        commitSource = () => {
          this.finishSelection();
          this.targetDeleted = true;
          this.decision = { kind: "deleted" };
          this.observation = { kind: "deleted" };
          this.recoveryContent = undefined;
          this.content = endedContent;
          this.version++;
          this.confirmedVersion = this.version;
          this.error = "";
        };
      }
      const preparedTarget = prepareTarget(
        cloneClientJson(result.target.draft),
      );
      const commitTarget =
        typeof preparedTarget === "function"
          ? preparedTarget
          : preparedTarget.commit;
      if (typeof preparedTarget !== "function") preparedTarget.validate();
      // 外部准备及 validate 都可能同步改变会话；最后复核全部固定依据。
      this.requireCopyOwner(owner);
      this.requireCopyEligibility(observation, true);
      if (
        recovery.status !== "pending" ||
        this.selectedCopy !== selection ||
        selection?.recovery !== recovery ||
        recovery.generation !== recoveryGeneration ||
        this.operationRecoveries.get(operation) !== recovery ||
        !this.recoveryRecords.includes(recovery) ||
        recovery.operations.at(-1) !== operation ||
        recovery.sourceVersion !== operation.sourceVersion ||
        !sameContent(recovery.input, input) ||
        epoch !== this.observationEpoch ||
        version !== this.version ||
        this.revision !== sourceRevision ||
        this.confirmedVersion !== sourceConfirmedVersion ||
        this.exportedRequestId !== sourceExportedRequestId ||
        this.exportState !== sourceExportState ||
        this.deletionState !== sourceDeletionState ||
        this.draft.id !== sourceId ||
        this.draft.createdAt !== sourceCreatedAt ||
        operation.sourceId !== sourceId ||
        operation.sourceVersion !== version ||
        !sameContent(this.content, input) ||
        this.copyOperation !== operation ||
        operation.phase !== "created_untransferred" ||
        operation.terminalTarget ||
        operation.abandoned ||
        stringifyJson(operation.result) !== stringifyJson(result)
      )
        throw Error("转交准备期间版本或资格已经改变");
      const commitOperation = operation.prepareTransferCompletion();
      // 提交回调只能应用已经准备的内存状态，不能执行验证、读取或异步动作。
      commitSource();
      commitTarget();
      commitOperation();
      recovery.status = "transferred";
      recovery.transferredBy = operation.attemptId;
      recovery.generation++;
      this.selectedCopy = undefined;
      this.editGeneration++;
      this.changed();
    } finally {
      this.changed();
    }
  }
  /** 已有目标会话只有完整清洁且没有其他操作责任时才可准备接纳。 */
  prepareCopyTarget(actual: Draft): () => void {
    requireDraftRecord(actual);
    if (
      actual.id !== this.draft.id ||
      actual.createdAt !== this.draft.createdAt ||
      actual.exportedRequestId ||
      !this.canSave ||
      !this.saved ||
      this.saving ||
      this.running ||
      actual.revision < this.latestReliable.revision ||
      (actual.revision === this.latestReliable.revision &&
        !sameContent(actual.content, this.latestReliable.content))
    )
      throw Error("目标会话有未保存输入、待定责任或不可靠版本，不能转交覆盖");
    const transition: WholeDraftTransition = {
      draftId: actual.id,
      localVersion: this.version,
      origin: cloneClientJson(this.content),
      next: cloneClientJson(actual.content),
      baseline: this.confirmedDraft,
      actual: cloneClientJson(actual),
      reason: "remote",
    };
    const commitView = this.wholeView?.(cloneClientJson(transition));
    return this.prepareWholeCommit(transition, () => {}, commitView, false);
  }
  get confirmedDraft(): Draft {
    return cloneClientJson(this.confirmed);
  }
  get targetObservation(): DraftObservation {
    return cloneClientJson(this.observation);
  }
  get lastReliableDraft(): Draft {
    return cloneClientJson(this.latestReliable);
  }
  get saveStatus(): DraftSaveStatus {
    if (this.copyLocked) return "copying";
    if (this.wholeAcceptance) return "accepting";
    if (this.saving) return this.selectionLocked ? "replacing" : "saving";
    if (this.pendingWrite)
      return this.decision.kind === "superseded_write"
        ? "superseded_write"
        : "unknown";
    if (this.selectionLocked) return "selecting";
    if (this.targetDeleted || this.deletionState === "deleted")
      return "deleted";
    if (this.exportState === "exported") return "exported";
    if (
      ["conflict", "superseded_write", "invalid", "unavailable"].includes(
        this.decision.kind,
      )
    )
      return this.decision.kind as DraftSaveStatus;
    if (!this.canEdit) return "blocked";
    return this.saved ? "saved" : "dirty";
  }
  get editable() {
    return this.canEdit;
  }
  get canEdit() {
    return (
      this.exportState === "editable" &&
      !this.appendLocked &&
      this.deletionState === "idle" &&
      !this.targetDeleted &&
      !this.selectionLocked &&
      !this.selectionOperation &&
      !this.wholeAcceptance &&
      !this.wholePreparing &&
      !this.copyLocked
    );
  }
  get canSave() {
    return (
      this.canEdit &&
      !this.pendingWrite &&
      ![
        "conflict",
        "invalid",
        "unavailable",
        "superseded_write",
        "blocked",
      ].includes(this.decision.kind)
    );
  }
  async delete(
    remove: (revision: number) => Promise<void>,
    read: () => Promise<Draft | undefined>,
  ) {
    await this.flush();
    if (!this.editable || !this.saved) throw new Error("请先核实草稿保存状态");
    this.deletionState = "deleting";
    this.cancelScheduledSave();
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
      const capabilities = this.observation.kind === "available" &&
        stringifyJson(this.observation.draft) === stringifyJson(actual)
        ? this.observation.capabilities : undefined;
      this.observe(actual, undefined, capabilities);
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
    if (!this.canSave || !this.saved || this.saving)
      throw Error("请先核实草稿保存状态");
    this.cancelScheduledSave();
    this.appendGeneration++;
    this.appendLocked = true;
    this.changed();
  }
  unlockAppend() {
    this.appendGeneration++;
    this.appendLocked = false;
    this.changed();
  }
  get saved() {
    if (
      this.copyLocked ||
      this.pendingWrite ||
      this.wholeAcceptance ||
      this.selectionLocked ||
      this.exportState === "exporting" ||
      this.exportState === "unknown" ||
      this.deletionState === "deleting" ||
      this.deletionState === "unknown"
    )
      return false;
    if (this.exportedRequestId) return !this.recoveryContent;
    return (
      (this.version === this.confirmedVersion || this.hasConfirmedCurrentInput()) &&
      !this.error &&
      !this.pendingWrite &&
      !this.wholeAcceptance &&
      !this.selectionLocked &&
      !["conflict", "invalid", "unavailable", "superseded_write"].includes(
        this.decision.kind,
      )
    );
  }
  edit(content: DraftContent) {
    this.commitEdit(this.prepareEdit(content));
  }
  prepareEdit(content: DraftContent) {
    if (!this.editable) throw new Error("此草稿的导出状态尚未确认或已经只读");
    checkDraftContent(content);
    const actual = cloneClientJson(
      this.capabilities
        ? coordinatePreviews(content, this.capabilities).content
        : content,
    );
    return {
      content: actual,
      version: this.version,
      generation: this.editGeneration,
    };
  }
  commitEdit(prepared: {
    content: DraftContent;
    version: number;
    generation: number;
  }) {
    if (
      !this.editable ||
      prepared.version !== this.version ||
      prepared.generation !== this.editGeneration
    )
      throw Error("草稿状态已改变，不能接纳本次编辑");
    checkDraftContent(prepared.content);
    this.content = cloneClientJson(prepared.content);
    this.version++;
    this.lastEditAt = Date.now();
    if (this.failedWrite?.reason === "save") this.automaticSaveBlocked = false;
    if (
      !this.pendingWrite &&
      !["conflict", "invalid", "unavailable", "superseded_write"].includes(
        this.decision.kind,
      )
    )
      this.error = "";
    this.changed();
  }
  private cancelScheduledSave() {
    clearTimeout(this.timer);
    this.timer = undefined;
  }
  schedule() {
    this.cancelScheduledSave();
    if (this.canSave && !this.hasConfirmedCurrentInput() && !this.pendingWrite && !this.automaticSaveBlocked) {
      // 旧尝试失败不能重置期间新输入的编辑时刻。
      const delay =
        this.failedWrite?.reason === "save" &&
        this.failedWrite.version !== this.version
          ? Math.max(0, this.lastEditAt + 450 - Date.now())
          : 450;
      const generation = this.editGeneration;
      this.timer = setTimeout(() => {
        if (generation !== this.editGeneration) return;
        this.timer = undefined;
        void this.flush().catch(() => {});
      }, delay);
    }
  }
  async flush(): Promise<void> {
    this.cancelScheduledSave();
    if (this.copyLocked) throw Error("请先完成或核实另存操作");
    if (this.selectionOperation) throw Error("请先处理所选操作的待决定责任");
    if (this.running) return this.running;
    if (this.wholeAcceptance) throw Error("保存已经证明，请先重试完整接纳");
    if (!this.canEdit && !this.pendingWrite)
      throw new Error("请先核实草稿的操作状态");
    if (!this.pendingWrite && !this.canSave && !this.retryableRejected)
      throw Error("请先核实后端记录并完成完整版本选择");
    this.running = this.saveLoop();
    try {
      await this.running;
    } finally {
      this.running = undefined;
    }
  }
  private async verifyWrite(
    epoch?: number,
    qualification?: SaveVerification,
  ): Promise<"saved" | "not_saved"> {
    const pending = this.pendingWrite!;
    const readEpoch = epoch ?? this.observationEpoch;
    const observation = await this.readObservation();
    if (
      pending.generation !== this.operationGeneration ||
      this.pendingWrite !== pending ||
      readEpoch !== this.observationEpoch ||
      (qualification !== undefined &&
        (qualification.localVersion !== this.version ||
          !sameContent(qualification.input, this.content)))
    )
      throw Error("本次保存核实已被完整版本选择取代");
    this.observeTarget(observation);
    if (observation.kind === "unavailable")
      throw new Error("保存结果尚未确认；请恢复连接后重试，当前输入已保留。");
    if (observation.kind === "deleted")
      throw Error("草稿已删除；本次保存结果不可确认，当前输入已保留");
    if (this.decision.kind === "invalid" || stringifyJson(this.observation) !== stringifyJson(observation))
      throw Error("保存核实记录不满足当前可靠观察契约");
    const actual = observation.draft;
    const recordDecision = classifyDraftObservation(
      this.confirmed,
      this.content,
      observation,
      { kind: "none" },
    );
    if (recordDecision.kind === "invalid" || recordDecision.kind === "ignore") {
      this.observeTarget(observation);
      throw Error("保存核实记录不满足完整版本契约");
    }
    if (observation.capabilities)
      this.capabilities = cloneClientJson(observation.capabilities);
    if (actual.exportedRequestId) {
      this.observeTarget(observation);
      throw Error("草稿已导出；未保存的输入已保留为可恢复内容");
    }
    if (this.matchesWrite(actual, pending)) {
      this.acceptWrite(actual, pending);
      return "saved";
    }
    if (this.latestReliable && actual.revision < this.latestReliable.revision)
      throw Error("保存核实观察早于已知可靠版本，旧请求责任仍需核实");
    if (
      actual.revision === pending.revision &&
      sameContent(actual.content, pending.baseline)
    ) {
      this.settleNotWritten(pending, "本次保存已可靠确认未写入");
      this.observeTarget(observation);
      return "not_saved";
    }
    this.observeTarget(observation);
    this.conflict = cloneClientJson(actual);
    throw new Error(
      "保存记录与本次已知写入不一致；当前输入与后端记录均已保留，请核对冲突。",
    );
  }
  private matchesWrite(actual: Draft, pending: PendingWrite) {
    try {
      requireDraftRecord(actual);
    } catch {
      return false;
    }
    if (
      actual.id !== this.draft.id ||
      actual.createdAt !== this.draft.createdAt ||
      actual.exportedRequestId
    )
      return false;
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
    if (
      pending.generation !== this.operationGeneration ||
      this.pendingWrite !== pending
    )
      return;
    const unchanged = this.version === pending.version;
    if (pending.reason === "replace" && this.selectionOperation) {
      this.selectionOperation.written = cloneClientJson(actual);
      this.selectionOperation.phase = "acceptance";
    }
    const next = unchanged
      ? actual.content
      : this.capabilities
        ? coordinatePreviews(this.content, this.capabilities).content
        : this.content;
    this.acceptWhole(
      {
        draftId: this.draft.id,
        localVersion: this.version,
        origin: this.content,
        next,
        baseline: this.confirmed,
        actual,
        reason: pending.reason,
      },
      () => {
        this.confirmedVersion = sameContent(
          this.content,
          this.confirmed.content,
        )
          ? this.version
          : pending.version;
        this.pendingWrite = undefined;
        this.failedWrite = undefined;
        if (!this.abandonedWrites.length) this.supersededWriteDiagnostic = false;
        this.automaticSaveBlocked = false;
        if (pending.reason === "replace") this.finishSelection();
      },
    );
  }
  private async saveLoop() {
    this.error = "";
    this.saving = true;
    this.changed();
    try {
      if (this.pendingWrite) {
        const reason = this.pendingWrite.reason;
        if ((await this.verifyWrite()) === "not_saved" && reason === "replace")
          throw Error("本次替换已可靠确认未写入，请重新选择完整版本。");
        // flush 是显式完成保存；普通未写入可继续合法原条件保存，替换须重新选择。
      }
      while (this.confirmedVersion !== this.version && !this.hasConfirmedCurrentInput()) {
        if (!this.canSave && !this.retryableRejected)
          throw new Error("草稿不可继续保存，请核实当前版本");
        const pending: PendingWrite = {
          revision: this.revision,
          content: cloneClientJson(this.content),
          version: this.version,
          baseline: cloneClientJson(this.baseline),
          baselineDraft: cloneClientJson(this.confirmed),
          requestEnded: false,
          generation: this.operationGeneration,
          reason: "save",
        };
        this.pendingWrite = pending;
        this.failedWrite = undefined;
        this.automaticSaveBlocked = false;
        this.retryableRejected = false;
        await this.sendWrite(pending);
        this.changed();
      }
    } catch (error) {
      if (!this.hasConfirmedCurrentInput())
        this.error =
          this.error ||
          (error instanceof Error ? error.message : String(error));
      throw error;
    } finally {
      this.saving = false;
      this.changed();
    }
  }
  private async readObservation(): Promise<DraftObservation> {
    try {
      const observation = this.transport.readObservation
        ? await this.transport.readObservation(this.draft.id)
        : {
            kind: "available" as const,
            draft: await this.transport.read(this.draft.id),
          };
      if (observation.kind === "available")
        requireDraftRecord(observation.draft);
      if (!["available", "deleted", "unavailable"].includes(observation.kind))
        throw Error("来源观察分类无效");
      return cloneClientJson(observation);
    } catch (error) {
      if (error instanceof SupersededStateReadError) throw error;
      return {
        kind: "unavailable",
        message: error instanceof Error ? error.message : String(error),
      };
    }
  }
  private async sendWrite(pending: PendingWrite) {
    let result: Draft;
    try {
      result = await this.transport.save(
        this.draft.id,
        pending.revision,
        cloneClientJson(pending.content),
      );
    } catch (error) {
      pending.requestEnded = true;
      if (pending.generation !== this.operationGeneration) return;
      if (
        error instanceof HttpError &&
        error.status >= 400 &&
        error.code !== "result_unconfirmed"
      ) {
        this.settleNotWritten(pending, `本次保存明确未写入：${error.message}`);
        const readEpoch = this.observationEpoch;
        const observation = await this.readObservation();
        if (pending.generation !== this.operationGeneration) return;
        if (readEpoch !== this.observationEpoch)
          throw Error("拒绝写入后的读取已被较新来源观察取代");
        this.observeTarget(observation);
        if (observation.kind === "unavailable") {
          // 拒绝已证明；读取故障只影响新选择，不把已结束请求重新当作未知。
          this.retryableRejected = pending.reason === "save";
          this.error = `${this.saveDiagnostic}；读取后端记录失败：${observation.message}`;
        }
        throw error;
      }
      const verified = await this.verifyWrite();
      if (verified === "not_saved") throw error;
      return;
    }
    pending.requestEnded = true;
    if (pending.generation !== this.operationGeneration) return;
    if (!this.matchesWrite(result, pending)) {
      const verified = await this.verifyWrite();
      if (verified === "not_saved") throw Error("保存回执与提交内容不一致");
      return;
    }
    // 回执成功后的显示拒绝只留下本地接纳责任，不能再进入请求核实或重发。
    this.acceptWrite(result, pending);
  }
  private settleNotWritten(pending: PendingWrite, diagnostic: string) {
    this.failedWrite = cloneClientJson(pending);
    this.pendingWrite = undefined;
    this.automaticSaveBlocked =
      pending.reason === "replace" || pending.version === this.version;
    this.lastSaveDiagnostic = diagnostic;
    if (pending.reason === "replace" && this.selectionOperation)
      this.selectionOperation.phase = "confirmation";
    if (pending.reason === "replace") this.selectionLocked = false;
  }
  private hasConfirmedCurrentInput() {
    return (
      this.canSave &&
      !this.hasUnfinishedResponsibilities &&
      !this.recoveryContent &&
      sameContent(this.content, this.confirmed.content)
    );
  }
  beginExport(capabilities = this.capabilities) {
    if (!this.canSave || !this.saved) throw new Error("请先完成草稿保存");
    this.cancelScheduledSave();
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
    this.cancelScheduledSave();
    this.changed();
  }
  completeRecovery() {
    this.recoveredVersion = this.version;
    this.recoveryContent = undefined;
    this.changed();
  }
  // 普通观察只登记当前事实；兼容参数不能取得具体导出核实权。
  observe(
    actual: Draft,
    _unexportedToken?: number,
    capabilities?: CapabilityState,
  ) {
    this.observeTarget({ kind: "available", draft: actual, capabilities });
  }
  observeTarget(input: DraftObservation, _unexportedToken?: number) {
    const previousDecision = this.decision.kind;
    const observation = cloneClientJson(input);
    let decision = classifyDraftObservation(
      this.confirmed,
      this.content,
      observation,
      this.pendingWrite
        ? {
            kind: "save",
            revision: this.pendingWrite.revision,
            requestEnded: this.pendingWrite.requestEnded,
          }
        : this.exportState === "unknown" ||
            this.exportState === "exporting" ||
            this.deletionState !== "idle" ||
            this.appendLocked ||
            this.selectionLocked ||
            this.wholeAcceptance ||
            this.wholePreparing ||
            this.copyLocked
          ? { kind: "other" }
          : { kind: "none" },
    );
    if (decision.kind === "ignore") return;
    if (observation.kind === "available" && decision.kind !== "invalid") {
      if (
        this.targetDeleted ||
        observation.draft.revision < this.latestReliable.revision
      )
        return;
      if (
        (this.exportedRequestId !== undefined && observation.draft.exportedRequestId !== this.exportedRequestId) ||
        (observation.draft.revision === this.latestReliable.revision &&
        (!sameContent(observation.draft.content, this.latestReliable.content) ||
          observation.draft.exportedRequestId !==
            this.latestReliable.exportedRequestId))
      )
        decision = { kind: "invalid" };
      else this.latestReliable = cloneClientJson(observation.draft);
    }
    if (stringifyJson(this.observation) !== stringifyJson(observation))
      this.observationEpoch++;
    this.observation = observation;
    this.retryableRejected = false;
    this.decision = decision;
    if (observation.kind === "available" && observation.capabilities)
      this.capabilities = cloneClientJson(observation.capabilities);
    if (decision.kind === "invalid" || decision.kind === "unavailable") {
      this.cancelScheduledSave();
      this.error =
        observation.kind === "unavailable"
          ? `读取后端记录失败：${observation.message}`
          : "后端记录不满足完整版本契约，请重新核实。";
      this.changed();
      return;
    }
    if (decision.kind === "deleted") {
      this.targetDeleted = true;
      if (!sameContent(this.content, this.baseline))
        this.recoveryContent = cloneClientJson(this.content);
      this.cancelScheduledSave();
      this.error = "原草稿已删除，当前未保存资料已保留。";
      this.changed();
      return;
    }
    if (observation.kind !== "available") return;
    const actual = observation.draft;
    if (decision.kind === "exported") {
      if (
        !sameContent(this.content, actual.content) &&
        this.recoveredVersion !== this.version
      )
        this.recoveryContent = cloneClientJson(this.content);
      // 普通终态观察不能结束其他操作的核实责任。
      this.exportedRequestId = actual.exportedRequestId;
      if (this.exportState === "editable" || this.exportState === "exported")
        this.exportState = "exported";
      this.cancelScheduledSave();
      this.error = this.recoveryContent
        ? "原草稿已导出，未保存输入已保留。"
        : "";
      this.changed();
      return;
    }
    if (decision.kind === "blocked") {
      if (previousDecision === "unavailable" || previousDecision === "invalid")
        this.error = "";
      this.changed();
      return;
    }
    if (this.failedWrite && !this.pendingWrite) {
      // 网络责任结束后仍使用该次操作归属；不能把明确替换重新解释为旧B的普通观察。
      if (sameContent(actual.content, this.content))
        decision = {
          kind: this.abandonedWrites.length
            ? "superseded_write"
            : "saved_content",
        };
      else if (this.failedWrite.reason === "replace")
        decision = { kind: "conflict" };
      // 普通观察沿当前合法接纳的B分类，固定失败依据仅保留原操作事实。
      else if (sameContent(actual.content, this.baseline))
        decision = { kind: "same_baseline" };
      else if (decision.kind !== "derived" && decision.kind !== "remote")
        decision = { kind: "conflict" };
      this.decision = decision;
    }
    if (this.selectionOperation) {
      this.conflict = cloneClientJson(actual);
      this.cancelScheduledSave();
      this.changed();
      return;
    }
    if (decision.kind === "superseded_write" || decision.kind === "conflict") {
      this.conflict = cloneClientJson(actual);
      this.cancelScheduledSave();
      this.error =
        decision.kind === "superseded_write"
          ? "旧保存结果不可确认，旧条件写入已不能改变当前目标，请明确选择完整版本。"
          : "本页输入与已保存版本不同，请选择完整版本。";
      if (decision.kind === "superseded_write")
        this.supersededWriteDiagnostic = true;
      if (
        decision.kind === "superseded_write" &&
        this.pendingWrite?.reason === "replace" &&
        this.pendingWrite.requestEnded &&
        !this.wholeAcceptance
      )
        this.selectionLocked = false;
      this.changed();
      return;
    }
    const next =
      decision.kind === "derived"
        ? sameContent(this.content, this.baseline)
          ? actual.content
          : coordinatePreviews(this.content, observation.capabilities!).content
        : decision.kind === "same_baseline"
          ? this.content
          : actual.content;
    // 相同完整内容不涉及新的动作集合，纯版本推进只更新可靠事实。
    if (
      sameContent(next, this.content) &&
      (decision.kind === "same_baseline" ||
        sameContent(actual.content, this.baseline))
    ) {
      this.confirmed = cloneClientJson(actual);
      this.baseline = cloneClientJson(actual.content);
      this.revision = actual.revision;
      this.conflict = undefined;
      if (decision.kind === "saved_content") {
        this.failedWrite = undefined;
        this.automaticSaveBlocked = false;
      }
      this.error =
        this.failedWrite && this.automaticSaveBlocked && !this.hasConfirmedCurrentInput()
          ? this.lastSaveDiagnostic
          : "";
      if (sameContent(this.content, actual.content))
        this.confirmedVersion = this.version;
      this.coordinate(observation.capabilities);
      if (!this.saved && !this.timer) this.schedule();
      this.changed();
      return;
    }
    this.acceptWhole(
      {
        draftId: this.draft.id,
        localVersion: this.version,
        origin: this.content,
        next,
        baseline: this.confirmed,
        actual,
        reason: decision.kind === "derived" ? "derived" : "remote",
      },
      () => {
        if (decision.kind === "saved_content") {
          this.failedWrite = undefined;
          this.automaticSaveBlocked = false;
        }
      },
    );
    if (!this.saved) this.schedule();
  }
  bindWholeView(prepare: (input: WholeDraftTransition) => () => void) {
    const previous = this.wholeView;
    this.wholeView = prepare;
    return () => {
      if (this.wholeView === prepare) this.wholeView = previous;
    };
  }
  private acceptWhole(input: WholeDraftTransition, finish: () => void, validate?: () => void) {
    const preparation = ++this.wholePreparationGeneration;
    const transition = cloneClientJson(input);
    const observed = cloneClientJson(this.observation);
    const readingLostAfterPreparation =
      !!this.wholeAcceptance &&
      this.wholeAcceptance.observationEpoch !== this.observationEpoch;
    checkDraftContent(transition.next);
    requireDraftRecord(transition.actual);
    let commitView: (() => void) | undefined;
    const operation = this.selectionOperation;
    const epoch = this.observationEpoch,
      version = this.version,
      editGeneration = this.editGeneration,
      operationGeneration = this.operationGeneration,
      sourceId = this.draft.id,
      sourceCreatedAt = this.draft.createdAt,
      revision = this.revision,
      confirmedVersion = this.confirmedVersion,
      origin = cloneClientJson(this.content);
    try {
      this.wholePreparing++;
      commitView = this.wholeView?.(cloneClientJson(transition));
      validate?.();
      if (
        preparation !== this.wholePreparationGeneration ||
        editGeneration !== this.editGeneration ||
        operationGeneration !== this.operationGeneration ||
        sourceId !== this.draft.id ||
        sourceCreatedAt !== this.draft.createdAt ||
        revision !== this.revision ||
        confirmedVersion !== this.confirmedVersion ||
        this.selectionOperation !== operation ||
          epoch !== this.observationEpoch ||
          version !== this.version ||
          !sameContent(origin, this.content) ||
          (transition.reason === "adopt" &&
            operation && !this.selectionStillConfirmed(operation))
      )
        throw Error("完整接纳前确认资格已改变，请重新确认");
    } catch (error) {
      if (preparation !== this.wholePreparationGeneration) throw error;
      this.wholeAcceptance = {
        transition,
        finish,
        observationEpoch:
          this.wholeAcceptance?.observationEpoch ?? this.observationEpoch,
        generation: preparation,
      };
      this.cancelScheduledSave();
      this.error = `完整版本已证明，但显示接纳未完成：${error instanceof Error ? error.message : String(error)}`;
      this.changed();
      throw error;
    } finally {
      this.wholePreparing--;
    }
    const actual = transition.actual;
    this.prepareWholeCommit(transition, finish, commitView)();
    // 请求回执与较新当前观察分别保存；先结束具体写入责任，再消费期间取得的事实。
    if (
      (observed.kind === "unavailable" &&
        (readingLostAfterPreparation ||
          this.latestReliable.revision > actual.revision)) ||
      observed.kind === "deleted" ||
      (observed.kind === "available" &&
        (observed.draft.revision > actual.revision ||
          (observed.draft.revision === actual.revision &&
            !sameContent(observed.draft.content, actual.content))))
    )
      this.observeTarget(observed);
  }
  private prepareWholeCommit(
    transition: WholeDraftTransition,
    finish: () => void,
    commitView?: () => void,
    notify = true,
  ): () => void {
    const next = cloneClientJson(transition.next),
      actual = cloneClientJson(transition.actual);
    const baseline = cloneClientJson(actual.content),
      reliable = cloneClientJson(actual),
      observation = {
        kind: "available" as const,
        draft: cloneClientJson(actual),
        capabilities: this.capabilities,
      };
    return () => {
      this.content = next;
      this.baseline = baseline;
      this.confirmed = actual;
      if (actual.revision > this.latestReliable.revision)
        this.latestReliable = reliable;
      this.revision = actual.revision;
      if (
        !sameContent(transition.origin, next) ||
        transition.reason === "adopt"
      )
        this.version++;
      this.confirmedVersion = sameContent(next, actual.content)
        ? this.version
        : this.confirmedVersion;
      this.conflict = undefined;
      this.error = "";
      this.wholeAcceptance = undefined;
      this.decision = {
        kind: sameContent(next, actual.content)
          ? "saved_content"
          : "same_baseline",
      };
      this.observation = observation;
      finish();
      commitView?.();
      if (notify) this.changed();
    };
  }
  get wholeAcceptanceSnapshot(): WholeAcceptanceSnapshot | undefined {
    const acceptance = this.wholeAcceptance;
    if (!acceptance || this.selectionOperation || this.copyLocked || this.wholeExecution || this.wholePreparing ||
      !["save", "remote", "derived"].includes(acceptance.transition.reason)) return;
    const origin = cloneClientJson(this.content);
    const snapshot = Object.freeze({
      generation: acceptance.generation,
      localVersion: this.version,
      get origin() { return cloneClientJson(origin); },
      observationEpoch: this.observationEpoch,
    });
    this.ordinaryAcceptances.set(snapshot, {acceptance, editGeneration: this.editGeneration,
      operationGeneration: this.operationGeneration, observation: stringifyJson(this.observation), origin,
      sourceId: this.draft.id, sourceCreatedAt: this.draft.createdAt,
      revision: this.revision, confirmedVersion: this.confirmedVersion});
    return snapshot;
  }
  private requireWholeAcceptance(snapshot: WholeAcceptanceSnapshot | undefined, owner?: object) {
    const fixed = snapshot && this.ordinaryAcceptances.get(snapshot);
    if (!fixed || !snapshot || this.wholeAcceptance !== fixed.acceptance ||
      this.selectionOperation || this.selectionExecution || this.copyLocked || this.saving || this.running ||
      (this.wholeExecution && this.wholeExecution !== owner) ||
      !["save", "remote", "derived"].includes(fixed.acceptance.transition.reason) ||
      snapshot.generation !== fixed.acceptance.generation || snapshot.localVersion !== this.version ||
      snapshot.observationEpoch !== this.observationEpoch ||
      fixed.editGeneration !== this.editGeneration || fixed.operationGeneration !== this.operationGeneration ||
      fixed.sourceId !== this.draft.id || fixed.sourceCreatedAt !== this.draft.createdAt ||
      fixed.revision !== this.revision || fixed.confirmedVersion !== this.confirmedVersion ||
      fixed.observation !== stringifyJson(this.observation) || !sameContent(fixed.origin, this.content))
      throw Error("完整接纳的责任或固定资格已改变");
    return fixed.acceptance;
  }
  retryWholeAcceptance(snapshot: WholeAcceptanceSnapshot | undefined) {
    const acceptance = this.requireWholeAcceptance(snapshot), owner = {};
    this.wholeExecution = owner;
    this.changed();
    try {
      this.acceptWhole(acceptance.transition, acceptance.finish, () => this.requireWholeAcceptance(snapshot, owner));
      if (!this.saved && this.canSave) this.schedule();
    } finally {
      if (this.wholeExecution === owner) this.wholeExecution = undefined;
      this.changed();
    }
  }
  private retryWholeAcceptanceOwned(owner?: object) {
    if (this.selectionExecution && this.selectionExecution !== owner)
      throw Error("所选操作正在执行");
    const acceptance = this.wholeAcceptance;
    if (!acceptance) throw Error("没有等待完整接纳的版本");
    const operation = this.selectionOperation;
    if (!owner || !operation || this.selectionExecution !== owner)
      throw Error("所选操作的接纳缺少执行资格");
    if (
      acceptance.transition.reason === "adopt" &&
      this.selectionOperation &&
      !this.selectionStillConfirmed(this.selectionOperation)
    )
      throw Error("采用确认资格已改变，请重新核实并确认完整版本");
    this.acceptWhole(acceptance.transition, acceptance.finish, () => {
      if (this.selectionExecution !== owner || this.selectionOperation !== operation || this.wholeAcceptance !== acceptance)
        throw Error("所选操作的接纳资格已改变");
    });
    if (!this.saved && this.canSave) this.schedule();
  }
  prepareConflictSelection(): ConflictSelectionSnapshot {
    if (!this.canSelect()) throw Error("当前状态不能确认完整版本选择");
    const snapshot = {
      draftId: this.draft.id,
      localVersion: this.version,
      local: cloneClientJson(this.content),
      remote: cloneClientJson(
        (this.observation as Extract<DraftObservation, { kind: "available" }>)
          .draft,
      ),
      observationEpoch: this.observationEpoch,
    };
    this.selections.set(snapshot, cloneClientJson(snapshot));
    return snapshot;
  }
  private canSelect() {
    return (
      this.canEdit &&
      !this.saving &&
      !this.running &&
      this.observation.kind === "available" &&
      !this.observation.draft.exportedRequestId &&
      ["conflict", "superseded_write"].includes(this.decision.kind)
    );
  }
  private requireSelection(snapshot: ConflictSelectionSnapshot) {
    const fixed = this.selections.get(snapshot);
    if (
      !fixed ||
      !this.canSelect() ||
      stringifyJson(fixed) !== stringifyJson(snapshot) ||
      !selectionMatches(
        fixed,
        this.draft.id,
        this.version,
        this.content,
        this.observation,
        this.observationEpoch,
      )
    )
      throw Error("本页或后端版本已改变，请重新确认完整版本");
    return cloneClientJson(fixed);
  }
  private finishSelection() {
    this.cancelScheduledSave();
    this.operationGeneration++;
    this.pendingWrite = undefined;
    this.failedWrite = undefined;
    this.automaticSaveBlocked = false;
    this.abandonedWrites = [];
    this.selectionLocked = false;
    this.selectionOperation = undefined;
    this.editGeneration++;
    this.observationEpoch++;
  }
  adoptSavedVersion(snapshot: ConflictSelectionSnapshot) {
    const fixed = this.requireSelection(snapshot);
    this.selectionOperation = {
      kind: "adopt",
      generation: ++this.selectionGeneration,
      version: this.version,
      input: cloneClientJson(this.content),
      snapshot: fixed,
      epoch: this.observationEpoch,
      phase: "acceptance",
    };
    this.selectionLocked = true;
    this.cancelScheduledSave();
    const owner = {};
    this.selectionExecution = owner;
    try {
      this.acceptWhole(
        {
          draftId: this.draft.id,
          localVersion: this.version,
          origin: this.content,
          next: fixed.remote.content,
          baseline: this.confirmed,
          actual: fixed.remote,
          reason: "adopt",
        },
        () => this.finishSelection(),
      );
    } finally {
      if (this.selectionExecution === owner)
        this.selectionExecution = undefined;
      this.changed();
    }
  }
  async replaceSavedVersion(
    snapshot: ConflictSelectionSnapshot,
  ): Promise<void> {
    const fixed = this.requireSelection(snapshot);
    if (sameContent(fixed.local, fixed.remote.content))
      throw Error("当前完整输入已经保存，请采用已保存版本");
    this.selectionLocked = true;
    // 确认只授权一次条件替换，拒绝后相同R也需要新的明确选择。
    this.observationEpoch++;
    this.cancelScheduledSave();
    if (this.pendingWrite) this.abandonedWrites.push(this.pendingWrite);
    this.operationGeneration++;
    this.selectionOperation = {
      kind: "replace",
      generation: ++this.selectionGeneration,
      version: this.version,
      input: cloneClientJson(this.content),
      snapshot: fixed,
      epoch: this.observationEpoch,
      phase: "unknown",
    };
    const pending: PendingWrite = {
      revision: fixed.remote.revision,
      content: fixed.local,
      baseline: fixed.remote.content,
      baselineDraft: cloneClientJson(fixed.remote),
      version: this.version,
      requestEnded: false,
      generation: this.operationGeneration,
      reason: "replace",
    };
    this.pendingWrite = pending;
    this.failedWrite = undefined;
    this.automaticSaveBlocked = false;
    this.saving = true;
    this.changed();
    const operation = (async () => {
      try {
        await this.sendWrite(pending);
      } catch (error) {
        if (
          !this.wholeAcceptance &&
          (!this.pendingWrite || this.decision.kind === "superseded_write")
        )
          this.selectionLocked = false;
        if (!this.hasConfirmedCurrentInput())
          this.error =
            this.error ||
            (error instanceof Error ? error.message : String(error));
        throw error;
      } finally {
        this.saving = false;
        this.changed();
      }
    })();
    this.running = operation;
    try {
      await operation;
    } finally {
      if (this.running === operation) this.running = undefined;
      this.changed();
    }
  }
  coordinate(capabilities = this.capabilities) {
    if (capabilities) this.capabilities = capabilities;
    if (!capabilities || !this.canSave || this.saving || this.pendingWrite)
      return;
    const next = coordinatePreviews(this.content, capabilities).content;
    if (!sameContent(next, this.content)) {
      this.acceptWhole(
        {
          draftId: this.draft.id,
          localVersion: this.version,
          origin: this.content,
          next,
          baseline: this.confirmed,
          actual: this.confirmed,
          reason: "derived",
        },
        () => {},
      );
      this.schedule();
    }
  }
  async checkExport() {
    const token = this.exportToken, state = this.exportState,
      expected = this.exportSnapshot, epoch = this.observationEpoch,
      sourceId = this.draft.id, createdAt = this.draft.createdAt,
      version = this.version, revision = this.revision,
      confirmedVersion = this.confirmedVersion,
      input = cloneClientJson(this.content);
    const observation = await this.readObservation();
    if (epoch !== this.observationEpoch || token !== this.exportToken ||
      state !== this.exportState || expected !== this.exportSnapshot ||
      sourceId !== this.draft.id || createdAt !== this.draft.createdAt ||
      version !== this.version || revision !== this.revision ||
      confirmedVersion !== this.confirmedVersion || !sameContent(input, this.content))
      throw Error("导出核实已被较新来源观察取代");
    this.observeTarget(observation);
    if (observation.kind !== "available")
      throw Error("导出结果尚未确认，请恢复可靠读取后核实");
    if (this.decision.kind === "invalid" ||
      stringifyJson(this.observation) !== stringifyJson(observation))
      throw Error("导出核实记录不满足当前可靠观察契约");
    if (observation.draft.exportedRequestId)
      this.confirmExport(observation.draft.exportedRequestId);
    else if (state === "unknown" && expected) {
      const actual = observation.draft;
      const original = { ...this.confirmed, revision: expected.revision, content: expected.content };
      const decision = classifyDraftObservation(original, expected.content, observation, { kind: "none" });
      if ((actual.revision === expected.revision && sameContent(actual.content, expected.content)) ||
        decision.kind === "derived" ||
        (observation.capabilities && actual.revision > expected.revision &&
          sameContent(coordinatePreviews(expected.content, observation.capabilities).content, actual.content))) {
        this.exportFailed();
        this.observeTarget(observation);
      } else {
        this.conflict = cloneClientJson(actual);
        this.error = observation.capabilities
          ? "导出结果与已知草稿基线不一致，请核对后端记录。"
          : "导出核实还需要同次能力与草稿观察，当前输入已保留。";
        this.changed();
      }
    }
    return observation.draft;
  }
  accept(draft: Draft) {
    this.observeTarget({ kind: "available", draft });
  }
  bindAppendView(prepare: (transition: VerifiedAppend) => () => void) {
    const previous = this.appendView;
    this.appendView = prepare;
    return () => {
      if (this.appendView === prepare) this.appendView = previous;
    };
  }
  acceptAppend(input: VerifiedAppend) {
    const observed = cloneClientJson(this.observation);
    const transition = cloneClientJson(input),
      { origin, baseline, actual } = transition;
    if (
      this.acceptedAppend &&
      stringifyJson(this.acceptedAppend) === stringifyJson(transition)
    )
      return;
    for (const draft of [origin, baseline, actual]) {
      requireDraftRecord(draft);
      if (draft.createdAt !== this.draft.createdAt)
        throw Error("追加记录的创建身份与当前草稿不一致");
    }
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
      !this.appendLocked ||
      this.appendPreparing ||
      this.saving ||
      this.pendingWrite ||
      !this.saved
    )
      throw Error("当前草稿状态不能接纳追加结果");
    let prior = origin;
    for (const change of transition.baselineChanges) {
      requireDraftRecord(change.actual);
      if (
        change.actual.createdAt !== this.draft.createdAt ||
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
      !transition.allocation?.id ||
      transition.allocation.id !== transition.expected.allocation?.id ||
      !sameContent(
        appendContentAction(
          baseline.content,
          transition.action,
          true,
          transition.allocation,
        ),
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
    const epoch = this.observationEpoch, observation = stringifyJson(this.observation),
      sourceId = this.draft.id, createdAt = this.draft.createdAt,
      revision = this.revision, version = this.version, confirmedVersion = this.confirmedVersion,
      editGeneration = this.editGeneration, operationGeneration = this.operationGeneration,
      appendGeneration = this.appendGeneration, view = this.appendView,
      originInput = cloneClientJson(this.content), baselineInput = cloneClientJson(this.baseline),
      confirmed = stringifyJson(this.confirmed), reliable = stringifyJson(this.latestReliable),
      recovery = stringifyJson(this.recoveryContent),
      selection = this.selectionOperation, selectedCopy = this.selectedCopy,
      whole = this.wholeAcceptance, wholePreparation = this.wholePreparationGeneration,
      wholePreparing = this.wholePreparing, copyExecution = this.copyExecution,
      wholeExecution = this.wholeExecution, selectionExecution = this.selectionExecution,
      exportToken = this.exportToken, exportSnapshot = this.exportSnapshot,
      abandoned = stringifyJson(this.abandonedWrites), recoveries = stringifyJson(this.pendingCopyRecoveries);
    // 副本和显示准备全部在首个共同提交前完成；外部准备不能授予旧依据新的资格。
    const next = cloneClientJson(actual.content), nextBaseline = cloneClientJson(actual.content),
      nextConfirmed = cloneClientJson(actual), nextReliable = cloneClientJson(actual),
      nextObservation: DraftObservation = {
        kind: "available", draft: cloneClientJson(actual),
        capabilities: cloneClientJson(transition.capabilities),
      };
    let commitView: (() => void) | undefined;
    try {
      this.appendPreparing = true;
      commitView = view?.(cloneClientJson(transition));
      if (epoch !== this.observationEpoch || observation !== stringifyJson(this.observation) ||
        sourceId !== this.draft.id || createdAt !== this.draft.createdAt ||
        revision !== this.revision || version !== this.version || confirmedVersion !== this.confirmedVersion ||
        editGeneration !== this.editGeneration || operationGeneration !== this.operationGeneration ||
        appendGeneration !== this.appendGeneration || !this.appendLocked || view !== this.appendView ||
        !sameContent(originInput, this.content) || !sameContent(baselineInput, this.baseline) ||
        confirmed !== stringifyJson(this.confirmed) || reliable !== stringifyJson(this.latestReliable) ||
        recovery !== stringifyJson(this.recoveryContent) || selection !== this.selectionOperation ||
        selectedCopy !== this.selectedCopy || whole !== this.wholeAcceptance ||
        wholePreparation !== this.wholePreparationGeneration || wholePreparing !== this.wholePreparing ||
        copyExecution !== this.copyExecution || wholeExecution !== this.wholeExecution ||
        selectionExecution !== this.selectionExecution || exportToken !== this.exportToken ||
        exportSnapshot !== this.exportSnapshot || abandoned !== stringifyJson(this.abandonedWrites) ||
        recoveries !== stringifyJson(this.pendingCopyRecoveries) || this.selectionLocked || this.copyLocked ||
        this.exportState !== "editable" || this.deletionState !== "idle" || this.saving || this.pendingWrite ||
        !this.saved)
        throw Error("追加完整接纳前目标、输入或操作资格已改变，请重新核实");
    } finally {
      this.appendPreparing = false;
    }
    this.content = next;
    this.baseline = nextBaseline;
    this.confirmed = nextConfirmed;
    if (actual.revision > this.latestReliable.revision)
      this.latestReliable = nextReliable;
    this.revision = actual.revision;
    this.version++;
    this.confirmedVersion = this.version;
    this.error = "";
    this.conflict = undefined;
    this.acceptedAppend = transition;
    this.observation = nextObservation;
    this.decision = { kind: "saved_content" };
    commitView?.();
    this.appendGeneration++;
    this.appendLocked = false;
    this.changed();
    if (
      observed.kind === "deleted" ||
      (observed.kind === "unavailable" &&
        this.latestReliable.revision > actual.revision) ||
      (observed.kind === "available" &&
        observed.draft.revision > actual.revision)
    )
      this.observeTarget(observed);
  }
}

/** 调用方暂停新操作；未知导出由各会话自己核实，兼容回调不消费操作责任。 */
export async function prepareSessionsForReload(
  sessions: DraftSession[],
  _observe?: () => Promise<void>,
) {
  try {
    for (const session of sessions) {
      if (session.exportState === "unknown") await session.checkExport();
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
