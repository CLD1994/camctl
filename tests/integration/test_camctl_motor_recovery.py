"""真实 CLI、SQLite 与管道的发送断点、恢复及报告链。"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from camctl_fixtures import Deployment, future_schedule

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='部署侧 POSIX 管道')
ENTRY = Path(__file__).with_name('_motor_fault_entry.py')


def motor_plan(request='701', *, delay=60000, scheduled=None):
    return {'request_id':request, 'created_at':'2026-10-08 00:00:00', 'name':'电机恢复',
            'actions':[{'name':'位置', 'type':'motor_control', 'scheduled_at':scheduled or future_schedule(-1),
                        'params':{'position':-2147483648}, 'policy':{'max_delay_ms':delay}}]}


@pytest.fixture
def deployment(tmp_path):
    value = Deployment(tmp_path, devices=False)
    result = value.camctl('init','--config',str(value.config_path))
    assert result.exit_code == 0, result.stderr
    return value


def run_pipe(deployment, path=None, *, phase='', channel='normal'):
    read_fd, write_fd = os.pipe()
    filler = b''
    if channel == 'full':
        os.set_blocking(write_fd, False)
        while True:
            try:
                os.write(write_fd, b'x'*4096)
                filler += b'x'*4096
            except BlockingIOError:
                break
    if channel == 'broken':
        os.close(read_fd)
        read_fd = None
    environment = {**os.environ, 'CAMCTL_MOTOR_FAULT':phase,
                   'CAMCTL_MOTOR_FAULT_MARKER':str(deployment.root/'fault.marker'),
                   'CAMCTL_MOTOR_WRITE_TRACE':str(deployment.root/'write.trace')}
    command = [sys.executable, str(ENTRY),'run','--config',str(deployment.config_path)]
    if path is not None: command.append(str(path))
    if channel != 'absent': command += ['--host-notification-fd',str(write_fd)]
    try:
        result = subprocess.run(command, pass_fds=(write_fd,) if channel != 'absent' else (),
                                capture_output=True, text=True, env=environment, timeout=60)
    finally:
        os.close(write_fd)
    payload = b''
    if read_fd is not None:
        try:
            while chunk := os.read(read_fd, 4096): payload += chunk
        finally:
            os.close(read_fd)
    assert payload.startswith(filler)
    return result, payload[len(filler):]


def facts(deployment):
    with closing(sqlite3.connect(deployment.state_db)) as connection:
        action = connection.execute('SELECT status,error_code FROM actions WHERE type=8 ORDER BY id').fetchall()
        notices = connection.execute('SELECT outcome,written_bytes FROM motor_notifications ORDER BY id').fetchall()
        return action, notices


def assert_motor_report(deployment, status, code=None):
    reports = [json.loads(path.read_text()) for path in deployment.ready.glob('status-report-*.json')]
    reports.sort(key=lambda report: int(report['report_id']))
    actions = [action for report in reports for plan in report.get('plans', [])
               for action in plan['actions'] if action['type'] == 'motor_control']
    assert actions
    action = actions[-1]
    assert action['status'] == status
    assert action['input_params'] == {'position': -2147483648}
    assert not ({'device_execution', 'effective_params', 'outputs', 'deliveries', 'result'} & action.keys())
    if code is not None:
        assert action['error']['code'] == code
    return action


@pytest.mark.parametrize('phase,writes,terminal', [
    ('before_intent',0,3), ('after_intent',0,4), ('before_write',0,4),
    ('after_write',1,4), ('before_result',1,4), ('after_result',1,3),
    ('intent_commit_unknown',0,4), ('result_commit_unknown',1,3),
    ('observe_commit_unknown',1,3), ('intent_absent_unknown',0,3), ('result_absent_unknown',1,4),
])
def test_restart_never_repeats_saved_intent(deployment, phase, writes, terminal):
    plan = deployment.write_plan(motor_plan())
    interrupted, first = run_pipe(deployment,plan,phase=phase)
    expected_exit = 1 if "absent_unknown" in phase else 0 if phase.endswith("_unknown") else 91
    assert interrupted.returncode == expected_exit, interrupted.stderr
    assert len(first.splitlines()) == writes
    assert (deployment.root/'fault.marker').read_text() == phase
    recovered, second = run_pipe(deployment)
    assert recovered.returncode == 0, recovered.stderr
    assert len(second.splitlines()) == (1 if phase in ('before_intent','intent_absent_unknown') else 0)
    rows, notices = facts(deployment)
    assert rows == [(terminal, None if terminal==3 else 29)]
    assert notices[0][0] == (3 if terminal==3 else 5)
    trace = deployment.root/'write.trace'
    expected_writes = writes + (1 if phase in ('before_intent','intent_absent_unknown') else 0)
    assert len((trace.read_bytes() if trace.exists() else b'').splitlines()) == expected_writes
    # 同请求重送、已有终态及报告重建均不能再次发出通知。
    resent, third = run_pipe(deployment,plan)
    assert resent.returncode == 0, resent.stderr
    assert third == b''
    report_plan = deployment.write_plan({'request_id':'702','created_at':'2026-10-08 00:00:00',
        'name':'重建报告','actions':[{'name':'报告','type':'report_status','params':{'scope':'full'}}]})
    rebuilt, fourth = run_pipe(deployment,report_plan)
    assert rebuilt.returncode == 0, rebuilt.stderr
    assert fourth == b''
    reports = [json.loads(path.read_bytes()) for path in deployment.ready.glob('status-report-*.json')]
    motors = [action for report in reports for plan in report.get('plans',[]) for action in plan['actions'] if action['type']=='motor_control']
    assert any(action['status']==('succeeded' if terminal==3 else 'failed') for action in motors)
    assert all(not ({'device_execution','effective_params','outputs','deliveries','result'} & action.keys()) for action in motors)


@pytest.mark.parametrize('channel,code,reason', [('absent',27,'not_connected'),('full',28,'would_block'),('broken',28,'broken_pipe')])
def test_channel_failure_is_final_and_not_retried(deployment,channel,code,reason):
    result, message = run_pipe(deployment,deployment.write_plan(motor_plan()),channel=channel)
    assert result.returncode==0,result.stderr
    assert message==b''
    assert facts(deployment)[0]==[(4,code)]
    with closing(sqlite3.connect(deployment.state_db)) as connection:
        detail=json.loads(connection.execute('SELECT error_details_json FROM actions WHERE type=8').fetchone()[0])
    assert detail['reason']==reason
    assert_motor_report(deployment,'failed','motor_channel_unavailable' if code==27 else 'motor_notification_failed')
    repeated, message=run_pipe(deployment)
    assert repeated.returncode==0,repeated.stderr
    assert message==b''


def test_missed_window_never_creates_send_intent(deployment):
    result,message=run_pipe(deployment,deployment.write_plan(motor_plan(delay=0,scheduled=future_schedule(-5))))
    assert result.returncode==0,result.stderr
    assert message==b''
    assert facts(deployment)==([(5,None)],[])
    assert assert_motor_report(deployment,'expired')['expiration_reason']=='window_missed'


@pytest.mark.parametrize('target', [{'request_id':'701'}, {'plan_instance_id':'1'},
                                  {'action_instance_id':'1'}, {'plan_instance_id':'1','group':'axis'}])
def test_all_cancel_targets_stop_future_motor_without_intent(deployment,target):
    body=motor_plan(scheduled=future_schedule(60))
    body['actions'][0]['group']='axis'
    original=deployment.write_plan(body)
    accepted=deployment.camctl('submit',str(original),'--config',str(deployment.config_path))
    assert accepted.exit_code==0,accepted.stderr
    cancel=deployment.write_plan({'request_id':'703','created_at':'2026-10-08 00:00:00',
        'name':'取消位置','actions':[{'name':'取消','type':'cancel_task','params':{'target':target}}]})
    result,message=run_pipe(deployment,cancel)
    assert result.returncode==0,result.stderr
    assert message==b''
    assert facts(deployment)==([(6,None)],[])
    assert_motor_report(deployment,'canceled')
    with closing(sqlite3.connect(deployment.state_db)) as connection:
        assert connection.execute('SELECT status FROM actions WHERE type=6').fetchone()==(3,)


def test_cancel_cannot_retract_unknown_intent(deployment):
    plan=deployment.write_plan(motor_plan())
    first,message=run_pipe(deployment,plan,phase='after_intent')
    assert first.returncode==91 and message==b''
    cancel=deployment.write_plan({'request_id':'704','created_at':'2026-10-08 00:00:00',
        'name':'取消未知发送','actions':[{'name':'取消','type':'cancel_task',
                                      'params':{'target':{'action_instance_id':'1'}}}]})
    result,message=run_pipe(deployment,cancel)
    assert result.returncode==0,result.stderr
    assert message==b''
    assert facts(deployment)[0]==[(4,29)]
    with closing(sqlite3.connect(deployment.state_db)) as connection:
        assert connection.execute('SELECT status FROM actions WHERE type=6').fetchone()==(4,)
        assert connection.execute('SELECT status,cancellation_effect FROM cancel_items').fetchone()==(4,1)


def test_admission_failure_report_preserves_original_parameters(deployment):
    body=motor_plan()
    body['actions'][0]['params']={'position': 'too far', 'extra': {'keep': [1, True, None]}}
    result,message=run_pipe(deployment,deployment.write_plan(body))
    assert result.returncode==0,result.stderr
    assert message==b''
    reports=[json.loads(path.read_text()) for path in deployment.ready.glob('status-report-*.json')]
    actions=[action for report in reports for plan in report.get('plans',[]) for action in plan['actions']]
    assert actions
    assert all(action['status']=='failed' and action['error']['stage']=='admission' for action in actions)
    assert all(action['input_params']==body['actions'][0]['params'] for action in actions)
    assert facts(deployment)[1]==[]
