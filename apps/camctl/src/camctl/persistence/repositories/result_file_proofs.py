"""按固定历史边界读取原页文件证明，不搜索其他结果补齐完成依据。"""
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace

from camctl.capture.files import OwnershipFromResultPageSave
from camctl.capture.result_pages import ResultPageRef
from camctl.capture.segment_models import (
    FileCompletionState, OwnershipState, PreviewPairingState, SegmentMemberRole,
)
from camctl.capture.segment_storage import SegmentMemberInput
from camctl.contracts.enums import enum_for, load_registry
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import ConsistencyError, ObjectId
from camctl.persistence.transaction import read_transaction_range, row_facts

from . import capture, result_pages
from .result_file_ownership import _proof
from .segments import _fixed_file


def _one(connection, sql, parameters):
    with closing(connection.execute(sql, parameters)) as cursor:
        return cursor.fetchone()


def _file_at(connection, file_id, boundary):
    transaction = read_transaction_range(connection, boundary.txn_id)
    if transaction.last_event_id != boundary.last_event_id:
        raise ConsistencyError("严格文件读取必须使用完整历史边界")
    owner = load_registry()["history_objects"]["device_file"]["id"]
    event = _one(connection,
        "SELECT MAX(event_id) FROM entity_event_links"
        " WHERE entity_type=? AND entity_id=? AND event_id<=?",
        (owner, file_id, boundary.last_event_id))
    if event is None or event[0] is None:
        raise ConsistencyError("固定边界没有该文件的实际自身事实")
    return _fixed_file(connection, file_id, event[0])


def _completion_page(connection, page, file, boundary):
    """缺证明保持未知；显式引用损坏或矛盾时抛出错误。"""
    evidence = file["completion_evidence_json"]
    if (file["completion_state"] != int(enum_for("device_files.completion_state").COMPLETE)
            or evidence is None or "result_page_event_id" not in evidence):
        return None
    page_id = ObjectId(evidence["result_page_event_id"])
    if page_id > boundary.last_event_id:
        raise ConsistencyError("文件完成引用越过原固定边界")
    original = result_pages._first_complete_event(connection, file)
    if original.event_id > file["last_event_id"]:
        raise ConsistencyError("文件首次完成越过固定文件自身事实")
    completed, _ = result_pages._at(connection, page_id)
    if (completed.ref.ticket.run_id != page.ticket.run_id
            or completed.ref.ticket.target_id != page.ticket.target_id):
        raise ConsistencyError("文件完成原页不属于同一核实责任和活动")
    state = {"device_files": {file["id"]: file}}
    reader = SimpleNamespace(connection=connection)
    basis = enum_for("device_files.completion_evidence_json.basis")
    try:
        if evidence["basis"] == int(basis.STOP_RETURN_AND_WAIT):
            capture._load_stop_wait_file_sources(reader, file, evidence, state)
            capture._verify_stop_wait_file(file, evidence, file["size_bytes"], state,
                                          event_id=original.event_id)
        elif evidence["basis"] == int(basis.TIME_AND_OUTPUTS):
            capture._load_natural_wait_file_sources(reader, file, evidence, state)
            capture._verify_natural_wait_file(file, evidence, file["size_bytes"], state,
                event_id=original.event_id, occurred_at=original.occurred_at)
        else:
            raise ConsistencyError("声明原页的完成证明没有适用的停止或自然等待依据")
    except (KeyError, TypeError, ValueError) as error:
        raise ConsistencyError(f"原页完成证明无效: {error}") from error
    return int(page_id)


def load_completion_input(connection, page, file_id, boundary, *, current_boundary=None):
    """返回一个固定文件的引用及资格；不返回增长目录或候选数组。"""
    if not isinstance(page, ResultPageRef) or not isinstance(boundary, HistoryBoundary):
        raise TypeError("严格文件读取要求原页引用和完整历史边界")
    ObjectId(file_id)
    if page.event_id > boundary.last_event_id:
        raise ConsistencyError("原页位于固定文件边界之后")
    source, role, _ = _proof(connection, OwnershipFromResultPageSave(page, file_id, 0),
                              current_boundary=current_boundary)
    file = _file_at(connection, file_id, boundary)
    owned = (file["source_action_id"] == source and file["role"] == role
             and file["ownership_evidence_json"] is not None)
    original_role = role == int(enum_for("device_files.role").ORIGINAL)
    member_role = SegmentMemberRole.ORIGINAL if original_role else SegmentMemberRole.PREVIEW
    if original_role:
        from camctl.devices.segment_profiles import SegmentFileKind, profile_for_policy
        spec = row_facts(connection, "actions", source)["execution_spec_json"]
        if spec.get("result_policy") is not None:
            identity = parse_exact_json(file["identity_key"])[2]
            if profile_for_policy(spec["result_policy"]).classify_file(identity).kind is SegmentFileKind.PHOTO:
                member_role = SegmentMemberRole.OTHER
    completed_page = _completion_page(connection, page, file, boundary)
    return SegmentMemberInput(
        file_id, page.event_id if completed_page is None else completed_page,
        file["last_event_id"],
        member_role,
        OwnershipState.OWNED if owned else OwnershipState.UNKNOWN,
        FileCompletionState.UNKNOWN if completed_page is None else FileCompletionState.COMPLETE,
        pairing_state=(PreviewPairingState.NOT_APPLICABLE if original_role
                       else PreviewPairingState.UNKNOWN))


def load_strict_result_file_input(connection, page, file_id, boundary, *, pairing_fact_id=None,
                                  current_boundary=None):
    result = load_completion_input(connection, page, file_id, boundary,
                                   current_boundary=current_boundary)
    file = _file_at(connection, file_id, boundary)
    if result.role is not SegmentMemberRole.PREVIEW:
        if pairing_fact_id is not None:
            raise ConsistencyError('原片不能引用预览配对事实')
        return result
    evidence = file['pairing_evidence_json']
    method = int(enum_for('device_files.pairing_evidence_json.method').RESULT_SCAN_PAIRING)
    if pairing_fact_id is None and isinstance(evidence, dict) and evidence.get('method') == method:
        if set(evidence) != {'method', 'pairing_fact_id'}:
            raise ConsistencyError('固定文件的首次扫描配对证据不完整')
        pairing_fact_id = evidence['pairing_fact_id']
    if pairing_fact_id is None:
        return result
    ObjectId(pairing_fact_id)
    from .result_file_pairing import verify_pairing_fact
    fact = verify_pairing_fact(connection, pairing_fact_id, file_id, boundary,
                               current_boundary=current_boundary)
    paired = fact['state'] == int(enum_for('device_file_pairing_facts.state').PAIRED)
    identity = None
    if paired:
        if (file['original_device_file_id'] != fact['original_file_id']
                or not isinstance(evidence, dict) or set(evidence) != {'method', 'pairing_fact_id'}
                or evidence['method'] != method):
            raise ConsistencyError('固定文件缺少相同原片的首次扫描配对关联')
        first = (fact if evidence['pairing_fact_id'] == pairing_fact_id else
                 verify_pairing_fact(connection, evidence['pairing_fact_id'], file_id, boundary,
                                     current_boundary=current_boundary))
        if (first['state'] != int(enum_for('device_file_pairing_facts.state').PAIRED)
                or first['original_file_id'] != fact['original_file_id']
                or first['created_event_id'] > fact['created_event_id']):
            raise ConsistencyError('配对事实与文件首次正向关联不一致')
        original = _file_at(connection, fact['original_file_id'], boundary)
        identity = parse_exact_json(original['identity_key'])[2]
    return replace(result, pairing_fact_id=pairing_fact_id, pairing_identity=identity,
        pairing_state=PreviewPairingState.PAIRED if paired else PreviewPairingState.UNPAIRED)


def read_strict_result_file_input(page, file_id, boundary, owned, *, pairing_fact_id=None):
    connection = owned.connection
    opened = not connection.in_transaction
    if opened:
        connection.execute("BEGIN")
    try:
        return load_strict_result_file_input(connection, page, file_id, boundary,
                                             pairing_fact_id=pairing_fact_id)
    finally:
        if opened:
            connection.execute("ROLLBACK")
