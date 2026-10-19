import { useState } from "react";
import type { DraftContent } from "../server/models";
import {
  canSwitchObjectBranch,
  switchObjectBranch,
  replaceObjectValue,
} from "../shared/object-variants";
import { Field } from "./Fields";
import { stringifyJson } from "../shared/json";
import { Select, SelectItem } from "./Select";
import { ValidationControl } from "./Validation";
import {
  objectBranchState,
  objectBranchJsonEdit,
  type ObjectBranches,
} from "./object-branches";
import type { Path } from "./editing";

export function ObjectBranchField({
  catalog,
  content,
  path,
  name,
  required,
  change,
}: {
  catalog: ObjectBranches;
  content: DraftContent;
  path: Path;
  name: string;
  required: boolean;
  change: (content: DraftContent) => void;
}) {
  const [json, setJson] = useState(false),
    [error, setError] = useState("");
  const state = objectBranchState(content, path, catalog);
  const index = Number(path[1]),
    relative = path.slice(3).map(String);
  const label =
    typeof catalog.schema.title === "string" ? catalog.schema.title : name;
  const raw = state.kind === "raw" || state.kind === "blocked";
  const apply = (operation: () => DraftContent) => {
    try {
      change(operation());
      setError("");
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    }
  };
  const modes = catalog.branches.map(
    (branch) =>
      (branch.schema.properties as Record<string, Record<string, unknown>>)[
        catalog.discriminator
      ],
  );
  const modeSchema = state.kind === "selected" ? modes[state.index] : undefined;
  const modeTitle =
    modeSchema?.title ??
    (modes.every((mode) => mode.title === modes[0].title)
      ? modes[0].title
      : undefined);
  const modeLabel =
    typeof modeTitle === "string" ? modeTitle : catalog.discriminator;
  return (
    <section className="object-branch-field">
      <div className="section-head">
        <h5>
          {label} {required && <span className="required">必填</span>}{" "}
          <small className="field-key">{name}</small>
        </h5>
        {!raw && (
          <button
            className="inline"
            aria-label={`${label} JSON`}
            onClick={() => setJson(!json)}
          >
            {json ? "参数表单" : "高级 JSON"}
          </button>
        )}
      </div>
      {raw || json ? (
        <>
          <Field
            schema={catalog.schema}
            rulesSchema={catalog.schema}
            name={name}
            required={required}
            content={content}
            path={path}
            change={(next) =>
              apply(() =>
                objectBranchJsonEdit(
                  content,
                  next,
                  path,
                  catalog.discriminator,
                ),
              )
            }
          />
          {raw && (
            <>
              <small className="danger-text">
                {state.kind === "raw" &&
                state.reason === "discriminator_changed"
                  ? "当前判别字段与保存的模式资料不一致，原值和模式资料已保留。"
                  : "当前原值或未完成输入不能完整表示为级联字段，原文已保留。"}
              </small>
              <button
                aria-label={`重新填写${label}`}
                disabled={state.kind === "blocked"}
                onClick={() => {
                  if (
                    !window.confirm(
                      `重新填写将移除${label}的当前值、未完成原文及所有模式的编辑内容，然后重新选择模式。其他字段保持。是否继续？`,
                    )
                  )
                    return;
                  apply(() =>
                    replaceObjectValue(content, index, relative, undefined, {
                      omit: true,
                      resetBranches: true,
                    }),
                  );
                  setJson(false);
                }}
              >
                重新填写
              </button>
            </>
          )}
        </>
      ) : (
        <>
          {typeof catalog.schema.description === "string" && (
            <p className="muted">{catalog.schema.description}</p>
          )}
          <label className="field">
            <span>
              {modeLabel} <span className="required">必填</span>{" "}
              <small className="field-key">{catalog.discriminator}</small>
            </span>
            <ValidationControl path={[...path, catalog.discriminator]}>
              <Select
                aria-label={`${modeLabel} (${catalog.discriminator})`}
                value={state.kind === "selected" ? `branch:${state.index}` : ""}
                disabled={
                  !canSwitchObjectBranch(
                    content,
                    index,
                    relative,
                    catalog.discriminator,
                  )
                }
                onValueChange={(selected) => {
                  const branch = catalog.branches.find(
                    (_branch, i) => selected === `branch:${i}`,
                  );
                  if (branch)
                    apply(() =>
                      switchObjectBranch(
                        content,
                        index,
                        relative,
                        catalog.discriminator,
                        branch.value,
                      ),
                    );
                }}
              >
                <SelectItem value="">请选择</SelectItem>
                {catalog.branches.map((branch, i) => (
                  <SelectItem key={branch.value} value={`branch:${i}`}>
                    {branch.value}
                  </SelectItem>
                ))}
              </Select>
            </ValidationControl>
            {typeof modeSchema?.description === "string" && (
              <small>{modeSchema.description}</small>
            )}
          </label>
          {state.kind === "selected" && (
            <>
              {typeof state.branch.schema.title === "string" && (
                <h6>{state.branch.schema.title}</h6>
              )}
              {typeof state.branch.schema.description === "string" && (
                <p className="muted">{state.branch.schema.description}</p>
              )}
              <div className="form-grid">
                {state.branch.members.map((member) => (
                  <Field
                    key={member.name}
                    schema={member.schema}
                    name={member.name}
                    required={member.required}
                    choices={member.choices}
                    choiceBasis="independent"
                    content={content}
                    path={[...path, member.name]}
                    change={change}
                  />
                ))}
              </div>
            </>
          )}
          <details className="advanced">
            <summary>完整字段规则</summary>
            <pre>{stringifyJson(catalog.schema, 2)}</pre>
          </details>
        </>
      )}
      {error && <p className="danger-text">{error}</p>}
    </section>
  );
}
