"""配置来源和可缺省文件快照的隔离单元。"""
import hashlib
import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest

source = Path(__file__).absolute().parents[5] / 'apps/camctl/src/camctl/update_engine/update_transaction.py'
spec = importlib.util.spec_from_file_location('owned_update_transaction_under_test', source)
transaction = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transaction)


def snapshot(source_mode, content):
    with patch.object(Path, 'resolve', return_value=Path('/resolved/config')), \
            patch.object(Path, 'read_bytes', side_effect=content if isinstance(content, Exception) else None,
                         return_value=content):
        return transaction.snapshot_config(Path('/original/config'), source_mode)


@pytest.mark.parametrize('source_mode', [None, '/original/config'])
def test_missing_default_or_explicit_config_is_preserved(source_mode):
    assert snapshot(source_mode, FileNotFoundError()) == {
        'source': source_mode, 'resolved': '/resolved/config', 'present': False, 'sha256': None}


@pytest.mark.parametrize('source_mode', [None, '/original/config'])
def test_existing_config_hashes_bytes_and_keeps_source(source_mode):
    actual = snapshot(source_mode, b'configuration')
    assert actual == {'source': source_mode, 'resolved': '/resolved/config', 'present': True,
                      'sha256': hashlib.sha256(b'configuration').hexdigest()}


def test_config_read_permission_error_is_not_missing():
    with pytest.raises(PermissionError):
        snapshot(None, PermissionError('denied'))


@pytest.mark.parametrize('old,new', [
    (FileNotFoundError(), b'appeared'),
    (b'existing', FileNotFoundError()),
    (b'existing', b'changed'),
])
def test_config_change_cannot_be_claimed_unchanged(old, new):
    original = {'config': '/original/config', 'config_snapshot': snapshot(None, old)}
    with patch.object(Path, 'resolve', return_value=Path('/resolved/config')), \
            patch.object(Path, 'read_bytes', side_effect=new if isinstance(new, Exception) else None,
                         return_value=new):
        with pytest.raises(ValueError, match='配置'):
            transaction.verify_config(original)


def test_config_target_change_is_not_content_equality():
    original = {'config': '/original/config', 'config_snapshot': snapshot(None, b'same')}
    with patch.object(Path, 'resolve', return_value=Path('/another/config')), \
            patch.object(Path, 'read_bytes', return_value=b'same'):
        with pytest.raises(ValueError, match='配置'):
            transaction.verify_config(original)
