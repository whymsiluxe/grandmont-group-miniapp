# iPhone Screenshot Audit — 22.09.2026

Owner sent 20 real-device Telegram Mini App screenshots taken on iPhone.
Stage 0 (build-truth check) confirmed via `curl https://app.promonta.fun/api/health`
and `curl -I https://app.promonta.fun/app.html`: production correctly served
`c28ff97` (this session's own deploy) with `cache-control: no-store`, ruling out
a deployment-pipeline bug. Several screenshots were later confirmed to predate
that deploy (still showing `c23894d`-era behavior) — those are marked
**ALREADY FIXED ON MAIN** below rather than re-patched.

Branch: `screenshot-audit-2026-09-22`. Commits: `eef56d1`, `49b62a0`, `381ef7e`,
`b5af336`, `6917aad`, `467f608`. **Not deployed** — awaiting explicit owner
approval per the original instruction ("Do not deploy production without
explicit owner approval").

## Summary table

| Item | Symptom | Reproduced on main? | Root cause | File/function | Priority | Fix | Regression test |
|---|---|---|---|---|---|---|---|
| A | "Plan for tomorrow" alert with wrong/stale date | NO | N/A — date math already correct at the reported moment | `backend/daily_plan_cutoff_check.py` | P0 | Added `ref_id=target_date` so each business date gets its own dedup key (previously alerts for different dates could collapse into the same unresolved record) | `tests/test_business_date.py::EveningCutoffCheckTomorrowDateTests` |
| B | Same critical alert shown 2-3× in a row, "Принял" reappearing after ACK | YES | Backend `_create_critical_alert()` had no idempotency; frontend queue was wholesale-replaced every 15s poll with only `_criticalAlertModalOpen` as a guard | `backend/main.py::_create_critical_alert`, `frontend/js/critical-alerts.js` | P0 | Backend: stable dedup key `kind+target_user_id+ref_id`, returns existing unresolved alert instead of creating a duplicate. Frontend: queue merges by `id`, ACK removes by id (not `.shift()`), reconciles against server state after each poll | `tests/test_critical_alert_dedup.py`, `tests/test_critical_alerts_queue_frontend_contract.py`, `tests/node-critical-alerts-dedup.js` |
| C | Diagnostics: duplicated SHA (`c28ff97 · c28ff97`), raw English tokens (`not_configured`), green headline contradicting yellow warning rows | YES | No severity model (binary `ok`/`degraded` couldn't represent "core OK, integration missing"); frontend showed raw backend values and always duplicated `build_version` | `backend/system_status.py`, `frontend/js/diagnostics.js`, `frontend/js/profile.js` | P1 | 3-state severity model (`ok`/`warning`/`error`) distinguishing required vs optional subsystems; frontend localization map; SHA/version shown only when they actually differ | `tests/test_diagnostics_severity_model.py`, `tests/test_diagnostics_frontend_contract.py`, `tests/test_health.py` (updated), `tests/test_profile_ios_settings_contract.py` (updated) |
| D | Worker Card header/title under Dynamic Island; duplicate back arrow; sticky tabs misaligned after scroll | YES | `.worker-card-header` had zero `--tg-safe-top` padding (unlike every other header in the app); `#wc-back` wasn't in the native-back dedup list; `.wc-tabs` used a hardcoded `top: 48px` guess | `frontend/app.html` (`.worker-card-header`, `#wc-back`, `.wc-tabs`) | P1 | Added safe-area padding to the header; added `#wc-back` to the existing `body.tg-native-back` hide rule; `.wc-tabs` offset now uses the same safe-area term as the header | `tests/test_worker_card_safe_area_frontend_contract.py` |
| E | Objects list FAB overlapping last card's budget row | Partially — FAB position itself already fixed | `.objects-fab` already used measured `--app-bottom-nav-height` (pre-existing fix); but `#objects-cards` (`.cards`, no dedicated override) had **zero** bottom padding reserve, unlike `#view-tools .cards`/`#working-objects-slot` | `frontend/app.html` (`#objects-list-view .cards`) | P1 | Added the same measured-height padding reserve, sized to clear the FAB's own footprint | `tests/test_objects_frontend_contract.py::test_objects_cards_reserve_space_under_the_fab_and_bottom_nav` |
| F | Object Detail sticky zone-tabs header under Dynamic Island | **NOT REPRODUCIBLE** | `.form-header` (shared by all views, object-detail included) already carries `--tg-safe-top` padding; `#obj-detail-back` already deduped against Telegram's native BackButton (29.07); `#obj-detail-tabs` is a normal in-flow grid, not `position:sticky` — no sticky-header safe-area gap exists to fix. The embedded chat's `--obj-detail-chat-offset` already recomputes on scroll (pre-existing) | — | P1 | None needed | — |
| G | Object task Kanban causing horizontal page overflow at 390px | **NOT REPRODUCIBLE** | `.obj-task-kanban-columns` already scrolls horizontally inside its own container (`overflow-x: auto` + relative `minmax(218px, 82%)` column widths, no fixed px wider than viewport) — correct contained-scroll pattern | — | P1 | None needed; added a regression test to lock the property in | `tests/test_object_task_kanban_frontend_contract.py::test_kanban_columns_scroll_horizontally_inside_their_own_container` |
| — | Stage picker asks "Какой этап сегодня?" even with exactly one stage | YES (by design, 28.07) | Owner's explicit new request: single unambiguous stage shouldn't prompt | `frontend/js/worker-checkin-fab.js::_openStagePickerThenStart` | Owner request | Auto-starts the shift when `stages.length === 1`; picker still shown for 0 (need add-first-stage UI) or 2+ (real ambiguity) | `tests/test_checkin_frontend_contract.py::test_stage_picker_auto_selects_when_exactly_one_unambiguous_stage` |
| H | New Object: German placeholder "Straße, PLZ Ort" (looked like a forgotten localization); possible empty date-input UX issue; possible keyboard hiding submit button | Partially | Placeholder was literal untranslated label text, not an example value. Submit-bar's base CSS rule (used outside sheet contexts) hardcoded `bottom: 7.5rem`. New Object itself uses a sheet-scoped `position: sticky` override so keyboard-avoidance was never actually broken for it | `frontend/app.html` (`#new-obj-adresse` placeholder, `.form-submit-bar`) | P2 | Real German street/PLZ/city example placeholder; base submit-bar rule switched to measured nav height (defensive fix for any other consumer) | `tests/test_objects_frontend_contract.py::test_new_object_address_placeholder_*`, `::test_new_object_submit_bar_uses_measured_nav_height_not_a_magic_number` |
| H (date input) | Empty date-input visual state looked broken | **STILL OPEN** | Labels exist above both date fields; shared input sizing (padding/font-size) is uniform across all field types in CSS — could not reproduce a layout bug from source inspection alone | `frontend/app.html` (`input[type="date"]`) | P2 | Not patched — needs a real device screenshot to compare against before guessing at native date-picker rendering (iOS WebKit intrinsic sizing is not reliably predictable from CSS alone) | — |
| I | Feed/News author or comment name shown as raw ID | Partially | `list_feed_photos()` post author already resolved (prior commit); but `get_news_comments()` and `get_feed_photo_comments()` both returned each comment's `name` exactly as frozen at write time, with zero read-side resolution | `backend/main.py::get_news_comments`, `::get_feed_photo_comments` | P1 | Applied the same `_resolve_current_display_name()` resolver used everywhere else, before returning comments | `tests/test_identity_display_name_resolution.py::FeedCommentNameResolutionTests` |
| I (chips) | News category chip overflow | **NOT REPRODUCIBLE** | `.feed-news-category-filters` already has `overflow-x: auto` + own scroll container; container's own right padding provides scroll-end breathing room | — | P2 | None needed | — |
| I (news-card) | (Found during audit, not in original screenshot set) `.news-cat`/`.news-src` had no overflow guard | YES (latent) | `justify-content: space-between` flex row with no `min-width:0`, no `text-overflow` — a long scraped category/source name could overflow `.news-card` (no `overflow:hidden` of its own), risking page-level horizontal scroll | `frontend/app.html` (`.news-cat`, `.news-src`) | P1 | Added `min-width:0`/ellipsis/`nowrap` containment, same pattern as Kanban | `tests/test_feed_photo_frontend_contract.py::test_news_card_top_contains_long_category_or_source_names` |
| — | Calendar showing raw Telegram ID `5298622655` | **ALREADY FIXED ON MAIN** | `list_all_abwesenheit()` already resolves via `_resolve_current_display_name()` **before** the `ABWESENHEIT_PUBLIC_FIELDS` filter (prior commit `49b62a0`) — confirmed by direct code read | — | P0/P1 | None needed — screenshot predates the fix (stale build) | Covered by `tests/test_identity_display_name_resolution.py::AbwesenheitNameResolutionTests` |
| — | Chat showing raw Telegram ID `872079437` | **ALREADY FIXED ON MAIN** | `get_chat_messages()` already resolves via the same resolver, skipping `system` sender (prior commit `49b62a0`) | — | P0/P1 | None needed — screenshot predates the fix | Covered by `tests/test_identity_display_name_resolution.py::ChatNameResolutionTests` |
| — | Chat message avatar too small (20×20px) | YES | `.chat-msg-avatar` was 20×20px, confirmed too small on a real device; Feed already used 34×34 | `frontend/app.html` (`.chat-msg-avatar`) | P1 | Increased to 32×32px, font to 0.78rem | `tests/test_chat_frontend_contract.py` (appended) |
| — | Worker Start / Home CTA showed "Дом Мюллер" then still opened a 2-object picker | **ALREADY FIXED ON MAIN, likely stale build** | Not independently re-verified this session beyond source read — current main's shift-start flow already resolves via `resolveWorkerShiftState()`/shared object-picker logic per the Worker UX V2 architecture; screenshot's build predates the deploy | `frontend/js/worker-shift-state.js`, `frontend/js/home.js` | P1 | None applied — flagged for a manual on-device re-check after this branch deploys, since it wasn't independently reproduced from source alone | — |
| — | `--bottom-nav-safe-pad` used across 11 CSS rules but never actually set anywhere (found during this audit's investigation of Item F/G/Calendar) | YES (real, latent) | Dead CSS custom property — every read fell back to a flat guess (`7.5rem`) or, where no fallback existed, to 0 bottom padding (`body`, `#stages-view`, `#view-object-detail`) | `frontend/app.html` (11 sites) | P1 | Migrated all 11 usages to `--app-bottom-nav-height` (the variable this app actually measures via `_applyBottomNavHeight()`) | `tests/test_bottom_nav_safe_pad_dead_variable.py` (new), 3 pre-existing tests updated |
| — | Object Detail V2 Step 2 canonical DOM-mount conflict (owner architectural correction, not a screenshot) | N/A — preventive | `renderObjectStagesTab()` hardcodes `getElementById('obj-detail-panel-stages')` as its only mount; deleting `#stages-view` alone would not resolve a future conflict with `#obj-detail-panel-work` | `frontend/js/object-info.js::renderObjectStagesTab` | Architecture guard | No functional change — added a structural regression test asserting the single-mount invariant, so a future step-2/3 change can't silently duplicate stage markup into two panels before a real canonical-mount decision is made | `tests/test_object_detail_v2_zone_shell.py::test_stage_dom_ids_are_never_duplicated_across_panels` |

## Explicit non-fixes (per original instruction, out of scope)

- iOS system photo picker — not touched.
- Telegram-native Close/Back/menu controls — not touched (only internal
  duplicates of them were hidden).
- User-entered names (real free-text names workers typed themselves) — not
  touched, only the *resolution* of missing/placeholder names was fixed.
- Owner bottom-nav — screenshots are owner-role; worker-role nav untouched.

## Quality gate (final)

- `pytest .` — **1071 passed, 1 skipped, 0 failed** (baseline before this
  audit: 1056; net +15 new/updated regression tests across the 6 commits).
- `node --check` on every changed `.js` file — clean.
- Node runtime regression test (`tests/node-critical-alerts-dedup.js`, run via
  `tests/test_node_runtime_regressions.py`) — passes, actually executes the
  frontend dedup logic in a `vm` sandbox rather than only asserting source
  text.
- Route count: unchanged at 186 (only existing handler bodies edited, no
  routes added or removed).
- `git status` — clean on `screenshot-audit-2026-09-22` after each commit.
- Manual iPhone Telegram pass — **not performed this session** (no physical
  device access in this environment); the owner should do a real-device pass
  after reviewing this doc, particularly for the two **STILL OPEN**/
  **not independently reproduced** rows (New Object empty date-input, Worker
  Start stale-build re-check).

## Deployment status (updated 2026-09-22, post-merge)

- **Merged**: PR #4, `screenshot-audit-2026-09-22` → `main` (merge commit
  `63b9daee6441e77e7b4367f93d176bd7bd0fa1ff`).
- **GitHub CI**: green — 1071 passed, 1 skipped, 0 failed.
- **Production target SHA**: `63b9daee6441e77e7b4367f93d176bd7bd0fa1ff`, per
  `scripts/deploy.sh`'s own SHA-verification step on the VPS (step 14/14).
  This was confirmed by the deploy script's own output in this session, not
  by an independent external request — if you need to verify externally,
  `GET https://app.promonta.fun/api/health` should report this commit.
- **Manual iPhone Telegram verification**: **still pending.** This is the
  actual next gate, not another code audit. Recommended pass, in order:
  Profile → Diagnostics → Worker Card → Calendar → Chat → Objects → start a
  shift on an object with one stage (picker should NOT appear) → start on an
  object with 2+ stages (picker SHOULD appear) → trigger/ACK a Critical Alert
  → re-open the app (popup must not return) → scroll a long list to the very
  bottom. Specifically confirm on-device: an alert shows exactly once; the
  "tomorrow" date is correct near midnight; Chat/Calendar no longer show a
  raw Telegram ID; the 32px avatar looks right; Worker Card no longer sits
  under the Dynamic Island; the bottom nav doesn't cover anything; the old
  "Дом Мюллер → still asked to pick between 2 objects" screenshot no longer
  reproduces.
- **Object Detail V2 is not finished.** PR #4 only added the duplicate-DOM
  guard (`test_stage_dom_ids_are_never_duplicated_across_panels`). The actual
  canonical cutover (stages → work), porting the still-missing timer/pause/
  manual-time panels, and retiring the legacy `#stages-view` are a separate,
  later step — see `docs/OBJECT_DETAIL_V2_IMPLEMENTATION_PLAN.md`.
