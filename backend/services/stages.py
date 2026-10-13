"""Business orchestration for stages, roadmaps, and stage-change requests.

This layer is deliberately framework-agnostic: no FastAPI/Pydantic imports and no
back-edge to main.py or routes. HTTP authentication/DTO handling stays in
routes/stages.py; persistence collaborators are injected by the composition root.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


class StageServiceError(RuntimeError):
    """Expected application failure that the HTTP adapter maps to a status."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class StageServiceDeps:
    cached_get_used_range: Callable[..., Any]
    load_repo_objekte_lib: Callable[..., Any]
    roadmap_lib: Callable[..., Any]
    safe_load_json: Callable[..., Any]
    update_json_transaction: Callable[..., Any]
    business_today_str: Callable[..., str]
    append_object_history_best_effort: Callable[..., Any]
    get_worker_profile: Callable[..., dict]
    sanitize_display_name: Callable[..., str]
    load_roles: Callable[..., dict]
    create_critical_alert: Callable[..., dict]
    send_telegram_message: Callable[..., Any]


@dataclass(frozen=True)
class StageService:
    deps: StageServiceDeps

    def cached_all_stages(self, object_id: str) -> list:
        o = self.deps.load_repo_objekte_lib()
        values = self.deps.cached_get_used_range("Этапы")
        if not values:
            return []
        headers = values[0]
        rows = []
        normalized_object_id = object_id.strip().upper()
        for row_num, row in enumerate(values[1:], start=2):
            if row and row[0].strip().upper() == normalized_object_id:
                stage = o._row_to_dict(headers, row)
                stage["_row"] = row_num
                rows.append(stage)
        rows.sort(key=lambda stage: int(stage.get("№ этапа") or 0))
        return rows

    def load_roadmap_store(self) -> dict:
        rl = self.deps.roadmap_lib()
        return self.deps.safe_load_json(rl.ROADMAP_FILE, rl._default_store())

    def load_stage_requests(self) -> list:
        rl = self.deps.roadmap_lib()
        return self.deps.safe_load_json(rl.STAGE_REQUESTS_FILE, rl._default_requests())

    def find_stage_by_row(self, object_id: str, row_num: int) -> dict:
        o = self.deps.load_repo_objekte_lib()
        stages = o.all_stages(object_id)
        stage = next((item for item in stages if item["_row"] == row_num), None)
        if not stage:
            raise StageServiceError(404, "Этап не найден")
        return stage

    def list_stages(self, object_id: str) -> dict:
        return {"stages": self.cached_all_stages(object_id)}

    def create_stage(self, object_id: str, name: str, description: str) -> dict:
        o = self.deps.load_repo_objekte_lib()
        clean_name = name.strip()
        if not clean_name:
            raise StageServiceError(400, "Name erforderlich")
        stage_num = o.add_stage(object_id, clean_name, description.strip()[:2000])
        o.sync_current_stage(object_id)
        return {"stage_num": stage_num}

    def update_stage_description(self, object_id: str, row_num: int, description: str) -> dict:
        # Bind the physical Sheets row to the object path before mutating it. The
        # legacy endpoint trusted row_num after object-level auth, so an object/row
        # mismatch could target another object's row.
        self.find_stage_by_row(object_id, row_num)
        o = self.deps.load_repo_objekte_lib()
        try:
            o.update_stage_description(row_num, description.strip()[:2000])
        except ValueError as exc:
            raise StageServiceError(404, str(exc)) from exc
        return {"status": "ok"}

    def update_stage_status(self, object_id: str, row_num: int, status: str, user: dict) -> dict:
        o = self.deps.load_repo_objekte_lib()
        try:
            stage_before = self.find_stage_by_row(object_id, row_num)
        except StageServiceError:
            stage_before = {}
        try:
            o.update_stage_status(row_num, status, self.deps.business_today_str())
        except ValueError as exc:
            raise StageServiceError(400, str(exc)) from exc
        o.sync_current_stage(object_id)

        stage_name = stage_before.get("Название этапа") or f"#{row_num}"
        old_status = stage_before.get("Статус", "")
        subtitle = (
            f"{stage_name} · {old_status} -> {status}"
            if old_status
            else f"{stage_name} · {status}"
        )
        self.deps.append_object_history_best_effort(
            object_id,
            "stage_status_changed",
            "Статус этапа изменён",
            user=user,
            subtitle=subtitle,
            meta={
                "row_num": row_num,
                "stage_name": stage_name,
                "old_status": old_status,
                "new_status": status,
            },
        )
        return {"status": "ok"}

    def remove_stage(self, object_id: str, row_num: int) -> dict:
        o = self.deps.load_repo_objekte_lib()
        try:
            o.delete_stage(object_id, row_num)
        except ValueError as exc:
            raise StageServiceError(404, str(exc)) from exc
        o.sync_current_stage(object_id)
        return {"status": "ok"}

    def swap_stage(self, object_id: str, row_num: int, row_num_b: int) -> dict:
        o = self.deps.load_repo_objekte_lib()
        try:
            o.swap_stage_order(object_id, row_num, row_num_b)
        except ValueError as exc:
            raise StageServiceError(400, str(exc)) from exc
        return {"status": "ok"}

    def complete_stage(self, object_id: str, row_num: int, user: dict) -> dict:
        o = self.deps.load_repo_objekte_lib()
        try:
            stage_before = self.find_stage_by_row(object_id, row_num)
        except StageServiceError:
            stage_before = {}
        try:
            o.worker_complete_stage(
                object_id,
                row_num,
                str(user["id"]),
                self.deps.business_today_str(),
            )
        except ValueError as exc:
            raise StageServiceError(400, str(exc)) from exc

        stage_name = stage_before.get("Название этапа") or f"#{row_num}"
        self.deps.append_object_history_best_effort(
            object_id,
            "stage_completed",
            "Этап завершён",
            user=user,
            subtitle=stage_name,
            meta={"row_num": row_num, "stage_name": stage_name},
        )
        return {"status": "ok"}

    def set_stage_blocker(
        self,
        object_id: str,
        row_num: int,
        user: dict,
        *,
        quick_reason: str,
        comment: str,
        photo_url: str,
        who_decides: str,
        expected_date: str,
    ) -> dict:
        if not (quick_reason.strip() or comment.strip()):
            raise StageServiceError(400, "Укажите причину")
        rl = self.deps.roadmap_lib()
        stage = self.find_stage_by_row(object_id, row_num)
        stage_key = stage["ID строки этапа"]

        def mutate(store):
            return rl.set_stage_block_meta(
                store,
                stage_key,
                None,
                str(user["id"]),
                quick_reason=quick_reason,
                comment=comment,
                photo_url=photo_url,
                who_decides=who_decides,
                expected_date=expected_date,
            )

        meta = self.deps.update_json_transaction(
            rl.ROADMAP_FILE,
            rl._default_store,
            mutate,
        )
        return {"status": "ok", "meta": meta}

    def clear_stage_blocker(self, object_id: str, row_num: int) -> dict:
        rl = self.deps.roadmap_lib()
        stage = self.find_stage_by_row(object_id, row_num)
        stage_key = stage["ID строки этапа"]

        def mutate(store):
            rl.clear_stage_block_meta(store, stage_key)
            return {"status": "ok"}

        return self.deps.update_json_transaction(
            rl.ROADMAP_FILE,
            rl._default_store,
            mutate,
        )

    def get_stage_roadmap(self, object_id: str, row_num: int) -> dict:
        rl = self.deps.roadmap_lib()
        stage = self.find_stage_by_row(object_id, row_num)
        snapshot = rl.stage_snapshot(
            self.load_roadmap_store(),
            stage["ID строки этапа"],
        )
        snapshot["stage"] = stage
        return snapshot

    def create_roadmap_category(self, object_id: str, row_num: int, title: str) -> dict:
        rl = self.deps.roadmap_lib()
        stage = self.find_stage_by_row(object_id, row_num)
        clean_title = title.strip()
        if not clean_title:
            raise StageServiceError(400, "Название категории обязательно")
        return self.deps.update_json_transaction(
            rl.ROADMAP_FILE,
            rl._default_store,
            lambda store: rl.new_category(
                store,
                stage["ID строки этапа"],
                clean_title[:100],
            ),
        )

    def delete_roadmap_category(self, object_id: str, row_num: int, category_id: str) -> dict:
        rl = self.deps.roadmap_lib()
        stage = self.find_stage_by_row(object_id, row_num)
        stage_key = stage["ID строки этапа"]

        def mutate(store):
            has_items = any(
                item.get("category_id") == category_id
                for item in store["items"].get(stage_key, [])
            )
            if has_items:
                raise StageServiceError(
                    400,
                    "Нельзя удалить категорию с пунктами -- сначала перенесите или удалите их",
                )
            if not rl.delete_category(store, stage_key, category_id):
                raise StageServiceError(404, "Категория не найдена")
            return {"status": "ok"}

        return self.deps.update_json_transaction(
            rl.ROADMAP_FILE,
            rl._default_store,
            mutate,
        )

    def create_roadmap_item(
        self,
        object_id: str,
        row_num: int,
        *,
        title: str,
        category_id: str | None,
        description: str,
        required: bool,
        safety_critical: bool,
        weight: int,
    ) -> dict:
        rl = self.deps.roadmap_lib()
        stage = self.find_stage_by_row(object_id, row_num)
        clean_title = title.strip()
        if not clean_title:
            raise StageServiceError(400, "Название пункта обязательно")
        stage_key = stage["ID строки этапа"]
        return self.deps.update_json_transaction(
            rl.ROADMAP_FILE,
            rl._default_store,
            lambda store: rl.new_item(
                store,
                stage_key,
                clean_title[:200],
                category_id=category_id,
                description=description.strip()[:1000],
                required=required,
                safety_critical=safety_critical,
                weight=weight,
            ),
        )

    def edit_roadmap_item(self, object_id: str, row_num: int, item_id: str, fields: dict) -> dict:
        rl = self.deps.roadmap_lib()
        stage = self.find_stage_by_row(object_id, row_num)
        stage_key = stage["ID строки этапа"]

        def mutate(store):
            item = rl.edit_item(store, stage_key, item_id, **fields)
            if not item:
                raise StageServiceError(404, "Пункт не найден")
            return item

        return self.deps.update_json_transaction(
            rl.ROADMAP_FILE,
            rl._default_store,
            mutate,
        )

    def delete_roadmap_item(self, object_id: str, row_num: int, item_id: str) -> dict:
        rl = self.deps.roadmap_lib()
        stage = self.find_stage_by_row(object_id, row_num)
        stage_key = stage["ID строки этапа"]

        def mutate(store):
            if not rl.delete_item(store, stage_key, item_id):
                raise StageServiceError(404, "Пункт не найден")
            return {"status": "ok"}

        return self.deps.update_json_transaction(
            rl.ROADMAP_FILE,
            rl._default_store,
            mutate,
        )

    def update_roadmap_item_status(
        self,
        object_id: str,
        row_num: int,
        item_id: str,
        status: str,
        user: dict,
    ) -> dict:
        rl = self.deps.roadmap_lib()
        stage = self.find_stage_by_row(object_id, row_num)
        stage_key = stage["ID строки этапа"]

        def mutate(store):
            try:
                item = rl.update_item_status(
                    store,
                    stage_key,
                    item_id,
                    status,
                    str(user["id"]),
                )
            except ValueError as exc:
                raise StageServiceError(400, str(exc)) from exc
            if not item:
                raise StageServiceError(404, "Пункт не найден")
            return item

        return self.deps.update_json_transaction(
            rl.ROADMAP_FILE,
            rl._default_store,
            mutate,
        )

    def create_roadmap_note(
        self,
        object_id: str,
        row_num: int,
        text: str,
        item_id: str | None,
        user: dict,
    ) -> dict:
        rl = self.deps.roadmap_lib()
        stage = self.find_stage_by_row(object_id, row_num)
        if not text.strip():
            raise StageServiceError(400, "Текст заметки обязателен")
        profile = self.deps.get_worker_profile(user["id"])
        author_name = self.deps.sanitize_display_name(
            profile.get("name"),
            str(user["id"]),
        )
        stage_key = stage["ID строки этапа"]
        return self.deps.update_json_transaction(
            rl.ROADMAP_FILE,
            rl._default_store,
            lambda store: rl.new_note(
                store,
                stage_key,
                str(user["id"]),
                author_name,
                text,
                item_id=item_id,
            ),
        )

    def list_roadmap_notes(self, object_id: str, row_num: int, item_id: str = "") -> dict:
        rl = self.deps.roadmap_lib()
        stage = self.find_stage_by_row(object_id, row_num)
        notes = rl.stage_notes(
            self.load_roadmap_store(),
            stage["ID строки этапа"],
            item_id=item_id or None,
        )
        return {"notes": sorted(notes, key=lambda note: note["created_at"])}

    def create_stage_request(
        self,
        object_id: str,
        row_num: int,
        *,
        kind: str,
        new_status: str,
        user: dict,
        role: str,
    ) -> dict:
        rl = self.deps.roadmap_lib()
        o = self.deps.load_repo_objekte_lib()
        if role == "owner":
            raise StageServiceError(400, "Owner меняет этапы напрямую, без запроса")
        stage = self.find_stage_by_row(object_id, row_num)
        if kind not in rl.REQUEST_KINDS:
            raise StageServiceError(400, "Недопустимый тип запроса")

        payload = {}
        if kind == "change_status":
            if new_status not in o.VALID_STAGE_STATUS:
                raise StageServiceError(400, "Недопустимый статус")
            payload["new_status"] = new_status

        profile = self.deps.get_worker_profile(user["id"])
        requester_name = self.deps.sanitize_display_name(
            profile.get("name"),
            str(user["id"]),
        )
        request = self.deps.update_json_transaction(
            rl.STAGE_REQUESTS_FILE,
            rl._default_requests,
            lambda requests: rl.new_stage_request(
                requests,
                object_id,
                stage["ID строки этапа"],
                row_num,
                kind,
                str(user["id"]),
                requester_name,
                payload=payload,
            ),
        )

        roles = self.deps.load_roles()
        owner_id = next(
            (uid for uid, assigned_role in roles.items() if assigned_role == "owner"),
            None,
        )
        if owner_id:
            kind_label = (
                "удаление этапа"
                if kind == "delete_stage"
                else f"смену статуса на «{payload.get('new_status', '')}»"
            )
            alert = self.deps.create_critical_alert(
                target_user_id=owner_id,
                kind="stage_request",
                title=f"{requester_name} просит {kind_label}",
                subtitle=stage.get("Название этапа", ""),
                ref_id=request["id"],
            )
            request["critical_alert_id"] = alert["id"]

            def attach_alert(requests):
                stored = rl.find_stage_request(requests, request["id"])
                if stored:
                    stored["critical_alert_id"] = alert["id"]
                return stored

            self.deps.update_json_transaction(
                rl.STAGE_REQUESTS_FILE,
                rl._default_requests,
                attach_alert,
            )
        return request

    def list_stage_requests(self, object_id: str) -> dict:
        rl = self.deps.roadmap_lib()
        return {
            "requests": rl.pending_requests_for_object(
                self.load_stage_requests(),
                object_id,
            )
        }

    def _rollback_request_decision(self, request_id: str, decided_by: str) -> None:
        """Best-effort compensation when an approved Sheets mutation fails."""
        rl = self.deps.roadmap_lib()

        def mutate(requests):
            request = rl.find_stage_request(requests, request_id)
            if (
                request
                and request.get("status") == "approved"
                and str(request.get("decided_by")) == str(decided_by)
            ):
                request["status"] = "pending"
                request["decided_at"] = None
                request["decided_by"] = None
            return request

        try:
            self.deps.update_json_transaction(
                rl.STAGE_REQUESTS_FILE,
                rl._default_requests,
                mutate,
            )
        except Exception:
            # Do not mask the original mutation failure. A durable outbox is the
            # longer-term way to close the remaining process-crash window.
            pass

    def decide_stage_request(
        self,
        object_id: str,
        request_id: str,
        approve: bool,
        user: dict,
    ) -> dict:
        rl = self.deps.roadmap_lib()
        o = self.deps.load_repo_objekte_lib()

        existing = self.load_stage_requests()
        pre_check = rl.find_stage_request(existing, request_id)
        if not pre_check or pre_check["object_id"] != object_id:
            raise StageServiceError(404, "Запрос не найден")

        def mutate(requests):
            decided = rl.decide_stage_request(
                requests,
                request_id,
                approve,
                str(user["id"]),
            )
            if not decided:
                raise StageServiceError(400, "Запрос уже обработан")
            return decided

        decided = self.deps.update_json_transaction(
            rl.STAGE_REQUESTS_FILE,
            rl._default_requests,
            mutate,
        )

        if approve:
            try:
                if decided["kind"] == "delete_stage":
                    o.delete_stage(decided["object_id"], decided["stage_row"])
                    o.sync_current_stage(decided["object_id"])
                elif decided["kind"] == "change_status":
                    o.update_stage_status(
                        decided["stage_row"],
                        decided["payload"]["new_status"],
                        self.deps.business_today_str(),
                    )
                    o.sync_current_stage(decided["object_id"])
            except ValueError as exc:
                self._rollback_request_decision(request_id, str(user["id"]))
                raise StageServiceError(400, str(exc)) from exc
            except Exception:
                self._rollback_request_decision(request_id, str(user["id"]))
                raise

        try:
            self.deps.send_telegram_message(
                int(decided["requested_by"]),
                (
                    f"{'Одобрено' if approve else 'Отклонено'}: "
                    f"ваш запрос по этапу «{decided.get('stage_row')}»"
                ),
            )
        except Exception:
            pass
        return decided
