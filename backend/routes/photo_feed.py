"""Photo-feed domain extracted from ``backend.main``.

Owns photo metadata/reactions, check-in photo mirroring, photo list/upload and
comment/reaction lifecycle. Cross-domain saved-state and activity-alert behavior
is injected so this module never imports ``main``/chat/news.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

try:
    from ..core.media_paths import resolve_owned_media_path
except ImportError:
    from core.media_paths import resolve_owned_media_path  # noqa: E402


logger = logging.getLogger(__name__)


class PhotoCommentBody(BaseModel):
    text: str
    reply_to: str | None = None


class PhotoReactionBody(BaseModel):
    liked: bool


@dataclass(frozen=True)
class PhotoFeedRouteDeps:
    get_current_user: Callable[..., dict]
    get_role: Callable[..., str]
    safe_load_json: Callable[[str, Any], Any]
    atomic_write_json: Callable[[str, Any], None]
    photo_dir: Callable[[], str]
    photo_meta_file: Callable[[], str]
    photo_reactions_file: Callable[[], str]
    checkin_photo_base: Callable[[], str]
    photo_max_bytes: int
    photo_max_count: int
    photo_max_files: int
    sniff_image: Callable[[bytes], str | None]
    allowed_image_mime_ext: Mapping[str, str]
    load_feed_saved: Callable[[], dict]
    is_feed_saved: Callable[[dict, str, str, str], bool]
    sanitize_display_name: Callable[[Any, str], str]
    resolve_current_display_name: Callable[[Any, Any], str]
    resolve_object_name: Callable[[str], str]
    emit_comment_activity_alerts: Callable[..., Any]
    comment_alert_recipients: Callable[..., set]


@dataclass(frozen=True)
class PhotoFeedHandlers:
    photo_lock: Any
    load_photo_meta: Callable[[], list]
    save_photo_meta: Callable[[list], None]
    load_photo_reactions: Callable[[], dict]
    save_photo_reactions: Callable[[dict], None]
    copy_checkin_photos_to_feed: Callable[[list, str], list]
    upsert_checkin_feed_post: Callable[..., None]
    list_feed_photos: Callable[..., dict]
    upload_feed_photo: Callable[..., Any]
    add_feed_photo_comment: Callable[..., dict]
    react_feed_photo: Callable[..., dict]
    get_feed_photo_comments: Callable[..., dict]
    delete_feed_photo_comment: Callable[..., dict]


def create_photo_feed_router(deps: PhotoFeedRouteDeps) -> tuple[APIRouter, PhotoFeedHandlers]:
    router = APIRouter()
    photo_lock = threading.Lock()
    os.makedirs(deps.photo_dir(), exist_ok=True)

    def load_photo_meta() -> list:
        return deps.safe_load_json(deps.photo_meta_file(), [])

    def save_photo_meta(items: list):
        deps.atomic_write_json(deps.photo_meta_file(), items)

    def _remove_photo_files_best_effort(filenames: list[str]):
        for filename in filenames:
            safe_path = resolve_owned_media_path(deps.photo_dir(), filename)
            if not safe_path:
                continue
            try:
                os.remove(safe_path)
            except FileNotFoundError:
                pass
            except OSError:
                logger.exception(
                    'Failed to remove photo-feed binary after metadata transition',
                    extra={'filename': filename},
                )

    def load_photo_reactions() -> dict:
        return deps.safe_load_json(deps.photo_reactions_file(), {})

    def save_photo_reactions(data: dict):
        deps.atomic_write_json(deps.photo_reactions_file(), data)

    def copy_checkin_photos_to_feed(rel_paths: list, prefix: str) -> list:
        saved = []
        for index, rel in enumerate(rel_paths):
            src_path = resolve_owned_media_path(deps.checkin_photo_base(), rel)
            if not src_path or not os.path.exists(src_path):
                continue
            ext = os.path.splitext(rel)[1] or '.jpg'
            filename = f'{prefix}_{index}{ext}'
            with open(src_path, 'rb') as source, open(os.path.join(deps.photo_dir(), filename), 'wb') as target:
                target.write(source.read())
            saved.append(filename)
        return saved

    def upsert_checkin_feed_post(session: dict, kind: str, object_name: str, user_id, user_name: str):
        rel_paths = session.get('start_photos' if kind == 'start' else 'finish_photos') or []
        if not rel_paths:
            return
        prefix = f"checkin_{session['id']}_{kind}"
        new_files = copy_checkin_photos_to_feed(rel_paths, prefix)
        if not new_files:
            return

        time_str = datetime.fromtimestamp(
            session['start_at'] if kind == 'start' else session['finish_at']
        ).strftime('%H:%M')
        label = ('Начало смены' if kind == 'start' else 'Конец смены') + f' · {time_str}'

        try:
            with photo_lock:
                items = load_photo_meta()
                post = next((item for item in items if item.get('checkin_session_id') == session['id']), None)
                if post:
                    start_index = len(post['files'])
                    post['files'].extend(new_files)
                    post.setdefault('photo_labels', {})
                    for index, _filename in enumerate(new_files):
                        post['photo_labels'][str(start_index + index)] = label
                else:
                    post = {
                        'id': uuid.uuid4().hex,
                        'files': new_files,
                        'ts': int(time.time()),
                        'user_id': user_id,
                        'name': user_name,
                        'object_id': object_name,
                        'caption': '',
                        'checkin_session_id': session['id'],
                        'photo_labels': {str(index): label for index in range(len(new_files))},
                    }
                    items.append(post)
                save_photo_meta(items)
        except Exception:
            _remove_photo_files_best_effort(new_files)
            raise

    @router.get('/api/feed/photos')
    def list_feed_photos(user: dict = Depends(deps.get_current_user)):
        with photo_lock:
            items = load_photo_meta()
        reactions = load_photo_reactions()
        saved = deps.load_feed_saved()
        uid = str(user['id'])
        photos = []
        for stored in reversed(items):
            photo = dict(stored)
            photo_reactions = reactions.get(photo.get('id'), {})
            photo['likes'] = len(photo_reactions)
            photo['liked_by_me'] = uid in photo_reactions
            photo['saved_by_me'] = deps.is_feed_saved(saved, uid, 'photo', photo.get('id'))
            photo['comment_count'] = len(photo.pop('comments', []))
            if photo.get('user_id'):
                photo['name'] = deps.resolve_current_display_name(photo['user_id'], photo.get('name'))
            if 'files' not in photo:
                photo['files'] = [photo['file']] if photo.get('file') else []
            photo['files'] = [
                filename for filename in photo.get('files', [])
                if (
                    (safe_path := resolve_owned_media_path(deps.photo_dir(), filename))
                    and os.path.exists(safe_path)
                )
            ]
            if not photo['files']:
                continue
            photos.append(photo)
        return {'photos': photos}

    @router.post('/api/feed/photos')
    async def upload_feed_photo(
        files: list[UploadFile] = File(...),
        object_id: str = Form(''),
        caption: str = Form(''),
        user: dict = Depends(deps.get_current_user),
    ):
        if not files:
            raise HTTPException(400, 'Нужно хотя бы одно фото')
        if len(files) > deps.photo_max_files:
            raise HTTPException(400, f'Максимум {deps.photo_max_files} фото за раз')

        # Validate the entire batch before creating any binaries. A bad second
        # upload must not leave the already-written first file orphaned.
        validated = []
        for upload in files:
            raw = await upload.read()
            if len(raw) > deps.photo_max_bytes:
                raise HTTPException(400, 'Фото слишком большое (макс. 8 МБ на файл)')
            detected = deps.sniff_image(raw)
            if not detected:
                raise HTTPException(400, 'Все файлы должны быть изображениями')
            validated.append((raw, deps.allowed_image_mime_ext[detected]))

        photo_id = uuid.uuid4().hex
        saved_files = []
        evicted_files = []
        try:
            for raw, ext in validated:
                filename = f'{photo_id}_{len(saved_files)}.{ext}'
                with open(os.path.join(deps.photo_dir(), filename), 'wb') as output:
                    output.write(raw)
                saved_files.append(filename)

            entry = {
                'id': photo_id,
                'files': saved_files,
                'ts': int(time.time()),
                'user_id': user['id'],
                'name': deps.sanitize_display_name(user.get('first_name'), str(user['id'])),
                'object_id': object_id.strip()[:100],
                'caption': caption.strip()[:300],
            }

            with photo_lock:
                items = load_photo_meta()
                items.append(entry)
                if len(items) > deps.photo_max_count:
                    evicted = items[:-deps.photo_max_count]
                    for old in evicted:
                        evicted_files.extend(old.get('files') or ([old['file']] if old.get('file') else []))
                    items = items[-deps.photo_max_count:]
                # Metadata is the ownership record. Commit it BEFORE deleting
                # evicted binaries; a failed metadata write must leave the old
                # feed fully intact rather than pointing at already-deleted files.
                save_photo_meta(items)
        except Exception:
            _remove_photo_files_best_effort(saved_files)
            raise

        # Metadata commit succeeded. Failure to remove evicted binaries now only
        # creates harmless disk orphans, never broken live metadata.
        _remove_photo_files_best_effort(evicted_files)
        return {'photo': entry}

    @router.post('/api/feed/photos/{photo_id}/comments')
    def add_feed_photo_comment(
        photo_id: str,
        body: PhotoCommentBody,
        user: dict = Depends(deps.get_current_user),
    ):
        text = (body.text or '').strip()[:500]
        if not text:
            raise HTTPException(400, 'Комментарий не может быть пустым')
        with photo_lock:
            items = load_photo_meta()
            entry = next((item for item in items if item['id'] == photo_id), None)
            if not entry:
                raise HTTPException(404, 'Фото не найдено')
            comments = entry.setdefault('comments', [])
            prior_ids = {comment.get('user_id') for comment in comments}
            photo_author = entry.get('user_id')
            photo_object_id = entry.get('object_id', '')
            actor_name = deps.sanitize_display_name(user.get('first_name'), str(user['id']))
            new_id = uuid.uuid4().hex
            comments.append({
                'id': new_id,
                'user_id': str(user['id']),
                'name': actor_name,
                'text': text,
                'reply_to': body.reply_to or None,
                'at': datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            })
            save_photo_meta(items)
        try:
            object_name = deps.resolve_object_name(photo_object_id)
            subtitle = f'Объект: {object_name}' if object_name else ''
            deps.emit_comment_activity_alerts(
                'photo_comment', photo_id, new_id, str(user['id']), actor_name,
                f'{actor_name} прокомментировал фото', subtitle,
                deps.comment_alert_recipients(user['id'], prior_ids, author_id=photo_author),
            )
        except Exception:
            pass
        return {'comments': entry['comments']}

    @router.post('/api/feed/photos/{photo_id}/react')
    def react_feed_photo(
        photo_id: str,
        body: PhotoReactionBody,
        user: dict = Depends(deps.get_current_user),
    ):
        with photo_lock:
            items = load_photo_meta()
            if not any(item.get('id') == photo_id for item in items):
                raise HTTPException(404, 'Фото не найдено')
            reactions = load_photo_reactions()
            photo_reactions = reactions.setdefault(photo_id, {})
            uid = str(user['id'])
            if body.liked:
                photo_reactions[uid] = True
            else:
                photo_reactions.pop(uid, None)
            save_photo_reactions(reactions)
        return {'likes': len(photo_reactions), 'liked_by_me': bool(body.liked)}

    @router.get('/api/feed/photos/{photo_id}/comments')
    def get_feed_photo_comments(photo_id: str, user: dict = Depends(deps.get_current_user)):
        with photo_lock:
            items = load_photo_meta()
        entry = next((item for item in items if item['id'] == photo_id), None)
        if not entry:
            raise HTTPException(404, 'Фото не найдено')
        comments = entry.get('comments', [])
        for comment in comments:
            comment['name'] = deps.resolve_current_display_name(
                comment.get('user_id'), comment.get('name')
            )
        return {'comments': comments}

    @router.delete('/api/feed/photos/{photo_id}/comments/{comment_id}')
    def delete_feed_photo_comment(
        photo_id: str,
        comment_id: str,
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        with photo_lock:
            items = load_photo_meta()
            entry = next((item for item in items if item['id'] == photo_id), None)
            if not entry:
                raise HTTPException(404, 'Фото не найдено')
            comments = entry.get('comments', [])
            comment = next((item for item in comments if item.get('id') == comment_id), None)
            if not comment:
                raise HTTPException(404, 'Комментарий не найден')
            if str(comment.get('user_id')) != str(user['id']) and role != 'owner':
                raise HTTPException(403, 'Можно удалить только свой комментарий')
            entry['comments'] = [item for item in comments if item.get('id') != comment_id]
            save_photo_meta(items)
        return {'comments': entry['comments']}

    return router, PhotoFeedHandlers(
        photo_lock=photo_lock,
        load_photo_meta=load_photo_meta,
        save_photo_meta=save_photo_meta,
        load_photo_reactions=load_photo_reactions,
        save_photo_reactions=save_photo_reactions,
        copy_checkin_photos_to_feed=copy_checkin_photos_to_feed,
        upsert_checkin_feed_post=upsert_checkin_feed_post,
        list_feed_photos=list_feed_photos,
        upload_feed_photo=upload_feed_photo,
        add_feed_photo_comment=add_feed_photo_comment,
        react_feed_photo=react_feed_photo,
        get_feed_photo_comments=get_feed_photo_comments,
        delete_feed_photo_comment=delete_feed_photo_comment,
    )
