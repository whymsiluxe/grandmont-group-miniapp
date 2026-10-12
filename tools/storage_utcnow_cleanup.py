#!/usr/bin/env python3
from pathlib import Path

path = Path('backend/core/storage.py')
text = path.read_text(encoding='utf-8')
replacements = [
    ('from datetime import datetime\n', 'from datetime import datetime, timezone\n'),
    ("datetime.utcnow().isoformat() + 'Z'", "datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + 'Z'"),
]
for old, new in replacements:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f'expected one {old!r}, found {count}')
    text = text.replace(old, new, 1)
path.write_text(text, encoding='utf-8')
print('storage utcnow cleanup applied')
