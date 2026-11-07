"""维护入口和显式初始化共用的部署布局定位。"""
import json
import os
from pathlib import Path


def environment_locator(environment):
    return Path(environment) / '.camctl-maintenance-layout'


def database_locator(state_db):
    path = Path(state_db)
    return path.with_name(path.name + '.camctl-maintenance-layout')


def read_reference(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError('维护布局定位无效: ' + str(path))
    return Path(value)


def check_initialization(environment, state_db, staging, ready, processing):
    environment = Path(environment).resolve()
    state_db = Path(state_db).resolve()
    references = [read_reference(environment_locator(environment)),
                  read_reference(database_locator(state_db))]
    present = [reference for reference in references if reference is not None]
    if not present:
        return
    if any(reference != present[0] for reference in present):
        raise ValueError('维护布局定位不一致')
    layout = json.loads(present[0].read_text(encoding='utf-8'))
    expected = {'environment': str(environment), 'state_db': str(state_db),
                'staging': os.path.abspath(staging), 'ready': os.path.abspath(ready),
                'processing': os.path.abspath(processing)}
    if not isinstance(layout, dict) or layout != expected:
        raise ValueError('已登记维护的部署不能改变状态库或交接目录')
