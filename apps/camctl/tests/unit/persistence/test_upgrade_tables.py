"""升级保全原表，只允许已登记且为空的新表。"""

import pytest

from camctl.persistence.upgrade_tables import validate_upgrade_tables


@pytest.mark.parametrize("added", [set(), {"batches"}])
def test_exact_old_tables_survive_version_change(added):
    validate_upgrade_tables(original={"actions", "events"},
        target={"actions", "events"} | added, declared_added=added, nonempty_added=set())


@pytest.mark.parametrize("original,target,added,nonempty", [
    ({"actions", "events"}, {"actions", "batches"}, {"batches"}, set()),
    ({"actions"}, {"actions", "foreign"}, {"batches"}, set()),
    ({"actions"}, {"actions", "batches", "foreign"}, {"batches"}, set()),
    ({"actions"}, {"actions", "batches"}, {"batches"}, {"batches"}),
    ({"actions", "batches"}, {"actions", "batches"}, {"batches"}, set()),
])
def test_upgrade_rejects_dropped_unknown_nonempty_or_premature_tables(original, target, added, nonempty):
    with pytest.raises(ValueError):
        validate_upgrade_tables(original=original, target=target,
            declared_added=added, nonempty_added=nonempty)
