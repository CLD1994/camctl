import {
  AppError,
  DRAFT_COMMON_ACTION_FIELDS,
  type DraftContent,
  type ObjectVariantMap,
} from "../server/models";
import { parseClientJson, exactJsonIdentity } from "./json";
import { validPreviewMetadata } from "./automatic-previews";
import {
  checkParameterVariants,
  checkParameterVariantLocations,
  checkActiveParameterVariant,
} from "./parameter-variants";
import {
  checkObjectVariants,
  checkObjectVariantLocations,
} from "./object-variants";
function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** 编辑资料的完整结构契约；业务原文是否合法由导出校验负责。 */
export function checkDraftContent(
  input: unknown,
): asserts input is DraftContent {
  const content = input as DraftContent;
  if (!object(content) || typeof content.text !== "string")
    throw new AppError("invalid_content", "草稿必须包含编辑文本");
  if (
    content.automaticPreviews !== undefined &&
    !validPreviewMetadata(content.automaticPreviews)
  )
    throw new AppError("invalid_content", "自动预览编辑资料格式不正确");
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
  if (content.actionVariants !== undefined) {
    const fail = () => {
      throw new AppError("invalid_content", "动作类型编辑资料格式不正确");
    };
    if (!object(content.actionVariants)) fail();
    for (const [index, variants] of Object.entries(content.actionVariants)) {
      if (
        !/^(0|[1-9]\d*)$/.test(index) ||
        !Number.isSafeInteger(Number(index)) ||
        !Array.isArray(variants)
      )
        fail();
      for (const variant of variants) {
        if (
          !object(variant) ||
          !object(variant.fields) ||
          !object(variant.pending) ||
          Object.keys(variant).some(
            (key) =>
              ![
                "type",
                "fields",
                "fieldsText",
                "pending",
                "parameterVariants",
                "objectVariants",
              ].includes(key),
          ) ||
          Object.keys(variant.fields).some((key) =>
            [...DRAFT_COMMON_ACTION_FIELDS, "type"].includes(key),
          ) ||
          Object.entries(variant.pending).some(
            ([key, value]) =>
              !/^\/(?:[^~]|~[01])*$/.test(key) ||
              DRAFT_COMMON_ACTION_FIELDS.some(
                (field) => key === `/${field}` || key.startsWith(`/${field}/`),
              ) ||
              !object(value) ||
              !["number", "json"].includes(String(value.kind)) ||
              typeof value.text !== "string",
          )
        )
          fail();
        if (variant.parameterVariants !== undefined) {
          try {
            checkParameterVariants(variant.parameterVariants);
          } catch {
            fail();
          }
        }
        if (variant.objectVariants !== undefined) {
          try {
            const fields =
              variant.fieldsText === undefined
                ? variant.fields
                : parseClientJson(variant.fieldsText as string);
            checkObjectVariants(
              variant.objectVariants,
              (fields as Record<string, unknown>).params,
              true,
            );
          } catch {
            fail();
          }
        }
        if (variant.fieldsText !== undefined) {
          try {
            if (typeof variant.fieldsText !== "string") fail();
            const fields = parseClientJson(variant.fieldsText!);
            if (
              !object(fields) ||
              exactJsonIdentity(fields) !== exactJsonIdentity(variant.fields)
            )
              fail();
          } catch {
            fail();
          }
        }
        if (variant.parameterVariants !== undefined) {
          try {
            const fields =
              variant.fieldsText === undefined
                ? variant.fields
                : parseClientJson(variant.fieldsText);
            checkActiveParameterVariant(
              variant.parameterVariants,
              fields as Record<string, unknown>,
            );
          } catch {
            fail();
          }
        }
      }
    }
  }
  if (content.parameterVariants !== undefined) {
    try {
      if (!object(content.parameterVariants))
        throw Error("参数类型资料必须是动作位置映射");
      for (const [index, variants] of Object.entries(
        content.parameterVariants,
      )) {
        if (
          !/^(0|[1-9]\d*)$/.test(index) ||
          !Number.isSafeInteger(Number(index))
        )
          throw Error("参数类型资料动作位置不正确");
        checkParameterVariants(variants);
      }
    } catch {
      throw new AppError("invalid_content", "参数类型编辑资料格式不正确");
    }
  }
  if (content.objectVariants !== undefined) {
    try {
      if (!object(content.objectVariants))
        throw Error("对象分支资料必须是动作位置映射");
      for (const [index, map] of Object.entries(content.objectVariants)) {
        if (
          !/^(0|[1-9]\d*)$/.test(index) ||
          !Number.isSafeInteger(Number(index))
        )
          throw Error("对象分支动作位置不正确");
        checkObjectVariants(map);
      }
    } catch {
      throw new AppError("invalid_content", "对象分支编辑资料格式不正确");
    }
  }
  // 无法解析的正文仍可作为编辑原文保存；可解释时不能保存悬空的新资料位置。
  let root: unknown;
  try {
    root = parseClientJson(content.text);
  } catch {
    return;
  }
  if (!object(root) || !Array.isArray(root.actions)) {
    const hasObjectData = (map: ObjectVariantMap | undefined) =>
      Object.keys(map ?? {}).length > 0;
    const hasLocatedData =
      Object.values(content.objectVariants ?? {}).some(hasObjectData) ||
      Object.values(content.parameterVariants ?? {}).some(
        (variants) => variants.length > 0,
      ) ||
      Object.values(content.actionVariants ?? {}).some((variants) =>
        variants.some(
          (variant) =>
            hasObjectData(variant.objectVariants) ||
            (variant.parameterVariants?.length ?? 0) > 0,
        ),
      );
    if (hasLocatedData)
      throw new AppError(
        "invalid_content",
        "已解析的计划没有动作集合，无法确定参数及对象分支资料的归属",
      );
    return;
  }
  if (object(root) && Array.isArray(root.actions)) {
    try {
      checkParameterVariantLocations(content, root.actions);
      checkObjectVariantLocations(content, root.actions);
      for (const [index, variants] of Object.entries(
        content.parameterVariants ?? {},
      )) {
        const action = root.actions[Number(index)];
        if (object(action)) checkActiveParameterVariant(variants, action);
      }
    } catch {
      throw new AppError("invalid_content", "参数类型编辑资料的动作归属不正确");
    }
  }
}
