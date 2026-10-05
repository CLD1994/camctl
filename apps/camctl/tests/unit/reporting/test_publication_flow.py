"""报告专属文件规则与发布编排；文件及状态库协作由替身提供。

覆盖文件名构造与解析、恢复观察决策表、补投资格决策表，以及发
布编排的证据顺序与失败分类；真实文件与 SQLite 组合由集成测试
验证。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from camctl.host_files.handoff import (
    HandoffDirectories,
    PublishResult,
    PublishStage,
    ReadyName,
    WithdrawResult,
    WithdrawStage,
)
from camctl.host_files.io import DirectorySyncStage
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.reporting.ack import AckReport
from camctl.reporting.models import FrozenReport, ReportBytes
from camctl.reporting import publication


_SHA = "ab" * 32
_OTHER_SHA = "cd" * 32


def _moved() -> PublishResult:
    return PublishResult(PublishStage.MOVED, DirectorySyncStage.SYNCED, True, None)


# ---- 文件名构造与解析 ------------------------------------------------


@pytest.mark.parametrize("report_id", [1, 42, 2**63 - 1])
def test_report_file_name_uses_canonical_decimal(report_id):
    assert publication.report_file_name(report_id, _SHA) == (
        f"status-report-{report_id}-{_SHA}.json"
    )


@pytest.mark.parametrize("report_id", [0, -1, "1", True, None, 1.0])
def test_report_file_name_rejects_non_canonical_identity(report_id):
    with pytest.raises(ValueError):
        publication.report_file_name(report_id, _SHA)


@pytest.mark.parametrize("sha", ["a" * 63, _SHA.upper(), "g" * 64, 5, None, ""])
def test_report_file_name_rejects_invalid_digest(sha):
    with pytest.raises(ValueError):
        publication.report_file_name(1, sha)


def test_parse_round_trips_legal_name():
    name = publication.report_file_name(7, _SHA)
    identity = publication.parse_report_file_name(name)
    assert identity == publication.ReportFileIdentity(7, _SHA)


@pytest.mark.parametrize("name", [
    "",
    "status-report-.json",
    f"status-report-01-{_SHA}.json",
    f"status-report-0-{_SHA}.json",
    f"status-report-+1-{_SHA}.json",
    f"status-report-1-{_SHA.upper()}.json",
    f"status-report-1-{_SHA[:-1]}.json",
    f"status-report-1-{_SHA}",
    f"status-report-1-{_SHA}.json.bak",
    f"status-report-1-{_SHA}.JSON",
    f" status-report-1-{_SHA}.json",
    "report-1-{sha}.json".format(sha=_SHA),
    "other-name.json",
    f"sub/status-report-1-{_SHA}.json",
])
def test_parse_rejects_malformed_names(name):
    assert publication.parse_report_file_name(name) is None


# ---- 恢复观察决策表 --------------------------------------------------


def _locations(*, ready=(), processing=(), staging=(),
               ready_error=None, processing_error=None, staging_error=None):
    def _sighting(entries):
        return publication.Sighting(
            tuple(publication.ReportFileIdentity(*entry) for entry in entries),
            None)

    return publication.ReportLocations(
        ready=_sighting(ready) if ready_error is None
        else publication.Sighting((), ready_error),
        processing=_sighting(processing) if processing_error is None
        else publication.Sighting((), processing_error),
        staging=_sighting(staging) if staging_error is None
        else publication.Sighting((), staging_error),
    )


def _report(report_id=5):
    return FrozenReport(report_id, None, 0, 30, 1)


def test_recover_keeps_report_found_in_ready():
    locations = _locations(ready=((5, _SHA),))
    assert publication.recover_report_files(
        _report(), locations) is publication.ReportFileDecision.PRESENT_ELSEWHERE


def test_recover_keeps_report_found_in_processing():
    locations = _locations(processing=((5, _SHA),))
    assert publication.recover_report_files(
        _report(), locations) is publication.ReportFileDecision.PRESENT_ELSEWHERE


def test_recover_prefers_delivered_location_over_staging_residue():
    locations = _locations(ready=((5, _SHA),), staging=((5, _SHA),))
    assert publication.recover_report_files(
        _report(), locations) is publication.ReportFileDecision.PRESENT_ELSEWHERE


def test_recover_reports_staging_residue_when_only_there():
    locations = _locations(staging=((5, _SHA),))
    assert publication.recover_report_files(
        _report(), locations) is publication.ReportFileDecision.STAGING_RESIDUE


def test_recover_reports_missing_when_reliably_absent():
    assert publication.recover_report_files(
        _report(), _locations()) is publication.ReportFileDecision.NOT_PRESENT


@pytest.mark.parametrize("kwargs", [
    {"ready_error": "listdir failed"},
    {"processing_error": "listdir failed"},
    {"staging_error": "listdir failed"},
])
def test_recover_rejects_unreliable_observation(kwargs):
    assert publication.recover_report_files(
        _report(), _locations(**kwargs)) is publication.ReportFileDecision.UNRELIABLE


def test_recover_matches_by_report_id_not_file_digest():
    # 恢复识别按文件位置与报告登记关联；字节依据保存在数据库。
    locations = _locations(ready=((5, _OTHER_SHA),))
    assert publication.recover_report_files(
        _report(), locations) is publication.ReportFileDecision.PRESENT_ELSEWHERE


def test_recover_ignores_unparseable_names():
    locations = _locations(ready=(), staging=())
    assert publication.recover_report_files(
        _report(), locations) is publication.ReportFileDecision.NOT_PRESENT


def test_recover_only_considers_own_report_identity():
    locations = _locations(ready=((6, _SHA),), staging=((7, _SHA),))
    assert publication.recover_report_files(
        _report(), locations) is publication.ReportFileDecision.NOT_PRESENT


# ---- 补投资格决策表 --------------------------------------------------


def _covering(report_id=5):
    return AckReport(report_id, 0, 30, 1)


def test_republish_not_needed_without_open_responsibility():
    assert publication.decide_republish(
        False, _covering(), _locations()) is publication.RepublishDecision.NOT_NEEDED


def test_republish_not_needed_even_with_unreliable_files():
    # 责任判断优先；无责任时不依赖目录观察。
    assert publication.decide_republish(
        False, _covering(), _locations(ready_error="x")
    ) is publication.RepublishDecision.NOT_NEEDED


def test_republish_creates_new_report_without_covering_candidate():
    assert publication.decide_republish(
        True, None, _locations()) is publication.RepublishDecision.CREATE_NEW


def test_republish_keeps_existing_report_in_ready():
    locations = _locations(ready=((5, _SHA),))
    assert publication.decide_republish(
        True, _covering(), locations) is publication.RepublishDecision.KEEP_EXISTING


def test_republish_keeps_existing_report_in_processing():
    locations = _locations(processing=((5, _SHA),))
    assert publication.decide_republish(
        True, _covering(), locations) is publication.RepublishDecision.KEEP_EXISTING


def test_republish_reuses_identity_when_reliably_absent():
    assert publication.decide_republish(
        True, _covering(), _locations()) is publication.RepublishDecision.REPUBLISH_EXISTING


def test_republish_does_not_count_staging_residue_as_delivered():
    locations = _locations(staging=((5, _SHA),))
    assert publication.decide_republish(
        True, _covering(), locations) is publication.RepublishDecision.REPUBLISH_EXISTING


def test_republish_unreliable_when_covering_exists_and_observation_fails():
    assert publication.decide_republish(
        True, _covering(), _locations(processing_error="x")
    ) is publication.RepublishDecision.UNRELIABLE


# ---- 发布编排 --------------------------------------------------------


class FakeSession:
    """记录调用并按方法返回可编程结果的状态库替身。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.responses: dict = {}

    def _respond(self, method, *args):
        self.calls.append((method, *args))
        handler = self.responses.get(method, DbOutcome(
            kind=DbOutcomeKind.COMPLETED, value=None))
        if callable(handler):
            return handler(*args)
        return handler

    def prepare(self, report_id, contents):
        return self._respond("prepare", report_id, contents)

    def record_intent(self, report_id):
        return self._respond("record_intent", report_id)

    def save_publication(self, report_id, file_result):
        return self._respond("save_publication", report_id, file_result)

    def record_failure(self, report_id, error):
        return self._respond("record_failure", report_id, error)

    def settle_local(self, action_id, report_id):
        return self._respond("settle_local", action_id, report_id)


class FileFake:
    """记录文件操作并返回可编程结果的文件替身。"""

    def __init__(self, monkeypatch) -> None:
        self.staged: list[tuple] = []
        self.stale: list[str] = []
        self.withdrawn: list[str] = []
        self.moved: list[tuple] = []
        self.stage_error: str | None = None
        self.stale_names: tuple = ()
        self.stale_error: str | None = None
        self.withdraw_result = WithdrawResult(WithdrawStage.WITHDRAWN, None)
        self.move_result = _moved()
        monkeypatch.setattr(publication, "_stage_payload", self._stage_payload)
        monkeypatch.setattr(publication, "_list_stale_ready", self._list_stale_ready)
        monkeypatch.setattr(publication, "_withdraw_stale", self._withdraw_stale)
        monkeypatch.setattr(publication, "_move_staged", self._move_staged)

    def _stage_payload(self, staging_root, name, payload):
        self.staged.append((Path(staging_root), name, bytes(payload)))
        return self.stage_error

    def _list_stale_ready(self, ready_dir, current):
        return tuple(self.stale_names), self.stale_error

    def _withdraw_stale(self, ready_dir, name):
        self.withdrawn.append(name)
        return self.withdraw_result

    async def _move_staged(self, staging_root, ready_dir, name):
        self.moved.append((Path(staging_root), Path(ready_dir), name))
        return self.move_result


def _deliver(session, files, *, report_id=5, payload=b'{"v":1}', local_actions=()):
    import hashlib
    return asyncio.run(publication.deliver_report(
        report_id, payload,
        HandoffDirectories(Path("s"), Path("r")),
        session, local_actions=tuple(local_actions)))


def _digest(payload: bytes) -> str:
    import hashlib
    return hashlib.sha256(payload).hexdigest()


def test_full_chain_publishes_with_evidence_order(tmp_path, monkeypatch):
    session, files = FakeSession(), FileFake(monkeypatch)
    payload = b'{"v":1}'
    result = _deliver(session, files, payload=payload)
    assert result.outcome is publication.DeliveryOutcome.PUBLISHED
    assert result.report.name == f"status-report-5-{_digest(payload)}.json"
    assert result.report.size_bytes == len(payload)
    assert result.report.sha256 == _digest(payload)
    assert [call[0] for call in session.calls] == [
        "prepare", "record_intent", "save_publication"]
    assert session.calls[0][2] == ReportBytes(len(payload), _digest(payload))
    assert session.calls[2][2] is files.move_result
    assert files.moved and files.moved[0][2] == result.report.name


def test_stale_ready_report_is_withdrawn_before_move(monkeypatch):
    session, files = FakeSession(), FileFake(monkeypatch)
    stale = f"status-report-9-{_OTHER_SHA}.json"
    files.stale_names = (stale,)
    result = _deliver(session, files)
    assert result.outcome is publication.DeliveryOutcome.PUBLISHED
    assert files.withdrawn == [stale]
    assert files.moved


def test_withdraw_failure_stops_publication(monkeypatch):
    session, files = FakeSession(), FileFake(monkeypatch)
    files.stale_names = (f"status-report-9-{_OTHER_SHA}.json",)
    files.withdraw_result = WithdrawResult(WithdrawStage.FAILED, "busy")
    result = _deliver(session, files)
    assert result.outcome is publication.DeliveryOutcome.WITHDRAW_FAILED
    assert not files.moved
    assert "save_publication" not in [c[0] for c in session.calls]


def test_withdraw_unknown_stops_publication(monkeypatch):
    session, files = FakeSession(), FileFake(monkeypatch)
    files.stale_names = (f"status-report-9-{_OTHER_SHA}.json",)
    files.withdraw_result = WithdrawResult(WithdrawStage.UNKNOWN, "interrupted")
    result = _deliver(session, files)
    assert result.outcome is publication.DeliveryOutcome.WITHDRAW_FAILED


def test_missing_stale_file_continues(monkeypatch):
    session, files = FakeSession(), FileFake(monkeypatch)
    files.stale_names = (f"status-report-9-{_OTHER_SHA}.json",)
    files.withdraw_result = WithdrawResult(WithdrawStage.NOT_PRESENT, None)
    result = _deliver(session, files)
    assert result.outcome is publication.DeliveryOutcome.PUBLISHED


def test_stale_observation_failure_stops_publication(monkeypatch):
    session, files = FakeSession(), FileFake(monkeypatch)
    files.stale_error = "listdir failed"
    result = _deliver(session, files)
    assert result.outcome is publication.DeliveryOutcome.WITHDRAW_FAILED
    assert not files.moved


def test_write_failure_skips_store(monkeypatch):
    session, files = FakeSession(), FileFake(monkeypatch)
    files.stage_error = "disk full"
    result = _deliver(session, files)
    assert result.outcome is publication.DeliveryOutcome.WRITE_FAILED
    assert session.calls == []


def test_prepare_rollback_stops_before_move(monkeypatch):
    session, files = FakeSession(), FileFake(monkeypatch)
    session.responses["prepare"] = DbOutcome(
        kind=DbOutcomeKind.ROLLED_BACK, error="guard")
    result = _deliver(session, files)
    assert result.outcome is publication.DeliveryOutcome.STORE_FAILED
    assert not files.moved


def test_intent_unknown_stops_before_move(monkeypatch):
    session, files = FakeSession(), FileFake(monkeypatch)
    session.responses["record_intent"] = DbOutcome(
        kind=DbOutcomeKind.UNKNOWN, error="unknown")
    result = _deliver(session, files)
    assert result.outcome is publication.DeliveryOutcome.UNKNOWN
    assert not files.moved


def test_move_not_moved_records_failure(monkeypatch):
    session, files = FakeSession(), FileFake(monkeypatch)
    files.move_result = PublishResult(
        PublishStage.NOT_MOVED, DirectorySyncStage.NOT_ATTEMPTED, False, "target_exists")
    result = _deliver(session, files)
    assert result.outcome is publication.DeliveryOutcome.HANDOFF_FAILED
    assert [c[0] for c in session.calls] == [
        "prepare", "record_intent", "record_failure"]
    assert "target_exists" in session.calls[2][2]["error"]


def test_move_unknown_keeps_result_uncertain(monkeypatch):
    session, files = FakeSession(), FileFake(monkeypatch)
    files.move_result = PublishResult(
        PublishStage.UNKNOWN, DirectorySyncStage.NOT_ATTEMPTED, False,
        "move_unknown: InterruptedError")
    result = _deliver(session, files)
    assert result.outcome is publication.DeliveryOutcome.UNKNOWN
    assert "record_failure" not in [c[0] for c in session.calls]
    assert "save_publication" not in [c[0] for c in session.calls]


def test_save_publication_rollback_reports_store_failure(monkeypatch):
    session, files = FakeSession(), FileFake(monkeypatch)
    session.responses["save_publication"] = DbOutcome(
        kind=DbOutcomeKind.ROLLED_BACK, error="guard")
    result = _deliver(session, files)
    assert result.outcome is publication.DeliveryOutcome.STORE_FAILED


def test_save_publication_unknown_reports_uncertain(monkeypatch):
    session, files = FakeSession(), FileFake(monkeypatch)
    session.responses["save_publication"] = DbOutcome(
        kind=DbOutcomeKind.UNKNOWN, error="unknown")
    result = _deliver(session, files)
    assert result.outcome is publication.DeliveryOutcome.UNKNOWN


def test_local_settlement_follows_publication(monkeypatch):
    session, files = FakeSession(), FileFake(monkeypatch)
    result = _deliver(session, files, local_actions=(7, 8))
    assert result.outcome is publication.DeliveryOutcome.PUBLISHED
    assert [c[:2] for c in session.calls if c[0] == "settle_local"] == [
        ("settle_local", 7), ("settle_local", 8)]


def test_local_settlement_failure_keeps_publication(monkeypatch):
    session, files = FakeSession(), FileFake(monkeypatch)
    session.responses["settle_local"] = DbOutcome(
        kind=DbOutcomeKind.ROLLED_BACK, error="conflict")
    result = _deliver(session, files, local_actions=(7,))
    assert result.outcome is publication.DeliveryOutcome.PUBLISHED
    assert result.error is not None


def test_no_local_settlement_without_actions(monkeypatch):
    session, files = FakeSession(), FileFake(monkeypatch)
    _deliver(session, files)
    assert "settle_local" not in [c[0] for c in session.calls]
