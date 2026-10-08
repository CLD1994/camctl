"""I4 无设备的真实导出、报告、领取与 ACK 闭环。

真实 camctl CLI（无设备部署）完成受理与报告发布；WSL 编译的真实
host-demo 主程序经受管工具调用 camctl submit/run，并把 ready 报告
领取到 processing；客户端经其服务的真实导出路径产生计划（消费
describe 能力、分配整数请求身份），经真实导入路径核验并保存报
告，随后导出的下一份计划自动携带 last_report_id（ACK），由统一
受理接口吸收。C 模块为 POSIX 实现，按部署验证裁决在 WSL x86
Linux 构建，操作同一部署目录。
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from _wsl_host_demo import HostDemo, query_state, to_wsl, wait_for
from camctl_fixtures import Deployment, future_schedule, terminate_process_tree


def _query(state_db: Path, sql: str, params=()) -> list[tuple]:
    return query_state(state_db, sql, params)


def _await(predicate, timeout_s: float, message: str):
    return wait_for(predicate, timeout_s, message)


def _plan_body(name: str) -> dict:
    return {
        "name": name,
        "actions": [
            {
                "name": "sync",
                "type": "report_status",
                "scheduled_at": future_schedule(1),
                "params": {"scope": "full"},
            }
        ],
    }


def _deployment_with_client(tmp_path: Path) -> tuple[Deployment, Path]:
    """初始化无设备部署，并把 describe 能力装入客户端存储。"""
    deployment = Deployment(tmp_path, devices=False)
    initialized = deployment.camctl(
        "init", "--config", str(deployment.config_path))
    assert initialized.exit_code == 0, initialized.stderr
    described = deployment.camctl(
        "describe", "--config", str(deployment.config_path))
    assert described.exit_code == 0, described.stderr
    assert json.loads(described.stdout) == {"devices": []}
    client_store = deployment.root / "client-store"
    client_store.mkdir()
    (client_store / "device-capabilities.json").write_text(
        described.stdout, encoding="utf-8")
    return deployment, deployment.state_db


def _published_report(deployment: Deployment, demo: HostDemo,
                      plan_path: Path) -> tuple[int, str]:
    """递交计划并等待报告发布，返回（报告 id、ready 文件名）。"""
    demo.submit(plan_path)
    ready_name = _await(
        lambda: glob.glob("status-report-*.json",
                          root_dir=str(deployment.ready)) or None,
        timeout_s=120, message="报告未发布到 ready")[0]
    _await(lambda: (
        _query(deployment.state_db,
               "SELECT status FROM actions WHERE name = 'sync'"),
        _query(deployment.state_db, "SELECT status FROM plans"),
    ) == ([(3,)], [(3,)]) or None,
        timeout_s=120, message="同步动作与计划未到终态")
    rows = _query(
        deployment.state_db,
        "SELECT id FROM reports WHERE status = 4 ORDER BY id")
    assert rows, "没有已发布报告"
    return rows[-1][0], ready_name


def test_report_import_ack_roundtrip(tmp_path: Path, host_demo: str) -> None:
    """无设备闭环：导出→submit/run→发布→领取→导入→ACK 吸收。

    请求身份由客户端分配并在主机侧保持；报告经 C 领取移动位置，
    客户端按原字节核验保存；下一份导出携带的 ACK 被受理接口吸收
    并推进主机累计确认。
    """
    deployment, state_db = _deployment_with_client(tmp_path)

    # 客户端真实导出：能力校验+整数请求身份分配；首份无 ACK。
    plan_path, receipt = deployment.export_plan_with_client(_plan_body("首份同步"))
    request_id = receipt["request_id"]
    assert receipt["has_ack"] is False

    demo = HostDemo(deployment, host_demo)
    try:
        demo.start()
        report_id, ready_name = _published_report(deployment, demo, plan_path)
        size_sha = _query(
            state_db, "SELECT size_bytes, sha256 FROM reports WHERE id = ?",
            (report_id,))[0]
        assert str(report_id) == json.loads(
            (deployment.ready / ready_name).read_text("utf-8")
        )["report_id"]

        demo.claim()
        # 领取后位置：ready 撤空、processing 收到同一文件（原字节）。
        _await(lambda: glob.glob("status-report-*",
                                  root_dir=str(deployment.processing)) or None,
               timeout_s=30, message="领取未移动报告到 processing")
        assert not glob.glob("status-report-*",
                             root_dir=str(deployment.ready))
        claimed = deployment.processing / ready_name
        content_bytes = claimed.read_bytes()
        assert len(content_bytes) == size_sha[0]
        assert hashlib.sha256(content_bytes).hexdigest() == size_sha[1]

        # 客户端真实导入：原字节核验、可靠保存并形成 ACK 依据。
        saved = deployment.import_reports_with_client(deployment.processing)
        assert saved["saved_report_ids"] == [str(report_id)]
        assert saved["ack_id"] == str(report_id)

        # 第二份导出自动携带 ACK；经主程序递交后被受理接口吸收。
        second_plan, second_receipt = deployment.export_plan_with_client(
            _plan_body("第二份同步"))
        assert second_receipt["has_ack"] is True
        assert second_receipt["last_report_id"] == str(report_id)
        demo.submit(second_plan)
        _await(lambda: _query(
            state_db,
            "SELECT acknowledged_report_id, acknowledged_wm FROM runtime_state"
        ) == [(report_id, _query(
            state_db, "SELECT to_wm FROM reports WHERE id = ?", (report_id,))[0][0])]
        or None, timeout_s=120, message="ACK 未被吸收")

        # 主程序侧事实：两次 submit 与领取都有模块日志记录。
        log = demo.module_log()
        assert f"submit accepted path={to_wsl(plan_path)}" in log
        assert "claim" in log
    finally:
        demo.stop()


def test_invalid_body_with_valid_ack_absorbs_ack_only(
        tmp_path: Path, host_demo: str) -> None:
    """非法正文但有效 ACK：正文被拒、不建计划，ACK 仍被吸收。"""
    deployment, state_db = _deployment_with_client(tmp_path)
    plan_path, _ = deployment.export_plan_with_client(_plan_body("同步前序"))
    demo = HostDemo(deployment, host_demo)
    try:
        demo.start()
        report_id, _ = _published_report(deployment, demo, plan_path)
        plans_before = _query(state_db, "SELECT COUNT(*) FROM plans")[0][0]
        # 正文缺少名称与动作：受理必须拒绝；顶层 ACK 引用已发布报告。
        invalid = deployment.root / "invalid-with-ack.json"
        invalid.write_text(json.dumps({
            "request_id": "414141",
            "last_report_id": str(report_id),
        }, ensure_ascii=False), encoding="utf-8")
        demo.submit(invalid)
        _await(lambda: _query(
            state_db,
            "SELECT acknowledged_report_id FROM runtime_state"
        ) == [(report_id,)] or None,
            timeout_s=120, message="合法 ACK 未被吸收")
        # 正文被拒绝：不产生新计划，也不产生新动作。
        assert _query(state_db, "SELECT COUNT(*) FROM plans")[0][0] \
            == plans_before
        assert not _query(
            state_db, "SELECT id FROM plans WHERE request_id = 414141")
    finally:
        demo.stop()


def test_resubmitted_request_keeps_single_identity(
        tmp_path: Path, host_demo: str) -> None:
    """同一请求重送：计划与动作身份不重复，重送不产生新事实。"""
    deployment, state_db = _deployment_with_client(tmp_path)
    plan_path, receipt = deployment.export_plan_with_client(_plan_body("重送同步"))
    request_id = int(receipt["request_id"])
    demo = HostDemo(deployment, host_demo)
    try:
        demo.start()
        demo.submit(plan_path)
        _await(lambda: _query(
            state_db, "SELECT id FROM plans WHERE request_id = ?",
            (request_id,)) or None,
            timeout_s=120, message="首次递交未受理")
        demo.submit(plan_path)
        # 会话推进到计划终态后重送：身份与事实数量保持一次受理。
        _await(lambda: _query(
            state_db, "SELECT status FROM plans WHERE request_id = ?",
            (request_id,)) == [(3,)] or None,
            timeout_s=120, message="计划未到终态")
        assert _query(
            state_db, "SELECT COUNT(*) FROM plans WHERE request_id = ?",
            (request_id,)) == [(1,)]
        assert _query(
            state_db, "SELECT COUNT(*) FROM actions WHERE plan_id ="
            " (SELECT id FROM plans WHERE request_id = ?)",
            (request_id,)) == [(1,)]
    finally:
        demo.stop()


def test_report_generation_failure_preserves_responsibility(
        tmp_path: Path) -> None:
    """普通生成失败：失败责任保留、同步动作不虚构终态、会话按
    report_error 退出；下一会话首轮重试并发布同一报告。

    会话边界由直接 CLI 驱动：重试语义要求新会话没有可冻结的新工
    作，C 主程序的受管通道只有 submit/claim，无法表达空 run。
    """
    deployment, state_db = _deployment_with_client(tmp_path)
    plan_path, _ = deployment.export_plan_with_client(_plan_body("生成失败重试"))

    # staging 的报告子目录被普通文件占据：生成子进程无法写出报告文
    # 件，构成与状态库无关的普通生成失败。
    reports_staging = deployment.staging / "reports"
    reports_staging.write_text("", encoding="utf-8")

    submitted = deployment.camctl(
        "submit", str(plan_path), "--config", str(deployment.config_path))
    assert submitted.exit_code == 0, submitted.stderr
    failing = deployment.camctl(
        "run", "--config", str(deployment.config_path))
    # 会话不为等待报告重试持续运行：按报告失败退出并携带机器原因。
    assert failing.exit_code == 1
    failure = json.loads(failing.stdout)
    assert failure["kind"] == "error"
    assert failure["body"]["reason"] == "report_error"

    # 同步动作与计划保留未完成事实（不虚构为成功或业务失败）；报告
    # 保留失败责任且从未取得确定字节；ready 不出现报告文件。
    assert _query(state_db, "SELECT status FROM plans") == [(2,)]
    assert _query(
        state_db, "SELECT status FROM actions WHERE name = 'sync'") == [(2,)]
    rows = _query(
        state_db,
        "SELECT id, status, size_bytes, sha256, last_error_json FROM reports")
    assert len(rows) == 1
    report_id, status, size, sha, error = rows[0]
    assert status == 5
    assert (size, sha) == (None, None)
    assert "生成失败" in json.loads(error)["error"]
    assert _query(
        state_db, "SELECT local_report_id FROM state_syncs") == [(None,)]
    assert not glob.glob("status-report-*", root_dir=str(deployment.ready))

    # 故障消除后：新的 run 会话在首轮重试此前失败的报告并发布；同
    # 步动作以该报告本地完成，计划随后到终态，覆盖这些变化的下一
    # 份报告在同会话发布并按生命周期撤下旧文件。
    reports_staging.unlink()
    retrying = deployment.camctl(
        "run", "--config", str(deployment.config_path))
    assert retrying.exit_code == 0, retrying.stderr
    assert _query(state_db, "SELECT status FROM plans") == [(3,)]
    assert _query(
        state_db, "SELECT status FROM actions WHERE name = 'sync'") == [(3,)]
    assert _query(state_db, "SELECT status FROM reports") == [(4,), (4,)]
    assert _query(
        state_db, "SELECT local_report_id FROM state_syncs") == [(report_id,)]
    names = glob.glob("status-report-*.json", root_dir=str(deployment.ready))
    assert len(names) == 1
    latest = json.loads((deployment.ready / names[0]).read_text("utf-8"))
    saved = deployment.import_reports_with_client(deployment.ready)
    assert saved["saved_report_ids"] == [latest["report_id"]]


def test_report_save_failure_retry_keeps_determined_bytes(
        tmp_path: Path) -> None:
    """报告保存失败重试：字节已登记的失败报告由下一会话重建发布，
    重建字节与首次确定字节保持一致。"""
    deployment, state_db = _deployment_with_client(tmp_path)
    plan_path, _ = deployment.export_plan_with_client(_plan_body("保存失败重试"))

    # ready 位置被普通文件占据：报告字节已生成并登记，交接无法进行。
    shutil.rmtree(deployment.ready)
    deployment.ready.write_text("", encoding="utf-8")

    submitted = deployment.camctl(
        "submit", str(plan_path), "--config", str(deployment.config_path))
    assert submitted.exit_code == 0, submitted.stderr
    failing = deployment.camctl(
        "run", "--config", str(deployment.config_path))
    assert failing.exit_code == 1
    failure = json.loads(failing.stdout)
    assert failure["kind"] == "error"
    assert failure["body"]["reason"] == "report_error"

    # 同步动作与计划保留未完成事实；报告保留保存失败责任，首次生成
    # 已确定的字节事实不被失败抹除。
    assert _query(state_db, "SELECT status FROM plans") == [(2,)]
    assert _query(
        state_db, "SELECT status FROM actions WHERE name = 'sync'") == [(2,)]
    rows = _query(
        state_db,
        "SELECT id, status, size_bytes, sha256, last_error_json FROM reports")
    assert len(rows) == 1
    report_id, status, determined, sha, error = rows[0]
    assert status == 5
    assert determined is not None and sha is not None
    assert "发布失败" in json.loads(error)["error"]
    assert _query(
        state_db, "SELECT local_report_id FROM state_syncs") == [(None,)]

    # 恢复 ready 后：下一会话首轮重建同一报告并发布；重建字节与首
    # 次确定字节不一致会在字节登记事务被拒绝，发布不可能完成。
    deployment.ready.unlink()
    deployment.ready.mkdir()
    retrying = deployment.camctl(
        "run", "--config", str(deployment.config_path))
    assert retrying.exit_code == 0, retrying.stderr
    assert _query(state_db, "SELECT status FROM plans") == [(3,)]
    assert _query(
        state_db, "SELECT status FROM actions WHERE name = 'sync'") == [(3,)]
    assert _query(
        state_db, "SELECT status, size_bytes, sha256 FROM reports WHERE id = ?",
        (report_id,)) == [(4, determined, sha)]
    assert _query(state_db, "SELECT status FROM reports") == [(4,), (4,)]
    assert _query(
        state_db, "SELECT local_report_id FROM state_syncs") == [(report_id,)]
    names = glob.glob("status-report-*.json", root_dir=str(deployment.ready))
    assert len(names) == 1
    latest = json.loads((deployment.ready / names[0]).read_text("utf-8"))
    saved = deployment.import_reports_with_client(deployment.ready)
    assert saved["saved_report_ids"] == [latest["report_id"]]


def _report_plan(request_id: str, action: str, scheduled_at: str) -> dict:
    """直接递交的 report_status 计划：仅保持会话驻留，不在测试内到时。"""
    return {
        "request_id": request_id,
        "created_at": "2026-01-15 08:00:00",
        "name": f"plan-{request_id}",
        "actions": [
            {
                "name": action,
                "type": "report_status",
                "scheduled_at": scheduled_at,
                "params": {"scope": "full"},
            }
        ],
    }


def _log_copies(directory: Path) -> list[Path]:
    return sorted(directory.glob("log-copy-*.log"))


@pytest.mark.skipif(
    os.name != "posix",
    reason="会话中替换数据库文件的故障注入需要 POSIX 重命名语义；"
           "按部署验证裁决由 WSL x86 Linux 承载")
def test_state_db_failure_delivers_log_copy(
        tmp_path: Path, host_demo: str) -> None:
    """数据库失效日志副本：会话执行中状态库文件失效时报告处理失败，
    日志副本不经状态库直接交付 ready；报告处理可靠恢复后故障轮结
    束（标记删除），再次失效取得新一轮首次触发；C 主程序按既有
    ready→processing 流程中转日志副本，保留完整文件名和内容。

    注入点取报告生成子进程写出 staging 临时文件的时刻：报告流程
    阻塞等待生成完成，同轮其余流程不可能运行；已打开的数据库连接
    在自己的文件描述符上完成生成本轮的发布，下一轮报告流程打开
    状态库失败，构成不依赖轮询时序的报告处理失败。
    """
    deployment = Deployment(tmp_path, devices=False)
    initialized = deployment.camctl(
        "init", "--config", str(deployment.config_path))
    assert initialized.exit_code == 0, initialized.stderr
    marker_path = deployment.staging / "logs" / "failure-marker.json"
    hidden_db = deployment.state_db.with_name("state.db.hidden")
    reports_staging = deployment.staging / "reports"

    def _run_fault_round(plan_path: Path, tag: str) -> None:
        """递交计划并在报告生成子进程运行期间替换走会话的状态库。

        计划携带一个远期动作使会话在生成本轮完成后仍有未完成责
        任，必须进入下一轮；下一轮报告流程的连接打开失败触发日志
        副本，随后其余流程的连接打开失败使会话按状态库错误退出。
        """
        submitted = deployment.camctl(
            "submit", str(plan_path), "--config", str(deployment.config_path))
        assert submitted.exit_code == 0, submitted.stderr
        known = set(glob.glob("*.tmp", root_dir=str(reports_staging)))
        session = deployment.start_camctl(
            "run", "--config", str(deployment.config_path))
        try:
            def _generation_started():
                if session.poll() is not None:
                    session.wait()
                    raise AssertionError(
                        f"{tag}: 会话未开始生成就退出"
                        f"({session.returncode}): {session.stderr.read()[:400]}")
                return (set(glob.glob(
                    "*.tmp", root_dir=str(reports_staging))) - known) or None

            _await(_generation_started,
                   timeout_s=120, message=f"{tag}: 报告生成未开始")
            os.rename(deployment.state_db, hidden_db)
            stdout, _ = session.communicate(timeout=120)
        finally:
            if session.poll() is None:
                terminate_process_tree(session)
        assert session.returncode == 1
        failure = json.loads(stdout)
        assert failure["kind"] == "error"
        assert failure["body"]["reason"] == "state_db_error"

    # 故障轮一：日志副本随报告处理失败交付，不依赖状态库可用。
    first = deployment.write_plan(
        _report_plan("5001", "sync-one", future_schedule(12)))
    _run_fault_round(first, "故障轮一")
    copies = _log_copies(deployment.ready)
    assert len(copies) == 1
    first_copy = copies[0]
    trigger_text = first_copy.read_text(encoding="utf-8", errors="replace")
    # 触发记录呈现报告处理失败及其状态库原因；错误类型取决于失效
    # 被哪个打开动作观察到（文件缺失或打开失败），不逐字断言。
    assert "报告处理失败: " in trigger_text
    assert "状态库" in trigger_text
    marker = json.loads(marker_path.read_bytes())
    assert marker["copy_name"] == first_copy.name
    assert marker["stage"] == "published"
    first_round_id = marker["round_id"]

    # 恢复：状态库归位后下一会话完成报告处理并正常退出，故障轮结
    # 束、标记删除。
    os.rename(hidden_db, deployment.state_db)
    recovered = deployment.camctl(
        "run", "--config", str(deployment.config_path))
    assert recovered.exit_code == 0, recovered.stderr
    assert not marker_path.exists()
    assert _query(
        deployment.state_db,
        "SELECT status FROM actions WHERE name = 'sync-one'") == [(3,)]
    assert _query(
        deployment.state_db, "SELECT status FROM plans") == [(3,)]

    # 故障轮二：恢复后的再次失效取得新的首次触发（新副本、新轮次）。
    second = deployment.write_plan(
        _report_plan("5002", "sync-two", future_schedule(60)))
    _run_fault_round(second, "故障轮二")
    copies = _log_copies(deployment.ready)
    assert len(copies) == 2
    second_marker = json.loads(marker_path.read_bytes())
    assert second_marker["stage"] == "published"
    assert second_marker["round_id"] != first_round_id
    assert second_marker["copy_name"] not in {first_copy.name}
    # 副本是复制时刻普通日志的字节快照：两份副本互为前缀，且都是最
    # 终日志文件的前缀（此后日志只追加）。
    log_bytes = (deployment.home / "camctl.log").read_bytes()
    snapshots = sorted(
        (path.read_bytes() for path in copies), key=len)
    assert log_bytes.startswith(snapshots[1])
    assert snapshots[1].startswith(snapshots[0])

    # C 主程序领取：日志副本与报告一起按既有流程移动到 processing，
    # 文件名与内容保持，ready 撤空。
    demo = HostDemo(deployment, host_demo)
    try:
        demo.start()
        payloads = {path.name: path.read_bytes() for path in copies}
        demo.claim()
        _await(lambda: len(_log_copies(deployment.processing)) == 2 or None,
               timeout_s=30, message="领取未中转日志副本")
        assert not _log_copies(deployment.ready)
        for name, payload in payloads.items():
            assert (deployment.processing / name).read_bytes() == payload
        module_log = demo.module_log()
        for name in payloads:
            assert "operation=moved" in module_log
            assert f"file={name}" in module_log
    finally:
        demo.stop()
