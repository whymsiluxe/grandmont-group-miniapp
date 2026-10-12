"""Workforce profile statistics and owner team-hours views."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date, datetime, timedelta
from typing import Any, Callable

from fastapi import APIRouter, Depends


@dataclass(frozen=True)
class WorkforceStatsDeps:
    get_current_user: Callable[..., dict]
    get_role: Callable[..., str]
    require_owner: Callable[..., None]
    load_checkin_meta: Callable[[], list]
    load_roles: Callable[[], dict]
    load_worker_profiles: Callable[[], dict]
    sanitize_display_name: Callable[[Any, str], str]
    hours_from_session: Callable[[dict], float]
    session_hours_live: Callable[[dict], float]
    is_active_photo_checkin_session: Callable[[dict], bool]
    business_today: Callable[[], Any]
    load_assignments: Callable[[], dict]
    cached_get_used_range: Callable[[str], list]
    load_abwesenheit: Callable[[], list]
    get_worker_profile: Callable[[Any], dict]
    get_worker_skills_v2: Callable[[Any], list]
    legacy_skill_names_from_v2: Callable[[list], list]


@dataclass(frozen=True)
class WorkforceStatsHandlers:
    profile_stats: Callable[..., dict]
    get_dashboard_team_hours: Callable[..., dict]


def create_workforce_stats_router(
    deps: WorkforceStatsDeps,
) -> tuple[APIRouter, WorkforceStatsHandlers]:
    router = APIRouter()

    @router.get('/api/profile/stats')
    def profile_stats(
        user_id: str = '',
        period: str = 'week',
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        target = user_id if (role == 'owner' and user_id) else str(user['id'])
        sessions = [
            session for session in deps.load_checkin_meta()
            if str(session.get('user_id')) == target
        ]

        team_hours = None
        if role == 'owner' and not user_id:
            roles = deps.load_roles()
            worker_ids = [uid for uid, current_role in roles.items() if current_role == 'worker']
            profiles = deps.load_worker_profiles()
            today0 = deps.business_today()
            week_start = today0 - timedelta(days=6)
            team_hours = []
            for wid in worker_ids:
                worker_sessions = [
                    session for session in deps.load_checkin_meta()
                    if str(session.get('user_id')) == wid
                    and week_start.isoformat() <= session.get('date', '') <= today0.isoformat()
                ]
                hours = round(sum(deps.hours_from_session(session) for session in worker_sessions), 1)
                team_hours.append({
                    'user_id': wid,
                    'name': deps.sanitize_display_name(profiles.get(wid, {}).get('name'), wid),
                    'hours': hours,
                })
            team_hours.sort(key=lambda item: item['hours'], reverse=True)

        period_data = None
        if period == 'month':
            today0 = deps.business_today()
            days = []
            for index in range(29, -1, -1):
                day = today0 - timedelta(days=index)
                iso = day.isoformat()
                hours = sum(
                    deps.hours_from_session(session)
                    for session in sessions if session.get('date') == iso
                )
                days.append({'date': iso, 'hours': round(hours, 2)})
            period_data = {
                'kind': 'heatmap',
                'days': days,
                'total_hours': round(sum(day['hours'] for day in days), 1),
            }
        elif period in ('3months', 'year'):
            weeks_back = 13 if period == '3months' else 52
            today0 = deps.business_today()
            buckets = []
            for index in range(weeks_back - 1, -1, -1):
                week_end = today0 - timedelta(days=index * 7)
                week_start = week_end - timedelta(days=6)
                hours = sum(
                    deps.hours_from_session(session)
                    for session in sessions
                    if week_start.isoformat() <= session.get('date', '') <= week_end.isoformat()
                )
                buckets.append({'label': week_start.isoformat(), 'hours': round(hours, 2)})
            period_data = {
                'kind': 'bar',
                'buckets': buckets,
                'total_hours': round(sum(bucket['hours'] for bucket in buckets), 1),
            }

        today = deps.business_today()
        week = []
        for index in range(6, -1, -1):
            day = today - timedelta(days=index)
            iso = day.isoformat()
            hours = sum(
                deps.hours_from_session(session)
                for session in sessions if session.get('date') == iso
            )
            week.append({'date': iso, 'weekday': day.weekday(), 'hours': round(hours, 2)})

        by_object = {}
        for session in sessions:
            object_id = str(session.get('object_id', ''))
            record = by_object.setdefault(object_id, {
                'object_id': object_id,
                'sessions': 0,
                'total_hours': 0.0,
                'last_date': '',
                'assigned_stages': [],
            })
            record['sessions'] += 1
            record['total_hours'] += deps.hours_from_session(session)
            record['last_date'] = max(record['last_date'], session.get('date', ''))
        for object_id, assignments in deps.load_assignments().items():
            for assignment in assignments:
                if assignment.get('user_id') == target:
                    record = by_object.setdefault(str(object_id), {
                        'object_id': str(object_id),
                        'sessions': 0,
                        'total_hours': 0.0,
                        'last_date': '',
                        'assigned_stages': [],
                    })
                    if assignment.get('stage_id'):
                        record['assigned_stages'].append(assignment['stage_id'])
        object_history = sorted(by_object.values(), key=lambda item: item['last_date'], reverse=True)
        for record in object_history:
            record['total_hours'] = round(record['total_hours'], 1)
        try:
            rows = deps.cached_get_used_range('Объекты')
            names = {}
            if rows:
                header = rows[0]
                for row_data in rows[1:]:
                    row = dict(zip(header, row_data))
                    names[str(row.get('ID объекта', ''))] = row.get('Объект', '')
            for record in object_history:
                record['object_name'] = names.get(record['object_id']) or record['object_id']
        except Exception:
            for record in object_history:
                record['object_name'] = record['object_id']

        finished = [
            session for session in sessions
            if not session.get('manual_entry') and session.get('finish_at')
        ]
        avg_session_hours = (
            round(sum(deps.hours_from_session(session) for session in finished) / len(finished), 2)
            if finished else None
        )
        analyzed = [session for session in sessions if (session.get('analysis') or {}).get('progress')]
        work_speed = None
        if analyzed:
            last = max(analyzed, key=lambda session: session.get('finish_at') or 0)
            work_speed = {
                'analyzed_sessions': len(analyzed),
                'last_summary': (last['analysis']['progress'] or '')[:400],
            }

        yearly_days = 24
        this_year = str(today.year)
        urlaub_used = 0
        krankheit_used = 0
        for entry in deps.load_abwesenheit():
            if str(entry.get('user_id')) != target or entry.get('status') != 'approved':
                continue
            if not entry.get('date_from', '').startswith(this_year):
                continue
            date_from = datetime.strptime(entry['date_from'], '%Y-%m-%d').date()
            date_to = datetime.strptime(entry['date_to'], '%Y-%m-%d').date()
            days = (date_to - date_from).days + 1
            if entry.get('reason') == 'Urlaub':
                urlaub_used += days
            elif entry.get('reason') == 'Krankheit':
                krankheit_used += days

        profile = deps.get_worker_profile(target)
        skills_v2 = deps.get_worker_skills_v2(target)
        return {
            'user_id': target,
            'name': deps.sanitize_display_name(
                profile.get('name') or (user.get('first_name') if target == str(user['id']) else None),
                target,
            ),
            'role': deps.load_roles().get(target, 'worker'),
            'skills': deps.legacy_skill_names_from_v2(skills_v2),
            'skills_v2': skills_v2,
            'sizes': {
                'pants': profile.get('pants_size', ''),
                'shirt': profile.get('shirt_size', ''),
                'shoe': profile.get('shoe_size', ''),
            },
            'has_avatar': bool(profile.get('avatar')),
            'birthday': profile.get('birthday'),
            'urlaub': {
                'used': urlaub_used,
                'total': yearly_days,
                'remaining': max(0, yearly_days - urlaub_used),
            },
            'krankheit_days_this_year': krankheit_used,
            'week': week,
            'week_total_hours': round(sum(day['hours'] for day in week), 1),
            'period': period,
            'period_data': period_data,
            'team_hours': team_hours,
            'avg_session_hours': avg_session_hours,
            'work_speed': work_speed,
            'objects': object_history,
        }

    @router.get('/api/dashboard/team-hours')
    def get_dashboard_team_hours(
        date_from: str = '',
        date_to: str = '',
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(deps.require_owner),
    ):
        def _parse(value):
            try:
                return _date.fromisoformat(value)
            except Exception:
                return None

        today = deps.business_today()
        range_start, range_end = _parse(date_from), _parse(date_to)
        if not (range_start and range_end):
            range_start = today - timedelta(days=today.weekday())
            range_end = range_start + timedelta(days=6)
        if range_start > range_end:
            range_start, range_end = range_end, range_start
        if (range_end - range_start).days > 62:
            range_end = range_start + timedelta(days=62)
        from_iso, to_iso, today_iso = (
            range_start.isoformat(), range_end.isoformat(), today.isoformat()
        )
        today_in_range = from_iso <= today_iso <= to_iso

        roles = deps.load_roles()
        worker_ids = [uid for uid, current_role in roles.items() if current_role == 'worker']
        profiles = deps.load_worker_profiles()
        all_sessions = deps.load_checkin_meta()

        object_names = {}
        try:
            rows = deps.cached_get_used_range('Объекты')
            if rows:
                header = rows[0]
                for row_data in rows[1:]:
                    row = dict(zip(header, row_data))
                    object_names[str(row.get('ID объекта', ''))] = row.get('Объект', '')
        except Exception:
            object_names = {}

        def _safe_team_name(worker_id):
            name = deps.sanitize_display_name(
                profiles.get(worker_id, {}).get('name'), 'Сотрудник'
            )
            return 'Сотрудник' if (
                name.strip() == str(worker_id) or name.strip().isdigit()
            ) else name

        workers = []
        for worker_id in worker_ids:
            worker_sessions = [
                session for session in all_sessions
                if str(session.get('user_id')) == worker_id
            ]
            in_range = [
                session for session in worker_sessions
                if from_iso <= session.get('date', '') <= to_iso
            ]
            hours_week = round(sum(deps.session_hours_live(session) for session in in_range), 1)
            today_sessions = (
                [session for session in worker_sessions if session.get('date', '') == today_iso]
                if today_in_range else []
            )
            hours_today = round(
                sum(deps.session_hours_live(session) for session in today_sessions), 1
            )
            open_session = next(
                (
                    session for session in today_sessions
                    if deps.is_active_photo_checkin_session(session)
                ),
                None,
            )
            current_object_id = str(open_session.get('object_id', '')) if open_session else ''
            workers.append({
                'user_id': worker_id,
                'name': _safe_team_name(worker_id),
                'has_avatar': bool(profiles.get(worker_id, {}).get('avatar')),
                'hours_today': hours_today,
                'hours_week': hours_week,
                'is_working_now': open_session is not None,
                'current_object_id': current_object_id,
                'current_object_name': (
                    object_names.get(current_object_id, current_object_id)
                    if current_object_id else ''
                ),
            })
        workers.sort(key=lambda worker: (
            not worker['is_working_now'], -worker['hours_week'], worker['name'].lower()
        ))
        return {
            'date_from': from_iso,
            'date_to': to_iso,
            'total_hours': round(sum(worker['hours_week'] for worker in workers), 1),
            'today_hours': round(sum(worker['hours_today'] for worker in workers), 1),
            'workers_with_hours': sum(1 for worker in workers if worker['hours_week'] > 0),
            'workers': workers,
        }

    return router, WorkforceStatsHandlers(
        profile_stats=profile_stats,
        get_dashboard_team_hours=get_dashboard_team_hours,
    )
