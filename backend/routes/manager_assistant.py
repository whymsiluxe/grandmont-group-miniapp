"""Manager command parsing and worker task-extraction helpers."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel


class ExtractTaskBody(BaseModel):
    text: str
    object_id: str = ''


class ManagementCommandBody(BaseModel):
    text: str


@dataclass(frozen=True)
class ManagerAssistantDeps:
    get_current_user: Callable[..., dict]
    get_role: Callable[..., str]
    business_now: Callable[[], datetime]
    load_worker_profiles: Callable[[], dict]
    load_roles: Callable[[], dict]
    sanitize_display_name: Callable[[object, str], str]
    cached_get_used_range: Callable[[str], list]
    call_glm_json: Callable[[str, str], str]


@dataclass(frozen=True)
class ManagerAssistantHandlers:
    normalize_ru_lookup: Callable[[str], str]
    normalize_ru_name_hint: Callable[[str], str]
    management_command_date: Callable[..., str]
    resolve_management_worker: Callable[..., dict]
    resolve_management_object: Callable[..., dict]
    parse_management_command: Callable[..., dict]
    management_workers_for_parse: Callable[[], list]
    management_objects_for_parse: Callable[[], list]
    parse_manager_command: Callable[..., dict]
    extract_task_from_text: Callable[..., dict]


def create_manager_assistant_router(
    deps: ManagerAssistantDeps,
) -> tuple[APIRouter, ManagerAssistantHandlers]:
    router = APIRouter()

    def normalize_ru_lookup(value: str) -> str:
        return re.sub(r'[^a-zа-я0-9]+', '', str(value or '').lower().replace('ё', 'е'))

    def normalize_ru_name_hint(value: str) -> str:
        value = str(value or '').strip()
        if len(value) > 3 and value[-1:].lower() in ('у', 'ю'):
            return value[:-1]
        return value

    def management_command_date(label: str, base_dt: datetime | None = None) -> str:
        base = base_dt or deps.business_now()
        key = str(label or '').strip().lower()
        if key == 'сегодня':
            return base.date().isoformat()
        if key == 'завтра':
            return (base.date() + timedelta(days=1)).isoformat()
        if key == 'послезавтра':
            return (base.date() + timedelta(days=2)).isoformat()
        match = re.match(r'^(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?$', key)
        if not match:
            return ''
        day, month = int(match.group(1)), int(match.group(2))
        year = int(match.group(3)) if match.group(3) else base.year
        if year < 100:
            year += 2000
        try:
            return datetime(year, month, day).date().isoformat()
        except ValueError:
            return ''

    def resolve_management_worker(worker_query: str, workers: list[dict] | None = None) -> dict:
        workers = workers or []
        query_norm = normalize_ru_lookup(normalize_ru_name_hint(worker_query))
        for worker in workers:
            name = str(worker.get('name') or worker.get('first_name') or '').strip()
            if query_norm and query_norm in normalize_ru_lookup(name):
                return {'id': str(worker.get('user_id') or worker.get('id') or ''), 'name': name}
        return {'id': '', 'name': normalize_ru_name_hint(worker_query)}

    def resolve_management_object(object_query: str, objects: list[dict] | None = None) -> dict:
        objects = objects or []
        query_norm = normalize_ru_lookup(object_query)
        for obj in objects:
            name = str(obj.get('name') or obj.get('Объект') or '').strip()
            object_id = str(obj.get('id') or obj.get('ID объекта') or '').strip()
            if query_norm and (
                query_norm in normalize_ru_lookup(name)
                or query_norm in normalize_ru_lookup(object_id)
            ):
                return {'id': object_id, 'name': name or object_id}
        return {'id': '', 'name': object_query.strip()}

    def parse_management_command(
        text: str,
        base_dt: datetime | None = None,
        workers: list[dict] | None = None,
        objects: list[dict] | None = None,
    ) -> dict:
        raw = re.sub(r'\s+', ' ', str(text or '').strip().rstrip('.!?'))
        if not raw:
            raise ValueError('Пустая команда')
        match = re.match(
            r'(?i)^поставь\s+(?P<worker>.+?)\s+'
            r'(?P<date>сегодня|завтра|послезавтра|\d{1,2}\.\d{1,2}(?:\.\d{2,4})?)\s+'
            r'задач[ауи]\s+(?P<body>.+)$',
            raw,
        )
        if not match:
            raise ValueError('Не удалось разобрать команду управления')

        task_body = match.group('body').strip()
        comment = ''
        comment_match = re.search(r'(?i)\s+и\s+скажи\s+ему\s+(?P<comment>.+)$', task_body)
        if comment_match:
            comment = comment_match.group('comment').strip()
            task_body = task_body[:comment_match.start()].strip()

        object_query = ''
        task_text = task_body
        object_match = re.search(r'(?i)\s+у\s+(?P<object>[^,]+)$', task_body)
        if object_match:
            object_query = object_match.group('object').strip()
            task_text = task_body[:object_match.start()].strip()

        worker = resolve_management_worker(match.group('worker').strip(), workers)
        obj = resolve_management_object(object_query, objects) if object_query else {'id': '', 'name': ''}
        due_date = management_command_date(match.group('date'), base_dt)
        return {
            'intent': 'assign_task',
            'worker_query': match.group('worker').strip(),
            'worker_id': worker.get('id', ''),
            'worker_name': worker.get('name', ''),
            'date': due_date,
            'date_text': match.group('date').strip(),
            'object_query': object_query,
            'object_id': obj.get('id', ''),
            'object_name': obj.get('name', ''),
            'task': task_text,
            'comment': comment,
            'requires_confirmation': True,
        }

    def management_workers_for_parse() -> list[dict]:
        profiles = deps.load_worker_profiles()
        roles = deps.load_roles()
        result = []
        for uid, role in roles.items():
            if role == 'worker':
                profile = profiles.get(str(uid), {})
                result.append({
                    'user_id': str(uid),
                    'name': deps.sanitize_display_name(
                        profile.get('name') or profile.get('first_name'), str(uid)
                    ),
                })
        return result

    def management_objects_for_parse() -> list[dict]:
        rows = deps.cached_get_used_range('Объекты')
        if not rows:
            return []
        header, data = rows[0], rows[1:]
        result = []
        for row in data:
            obj = dict(zip(header, row))
            result.append({
                'id': str(obj.get('ID объекта', '')).strip(),
                'name': str(obj.get('Объект', '')).strip(),
            })
        return result

    @router.post('/api/manager/command/parse')
    def parse_manager_command(
        body: ManagementCommandBody,
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        if role != 'owner':
            raise HTTPException(403, 'Только для владельца')
        text = body.text.strip()
        if not text:
            raise HTTPException(400, 'Пустая команда')
        try:
            workers = management_workers_for_parse()
        except Exception:
            workers = []
        try:
            objects = management_objects_for_parse()
        except Exception:
            objects = []
        try:
            draft = parse_management_command(text, workers=workers, objects=objects)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {'draft': draft, 'requires_confirmation': True}

    @router.post('/api/tasks/extract')
    def extract_task_from_text(
        body: ExtractTaskBody,
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        if role == 'owner':
            raise HTTPException(403, 'Потребности создают работники')
        text = body.text.strip()
        if not text:
            raise HTTPException(400, 'Пустой текст')
        system = (
            'Ты помощник, который извлекает из голосового сообщения строителя запрос на '
            'инструмент/материалы/защиту. Верни СТРОГО валидный JSON без пояснений: '
            '{"title": "короткое название (макс 60 символов)", "description": "детали, если есть"}. '
            'Если в тексте нет реального запроса на что-либо — верни {"title": "", "description": ""}.'
        )
        try:
            raw = deps.call_glm_json(system, text)
        except Exception as exc:
            raise HTTPException(502, f'AI недоступен: {exc}')
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not match:
            raise HTTPException(502, 'AI вернул не-JSON ответ')
        try:
            parsed = json.loads(match.group(0))
        except Exception:
            raise HTTPException(502, 'Не удалось разобрать ответ AI')
        return {
            'title': parsed.get('title', '')[:200],
            'description': parsed.get('description', '')[:1000],
        }

    return router, ManagerAssistantHandlers(
        normalize_ru_lookup=normalize_ru_lookup,
        normalize_ru_name_hint=normalize_ru_name_hint,
        management_command_date=management_command_date,
        resolve_management_worker=resolve_management_worker,
        resolve_management_object=resolve_management_object,
        parse_management_command=parse_management_command,
        management_workers_for_parse=management_workers_for_parse,
        management_objects_for_parse=management_objects_for_parse,
        parse_manager_command=parse_manager_command,
        extract_task_from_text=extract_task_from_text,
    )
