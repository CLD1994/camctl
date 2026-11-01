"""交付文件完整性与目标平台的共同契约。"""
import hashlib
from pathlib import Path
import platform
import re
import sys

# 目录名、目标架构与 pip 的兼容标签共用这一份登记。
WHEEL_TARGETS = {
    'x86_64': ('manylinux2014_x86_64', 'manylinux_2_17_x86_64'),
    'aarch64': ('manylinux2014_aarch64', 'manylinux_2_17_aarch64'),
}


def update_metadata(release):
    if not isinstance(release, dict):
        raise ValueError('发行记录必须是对象')
    update = release.get('update', {})
    if not isinstance(update, dict):
        raise ValueError('更新元数据必须是对象')
    force = update.get('force', False)
    if type(force) is not bool:
        raise ValueError('更新模式必须是布尔值')
    if force:
        if not isinstance(update.get('config_toml'), str):
            raise ValueError('强制更新必须提供完整配置')
        return {'force': True, 'config_toml': update['config_toml']}
    if 'config_toml' in update:
        raise ValueError('普通更新不能包含替换配置')
    return {'force': False}


def wheel_target_manifest(bundle):
    return {name: {'platforms': list(platforms),
                   'files': sorted(p.name for p in (bundle / 'camctl/wheelhouse' / name).glob('*.whl'))}
            for name, platforms in WHEEL_TARGETS.items()}


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def verify_checksums(bundle):
    expected = {}
    for line in (bundle / 'SHA256SUMS').read_text(encoding='utf-8').splitlines():
        digest, name = line.split('  ', 1)
        path = Path(name)
        if (not re.fullmatch(r'[0-9a-f]{64}', digest) or path.is_absolute()
                or '..' in path.parts or name in expected or name == 'SHA256SUMS'):
            raise ValueError('校验清单无效：' + name)
        expected[name] = digest
    actual = {p.relative_to(bundle).as_posix(): file_sha256(p)
              for p in bundle.rglob('*') if p.is_file() and p != bundle / 'SHA256SUMS'}
    if expected != actual:
        raise ValueError('交付文件集合或摘要与 SHA256SUMS 不一致')


def current_target():
    machine = platform.machine()
    libc, version = platform.libc_ver()
    if sys.platform != 'linux' or machine not in WHEEL_TARGETS:
        raise ValueError('不支持的平台：' + sys.platform + '/' + machine)
    try:
        supported = libc == 'glibc' and tuple(map(int, version.split('.'))) >= (2, 17)
    except ValueError:
        supported = False
    if not supported:
        raise ValueError('需要 glibc 2.17 或以上，实际：' + libc + ' ' + version)
    return machine
