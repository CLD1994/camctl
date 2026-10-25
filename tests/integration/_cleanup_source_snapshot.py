"""清理分片内的已完成 OSMO 来源；同一路径上恢复完整独立世界。"""

from contextlib import closing, contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import time

import pytest

from _cleanup_roundtrip_support import ARTIFACTS, World, json_write, records, rows, sha


def _inventory(root):
    result = {}
    for path in (root, *sorted(root.rglob("*"))):
        assert not path.is_symlink(), f"来源快照包含符号链接：{path}"
        entry = {"mode": stat.S_IMODE(path.stat().st_mode)}
        if path.is_dir():
            entry["type"] = "directory"
        else:
            assert path.is_file(), f"来源快照包含未结束的特殊文件：{path}"
            entry.update(type="file", size=path.stat().st_size,
                         sha256=sha(path.read_bytes()))
        result[path.relative_to(root).as_posix()] = entry
    return result


def _digest(value):
    return sha(json.dumps(value, sort_keys=True, ensure_ascii=False).encode())


def _database_digest(path, *, immutable=False):
    suffix = "&immutable=1" if immutable else ""
    with closing(sqlite3.connect(f"file:{path}?mode=ro{suffix}", uri=True)) as connection:
        assert connection.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        return sha("\n".join(connection.iterdump()).encode())


def _root_users(root):
    """核对本世界的实际 cwd、命令参数和文件描述符，不按进程名猜测。"""
    prefix = str(root) + os.sep
    found = []

    def belongs(value):
        return value == str(root) or value.startswith(prefix)

    for process in Path("/proc").glob("[0-9]*"):
        try:
            reasons = []
            cwd = os.readlink(process / "cwd")
            if belongs(cwd):
                reasons.append({"cwd": cwd})
            for argument in (process / "cmdline").read_bytes().split(b"\0"):
                text = os.fsdecode(argument)
                if belongs(text):
                    reasons.append({"argument": text})
            for descriptor in (process / "fd").iterdir():
                try:
                    target = os.readlink(descriptor)
                except FileNotFoundError:
                    continue
                if belongs(target):
                    reasons.append({"fd": descriptor.name, "target": target})
        except (FileNotFoundError, ProcessLookupError):
            continue
        if reasons:
            found.append({"pid": int(process.name), "uses": reasons})
    return found


class CompletedOsmoSource:
    def __init__(self, package, host_demo, parent):
        self.parent = parent
        self.root = parent / "world"
        self.baseline = parent / "baseline"
        self.package = package
        self.host_demo = host_demo
        self.consumers = []
        self.busy = False
        started = time.monotonic()
        with pytest.MonkeyPatch.context() as patch:
            self.template = World(package, parent, host_demo, patch)
            try:
                # 最后的普通 host 由每个消费者分别启动；来源原断言全部执行。
                self.template.prepare_osmo(start_host=False)
            finally:
                self.template.stop()
            assert self.template.host is None
            patch.chdir(parent)
            assert _root_users(self.root) == []
        self.producer_seconds = time.monotonic() - started
        self.databases = [Path("deployment/state.db"), Path("client-store/client.db")]
        assert all((self.root / path).is_file() for path in self.databases)
        sidecars = {self.root / (str(path) + suffix)
                    for path in self.databases for suffix in ("-wal", "-shm", "-journal")}
        shutil.copytree(self.root, self.baseline, ignore=lambda directory, names: [
            name for name in names if Path(directory) / name in sidecars])
        self.database_digests = {}
        for relative in self.databases:
            source, target = self.root / relative, self.baseline / relative
            expected = _database_digest(source)
            target.unlink()
            with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as reader:
                with closing(sqlite3.connect(target)) as writer:
                    reader.backup(writer)
                    # 备份已包含全部提交的 WAL；副本不携带旧连接的旁文件。
                    assert writer.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
            assert _database_digest(target, immutable=True) == expected
            self.database_digests[relative.as_posix()] = expected
        self.restore_inventory = _inventory(self.baseline)
        for relative, entry in self.restore_inventory.items():
            (self.baseline / relative).chmod(entry["mode"] & ~0o222)
        self.readonly_inventory = _inventory(self.baseline)
        calls = records(self.root / "adb.jsonl")
        stop, = [call for call in calls if call.get("kind") == "stop"]
        directory = next(call for call in calls if call.get("kind") == "directory"
                         and call["started_ns"] > stop["returned_ns"])
        self.proof = {
            "producer_runs": 1, "producer_seconds": self.producer_seconds,
            "root": str(self.root), "params": deepcopy(self.template.params),
            "clock": json.loads(self.template.clock.read_text()),
            "stop_to_directory_ns": directory["started_ns"] - stop["returned_ns"],
            "database_digests": self.database_digests,
            "baseline_digest": _digest(self.readonly_inventory),
            "restore_inventory": self.restore_inventory,
            "source_actions": rows(self.template.db, "SELECT * FROM actions"),
            "bindings": rows(self.template.db, "SELECT * FROM database_metadata"),
            "producer_processes": [json.loads(path.read_text())
                                   for path in sorted(self.root.glob("settled-*.json"))],
        }
        assert self.proof["stop_to_directory_ns"] >= 30_000_000_000
        self._assert_baseline()
        json_write(parent / "source-proof.json", self.proof)

    def _assert_baseline(self):
        assert _inventory(self.baseline) == self.readonly_inventory

    @contextmanager
    def consumer(self, tmp_path, monkeypatch):
        assert not self.busy, "同一完成来源只能串行恢复消费者"
        self.busy = True
        started = time.monotonic()
        world = None
        evidence = {"case": tmp_path.name,
                    "nodeid": os.environ.get("PYTEST_CURRENT_TEST", "").rsplit(" (", 1)[0],
                    "baseline_digest": self.proof["baseline_digest"]}
        try:
            # 删除前离开旧目录；相同 pathname 重建后仍须重新进入其新 inode。
            monkeypatch.chdir(self.parent)
            assert _root_users(self.root) == []
            self._assert_baseline()
            shutil.rmtree(self.root)
            shutil.copytree(self.baseline, self.root)
            for relative, entry in self.restore_inventory.items():
                (self.root / relative).chmod(entry["mode"])
            assert _inventory(self.root) == self.restore_inventory
            for relative, expected in self.database_digests.items():
                assert _database_digest(self.root / relative, immutable=True) == expected
            world = World(self.package, self.parent, self.host_demo, monkeypatch,
                          prepared=self.template, case_id=tmp_path.name)
            assert Path.cwd().samefile(self.root)
            assert world.env == self.template.env and world.env is not self.template.env
            assert world.sources is not self.template.sources
            assert world.params is not self.template.params
            assert world.params["exposure"] is not self.template.params["exposure"]
            assert world.deployment is not self.template.deployment
            assert world.deployment.run_client_driver.__self__ is world
            assert world.config_extra == "" and world.frozen == {} and world.members == {}
            assert not any(hasattr(world, name) for name in (
                "copies", "frozen_history", "frozen_transactions"))
            assert not world.marker.exists() and not world.manifest.exists()
            assert world.calls() == [] and rows(world.db, "SELECT id FROM cleanup_items") == []
            assert json.loads(world.clock.read_text()) == self.proof["clock"]
            assert world.sources == self.template.sources
            evidence["restore_seconds"] = time.monotonic() - started
            with world:
                world.start()
                yield world
        finally:
            monkeypatch.chdir(self.parent)
            try:
                assert _root_users(self.root) == []
                if world is not None:
                    # 每例保留完整世界；下一次恢复不能覆盖故障门、DB 或调用日志。
                    shutil.copytree(self.root, tmp_path / "world")
                    evidence.update(consumer_seconds=time.monotonic() - started,
                                    final_inventory_digest=_digest(_inventory(self.root)))
                    json_write(tmp_path / "source-reuse.json", evidence)
                    self.consumers.append(evidence)
                self._assert_baseline()
            finally:
                self.busy = False

    def archive(self):
        assert not self.busy and _root_users(self.root) == []
        self._assert_baseline()
        target = ARTIFACTS / (self.parent.name + "-completed-source")
        shutil.copytree(self.baseline, target / "baseline")
        json_write(target / "source-proof.json", self.proof)
        json_write(target / "consumers.json", self.consumers)


@pytest.fixture(scope="module")
def cleanup_osmo_source(cleanup_package, host_demo, tmp_path_factory):
    source = CompletedOsmoSource(cleanup_package, host_demo,
                                 tmp_path_factory.mktemp("r6-completed-osmo"))
    try:
        yield source
    finally:
        source.archive()
