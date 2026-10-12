"""Regression tests for DATA_ROOT isolation and configuration consistency."""
import os
import subprocess
import sys
import tempfile
import unittest


class DataRootIsolationTests(unittest.TestCase):

    def test_current_data_root_is_not_production(self):
        """The current pytest session must have isolated DATA_ROOT (conftest.py ran)."""
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
        import main as backend
        self.assertNotEqual(
            backend.DATA_ROOT,
            '/home/grandmont/agent/miniapp',
            "DATA_ROOT is the production path — conftest.py guard failed!",
        )

    def test_current_data_root_matches_core_paths(self):
        """main.py and core.paths must never derive different storage roots."""
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
        import main as backend
        import core.paths as paths
        self.assertEqual(
            backend.DATA_ROOT,
            paths.DATA_ROOT,
            f"Split storage roots: main={backend.DATA_ROOT!r}, core.paths={paths.DATA_ROOT!r}",
        )

    def test_default_data_root_matches_without_env_override(self):
        """Production-style import with MINIAPP_DATA_ROOT unset must derive one root."""
        env = {k: v for k, v in os.environ.items()
               if k not in ('GRANDMONT_GROUP_ENV', 'PROMONTA_ENV', 'MINIAPP_DATA_ROOT')}
        env['BOT_TOKEN'] = 'ci-dummy'
        code = (
            'import sys; sys.path.insert(0, "backend"); '
            'import main; import core.paths as p; '
            'print(main.DATA_ROOT); print(p.DATA_ROOT); '
            'assert main.DATA_ROOT == p.DATA_ROOT == "/home/grandmont/agent/miniapp"'
        )
        result = subprocess.run(
            [sys.executable, '-c', code],
            capture_output=True, text=True, env=env,
            cwd=os.path.join(os.path.dirname(__file__), '..'),
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_data_root_contains_pytest_prefix(self):
        """conftest.py uses 'grandmont-group-pytest-' prefix; standalone tests use 'grandmont-group-test-'."""
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
        import main as backend
        self.assertTrue(
            'grandmont-group-pytest-' in backend.DATA_ROOT or 'grandmont-group-test-' in backend.DATA_ROOT,
            f"DATA_ROOT does not have expected prefix: {backend.DATA_ROOT}",
        )

    def test_main_raises_if_test_env_and_prod_path(self):
        """main.py must raise RuntimeError when GRANDMONT_GROUP_ENV=test + DATA_ROOT=prod."""
        env = {
            **os.environ,
            'GRANDMONT_GROUP_ENV': 'test',
            'MINIAPP_DATA_ROOT': '/home/grandmont/agent/miniapp',
            'BOT_TOKEN': 'ci-dummy',
        }
        result = subprocess.run(
            [sys.executable, '-c',
             'import sys; sys.path.insert(0, "backend"); import main'],
            capture_output=True, text=True, env=env,
            cwd=os.path.join(os.path.dirname(__file__), '..'),
        )
        self.assertNotEqual(result.returncode, 0,
                            "Expected RuntimeError but process exited 0")
        self.assertIn('REFUSING TO RUN TESTS AGAINST PRODUCTION DATA ROOT',
                      result.stderr + result.stdout)

    def test_legacy_promonta_env_name_still_arms_guard(self):
        """Legacy PROMONTA_ENV=test still arms the current production-root guard."""
        env = {k: v for k, v in os.environ.items() if k != 'GRANDMONT_GROUP_ENV'}
        env.update({
            'PROMONTA_ENV': 'test',
            'MINIAPP_DATA_ROOT': '/home/grandmont/agent/miniapp',
            'BOT_TOKEN': 'ci-dummy',
        })
        result = subprocess.run(
            [sys.executable, '-c',
             'import sys; sys.path.insert(0, "backend"); import main'],
            capture_output=True, text=True, env=env,
            cwd=os.path.join(os.path.dirname(__file__), '..'),
        )
        self.assertNotEqual(result.returncode, 0,
                            "Expected RuntimeError but process exited 0")
        self.assertIn('REFUSING TO RUN TESTS AGAINST PRODUCTION DATA ROOT',
                      result.stderr + result.stdout)

    def test_main_allows_temp_path_even_with_test_env(self):
        """main.py must NOT raise when GRANDMONT_GROUP_ENV=test + DATA_ROOT=temp dir."""
        tmp = tempfile.mkdtemp(prefix='grandmont-group-guard-test-')
        env = {
            **os.environ,
            'GRANDMONT_GROUP_ENV': 'test',
            'MINIAPP_DATA_ROOT': tmp,
            'BOT_TOKEN': 'ci-dummy',
        }
        result = subprocess.run(
            [sys.executable, '-c',
             'import sys; sys.path.insert(0, "backend"); import main; print("OK")'],
            capture_output=True, text=True, env=env,
            cwd=os.path.join(os.path.dirname(__file__), '..'),
        )
        self.assertEqual(result.returncode, 0,
                         f"Unexpected failure: {result.stderr}")
        self.assertIn('OK', result.stdout)

    def test_main_allows_prod_path_without_test_env(self):
        """Production process must work with the unified default DATA_ROOT."""
        env = {k: v for k, v in os.environ.items()
               if k not in ('GRANDMONT_GROUP_ENV', 'PROMONTA_ENV', 'MINIAPP_DATA_ROOT')}
        env['BOT_TOKEN'] = 'ci-dummy'
        result = subprocess.run(
            [sys.executable, '-c',
             'import sys; sys.path.insert(0, "backend"); import main; print("OK")'],
            capture_output=True, text=True, env=env,
            cwd=os.path.join(os.path.dirname(__file__), '..'),
        )
        self.assertNotIn('REFUSING TO RUN TESTS', result.stderr + result.stdout)
        if result.returncode != 0:
            self.assertNotIn('RuntimeError', result.stderr)
