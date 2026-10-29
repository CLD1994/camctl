import hashlib
import json
import pathlib
import sqlite3
import sys
from types import SimpleNamespace

source = pathlib.Path('/tmp/camctl-44-diagnostic-current-20261025/tasks/004-root-test_action6_segmented_recovery_roundtrip/source')
sys.path.insert(0, str(source / 'apps/camctl/src'))
from camctl.capture.media_flow import resume_binding_internal_read_ends
from camctl.devices.read_session import ReadEnd
from camctl.operations.models import AttemptTicket
from camctl.outputs.read_attempts import hold_read_end

world = pathlib.Path('/tmp/camctl-44-diagnostic-current-20261025/tasks/004-root-test_action6_segmented_recovery_roundtrip/tmp/pytest/test_installed_action6_native_0/action6-native')
database = world / 'deployment/state.db'
connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
connection.row_factory = sqlite3.Row
connection.execute('PRAGMA query_only=ON')
rows = list(connection.execute('''
SELECT c.id AS copy_id,c.delivery_id,c.processing_id,c.inspection_id,c.source_device_file_id,
 c.committed_bytes,c.source_size,c.source_sha256,c.target_sha256,c.verification_state,
 s.action_id,s.set_id,s.member_id,s.source_device_file_id AS inspection_source,
 m.set_id AS member_set,m.file_id AS member_source,f.source_action_id,f.observer_action_id,
 a.device_id,a.driver_id,a.status AS action_status,ob.device_id AS observer_device,ob.driver_id AS observer_driver,
 r.id AS run_id,r.responsibility_key,r.kind,r.status AS run_status,r.attempts_used,
 t.attempt_no,t.status AS attempt_status,t.result_json,e.occurred_at AS finish_at
FROM file_copies c
 JOIN segment_inspections s ON s.id=c.inspection_id
 JOIN segment_members m ON m.id=s.member_id
 JOIN device_files f ON f.id=c.source_device_file_id
 JOIN actions a ON a.id=s.action_id
 JOIN actions ob ON ob.id=f.observer_action_id
 JOIN operation_runs r ON r.copy_id=c.id
 JOIN operation_attempts t ON t.run_id=r.id
 LEFT JOIN history_events e ON e.id=t.result_event_id
ORDER BY c.id,t.attempt_no
'''))
results=[]
statements=[]
connection.set_trace_callback(statements.append)
for row in rows:
    calls=[]
    ticket=AttemptTicket(row['attempt_no'], 'read', str(row['copy_id']), row['responsibility_key'], row['run_id'])
    marker=ReadEnd(stopped=True, bytes_read=row['source_size'], error=None)
    runtime=SimpleNamespace(pending_read_ends={}, occurred_at=lambda: row['finish_at'])
    async def resume(current, held):
        raise AssertionError('This probe must stop before a continuation is invoked')
    hold_read_end(runtime,ticket,marker,True,resume=resume)
    before_count=connection.total_changes
    try:
        resume_binding_internal_read_ends(SimpleNamespace(connection=connection),
            pending_read_results={},pending_read_business={},pending_read_ends=runtime.pending_read_ends,
            continuing_read_tickets={},binding_check=lambda binding: calls.append(binding),occurred_at=lambda: row['finish_at'])
    except Exception as error:
        outcome={'class':type(error).__name__,'message':str(error)}
    else:
        outcome={'class':None,'message':None}
    results.append({'copy_id':row['copy_id'],'binding_check_calls':len(calls),
                    'database_changes':connection.total_changes-before_count,'outcome':outcome})
connection.set_trace_callback(None)
fixed=pathlib.Path('/tmp/camctl-failure-diagnosis-20261025')
workspace=pathlib.Path('/opt/devcontainer/.codex/worktrees/de38/camctl')
files=['apps/camctl/src/camctl/capture/media_flow.py','apps/camctl/src/camctl/capture/segment_inspections.py','apps/camctl/src/camctl/outputs/read_attempts.py','docs/camctl/database/schema/files.sql']
identities=[]
for name in files:
    hashes={label:hashlib.sha256((root/name).read_bytes()).hexdigest() for label,root in [('diagnostic',fixed),('executed_snapshot',source),('workspace',workspace)]}
    identities.append({'file':name,'sha256':hashes,'all_equal':len(set(hashes.values()))==1})
report={'python':sys.version,'sqlite':sqlite3.sqlite_version,'source':str(source),'database':str(database),
 'scope':'Read-only branch probe. HeldReadEnd markers are constructed through production hold_read_end from real saved copy/ticket identities, not recovered original in-memory objects. Final database already contains terminal source/read results. This proves the unconditional query exclusion before status/binding checks; it is not a replay of original runtime interleaving.',
 'query_only':connection.execute('PRAGMA query_only').fetchone()[0],
 'foreign_key_check':[dict(x) for x in connection.execute('PRAGMA foreign_key_check')],
 'copies':[dict(row) for row in rows], 'branch_probes':results,'traced_statements':statements,
 'source_identity':identities,'total_changes':connection.total_changes,'in_transaction':connection.in_transaction}
pathlib.Path('/tmp/camctl-contract-read-probe.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({key:report[key] for key in ['python','sqlite','query_only','foreign_key_check','branch_probes','source_identity','total_changes','in_transaction']},ensure_ascii=False,indent=2))
connection.close()
