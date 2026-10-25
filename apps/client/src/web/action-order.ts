import { isAutomaticPreviewAction } from "../shared/automatic-previews";

/** 普通界面的序号独立于 JSON 数组位置，自动预览只保留技术身份。 */
export function userActionOrdinals(actions: readonly unknown[]) {
  let ordinal = 0;
  return actions.map((action) =>
    isAutomaticPreviewAction(action) ? undefined : ++ordinal,
  );
}
