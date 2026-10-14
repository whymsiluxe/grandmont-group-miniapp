#!/usr/bin/env python3
"""daily_plan_cutoff_check.py — owner alerts around DailyPlan publishing timing.

Owner request (18.09): the plan should arrive the evening BEFORE the work day, not
be assembled the morning of. Neither alert here blocks a worker's shift start --
start without a plan stays freely allowed either way (checkin_start's existing
behavior, unchanged). Two modes, run by two separate systemd timers:

  evening (18:00) — reminder: any worker with an accepted assignment covering
    TOMORROW who has no plan yet for tomorrow. Owner still has the evening to
    publish it.
  morning (06:30) — overdue: any worker with an accepted assignment covering
    TODAY who still has no plan for today. The evening reminder was missed.

Does NOT run inline in FastAPI request handlers -- invoked by systemd timers,
same standalone-script pattern as backend/cleanup_old_attachments.py.

Deploy: like cleanup_old_attachments.py, this file is NOT copied automatically by
scripts/deploy.sh's manifest (it isn't imported by main.py, so it isn't a runtime
dependency of the app) -- copy it manually into the runtime package directory,
next to main.py (/home/grandmont/agent/miniapp/), so `import main as backend`
below resolves the same top-level `main` module the systemd uvicorn service and
every tests/test_*.py file use (as opposed to the `miniapp.main` package import
uvicorn itself uses -- see main.py's own relative/absolute import fallback
comment for why both forms exist side by side in this codebase).

Usage:
  python3 daily_plan_cutoff_check.py evening   # 18:00 timer
  python3 daily_plan_cutoff_check.py morning   # 06:30 timer (default if omitted,
                                                # for backward compat with the
                                                # original single-timer deploy)

Idempotency: writes a per-day, per-mode marker to
$MINIAPP_DATA_ROOT/daily_plan_cutoff_state.json so a timer misfire/retry on the
same day does not spam duplicate alerts. The two modes use separate state keys
so one running twice in a day (e.g. a manual test run) can't suppress the other.
"""
import json
import logging
import os
import sys
from datetime import date, timedelta

logging.basicConfig(
    format='%(asctime)s [daily_plan_cutoff_check] %(levelname)s %(message)s',
    level=logging.INFO,
)
log = logging.getLogger(__name__)

DATA_ROOT = os.environ.get('MINIAPP_DATA_ROOT', '/home/grandmont/agent/miniapp')
STATE_FILE = os.path.join(DATA_ROOT, 'daily_plan_cutoff_state.json')

MODES = {
    # mode: (state_key, alert_kind, date_offset_days, title_verb)
    'morning': ('last_checked_date_morning', 'plan_overdue', 0,
                'не опубликован'),
    'evening': ('last_checked_date_evening', 'plan_publish_reminder', 1,
                'ещё не опубликован на завтра'),
}


def _load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def _find_workers_without_plan(backend, dpl, target_date: str) -> set:
    # 18.09 (audit finding): a legacy/undated assignment (no date_from/date_to --
    # historically meant "no expiry", not "explicitly scheduled on every single
    # day forever") on a non-working day (weekend, holiday_exceptions in
    # work_calendar.json) used to still trigger "plan not published" -- there was
    # never going to BE a plan for that day, nobody asked for one. An assignment
    # with EXPLICIT dates covering target_date is trusted as intentional (the
    # owner scheduled work on that specific day on purpose, weekend or not) and
    # still alerts either way.
    target_date_obj = date.fromisoformat(target_date)
    target_is_working_day = dpl.is_working_day(target_date_obj)

    assignments = backend._load_assignments()
    workers_without_plan = set()
    for object_id, candidates in assignments.items():
        for a in candidates:
            if backend._assignment_status(a) != 'accepted':
                continue
            d_from, d_to = a.get('date_from', ''), a.get('date_to', '')
            has_explicit_dates = bool(d_from and d_to)
            if has_explicit_dates and not (d_from <= target_date <= d_to):
                continue
            if not has_explicit_dates and not target_is_working_day:
                continue
            worker_id = str(a.get('user_id', ''))
            if not worker_id:
                continue
            plan = dpl.get_today_plan_for_worker(worker_id, target_date)
            has_real_plan = plan is not None and plan.get('status') in (
                'published', 'accepted', 'amendment_pending', 'in_progress',
            )
            if not has_real_plan:
                workers_without_plan.add(worker_id)
    return workers_without_plan


def main(mode: str = 'morning') -> int:
    if mode not in MODES:
        log.error("Unknown mode %r, expected one of %s", mode, list(MODES))
        return 1
    state_key, alert_kind, offset_days, title_verb = MODES[mode]

    import main as backend  # noqa: E402
    import daily_plan_lib as dpl  # noqa: E402

    target_date = (backend.business_today() + timedelta(days=offset_days)).strftime('%Y-%m-%d')

    state = _load_state()
    if state.get(state_key) == target_date:
        log.info("[%s] Already checked %s, skipping (idempotent).", mode, target_date)
        return 0

    roles = backend._load_roles()
    owner_id = next((uid for uid, r in roles.items() if r == 'owner'), None)
    if not owner_id:
        log.warning("No owner found in roles, cannot send alert.")
        _save_state({**state, state_key: target_date})
        return 0

    workers_without_plan = _find_workers_without_plan(backend, dpl, target_date)

    if not workers_without_plan:
        log.info("[%s] All assigned workers have a plan for %s. Nothing to alert.", mode, target_date)
        _save_state({**state, state_key: target_date})
        return 0

    profiles = backend._load_worker_profiles()
    names = [
        backend._sanitize_display_name(profiles.get(wid, {}).get('name'), wid)
        for wid in sorted(workers_without_plan)
    ]
    log.info("[%s] Workers without a plan for %s: %s", mode, target_date, names)

    # 22.09 (screenshot audit): ref_id=target_date so _create_critical_alert()'s
    # dedup key (kind, target_user_id, ref_id) treats each business date as its
    # own alert -- without this, EVERY day's plan_overdue/plan_publish_reminder
    # for this owner would dedupe against each other forever after the first one.
    backend._create_critical_alert(
        target_user_id=owner_id,
        kind=alert_kind,
        title=f"План на {target_date} {title_verb} для {len(names)} "
              f"{'работника' if len(names) == 1 else 'работников'}",
        subtitle=', '.join(names)[:200],
        ref_id=target_date,
    )

    _save_state({**state, state_key: target_date})
    return 0


if __name__ == '__main__':
    _mode = sys.argv[1] if len(sys.argv) > 1 else 'morning'
    sys.exit(main(_mode))
