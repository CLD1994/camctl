"""从 camctl 的权威源码加载交付校验规则。"""
from pathlib import Path

_source = Path(__file__).resolve().parents[4] / 'apps/camctl/src/camctl/update_engine/bundle_support.py'
exec(compile(_source.read_bytes(), str(_source), 'exec'))
