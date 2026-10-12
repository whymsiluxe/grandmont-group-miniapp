from pathlib import Path


def replace_once(path: str, old: str, new: str, label: str) -> None:
    p = Path(path)
    text = p.read_text(encoding='utf-8')
    count = text.count(old)
    if count != 1:
        raise SystemExit(f'{label}: expected one match, found {count}')
    p.write_text(text.replace(old, new, 1), encoding='utf-8')


replace_once('backend/core/storage.py', 'import os\nimport threading\nimport time\n', 'import os\nimport time\n', 'storage threading')
replace_once(
    'backend/core/workforce_shadow.py',
    'from .grandmont_core_client import CoreErrorKind, CoreIntegrationError, get_core_client',
    'from .grandmont_core_client import CoreIntegrationError, get_core_client',
    'workforce relative CoreErrorKind',
)
replace_once(
    'backend/core/workforce_shadow.py',
    'from grandmont_core_client import CoreErrorKind, CoreIntegrationError, get_core_client  # noqa: E402',
    'from grandmont_core_client import CoreIntegrationError, get_core_client  # noqa: E402',
    'workforce fallback CoreErrorKind',
)
replace_once('backend/routes/contracts.py', 'from dataclasses import dataclass\nimport os\nimport threading\n', 'from dataclasses import dataclass\nimport threading\n', 'contracts os')
replace_once('backend/routes/feed_news.py', 'from typing import Any, Callable, Collection, Mapping\n', 'from typing import Any, Callable, Collection\n', 'feed Mapping')
replace_once('backend/routes/objects.py', 'import logging\nimport os\nimport subprocess\n', 'import logging\nimport subprocess\n', 'objects os')
replace_once('backend/routes/daily_plan.py', '        today = deps.business_today_str()\n        store = dpl.get_store_snapshot()\n', '        store = dpl.get_store_snapshot()\n', 'daily plan unused today')

print('Ruff correctness cleanup applied')
