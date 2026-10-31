"""部署维护操作对 SQLite 内容和外键的一致性检查。"""


def verify_maintenance_integrity(connection):
    integrity = connection.execute('PRAGMA integrity_check').fetchall()
    if integrity != [('ok',)]:
        raise ValueError('状态库完整性检查失败: ' + repr(integrity))
    violation = connection.execute('PRAGMA foreign_key_check').fetchone()
    if violation is not None:
        raise ValueError('状态库存在外键违规: ' + repr(violation))
