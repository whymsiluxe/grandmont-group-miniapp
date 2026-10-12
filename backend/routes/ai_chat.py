"""Owner/worker AI chat domain extracted from ``backend.main``.

Owns model selection, rate limiting, GLM/Claude execution, message validation and
AI upload parsing. External paths/limits/sniffers are injected; this module does
not import ``main``.
"""
from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib import error as _urlerror
from urllib import request as _urlreq

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel


AI_SYSTEM_PROMPT = (
    "Ты ИИ-ассистент строительной фирмы Grandmont Group UG (haftungsbeschränkt) "
    "(Chemnitz, Sachsen, Германия). Специализация: Trockenbau, Malerarbeiten, "
    "Spachtel Q2/Q3, Fliesen, Bodenbelag, WDVS/Fassade. Клиенты: Bauunternehmen, "
    "Hausverwaltungen, частные. Отвечай кратко и по делу. Внутренние ответы — на "
    "русском, тексты клиентам — на деловом немецком."
)

WORKER_AI_SYSTEM_PROMPT = (
    "Ты помощник для строителей. Отвечай только на общие вопросы о строительных "
    "работах: технологии (Trockenbau, Malerarbeiten, Spachtel, Fliesen, Bodenbelag, "
    "WDVS/Fassade), материалы, инструменты, безопасность труда, нормативы. "
    "НЕ обсуждай: конкретные объекты, клиентов, бюджеты, финансы фирмы, зарплаты, "
    "внутренние данные компании -- у тебя нет доступа к этой информации и её не "
    "существует в этом разговоре. Если спросят про конкретный объект/клиента/деньги -- "
    "скажи, что это нужно уточнить у владельца. Отвечай кратко и по делу, на русском."
)

CLAUDE_BIN = os.environ.get('CLAUDE_BIN', 'claude')
OWNER_AI_ENABLED = os.environ.get('OWNER_AI_ENABLED', 'false').strip().lower() in ('1', 'true', 'yes')
_claude_cli_lock = threading.Lock()


class AiChatBody(BaseModel):
    messages: list


class AiModelBody(BaseModel):
    model: str


class WorkerAiChatBody(BaseModel):
    messages: list


@dataclass(frozen=True)
class AiChatRouteDeps:
    get_current_user: Callable[..., dict]
    get_role: Callable[..., str]
    ai_rate_file: Callable[[], str]
    worker_ai_rate_file: Callable[[], str]
    ai_model_file: Callable[[], str]
    ai_rate_limit: int
    worker_ai_rate_limit: int
    ai_rate_window: int
    ai_models: tuple[str, ...] | list[str]
    ai_model_default: str
    owner_ai_env_allowlist: tuple[str, ...] | list[str]
    agent_root: Callable[[], str]
    ai_upload_max_bytes: int
    sniff_image_or_pdf: Callable[[bytes], str | None]
    allowed_image_mime_ext: Mapping[str, str]


@dataclass(frozen=True)
class AiChatHandlers:
    owner_ai_subprocess_env: Callable[[], dict]
    get_ai_model_value: Callable[[], str]
    set_ai_model_value: Callable[[str], None]
    check_ai_rate: Callable[..., None]
    call_glm: Callable[..., str]
    call_glm_json: Callable[[str, str], str]
    messages_to_prompt: Callable[[list], str]
    call_claude_cli: Callable[[list, str], str]
    call_ai: Callable[[list], str]
    is_multimodal_content: Callable[[Any], bool]
    ai_chat: Callable[..., dict]
    worker_ai_chat: Callable[..., dict]
    get_ai_model: Callable[..., dict]
    set_ai_model: Callable[..., dict]
    ai_chat_upload: Callable[..., Any]


def create_ai_chat_router(deps: AiChatRouteDeps) -> tuple[APIRouter, AiChatHandlers]:
    router = APIRouter()

    def owner_ai_subprocess_env() -> dict:
        return {
            key: os.environ[key]
            for key in deps.owner_ai_env_allowlist
            if key in os.environ
        }

    def get_ai_model_value() -> str:
        path = deps.ai_model_file()
        if os.path.exists(path):
            with open(path, encoding='utf-8') as handle:
                model = json.load(handle).get('model', deps.ai_model_default)
            if model in deps.ai_models:
                return model
        return deps.ai_model_default

    def set_ai_model_value(model: str):
        if model not in deps.ai_models:
            raise HTTPException(
                400,
                f"Неизвестная модель. Доступно: {', '.join(deps.ai_models)}",
            )
        with open(deps.ai_model_file(), 'w', encoding='utf-8') as handle:
            json.dump({'model': model}, handle)

    def check_ai_rate(user_id: int, rate_file: str = None, limit: int = None):
        rate_file = rate_file or deps.ai_rate_file()
        limit = limit if limit is not None else deps.ai_rate_limit
        data = {}
        if os.path.exists(rate_file):
            with open(rate_file, encoding='utf-8') as handle:
                data = json.load(handle)

        uid = str(user_id)
        now = time.time()
        user_data = data.get(uid, {'count': 0, 'window_start': now})
        if now - user_data['window_start'] >= deps.ai_rate_window:
            user_data = {'count': 0, 'window_start': now}
        if user_data['count'] >= limit:
            remaining = int(deps.ai_rate_window - (now - user_data['window_start']))
            raise HTTPException(
                429,
                f"Лимит {limit} запросов/час исчерпан. Сброс через "
                f"{remaining // 60} мин {remaining % 60} сек",
            )

        user_data['count'] += 1
        data[uid] = user_data
        with open(rate_file, 'w', encoding='utf-8') as handle:
            json.dump(data, handle)

    def call_glm(messages: list, system: str = None) -> str:
        glm_key = os.environ.get('GLM_KEY', '')
        if not glm_key:
            raise HTTPException(503, 'GLM API не настроен (нет GLM_KEY)')
        payload = {
            'model': 'glm-4.5-flash',
            'max_tokens': 1024,
            'system': system if system is not None else AI_SYSTEM_PROMPT,
            'messages': messages,
        }
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        request = _urlreq.Request(
            'https://api.z.ai/api/anthropic/v1/messages',
            data=data,
            method='POST',
            headers={
                'Content-Type': 'application/json',
                'x-api-key': glm_key,
                'anthropic-version': '2023-06-01',
            },
        )
        try:
            with _urlreq.urlopen(request, timeout=45) as response:
                result = json.loads(response.read().decode('utf-8'))
            return result['content'][0]['text']
        except _urlerror.HTTPError as exc:
            err_body = exc.read().decode('utf-8')
            raise HTTPException(502, f'GLM API ошибка: {err_body[:300]}')
        except Exception as exc:
            raise HTTPException(502, f'GLM API недоступен: {str(exc)[:200]}')

    def call_glm_json(system: str, user_text: str) -> str:
        glm_key = os.environ.get('GLM_KEY', '')
        if not glm_key:
            raise HTTPException(503, 'GLM API не настроен (нет GLM_KEY)')
        payload = {
            'model': 'glm-4.5-flash',
            'max_tokens': 512,
            'system': system,
            'messages': [{'role': 'user', 'content': user_text}],
        }
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        request = _urlreq.Request(
            'https://api.z.ai/api/anthropic/v1/messages',
            data=data,
            method='POST',
            headers={
                'Content-Type': 'application/json',
                'x-api-key': glm_key,
                'anthropic-version': '2023-06-01',
            },
        )
        with _urlreq.urlopen(request, timeout=45) as response:
            result = json.loads(response.read().decode('utf-8'))
        return result['content'][0]['text']

    def messages_to_prompt(messages: list) -> str:
        parts = [f'[SYSTEM]\n{AI_SYSTEM_PROMPT}']
        for message in messages:
            role = message.get('role', 'user').upper()
            content = message.get('content')
            if isinstance(content, str):
                text = content
            else:
                text = ' '.join(
                    block.get('text', '')
                    for block in content
                    if isinstance(block, dict) and block.get('type') == 'text'
                )
            parts.append(f'[{role}]\n{text}')
        return '\n\n'.join(parts)

    def call_claude_cli(messages: list, model: str) -> str:
        if not OWNER_AI_ENABLED:
            raise HTTPException(
                503,
                'Owner AI (Sonnet/Opus) временно отключён в production '
                '(OWNER_AI_ENABLED=false) -- доступен GLM-режим',
            )
        prompt = messages_to_prompt(messages)
        if not _claude_cli_lock.acquire(timeout=1):
            raise HTTPException(
                429,
                'Уже выполняется другой запрос к Claude — подожди и повтори',
            )
        try:
            result = subprocess.run(
                [CLAUDE_BIN, '-p', '--model', model, prompt],
                cwd=deps.agent_root(),
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=True,
                timeout=120,
                env=owner_ai_subprocess_env(),
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(504, f'Claude ({model}) не ответил за 120 сек')
        except Exception as exc:
            raise HTTPException(502, f'Claude CLI недоступен: {str(exc)[:200]}')
        finally:
            _claude_cli_lock.release()
        if result.returncode != 0:
            raise HTTPException(502, f"Claude CLI ошибка: {(result.stderr or '')[:300]}")
        reply = (result.stdout or '').strip()
        if not reply:
            raise HTTPException(502, 'Claude вернул пустой ответ')
        return reply

    def call_ai(messages: list) -> str:
        model = get_ai_model_value()
        if model == 'glm':
            return call_glm(messages)
        return call_claude_cli(messages, model)

    def is_multimodal_content(content) -> bool:
        return isinstance(content, list)

    @router.post('/api/ai-chat')
    def ai_chat(
        body: AiChatBody,
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        if role != 'owner':
            raise HTTPException(403, 'Только для владельца')
        if not body.messages:
            raise HTTPException(400, 'Нет сообщений')
        for message in body.messages:
            if not isinstance(message, dict) or message.get('role') not in ('user', 'assistant'):
                raise HTTPException(400, 'Неверный формат сообщений: {role, content} required')
            content = message.get('content')
            if isinstance(content, str):
                if len(content) > 8000:
                    raise HTTPException(400, 'Сообщение слишком длинное')
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict) or block.get('type') not in ('text', 'image'):
                        raise HTTPException(400, 'Неверный формат content-блока')
            else:
                raise HTTPException(400, 'content должен быть строкой или списком блоков')
        check_ai_rate(user['id'])
        return {'reply': call_ai(body.messages)}

    @router.post('/api/ai-chat/worker')
    def worker_ai_chat(
        body: WorkerAiChatBody,
        user: dict = Depends(deps.get_current_user),
    ):
        if not body.messages:
            raise HTTPException(400, 'Нет сообщений')
        for message in body.messages:
            if not isinstance(message, dict) or message.get('role') not in ('user', 'assistant'):
                raise HTTPException(400, 'Неверный формат сообщений: {role, content} required')
            content = message.get('content')
            if not isinstance(content, str):
                raise HTTPException(400, 'content должен быть строкой (без фото/вложений в этом чате)')
            if len(content) > 4000:
                raise HTTPException(400, 'Сообщение слишком длинное')
        check_ai_rate(
            user['id'],
            rate_file=deps.worker_ai_rate_file(),
            limit=deps.worker_ai_rate_limit,
        )
        return {'reply': call_glm(body.messages, system=WORKER_AI_SYSTEM_PROMPT)}

    @router.get('/api/ai-model')
    def get_ai_model(
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        if role != 'owner':
            raise HTTPException(403, 'Только для владельца')
        return {'model': get_ai_model_value(), 'available': list(deps.ai_models)}

    @router.post('/api/ai-model')
    def set_ai_model(
        body: AiModelBody,
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        if role != 'owner':
            raise HTTPException(403, 'Только для владельца')
        set_ai_model_value(body.model)
        return {'model': body.model}

    @router.post('/api/ai-chat/upload')
    async def ai_chat_upload(
        file: UploadFile = File(...),
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        if role != 'owner':
            raise HTTPException(403, 'Только для владельца')
        raw = await file.read()
        if len(raw) > deps.ai_upload_max_bytes:
            raise HTTPException(400, 'Файл слишком большой (макс. 8 МБ)')
        filename = file.filename or 'file'
        detected = deps.sniff_image_or_pdf(raw)
        if detected in deps.allowed_image_mime_ext:
            encoded = base64.b64encode(raw).decode('ascii')
            return {
                'kind': 'image',
                'filename': filename,
                'block': {
                    'type': 'image',
                    'source': {
                        'type': 'base64',
                        'media_type': detected,
                        'data': encoded,
                    },
                },
            }
        if detected == 'application/pdf':
            try:
                import pypdf
                reader = pypdf.PdfReader(io.BytesIO(raw))
                text = '\n'.join(page.extract_text() or '' for page in reader.pages)
            except Exception as exc:
                raise HTTPException(400, f'Не удалось прочитать PDF: {str(exc)[:200]}')
            text = text[:12000]
            if not text.strip():
                raise HTTPException(400, 'PDF не содержит извлекаемого текста (возможно скан-изображение)')
            return {'kind': 'text', 'filename': filename, 'text': text}
        try:
            text = raw.decode('utf-8', errors='ignore')[:12000]
        except Exception:
            raise HTTPException(400, 'Не удалось прочитать файл как текст')
        if not text.strip():
            raise HTTPException(400, 'Файл пуст или в неподдерживаемом формате')
        return {'kind': 'text', 'filename': filename, 'text': text}

    return router, AiChatHandlers(
        owner_ai_subprocess_env=owner_ai_subprocess_env,
        get_ai_model_value=get_ai_model_value,
        set_ai_model_value=set_ai_model_value,
        check_ai_rate=check_ai_rate,
        call_glm=call_glm,
        call_glm_json=call_glm_json,
        messages_to_prompt=messages_to_prompt,
        call_claude_cli=call_claude_cli,
        call_ai=call_ai,
        is_multimodal_content=is_multimodal_content,
        ai_chat=ai_chat,
        worker_ai_chat=worker_ai_chat,
        get_ai_model=get_ai_model,
        set_ai_model=set_ai_model,
        ai_chat_upload=ai_chat_upload,
    )
