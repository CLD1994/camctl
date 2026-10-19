"""会话内普通录像的截止提示；实际停止资格仍由录像状态机决定。"""

from dataclasses import dataclass


@dataclass
class _Deadline:
    action_id: int
    target_ns: int
    at: float
    early_checked: bool = False


class RecordingWakeups:
    """每设备保留一个原截止，提供一次提前检查和一次到期检查。"""

    def __init__(self):
        self._devices: dict[str, _Deadline] = {}

    def schedule(self, device_id: str, action_id: int, target_ns: int, *,
                 now_ns: int, now_s: float) -> bool:
        prior = self._devices.get(device_id)
        if prior is not None and (prior.action_id, prior.target_ns) == (action_id, target_ns):
            return False
        self._devices[device_id] = _Deadline(
            action_id, target_ns, now_s + max(0, target_ns - now_ns) / 1_000_000_000)
        return True

    def discard(self, device_id: str, action_id: int) -> bool:
        prior = self._devices.get(device_id)
        if prior is not None and prior.action_id == action_id:
            del self._devices[device_id]
            return True
        return False

    def clear(self) -> None:
        self._devices.clear()

    def next_at(self, now: float, margin_s: float) -> float | None:
        candidates = []
        for device_id, deadline in tuple(self._devices.items()):
            if now >= deadline.at:
                del self._devices[device_id]
                candidates.append(now)
            elif deadline.early_checked or margin_s == 0:
                candidates.append(deadline.at)
            elif now >= deadline.at - margin_s:
                deadline.early_checked = True
                candidates.append(deadline.at)
            else:
                candidates.append(deadline.at - margin_s)
        return min(candidates, default=None)
