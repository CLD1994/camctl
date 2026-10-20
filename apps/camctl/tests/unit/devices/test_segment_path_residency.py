"""增长目录中的动态名称不进入进程级字符串驻留表。"""
import sys
import pytest
from camctl.devices.drivers.adb_cameras.osmo360ii_segments import parse_osmo_segment_name


def test_dynamic_segment_filename_is_not_interned(monkeypatch):
    original=sys.intern
    seen=[]
    def intern(value):
        if value.startswith('CAM_20261021'):seen.append(value)
        return original(value)
    monkeypatch.setattr(sys,'intern',intern)
    assert parse_osmo_segment_name('/DCIM/CAM001/CAM_20261021090000_0001_D.OSV') is not None
    assert seen==[]

@pytest.mark.parametrize('prefix,valid',[('/DCIM/',True),('//DCIM/',True),('///DCIM/',False),('/DCIM//',False),('/DCIM/./',False),('/DCIM/../',False)])
def test_canonical_segment_path_prefix_keeps_existing_semantics(prefix,valid):
    assert (parse_osmo_segment_name(prefix+'CAM001/CAM_20261021090000_0001_D.OSV') is not None) is valid

@pytest.mark.parametrize('suffix',['/','/.','/..'])
def test_canonical_segment_filename_cannot_have_trailing_path_segments(suffix):
    assert parse_osmo_segment_name('/DCIM/CAM001/CAM_20261021090000_0001_D.OSV'+suffix) is None
