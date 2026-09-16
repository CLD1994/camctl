import { Select, SelectItem } from "./Select";
import type { DraftContent } from "../server/models";
import {
  parseDraft,
  valueAt,
  pointer,
  pendingBlocks,
  editValue,
  setValue,
  type Path,
} from "./editing";
import { sameValue, optionLabel } from "./parameter-options";
export function JsonField({
  content,
  path,
  label,
  change,
  required = false,
}: {
  content: DraftContent;
  path: Path;
  label: string;
  change: (c: DraftContent) => void;
  required?: boolean;
}) {
  const value = valueAt(parseDraft(content), path),
    pending = content.pending?.[pointer(path)];
  return (
    <label className="field">
      <span>
        {label} {required && <span className="required">必填</span>}
      </span>
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
export function Field({
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
            <Select
              aria-label={label}
              disabled={choicesBlocked || pendingBlocks(content, path)}
              value={
                value === undefined
                  ? ""
                  : matched !== undefined && matched >= 0
                    ? String(matched)
                    : "invalid"
              }
              onValueChange={(selectedValue) =>
                selectedValue === ""
                  ? set(undefined, true)
                  : set(enumeration[Number(selectedValue)])
              }
            >
              <SelectItem value="">请选择</SelectItem>
              {value !== undefined && matched === -1 && (
                <SelectItem value="invalid" disabled>
                  {optionLabel(value)}（待修正）
                </SelectItem>
              )}
              {enumeration.map(
                (v, i) =>
                  (!allowed || allowed.some((a) => sameValue(a, v))) && (
                    <SelectItem key={i} value={i}>
                      {optionLabel(v)}
                    </SelectItem>
                  ),
              )}
            </Select>
          ) : type === "boolean" ? (
            <Select
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
              onValueChange={(selectedValue) =>
                selectedValue === ""
                  ? set(undefined, true)
                  : set(selectedValue === "true")
              }
            >
              <SelectItem value="">请选择</SelectItem>
              <SelectItem value="true">是 · true</SelectItem>
              <SelectItem value="false">否 · false</SelectItem>
              {value !== undefined && typeof value !== "boolean" && (
                <SelectItem value="invalid" disabled>
                  {optionLabel(value)}（待修正）
                </SelectItem>
              )}
            </Select>
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
        {!required && (value !== undefined || pending) && (
          <button
            className="inline"
            aria-label={`清空${label}`}
            onClick={() => set(undefined, true)}
          >
            清空（不填写）
          </button>
        )}
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
    </div>
  );
}
