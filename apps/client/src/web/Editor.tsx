import { Select, SelectItem, SelectFieldset } from "./Select";
import { isCameraAction } from "../shared/actions";
import { useState, useLayoutEffect, useRef, useMemo } from "react";
import { useFeedback } from "./feedback";
import type { DraftContent, Preset } from "../server/models";
import type { Capabilities, Issue, ParameterType } from "../shared/types";
import { ACTION_TYPES, validatePlan } from "../shared/plan";
import { validateParams } from "../shared/capabilities";
import { isObject } from "../shared/validation";
import { parseJson, cloneClientJson, stringifyJson } from "../shared/json";
import {
  previewIntent,
  setPreviewIntent,
  coordinatePreviews,
  copyDraftAction,
  createActionAllocation,
  initializePreviewMetadata,
  isAutomaticPreviewAction,
  type CapabilityState,
} from "../shared/automatic-previews";
import {
  parseDraft,
  setValue,
  editPlanText,
  editValue,
  removeAction,
  appendDraftAction,
  pointer,
  valueAt,
  pointerPath,
  checkActionListChange,
  resolveField,
  localToUtc,
  utcToLocal,
  type Path,
  type EditObject,
} from "./editing";
import { DraftSession, sameContent, type DraftSaveStatus } from "./session";
import { identityForSession } from "./draft-session-view";
import { ActionUiIdentity } from "./action-ui-identity";
import { actionLabel, Issues, ErrorBox } from "./common";
import { Field, JsonField } from "./Fields";
import { ObjectBranchField } from "./ObjectBranchField";
import { objectBranches } from "./object-branches";
import {
  estimateDraftAction,
  type EstimateReloadPhase,
} from "./video-estimate-state";
import { VideoSizeEstimate } from "./VideoSizeEstimate";
import { PreviewControl } from "./PreviewControl";
import { BuiltinFields } from "./BuiltinFields";
import { presentIssues, presentPendingInput } from "./validation-presentation";
import {
  ValidationControl,
  ValidationProvider,
  type PresentedIssue,
} from "./Validation";
import { deviceOptions } from "./device-options";
import { userActionOrdinals } from "./action-order";
import { canSwitchActionType, switchActionType } from "./action-drafts";
import {
  canSwitchParameterType,
  switchParameterType,
} from "./parameter-drafts";
import {
  parameterOptions,
  compatibleValues,
  parameterRequired,
  parameterChoiceBlocked,
  parameterFieldOptions,
} from "./parameter-options";

interface Props {
  session: DraftSession;
  capabilities: Capabilities | null;
  capabilityState: CapabilityState;
  estimateReloadPhase: EstimateReloadPhase;
  presets: Preset[];
  reports: Array<{ report_id: string; to_wm: number }>;
  coverage: number;
  busy: boolean;
  onExport: () => void;
  onDelete: () => void;
  onCopy: () => void;
  checkDeletion: () => void;
  checkExport: () => void;
  onConflict?: () => void;
  onSaveAs?: () => void;
  savePreset: (input: {
    id?: string;
    name: string;
    deviceId: string;
    actionType: string;
    params: unknown;
  }) => Promise<Preset>;
}
export function Editor(props: Props) {
  const { session, capabilities, busy } = props;
  const [json, setJson] = useState(false);
  const [copyError, setCopyError] = useState("");
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const editor = useRef<HTMLElement>(null);
  const [focusTarget, setFocusTarget] = useState<{
    path: string | null;
    pending: boolean;
    sequence: number;
  }>();
  const content = session.content;
  const saveVerification = session.saveVerification;
  const wholeAcceptance = session.wholeAcceptanceSnapshot;
  const [actionIdentity] = useState(() => identityForSession(session));
  useLayoutEffect(() => {
    const releaseAppend = session.bindAppendView((transition) =>
      actionIdentity.prepareAppend(transition),
    );
    const releaseWhole = session.bindWholeView((transition) =>
      actionIdentity.prepareWhole(transition),
    );
    return () => {
      releaseAppend();
      releaseWhole();
    };
  }, [session, actionIdentity]);
  let identityProblem = "";
  try {
    actionIdentity.update(content);
  } catch (error) {
    identityProblem = (error as Error).message;
  }
  if (
    !identityProblem &&
    [...collapsed].some((key) => !actionIdentity.keys.includes(key))
  )
    setCollapsed(
      new Set(
        [...collapsed].filter((key) => actionIdentity.keys.includes(key)),
      ),
    );
  const intent = previewIntent(content);
  const previewIssues = useMemo(
    () => coordinatePreviews(content, props.capabilityState).issues,
    [
      content,
      props.capabilityState.active,
      props.capabilityState.error,
      props.capabilityState.generation,
      props.capabilityState.version,
    ],
  );
  const identity = (index: number) => actionIdentity.keys[index];
  const change = (
    next: DraftContent,
    operation?: Parameters<ActionUiIdentity["update"]>[1],
    binding?: DraftContent,
  ) => {
    setCopyError("");
    const prepared = session.prepareEdit(next);
    actionIdentity.update(
      next,
      operation,
      () => {
        session.commitEdit(prepared);
        return session.content;
      },
      binding,
      prepared.content,
    );
    setCollapsed(
      (current) =>
        new Set(
          [...current].filter((key) => actionIdentity.keys.includes(key)),
        ),
    );
    session.schedule();
  };
  const choosePreviewIntent = (selected: "enabled" | "disabled") => {
    if (busy || !session.editable || identityProblem) return;
    const namespace = crypto.randomUUID();
    const prepared = content.automaticPreviews
      ? content
      : initializePreviewMetadata(content, selected, namespace);
    // 首次显式选择给原位置绑定共享规则创建的身份，再跟踪协调后的增删。
    change(
      setPreviewIntent(prepared, selected, namespace, props.capabilityState)
        .content,
      undefined,
      prepared,
    );
  };
  const { plan, problem } = useMemo(() => {
    try {
      return { plan: parseDraft(content), problem: "" };
    } catch (error) {
      return { plan: undefined, problem: (error as Error).message };
    }
  }, [content]);
  const actionOrdinals = useMemo(
    () => userActionOrdinals(plan?.actions ?? []),
    [plan],
  );
  const actionCount = actionOrdinals.filter(
    (ordinal) => ordinal !== undefined,
  ).length;
  // 计划校验只消费报告身份及水位，轮询携带的其他报告字段不改变依据。
  const reportBasis = JSON.stringify(
    props.reports.map((r) => [r.report_id, r.to_wm]),
  );
  const issues = useMemo(() => {
    const issues: Issue[] = [];
    try {
      for (const [path, value] of Object.entries(content.pending ?? {}))
        issues.push({
          path,
          code: "unfinished_input",
          message: `尚未完成的${value.kind === "json" ? "JSON" : "数值"}输入：${value.text}`,
        });
      const value = parseJson(content.text);
      if (!isObject(value))
        issues.push({
          path: "",
          code: "invalid_plan",
          message: "计划必须是对象",
        });
      else
        issues.push(
          ...validatePlan(
            {
              ...value,
              request_id: "1",
              created_at: "2026-01-01 00:00:00",
            },
            capabilities,
            { reports: props.reports, coverage: props.coverage },
          ),
        );
    } catch (error) {
      issues.push({
        path: "",
        code: "invalid_json",
        message: (error as Error).message,
      });
    }
    return issues;
  }, [content, capabilities, reportBasis, props.coverage]);
  const add = () => {
    if (plan) {
      const names = new Set(
        plan.actions.filter(isObject).map((action) => action.name),
      );
      let ordinal = actionCount + 1;
      while (names.has(`动作 ${ordinal}`)) ordinal++;
      change(
        appendDraftAction(
          content,
          {
            name: `动作 ${ordinal}`,
          },
          createActionAllocation(),
        ),
        "append",
      );
    }
  };
  const remove = (index: number) => {
    change(removeAction(content, index), { remove: index });
  };
  const pendingLabels = useMemo(
    () =>
      new Map(
        Object.keys(content.pending ?? {}).map((path, index) => [
          path,
          presentPendingInput(path, index + 1, plan, capabilities),
        ]),
      ),
    [content, plan, capabilities],
  );
  const presentations: PresentedIssue[] = useMemo(
    () =>
      presentIssues(issues, plan, capabilities, content).map((item) => ({
        ...item,
        ...(item.issue.code === "unfinished_input"
          ? { location: pendingLabels.get(item.issue.path)! }
          : {}),
      })),
    [issues, plan, capabilities, content, pendingLabels],
  );
  const locate = (index: number) => {
    const target = presentations[index];
    const controls = Array.from(
      editor.current?.querySelectorAll<HTMLElement>("[data-validation-path]") ??
        [],
    );
    const pending = target.issue.code === "unfinished_input";
    const controlPath = pending ? target.issue.path : target.target;
    const matching =
      controlPath === null
        ? undefined
        : (controls.find(
            (control) =>
              control.dataset.validationPath === controlPath &&
              (!target.json || control.dataset.validationJson === "true") &&
              (control.dataset.validationPending === "true") === pending &&
              (pending || control.dataset.validationPath !== ""),
          ) ??
          controls
            .filter(
              (control) =>
                !pending &&
                control.dataset.validationJson === "true" &&
                control.dataset.validationPath &&
                controlPath.startsWith(control.dataset.validationPath + "/"),
            )
            .sort(
              (a, b) =>
                b.dataset.validationPath!.length -
                a.dataset.validationPath!.length,
            )[0]);
    const path = matching?.dataset.validationPath ?? null;
    if (path !== null) {
      setJson(false);
      const parts = pending ? [] : pointerPath(path);
      if (parts[0] === "actions" && /^(0|[1-9]\d*)$/.test(String(parts[1]))) {
        const key = identity(Number(parts[1]));
        setCollapsed((current) => {
          const next = new Set(current);
          next.delete(key);
          return next;
        });
      }
    } else setJson(true);
    setFocusTarget((current) => ({
      path,
      pending,
      sequence: (current?.sequence ?? 0) + 1,
    }));
  };
  useLayoutEffect(() => {
    if (!focusTarget || !editor.current) return;
    const control = Array.from(
      editor.current.querySelectorAll<HTMLElement>("[data-validation-path]"),
    ).find(
      (candidate) =>
        candidate.dataset.validationPath === (focusTarget.path ?? "") &&
        (focusTarget.path === null
          ? candidate.dataset.validationJson === "true" &&
            candidate.dataset.validationPending !== "true"
          : (candidate.dataset.validationPending === "true") ===
            focusTarget.pending),
    );
    if (!control) return;
    for (
      let ancestor = control.parentElement;
      ancestor && ancestor !== editor.current;
      ancestor = ancestor.parentElement
    ) {
      if (ancestor instanceof HTMLDetailsElement) ancestor.open = true;
    }
    control.focus();
    control.scrollIntoView({ block: "center" });
  }, [focusTarget]);
  return (
    <ValidationProvider issues={presentations}>
      <section
        className="panel editor"
        ref={editor}
        tabIndex={-1}
        data-testid="draft-editor"
      >
        <div className="section-head">
          <div>
            <p className="eyebrow">准备新的执行请求</p>
            <h2>编辑草稿</h2>
          </div>
          <div className="button-row">
            <button disabled={busy || !session.editable} onClick={props.onCopy}>
              复制草稿
            </button>
            <button
              className="danger-text"
              disabled={busy || !session.editable}
              onClick={props.onDelete}
            >
              删除草稿
            </button>
            <button
              data-testid="draft-json-toggle"
              onClick={() => setJson(!json)}
            >
              {json ? "使用表单" : "整份计划 JSON"}
            </button>
          </div>
        </div>
        <ErrorBox error={copyError} />
        <ErrorBox error={identityProblem} />
        {Object.keys(content.pending ?? {}).length > 0 && (
          <p className="notice pending-validation-note">
            未完成原文尚未应用到草稿正文。下方其余诊断针对草稿正文；修正原文后将重新校验。
          </p>
        )}
        <Issues
          issues={presentations.map((item) => item.issue)}
          rawIssues={issues}
          presentations={presentations}
          onLocate={locate}
        />
        <div className="save-line">
          <div className="save-feedback">
            <div className="save-summary">
              <span data-testid="save-status" role="status">
                {session.deletionState === "deleting"
                  ? "正在删除草稿…"
                  : session.deletionState === "unknown"
                    ? "删除结果尚未确认"
                    : saveStatusLabels[session.saveStatus]}
              </span>
              <span role="status" data-testid="input-save-fact">
                {session.selectionDecision?.written &&
                sameContent(content, session.selectionDecision.written.content)
                  ? "本页完整输入已可靠写入；本地显示接纳尚未完成。"
                  : sameContent(content, session.confirmedDraft.content)
                    ? "本页完整输入已有可靠保存版本。"
                    : "本页完整输入尚未可靠保存。"}
              </span>
              <span>
                最后完整接纳的保存时间：{session.confirmedDraft.updatedAt}
              </span>
              {session.error && <span role="status">{session.error}</span>}
              {session.saveDiagnostic && (
                <span role="status">{session.saveDiagnostic}</span>
              )}
            </div>
            <div className="save-actions">
              {saveVerification && !session.selectionPendingDecision && (
                <button
                  disabled={busy || session.saving}
                  onClick={() => {
                    void session
                      .verifySaveResult(saveVerification)
                      .catch(() => {});
                  }}
                >
                  核实保存结果
                </button>
              )}
              {session.canSave &&
                !session.saved &&
                !session.saving &&
                !saveVerification &&
                !session.selectionPendingDecision && (
                  <button
                    disabled={busy}
                    onClick={() => void session.flush().catch(() => {})}
                  >
                    {session.error || session.saveDiagnostic
                      ? "重试保存"
                      : "保存当前输入"}
                  </button>
                )}
              {wholeAcceptance && (
                <button
                  disabled={busy}
                  onClick={() => {
                    try {
                      session.retryWholeAcceptance(wholeAcceptance);
                    } catch {
                      /* 领域诊断持续保留。 */
                    }
                  }}
                >
                  重试完整接纳
                </button>
              )}
              {session.conflict && props.onConflict && (
                <button
                  onClick={props.onConflict}
                  disabled={
                    busy ||
                    session.selectionPendingDecision ||
                    session.copyPendingDecision
                  }
                >
                  查看完整差异
                </button>
              )}
              {(session.saveStatus === "deleted" ||
                session.saveStatus === "exported") &&
                props.onSaveAs &&
                !session.selectionPendingDecision &&
                !session.copyPendingDecision && (
                  <button disabled={busy} onClick={props.onSaveAs}>
                    将本页另存为新草稿
                  </button>
                )}
            </div>
          </div>
          <PreviewControl
            intent={intent}
            disabled={busy || !session.editable || !!identityProblem}
            hasAutomatic={
              !!content.automaticPreviews?.actions.some((a) => a.sourceId)
            }
            issues={previewIssues}
            onChoose={choosePreviewIntent}
          />
        </div>
        {session.deletionState === "unknown" && (
          <div className="notice warning">
            <p>删除结果尚未确认，当前输入已保留。</p>
            <button disabled={busy} onClick={props.checkDeletion}>
              重新核实删除结果
            </button>
          </div>
        )}
        {session.exportState === "unknown" && (
          <div className="notice warning">
            <p>
              导出结果尚未确认，输入已保留。请先核实原草稿，暂不能编辑、保存或追加。
            </p>
            <button disabled={busy} onClick={props.checkExport}>
              重新核实导出结果
            </button>
          </div>
        )}
        {session.conflict && (
          <details>
            <summary>查看后端冲突记录</summary>
            <pre>{stringifyJson(session.conflict, 2)}</pre>
          </details>
        )}
        {session.appendLocked && (
          <p className="notice">
            此草稿正在核实一次后续追加；请从上方返回核实，当前内容暂为只读。
          </p>
        )}
        <SelectFieldset
          disabled={busy || !session.editable || !!identityProblem}
        >
          <PendingInputs
            content={content}
            change={change}
            labels={pendingLabels}
          />
          {json ? (
            <>
              <label className="field">
                <span>
                  计划 JSON 文本 <span className="required">必填</span>
                </span>
                <ValidationControl path={[]} json>
                  <textarea
                    className="code-input"
                    data-testid="draft-json-input"
                    spellCheck={false}
                    readOnly={Object.keys(content.pending ?? {}).length > 0}
                    value={content.text}
                    onChange={(e) => {
                      if (
                        (content.automaticPreviews ||
                          Object.keys(content.actionVariants ?? {}).length ||
                          Object.keys(content.parameterVariants ?? {}).length ||
                          Object.keys(content.objectVariants ?? {}).length) &&
                        !window.confirm(
                          "替换整份计划 JSON 将清除自动预览意图、动作关联和按动作类型、参数类型、对象模式保存的编辑内容。确认后自动预览为尚未设置，并保存输入原文。是否继续？",
                        )
                      )
                        return;
                      change(
                        editPlanText(content, e.target.value, true),
                        "reset",
                      );
                    }}
                  />
                </ValidationControl>
              </label>
              {Object.keys(content.pending ?? {}).length > 0 && (
                <p className="notice">
                  请先在“未完成输入”中修正或放弃各路径的输入。整份 JSON
                  暂为只读，保留原值与输入。
                </p>
              )}
            </>
          ) : null}
          {plan && !identityProblem ? (
            <div hidden={json}>
              <label className="field">
                <span>
                  计划名称 <span className="required">必填</span>
                </span>
                <ValidationControl path={["name"]}>
                  <input
                    aria-label="计划名称"
                    value={typeof plan.name === "string" ? plan.name : ""}
                    onChange={(e) =>
                      change(setValue(content, ["name"], e.target.value))
                    }
                  />
                </ValidationControl>
              </label>
              <div className="section-head">
                <h3>
                  动作 <span className="muted">{actionCount}</span>
                </h3>
                <div className="button-row">
                  {!!actionCount && (
                    <>
                      <button onClick={() => setCollapsed(new Set())}>
                        全部展开
                      </button>
                      <button
                        onClick={() =>
                          setCollapsed(
                            new Set(plan.actions.map((_, i) => identity(i))),
                          )
                        }
                      >
                        全部收起
                      </button>
                    </>
                  )}
                  <button onClick={add}>添加动作</button>
                </div>
              </div>
              {!plan.actions.length && (
                <p className="empty">
                  添加录像、取回、清理、取消或状态报告动作。
                </p>
              )}
              {plan.actions.map((action, index) =>
                isAutomaticPreviewAction(action) ? (
                  <details
                    className="advanced"
                    key={identity(index)}
                    data-testid="derived-preview"
                  >
                    <summary>
                      自动取回 · {String(action.name ?? "未命名")} · 技术详情
                    </summary>
                    <p>自动预览动作由开关管理，完整内容随计划导出。</p>
                    <pre>{stringifyJson(action, 2)}</pre>
                  </details>
                ) : isObject(action) ? (
                  <ActionEditor
                    key={identity(index)}
                    {...props}
                    content={content}
                    index={index}
                    ordinal={actionOrdinals[index]!}
                    action={action}
                    change={change}
                    collapsed={collapsed.has(identity(index))}
                    toggle={() =>
                      setCollapsed((current) => {
                        const next = new Set(current);
                        if (next.has(identity(index)))
                          next.delete(identity(index));
                        else next.add(identity(index));
                        return next;
                      })
                    }
                    remove={() => remove(index)}
                    copy={() => {
                      try {
                        change(
                          copyDraftAction(
                            content,
                            index,
                            createActionAllocation(),
                          ),
                          "append",
                        );
                      } catch (error) {
                        setCopyError(
                          `动作复制未完成：${(error as Error).message}`,
                        );
                      }
                    }}
                    issueCount={
                      presentations.filter(
                        ({ actionIndex }) => actionIndex === index,
                      ).length
                    }
                  />
                ) : (
                  <div className="notice error" key={identity(index)}>
                    动作 {actionOrdinals[index]} 不是对象，请在整份 JSON
                    中修正。
                    <button onClick={() => remove(index)}>删除此动作</button>
                  </div>
                ),
              )}
            </div>
          ) : (
            <div hidden={json}>
              <ErrorBox error={problem} />
              <p>原始输入已保留。请切换到整份计划 JSON 继续编辑。</p>
            </div>
          )}
        </SelectFieldset>
        <div className="editor-footer">
          <p>导出生成固定请求。下载后，请手工交给负责传输的部门。</p>
          <button
            className="primary"
            data-testid="export-button"
            disabled={busy || !session.editable}
            onClick={props.onExport}
          >
            {busy ? "正在处理…" : "校验并导出 JSON"}
          </button>
        </div>
      </section>
    </ValidationProvider>
  );
}
const saveStatusLabels: Record<DraftSaveStatus, string> = {
  saved: "已保存",
  dirty: "未保存，等待保存…",
  saving: "保存中…",
  replacing: "替换正在提交…",
  selecting: "正在选择完整版本",
  accepting: "完整版本已证明，本地接纳未完成",
  unknown: "保存结果尚未确认，需要核实",
  conflict: "完整版本冲突，自动保存已暂停",
  superseded_write: "旧保存结果不可确认，请选择当前完整版本",
  invalid: "后端记录无效，需要重新核实",
  unavailable: "读取不可用，输入已保留",
  blocked: "当前操作限制保存",
  exported: "原草稿已导出，只读输入已保留",
  deleted: "原草稿已删除，输入已保留",
  copying: "另存尚待处理，固定输入已保留",
};
function PendingInputs({
  content,
  change,
  labels,
}: {
  content: DraftContent;
  change: (content: DraftContent) => void;
  labels: Map<string, string>;
}) {
  const entries = Object.entries(content.pending ?? {});
  if (!entries.length) return null;
  return (
    <section className="notice warning">
      <h3>未完成输入</h3>
      <p>
        这里保留了你尚未完成的输入。请修正后确认修改，或清除这项输入。
        清除会移除对应字段；必填字段需要重新填写才能导出。
      </p>
      {entries.map(([key, value]) => (
        <PendingInput
          key={key}
          path={key}
          label={labels.get(key)!}
          value={value}
          content={content}
          change={change}
        />
      ))}
    </section>
  );
}
function PendingInput({
  path,
  label,
  value,
  content,
  change,
}: {
  path: string;
  label: string;
  value: { kind: "number" | "json"; text: string };
  content: DraftContent;
  change: (content: DraftContent, operation?: "reset") => void;
}) {
  const [error, setError] = useState("");
  const apply = (omit: boolean) => {
    try {
      const parts = pointerPath(path);
      if (parts.length === 1 && parts[0] === "actions") {
        checkActionListChange(content, omit ? undefined : value.text);
        if (
          !window.confirm(
            `${omit ? "移除" : "替换"}整组动作将清除动作身份、自动预览和按动作类型、参数类型、对象模式保存的编辑内容，以及动作集合内的未完成输入。自动预览将变为尚未设置，集合外的输入继续保留。是否继续？`,
          )
        )
          return;
        change(
          omit
            ? setValue(content, parts, undefined, true, false, undefined, true)
            : editValue(content, parts, value.text, value.kind, true),
          "reset",
        );
        setError("");
        return;
      }
      change(
        omit
          ? setValue(content, parts, undefined, true)
          : editValue(content, parts, value.text, value.kind),
      );
      setError("");
    } catch (e) {
      setError((e as Error).message);
    }
  };
  return (
    <div className="field" data-pending-path={path}>
      <label>
        {label}
        <ValidationControl path={path} pending>
          <textarea
            rows={1}
            aria-label={`未完成输入：${label}`}
            value={value.text}
            onChange={(e) =>
              change({
                ...content,
                pending: {
                  ...content.pending,
                  [path]: { ...value, text: e.target.value },
                },
              })
            }
          />
        </ValidationControl>
      </label>
      <small>修正上面的内容后，点击“确认修改”将它填入对应字段。</small>
      <div className="button-row">
        <button aria-label={`确认修改：${label}`} onClick={() => apply(false)}>
          确认修改
        </button>
        <button
          aria-label={`清除这项输入：${label}`}
          onClick={() => apply(true)}
        >
          清除这项输入
        </button>
      </div>
      <details>
        <summary>技术详情</summary>
        <code>{path === "" ? "整个计划（根路径）" : path}</code>
      </details>
      <ErrorBox error={error} />
    </div>
  );
}
function ActionEditor(
  props: Props & {
    content: DraftContent;
    index: number;
    ordinal: number;
    action: EditObject;
    change: (c: DraftContent) => void;
    collapsed: boolean;
    toggle: () => void;
    remove: () => void;
    copy: () => void;
    issueCount: number;
  },
) {
  const { index, action, content, change, capabilities, presets } = props;
  const base: Path = ["actions", index];
  const [json, setJson] = useState(false),
    [presetId, setPresetId] = useState(""),
    [presetName, setPresetName] = useState(""),
    [notice, setNotice] = useFeedback("action", 3000),
    [error, setError] = useState(""),
    [typeError, setTypeError] = useState(""),
    [savingPreset, setSavingPreset] = useState(false);
  const put = (key: string, value: unknown, omit = false) =>
    change(setValue(content, [...base, key], value, omit));
  const device = capabilities?.devices.find(
      (d) => d.device_id === action.device_id,
    ),
    types =
      device?.actions.find((a) => a.type === action.type)?.parameter_types ??
      [];
  const parameter = types.find(
    (p) => isObject(action.params) && p.type === action.params.type,
  );
  const options = deviceOptions(capabilities, action.type);
  const unselected = action.type === undefined || action.type === "";
  const showDevice = options.requiresDevice;
  const compatible = presets.filter(
    (p) => p.deviceId === action.device_id && p.actionType === action.type,
  );
  const selected = compatible.find((p) => p.id === presetId);
  const pendingPath = pointer([...base, "params"]);
  const pending = content.pending?.[pendingPath];
  const hasPending = Object.keys(content.pending ?? {}).some(
    (key) => key === pendingPath || key.startsWith(pendingPath + "/"),
  );
  const currentParamsIssues = useMemo(
    () =>
      isCameraAction(action.type)
        ? validateParams(
            action.device_id,
            action.type,
            action.params,
            capabilities,
          )
        : [],
    [action.device_id, action.type, action.params, capabilities],
  );
  const videoEstimate = useMemo(
    () =>
      estimateDraftAction(
        content,
        index,
        props.capabilityState,
        props.estimateReloadPhase,
      ),
    [
      content,
      index,
      props.capabilityState.active,
      props.capabilityState.error,
      props.capabilityState.generation,
      props.capabilityState.version,
      props.estimateReloadPhase,
    ],
  );
  const save = async (update: boolean) => {
    if (hasPending) {
      setError("参数尚未完成，请先修正当前输入");
      return;
    }
    if (currentParamsIssues.length) {
      setError("参数校验未通过，请先修正当前输入");
      return;
    }
    if (update && !selected) {
      setError("请选择要更新的预设");
      return;
    }
    setSavingPreset(true);
    setError("");
    setNotice("");
    try {
      const preset = await props.savePreset({
        ...(update ? { id: selected!.id } : {}),
        name: update ? selected!.name : presetName,
        deviceId: action.device_id,
        actionType: action.type,
        params: cloneClientJson(action.params),
      });
      setPresetId(preset.id);
      setNotice(update ? "预设已更新" : "预设已保存");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSavingPreset(false);
    }
  };
  return (
    <article className="action-card">
      <div className="section-head action-summary">
        <div>
          <h3>
            <span className="step">{props.ordinal}</span>
            {typeof action.name === "string" ? action.name : "未命名动作"}
          </h3>
          <p className="muted">
            {unselected ? "尚未选择动作类型" : actionLabel(action.type)} ·{" "}
            {Object.hasOwn(action, "scheduled_at")
              ? utcToLocal(action.scheduled_at).replace("T", " ") ||
                "执行时间待修正"
              : "尚未设置执行时间"}
            {props.issueCount > 0 && (
              <span className="action-problems">
                {props.issueCount} 项待修正
              </span>
            )}
          </p>
        </div>
        <div className="button-row">
          <button onClick={props.copy}>复制动作</button>
          <button
            aria-label={props.collapsed ? "展开动作" : "收起动作"}
            aria-expanded={!props.collapsed}
            onClick={props.toggle}
          >
            {props.collapsed ? "展开" : "收起"}
          </button>
          <button className="danger-text" onClick={props.remove}>
            删除动作
          </button>
        </div>
      </div>
      <div hidden={props.collapsed}>
        <ErrorBox error={typeError} />
        <div className="form-grid">
          <label className="field">
            <span>
              动作名称 <span className="required">必填</span>
            </span>
            <ValidationControl path={[...base, "name"]}>
              <input
                aria-label="动作名称"
                value={typeof action.name === "string" ? action.name : ""}
                onChange={(e) => put("name", e.target.value)}
              />
            </ValidationControl>
          </label>
          <label className="field">
            <span>
              动作类型 <span className="required">必填</span>
            </span>
            <ValidationControl path={[...base, "type"]}>
              <Select
                aria-label="动作类型"
                value={typeof action.type === "string" ? action.type : ""}
                disabled={!canSwitchActionType(content, index)}
                onValueChange={(selectedValue) => {
                  try {
                    change(
                      switchActionType(
                        content,
                        index,
                        selectedValue || undefined,
                      ),
                    );
                    setTypeError("");
                    setJson(false);
                    setError("");
                    setNotice("");
                  } catch (error) {
                    setTypeError(
                      `动作类型切换未完成：${error instanceof Error ? error.message : String(error)}`,
                    );
                  }
                }}
              >
                <SelectItem value="">请选择动作类型</SelectItem>
                {action.type !== undefined &&
                  action.type !== "" &&
                  !options.actions.includes(action.type) && (
                    <SelectItem disabled value={String(action.type)}>
                      {String(action.type)}（当前不可用）
                    </SelectItem>
                  )}
                {options.actions.map((type) => (
                  <SelectItem key={type} value={type}>
                    {actionLabel(type)}
                  </SelectItem>
                ))}
              </Select>
            </ValidationControl>
          </label>
          {showDevice && (
            <label className="field">
              <span>
                目标设备 <span className="required">必填</span>
              </span>
              <ValidationControl path={[...base, "device_id"]}>
                <Select
                  aria-label="目标设备"
                  value={action.device_id ?? ""}
                  onValueChange={(selectedValue) =>
                    put("device_id", selectedValue, selectedValue === "")
                  }
                >
                  <SelectItem value="">请选择设备</SelectItem>
                  {action.device_id &&
                    !options.devices.some(
                      (d) => d.device_id === action.device_id,
                    ) && (
                      <SelectItem disabled value={action.device_id}>
                        {action.device_id}（当前不可用）
                      </SelectItem>
                    )}
                  {options.devices.map((d) => (
                    <SelectItem key={d.device_id} value={d.device_id}>
                      {d.device_id} · {d.driver_id}
                    </SelectItem>
                  ))}
                </Select>
              </ValidationControl>
            </label>
          )}
          <label className="field">
            <span>
              执行时间{" "}
              {isCameraAction(action.type) ||
              [
                "obtain_action_outputs",
                "delete_action_outputs",
                "motor_control",
              ].includes(action.type) ? (
                <span className="required">必填</span>
              ) : unselected ? (
                <small>选择动作类型后确定时间要求</small>
              ) : (
                <small>可选，省略时尽快处理</small>
              )}
            </span>
            <ValidationControl path={[...base, "scheduled_at"]}>
              <input
                aria-label="执行时间"
                type="datetime-local"
                step="1"
                value={utcToLocal(action.scheduled_at)}
                onClick={(e) => e.currentTarget.showPicker?.()}
                onChange={(e) => {
                  const value = localToUtc(e.target.value);
                  put("scheduled_at", value, value === undefined);
                }}
              />
            </ValidationControl>
            {Object.hasOwn(action, "scheduled_at") &&
              !utcToLocal(action.scheduled_at) && (
                <small>
                  执行时间无效，请重新选择或通过整份计划 JSON
                  修正；原输入已保留。
                </small>
              )}
            {(action.type === "cancel_task" ||
              action.type === "report_status") &&
              Object.hasOwn(action, "scheduled_at") && (
                <button onClick={() => put("scheduled_at", undefined, true)}>
                  不指定执行时间
                </button>
              )}
          </label>
          {action.type !== "obtain_action_outputs" ? (
            <Field
              content={content}
              path={[...base, "group"]}
              schema={{ type: "string", title: "动作组" }}
              name="group"
              required={false}
              change={change}
            />
          ) : Object.hasOwn(action, "group") ? (
            <div className="notice warning">
              取回动作不能属于动作组。原值：{JSON.stringify(action.group)}
              <button onClick={() => put("group", undefined, true)}>
                移除不适用的动作组
              </button>
            </div>
          ) : null}
        </div>
        {isCameraAction(action.type) ? (
          <>
            <div className="form-grid">
              <label className="field">
                <span>
                  参数类型 <span className="required">必填</span>
                </span>
                <ValidationControl path={[...base, "params", "type"]}>
                  <Select
                    aria-label="参数类型"
                    value={
                      isObject(action.params) &&
                      typeof action.params.type === "string"
                        ? action.params.type
                        : ""
                    }
                    disabled={!canSwitchParameterType(content, index)}
                    onValueChange={(selectedValue) => {
                      try {
                        const next = switchParameterType(
                          content,
                          index,
                          selectedValue || undefined,
                        );
                        if (next === content) return;
                        change(next);
                        setTypeError("");
                        setJson(false);
                        setError("");
                        setNotice("");
                      } catch (error) {
                        setTypeError(
                          `参数类型切换未完成：${error instanceof Error ? error.message : String(error)}`,
                        );
                      }
                    }}
                  >
                    <SelectItem value="">请选择参数类型</SelectItem>
                    {isObject(action.params) &&
                      typeof action.params.type === "string" &&
                      !parameter && (
                        <SelectItem disabled value={action.params.type}>
                          {action.params.type}（当前不可用）
                        </SelectItem>
                      )}
                    {types.map((p) => (
                      <SelectItem key={p.type} value={p.type}>
                        {p.name}
                      </SelectItem>
                    ))}
                  </Select>
                </ValidationControl>
              </label>
            </div>
            {(!Object.hasOwn(action, "policy") || isObject(action.policy)) && (
              <Field
                content={content}
                path={[...base, "policy", "max_delay_ms"]}
                schema={{
                  type: "integer",
                  minimum: 0,
                  title: "最大允许延迟",
                  description: "单位为毫秒；必须明确填写，0 表示不允许延迟。",
                }}
                name="max_delay_ms"
                required
                change={change}
              />
            )}
            {parameter && (
              <div className="parameter-intro">
                <strong>{parameter.name}</strong>
                <p>{parameter.description}</p>
              </div>
            )}
            <div className="section-head">
              <h4>拍摄参数</h4>
              <button onClick={() => setJson(!json)}>
                {json ? "参数表单" : "参数 JSON"}
              </button>
            </div>
            {json ||
            pending ||
            (Object.hasOwn(action, "params") && !isObject(action.params)) ? (
              <JsonField
                content={content}
                path={[...base, "params"]}
                label="参数 JSON 文本"
                required
                change={change}
              />
            ) : parameter ? (
              <SchemaFields
                parameter={parameter}
                content={content}
                path={[...base, "params"]}
                change={change}
              />
            ) : (
              <p className="notice">
                请选择可用的设备和参数类型；已有参数原值保留，可通过参数 JSON
                修正。
              </p>
            )}
            {pending && !json && (
              <p className="notice">
                参数 JSON 尚未完成，请在上方修正后继续使用表单。
              </p>
            )}
            <VideoSizeEstimate result={videoEstimate} />
            <details className="preset-box">
              <summary>拍摄参数预设</summary>
              <p className="muted">
                预设仅保存当前设备的拍摄参数。动作名称、时间与策略独立保留。
              </p>
              <div className="form-grid">
                <label className="field">
                  已有预设
                  <Select
                    aria-label="已有预设"
                    value={selected?.id ?? ""}
                    onValueChange={(selectedValue) =>
                      setPresetId(selectedValue)
                    }
                  >
                    <SelectItem value="">选择当前设备的预设</SelectItem>
                    {compatible.map((p) => (
                      <SelectItem key={p.id} value={p.id}>
                        {p.name}
                        {validateParams(
                          p.deviceId,
                          p.actionType,
                          p.params,
                          capabilities,
                        ).length
                          ? "（需修正）"
                          : ""}
                      </SelectItem>
                    ))}
                  </Select>
                </label>
                <div className="button-row">
                  <button
                    disabled={!selected}
                    onClick={() => {
                      if (selected) {
                        change(
                          setValue(
                            content,
                            [...base, "params"],
                            cloneClientJson(selected.params),
                            false,
                            true,
                          ),
                        );
                        setNotice("预设参数已复制到当前动作");
                      }
                    }}
                  >
                    应用预设
                  </button>
                  <button
                    disabled={
                      !selected ||
                      savingPreset ||
                      hasPending ||
                      currentParamsIssues.length > 0
                    }
                    onClick={() => void save(true)}
                  >
                    更新所选预设
                  </button>
                </div>
              </div>
              <div className="form-grid">
                <label className="field">
                  <span>
                    预设名称 <span className="required">必填</span>
                  </span>
                  <input
                    aria-label="预设名称"
                    value={presetName}
                    onChange={(e) => setPresetName(e.target.value)}
                  />
                </label>
                <div className="button-row">
                  <button
                    disabled={
                      savingPreset ||
                      hasPending ||
                      currentParamsIssues.length > 0 ||
                      !presetName.trim()
                    }
                    onClick={() => void save(false)}
                  >
                    保存为新预设
                  </button>
                </div>
              </div>
              <ErrorBox error={error} />
              {notice && (
                <p role="status" className="success-text">
                  {notice}
                </p>
              )}
            </details>
          </>
        ) : (
          <>
            {action.type === "motor_control" &&
              (!Object.hasOwn(action, "policy") || isObject(action.policy)) && (
                <Field
                  content={content}
                  path={[...base, "policy", "max_delay_ms"]}
                  schema={{
                    type: "integer",
                    minimum: 0,
                    title: "最大允许延迟",
                    description: "单位为毫秒；必须明确填写，0 表示不允许延迟。",
                  }}
                  name="max_delay_ms"
                  required
                  change={change}
                />
              )}
            {action.type !== "motor_control" &&
              Object.hasOwn(action, "policy") &&
              (!isObject(action.policy) ||
                Object.keys(action.policy).length > 0) && (
                <div className="notice warning">
                  此动作不支持业务策略字段，原值：
                  <pre>{JSON.stringify(action.policy, null, 2)}</pre>
                  <button onClick={() => put("policy", undefined, true)}>
                    移除不适用的业务策略
                  </button>
                </div>
              )}
            {!showDevice && Object.hasOwn(action, "device_id") && (
              <p className="notice">
                {ACTION_TYPES.includes(action.type)
                  ? "此动作不使用设备字段。当前值："
                  : "尚未确定设备字段是否适用。原值："}
                {String(action.device_id)}{" "}
                <button onClick={() => put("device_id", undefined, true)}>
                  省略设备字段
                </button>
              </p>
            )}
            {ACTION_TYPES.includes(action.type) ? (
              <BuiltinFields
                key={action.type}
                content={content}
                path={[...base, "params"]}
                type={action.type}
                change={change}
                reports={props.reports}
                coverage={props.coverage}
              />
            ) : unselected &&
              !Object.hasOwn(action, "params") &&
              !hasPending ? (
              <p className="notice">请选择动作类型，再填写对应参数。</p>
            ) : (
              <JsonField
                content={content}
                path={[...base, "params"]}
                label="动作参数 JSON"
                change={change}
              />
            )}
          </>
        )}
        <details
          className="advanced"
          open={
            (Object.hasOwn(action, "policy") && !isObject(action.policy)) ||
            undefined
          }
        >
          <summary>完整业务策略 JSON</summary>
          <JsonField
            content={content}
            path={[...base, "policy"]}
            label="业务策略 JSON"
            required={
              isCameraAction(action.type) || action.type === "motor_control"
            }
            change={change}
          />
          {!isCameraAction(action.type) &&
            action.type !== "motor_control" &&
            Object.hasOwn(action, "policy") && (
              <button onClick={() => put("policy", undefined, true)}>
                省略策略字段
              </button>
            )}
        </details>
      </div>
    </article>
  );
}
export function SchemaFields({
  parameter,
  content,
  path,
  change,
}: {
  parameter: ParameterType;
  content: DraftContent;
  path: Path;
  change: (c: DraftContent) => void;
}) {
  const schema = parameter.schema,
    properties = isObject(schema.properties) ? schema.properties : {};
  const fields = Object.entries(properties).filter(([name]) => name !== "type");
  const catalog = parameterOptions(parameter);
  const params = valueAt(parseDraft(content), path);
  const hasPending =
    catalog.kind !== "unavailable" &&
    fields.some(
      ([name]) =>
        parameterFieldOptions(catalog, name).kind === "finite" &&
        parameterChoiceBlocked(content, [...path, name], catalog),
    );
  const form = fields.length ? (
    <>
      {catalog.kind === "unavailable" && (
        <p className="notice">
          当前规则无法可靠生成选项控件，选项字段请通过 JSON
          填写；填写后将检查全部规则。
        </p>
      )}
      {hasPending && (
        <p className="notice">请先修正上方未完成输入，再选择关联参数。</p>
      )}
      <div className="form-grid">
        {fields.map(([name, field]) => {
          const scope = parameterFieldOptions(catalog, name);
          const branches = objectBranches(parameter, name);
          if (branches)
            return (
              <ObjectBranchField
                key={name}
                catalog={branches}
                name={name}
                required={
                  Array.isArray(schema.required) &&
                  schema.required.includes(name)
                }
                content={content}
                path={[...path, name]}
                change={change}
              />
            );
          const choices =
            scope.kind === "unavailable"
              ? undefined
              : scope.fields.find((entry) => entry.name === name)?.values;
          const blocked =
            catalog.kind !== "unavailable" &&
            parameterChoiceBlocked(content, [...path, name], catalog);
          return (
            <Field
              key={name}
              name={name}
              schema={resolveField(field, schema)}
              rulesSchema={field}
              required={
                scope.kind !== "unavailable" && isObject(params) && !blocked
                  ? parameterRequired(catalog, params, name)
                  : Array.isArray(schema.required) &&
                    schema.required.includes(name)
              }
              content={content}
              path={[...path, name]}
              change={change}
              choices={choices}
              allowed={
                scope.kind !== "unavailable" &&
                isObject(params) &&
                !blocked &&
                choices
                  ? compatibleValues(catalog, params, name)
                  : undefined
              }
              choicesBlocked={blocked}
              choiceBasis={
                scope.kind === "independent" ? "independent" : "linked"
              }
              jsonChoices={
                scope.kind === "unavailable" ||
                (scope.kind === "independent" && !choices)
              }
            />
          );
        })}
      </div>
    </>
  ) : catalog.kind === "unavailable" ? (
    <p className="notice">
      此任务的完整参数无法用普通字段表达，请通过参数 JSON 设置并检查。
    </p>
  ) : catalog.kind === "finite" && catalog.rows.length === 0 ? (
    <p className="notice warning">
      当前规则没有允许的参数组合，请联系提供设备说明的人员。
    </p>
  ) : (
    <p className="muted">该任务使用固定设置，无需填写其他参数。</p>
  );
  return form;
}
