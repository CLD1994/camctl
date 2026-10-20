import {
  displayJsonValue,
  exactJsonValueIdentity,
  originalNumberToken,
  rememberNumberToken,
} from "../shared/json";
import { isLocalScalarSchema } from "./scalar-schema";

/** 数字的机器身份始终携带其所属容器，不从 Number 投影重建。 */
export interface ScalarChoice {
  value: unknown;
  parent: object;
  key: string | number;
  label: string;
}
export function optionLabel(
  value: unknown,
  parent?: unknown,
  key: string | number = "",
): string {
  if (typeof value === "string") return value === "" ? "空字符串" : value;
  if (typeof value === "boolean") return value ? "是" : "否";
  return displayJsonValue(parent, key, value);
}
export function titledChoiceLabels(
  choices: Array<{
    value: unknown;
    parent: object;
    key: string | number;
    title?: unknown;
  }>,
): ScalarChoice[] {
  const titles = choices.map((choice) =>
    typeof choice.title === "string" && choice.title.trim()
      ? choice.title
      : undefined,
  );
  const counts = new Map<string, number>();
  for (const title of titles)
    if (title) counts.set(title, (counts.get(title) ?? 0) + 1);
  return choices.map((choice, index) => ({
    value: choice.value,
    parent: choice.parent,
    key: choice.key,
    label: titles[index]
      ? titles[index] +
        ((counts.get(titles[index]!) ?? 0) > 1
          ? `（${optionLabel(choice.value, choice.parent, choice.key)}）`
          : "")
      : optionLabel(choice.value, choice.parent, choice.key),
  }));
}
const cache = new WeakMap<
  Record<string, unknown>,
  ScalarChoice[] | undefined
>();
/** 仅接受已证明可解释的直接标量常量分支；规则校验仍由原 Schema 负责。 */
export function scalarChoices(
  schema: Record<string, unknown>,
): ScalarChoice[] | undefined {
  if (cache.has(schema)) return cache.get(schema);
  const result =
    Array.isArray(schema.oneOf) && isLocalScalarSchema(schema)
      ? titledChoiceLabels(
          schema.oneOf.map((branch) => ({
            value: branch.const,
            parent: branch,
            key: "const",
            title: branch.title,
          })),
        )
      : undefined;
  cache.set(schema, result);
  return result;
}
export function scalarChoiceValues(choices: ScalarChoice[]): unknown[] {
  const values = choices.map((choice) => choice.value);
  choices.forEach((choice, index) =>
    rememberNumberToken(
      values,
      index,
      originalNumberToken(choice.parent, choice.key, choice.value),
    ),
  );
  return values;
}
const labels = new WeakMap<Record<string, unknown>, Map<string, string>>();
export function scalarChoiceLabel(
  schema: Record<string, unknown>,
  value: unknown,
  parent?: unknown,
  key: string | number = "",
): string {
  let byIdentity = labels.get(schema);
  if (!byIdentity) {
    byIdentity = new Map(
      (scalarChoices(schema) ?? []).map((choice) => [
        exactJsonValueIdentity(choice.value, true, choice),
        choice.label,
      ]),
    );
    labels.set(schema, byIdentity);
  }
  return (
    byIdentity.get(exactJsonValueIdentity(value, true, { parent, key })) ??
    optionLabel(value, parent, key)
  );
}
