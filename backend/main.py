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


# ---------- Workforce profiles domain ----------
# Roster, profile/onboarding, skills verification and avatar HTTP flow live in
# routes/workforce_profiles.py. Cross-domain check-in/Sheets state is injected.
try:
    from .core.profiles import _load_worker_profiles, _sanitize_display_name
    from .routes.workforce_profiles import (
        ProfileUpdateBody,
        SkillV2Body,
        SkillVerificationBody,
        WorkforceProfilesDeps,
        create_workforce_profiles_router,
    )
except ImportError:
    from core.profiles import _load_worker_profiles, _sanitize_display_name  # noqa: E402
    from routes.workforce_profiles import (  # noqa: E402
        ProfileUpdateBody,
        SkillV2Body,
        SkillVerificationBody,
        WorkforceProfilesDeps,
        create_workforce_profiles_router,
    )

_workforce_profiles_router, _workforce_profiles_handlers = create_workforce_profiles_router(
    WorkforceProfilesDeps(
        get_current_user=get_current_user,
        get_role=get_role,
        require_owner=require_owner,
        load_roles=lambda: _load_roles(),
        load_worker_profiles=lambda: _load_worker_profiles(),
        sanitize_display_name=lambda raw, fallback: _sanitize_display_name(raw, fallback),
        update_json_transaction=update_json_transaction,
        atomic_write_json=lambda path, value: _atomic_write_json(path, value),
        worker_profiles_file=lambda: WORKER_PROFILES_FILE,
        work_types_catalog=wt.work_types_catalog,
        skill_options=[w['name'] for w in wt.WORK_TYPES if w['active']],
        normalize_profile_skills=pskills.normalize_profile_skills,
        legacy_skill_names_from_v2=pskills.legacy_skill_names_from_v2,
        set_skill_verification=pskills.set_skill_verification,
        skill_levels=pskills.SKILL_LEVELS,
        business_today=lambda: business_today(),
        business_today_str=lambda: business_today_str(),
        last_seen_for=lambda uid: _last_seen.get(str(uid)),
        online_threshold_seconds=ONLINE_THRESHOLD_SECONDS,
        load_checkin_meta=lambda: _load_checkin_meta(),
        is_active_photo_checkin_session=lambda session: _is_active_photo_checkin_session(session),
        cached_get_used_range=lambda tab: _cached_get_used_range(tab),
        avatar_dir=lambda: AVATAR_DIR,
        avatar_max_bytes=AVATAR_MAX_BYTES,
        sniff_image=sniff_image,
        allowed_image_mime_ext=_ALLOWED_IMAGE_MIME_EXT,
    )
)
app.include_router(_workforce_profiles_router)

SKILL_OPTIONS = _workforce_profiles_handlers.skill_options
_save_worker_profiles = _workforce_profiles_handlers.save_worker_profiles
_get_worker_profile = _workforce_profiles_handlers.get_worker_profile
_get_worker_skills_v2 = _workforce_profiles_handlers.get_worker_skills_v2
_is_meaningful_name = _workforce_profiles_handlers.is_meaningful_name
_resolve_current_display_name = _workforce_profiles_handlers.resolve_current_display_name
_validate_birthday = _workforce_profiles_handlers.validate_birthday
_profile_completion_status = _workforce_profiles_handlers.profile_completion_status
list_workers = _workforce_profiles_handlers.list_workers
get_work_types = _workforce_profiles_handlers.get_work_types
get_my_profile = _workforce_profiles_handlers.get_my_profile
get_user_card = _workforce_profiles_handlers.get_user_card
update_my_profile = _workforce_profiles_handlers.update_my_profile
verify_worker_skill = _workforce_profiles_handlers.verify_worker_skill
upload_my_avatar = _workforce_profiles_handlers.upload_my_avatar
get_avatar = _workforce_profiles_handlers.get_avatar

# Check-in-specific GPS heuristic stays in the composition module until the
# remaining check-in support helpers are consolidated separately.
def _gps_suspect(lat: str, lon: str) -> bool:
    try:
        f_lat, f_lon = float(lat), float(lon)
    except (ValueError, TypeError):
        return True
    if f_lat == 0 and f_lon == 0:
        return True
    return not (47 <= f_lat <= 55.5 and 5 <= f_lon <= 16)

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


# ---------- Workforce statistics domain ----------
try:
    from .routes.workforce_stats import WorkforceStatsDeps, create_workforce_stats_router
except ImportError:
    from routes.workforce_stats import WorkforceStatsDeps, create_workforce_stats_router  # noqa: E402

_workforce_stats_router, _workforce_stats_handlers = create_workforce_stats_router(
    WorkforceStatsDeps(
        get_current_user=get_current_user,
        get_role=get_role,
        require_owner=require_owner,
        load_checkin_meta=lambda: _load_checkin_meta(),
        load_roles=lambda: _load_roles(),
        load_worker_profiles=lambda: _load_worker_profiles(),
        sanitize_display_name=lambda raw, fallback: _sanitize_display_name(raw, fallback),
        hours_from_session=lambda session: _hours_from_session(session),
        session_hours_live=lambda session: _session_hours_live(session),
        is_active_photo_checkin_session=lambda session: _is_active_photo_checkin_session(session),
        business_today=lambda: business_today(),
        load_assignments=lambda: _load_assignments(),
        cached_get_used_range=lambda tab: _cached_get_used_range(tab),
        load_abwesenheit=lambda: _load_abwesenheit(),
        get_worker_profile=lambda uid: _get_worker_profile(uid),
        get_worker_skills_v2=lambda uid: _get_worker_skills_v2(uid),
        legacy_skill_names_from_v2=pskills.legacy_skill_names_from_v2,
    )
)
app.include_router(_workforce_stats_router)
profile_stats = _workforce_stats_handlers.profile_stats
get_dashboard_team_hours = _workforce_stats_handlers.get_dashboard_team_hours


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






ASSIGNMENT_CONFIRM_WARNING_SECONDS = 2 * 60 * 60
ASSIGNMENT_CONFIRM_DANGER_SECONDS = 4 * 60 * 60


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


# ---------- Owner dashboard domain ----------
try:
    from .routes.owner_dashboard import OwnerDashboardDeps, create_owner_dashboard_router
except ImportError:
    from routes.owner_dashboard import OwnerDashboardDeps, create_owner_dashboard_router  # noqa: E402

_owner_dashboard_router, _owner_dashboard_handlers = create_owner_dashboard_router(
    OwnerDashboardDeps(
        get_current_user=get_current_user,
        require_owner=require_owner,
        business_today_str=lambda: business_today_str(),
        business_today=lambda: business_today(),
        load_checkin_meta=lambda: _load_checkin_meta(),
        load_worker_profiles=lambda: _load_worker_profiles(),
        sanitize_display_name=lambda raw, fallback: _sanitize_display_name(raw, fallback),
        cached_get_used_range=lambda tab: _cached_get_used_range(tab),
        is_active_photo_checkin_session=lambda session: _is_active_photo_checkin_session(session),
        hours_from_session=lambda session: _hours_from_session(session),
        load_assignments=lambda: _load_assignments(),
        load_abwesenheit=lambda: _load_abwesenheit(),
        load_roles=lambda: _load_roles(),
        load_repo_objekte_lib=lambda: _load_repo_objekte_lib(),
        load_roadmap_store=lambda: _safe_load_json(rl.ROADMAP_FILE, rl._default_store()),
        skill_display_name=pskills.skill_display_name,
        confirm_warning_seconds=ASSIGNMENT_CONFIRM_WARNING_SECONDS,
        confirm_danger_seconds=ASSIGNMENT_CONFIRM_DANGER_SECONDS,
    )
)
app.include_router(_owner_dashboard_router)

_assignment_status = _owner_dashboard_handlers.assignment_status
_parse_assignment_timestamp = _owner_dashboard_handlers.parse_assignment_timestamp
_assignment_pending_escalation = _owner_dashboard_handlers.assignment_pending_escalation
get_dashboard_shifts_today = _owner_dashboard_handlers.get_dashboard_shifts_today
get_active_blockers = _owner_dashboard_handlers.get_active_blockers
get_team_plan = _owner_dashboard_handlers.get_team_plan
_format_assignment_wait = _owner_dashboard_handlers.format_assignment_wait
_assignment_confirmation_alerts = _owner_dashboard_handlers.assignment_confirmation_alerts


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


# ---------- Photo feed domain ----------
if __package__:
    from .routes.photo_feed import (
        PhotoCommentBody, PhotoFeedRouteDeps, PhotoReactionBody, create_photo_feed_router,
    )
else:
    from routes.photo_feed import (  # noqa: E402
        PhotoCommentBody, PhotoFeedRouteDeps, PhotoReactionBody, create_photo_feed_router,
    )

_photo_feed_router, _photo_feed_handlers = create_photo_feed_router(
    PhotoFeedRouteDeps(
        get_current_user=get_current_user,
        get_role=get_role,
        safe_load_json=lambda path, default: _safe_load_json(path, default),
        atomic_write_json=lambda path, value: _atomic_write_json(path, value),
        photo_dir=lambda: PHOTO_DIR,
        photo_meta_file=lambda: PHOTO_META_FILE,
        photo_reactions_file=lambda: PHOTO_REACTIONS_FILE,
        checkin_photo_base=lambda: CHECKIN_PHOTO_BASE,
        photo_max_bytes=PHOTO_MAX_BYTES,
        photo_max_count=PHOTO_MAX_COUNT,
        photo_max_files=PHOTO_MAX_FILES,
        sniff_image=lambda raw: sniff_image(raw),
        allowed_image_mime_ext=_ALLOWED_IMAGE_MIME_EXT,
        load_feed_saved=lambda: _load_feed_saved(),
        is_feed_saved=lambda saved, uid, item_type, item_id: _is_feed_saved(saved, uid, item_type, item_id),
        sanitize_display_name=lambda raw, fallback: _sanitize_display_name(raw, fallback),
        resolve_current_display_name=lambda uid, fallback: _resolve_current_display_name(uid, fallback),
        resolve_object_name=lambda object_id: _resolve_object_name(object_id),
        emit_comment_activity_alerts=lambda *args, **kwargs: _emit_comment_activity_alerts(*args, **kwargs),
        comment_alert_recipients=lambda *args, **kwargs: _comment_alert_recipients(*args, **kwargs),
    )
)
app.include_router(_photo_feed_router)

# Compatibility aliases for remaining bridges/check-in callers during migration.
_photo_lock = _photo_feed_handlers.photo_lock
_load_photo_meta = _photo_feed_handlers.load_photo_meta
_save_photo_meta = _photo_feed_handlers.save_photo_meta
_load_photo_reactions = _photo_feed_handlers.load_photo_reactions
_save_photo_reactions = _photo_feed_handlers.save_photo_reactions
_copy_checkin_photos_to_feed = _photo_feed_handlers.copy_checkin_photos_to_feed
_upsert_checkin_feed_post = _photo_feed_handlers.upsert_checkin_feed_post
list_feed_photos = _photo_feed_handlers.list_feed_photos
upload_feed_photo = _photo_feed_handlers.upload_feed_photo
add_feed_photo_comment = _photo_feed_handlers.add_feed_photo_comment
react_feed_photo = _photo_feed_handlers.react_feed_photo
get_feed_photo_comments = _photo_feed_handlers.get_feed_photo_comments
delete_feed_photo_comment = _photo_feed_handlers.delete_feed_photo_comment


# ---------- Activity/comment bridge wiring is composed after Chat core. ----------

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



# ---------- Activity/comment bridge ----------
# This integration surface needs Feed/Photo plus Chat handlers, so composition
# intentionally happens after Chat core has been constructed.
if __package__:
    from .routes.activity_bridge import (
        ActivityBridgeDeps, ActivityReadBody, CommentForwardBody,
        create_activity_bridge_router,
    )
else:
    from routes.activity_bridge import (  # noqa: E402
        ActivityBridgeDeps, ActivityReadBody, CommentForwardBody,
        create_activity_bridge_router,
    )

_activity_bridge_router, _activity_bridge_handlers = create_activity_bridge_router(
    ActivityBridgeDeps(
        get_current_user=get_current_user,
        get_role=get_role,
        safe_load_json=lambda path, default: _safe_load_json(path, default),
        atomic_write_json=lambda path, value: _atomic_write_json(path, value),
        activity_alerts_file=lambda: ACTIVITY_ALERTS_FILE,
        news_feed_file=lambda: NEWS_FEED_FILE,
        photo_dir=lambda: PHOTO_DIR,
        cached_get_used_range=lambda tab_name: _cached_get_used_range(tab_name),
        load_roles=lambda: _load_roles(),
        load_news_comments=lambda: _load_news_comments(),
        photo_lock=_photo_lock,
        load_photo_meta=lambda: _load_photo_meta(),
        check_thread_access=lambda thread_key, uid, role: _check_thread_access(thread_key, uid, role),
        reject_self_chat=lambda uid, target_uid: _reject_self_chat(uid, target_uid),
        validate_dm_recipient=lambda target_uid: _validate_dm_recipient(target_uid),
        chat_thread_id=lambda uid, target_uid: _chat_thread_id(uid, target_uid),
        load_chat_thread_meta=lambda: _load_chat_thread_meta(),
        chat_lock=_chat_lock,
        load_chat=lambda: _load_chat(),
        save_chat=lambda messages: _save_chat(messages),
    )
)
app.include_router(_activity_bridge_router)

# Compatibility aliases for existing alert aggregation, tests and callers.
_load_activity_alerts = _activity_bridge_handlers.load_activity_alerts
_save_activity_alerts = _activity_bridge_handlers.save_activity_alerts
_news_post_title = _activity_bridge_handlers.news_post_title
_find_object_row_by_id = _activity_bridge_handlers.find_object_row_by_id
_resolve_object_name = _activity_bridge_handlers.resolve_object_name
_comment_alert_recipients = _activity_bridge_handlers.comment_alert_recipients
_emit_comment_activity_alerts = _activity_bridge_handlers.emit_comment_activity_alerts
mark_activity_alerts_read = _activity_bridge_handlers.mark_activity_alerts_read
forward_comment = _activity_bridge_handlers.forward_comment
get_feed_photo_file = _activity_bridge_handlers.get_feed_photo_file

# ---------- Voice transcription domain ----------
if __package__:
    from .routes.voice import VoiceRouteDeps, create_voice_router
else:
    from routes.voice import VoiceRouteDeps, create_voice_router  # noqa: E402

_voice_router, _voice_handlers = create_voice_router(
    VoiceRouteDeps(
        get_current_user=get_current_user,
        get_role=get_role,
        transcribe_audio_dir=lambda: TRANSCRIBE_AUDIO_DIR,
        transcribe_max_bytes=TRANSCRIBE_MAX_BYTES,
        chat_attach_dir=lambda: CHAT_ATTACH_DIR,
        sniff_audio=lambda data: sniff_audio(data),
        allowed_audio_mime_ext=_ALLOWED_AUDIO_MIME_EXT,
        check_thread_access=lambda thread_key, uid, role: _check_thread_access(thread_key, uid, role),
        reject_self_chat=lambda uid, target_uid: _reject_self_chat(uid, target_uid),
        validate_dm_recipient=lambda target_uid: _validate_dm_recipient(target_uid),
        chat_thread_id=lambda uid, target_uid: _chat_thread_id(uid, target_uid),
        load_chat_thread_meta=lambda: _load_chat_thread_meta(),
        chat_lock=_chat_lock,
        load_chat=lambda: _load_chat(),
        save_chat=lambda messages: _save_chat(messages),
    )
)
app.include_router(_voice_router)
_get_whisper = _voice_handlers.get_whisper
_transcribe_voice = _voice_handlers.transcribe_voice
transcribe_voice_endpoint = _voice_handlers.transcribe_voice_endpoint
get_transcribe_audio = _voice_handlers.get_transcribe_audio
post_chat_voice = _voice_handlers.post_chat_voice


# ---------- Manager assistant domain ----------
if __package__:
    from .routes.manager_assistant import (
        ExtractTaskBody, ManagementCommandBody, ManagerAssistantDeps,
        create_manager_assistant_router,
    )
else:
    from routes.manager_assistant import (  # noqa: E402
        ExtractTaskBody, ManagementCommandBody, ManagerAssistantDeps,
        create_manager_assistant_router,
    )

_manager_assistant_router, _manager_assistant_handlers = create_manager_assistant_router(
    ManagerAssistantDeps(
        get_current_user=get_current_user,
        get_role=get_role,
        business_now=lambda: business_now(),
        load_worker_profiles=lambda: _load_worker_profiles(),
        load_roles=lambda: _load_roles(),
        sanitize_display_name=lambda raw, fallback: _sanitize_display_name(raw, fallback),
        cached_get_used_range=lambda tab_name: _cached_get_used_range(tab_name),
        call_glm_json=lambda system, text: _call_glm_json(system, text),
    )
)
app.include_router(_manager_assistant_router)
_normalize_ru_lookup = _manager_assistant_handlers.normalize_ru_lookup
_normalize_ru_name_hint = _manager_assistant_handlers.normalize_ru_name_hint
_management_command_date = _manager_assistant_handlers.management_command_date
_resolve_management_worker = _manager_assistant_handlers.resolve_management_worker
_resolve_management_object = _manager_assistant_handlers.resolve_management_object
parse_management_command = _manager_assistant_handlers.parse_management_command
_management_workers_for_parse = _manager_assistant_handlers.management_workers_for_parse
_management_objects_for_parse = _manager_assistant_handlers.management_objects_for_parse
parse_manager_command = _manager_assistant_handlers.parse_manager_command
extract_task_from_text = _manager_assistant_handlers.extract_task_from_text


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


# ---------- AI Chat domain ----------
if __package__:
    from .routes import ai_chat as _ai_chat_domain
    from .routes.ai_chat import (
        AiChatBody, AiChatRouteDeps, AiModelBody, WorkerAiChatBody,
        create_ai_chat_router,
    )
else:
    import routes.ai_chat as _ai_chat_domain  # noqa: E402
    from routes.ai_chat import (  # noqa: E402
        AiChatBody, AiChatRouteDeps, AiModelBody, WorkerAiChatBody,
        create_ai_chat_router,
    )

_ai_chat_router, _ai_chat_handlers = create_ai_chat_router(
    AiChatRouteDeps(
        get_current_user=get_current_user,
        get_role=get_role,
        ai_rate_file=lambda: AI_RATE_FILE,
        worker_ai_rate_file=lambda: WORKER_AI_RATE_FILE,
        ai_model_file=lambda: AI_MODEL_FILE,
        ai_rate_limit=AI_RATE_LIMIT,
        worker_ai_rate_limit=WORKER_AI_RATE_LIMIT,
        ai_rate_window=AI_RATE_WINDOW,
        ai_models=AI_MODELS,
        ai_model_default=AI_MODEL_DEFAULT,
        owner_ai_env_allowlist=_OWNER_AI_ENV_ALLOWLIST,
        agent_root=lambda: AGENT_ROOT,
        ai_upload_max_bytes=AI_UPLOAD_MAX_BYTES,
        sniff_image_or_pdf=lambda raw: sniff_image_or_pdf(raw),
        allowed_image_mime_ext=_ALLOWED_IMAGE_MIME_EXT,
    )
)
app.include_router(_ai_chat_router)

# Compatibility names for task extraction/direct-call tests during migration.
CLAUDE_BIN = _ai_chat_domain.CLAUDE_BIN
OWNER_AI_ENABLED = _ai_chat_domain.OWNER_AI_ENABLED
AI_SYSTEM_PROMPT = _ai_chat_domain.AI_SYSTEM_PROMPT
WORKER_AI_SYSTEM_PROMPT = _ai_chat_domain.WORKER_AI_SYSTEM_PROMPT
_claude_cli_lock = _ai_chat_domain._claude_cli_lock
_owner_ai_subprocess_env = _ai_chat_handlers.owner_ai_subprocess_env
_get_ai_model = _ai_chat_handlers.get_ai_model_value
_set_ai_model = _ai_chat_handlers.set_ai_model_value
_check_ai_rate = _ai_chat_handlers.check_ai_rate
_call_glm = _ai_chat_handlers.call_glm
_call_glm_json = _ai_chat_handlers.call_glm_json
_messages_to_prompt = _ai_chat_handlers.messages_to_prompt
_call_claude_cli = _ai_chat_handlers.call_claude_cli
_call_ai = _ai_chat_handlers.call_ai
_is_multimodal_content = _ai_chat_handlers.is_multimodal_content
ai_chat = _ai_chat_handlers.ai_chat
worker_ai_chat = _ai_chat_handlers.worker_ai_chat
get_ai_model = _ai_chat_handlers.get_ai_model
set_ai_model = _ai_chat_handlers.set_ai_model
ai_chat_upload = _ai_chat_handlers.ai_chat_upload


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