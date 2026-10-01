"""主机收场守卫：遗留执行进程与下一会话放行。

新会话开始前核对旧会话遗留的本地执行进程：任一仍在运行或退出
未被确认时不放行；进程状态无法确认按未知保留，不解释为没有遗
留工作。本守卫只判定状态，不发送终止信号——收场动作由接入模
块按其收场规则执行，发送信号本身不触发放行。
"""

from __future__ import annotations

from enum import Enum
from typing import Iterable, Protocol

__all__ = [
    "HostSettlement",
    "SettlementProbe",
    "check_host_settlement",
]


class HostSettlement(Enum):
    """主机收场状态分区。"""

    CLEAR = "clear"
    WAIT = "wait"
    UNKNOWN = "unknown"


class SettlementProbe(Protocol):
    """遗留进程的探测端口。

    is_alive 返回 None 表示状态无法确认；wait_exit 确认已退出进程
    的实际结束与回收，返回 False 表示确认失败，保留诊断责任。
    """

    def is_alive(self, pid: int) -> bool | None: ...

    def wait_exit(self, pid: int, timeout_s: float) -> bool: ...


def check_host_settlement(
    pids: Iterable[int], probe: SettlementProbe, *, timeout_s: float = 30.0
) -> HostSettlement:
    """核对遗留进程集：全部停止并确认收场后才放行。

    状态未知的进程使整体保持未知，不与存活或已退出混同；发送终
    止信号不在此处发生，也不构成放行依据。
    """
    settled: list[bool] = []
    for pid in pids:
        alive = probe.is_alive(pid)
        if alive is None:
            return HostSettlement.UNKNOWN
        if alive:
            return HostSettlement.WAIT
        if not probe.wait_exit(pid, timeout_s):
            return HostSettlement.WAIT
        settled.append(True)
    return HostSettlement.CLEAR
