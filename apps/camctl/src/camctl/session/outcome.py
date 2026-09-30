"""机器会话结果的共享契约。

结果表达整个 CLI 会话的最终机器判定：成功或带稳定 reason 及
details 的错误。业务结果（计划拒绝等）通过状态报告表达，不改
变会话结果的种类。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

__all__ = ["SessionOutcome"]


def _empty_details() -> Mapping[str, Any]:
    return MappingProxyType({})


@dataclass(frozen=True)
class SessionOutcome:
    """一次 run/submit 会话的最终机器结果。

    succeeded 为真时 needs_run 按命令契约携带（submit 成功必须为
    布尔值，run 不携带）；为假时 reason 必须是稳定错误标识，
    details 提供上下文，默认为空对象。
    """

    succeeded: bool
    needs_run: bool | None = None
    reason: str | None = None
    details: Mapping[str, Any] = field(default_factory=_empty_details)

    def __post_init__(self) -> None:
        if not isinstance(self.succeeded, bool):
            raise ValueError(f"会话结果 succeeded 必须是布尔值: {self.succeeded!r}")
        if self.succeeded:
            if self.needs_run is not None and not isinstance(self.needs_run, bool):
                raise ValueError(f"needs_run 必须是布尔值: {self.needs_run!r}")
            if self.reason is not None:
                raise ValueError("成功结果不携带错误标识")
        else:
            if not isinstance(self.reason, str) or not self.reason:
                raise ValueError("错误结果必须携带非空 reason")
            if self.needs_run is not None:
                raise ValueError("错误结果不提供接管判定")
