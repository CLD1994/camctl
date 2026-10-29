"""Action6 安装版 CLI 入口上的测试专用真实仓储同步点。"""

from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys
import time


def _record_cleanup_run() -> None:
    """记录实际 run 进入前的历史边界，不修改状态库或清理结果。"""
    if sys.argv[1:2] != ["run"]:
        return
    state_db = Path(os.environ["CAMCTL_ACTION6_TEST_STATE_DB"])
    with closing(sqlite3.connect(state_db.as_uri() + "?mode=ro", uri=True)) as connection:
        horizon = connection.execute("SELECT MAX(id) FROM history_events").fetchone()[0] or 0
    from _osmo_builtin_clock_entry import _append_record

    _append_record(Path(os.environ["CAMCTL_ACTION6_TEST_CLEANUP_RUNS"]), {
        "pid": os.getpid(), "history_horizon": horizon,
        "started_monotonic_ns": time.monotonic_ns(),
    })


def main() -> int:
    _record_cleanup_run()
    if os.environ.get("CAMCTL_ACTION6_TEST_OBSERVER_STAGE"):
        from _action6_segmented_observer import install

        install()
    from _osmo_builtin_clock_entry import main as clocked_cli_main

    return clocked_cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
