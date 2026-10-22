"""默认范围按已保存序号续读，不从产物编号推断设备顺序。"""

from contextlib import closing
import sqlite3
from unittest.mock import create_autospec

from camctl.outputs.catalog import OutputKind
from camctl.outputs.sources import CatalogEntry, SelectionMode
from camctl.persistence.repositories import segment_selections
from camctl.persistence.repositories.outputs import _CatalogMember


def test_scope_order_and_unavailable_member_do_not_select_outside_scope(monkeypatch):
    with closing(sqlite3.connect(":memory:")) as database:
        database.execute(
            "CREATE TABLE segment_scope_members (set_id INTEGER, ordinal INTEGER, output_id INTEGER)"
        )
        database.executemany(
            "INSERT INTO segment_scope_members VALUES (1,?,?)",
            ((1, 50), (2, 2), (3, 19)),
        )
        family = create_autospec(
            segment_selections._family,
            side_effect=lambda db, identity, saved: (
                _CatalogMember(
                    {},
                    CatalogEntry(
                        identity,
                        OutputKind.ORIGINAL,
                        3 if identity == 2 else 1,
                        size_bytes=10,
                    ),
                ),
            ),
        )
        monkeypatch.setattr(segment_selections, "_family", family)
        selected = list(
            segment_selections.items(
                database,
                {
                    "error": None,
                    "source": {"id": 1},
                    "mode": SelectionMode.DEFAULT,
                    "setrow": {"id": 1},
                    "batch": 2,
                    "saved": None,
                },
            )
        )
        assert [item.output_id for item in selected] == [50, 2, 19]
        assert [item.status for item in selected] == [2, 4, 2]
        assert [call.args[1] for call in family.call_args_list] == [50, 2, 19]
