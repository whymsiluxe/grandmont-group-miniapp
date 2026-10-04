# Security

## Authentication

Telegram WebApp `initData` HMAC-SHA256 validation (`_secret_key()` / signature check in `main.py`), the standard scheme Telegram documents for Mini Apps. No separate password/login, no JWT, no session cookies — the Telegram client itself is the identity provider, `BOT_TOKEN` is the shared secret.

### Bot token migration window (added 2026-10-04, `feat/bot-token-migration`)

Owner is migrating the Mini App to a new Telegram bot (`@GrandMont_bot`, replacing `@promonta_bot`). Flagging this explicitly per this file's own rule (auth change → tell the owner, don't just patch silently):

- `validate_init_data()` in `backend/core/permissions.py` now accepts initData signed by **either** `BOT_TOKEN` (old bot) or the optional `BOT_TOKEN_NEW` (new bot) while the latter is set. This widens the set of valid signers from one to two for the duration of the migration — a deliberate, temporary widening, not a permanent weakening: once `BOT_TOKEN_NEW` is unset again (post-cutover cleanup), only `BOT_TOKEN` validates, same as before this change.
- This does NOT widen outbound trust: `send_telegram_message`/`send_pdf_to_chat` and session-token signing (`_session_secret()`) stay on the single old `BOT_TOKEN` throughout — only inbound Mini-App-launch verification is dual.
- Risk during the window: if `BOT_TOKEN_NEW` were ever set to the wrong value (e.g. a stale/leaked token) rather than the actual new bot's real token, that wrong token's signer would also validate. Mitigate by setting `BOT_TOKEN_NEW` only from the exact value BotFather gave for `@GrandMont_bot`, and by keeping the migration window short (owner's stated plan: full migration, remove the old-token fallback only once new-bot acceptance is confirmed across onboarding/DailyPlan/shifts/absence/invite flow for a real existing worker).
- See `tests/test_bot_token_migration.py` for the regression coverage (old accepted, new accepted, third-party token still rejected, tampered signature still rejected, window-closed behavior restored).

## Authorization

Two roles, `owner`/`worker`, resolved from `roles.json` by Telegram user ID through `get_current_user()`. See [ROLES_AND_PERMISSIONS.md](ROLES_AND_PERMISSIONS.md) for the endpoint-by-endpoint audit — **that document may contain flagged gaps** (endpoints that should arguably be owner-only but don't visibly check role in the code read so far). Treat any such flag there as a live finding requiring owner decision, not something to silently patch.

## Fixed in the 2026-07-15 audit (for reference — verify still holds before assuming)

- **Stored XSS**: several places rendered user-controlled strings (e.g. `tool.holder`, various `e.message` in error handlers) directly into `innerHTML` without escaping. Fixed via a shared `esc()` helper applied at each site.
- **IDOR on file downloads**: `chat_attachments`, `checkin_photos`, `critical_alert_photos` were served by UUID/filename alone with no ownership check. Fixed — routes now verify thread membership / session ownership / target-user match before returning the file.
- **GPS privacy leak**: `GET /api/checkin` returned all workers' start/finish GPS coordinates to any authenticated worker. Fixed — now filtered to the requesting user's own records unless the caller is owner.
- **JSON store race/corruption**: unlocked read-modify-write on shared JSON files could corrupt a file mid-write on concurrent access or crash. Fixed via atomic write + per-file lock (see [DATABASE.md](DATABASE.md)).

These are recorded from a prior session's own account of its findings (not independently re-verified in this recovery pass) — spot-check before relying on the "fixed" status if a security-sensitive change is planned nearby.

## Input validation

FastAPI + Pydantic models provide request-shape validation on most routes (rejecting malformed JSON bodies). File upload size limits exist for at least chat attachments (8MB, per prior session notes) — UNVERIFIED whether this is enforced consistently across all upload routes (checkin photos, avatars, feed photos) or only chat.

## Secrets

`BOT_TOKEN`, `CLAUDE_BIN`, `GLM_KEY` — read from environment only (`os.environ`), never hardcoded (verified by grep across `main.py` and the JS PDF-generator files during this recovery — zero hardcoded-secret matches). Stored server-side in `/etc/claude-agent.env`, outside this repo, `chmod` restricted. Google Drive OAuth creds (`.gdrive_creds.json`/`.gdrive_token.json`, used by other Promonta agent scripts, not this miniapp directly) live alongside on the VPS with `chmod 600`.

**This repo's `.gitignore` explicitly excludes**: `.env*` (except `.env.example`), `*.pem/*.key/*.crt/*.p12/*.pfx`, `credentials.json`, `service-account*.json`, `*.gdrive_creds.json`, `*.gdrive_token.json`, and all runtime JSON data stores.

## Rate limiting

`ai_chat_rate.json` suggests some rate limiting exists for AI chat specifically — UNVERIFIED for other endpoints. No general API rate limiting confirmed at the FastAPI/Caddy level.

## Audit logging

`audit.log` (JSONL) records all POST/PATCH/DELETE requests with logrotate (weekly, 8 rotations). Does not cover GET requests, so read-access to sensitive data (e.g. viewing another worker's profile via an under-protected route) would not show up here — a limitation to keep in mind when investigating a suspected data-exposure incident.

## Personal data (GDPR-relevant, Germany-based business)

Worker profiles contain name (via Telegram), clothing sizes, birthday, GPS check-in/out locations, chat messages, and photos. All stored unencrypted-at-rest as flat JSON/files on the VPS disk. No formal data-retention policy beyond: chat messages (7-day retention, 200-message cap per `server-structure.md`), notification dedup entries (7-day TTL). No documented process for data subject access/deletion requests. This is a real gap for a Germany-based employer handling employee GPS/photo data — flag to the business owner if this becomes operationally relevant (e.g. an employee asks what data is held on them), don't silently build a deletion feature without that conversation.

## Known open items (not silently fixed, requires owner decision)

See [ROLES_AND_PERMISSIONS.md](ROLES_AND_PERMISSIONS.md) for endpoint-level findings from this recovery's audit pass, if any were flagged there.
