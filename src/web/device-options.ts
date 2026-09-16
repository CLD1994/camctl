import type { Capabilities } from "../shared/types";
import { ACTION_TYPES } from "../shared/plan";
import { builtinFields } from "../shared/action-params";

export function deviceOptions(
  capabilities: Capabilities | null,
  deviceId: unknown,
  actionType: unknown,
) {
  const devices = capabilities?.devices ?? [];
  const builtin =
    typeof actionType === "string" && Object.hasOwn(builtinFields, actionType);
  const scope =
    deviceId === undefined || deviceId === "" || builtin
      ? devices
      : devices.filter((d) => d.device_id === deviceId);
  return {
    devices:
      actionType === "camera_record"
        ? devices.filter((d) => d.actions.some((a) => a.type === actionType))
        : devices,
    actions: ACTION_TYPES.filter(
      (type) =>
        Object.hasOwn(builtinFields, type) ||
        scope.some((d) => d.actions.some((a) => a.type === type)),
    ),
  };
}
