"""Persisted critical-alert workflow: create, acknowledge, resolve and photos."""
from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

try:
    from ..core.media_paths import resolve_owned_media_path
except ImportError:
    from core.media_paths import resolve_owned_media_path  # noqa: E402

logger = logging.getLogger(__name__)


class CriticalAlertAckBody(BaseModel):
    comment: str = ''


class CriticalAlertCreateBody(BaseModel):
    target_user_id: str
    title: str
    subtitle: str = ''
    deadline_minutes: int | None = None


@dataclass(frozen=True)
class CriticalAlertsDeps:
    get_current_user: Callable[..., dict]
    get_role: Callable[..., str]
    require_owner: Callable[..., None]
    critical_alerts_file: Callable[[], str]
    critical_alert_photo_dir: Callable[[], str]
    safe_load_json: Callable[[str, Any], Any]
    atomic_write_json: Callable[[str, Any], None]
    update_json_transaction: Callable[[str, Any, Callable[[Any], Any]], Any]
    load_roles: Callable[[], dict]
    send_telegram_message: Callable[[int, str], Any]
    chat_lock: Any
    load_chat: Callable[[], list]
    save_chat: Callable[[list], None]
    chat_thread_id: Callable[[Any, Any], str]
    sniff_image: Callable[[bytes], str | None]
    allowed_image_mime_ext: Mapping[str, str]


@dataclass(frozen=True)
class CriticalAlertsHandlers:
    load_critical_alerts: Callable[[], list]
    save_critical_alerts: Callable[[list], None]
    is_legacy_bare_uid_birthday_ref: Callable[[dict], bool]
    supersede_legacy_siblings: Callable[[list, dict], int]
    create_critical_alert: Callable[..., dict]
    ensure_critical_alert_chat: Callable[..., None]
    ack_critical_alert: Callable[..., dict]
    resolve_critical_alert: Callable[..., dict]
    get_critical_alert_photo: Callable[..., Any]
    list_pending_critical_alerts: Callable[..., dict]
    create_critical_alert_endpoint: Callable[..., dict]


def create_critical_alerts_router(
    deps: CriticalAlertsDeps,
) -> tuple[APIRouter, CriticalAlertsHandlers]:
    router = APIRouter()
    os.makedirs(deps.critical_alert_photo_dir(), exist_ok=True)

    def load_critical_alerts() -> list:
        return deps.safe_load_json(deps.critical_alerts_file(), [])

    def save_critical_alerts(items: list):
        deps.atomic_write_json(deps.critical_alerts_file(), items)

    def is_legacy_bare_uid_birthday_ref(alert: dict) -> bool:
        if alert.get('kind') != 'birthday':
            return False
        ref_id = alert.get('ref_id') or ''
        return bool(ref_id) and ref_id.isdigit()

    def supersede_legacy_siblings(items: list, canonical: dict) -> int:
        if not is_legacy_bare_uid_birthday_ref(canonical):
            return 0
        now = canonical.get('acknowledged_at') or int(time.time())
        superseded = 0
        for alert in items:
            if alert['id'] == canonical['id']:
                continue
            if alert.get('acknowledged_at'):
                continue
            if not is_legacy_bare_uid_birthday_ref(alert):
                continue
            if (
                alert['kind'] != canonical['kind']
                or alert['target_user_id'] != canonical['target_user_id']
                or alert.get('ref_id') != canonical.get('ref_id')
            ):
                continue
            alert['acknowledged_at'] = now
            alert['superseded_by'] = canonical['id']
            superseded += 1
        return superseded

    def _append_chat_message_once(message: dict):
        """Append a secondary chat notification idempotently.

        The deterministic id makes retry after an ambiguous/partial failure safe:
        if the chat write actually committed, a later retry observes the message
        and does not append a duplicate.
        """
        with deps.chat_lock:
            messages = deps.load_chat()
            if any(
                isinstance(existing, dict)
                and existing.get('id') == message['id']
                for existing in messages
            ):
                return
            messages.append(message)
            deps.save_chat(messages)

    def _remove_photo_paths(paths: list[str]):
        for path in paths:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError:
                logger.exception('Failed to clean critical-alert photo %s', path)

    def ensure_critical_alert_chat(alert: dict, thread_id: str, owner_ids: list):
        del owner_ids  # compatibility argument retained for existing callers/tests
        msg = {
            'id': uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"critical-alert:create:{alert['id']}",
            ).hex,
            'ts': int(time.time()),
            'user_id': 'system',
            'name': 'Система',
            'text': f"🔴 Критический алерт: {alert['title']}",
            'to_user_id': None if thread_id == 'group' else thread_id,
            'critical_alert_id': alert['id'],
        }
        _append_chat_message_once(msg)

    def create_critical_alert(
        target_user_id: str,
        kind: str,
        title: str,
        ref_id: str = '',
        subtitle: str = '',
        deadline_at: int | None = None,
    ) -> dict:
        def _mutate(items):
            existing = next(
                (
                    alert for alert in items
                    if ref_id
                    and alert['target_user_id'] == str(target_user_id)
                    and alert['kind'] == kind
                    and alert.get('ref_id', '') == ref_id
                    and not alert.get('acknowledged_at')
                ),
                None,
            )
            if existing:
                return existing, False
            alert = {
                'id': uuid.uuid4().hex,
                'target_user_id': str(target_user_id),
                'kind': kind,
                'title': title,
                'subtitle': subtitle,
                'ref_id': ref_id,
                'created_at': int(time.time()),
                'deadline_at': deadline_at,
                'acknowledged_at': None,
                'comment': None,
                'resolution': None,
                'resolution_note': None,
                'resolution_photos': [],
                '_chat_create_pending': True,
            }
            items.append(alert)
            return alert, True

        alert, was_created = deps.update_json_transaction(
            deps.critical_alerts_file(), [], _mutate
        )
        needs_chat_repair = was_created or bool(alert.get('_chat_create_pending'))
        if needs_chat_repair:
            try:
                roles = deps.load_roles()
                owner_ids = [uid for uid, role in roles.items() if role == 'owner']
                thread_id = (
                    deps.chat_thread_id(
                        target_user_id,
                        owner_ids[0] if owner_ids else None,
                    )
                    if owner_ids else 'group'
                )
                ensure_critical_alert_chat(alert, thread_id, owner_ids)
            except Exception:
                # Alert JSON is the primary durable operation. A secondary chat
                # notification must never turn it into a false 500/retry loop.
                logger.exception(
                    'Critical-alert chat delivery failed after alert persisted: %s',
                    alert['id'],
                )
            else:
                def _clear_pending(items):
                    current = next(
                        (item for item in items if item['id'] == alert['id']),
                        None,
                    )
                    if current is not None:
                        current.pop('_chat_create_pending', None)

                try:
                    deps.update_json_transaction(
                        deps.critical_alerts_file(), [], _clear_pending
                    )
                except Exception:
                    # The chat append itself is idempotent, so leaving the repair
                    # marker behind is safe: a later dedup call can retry it.
                    logger.exception(
                        'Failed to clear critical-alert chat repair marker: %s',
                        alert['id'],
                    )

        if was_created:
            try:
                deps.send_telegram_message(int(target_user_id), f'🔴 {title}')
            except Exception:
                pass

        public_alert = dict(alert)
        public_alert.pop('_chat_create_pending', None)
        return public_alert

    @router.post('/api/critical-alerts/{alert_id}/ack')
    def ack_critical_alert(
        alert_id: str,
        body: CriticalAlertAckBody,
        user: dict = Depends(deps.get_current_user),
    ):
        def _mutate(items):
            alert = next((item for item in items if item['id'] == alert_id), None)
            if not alert:
                raise HTTPException(404, 'Алерт не найден')
            if alert['target_user_id'] != str(user['id']):
                raise HTTPException(403, 'Не ваш алерт')
            if not alert.get('acknowledged_at'):
                alert['acknowledged_at'] = int(time.time())
            alert['comment'] = body.comment.strip()[:500] or None
            supersede_legacy_siblings(items, alert)
            return alert

        alert = deps.update_json_transaction(deps.critical_alerts_file(), [], _mutate)
        if alert['comment']:
            try:
                roles = deps.load_roles()
                owner_ids = [uid for uid, role in roles.items() if role == 'owner']
                thread_id = (
                    deps.chat_thread_id(
                        alert['target_user_id'],
                        owner_ids[0] if owner_ids else None,
                    )
                    if owner_ids else 'group'
                )
                comment = alert['comment']
                msg = {
                    'id': uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"critical-alert:ack:{alert['id']}:{comment}",
                    ).hex,
                    'ts': int(time.time()),
                    'user_id': user['id'],
                    'name': user.get('first_name', str(user['id'])),
                    'text': comment,
                    'to_user_id': None if thread_id == 'group' else thread_id,
                    'critical_alert_id': alert['id'],
                }
                _append_chat_message_once(msg)
            except Exception:
                logger.exception(
                    'Critical-alert ack chat delivery failed after ack persisted: %s',
                    alert['id'],
                )
        return alert

    @router.post('/api/critical-alerts/{alert_id}/resolve')
    def resolve_critical_alert(
        alert_id: str,
        resolution: str = Form(...),
        note: str = Form(''),
        files: list[UploadFile] = File(default=[]),
        user: dict = Depends(deps.get_current_user),
    ):
        if resolution not in ('yes', 'no'):
            raise HTTPException(400, 'resolution должен быть yes или no')
        existing = next(
            (alert for alert in load_critical_alerts() if alert['id'] == alert_id), None
        )
        if not existing:
            raise HTTPException(404, 'Алерт не найден')
        if existing['target_user_id'] != str(user['id']):
            raise HTTPException(403, 'Не ваш алерт')

        normalized_note = note.strip()[:500]
        validated_photos: list[tuple[bytes, str]] = []
        if resolution == 'yes' and files:
            # Validate the entire batch before writing the first file. Otherwise a
            # valid first upload + invalid second upload leaves an orphan binary.
            for upload in files:
                data = upload.file.read()
                if len(data) > 8 * 1024 * 1024:
                    raise HTTPException(400, 'Файл слишком большой (макс. 8 МБ)')
                detected = deps.sniff_image(data)
                if not detected:
                    raise HTTPException(400, 'Файл должен быть изображением')
                validated_photos.append(
                    (data, '.' + deps.allowed_image_mime_ext[detected])
                )

        alert_dir = os.path.join(
            deps.critical_alert_photo_dir(), os.path.basename(alert_id)
        )
        saved_photos: list[str] = []
        saved_paths: list[str] = []
        if validated_photos:
            os.makedirs(alert_dir, exist_ok=True)
            try:
                for data, ext in validated_photos:
                    filename = f'{uuid.uuid4().hex}{ext}'
                    path = os.path.join(alert_dir, filename)
                    with open(path, 'wb') as output:
                        output.write(data)
                    saved_photos.append(filename)
                    saved_paths.append(path)
            except Exception:
                _remove_photo_paths(saved_paths)
                raise

        def _mutate(items):
            alert = next((item for item in items if item['id'] == alert_id), None)
            if not alert:
                raise HTTPException(404, 'Алерт не найден')
            if alert['target_user_id'] != str(user['id']):
                raise HTTPException(403, 'Не ваш алерт')
            previous_photos = list(alert.get('resolution_photos') or [])
            alert['resolution'] = resolution
            alert['resolution_note'] = normalized_note or None
            alert['resolution_photos'] = saved_photos
            return alert, previous_photos

        try:
            alert, previous_photos = deps.update_json_transaction(
                deps.critical_alerts_file(), [], _mutate
            )
        except Exception:
            # Metadata did not commit; new binaries belong to no durable record.
            _remove_photo_paths(saved_paths)
            raise

        # Metadata now points at the new set. Only after that durable commit may
        # files from the previous resolution be removed.
        for filename in previous_photos:
            if not isinstance(filename, str) or os.path.basename(filename) != filename:
                continue
            if filename in saved_photos:
                continue
            try:
                os.remove(os.path.join(alert_dir, filename))
            except FileNotFoundError:
                pass
            except OSError:
                logger.exception(
                    'Failed to remove superseded critical-alert photo %s/%s',
                    alert_id,
                    filename,
                )

        try:
            roles = deps.load_roles()
            owner_ids = [uid for uid, role in roles.items() if role == 'owner']
            thread_id = (
                deps.chat_thread_id(
                    alert['target_user_id'],
                    owner_ids[0] if owner_ids else None,
                )
                if owner_ids else 'group'
            )
            text = f"Вопрос решён: {'да' if resolution == 'yes' else 'нет'}"
            if normalized_note:
                text += f'\n{normalized_note}'
            if saved_photos:
                text += f'\n📷 {len(saved_photos)} фото'
            msg = {
                'id': uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    (
                        f"critical-alert:resolve:{alert['id']}:"
                        f'{resolution}:{normalized_note}'
                    ),
                ).hex,
                'ts': int(time.time()),
                'user_id': user['id'],
                'name': user.get('first_name', str(user['id'])),
                'text': text,
                'to_user_id': None if thread_id == 'group' else thread_id,
                'critical_alert_id': alert['id'],
            }
            _append_chat_message_once(msg)
        except Exception:
            logger.exception(
                'Critical-alert resolve chat delivery failed after resolution persisted: %s',
                alert['id'],
            )
        return alert

    @router.get('/api/critical-alerts/{alert_id}/photo/{filename}')
    def get_critical_alert_photo(
        alert_id: str,
        filename: str,
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        safe_alert_id = os.path.basename(alert_id)
        safe_filename = os.path.basename(filename)
        if safe_alert_id != alert_id or safe_filename != filename:
            raise HTTPException(404, 'Фото не найдено')

        alert = next(
            (item for item in load_critical_alerts() if item['id'] == alert_id), None
        )
        if not alert:
            raise HTTPException(404, 'Фото не найдено')
        if filename not in (alert.get('resolution_photos') or []):
            raise HTTPException(404, 'Фото не найдено')
        if role != 'owner' and alert['target_user_id'] != str(user['id']):
            raise HTTPException(403, 'Нет доступа к этому фото')

        path = resolve_owned_media_path(
            deps.critical_alert_photo_dir(),
            os.path.join(safe_alert_id, safe_filename),
        )
        if not path or not os.path.isfile(path):
            raise HTTPException(404, 'Фото не найдено')
        return FileResponse(path)

    @router.get('/api/critical-alerts/pending')
    def list_pending_critical_alerts(
        user: dict = Depends(deps.get_current_user),
    ):
        items = [
            alert for alert in load_critical_alerts()
            if alert['target_user_id'] == str(user['id'])
            and not alert.get('acknowledged_at')
        ]
        legacy_by_key: dict[tuple, dict] = {}
        result = []
        for alert in items:
            if not is_legacy_bare_uid_birthday_ref(alert):
                result.append(alert)
                continue
            key = (alert['kind'], alert['target_user_id'], alert.get('ref_id'))
            current = legacy_by_key.get(key)
            if current is None or alert.get('created_at', 0) > current.get('created_at', 0):
                legacy_by_key[key] = alert
        result.extend(legacy_by_key.values())
        return {'alerts': result}

    @router.post('/api/critical-alerts')
    def create_critical_alert_endpoint(
        body: CriticalAlertCreateBody,
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(deps.require_owner),
    ):
        deadline_at = (
            int(time.time()) + body.deadline_minutes * 60
            if body.deadline_minutes else None
        )
        return create_critical_alert(
            target_user_id=body.target_user_id,
            kind='manual',
            title=body.title,
            subtitle=body.subtitle,
            deadline_at=deadline_at,
        )

    return router, CriticalAlertsHandlers(
        load_critical_alerts=load_critical_alerts,
        save_critical_alerts=save_critical_alerts,
        is_legacy_bare_uid_birthday_ref=is_legacy_bare_uid_birthday_ref,
        supersede_legacy_siblings=supersede_legacy_siblings,
        create_critical_alert=create_critical_alert,
        ensure_critical_alert_chat=ensure_critical_alert_chat,
        ack_critical_alert=ack_critical_alert,
        resolve_critical_alert=resolve_critical_alert,
        get_critical_alert_photo=get_critical_alert_photo,
        list_pending_critical_alerts=list_pending_critical_alerts,
        create_critical_alert_endpoint=create_critical_alert_endpoint,
    )
