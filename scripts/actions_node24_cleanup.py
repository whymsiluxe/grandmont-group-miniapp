from pathlib import Path

paths = [
    Path('.github/workflows/ci.yml'),
    Path('.github/workflows/high-risk-review-hint.yml'),
]

replacements = {
    'actions/checkout@v4': 'actions/checkout@v7',
    'actions/setup-python@v5': 'actions/setup-python@v7',
}

counts = {old: 0 for old in replacements}
for path in paths:
    text = path.read_text(encoding='utf-8')
    for old, new in replacements.items():
        count = text.count(old)
        counts[old] += count
        text = text.replace(old, new)
    path.write_text(text, encoding='utf-8')

if counts['actions/checkout@v4'] != 2:
    raise SystemExit(f"expected exactly 2 checkout@v4 occurrences, found {counts['actions/checkout@v4']}")
if counts['actions/setup-python@v5'] != 1:
    raise SystemExit(f"expected exactly 1 setup-python@v5 occurrence, found {counts['actions/setup-python@v5']}")

for path in paths:
    text = path.read_text(encoding='utf-8')
    if 'actions/checkout@v4' in text or 'actions/setup-python@v5' in text:
        raise SystemExit(f'deprecated action major remains in {path}')

print('GitHub Actions Node 24-compatible majors applied')
