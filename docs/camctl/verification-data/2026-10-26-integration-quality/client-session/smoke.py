import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

workspace = Path('/tmp/camctl-quality-client-session-20261026')
sys.path.insert(0, str(workspace / 'tests/integration'))
from _client_session import ClientSession, SessionError

root = Path('/tmp/camctl-quality-evidence/client-session/smoke-locked')
root.mkdir()
world = root / 'world'
world.mkdir()
reports = root / 'reports'
reports.mkdir()
body = root / 'body.json'
body.write_text(json.dumps({'name': '共享会话验证', 'actions': [{'name': '报告', 'type': 'report_status', 'params': {'scope': 'full'}}]}))
media_bytes = b'client-session-real-media'
report = json.loads(next((workspace / 'protocol/examples/client-protocol/01-success').glob('status-report-1-*.json')).read_text())
for action in report['plans'][0]['actions']:
    for output in action.get('outputs', []):
        output.update(size=len(media_bytes), checksum={'status': 'available', 'sha256': hashlib.sha256(media_bytes).hexdigest()})
    for delivery in action.get('deliveries', []):
        delivery.update(size=len(media_bytes), sha256=hashlib.sha256(media_bytes).hexdigest())
report_bytes = json.dumps(report, ensure_ascii=False, separators=(',', ':')).encode()
report_path = reports / f"status-report-1-{hashlib.sha256(report_bytes).hexdigest()}.json"
report_path.write_bytes(report_bytes)
media_path = root / '1.mp4'
media_path.write_bytes(media_bytes)
command = ['node', '--import', 'tsx', str(workspace / 'tests/integration/client_driver.ts')]
env = os.environ.copy()
env['TMPDIR'] = '/tmp/camctl-quality-client-session-temp'
started = time.perf_counter()
session = ClientSession(command, cwd=workspace / 'apps/client', env=env, root=world, timeout=120)
try:
    store = world / 'client-store'
    exported = session.call('export', str(store), str(body), str(root / 'plan-1.json'), output=root / 'export-1.json')
    assert not exported['has_ack']
    imported = session.call('import', str(reports), str(store), output=root / 'import-1.json')
    assert imported['saved_report_ids'] == ['1'] and imported['ack_id'] == '1' and imported['coverage'] == 20
    media = session.call('import-media', str(store), str(media_path), output=root / 'media-1.json')
    assert (media['delivery_id'], media['output_id'], media['import']['status']) == ('1', '4', 'verified')
    exported = session.call('export', str(store), str(body), str(root / 'plan-2.json'), output=root / 'export-2.json')
    assert exported['has_ack'] and exported['last_report_id'] == '1'
    metrics = json.loads(session.node_metrics.read_text())
    assert metrics['counts'] == {'node_process_starts': 1, 'application_starts': 1, 'files_starts': 1, 'http_server_starts': 0, 'browser_starts': 0}, metrics
    # 保留既有HB省略store的浏览器命令，再切回完整原export参数。
    browser = session.call('browser-plan-export', str(body), str(root / 'browser-plan.json'), output=root / 'browser-export.json')
    assert browser['last_report_id'] == '1'
    exported = session.call('export', str(store), str(body), str(root / 'plan-3.json'), output=root / 'export-3.json')
    assert exported['last_report_id'] == '1'
finally:
    session.close()
assert session.state == 'closed' and session.process.returncode == 0
metrics = json.loads(session.node_metrics.read_text())
assert metrics['counts'] == {'node_process_starts': 1, 'application_starts': 3, 'files_starts': 3, 'http_server_starts': 1, 'browser_starts': 1}, metrics
assert metrics['status'] == 'closed' and all(not item['active'] for item in metrics['browser_processes'])
assert json.loads(session.python_metrics.read_text())['phase_timeout_seconds'] == 120

# 原独立命令继续按同一完整签名运行，媒体仍走真实Files.upload。
standalone = root / 'standalone-store'
for operation, args, receipt in [
    ('export', [standalone, body, root / 'standalone-plan.json'], root / 'standalone-export.json'),
    ('import', [reports, standalone], root / 'standalone-import.json'),
    ('import-media', [standalone, media_path], root / 'standalone-media.json'),
]:
    result = subprocess.run([*command, operation, *map(str, args), str(receipt)], cwd=workspace / 'apps/client', env=env, text=True, capture_output=True, timeout=120)
    (root / f'standalone-{operation}.stderr.log').write_text(result.stderr)
    assert result.returncode == 0, result.stderr
    assert receipt.is_file()
assert json.loads((root / 'standalone-import.json').read_text())['ack_id'] == '1'
assert json.loads((root / 'standalone-media.json').read_text())['delivery_id'] == '1'

for operation, args in [('export', [root / 'wrong-store', body, root / 'invalid-plan.json']), ('unknown-operation', [])]:
    failed = ClientSession(command, cwd=workspace / 'apps/client', env=env, root=root / f'failed-{operation}', timeout=120)
    try:
        failed.call(operation, *map(str, args), output=root / f'failed-{operation}.json')
    except SessionError:
        assert failed.state == 'failed' and failed.process.poll() is not None
        assert json.loads(failed.python_metrics.read_text())['cleanup']['survivors'] == []
    else:
        raise AssertionError('非法会话操作未拒绝')
(root / 'smoke-result.json').write_text(json.dumps({'passed': True, 'wall_seconds': time.perf_counter() - started, 'cases': ['真实共享export/import/import-media与ACK', '单Application/Files且零HTTP/browser', '既有HB省略store的browser-plan-export', 'headless/browser/headless明确关闭重建', '原独立三操作', 'store不匹配与未知操作的错误回执和进程收场'], 'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=workspace, text=True).strip()}, ensure_ascii=False, indent=2) + '\n')
print((root / 'smoke-result.json').read_text())
