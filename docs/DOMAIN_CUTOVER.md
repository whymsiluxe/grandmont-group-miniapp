# Domain Cutover Checklist — Grandmont Group

Prepared 2026-09-27, Wave L. **No new domain is chosen or invented here.** This
is a checklist of exactly what needs to change, and where, once a real
`grandmont-group.<tld>` (or whatever the owner registers) domain exists and DNS
is pointed at this VPS (162.55.53.147). Until then, everything below stays on
`*.promonta.fun` — that is a deliberate, working, temporary state, not a bug.

## Current temporary hostnames (Caddy, `/etc/caddy/Caddyfile`)

| Current | Serves | Cutover target |
|---|---|---|
| `app.promonta.fun` | Mini App (Telegram WebApp) | `app.<new-domain>` |
| `crm.promonta.fun` | CRM (React frontend + FastAPI backend) | `crm.<new-domain>` (or a non-public subdomain — CRM is internal-only) |
| `grandmont.promonta.fun` | Website staging | `<new-domain>` (apex/www, once it becomes canonical) |

## Code references to update at cutover time

1. **`grandmont-group-website/site/src/lib/seo/site-config.ts`** (line ~27-28):
   the `DOMAIN_COMPAT_PENDING` comment marker and the `url` field — currently a
   placeholder (`grandmont-group.de`, never registered, never used as a live
   URL). Replace with the real canonical URL once chosen.
2. **`grandmont-group-website/site/src/lib/crm/client.ts`** (line 19):
   `CRM_BASE_URL` defaults to `https://crm.promonta.fun/api` — either update the
   default or (cleaner) set `CRM_BASE_URL` explicitly in the website's
   production `.env` and leave the code default alone as a dev/fallback value.
3. **Miniapp `angebot_free.js`** (`LEGACY_CONTACT_EMAIL = 'anfragen@promonta-bau.de'`):
   this is the sender address on generated Angebote. **Do not touch
   `promonta-bau.de` itself** (hard exclusion, separate real business) — this is
   only the fallback used when `GRANDMONT_GROUP_CONTACT_EMAIL` isn't set. Once
   a real Grandmont Group contact email exists (likely `@<new-domain>`), set
   `GRANDMONT_GROUP_CONTACT_EMAIL` in `/etc/claude-agent.env` (or the miniapp's
   own env) — no code change needed, the fallback is only used if that var is
   absent.
4. **Miniapp `rechnung.js`** (`GRANDMONT_GROUP_LOGO_PATH`): already
   env-overridable, defaults to a local `grandmont-group-logo.png`. No domain
   dependency, listed here only because it's adjacent branding config someone
   doing a full cutover pass will want to double check alongside the domain.

## Caddy config changes

- Add new `server_name { ... }` blocks (or edit the existing 3) for the new
  hostnames, matching the same `reverse_proxy`/`handle`/header structure
  already in place — don't redesign the routing, just retarget the `Host`
  match.
- Get TLS certs issued for the new hostnames (Caddy's automatic HTTPS handles
  this on first request if DNS is already pointed correctly — no manual
  certbot step expected).
- Decide the `*.promonta.fun` hostnames' fate at cutover time: keep them as
  redirects to the new domain (recommended, avoids breaking old links/bookmarks
  and any Telegram WebApp URL cached by Telegram's client), or retire them —
  that's an explicit decision for the owner, not automated here.

## Telegram Bot / WebApp

The Mini App's Telegram WebApp URL (configured via BotFather, `/setmenubutton`
or similar) currently points at `https://app.promonta.fun/app.html`. This must
be updated in BotFather **manually** (no API for this that doesn't also touch
bot identity) once the new `app.<new-domain>` is live and confirmed working —
do this LAST, after the new hostname is verified end-to-end, since changing it
prematurely breaks the live bot for all users immediately.

## Order of operations (recommended, not enforced by this document)

1. Register domain, point DNS at 162.55.53.147.
2. Add new Caddy hostname blocks, verify TLS issues correctly, verify each
   service responds identically on the new hostname (side-by-side with the old
   one — don't remove old blocks yet).
3. Update the 2 website code references (`site-config.ts`, `crm/client.ts`) and
   any env vars (`CRM_BASE_URL`, `GRANDMONT_GROUP_CONTACT_EMAIL`), redeploy.
4. Full smoke test on new hostnames (same checks used throughout Waves A-J:
   health endpoints, lead webhook + Telegram alert + idempotency, PDF
   generation, login/session).
5. Only once step 4 passes: update the Telegram Bot's WebApp URL in BotFather.
6. Decide old-hostname fate (redirect vs retire) and act on that decision.
7. Update this document's "current temporary hostnames" table to reflect the
   new reality, or delete it once cutover is fully complete and stable.

## What this document deliberately does NOT do

- Does not invent, suggest, or reserve a domain name.
- Does not touch `promonta-bau.de` (hard exclusion, unrelated real business).
- Does not perform any of the steps above — this is a checklist for when a
  domain exists, not an action taken now.