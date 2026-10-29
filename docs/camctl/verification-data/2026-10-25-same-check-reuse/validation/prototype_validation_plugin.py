from __future__ import annotations
import ast
import hashlib
import importlib
import inspect
import json
import os
from pathlib import Path
import platform
import sqlite3
import sys
import time
import pytest

ROOT = Path('/tmp/camctl-same-check-options-ykdqfnct')
RUN = ROOT / 'validation'
SOURCE = ROOT / 'source'
CANDIDATES = ROOT / 'candidate-functions.py'
STATE = {'scope': '外部函数替换的可行性试验，非仓库正式实现或全量回归', 'pid': os.getpid(), 'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'python': platform.python_version(), 'executable': sys.executable, 'sqlite': sqlite3.sqlite_version, 'replacements': [], 'reports': [], 'current': None}

def save():
    (RUN/'pytest-progress.json').write_text(json.dumps(STATE,ensure_ascii=False,indent=2)+'\n')

def audit_paths():
    paths = {}
    for name, module in tuple(sys.modules.items()):
        if name == 'camctl' or name.startswith('camctl.'):
            path = getattr(module,'__file__',None)
            if path:
                resolved=Path(path).resolve()
                if not resolved.is_relative_to(SOURCE/'apps/camctl/src'):
                    raise RuntimeError(f'camctl imported outside isolated source: {name}={resolved}')
                paths[name]=str(resolved)
    STATE['camctl_imports']=paths

@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(session, config, items):
    text=CANDIDATES.read_text()
    nodes={node.name:node for node in ast.parse(text,filename=str(CANDIDATES)).body if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef))}
    expected={'_verify_page_relations','_verify_scope_members','_canonical_events'}
    if set(nodes)!=expected:raise RuntimeError(f'candidate function names differ: {set(nodes)}')
    for module_name,names in [('camctl.persistence.repositories.result_pages',('_verify_page_relations',)),('camctl.persistence.repositories.segment_transactions',('_verify_scope_members','_canonical_events'))]:
        module=importlib.import_module(module_name)
        if not Path(module.__file__).resolve().is_relative_to(SOURCE/'apps/camctl/src'):
            raise RuntimeError(f'target imported outside isolated source: {module.__file__}')
        for name in names:
            old=getattr(module,name)
            signature=str(inspect.signature(old))
            was_generator=inspect.isgeneratorfunction(old)
            tree=ast.Module(body=[nodes[name]],type_ignores=[])
            exec(compile(tree,str(CANDIDATES),'exec',dont_inherit=True),module.__dict__,module.__dict__)
            new=getattr(module,name)
            if new is old or new.__globals__ is not module.__dict__ or str(inspect.signature(new))!=signature or inspect.isgeneratorfunction(new)!=was_generator:
                raise RuntimeError(f'replacement assembly mismatch: {module_name}.{name}')
            STATE['replacements'].append({'module':module_name,'module_file':str(Path(module.__file__).resolve()),'function':name,'old_id':id(old),'new_id':id(new),'old_code_file':old.__code__.co_filename,'new_code_file':new.__code__.co_filename,'signature':signature,'is_generator':was_generator,'full_target_globals':new.__globals__ is module.__dict__,'candidate_ast_sha256':hashlib.sha256(ast.dump(nodes[name],include_attributes=False).encode()).hexdigest()})
    audit_paths()
    STATE['collected']=len(items)
    STATE['nodeids']=[item.nodeid for item in items]
    save()

def pytest_runtest_logstart(nodeid, location):
    STATE['current']={'nodeid':nodeid,'started_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())}
    save()

def pytest_runtest_logreport(report):
    STATE['reports'].append({'nodeid':report.nodeid,'when':report.when,'outcome':report.outcome,'duration':report.duration,'failure':str(report.longrepr) if report.failed else None})
    save()

def pytest_sessionfinish(session, exitstatus):
    STATE['exit_code']=int(exitstatus)
    STATE['finished_utc']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
    STATE['current']=None
    audit_paths()
    save()
