import { isCameraAction } from '../shared/actions';
import type { CameraActionType } from '../shared/actions';
import { useId, useState } from "react";
import type { DraftContent } from "../server/models";
import { isObject, isName } from "../shared/validation";
import {
  validateBuiltinParams,
  sources,
  targets,
  builtinFields,
  isSyncBasis,
} from "../shared/action-params";
import type { ActionType } from "../shared/types";
import { parseDraft, setValue, pointer, valueAt, type Path } from "./editing";
import { JsonField } from "./Fields";

export function BuiltinFields({
  content,
  path,
  type,
  change,
  reports,
  coverage,
}: {
  content: DraftContent;
  path: Path;
  type: Exclude<ActionType, CameraActionType>;
  change: (next: DraftContent) => void;
  reports: Array<{ report_id: number; to_wm: number }>;
  coverage: number;
}) {
  const [json, setJson] = useState(false);
  const root = parseDraft(content),
    value = valueAt(root, path);
  const params = isObject(value) ? value : undefined;
  const prefix = pointer(path);
  const pending = Object.keys(content.pending ?? {}).some(
    (k) =>
      k === prefix || k.startsWith(prefix + "/") || prefix.startsWith(k + "/"),
  );
  const structural = value !== undefined && !params;
  const issues = validateBuiltinParams(type, value, value !== undefined);
  const put = (key: string, v: unknown, omit = false) =>
    change(setValue(content, [...path, key], v, omit));
  const reset = () => change(setValue(content, path, {}, false, true));
  const modes = type === "obtain_action_outputs" ? sources : targets;
  const referenceKey = type === "obtain_action_outputs" ? "source" : "target";
  const reference = params?.[referenceKey];
  const mode = isObject(reference)
    ? modes.find(
        (m) =>
          Object.keys(m.fields).length === Object.keys(reference).length &&
          Object.keys(m.fields).every((k) => Object.hasOwn(reference, k)),
      )
    : undefined;
  const group = isObject(reference) && Object.hasOwn(reference, "group");
  const outputFilter = mode?.id === "action_instance_id";
  const basis = reports.filter((r) => isSyncBasis(r, coverage));
  const reportMode =
    value === undefined || (params && Object.keys(params).length === 0)
      ? "normal"
      : params?.scope === "full" || params?.scope === "since"
        ? params.scope
        : "invalid";
  const extra = params
    ? Object.keys(params).filter((k) => !builtinFields[type].includes(k))
    : [];
  const cameraActions = root.actions.filter(
    (a) => isObject(a) && isCameraAction(a.type),
  );
  return (
    <section className="builtin-parameters">
      <div className="section-head">
        <h4>动作参数</h4>
        <button onClick={() => setJson(!json)}>
          {json ? "参数表单" : "参数 JSON"}
        </button>
      </div>
      {json || structural || pending ? (
        <>
          <JsonField
            content={content}
            path={path}
            label="动作参数 JSON"
            required={type !== "report_status"}
            change={change}
          />
          {structural && (
            <p className="notice">
              参数必须是对象，原值已保留。
              <button disabled={pending} onClick={reset}>
                清空参数并重新填写
              </button>
            </p>
          )}
          {pending && (
            <p className="notice">请先修正或放弃未完成输入，再使用表单。</p>
          )}
        </>
      ) : (
        <>
          {extra.length > 0 && (
            <p className="notice warning">
              包含不适用字段：{extra.join("、")}。原值保留，请在参数 JSON
              中修正。
              <button
                onClick={() => {
                  let next = content;
                  for (const key of extra)
                    next = setValue(next, [...path, key], undefined, true);
                  change(next);
                }}
              >
                移除不适用参数字段
              </button>
            </p>
          )}
          {(type === "obtain_action_outputs" || type === "cancel_task") && (
            <>
              <p className="muted">
                切换{type === "obtain_action_outputs" ? "来源" : "目标"}
                会清空原引用，需重新填写。
                {type === "obtain_action_outputs" &&
                  "只有指定动作实例可选择产物筛选，切换到其他来源时会清除筛选。"}
              </p>
              <label className="field">
                <span>
                  {type === "obtain_action_outputs" ? "取回来源" : "取消目标"}{" "}
                  <span className="required">必填</span>
                </span>
                <select
                  aria-label={
                    type === "obtain_action_outputs" ? "取回来源" : "取消目标"
                  }
                  value={mode?.id ?? ""}
                  onChange={(e) => {
                    const selected = modes.find((m) => m.id === e.target.value);
                    if (!selected) return;
                    let next = setValue(
                      content,
                      [...path, referenceKey],
                      Object.fromEntries(
                        Object.keys(selected.fields).map((k) => [k, ""]),
                      ),
                    );
                    if (
                      type === "obtain_action_outputs" &&
                      selected.id !== "action_instance_id"
                    )
                      next = setValue(
                        next,
                        [...path, "output_ids"],
                        undefined,
                        true,
                      );
                    change(next);
                  }}
                >
                  <option value="" disabled>
                    请选择
                  </option>
                  {modes.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.label}
                    </option>
                  ))}
                </select>
              </label>
              {reference !== undefined && !mode && (
                <div className="notice warning">
                  引用字段组合需要修正：
                  <pre>{JSON.stringify(reference, null, 2)}</pre>
                  选择一种来源或目标重新填写，或通过参数 JSON 修正。
                </div>
              )}
              {mode && isObject(reference) && (
                <div className="form-grid">
                  {Object.entries(mode.fields).map(([key, label]) => (
                    <ReferenceField
                      key={key}
                      label={label}
                      value={reference[key]}
                      candidates={
                        type === "obtain_action_outputs" &&
                        mode.id === "action_name"
                          ? cameraActions.map((a) => a.name).filter(isName)
                          : type === "obtain_action_outputs" &&
                              mode.id === "group"
                            ? [
                                ...new Set(
                                  cameraActions
                                    .map((a) => a.group)
                                    .filter(isName),
                                ),
                              ]
                            : []
                      }
                      change={(v) =>
                        change(
                          setValue(content, [...path, referenceKey, key], v),
                        )
                      }
                    />
                  ))}
                </div>
              )}
              {type === "obtain_action_outputs" && (
                <>
                  {group ? (
                    <p className="muted">
                      取回来源组的正式产物，不支持按产物 ID 筛选。
                    </p>
                  ) : (
                    outputFilter && (
                      <label className="optional-field">
                        <input
                          type="checkbox"
                          aria-label="指定产物筛选"
                          checked={
                            params !== undefined &&
                            Object.hasOwn(params, "output_ids")
                          }
                          onChange={(e) =>
                            put("output_ids", [], !e.target.checked)
                          }
                        />
                        <span>仅取回指定产物；不勾选时取回全部正式产物</span>
                      </label>
                    )
                  )}
                  {!outputFilter &&
                    params &&
                    Object.hasOwn(params, "output_ids") && (
                      <div className="notice warning">
                        此来源不提供产物筛选表单；已有输入保留，可通过参数 JSON
                        核对或移除。原值：
                        <pre>{JSON.stringify(params.output_ids, null, 2)}</pre>
                        <button
                          onClick={() => put("output_ids", undefined, true)}
                        >
                          移除不适用的产物筛选
                        </button>
                      </div>
                    )}
                  {outputFilter &&
                    params &&
                    Object.hasOwn(params, "output_ids") && (
                      <OutputIds
                        content={content}
                        path={[...path, "output_ids"]}
                        change={change}
                      />
                    )}
                </>
              )}
            </>
          )}
          {type === "delete_action_outputs" && (
            <>
              <p className="muted">
                明确列出要删除的正式产物 ID。列表不能为空，删除不表示清理全部。
              </p>
              <OutputIds
                content={content}
                path={[...path, "output_ids"]}
                change={change}
              />
            </>
          )}
          {type === "report_status" && (
            <>
              <p className="muted">
                切换报告范围会替换原报告参数。普通报告不额外发起同步。
              </p>
              <label className="field">
                报告范围
                <select
                  aria-label="报告范围"
                  value={reportMode}
                  onChange={(e) =>
                    change(
                      setValue(
                        content,
                        path,
                        e.target.value === "normal"
                          ? undefined
                          : { scope: e.target.value },
                        e.target.value === "normal",
                      ),
                    )
                  }
                >
                  {reportMode === "invalid" && (
                    <option value="invalid" disabled>
                      原参数待修正
                    </option>
                  )}
                  <option value="normal">普通报告</option>
                  <option value="full">完整同步</option>
                  <option value="since" disabled={!basis.length}>
                    从已保存报告之后补齐
                  </option>
                </select>
              </label>
              {reportMode === "since" && (
                <label className="field">
                  <span>
                    同步起点报告 <span className="required">必填</span>
                  </span>
                  <select
                    aria-label="同步起点报告"
                    value={
                      params?.after_report_id === undefined
                        ? ""
                        : basis.some(
                              (r) => r.report_id === params.after_report_id,
                            )
                          ? String(params.after_report_id)
                          : "invalid"
                    }
                    onChange={(e) =>
                      put(
                        "after_report_id",
                        Number(e.target.value),
                        e.target.value === "",
                      )
                    }
                  >
                    <option value="">请选择</option>
                    {params?.after_report_id !== undefined &&
                      !basis.some(
                        (r) => r.report_id === params.after_report_id,
                      ) && (
                        <option value="invalid" disabled>
                          {JSON.stringify(params.after_report_id)}（无可靠依据）
                        </option>
                      )}
                    {basis.map((r) => (
                      <option key={r.report_id} value={r.report_id}>
                        报告 {r.report_id} · 已完整保存至 {r.to_wm}
                      </option>
                    ))}
                  </select>
                </label>
              )}
              {!basis.length && (
                <p className="muted">
                  尚无可用的增量同步起点，可以请求完整同步。
                </p>
              )}
              {issues.length > 0 && params && (
                <div className="notice warning">
                  报告参数需要修正，原值：
                  <pre>{JSON.stringify(params, null, 2)}</pre>
                  <button onClick={reset}>清空参数并重新填写</button>
                </div>
              )}
            </>
          )}
        </>
      )}
    </section>
  );
}

function ReferenceField({
  label,
  value,
  candidates,
  change,
}: {
  label: string;
  value: unknown;
  candidates: string[];
  change: (value: string) => void;
}) {
  const id = useId();
  return (
    <label className="field">
      <span>
        {label} <span className="required">必填</span>
      </span>
      <input
        aria-label={label}
        list={candidates.length ? id : undefined}
        value={typeof value === "string" ? value : ""}
        onChange={(e) => change(e.target.value)}
      />
      {candidates.length > 0 && (
        <datalist id={id}>
          {candidates.map((v) => (
            <option key={v} value={v} />
          ))}
        </datalist>
      )}
      {value !== undefined && typeof value !== "string" && (
        <small className="danger-text">
          原值 {JSON.stringify(value)} 不是文本，请重新填写。
        </small>
      )}
    </label>
  );
}

function OutputIds({
  content,
  path,
  change,
}: {
  content: DraftContent;
  path: Path;
  change: (next: DraftContent) => void;
}) {
  const value = valueAt(parseDraft(content), path);
  if (value !== undefined && !Array.isArray(value))
    return (
      <div className="notice warning">
        产物 ID 必须是列表，原值已保留。
        <JsonField
          content={content}
          path={path}
          label="产物列表 JSON"
          required
          change={change}
        />
        <button onClick={() => change(setValue(content, path, []))}>
          清空并重新填写产物列表
        </button>
      </div>
    );
  const ids = value ?? [];
  return (
    <div className="output-id-list">
      <p>
        产物 ID <span className="required">必填</span>
      </p>
      {ids.map((id, index) => (
        <div className="form-grid" key={index}>
          <ReferenceField
            label={`产物 ID ${index + 1}`}
            value={id}
            candidates={[]}
            change={(v) => change(setValue(content, [...path, index], v))}
          />
          <button
            onClick={() =>
              change(
                setValue(
                  content,
                  path,
                  ids.filter((_, i) => i !== index),
                ),
              )
            }
            aria-label={`移除产物 ${index + 1}`}
          >
            移除
          </button>
        </div>
      ))}
      <button onClick={() => change(setValue(content, path, [...ids, ""]))}>
        添加产物 ID
      </button>
      {!ids.length && (
        <p className="notice">至少添加一个产物 ID；空列表不能导出。</p>
      )}
    </div>
  );
}
