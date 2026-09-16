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
