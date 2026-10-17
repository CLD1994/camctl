"""冻结产物规则明确预览是否必要；未知角色保留原语义。"""
import pytest

from camctl.capture.results import FileKind, ProductRule


@pytest.mark.parametrize('kind,format_id,paired,rules,want', [
    (FileKind.VIDEO,'lrf','/DCIM/a.OSV',(ProductRule(FileKind.VIDEO,'osv'),),True),
    (FileKind.VIDEO,'lrf','/DCIM/a.OSV',(ProductRule(FileKind.VIDEO,'lrf',min_count=1),),False),
    (FileKind.VIDEO,'lrf','/DCIM/a.OSV',(ProductRule(FileKind.VIDEO,'lrf',min_count=0,exact_count=1),),False),
    (FileKind.VIDEO,'lrf','/DCIM/a.OSV',(ProductRule(FileKind.VIDEO,'lrf',min_count=0),),True),
    (FileKind.VIDEO,'lrf','/DCIM/a.OSV',(ProductRule(FileKind.VIDEO),),False),
    (FileKind.VIDEO,'lrf','/DCIM/a.OSV',(),False),
    (FileKind.VIDEO,None,'/DCIM/a.OSV',(ProductRule(FileKind.VIDEO,'osv'),),False),
    (FileKind.VIDEO,'osv',None,(ProductRule(FileKind.VIDEO,'osv'),),False),
    (FileKind.VIDEO,'lrf',None,(ProductRule(FileKind.VIDEO,'osv'),),False),
    (FileKind.OTHER,'lrf','/DCIM/a.OSV',(ProductRule(FileKind.VIDEO,'osv'),),False),
])
def test_optional_preview_requires_explicit_format_pair_role_and_nonrequired_rules(kind,format_id,paired,rules,want):
    from camctl.capture.results import is_optional_preview
    assert is_optional_preview(kind,format_id,paired,rules) is want
