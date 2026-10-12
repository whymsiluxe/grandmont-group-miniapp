#!/usr/bin/env python3
from pathlib import Path

path = Path('tests/test_daily_plan.py')
text = path.read_text(encoding='utf-8')
old = "        self.assertEqual(plan['status'], 'published')\n        return plan\n"
new = "        self.assertEqual(plan['status'], 'published')\n"
count = text.count(old)
if count != 1:
    raise SystemExit(f'expected one target block, found {count}')
text = text.replace(old, new, 1)
path.write_text(text, encoding='utf-8')
print('unittest warning cleanup applied')
