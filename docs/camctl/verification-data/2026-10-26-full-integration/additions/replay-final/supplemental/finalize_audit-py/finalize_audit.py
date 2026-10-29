from pathlib import Path
from collections import Counter
from datetime import datetime, timezone
import hashlib,json

B=Path('/tmp/camctl-full-272f-audit')
read=lambda p:json.loads(p.read_text())
write=lambda p,v:p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n')
cov=read(B/'combined-coverage-audit.json')
hist=read(B/'combined-case-histories.json')
narrow=read(B/'final-narrow-audit.json')
res={label:read(path) for label,path in [('main',B/'resource-audit.json'),('replay',B/'replay/resource-audit.json')]}
now=datetime.now(timezone.utc).isoformat()
latest={n:next((r for r in reversed(rows) if r['complete_phases']),rows[-1]) for n,rows in hist.items()}
phase=Counter();setup=Counter()
for row in latest.values():
    phase.update(row['case']['phases'].keys())
    setup.update([row['case']['phases']['setup']['outcome']])
run_roots={'main':Path('/tmp/camctl-integration-all-final-20261026-u19lkvoe'),'replay':Path('/tmp/camctl-integration-replay-20261026-u19lkvoe')}
launchers=[]
for label,root in run_roots.items():
    path=Path(str(root)+'-budget/launcher.json');s=read(path)
    launchers.append({'run':label,'path':str(path),'requested_limit_bytes':s['limit_bytes'],'status':s['status'],'exit_code':s['exit_code'],'delegation_path':s['delegation_path'],'delegation_exists':Path(s['delegation_path']).exists(),'removal':s['removal'],'raw_groups':s['groups']})
pending_groups=[i for i in res['replay']['receipts'] if 'evidence' not in i['receipt']]
resource={
    'audited_at':now,
    'business_instances':{
        'unique_groups':sum(s['business_2gib']['unique_groups'] for s in res.values()),
        'finalized_evidence_groups':sum(s['business_2gib']['finalized_evidence_groups'] for s in res.values()),
        'pending_without_final_evidence_groups':len(pending_groups),
        'actual_memory_and_memory_plus_swap_limits_all_2gib':all(s['business_2gib']['actual_memory_and_combined_limits_match'] for s in res.values()),
        'observed_finalized_combined_peak_max_bytes':max(s['business_2gib']['combined_peak_max_bytes'] for s in res.values()),
        'finalized_evidence_events':{k:sum(s['business_2gib']['events'][k] for s in res.values()) for k in ('oom','oom_kill','oom_group_kill','failcnt','memsw_failcnt')},
        'all_actual_groups_absent':all(s['business_2gib']['all_actual_groups_absent'] for s in res.values()),
        'all_known_owned_pids_absent':all(not s['live_known_pids'] for s in res.values()),
        'all_direct_argument_workload_candidates_absent':all(not s['live_workload_candidates'] for s in res.values()),
        'complete_resource_acceptance':False,
    },
    'runs':{label:{'audit':str(B/('resource-audit.json' if label=='main' else 'replay/resource-audit.json')),'raw_receipt_paths':s['raw_receipt_paths'],'unique_groups_including_calibration':s['unique_groups'],'business_2gib':s['business_2gib'],'calibration_64mib':s['calibration_64mib'],'bad_json':s['bad_json'],'groups_still_present':s['groups_still_present'],'live_known_pids':s['live_known_pids'],'live_workload_candidates':s['live_workload_candidates']} for label,s in res.items()},
    'launchers':launchers,
    'delegation_parent_explanation':'The delegate parent supports parallel bounded children; its own unlimited limit is not the 2GiB per-instance limit. Actual per-instance memory and memory+swap limits come from raw receipts.',
    'pending_final_evidence_gap':{
        'count':len(pending_groups),
        'groups_by_task':dict(Counter(Path(i['paths'][0]).parts[4] for i in pending_groups)),
        'reason':'Cleanup source reuse copies each world before pytest teardown finalizes registered memory receipts; the next consumer removes the original world. Surviving snapshots retain pending receipts while original finalized receipts were removed.',
        'source_evidence':[{'path':str(run_roots['replay']/'snapshot/tests/integration/_cleanup_source_snapshot.py'),'lines':[233,277],'meaning':'remove prior world; copy world before pytest teardown'},{'path':str(run_roots['replay']/'snapshot/scripts/camctl_memory_plugin.py'),'lines':[127,129],'meaning':'finalize registered receipts after pytest teardown yield'}],
        'actual_limits_evidence':'Each pending receipt retains a post-configuration baseline with memory and memory+swap both 2147483648 bytes. Final peak and final event counts are unavailable.',
        'missing_facts':['final per-group peak','final OOM and limit-hit counters','per-receipt group_removed acknowledgement'],
        'cannot_be_reconstructed_after_group_deletion':True,
        'receipt_paths':[i['paths'] for i in pending_groups],
    },
    'capacity_execution_gap':str(B/'capacity-execution-gap.json'),
    'interpretation':'No business OOM/limit hit appears in finalized receipts. The 89 missing final receipts prevent claiming complete memory-budget acceptance. Intentional 64MiB OOM calibration is separate from business 2GiB groups.',
}
write(B/'unified-resource-audit.json',resource)
unified={
    'audited_at':now,
    'status':'complete_with_recorded_test_failures_and_resource_evidence_gap',
    'frozen_production_batches':{'versions':cov['versions'],'four_production_scope_trees_identical':True,'guidance_runner_and_test_snapshot_differences_retained':True},
    'deduplicated_frozen_production_coverage':{
        'expected_known_collectable_pytest_nodes':cov['expected_collectable_unique_pytest_nodes'],
        'complete_unique_pytest_nodes':cov['complete_unique_pytest_nodes'],
        'latest_complete_outcomes':cov['latest_complete_or_partial_outcomes'],
        'unique_latest_phase_counts':dict(phase),'unique_latest_setup_outcomes':dict(setup),
        'missing_cases':cov['expected_without_any_case'],'incomplete_nodes':cov['expected_without_complete_phases'],
        'recorded_case_occurrences':cov['recorded_case_occurrences'],'complete_case_occurrences':cov['complete_case_occurrences'],
        'repeated_unique_nodes':len(cov['repeated_nodes']),
        'repetition_explanation':'Three capture failure nodes passed after fixture repair. One session node was interrupted after setup and replayed completely. Original completed 12 session nodes and original root cancellation node were not repeated.',
        'all_12_hasselblad_photo_matrix_nodes_replay_passed':cov['hb_full_12_replay_passed'],
        'node_tests_passed':118,'nested_pytest_subchecks_passed':342,'node_and_subcheck_counts_are_separate_from_main_pytest_nodes':True,
        'uncollected_obsolete_files':cov['uncollected_obsolete_files'],
        'capacity_note':'Five growth/history targets passed in main. The 513-input capacity test and 129-input pagination test (2 nodes) failed during shared fixture construction. An additional 5-input unbudgeted worker helper test (1 node) failed there too. No actual capacity worker or capacity memory receipt was produced.',
    },
    'third_current_source_narrow_batch':{
        'source_head':narrow['source_head'],'selected_modules':13,'passed_nodes':387,'failed_nodes':0,'skipped_nodes':0,'exit_code':0,
        'xml_time_seconds':float(narrow['xml_suites'][0]['time']),'wall_seconds':narrow['wall_seconds'],
        'audited_xml_and_current_source_hashes':True,'production_changes_from_frozen_batches':narrow['production_diff_from_frozen_main_replay'],
        'audit':str(B/'final-narrow-audit.json'),'excluded_from_frozen_production_union_counts':True,'full_current_source_integration_pass_claimed':False,
    },
    'resources':{'audit':str(B/'unified-resource-audit.json'),'complete_resource_acceptance':False,'pending_final_evidence_gap':89,'all_owned_groups_and_known_pids_absent':True,'both_launcher_delegations_removed':True},
    'evidence':{'coverage':str(B/'combined-coverage-audit.json'),'case_histories':str(B/'combined-case-histories.json'),'source_identity':str(B/'production-identity.json'),'resources':str(B/'unified-resource-audit.json'),'capacity_gap':str(B/'capacity-execution-gap.json'),'third_batch':str(B/'final-narrow-audit.json')},
    'conclusion':'Coverage of 10312 known collectable pytest nodes is complete across two frozen test snapshots with identical production trees; 5083 latest results failed. One obsolete file remains uncollectable with unknown node count. The later production repair has a separate 387-node narrow validation. This is not full integration success.',
}
write(B/'unified-audit.json',unified)
files=['unified-audit.json','combined-coverage-audit.json','combined-case-histories.json','unified-resource-audit.json','production-identity.json','capacity-execution-gap.json','final-narrow-audit.json','coverage-audit.json','canonical-inventory.json','canonical-cases.json','resource-audit.json','replay/coverage-audit.json','replay/canonical-inventory.json','replay/canonical-cases.json','replay/resource-audit.json']
manifest={name:{'path':str(B/name),'bytes':(B/name).stat().st_size,'sha256':hashlib.sha256((B/name).read_bytes()).hexdigest()} for name in files}
write(B/'final-audit-manifest.json',{'audited_at':now,'files':manifest})
print(json.dumps({'unified':str(B/'unified-audit.json'),'manifest':str(B/'final-audit-manifest.json'),'coverage':unified['deduplicated_frozen_production_coverage'],'resources':resource['business_instances']},ensure_ascii=False,indent=2))
