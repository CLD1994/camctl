import { Select, SelectItem, SelectFieldset } from "./Select";
import { isCameraAction } from "../shared/actions";
import { useState, useLayoutEffect, useRef } from "react";
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
  initializePreviewMetadata,
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
import { DraftSession } from "./session";
import { ActionUiIdentity } from "./action-ui-identity";
import { actionLabel, Issues, ErrorBox } from "./common";
import { Field, JsonField } from "./Fields";
import {
  estimateDraftAction,
  type EstimateReloadPhase,
} from "./video-estimate-state";
import { VideoSizeEstimate } from "./VideoSizeEstimate";
import { BuiltinFields } from "./BuiltinFields";
import { presentIssue } from "./validation-presentation";
import {
  ValidationControl,
  ValidationProvider,
  type PresentedIssue,
} from "./Validation";
import { deviceOptions } from "./device-options";
import { canSwitchActionType, switchActionType } from "./action-drafts";
import {
  parameterOptions,
  compatibleValues,
  parameterRequired,
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
  const [actionIdentity] = useState(() => new ActionUiIdentity(content));
  useLayoutEffect(
    () =>
      session.bindAppendView((transition) =>
        actionIdentity.prepareAppend(transition),
      ),
    [session, actionIdentity],
  );
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
  const previewIssues = coordinatePreviews(
    content,
    props.capabilityState,
  ).issues;
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
  let plan: ReturnType<typeof parseDraft> | undefined;
  let problem = "";
  let issues: Issue[] = [];
  try {
    plan = parseDraft(content);
  } catch (error) {
    problem = (error as Error).message;
  }
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
  const add = () => {
    if (plan) {
      change(
        appendDraftAction(content, {
          name: `动作 ${plan.actions.length + 1}`,
        }),
        "append",
      );
    }
  };
  const remove = (index: number) => {
    change(removeAction(content, index), { remove: index });
  };
  const presentations: PresentedIssue[] = issues.map((issue) => ({
    issue,
    ...presentIssue(issue, plan, capabilities),
  }));
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
      <section className="panel editor" ref={editor}>
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
              className="quiet danger-text"
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
        <Issues
          issues={issues}
          presentations={presentations}
          onLocate={locate}
        />
        <div className="save-line">
          <span data-testid="save-status" role="status">
            {session.deletionState === "deleting"
              ? "正在删除草稿…"
              : session.deletionState === "unknown"
                ? "删除结果尚未确认"
                : session.error
                  ? `保存失败或未确认：${session.error}`
                  : session.saving
                    ? "保存中…"
                    : session.saved
                      ? "已保存"
                      : "等待保存…"}
          </span>
          {session.error && session.editable && (
            <button
              disabled={busy}
              onClick={() => void session.flush().catch(() => {})}
            >
              重试保存
            </button>
          )}
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
          <section className="notice" aria-label="自动获取预览文件">
            <strong data-testid="preview-intent">
              自动预览
              {intent === "enabled"
                ? "已开启"
                : intent === "disabled"
                  ? "已关闭"
                  : "尚未设置"}
            </strong>
            <div className="button-row">
              <button
                disabled={intent === "enabled"}
                onClick={() => choosePreviewIntent("enabled")}
              >
                开启自动预览
              </button>
              <button
                disabled={intent === "disabled"}
                onClick={() => choosePreviewIntent("disabled")}
              >
                关闭自动预览
              </button>
            </div>
            <p data-testid="preview-status">
              {intent === "unset"
                ? "保留全部显式动作；选择开启或关闭后按该意图维护。"
                : intent === "disabled"
                  ? "手动取回仍独立保留。"
                  : previewIssues.length
                    ? "部分自动预览关联待核实。"
                    : content.automaticPreviews?.actions.some((a) => a.sourceId)
                      ? "符合条件的拍摄由客户端维护自动取回。"
                      : "当前没有适用动作；新增符合条件的拍摄后自动维护。"}
            </p>
            {previewIssues.map((message, i) => (
              <p key={i} className="warning">
                {message}
              </p>
            ))}
          </section>
          <PendingInputs content={content} change={change} />
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
                          Object.keys(content.actionVariants ?? {}).length) &&
                        !window.confirm(
                          "替换整份计划 JSON 将清除自动预览意图、动作关联和其他动作类型暂存内容。确认后自动预览为尚未设置，并保存输入原文。是否继续？",
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
                  动作 <span className="muted">{plan.actions.length}</span>
                </h3>
                <div className="button-row">
                  {!!plan.actions.length && (
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
                isObject(action) &&
                action.type === "obtain_action_outputs" &&
                isObject(action.params) &&
                action.params.purpose === "auto_preview" ? (
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
                        change(copyDraftAction(content, index), "append");
                      } catch (error) {
                        setCopyError(
                          `动作复制未完成：${(error as Error).message}`,
                        );
                      }
                    }}
                    issueCount={
                      issues.filter(
                        (issue) =>
                          issue.path === `actions[${index}]` ||
                          issue.path.startsWith(`actions[${index}].`) ||
                          issue.path.startsWith(`/actions/${index}/`),
                      ).length
                    }
                  />
                ) : (
                  <div className="notice error" key={identity(index)}>
                    动作 {index + 1} 不是对象，请在整份 JSON 中修正。
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
function PendingInputs({
  content,
  change,
}: {
  content: DraftContent;
  change: (content: DraftContent) => void;
}) {
  const entries = Object.entries(content.pending ?? {});
  if (!entries.length) return null;
  return (
    <section className="notice warning">
      <h3>未完成输入</h3>
      <p>
        以下原文随草稿保留。每项均可修正或放弃，包括当前没有对应控件的字段。
        放弃会清除该字段；必填字段仍需重新填写才能导出。
      </p>
      {entries.map(([key, value]) => (
        <PendingInput
          key={key}
          path={key}
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
  value,
  content,
  change,
}: {
  path: string;
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
            `${omit ? "移除" : "替换"}整组动作将清除动作身份、自动预览和其他动作类型编辑资料，以及动作集合内的未完成输入。自动预览将变为尚未设置，集合外的输入继续保留。是否继续？`,
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
    <div className="field">
      <label>
        {path}
        <ValidationControl path={path} pending>
          <textarea
            rows={1}
            aria-label={`未完成输入 ${path}`}
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
      <small>原文随草稿保存，点击应用修正后写入对应字段。</small>
      <div className="button-row">
        <button onClick={() => apply(false)}>应用修正 {path}</button>
        <button onClick={() => apply(true)}>放弃输入 {path}</button>
      </div>
      <ErrorBox error={error} />
    </div>
  );
}
function ActionEditor(
  props: Props & {
    content: DraftContent;
    index: number;
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
  const currentParamsIssues = isCameraAction(action.type)
    ? validateParams(action.device_id, action.type, action.params, capabilities)
    : [];
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
            <span className="step">{index + 1}</span>
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
          <button className="quiet danger-text" onClick={props.remove}>
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
                    {actionLabel(type)} · {type}
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
                    disabled={!!pending}
                    onValueChange={(selectedValue) =>
                      change(
                        setValue(
                          content,
                          [...base, "params", "type"],
                          selectedValue,
                          selectedValue === "",
                        ),
                      )
                    }
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
            <VideoSizeEstimate
              result={estimateDraftAction(
                content,
                index,
                props.capabilityState,
                props.estimateReloadPhase,
              )}
            />
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
            {action.type === "motor_control" && (
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
        <details className="advanced">
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
function SchemaFields({
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
  const hasPending = Object.keys(content.pending ?? {}).some(
    (key) => key === pointer(path) || key.startsWith(pointer(path) + "/"),
  );
  return fields.length ? (
    <>
      {catalog.kind === "unavailable" && (
        <p className="notice">
          这些参数包含无法完整推导的组合规则，选项字段请通过 JSON
          填写；填写后将检查全部规则。
        </p>
      )}
      {hasPending && (
        <p className="notice">请先修正上方未完成输入，再选择关联参数。</p>
      )}
      <div className="form-grid">
        {fields.map(([name, field]) => (
          <Field
            key={name}
            name={name}
            schema={resolveField(field, schema)}
            required={
              catalog.kind === "finite" && isObject(params)
                ? parameterRequired(catalog, params, name)
                : Array.isArray(schema.required) &&
                  schema.required.includes(name)
            }
            content={content}
            path={[...path, name]}
            change={change}
            choices={
              catalog.kind === "finite"
                ? catalog.fields.find((f) => f.name === name)?.values
                : undefined
            }
            allowed={
              catalog.kind === "finite" && isObject(params)
                ? compatibleValues(catalog, params, name)
                : undefined
            }
            choicesBlocked={hasPending}
            jsonChoices={catalog.kind === "unavailable"}
          />
        ))}
      </div>
    </>
  ) : catalog.kind === "unavailable" ? (
    <p className="notice">
      此任务的完整参数无法用普通字段表达，请通过参数 JSON 设置并检查。
    </p>
  ) : catalog.rows.length === 0 ? (
    <p className="notice warning">
      当前规则没有允许的参数组合，请联系提供设备说明的人员。
    </p>
  ) : (
    <p className="muted">该任务使用固定设置，无需填写其他参数。</p>
  );
}
