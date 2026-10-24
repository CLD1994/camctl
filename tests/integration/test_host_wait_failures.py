"""等待观察器通过真实 C host、文件日志和 SQLite 提供失败诊断。"""

from __future__ import annotations

import json
import os
import shlex
import sqlite3
from types import SimpleNamespace

import pytest

from _wsl_host_demo import HostDemo, wait_for
from camctl.contracts.enums import enum_for


def _deployment(root):
    ready, processing = root / "ready", root / "processing"
    ready.mkdir()
    processing.mkdir()
    return SimpleNamespace(
        root=root, ready=ready, processing=processing, config_path=None,
        state_db=root / "state.db",
    )


@pytest.mark.skipif(os.name != "posix", reason="真实 POSIX host 观察器")
@pytest.mark.parametrize("outcome", [
    "error", "abnormal", "success", "not_started", "abnormal_once", "not_started_once",
])
def test_wait_observes_current_run_result_while_host_stays_alive(tmp_path, host_demo, outcome):
    deployment = _deployment(tmp_path)
    launcher = tmp_path / "cli"
    message = {"kind": "error", "body": {"reason": "state_db_error", "details": {"stage": "execution"}}}
    if outcome in {"success", "abnormal_once", "not_started_once"}:
        message = {"kind": "succeeded"}
    script = (
        "#!/bin/sh\nprintf '%s\\n' '" + json.dumps(message) + "'\n"
        + ("printf '%s\\n' 'result=error is diagnostic text' >&2\n" if outcome == "success" else "")
        + ("exit 0\n" if message["kind"] == "succeeded" else "exit 1\n" if outcome == "error" else "exit 7\n")
    )
    if outcome == "abnormal_once":
        marker = tmp_path / "first-attempt"
        script = "#!/bin/sh\nif [ ! -f '" + str(marker) + "' ]; then touch '" + str(marker) + "'; exit 7; fi\n" + script
    launcher.write_text(script, encoding="utf-8")
    launcher.chmod(0o700)
    if outcome in {"not_started", "not_started_once"}:
        launcher.unlink()
    host = tmp_path / "host"
    # 永久故障只需走完一次真实重试；恢复分区保留创建入口的时间。
    retry_delay = 5000 if outcome.endswith("_once") else 0
    host.write_text(
        "#!/bin/sh\nexec " + shlex.quote(host_demo)
        + f' --retry-limit 1 --retry-delay-ms {retry_delay} "$@"\n',
        encoding="utf-8",
    )
    host.chmod(0o700)
    demo = HostDemo(deployment, str(host), launcher=str(launcher))
    # 复用同一部署目录时，前一个 host 的失败不能污染新 host。
    demo.log_path.write_text("call=1 command=run pid=99 result=error exit=1\n", encoding="utf-8")
    check_failure = demo.watch_failures()
    try:
        demo.start()
        if outcome == "not_started_once":
            wait_for(lambda: "not_started errno=" in demo.module_log(), 30, "未观察到启动失败")
            assert check_failure() is None
            launcher.write_text(script, encoding="utf-8")
            launcher.chmod(0o700)
        if outcome in {"success", "abnormal_once", "not_started_once"}:
            result = wait_for(
                lambda: "finished" if "result=success" in demo.module_log() else None,
                30, "正常 run 未结束", failure=check_failure,
            )
            assert result == "finished"
            assert check_failure() is None
            assert wait_for(lambda: ["ready"], 30, "兼容原调用") == ["ready"]
        else:
            with pytest.raises(AssertionError, match="run 调用") as failure:
                wait_for(lambda: None, 30, "等待业务同步点", failure=check_failure)
            assert "等待超时" not in str(failure.value)
            assert "command=run" in str(failure.value)
            if outcome == "error":
                assert "state_db_error" in str(failure.value)
            # 之后的新请求有独立观察起点，并可重新建立 run 职责。
            later_log_start = len(demo.module_log())
            later_failure = demo.watch_failures()
            launcher.write_text(
                '#!/bin/sh\nif [ "$1" = submit ]; then\n'
                'printf \'%s\\n\' \'{"kind":"succeeded","body":{"needs_run":true}}\'\n'
                'else printf \'%s\\n\' \'{"kind":"succeeded"}\'; fi\n',
                encoding="utf-8",
            )
            launcher.chmod(0o700)
            plan = tmp_path / "later-plan.json"
            plan.write_text("{}", encoding="utf-8")
            demo.submit(plan)
            wait_for(
                lambda: any("command=run" in line and "result=success" in line
                            for line in demo.module_log()[later_log_start:].splitlines()),
                30, "后续请求未重新完成 run", failure=later_failure,
            )
        assert demo.proc.poll() is None
    finally:
        demo.stop()


@pytest.mark.parametrize("name", ["PENDING", "RUNNING", "SUCCEEDED", "FAILED", "EXPIRED", "CANCELED"])
def test_wait_limits_incompatible_terminal_to_requested_actions(tmp_path, name):
    deployment = _deployment(tmp_path)
    statuses = enum_for("actions.status")
    with sqlite3.connect(deployment.state_db) as connection:
        connection.executescript(
            "CREATE TABLE plans(id INTEGER,request_id TEXT);"
            "CREATE TABLE actions(id INTEGER,plan_id INTEGER,name TEXT,status INTEGER,"
            "error_code INTEGER,error_details_json TEXT);"
            "INSERT INTO plans VALUES(1,'old'),(2,'current');"
        )
        connection.executemany(
            "INSERT INTO actions VALUES(?,?,?,?,NULL,NULL)",
            [(1, 1, "capture", int(statuses.FAILED)),
             (2, 2, "other", int(statuses.FAILED)),
             (3, 2, "capture", int(statuses[name]))],
        )
    demo = HostDemo(deployment, "unused")
    reason = demo.watch_failures(request_id="current", expected_statuses={"capture": "SUCCEEDED"})()
    if name in {"FAILED", "EXPIRED", "CANCELED"}:
        assert "current" in reason and "capture" in reason and name in reason
    else:
        assert reason is None
