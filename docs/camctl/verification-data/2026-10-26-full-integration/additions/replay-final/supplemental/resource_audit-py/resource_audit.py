from pathlib import Path
from collections import Counter
from datetime import datetime, timezone
import json, os, sys
ROOT=Path(sys.argv[1]) if len(sys.argv)>1 else Path('/tmp/camctl-integration-all-final-20261026-u19lkvoe')
OUT=Path(sys.argv[2]) if len(sys.argv)>2 else Path('/tmp/camctl-full-272f-audit')
OUT.mkdir(parents=True,exist_ok=True)
launcher_path=Path(str(ROOT)+'-budget/launcher.json')
launcher=json.loads(launcher_path.read_text())
receipts={};paths=[];bad_json=[]
for pattern in ('tasks/*/tmp/**/memory-budget/camctl-*.json','tasks/*/tmp/**/memory-budget.json','tasks/*/tmp/pytest/test_real_cgroup*/*-receipt.json'):
    for path in ROOT.glob(pattern):
        try: row=json.loads(path.read_text())
        except json.JSONDecodeError:
            bad_json.append(str(path));continue
        if row.get('budget_scope')!='memory_and_swap' or not row.get('group','').startswith('/sys/fs/cgroup/'):continue
        group=row['group'];paths.append(str(path));item=receipts.setdefault(group,{'group':group,'paths':[]})
        item['paths'].append(str(path))
        old=item.get('receipt')
        score=lambda r: ('evidence' in r, bool(r.get('group_removed')), r.get('status') not in ('pending','starting','finalizing'))
        if old is None or score(row)>score(old):
            item['receipt']=row;item['selected_receipt_path']=str(path)
all_rows=list(receipts.values());nonstandard=[];over=[];oom=[];unfinished=[];present=[]
for item in all_rows:
    row=item['receipt'];e=row.get('evidence',{});limit=row.get('limit_bytes');peak=e.get('combined_peak_bytes');events=e.get('events',{})
    if limit!=2147483648:nonstandard.append({'paths':item['paths'],'limit':limit,'calibration':any('test_real_cgroup_' in p for p in item['paths'])})
    if peak is not None and limit is not None and peak>limit:over.append({'paths':item['paths'],'limit':limit,'combined_peak':peak})
    if any(events.get(k,0) for k in ('oom','oom_kill','oom_group_kill')):oom.append({'paths':item['paths'],'events':events,'limit':limit})
    if 'evidence' not in row:unfinished.append({'paths':item['paths'],'status':row.get('status'),'final_status':row.get('final_status')})
    if Path(item['group']).exists():present.append(item['group'])
known=set()
s=json.loads((ROOT/'summary.json').read_text())
for t in s['tasks']:
    for key in ('pid','preparation_pid'):
        if t.get(key):known.add(t[key])
    for p in t.get('preparations',[]):
        if p.get('pid'):known.add(p['pid'])
for item in all_rows:
    if item['receipt'].get('pid'):known.add(item['receipt']['pid'])
for key in ('command_pid','command_pgid'):
    if launcher.get(key):known.add(launcher[key])
live_known=[]
for pid in sorted(known):
    proc=Path('/proc')/str(pid)
    if proc.exists():
        try: command=(proc/'cmdline').read_bytes().replace(b'\0',b' ').decode(errors='replace')
        except (FileNotFoundError,ProcessLookupError):continue
        live_known.append({'pid':pid,'command':command,'same_run_path_present':str(ROOT) in command})
run_processes=[]
for proc in Path('/proc').iterdir():
    if not proc.name.isdecimal() or int(proc.name)==os.getpid():continue
    try:
        argv=[v.decode(errors='replace') for v in (proc/'cmdline').read_bytes().split(b'\0') if v]
        command=' '.join(argv)
    except (FileNotFoundError,ProcessLookupError,PermissionError):continue
    if str(ROOT) in command:
        direct=any(v.split('=',1)[-1].startswith(str(ROOT)+'/') for v in argv)
        run_processes.append({'pid':int(proc.name),'command':command,'direct_argument_points_into_run_tree':direct})
business=[item for item in all_rows if item['receipt'].get('limit_bytes')==2147483648]
calibration=[item for item in all_rows if item['receipt'].get('limit_bytes')==67108864 and any('test_real_cgroup_' in p for p in item['paths'])]
def event_totals(items):
    return {key:sum(item['receipt'].get('evidence',{}).get('events',{}).get(key,0) for item in items) for key in ('oom','oom_kill','oom_group_kill','failcnt','memsw_failcnt')}
def actual_limits_ok(items):
    return all(item['receipt'].get('evidence',item['receipt'].get('baseline',{})).get('limit_bytes')==item['receipt'].get('limit_bytes')==item['receipt'].get('evidence',item['receipt'].get('baseline',{})).get('combined_limit_bytes') for item in items)
report={'business_2gib':{'unique_groups':len(business),'finalized_evidence_groups':sum('evidence' in item['receipt'] for item in business),'pending_without_final_evidence_groups':sum('evidence' not in item['receipt'] for item in business),'all_final_evidence_present':all('evidence' in item['receipt'] for item in business),'actual_limits_source':'final evidence when present, otherwise original post-configuration baseline; absence of final evidence remains an explicit peak/event gap','all_actual_groups_absent':all(not Path(item['group']).exists() for item in business),'actual_memory_and_combined_limits_match':actual_limits_ok(business),'combined_peak_max_bytes':max((item['receipt'].get('evidence',{}).get('combined_peak_bytes',0) for item in business),default=0),'events':event_totals(business),'all_receipts_group_removed':all(item['receipt'].get('group_removed') for item in business)},'calibration_64mib':{'unique_groups':len(calibration),'actual_memory_and_combined_limits_match':actual_limits_ok(calibration),'events':event_totals(calibration),'receipts':[{'paths':item['paths'],'status':item['receipt'].get('status'),'returncode':item['receipt'].get('returncode'),'peak':item['receipt'].get('evidence',{}).get('combined_peak_bytes'),'events':item['receipt'].get('evidence',{}).get('events'),'group_removed':item['receipt'].get('group_removed')} for item in calibration]},'launcher_requested_2gib':launcher.get('limit_bytes')==2147483648,'delegation_parent_is_not_instance_limit':True,'launcher_delegation_groups_raw':launcher.get('groups',[]),'launcher_executed_command':launcher.get('executed_command'),'launcher_delegation_scope_explanation':'The delegated parent permits concurrent 2GiB child instances; its own limit is not the per-instance limit. Actual per-instance memory and memory+swap limits are verified from each raw receipt.','receipt_status_counts':dict(Counter(item['receipt'].get('status') for item in all_rows)),'audited_at' :datetime.now(timezone.utc).isoformat(),'runner_status':s['status'],'launcher_status':launcher['status'],'launcher_exit_code':launcher.get('exit_code'),'launcher_errors':launcher.get('errors'),'launcher_path':str(launcher_path),'delegation':launcher.get('delegation_path'),'delegation_exists':Path(launcher['delegation_path']).exists(),'removal':launcher.get('removal'),'raw_receipt_paths':len(paths),'unique_groups':len(all_rows),'combined_peak_max_bytes':max((item['receipt'].get('evidence',{}).get('combined_peak_bytes',0) for item in all_rows),default=0),'nonstandard_limits':nonstandard,'peak_over_limit':over,'oom_events':oom,'unfinished_receipts':unfinished,'groups_still_present':present,'bad_json':bad_json,'live_known_pids':live_known,'processes_referencing_run':run_processes,'live_workload_candidates':[p for p in run_processes if p['direct_argument_points_into_run_tree']],'controller_or_diagnostic_references':[p for p in run_processes if not p['direct_argument_points_into_run_tree']],'receipts':all_rows}
(OUT/'resource-audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({k:report[k] for k in ('business_2gib','calibration_64mib','runner_status','launcher_status','launcher_exit_code','launcher_errors','delegation_exists','removal','raw_receipt_paths','unique_groups','combined_peak_max_bytes','nonstandard_limits','peak_over_limit','oom_events','unfinished_receipts','groups_still_present','live_known_pids','processes_referencing_run')},ensure_ascii=False,indent=2))
