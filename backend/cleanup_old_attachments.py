#!/usr/bin/env python3
"""Delete retained attachment files older than ``RETENTION_DAYS``.

Uses the same ``MINIAPP_DATA_ROOT`` contract as the application so the timer
cannot silently clean a retired pre-rebrand serving path.
"""
import os
import sys
import time

RETENTION_DAYS = 90
CUTOFF = time.time() - RETENTION_DAYS * 86400
DATA_ROOT = os.environ.get('MINIAPP_DATA_ROOT', '/home/grandmont/agent/miniapp')

DIRS = [
    os.path.join(DATA_ROOT, 'chat_attachments'),
    os.path.join(DATA_ROOT, 'critical_alert_photos'),
    os.path.join(DATA_ROOT, 'checkin_photos'),
]


def cleanup_dir(path, *, errors=None):
    """Remove expired files while continuing past individual filesystem errors.

    ``errors`` is an optional mutable list used by the CLI entrypoint to make
    partial cleanup failures observable to systemd/monitoring.  Keeping it
    optional preserves the long-standing ``cleanup_dir(path) -> int`` helper
    contract used by tests and maintenance callers.
    """
    if not os.path.isdir(path):
        return 0

    def record_error(error_path, exc):
        if errors is not None:
            errors.append((str(error_path), str(exc)))

    def walk_error(exc):
        record_error(getattr(exc, 'filename', None) or path, exc)

    removed = 0
    for root, _dirs, files in os.walk(path, onerror=walk_error):
        for fname in files:
            fpath = os.path.join(root, fname)
            try:
                if os.path.getmtime(fpath) < CUTOFF:
                    os.remove(fpath)
                    removed += 1
            except OSError as exc:
                record_error(fpath, exc)
                continue
    return removed


def main():
    total = 0
    errors = []
    for directory in DIRS:
        removed = cleanup_dir(directory, errors=errors)
        total += removed
        if removed:
            print(f'{directory}: removed {removed} files older than {RETENTION_DAYS}d')

    if errors:
        for path, message in errors:
            print(f'ERROR: cleanup failed for {path}: {message}', file=sys.stderr)
        print(
            f'ERROR: attachment cleanup incomplete ({len(errors)} filesystem errors)',
            file=sys.stderr,
        )
        return 1

    if not total:
        print('OK: nothing to clean')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
