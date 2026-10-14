from pathlib import Path


RUNTIME_FILES = [
    'backend/mangel_lib.py',
    'backend/roadmap_lib.py',
    'backend/daily_plan_cutoff_check.py',
    'backend/cleanup_legacy_critical_alert_duplicates.py',
    'backend/cleanup_old_attachments.py',
]


def test_active_runtime_data_roots_do_not_default_to_retired_promonta_path():
    retired = '/home/promonta/agent/miniapp'
    for filename in RUNTIME_FILES:
        text = Path('../' + filename).read_text(encoding='utf-8')
        assert retired not in text, filename


def test_deploy_contains_exact_requirements_parity_gate():
    text = Path('../scripts/deploy.sh').read_text(encoding='utf-8')
    assert 'production venv exactly matches backend/requirements.txt' in text
    assert "from importlib.metadata import PackageNotFoundError, version" in text
