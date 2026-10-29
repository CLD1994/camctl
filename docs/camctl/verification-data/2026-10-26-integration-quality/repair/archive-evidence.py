"""归档本次修复的原始验证资料，保留字节及来源摘要。"""
from pathlib import Path
import gzip
import hashlib
import json
import os
import shutil

SOURCE = Path('/tmp/camctl-quality-repair-evidence')
DEST = Path('/opt/devcontainer/.codex/worktrees/de38/camctl/docs/camctl/verification-data/2026-10-26-integration-quality')
EXCLUDE_DIRS = {
    'node_modules', 'npm-cache', 'uv-cache', '__pycache__', 'pycache', 'tmp',
    'pytest-cache', '.pytest_cache', 'snapshot', 'workers', 'data', '.venv',
    '.venv311', 'control', 'cache', 'copied-staging', 'source', 'cases',
}
EXCLUDE_FILES = {'controlled-clock-reads.jsonl'}
EXTENSIONS = {'.json', '.jsonl', '.log', '.xml', '.md', '.py', '.sh', '.txt',
              '.exit', '.gz', '.diff', '.patch', '.db', '.sqlite', '.sql'}

def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()

manifest_path = DEST/'evidence-index.json'
manifest = json.loads(manifest_path.read_text())
old = {item['path']: item for item in manifest['files']}
added = []
omitted = []
for directory, children, files in os.walk(SOURCE):
    children[:] = sorted(name for name in children if name not in EXCLUDE_DIRS)
    for name in sorted(files):
        path = Path(directory)/name
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(SOURCE)
        if name in EXCLUDE_FILES:
            omitted.append(dict(path=str(relative), bytes=path.stat().st_size,
                                sha256=digest(path), reason='重复时钟读取日志；保留相关阶段、命令与实际读取同步证据。'))
            continue
        if path.suffix not in EXTENSIONS and '.log.' not in name:
            continue
        size = path.stat().st_size
        compressed = (path.suffix in {'.db', '.sqlite'}
                      or (size >= 16*1024 and path.suffix in {'.log', '.jsonl', '.xml'})
                      or (size >= 256*1024 and path.suffix != '.gz'))
        archive_relative = Path('repair')/relative
        if compressed:
            previous = old.pop(str(archive_relative), None)
            if previous is not None:
                (DEST/archive_relative).unlink(missing_ok=True)
            archive_relative = Path(str(archive_relative)+'.gz')
        target = DEST/archive_relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name+'.archive-tmp')
        if compressed:
            with path.open('rb') as source, temporary.open('wb') as raw:
                with gzip.GzipFile(fileobj=raw, mode='wb', filename='', mtime=0) as archive:
                    shutil.copyfileobj(source, archive)
        else:
            shutil.copyfile(path, temporary)
        temporary.replace(target)
        item = dict(path=str(archive_relative), original_path=str(path),
                    bytes=target.stat().st_size, sha256=digest(target))
        if compressed:
            item.update(encoding='gzip', original_bytes=size, original_sha256=digest(path))
        old[item['path']] = item
        added.append(item)
notes = SOURCE/'archive-notes.json'
notes.write_text(json.dumps(dict(
    description='缓存、依赖、完整源码快照、实际媒体与可重建运行目录不纳入验证资料；原始业务日志和XML不改写。',
    omitted_repetitive_clock_logs=omitted,
), ensure_ascii=False, indent=2)+'\n')
target = DEST/'repair/archive-notes.json'
shutil.copyfile(notes, target)
old['repair/archive-notes.json'] = dict(path='repair/archive-notes.json', original_path=str(notes),
    bytes=target.stat().st_size, sha256=digest(target))
manifest['description'] = '集成测试质量审计、各历史同步基线及当前生产修复与测试适配的逐测证据；原始字节与源码身份分别登记。'
manifest['files'] = sorted(old.values(), key=lambda item: item['path'])
manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n')
for item in manifest['files']:
    target = DEST/item['path']
    assert target.stat().st_size == item['bytes'] and digest(target) == item['sha256'], item['path']
    if item.get('encoding') == 'gzip':
        total = 0
        actual = hashlib.sha256()
        with gzip.open(target, 'rb') as archive:
            for block in iter(lambda: archive.read(1024*1024), b''):
                total += len(block)
                actual.update(block)
        assert total == item['original_bytes'] and actual.hexdigest() == item['original_sha256'], item['path']
print(json.dumps(dict(new_files=len(added)+1, total_files=len(manifest['files']),
    new_archive_bytes=sum(item['bytes'] for item in added),
    repetitive_clock_logs_omitted=len(omitted)), ensure_ascii=False))
