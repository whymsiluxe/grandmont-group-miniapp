"""Roles/whitelist management -- GET/POST/DELETE /api/roles(/{target_user_id}).

First real router extraction (Phase A's core/* work was the prerequisite --
see core/permissions.py's module docstring). Dependency direction proven
here, to be the template for the remaining domains:

    core.paths / core.storage
              ^
    core.permissions / core.profiles / core.telegram
              ^
    routes.auth
              ^
    main.py (app composition: app.include_router(...))

No import from main.py anywhere in this module. Route paths/methods/request
model/response shapes/permission checks/side effects are byte-for-byte the
same as the endpoints previously defined directly on `app` in main.py --
this is code motion, not a behavior change.
"""
import hashlib

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

try:
    from ..core.permissions import (
        get_current_user,
        get_verified_telegram_user,
        require_owner,
        _load_roles,
        _save_roles,
        _load_notified_users,
        RoleSetBody,
    )
    from ..core.profiles import _load_worker_profiles, _sanitize_display_name
    from ..core.telegram import send_telegram_message
    from ..core.grandmont_core_client import get_core_client, CoreIntegrationError, CoreErrorKind
except ImportError:
    from core.permissions import (  # noqa: E402
        get_current_user,
        get_verified_telegram_user,
        require_owner,
        _load_roles,
        _save_roles,
        _load_notified_users,
        RoleSetBody,
    )
    from core.profiles import _load_worker_profiles, _sanitize_display_name  # noqa: E402
    from core.telegram import send_telegram_message  # noqa: E402
    from core.grandmont_core_client import get_core_client, CoreIntegrationError, CoreErrorKind  # noqa: E402


router = APIRouter()


@router.get("/api/roles")
def list_roles(user: dict = Depends(get_current_user), _: None = Depends(require_owner)):
    """10.29 (Fable-аудит): раньше добавление воркера требовало ручной SSH+правку JSON —
    теперь owner может смотреть/менять whitelist прямо из приложения."""
    roles = _load_roles()
    notified = _load_notified_users()
    profiles = _load_worker_profiles()
    # 09.09: pending раньше строился ТОЛЬКО из notified_users - roles -- пользователь,
    # который прошёл онбординг (появился в worker_profiles.json) но никогда не попадал
    # в notified_users (например если процесс уведомления сбоил, или профиль создан
    # каким-то другим путём), был невидим здесь целиком: не в roles (нет доступа), не
    # в pending (не в notified) -- "призрак", которого Access-вкладка не показывала
    # вообще, хотя /api/workers считал его активным работником (тот же баг, см.
    # комментарий там). Теперь pending = любой профиль без активной роли, не
    # пересечение с notified -- notified_users используется только чтобы ПОМЕТИТЬ
    # (was_notified), не как обязательное условие попадания в список.
    pending_ids = sorted(set(profiles.keys()) - set(roles.keys()))
    return {
        "roles": [{"user_id": uid, "role": r,
                   "name": _sanitize_display_name(profiles.get(uid, {}).get('name'), uid)}
                  for uid, r in roles.items()],
        "pending": [{"user_id": uid,
                     "name": _sanitize_display_name(profiles.get(uid, {}).get('name'), uid),
                     "was_notified": uid in notified}
                    for uid in pending_ids],
    }


@router.post("/api/roles")
def set_role(body: RoleSetBody, user: dict = Depends(get_current_user), _: None = Depends(require_owner)):
    if body.role not in ('owner', 'worker'):
        raise HTTPException(400, "role должен быть owner или worker")
    roles = _load_roles()
    if body.role == 'worker' and roles.get(str(body.user_id)) == 'owner':
        remaining_owners = sum(1 for r in roles.values() if r == 'owner') - 1
        if remaining_owners < 1:
            raise HTTPException(400, "Нельзя понизить последнего owner — фирма останется без владельца в приложении")
    roles[str(body.user_id)] = body.role
    _save_roles(roles)
    try:
        send_telegram_message(int(body.user_id), f"Вам предоставлен доступ к miniapp (роль: {body.role}).")
    except Exception:
        pass
    return {"status": "ok"}


@router.delete("/api/roles/{target_user_id}")
def revoke_role(target_user_id: str, user: dict = Depends(get_current_user), _: None = Depends(require_owner)):
    if target_user_id == str(user['id']):
        raise HTTPException(400, "Нельзя удалить свою же роль")
    roles = _load_roles()
    if roles.get(target_user_id) == 'owner':
        remaining_owners = sum(1 for r in roles.values() if r == 'owner') - 1
        if remaining_owners < 1:
            raise HTTPException(400, "Нельзя удалить последнего owner — фирма останется без владельца в приложении")
    roles.pop(target_user_id, None)
    _save_roles(roles)
    return {"status": "ok"}


class InviteConsumeBody(BaseModel):
    token: str


class OnboardingRequestBody(BaseModel):
    first_name: str
    last_name: str = ''
    phone: str = ''


@router.post("/api/invite/consume")
def consume_invite(
    body: InviteConsumeBody,
    user: dict = Depends(get_verified_telegram_user),
):
    """Flow A (CRM-first): worker opened the Mini App via a t.me deep-link
    (?startapp=<token>) after an admin created them in CRM and generated an
    invite. Depends on get_verified_telegram_user, NOT get_current_user --
    this user is by definition not in roles.json yet, so the whitelist gate
    in get_current_user would 403 before this code ever ran.

    telegram_user_id comes from `user` (server-verified via HMAC initData
    check), never from the request body -- the frontend cannot assert who it
    is. The raw token is hashed here; only the hash ever reaches Core.
    """
    client = get_core_client()
    if not client.config.enabled:
        raise HTTPException(503, "Интеграция с Core отключена")
    if not client.config.organization_id:
        raise HTTPException(503, "CORE_ORGANIZATION_ID не настроен")

    token_hash = hashlib.sha256(body.token.encode()).hexdigest()
    telegram_user_id = str(user['id'])
    idempotency_key = f"miniapp-invite-consume-{token_hash}-{telegram_user_id}"

    try:
        result = client.consume_worker_invite(
            organization_id=client.config.organization_id,
            token_hash=token_hash,
            telegram_user_id=telegram_user_id,
            idempotency_key=idempotency_key,
        )
    except CoreIntegrationError as e:
        if e.kind == CoreErrorKind.REJECTED:
            raise HTTPException(409, "Приглашение недействительно, истекло или уже использовано")
        raise HTTPException(503, "Core недоступен, попробуйте позже")

    if not result.ok:
        raise HTTPException(409, "Приглашение недействительно, истекло или уже использовано")

    # Successful Core-side consume is the authorization decision. roles.json
    # stays the LOCAL authorization representation get_current_user already
    # reads on every request -- Core's legacy_maps stays the canonical
    # identity link; this write only mirrors that decision into the
    # whitelist gate so the worker does not need a manual SSH/roles.json
    # edit to get in.
    roles = _load_roles()
    if telegram_user_id not in roles:
        roles[telegram_user_id] = 'worker'
        _save_roles(roles)

    worker = (result.data or {}).get('worker') or {}
    return {"status": "ok", "worker_id": worker.get('id')}


@router.post("/api/onboarding/request")
def request_onboarding(
    body: OnboardingRequestBody,
    user: dict = Depends(get_verified_telegram_user),
):
    """Flow B (Miniapp-first): worker opened the Mini App with no invite
    token and is not yet whitelisted. Creates a PENDING onboarding request in
    Core for CRM admin review -- never auto-creates a canonical Worker, never
    grants Miniapp access by itself. Depends on get_verified_telegram_user
    for the same reason as consume_invite above.
    """
    client = get_core_client()
    if not client.config.enabled:
        raise HTTPException(503, "Интеграция с Core отключена")
    if not client.config.organization_id:
        raise HTTPException(503, "CORE_ORGANIZATION_ID не настроен")

    telegram_user_id = str(user['id'])
    idempotency_key = f"miniapp-onboarding-request-{telegram_user_id}"

    try:
        result = client.create_worker_onboarding_request(
            organization_id=client.config.organization_id,
            telegram_user_id=telegram_user_id,
            telegram_username=user.get('username'),
            first_name=body.first_name,
            last_name=body.last_name,
            phone=body.phone,
            idempotency_key=idempotency_key,
        )
    except CoreIntegrationError:
        raise HTTPException(503, "Core недоступен, попробуйте позже")

    if not result.ok:
        raise HTTPException(409, "Не удалось создать заявку")

    request_data = (result.data or {}).get('request') or {}
    return {"status": "pending", "request_id": request_data.get('id')}
