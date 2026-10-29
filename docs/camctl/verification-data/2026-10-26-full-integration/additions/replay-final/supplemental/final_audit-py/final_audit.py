from pathlib import Path
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib, json, os, re, sys
ROOT=Path(sys.argv[1]) if len(sys.argv)>1 else Path('/tmp/camctl-integration-all-final-20261026-u19lkvoe')
OUT=Path(sys.argv[2]) if len(sys.argv)>2 else Path('/tmp/camctl-full-272f-audit')
OUT.mkdir(parents=True,exist_ok=True)

def read(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default

def canonical(node, scope):
    file, marker, name=node.partition('::')
    if scope=='camctl':
        prefix='apps/camctl/tests/integration/'
        if prefix in file: file=file[file.rfind(prefix):]
        elif 'tests/integration/' in file: file='apps/camctl/'+file[file.rfind('tests/integration/'):]
    elif 'tests/integration/' in file: file=file[file.rfind('tests/integration/'):]
    return file+marker+name

def records(path):
    if not path.exists(): return []
    result=[]
    for line in path.read_text().splitlines():
        try: result.append(json.loads(line))
        except json.JSONDecodeError: result.append({'malformed_line':line[:200]})
    return result

summary=read(ROOT/'summary.json');plan=read(ROOT/'plan.json');identity=read(ROOT/'source.json')
all_inventory=Counter();all_cases=Counter();outcomes=Counter();phases=Counter();subchecks=Counter()
tasks=[];all_case_rows={};base_ids=set();missing_artifacts=[]
for task in summary['tasks']:
    base_ids.add(task.get('parent_id',task['id']))
    a=Path(task['artifacts']) if task.get('artifacts') else None
    row={'id':task['id'],'scope':task['scope'],'status':task['status'],'exit_code':task.get('exit_code')}
    if a is None:
        missing_artifacts.append(task['id']);tasks.append(row);continue
    inventory=read(a/'inventory.json');cases=read(a/'cases.json');events=records(a/'events.jsonl')
    row.update(artifacts=str(a),inventory_present=inventory is not None,cases_present=cases is not None)
    if task['scope']=='node':
        row['node_result']=read(a/'progress.json');tasks.append(row);continue
    inv=[canonical(n,task['scope']) for n in (inventory or [])]
    cs=[canonical(c['nodeid'],task['scope']) for c in (cases or [])]
    all_inventory.update(inv);all_cases.update(cs)
    row.update(collected=len(inv),case_count=len(cs),inventory_duplicates=[n for n,c in Counter(inv).items() if c>1],case_duplicates=[n for n,c in Counter(cs).items() if c>1],collected_but_no_case=sorted(set(inv)-set(cs)),case_not_in_inventory=sorted(set(cs)-set(inv)))
    phase_counts=Counter((canonical(e['nodeid'],task['scope']),e['phase']) for e in events if e.get('record')=='phase')
    row['duplicate_phase_reports']=[{'nodeid':n,'phase':p,'count':count} for (n,p),count in phase_counts.items() if count!=1]
    incomplete=[];blocked=[]
    for case,node in zip(cases or [],cs):
        all_case_rows[node]=case;outcomes[case['outcome']]+=1;phases.update(case['phases'].keys())
        found=set(case['phases']);setup=case['phases'].get('setup',{}).get('outcome')
        needed={'setup','teardown'}|({'call'} if setup=='passed' else set())
        if needed-found:incomplete.append({'nodeid':node,'missing':sorted(needed-found),'found':sorted(found)})
        if setup in ('failed','skipped'):blocked.append({'nodeid':node,'setup':setup,'call_present':'call' in found})
    row.update(incomplete_phases=incomplete,call_blocked_by_setup=blocked)
    for event in events:
        if event.get('record')=='subcheck':subchecks[event['outcome']]+=1
    row['recorded_subchecks']=dict(Counter(e['outcome'] for e in events if e.get('record')=='subcheck'))
    row['progress']=read(a/'progress.json')
    tasks.append(row)
collections=[]
for p in ROOT.glob('collection/*/artifacts/inventory.json'):
    expected=[canonical(n,'root') for n in read(p)];base='root/'+p.parents[1].name.removeprefix('root-')
    matches=[t for t in summary['tasks'] if t.get('parent_id',t['id'])==base]
    planned=[canonical(n,'root') for t in matches for n in t['targets']]
    actual=[canonical(n,'root') for t in matches if t.get('artifacts') for n in read(Path(t['artifacts'])/'inventory.json',[]) ]
    completed=[canonical(c['nodeid'],'root') for t in matches if t.get('artifacts') for c in read(Path(t['artifacts'])/'cases.json',[]) ]
    collections.append({'module':base,'collection_count':len(expected),'planned_count':len(planned),'planned_unique':len(set(planned)),'plan_exact_unique_cover':Counter(planned)==Counter(expected),'executed_inventory_count':len(actual),'execution_exact_unique_cover':Counter(actual)==Counter(expected),'recorded_case_count':len(completed),'cases_exact_unique_cover':Counter(completed)==Counter(expected),'missing_execution':sorted(set(expected)-set(actual)),'missing_cases':sorted(set(expected)-set(completed))})
hb_expected={f'tests/integration/test_hasselblad_photo_roundtrip.py::test_installed_hasselblad_photo_roundtrip[{fmt}-{source}-{shutter}]' for fmt in ('raw','jpeg') for source in ('host','device') for shutter in ('0.000667','0.000333','0.00025')}
hb={node:{'inventory_count':all_inventory[node],'case_count':all_cases[node],'outcome':all_case_rows.get(node,{}).get('outcome'),'phases':all_case_rows.get(node,{}).get('phases')} for node in sorted(hb_expected)}
capacity_fragments=('test_large_growth_crosses_page_and_disk_cache_capacity','test_large_window_releases_overflow_once_and_keyset_pages_do_not_rescan_window','test_member_growth_restore_cost','test_event_growth_restore_pages_cost','test_unrelated_growth_keeps_target_restore_stable','test_fixed_batch_write_and_verification_have_bounded_residency','test_fixed_batch_reads_and_output_identity_lookup_keep_their_bounds')
capacity={fragment:[{'nodeid':node,'inventory_count':all_inventory[node],'case_count':all_cases[node],'outcome':all_case_rows.get(node,{}).get('outcome'),'phases':all_case_rows.get(node,{}).get('phases')} for node in sorted(set(all_inventory)|set(all_cases)) if node.partition('::')[2].split('[')[0]==fragment] for fragment in capacity_fragments}
node_tasks=[t for t in tasks if t['scope']=='node'];node_counts=Counter()
for t in node_tasks:node_counts.update((t.get('node_result') or {}).get('outcomes',{}))
source_changes=[]
for row in identity['files']:
    p=ROOT/'snapshot'/row['path']
    actual=hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
    if actual!=row['sha256']:source_changes.append({'path':row['path'],'expected':row['sha256'],'actual':actual})
report={'audited_at':datetime.now(timezone.utc).isoformat(),'runner_status':summary['status'],'runner_exit_code':summary.get('exit_code'),'source_head':identity['head'],'source_status':identity['status'],'snapshot_sha256':identity['snapshot_sha256'],'snapshot_files_unchanged':not source_changes,'snapshot_changes':source_changes,'planned_task_count':len(plan),'summary_task_count':len(summary['tasks']),'task_states':dict(Counter(t['status'] for t in summary['tasks'])),'base_task_count':len(base_ids),'pytest_inventory_count':sum(all_inventory.values()),'pytest_inventory_unique':len(all_inventory),'pytest_cases_count':sum(all_cases.values()),'pytest_cases_unique':len(all_cases),'pytest_outcomes':dict(outcomes),'phase_counts':dict(phases),'subchecks_from_events':dict(subchecks),'duplicate_inventory_global':{n:c for n,c in all_inventory.items() if c!=1},'duplicate_cases_global':{n:c for n,c in all_cases.items() if c!=1},'collected_without_case':sorted(set(all_inventory)-set(all_cases)),'case_without_inventory':sorted(set(all_cases)-set(all_inventory)),'incomplete_phase_tasks':[t['id'] for t in tasks if t.get('incomplete_phases')],'duplicate_phase_tasks':[t['id'] for t in tasks if t.get('duplicate_phase_reports')],'missing_task_artifacts':missing_artifacts,'collection_shard_coverage':collections,'hb_full_12':hb,'hb_all_12_recorded_passed':all(v['inventory_count']==1 and v['case_count']==1 and v['outcome']=='passed' for v in hb.values()),'capacity_checks':capacity,'capacity_all_recorded_passed':all(rows and all(row['case_count']==1 and row['outcome']=='passed' for row in rows) for rows in capacity.values()),'node_counts':dict(node_counts),'tasks':tasks}
(OUT/'coverage-audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
(OUT/'canonical-inventory.json').write_text(json.dumps(dict(all_inventory),ensure_ascii=False,indent=2)+'\n')
(OUT/'canonical-cases.json').write_text(json.dumps(all_case_rows,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({k:report[k] for k in ('runner_status','runner_exit_code','planned_task_count','task_states','pytest_inventory_count','pytest_inventory_unique','pytest_cases_count','pytest_cases_unique','pytest_outcomes','phase_counts','subchecks_from_events','hb_all_12_recorded_passed','capacity_all_recorded_passed','node_counts')},ensure_ascii=False,indent=2))
