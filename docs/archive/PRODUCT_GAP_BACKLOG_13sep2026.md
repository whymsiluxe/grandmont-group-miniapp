# Product Gap Backlog - 2026-09-13

Source: owner-pasted gap list plus current repo/docs context. This is not the immediate visual polish task. It is the product roadmap backlog to feed later autonomous stages.

## P1 - Operational Gaps That Affect Daily Use

1. Offline finish/check-in outbox.
   - Worker can take photos and write a report with bad internet.
   - App stores locally and retries later.
   - Must preserve idempotency keys and avoid duplicate finish submissions.

2. Frontend API timeout/retry layer.
   - Shared `api()` should support timeout, abort, retry where safe, and consistent user-visible retry states.
   - Must not create duplicate writes for POST unless idempotency is present.

3. Server-side DailyPlan validation at finish.
   - Backend must verify the submitted `daily_plan_id` belongs to this worker and accepted context.
   - Do not trust client-provided plan identity.

4. Per-worker DailyPlan acknowledgment.
   - Store `acknowledged_by` or equivalent per assigned worker.
   - Manager cockpit should show who accepted, who ignored, and escalation at 2h/4h.

5. Owner daily cockpit.
   - Morning summary, active shifts, not-started workers, overdue/unconfirmed tasks, material/delay risks.
   - This overlaps with Stage 3 of `UNIFIED_AUTONOMOUS_MASTER_PLAN_13sep2026.md`.

6. Human-readable object history.
   - Object-scoped timeline: status changed, worker assigned, stage completed, document uploaded, defect created, finish submitted.
   - Use audit log only as raw source if useful; UI should be business-readable.

## P2 - High-Value Product UI

1. Drag/drop assignment from worker to object.
   - Current Assignment Sheet exists.
   - Desired: drag worker onto object -> sheet opens with from/to/task/work type.

2. Budget dashboard inside object.
   - Donut/chart: budget, spent, remaining, risk.
   - Should sit inside object cockpit, not global dashboard clutter.

3. Object task Kanban.
   - Needs/tasks exist.
   - Desired board: To Do -> In Progress -> Done with drag/drop and status rules.

4. Document gallery/preview grid.
   - Upload/view exists.
   - Desired grid with PDF/photo previews, object-scoped filters, quick open/share/delete.

5. Extended calendar.
   - Week/month/year, drag/drop event moves, bottom sheet on day tap.
   - Current calendar/statistics are partial.

6. Interactive Stundenzettel graphs.
   - Hours/day/object charts, not just stats rows.

7. Dashboard mini charts.
   - KPI tiles should show trends/sparklines: hours up/down, defects, plan completion.

8. Tool booking by availability calendar.
   - Tools module exists.
   - Desired booking for date/object/worker, conflict prevention, upcoming reservations.

9. News category/tag filtering.
   - Current feed has news/saved/comments/reactions.
   - Desired category/tag filter and maybe mute/unmute categories.

10. Swipe-to-reply in chat.
    - Reply exists.
    - Desired: swipe message right -> reply, like Telegram/WhatsApp.

## P3 - Larger Platform / Architecture

1. CRM block.
   - Customers, leads, contacts, deals/objects, communication history, documents.

2. Google Sheets write-back for DailyPlan.
   - Current DailyPlan is local.
   - Needs durable sync model before enabling.

3. Durable sync queue for writes to external systems.
   - Required before Sheets write-back or any fragile external write path.

4. Contract RED risk.
   - Calculate true deadline risk from contract finish date and actual object progress.

5. Crew productivity accuracy.
   - Fix paths where `crew_size` is assumed as 1 for multi-worker objects.

6. Real staging environment.
   - Current docs say production only.
   - Need staging close to production.

7. Full automated E2E.
   - Scenario: owner assigns worker -> worker accepts -> starts shift -> completes DailyPlan -> photos -> finishes -> owner sees result.
   - Add Telegram/WebView realism where feasible.

8. Backend `main.py` domain split.
   - Auth, objects, workers, check-in, feed, chat, production control, contracts.

9. Frontend thin-shell split.
   - `app.html`, `home.js`, and `chat.js` are still too large.
   - Move toward components/modules.

10. PostgreSQL migration.
    - Not urgent at current scale, but flat JSON will become limiting.

11. Self-contained server install.
    - External `create_object.py` / `create_object_folder.py` dependency must be moved into repo or documented as official external dependency.

12. CI final hardening.
    - Ensure backend/core, manifest, and JS subdirs are covered in GitHub Actions.

## Product Decision Needed

Can a worker start a shift without a published DailyPlan?

Current behavior: allowed.

Options:

- Strict: block start without DailyPlan.
- Flexible: allow start, but show owner alert and require worker reason.
- Hybrid recommended: allow only if owner has not published a plan by a cutoff time or worker explicitly chooses "start without plan" with reason. Manager cockpit surfaces this as a warning.
