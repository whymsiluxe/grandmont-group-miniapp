"""Canonical authentication, session-token and role/whitelist helpers.

This module is the single source of truth for authentication and role lookup.
Only the current GrandMont Telegram bot token is accepted for initData and
session-token verification; retired bot secrets are intentionally not loaded.
"""
import base64
import hashlib
import hmac
import json
import time
from contextvars import ContextVar
from urllib.parse import parse_qsl

from fastapi import Depends, Header, HTTPException
from pydantic import BaseModel

try:
    from .limits import INIT_DATA_MAX_AGE, SESSION_TOKEN_MAX_AGE, NOTIFIED_USERS_TTL
    from .paths import ROLES_FILE, NOTIFIED_USERS_FILE
    from .storage import _safe_load_json, _atomic_write_json
    from .telegram import BOT_TOKEN, send_telegram_message
except ImportError:
    from limits import INIT_DATA_MAX_AGE, SESSION_TOKEN_MAX_AGE, NOTIFIED_USERS_TTL  # noqa: E402
    from paths import ROLES_FILE, NOTIFIED_USERS_FILE  # noqa: E402
    from storage import _safe_load_json, _atomic_write_json  # noqa: E402
    from telegram import BOT_TOKEN, send_telegram_message  # noqa: E402


_auth_audit_context: ContextVar[dict | None] = ContextVar(
    'grandmont_group_auth_audit_context', default=None
)
_last_seen: dict = {}


def _secret_key(bot_token: str = BOT_TOKEN) -> bytes:
    """Derive Telegram WebAppData verification key from the current bot token."""
    return hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()


def validate_init_data(init_data: str) -> dict:
    """Validate Telegram WebApp initData HMAC and freshness."""
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError as exc:
        raise HTTPException(401, "initData: malformed") from exc

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
    except ValueError as exc:
        raise HTTPException(401, "initData: malformed auth_date") from exc

    data_check_string = '\n'.join(f'{k}={v}' for k, v in sorted(parsed.items()))
    expected_hash = hmac.new(
        _secret_key(), data_check_string.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        raise HTTPException(401, "initData: invalid signature")

    try:
        user = json.loads(parsed['user'])
    except (KeyError, json.JSONDecodeError) as exc:
        raise HTTPException(401, "initData: no user") from exc

    user['_start_param'] = parsed.get('start_param')
    return user


def _session_secret(bot_token: str = BOT_TOKEN) -> bytes:
    """Derive a domain-separated session-token secret from the current bot token."""
    return hmac.new(b"SessionToken", bot_token.encode(), hashlib.sha256).digest()


def create_session_token(user_id) -> str:
    """Create a signed, short-lived backend session token for ``user_id``."""
    exp = int(time.time()) + SESSION_TOKEN_MAX_AGE
    payload = f"{user_id}.{exp}"
    payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip('=')
    sig = hmac.new(_session_secret(), payload_b64.encode(), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"


def verify_session_token(token: str) -> str:
    """Return user id for a valid current-secret session token; otherwise 401."""
    try:
        payload_b64, sig = token.rsplit('.', 1)
    except ValueError as exc:
        raise HTTPException(401, "session token: malformed") from exc

    expected_sig = hmac.new(
        _session_secret(), payload_b64.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected_sig, sig):
        raise HTTPException(401, "session token: invalid signature")

    try:
        padded = payload_b64 + '=' * (-len(payload_b64) % 4)
        payload = base64.urlsafe_b64decode(padded.encode()).decode()
        user_id_str, exp_str = payload.split('.', 1)
        exp = int(exp_str)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(401, "session token: malformed payload") from exc

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
        now = time.time()
        return {uid: now for uid in raw}
    cutoff = time.time() - NOTIFIED_USERS_TTL
    return {uid: ts for uid, ts in raw.items() if ts >= cutoff}


def _save_notified_users(notified: dict):
    _atomic_write_json(NOTIFIED_USERS_FILE, notified)


def _notify_owner_new_user(user: dict, roles: dict):
    owner_id = next((uid for uid, role in roles.items() if role == 'owner'), None)
    if not owner_id:
        return

    notified = _load_notified_users()
    uid = str(user['id'])
    if uid in notified:
        return

    name = user.get('first_name', '') + (
        ' ' + user['last_name'] if user.get('last_name') else ''
    )
    username = f" (@{user['username']})" if user.get('username') else ''
    text = (
        f"Новый пользователь открыл miniapp:\n"
        f"{name.strip() or '—'}{username}\nID: {uid}\n\n"
        "Добавьте в roles.json, чтобы дать доступ."
    )
    try:
        send_telegram_message(owner_id, text)
    except Exception:
        return

    notified[uid] = time.time()
    _save_notified_users(notified)


def get_verified_telegram_user(
    authorization: str | None = Header(default=None),
    x_telegram_init_data: str | None = Header(default=None),
) -> dict:
    """Verify caller identity without applying the roles whitelist gate."""
    if authorization and authorization.lower().startswith('bearer '):
        token = authorization[7:].strip()
        user_id = verify_session_token(token)
        user = {'id': int(user_id)} if user_id.lstrip('-').isdigit() else {'id': user_id}
        user['_auth_source'] = 'bearer'
        user['_start_param'] = None
        return user

    if x_telegram_init_data:
        user = validate_init_data(x_telegram_init_data)
        user['_auth_source'] = 'telegram_init_data'
        return user

    raise HTTPException(401, "Нет initData и нет session token")


def get_current_user(
    authorization: str | None = Header(default=None),
    x_telegram_init_data: str | None = Header(default=None),
) -> dict:
    """Verify caller and require explicit membership in ``roles.json``."""
    user = get_verified_telegram_user(
        authorization=authorization,
        x_telegram_init_data=x_telegram_init_data,
    )
    auth_source = user['_auth_source']

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
    role: str
