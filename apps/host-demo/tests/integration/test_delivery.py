"""通过实际源码包、安装目录和独立调用方验证交付契约。"""
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

SOURCE, CMAKE = pathlib.Path(sys.argv[1]), sys.argv[2]
CPACK = str(pathlib.Path(CMAKE).with_name('cpack'))
sys.argv = [sys.argv[0], *sys.argv[3:]]


class DeliveryIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='camctl-delivery-')
        cls.root = pathlib.Path(cls.tmp.name)
        cls.installed = None
        cls.configurations = 0

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_command(self, args, cwd):
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, f'{args}\n{result.stdout}\n{result.stderr}')
        return result.stdout

    def configure(self, parent, *, build_within_source=False):
        source = self.root / parent / 'source'
        shutil.copytree(SOURCE, source, ignore=shutil.ignore_patterns(
            'build*', '.git', '.local', '__pycache__', '*.pyc'))
        expected = {p.relative_to(source).as_posix(): p.read_bytes()
                    for p in source.rglob('*') if p.is_file()}
        for relative in ['build-junk/marker', '.local/cache/marker', '.git/marker',
                         'tests/__pycache__/marker.pyc', 'generated.pyc']:
            marker = source / relative
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text('excluded artifact')
        type(self).configurations += 1
        build = source / 'out' if build_within_source else self.root / f'output-{self.configurations}'
        build.mkdir()
        hook = self.root / 'project-info.cmake'
        hook.write_text('file(WRITE "${CMAKE_BINARY_DIR}/project-info.txt" '
                        '"${PROJECT_NAME}|${PROJECT_VERSION}|${PROJECT_VERSION_MAJOR}|'
                        '${PROJECT_VERSION_MINOR}|${PROJECT_VERSION_PATCH}")\n')
        self.run_command([CMAKE, str(source), '-DCMAKE_BUILD_TYPE=Release',
                          '-DCMAKE_INSTALL_LIBDIR=lib',
                          f'-DCMAKE_INSTALL_PREFIX={build}/installed',
                          f'-DCMAKE_PROJECT_camctl_host_INCLUDE={hook}'], build)
        return source, build, expected

    def source_package(self, build):
        self.run_command([CPACK, '--config', str(build / 'CPackSourceConfig.cmake')], build)
        packages = list(build.glob('*-Source.tar.gz'))
        self.assertEqual(len(packages), 1)
        return packages[0]

    def installation(self):
        cls = type(self)
        if cls.installed is None:
            source, build, _ = self.configure('installation')
            self.run_command([CMAKE, '--build', str(build), '--target', 'install', '--', '-j2'], build)
            cls.installed = source, build, build / 'installed'
        return cls.installed

    def test_source_archive_excludes_only_component_artifacts(self):
        for parent in ['normal', 'build-area', '.local', 'space area', 'meta [1]+(test)']:
            with self.subTest(parent=parent):
                _, build, expected = self.configure(parent, build_within_source=parent == 'normal')
                package = self.source_package(build)
                extract = build / 'extracted'
                with tarfile.open(package) as archive:
                    files = [m for m in archive.getmembers() if m.isfile()]
                    self.assertTrue(files, '源码包不能为空')
                    actual = {m.name.split('/', 1)[1]: archive.extractfile(m).read() for m in files}
                    self.assertEqual(actual.keys(), expected.keys(), '源码缺失或混入构建产物')
                    for relative, content in expected.items():
                        self.assertEqual(actual[relative], content, relative)
                    archive.extractall(extract)
                unpacked = next(extract.iterdir())
                independent = build / 'independent'
                independent.mkdir()
                self.run_command([CMAKE, str(unpacked), '-DCMAKE_BUILD_TYPE=Release'], independent)
                self.run_command([CMAKE, '--build', str(independent), '--', '-j2'], independent)
                self.assertTrue((independent / 'libcamctl_host.a').is_file())
                self.assertTrue((unpacked / 'vendor/yyjson/LICENSE').is_file())

    def test_package_versions_match_actual_project(self):
        _, build, _ = self.installation()
        name, version, major, minor, patch = (build / 'project-info.txt').read_text().split('|')
        for config in ['CPackConfig.cmake', 'CPackSourceConfig.cmake']:
            script = build / 'read-package-info.cmake'
            info = build / 'package-info.txt'
            script.write_text(f'include("{build / config}")\n'
                              f'file(WRITE "{info}" '
                              '"${CPACK_PACKAGE_NAME}|${CPACK_PACKAGE_VERSION}|'
                              '${CPACK_PACKAGE_VERSION_MAJOR}|${CPACK_PACKAGE_VERSION_MINOR}|'
                              '${CPACK_PACKAGE_VERSION_PATCH}")\n')
            self.run_command([CMAKE, '-P', str(script)], build)
            self.assertEqual(info.read_text().split('|'), [name, version, major, minor, patch])
        source_package = self.source_package(build)
        self.assertEqual(source_package.name, f'{name}-{version}-Source.tar.gz')
        self.run_command([CPACK, '--config', str(build / 'CPackConfig.cmake')], build)
        binary = [p for p in build.glob('*.tar.gz') if not p.name.endswith('-Source.tar.gz')]
        self.assertEqual(len(binary), 1)
        self.assertTrue(binary[0].name.startswith(f'{name}-{version}-'))

    def test_installed_readme_links_resolve(self):
        _, _, prefix = self.installation()
        readme = prefix / 'share/doc/camctl_host/README.md'
        links = re.findall(r'\[[^\]]+\]\(([^)]+)\)', readme.read_text())
        self.assertTrue(links)
        for link in links:
            if '://' in link or link.startswith('#'):
                continue
            with self.subTest(link=link):
                self.assertTrue((readme.parent / link.split('#', 1)[0]).exists(), link)

    def test_installed_example_uses_public_header_and_library(self):
        _, build, prefix = self.installation()
        example = prefix / 'share/doc/camctl_host/examples/demo.c'
        executable = build / 'independent-example'
        self.run_command(['cc', '-std=c11', '-D_GNU_SOURCE', str(example),
                          f'-I{prefix}/include', str(prefix / 'lib/libcamctl_host.a'),
                          '-pthread', '-o', str(executable)], build)
        self.run_command([str(executable), '--help'], build)


if __name__ == '__main__':
    unittest.main()
