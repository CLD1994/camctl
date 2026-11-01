"""更新 ZIP 清单、成员类型和解包边界的隔离单元。"""
import hashlib
import importlib.util
import io
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

import pytest

source = Path(__file__).absolute().parents[5] / 'apps/camctl/src/camctl/update_engine/archive_support.py'
spec = importlib.util.spec_from_file_location('owned_update_archive_under_test', source)
module = importlib.util.module_from_spec(spec)
support = ModuleType('bundle_support')
support.WHEEL_TARGETS = {'x86_64': ('x86',), 'aarch64': ('arm',)}
with patch.dict(sys.modules, {'bundle_support': support}):
    spec.loader.exec_module(module)


def info(name, mode=0o100644):
    return SimpleNamespace(filename=name, external_attr=mode << 16, is_dir=lambda: False)


def fake_archive(members, contents):
    archive = Mock()
    archive.infolist.return_value = members
    archive.read.side_effect = lambda item: contents[item.filename]
    archive.open.side_effect = lambda item: io.BytesIO(contents[item.filename])
    return archive


def valid_contents(extra=None):
    files = {'RELEASE.json': b'{}', 'source-manifest.json': b'{}', 'camctl/requirements.txt': b'requirements',
             'camctl/camctl-0.3.0-py3-none-any.whl': b'wheel',
             'camctl/wheelhouse/x86_64/dependency.whl': b'x86',
             'camctl/wheelhouse/aarch64/dependency.whl': b'arm'}
    files.update(extra or {})
    checksum = ''.join(hashlib.sha256(data).hexdigest() + '  ' + name + '\n'
                       for name, data in sorted(files.items())).encode()
    files['SHA256SUMS'] = checksum
    return files


def bundle_fixture(files):
    members = [info('camctl-update/' + name) for name in files]
    return members, {'camctl-update/' + name: data for name, data in files.items()}


def test_valid_archive_checks_file_set_and_every_digest():
    members, contents = bundle_fixture(valid_contents())
    archive = fake_archive(members, contents)
    with patch.object(module.zipfile, 'ZipFile', return_value=archive):
        with module.UpdateArchive(Path('/bundle.zip')) as bundle:
            bundle.verify()
            assert bundle.wheel_name() == 'camctl/camctl-0.3.0-py3-none-any.whl'
    archive.close.assert_called_once()
    assert archive.open.call_count == len(members) - 1


@pytest.mark.parametrize('members', [
    [info('camctl-update/../outside')],
    [info('camctl-update/a'), info('camctl-update/a')],
    [info('camctl-update/link', 0o120777)],
    [info('/camctl-update/absolute')],
    [info('camctl-update/a//b')],
    [info('camctl-update/a'), info('camctl-update/a/b')],
])
def test_archive_rejects_unsafe_or_duplicate_members_before_reading(members):
    archive = fake_archive(members, {})
    with patch.object(module.zipfile, 'ZipFile', return_value=archive):
        with pytest.raises(ValueError):
            module.UpdateArchive(Path('/bundle.zip'))
    archive.close.assert_called_once()
    archive.read.assert_not_called()


def test_archive_rejects_content_hash_mismatch():
    files = valid_contents()
    files['camctl/camctl-0.3.0-py3-none-any.whl'] = b'changed'
    members, contents = bundle_fixture(files)
    archive = fake_archive(members, contents)
    with patch.object(module.zipfile, 'ZipFile', return_value=archive):
        with module.UpdateArchive(Path('/bundle.zip')) as bundle:
            with pytest.raises(ValueError, match='摘要'):
                bundle.verify()


def test_archive_rejects_listed_non_update_content():
    members, contents = bundle_fixture(valid_contents({'host/program': b'host'}))
    archive = fake_archive(members, contents)
    with patch.object(module.zipfile, 'ZipFile', return_value=archive):
        with module.UpdateArchive(Path('/bundle.zip')) as bundle:
            with pytest.raises(ValueError, match='不属于'):
                bundle.verify()


def test_archive_rejects_unlisted_member():
    members = [info('camctl-update/SHA256SUMS'), info('camctl-update/unlisted')]
    archive = fake_archive(members, {members[0].filename: b'', members[1].filename: b'data'})
    with patch.object(module.zipfile, 'ZipFile', return_value=archive):
        with module.UpdateArchive(Path('/bundle.zip')) as bundle:
            with pytest.raises(ValueError, match='集合'):
                bundle.verify()
    archive.open.assert_not_called()


def test_extract_writes_only_exclusive_files_under_owned_destination():
    member = info('camctl-update/camctl/payload.whl')
    archive = fake_archive([member], {member.filename: b'payload'})
    destination, target = Mock(), Mock()
    destination.joinpath.return_value = target
    output = Mock()
    context = Mock()
    context.__enter__ = Mock(return_value=output)
    context.__exit__ = Mock(return_value=False)
    target.open.return_value = context
    with patch.object(module.zipfile, 'ZipFile', return_value=archive):
        with module.UpdateArchive(Path('/bundle.zip')) as bundle:
            bundle.extract(destination)
    destination.mkdir.assert_called_once_with()
    destination.joinpath.assert_called_once_with('camctl', 'payload.whl')
    target.parent.mkdir.assert_called_once_with(parents=True, exist_ok=True)
    target.open.assert_called_once_with('xb')
    output.write.assert_called_once_with(b'payload')
