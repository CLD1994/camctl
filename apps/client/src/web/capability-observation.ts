import type { CapabilityState } from "../shared/automatic-previews";

export function stabilizeCapabilities(
  previous: CapabilityState | undefined,
  incoming: CapabilityState,
): CapabilityState {
  if (
    previous?.active &&
    incoming.active &&
    incoming.version &&
    incoming.version === previous.version
  )
    return { ...incoming, active: previous.active };
  return incoming;
}
