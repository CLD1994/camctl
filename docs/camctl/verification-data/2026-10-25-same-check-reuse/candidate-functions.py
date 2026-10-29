def _verify_page_relations(connection, saved, raw, event, transaction):
    """按完整原事务核对 APPEND 与 INDEX_FILES 的双向精确关系。"""
    from .capture import file_discovery_row

    attempt_id = event.evidence["attempt_id"]
    with closing(connection.execute(
            "SELECT r.action_id,r.activity_id,r.id,a.run_id FROM operation_attempts a"
            " JOIN operation_runs r ON r.id=a.run_id WHERE a.id=?", (attempt_id,))) as cursor:
        owner = cursor.fetchone()
    if owner is None:
        raise ConsistencyError("结果页关系缺少原活动、动作或流程")
    action_id, activity_id, run_id, joined_run_id = owner
    if joined_run_id != raw["run_id"] or activity_id != raw["activity_id"]:
        raise ConsistencyError("结果页关系改变原流程、活动或尝试")
    action = row_facts(connection, "actions", action_id)
    if action is None:
        raise ConsistencyError("结果页动作不存在")
    append_id = event.event_id
    with closing(connection.execute(
            f"SELECT {_COLUMNS} FROM history_events WHERE transaction_id=? ORDER BY id LIMIT 131",
            (transaction.txn_id,))) as cursor:
        transaction_events = [decode_event_row(row) for row in cursor]
    if (len(transaction_events) != transaction.last_event_id - transaction.first_event_id + 1
            or len(transaction_events) > 130
            or any(item.event_id != transaction.first_event_id + index
                   or item.transaction_id != transaction.txn_id
                   for index, item in enumerate(transaction_events))):
        raise ConsistencyError("结果页所在原事务事件范围不连续或超过页组容量")
    append_position = next((index for index, item in enumerate(transaction_events)
                            if item.event_id == append_id), None)
    if append_position is None or transaction_events[append_position] != event:
        raise ConsistencyError("原页APPEND不在其完整历史事务内")
    create_events = transaction_events[:append_position]
    create_type = _FILE_EVENT["id"]
    create_reason = _FILE_EVENT["branches"]["CREATE"]["reason"]
    page_entries = {entry.identity: entry for entry in saved.page.entries}
    file_ids = tuple(saved.file_ids)
    file_id_by_identity = dict(file_ids)
    created_event_by_file = {}
    expected_created = []
    for identity, file_id in file_ids:
        facts = row_facts(connection, "device_files", file_id)
        if facts is None or facts["identity_key"] != file_identity_key(
                action["device_id"], action["driver_id"], identity):
            raise ConsistencyError("原页文件ID与完整身份登记不符")
        created_event_by_file[file_id] = facts["created_event_id"]
        if transaction.first_event_id <= facts["created_event_id"] <= transaction.last_event_id:
            expected_created.append((identity, file_id, facts["created_event_id"]))
        elif facts["created_event_id"] >= transaction.first_event_id:
            raise ConsistencyError("原页文件首次发现身份越过事务末位")
    expected_created_ids = [(identity, file_id, created_id)
                            for identity, file_id, created_id in expected_created]
    actual_created = []
    for create in create_events:
        if ((create.event_type, create.reason) != (create_type, create_reason)
                or create.occurred_at != event.occurred_at or len(create.rows) != 1):
            raise ConsistencyError("结果页APPEND前包含非单行首次发现事件")
        row = create.rows[0]
        if row.table != "device_files" or row.before.exists or not row.after.exists:
            raise ConsistencyError("结果页首次发现事件改变了其他文件事实")
        try:
            identity_key = parse_exact_json(row.after.values["identity_key"])
        except (KeyError, TypeError, ValueError) as error:
            raise ConsistencyError("原首次发现身份不可解释") from error
        identity = identity_key[2] if isinstance(identity_key, list) and len(identity_key) == 3 else None
        file_id = file_id_by_identity.get(identity)
        entry = page_entries.get(identity)
        if file_id is None or entry is None or row.row_id != file_id:
            raise ConsistencyError("原首次发现不是APPEND的实际差集身份")
        command = FileObservationSave(action_id, identity, entry.locator, event.occurred_at,
            entry.original_name, entry.media_type)
        expected_row = file_discovery_row(command, file_id,
            file_identity_key(action["device_id"], action["driver_id"], identity))
        if dict(row.after.values) != dict(expected_row.after.values):
            raise ConsistencyError("原首次发现没有精确保存该页的原文件事实")
        if created_event_by_file[file_id] != create.event_id:
            raise ConsistencyError("原首次发现的持久文件创建事件身份不同")
        actual_created.append((identity, file_id, create.event_id))
    expected_create_order = [(identity, file_id) for identity, file_id, _ in expected_created_ids]
    if actual_created != [(identity, file_id, created_id) for identity, file_id, created_id in expected_created_ids]:
        raise ConsistencyError("结果页CREATE前缀没有精确覆盖本页首次发现")
    if [(identity, file_id) for identity, file_id, _ in actual_created] != expected_create_order:
        raise ConsistencyError("结果页CREATE前缀没有保持APPEND差集顺序")
    if any(item.event_id < append_id for item in transaction_events[:append_position]
           if item.event_type != create_type):
        raise ConsistencyError("结果页CREATE前缀混入其他事件")
    with closing(connection.execute(
            "SELECT id FROM history_events WHERE transaction_id=? AND event_type=?"
            " AND json_extract(body_json,'$.reason')=? ORDER BY id",
            (transaction.txn_id, _TYPE, _APPEND))) as cursor:
        append_rows = cursor.fetchmany(2)
    if len(append_rows) != 1 or append_rows[0][0] != append_id:
        raise ConsistencyError("原事务没有唯一RESULT_PAGE.APPEND")
    with closing(connection.execute(
            "SELECT id,transaction_id,event_type,event_version,occurred_at,clock_status,change_seq,body_json"
            " FROM history_events WHERE transaction_id=? AND event_type=?"
            " AND json_extract(body_json,'$.reason')=? ORDER BY id",
            (transaction.txn_id, _TYPE, _INDEX_FILES))) as cursor:
        index_rows = [decode_event_row(row) for row in cursor]
    with closing(connection.execute(
            "SELECT id,created_event_id,action_id,run_id,attempt_id,page_event_id,file_id,identity,"
            "family_directory,family_stem,family_role FROM result_page_files"
            " WHERE page_event_id=? ORDER BY id", (append_id,))) as cursor:
        relations = cursor.fetchmany(128)
        extra_relation = cursor.fetchone()
    if extra_relation is not None:
        raise ConsistencyError("原页INDEX_FILES关系超过单页容量")
    file_ids = tuple(saved.file_ids)
    if not file_ids:
        if index_rows or relations or transaction.last_event_id != append_id:
            raise ConsistencyError("零差集页包含关系或非规范尾事件")
        return
    if len(index_rows) != 1 or len(relations) != len(file_ids):
        raise ConsistencyError("原页与INDEX_FILES关系数量不完整或存在额外成员")
    index = index_rows[0]
    if (index.event_id != transaction.last_event_id or index.event_id != append_id + 1
            or index.occurred_at != event.occurred_at
            or index.evidence != {"result_page_index": {"page_event_id": append_id}}):
        raise ConsistencyError("原页INDEX_FILES身份、证据、时刻或事务位置不符")
    expected_rows = []
    for pair in file_ids:
        identity, file_id = pair
        expected_rows.append((
            index.event_id, action_id, run_id, attempt_id, append_id,
            file_id, identity, *_family_fields(
                identity, result_policy=action["execution_spec_json"].get("result_policy")
            )))
    relation_shape = [row[1:] for row in relations]
    if relation_shape != expected_rows:
        raise ConsistencyError("原页关系与实际APPEND差集身份、文件或族字段不符")
    if (len(index.rows) != len(expected_rows)
            or any(row.table != "result_page_files" or row.before.exists for row in index.rows)):
        raise ConsistencyError("INDEX_FILES行形状与原页关系不符")
    for relation, row in zip(relations, index.rows):
        relation_id = relation[0]
        expected = dict(zip(("created_event_id", "action_id", "run_id", "attempt_id",
                             "page_event_id", "file_id", "identity", "family_directory",
                             "family_stem", "family_role"), relation[1:]))
        if row.row_id != relation_id or dict(row.after.values) != expected:
            raise ConsistencyError("INDEX_FILES正文与持久关系行不符")


def _verify_scope_members(request,assessment,reader):
    """将正式关联与冻结范围的实际有序原片逐项对应，不能只比计数。"""
    selected=assessment.default_scope
    def expected():
        if selected.state is not DefaultScopeState.NONEMPTY:return
        setrow=storage._set(reader.connection,request.ref,fixed=True)
        cursor=None
        while True:
            page=storage.read_segment_members(request.ref,cursor,128,reader)
            for stored in page.items:
                member=stored.member
                if member.cursor is None or not selected.first_cursor<=member.cursor<=selected.last_cursor:continue
                candidate=storage._output_candidate(reader.connection,setrow,stored)
                if candidate is not None and candidate.kind is OutputKind.ORIGINAL:yield candidate.member_id
            if page.next_cursor is None:return
            cursor=page.next_cursor
    actual=(row for page in _scopes(request,reader) for row in page)
    count=0
    for member_id,row in zip_longest(expected(),actual):
        count+=1
        if row is None or member_id is None or row['member_id']!=member_id or row['ordinal']!=count:
            raise ConsistencyError('默认范围没有完整覆盖冻结选段的实际有序原片')
    if count!=(selected.member_count or 0):raise ConsistencyError('冻结选段计数与实际成员不同')


def _canonical_events(request,transaction,header,reader,qualification):
    """原冻结事实的唯一事件协议；每阶段只驻留当前候选/范围页。

    reader 的前序产物端口只承认实际已验证事件；恢复时使用原完整组。
    此生成器同时约束生产、必经 seal 和原键核实，不从实际待验证行生成预期。
    """
    db=reader.connection
    action,run,setrow,assessment=_identity(request,db)
    _verify_qualification_header(header,qualification)
    status,error,details=_failure(request,assessment,header['original_count'],setrow,db,before_event_id=transaction.first_event_id)
    if status!=header['action_status']:raise ConsistencyError('规范终态与原冻结判断不同')
    types=load_event_registry()['events']
    identity=transaction.first_event_id
    def envelope(name,reason,rows,evidence=None):
        return event_envelope(identity,transaction.txn_id,types[name]['id'],reason,tuple(rows),request.occurred_at,evidence)
    before={'status':qualification['action_status']};after={'status':status}
    if status==4:
        before.update(error_code=None,error_details_json=None)
        after.update(error_code=error,error_details_json=details)
    reason = types["ACTION_FINISHED"]["branches"][
        {3: "SUCCEED", 4: "FAIL", 6: "CANCEL"}[status]
    ]["reason"]
    yield envelope(
        "ACTION_FINISHED",
        reason,
        (update_change("actions", request.ref.action_id, before, after),),
        {"segment_finish_request": header},
    )
    identity+=1
    for template in qualification["preparation_events"]:
        yield replace(template, event_id=identity, transaction_id=transaction.txn_id)
        identity += 1

    def created(file_id,kind):
        resolver=getattr(reader,'segment_created_output',None)
        if resolver is not None:return resolver(transaction,request.ref.action_id,file_id,kind)
        return _created_output(db,transaction,request.ref.action_id,file_id,kind)
    output_id=header['first_output_id'];origin_id=header['first_origin_id']
    originals=previews=0
    preview_seen=False
    for wanted in (OutputKind.ORIGINAL,OutputKind.PREVIEW):
        if wanted is OutputKind.PREVIEW and not preview_seen:continue
        for candidate in _candidates(request,reader,header['batch_size']):
            if candidate.kind is OutputKind.PREVIEW:preview_seen=True
            if candidate.kind is not wanted:continue
            member=row_facts(db,'segment_members',candidate.member_id)
            file=storage._fixed_file(db,candidate.file_id,member['file_event_id'])
            from .capture import _unchecked_media
            values=dict(source_action_id=request.ref.action_id,kind=int(enum_for('outputs.kind')[wanted.name]),device_file_id=candidate.file_id,intermediate_file_id=None,
                original_name=file['original_name'],media_type=file['media_type'],availability=1,cleanup_status=1,cleanup_error_json=None,media_json=_unchecked_media(),error_json=None)
            rows=(row_change('outputs',output_id,values),)
            if wanted is OutputKind.PREVIEW:
                original=created(candidate.original_file_id,int(enum_for('outputs.kind').ORIGINAL))
                rows+=(row_change('output_origins',origin_id,dict(output_id=output_id,original_output_id=original)),)
                origin_id+=1;previews+=1
            else:originals+=1
            yield envelope('OUTPUT_REGISTERED',values['kind'],rows)
            identity+=1;output_id+=1
    count=0
    for page in _scopes(request,reader,header['batch_size']):
        if not page:continue
        bindings=[];rows=[]
        for row in page:
            member=row_facts(db,'segment_members',row['member_id'])
            output=created(member['file_id'],int(enum_for('outputs.kind').ORIGINAL))
            bindings.append((row['id'],output))
            rows.append(update_change('segment_scope_members',row['id'],{'output_id':None},{'output_id':output}))
            count+=1
        evidence={'segment_request':{'ref':document(request.ref),'input':storage._request_document(tuple(bindings))}}
        yield envelope('SEGMENT_FACTS',storage._EVENT['branches']['PUBLISH']['reason'],rows,evidence)
        identity+=1
    if (originals,previews,count)!=(header['original_count'],header['preview_count'],header['scope_count']):
        raise ConsistencyError('规范事件阶段没有耗尽原完整候选及范围计数')
    if qualification['finish_run']:
        evidence={'stale_run_request':{'kind':'stale_run_finish','responsibility_keys':[run['responsibility_key']],'status':3,'error':None}}
        yield envelope('OPERATION_CONFIGURED',3,(_run_finish_row(request,qualification),),evidence)
        identity+=1
    if qualification['complete_plan']:
        yield envelope('PLAN_STATUS_CHANGED',2,(update_change('plans',action['plan_id'],{'status':qualification['plan_status']},{'status':3}),))
        identity+=1
    if identity!=transaction.last_event_id:raise ConsistencyError('规范事件阶段与原完整L不同')
    yield envelope('SEGMENT_FACTS',storage._EVENT['branches']['ACTIVATE']['reason'],
        (update_change('segment_sets',setrow['id'],{'published_event_id':None},{'published_event_id':identity}),),
        {'segment_request':{'ref':document(request.ref),'input':None}})
