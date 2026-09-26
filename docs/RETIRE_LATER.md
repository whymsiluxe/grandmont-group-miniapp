# RETIRE_LATER — Grandmont Group rebrand, catalogued not deleted

Generated 2026-09-27, Wave K. Nothing in this list has been touched. All items are
safe rollback sources or genuinely idle leftovers from the promonta -> grandmont
migration (Waves A-J). Review and decide retirement timing separately — no
automatic cleanup will happen.

## 1. Old runtime directories (rollback sources — keep until confident, then archive/delete)

| Path | Size | Role |
|---|---|---|
| `/home/promonta/agent/miniapp` | 592M | Pre-Wave-A miniapp runtime (superseded by `/home/grandmont/agent/miniapp`) |
| `/home/promonta/agent/miniapp-repo` | 128M | Pre-Wave-D git checkout (superseded by `/home/grandmont/agent/miniapp-repo`) |
| `/home/promonta/agent/grandmont-group-crm` | 1.2G | Pre-Wave-D CRM checkout (superseded by `/home/grandmont/agent/grandmont-group-crm`) |
| `/home/promonta/agent/grandmont-group-website` | 2.0G | Website+CMS checkout — **never migrated to grandmont**, still the live source for `grandmont-group-website.service`/`grandmont-group-cms.service`/`grandmont-group-website-lead-cleanup.service` (all 3 intentionally left on `User=promonta` in Wave D, not stale) |

Note: within these, `.venv/bin/python3` symlinks in the old miniapp/CRM checkouts
already point at `/usr/bin/python3` (system Python, not a real venv) — they were
likely broken/unused before this migration even started, unrelated to Wave A-J.

## 2. Disabled old-name systemd units (on disk, disabled, not started)

22 units renamed in Wave C, old files kept for rollback per the "no destructive
action without explicit approval" rule:

`grandmont-analyze`, `grandmont-backup`, `grandmont-bot`, `grandmont-budget-check`,
`grandmont-cms`, `grandmont-competitors`, `grandmont-daily-plan-cutoff-evening`,
`grandmont-daily-plan-cutoff-morning`, `grandmont-digest`, `grandmont-followup`,
`grandmont-miniapp-cleanup`, `grandmont-miniapp`, `grandmont-night-task`,
`grandmont-reconcile`, `grandmont-report_weekly`, `grandmont-self_improve`,
`grandmont-system-scout`, `grandmont-watchdog`, `grandmont-weather-check`,
`grandmont-webhook`, `grandmont-website-lead-cleanup`, `grandmont-website`
(`.service` suffix on all; some also have a matching disabled `.timer`).

Live replacements are the `grandmont-group-*` versions of the same names.

## 3. Old sudoers file

`/etc/sudoers.d/promonta-miniapp-deploy` — superseded by
`/etc/sudoers.d/grandmont-miniapp-deploy` (least-privilege, single restart
command only). Old file still grants promonta the same rights; harmless while
promonta account itself stays active, but redundant once promonta is fully
retired.

## 4. Old MongoDB database

`promonta_konstruktion` — 13KB dataSize, superseded by `grandmont_group`
(Wave E). CRM has pointed at `grandmont_group` since Wave E; nothing writes to
the old DB anymore. Kept as rollback snapshot per explicit instruction (never
drop without separate approval).

## 5. Linux user `promonta`

Still active, not locked, not deleted (per explicit instruction throughout
Waves D-J). Currently still the sole owner/runner of:
- `grandmont-group-website.service`, `grandmont-group-cms.service`,
  `grandmont-group-website-lead-cleanup.service` (website/CMS checkout never
  migrated off `/home/promonta/agent/grandmont-group-website`)
- `grandmont-group-self_improve.service`, `grandmont-group-system-scout.service`,
  `grandmont-group-night-task.service` (read session history / `~/Projects`
  paths specific to promonta's account, deliberately not migrated)
- `grandmont-group-backup.service` (backs up `/home/promonta` itself — correct
  as-is)
- An apparently-idle `craco start` dev server for the CRM frontend (PID
  344736 and children) — not part of any deploy/prod path (prod frontend is
  a static build served by Caddy from `/var/www/grandmont-group-crm`), looks
  like a leftover manual dev session. Not stopped — could be someone's active
  work; flagging only.

## 6. Old-checkout node_modules/venvs (disk space, no functional risk)

- `/home/promonta/agent/grandmont-group-website/site/node_modules` — 507M
- `/home/promonta/agent/grandmont-group-website/cms/node_modules` — 922M

Both belong to directory tree #1 above (never migrated, still live for
website/cms services) — not orphaned, just large.

## What this list is NOT

- Not a deletion plan. Nothing here gets removed without a separate, explicit
  "yes, delete X" instruction per item.
- Not exhaustive for every possible legacy byte — it covers what Waves A-J
  actually produced or found. A full disk audit (docs/logs/misc scripts with
  historical Promonta mentions in comments) was intentionally out of scope —
  those are Category E (historical/comment) references, left alone throughout
  this migration.