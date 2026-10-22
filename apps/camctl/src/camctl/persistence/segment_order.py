"""临时分段排序索引的连接与清理，不连接正式状态库。"""

from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory


@contextmanager
def temporary_segment_index():
    with (
        TemporaryDirectory(prefix="camctl-segment-order-") as folder,
        closing(sqlite3.connect(str(Path(folder) / "order.db"))) as connection,
    ):
        yield connection
