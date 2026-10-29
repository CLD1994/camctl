"""归档已结束 OSMO 实测，SQLite 使用备份接口，较大文本日志无损 gzip。"""
from pathlib import Path
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time

label = sys.argv[1]
repo = Path('/tmp/camctl-quality-repair-lidlmje7/osmo-recovery')
ev = Path('/tmp/camctl-quality-repair-evidence/final-osmo')
runtime = Path('/tmp/camctl-quality-final-osmo-runtime-lidlmje7')
base = runtime / (label + '-pytest')
command = json.loads((ev / (label + '-command.json')).read_text())
result = json.loads((ev / (label + '-result.json')).read_text())
launcher = json.loads((ev / (label + '-budget/launcher.json')).read_text())
summary = json.loads((ev / (label + '-summary.json')).read_text())
assert result['exitcode'] == 0 and len(summary['worlds']) == 8
assert launcher['status'] != 'running'
pytest_pid = launcher['command_pid']
source_archive = repo / 'tmp' / ('osmo-installed-chain-archive-' + str(pytest_pid))
raw = ev / (label + '-raw')
raw.mkdir(exist_ok=True)
manifest = []


def copy_file(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    contents = source.read_bytes()
    digest = hashlib.sha256(contents).hexdigest()
    compress = source.suffix in {'.log', '.jsonl'} and len(contents) >= 256 * 1024
    if compress:
        target = target.with_name(target.name + '.gz')
        with target.open('wb') as stream:
            with gzip.GzipFile(filename='', mode='wb', fileobj=stream, mtime=0) as output:
                output.write(contents)
        with gzip.open(target, 'rb') as stream:
            assert hashlib.sha256(stream.read()).hexdigest() == digest
    else:
        shutil.copy2(source, target)
    manifest.append({'source': str(source), 'saved': str(target), 'source_bytes': len(contents),
                     'saved_bytes': target.stat().st_size, 'source_sha256': digest, 'gzip': compress})


for path in source_archive.rglob('*'):
    if path.is_file():
        copy_file(path, raw / 'installed-chain' / path.relative_to(source_archive))
worlds = []
for world in sorted(base.glob('test_installed_segment_source_[0-9]*/world')):
    if world.is_symlink():
        continue
    session = json.loads((world / 'client-session-python.json').read_text())
    scenario = session['nodeid'].rsplit('[', 1)[-1].rstrip(']')
    target = raw / 'worlds' / scenario
    target.mkdir(parents=True, exist_ok=True)
    source = world / 'deployment/state.db'
    database = target / 'state.db'
    connection = sqlite3.connect('file:' + str(source) + '?mode=ro', uri=True)
    saved = sqlite3.connect(database)
    connection.backup(saved)
    saved.close()
    connection.row_factory = sqlite3.Row
    counts = {table: connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
              for table in ['recording_processing', 'segment_inspections', 'file_copies', 'deliveries']}
    worlds.append({'scenario': scenario, 'state_backup': str(database), 'counts': counts})
    connection.close()
    for name in ['client-session-python.json', 'client-session-node.json']:
        copy_file(world / name, target / name)
    for plan in world.glob('client-plan-*.json'):
        copy_file(plan, target / plan.name)

adb = source_archive / 'runtime/native-tail/adb-calls.jsonl'
records = [json.loads(row) for row in adb.read_text().splitlines()]
blocked = [record for record in records if record['kind'] == 'read_blocked']
assert len(blocked) == 2
old, new = blocked
assert old['forwarded_bytes'] == 65536 and new['forwarded_bytes'] == 0
assert old['script'].endswith('skip=0') and new['script'] == old['script'][:-1] + '1'
assert new['started_ns'] > old['started_ns']
assert not any(record['kind'] == 'read' and record['started_ns'] == old['started_ns'] for record in records)
connection = sqlite3.connect(raw / 'worlds/native-tail-indexes7-2-succeeded/state.db')
connection.row_factory = sqlite3.Row
copies = [dict(row) for row in connection.execute(
    'SELECT c.id AS copy_id,c.delivery_id,c.committed_bytes,c.source_size,d.output_id,'
    'a.name AS action_name,o.original_name FROM file_copies c '
    'JOIN deliveries d ON d.id=c.delivery_id JOIN actions a ON a.id=d.action_id '
    'JOIN outputs o ON o.id=d.output_id ORDER BY c.id')]
connection.close()
observation = {'source_commit': result['git_head'], 'source_test_sha256': result['test_sha256'],
               'pytest_case': 'native-tail-indexes7-2-succeeded', 'case_result': 'passed',
               'interrupted_read': old, 'resumed_read_blocked_before_first_byte': new,
               'old_read_completion_record_absent': True, 'final_copy_delivery_rows': copies,
               'before_gate_assertions': 'The original test passed same copy/delivery/persisted offset and complete delivery/output assertions before releasing the actual read gate at forwarded_bytes=0. Final database rows alone do not prove the intermediate offset.'}
(ev / (label + '-native-resume.json')).write_text(json.dumps(observation, indent=2) + '\n')
(ev / (label + '-state-inventory.json')).write_text(json.dumps(worlds, indent=2) + '\n')

# 对汇总器已保存的较大日志也执行相同无损压缩。
for path in list((ev / (label + '-worlds')).rglob('*')):
    if path.is_file() and path.suffix in {'.log', '.jsonl'} and path.stat().st_size >= 256 * 1024:
        copy_file(path, path)
        path.unlink()
(ev / (label + '-archive-manifest.json')).write_text(json.dumps(manifest, indent=2) + '\n')
ancestors, pid = set(), os.getpid()
while pid > 1:
    ancestors.add(pid)
    try:
        pid = int(Path('/proc', str(pid), 'stat').read_text().split(') ', 1)[1].split()[1])
    except (FileNotFoundError, ProcessLookupError):
        break
live = []
for proc in Path('/proc').iterdir():
    if not proc.name.isdigit() or int(proc.name) in ancestors:
        continue
    try:
        cwd = os.readlink(proc / 'cwd')
        args = (proc / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        continue
    if cwd.startswith(str(runtime)) or cwd.startswith(str(repo)) or str(runtime) in args:
        live.append({'pid': int(proc.name), 'cwd': cwd, 'command': args})
post = {'checked_at': time.time(), 'git_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip(),
        'git_status': subprocess.check_output(['git', 'status', '--short'], cwd=repo, text=True),
        'remaining_test_processes': live}
(ev / (label + '-postflight.json')).write_text(json.dumps(post, indent=2) + '\n')
receipt = {'commit': summary['source_head'], 'collected': 8, 'passed': 8, 'failed': 0, 'remaining': 0,
           'launcher_wall_seconds': summary['wall_seconds'], 'cli_groups': sum(w['cli_group_count'] for w in summary['worlds']),
           'peak_combined_bytes': max(w['max_combined_peak_bytes'] for w in summary['worlds']),
           'all_groups_removed': all(w['groups_removed'] for w in summary['worlds']),
           'all_limits_2GiB': all(w['all_combined_limits_2GiB'] for w in summary['worlds']),
           'resource_events': [row for w in summary['worlds'] for row in w['oom_or_limit_events'] if row],
           'all_client_sessions_closed': all(w['status'] == 'closed' and w['close']['outcome'] == 'passed' for w in summary['worlds']),
           'all_client_operations_passed': all(w['all_operations_passed'] for w in summary['worlds']),
           'remaining_pids': summary['remaining_pids'], 'launcher_errors': summary['launcher_errors'],
           'delegation_removed': summary['delegation_removed'], 'native_gate_forwarded_bytes': [old['forwarded_bytes'], new['forwarded_bytes']],
           'archived_worlds': len(worlds), 'compressed_logs': sum(item['gzip'] for item in manifest),
           'archived_source_bytes': sum(item['source_bytes'] for item in manifest), 'archived_saved_bytes': sum(item['saved_bytes'] for item in manifest)}
(ev / (label + '-receipt.json')).write_text(json.dumps(receipt, indent=2) + '\n')
print(json.dumps(receipt, indent=2))
