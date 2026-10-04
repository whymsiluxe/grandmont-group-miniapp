# Environment variables

Loaded on the VPS from `/etc/systemd/system/grandmont-miniapp.service`'s `EnvironmentFile=/etc/claude-agent.env` — a file shared with other Grandmont Group agent services, **not** specific to this app, and **not** in this repo. See `backend/.env.example` for names only.

| Variable | Required | Used for | Where read | Redeploy needed to change? |
|---|---|---|---|---|
| `BOT_TOKEN` | Yes | Telegram WebApp `initData` HMAC validation, `sendMessage`/`sendDocument` Bot API calls | `main.py` (`os.environ['BOT_TOKEN']`, line ~22 — hard fails at import if missing) | Yes, service restart |
| `BOT_TOKEN_OLD` | No — only during the final-cutover migration window (13h from cutover deploy) | Verify-only fallback for `initData` AND session-token signatures created before cutover (see `docs/DECISIONS.md`'s final-cutover entry and `docs/SECURITY.md`). Never used for outbound sends or signing new session tokens. Renamed from the interim phase's `BOT_TOKEN_NEW` once `BOT_TOKEN` itself became the new bot's value — see decision log for why. | `backend/core/telegram.py` (`os.environ.get('BOT_TOKEN_OLD', '')`), read by `backend/core/permissions.py`'s `validate_init_data`/`verify_session_token` | Yes, service restart. Remove this var (not just leave blank) once the 13h window closes AND acceptance has been re-confirmed. |
| `CLAUDE_BIN` | Yes for AI chat features | Path to Claude Code CLI binary, invoked as subprocess for AI chat/task-extraction | `main.py` | Yes |
| `GLM_KEY` | Optional | Fallback/alternative AI model (GLM) for chat features | `main.py` | Yes |
| `ALLOWED_CHAT` | No — not referenced anywhere in `main.py` (verified via grep). Belongs to another Grandmont Group service (`bot.py`/`webhook.py`) sharing the same env file | N/A to this app | — | N/A to this app |
| `WEBHOOK_SECRET` | No — belongs to a *different* service (`promonta-webhook`, lead intake), listed here only because it's in the same shared env file | Lead webhook auth | Not used by miniapp `main.py` | N/A to this app |

## Who issues values

Owner (business owner, has Telegram/BotFather access and the VPS root credentials). `BOT_TOKEN` is issued once per bot via @BotFather and is long-lived unless manually rotated.

## Consequences of a missing variable

`BOT_TOKEN` missing → `main.py` fails at import time (`os.environ['BOT_TOKEN']` raises `KeyError`, no default) → `grandmont-miniapp.service` fails to start → entire app down. This is the single point of failure to check first if the service won't start after an env file edit.

`CLAUDE_BIN`/`GLM_KEY` missing → AI chat/extraction features fail at call time, rest of the app unaffected — UNVERIFIED whether these fail gracefully (error toast) or throw a raw 500; worth checking next time that code path is touched.

## Local development

Verified: `main.py` does **not** call `load_dotenv()` (grepped, no match) despite `python-dotenv` being an installed dependency — it must be a transitive dependency of something else, or unused. A local `.env` file will **not** be picked up automatically; env vars must be exported into the shell/process environment directly (or `load_dotenv()` added, which would be a small, worthwhile local-dev improvement — see TODO.md).

## Grandmont Group rebrand env vars (26.09)

All optional; unset = pre-rebrand behaviour. `main.py` (`_env_compat`) and `scripts/autonomous_codex_runner.sh` still read the old `PROMONTA_*` name when the new one is unset.

| Variable | Legacy name | Purpose |
|---|---|---|
| `GRANDMONT_GROUP_ENV` | `PROMONTA_ENV` | `test` arms the prod-DATA_ROOT import guard (set by `tests/conftest.py`) |
| `GRANDMONT_GROUP_AGENT_ROOT` | `PROMONTA_AGENT_ROOT` | agent root, default `/home/promonta/agent` |
| `GRANDMONT_GROUP_CREATE_OBJECT_SCRIPT` / `..._FOLDER_SCRIPT` | `PROMONTA_CREATE_OBJECT_SCRIPT` / `..._FOLDER_SCRIPT` | object-creation script overrides |
| `GRANDMONT_GROUP_CONTACT_EMAIL` | — | Angebot PDF contact email. LEGACY_CONTACT_DOMAIN / DOMAIN_COMPAT_PENDING: unset -> current `anfragen@promonta-bau.de` |
| `GRANDMONT_GROUP_LOGO_PATH` | — | Rechnung PDF logo; default `backend/grandmont-group-logo.png`, text wordmark if missing |

## Grandmont Core workforce shadow integration (27.09)

All optional; unset/`false` = 100% current behavior, zero network calls, zero
log lines from the integration (`backend/core/grandmont_core_client.py` /
`backend/core/workforce_shadow.py`). No legacy `PROMONTA_*` fallback — this
integration didn't exist before the rebrand. **Do not set
`CORE_INTEGRATION_ENABLED=true` in production without explicit owner
approval** — even enabled, the Mini legacy path stays authoritative
(shadow-mode only); see `docs/DECISIONS.md`.

| Variable | Required when enabled? | Purpose |
|---|---|---|
| `CORE_INTEGRATION_ENABLED` | — | `true`/`1`/`yes`/`on` to enable; anything else (including unset) is disabled |
| `CORE_BASE_URL` | Yes | Core service base URL. Never hardcoded in code. |
| `CORE_SERVICE_CREDENTIAL` | Yes | Sent as `X-Core-Service-Token` header on every Core request; never logged, never included in raised exception messages |
| `CORE_TIMEOUT_SECONDS` | No (default `5.0`) | Per-request timeout to Core; must be `> 0` when enabled |
