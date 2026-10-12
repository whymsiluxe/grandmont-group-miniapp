"""Contract ingestion review HTTP domain."""
from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel


class ContractReviewBody(BaseModel):
    notes: str = ''


@dataclass(frozen=True)
class ContractsDeps:
    get_current_user: Callable[..., dict]
    require_owner: Callable[..., None]
    safe_load_json: Callable[[str, dict], dict]
    atomic_write_json: Callable[[str, dict], None]
    state_file: Callable[[], str]
    drive_folder_id: Callable[[], str]
    now: Callable[[], float]


@dataclass(frozen=True)
class ContractsHandlers:
    load_contract_store: Callable[[], dict]
    save_contract_store: Callable[[dict], None]
    list_contracts: Callable[..., dict]
    get_contract: Callable[..., dict]
    approve_contract: Callable[..., dict]
    reject_contract: Callable[..., dict]
    trigger_contract_ingest: Callable[..., dict]
    lock: threading.Lock


def create_contracts_router(deps: ContractsDeps) -> tuple[APIRouter, ContractsHandlers]:
    router = APIRouter()
    lock = threading.Lock()

    def load_contract_store() -> dict:
        return deps.safe_load_json(deps.state_file(), {'contracts': {}})

    def save_contract_store(data: dict) -> None:
        deps.atomic_write_json(deps.state_file(), data)

    @router.get('/api/contracts')
    def list_contracts(_: None = Depends(deps.require_owner)):
        store = load_contract_store()
        contracts = list(store['contracts'].values())
        contracts.sort(key=lambda contract: contract.get('ingested_at', 0), reverse=True)
        folder_id = deps.drive_folder_id()
        return {
            'drive_configured': bool(folder_id),
            'drive_scope_status': 'DRIVE_SCOPE_REQUIRED' if not folder_id else 'configured',
            'total': len(contracts),
            'contracts': [
                {
                    'id': contract['id'],
                    'file_name': contract.get('file_name', ''),
                    'status': contract.get('status', 'unknown'),
                    'ingested_at': contract.get('ingested_at'),
                    'approved_at': contract.get('approved_at'),
                    'rejected_at': contract.get('rejected_at'),
                    'error': contract.get('error'),
                    'has_draft_plan': bool(contract.get('project_plan_draft')),
                }
                for contract in contracts
            ],
        }

    @router.get('/api/contracts/{contract_id}')
    def get_contract(contract_id: str, _: None = Depends(deps.require_owner)):
        contract = load_contract_store()['contracts'].get(contract_id)
        if not contract:
            raise HTTPException(404, 'Договор не найден')
        return contract

    @router.post('/api/contracts/{contract_id}/approve')
    def approve_contract(
        contract_id: str,
        body: ContractReviewBody = ContractReviewBody(),
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(deps.require_owner),
    ):
        with lock:
            store = load_contract_store()
            contract = store['contracts'].get(contract_id)
            if not contract:
                raise HTTPException(404, 'Договор не найден')
            if contract.get('status') not in ('ingested', 'needs_manual_review'):
                raise HTTPException(400, f"Договор в статусе '{contract['status']}' не может быть утверждён")
            if not contract.get('project_plan_draft'):
                raise HTTPException(400, 'Нет черновика плана для утверждения')
            contract['status'] = 'approved'
            contract['approved_at'] = deps.now()
            contract['approved_by'] = str(user['id'])
            contract['review_notes'] = body.notes.strip()[:1000]
            save_contract_store(store)
        return {'status': 'approved', 'contract_id': contract_id}

    @router.post('/api/contracts/{contract_id}/reject')
    def reject_contract(
        contract_id: str,
        body: ContractReviewBody = ContractReviewBody(),
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(deps.require_owner),
    ):
        with lock:
            store = load_contract_store()
            contract = store['contracts'].get(contract_id)
            if not contract:
                raise HTTPException(404, 'Договор не найден')
            if contract.get('status') == 'approved':
                raise HTTPException(400, 'Утверждённый договор нельзя отклонить без отмены')
            contract['status'] = 'rejected'
            contract['rejected_at'] = deps.now()
            contract['rejected_by'] = str(user['id'])
            contract['review_notes'] = body.notes.strip()[:1000]
            save_contract_store(store)
        return {'status': 'rejected', 'contract_id': contract_id}

    @router.post('/api/contracts/ingest')
    def trigger_contract_ingest(_: None = Depends(deps.require_owner)):
        folder_id = deps.drive_folder_id()
        if not folder_id:
            raise HTTPException(503, 'DRIVE_SCOPE_REQUIRED: задайте CONTRACTS_DRIVE_FOLDER_ID в env')
        return {
            'status': 'trigger_sent',
            'message': 'Синхронизация с Drive будет выполнена при следующем запуске plan_sync / contract_ingest.py',
            'folder_id': folder_id,
        }

    return router, ContractsHandlers(
        load_contract_store=load_contract_store,
        save_contract_store=save_contract_store,
        list_contracts=list_contracts,
        get_contract=get_contract,
        approve_contract=approve_contract,
        reject_contract=reject_contract,
        trigger_contract_ingest=trigger_contract_ingest,
        lock=lock,
    )
