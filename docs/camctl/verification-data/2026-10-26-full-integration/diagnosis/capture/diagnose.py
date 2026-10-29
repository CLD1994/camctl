import ast, collections, datetime, hashlib, json, pathlib, re, subprocess

ROOT = pathlib.Path('/opt/devcontainer/.codex/worktrees/de38/camctl')
TASK = pathlib.Path('/tmp/camctl-integration-all-final-20261026-u19lkvoe/tasks/004-camctl-capture')
OUT = pathlib.Path('/tmp/camctl-full-272f-diagnosis/capture')
ART = TASK / 'artifacts'
lines = (ART / 'output.log').read_text().splitlines()
progress = json.loads((ART / 'progress.json').read_text())
assert progress['status'] == 'finished'
events = [json.loads(line) for line in (ART / 'events.jsonl').read_text().splitlines()]
failed_events = [e for e in events if e.get('outcome') == 'failed']
summary = []
for line in lines:
    if line.startswith(('FAILED ', 'ERROR ')):
        kind, nodeid = line.split(' ', 1)
        summary.append((kind, nodeid))
by_heading = {'.'.join(nodeid.split('::')[1:]): (kind, nodeid) for kind, nodeid in summary}
assert len(by_heading) == len(summary)
heads = []
for index, line in enumerate(lines):
    match = re.match(r'^_+ (.*?) _+$', line)
    if match and set(match.group(1)) != {'_', ' '}:
        heads.append((index, match.group(1)))
assert len(heads) == len(summary) == len(failed_events) == 1801

cache = {}
def data_at(side, path):
    key = side, path
    if key not in cache:
        if side == 'tested':
            cache[key] = (TASK / 'source' / path).read_bytes()
        else:
            result = subprocess.run(['git', 'show', side + ':' + path], cwd=ROOT, capture_output=True)
            cache[key] = result.stdout if result.returncode == 0 else None
    return cache[key]

def provenance(path):
    tested = data_at('tested', path)
    return {side: {'exists': data_at(side, path) is not None,
                   'equals_tested': data_at(side, path) == tested,
                   'sha256': hashlib.sha256(data_at(side, path)).hexdigest() if data_at(side, path) else None}
            for side in ['a7755d81', '272fda66']}

facts_to_read = [
    ('capture-runtime', 'apps/camctl/src/camctl/capture/handlers.py', 'CaptureRuntime'),
    ('runtime-fixture', 'apps/camctl/tests/integration/capture/test_capture_contract.py', '_runtime'),
    ('media-flow', 'apps/camctl/src/camctl/capture/media_flow.py', 'MediaFlow'),
    ('media-fixture', 'apps/camctl/tests/integration/capture/test_media_flow.py', '_flow'),
    ('work-file-context', 'apps/camctl/src/camctl/outputs/work_files.py', 'WorkFileContext'),
    ('copy-context', 'apps/camctl/src/camctl/outputs/copy.py', 'CopyContext'),
    ('accept-input', 'apps/camctl/src/camctl/acceptance/service.py', 'accept_input'),
    ('accept-plan-fixture', 'apps/camctl/tests/integration/scheduling/test_start_action.py', '_accept_plan'),
    ('read-session', 'apps/camctl/src/camctl/capture/media_flow.py', 'DriverReadSessions.open_session'),
    ('capture-factory', 'apps/camctl/src/camctl/bootstrap/capture_assembly.py', 'session_capture_assembly.factory'),
    ('photo-world-fixture', 'apps/camctl/tests/integration/capture/test_host_photo_flow.py', '_world'),
    ('cleanup-factory', 'apps/camctl/src/camctl/bootstrap/cleanup_assembly.py', 'session_cleanup_assembly.factory'),
    ('processing-port', 'apps/camctl/src/camctl/capture/media.py', 'ProcessingSaves'),
    ('synchronous-processing-fixture', 'apps/camctl/tests/integration/capture/test_media_execution.py', '_RepositorySaves'),
    ('input-copy', 'apps/camctl/src/camctl/capture/input_copy.py', 'obtain_recording_input'),
    ('input-copy-fixture', 'apps/camctl/tests/integration/capture/test_input_copy.py', '_Copies'),
    ('check-save-before-tools', 'apps/camctl/src/camctl/capture/media.py', '_execute_check'),
    ('repair-save-before-tools', 'apps/camctl/src/camctl/capture/media.py', '_repair_output'),
    ('grant-fixture', 'apps/camctl/tests/integration/capture/test_recording_start.py', 'StoreAdapter'),
    ('recording-start', 'apps/camctl/src/camctl/capture/recording.py', 'start_recording'),
    ('conclude-activity', 'apps/camctl/src/camctl/capture/handlers.py', '_conclude_activity'),
    ('action-guard', 'apps/camctl/src/camctl/persistence/repositories/capture.py', '_action_finish_guard'),
    ('action-guard-fixture', 'apps/camctl/tests/integration/capture/test_host_photo_finish_pair_boundary.py', 'test_action_finish_event_guard_rejects_host_spec_without_source_responsibilities'),
    ('result-owner', 'apps/camctl/src/camctl/capture/result_pages.py', 'ResultPageSaveOwner'),
    ('result-history-test', 'apps/camctl/tests/integration/capture/test_result_page_history.py', 'test_page_and_first_discovery_fault_reuses_original_key_after_reopen'),
]
def find_symbol(tree, qualified):
    node = tree
    for name in qualified.split('.'):
        matches = [child for child in node.body if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == name]
        assert len(matches) == 1, (qualified, name)
        node = matches[0]
    return node

def callable_meta(node):
    if isinstance(node, ast.ClassDef):
        return {'kind': 'class', 'annotated_fields': [child.target.id for child in node.body if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name)],
                'methods': {child.name: callable_meta(child) for child in node.body if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))}}
    return {'kind': 'async' if isinstance(node, ast.AsyncFunctionDef) else 'sync', 'arguments': ast.dump(node.args, include_attributes=False)}

facts = {}
for key, path, name in facts_to_read:
    sides = {}
    for side in ['tested', 'a7755d81', '272fda66']:
        raw = data_at(side, path)
        if raw is None:
            sides[side] = None
            continue
        source = raw.decode()
        node = find_symbol(ast.parse(source), name)
        sides[side] = {'line': node.lineno, 'end_line': node.end_lineno,
                       'ast_sha256': hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest(),
                       'interface': callable_meta(node),
                       'source': '\n'.join(source.splitlines()[node.lineno-1:node.end_lineno])}
    facts[key] = {'path': path, 'symbol': name, 'sides': sides,
                  'same_ast_both_parents': sides['a7755d81'] is not None and sides['272fda66'] is not None and sides['a7755d81']['ast_sha256'] == sides['272fda66']['ast_sha256']}
(OUT / 'interface-provenance.json').write_text(json.dumps(facts, ensure_ascii=False, indent=2) + '\n')

records = []
for index, (start, heading) in enumerate(heads):
    end = heads[index+1][0] if index+1 < len(heads) else next((i for i in range(start+1, len(lines)) if lines[i].startswith('=============================== warnings')), len(lines))
    name = heading.removeprefix('ERROR at setup of ')
    report_kind, nodeid = by_heading[name]
    block = lines[start:end]
    errors = [re.sub(r'^E\s*', '', line) for line in block if re.match(r'^E\s', line)]
    first = errors[0]
    normalized = re.sub(r'0x[0-9a-fA-F]+', '<address>', first)
    if 'OwnedConnection' in first and "has no attribute 'write'" in first:
        category, group = '旧同步OwnedConnection', '直接连接传给异步接纳入口'
        refs = ['accept-input', 'accept-plan-fixture']
    elif 'OwnedConnection' in first and "has no attribute 'read'" in first:
        category, group = '旧同步OwnedConnection', '直接连接传给异步读取会话'
        refs = ['read-session']
    elif 'CaptureRuntime.__init__' in first:
        category, group = '旧接口', 'CaptureRuntime仍传owned'
        refs = ['capture-runtime', 'runtime-fixture']
    elif 'MediaFlow.__init__' in first:
        category, group = '旧接口', 'MediaFlow仍传owned'
        refs = ['media-flow', 'media-fixture']
    elif 'WorkFileContext.__init__' in first:
        category, group = '旧接口', 'WorkFileContext仍传owned'
        refs = ['work-file-context']
    elif 'CopyContext.__init__' in first:
        category, group = '旧接口', 'CopyContext仍传owned'
        refs = ['copy-context']
    elif 'RuntimeWarning' in first:
        category, group = '旧接口', '优化解释器脚本未await异步_conclude_activity'
        refs = ['conclude-activity']
    elif 'coroutine' in first:
        category, group = '旧接口', '异步装配factory未await'
        refs = ['capture-factory', 'photo-world-fixture', 'cleanup-factory']
    elif 'SaveReceipt' in first:
        category, group = '旧接口', '业务保存适配器仍同步返回SaveReceipt'
        refs = ['processing-port', 'synchronous-processing-fixture', 'input-copy', 'input-copy-fixture']
    elif 'GrantDecision' in first:
        category, group = '旧接口', '录像授予适配器仍同步返回GrantDecision'
        refs = ['grant-fixture', 'recording-start']
    elif 'event_type' in first:
        category, group = '旧接口', '伪造事件缺少真实event_type和reason字段'
        refs = ['action-guard', 'action-guard-fixture']
    elif first == 'asyncio.exceptions.CancelledError':
        category = '旧接口'
        if 'test_recovery_does_not_end_control' in name:
            group = '旧异步factory使设备入口未开始而门闩超时'
            refs = ['capture-factory', 'photo-world-fixture']
        else:
            group = '同步媒体保存适配器使工具未开始而门闩超时'
            refs = ['processing-port', 'synchronous-processing-fixture', 'check-save-before-tools', 'repair-save-before-tools']
    elif first.startswith('AssertionError: OperationalError'):
        category, group = 'merge接缝', '重开后的原结果页恢复仍调用save'
        refs = ['result-owner', 'result-history-test']
    else:
        category, group, refs = '生产断言问题待核实', '未归类', []
    test_path = nodeid.split('::')[0]
    records.append({'nodeid': nodeid, 'report_kind': report_kind, 'phase': 'setup' if report_kind == 'ERROR' else 'call',
                    'category': category, 'first_cause_group': group, 'first_trace_exception': first, 'all_trace_error_lines': errors,
                    'normalized_first_trace_exception': normalized,
                    'trace_artifact': str(ART/'output.log'), 'trace_line_start': start+1, 'trace_line_end': end,
                    'source_proof_ids': refs, 'test_file_provenance': provenance(test_path),
                    'introduced_by_this_merge': category == 'merge接缝',
                    'inference_note': '等待门闩超时是堆栈直接事实；旧接口阻断门闩由所引生产前置await与测试适配器源码推导，未新增执行。' if first == 'asyncio.exceptions.CancelledError' else None})
assert {r['nodeid'].removeprefix('apps/camctl/') for r in records} == {e['nodeid'] for e in failed_events}
(OUT/'failures.json').write_text(json.dumps(records, ensure_ascii=False, indent=2)+'\n')
with (OUT/'failures.tsv').open('w') as f:
    f.write('nodeid\tphase\tcategory\tfirst_cause_group\ttrace_line_start\ttrace_line_end\tfirst_trace_exception\n')
    for r in records:
        f.write('\t'.join(str(r[k]).replace('\t',' ').replace('\n',' ') for k in ['nodeid','phase','category','first_cause_group','trace_line_start','trace_line_end','first_trace_exception'])+'\n')
counts = collections.Counter(r['category'] for r in records)
counts.setdefault('生产断言问题', 0)
groups = collections.Counter(r['first_cause_group'] for r in records)
exceptions = collections.Counter(r['normalized_first_trace_exception'] for r in records)
result = {'created_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'complete': True,
          'task_progress': progress, 'pytest_final_summary': next(line for line in reversed(lines) if re.match(r'^\d+ failed,',line)),
          'counts': dict(counts), 'first_cause_group_counts': dict(groups), 'exception_counts': dict(exceptions),
          'all_failed_phase_reports': len(failed_events), 'all_trace_blocks': len(heads), 'all_failure_summary_nodes': len(summary),
          'static_comparison': ['a7755d81','272fda66'], 'tested_snapshot': json.loads((ART/'command.json').read_text())['source'],
          'original_log_sha256': hashlib.sha256((ART/'output.log').read_bytes()).hexdigest(),
          'events_sha256': hashlib.sha256((ART/'events.jsonl').read_bytes()).hexdigest(),
          'limitations': ['未重跑旧夹具失败项；旧接口归因由本次完整堆栈及两父提交源码共同支持。', '五项门闩超时的被压制后台异常未在原日志交付，旧接口阻断由静态前置路径证明，逐项单列推导。', '本结果只诊断最先失败边界；尚未到达的后续业务断言不能据此认为通过。'],
          'merge_adapter_fix': {'commit': 'c8f50082', 'base':'9fbb734c', 'nodes':3, 'result':'3 passed in 3.83s', 'evidence':'result-page-retry/receipt.json'}}
test_files = {r['nodeid'].split('::')[0]: r['test_file_provenance'] for r in records}
result['failed_test_file_count'] = len(test_files)
result['failed_test_file_provenance_counts'] = dict(collections.Counter(
    'both' if all(value['equals_tested'] for value in sides.values()) else
    'a7755d81' if sides['a7755d81']['equals_tested'] else
    '272fda66' if sides['272fda66']['equals_tested'] else 'neither'
    for sides in test_files.values()))
runtime_fields = [facts['capture-runtime']['sides'][side]['interface']['annotated_fields']
                  for side in ['tested', 'a7755d81', '272fda66']]
assert runtime_fields[0] == runtime_fields[1] == runtime_fields[2]
assert all('database' in fields and 'owned' not in fields for fields in runtime_fields)
result['capture_runtime_constructor_fields_equal_in_tested_and_both_parents'] = True
qualify_statements = []
for side in ['tested', 'a7755d81', '272fda66']:
    source = data_at(side, 'apps/camctl/src/camctl/capture/input_copy.py').decode()
    function = find_symbol(ast.parse(source), 'obtain_recording_input')
    statement = next(node for node in function.body if isinstance(node, ast.Assign))
    qualify_statements.append(ast.dump(statement, include_attributes=False))
assert qualify_statements[0] == qualify_statements[1] == qualify_statements[2]
result['input_copy_first_qualify_await_equal_in_tested_and_both_parents'] = True
(OUT/'summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
print(json.dumps({k:result[k] for k in ['pytest_final_summary','counts','first_cause_group_counts','all_trace_blocks']},ensure_ascii=False,indent=2))
print('parent symbol ast equality:', json.dumps({k:v['same_ast_both_parents'] for k,v in facts.items()},ensure_ascii=False))
