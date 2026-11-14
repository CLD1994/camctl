"""以内存源码检验仓储事务与连接审计。"""

import pytest

from support.repository_audit import _READ_TRANSACTION_FUNCTIONS, audit_repositories

@pytest.mark.parametrize('owner', sorted(_READ_TRANSACTION_FUNCTIONS))
@pytest.mark.parametrize('statement', ['BEGIN', 'ROLLBACK', 'BEGIN IMMEDIATE', 'COMMIT'])
def test_read_transaction_allowance_rejects_write_lock(owner, statement):
    if owner.startswith('module:'):
        source = f'def {owner.split(":", 1)[1]}():\n    connection.execute({statement!r})\n'
    else:
        cls, function = owner.split('.')
        source = f'class {cls}:\n    def {function}(self):\n        connection.execute({statement!r})\n'
    audit = lambda: audit_repositories({"repository.py": source})
    if statement in ('BEGIN', 'ROLLBACK', 'COMMIT'):
        audit()
    else:
        with pytest.raises(AssertionError):
            audit()


def test_repository_connection_commit_is_rejected_as_direct_connection():

    source = "import sqlite3\ndef bypass():\n    sqlite3.connect('state.db').commit()\n"
    audit = lambda: audit_repositories({"repository.py": source})
    with pytest.raises(AssertionError, match="仓储直连数据库"):
        audit()


@pytest.mark.parametrize("source,accepted", [
    ("def save(owned):\n    owned.connection.commit()\n", False),
    ("def save(atomic_transaction):\n    atomic_transaction.commit()\n", True),
])
def test_repository_commit_audit_checks_explicit_connection(source, accepted):

    audit = lambda: audit_repositories({"repository.py": source})
    if accepted:
        audit()
    else:
        with pytest.raises(AssertionError, match="仓储直接提交连接"):
            audit()


def test_rejects_transaction_in_unregistered_owner():
    with pytest.raises(AssertionError, match="白名单外"):
        audit_repositories({"repository.py": "class Other:\n    def save(self):\n        connection.execute('BEGIN')\n"})
