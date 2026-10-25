"""交付展示保留必要来源与目标证明，避免展开源文件经历。"""

from unittest.mock import create_autospec

import pytest

from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.values import ConsistencyError
from camctl.persistence.repositories.history import HistoryRepository
from camctl.reporting.generation import _merge_delivery


@pytest.mark.parametrize("source_column", ["device_file_id", "intermediate_file_id"])
def test_delivery_reads_source_identity_and_its_own_copy_target(source_column):
    boundary = HistoryBoundary(3, 9)
    delivery = {"id": 11, "output_id": 8}
    copy = {"id": 12, "target_file_id": 13, "source_size": 100, "target_sha256": "a" * 64}
    source = {"id": 8, "source_action_id": 7, source_column: 77}
    target = {"id": 13, "size_bytes": 100, "sha256": "a" * 64}
    repo = create_autospec(HistoryRepository, instance=True)

    def restore(entity, identity, actual_boundary):
        assert actual_boundary == boundary
        if (entity, identity) == ("delivery", 11):
            return {("deliveries", 11): delivery, ("file_copies", 12): copy}
        if (entity, identity) == ("intermediate_file", 13):
            return {("intermediate_files", 13): target}
        raise AssertionError("交付展示不应恢复源产物或源文件的完整对象")

    repo.restore_entity.side_effect = restore
    repo.restore_primary_at_h.return_value = source
    facts = {}
    _merge_delivery(repo, facts, 11, boundary)
    assert facts == {
        "deliveries": {11: delivery}, "file_copies": {12: copy},
        "outputs": {8: source}, "intermediate_files": {13: target},
    }
    repo.restore_primary_at_h.assert_called_once_with("output", 8, boundary)


def test_delivery_rejects_source_missing_at_report_boundary():
    boundary = HistoryBoundary(3, 9)
    repo = create_autospec(HistoryRepository, instance=True)
    repo.restore_entity.return_value = {("deliveries", 11): {"id": 11, "output_id": 8}}
    repo.restore_primary_at_h.return_value = None
    with pytest.raises(ConsistencyError):
        _merge_delivery(repo, {}, 11, boundary)
