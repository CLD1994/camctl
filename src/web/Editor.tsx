import { useState } from "react";
import type { DraftContent, Preset } from "../server/models";
import type { Capabilities, Issue, ParameterType } from "../shared/types";
import { ACTION_TYPES, validatePlan } from "../shared/plan";
import { validateParams } from "../shared/capabilities";
import { isObject } from "../shared/validation";
import { parseJson } from "../shared/json";
import {
  parseDraft,
  setValue,
  pendingBlocks,
  editPlanText,
  editValue,
  removeAction,
  appendDraftAction,
  pointer,
  valueAt,
  resolveField,
  localToUtc,
  utcToLocal,
  type Path,
  type EditObject,
} from "./editing";
import { DraftSession } from "./session";
import { actionLabel, Issues, ErrorBox } from "./common";
import {
  parameterOptions,
  compatibleValues,
  optionLabel,
  sameValue,
  parameterRequired,
} from "./parameter-options";

interface Props {
  session: DraftSession;
  capabilities: Capabilities | null;
  presets: Preset[];
  reports: Array<{ report_id: number; to_wm: number }>;
  coverage: number;
  busy: boolean;
  onExport: () => void;
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
  const [collapsed, setCollapsed] = useState<Set<number>>(new Set());
  const content = session.content;
  const change = (next: DraftContent) => {
    session.edit(next);
    session.schedule();
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
            request_id: "validation",
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
      setCollapsed(
        (current) =>
          new Set([...current].filter((i) => i !== plan.actions.length)),
      );
      change(
        appendDraftAction(content, {
          name: `动作 ${plan.actions.length + 1}`,
          type: "camera_record",
          params: {},
          policy: {},
        }),
      );
    }
  };
  const remove = (index: number) => {
    setCollapsed(
      (current) =>
        new Set(
          [...current]
            .filter((i) => i !== index)
            .map((i) => (i > index ? i - 1 : i)),
        ),
    );
    change(removeAction(content, index));
  };
  return (
    <section className="panel editor">
      <div className="section-head">
        <div>
          <p className="eyebrow">准备新的执行请求</p>
          <h2>编辑草稿</h2>
        </div>
        <button data-testid="draft-json-toggle" onClick={() => setJson(!json)}>
          {json ? "使用表单" : "整份计划 JSON"}
        </button>
      </div>
      <div className="save-line">
        <span data-testid="save-status" role="status">
          {session.error
            ? `保存失败或未确认：${session.error}`
            : session.saving
              ? "保存中…"
              : session.saved
                ? "已保存"
                : "等待保存…"}
        </span>
        {session.error && session.editable && (
          <button onClick={() => void session.flush().catch(() => {})}>
            重试保存
          </button>
        )}
      </div>
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
          <pre>{JSON.stringify(session.conflict, null, 2)}</pre>
        </details>
      )}
      {session.appendLocked && (
        <p className="notice">
          此草稿正在核实一次后续追加；请从上方返回核实，当前内容暂为只读。
        </p>
      )}
      <fieldset disabled={busy || !session.editable}>
        <PendingInputs content={content} change={change} />
        {json ? (
          <>
            <label className="field">
              计划 JSON 文本
              <textarea
                className="code-input"
                data-testid="draft-json-input"
                spellCheck={false}
                readOnly={Object.keys(content.pending ?? {}).length > 0}
                value={content.text}
                onChange={(e) => change(editPlanText(content, e.target.value))}
              />
            </label>
            {Object.keys(content.pending ?? {}).length > 0 && (
              <p className="notice">
                请先在“未完成输入”中修正或放弃各路径的输入。整份 JSON
                暂为只读，保留原值与输入。
              </p>
            )}
          </>
        ) : plan ? (
          <>
            <label className="field">
              计划名称
              <input
                aria-label="计划名称"
                value={typeof plan.name === "string" ? plan.name : ""}
                onChange={(e) =>
                  change(setValue(content, ["name"], e.target.value))
                }
              />
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
                        setCollapsed(new Set(plan.actions.map((_, i) => i)))
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
              isObject(action) ? (
                <ActionEditor
                  key={index}
                  {...props}
                  content={content}
                  index={index}
                  action={action}
                  change={change}
                  collapsed={collapsed.has(index)}
                  toggle={() =>
                    setCollapsed((current) => {
                      const next = new Set(current);
                      if (next.has(index)) next.delete(index);
                      else next.add(index);
                      return next;
                    })
                  }
                  remove={() => remove(index)}
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
                <div className="notice error" key={index}>
                  动作 {index + 1} 不是对象，请在整份 JSON 中修正。
                  <button onClick={() => remove(index)}>删除此动作</button>
                </div>
              ),
            )}
          </>
        ) : (
          <>
            <ErrorBox error={problem} />
            <p>原始输入已保留。请切换到整份计划 JSON 继续编辑。</p>
          </>
        )}
      </fieldset>
      <Issues issues={issues} />
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
  change: (content: DraftContent) => void;
}) {
  const [error, setError] = useState("");
  const parts = path
    .slice(1)
    .split("/")
    .map((p) => p.replace(/~1/g, "/").replace(/~0/g, "~"));
  const apply = (omit: boolean) => {
    try {
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
        <textarea
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
    issueCount: number;
  },
) {
  const { index, action, content, change, capabilities, presets } = props;
  const base: Path = ["actions", index];
  const [json, setJson] = useState(false),
    [presetId, setPresetId] = useState(""),
    [presetName, setPresetName] = useState(""),
    [notice, setNotice] = useState(""),
    [error, setError] = useState(""),
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
  const compatible = presets.filter(
    (p) => p.deviceId === action.device_id && p.actionType === action.type,
  );
  const selected = compatible.find((p) => p.id === presetId);
  const pendingPath = pointer([...base, "params"]);
  const pending = content.pending?.[pendingPath];
  const hasPending = Object.keys(content.pending ?? {}).some(
    (key) => key === pendingPath || key.startsWith(pendingPath + "/"),
  );
  const currentParamsIssues =
    action.type === "camera_record"
      ? validateParams(
          action.device_id,
          action.type,
          action.params,
          capabilities,
        )
      : [];
  const save = async (update: boolean) => {
    if (hasPending) {
      setError("参数尚未完成，请先修正当前输入");
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
        params: structuredClone(action.params),
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
            {actionLabel(action.type)} ·{" "}
            {typeof action.scheduled_at === "string"
              ? `${action.scheduled_at} UTC`
              : "尚未设置执行时间"}
            {props.issueCount > 0 && (
              <span className="action-problems">
                {props.issueCount} 项待修正
              </span>
            )}
          </p>
        </div>
        <div className="button-row">
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
        <div className="form-grid">
          <label className="field">
            动作名称
            <input
              aria-label="动作名称"
              value={typeof action.name === "string" ? action.name : ""}
              onChange={(e) => put("name", e.target.value)}
            />
          </label>
          <label className="field">
            动作类型
            <select
              aria-label="动作类型"
              value={typeof action.type === "string" ? action.type : ""}
              onChange={(e) => put("type", e.target.value)}
            >
              {!ACTION_TYPES.includes(action.type) && (
                <option value={String(action.type ?? "")}>
                  {String(action.type ?? "尚未选择")}（不支持）
                </option>
              )}
              {ACTION_TYPES.map((type) => (
                <option key={type} value={type}>
                  {actionLabel(type)} · {type}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            执行时间{" "}
            <small>
              {Intl.DateTimeFormat().resolvedOptions().timeZone}，保存为 UTC
            </small>
            <input
              aria-label="执行时间"
              type="datetime-local"
              step="1"
              value={utcToLocal(action.scheduled_at)}
              onChange={(e) => {
                const value = localToUtc(e.target.value);
                put("scheduled_at", value, value === undefined);
              }}
            />
            {action.scheduled_at !== undefined && (
              <small>UTC 原值：{String(action.scheduled_at)}</small>
            )}
          </label>
          <Field
            content={content}
            path={[...base, "group"]}
            schema={{ type: "string", title: "动作组" }}
            name="group"
            required={false}
            change={change}
          />
        </div>
        {action.type === "camera_record" ? (
          <>
            <div className="form-grid">
              <label className="field">
                目标设备
                <select
                  aria-label="目标设备"
                  value={action.device_id ?? ""}
                  onChange={(e) =>
                    put("device_id", e.target.value, e.target.value === "")
                  }
                >
                  <option value="">请选择设备</option>
                  {action.device_id && !device && (
                    <option value={action.device_id}>
                      {action.device_id}（当前不可用）
                    </option>
                  )}
                  {capabilities?.devices.map((d) => (
                    <option key={d.device_id} value={d.device_id}>
                      {d.device_id} · {d.driver_id}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                参数类型
                <select
                  aria-label="参数类型"
                  value={
                    isObject(action.params) &&
                    typeof action.params.type === "string"
                      ? action.params.type
                      : ""
                  }
                  disabled={!!pending}
                  onChange={(e) =>
                    change(
                      setValue(
                        content,
                        [...base, "params", "type"],
                        e.target.value,
                        e.target.value === "",
                      ),
                    )
                  }
                >
                  <option value="">请选择参数类型</option>
                  {isObject(action.params) &&
                    typeof action.params.type === "string" &&
                    !parameter && (
                      <option value={action.params.type}>
                        {action.params.type}（当前不可用）
                      </option>
                    )}
                  {types.map((p) => (
                    <option key={p.type} value={p.type}>
                      {p.name}
                    </option>
                  ))}
                </select>
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
            {json || pending || !isObject(action.params) ? (
              <JsonField
                content={content}
                path={[...base, "params"]}
                label="参数 JSON 文本"
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
            <div className="preset-box">
              <h4>拍摄参数预设</h4>
              <p className="muted">
                预设仅保存当前设备的拍摄参数。动作名称、时间与策略独立保留。
              </p>
              <div className="form-grid">
                <label className="field">
                  已有预设
                  <select
                    aria-label="已有预设"
                    value={selected?.id ?? ""}
                    onChange={(e) => setPresetId(e.target.value)}
                  >
                    <option value="">选择当前设备的预设</option>
                    {compatible.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name}
                        {validateParams(
                          p.deviceId,
                          p.actionType,
                          p.params,
                          capabilities,
                        ).length
                          ? "（需修正）"
                          : ""}
                      </option>
                    ))}
                  </select>
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
                            structuredClone(selected.params),
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
                  预设名称
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
            </div>
          </>
        ) : (
          <>
            {Object.hasOwn(action, "device_id") && (
              <p className="notice">
                此动作不使用设备字段。当前值：{String(action.device_id)}{" "}
                <button onClick={() => put("device_id", undefined, true)}>
                  省略设备字段
                </button>
              </p>
            )}
            <JsonField
              content={content}
              path={[...base, "params"]}
              label="动作参数 JSON"
              change={change}
            />
            <p className="muted">
              {action.type === "obtain_action_outputs"
                ? "使用 source 指定来源；按需填写 output_ids。"
                : action.type === "delete_action_outputs"
                  ? "使用 output_ids 指定正式产物 ID。"
                  : action.type === "cancel_task"
                    ? "使用 target 指定动作、计划或请求身份。"
                    : "可省略参数准备普通报告，或通过“准备状态同步”取得同步参数。"}
            </p>
            {Object.hasOwn(action, "params") && (
              <button
                className="inline"
                onClick={() => put("params", undefined, true)}
              >
                省略参数字段
              </button>
            )}
          </>
        )}
        <details className="advanced">
          <summary>完整业务策略 JSON</summary>
          <JsonField
            content={content}
            path={[...base, "policy"]}
            label="业务策略 JSON"
            change={change}
          />
          {action.type !== "camera_record" &&
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
function JsonField({
  content,
  path,
  label,
  change,
}: {
  content: DraftContent;
  path: Path;
  label: string;
  change: (c: DraftContent) => void;
}) {
  const value = valueAt(parseDraft(content), path),
    pending = content.pending?.[pointer(path)];
  return (
    <label className="field">
      {label}
      <textarea
        aria-label={label}
        className="code-input compact"
        spellCheck={false}
        readOnly={pendingBlocks(content, path)}
        value={
          pending?.text ??
          (value === undefined ? "" : JSON.stringify(value, null, 2))
        }
        onChange={(e) => {
          change(editValue(content, path, e.target.value, "json"));
        }}
      />
      {pending && (
        <small className="danger-text">
          JSON 尚未完成或存在重复键，原文将随草稿保存。
        </small>
      )}
      {pendingBlocks(content, path) && (
        <small>
          此 JSON 中有未完成字段，请在上方“未完成输入”中修正或放弃输入。
        </small>
      )}
    </label>
  );
}
function Field({
  schema,
  name,
  required,
  content,
  path,
  change,
  choices,
  allowed,
  choicesBlocked = false,
  jsonChoices = false,
}: {
  schema: Record<string, unknown>;
  name: string;
  required: boolean;
  content: DraftContent;
  path: Path;
  change: (c: DraftContent) => void;
  choices?: unknown[];
  allowed?: unknown[];
  choicesBlocked?: boolean;
  jsonChoices?: boolean;
}) {
  const root = parseDraft(content),
    value = valueAt(root, path),
    pending = content.pending?.[pointer(path)],
    label = `${typeof schema.title === "string" ? schema.title : name} (${name})`;
  const set = (v: unknown, omit = false) =>
    change(setValue(content, path, v, omit));
  const [enabled, setEnabled] = useState(false);
  const active = required || enabled || value !== undefined || !!pending;
  const declared =
    choices ?? (Array.isArray(schema.enum) ? schema.enum : undefined);
  const enumeration = jsonChoices ? undefined : declared;
  const type = Array.isArray(schema.type)
    ? schema.type.filter((t) => t !== "null").length === 1
      ? schema.type.find((t) => t !== "null")
      : undefined
    : schema.type;
  const nullable = Array.isArray(schema.type) && schema.type.includes("null");
  const matched = enumeration?.findIndex(
    (v) =>
      sameValue(v, value) && (!allowed || allowed.some((a) => sameValue(a, v))),
  );
  const unsupportedChoice =
    jsonChoices &&
    (!!declared || type === "boolean" || Object.hasOwn(schema, "const"));
  const showRaw =
    !pending &&
    value !== undefined &&
    !enumeration &&
    !unsupportedChoice &&
    (type === "string"
      ? typeof value !== "string"
      : type === "number" || type === "integer"
        ? typeof value !== "number"
        : false);
  return (
    <div className="field">
      {!required && (
        <label className="optional-field">
          <input
            type="checkbox"
            aria-label={`填写${label}`}
            checked={active}
            onChange={(e) => {
              setEnabled(e.target.checked);
              if (!e.target.checked) set(undefined, true);
            }}
          />
          <span>
            填写{typeof schema.title === "string" ? schema.title : name}
          </span>
          <small>可选</small>
        </label>
      )}
      {active && (
        <>
          <label>
            <span>
              {typeof schema.title === "string" ? schema.title : name}{" "}
              {required && <span className="required">必填</span>}{" "}
              <small className="field-key">{name}</small>
            </span>
            {unsupportedChoice ? (
              <textarea
                aria-label={label}
                readOnly={pendingBlocks(content, path)}
                value={
                  pending?.text ??
                  (value === undefined ? "" : JSON.stringify(value, null, 2))
                }
                onChange={(e) =>
                  change(editValue(content, path, e.target.value, "json"))
                }
              />
            ) : enumeration ? (
              <select
                aria-label={label}
                disabled={choicesBlocked || pendingBlocks(content, path)}
                value={
                  value === undefined
                    ? ""
                    : matched !== undefined && matched >= 0
                      ? String(matched)
                      : "invalid"
                }
                onChange={(e) =>
                  e.target.value === ""
                    ? set(undefined, true)
                    : set(enumeration[Number(e.target.value)])
                }
              >
                <option value="">请选择</option>
                {value !== undefined && matched === -1 && (
                  <option value="invalid" disabled>
                    {optionLabel(value)}（待修正）
                  </option>
                )}
                {enumeration.map(
                  (v, i) =>
                    (!allowed || allowed.some((a) => sameValue(a, v))) && (
                      <option key={i} value={i}>
                        {optionLabel(v)}
                      </option>
                    ),
                )}
              </select>
            ) : type === "boolean" ? (
              <select
                aria-label={label}
                disabled={pendingBlocks(content, path)}
                value={
                  value === undefined
                    ? ""
                    : value === true
                      ? "true"
                      : value === false
                        ? "false"
                        : "invalid"
                }
                onChange={(e) =>
                  e.target.value === ""
                    ? set(undefined, true)
                    : set(e.target.value === "true")
                }
              >
                <option value="">请选择</option>
                <option value="true">是 · true</option>
                <option value="false">否 · false</option>
                {value !== undefined && typeof value !== "boolean" && (
                  <option value="invalid" disabled>
                    {optionLabel(value)}（待修正）
                  </option>
                )}
              </select>
            ) : type === "string" ? (
              <input
                aria-label={label}
                readOnly={pendingBlocks(content, path)}
                value={typeof value === "string" ? value : ""}
                onChange={(e) => set(e.target.value)}
              />
            ) : type === "number" || type === "integer" ? (
              <input
                aria-label={label}
                readOnly={pendingBlocks(content, path)}
                inputMode="decimal"
                value={
                  pending?.text ?? (value === undefined ? "" : String(value))
                }
                onChange={(e) =>
                  change(editValue(content, path, e.target.value, "number"))
                }
              />
            ) : (
              <textarea
                aria-label={label}
                readOnly={pendingBlocks(content, path)}
                value={
                  pending?.text ??
                  (value === undefined ? "" : JSON.stringify(value, null, 2))
                }
                onChange={(e) =>
                  change(editValue(content, path, e.target.value, "json"))
                }
              />
            )}
          </label>
          {showRaw && (
            <small className="danger-text">
              原值 {JSON.stringify(value)} 无法用此控件表示，请重新填写或在 JSON
              中修正。
            </small>
          )}
          {enumeration && allowed?.length === 0 && !choicesBlocked && (
            <small className="danger-text">
              没有兼容选项，请先清空冲突字段或在参数 JSON 中修正。
            </small>
          )}
          {enumeration && value !== undefined && matched === -1 && (
            <small className="danger-text">
              此值与当前参数不兼容，原值已保留；请选择兼容值或清空后重新选择。
            </small>
          )}
          <small>
            {String(schema.description ?? "")}
            {schema.default !== undefined
              ? ` 默认值说明：${optionLabel(schema.default)}。`
              : ""}
            {schema.minimum !== undefined ? ` 最小值 ${schema.minimum}。` : ""}
            {schema.maximum !== undefined ? ` 最大值 ${schema.maximum}。` : ""}
          </small>
          {pending && <small className="danger-text">输入尚未完成</small>}
          {nullable && !enumeration && value !== null && (
            <button
              className="inline"
              disabled={pendingBlocks(content, path)}
              onClick={() => set(null)}
            >
              设为 null
            </button>
          )}
        </>
      )}
    </div>
  );
}
