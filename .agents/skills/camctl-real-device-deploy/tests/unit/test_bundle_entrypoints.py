"""随包 Python 入口导入本地模块前禁止产生缓存。"""
import builtins
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts'


class LocalImportObserved(Exception):
    pass


class BundleEntrypointTests(unittest.TestCase):
    def assert_bytecode_disabled_before_import(self, filename, local_module, *, call_main):
        spec = importlib.util.spec_from_file_location('bundle_entrypoint_under_test', SCRIPTS / filename)
        module = importlib.util.module_from_spec(spec)
        # 仅解释器加载源码；切换标志前完成，避免加载器写入缓存。
        code = spec.loader.get_code(spec.name)
        original_import = builtins.__import__
        observed = []

        def intercept(name, *args, **kwargs):
            if name == local_module:
                observed.append(sys.dont_write_bytecode)
                raise LocalImportObserved
            return original_import(name, *args, **kwargs)

        with patch.object(sys, 'dont_write_bytecode', False), patch('builtins.__import__', side_effect=intercept):
            with self.assertRaises(LocalImportObserved):
                exec(code, module.__dict__)
                if call_main:
                    module.main()
        self.assertEqual(observed, [True])

    def test_updater_disables_bytecode_before_bundle_import_on_each_start(self):
        self.assert_bytecode_disabled_before_import('update-camctl.py', 'bundle_support', call_main=False)

    def test_installer_disables_bytecode_before_first_bundle_import(self):
        self.assert_bytecode_disabled_before_import('install-host.py', 'deploy_support', call_main=True)


if __name__ == '__main__':
    unittest.main()
