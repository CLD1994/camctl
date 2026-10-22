"""动作所属分段关系的有限事务与稳定分页；不执行终态发布用例。"""
from contextlib import closing
from itertools import zip_longest

from camctl.capture.segment_models import *
from camctl.capture.segment_storage import (
    SegmentSetCreate,
    SegmentMemberInput,
    SegmentMemberCursor,
    SegmentGapCursor,
    SegmentScopeCursor,
    StoredSegmentMember,
    SegmentOutputCandidate,
    document,
    encode_enum,
    decode_enum,
    assessment_document,
    assessment_from_document,
    facts_from_document,
    SegmentResultRef,
)
from camctl.capture.segments import advance_segment_assessment, finish_segment_assessment, iter_segment_gaps
from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import json_equal, parse_exact_json
from camctl.contracts.pages import Page
from camctl.contracts.history_values import HistoryBoundary
from camctl.persistence.row_history import read_row_values_at_boundary
from camctl.contracts.values import ConsistencyError, ObjectId, UtcMicros, MAX_OBJECT_ID
from camctl.devices.segment_profiles import SegmentFileKind
from .segment_profiles import profile_for_action
from camctl.devices.segments import SegmentKey
from camctl.devices.tasks import CaptureResultPolicy
from camctl.outputs.catalog import OutputKind
from camctl.history.events import load_event_registry, business_columns
from camctl.history.reads import SegmentReadScope
from camctl.history.validators import register_guard, EventValidationError
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.transaction import (
    CommandPlan, commit_operation, event_envelope, next_row_id, row_change, row_facts,
    saved_transaction_events, update_change, TransactionError, read_transaction_range,
)
from .result_pages import _at, read_pages

_EVENT = load_event_registry()['events']['SEGMENT_FACTS']
_STATE = enum_for('segment_sets.state')
_FAMILY_ROLE = enum_for('result_page_files.family_role')
_ORDER = '(COALESCE(directory_number,0),COALESCE(segment_index,0),identity,id)'


def _one(db, sql, params=()):
    with closing(db.execute(sql,params)) as reader:
        return reader.fetchone()


def _batch(values):
    if not isinstance(values,tuple) or not 1 <= len(values) <= 128:
        raise ValueError('分段写入批次必须是 1—128 项元组')
    return values


def _ref(row):
    if row["current_attempt_id"] is None:
        return SegmentResultRef(row["action_id"], row["id"], row["version"])
    return SegmentSetRef(row['action_id'],row['current_attempt_id'],row['current_final_page_event_id'],row['version'])


def _set(db,ref, *, fixed=False):
    if isinstance(ref, SegmentResultRef):
        from .segment_result_sets import read_result_set
        from types import SimpleNamespace

        row = read_result_set(ref, SimpleNamespace(connection=db))
        if fixed and row["state"] == _STATE.COLLECTING:
            raise ConsistencyError("原结果成员尚未可靠固定")
        return row
    if not isinstance(ref,SegmentSetRef):
        raise TypeError('必须使用冻结分段集合引用')
    found = _one(db,'SELECT id FROM segment_sets WHERE action_id=? AND version=?',(ref.action_id,ref.version))
    if found is None:
        raise ConsistencyError('原分段集合不存在')
    row = row_facts(db,'segment_sets',found[0])
    if _ref(row) != ref or fixed and row['state'] == _STATE.COLLECTING:
        raise ConsistencyError('原集合版本、扫描、末页或固定状态不同')
    return row


def _check_write_kind(db,setrow,kind):
    from camctl.capture.segment_storage import SegmentSourceKind,PublicationKind
    if (setrow['source_kind']!=encode_enum(SegmentSourceKind.COMPLETE_SCAN)
            or setrow['publication_kind']!=encode_enum(PublicationKind.RESULT)):
        action=row_facts(db,'actions',setrow['action_id'])
        from camctl.devices.tasks import CaptureResultPolicy
        action6=(action is not None and action['execution_spec_json'].get('result_policy')
            == CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value)
        allowed=(action6 and kind in ('MEMBERS','FIX_MEMBERS','ASSESS','SCOPE','ACTIVATE')
            and setrow['source_kind'] in tuple(encode_enum(value) for value in (
                SegmentSourceKind.COMPLETE_SCAN, SegmentSourceKind.RETAINED_SOURCES,
                SegmentSourceKind.NO_RESULTS))
            and setrow['publication_kind'] in tuple(encode_enum(value) for value in (
                PublicationKind.RESULT, PublicationKind.CANCELED)))
        if not allowed:
            raise ConsistencyError('普通完整扫描写入不适用于该结果来源或发布处置')
    if kind in ('MEMBERS','FIX_MEMBERS'):
        reason=_one(db,"SELECT json_extract(body_json,'$.reason') FROM history_events WHERE id=?",
            (setrow['created_event_id'],))[0]
        if reason!=_EVENT['branches']['CREATE']['reason']:
            raise ConsistencyError('统一结果成员必须按原H0保存和双向固定，不能使用原完整扫描成员写入')


def _source(db,ref):
    saved,raw = _at(db,ref.final_page_event_id)
    attempt = row_facts(db,'operation_attempts',ref.scan_id)
    if attempt is None:
        raise ConsistencyError('分段扫描必须引用实际尝试身份')
    run = row_facts(db,'operation_runs',attempt['run_id'])
    action = None if run is None else row_facts(db,'actions',run['action_id'])
    activity = None if run is None else row_facts(db,'device_activities',run['activity_id'])
    if (action is None or activity is None or action['id'] != ref.action_id
        or activity['action_id'] != ref.action_id or raw['activity_id'] != activity['id']
        or raw['run_id'] != run['id'] or raw['attempt_no'] != attempt['attempt_no']
        or attempt['result_last_page_event_id'] != ref.final_page_event_id
        or saved.page.next_cursor is not None or saved.page.outcome.error is not None
        or not saved.page.scan_complete or not saved.page.set_finalized):
        raise ConsistencyError('分段集合缺少同一原活动和可靠完整末页')
    spec=action['execution_spec_json']
    if spec.get('result_policy') not in {policy.value for policy in CaptureResultPolicy}:
        raise ConsistencyError('原动作没有声明 OSMO 分段结果策略')
    return action,activity,run,attempt,saved


def _member_by_identity(db,set_id,identity):
    found=_one(db,'SELECT id FROM segment_members WHERE set_id=? AND identity=?',(set_id,identity))
    if found is None:
        raise ConsistencyError('分段引用不是原集合的真实成员')
    return row_facts(db,'segment_members',found[0])


def _position(row):
    return (row['directory_number'] or 0,row['segment_index'] or 0,row['identity'],row['id'])


def _read_member(db, row, *, page_cache=None, current_boundary=None):
    page_id = row['page_event_id']
    saved_page = None if page_cache is None else page_cache.get(page_id)
    if saved_page is None:
        saved_page = _at(db, page_id)
        if page_cache is not None:
            page_cache[page_id] = saved_page
    saved, _ = saved_page
    entry = next((entry for entry in saved.page.entries if entry.identity==row['identity']),None)
    if entry is None or (row['identity'],row['file_id']) not in saved.file_ids:
        raise ConsistencyError('成员缺少原实际页和真实文件引用')
    file=_fixed_file(db,row['file_id'],row['file_event_id'])
    _verify_strict_member(db, _member_input_from_row(row), file, saved,
                          current_boundary=current_boundary)
    member=SegmentMember(row['identity'],None if row['directory_number'] is None else
        SegmentKey(row['directory_number'],row['segment_index']),
        decode_enum(SegmentMemberRole,row['role']),decode_enum(OwnershipState,row['ownership']),
        decode_enum(FileCompletionState,row['completion']),file['size_bytes'],
        row['pairing_identity'],decode_enum(PreviewPairingState,row['pairing_state']))
    return StoredSegmentMember(row['id'],row['file_id'],row['page_event_id'],row['file_event_id'],member,
                               row['pairing_fact_id'])


def _eligible_original(member):
    return (member.role is SegmentMemberRole.ORIGINAL
        and member.ownership is OwnershipState.OWNED
        and member.completion is FileCompletionState.COMPLETE
        and member.size_bytes is not None and member.size_bytes>0)


def _output_candidate(db,setrow,stored):
    """统一两方向资格，只使用冻结成员及其固定file_event事实。"""
    member=stored.member
    if (member.role is SegmentMemberRole.OTHER
            and profile_for_action(db,setrow['action_id']).classify_file(member.identity).kind is SegmentFileKind.PHOTO
            and member.ownership is OwnershipState.OWNED
            and member.completion is FileCompletionState.COMPLETE
            and member.size_bytes is not None and member.size_bytes > 0):
        return SegmentOutputCandidate(stored.member_id,stored.file_id,OutputKind.ORIGINAL)
    if _eligible_original(member):
        return SegmentOutputCandidate(stored.member_id,stored.file_id,OutputKind.ORIGINAL)
    if member.role is not SegmentMemberRole.PREVIEW or member.pairing_state is not PreviewPairingState.PAIRED:
        return None
    paired=_read_member(db,_member_by_identity(db,setrow['id'],member.pairing_identity))
    if stored.pairing_fact_id is not None:
        from .result_pages import _family_fields
        action = row_facts(db, 'actions', setrow['action_id'])
        result_policy = action['execution_spec_json'].get('result_policy')
        left, right = (
            _family_fields(member.identity, result_policy=result_policy),
            _family_fields(paired.member.identity, result_policy=result_policy),
        )
        same_family = (left[0] is not None and left[:2] == right[:2]
                       and left[2] == int(_FAMILY_ROLE.PREVIEW)
                       and right[2] == int(_FAMILY_ROLE.ORIGINAL))
        fact = row_facts(db, 'device_file_pairing_facts', stored.pairing_fact_id)
        same_family = (same_family and fact is not None
                       and fact['original_file_id'] == paired.file_id)
    else:
        profile=profile_for_action(db,setrow['action_id'])
        left=profile.classify_file(member.identity)
        right=profile.classify_file(paired.member.identity)
        same_family = left.key is not None and right.key is not None and left.pairing_identity == right.pairing_identity
    if paired.member.role is not SegmentMemberRole.ORIGINAL or not same_family:
        raise ConsistencyError('预览没有同集合、同完整名称主干的真实原片')
    if (member.ownership is not OwnershipState.OWNED
            or member.completion is not FileCompletionState.COMPLETE
            or member.size_bytes is None or member.size_bytes<=0
            or not _eligible_original(paired.member)):
        return None
    return SegmentOutputCandidate(stored.member_id,stored.file_id,OutputKind.PREVIEW,paired.member_id,paired.file_id)


def segment_output_candidate(ref,member_id,owned):
    """P3单成员候选计数/事件规划端口；不以当前文件状态替代冻结事实。"""
    ObjectId(member_id)
    db = owned.connection
    opened = not db.in_transaction
    if opened: db.execute('BEGIN')
    try:
        row=_set(db,ref,fixed=True)
        member=row_facts(db,'segment_members',member_id)
        if member is None or member['set_id']!=row['id']:
            raise ConsistencyError('产物候选成员不属于原冻结集合')
        return _output_candidate(db,row,_read_member(db,member))
    finally:
        if opened: db.execute('ROLLBACK')


def read_segment_output_candidates(ref,cursor,batch,owned):
    """按原始成员技术游标续读；空候选页仍可能有继续位置。"""
    db = owned.connection
    opened = not db.in_transaction
    if opened: db.execute('BEGIN')
    try:
        page=read_segment_members(ref,cursor,batch,owned)
        row=_set(db,ref,fixed=True)
        candidates=tuple(candidate for stored in page.items
            if (candidate:=_output_candidate(db,row,stored)) is not None)
        return Page(candidates,page.next_cursor)
    finally:
        if opened: db.execute('ROLLBACK')


def read_segment_members(ref,cursor,batch,owned):
    """固定集合的完整顺序页；返回独立有限值，读错误直接传播。"""
    if type(batch) is not int or not 1<=batch<=128:
        raise ValueError('分段读取批次必须是 1—128')
    db = owned.connection
    opened = not db.in_transaction
    if opened:
        db.execute('BEGIN')
    try:
        return _read_segment_members(ref, cursor, batch, db)
    finally:
        if opened:
            db.execute('ROLLBACK')


def _read_segment_members(ref, cursor, batch, db):
    row = _set(db, ref, fixed=True)
    after=(0,0,'',0)
    if cursor is not None:
        if not isinstance(cursor,SegmentMemberCursor) or cursor.ref!=ref:
            raise ConsistencyError('分段游标改变集合或读取种类')
        after=cursor.position
        found=row_facts(db,'segment_members',after[-1])
        if found is None or found['set_id']!=row['id'] or _position(found)!=after:
            raise ConsistencyError('分段游标不是原集合完整排序位置')
    with closing(db.execute(f'SELECT id FROM segment_members WHERE set_id=? AND {_ORDER}>(?,?,?,?) ORDER BY {_ORDER[1:-1]} LIMIT ?',
        (row['id'],*after,batch))) as reader:
        ids=reader.fetchmany(batch)
    # 缓存只服务本次快照中的一批成员，数量不超过 batch；不跨读取复用。
    page_cache = {}
    records=tuple(_read_member(db,row_facts(db,'segment_members',value[0]),
                              page_cache=page_cache) for value in ids[:batch])
    position=None if not ids else _position(row_facts(db,'segment_members',ids[-1][0]))
    more=position is not None and len(ids)==batch and _one(db,f'SELECT 1 FROM segment_members WHERE set_id=? AND {_ORDER}>(?,?,?,?) LIMIT 1',(row['id'],*position)) is not None
    last=SegmentMemberCursor(ref,position) if more else None
    return Page(records,last)


def iter_segment_members(ref,owned):
    cursor=None
    while True:
        page=read_segment_members(ref,cursor,128,owned)
        for stored in page.items:
            yield stored.member
        if page.next_cursor is None:
            return
        cursor=page.next_cursor


def _gap(db,row):
    def identity(field):
        return None if row[field] is None else row_facts(db,'segment_members',row[field])['identity']
    neighbors=row['neighbors_json']
    if set(neighbors)!={'left','right'}:
        raise ConsistencyError('缺口邻接分类不完整')
    return SegmentGap(SegmentKey(row['start_directory'],row['start_index']),SegmentKey(row['end_directory'],row['end_index']),
        decode_enum(SegmentGapKind,row['kind']),identity('left_member_id'),identity('right_member_id'),
        decode_enum(SegmentGapNeighborState,neighbors['left']),decode_enum(SegmentGapNeighborState,neighbors['right']),identity('empty_member_id'))


def _relation_page(ref,cursor,batch,owned,table,cursor_type,order):
    db=owned.connection;row=_set(db,ref,fixed=True)
    if type(batch) is not int or not 1<=batch<=128:
        raise ValueError('关系读取批次必须是 1—128')
    after=0
    if cursor is not None:
        if not isinstance(cursor,cursor_type) or cursor.ref!=ref:
            raise ConsistencyError('关系游标改变冻结集合或种类')
        after,=cursor.position
        if _one(db,f'SELECT 1 FROM {table} WHERE set_id=? AND {order}=?',(row['id'],after)) is None:
            raise ConsistencyError('关系游标不属于原集合')
    with closing(db.execute(f'SELECT id FROM {table} WHERE set_id=? AND {order}>? ORDER BY {order} LIMIT ?',
        (row['id'],after,batch))) as reader:
        ids=reader.fetchmany(batch)
    rows=tuple(row_facts(db,table,item[0]) for item in ids[:batch])
    more=bool(rows) and len(rows)==batch and _one(db,f'SELECT 1 FROM {table} WHERE set_id=? AND {order}>? LIMIT 1',(row['id'],rows[-1][order])) is not None
    following=cursor_type(ref,(rows[-1][order],)) if more else None
    return Page(rows,following)


def read_segment_gaps(ref,cursor,batch,owned):
    page=_relation_page(ref,cursor,batch,owned,'segment_gaps',SegmentGapCursor,'gap_no')
    return Page(tuple(_gap(owned.connection,row) for row in page.items),page.next_cursor)


def read_segment_scope(ref,cursor,batch,owned,*,published=True):
    page=_relation_page(ref,cursor,batch,owned,'segment_scope_members',SegmentScopeCursor,'ordinal')
    if published:
        setrow=_set(owned.connection,ref,fixed=True)
        action=row_facts(owned.connection,'actions',ref.action_id)
        if setrow['published_event_id'] is None or action['status'] not in (3,4,5,6) or any(row['output_id'] is None for row in page.items):
            raise ConsistencyError('默认范围尚未由来源终态共同发布')
        if setrow['state']!=_STATE.ASSESSED:raise ConsistencyError('默认范围判断尚未保存')
        assessment=assessment_from_document(setrow['assessment_json'])
        count=_one(owned.connection,'SELECT COUNT(*),COUNT(output_id) FROM segment_scope_members WHERE set_id=?',(setrow['id'],))
        expected=assessment.default_scope.member_count
        expected=0 if assessment.default_scope.state is DefaultScopeState.UNKNOWN else expected
        if count!=(expected,expected):
            raise ConsistencyError('默认范围发布数量与原判断矛盾')
    return page


def _stored_gaps(ref,owned):
    cursor=None
    while True:
        page=read_segment_gaps(ref,cursor,128,owned)
        yield from page.items
        if page.next_cursor is None:return
        cursor=page.next_cursor


def _verify_assessment(db,setrow,facts,assessment):
    _check_write_kind(db,setrow,'ASSESS')
    from camctl.capture.segment_models import SegmentDecision
    if isinstance(assessment, SegmentDecision):
        _verify_action6_decision(db, setrow, assessment)
        return
    if setrow["source_kind"] != int(enum_for("segment_sets.source_kind").COMPLETE_SCAN):
        raise ConsistencyError("普通完整扫描判断不能替代跨轮保留来源")
    action,*_=_source(db,_ref(setrow))
    if action['execution_spec_json'].get('result_policy')!=CaptureResultPolicy.OSMO360II_RECORD_SEGMENTS_V1.value:
        raise ConsistencyError('完整分段判断仅适用于普通录像，不用于原生延时采集目标')
    class Reader:
        connection=db
    ref=_ref(setrow)
    if assessment.set_ref!=ref or assessment.target_duration_ms!=setrow['target_duration_ms']:
        raise ConsistencyError('判断改变原冻结集合或原目标')
    created_reason = _one(
        db,
        "SELECT json_extract(body_json,'$.reason') FROM history_events WHERE id=?",
        (setrow["created_event_id"],),
    )[0]
    if (
        facts.start_observation is not None
        or created_reason == _EVENT["branches"]["CREATE_RESULT"]["reason"]
    ):
        from .segment_starts import segment_start_from_set

        actual_start = segment_start_from_set(db, setrow)
        if (facts.start_key, facts.start_observation) != (
            actual_start.key,
            actual_start.observation,
        ):
            raise ConsistencyError("分段起点或原始观察引用与原基准和实际扫描不同")
    state=SegmentAccumulator(ref,setrow['target_duration_ms'],facts.start_key)
    for member in iter_segment_members(ref,Reader):
        state=advance_segment_assessment(state,member)
    actual=finish_segment_assessment(state,facts)
    if actual!=assessment:
        raise ConsistencyError('保存判断与原冻结成员的 P1 规则不一致')
    if actual.gap_count is None:
        if next(_stored_gaps(ref,Reader),None) is not None:
            raise ConsistencyError('缺口分类未知时不能保存已确认缺口行')
        return
    generated=iter_segment_gaps(iter_segment_members(ref,Reader),last_nonzero_cursor=actual.last_nonzero_cursor,
        last_owned_cursor=actual.last_owned_cursor,start_key=actual.confirmed_start_key)
    count=0;first=None
    for expected,saved in zip_longest(generated,_stored_gaps(ref,Reader)):
        if expected!=saved:
            raise ConsistencyError('第二遍缺口缺项、多项、身份或邻接事实不同')
        count+=1
        if first is None:first=saved
    if count!=actual.gap_count or first!=actual.first_gap:
        raise ConsistencyError('两遍缺口数量或首项不一致')


def _verify_action6_decision(db, setrow, decision):
    """独立复核 Action6 公共正常判断、冻结目标和非照片视频范围。"""
    from camctl.capture.segment_models import (
        CoverageMetric, CoverageStatus, DefaultScopeState, SegmentDecisionStatus,
    )
    from camctl.devices.segment_profiles import profile_for_policy
    from camctl.devices.tasks import CaptureResultPolicy

    action = row_facts(db, 'actions', setrow['action_id'])
    spec = action['execution_spec_json']
    if spec.get('result_policy') != CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value:
        raise ConsistencyError('公共 Action6 决策不适用于冻结的其他分段策略')
    if decision.set_ref != _ref(setrow):
        raise ConsistencyError('Action6 决策改变原冻结集合')
    from camctl.contracts.json_values import parse_exact_json
    params = action['effective_params_json']
    if isinstance(params, str):
        params = parse_exact_json(params)
    action_type = enum_for('actions.type')(action['type']).name.lower()
    target = profile_for_policy(spec['result_policy']).coverage_target(
        params, spec, action_type)
    if target is None:
        raise ConsistencyError('Action6 冻结特化没有提供覆盖目标')
    if (decision.metric, decision.target) != (target.metric, target.value):
        raise ConsistencyError('Action6 覆盖目标与受理时冻结参数不同')
    if decision.coverage_status is CoverageStatus.NOT_PERFORMED:
        if decision.confirmed is not None or decision.checked_count != 0:
            raise ConsistencyError('未执行 Action6 媒体检查不得携带实测值或检查计数')
    elif decision.coverage_status is CoverageStatus.REACHED:
        if decision.confirmed is None or decision.confirmed < decision.target:
            raise ConsistencyError('Action6 达标结论低于冻结目标')
    elif decision.coverage_status is CoverageStatus.CONFIRMED_SHORT:
        if decision.confirmed is None or decision.confirmed >= decision.target:
            raise ConsistencyError('Action6 不足结论未低于冻结目标')

    original_count = 0
    first_cursor = None
    last_cursor = None
    cursor_at_checked_count = None
    prefix_count = 0
    prefix_first_cursor = None
    prefix_last_cursor = None
    prefix_open = True
    ordered = True
    known_gap = False
    previous_key = None
    cursor = None
    Reader = type('Reader', (), {'connection': db})
    while True:
        page = read_segment_members(_ref(setrow), cursor, 128, Reader)
        for stored in page.items:
            member = stored.member
            if not _eligible_original(member):
                continue
            if member.cursor is None:
                ordered = False
                continue
            original_count += 1
            first_cursor = member.cursor if first_cursor is None else min(first_cursor, member.cursor)
            last_cursor = member.cursor if last_cursor is None else max(last_cursor, member.cursor)
            if previous_key is not None:
                if member.key <= previous_key:
                    ordered = False
                else:
                    try:
                        if previous_key.successor() != member.key:
                            known_gap = True
                            prefix_open = False
                    except ValueError:
                        known_gap = True
                        prefix_open = False
            previous_key = member.key
            if prefix_open:
                prefix_count += 1
                if prefix_first_cursor is None:
                    prefix_first_cursor = member.cursor
                prefix_last_cursor = member.cursor
            if original_count == decision.checked_count:
                cursor_at_checked_count = member.cursor
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    selected = decision.default_scope
    if selected.state is DefaultScopeState.EMPTY:
        expected_empty = (
            prefix_count == 0
            if decision.coverage_status is CoverageStatus.NOT_PERFORMED
            or decision.status is SegmentDecisionStatus.CANCELED
            else decision.checked_count == 0
        )
        if not expected_empty:
            raise ConsistencyError('Action6 空视频范围仍存在应纳入的合格原片')
    elif selected.state is DefaultScopeState.NONEMPTY:
        full_scope = (decision.coverage_status is CoverageStatus.NOT_PERFORMED
            or decision.status is SegmentDecisionStatus.CANCELED)
        expected_count = (
            (prefix_count if known_gap else original_count)
            if full_scope else decision.checked_count
        )
        expected_first = (
            (prefix_first_cursor if known_gap else first_cursor)
            if full_scope else first_cursor
        )
        expected_last = (
            (prefix_last_cursor if known_gap else last_cursor)
            if full_scope else cursor_at_checked_count
        )
        if (not ordered or expected_count != selected.member_count
                or expected_first != selected.first_cursor
                or expected_last != selected.last_cursor):
            raise ConsistencyError('Action6 默认范围必须是有序合格视频的完整范围或已检查前缀')
    elif selected.state is not DefaultScopeState.UNKNOWN:
        raise ConsistencyError('Action6 默认范围状态无效')
    if decision.status is SegmentDecisionStatus.SUCCEEDED:
        if (setrow['source_kind'] != int(enum_for('segment_sets.source_kind').COMPLETE_SCAN)
                or selected.state is not DefaultScopeState.NONEMPTY
                or decision.coverage_status is CoverageStatus.NOT_PERFORMED and known_gap):
            raise ConsistencyError('Action6 正常成功缺少完整扫描或已知默认视频范围')
    if decision.status is SegmentDecisionStatus.CANCELED:
        if setrow['publication_kind'] != int(enum_for('segment_sets.publication_kind').CANCELED):
            raise ConsistencyError('Action6 取消必须使用原取消发布类型')
    elif setrow['publication_kind'] == int(enum_for('segment_sets.publication_kind').CANCELED):
        raise ConsistencyError('Action6 取消发布必须保存取消决策')


def _fixed_file(db,file_id,file_event_id):
    """恢复独立文件到原固定事实事务；不读取整个动作。"""
    from camctl.contracts.enums import load_registry
    own_transaction=not db.in_transaction
    if own_transaction:db.execute('BEGIN')
    try:
        current=row_facts(db,'device_files',file_id)
        file_type=load_registry()['history_objects']['device_file']['id']
        anchor=_one(db,'SELECT e.transaction_id FROM history_events e JOIN entity_event_links l ON l.event_id=e.id WHERE e.id=? AND l.entity_type=? AND l.entity_id=?',
            (file_event_id,file_type,file_id))
        if current is None or anchor is None:
            raise ConsistencyError('固定文件位置没有同一真实文件的可靠自身历史')
        transaction=read_transaction_range(db,anchor[0])
        latest=_one(db,'SELECT MAX(event_id) FROM entity_event_links WHERE entity_type=? AND entity_id=? AND event_id<=?',(file_type,file_id,transaction.last_event_id))[0]
        if latest!=file_event_id:
            raise ConsistencyError('固定文件引用不是原完整事务内该文件的最后事实')
        # 独立文件的当前值由自身最后事件固定。使用该事件的完整
        # 事务作为C，避免为每份文件反复核实无关的增长完成事务。
        head=_one(db,'SELECT t.id,t.last_event_id FROM history_events e '
            'JOIN history_transactions t ON t.id=e.transaction_id WHERE e.id=?',
            (current['last_event_id'],))
        if head is None:raise ConsistencyError('文件当前状态缺少自己的可靠最后事件')
        frozen=read_row_values_at_boundary(db,owner=('device_file',file_id),table='device_files',row_id=file_id,
            columns=business_columns('device_files'),current_values=current,
            boundary=HistoryBoundary(transaction.txn_id,transaction.last_event_id),
            current_boundary=HistoryBoundary(head[0],head[1]))
        return {'id':file_id,**frozen,'last_event_id':file_event_id}
    finally:
        if own_transaction:db.execute('ROLLBACK')


def _input_member(db,item, *, current_boundary=None):
    file=_fixed_file(db,item.file_id,item.file_event_id)
    saved,_=_at(db,item.page_event_id)
    identity=parse_exact_json(file['identity_key'])[2]
    entry=next((value for value in saved.page.entries if value.identity==identity),None)
    if entry is None or (identity,item.file_id) not in saved.file_ids:
        raise ConsistencyError('固定文件与原页没有相同实际身份')
    _verify_strict_member(db, item, file, saved, current_boundary=current_boundary)
    source=row_facts(db,'operation_runs',saved.ref.ticket.run_id)['action_id']
    parsed=profile_for_action(db,source).classify_file(identity)
    size=file['size_bytes']
    return SegmentMember(identity,None if parsed is None else parsed.key,item.role,item.ownership,item.completion,
        size,item.pairing_identity,item.pairing_state),file


def _uses_strict_proof(item, file):
    evidence = file['ownership_evidence_json']
    return (item.pairing_fact_id is not None or isinstance(evidence, dict)
        and evidence.get('method') == int(enum_for(
            'device_files.ownership_evidence_json.method').RESULT_PAGE_DIFFERENCE))


def _verify_strict_member(db, item, file, saved, *, current_boundary=None):
    if not _uses_strict_proof(item, file):
        return False
    from .result_file_proofs import load_strict_result_file_input
    anchor = _one(db, 'SELECT transaction_id FROM history_events WHERE id=?', (item.file_event_id,))
    if anchor is None:
        raise ConsistencyError('严格成员没有真实文件自身事务')
    transaction = read_transaction_range(db, anchor[0])
    actual = load_strict_result_file_input(db, saved.ref, item.file_id,
        HistoryBoundary(transaction.txn_id, transaction.last_event_id), pairing_fact_id=item.pairing_fact_id,
        current_boundary=current_boundary)
    if actual != item:
        raise ConsistencyError('成员分类或配对事实与固定文件的严格证明不同')
    return True


def _member_row(db,setrow,item, *, current_boundary=None):
    member,file=_input_member(db,item,current_boundary=current_boundary);ref=_ref(setrow)
    action,activity,run,attempt,_=_source(db,ref)
    saved,raw=_at(db,item.page_event_id)
    entry=next((value for value in saved.page.entries if value.identity==member.identity),None)
    if (file is None or entry is None or (member.identity,item.file_id) not in saved.file_ids
        or raw['run_id']!=run['id'] or raw['attempt_no']!=attempt['attempt_no']
        or item.page_event_id>ref.final_page_event_id or file['source_action_id'] not in (None,ref.action_id)):
        raise ConsistencyError('成员的原页、文件或动作归属不同')
    profile=profile_for_action(db,ref.action_id)
    parsed=profile.classify_file(member.identity)
    strict = _uses_strict_proof(item, file)
    role=(SegmentMemberRole.ORIGINAL if parsed.kind is SegmentFileKind.VIDEO else SegmentMemberRole.PREVIEW if parsed.kind is SegmentFileKind.PREVIEW else SegmentMemberRole.OTHER)
    owned=OwnershipState.OWNED if file['source_action_id']==ref.action_id else OwnershipState.UNKNOWN
    completion=FileCompletionState.COMPLETE if file['completion_state']==3 else FileCompletionState.INCOMPLETE if file['completion_state']==4 else FileCompletionState.UNKNOWN
    if not strict and (member.key!=(None if parsed is None else parsed.key) or member.role!=role or member.ownership!=owned
        or member.completion!=completion
        or (completion is FileCompletionState.COMPLETE and (not entry.complete or entry.size_bytes!=file['size_bytes']))):
        raise ConsistencyError('成员分类与原可靠文件和实际页不一致')
    if not strict and member.pairing_state is PreviewPairingState.PAIRED:
        original=None if file['original_device_file_id'] is None else row_facts(db,'device_files',file['original_device_file_id'])
        if (original is None or original['source_action_id']!=ref.action_id or original['role']!=2
            or parse_exact_json(original['identity_key'])[2]!=member.pairing_identity
            or entry.paired_identity!=member.pairing_identity):
            raise ConsistencyError('预览配对不是同动作可靠原片')
        other=profile.classify_file(member.pairing_identity)
        if parsed.key is None or other.key is None or parsed.pairing_identity!=other.pairing_identity:
            raise ConsistencyError('预览与原片名称主干不匹配')
    if not strict and member.pairing_state is PreviewPairingState.UNPAIRED and (file['original_device_file_id'] is not None or entry.paired_identity is not None):
        raise ConsistencyError('已配对文件不能保存为未配对')
    return dict(set_id=setrow['id'],file_id=item.file_id,page_event_id=item.page_event_id,file_event_id=item.file_event_id,
        identity=member.identity,directory_number=None if member.key is None else member.key.directory_number,
        segment_index=None if member.key is None else member.key.index,role=encode_enum(member.role),ownership=encode_enum(member.ownership),
        completion=encode_enum(member.completion),pairing_state=encode_enum(member.pairing_state),
        pairing_identity=member.pairing_identity,pairing_fact_id=item.pairing_fact_id)


def _gap_row(db,setrow,number,gap):
    def ref(identity):
        return None if identity is None else _member_by_identity(db,setrow['id'],identity)['id']
    empty=ref(gap.empty_member_identity)
    if empty is not None:
        stored=_read_member(db,row_facts(db,'segment_members',empty)).member
        if stored.role is not SegmentMemberRole.ORIGINAL or stored.ownership is not OwnershipState.OWNED or stored.completion is not FileCompletionState.COMPLETE or stored.size_bytes!=0 or stored.key!=gap.start_key:
            raise ConsistencyError('非尾零缺口必须引用真实完整零原片')
    return dict(set_id=setrow['id'],gap_no=number,start_directory=gap.start_key.directory_number,start_index=gap.start_key.index,
        end_directory=gap.end_key.directory_number,end_index=gap.end_key.index,kind=encode_enum(gap.kind),
        left_member_id=ref(gap.left_member_identity),right_member_id=ref(gap.right_member_identity),empty_member_id=empty,
        neighbors_json={'left':encode_enum(gap.left_neighbor_state),'right':encode_enum(gap.right_neighbor_state)})


def _verify_set_identity(db, ref, values):
    action, activity, run, attempt, _ = _source(db, ref)
    expected = {'action_id':ref.action_id,'activity_id':activity['id'],'run_id':run['id'],
        'current_attempt_id':attempt['id'],'current_final_page_event_id':ref.final_page_event_id,
        'version':ref.version,'target_duration_ms':action['execution_spec_json'].get('target_duration_ms')}
    if any(not json_equal(values.get(name),value) for name,value in expected.items()):
        raise ConsistencyError('集合事件与原执行定义、活动和扫描不同')


def _source_member_count(db, ref):
    *_, tail = _source(db,ref)
    class Reader:
        connection = db
    count=0;position=None
    while True:
        page=read_pages(tail.ref.ticket,position,1,Reader)
        count+=sum(len(saved.file_ids) for saved in page.items)
        if page.next_cursor is None:return count
        position=page.next_cursor


def _expected_scope_ids(db,setrow,count):
    if setrow['state']!=_STATE.ASSESSED:
        raise ConsistencyError('默认成员需要已保存判断')
    selected=assessment_from_document(setrow['assessment_json']).default_scope
    if selected.state is not DefaultScopeState.NONEMPTY:
        raise ConsistencyError('未知或空范围没有可追加成员')
    previous=_one(db,'SELECT ordinal,member_id FROM segment_scope_members WHERE set_id=? ORDER BY ordinal DESC LIMIT 1',(setrow['id'],))
    number=0 if previous is None else previous[0]
    if number+count>selected.member_count:
        raise ConsistencyError('范围成员超过原判断数量')
    first,last=selected.first_cursor,selected.last_cursor
    after=(first.key.directory_number,first.key.index,first.member_identity,0) if previous is None else _position(row_facts(db,'segment_members',previous[1]))
    upper=(last.key.directory_number,last.key.index,last.member_identity,MAX_OBJECT_ID)
    order='(COALESCE(m.directory_number,0),COALESCE(m.segment_index,0),m.identity,m.id)'
    with closing(db.execute(f'SELECT m.id FROM segment_members m '
        f'WHERE m.set_id=? AND m.role=? AND m.ownership=? AND m.completion=? '
        f'AND {order}>(?,?,?,?) AND {order}<=(?,?,?,?) ORDER BY {order[1:-1]} LIMIT ?',
        (setrow['id'],encode_enum(SegmentMemberRole.ORIGINAL),encode_enum(OwnershipState.OWNED),
         encode_enum(FileCompletionState.COMPLETE),*after,*upper,count))) as reader:
        ids=tuple(row[0] for row in reader.fetchmany(count))
    if len(ids)!=count:raise ConsistencyError('范围成员不能覆盖所请求的可靠连续次序')
    for identity in ids:
        candidate=_output_candidate(db,setrow,_read_member(db,row_facts(db,'segment_members',identity)))
        if candidate is None or candidate.kind is not OutputKind.ORIGINAL:
            raise ConsistencyError('范围成员不是冻结事实中的合格原片')
    return number,ids


def _member_input_from_row(row):
    return SegmentMemberInput(row['file_id'],row['page_event_id'],row['file_event_id'],
        decode_enum(SegmentMemberRole,row['role']),decode_enum(OwnershipState,row['ownership']),
        decode_enum(FileCompletionState,row['completion']),row['pairing_identity'],
        decode_enum(PreviewPairingState,row['pairing_state']),row['pairing_fact_id'])


def _verify_request(event,ref,context,db):
    branch=next(name for name,value in _EVENT['branches'].items() if value['reason']==event.reason)
    def values(change):
        return {**context.state_rows.get(change.table,{}).get(change.row_id,{}),**change.after.values}
    if branch=='CREATE':
        row=values(event.rows[0])
        if row['state']!=_STATE.COLLECTING or any(row[name] is not None for name in ('member_count','facts_json','assessment_json','published_event_id')):
            raise ConsistencyError('新集合必须从收集状态开始')
        request=SegmentSetCreate(ref,row['activity_id'],row['run_id'],row['target_duration_ms'],event.occurred_at)
        from .result_pages import _page_before_boundary
        from camctl.capture.segment_storage import SegmentSourceKind,PublicationKind
        boundary=_page_before_boundary(db,context.transaction)
        if (row['source_kind']!=encode_enum(SegmentSourceKind.COMPLETE_SCAN)
                or row['publication_kind']!=encode_enum(PublicationKind.RESULT)
                or (row['source_txn_id'],row['source_event_id'])!=(boundary.txn_id,boundary.last_event_id)
                or row['predecessor_set_id'] is not None or row['cancel_event_id'] is not None):
            raise ConsistencyError('原完整扫描模型必须保留其实际创建前边界及来源')
    elif branch=='MEMBERS':
        request=tuple(_member_input_from_row(row) for change in event.rows for row in (values(change),))
    elif branch=='GAPS':
        request=tuple(_gap(db,values(change)) for change in event.rows)
        setrow=_set(db,ref)
        number=_one(db,'SELECT COALESCE(MAX(gap_no),0) FROM segment_gaps WHERE set_id=?',(setrow['id'],))[0]
        if any(values(change)['gap_no']!=number+i+1 for i,change in enumerate(event.rows)):
            raise ConsistencyError('缺口事件必须连续推进原序号')
    elif branch=='ASSESS':
        row=values(event.rows[0])
        request=(facts_from_document(row['facts_json']),assessment_from_document(row['assessment_json']))
    elif branch=='SCOPE':
        request=tuple(values(change)['member_id'] for change in event.rows)
    elif branch=='ACTIVATE':
        request=None
    elif branch=='PUBLISH':
        request=tuple((change.row_id,values(change)['output_id']) for change in event.rows)
    else:
        request=None
    expected={'segment_request':{'ref':document(ref),'input':_request_document(request)}}
    if not json_equal(event.evidence,expected):
        raise ConsistencyError('分段事件没有完整原有限申请')


def verify_segment_event(db,ref,event,context,*,prior_events=()):
    """提交守卫独立复核有限行与冻结集合，不相信调用方分类摘要。"""
    if len(event.rows)>128:
        raise ConsistencyError('分段事件超过有限行数')
    from camctl.capture.segment_storage import SegmentResultRef
    if isinstance(ref,SegmentResultRef):
        if event.reason == _EVENT["branches"]["DECIDE_RESULT"]["reason"]:
            from .segment_dispositions import verify_disposition_event

            return verify_disposition_event(db, ref, event)
        if event.reason == _EVENT["branches"]["ACTIVATE"]["reason"]:
            from .segment_transactions import verify_publication

            _verify_request(event, ref, context, db)
            return verify_publication(db, ref, event, context)
        if event.reason==_EVENT['branches']['CONCLUDE_CHECK']['reason']:
            from .segment_check_conclusions import verify_conclusion_event
            return verify_conclusion_event(db,ref,event,context)
        if event.reason==_EVENT['branches']['CREATE_RESULT']['reason']:
            from .segment_result_sets import verify_create_event
            return verify_create_event(db,ref,event,context)
        from .segment_result_members import verify_result_member_event
        return verify_result_member_event(db,ref,event,context)
    _verify_request(event,ref,context,db)
    branch=next(name for name,value in _EVENT['branches'].items() if value['reason']==event.reason)
    if branch!='CREATE':
        _check_write_kind(db,_set(db,ref),branch)
    if event.reason in (_EVENT['branches']['PUBLISH']['reason'],_EVENT['branches']['ACTIVATE']['reason']):
        _verify_publication(db,ref,event,context,prior_events)
    if any(not change.before.exists and change.after.values.get('created_event_id')!=event.event_id for change in event.rows):
        raise ConsistencyError('分段所属行必须引用自己的实际创建事件')
    if event.reason==_EVENT['branches']['SCOPE']['reason']:
        setrow=_set(db,ref)
        number,expected=_expected_scope_ids(db,setrow,len(event.rows))
        if any(change.after.values.get('member_id')!=identity or change.after.values.get('ordinal')!=number+i+1 or change.after.values.get('output_id') is not None for i,(change,identity) in enumerate(zip(event.rows,expected))):
            raise ConsistencyError('默认范围事件跳过或重排原有序成员')
    for change in event.rows:
        values={**context.state_rows.get(change.table,{}).get(change.row_id,{}),**change.after.values}
        if change.table=='segment_sets':
            _verify_set_identity(db,ref,values)
            if values['state']==_STATE.MEMBERS_FIXED:
                count=_one(db,'SELECT COUNT(*) FROM segment_members WHERE set_id=?',(values['id'],))[0]
                if count!=_source_member_count(db,ref) or values['member_count']!=count:
                    raise ConsistencyError('固定事件遗漏原完整扫描成员')
            if values['state']==_STATE.ASSESSED and event.reason==_EVENT['branches']['ASSESS']['reason']:
                _verify_assessment(db,values,facts_from_document(values['facts_json']),assessment_from_document(values['assessment_json']))
        else:
            setrow=_set(db,ref)
            if values['set_id']!=setrow['id']:raise ConsistencyError('分段事件混入其他集合')
            if change.table=='segment_members':
                if setrow['state']!=_STATE.COLLECTING:raise ConsistencyError('固定集合不能追加成员')
                from .result_file_ownership import _guard_boundary
                pending_boundary = _guard_boundary(db, event, context.transaction)
                stored=_read_member(db,{'id':change.row_id,**values},current_boundary=pending_boundary)
                expected={**_member_row(db,setrow,_member_input_from_row(values),current_boundary=pending_boundary),'created_event_id':values['created_event_id']}
                from camctl.contracts.enums import load_registry
                file_type=load_registry()['history_objects']['device_file']['id']
                link=_one(db,'SELECT 1 FROM entity_event_links l JOIN history_events e ON e.id=l.event_id WHERE l.entity_type=? AND l.entity_id=? AND l.event_id=?',
                    (file_type,stored.file_id,values['file_event_id']))
                if link is None or values['file_event_id']>=event.event_id or values['page_event_id']>=event.event_id:
                    raise ConsistencyError('成员必须引用同一真实文件的可靠先前历史')
                if not json_equal(values,{**expected,'id':change.row_id}) and not json_equal(values,expected):
                    raise ConsistencyError('分段成员事件与可靠原输入不符')
            elif change.table=='segment_gaps':
                if setrow['state']!=_STATE.MEMBERS_FIXED:raise ConsistencyError('缺口只能在成员固定后判断发布前保存')
                gap=_gap(db,values)
                expected=_gap_row(db,setrow,values['gap_no'],gap)
                if any(not json_equal(values.get(field),value) for field,value in expected.items()):
                    raise ConsistencyError('缺口事件与实际成员分类不同')
                for field in ('left_member_id','right_member_id','empty_member_id'):
                    if values[field] is not None and row_facts(db,'segment_members',values[field])['set_id']!=setrow['id']:
                        raise ConsistencyError('缺口引用其他集合')
            elif change.table=='segment_scope_members':
                member=row_facts(db,'segment_members',values['member_id'])
                if member is None or member['set_id']!=setrow['id'] or member['role']!=encode_enum(SegmentMemberRole.ORIGINAL):
                    raise ConsistencyError('默认范围只能引用本集合原片')
                if values['output_id'] is not None:
                    output=context.state_rows.get('outputs',{}).get(values['output_id']) or row_facts(db,'outputs',values['output_id'])
                    action=context.state_rows.get('actions',{}).get(ref.action_id) or row_facts(db,'actions',ref.action_id)
                    if output is None or output['source_action_id']!=ref.action_id or output['device_file_id']!=member['file_id'] or output['kind']!=1 or action['status'] not in (3,4,5,6):
                        raise ConsistencyError('正式默认关联必须与来源终态、实际 original 共同发布')


def register_segment_guards():
    def guard(event,context):
        try:
            if context.current_reads is None:raise ConsistencyError('分段守卫缺少原事务有限读端口')
            context.current_reads.verify_segment_event(event,context)
        except (ValueError,KeyError,TypeError) as error:
            raise EventValidationError(str(error)) from error
    register_guard('segments',guard)


class _Command:
    def __init__(self,kind,ref,payload,occurred_at,key):
        UtcMicros(occurred_at)
        self.kind,self.ref,self.payload,self.at,self.key=kind,ref,payload,occurred_at,key

    def plan(self,scope):
        db=scope.connection;ref=self.ref;kind=self.kind;payload=self.payload
        encoded=_request_document(payload)
        saved=saved_transaction_events(db,self.key)
        if saved is not None:
            if len(saved)!=1 or saved[0]['type']!=_EVENT['id'] or saved[0]['reason']!=_EVENT['branches'][kind]['reason'] or saved[0]['occurred_at']!=self.at or not json_equal(saved[0]['body']['evidence'],{'segment_request':{'ref':document(ref),'input':encoded}}):
                raise TransactionError('原分段申请的类型、引用、完整输入或时刻不同')
            if kind == 'MEMBERS':
                original_rows = saved[0]['body']['rows']
                setrow = _set(db, ref)
                if len(original_rows) != len(payload):
                    raise ConsistencyError('原成员事务没有精确覆盖原有限申请')
                for change, item in zip(original_rows, payload):
                    values = change['after']['values']
                    actual = row_facts(db, 'segment_members', change['id'])
                    if (change['table'] != 'segment_members' or change['before']['exists']
                            or not change['after']['exists'] or values['set_id'] != setrow['id']
                            or values['created_event_id'] != saved[0]['event_id']
                            or _member_input_from_row(values) != item or actual is None
                            or not json_equal({name: actual[name] for name in values}, values)):
                        raise ConsistencyError('原成员事实、实际投影或固定证明引用不同')
                    _read_member(db, actual)
            return CommandPlan((),{}, {},ref,True)
        action,activity,run,attempt,tail=_source(db,ref)
        state={'actions':{ref.action_id:action},'segment_sets':{}}
        if kind=='CREATE':
            request=payload
            if request.activity_id!=activity['id'] or request.run_id!=run['id'] or request.target_duration_ms!=action['execution_spec_json'].get('target_duration_ms'):
                raise ConsistencyError('集合声明与原执行定义、扫描或活动不一致')
            sid=next_row_id(db,'segment_sets')
            from camctl.capture.segment_storage import SegmentSourceKind,PublicationKind
            rows=[row_change('segment_sets',sid,dict(action_id=ref.action_id,activity_id=activity['id'],run_id=run['id'],
                current_attempt_id=attempt['id'],current_final_page_event_id=ref.final_page_event_id,
                source_kind=encode_enum(SegmentSourceKind.COMPLETE_SCAN),source_txn_id=scope.max_txn_id,
                source_event_id=scope.max_event_id,publication_kind=encode_enum(PublicationKind.RESULT),
                predecessor_set_id=None,cancel_event_id=None,version=ref.version,
                target_duration_ms=request.target_duration_ms,state=int(_STATE.COLLECTING),member_count=None,
                facts_json=None,assessment_json=None,published_event_id=None))]
        else:
            setrow=_set(db,ref);sid=setrow['id'];state['segment_sets'][sid]=setrow
            _check_write_kind(db,setrow,kind)
            if kind=='MEMBERS':
                if setrow['state']!=_STATE.COLLECTING:raise ConsistencyError('已固定集合不能追加成员')
                start=next_row_id(db,'segment_members');_capacity(start,len(payload))
                rows=[row_change('segment_members',start+i,_member_row(db,setrow,item)) for i,item in enumerate(payload)]
            elif kind=='FIX_MEMBERS':
                if setrow['state']!=_STATE.COLLECTING:raise ConsistencyError('成员只能固定一次')
                count=0;position=None
                while True:
                    page=read_pages(tail.ref.ticket,position,1,scope)
                    count+=sum(len(saved.file_ids) for saved in page.items)
                    if page.next_cursor is None:break
                    position=page.next_cursor
                actual=_one(db,'SELECT COUNT(*) FROM segment_members WHERE set_id=?',(sid,))[0]
                if actual!=count:raise ConsistencyError('固定成员没有覆盖原完整差集页')
                rows=[update_change('segment_sets',sid,{'state':setrow['state'],'member_count':None},{'state':int(_STATE.MEMBERS_FIXED),'member_count':actual})]
            elif kind=='GAPS':
                if setrow['state']!=_STATE.MEMBERS_FIXED:raise ConsistencyError('缺口需要固定成员且尚未发布判断')
                start=next_row_id(db,'segment_gaps');_capacity(start,len(payload))
                number=_one(db,'SELECT COALESCE(MAX(gap_no),0) FROM segment_gaps WHERE set_id=?',(sid,))[0]
                _capacity(number+1,len(payload))
                rows=[row_change('segment_gaps',start+i,_gap_row(db,setrow,number+i+1,gap)) for i,gap in enumerate(payload)]
            elif kind=='ASSESS':
                if setrow['state']!=_STATE.MEMBERS_FIXED:raise ConsistencyError('判断只能在固定成员后保存一次')
                facts,assessment=payload
                from camctl.capture.segment_models import SegmentDecision
                if isinstance(assessment, SegmentDecision):
                    if facts is not None:
                        raise ConsistencyError('公共分段判断不得伪造 OSMO 完成事实')
                elif facts is None:
                    raise ConsistencyError('OSMO 分段判断必须保存原完成事实')
                _verify_assessment(db,setrow,facts,assessment)
                from camctl.capture.segment_storage import facts_document

                rows=[update_change('segment_sets',sid,{'state':setrow['state'],'facts_json':None,'assessment_json':None},
                    {'state':int(_STATE.ASSESSED),'facts_json':facts_document(
                        facts, public_decision=isinstance(assessment, SegmentDecision)
                    ),'assessment_json':assessment_document(assessment)})]
            elif kind=='SCOPE':
                if setrow['state']!=_STATE.ASSESSED:raise ConsistencyError('默认范围需要原已保存判断')
                assessment=assessment_from_document(setrow['assessment_json']);selected=assessment.default_scope
                if selected.state is not DefaultScopeState.NONEMPTY:raise ConsistencyError('未知或空范围不能建立成员')
                number,expected=_expected_scope_ids(db,setrow,len(payload))
                if payload!=expected:raise ConsistencyError('默认范围必须沿原设备顺序连续保存')
                start=next_row_id(db,'segment_scope_members');_capacity(start,len(payload));_capacity(number+1,len(payload));rows=[]
                for i,member_id in enumerate(payload):
                    member=row_facts(db,'segment_members',member_id)
                    if member is None or member['set_id']!=sid:raise ConsistencyError('范围成员来自其他集合')
                    stored=_read_member(db,member)
                    actual=stored.member
                    candidate=_output_candidate(db,setrow,stored)
                    if candidate is None or candidate.kind is not OutputKind.ORIGINAL or not selected.first_cursor<=actual.cursor<=selected.last_cursor:
                        raise ConsistencyError('范围成员不是已保存范围内合格原片')
                    rows.append(row_change('segment_scope_members',start+i,dict(set_id=sid,ordinal=number+i+1,member_id=member_id,output_id=None)))
                if number+len(payload)>selected.member_count:raise ConsistencyError('范围成员超过原判断数量')
            else:raise ValueError('未登记的分段保存操作')
        allocation=scope.allocate(1)
        rows=[row_change(row.table,row.row_id,{**row.after.values,'created_event_id':allocation.first_event_id}) if not row.before.exists else row for row in rows]
        event=event_envelope(allocation.first_event_id,allocation.txn_id,_EVENT['id'],_EVENT['branches'][kind]['reason'],tuple(rows),self.at,
            {'segment_request':{'ref':document(ref),'input':encoded}})
        owners={(row.table,row.row_id):('action',ref.action_id) for row in rows}
        for row in rows:state.setdefault(row.table,{})
        return CommandPlan((event,),owners,state,ref,segment_read_scope=SegmentReadScope(ref))


def _capacity(start,count):
    if start+count-1>MAX_OBJECT_ID:raise ConsistencyError('分段行身份容量不足')


def _request_document(payload):
    if isinstance(payload,tuple):return [_request_document(item) for item in payload]
    if type(payload) is int:return payload
    if payload is None:return None
    if isinstance(payload,SegmentAssessment):return assessment_document(payload)
    return document(payload)


def segment_activation_rows(ref,event_id,owned):
    """P3 完成组中的唯一正式集合标记；本函数不提交。"""
    ObjectId(event_id)
    row=_set(owned.connection,ref,fixed=True)
    if not isinstance(ref, SegmentResultRef):
        _check_write_kind(owned.connection, row, "ACTIVATE")
    if row["state"] != _STATE.ASSESSED or row["published_event_id"] is not None:
        raise ConsistencyError('正式分段集合只能发布一次已保存判断')
    return (update_change('segment_sets',row['id'],{'published_event_id':None},{'published_event_id':event_id}),)


def _verify_final_activation(ref,setrow,event,context):
    """最终只证明固定marker身份；所有未来事件仍须顺序通过完整守卫。"""
    final=None if context.transaction_rows is None else context.transaction_rows.get('segment_sets',{}).get(setrow['id'])
    if final is None or final.get('published_event_id') is None:
        raise ConsistencyError('分段发布缺少同一完成事务的正式marker')
    marker=final['published_event_id']
    ObjectId(marker)
    if (marker<event.event_id
            or (event.reason==_EVENT['branches']['ACTIVATE']['reason'] and marker!=event.event_id)
            or context.current_reads is None
            or sum(row.get('action_id')==ref.action_id and row.get('published_event_id') is not None
                for row in context.transaction_rows.get('segment_sets',{}).values())!=1):
        raise ConsistencyError('正式marker必须唯一并在原发布范围之后')
    activation=context.current_reads.segment_activation_event(marker,context.transaction)
    expected={'segment_request':{'ref':document(ref),'input':None}}
    if (activation.event_type!=_EVENT['id'] or activation.reason!=_EVENT['branches']['ACTIVATE']['reason']
            or not json_equal(activation.evidence,expected) or len(activation.rows)!=1):
        raise ConsistencyError('正式marker没有同ref的唯一实际ACTIVATE')
    change=activation.rows[0]
    if (change.table!='segment_sets' or change.row_id!=setrow['id']
            or not change.before.exists or not change.after.exists
            or not json_equal(change.before.values,{'published_event_id':None})
            or not json_equal(change.after.values,{'published_event_id':marker})):
        raise ConsistencyError('正式marker必须精确引用同集合自己的ACTIVATE事件')


def _verify_publication(db,ref,event,context,prior):
    """只承认本完成事务中已验证事件，不以当前终态代替同组证明。"""
    if getattr(context.current_reads,'proposal_event',None) is not None:
        from .segment_transactions import verify_publication
        return verify_publication(db,ref,event,context)
    def events():
        return (accepted.envelope for accepted in prior)
    setrow=_set(db,ref,fixed=True)
    if setrow['state']!=_STATE.ASSESSED or setrow['published_event_id'] is not None:
        raise ConsistencyError('发布缺少尚未正式发布的完整判断')
    if _one(db,'SELECT 1 FROM segment_sets WHERE action_id=? AND published_event_id IS NOT NULL',(ref.action_id,)):
        raise ConsistencyError('原动作已有唯一正式分段集合')
    assessment=assessment_from_document(setrow['assessment_json'])
    from camctl.capture.segment_finish import FinishSegmentRecording
    from .segment_transactions import verify_completion_proof
    class ProofReader:connection=db
    verify_completion_proof(FinishSegmentRecording(ref,setrow['activity_id'],setrow['run_id'],event.occurred_at),ProofReader)
    _verify_final_activation(ref,setrow,event,context)
    if event.reason==_EVENT['branches']['PUBLISH']['reason'] and assessment.default_scope.state is not DefaultScopeState.NONEMPTY:
        raise ConsistencyError('未知或空范围不能发布默认成员关联')
    terminal=any(accepted.event_type==load_event_registry()['events']['ACTION_FINISHED']['id']
        and any(row.table=='actions' and row.row_id==ref.action_id and row.after.values.get('status') in (3,4,5,6)
            for row in accepted.rows) for accepted in events())
    if not terminal:
        raise ConsistencyError('分段发布必须与原来源终态在同一完成事务')
    action=context.state_rows.get('actions',{}).get(ref.action_id)
    if action is None:raise ConsistencyError('完成组没有来源动作状态')
    if action['status']==3 and assessment.status is not SegmentAssessmentStatus.SUCCEEDED:
        raise ConsistencyError('来源成功与原分段判断不一致')
    if assessment.gap_count is None and (action['status']==3 or assessment.default_scope.state is not DefaultScopeState.UNKNOWN):
        raise ConsistencyError('缺口分类未知不能发布成功或可靠默认范围')
    def created_output(file_id,kind):
        result=None
        for accepted in events():
            if accepted.event_type!=load_event_registry()['events']['OUTPUT_REGISTERED']['id']:continue
            for change in accepted.rows:
                if change.table=='outputs' and not change.before.exists:
                    values=change.after.values
                    if values.get('source_action_id')==ref.action_id and values.get('device_file_id')==file_id and values.get('kind')==kind:
                        if result is not None:raise ConsistencyError('完成组为同一成员重复登记产物')
                        result=change.row_id
        return result
    for accepted in events():
        if accepted.event_type!=load_event_registry()['events']['OUTPUT_REGISTERED']['id']:continue
        for change in accepted.rows:
            values=change.after.values
            if change.table!='outputs' or change.before.exists or values.get('source_action_id')!=ref.action_id:continue
            found=_one(db,'SELECT id FROM segment_members WHERE set_id=? AND file_id=?',(setrow['id'],values.get('device_file_id')))
            if found is None:raise ConsistencyError('完成组产物不属于原完整冻结成员')
            candidate=_output_candidate(db,setrow,_read_member(db,row_facts(db,'segment_members',found[0])))
            if candidate is None or values.get('kind')!=int(enum_for('outputs.kind')[candidate.kind.name]):
                raise ConsistencyError('完成组登记了不合格冻结成员')
    # PUBLISH 的每个 output 必须来自同组已验证创建事件。
    for change in event.rows:
        if change.table=='segment_scope_members':
            member=row_facts(db,'segment_members',row_facts(db,'segment_scope_members',change.row_id)['member_id'])
            if created_output(member['file_id'],1)!=change.after.values['output_id']:
                raise ConsistencyError('默认关联不是本完成事务创建的实际原片')
    if event.reason!=_EVENT['branches']['ACTIVATE']['reason']:return
    if len(event.rows)!=1 or event.rows[0].row_id!=setrow['id'] or event.rows[0].after.values.get('published_event_id')!=event.event_id:
        raise ConsistencyError('正式引用必须精确绑定自己的发布事件')
    class Reader:connection=db
    original_count=0
    cursor=None
    while True:
        page=read_segment_members(ref,cursor,128,Reader)
        for stored in page.items:
            candidate=_output_candidate(db,setrow,stored)
            if candidate is None:continue
            if candidate.kind is OutputKind.ORIGINAL:original_count+=1
            kind=int(enum_for('outputs.kind')[candidate.kind.name])
            identity=created_output(candidate.file_id,kind)
            if identity is None:raise ConsistencyError('完成组遗漏应保留的实际原片或预览')
            if candidate.kind is OutputKind.PREVIEW:
                original=created_output(candidate.original_file_id,int(enum_for('outputs.kind').ORIGINAL))
                if original is None or not any(row.table=='output_origins' and not row.before.exists and row.after.values.get('output_id')==identity and row.after.values.get('original_output_id')==original for accepted in events() for row in accepted.rows):
                    raise ConsistencyError('完成组预览缺少同一真实原片的origin')
        if page.next_cursor is None:break
        cursor=page.next_cursor
    from camctl.contracts.workflow_errors import registered_error
    if action.get('error_code')==registered_error('capture_segment_gap')['action_error_id']:
        from camctl.capture.segment_errors import validate_segment_error_header,segment_error_header
        header=validate_segment_error_header('capture_segment_gap',action['error_details_json'])
        if header!=segment_error_header(assessment,original_count):
            raise ConsistencyError('来源缺口错误头与原判断或保留原片数量不同')
    expected=assessment.default_scope.member_count or 0
    count=0;cursor=None
    while True:
        page=_relation_page(ref,cursor,128,Reader,'segment_scope_members',SegmentScopeCursor,'ordinal')
        for row in page.items:
            count+=1
            output_id=row['output_id']
            for accepted in events():
                for changed in accepted.rows:
                    if changed.table=='segment_scope_members' and changed.row_id==row['id']:
                        output_id=changed.after.values.get('output_id',output_id)
            member=row_facts(db,'segment_members',row['member_id'])
            if output_id is None or output_id!=created_output(member['file_id'],1):
                raise ConsistencyError('正式集合缺少完整实际默认 output 关联')
        if page.next_cursor is None:break
        cursor=page.next_cursor
    if count!=expected:raise ConsistencyError('正式默认范围数量与原判断不一致')
    # 有限直接提案也必须完整履行原申请；不能把新仓储的complete回调当守卫。
    from .segment_transactions import _header_request,_failure,_verify_scope_members,_completion_qualification,_verify_qualification_header
    accepted=next(item for item in events() if item.event_type==load_event_registry()['events']['ACTION_FINISHED']['id']
        and any(row.table=='actions' and row.row_id==ref.action_id for row in item.rows))
    header=accepted.evidence.get('segment_finish_request')
    request=_header_request(header)
    _verify_qualification_header(header,_completion_qualification(db,request))
    _verify_assessment(db,setrow,facts_from_document(setrow['facts_json']),assessment)
    _verify_scope_members(request,assessment,Reader)
    status,error,details=_failure(request,assessment,original_count,setrow,db,before_event_id=context.transaction.first_event_id)
    if (action['status'],action['error_code'],action['error_details_json'])!=(status,error,details):
        raise ConsistencyError('直接完成组来源结果与原有限实际失败不同')
    registry=load_event_registry()['events']
    if header['finish_run']:
        run=row_facts(db,'operation_runs',request.run_id)
        expected_row=update_change('operation_runs',run['id'],{name:run[name] for name in ('status','retry_wait_required','error_json')},
            {'status':3,'retry_wait_required':0,'error_json':None})
        evidence={'stale_run_request':{'kind':'stale_run_finish','responsibility_keys':[run['responsibility_key']],'status':3,'error':None}}
        if not any(item.event_type==registry['OPERATION_CONFIGURED']['id'] and item.reason==3 and item.rows==(expected_row,)
                and item.occurred_at==request.occurred_at and json_equal(item.evidence,evidence) for item in events()):
            raise ConsistencyError('直接完成组没有共同结束原RESULTS责任')
    if header['complete_plan']:
        plan=row_facts(db,'plans',action['plan_id'])
        expected_row=update_change('plans',plan['id'],{'status':plan['status']},{'status':3})
        if not any(item.event_type==registry['PLAN_STATUS_CHANGED']['id'] and item.reason==2 and item.rows==(expected_row,)
                and item.occurred_at==request.occurred_at for item in events()):
            raise ConsistencyError('直接完成组没有共同结束适用父计划')


def segment_scope_publication_rows(ref,bindings,owned):
    """供 P3 终态事务形成一批 output 关联；不独立提交，不生成身份。"""
    _batch(bindings)
    setrow=_set(owned.connection,ref,fixed=True)
    _check_write_kind(owned.connection,setrow,'PUBLISH')
    rows=[]
    for scope_member_id,output_id in bindings:
        ObjectId(scope_member_id);ObjectId(output_id)
        row=row_facts(owned.connection,'segment_scope_members',scope_member_id)
        if row is None or row['set_id']!=setrow['id'] or row['output_id'] is not None:
            raise ConsistencyError('正式范围关联必须沿原尚未发布成员')
        rows.append(update_change('segment_scope_members',scope_member_id,{'output_id':None},{'output_id':output_id}))
    return tuple(rows)


def segment_event(ref,branch,rows,event_id,transaction_id,occurred_at,*,request_input):
    """构造一条最多128行的登记事件，供P3组合完整终态；守卫仍独立核对。"""
    _batch(rows)
    return event_envelope(event_id,transaction_id,_EVENT['id'],_EVENT['branches'][branch]['reason'],rows,occurred_at,
        {'segment_request':{'ref':document(ref),'input':_request_document(request_input)}})


class SegmentRepository:
    """每项写入只形成一个有限事件；返回固定集合引用。"""

    def create_result_set(self,request,key,owned):
        from .segment_result_sets import CreateResultSetCommand
        register_segment_guards()
        receipt=commit_operation(CreateResultSetCommand(request,key),key,owned)
        return (DbOutcome(DbOutcomeKind.COMPLETED,value=receipt.result) if receipt.kind=='completed'
            else DbOutcome(DbOutcomeKind.ROLLED_BACK if receipt.kind=='rolled_back' else DbOutcomeKind.UNKNOWN,
                error=receipt.error))

    def decide_result(self, ref, occurred_at, key, owned):
        from .segment_dispositions import DecideResultCommand

        register_segment_guards()
        receipt = commit_operation(
            DecideResultCommand(ref, occurred_at, key), key, owned
        )
        return DbOutcome(
            DbOutcomeKind(receipt.kind), value=receipt.result, error=receipt.error
        )

    def conclude_result_check(self,request,key,owned):
        from .segment_check_conclusions import ConcludeResultCheckCommand
        register_segment_guards()
        receipt=commit_operation(ConcludeResultCheckCommand(request,key),key,owned)
        return DbOutcome(DbOutcomeKind(receipt.kind),value=receipt.result,error=receipt.error)

    def verify_result_check_conclusion(self,request,key,owned,*,execution_finished=True):
        from .segment_check_conclusions import verify_result_check_conclusion
        return verify_result_check_conclusion(request,key,owned,execution_finished=execution_finished)

    def read_result_set(self,ref,owned):
        from .segment_result_sets import read_result_set
        return read_result_set(ref,owned)

    def read_current_segment_ref(self,ref,owned):
        from .segment_result_sets import read_current_segment_ref
        return read_current_segment_ref(ref,owned)

    def read_prepared_result_ref(self, action_id, owned):
        from .segment_result_sets import read_prepared_result_ref

        return read_prepared_result_ref(action_id, owned)

    def prepare_result_set(self, action_id, occurred_at, owned):
        from .segment_result_sets import prepare_result_set

        return prepare_result_set(action_id, occurred_at, owned)

    def read_start(self, ref, owned):
        from .segment_starts import read_segment_start

        return read_segment_start(ref, owned)

    def read_result_member_inputs(self,ref,cursor,limit,owned):
        from .segment_result_sets import read_result_member_inputs
        return read_result_member_inputs(ref,cursor,limit,owned)

    def find_canceled_result_ref(self,action_id,predecessor_set_id,cancel_event_id,owned):
        from .segment_result_sets import find_canceled_result_ref
        return find_canceled_result_ref(action_id,predecessor_set_id,cancel_event_id,owned)

    def _save_result_members(self,kind,ref,payload,occurred_at,key,owned):
        from .segment_result_members import ResultMembersCommand
        register_segment_guards()
        receipt=commit_operation(ResultMembersCommand(kind,ref,payload,occurred_at,key),key,owned)
        return (DbOutcome(DbOutcomeKind.COMPLETED,value=receipt.result) if receipt.kind=='completed'
            else DbOutcome(DbOutcomeKind.ROLLED_BACK if receipt.kind=='rolled_back' else DbOutcomeKind.UNKNOWN,
                error=receipt.error))

    def append_result_members(self,ref,items,occurred_at,key,owned):
        return self._save_result_members('MEMBERS',ref,items,occurred_at,key,owned)

    def fix_result_members(self,ref,occurred_at,key,owned):
        return self._save_result_members('FIX_RESULT_MEMBERS',ref,None,occurred_at,key,owned)
    def _save(self,kind,ref,payload,occurred_at,key,owned):
        register_segment_guards()
        receipt=commit_operation(_Command(kind,ref,payload,occurred_at,key),key,owned)
        return DbOutcome(DbOutcomeKind(receipt.kind),value=receipt.result,error=receipt.error)

    def create(self,request,key,owned):
        if not isinstance(request,SegmentSetCreate):raise TypeError('创建要求 SegmentSetCreate')
        return self._save('CREATE',request.ref,request,request.occurred_at,key,owned)

    def append_members(self,ref,members,occurred_at,key,owned):
        return self._save('MEMBERS',ref,_batch(members),occurred_at,key,owned)

    def fix_members(self,ref,occurred_at,key,owned):
        return self._save('FIX_MEMBERS',ref,None,occurred_at,key,owned)

    def append_gaps(self,ref,gaps,occurred_at,key,owned):
        if isinstance(ref, SegmentResultRef):
            return self._save_result_members(
                "GAPS", ref, _batch(gaps), occurred_at, key, owned
            )
        return self._save('GAPS',ref,_batch(gaps),occurred_at,key,owned)

    def save_assessment(self,ref,facts,assessment,occurred_at,key,owned):
        if not isinstance(facts,CompletionFacts) or not isinstance(assessment,SegmentAssessment):raise TypeError('判断需要 P1 事实和摘要')
        return self._save('ASSESS',ref,(facts,assessment),occurred_at,key,owned)

    def save_decision(self, ref, decision, occurred_at, key, owned):
        from camctl.capture.segment_models import SegmentDecision
        if not isinstance(decision, SegmentDecision):
            raise TypeError('公共分段判断需要 SegmentDecision')
        return self._save('ASSESS', ref, (None, decision), occurred_at, key, owned)

    def append_scope(self,ref,member_ids,occurred_at,key,owned):
        _batch(member_ids)
        for identity in member_ids:ObjectId(identity)
        return self._save('SCOPE',ref,member_ids,occurred_at,key,owned)

    def read_active_set(self,action_id,owned):
        ObjectId(action_id)
        found=_one(owned.connection,'SELECT id FROM segment_sets WHERE action_id=? AND published_event_id IS NOT NULL',(action_id,))
        return None if found is None else _ref(row_facts(owned.connection,'segment_sets',found[0]))

    def read_set(self,ref,owned):
        """有限元信息；返回新字典，不携带增长成员。"""
        return _set(owned.connection,ref).copy()

    def read_members(self,ref,cursor,batch,owned):return read_segment_members(ref,cursor,batch,owned)
    def read_gaps(self,ref,cursor,batch,owned):return read_segment_gaps(ref,cursor,batch,owned)
    def read_output_candidates(self,ref,cursor,batch,owned):return read_segment_output_candidates(ref,cursor,batch,owned)
    def read_scope(self,ref,cursor,batch,owned,*,published=True):return read_segment_scope(ref,cursor,batch,owned,published=published)
    def read_assessment(self,ref,owned):
        row=_set(owned.connection,ref,fixed=True)
        if row['state']!=_STATE.ASSESSED:raise ConsistencyError('原集合判断尚未保存')
        return assessment_from_document(row['assessment_json'])
    def read_decision(self, ref, owned):
        from camctl.capture.segment_models import SegmentDecision
        decision = self.read_assessment(ref, owned)
        if not isinstance(decision, SegmentDecision):
            raise ConsistencyError('原集合保存的不是公共分段判断')
        return decision
