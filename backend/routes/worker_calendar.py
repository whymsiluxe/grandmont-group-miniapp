"""Worker calendar and period-statistics HTTP domain."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException


@dataclass(frozen=True)
class WorkerCalendarDeps:
    get_current_user: Callable[..., dict]
    get_role: Callable[..., str]
    load_absence: Callable[[], list]
    load_checkin_meta: Callable[[], list]
    load_assignments: Callable[[], dict]
    hours_from_session: Callable[[dict], float]
    get_worker_profile: Callable[[str], dict]
    sanitize_display_name: Callable[[object, str], str]


@dataclass(frozen=True)
class WorkerCalendarHandlers:
    get_worker_calendar: Callable[..., dict]
    worker_period_stats: Callable[..., dict]
    get_worker_calendar_stats: Callable[..., dict]


def create_worker_calendar_router(
    deps: WorkerCalendarDeps,
) -> tuple[APIRouter, WorkerCalendarHandlers]:
    router = APIRouter()

    @router.get('/api/workers/{target_user_id}/calendar')
    def get_worker_calendar(
        target_user_id: str,
        year: int,
        month: int,
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        if role != 'owner' and str(user['id']) != str(target_user_id):
            raise HTTPException(403, 'Можно смотреть только свой календарь')
        month_prefix = f'{year:04d}-{month:02d}'

        absence = [
            entry for entry in deps.load_absence()
            if str(entry['user_id']) == target_user_id
        ]
        unavailable_dates = set()
        for entry in absence:
            if entry.get('status') == 'rejected':
                continue
            current = datetime.strptime(entry['date_from'], '%Y-%m-%d')
            end = datetime.strptime(entry['date_to'], '%Y-%m-%d')
            while current <= end:
                date_str = current.strftime('%Y-%m-%d')
                if date_str.startswith(month_prefix):
                    unavailable_dates.add(date_str)
                current += timedelta(days=1)

        checkins = [
            item for item in deps.load_checkin_meta()
            if str(item.get('user_id')) == target_user_id
            and item.get('date', '').startswith(month_prefix)
        ]
        worked_dates = sorted(set(item['date'] for item in checkins))

        assigned_dates = set()
        for assignments in deps.load_assignments().values():
            for assignment in assignments:
                if (
                    assignment.get('user_id') != target_user_id
                    or not assignment.get('date_from')
                    or not assignment.get('date_to')
                ):
                    continue
                current = datetime.strptime(assignment['date_from'], '%Y-%m-%d')
                end = datetime.strptime(assignment['date_to'], '%Y-%m-%d')
                while current <= end:
                    date_str = current.strftime('%Y-%m-%d')
                    if date_str.startswith(month_prefix):
                        assigned_dates.add(date_str)
                    current += timedelta(days=1)
        assigned_dates -= unavailable_dates

        return {
            'unavailable_dates': sorted(unavailable_dates),
            'worked_dates': worked_dates,
            'assigned_dates': sorted(assigned_dates),
        }

    def worker_period_stats(target_id: str, date_from: str, date_to: str) -> dict:
        sessions = [
            session for session in deps.load_checkin_meta()
            if str(session.get('user_id')) == str(target_id)
            and date_from <= session.get('date', '') <= date_to
        ]
        worked_dates = sorted(set(
            session['date'] for session in sessions if session.get('date')
        ))
        total_hours = round(sum(deps.hours_from_session(s) for s in sessions), 2)
        days_worked = len(worked_dates)
        avg = round(total_hours / days_worked, 2) if days_worked else 0.0

        period_from = datetime.strptime(date_from, '%Y-%m-%d').date()
        period_to = datetime.strptime(date_to, '%Y-%m-%d').date()
        sick_days = vacation_days = 0
        for entry in deps.load_absence():
            if (
                str(entry.get('user_id')) != str(target_id)
                or entry.get('status') != 'approved'
            ):
                continue
            entry_from = datetime.strptime(entry['date_from'], '%Y-%m-%d').date()
            entry_to = datetime.strptime(
                entry.get('date_to') or entry['date_from'], '%Y-%m-%d'
            ).date()
            low, high = max(entry_from, period_from), min(entry_to, period_to)
            if low > high:
                continue
            days = (high - low).days + 1
            if entry.get('reason') == 'Krankheit':
                sick_days += days
            elif entry.get('reason') == 'Urlaub':
                vacation_days += days

        return {
            'date_from': date_from,
            'date_to': date_to,
            'days_worked': days_worked,
            'total_hours': total_hours,
            'avg_per_day': avg,
            'sick_days': sick_days,
            'vacation_days': vacation_days,
            'worked_dates': worked_dates,
        }

    @router.get('/api/workers/{target_user_id}/calendar-stats')
    def get_worker_calendar_stats(
        target_user_id: str,
        date_from: str,
        date_to: str,
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        if role != 'owner' and str(user['id']) != str(target_user_id):
            raise HTTPException(403, 'Можно смотреть только свою статистику')
        for value in (date_from, date_to):
            try:
                datetime.strptime(value, '%Y-%m-%d')
            except (ValueError, TypeError) as exc:
                raise HTTPException(
                    400, 'date_from/date_to должны быть YYYY-MM-DD'
                ) from exc
        if date_from > date_to:
            raise HTTPException(400, 'date_from не может быть позже date_to')
        stats = worker_period_stats(target_user_id, date_from, date_to)
        profile = deps.get_worker_profile(target_user_id)
        stats['name'] = deps.sanitize_display_name(
            profile.get('name'), str(target_user_id)
        )
        return stats

    return router, WorkerCalendarHandlers(
        get_worker_calendar=get_worker_calendar,
        worker_period_stats=worker_period_stats,
        get_worker_calendar_stats=get_worker_calendar_stats,
    )
