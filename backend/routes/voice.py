"""Voice transcription and chat-voice domain extracted from ``backend.main``."""
from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse


@dataclass(frozen=True)
class VoiceRouteDeps:
    get_current_user: Callable[..., dict]
    get_role: Callable[..., str]
    transcribe_audio_dir: Callable[[], str]
    transcribe_max_bytes: int
    chat_attach_dir: Callable[[], str]
    sniff_audio: Callable[[bytes], str | None]
    allowed_audio_mime_ext: Mapping[str, str]
    check_thread_access: Callable[[str, str, str], Any]
    reject_self_chat: Callable[[Any, Any], Any]
    validate_dm_recipient: Callable[[Any], Any]
    chat_thread_id: Callable[[Any, Any], str]
    load_chat_thread_meta: Callable[[], dict]
    chat_lock: Any
    load_chat: Callable[[], list]
    save_chat: Callable[[list], None]


@dataclass(frozen=True)
class VoiceHandlers:
    get_whisper: Callable[[], Any]
    transcribe_voice: Callable[[str], str]
    transcribe_voice_endpoint: Callable[..., Any]
    get_transcribe_audio: Callable[..., Any]
    post_chat_voice: Callable[..., Any]


def create_voice_router(deps: VoiceRouteDeps) -> tuple[APIRouter, VoiceHandlers]:
    router = APIRouter()
    whisper_lock = threading.Lock()
    whisper_state: dict[str, Any] = {'model': None}
    os.makedirs(deps.transcribe_audio_dir(), exist_ok=True)

    def get_whisper():
        if whisper_state['model'] is None:
            with whisper_lock:
                if whisper_state['model'] is None:
                    from faster_whisper import WhisperModel
                    whisper_state['model'] = WhisperModel('base', device='cpu', compute_type='int8')
        return whisper_state['model']

    def transcribe_voice(path: str) -> str:
        model = get_whisper()
        segments, _ = model.transcribe(path, language=None)
        return ' '.join(segment.text.strip() for segment in segments).strip()

    @router.post('/api/transcribe')
    async def transcribe_voice_endpoint(
        file: UploadFile = File(...),
        user: dict = Depends(deps.get_current_user),
    ):
        data = await file.read()
        if len(data) > deps.transcribe_max_bytes:
            raise HTTPException(400, 'Голосовое слишком большое (макс. 8 МБ)')
        if not data:
            raise HTTPException(400, 'Пустой файл')
        detected = deps.sniff_audio(data)
        if detected is None:
            raise HTTPException(400, 'Недопустимый формат аудио')
        ext = f".{deps.allowed_audio_mime_ext[detected]}"

        uid = str(user['id'])
        user_dir = os.path.join(deps.transcribe_audio_dir(), uid)
        os.makedirs(user_dir, exist_ok=True)
        file_id = uuid.uuid4().hex
        path = os.path.join(user_dir, f'{file_id}{ext}')
        with open(path, 'wb') as handle:
            handle.write(data)

        try:
            transcript = transcribe_voice(path)
        except Exception as exc:
            raise HTTPException(502, f'Не удалось распознать голосовое: {str(exc)[:200]}')
        if not transcript:
            raise HTTPException(422, 'Не удалось разобрать речь в записи — попробуй ещё раз')
        return {
            'raw_transcript': transcript,
            'status': 'ok',
            'file_id': file_id,
            'audio_url': f'/api/transcribe/{file_id}/audio',
        }

    @router.get('/api/transcribe/{file_id}/audio')
    def get_transcribe_audio(
        file_id: str,
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        safe_file_id = os.path.basename(file_id)
        if safe_file_id != file_id:
            raise HTTPException(404, 'Файл не найден')
        uid = str(user['id'])
        root = deps.transcribe_audio_dir()
        if role == 'owner':
            search_dirs = os.listdir(root) if os.path.isdir(root) else []
        else:
            search_dirs = [uid]
        for directory in search_dirs:
            user_dir = os.path.join(root, os.path.basename(directory))
            if not os.path.isdir(user_dir):
                continue
            for filename in os.listdir(user_dir):
                if filename.startswith(safe_file_id):
                    return FileResponse(os.path.join(user_dir, filename))
        raise HTTPException(404, 'Файл не найден')

    @router.post('/api/chat/messages/voice')
    async def post_chat_voice(
        thread_key: str = Form(''),
        to_user_id: str = Form(''),
        file: UploadFile = File(...),
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        if thread_key:
            deps.check_thread_access(thread_key, str(user['id']), role)
        else:
            target = to_user_id or None
            deps.reject_self_chat(user['id'], target)
            deps.validate_dm_recipient(target)
            thread_id = deps.chat_thread_id(user['id'], target)
            thread_meta = deps.load_chat_thread_meta()
            if thread_meta.get(thread_id, {}).get('closed') and role != 'owner':
                raise HTTPException(403, 'Чат закрыт руководством')

        data = await file.read()
        if len(data) > 8 * 1024 * 1024:
            raise HTTPException(400, 'Голосовое слишком большое (макс. 8 МБ)')
        detected = deps.sniff_audio(data)
        if detected is None:
            raise HTTPException(400, 'Недопустимый формат аудио')
        filename = f"{uuid.uuid4().hex}.{deps.allowed_audio_mime_ext[detected]}"
        path = os.path.join(deps.chat_attach_dir(), filename)
        with open(path, 'wb') as handle:
            handle.write(data)

        try:
            transcript = transcribe_voice(path)
        except Exception as exc:
            transcript = ''
            print(f'WARNING: транскрипция голосового не удалась: {exc}')

        message = {
            'id': uuid.uuid4().hex,
            'ts': int(time.time()),
            'user_id': user['id'],
            'name': user.get('first_name', str(user['id'])),
            'text': '',
            'to_user_id': to_user_id or None,
            'thread_key': thread_key or None,
            'attachment': {
                'file': filename,
                'name': file.filename or filename,
                'content_type': file.content_type or 'audio/ogg',
            },
            'voice_transcript': transcript,
        }
        with deps.chat_lock:
            messages = deps.load_chat()
            messages.append(message)
            deps.save_chat(messages)
        return {'message': message}

    return router, VoiceHandlers(
        get_whisper=get_whisper,
        transcribe_voice=transcribe_voice,
        transcribe_voice_endpoint=transcribe_voice_endpoint,
        get_transcribe_audio=get_transcribe_audio,
        post_chat_voice=post_chat_voice,
    )
