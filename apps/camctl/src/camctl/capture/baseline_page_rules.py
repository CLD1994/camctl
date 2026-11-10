"""按原调用范围核对一页基准目录的推进。"""

from camctl.contracts.values import ConsistencyError


def validate_baseline_page_progress(*, binding, directories, cursor, page):
    """输入是原调用事实和至多一页实际返回，不读取其他批次。"""
    index = 0 if cursor is None else cursor.directory_index
    previous = None if cursor is None else cursor.after_path
    if (any(not item.path.startswith(directories[index] + '/') for item in page.items)
            or any(a.sort_key >= b.sort_key for a, b in zip(page.items, page.items[1:]))
            or page.items and previous is not None and page.items[0].path <= previous):
        raise ConsistencyError('基准返回没有沿原输入目录及游标有序推进')
    next_cursor = page.next_cursor
    if next_cursor is not None:
        if next_cursor.binding != binding or next_cursor.directories != directories:
            raise ConsistencyError('基准返回游标改变原绑定或完整目录')
        same = (next_cursor.directory_index == index and page.items
                and next_cursor.after_path == page.items[-1].path)
        following = next_cursor.directory_index == index + 1 and next_cursor.after_path is None
        if not (same or following):
            raise ConsistencyError('基准返回游标不是本目录进度或下一目录')
    elif index + 1 != len(directories):
        raise ConsistencyError('基准返回尚有目录但提前结束')
