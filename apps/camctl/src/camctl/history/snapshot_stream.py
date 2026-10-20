"""磁盘历史句柄的逐块快照编码与增量解码。"""
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.json_values import json_equal, parse_exact_json
from camctl.contracts.values import ConsistencyError, ObjectId
from camctl.history.events import load_event_registry
from camctl.history.snapshots import CHUNK_BYTES, FORMAT_VERSION, SnapshotMaintenanceError
from camctl.persistence.transaction import encode_json_value


@dataclass(frozen=True)
class PreparedSnapshotSource:
    entity_type: int
    entity_id: int
    boundary: HistoryBoundary
    change_count: int
    format_version: int
    length: int
    _temporary: TemporaryDirectory
    path: Path

    @property
    def chunks(self):
        """每次调用从首块重读；同S重送使用相同的有限来源。"""
        with self.path.open('rb') as source:
            total = 0
            while chunk := source.read(CHUNK_BYTES):
                total += len(chunk)
                yield chunk
            if total != self.length:
                raise SnapshotMaintenanceError('快照来源长度发生变化')

    def close(self):
        self._temporary.cleanup()


def prepare_segment_snapshot(handle):
    handle._require_ready()
    if handle.seed.floor[1] < 1:
        raise SnapshotMaintenanceError('不存在的动作不能保存快照')
    temporary = TemporaryDirectory(prefix='camctl-snapshot-')
    path = Path(temporary.name)/'content.jsonl'
    try:
        length = 0
        header = {'format_version':FORMAT_VERSION,'entity_type':handle.seed.ref[0],
                  'entity_id':handle.action_id,'boundary_event_id':handle.boundary.last_event_id,
                  'change_count':handle.seed.floor[1]}
        with path.open('wb') as target:
            def write(value):
                nonlocal length
                raw = (encode_json_value(value)+'\n').encode('utf-8')
                for offset in range(0,len(raw),CHUNK_BYTES):
                    length += target.write(raw[offset:offset+CHUNK_BYTES])
            write(header)
            for record in handle.iter_rows():
                write({'table':record.table,'row':dict(record.values)})
        return PreparedSnapshotSource(handle.seed.ref[0],handle.action_id,handle.boundary,
            handle.seed.floor[1],FORMAT_VERSION,length,temporary,path)
    except BaseException:
        temporary.cleanup()
        raise


def _records(chunks):
    buffer = bytearray()
    for chunk in chunks:
        buffer.extend(chunk)
        while (end := buffer.find(b'\n')) >= 0:
            raw = bytes(buffer[:end])
            del buffer[:end+1]
            try:
                yield parse_exact_json(raw.decode('utf-8'))
            except (ValueError,UnicodeError) as error:
                raise SnapshotMaintenanceError('快照行无法增量解码') from error
    if buffer:
        raise SnapshotMaintenanceError('快照正文缺少结尾换行')


def _chunks(handle,choice):
    offset = 0
    size = None
    while size is None or offset < size:
        with handle.repository._read_connection() as connection:
            with closing(connection.execute('SELECT entity_type,entity_id,boundary_event_id,change_count,format_version,length(content) FROM entity_snapshots WHERE id=?',(choice.snapshot_id,))) as cursor:
                row = cursor.fetchone()
            if row is None or tuple(row[:5]) != (*handle.seed.ref,choice.boundary.last_event_id,choice.position[1],choice.format_version):
                raise SnapshotMaintenanceError('固定快照身份或元信息发生变化')
            if size is not None and size != row[5]:
                raise SnapshotMaintenanceError('固定快照长度发生变化')
            size = row[5]
            with connection.blobopen('entity_snapshots','content',choice.snapshot_id,readonly=True) as blob:
                blob.seek(offset)
                chunk = blob.read(min(CHUNK_BYTES,size-offset))
        if not chunk:
            raise SnapshotMaintenanceError('快照正文缺失')
        offset += len(chunk)
        yield chunk


def load_snapshot(handle):
    choice = handle.seed.snapshot
    if choice is None:
        raise ConsistencyError('请求的快照恢复路径没有可靠S')
    try:
        records = iter(_records(_chunks(handle,choice)))
        header = next(records,None)
        expected = {'format_version':FORMAT_VERSION,'entity_type':handle.seed.ref[0],
                    'entity_id':handle.action_id,'boundary_event_id':choice.boundary.last_event_id,
                    'change_count':choice.position[1]}
        if not json_equal(header,expected):
            raise SnapshotMaintenanceError('快照头不符合固定S及目录计数')
        registry = load_event_registry()['tables']
        previous = ('',0)
        for record in records:
            if not isinstance(record,dict) or set(record) != {'table','row'}:
                raise SnapshotMaintenanceError('快照行结构非法')
            table,values = record['table'],record['row']
            if table not in registry or not isinstance(values,dict):
                raise SnapshotMaintenanceError('快照行类型非法')
            ObjectId(values.get('id'))
            key = (table,values['id'])
            if key <= previous:
                raise SnapshotMaintenanceError('快照行排序不连续或包含重复身份')
            current = handle._get(*key,source='candidates')
            if current is None or values.keys() != current.keys():
                raise SnapshotMaintenanceError('快照行超出自身范围或列不完整')
            for column in (*registry[table]['immutable'],'id','created_event_id'):
                if column in current and not json_equal(values[column],current[column]):
                    raise SnapshotMaintenanceError('快照不可变身份不符合永久投影')
            for column in registry[table]['write_once']:
                if values[column] is not None and not json_equal(values[column],current[column]):
                    raise SnapshotMaintenanceError('快照一次固定事实与原投影矛盾')
            if ('created_event_id' in values and values['created_event_id'] > choice.boundary.last_event_id):
                raise SnapshotMaintenanceError('快照包含S之后创建的行')
            handle._put(*key,values)
            previous = key
        primary = handle._get('actions',handle.action_id)
        if primary is None or (primary['last_event_id'],primary['change_count']) != choice.position:
            raise SnapshotMaintenanceError('快照主行缺失或目录计数不符')
    except (SnapshotMaintenanceError,ValueError,TypeError,KeyError) as error:
        raise ConsistencyError('已选快照损坏，无法可靠恢复') from error
