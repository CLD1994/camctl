import type { Capabilities } from "../shared/types";
import { ACTION_TYPES } from "../shared/plan";
import { builtinFields } from "../shared/action-params";

export function deviceOptions(
  capabilities: Capabilities | null,
  actionType: unknown,
) {
  const devices = capabilities?.devices ?? [];
  const requiresDevice = ACTION_TYPES.some(
    (type) => type === actionType && !Object.hasOwn(builtinFields, type),
  );
  return {
    requiresDevice,
    devices: requiresDevice
      ? devices.filter((d) => d.actions.some((a) => a.type === actionType))
      : [],
    actions: ACTION_TYPES.filter(
      (type) =>
        Object.hasOwn(builtinFields, type) ||
        devices.some((d) => d.actions.some((a) => a.type === type)),
    ),
  };
}

/** 未知来源和旧目录缺少声明时保留入口；只隐藏明确声明不支持的设备。 */
export function cleanupEntryVisible(
  capabilities: Capabilities | null,
  deviceId: unknown,
): boolean {
  if (typeof deviceId !== "string") return true;
  return (
    capabilities?.devices.find((device) => device.device_id === deviceId)
      ?.delete_supported !== false
  );
}
