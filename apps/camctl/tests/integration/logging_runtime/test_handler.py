"""L4/L6 多进程文件写入、轮换与有序关闭的组合测试。

真实文件与 CLH：轮换失败、保留数量及两个独立进程写同一文件。
"""

from __future__ import annotations

import subprocess
import json
import sys
from pathlib import Path

import pytest

from camctl.logging_runtime.clh_adapter import FileChannel
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import LogRecord

pytestmark = pytest.mark.asyncio


def _record(message: str, level: LogLevel = LogLevel.INFO) -> LogRecord:
    return LogRecord(level=level, message=message)


class TestFileChannel:
    async def test_rotation_failure_does_not_hide_append(self, tmp_path: Path) -> None:
        channel = FileChannel(
            tmp_path / "camctl.log", max_bytes=32, file_count=2
        )
        channel.write_record(_record("w" * 40))  # 先超过阈值，使下一条触发轮换
        import unittest.mock as mock
        # 正式归档名建立失败时，当前文件仍可以独立追加。
        with mock.patch(
            "os.link",
            side_effect=OSError("轮换失败"),
        ):
            result = channel.write_record(_record("x" * 40))
        assert result.appended is True
        assert result.rotation_error is not None
        assert "轮换失败" in result.rotation_error
        channel.close()

    async def test_retention_keeps_active_plus_backups(self, tmp_path: Path) -> None:
        channel = FileChannel(
            tmp_path / "camctl.log", max_bytes=32, file_count=2
        )
        for index in range(12):
            result = channel.write_record(_record(f"line-{index:03d}"))
            assert result.appended is True
        channel.close()
        log_files = [p for p in tmp_path.iterdir() if p.name.startswith("camctl.log")]
        # 保留总量 2 = 活动文件 + 1 份归档。
        assert len(log_files) <= 2


class TestTwoProcesses:
    def test_two_processes_share_file(self, tmp_path: Path) -> None:
        script = (
            "from camctl.logging_runtime.clh_adapter import FileChannel\n"
            "from camctl.logging_runtime.models import LogLevel\n"
            "from camctl.logging_runtime.service import LogRecord\n"
            "import sys\n"
            "channel = FileChannel(sys.argv[1], max_bytes=2048, file_count=3)\n"
            "for index in range(30):\n"
            "    result = channel.write_record(LogRecord(level=LogLevel.INFO,"
            " message=f'proc-{sys.argv[2]}-line-{index:03d}'))\n"
            "    assert result.appended\n"
            "channel.close()\n"
        )
        target = tmp_path / "camctl.log"
        processes = [
            subprocess.run(
                [sys.executable, "-c", script, str(target), str(tag)],
                capture_output=True, text=True, timeout=60, cwd=str(Path(__file__).parents[3]),
            )
            for tag in (1, 2)
        ]
        for process in processes:
            assert process.returncode == 0, process.stderr
        combined = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(tmp_path.iterdir())
            if path.name.startswith("camctl.log")
        )
        # 结构化元数据增加行长；保留窗口仍包含两进程的近期记录并跨过轮换。
        assert (tmp_path / "camctl.log.1").exists()
        records = [json.loads(line) for line in combined.splitlines() if line]
        assert all(record["level"] == "INFO" for record in records)
        assert any(record["message"].startswith("proc-1-line-02") for record in records)
        assert any(record["message"].startswith("proc-2-line-02") for record in records)
