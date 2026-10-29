import collections
import json
from pathlib import Path
import re

source = Path('/tmp/camctl-integration-all-final-20261026-u19lkvoe/tasks/001-camctl-history/artifacts')
target = Path(__file__).parent
progress = json.loads((source/'progress.json').read_text())
if progress.get('status') not in ('finished', 'complete', 'completed'):
    raise SystemExit('Task has not naturally finished: '+repr(progress))
text = (source/'output.log').read_text(errors='replace')
events = [json.loads(line) for line in (source/'events.jsonl').read_text().splitlines()]
failed_events = [e for e in events if e.get('record') == 'phase' and e.get('outcome') == 'failed']
summary = []
for match in re.finditer(r'^(FAILED|ERROR) (.+?)(?: - (.*))?$', text, re.M):
    summary.append({'outcome':match[1], 'nodeid':match[2], 'summary':match[3] or ''})
headers=list(re.finditer(r'^_+ ((?:ERROR at (?:setup|teardown) of )?(?:[\w]+\.)?test_.+?) _+\s*$',text,re.M))
sections=[]
for index, match in enumerate(headers):
    end=headers[index+1].start() if index+1<len(headers) else len(text)
    body=text[match.end():end]
    boundary=re.search(r'^={3,} (?:warnings summary|short test summary info|slowest).*$',body,re.M)
    if boundary: body=body[:boundary.start()]
    sections.append((match[1],body))
trace_dir=target/'traces'
trace_dir.mkdir(exist_ok=True)
records=[]
for index, record in enumerate(summary,1):
    test=record['nodeid'].split('::',1)[-1].replace('::','.')
    found=[(header,body) for header,body in sections if header==test or header.endswith(' of '+test)]
    record['trace_match_count']=len(found)
    record['trace_files']=[]
    record['exception_lines']=[]
    for number,(header,body) in enumerate(found,1):
        path=trace_dir/(f'{index:04d}-{number}.txt')
        path.write_text(header+'\n'+body)
        record['trace_files'].append(str(path))
        record['exception_lines'].extend(re.findall(r'^E\s+(.+)$',body,re.M))
    records.append(record)
(target/'failed-cases.json').write_text(json.dumps(records,ensure_ascii=False,indent=2)+'\n')
(target/'parse-summary.json').write_text(json.dumps({'task_progress':progress,'case_summary_count':len(summary),'failed_phase_count':len(failed_events),'summary_outcomes':dict(collections.Counter(r['outcome'] for r in records)),'trace_sections':len(sections),'trace_match_counts':dict(collections.Counter(r['trace_match_count'] for r in records)),'latest_event':events[-1]},ensure_ascii=False,indent=2)+'\n')
print((target/'parse-summary.json').read_text())
print('Most frequent terminal diagnostics:')
for value,count in collections.Counter(r['exception_lines'][-1] if r['exception_lines'] else r['summary'] for r in records).most_common(40):
    print(count,value)
