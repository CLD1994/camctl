"""从 camctl 权威源码生成只依赖标准库的维护引擎。"""
from pathlib import Path
import zipfile


def build(project: Path, output: Path) -> None:
    source = project / 'src/camctl'
    engine = source / 'update_engine'
    members = {
        '__main__.py': b"import sys\nsys.dont_write_bytecode = True\nif len(sys.argv) > 1 and sys.argv[1] == '--worker':\n    del sys.argv[1]\n    import worker\n    raise SystemExit(worker.main())\nimport engine\nraise SystemExit(engine.main())\n",
        **{name: (engine / name).read_bytes() for name in
           ('engine.py', 'update_transaction.py', 'bundle_support.py', 'worker.py', 'probe.py', 'diagnostic_fallback.py')},
        'maintenance_integrity.py': (source / 'persistence/maintenance_integrity.py').read_bytes(),
        'error_diagnostics.py': (source / 'operations/error_diagnostics.py').read_bytes(),
    }
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(members.items()):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
