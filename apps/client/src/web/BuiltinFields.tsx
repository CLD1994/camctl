import { Select, SelectItem } from "./Select";
import { isCameraAction } from "../shared/actions";
import type { CameraActionType } from "../shared/actions";
import { useId, useState } from "react";
import type { DraftContent } from "../server/models";
import { isObject, isName } from "../shared/validation";
import {
  validateBuiltinParams,
  sources,
  targets,
  builtinFields,
  isSyncBasis,
  cleanupModes,
  referenceMode,
} from "../shared/action-params";
import {
  stringifyJson,
  displayJsonValue,
  parseClientJson,
  cloneClientJson,
} from "../shared/json";
import type { ActionType } from "../shared/types";
import {
  parseDraft,
  setValue,
  pointer,
  valueAt,
  changeBuiltinMode,
  changeObtainSelection,
  type Path,
} from "./editing";
import { JsonField, Field } from "./Fields";
import notificationSchema from "../../../../protocol/schemas/host-notification.schema.json";

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
  reports: Array<{ report_id: string; to_wm: number }>;
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
  const modes =
    type === "delete_action_outputs"
      ? cleanupModes
      : type === "obtain_action_outputs"
        ? sources
        : targets;
  const referenceKey = type === "cancel_task" ? "target" : "source";
  const reference = params?.[referenceKey];
  const exactCleanup =
    type === "delete_action_outputs" &&
    params &&
    Object.hasOwn(params, "output_ids");
  const mode = exactCleanup
    ? reference === undefined
      ? cleanupModes.find((m) => m.id === "output_ids")
      : undefined
    : referenceMode(
        reference,
        modes.filter((m) => m.id !== "output_ids"),
      );
  const outputFilter = mode?.id === "action_instance_id";
  const basis = reports.filter((r) => isSyncBasis(r, coverage));
  const reportMode =
    value === undefined || (params && Object.keys(params).length === 0)
      ? ""
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
            required
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
          {type === "motor_control" && (
            <Field
              content={content}
              path={[...path, "position"]}
              schema={{
                ...notificationSchema.$defs.motor_params.properties.position,
                title: "位置",
                description: "位置单位、零点和业务合法范围由主程序定义。",
              }}
              name="position"
              required
              change={change}
            />
          )}
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
          {(type === "obtain_action_outputs" ||
            type === "delete_action_outputs" ||
            type === "cancel_task") && (
            <>
              <p className="muted">
                切换{type === "cancel_task" ? "目标" : "来源"}
                会清空原引用，需重新填写。
                {type === "obtain_action_outputs" &&
                  "只有指定动作实例提供精确产物 ID 控件，切换到其他来源时会清除精确列表。默认取回排除预览并优先对应修复成品。"}
                {type === "delete_action_outputs" &&
                  "切换精确列表或来源范围会移除另一种选择。来源范围包含原文件、预览和修复成品，不清理客户端副本。"}
              </p>
              <label className="field">
                <span>
                  {type === "delete_action_outputs"
                    ? "清理范围"
                    : type === "obtain_action_outputs"
                      ? "取回来源"
                      : "取消目标"}{" "}
                  <span className="required">必填</span>
                </span>
                <Select
                  aria-label={
                    type === "delete_action_outputs"
                      ? "清理范围"
                      : type === "obtain_action_outputs"
                        ? "取回来源"
                        : "取消目标"
                  }
                  value={mode?.id ?? ""}
                  onValueChange={(selectedValue) => {
                    const selected = modes.find((m) => m.id === selectedValue);
                    if (!selected) return;
                    change(changeBuiltinMode(content, path, type, selected.id));
                  }}
                >
                  <SelectItem value="" disabled>
                    请选择
                  </SelectItem>
                  {modes.map((m) => (
                    <SelectItem key={m.id} value={m.id}>
                      {m.label}
                    </SelectItem>
                  ))}
                </Select>
              </label>
              {reference !== undefined && !mode && (
                <div className="notice warning">
                  引用字段组合需要修正：
                  <pre>
                    {displayJsonValue(params, referenceKey, reference, 2)}
                  </pre>
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
                      parent={reference}
                      fieldKey={key}
                      candidates={
                        type !== "cancel_task" && mode.id === "action_name"
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
              {mode?.id === "current_plan" && isObject(reference) && (
                <p
                  className={
                    reference.current_plan === true ? "muted" : "notice warning"
                  }
                >
                  {reference.current_plan === true
                    ? "来源为当前整个计划。"
                    : `原值 current_plan: ${displayJsonValue(reference, "current_plan", reference.current_plan)} 必须为布尔 true；请通过 JSON 修正或明确重新填写当前计划引用。`}
                  {reference.current_plan !== true && (
                    <button
                      onClick={() =>
                        change(
                          changeBuiltinMode(
                            content,
                            path,
                            type as
                              "obtain_action_outputs" | "delete_action_outputs",
                            "current_plan",
                          ),
                        )
                      }
                    >
                      重新填写当前计划引用
                    </button>
                  )}
                </p>
              )}
              {type === "obtain_action_outputs" && (
                <>
                  <p className="muted">
                    选择默认或手动预览筛选会移除精确列表；开启精确列表会移除
                    filter。用途 purpose
                    保留原值，手动预览选择不会写入自动用途。
                  </p>
                  <label className="field">
                    <span>取回筛选</span>
                    <Select
                      aria-label="取回筛选"
                      value={
                        params?.filter === undefined
                          ? "implicit"
                          : params.filter === "default" ||
                              params.filter === "preview"
                            ? params.filter
                            : "invalid"
                      }
                      onValueChange={(v) => {
                        if (
                          v === "implicit" ||
                          v === "default" ||
                          v === "preview"
                        )
                          change(changeObtainSelection(content, path, v));
                      }}
                    >
                      <SelectItem value="implicit">不填写筛选</SelectItem>
                      <SelectItem value="default">默认产物</SelectItem>
                      <SelectItem value="preview">预览产物</SelectItem>
                      {params?.filter !== undefined &&
                        params.filter !== "default" &&
                        params.filter !== "preview" && (
                          <SelectItem value="invalid" disabled>
                            原筛选待修正
                          </SelectItem>
                        )}
                    </Select>
                  </label>
                  {params && Object.hasOwn(params, "purpose") && (
                    <p className="muted">
                      已填写用途 purpose：
                      {displayJsonValue(params, "purpose", params.purpose)}
                      。可通过参数 JSON 核对或修改。
                    </p>
                  )}
                  {outputFilter && (
                    <label className="optional-field">
                      <input
                        type="checkbox"
                        aria-label="指定产物筛选"
                        checked={
                          params !== undefined &&
                          Object.hasOwn(params, "output_ids")
                        }
                        onChange={(e) =>
                          e.target.checked
                            ? change(
                                changeObtainSelection(content, path, "exact"),
                              )
                            : put("output_ids", undefined, true)
                        }
                      />
                      <span>仅取回指定产物；不勾选时按来源与筛选取回</span>
                    </label>
                  )}
                  {!outputFilter &&
                    params &&
                    Object.hasOwn(params, "output_ids") && (
                      <div className="notice warning">
                        此来源不提供产物筛选表单；已有输入保留，可通过参数 JSON
                        核对或移除。原值：
                        <pre>
                          {displayJsonValue(
                            params,
                            "output_ids",
                            params.output_ids,
                            2,
                          )}
                        </pre>
                        <button
                          onClick={() => put("output_ids", undefined, true)}
                        >
                          移除已有精确产物列表
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
              {type === "delete_action_outputs" && exactCleanup && (
                <OutputIds
                  content={content}
                  path={[...path, "output_ids"]}
                  change={change}
                />
              )}
            </>
          )}
          {type === "report_status" && (
            <>
              <p className="muted">
                业务状态变化后由主机自动维护报告。需要补齐状态时，请选择同步范围；切换范围会替换原报告参数。
              </p>
              <label className="field">
                <span>
                  报告范围 <span className="required">必填</span>
                </span>
                <Select
                  aria-label="报告范围"
                  value={reportMode}
                  onValueChange={(selectedValue) =>
                    change(setValue(content, path, { scope: selectedValue }))
                  }
                >
                  {reportMode === "invalid" && (
                    <SelectItem value="invalid" disabled>
                      原参数待修正
                    </SelectItem>
                  )}
                  <SelectItem value="" disabled>
                    请选择同步范围
                  </SelectItem>
                  <SelectItem value="full">完整同步</SelectItem>
                  <SelectItem value="since" disabled={!basis.length}>
                    从已保存报告之后补齐
                  </SelectItem>
                </Select>
              </label>
              {reportMode === "" && (
                <p className="notice warning">
                  请选择完整或增量同步并补齐必填项，填写完成后才能导出。
                </p>
              )}
              {reportMode === "since" && (
                <label className="field">
                  <span>
                    同步起点报告 <span className="required">必填</span>
                  </span>
                  <Select
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
                    onValueChange={(selectedValue) =>
                      put(
                        "after_report_id",
                        selectedValue,
                        selectedValue === "",
                      )
                    }
                  >
                    <SelectItem value="">请选择</SelectItem>
                    {params?.after_report_id !== undefined &&
                      !basis.some(
                        (r) => r.report_id === params.after_report_id,
                      ) && (
                        <SelectItem value="invalid" disabled>
                          {displayJsonValue(
                            params,
                            "after_report_id",
                            params.after_report_id,
                          )}
                          （无可靠依据）
                        </SelectItem>
                      )}
                    {basis.map((r) => (
                      <SelectItem key={r.report_id} value={r.report_id}>
                        报告 {r.report_id} · 已完整保存至 {r.to_wm}
                      </SelectItem>
                    ))}
                  </Select>
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
                  <pre>{stringifyJson(params, 2)}</pre>
                  <button onClick={reset}>清空参数并重新填写</button>
                </div>
              )}
            </>
          )}
        </>
      )}
      {issues.length > 0 && type !== "report_status" && (
        <p className="notice warning">
          参数字段、值或组合需要修正；原输入保留，请补齐表单或通过参数 JSON
          核对。
        </p>
      )}
    </section>
  );
}

function ReferenceField({
  label,
  value,
  candidates,
  change,
  parent,
  fieldKey,
}: {
  label: string;
  value: unknown;
  candidates: string[];
  change: (value: string) => void;
  parent: unknown;
  fieldKey: string | number;
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
          原值 {displayJsonValue(parent, fieldKey, value)}{" "}
          不是文本，请重新填写。
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
            parent={ids}
            fieldKey={index}
            candidates={[]}
            change={(v) => change(setValue(content, [...path, index], v))}
          />
          <button
            onClick={() =>
              change(
                setValue(
                  content,
                  path,
                  parseClientJson(
                    `[${ids.flatMap((v, i) => (i === index ? [] : [displayJsonValue(ids, i, v)])).join(",")}]`,
                  ),
                ),
              )
            }
            aria-label={`移除产物 ${index + 1}`}
          >
            移除
          </button>
        </div>
      ))}
      <button
        onClick={() => {
          const next = cloneClientJson(ids);
          next.push("");
          change(setValue(content, path, next));
        }}
      >
        添加产物 ID
      </button>
      {!ids.length && (
        <p className="notice">至少添加一个产物 ID；空列表不能导出。</p>
      )}
    </div>
  );
}
