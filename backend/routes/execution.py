"""Manual time entry and AI photo-analysis HTTP routes. Split out of the
checkin shift lifecycle (routes/checkin.py) per the Execution dependency-map
decision: these routes have a different coupling profile -- no lock,
idempotency, or outbox involvement for manual entry beyond a simple
lock+idempotency pair, and the AI-analysis routes are entirely block-local
except for one cross-domain write into Mangel (analyze_checkin_defects ->
mangel_lib.create_ticket), passed in explicitly as a dep rather than
imported, so this module never grows a direct dependency on mangel_lib.

This module deliberately does not import backend.main. main.py owns the
shared runtime state (checkin_meta store, the checkin lock, the durable
idempotency layer) and wires it through ExecutionRouteDeps, then includes
the returned router and re-exports the handlers for legacy direct-call
tests.

Per the explicit architecture decision for this extraction: shared checkin
state / locks / idempotency are NOT being moved into core/* in this pass --
they stay canonically in main.py and are injected here via deps.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.request as _urlreq
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel

try:
    from ..core.media_paths import resolve_owned_media_path
except ImportError:
    from core.media_paths import resolve_owned_media_path  # noqa: E402


MANUAL_TIME_ART_WHITELIST = {'Arbeitszeit', 'Fahrzeit'}


class ZeiterfassungBody(BaseModel):
    object_id: str
    art: str = "Arbeitszeit"
    date: str
    start_time: str
    end_time: str
    pause_minutes: int = 0
    description: str = ''
    mitarbeiter_user_id: str | None = None  # owner может внести за другого работника


@dataclass(frozen=True)
class ExecutionRouteDeps:
    get_current_user: Callable
    get_role: Callable
    checkin_lock: object
    load_checkin_meta: Callable
    save_checkin_meta: Callable
    idempotency_scope: Callable
    idempotency_claim: Callable
    idempotency_save: Callable
    idempotency_release: Callable
    idempotent_entity_id: Callable
    write_zeiterfassung_row: Callable
    cached_get_used_range: Callable
    load_assignments: Callable
    load_roles: Callable
    has_active_object_access: Callable
    business_now: Callable
    business_today_str: Callable
    checkin_photo_base: Callable
    check_ai_rate: Callable
    create_mangel_ticket: Callable


def create_execution_router(deps: ExecutionRouteDeps):
    router = APIRouter()

    def _parse_manual_date(value: str) -> str:
        date_str = (value or '').strip()
        try:
            datetime.strptime(date_str, '%Y-%m-%d')
        except Exception:
            raise HTTPException(400, "date должен быть в формате YYYY-MM-DD")
        if date_str > deps.business_today_str():
            raise HTTPException(400, "Нельзя внести время за будущую дату")
        return date_str

    def _parse_manual_hhmm(value: str, field_name: str) -> int:
        raw = (value or '').strip()
        if not re.fullmatch(r'\d{2}:\d{2}', raw):
            raise HTTPException(400, f"{field_name} должен быть в формате HH:MM")
        hours, minutes = map(int, raw.split(':'))
        if hours > 23 or minutes > 59:
            raise HTTPException(400, f"{field_name} должен быть реальным временем HH:MM")
        return hours * 60 + minutes

    def _object_id_exists(object_id: str) -> bool:
        oid = (object_id or '').strip()
        if not oid:
            return False
        try:
            rows = deps.cached_get_used_range('Объекты')
            if rows:
                header, data = rows[0], rows[1:]
                try:
                    id_idx = header.index('ID объекта')
                except ValueError:
                    id_idx = 0
                for row in data:
                    if id_idx < len(row) and str(row[id_idx]).strip() == oid:
                        return True
        except Exception as e:
            print(f'WARNING: object existence check failed for manual time {oid}: {e}')
        return oid in deps.load_assignments()

    def _manual_interval_overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
        return a_start < b_end and b_start < a_end

    def _assert_no_manual_time_overlap(target_user_id: str, date_str: str, start_minutes: int, end_minutes: int,
                                        exclude_id: str | None = None, items: list | None = None) -> None:
        """F02: та же дата+работник не может иметь два пересекающихся интервала времени --
        ни ручная-vs-ручная, ни ручная-vs-фото check-in (photo-сессия того же дня с известным
        finish_at также считается интервалом; ещё активная (finish_at is None) не пересекает
        по времени, т.к. её конец неизвестен -- не блокируем на этом основании)."""
        for item in items if items is not None else deps.load_checkin_meta():
            if str(item.get('user_id')) != str(target_user_id) or item.get('date') != date_str:
                continue
            if exclude_id and item.get('id') == exclude_id:
                continue
            if item.get('manual_entry'):
                try:
                    h1, m1 = map(int, item['start_time'].split(':'))
                    h2, m2 = map(int, item['end_time'].split(':'))
                except Exception:
                    continue
                other_start, other_end = h1 * 60 + m1, h2 * 60 + m2
            elif item.get('start_at') and item.get('finish_at'):
                tz = deps.business_now().tzinfo
                other_start_dt = datetime.fromtimestamp(item['start_at'], tz)
                other_end_dt = datetime.fromtimestamp(item['finish_at'], tz)
                other_start = other_start_dt.hour * 60 + other_start_dt.minute
                other_end = other_end_dt.hour * 60 + other_end_dt.minute
                if other_end <= other_start:
                    continue  # смена перевалила через полночь -- вне объёма этой проверки
            else:
                continue
            if _manual_interval_overlaps(start_minutes, end_minutes, other_start, other_end):
                raise HTTPException(409, "Указанный интервал пересекается с уже существующей записью времени за эту дату")

    def _validate_manual_time_body(body: 'ZeiterfassungBody', user: dict, role: str) -> dict:
        object_id = body.object_id.strip()[:100]
        if not object_id:
            raise HTTPException(400, "object_id обязателен")
        if not _object_id_exists(object_id):
            raise HTTPException(404, "Объект не найден")

        date_str = _parse_manual_date(body.date)
        start_minutes = _parse_manual_hhmm(body.start_time, 'start_time')
        end_minutes = _parse_manual_hhmm(body.end_time, 'end_time')
        if end_minutes <= start_minutes:
            raise HTTPException(400, "end_time должен быть позже start_time")
        pause_minutes = int(body.pause_minutes or 0)
        if pause_minutes < 0:
            raise HTTPException(400, "pause_minutes не может быть отрицательным")
        duration_minutes = end_minutes - start_minutes
        if pause_minutes >= duration_minutes:
            raise HTTPException(400, "pause_minutes должен быть меньше длительности смены")

        art = (body.art or 'Arbeitszeit').strip()[:50] or 'Arbeitszeit'
        if art not in MANUAL_TIME_ART_WHITELIST:
            raise HTTPException(400, f"art должен быть одним из: {', '.join(sorted(MANUAL_TIME_ART_WHITELIST))}")

        target_user_id = str(body.mitarbeiter_user_id).strip() if (role == 'owner' and body.mitarbeiter_user_id) else str(user['id'])
        if not re.fullmatch(r'\d+', target_user_id):
            raise HTTPException(400, "mitarbeiter_user_id должен быть числовым Telegram ID")
        roles = deps.load_roles()
        if target_user_id not in roles:
            raise HTTPException(404, "Работник не найден")
        if role != 'owner' and target_user_id != str(user['id']):
            raise HTTPException(403, "Нельзя внести время за другого работника")
        if roles.get(target_user_id) != 'owner' and not deps.has_active_object_access(target_user_id, object_id, today=date_str):
            raise HTTPException(403, "У работника нет принятого назначения на этот объект в указанную дату")

        start_time = (body.start_time or '').strip()
        end_time = (body.end_time or '').strip()
        return {
            "object_id": object_id,
            "target_user_id": target_user_id,
            "date": date_str,
            "start_time": start_time,
            "end_time": end_time,
            "pause_minutes": pause_minutes,
            "art": art,
            "description": body.description.strip()[:500],
        }

    @router.post("/api/checkin/manual")
    def checkin_manual(body: ZeiterfassungBody, user: dict = Depends(deps.get_current_user), role: str = Depends(deps.get_role),
                        idempotency_key: str = Header(default='', alias='Idempotency-Key')):
        clean = _validate_manual_time_body(body, user, role)
        target_user_id = clean["target_user_id"]
        idempotency_scope = deps.idempotency_scope(
            'checkin_manual', user['id'], f"{target_user_id}:{clean['object_id']}:{clean['date']}", clean,
        )
        cached = deps.idempotency_claim(idempotency_key, idempotency_scope)
        if cached is not None:
            return cached

        entity_id = deps.idempotent_entity_id(idempotency_key, idempotency_scope)
        entry = {
            "id": entity_id,
            "object_id": clean["object_id"],
            "date": clean["date"],
            "user_id": target_user_id,
            "art": clean["art"],
            "start_time": clean["start_time"],
            "end_time": clean["end_time"],
            "pause_minutes": clean["pause_minutes"],
            "description": clean["description"],
            "manual_entry": True,
            "created_at": int(time.time()),
        }
        try:
            with deps.checkin_lock:
                items = deps.load_checkin_meta()
                # F02/F03: overlap validation and the append must share the same
                # critical section. Otherwise two concurrent requests can both
                # validate the same stale snapshot and persist overlapping time.
                # exclude_id keeps crash-recovered retries for the same key valid.
                _assert_no_manual_time_overlap(
                    target_user_id, clean['date'],
                    _parse_manual_hhmm(clean['start_time'], 'start_time'),
                    _parse_manual_hhmm(clean['end_time'], 'end_time'),
                    exclude_id=entity_id,
                    items=items,
                )
                existing_entry = next((i for i in items if i.get('id') == entity_id), None)
                if existing_entry is not None:
                    # F03: crash-recovered retry -- an earlier attempt with the SAME
                    # Idempotency-Key already wrote this exact business fact (same
                    # derived id) but died before _idempotency_save() ran. Don't
                    # duplicate it -- resolve the response from what's already there.
                    entry = existing_entry
                else:
                    items.append(entry)
                    deps.save_checkin_meta(items)
            if existing_entry is None:
                deps.write_zeiterfassung_row(entry, entry['object_id'], target_user_id)
            deps.idempotency_save(idempotency_key, entry, idempotency_scope)
        except Exception:
            deps.idempotency_release(idempotency_key, idempotency_scope)
            raise
        return entry

    def _image_block_from_file(path: str) -> dict | None:
        if not os.path.exists(path):
            return None
        ext = path.rsplit('.', 1)[-1].lower()
        media_type = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png', 'webp': 'image/webp'}.get(ext, 'image/jpeg')
        with open(path, 'rb') as f:
            b64 = base64.b64encode(f.read()).decode('ascii')
        return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}}

    def _call_glm_vision(system_prompt: str, image_paths: list, text_prompt: str) -> str:
        content = []
        for p in image_paths:
            safe_path = resolve_owned_media_path(deps.checkin_photo_base(), p)
            if not safe_path:
                continue
            block = _image_block_from_file(safe_path)
            if block:
                content.append(block)
        content.append({"type": "text", "text": text_prompt})

        glm_key = os.environ.get('GLM_KEY', '')
        if not glm_key:
            raise HTTPException(503, "GLM API не настроен (нет GLM_KEY)")

        payload = {
            "model": "glm-4.5-flash",
            "max_tokens": 1024,
            "system": system_prompt,
            "messages": [{"role": "user", "content": content}],
        }
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        req = _urlreq.Request(
            'https://api.z.ai/api/anthropic/v1/messages',
            data=data, method='POST',
            headers={'Content-Type': 'application/json', 'x-api-key': glm_key, 'anthropic-version': '2023-06-01'},
        )
        try:
            with _urlreq.urlopen(req, timeout=60) as resp:
                result = json.loads(resp.read().decode('utf-8'))
            return result['content'][0]['text']
        except _urlreq.HTTPError as e:
            raise HTTPException(502, f"GLM API ошибка: {e.read().decode('utf-8')[:300]}")
        except Exception as e:
            raise HTTPException(502, f"GLM недоступен: {str(e)[:200]}")

    def _get_checkin_session(session_id: str, user_id=None, role=None) -> dict:
        items = deps.load_checkin_meta()
        session = next((i for i in items if i.get('id') == session_id), None)
        if not session:
            raise HTTPException(404, "Сессия не найдена")
        if role is not None and role != 'owner' and str(session.get('user_id')) != str(user_id):
            raise HTTPException(403, "Нет доступа к этой смене")
        if not session.get('finish_photos'):
            raise HTTPException(400, "Смена ещё не завершена — нет финишных фото для сравнения")
        return session

    def _save_checkin_analysis(session_id: str, key: str, value):
        items = deps.load_checkin_meta()
        for i in items:
            if i.get('id') == session_id:
                i.setdefault('analysis', {})[key] = value
                deps.save_checkin_meta(items)
                return
        raise HTTPException(404, "Сессия не найдена")

    @router.post("/api/checkin/{session_id}/analyze-progress")
    def analyze_checkin_progress(session_id: str, user: dict = Depends(deps.get_current_user), role: str = Depends(deps.get_role)):
        session = _get_checkin_session(session_id, user['id'], role)
        deps.check_ai_rate(user['id'])
        result = _call_glm_vision(
            "Ты — опытный прораб на стройке. Сравниваешь фото 'до' и 'после' работ на одном участке "
            "объекта. Кратко (2-4 предложения, по-русски) опиши: какой прогресс виден, что изменилось, "
            "выглядит ли работа завершённой или частично сделанной.",
            session['start_photos'] + session['finish_photos'],
            "Вот фото начала смены, затем фото конца смены. Сравни прогресс работ.",
        )
        _save_checkin_analysis(session_id, 'progress', result)
        return {"analysis": result}

    @router.post("/api/checkin/{session_id}/analyze-materials")
    def analyze_checkin_materials(session_id: str, user: dict = Depends(deps.get_current_user), role: str = Depends(deps.get_role)):
        session = _get_checkin_session(session_id, user['id'], role)
        deps.check_ai_rate(user['id'])
        result = _call_glm_vision(
            "Ты — сметчик на стройке. По фото оцениваешь примерный расход строительных материалов "
            "(мешки, паллеты, упаковки — что видно в кадре). Дай краткую (2-3 предложения, по-русски) "
            "оценку расхода материала. Это ПРЕДЛОЖЕНИЕ для владельца на проверку, не финальная цифра — "
            "явно укажи, что оценка приблизительная по фото.",
            session['start_photos'] + session['finish_photos'],
            "Оцени примерный расход материала по этим фото (начало и конец смены).",
        )
        _save_checkin_analysis(session_id, 'materials', result)
        return {"analysis": result, "note": "Предложение на проверку владельцем — бюджет объекта не изменён автоматически."}

    @router.post("/api/checkin/{session_id}/analyze-defects")
    def analyze_checkin_defects(session_id: str, user: dict = Depends(deps.get_current_user), role: str = Depends(deps.get_role)):
        session = _get_checkin_session(session_id, user['id'], role)
        deps.check_ai_rate(user['id'])
        result = _call_glm_vision(
            "Ты — инспектор качества на стройке. Смотришь на фото участка работ и ищешь видимые дефекты: "
            "трещины, протечки, неровности, брак. Ответь СТРОГО в формате: первая строка 'ДЕФЕКТ: да' или "
            "'ДЕФЕКТ: нет', затем если да — короткое описание дефекта на русском (1-2 предложения).",
            session['finish_photos'],
            "Есть ли видимые дефекты на этом фото?",
        )
        has_defect = result.strip().upper().startswith('ДЕФЕКТ: ДА') or result.strip().upper().startswith('ДЕФЕКТ:ДА')
        ticket = None
        if has_defect:
            description = result.split('\n', 1)[1].strip() if '\n' in result else 'Дефект обнаружен AI-анализом'
            ticket = deps.create_mangel_ticket(
                object_id=session['object_id'],
                description=description[:500],
                created_by=str(user['id']),
                photo_paths=session['finish_photos'][:1],
                created_by_ai=True,
            )
        _save_checkin_analysis(session_id, 'defects', result)
        return {"analysis": result, "ticket_created": ticket}

    handlers = SimpleNamespace(
        checkin_manual=checkin_manual,
        analyze_checkin_progress=analyze_checkin_progress,
        analyze_checkin_materials=analyze_checkin_materials,
        analyze_checkin_defects=analyze_checkin_defects,
        validate_manual_time_body=_validate_manual_time_body,
        assert_no_manual_time_overlap=_assert_no_manual_time_overlap,
        object_id_exists=_object_id_exists,
        parse_manual_date=_parse_manual_date,
        parse_manual_hhmm=_parse_manual_hhmm,
        get_checkin_session=_get_checkin_session,
        save_checkin_analysis=_save_checkin_analysis,
        image_block_from_file=_image_block_from_file,
        call_glm_vision=_call_glm_vision,
    )
    return router, handlers
