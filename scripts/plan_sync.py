#!/usr/bin/env python3
"""plan_sync.py — Sheets → DailyPlan local-store sync worker.

Reads Plan_этапов and Plan_дня tabs from Google Sheets every 60 seconds,
computes stable content hashes, diffs against plan_sync_state.json, and
creates/updates DailyPlan records in daily_plan_store.json.

Does NOT write back to Sheets (the store is append-only from the sync direction).
Does NOT run inline in FastAPI request handlers — invoked by a systemd timer.

Deploy:
  1. Copy to /home/grandmont/agent/miniapp/ (or keep in repo + symlink)
  2. Create promonta-plan-sync.service + promonta-plan-sync.timer (see docs/DEPLOYMENT.md)
  3. sudo systemctl enable --now promonta-plan-sync.timer

Environment (from /etc/claude-agent.env):
  MINIAPP_DATA_ROOT — data root (default /home/grandmont/agent/miniapp)
  BOT_TOKEN — required for main.py import, not used by this script directly

Sheets IDs are read from OBJEKTE_SHEET_ID env (fallback: same as objekte_lib.SHEET_ID).
The 5 new tabs (Plan_этапов, Plan_дня, Нормы_работ, Факт_дня, Производительность)
are listed in REQUIRED_TABS; the worker creates them with stub headers if missing
(only after the owner has confirmed it's safe — see OPEN_QUESTIONS.md #Q4).
"""
import hashlib
import json
import logging
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

# ── Environment setup ────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [plan_sync] %(levelname)s %(message)s',
)
log = logging.getLogger('plan_sync')

DATA_ROOT = os.environ.get('MINIAPP_DATA_ROOT', '/home/grandmont/agent/miniapp')
SHEETS_CRED = '/home/grandmont/agent/.sheets.json'
SYNC_STATE_FILE = os.path.join(DATA_ROOT, 'plan_sync_state.json')
DAILY_PLAN_STORE_FILE = os.path.join(DATA_ROOT, 'daily_plan_store.json')

# ── Import daily_plan_lib for shared store access (cross-process safe) ────────
# Avoids duplicating read-modify-write logic — both FastAPI and plan_sync use the
# same lock-protected primitives. daily_plan_lib does NOT import main.py.
_BACKEND_DIR = os.path.join(os.path.dirname(__file__), '..', 'backend')
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

try:
    import daily_plan_lib as dpl
    _WORK_CALENDAR_FILE = os.path.join(DATA_ROOT, 'work_calendar.json')
    dpl.configure(DAILY_PLAN_STORE_FILE, SYNC_STATE_FILE, _WORK_CALENDAR_FILE)
    _PLAN_LIB_AVAILABLE = True
except ImportError as e:
    log.warning("daily_plan_lib not available — plan_sync will only cache row hashes: %s", e)
    _PLAN_LIB_AVAILABLE = False

# Google Sheet ID (same spreadsheet as objekte_lib)
SHEET_ID = os.environ.get(
    'OBJEKTE_SHEET_ID',
    '14CXpSaW9ErmViK09zAh09X52EUmEJjnkxGUSmW3Z9sA',
)

# New Sheets tabs — NOT auto-created without owner approval (see OPEN_QUESTIONS.md #Q4)
PLAN_TABS = {
    'план_этапов': 'План_этапов',
    'план_дня': 'План_дня',
    'нормы_работ': 'Нормы_работ',
}

SYNC_INTERVAL_S = int(os.environ.get('PLAN_SYNC_INTERVAL', '60'))
MAX_BACKOFF_S = 300
QUOTA_PAUSE_S = 600

# ── OAuth token ──────────────────────────────────────────────────────────────
_token_cache: dict = {'token': None, 'expires_at': 0}


def _get_token() -> str:
    if _token_cache['token'] and time.time() < _token_cache['expires_at'] - 60:
        return _token_cache['token']
    try:
        c = json.load(open(SHEETS_CRED))
    except FileNotFoundError:
        raise RuntimeError(f"Sheets credential not found: {SHEETS_CRED}")
    payload = urllib.parse.urlencode({
        'client_id': c['client_id'],
        'client_secret': c['client_secret'],
        'refresh_token': c['refresh_token'],
        'grant_type': 'refresh_token',
    }).encode()
    resp = json.load(urllib.request.urlopen(
        'https://oauth2.googleapis.com/token', payload, timeout=20))
    _token_cache['token'] = resp['access_token']
    _token_cache['expires_at'] = time.time() + resp.get('expires_in', 3600)
    return _token_cache['token']


def _sheets_request(url: str) -> dict:
    token = _get_token()
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


# ── Sheets read ───────────────────────────────────────────────────────────────

def _read_tab(tab_name: str) -> list[dict] | None:
    """Reads a Sheets tab, returns list of row dicts keyed by header. None if tab missing."""
    encoded = urllib.parse.quote(tab_name)
    url = (
        f'https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}'
        f'/values/{encoded}?majorDimension=ROWS'
    )
    try:
        data = _sheets_request(url)
    except urllib.error.HTTPError as e:
        if e.code == 400:
            log.warning("Tab %r not found in spreadsheet (HTTP 400)", tab_name)
            return None
        if e.code == 429:
            log.warning("Sheets quota exceeded (429) — pausing %ds", QUOTA_PAUSE_S)
            time.sleep(QUOTA_PAUSE_S)
            return None
        raise

    rows = data.get('values', [])
    if not rows:
        return []
    header = rows[0]
    return [dict(zip(header, r + [''] * max(0, len(header) - len(r)))) for r in rows[1:]]


# ── Content hash ─────────────────────────────────────────────────────────────

def _hash_rows(rows: list[dict]) -> str:
    canonical = json.dumps(rows, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


# ── Sync state ────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if not os.path.exists(SYNC_STATE_FILE):
        return {}
    try:
        with open(SYNC_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    tmp = SYNC_STATE_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SYNC_STATE_FILE)


# ── Plan_этапов processing ────────────────────────────────────────────────────

def _process_stage_rows(rows: list[dict], state: dict) -> int:
    """Syncs stage-level plan rows to plan_sync_state for later use by DailyPlan builder."""
    new_hash = _hash_rows(rows)
    old_hash = state.get('plan_etapov_hash')
    if new_hash == old_hash:
        return 0

    state['plan_etapov_hash'] = new_hash
    state['plan_etapov_rows'] = rows
    state['plan_etapov_synced_at'] = time.time()
    log.info("Plan_этапов updated: %d rows", len(rows))
    return len(rows)


def _raw_row_plan_id(row: dict) -> str:
    """Extracts plan_id from a raw Sheet row without full validation (item 3+6
    interaction fix). Used so reconciliation can distinguish a row that's still
    present-but-invalid from a row that's truly gone from the Sheet."""
    for k in ('plan_id', 'PLAN_ID', 'id'):
        v = row.get(k) or row.get(k.lower()) or row.get(k.upper()) or ''
        if v:
            return str(v).strip()
    return ''


def _row_to_plan_fields(row: dict) -> dict | None:
    """Parse a Plan_дня Sheet row into DailyPlan fields.

    Expected Plan_дня columns (case-insensitive, missing = graceful skip):
      plan_id   — stable UUID; if missing, synthesized from object_id+date+stage_key
      date      — YYYY-MM-DD
      object_id — ID объекта
      stage_key — stage identifier
      worker_ids — comma-separated Telegram user IDs (optional, defaults to [])
      status    — 'draft' or 'published' (default: 'draft')
      items_json — JSON-encoded list of plan items
    """
    def _get(*keys):
        for k in keys:
            v = row.get(k) or row.get(k.lower()) or row.get(k.upper()) or ''
            if v:
                return str(v).strip()
        return ''

    date_str = _get('date', 'Дата', 'DATE')
    object_id = _get('object_id', 'Объект', 'ID объекта', 'OBJECT_ID')
    stage_key = _get('stage_key', 'stage', 'Этап', 'STAGE_KEY') or 'default'
    if not date_str or not object_id:
        return None  # required fields missing

    plan_id = _get('plan_id', 'PLAN_ID', 'id')
    if not plan_id:
        log.error("Row object_id=%r date=%r stage_key=%r: missing/blank plan_id — "
                   "skipping row (no synthetic id, an explicit plan_id column value is required)",
                   object_id, date_str, stage_key)
        return None

    worker_ids_raw = _get('worker_ids', 'workers', 'Работники', 'WORKER_IDS')
    worker_ids = [w.strip() for w in worker_ids_raw.split(',') if w.strip()] if worker_ids_raw else []

    status_raw = _get('status', 'Status', 'Статус').lower()
    status = 'published' if status_raw in ('published', 'опубликован', '1', 'yes', 'true') else 'draft'

    items_raw = _get('items_json', 'items', 'Пункты', 'ITEMS_JSON')
    try:
        items = json.loads(items_raw) if items_raw else []
    except Exception as e:
        log.error("Row plan_id=%r: items_json is not valid JSON (%s) — skipping row, "
                   "keeping existing plan untouched", _get('plan_id', 'PLAN_ID', 'id'), e)
        return None
    if not isinstance(items, list):
        log.error("Row plan_id=%r: items_json must be a JSON list, got %s — skipping row, "
                   "keeping existing plan untouched", _get('plan_id', 'PLAN_ID', 'id'), type(items).__name__)
        return None

    return {
        "plan_id": plan_id,
        "date": date_str,
        "object_id": object_id,
        "stage_key": stage_key,
        "worker_ids": worker_ids,
        "status": status,
        "items": items,
    }


def _process_daily_plan_rows(rows: list[dict], state: dict) -> int:
    """Syncs Plan_дня rows — creates/updates/publishes DailyPlan records in the local store.

    Uses daily_plan_lib.create_plan / update_plan_items / publish_plan so that:
    - Cross-process locking (fcntl) is handled by daily_plan_lib
    - Sheet row → DailyPlan creation is idempotent (keyed on plan_id)
    - Pre-acceptance edits bump the version; post-acceptance edits create an Amendment
    - Sheet row deletion after acceptance: accepted snapshot survives unchanged

    Plan_дня schema: see _row_to_plan_fields() above.
    """
    new_hash = _hash_rows(rows)
    old_hash = state.get('plan_dnya_hash')
    if new_hash == old_hash:
        return 0

    state['plan_dnya_hash'] = new_hash
    state['plan_dnya_rows'] = rows
    state['plan_dnya_synced_at'] = time.time()
    log.info("Plan_дня updated: %d rows", len(rows))

    if not _PLAN_LIB_AVAILABLE:
        log.warning("daily_plan_lib unavailable — row hashes cached but plans not created")
        return len(rows)

    changed = 0
    seen_plan_ids = set()

    for row in rows:
        # Item 3+6 interaction fix: a row that's present in the Sheet but fails
        # validation (bad items_json, missing plan_id, etc.) must still count as
        # "present" for reconciliation purposes -- otherwise reconciliation later
        # sees an empty slot for that plan_id and wrongly treats the row as
        # deleted, cancelling/source-deleting an otherwise-untouched valid plan.
        raw_pid = _raw_row_plan_id(row)
        if raw_pid:
            seen_plan_ids.add(raw_pid)

        fields = _row_to_plan_fields(row)
        if not fields:
            # Row present but invalid: sync error already logged inside
            # _row_to_plan_fields. Existing plan (if any) stays untouched --
            # raw_pid is already in seen_plan_ids above, so reconciliation
            # will not mark it cancelled/source_deleted.
            continue

        pid = fields["plan_id"]
        seen_plan_ids.add(pid)

        existing = dpl.get_plan_by_sheets_source_row(pid)
        if existing is None:
            # New plan: create from Sheet row, then publish if status=published
            try:
                plan = dpl.create_plan(
                    object_id=fields["object_id"],
                    stage_key=fields["stage_key"],
                    date_str=fields["date"],
                    assigned_worker_ids=fields["worker_ids"],
                    items=fields["items"],
                    created_by="plan_sync",
                    sheets_source_row=pid,
                )
                if fields["status"] == "published":
                    dpl.publish_plan(plan["id"], "plan_sync")
                log.info("Created DailyPlan %s for %s on %s", plan["id"], fields["object_id"], fields["date"])
                changed += 1
            except Exception as e:
                log.error("Failed to create DailyPlan for row %s: %s", pid, e)
        else:
            # Existing plan: detect non-items field changes (item 5) before items
            try:
                dpl.update_plan_fields(
                    plan_id=existing["id"],
                    worker_ids=fields["worker_ids"],
                    date_str=fields["date"],
                    object_id=fields["object_id"],
                    stage_key=fields["stage_key"],
                    updated_by="plan_sync",
                )
            except Exception as e:
                log.error("Failed to update fields for DailyPlan %s: %s", existing["id"], e)

            # Existing plan: update items if content changed; publish if newly marked published
            new_items_hash = dpl._items_hash(fields["items"])
            if new_items_hash != existing.get("content_hash"):
                try:
                    dpl.update_plan_items(
                        plan_id=existing["id"],
                        new_items=fields["items"],
                        change_type="sheets_edit",
                        change_summary="Обновлено из Plan_дня (план_синк)",
                        updated_by="plan_sync",
                    )
                    log.info("Updated DailyPlan %s (items changed)", existing["id"])
                    changed += 1
                except Exception as e:
                    log.error("Failed to update DailyPlan %s: %s", existing["id"], e)

            if fields["status"] == "published" and existing.get("status") == "draft":
                try:
                    dpl.publish_plan(existing["id"], "plan_sync")
                    log.info("Published DailyPlan %s", existing["id"])
                    changed += 1
                except Exception as e:
                    log.warning("Could not publish DailyPlan %s (status=%s): %s",
                                existing["id"], existing.get("status"), e)

    # Item 6: reconciliation — a sync-managed plan whose Sheet row disappeared
    # this run must not be left dangling as an active candidate. Only plans this
    # sync run has previously created/updated (sheets_source_row set) are in scope;
    # manually-created plans (no sheets_source_row) are never touched here.
    store_snapshot = dpl.get_store_snapshot()
    for plan in store_snapshot["daily_plans"].values():
        src_row = plan.get("sheets_source_row")
        if not src_row or src_row in seen_plan_ids:
            continue
        if plan["status"] in ("cancelled",) or plan.get("source_deleted"):
            continue
        try:
            dpl.mark_plan_source_deleted(plan["id"])
            log.info("DailyPlan %s: Sheet row %r disappeared — marked %s",
                      plan["id"], src_row,
                      "source_deleted" if plan.get("status") != "draft" else "cancelled")
            changed += 1
        except Exception as e:
            log.error("Failed to reconcile removed row for DailyPlan %s: %s", plan["id"], e)

    return changed


# ── Main loop ─────────────────────────────────────────────────────────────────

def run_once() -> bool:
    """Runs one sync cycle. Returns True on success, False on retriable error."""
    state = _load_state()
    changed_total = 0

    try:
        for tab_key, tab_name in PLAN_TABS.items():
            rows = _read_tab(tab_name)
            if rows is None:
                log.info("Tab %r not found — skipping (create it manually when ready)", tab_name)
                continue

            if tab_key == 'план_этапов':
                changed_total += _process_stage_rows(rows, state)
            elif tab_key == 'план_дня':
                changed_total += _process_daily_plan_rows(rows, state)
            # нормы_работ: read-only cache only (no local plan-store writes for norms)
            elif tab_key == 'нормы_работ':
                nh = _hash_rows(rows)
                if nh != state.get('normy_hash'):
                    state['normy_hash'] = nh
                    state['normy_rows'] = rows
                    state['normy_synced_at'] = time.time()
                    log.info("Нормы_работ updated: %d rows", len(rows))
                    changed_total += len(rows)

    except urllib.error.URLError as e:
        log.warning("Sheets network error: %s", e)
        return False
    except Exception as e:
        log.error("Unexpected error in sync cycle: %s", e, exc_info=True)
        return False

    state['last_sync_at'] = time.time()
    state['last_sync_ok'] = True
    _save_state(state)

    if changed_total:
        log.info("Sync cycle complete — %d rows changed", changed_total)
    return True


def main():
    log.info("plan_sync starting — interval=%ds, sheet=%s", SYNC_INTERVAL_S, SHEET_ID)
    consecutive_failures = 0
    backoff = 5

    while True:
        success = run_once()
        if success:
            consecutive_failures = 0
            backoff = 5
        else:
            consecutive_failures += 1
            backoff = min(backoff * 2, MAX_BACKOFF_S)
            log.warning("Sync failed (attempt %d) — retry in %ds", consecutive_failures, backoff)

        if success:
            time.sleep(SYNC_INTERVAL_S)
        else:
            time.sleep(backoff)


if __name__ == '__main__':
    main()