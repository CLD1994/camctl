import json
import os
import pathlib
import select
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

    def until(self, condition):
        deadline = time.monotonic()+5
        while time.monotonic() < deadline:
            if condition(): return
            time.sleep(.005)
        self.fail('未观察到预期事件: '+str(self.events()))

    def plan(self, name, mode='false'):
        p=self.root/name; p.write_text(mode); return p

    def test_concurrent_fifo_capacity_and_host_coexistence(self):
        (self.root/'hold-run').touch(); (self.root/'hold-submit').touch()
        a=self.plan('中文 A.json'); b=self.plan('B.json'); c=self.plan('C.json')
        self.start()
        self.assertEqual(self.command('submit '+str(a)), 'submit 0')
        self.until(lambda: len(self.events())==2)
        self.assertEqual(self.command('submit '+str(b)), 'submit 0')
        self.assertEqual(self.command('submit '+str(c)), 'submit -1')
        self.assertEqual(self.command('coexist'), 'coexists')
        (self.root/'hold-submit').unlink()
        self.until(lambda: len(self.events())==3)
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
        self.assertEqual(self.command('submit '+str(a)), 'submit 0')
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
        self.assertEqual(self.command('submit '+str(plan)), 'submit 0')
        self.until(lambda: 'command=submit not_started errno=22' in self.logs())
        self.assertEqual([e['command'] for e in self.events()], ['run'])

if __name__=='__main__': unittest.main()
