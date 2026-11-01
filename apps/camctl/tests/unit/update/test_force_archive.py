"""归档入口使用内存协作者，禁止访问真实部署。"""
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

import pytest

modules = {name: ModuleType(name) for name in ('force_record', 'force_snapshot', 'update_transaction')}
source = Path(__file__).absolute().parents[5] / 'apps/camctl/src/camctl/update_engine/force_archive.py'
spec = importlib.util.spec_from_file_location('force_archive_under_test', source)
archive = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, modules):
    spec.loader.exec_module(archive)


def record(state='updating'):
    return {'state': state, 'output': '/target/updates/' + 'a' * 32,
            'manifest': {'environment': None, 'staging': None, 'ready': None},
            'logs': [], 'layout': {'processing': '/processing'},
            'processing_identity': {'dev': 1}, 'namespaces': {}, 'mutation_intent': True}


class World:
    def __init__(self, state='prepared'):
        self.original = record()
        self.committed = {**self.original, 'state': 'committed'}
        self.job = {'version': 1, 'record': self.original, 'state': state,
                    'sha256': 'a' * 64 if state != 'prepared' else None,
                    'inputs': {'update.log': 'b' * 64, 'receipt.json': 'c' * 64}}
        self.events = []

    def save(self, path, job):
        self.events.append(('save', job['state']))
        self.job = copy.deepcopy(job)

    def read(self, path, **kwargs):
        assert path.name == 'archive-job.json'
        return json.dumps(self.job)

    def setup(self):
        return patch.multiple(archive, _identity=lambda rec: Path(rec['output']),
            _build=lambda *args: self.events.append(('build',)),
            _publish=lambda *args: self.events.append(('publish',)),
            _cleanup=lambda *args: self.events.append(('cleanup',)))

    def collaborators(self):
        return patch.multiple(archive.transaction, create=True,
            sync_file=lambda *args: None, sync_dir=lambda *args: None,
            directory_identity=lambda *args: {'dev': 1}, digest=lambda *args: 'a' * 64,
            atomic_json=self.save)


def test_prepare_freezes_inputs_before_registering_job():
    saved = []
    rec = record()
    with patch.object(archive, '_identity', return_value=Path(rec['output'])), \
         patch.object(archive.force_record, 'classify', create=True, return_value='updating'), \
         patch.object(Path, 'read_text', side_effect=FileNotFoundError), \
         patch.object(Path, 'mkdir'), patch.object(archive, '_copy_sync', side_effect=[True, False]) as copy_input, \
         patch.object(archive.transaction, 'sync_dir', create=True), \
         patch.object(archive.transaction, 'digest', create=True, return_value='d' * 64), \
         patch.object(archive.transaction, 'atomic_json', create=True, side_effect=lambda p, v: saved.append(copy.deepcopy(v))):
        archive.prepare(rec)
    assert copy_input.call_count == 2
    assert saved[0]['state'] == 'prepared'
    assert saved[0]['inputs'] == {'update.log': 'd' * 64, 'receipt.json': None}
    assert saved[0]['record'] == rec


def test_prepare_copy_failure_never_registers_or_reduces_backup():
    rec = record()
    with patch.object(archive, '_identity', return_value=Path(rec['output'])), \
         patch.object(archive.force_record, 'classify', create=True, return_value='updating'), \
         patch.object(Path, 'read_text', side_effect=FileNotFoundError), \
         patch.object(Path, 'mkdir'), patch.object(archive, '_copy_sync', side_effect=OSError('read failed')), \
         patch.object(archive.transaction, 'sync_dir', create=True), \
         patch.object(archive.transaction, 'atomic_json', create=True) as save, \
         patch.object(archive, '_cleanup') as cleanup:
        with pytest.raises(OSError, match='read failed'):
            archive.prepare(rec)
    save.assert_not_called()
    cleanup.assert_not_called()


def test_prepare_existing_job_reestablishes_durability_before_return():
    world = World()
    events = []
    with patch.object(archive, '_identity', return_value=Path(world.original['output'])), \
         patch.object(archive.force_record, 'classify', create=True, return_value='updating'), \
         patch.object(Path, 'read_text', lambda path, **kw: world.read(path, **kw)), \
         patch.object(archive.transaction, 'digest', create=True, side_effect=['b' * 64, 'c' * 64]), \
         patch.object(archive.transaction, 'sync_file', create=True, side_effect=lambda p: events.append(('file', p.name))), \
         patch.object(archive.transaction, 'sync_dir', create=True, side_effect=lambda p: events.append(('dir', p.name))):
        archive.prepare(world.original)
    assert events == [('file', 'update.log'), ('file', 'receipt.json'), ('dir', 'archive-input'),
                      ('file', 'archive-job.json'), ('dir', 'a' * 32)]


def test_prepare_existing_job_propagates_sync_failure():
    world = World()
    with patch.object(archive, '_identity', return_value=Path(world.original['output'])), \
         patch.object(archive.force_record, 'classify', create=True, return_value='updating'), \
         patch.object(Path, 'read_text', lambda path, **kw: world.read(path, **kw)), \
         patch.object(archive.transaction, 'digest', create=True, return_value='b' * 64), \
         patch.object(archive.transaction, 'sync_file', create=True, side_effect=OSError('sync')):
        with pytest.raises(OSError, match='sync'):
            archive.prepare(world.original)


def test_committed_job_publishes_before_completion_and_cleanup():
    world = World()
    with world.setup(), world.collaborators(), patch.object(Path, 'read_text', lambda path, **kw: world.read(path, **kw)), \
         patch.object(archive.force_record, 'classify', create=True, return_value='committed'), \
         patch.object(archive.os, 'replace'):
        archive.finalize(world.committed)
    assert world.events == [('build',), ('save', 'compressed'), ('publish',),
                            ('save', 'complete'), ('cleanup',), ('save', 'cleaned')]


def test_complete_job_retries_cleanup_without_republication():
    world = World('complete')
    with world.setup(), world.collaborators(), patch.object(Path, 'read_text', lambda path, **kw: world.read(path, **kw)), \
         patch.object(archive.force_record, 'classify', create=True, return_value='committed'):
        archive.finalize(world.committed)
        archive.finalize(world.committed)
    assert world.events == [('cleanup',), ('save', 'cleaned')]


def test_publish_failure_retains_compressed_state_and_backup():
    world = World('compressed')
    with world.setup(), world.collaborators(), patch.object(Path, 'read_text', lambda path, **kw: world.read(path, **kw)), \
         patch.object(archive.force_record, 'classify', create=True, return_value='committed'), \
         patch.object(archive, '_publish', side_effect=OSError('fsync')):
        with pytest.raises(OSError, match='fsync'):
            archive.finalize(world.committed)
    assert world.job['state'] == 'compressed'
    assert world.events == []


def test_cleanup_failure_retries_without_republishing():
    world = World('complete')
    with world.setup(), world.collaborators(), patch.object(Path, 'read_text', lambda path, **kw: world.read(path, **kw)), \
         patch.object(archive.force_record, 'classify', create=True, return_value='committed'), \
         patch.object(archive, '_cleanup', side_effect=[OSError('cleanup'), None]) as cleanup:
        with pytest.raises(OSError, match='cleanup'):
            archive.finalize(world.committed)
        archive.finalize(world.committed)
    assert cleanup.call_count == 2
    assert world.events == [('save', 'cleaned')]


def test_commit_and_frozen_record_are_required():
    world = World()
    with world.setup(), world.collaborators(), patch.object(Path, 'read_text', lambda path, **kw: world.read(path, **kw)), \
         patch.object(archive.force_record, 'classify', create=True, side_effect=lambda rec: rec['state']):
        with pytest.raises(ValueError):
            archive.finalize(world.original)
        changed = {**world.committed, 'layout': {'processing': '/elsewhere'}}
        with pytest.raises(ValueError):
            archive.finalize(changed)
    assert world.events == []


def test_manifest_excludes_work_content_even_without_media_suffix():
    content = b'configuration'
    entry = {'kind': 'file', 'mode': 0o600, 'uid': 1, 'gid': 2, 'mtime_ns': 0,
             'xattrs': {}, 'size': len(content),
             'sha256': archive.hashlib.sha256(content).hexdigest(), 'hardlink': 'configuration'}
    manifest = {'configuration': entry, 'staging': {'kind': 'dir'},
                'staging/no-extension': entry, 'ready': {'kind': 'dir'}, 'ready/file': entry}
    data = io.BytesIO()
    opened = []
    def fake_open(path, mode):
        opened.append(str(path))
        return io.BytesIO(content)
    with patch.object(Path, 'open', fake_open):
        with tarfile.open(fileobj=data, mode='w') as tar:
            archive._add_manifest(tar, Path('/backup'), manifest)
    data.seek(0)
    with tarfile.open(fileobj=data, mode='r') as tar:
        assert tar.getnames() == ['objects/configuration']
        assert tar.extractfile('objects/configuration').read() == content
    assert opened == ['/backup/configuration']


def test_existing_published_name_must_have_same_digest():
    output = Path('/target/updates/' + 'a' * 32)
    processing = Path('/processing')
    class Output(io.BytesIO):
        def fileno(self): return 4
        def close(self): pass
    def opened(path, mode):
        return Output() if mode == 'xb' else io.BytesIO(b'archive')
    def lstat(path):
        if path == processing / ('.camctl-force-' + output.name):
            return SimpleNamespace(st_mode=0o40700)
        if path.name == 'archive.tar.gz' and path.parent != processing:
            raise FileNotFoundError(path)
        return SimpleNamespace(st_mode=0o100600)
    with patch.object(Path, 'open', opened), patch.object(Path, 'unlink'), patch.object(Path, 'mkdir'), \
         patch.object(Path, 'lstat', lstat), patch.object(archive.os, 'replace'), \
         patch.object(archive.os, 'link', side_effect=FileExistsError), \
         patch.object(archive.os, 'fsync'), \
         patch.object(archive.transaction, 'sync_dir', create=True), \
         patch.object(archive.transaction, 'digest', create=True, side_effect=['a' * 64, 'b' * 64]):
        with pytest.raises(ValueError, match='内容不同'):
            archive._publish(output, processing, 'archive.tar.gz', 'a' * 64)


def test_publish_reuses_complete_private_copy_and_links_into_processing():
    output = Path('/target/updates/' + 'a' * 32)
    processing = Path('/processing')
    preparation = processing / ('.camctl-force-' + output.name)
    def lstat(path):
        return SimpleNamespace(st_mode=0o40700 if path == preparation else 0o100600)
    with patch.object(Path, 'mkdir'), patch.object(Path, 'lstat', lstat), \
         patch.object(Path, 'open', side_effect=AssertionError('不得重建完整副本')), \
         patch.object(archive.os, 'link') as link, \
         patch.object(archive.transaction, 'sync_file', create=True) as sync_file, \
         patch.object(archive.transaction, 'sync_dir', create=True), \
         patch.object(archive.transaction, 'digest', create=True, return_value='a' * 64):
        archive._publish(output, processing, 'archive.tar.gz', 'a' * 64)
    sync_file.assert_called_once_with(preparation / 'archive.tar.gz')
    link.assert_called_once_with(preparation / 'archive.tar.gz', processing / 'archive.tar.gz',
                                 follow_symlinks=False)
