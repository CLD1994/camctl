"""Read-only audit of saved Action6 history and production discovery.

This does not call cleanup or run/submit, and it opens every original database
with mode=ro and query_only. The schedule assumption is explicit in the output.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

SOURCE = Path('/tmp/camctl-failure-diagnosis-20261025')
from camctl.bootstrap.work_file_assembly import WorkFileRuntime
import camctl
from camctl.outputs.work_files import WorkFileLimits, classify_work_file, CleanupScan
from camctl.outputs.segment_copy_reuse import bound_waiters_at_publication, _publication_boundary, _current_boundary
from camctl.persistence.runtime import OwnedConnection, DatabaseMetadata
from camctl.persistence.row_history import read_row_values_at_boundary
from camctl.persistence.transaction import row_facts

ROOTS = {
    'historical': Path('/tmp/camctl-itopt-final-20261025/tasks/018-root-test_action6_segmented_recovery_roundtrip-shard-4/tmp/pytest/test_installed_action6_ordinar1/action6-record'),
    'current': Path('/tmp/camctl-44-diagnostic-current-20261025/tasks/004-root-test_action6_segmented_recovery_roundtrip/tmp/pytest/test_installed_action6_ordinar0/action6-record'),
}

def dict_rows(connection, sql, args=()):
    cur = connection.execute(sql, args)
    keys = [x[0] for x in cur.description]
    return [dict(zip(keys, row)) for row in cur.fetchall()]

def inspect(label, root):
    db = root / 'deployment/state.db'
    con = sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)
    con.execute('PRAGMA query_only=ON')
    meta = con.execute('SELECT application_id,instance_id,format_version,staging_path,ready_path,processing_path FROM database_metadata').fetchone()
    owned = OwnedConnection(con, DatabaseMetadata(*meta))
    result = {'world': str(root), 'database': str(db), 'readonly': con.execute('PRAGMA query_only').fetchone()[0] == 1}
    result['actions'] = dict_rows(con, 'SELECT id,name,status,created_event_id,last_event_id FROM actions ORDER BY id')
    published = con.execute('SELECT published_event_id FROM segment_sets WHERE action_id=1 AND published_event_id IS NOT NULL').fetchone()[0]
    con.execute('BEGIN')
    boundary = _publication_boundary(con, published)
    current = _current_boundary(con)
    result['publication'] = {'transaction_id': boundary.txn_id, 'event_id': boundary.last_event_id}
    waiters = list(bound_waiters_at_publication(1, published, owned))
    result['bound_waiters_at_h'] = []
    for w in waiters:
        a = read_row_values_at_boundary(con, owner=('action', w.action_id), table='actions', row_id=w.action_id, columns=frozenset({'status'}), current_values=row_facts(con, 'actions', w.action_id), boundary=boundary, current_boundary=current)
        s = read_row_values_at_boundary(con, owner=('action', w.action_id), table='obtain_source_selections', row_id=w.selection_id, columns=frozenset({'status'}), current_values=row_facts(con, 'obtain_source_selections', w.selection_id), boundary=boundary, current_boundary=current)
        result['bound_waiters_at_h'].append({'action_id': w.action_id, 'selection_id': w.selection_id, 'action_status_at_h': a['status'], 'selection_status_at_h': s['status']})
    con.commit()
    result['inputs'] = dict_rows(con, 'SELECT id,owner_action_id,purpose,relative_path,retention_state,cleanup_state,created_event_id,last_event_id FROM intermediate_files WHERE owner_action_id=1 ORDER BY id')
    result['inspections'] = dict_rows(con, 'SELECT id,action_id,member_id,state,result_event_id,result_json FROM segment_inspections ORDER BY id')
    result['copies'] = dict_rows(con, 'SELECT id,inspection_id,delivery_id,source_device_file_id,source_intermediate_file_id,target_file_id,committed_bytes,source_size,slot_device_id,verification_state FROM file_copies ORDER BY id')
    result['running_file_attempts'] = dict_rows(con, 'SELECT a.id,a.status,r.id run_id,r.copy_id FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id JOIN file_copies c ON c.id=r.copy_id WHERE a.status=1')
    result['runtime'] = dict_rows(con, 'SELECT cleanup_cursor_file_id FROM runtime_state')
    lifecycle=[]
    for event_id, txn, et, at, body in con.execute('SELECT id,transaction_id,event_type,occurred_at,body_json FROM history_events ORDER BY id'):
        for row in json.loads(body).get('rows', []):
            if row['table']=='intermediate_files' and row['id'] <= 3 or row['table']=='actions' and row['id'] <= 3 and 'status' in row['after']['values']:
                lifecycle.append({'event_id':event_id,'transaction_id':txn,'event_type':et,'occurred_at':at,'row':row})
    result['lifecycle'] = lifecycle
    result['file_exists'] = [{'id':row['id'],'exists':(Path(meta[3])/row['relative_path']).exists()} for row in result['inputs']]
    logs = root / 'cli-processes.jsonl'
    result['run_processes'] = [v for line in logs.read_text().splitlines() if (v:=json.loads(line)).get('command',[None])[0]=='run']
    result['module_log'] = [{'line':i,'text':line} for i,line in enumerate((root/'module.log').read_text().splitlines(),1) if 'command=run' in line or 'needs_run=' in line]
    if label == 'historical':
        runtime = WorkFileRuntime(staging=Path(meta[3]), limits=WorkFileLimits(batch_size=32, limit_per_run=128))
        # Legal same-run schedule: source result H has been consumed while both
        # H waiters were RUNNING. The preserved final DB supplies the exact later
        # production rows/events. No row or event is synthesized or edited.
        runtime._discovery_after = published
        runtime.history.scan = CleanupScan(start_after=0, after_id=0, ceiling=0, remaining=128, batch_size=32)
        before = {'first_files': sorted(runtime._first_files), 'processed': dict(runtime.processed), 'discovery_after':runtime._discovery_after}
        runtime._discover(owned)
        discovered = {'first_files': sorted(runtime._first_files), 'processed': dict(runtime.processed), 'discovery_after':runtime._discovery_after}
        input_ids = {row['id'] for row in result['inputs']}
        result['conditional_discovery_probe'] = {'schedule_assumption':'H was consumed while bound waiters were RUNNING; no actual cleanup attempt was made. Same-run inputs were outside initial historical ceiling 0.', 'before':before, 'after_waiter_events':discovered, 'missed_inputs':sorted(input_ids-runtime._first_files)}
        result['final_production_classification'] = [{'file_id':fid,'facts':vars(runtime.repository.load_work_file_state(fid, owned)), 'decision':classify_work_file(runtime.repository.load_work_file_state(fid, owned)).action.value} for fid in sorted(input_ids)]
        runtime._add_owned_files(owned, 'owner_action_id', 1)
        result['conditional_discovery_probe']['after_explicit_source_rediscovery'] = {'first_files':sorted(runtime._first_files),'processed':dict(runtime.processed)}
        assert result['conditional_discovery_probe']['missed_inputs'] == sorted(input_ids)
        assert all(x['decision']=='delete' for x in result['final_production_classification'])
        assert input_ids <= runtime._first_files
        assert runtime.processed == {}
    con.close()
    return result

async def main():
    result={'interpreter':sys.version,'source':str(SOURCE),'scope':'read-only real original databases; production discovery functions; no cleanup/submit/run', 'worlds':{k:inspect(k,p) for k,p in ROOTS.items()}}
    files=['apps/camctl/src/camctl/bootstrap/work_file_assembly.py','apps/camctl/src/camctl/outputs/work_files.py','apps/camctl/src/camctl/outputs/segment_copy_reuse.py','apps/camctl/src/camctl/persistence/repositories/outputs.py','tests/integration/test_action6_segmented_recovery_roundtrip.py']
    roots={'diagnostic':SOURCE,'checkout':Path('/opt/devcontainer/.codex/worktrees/de38/camctl')}
    result['source_sha256']={f:{name:hashlib.sha256((p/f).read_bytes()).hexdigest() for name,p in roots.items()} for f in files}
    result['loaded_package']=str(Path(camctl.__file__).parent)
    result['loaded_package_sha256']={f:hashlib.sha256((Path(camctl.__file__).parent / f.removeprefix('apps/camctl/src/camctl/')).read_bytes()).hexdigest() for f in files if f.startswith('apps/camctl/src/camctl/')}
    assert all(result['loaded_package_sha256'][f] == result['source_sha256'][f]['diagnostic'] for f in result['loaded_package_sha256'])
    dest=Path('/tmp/camctl-contract-cleanup-probe.json')
    dest.write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str)+'\n')
    print(json.dumps({'result':str(dest),'historical_probe':result['worlds']['historical']['conditional_discovery_probe'],'source_equal':{f:len(set(v.values()))==1 for f,v in result['source_sha256'].items()}},ensure_ascii=False,indent=2))

asyncio.run(main())
