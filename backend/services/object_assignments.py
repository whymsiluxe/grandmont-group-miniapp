"""Assignment orchestration for the objects domain.

Framework-agnostic application service. FastAPI/Pydantic stay in routes.objects;
runtime storage and cross-domain collaborators are injected by main.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


class ObjectAssignmentError(RuntimeError):
    """Expected application failure mapped to HTTP by the route adapter."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class ObjectAssignmentDeps:
    cached_get_used_range: Callable[..., Any]
    load_assignments: Callable[..., Any]
    load_worker_profiles: Callable[..., Any]
    load_abwesenheit: Callable[..., Any]
    load_roles: Callable[..., Any]
    load_checkin_meta: Callable[..., Any]
    update_json_transaction: Callable[..., Any]
    object_assignments_file: Callable[[], str]
    validate_date_str: Callable[..., Any]
    dates_overlap: Callable[..., bool]
    assignment_periods_overlap: Callable[..., bool]
    assignment_status: Callable[..., str]
    utcnow_iso: Callable[[], str]
    sanitize_display_name: Callable[..., str]
    object_history_worker_name: Callable[..., str]
    append_object_history_best_effort: Callable[..., Any]
    shadow_compare_assignments: Callable[..., Any] | None
    get_work_type: Callable[[str], dict | None]
    skill_display_name: Callable[[str], str]
    build_candidates: Callable[..., Any]
    new_id: Callable[[], str]


@dataclass(frozen=True)
class ObjectAssignmentService:
    deps: ObjectAssignmentDeps

    def _object_row(self, object_id: str) -> dict | None:
        rows = self.deps.cached_get_used_range("Объекты")
        if not rows:
            return None
        header, data = rows[0], rows[1:]
        for raw in data:
            row = dict(zip(header, raw))
            if str(row.get("ID объекта", "")) == str(object_id):
                return row
        return None

    def _validate_dates(self, date_from: str, date_to: str, *, allow_empty: bool) -> None:
        if date_from:
            self.deps.validate_date_str(date_from, "date_from")
        elif not allow_empty:
            raise ObjectAssignmentError(400, "date_from обязателен")
        if date_to:
            self.deps.validate_date_str(date_to, "date_to")
        elif not allow_empty:
            raise ObjectAssignmentError(400, "date_to обязателен")
        if date_from and date_to and date_from > date_to:
            raise ObjectAssignmentError(400, "date_from не может быть позже date_to")

    def my_assignments(self, user_id: str) -> dict:
        assignments = self.deps.load_assignments()
        uid = str(user_id)
        rows = self.deps.cached_get_used_range("Объекты")
        names: dict[str, str] = {}
        if rows:
            header, data = rows[0], rows[1:]
            for raw in data:
                obj = dict(zip(header, raw))
                oid = str(obj.get("ID объекта", ""))
                names[oid] = obj.get("Объект") or obj.get("Название") or obj.get("Адрес") or oid

        result = []
        for oid, entries in (assignments.items() if isinstance(assignments, dict) else []):
            for assignment in (entries if isinstance(entries, list) else []):
                if not isinstance(assignment, dict) or str(assignment.get("user_id", "")) != uid:
                    continue
                work_type_id = assignment.get("work_type_id", "")
                result.append({
                    "id": assignment.get("id", ""),
                    "object_id": oid,
                    "object_name": names.get(oid, oid),
                    "stage_id": assignment.get("stage_id", ""),
                    "work_type_id": work_type_id,
                    "work_type_name": self.deps.skill_display_name(work_type_id) if work_type_id else "",
                    "date_from": assignment.get("date_from", ""),
                    "date_to": assignment.get("date_to", ""),
                    "assigned_at": assignment.get("assigned_at", ""),
                    "status": self.deps.assignment_status(assignment),
                    "decline_reason": assignment.get("decline_reason", ""),
                    "task_note": assignment.get("task_note", ""),
                })
        result.sort(key=lambda item: item["date_from"] or "", reverse=True)
        if self.deps.shadow_compare_assignments:
            try:
                self.deps.shadow_compare_assignments(uid, result)
            except Exception:
                pass
        return {"assignments": result}

    def assign_user(self, object_id: str, payload: dict, user: dict) -> dict:
        key = str(object_id)
        uid = str(payload.get("user_id", ""))
        date_from = str(payload.get("date_from") or "")
        date_to = str(payload.get("date_to") or "")
        self._validate_dates(date_from, date_to, allow_empty=True)

        object_row = self._object_row(key)
        if object_row is None:
            raise ObjectAssignmentError(404, "Объект не найден")
        if object_row.get("Статус") == "Завершён":
            raise ObjectAssignmentError(400, "Объект завершён, назначение недоступно")

        work_type_id = str(payload.get("work_type_id") or "")
        if work_type_id:
            work_type = self.deps.get_work_type(work_type_id)
            if work_type is None or not work_type.get("active"):
                raise ObjectAssignmentError(400, "Неизвестный или неактивный вид работ")

        abwesenheit = self.deps.load_abwesenheit()
        result_holder: dict[str, Any] = {}

        def mutate(assignments):
            if key not in assignments:
                assignments[key] = []
            already = any(
                isinstance(item, dict)
                and str(item.get("user_id", "")) == uid
                and item.get("stage_id", "") == payload.get("stage_id", "")
                and self.deps.assignment_status(item) != "declined"
                for item in assignments[key]
            )
            if already:
                raise ObjectAssignmentError(409, "Это назначение уже существует")
            for entry in abwesenheit:
                if not isinstance(entry, dict):
                    continue
                if str(entry.get("user_id")) != uid or entry.get("status") != "approved":
                    continue
                if self.deps.dates_overlap(
                    date_from, date_to, entry.get("date_from", ""), entry.get("date_to", "")
                ):
                    raise ObjectAssignmentError(
                        409,
                        f"Работник недоступен ({entry.get('reason', 'отсутствие')}) "
                        f"{entry.get('date_from')} — {entry.get('date_to')}",
                    )
            for other_oid, other_list in assignments.items():
                if other_oid == key or not isinstance(other_list, list):
                    continue
                for item in other_list:
                    if not isinstance(item, dict):
                        continue
                    if str(item.get("user_id", "")) != uid or self.deps.assignment_status(item) == "declined":
                        continue
                    if self.deps.assignment_periods_overlap(
                        {"date_from": date_from, "date_to": date_to}, item
                    ):
                        raise ObjectAssignmentError(
                            409,
                            f"Этот работник уже назначен на объект {other_oid} "
                            f"на период {item.get('date_from') or '(без даты)'} — "
                            f"{item.get('date_to') or '(без даты)'}",
                        )

            assigned_at = self.deps.utcnow_iso()
            assignment = {
                "id": self.deps.new_id(),
                "user_id": uid,
                "stage_id": payload.get("stage_id", ""),
                "work_type_id": work_type_id,
                "date_from": date_from,
                "date_to": date_to,
                "assigned_at": assigned_at,
                "pending_since": assigned_at,
                "status": "pending",
                "decline_reason": "",
                "responded_at": "",
                "task_note": str(payload.get("task_note") or "").strip()[:500],
            }
            assignments[key].append(assignment)
            result_holder["assignment"] = assignment

        self.deps.update_json_transaction(self.deps.object_assignments_file(), {}, mutate)
        created = result_holder.get("assignment") or {}
        if created:
            worker_name = self.deps.object_history_worker_name(created.get("user_id", ""))
            work_label = (
                self.deps.skill_display_name(created.get("work_type_id", ""))
                if created.get("work_type_id")
                else created.get("stage_id", "")
            )
            subtitle = " · ".join(
                part for part in (worker_name, work_label, created.get("task_note", "")) if part
            )
            self.deps.append_object_history_best_effort(
                key,
                "worker_assigned",
                f"Назначен работник: {worker_name}",
                user=user,
                subtitle=subtitle,
                meta={
                    "assignment_id": created.get("id", ""),
                    "worker_id": created.get("user_id", ""),
                    "work_type_id": created.get("work_type_id", ""),
                    "stage_id": created.get("stage_id", ""),
                    "date_from": created.get("date_from", ""),
                    "date_to": created.get("date_to", ""),
                },
            )
        return {"status": "ok"}

    def unassign_user(self, object_id: str, user_id: str) -> dict:
        key = str(object_id)
        uid = str(user_id)
        result_holder: dict[str, bool] = {}

        def mutate(assignments):
            entries = assignments.get(key, [])
            active_indices = [
                index
                for index, item in enumerate(entries)
                if isinstance(item, dict)
                and str(item.get("user_id", "")) == uid
                and self.deps.assignment_status(item) != "declined"
            ]
            if not active_indices:
                result_holder["not_found"] = True
                return
            if len(active_indices) > 1:
                result_holder["multiple"] = True
                return
            target_index = active_indices[0]
            assignments[key] = [item for index, item in enumerate(entries) if index != target_index]

        self.deps.update_json_transaction(self.deps.object_assignments_file(), {}, mutate)
        if result_holder.get("not_found"):
            raise ObjectAssignmentError(404, "Активное назначение этого работника не найдено")
        if result_holder.get("multiple"):
            raise ObjectAssignmentError(409, "У работника несколько назначений. Используйте assignment_id.")
        return {"status": "ok"}

    def update_assignment(self, object_id: str, assignment_id: str, updates: dict) -> dict:
        key = str(object_id)
        clean_updates = dict(updates)
        if "task_note" in clean_updates and clean_updates["task_note"] is not None:
            clean_updates["task_note"] = str(clean_updates["task_note"]).strip()[:500]
        if "work_type_id" in clean_updates and clean_updates["work_type_id"] is not None:
            work_type = self.deps.get_work_type(clean_updates["work_type_id"])
            if work_type is None or not work_type.get("active"):
                raise ObjectAssignmentError(400, "Неизвестный или неактивный вид работ")
        if "date_from" in clean_updates and clean_updates["date_from"] is not None:
            self.deps.validate_date_str(clean_updates["date_from"], "date_from")
        if "date_to" in clean_updates and clean_updates["date_to"] is not None:
            self.deps.validate_date_str(clean_updates["date_to"], "date_to")

        roles = self.deps.load_roles()
        object_row = self._object_row(key)
        abwesenheit = self.deps.load_abwesenheit()
        result_holder: dict[str, Any] = {}

        def mutate(assignments):
            entries = assignments.get(key, [])
            target = next(
                (item for item in entries if isinstance(item, dict) and item.get("id") == assignment_id),
                None,
            )
            if target is None:
                result_holder["not_found"] = True
                return

            merged_work_type = clean_updates.get("work_type_id", target.get("work_type_id"))
            merged_date_from = clean_updates.get("date_from", target.get("date_from", ""))
            merged_date_to = clean_updates.get("date_to", target.get("date_to", ""))
            if merged_date_from and merged_date_to and merged_date_from > merged_date_to:
                result_holder["error"] = "date_from не может быть позже date_to"
                return

            uid = str(target.get("user_id"))
            role = roles.get(uid)
            if role != "worker":
                result_holder["error"] = f"Пользователь {uid} не является Worker (роль: {role})"
                return
            if object_row is None:
                result_holder["error"] = "Объект не найден"
                return

            if any(
                isinstance(entry, dict)
                and str(entry.get("user_id")) == uid
                and entry.get("status") == "approved"
                and self.deps.dates_overlap(
                    merged_date_from,
                    merged_date_to,
                    entry.get("date_from", ""),
                    entry.get("date_to", ""),
                )
                for entry in abwesenheit
            ):
                result_holder["error"] = "Работник недоступен (отсутствие) на этот период"
                return

            overlap = False
            for other_oid, other_list in assignments.items():
                if not isinstance(other_list, list):
                    continue
                for item in other_list:
                    if not isinstance(item, dict):
                        continue
                    if item.get("id") == assignment_id:
                        continue
                    if str(item.get("user_id")) != uid or self.deps.assignment_status(item) == "declined":
                        continue
                    if other_oid == key:
                        continue
                    if self.deps.assignment_periods_overlap(
                        {"date_from": merged_date_from, "date_to": merged_date_to}, item
                    ):
                        overlap = True
                        break
                if overlap:
                    break
            if overlap:
                result_holder["error"] = "Пересекается с другим назначением этого работника на другом объекте"
                return

            was_accepted = self.deps.assignment_status(target) == "accepted"
            significant_change = (
                merged_work_type != target.get("work_type_id")
                or merged_date_from != target.get("date_from", "")
                or merged_date_to != target.get("date_to", "")
                or (
                    "task_note" in clean_updates
                    and clean_updates["task_note"] != target.get("task_note", "")
                )
            )
            updated_at = self.deps.utcnow_iso()
            target.update({key_: value for key_, value in clean_updates.items() if value is not None})
            if was_accepted and significant_change:
                target["status"] = "pending"
                target["decline_reason"] = ""
                target["responded_at"] = ""
                target["pending_since"] = updated_at
            target["updated_at"] = updated_at
            result_holder["ok"] = True

        self.deps.update_json_transaction(self.deps.object_assignments_file(), {}, mutate)
        if result_holder.get("not_found"):
            raise ObjectAssignmentError(404, "Назначение не найдено")
        if result_holder.get("error"):
            detail = result_holder["error"]
            status = 409 if "Пересекается" in detail or "недоступен" in detail else 400
            raise ObjectAssignmentError(status, detail)
        return {"status": "ok"}

    def delete_assignment(self, object_id: str, assignment_id: str) -> dict:
        key = str(object_id)
        found: dict[str, bool] = {}

        def mutate(assignments):
            entries = assignments.get(key, [])
            if not any(
                isinstance(item, dict) and item.get("id") == assignment_id
                for item in entries
            ):
                return
            found["ok"] = True
            assignments[key] = [
                item
                for item in entries
                if not (isinstance(item, dict) and item.get("id") == assignment_id)
            ]

        self.deps.update_json_transaction(self.deps.object_assignments_file(), {}, mutate)
        if not found.get("ok"):
            raise ObjectAssignmentError(404, "Назначение не найдено")
        return {"status": "ok"}

    def respond_to_assignment(
        self,
        object_id: str,
        user_id: str,
        *,
        assignment_id: str,
        accept: bool,
        decline_reason: str,
    ) -> dict:
        key = str(object_id)
        uid = str(user_id)
        if not accept and not decline_reason.strip():
            raise ObjectAssignmentError(400, "Укажите причину отказа")

        def mutate(assignments):
            entries = assignments.get(key, [])
            pending = [
                item
                for item in entries
                if isinstance(item, dict)
                and str(item.get("user_id", "")) == uid
                and self.deps.assignment_status(item) == "pending"
            ]
            if assignment_id:
                target = next((item for item in pending if item.get("id") == assignment_id), None)
            else:
                if len(pending) > 1:
                    raise ObjectAssignmentError(
                        409,
                        "Несколько ожидающих назначений. Укажите assignment_id.",
                    )
                target = pending[0] if pending else None
            if not target:
                raise ObjectAssignmentError(404, "Ожидающее назначение не найдено")
            target["status"] = "accepted" if accept else "declined"
            target["decline_reason"] = decline_reason.strip()[:500] if not accept else ""
            target["responded_at"] = self.deps.utcnow_iso()
            return target

        return self.deps.update_json_transaction(self.deps.object_assignments_file(), {}, mutate)

    def assignment_candidates(
        self, object_id: str, work_type_id: str, date_from: str, date_to: str
    ):
        if not self.deps.sanitize_display_name(work_type_id, ""):
            raise ObjectAssignmentError(400, "work_type_id обязателен")
        self._validate_dates(date_from, date_to, allow_empty=False)
        roles = self.deps.load_roles()
        profiles = self.deps.load_worker_profiles()
        worker_ids = {uid for uid, role in roles.items() if role == "worker"}
        workers = []
        for uid in worker_ids:
            profile = profiles.get(uid, {}) if isinstance(profiles, dict) else {}
            if not isinstance(profile, dict):
                profile = {}
            workers.append({
                "user_id": uid,
                "name": self.deps.sanitize_display_name(profile.get("name"), uid),
                "has_avatar": bool(profile.get("avatar")),
                "profile": profile,
            })
        return self.deps.build_candidates(
            work_type_id,
            object_id,
            date_from,
            date_to,
            workers,
            self.deps.load_assignments(),
            self.deps.load_abwesenheit(),
            self.deps.load_checkin_meta(),
        )

    def batch_assign(self, object_id: str, payload: dict, user: dict) -> dict:
        work_type_ids = list(dict.fromkeys(payload.get("work_type_ids") or []))
        if not work_type_ids:
            raise ObjectAssignmentError(400, "Укажите хотя бы один вид работ")
        work_types: dict[str, dict] = {}
        for work_type_id in work_type_ids:
            work_type = self.deps.get_work_type(work_type_id)
            if work_type is None or not work_type.get("active"):
                raise ObjectAssignmentError(400, "Неизвестный или неактивный вид работ")
            work_types[work_type_id] = work_type

        object_row = self._object_row(str(object_id))
        if object_row is None:
            raise ObjectAssignmentError(404, "Объект не найден")
        if object_row.get("Статус") == "Завершён":
            raise ObjectAssignmentError(400, "Объект завершён, назначение недоступно")

        user_ids = list(dict.fromkeys(str(uid) for uid in (payload.get("user_ids") or [])))
        if not user_ids:
            raise ObjectAssignmentError(400, "Укажите хотя бы одного работника")
        date_from = str(payload.get("date_from") or "")
        date_to = str(payload.get("date_to") or "")
        self._validate_dates(date_from, date_to, allow_empty=False)
        task_note = str(payload.get("task_note") or "").strip()[:500]

        roles = self.deps.load_roles()
        for uid in user_ids:
            role = roles.get(uid)
            if role is None:
                raise ObjectAssignmentError(400, f"Пользователь {uid} не найден в списке доступа")
            if role != "worker":
                raise ObjectAssignmentError(
                    400, f"Пользователь {uid} не является Worker (роль: {role})"
                )

        abwesenheit = self.deps.load_abwesenheit()
        key = str(object_id)
        result_holder: dict[str, list] = {"created": [], "skipped": []}

        def mutate(assignments):
            if key not in assignments:
                assignments[key] = []
            created, skipped = [], []
            for uid in user_ids:
                for work_type_id in work_type_ids:
                    duplicate = any(
                        isinstance(item, dict)
                        and str(item.get("user_id", "")) == uid
                        and item.get("work_type_id") == work_type_id
                        and self.deps.assignment_status(item) != "declined"
                        and self.deps.dates_overlap(
                            date_from,
                            date_to,
                            item.get("date_from", ""),
                            item.get("date_to", ""),
                        )
                        for item in assignments[key]
                    )
                    if duplicate:
                        skipped.append({
                            "user_id": uid,
                            "work_type_id": work_type_id,
                            "reason": "overlap",
                        })
                        continue

                    absence_hit = any(
                        isinstance(entry, dict)
                        and str(entry.get("user_id")) == uid
                        and entry.get("status") == "approved"
                        and self.deps.dates_overlap(
                            date_from,
                            date_to,
                            entry.get("date_from", ""),
                            entry.get("date_to", ""),
                        )
                        for entry in abwesenheit
                    )
                    if absence_hit:
                        skipped.append({
                            "user_id": uid,
                            "work_type_id": work_type_id,
                            "reason": "absence",
                        })
                        continue

                    cross_object_hit = False
                    for other_oid, other_list in assignments.items():
                        if other_oid == key or not isinstance(other_list, list):
                            continue
                        if any(
                            isinstance(item, dict)
                            and str(item.get("user_id", "")) == uid
                            and self.deps.assignment_status(item) != "declined"
                            and self.deps.assignment_periods_overlap(
                                {"date_from": date_from, "date_to": date_to}, item
                            )
                            for item in other_list
                        ):
                            cross_object_hit = True
                            break
                    if cross_object_hit:
                        skipped.append({
                            "user_id": uid,
                            "work_type_id": work_type_id,
                            "reason": "overlap",
                        })
                        continue

                    assignment_id = self.deps.new_id()
                    assigned_at = self.deps.utcnow_iso()
                    assignments[key].append({
                        "id": assignment_id,
                        "user_id": uid,
                        "stage_id": work_types[work_type_id]["name"],
                        "work_type_id": work_type_id,
                        "date_from": date_from,
                        "date_to": date_to,
                        "assigned_at": assigned_at,
                        "pending_since": assigned_at,
                        "status": "pending",
                        "decline_reason": "",
                        "responded_at": "",
                        "task_note": task_note,
                        "created_by": str(user["id"]),
                    })
                    created.append({
                        "user_id": uid,
                        "work_type_id": work_type_id,
                        "assignment_id": assignment_id,
                    })
            result_holder["created"] = created
            result_holder["skipped"] = skipped

        self.deps.update_json_transaction(self.deps.object_assignments_file(), {}, mutate)
        if not result_holder["created"]:
            raise ObjectAssignmentError(409, "Ни одно назначение не создано (все пропущены)")

        for created in result_holder["created"]:
            worker_name = self.deps.object_history_worker_name(created.get("user_id", ""))
            work_type_id = created.get("work_type_id", "")
            work_label = work_types.get(work_type_id, {}).get("name") or (
                self.deps.skill_display_name(work_type_id) if work_type_id else ""
            )
            subtitle = " · ".join(part for part in (worker_name, work_label, task_note) if part)
            self.deps.append_object_history_best_effort(
                key,
                "worker_assigned",
                f"Назначен работник: {worker_name}",
                user=user,
                subtitle=subtitle,
                meta={
                    "assignment_id": created.get("assignment_id", ""),
                    "worker_id": created.get("user_id", ""),
                    "work_type_id": work_type_id,
                    "date_from": date_from,
                    "date_to": date_to,
                },
            )
        return result_holder
