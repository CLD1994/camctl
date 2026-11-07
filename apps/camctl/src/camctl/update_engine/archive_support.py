"""只读核对更新 ZIP，或在已获准的事务目录中展开其普通文件。"""
import hashlib
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile

from bundle_support import WHEEL_TARGETS


class UpdateArchive:
    def __init__(self, path):
        self.archive = zipfile.ZipFile(path)
        self.members = {}
        try:
            for info in self.archive.infolist():
                name = info.filename
                path_name = PurePosixPath(name)
                mode = info.external_attr >> 16
                if (not name.startswith('camctl-update/') or path_name.is_absolute()
                        or path_name.as_posix() != name or '..' in path_name.parts
                        or '\\' in name or not stat.S_ISREG(mode) or info.is_dir()):
                    raise ValueError('更新 ZIP 包含非法路径或非普通文件: ' + name)
                relative = name[len('camctl-update/'):]
                if not relative or relative in self.members:
                    raise ValueError('更新 ZIP 包含重复成员: ' + name)
                self.members[relative] = info
            for name in self.members:
                for parent in PurePosixPath(name).parents:
                    if parent.as_posix() in self.members:
                        raise ValueError('更新 ZIP 文件与目录路径冲突: ' + name)
        except BaseException:
            self.archive.close()
            raise

    def close(self):
        self.archive.close()

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()

    def read(self, name):
        return self.archive.read(self.members[name])

    def verify(self):
        expected = {}
        for line in self.read('SHA256SUMS').decode('utf-8').splitlines():
            digest, name = line.split('  ', 1)
            path = PurePosixPath(name)
            if (not re.fullmatch('[0-9a-f]{64}', digest) or path.is_absolute() or '..' in path.parts
                    or path.as_posix() != name or '\\' in name or name in expected or name == 'SHA256SUMS'):
                raise ValueError('更新 ZIP 校验清单无效: ' + name)
            expected[name] = digest
        if set(expected) != set(self.members) - {'SHA256SUMS'}:
            raise ValueError('更新 ZIP 文件集合与校验清单不一致')
        required = {'SHA256SUMS', 'RELEASE.json', 'source-manifest.json', 'camctl/requirements.txt'}
        if not required.issubset(self.members):
            raise ValueError('更新 ZIP 缺少必要元数据')
        wheel = self.wheel_name()
        allowed = required | {wheel}
        for arch in WHEEL_TARGETS:
            prefix = 'camctl/wheelhouse/' + arch + '/'
            wheels = {name for name in self.members if name.startswith(prefix)
                      and '/' not in name[len(prefix):] and name.endswith('.whl')}
            if not wheels:
                raise ValueError('更新 ZIP 缺少目标平台依赖: ' + arch)
            allowed.update(wheels)
        if set(self.members) != allowed:
            raise ValueError('更新 ZIP 包含不属于组件更新的文件')
        for name, digest in expected.items():
            actual = hashlib.sha256()
            with self.archive.open(self.members[name]) as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    actual.update(block)
            if actual.hexdigest() != digest:
                raise ValueError('更新 ZIP 成员摘要不一致: ' + name)

    def wheel_targets(self, targets):
        result = {}
        for arch, platforms in targets.items():
            prefix = 'camctl/wheelhouse/' + arch + '/'
            files = sorted(name[len(prefix):] for name in self.members
                           if name.startswith(prefix) and '/' not in name[len(prefix):] and name.endswith('.whl'))
            result[arch] = {'platforms': list(platforms), 'files': files}
        return result

    def wheel_name(self):
        wheels = [name for name in self.members if name.startswith('camctl/camctl-')
                  and name.endswith('.whl') and '/' not in name[len('camctl/'):]]
        if len(wheels) != 1:
            raise ValueError('更新 ZIP 必须包含唯一 camctl wheel')
        return wheels[0]

    def extract(self, destination):
        destination.mkdir()
        for name, info in self.members.items():
            path = destination.joinpath(*PurePosixPath(name).parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            with self.archive.open(info) as source, path.open('xb') as output:
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    output.write(block)
