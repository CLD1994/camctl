from __future__ import annotations
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from time import perf_counter
import pytest

_stream = None
_index = {}
_started = None

def _write(record):
    _stream.write(json.dumps(record, ensure_ascii=False, separators=(',', ':'))+'\n')

def pytest_configure(config):
    global _stream, _started
    _started = perf_counter()
    target=Path(os.environ['CAMCTL_TIMING_DATA'])
    target.mkdir(parents=True,exist_ok=True)
    _stream=(target/'events.jsonl').open('w',encoding='utf-8',buffering=65536)
    _write({'record':'run','utc':datetime.now(timezone.utc).isoformat(),'cwd':os.getcwd(),'args':list(config.invocation_params.args),'pid':os.getpid()})

def pytest_collection_finish(session):
    repo=Path(os.environ['CAMCTL_TIMING_REPO'])
    inventory=[]
    for item in session.items:
        relative=Path(item.path).resolve().relative_to(repo).as_posix()
        suffix=item.nodeid.split('::',1)[1] if '::' in item.nodeid else ''
        full=relative+('::'+suffix if suffix else '')
        _index[item.nodeid]=full
        inventory.append({'nodeid':full,'raw_nodeid':item.nodeid,'file':relative,'line':item.location[1]+1,'function':getattr(item,'originalname',None),'fixtures':list(item.fixturenames),'markers':[m.name for m in item.iter_markers()]})
    path=Path(os.environ['CAMCTL_TIMING_DATA'])/'inventory.json'
    path.write_text(json.dumps(inventory,ensure_ascii=False,indent=2)+'\n')
    _write({'record':'collection','items':len(inventory)})
    _stream.flush()

def pytest_runtest_logreport(report):
    data={'record':'phase','nodeid':_index.get(report.nodeid,report.nodeid),'phase':report.when,'duration_seconds':report.duration,'outcome':report.outcome,'wasxfail':getattr(report,'wasxfail',None)}
    if report.skipped:
        data['reason']=str(report.longrepr)
    _write(data)

@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef, request):
    started=perf_counter()
    outcome=yield
    _write({'record':'fixture_setup','name':fixturedef.argname,'scope':fixturedef.scope,'baseid':fixturedef.baseid,'owner':request.node.nodeid,'duration_seconds':perf_counter()-started,'outcome':'failed' if outcome.excinfo else 'passed'})

def pytest_sessionfinish(session,exitstatus):
    _write({'record':'finish','exit_status':int(exitstatus),'collected':session.testscollected,'failed':session.testsfailed,'elapsed_seconds':perf_counter()-_started,'utc':datetime.now(timezone.utc).isoformat()})
    _stream.flush()

def pytest_unconfigure(config):
    global _stream
    if _stream is not None:
        _stream.close()
        _stream=None
