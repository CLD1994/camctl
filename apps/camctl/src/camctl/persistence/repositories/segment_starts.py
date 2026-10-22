"""从原基准差集及全部已保存观察推导可信起点。"""

from camctl.capture.segment_models import SegmentStart, SegmentStartObservation
from camctl.contracts.enums import enum_for, load_registry
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import ConsistencyError
from camctl.devices.segment_profiles import SegmentFileKind
from .segment_profiles import profile_for_action
from camctl.history.events import load_event_registry

from .result_file_proofs import _file_at
from .result_file_ownership import _proof
from .result_pages import _at
from .segment_result_sets import _one, _read_transaction


def segment_start_from_set(connection, setrow):
    """逐文件读取原 H0，不用合格产物过滤起点，也不借旧扫描补当前扫描。"""
    from camctl.capture.files import OwnershipFromResultPageSave
    from camctl.persistence.transaction import row_facts

    unknown = SegmentStart()
    if setrow["current_attempt_id"] is None or setrow["run_id"] is None:
        return unknown
    activity = row_facts(connection, "device_activities", setrow["activity_id"])
    if activity["ownership_mode"] != int(
        enum_for("device_activities.ownership_mode").BASELINE_COMPARISON
    ) or activity["baseline_state"] != int(
        enum_for("device_activities.baseline_state").FIXED
    ):
        return unknown
    boundary = HistoryBoundary(setrow["source_txn_id"], setrow["source_event_id"])
    contract = load_event_registry()["events"]["BASELINE_CHUNK"]
    owner = load_registry()["history_objects"]["action"]["id"]
    fixed = _one(
        connection,
        "SELECT e.id FROM history_events e INDEXED BY history_events_type"
        " CROSS JOIN entity_event_links l ON l.event_id=e.id"
        " WHERE l.entity_type=? AND l.entity_id=? AND e.event_type=? AND e.id<=?"
        " AND json_extract(e.body_json,'$.evidence.activity_id')=?"
        " AND json_extract(e.body_json,'$.reason') IN (?,?) ORDER BY e.id DESC LIMIT 1",
        (
            owner,
            setrow["action_id"],
            contract["id"],
            boundary.last_event_id,
            activity["id"],
            contract["branches"]["FIX"]["reason"],
            contract["branches"]["FIX_EMPTY"]["reason"],
        ),
    )
    if fixed is None:
        raise ConsistencyError("原固定基准缺少实际 FIX 事件")
    # 使用原页索引遍历所有轮次的实际差集，文件后来缺失也保留最早位置。
    # 每次仅驻留一个文件及其首次观察页，不积累目录或排序候选。
    previous = 0
    earliest = None
    order_unknown = False
    while True:
        source = _one(
            connection,
            "SELECT file_id,MIN(page_event_id) FROM result_page_files"
            " WHERE run_id=? AND file_id>? AND created_event_id<=?"
            " GROUP BY file_id ORDER BY file_id LIMIT 1",
            (setrow["run_id"], previous, boundary.last_event_id),
        )
        if source is None:
            break
        file_id, page_id = source
        previous = file_id
        saved, _ = _at(connection, page_id)
        action_id, role, _ = _proof(
            connection, OwnershipFromResultPageSave(saved.ref, file_id, 0)
        )
        if action_id != setrow["action_id"] or page_id <= fixed[0]:
            raise ConsistencyError("起点观察没有沿用同一原基准和动作")
        file = _file_at(connection, file_id, boundary)
        identity = parse_exact_json(file["identity_key"])[2]
        parsed = profile_for_action(connection, setrow["action_id"]).classify_file(identity)
        if parsed.kind is SegmentFileKind.PHOTO:
            continue
        if file["source_action_id"] != action_id or file["role"] != role:
            order_unknown = True
        if parsed.key is None:
            order_unknown = True
            continue
        position = (parsed.key, page_id, file_id)
        if earliest is None or position < earliest:
            earliest = position
    if earliest is None or order_unknown:
        return unknown
    # 成员按设备键已建立稳定索引；冲突的实际原片不提供摄制顺序。
    originals = int(enum_for("segment_members.role").ORIGINAL)
    conflict = _one(
        connection,
        "SELECT 1 FROM segment_members WHERE set_id=? AND role=?"
        " GROUP BY directory_number,segment_index HAVING COUNT(*)>1 LIMIT 1",
        (setrow["id"], originals),
    )
    if conflict is not None:
        return unknown
    key, page_id, file_id = earliest
    return SegmentStart(key, SegmentStartObservation(fixed[0], page_id, file_id))


def read_segment_start(ref, owned):
    from .segment_result_sets import read_result_set

    with _read_transaction(owned) as connection:
        setrow = read_result_set(ref, owned)
        if setrow["state"] == int(enum_for("segment_sets.state").COLLECTING):
            raise ConsistencyError("可信起点只能从已固定的完整成员范围取得")
        return segment_start_from_set(connection, setrow)
