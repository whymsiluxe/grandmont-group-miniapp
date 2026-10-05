> **SUPERSEDED 2026-09-18** — historical snapshot, kept for context/git-blame only, not maintained. See [BACKLOG.md](BACKLOG.md) for current state.

# READY FOR OWNER REVIEW — 09.09.2026 evening autonomous run

**Production SHA**: 29a2c4eb97aca9b8628100363d4e40709a31a589  
**Deployed**: yes — health check passed, SHA confirmed  
**Rollback backup**: /tmp/rollback_backup_20260911_114314

---

## What shipped (deployed, SHA + description)

| Commit | Description |
|--------|-------------|
| d53eef9 | **Item 3 fix**: Team-add popup opens instantly — removed blocking `/api/objects` fetch, use `_objDetailCurrentName` already in memory |
| acd8907 | **Item 7 feat**: Dashboard "Сообщения" tile now shows unread count badge (was populated in DOM but never filled) |
| cc63342 | **Item 5 feat**: When object is "Завершён" and assignment is blocked, form shows inline button "Перевести объект в В работе и назначить" — calls existing PATCH `/api/objects/{id}/status`, then auto-retries submit. No new routes. |
| b67d797 | **Item 2 fix**: Chat reply-bar positioning — was hidden behind composer after position:absolute change in 74b9e6f. Now also position:absolute above composer via `bottom: var(--chat-composer-height,64px)`. Same keyboard transform as voice-recording-bar. |
| f587226 | **Item 4 fix**: Assignment sheet (task-note textarea) no longer hides under keyboard — added `height: var(--tg-vp-height, 100dvh)` to `.bottom-sheet-overlay`, fixed `.assignment-sheet-panel` max-height to use live viewport height. Same pattern as `.obj-stage-add-sheet` which already worked. |
| 29a2c4e | docs: plan/rules/handoff/open-questions for this run |

---

## What's deployed vs. committed-only

All fixes are deployed.

---

## Still open

### Item 1 — Composer keyboard lag (pending owner live test)
Committed and deployed in previous session (7d0d258 + localStorage cache). No code changed in this run. Owner must test live: focus the chat input, type, close keyboard, open again. If lag still occurs → fallback: remove the predictive animation entirely (`transform: none` on `.chat-input-bar`, plain `bottom: var(--keyboard-inset, 0px)`).

### Item 6 — Worker profile "duplicate calendar"
Deferred — needs owner to clarify what to remove. Details in `docs/OPEN_QUESTIONS_09sep2026.md`. The relevant code is in `frontend/js/profile.js` worker-card section.

---

## Tests & route count at deploy

- 657 passed, 1 skipped, 50 warnings (unchanged from session start)
- Route count: 176 (unchanged — no new backend routes added)

---

## Notes for owner

- The bottom-sheet keyboard fix (item 4) applies to ALL `.bottom-sheet-overlay` instances, not just the task-note step — this means the team-add sheet, new-object sheet, and similar modals also benefit. Should be safe (same pattern as the stage-add sheet which already worked).
- The reply-bar fix (item 2) assumes the composer is ~64px tall (the existing `--chat-composer-height` fallback). If the bar still appears at the wrong position, the variable can be set by JS: `document.documentElement.style.setProperty('--chat-composer-height', inputBar.offsetHeight + 'px')` on resize.
