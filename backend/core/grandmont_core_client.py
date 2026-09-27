"""Integration seam for the future Grandmont Core service.

This module is a pure adapter/contract layer. It does not implement any real
Core integration -- Core production-readiness isn't there yet (see the
parallel Core PR #3/#2/#4 work happening in whymsiluxe/grandmont-group-core).
Its only job right now is to give a later pass a stable place to plug real
Core HTTP calls into, without touching any of the existing legacy code paths
in daily_plan_lib.py / assignment_matching.py / routes/daily_plan.py /
routes/checkin.py / routes/execution.py.

Hard invariant: CoreIntegrationConfig.enabled defaults to False, and
GrandmontCoreClient in the disabled state raises before doing anything
network-shaped. Nothing in this module is wired into main.py or any route
yet -- importing it has zero effect on current production behavior.

No Core endpoints are invented here. No auth scheme is assumed. Callers
supply an idempotency key and get back a CoreResult; how a real HTTP call
maps onto that (path, payload shape, header names) is deliberately left for
whoever wires up the real client once Core's actual API exists.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


def _env_compat(name: str, legacy_name: str, default=None):
    """Same fallback pattern already used in main.py's AGENT_ROOT: new name
    wins, old name still honoured. Kept local (not imported from main.py) so
    this module has zero coupling to main.py, matching the existing
    convention in daily_plan_lib.py/core/paths.py of each module re-deriving
    its own env-backed config independently."""
    value = os.environ.get(name)
    if value is None:
        value = os.environ.get(legacy_name)
    return default if value is None else value


def _env_bool(name: str, legacy_name: str, default: bool) -> bool:
    raw = _env_compat(name, legacy_name, None)
    if raw is None:
        return default
    return str(raw).strip().lower() in ('1', 'true', 'yes', 'on')


def _env_float(name: str, legacy_name: str, default: float) -> float:
    raw = _env_compat(name, legacy_name, None)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


class CoreErrorKind(str, Enum):
    """Deliberately small and transport-agnostic -- no HTTP status codes here.
    A real client implementation maps whatever the future Core API actually
    returns onto these; callers of GrandmontCoreClient never need to know
    Core speaks HTTP (or gRPC, or anything else) at all."""
    DISABLED = 'disabled'
    CONFIG_INVALID = 'config_invalid'
    TIMEOUT = 'timeout'
    UNAVAILABLE = 'unavailable'
    REJECTED = 'rejected'
    UNKNOWN = 'unknown'


class CoreIntegrationError(Exception):
    """Raised by GrandmontCoreClient for any failure -- disabled-mode calls,
    config problems, or (once a real transport exists) network/timeout/
    rejection failures. Callers catch this one type and branch on `.kind`
    rather than needing to know the transport-level exception hierarchy."""

    def __init__(self, kind: CoreErrorKind, message: str, status_code: int | None = None):
        self.kind = kind
        self.message = message
        self.status_code = status_code
        super().__init__(f"[{kind.value}] {message}")


@dataclass(frozen=True)
class CoreIntegrationConfig:
    """Config for the future Core client. CRITICAL: enabled defaults to
    False -- current Mini App production behavior must be 100% unaffected
    until someone explicitly turns this on with real values.

    base_url / service_credential are placeholders: no real Core endpoint or
    auth scheme is assumed or hardcoded here. Whoever wires up the real
    client fills these in against Core's actual, by-then-finalized API.
    """
    enabled: bool = False
    base_url: str = ''
    service_credential: str = ''
    timeout_seconds: float = 5.0

    @classmethod
    def from_env(cls) -> 'CoreIntegrationConfig':
        """CORE_INTEGRATION_ENABLED / GRANDMONT_GROUP_CORE_INTEGRATION_ENABLED
        -- off unless explicitly set truthy. No legacy PROMONTA_* name for
        this: Core integration did not exist before the rebrand, so there is
        nothing to stay backward-compatible with here (unlike AGENT_ROOT).
        """
        return cls(
            enabled=_env_bool('CORE_INTEGRATION_ENABLED', 'CORE_INTEGRATION_ENABLED', False),
            base_url=_env_compat('CORE_BASE_URL', 'CORE_BASE_URL', '') or '',
            service_credential=_env_compat('CORE_SERVICE_CREDENTIAL', 'CORE_SERVICE_CREDENTIAL', '') or '',
            timeout_seconds=_env_float('CORE_TIMEOUT_SECONDS', 'CORE_TIMEOUT_SECONDS', 5.0),
        )

    def validate(self) -> None:
        """Only meaningful once enabled=True. A disabled config is always
        considered valid -- it's never going to be used for anything, so
        there's nothing to validate. Raises CoreIntegrationError with kind
        CONFIG_INVALID when enabled but missing what a real call would need.
        """
        if not self.enabled:
            return
        if not self.base_url.strip():
            raise CoreIntegrationError(
                CoreErrorKind.CONFIG_INVALID,
                "CORE_BASE_URL is required when Core integration is enabled",
            )
        if not self.service_credential.strip():
            raise CoreIntegrationError(
                CoreErrorKind.CONFIG_INVALID,
                "CORE_SERVICE_CREDENTIAL is required when Core integration is enabled",
            )
        if self.timeout_seconds <= 0:
            raise CoreIntegrationError(
                CoreErrorKind.CONFIG_INVALID,
                "CORE_TIMEOUT_SECONDS must be > 0",
            )


@dataclass(frozen=True)
class CoreResult:
    """Generic response envelope for a Core command call. `data` shape is
    entirely command-specific and opaque to this module on purpose -- this
    layer doesn't know or guess what future Core commands return."""
    ok: bool
    data: Any = None
    idempotency_key: str = ''


TransportFn = Callable[[str, dict, str, float], dict]
"""Signature a real transport implementation must satisfy:
(command_name, payload, idempotency_key, timeout_seconds) -> raw response
dict. Disabled mode never reaches the point of calling it. This exists
purely so a real HTTP transport (or a test fake) can be injected without
changing GrandmontCoreClient's own code.
"""

ReadTransportFn = Callable[[str, dict, float], dict]
"""Signature for GET-style read calls (identity resolve, list assignments/
absences): (path, params, timeout_seconds) -> raw response dict. Kept
separate from TransportFn because reads are not idempotency-keyed commands.
"""


@dataclass
class GrandmontCoreClient:
    """Adapter boundary for the future Grandmont Core integration.

    Nothing in the current Mini App calls this yet. It exists so that once
    Core is production-ready, the real integration work is "implement
    `transport` and flip CORE_INTEGRATION_ENABLED=true", not "go rewrite
    daily_plan_lib.py/assignment_matching.py from scratch".

    generic `command()` method:
      - raises CoreIntegrationError(DISABLED, ...) immediately when
        config.enabled is False -- no network-shaped code runs at all.
      - validates config (raises CONFIG_INVALID) before ever touching
        `transport`.
      - propagates the given idempotency_key through to the transport call
        unchanged -- caller-supplied, never generated silently, so retries
        from the Mini App side stay under the caller's control (matching the
        Idempotency-Key pattern already used at the HTTP boundary in
        routes/checkin.py).
      - wraps any transport-raised exception as CoreIntegrationError(kind,...)
        rather than letting arbitrary exceptions leak past this boundary.
    """
    config: CoreIntegrationConfig = field(default_factory=CoreIntegrationConfig.from_env)
    transport: TransportFn | None = None
    read_transport: ReadTransportFn | None = None

    def _ensure_enabled(self) -> None:
        """Shared disabled/config-validation gate for command() and every
        read helper below -- disabled mode must short-circuit before
        touching config validation or any transport, for reads exactly as
        for commands."""
        if not self.config.enabled:
            raise CoreIntegrationError(
                CoreErrorKind.DISABLED,
                "Core integration is disabled (CORE_INTEGRATION_ENABLED is not set) "
                "-- refusing network access",
            )
        self.config.validate()

    def command(self, name: str, payload: dict, idempotency_key: str | None = None) -> CoreResult:
        self._ensure_enabled()

        # Idempotency key must be caller-supplied, never generated here. A
        # silently-generated key defeats the whole point of idempotency: a
        # caller-side retry (network timeout, crash, double-submit) needs to
        # reuse the SAME key so Core can recognize the replay -- if this
        # client minted a fresh one on a missing key, every retry would look
        # like a brand-new command to Core. Fail loudly instead of hiding
        # that bug behind a client-generated key.
        if not idempotency_key or not idempotency_key.strip():
            raise CoreIntegrationError(
                CoreErrorKind.CONFIG_INVALID,
                f"idempotency_key is required for command {name!r} -- "
                "the caller must supply and reuse the same key across retries",
            )
        key = idempotency_key

        if self.transport is None:
            raise CoreIntegrationError(
                CoreErrorKind.CONFIG_INVALID,
                "Core integration is enabled but no transport is configured -- "
                "real Core endpoints are not implemented yet",
            )

        try:
            raw = self.transport(name, payload, key, self.config.timeout_seconds)
        except TimeoutError as e:
            raise CoreIntegrationError(CoreErrorKind.TIMEOUT, str(e)) from e
        except CoreIntegrationError:
            raise
        except Exception as e:
            raise CoreIntegrationError(CoreErrorKind.UNAVAILABLE, str(e)) from e

        return CoreResult(ok=True, data=raw, idempotency_key=key)

    def _read(self, path: str, params: dict) -> dict:
        self._ensure_enabled()
        if self.read_transport is None:
            raise CoreIntegrationError(
                CoreErrorKind.CONFIG_INVALID,
                "Core integration is enabled but no read transport is configured -- "
                "real Core endpoints are not implemented yet",
            )
        try:
            return self.read_transport(path, params, self.config.timeout_seconds)
        except TimeoutError as e:
            raise CoreIntegrationError(CoreErrorKind.TIMEOUT, str(e)) from e
        except CoreIntegrationError:
            raise
        except Exception as e:
            raise CoreIntegrationError(CoreErrorKind.UNAVAILABLE, str(e)) from e

    def resolve_worker_identity(self, legacy_id: str) -> CoreResult:
        """GET /worker-identity/resolve for source_system=miniapp,
        entity_type=worker. A 404 (no mapping exists, or the mapping isn't
        visible to this tenant) is a controlled 'unmapped worker' outcome,
        not an error -- returns CoreResult(ok=False, data=None) instead of
        raising. Never auto-creates a Worker, never invents a canonical id."""
        try:
            raw = self._read('/worker-identity/resolve', {
                'source_system': 'miniapp',
                'entity_type': 'worker',
                'legacy_id': str(legacy_id),
            })
        except CoreIntegrationError as e:
            if e.kind == CoreErrorKind.REJECTED and e.status_code == 404:
                return CoreResult(ok=False, data=None)
            raise
        return CoreResult(ok=True, data=raw)

    def list_assignments(self, *, worker_id: str, project_id: str | None = None,
                          status: str | None = None, from_at: str | None = None,
                          to_at: str | None = None) -> CoreResult:
        """GET /assignments?worker_id=... Worker-facing shadow reads always
        scope by worker_id (the caller's own resolved Core Worker UUID)."""
        params: dict = {'worker_id': worker_id}
        if project_id:
            params['project_id'] = project_id
        if status:
            params['status'] = status
        if from_at:
            params['from_at'] = from_at
        if to_at:
            params['to_at'] = to_at
        raw = self._read('/assignments', params)
        return CoreResult(ok=True, data=raw)

    def list_absences(self, *, worker_id: str, status: str | None = None,
                       from_at: str | None = None, to_at: str | None = None) -> CoreResult:
        """GET /absences?worker_id=..."""
        params: dict = {'worker_id': worker_id}
        if status:
            params['status'] = status
        if from_at:
            params['from_at'] = from_at
        if to_at:
            params['to_at'] = to_at
        raw = self._read('/absences', params)
        return CoreResult(ok=True, data=raw)

    def create_absence(self, *, organization_id: str, worker_id: str, type_: str,
                        starts_at: str, ends_at: str, idempotency_key: str,
                        note: str | None = None) -> CoreResult:
        """POST /commands/absence/create via the generic command() path --
        canonical Worker UUID and organization_id must already be resolved
        by the caller (see workforce_shadow.resolve_worker_uuid); this
        method never resolves identity itself."""
        payload: dict = {
            'organization_id': organization_id,
            'worker_id': worker_id,
            'type': type_,
            'starts_at': starts_at,
            'ends_at': ends_at,
        }
        if note:
            payload['note'] = note
        return self.command('absence/create', payload, idempotency_key=idempotency_key)


class CoreHttpTransport:
    """Real HTTP transport for GrandmontCoreClient against the deployed
    Grandmont Core service. Only constructed when CORE_INTEGRATION_ENABLED
    is true (see get_core_client()) -- disabled mode never imports httpx or
    opens a connection.

    Maps command() calls onto POST {base_url}/commands/{name} with the
    command envelope Core expects, and read calls onto
    GET {base_url}{path}?{params}. Auth is a single X-Core-Service-Token
    header; the credential is never included in a log line or in any raised
    exception's message -- error messages here are either Core's own
    response body (`code`/`message`, which Core controls, not us) or a
    static string, never a formatted transport exception.
    """

    def __init__(self, config: CoreIntegrationConfig, client=None):
        import httpx

        self._config = config
        self._client = client or httpx.Client(base_url=config.base_url, timeout=config.timeout_seconds)

    def _headers(self) -> dict:
        return {
            'X-Core-Service-Token': self._config.service_credential,
            'Content-Type': 'application/json',
        }

    def _handle_response(self, response) -> dict:
        if response.status_code < 300:
            return response.json()
        try:
            body = response.json()
            message = body.get('message') or body.get('code') or f"Core returned HTTP {response.status_code}"
        except Exception:
            message = f"Core returned HTTP {response.status_code}"
        if response.status_code < 500:
            raise CoreIntegrationError(CoreErrorKind.REJECTED, message, status_code=response.status_code)
        raise CoreIntegrationError(CoreErrorKind.UNAVAILABLE, message, status_code=response.status_code)

    def __call__(self, name: str, payload: dict, idempotency_key: str, timeout_seconds: float) -> dict:
        """TransportFn implementation: POST /commands/{name}."""
        import httpx

        body = {
            'command_id': idempotency_key,
            'idempotency_key': idempotency_key,
            'actor_id': 'service:miniapp',
            'source': 'miniapp',
            'payload': payload,
        }
        try:
            response = self._client.post(f'/commands/{name}', json=body, headers=self._headers(), timeout=timeout_seconds)
        except httpx.TimeoutException as e:
            raise TimeoutError("Core command request timed out") from e
        except httpx.HTTPError as e:
            raise CoreIntegrationError(CoreErrorKind.UNAVAILABLE, "Core command request failed") from e
        return self._handle_response(response)

    def get(self, path: str, params: dict, timeout_seconds: float) -> dict:
        """ReadTransportFn implementation: GET {path}?{params}."""
        import httpx

        try:
            response = self._client.get(path, params=params, headers=self._headers(), timeout=timeout_seconds)
        except httpx.TimeoutException as e:
            raise TimeoutError("Core read request timed out") from e
        except httpx.HTTPError as e:
            raise CoreIntegrationError(CoreErrorKind.UNAVAILABLE, "Core read request failed") from e
        return self._handle_response(response)


_default_client: GrandmontCoreClient | None = None


def get_core_client() -> GrandmontCoreClient:
    """Module-level singleton accessor, mirroring the pattern other backend
    singletons in this repo use (e.g. daily_plan_lib's module-level store
    config). Lazily constructed from env on first use so importing this
    module has no side effects and no env is read at import time. Only
    constructs a real CoreHttpTransport (and only then imports httpx / opens
    a client) when the config is actually enabled -- disabled mode stays
    100% network-free."""
    global _default_client
    if _default_client is None:
        config = CoreIntegrationConfig.from_env()
        if config.enabled:
            transport = CoreHttpTransport(config)
            _default_client = GrandmontCoreClient(config=config, transport=transport, read_transport=transport.get)
        else:
            _default_client = GrandmontCoreClient(config=config)
    return _default_client


def reset_core_client_for_tests() -> None:
    """Test-only helper to force re-reading env/config between test cases --
    mirrors the reset helpers already used elsewhere in this test suite for
    other module-level singletons."""
    global _default_client
    _default_client = None
