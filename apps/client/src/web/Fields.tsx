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
import { sameValueAt, optionLabel } from "./parameter-options";
import {
  displayNumber,
  displayJsonValue,
  originalNumberToken,
} from "../shared/json";
import { ValidationControl } from "./Validation";
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
  const root = parseDraft(content),
    value = valueAt(root, path),
    pending = content.pending?.[pointer(path)];
  return (
    <label className="field">
      <span>
        {label} {required && <span className="required">必填</span>}
      </span>
      <ValidationControl path={path} json>
        <textarea
          aria-label={label}
          className="code-input compact"
          spellCheck={false}
          readOnly={pendingBlocks(content, path)}
          value={
            pending?.text ??
            (value === undefined
              ? ""
              : displayJsonValue(
                  valueAt(root, path.slice(0, -1)),
                  path.at(-1)!,
                  value,
                  2,
                ))
          }
          onChange={(e) => {
            change(editValue(content, path, e.target.value, "json"));
          }}
        />
      </ValidationControl>
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
  choiceBasis = "linked",
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
  choiceBasis?: "linked" | "independent";
}) {
  const root = parseDraft(content),
    value = valueAt(root, path),
    pending = content.pending?.[pointer(path)],
    label = `${typeof schema.title === "string" ? schema.title : name} (${name})`;
  const set = (v: unknown, omit = false, numberToken?: string) =>
    change(setValue(content, path, v, omit, false, numberToken));
  const declared =
    choices ?? (Array.isArray(schema.enum) ? schema.enum : undefined);
  const enumeration = jsonChoices ? undefined : declared;
  const type = Array.isArray(schema.type)
    ? schema.type.filter((t) => t !== "null").length === 1
      ? schema.type.find((t) => t !== "null")
      : undefined
    : schema.type;
  const nullable = Array.isArray(schema.type) && schema.type.includes("null");
  const parent = valueAt(root, path.slice(0, -1));
  const choicePaused =
    choicesBlocked || pendingBlocks(content, path) || !!pending;
  const candidateAllowed = (index: number) =>
    !allowed ||
    allowed.some((_value, allowedIndex) =>
      sameValueAt(allowed, allowedIndex, enumeration!, index),
    );
  const matched =
    enumeration && !choicePaused
      ? enumeration.findIndex(
          (_value, index) =>
            typeof parent === "object" &&
            parent !== null &&
            sameValueAt(enumeration, index, parent, path.at(-1)!) &&
            candidateAllowed(index),
        )
      : enumeration
        ? -1
        : undefined;
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
          <ValidationControl
            path={path}
            json={
              unsupportedChoice ||
              (!enumeration &&
                type !== "boolean" &&
                type !== "string" &&
                type !== "number" &&
                type !== "integer")
            }
          >
            {unsupportedChoice ? (
              <textarea
                rows={1}
                aria-label={label}
                readOnly={pendingBlocks(content, path)}
                value={
                  pending?.text ??
                  (value === undefined
                    ? ""
                    : displayJsonValue(
                        valueAt(root, path.slice(0, -1)),
                        path.at(-1)!,
                        value,
                        2,
                      ))
                }
                onChange={(e) =>
                  change(editValue(content, path, e.target.value, "json"))
                }
              />
            ) : enumeration ? (
              <Select
                aria-label={label}
                disabled={choicePaused}
                value={
                  value === undefined && !pending
                    ? ""
                    : matched !== undefined && matched >= 0
                      ? String(matched)
                      : "invalid"
                }
                onValueChange={(selectedValue) => {
                  if (choicePaused) return;
                  if (selectedValue === "") return set(undefined, true);
                  const index = Number(selectedValue);
                  if (
                    !Number.isInteger(index) ||
                    !Object.hasOwn(enumeration, index) ||
                    !candidateAllowed(index)
                  )
                    return;
                  set(
                    enumeration[index],
                    false,
                    originalNumberToken(enumeration, index, enumeration[index]),
                  );
                }}
              >
                <SelectItem value="">请选择</SelectItem>
                {(value !== undefined || pending) && matched === -1 && (
                  <SelectItem value="invalid" disabled>
                    {pending?.text ?? optionLabel(value, parent, path.at(-1)!)}
                    （{choicePaused ? "等待完成输入" : "待修正"}）
                  </SelectItem>
                )}
                {enumeration.map(
                  (v, i) =>
                    candidateAllowed(i) && (
                      <SelectItem key={i} value={i}>
                        {optionLabel(v, enumeration, i)}
                      </SelectItem>
                    ),
                )}
              </Select>
            ) : type === "boolean" ? (
              <Select
                aria-label={label}
                disabled={choicePaused}
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
                  pending?.text ??
                  (value === undefined
                    ? ""
                    : typeof value === "number"
                      ? displayNumber(
                          valueAt(root, path.slice(0, -1)),
                          path.at(-1)!,
                          value,
                        )
                      : String(value))
                }
                onChange={(e) =>
                  change(editValue(content, path, e.target.value, "number"))
                }
              />
            ) : (
              <textarea
                rows={1}
                aria-label={label}
                readOnly={pendingBlocks(content, path)}
                value={
                  pending?.text ??
                  (value === undefined
                    ? ""
                    : displayJsonValue(
                        valueAt(root, path.slice(0, -1)),
                        path.at(-1)!,
                        value,
                        2,
                      ))
                }
                onChange={(e) =>
                  change(editValue(content, path, e.target.value, "json"))
                }
              />
            )}
          </ValidationControl>
        </label>
        {showRaw && (
          <small className="danger-text">
            原值{" "}
            {displayJsonValue(
              valueAt(root, path.slice(0, -1)),
              path.at(-1)!,
              value,
            )}{" "}
            无法用此控件表示，请重新填写或在 JSON 中修正。
          </small>
        )}
        {unsupportedChoice && (
          <small>
            当前字段的完整选项无法由表单可靠生成，请通过 JSON 填写并检查。
          </small>
        )}
        {enumeration &&
          (allowed ?? enumeration).length === 0 &&
          !choicePaused && (
            <small className="danger-text">
              {choiceBasis === "independent"
                ? `该字段没有允许值。${required ? "请联系设备说明提供者修正该字段规则。" : "可以清空此项，保持不填写。"}`
                : "没有兼容选项，请先清空冲突字段或在参数 JSON 中修正。"}
            </small>
          )}
        {enumeration &&
          value !== undefined &&
          matched === -1 &&
          !choicePaused && (
            <small className="danger-text">
              {choiceBasis === "independent"
                ? "当前值不在该字段允许范围内，原值已保留；请选择允许值或清空后重新选择。"
                : "此值与当前参数不兼容，原值已保留；请选择兼容值或清空后重新选择。"}
            </small>
          )}
        <small>
          {String(schema.description ?? "")}
          {schema.default !== undefined
            ? ` 默认值说明：${optionLabel(schema.default)}。`
            : ""}
          {schema.minimum !== undefined ? ` 最小值 ${schema.minimum}。` : ""}
          {schema.maximum !== undefined ? ` 最大值 ${schema.maximum}。` : ""}
          {(type === "number" || type === "integer") &&
          schema.exclusiveMinimum !== undefined
            ? ` 必须大于 ${displayJsonValue(schema, "exclusiveMinimum", schema.exclusiveMinimum)}。`
            : ""}
          {(type === "number" || type === "integer") &&
          schema.exclusiveMaximum !== undefined
            ? ` 必须小于 ${displayJsonValue(schema, "exclusiveMaximum", schema.exclusiveMaximum)}。`
            : ""}
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
