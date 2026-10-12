"""Owner workforce dashboard and assignment-confirmation presentation logic."""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from fastapi import APIRouter, Depends


@dataclass(frozen=True)
class OwnerDashboardDeps:
    get_current_user: Callable[..., dict]
    require_owner: Callable[..., None]
    business_today_str: Callable[[], str]
    business_today: Callable[[], Any]
    load_checkin_meta: Callable[[], list]
    load_worker_profiles: Callable[[], dict]
    sanitize_display_name: Callable[[Any, str], str]
    cached_get_used_range: Callable[[str], list]
    is_active_photo_checkin_session: Callable[[dict], bool]
    hours_from_session: Callable[[dict], float]
    load_assignments: Callable[[], dict]
    load_abwesenheit: Callable[[], list]
    load_roles: Callable[[], dict]
    load_repo_objekte_lib: Callable[[], Any]
    load_roadmap_store: Callable[[], dict]
    skill_display_name: Callable[[str], str]
    confirm_warning_seconds: int = 2 * 60 * 60
    confirm_danger_seconds: int = 4 * 60 * 60


@dataclass(frozen=True)
class OwnerDashboardHandlers:
    assignment_status: Callable[[dict], str]
    parse_assignment_timestamp: Callable[[Any], int | None]
    assignment_pending_escalation: Callable[..., dict]
    get_dashboard_shifts_today: Callable[..., dict]
    get_active_blockers: Callable[..., dict]
    get_team_plan: Callable[..., dict]
    format_assignment_wait: Callable[[int], str]
    assignment_confirmation_alerts: Callable[..., list]


def create_owner_dashboard_router(
    deps: OwnerDashboardDeps,
) -> tuple[APIRouter, OwnerDashboardHandlers]:
    router = APIRouter()

    def assignment_status(assignment: dict) -> str:
        return assignment.get('status') or 'accepted'

    def parse_assignment_timestamp(value) -> int | None:
        if value in (None, ''):
            return None
        if isinstance(value, (int, float)):
            return int(value)
        raw = str(value).strip()
        if not raw:
            return None
        try:
            if raw.endswith('Z'):
                raw = raw[:-1] + '+00:00'
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())

    def assignment_pending_escalation(assignment: dict, now_ts: int | None = None) -> dict:
        if assignment_status(assignment) != 'pending':
            return {'level': '', 'type': '', 'age_seconds': 0}
        now_ts = int(time.time()) if now_ts is None else int(now_ts)
        start_ts = (
            parse_assignment_timestamp(assignment.get('pending_since'))
            or parse_assignment_timestamp(assignment.get('updated_at'))
            or parse_assignment_timestamp(assignment.get('assigned_at'))
        )
        if not start_ts:
            return {'level': '', 'type': '', 'age_seconds': 0}
        age = max(0, now_ts - start_ts)
        if age >= deps.confirm_danger_seconds:
            return {'level': 'danger', 'type': 'red', 'age_seconds': age}
        if age >= deps.confirm_warning_seconds:
            return {'level': 'warning', 'type': 'yellow', 'age_seconds': age}
        return {'level': '', 'type': '', 'age_seconds': age}

    def _object_names() -> dict:
        rows = deps.cached_get_used_range('Объекты')
        names = {}
        if rows:
            header = rows[0]
            for row_data in rows[1:]:
                row = dict(zip(header, row_data))
                names[str(row.get('ID объекта', ''))] = row.get('Объект', '')
        return names

    @router.get('/api/dashboard/shifts-today')
    def get_dashboard_shifts_today(
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(deps.require_owner),
    ):
        today = deps.business_today_str()
        sessions = deps.load_checkin_meta()
        today_sessions = [session for session in sessions if session.get('date') == today]
        profiles = deps.load_worker_profiles()
        object_names = _object_names()

        def worker_name(uid):
            return deps.sanitize_display_name(profiles.get(str(uid), {}).get('name'), str(uid))

        def worker_specialty(uid):
            skills = profiles.get(str(uid), {}).get('skills') or []
            return skills[0] if skills else ''

        working_now = []
        finished_today = []
        for session in today_sessions:
            entry = {
                'user_id': str(session['user_id']),
                'worker_name': worker_name(session['user_id']),
                'specialty': worker_specialty(session['user_id']),
                'object_id': session['object_id'],
                'object_name': object_names.get(session['object_id'], session['object_id']),
                'start_at': session.get('start_at'),
            }
            if deps.is_active_photo_checkin_session(session):
                working_now.append(entry)
            else:
                entry['finish_at'] = session.get('finish_at')
                finished_today.append(entry)

        working_uids = {entry['user_id'] for entry in working_now}
        finished_uids = {entry['user_id'] for entry in finished_today}
        by_uid = {}
        for object_id, assignments in deps.load_assignments().items():
            for assignment in assignments:
                uid = str(assignment.get('user_id', ''))
                date_from, date_to = assignment.get('date_from', ''), assignment.get('date_to', '')
                if not uid or not (date_from and date_to and date_from <= today <= date_to):
                    continue
                by_uid.setdefault(uid, []).append((object_id, assignment))

        now_ts = int(time.time())

        def make_entry(uid, object_id, assignment):
            escalation = assignment_pending_escalation(assignment, now_ts)
            return {
                'user_id': uid,
                'worker_name': worker_name(uid),
                'specialty': worker_specialty(uid),
                'object_id': object_id,
                'object_name': object_names.get(object_id, object_id),
                'stage_id': assignment.get('stage_id', ''),
                'date_from': assignment.get('date_from', ''),
                'date_to': assignment.get('date_to', ''),
                'task_note': assignment.get('task_note', ''),
                'assignment_status': assignment_status(assignment),
                'assignment_id': assignment.get('id', ''),
                'assigned_at': assignment.get('assigned_at', ''),
                'pending_since': (
                    assignment.get('pending_since')
                    or assignment.get('updated_at')
                    or assignment.get('assigned_at', '')
                ),
                'response_wait_seconds': escalation['age_seconds'],
                'response_escalation': escalation['level'],
                'response_alert_type': escalation['type'],
            }

        not_started = []
        awaiting_response = []
        for uid, pairs in by_uid.items():
            if uid in working_uids or uid in finished_uids:
                continue
            accepted_pair = next(
                (pair for pair in pairs if assignment_status(pair[1]) == 'accepted'), None
            )
            if accepted_pair:
                not_started.append(make_entry(uid, *accepted_pair))
                continue
            pending_pair = next(
                (pair for pair in pairs if assignment_status(pair[1]) == 'pending'), None
            )
            if pending_pair:
                awaiting_response.append(make_entry(uid, *pending_pair))

        busy_uids = (
            working_uids
            | finished_uids
            | {entry['user_id'] for entry in not_started}
            | {entry['user_id'] for entry in awaiting_response}
        )
        absent_uids = {
            str(entry.get('user_id'))
            for entry in deps.load_abwesenheit()
            if entry.get('status') == 'approved'
            and entry.get('date_from', '') <= today <= entry.get('date_to', '')
        }
        roles = deps.load_roles()
        available_today = [
            {
                'user_id': uid,
                'worker_name': worker_name(uid),
                'specialty': worker_specialty(uid),
            }
            for uid in profiles
            if roles.get(uid, 'worker') != 'owner'
            and uid not in busy_uids
            and uid not in absent_uids
        ]

        for entry in working_now:
            session = next(
                (
                    current for current in today_sessions
                    if str(current.get('user_id')) == entry['user_id']
                    and deps.is_active_photo_checkin_session(current)
                ),
                None,
            )
            entry['stage_name'] = (session or {}).get('stage_name') or ''

        hours_today_total = 0.0
        for session in today_sessions:
            if session.get('finish_at') is not None or session.get('manual_entry'):
                hours_today_total += deps.hours_from_session(session)
            elif session.get('start_at'):
                elapsed = (
                    time.time() - session['start_at'] - (session.get('pause_accumulated_seconds') or 0)
                ) / 3600.0
                hours_today_total += max(0.0, elapsed)

        sparkline_days = []
        today_date = deps.business_today()
        for index in range(6, -1, -1):
            day = (today_date - timedelta(days=index)).isoformat()
            day_sessions = [session for session in sessions if session.get('date') == day]
            day_hours = 0.0
            for session in day_sessions:
                if session.get('finish_at') is not None or session.get('manual_entry'):
                    day_hours += deps.hours_from_session(session)
                elif day == today and session.get('start_at'):
                    elapsed = (
                        time.time() - session['start_at'] - (session.get('pause_accumulated_seconds') or 0)
                    ) / 3600.0
                    day_hours += max(0.0, elapsed)
            sparkline_days.append({
                'date': day,
                'hours': round(day_hours, 1),
                'shifts': len(day_sessions),
                'finished': sum(
                    1 for session in day_sessions
                    if session.get('finish_at') is not None or session.get('manual_entry')
                ),
            })

        return {
            'date': today,
            'working_now': working_now,
            'hours_today_total': round(hours_today_total, 1),
            'sparkline': {'days': sparkline_days},
            'not_started': not_started,
            'finished_today': finished_today,
            'awaiting_response': awaiting_response,
            'available_today': available_today,
        }

    @router.get('/api/dashboard/active-blockers')
    def get_active_blockers(
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(deps.require_owner),
    ):
        objects_lib = deps.load_repo_objekte_lib()
        store = deps.load_roadmap_store()
        stage_blocks = store.get('stage_blocks', {})
        if not stage_blocks:
            return {'blockers': []}
        object_names = _object_names()
        profiles = deps.load_worker_profiles()
        grouped = objects_lib.all_stages_grouped()
        result = []
        for object_id, stages in grouped.items():
            for stage in stages:
                stage_key = stage.get('ID строки этапа')
                meta = stage_blocks.get(stage_key)
                if not meta:
                    continue
                blocked_by = str(meta.get('blocked_by', ''))
                result.append({
                    'object_id': object_id,
                    'object_name': object_names.get(object_id, object_id),
                    'stage_name': stage.get('Название этапа', ''),
                    'row_num': stage.get('_row'),
                    'reason': meta.get('quick_reason') or meta.get('comment') or '',
                    'reported_by_name': deps.sanitize_display_name(
                        profiles.get(blocked_by, {}).get('name'), blocked_by
                    ),
                    'blocked_at': meta.get('blocked_at'),
                })
        result.sort(key=lambda blocker: blocker.get('blocked_at') or 0, reverse=True)
        return {'blockers': result}

    @router.get('/api/dashboard/team-plan')
    def get_team_plan(
        date: str = '',
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(deps.require_owner),
    ):
        target_date = date or deps.business_today_str()
        object_names = _object_names()
        profiles = deps.load_worker_profiles()

        def worker_name(uid):
            return deps.sanitize_display_name(profiles.get(str(uid), {}).get('name'), str(uid))

        objects_lib = deps.load_repo_objekte_lib()
        stage_names_by_object = {}
        for object_id, stages in objects_lib.all_stages_grouped().items():
            stage_names_by_object[object_id] = {
                stage.get('Название этапа', '').strip().lower(): stage.get('Название этапа', '')
                for stage in stages
            }

        def resolve_stage_name(object_id, stage_id):
            names = stage_names_by_object.get(object_id, {})
            return names.get((stage_id or '').strip().lower(), '')

        is_today = target_date == deps.business_today_str()
        sessions_by_uid = {}
        if is_today:
            for session in deps.load_checkin_meta():
                if session.get('date') == target_date:
                    sessions_by_uid.setdefault(str(session.get('user_id')), []).append(session)

        by_object = {}
        for object_id, assignments in deps.load_assignments().items():
            for assignment in assignments:
                date_from, date_to = assignment.get('date_from', ''), assignment.get('date_to', '')
                if not (date_from and date_to and date_from <= target_date <= date_to):
                    continue
                status = assignment_status(assignment)
                uid = str(assignment.get('user_id', ''))
                stage_id = assignment.get('stage_id', '')
                entry = {
                    'user_id': uid,
                    'worker_name': worker_name(uid),
                    'stage_id': stage_id,
                    'stage_name': resolve_stage_name(object_id, stage_id),
                    'task_note': assignment.get('task_note', ''),
                    'date_from': date_from,
                    'date_to': date_to,
                    'assignment_status': status,
                }
                if is_today and status == 'accepted':
                    uid_sessions = sessions_by_uid.get(uid, [])
                    active = next(
                        (
                            session for session in uid_sessions
                            if session.get('object_id') == object_id
                            and deps.is_active_photo_checkin_session(session)
                        ),
                        None,
                    )
                    finished = next(
                        (
                            session for session in uid_sessions
                            if session.get('object_id') == object_id
                            and session.get('finish_at') is not None
                        ),
                        None,
                    )
                    if active:
                        entry['shift_state'] = 'active'
                    elif finished:
                        entry['shift_state'] = 'finished'
                    else:
                        entry['shift_state'] = 'not_started'
                by_object.setdefault(object_id, {
                    'object_id': object_id,
                    'object_name': object_names.get(object_id, object_id),
                    'assignments': [],
                })
                by_object[object_id]['assignments'].append(entry)

        objects_plan = sorted(
            by_object.values(), key=lambda item: item['object_name'] or item['object_id']
        )
        return {'date': target_date, 'objects': objects_plan}

    def format_assignment_wait(age_seconds: int) -> str:
        minutes = max(0, int(age_seconds) // 60)
        hours, mins = divmod(minutes, 60)
        return f'{hours} ч {mins} мин' if hours else f'{mins} мин'

    def assignment_confirmation_alerts(now_ts: int | None = None) -> list:
        now_ts = int(time.time()) if now_ts is None else int(now_ts)
        profiles = deps.load_worker_profiles()
        object_names = {}
        try:
            rows = deps.cached_get_used_range('Объекты')
        except Exception:
            rows = None
        if rows:
            header = rows[0]
            for row_data in rows[1:]:
                row = dict(zip(header, row_data))
                object_id = str(row.get('ID объекта', '')).strip()
                if object_id:
                    object_names[object_id] = (
                        row.get('Объект') or row.get('Название') or row.get('Адрес') or object_id
                    )

        result = []
        for object_id, assignments in deps.load_assignments().items():
            for assignment in assignments:
                if assignment_status(assignment) != 'pending':
                    continue
                escalation = assignment_pending_escalation(assignment, now_ts)
                if not escalation['level']:
                    continue
                uid = str(assignment.get('user_id', ''))
                worker_name = deps.sanitize_display_name(profiles.get(uid, {}).get('name'), uid)
                object_name = object_names.get(str(object_id), str(object_id))
                work_label = (
                    deps.skill_display_name(assignment.get('work_type_id', ''))
                    if assignment.get('work_type_id')
                    else assignment.get('stage_id', '')
                )
                wait_label = format_assignment_wait(escalation['age_seconds'])
                parts = [part for part in (object_name, work_label, f'ждёт {wait_label}') if part]
                alert_id = assignment.get('id') or f"{uid}-{assignment.get('stage_id', '')}"
                result.append({
                    'id': f"assignment-confirm-{escalation['level']}-{object_id}-{alert_id}",
                    'type': escalation['type'],
                    'role_filter': 'owner',
                    'title': (
                        'Не подтверждена задача' if escalation['level'] == 'danger'
                        else 'Ждёт подтверждения'
                    ) + f': {worker_name}',
                    'subtitle': ' · '.join(parts),
                    'at': (
                        assignment.get('pending_since')
                        or assignment.get('updated_at')
                        or assignment.get('assigned_at')
                    ),
                    'assignment_confirmation': True,
                    'assignment_id': assignment.get('id', ''),
                    'assignment_object_id': str(object_id),
                    'assignment_worker_id': uid,
                    'response_wait_seconds': escalation['age_seconds'],
                    'response_escalation': escalation['level'],
                })
        return result

    return router, OwnerDashboardHandlers(
        assignment_status=assignment_status,
        parse_assignment_timestamp=parse_assignment_timestamp,
        assignment_pending_escalation=assignment_pending_escalation,
        get_dashboard_shifts_today=get_dashboard_shifts_today,
        get_active_blockers=get_active_blockers,
        get_team_plan=get_team_plan,
        format_assignment_wait=format_assignment_wait,
        assignment_confirmation_alerts=assignment_confirmation_alerts,
    )
