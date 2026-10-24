"""一次结果扫描拥有者持有的有限工作缓存。"""

from dataclasses import dataclass
from typing import Any

from camctl.capture.baseline_models import BaselineChunk, BaselineRef
from camctl.capture.result_pages import ResultPageRef
from camctl.devices.bindings import DeviceBinding
from camctl.devices.directory import DirectoryCursor
from camctl.operations.models import AttemptTicket


@dataclass
class ResultReadingContext:
    """暂停时保留清单；关闭后不能重新冒充原尝试的首页。"""

    ticket: AttemptTicket
    binding: DeviceBinding | None = None
    directories: tuple[str, ...] | None = None
    directory_scan: Any | None = None
    directory_cursor: DirectoryCursor | None = None
    directory_started: bool = False
    closed: bool = False
    baseline_ref: BaselineRef | None = None
    baseline_chunk: BaselineChunk | None = None
    baseline_cursor: int | None = None
    baseline_index: int = 0
    baseline_exhausted: bool = False
    candidate_last_key: tuple[str, str, str] | None = None
    history_owner: object | None = None
    history_initialized: bool = False
    verified_last_ref: ResultPageRef | None = None

    def close(self) -> None:
        """目录调用已经收场后释放本地清单；重复关闭保持同一结果。"""
        if self.directory_scan is not None:
            self.directory_scan.close()
            self.directory_scan = None
        self.closed = True
        self.baseline_chunk = None
