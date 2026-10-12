#!/usr/bin/env python3
from pathlib import Path

REPLACEMENTS = {
    "backend/routes/workforce_profiles.py": [
        ("from datetime import datetime\n", "from datetime import datetime, timezone\n"),
        ("datetime.utcnow().isoformat() + 'Z'", "datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + 'Z'"),
    ],
    "backend/routes/photo_feed.py": [
        ("from datetime import datetime\n", "from datetime import datetime, timezone\n"),
        ("datetime.utcnow().isoformat()", "datetime.now(timezone.utc).replace(tzinfo=None).isoformat()"),
    ],
    "backend/routes/mangel.py": [
        ("from datetime import datetime\n", "from datetime import datetime, timezone\n"),
        ("datetime.utcnow().strftime(\"%Y-%m-%d %H:%M\")", "datetime.now(timezone.utc).strftime(\"%Y-%m-%d %H:%M\")"),
    ],
    "backend/routes/objects.py": [
        ("body.dict(exclude_unset=True)", "body.model_dump(exclude_unset=True)"),
    ],
}

for filename, replacements in REPLACEMENTS.items():
    path = Path(filename)
    text = path.read_text(encoding="utf-8")
    for old, new in replacements:
        count = text.count(old)
        if count != 1:
            raise SystemExit(f"{filename}: expected exactly one occurrence of {old!r}, found {count}")
        text = text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8")

print("route deprecation cleanup applied")
