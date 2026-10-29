"""只执行已保存替身与生产帧解码器，不启动 pytest 或设备调用。"""
import ast
import asyncio
import json
from pathlib import Path
import shlex
from types import SimpleNamespace

from camctl.devices.bindings import DeviceBinding
from camctl.devices.file_identity import FileIdentity
from camctl.devices.drivers.adb_cameras.filesystem import (
    METADATA_FRAME_HEADER, ShellFileTools, _decode_metadata_frame,
)
from camctl.devices.drivers.adb_cameras.directory_scan import _DirectoryFrame
from camctl.operations.process import LocalExit, RawToolOutcome

ROOT = Path('/tmp/camctl-failure-diagnosis-20261025')
test_path = ROOT / 'apps/camctl/tests/integration/bootstrap/test_osmo_shared_recovery.py'
tree = ast.parse(test_path.read_text())
channel_ast = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Channel')
path = '/mnt/media_rw/emulated/DCIM/CAM_001/CAM_20261022100000_0007_D.OSV'
namespace = {'OSV_PATH': path, 'shlex': shlex, 'LocalExit': LocalExit, 'RawToolOutcome': RawToolOutcome}
exec(compile(ast.Module(body=[channel_ast], type_ignores=[]), str(test_path), 'exec'), namespace)

async def main():
    channel = namespace['Channel']()
    binding = DeviceBinding('cam-1', 'dji-osmo360-ii')
    identity = FileIdentity(binding, path)
    script = ShellFileTools().metadata_many_script((path,))
    spec = SimpleNamespace(argv=('adb', '-s', 'serial-1', 'shell', '-T', 'sh -c ' + shlex.quote(script)))
    raw = await channel.run(spec, None)
    items, errors, complete = _decode_metadata_frame(raw.output, (identity,))
    metadata = {'fixture_output_hex': raw.output.hex(), 'fixture_exit_code': raw.exit.exit_code,
                'decoded_item_count': len(items), 'frame_errors': errors, 'frame_complete': complete}
    corrected_frame = METADATA_FRAME_HEADER + b'\0'.join((path.encode(), b'50\n', b'', b'0', b'END', b''))
    control_items, control_errors, control_complete = _decode_metadata_frame(corrected_frame, (identity,))
    metadata['valid_frame_control'] = {'decoded_sizes': [item.value for item in control_items],
        'frame_errors': control_errors, 'frame_complete': control_complete}

    stream_data = []
    async def receive(data):
        stream_data.append(data)
    stream_raw = await channel.run_stream(SimpleNamespace(argv=()), None, receive)
    frame = _DirectoryFrame(binding, '/mnt/media_rw/emulated/DCIM')
    for data in stream_data:
        frame.receive(data)
    try:
        frame.finish()
    except ValueError as error:
        frame_error = str(error)
    else:
        frame_error = None
    directory = {'fixture_output_hex': b''.join(stream_data).hex(),
        'fixture_exit_code': stream_raw.exit.exit_code, 'stderr': stream_raw.stderr.decode(),
        'frame_error': frame_error}
    channel.forbidden = True
    channel.allowed_ticket = (3, 2)
    channel.current_result_ticket = lambda: (3, 2)
    try:
        await channel.run_stream(SimpleNamespace(argv=()), None, receive)
    except AssertionError as error:
        directory['allowed_new_result_ticket_still_rejected'] = str(error)
    result = {'source': str(ROOT), 'python': __import__('sys').version,
        'metadata': metadata, 'directory_stream': directory}
    print(json.dumps(result, ensure_ascii=False, indent=2))

asyncio.run(main())
