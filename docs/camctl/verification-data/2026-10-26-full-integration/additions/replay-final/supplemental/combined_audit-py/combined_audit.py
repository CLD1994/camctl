from pathlib import Path
from collections import Counter
from datetime import datetime, timezone
import json

OUT=Path('/tmp/camctl-full-272f-audit')
MAIN=Path('/tmp/camctl-integration-all-final-20261026-u19lkvoe')
REPLAY=Path('/tmp/camctl-integration-replay-20261026-u19lkvoe')

def read(p): return json.loads(p.read_text())
def canonical(node,scope):
    file,marker,name=node.partition('::')
    if scope=='camctl':
        prefix='apps/camctl/tests/integration/'
        if prefix in file: file=file[file.rfind(prefix):]
        elif 'tests/integration/' in file:file='apps/camctl/'+file[file.rfind('tests/integration/'):]
    elif 'tests/integration/' in file:file=file[file.rfind('tests/integration/'):]
    return file+marker+name

def complete(case):
    found=set(case.get('phases',{}))
    setup=case.get('phases',{}).get('setup',{}).get('outcome')
    needed={'setup','teardown'}|({'call'} if setup=='passed' else set())
    return not (needed-found)

audits=[('main',MAIN,OUT),('replay',REPLAY,OUT/'replay')]
expected=set();inventories={};histories={};versions=[]
for label,root,auditdir in audits:
    a=read(auditdir/'coverage-audit.json')
    if a['runner_status']!='finished':raise RuntimeError(f'{label} is not terminal')
    inv=read(auditdir/'canonical-inventory.json');inventories[label]=set(inv);expected.update(inv)
    for p in root.glob('collection/*/artifacts/inventory.json'):
        expected.update(canonical(n,'root') for n in read(p))
    for node,case in read(auditdir/'canonical-cases.json').items():
        histories.setdefault(node,[]).append({'run':label,'source_head':a['source_head'],'outcome':case['outcome'],'complete_phases':complete(case),'case':case})
    versions.append({k:a[k] for k in ('source_head','snapshot_sha256','snapshot_files_unchanged','runner_status','runner_exit_code','task_states','pytest_inventory_count','pytest_cases_count','pytest_outcomes','phase_counts','subchecks_from_events','node_counts') }|{'run':label,'root':str(root),'coverage_audit':str(auditdir/'coverage-audit.json'),'resource_audit':str(auditdir/'resource-audit.json')})

completed={n for n,rows in histories.items() if any(r['complete_phases'] for r in rows)}
latest={n:next((r for r in reversed(rows) if r['complete_phases']),rows[-1]) for n,rows in histories.items()}
selection=read(Path('/tmp/camctl-full-272f-session-replay-selection.json'))
session_completed={canonical(n,'camctl') for n in selection['completed_in_first']}
session_remaining={canonical(n,'camctl') for n in selection['remaining_targets']}
replayed_original_session_completed=sorted(session_completed&inventories['replay'])
capture={n:rows for n,rows in histories.items() if 'capture/test_result_page_history.py::test_page_and_first_discovery_fault_reuses_original_key_after_reopen[' in n}
hb_expected={f'tests/integration/test_hasselblad_photo_roundtrip.py::test_installed_hasselblad_photo_roundtrip[{fmt}-{source}-{shutter}]' for fmt in ('raw','jpeg') for source in ('host','device') for shutter in ('0.000667','0.000333','0.00025')}
hb={n:[{k:r[k] for k in ('run','source_head','outcome','complete_phases')} for r in histories.get(n,[])] for n in sorted(hb_expected)}
capacity_fragments=('test_large_growth_crosses_page_and_disk_cache_capacity','test_large_window_releases_overflow_once_and_keyset_pages_do_not_rescan_window','test_member_growth_restore_cost','test_event_growth_restore_pages_cost','test_unrelated_growth_keeps_target_restore_stable','test_fixed_batch_write_and_verification_have_bounded_residency','test_fixed_batch_reads_and_output_identity_lookup_keep_their_bounds')
capacity={f:{n:[{k:r[k] for k in ('run','source_head','outcome','complete_phases')} for r in histories.get(n,[])] for n in sorted(expected) if n.partition('::')[2].split('[')[0]==f} for f in capacity_fragments}
report={'audited_at':datetime.now(timezone.utc).isoformat(),'interpretation':'Two frozen test snapshots with identical four production scope trees; outcomes retain source identity. This is coverage reconciliation, not a single-source all-pass claim.','versions':versions,'expected_collectable_unique_pytest_nodes':len(expected),'recorded_unique_pytest_nodes':len(histories),'complete_unique_pytest_nodes':len(completed),'expected_without_any_case':sorted(expected-set(histories)),'expected_without_complete_phases':sorted(expected-completed),'unexpected_recorded_nodes':sorted(set(histories)-expected),'recorded_case_occurrences':sum(len(v) for v in histories.values()),'complete_case_occurrences':sum(r['complete_phases'] for rows in histories.values() for r in rows),'latest_complete_or_partial_outcomes':dict(Counter(r['outcome'] for r in latest.values())),'repeated_nodes':{n:[{k:r[k] for k in ('run','source_head','outcome','complete_phases')} for r in rows] for n,rows in histories.items() if len(rows)>1},'partial_cases':{n:[{k:r[k] for k in ('run','source_head','outcome','complete_phases')} for r in rows if not r['complete_phases']] for n,rows in histories.items() if any(not r['complete_phases'] for r in rows)},'session_completed_in_main':len(session_completed),'session_remaining_for_replay':len(session_remaining),'original_completed_session_repeated_in_replay':replayed_original_session_completed,'remaining_session_absent_from_replay_inventory':sorted(session_remaining-inventories['replay']),'remaining_session_incomplete_after_replay':sorted(session_remaining-completed),'capture_three_repair_histories':capture,'hb_full_12':hb,'hb_full_12_replay_passed':all(any(r['run']=='replay' and r['outcome']=='passed' and r['complete_phases'] for r in rows) for rows in hb.values()),'capacity_checks':capacity,'uncollected_obsolete_files':[{'path':'apps/camctl/tests/integration/persistence/test_page_read_cache.py','reason':'Imports removed camctl.persistence.page_read_cache; original main collection failed; replay excluded this obsolete file.','unknown_test_count':True}],'interruption_receipt':'/tmp/camctl-full-272f-session-interrupt.json','production_identity_receipt':str(OUT/'production-identity.json')}
(OUT/'combined-coverage-audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
(OUT/'combined-case-histories.json').write_text(json.dumps(histories,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({k:report[k] for k in ('expected_collectable_unique_pytest_nodes','recorded_unique_pytest_nodes','complete_unique_pytest_nodes','expected_without_any_case','expected_without_complete_phases','unexpected_recorded_nodes','recorded_case_occurrences','complete_case_occurrences','latest_complete_or_partial_outcomes','session_completed_in_main','session_remaining_for_replay','original_completed_session_repeated_in_replay','remaining_session_absent_from_replay_inventory','remaining_session_incomplete_after_replay','hb_full_12_replay_passed')},ensure_ascii=False,indent=2))
