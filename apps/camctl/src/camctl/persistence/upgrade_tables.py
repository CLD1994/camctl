"""显式升级的表集合契约；调用方负责读取实际结构与记录数。"""


def validate_upgrade_tables(*, original, target, declared_added, nonempty_added):
    original, target, declared_added = set(original), set(target), set(declared_added)
    if original & declared_added:
        raise ValueError("原格式不能提前拥有新增格式表")
    if target != original | declared_added:
        raise ValueError("升级副本必须精确保留原表并仅增加已登记的新表")
    if set(nonempty_added):
        raise ValueError("新增格式表必须为空，不从旧尝试补造归属")
