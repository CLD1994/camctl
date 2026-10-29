"""核对最新同步组合的原20节点、XML及生产目录身份。"""
from pathlib import Path
import json
import subprocess
import xml.etree.ElementTree as ET

REPO = Path('/opt/devcontainer/.codex/worktrees/de38/camctl')
PROOF = Path('/tmp/camctl-quality-repair-evidence')
INPUTS = {
    'unit': (PROOF/'latest-root/unit/result.xml', True),
    'capture': (PROOF/'latest-root/capture/result.xml', True),
    'persistence': (PROOF/'latest-root/persistence/result.xml', True),
    'outputs': (PROOF/'latest-root/outputs/result.xml', True),
    'bootstrap': (PROOF/'latest-root/bootstrap/result.xml', True),
    'action6': (PROOF/'final-action6/after-latest-d896d4b2/result.xml', False),
    'osmo': (PROOF/'final-osmo/after-latest-d896d4b2.xml', False),
    'history': (PROOF/'final-history-after-latest/pytest-results.xml', True),
}
groups, nodes = {}, {}
for group, (path, component) in INPUTS.items():
    root = ET.parse(path)
    cases = root.findall('.//testcase')
    failed = sum(case.find('failure') is not None or case.find('error') is not None for case in cases)
    skipped = sum(case.find('skipped') is not None for case in cases)
    assert failed == 0, (group, failed)
    groups[group] = dict(xml=str(path), passed=len(cases)-skipped, skipped=skipped, failed=failed)
    for case in cases:
        raw = case.attrib['classname']
        module = raw.replace('.', '/')+'.py'
        if 'tests/integration/' not in module:
            continue
        module = module[module.rindex('tests/integration/'):]
        node = ('apps/camctl/' if component else '')+module+'::'+case.attrib['name']
        nodes[node] = dict(nodeid=node, group=group,
                           seconds_including_pytest_phases=float(case.attrib['time']),
                           passed=all(case.find(tag) is None for tag in ('failure', 'error', 'skipped')),
                           xml=str(path), raw_xml_classname=raw)
selection = json.loads((REPO/'docs/camctl/verification-data/2026-10-26-integration-quality/selection.json').read_text())
selected = [nodes[case['nodeid']] for case in selection['cases']]
assert len(selected) == 20 and all(case['passed'] for case in selected)
identities = []
for reference in ['d896d4b2', 'HEAD']:
    commit = subprocess.check_output(['git', 'rev-parse', reference], cwd=REPO, text=True).strip()
    trees = {scope: subprocess.check_output(['git', 'rev-parse', commit+':'+scope], cwd=REPO, text=True).strip()
             for scope in ['apps/camctl/src', 'apps/client', 'apps/host-demo', 'protocol']}
    identities.append(dict(commit=commit, production_trees=trees))
assert identities[0]['production_trees'] == identities[1]['production_trees']
result = dict(synchronized_implement_camctl='27f845274cfaa1954c4a06ce4379d3616f685a6b',
              shared_test_commit='d896d4b28194258cb5da71b160d5ef22d642d48e',
              groups=groups, selected_passed=20, selected_failed=0,
              selected_cases=selected, production_identities=identities)
(PROOF/'latest-final-verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
print(json.dumps(dict(groups=groups, selected_passed=20), ensure_ascii=False))
