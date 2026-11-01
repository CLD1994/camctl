"""更新写入者继承锁的隔离单元。"""
import importlib.util
import os
import shutil
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

SOURCE = Path(__file__).absolute().parents[5] / 'apps/camctl/src/camctl/update_engine/worker.py'
spec = importlib.util.spec_from_file_location('update_worker_under_test', SOURCE)
worker = importlib.util.module_from_spec(spec)
with patch.object(sys, 'dont_write_bytecode', True):
    spec.loader.exec_module(worker)


def test_worker_keeps_lock_descriptors_while_command_runs():
    with patch.object(shutil, 'get_terminal_size', return_value=os.terminal_size((80, 24))), \
            patch.object(worker.argparse, '_', new=lambda text: text), \
            patch.object(sys, 'argv', ['update-worker.py', '--lock-fd', '7', '--lock-fd', '8', '--',
                                    'uv', 'pip', 'install']), \
            patch.object(worker.subprocess, 'run', return_value=SimpleNamespace(returncode=4)) as run:
        assert worker.main() == 4
    run.assert_called_once_with(['uv', 'pip', 'install'], pass_fds=(7, 8), check=False)
