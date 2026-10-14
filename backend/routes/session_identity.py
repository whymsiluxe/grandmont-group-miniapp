"""Current-user identity and session-token HTTP routes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, Depends, Header


@dataclass(frozen=True)
class SessionIdentityDeps:
    get_current_user: Callable[..., dict]
    get_role: Callable[..., str]
    create_session_token: Callable[[Any], str]
    session_token_max_age: int


@dataclass(frozen=True)
class SessionIdentityHandlers:
    me: Callable[..., Any]
    create_session: Callable[..., Any]


def create_session_identity_router(
    deps: SessionIdentityDeps,
) -> tuple[APIRouter, SessionIdentityHandlers]:
    router = APIRouter()

    @router.get("/api/me")
    def me(
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        return {
            "user_id": user["id"],
            "name": user.get("first_name"),
            "role": role,
        }

    @router.post("/api/session")
    def create_session(x_telegram_init_data: str = Header(...)) -> dict:
        """Exchange fresh Telegram initData for the existing session-token contract."""
        user = deps.get_current_user(
            authorization=None,
            x_telegram_init_data=x_telegram_init_data,
        )
        token = deps.create_session_token(user["id"])
        return {
            "token": token,
            "expires_in": deps.session_token_max_age,
            "user_id": user["id"],
        }

    return router, SessionIdentityHandlers(
        me=me,
        create_session=create_session,
    )
