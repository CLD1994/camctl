from pathlib import Path
import ast
from collections import Counter
import hashlib
import json
import os
import platform
import sqlite3
import subprocess
import sys
import time

REPO = Path('/tmp/camctl-quality-repair-lidlmje7/action6')
EVIDENCE = Path(__file__).resolve().parent
stage, moment = sys.argv[1:]
directory = EVIDENCE / stage


def git(*args):
    return subprocess.check_output(['git', *args], cwd=REPO, text=True).strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


expected = 'd896d4b28194258cb5da71b160d5ef22d642d48e'
head = git('rev-parse', 'HEAD')
status = git('status', '--short')
test_path = REPO / 'tests/integration/test_action6_segmented_recovery_roundtrip.py'
test_tree = ast.parse(test_path.read_text())
manifest = {
    name: digest(REPO / name)
    for name in git('ls-files').splitlines()
    if (REPO / name).is_file()
}
identity = {
    'repo': str(REPO), 'head': head,
    'branch': git('branch', '--show-current'), 'status': status,
    'observed_time_ns': time.time_ns(), 'pid': os.getpid(),
    'python': sys.version, 'python_executable': sys.executable,
    'sqlite': sqlite3.sqlite_version, 'platform': platform.platform(),
    'node': subprocess.check_output(['node', '--version'], text=True).strip(),
    'npm': subprocess.check_output(['npm', '--version'], text=True).strip(),
    'test_sha256': digest(test_path),
    'assert_count': sum(isinstance(node, ast.Assert) for node in ast.walk(test_tree)),
    'tracked_manifest_sha256': hashlib.sha256(
        json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
    'tracked_file_sha256': manifest,
    'environment': {key: os.environ.get(key) for key in (
        'DEVCONTAINER', 'PATH', 'PYTHONPATH', 'PYTHONPYCACHEPREFIX',
        'TMPDIR', 'UV_PYTHON', 'UV_CACHE_DIR', 'UV_LINK_MODE',
        'UV_PROJECT_ENVIRONMENT', 'CAMCTL_TIMING_REPO', 'CAMCTL_TIMING_DATA',
        'CAMCTL_INTEGRATION_PROGRESS')},
}
production = expected
relative_changes = git('diff', '--name-only', production).splitlines()
identity['shared_production_commit'] = production
identity['changes_from_shared_production'] = relative_changes
identity['production_files_identical'] = not git('diff', '--name-only', production, '--', 'apps', 'protocol', 'scripts', '.agents', 'AGENTS.md')
baseline = subprocess.check_output(['git','show','3fcc19004845b5e96f2af0bce4045db959972174:tests/integration/test_action6_segmented_recovery_roundtrip.py'], cwd=REPO, text=True)
original_asserts = Counter(ast.dump(n) for n in ast.walk(ast.parse(baseline)) if isinstance(n, ast.Assert))
current_asserts = Counter(ast.dump(n) for n in ast.walk(test_tree) if isinstance(n, ast.Assert))
identity['original_assert_count'] = sum(original_asserts.values())
identity['original_asserts_all_preserved'] = not (original_asserts - current_asserts)
identity['matches_expected_commit'] = head == expected
identity['original_test_preserved'] = (
    identity['original_assert_count'] == 127 and identity['original_asserts_all_preserved']
    and identity['assert_count'] == 128)
if moment == 'after':
    before = json.loads((directory / 'source-before.json').read_text())
    identity['tracked_source_unchanged'] = manifest == before['tracked_file_sha256']
(directory / f'source-{moment}.json').write_text(
    json.dumps(identity, ensure_ascii=False, indent=2) + '\n')
assert head == expected, (head, expected)
assert not status, status
assert identity['original_test_preserved']
assert identity['production_files_identical']
assert relative_changes == [], relative_changes
assert identity.get('tracked_source_unchanged', True)
print(json.dumps({key: identity[key] for key in (
    'repo', 'head', 'branch', 'status', 'assert_count', 'test_sha256',
    'matches_expected_commit', 'original_test_preserved')}, ensure_ascii=False))
