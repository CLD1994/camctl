import json
import os
import pathlib
import select
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
            proc=subprocess.Popen([DEMO,'--camctl',CLI,'--ready',str(ready),'--processing',str(processing),
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
                try: os.killpg(proc.pid,signal.SIGKILL)
                except ProcessLookupError: pass
                proc.communicate(timeout=5)
    def test_invalid_start_arguments(self):
        result=subprocess.run([DEMO],stdout=subprocess.PIPE,stderr=subprocess.PIPE,universal_newlines=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('--camctl',result.stderr)
if __name__=='__main__': unittest.main()
