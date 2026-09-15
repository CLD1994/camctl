import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { Store } from "./database";
import {
  AppError,
  errorMessage,
  type Draft,
  type DraftContent,
  type ExportedRequest,
  type Preset,
  type ImportFile,
} from "./models";
import { parseJson } from "../shared/json";
import { loadCapabilities, validateParams } from "../shared/capabilities";
import { validatePlan } from "../shared/plan";
import type { Capabilities, StatusReport } from "../shared/types";
import {
  mergeReport,
  parseReport,
  reportDecision,
  selectSyncReport,
  validateReportAgainstHistory,
} from "../domain/reports";

function utc() {
  return new Date().toISOString().replace("T", " ").replace(/Z$/, "");
}
function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
export class Application {
  readonly store: Store;
  capabilities: {
    active: Capabilities | null;
    error: string | null;
    generation: number;
  } = { active: null, error: null, generation: 0 };
  constructor(directory: string) {
    this.store = new Store(directory);
    this.reloadCapabilities();
  }
  reloadCapabilities() {
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
      this.capabilities = {
        active,
        error: null,
        generation: this.capabilities.generation + 1,
      };
    } catch (error) {
      this.capabilities = { ...this.capabilities, error: errorMessage(error) };
    }
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
  }
  createDraft(
    content: DraftContent = {
      text: JSON.stringify({ name: "新计划", actions: [] }, null, 2),
    },
  ): Draft {
    this.checkContent(content);
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
      const updated = {
        ...d,
        content,
        revision: d.revision + 1,
        updatedAt: utc(),
      };
      this.store.set("drafts", id, updated);
      return updated;
    });
  }
  coverage(): number {
    return this.store.businessState().coverage;
  }
  snapshot(): StatusReport {
    return this.store.businessState().snapshot;
  }
  ackId(): number | null {
    const state = this.store.businessState();
    return selectSyncReport(state.reports, state.coverage);
  }
  validateContent(content: DraftContent) {
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
    // 请求身份和生成时间属于首次导出，由后端分配，编辑意图不包含这些字段。
    const plan = { ...value, request_id: "validation", created_at: utc() };
    const issues = validatePlan(plan, this.capabilities.active, {
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
      const value = this.validateContent(content);
      const requestId = randomUUID();
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
    const { request_id, created_at, last_report_id, ...intent } =
      this.request(id).body;
    return this.createDraft({ text: JSON.stringify(intent, null, 2) });
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
    const issues = validateParams(
      input.deviceId,
      input.actionType,
      input.params,
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
  ): Draft {
    const draft = this.draft(id);
    const parsed = parseJson(draft.content.text);
    if (!object(parsed) || !Array.isArray(parsed.actions))
      throw new AppError(
        "invalid_draft",
        "请先修正草稿 JSON 和动作列表，原输入已保留",
      );
    const names = new Set(parsed.actions.filter(object).map((a) => a.name));
    let name = String(action.name ?? "后续动作");
    const base = name;
    let n = 2;
    while (names.has(name)) name = `${base} ${n++}`;
    const text = JSON.stringify(
      { ...parsed, actions: [...parsed.actions, { ...action, name }] },
      null,
      2,
    );
    return this.saveDraft(id, revision, { ...draft.content, text });
  }
  applyReports(inputs: Array<{ file: ImportFile; bytes: Uint8Array }>): void {
    this.store.businessState();
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
      try {
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
      return { startup, capabilities: this.capabilities };
    const imports = this.store.all<ImportFile>("imports");
    const coverage = this.coverage();
    const gaps = imports
      .filter((f) => f.status === "gap" && f.toWm !== undefined)
      .map((f) => f.toWm!);
    const gapTarget = gaps.length ? Math.max(...gaps) : null;
    return {
      startup,
      capabilities: this.capabilities,
      drafts: this.store.all<Draft>("drafts"),
      requests: this.store.all<ExportedRequest>("requests"),
      presets: this.store.all<Preset>("presets"),
      snapshot: this.snapshot(),
      reports: this.store.reports(),
      imports,
      coverage,
      gapTarget,
      historyMissing: gapTarget !== null && coverage < gapTarget,
      ackId: this.ackId(),
    };
  }
}
