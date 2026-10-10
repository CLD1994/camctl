"""基准拥有者只交接与原申请一致的可靠回执。"""
from unittest.mock import Mock

import pytest

from camctl.capture.baseline_models import (
    BaselineChunkResult, BaselineChunkSave, BaselineFixSave, BaselineRef,
    BaselineState,
)
from camctl.capture.baseline_saves import BaselineSaveOwner
from camctl.contracts.values import ConsistencyError
from camctl.devices.bindings import DeviceBinding
from camctl.devices.file_identity import FileIdentity
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository


@pytest.mark.parametrize("response", [
    BaselineChunkResult(1, 2, 8),
    BaselineChunkResult(1, 1, 0),
    BaselineChunkResult(2, 1, 8),
])
def test_append_response_must_match_original_chunk(response):
    request = BaselineChunkSave(1, 1, (
        FileIdentity(DeviceBinding("cam-1", "dji-action6"), "/DCIM/a.mp4"),
    ), 1_700_000_000_000_000)
    owner = BaselineSaveOwner()
    pending = owner.begin(request)
    repository = Mock(spec=CaptureRepository)
    repository.append_baseline.return_value = DbOutcome(DbOutcomeKind.COMPLETED, response)
    with pytest.raises(ConsistencyError):
        owner.save(repository, object())
    assert owner.pending == pending


@pytest.mark.parametrize("response", [
    BaselineRef(1, BaselineState.COLLECTING, None, None, 0, 0),
    BaselineRef(1, BaselineState.FIXED, 8, 8, 1, 1),
    BaselineRef(2, BaselineState.FIXED, None, None, 0, 0),
])
def test_fix_response_must_match_original_fixed_counts(response):
    owner = BaselineSaveOwner()
    pending = owner.begin(BaselineFixSave(1, 0, 0, 1_700_000_000_000_000))
    repository = Mock(spec=CaptureRepository)
    repository.fix_baseline.return_value = DbOutcome(DbOutcomeKind.COMPLETED, response)
    with pytest.raises(ConsistencyError):
        owner.save(repository, object())
    assert owner.pending == pending
