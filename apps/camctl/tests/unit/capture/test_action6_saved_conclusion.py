"""已保存的 Action6 保留决定直接形成完成申请。"""
import sqlite3
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.capture import action6_completion as completion, segment_completion
from camctl.capture.segment_storage import SegmentResultRef
from camctl.contracts.enums import enum_for
from camctl.persistence.repositories import segment_check_conclusions, segment_current


@pytest.mark.parametrize("failure",[None,{"code":"capture_failed","details":{"activity_id":"3","reason":"device_failed"}}])
def test_retained_finish_consumes_saved_failure_without_replaying_check(monkeypatch,failure):
    db=create_autospec(sqlite3.Connection,instance=True,spec_set=True)
    owned=SimpleNamespace(connection=db)
    runtime=SimpleNamespace(owned=owned,pending_segment_writes={},capture=object(),wall_us=lambda:5)
    ref=SegmentResultRef(1,2,1)
    repository=create_autospec(completion.SegmentRepository,instance=True,spec_set=True)
    repository.read_result_set.return_value={"state":int(enum_for("segment_sets.state").ASSESSED),"activity_id":3,"run_id":4}
    monkeypatch.setattr(completion,"SegmentRepository",create_autospec(completion.SegmentRepository,return_value=repository))
    for name in ("_prepare_members","resume_segment_writes"):
        monkeypatch.setattr(segment_completion,name,create_autospec(getattr(segment_completion,name)))
    saved=create_autospec(segment_check_conclusions.read_fixed_result_check_conclusion,return_value={"current_failure_json":failure})
    monkeypatch.setattr(segment_check_conclusions,"read_fixed_result_check_conclusion",saved)
    replay=create_autospec(segment_check_conclusions.read_verified_conclusion,side_effect=AssertionError("重演旧决定"))
    monkeypatch.setattr(segment_check_conclusions,"read_verified_conclusion",replay)
    classify=create_autospec(segment_current.current_decision,side_effect=AssertionError("重算旧决定"))
    monkeypatch.setattr(segment_current,"current_decision",classify)
    write=create_autospec(segment_completion._save)
    monkeypatch.setattr(segment_completion,"_save",write)
    completion.finish_action6_retained_unconfirmed(runtime,{"id":1},ref)
    request=write.call_args.args[-1]
    assert request.ref==ref
    assert request.failure.code==("capture_result_unconfirmed" if failure is None else failure["code"])
    saved.assert_called_once_with(db,repository.read_result_set.return_value)
    replay.assert_not_called()
    classify.assert_not_called()
    db.execute.assert_not_called()
