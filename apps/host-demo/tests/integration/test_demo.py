import json
import os
import pathlib
import select
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest

DEMO, CLI = sys.argv[1:3]
sys.argv=sys.argv[:1]
class DemoIntegration(unittest.TestCase):
    def test_terminal_uses_public_interface(self):
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory); ready=root/'ready'; processing=root/'processing'
            ready.mkdir(); processing.mkdir()
            plan=root/'计划 有空格.json'; plan.write_text('false')
            launcher=root/'controlled-camctl'
            launcher.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+' '+shlex.quote(CLI)+' "$@"\n')
            launcher.chmod(0o755)
            proc=subprocess.Popen([DEMO,'--camctl',str(launcher),'--ready',str(ready),'--processing',str(processing),
                '--log',str(root/'module.log'),'--config',str(root)], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,universal_newlines=True,start_new_session=True)
            def response():
                self.assertTrue(select.select([proc.stdout],[],[],5)[0])
                return proc.stdout.readline().strip()
            def command(line):
                proc.stdin.write(line+'\n'); proc.stdin.flush(); return response()
            try:
                self.assertIn('初始化',response())
                self.assertIn('已接收',command('submit '+str(plan)))
                (ready/'report').write_bytes(b'original bytes')
                self.assertIn('领取调用已返回',command('claim'))
                self.assertEqual((processing/'report').read_bytes(),b'original bytes')
                self.assertIn('未知命令',command('invalid'))
                self.assertIn('过长',command('x'*9000))
                self.assertIn('领取调用已返回',command('claim'))
                deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    trace=root/'trace'
                    if trace.exists() and any(json.loads(line)['plan']==str(plan) for line in trace.read_text().splitlines()): break
                    time.sleep(.005)
                else: self.fail('终端路径未传给 CLI')
                proc.stdin.close(); proc.stdin=None
                self.assertIn('继续运行',response())
                self.assertIsNone(proc.poll())
            finally:
                trace=root/'trace'
                if trace.exists():
                    for line in trace.read_text().splitlines():
                        child=json.loads(line)
                        try:
                            if os.getpgid(child['pid']) == child['pgid']:
                                os.killpg(child['pgid'],signal.SIGKILL)
                        except ProcessLookupError: pass
                try: os.killpg(proc.pid,signal.SIGKILL)
                except ProcessLookupError: pass
                proc.communicate(timeout=5)
    def test_demo_records_motor_notification_position(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            ready, processing = root / 'ready', root / 'processing'
            ready.mkdir()
            processing.mkdir()
            (root / 'run-mode').write_text('motor_control')
            launcher = root / 'controlled-camctl'
            launcher.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' ' +
                                shlex.quote(CLI) + ' "$@"\n')
            launcher.chmod(0o755)
            output = root / 'stdout'
            with output.open('w') as stream:
                proc = subprocess.Popen([DEMO, '--camctl', str(launcher), '--ready', str(ready),
                                         '--processing', str(processing), '--log', str(root / 'log'),
                                         '--config', str(root)], stdin=subprocess.PIPE,
                                        stdout=stream, stderr=subprocess.PIPE, start_new_session=True)
                try:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        if 'position=-12' in output.read_text():
                            break
                        time.sleep(.005)
                    else:
                        self.fail('演示回调未呈现通知的位置参数')
                finally:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.communicate(timeout=5)

    def test_invalid_start_arguments(self):
        result=subprocess.run([DEMO, '--unknown'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,universal_newlines=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('--camctl',result.stderr)

    def test_home_paths_work_without_explicit_path_options(self):
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory) / '用户 home'
            runtime = home / '.camctl'
            ready, processing = runtime / 'ready', runtime / 'processing'
            ready.mkdir(parents=True)
            processing.mkdir()
            launcher = runtime / 'venv/bin/camctl'
            launcher.parent.mkdir(parents=True)
            launcher.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' ' +
                                shlex.quote(CLI) + ' "$@" --config ' +
                                shlex.quote(str(runtime)) + '\n')
            launcher.chmod(0o755)
            with (runtime / 'output').open('w') as output:
                proc = subprocess.Popen([DEMO], env={**os.environ, 'HOME': str(home)},
                                        stdin=subprocess.PIPE, stdout=output, stderr=subprocess.PIPE,
                                        start_new_session=True)
                try:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        if (runtime / 'trace').exists():
                            break
                        if proc.poll() is not None:
                            self.fail(proc.stderr.read().decode())
                        time.sleep(.005)
                    else:
                        self.fail('默认 CLI 路径没有被启动')
                    (ready / 'report').write_bytes(b'home report')
                    proc.stdin.write(b'claim\n')
                    proc.stdin.flush()
                    deadline = time.monotonic() + 5
                    while not (processing / 'report').exists() and time.monotonic() < deadline:
                        time.sleep(.005)
                    self.assertEqual((processing / 'report').read_bytes(), b'home report')
                    self.assertTrue((runtime / 'host.log').exists())
                finally:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.communicate(timeout=5)

    def test_relative_home_is_rejected_before_start(self):
        result = subprocess.run([DEMO], env={**os.environ, 'HOME': 'relative'},
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stderr)
if __name__=='__main__': unittest.main()
