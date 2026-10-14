from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / 'scripts' / 'assert_single_worker.sh'
RUNTIME_MANIFEST = ROOT / 'scripts' / 'runtime_manifest.sh'


def _run(exec_start: str, environment: str = '') -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ['bash', str(GUARD), exec_start, environment],
        text=True,
        capture_output=True,
        check=False,
    )


def test_default_uvicorn_configuration_is_single_worker():
    result = _run('/venv/bin/uvicorn miniapp.main:app --host 127.0.0.1 --port 8001')
    assert result.returncode == 0, result.stderr
    assert 'single-worker invariant' in result.stdout


def test_explicit_one_worker_is_allowed():
    result = _run('/venv/bin/uvicorn miniapp.main:app --workers 1', 'WEB_CONCURRENCY=1')
    assert result.returncode == 0, result.stderr


def test_multiple_cli_workers_are_rejected():
    for exec_start in (
        '/venv/bin/uvicorn miniapp.main:app --workers 2',
        '/venv/bin/uvicorn miniapp.main:app --workers=4',
    ):
        result = _run(exec_start)
        assert result.returncode != 0
        assert 'ровно один backend worker' in result.stderr


def test_web_concurrency_multiple_workers_is_rejected():
    result = _run(
        '/venv/bin/uvicorn miniapp.main:app',
        'BOT_TOKEN=dummy WEB_CONCURRENCY=3 MINIAPP_DATA_ROOT=/tmp/data',
    )
    assert result.returncode != 0
    assert 'WEB_CONCURRENCY=3' in result.stderr


def test_reload_is_rejected_in_production():
    result = _run('/venv/bin/uvicorn miniapp.main:app --reload')
    assert result.returncode != 0
    assert '--reload' in result.stderr


def test_runtime_manifest_enforces_guard_when_sourced_by_deploy():
    source = RUNTIME_MANIFEST.read_text(encoding='utf-8')
    assert 'BASH_SOURCE[1]' in source
    assert '/deploy.sh' in source
    assert 'assert_single_worker.sh' in source
    assert 'systemctl show' in source
