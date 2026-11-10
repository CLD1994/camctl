"""结果文件归属按原责任和冻结产品规则判断。"""

import pytest

from camctl.capture.result_inputs import ObservedFile
from camctl.capture.result_ownership import (
    decide_result_ownership,
    require_result_file_ownership, resolved_result_role, result_observation_role,
    validate_result_ownership_policy,
)
from camctl.capture.results import FileKind, ProductRule
from camctl.contracts.values import ConsistencyError
from camctl.contracts.enums import enum_for
from camctl.devices.tasks import CaptureResultPolicy
from camctl.capture.files import ObservationDisposition


def test_ownership_decision_preserves_first_same_and_conflicting_source():
    role = enum_for('device_files.role')
    unknown = {'source_action_id': None, 'ownership_evidence_json': None,
               'role': int(role.UNDETERMINED), 'original_device_file_id': None,
               'pairing_evidence_json': None}
    assert decide_result_ownership(unknown, 7, int(role.ORIGINAL)) is ObservationDisposition.SAVED
    saved = unknown | {'source_action_id': 7, 'ownership_evidence_json': {}, 'role': int(role.ORIGINAL)}
    assert decide_result_ownership(saved, 7, int(role.ORIGINAL)) is ObservationDisposition.ALREADY
    for conflicting in (saved | {'source_action_id': 8}, saved | {'role': int(role.PREVIEW)},
                        unknown | {'original_device_file_id': 4},
                        unknown | {'pairing_evidence_json': {}}):
        with pytest.raises(ConsistencyError):
            decide_result_ownership(conflicting, 7, int(role.ORIGINAL))


def check(**changes):
    facts = dict(action_id=7, source_action_id=None, ownership_evidence=None,
                 responsibility_key='results/9', target_id=9,
                 observed_file=ObservedFile('/raw.lrf', {}, {}, True, 10,
                                            kind=FileKind.VIDEO, format_id='lrf',
                                            paired_identity='/raw.osv'),
                 product_rules=(ProductRule(FileKind.VIDEO, format_id='osv'),))
    require_result_file_ownership(**(facts | changes))


def test_fixed_owned_file_needs_no_optional_preview_exception():
    check(source_action_id=7, ownership_evidence={}, responsibility_key='scope/9',
          product_rules=())


def test_explicit_optional_preview_may_remain_unowned():
    check()


@pytest.mark.parametrize('changes', [
    {'responsibility_key': 'scope/9'},
    {'source_action_id': 8},
    {'source_action_id': 7, 'ownership_evidence': None},
    {'observed_file': ObservedFile('/raw.lrf', {}, {}, True, 10,
                                   kind=FileKind.VIDEO, format_id='lrf')},
    {'product_rules': ()},
    {'product_rules': (ProductRule(FileKind.VIDEO, format_id='lrf'),)},
])
def test_required_or_foreign_file_without_owned_evidence_is_rejected(changes):
    with pytest.raises(ConsistencyError, match='未确认归属'):
        check(**changes)


@pytest.mark.parametrize(('scope', 'policy', 'identity', 'format_id', 'pair', 'expected'), [
    (False, CaptureResultPolicy.OSMO360II_RECORD_SEGMENTS_V1.value, '/DCIM/a.OSV', 'osv', None, 'ORIGINAL'),
    (False, CaptureResultPolicy.OSMO360II_RECORD_SEGMENTS_V1.value, '/DCIM/a.LRF', 'lrf', None, 'PREVIEW'),
    (False, CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value, '/DCIM/a.MP4', 'mp4', None, 'ORIGINAL'),
    (False, CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value, '/DCIM/a.LRF', 'lrf', None, 'PREVIEW'),
    (False, CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value, '/DCIM/a.JPG', 'jpeg', None, 'ORIGINAL'),
    (True, None, '/DCIM/a.other', None, None, 'UNDETERMINED'),
    (True, None, '/DCIM/a.other', None, '/DCIM/a.raw', 'PREVIEW'),
    (True, None, '/DCIM/a.other', 'jpeg', None, 'ORIGINAL'),
])
def test_observed_file_role_uses_frozen_policy_or_explicit_scope_fact(
        scope, policy, identity, format_id, pair, expected):
    observed = ObservedFile(identity, {}, {}, True, 10, format_id=format_id, paired_identity=pair)
    format_from_declaration = 'jpeg' if identity.lower().endswith('.jpg') else None
    actual = result_observation_role(scope_only=scope, policy=policy, observed_file=observed,
                                     action6_format_id=format_from_declaration)
    assert actual == int(enum_for('device_files.role')[expected])


@pytest.mark.parametrize(('policy', 'identity', 'format_id'), [
    (CaptureResultPolicy.OSMO360II_RECORD_SEGMENTS_V1.value, '/DCIM/a.OSV', 'lrf'),
    (CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value, '/DCIM/a.MP4', 'lrf'),
    (CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value, '/DCIM/a.jpeg', 'jpeg'),
])
def test_result_observation_rejects_mismatched_or_undeclared_format(policy, identity, format_id):
    observed = ObservedFile(identity, {}, {}, True, 10, format_id=format_id)
    with pytest.raises(ConsistencyError):
        result_observation_role(scope_only=False, policy=policy, observed_file=observed,
                                action6_format_id=None)


def test_policy_and_saved_role_are_checked_without_assuming_unknown_scope_is_owned():
    with pytest.raises(ConsistencyError):
        validate_result_ownership_policy(scope_only=False, policy=None)
    validate_result_ownership_policy(scope_only=True, policy=None)
    role = enum_for('device_files.role')
    file = {'source_action_id': 7, 'ownership_evidence_json': {}, 'role': int(role.PREVIEW)}
    assert resolved_result_role(scope_only=True, role=int(role.UNDETERMINED),
                                action_id=7, saved_file=file) == int(role.PREVIEW)
    assert resolved_result_role(scope_only=True, role=int(role.UNDETERMINED),
                                action_id=8, saved_file=file) == int(role.UNDETERMINED)
