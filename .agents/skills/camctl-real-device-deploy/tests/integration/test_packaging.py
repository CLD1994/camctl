"""交付归档与文件校验的真实文件边界。完整三组件构建由 package.py 自带验收。"""
import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile


SCRIPTS = Path(__file__).parents[2] / 'scripts'


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


package = load('package')
verify = load('verify_bundle')


class PackagingFiles(unittest.TestCase):
    def test_zip_has_fixed_root_order_metadata_and_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / 'input'
            bundle.mkdir()
            (bundle / '说明.txt').write_text('中文\n')
            (bundle / 'start.sh').write_text('#!/bin/sh\n')
            package.write_checksums(bundle)
            first, second = root / 'a.zip', root / 'b.zip'
            package.write_zip(bundle, first)
            (bundle / 'start.sh').chmod(0o777)
            package.write_zip(bundle, second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with zipfile.ZipFile(first) as archive:
                self.assertEqual(archive.namelist(), sorted(archive.namelist()))
                for info in archive.infolist():
                    self.assertTrue(info.filename.startswith('camctl-deploy/'))
                    self.assertEqual(info.date_time, (1980, 1, 1, 0, 0, 0))
                archive.extractall(root / 'extracted')
            verify.verify_checksums(root / 'extracted/camctl-deploy')

    def test_checksum_rejects_changed_missing_and_extra_files(self):
        for mutation in ('changed', 'missing', 'extra', 'nested_manifest'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                bundle = Path(directory)
                (bundle / 'README.md').write_text('原始说明')
                package.write_checksums(bundle)
                if mutation == 'changed':
                    (bundle / 'README.md').write_text('修改后')
                elif mutation == 'missing':
                    (bundle / 'README.md').unlink()
                elif mutation == 'extra':
                    (bundle / 'unexpected').write_text('意外文件')
                else:
                    (bundle / 'nested').mkdir()
                    (bundle / 'nested/SHA256SUMS').write_text('意外文件')
                with self.assertRaises(ValueError):
                    verify.verify_checksums(bundle)


if __name__ == '__main__':
    unittest.main()
