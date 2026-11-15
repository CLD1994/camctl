"""部署迁移保全 V9 的事实，并按 V10 权威结构重新打开。"""

import os
import sqlite3
from pathlib import Path

import pytest

from camctl.capture.baseline_models import BaselineReadUnconfirmedSave
from camctl.deployment import migrate_copy
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.history import HistoryRepository
from camctl.persistence.runtime import DatabaseInvalidError, DbConfig, DbOpenMode, open_existing

from .test_baseline_read_facts import _intent
from ..scheduling.test_start_action import _SCHEDULED, owned


def _legacy_v9(path: Path) -> None:
    """将样本库的结构收窄为已发布 V9；保留实际产生的业务事实。"""
    with sqlite3.connect(path) as connection:
        connection.execute('DROP TABLE acceptance_receipts')
        connection.execute('DROP INDEX report_changes_by_sequence')
        connection.execute('DROP INDEX report_changes_by_entity')
        connection.execute('CREATE INDEX report_changes_by_sequence ON report_entity_changes(entity_type, change_seq, entity_id)')
        connection.execute('CREATE INDEX report_changes_by_entity ON report_entity_changes(entity_type, entity_id, change_seq)')
        connection.execute('UPDATE database_metadata SET format_version=9')
        # 仅更改两张表的已发布 CHECK；既有行在重新打开后由 SQLite 校验。
        connection.execute('PRAGMA writable_schema=ON')
        for table, old, new in (
            ('database_metadata', 'format_version IN (1,2,3,4,5,6,7,8,9,10)', 'format_version IN (1,2,3,4,5,6,7,8,9)'),
            ('baseline_read_calls', 'state IN (1,2,3,4)', 'state IN (1,2,3)'),
            ('baseline_read_calls', 'state IN (3,4)', 'state = 3'),
        ):
            sql = connection.execute('SELECT sql FROM sqlite_master WHERE type="table" AND name=?', (table,)).fetchone()[0]
            assert old in sql
            connection.execute('UPDATE sqlite_master SET sql=? WHERE type="table" AND name=?',
                               (sql.replace(old, new), table))
        connection.execute('PRAGMA writable_schema=OFF')
        connection.execute('PRAGMA schema_version=1001')
        connection.commit()


def _rows(path: Path):
    with sqlite3.connect(path) as connection:
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {table: connection.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall()
                for table in tables}


@pytest.mark.asyncio
@pytest.mark.parametrize("old_state", [1, 2, 3])
async def test_deployment_migrates_old_facts_and_keeps_source(owned, tmp_path, old_state):
    intent = await _intent(owned, tmp_path)
    capture = CaptureRepository()
    first = capture.save_baseline_read_intent(intent, intent.call_key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    if old_state == 2:
        from camctl.capture.baseline_models import BaselineReadReturn
        from camctl.contracts.pages import Page
        from camctl.devices.directory import DirectoryCursor, DirectoryRead
        cursor = DirectoryCursor(intent.binding, intent.directories, 1, None)
        request = BaselineReadReturn(first.value.call_id, _SCHEDULED + 1, None,
                                     DirectoryRead(None, None, Page((), cursor)))
        saved = capture.save_baseline_read_return(request, intent.result_save_key, owned)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    elif old_state == 3:
        saved = capture.save_baseline_read_unconfirmed(
            BaselineReadUnconfirmedSave(first.value.call_id, _SCHEDULED + 5), intent.result_save_key, owned)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    history = HistoryRepository(tmp_path / 'state.db')
    boundary = history.current_boundary()
    before_replay = history.restore_entity('action', intent.action_id, boundary)
    owned.connection.close()
    source = tmp_path / 'state.db'
    _legacy_v9(source)
    with sqlite3.connect(source) as connection:
        assert connection.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
        assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
        assert connection.execute('SELECT state FROM baseline_read_calls').fetchall() == [(old_state,)]
        destination = tmp_path / 'copy.db'
        with sqlite3.connect(destination) as target:
            connection.backup(target)
    os.chmod(destination, 0o640)
    before = _rows(source)
    config = tmp_path / 'config.toml'
    config.write_text(f'''[paths]
state_db = "{source}"
log_file = "{tmp_path / 'camctl.log'}"
staging = "/srv/camctl/staging"
ready = "/srv/camctl/ready"
processing = "/srv/camctl/processing"
''')
    result = migrate_copy(config, destination)
    assert result['format_version'] == 10
    assert destination.stat().st_mode & 0o777 == 0o640
    assert _rows(source) == before
    after = _rows(destination)
    assert after.pop('acceptance_receipts') == []
    old_metadata = before.pop('database_metadata')[0]
    new_metadata = after.pop('database_metadata')[0]
    assert new_metadata == (*old_metadata[:3], 10, *old_metadata[4:])
    assert after == before
    reopened = open_existing(destination, DbOpenMode.EXISTING_RO, DbConfig())
    try:
        assert reopened.connection.execute('SELECT state FROM baseline_read_calls').fetchall() == [(old_state,)]
        for index, expected in (
            ('report_changes_by_sequence', ['entity_type', 'change_seq', 'entity_id', 'event_id']),
            ('report_changes_by_entity', ['entity_type', 'entity_id', 'change_seq', 'event_id']),
        ):
            assert [row[2] for row in reopened.connection.execute(f'PRAGMA index_info({index})')] == expected
    finally:
        reopened.connection.close()
    assert HistoryRepository(destination).restore_entity('action', intent.action_id, boundary) == before_replay
    # 再次执行只核验；已有新答复不得被重新初始化或清除。
    with sqlite3.connect(destination) as connection:
        transaction_id = connection.execute('SELECT id FROM history_transactions LIMIT 1').fetchone()[0]
        connection.execute('INSERT INTO acceptance_receipts VALUES (?, 1, ?, ?)',
                           (transaction_id, 'identity', '{"ok":true}'))
    assert migrate_copy(config, destination)['format_version'] == 10
    with sqlite3.connect(destination) as connection:
        assert connection.execute('SELECT transaction_id, result_json FROM acceptance_receipts').fetchall() == [(transaction_id, '{"ok":true}')]


@pytest.mark.parametrize('object_name', ['acceptance_receipts', 'report_changes_by_sequence', 'baseline_read_calls'])
def test_v10_open_rejects_missing_required_structure(tmp_path, object_name):
    from ..support.database import create_test_database
    path = tmp_path / 'state.db'
    create_test_database(path)
    with sqlite3.connect(path) as connection:
        if object_name == 'baseline_read_calls':
            connection.execute('PRAGMA writable_schema=ON')
            sql = connection.execute("SELECT sql FROM sqlite_master WHERE name=?", (object_name,)).fetchone()[0]
            assert 'state IN (1,2,3,4)' in sql
            connection.execute('UPDATE sqlite_master SET sql=? WHERE name=?',
                               (sql.replace('state IN (1,2,3,4)', 'state IN (1,2,3)'), object_name))
            connection.execute('PRAGMA writable_schema=OFF')
            connection.execute('PRAGMA schema_version=1002')
        elif object_name == 'report_changes_by_sequence':
            connection.execute('DROP INDEX report_changes_by_sequence')
            connection.execute('CREATE INDEX report_changes_by_sequence ON report_entity_changes(entity_type, change_seq, entity_id)')
        else:
            connection.execute('DROP TABLE acceptance_receipts')
    with pytest.raises(DatabaseInvalidError):
        open_existing(path, DbOpenMode.EXISTING_RO, DbConfig())


def test_deployment_rejects_source_as_copy_without_changing_it(tmp_path):
    from ..support.database import create_test_database
    source = tmp_path / 'state.db'
    create_test_database(source)
    before = source.read_bytes()
    config = tmp_path / 'config.toml'
    config.write_text(f"[paths]\nstate_db = '{source}'\n")
    with pytest.raises(ValueError, match='独立数据库副本'):
        migrate_copy(config, source)
    assert source.read_bytes() == before
