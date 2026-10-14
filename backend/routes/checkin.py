"""Checkin shift-lifecycle HTTP routes (start/pause/finish/list/export/
finish-context/photo). Manual time entry and AI photo analysis live in
routes/execution.py -- different coupling profile (no lock/idempotency/
outbox involvement), split out per the Execution dependency-map decision.

This module deliberately does not import backend.main. main.py owns the
shared runtime state (checkin_meta store, the checkin lock, the durable
idempotency layer, the finish outbox, the feed-photo subsystem) and wires
it through CheckinRouteDeps, then includes the returned router and
re-exports the handlers for legacy direct-call tests.

Per the explicit architecture decision for this extraction: shared checkin
state / locks / idempotency / outbox are NOT being moved into core/* in this
pass -- that is a separate, higher-risk refactor. They stay canonically in
main.py and are injected here via deps, exactly like objects.py/stages.py/
daily_plan.py already do for _load_checkin_meta and
_is_active_photo_checkin_session.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Callable
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response

logger = logging.getLogger(__name__)

try:
    from .. import daily_plan_lib as dpl
    from ..core.media_paths import resolve_owned_media_path
    from ..services.checkin_access import CheckinAccessDeps, CheckinAccessError, CheckinAccessService
except ImportError:
    import daily_plan_lib as dpl  # noqa: E402
    from core.media_paths import resolve_owned_media_path  # noqa: E402
    from services.checkin_access import CheckinAccessDeps, CheckinAccessError, CheckinAccessService  # noqa: E402


@dataclass(frozen=True)
class CheckinRouteDeps:
    get_current_user: Callable
    get_role: Callable
    checkin_photo_base: Callable
    checkin_lock: object
    finish_outbox_lock: object
    load_checkin_meta: Callable
    save_checkin_meta: Callable
    save_checkin_photos: Callable
    cleanup_checkin_photo_files: Callable
    checkin_file_count: Callable
    is_active_photo_checkin_session: Callable
    gps_suspect: Callable
    can_access_object: Callable
    get_active_assignment_for_checkin: Callable
    idempotency_scope: Callable
    idempotency_claim: Callable
    idempotency_save: Callable
    idempotency_release: Callable
    idempotent_entity_id: Callable
    load_worker_profiles: Callable
    sanitize_display_name: Callable
    cached_get_used_range: Callable
    upsert_checkin_feed_post: Callable
    business_now: Callable
    business_today_str: Callable
    outbox_write_pending: Callable
    outbox_mark_applied: Callable
    outbox_mark_failed: Callable
    outbox_mark_dead_letter: Callable
    clear_pending_execution_report: Callable
    reject_pending_execution_report: Callable
    write_zeiterfassung_row: Callable
    object_history_worker_name: Callable
    append_object_history_best_effort: Callable
    extra_works_summary_text: Callable
    load_roles: Callable
    send_telegram_message: Callable
    photo_pause_minutes: Callable
    hours_from_session: Callable
    get_worker_profile: Callable
    csv_safe: Callable


def create_checkin_router(deps: CheckinRouteDeps):
    router = APIRouter()
    access_service = CheckinAccessService(CheckinAccessDeps(
        checkin_lock=deps.checkin_lock,
        load_meta=deps.load_checkin_meta,
        save_meta=deps.save_checkin_meta,
        photo_base=deps.checkin_photo_base,
        resolve_media_path=resolve_owned_media_path,
        now_timestamp=lambda: int(time.time()),
    ))

    def call_access(method, *args, **kwargs):
        try:
            return method(*args, **kwargs)
        except CheckinAccessError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc

    def _save_idempotency_response_after_commit(
        key: str, response: dict, scope: dict, *, operation: str
    ) -> None:
        """Persist replay metadata without undoing an already-durable shift.

        checkin_meta (and finish outbox where applicable) is the primary business
        state. A failure in the secondary idempotency response store after that
        commit must not turn a successful Start/Finish into a false 500. Retries
        remain safe because session ids are deterministic for an idempotency scope.
        """
        try:
            deps.idempotency_save(key, response, scope)
        except Exception:
            logger.exception(
                'Check-in %s idempotency response persistence failed after durable commit: key=%s',
                operation,
                key,
            )

    CHECKIN_EVENT_MAX_AGE_SECONDS = 7 * 24 * 3600
    CHECKIN_EVENT_FUTURE_SKEW_SECONDS = 5 * 60

    def _parse_checkin_occurred_at(raw: str, field_name: str, received_at: int) -> int:
        """Client action time for offline-safe checkin events.

        geo_timestamp is GPS metadata, not the business event timestamp. Start/Finish
        use occurred_at when the client supplies it, and persist received_at separately
        so delayed outbox sync does not turn into fake work hours."""
        value = str(raw or '').strip()
        if not value:
            return received_at
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            raise HTTPException(400, f"{field_name}: некорректное время события")
        if parsed > 10_000_000_000:
            parsed = parsed / 1000
        occurred_at = int(parsed)
        if occurred_at <= 0:
            raise HTTPException(400, f"{field_name}: некорректное время события")
        if occurred_at > received_at + CHECKIN_EVENT_FUTURE_SKEW_SECONDS:
            raise HTTPException(400, f"{field_name}: время события не может быть в будущем")
        if occurred_at < received_at - CHECKIN_EVENT_MAX_AGE_SECONDS:
            raise HTTPException(400, f"{field_name}: время события слишком старое")
        return occurred_at

    def _checkin_business_date_from_timestamp(ts: int) -> str:
        return datetime.fromtimestamp(ts, deps.business_now().tzinfo).strftime('%Y-%m-%d')

    def _build_finish_context(session: dict) -> dict:
        """Shared shape-builder for the frozen accepted-plan snapshot shown/submitted
        at Finish. 21.09 (owner review finding, round 3): extracted out of
        checkin_finish_context() so checkin_start() can embed the SAME shape in its
        own response -- see the finish_context field there. Before this, a plan-
        linked Start followed immediately by an offline stretch (network drops
        before Finish is opened even once) had nothing to fall back to: the
        frontend's own prefetch (_prefetchFinishContextAfterStart) is a second,
        best-effort network round-trip that can simply fail to complete before
        connectivity is lost. Embedding this directly in the Start response makes
        the offline cache deterministic -- no second round-trip needed at all."""
        session_id = session.get('id') or ''
        plan_id = session.get('daily_plan_id') or ''
        if not plan_id:
            return {
                "session_id": session_id,
                "object_id": session.get("object_id") or "",
                "has_plan": False,
            }

        store = dpl.get_store_snapshot()
        worker_id = str(session.get('user_id'))
        acceptance_id = session.get('daily_plan_acceptance_id') or ''
        acceptance = store["acceptances"].get(acceptance_id) if acceptance_id else None
        if not acceptance:
            acceptance = next(
                (a for a in store["acceptances"].values()
                 if a.get("daily_plan_id") == plan_id and str(a.get("worker_id")) == worker_id),
                None,
            )
        # 18.09 (audit finding, merged from upstream 170bf24): a session referencing
        # a plan/acceptance that no longer resolves cleanly (deleted plan, acceptance
        # id drift) must not turn Finish into a hard error -- same "not a plan-linked
        # shift, carry on" fallback checkin_start already uses elsewhere. Was a 409
        # here, which could block a worker from finishing their shift at all.
        plan = store["daily_plans"].get(plan_id) if acceptance else None
        if not acceptance or not plan:
            return {
                "session_id": session_id,
                "object_id": session.get("object_id") or "",
                "has_plan": False,
            }

        snapshot = acceptance.get("accepted_context_snapshot") or {}
        # 21.09 (owner review finding, round 4): pass the EXACT acceptance already
        # resolved above -- without acceptance_id, get_accepted_snapshot() picked
        # the first (effectively oldest) acceptance for (plan_id, worker_id),
        # which after an amendment (accept v1 -> A1, amend -> v2, accept v2 -> A2)
        # could return v1's items while this response correctly reports
        # plan.version=2 from A2 above -- a real content/version mismatch in what
        # Finish shows and submits as plan-fact.
        items_snapshot = dpl.get_accepted_snapshot(plan_id, worker_id, acceptance_id=acceptance.get("id"))
        return {
            "session_id": session_id,
            "object_id": session.get("object_id") or "",
            "has_plan": True,
            "plan": {
                "id": plan_id,
                "version": acceptance.get("plan_version") or snapshot.get("plan_version") or plan.get("version") or 0,
                "object_id": snapshot.get("object_id") or plan.get("object_id") or "",
                "date": snapshot.get("date") or plan.get("date") or "",
                "stage_key": snapshot.get("stage_key") or plan.get("stage_key") or "",
                "items": items_snapshot,
            },
            "acceptance": {
                "id": acceptance.get("id") or "",
                "plan_version": acceptance.get("plan_version") or snapshot.get("plan_version") or 0,
                "accepted_at": acceptance.get("accepted_at"),
            },
        }

    @router.post("/api/checkin/start")
    async def checkin_start(
        object_id: str = Form(''),
        lat: str = Form(''),
        lon: str = Form(''),
        accuracy: str = Form(''),
        geo_timestamp: str = Form(''),
        occurred_at: str = Form(''),
        stage_name: str = Form(''),
        files: list[UploadFile] = File(default=[]),
        daily_plan_id: str = Form(''),
        daily_plan_version: str = Form(''),
        daily_plan_acceptance_id: str = Form(''),
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
        idempotency_key: str = Header(default='', alias='Idempotency-Key'),
    ):
        if not object_id.strip():
            raise HTTPException(400, "object_id обязателен")
        if not lat.strip() or not lon.strip():
            raise HTTPException(400, "Включи геолокацию, чтобы начать смену")
        accuracy_clean = accuracy.strip()[:50] if isinstance(accuracy, str) else ''
        geo_timestamp_clean = geo_timestamp.strip()[:50] if isinstance(geo_timestamp, str) else ''
        occurred_at_clean = occurred_at.strip()[:50] if isinstance(occurred_at, str) else ''
        start_received_at = int(time.time())
        start_event_at = _parse_checkin_occurred_at(occurred_at_clean, 'occurred_at', start_received_at)
        # 03.08: Europe/Berlin, не UTC сервера -- проверка периода назначения
        # (_get_active_assignment_for_checkin ниже) должна сверяться с той же датой, что
        # реально "сегодня" по местному времени, иначе вечером/ночью Berlin worker мог бы
        # получить доступ на день раньше/позже реального начала/конца периода.
        # F04: для offline-синхронизации дата берётся от времени события, а не доставки.
        date_str = _checkin_business_date_from_timestamp(start_event_at)

        # 30.07 (аудит п.5): единая проверка ДО сохранения фото/создания сессии -- нельзя
        # сначала записать файлы, а потом вернуть 403. Owner не назначается вообще
        # (can_access_object пропускает owner безусловно), для него assignment_id пуст.
        assignment_id = ''
        if role == 'owner':
            if not deps.can_access_object(user, role, object_id.strip()):
                raise HTTPException(403, "Нет доступа к этому объекту")
        else:
            assignment_id = deps.get_active_assignment_for_checkin(str(user['id']), object_id.strip(), date_str)

        # Server-trust validation for optional DailyPlan linkage at Start
        _dp_id_clean = daily_plan_id.strip()
        _dp_session_plan_id = None
        _dp_session_plan_version = None
        _dp_session_acceptance_id = None
        if _dp_id_clean:
            _dp_at_start = dpl.get_plan(_dp_id_clean)
            if not _dp_at_start:
                raise HTTPException(400, f"daily_plan_id {_dp_id_clean!r} не найден")
            _wid_str = str(user['id'])
            if _wid_str not in [str(w) for w in _dp_at_start.get('assigned_worker_ids', [])]:
                raise HTTPException(403, "Этот план не назначен вам")
            if _dp_at_start.get('object_id') != object_id.strip():
                raise HTTPException(400, "daily_plan_id объект не совпадает с объектом смены")
            if _dp_at_start.get('date') != date_str:
                raise HTTPException(400, "daily_plan_id дата не совпадает с сегодняшней датой")
            _dp_acc_clean = daily_plan_acceptance_id.strip()
            if not _dp_acc_clean:
                raise HTTPException(400, "Сначала примите план дня")
            _dp_store = dpl.get_store_snapshot()
            _acc = _dp_store["acceptances"].get(_dp_acc_clean)
            if not _acc:
                raise HTTPException(400, f"acceptance_id {_dp_acc_clean!r} не найден")
            if str(_acc.get("worker_id")) != _wid_str:
                raise HTTPException(403, "acceptance_id принадлежит другому работнику")
            if _acc.get("daily_plan_id") != _dp_id_clean:
                raise HTTPException(400, "acceptance_id принадлежит другому плану")
            _snapshot = _acc.get("accepted_context_snapshot") or {}
            if _snapshot:
                if _wid_str not in [str(w) for w in _snapshot.get("assigned_worker_ids", [])]:
                    raise HTTPException(403, "Вы не назначены на принятую версию плана")
                if _snapshot.get("object_id") != object_id.strip():
                    raise HTTPException(400, "Принятый план относится к другому объекту")
                if _snapshot.get("date") != date_str:
                    raise HTTPException(400, "Принятый план относится к другой дате")
            _dp_session_plan_id = _dp_id_clean
            _dp_session_plan_version = str(_acc.get("plan_version") or _snapshot.get("plan_version") or _dp_at_start.get("version") or '')
            _dp_session_acceptance_id = _dp_acc_clean

        idempotency_scope = deps.idempotency_scope('checkin_start', user['id'], object_id.strip(), {
            "object_id": object_id.strip(),
            "lat": lat.strip(),
            "lon": lon.strip(),
            "accuracy": accuracy_clean,
            "geo_timestamp": geo_timestamp_clean,
            "occurred_at": occurred_at_clean,
            "stage_name": (stage_name.strip()[:200] if isinstance(stage_name, str) else ''),
            "daily_plan_id": _dp_session_plan_id or '',
            "daily_plan_version": _dp_session_plan_version or '',
            "daily_plan_acceptance_id": _dp_session_acceptance_id or '',
            "file_count": deps.checkin_file_count(files),
        })
        cached = deps.idempotency_claim(idempotency_key, idempotency_scope)
        if cached is not None:
            return cached
        entity_id = deps.idempotent_entity_id(idempotency_key, idempotency_scope)

        with deps.checkin_lock:
            # 10.29 (Fable-аудит): раньше можно было создать сколько угодно параллельных
            # "стартов" смены — часы потом считались некорректно.
            existing = deps.load_checkin_meta()
            open_session = next((i for i in existing
                                  if str(i.get('user_id')) == str(user['id']) and deps.is_active_photo_checkin_session(i)), None)
            if open_session and open_session.get('id') != entity_id:
                deps.idempotency_release(idempotency_key, idempotency_scope)
                raise HTTPException(409, f"У вас уже есть незавершённая смена на объекте {open_session['object_id']} — сначала завершите её")

        photo_paths = await deps.save_checkin_photos(files, object_id.strip()[:100], date_str)
        # 17.09 (audit finding, P0): frontend requires >=1 start photo before allowing
        # Start, but backend accepted the request unconditionally regardless of how
        # many photos actually made it through _save_checkin_photos (0 if all files
        # failed the size/sniff check, or the client sent none at all) -- an old or
        # broken client could start a shift with zero evidence photos. Same pattern
        # Finish already uses (see the len(photo_paths) < 2 check below in
        # checkin_finish): reject BEFORE creating the session, clean up any files
        # that did save from this failed attempt so they don't become orphans.
        if len(photo_paths) < 1:
            deps.cleanup_checkin_photo_files(photo_paths)
            deps.idempotency_release(idempotency_key, idempotency_scope)
            raise HTTPException(400, "Для начала смены нужно минимум одно корректное фото")

        entry = {
            "id": entity_id,
            "object_id": object_id.strip()[:100],
            "assignment_id": assignment_id,
            "date": date_str,
            "user_id": user['id'],
            "start_at": start_event_at,
            "start_received_at": start_received_at,
            "start_occurred_at_source": 'client' if occurred_at_clean else 'server',
            "start_photos": photo_paths,
            "start_lat": lat,
            "start_lon": lon,
            "start_accuracy": accuracy_clean or None,
            "start_geo_timestamp": geo_timestamp_clean or None,
            "start_gps_suspect": deps.gps_suspect(lat, lon),
            "stage_name": (stage_name.strip()[:200] if isinstance(stage_name, str) else '') or None,
            "daily_plan_id": _dp_session_plan_id,
            "daily_plan_version": _dp_session_plan_version,
            "daily_plan_acceptance_id": _dp_session_acceptance_id,
            "finish_at": None,
            "finish_photos": [],
            "finish_lat": None,
            "finish_lon": None,
            "finish_accuracy": None,
            "finish_geo_timestamp": None,
            "finish_gps_suspect": None,
            "pause_started_at": None,
            "pause_accumulated_seconds": 0,
        }
        with deps.checkin_lock:
            items = deps.load_checkin_meta()
            # F03: crash-recovered retry -- same Idempotency-Key derives the same entity_id,
            # so if an earlier attempt already wrote this exact session (and then died before
            # _idempotency_save() ran), reuse it instead of creating a second open session.
            existing_entry = next((i for i in items if i.get('id') == entity_id), None)
            if existing_entry is not None:
                deps.cleanup_checkin_photo_files(photo_paths)
                entry = existing_entry
            else:
                # Повторная проверка внутри финального лока — на случай гонки между двумя
                # параллельными checkin_start запросами (TOCTOU между первой проверкой и этой записью).
                open_session = next((i for i in items
                                      if str(i.get('user_id')) == str(user['id']) and deps.is_active_photo_checkin_session(i)), None)
                if open_session:
                    deps.cleanup_checkin_photo_files(photo_paths)
                    deps.idempotency_release(idempotency_key, idempotency_scope)
                    raise HTTPException(409, f"У вас уже есть незавершённая смена на объекте {open_session['object_id']} — сначала завершите её")
                items.append(entry)
                try:
                    deps.save_checkin_meta(items)
                except Exception:
                    # Photo files are written before the metadata commit so async
                    # file I/O stays outside checkin_lock. Atomic JSON persistence
                    # has no fallible step after os.replace; therefore an exception
                    # here means the new session was not committed and these files
                    # are safe to remove.
                    deps.cleanup_checkin_photo_files(photo_paths)
                    deps.idempotency_release(idempotency_key, idempotency_scope)
                    raise

        if existing_entry is None and photo_paths:
            try:
                profiles = deps.load_worker_profiles()
                worker_name = deps.sanitize_display_name(profiles.get(str(user['id']), {}).get('name'), str(user['id']))
                rows = deps.cached_get_used_range('Объекты')
                object_name = entry['object_id']
                if rows:
                    header, data = rows[0], rows[1:]
                    for r in data:
                        obj = dict(zip(header, r))
                        if str(obj.get('ID объекта', '')) == entry['object_id']:
                            object_name = obj.get('Объект', entry['object_id'])
                            break
                deps.upsert_checkin_feed_post(entry, 'start', object_name, user['id'], worker_name)
            except Exception as e:
                print(f'WARNING: checkin-start feed post failed: {e}')

        # 21.09 (owner review finding, round 3): embed the SAME frozen accepted-plan
        # snapshot GET /finish-context returns, right in the Start response -- a
        # deterministic, zero-extra-round-trip alternative to the frontend's
        # best-effort prefetch (_prefetchFinishContextAfterStart), which can fail to
        # complete if connectivity drops between Start succeeding and that second
        # request landing. A copy, not a mutation of `entry` -- this is a response-
        # only field, not part of what gets persisted to checkin_meta.json.
        response = dict(entry)
        try:
            response['finish_context'] = _build_finish_context(entry)
        except Exception as e:
            print(f'WARNING: checkin-start finish-context prefetch failed: {e}')
            response['finish_context'] = None

        _save_idempotency_response_after_commit(
            idempotency_key, response, idempotency_scope, operation='start'
        )
        return response

    @router.post("/api/checkin/{session_id}/pause")
    def checkin_pause(session_id: str, action: str = Query(''), user: dict = Depends(deps.get_current_user), role: str = Depends(deps.get_role)):
        return call_access(access_service.pause_session, session_id, action, str(user['id']), role)


    @router.post("/api/checkin/{session_id}/finish")
    async def checkin_finish(
        session_id: str,
        lat: str = Form(''),
        lon: str = Form(''),
        accuracy: str = Form(''),
        geo_timestamp: str = Form(''),
        occurred_at: str = Form(''),
        done_summary: str = Form(''),
        extra_work: str = Form(''),
        extra_works: str = Form(''),
        needs: str = Form(''),
        defects: str = Form(''),
        next_day_needs: str = Form(''),
        pause_minutes: int = Form(0),
        voice_note_file_id: str = Form(''),
        daily_plan_report: str = Form(''),
        files: list[UploadFile] = File(default=[]),
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
        idempotency_key: str = Header(default='', alias='Idempotency-Key'),
    ):
        """27.07 (B3, finish-shift wizard): extra_works/needs/defects — JSON-массивы
        структурированных пунктов из wizard-шагов 3-4 (описание/зона/время/согласование
        для доп-работ; категория+текст для потребностей/дефектов). extra_work (str) --
        старое одиночное текстовое поле, оставлено для обратной совместимости с
        checkin_manual и старым фронтендом, если он ещё где-то шлёт этот формат;
        новый wizard шлёт extra_works, extra_work остаётся пустым. Ни Need ни Mangel
        не создаются автоматически -- это только сохраняет данные в сессию, реальное
        создание тикетов делает отдельный подтверждающий вызов с фронтенда после
        показа сводки юзеру (см. wizard Step 6)."""
        def _parse_json_list(raw: str, field_name: str) -> list:
            if not raw.strip():
                return []
            try:
                parsed = json.loads(raw)
            except Exception:
                raise HTTPException(400, f"{field_name}: некорректный JSON")
            if not isinstance(parsed, list):
                raise HTTPException(400, f"{field_name}: ожидался список")
            return parsed

        extra_works_list = _parse_json_list(extra_works, 'extra_works')
        needs_list = _parse_json_list(needs, 'needs')
        defects_list = _parse_json_list(defects, 'defects')

        if not lat.strip() or not lon.strip():
            raise HTTPException(400, "Включи геолокацию, чтобы завершить смену")
        if not done_summary.strip():
            raise HTTPException(400, "Заполни короткий отчёт: что сделано за смену")
        accuracy_clean = accuracy.strip()[:50] if isinstance(accuracy, str) else ''
        geo_timestamp_clean = geo_timestamp.strip()[:50] if isinstance(geo_timestamp, str) else ''
        occurred_at_clean = occurred_at.strip()[:50] if isinstance(occurred_at, str) else ''
        finish_received_at = int(time.time())
        finish_event_at = _parse_checkin_occurred_at(occurred_at_clean, 'occurred_at', finish_received_at)

        with deps.checkin_lock:
            items = deps.load_checkin_meta()
            session = next((i for i in items if i.get('id') == session_id), None)
            if not session:
                raise HTTPException(404, "Сессия check-in не найдена")
            if role != 'owner' and str(session.get('user_id')) != str(user['id']):
                raise HTTPException(403, "Нельзя завершить чужую смену")
            if session.get('manual_entry'):
                raise HTTPException(400, "Ручная запись времени не завершается как фото-смена")
            if session.get('start_at') and finish_event_at < int(session.get('start_at')):
                raise HTTPException(400, "Время завершения не может быть раньше старта смены")
            object_id, date_str = session['object_id'], session['date']
            idempotency_scope = deps.idempotency_scope('checkin_finish', user['id'], session_id, {
                "session_id": session_id,
                "object_id": object_id,
                "lat": lat.strip(),
                "lon": lon.strip(),
                "accuracy": accuracy_clean,
                "geo_timestamp": geo_timestamp_clean,
                "occurred_at": occurred_at_clean,
                "done_summary": done_summary.strip()[:1000],
                "extra_work": extra_work.strip()[:1000],
                "extra_works": extra_works_list,
                "needs": needs_list,
                "defects": defects_list,
                "next_day_needs": next_day_needs.strip()[:1000],
                "voice_note_file_id": os.path.basename(voice_note_file_id.strip()) if voice_note_file_id.strip() else '',
                "daily_plan_report": daily_plan_report.strip(),
                "file_count": deps.checkin_file_count(files),
            })
            cached = deps.idempotency_claim(idempotency_key, idempotency_scope)
            if cached is not None:
                return cached
            if session['finish_at'] is not None:
                # F03 (owner review commit db584ac, CHANGES REQUIRED): crash window --
                # the earlier attempt's business write (finish_at + session fields,
                # committed a few lines below in the second _checkin_lock block) can
                # succeed while the process dies before _idempotency_save() runs. A
                # bare retry with the SAME Idempotency-Key then found no cached
                # response above (claim/entry pruned or never written) and landed
                # here, where the old code unconditionally raised "already finished"
                # instead of returning the original success. Since this session
                # matches the idempotency scope's own session_id, replaying it IS the
                # original result -- self-heal the durable store instead of erasing
                # the caller's ability to see what actually happened.
                if idempotency_key:
                    _save_idempotency_response_after_commit(
                        idempotency_key, session, idempotency_scope, operation='finish'
                    )
                    return session
                deps.idempotency_release(idempotency_key, idempotency_scope)
                raise HTTPException(400, "Смена уже завершена")

        if deps.checkin_file_count(files) < 2:
            deps.idempotency_release(idempotency_key, idempotency_scope)
            raise HTTPException(400, "Прикрепите минимум 2 фото выполненной работы")

        # Сохранение фото (I/O, await) — вне лока, как и в checkin_start. Раньше await стоял
        # внутри with _checkin_lock: — второй параллельный check-in-запрос (частый сценарий,
        # два работника жмут "Финиш" в конце смены одновременно) блокировал event loop навсегда.
        #
        # 03.08 (реальный найденный баг): len(files) < 2 проверяет ПЕРЕДАННОЕ количество,
        # не РЕАЛЬНО СОХРАНЁННОЕ -- _save_checkin_photos() молча пропускает (continue)
        # битые/слишком большие/не-изображение файлы, ничего не бросая. Раньше finish_at
        # мог записаться после отправки 2 файлов, из которых 0-1 реально сохранились.
        # Теперь: сохраняем, считаем РЕАЛЬНО сохранённые, при недостатке -- удаляем файлы
        # ИМЕННО этого неуспешного запроса (не трогаем фото прошлых успешных попыток) и
        # 400 ДО захвата _checkin_lock -- session['finish_at'] не пишется вообще.
        photo_paths = await deps.save_checkin_photos(files, object_id, date_str)
        if len(photo_paths) < 2:
            deps.cleanup_checkin_photo_files(photo_paths)
            deps.idempotency_release(idempotency_key, idempotency_scope)
            raise HTTPException(400, "Для завершения смены необходимо минимум 2 корректных фото")

        with deps.checkin_lock:
            items = deps.load_checkin_meta()
            session = next((i for i in items if i.get('id') == session_id), None)
            if not session:
                deps.cleanup_checkin_photo_files(photo_paths)
                deps.idempotency_release(idempotency_key, idempotency_scope)
                raise HTTPException(404, "Сессия check-in не найдена")
            if session['finish_at'] is not None:
                deps.cleanup_checkin_photo_files(photo_paths)
                # F03: same self-heal as the earlier check above -- a concurrent/retried
                # attempt that lost this race already got its own finish committed by
                # the other one; replay that result instead of erasing it.
                if idempotency_key:
                    _save_idempotency_response_after_commit(
                        idempotency_key, session, idempotency_scope, operation='finish'
                    )
                    return session
                deps.idempotency_release(idempotency_key, idempotency_scope)
                raise HTTPException(400, "Смена уже завершена")
            if session.get('start_at') and finish_event_at < int(session.get('start_at')):
                deps.cleanup_checkin_photo_files(photo_paths)
                deps.idempotency_release(idempotency_key, idempotency_scope)
                raise HTTPException(400, "Время завершения не может быть раньше старта смены")
            session['finish_at'] = finish_event_at
            session['finish_received_at'] = finish_received_at
            session['finish_occurred_at_source'] = 'client' if occurred_at_clean else 'server'
            session['finish_photos'] = photo_paths
            session['finish_lat'] = lat
            session['finish_lon'] = lon
            session['finish_accuracy'] = accuracy_clean or None
            session['finish_geo_timestamp'] = geo_timestamp_clean or None
            session['finish_gps_suspect'] = deps.gps_suspect(lat, lon)
            # 12.09: короткий отчёт по смене обязателен и на frontend, и на backend.
            # Остальные блоки опциональны: доп-работы, потребности, дефекты, завтра.
            session['done_summary'] = done_summary.strip()[:1000] or None
            session['extra_work'] = extra_work.strip()[:1000] or None
            session['extra_works'] = extra_works_list or None
            session['needs'] = needs_list or None
            session['defects'] = defects_list or None
            session['next_day_needs'] = next_day_needs.strip()[:1000] or None
            # 28.07: owner request -- голосовое "что сделано" должно быть прослушиваемо
            # владельцем, не только видно транскриптом. file_id уже создан/сохранён
            # раньше через /api/transcribe (тот же поток что и для распознавания) --
            # тут просто привязываем его к сессии смены. os.path.basename на всякий
            # случай -- та же защита от path traversal, что и в get_transcribe_audio.
            vnf = os.path.basename(voice_note_file_id.strip()) if voice_note_file_id.strip() else ''
            session['voice_note_file_id'] = vnf or None
            # 24.07: если воркер финиширует смену прямо во время активной паузы (забыл нажать
            # "Продолжить") — закрываем её здесь же, не оставляем pause_started_at висеть
            # в завершённой сессии.
            if session.get('pause_started_at'):
                elapsed = max(0, finish_event_at - session['pause_started_at'])
                session['pause_accumulated_seconds'] = session.get('pause_accumulated_seconds', 0) + elapsed
                session['pause_started_at'] = None
            # 20.09 (P0, found by audit, hardened further during merge): the client
            # pause_minutes Form param must never be trusted directly -- a worker
            # (or a stale localStorage session missing pauseAccumulatedSeconds, see
            # worker-checkin-fab.js's refreshCheckinButtons wrapper) can send 0 and
            # silently erase a real 45-minute pause from payroll. The server has
            # just closed any hanging pause above and _photo_pause_minutes() reads
            # pause_accumulated_seconds unconditionally when present -- the client
            # value is never consulted, not even as a max() floor, so it can't be
            # used to inflate paid hours either.
            session['pause_minutes'] = deps.photo_pause_minutes(session)
            # P0 fix (owner review): persist the raw execution report INSIDE the same
            # checkin_meta write that commits finish_at -- previously daily_plan_report
            # only existed as a request Form parameter, never durably stored anywhere
            # until the outbox write a few lines below. A crash between this save and
            # that outbox write meant the report was permanently lost: finish_at=true,
            # photos saved, but no outbox event and no way to reconstruct item_results
            # from checkin_meta.json alone (there was nothing to reconstruct FROM).
            # Now it's part of this one atomic write -- reconciliation on startup can
            # find a finished session with pending_execution_report set and no
            # outbox event, and rebuild the outbox entry from it.
            session['pending_execution_report'] = daily_plan_report.strip() or None
            try:
                deps.save_checkin_meta(items)
            except Exception:
                # Same ordering as Start: finish photos exist before the atomic
                # metadata write. If that write fails, finish_at was not committed,
                # so retain no unreferenced binaries and release the claim for retry.
                deps.cleanup_checkin_photo_files(photo_paths)
                deps.idempotency_release(idempotency_key, idempotency_scope)
                raise

        # Apply daily plan execution report via durable outbox.
        # Outbox event is written AFTER checkin_meta is committed and BEFORE apply_daily_execution,
        # so a crash between these two leaves a pending event retried on startup.
        if daily_plan_report.strip():
            try:
                rpt = json.loads(daily_plan_report)
                plan_id = rpt.get('plan_id', '') or session.get('daily_plan_id', '')
                plan_ver = int(rpt.get('plan_version', 0) or session.get('daily_plan_version', 0) or 0)
                item_results = rpt.get('item_results') or []
                if plan_id and isinstance(item_results, list) and item_results:
                    # Round 1.2 #2 + P0 integrity pass: validate against the worker's
                    # IMMUTABLE accepted context snapshot, not the live (possibly
                    # since-mutated-by-Sheets-edit) plan fields. A later
                    # update_plan_fields() call (object/date/worker/stage change) must
                    # never invalidate an already-accepted, already-started shift's
                    # Finish -- the accepted snapshot is the trusted historical truth
                    # for this comparison, not current plan state. This pre-check is a
                    # fast local reject (avoids an outbox write for an obviously-bad
                    # report); dpl.apply_daily_execution() below re-validates the exact
                    # same rules as defense-in-depth (P0-3) -- both this route AND
                    # startup reconciliation ultimately share that one validator, so a
                    # report rejected here can never be resurrected by a restart.
                    _worker_id_str = str(session['user_id'])
                    _acceptance_id = session.get('daily_plan_acceptance_id') or ''
                    _snapshot = None
                    if _acceptance_id:
                        _dp_store = dpl.get_store_snapshot()
                        _acc = _dp_store["acceptances"].get(_acceptance_id)
                        if _acc and str(_acc.get('worker_id')) == _worker_id_str and _acc.get('daily_plan_id') == plan_id:
                            _snapshot = _acc.get('accepted_context_snapshot')
                    _reject_reason = None
                    if _snapshot:
                        # Trusted path: compare against what THIS worker actually accepted.
                        if _worker_id_str not in [str(w) for w in _snapshot.get('assigned_worker_ids', [])]:
                            _reject_reason = f'finish plan_id {plan_id} worker not in accepted snapshot'
                        elif _snapshot.get('object_id') != object_id:
                            _reject_reason = f'finish plan_id {plan_id} object mismatch vs accepted snapshot'
                        elif _snapshot.get('date') != date_str:
                            _reject_reason = f'finish plan_id {plan_id} date mismatch vs accepted snapshot'
                    else:
                        # Backward-compat fallback: no acceptance_id on this session, or
                        # the acceptance predates round 1.2 (no snapshot stored yet).
                        # Conservative choice: fall back to the live plan fields exactly
                        # as before -- cannot fabricate a historical snapshot that was
                        # never recorded. This preserves pre-1.2 behavior for old sessions
                        # instead of silently skipping execution for them.
                        _plan_obj = dpl.get_plan(plan_id)
                        if _plan_obj:
                            if _worker_id_str not in [str(w) for w in _plan_obj.get('assigned_worker_ids', [])]:
                                _reject_reason = f'finish plan_id {plan_id} worker not assigned (legacy path)'
                            elif _plan_obj.get('object_id') != object_id:
                                _reject_reason = f'finish plan_id {plan_id} object mismatch (legacy path)'
                            elif _plan_obj.get('date') != date_str:
                                _reject_reason = f'finish plan_id {plan_id} date mismatch (legacy path)'
                    if _reject_reason:
                        print(f'WARNING: {_reject_reason} — skipping execution')
                        # P0 fix: a rejected report must not sit in
                        # pending_execution_report where a later restart's
                        # reconciliation could resurrect exactly what was just
                        # rejected here. Records the reason for owner diagnosis
                        # instead of silently deleting it.
                        deps.reject_pending_execution_report(session_id, _reject_reason)
                        plan_id = ''
                    if plan_id:
                        # Write outbox event before applying (durable, crash-safe).
                        # acceptance_id travels with the event so a startup retry of
                        # THIS SAME event re-validates against the identical trusted
                        # snapshot, not just the live plan.
                        deps.outbox_write_pending(session_id, plan_id, plan_ver,
                                              str(session['user_id']), date_str, object_id, item_results,
                                              acceptance_id=(_acceptance_id or None))
                        try:
                            dpl.apply_daily_execution(
                                session_id=session_id,
                                daily_plan_id=plan_id,
                                plan_version=plan_ver,
                                worker_id=str(session['user_id']),
                                date_str=date_str,
                                object_id=object_id,
                                item_results=item_results,
                                acceptance_id=(_acceptance_id or None),
                            )
                            deps.outbox_mark_applied(session_id)
                            deps.clear_pending_execution_report(session_id)
                            # Auto-record productivity observations from execution
                            try:
                                plan_obj = dpl.get_plan(plan_id)
                                shift_hours = max(0.0, (session['finish_at'] - session.get('start_at', session['finish_at'])) / 3600.0)
                                if plan_obj and shift_hours > 0:
                                    dpl.auto_record_execution_productivity(
                                        session_id=session_id,
                                        worker_id=str(session['user_id']),
                                        plan=plan_obj,
                                        item_results=item_results,
                                        shift_hours=shift_hours,
                                    )
                            except Exception as _pe:
                                print(f'WARNING: auto_record_execution_productivity failed: {_pe}')
                        except dpl.ExecutionValidationError as _ve:
                            # Defense-in-depth caught something the pre-check above
                            # missed (or a race changed state between the two) -- same
                            # treatment as a pre-check rejection: never silently apply,
                            # never silently drop, dead-letter for owner review.
                            print(f'WARNING: apply_daily_execution rejected by validator: {_ve}')
                            deps.outbox_mark_dead_letter(session_id, f'validation rejected: {_ve}')
                            deps.reject_pending_execution_report(session_id, f'validation rejected: {_ve}')
                        except Exception as _ae:
                            deps.outbox_mark_failed(session_id, str(_ae))
                            print(f'WARNING: apply_daily_execution failed (event pending in outbox): {_ae}')
            except Exception as e:
                print(f'WARNING: apply_daily_execution failed: {e}')

        deps.write_zeiterfassung_row(session, object_id, session['user_id'])
        worker_name_for_history = deps.object_history_worker_name(str(session['user_id']))
        finish_summary = session.get('done_summary') or done_summary.strip()
        deps.append_object_history_best_effort(
            object_id, 'finish_submitted', 'Финиш смены отправлен',
            user=user,
            subtitle=' · '.join(p for p in (worker_name_for_history, finish_summary) if p),
            at=datetime.fromtimestamp(session['finish_at'], timezone.utc).replace(tzinfo=None).isoformat() if session.get('finish_at') else None,
            meta={
                "session_id": session_id,
                "worker_id": str(session['user_id']),
                "photo_count": len(session.get('finish_photos') or []),
                "daily_plan_id": session.get('daily_plan_id') or '',
            },
        )

        if photo_paths:
            try:
                profiles = deps.load_worker_profiles()
                worker_name = deps.sanitize_display_name(profiles.get(str(session['user_id']), {}).get('name'), str(session['user_id']))
                rows = deps.cached_get_used_range('Объекты')
                object_name = object_id
                if rows:
                    header, data = rows[0], rows[1:]
                    for r in data:
                        obj = dict(zip(header, r))
                        if str(obj.get('ID объекта', '')) == object_id:
                            object_name = obj.get('Объект', object_id)
                            break
                deps.upsert_checkin_feed_post(session, 'finish', object_name, session['user_id'], worker_name)
            except Exception as e:
                print(f'WARNING: checkin-finish feed post failed: {e}')

        extra_work_summary = deps.extra_works_summary_text(session)
        if extra_work_summary or next_day_needs.strip():
            # Owner получает пуш только если worker реально что-то указал — не спамим
            # при пустом опроснике. 24.07: extra_work (доп-работы вне плана) теперь тоже
            # шлётся — раньше уходила только в Zeiterfassung sheet, owner мог её пропустить
            # без захода в таблицу. Нужно для billing: если заказчик попросил доп-работу на
            # месте, а её не заметили — компании не доплатят, хотя воркеру платят за время.
            # 27.07: extra_work_summary теперь может прийти из structured extra_works[]
            # (wizard), не только из старого одиночного текстового поля.
            roles = deps.load_roles()
            owner_id = next((uid for uid, r in roles.items() if r == 'owner'), None)
            if owner_id:
                if extra_work_summary:
                    try:
                        deps.send_telegram_message(int(owner_id),
                            f"⚠️ Доп-работы вне плана ({object_id}): {extra_work_summary[:300]}")
                    except Exception:
                        pass
                if next_day_needs.strip():
                    try:
                        deps.send_telegram_message(int(owner_id),
                            f"📋 На завтра нужно ({object_id}): {next_day_needs.strip()[:300]}")
                    except Exception:
                        pass
        _save_idempotency_response_after_commit(
            idempotency_key, session, idempotency_scope, operation='finish'
        )
        return session

    @router.get("/api/checkin/stundenzettel")
    def export_stundenzettel(user_id: str = '', year: int = 0, month: int = 0,
                              date_from: str = '', date_to: str = '',
                              user: dict = Depends(deps.get_current_user), role: str = Depends(deps.get_role)):
        """10.29 (Fable-аудит, идея): экспорт табеля рабочего времени — в Германии
        учёт рабочего времени обязателен по решению BAG (2022). CSV, открывается
        в Excel/LibreOffice — не тащим Node-PDF-пайплайн ради одного отчёта."""
        target_id = user_id or str(user['id'])
        if target_id != str(user['id']) and role != 'owner':
            raise HTTPException(403, "Можно выгружать только свой табель")
        # Раунд 5 §13: произвольный период date_from/date_to (Неделя/Месяц/3 месяца/свой).
        # Обратная совместимость: без диапазона — месяц year/month (старый вызов из UI).
        if date_from and date_to:
            for d in (date_from, date_to):
                try:
                    datetime.strptime(d, '%Y-%m-%d')
                except ValueError:
                    raise HTTPException(400, "date_from/date_to должны быть YYYY-MM-DD")
            if date_from > date_to:
                raise HTTPException(400, "date_from не может быть позже date_to")
            period_label = f'{date_from}_{date_to}'
            sessions = [s for s in deps.load_checkin_meta()
                        if str(s.get('user_id')) == target_id and date_from <= s.get('date', '') <= date_to]
        else:
            if not year or not month:
                now = deps.business_now()
                year, month = now.year, now.month
            month_prefix = f'{year:04d}-{month:02d}'
            period_label = month_prefix
            sessions = [s for s in deps.load_checkin_meta()
                        if str(s.get('user_id')) == target_id and s.get('date', '').startswith(month_prefix)]

        buf = io.StringIO()
        writer = csv.writer(buf, delimiter=';', lineterminator='\n')
        writer.writerow(['Дата', 'Объект', 'Начало', 'Конец', 'Пауза (мин)', 'Часы', 'Тип'])
        for s in sorted(sessions, key=lambda x: x.get('date', '')):
            kind = 'Ручной ввод' if s.get('manual_entry') else 'Фото-чекин'
            if s.get('manual_entry'):
                start, finish = s.get('start_time', ''), s.get('end_time', '')
            else:
                start = datetime.fromtimestamp(s['start_at']).strftime('%H:%M') if s.get('start_at') else ''
                finish = datetime.fromtimestamp(s['finish_at']).strftime('%H:%M') if s.get('finish_at') else 'не завершено'
            hours = round(deps.hours_from_session(s), 2)
            pause = int(s.get('pause_minutes') or 0) if s.get('manual_entry') else deps.photo_pause_minutes(s)
            writer.writerow([deps.csv_safe(s.get('date', '')), deps.csv_safe(s.get('object_id', '')), start, finish, pause, hours, kind])

        total_hours = round(sum(deps.hours_from_session(s) for s in sessions), 2)
        writer.writerow(['', '', '', '', '', total_hours, 'ИТОГО'])

        csv_content = buf.getvalue()
        # Раунд 6 §2.4: имя файла с реальным именем работника (если заполнено), не Telegram ID.
        _prof = deps.get_worker_profile(target_id)
        _disp = deps.sanitize_display_name(_prof.get('name'), '')
        if _disp and _disp != str(target_id):
            _safe_name = re.sub(r'[^0-9A-Za-zА-Яа-яЁё]+', '_', _disp).strip('_') or str(target_id)
        else:
            _safe_name = str(target_id)
        filename = f'Stundenzettel_{_safe_name}_{period_label}.csv'
        # Content-Disposition должен быть latin-1-safe (Starlette кодирует заголовки в latin-1),
        # поэтому кириллическое имя отдаём через RFC 5987 filename* (UTF-8, percent-encoded), а в
        # ASCII-fallback filename — только латиница/цифры (кириллица → '_').
        ascii_name = re.sub(r'[^0-9A-Za-z._-]+', '_', filename).strip('_') or 'Stundenzettel.csv'
        if not ascii_name.endswith('.csv'):
            ascii_name += '.csv'
        encoded_name = quote(filename)
        return Response(
            content='\ufeff' + csv_content,  # BOM — Excel корректно определяет UTF-8
            media_type='text/csv; charset=utf-8',
            headers={'Content-Disposition': f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded_name}"}
        )

    @router.get("/api/checkin")
    def list_checkins(object_id: str = '', date: str = '', user: dict = Depends(deps.get_current_user), role: str = Depends(deps.get_role)):
        return access_service.list_sessions(str(user['id']), role, object_id, date)


    @router.get("/api/checkin/{session_id}/finish-context")
    def checkin_finish_context(session_id: str, user: dict = Depends(deps.get_current_user), role: str = Depends(deps.get_role)):
        session = call_access(access_service.get_accessible_session, session_id, str(user['id']), role)
        return _build_finish_context(session)


    @router.get("/api/checkin/{session_id}/photo/{which}/{index}")
    def get_checkin_photo(session_id: str, which: str, index: int, user: dict = Depends(deps.get_current_user), role: str = Depends(deps.get_role)):
        path = call_access(access_service.photo_path, session_id, which, index, str(user['id']), role)
        return FileResponse(path)


    handlers = SimpleNamespace(
        checkin_start=checkin_start,
        checkin_pause=checkin_pause,
        checkin_finish=checkin_finish,
        export_stundenzettel=export_stundenzettel,
        list_checkins=list_checkins,
        checkin_finish_context=checkin_finish_context,
        get_checkin_photo=get_checkin_photo,
        build_finish_context=_build_finish_context,
        parse_checkin_occurred_at=_parse_checkin_occurred_at,
        checkin_business_date_from_timestamp=_checkin_business_date_from_timestamp,
    )
    return router, handlers
