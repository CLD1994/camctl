"""跨组件测试使用的 host-demo 构建配方和本轮共享构建。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import time


def build_steps(source: str, build: str) -> tuple[str, str]:
    """直接运行和本轮共享构建使用同一份 Release 配方。"""
    source, build = shlex.quote(source), shlex.quote(build)
    return (f"cmake -S {source} -B {build} -DCMAKE_BUILD_TYPE=Release >/dev/null",
            f"cmake --build {build} --target host-demo -j4 >/dev/null")


def build(source: str, destination: str, run) -> None:
    for step in build_steps(source, destination):
        result = run(step)
        if result.returncode:
            raise RuntimeError(f"host-demo 构建步骤失败: {step}\n{result.stderr}")


def copy_round_build(source: Path, shared: Path, destination: Path, receipt: Path, run) -> None:
    """首次请求时构建，持锁复制两个产物到调用方独占目录。"""
    import fcntl

    shared.mkdir(parents=True, exist_ok=True)
    with (shared / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = shared / 'state'
        if not state.exists():
            # 进程意外退出也留下未完成状态，其他请求不得把残留视为成功。
            state.write_text('building', encoding='utf-8')
            started = time.monotonic()
            started_unix = time.time()

            def record(status):
                temporary = receipt.with_suffix('.tmp')
                temporary.write_text(json.dumps({'status': status, 'build_count': 1,
                                                  'started_unix_seconds': started_unix,
                                                  'wall_seconds': time.monotonic() - started}), encoding='utf-8')
                os.replace(temporary, receipt)

            record('building')
            try:
                build(str(source), str(shared / 'build'), run)
                for name in ('host-demo', 'libcamctl_host.a'):
                    artifact = shared / 'build' / name
                    artifact.chmod(artifact.stat().st_mode & ~0o222)
                record('ready')
                state.write_text('ready', encoding='utf-8')
            except BaseException:
                record('failed')
                raise
        if state.read_text(encoding='utf-8') != 'ready':
            raise RuntimeError('本轮 host-demo 构建未完成或失败')
        destination.mkdir(parents=True, exist_ok=False)
        for name in ('host-demo', 'libcamctl_host.a'):
            target = destination / name
            shutil.copyfile(shared / 'build' / name, target)
            mode = (shared / 'build' / name).stat().st_mode
            target.chmod(stat.S_IMODE(mode) | stat.S_IWUSR)
