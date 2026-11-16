"""为跨组件测试准备标准客户端网页构建。"""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import stat
import subprocess

from round_test_build import build_once, clone_build_source, load_round_context


def prepare_standard_client(project: Path, run) -> None:
    """使用本轮冻结源码构建一次，并为调用方准备独立网页副本。"""
    project = Path(project).resolve()
    context = load_round_context("client-web")
    if context is None:
        run(["npm", "run", "build"], cwd=project)
        return

    def build() -> None:
        source = clone_build_source(context)
        client = source / "apps/client"
        dependencies = (project / "node_modules").resolve(strict=True)
        (client / "node_modules").symlink_to(dependencies, target_is_directory=True)
        run(["npm", "run", "build"], cwd=client)
        web = client / "dist/web"
        if not web.is_dir():
            raise RuntimeError(f"客户端构建没有生成网页目录: {web}")
        artifacts = context.workspace / "artifacts"
        artifacts.mkdir()
        shutil.move(str(web), artifacts / "web")
        for path in (artifacts / "web").rglob("*"):
            path.chmod(path.stat().st_mode & ~0o222)
        (artifacts / "web").chmod((artifacts / "web").stat().st_mode & ~0o222)

    build_once(context.workspace, context.receipt, build, label="client-web")
    destination = project / "dist/web"
    if destination.is_symlink() or destination.is_file():
        destination.unlink()
    elif destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(context.workspace / "artifacts/web", destination)
    for path in (destination, *destination.rglob("*")):
        path.chmod(path.stat().st_mode | stat.S_IWUSR)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    args = parser.parse_args()
    prepare_standard_client(args.project, lambda command, *, cwd: subprocess.run(command, cwd=cwd, check=True))


if __name__ == "__main__":
    main()
