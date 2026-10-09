import { randomUUID } from "node:crypto";
import {
  CryptoRandomSource,
  MAX_REQUEST_ID,
  MAX_ID_RESELECTIONS,
  type RandomSource,
} from "../domain/request-id";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { Store } from "./database";
import {
  AppError,
  DRAFT_COMMON_ACTION_FIELDS,
  errorMessage,
  type Draft,
  type DraftContent,
  type ExportedRequest,
  type Preset,
  type ImportFile,
} from "./models";
import {
  parseJson,
  parseClientJson,
  stringifyJson,
  exactJsonIdentity,
  motorInputTexts,
  type MotorInputText,
  MOTOR_ORIGINAL_INPUT_FIELDS,
  cloneClientJson,
} from "../shared/json";
import {
  coordinatePreviews,
  initializePreviewMetadata,
  copyDraftContent,
  validPreviewMetadata,
  sameContent,
  appendContentAction,
  copyDraftAction,
  type CapabilityState,
} from "../shared/automatic-previews";
import { isCameraAction } from "../shared/actions";
import { loadCapabilities, validateParams } from "../shared/capabilities";
import { validatePlan } from "../shared/plan";
import type { StatusReport } from "../shared/types";
import {
  mergeReport,
  parseReport,
  reportDecision,
  selectSyncReport,
  validateReportAgainstHistory,
} from "../domain/reports";
import { formatProtocolTime, type UtcText } from "../domain/protocol-time";

function utc(): UtcText {
  // toISOString 恒为 UTC 且格式固定；截到秒符合公共协议时间字面量。
  return formatProtocolTime(
    new Date().toISOString().slice(0, 19).replace("T", " "),
  );
}
function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function allocateRequestId(
  random: RandomSource,
  used: (id: string) => boolean,
): string {
  for (let attempt = 0; attempt <= MAX_ID_RESELECTIONS; attempt += 1) {
    const candidate = random.next();
    if (candidate < 1n || candidate > MAX_REQUEST_ID) {
      throw new AppError("request_id_fault", "随机源产生越界请求身份");
    }
    if (!used(candidate.toString(10))) return candidate.toString(10);
  }
  throw new AppError(
    "request_id_fault",
    `请求身份重选耗尽（${MAX_ID_RESELECTIONS + 1} 次冲突）`,
  );
}

export class Application {
  private motorInputsReady = false;
  private ensureMotorInputs() {
    if (this.motorInputsReady) return;
    if (!this.store.businessState().reports.length) {
      this.motorInputsReady = true;
      return;
    }
    this.store.transaction(() => {
      const snapshot = this.store.businessState().snapshot;
      const current = new Map(
        snapshot.plans
          ?.flatMap((p) => p.actions ?? [])
          .filter((a) => a.type === "motor_control")
          .map((a) => [a.action_instance_id, a]) ?? [],
      );
      const restored = new Set<string>();
      const firstInputs = new Map<string, MotorInputText>();
      for (const source of this.store.acceptedReportSources()) {
        const report = parseReport(source.file_name, source.bytes);
        const inputs = motorInputTexts(
          new TextDecoder("utf-8", { fatal: true }).decode(source.bytes),
        );
        for (const [id, input] of Object.entries(inputs)) {
          const first = firstInputs.get(id);
          if (first) {
            if (first.inputIdentity !== input.inputIdentity)
              throw new Error(
                "已接受报告的电机原参数互相矛盾，无法恢复派生信息",
              );
            continue;
          }
          firstInputs.set(id, input);
          const saved = this.store.get<Partial<MotorInputText>>(
            "motor_input_texts",
            id,
          );
          if (
            saved?.inputIdentity !== input.inputIdentity ||
            saved?.text !== input.text
          )
            this.store.set("motor_input_texts", id, input);
        }
        for (const original of report.plans?.flatMap((p) => p.actions ?? []) ??
          []) {
          const target = current.get(original.action_instance_id);
          if (
            !target ||
            original.type !== "motor_control" ||
            restored.has(original.action_instance_id)
          )
            continue;
          for (const key of MOTOR_ORIGINAL_INPUT_FIELDS) {
            if (Object.hasOwn(original, key))
              Reflect.set(target, key, Reflect.get(original, key));
            else Reflect.deleteProperty(target, key);
          }
          restored.add(original.action_instance_id);
        }
      }
      if (restored.size) this.store.set("state", "snapshot", snapshot);
    });
    this.motorInputsReady = true;
  }
  readonly store: Store;
  capabilities: CapabilityState = {
    active: null,
    error: null,
    generation: 0,
    version: randomUUID(),
  };
  private readonly random: RandomSource;
  constructor(
    directory: string,
    random: RandomSource = new CryptoRandomSource(),
  ) {
    this.random = random;
    this.store = new Store(directory);
    this.reloadCapabilities();
  }
  reloadCapabilities() {
    let next: CapabilityState;
    try {
      const active = loadCapabilities(
        parseJson(
          new TextDecoder("utf-8", { fatal: true }).decode(
            readFileSync(
              join(this.store.directory, "device-capabilities.json"),
            ),
          ),
        ),
      );
      next = {
        active,
        error: null,
        generation: this.capabilities.generation + 1,
        version: randomUUID(),
      };
    } catch (error) {
      this.capabilities = { ...this.capabilities, error: errorMessage(error) };
      return this.capabilities;
    }
    // 数据库故障不属于能力加载失败，也不允许以空库替代。
    const startup = this.store.status();
    if (startup.state === "ready")
      this.store.transaction(() => {
        for (const draft of this.store.all<Draft>("drafts")) {
          this.checkContent(draft.content);
          if (draft.exportedRequestId) continue;
          const content = coordinatePreviews(draft.content, next).content;
          if (!sameContent(content, draft.content))
            this.store.set("drafts", draft.id, {
              ...draft,
              content,
              revision: draft.revision + 1,
              updatedAt: utc(),
            });
        }
      });
    this.capabilities = next;
    return this.capabilities;
  }
  draft(id: string): Draft {
    const d = this.store.get<Draft>("drafts", id);
    if (!d) throw new AppError("not_found", "草稿不存在", 404);
    return d;
  }
  request(id: string): ExportedRequest {
    const r = this.store.get<ExportedRequest>("requests", id);
    if (!r) throw new AppError("not_found", "原请求不存在", 404);
    return r;
  }
  private checkContent(content: DraftContent) {
    if (!object(content) || typeof content.text !== "string")
      throw new AppError("invalid_content", "草稿必须包含编辑文本");
    if (
      content.automaticPreviews !== undefined &&
      !validPreviewMetadata(content.automaticPreviews)
    )
      throw new AppError("invalid_content", "自动预览编辑资料格式不正确");
    if (
      content.pending !== undefined &&
      (!object(content.pending) ||
        Object.values(content.pending).some(
          (v) =>
            !object(v) ||
            !["number", "json"].includes(String(v.kind)) ||
            typeof v.text !== "string",
        ))
    )
      throw new AppError("invalid_content", "未完成输入格式不正确");
    if (content.actionVariants !== undefined) {
      const fail = () => {
        throw new AppError("invalid_content", "动作类型编辑资料格式不正确");
      };
      if (!object(content.actionVariants)) fail();
      for (const [index, variants] of Object.entries(content.actionVariants)) {
        if (
          !/^(0|[1-9]\d*)$/.test(index) ||
          !Number.isSafeInteger(Number(index)) ||
          !Array.isArray(variants)
        )
          fail();
        for (const variant of variants) {
          if (
            !object(variant) ||
            !object(variant.fields) ||
            !object(variant.pending) ||
            Object.keys(variant).some(
              (key) =>
                !["type", "fields", "fieldsText", "pending"].includes(key),
            ) ||
            Object.keys(variant.fields).some((key) =>
              [...DRAFT_COMMON_ACTION_FIELDS, "type"].includes(key),
            ) ||
            Object.entries(variant.pending).some(
              ([key, value]) =>
                !/^\/(?:[^~]|~[01])*$/.test(key) ||
                DRAFT_COMMON_ACTION_FIELDS.some(
                  (field) =>
                    key === `/${field}` || key.startsWith(`/${field}/`),
                ) ||
                !object(value) ||
                !["number", "json"].includes(String(value.kind)) ||
                typeof value.text !== "string",
            )
          )
            fail();
          if (variant.fieldsText !== undefined) {
            try {
              if (typeof variant.fieldsText !== "string") fail();
              const fields = parseClientJson(variant.fieldsText!);
              if (
                !object(fields) ||
                exactJsonIdentity(fields) !== exactJsonIdentity(variant.fields)
              )
                fail();
            } catch {
              fail();
            }
          }
        }
      }
    }
  }
  createDraft(
    content: DraftContent = initializePreviewMetadata(
      { text: JSON.stringify({ name: "新计划", actions: [] }, null, 2) },
      "enabled",
      randomUUID(),
    ),
  ): Draft {
    this.checkContent(content);
    content = coordinatePreviews(
      copyDraftContent(content, randomUUID()),
      this.capabilities,
    ).content;
    const now = utc();
    const d: Draft = {
      id: randomUUID(),
      revision: 1,
      content,
      createdAt: now,
      updatedAt: now,
    };
    this.store.set("drafts", d.id, d);
    return d;
  }
  saveDraft(id: string, revision: number, content: DraftContent): Draft {
    this.checkContent(content);
    return this.store.transaction(() => {
      const d = this.draft(id);
      if (d.exportedRequestId)
        throw new AppError(
          "already_exported",
          "草稿已经导出，请打开计划记录",
          409,
        );
      if (d.revision !== revision)
        throw new AppError(
          "revision_conflict",
          "草稿已发生变化，请重新打开并核对当前输入",
          409,
        );
      const coordinated = coordinatePreviews(
        content,
        this.capabilities,
      ).content;
      const updated = {
        ...d,
        content: coordinated,
        revision: d.revision + 1,
        updatedAt: utc(),
        lastWrite: {
          revision: d.revision + 1,
          input: cloneClientJson(content),
          content: coordinated,
        },
      };
      this.store.set("drafts", id, updated);
      return updated;
    });
  }
  deleteDraft(id: string, revision: number): void {
    this.store.transaction(() => {
      const draft = this.store.get<Draft>("drafts", id);
      if (!draft) return;
      if (draft.exportedRequestId)
        throw new AppError(
          "already_exported",
          "草稿已经导出，请打开计划记录",
          409,
        );
      if (draft.revision !== revision)
        throw new AppError(
          "revision_conflict",
          "草稿已发生变化，请重新打开并核对后删除",
          409,
        );
      this.store.remove("drafts", id);
    });
  }
  coverage(): number {
    return this.store.businessState().coverage;
  }
  snapshot(): StatusReport {
    this.ensureMotorInputs();
    return this.store.businessState().snapshot;
  }
  ackId(): string | null {
    const state = this.store.businessState();
    return selectSyncReport(state.reports, state.coverage);
  }
  validateContent(content: DraftContent, capabilities = this.capabilities) {
    this.checkContent(content);
    if (Object.keys(content.pending ?? {}).length)
      throw new AppError(
        "unfinished_input",
        "仍有未完成的参数或数值输入，请修正后导出",
      );
    let value: unknown;
    try {
      value = parseJson(content.text);
    } catch (error) {
      throw new AppError("invalid_json", errorMessage(error));
    }
    if (!object(value))
      throw new AppError("invalid_plan", "计划必须是 JSON 对象");
    if (
      (capabilities.error || !capabilities.active) &&
      Array.isArray(value.actions) &&
      value.actions.some((a) => object(a) && isCameraAction(a.type))
    )
      throw new AppError(
        "capabilities_unavailable",
        "本计划需要可靠的能力说明，当前说明加载失败或不可用",
      );
    // 请求身份和生成时间属于首次导出，由后端分配，编辑意图不包含这些字段。
    const plan = { ...value, request_id: "1", created_at: utc() };
    const issues = validatePlan(plan, capabilities.active, {
      reports: this.store.reports(),
      coverage: this.coverage(),
    });
    if (issues.length)
      throw new AppError("invalid_plan", "计划校验未通过", 400, issues);
    return value;
  }
  exportDraft(
    id: string,
    revision: number,
    content: DraftContent,
    capabilityVersion: string | null | undefined = this.capabilities.version,
  ): ExportedRequest {
    return this.store.transaction(() => {
      const draft = this.draft(id);
      if (draft.exportedRequestId) return this.request(draft.exportedRequestId);
      if (draft.revision !== revision)
        throw new AppError(
          "revision_conflict",
          "草稿已发生变化，请核对后重新导出",
          409,
        );
      if (!sameContent(draft.content, content))
        throw new AppError(
          "content_conflict",
          "完整草稿内容与保存版本不一致",
          409,
        );
      const capabilities = this.capabilities;
      if (
        typeof capabilityVersion !== "string" ||
        capabilityVersion !== capabilities.version
      )
        throw new AppError(
          "capabilities_changed",
          "能力说明已经变化，请重新核对草稿",
          409,
        );
      const coordinated = coordinatePreviews(content, capabilities);
      if (!sameContent(content, coordinated.content))
        throw new AppError(
          "content_conflict",
          "自动预览需要重新协调和保存",
          409,
        );
      const value = this.validateContent(content, capabilities);
      if (
        coordinated.issues.length &&
        content.automaticPreviews?.intent !== "unset"
      )
        throw new AppError(
          "invalid_preview_metadata",
          "自动预览关联尚不能可靠确认",
          400,
          coordinated.issues,
        );
      const requestId = allocateRequestId(this.random, (id) => {
        try {
          return this.store.get<ExportedRequest>("requests", id) !== undefined;
        } catch (error) {
          throw new AppError(
            "request_id_fault",
            `请求身份查询失败：${errorMessage(error)}`,
          );
        }
      });
      const now = utc();
      const {
        last_report_id: ack,
        request_id: oldId,
        created_at: oldTime,
        ...intent
      } = value;
      const record: ExportedRequest = {
        id: requestId,
        draftId: id,
        body: { ...intent, request_id: requestId, created_at: now },
        exportedAt: now,
        handedAt: null,
        copyContent: cloneClientJson(content),
      };
      this.store.set("requests", requestId, record);
      this.store.set("drafts", id, {
        ...draft,
        content,
        revision: draft.revision + 1,
        updatedAt: now,
        exportedRequestId: requestId,
      });
      return record;
    });
  }
  downloadRequest(id: string): Record<string, unknown> {
    const request = this.request(id);
    const ack = this.ackId();
    return ack === null
      ? request.body
      : { ...request.body, last_report_id: ack };
  }
  copyRequest(id: string): Draft {
    const original = this.request(id);
    const { request_id, created_at, last_report_id, ...intent } = original.body;
    return this.createDraft({
      text: stringifyJson(intent, 2),
      ...(original.copyContent?.automaticPreviews
        ? { automaticPreviews: original.copyContent.automaticPreviews }
        : {}),
    });
  }
  copyDraft(id: string): Draft {
    return this.createDraft(this.draft(id).content);
  }
  copyAction(id: string, revision: number, index: number): Draft {
    return this.saveDraft(
      id,
      revision,
      copyDraftAction(this.draft(id).content, index),
    );
  }
  markHandoff(id: string, marked: boolean): ExportedRequest {
    return this.store.transaction(() => {
      const current = this.request(id);
      const next = {
        ...current,
        handedAt: marked ? (current.handedAt ?? utc()) : null,
      };
      this.store.set("requests", id, next);
      return next;
    });
  }
  savePreset(input: {
    id?: string;
    name: string;
    deviceId: string;
    actionType: string;
    params: unknown;
  }): Preset {
    if (typeof input.name !== "string" || !input.name.trim())
      throw new AppError("invalid_preset", "请填写预设名称");
    let params: unknown;
    try {
      params = parseJson(stringifyJson(input.params));
    } catch (error) {
      throw new AppError(
        "invalid_preset",
        `预设参数不属于合法公共 JSON：${errorMessage(error)}`,
      );
    }
    const issues = validateParams(
      input.deviceId,
      input.actionType,
      params,
      this.capabilities.active,
    );
    if (issues.length)
      throw new AppError("invalid_preset", "预设参数校验未通过", 400, issues);
    return this.store.transaction(() => {
      if (input.id) {
        const prior = this.store.get<Preset>("presets", input.id);
        if (!prior) throw new AppError("not_found", "预设不存在", 404);
        if (
          prior.deviceId !== input.deviceId ||
          prior.actionType !== input.actionType
        )
          throw new AppError("preset_target", "更新预设不能改变设备或动作类型");
      }
      const preset: Preset = {
        ...input,
        params,
        id: input.id ?? randomUUID(),
        updatedAt: utc(),
      };
      this.store.set("presets", preset.id, preset);
      return preset;
    });
  }
  syncParams(full = false): Record<string, unknown> {
    const state = this.store.businessState();
    const id = full ? null : selectSyncReport(state.reports, state.coverage);
    return id === null
      ? { scope: "full" }
      : { scope: "since", after_report_id: id };
  }
  appendAction(
    id: string,
    revision: number,
    action: Record<string, unknown>,
    expectedContent?: DraftContent,
    capabilityVersion?: string,
  ): Draft {
    const draft = this.draft(id);
    if (
      capabilityVersion !== undefined &&
      capabilityVersion !== this.capabilities.version
    )
      throw new AppError("capabilities_changed", "能力说明已经变化", 409);
    const content = coordinatePreviews(
      appendContentAction(draft.content, action, true),
      this.capabilities,
    ).content;
    if (expectedContent && !sameContent(content, expectedContent))
      throw new AppError(
        "content_conflict",
        "追加的完整预期与当前依据不一致",
        409,
      );
    return this.saveDraft(id, revision, content);
  }
  applyReports(inputs: Array<{ file: ImportFile; bytes: Uint8Array }>): void {
    this.store.businessState();
    this.ensureMotorInputs();
    const remaining: Array<{
      file: ImportFile;
      bytes: Uint8Array;
      report: StatusReport;
    }> = [];
    for (const input of inputs) {
      try {
        const report = parseReport(input.file.fileName, input.bytes);
        remaining.push({ ...input, report });
      } catch (error) {
        this.store.set("imports", input.file.id, {
          ...input.file,
          status: "failed",
          message: errorMessage(error),
        });
      }
    }
    while (remaining.length) {
      remaining.sort((a, b) => a.report.to_wm - b.report.to_wm);
      const coverage = this.coverage();
      let index = remaining.findIndex(
        (e) =>
          this.store.report(e.report.report_id) !== undefined ||
          e.report.from_wm <= coverage ||
          e.report.to_wm <= coverage,
      );
      if (index < 0) index = 0;
      const item = remaining.splice(index, 1)[0];
      const { report, bytes } = item;
      const file = {
        ...item.file,
        reportId: report.report_id,
        fromWm: report.from_wm,
        toWm: report.to_wm,
      };
      const existing = this.store.report(report.report_id);
      if (existing) {
        const same = Buffer.from(existing.bytes).equals(Buffer.from(bytes));
        this.store.set("imports", file.id, {
          ...file,
          status: same ? "duplicate" : "conflict",
          message: same
            ? "报告已接受，复用原记录"
            : "同一报告身份的内容发生冲突",
        });
        continue;
      }
      const decision = reportDecision(coverage, report.from_wm, report.to_wm);
      const current = this.snapshot();
      let merged: StatusReport | undefined;
      const motorTexts = motorInputTexts(
        new TextDecoder("utf-8", { fatal: true }).decode(bytes),
      );
      try {
        for (const [id, value] of Object.entries(motorTexts)) {
          const saved = this.store.get<MotorInputText>("motor_input_texts", id);
          if (saved && saved.inputIdentity !== value.inputIdentity)
            throw new Error("电机动作原始参数的数学值或类型改变");
        }
        validateReportAgainstHistory(current, report);
        if (decision !== "gap") merged = mergeReport(current, report);
      } catch (error) {
        this.store.set("imports", file.id, {
          ...file,
          status: "failed",
          message: errorMessage(error),
        });
        continue;
      }
      if (decision === "gap") {
        this.store.set("imports", file.id, {
          ...file,
          status: "gap",
          message: `缺少历史：本地完整进度 ${coverage}，需要补齐至 ${report.to_wm}`,
        });
        continue;
      }
      // 数据保存失败向调用者传播，停止依赖当前水位的后续应用。
      this.store.transaction(() => {
        this.store.saveReport(
          report.report_id,
          file.fileName,
          bytes,
          report.from_wm,
          report.to_wm,
        );
        for (const [id, value] of Object.entries(motorTexts))
          if (!this.store.get<MotorInputText>("motor_input_texts", id))
            this.store.set("motor_input_texts", id, value);
        if (decision === "apply") {
          this.store.set("state", "snapshot", merged);
          this.store.set("state", "coverage", report.to_wm);
        }
        this.store.set("imports", file.id, {
          ...file,
          status: decision === "apply" ? "accepted" : "covered",
          message:
            decision === "apply" ? "报告已应用" : "报告已接受，范围已完整覆盖",
        });
      });
    }
  }
  state() {
    const startup = this.store.status();
    if (startup.state !== "ready")
      return {
        startup,
        capabilities: this.capabilities,
        motorInputTexts: {} as Record<string, string>,
      };
    this.ensureMotorInputs();
    const imports = this.store.all<ImportFile>("imports");
    const coverage = this.coverage();
    const gaps = imports
      .filter((f) => f.status === "gap" && f.toWm !== undefined)
      .map((f) => f.toWm!);
    const gapTarget = gaps.length ? Math.max(...gaps) : null;
    const snapshot = this.snapshot();
    return {
      startup,
      capabilities: this.capabilities,
      drafts: this.store.all<Draft>("drafts"),
      requests: this.store.all<ExportedRequest>("requests"),
      presets: this.store.all<Preset>("presets"),
      snapshot,
      motorInputTexts: Object.fromEntries(
        snapshot.plans?.flatMap((plan) =>
          (plan.actions ?? [])
            .filter((action) => action.type === "motor_control")
            .flatMap((action) => {
              const value = this.store.get<MotorInputText>(
                "motor_input_texts",
                action.action_instance_id,
              );
              return value?.text !== undefined
                ? [[action.action_instance_id, value.text]]
                : [];
            }),
        ) ?? [],
      ) as Record<string, string>,
      reports: this.store.reports(),
      imports,
      coverage,
      gapTarget,
      historyMissing: gapTarget !== null && coverage < gapTarget,
      ackId: this.ackId(),
    };
  }
}
