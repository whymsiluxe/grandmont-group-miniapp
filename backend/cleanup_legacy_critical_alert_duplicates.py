#!/usr/bin/env python3
"""One-off migration for legacy duplicate critical-alert records created before
this session's dedup fixes existed (22.09 hotfix, owner live-device finding).

Root cause history (see backend/main.py's _create_critical_alert and
_check_upcoming_birthdays comments): _create_critical_alert() had no
idempotency at all before this session, and _check_upcoming_birthdays()
additionally passed ref_id=uid for BOTH the "3 days before" and "the day of"
birthday alert -- production's critical_alerts.json was found to hold 196
duplicate unacknowledged `kind=birthday, ref_id=<uid>` records, created within
one ~19-hour window, almost certainly from concurrent GET /api/feed/birthdays
calls racing _check_upcoming_birthdays()'s own un-locked read-modify-write.

23.09 (owner review, second pass): the first version of this script grouped
ANY unresolved alert by the generic (kind, target_user_id, ref_id) key. That
is too broad for a one-off migration script -- it could also collapse alerts
that were never part of this bug (a blank ref_id, a non-birthday kind that
happens to share a numeric-looking ref_id, or the NEW idem-shaped birthday
ref_id from the ref_id=idem fix). This script now filters to the exact
legacy shape FIRST -- kind == 'birthday' AND ref_id non-empty AND ref_id is
the bare numeric worker uid (matches backend/main.py's own
_is_legacy_bare_uid_birthday_ref(), which the live ACK-time supersede logic
already uses) -- and only groups records that pass that filter. Anything
else (blank ref_id, non-birthday, new idem-shaped birthday ref_id) is left
completely untouched, regardless of how many records share its key.

Within the (already legacy-filtered) records, the grouping key is
kind + target_user_id + ref_id -- no title/subtitle/date heuristic, per the
owner's original instruction: those are fragile (whitespace/emoji/locale
drift), while the semantic key is not, and it's the one this codebase
already trusts everywhere else for the same purpose.

This means the "3 days before" and "the day of" birthday alerts for ONE
worker DO share one group under this key -- that's a known, accepted
consequence of the legacy data shape (ref_id=uid was the same for both event
types before the ref_id=idem fix). The migration doesn't try to reconstruct
which stale record was which event; it keeps one canonical PENDING alert per
group (the most recent one -- closest to reflecting "the current, real
occurrence" for that worker) and supersedes every other UNACKNOWLEDGED
record in the group.

Non-destructive: nothing is deleted. Every superseded record keeps its
original id, title, timestamps -- it is marked `acknowledged_at` (so it stops
appearing in GET /api/critical-alerts/pending and the frontend popup queue)
and gets a `superseded_by: <canonical_id>` field recording which record
replaced it, for anyone auditing history later. Already-acknowledged records
are never touched regardless of grouping (an owner may have legitimately
acked one occurrence; that's real history, not noise).

Rollout note (cross-process safety, not fixed by this script alone): the
running backend's own writes to this same file go through
update_json_transaction(), which uses an in-process threading.Lock -- it does
NOT coordinate with a separate `python3 cleanup_legacy_...py --apply`
process. Running --apply while the backend service is live could race a
concurrent ack/create and lose one side's write. Do NOT do a larger
cross-process file-locking refactor to fix this generically -- for this
one-off migration, the safe rollout is operational: stop the
grandmont-miniapp service, run --apply, inspect the result, restart the
service. Do not run --apply against a live backend.

Usage:
    python3 cleanup_legacy_critical_alert_duplicates.py [--apply]

Without --apply: dry run, prints the before/after/superseded/groups-affected
report and writes nothing. With --apply: writes a timestamped backup of the
original file next to it, then writes the migrated list atomically.
"""
import argparse
import collections
import json
import os
import shutil
import sys
import time

DATA_ROOT = os.environ.get('MINIAPP_DATA_ROOT', '/home/grandmont/agent/miniapp')
CRITICAL_ALERTS_FILE = os.path.join(DATA_ROOT, 'critical_alerts.json')


def _is_legacy_bare_uid_birthday_ref(alert: dict) -> bool:
    """Mirrors backend/main.py's _is_legacy_bare_uid_birthday_ref() exactly --
    kept as a separate copy (not an import) since this script must remain
    runnable standalone without importing the whole FastAPI app module.
    kind='birthday' with ref_id set to the bare worker uid (digits only),
    from before ref_id=idem (birthday:<uid>:<year>:3days / :today) existed.
    New birthday alerts always have an idem-shaped ref_id (contains ':') and
    must never be touched by this legacy-only logic. Any other kind, a blank
    ref_id, or an already-idem-shaped ref_id is left alone."""
    if alert.get('kind') != 'birthday':
        return False
    ref_id = alert.get('ref_id') or ''
    return bool(ref_id) and ref_id.isdigit()


def _group_key(alert: dict) -> tuple:
    return (alert.get('kind', ''), alert.get('target_user_id', ''), alert.get('ref_id', ''))


def find_legacy_duplicate_groups(alerts: list) -> dict:
    """Returns {group_key: [alert, ...]} for every group of 2+ UNACKNOWLEDGED
    alerts matching the legacy bare-uid birthday shape AND sharing
    (kind, target_user_id, ref_id). Every other record -- blank ref_id,
    non-birthday kind, new idem-shaped birthday ref_id, already acknowledged
    -- is excluded from grouping entirely, regardless of what it shares a
    key with."""
    by_key = collections.defaultdict(list)
    for a in alerts:
        if a.get('acknowledged_at'):
            continue
        if not _is_legacy_bare_uid_birthday_ref(a):
            continue
        by_key[_group_key(a)].append(a)
    return {k: v for k, v in by_key.items() if len(v) > 1}


def migrate(alerts: list) -> tuple[list, dict]:
    """Returns (migrated_list, report_dict). Keeps the MOST RECENT record
    (max created_at) per duplicate group as the canonical pending alert;
    every other unacknowledged record in the group is superseded in place
    (acknowledged_at set, superseded_by recorded) -- never removed.
    Already-acknowledged records, non-duplicated records, and anything not
    matching the legacy bare-uid birthday shape are untouched."""
    dupe_groups = find_legacy_duplicate_groups(alerts)
    now = int(time.time())
    superseded_count = 0
    groups_report = []

    for key, group in dupe_groups.items():
        canonical = max(group, key=lambda a: a.get('created_at', 0))
        for a in group:
            if a['id'] == canonical['id']:
                continue
            a['acknowledged_at'] = now
            a['superseded_by'] = canonical['id']
            a['comment'] = (a.get('comment') or '') + \
                (' ' if a.get('comment') else '') + \
                f'[legacy-duplicate-migration: superseded by {canonical["id"]}]'
            superseded_count += 1
        groups_report.append({
            'key': list(key), 'count': len(group), 'canonical_id': canonical['id'],
        })

    report = {
        'total_records': len(alerts),
        'superseded': superseded_count,
        'groups_affected': len(dupe_groups),
        'groups': groups_report,
    }
    return alerts, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Actually write the migrated file (default: dry run)')
    args = parser.parse_args()

    if not os.path.exists(CRITICAL_ALERTS_FILE):
        print(f'No file at {CRITICAL_ALERTS_FILE}, nothing to do.')
        return 0

    with open(CRITICAL_ALERTS_FILE, encoding='utf-8') as f:
        alerts = json.load(f)

    migrated, report = migrate(alerts)

    print(f"total records:    {report['total_records']}")
    print(f"superseded:       {report['superseded']}")
    print(f"groups affected:  {report['groups_affected']}")
    for g in report['groups']:
        kind, uid, ref_id = g['key']
        print(f"  - kind={kind!r} target_user_id={uid!r} ref_id={ref_id!r}: "
              f"{g['count']} records, canonical (kept pending) id={g['canonical_id']}")

    if not args.apply:
        print('\nDry run -- no changes written. Re-run with --apply to write.')
        return 0

    if report['superseded'] == 0:
        print('\nNothing to migrate -- not writing.')
        return 0

    backup_path = f'{CRITICAL_ALERTS_FILE}.backup-{int(time.time())}'
    shutil.copy2(CRITICAL_ALERTS_FILE, backup_path)
    print(f'\nBackup written: {backup_path}')

    tmp_path = f'{CRITICAL_ALERTS_FILE}.tmp-{os.getpid()}'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(migrated, f, ensure_ascii=False)
    os.replace(tmp_path, CRITICAL_ALERTS_FILE)
    print(f'Wrote migrated file: {CRITICAL_ALERTS_FILE}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
