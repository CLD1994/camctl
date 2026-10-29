from __future__ import annotations
import hashlib
import json
import shutil
from pathlib import Path

REPO = Path('/opt/devcontainer/.codex/worktrees/de38/camctl')
DEST = REPO / 'docs/camctl/verification-data/2026-10-25-integration-failure-repairs'
RESULTS = [Path('/tmp/camctl-fix44-'+name+'-results.json') for name in [
    'components','osmo','osmo-reading-context','clock','browser','process','media',
    'total-changes','cleanup','read','media-clock','history-signature',
]]
RESULTS += [Path('/tmp/camctl-fix44-browser-followup-20261025/results.json')]
RESULTS += [Path('/tmp/camctl-fix44-browser-final-waits-20261025/results.json')]
RESULTS += [Path('/tmp/camctl-fix44-browser-final-waits-20261025/phase-diagnosis.json'),Path('/tmp/camctl-fix44-browser-final-waits-20261025/strict-count-review.json')]
REVIEWS = [Path('/tmp/camctl-fix44-'+name+'-independent-review.json') for name in ['cleanup','read']]
REVIEWS += [Path('/tmp/camctl-fix44-mainline-compatibility.json')]
REVIEWS += [Path('/tmp/camctl-fix44-browser-coverage-review.json')]
SUFFIXES = {'.json','.jsonl','.log','.xml','.diff','.csv','.txt','.md'}
entries = {}
skipped = []

def archive(source: Path, relative: Path):
    target = DEST / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    content = target.read_bytes()
    entries[relative.as_posix()] = {
        'source_path': str(source), 'archive_path': relative.as_posix(),
        'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest(),
    }

def paths(value):
    if isinstance(value, dict):
        for child in value.values(): yield from paths(child)
    elif isinstance(value, list):
        for child in value: yield from paths(child)
    elif isinstance(value, str) and value.startswith('/tmp/camctl-fix44-'):
        yield Path(value)

for receipt in RESULTS + REVIEWS:
    if not receipt.exists():
        skipped.append({'path':str(receipt),'reason':'尚未生成收据'})
        continue
    special={'camctl-fix44-browser-followup-20261025':'browser-followup-results.json','camctl-fix44-browser-final-waits-20261025':'browser-final-waits-results.json'}
    name=special.get(receipt.parent.name,receipt.name) if receipt.name=='results.json' else receipt.name.removeprefix('camctl-fix44-')
    relative = Path('groups') / name
    archive(receipt,relative)
    data = json.loads(receipt.read_text())
    for source in paths(data):
        if source.is_dir() and (source/'summary.json').is_file():
            for name in ['summary.json','invocation.json']:
                item=source/name
                if item.is_file(): archive(item,Path('raw')/item.relative_to('/tmp'))
            for task in (source/'tasks').glob('*'):
                for name in ['command.json','cases.json','inventory.json','result.json','output.log']:
                    item=task/'artifacts'/name
                    if item.is_file(): archive(item,Path('raw')/item.relative_to('/tmp'))
            continue
        if not source.is_file() or source.is_symlink() or source.suffix not in SUFFIXES:
            continue
        if source == receipt:
            continue
        if source.stat().st_size > 8 * 1024 * 1024:
            skipped.append({'path':str(source),'reason':'超过8MiB；由对应收据保留来源，必要事实另摘录'})
            continue
        archive(source,Path('raw')/source.relative_to('/tmp'))

for directory, label in [
    ('/tmp/camctl-fix44-final-20261025','combined-44-before-media-fix'),
    ('/tmp/camctl-fix44-verified-20261025','combined-mainline'),
    ('/tmp/camctl-fix44-browser-final-20261025','browser-final'),
    ('/tmp/camctl-fix44-related-final-20261025','related-before-mainline'),
    ('/tmp/camctl-fix44-related-mainline-20261025','related'),
]:
    root=Path(directory)
    if not root.exists(): continue
    for name in ['summary.json','invocation.json','source.json','plan.json']:
        source=root/name
        if source.is_file(): archive(source,Path(label)/name)
    for task in (root/'tasks').glob('*'):
        for name in ['command.json','cases.json','inventory.json','result.json','output.log']:
            source=task/'artifacts'/name
            if source.is_file(): archive(source,Path(label)/'tasks'/task.name/name)

selection=Path('/tmp/camctl-fix44-verified-command.json')
if selection.is_file(): archive(selection,Path('combined-mainline')/'selection-command.json')
selection=Path('/tmp/camctl-fix44-browser-final-command.json')
if selection.is_file(): archive(selection,Path('browser-final')/'selection-command.json')

for directory,label in [
    ('/tmp/camctl-fix44-unit-final-20261025','unit-before-resource-preparation'),
    ('/tmp/camctl-fix44-unit-prepared-20261025','unit-before-mainline'),
    ('/tmp/camctl-fix44-unit-mainline-20261025','unit'),
]:
    unit=Path(directory)
    if unit.exists():
        for source in unit.iterdir():
            if source.is_file() and source.suffix in SUFFIXES:
                archive(source,Path(label)/source.name)

for source in [Path('/tmp/camctl-fix44-first-run-cleanup-proof.py'),Path('/tmp/camctl-fix44-build-repair-ledger.py'),Path('/tmp/camctl-archive-fix44.py')]:
    if source.is_file(): archive(source,Path('probes')/source.name)

for name in ['first-run-cleanup-proof.json','repair-ledger.json','repair-ledger.csv','source-identity.json']:
    source=DEST/name
    if source.is_file():
        content=source.read_bytes()
        entries[name]={'source_path':str(source),'archive_path':name,'bytes':len(content),'sha256':hashlib.sha256(content).hexdigest()}

DEST.mkdir(parents=True,exist_ok=True)
manifest={'date':'2026-10-25','method':'保存实际执行收据、原始命令与结果；source_path只供溯源，报告依赖仓库内archive_path。历史失败证据继续由原诊断记录维护。','files':list(entries.values()),'unarchived':skipped}
(DEST/'archive-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'archived_files':len(entries),'bytes':sum(x['bytes'] for x in entries.values()),'unarchived_entries':len(skipped)},ensure_ascii=False))
