import json
import os
import pathlib
import select
import errno
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

DRIVER, CLI = sys.argv[1:3]
sys.argv = sys.argv[:1]

class HostIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.ready = self.root / 'ready'
        self.processing = self.root / 'processing'
        self.ready.mkdir(); self.processing.mkdir()
        self.proc = None
        self.cli = self.root/'controlled-camctl'
        self.cli.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+' '+shlex.quote(CLI)+' "$@"\n')
        self.cli.chmod(0o755)

    def tearDown(self):
        if self.proc:
            for child in self.events():
                try:
                    if os.getpgid(child['pid']) == child['pgid']:
                        os.killpg(child['pgid'], signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # 失败断言可能已经结束整个测试进程组。
            self.proc.communicate(timeout=5)
        self.tmp.cleanup()

    def start(self, initial='-', program=CLI, log=None, retries=3, slow_log=False, settings=None):
        environment=os.environ.copy()
        environment.update(settings or {})
        if slow_log: environment['HOST_TEST_SLOW_LOG']='1'
        if program == CLI: program = self.cli
        self.proc = subprocess.Popen([DRIVER, str(program), str(self.ready), str(self.processing),
            str(log or self.root / 'module.log'), str(self.root), str(initial), str(retries)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, start_new_session=True,env=environment)
        self.assertEqual(self.reply(), 'initialized')

    def reply(self):
        self.assertTrue(select.select([self.proc.stdout], [], [], 5)[0], '主程序调用未及时返回')
        line = self.proc.stdout.readline().strip()
        if not line and self.proc.poll() is not None:
            self.fail(self.proc.stderr.read())
        return line

    def command(self, text):
        self.proc.stdin.write(text+'\n'); self.proc.stdin.flush()
        return self.reply()

    def events(self):
        p = self.root/'trace'
        if not p.exists(): return []
        return [json.loads(line) for line in p.read_text().splitlines() if line.endswith('}')]

    def logs(self):
        text = ''
        for path in self.root.glob('module.log*'):
            try:
                text += path.read_text()
            except FileNotFoundError:
                pass  # 日志线程可能刚刚轮换此归档；下一次观察继续读取。
        return text

    def diagnostics(self):
        return sorted(self.processing.glob('camctl-host-diagnostic-*.log'))

    def diagnostic_header(self, path):
        data = path.read_bytes()
        header, marker, _ = data.partition(b'\nstdout_begin\n')
        self.assertTrue(marker, '诊断文件缺少 stdout 字节区段标记')
        return dict(line.split('=', 1) for line in header.decode('ascii').splitlines()
                    if '=' in line)

    def until(self, condition):
        deadline = time.monotonic()+5
        while time.monotonic() < deadline:
            if condition(): return
            time.sleep(.005)
        self.fail('未观察到预期事件: '+str(self.events()))

    def plan(self, name, mode='false'):
        p=self.root/name; p.write_text(mode); return p

    def test_submit_waits_for_input_processing_but_not_run_completion(self):
        (self.root/'hold-run').touch(); (self.root/'hold-submit').touch()
        plan = self.plan('计划.json')
        self.start()
        self.assertEqual(self.command('start-submit '+str(plan)), 'started 0')
        self.until(lambda: any(e['command'] == 'submit' for e in self.events()))
        self.assertEqual(self.command('submit-result 0'), 'pending')
        self.assertEqual(self.command('coexist'), 'coexists')
        (self.root/'hold-submit').unlink()
        self.until(lambda: self.command('submit-result 0') == 'submit 0 errno=0')
        plan.unlink()
        self.assertTrue((self.root/'hold-run').exists())

    def test_submit_waits_for_process_exit_after_success_message(self):
        (self.root/'hold-submit-exit').touch()
        self.start()
        self.assertEqual(self.command('start-submit '+str(self.plan('A'))), 'started 0')
        self.until(lambda: (self.root/'submit-result-written').exists())
        self.assertEqual(self.command('submit-result 0'), 'pending')
        (self.root/'hold-submit-exit').unlink()
        self.until(lambda: self.command('submit-result 0') == 'submit 0 errno=0')

    def test_submit_returns_failure_for_session_error(self):
        self.start()
        self.assertEqual(self.command('submit '+str(self.plan('A', 'error'))), 'submit -1')

    def test_successful_run_does_not_publish_diagnostic(self):
        self.start(retries=0)
        self.until(lambda: 'result=success' in self.logs())
        self.assertEqual(self.diagnostics(), [])

    def test_run_session_error_publishes_actual_stdout_stderr_and_exit(self):
        (self.root/'run-mode').write_text('diagnostic')
        self.start(retries=0)
        self.until(lambda: 'result=error' in self.logs())
        self.until(lambda: len(self.diagnostics()) == 1)
        artifact = self.diagnostics()[0].read_bytes()
        self.assertIn(b'CAMCTL-HOST-DIAGNOSTIC 1\n', artifact)
        self.assertIn(b'command=run\n', artifact)
        self.assertIn(b'classification=error\n', artifact)
        self.assertIn(b'termination=normal\n', artifact)
        self.assertIn(b'exit_code=1\n', artifact)
        self.assertIn(f'working_directory_hex={os.getcwd().encode().hex()}\n'.encode(), artifact)
        self.assertIn(b'"reason": "state_db_error"', artifact)
        self.assertIn(b'controlled stderr diagnostic\x00tail\n', artifact)
        self.until(lambda: 'diagnostic_published' in self.logs())
        self.assertEqual(list(self.processing.glob('.camctl-host-diagnostic-prepare-*')), [],
                         self.logs())

    def test_signal_exit_publishes_signal_number(self):
        (self.root/'run-mode').write_text('signal')
        self.start(retries=0)
        self.until(lambda: 'result=abnormal' in self.logs())
        self.until(lambda: len(self.diagnostics()) == 1)
        header = self.diagnostic_header(self.diagnostics()[0])
        self.assertEqual(header['classification'], 'abnormal')
        self.assertEqual(header['termination'], 'signal')
        self.assertEqual(header['signal_number'], str(signal.SIGTERM))

    def test_protocol_error_publishes_diagnostic(self):
        self.start()
        self.assertEqual(self.command('submit '+str(self.plan('empty', 'empty'))), 'submit -1')
        self.until(lambda: len(self.diagnostics()) == 1)
        header = self.diagnostic_header(self.diagnostics()[0])
        self.assertEqual(header['command'], 'submit')
        self.assertEqual(header['classification'], 'abnormal')
        self.assertEqual(header['termination'], 'normal')
        self.assertEqual(header['exit_code'], '0')
        self.assertEqual(header['stdout_bytes_saved'], '0')

    def test_raw_stream_sections_preserve_lengths_nul_and_non_utf8(self):
        self.start()
        self.assertEqual(self.command('submit '+str(self.plan('raw', 'diagnostic-raw'))),
                         'submit -1')
        self.until(lambda: len(self.diagnostics()) == 1)
        artifact = self.diagnostics()[0].read_bytes()
        header, marker, rest = artifact.partition(b'\nstdout_begin\n')
        self.assertTrue(marker)
        fields = dict(line.split('=', 1) for line in header.decode('ascii').splitlines()
                      if '=' in line)
        stdout_length = int(fields['stdout_bytes_saved'])
        stderr_length = int(fields['stderr_bytes_saved'])
        stdout = rest[:stdout_length]
        tail = rest[stdout_length:]
        stderr_marker = b'\nstderr_begin\n'
        self.assertTrue(tail.startswith(stderr_marker))
        stderr = tail[len(stderr_marker):len(stderr_marker) + stderr_length]
        self.assertEqual(stdout, b'raw\x00stdout\xffbytes')
        self.assertEqual(stderr, b'raw\xff\x00stderr')
        self.assertEqual(len(rest), stdout_length + len(stderr_marker) + stderr_length)

    def test_pending_diagnostic_keeps_submit_context_until_writer_finishes(self):
        self.start(retries=0, settings={'HOST_TEST_HOLD_DIAGNOSTIC_PUBLISH': '1'})
        self.until(lambda: 'result=success' in self.logs())
        first = self.plan('first', 'diagnostic')
        self.assertEqual(self.command('start-submit '+str(first)), 'started 0')
        self.until(lambda: len([event for event in self.events()
                                if event['command'] == 'submit']) == 1)
        self.until(lambda: self.command('diagnostic-state') == 'diagnostic-entered 1')
        self.assertEqual(self.command('submit-result 0'), 'pending')
        second = self.plan('second', 'true')
        self.assertEqual(self.command('start-submit '+str(second)), 'started 1')
        self.until(lambda: f'path={second}' in self.logs())
        self.assertEqual(len([event for event in self.events()
                              if event['command'] == 'submit']), 1)
        self.assertEqual(self.diagnostics(), [])
        self.assertEqual(self.command('unblock-diagnostic'), 'diagnostic-resumed')
        self.until(lambda: len(self.diagnostics()) == 1)
        self.until(lambda: len([event for event in self.events()
                                if event['command'] == 'submit']) == 2)
        header = self.diagnostic_header(self.diagnostics()[0])
        self.assertEqual(header['plan_path_hex'], str(first).encode().hex())
        self.assertNotEqual(header['plan_path_hex'], str(second).encode().hex())
        self.until(lambda: self.command('submit-result 0').startswith('submit -1'))
        self.until(lambda: self.command('submit-result 1') == 'submit 0 errno=0')

    def test_truncated_stdout_and_stderr_are_counted_and_drained(self):
        (self.root/'run-mode').write_text('diagnostic-overflow')
        self.start(retries=0)
        self.until(lambda: 'result=abnormal' in self.logs())
        self.until(lambda: len(self.diagnostics()) == 1)
        header = self.diagnostic_header(self.diagnostics()[0])
        self.assertEqual(header['stdout_bytes_seen'], '4096')
        self.assertEqual(header['stdout_bytes_saved'], '1024')
        self.assertEqual(header['stdout_truncated'], '1')
        self.assertEqual(header['stderr_bytes_seen'], '4096')
        self.assertEqual(header['stderr_bytes_saved'], '1024')
        self.assertEqual(header['stderr_truncated'], '1')

    def test_concurrent_run_and_submit_failures_have_unique_attempts(self):
        (self.root/'hold-run').touch()
        (self.root/'hold-submit').touch()
        (self.root/'run-mode').write_text('diagnostic')
        plan = self.plan('A', 'diagnostic')
        self.start(retries=0)
        self.until(lambda: len(self.events()) == 1)
        self.assertEqual(self.command('start-submit '+str(plan)), 'started 0')
        self.until(lambda: len(self.events()) == 2)
        (self.root/'hold-run').unlink()
        (self.root/'hold-submit').unlink()
        self.until(lambda: len(self.diagnostics()) == 2)
        self.until(lambda: self.command('submit-result 0').startswith('submit -1'))
        headers = [self.diagnostic_header(path) for path in self.diagnostics()]
        self.assertEqual({header['command'] for header in headers}, {'run', 'submit'})
        self.assertEqual(len({header['call_id'] for header in headers}), 2)
        self.assertEqual(len({header['attempt_id'] for header in headers}), 2)
        self.assertEqual(len({path.name for path in self.diagnostics()}), 2)
        self.until(lambda: self.logs().count('diagnostic_published') >= 2)
        self.assertEqual(list(self.processing.glob('.camctl-host-diagnostic-prepare-*')), [])

    def test_submit_returns_failure_for_unknown_result(self):
        self.start()
        self.assertEqual(self.command('submit '+str(self.plan('A', 'empty'))), 'submit -1')

    def test_submit_returns_last_start_error_after_retry_exhaustion(self):
        self.start(retries=1)
        self.until(lambda: 'result=success' in self.logs())
        camctl_script = self.cli.read_bytes()
        self.cli.unlink()
        self.assertEqual(self.command('submit '+str(self.plan('A'))), 'submit -1')
        self.until(lambda: len(self.diagnostics()) == 2)
        headers = [self.diagnostic_header(path) for path in self.diagnostics()]
        submit_attempts = [header for header in headers if header['command'] == 'submit']
        self.assertEqual(len(submit_attempts), 2)
        self.assertEqual(len({header['call_id'] for header in submit_attempts}), 1)
        self.assertEqual(len({header['attempt_id'] for header in submit_attempts}), 2)
        for header in submit_attempts:
            self.assertEqual(header['classification'], 'not_started')
            self.assertEqual(header['termination'], 'not_started')
            self.assertEqual(header['spawn_errno'], str(errno.ENOENT))
            self.assertEqual(header['stdout_bytes_saved'], '0')
            self.assertEqual(header['stderr_bytes_saved'], '0')
        self.until(lambda: 'retry_exhausted' in self.logs())
        self.cli.write_bytes(camctl_script)
        self.cli.chmod(0o755)
        plan = self.plan('B', 'true')
        self.assertEqual(self.command('submit '+str(plan)), 'submit 0')
        self.until(lambda: len(self.events()) == 2)
        self.assertEqual(self.events()[1]['plan'], str(plan))

    def test_diagnostic_writer_does_not_block_other_cli_drain(self):
        (self.root/'run-mode').write_text('diagnostic')
        self.start(retries=0, settings={'HOST_TEST_HOLD_DIAGNOSTIC_PUBLISH': '1'})
        self.until(lambda: self.command('diagnostic-state') == 'diagnostic-entered 1')
        self.assertEqual(self.diagnostics(), [])
        self.assertEqual(len(list(self.processing.glob('.camctl-host-diagnostic-prepare-*'))), 1)
        noisy = self.plan('noisy', 'noisy')
        self.assertEqual(self.command('submit '+str(noisy)), 'submit 0')
        self.until(lambda: len(self.events()) == 2)
        self.assertEqual(self.diagnostics(), [])
        self.assertEqual(self.command('unblock-diagnostic'), 'diagnostic-resumed')
        self.until(lambda: len(self.diagnostics()) == 1)
        self.until(lambda: 'diagnostic_published' in self.logs())
        self.assertEqual(list(self.processing.glob('.camctl-host-diagnostic-prepare-*')), [])

    def test_publish_failure_keeps_submit_result_and_cleans_preparation(self):
        self.start(retries=0, settings={'HOST_TEST_FAIL_DIAGNOSTIC_PUBLISH': '1'})
        plan = self.plan('failure', 'diagnostic')
        self.assertEqual(self.command('submit '+str(plan)), 'submit -1')
        self.until(lambda: 'diagnostic_write_failed step=publish_rename' in self.logs())
        self.assertEqual(self.diagnostics(), [])
        self.assertEqual(list(self.processing.glob('.camctl-host-diagnostic-prepare-*')), [])

    def test_no_replace_publish_preserves_existing_processing_file(self):
        self.start(retries=0, settings={'HOST_TEST_COLLIDE_DIAGNOSTIC_PUBLISH': '1'})
        plan = self.plan('collision', 'diagnostic')
        self.assertEqual(self.command('submit '+str(plan)), 'submit -1')
        self.until(lambda: 'diagnostic_write_failed step=publish_rename' in self.logs())
        files = self.diagnostics()
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_bytes(), b'preserve')
        self.assertIn('errno='+str(errno.EEXIST), self.logs())
        self.assertIn('published=0', self.logs())
        self.assertEqual(list(self.processing.glob('.camctl-host-diagnostic-prepare-*')), [])

    def test_clock_failure_releases_queued_submit_waiters(self):
        (self.root/'hold-submit').touch()
        self.start()
        self.assertEqual(self.command('start-submit '+str(self.plan('A'))), 'started 0')
        self.until(lambda: any(e['command'] == 'submit' for e in self.events()))
        self.assertEqual(self.command('start-submit '+str(self.plan('B'))), 'started 1')
        self.until(lambda: 'call=3 submit accepted' in self.logs())
        self.assertEqual(self.command('break-clock'), 'clock-failed')
        for index in [0, 1]:
            self.until(lambda: self.command(f'submit-result {index}') == f'submit -1 errno={errno.EIO}')

    def test_poll_failure_releases_waiter_and_keeps_claim(self):
        (self.root/'hold-submit').touch()
        self.start()
        self.assertEqual(self.command('start-submit '+str(self.plan('A'))), 'started 0')
        self.until(lambda: any(e['command'] == 'submit' for e in self.events()))
        self.assertEqual(self.command('break-poll'), 'poll-failed')
        self.until(lambda: self.command('submit-result 0') == f'submit -1 errno={errno.EIO}')
        self.assertEqual(self.command('submit '+str(self.plan('B'))), 'submit -1')
        (self.ready/'file').write_text('data')
        self.assertEqual(self.command('claim'), 'claimed')
        self.assertTrue((self.processing/'file').exists())

    def test_submit_defers_cancellation_until_queue_reference_is_released(self):
        (self.root/'hold-submit').touch()
        self.start()
        self.assertEqual(self.command('start-submit '+str(self.plan('A'))), 'started 0')
        self.until(lambda: any(e['command'] == 'submit' for e in self.events()))
        self.assertEqual(self.command('cancel-submit 0'), 'cancel-requested')
        self.assertEqual(self.command('submit-result 0'), 'pending')
        (self.root/'hold-submit').unlink()
        self.assertEqual(self.command('join-submit 0'), 'canceled')
        self.assertEqual(self.command('submit '+str(self.plan('B'))), 'submit 0')

    def test_concurrent_fifo_capacity_and_host_coexistence(self):
        (self.root/'hold-run').touch(); (self.root/'hold-submit').touch()
        a=self.plan('中文 A.json'); b=self.plan('B.json'); c=self.plan('C.json')
        self.start()
        self.assertEqual(self.command('start-submit '+str(a)), 'started 0')
        self.until(lambda: len(self.events())==2)
        self.assertEqual(self.command('start-submit '+str(b)), 'started 1')
        self.until(lambda: 'call=3 submit accepted' in self.logs())
        self.assertEqual(self.command('submit '+str(c)), 'submit -1')
        self.assertEqual(self.command('submit-result 0'), 'pending')
        self.assertEqual(self.command('submit-result 1'), 'pending')
        self.assertEqual(self.command('coexist'), 'coexists')
        (self.root/'hold-submit').unlink()
        self.until(lambda: len(self.events())==3)
        for index in [0, 1]:
            self.until(lambda: self.command(f'submit-result {index}') == 'submit 0 errno=0')
        self.assertEqual([e['plan'] for e in self.events() if e['command']=='submit'], [str(a),str(b)])
        self.assertEqual(self.command('reinit'), 'rejected')

    def test_pending_waits_for_old_run(self):
        (self.root/'hold-run').touch(); self.start()
        a=self.plan('A','true'); b=self.plan('B','false')
        self.assertEqual(self.command('submit '+str(a)), 'submit 0')
        self.until(lambda: len(self.events())==2)
        self.until(lambda: 'result=success' in self.logs())
        self.assertEqual(self.command('submit '+str(b)), 'submit 0')
        self.until(lambda: len(self.events())==3)
        self.assertEqual(sum(e['command']=='run' for e in self.events()),1)
        (self.root/'hold-run').unlink()
        self.until(lambda: sum(e['command']=='run' for e in self.events())==2)

    def test_abnormal_run_bounded_and_drops_initial(self):
        (self.root/'run-mode').write_text('127')
        a=self.plan('A'); self.start(a)
        self.until(lambda: len(self.events())==4)
        self.until(lambda: 'retry_exhausted' in self.logs())
        self.assertEqual([e['plan'] for e in self.events()], [str(a),None,None,None])

    def test_retry_delay_starts_after_final_result(self):
        (self.root/'run-mode').write_text('empty')
        self.start(retries=1, settings={'HOST_TEST_RETRY_DELAY_MS':'200',
                                      'HOST_TEST_FINAL_SCAN_DELAY_MS':'300'})
        self.until(lambda: float(self.command('retry-gap').split()[1]) > 0)
        gap = float(self.command('retry-gap').split()[1])
        # 单调钟按整毫秒存储；这里只容许不足一毫秒的量化误差。
        self.assertGreaterEqual(gap, 199, f'结果判定后只等待了 {gap}ms')

    def test_clock_failure_at_completion_keeps_result_and_claim(self):
        (self.root/'run-mode').write_text('empty')
        self.start(retries=1, settings={'HOST_TEST_FAIL_COMPLETION_CLOCK':'1'})
        self.until(lambda: 'clock_unknown' in self.logs())
        self.assertIn('result=abnormal', self.logs())
        self.assertEqual(self.command('submit '+str(self.plan('A'))), 'submit -1')
        self.assertEqual(self.command('retry-gap'), 'retry-gap 0.000')
        (self.ready/'file').write_text('data')
        self.assertEqual(self.command('claim'), 'claimed')
        self.assertTrue((self.processing/'file').exists())

    def test_legal_error_no_restart_and_submit_unknown_no_resend(self):
        (self.root/'run-mode').write_text('error'); self.start()
        self.until(lambda: 'result=error' in self.logs())
        a=self.plan('A','empty'); b=self.plan('B','false')
        self.assertEqual(self.command('submit '+str(a)), 'submit -1')
        self.assertEqual(self.command('submit '+str(b)), 'submit 0')
        self.until(lambda: len(self.events())==3)
        self.assertEqual([e['command'] for e in self.events()], ['run','submit','submit'])

    def test_start_failure_retains_initial_after_exhaustion(self):
        executable=self.root/'camctl'; a=self.plan('A'); self.start(a, executable)
        self.until(lambda: 'retry_exhausted' in self.logs())
        executable.symlink_to(self.cli)
        b=self.plan('B','true'); self.assertEqual(self.command('submit '+str(b)), 'submit 0')
        self.until(lambda: len(self.events())==2)
        self.assertEqual(self.events()[1]['plan'],str(a))

    def test_claim_replaces_inode_and_continues_file_error(self):
        self.start()
        (self.ready/'report').write_bytes(b'new\x00data'); (self.processing/'report').write_bytes(b'old')
        inode=(self.ready/'report').stat().st_ino
        (self.ready/'blocked').write_text('x'); (self.processing/'blocked').mkdir()
        (self.ready/'other-report').write_text('other')
        self.assertEqual(self.command('claim'),'claimed')
        self.assertEqual((self.processing/'report').read_bytes(),b'new\x00data')
        self.assertEqual((self.processing/'report').stat().st_ino,inode)
        self.assertFalse((self.ready/'report').exists())
        self.assertTrue((self.ready/'blocked').exists()); self.assertTrue((self.processing/'other-report').exists())

    def test_logging_failure_does_not_block_output_or_claim(self):
        self.start(log=self.root/'absent'/'log')
        p=self.plan('noisy','noisy'); q=self.plan('next','false')
        self.assertEqual(self.command('submit '+str(p)),'submit 0')
        self.assertEqual(self.command('submit '+str(q)),'submit 0')
        self.until(lambda: len(self.events())==3)
        (self.ready/'file').write_text('data')
        self.assertEqual(self.command('claim'),'claimed')
        self.assertTrue((self.processing/'file').exists())

    def test_log_rotation_is_bounded(self):
        self.start(); p=self.plan('noisy','noisy')
        self.assertEqual(self.command('submit '+str(p)),'submit 0')
        self.until(lambda: (self.root/'module.log.1').exists())
        # 冻结所有主程序线程后检查同一时刻的目录，避免与合法轮换竞争。
        os.kill(self.proc.pid,signal.SIGSTOP)
        stopped,status=os.waitpid(self.proc.pid,os.WUNTRACED)
        self.assertEqual(stopped,self.proc.pid); self.assertTrue(os.WIFSTOPPED(status))
        self.assertLessEqual(len(list(self.root.glob('module.log*'))),3)
        for p in self.root.glob('module.log*'): self.assertLessEqual(p.stat().st_size,2048)
        os.kill(self.proc.pid,signal.SIGCONT)

    def test_slow_log_does_not_block_management(self):
        self.start(slow_log=True)
        self.until(lambda: self.command('log-state')=='log-entered 1')
        p=self.plan('noisy','noisy'); q=self.plan('next','false')
        self.assertEqual(self.command('submit '+str(p)),'submit 0')
        self.assertEqual(self.command('submit '+str(q)),'submit 0')
        self.until(lambda: len(self.events())==3)
        (self.ready/'file').write_text('data')
        self.assertEqual(self.command('claim'),'claimed')
        self.assertTrue((self.processing/'file').exists())
        self.assertEqual(self.command('unblock-log'),'log-resumed')
        self.until(lambda: 'dropped=' in self.logs())

    def test_clock_failure_rejects_new_inputs_and_keeps_claim(self):
        self.start()
        self.assertEqual(self.command('break-clock'),'clock-failed')
        self.until(lambda: 'clock_unknown' in self.logs())
        p=self.plan('A')
        self.assertEqual(self.command('submit '+str(p)),'submit -1')
        (self.ready/'file').write_text('data')
        self.assertEqual(self.command('claim'),'claimed')
        self.assertTrue((self.processing/'file').exists())

    def test_slow_archive_cleanup_does_not_block_management(self):
        (self.root/'module.log.1').write_text('old archive')
        self.start(settings={'HOST_TEST_LOG_FILE_COUNT':'1', 'HOST_TEST_SLOW_ARCHIVE_CLEANUP':'1'})
        self.until(lambda: self.command('cleanup-state') == 'cleanup-entered 1')
        for name, mode in [('noisy','noisy'), ('next','false')]:
            self.assertEqual(self.command('submit '+str(self.plan(name, mode))), 'submit 0')
        self.until(lambda: len(self.events()) == 3)
        (self.ready/'file').write_text('data')
        self.assertEqual(self.command('claim'), 'claimed')
        self.assertTrue((self.processing/'file').exists())
        self.assertEqual(self.command('unblock-log'), 'log-resumed')
        self.until(lambda: (self.root/'module.log').exists() and
                           not (self.root/'module.log.1').exists())

    def test_archive_cleanup_failure_does_not_block_management(self):
        (self.root/'module.log.1').write_text('old archive')
        self.start(settings={'HOST_TEST_LOG_FILE_COUNT':'1', 'HOST_TEST_DENY_ARCHIVE_CLEANUP':'1'})
        self.until(lambda: self.command('cleanup-state') == 'cleanup-entered 1')
        for name in ['A','B']:
            self.assertEqual(self.command('submit '+str(self.plan(name))), 'submit 0')
        self.until(lambda: len(self.events()) == 3)
        (self.ready/'file').write_text('data')
        self.assertEqual(self.command('claim'), 'claimed')
        self.assertTrue((self.processing/'file').exists())
        self.assertEqual((self.root/'module.log.1').read_text(), 'old archive')

    def test_changed_sigchld_prevents_next_launch(self):
        self.start(retries=0)
        self.until(lambda: 'command=run' in self.logs() and 'result=success' in self.logs())
        self.assertEqual(self.command('ignore-children'), 'children-ignored')
        plan=self.plan('A')
        self.assertEqual(self.command('submit '+str(plan)), 'submit -1')
        self.until(lambda: 'command=submit not_started errno=22' in self.logs())
        self.assertEqual([e['command'] for e in self.events()], ['run'])

if __name__=='__main__': unittest.main()
