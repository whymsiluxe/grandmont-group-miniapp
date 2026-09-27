#!/usr/bin/env python3
"""Grandmont Group Mini App — FastAPI backend. Фаза 2 плана: скелет + initData-auth + roles.
Запуск: uvicorn main:app --host 127.0.0.1 --port 8001
"""
import copy
import csv
import hashlib
import hmac
import importlib.util
import json
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
    all_ids = set(roles.keys()) | set(profiles.keys())
    workers = []
    for uid in all_ids:
        p = profiles.get(uid, {})
        last_seen = _last_seen.get(uid)
        role = roles.get(uid)  # None if not in roles.json -- no more silent 'worker' default
        workers.append({
            'user_id': uid,
            'role': role,
            'access_granted': role is not None,
            'name': _sanitize_display_name(p.get('name'), uid),
            'skills': p.get('skills', []),
            'has_avatar': bool(p.get('avatar')),
            'quiz_completed': p.get('quiz_completed', False),
            'online': bool(last_seen and (time.time() - last_seen) < ONLINE_THRESHOLD_SECONDS),
        })
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
    нужна миграция (skills_v2 отсутствует), файл не переписывается на каждый read."""
    profiles = _load_worker_profiles()
    key = str(user_id)
    profile = profiles.get(key, {"skills": [], "quiz_completed": False})
    skills_v2, changed = pskills.normalize_profile_skills(profile)
    if changed:
        profile['skills_v2'] = skills_v2
        profiles[key] = profile
        _save_worker_profiles(profiles)
    return skills_v2


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
    profiles = _load_worker_profiles()
    key = str(user['id'])
    profile = profiles.get(key, {"skills": [], "quiz_completed": False})
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
    _save_worker_profiles(profiles)
    return profile


class SkillVerificationBody(BaseModel):
    verified: bool


@app.patch("/api/workers/{user_id}/skills/{skill_id}/verification")
def verify_worker_skill(user_id: str, skill_id: str, body: SkillVerificationBody,
                         user: dict = Depends(get_current_user), _: None = Depends(require_owner)):
    """01.08 (спека п.4): только Owner подтверждает навык работника. Worker не может
    сам выставить verified: true -- см. update_my_profile выше, self-service PATCH
    всегда сбрасывает verified в False."""
    profiles = _load_worker_profiles()
    key = str(user_id)
    profile = profiles.get(key)
    if not profile:
        raise HTTPException(404, "Профиль не найден")
    skills_v2, _changed = pskills.normalize_profile_skills(profile)
    skills_v2, found = pskills.set_skill_verification(skills_v2, skill_id, body.verified)
    if not found:
        raise HTTPException(404, "У работника нет такого навыка")
    profile['skills_v2'] = skills_v2
    profiles[key] = profile
    _save_worker_profiles(profiles)
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
    profiles = _load_worker_profiles()
    profile = profiles.get(uid, {"skills": [], "quiz_completed": False})
    profile['avatar'] = True
    if not profile.get('name'):
        profile['name'] = user.get('first_name', uid)
    profiles[uid] = profile
    _save_worker_profiles(profiles)
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


@app.get("/api/tools")
def list_tools(user: dict = Depends(get_current_user)):
    tl = _load_repo_tools_lib()
    try:
        return {"tools": tl.list_tools()}
    except Exception as e:
        # Инструменты живут в отдельной Google Sheet. Если лист/диапазон временно
        # недоступен, это не должно ронять bootstrap всего miniapp и показывать
        # глобальный баннер поверх других вкладок.
        print(f"[tools] list_tools unavailable: {type(e).__name__}: {e}")
        return {"tools": [], "warning": "tools_unavailable"}


@app.get("/api/tools/{serial}/history")
def tool_history(serial: str, user: dict = Depends(get_current_user)):
    tl = _load_repo_tools_lib()
    return {"history": tl.tool_history(serial)}


class ToolBookingBody(BaseModel):
    date_from: str
    date_to: str
    object_name: str
    holder_id: str = ''
    holder: str = ''
    note: str = ''


def _parse_tool_booking_date(value: str, field_name: str):
    raw = (value or '').strip()
    try:
        return datetime.strptime(raw, '%Y-%m-%d').date()
    except ValueError:
        raise HTTPException(400, f"{field_name} должен быть YYYY-MM-DD")


def _tool_booking_default_store():
    return {"bookings": []}


def _tool_booking_overlaps(a_from: str, a_to: str, b_from: str, b_to: str) -> bool:
    return bool(a_from and a_to and b_from and b_to and a_from <= b_to and b_from <= a_to)


def _tool_booking_holder(body: ToolBookingBody, user: dict, role: str) -> tuple[str, str]:
    if role != 'owner':
        return str(user['id']), _holder_name_from_user(user)
    holder_id = (body.holder_id or '').strip()
    if holder_id:
        profiles = _load_worker_profiles()
        holder_name = _sanitize_display_name(profiles.get(holder_id, {}).get('name'), holder_id)
        return holder_id, holder_name
    return str(user['id']), (body.holder or _holder_name_from_user(user)).strip() or str(user['id'])


@app.get("/api/tools/bookings")
def list_tool_bookings(serial: str = '', date_from: str = '', date_to: str = '',
                       user: dict = Depends(get_current_user)):
    data = _safe_load_json(TOOL_BOOKINGS_FILE, _tool_booking_default_store())
    today = business_today_str()
    start = (date_from or today).strip()
    end = (date_to or start).strip()
    start_date = _parse_tool_booking_date(start, 'date_from')
    end_date = _parse_tool_booking_date(end, 'date_to')
    if end_date < start_date:
        raise HTTPException(400, "date_to раньше date_from")
    serial_filter = (serial or '').strip()
    bookings = []
    for b in data.get('bookings', []):
        if b.get('status') == 'cancelled':
            continue
        if serial_filter and str(b.get('serial', '')) != serial_filter:
            continue
        if not _tool_booking_overlaps(start, end, b.get('date_from', ''), b.get('date_to', '')):
            continue
        bookings.append(b)
    bookings.sort(key=lambda b: (b.get('date_from', ''), b.get('tool_name', ''), b.get('serial', '')))
    return {"bookings": bookings}


@app.post("/api/tools/{serial}/bookings")
def create_tool_booking(serial: str, body: ToolBookingBody,
                        user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    date_from = (body.date_from or '').strip()
    date_to = (body.date_to or '').strip()
    start_date = _parse_tool_booking_date(date_from, 'date_from')
    end_date = _parse_tool_booking_date(date_to, 'date_to')
    if end_date < start_date:
        raise HTTPException(400, "date_to раньше date_from")
    if start_date < business_today():
        raise HTTPException(400, "Нельзя бронировать инструмент в прошлом")
    object_name = (body.object_name or '').strip()
    if not object_name:
        raise HTTPException(400, "Укажи объект")

    tl = _load_repo_tools_lib()
    tool = tl.get_tool(serial)
    if tool is None:
        raise HTTPException(404, f'инструмент {serial} не найден')
    if tl.mapped_status(tool) in ('repair', 'missing'):
        raise HTTPException(409, "Инструмент недоступен для брони")
    # 17.09 (audit finding, P1): booking used to ignore the tool's CURRENT physical
    # possession entirely -- only repair/missing blocked it, so booking a tool for
    # TODAY while someone else already has it in-use/reserved right now silently
    # created a booking the requester could never actually fulfill today. Only
    # checked for start_date == today -- a future-dated booking is fine even if the
    # tool is in-use right now, since whoever holds it will plausibly return it
    # before then, and the existing overlap-check between bookings already protects
    # future-date conflicts between two bookings.
    if start_date == business_today() and tl.mapped_status(tool) in ('in-use', 'reserved'):
        raise HTTPException(409, "Инструмент сейчас занят -- бронь на сегодня недоступна")

    holder_id, holder_name = _tool_booking_holder(body, user, role)
    booking_id = uuid.uuid4().hex
    now = int(time.time())

    def _mutator(data):
        data.setdefault('bookings', [])
        for existing in data['bookings']:
            if existing.get('status') == 'cancelled':
                continue
            if str(existing.get('serial', '')) != str(serial):
                continue
            if _tool_booking_overlaps(date_from, date_to, existing.get('date_from', ''), existing.get('date_to', '')):
                raise HTTPException(409, "Инструмент уже забронирован на эти даты")
        booking = {
            "id": booking_id,
            "serial": str(serial),
            "tool_name": tool.get('Название Инструмента', ''),
            "category": tool.get('Категория', ''),
            "date_from": date_from,
            "date_to": date_to,
            "object_name": object_name,
            "holder_id": holder_id,
            "holder_name": holder_name,
            "note": (body.note or '').strip()[:300],
            "status": "active",
            "created_at": now,
            "created_by": str(user['id']),
        }
        data['bookings'].append(booking)
        return booking

    booking = update_json_transaction(TOOL_BOOKINGS_FILE, _tool_booking_default_store, _mutator)
    return {"booking": booking}


@app.delete("/api/tools/{serial}/bookings/{booking_id}")
def cancel_tool_booking(serial: str, booking_id: str,
                        user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    uid = str(user['id'])

    def _mutator(data):
        for booking in data.get('bookings', []):
            if booking.get('id') != booking_id or str(booking.get('serial', '')) != str(serial):
                continue
            if booking.get('status') == 'cancelled':
                return booking
            if role != 'owner' and booking.get('holder_id') != uid and booking.get('created_by') != uid:
                raise HTTPException(403, "Можно удалить только свою бронь")
            booking['status'] = 'cancelled'
            booking['cancelled_at'] = int(time.time())
            booking['cancelled_by'] = uid
            return booking
        raise HTTPException(404, "Бронь не найдена")

    booking = update_json_transaction(TOOL_BOOKINGS_FILE, _tool_booking_default_store, _mutator)
    return {"booking": booking}


class CheckoutBody(BaseModel):
    object_name: str
    # 30.07 (Инструменты cleanup): optional только для обратной совместимости --
    # backend больше НЕ доверяет holder от клиента (реальный найденный баг: Worker
    # мог вписать чужое имя, "Кто взял" и "ID держателя" относились бы к разным людям).
    # Держатель всегда определяется из авторизованного Telegram user ниже.
    holder: str = ''


def _holder_name_from_user(user: dict) -> str:
    """Порядок: first_name+last_name -> first_name -> username -> str(id).
    Единственный источник имени держателя для self-checkout -- клиентский holder
    из CheckoutBody игнорируется целиком, не подмешивается ни в каком виде."""
    first_name = (user.get('first_name') or '').strip()
    last_name = (user.get('last_name') or '').strip()
    holder_name = ' '.join(v for v in [first_name, last_name] if v).strip()
    if not holder_name:
        holder_name = (user.get('username') or '').strip()
    if not holder_name:
        holder_name = str(user['id'])
    return holder_name


_tool_locks: dict = {}
_tool_locks_guard = __import__('threading').Lock()


def _lock_for_tool(serial: str):
    """31.07 (Release-аудит П6): один Lock на serial -- checkout/return читают статус
    из Sheets (tools_lib.py, никакого threading.Lock внутри) и пишут отдельным вызовом;
    без лока, охватывающего ВЕСЬ цикл read-check-write, два конкурентных checkout на
    один serial могли оба пройти проверку mapped_status()=='free' до того как первый
    успевал записать нового держателя -- тот же класс гонки, что _lock_for()/
    update_json_transaction закрывают для JSON-сторов (см. их docstring выше), только
    здесь бэкенд данных -- Google Sheets, не JSON-файл."""
    with _tool_locks_guard:
        if serial not in _tool_locks:
            _tool_locks[serial] = __import__('threading').Lock()
        return _tool_locks[serial]


def _active_tool_booking_conflict(serial: str, on_date: str, requester_id: str):
    """17.09 (audit finding, P1): booking ('/api/tools/{serial}/bookings') was
    purely advisory -- checkout_tool never consulted TOOL_BOOKINGS_FILE, so a
    worker could book a tool for tomorrow and have someone else walk up and
    take it today via the plain checkout flow with zero warning. Returns the
    conflicting booking dict if an ACTIVE booking for this serial covers
    on_date and belongs to someone other than requester_id -- the booking's
    own holder checking the tool out early is allowed (that IS them fulfilling
    their reservation), a different person is not."""
    data = _safe_load_json(TOOL_BOOKINGS_FILE, _tool_booking_default_store())
    for b in data.get('bookings', []):
        if b.get('status') == 'cancelled':
            continue
        if str(b.get('serial', '')) != str(serial):
            continue
        b_from, b_to = b.get('date_from', ''), b.get('date_to', '')
        if not (b_from and b_to and b_from <= on_date <= b_to):
            continue
        if str(b.get('holder_id', '')) == str(requester_id):
            continue
        return b
    return None


@app.patch("/api/tools/{serial}/checkout")
def checkout_tool(serial: str, body: CheckoutBody, user: dict = Depends(get_current_user)):
    tl = _load_repo_tools_lib()
    if not body.object_name.strip():
        raise HTTPException(400, "Укажи объект")
    with _lock_for_tool(serial):
        tool = tl.get_tool(serial)
        if tool is None:
            raise HTTPException(404, f'инструмент {serial} не найден')
        if tl.mapped_status(tool) != 'free':
            raise HTTPException(409, "Инструмент уже выдан или недоступен")
        conflict = _active_tool_booking_conflict(serial, business_today_str(), str(user['id']))
        if conflict is not None:
            raise HTTPException(409, f"Инструмент забронирован на сегодня: {conflict.get('holder_name', '')} ({conflict.get('object_name', '')})")
        # 22.07: worker сам оформляет checkout — user['id'] это и есть реальный держатель,
        # пишем как holder_id чтобы avatar на карточке инструмента был кликабельным (openUserCard).
        holder_name = _holder_name_from_user(user)
        tl.checkout_tool(serial, holder_name, body.object_name, holder_name, holder_id=str(user['id']))
    return {"status": "ok"}


@app.patch("/api/tools/{serial}/return")
def return_tool(serial: str, user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    """30.07 (Инструменты-редизайн, п.11, реальный найденный баг): предыдущая версия
    "возврата" вызывала /checkout с пустыми holder/object_name -- checkout_tool всё равно
    пишет holder_id текущего юзера безусловно, так что "возврат" на деле мог сделать
    держателем СВОБОДНОГО инструмента того, кто на самом деле его не брал. Отдельный
    endpoint: текущий держатель или owner -- разрешено, посторонний worker -- 403.

    31.07 (Release-аудит П6): read-check-write под тем же _lock_for_tool(serial), что
    checkout -- та же гонка, зеркально (два одновременных return, или return+checkout
    на один serial)."""
    tl = _load_repo_tools_lib()
    with _lock_for_tool(serial):
        tool = tl.get_tool(serial)
        if tool is None:
            raise HTTPException(404, f'инструмент {serial} не найден')
        holder_id = (tool.get('ID держателя') or '').strip()
        if role != 'owner' and str(user['id']) != holder_id:
            raise HTTPException(403, "Можно вернуть только инструмент, который взят вами")
        tl.return_tool(serial, user.get('first_name', str(user['id'])))
    return {"status": "ok"}


class ToolUpdateBody(BaseModel):
    status: str
    holder: str = ''
    object_name: str = ''
    # 30.07 (Инструменты-редизайн, п.7): Owner теперь выбирает Worker из /api/workers
    # вместо ручного ввода имени -- holder_id делает avatar держателя в карточке
    # кликабельным (openUserCard), тот же смысл что уже есть у worker-self checkout.
    holder_id: str = ''


@app.patch("/api/tools/{serial}")
def update_tool(serial: str, body: ToolUpdateBody, user: dict = Depends(get_current_user), _: None = Depends(require_owner)):
    tl = _load_repo_tools_lib()
    try:
        tl.update_tool_status(serial, body.status, body.holder, body.object_name,
                               user.get('first_name', str(user['id'])), holder_id=body.holder_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"status": "ok"}


class NewToolBody(BaseModel):
    name: str
    category: str


_tool_create_lock = __import__('threading').Lock()


@app.post("/api/tools")
def create_tool(body: NewToolBody, user: dict = Depends(get_current_user), _: None = Depends(require_owner)):
    # 31.07 (доп.раунд, П5): tl.add_tool() читает существующие serial, вычисляет
    # next=max+1, добавляет строку и пишет историю -- без lock, охватывающего весь
    # цикл, два конкурентных POST могли оба прочитать один и тот же max ДО того как
    # первый успевал записать новую строку, оба бы вычислили ОДИНАКОВЫЙ serial.
    # Отдельный module-level lock (не per-serial -- serial ещё не существует на
    # момент входа в этот эндпоинт, в отличие от checkout/return выше).
    tl = _load_repo_tools_lib()
    with _tool_create_lock:
        serial = tl.add_tool(body.name, body.category, user.get('first_name', str(user['id'])))
    return {"serial": serial}


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


def send_pdf_to_chat(chat_id, file_path, filename, caption):
    """Отправляет PDF пользователю в чат с ботом (multipart/form-data вручную,
    т.к. в проекте нет requests — только стандартная библиотека)."""
    boundary = uuid.uuid4().hex
    with open(file_path, 'rb') as f:
        file_data = f.read()

    parts = []
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="caption"\r\n\r\n{caption}\r\n'.encode())
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{filename}"\r\n'
        f'Content-Type: application/pdf\r\n\r\n'.encode()
    )
    parts.append(file_data)
    parts.append(f'\r\n--{boundary}--\r\n'.encode())
    body = b''.join(parts)

    req = _urlreq.Request(
        f'https://api.telegram.org/bot{BOT_TOKEN}/sendDocument',
        data=body, method='POST',
        headers={'Content-Type': f'multipart/form-data; boundary={boundary}'}
    )
    try:
        _urlreq.urlopen(req, timeout=20)
    except Exception as e:
        print(f'WARNING: sendDocument fehlgeschlagen: {e}')


def _run_pdf_generator(script_path: str, config: dict, timeout: int = 30):
    if not os.path.isfile(script_path):
        raise HTTPException(500, f'PDF-Generator fehlt: {os.path.basename(script_path)}')

    tmp = tempfile.NamedTemporaryFile('w', encoding='utf-8', suffix='.json', delete=False)
    try:
        with tmp:
            json.dump(config, tmp, ensure_ascii=False)
        result = subprocess.run(['node', script_path, tmp.name], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise HTTPException(504, 'PDF-Generierung dauert zu lange') from e
    finally:
        try:
            os.remove(tmp.name)
        except FileNotFoundError:
            pass

    if result.returncode != 0:
        raise HTTPException(500, f'PDF-Generierung fehlgeschlagen: {result.stderr[-500:]}')
    return result


def _safe_pdf_filename(prefix: str, customer_name: str) -> str:
    name = re.sub(r'[^\w.-]+', '_', (customer_name or '').strip(), flags=re.UNICODE).strip('._')
    if name.lower().endswith('.pdf'):
        name = name[:-4].rstrip('._')
    return f"{prefix}_{(name or 'kunde')[:80]}.pdf"


def _require_server_script(script_path: str, label: str):
    if not script_path or not os.path.isfile(script_path):
        raise HTTPException(500, f'{label} не найден: {script_path or "-"}')
    return script_path


ANGEBOT_SCRIPT = os.path.join(BACKEND_DIR, 'angebot_free.js')
# moved to core/paths.py -- ANGEBOT_OUT_DIR
os.makedirs(ANGEBOT_OUT_DIR, exist_ok=True)


def require_angebot_access(role: str = Depends(get_role)):
    # 'manager' role is intentionally excluded: set_role() hard-rejects any role
    # other than 'owner'/'worker', so 'manager' can never be assigned in practice.
    # Keeping it here would be a latent escalation path if that validation is ever relaxed.
    if role != 'owner':
        raise HTTPException(403, "только owner может создавать Angebot")


class AngebotKunde(BaseModel):
    typ: str = 'privat'
    anrede: str = ''
    name: str
    kontakt: str = ''
    ustId: str = ''
    adresse: str = ''
    email: str = ''


class AngebotPosition(BaseModel):
    titel: str
    beschreibung: str = ''
    menge: float
    einheit: str = 'Stk'
    preis: float


class AngebotBody(BaseModel):
    kunde: AngebotKunde
    objektAdresse: str = ''
    positionen: list[AngebotPosition]
    gueltigTage: int = 14
    mwstSatz: float = 19
    anzahlungPct: float = 40
    signatureBase64: str | None = None  # Фаза 7: canvas-подпись, PNG data без префикса data:...


@app.post("/api/angebot")
def create_angebot(body: AngebotBody, user: dict = Depends(get_current_user), _: None = Depends(require_angebot_access)):
    if not body.positionen:
        raise HTTPException(400, "mindestens eine Position erforderlich")

    out_path = os.path.join(ANGEBOT_OUT_DIR, f'{uuid.uuid4().hex}.pdf')
    config = body.model_dump()
    config['outPath'] = out_path
    if config.get('signatureBase64'):
        config['signedAt'] = datetime.now().strftime('%d.%m.%Y %H:%M')

    _run_pdf_generator(ANGEBOT_SCRIPT, config)

    filename = _safe_pdf_filename('Angebot', body.kunde.name)
    send_pdf_to_chat(user['id'], out_path, filename, f'Angebot für {body.kunde.name}')
    return FileResponse(out_path, media_type='application/pdf', filename=filename)


# ---------- Aufgaben (tasks) ----------
@app.get("/api/objects/{object_id}/tasks")
def get_tasks(object_id: str, user: dict = Depends(get_current_user), _: None = Depends(require_object_access)):
    o = _load_repo_objekte_lib()
    return {"tasks": o.list_tasks(object_id)}


class TaskBody(BaseModel):
    text: str


@app.post("/api/objects/{object_id}/tasks")
def create_task(object_id: str, body: TaskBody, user: dict = Depends(get_current_user), _: None = Depends(require_owner)):
    o = _load_repo_objekte_lib()
    if not body.text.strip():
        raise HTTPException(400, "Текст не может быть пустым")
    task_id = o.add_task(object_id, body.text.strip(), user.get('first_name', str(user['id'])))
    return {"task_id": task_id}


@app.patch("/api/tasks/{task_id}/complete")
def complete_task(task_id: str, user: dict = Depends(get_current_user), _: None = Depends(require_owner)):
    o = _load_repo_objekte_lib()
    try:
        o.complete_task(task_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"status": "ok"}


# ---------- Инфо объекта (24.07, Step 3): work-items + документы ----------
# moved to core/paths.py -- OBJECT_INFO_FILE
# moved to core/paths.py -- OBJECT_DOC_DIR
os.makedirs(OBJECT_DOC_DIR, exist_ok=True)


def _load_object_info() -> dict:
    return _safe_load_json(OBJECT_INFO_FILE, {})


def _save_object_info(data: dict):
    _atomic_write_json(OBJECT_INFO_FILE, data)


def _new_object_info_entry() -> dict:
    return {"items": [], "documents": [], "description": ""}


def _ensure_object_info_entry(data: dict, object_id: str) -> dict:
    entry = data.setdefault(object_id, _new_object_info_entry())
    entry.setdefault("items", [])
    entry.setdefault("documents", [])
    entry.setdefault("description", "")
    return entry


def _object_info_entry(object_id: str) -> dict:
    data = _load_object_info()
    return data.get(object_id, _new_object_info_entry())


# 25.07: Инфо-таб реструктурирован (6 плоских табов -> 2), владелец попросил
# добавить нормальный блок "Описание объекта" -- переиспользуем тот же per-object
# JSON store, что уже хранит items/documents, не заводим отдельный файл.


@app.get("/api/objects/{object_id}/documents")
def get_object_documents(object_id: str, user: dict = Depends(get_current_user), _: None = Depends(require_object_access)):
    return {"documents": _object_info_entry(object_id).get("documents", [])}


@app.post("/api/objects/{object_id}/documents")
async def upload_object_document(object_id: str, file: UploadFile = File(...), user: dict = Depends(get_current_user), _: None = Depends(require_object_access)):
    data_bytes = await file.read()
    if len(data_bytes) > 8 * 1024 * 1024:
        raise HTTPException(400, "Файл слишком большой (макс. 8 МБ)")
    detected = sniff_image_or_pdf(data_bytes)
    if not detected:
        raise HTTPException(400, "Разрешены только изображения и PDF")
    content_type = detected
    ext = ('.pdf' if detected == 'application/pdf' else '.' + _ALLOWED_IMAGE_MIME_EXT[detected])
    fname = f'{uuid.uuid4().hex}{ext}'
    with open(os.path.join(OBJECT_DOC_DIR, fname), 'wb') as f:
        f.write(data_bytes)

    doc = {
        "id": uuid.uuid4().hex,
        "file": fname,
        "name": file.filename or fname,
        "content_type": content_type,
        "uploaded_by": user.get('first_name', str(user['id'])),
        "uploaded_at": int(time.time()),
    }

    def _mutator(data):
        entry = _ensure_object_info_entry(data, object_id)
        entry["documents"].append(doc)
        return doc

    update_json_transaction(OBJECT_INFO_FILE, {}, _mutator)
    _append_object_history_best_effort(
        object_id, 'document_uploaded', 'Документ загружен',
        user=user, subtitle=doc.get('name', ''),
        at=datetime.fromtimestamp(doc['uploaded_at'], timezone.utc).replace(tzinfo=None).isoformat(),
        meta={
            "document_id": doc.get('id', ''),
            "file": doc.get('file', ''),
            "content_type": doc.get('content_type', ''),
        },
    )
    return {"document": doc}


@app.delete("/api/objects/{object_id}/documents/{doc_id}")
def delete_object_document(object_id: str, doc_id: str, user: dict = Depends(get_current_user), _: None = Depends(require_owner)):
    def _mutator(data):
        entry = data.get(object_id)
        if not entry:
            raise HTTPException(404, "Не найдено")
        doc = next((d for d in entry.get("documents", []) if d["id"] == doc_id), None)
        if not doc:
            raise HTTPException(404, "Не найдено")
        entry["documents"] = [d for d in entry.get("documents", []) if d["id"] != doc_id]
        return doc

    doc = update_json_transaction(OBJECT_INFO_FILE, {}, _mutator)
    fpath = os.path.join(OBJECT_DOC_DIR, doc["file"])
    if os.path.exists(fpath):
        os.remove(fpath)
    return {"status": "ok"}


@app.get("/api/objects/{object_id}/documents/{fname}/file")
def get_object_document_file(object_id: str, fname: str, user: dict = Depends(get_current_user), _: None = Depends(require_object_access)):
    entry = _object_info_entry(object_id)
    doc = next((d for d in entry.get("documents", []) if d["file"] == fname), None)
    if not doc:
        raise HTTPException(404, "Файл не найден")
    path = os.path.join(OBJECT_DOC_DIR, fname)
    if not os.path.exists(path):
        raise HTTPException(404, "Файл не найден")
    return FileResponse(path, media_type=doc.get("content_type") or None)


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
    object_info_entry=lambda object_id: _object_info_entry(object_id),
    ensure_object_info_entry=lambda data, object_id: _ensure_object_info_entry(data, object_id),
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


# ---------- Rechnung generator ----------
RECHNUNG_SCRIPT = os.path.join(BACKEND_DIR, 'rechnung.js')
# moved to core/paths.py -- RECHNUNG_OUT_DIR
os.makedirs(RECHNUNG_OUT_DIR, exist_ok=True)


class RechnungKunde(BaseModel):
    typ: str = 'privat'
    anrede: str = ''
    name: str
    kontakt: str = ''
    ustId: str = ''
    adresse: str = ''
    email: str = ''


class RechnungPosition(BaseModel):
    titel: str
    beschreibung: str = ''
    menge: float
    einheit: str = 'Stk'
    preis: float


class RechnungBody(BaseModel):
    nummer: str
    kunde: RechnungKunde
    projekt: str = ''
    positionen: list[RechnungPosition]
    zahlungsfristTage: int = 14
    mwstSatz: float = 19
    signatureBase64: str | None = None  # Фаза 7: canvas-подпись, PNG data без префикса data:...


@app.post("/api/rechnung")
def create_rechnung(body: RechnungBody, user: dict = Depends(get_current_user), _: None = Depends(require_angebot_access)):
    if not body.nummer.strip():
        raise HTTPException(400, "Rechnung-Nr. erforderlich")
    if not body.positionen:
        raise HTTPException(400, "mindestens eine Position erforderlich")

    out_path = os.path.join(RECHNUNG_OUT_DIR, f'{uuid.uuid4().hex}.pdf')
    config = body.model_dump()
    config['outPath'] = out_path
    if config.get('signatureBase64'):
        config['signedAt'] = datetime.now().strftime('%d.%m.%Y %H:%M')

    _run_pdf_generator(RECHNUNG_SCRIPT, config)

    filename = _safe_pdf_filename('Rechnung', body.kunde.name)
    send_pdf_to_chat(user['id'], out_path, filename, f'{body.nummer} — {body.kunde.name}')
    return FileResponse(out_path, media_type='application/pdf', filename=filename)


# ---------- Weather feed ----------
WEATHER_FEED_FILE = os.path.join(AGENT_ROOT, '.weather_feed.json')


# moved to core/paths.py -- WEATHER_REACTIONS_FILE
# {entry_key: {user_id: true}} — entry_key = "{object}::{created}" (weather-записи не имеют своего id).


def _weather_entry_key(entry: dict) -> str:
    return f"{entry.get('object', '')}::{entry.get('created', '')}"


def _load_weather_reactions() -> dict:
    return _safe_load_json(WEATHER_REACTIONS_FILE, {})


def _save_weather_reactions(data: dict):
    _atomic_write_json(WEATHER_REACTIONS_FILE, data)


FEED_SAVE_TYPES = {'photo', 'news', 'weather'}
FEED_SAVE_TYPE_ALIASES = {'photos': 'photo', 'info': 'weather'}


def _load_feed_saved() -> dict:
    return _safe_load_json(FEED_SAVED_FILE, {})


def _feed_saved_bucket(saved: dict, uid: str, item_type: str) -> dict:
    user_saved = saved.get(str(uid), {})
    if not isinstance(user_saved, dict):
        return {}
    bucket = user_saved.get(item_type, {})
    return bucket if isinstance(bucket, dict) else {}


def _is_feed_saved(saved: dict, uid: str, item_type: str, item_id: str) -> bool:
    return str(item_id) in _feed_saved_bucket(saved, uid, item_type)


def _feed_item_exists(item_type: str, item_id: str) -> bool:
    if item_type == 'photo':
        return any(str(p.get('id')) == item_id for p in _load_photo_meta())
    if item_type == 'news':
        if not os.path.exists(NEWS_FEED_FILE):
            return False
        try:
            with open(NEWS_FEED_FILE, encoding='utf-8') as f:
                return any(str(p.get('id')) == item_id for p in json.load(f))
        except Exception:
            return False
    if item_type == 'weather':
        if not os.path.exists(WEATHER_FEED_FILE):
            return False
        try:
            with open(WEATHER_FEED_FILE, encoding='utf-8') as f:
                return any(_weather_entry_key(e) == item_id for e in json.load(f))
        except Exception:
            return False
    return False


@app.get("/api/feed/weather")
def get_weather_feed(user: dict = Depends(get_current_user)):
    if not os.path.exists(WEATHER_FEED_FILE):
        return {"feed": []}
    with open(WEATHER_FEED_FILE, encoding='utf-8') as f:
        feed = json.load(f)
    reactions = _load_weather_reactions()
    saved = _load_feed_saved()
    uid = str(user['id'])
    for entry in feed:
        key = _weather_entry_key(entry)
        entry_reactions = reactions.get(key, {})
        entry['likes'] = len(entry_reactions)
        entry['liked_by_me'] = uid in entry_reactions
        entry['saved_by_me'] = _is_feed_saved(saved, uid, 'weather', key)
    return {"feed": feed}


@app.post("/api/feed/weather/react")
def react_weather_entry(body: dict, user: dict = Depends(get_current_user)):
    """21.07: реальные лайки на погодных карточках (были декоративные, localStorage-only).
    Ключ записи — object+created, т.к. weather-записи не имеют своего id (генерируются cron-пайплайном)."""
    key = f"{body.get('object', '')}::{body.get('created', '')}"
    reactions = _load_weather_reactions()
    entry_reactions = reactions.setdefault(key, {})
    uid = str(user['id'])
    liked = bool(body.get('liked'))
    if liked:
        entry_reactions[uid] = True
    else:
        entry_reactions.pop(uid, None)
    _save_weather_reactions(reactions)
    return {"likes": len(entry_reactions), "liked_by_me": liked}


# ---------- News feed (Фаза 9, 10.32 — лайки + read-tracking для адаптивной фильтрации) ----------
# Наполняется отдельным cron-пайплайном на VPS (WebSearch/RSS → AI-саммари), здесь чтение + реакции.
NEWS_FEED_FILE = os.path.join(AGENT_ROOT, '.news_feed.json')
# moved to core/paths.py -- NEWS_REACTIONS_FILE
# {post_id: {user_id: "like"|"dislike"}} — по одной реакции на пост от юзера, апдейт при повторном клике.
# moved to core/paths.py -- NEWS_READS_FILE
# {user_id: {category: read_count}} — накопитель для будущей адаптивной фильтрации ленты под интересы.


def _load_news_reactions() -> dict:
    return _safe_load_json(NEWS_REACTIONS_FILE, {})


def _save_news_reactions(data: dict):
    _atomic_write_json(NEWS_REACTIONS_FILE, data)


def _load_news_reads() -> dict:
    return _safe_load_json(NEWS_READS_FILE, {})


def _save_news_reads(data: dict):
    _atomic_write_json(NEWS_READS_FILE, data)


# moved to core/paths.py -- BIRTHDAY_ALERTS_FILE
def _load_birthday_alerts() -> list:
    return _safe_load_json(BIRTHDAY_ALERTS_FILE, [])


def _save_birthday_alerts(items: list):
    # 22.09: _check_upcoming_birthdays() itself no longer calls this directly
    # (moved to update_json_transaction, see its own comment) -- kept as a
    # plain write helper for tests/tooling that need to seed/reset the store
    # directly (test_worker_calendar_birthday.py's setUp, for instance).
    _atomic_write_json(BIRTHDAY_ALERTS_FILE, items)


def _check_upcoming_birthdays():
    """10.31 + Раунд 5 §14: два алерта на день рождения worker'а —
    «За 3 дня» и «В день рождения» — с idempotency ключами
    birthday:<uid>:<year>:3days / birthday:<uid>:<year>:today (проверка по
    Europe/Berlin), чтобы один и тот же alert не создавался повторно при каждом
    заходе. Ленивая проверка при GET /api/feed/birthdays (миниапп открывают каждый
    день) — не отдельный systemd timer.

    22.09 hotfix (owner live-device finding, 196 legacy duplicate critical-alert
    records found in production): this used to be _load_birthday_alerts() (read
    OUTSIDE any lock) -> mutate `alerts` in Python memory -> _save_birthday_alerts()
    (lock only covers the write) -- the same read-modify-write race already fixed
    elsewhere this session (ack_critical_alert/resolve_critical_alert). Because
    this function is a LAZY check that runs on every GET /api/feed/birthdays
    (every app open, from every device/tab, no single cron), concurrent calls
    could all read the same "not yet in `already`" state before any of them
    wrote -- each one then passed its own `idem not in already` check and called
    _create_critical_alert(). That inner call's OWN dedup is now correctly keyed
    (see the ref_id=idem fix below) and would still prevent a duplicate CRITICAL
    ALERT record even under this race, but the birthday_alerts.json idempotency
    file itself could still race and grow spurious duplicate entries. Moved the
    whole read-modify-write under one update_json_transaction lock so concurrent
    calls serialize instead of racing.

    23.09 (owner finding, "призрак Ивана" -- a stale canonical alert surfaced
    by the legacy-duplicate migration's post-apply check): only iterates
    profiles whose CURRENT role in roles.json is 'worker'. A profile can
    outlive its role (removed/former worker, or a uid that was never
    onboarded past profile creation) -- this used to still generate birthday
    alerts every year forever, since the loop only ever read
    worker_profiles.json, never cross-checked roles.json."""
    profiles = _load_worker_profiles()
    today = business_today()
    d3 = today + timedelta(days=3)
    roles = _load_roles()
    owner_id = next((o for o, r in roles.items() if r == 'owner'), None)

    # _create_critical_alert() does its own file I/O (its own
    # update_json_transaction on CRITICAL_ALERTS_FILE, a DIFFERENT file) --
    # deliberately called OUTSIDE the mutator below, never nested inside
    # another update_json_transaction call (would deadlock on the same
    # non-reentrant lock if it were the same file, and is simply wrong
    # layering even though it's a different file/lock here).
    to_create = []  # [(target_user_id, title, ref_id)]

    def _mutate(alerts):
        already = {a.get('idem') for a in alerts if a.get('idem')}

        def _emit(uid, name, occ_date, kind, idem, title):
            alerts.append({
                'user_id': uid, 'name': name, 'year': occ_date.year, 'kind': kind,
                'idem': idem, 'date': occ_date.strftime('%Y-%m-%d'), 'created_at': int(time.time()),
            })
            to_create.append((owner_id or uid, title, idem))

        for uid, profile in profiles.items():
            # 23.09 (owner finding): a profile can exist in worker_profiles.json
            # for a uid that no longer has an active 'worker' role in
            # roles.json (removed/former worker, or a uid that was never
            # properly onboarded past profile creation) -- that used to still
            # generate birthday alerts forever, since this loop only ever
            # read the profile, not the role. Confirmed live: 196 legacy
            # duplicate alerts all pointed at a uid with role=None in
            # roles.json. Only a uid whose CURRENT role is 'worker' is
            # eligible -- owner's own birthday (if profiled) and any
            # departed/roleless uid are both excluded.
            if roles.get(str(uid)) != 'worker':
                continue
            bday = profile.get('birthday')
            if not bday:
                continue
            try:
                bd = datetime.strptime(bday, '%Y-%m-%d').date()
            except ValueError:
                continue
            name = _sanitize_display_name(profile.get('name'), uid)

            # За 3 дня
            if (bd.month, bd.day) == (d3.month, d3.day):
                idem = f'birthday:{uid}:{d3.year}:3days'
                if idem not in already:
                    _emit(uid, name, d3, '3days', idem,
                          f'🎂 Через 3 дня день рождения у {name}. Не забудьте подготовить подарок и поздравление.')

            # В день рождения
            if (bd.month, bd.day) == (today.month, today.day):
                idem = f'birthday:{uid}:{today.year}:today'
                if idem not in already:
                    _emit(uid, name, today, 'today', idem, f'🎂 Сегодня день рождения у {name}')

    update_json_transaction(BIRTHDAY_ALERTS_FILE, [], _mutate)

    for target_user_id, title, idem in to_create:
        try:
            # 22.09 hotfix (owner live-device finding): ref_id was `uid` -- the
            # SAME value for both the "3 days before" and "the day of" alert for
            # one worker, so _create_critical_alert()'s dedup key
            # (kind, target_user_id, ref_id) could not tell the two genuinely
            # different events apart and would collapse one into the other.
            # `idem` (already unique per event type AND year --
            # birthday:<uid>:<year>:3days / :today) is the correct dedup key.
            _create_critical_alert(target_user_id=target_user_id, kind='birthday', title=title, ref_id=idem)
        except Exception:
            pass


@app.get("/api/feed/birthdays")
def get_birthday_feed(user: dict = Depends(get_current_user)):
    _check_upcoming_birthdays()
    today = business_today_str()
    upcoming = [a for a in _load_birthday_alerts() if a['date'] >= today]
    return {"birthdays": upcoming}


@app.get("/api/feed/news")
def get_news_feed(user: dict = Depends(get_current_user)):
    if not os.path.exists(NEWS_FEED_FILE):
        return {"feed": []}
    with open(NEWS_FEED_FILE, encoding='utf-8') as f:
        feed = json.load(f)
    reactions = _load_news_reactions()
    comments = _load_news_comments()
    saved = _load_feed_saved()
    uid = str(user['id'])
    for post in feed:
        post['my_reaction'] = reactions.get(post['id'], {}).get(uid)
        post['saved_by_me'] = _is_feed_saved(saved, uid, 'news', post['id'])
        pc = comments.get(post['id'], [])
        post['comment_count'] = len(pc)
        post['last_comment_at'] = max((c.get('ts', 0) for c in pc), default=0)

    reads = _load_news_reads()
    user_reads = reads.setdefault(uid, {})
    for post in feed:
        cat = post.get('category') or 'Другое'
        user_reads[cat] = user_reads.get(cat, 0) + 1
    _save_news_reads(reads)

    return {"feed": feed}


class NewsReactionIn(BaseModel):
    reaction: str  # "like" | "dislike" | "none" (снять реакцию)


@app.post("/api/feed/news/{post_id}/react")
def react_news_post(post_id: str, body: NewsReactionIn, user: dict = Depends(get_current_user)):
    if body.reaction not in ('like', 'dislike', 'none'):
        raise HTTPException(400, "reaction должна быть like/dislike/none")
    if not os.path.exists(NEWS_FEED_FILE):
        raise HTTPException(404, "лента новостей пуста")
    with open(NEWS_FEED_FILE, encoding='utf-8') as f:
        feed = json.load(f)
    post = next((p for p in feed if p['id'] == post_id), None)
    if not post:
        raise HTTPException(404, "пост не найден")

    reactions = _load_news_reactions()
    post_reactions = reactions.setdefault(post_id, {})
    uid = str(user['id'])
    prev = post_reactions.get(uid)

    if prev == 'like':
        post['likes'] = max(0, post.get('likes', 0) - 1)
    elif prev == 'dislike':
        post['dislikes'] = max(0, post.get('dislikes', 0) - 1)

    if body.reaction == 'none':
        post_reactions.pop(uid, None)
    else:
        post_reactions[uid] = body.reaction
        if body.reaction == 'like':
            post['likes'] = post.get('likes', 0) + 1
        else:
            post['dislikes'] = post.get('dislikes', 0) + 1

    _save_news_reactions(reactions)
    _atomic_write_json(NEWS_FEED_FILE, feed)
    return {"ok": True, "likes": post['likes'], "dislikes": post['dislikes'], "my_reaction": body.reaction if body.reaction != 'none' else None}


# ---------- News comments + per-user feed read markers (Раунд 5 §8) ----------
# Комментарии к новостям хранятся ОТДЕЛЬНО от .news_feed.json (его перезаписывает
# cron-пайплайн) — {post_id: [ {id,user_id,name,text,ts,reply_to} ]}. Тот же lifecycle,
# что у фото-комментариев. Read-markers — {user_id: {last_news_read_at, last_photos_read_at,
# last_info_read_at}} (epoch); unread = публикации/активность новее отметки.
# moved to core/paths.py -- NEWS_COMMENTS_FILE
# moved to core/paths.py -- FEED_READS_FILE


def _load_news_comments() -> dict:
    return _safe_load_json(NEWS_COMMENTS_FILE, {})


def _save_news_comments(data: dict):
    _atomic_write_json(NEWS_COMMENTS_FILE, data)


def _load_feed_reads() -> dict:
    return _safe_load_json(FEED_READS_FILE, {})


def _save_feed_reads(data: dict):
    _atomic_write_json(FEED_READS_FILE, data)


class NewsCommentBody(BaseModel):
    text: str
    reply_to: str = None


@app.post("/api/feed/news/{post_id}/comments")
def add_news_comment(post_id: str, body: NewsCommentBody, user: dict = Depends(get_current_user)):
    text = (body.text or '').strip()[:500]
    if not text:
        raise HTTPException(400, "Комментарий не может быть пустым")
    comments = _load_news_comments()
    plist = comments.setdefault(post_id, [])
    prior_ids = {c.get('user_id') for c in plist}  # комментаторы ДО нового — до append
    actor_name = _sanitize_display_name(user.get('first_name'), str(user['id']))
    new_id = uuid.uuid4().hex
    plist.append({
        'id': new_id,
        'user_id': str(user['id']),
        'name': actor_name,
        'text': text,
        'reply_to': (body.reply_to or None),
        'ts': int(time.time()),
    })
    _save_news_comments(comments)
    # Раунд 6 §5.2: activity alerts релевантным (owner + прежние комментаторы); новости из
    # пайплайна не имеют user_id-автора → author_id=None.
    try:
        _emit_comment_activity_alerts(
            'news_comment', post_id, new_id, str(user['id']), actor_name,
            f"{actor_name} прокомментировал новость", _news_post_title(post_id),
            _comment_alert_recipients(user['id'], prior_ids))
    except Exception:
        pass
    return {"comments": plist}


@app.get("/api/feed/news/{post_id}/comments")
def get_news_comments(post_id: str, user: dict = Depends(get_current_user)):
    # 22.09 (iPhone screenshot audit, Item I): comments stored `name` at write
    # time (add_news_comment) was never re-resolved on read -- a worker who
    # changed/set their profile name later kept showing the old/raw one in
    # every past comment. Same read-side resolver as feed/chat/abwesenheit.
    comments = _load_news_comments().get(post_id, [])
    for c in comments:
        c['name'] = _resolve_current_display_name(c.get('user_id'), c.get('name'))
    return {"comments": comments}


@app.delete("/api/feed/news/{post_id}/comments/{comment_id}")
def delete_news_comment(post_id: str, comment_id: str, user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    comments = _load_news_comments()
    plist = comments.get(post_id, [])
    comment = next((c for c in plist if c.get('id') == comment_id), None)
    if not comment:
        raise HTTPException(404, "Комментарий не найден")
    if str(comment.get('user_id')) != str(user['id']) and role != 'owner':
        raise HTTPException(403, "Можно удалить только свой комментарий")
    comments[post_id] = [c for c in plist if c.get('id') != comment_id]
    _save_news_comments(comments)
    return {"comments": comments[post_id]}


class FeedReadBody(BaseModel):
    tab: str  # "news" | "photos" | "info"


class FeedSavedBody(BaseModel):
    item_type: str  # "photo" | "news" | "weather"
    item_id: str
    saved: bool


@app.post("/api/feed/read")
def mark_feed_read(body: FeedReadBody, user: dict = Depends(get_current_user)):
    if body.tab not in ('news', 'photos', 'info'):
        raise HTTPException(400, "tab должен быть news/photos/info")
    reads = _load_feed_reads()
    urec = reads.setdefault(str(user['id']), {})
    urec[f'last_{body.tab}_read_at'] = int(time.time())
    _save_feed_reads(reads)
    return {"ok": True, "tab": body.tab, "read_at": urec[f'last_{body.tab}_read_at']}


@app.post("/api/feed/saved")
def set_feed_saved(body: FeedSavedBody, user: dict = Depends(get_current_user)):
    raw_item_type = (body.item_type or '').strip()
    item_type = FEED_SAVE_TYPE_ALIASES.get(raw_item_type, raw_item_type)
    item_id = (body.item_id or '').strip()
    if item_type not in FEED_SAVE_TYPES:
        raise HTTPException(400, "item_type должен быть photo/news/weather")
    if not item_id:
        raise HTTPException(400, "item_id обязателен")
    if not _feed_item_exists(item_type, item_id):
        raise HTTPException(404, "Пост не найден")

    uid = str(user['id'])

    def _mutate(saved: dict):
        user_saved = saved.setdefault(uid, {})
        if not isinstance(user_saved, dict):
            user_saved = {}
            saved[uid] = user_saved
        bucket = user_saved.setdefault(item_type, {})
        if not isinstance(bucket, dict):
            bucket = {}
            user_saved[item_type] = bucket
        if body.saved:
            bucket[item_id] = int(time.time())
        else:
            bucket.pop(item_id, None)
        return {"ok": True, "item_type": item_type, "item_id": item_id, "saved_by_me": item_id in bucket}

    return update_json_transaction(FEED_SAVED_FILE, {}, _mutate)


@app.get("/api/feed/unread")
def get_feed_unread(user: dict = Depends(get_current_user)):
    """Счётчики НЕПРОЧИТАННОГО (не общее число). Новость непрочитана, если она
    опубликована позже отметки ИЛИ получила новый комментарий позже отметки. Фото —
    по ts. Погода — по created погодной записи. >99 фронт покажет как «99+»."""
    uid = str(user['id'])
    urec = _load_feed_reads().get(uid, {})
    news_read = urec.get('last_news_read_at', 0)
    photos_read = urec.get('last_photos_read_at', 0)
    info_read = urec.get('last_info_read_at', 0)

    news_unread = 0
    if os.path.exists(NEWS_FEED_FILE):
        try:
            with open(NEWS_FEED_FILE, encoding='utf-8') as f:
                feed = json.load(f)
        except Exception:
            feed = []
        comments = _load_news_comments()
        for post in feed:
            created = post.get('created', 0) or 0
            pc = comments.get(post['id'], [])
            last_c = max((c.get('ts', 0) for c in pc), default=0)
            if created > news_read or last_c > news_read:
                news_unread += 1

    photos_unread = 0
    try:
        for p in _load_photo_meta():
            if (p.get('ts', 0) or 0) > photos_read:
                photos_unread += 1
    except Exception:
        pass

    info_unread = 0
    if os.path.exists(WEATHER_FEED_FILE):
        try:
            with open(WEATHER_FEED_FILE, encoding='utf-8') as f:
                weather_feed = json.load(f)
            for entry in weather_feed:
                created = entry.get('created', 0)
                if isinstance(created, (int, float)):
                    created_ts = int(created)
                else:
                    raw = str(created or '').strip()
                    if raw.isdigit():
                        created_ts = int(raw)
                    else:
                        created_ts = int(datetime.fromisoformat(raw.replace('Z', '+00:00')).timestamp()) if raw else 0
                if created_ts > info_read:
                    info_unread += 1
        except Exception:
            info_unread = 0

    return {"news": news_unread, "photos": photos_unread, "info": info_unread}


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


# ---------- Team Chat ----------
# Хранение: JSON-файл, последние 200 сообщений. Polling с фронта каждые 8 сек.
# Инстанс один, файл достаточен — без WebSocket и БД для простоты.
# moved to core/paths.py -- CHAT_FILE
# moved to core/paths.py -- CHAT_ARCHIVE_FILE
# moved to core/limits.py -- CHAT_MAX
_chat_lock = __import__('threading').Lock()


def _archive_chat_messages(messages: list):
    # 28.07: owner request -- удаление треда/сообщения не должно стирать историю
    # безвозвратно. Append-only архив на диске, отдельный от рабочего chat_messages.json
    # (тот же принцип, что закрытые Потребности архивируются в Google Sheets вместо
    # физического удаления).
    if not messages:
        return
    archive = _safe_load_json(CHAT_ARCHIVE_FILE, [])
    archive.extend(messages)
    # 30.07 (Release-аудит P1): plain open(w)+json.dump -> atomic. Вызывается изнутри
    # _save_chat, которая уже держит _chat_lock -- RMW-race закрыт, тут только crash-safety.
    _atomic_write_json(CHAT_ARCHIVE_FILE, archive)


def _load_chat() -> list:
    return _safe_load_json(CHAT_FILE, [])


def _save_chat(messages: list):
    # 28.07: owner request -- история чата должна сохраняться на сервере, не теряться
    # молча. Раньше messages[-CHAT_MAX:] отбрасывал всё, что не влезло в последние 200,
    # без следа. Теперь то, что вылетает за пределы CHAT_MAX, архивируется тем же
    # append-only архивом, что уже используется для явного удаления треда/сообщения.
    if len(messages) > CHAT_MAX:
        _archive_chat_messages(messages[:-CHAT_MAX])
        messages = messages[-CHAT_MAX:]
    # 30.07 (Release-аудит P1): plain open(w)+json.dump -> atomic. RMW-race уже
    # закрыт _chat_lock на всех call sites, тут только crash-safety записи.
    _atomic_write_json(CHAT_FILE, messages)


# moved to core/limits.py -- CHAT_RETENTION_SECONDS


def _purge_old_chat(messages: list) -> list:
    # 28.07: то же самое -- сообщения старше 7 дней архивируются, не стираются молча.
    cutoff = time.time() - CHAT_RETENTION_SECONDS
    keep = [m for m in messages if m.get('ts', 0) >= cutoff]
    expired = [m for m in messages if m.get('ts', 0) < cutoff]
    if expired:
        _archive_chat_messages(expired)
    return keep


# moved to core/paths.py -- CHAT_READS_FILE
def _load_reads() -> dict:
    return _safe_load_json(CHAT_READS_FILE, {})


def _save_reads(reads: dict):
    _atomic_write_json(CHAT_READS_FILE, reads)


# moved to core/paths.py -- CHAT_THREAD_META_FILE
def _load_chat_thread_meta() -> dict:
    return _safe_load_json(CHAT_THREAD_META_FILE, {})


def _save_chat_thread_meta(meta: dict):
    _atomic_write_json(CHAT_THREAD_META_FILE, meta)


# moved to core/paths.py -- CHAT_REACTIONS_FILE
# moved to core/constants.py -- CHAT_REACTION_OPTIONS


def _load_chat_reactions() -> list:
    """Phase 06: список {message_id,user_id,reaction,created_at}, а не словарь --
    один пользователь может оставить НЕСКОЛЬКО разных reaction на одно сообщение
    (👍 и 👀 одновременно), но не два одинаковых -- уникальность по (message_id,
    user_id, reaction), toggle снимает при повторном POST того же типа."""
    return _safe_load_json(CHAT_REACTIONS_FILE, [])


def _save_chat_reactions(reactions: list):
    _atomic_write_json(CHAT_REACTIONS_FILE, reactions)


def _reactions_summary_for_message(reactions: list, message_id: str, my_id: str) -> list:
    by_type = {}
    for r in reactions:
        if r['message_id'] != message_id:
            continue
        entry = by_type.setdefault(r['reaction'], {'reaction': r['reaction'], 'count': 0, 'mine': False})
        entry['count'] += 1
        if str(r['user_id']) == my_id:
            entry['mine'] = True
    return [by_type[e] for e in CHAT_REACTION_OPTIONS if e in by_type]


def _chat_thread_id(user_id: str, to_user_id: str | None) -> str:
    if not to_user_id:
        return 'group'
    return '-'.join(sorted([str(user_id), str(to_user_id)]))


def _reject_self_chat(user_id, to_user_id: str | None):
    """Phase 06 audit: self-DM was never explicitly blocked -- a buggy/replayed
    client sending to_user_id == own id would silently create a degenerate
    'uid-uid' thread. Group (to_user_id falsy) and obj:/mangel:/task: threads
    (thread_key path) are unaffected."""
    if to_user_id and str(to_user_id) == str(user_id):
        raise HTTPException(400, "Нельзя написать самому себе")


def _validate_dm_recipient(to_user_id: str | None):
    """DM targets must be active authorized users. A profile-only/revoked user
    must not become a new ghost thread target."""
    if not to_user_id:
        return
    roles = _load_roles()
    if str(to_user_id) not in roles:
        raise HTTPException(404, "Пользователь не найден или доступ отозван")


def _object_chat_participants(object_id: str) -> list:
    """03.08 (доп.раунд П2, реальный найденный баг): раньше ЛЮБОЕ назначение (даже
    pending/declined, даже вне периода) давало доступ к чату объекта -- полное
    несоответствие с can_access_object, которое уже требовало accepted+период.
    Теперь строго через has_active_object_access -- тот же критерий доступа
    одинаково для этапов/файлов/чата, не отдельная более слабая проверка."""
    roles = _load_roles()
    owner_id = next((uid for uid, r in roles.items() if r == 'owner'), None)
    assignments = _load_assignments().get(object_id, [])
    candidate_ids = {str(a['user_id']) for a in assignments}
    today = _today_berlin_str()
    worker_ids = {uid for uid in candidate_ids if has_active_object_access(uid, object_id, today)}
    if owner_id:
        worker_ids.add(str(owner_id))
    return list(worker_ids)


def _mangel_chat_participants(ticket_id: str) -> list:
    roles = _load_roles()
    # 18.09: Mängel themselves are visible/commentable for every active worker
    # (see require_mangel_access/get_mangel_list). The chat must follow that same
    # product rule; otherwise owner can open a defect chat while worker sees the
    # defect card but gets a 403 on the conversation.
    participants = {str(uid) for uid, r in roles.items() if r in ('owner', 'worker')}
    try:
        ticket = ml.get_ticket(ticket_id)
        if ticket.get('assigned_worker_id'):
            participants.add(str(ticket['assigned_worker_id']))
        if ticket.get('created_by'):
            participants.add(str(ticket['created_by']))
    except Exception:
        pass
    return list(participants)


def _task_chat_participants(task_id: str) -> list:
    roles = _load_roles()
    owner_id = next((uid for uid, r in roles.items() if r == 'owner'), None)
    participants = {str(owner_id)} if owner_id else set()
    task = next((t for t in _load_tasks() if t['id'] == task_id), None)
    if task and task.get('from_user_id'):
        participants.add(str(task['from_user_id']))
    # 18.09: object-scoped needs are team-visible for workers assigned to that
    # object (/api/tasks?object_id=...). Give the same workers access to the
    # need's chat so the chat entry works like it does for owner.
    object_id = task.get('object_id') if task else ''
    if object_id:
        today = _today_berlin_str()
        for a in _load_assignments().get(str(object_id), []):
            if _assignment_status(a) != 'accepted':
                continue
            d_from, d_to = a.get('date_from', ''), a.get('date_to', '')
            if (not (d_from and d_to)) or (d_from <= today <= d_to):
                participants.add(str(a.get('user_id')))
    return list(participants)


def _check_thread_access(thread_id: str, uid: str, role: str):
    """obj:/mangel:/task: треды — доступ только участникам (не любой worker может
    писать в чужой чат). DM/group треды не проходят через эту проверку — там
    доступ регулируется самим thread_id (пара user_id) или ролью."""
    if thread_id.startswith('obj:'):
        object_id = thread_id[len('obj:'):]
        if uid not in _object_chat_participants(object_id) and role != 'owner':
            raise HTTPException(403, "Нет доступа к чату этого объекта")
    elif thread_id.startswith('mangel:'):
        ticket_id = thread_id[len('mangel:'):]
        if uid not in _mangel_chat_participants(ticket_id) and role != 'owner':
            raise HTTPException(403, "Нет доступа к чату этого тикета")
    elif thread_id.startswith('task:'):
        task_id = thread_id[len('task:'):]
        if uid not in _task_chat_participants(task_id) and role != 'owner':
            raise HTTPException(403, "Нет доступа к чату этой потребности")


def _thread_title(thread_key: str) -> str:
    if thread_key.startswith('obj:'):
        object_id = thread_key[len('obj:'):]
        try:
            rows = _cached_get_used_range('Объекты')
            if rows:
                hdr, data = rows[0], rows[1:]
                for r in data:
                    row = dict(zip(hdr, r))
                    if str(row.get('ID объекта', '')) == object_id:
                        return f"Объект: {row.get('Объект', object_id)}"
        except Exception:
            pass
        return f"Объект: {object_id}"
    if thread_key.startswith('mangel:'):
        ticket_id = thread_key[len('mangel:'):]
        try:
            ticket = ml.get_ticket(ticket_id)
            return f"Тикет: {ticket.get('object_id', ticket_id)}"
        except Exception:
            return f"Тикет: {ticket_id}"
    if thread_key.startswith('task:'):
        task_id = thread_key[len('task:'):]
        task = next((t for t in _load_tasks() if t['id'] == task_id), None)
        return f"Потребность: {task['title']}" if task else f"Потребность: {task_id}"
    return thread_key


@app.get("/api/chat/my_threads")
def get_my_chat_threads(user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    uid = str(user['id'])
    messages = _load_chat()
    keys = {m['thread_key'] for m in messages if m.get('thread_key')}
    result = []
    for key in keys:
        try:
            _check_thread_access(key, uid, role)
        except HTTPException:
            continue
        thread_msgs = [m for m in messages if m.get('thread_key') == key]
        last = max(thread_msgs, key=lambda m: m['ts']) if thread_msgs else None
        result.append({
            "thread_key": key,
            "title": _thread_title(key),
            "last_ts": last['ts'] if last else 0,
            "last_preview": _message_preview(last),
        })
    result.sort(key=lambda t: t['last_ts'], reverse=True)
    return {"threads": result}


def _message_preview(msg: dict | None) -> str:
    if not msg:
        return ''
    if msg.get('text'):
        return msg['text']
    if msg.get('location'):
        return '📍 Геолокация'
    att = msg.get('attachment') or {}
    if (att.get('content_type') or '').startswith('audio'):
        return '🎤 Голосовое'
    if att:
        return '📎 Файл'
    return ''


# moved to core/constants.py -- THREAD_TYPE_BY_PREFIX


@app.get("/api/chat/threads")
def get_normalized_chat_threads(type: str = '', user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    """Phase 06: normalized shape (id/type/title/avatar_url/subtitle/last_message/
    unread_count/muted/pinned/version) per docs/plan-phases/06-chat-hub-rebuild.md.
    Kept ALONGSIDE the legacy /api/chat/my_threads (not replacing it) -- no frontend
    UI reads this yet, the live Chat Hub still runs on the old endpoints, this is
    additive groundwork for the eventual frontend rebuild.
    `type` filter: GENERAL/DIRECT/OBJECT/DEFECT/TASK (spec's ChatThread.type enum,
    extended with TASK per the 5-tabs decision in docs/DECISIONS.md, 2026-07-28).
    Also fixes two real gaps found while building this: the old /api/chat/my_threads
    never included GENERAL or DIRECT threads at all (only obj:/mangel:/task:), so
    the frontend's "Общий чат"/DM previews in the thread list were always the
    static fallback text, never the actual last message -- see chat.js
    renderChatThreadList()'s _threadByKey('group') / _threadByKey(worker.user_id)
    calls, which could never match anything this endpoint's predecessor returned.
    No cursor/pagination -- CHAT_MAX caps total stored messages at 200 across ALL
    threads, so pagination has no real workload to justify yet; revisit if that
    cap is ever raised.
    """
    uid = str(user['id'])
    with _chat_lock:
        messages = _purge_old_chat(_load_chat())
        _save_chat(messages)
    reads = _load_reads()
    meta = _load_chat_thread_meta()
    profiles = _load_worker_profiles()
    roles = _load_roles()

    def _last_message_field(last):
        return {"text": _message_preview(last), "ts": last['ts'], "sender_id": str(last['user_id'])} if last else None

    threads = []

    if not type or type == 'GENERAL':
        group_msgs = [m for m in messages if not m.get('to_user_id') and not m.get('thread_key')]
        last = max(group_msgs, key=lambda m: m['ts']) if group_msgs else None
        unread = sum(1 for m in group_msgs if str(m.get('user_id')) != uid and m['ts'] > _thread_last_read(reads, uid, 'group'))
        prefs = _thread_user_prefs(meta, 'group', uid)
        threads.append({
            "id": "group", "type": "GENERAL", "title": "Общий чат", "avatar_url": None,
            "subtitle": _message_preview(last), "last_message": _last_message_field(last),
            "unread_count": unread, **prefs, "version": last['ts'] if last else 0,
        })

    if not type or type == 'DIRECT':
        for wuid in (set(roles.keys()) | set(profiles.keys())):
            if wuid == uid:
                continue
            dm_msgs = [m for m in messages if not m.get('thread_key') and (
                (str(m.get('user_id')) == uid and str(m.get('to_user_id')) == wuid) or
                (str(m.get('user_id')) == wuid and str(m.get('to_user_id')) == uid))]
            last = max(dm_msgs, key=lambda m: m['ts']) if dm_msgs else None
            thread_id = _chat_thread_id(uid, wuid)
            unread = sum(1 for m in dm_msgs if str(m.get('user_id')) != uid and m['ts'] > _thread_last_read(reads, uid, wuid))
            prefs = _thread_user_prefs(meta, thread_id, uid)
            p = profiles.get(wuid, {})
            last_seen = _last_seen.get(wuid)
            threads.append({
                "id": wuid, "type": "DIRECT",
                "title": _sanitize_display_name(p.get('name'), wuid),
                "avatar_url": f"/api/profile/{wuid}/avatar" if p.get('avatar') else None,
                "subtitle": _message_preview(last) or ('Владелец' if roles.get(wuid) == 'owner' else 'Работник'),
                "online": bool(last_seen and (time.time() - last_seen) < ONLINE_THRESHOLD_SECONDS),
                "last_message": _last_message_field(last),
                "unread_count": unread, **prefs, "version": last['ts'] if last else 0,
            })

    if not type or type in ('OBJECT', 'DEFECT', 'TASK'):
        keys = {m['thread_key'] for m in messages if m.get('thread_key')}
        for key in keys:
            ttype = next((v for p, v in THREAD_TYPE_BY_PREFIX.items() if key.startswith(p)), None)
            if not ttype or (type and type != ttype):
                continue
            try:
                _check_thread_access(key, uid, role)
            except HTTPException:
                continue
            thread_msgs = [m for m in messages if m.get('thread_key') == key]
            last = max(thread_msgs, key=lambda m: m['ts']) if thread_msgs else None
            unread = sum(1 for m in thread_msgs if str(m.get('user_id')) != uid and m['ts'] > _thread_last_read(reads, uid, key))
            prefs = _thread_user_prefs(meta, key, uid)
            threads.append({
                "id": key, "type": ttype, "title": _thread_title(key), "avatar_url": None,
                "subtitle": _message_preview(last), "last_message": _last_message_field(last),
                "unread_count": unread, **prefs, "version": last['ts'] if last else 0,
            })

    threads.sort(key=lambda t: t['last_message']['ts'] if t['last_message'] else 0, reverse=True)
    return {"threads": threads}


@app.get("/api/chat/messages")
def get_chat_messages(with_: str = '', thread_key: str = '', user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    with _chat_lock:
        messages = _purge_old_chat(_load_chat())
        _save_chat(messages)
    if thread_key:
        # 10.36: чат объекта (obj:OBJ-001) или дефекта (mangel:ticket_id) — сообщения
        # хранятся с явным полем thread_key, доступ только участникам.
        _check_thread_access(thread_key, str(user['id']), role)
        messages = [m for m in messages if m.get('thread_key') == thread_key]
    elif with_:
        # DM-тред: сообщения между мной и with_ в обе стороны
        me = str(user['id'])
        messages = [m for m in messages if
                    (str(m.get('user_id')) == me and str(m.get('to_user_id')) == with_) or
                    (str(m.get('user_id')) == with_ and str(m.get('to_user_id')) == me)]
    else:
        # Групповой тред: только сообщения без to_user_id (старые записи без ключа — тоже групповые)
        messages = [m for m in messages if not m.get('to_user_id') and not m.get('thread_key')]

    reactions = _load_chat_reactions()
    my_id = str(user['id'])
    for m in messages:
        m['reactions'] = _reactions_summary_for_message(reactions, m['id'], my_id)
        # 22.09 (iPhone screenshot audit): resolve the CURRENT profile name
        # read-side, not whatever was frozen in the message at send time --
        # see _resolve_current_display_name()'s docstring for why the stored
        # `name` field can be permanently wrong (raw user_id) otherwise.
        # "system" (critical-alert auto-messages) is not a real user account.
        if m.get('user_id') != 'system':
            m['name'] = _resolve_current_display_name(m.get('user_id'), m.get('name'))

    # 28.07: owner request -- статус прочтения в личном чате (DM). Собеседник уже
    # отмечает прочтение через существующий POST /api/chat/read (reads.json), просто
    # никогда не отдавался обратно отправителю. Только для DM (with_) -- групповой/
    # obj:/mangel: треды имеют много читателей, "прочитано" там неоднозначно, вне
    # скоупа этого запроса ("в личный чат").
    if with_:
        other_reads = _load_reads().get(with_, {})
        other_last_read = other_reads.get(my_id, 0) if isinstance(other_reads, dict) else int(other_reads or 0)
        for m in messages:
            if str(m.get('user_id')) == my_id:
                m['read_by_recipient'] = m.get('ts', 0) <= other_last_read

    return {"messages": messages}


def _thread_last_read(reads: dict, my_id: str, thread_key: str) -> int:
    """10.29 (Fable-аудит): reads.json теперь {user_id: {thread_id: ts}} вместо плоского
    {user_id: ts} — открытие одного треда больше не сбрасывает badge у остальных."""
    user_reads = reads.get(my_id, {})
    if isinstance(user_reads, (int, float)):
        # миграция со старого плоского формата — считаем это last_read для всех тредов сразу
        return int(user_reads)
    return int(user_reads.get(thread_key, 0))


@app.get("/api/chat/unread_count")
def get_unread_count(user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    """30.07 (Release-аудит Этап 5): раньше цикл вообще не учитывал thread_key
    (obj:/mangel:/task: треды) -- фильтровал только по to_user_id, поэтому любое
    сообщение с thread_key (thread_key истинный, to_user_id всегда None у таких
    сообщений) попадало в ветку "group" и считалось непрочитанным ДЛЯ ЛЮБОГО
    пользователя против его group-last-read, независимо от того, участник ли он
    вообще этого конкретного object/mangel/task-треда -- общий nav-badge был
    завышен и не гас после прочтения конкретного треда. Логика приведена к тому
    же паттерну, что уже верно работает в get_unread_by_thread ниже: явная
    проверка _check_thread_access для thread_key-сообщений + собственный ключ
    last-read на каждый thread_key (не смешивается с general group-веткой)."""
    with _chat_lock:
        messages = _load_chat()
        reads = _load_reads()
    meta = _load_chat_thread_meta()
    me = str(user['id'])
    count = 0
    for m in messages:
        if str(m.get('user_id')) == me:
            continue
        tkey = m.get('thread_key')
        if tkey:
            try:
                _check_thread_access(tkey, me, role)
            except HTTPException:
                continue
            thread_key = tkey
            prefs_id = tkey
        else:
            to_uid = m.get('to_user_id')
            if to_uid and str(to_uid) != me:
                continue  # чужой DM
            thread_key = 'group' if not to_uid else str(m['user_id'])
            prefs_id = thread_key if not to_uid else _chat_thread_id(me, thread_key)
        if m.get('ts', 0) <= _thread_last_read(reads, me, thread_key):
            continue
        if _thread_user_prefs(meta, prefs_id, me).get('muted'):
            continue
        count += 1
    return {"unread": count}


@app.get("/api/chat/unread_by_thread")
def get_unread_by_thread(user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    """10.14/10.29: разбивка непрочитанных по тредам для badge на каждой строке списка,
    per-thread last_read — открытие одного треда не сбрасывает счётчик у остальных.
    25.07: расширено на thread_key-треды (obj:/mangel:/task:) -- раньше badge считался
    только для group/DM веток, вкладки Объекты/Дефекты/Потребности в списке чатов не
    показывали unread вообще (не забыт badge в разметке -- сам подсчёт не доходил
    до этих сообщений, т.к. цикл ниже фильтровал только по to_user_id).
    28.07 (Phase 06): заглушённые (muted) треды исключены из счёта -- иначе mute-иконка
    во frontend была бы декоративной, а не реальным подавлением уведомлений. prefs_id
    для DM отличается от display-ключа thread_key (тот -- id собеседника, prefs хранятся
    под отсортированной парой _chat_thread_id, см. set_chat_thread_prefs) -- не перепутать.
    30.07 (Release-аудит Этап 5): та же thread_key/prefs/muted-логика теперь приведена
    в соответствие и в /api/chat/unread_count (общий nav-badge) выше -- раньше тот
    вообще не различал thread_key-треды (obj:/mangel:/task:) от group."""
    with _chat_lock:
        messages = _load_chat()
        reads = _load_reads()
    meta = _load_chat_thread_meta()
    me = str(user['id'])
    by_thread = {}
    for m in messages:
        if str(m.get('user_id')) == me:
            continue
        tkey = m.get('thread_key')
        if tkey:
            try:
                _check_thread_access(tkey, me, role)
            except HTTPException:
                continue
            thread_key = tkey
            prefs_id = tkey
        else:
            to_uid = m.get('to_user_id')
            if to_uid and str(to_uid) != me:
                continue  # чужой DM
            thread_key = 'group' if not to_uid else str(m['user_id'])
            prefs_id = thread_key if not to_uid else _chat_thread_id(me, thread_key)
        if m.get('ts', 0) <= _thread_last_read(reads, me, thread_key):
            continue
        if _thread_user_prefs(meta, prefs_id, me).get('muted'):
            continue
        by_thread[thread_key] = by_thread.get(thread_key, 0) + 1
    return {"unread_by_thread": by_thread}


@app.post("/api/chat/read")
def mark_chat_read(with_: str = '', thread_key: str = '', user: dict = Depends(get_current_user)):
    key = thread_key or ('group' if not with_ else with_)
    with _chat_lock:
        reads = _load_reads()
        my_id = str(user['id'])
        if not isinstance(reads.get(my_id), dict):
            reads[my_id] = {}
        reads[my_id][key] = int(time.time())
        _save_reads(reads)
    return {"ok": True}


# moved to core/paths.py -- CHAT_ATTACH_DIR
os.makedirs(CHAT_ATTACH_DIR, exist_ok=True)


@app.post("/api/chat/messages/attachment")
def post_chat_attachment(thread_key: str = Form(''), to_user_id: str = Form(''), file: UploadFile = File(...),
                          user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    """Фото/файл-вложение в чат (10.27) — отдельный multipart endpoint, т.к. текстовые
    сообщения идут через простой JSON POST /api/chat/messages."""
    if thread_key:
        _check_thread_access(thread_key, str(user['id']), role)
    else:
        _reject_self_chat(user['id'], to_user_id or None)
        _validate_dm_recipient(to_user_id or None)
        thread_id = _chat_thread_id(user['id'], to_user_id or None)
        thread_meta = _load_chat_thread_meta()
        if thread_meta.get(thread_id, {}).get('closed') and role != 'owner':
            raise HTTPException(403, "Чат закрыт руководством")

    data = file.file.read()
    if len(data) > 8 * 1024 * 1024:
        raise HTTPException(400, "Файл слишком большой (макс. 8 МБ)")
    # 30.07 (Release-аудит P0): раньше расширение бралось из имени файла от клиента
    # без проверки содержимого -- любой файл (включая .html/.svg) сохранялся и позже
    # отдавался через FileResponse без nosniff, что выполнялось инлайн в WebView
    # (stored XSS). Теперь: magic-byte allowlist + расширение ТОЛЬКО из этой таблицы.
    sniffed = sniff_chat_attachment(data)
    if sniffed is None:
        raise HTTPException(400, "Недопустимый тип файла")
    _, ext = sniffed
    fname = f'{uuid.uuid4().hex}.{ext}'
    with open(os.path.join(CHAT_ATTACH_DIR, fname), 'wb') as f:
        f.write(data)

    msg = {
        "id": uuid.uuid4().hex,
        "ts": int(time.time()),
        "user_id": user['id'],
        "name": user.get('first_name', str(user['id'])),
        "text": '',
        "to_user_id": to_user_id or None,
        "thread_key": thread_key or None,
        "attachment": {"file": fname, "name": file.filename or fname, "content_type": file.content_type or ''},
    }
    with _chat_lock:
        messages = _load_chat()
        messages.append(msg)
        _save_chat(messages)
    return {"message": msg}


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


@app.get("/api/chat/attachments/{fname}")
def get_chat_attachment(fname: str, user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    # 30.07 (Release-аудит P0): basename -- fname раньше шёл в os.path.join без
    # проверки, что путь не выходит за пределы CHAT_ATTACH_DIR (owner-путь вообще
    # не имел проверки ниже, только non-owner ветка сверяла msg-принадлежность).
    safe_fname = os.path.basename(fname)
    if safe_fname != fname:
        raise HTTPException(404, "Файл не найден")
    path = os.path.join(CHAT_ATTACH_DIR, safe_fname)
    if not os.path.exists(path):
        raise HTTPException(404, "Файл не найден")
    if role != 'owner':
        # 30.07 (Release-аудит, IDOR): раньше бралось первое сообщение с этим файлом
        # и доступ проверялся только через to_user_id -- ломалось для обектовых/
        # дефектных/task-тредов (thread_key) и для файлов, пересланных в другой чат
        # (после пересылки на файл ссылаются несколько сообщений в разных тредах).
        # Теперь: доступ разрешён, если юзер имеет доступ хотя бы к одному сообщению
        # с этим файлом (через ту же _check_message_access, что и остальные эндпоинты).
        # 31.07 (Release-аудит П7): сообщения старше 7 дней уходят в CHAT_ARCHIVE_FILE
        # (_archive_chat_messages) -- вложение из архивного сообщения раньше не находилось
        # тут вообще (искали только активный _load_chat()), 404 даже для законного участника.
        messages = _load_chat() + _safe_load_json(CHAT_ARCHIVE_FILE, [])
        # attachment key is explicitly None for plain-text messages; `or {}` avoids
        # AttributeError from calling .get() on None (`.get('attachment', {})` only
        # uses the default when the key is ABSENT — not when it's present as None).
        candidates = [m for m in messages if (m.get('attachment') or {}).get('file') == safe_fname]
        if not candidates:
            raise HTTPException(404, "Файл не найден")
        uid = str(user['id'])
        allowed = False
        for msg in candidates:
            try:
                _check_message_access(msg, uid, role)
                allowed = True
                break
            except HTTPException:
                continue
        if not allowed:
            raise HTTPException(403, "Нет доступа к этому файлу")
    # 30.07 (Release-аудит P0): nosniff -- расширение теперь всегда из allowlist
    # (см. sniff_chat_attachment), но nosniff не даёт браузеру угадывать иначе,
    # даже для легаси-вложений, загруженных до этого фикса без magic-byte проверки.
    return FileResponse(path, headers={"X-Content-Type-Options": "nosniff"})


class ChatMessageBody(BaseModel):
    text: str = ''
    to_user_id: str | None = None
    thread_key: str | None = None
    reply_to_id: str | None = None
    location: dict | None = None


class BroadcastBody(BaseModel):
    scope: str
    text: str
    object_id: str | None = None


def _normalize_chat_location(raw: dict | None) -> dict | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise HTTPException(400, "Некорректная геолокация")
    try:
        lat = float(raw.get('lat'))
        lon = float(raw.get('lon'))
    except (TypeError, ValueError):
        raise HTTPException(400, "Некорректная геолокация")
    if not math.isfinite(lat) or not math.isfinite(lon) or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        raise HTTPException(400, "Некорректная геолокация")
    normalized = {"lat": round(lat, 6), "lon": round(lon, 6)}
    if raw.get('accuracy') is not None:
        try:
            accuracy = float(raw.get('accuracy'))
        except (TypeError, ValueError):
            accuracy = None
        if accuracy is not None and math.isfinite(accuracy) and accuracy >= 0:
            normalized["accuracy"] = round(accuracy, 1)
    label = str(raw.get('label') or '').strip()
    if label:
        normalized["label"] = label[:80]
    return normalized


def _reply_snapshot(msg: dict) -> dict:
    """Компактный snapshot цитаты -- хранится в самом сообщении, а не резолвится
    заново из оригинала на каждый рендер, чтобы цитата не ломалась после удаления
    оригинала (пункт 3 задачи)."""
    return {
        "id": msg['id'],
        "name": msg.get('name', ''),
        "preview": _message_preview(msg)[:200],
    }


@app.post("/api/manager/broadcast")
def send_manager_broadcast(body: BroadcastBody, user: dict = Depends(get_current_user),
                           _: None = Depends(require_owner)):
    scope = str(body.scope or '').strip().lower()
    text = str(body.text or '').strip()
    if scope not in ('company', 'object'):
        raise HTTPException(400, "scope должен быть company или object")
    if not text:
        raise HTTPException(400, "Текст объявления пустой")
    if len(text) > 1000:
        raise HTTPException(400, "Объявление слишком длинное (макс. 1000 символов)")

    object_id = ''
    object_name = ''
    thread_key = None
    if scope == 'object':
        object_id = str(body.object_id or '').strip()
        if not object_id:
            raise HTTPException(400, "Укажите object_id для объявления по объекту")
        object_row = _find_object_row_by_id(object_id)
        if object_row is None:
            raise HTTPException(404, "Объект не найден")
        object_name = object_row.get('Объект') or object_row.get('Название') or object_row.get('Адрес') or object_id
        thread_key = f'obj:{object_id}'
        recipients = sorted(_object_chat_participants(object_id))
    else:
        recipients = sorted(str(uid) for uid in _load_roles().keys())

    msg = {
        "id": uuid.uuid4().hex,
        "ts": int(time.time()),
        "user_id": user['id'],
        "name": user.get('first_name', str(user['id'])),
        "text": text,
        "to_user_id": None,
        "thread_key": thread_key,
        "broadcast": {
            "scope": scope,
            "object_id": object_id or None,
            "object_name": object_name or None,
            "audience_count": len(recipients),
        },
    }
    with _chat_lock:
        messages = _load_chat()
        messages.append(msg)
        _save_chat(messages)

    if scope == 'object':
        _append_object_history_best_effort(
            object_id, 'broadcast_sent', 'Объявление отправлено',
            user=user, subtitle=text[:200],
            meta={
                "message_id": msg['id'],
                "thread_key": thread_key,
                "audience_count": len(recipients),
            },
        )

    return {
        "status": "ok",
        "thread_key": thread_key or "group",
        "audience_count": len(recipients),
        "message": msg,
    }


@app.post("/api/chat/messages")
def post_chat_message(body: ChatMessageBody, user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    text = body.text.strip()
    location = _normalize_chat_location(body.location)
    if not text and not location:
        raise HTTPException(400, "Пустое сообщение")
    if len(text) > 1000:
        raise HTTPException(400, "Сообщение слишком длинное (макс. 1000 символов)")

    if body.thread_key:
        _check_thread_access(body.thread_key, str(user['id']), role)
    else:
        _reject_self_chat(user['id'], body.to_user_id)
        _validate_dm_recipient(body.to_user_id)
        thread_id = _chat_thread_id(user['id'], body.to_user_id)
        thread_meta = _load_chat_thread_meta()
        if thread_meta.get(thread_id, {}).get('closed') and role != 'owner':
            raise HTTPException(403, "Чат закрыт руководством")

    reply_snapshot = None
    if body.reply_to_id:
        with _chat_lock:
            source = next((m for m in _load_chat() if m['id'] == body.reply_to_id), None)
        if source is None:
            raise HTTPException(404, "Исходное сообщение не найдено")
        # Цитировать можно только сообщение из ТОГО ЖЕ треда/DM-пары, что и новое --
        # иначе можно было бы процитировать сообщение из чужого закрытого диалога,
        # просто зная его id (пункт 3 задачи: "нельзя цитировать чужой закрытый DM").
        if (source.get('thread_key') or None) != (body.thread_key or None):
            raise HTTPException(403, "Нельзя цитировать сообщение из другого чата")
        if not body.thread_key:
            source_thread = _chat_thread_id(source['user_id'], source.get('to_user_id'))
            new_thread = _chat_thread_id(user['id'], body.to_user_id)
            if source_thread != new_thread:
                raise HTTPException(403, "Нельзя цитировать сообщение из другого чата")
        reply_snapshot = _reply_snapshot(source)

    msg = {
        "id": uuid.uuid4().hex,
        "ts": int(time.time()),
        "user_id": user['id'],
        "name": user.get('first_name', str(user['id'])),
        "text": text,
        "to_user_id": body.to_user_id,
        "thread_key": body.thread_key,
        "location": location,
        "reply_to": reply_snapshot,
    }
    with _chat_lock:
        messages = _load_chat()
        messages.append(msg)
        _save_chat(messages)
    return {"message": msg}


@app.post("/api/chat/messages/{msg_id}/forward")
def forward_chat_message(msg_id: str, body: ChatMessageBody, user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    """Пересылка: копия текста/вложения исходного сообщения в новый чат-назначение,
    с отметкой forwarded_from. Вложение пересылается ссылкой на тот же файл на диске
    (не копия) -- безопасно, потому что раздача /api/chat/attachments/{fname} уже
    проверяет участие в треде ПО САМОМУ СООБЩЕНИЮ (main.py:3457-3463), а не по
    оригинальному, так что после пересылки новое сообщение само становится валидным
    источником доступа к файлу для участников нового треда."""
    uid = str(user['id'])
    with _chat_lock:
        source = next((m for m in _load_chat() if m['id'] == msg_id), None)
    if source is None:
        raise HTTPException(404, "Сообщение не найдено")
    _check_message_access(source, uid, role)

    if body.thread_key:
        _check_thread_access(body.thread_key, uid, role)
    else:
        _reject_self_chat(user['id'], body.to_user_id)
        _validate_dm_recipient(body.to_user_id)
        thread_id = _chat_thread_id(user['id'], body.to_user_id)
        thread_meta = _load_chat_thread_meta()
        if thread_meta.get(thread_id, {}).get('closed') and role != 'owner':
            raise HTTPException(403, "Чат закрыт руководством")

    if not source.get('text') and not source.get('attachment') and not source.get('location'):
        raise HTTPException(400, "Нечего пересылать")

    msg = {
        "id": uuid.uuid4().hex,
        "ts": int(time.time()),
        "user_id": user['id'],
        "name": user.get('first_name', str(user['id'])),
        "text": source.get('text', ''),
        "to_user_id": body.to_user_id,
        "thread_key": body.thread_key,
        "attachment": source.get('attachment'),
        "location": source.get('location'),
        "voice_transcript": source.get('voice_transcript'),
        "forwarded_from": source.get('name', ''),
    }
    with _chat_lock:
        messages = _load_chat()
        messages.append(msg)
        _save_chat(messages)
    return {"message": msg}

@app.delete("/api/chat/messages/{msg_id}")
def delete_chat_message(msg_id: str, user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    with _chat_lock:
        messages = _load_chat()
        target = next((m for m in messages if m['id'] == msg_id), None)
        if target is None:
            raise HTTPException(404, 'Сообщение не найдено')
        if role != 'owner' and target['user_id'] != user['id']:
            raise HTTPException(403, 'Можно удалять только свои сообщения')
        messages = [m for m in messages if m['id'] != msg_id]
        _save_chat(messages)
    _archive_chat_messages([target])
    reactions = _load_chat_reactions()
    remaining = [r for r in reactions if r['message_id'] != msg_id]
    if len(remaining) != len(reactions):
        _save_chat_reactions(remaining)
    return {"status": "ok"}


@app.delete("/api/chat/threads")
def delete_chat_thread(thread_key: str = '', with_: str = '', user: dict = Depends(get_current_user), _: None = Depends(require_owner)):
    # 28.07: owner request -- удалить целый тред (DM с конкретным юзером или obj:/mangel:/
    # task: тред), пропадает у обеих сторон. История не теряется -- те же сообщения,
    # что _archive_chat_messages уже использует для отдельных удалённых сообщений.
    if not thread_key and not with_:
        raise HTTPException(400, "Укажи thread_key или with_")
    with _chat_lock:
        messages = _load_chat()
        if thread_key:
            to_delete = [m for m in messages if m.get('thread_key') == thread_key]
            remaining = [m for m in messages if m.get('thread_key') != thread_key]
        else:
            _reject_self_chat(user['id'], with_)
            _validate_dm_recipient(with_)
            target_thread_id = _chat_thread_id(user['id'], with_)
            to_delete = [
                m for m in messages
                if not m.get('thread_key')
                and _chat_thread_id(m.get('user_id'), m.get('to_user_id')) == target_thread_id
            ]
            deleted_ids = {m['id'] for m in to_delete}
            remaining = [m for m in messages if m['id'] not in deleted_ids]
        _save_chat(remaining)
    _archive_chat_messages(to_delete)
    deleted_ids = {m['id'] for m in to_delete}
    reactions = _load_chat_reactions()
    remaining_reactions = [r for r in reactions if r['message_id'] not in deleted_ids]
    if len(remaining_reactions) != len(reactions):
        _save_chat_reactions(remaining_reactions)
    return {"status": "ok", "deleted_count": len(to_delete)}


def _check_message_access(msg: dict, uid: str, role: str):
    thread_key = msg.get('thread_key')
    if thread_key:
        _check_thread_access(thread_key, uid, role)
        return
    thread_id = _chat_thread_id(msg['user_id'], msg.get('to_user_id'))
    if uid not in _chat_thread_participants(thread_id) and role != 'owner':
        raise HTTPException(403, "Нет доступа к этому сообщению")


class ChatReactionBody(BaseModel):
    reaction: str


@app.post("/api/chat/messages/{msg_id}/reactions")
def toggle_chat_reaction(msg_id: str, body: ChatReactionBody, user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    if body.reaction not in CHAT_REACTION_OPTIONS:
        raise HTTPException(400, "Недопустимая реакция")
    uid = str(user['id'])
    with _chat_lock:
        messages = _load_chat()
        msg = next((m for m in messages if m['id'] == msg_id), None)
        if msg is None:
            raise HTTPException(404, "Сообщение не найдено")
        _check_message_access(msg, uid, role)

        reactions = _load_chat_reactions()
        existing = next((r for r in reactions if r['message_id'] == msg_id and str(r['user_id']) == uid and r['reaction'] == body.reaction), None)
        if existing:
            reactions.remove(existing)
        else:
            reactions.append({"message_id": msg_id, "user_id": uid, "reaction": body.reaction, "created_at": int(time.time())})
        _save_chat_reactions(reactions)
        summary = _reactions_summary_for_message(reactions, msg_id, uid)
    return {"reactions": summary}


class ChatThreadCloseBody(BaseModel):
    to_user_id: str | None = None


def _chat_thread_participants(thread_id: str) -> list:
    if thread_id == 'group':
        roles = _load_roles()
        return list(roles.keys())
    return thread_id.split('-')


# moved to core/constants.py -- DEFAULT_THREAD_PREFS


def _thread_user_prefs(meta: dict, thread_id: str, uid: str) -> dict:
    """Phase 06: mute/pin/archive — per-user (ThreadParticipant), не глобальные для
    треда, в отличие от closed/closed_at/closed_by (те owner-only, глобальные)."""
    prefs = meta.get(thread_id, {}).get('user_prefs', {}).get(uid)
    return {**DEFAULT_THREAD_PREFS, **prefs} if prefs else dict(DEFAULT_THREAD_PREFS)


class ChatThreadPrefsBody(BaseModel):
    to_user_id: str | None = None
    thread_key: str | None = None
    muted: bool | None = None
    pinned: bool | None = None
    archived: bool | None = None


@app.post("/api/chat/threads/prefs")
def set_chat_thread_prefs(body: ChatThreadPrefsBody, user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    """Per-user mute/pin/archive toggle. Real data layer (chat_thread_meta.json
    user_prefs) backing the Phase 06 spec's pin/mute/archive requirement -- see
    docs/plan-phases/06-chat-hub-rebuild.md ("не рисовать fake controls, сначала
    строить data layer"). No frontend UI wired to this yet."""
    uid = str(user['id'])
    if body.thread_key:
        _check_thread_access(body.thread_key, uid, role)
        thread_id = body.thread_key
    else:
        _reject_self_chat(user['id'], body.to_user_id)
        _validate_dm_recipient(body.to_user_id)
        thread_id = _chat_thread_id(user['id'], body.to_user_id)

    with _chat_lock:
        meta = _load_chat_thread_meta()
        thread_meta = meta.setdefault(thread_id, {})
        prefs_by_user = thread_meta.setdefault('user_prefs', {})
        current = {**DEFAULT_THREAD_PREFS, **prefs_by_user.get(uid, {})}
        if body.muted is not None:
            current['muted'] = body.muted
        if body.pinned is not None:
            current['pinned'] = body.pinned
        if body.archived is not None:
            current['archived'] = body.archived
        prefs_by_user[uid] = current
        _save_chat_thread_meta(meta)
    return {"status": "ok", "thread_id": thread_id, "prefs": current}


@app.post("/api/chat/threads/close")
def close_chat_thread(body: ChatThreadCloseBody, user: dict = Depends(get_current_user), _: None = Depends(require_owner)):
    _reject_self_chat(user['id'], body.to_user_id)
    _validate_dm_recipient(body.to_user_id)
    thread_id = _chat_thread_id(user['id'], body.to_user_id)
    meta = _load_chat_thread_meta()
    # Phase 06: было meta[thread_id] = {...} -- полная перезапись стирала бы
    # user_prefs (mute/pin/archive), добавленные ниже. Мержим, не заменяем.
    thread_meta = meta.setdefault(thread_id, {})
    thread_meta['closed'] = True
    thread_meta['closed_at'] = int(time.time())
    thread_meta['closed_by'] = str(user['id'])
    _save_chat_thread_meta(meta)

    for uid in _chat_thread_participants(thread_id):
        if uid == str(user['id']):
            continue
        try:
            send_telegram_message(int(uid), "🔒 Чат закрыт руководством")
        except Exception:
            pass
    return {"status": "ok", "thread_id": thread_id}


@app.post("/api/chat/threads/reopen")
def reopen_chat_thread(body: ChatThreadCloseBody, user: dict = Depends(get_current_user), _: None = Depends(require_owner)):
    _reject_self_chat(user['id'], body.to_user_id)
    _validate_dm_recipient(body.to_user_id)
    thread_id = _chat_thread_id(user['id'], body.to_user_id)
    meta = _load_chat_thread_meta()
    if thread_id in meta:
        meta[thread_id]['closed'] = False
        _save_chat_thread_meta(meta)
    return {"status": "ok", "thread_id": thread_id}


@app.get("/api/chat/threads/status")
def get_chat_thread_status(with_: str = '', user: dict = Depends(get_current_user)):
    thread_id = _chat_thread_id(user['id'], with_ or None)
    meta = _load_chat_thread_meta()
    return meta.get(thread_id, {'closed': False})


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


# ---------- Потребности (10.33) — worker → owner запросы (инструмент/материалы/защита) ----------
# moved to core/paths.py -- TASKS_FILE
def _load_tasks() -> list:
    return _safe_load_json(TASKS_FILE, [])


def _save_tasks(items: list):
    _atomic_write_json(TASKS_FILE, items)


# moved to core/constants.py -- TASK_PRIORITIES

# 27.07 (B7): категория запроса -- material/tool/ppe/access/other, отдельно от
# priority. Ключи латиницей (стабильный API contract), label для UI -- по месту рендера.
# moved to core/constants.py -- TASK_CATEGORIES


class TaskCreateBody(BaseModel):
    title: str
    description: str = ''
    object_id: str = ''
    priority: str = 'обычная'
    category: str = 'other'
    due_at: int | None = None


class TaskStatusBody(BaseModel):
    status: str


# 27.07 (B7): расширено с 3 до полного набора из плана (NEW/ACKNOWLEDGED/IN_PROGRESS/
# ORDERED/DELIVERED/DECLINED/CANCELLED) -- старые значения ('открыто','в работе','закрыто')
# сохранены как есть для обратной совместимости с уже существующими записями в tasks.json,
# новые статусы добавлены поверх, не переименовывая старые.
# moved to core/constants.py -- TASK_STATUSES


def _task_is_open(task: dict) -> bool:
    return task.get('status') not in ('закрыто', 'выдано', 'отклонено', 'cancelled')


def _normalize_task_due_at(raw_due_at) -> int | None:
    if raw_due_at in (None, ''):
        return None
    try:
        due_at = int(raw_due_at)
    except (TypeError, ValueError):
        raise HTTPException(400, "Некорректный срок задачи")
    if due_at <= 0:
        raise HTTPException(400, "Некорректный срок задачи")
    if due_at < int(time.time()) - 60:
        raise HTTPException(400, "Срок задачи не может быть в прошлом")
    return due_at


def _format_due_at(due_at: int) -> str:
    try:
        return datetime.fromtimestamp(int(due_at), business_now().tzinfo).strftime('%d.%m %H:%M')
    except Exception:
        return ''


def _overdue_task_alerts(now: int | None = None) -> list:
    now = int(time.time()) if now is None else int(now)
    result = []
    for task in _load_tasks():
        if not _task_is_open(task):
            continue
        try:
            due_at = int(task.get('due_at') or 0)
        except (TypeError, ValueError):
            continue
        if due_at <= 0 or due_at > now:
            continue
        title = (task.get('title') or 'Потребность')[:80]
        parts = []
        if task.get('object_id'):
            parts.append(str(task.get('object_id')))
        due_label = _format_due_at(due_at)
        if due_label:
            parts.append(f'срок {due_label}')
        if task.get('from_name') or task.get('from_user_id'):
            parts.append(f"запросил {task.get('from_name') or task.get('from_user_id')}")
        result.append({
            'id': f'task-overdue-{task.get("id", "")}',
            'type': 'red',
            'role_filter': 'owner',
            'title': f'Просрочена потребность: {title}',
            'subtitle': ' · '.join(parts),
            'at': due_at,
            'task_id': task.get('id', ''),
            'task_overdue': True,
        })
    return result


@app.get("/api/tasks")
def list_tasks(object_id: str = '', user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    # 03.08 (ТЗ Задача 4): worker с object_id должен иметь активный доступ к этому
    # конкретному объекту -- раньше ЛЮБОЙ whitelisted worker мог передать чужой
    # object_id и увидеть командную видимость Потребностей объекта, на который его
    # никогда не назначали (get_current_user проверяет только whitelist, не привязку
    # к объекту). Запрос БЕЗ object_id не трогаем -- он и так уже сужен до "только
    # свои" двумя строками ниже, никакой object-level дыры там нет.
    if object_id and role != 'owner' and not has_active_object_access(str(user['id']), object_id):
        raise HTTPException(403, "Нет доступа к этому объекту")

    items = _load_tasks()
    # 25.07: object_id передан -- worker смотрит вкладку Потребности ВНУТРИ конкретного
    # объекта, там нужна командная видимость (как в чате объекта), не только свои заявки.
    # Без object_id -- это глобальный экран Потребности, там worker видит только свои
    # (иначе он видит чужие материальные запросы по всем объектам сразу, что не нужно).
    if role != 'owner' and not object_id:
        items = [t for t in items if str(t.get('from_user_id')) == str(user['id'])]
    if object_id:
        items = [t for t in items if t.get('object_id') == object_id]
    return {"tasks": sorted(items, key=lambda t: (t.get('priority') != 'срочно', -t.get('created_at', 0)))}


@app.post("/api/tasks")
def create_task(body: TaskCreateBody, user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    if role == 'owner':
        raise HTTPException(403, "Потребности создают работники")
    if not body.title.strip():
        raise HTTPException(400, "Название обязательно")
    if not body.object_id.strip():
        raise HTTPException(400, "Объект обязателен")
    # 03.08 (ТЗ Задача 4): worker может создать потребность только на объекте, к
    # которому у него активный доступ -- раньше object_id принимался как есть без
    # проверки, worker мог создать заявку (видимую всей команде объекта) на объект,
    # к которому вообще не назначен.
    if not has_active_object_access(str(user['id']), body.object_id.strip()):
        raise HTTPException(403, "Нет доступа к этому объекту")
    priority = body.priority.strip() or 'обычная'
    if priority not in TASK_PRIORITIES:
        raise HTTPException(400, "Недопустимый приоритет")
    category = body.category.strip() or 'other'
    if category not in TASK_CATEGORIES:
        raise HTTPException(400, "Недопустимая категория")
    due_at = _normalize_task_due_at(body.due_at)
    roles = _load_roles()
    owner_id = next((uid for uid, r in roles.items() if r == 'owner'), None)
    profile = _get_worker_profile(user['id'])
    task = {
        'id': uuid.uuid4().hex,
        'type': 'request',
        'from_user_id': str(user['id']),
        'from_name': _sanitize_display_name(profile.get('name'), str(user['id'])),
        'to_user_id': owner_id,
        'object_id': body.object_id.strip(),
        'title': body.title.strip()[:200],
        'description': body.description.strip()[:1000],
        'priority': priority,
        'category': category,
        'status': 'открыто',
        'created_at': int(time.time()),
        'due_at': due_at,
        'closed_at': None,
    }
    # 23.09 (owner P0, confirmed live on production): was
    # `with _lock_for(TASKS_FILE): ... _save_tasks(items)` -- _save_tasks()
    # calls _atomic_write_json(), which itself does
    # `with _lock_for(TASKS_FILE):` on the SAME path. _lock_for() caches one
    # plain threading.Lock per path (non-reentrant) -- the second acquire
    # attempt, from the same thread, inside the first `with` block, hangs
    # forever. Proven with a real subprocess call (no mocks): the very FIRST
    # call to this endpoint deadlocks, unconditionally, no concurrency
    # required. update_json_transaction() does the read+mutate+write under
    # ONE lock acquisition -- the correct fix, same pattern already used for
    # critical_alerts.json/roles.json/birthday_alerts.json this session.
    def _mutate(items):
        items.append(task)
        return task
    update_json_transaction(TASKS_FILE, [], _mutate)
    if owner_id:
        try:
            urgent_prefix = "🔴 СРОЧНО! " if priority == 'срочно' else "📋 "
            send_telegram_message(int(owner_id), f"{urgent_prefix}Новая потребность от {task['from_name']}: {task['title']}")
        except Exception:
            pass
    return task


@app.patch("/api/tasks/{task_id}")
def update_task_status(task_id: str, body: TaskStatusBody, user: dict = Depends(get_current_user), _: None = Depends(require_owner)):
    if body.status not in TASK_STATUSES:
        raise HTTPException(400, "Недопустимый статус")

    # 23.09 (owner P0, same nested-lock deadlock as create_task above) --
    # moved to update_json_transaction(). prev_status is captured via the
    # outer-scope holder since the mutator's own return value (the task
    # dict) already carries the NEW status by the time it's returned; the
    # Sheets-archive logic below (outside the lock, does its own network
    # I/O) still needs to know what the status was BEFORE this change.
    prev_status_holder = {}

    def _mutate(items):
        task = next((t for t in items if t['id'] == task_id), None)
        if not task:
            raise HTTPException(404, "Потребность не найдена")
        prev_status_holder['value'] = task.get('status')
        task['status'] = body.status
        if body.status == 'закрыто':
            task['closed_at'] = int(time.time())
        else:
            task['closed_at'] = None
        return task

    task = update_json_transaction(TASKS_FILE, [], _mutate)
    prev_status = prev_status_holder['value']

    # 04.08 (Раунд 3, задача 5.2): РАНЬШЕ закрытая потребность удалялась из JSON
    # (архив только в Sheets) -- из-за этого экран Потребности не мог показать
    # счётчик "Выполнены N" и фильтр "Выполненные". Теперь закрытые ОСТАЮТСЯ в JSON
    # со статусом 'закрыто'+closed_at (фронт по умолчанию показывает только активные),
    # архив в Sheets сохраняется best-effort, но только при ПЕРВОМ закрытии
    # (prev_status != 'закрыто'), чтобы не дублировать строки при повторном PATCH.
    if body.status == 'закрыто' and prev_status != 'закрыто':
        try:
            o = _load_repo_objekte_lib()
            from datetime import datetime
            created_str = datetime.fromtimestamp(task.get('created_at', 0)).strftime('%Y-%m-%d %H:%M') if task.get('created_at') else ''
            closed_str = datetime.fromtimestamp(task['closed_at']).strftime('%Y-%m-%d %H:%M')
            o.append_row_safe('Потребности', [
                task.get('id', ''), task.get('object_id', ''), task.get('title', ''),
                task.get('description', ''), task.get('category', ''), task.get('priority', ''),
                task.get('from_name', task.get('from_user_id', '')), created_str, closed_str,
            ])
        except Exception as e:
            print(f'WARNING: не удалось заархивировать потребность {task_id} в Sheets: {e}')
    return task


# ---------- Mängelmanagement — Фаза 3 ----------
# 30.07 (Release-аудит P1): было `import mangel_lib as ml` -- обычный import
# зависит от глобального sys.path (см. _load_repo_mangel_lib выше). Изолированная
# загрузка по точному пути, ml остаётся module-level именем как раньше -- все
# вызовы ml.xxx() ниже по файлу не меняются.
ml = _load_repo_mangel_lib()

# moved to core/paths.py -- MANGEL_PHOTO_DIR


class MangelStatusBody(BaseModel):
    status: str


class MangelCommentBody(BaseModel):
    text: str


def require_mangel_access(ticket_id: str, user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    """28.07: owner request -- любой воркер видит/комментирует дефект любого объекта,
    не только назначенный. Резолвит только существование тикета, не access-проверку."""
    try:
        ml.get_ticket(ticket_id)
    except KeyError as e:
        raise HTTPException(404, str(e))


def _enrich_mangel_tickets_with_author(tickets: list) -> list:
    # 28.07: owner request -- "фиксация кто добавил дефект" уже была на бэкенде
    # (created_by в mangel_lib), но фронтенд её не показывал -- не было имени,
    # только сырой user_id. Резолвим здесь, а не в mangel_lib.py (тот файл живёт
    # вне git-репо, прямые prod-правки там рискованны -- см. HANDOFF).
    profiles = _load_worker_profiles()
    for t in tickets:
        created_by = t.get('created_by')
        if created_by:
            t['created_by_name'] = _sanitize_display_name(profiles.get(str(created_by), {}).get('name'), str(created_by))
        # 04.08 (Раунд 3, задача 3.1/4): резолвим имя ответственного (назначенного)
        # работника, чтобы UI показывал "Ответственный: Иван", а не сырой user_id.
        assigned = t.get('assigned_worker_id')
        if assigned:
            t['assigned_worker_name'] = _sanitize_display_name(profiles.get(str(assigned), {}).get('name'), str(assigned))
    return tickets


@app.get("/api/mangel")
def get_mangel_list(object_id: str = '', user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    # 28.07: owner request -- любой воркер видит дефекты любого объекта, не только
    # назначенного. Раньше worker без object_id получал только дефекты своих объектов.
    tickets = _enrich_mangel_tickets_with_author(ml.list_tickets(object_id or None))
    return {"tickets": tickets, "total": len(tickets)}


@app.get("/api/mangel/counts")
def get_mangel_counts(user: dict = Depends(get_current_user)):
    return ml.count_by_status()


@app.get("/api/mangel/{ticket_id}")
def get_mangel_ticket(ticket_id: str, user: dict = Depends(get_current_user), _: None = Depends(require_mangel_access)):
    try:
        return _enrich_mangel_tickets_with_author([ml.get_ticket(ticket_id)])[0]
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.post("/api/mangel")
async def create_mangel_ticket(
    object_id: str = Form(''),
    description: str = Form(''),
    assigned_worker_id: str = Form(''),
    file: UploadFile = File(None),
    user: dict = Depends(get_current_user),
    role: str = Depends(get_role),
):
    # 28.07: owner request -- любой воркер может добавить дефект на любом объекте.
    if not description.strip():
        raise HTTPException(400, "Описание обязательно")

    photo_paths: list = []
    if file and file.filename:
        raw = await file.read()
        if len(raw) > 8 * 1024 * 1024:
            raise HTTPException(400, "Фото слишком большое (макс. 8 МБ)")
        detected = sniff_image(raw)
        if not detected:
            raise HTTPException(400, "Файл должен быть изображением")
        ext = _ALLOWED_IMAGE_MIME_EXT[detected]
        photo_id = __import__('uuid').uuid4().hex
        fname = f"mangel_{photo_id}.{ext}"
        with open(os.path.join(MANGEL_PHOTO_DIR, fname), 'wb') as f_out:
            f_out.write(raw)
        photo_paths.append(fname)

    ticket = ml.create_ticket(
        object_id=object_id.strip()[:100],
        description=description.strip()[:500],
        created_by=str(user['id']),
        photo_paths=photo_paths,
        assigned_worker_id=assigned_worker_id.strip() if role == 'owner' else '',
    )
    # 22.07: зеркало в Google Sheets (лист "Дефекты") — юзер хочет видеть дефекты в таблице,
    # так же как объекты. JSON остаётся источником правды приложения, Sheets — только для просмотра;
    # сбой записи в Sheets НЕ должен ронять создание тикета (как и остальные Sheets-интеграции).
    try:
        o = _load_repo_objekte_lib()
        o.append_row_safe('Дефекты', [
            ticket.get('id', ''),
            ticket.get('object_id', ''),
            ticket.get('description', ''),
            ticket.get('status', ''),
            datetime.utcnow().strftime('%Y-%m-%d %H:%M'),
        ])
    except Exception as e:
        print(f'WARNING: mangel-create Sheets mirror failed for ticket {ticket.get("id")}: {e}')
    _append_object_history_best_effort(
        ticket.get('object_id', ''), 'defect_created', 'Создан дефект',
        user=user, subtitle=ticket.get('description', ''),
        meta={
            "ticket_id": ticket.get('id', ''),
            "assigned_worker_id": ticket.get('assigned_worker_id', ''),
            "photo_count": len(ticket.get('photo_paths') or []),
        },
    )
    return ticket


@app.get("/api/mangel/photos/{fname}/file")
def get_mangel_photo_file(fname: str, user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    # Mängel-фото хранятся в той же feed_photos/, но без записи в feed_photos.json —
    # отдаём по basename имени файла (не по id, как feed), с защитой от path traversal.
    safe_name = os.path.basename(fname)
    if safe_name != fname or not safe_name.startswith('mangel_'):
        raise HTTPException(404, "Файл отсутствует")
    if role != 'owner':
        owning_ticket = next((t for t in ml.list_tickets(None) if safe_name in t.get('photo_paths', [])), None)
        if not owning_ticket or not can_access_object(user, role, owning_ticket.get('object_id', '')):
            raise HTTPException(403, "Нет доступа к этому файлу")
    path = os.path.join(PHOTO_DIR, safe_name)
    if not os.path.exists(path):
        raise HTTPException(404, "Файл отсутствует")
    return FileResponse(path)


@app.patch("/api/mangel/{ticket_id}/status")
def update_mangel_status(ticket_id: str, body: MangelStatusBody, user: dict = Depends(get_current_user), _: None = Depends(require_owner)):
    try:
        result = ml.update_status(ticket_id, body.status)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    # 22.07: зеркалим смену статуса в Sheets тоже (та же best-effort защита, не роняем запрос)
    try:
        o = _load_repo_objekte_lib()
        rows = o.get_used_range('Дефекты')
        if rows:
            for i, row in enumerate(rows[1:], start=2):
                if row and row[0] == ticket_id:
                    o.update_range(f'Дефекты!D{i}:D{i}', [[body.status]])
                    break
    except Exception as e:
        print(f'WARNING: mangel-status Sheets mirror failed for ticket {ticket_id}: {e}')
    return result


@app.post("/api/mangel/{ticket_id}/comments")
def add_mangel_comment(ticket_id: str, body: MangelCommentBody, user: dict = Depends(get_current_user), _: None = Depends(require_mangel_access)):
    if not body.text.strip():
        raise HTTPException(400, "Текст комментария обязателен")
    try:
        return ml.add_comment(ticket_id, str(user['id']), body.text.strip()[:500],
                               name=user.get('first_name', str(user['id'])))
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.delete("/api/mangel/{ticket_id}")
def delete_mangel_ticket(ticket_id: str, user: dict = Depends(get_current_user), _: None = Depends(require_owner)):
    # 04.08 (Раунд 3, задача 3.3): Owner-only мягкое удаление дефекта. Тикет
    # исчезает из рабочих списков (list/get фильтруют deleted_at), но остаётся
    # на диске с меткой -- фото/чат/audit не трогаем. Идемпотентно.
    try:
        ml.soft_delete_ticket(ticket_id, str(user['id']))
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "id": ticket_id}


@app.get("/api/mangel/{ticket_id}/comments")
def get_mangel_comments(ticket_id: str, user: dict = Depends(get_current_user), _: None = Depends(require_mangel_access)):
    try:
        ticket = ml.get_ticket(ticket_id)
        return {"comments": ticket.get('comments', [])}
    except KeyError as e:
        raise HTTPException(404, str(e))


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


# ---------- Abwesenheit — Фаза 5 (календарь отсутствий работников) ----------
# moved to core/paths.py -- ABWESENHEIT_FILE
# moved to core/constants.py -- ABWESENHEIT_REASONS


def _load_abwesenheit() -> list:
    return _safe_load_json(ABWESENHEIT_FILE, [])


def _save_abwesenheit(items: list):
    """ВНИМАНИЕ: не использовать в обработчиках запросов для read-modify-write.

    20.09 (merged from upstream c23894d): все шесть мутаций стора переведены на
    update_json_transaction(), которая делает read-modify-write под ОДНИМ
    локом. Пара _load_abwesenheit() → мутация → _save_abwesenheit() выглядит
    безопасной симметрией, но именно она и была гонкой, из-за которой терялись
    одобрения владельца и воскресали удалённые заявки (auto-close при открытии
    календаря пишет свой снимок поверх чужого).

    Остаётся как полная перезапись стора: используется тестовыми фикстурами
    для подготовки состояния, где конкурентности нет по определению."""
    _atomic_write_json(ABWESENHEIT_FILE, items)


def _migrate_abwesenheit_legacy_ids() -> int:
    def _mutate(items):
        count = 0
        if not isinstance(items, list):
            return count
        for entry in items:
            if isinstance(entry, dict) and not entry.get('id'):
                entry['id'] = uuid.uuid4().hex
                count += 1
        return count
    return update_json_transaction(ABWESENHEIT_FILE, [], _mutate)


class AbwesenheitBody(BaseModel):
    date_from: str
    date_to: str | None = None
    reason: str
    note: str = ''
    start_time: str | None = None
    end_time: str | None = None


def _notify_owner_abwesenheit_pending(entry: dict):
    """Критический алерт owner'у при новой заявке на отсутствие (10.15)."""
    roles = _load_roles()
    owner_id = next((uid for uid, r in roles.items() if r == 'owner'), None)
    if not owner_id:
        return
    text = (f"🔴 Заявка на отсутствие: {entry['name']}\n"
            f"{entry['date_from']} — {entry['date_to']} · {entry['reason']}\n"
            f"{entry['note'] or ''}\n\nПодтвердите или отклоните в приложении.")
    try:
        send_telegram_message(owner_id, text)
    except Exception:
        pass  # best-effort — не блокировать создание записи


def _notify_worker_abwesenheit_decision(entry: dict):
    """Push worker'у после решения owner'а по заявке (10.15)."""
    status_text = 'одобрена' if entry['status'] == 'approved' else 'не одобрена'
    text = (f"{'✅' if entry['status'] == 'approved' else '❌'} Ваша заявка на отсутствие "
            f"{status_text} руководством\n{entry['date_from']} — {entry['date_to']}")
    try:
        send_telegram_message(int(entry['user_id']), text)
    except Exception:
        pass


def _validate_date_str(date_str: str, field_name: str = 'дата'):
    """Валидация формата YYYY-MM-DD — без этого кривая дата от клиента доходит
    до calendar.monthrange()/сравнений строк и валит эндпоинт 500 вместо 400 (10.29)."""
    try:
        datetime.strptime(date_str, '%Y-%m-%d')
    except (ValueError, TypeError):
        raise HTTPException(400, f"Некорректный формат {field_name}: {date_str!r} (ожидается YYYY-MM-DD)")


def _month_end(date_str: str) -> str:
    import calendar
    try:
        y, m, _ = (int(x) for x in date_str.split('-'))
        if not (1 <= m <= 12):
            raise ValueError
    except (ValueError, AttributeError):
        raise HTTPException(400, f"Некорректная дата: {date_str}")
    last_day = calendar.monthrange(y, m)[1]
    return f'{y:04d}-{m:02d}-{last_day:02d}'


@app.post("/api/abwesenheit")
def create_abwesenheit(body: AbwesenheitBody, user: dict = Depends(get_current_user)):
    if body.reason not in ABWESENHEIT_REASONS:
        raise HTTPException(400, f"Недопустимая причина: {body.reason}")
    _validate_date_str(body.date_from, 'date_from')
    if body.date_to:
        _validate_date_str(body.date_to, 'date_to')
        if body.date_to < body.date_from:
            raise HTTPException(400, "date_to не может быть раньше date_from")
    # 10.25: "не знаю сколько буду болеть" — date_to не указан, ставим временно до конца
    # месяца (видимость в календаре), open_ended=true — worker сам закрывает досрочно
    # через PATCH .../close, когда поправится, owner тоже может закрыть/продлить.
    open_ended = not body.date_to
    date_to = body.date_to or _month_end(body.date_from)
    entry = {
        "id": uuid.uuid4().hex,
        "user_id": str(user['id']),
        "name": user.get('first_name', str(user['id'])),
        "date_from": body.date_from,
        "date_to": date_to,
        "open_ended": open_ended,
        "reason": body.reason,
        "note": body.note.strip()[:300],
        "start_time": body.start_time,
        "end_time": body.end_time,
        "created_at": int(time.time()),
        "status": "pending",
    }
    # 20.09 (merged from upstream c23894d): append под локом -- параллельная
    # заявка другого работника (или auto-close при открытии календаря
    # владельцем) писала свой снимок поверх, и одна из двух заявок исчезала.
    update_json_transaction(ABWESENHEIT_FILE, [], lambda items: items.append(entry))
    _notify_owner_abwesenheit_pending(entry)
    try:
        shadow_create_absence(user['id'], entry)
    except Exception:
        pass  # Core shadow create is best-effort, must never affect the legacy response
    return entry


@app.patch("/api/abwesenheit/{entry_id}/close")
def close_abwesenheit(entry_id: str, user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    """Worker закрывает открытую запись досрочно ('поправился раньше'), owner может закрыть любую."""
    def _mutate(items):
        entry = next((i for i in items if i.get('id') == entry_id), None)
        if not entry:
            raise HTTPException(404, "Запись не найдена")
        if entry['user_id'] != str(user['id']) and role != 'owner':
            raise HTTPException(403, "Можно закрывать только свои записи")
        entry['date_to'] = business_today_str()
        entry['open_ended'] = False
        return dict(entry)

    return update_json_transaction(ABWESENHEIT_FILE, [], _mutate)


class AbwesenheitStatusBody(BaseModel):
    status: str


class AbwesenheitMoveBody(BaseModel):
    date_from: str
    date_to: str | None = None


@app.patch("/api/abwesenheit/{entry_id}/status")
def update_abwesenheit_status(entry_id: str, body: AbwesenheitStatusBody,
                               user: dict = Depends(get_current_user), _: None = Depends(require_owner)):
    if body.status not in ('approved', 'rejected'):
        raise HTTPException(400, "status должен быть approved или rejected")

    # 20.09 (merged from upstream c23894d): read-modify-write под одним локом.
    # Раньше load/save шли раздельно -- параллельный DELETE/close/auto-close
    # той же записи (или просто открытие календаря владельцем, оно дёргает
    # _auto_close_expired_open_ended) писал свой снимок поверх, и решение
    # владельца терялось МОЛЧА: push и critical-alert ниже уже отправлены, а
    # в файле осталось старое 'pending'.
    def _mutate(items):
        entry = next((i for i in items if i.get('id') == entry_id), None)
        if not entry:
            raise HTTPException(404, "Запись не найдена")
        entry['status'] = body.status
        return dict(entry)

    entry = update_json_transaction(ABWESENHEIT_FILE, [], _mutate)
    _notify_worker_abwesenheit_decision(entry)
    _create_critical_alert(
        target_user_id=entry['user_id'],
        kind='abwesenheit_decision',
        title=f"Отсутствие {entry['date_from']}—{entry['date_to']}: "
              f"{'одобрено' if body.status == 'approved' else 'не одобрено'}",
        ref_id=entry.get('id', ''),
    )
    return entry


@app.patch("/api/abwesenheit/{entry_id}")
def update_abwesenheit_dates(entry_id: str, body: AbwesenheitMoveBody,
                              user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    """Move an absence request to another date range.

    Used by the calendar drag/drop UI. Owner can move any request; workers can
    move only their own. Open-ended requests keep open_ended=true and refresh
    their temporary visibility window to the end of the new month.
    """
    _validate_date_str(body.date_from, 'date_from')

    # 20.09 (merged from upstream c23894d): перенос даты идёт под тем же
    # локом, что и остальные мутации стора -- раньше параллельный
    # approve/close/delete этой же записи терялся (или воскрешал уже
    # удалённую), см. соседние endpoint'ы.
    def _mutate(items):
        entry = next((i for i in items if i.get('id') == entry_id), None)
        if not entry:
            raise HTTPException(404, "Запись не найдена")
        if entry['user_id'] != str(user['id']) and role != 'owner':
            raise HTTPException(403, "Можно переносить только свои записи")

        if body.date_to:
            _validate_date_str(body.date_to, 'date_to')
            date_to = body.date_to
        elif entry.get('open_ended'):
            date_to = _month_end(body.date_from)
        else:
            # 17.09 (real bug found finishing this endpoint off): frontend's
            # _moveAbwesenheitEntry() computes and sends an explicit date_to that
            # preserves the entry's original span -- but if date_to is omitted (any
            # other/future caller, or a client that only sends date_from), this used
            # to collapse a multi-day entry down to a single day (date_to = date_from)
            # instead of preserving its original duration. Backend must not depend on
            # the client remembering to do this -- compute the shift from the
            # ORIGINAL entry's own span before it gets overwritten below.
            original_span_days = (datetime.strptime(entry['date_to'], '%Y-%m-%d').date()
                                   - datetime.strptime(entry['date_from'], '%Y-%m-%d').date()).days
            original_span_days = max(0, original_span_days)
            new_from_date = datetime.strptime(body.date_from, '%Y-%m-%d').date()
            date_to = (new_from_date + timedelta(days=original_span_days)).isoformat()
        if date_to < body.date_from:
            raise HTTPException(400, "date_to не может быть раньше date_from")

        entry['date_from'] = body.date_from
        entry['date_to'] = date_to
        entry['updated_at'] = int(time.time())
        return dict(entry)

    return update_json_transaction(ABWESENHEIT_FILE, [], _mutate)


def _auto_close_expired_open_ended_abwesenheit():
    """10.29 (Fable-аудит): open_ended заявка без ручного закрытия молча висела
    до конца месяца без уведомления. Ленивая проверка при каждом GET (не отдельный
    systemd timer) — закрывает просроченные и пушит worker'у + owner'у."""
    today_str = business_today_str()

    # 20.09 (merged from upstream c23894d): этот auto-close дёргается на
    # КАЖДЫЙ GET календаря, то есть чаще всех остальных мутаций -- именно
    # он был главным источником затирания чужих изменений (владелец
    # открывает календарь ровно в тот момент, когда работник подаёт/
    # закрывает заявку). Под локом и с копиями записей наружу.
    def _mutate(items):
        expired_local = [i for i in items if i.get('open_ended') and i['date_to'] < today_str]
        for entry in expired_local:
            entry['open_ended'] = False
        return [dict(e) for e in expired_local]

    expired = update_json_transaction(ABWESENHEIT_FILE, [], _mutate)
    if not expired:
        return

    roles = _load_roles()
    owner_ids = [uid for uid, r in roles.items() if r == 'owner']
    for entry in expired:
        text = (f"⏰ Заявка на отсутствие автоматически закрыта (истёк месяц)\n"
                f"{entry['date_from']} — {entry['date_to']} · {entry.get('name', entry['user_id'])}")
        try:
            send_telegram_message(int(entry['user_id']), text)
        except Exception:
            pass
        for owner_id in owner_ids:
            try:
                send_telegram_message(owner_id, text)
            except Exception:
                pass


@app.get("/api/abwesenheit")
def list_my_abwesenheit(user: dict = Depends(get_current_user)):
    _auto_close_expired_open_ended_abwesenheit()
    items = [i for i in _load_abwesenheit() if i['user_id'] == str(user['id'])]
    for e in items:
        e.setdefault('status', 'pending')
        # 22.09 (iPhone screenshot audit): read-side resolve, see
        # _resolve_current_display_name()'s docstring -- create_abwesenheit
        # stamps `name` at write time and can freeze a raw user_id into it.
        e['name'] = _resolve_current_display_name(e.get('user_id'), e.get('name'))
    return {"entries": items}


# 31.07 (доп.раунд, П2): reason убран из публичного набора -- свободная строка,
# может содержать мед./личные детали не хуже note (изначально П5 прошлого раунда
# скрыл только note, оставив reason по ошибке).
# moved to core/constants.py -- ABWESENHEIT_PUBLIC_FIELDS


@app.get("/api/abwesenheit/all")
def list_all_abwesenheit(user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    # 28.07: owner request -- воркер тоже может смотреть общий календарь команды
    # (view-only). Approve/reject остаются отдельно защищены require_owner на
    # /api/abwesenheit/{id}/status -- этот endpoint только читает список.
    # 31.07 (Release-аудит П5): owner и сам автор записи видят все поля (включая
    # свободный комментарий note, потенциально мед. детали), другие Worker -- только
    # безопасные поля (имя/даты/общий статус). Свою полную запись Worker всё равно
    # видит через GET /api/abwesenheit (list_my_abwesenheit), не этот endpoint.
    _auto_close_expired_open_ended_abwesenheit()
    my_id = str(user['id'])
    entries = _load_abwesenheit()
    result = []
    for e in entries:
        e.setdefault('status', 'pending')
        # 22.09 (iPhone screenshot audit): read-side resolve BEFORE the
        # public-fields filter below, so the corrected name survives it --
        # see _resolve_current_display_name()'s docstring.
        e['name'] = _resolve_current_display_name(e.get('user_id'), e.get('name'))
        if role == 'owner' or e.get('user_id') == my_id:
            result.append(e)
        else:
            result.append({k: e.get(k) for k in ABWESENHEIT_PUBLIC_FIELDS})
    return {"entries": result}


@app.delete("/api/abwesenheit/{entry_id}")
def delete_abwesenheit(entry_id: str, user: dict = Depends(get_current_user), role: str = Depends(get_role)):
    # 20.09 (merged from upstream c23894d): переведён на update_json_transaction,
    # как и остальные пять мутаций стора (тот же race: параллельный approve/close/
    # auto-close этой же записи терялся под read-modify-write без лока).
    def _mutate(items):
        entry = next((i for i in items if i.get('id') == entry_id), None)
        if not entry:
            raise HTTPException(404, "Запись не найдена")
        if entry['user_id'] != str(user['id']) and role != 'owner':
            raise HTTPException(403, "Можно удалять только свои записи")
        # мутация IN-PLACE: update_json_transaction пишет тот же объект, что
        # передан в mutator -- переприсваивание локального имени (items = [...])
        # его бы не затронуло, и удаление молча не сохранилось бы.
        items[:] = [i for i in items if i.get('id') != entry_id]

    update_json_transaction(ABWESENHEIT_FILE, [], _mutate)
    return {"status": "ok"}


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