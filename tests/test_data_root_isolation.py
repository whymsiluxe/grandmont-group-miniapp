"""Regression tests for DATA_ROOT isolation and configuration consistency."""
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class DataRootIsolationTests(unittest.TestCase):

    def test_current_data_root_is_not_production(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
        import main as backend
        self.assertNotEqual(
            backend.DATA_ROOT,
            '/home/grandmont/agent/miniapp',
            "DATA_ROOT is the production path — conftest.py guard failed!",
        )

    def test_current_data_root_matches_core_paths(self):
        """Runtime main.py and core.paths must resolve the same injected temp root."""
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
        import main as backend
        import core.paths as paths
        self.assertEqual(backend.DATA_ROOT, paths.DATA_ROOT)

    def test_default_data_root_literals_match_without_importing_production(self):
        """Defaults must match without importing main and writing under /home."""
        repo = Path(__file__).resolve().parent.parent
        main_src = (repo / 'backend' / 'main.py').read_text(encoding='utf-8')
        paths_src = (repo / 'backend' / 'core' / 'paths.py').read_text(encoding='utf-8')
        main_match = re.search(
            r"DATA_ROOT\s*=\s*os\.environ\.get\('MINIAPP_DATA_ROOT',\s*'([^']+)'\)",
            main_src,
        )
        paths_match = re.search(
            r"DATA_ROOT\s*=\s*os\.environ\.get\('MINIAPP_DATA_ROOT',\s*'([^']+)'\)",
            paths_src,
        )
        self.assertIsNotNone(main_match)
        self.assertIsNotNone(paths_match)
        self.assertEqual(main_match.group(1), '/home/grandmont/agent/miniapp')
        self.assertEqual(paths_match.group(1), main_match.group(1))

    def test_core_paths_default_without_env_override(self):
        """core.paths alone can safely prove the production default."""
        env = {k: v for k, v in os.environ.items() if k != 'MINIAPP_DATA_ROOT'}
        code = (
            'import sys; sys.path.insert(0, "backend"); '
            'import core.paths as p; print(p.DATA_ROOT); '
            'assert p.DATA_ROOT == "/home/grandmont/agent/miniapp"'
        )
        result = subprocess.run(
            [sys.executable, '-c', code], capture_output=True, text=True, env=env,
            cwd=os.path.join(os.path.dirname(__file__), '..'),
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_data_root_contains_pytest_prefix(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
        import main as backend
        self.assertTrue(
            'grandmont-group-pytest-' in backend.DATA_ROOT or 'grandmont-group-test-' in backend.DATA_ROOT,
            f"DATA_ROOT does not have expected prefix: {backend.DATA_ROOT}",
        )

    def test_main_raises_if_test_env_and_prod_path(self):
        env = {
            **os.environ,
            'GRANDMONT_GROUP_ENV': 'test',
            'MINIAPP_DATA_ROOT': '/home/grandmont/agent/miniapp',
            'BOT_TOKEN': 'ci-dummy',
        }
        result = subprocess.run(
            [sys.executable, '-c', 'import sys; sys.path.insert(0, "backend"); import main'],
            capture_output=True, text=True, env=env,
            cwd=os.path.join(os.path.dirname(__file__), '..'),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('REFUSING TO RUN TESTS AGAINST PRODUCTION DATA ROOT', result.stderr + result.stdout)

    def test_legacy_promonta_env_name_still_arms_guard(self):
        env = {k: v for k, v in os.environ.items() if k != 'GRANDMONT_GROUP_ENV'}
        env.update({
            'PROMONTA_ENV': 'test',
            'MINIAPP_DATA_ROOT': '/home/grandmont/agent/miniapp',
            'BOT_TOKEN': 'ci-dummy',
        })
        result = subprocess.run(
            [sys.executable, '-c', 'import sys; sys.path.insert(0, "backend"); import main'],
            capture_output=True, text=True, env=env,
            cwd=os.path.join(os.path.dirname(__file__), '..'),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('REFUSING TO RUN TESTS AGAINST PRODUCTION DATA ROOT', result.stderr + result.stdout)

    def test_main_allows_temp_path_even_with_test_env(self):
        tmp = tempfile.mkdtemp(prefix='grandmont-group-guard-test-')
        env = {
            **os.environ,
            'GRANDMONT_GROUP_ENV': 'test',
            'MINIAPP_DATA_ROOT': tmp,
            'BOT_TOKEN': 'ci-dummy',
        }
        result = subprocess.run(
            [sys.executable, '-c', 'import sys; sys.path.insert(0, "backend"); import main; print("OK")'],
            capture_output=True, text=True, env=env,
            cwd=os.path.join(os.path.dirname(__file__), '..'),
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('OK', result.stdout)

    def test_main_allows_prod_path_without_test_env(self):
        """Guard itself must not reject production mode; avoid requiring runner write access."""
        repo = Path(__file__).resolve().parent.parent
        main_src = (repo / 'backend' / 'main.py').read_text(encoding='utf-8')
        self.assertIn("_is_test_context and DATA_ROOT == _PROD_DATA_ROOT", main_src)
