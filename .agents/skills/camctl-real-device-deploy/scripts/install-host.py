#!/usr/bin/env python3
"""在新的 Linux 目录安装测试实例；保留全部已有部署。"""
import argparse
from decimal import Decimal
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import tomllib

from deploy_support import configure_paths, install_host, write_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', type=Path, required=True, help='尚不存在的绝对部署目录')
    parser.add_argument('--config', type=Path, required=True, help='包含实际设备绑定与本地策略的 TOML 配置')
    args = parser.parse_args()
    bundle = Path(__file__).resolve().parent
    target = args.target
    if sys.platform != 'linux' or sys.version_info[:2] != (3, 11):
        parser.error('请在目标 Linux 主机使用 Python 3.11 执行')
    if not target.is_absolute() or target.exists() or target.is_symlink():
        parser.error('--target 必须是尚不存在的绝对路径；已有数据继续保留')
    try:
        configuration = tomllib.loads(args.config.read_text(encoding='utf-8'), parse_float=Decimal)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    for command in ('uv', 'cmake'):
        if shutil.which(command) is None:
            parser.error('缺少部署工具 ' + command)
    wheels = list((bundle / 'camctl').glob('*.whl'))
    archives = list((bundle / 'host').glob('*-Source.tar.gz'))
    if len(wheels) != 1 or len(archives) != 1:
        parser.error('交付文件不完整：应有一个 wheel 和一个 C 源码包')
    try:
        release = json.loads((bundle / 'RELEASE.json').read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        parser.error(str(error))
    target.mkdir(parents=True)
    receipt = {'status': 'installing', 'release': release,
               'config_source': str(args.config.resolve()), 'steps': []}
    environment = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV')}
    def save():
        (target / 'install-receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
    def run(label, argv, *, cwd=None, output=None):
        argv = [str(v) for v in argv]
        print(label, flush=True)
        with (target / 'install.log').open('ab') as log:
            log.write(('\n$ ' + shlex.join(argv) + '\n').encode()); log.flush()
            if output is None:
                result = subprocess.run(argv, cwd=cwd, stdout=log, stderr=log, env=environment)
            else:
                with output.open('wb') as out:
                    result = subprocess.run(argv, cwd=cwd, stdout=out, stderr=log, env=environment)
        receipt['steps'].append({'label': label, 'argv': argv, 'cwd': str(cwd) if cwd else None,
                                 'returncode': result.returncode})
        save()
        if result.returncode:
            raise RuntimeError(label + ' 失败，详见 install.log')
    try:
        save()
        run('创建 Python 环境', ['uv', 'venv', '--python', sys.executable, target / 'venv'])
        run('安装锁定依赖', ['uv', 'pip', 'install', '--python', target / 'venv/bin/python', '--require-hashes', '-r', bundle / 'camctl/requirements.txt'])
        run('安装 camctl', ['uv', 'pip', 'install', '--python', target / 'venv/bin/python', '--no-deps', wheels[0]])
        run('检查依赖', ['uv', 'pip', 'check', '--python', target / 'venv/bin/python'])
        configure_paths(configuration, target)
        config_path = target / 'config.toml'
        write_config(config_path, configuration)
        run('按实际配置导出客户端能力说明', [target / 'venv/bin/camctl', 'describe', '--config', config_path], output=target / 'device-capabilities.json')
        source = target / 'source'
        source.mkdir()
        with tarfile.open(archives[0]) as archive:
            # 交付的 CPack 源码只包含普通目录和文件。
            members = archive.getmembers()
            if any(not (m.isfile() or m.isdir()) or Path(m.name).is_absolute() or '..' in Path(m.name).parts for m in members):
                raise ValueError('C 源码包包含无效成员')
            archive.extractall(source, members=members)
        candidates = [p.parent for p in source.glob('*/CMakeLists.txt')]
        if len(candidates) != 1:
            raise ValueError('C 源码包没有唯一构建入口')
        install_host(run, candidates[0], target)
        run('显式初始化新状态库', [target / 'venv/bin/camctl', 'init', '--config', config_path])
        command = [target / 'host/bin/host-demo', '--camctl', target / 'venv/bin/camctl', '--config', config_path,
                   '--ready', target / 'ready', '--processing', target / 'processing', '--log', target / 'host.log']
        launcher = target / 'start-host-demo.sh'
        launcher.write_text('#!/usr/bin/env bash\nset -euo pipefail\nexec ' + shlex.join([str(v) for v in command]) + '\n')
        launcher.chmod(0o700)
        receipt['status'] = 'installed_stopped'
        save()
        print('安装完成；启动入口：' + str(launcher))
        print('客户端能力说明：' + str(target / 'device-capabilities.json'))
        print('相机与主程序尚未启动。')
    except Exception as error:
        receipt.update(status='failed', error=str(error)); save()
        print(str(error) + '；保留该目录和日志，修正后使用新的目标目录。', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
