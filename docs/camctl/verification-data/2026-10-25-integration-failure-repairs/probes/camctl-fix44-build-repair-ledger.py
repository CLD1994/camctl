from __future__ import annotations
import csv,json
from pathlib import Path
repo=Path('/opt/devcontainer/.codex/worktrees/de38/camctl')
base=repo/'docs/camctl/verification-data/2026-10-25-integration-failure-diagnosis'
out=repo/'docs/camctl/verification-data/2026-10-25-integration-failure-repairs'
original=json.loads((base/'case-ledger.json').read_text())['cases']
assert sorted(c['diagnosis_id'] for c in original)==list(range(1,45))

def read_run(root:Path,label:str):
    summary=json.loads((root/'summary.json').read_text());result={}
    assert summary['status']=='finished'
    for task in summary['tasks']:
        path=Path(task['artifacts'])/'cases.json'
        for case in json.loads(path.read_text()):
            node=case['nodeid']
            if task['scope']=='camctl':node='apps/camctl/'+node
            assert node not in result,node
            result[node]={**case,'nodeid':node,'task':task['id'],'source_commit':summary['source']['head'],
                          'artifact':str(Path(label)/'tasks'/path.parent.parent.name/'cases.json')}
    return summary,result
first,prior=read_run(Path('/tmp/camctl-fix44-final-20261025'),'combined-44-before-media-fix')
combined,current=read_run(Path('/tmp/camctl-fix44-verified-20261025'),'combined-mainline')
final,browser=read_run(Path('/tmp/camctl-fix44-browser-final-20261025'),'browser-final')
assert {node for node,c in current.items() if c['outcome']=='failed'}==set(browser),set(browser)
assert all(c['outcome']=='passed' for c in browser.values())
combined_results=current.copy()
current.update(browser)
expected={c['nodeid'] for c in original}
selection=json.loads(Path('/tmp/camctl-fix44-verified-command.json').read_text())
additional=set(selection['additional_targets'])
assert set(current)==expected|additional,(set(current)-(expected|additional),(expected|additional)-set(current))
assert set(prior)==expected
assert final['exit_code']==0
assert all(c['outcome']=='passed' and all(p['outcome']=='passed' for p in c['phases'].values()) for c in current.values())
rows=[]
for case in original:
    node=case['nodeid']
    row={'diagnosis_id':case['diagnosis_id'],'nodeid':node,
         'historical_root_cause_resolved':case['historical_root_cause_resolved'],
         'historical_evidence':'../2026-10-25-integration-failure-diagnosis/case-ledger.json',
         'first_combined_result':prior[node],'mainline_combined_result':combined_results[node],'current_result':current[node]}
    if case['diagnosis_id']==30:row['historical_limit']='历史底层异常链未记录，无法唯一恢复原state_db_error原因；当前同身份通过不改变该结论。'
    rows.append(row)
payload={'date':'2026-10-25','scope':'原44失败身份与3项兼容检查的逐项最新有效验证；组合42原身份及3兼容项通过后，两个浏览器身份另批修复补测；非单轮44全通过，非完整集成回归',
         'source_commit':final['source']['head'],'verification_batches':[{'artifact':'combined-mainline/summary.json','source_commit':combined['source']['head'],'jobs':combined['jobs'],'wall_seconds':combined['wall_seconds'],'exit_code':combined['exit_code'],'passed':45,'failed':2},{'artifact':'browser-final/summary.json','source_commit':final['source']['head'],'jobs':final['jobs'],'wall_seconds':final['wall_seconds'],'exit_code':final['exit_code'],'passed':len(browser),'failed':0}],
         'exit_code':final['exit_code'],'counts':{'original_passed':len(rows),'additional_passed':len(additional),'failed':0,'skipped':0},'cases':rows,
         'additional_cases':[current[node] for node in sorted(additional)]}
(out/'repair-ledger.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n')
with (out/'repair-ledger.csv').open('w',newline='') as f:
    w=csv.writer(f);w.writerow(['diagnosis_id','nodeid','outcome','source_commit','task','setup_seconds','call_seconds','teardown_seconds','duration_seconds','historical_root_cause_resolved'])
    for row in rows:
        c=row['current_result'];w.writerow([row['diagnosis_id'],row['nodeid'],c['outcome'],c['source_commit'],c['task'],*[c['phases'].get(k,{}).get('duration_seconds','') for k in ['setup','call','teardown']],c['duration_seconds'],row['historical_root_cause_resolved']])
print(json.dumps({k:v for k,v in payload.items() if k!='cases'},ensure_ascii=False,indent=2))
