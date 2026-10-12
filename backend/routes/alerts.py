"""Role-aware alerts inbox and derived-alert dismissals."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, Depends
from pydantic import BaseModel


class AlertDismissBody(BaseModel):
    alert_ids: list[str]


@dataclass(frozen=True)
class AlertsDeps:
    get_current_user: Callable[..., dict]
    get_role: Callable[..., str]
    cached_get_used_range: Callable[[str], list]
    load_repo_objekte_lib: Callable[[], Any]
    load_repo_tools_lib: Callable[[], Any]
    load_assignments: Callable[[], dict]
    load_abwesenheit: Callable[[], list]
    assignment_confirmation_alerts: Callable[[], list]
    overdue_task_alerts: Callable[[], list]
    load_critical_alerts: Callable[[], list]
    load_activity_alerts: Callable[[], list]
    safe_load_json: Callable[[str, Any], Any]
    atomic_write_json: Callable[[str, Any], None]
    alert_dismissals_file: Callable[[], str]
    alert_dismiss_ttl: int


@dataclass(frozen=True)
class AlertsHandlers:
    load_alert_dismissals: Callable[[], dict]
    save_alert_dismissals: Callable[[dict], None]
    get_alerts: Callable[..., dict]
    dismiss_alerts: Callable[..., dict]


def create_alerts_router(deps: AlertsDeps) -> tuple[APIRouter, AlertsHandlers]:
    router = APIRouter()

    def load_alert_dismissals() -> dict:
        return deps.safe_load_json(deps.alert_dismissals_file(), {})

    def save_alert_dismissals(data: dict):
        deps.atomic_write_json(deps.alert_dismissals_file(), data)

    @router.get('/api/alerts')
    def get_alerts(
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        alerts = []

        try:
            rows = deps.cached_get_used_range('Объекты')
            if rows:
                header, data_rows = rows[0], rows[1:]
                for row in data_rows:
                    obj = dict(zip(header, row))
                    try:
                        pct = float(deps.load_repo_objekte_lib().get_budget_percent(obj) or 0)
                    except (ValueError, TypeError):
                        pct = 0
                    object_id = obj.get('ID объекта', '')
                    name = obj.get('Объект', object_id)
                    if pct >= 90:
                        alerts.append({
                            'id': f'budget-red-{object_id}',
                            'type': 'red',
                            'role_filter': 'owner',
                            'title': f'Бюджет перегружен: {name}',
                            'subtitle': f'{int(pct)}% использовано',
                            'at': None,
                        })
                    elif pct >= 60:
                        alerts.append({
                            'id': f'budget-yellow-{object_id}',
                            'type': 'yellow',
                            'role_filter': 'owner',
                            'title': f'Бюджет под угрозой: {name}',
                            'subtitle': f'{int(pct)}% использовано',
                            'at': None,
                        })
        except Exception:
            pass

        try:
            for tool in deps.load_repo_tools_lib().list_tools():
                raw_status = (tool.get('Статус') or '').strip().lower()
                if raw_status in ('в ремонте', 'не найден'):
                    alerts.append({
                        'id': f"tool-{tool.get('Серийный #', '')}",
                        'type': 'red',
                        'role_filter': 'owner',
                        'title': f"Инструмент: {tool.get('Название Инструмента', '')}",
                        'subtitle': f"Статус: {tool.get('Статус', '')}",
                        'at': None,
                    })
        except Exception:
            pass

        if role == 'worker':
            uid = str(user['id'])
            for object_id, assigned_list in deps.load_assignments().items():
                for assignment in assigned_list:
                    if assignment.get('user_id') == uid:
                        stage_text = (
                            f"Этап: {assignment['stage_id']}"
                            if assignment.get('stage_id') else 'Новое назначение'
                        )
                        alerts.append({
                            'id': f"assign-{object_id}-{assignment.get('stage_id', '')}",
                            'type': 'yellow',
                            'role_filter': 'worker',
                            'title': 'Вы назначены на объект',
                            'subtitle': stage_text,
                            'at': assignment.get('assigned_at'),
                        })

        if role == 'owner':
            for entry in deps.load_abwesenheit():
                if entry.get('status', 'pending') == 'pending':
                    alerts.append({
                        'id': f"abw-pending-{entry.get('id', '')}",
                        'type': 'red',
                        'role_filter': 'owner',
                        'title': (
                            f"Заявка на отсутствие: "
                            f"{entry.get('name', entry.get('user_id', '?'))}"
                        ),
                        'subtitle': (
                            f"{entry.get('date_from', '?')} — {entry.get('date_to', '?')}"
                            f" · {entry.get('reason', '?')}"
                        ),
                        'at': entry.get('created_at'),
                    })
            alerts.extend(deps.assignment_confirmation_alerts())
            alerts.extend(deps.overdue_task_alerts())

        for critical in deps.load_critical_alerts():
            if critical.get('target_user_id') != str(user['id']):
                continue
            if critical.get('acknowledged_at'):
                continue
            alerts.append({
                'id': f"critical-{critical['id']}",
                'type': 'red',
                'role_filter': role,
                'title': critical['title'],
                'subtitle': critical.get('subtitle', ''),
                'at': critical.get('created_at'),
                'critical_alert_id': critical['id'],
            })

        try:
            activities = [
                activity for activity in deps.load_activity_alerts()
                if activity.get('target_user_id') == str(user['id'])
                and not activity.get('read_at')
            ]
            aggregated = {}
            for activity in activities:
                aggregated.setdefault((activity['kind'], activity['ref_id']), []).append(activity)
            for (kind, ref_id), group in aggregated.items():
                count = len(group)
                latest = max(group, key=lambda item: item.get('created_at', 0))
                noun = 'новости' if kind == 'news_comment' else 'фото'
                title = (
                    f'{count} новых комментария к {noun}'
                    if count > 1 else latest.get('title', '')
                )
                alerts.append({
                    'id': f'activity-{kind}-{ref_id}',
                    'type': 'info',
                    'role_filter': role,
                    'title': title,
                    'subtitle': latest.get('subtitle', ''),
                    'at': latest.get('created_at'),
                    'activity_kind': kind,
                    'activity_ref_id': ref_id,
                    'activity_deep_link': 'news' if kind == 'news_comment' else 'photos',
                })
        except Exception:
            pass

        filtered = [alert for alert in alerts if alert['role_filter'] == role]
        dismissals = load_alert_dismissals().get(str(user['id']), {})
        now = int(time.time())
        filtered = [
            alert for alert in filtered
            if now - dismissals.get(alert['id'], 0) > deps.alert_dismiss_ttl
        ]
        return {'alerts': filtered, 'count': len(filtered)}

    @router.post('/api/alerts/dismiss')
    def dismiss_alerts(
        body: AlertDismissBody,
        user: dict = Depends(deps.get_current_user),
    ):
        data = load_alert_dismissals()
        entry = data.setdefault(str(user['id']), {})
        now = int(time.time())
        for alert_id in body.alert_ids:
            entry[alert_id] = now
        save_alert_dismissals(data)
        return {'ok': True}

    return router, AlertsHandlers(
        load_alert_dismissals=load_alert_dismissals,
        save_alert_dismissals=save_alert_dismissals,
        get_alerts=get_alerts,
        dismiss_alerts=dismiss_alerts,
    )
