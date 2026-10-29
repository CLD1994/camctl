from pathlib import Path
import hashlib
import json
import os
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET

directory = Path(sys.argv[1]).resolve()


def read_json(path):
    return json.loads(path.read_text())


def write_json(name, value):
    (directory / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


resources = []
known = {}
for path in (directory / 'data').rglob('memory-budget/camctl-*.json'):
    receipt = read_json(path)
    resources.append({'file': str(path), **receipt})
    known.setdefault(receipt['pid'], {})
write_json('resource-summary.json', resources)
for line in (directory / 'process-snapshots.jsonl').read_text().splitlines():
    for process in json.loads(line)['processes']:
        known[process['pid']] = process
cli_count = 0
origins = []
for path in (directory / 'data').rglob('cli-processes.jsonl'):
    for line in path.read_text().splitlines():
        receipt = json.loads(line)
        known.setdefault(receipt['pid'], {})
        if receipt.get('stage') == 'started':
            cli_count += 1
            origins.append({'pid': receipt['pid'], 'python': receipt['python'],
                'import_origins': receipt.get('import_origins', {})})
for path in (directory / 'data').rglob('client-session-python.json'):
    receipt = read_json(path)
    if receipt.get('node_pid'):
        known.setdefault(receipt['node_pid'], {})
launcher = read_json(directory / 'budget/launcher.json')
for key in ('command_pid', 'command_pgid'):
    if launcher.get(key):
        known.setdefault(launcher[key], {})
for pid, record in known.items():
    proc = Path('/proc') / str(pid)
    record['exists'] = proc.exists()
    if proc.exists():
        stat = (proc / 'stat').read_text()
        tail = stat[stat.rfind(')') + 2:].split()
        record['current_starttime'] = tail[19]
        record['same_process'] = record.get('starttime', tail[19]) == tail[19]
    else:
        record['same_process'] = False
process_receipt = {'observed_time_ns': time.time_ns(), 'known_owned_pids': known,
    'live_owned_pids': [pid for pid, row in known.items() if row['exists'] and row['same_process']],
    'delegation_path': launcher.get('delegation_path'),
    'delegation_exists': Path(launcher['delegation_path']).exists(),
    'delegation_removal': launcher.get('removal'),
    'observer_exit': read_json(directory / 'observer-exit.json')}
write_json('process-receipt.json', process_receipt)
write_json('cli-origins.json', origins)
db_facts = []
for path in (directory / 'data').rglob('state.db'):
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        tables = {row[0] for row in conn.execute('SELECT name FROM sqlite_master WHERE type=\'table\'')}
        counts = {table: conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
            for table in ('plans','actions','device_files','outputs','deliveries','segment_sets',
                'segment_members','segment_inspections','history_events') if table in tables}
        actions = [dict(zip(('id','name','status'), row))
            for row in conn.execute('SELECT id,name,status FROM actions ORDER BY id')]
        db_facts.append({'path': str(path), 'counts': counts, 'actions': actions,
            'integrity_check': conn.execute('PRAGMA quick_check').fetchone()[0]})
    finally:
        conn.close()
write_json('database-summary.json', db_facts)
xml = ET.parse(directory / 'result.xml').getroot()
cases = [dict(case.attrib, outcome=('failed' if case.find('failure') is not None else
    'error' if case.find('error') is not None else
    'skipped' if case.find('skipped') is not None else 'passed'))
    for case in xml.iter('testcase')]
before = read_json(directory / 'source-before.json')
after = read_json(directory / 'source-after.json')
wheel_facts = [{'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    for path in (directory / 'data').rglob('dist/camctl-*.whl')]
resource = {'scope': 'each camctl CLI and all descendants; pytest/client/C host outside',
    'leaf_count': len(resources), 'requested_combined_limit_bytes': 2147483648,
    'all_leaf_combined_limits_correct': all(row['evidence']['combined_limit_bytes'] == 2147483648 for row in resources),
    'max_leaf_combined_peak_bytes': max(row['evidence']['combined_peak_bytes'] for row in resources),
    'oom_kills': sum(row['evidence']['events'].get('oom_kill', 0) for row in resources),
    'all_leaf_groups_removed': all(row.get('group_removed') for row in resources),
    'budget_launcher_exit_code': launcher['exit_code'],
    'delegation_removed': launcher.get('removal', {}).get('ok') is True}
summary = {'source_identity': {key: before[key] for key in (
    'repo','head','branch','status','python','sqlite','node','test_sha256','assert_count')},
    'pytest_exit_code': int((directory / 'exit-code.txt').read_text()),
    'progress': read_json(directory / 'progress.json'), 'cases': cases,
    'case_phases': read_json(directory / 'cases.json'),
    'original_success_budgets_seconds': {'START': 90, 'report': 240},
    'tracked_source_unchanged': after['tracked_source_unchanged'],
    'final_git_status': after['status'], 'wheel': wheel_facts,
    'actual_cli_starts': cli_count, 'resource': resource,
    'all_known_owned_processes_exited': not process_receipt['live_owned_pids'],
    'database_summary': str(directory / 'database-summary.json'),
    'process_receipt': str(directory / 'process-receipt.json'),
    'live_rotated_logs': str(directory / 'log-archive-index.jsonl')}
write_json('result-summary.json', summary)
print(json.dumps({key: summary[key] for key in (
    'pytest_exit_code','progress','resource','all_known_owned_processes_exited')}, ensure_ascii=False, indent=2))
