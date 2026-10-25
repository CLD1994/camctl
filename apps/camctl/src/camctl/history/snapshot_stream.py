"""磁盘历史句柄的逐块快照编码与增量解码。"""
from contextlib import closing
import codecs
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory, TemporaryFile

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
    detached=handle._db is None
    try:
        return _prepare_segment_snapshot(handle)
    finally:
        if detached:
            handle.detach()


def _prepare_segment_snapshot(handle):
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
    """LF前只暂存磁盘和固定UTF8余量；完整行构造一个必要值。

    第一版单个字段须能在目标环境处理。完整行的解析驻留取决于
    该行大小；总正文、缺LF残留和累计行数均不进入Python全集。
    """
    decoder=codecs.getincrementaldecoder('utf-8')('strict')
    try:
        with TemporaryFile(mode='w+b',prefix='camctl-snapshot-row-') as pending:
            length=0
            for chunk in chunks:
                # 来源按CHUNK_BYTES读取；对直接调用也控制每次解码的片段。
                for offset in range(0,len(chunk),CHUNK_BYTES):
                    block=chunk[offset:offset+CHUNK_BYTES]
                    start=0
                    while start<len(block):
                        end=block.find(b'\n',start)
                        fragment=block[start:] if end<0 else block[start:end]
                        decoder.decode(fragment,final=False)
                        if pending.write(fragment)!=len(fragment):
                            raise SnapshotMaintenanceError('快照临时行写入不完整')
                        length+=len(fragment)
                        if end<0:
                            break
                        decoder.decode(b'',final=True)
                        pending.seek(0)
                        raw=pending.read()
                        try:
                            if len(raw)!=length:
                                raise SnapshotMaintenanceError('快照临时行回读不完整')
                            text=raw.decode('utf-8')
                        finally:
                            del raw
                        try:
                            value=parse_exact_json(text)
                        finally:
                            del text
                        pending.seek(0);pending.truncate(0)
                        length=0;decoder.reset()
                        yield value
                        del value
                        start=end+1
            decoder.decode(b'',final=True)
            if length:
                raise SnapshotMaintenanceError('快照正文缺少结尾换行')
    except (ValueError,UnicodeError) as error:
        raise SnapshotMaintenanceError('快照行无法增量解码') from error


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


def load_snapshot(handle, *, include_row=None):
    choice = handle.seed.snapshot
    if choice is None:
        raise ConsistencyError('请求的快照恢复路径没有可靠S')
    try:
        with closing(_records(_chunks(handle,choice))) as records:
            header = next(records,None)
            expected = {'format_version':FORMAT_VERSION,'entity_type':handle.seed.ref[0],
                        'entity_id':handle.seed.ref[1],'boundary_event_id':choice.boundary.last_event_id,
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
                previous = key
                if include_row is not None and not include_row(table, values):
                    continue
                current = handle._get(*key,source='candidates')
                if current is None or values.keys() != current.keys():
                    raise SnapshotMaintenanceError('快照行超出自身范围或列不完整')
                for column in (*registry[table]['immutable'],'id','created_event_id'):
                    if column in current and not json_equal(values[column],current[column]):
                        raise SnapshotMaintenanceError('快照不可变身份不符合永久投影')
                fixed=handle._fixed_values(*key)
                for column in registry[table]['write_once']:
                    if not json_equal(values[column],fixed[column]):
                        raise SnapshotMaintenanceError('快照一次固定事实不属于原S')
                if ('created_event_id' in values and values['created_event_id'] > choice.boundary.last_event_id):
                    raise SnapshotMaintenanceError('快照包含S之后创建的行')
                handle._put(*key,values)
                del record,values,current,fixed
            primary = handle._get(handle.seed.primary_table,handle.seed.ref[1])
            derived = registry[handle.seed.primary_table]['derived']
            if (primary is None
                    or 'last_event_id' in derived and primary['last_event_id'] != choice.position[0]
                    or 'change_count' in derived and primary['change_count'] != choice.position[1]):
                raise SnapshotMaintenanceError('快照主行缺失或目录计数不符')
    except (SnapshotMaintenanceError,ValueError,TypeError,KeyError) as error:
        raise ConsistencyError('已选快照损坏，无法可靠恢复') from error
