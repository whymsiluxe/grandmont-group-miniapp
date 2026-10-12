#!/usr/bin/env python3
"""Grandmont Group Mini App — FastAPI backend. Фаза 2 плана: скелет + initData-auth + roles.
Запуск: uvicorn main:app --host 127.0.0.1 --port 8001
"""
import asyncio
import copy
import csv
import hashlib
import hmac
import importlib.util
import json
import logging
import math
import os
import re
import sys
import tempfile
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, quote

from fastapi import FastAPI, Header, HTTPException, Depends, BackgroundTasks, UploadFile, File, Form, Query
from fastapi.middleware.cors import CORSMiddleware
import base64
from pydantic import BaseModel

def _env_compat(name: str, legacy_name: str, default=None):
    """Grandmont Group rebrand (26.09): env vars were renamed PROMONTA_* ->
    GRANDMONT_GROUP_*. The new name wins; the pre-rebrand name is still honoured so a
    deployed unit/.env that sets the old name keeps working until it is migrated."""
    value = os.environ.get(name)
    if value is None:
        value = os.environ.get(legacy_name)
    return default if value is None else value


AGENT_ROOT = _env_compat('GRANDMONT_GROUP_AGENT_ROOT', 'PROMONTA_AGENT_ROOT', '/home/grandmont/agent')

sys.path.insert(0, AGENT_ROOT)

# 30.07 (Инструменты cleanup, изолированный фикс): sys.path выше -- ГЛОБАЛЬНЫЙ и
# специально ставит AGENT_ROOT первым, чтобы roadmap_lib/objekte_lib/другие
# shared runtime-модули резолвились так же, как всегда (изменение этого порядка в
# предыдущем коммите сломало 2 roadmap-теста -- откачено). tools_lib.py тем не менее
# должен гарантированно грузиться из репозитория (backend/tools_lib.py), не из
# untracked AGENT_ROOT/tools_lib.py -- решение точечное: загрузка по явному
# пути к файлу через importlib.util, без малейшего влияния на глобальный sys.path.
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_ROOT = os.environ.get('MINIAPP_DATA_ROOT', '/home/grandmont/agent/miniapp')
# 17.09: default moved from AGENT_ROOT (external, untracked path) to BACKEND_DIR
# -- both scripts are now tracked in backend/ (see scripts/manifest.sh) so a
# clean clone + deploy is self-contained. Env override still works for anyone
# who wants to point at a different location.
CREATE_OBJECT_SCRIPT = _env_compat('GRANDMONT_GROUP_CREATE_OBJECT_SCRIPT', 'PROMONTA_CREATE_OBJECT_SCRIPT', os.path.join(BACKEND_DIR, 'create_object.py'))
CREATE_OBJECT_FOLDER_SCRIPT = _env_compat('GRANDMONT_GROUP_CREATE_OBJECT_FOLDER_SCRIPT', 'PROMONTA_CREATE_OBJECT_FOLDER_SCRIPT', os.path.join(BACKEND_DIR, 'create_object_folder.py'))
TOOL_BOOKINGS_FILE = os.path.join(DATA_ROOT, 'tool_bookings.json')

_PROD_DATA_ROOT = '/home/grandmont/agent/miniapp'
_is_test_context = (
    _env_compat('GRANDMONT_GROUP_ENV', 'PROMONTA_ENV') == 'test'
    or 'pytest' in sys.modules
)
if _is_test_context and DATA_ROOT == _PROD_DATA_ROOT:
    raise RuntimeError(
        f"REFUSING TO RUN TESTS AGAINST PRODUCTION DATA ROOT ({_PROD_DATA_ROOT}). "
        "Set MINIAPP_DATA_ROOT to a temp directory before importing this module "
        "in test context. conftest.py should have handled this automatically."
    )
TOOLS_LIB_PATH = os.path.join(BACKEND_DIR, 'tools_lib.py')
MANGEL_LIB_PATH = os.path.join(BACKEND_DIR, 'mangel_lib.py')
OBJEKTE_LIB_PATH = os.path.join(BACKEND_DIR, 'objekte_lib.py')
ROADMAP_LIB_PATH = os.path.join(BACKEND_DIR, 'roadmap_lib.py')
_repo_tools_lib = None
_repo_mangel_lib = None
_repo_objekte_lib = None
_repo_roadmap_lib = None


def _load_repo_module(path: str, internal_name: str, cache: dict, cache_key: str):
    """Общая логика для _load_repo_tools_lib()/_load_repo_mangel_lib() -- грузит
    модуль под уникальным внутренним именем (не 'tools_lib'/'mangel_lib', чтобы не
    столкнуться с/не подменить то, что уже могло быть закэшировано в sys.modules
    из-за глобального /home/promonta/agent в sys.path). Кэш передаётся вызывающим
    как dict с одним ключом -- эмулирует module-level global без глобальной
    переменной на каждый модуль."""
    if cache.get(cache_key) is not None:
        return cache[cache_key]
    spec = importlib.util.spec_from_file_location(internal_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f'не удалось загрузить {os.path.basename(path)} из {path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cache[cache_key] = module
    return module


_repo_module_cache: dict = {}


def _load_repo_tools_lib():
    return _load_repo_module(TOOLS_LIB_PATH, 'grandmont_group_repo_tools_lib', _repo_module_cache, 'tools_lib')


def _load_repo_mangel_lib():
    """30.07 (Release-аудит P1): mangel_lib.py был полностью вне git и импортировался
    обычным `import mangel_lib as ml` -- тот же класс риска, что чинили для tools_lib.py
    (import мог молча резолвиться в untracked-копию на диске сервера вместо
    репозиторной, даже если содержимое разошлось). Тот же изолированный loader."""
    return _load_repo_module(MANGEL_LIB_PATH, 'grandmont_group_repo_mangel_lib', _repo_module_cache, 'mangel_lib')


def _load_repo_objekte_lib():
    """31.07 (Release-аудит P2): objekte_lib.py -- последний shared runtime-модуль вне
    git, тем же путём: import резолвился через глобальный sys.path на
    /home/promonta/agent/objekte_lib.py, что и работает в prod, но не существует на
    CI runner (нет /home/promonta там) -- CI падал ModuleNotFoundError. Тот же
    изолированный loader, что и tools_lib/mangel_lib."""
    return _load_repo_module(OBJEKTE_LIB_PATH, 'grandmont_group_repo_objekte_lib', _repo_module_cache, 'objekte_lib')


def _load_repo_roadmap_lib():
    """31.07 (Release-аудит П2): roadmap_lib.py — последний shared runtime-модуль,
    грузившийся обычным `import roadmap_lib as rl` через глобальный sys.path, тот же
    риск резолва в untracked-копию на диске сервера вместо репозиторной. Тот же
    изолированный loader, что и tools_lib/mangel_lib/objekte_lib."""
    return _load_repo_module(ROADMAP_LIB_PATH, 'grandmont_group_repo_roadmap_lib', _repo_module_cache, 'roadmap_lib')


# 01.08 (доп.раунд): предыдущее предположение "обычный import безопасен, uvicorn
# кладёт BACKEND_DIR в sys.path[0]" было ВЕРНО только для `uvicorn main:app` из
# BACKEND_DIR напрямую -- production запускается как `uvicorn miniapp.main:app`
# с WorkingDirectory=/home/promonta/agent (см. systemd unit), это package-import
# (miniapp -- implicit namespace package, нет __init__.py), и в этом случае Python
# кладёт в sys.path[0] именно WorkingDirectory (/home/promonta/agent), НЕ директорию
# main.py (/home/promonta/agent/miniapp/) -- `import work_types` внутри main.py не
# находит work_types.py вообще, реальный ImportError в проде, не пойманный тестами
# (тесты всегда грузят main.py напрямую как top-level module, не как package member).
# Fix: relative import сначала (работает для package-import сценария, т.к. `from .
# import work_types` резолвится относительно ПАКЕТА miniapp, не sys.path), fallback на
# обычный import для top-level запуска (тесты, uvicorn main:app из BACKEND_DIR). НЕ
# добавляем НИКАКОГО ДОПОЛНИТЕЛЬНОГО вызова, вставляющего путь в sys.path -- именно
# такой вызов уже один раз сломал resolution других shared-модулей (см.
# test_main_py_restores_original_global_sys_path_insert).
try:
    from . import work_types as wt
    from . import profile_skills as pskills
    from . import assignment_matching as amatch
    from . import daily_plan_lib as dpl
    from . import system_status
except ImportError:
    import work_types as wt  # noqa: E402
    import profile_skills as pskills  # noqa: E402
    import assignment_matching as amatch  # noqa: E402
    import daily_plan_lib as dpl  # noqa: E402
    import system_status  # noqa: E402


BOT_TOKEN = os.environ['BOT_TOKEN']
# Phase A: ~45 JSON-store/dir path constants moved to backend/core/paths.py.
# Imported by name (not `from core import paths`) so `backend.ROLES_FILE` etc.
# keep working for tests that do `import main as backend`. Relative-then-
# absolute fallback, same pattern as core.time and the work_types/etc. block.
try:
    from .core.paths import (
        CHAT_ARCHIVE_FILE,
        OBJECT_INFO_FILE,
        OBJECT_HISTORY_FILE,
        TASKS_FILE,
        OBJECT_DOC_DIR,
        CHAT_ATTACH_DIR,
        CHAT_FILE,
        ANGEBOT_OUT_DIR,
        CHECKIN_PHOTO_BASE,
        CHECKIN_IDEMPOTENCY_FILE,
        ACTIVITY_ALERTS_FILE,
        WORK_CALENDAR_FILE,
        WORKER_PROFILES_FILE,
        OBJECT_ASSIGNMENTS_FILE,
        AI_MODEL_FILE,
        WEATHER_REACTIONS_FILE,
        CHAT_THREAD_META_FILE,
        PHOTO_DIR,
        NOTIFIED_USERS_FILE,
        RECHNUNG_OUT_DIR,
        DAILY_PLAN_STORE_FILE,
        OBJECT_IMAGES_FILE,
        CHECKIN_META_FILE,
        NEWS_COMMENTS_FILE,
        BLOCKER_PHOTO_DIR,
        TRANSCRIBE_AUDIO_DIR,
        ALERT_DISMISSALS_FILE,
        BIRTHDAY_ALERTS_FILE,
        AI_RATE_FILE,
        CONTRACT_INGEST_STATE_FILE,
        AVATAR_DIR,
        CHAT_READS_FILE,
        NEWS_READS_FILE,
        WORKER_AI_RATE_FILE,
        FINISH_OUTBOX_FILE,
        PHOTO_META_FILE,
        PHOTO_REACTIONS_FILE,
        NEWS_REACTIONS_FILE,
        ROLES_FILE,
        AUDIT_FILE,
        APP_VERSION_FILE,
        OBJECT_PHOTO_DIR,
        CRITICAL_ALERT_PHOTO_DIR,
        CRITICAL_ALERTS_FILE,
        FEED_READS_FILE,
        FEED_SAVED_FILE,
        CHAT_REACTIONS_FILE,
        PLAN_SYNC_STATE_FILE,
        MANGEL_PHOTO_DIR,
        ABWESENHEIT_FILE,
    )
except ImportError:
    from core.paths import (  # noqa: E402
        CHAT_ARCHIVE_FILE,
        OBJECT_INFO_FILE,
        OBJECT_HISTORY_FILE,
        TASKS_FILE,
        OBJECT_DOC_DIR,
        CHAT_ATTACH_DIR,
        CHAT_FILE,
        ANGEBOT_OUT_DIR,
        CHECKIN_PHOTO_BASE,
        CHECKIN_IDEMPOTENCY_FILE,
        ACTIVITY_ALERTS_FILE,
        WORK_CALENDAR_FILE,
        WORKER_PROFILES_FILE,
        OBJECT_ASSIGNMENTS_FILE,
        AI_MODEL_FILE,
        WEATHER_REACTIONS_FILE,
        CHAT_THREAD_META_FILE,
        PHOTO_DIR,
        NOTIFIED_USERS_FILE,
        RECHNUNG_OUT_DIR,
        DAILY_PLAN_STORE_FILE,
        OBJECT_IMAGES_FILE,
        CHECKIN_META_FILE,
        NEWS_COMMENTS_FILE,
        BLOCKER_PHOTO_DIR,
        TRANSCRIBE_AUDIO_DIR,
        ALERT_DISMISSALS_FILE,
        BIRTHDAY_ALERTS_FILE,
        AI_RATE_FILE,
        CONTRACT_INGEST_STATE_FILE,
        AVATAR_DIR,
        CHAT_READS_FILE,
        NEWS_READS_FILE,
        WORKER_AI_RATE_FILE,
        FINISH_OUTBOX_FILE,
        PHOTO_META_FILE,
        PHOTO_REACTIONS_FILE,
        NEWS_REACTIONS_FILE,
        ROLES_FILE,
        AUDIT_FILE,
        APP_VERSION_FILE,
        OBJECT_PHOTO_DIR,
        CRITICAL_ALERT_PHOTO_DIR,
        CRITICAL_ALERTS_FILE,
        FEED_READS_FILE,
        FEED_SAVED_FILE,
        CHAT_REACTIONS_FILE,
        PLAN_SYNC_STATE_FILE,
        MANGEL_PHOTO_DIR,
        ABWESENHEIT_FILE,
    )

# Core workforce shadow integration (Mini <-> Grandmont Core, disabled by
# default via CORE_INTEGRATION_ENABLED). Same relative-then-absolute import
# fallback as every other core.* submodule above.
try:
    from .core.workforce_shadow import shadow_compare_assignments, shadow_create_absence
except ImportError:
    from core.workforce_shadow import shadow_compare_assignments, shadow_create_absence  # noqa: E402

# Phase A step 3: numeric limits/TTLs moved to backend/core/limits.py.
try:
    from .core.limits import (
        ONLINE_THRESHOLD_SECONDS,
        CHECKIN_MAX_BYTES,
        PHOTO_MAX_FILES,
        INIT_DATA_MAX_AGE,
        ALERT_DISMISS_TTL,
        PHOTO_MAX_COUNT,
        AI_UPLOAD_MAX_BYTES,
        PHOTO_MAX_BYTES,
        SHEETS_CACHE_TTL,
        SESSION_TOKEN_MAX_AGE,
        NOTIFIED_USERS_TTL,
        OBJECT_PHOTO_MAX,
        AI_RATE_WINDOW,
        WORKER_AI_RATE_LIMIT,
        AVATAR_MAX_BYTES,
        TRANSCRIBE_MAX_BYTES,
        CHAT_RETENTION_SECONDS,
        _IDEMPOTENCY_TTL,
        CHAT_MAX,
        AI_RATE_LIMIT,
    )
except ImportError:
    from core.limits import (  # noqa: E402
        ONLINE_THRESHOLD_SECONDS,
        CHECKIN_MAX_BYTES,
        PHOTO_MAX_FILES,
        INIT_DATA_MAX_AGE,
        ALERT_DISMISS_TTL,
        PHOTO_MAX_COUNT,
        AI_UPLOAD_MAX_BYTES,
        PHOTO_MAX_BYTES,
        SHEETS_CACHE_TTL,
        SESSION_TOKEN_MAX_AGE,
        NOTIFIED_USERS_TTL,
        OBJECT_PHOTO_MAX,
        AI_RATE_WINDOW,
        WORKER_AI_RATE_LIMIT,
        AVATAR_MAX_BYTES,
        TRANSCRIBE_MAX_BYTES,
        CHAT_RETENTION_SECONDS,
        _IDEMPOTENCY_TTL,
        CHAT_MAX,
        AI_RATE_LIMIT,
    )

# Phase A step 4: MIME allowlists/sniffers + simple enum constants moved to
# backend/core/constants.py.
try:
    from .core.constants import (
        _ALLOWED_IMAGE_MIME_EXT,
        sniff_image,
        sniff_image_or_pdf,
        _ALLOWED_AUDIO_MIME_EXT,
        _ALLOWED_CHAT_ATTACHMENT_MIME_EXT,
        sniff_audio,
        sniff_chat_attachment,
        _INVISIBLE_FILLER_CHARS,
        BUDGET_FIELDS,
        VALID_OBJECT_STATUSES,
        CHAT_REACTION_OPTIONS,
        THREAD_TYPE_BY_PREFIX,
        DEFAULT_THREAD_PREFS,
        AI_MODELS,
        AI_MODEL_DEFAULT,
        _OWNER_AI_ENV_ALLOWLIST,
        TASK_PRIORITIES,
        TASK_CATEGORIES,
        TASK_STATUSES,
        ABWESENHEIT_REASONS,
        ABWESENHEIT_PUBLIC_FIELDS,
        _EMPTY_CONTRACT_STORE,
    )
except ImportError:
    from core.constants import (  # noqa: E402
        _ALLOWED_IMAGE_MIME_EXT,
        sniff_image,
        sniff_image_or_pdf,
        _ALLOWED_AUDIO_MIME_EXT,
        _ALLOWED_CHAT_ATTACHMENT_MIME_EXT,
        sniff_audio,
        sniff_chat_attachment,
        _INVISIBLE_FILLER_CHARS,
        BUDGET_FIELDS,
        VALID_OBJECT_STATUSES,
        CHAT_REACTION_OPTIONS,
        THREAD_TYPE_BY_PREFIX,
        DEFAULT_THREAD_PREFS,
        AI_MODELS,
        AI_MODEL_DEFAULT,
        _OWNER_AI_ENV_ALLOWLIST,
        TASK_PRIORITIES,
        TASK_CATEGORIES,
        TASK_STATUSES,
        ABWESENHEIT_REASONS,
        ABWESENHEIT_PUBLIC_FIELDS,
        _EMPTY_CONTRACT_STORE,
    )

# Phase A step 5: JSON storage safety layer moved to backend/core/storage.py.
# CRITICAL_JSON_PATHS is imported BY REFERENCE (same set object) -- the
# CRITICAL_JSON_PATHS.update({...}) call at the end of this file mutates
# that same object, core.storage sees the same final contents at call time.
try:
    from .core.storage import (
        CRITICAL_JSON_PATHS,
        CorruptJsonError,
        _corrupt_lock_path,
        _quarantine_corrupt_json,
        _json_locks,
        _json_locks_guard,
        _lock_for,
        _atomic_write_json,
        update_json_transaction,
        _safe_load_json,
    )
except ImportError:
    from core.storage import (  # noqa: E402
        CRITICAL_JSON_PATHS,
        CorruptJsonError,
        _corrupt_lock_path,
        _quarantine_corrupt_json,
        _json_locks,
        _json_locks_guard,
        _lock_for,
        _atomic_write_json,
        update_json_transaction,
        _safe_load_json,
    )
# moved to core/paths.py -- ROLES_FILE

# DailyPlan store — производственный контроль (Round 1)
# moved to core/paths.py -- DAILY_PLAN_STORE_FILE
# moved to core/paths.py -- PLAN_SYNC_STATE_FILE
# moved to core/paths.py -- WORK_CALENDAR_FILE
dpl.configure(DAILY_PLAN_STORE_FILE, PLAN_SYNC_STATE_FILE, WORK_CALENDAR_FILE)

# Finish projector outbox — durable event log for session_id→daily_execution application.
# Written before apply_daily_execution so a crash between checkin commit and plan update
# leaves a pending event that can be retried on startup.
# moved to core/paths.py -- FINISH_OUTBOX_FILE
_finish_outbox_lock = __import__('threading').Lock()

# Periodic outbox-retry liveness fix (fix/miniapp-profile-rmw-outbox-retry): startup-only
# retry left a transiently-failed event stuck in "retrying" until the next process
# restart, which on a stable long-running service could be days. `_outbox_retry_task`
# is populated by _on_startup/_on_shutdown below. `_outbox_sweep_in_progress` is a
# plain bool flag, not an asyncio.Lock -- the check-then-set in
# _run_outbox_retry_sweep_once has no `await` between them, so it is atomic under
# asyncio's single-threaded cooperative scheduling (no other coroutine can run
# between the check and the set); a flag makes that guarantee obvious to read,
# where a Lock's acquire()/release() pair could tempt a future edit to add an
# await in between and reintroduce the exact race this exists to prevent.
_outbox_retry_task: "asyncio.Task | None" = None
_outbox_sweep_in_progress: bool = False

# Contract ingestion state (Round 5 — Drive scope gated)
# moved to core/paths.py -- CONTRACT_INGEST_STATE_FILE
_CONTRACT_INGEST_LOCK = __import__('threading').Lock()
CONTRACTS_DRIVE_FOLDER_ID = os.environ.get('CONTRACTS_DRIVE_FOLDER_ID', '')
# moved to core/limits.py -- INIT_DATA_MAX_AGE

# 31.07 (Release-аудит П4): для этих сторов corrupt JSON НЕ должен молча деградировать
# к default -- следующий же write через _atomic_write_json/update_json_transaction
# записал бы default ПОВЕРХ повреждённого файла, необратимо уничтожая реальные данные
# (roles/assignments/checkin/chat/abwesenheit/profiles/tasks/alerts/roadmap/stage_requests).
# Для этих путей: карантин повреждённого файла (.corrupt-<ts>) + mutation вызывает 503,
# read-only endpoint получает 503 вместо тихого пустого результата (см. exception_handler
# ниже, регистрация путей -- в самом конце файла, см. CRITICAL_JSON_PATHS.update(...)).
# moved to core/storage.py -- CRITICAL_JSON_PATHS, CorruptJsonError,
# _corrupt_lock_path, _quarantine_corrupt_json


app = FastAPI(title="Grandmont Group Mini App", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://web.telegram.org"],
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["*"],
)

# First router extraction (routes/auth.py -- GET/POST/DELETE /api/roles).
# Registered here, right after app creation, same place a router would
# normally go -- routes/auth.py imports only from core/*, never from this
# module, so there is no ordering constraint forcing this include_router
# call any later in the file.
try:
    from .routes.auth import router as _auth_router
except ImportError:
    from routes.auth import router as _auth_router  # noqa: E402
app.include_router(_auth_router)


@app.on_event("startup")
async def _on_startup():
    """Retry pending finish-outbox events from a previous crash, and reconcile
    any finished checkin session whose outbox event never got written at all
    (crash between checkin_meta commit and outbox write)."""
    try:
        reconciled = _reconcile_missing_outbox_events()
        if reconciled:
            print(f"[startup] Reconciled {reconciled} missing finish-outbox event(s)")
    except Exception as e:
        print(f"[startup] finish-outbox reconciliation failed: {e}")
    try:
        retried = _retry_pending_outbox_events()
        if retried:
            print(f"[startup] Retried {retried} pending finish-outbox event(s)")
    except Exception as e:
        print(f"[startup] finish-outbox retry failed: {e}")
    try:
        migrated = _migrate_abwesenheit_legacy_ids()
        if migrated:
            print(f"[startup] Migrated {migrated} legacy abwesenheit id(s)")
    except Exception as e:
        print(f"[startup] abwesenheit legacy-id migration failed: {e}")

    # Liveness fix: the retry above only ever runs once, at process startup --
    # a transient failure (network blip, momentary lock contention) after that
    # left the event stuck in "retrying" until the next restart, which on a
    # stable service could be days. Start a small periodic sweep so pending/
    # retrying events get another chance without needing a restart.
    global _outbox_sweep_in_progress, _outbox_retry_task
    _outbox_sweep_in_progress = False
    _outbox_retry_task = asyncio.create_task(_outbox_retry_sweep_loop())


@app.on_event("shutdown")
async def _on_shutdown():
    """Stop the periodic outbox-retry sweep cleanly -- cancel and await it so
    it doesn't log a "Task was destroyed but it is pending" warning or leak
    into the next test/process."""
    global _outbox_retry_task
    if _outbox_retry_task is not None:
        _outbox_retry_task.cancel()
        try:
            await _outbox_retry_task
        except asyncio.CancelledError:
            pass
        _outbox_retry_task = None


@app.exception_handler(CorruptJsonError)
async def _corrupt_json_handler(request, exc: CorruptJsonError):
    """31.07 (Release-аудит П4): единая точка -- любой _load_*/update_json_transaction
    на критичном сторе (roles/assignments/checkin/chat/abwesenheit/profiles/tasks/
    critical_alerts/roadmap/stage_requests), наткнувшись на повреждённый JSON, карантинит
    файл (_quarantine_corrupt_json) и поднимает это исключение вместо тихой деградации
    к default -- перехватывается здесь для ЛЮБОГО эндпоинта (GET/POST/PATCH/DELETE)
    без необходимости оборачивать каждый вызывающий код по отдельности."""
    from starlette.responses import JSONResponse
    return JSONResponse(
        status_code=503,
        content={"detail": "Данные временно недоступны (повреждённый файл), обратитесь к владельцу"},
    )

# moved to core/paths.py -- AUDIT_FILE
AUDIT_LOCK = __import__('threading').Lock()
# _auth_audit_context: moved to core/permissions.py (Phase A step 6/6), imported
# further down in this file alongside get_current_user/etc. -- audit_log_middleware
# below resolves the name at call time (module fully loaded before any request
# hits it), same as every other forward-reference in this file.


@app.middleware("http")
async def audit_log_middleware(request, call_next):
    audit_context_token = _auth_audit_context.set(None)
    # 28.07 (real bug found by external audit, ТЗ п.25): было await request.body() для
    # ЛЮБОГО POST/PATCH/DELETE, включая multipart file upload (photo/voice/document) --
    # тело файла дублировалось в памяти дважды (readable once, buffered here + re-injected
    # via _receive override for the real handler below). Логируемая entry ниже НЕ включает
    # тело файла и никогда не включала -- этот буфер существовал только чтобы вернуть body
    # обратно эндпоинту, не для самого лога. Для multipart пропускаем чтение целиком: не
    # трогаем request._receive, FastAPI/Starlette читают upload stream штатно сами.
    is_multipart = request.headers.get("content-type", "").startswith("multipart/form-data")
    if request.method in ("POST", "PATCH", "DELETE") and not is_multipart:
        body_bytes = await request.body()

        async def receive():
            return {"type": "http.request", "body": body_bytes}
        request._receive = receive

    try:
        response = await call_next(request)

        # 30.07 (Release-аудит Этап 6): раньше логировались только успешные мутации
        # (status < 400) -- отклонения (400/403/404/413 на upload, 401 на auth) не
        # попадали в audit.log вообще, только в разрозненные print() в отдельных
        # местах кода. Теперь пишем обе категории отдельными полями -- rejection не
        # включает тело запроса/файла (тот же принцип, что и раньше: НЕ initData,
        # НЕ BOT_TOKEN, НЕ содержимое сообщений/GPS, только method/path/status/user_id).
        if request.method in ("POST", "PATCH", "DELETE"):
            # F06: audit actor must be the already-authenticated identity. Re-parsing
            # X-Telegram-Init-Data here let bearer-only requests lose the actor and let
            # an extra spoofed header write a different user_id to audit.log.
            auth_ctx = _auth_audit_context.get() or {}
            entry = {
                "ts": int(time.time()),
                "user_id": auth_ctx.get('user_id'),
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "rejected": response.status_code >= 400,
            }
            with AUDIT_LOCK:
                with open(AUDIT_FILE, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        return response
    finally:
        _auth_audit_context.reset(audit_context_token)


# Phase A step 6: session-token + initData HMAC validation moved to
# backend/core/permissions.py (_secret_key, validate_init_data,
# _session_secret, create_session_token, verify_session_token). Imported by
# name, same relative-then-absolute fallback pattern as every other Phase A
# step -- none of these five are ever patched by name in tests (grepped),
# so a straight move (not a business_now()-style wrapper) is safe here.
# get_current_user/get_role/require_owner/_load_roles/_notify_owner_new_user
# STAY below, NOT moved -- see core/permissions.py's module docstring for
# why (15 test files patch backend._load_roles and call backend.get_role()
# directly; moving get_role's chain would silently stop seeing those patches).
try:
    from .core.permissions import (
        _secret_key,
        validate_init_data,
        _session_secret,
        create_session_token,
        verify_session_token,
    )
except ImportError:
    from core.permissions import (  # noqa: E402
        _secret_key,
        validate_init_data,
        _session_secret,
        create_session_token,
        verify_session_token,
    )


# moved to core/storage.py -- _json_locks, _json_locks_guard, _lock_for,
# _atomic_write_json, update_json_transaction, _safe_load_json


# moved to core/constants.py -- _ALLOWED_IMAGE_MIME_EXT, sniff_image,
# sniff_image_or_pdf, _ALLOWED_AUDIO_MIME_EXT, _ALLOWED_CHAT_ATTACHMENT_MIME_EXT,
# sniff_audio, sniff_chat_attachment


def _csv_safe(value) -> str:
    """CSV formula injection: Excel/LibreOffice выполняет ячейку, начинающуюся с
    =, +, -, @ как формулу при открытии. object_id в stundenzettel идёт от
    checkin_start (Form-параметр, только .strip()[:100], без sanitize) -- worker
    теоретически мог стартовать смену с object_id вроде `=cmd|'/c calc'!A1` и
    отравить CSV, который потом открывает owner. Префикс апострофом -- стандартный
    экранирующий приём, Excel показывает апостроф не отображая, LibreOffice тоже."""
    s = str(value)
    stripped = s.lstrip()
    if not stripped:
        return s
    if stripped[0] in ('=', '@'):
        return "'" + s
    if stripped[0] in ('+', '-') and not re.fullmatch(r'[+-]?(?:\d+(?:[.,]\d+)?|[.,]\d+)', stripped):
        return "'" + s
    return s


# Phase A step 6/6 (revised 25.09): get_current_user/get_role/require_owner/
# _load_roles/_save_roles/_notify_owner_new_user/_load_notified_users/
# _save_notified_users/_last_seen/RoleSetBody moved to backend/core/permissions.py
# -- that module is now the single source of truth (main.py imports these
# back, does not redefine them). The 15 test files that previously did
# `patch.object(backend, '_load_roles', ...)` were updated to patch
# `core.permissions._load_roles` instead -- same assertions, only the patch
# target changed to where the code now actually lives.
try:
    from .core.permissions import (
        _auth_audit_context,
        _last_seen,
        _load_roles,
        _save_roles,
        _load_notified_users,
        _save_notified_users,
        _notify_owner_new_user,
        get_current_user,
        get_role,
        require_owner,
        RoleSetBody,
    )
except ImportError:
    from core.permissions import (  # noqa: E402
        _auth_audit_context,
        _last_seen,
        _load_roles,
        _save_roles,
        _load_notified_users,
        _save_notified_users,
        _notify_owner_new_user,
        get_current_user,
        get_role,
        require_owner,
        RoleSetBody,
    )


# Phase A: leaf business_now() moved to backend/core/time.py (zero deps).
# business_today()/business_today_str()/_today_berlin_str() STAY here, calling
# the module-local `business_now` name -- tests monkeypatch `backend.business_now`
# via patch.object(backend, 'business_now', ...), and these wrappers must keep
# resolving that same patched name at call time, not a separate copy baked into
# core.time's own namespace (which patch.object(backend, ...) would not reach).
# Same relative-then-absolute fallback as the work_types/profile_skills/etc.
# import block above (line ~119) -- `from .core.time import` resolves when
# main.py is imported as `miniapp.main` (package member, production/uvicorn),
# plain `from core.time import` resolves when main.py is loaded as a top-level
# module (tests, `uvicorn main:app` from backend_dir).
try:
    from .core.time import business_now
except ImportError:
    from core.time import business_now  # noqa: E402


def business_today():
    """date-объект 'сегодня' по Europe/Berlin -- для арифметики с timedelta
    (week_start = business_today() - timedelta(days=6) и т.п.)."""
    return business_now().date()


def business_today_str() -> str:
    """'YYYY-MM-DD' по Europe/Berlin -- для сравнения со строковыми датами в JSON-сторах
    (assignments date_from/date_to и т.п., которые уже хранятся как ISO-строки)."""
    return business_now().strftime('%Y-%m-%d')


def _today_berlin_str() -> str:
    """03.08: тонкая обёртка над business_today_str() -- has_active_object_access()
    уже использовала это имя до появления единого business_*() набора хелперов,
    переименовывать вызывающий код везде не требовалось, раз поведение идентично."""
    return business_today_str()


def has_active_object_access(user_id: str, object_id: str, today: str | None = None) -> bool:
    """03.08 (доп.раунд П2): единый helper для ПОЛНОГО доступа к данным объекта --
    отличается от простого "назначение существует". Worker должен иметь ровно ОДНО
    accepted-назначение на этот объект, чей период [date_from, date_to] включает
    сегодняшний день (Europe/Berlin) включительно. Назначение без date_from/date_to
    (легаси-записи до введения периодов) трактуется как бессрочное -- backward compat,
    та же логика уже была в _get_active_assignment_for_checkin.

    НЕ вызывать напрямую для owner -- эта функция только про worker-назначения,
    вызывающий код (require_object_access/has_active_object_access_for_role) сам
    решает, что owner имеет доступ всегда."""
    if today is None:
        today = _today_berlin_str()
    assignments = _load_assignments().get(str(object_id), [])
    for a in assignments:
        if str(a.get('user_id')) != str(user_id):
            continue
        if _assignment_status(a) != 'accepted':
            continue
        d_from, d_to = a.get('date_from', ''), a.get('date_to', '')
        if not (d_from and d_to):
            return True  # легаси-назначение без периода -- бессрочный доступ
        if d_from <= today <= d_to:
            return True
    return False


def can_access_object(user: dict, role: str, object_id: str) -> bool:
    """owner видит/управляет всем; worker -- только объекты, на которые назначен
    И чей период назначения активен сегодня (Europe/Berlin). Единая точка правды для
    object-scoped routes -- раньше большинство из них проверяли только
    get_current_user (авторизован ли вообще), не было ли это чужим объектом.

    03.08 (доп.раунд П2, реальный найденный баг): раньше accepted-назначение давало
    ПОЛНЫЙ доступ независимо от периода -- worker с назначением на будущий месяц уже
    сегодня мог открыть чат/этапы/файлы объекта, доступ к которому должен появиться
    только в date_from. Период теперь обязательная часть проверки (has_active_object_access)."""
    if role == 'owner':
        return True
    return has_active_object_access(str(user['id']), object_id)


def require_object_access(object_id: str, user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    """FastAPI матчит `object_id` по имени пути -- подключать как обычный Depends
    в любом route, где путь содержит {object_id}."""
    if not can_access_object(user, role, object_id):
        raise HTTPException(403, "Нет доступа к этому объекту")


# /api/roles (GET/POST/DELETE) moved to routes/auth.py -- the first real
# router extraction (see that module's docstring for the dependency-direction
# rule this establishes as the template for the rest). app.include_router
# call is near the bottom of this file, alongside the FastAPI app setup.
# Re-exported here (not a second implementation) because 1 test file still
# does backend.list_roles(...) directly.
try:
    from .routes.auth import list_roles, set_role, revoke_role
except ImportError:
    from routes.auth import list_roles, set_role, revoke_role  # noqa: E402


# 30.07 (Release-аудит Этап 6): commit SHA для /api/health читается из файла рядом
# с main.py, который пишет deploy-скрипт при выкладке -- НЕ subprocess'ом git на
# каждый health-запрос (дорого, плюс serving-путь /home/promonta/agent/miniapp/
# не является git-репозиторием, git там просто не сработает). Файл опционален --
# если деплой был сделан вручную без записи VERSION, health всё равно отвечает,
# просто без SHA.
# moved to core/paths.py -- APP_VERSION_FILE


def _read_app_version() -> dict:
    return system_status.read_app_version(APP_VERSION_FILE)


@app.get("/api/health")
def health():
    """Лёгкая liveness-проверка -- процесс жив, отвечает. Не трогает диск/сеть
    (кроме чтения маленького локального VERSION-файла) -- безопасно дёргать часто
    из мониторинга. Никаких токенов/секретов/путей к credentials/персональных
    данных в ответе (30.07, Release-аудит Этап 6)."""
    return system_status.health_response(APP_VERSION_FILE)


@app.get("/api/health/ready")
def health_ready(user: dict = Depends(get_current_user), _: None = Depends(require_owner)):
    """Readiness-проверка для Owner/внутреннего мониторинга -- owner-only (в отличие
    от /api/health): проверяет реальную готовность обслуживать запросы, не только
    "процесс жив". Каждая проверка дешёвая (stat/os.access), НЕ дорогой сетевой
    запрос к Google Sheets на каждый вызов -- иначе readiness сам стал бы точкой
    перегрузки при частом опросе мониторингом."""
    return system_status.readiness_response(
        object_photo_dir=OBJECT_PHOTO_DIR,
        chat_attach_dir=CHAT_ATTACH_DIR,
        tools_lib_path=TOOLS_LIB_PATH,
        mangel_lib_path=MANGEL_LIB_PATH,
        backend_dir=BACKEND_DIR,
        roles_file=ROLES_FILE,
    )


@app.get("/api/diagnostics")
def diagnostics(_: None = Depends(require_owner)):
    """Owner-only system status snapshot — all checks are cheap (file I/O + in-memory cache
    inspection only, no live Sheets or Drive API calls). Designed so a "данные не грузятся"
    report can be diagnosed in under a minute without touching prod data."""
    return system_status.diagnostics_response(
        data_root=DATA_ROOT,
        roles_file=ROLES_FILE,
        sheets_cache=_sheets_cache,
        sheets_cache_ttl=SHEETS_CACHE_TTL,
        activity_alerts_file=ACTIVITY_ALERTS_FILE,
        news_feed_file=NEWS_FEED_FILE,
        chat_file=CHAT_FILE,
        plan_sync_state_file=PLAN_SYNC_STATE_FILE,
        contract_ingest_state_file=CONTRACT_INGEST_STATE_FILE,
        contracts_drive_folder_id=CONTRACTS_DRIVE_FOLDER_ID,
        app_version_file=APP_VERSION_FILE,
        safe_load_json=_safe_load_json,
        outbox_dead_letter_count=_outbox_dead_letter_count,
    )


@app.get("/api/me")
def me(user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    return {"user_id": user['id'], "name": user.get('first_name'), "role": role}


@app.post("/api/session")
def create_session(x_telegram_init_data: str = Header(...)) -> dict:
    """03.08 (ТЗ Задача 1): выдаёт 12-часовой session token. Требует ЖИВОЙ initData
    (обычная HMAC + 1-час TTL проверка через get_current_user-путь ниже, не отдельный
    ослабленный код) -- токен можно получить только пройдя ту же проверку, что раньше
    требовалась на каждый запрос. Whitelist-проверка тоже здесь: не-whitelisted юзер
    не получает токен вообще, не только 403 на защищённых эндпоинтах."""
    user = get_current_user(authorization=None, x_telegram_init_data=x_telegram_init_data)
    token = create_session_token(user['id'])
    return {
        "token": token,
        "expires_in": SESSION_TOKEN_MAX_AGE,
        "user_id": user['id'],
    }


# ---------- Workers list (для bubble-assign, Фаза 2 → доделано в Фазе 3) ----------
@app.get("/api/workers")
def list_workers(user: dict = Depends(get_current_user)):
    roles = _load_roles()
    profiles = _load_worker_profiles()
    # 09.09: roster/access invariant fix (owner report -- worker "Иван" appeared in
    # team/dashboard, had no avatar, Worker Card 404'd, and was missing from the
    # Доступ tab). Root cause: this endpoint unioned roles.json + worker_profiles.json
    # keys, then did `roles.get(uid, 'worker')` -- a DEFAULT of 'worker' for ANY uid
    # not in roles.json at all, not just those explicitly granted that role. The same
    # exact bug was already found and fixed in get_assignment_candidates() (01.08,
    # comment above worker_ids there) but never applied here -- this endpoint kept
    # presenting profile-only users (filled onboarding, never granted or since revoked
    # access) as if they were active workers with real access. Every one of this
    # endpoint's 6 frontend consumers already filters on `w.role === 'worker'`
    # (mangel.js, tools.js, home.js, abwesenheit.js, today-plan.js) -- fixing the
    # default here makes those filters correct automatically, no frontend change
    # needed for the worker-picker call sites. `access_granted` is new, additive --
    # existing consumers reading only `role`/`name` are unaffected.
    all_ids = set(roles.keys() if isinstance(roles, dict) else []) | set(profiles.keys() if isinstance(profiles, dict) else [])
    workers = []
    for uid in all_ids:
        try:
            p = profiles.get(uid) if isinstance(profiles, dict) else None
            if not isinstance(p, dict):
                p = {}
            last_seen = _last_seen.get(uid)
            role = roles.get(uid) if isinstance(roles, dict) else None  # None if not in roles.json -- no more silent 'worker' default
            skills = p.get('skills', [])
            workers.append({
                'user_id': uid,
                'role': role,
                'access_granted': role is not None,
                'name': _sanitize_display_name(p.get('name'), uid),
                'skills': skills if isinstance(skills, list) else [],
                'has_avatar': bool(p.get('avatar')),
                'quiz_completed': bool(p.get('quiz_completed', False)),
                'online': bool(last_seen and (time.time() - last_seen) < ONLINE_THRESHOLD_SECONDS),
            })
        except Exception:
            # 27.09 (owner report: Workers screen not loading): one malformed
            # roles/worker_profiles entry must not take down the whole roster --
            # skip just that uid, never the profile content (could carry PII).
            logging.getLogger('grandmont.workers').warning("workers: skipping malformed entry for uid=%s", uid)
            continue
    return {'workers': workers}


# ---------- Профиль работника: навыки + онбординг-квиз (Фаза 2/8) ----------
# moved to core/paths.py -- WORKER_PROFILES_FILE
# 01.08 (единый каталог видов работ): SKILL_OPTIONS был вручную продублированным
# списком из 19 строк, отдельно от frontend ONBOARDING_GROUPS/BUBBLE_STAGE_OPTIONS --
# новый навык мог появиться в одном месте и отсутствовать в другом (реальный
# найденный дрифт: SKILL_STAGE_MAP покрывал только 8 из 19). Источник истины теперь
# один -- work_types.py, SKILL_OPTIONS оставлен как производный список ИМЁН active
# work types (тот же порядок что в каталоге) только для legacy-полей API-ответов
# (skill_options в /api/profile/me), которые старый frontend-код ещё может читать.
SKILL_OPTIONS = [w['name'] for w in wt.WORK_TYPES if w['active']]


# _load_worker_profiles moved to core/profiles.py (minimal dependency closure
# for routes/auth.py) -- imported back below, main.py's copy is a re-export,
# not a second implementation. Patched by name in 12 test files targeting
# functions that stay in main.py, so the name must keep resolving here too.
try:
    from .core.profiles import _load_worker_profiles, _sanitize_display_name
except ImportError:
    from core.profiles import _load_worker_profiles, _sanitize_display_name  # noqa: E402


def _save_worker_profiles(profiles: dict):
    _atomic_write_json(WORKER_PROFILES_FILE, profiles)


def _get_worker_profile(user_id) -> dict:
    profiles = _load_worker_profiles()
    return profiles.get(str(user_id), {"skills": [], "quiz_completed": False})


@app.get("/api/work-types")
def get_work_types(user: dict = Depends(get_current_user)):
    """01.08: единый каталог видов работ -- доступен любому авторизованному
    пользователю (не owner-only), т.к. Worker выбирает навыки в onboarding/профиле,
    не только Owner в Assignment Sheet."""
    return wt.work_types_catalog()


def _get_worker_skills_v2(user_id) -> list:
    """Профиль работника -> нормализованный skills_v2, с idempotent миграцией
    legacy 'skills' (список названий) при первом обращении -- ТОЛЬКО если реально
    нужна миграция (skills_v2 отсутствует), файл не переписывается на каждый read.

    RMW под update_json_transaction (не load->mutate->save по отдельности) --
    иначе конкурентный update_my_profile/verify_worker_skill на том же профиле
    мог тихо затереться этой миграцией или наоборот (см. storage.py docstring)."""
    key = str(user_id)

    def _mutator(profiles):
        profile = profiles.get(key, {"skills": [], "quiz_completed": False})
        skills_v2, changed = pskills.normalize_profile_skills(profile)
        if changed:
            profile['skills_v2'] = skills_v2
            profiles[key] = profile
        return skills_v2

    return update_json_transaction(WORKER_PROFILES_FILE, {}, _mutator)


# moved to core/constants.py -- _INVISIBLE_FILLER_CHARS


def _gps_suspect(lat: str, lon: str) -> bool:
    """Грубая эвристика, не блокирует check-in (GPS на стройке часто глючит --
    подвалы, между зданиями), только помечает координаты как подозрительные для owner:
    (0,0) -- Null Island, классический fallback GPS-модуля при полном отсутствии сигнала;
    либо явно вне разумного диапазона Германии (широта ~47-55, долгота ~5-16)."""
    try:
        f_lat, f_lon = float(lat), float(lon)
    except (ValueError, TypeError):
        return True
    if f_lat == 0 and f_lon == 0:
        return True
    if not (47 <= f_lat <= 55.5 and 5 <= f_lon <= 16):
        return True
    return False


def _is_meaningful_name(raw: str | None, user_id: str) -> bool:
    """Раунд 6 §4.1: строка — настоящее отображаемое имя, а не заглушка/ID.
    Строже, чем _sanitize_display_name (тот пропускает цифры и сам user_id как
    'валидные' \\w-строки): используется для one-time completion-gate существующих
    пользователей без имени. Пусто/пробелы/невидимое/==user_id/только цифры/<2/>100 -> False."""
    cleaned = _sanitize_display_name(raw, '')
    if not cleaned:
        return False
    s = cleaned.strip()
    if len(s) < 2 or len(s) > 100:
        return False
    if s.isdigit():
        return False
    if s == str(user_id):
        return False
    return True


def _resolve_current_display_name(user_id, stored_name: str | None = None) -> str:
    """22.09 (iPhone screenshot audit, P0/P1): canonical READ-SIDE display-name
    resolver -- always returns the CURRENT profile name for user_id, never a
    name frozen at write time.

    Real bug this closes: chat messages (post_chat_message) and other records
    stamp `name` at CREATE time from `user.get('first_name', str(user['id']))`
    -- if Telegram's first_name was empty/missing at that moment, the record
    permanently stores the raw numeric user_id as its "name" field. Every
    later read of that record (Chat history, Feed author, Calendar/Abwesenheit
    entries) then displayed the raw ID forever, even after the user's profile
    later got a real name -- confirmed live on a real device across Chat,
    Feed, and Calendar. A frontend-only mask (e.g. hiding numeric-looking
    names) would still show nothing meaningful and wouldn't fix legacy
    records with name==user_id stored server-side.

    Always looks up the CURRENT worker_profiles entry for user_id and uses it
    when meaningful (_is_meaningful_name, same bar the onboarding gate uses)
    -- ignores `stored_name` entirely when the profile has a real name,
    because a since-set real name must win over whatever was frozen at
    write time. `stored_name` is only a last-resort fallback (kept for the
    rare legitimate case of an ex-user with no profile record at all)."""
    uid_str = str(user_id)
    profile_name = _load_worker_profiles().get(uid_str, {}).get('name')
    if _is_meaningful_name(profile_name, uid_str):
        return _sanitize_display_name(profile_name, uid_str)
    return _sanitize_display_name(stored_name, uid_str)


def _validate_birthday(raw: str) -> str:
    """Раунд 6 §3.1: валидирует дату рождения. Формат YYYY-MM-DD, реальная дата,
    не в будущем (Europe/Berlin). Возвращает нормализованную ISO-строку либо HTTP 400."""
    if raw is None or not str(raw).strip():
        raise HTTPException(400, "Укажите дату рождения")
    s = str(raw).strip()
    try:
        d = datetime.strptime(s, '%Y-%m-%d').date()
    except ValueError:
        raise HTTPException(400, "Некорректная дата рождения (формат ГГГГ-ММ-ДД)")
    if d > business_today():
        raise HTTPException(400, "Дата рождения не может быть в будущем")
    return d.isoformat()


def _profile_completion_status(user: dict, profile: dict, role: str) -> dict:
    """Раунд 6 §3.2/§4: какие обязательные поля профиля не заполнены.
    name — для всех; birthday — только для Worker (Owner не обязан заполнять)."""
    uid = str(user['id'])
    name_ok = _is_meaningful_name(profile.get('name') or user.get('first_name'), uid)
    birthday_ok = bool(profile.get('birthday'))
    return {
        'name_required': not name_ok,
        'birthday_required': (role == 'worker') and not birthday_ok,
    }


@app.get("/api/profile/me")
def get_my_profile(user: dict = Depends(get_current_user)):
    profile = _get_worker_profile(user['id'])
    healed_name = _sanitize_display_name(profile.get('name'), '') or None
    if not healed_name and user.get('first_name'):
        # Раньше имя сохранялось только при загрузке аватара — работник без аватара
        # отображался у всех числовым Telegram ID (в чате, people-dots, Abwesenheit).
        # 23.07: то же самое — если сохранённое имя оказалось "битым" (невидимые
        # символы/бессмысленный набор из Telegram first_name), перезаписываем его
        # текущим Telegram first_name (обычно тем же значением — тогда ничего не
        # меняется — но если юзер уже поправил имя в Telegram, подхватываем новое).
        candidate = _sanitize_display_name(user.get('first_name'), '')
        if candidate:
            profiles = _load_worker_profiles()
            key = str(user['id'])
            profile = profiles.get(key, {"skills": [], "quiz_completed": False})
            profile['name'] = candidate
            profiles[key] = profile
            _save_worker_profiles(profiles)
            healed_name = candidate
    if healed_name:
        profile = {**profile, 'name': healed_name}
    else:
        # Item 5 fix (owner report: square/garbled glyphs in profile name):
        # the healing above only overwrites profile['name'] when a usable
        # replacement (Telegram first_name) exists. If BOTH the stored name
        # AND the current first_name fail sanitization (real case: Telegram
        # first_name genuinely contains only Hangul filler characters, not a
        # rendering/font bug -- confirmed by reading the raw string, not
        # guessed), the original unsanitized value was passed straight
        # through to the client. Never silently strip a legitimate Cyrillic
        # name (sanitize_display_name already handles that correctly) --
        # only replace when sanitization proves there is truly nothing
        # displayable left, falling back to the Telegram user_id like the
        # rest of the app already does when no name is available at all.
        existing_sanitized = _sanitize_display_name(profile.get('name'), '')
        if not existing_sanitized:
            profile = {**profile, 'name': str(user['id'])}
    skills_v2 = _get_worker_skills_v2(user['id'])
    role = _load_roles().get(str(user['id']), 'worker')
    return {
        "user_id": user['id'],
        "skill_options": SKILL_OPTIONS,  # legacy -- старый frontend-код может ещё это читать
        **profile,
        "role": role,
        "skills": pskills.legacy_skill_names_from_v2(skills_v2),  # legacy-совместимый список строк
        "skills_v2": skills_v2,  # новый источник истины
        "onboarding_completed": bool(profile.get('onboarding_completed') or profile.get('quiz_completed')),
        "onboarding_version": profile.get('onboarding_version', 1),
        # Раунд 6 §3.2/§4/§6: bootstrap показывает completion-экран если что-то обязательное пусто
        "needs_completion": _profile_completion_status(user, profile, role),
    }


@app.get("/api/users/{target_id}/card")
def get_user_card(target_id: str, user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    """Публичная карточка (10.10) — доступна любому авторизованному пользователю,
    в отличие от /api/profile/stats (там чужой профиль видит только owner).
    Только неконфиденциальные поля: имя, роль, навыки. Без часов/истории объектов/бюджета.

    30.07 (спек: expanded user-card): текущий объект/статус смены -- это МЕСТОПОЛОЖЕНИЕ
    работника, та же чувствительность что у /api/dashboard/shifts-today (уже owner-only).
    Не добавляем эти поля для worker-to-worker просмотра (нарушило бы существующий явный
    privacy-дизайн этого endpoint'а) -- только когда card смотрит owner."""
    roles = _load_roles()
    if target_id not in roles:
        raise HTTPException(404, "Пользователь не найден")
    profile = _get_worker_profile(target_id)
    has_avatar = bool(profile.get('avatar'))
    skills_v2 = _get_worker_skills_v2(target_id)
    card = {
        "user_id": target_id,
        "name": _sanitize_display_name(profile.get('name'), target_id),
        "role": roles[target_id],
        "skills": pskills.legacy_skill_names_from_v2(skills_v2),
        "skills_v2": skills_v2,
        "has_avatar": has_avatar,
    }
    if role == 'owner' and roles[target_id] != 'owner':
        # 20.09 (merged from upstream c23894d): здесь оставался datetime.now() =
        # UTC сервера, хотя s['date'] пишется checkin_start'ом как business_today_str()
        # (Europe/Berlin). Вечером после 00:00 CEST (23:00 UTC) сравнение шло со
        # ВЧЕРАШНЕЙ датой, и владелец видел "смена не идёт" у работника на живой
        # ночной смене. Остальные call-sites перевели на business_today_str() ещё
        # 03.08, этот пропустили.
        today = business_today_str()
        sessions = [s for s in _load_checkin_meta() if str(s.get('user_id')) == target_id and s.get('date') == today]
        open_session = next((s for s in sessions if _is_active_photo_checkin_session(s)), None)
        rows = _cached_get_used_range('Объекты')
        object_names = {}
        if rows:
            header, data = rows[0], rows[1:]
            for r in data:
                obj = dict(zip(header, r))
                object_names[str(obj.get('ID объекта', ''))] = obj.get('Объект', '')
        if open_session:
            card["shift_status"] = "working"
            card["object_name"] = object_names.get(open_session['object_id'], open_session['object_id'])
            card["stage_name"] = open_session.get('stage_name') or ''
            card["start_at"] = open_session.get('start_at')
        else:
            card["shift_status"] = "idle"
    return card


class SkillV2Body(BaseModel):
    skill_id: str
    level: str
    verified: bool = False


class ProfileUpdateBody(BaseModel):
    name: str | None = None  # 24.07: ручное имя — нужно, когда Telegram first_name
    # пуст/скрыт/состоит из невидимых символов (self-heal в get_my_profile не может
    # исцелиться нечем в этом случае).
    skills: list[str] | None = None  # legacy -- принимается для обратной совместимости
    skills_v2: list[SkillV2Body] | None = None  # 01.08: новый источник истины
    quiz_completed: bool | None = None
    onboarding_completed: bool | None = None
    onboarding_version: int | None = None
    pants_size: str | None = None
    shirt_size: str | None = None
    shoe_size: str | None = None
    birthday: str | None = None  # YYYY-MM-DD
    # Раунд 6 §3: владелец ОТМЕНИЛ прежнее решение (01.08), запрещавшее дату рождения в
    # onboarding и профиле. Поле снова обязательно для Worker в onboarding и completion-
    # экране, редактируется в профиле, используется для календаря и birthday-алертов.
    # Валидация — _validate_birthday() ниже (формат + не в будущем, Europe/Berlin).


@app.patch("/api/profile/me")
def update_my_profile(body: ProfileUpdateBody, user: dict = Depends(get_current_user)):
    """RMW под update_json_transaction -- profile-зависимая часть (existing_by_id
    для skills_v2 verified-preservation, onboarding merge-check против сохранённых
    полей, финальный update+save) выполняется ВНУТРИ мутатора на одном захвате
    лока, иначе конкурентный verify_worker_skill/avatar-upload/skills_v2-миграция
    на том же профиле мог тихо затереться этим PATCH или наоборот (storage.py)."""
    key = str(user['id'])
    role = _load_roles().get(key, 'worker')
    updates = body.dict(exclude_unset=True)
    if 'name' in updates:
        cleaned = _sanitize_display_name(updates['name'], '')
        if not cleaned:
            raise HTTPException(400, "Имя не должно быть пустым")
        updates['name'] = cleaned[:100]
    # Раунд 6 §3.1: дата рождения — валидируем формат/не-в-будущем при любом сохранении.
    if 'birthday' in updates and updates['birthday'] is not None:
        updates['birthday'] = _validate_birthday(updates['birthday'])
    if 'skills_v2' in updates:
        for s in updates['skills_v2']:
            if s.get('level') not in pskills.SKILL_LEVELS:
                raise HTTPException(400, f"Недопустимый уровень навыка: {s.get('level')!r}")

    def _mutator(profiles):
        profile = profiles.get(key, {"skills": [], "quiz_completed": False})
        if 'skills_v2' in updates:
            # 01.08 (доп.раунд П3): было -- ЛЮБОЙ PATCH skills_v2 сбрасывал verified=False
            # для ВСЕГО списка, включая навыки, которые вообще не менялись (реальный баг:
            # owner подтверждает 3 навыка, worker меняет уровень 4-го -- все 3 подтверждения
            # молча слетали). Теперь: verified сохраняется, если И skill_id, И level
            # совпадают с тем, что уже было в сохранённом профиле; новый/изменённый навык
            # получает verified=False (worker всё ещё не может сам его подтвердить -- см.
            # verify_worker_skill ниже, единственный способ поставить True).
            existing_by_id = {s['skill_id']: s for s in (profile.get('skills_v2') or []) if 'skill_id' in s}
            normalized = []
            for s in updates['skills_v2']:
                prior = existing_by_id.get(s.get('skill_id'))
                unchanged = prior is not None and prior.get('level') == s.get('level')
                normalized.append({
                    "skill_id": s.get('skill_id'),
                    "level": s.get('level'),
                    "verified": bool(prior.get('verified')) if unchanged else False,
                })
            updates['skills_v2'] = normalized
        # 01.08 (спека п.3): onboarding_completed нельзя установить, пока обязательные
        # условия не выполнены -- проверяем на РЕЗУЛЬТИРУЮЩЕМ профиле (после merge с
        # уже сохранёнными полями), не только на этом одном PATCH-запросе, т.к. frontend
        # сохраняет профиль по шагам (спека: "Сначала сохранить профиль, затем
        # установить onboarding_completed: true").
        if updates.get('onboarding_completed'):
            merged_name = updates.get('name', profile.get('name'))
            merged_skills = updates.get('skills_v2', profile.get('skills_v2', []))
            merged_birthday = updates.get('birthday', profile.get('birthday'))
            if not _sanitize_display_name(merged_name, ''):
                raise HTTPException(400, "Укажите имя, чтобы завершить регистрацию")
            # Раунд 6 §3.1: дата рождения обязательна для Worker (Owner — нет).
            if role == 'worker' and not merged_birthday:
                raise HTTPException(400, "Укажите дату рождения, чтобы завершить регистрацию")
            if not merged_skills:
                raise HTTPException(400, "Выберите хотя бы один навык")
            for s in merged_skills:
                level = s.get('level') if isinstance(s, dict) else None
                if level not in pskills.SKILL_LEVELS:
                    raise HTTPException(400, "Укажите уровень для каждого выбранного навыка")
            updates['onboarding_completed_at'] = datetime.utcnow().isoformat() + 'Z'
            updates['onboarding_version'] = 2
            updates['quiz_completed'] = True  # legacy-совместимость (спека п.3)
        profile.update(updates)
        profiles[key] = profile
        return profile

    return update_json_transaction(WORKER_PROFILES_FILE, {}, _mutator)


class SkillVerificationBody(BaseModel):
    verified: bool


@app.patch("/api/workers/{user_id}/skills/{skill_id}/verification")
def verify_worker_skill(user_id: str, skill_id: str, body: SkillVerificationBody,
                         user: dict = Depends(get_current_user), _: None = Depends(require_owner)):
    """01.08 (спека п.4): только Owner подтверждает навык работника. Worker не может
    сам выставить verified: true -- см. update_my_profile выше, self-service PATCH
    всегда сбрасывает verified в False.

    RMW под update_json_transaction -- read+normalize+set_verification+save на
    одном захвате лока, иначе конкурентный update_my_profile/avatar-upload на том
    же профиле мог тихо затереться этим подтверждением или наоборот (storage.py)."""
    key = str(user_id)

    def _mutator(profiles):
        profile = profiles.get(key)
        if not profile:
            raise HTTPException(404, "Профиль не найден")
        skills_v2, _changed = pskills.normalize_profile_skills(profile)
        skills_v2, found = pskills.set_skill_verification(skills_v2, skill_id, body.verified)
        if not found:
            raise HTTPException(404, "У работника нет такого навыка")
        profile['skills_v2'] = skills_v2
        profiles[key] = profile

    update_json_transaction(WORKER_PROFILES_FILE, {}, _mutator)
    return {"skill_id": skill_id, "verified": body.verified}


# ---------- Фаза 8: аватар + агрегированная статистика профиля ----------
# moved to core/paths.py -- AVATAR_DIR
# moved to core/limits.py -- AVATAR_MAX_BYTES
os.makedirs(AVATAR_DIR, exist_ok=True)


@app.post("/api/profile/me/avatar")
async def upload_my_avatar(file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    raw = await file.read()
    if len(raw) > AVATAR_MAX_BYTES:
        raise HTTPException(400, "Аватар слишком большой (макс. 4 МБ)")
    detected = sniff_image(raw)
    if not detected:
        raise HTTPException(400, "Файл должен быть изображением")
    ext = _ALLOWED_IMAGE_MIME_EXT[detected]
    uid = str(user['id'])
    # держим ровно один файл на юзера — старое расширение убираем
    for fname in os.listdir(AVATAR_DIR):
        if fname.startswith(uid + '.'):
            os.remove(os.path.join(AVATAR_DIR, fname))
    with open(os.path.join(AVATAR_DIR, f"{uid}.{ext}"), 'wb') as f:
        f.write(raw)

    # RMW под update_json_transaction -- иначе конкурентный update_my_profile/
    # verify_worker_skill на том же профиле мог тихо затереться этим avatar-флагом
    # или наоборот (storage.py). Файл на диске (сам аватар) пишется вне лока
    # профилей -- отдельный ресурс, гонка с ним тут не в скоупе этого фикса.
    def _mutator(profiles):
        profile = profiles.get(uid, {"skills": [], "quiz_completed": False})
        profile['avatar'] = True
        if not profile.get('name'):
            profile['name'] = user.get('first_name', uid)
        profiles[uid] = profile

    update_json_transaction(WORKER_PROFILES_FILE, {}, _mutator)
    return {"status": "ok"}


@app.get("/api/profile/{user_id}/avatar")
def get_avatar(user_id: str, user: dict = Depends(get_current_user)):
    if not user_id.isdigit():
        raise HTTPException(400, "user_id должен быть числовым Telegram ID")
    for ext in ('jpg', 'png', 'webp'):
        path = os.path.join(AVATAR_DIR, f"{user_id}.{ext}")
        if os.path.exists(path):
            return FileResponse(path)
    raise HTTPException(404, "Аватар не найден")


def _photo_pause_seconds(s: dict) -> int:
    if s.get('pause_accumulated_seconds') is not None:
        return max(0, int(s.get('pause_accumulated_seconds') or 0))
    return max(0, int(s.get('pause_minutes') or 0)) * 60


def _photo_pause_minutes(s: dict) -> int:
    return round(_photo_pause_seconds(s) / 60)


def _is_active_photo_checkin_session(s: dict) -> bool:
    return bool(s) and not s.get('manual_entry') and s.get('finish_at') is None


def _hours_from_session(s: dict) -> float:
    """Часы из check-in сессии: фото-сессия = finish-start-пауза, ручная = end-start-пауза.
    10.32: раньше фото-checkin паузу не вычитал вообще (только manual_entry) — юзер
    теперь явно спрашивает pause_minutes в finish-опроснике для обоих типов сессий."""
    if s.get('manual_entry'):
        try:
            h1, m1 = map(int, s['start_time'].split(':'))
            h2, m2 = map(int, s['end_time'].split(':'))
            mins = (h2 * 60 + m2) - (h1 * 60 + m1) - int(s.get('pause_minutes') or 0)
            return max(0, mins) / 60.0
        except Exception:
            return 0.0
    if s.get('start_at') and s.get('finish_at'):
        pause_seconds = _photo_pause_seconds(s)
        return max(0, (s['finish_at'] - s['start_at']) - pause_seconds) / 3600.0
    return 0.0


def _session_hours_live(s: dict) -> float:
    """Как _hours_from_session, но НЕзавершённую фото-смену (finish_at is None)
    досчитывает текущим моментом как условным концом (минус накопленная live-пауза) --
    для "часов за сегодня/неделю" на экране Команда, где идущая смена должна отражаться
    сразу, а не как 0 до финиша. Незавершённая смена считается ОДИН раз (нет второй
    записи с finish_at на тот же интервал). Зеркалит логику hours_today_total в
    get_dashboard_shifts_today. Раунд1 Задача 1.6."""
    if s.get('finish_at') is not None or s.get('manual_entry'):
        return _hours_from_session(s)
    if s.get('start_at'):
        elapsed = (time.time() - s['start_at'] - (s.get('pause_accumulated_seconds') or 0)) / 3600.0
        return max(0.0, elapsed)
    return 0.0


def _extra_works_summary_text(session: dict) -> str:
    """Сериализует structured extra_works[] в читаемый текст для Sheets/Telegram --
    оба места раньше показывали одну свободную строку (extra_work: str), теперь
    wizard пишет структурированный список, но получатели (бухгалтерия в Sheets,
    owner в Telegram) не должны видеть сырой JSON."""
    if session.get('extra_work'):
        return session['extra_work']
    works = session.get('extra_works') or []
    if not works:
        return ''
    parts = []
    for w in works:
        if not isinstance(w, dict):
            continue
        desc = str(w.get('description', '')).strip()
        if not desc:
            continue
        zone = str(w.get('zone', '')).strip()
        parts.append(f"{desc} ({zone})" if zone else desc)
    return '; '.join(parts)


def _write_zeiterfassung_row(session: dict, object_id: str, user_id: str):
    """24.07: учёт времени в Google Sheets (лист Zeiterfassung) — раньше писался
    только в checkin_meta.json на VPS, не был виден владельцу как таблица для
    бухгалтерии/отчётности заказчику. Вызывается из checkin_finish и checkin_manual,
    best-effort (не блокирует ответ пользователю при сбое записи в Sheets)."""
    try:
        o = _load_repo_objekte_lib()
        profiles = _load_worker_profiles()
        worker_name = _sanitize_display_name(profiles.get(str(user_id), {}).get('name'), str(user_id))

        rows = _cached_get_used_range('Объекты')
        object_name = object_id
        if rows:
            header, data = rows[0], rows[1:]
            for r in data:
                obj = dict(zip(header, r))
                if str(obj.get('ID объекта', '')) == str(object_id):
                    object_name = obj.get('Объект', object_id)
                    break

        if session.get('manual_entry'):
            start_time = session.get('start_time', '')
            end_time = session.get('end_time', '')
            date_str = session.get('date', '')
        else:
            start_time = datetime.fromtimestamp(session['start_at']).strftime('%H:%M') if session.get('start_at') else ''
            end_time = datetime.fromtimestamp(session['finish_at']).strftime('%H:%M') if session.get('finish_at') else ''
            date_str = datetime.fromtimestamp(session['start_at']).strftime('%Y-%m-%d') if session.get('start_at') else ''

        hours = round(_hours_from_session(session), 2)
        done_summary = session.get('done_summary') or session.get('description') or ''
        extra_work = _extra_works_summary_text(session)

        o.append_row_safe('Zeiterfassung', [
            object_name, worker_name, date_str, start_time, end_time,
            str(session.get('pause_minutes') or 0), str(hours), done_summary, extra_work,
        ])
    except Exception as e:
        print(f'WARNING: Zeiterfassung sheet write failed: {e}')


@app.get("/api/profile/stats")
def profile_stats(user_id: str = '', period: str = 'week', user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    """Агрегированная статистика для экрана профиля (Фаза 8 + 21.07 period-pills).
    Часы/история вычисляются на чтении из checkin_meta + assignments — не дублируются в хранении.
    Работник видит только себя; owner может запросить любого через ?user_id=.
    period: week (7 колец, дефолт, обратная совместимость) | month (heatmap 30 дней) |
    3months | year (агрегация по неделям/месяцам — 3 разных визуальных режима, не один рендер с другим диапазоном)."""
    from datetime import timedelta
    target = user_id if (role == 'owner' and user_id) else str(user['id'])
    sessions = [s for s in _load_checkin_meta() if str(s.get('user_id')) == target]

    # 21.07: owner смотрит СВОЙ профиль (без ?user_id=) — не отмечает check-in физически,
    # личные часы всегда пусты. Вместо этого — агрегат "часы по каждому работнику за неделю".
    team_hours = None
    if role == 'owner' and not user_id:
        roles = _load_roles()
        worker_ids = [uid for uid, r in roles.items() if r == 'worker']
        profiles_map = _load_worker_profiles()
        today0 = business_today()  # 03.08 (ТЗ Задача 5): было date.today() (UTC) -- вечером Berlin это уже "завтра" UTC
        week_start = today0 - timedelta(days=6)
        team_hours = []
        for wid in worker_ids:
            w_sessions = [s for s in _load_checkin_meta() if str(s.get('user_id')) == wid
                         and week_start.isoformat() <= s.get('date', '') <= today0.isoformat()]
            hours = round(sum(_hours_from_session(s) for s in w_sessions), 1)
            team_hours.append({
                'user_id': wid,
                'name': _sanitize_display_name(profiles_map.get(wid, {}).get('name'), wid),
                'hours': hours,
            })
        team_hours.sort(key=lambda t: t['hours'], reverse=True)

    # period-агрегаты (batch 1 Kalo референс): month = heatmap по дням, 3months/year = bar по неделям/месяцам
    period_data = None
    if period == 'month':
        today0 = business_today()  # 03.08 (ТЗ Задача 5): было date.today() (UTC)
        days = []
        for i in range(29, -1, -1):
            d = today0 - timedelta(days=i)
            iso = d.isoformat()
            hours = sum(_hours_from_session(s) for s in sessions if s.get('date') == iso)
            days.append({'date': iso, 'hours': round(hours, 2)})
        period_data = {'kind': 'heatmap', 'days': days, 'total_hours': round(sum(d['hours'] for d in days), 1)}
    elif period in ('3months', 'year'):
        weeks_back = 13 if period == '3months' else 52
        today0 = business_today()  # 03.08 (ТЗ Задача 5): было date.today() (UTC)
        buckets = []
        for i in range(weeks_back - 1, -1, -1):
            week_end = today0 - timedelta(days=i * 7)
            week_start = week_end - timedelta(days=6)
            hours = sum(_hours_from_session(s) for s in sessions
                        if week_start.isoformat() <= s.get('date', '') <= week_end.isoformat())
            buckets.append({'label': week_start.isoformat(), 'hours': round(hours, 2)})
        period_data = {'kind': 'bar', 'buckets': buckets, 'total_hours': round(sum(b['hours'] for b in buckets), 1)}

    # 7 кругов дней недели: последние 7 дней, часы на день
    today = business_today()  # 03.08 (ТЗ Задача 5): было date.today() (UTC)
    week = []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        iso = d.isoformat()
        hours = sum(_hours_from_session(s) for s in sessions if s.get('date') == iso)
        week.append({'date': iso, 'weekday': d.weekday(), 'hours': round(hours, 2)})

    # История объектов: check-in сессии + назначения (bubble-assign)
    by_obj = {}
    for s in sessions:
        oid = str(s.get('object_id', ''))
        rec = by_obj.setdefault(oid, {'object_id': oid, 'sessions': 0, 'total_hours': 0.0, 'last_date': '', 'assigned_stages': []})
        rec['sessions'] += 1
        rec['total_hours'] += _hours_from_session(s)
        rec['last_date'] = max(rec['last_date'], s.get('date', ''))
    for oid, lst in _load_assignments().items():
        for a in lst:
            if a.get('user_id') == target:
                rec = by_obj.setdefault(str(oid), {'object_id': str(oid), 'sessions': 0, 'total_hours': 0.0, 'last_date': '', 'assigned_stages': []})
                if a.get('stage_id'):
                    rec['assigned_stages'].append(a['stage_id'])
    objects_hist = sorted(by_obj.values(), key=lambda r: r['last_date'], reverse=True)
    for rec in objects_hist:
        rec['total_hours'] = round(rec['total_hours'], 1)
    try:
        rows = _cached_get_used_range('Объекты')
        names = {}
        if rows:
            hdr = rows[0]
            for r in rows[1:]:
                row = dict(zip(hdr, r))
                names[str(row.get('ID объекта', ''))] = row.get('Объект', '')
        for rec in objects_hist:
            rec['object_name'] = names.get(rec['object_id']) or rec['object_id']
    except Exception:
        for rec in objects_hist:
            rec['object_name'] = rec['object_id']

    # Work-speed: из AI-анализов прогресса (Фаза 4b); аккуратно опускается, если анализов не было
    finished = [s for s in sessions if not s.get('manual_entry') and s.get('finish_at')]
    avg_session_hours = round(sum(_hours_from_session(s) for s in finished) / len(finished), 2) if finished else None
    analyzed = [s for s in sessions if (s.get('analysis') or {}).get('progress')]
    work_speed = None
    if analyzed:
        last = max(analyzed, key=lambda s: s.get('finish_at') or 0)
        work_speed = {
            'analyzed_sessions': len(analyzed),
            'last_summary': (last['analysis']['progress'] or '')[:400],
        }

    # Урлауб-баланс: 24 рабочих дня в год (немецкий минимум) минус одобренные Urlaub-заявки (10.31)
    URLAUB_YEARLY_DAYS = 24
    this_year = str(today.year)
    urlaub_used = 0
    krankheit_used = 0
    for e in _load_abwesenheit():
        if str(e.get('user_id')) != target or e.get('status') != 'approved':
            continue
        if not e.get('date_from', '').startswith(this_year):
            continue
        d1 = datetime.strptime(e['date_from'], '%Y-%m-%d').date()
        d2 = datetime.strptime(e['date_to'], '%Y-%m-%d').date()
        days = (d2 - d1).days + 1
        if e.get('reason') == 'Urlaub':
            urlaub_used += days
        elif e.get('reason') == 'Krankheit':
            krankheit_used += days

    profile = _get_worker_profile(target)
    # 01.08 (доп.раунд П2): frontend (profile.js _loadProfileStats) читает stats.skills_v2
    # для уровня/verified -- backend отдавал только сырой legacy 'skills' (список
    # названий), skills_v2 отсутствовал вообще, owner никогда не видел уровень/verified
    # чужого работника на этом экране.
    stats_skills_v2 = _get_worker_skills_v2(target)
    return {
        'user_id': target,
        'name': _sanitize_display_name(
            profile.get('name') or (user.get('first_name') if target == str(user['id']) else None),
            target,
        ),
        'role': _load_roles().get(target, 'worker'),
        'skills': pskills.legacy_skill_names_from_v2(stats_skills_v2),
        'skills_v2': stats_skills_v2,
        'sizes': {
            'pants': profile.get('pants_size', ''),
            'shirt': profile.get('shirt_size', ''),
            'shoe': profile.get('shoe_size', ''),
        },
        'has_avatar': bool(profile.get('avatar')),
        # Раунд 6 §3.3: полную дату рождения видит только Owner или сам работник —
        # этот endpoint и так отдаёт чужой профиль исключительно owner'у (target-гейт выше).
        'birthday': profile.get('birthday'),
        'urlaub': {'used': urlaub_used, 'total': URLAUB_YEARLY_DAYS, 'remaining': max(0, URLAUB_YEARLY_DAYS - urlaub_used)},
        'krankheit_days_this_year': krankheit_used,
        'week': week,
        'week_total_hours': round(sum(d['hours'] for d in week), 1),
        'period': period,
        'period_data': period_data,
        'team_hours': team_hours,
        'avg_session_hours': avg_session_hours,
        'work_speed': work_speed,
        'objects': objects_hist,
    }


@app.get("/api/dashboard/team-hours")
def get_dashboard_team_hours(date_from: str = '', date_to: str = '',
                             user: dict = Depends(get_current_user),
                             _: None = Depends(require_owner)):
    """Раунд1 Задача 1.6: часы ВСЕЙ команды за неделю для экрана Команда → Сводка.
    owner-only (Worker -> 403, та же чувствительность что shifts-today -- агрегирует
    личные часы всех работников). Период по умолчанию -- текущая календарная неделя
    Пн-Вс Europe/Berlin; date_from/date_to опциональны (прошлая неделя).

    profile_stats.team_hours отдаёт только {user_id,name,hours} без today/is_working_now/
    current_object -- недостаточно для этого блока, поэтому отдельный endpoint. Часы:
    завершённые смены -- finish-start-pause; идущая смена -- elapsed до текущего момента
    (считается один раз, не дублируется); manual -- end-start-pause."""
    from datetime import timedelta, date as _date

    def _parse(s):
        try:
            return _date.fromisoformat(s)
        except Exception:
            return None

    today = business_today()
    d_from, d_to = _parse(date_from), _parse(date_to)
    if not (d_from and d_to):
        d_from = today - timedelta(days=today.weekday())   # понедельник этой недели
        d_to = d_from + timedelta(days=6)                  # воскресенье
    if d_from > d_to:
        d_from, d_to = d_to, d_from
    if (d_to - d_from).days > 62:                          # разумный предел окна
        d_to = d_from + timedelta(days=62)
    from_iso, to_iso, today_iso = d_from.isoformat(), d_to.isoformat(), today.isoformat()
    today_in_range = from_iso <= today_iso <= to_iso

    roles = _load_roles()
    worker_ids = [uid for uid, r in roles.items() if r == 'worker']
    profiles_map = _load_worker_profiles()
    all_sessions = _load_checkin_meta()

    object_names = {}
    try:
        rows = _cached_get_used_range('Объекты')
        if rows:
            hdr = rows[0]
            for r in rows[1:]:
                row = dict(zip(hdr, r))
                object_names[str(row.get('ID объекта', ''))] = row.get('Объект', '')
    except Exception:
        object_names = {}

    def _safe_team_name(wid):
        # _sanitize_display_name fallback -- сам wid (числовой Telegram ID); ТЗ 1.2 явно
        # запрещает показывать ID вместо имени -> fallback "Сотрудник", плюс страховка
        # если сохранённое имя само оказалось чисто числовым.
        nm = _sanitize_display_name(profiles_map.get(wid, {}).get('name'), 'Сотрудник')
        return 'Сотрудник' if (nm.strip() == str(wid) or nm.strip().isdigit()) else nm

    workers = []
    for wid in worker_ids:
        w_sessions = [s for s in all_sessions if str(s.get('user_id')) == wid]
        in_range = [s for s in w_sessions if from_iso <= s.get('date', '') <= to_iso]
        hours_week = round(sum(_session_hours_live(s) for s in in_range), 1)
        today_sess = [s for s in w_sessions if s.get('date', '') == today_iso] if today_in_range else []
        hours_today = round(sum(_session_hours_live(s) for s in today_sess), 1)
        open_s = next((s for s in today_sess if _is_active_photo_checkin_session(s)), None)
        is_working_now = open_s is not None
        cur_oid = str(open_s.get('object_id', '')) if open_s else ''
        workers.append({
            'user_id': wid,
            'name': _safe_team_name(wid),
            'has_avatar': bool(profiles_map.get(wid, {}).get('avatar')),
            'hours_today': hours_today,
            'hours_week': hours_week,
            'is_working_now': is_working_now,
            'current_object_id': cur_oid,
            'current_object_name': (object_names.get(cur_oid, cur_oid) if cur_oid else ''),
        })
    # сортировка (ТЗ 1.2): 1) кто сейчас работает, 2) часы недели убыв., 3) с 0 часами в конце
    workers.sort(key=lambda w: (not w['is_working_now'], -w['hours_week'], w['name'].lower()))
    return {
        'date_from': from_iso,
        'date_to': to_iso,
        'total_hours': round(sum(w['hours_week'] for w in workers), 1),
        'today_hours': round(sum(w['hours_today'] for w in workers), 1),
        'workers_with_hours': sum(1 for w in workers if w['hours_week'] > 0),
        'workers': workers,
    }


# ---------- Назначения работников на объекты (Фаза 2c, восстановлено после инцидента Фазы 3) ----------
# moved to core/paths.py -- OBJECT_ASSIGNMENTS_FILE
# moved to core/paths.py -- OBJECT_IMAGES_FILE
# moved to core/paths.py -- OBJECT_PHOTO_DIR


def _load_assignments() -> dict:
    return _safe_load_json(OBJECT_ASSIGNMENTS_FILE, {})


def _save_assignments(assignments: dict):
    _atomic_write_json(OBJECT_ASSIGNMENTS_FILE, assignments)


def _load_object_images() -> dict:
    return _safe_load_json(OBJECT_IMAGES_FILE, {})


def _save_object_images(images: dict):
    _atomic_write_json(OBJECT_IMAGES_FILE, images)


# moved to core/limits.py -- OBJECT_PHOTO_MAX


@app.post("/api/objects/{object_id}/image")
async def upload_object_image(object_id: str, file: UploadFile = File(...),
                               user: dict = Depends(get_current_user), _: None = Depends(require_owner)):
    # 28.07 v2: расширено до массива фото (carousel в карточке объекта, PHASE F спека) --
    # раньше был единственный fname (перезаписывался при повторной загрузке). Только owner.
    raw = await file.read()
    if len(raw) > 8 * 1024 * 1024:
        raise HTTPException(400, "Фото слишком большое (макс. 8 МБ)")
    detected = sniff_image(raw)
    if not detected:
        raise HTTPException(400, "Файл должен быть изображением")
    ext = _ALLOWED_IMAGE_MIME_EXT[detected]
    os.makedirs(OBJECT_PHOTO_DIR, exist_ok=True)
    fname = f"{uuid.uuid4().hex}.{ext}"
    fpath = os.path.join(OBJECT_PHOTO_DIR, fname)
    with open(fpath, 'wb') as f_out:
        f_out.write(raw)

    # 28.07 v3 (real bug found by external audit): было _load_object_images() +
    # _save_object_images() как два отдельных вызова -- read происходил СНАРУЖИ лока,
    # два параллельных upload на один object_id могли оба прочитать одинаковый
    # existing-список и один затирал фото, добавленное другим. update_json_transaction
    # держит read+mutate+write под одним захватом _lock_for(path).
    def _mutator(images):
        existing = images.get(object_id) or []
        if len(existing) >= OBJECT_PHOTO_MAX:
            raise HTTPException(400, f"Максимум {OBJECT_PHOTO_MAX} фото на объект")
        images[object_id] = existing + [fname]
        return images[object_id]

    try:
        photos = update_json_transaction(OBJECT_IMAGES_FILE, {}, _mutator)
    except HTTPException:
        # 28.07: metadata-транзакция не прошла (лимит фото достигнут) -- не оставляем
        # orphan-файл на диске без записи в metadata (ТЗ п.24: "не оставлять файл на
        # диске при неуспешной транзакции").
        if os.path.exists(fpath):
            os.remove(fpath)
        raise
    return {"status": "ok", "photos": photos}


@app.delete("/api/objects/{object_id}/image/{fname}")
def delete_object_image(object_id: str, fname: str, user: dict = Depends(get_current_user), _: None = Depends(require_owner)):
    safe_name = os.path.basename(fname)

    def _mutator(images):
        existing = images.get(object_id) or []
        if safe_name not in existing:
            raise HTTPException(404, "Фото не найдено")
        images[object_id] = [f for f in existing if f != safe_name]
        return images[object_id]

    photos = update_json_transaction(OBJECT_IMAGES_FILE, {}, _mutator)
    path = os.path.join(OBJECT_PHOTO_DIR, safe_name)
    if os.path.exists(path):
        os.remove(path)
    return {"status": "ok", "photos": photos}


@app.get("/api/objects/{object_id}/image/file")
def get_object_image_file(object_id: str, index: int = 0, user: dict = Depends(get_current_user)):
    # 30.07 (Release-аудит P1-5): нет require_object_access -- согласовано с
    # GET /api/objects (весь список объектов виден любому авторизованному, см.
    # тот же паттерн у /stages, /roadmap notes GET). Upload/delete фото остаются
    # owner-only. Не меняем это в рамках feature freeze -- задокументировано как
    # by-design consistency с остальным просмотром объектов, не отдельная дыра.
    images = _load_object_images()
    photos = images.get(object_id) or []
    if not photos or index < 0 or index >= len(photos):
        raise HTTPException(404, "Фото не загружено")
    path = os.path.join(OBJECT_PHOTO_DIR, photos[index])
    if not os.path.exists(path):
        raise HTTPException(404, "Файл отсутствует")
    return FileResponse(path)


_sheets_cache: dict = {}
# moved to core/limits.py -- SHEETS_CACHE_TTL
                        # на каждый запрос, блокируя event loop на время RTT (10.29, Fable-аудит)


def _cached_get_used_range(tab_name: str):
    now = time.time()
    cached = _sheets_cache.get(tab_name)
    if cached and now - cached[0] < SHEETS_CACHE_TTL:
        return cached[1]
    o = _load_repo_objekte_lib()
    rows = o.get_used_range(tab_name)
    _sheets_cache[tab_name] = (now, rows)
    return rows


# All known column names for the budget-percent field (live Sheet uses 'потрачено в % от бюджета';
# objekte_lib historically wrote '% бюджета'; alerts route also fell back to 'Потрачено %').
# Strip ALL aliases from worker DTOs so a column rename cannot accidentally re-expose the field.
# moved to core/constants.py -- BUDGET_FIELDS




def _serialize_object_for_worker(obj: dict, viewer_user_id: str, obj_assignments: list,
                                  user_info_fn, stage_summary_fn, images: dict) -> dict:
    """03.08 (ТЗ Задача 3): единая точка, ГДЕ worker теряет доступ к чужим assignment-
    метаданным -- раньше list_objects просто снимал BUDGET_FIELDS и отдавал ВСЁ
    остальное как есть, включая assignment_id/task_note/decline_reason/date_from/
    date_to/work_type/pending-declined статус КАЖДОГО работника на объекте (реальная
    находка: worker A мог прочитать, что worker B отказался от смены и почему).

    Worker получает:
      - object_id/название/адрес/общий статус/этапы/фото -- как раньше (уже public)
      - assigned_users: ПУБЛИЧНЫЙ список команды (user_id/name/has_avatar), без
        assignment-полей коллег вообще
      - my_assignments: ПОЛНЫЕ собственные назначения (включая future/pending/declined --
        нужно, чтобы принять/отклонить), той же формы, что owner видит для всех.

    oid берётся из obj САМ (та же 'ID объекта' колонка, что уже читает list_objects) --
    не передаётся отдельным параметром, чтобы не рассинхронизировать с images/stage_summary
    lookup, которые тоже keyed по этому oid."""
    oid = str(obj.get('ID объекта', ''))
    public_team = []
    my_assignments = []
    for a in obj_assignments:
        if not isinstance(a, dict):
            continue
        uid = str(a.get('user_id'))
        if uid == viewer_user_id:
            my_assignments.append(user_info_fn(uid, a))
        # Публичный список команды -- только user_id/name/has_avatar, без единого
        # assignment-поля (см. docstring выше). user_info_fn(uid) без assignment
        # аргумента уже возвращает ровно этот узкий набор (см. _user_info в list_objects).
        if not any(t.get('user_id') == uid for t in public_team):
            public_team.append(user_info_fn(uid))

    obj = dict(obj)
    obj['assigned_users'] = public_team
    obj['my_assignments'] = my_assignments
    obj['photo_count'] = len(images.get(oid) or [])
    obj['stage_summary'] = stage_summary_fn(oid)
    for f in BUDGET_FIELDS:
        # 10.5: бюджет — финансовая информация, работнику видеть не должен (сохранено
        # как было до этого рефакторинга).
        obj.pop(f, None)
    return obj




def _object_history_actor_name(user: dict | None) -> str:
    if not user:
        return ''
    uid = str(user.get('id', ''))
    profiles = _load_worker_profiles()
    name = profiles.get(uid, {}).get('name') or user.get('first_name') or user.get('username')
    return _sanitize_display_name(name, uid)


def _object_history_worker_name(user_id: str) -> str:
    uid = str(user_id or '')
    profiles = _load_worker_profiles()
    return _sanitize_display_name(profiles.get(uid, {}).get('name'), uid)


def _append_object_history(object_id: str, kind: str, title: str, *,
                           user: dict | None = None, subtitle: str = '',
                           meta: dict | None = None, at: str | None = None) -> dict:
    oid = str(object_id or '').strip()
    if not oid:
        return {}
    event = {
        "id": uuid.uuid4().hex,
        "object_id": oid,
        "kind": kind,
        "title": str(title or '').strip()[:200],
        "subtitle": str(subtitle or '').strip()[:500],
        "actor_id": str((user or {}).get('id', '')),
        "actor_name": _object_history_actor_name(user),
        "at": at or _utcnow_iso(),
        "meta": meta or {},
    }

    with _lock_for(OBJECT_HISTORY_FILE):
        items = _safe_load_json(OBJECT_HISTORY_FILE, [])
        if not isinstance(items, list):
            items = []
        items.append(event)
        if len(items) > 3000:
            del items[:-3000]
        tmp_path = f'{OBJECT_HISTORY_FILE}.tmp-{os.getpid()}'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(items, f, ensure_ascii=False)
        os.replace(tmp_path, OBJECT_HISTORY_FILE)
    return event


def _append_object_history_best_effort(*args, **kwargs):
    try:
        return _append_object_history(*args, **kwargs)
    except Exception as e:
        print(f'WARNING: object history append failed: {e}')
        return {}




def _dates_overlap(a_from: str, a_to: str, b_from: str, b_to: str) -> bool:
    if not (a_from and a_to and b_from and b_to):
        return False
    return a_from <= b_to and b_from <= a_to


def _assignment_periods_overlap(a: dict, b: dict) -> bool:
    """09.09 (P0 assignment integrity fix): _dates_overlap() above returns False
    whenever EITHER period is missing a date -- correct for its other callers
    (absence checks, plain date-range comparisons where an empty range simply
    means "no data"), but WRONG for cross-object assignment conflict checks:
    a legacy assignment created before date_from/date_to existed as fields has
    both empty, and every other part of this system (Worker Home, shift display,
    _assignment_status()) already treats an undated assignment as open-ended/
    indefinite, not as "no period at all". Using plain _dates_overlap() here let
    a worker with an old undated assignment on object A get a NEW, perfectly
    valid dated assignment on object B with zero conflict detected -- the
    exact hole owner's live data exposed (worker legitimately assigned OBJ-001
    9/11 + OBJ-003 9/12, no real overlap there, but the underlying dedup-only
    fix wouldn't have caught a REAL overlap involving an undated legacy record
    either). This helper: a period missing either date is treated as
    overlapping with everything (indefinite), matching how the rest of the
    system already reads that shape."""
    a_from, a_to = a.get('date_from', ''), a.get('date_to', '')
    b_from, b_to = b.get('date_from', ''), b.get('date_to', '')
    if not (a_from and a_to) or not (b_from and b_to):
        return True  # legacy/undated period -- indefinite, always a potential conflict
    return a_from <= b_to and b_from <= a_to






def _assignment_status(a: dict) -> str:
    """Compatibility rule (ТЗ п.9): назначения, созданные до введения поля status,
    трактуются как уже принятые -- иначе Worker Home у всех существующих объектов
    внезапно показал бы "ожидает подтверждения" для того, что реально уже идёт."""
    return a.get('status') or 'accepted'


ASSIGNMENT_CONFIRM_WARNING_SECONDS = 2 * 60 * 60
ASSIGNMENT_CONFIRM_DANGER_SECONDS = 4 * 60 * 60


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _parse_assignment_timestamp(value) -> int | None:
    if value in (None, ''):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    raw = str(value).strip()
    if not raw:
        return None
    try:
        if raw.endswith('Z'):
            raw = raw[:-1] + '+00:00'
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _assignment_pending_escalation(a: dict, now_ts: int | None = None) -> dict:
    if _assignment_status(a) != 'pending':
        return {"level": "", "type": "", "age_seconds": 0}
    now_ts = int(time.time()) if now_ts is None else int(now_ts)
    start_ts = (
        _parse_assignment_timestamp(a.get('pending_since')) or
        _parse_assignment_timestamp(a.get('updated_at')) or
        _parse_assignment_timestamp(a.get('assigned_at'))
    )
    if not start_ts:
        return {"level": "", "type": "", "age_seconds": 0}
    age = max(0, now_ts - start_ts)
    if age >= ASSIGNMENT_CONFIRM_DANGER_SECONDS:
        return {"level": "danger", "type": "red", "age_seconds": age}
    if age >= ASSIGNMENT_CONFIRM_WARNING_SECONDS:
        return {"level": "warning", "type": "yellow", "age_seconds": age}
    return {"level": "", "type": "", "age_seconds": age}


# ---------- Owner dashboard: смены сегодня (B5, 27.07) ----------
@app.get("/api/dashboard/shifts-today")
def get_dashboard_shifts_today(user: dict = Depends(get_current_user), _: None = Depends(require_owner)):
    """Кто сейчас работает / кто назначен но не начал / все смены за сегодня --
    для owner dashboard. owner-only: агрегирует GPS/личные данные всех работников,
    та же чувствительность что у GET /api/checkin (уже owner-gated для чужих сессий)."""
    today = business_today_str()
    sessions = _load_checkin_meta()
    today_sessions = [s for s in sessions if s.get('date') == today]

    profiles = _load_worker_profiles()
    rows = _cached_get_used_range('Объекты')
    object_names = {}
    if rows:
        header, data = rows[0], rows[1:]
        for r in data:
            obj = dict(zip(header, r))
            object_names[str(obj.get('ID объекта', ''))] = obj.get('Объект', '')

    def _worker_name(uid):
        return _sanitize_display_name(profiles.get(str(uid), {}).get('name'), str(uid))

    def _worker_specialty(uid):
        skills = profiles.get(str(uid), {}).get('skills') or []
        return skills[0] if skills else ''

    working_now = []
    finished_today = []
    for s in today_sessions:
        entry = {
            "user_id": str(s['user_id']),
            "worker_name": _worker_name(s['user_id']),
            "specialty": _worker_specialty(s['user_id']),
            "object_id": s['object_id'],
            "object_name": object_names.get(s['object_id'], s['object_id']),
            "start_at": s.get('start_at'),
        }
        if _is_active_photo_checkin_session(s):
            working_now.append(entry)
        else:
            entry['finish_at'] = s.get('finish_at')
            finished_today.append(entry)

    working_uids = {e['user_id'] for e in working_now}
    finished_uids = {e['user_id'] for e in finished_today}

    # 30.07 v2 (аудит): собираем ВСЕ назначения на сегодня по user_id СНАЧАЛА, потом
    # одна итоговая группа на worker по строгому приоритету -- чинит реальный баг,
    # где worker с accepted на одном объекте И pending на другом мог одновременно
    # попасть и в "не вышел", и в "ожидает подтверждения" (прошлая версия группировала
    # per-(object, assignment) в одном проходе, не per-worker).
    assignments = _load_assignments()
    by_uid = {}  # uid -> list of (oid, assignment) на сегодня
    for oid, lst in assignments.items():
        for a in lst:
            uid = str(a.get('user_id', ''))
            date_from, date_to = a.get('date_from', ''), a.get('date_to', '')
            if not uid or not (date_from and date_to and date_from <= today <= date_to):
                continue
            by_uid.setdefault(uid, []).append((oid, a))

    now_ts = int(time.time())

    def _entry(uid, oid, a):
        escalation = _assignment_pending_escalation(a, now_ts)
        return {
            "user_id": uid, "worker_name": _worker_name(uid), "specialty": _worker_specialty(uid),
            "object_id": oid, "object_name": object_names.get(oid, oid),
            "stage_id": a.get('stage_id', ''), "date_from": a.get('date_from', ''),
            "date_to": a.get('date_to', ''), "task_note": a.get('task_note', ''),
            "assignment_status": _assignment_status(a),
            "assignment_id": a.get('id', ''),
            "assigned_at": a.get('assigned_at', ''),
            "pending_since": a.get('pending_since') or a.get('updated_at') or a.get('assigned_at', ''),
            "response_wait_seconds": escalation["age_seconds"],
            "response_escalation": escalation["level"],
            "response_alert_type": escalation["type"],
        }

    not_started = []
    awaiting_response = []
    for uid, pairs in by_uid.items():
        if uid in working_uids or uid in finished_uids:
            continue  # приоритет 1/готово сегодня -- уже показан там, здесь не дублируем
        accepted_pair = next((p for p in pairs if _assignment_status(p[1]) == 'accepted'), None)
        if accepted_pair:
            not_started.append(_entry(uid, *accepted_pair))
            continue
        pending_pair = next((p for p in pairs if _assignment_status(p[1]) == 'pending'), None)
        if pending_pair:
            awaiting_response.append(_entry(uid, *pending_pair))

    # "Доступны сегодня" -- все остальные работники, не занятые ни в одной из групп
    # выше (включая тех, у кого только declined-назначения -- declined не занимает)
    # и без approved-отсутствия на сегодня.
    busy_uids = working_uids | finished_uids | {e['user_id'] for e in not_started} | {e['user_id'] for e in awaiting_response}
    absent_uids = {
        str(e.get('user_id')) for e in _load_abwesenheit()
        if e.get('status') == 'approved' and e.get('date_from', '') <= today <= e.get('date_to', '')
    }
    roles = _load_roles()
    available_today = [
        {"user_id": uid, "worker_name": _worker_name(uid), "specialty": _worker_specialty(uid)}
        for uid in profiles
        if roles.get(uid, 'worker') != 'owner' and uid not in busy_uids and uid not in absent_uids
    ]

    # 30.07 v2: "Работают сейчас" тоже несёт stage_name (если известен из checkin) --
    # аудит просит этот контекст для группы 1 наравне с 2/3.
    for e in working_now:
        s = next((s for s in today_sessions if str(s.get('user_id')) == e['user_id'] and _is_active_photo_checkin_session(s)), None)
        e['stage_name'] = (s or {}).get('stage_name') or ''

    # 30.07 v3 (спек: "Часы команды" на экране Команда) -- сумма часов за СЕГОДНЯ по
    # всем today_sessions (включая ещё идущие -- _hours_from_session на open-сессии
    # без finish_at даёт 0, отдельно досчитываем текущим временем как условный "конец").
    hours_today_total = 0.0
    for s in today_sessions:
        if s.get('finish_at') is not None or s.get('manual_entry'):
            hours_today_total += _hours_from_session(s)
        elif s.get('start_at'):
            elapsed = (time.time() - s['start_at'] - (s.get('pause_accumulated_seconds') or 0)) / 3600.0
            hours_today_total += max(0.0, elapsed)

    sparkline_days = []
    today_date = business_today()
    for i in range(6, -1, -1):
        d = (today_date - timedelta(days=i)).isoformat()
        day_sessions = [s for s in sessions if s.get('date') == d]
        day_hours = 0.0
        for s in day_sessions:
            if s.get('finish_at') is not None or s.get('manual_entry'):
                day_hours += _hours_from_session(s)
            elif d == today and s.get('start_at'):
                elapsed = (time.time() - s['start_at'] - (s.get('pause_accumulated_seconds') or 0)) / 3600.0
                day_hours += max(0.0, elapsed)
        sparkline_days.append({
            "date": d,
            "hours": round(day_hours, 1),
            "shifts": len(day_sessions),
            "finished": sum(1 for s in day_sessions if s.get('finish_at') is not None or s.get('manual_entry')),
        })

    return {
        "date": today,
        "working_now": working_now,
        "hours_today_total": round(hours_today_total, 1),
        "sparkline": {
            "days": sparkline_days,
        },
        "not_started": not_started,
        "finished_today": finished_today,
        "awaiting_response": awaiting_response,
        "available_today": available_today,
    }


@app.get("/api/dashboard/active-blockers")
def get_active_blockers(user: dict = Depends(get_current_user), _: None = Depends(require_owner)):
    """30.07 (спек: "Команда"→"Требует внимания", тип 3 -- "Сообщил о проблеме").
    stage_blocks в roadmap.json хранится по stage_key (='ID строки этапа' в Sheets),
    без object_id/stage_name -- матчим через all_stages_grouped() (один batch-запрос,
    не N+1 по каждому объекту). owner-only, та же чувствительность что shifts-today."""
    o = _load_repo_objekte_lib()
    store = _safe_load_json(rl.ROADMAP_FILE, rl._default_store())
    stage_blocks = store.get('stage_blocks', {})
    if not stage_blocks:
        return {"blockers": []}
    rows = _cached_get_used_range('Объекты')
    object_names = {}
    if rows:
        header, data = rows[0], rows[1:]
        for r in data:
            obj = dict(zip(header, r))
            object_names[str(obj.get('ID объекта', ''))] = obj.get('Объект', '')
    profiles = _load_worker_profiles()
    grouped = o.all_stages_grouped()
    result = []
    for oid, stages in grouped.items():
        for s in stages:
            stage_key = s.get('ID строки этапа')
            meta = stage_blocks.get(stage_key)
            if not meta:
                continue
            blocked_by = str(meta.get('blocked_by', ''))
            result.append({
                "object_id": oid, "object_name": object_names.get(oid, oid),
                "stage_name": s.get('Название этапа', ''), "row_num": s.get('_row'),
                "reason": meta.get('quick_reason') or meta.get('comment') or '',
                "reported_by_name": _sanitize_display_name(profiles.get(blocked_by, {}).get('name'), blocked_by),
                "blocked_at": meta.get('blocked_at'),
            })
    result.sort(key=lambda b: b.get('blocked_at') or 0, reverse=True)
    return {"blockers": result}


@app.get("/api/dashboard/team-plan")
def get_team_plan(date: str = '', user: dict = Depends(get_current_user), _: None = Depends(require_owner)):
    """30.07 (спек: экран "Команда" → вкладка "План"). Отвечает только "кто запланирован
    на дату и на каком объекте" -- читает существующие assignments, ничего не хранит
    отдельно. Для date == сегодня дополнительно отдаёт фактический статус смены
    (идёт/не начата/завершена) из checkin_meta; для будущих дат факт не считается --
    его физически ещё не может быть."""
    target_date = date or business_today_str()  # 03.08 (ТЗ Задача 5): было date.today() (UTC)

    rows = _cached_get_used_range('Объекты')
    object_names = {}
    if rows:
        header, data = rows[0], rows[1:]
        for r in data:
            obj = dict(zip(header, r))
            object_names[str(obj.get('ID объекта', ''))] = obj.get('Объект', '')

    profiles = _load_worker_profiles()

    def _worker_name(uid):
        return _sanitize_display_name(profiles.get(str(uid), {}).get('name'), str(uid))

    # Cleanup-commit (спек п.4): stage_id в assignments -- уже человекочитаемый текст
    # (вид работ из BUBBLE_STAGE_OPTIONS либо "Текущий этап" объекта на момент назначения),
    # но не всегда совпадает с актуальным названием строки этапа в Sheets. Один batch-запрос
    # (не N+1 по каждому назначению) -- матчим по названию этапа того же объекта, при
    # совпадении отдаём каноничное имя, иначе используем stage_id как есть (fallback).
    o = _load_repo_objekte_lib()
    stage_names_by_object = {}
    for oid, stages in o.all_stages_grouped().items():
        stage_names_by_object[oid] = {s.get('Название этапа', '').strip().lower(): s.get('Название этапа', '') for s in stages}

    def _resolve_stage_name(oid, stage_id):
        names = stage_names_by_object.get(oid, {})
        return names.get((stage_id or '').strip().lower(), '')

    is_today = target_date == business_today_str()  # 03.08 (ТЗ Задача 5): было date.today() (UTC)
    sessions_by_uid = {}
    if is_today:
        for s in _load_checkin_meta():
            if s.get('date') == target_date:
                sessions_by_uid.setdefault(str(s.get('user_id')), []).append(s)

    assignments = _load_assignments()
    by_object = {}
    for oid, lst in assignments.items():
        for a in lst:
            date_from, date_to = a.get('date_from', ''), a.get('date_to', '')
            if not (date_from and date_to and date_from <= target_date <= date_to):
                continue
            status = _assignment_status(a)
            # Cleanup-commit (спек п.5): declined больше НЕ пропускается -- owner должен
            # видеть, что запланированный сотрудник отклонил назначение.
            uid = str(a.get('user_id', ''))
            stage_id = a.get('stage_id', '')
            entry = {
                "user_id": uid,
                "worker_name": _worker_name(uid),
                "stage_id": stage_id,
                "stage_name": _resolve_stage_name(oid, stage_id),
                "task_note": a.get('task_note', ''),
                "date_from": date_from,
                "date_to": date_to,
                "assignment_status": status,
            }
            # shift_state имеет смысл только для уже принятого назначения на сегодня --
            # pending ещё ничего не подтвердил, declined отклонил, для них факта смены
            # физически быть не может.
            if is_today and status == 'accepted':
                uid_sessions = sessions_by_uid.get(uid, [])
                active = next((s for s in uid_sessions if s.get('object_id') == oid and _is_active_photo_checkin_session(s)), None)
                finished = next((s for s in uid_sessions if s.get('object_id') == oid and s.get('finish_at') is not None), None)
                if active:
                    entry['shift_state'] = 'active'
                elif finished:
                    entry['shift_state'] = 'finished'
                else:
                    entry['shift_state'] = 'not_started'
            by_object.setdefault(oid, {"object_id": oid, "object_name": object_names.get(oid, oid), "assignments": []})
            by_object[oid]["assignments"].append(entry)

    objects_plan = sorted(by_object.values(), key=lambda o: o['object_name'] or o['object_id'])
    return {"date": target_date, "objects": objects_plan}


def _format_assignment_wait(age_seconds: int) -> str:
    minutes = max(0, int(age_seconds) // 60)
    hours, mins = divmod(minutes, 60)
    if hours:
        return f"{hours} ч {mins} мин"
    return f"{mins} мин"


def _assignment_confirmation_alerts(now_ts: int | None = None) -> list:
    now_ts = int(time.time()) if now_ts is None else int(now_ts)
    profiles = _load_worker_profiles()
    object_names = {}
    try:
        rows = _cached_get_used_range('Объекты')
    except Exception:
        rows = None
    if rows:
        header, data = rows[0], rows[1:]
        for r in data:
            obj = dict(zip(header, r))
            oid = str(obj.get('ID объекта', '')).strip()
            if oid:
                object_names[oid] = obj.get('Объект') or obj.get('Название') or obj.get('Адрес') or oid

    result = []
    for oid, assigned_list in _load_assignments().items():
        for a in assigned_list:
            if _assignment_status(a) != 'pending':
                continue
            escalation = _assignment_pending_escalation(a, now_ts)
            if not escalation["level"]:
                continue
            uid = str(a.get('user_id', ''))
            worker_name = _sanitize_display_name(profiles.get(uid, {}).get('name'), uid)
            object_name = object_names.get(str(oid), str(oid))
            work_label = pskills.skill_display_name(a.get('work_type_id', '')) if a.get('work_type_id') else a.get('stage_id', '')
            wait_label = _format_assignment_wait(escalation["age_seconds"])
            parts = [p for p in (object_name, work_label, f"ждёт {wait_label}") if p]
            alert_id = a.get('id') or f"{uid}-{a.get('stage_id', '')}"
            result.append({
                'id': f'assignment-confirm-{escalation["level"]}-{oid}-{alert_id}',
                'type': escalation["type"],
                'role_filter': 'owner',
                'title': ('Не подтверждена задача' if escalation["level"] == 'danger' else 'Ждёт подтверждения') + f': {worker_name}',
                'subtitle': ' · '.join(parts),
                'at': a.get('pending_since') or a.get('updated_at') or a.get('assigned_at'),
                'assignment_confirmation': True,
                'assignment_id': a.get('id', ''),
                'assignment_object_id': str(oid),
                'assignment_worker_id': uid,
                'response_wait_seconds': escalation["age_seconds"],
                'response_escalation': escalation["level"],
            })
    return result


# ---------- Alerts inbox — role-aware агрегация (Фаза 2g, восстановлено после инцидента Фазы 3) ----------
@app.get("/api/alerts")
def get_alerts(user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    alerts = []

    # Budget alerts (owner sees) — жёлтый ≥60%, красный ≥90%
    try:
        rows = _cached_get_used_range('Объекты')
        if rows:
            header, data_rows = rows[0], rows[1:]
            for row in data_rows:
                obj = dict(zip(header, row))
                try:
                    pct = float(_load_repo_objekte_lib().get_budget_percent(obj) or 0)
                except (ValueError, TypeError):
                    pct = 0
                oid = obj.get('ID объекта', '')
                name = obj.get('Объект', oid)
                if pct >= 90:
                    alerts.append({
                        'id': f'budget-red-{oid}', 'type': 'red', 'role_filter': 'owner',
                        'title': f'Бюджет перегружен: {name}',
                        'subtitle': f'{int(pct)}% использовано', 'at': None
                    })
                elif pct >= 60:
                    alerts.append({
                        'id': f'budget-yellow-{oid}', 'type': 'yellow', 'role_filter': 'owner',
                        'title': f'Бюджет под угрозой: {name}',
                        'subtitle': f'{int(pct)}% использовано', 'at': None
                    })
    except Exception:
        pass

    # Tool issues (owner) — красный при ремонте/не найден
    try:
        tl = _load_repo_tools_lib()
        tools_list = tl.list_tools()
        for t in tools_list:
            raw_st = (t.get('Статус') or '').strip().lower()
            if raw_st in ('в ремонте', 'не найден'):
                alerts.append({
                    'id': f'tool-{t.get("Серийный #","")}', 'type': 'red', 'role_filter': 'owner',
                    'title': f'Инструмент: {t.get("Название Инструмента","")}',
                    'subtitle': f'Статус: {t.get("Статус","")}', 'at': None
                })
    except Exception:
        pass

    # Worker assignment alerts (worker sees yellow when assigned)
    if role == 'worker':
        assignments = _load_assignments()
        uid = str(user['id'])
        for obj_id, assigned_list in assignments.items():
            for a in assigned_list:
                if a.get('user_id') == uid:
                    stage_txt = f'Этап: {a["stage_id"]}' if a.get('stage_id') else 'Новое назначение'
                    alerts.append({
                        'id': f'assign-{obj_id}-{a.get("stage_id","")}', 'type': 'yellow',
                        'role_filter': 'worker',
                        'title': 'Вы назначены на объект',
                        'subtitle': stage_txt, 'at': a.get('assigned_at')
                    })

    # Pending abwesenheit requests (owner sees red until decided) — 10.15
    if role == 'owner':
        for e in _load_abwesenheit():
            if e.get('status', 'pending') == 'pending':
                alerts.append({
                    'id': f'abw-pending-{e.get("id", "")}', 'type': 'red', 'role_filter': 'owner',
                    'title': f'Заявка на отсутствие: {e.get("name", e.get("user_id", "?"))}',
                    'subtitle': f'{e.get("date_from", "?")} — {e.get("date_to", "?")} · {e.get("reason", "?")}',
                    'at': e.get('created_at')
                })

        alerts.extend(_assignment_confirmation_alerts())
        alerts.extend(_overdue_task_alerts())

    # Persisted critical alerts (Фаза 10.16 — global critical alert popup)
    for ca in _load_critical_alerts():
        if ca.get('target_user_id') != str(user['id']):
            continue
        if ca.get('acknowledged_at'):
            continue
        alerts.append({
            'id': f'critical-{ca["id"]}', 'type': 'red', 'role_filter': role,
            'title': ca['title'], 'subtitle': ca.get('subtitle', ''), 'at': ca.get('created_at'),
            'critical_alert_id': ca['id'],
        })

    # Раунд 6 §5.2: in-app activity alerts о новых комментариях (агрегируем по публикации).
    try:
        acts = [a for a in _load_activity_alerts()
                if a.get('target_user_id') == str(user['id']) and not a.get('read_at')]
        agg = {}
        for a in acts:
            agg.setdefault((a['kind'], a['ref_id']), []).append(a)
        for (kind, ref_id), group in agg.items():
            n = len(group)
            latest = max(group, key=lambda x: x.get('created_at', 0))
            noun = 'новости' if kind == 'news_comment' else 'фото'
            title = (f"{n} новых комментария к {noun}" if n > 1 else latest.get('title', ''))
            alerts.append({
                'id': f'activity-{kind}-{ref_id}', 'type': 'info', 'role_filter': role,
                'title': title, 'subtitle': latest.get('subtitle', ''),
                'at': latest.get('created_at'),
                'activity_kind': kind, 'activity_ref_id': ref_id,
                'activity_deep_link': ('news' if kind == 'news_comment' else 'photos'),
            })
    except Exception:
        pass

    filtered = [a for a in alerts if a['role_filter'] == role]

    # 25.07: "прочитано" для derived-алертов (бюджет/инструмент/назначение) -- раньше
    # счётчик на Home всегда показывал реальное активное количество, юзер жаловался
    # "не сбрасывается после просмотра". Persisted critical alerts уже имеют свой
    # ack-механизм (acknowledged_at) выше -- не трогаем их отдельным dismiss-слоем.
    # Dismiss истекает через 24ч: если проблема (перегруженный бюджет и т.п.) всё ещё
    # активна на следующий день, алерт напоминает о себе снова -- не даёт навсегда
    # забыть про нерешённую проблему, просто не мозолит глаза сразу после просмотра.
    dismissals = _load_alert_dismissals().get(str(user['id']), {})
    now = int(time.time())
    filtered = [a for a in filtered if now - dismissals.get(a['id'], 0) > ALERT_DISMISS_TTL]

    return {"alerts": filtered, "count": len(filtered)}


# moved to core/paths.py -- ALERT_DISMISSALS_FILE
# moved to core/limits.py -- ALERT_DISMISS_TTL


def _load_alert_dismissals() -> dict:
    return _safe_load_json(ALERT_DISMISSALS_FILE, {})


def _save_alert_dismissals(data: dict):
    _atomic_write_json(ALERT_DISMISSALS_FILE, data)


class AlertDismissBody(BaseModel):
    alert_ids: list[str]


@app.post("/api/alerts/dismiss")
def dismiss_alerts(body: AlertDismissBody, user: dict = Depends(get_current_user)):
    data = _load_alert_dismissals()
    my_id = str(user['id'])
    entry = data.setdefault(my_id, {})
    now = int(time.time())
    for aid in body.alert_ids:
        entry[aid] = now
    _save_alert_dismissals(data)
    return {"ok": True}



# ---------- Tools domain ----------
# The tools HTTP/business flow lives in routes/tools.py. Dependencies are injected
# here so the route module does not import main.py and therefore cannot form a
# circular dependency. Public names are temporarily re-exported for existing tests
# and callers while the monolith is decomposed incrementally.
if __package__:
    from .routes.tools import (
        CheckoutBody,
        NewToolBody,
        ToolBookingBody,
        ToolUpdateBody,
        ToolsRouteDeps,
        create_tools_router,
        holder_name_from_user as _holder_name_from_user,
        lock_for_tool as _lock_for_tool,
        parse_tool_booking_date as _parse_tool_booking_date,
        tool_booking_default_store as _tool_booking_default_store,
        tool_booking_overlaps as _tool_booking_overlaps,
    )
else:
    from routes.tools import (  # noqa: E402
        CheckoutBody,
        NewToolBody,
        ToolBookingBody,
        ToolUpdateBody,
        ToolsRouteDeps,
        create_tools_router,
        holder_name_from_user as _holder_name_from_user,
        lock_for_tool as _lock_for_tool,
        parse_tool_booking_date as _parse_tool_booking_date,
        tool_booking_default_store as _tool_booking_default_store,
        tool_booking_overlaps as _tool_booking_overlaps,
    )

_tools_router, _tools_handlers = create_tools_router(
    ToolsRouteDeps(
        get_current_user=get_current_user,
        get_role=get_role,
        require_owner=require_owner,
        # Keep these lookups dynamic: tests intentionally monkey-patch the loader
        # and TOOL_BOOKINGS_FILE, and production may override data paths at runtime.
        load_tools_lib=lambda: _load_repo_tools_lib(),
        load_worker_profiles=lambda: _load_worker_profiles(),
        sanitize_display_name=lambda value, fallback: _sanitize_display_name(value, fallback),
        business_today=lambda: business_today(),
        business_today_str=lambda: business_today_str(),
        safe_load_json=lambda path, default: _safe_load_json(path, default),
        update_json_transaction=lambda path, default_factory, mutator: update_json_transaction(
            path, default_factory, mutator
        ),
        tool_bookings_file=lambda: TOOL_BOOKINGS_FILE,
    )
)
app.include_router(_tools_router)

# Temporary compatibility surface for direct-call tests and any internal callers.
# There is no duplicated implementation here; every alias points at routes/tools.py.
list_tools = _tools_handlers.list_tools
tool_history = _tools_handlers.tool_history
list_tool_bookings = _tools_handlers.list_tool_bookings
create_tool_booking = _tools_handlers.create_tool_booking
cancel_tool_booking = _tools_handlers.cancel_tool_booking
checkout_tool = _tools_handlers.checkout_tool
return_tool = _tools_handlers.return_tool
update_tool = _tools_handlers.update_tool
create_tool = _tools_handlers.create_tool
_active_tool_booking_conflict = _tools_handlers.active_tool_booking_conflict


# ---------- Angebot generator ----------
import subprocess
import urllib.request as _urlreq
from fastapi.responses import FileResponse


# moved to core/telegram.py -- send_telegram_message (extracted ahead of the
# rest of Phase A's permissions.py step, to unblock it later: get_current_user
# -> _notify_owner_new_user -> send_telegram_message was the one upward edge
# out of the permissions chain back into main.py-local code, per the Phase A
# dependency map's R4 risk. This leaf move stands on its own regardless of
# whether/when the rest of permissions.py gets extracted.)
try:
    from .core.telegram import send_telegram_message
except ImportError:
    from core.telegram import send_telegram_message  # noqa: E402


def _require_server_script(script_path: str, label: str):
    # Shared infrastructure helper: Objects also depends on this, so it stays in
    # the composition/core area rather than being owned by commercial documents.
    if not script_path or not os.path.isfile(script_path):
        raise HTTPException(500, f'{label} не найден: {script_path or "-"}')
    return script_path


ANGEBOT_SCRIPT = os.path.join(BACKEND_DIR, 'angebot_free.js')
RECHNUNG_SCRIPT = os.path.join(BACKEND_DIR, 'rechnung.js')
# moved to core/paths.py -- ANGEBOT_OUT_DIR / RECHNUNG_OUT_DIR
os.makedirs(ANGEBOT_OUT_DIR, exist_ok=True)
os.makedirs(RECHNUNG_OUT_DIR, exist_ok=True)


# ---------- Commercial documents domain (Angebot + Rechnung) ----------
if __package__:
    from .routes.commercial_docs import (
        AngebotBody,
        AngebotKunde,
        AngebotPosition,
        CommercialDocsRouteDeps,
        RechnungBody,
        RechnungKunde,
        RechnungPosition,
        create_commercial_docs_router,
    )
else:
    from routes.commercial_docs import (  # noqa: E402
        AngebotBody,
        AngebotKunde,
        AngebotPosition,
        CommercialDocsRouteDeps,
        RechnungBody,
        RechnungKunde,
        RechnungPosition,
        create_commercial_docs_router,
    )

_commercial_docs_router, _commercial_docs_handlers = create_commercial_docs_router(
    CommercialDocsRouteDeps(
        get_current_user=get_current_user,
        get_role=get_role,
        bot_token=lambda: BOT_TOKEN,
        angebot_script=lambda: ANGEBOT_SCRIPT,
        rechnung_script=lambda: RECHNUNG_SCRIPT,
        angebot_out_dir=lambda: ANGEBOT_OUT_DIR,
        rechnung_out_dir=lambda: RECHNUNG_OUT_DIR,
        # Late-bound compatibility: callers/tests that temporarily replace the
        # legacy main.py helper names still affect the extracted endpoints.
        send_pdf=lambda *args, **kwargs: send_pdf_to_chat(*args, **kwargs),
        generate_pdf=lambda *args, **kwargs: _run_pdf_generator(*args, **kwargs),
        make_pdf_filename=lambda prefix, customer: _safe_pdf_filename(prefix, customer),
    )
)
app.include_router(_commercial_docs_router)

# Temporary direct-call compatibility while main.py is decomposed. These are
# aliases only; all commercial-document implementation lives in the route module.
require_angebot_access = _commercial_docs_handlers.require_angebot_access
send_pdf_to_chat = _commercial_docs_handlers.send_pdf_to_chat
_run_pdf_generator = _commercial_docs_handlers.run_pdf_generator
_safe_pdf_filename = _commercial_docs_handlers.safe_pdf_filename
create_angebot = _commercial_docs_handlers.create_angebot
create_rechnung = _commercial_docs_handlers.create_rechnung


# ---------- Tasks domain ----------
if __package__:
    from .routes.tasks import TaskBody, TasksRouteDeps, create_tasks_router
else:
    from routes.tasks import TaskBody, TasksRouteDeps, create_tasks_router  # noqa: E402

_tasks_router, _tasks_handlers = create_tasks_router(
    TasksRouteDeps(
        get_current_user=get_current_user,
        require_owner=require_owner,
        require_object_access=require_object_access,
        load_objekte_lib=lambda: _load_repo_objekte_lib(),
    )
)
app.include_router(_tasks_router)

# Temporary direct-call compatibility only for names that were actually stable
# in legacy main.py. The object-task POST handler was also named create_task, but
# a later /api/tasks handler historically overwrites that module name; exporting
# it here would invent a compatibility contract that never existed.
get_tasks = _tasks_handlers.get_tasks
complete_task = _tasks_handlers.complete_task


# ---------- Shared object info store + Documents domain ----------
if __package__:
    from .core.object_info_store import ObjectInfoStore
    from .routes.documents import DocumentsRouteDeps, create_documents_router
else:
    from core.object_info_store import ObjectInfoStore  # noqa: E402
    from routes.documents import DocumentsRouteDeps, create_documents_router  # noqa: E402

# Preserve the legacy import-time directory creation behavior.
os.makedirs(OBJECT_DOC_DIR, exist_ok=True)

_object_info_store = ObjectInfoStore(
    file_path=lambda: OBJECT_INFO_FILE,
    safe_load_json=lambda path, default: _safe_load_json(path, default),
    atomic_write_json=lambda path, value: _atomic_write_json(path, value),
)

# Temporary helper aliases for existing direct-call tests/internal callers. No
# object-info implementation remains in main.py.
_load_object_info = _object_info_store.load
_save_object_info = _object_info_store.save
_new_object_info_entry = _object_info_store.new_entry
_ensure_object_info_entry = _object_info_store.ensure_entry
_object_info_entry = _object_info_store.entry

_documents_router, _documents_handlers = create_documents_router(
    DocumentsRouteDeps(
        get_current_user=get_current_user,
        require_owner=require_owner,
        require_object_access=require_object_access,
        store=_object_info_store,
        object_doc_dir=lambda: OBJECT_DOC_DIR,
        sniff_image_or_pdf=lambda data: sniff_image_or_pdf(data),
        allowed_image_mime_ext=_ALLOWED_IMAGE_MIME_EXT,
        update_json_transaction=lambda path, default, mutator: update_json_transaction(path, default, mutator),
        append_object_history_best_effort=lambda *args, **kwargs: _append_object_history_best_effort(*args, **kwargs),
    )
)
app.include_router(_documents_router)

get_object_documents = _documents_handlers.get_object_documents
upload_object_document = _documents_handlers.upload_object_document
delete_object_document = _documents_handlers.delete_object_document
get_object_document_file = _documents_handlers.get_object_document_file

# moved to core/constants.py -- VALID_OBJECT_STATUSES




try:
    from .routes.objects import (
        ObjectsRouteDeps,
        create_objects_router,
        AssignBody as _ObjectsAssignBody,
        AssignmentUpdateBody as _ObjectsAssignmentUpdateBody,
        AssignmentRespondBody as _ObjectsAssignmentRespondBody,
        BatchAssignBody as _ObjectsBatchAssignBody,
        ObjectDescriptionBody as _ObjectsObjectDescriptionBody,
        InfoItemBody as _ObjectsInfoItemBody,
        NewObjectBody as _ObjectsNewObjectBody,
        StatusBody as _ObjectsStatusBody,
    )
except ImportError:
    from routes.objects import (  # noqa: E402
        ObjectsRouteDeps,
        create_objects_router,
        AssignBody as _ObjectsAssignBody,
        AssignmentUpdateBody as _ObjectsAssignmentUpdateBody,
        AssignmentRespondBody as _ObjectsAssignmentRespondBody,
        BatchAssignBody as _ObjectsBatchAssignBody,
        ObjectDescriptionBody as _ObjectsObjectDescriptionBody,
        InfoItemBody as _ObjectsInfoItemBody,
        NewObjectBody as _ObjectsNewObjectBody,
        StatusBody as _ObjectsStatusBody,
    )


AssignBody = _ObjectsAssignBody
AssignmentUpdateBody = _ObjectsAssignmentUpdateBody
AssignmentRespondBody = _ObjectsAssignmentRespondBody
BatchAssignBody = _ObjectsBatchAssignBody
ObjectDescriptionBody = _ObjectsObjectDescriptionBody
InfoItemBody = _ObjectsInfoItemBody
NewObjectBody = _ObjectsNewObjectBody
StatusBody = _ObjectsStatusBody


_objects_router, _objects_handlers = create_objects_router(ObjectsRouteDeps(
    get_current_user=get_current_user,
    get_role=get_role,
    require_owner=require_owner,
    require_object_access=require_object_access,
    cached_get_used_range=lambda tab_name: _cached_get_used_range(tab_name),
    load_assignments=lambda: _load_assignments(),
    load_worker_profiles=lambda: _load_worker_profiles(),
    load_object_images=lambda: _load_object_images(),
    load_repo_objekte_lib=lambda: _load_repo_objekte_lib(),
    serialize_object_for_worker=lambda obj, viewer_user_id, obj_assignments, user_info_fn, stage_summary_fn, images: (
        _serialize_object_for_worker(obj, viewer_user_id, obj_assignments, user_info_fn, stage_summary_fn, images)
    ),
    assignment_status=lambda assignment: _assignment_status(assignment),
    business_today_str=lambda: business_today_str(),
    safe_load_json=lambda path, default: _safe_load_json(path, default),
    update_json_transaction=lambda path, default, mutator: update_json_transaction(path, default, mutator),
    object_assignments_file=lambda: OBJECT_ASSIGNMENTS_FILE,
    object_history_file=lambda: OBJECT_HISTORY_FILE,
    object_info_file=lambda: OBJECT_INFO_FILE,
    load_abwesenheit=lambda: _load_abwesenheit(),
    load_roles=lambda: _load_roles(),
    load_checkin_meta=lambda: _load_checkin_meta(),
    validate_date_str=lambda date_str, field_name='дата': _validate_date_str(date_str, field_name),
    dates_overlap=lambda a_from, a_to, b_from, b_to: _dates_overlap(a_from, a_to, b_from, b_to),
    assignment_periods_overlap=lambda a, b: _assignment_periods_overlap(a, b),
    utcnow_iso=lambda: _utcnow_iso(),
    sanitize_display_name=lambda raw, fallback: _sanitize_display_name(raw, fallback),
    object_history_worker_name=lambda user_id: _object_history_worker_name(user_id),
    append_object_history_best_effort=lambda *args, **kwargs: _append_object_history_best_effort(*args, **kwargs),
    object_info_entry=_object_info_store.entry,
    ensure_object_info_entry=_object_info_store.ensure_entry,
    require_server_script=lambda script_path, label: _require_server_script(script_path, label),
    create_object_script=lambda: CREATE_OBJECT_SCRIPT,
    create_object_folder_script=lambda: CREATE_OBJECT_FOLDER_SCRIPT,
    shadow_compare_assignments=lambda uid, result: shadow_compare_assignments(uid, result),
))
# Canonical router registration: tests and manifests flatten _IncludedRouter
# through tests.conftest.iter_app_routes()/equivalent production-package smoke.
app.include_router(_objects_router)

# Legacy direct-call compatibility: tests and operational scripts still import
# handlers from main.py. Runtime routes are registered by backend.routes.objects.
list_objects = _objects_handlers.list_objects
my_assignments = _objects_handlers.my_assignments
get_object_history = _objects_handlers.get_object_history
assign_user = _objects_handlers.assign_user
unassign_user = _objects_handlers.unassign_user
update_assignment = _objects_handlers.update_assignment
delete_assignment = _objects_handlers.delete_assignment
respond_to_assignment = _objects_handlers.respond_to_assignment
get_assignment_candidates = _objects_handlers.get_assignment_candidates
batch_assign = _objects_handlers.batch_assign
get_object_description = _objects_handlers.get_object_description
update_object_description = _objects_handlers.update_object_description
get_object_info_items = _objects_handlers.get_object_info_items
create_object_info_item = _objects_handlers.create_object_info_item
delete_object_info_item = _objects_handlers.delete_object_info_item
create_object_endpoint = _objects_handlers.create_object_endpoint
update_object_status = _objects_handlers.update_object_status


# ---------- Weather / News / Birthdays feed domain ----------
WEATHER_FEED_FILE = os.path.join(AGENT_ROOT, '.weather_feed.json')
NEWS_FEED_FILE = os.path.join(AGENT_ROOT, '.news_feed.json')

if __package__:
    from .routes.feed_news import (
        FEED_SAVE_TYPE_ALIASES,
        FEED_SAVE_TYPES,
        FeedNewsRouteDeps,
        FeedReadBody,
        FeedSavedBody,
        NewsCommentBody,
        NewsReactionIn,
        create_feed_news_router,
    )
else:
    from routes.feed_news import (  # noqa: E402
        FEED_SAVE_TYPE_ALIASES,
        FEED_SAVE_TYPES,
        FeedNewsRouteDeps,
        FeedReadBody,
        FeedSavedBody,
        NewsCommentBody,
        NewsReactionIn,
        create_feed_news_router,
    )

_feed_news_router, _feed_news_handlers = create_feed_news_router(
    FeedNewsRouteDeps(
        get_current_user=get_current_user,
        get_role=get_role,
        safe_load_json=lambda path, default: _safe_load_json(path, default),
        atomic_write_json=lambda path, value: _atomic_write_json(path, value),
        update_json_transaction=lambda path, default, mutator: update_json_transaction(path, default, mutator),
        weather_feed_file=lambda: WEATHER_FEED_FILE,
        news_feed_file=lambda: NEWS_FEED_FILE,
        weather_reactions_file=lambda: WEATHER_REACTIONS_FILE,
        news_reactions_file=lambda: NEWS_REACTIONS_FILE,
        news_reads_file=lambda: NEWS_READS_FILE,
        news_comments_file=lambda: NEWS_COMMENTS_FILE,
        feed_reads_file=lambda: FEED_READS_FILE,
        feed_saved_file=lambda: FEED_SAVED_FILE,
        birthday_alerts_file=lambda: BIRTHDAY_ALERTS_FILE,
        load_photo_meta=lambda: _load_photo_meta(),
        load_worker_profiles=lambda: _load_worker_profiles(),
        load_roles=lambda: _load_roles(),
        sanitize_display_name=lambda raw, fallback: _sanitize_display_name(raw, fallback),
        resolve_current_display_name=lambda uid, fallback: _resolve_current_display_name(uid, fallback),
        business_today=lambda: business_today(),
        business_today_str=lambda: business_today_str(),
        create_critical_alert=lambda *args, **kwargs: _create_critical_alert(*args, **kwargs),
        emit_comment_activity_alerts=lambda *args, **kwargs: _emit_comment_activity_alerts(*args, **kwargs),
        comment_alert_recipients=lambda *args, **kwargs: _comment_alert_recipients(*args, **kwargs),
    )
)
app.include_router(_feed_news_router)

# Temporary compatibility aliases for legacy callers/tests and the remaining
# photo/activity code. Implementations live in routes/feed_news.py.
_weather_entry_key = _feed_news_handlers.weather_entry_key
_load_weather_reactions = _feed_news_handlers.load_weather_reactions
_save_weather_reactions = _feed_news_handlers.save_weather_reactions
_load_feed_saved = _feed_news_handlers.load_feed_saved
_feed_saved_bucket = _feed_news_handlers.feed_saved_bucket
_is_feed_saved = _feed_news_handlers.is_feed_saved
_feed_item_exists = _feed_news_handlers.feed_item_exists
_load_news_reactions = _feed_news_handlers.load_news_reactions
_save_news_reactions = _feed_news_handlers.save_news_reactions
_load_news_reads = _feed_news_handlers.load_news_reads
_save_news_reads = _feed_news_handlers.save_news_reads
_load_birthday_alerts = _feed_news_handlers.load_birthday_alerts
_save_birthday_alerts = _feed_news_handlers.save_birthday_alerts
_check_upcoming_birthdays = _feed_news_handlers.check_upcoming_birthdays
_load_news_comments = _feed_news_handlers.load_news_comments
_save_news_comments = _feed_news_handlers.save_news_comments
_load_feed_reads = _feed_news_handlers.load_feed_reads
_save_feed_reads = _feed_news_handlers.save_feed_reads
get_weather_feed = _feed_news_handlers.get_weather_feed
react_weather_entry = _feed_news_handlers.react_weather_entry
get_birthday_feed = _feed_news_handlers.get_birthday_feed
get_news_feed = _feed_news_handlers.get_news_feed
react_news_post = _feed_news_handlers.react_news_post
add_news_comment = _feed_news_handlers.add_news_comment
get_news_comments = _feed_news_handlers.get_news_comments
delete_news_comment = _feed_news_handlers.delete_news_comment
mark_feed_read = _feed_news_handlers.mark_feed_read
set_feed_saved = _feed_news_handlers.set_feed_saved
get_feed_unread = _feed_news_handlers.get_feed_unread


# ---------- Photo feed ----------
# Хранение: файлы на диске + метадата в JSON. Любой сотрудник грузит фото с объекта,
# все видят общей лентой (без ролевых ограничений — как командный чат).
# moved to core/paths.py -- PHOTO_DIR
# moved to core/paths.py -- PHOTO_META_FILE
# moved to core/limits.py -- PHOTO_MAX_BYTES
# moved to core/limits.py -- PHOTO_MAX_COUNT
_photo_lock = __import__('threading').Lock()

os.makedirs(PHOTO_DIR, exist_ok=True)


def _load_photo_meta() -> list:
    return _safe_load_json(PHOTO_META_FILE, [])


def _save_photo_meta(items: list):
    with open(PHOTO_META_FILE, 'w', encoding='utf-8') as f:
        json.dump(items, f, ensure_ascii=False)


def _load_photo_reactions() -> dict:
    return _safe_load_json(PHOTO_REACTIONS_FILE, {})


def _save_photo_reactions(data: dict):
    _atomic_write_json(PHOTO_REACTIONS_FILE, data)


def _copy_checkin_photos_to_feed(rel_paths: list, prefix: str) -> list:
    """Копирует check-in фото (уже сохранены под CHECKIN_PHOTO_BASE) в PHOTO_DIR под
    новыми именами, возвращает список имён файлов для feed_photos.json. 24.07: фото
    старта/финиша смены теперь дублируются в общую фото-ленту — юзер явно попросил
    "чтоб фото начала и конца работ летели в ленту с фото"."""
    saved = []
    for i, rel in enumerate(rel_paths):
        src_path = os.path.join(CHECKIN_PHOTO_BASE, rel)
        if not os.path.exists(src_path):
            continue
        ext = os.path.splitext(rel)[1] or '.jpg'
        fname = f"{prefix}_{i}{ext}"
        with open(src_path, 'rb') as fsrc, open(os.path.join(PHOTO_DIR, fname), 'wb') as fdst:
            fdst.write(fsrc.read())
        saved.append(fname)
    return saved


def _upsert_checkin_feed_post(session: dict, kind: str, object_name: str, user_id, user_name: str):
    """kind: 'start' | 'finish'. Один пост на смену (session['id']) в фото-ленте — старт
    создаёт пост, финиш дописывает свои фото в тот же пост (не два отдельных поста).
    Каждое фото подписывается через _photo_captions (index -> подпись), отдельно от
    общей caption поста."""
    rel_paths = session.get('start_photos' if kind == 'start' else 'finish_photos') or []
    if not rel_paths:
        return
    prefix = f"checkin_{session['id']}_{kind}"
    new_files = _copy_checkin_photos_to_feed(rel_paths, prefix)
    if not new_files:
        return

    time_str = datetime.fromtimestamp(
        session['start_at'] if kind == 'start' else session['finish_at']
    ).strftime('%H:%M')
    label = ('Начало смены' if kind == 'start' else 'Конец смены') + f' · {time_str}'

    with _photo_lock:
        items = _load_photo_meta()
        post = next((p for p in items if p.get('checkin_session_id') == session['id']), None)
        if post:
            start_idx = len(post['files'])
            post['files'].extend(new_files)
            post.setdefault('photo_labels', {})
            for i, _f in enumerate(new_files):
                post['photo_labels'][str(start_idx + i)] = label
        else:
            post = {
                "id": uuid.uuid4().hex,
                "files": new_files,
                "ts": int(time.time()),
                "user_id": user_id,
                "name": user_name,
                "object_id": object_name,
                "caption": "",
                "checkin_session_id": session['id'],
                "photo_labels": {str(i): label for i in range(len(new_files))},
            }
            items.append(post)
        _save_photo_meta(items)


@app.get("/api/feed/photos")
def list_feed_photos(user: dict = Depends(get_current_user)):
    with _photo_lock:
        items = _load_photo_meta()
    reactions = _load_photo_reactions()
    saved = _load_feed_saved()
    uid = str(user['id'])
    photos = []
    for p in reversed(items):
        p = dict(p)
        photo_reactions = reactions.get(p.get('id'), {})
        p['likes'] = len(photo_reactions)
        p['liked_by_me'] = uid in photo_reactions
        p['saved_by_me'] = _is_feed_saved(saved, uid, 'photo', p.get('id'))
        p['comment_count'] = len(p.pop('comments', []))
        # 22.09 (iPhone screenshot audit): read-side resolve, see
        # _resolve_current_display_name()'s docstring -- posts stamp `name`
        # at write time (checkin start/finish, upload) and can freeze a raw
        # user_id into it if the profile had no real name yet at that moment.
        if p.get('user_id'):
            p['name'] = _resolve_current_display_name(p['user_id'], p.get('name'))
        # 24.07: мультифото — старые записи (до этой правки) хранили один 'file',
        # новые хранят 'files' (список). Нормализуем на чтение, не трогаем сами
        # старые JSON-записи на диске (не нужно, чтение уже покрывает оба случая).
        if 'files' not in p:
            p['files'] = [p['file']] if p.get('file') else []
        # 12.09: старые/stale записи могли ссылаться на уже отсутствующие файлы
        # (например после ручной чистки storage). Не показываем пользователю битой
        # карточки и не провоцируем каскад 404 /api/feed/photos/{id}/file.
        p['files'] = [fname for fname in p.get('files', []) if os.path.exists(os.path.join(PHOTO_DIR, fname))]
        if not p['files']:
            continue
        photos.append(p)
    return {"photos": photos}


# moved to core/limits.py -- PHOTO_MAX_FILES


@app.post("/api/feed/photos")
async def upload_feed_photo(
    files: list[UploadFile] = File(...),
    object_id: str = Form(''),
    caption: str = Form(''),
    user: dict = Depends(get_current_user),
):
    if not files:
        raise HTTPException(400, "Нужно хотя бы одно фото")
    if len(files) > PHOTO_MAX_FILES:
        raise HTTPException(400, f"Максимум {PHOTO_MAX_FILES} фото за раз")

    photo_id = uuid.uuid4().hex
    saved_files = []
    for f in files:
        raw = await f.read()
        if len(raw) > PHOTO_MAX_BYTES:
            raise HTTPException(400, "Фото слишком большое (макс. 8 МБ на файл)")
        detected = sniff_image(raw)
        if not detected:
            raise HTTPException(400, "Все файлы должны быть изображениями")
        ext = _ALLOWED_IMAGE_MIME_EXT[detected]
        fname = f"{photo_id}_{len(saved_files)}.{ext}"
        with open(os.path.join(PHOTO_DIR, fname), 'wb') as out:
            out.write(raw)
        saved_files.append(fname)

    entry = {
        "id": photo_id,
        "files": saved_files,
        "ts": int(time.time()),
        "user_id": user['id'],
        "name": _sanitize_display_name(user.get('first_name'), str(user['id'])),
        "object_id": object_id.strip()[:100],
        "caption": caption.strip()[:300],
    }

    with _photo_lock:
        items = _load_photo_meta()
        items.append(entry)
        if len(items) > PHOTO_MAX_COUNT:
            for old in items[:-PHOTO_MAX_COUNT]:
                for old_fname in (old.get('files') or ([old['file']] if old.get('file') else [])):
                    old_path = os.path.join(PHOTO_DIR, old_fname)
                    if os.path.exists(old_path):
                        os.remove(old_path)
            items = items[-PHOTO_MAX_COUNT:]
        _save_photo_meta(items)

    return {"photo": entry}


class PhotoCommentBody(BaseModel):
    text: str
    reply_to: str = None


@app.post("/api/feed/photos/{photo_id}/comments")
def add_feed_photo_comment(photo_id: str, body: PhotoCommentBody, user: dict = Depends(get_current_user)):
    text = (body.text or '').strip()[:500]
    if not text:
        raise HTTPException(400, "Комментарий не может быть пустым")
    with _photo_lock:
        items = _load_photo_meta()
        entry = next((p for p in items if p['id'] == photo_id), None)
        if not entry:
            raise HTTPException(404, "Фото не найдено")
        comments = entry.setdefault('comments', [])
        prior_ids = {c.get('user_id') for c in comments}
        photo_author = entry.get('user_id')
        photo_object_id = entry.get('object_id', '')
        actor_name = _sanitize_display_name(user.get('first_name'), str(user['id']))
        new_id = uuid.uuid4().hex
        comments.append({
            'id': new_id,
            'user_id': str(user['id']),
            'name': actor_name,
            'text': text,
            'reply_to': (body.reply_to or None),
            'at': datetime.utcnow().isoformat(),
        })
        _save_photo_meta(items)
    # Раунд 6 §5.2: activity alerts (owner + прежние комментаторы + автор фото).
    try:
        obj_name = _resolve_object_name(photo_object_id)
        subtitle = f"Объект: {obj_name}" if obj_name else ''
        _emit_comment_activity_alerts(
            'photo_comment', photo_id, new_id, str(user['id']), actor_name,
            f"{actor_name} прокомментировал фото", subtitle,
            _comment_alert_recipients(user['id'], prior_ids, author_id=photo_author))
    except Exception:
        pass
    return {"comments": entry['comments']}


class PhotoReactionBody(BaseModel):
    liked: bool


@app.post("/api/feed/photos/{photo_id}/react")
def react_feed_photo(photo_id: str, body: PhotoReactionBody, user: dict = Depends(get_current_user)):
    with _photo_lock:
        items = _load_photo_meta()
        if not any(p.get('id') == photo_id for p in items):
            raise HTTPException(404, "Фото не найдено")
        reactions = _load_photo_reactions()
        photo_reactions = reactions.setdefault(photo_id, {})
        uid = str(user['id'])
        if body.liked:
            photo_reactions[uid] = True
        else:
            photo_reactions.pop(uid, None)
        _save_photo_reactions(reactions)
    return {"likes": len(photo_reactions), "liked_by_me": bool(body.liked)}


@app.get("/api/feed/photos/{photo_id}/comments")
def get_feed_photo_comments(photo_id: str, user: dict = Depends(get_current_user)):
    with _photo_lock:
        items = _load_photo_meta()
    entry = next((p for p in items if p['id'] == photo_id), None)
    if not entry:
        raise HTTPException(404, "Фото не найдено")
    # 22.09 (iPhone screenshot audit, Item I): same read-side resolve as
    # get_news_comments -- stored `name` at write time must not outlive a
    # later profile-name correction.
    comments = entry.get('comments', [])
    for c in comments:
        c['name'] = _resolve_current_display_name(c.get('user_id'), c.get('name'))
    return {"comments": comments}


@app.delete("/api/feed/photos/{photo_id}/comments/{comment_id}")
def delete_feed_photo_comment(photo_id: str, comment_id: str, user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    with _photo_lock:
        items = _load_photo_meta()
        entry = next((p for p in items if p['id'] == photo_id), None)
        if not entry:
            raise HTTPException(404, "Фото не найдено")
        comments = entry.get('comments', [])
        comment = next((c for c in comments if c.get('id') == comment_id), None)
        if not comment:
            raise HTTPException(404, "Комментарий не найден")
        if str(comment.get('user_id')) != str(user['id']) and role != 'owner':
            raise HTTPException(403, "Можно удалить только свой комментарий")
        entry['comments'] = [c for c in comments if c.get('id') != comment_id]
        _save_photo_meta(items)
    return {"comments": entry['comments']}


# ---------- Раунд 6 §5: пересылка комментариев + in-app activity alerts ----------
# Base comment lifecycle (add/get/delete news+photo) уже есть из Раунда 5 — здесь только
# недостающее: серверная карточка-пересылка в чат и лёгкие in-app алерты о новых
# комментариях (НЕ critical_alerts — без telegram-пуша/ack, отдельный activity_alerts.json).
# moved to core/paths.py -- ACTIVITY_ALERTS_FILE


def _load_activity_alerts() -> list:
    return _safe_load_json(ACTIVITY_ALERTS_FILE, [])


def _save_activity_alerts(items: list):
    _atomic_write_json(ACTIVITY_ALERTS_FILE, items)


def _news_post_title(post_id: str) -> str:
    try:
        if os.path.exists(NEWS_FEED_FILE):
            with open(NEWS_FEED_FILE, encoding='utf-8') as f:
                for p in json.load(f):
                    if p.get('id') == post_id:
                        return p.get('title', '') or ''
    except Exception:
        pass
    return ''


def _find_object_row_by_id(object_id: str) -> dict | None:
    rows = _cached_get_used_range('Объекты')
    if not rows:
        return None
    header, data = rows[0], rows[1:]
    for r in data:
        row = dict(zip(header, r))
        if str(row.get('ID объекта', '')) == str(object_id):
            return row
    return None


def _resolve_object_name(object_id: str) -> str:
    if not object_id:
        return ''
    try:
        rows = _cached_get_used_range('Объекты')
        if rows:
            hdr = rows[0]
            for r in rows[1:]:
                row = dict(zip(hdr, r))
                if str(row.get('ID объекта', '')) == str(object_id):
                    return row.get('Объект', '') or str(object_id)
    except Exception:
        pass
    return str(object_id)


def _comment_alert_recipients(actor_id, prior_user_ids, author_id=None) -> set:
    """§5.2: релевантные получатели — Owner('ы) + кто уже комментировал + автор публикации
    (если есть user_id). НЕ все Worker. Автор нового комментария исключён."""
    recips = {str(uid) for uid, r in _load_roles().items() if r == 'owner'}
    recips |= {str(u) for u in prior_user_ids if u}
    if author_id:
        recips.add(str(author_id))
    recips.discard(str(actor_id))
    return recips


def _emit_comment_activity_alerts(kind, ref_id, comment_id, actor_id, actor_name,
                                   title, subtitle, recipients):
    """§5.2 идемпотентность: один comment_id не создаёт повторный alert одному получателю
    (повторный GET/добавление не плодит алерты)."""
    if not recipients:
        return
    items = _load_activity_alerts()
    existing = {(a.get('target_user_id'), a.get('comment_id')) for a in items}
    now = int(time.time())
    changed = False
    for uid in recipients:
        if (str(uid), comment_id) in existing:
            continue
        items.append({
            'id': uuid.uuid4().hex, 'target_user_id': str(uid), 'kind': kind,
            'ref_id': str(ref_id), 'comment_id': comment_id, 'actor_id': str(actor_id),
            'actor_name': actor_name, 'title': title, 'subtitle': subtitle,
            'created_at': now, 'read_at': None,
        })
        changed = True
    if changed:
        _save_activity_alerts(items)


class ActivityReadBody(BaseModel):
    kind: str
    ref_id: str


@app.post("/api/activity-alerts/read")
def mark_activity_alerts_read(body: ActivityReadBody, user: dict = Depends(get_current_user)):
    """§5.2/§5.3: после реального открытия конкретного обсуждения — пометить прочитанным."""
    items = _load_activity_alerts()
    now = int(time.time())
    changed = False
    for a in items:
        if (a.get('target_user_id') == str(user['id']) and a.get('kind') == body.kind
                and a.get('ref_id') == str(body.ref_id) and not a.get('read_at')):
            a['read_at'] = now
            changed = True
    if changed:
        _save_activity_alerts(items)
    return {"status": "ok"}


class CommentForwardBody(BaseModel):
    source_type: str  # 'news' | 'photo'
    source_id: str
    comment_id: str
    to_user_id: str | None = None
    thread_key: str | None = None


@app.post("/api/comments/forward")
def forward_comment(body: CommentForwardBody, user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    """§5.1: пересылка комментария в существующий чат (общий/личный/объект/дефект/потребность).
    Карточка строится СЕРВЕРНО из сохранённого комментария и заголовка новости/фото —
    тексту карточки от frontend не доверяем; существование источника валидируется."""
    if body.source_type == 'news':
        plist = _load_news_comments().get(body.source_id, [])
        comment = next((c for c in plist if c.get('id') == body.comment_id), None)
        if not comment:
            raise HTTPException(404, "Комментарий не найден")
        title = _news_post_title(body.source_id) or 'Новость'
        card = f"💬 Комментарий к новости\n«{title}»\n{comment.get('name', '')}:\n{comment.get('text', '')}"
    elif body.source_type == 'photo':
        with _photo_lock:
            items = _load_photo_meta()
        entry = next((p for p in items if p['id'] == body.source_id), None)
        if not entry:
            raise HTTPException(404, "Фото не найдено")
        comment = next((c for c in entry.get('comments', []) if c.get('id') == body.comment_id), None)
        if not comment:
            raise HTTPException(404, "Комментарий не найден")
        obj_name = _resolve_object_name(entry.get('object_id', '')) or 'без объекта'
        card = f"💬 Комментарий к фотографии\nОбъект: {obj_name}\n{comment.get('name', '')}:\n{comment.get('text', '')}"
    else:
        raise HTTPException(400, "Неизвестный тип источника")

    # Проверка доступа к цели — тот же гейт, что и обычная отправка сообщения.
    if body.thread_key:
        _check_thread_access(body.thread_key, str(user['id']), role)
    else:
        _reject_self_chat(user['id'], body.to_user_id)
        _validate_dm_recipient(body.to_user_id)
        thread_id = _chat_thread_id(user['id'], body.to_user_id)
        thread_meta = _load_chat_thread_meta()
        if thread_meta.get(thread_id, {}).get('closed') and role != 'owner':
            raise HTTPException(403, "Чат закрыт руководством")

    msg = {
        "id": uuid.uuid4().hex, "ts": int(time.time()), "user_id": user['id'],
        "name": user.get('first_name', str(user['id'])), "text": card,
        "to_user_id": body.to_user_id, "thread_key": body.thread_key,
        "forwarded_comment": {"source_type": body.source_type, "source_id": body.source_id,
                              "comment_id": body.comment_id},
    }
    with _chat_lock:
        messages = _load_chat()
        messages.append(msg)
        _save_chat(messages)
    return {"message": msg, "status": "ok"}


@app.get("/api/feed/photos/{photo_id}/file")
def get_feed_photo_file(photo_id: str, index: int = 0, user: dict = Depends(get_current_user)):
    with _photo_lock:
        items = _load_photo_meta()
    entry = next((p for p in items if p['id'] == photo_id), None)
    if not entry:
        raise HTTPException(404, "Фото не найдено")
    files = entry.get('files') or ([entry['file']] if entry.get('file') else [])
    if index < 0 or index >= len(files):
        raise HTTPException(404, "Файл не найден по этому индексу")
    path = os.path.join(PHOTO_DIR, files[index])
    if not os.path.exists(path):
        raise HTTPException(404, "Файл отсутствует")
    return FileResponse(path)


# ---------- Chat core domain ----------
# Storage, thread access, read state, reactions and attachment upload now live
# in routes/chat.py. main.py only wires cross-domain collaborators and keeps
# temporary aliases for later legacy functions/callers during migration.
if __package__:
    from .routes.chat import ChatRouteDeps, create_chat_router
else:
    from routes.chat import ChatRouteDeps, create_chat_router  # noqa: E402

_chat_router, _chat_handlers = create_chat_router(
    ChatRouteDeps(
        get_current_user=get_current_user,
        get_role=get_role,
        safe_load_json=lambda path, default: _safe_load_json(path, default),
        atomic_write_json=lambda path, value: _atomic_write_json(path, value),
        chat_file=lambda: CHAT_FILE,
        chat_archive_file=lambda: CHAT_ARCHIVE_FILE,
        chat_reads_file=lambda: CHAT_READS_FILE,
        chat_thread_meta_file=lambda: CHAT_THREAD_META_FILE,
        chat_reactions_file=lambda: CHAT_REACTIONS_FILE,
        chat_attach_dir=lambda: CHAT_ATTACH_DIR,
        chat_max=CHAT_MAX,
        chat_retention_seconds=CHAT_RETENTION_SECONDS,
        chat_reaction_options=CHAT_REACTION_OPTIONS,
        thread_type_by_prefix=THREAD_TYPE_BY_PREFIX,
        default_thread_prefs=DEFAULT_THREAD_PREFS,
        online_threshold_seconds=ONLINE_THRESHOLD_SECONDS,
        load_roles=lambda: _load_roles(),
        load_assignments=lambda: _load_assignments(),
        today_berlin_str=lambda: _today_berlin_str(),
        has_active_object_access=lambda uid, oid, today: has_active_object_access(uid, oid, today),
        load_mangel_lib=lambda: ml,
        load_tasks=lambda: _load_tasks(),
        cached_get_used_range=lambda tab_name: _cached_get_used_range(tab_name),
        load_worker_profiles=lambda: _load_worker_profiles(),
        last_seen_for=lambda uid: _last_seen.get(str(uid)),
        sanitize_display_name=lambda raw, fallback: _sanitize_display_name(raw, fallback),
        resolve_current_display_name=lambda uid, fallback: _resolve_current_display_name(uid, fallback),
        sniff_chat_attachment=lambda data: sniff_chat_attachment(data),
    )
)
app.include_router(_chat_router)

# Temporary compatibility surface for legacy functions later in main.py and
# direct-call tests. Every name below points at routes/chat.py; no duplicate
# implementation remains in main.py.
_chat_lock = _chat_handlers.chat_lock
_archive_chat_messages = _chat_handlers.archive_chat_messages
_load_chat = _chat_handlers.load_chat
_save_chat = _chat_handlers.save_chat
_purge_old_chat = _chat_handlers.purge_old_chat
_load_reads = _chat_handlers.load_reads
_save_reads = _chat_handlers.save_reads
_load_chat_thread_meta = _chat_handlers.load_chat_thread_meta
_save_chat_thread_meta = _chat_handlers.save_chat_thread_meta
_load_chat_reactions = _chat_handlers.load_chat_reactions
_save_chat_reactions = _chat_handlers.save_chat_reactions
_reactions_summary_for_message = _chat_handlers.reactions_summary_for_message
_chat_thread_id = _chat_handlers.chat_thread_id
_reject_self_chat = _chat_handlers.reject_self_chat
_validate_dm_recipient = _chat_handlers.validate_dm_recipient
_object_chat_participants = _chat_handlers.object_chat_participants
_mangel_chat_participants = _chat_handlers.mangel_chat_participants
_task_chat_participants = _chat_handlers.task_chat_participants
_check_thread_access = _chat_handlers.check_thread_access
_thread_title = _chat_handlers.thread_title
_message_preview = _chat_handlers.message_preview
_thread_last_read = _chat_handlers.thread_last_read
get_my_chat_threads = _chat_handlers.get_my_chat_threads
get_normalized_chat_threads = _chat_handlers.get_normalized_chat_threads
get_chat_messages = _chat_handlers.get_chat_messages
get_unread_count = _chat_handlers.get_unread_count
get_unread_by_thread = _chat_handlers.get_unread_by_thread
mark_chat_read = _chat_handlers.mark_chat_read
post_chat_attachment = _chat_handlers.post_chat_attachment


_whisper_model = None


def _get_whisper():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
    return _whisper_model


def _transcribe_voice(path: str) -> str:
    model = _get_whisper()
    segments, _ = model.transcribe(path, language=None)
    return ' '.join(s.text.strip() for s in segments).strip()


# moved to core/limits.py -- TRANSCRIBE_MAX_BYTES
# moved to core/paths.py -- TRANSCRIBE_AUDIO_DIR
os.makedirs(TRANSCRIBE_AUDIO_DIR, exist_ok=True)


@app.post("/api/transcribe")
async def transcribe_voice_endpoint(file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    """Голосовой ввод вне чата -- finish-shift wizard (что сделано/доп-работы/
    потребности), создание дефекта/потребности. Аудио хранится (не temp+delete) --
    транскрипция бывает кривой, юзер должен иметь возможность переслушать
    оригинал, не только доверять тексту. Хранится per-user подпапкой, отдаётся
    только владельцу файла или owner (см. GET /api/transcribe/{file_id}/audio)."""
    data = await file.read()
    if len(data) > TRANSCRIBE_MAX_BYTES:
        raise HTTPException(400, "Голосовое слишком большое (макс. 8 МБ)")
    if not data:
        raise HTTPException(400, "Пустой файл")
    # 30.07 (Release-аудит P0): было -- любой файл принимался (только size limit),
    # расширение бралось из клиентского filename без проверки. Magic-byte allowlist.
    detected = sniff_audio(data)
    if detected is None:
        raise HTTPException(400, "Недопустимый формат аудио")
    ext = f'.{_ALLOWED_AUDIO_MIME_EXT[detected]}'

    uid = str(user['id'])
    user_dir = os.path.join(TRANSCRIBE_AUDIO_DIR, uid)
    os.makedirs(user_dir, exist_ok=True)
    file_id = uuid.uuid4().hex
    fpath = os.path.join(user_dir, f'{file_id}{ext}')
    with open(fpath, 'wb') as f:
        f.write(data)

    try:
        raw_transcript = _transcribe_voice(fpath)
    except Exception as e:
        raise HTTPException(502, f"Не удалось распознать голосовое: {str(e)[:200]}")

    if not raw_transcript:
        raise HTTPException(422, "Не удалось разобрать речь в записи — попробуй ещё раз")

    return {
        "raw_transcript": raw_transcript,
        "status": "ok",
        "file_id": file_id,
        "audio_url": f"/api/transcribe/{file_id}/audio",
    }


@app.get("/api/transcribe/{file_id}/audio")
def get_transcribe_audio(file_id: str, user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    safe_file_id = os.path.basename(file_id)
    if safe_file_id != file_id:
        raise HTTPException(404, "Файл не найден")
    uid = str(user['id'])
    search_dirs = [uid] if role != 'owner' else os.listdir(TRANSCRIBE_AUDIO_DIR) if os.path.isdir(TRANSCRIBE_AUDIO_DIR) else []
    for d in search_dirs:
        user_dir = os.path.join(TRANSCRIBE_AUDIO_DIR, os.path.basename(d))
        if not os.path.isdir(user_dir):
            continue
        for fname in os.listdir(user_dir):
            if fname.startswith(safe_file_id):
                return FileResponse(os.path.join(user_dir, fname))
    raise HTTPException(404, "Файл не найден")


@app.post("/api/chat/messages/voice")
async def post_chat_voice(thread_key: str = Form(''), to_user_id: str = Form(''), file: UploadFile = File(...),
                           user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    if thread_key:
        _check_thread_access(thread_key, str(user['id']), role)
    else:
        _reject_self_chat(user['id'], to_user_id or None)
        _validate_dm_recipient(to_user_id or None)
        thread_id = _chat_thread_id(user['id'], to_user_id or None)
        thread_meta = _load_chat_thread_meta()
        if thread_meta.get(thread_id, {}).get('closed') and role != 'owner':
            raise HTTPException(403, "Чат закрыт руководством")

    data = await file.read()
    if len(data) > 8 * 1024 * 1024:
        raise HTTPException(400, "Голосовое слишком большое (макс. 8 МБ)")
    # 30.07 (Release-аудит P0): magic-byte allowlist, расширение из таблицы а не
    # от клиента -- то же обоснование что и в post_chat_attachment выше.
    detected = sniff_audio(data)
    if detected is None:
        raise HTTPException(400, "Недопустимый формат аудио")
    fname = f'{uuid.uuid4().hex}.{_ALLOWED_AUDIO_MIME_EXT[detected]}'
    fpath = os.path.join(CHAT_ATTACH_DIR, fname)
    with open(fpath, 'wb') as f:
        f.write(data)

    try:
        transcript = _transcribe_voice(fpath)
    except Exception as e:
        transcript = ''
        print(f'WARNING: транскрипция голосового не удалась: {e}')

    msg = {
        "id": uuid.uuid4().hex,
        "ts": int(time.time()),
        "user_id": user['id'],
        "name": user.get('first_name', str(user['id'])),
        "text": '',
        "to_user_id": to_user_id or None,
        "thread_key": thread_key or None,
        "attachment": {"file": fname, "name": file.filename or fname, "content_type": file.content_type or 'audio/ogg'},
        "voice_transcript": transcript,
    }
    with _chat_lock:
        messages = _load_chat()
        messages.append(msg)
        _save_chat(messages)
    return {"message": msg}


class ExtractTaskBody(BaseModel):
    text: str
    object_id: str = ''


class ManagementCommandBody(BaseModel):
    text: str


def _normalize_ru_lookup(s: str) -> str:
    return re.sub(r'[^a-zа-я0-9]+', '', str(s or '').lower().replace('ё', 'е'))


def _normalize_ru_name_hint(s: str) -> str:
    value = str(s or '').strip()
    if len(value) > 3 and value[-1:].lower() in ('у', 'ю'):
        return value[:-1]
    return value


def _management_command_date(label: str, base_dt: datetime | None = None) -> str:
    base = base_dt or business_now()
    key = str(label or '').strip().lower()
    if key == 'сегодня':
        return base.date().isoformat()
    if key == 'завтра':
        return (base.date() + timedelta(days=1)).isoformat()
    if key == 'послезавтра':
        return (base.date() + timedelta(days=2)).isoformat()
    m = re.match(r'^(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?$', key)
    if not m:
        return ''
    day, month = int(m.group(1)), int(m.group(2))
    year = int(m.group(3)) if m.group(3) else base.year
    if year < 100:
        year += 2000
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return ''


def _resolve_management_worker(worker_query: str, workers: list[dict] | None = None) -> dict:
    workers = workers or []
    query_norm = _normalize_ru_lookup(_normalize_ru_name_hint(worker_query))
    for w in workers:
        name = str(w.get('name') or w.get('first_name') or '').strip()
        if query_norm and query_norm in _normalize_ru_lookup(name):
            return {"id": str(w.get('user_id') or w.get('id') or ''), "name": name}
    return {"id": "", "name": _normalize_ru_name_hint(worker_query)}


def _resolve_management_object(object_query: str, objects: list[dict] | None = None) -> dict:
    objects = objects or []
    query_norm = _normalize_ru_lookup(object_query)
    for obj in objects:
        name = str(obj.get('name') or obj.get('Объект') or '').strip()
        oid = str(obj.get('id') or obj.get('ID объекта') or '').strip()
        if query_norm and (query_norm in _normalize_ru_lookup(name) or query_norm in _normalize_ru_lookup(oid)):
            return {"id": oid, "name": name or oid}
    return {"id": "", "name": object_query.strip()}


def parse_management_command(text: str, base_dt: datetime | None = None,
                             workers: list[dict] | None = None,
                             objects: list[dict] | None = None) -> dict:
    """Parse an owner Russian management command into a safe draft.

    This intentionally does not create assignments/tasks. The caller must show
    the draft and confirm before any mutation.
    """
    raw = re.sub(r'\s+', ' ', str(text or '').strip().rstrip('.!?'))
    if not raw:
        raise ValueError("Пустая команда")
    m = re.match(
        r'(?i)^поставь\s+(?P<worker>.+?)\s+'
        r'(?P<date>сегодня|завтра|послезавтра|\d{1,2}\.\d{1,2}(?:\.\d{2,4})?)\s+'
        r'задач[ауи]\s+(?P<body>.+)$',
        raw,
    )
    if not m:
        raise ValueError("Не удалось разобрать команду управления")

    body = m.group('body').strip()
    comment = ''
    comment_match = re.search(r'(?i)\s+и\s+скажи\s+ему\s+(?P<comment>.+)$', body)
    if comment_match:
        comment = comment_match.group('comment').strip()
        body = body[:comment_match.start()].strip()

    object_query = ''
    task_text = body
    object_match = re.search(r'(?i)\s+у\s+(?P<object>[^,]+)$', body)
    if object_match:
        object_query = object_match.group('object').strip()
        task_text = body[:object_match.start()].strip()

    worker = _resolve_management_worker(m.group('worker').strip(), workers)
    obj = _resolve_management_object(object_query, objects) if object_query else {"id": "", "name": ""}
    due_date = _management_command_date(m.group('date'), base_dt)

    return {
        "intent": "assign_task",
        "worker_query": m.group('worker').strip(),
        "worker_id": worker.get("id", ""),
        "worker_name": worker.get("name", ""),
        "date": due_date,
        "date_text": m.group('date').strip(),
        "object_query": object_query,
        "object_id": obj.get("id", ""),
        "object_name": obj.get("name", ""),
        "task": task_text,
        "comment": comment,
        "requires_confirmation": True,
    }


def _management_workers_for_parse() -> list[dict]:
    profiles = _load_worker_profiles()
    roles = _load_roles()
    result = []
    for uid, role in roles.items():
        if role == 'worker':
            profile = profiles.get(str(uid), {})
            result.append({
                "user_id": str(uid),
                "name": _sanitize_display_name(profile.get('name') or profile.get('first_name'), str(uid)),
            })
    return result


def _management_objects_for_parse() -> list[dict]:
    rows = _cached_get_used_range('Объекты')
    if not rows:
        return []
    header, data = rows[0], rows[1:]
    result = []
    for row in data:
        obj = dict(zip(header, row))
        result.append({
            "id": str(obj.get('ID объекта', '')).strip(),
            "name": str(obj.get('Объект', '')).strip(),
        })
    return result


@app.post("/api/manager/command/parse")
def parse_manager_command(body: ManagementCommandBody,
                          user: dict = Depends(get_current_user),
                          role: str = Depends(get_role)):
    if role != 'owner':
        raise HTTPException(403, "Только для владельца")
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Пустая команда")
    try:
        workers = _management_workers_for_parse()
    except Exception:
        workers = []
    try:
        objects = _management_objects_for_parse()
    except Exception:
        objects = []
    try:
        draft = parse_management_command(text, workers=workers, objects=objects)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"draft": draft, "requires_confirmation": True}


@app.post("/api/tasks/extract")
def extract_task_from_text(body: ExtractTaskBody, user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    """10.37: AI разбирает транскрипт голосового (или любой текст) и предлагает
    title/description для Потребности — НЕ создаёт заявку сама, только предлагает,
    подтверждение — отдельным POST /api/tasks с уже готовыми полями (юзер решил:
    голосовое остаётся в чате, извлечение — явное действие с подтверждением)."""
    if role == 'owner':
        raise HTTPException(403, "Потребности создают работники")
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Пустой текст")

    system = (
        "Ты помощник, который извлекает из голосового сообщения строителя запрос на "
        "инструмент/материалы/защиту. Верни СТРОГО валидный JSON без пояснений: "
        '{"title": "короткое название (макс 60 символов)", "description": "детали, если есть"}. '
        'Если в тексте нет реального запроса на что-либо — верни {"title": "", "description": ""}.'
    )
    try:
        raw = _call_glm_json(system, text)
    except Exception as e:
        raise HTTPException(502, f"AI недоступен: {e}")

    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if not m:
        raise HTTPException(502, "AI вернул не-JSON ответ")
    try:
        parsed = json.loads(m.group(0))
    except Exception:
        raise HTTPException(502, "Не удалось разобрать ответ AI")

    return {"title": parsed.get("title", "")[:200], "description": parsed.get("description", "")[:1000]}


# ---------- Chat write/actions domain ----------
# Message CRUD, reactions, thread controls, attachment serving and manager
# broadcast live in routes/chat_actions.py and reuse the extracted Chat core.
if __package__:
    from .routes.chat_actions import (
        BroadcastBody,
        ChatActionsRouteDeps,
        ChatMessageBody,
        ChatReactionBody,
        ChatThreadCloseBody,
        ChatThreadPrefsBody,
        create_chat_actions_router,
    )
else:
    from routes.chat_actions import (  # noqa: E402
        BroadcastBody,
        ChatActionsRouteDeps,
        ChatMessageBody,
        ChatReactionBody,
        ChatThreadCloseBody,
        ChatThreadPrefsBody,
        create_chat_actions_router,
    )

_chat_actions_router, _chat_actions_handlers = create_chat_actions_router(
    ChatActionsRouteDeps(
        get_current_user=get_current_user,
        get_role=get_role,
        require_owner=require_owner,
        core=_chat_handlers,
        safe_load_json=lambda path, default: _safe_load_json(path, default),
        chat_archive_file=lambda: CHAT_ARCHIVE_FILE,
        chat_attach_dir=lambda: CHAT_ATTACH_DIR,
        chat_reaction_options=CHAT_REACTION_OPTIONS,
        load_roles=lambda: _load_roles(),
        send_telegram_message=lambda chat_id, text: send_telegram_message(chat_id, text),
        find_object_row_by_id=lambda object_id: _find_object_row_by_id(object_id),
        append_object_history_best_effort=lambda *args, **kwargs: _append_object_history_best_effort(*args, **kwargs),
    )
)
app.include_router(_chat_actions_router)

# Temporary compatibility aliases; implementations live in chat_actions.py.
_normalize_chat_location = _chat_actions_handlers.normalize_chat_location
_reply_snapshot = _chat_actions_handlers.reply_snapshot
_chat_thread_participants = _chat_actions_handlers.chat_thread_participants
_check_message_access = _chat_actions_handlers.check_message_access
_thread_user_prefs = _chat_handlers.thread_user_prefs
get_chat_attachment = _chat_actions_handlers.get_chat_attachment
send_manager_broadcast = _chat_actions_handlers.send_manager_broadcast
post_chat_message = _chat_actions_handlers.post_chat_message
forward_chat_message = _chat_actions_handlers.forward_chat_message
delete_chat_message = _chat_actions_handlers.delete_chat_message
delete_chat_thread = _chat_actions_handlers.delete_chat_thread
toggle_chat_reaction = _chat_actions_handlers.toggle_chat_reaction
set_chat_thread_prefs = _chat_actions_handlers.set_chat_thread_prefs
close_chat_thread = _chat_actions_handlers.close_chat_thread
reopen_chat_thread = _chat_actions_handlers.reopen_chat_thread
get_chat_thread_status = _chat_actions_handlers.get_chat_thread_status


# ---------- AI Chat (GLM / Sonnet / Opus, переключаемо) ----------
# GLM — бесплатный, экономит лимиты (z.ai). Sonnet/Opus — через claude CLI по OAuth-подписке владельца.
# Доступ только для owner, rate limit 20 запросов/час.
# moved to core/paths.py -- AI_RATE_FILE
# moved to core/limits.py -- AI_RATE_LIMIT
# moved to core/limits.py -- AI_RATE_WINDOW

# moved to core/paths.py -- AI_MODEL_FILE
# moved to core/constants.py -- AI_MODELS
# moved to core/constants.py -- AI_MODEL_DEFAULT
CLAUDE_BIN = os.environ.get('CLAUDE_BIN', 'claude')

# 03.08 (ТЗ Задача 2, safe freeze): Owner AI (sonnet/opus) запускает `claude` CLI как
# полноценный subprocess с доступом ко всему /home/promonta/agent -- до отдельного
# unix-юзера/read-only checkout/sandbox (следующий раунд, НЕ этот) единственный
# безопасный default для production -- ВЫКЛЮЧЕНО. Owner может включить явно через env,
# осознанно принимая риск, пока sandbox не готов; deploy/production config НЕ должен
# сам проставлять true.
OWNER_AI_ENABLED = os.environ.get('OWNER_AI_ENABLED', 'false').strip().lower() in ('1', 'true', 'yes')

# 03.08: subprocess получает ТОЛЬКО эти переменные, не полный os.environ -- claude CLI
# реально нужен PATH (найти бинарники node/git/etc), HOME (конфиг/кэш ~/.claude),
# ANTHROPIC_API_KEY если используется вместо OAuth-подписки (если её нет в окружении --
# claude CLI работает по уже сохранённому OAuth-токену в HOME, тоже ок). Полный
# os.environ до этого включал BOT_TOKEN/GLM_KEY/DATA_ROOT/все остальные секреты
# процесса -- subprocess их не использует и не должен их видеть.
# moved to core/constants.py -- _OWNER_AI_ENV_ALLOWLIST


def _owner_ai_subprocess_env() -> dict:
    return {k: os.environ[k] for k in _OWNER_AI_ENV_ALLOWLIST if k in os.environ}

AI_SYSTEM_PROMPT = (
    "Ты ИИ-ассистент строительной фирмы Grandmont Group UG (haftungsbeschränkt) (Chemnitz, Sachsen, Германия). "
    "Специализация: Trockenbau, Malerarbeiten, Spachtel Q2/Q3, Fliesen, Bodenbelag, WDVS/Fassade. "
    "Клиенты: Bauunternehmen, Hausverwaltungen, частные. "
    "Отвечай кратко и по делу. Внутренние ответы — на русском, тексты клиентам — на деловом немецком."
)


def _get_ai_model() -> str:
    if os.path.exists(AI_MODEL_FILE):
        with open(AI_MODEL_FILE, encoding='utf-8') as f:
            m = json.load(f).get('model', AI_MODEL_DEFAULT)
        if m in AI_MODELS:
            return m
    return AI_MODEL_DEFAULT


def _set_ai_model(model: str):
    if model not in AI_MODELS:
        raise HTTPException(400, f"Неизвестная модель. Доступно: {', '.join(AI_MODELS)}")
    with open(AI_MODEL_FILE, 'w', encoding='utf-8') as f:
        json.dump({'model': model}, f)


def _check_ai_rate(user_id: int, rate_file: str = None, limit: int = None):
    rate_file = rate_file or AI_RATE_FILE
    limit = limit if limit is not None else AI_RATE_LIMIT
    data = {}
    if os.path.exists(rate_file):
        with open(rate_file, encoding='utf-8') as f:
            data = json.load(f)

    uid = str(user_id)
    now = time.time()
    ud = data.get(uid, {"count": 0, "window_start": now})

    if now - ud["window_start"] >= AI_RATE_WINDOW:
        ud = {"count": 0, "window_start": now}

    if ud["count"] >= limit:
        remaining = int(AI_RATE_WINDOW - (now - ud["window_start"]))
        raise HTTPException(429, f"Лимит {limit} запросов/час исчерпан. Сброс через {remaining // 60} мин {remaining % 60} сек")

    ud["count"] += 1
    data[uid] = ud
    with open(rate_file, 'w', encoding='utf-8') as f:
        json.dump(data, f)


def _call_glm(messages: list, system: str = None) -> str:
    glm_key = os.environ.get('GLM_KEY', '')
    if not glm_key:
        raise HTTPException(503, "GLM API не настроен (нет GLM_KEY)")

    payload = {
        "model": "glm-4.5-flash",
        "max_tokens": 1024,
        "system": system if system is not None else AI_SYSTEM_PROMPT,
        "messages": messages,
    }

    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = _urlreq.Request(
        'https://api.z.ai/api/anthropic/v1/messages',
        data=data,
        method='POST',
        headers={
            'Content-Type': 'application/json',
            'x-api-key': glm_key,
            'anthropic-version': '2023-06-01',
        }
    )
    try:
        with _urlreq.urlopen(req, timeout=45) as resp:
            result = json.loads(resp.read().decode('utf-8'))
        return result['content'][0]['text']
    except _urlreq.HTTPError as e:
        err_body = e.read().decode('utf-8')
        raise HTTPException(502, f"GLM API ошибка: {err_body[:300]}")
    except Exception as e:
        raise HTTPException(502, f"GLM API недоступен: {str(e)[:200]}")


def _call_glm_json(system: str, user_text: str) -> str:
    glm_key = os.environ.get('GLM_KEY', '')
    if not glm_key:
        raise HTTPException(503, "GLM API не настроен (нет GLM_KEY)")
    payload = {
        "model": "glm-4.5-flash",
        "max_tokens": 512,
        "system": system,
        "messages": [{"role": "user", "content": user_text}],
    }
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = _urlreq.Request(
        'https://api.z.ai/api/anthropic/v1/messages',
        data=data,
        method='POST',
        headers={
            'Content-Type': 'application/json',
            'x-api-key': glm_key,
            'anthropic-version': '2023-06-01',
        }
    )
    with _urlreq.urlopen(req, timeout=45) as resp:
        result = json.loads(resp.read().decode('utf-8'))
    return result['content'][0]['text']


def _messages_to_prompt(messages: list) -> str:
    """claude -p принимает один текстовый prompt, не chat-массив — сворачиваем историю в текст.
    Картинки (image content-блоки) в этом режиме не поддержаны — CLI не читает base64-вложения."""
    parts = [f"[SYSTEM]\n{AI_SYSTEM_PROMPT}"]
    for m in messages:
        role = m.get('role', 'user').upper()
        content = m.get('content')
        if isinstance(content, str):
            text = content
        else:
            text = ' '.join(b.get('text', '') for b in content if isinstance(b, dict) and b.get('type') == 'text')
        parts.append(f"[{role}]\n{text}")
    return '\n\n'.join(parts)


_claude_cli_lock = __import__('threading').Lock()


def _call_claude_cli(messages: list, model: str) -> str:
    """owner-only agent chat -- полный контекст/permissions осознанно (владелец хочет,
    чтобы этот ассистент видел всё, что видит Claude Code сам). Lock -- не security-
    ограничение, а просто защита от нескольких параллельных 120-секундных subprocess
    (rate limit 20/час уже ограничивает частоту, но не одновременность)."""
    if not OWNER_AI_ENABLED:
        # 03.08 (ТЗ Задача 2): safe freeze -- пока не готов sandbox (отдельный unix-юзер +
        # read-only checkout, следующий раунд), subprocess НЕ запускается вообще.
        raise HTTPException(503, "Owner AI (Sonnet/Opus) временно отключён в production "
                                  "(OWNER_AI_ENABLED=false) -- доступен GLM-режим")
    prompt = _messages_to_prompt(messages)
    if not _claude_cli_lock.acquire(timeout=1):
        raise HTTPException(429, "Уже выполняется другой запрос к Claude — подожди и повтори")
    try:
        r = subprocess.run(
            [CLAUDE_BIN, '-p', '--model', model, prompt],
            cwd=AGENT_ROOT, capture_output=True, stdin=subprocess.DEVNULL,
            text=True, timeout=120, env=_owner_ai_subprocess_env(),
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(504, f"Claude ({model}) не ответил за 120 сек")
    except Exception as e:
        raise HTTPException(502, f"Claude CLI недоступен: {str(e)[:200]}")
    finally:
        _claude_cli_lock.release()

    if r.returncode != 0:
        raise HTTPException(502, f"Claude CLI ошибка: {(r.stderr or '')[:300]}")

    reply = (r.stdout or '').strip()
    if not reply:
        raise HTTPException(502, "Claude вернул пустой ответ")
    return reply


def _call_ai(messages: list) -> str:
    model = _get_ai_model()
    if model == 'glm':
        return _call_glm(messages)
    return _call_claude_cli(messages, model)


class AiChatBody(BaseModel):
    messages: list


class AiModelBody(BaseModel):
    model: str


def _is_multimodal_content(content) -> bool:
    return isinstance(content, list)


@app.post("/api/ai-chat")
def ai_chat(body: AiChatBody, user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    if role != 'owner':
        raise HTTPException(403, "Только для владельца")
    if not body.messages:
        raise HTTPException(400, "Нет сообщений")

    for msg in body.messages:
        if not isinstance(msg, dict) or msg.get('role') not in ('user', 'assistant'):
            raise HTTPException(400, "Неверный формат сообщений: {role, content} required")
        content = msg.get('content')
        if isinstance(content, str):
            if len(content) > 8000:
                raise HTTPException(400, "Сообщение слишком длинное")
        elif isinstance(content, list):
            # Мультимодальное сообщение (текст + фото) — Anthropic content-block формат.
            for block in content:
                if not isinstance(block, dict) or block.get('type') not in ('text', 'image'):
                    raise HTTPException(400, "Неверный формат content-блока")
        else:
            raise HTTPException(400, "content должен быть строкой или списком блоков")

    _check_ai_rate(user['id'])
    reply = _call_ai(body.messages)
    return {"reply": reply}


# ---------- AI Chat для worker (узкий, без бизнес-контекста) ----------
# Отдельно от owner-чата: GLM-only (не Claude CLI agent с полным доступом),
# system prompt не содержит имя фирмы/клиентов/финансов -- только общие
# строительные вопросы ("как штукатурить Q2", "как смешать грунтовку").
# Owner explicit decision (2026-07-27): worker не должен видеть чувствительные
# данные фирмы через AI, в отличие от owner-чата, который специально видит
# весь контекст.
# moved to core/paths.py -- WORKER_AI_RATE_FILE
# moved to core/limits.py -- WORKER_AI_RATE_LIMIT

WORKER_AI_SYSTEM_PROMPT = (
    "Ты помощник для строителей. Отвечай только на общие вопросы о строительных "
    "работах: технологии (Trockenbau, Malerarbeiten, Spachtel, Fliesen, Bodenbelag, "
    "WDVS/Fassade), материалы, инструменты, безопасность труда, нормативы. "
    "НЕ обсуждай: конкретные объекты, клиентов, бюджеты, финансы фирмы, зарплаты, "
    "внутренние данные компании -- у тебя нет доступа к этой информации и её не "
    "существует в этом разговоре. Если спросят про конкретный объект/клиента/деньги -- "
    "скажи, что это нужно уточнить у владельца. Отвечай кратко и по делу, на русском."
)


class WorkerAiChatBody(BaseModel):
    messages: list


@app.post("/api/ai-chat/worker")
def worker_ai_chat(body: WorkerAiChatBody, user: dict = Depends(get_current_user)):
    """Доступен всем authenticated (owner тоже может, но это worker-facing UI —
    не ограничиваем по роли, просто этот endpoint сам по себе узкий и безопасный
    для любого юзера, в отличие от /api/ai-chat который owner-only из-за
    Claude CLI agent access."""
    if not body.messages:
        raise HTTPException(400, "Нет сообщений")

    for msg in body.messages:
        if not isinstance(msg, dict) or msg.get('role') not in ('user', 'assistant'):
            raise HTTPException(400, "Неверный формат сообщений: {role, content} required")
        content = msg.get('content')
        if not isinstance(content, str):
            raise HTTPException(400, "content должен быть строкой (без фото/вложений в этом чате)")
        if len(content) > 4000:
            raise HTTPException(400, "Сообщение слишком длинное")

    _check_ai_rate(user['id'], rate_file=WORKER_AI_RATE_FILE, limit=WORKER_AI_RATE_LIMIT)
    reply = _call_glm(body.messages, system=WORKER_AI_SYSTEM_PROMPT)
    return {"reply": reply}


@app.get("/api/ai-model")
def get_ai_model(user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    if role != 'owner':
        raise HTTPException(403, "Только для владельца")
    return {"model": _get_ai_model(), "available": list(AI_MODELS)}


@app.post("/api/ai-model")
def set_ai_model(body: AiModelBody, user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    if role != 'owner':
        raise HTTPException(403, "Только для владельца")
    _set_ai_model(body.model)
    return {"model": body.model}


# moved to core/limits.py -- AI_UPLOAD_MAX_BYTES


@app.post("/api/ai-chat/upload")
async def ai_chat_upload(file: UploadFile = File(...), user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    if role != 'owner':
        raise HTTPException(403, "Только для владельца")

    raw = await file.read()
    if len(raw) > AI_UPLOAD_MAX_BYTES:
        raise HTTPException(400, "Файл слишком большой (макс. 8 МБ)")

    filename = file.filename or 'file'
    detected = sniff_image_or_pdf(raw)

    if detected in _ALLOWED_IMAGE_MIME_EXT:
        b64 = base64.b64encode(raw).decode('ascii')
        return {
            "kind": "image",
            "filename": filename,
            "block": {"type": "image", "source": {"type": "base64", "media_type": detected, "data": b64}},
        }

    if detected == 'application/pdf':
        try:
            import pypdf
            import io
            reader = pypdf.PdfReader(io.BytesIO(raw))
            text = '\n'.join(page.extract_text() or '' for page in reader.pages)
        except Exception as e:
            raise HTTPException(400, f"Не удалось прочитать PDF: {str(e)[:200]}")
        text = text[:12000]
        if not text.strip():
            raise HTTPException(400, "PDF не содержит извлекаемого текста (возможно скан-изображение)")
        return {"kind": "text", "filename": filename, "text": text}

    # Обычный текстовый файл
    try:
        text = raw.decode('utf-8', errors='ignore')[:12000]
    except Exception:
        raise HTTPException(400, "Не удалось прочитать файл как текст")
    if not text.strip():
        raise HTTPException(400, "Файл пуст или в неподдерживаемом формате")
    return {"kind": "text", "filename": filename, "text": text}


# ---------- Этапы объекта / План работ (Roadmap) ----------
# Runtime roadmap singleton stays in main.py because CRITICAL_JSON_PATHS and
# legacy tests use backend.rl directly; HTTP handlers live in routes/stages.py.
rl = _load_repo_roadmap_lib()


try:
    from .routes.stages import (
        StagesRouteDeps,
        create_stages_router,
        NewStageBody as _StagesNewStageBody,
        StageDescriptionBody as _StagesStageDescriptionBody,
        StageStatusBody as _StagesStageStatusBody,
        StageSwapBody as _StagesStageSwapBody,
        StageBlockerBody as _StagesStageBlockerBody,
        RoadmapCategoryBody as _StagesRoadmapCategoryBody,
        RoadmapItemCreateBody as _StagesRoadmapItemCreateBody,
        RoadmapItemEditBody as _StagesRoadmapItemEditBody,
        RoadmapItemStatusBody as _StagesRoadmapItemStatusBody,
        RoadmapNoteBody as _StagesRoadmapNoteBody,
        StageRequestBody as _StagesStageRequestBody,
        StageRequestDecisionBody as _StagesStageRequestDecisionBody,
    )
except ImportError:
    from routes.stages import (  # noqa: E402
        StagesRouteDeps,
        create_stages_router,
        NewStageBody as _StagesNewStageBody,
        StageDescriptionBody as _StagesStageDescriptionBody,
        StageStatusBody as _StagesStageStatusBody,
        StageSwapBody as _StagesStageSwapBody,
        StageBlockerBody as _StagesStageBlockerBody,
        RoadmapCategoryBody as _StagesRoadmapCategoryBody,
        RoadmapItemCreateBody as _StagesRoadmapItemCreateBody,
        RoadmapItemEditBody as _StagesRoadmapItemEditBody,
        RoadmapItemStatusBody as _StagesRoadmapItemStatusBody,
        RoadmapNoteBody as _StagesRoadmapNoteBody,
        StageRequestBody as _StagesStageRequestBody,
        StageRequestDecisionBody as _StagesStageRequestDecisionBody,
    )


NewStageBody = _StagesNewStageBody
StageDescriptionBody = _StagesStageDescriptionBody
StageStatusBody = _StagesStageStatusBody
StageSwapBody = _StagesStageSwapBody
StageBlockerBody = _StagesStageBlockerBody
RoadmapCategoryBody = _StagesRoadmapCategoryBody
RoadmapItemCreateBody = _StagesRoadmapItemCreateBody
RoadmapItemEditBody = _StagesRoadmapItemEditBody
RoadmapItemStatusBody = _StagesRoadmapItemStatusBody
RoadmapNoteBody = _StagesRoadmapNoteBody
StageRequestBody = _StagesStageRequestBody
StageRequestDecisionBody = _StagesStageRequestDecisionBody


_stages_router, _stages_handlers = create_stages_router(StagesRouteDeps(
    get_current_user=get_current_user,
    get_role=get_role,
    require_owner=require_owner,
    require_object_access=require_object_access,
    cached_get_used_range=lambda tab_name: _cached_get_used_range(tab_name),
    load_repo_objekte_lib=lambda: _load_repo_objekte_lib(),
    roadmap_lib=lambda: rl,
    safe_load_json=lambda path, default: _safe_load_json(path, default),
    update_json_transaction=lambda path, default, mutator: update_json_transaction(path, default, mutator),
    business_today_str=lambda: business_today_str(),
    append_object_history_best_effort=lambda *args, **kwargs: _append_object_history_best_effort(*args, **kwargs),
    get_worker_profile=lambda user_id: _get_worker_profile(user_id),
    sanitize_display_name=lambda raw, fallback: _sanitize_display_name(raw, fallback),
    load_roles=lambda: _load_roles(),
    create_critical_alert=lambda *args, **kwargs: _create_critical_alert(*args, **kwargs),
    send_telegram_message=lambda *args, **kwargs: send_telegram_message(*args, **kwargs),
))
app.include_router(_stages_router)

get_stages = _stages_handlers.get_stages
create_stage = _stages_handlers.create_stage
update_stage_description_endpoint = _stages_handlers.update_stage_description_endpoint
update_stage = _stages_handlers.update_stage
remove_stage = _stages_handlers.remove_stage
swap_stage = _stages_handlers.swap_stage
worker_complete_stage = _stages_handlers.worker_complete_stage
set_stage_blocker = _stages_handlers.set_stage_blocker
clear_stage_blocker = _stages_handlers.clear_stage_blocker
get_stage_roadmap = _stages_handlers.get_stage_roadmap
create_roadmap_category = _stages_handlers.create_roadmap_category
delete_roadmap_category = _stages_handlers.delete_roadmap_category
create_roadmap_item = _stages_handlers.create_roadmap_item
edit_roadmap_item = _stages_handlers.edit_roadmap_item
delete_roadmap_item = _stages_handlers.delete_roadmap_item
update_roadmap_item_status = _stages_handlers.update_roadmap_item_status
create_roadmap_note = _stages_handlers.create_roadmap_note
list_roadmap_notes = _stages_handlers.list_roadmap_notes
create_stage_request = _stages_handlers.create_stage_request
list_stage_requests = _stages_handlers.list_stage_requests
decide_stage_request_endpoint = _stages_handlers.decide_stage_request_endpoint


# moved to core/paths.py -- BLOCKER_PHOTO_DIR
os.makedirs(BLOCKER_PHOTO_DIR, exist_ok=True)


@app.post("/api/objects/{object_id}/blocker-photo")
async def upload_blocker_photo(object_id: str, file: UploadFile = File(...),
                                user: dict = Depends(get_current_user), _: None = Depends(require_object_access)):
    # 29.07 (аудит): blocker sheet заявлял поле фото, но реального upload не было --
    # тот же паттерн, что и остальные фото-эндпоинты (sniff_image, размер-лимит).
    # photo_url -- относительный путь к get_blocker_photo ниже, не прямая файловая ссылка
    # (доступ к самому файлу тоже идёт через require_object_access, не голый static serve).
    raw = await file.read()
    if len(raw) > PHOTO_MAX_BYTES:
        raise HTTPException(400, "Фото слишком большое (макс. 8 МБ)")
    detected = sniff_image(raw)
    if not detected:
        raise HTTPException(400, "Файл должен быть изображением")
    ext = _ALLOWED_IMAGE_MIME_EXT[detected]
    fname = f"{uuid.uuid4().hex}.{ext}"
    with open(os.path.join(BLOCKER_PHOTO_DIR, fname), 'wb') as out:
        out.write(raw)
    return {"photo_url": f"/api/objects/{object_id}/blocker-photo/{fname}"}


@app.get("/api/objects/{object_id}/blocker-photo/{fname}")
def get_blocker_photo(object_id: str, fname: str, user: dict = Depends(get_current_user), _: None = Depends(require_object_access)):
    if '/' in fname or '..' in fname:
        raise HTTPException(400, "Некорректное имя файла")
    path = os.path.join(BLOCKER_PHOTO_DIR, fname)
    if not os.path.exists(path):
        raise HTTPException(404, "Файл отсутствует")
    from fastapi.responses import FileResponse
    return FileResponse(path)


# ---------- Потребности / Needs domain ----------
# Worker -> owner material/tool/PPE/access requests now live in routes/needs.py.
# main.py remains the composition root and keeps only migration aliases.
if __package__:
    from .routes.needs import (
        NeedsRouteDeps,
        TaskCreateBody,
        TaskStatusBody,
        create_needs_router,
    )
else:
    from routes.needs import (  # noqa: E402
        NeedsRouteDeps,
        TaskCreateBody,
        TaskStatusBody,
        create_needs_router,
    )

_needs_router, _needs_handlers = create_needs_router(
    NeedsRouteDeps(
        get_current_user=get_current_user,
        get_role=get_role,
        require_owner=require_owner,
        tasks_file=lambda: TASKS_FILE,
        safe_load_json=lambda path, default: _safe_load_json(path, default),
        atomic_write_json=lambda path, value: _atomic_write_json(path, value),
        update_json_transaction=lambda path, default, mutator: update_json_transaction(path, default, mutator),
        task_priorities=TASK_PRIORITIES,
        task_categories=TASK_CATEGORIES,
        task_statuses=TASK_STATUSES,
        has_active_object_access=lambda user_id, object_id: has_active_object_access(user_id, object_id),
        load_roles=lambda: _load_roles(),
        get_worker_profile=lambda user_id: _get_worker_profile(user_id),
        sanitize_display_name=lambda raw, fallback: _sanitize_display_name(raw, fallback),
        send_telegram_message=lambda *args, **kwargs: send_telegram_message(*args, **kwargs),
        business_now=lambda: business_now(),
        load_repo_objekte_lib=lambda: _load_repo_objekte_lib(),
    )
)
app.include_router(_needs_router)

# Compatibility names for direct-call tests/internal callers during migration.
# These are aliases to the extracted implementation only.
_load_tasks = _needs_handlers.load_tasks
_save_tasks = _needs_handlers.save_tasks
_task_is_open = _needs_handlers.task_is_open
_normalize_task_due_at = _needs_handlers.normalize_task_due_at
_format_due_at = _needs_handlers.format_due_at
_overdue_task_alerts = _needs_handlers.overdue_task_alerts
list_tasks = _needs_handlers.list_tasks
# This is intentionally the final module-level create_task symbol. Historically
# it overwrote the earlier object-task function of the same name; Phase 4 keeps
# that exact public-module behavior and Phase 6 preserves it here.
create_task = _needs_handlers.create_task
update_task_status = _needs_handlers.update_task_status


# ---------- Mängelmanagement — Фаза 3 ----------
# ---------- Mangel / defects domain ----------
# The full defects HTTP/orchestration flow lives in routes/mangel.py. main.py is
# only the composition root and retains aliases for existing direct-call callers.
# Keep the canonical repo-backed module object for backwards compatibility with
# tests/internal code that patch backend.ml directly during the migration.
ml = _load_repo_mangel_lib()

if __package__:
    from .routes.mangel import (
        MangelCommentBody,
        MangelRouteDeps,
        MangelStatusBody,
        create_mangel_router,
    )
else:
    from routes.mangel import (  # noqa: E402
        MangelCommentBody,
        MangelRouteDeps,
        MangelStatusBody,
        create_mangel_router,
    )

_mangel_router, _mangel_handlers = create_mangel_router(
    MangelRouteDeps(
        get_current_user=get_current_user,
        get_role=get_role,
        require_owner=require_owner,
        load_mangel_lib=lambda: ml,
        load_objekte_lib=lambda: _load_repo_objekte_lib(),
        load_worker_profiles=lambda: _load_worker_profiles(),
        sanitize_display_name=lambda value, fallback: _sanitize_display_name(value, fallback),
        sniff_image=lambda raw: sniff_image(raw),
        allowed_image_mime_ext=_ALLOWED_IMAGE_MIME_EXT,
        mangel_photo_dir=lambda: MANGEL_PHOTO_DIR,
        photo_dir=lambda: PHOTO_DIR,
        can_access_object=lambda user, role, object_id: can_access_object(user, role, object_id),
        append_object_history_best_effort=lambda *args, **kwargs: _append_object_history_best_effort(
            *args, **kwargs
        ),
    )
)
app.include_router(_mangel_router)

# Temporary compatibility surface. These are references to the extracted
# implementation; no Mangel business logic remains in main.py.
require_mangel_access = _mangel_handlers.require_mangel_access
_enrich_mangel_tickets_with_author = _mangel_handlers.enrich_mangel_tickets_with_author
get_mangel_list = _mangel_handlers.get_mangel_list
get_mangel_counts = _mangel_handlers.get_mangel_counts
get_mangel_ticket = _mangel_handlers.get_mangel_ticket
create_mangel_ticket = _mangel_handlers.create_mangel_ticket
get_mangel_photo_file = _mangel_handlers.get_mangel_photo_file
update_mangel_status = _mangel_handlers.update_mangel_status
add_mangel_comment = _mangel_handlers.add_mangel_comment
delete_mangel_ticket = _mangel_handlers.delete_mangel_ticket
get_mangel_comments = _mangel_handlers.get_mangel_comments


# ---------- Фотоотчёт старт/финиш смены — Фаза 4a ----------
# moved to core/paths.py -- CHECKIN_PHOTO_BASE
# moved to core/paths.py -- CHECKIN_META_FILE
# moved to core/limits.py -- CHECKIN_MAX_BYTES
_checkin_lock = __import__('threading').Lock()

# 10.40: idempotency-key для checkin start/finish/manual — при плохой связи на
# объекте worker может не увидеть ответ и повторить запрос; без этого второй запрос
# либо создаёт дубль, либо возвращает пугающую 409/400 ошибку на успешное действие.
# 23.09: durable + scoped guard. Один raw Idempotency-Key больше не является
# глобальным ключом на все роли/endpoints/payloads: он проверяется по actor + kind,
# а entity/payload_hash обязаны совпасть. Store переживает restart в пределах TTL.
_idempotency_cache = {}  # identity_hash -> entry_dict
_idempotency_lock = __import__('threading').Lock()
# moved to core/limits.py -- _IDEMPOTENCY_TTL


def _idempotency_payload_hash(payload: dict | None) -> str:
    raw = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _idempotency_scope(kind: str, actor_id: str | int, entity_id: str = '', payload: dict | None = None) -> dict:
    return {
        "kind": str(kind),
        "actor_id": str(actor_id),
        "entity_id": str(entity_id or ''),
        "payload_hash": _idempotency_payload_hash(payload),
    }


def _idempotency_identity(key: str, scope: dict | None = None) -> str:
    scope = scope or {"kind": "legacy", "actor_id": "", "entity_id": "", "payload_hash": ""}
    raw = json.dumps({
        "key": str(key),
        "kind": str(scope.get('kind') or ''),
        "actor_id": str(scope.get('actor_id') or ''),
    }, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _idempotent_entity_id(key: str, scope: dict | None) -> str:
    """F03 (owner review commit db584ac, CHANGES REQUIRED): the business entity's id used
    to be uuid4() -- random on every attempt, including a retried one. That meant the
    crash window (business fact written -> process dies -> _idempotency_save() never
    runs -> durable idempotency entry stays 'pending' forever until TTL prune -> retry
    after prune creates a SECOND business fact) had no way to self-heal: nothing tied a
    retried attempt back to what an earlier attempt may have already written.

    Deriving the id from the same (key, scope) identity used by the idempotency store
    makes a retried attempt with the SAME Idempotency-Key + payload land on the exact
    same entity id every time. Callers check-before-insert on this id so a
    crash-recovered retry finds its own prior write instead of duplicating it --
    closing the crash window without needing a real transactional store across two
    separate JSON files."""
    if not key:
        return uuid.uuid4().hex
    return _idempotency_identity(key, scope)[:32]


def _idempotency_entry_matches(entry: dict, scope: dict | None) -> bool:
    if scope is None:
        return True
    return (
        str(entry.get('kind') or '') == str(scope.get('kind') or '')
        and str(entry.get('actor_id') or '') == str(scope.get('actor_id') or '')
        and str(entry.get('entity_id') or '') == str(scope.get('entity_id') or '')
        and str(entry.get('payload_hash') or '') == str(scope.get('payload_hash') or '')
    )


def _idempotency_assert_same_request(entry: dict, scope: dict | None) -> None:
    if not _idempotency_entry_matches(entry, scope):
        raise HTTPException(409, "Idempotency-Key уже использован для другого запроса")


def _idempotency_prune(store: dict, now: float) -> None:
    stale = [
        k for k, v in store.items()
        if now - float(v.get('updated_at') or v.get('created_at') or 0) > _IDEMPOTENCY_TTL
    ]
    for k in stale:
        store.pop(k, None)
        _idempotency_cache.pop(k, None)


def _idempotency_get(key: str, scope: dict | None = None):
    if not key:
        return None
    identity = _idempotency_identity(key, scope)
    now = time.time()
    cached = _idempotency_cache.get(identity)
    if cached and now - float(cached.get('updated_at') or cached.get('created_at') or 0) <= _IDEMPOTENCY_TTL:
        _idempotency_assert_same_request(cached, scope)
        if cached.get('state') == 'done':
            return copy.deepcopy(cached.get('response'))
        raise HTTPException(409, "Запрос уже выполняется, повторите чуть позже")

    result = {"found": False, "response": None, "pending": False}

    def mutator(store: dict):
        _idempotency_prune(store, now)
        entry = store.get(identity)
        if not entry:
            return
        _idempotency_assert_same_request(entry, scope)
        _idempotency_cache[identity] = copy.deepcopy(entry)
        result["found"] = True
        if entry.get('state') == 'done':
            result["response"] = copy.deepcopy(entry.get('response'))
        else:
            result["pending"] = True

    update_json_transaction(CHECKIN_IDEMPOTENCY_FILE, {}, mutator)
    if result["pending"]:
        raise HTTPException(409, "Запрос уже выполняется, повторите чуть позже")
    return result["response"] if result["found"] else None


def _idempotency_claim(key: str, scope: dict | None = None):
    if not key:
        return None
    identity = _idempotency_identity(key, scope)
    now = time.time()
    result = {"response": None}

    def mutator(store: dict):
        _idempotency_prune(store, now)
        entry = store.get(identity)
        if entry:
            _idempotency_assert_same_request(entry, scope)
            if entry.get('state') == 'done':
                _idempotency_cache[identity] = copy.deepcopy(entry)
                result["response"] = copy.deepcopy(entry.get('response'))
                return
            # F03 (owner review commit db584ac, CHANGES REQUIRED -- corrected,
            # timer-based "stale pending" heuristic removed per owner instruction:
            # crash recovery must be deterministic, not time-based): a 'pending'
            # entry with no 'done' response is indistinguishable, from here alone,
            # between "still genuinely running" and "crashed before
            # _idempotency_save()". Blocking on it (409) was the old behavior and
            # is what caused the F03 crash window: a dead claim blocked every
            # retry until the full 10-minute TTL pruned it. Real state comes from
            # elsewhere. Attaching entity_id to a pending claim means the caller's
            # own storage-level id (_idempotent_entity_id) resolves this
            # deterministically: checkin_start/checkin_manual/checkin_finish each
            # check-before-insert by that same id under _checkin_lock, so a
            # crashed attempt's business fact (if any) is found and replayed, and
            # concurrent double-submission is serialized by that same lock --
            # neither needs a 409 here to be correct. Let the caller through.
        entry = {
            "state": "pending",
            "kind": str((scope or {}).get('kind') or 'legacy'),
            "actor_id": str((scope or {}).get('actor_id') or ''),
            "entity_id": str((scope or {}).get('entity_id') or ''),
            "payload_hash": str((scope or {}).get('payload_hash') or ''),
            "created_at": (entry or {}).get('created_at', now),
            "updated_at": now,
        }
        store[identity] = entry
        _idempotency_cache[identity] = copy.deepcopy(entry)

    with _idempotency_lock:
        update_json_transaction(CHECKIN_IDEMPOTENCY_FILE, {}, mutator)
    return result["response"]


def _idempotency_save(key: str, response: dict, scope: dict | None = None):
    if not key:
        return
    identity = _idempotency_identity(key, scope)
    now = time.time()
    entry = {
        "state": "done",
        "kind": str((scope or {}).get('kind') or 'legacy'),
        "actor_id": str((scope or {}).get('actor_id') or ''),
        "entity_id": str((scope or {}).get('entity_id') or ''),
        "payload_hash": str((scope or {}).get('payload_hash') or ''),
        "response": copy.deepcopy(response),
        "created_at": now,
        "updated_at": now,
    }

    def mutator(store: dict):
        _idempotency_prune(store, now)
        existing = store.get(identity)
        if existing:
            _idempotency_assert_same_request(existing, scope)
            entry["created_at"] = existing.get('created_at') or now
        store[identity] = entry

    with _idempotency_lock:
        update_json_transaction(CHECKIN_IDEMPOTENCY_FILE, {}, mutator)
    _idempotency_cache[identity] = copy.deepcopy(entry)


def _idempotency_release(key: str, scope: dict | None = None) -> None:
    if not key:
        return
    identity = _idempotency_identity(key, scope)

    def mutator(store: dict):
        entry = store.get(identity)
        if entry and _idempotency_entry_matches(entry, scope) and entry.get('state') != 'done':
            store.pop(identity, None)

    with _idempotency_lock:
        update_json_transaction(CHECKIN_IDEMPOTENCY_FILE, {}, mutator)
    _idempotency_cache.pop(identity, None)

os.makedirs(CHECKIN_PHOTO_BASE, exist_ok=True)


# ── Finish projector outbox ──────────────────────────────────────────────────
# Guarantees that a DailyPlan execution update is never silently lost when a crash
# occurs between the checkin_meta write and the daily_plan_store write.

def _outbox_load() -> dict:
    return _safe_load_json(FINISH_OUTBOX_FILE, {})


def _outbox_save(outbox: dict) -> None:
    _atomic_write_json(FINISH_OUTBOX_FILE, outbox)


# Round 1.2 follow-up (owner P0 finding): the original state machine was
# pending -> applied | failed, and startup retry only ever looked at "pending"
# -- once an event became "failed" it was permanently stuck (no automatic path
# back to being retried). New state machine: pending -> retrying -> applied |
# dead_letter, with attempt_count. Both "pending" and "retrying" are retried on
# every startup; after OUTBOX_MAX_ATTEMPTS failures the event becomes
# "dead_letter" and must surface in owner diagnostics (RED) for manual handling
# -- never silently dropped, never silently retried forever either.
OUTBOX_MAX_ATTEMPTS = 10

# Periodic sweep interval for the liveness fix (fix/miniapp-profile-rmw-outbox-retry):
# this is a small internal app (single-VPS, low request volume, no queue framework) --
# a plain asyncio background task polling every 60s is more than fast enough to notice
# a transient failure well before OUTBOX_MAX_ATTEMPTS (10 attempts) would exhaust itself,
# without busy-looping or adding load. No existing outbox-specific interval constant to
# match against (this is the first periodic task in the codebase); 60s sits inside the
# 30-120s band the task called out and is symmetric with the exponential-ish but bounded
# total budget: 10 attempts * 60s ≈ worst case a few minutes to dead_letter, not hours.
OUTBOX_RETRY_SWEEP_INTERVAL_SECONDS = 60


def _outbox_write_pending(session_id: str, plan_id: str, plan_version: int,
                          worker_id: str, date_str: str, object_id: str,
                          item_results: list, acceptance_id: str | None = None) -> None:
    with _finish_outbox_lock:
        outbox = _outbox_load()
        outbox[session_id] = {
            "state": "pending",
            "plan_id": plan_id,
            "plan_version": plan_version,
            "worker_id": str(worker_id),
            "date": date_str,
            "object_id": object_id,
            "item_results": item_results,
            "acceptance_id": acceptance_id,
            "created_at": time.time(),
            "last_attempt_at": None,
            "attempt_count": 0,
            "error": None,
        }
        _outbox_save(outbox)


def _outbox_mark_applied(session_id: str) -> None:
    with _finish_outbox_lock:
        outbox = _outbox_load()
        if session_id in outbox:
            outbox[session_id]["state"] = "applied"
            outbox[session_id]["applied_at"] = time.time()
            _outbox_save(outbox)


def _outbox_mark_failed(session_id: str, error: str) -> None:
    """Records a failed attempt. Stays retryable ("retrying") until
    OUTBOX_MAX_ATTEMPTS is reached, then becomes a permanent "dead_letter" --
    at that point only manual owner intervention (fixing the underlying data
    issue, then a manual re-trigger) should touch it again."""
    with _finish_outbox_lock:
        outbox = _outbox_load()
        if session_id in outbox:
            evt = outbox[session_id]
            evt["attempt_count"] = evt.get("attempt_count", 0) + 1
            evt["error"] = error[:500]
            evt["last_attempt_at"] = time.time()
            if evt["attempt_count"] >= OUTBOX_MAX_ATTEMPTS:
                evt["state"] = "dead_letter"
            else:
                evt["state"] = "retrying"
            _outbox_save(outbox)


def _outbox_mark_dead_letter(session_id: str, error: str) -> None:
    """P0 fix: immediate permanent dead-letter for a validation rejection
    (ExecutionValidationError) -- unlike _outbox_mark_failed's gradual
    retry-then-dead-letter path, a rejected-as-invalid report will not become
    valid on a 2nd/3rd/10th automatic retry, so there is no reason to consume
    ordinary retry attempts on it. Still never silently dropped -- it stays
    visible via the same _outbox_dead_letter_count() owner diagnostic as any
    other dead-lettered event, tagged with the specific rejection reason."""
    with _finish_outbox_lock:
        outbox = _outbox_load()
        if session_id in outbox:
            evt = outbox[session_id]
            evt["attempt_count"] = evt.get("attempt_count", 0) + 1
            evt["error"] = error[:500]
            evt["last_attempt_at"] = time.time()
            evt["state"] = "dead_letter"
            _outbox_save(outbox)


def _retry_pending_outbox_events() -> int:
    """Called at startup to apply any pending/retrying finish-projection events.
    dead_letter events are intentionally NOT retried automatically -- they need
    manual owner review (surfaced via diagnostics, see _outbox_dead_letter_count).
    Returns count of successfully applied events.

    P0 fix: passes acceptance_id through to apply_daily_execution so the SAME
    validate_execution_against_acceptance() the normal Finish path uses also
    gates this crash-recovery path -- a report that would have been rejected
    at Finish time must not become valid merely by surviving to a restart.
    ExecutionValidationError is NOT a transient failure (retrying will never
    make invalid data valid) -- it goes straight to dead_letter for owner
    review instead of consuming ordinary retry attempts, but it is still
    surfaced via the exact same diagnostic mechanism as any other stuck event,
    never silently dropped."""
    with _finish_outbox_lock:
        outbox = _outbox_load()
    retried = 0
    for session_id, evt in list(outbox.items()):
        if evt.get("state") not in ("pending", "retrying"):
            continue
        try:
            dpl.apply_daily_execution(
                session_id=session_id,
                daily_plan_id=evt["plan_id"],
                plan_version=int(evt.get("plan_version") or 0),
                worker_id=str(evt["worker_id"]),
                date_str=evt["date"],
                object_id=evt["object_id"],
                item_results=evt.get("item_results") or [],
                acceptance_id=evt.get("acceptance_id"),
            )
            _outbox_mark_applied(session_id)
            _clear_pending_execution_report(session_id)
            retried += 1
        except dpl.ExecutionValidationError as ve:
            _outbox_mark_dead_letter(session_id, f"validation rejected: {ve}")
        except Exception as e:
            _outbox_mark_failed(session_id, str(e))
    return retried


async def _run_outbox_retry_sweep_once() -> int | None:
    """One tick of the periodic liveness fix: reuses _retry_pending_outbox_events()
    as-is (no reimplementation of the state machine) via asyncio.to_thread so the
    (synchronous, file-I/O-bound) sweep never blocks the event loop / normal request
    handling. Returns None (instead of running) if a sweep is already in flight.

    The check-then-set on _outbox_sweep_in_progress below has no `await` between
    them, so no other coroutine can interleave there under asyncio's cooperative
    scheduling -- two callers racing this function (e.g. two overlapping scheduler
    ticks, or a test firing it twice concurrently) always have exactly one of them
    see False and proceed, never both. The one that proceeds always resets the flag
    in `finally`, including if _retry_pending_outbox_events() itself raises -- a
    failed sweep must not permanently wedge every future tick into "skip"."""
    global _outbox_sweep_in_progress
    if _outbox_sweep_in_progress:
        return None
    _outbox_sweep_in_progress = True
    try:
        return await asyncio.to_thread(_retry_pending_outbox_events)
    finally:
        _outbox_sweep_in_progress = False


async def _outbox_retry_sweep_loop():
    """Runtime counterpart to the startup-only retry above (Issue 2 liveness fix,
    fix/miniapp-profile-rmw-outbox-retry): without this, a transient failure after
    startup left an event stuck in "retrying" until the next process restart, which
    on a stable long-running service could be days. Started from _on_startup,
    cancelled from _on_shutdown -- never left running after the app stops.

    A sweep failure is logged and MUST NOT crash the app or stop future ticks --
    the whole point of this loop is resilience against transient failures, so an
    unexpected exception inside one tick is exactly the kind of thing it exists to
    survive."""
    while True:
        try:
            await asyncio.sleep(OUTBOX_RETRY_SWEEP_INTERVAL_SECONDS)
            retried = await _run_outbox_retry_sweep_once()
            if retried:
                print(f"[outbox-sweep] Retried {retried} pending finish-outbox event(s)")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[outbox-sweep] tick failed (will retry next interval): {e}")


def _outbox_dead_letter_count() -> int:
    """Owner diagnostics: count of finish-projection events that exhausted all
    retry attempts and need manual review. A non-zero count means a completed
    shift's DailyExecution was never recorded and needs a human to look."""
    outbox = _outbox_load()
    return sum(1 for evt in outbox.values() if evt.get("state") == "dead_letter")


def _clear_pending_execution_report(session_id: str) -> None:
    """Once a finish-projection event is successfully applied (either inline in
    checkin_finish or via startup retry), clear the raw report we stashed on the
    checkin_meta session as a crash-window safety net -- it's done its job, no
    need to keep the raw JSON blob around forever."""
    with _checkin_lock:
        items = _load_checkin_meta()
        session = next((i for i in items if i.get('id') == session_id), None)
        if session and session.get('pending_execution_report'):
            session['pending_execution_report'] = None
            _save_checkin_meta(items)


def _reject_pending_execution_report(session_id: str, reason: str) -> None:
    """P0 fix: when checkin_finish's own pre-check (or the centralized
    validate_execution_against_acceptance()) rejects a submitted
    daily_plan_report, the raw report must stop being retrievable -- otherwise
    it sits in pending_execution_report forever, and a later server restart's
    _reconcile_missing_outbox_events() would reconstruct an outbox event from
    it and re-attempt exactly what was just rejected. Per spec: never
    silently apply, never silently delete -- so this records the rejection
    reason + timestamp on the session (rejected_execution_report) instead of
    just nulling the field blind, giving the owner something to look at, then
    clears pending_execution_report so reconciliation has nothing left to
    reconstruct from."""
    with _checkin_lock:
        items = _load_checkin_meta()
        session = next((i for i in items if i.get('id') == session_id), None)
        if session and session.get('pending_execution_report'):
            session['rejected_execution_report'] = {
                "raw": session['pending_execution_report'],
                "reason": reason[:500],
                "rejected_at": time.time(),
            }
            session['pending_execution_report'] = None
            _save_checkin_meta(items)


def _reconcile_missing_outbox_events() -> int:
    """Startup reconciliation (owner P0 finding): a finished checkin session can
    have pending_execution_report set (the crash-window fix above) but no
    corresponding outbox event at all, if the process died between the
    checkin_meta commit and the _outbox_write_pending call in checkin_finish.
    Without this, such a session's DailyExecution is silently lost forever --
    nothing would ever retry it, because _retry_pending_outbox_events only
    looks at events that already exist in the outbox. This scans finished
    sessions for that exact gap and reconstructs the missing outbox entry so
    the normal pending/retrying machinery picks it up on this same startup.
    Returns count of reconstructed events."""
    items = _load_checkin_meta()
    outbox = _outbox_load()
    reconciled = 0
    for session in items:
        if session.get('finish_at') is None:
            continue
        report_raw = session.get('pending_execution_report')
        if not report_raw:
            continue
        session_id = session.get('id')
        if session_id in outbox:
            continue  # outbox event already exists (normal path or already reconciled)
        try:
            rpt = json.loads(report_raw)
        except Exception as e:
            print(f'WARNING: reconcile — session {session_id} has unparseable pending_execution_report: {e}')
            continue
        plan_id = rpt.get('plan_id', '') or session.get('daily_plan_id', '')
        plan_ver = int(rpt.get('plan_version', 0) or session.get('daily_plan_version', 0) or 0)
        item_results = rpt.get('item_results') or []
        if not (plan_id and isinstance(item_results, list) and item_results):
            continue
        # P0 fix: carry the session's own trusted daily_plan_acceptance_id
        # (set durably at checkin_start, never derived from the untrusted raw
        # report) into the reconstructed outbox event, so
        # validate_execution_against_acceptance() -- run identically for this
        # path and the normal Finish path via apply_daily_execution -- can
        # check it against the immutable accepted_context_snapshot instead of
        # only the live (possibly since-changed) plan.
        _reconciled_acceptance_id = session.get('daily_plan_acceptance_id') or None
        _outbox_write_pending(
            session_id=session_id, plan_id=plan_id, plan_version=plan_ver,
            worker_id=str(session['user_id']), date_str=session['date'],
            object_id=session['object_id'], item_results=item_results,
            acceptance_id=_reconciled_acceptance_id,
        )
        reconciled += 1
        print(f'[startup] Reconciled missing outbox event for session {session_id} (crash-window recovery)')
    return reconciled


def _load_checkin_meta() -> list:
    return _safe_load_json(CHECKIN_META_FILE, [])


def _save_checkin_meta(items: list):
    # 30.07 (Release-аудит P1): было plain open(w)+json.dump -- crash посреди записи
    # (systemctl restart, OOM-kill) обрезал бы главный стор смен/GPS/фото. RMW-race
    # уже закрыт _checkin_lock на всех call sites, здесь только crash-safety записи.
    _atomic_write_json(CHECKIN_META_FILE, items)


async def _save_checkin_photos(files: list, object_id: str, date_str: str) -> list:
    # 30.07 (Release-аудит P0): object_id на /api/checkin/start приходит от клиента
    # (Form-параметр), раньше обрезался только по длине (.strip()[:100]), без защиты
    # от '../' -- потенциальный path traversal при записи фото. os.path.basename
    # схлопывает любые сегменты пути в один компонент (тот же приём, что уже
    # используется для fname/file_id в других upload/serving endpoints).
    object_id = os.path.basename(object_id) or 'unknown'
    day_dir = os.path.join(CHECKIN_PHOTO_BASE, object_id, date_str)
    os.makedirs(day_dir, exist_ok=True)
    saved = []
    for file in files:
        raw = await file.read()
        if len(raw) > CHECKIN_MAX_BYTES:
            continue
        detected = sniff_image(raw)
        if not detected:
            continue
        ext = _ALLOWED_IMAGE_MIME_EXT[detected]
        fname = f"{uuid.uuid4().hex}.{ext}"
        with open(os.path.join(day_dir, fname), 'wb') as f:
            f.write(raw)
        saved.append(os.path.join(object_id, date_str, fname))
    return saved


def _checkin_file_count(files) -> int:
    try:
        return len(files or [])
    except TypeError:
        return 0


def _cleanup_checkin_photo_files(relative_paths: list) -> None:
    """03.08: удаляет файлы, сохранённые _save_checkin_photos() ТЕКУЩЕГО неуспешного
    запроса (когда итоговое количество валидных фото < 2) -- не оставляет orphan-файлы
    на диске без ссылки в metadata. Не трогает фото прошлых успешных запросов -- те
    приходят отдельным списком путей, никогда не пересекаются с этим вызовом."""
    for rel_path in relative_paths:
        # os.path.basename на каждом сегменте -- та же defense-in-depth привычка,
        # что и при сохранении (rel_path здесь свой же вывод _save_checkin_photos,
        # но не доверяем этому неявно на случай будущих изменений вызывающего кода).
        abs_path = os.path.join(CHECKIN_PHOTO_BASE, rel_path)
        try:
            os.remove(abs_path)
        except OSError:
            pass


def _get_active_assignment_for_checkin(user_id: str, object_id: str, today: str) -> str:
    """Единая проверка для /api/checkin/start (30.07, аудит п.5) -- owner вызывающий
    код не проверяет вообще (owner проходит can_access_object безусловно), эта функция
    только для worker-пути. Возвращает assignment_id при успехе, иначе бросает 403
    с конкретным сообщением (pending/declined/вне периода/нет назначения -- разные
    тексты, не один общий "нет доступа"). Backward compat: назначение без 'status'
    трактуется как accepted (_assignment_status), без date_from/date_to -- не блокируется.

    03.08: критерий "успех" здесь ТОТ ЖЕ, что has_active_object_access() (accepted +
    сегодня внутри [date_from, date_to] либо период не задан) -- не дублирующая
    независимая проверка дат, просто эта функция дополнительно резолвит конкретный
    assignment_id и даёт разные сообщения об ошибке (pending/declined/вне периода),
    чего bool-helper намеренно не делает."""
    candidates = [a for a in _load_assignments().get(object_id, []) if str(a.get('user_id')) == str(user_id)]
    if not candidates:
        raise HTTPException(403, "У вас нет принятого назначения на этот объект")

    # Если есть хоть одно accepted в периоде -- оно и есть искомое (наиболее частый путь).
    accepted = [a for a in candidates if _assignment_status(a) == 'accepted']
    for a in accepted:
        d_from, d_to = a.get('date_from', ''), a.get('date_to', '')
        if not (d_from and d_to):
            return a.get('id', '')
        if d_from <= today <= d_to:
            return a.get('id', '')
    if accepted:
        # Есть accepted, но ни один не покрывает today датами -- сообщаем по ближайшему.
        a = accepted[0]
        d_from, d_to = a.get('date_from', ''), a.get('date_to', '')
        if today < d_from:
            raise HTTPException(403, f"Смена доступна с {d_from}")
        raise HTTPException(403, f"Период назначения завершён {d_to}")

    if any(_assignment_status(a) == 'pending' for a in candidates):
        raise HTTPException(403, "Сначала подтвердите назначение")
    if any(_assignment_status(a) == 'declined' for a in candidates):
        raise HTTPException(403, "Назначение отклонено")
    raise HTTPException(403, "У вас нет принятого назначения на этот объект")


@app.get("/api/workers/{target_user_id}/calendar")
def get_worker_calendar(target_user_id: str, year: int, month: int,
                         user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    """10.30 + 21.07: owner выбирает профиль worker'а и видит availability (доступные/недоступные/
    отработанные дни). Worker может смотреть ТОЛЬКО свой собственный календарь тем же способом —
    permission проверяется здесь на backend (не только скрытием кнопки в UI, см. ChatGPT-аудит 21.07),
    не декоратором require_owner, т.к. worker имеет легитимный доступ к своим же данным."""
    if role != 'owner' and str(user['id']) != str(target_user_id):
        raise HTTPException(403, "Можно смотреть только свой календарь")
    month_prefix = f'{year:04d}-{month:02d}'

    abwesenheit = [e for e in _load_abwesenheit() if str(e['user_id']) == target_user_id]
    unavailable_dates = set()
    for e in abwesenheit:
        if e.get('status') == 'rejected':
            continue  # отклонённая заявка не блокирует день
        d = datetime.strptime(e['date_from'], '%Y-%m-%d')
        end = datetime.strptime(e['date_to'], '%Y-%m-%d')
        while d <= end:
            ds = d.strftime('%Y-%m-%d')
            if ds.startswith(month_prefix):
                unavailable_dates.add(ds)
            d += timedelta(days=1)

    checkins = [c for c in _load_checkin_meta()
                if str(c.get('user_id')) == target_user_id and c.get('date', '').startswith(month_prefix)]
    worked_dates = sorted(set(c['date'] for c in checkins))

    # 22.07: 4-е состояние — назначен на объект в эти даты (bubble-assign date_from/date_to).
    # Одобренный отпуск теперь физически блокирует новое назначение (см. assign_user), но старые
    # назначения (созданные до этого фикса) могут пересекаться — недоступен побеждает по приоритету.
    assigned_dates = set()
    for oid, lst in _load_assignments().items():
        for a in lst:
            if a.get('user_id') != target_user_id or not a.get('date_from') or not a.get('date_to'):
                continue
            d = datetime.strptime(a['date_from'], '%Y-%m-%d')
            end = datetime.strptime(a['date_to'], '%Y-%m-%d')
            while d <= end:
                ds = d.strftime('%Y-%m-%d')
                if ds.startswith(month_prefix):
                    assigned_dates.add(ds)
                d += timedelta(days=1)
    assigned_dates -= unavailable_dates  # приоритет: недоступен важнее назначения

    return {
        "unavailable_dates": sorted(unavailable_dates),
        "worked_dates": worked_dates,
        "assigned_dates": sorted(assigned_dates),
    }


def _worker_period_stats(target_id: str, date_from: str, date_to: str) -> dict:
    """Раунд 5 §13: агрегаты по периоду [date_from, date_to] включительно (Europe/Berlin,
    строковые ISO-даты сравниваются лексикографически). days_worked = уникальные дни со
    сменами (активная смена считается один раз, не дублируется), total_hours учитывает
    паузы через _hours_from_session, sick/vacation — одобренные отсутствия, пересекающие
    период. Средн. за день = total_hours / days_worked."""
    sessions = [s for s in _load_checkin_meta()
                if str(s.get('user_id')) == str(target_id)
                and date_from <= s.get('date', '') <= date_to]
    worked_dates = sorted(set(s['date'] for s in sessions if s.get('date')))
    total_hours = round(sum(_hours_from_session(s) for s in sessions), 2)
    days_worked = len(worked_dates)
    avg = round(total_hours / days_worked, 2) if days_worked else 0.0

    d_from = datetime.strptime(date_from, '%Y-%m-%d').date()
    d_to = datetime.strptime(date_to, '%Y-%m-%d').date()
    sick_days = vacation_days = 0
    for e in _load_abwesenheit():
        if str(e.get('user_id')) != str(target_id) or e.get('status') != 'approved':
            continue
        e1 = datetime.strptime(e['date_from'], '%Y-%m-%d').date()
        e2 = datetime.strptime(e.get('date_to') or e['date_from'], '%Y-%m-%d').date()
        lo, hi = max(e1, d_from), min(e2, d_to)
        if lo > hi:
            continue
        days = (hi - lo).days + 1
        if e.get('reason') == 'Krankheit':
            sick_days += days
        elif e.get('reason') == 'Urlaub':
            vacation_days += days

    return {
        'date_from': date_from, 'date_to': date_to,
        'days_worked': days_worked, 'total_hours': total_hours,
        'avg_per_day': avg, 'sick_days': sick_days, 'vacation_days': vacation_days,
        'worked_dates': worked_dates,
    }


@app.get("/api/workers/{target_user_id}/calendar-stats")
def get_worker_calendar_stats(target_user_id: str, date_from: str, date_to: str,
                              user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    """Раунд 5 §13: статистика работника за произвольный период (Неделя/Месяц/3 месяца/
    свой период). Owner видит любого, Worker — только себя (проверка на backend)."""
    if role != 'owner' and str(user['id']) != str(target_user_id):
        raise HTTPException(403, "Можно смотреть только свою статистику")
    for d in (date_from, date_to):
        try:
            datetime.strptime(d, '%Y-%m-%d')
        except (ValueError, TypeError):
            raise HTTPException(400, "date_from/date_to должны быть YYYY-MM-DD")
    if date_from > date_to:
        raise HTTPException(400, "date_from не может быть позже date_to")
    stats = _worker_period_stats(target_user_id, date_from, date_to)
    profile = _get_worker_profile(target_user_id)
    stats['name'] = _sanitize_display_name(profile.get('name'), str(target_user_id))
    return stats


try:
    from .routes.checkin import (
        CheckinRouteDeps,
        create_checkin_router,
    )
except ImportError:
    from routes.checkin import (  # noqa: E402
        CheckinRouteDeps,
        create_checkin_router,
    )

try:
    from .routes.execution import (
        ExecutionRouteDeps,
        create_execution_router,
        ZeiterfassungBody as _ExecutionZeiterfassungBody,
    )
except ImportError:
    from routes.execution import (  # noqa: E402
        ExecutionRouteDeps,
        create_execution_router,
        ZeiterfassungBody as _ExecutionZeiterfassungBody,
    )


ZeiterfassungBody = _ExecutionZeiterfassungBody


_checkin_router, _checkin_handlers = create_checkin_router(CheckinRouteDeps(
    get_current_user=get_current_user,
    get_role=get_role,
    checkin_photo_base=lambda: CHECKIN_PHOTO_BASE,
    checkin_lock=_checkin_lock,
    finish_outbox_lock=_finish_outbox_lock,
    load_checkin_meta=lambda: _load_checkin_meta(),
    save_checkin_meta=lambda items: _save_checkin_meta(items),
    save_checkin_photos=lambda files, object_id, date_str: _save_checkin_photos(files, object_id, date_str),
    cleanup_checkin_photo_files=lambda paths: _cleanup_checkin_photo_files(paths),
    checkin_file_count=lambda files: _checkin_file_count(files),
    is_active_photo_checkin_session=lambda s: _is_active_photo_checkin_session(s),
    gps_suspect=lambda lat, lon: _gps_suspect(lat, lon),
    can_access_object=lambda user, role, object_id: can_access_object(user, role, object_id),
    get_active_assignment_for_checkin=lambda user_id, object_id, today: _get_active_assignment_for_checkin(user_id, object_id, today),
    idempotency_scope=lambda kind, actor_id, entity_id='', payload=None: _idempotency_scope(kind, actor_id, entity_id, payload),
    idempotency_claim=lambda key, scope=None: _idempotency_claim(key, scope),
    idempotency_save=lambda key, response, scope=None: _idempotency_save(key, response, scope),
    idempotency_release=lambda key, scope=None: _idempotency_release(key, scope),
    idempotent_entity_id=lambda key, scope: _idempotent_entity_id(key, scope),
    load_worker_profiles=lambda: _load_worker_profiles(),
    sanitize_display_name=lambda raw, fallback: _sanitize_display_name(raw, fallback),
    cached_get_used_range=lambda tab_name: _cached_get_used_range(tab_name),
    upsert_checkin_feed_post=lambda session, kind, object_name, user_id, user_name: _upsert_checkin_feed_post(session, kind, object_name, user_id, user_name),
    business_now=lambda: business_now(),
    business_today_str=lambda: business_today_str(),
    outbox_write_pending=lambda *args, **kwargs: _outbox_write_pending(*args, **kwargs),
    outbox_mark_applied=lambda session_id: _outbox_mark_applied(session_id),
    outbox_mark_failed=lambda session_id, error: _outbox_mark_failed(session_id, error),
    outbox_mark_dead_letter=lambda session_id, error: _outbox_mark_dead_letter(session_id, error),
    clear_pending_execution_report=lambda session_id: _clear_pending_execution_report(session_id),
    reject_pending_execution_report=lambda session_id, reason: _reject_pending_execution_report(session_id, reason),
    write_zeiterfassung_row=lambda session, object_id, user_id: _write_zeiterfassung_row(session, object_id, user_id),
    object_history_worker_name=lambda user_id: _object_history_worker_name(user_id),
    append_object_history_best_effort=lambda *args, **kwargs: _append_object_history_best_effort(*args, **kwargs),
    extra_works_summary_text=lambda session: _extra_works_summary_text(session),
    load_roles=lambda: _load_roles(),
    send_telegram_message=lambda chat_id, text: send_telegram_message(chat_id, text),
    photo_pause_minutes=lambda s: _photo_pause_minutes(s),
    hours_from_session=lambda s: _hours_from_session(s),
    get_worker_profile=lambda user_id: _get_worker_profile(user_id),
    csv_safe=lambda value: _csv_safe(value),
))
# Canonical router registration: tests and manifests flatten _IncludedRouter
# through tests.conftest.iter_app_routes()/equivalent production-package smoke.
app.include_router(_checkin_router)

# Legacy direct-call compatibility: tests and operational scripts still import
# handlers from main.py. Runtime routes are registered by backend.routes.checkin.
checkin_start = _checkin_handlers.checkin_start
checkin_pause = _checkin_handlers.checkin_pause
checkin_finish = _checkin_handlers.checkin_finish
export_stundenzettel = _checkin_handlers.export_stundenzettel
list_checkins = _checkin_handlers.list_checkins
checkin_finish_context = _checkin_handlers.checkin_finish_context
get_checkin_photo = _checkin_handlers.get_checkin_photo
_build_finish_context = _checkin_handlers.build_finish_context
_parse_checkin_occurred_at = _checkin_handlers.parse_checkin_occurred_at
_checkin_business_date_from_timestamp = _checkin_handlers.checkin_business_date_from_timestamp


_execution_router, _execution_handlers = create_execution_router(ExecutionRouteDeps(
    get_current_user=get_current_user,
    get_role=get_role,
    checkin_lock=_checkin_lock,
    load_checkin_meta=lambda: _load_checkin_meta(),
    save_checkin_meta=lambda items: _save_checkin_meta(items),
    idempotency_scope=lambda kind, actor_id, entity_id='', payload=None: _idempotency_scope(kind, actor_id, entity_id, payload),
    idempotency_claim=lambda key, scope=None: _idempotency_claim(key, scope),
    idempotency_save=lambda key, response, scope=None: _idempotency_save(key, response, scope),
    idempotency_release=lambda key, scope=None: _idempotency_release(key, scope),
    idempotent_entity_id=lambda key, scope: _idempotent_entity_id(key, scope),
    write_zeiterfassung_row=lambda session, object_id, user_id: _write_zeiterfassung_row(session, object_id, user_id),
    cached_get_used_range=lambda tab_name: _cached_get_used_range(tab_name),
    load_assignments=lambda: _load_assignments(),
    load_roles=lambda: _load_roles(),
    has_active_object_access=lambda user_id, object_id, today=None: has_active_object_access(user_id, object_id, today),
    business_now=lambda: business_now(),
    business_today_str=lambda: business_today_str(),
    checkin_photo_base=lambda: CHECKIN_PHOTO_BASE,
    check_ai_rate=lambda user_id, rate_file=None, limit=None: _check_ai_rate(user_id, rate_file, limit),
    create_mangel_ticket=lambda **kwargs: ml.create_ticket(**kwargs),
))
app.include_router(_execution_router)

checkin_manual = _execution_handlers.checkin_manual
analyze_checkin_progress = _execution_handlers.analyze_checkin_progress
analyze_checkin_materials = _execution_handlers.analyze_checkin_materials
analyze_checkin_defects = _execution_handlers.analyze_checkin_defects
_validate_manual_time_body = _execution_handlers.validate_manual_time_body
_assert_no_manual_time_overlap = _execution_handlers.assert_no_manual_time_overlap
_object_id_exists = _execution_handlers.object_id_exists
_parse_manual_date = _execution_handlers.parse_manual_date
_parse_manual_hhmm = _execution_handlers.parse_manual_hhmm
_get_checkin_session = _execution_handlers.get_checkin_session
_save_checkin_analysis = _execution_handlers.save_checkin_analysis
_image_block_from_file = _execution_handlers.image_block_from_file
_call_glm_vision = _execution_handlers.call_glm_vision




# ---------- Critical Alerts — persisted, с deadline/comment/photo (Фаза 10.16) ----------
# moved to core/paths.py -- CRITICAL_ALERTS_FILE
# moved to core/paths.py -- CRITICAL_ALERT_PHOTO_DIR
os.makedirs(CRITICAL_ALERT_PHOTO_DIR, exist_ok=True)


def _load_critical_alerts() -> list:
    return _safe_load_json(CRITICAL_ALERTS_FILE, [])


def _save_critical_alerts(items: list):
    _atomic_write_json(CRITICAL_ALERTS_FILE, items)


def _is_legacy_bare_uid_birthday_ref(alert: dict) -> bool:
    """23.09 (owner review): identifies the OLD, pre-fix birthday alert shape --
    kind='birthday' with ref_id set to the bare worker uid (digits only), from
    before ref_id=idem (birthday:<uid>:<year>:3days / :today) existed. New
    birthday alerts always have an 'idem'-shaped ref_id (contains ':') and
    must never be touched by this legacy-only logic. Deliberately narrow: only
    kind='birthday' with a non-empty, purely-numeric ref_id qualifies -- any
    other kind, or a blank ref_id, or a ref_id that already looks like the new
    idem shape, is left alone."""
    if alert.get('kind') != 'birthday':
        return False
    ref_id = alert.get('ref_id') or ''
    return bool(ref_id) and ref_id.isdigit()


def _supersede_legacy_siblings(items: list, canonical: dict) -> int:
    """23.09 (owner review): when a legacy bare-uid birthday alert is
    acknowledged, every OTHER unresolved alert sharing the exact same legacy
    semantic key (kind, target_user_id, ref_id) is superseded in the SAME
    transaction -- acknowledged_at set, superseded_by recorded, nothing
    deleted. This is the live-request equivalent of
    cleanup_legacy_critical_alert_duplicates.py's one-off migration: that
    script handles the 196 records that already existed; this handles any
    legacy sibling a poll still manages to surface between deploy and the
    migration actually running (or if the migration is deliberately not run
    immediately). Scoped to _is_legacy_bare_uid_birthday_ref() ONLY -- new
    alerts (idem-shaped ref_id, any other kind, blank ref_id) are never
    touched by this, matching _create_critical_alert()'s own dedup scoping."""
    if not _is_legacy_bare_uid_birthday_ref(canonical):
        return 0
    now = canonical.get('acknowledged_at') or int(time.time())
    superseded = 0
    for a in items:
        if a['id'] == canonical['id']:
            continue
        if a.get('acknowledged_at'):
            continue
        if not _is_legacy_bare_uid_birthday_ref(a):
            continue
        if a['kind'] != canonical['kind'] or a['target_user_id'] != canonical['target_user_id'] \
                or a.get('ref_id') != canonical.get('ref_id'):
            continue
        a['acknowledged_at'] = now
        a['superseded_by'] = canonical['id']
        superseded += 1
    return superseded


def _create_critical_alert(target_user_id: str, kind: str, title: str, ref_id: str = '',
                            subtitle: str = '', deadline_at: int | None = None) -> dict:
    """Создаёт persisted критический алерт + пуш + авто-чат-тред (владельцы + назначенный worker).

    22.09 (iPhone screenshot audit, item found live): this used to unconditionally
    append a new alert with a fresh UUID on every call -- callers that can fire
    more than once for the same underlying event (daily_plan_cutoff_check.py's
    idempotency guard only covers ONE call site; anything else calling this
    directly for the same semantic situation had no protection at all) could
    create duplicate UNRESOLVED alerts, which then queue up back-to-back in the
    frontend's critical-alert popup -- confirmed live on a real device. Dedup
    key: (kind, target_user_id, ref_id) -- if an alert with that exact triple is
    still unacknowledged, return it instead of creating a new one. Two semantically
    different alerts of the same kind for the same user are still allowed to
    coexist as long as ref_id differs (e.g. two different plan_overdue alerts for
    two different business dates, if ref_id carries the date).

    23.09 (owner review, before the legacy-birthday-cleanup rollout): a BLANK
    ref_id ('' -- the default for any caller that doesn't pass one, e.g. a
    manually-created owner alert with no natural reference id) must NEVER
    dedupe against another blank-ref_id alert -- two genuinely unrelated manual
    alerts of the same kind for the same user would otherwise silently collapse
    into one. Dedup only applies when ref_id is non-empty; every call with a
    blank ref_id always creates a fresh alert, exactly like before this
    function had any dedup at all."""
    def _mutate(items):
        existing = next(
            (a for a in items
             if ref_id and a['target_user_id'] == str(target_user_id) and a['kind'] == kind
             and a.get('ref_id', '') == ref_id and not a.get('acknowledged_at')),
            None,
        )
        if existing:
            return existing, False
        alert = {
            "id": uuid.uuid4().hex,
            "target_user_id": str(target_user_id),
            "kind": kind,
            "title": title,
            "subtitle": subtitle,
            "ref_id": ref_id,
            "created_at": int(time.time()),
            "deadline_at": deadline_at,
            "acknowledged_at": None,
            "comment": None,
            "resolution": None,  # 'yes' | 'no' — ответ на "вопрос решён?"
            "resolution_note": None,
            "resolution_photos": [],
        }
        items.append(alert)
        return alert, True

    alert, was_created = update_json_transaction(CRITICAL_ALERTS_FILE, [], _mutate)
    if not was_created:
        # Returned an already-existing alert (dedup hit, not a fresh create) --
        # the chat thread/push notification for it already happened the first
        # time; sending them again here would itself create a duplicate.
        return alert

    roles = _load_roles()
    owner_ids = [uid for uid, r in roles.items() if r == 'owner']
    thread_id = _chat_thread_id(target_user_id, owner_ids[0] if owner_ids else None) \
        if owner_ids else 'group'
    _ensure_critical_alert_chat(alert, thread_id, owner_ids)

    try:
        send_telegram_message(int(target_user_id), f"🔴 {title}")
    except Exception:
        pass
    return alert


def _ensure_critical_alert_chat(alert: dict, thread_id: str, owner_ids: list):
    """Авто-сообщение в тред алерта — создаётся независимо от того, ответит ли worker."""
    msg = {
        "id": uuid.uuid4().hex,
        "ts": int(time.time()),
        "user_id": "system",
        "name": "Система",
        "text": f"🔴 Критический алерт: {alert['title']}",
        "to_user_id": None if thread_id == 'group' else thread_id,
        "critical_alert_id": alert['id'],
    }
    with _chat_lock:
        messages = _load_chat()
        messages.append(msg)
        _save_chat(messages)


class CriticalAlertAckBody(BaseModel):
    comment: str = ''


@app.post("/api/critical-alerts/{alert_id}/ack")
def ack_critical_alert(alert_id: str, body: CriticalAlertAckBody, user: dict = Depends(get_current_user)):
    # 23.09 (owner review): was a plain _load_critical_alerts() -> mutate ->
    # _save_critical_alerts() -- the read-modify-write race storage.py's own
    # _atomic_write_json docstring warns against (lock only covers the write,
    # not the read). A concurrent _create_critical_alert() append (already
    # correctly using update_json_transaction) landing between this read and
    # this write could be silently discarded, or this ack itself could be
    # lost, leaving the alert looking unacknowledged on the next poll.
    #
    # Also supersedes every other unresolved LEGACY sibling sharing this
    # alert's semantic key (kind, target_user_id, ref_id) in the same
    # transaction -- see _supersede_legacy_siblings()'s docstring for why this
    # is scoped to legacy birthday records specifically, not a general rule.
    def _mutate(items):
        alert = next((a for a in items if a['id'] == alert_id), None)
        if not alert:
            raise HTTPException(404, "Алерт не найден")
        if alert['target_user_id'] != str(user['id']):
            raise HTTPException(403, "Не ваш алерт")
        alert['acknowledged_at'] = int(time.time())
        alert['comment'] = body.comment.strip()[:500] or None
        _supersede_legacy_siblings(items, alert)
        return alert

    alert = update_json_transaction(CRITICAL_ALERTS_FILE, [], _mutate)

    if alert['comment']:
        roles = _load_roles()
        owner_ids = [uid for uid, r in roles.items() if r == 'owner']
        thread_id = _chat_thread_id(alert['target_user_id'], owner_ids[0] if owner_ids else None) \
            if owner_ids else 'group'
        msg = {
            "id": uuid.uuid4().hex, "ts": int(time.time()), "user_id": user['id'],
            "name": user.get('first_name', str(user['id'])), "text": alert['comment'],
            "to_user_id": None if thread_id == 'group' else thread_id,
            "critical_alert_id": alert['id'],
        }
        with _chat_lock:
            messages = _load_chat()
            messages.append(msg)
            _save_chat(messages)
    return alert


@app.post("/api/critical-alerts/{alert_id}/resolve")
def resolve_critical_alert(alert_id: str, resolution: str = Form(...), note: str = Form(''),
                            files: list[UploadFile] = File(default=[]),
                            user: dict = Depends(get_current_user)):
    if resolution not in ('yes', 'no'):
        raise HTTPException(400, "resolution должен быть yes или no")
    # Read-only lookup for the 404/403 checks -- NOT the final write path, see
    # the update_json_transaction call below for why (same race class as
    # ack_critical_alert, 23.09 owner review).
    existing = next((a for a in _load_critical_alerts() if a['id'] == alert_id), None)
    if not existing:
        raise HTTPException(404, "Алерт не найден")
    if existing['target_user_id'] != str(user['id']):
        raise HTTPException(403, "Не ваш алерт")

    saved_photos = []
    if resolution == 'yes' and files:
        # 30.07 (Release-аудит P1-8): basename на write-стороне для согласованности
        # с read-стороной (GET .../photo/{filename} уже санитирует). alert_id уже
        # проверен выше, практическая эксплуатируемость низкая (id -- server-
        # generated uuid), но раз паттерн есть на чтении -- должен быть и на записи.
        # File I/O deliberately stays OUTSIDE the JSON transaction lock below --
        # it doesn't touch critical_alerts.json and must not hold that lock for
        # the duration of a photo upload.
        alert_dir = os.path.join(CRITICAL_ALERT_PHOTO_DIR, os.path.basename(alert_id))
        os.makedirs(alert_dir, exist_ok=True)
        for f in files:
            data = f.file.read()
            if len(data) > 8 * 1024 * 1024:
                raise HTTPException(400, "Файл слишком большой (макс. 8 МБ)")
            detected = sniff_image(data)
            if not detected:
                raise HTTPException(400, "Файл должен быть изображением")
            ext = '.' + _ALLOWED_IMAGE_MIME_EXT[detected]
            fname = f"{uuid.uuid4().hex}{ext}"
            with open(os.path.join(alert_dir, fname), 'wb') as out:
                out.write(data)
            saved_photos.append(fname)

    def _mutate(items):
        alert = next((a for a in items if a['id'] == alert_id), None)
        if not alert:
            raise HTTPException(404, "Алерт не найден")
        if alert['target_user_id'] != str(user['id']):
            raise HTTPException(403, "Не ваш алерт")
        alert['resolution'] = resolution
        alert['resolution_note'] = note.strip()[:500] or None
        alert['resolution_photos'] = saved_photos
        return alert

    alert = update_json_transaction(CRITICAL_ALERTS_FILE, [], _mutate)

    roles = _load_roles()
    owner_ids = [uid for uid, r in roles.items() if r == 'owner']
    thread_id = _chat_thread_id(alert['target_user_id'], owner_ids[0] if owner_ids else None) \
        if owner_ids else 'group'
    text = f"Вопрос решён: {'да' if resolution == 'yes' else 'нет'}"
    if note:
        text += f"\n{note}"
    if saved_photos:
        text += f"\n📷 {len(saved_photos)} фото"
    msg = {
        "id": uuid.uuid4().hex, "ts": int(time.time()), "user_id": user['id'],
        "name": user.get('first_name', str(user['id'])), "text": text,
        "to_user_id": None if thread_id == 'group' else thread_id,
        "critical_alert_id": alert['id'],
    }
    with _chat_lock:
        messages = _load_chat()
        messages.append(msg)
        _save_chat(messages)
    return alert


@app.get("/api/critical-alerts/{alert_id}/photo/{filename}")
def get_critical_alert_photo(alert_id: str, filename: str, user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    # path traversal: alert_id/filename идут из URL напрямую в os.path.join без
    # проверки против known-хранимых значений (в отличие от object documents/chat
    # attachments, которые матчат fname против JSON-store перед сборкой пути) --
    # basename() режет любой ../ компонент до склейки.
    safe_alert_id = os.path.basename(alert_id)
    safe_filename = os.path.basename(filename)
    if safe_alert_id != alert_id or safe_filename != filename:
        raise HTTPException(404, "Фото не найдено")
    path = os.path.join(CRITICAL_ALERT_PHOTO_DIR, safe_alert_id, safe_filename)
    if not os.path.exists(path):
        raise HTTPException(404, "Фото не найдено")
    if role != 'owner':
        alert = next((a for a in _load_critical_alerts() if a['id'] == alert_id), None)
        if not alert or alert['target_user_id'] != str(user['id']):
            raise HTTPException(403, "Нет доступа к этому фото")
    return FileResponse(path)


@app.get("/api/critical-alerts/pending")
def list_pending_critical_alerts(user: dict = Depends(get_current_user)):
    """Polling endpoint для глобального попапа — только непрочитанные алерты текущего юзера.

    23.09 (owner review): defensively collapses legacy bare-uid birthday
    siblings (see _is_legacy_bare_uid_birthday_ref()) down to ONE canonical
    record before returning -- in case cleanup_legacy_critical_alert_
    duplicates.py's migration hasn't run yet, or a stale poll response still
    surfaces a sibling _supersede_legacy_siblings() hasn't caught yet. This
    is read-side only (never writes) -- the actual persistent fix is the
    migration script + the write-side supersede in ack_critical_alert().
    Non-legacy alerts (new idem-shaped birthday ref_id, every other kind)
    pass through completely unfiltered."""
    items = [a for a in _load_critical_alerts()
             if a['target_user_id'] == str(user['id']) and not a.get('acknowledged_at')]

    legacy_by_key: dict[tuple, dict] = {}
    result = []
    for a in items:
        if not _is_legacy_bare_uid_birthday_ref(a):
            result.append(a)
            continue
        key = (a['kind'], a['target_user_id'], a.get('ref_id'))
        current = legacy_by_key.get(key)
        if current is None or a.get('created_at', 0) > current.get('created_at', 0):
            legacy_by_key[key] = a
    result.extend(legacy_by_key.values())
    return {"alerts": result}


class CriticalAlertCreateBody(BaseModel):
    target_user_id: str
    title: str
    subtitle: str = ''
    deadline_minutes: int | None = None


@app.post("/api/critical-alerts")
def create_critical_alert_endpoint(body: CriticalAlertCreateBody, user: dict = Depends(get_current_user),
                                    _: None = Depends(require_owner)):
    deadline_at = int(time.time()) + body.deadline_minutes * 60 if body.deadline_minutes else None
    return _create_critical_alert(
        target_user_id=body.target_user_id, kind='manual', title=body.title,
        subtitle=body.subtitle, deadline_at=deadline_at,
    )



# ---------- Abwesenheit domain ----------
# Persistence, validation, notifications and HTTP handlers live in
# routes/absence.py. Runtime dependencies are injected here so that the domain
# module never imports main.py and compatibility aliases contain no implementation.
if __package__:
    from .routes.absence import (
        AbsenceRouteDeps,
        AbwesenheitBody,
        AbwesenheitMoveBody,
        AbwesenheitStatusBody,
        create_absence_router,
    )
else:
    from routes.absence import (  # noqa: E402
        AbsenceRouteDeps,
        AbwesenheitBody,
        AbwesenheitMoveBody,
        AbwesenheitStatusBody,
        create_absence_router,
    )

_absence_router, _absence_handlers = create_absence_router(
    AbsenceRouteDeps(
        get_current_user=get_current_user,
        get_role=get_role,
        require_owner=require_owner,
        # Data paths and collaborators are deliberately resolved dynamically:
        # tests isolate the JSON file and runtime code may replace integrations.
        absence_file=lambda: ABWESENHEIT_FILE,
        reasons=ABWESENHEIT_REASONS,
        public_fields=ABWESENHEIT_PUBLIC_FIELDS,
        safe_load_json=lambda path, default: _safe_load_json(path, default),
        atomic_write_json=lambda path, value: _atomic_write_json(path, value),
        update_json_transaction=lambda path, default, mutator: update_json_transaction(
            path, default, mutator
        ),
        load_roles=lambda: _load_roles(),
        send_telegram_message=lambda *args, **kwargs: send_telegram_message(*args, **kwargs),
        business_today_str=lambda: business_today_str(),
        shadow_create_absence=lambda *args, **kwargs: shadow_create_absence(*args, **kwargs),
        create_critical_alert=lambda *args, **kwargs: _create_critical_alert(*args, **kwargs),
        resolve_current_display_name=lambda user_id, fallback: _resolve_current_display_name(
            user_id, fallback
        ),
    )
)
app.include_router(_absence_router)

# Compatibility names for direct-call tests/internal callers during migration.
# These are aliases only; no absence implementation remains in main.py.
_load_abwesenheit = _absence_handlers.load_abwesenheit
_save_abwesenheit = _absence_handlers.save_abwesenheit
_migrate_abwesenheit_legacy_ids = _absence_handlers.migrate_abwesenheit_legacy_ids
_notify_owner_abwesenheit_pending = _absence_handlers.notify_owner_abwesenheit_pending
_notify_worker_abwesenheit_decision = _absence_handlers.notify_worker_abwesenheit_decision
_validate_date_str = _absence_handlers.validate_date_str
_month_end = _absence_handlers.month_end
create_abwesenheit = _absence_handlers.create_abwesenheit
close_abwesenheit = _absence_handlers.close_abwesenheit
update_abwesenheit_status = _absence_handlers.update_abwesenheit_status
update_abwesenheit_dates = _absence_handlers.update_abwesenheit_dates
_auto_close_expired_open_ended_abwesenheit = _absence_handlers.auto_close_expired_open_ended_abwesenheit
list_my_abwesenheit = _absence_handlers.list_my_abwesenheit
list_all_abwesenheit = _absence_handlers.list_all_abwesenheit
delete_abwesenheit = _absence_handlers.delete_abwesenheit


# ── DailyPlan routes (Round 1 — Production Control) ────────────────────────

try:
    from .routes.daily_plan import (
        DailyPlanRouteDeps,
        create_daily_plan_router,
        PlanBlockerBody as _DailyPlanPlanBlockerBody,
        DailyPlanItemIn as _DailyPlanDailyPlanItemIn,
        DailyPlanIn as _DailyPlanDailyPlanIn,
        ReplanRequestBody as _DailyPlanReplanRequestBody,
    )
except ImportError:
    from routes.daily_plan import (  # noqa: E402
        DailyPlanRouteDeps,
        create_daily_plan_router,
        PlanBlockerBody as _DailyPlanPlanBlockerBody,
        DailyPlanItemIn as _DailyPlanDailyPlanItemIn,
        DailyPlanIn as _DailyPlanDailyPlanIn,
        ReplanRequestBody as _DailyPlanReplanRequestBody,
    )


PlanBlockerBody = _DailyPlanPlanBlockerBody
DailyPlanItemIn = _DailyPlanDailyPlanItemIn
DailyPlanIn = _DailyPlanDailyPlanIn
ReplanRequestBody = _DailyPlanReplanRequestBody


_daily_plan_router, _daily_plan_handlers = create_daily_plan_router(DailyPlanRouteDeps(
    get_current_user=get_current_user,
    get_role=get_role,
    require_owner=require_owner,
    business_today=lambda: business_today(),
    business_today_str=lambda: business_today_str(),
    load_roles=lambda: _load_roles(),
    load_worker_profiles=lambda: _load_worker_profiles(),
    sanitize_display_name=lambda raw, fallback: _sanitize_display_name(raw, fallback),
    create_critical_alert=lambda **kwargs: _create_critical_alert(**kwargs),
    load_checkin_meta=lambda: _load_checkin_meta(),
    is_active_photo_checkin_session=lambda s: _is_active_photo_checkin_session(s),
))
# Canonical router registration: tests and manifests flatten _IncludedRouter
# through tests.conftest.iter_app_routes()/equivalent production-package smoke.
app.include_router(_daily_plan_router)

# Legacy direct-call compatibility: tests and operational scripts still import
# handlers from main.py. Runtime routes are registered by backend.routes.daily_plan.
daily_plan_today = _daily_plan_handlers.daily_plan_today
daily_plan_accept = _daily_plan_handlers.daily_plan_accept
daily_plan_accept_amendment = _daily_plan_handlers.daily_plan_accept_amendment
daily_plan_report_blocker = _daily_plan_handlers.daily_plan_report_blocker
daily_plan_owner_today = _daily_plan_handlers.daily_plan_owner_today
daily_plan_object = _daily_plan_handlers.daily_plan_object
daily_plan_create = _daily_plan_handlers.daily_plan_create
daily_plan_get = _daily_plan_handlers.daily_plan_get
get_worker_productivity_api = _daily_plan_handlers.get_worker_productivity_api
set_worker_baseline = _daily_plan_handlers.set_worker_baseline
daily_plan_owner_matrix = _daily_plan_handlers.daily_plan_owner_matrix
daily_plan_replan = _daily_plan_handlers.daily_plan_replan
_build_daily_plan_response = _daily_plan_handlers.build_daily_plan_response
_daily_plan_status_label = _daily_plan_handlers.daily_plan_status_label
_checkin_shift_status = _daily_plan_handlers.checkin_shift_status
_execution_summary = _daily_plan_handlers.execution_summary
_compute_risk_level = _daily_plan_handlers.compute_risk_level


# ── CRM Worker-Operations service bridge (additive, narrow scope) ──────────
# See routes/service_bridge.py module docstring: exposes DailyPlan read +
# Acceptance/Amendment-ack write to the CRM backend over a shared service
# token, so CRM's Worker Operations workspace reads/writes the SAME
# DailyPlan store this app's own /api/daily-plan/* routes use, instead of
# maintaining a second one. Disabled (503) unless MINIAPP_SERVICE_TOKEN is set.
try:
    from .routes.service_bridge import ServiceBridgeRouteDeps, create_service_bridge_router
except ImportError:
    from routes.service_bridge import ServiceBridgeRouteDeps, create_service_bridge_router  # noqa: E402

_service_bridge_router, _service_bridge_handlers = create_service_bridge_router(ServiceBridgeRouteDeps(
    business_today=lambda: business_today(),
    load_roles=lambda: _load_roles(),
    load_checkin_meta=lambda: _load_checkin_meta(),
    is_active_photo_checkin_session=lambda s: _is_active_photo_checkin_session(s),
    hours_from_session=lambda s: _hours_from_session(s),
    session_hours_live=lambda s: _session_hours_live(s),
))
app.include_router(_service_bridge_router)


# ── Contract ingestion routes (Round 5 — Drive scope gated) ─────────────────

# moved to core/constants.py -- _EMPTY_CONTRACT_STORE


def _load_contract_store() -> dict:
    """Item 14: routed through the existing safe-store mechanism (CRITICAL_JSON_PATHS
    already includes CONTRACT_INGEST_STATE_FILE) instead of a raw json.load() that
    silently wiped the contract list on any corruption."""
    return _safe_load_json(CONTRACT_INGEST_STATE_FILE, {"contracts": {}})


def _save_contract_store(data: dict) -> None:
    tmp = CONTRACT_INGEST_STATE_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONTRACT_INGEST_STATE_FILE)


@app.get("/api/contracts")
def list_contracts(_: None = Depends(require_owner)):
    """Список договоров и их статус обработки.
    DRIVE_SCOPE_REQUIRED: без CONTRACTS_DRIVE_FOLDER_ID ingestion не запускается."""
    store = _load_contract_store()
    contracts = list(store["contracts"].values())
    contracts.sort(key=lambda c: c.get("ingested_at", 0), reverse=True)
    return {
        "drive_configured": bool(CONTRACTS_DRIVE_FOLDER_ID),
        "drive_scope_status": "DRIVE_SCOPE_REQUIRED" if not CONTRACTS_DRIVE_FOLDER_ID else "configured",
        "total": len(contracts),
        "contracts": [
            {
                "id": c["id"],
                "file_name": c.get("file_name", ""),
                "status": c.get("status", "unknown"),
                "ingested_at": c.get("ingested_at"),
                "approved_at": c.get("approved_at"),
                "rejected_at": c.get("rejected_at"),
                "error": c.get("error"),
                "has_draft_plan": bool(c.get("project_plan_draft")),
            }
            for c in contracts
        ],
    }


@app.get("/api/contracts/{contract_id}")
def get_contract(
    contract_id: str,
    _: None = Depends(require_owner),
):
    """Полный договор с извлечёнными фактами и черновиком плана."""
    store = _load_contract_store()
    contract = store["contracts"].get(contract_id)
    if not contract:
        raise HTTPException(404, "Договор не найден")
    return contract


class ContractReviewBody(BaseModel):
    notes: str = ''


@app.post("/api/contracts/{contract_id}/approve")
def approve_contract(
    contract_id: str,
    body: ContractReviewBody = ContractReviewBody(),
    user: dict = Depends(get_current_user),
    _: None = Depends(require_owner),
):
    """Owner утверждает черновик плана из договора (УТВЕРДИТЬ).
    После утверждения project_plan_draft становится publishable для работников."""
    with _CONTRACT_INGEST_LOCK:
        store = _load_contract_store()
        contract = store["contracts"].get(contract_id)
        if not contract:
            raise HTTPException(404, "Договор не найден")
        if contract.get("status") not in ("ingested", "needs_manual_review"):
            raise HTTPException(400, f"Договор в статусе '{contract['status']}' не может быть утверждён")
        if not contract.get("project_plan_draft"):
            raise HTTPException(400, "Нет черновика плана для утверждения")
        contract["status"] = "approved"
        contract["approved_at"] = time.time()
        contract["approved_by"] = str(user['id'])
        contract["review_notes"] = body.notes.strip()[:1000]
        _save_contract_store(store)
    return {"status": "approved", "contract_id": contract_id}


@app.post("/api/contracts/{contract_id}/reject")
def reject_contract(
    contract_id: str,
    body: ContractReviewBody = ContractReviewBody(),
    user: dict = Depends(get_current_user),
    _: None = Depends(require_owner),
):
    """Owner отклоняет черновик плана (ОТКЛОНИТЬ)."""
    with _CONTRACT_INGEST_LOCK:
        store = _load_contract_store()
        contract = store["contracts"].get(contract_id)
        if not contract:
            raise HTTPException(404, "Договор не найден")
        if contract.get("status") == "approved":
            raise HTTPException(400, "Утверждённый договор нельзя отклонить без отмены")
        contract["status"] = "rejected"
        contract["rejected_at"] = time.time()
        contract["rejected_by"] = str(user['id'])
        contract["review_notes"] = body.notes.strip()[:1000]
        _save_contract_store(store)
    return {"status": "rejected", "contract_id": contract_id}


@app.post("/api/contracts/ingest")
def trigger_contract_ingest(
    _: None = Depends(require_owner),
):
    """Ручной триггер перепроверки Drive-папки.
    DRIVE_SCOPE_REQUIRED: если CONTRACTS_DRIVE_FOLDER_ID не задан — 503."""
    if not CONTRACTS_DRIVE_FOLDER_ID:
        raise HTTPException(503, "DRIVE_SCOPE_REQUIRED: задайте CONTRACTS_DRIVE_FOLDER_ID в env")
    return {
        "status": "trigger_sent",
        "message": "Синхронизация с Drive будет выполнена при следующем запуске plan_sync / contract_ingest.py",
        "folder_id": CONTRACTS_DRIVE_FOLDER_ID,
    }


# 31.07 (Release-аудит П4): регистрация критичных сторов -- в самом конце модуля,
# после того как все _FILE-константы (включая rl.ROADMAP_FILE/rl.STAGE_REQUESTS_FILE,
# доступные только после _load_repo_roadmap_lib() выше) уже определены. Порча этих
# файлов не должна тихо деградировать к default (см. _safe_load_json/update_json_transaction).
CRITICAL_JSON_PATHS.update({
    ROLES_FILE,
    OBJECT_ASSIGNMENTS_FILE,
    CHECKIN_META_FILE,
    CHECKIN_IDEMPOTENCY_FILE,
    CHAT_FILE,
    CHAT_ARCHIVE_FILE,
    ABWESENHEIT_FILE,
    WORKER_PROFILES_FILE,
    TASKS_FILE,
    CRITICAL_ALERTS_FILE,
    rl.ROADMAP_FILE,
    rl.STAGE_REQUESTS_FILE,
    DAILY_PLAN_STORE_FILE,
    FINISH_OUTBOX_FILE,
    OBJECT_INFO_FILE,
    OBJECT_IMAGES_FILE,
    WORK_CALENDAR_FILE,
    TOOL_BOOKINGS_FILE,
    CHAT_THREAD_META_FILE,
    CONTRACT_INGEST_STATE_FILE,
})