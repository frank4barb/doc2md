#!/usr/bin/env python3
"""doc2md_api.py - servizio HTTP di doc2md.

Di norma lo avvia e lo spegne llama-swap (voce "doc2md" in llama-swap-doc2md.yaml):
parte alla prima richiesta, resta acceso mentre arrivano documenti, si spegne dopo il ttl.
Da riga di comando si usa d2m.py, che gli manda i file attraverso llama-swap.
Avvio a mano, per prove:
  uvicorn doc2md_api:app --host 127.0.0.1 --port 8090

Chiamate:
  GET  /v1/stato                       servizi raggiungibili (llama-swap, Jev, divisore...)
  POST /v1/converti                    multipart: file=@documento.pdf   oppure   url=https://...
       file: PDF, Office (anche vecchi), immagini, archivi, posta, epub, audio, video...
       url:  pagina web, video (YouTube e altri siti), documento raggiungibile da un link
             (si legge soltanto: nessun dato del chiamante va in rete)
       campi facoltativi:  immagini=embedded|separate|none (separate: solo con formato=json)   jev=true|false   doc_id=...
                           testate=true|false       formato=md|json
                           ocr=true  (legge con i modelli anche le pagine native)
                           lingua=it|en|...  (trascrizioni; predefinito: riconosciuta)
                           contesto=...      (trascrizioni: nomi e termini da scrivere giusti)
                           segmenti=true     (trascrizioni: nel rapporto i segmenti con inizio e fine)
       risposta: il Markdown (text/markdown), oppure JSON {doc_id, markdown, rapporto}
       con formato=json. Il doc_id e' anche nell'intestazione X-Doc-Id.

Esempio:
  curl -F file=@fattura.pdf -F jev=true http://127.0.0.1:8090/v1/converti -o fattura.md

Token facoltativo: se DOC2MD_TOKEN e' impostato serve "Authorization: Bearer <token>".
Limiti: [servizio] max_mb (100) per i documenti, max_mb_media (2048) per audio e video.
Le conversioni vanno una alla volta (DOC2MD_PARALLELI, predefinito 1): i modelli
su llama-swap servono una richiesta alla volta e la coda e' piu' prevedibile.
"""
import tempfile
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse

import config
import doc2md

MAX_MB = config.intero("servizio", "max_mb")
MAX_MB_MEDIA = config.intero("servizio", "max_mb_media")      # audio e video
# Starlette appoggia gli upload oltre 1 MB in un file temporaneo: in RAM, non sul disco
if Path("/dev/shm").is_dir():
    tempfile.tempdir = "/dev/shm"
TOKEN = config.valore("servizi", "token")
_coda = threading.BoundedSemaphore(config.intero("servizio", "paralleli"))
_cfg = doc2md.Config()

@asynccontextmanager
async def ciclo_di_vita(app):
    yield
    # spegnimento (llama-swap a fine ttl manda SIGTERM): LibreOffice si chiude e la sua cartella
    # in RAM si cancella qui, perche' uvicorn alla fine rilancia il segnale e i gestori atexit
    # non verrebbero eseguiti
    doc2md._ufficio.chiudi()


app = FastAPI(title="doc2md", version=doc2md.VERSIONE, lifespan=ciclo_di_vita)


def controlla_token(authorization: str = Header(default="")):
    if TOKEN and authorization != f"Bearer {TOKEN}":
        raise HTTPException(401, "token mancante o errato")


@app.get("/health")
def health():
    """Controllo di llama-swap all'avvio: risponde appena il processo e' pronto."""
    return {"ok": True}


@app.get("/v1/stato", dependencies=[Depends(controlla_token)])
def get_stato():
    return doc2md.stato(_cfg)


def _lavora(data, nome, opz):
    with _coda:
        if data is None:
            return doc2md.converti_url(nome, opz, _cfg)
        return doc2md.converti(data, nome, opz, _cfg)


@app.post("/v1/converti", dependencies=[Depends(controlla_token)])
async def converti(file: UploadFile | None = File(None),
                   url: str | None = Form(None),
                   immagini: str = Form("embedded"),
                   jev: bool = Form(False),
                   doc_id: str | None = Form(None),
                   testate: bool = Form(False),
                   ocr: bool = Form(False),
                   formato: str = Form("md"),
                   lingua: str | None = Form(None),
                   contesto: str | None = Form(None),
                   segmenti: bool = Form(False)):
    if immagini not in ("embedded", "separate", "none"):
        raise HTTPException(422, "immagini deve essere embedded, separate oppure none")
    if immagini == "separate" and formato != "json":
        raise HTTPException(422, "immagini=separate richiede formato=json (le immagini arrivano a parte)")
    if formato not in ("md", "json"):
        raise HTTPException(422, "formato deve essere md oppure json")
    if (file is None) == (not url):
        raise HTTPException(422, "serve un file oppure un url (uno solo dei due)")
    if url:
        data, nome = None, url
    else:
        testa = await file.read(1 << 16)  # tipo dal contenuto: audio e video hanno un limite piu' alto
        await file.seek(0)
        media = doc2md.riconosci_tipo(testa, file.filename or "") in doc2md.AUDIO | doc2md.VIDEO
        limite = MAX_MB_MEDIA if media else MAX_MB
        if file.size and file.size > limite * 1024 * 1024:
            raise HTTPException(413, f"file oltre {limite} MB" + ("" if media else " (audio e video: "
                                                                    f"{MAX_MB_MEDIA} MB)"))
        data = await file.read()          # in memoria: nessuna copia su disco
        if not data:
            raise HTTPException(422, "file vuoto")
        if len(data) > limite * 1024 * 1024:
            raise HTTPException(413, f"file oltre {limite} MB")
        nome = file.filename or "documento"
    opz = doc2md.Opzioni(immagini=immagini, jev=jev, doc_id=doc_id or None,
                         testate=testate, ocr=ocr, nome=nome, lingua=lingua or None,
                         contesto=contesto or None, segmenti=segmenti)
    try:
        md, rap = await run_in_threadpool(_lavora, data, nome, opz)
    except doc2md.Errore as e:
        raise HTTPException(422, str(e))
    head = {"X-Doc-Id": rap["doc_id"]}
    if formato == "json":
        imm = rap.pop("_immagini", [])
        return JSONResponse({"doc_id": rap["doc_id"], "markdown": md, "rapporto": rap, "immagini": imm},
                            headers=head)
    return PlainTextResponse(md, media_type="text/markdown; charset=utf-8", headers=head)
