#!/usr/bin/env python3
"""Delete retained attachment files older than ``RETENTION_DAYS``.

Uses the same ``MINIAPP_DATA_ROOT`` contract as the application so the timer
cannot silently clean a retired pre-rebrand serving path.
"""
import os
import time

RETENTION_DAYS = 90
CUTOFF = time.time() - RETENTION_DAYS * 86400
DATA_ROOT = os.environ.get('MINIAPP_DATA_ROOT', '/home/grandmont/agent/miniapp')

DIRS = [
    os.path.join(DATA_ROOT, 'chat_attachments'),
    os.path.join(DATA_ROOT, 'critical_alert_photos'),
    os.path.join(DATA_ROOT, 'checkin_photos'),
]


def cleanup_dir(path):
    if not os.path.isdir(path):
        return 0
    removed = 0
    for root, _dirs, files in os.walk(path):
        for fname in files:
            fpath = os.path.join(root, fname)
            try:
                if os.path.getmtime(fpath) < CUTOFF:
                    os.remove(fpath)
                    removed += 1
            except OSError:
                continue
    return removed


def main():
    total = 0
    for directory in DIRS:
        removed = cleanup_dir(directory)
        total += removed
        if removed:
            print(f'{directory}: removed {removed} files older than {RETENTION_DAYS}d')
    if not total:
        print('OK: nothing to clean')


if __name__ == '__main__':
    main()
