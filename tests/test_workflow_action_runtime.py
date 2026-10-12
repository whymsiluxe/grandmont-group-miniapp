from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / '.github' / 'workflows'
PERMANENT_WORKFLOWS = (
    WORKFLOWS / 'ci.yml',
    WORKFLOWS / 'high-risk-review-hint.yml',
)


def test_permanent_workflows_use_node24_compatible_official_actions():
    combined = '\n'.join(path.read_text(encoding='utf-8') for path in PERMANENT_WORKFLOWS)

    assert 'actions/checkout@v4' not in combined
    assert 'actions/setup-python@v5' not in combined
    assert combined.count('actions/checkout@v7') == 2
    assert combined.count('actions/setup-python@v7') == 1
