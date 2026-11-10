"""结果来源类型的业务身份、原持久化路径与字段保持一致。"""

from dataclasses import FrozenInstanceError
import pickle

import pytest

from camctl.capture.result_source_types import ResultSource, ResultSourceKind
from camctl.persistence.repositories import result_sources


def test_result_source_types_have_one_identity_and_original_values():
    assert result_sources.ResultSource is ResultSource
    assert result_sources.ResultSourceKind is ResultSourceKind
    assert {member.name: member.value for member in ResultSourceKind} == {
        'NATURAL': 'natural', 'STOP': 'stop', 'DIRECT': 'direct',
        'UNCONFIRMED': 'unconfirmed', 'UNANCHORED': 'unanchored',
    }
    source = ResultSource(ResultSourceKind.DIRECT, 5)
    assert (source.context, source.natural_lower_bound, source.requires_file_wait) == (None, None, False)
    with pytest.raises(FrozenInstanceError):
        source.intent_event_id = 6


def test_original_pickles_still_resolve_to_the_same_types():
    old_kind = (b'\x80\x04\x95U\x00\x00\x00\x00\x00\x00\x00\x8c.camctl.persistence.repositories.result_sources\x94'
                b'\x8c\x10ResultSourceKind\x94\x93\x94\x8c\x07natural\x94\x85\x94R\x94.')
    old_source = (b'\x80\x04\x95\xd5\x00\x00\x00\x00\x00\x00\x00\x8c.camctl.persistence.repositories.result_sources\x94'
                  b'\x8c\x0cResultSource\x94\x93\x94)\x81\x94}\x94(\x8c\x04kind\x94h\x00\x8c\x10ResultSourceKind\x94'
                  b'\x93\x94\x8c\x04stop\x94\x85\x94R\x94\x8c\x0fintent_event_id\x94K\x07\x8c\x07context\x94}\x94'
                  b'\x8c\x0bactivity_id\x94\x8c\x019\x94s\x8c\x13natural_lower_bound\x94K\x0c\x8c\x12requires_file_wait\x94\x88ub.')
    source = ResultSource(ResultSourceKind.STOP, 7, {'activity_id': '9'}, 12, True)
    assert pickle.loads(old_kind) is ResultSourceKind.NATURAL
    assert pickle.loads(old_source) == source
    assert pickle.loads(pickle.dumps(source)) == source
    assert pickle.dumps(source, protocol=4) == old_source
