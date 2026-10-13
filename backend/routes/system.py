"""System health, readiness and diagnostics HTTP routes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, Depends


@dataclass(frozen=True)
class SystemDeps:
    get_current_user: Callable[..., Any]
    require_owner: Callable[..., Any]
    health_response: Callable[[str], dict]
    readiness_response: Callable[..., dict]
    diagnostics_response: Callable[..., dict]
    app_version_file: str
    object_photo_dir: str
    chat_attach_dir: str
    tools_lib_path: str
    mangel_lib_path: str
    backend_dir: str
    roles_file: str
    get_diagnostics_kwargs: Callable[[], dict[str, Any]]


@dataclass(frozen=True)
class SystemHandlers:
    health: Callable[..., Any]
    health_ready: Callable[..., Any]
    diagnostics: Callable[..., Any]


def create_system_router(deps: SystemDeps) -> tuple[APIRouter, SystemHandlers]:
    router = APIRouter()

    @router.get("/api/health")
    def health():
        """Cheap liveness check; intentionally performs no network I/O."""
        return deps.health_response(deps.app_version_file)

    @router.get("/api/health/ready")
    def health_ready(
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(deps.require_owner),
    ):
        """Owner-only readiness check using cheap local-state probes."""
        return deps.readiness_response(
            object_photo_dir=deps.object_photo_dir,
            chat_attach_dir=deps.chat_attach_dir,
            tools_lib_path=deps.tools_lib_path,
            mangel_lib_path=deps.mangel_lib_path,
            backend_dir=deps.backend_dir,
            roles_file=deps.roles_file,
        )

    @router.get("/api/diagnostics")
    def diagnostics(_: None = Depends(deps.require_owner)):
        """Owner-only diagnostic snapshot without live external API calls."""
        return deps.diagnostics_response(**deps.get_diagnostics_kwargs())

    return router, SystemHandlers(
        health=health,
        health_ready=health_ready,
        diagnostics=diagnostics,
    )
