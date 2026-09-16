import schema from "../../../../protocol/schemas/status-report.schema.json";
import type { ActionType, CameraActionType } from "./status-report.generated";
export type { CameraActionType };
export const CAMERA_ACTION_TYPES: readonly CameraActionType[] = schema.$defs
  .camera_action_type.enum as CameraActionType[];
export const ACTION_TYPES: readonly ActionType[] = [
  ...CAMERA_ACTION_TYPES,
  ...schema.$defs.action_type.anyOf[1].enum!,
] as ActionType[];
export function isCameraAction(value: unknown): value is CameraActionType {
  return CAMERA_ACTION_TYPES.some((type) => type === value);
}
