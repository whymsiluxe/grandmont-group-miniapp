"""Auth core — Phase A step 6/6: canonical get_current_user()/roles/sessions.

This is now the SINGLE SOURCE OF TRUTH for authentication and role lookup --
main.py imports these names back (`from .core.permissions import
get_current_user, get_role, require_owner, ...`), it does not define its own
copies. Dependency direction is core/* -> main.py, never the reverse (no
routes/* or core/* module may import from main.py).

Revision note (25.09): an earlier pass at this same step deliberately left
get_current_user()/get_role()/require_owner()/_load_roles()/
_notify_owner_new_user() in main.py, reasoning that 15 test files do
`patch.object(backend, '_load_roles', return_value=...)` then call
`backend.get_role(user=user)` directly, and moving the chain would make
those patches silently no-op (the business_now()/business_today()
name-resolution trap). On review that was the wrong fix for a real-split
goal: it treated the test suite's patch targets as fixed architecture
instead of updating them to match the new canonical location. The correct
fix (this revision) is: move the real implementation here, and update the
15 test files' patch targets from `patch.object(backend, '_load_roles', ...)`
to `patch.object(permissions, '_load_roles', ...)` (they already import
`backend.core.permissions as permissions` or equivalent) -- same tests,
same assertions, only the patch target changed to where the code actually
lives now. main.py's own `_load_roles`/`get_role`/etc. names are now thin
re-exports of this module's, kept for any caller still spelling
`backend._load_roles(...)` as a plain call (not a patch target).

has_active_object_access()/can_access_object()/require_object_access() STILL
stay in main.py -- unrelated reason, they depend on _load_assignments()/
_assignment_status() (objects-domain state, not auth), see main.py's own
comment at their definition.
"""
import base64
import hashlib
import hmac
import time
from contextvars import ContextVar

from fastapi import Header, HTTPException, Depends
from pydantic import BaseModel

try:
    from .limits import INIT_DATA_MAX_AGE, SESSION_TOKEN_MAX_AGE, NOTIFIED_USERS_TTL
    from .paths import ROLES_FILE, NOTIFIED_USERS_FILE
    from .storage import _safe_load_json, _atomic_write_json
    from .telegram import BOT_TOKEN, BOT_TOKEN_OLD, send_telegram_message
except ImportError:
    from limits import INIT_DATA_MAX_AGE, SESSION_TOKEN_MAX_AGE, NOTIFIED_USERS_TTL  # noqa: E402
    from paths import ROLES_FILE, NOTIFIED_USERS_FILE  # noqa: E402
    from storage import _safe_load_json, _atomic_write_json  # noqa: E402
    from telegram import BOT_TOKEN, BOT_TOKEN_OLD, send_telegram_message  # noqa: E402

from urllib.parse import parse_qsl
import json


_auth_audit_context: ContextVar[dict | None] = ContextVar('grandmont_group_auth_audit_context', default=None)

# 24.07: online-статус для чата -- in-memory, не персистентный на диск.
# Обновляется на каждый authenticated-запрос (get_current_user), не отдельный
# heartbeat-эндпоинт. Переживает не рестарт сервиса -- приемлемо для
# присутствия-индикатора, не для чего-то critical.
_last_seen: dict = {}


def _secret_key(bot_token: str = BOT_TOKEN) -> bytes:
    """Default stays the old BOT_TOKEN -- existing callers (main.py's
    re-export, tests/test_access_control.py's backend._secret_key()) that
    call this with zero args are unaffected by the migration below."""
    return hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()


def validate_init_data(init_data: str) -> dict:
    """HMAC-валидация Telegram WebApp initData.
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

    Bot token migration -- FINAL CUTOVER (2026-10-04): BOT_TOKEN is now the
    NEW bot (@GrandMont_bot), checked first. BOT_TOKEN_OLD (retired
    @promonta_bot) is still accepted as a fallback ONLY so any Mini App
    instance still holding an old-bot-signed initData (e.g. a stale cached
    page) keeps working through the migration window -- see
    core/telegram.py's BOT_TOKEN_OLD docstring for the removal window.
    """
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        raise HTTPException(401, "initData: malformed")
    received_hash = parsed.pop('hash', None)
    if not received_hash:
        raise HTTPException(401, "initData: no hash")

    auth_date = parsed.get('auth_date')
    try:
        if not auth_date:
            raise HTTPException(401, "initData: expired")
        age = time.time() - int(auth_date)
        if age > INIT_DATA_MAX_AGE or age < -60:
            raise HTTPException(401, "initData: expired")
    except ValueError:
        raise HTTPException(401, "initData: malformed auth_date")

    data_check_string = '\n'.join(f'{k}={v}' for k, v in sorted(parsed.items()))

    candidate_tokens = [BOT_TOKEN]
    if BOT_TOKEN_OLD:
        candidate_tokens.append(BOT_TOKEN_OLD)

    if not any(
        hmac.compare_digest(
            hmac.new(_secret_key(tok), data_check_string.encode(), hashlib.sha256).hexdigest(),
            received_hash,
        )
        for tok in candidate_tokens
    ):
        raise HTTPException(401, "initData: invalid signature")

    try:
        user = json.loads(parsed['user'])
    except (KeyError, json.JSONDecodeError):
        raise HTTPException(401, "initData: no user")
    # start_param carries the invite token from a t.me deep-link
    # (?startapp=<token>) -- Telegram signs it as part of initData, so this
    # value is as trustworthy as the user id itself. None when the Mini App
    # was opened without a deep-link (normal menu-button open).
    user['_start_param'] = parsed.get('start_param')
    return user


def _session_secret(bot_token: str = BOT_TOKEN) -> bytes:
    """Домен-разделённый от _secret_key() (initData HMAC) -- разный "usage" в HMAC над
    тем же bot_token, так что компрометация одного не равна компрометации другого.
    Default stays the new primary BOT_TOKEN -- see verify_session_token for the
    dual-verify fallback during the cutover migration window."""
    return hmac.new(b"SessionToken", bot_token.encode(), hashlib.sha256).digest()


def create_session_token(user_id) -> str:
    """Token = base64url(user_id.exp).hex(hmac). Полезная нагрузка содержит ТОЛЬКО
    user_id и unix-время истечения -- никаких паролей/ключей/ролей внутри, роль каждый
    раз проверяется заново по актуальному roles.json (см. get_current_user).

    Bot token migration -- FINAL CUTOVER: always signs with the NEW primary
    secret (_session_secret() default) only. Never signs with BOT_TOKEN_OLD --
    old secret is verify-only, see verify_session_token."""
    exp = int(time.time()) + SESSION_TOKEN_MAX_AGE
    payload = f"{user_id}.{exp}"
    payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip('=')
    sig = hmac.new(_session_secret(), payload_b64.encode(), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"


def verify_session_token(token: str) -> str:
    """Возвращает user_id (str) при валидной подписи и не истёкшем токене, иначе 401.
    hmac.compare_digest -- constant-time сравнение, не `==` (timing-attack защита).

    Bot token migration -- FINAL CUTOVER (2026-10-04): accepts a signature
    from EITHER the new primary secret OR the old secret (BOT_TOKEN_OLD) as a
    fallback -- this is what lets a session token created BEFORE cutover
    keep working for the rest of its 12h TTL without forcing a re-login.
    create_session_token() above never uses the old secret; this is verify-only.
    Remove the BOT_TOKEN_OLD fallback branch here once the migration window
    closes (see core/telegram.py's BOT_TOKEN_OLD docstring)."""
    try:
        payload_b64, sig = token.rsplit('.', 1)
    except ValueError:
        raise HTTPException(401, "session token: malformed")

    candidate_secrets = [_session_secret()]
    if BOT_TOKEN_OLD:
        candidate_secrets.append(_session_secret(BOT_TOKEN_OLD))

    if not any(
        hmac.compare_digest(
            hmac.new(secret, payload_b64.encode(), hashlib.sha256).hexdigest(),
            sig,
        )
        for secret in candidate_secrets
    ):
        raise HTTPException(401, "session token: invalid signature")

    try:
        padded = payload_b64 + '=' * (-len(payload_b64) % 4)
        payload = base64.urlsafe_b64decode(padded.encode()).decode()
        user_id_str, exp_str = payload.split('.', 1)
        exp = int(exp_str)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(401, "session token: malformed payload")

    if time.time() > exp:
        raise HTTPException(401, "session token: expired")

    return user_id_str


def _load_roles() -> dict:
    return _safe_load_json(ROLES_FILE, {})


def _save_roles(roles: dict):
    _atomic_write_json(ROLES_FILE, roles)


def _load_notified_users() -> dict:
    raw = _safe_load_json(NOTIFIED_USERS_FILE, {})
    if isinstance(raw, list):
        # миграция со старого формата (список без timestamp) — считаем уведомлёнными сейчас
        now = time.time()
        return {uid: now for uid in raw}
    cutoff = time.time() - NOTIFIED_USERS_TTL
    return {uid: ts for uid, ts in raw.items() if ts >= cutoff}


def _save_notified_users(notified: dict):
    _atomic_write_json(NOTIFIED_USERS_FILE, notified)


def _notify_owner_new_user(user: dict, roles: dict):
    owner_id = next((uid for uid, r in roles.items() if r == 'owner'), None)
    if not owner_id:
        return
    notified = _load_notified_users()
    uid = str(user['id'])
    if uid in notified:
        return
    name = user.get('first_name', '') + (' ' + user['last_name'] if user.get('last_name') else '')
    username = f" (@{user['username']})" if user.get('username') else ''
    text = f"Новый пользователь открыл miniapp:\n{name.strip() or '—'}{username}\nID: {uid}\n\nДобавьте в roles.json, чтобы дать доступ."
    try:
        send_telegram_message(owner_id, text)
    except Exception:
        return  # уведомление best-effort — не блокировать 403-ответ, если Telegram недоступен
    notified[uid] = time.time()
    _save_notified_users(notified)


def get_verified_telegram_user(
    authorization: str | None = Header(default=None),
    x_telegram_init_data: str | None = Header(default=None),
) -> dict:
    """Auth-only half of the old get_current_user: proves WHO is calling
    (valid session token or HMAC-valid Telegram initData), without requiring
    that identity to already be in roles.json. Split out (30.09, worker
    invite flow) because invite/onboarding-request consumption must run for
    a Telegram user who is, by definition, not whitelisted yet -- the old
    combined function 403'd before that code could ever run. get_current_user
    below still does full auth+whitelist for every other route; this is not
    a relaxation of any existing endpoint's protection."""
    if authorization and authorization.lower().startswith('bearer '):
        token = authorization[7:].strip()
        user_id = verify_session_token(token)
        user = {'id': int(user_id)} if user_id.lstrip('-').isdigit() else {'id': user_id}
        user['_auth_source'] = 'bearer'
        user['_start_param'] = None
        return user
    elif x_telegram_init_data:
        user = validate_init_data(x_telegram_init_data)
        user['_auth_source'] = 'telegram_init_data'
        return user
    else:
        raise HTTPException(401, "Нет initData и нет session token")


def get_current_user(
    authorization: str | None = Header(default=None),
    x_telegram_init_data: str | None = Header(default=None),
) -> dict:
    """03.08 (ТЗ Задача 1): предпочитаем Authorization: Bearer <session token> --
    12-часовой backend-token, не требует свежего initData на каждый запрос.
    X-Telegram-Init-Data остаётся как fallback для обратной совместимости со старыми
    клиентами/вкладками, которые ещё не обновились на новый auth-путь -- временно,
    убрать после того, как весь трафик перейдёт на токены (см. PROJECT_STATE.md).

    30.09: identity verification itself moved to get_verified_telegram_user,
    but this function KEEPS the original (authorization, x_telegram_init_data)
    header params and calls get_verified_telegram_user as a plain function
    (not via Depends) -- main.py's create_session and 15+ test files call
    get_current_user(authorization=..., x_telegram_init_data=...) directly,
    bypassing FastAPI's DI resolution entirely, so changing this signature
    to `user: dict = Depends(...)` broke every direct caller with a
    TypeError. This function now only adds the whitelist/role gate on top of
    get_verified_telegram_user's identity check -- same 401/403 behavior as
    before the split, unchanged for every existing caller."""
    user = get_verified_telegram_user(authorization=authorization, x_telegram_init_data=x_telegram_init_data)
    auth_source = user['_auth_source']

    # Whitelist (Фаза 10.1): доступ только тем, кого владелец явно добавил в roles.json —
    # раньше любой Telegram user_id молча получал worker-права по умолчанию (см. get_role ниже).
    # 03.08: проверяется на КАЖДЫЙ запрос заново независимо от источника auth (initData
    # или session token) -- владелец, удаливший работника из whitelist, обрывает доступ
    # немедленно, даже если у клиента ещё живой 12-часовой токен.
    roles = _load_roles()
    if str(user['id']) not in roles:
        if auth_source == 'telegram_init_data':
            _notify_owner_new_user(user, roles)
        raise HTTPException(403, "Доступ не предоставлен. Обратитесь к владельцу.")
    _auth_audit_context.set({
        'user_id': str(user['id']),
        'role': roles.get(str(user['id']), 'worker'),
        'source': auth_source,
    })
    _last_seen[str(user['id'])] = time.time()
    return user


def get_role(user: dict = Depends(get_current_user)) -> str:
    roles = _load_roles()
    return roles.get(str(user['id']), 'worker')


def require_owner(role: str = Depends(get_role)):
    if role != 'owner':
        raise HTTPException(403, "owner only")


class RoleSetBody(BaseModel):
    user_id: str
    role: str  # 'owner' | 'worker'
