"""为需要启动版本记录的源码测试准备当前项目的发行元数据。"""

from importlib import metadata
from pathlib import Path
import tomllib


def prepare_project_metadata(tmp_path, monkeypatch):
    project = Path(__file__).resolve().parents[3] / "pyproject.toml"
    with project.open("rb") as stream:
        version = tomllib.load(stream)["project"]["version"]
    metadata_root = tmp_path / "distribution"
    dist_info = metadata_root / f"camctl-{version}.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: camctl\nVersion: {version}\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(metadata_root))
    assert metadata.version("camctl") == version
