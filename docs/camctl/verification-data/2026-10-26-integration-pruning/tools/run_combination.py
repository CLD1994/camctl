import datetime
import json
import os
from pathlib import Path
import subprocess
import time
import xml.etree.ElementTree as ET

repo=Path('/opt/devcontainer/.codex/worktrees/de38/camctl')
out=repo/'.local/verification/integration-pruning-20261026/combination'
out.mkdir(exist_ok=False)
python='/workspaces/camctl/apps/camctl/.venv311/bin/python'
base='apps/camctl/tests/'
groups={
 'unit-cover':[
 base+'unit/contracts/test_enums.py',base+'unit/persistence/test_operation_fact_reads.py',
 base+'unit/bootstrap/test_configuration.py::TestDefaultsAndOverride::test_no_document_uses_full_defaults',
 base+'unit/outputs/test_selection_lookup.py::test_explicit_target_not_queried_cannot_be_classified_as_missing',
 base+'unit/session/test_clock.py::TestCheckClock::test_implausible_reading_is_untrusted_without_update',
 base+'unit/session/test_clock.py::TestEnterExecution::test_untrusted_reading_enters_restricted_mode',
 base+'unit/session/test_handoff.py',base+'unit/session/test_work.py'],
 'bootstrap':[base+'integration/bootstrap/test_configuration.py'],
 'acceptance':[base+'integration/acceptance/test_links.py::TestAutoPreviewConflicts::test_all_duplicate_previews_fail'],
 'capture':[base+'integration/capture/test_finish_reuse.py::test_new_key_after_terminal_recovers_without_reregistering',base+'integration/capture/test_finish_reuse.py::test_new_key_after_terminal_rejects_appended_outputs'],
 'contracts':[base+'integration/contracts/test_enums.py'],
 'outputs':[base+'integration/outputs/test_selection_lookup.py',base+'integration/outputs/test_source_resolution_batches.py::test_six_original_forms_publish_exact_members_without_delete_selections'],
 'session':[base+'integration/session/test_handoff.py',base+'integration/session/test_submit_boundary.py::test_submit_response_uses_transactional_admission_observation',base+'integration/session/test_submit_boundary.py::test_submit_before_close_keeps_acceptor_responsible'],
 'logging_runtime':[base+'integration/logging_runtime/test_handler.py',base+'integration/logging_runtime/test_copies.py',base+'integration/logging_runtime/test_channel_lifecycle.py'],
 'persistence':[base+'integration/persistence/test_transactions.py'],
 'reporting':[base+'integration/reporting/test_result_error_generation.py',base+'integration/reporting/test_freeze.py::test_freeze_reads_current_ack',base+'integration/reporting/test_publish.py::test_success_requires_current_publish_intent'],
}
summary={'head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip(),'python':subprocess.check_output([python,'--version'],text=True).strip(),'started_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'groups':[]}
start=time.monotonic()
for label,targets in groups.items():
 p=out/label;p.mkdir()
 command=[python,'-m','pytest',*targets,'-q','--tb=short','--rootdir',str(repo),f'--junitxml={p/"results.xml"}',f'--basetemp={p/"tmp"}']
 env=dict(os.environ,PYTHONPATH=f'{repo}/apps/camctl/src:{repo}/apps/camctl/tests',PYTHONDONTWRITEBYTECODE='1')
 before=time.monotonic()
 with (p/'pytest.log').open('w') as stream:
  result=subprocess.run(command,cwd=repo,env=env,stdout=stream,stderr=subprocess.STDOUT)
 record={'name':label,'command':command,'cwd':str(repo),'exit_code':result.returncode,'wall_seconds':time.monotonic()-before}
 if (p/'results.xml').exists():
  cases=ET.parse(p/'results.xml').findall('.//testcase')
  record['counts']={k:sum(c.find(tag) is not None for c in cases) for k,tag in [('failed','failure'),('errors','error'),('skipped','skipped')]}
  record['counts']['passed']=len(cases)-sum(record['counts'].values())
 (p/'receipt.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
 summary['groups'].append(record)
 (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps(record,ensure_ascii=False),flush=True)
summary.update(wall_seconds=time.monotonic()-start,finished_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),exit_code=1 if any(x['exit_code'] for x in summary['groups']) else 0)
(out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'wall_seconds':summary['wall_seconds'],'exit_code':summary['exit_code']},ensure_ascii=False),flush=True)
raise SystemExit(summary['exit_code'])
