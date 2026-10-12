#!/usr/bin/env python3
"""Add dynamic legacy compatibility callbacks to absence wiring."""
from pathlib import Path

path = Path("backend/main.py")
source = path.read_text(encoding="utf-8")
needle = '''        resolve_current_display_name=lambda user_id, fallback: _resolve_current_display_name(
            user_id, fallback
        ),
'''
replacement = needle + '''        compat_load_abwesenheit=lambda: _load_abwesenheit(),
        compat_auto_close=lambda: _auto_close_expired_open_ended_abwesenheit(),
'''
if needle not in source:
    raise SystemExit("absence wiring marker not found")
if "compat_load_abwesenheit=lambda" in source:
    raise SystemExit("absence compatibility wiring already present")
path.write_text(source.replace(needle, replacement, 1), encoding="utf-8")
