"""Commercial PDF document routes (Angebot and Rechnung).

This domain owns request models, PDF generation orchestration and best-effort
Telegram delivery. Runtime auth/configuration is injected by ``backend.main`` so
this module has no dependency back on the monolith.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import urllib.request as urlreq
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel


class AngebotKunde(BaseModel):
    typ: str = "privat"
    anrede: str = ""
    name: str
    kontakt: str = ""
    ustId: str = ""
    adresse: str = ""
    email: str = ""


class AngebotPosition(BaseModel):
    titel: str
    beschreibung: str = ""
    menge: float
    einheit: str = "Stk"
    preis: float


class AngebotBody(BaseModel):
    kunde: AngebotKunde
    objektAdresse: str = ""
    positionen: list[AngebotPosition]
    gueltigTage: int = 14
    mwstSatz: float = 19
    anzahlungPct: float = 40
    signatureBase64: str | None = None


class RechnungKunde(BaseModel):
    typ: str = "privat"
    anrede: str = ""
    name: str
    kontakt: str = ""
    ustId: str = ""
    adresse: str = ""
    email: str = ""


class RechnungPosition(BaseModel):
    titel: str
    beschreibung: str = ""
    menge: float
    einheit: str = "Stk"
    preis: float


class RechnungBody(BaseModel):
    nummer: str
    kunde: RechnungKunde
    projekt: str = ""
    positionen: list[RechnungPosition]
    zahlungsfristTage: int = 14
    mwstSatz: float = 19
    signatureBase64: str | None = None


@dataclass(frozen=True)
class CommercialDocsRouteDeps:
    get_current_user: Callable[..., dict]
    get_role: Callable[..., str]
    bot_token: Callable[[], str]
    angebot_script: Callable[[], str]
    rechnung_script: Callable[[], str]
    angebot_out_dir: Callable[[], str]
    rechnung_out_dir: Callable[[], str]
    # Deprecated migration-only fields. They are intentionally NOT consulted by
    # endpoint execution anymore. main.py still passes them temporarily so this
    # cleanup can land without editing the monolith again, but production flow is
    # now fully owned by this module and no longer reaches back through main aliases.
    send_pdf: Callable[..., None] | None = None
    generate_pdf: Callable[..., subprocess.CompletedProcess] | None = None
    make_pdf_filename: Callable[[str, str], str] | None = None


@dataclass(frozen=True)
class CommercialDocsHandlers:
    require_angebot_access: Callable[..., None]
    send_pdf_to_chat: Callable[..., None]
    run_pdf_generator: Callable[..., subprocess.CompletedProcess]
    safe_pdf_filename: Callable[[str, str], str]
    create_angebot: Callable[..., FileResponse]
    create_rechnung: Callable[..., FileResponse]


def safe_pdf_filename(prefix: str, customer_name: str) -> str:
    name = re.sub(r"[^\w.-]+", "_", (customer_name or "").strip(), flags=re.UNICODE).strip("._")
    if name.lower().endswith(".pdf"):
        name = name[:-4].rstrip("._")
    return f"{prefix}_{(name or 'kunde')[:80]}.pdf"


def run_pdf_generator(script_path: str, config: dict, timeout: int = 30):
    if not os.path.isfile(script_path):
        raise HTTPException(500, f"PDF-Generator fehlt: {os.path.basename(script_path)}")

    tmp = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False)
    try:
        with tmp:
            json.dump(config, tmp, ensure_ascii=False)
        result = subprocess.run(
            ["node", script_path, tmp.name],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, "PDF-Generierung dauert zu lange") from exc
    finally:
        try:
            os.remove(tmp.name)
        except FileNotFoundError:
            pass

    if result.returncode != 0:
        raise HTTPException(500, f"PDF-Generierung fehlgeschlagen: {result.stderr[-500:]}")
    return result


def create_commercial_docs_router(
    deps: CommercialDocsRouteDeps,
) -> tuple[APIRouter, CommercialDocsHandlers]:
    router = APIRouter()

    def require_angebot_access(role: str = Depends(deps.get_role)):
        if role != "owner":
            raise HTTPException(403, "только owner может создавать Angebot")

    def send_pdf_to_chat(chat_id, file_path, filename, caption):
        """Best-effort Telegram document delivery using only stdlib multipart."""
        boundary = uuid.uuid4().hex
        with open(file_path, "rb") as file_handle:
            file_data = file_handle.read()

        parts = [
            f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n'.encode(),
            f'--{boundary}\r\nContent-Disposition: form-data; name="caption"\r\n\r\n{caption}\r\n'.encode(),
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{filename}"\r\n'
                f'Content-Type: application/pdf\r\n\r\n'
            ).encode(),
            file_data,
            f'\r\n--{boundary}--\r\n'.encode(),
        ]
        request = urlreq.Request(
            f"https://api.telegram.org/bot{deps.bot_token()}/sendDocument",
            data=b"".join(parts),
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            urlreq.urlopen(request, timeout=20)
        except Exception as exc:
            print(f"WARNING: sendDocument fehlgeschlagen: {exc}")

    @router.post("/api/angebot")
    def create_angebot(
        body: AngebotBody,
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(require_angebot_access),
    ):
        if not body.positionen:
            raise HTTPException(400, "mindestens eine Position erforderlich")

        out_path = os.path.join(deps.angebot_out_dir(), f"{uuid.uuid4().hex}.pdf")
        config = body.model_dump()
        config["outPath"] = out_path
        if config.get("signatureBase64"):
            config["signedAt"] = datetime.now().strftime("%d.%m.%Y %H:%M")

        # Production execution deliberately resolves helpers in THIS module.
        # Deprecated deps.* compatibility callbacks are ignored so the route can
        # no longer call back through aliases in backend.main.
        run_pdf_generator(deps.angebot_script(), config)
        filename = safe_pdf_filename("Angebot", body.kunde.name)
        send_pdf_to_chat(
            user["id"], out_path, filename, f"Angebot für {body.kunde.name}"
        )
        return FileResponse(out_path, media_type="application/pdf", filename=filename)

    @router.post("/api/rechnung")
    def create_rechnung(
        body: RechnungBody,
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(require_angebot_access),
    ):
        if not body.nummer.strip():
            raise HTTPException(400, "Rechnung-Nr. erforderlich")
        if not body.positionen:
            raise HTTPException(400, "mindestens eine Position erforderlich")

        out_path = os.path.join(deps.rechnung_out_dir(), f"{uuid.uuid4().hex}.pdf")
        config = body.model_dump()
        config["outPath"] = out_path
        if config.get("signatureBase64"):
            config["signedAt"] = datetime.now().strftime("%d.%m.%Y %H:%M")

        run_pdf_generator(deps.rechnung_script(), config)
        filename = safe_pdf_filename("Rechnung", body.kunde.name)
        send_pdf_to_chat(
            user["id"], out_path, filename, f"{body.nummer} — {body.kunde.name}"
        )
        return FileResponse(out_path, media_type="application/pdf", filename=filename)

    return router, CommercialDocsHandlers(
        require_angebot_access=require_angebot_access,
        send_pdf_to_chat=send_pdf_to_chat,
        run_pdf_generator=run_pdf_generator,
        safe_pdf_filename=safe_pdf_filename,
        create_angebot=create_angebot,
        create_rechnung=create_rechnung,
    )
