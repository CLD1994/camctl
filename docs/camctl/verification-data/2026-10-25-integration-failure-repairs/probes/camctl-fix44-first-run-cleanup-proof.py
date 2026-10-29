"""只读核对最终联合测试的输入清理属于首个 run，而非后续补救。"""
from __future__ import annotations
import hashlib
import json
import sqlite3
from pathlib import Path

repo=Path('/opt/devcontainer/.codex/worktrees/de38/camctl')
root=Path('/tmp/camctl-fix44-verified-20261025')
worlds=list(root.glob('tasks/*/tmp/pytest/test_installed_action6_ordinar*/action6-record'))
worlds+=list(root.glob('tasks/*/tmp/pytest/test_installed_action6_native_*/action6-native'))
worlds=sorted({p.resolve() for p in worlds})
assert len(worlds)==2,worlds
proofs=[]
for world in worlds:
    processes=[json.loads(line) for line in (world/'cli-processes.jsonl').read_text().splitlines()]
    runs=[p for p in processes if p.get('stage')=='started' and p.get('command',[None])[0]=='run']
    assert runs,world
    first=runs[0]
    ended=[p for p in processes if p.get('stage')=='finished' and p.get('pid')==first['pid']]
    assert len(ended)==1,(world,ended)
    assert ended[0]['exitcode']==0,(world,ended)
    # Stage/return fields are copied verbatim so the original process facts remain reviewable.
    clock=[json.loads(line) for line in (world/'clock-reads.jsonl').read_text().splitlines()]
    clock=[p for p in clock if p['pid']==first['pid']]
    assert clock,world
    lower=min(p['utc_us'] for p in clock)
    upper=max(p['utc_us'] for p in clock)
    package=Path(first['import_origins']['camctl']).parent
    registry=json.loads((package/'_resources/registry/enum-registry.json').read_text())
    purpose=registry['enums']['intermediate_files.purpose']['members']['RECORDING_INPUT']
    released=registry['enums']['intermediate_files.retention_state']['members']['RELEASABLE']
    completed=registry['enums']['intermediate_files.cleanup_state']['members']['COMPLETED']
    con=sqlite3.connect((world/'deployment/state.db').as_uri()+'?mode=ro',uri=True)
    con.execute('PRAGMA query_only=ON')
    con.row_factory=sqlite3.Row
    staging=Path(con.execute('SELECT staging_path FROM database_metadata').fetchone()[0])
    inputs=[dict(r) for r in con.execute('''
      SELECT f.id,f.relative_path,f.retention_state,f.cleanup_state,e.id AS event_id,e.occurred_at
      FROM intermediate_files f JOIN history_events e ON e.id=f.last_event_id
      WHERE f.owner_action_id=1 AND f.purpose=? ORDER BY f.id
    ''',(purpose,))]
    assert len(inputs)==3,(world,inputs)
    for row in inputs:
        assert lower<=row['occurred_at']<=upper,(world,row,lower,upper)
    source=next(world.parents[i]/'source' for i in range(len(world.parents)) if (world.parents[i]/'source').is_dir())
    files=['bootstrap/work_file_assembly.py','capture/media_flow.py','capture/segment_inspections.py','outputs/segment_copy_reuse.py','persistence/repositories/outputs.py']
    package=Path(first['import_origins']['camctl']).parent
    identities=[]
    for file in files:
        expected=(source/'apps/camctl/src/camctl'/file).read_bytes()
        actual=(package/file).read_bytes()
        assert actual==expected,file
        identities.append({'file':file,'sha256':hashlib.sha256(actual).hexdigest()})
    for row in inputs:
        assert (row['retention_state'],row['cleanup_state'])==(released,completed),row
        assert not (staging/row['relative_path']).exists(),row
    proofs.append({'world':str(world),'first_run_started':first,'first_run_finished':ended[0],
      'first_run_utc_interval':[lower,upper],'cleanup_events':inputs,'installed_source_identity':identities,
      'database_read_only':con.execute('PRAGMA query_only').fetchone()[0]==1,'total_changes':con.total_changes})
    con.close()
output=repo/'docs/camctl/verification-data/2026-10-25-integration-failure-repairs/first-run-cleanup-proof.json'
output.parent.mkdir(parents=True,exist_ok=True)
output.write_text(json.dumps({'scope':'两个目标用例的首次run清理归属；不重建历史失败游标，不代替确定性交错回归','worlds':proofs},ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'worlds':len(proofs),'all_input_cleanup_in_first_run':True},ensure_ascii=False))
