#!/usr/bin/env python3
"""lotto.py - conversione di grandi quantita' di documenti con doc2md, interrompibile e riprendibile.

Due sorgenti:
  1. un file SQLite di scambio con il tuo DB (tabella "documenti" con i BLOB): i risultati
     tornano nello stesso file (tabelle "risultati" e "immagini"), pronti da reimportare;
  2. file e cartelle: l'uscita ricalca la cartella input sotto output. Ogni cartella di primo
     livello di input e' un lotto, e i file direttamente in input formano il lotto di output:
       input/referto.pdf           -> output/referto.pdf.md          (output/lotto.db, output/img/)
       input/referti/2025/rx.pdf   -> output/referti/2025/rx.pdf.md  (output/referti/lotto.db, output/referti/img/)
     Convertire input/referti/2025 (o un file solo) aggiorna quindi output/referti/lotto.db e
     output/referti/img; lotto.db serve anche per interrompere e riprendere. I file indicati da
     soli si riconvertono sempre; quelli trovati nelle cartelle solo se mai fatti (o con --rifai).
     Fuori da input (o con --out) la cartella indicata e' la radice: <output>/<nome> o --out.
     Link: una lista .links (un link per riga, descrizione facoltativa dopo uno spazio) diventa
     un documento per link, input/referti/fonti.links -> output/referti/fonti.links/<nome>.md; una
     scorciatoia .url / .webloc / .desktop e' un documento solo (input/pagina.url -> output/pagina.url.md).

Il lavoro e' registrato documento per documento: Ctrl+C e rilancio ripartono da dove erano.
I documenti passano da doc2md attraverso llama-swap (come d2m.py): nessun modello caricato qui.

Uso:
  python lotto.py crea lotto.db                       # file vuoto con le tabelle giuste
  (riempi "documenti" dal tuo DB, per esempio con il trasferimento dati di DBeaver)
  python lotto.py db lotto.db [--jev] [--riprova-errori] [--limite N]
  python lotto.py cartella referti/2025 [--jev] [--rifai]   # cartella (con le sottocartelle)
  python lotto.py file referti/2025/rx_001.pdf               # un file solo (o piu' file e cartelle)
  python lotto.py stato lotto.db                      # avanzamento, tempi, errori
  python lotto.py esporta lotto.db                    # elenco dei risultati in lotto.csv
  python lotto.py chiave <doc_id>                     # dal doc_id di un .md alla chiave (o al percorso
                                                      # relativo a input, per i file)
  python lotto.py verifica lotto.db                   # ricalcola il confronto col testo atteso
  python lotto.py esamina lotto.db [chiave...]        # perche' la copertura e' bassa (predefinito: 3 peggiori)
  python lotto.py completezza lotto.db                # ricalcola il confronto col testo del PDF

Tabelle del file di scambio:
  documenti (chiave TEXT PRIMARY KEY, nome TEXT, contenuto BLOB, meta TEXT)
      chiave: id del record d'origine; nel Markdown il doc_id e' la chiave in base64url
              (reversibile: python lotto.py chiave <doc_id> la restituisce identica)
      nome:   facoltativo; il tipo si riconosce dal contenuto
  risultati (chiave, stato, tipo, doc_id, markdown, pagine, metodo, jev, jev_da_verificare,
             immagini, secondi, errore, rapporto, convertito, verifica, mancanti,
             completezza, mancanti_pdf)
  immagini  (hash PRIMARY KEY, mime, dati BLOB)   -- una riga per immagine DIVERSA: il Markdown le
             cita come img/<hash>.<est>; i documenti che la contengono si trovano cercando l'hash
             nel Markdown

Due controlli automatici, per ogni documento:
  verifica     quota del testo atteso (colonna "atteso") ritrovata nel Markdown
  completezza  quota del testo del PDF ritrovata nel Markdown (solo PDF con testo: nessun
               testo perso da doc2md; un po' sotto il 100% e' normale per intestazioni ripetute
               e numeri di pagina, che doc2md toglie)

Impostazioni: doc2md.ini ([servizi] doc2md_url e token, [cartelle] input e output).
Un file .db o una cartella indicati senza percorso si cercano anche nella cartella input.
"""
import argparse
import base64
import csv
import hashlib
import json
import os
import re
import signal
import sqlite3
import sys
import time
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config                                   # noqa: E402  (solo libreria standard)

URL = config.valore("servizi", "doc2md_url").rstrip("/")
TOKEN = config.valore("servizi", "token")
ESTENSIONI = {"image/png": "png", "image/jpeg": "jpg"}
NON_DOCUMENTI = (".db", ".db-wal", ".db-shm", ".db-journal")   # file di lotto: nel modo cartella si saltano

# file di link: al posto del file si converte la pagina o il video a cui punta (si legge soltanto)
FILE_LINK = (".url", ".webloc", ".desktop")   # un link per file: scorciatoie di Windows, macOS, Linux
LISTA_LINK = ".links"                          # lista: un link per riga, eventuale descrizione dopo uno spazio
E_LINK = re.compile(r"https?://\S+$", re.I)


class Link(NamedTuple):
    url: str
    contesto: str | None = None     # descrizione della riga: per i video aiuta la trascrizione (nomi)


def e_file_link(p: Path) -> bool:
    return p.suffix.lower() in FILE_LINK + (LISTA_LINK,)


def link_di(f: Path) -> list[Link]:
    """I link di un file .links (uno per riga; righe vuote e # ignorate; doppioni tolti) o il link
    di una scorciatoia .url / .webloc / .desktop."""
    suf = f.suffix.lower()
    if suf == ".webloc":
        import plistlib
        try:
            u = str(plistlib.loads(f.read_bytes()).get("URL") or "")
        except Exception:
            u = ""
        return [Link(u)] if E_LINK.match(u) else []
    testo = f.read_text(encoding="utf-8-sig", errors="replace")
    if suf in FILE_LINK:
        m = re.search(r"^\s*URL\s*=\s*(\S+)", testo, re.M | re.I)
        return [Link(m[1])] if m and E_LINK.match(m[1]) else []
    out, visti = [], set()
    for riga in testo.splitlines():
        parti = riga.strip().split(None, 1)
        if not parti or parti[0].startswith("#") or not E_LINK.match(parti[0]) or parti[0] in visti:
            continue
        visti.add(parti[0])
        out.append(Link(parti[0], parti[1].strip() if len(parti) > 1 else None))
    return out


def voci_di_lista(f: Path, rel: str) -> list[tuple[str, Link]]:
    """(chiave, link) per ogni riga di una lista: chiave = <lista>/<nome dal link>, quindi il .md va in
    <uscita>/<lista>.links/<nome>.md. Nomi uguali (stesso id da siti diversi): si aggiunge un'impronta."""
    from d2m import nome_da_link
    voci, usati = [], set()
    for ln in link_di(f):
        nome = nome_da_link(ln.url)
        if nome in usati:
            nome += "_" + hashlib.sha256(ln.url.encode()).hexdigest()[:6]
        usati.add(nome)
        voci.append((f"{rel}/{nome}", ln))
    return voci


def doc_id_link(rel_input: str, url: str) -> str:
    """doc_id di un link di una lista: "<lista relativa a input>#<url>" in base64url (lotto.py chiave
    lo riporta a lista e link); se troppo lungo, un'impronta."""
    try:
        return doc_id_da_chiave(f"{rel_input}#{url}")
    except ValueError:
        return "LNK-" + hashlib.sha256(f"{rel_input}#{url}".encode()).hexdigest()[:16].upper()
INPUT, OUTPUT = config.cartella("input"), config.cartella("output")


trova = config.trova      # un .db o una cartella: com'e' scritto, altrimenti dentro la cartella input

SCHEMA = """
CREATE TABLE IF NOT EXISTS documenti (
    chiave    TEXT PRIMARY KEY,
    nome      TEXT,
    contenuto BLOB,
    meta      TEXT,
    atteso    TEXT,                           -- facoltativo: testo principale atteso, per la verifica
    url       TEXT                            -- documento letto da un link (file .links / .url)
);
CREATE TABLE IF NOT EXISTS risultati (
    chiave            TEXT PRIMARY KEY,
    stato             TEXT NOT NULL CHECK (stato IN ('fatto', 'errore')),
    tipo              TEXT,                   -- pdf, docx, xlsx, png... riconosciuto dal contenuto
    doc_id            TEXT UNIQUE,            -- base64url della chiave
    markdown          TEXT,
    pagine            INTEGER,
    metodo            TEXT,                   -- JSON: {"nativa": 3, "ocr": 1}
    jev               REAL,                   -- solo con --jev
    jev_da_verificare INTEGER,
    immagini          INTEGER,
    secondi           REAL,
    errore            TEXT,
    rapporto          TEXT,                   -- JSON completo di doc2md
    convertito        TEXT,                   -- data e ora ISO
    verifica          REAL,                   -- quota del testo atteso ritrovata (0-1), se c'e' "atteso"
    mancanti          TEXT,                   -- parole del testo atteso che mancano piu' spesso
    completezza       REAL,                   -- quota del testo del PDF ritrovata nel Markdown (0-1)
    mancanti_pdf      TEXT                    -- parole del PDF che mancano piu' spesso nel Markdown
);
CREATE TABLE IF NOT EXISTS immagini (
    hash   TEXT PRIMARY KEY,                  -- SHA-256 del contenuto, 32 caratteri esadecimali
    mime   TEXT,
    dati   BLOB
);
CREATE TABLE IF NOT EXISTS info (
    chiave TEXT PRIMARY KEY,                  -- es. "sorgente": l'albero d'origine (modo cartella)
    valore TEXT
);
CREATE INDEX IF NOT EXISTS risultati_stato ON risultati(stato);
"""


class Interrotto(Exception):
    pass


def chiudi(con):
    """A fine lavoro il file torna autosufficiente (niente -wal accanto): si puo' copiare o aprire in DBeaver."""
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("PRAGMA journal_mode=DELETE")
    except sqlite3.OperationalError:
        pass                                     # un altro programma (DBeaver) lo tiene aperto: va bene lo stesso
    con.close()


def apri(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")       # DBeaver puo' leggere mentre il lotto scrive
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    # file creati con lo schema precedente: si aggiungono le colonne nuove
    for tab, col, tipo in (("documenti", "atteso", "TEXT"), ("documenti", "url", "TEXT"), ("risultati", "verifica", "REAL"),
                           ("risultati", "mancanti", "TEXT"), ("risultati", "completezza", "REAL"),
                           ("risultati", "mancanti_pdf", "TEXT")):
        if col not in {r[1] for r in con.execute(f"PRAGMA table_info({tab})")}:
            con.execute(f"ALTER TABLE {tab} ADD COLUMN {col} {tipo}")
    migra_immagini(con)
    return con


def impronta(dati: bytes) -> str:
    """Come doc2md.impronta: SHA-256 del contenuto, primi 128 bit in esadecimale."""
    return hashlib.sha256(dati).hexdigest()[:32]


def migra_immagini(con):
    """File creati prima: tabella immagini con una riga per (documento, immagine) -> una riga per
    immagine diversa, e nel Markdown immagini/<rif>.<est> diventa img/<hash>.<est>."""
    if "rif" not in {r[1] for r in con.execute("PRAGMA table_info(immagini)")}:
        return
    n = con.execute("SELECT COUNT(*) FROM immagini").fetchone()[0]
    print(f"Una tantum: {n} immagini al nuovo formato (una riga per immagine diversa)...", flush=True)
    t0 = time.time()
    with con:
        con.execute("PRAGMA foreign_keys=OFF")
        con.execute("CREATE TABLE immagini_nuove (hash TEXT PRIMARY KEY, mime TEXT, dati BLOB)")
        per_doc = {}
        for chiave, rif, mime, dati in con.execute("SELECT chiave, rif, mime, dati FROM immagini").fetchall():
            h = impronta(dati)
            con.execute("INSERT OR IGNORE INTO immagini_nuove VALUES (?, ?, ?)", (h, mime, dati))
            per_doc.setdefault(chiave, {})[rif] = f"{h}.{ESTENSIONI.get(mime, 'bin')}"
        for chiave, sost in per_doc.items():
            md = con.execute("SELECT markdown FROM risultati WHERE chiave = ?", (chiave,)).fetchone()
            if not md or not md[0]:
                continue
            nuovo = md[0]
            for rif, f in sost.items():
                nuovo = re.sub(rf"\]: immagini/{re.escape(rif)}\.\w+", f"]: img/{f}", nuovo)
            con.execute("UPDATE risultati SET markdown = ? WHERE chiave = ?", (nuovo, chiave))
        con.execute("DROP TABLE immagini")
        con.execute("ALTER TABLE immagini_nuove RENAME TO immagini")
    m = con.execute("SELECT COUNT(*) FROM immagini").fetchone()[0]
    print(f"  {n} -> {m} immagini diverse; ricompatto il file...", flush=True)
    con.execute("VACUUM")
    print(f"  fatto in {time.time() - t0:.0f} s", flush=True)


# ---------------------------------------------------------------- verifica col testo atteso
def _parole(t: str) -> list:
    t = unicodedata.normalize("NFKC", t).lower().replace("\u2019", "'")
    return [w for w in re.findall(r"\w+", t) if len(w) > 1 or w.isdigit()]


def _solo_testo(md: str) -> str:
    md = re.sub(r"\A---\n.*?\n---\n", "", md, flags=re.S)          # intestazione YAML
    md = re.sub(r"<!--.*?-->", " ", md, flags=re.S)                  # commenti DOCID, verifica...
    md = re.sub(r"^\[img-[^\]]*\]:.*$", " ", md, flags=re.M)         # immagini in fondo
    md = re.sub(r"!\[[^\]]*\]\[[^\]]*\]", " ", md)                   # riferimenti alle immagini
    md = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", md)                 # link: resta il testo
    return re.sub(r"^\*\*doc_id:\*\*.*$", " ", md, flags=re.M)


MOJIBAKE = re.compile("[\u00c2\u00c3][\u0080-\u00bf\u2018-\u203a\u0152-\u017e\u02c6\u02dc\ufffd]|\u00e2\u20ac")


def _byte_cp1252(t: str) -> bytes:
    """Ritorno ai byte letti come Windows-1252: i 5 byte senza carattere (0x81, 0x8D, 0x8F, 0x90,
    0x9D) arrivano come U+0081... oppure come U+FFFD, e U+FFFD non e' recuperabile."""
    out = bytearray()
    for ch in t:
        o = ord(ch)
        if o in (0x81, 0x8D, 0x8F, 0x90, 0x9D):
            out.append(o)
        elif ch == "\ufffd":
            out += b"?"
        else:
            out += ch.encode("cp1252", errors="replace")
    return bytes(out)


def ripara(t: str) -> str:
    """Testo atteso letto con la codifica sbagliata. In SQL Server CONVERT(VARCHAR, VARBINARY) legge
    i byte come Windows-1252: se erano UTF-8, "perché" diventa "perchÃ©"; se erano UTF-16, compaiono
    caratteri nulli tra le lettere. Si torna ai byte e si rilegge nel modo giusto; il risultato si
    tiene solo se e' piu' pulito dell'originale."""
    if t.count("\x00") > len(t) // 4:
        b = _byte_cp1252(t)
        return b.decode("utf-16-le" if b[1:2] == b"\x00" else "utf-16-be", errors="replace")
    n = len(MOJIBAKE.findall(t))
    if n:
        r = _byte_cp1252(t).decode("utf-8", errors="replace")
        if len(MOJIBAKE.findall(r)) < n:
            return r
    return t


def testo_atteso(t: str) -> str:
    """Il testo atteso puo' essere XML o HTML (referti strutturati): contano solo i testi, non i
    nomi dei tag (<campo key_campo=...>) ne' l'intestazione <?xml version=... encoding=utf-8?>."""
    import html
    t = ripara(t)
    if len(re.findall(r"<[A-Za-z?!/][^>]*>", t)) >= 3:
        t = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r" \1 ", t, flags=re.S)
        t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    return t


def confronto(atteso: str | None, md: str):
    """-> (copertura, mancanti): quota delle parole del testo atteso ritrovate nel Markdown
    (contate con le ripetizioni, in qualunque ordine) e le parole che mancano piu' spesso."""
    if not atteso or not atteso.strip() or md is None:
        return None, None
    return _copertura(Counter(_parole(testo_atteso(atteso))), md)


def _copertura(a: Counter, md: str, minimo: int = 1):
    m = Counter(_parole(_solo_testo(md)))
    tot = sum(a.values())
    if not tot or tot < minimo:
        return None, None
    trovate = sum(min(c, m[w]) for w, c in a.items())
    manc = sorted(((c - m[w], w) for w, c in a.items() if m[w] < c), reverse=True)[:20]
    return round(trovate / tot, 4), " ".join(f"{w}" + (f"x{n}" if n > 1 else "") for n, w in manc) or None


def doc_id_da_chiave(chiave: str) -> str:
    """Chiave (o percorso relativo) -> doc_id: base64url senza "=", tutti i caratteri conservati.
    L'alfabeto base64url (A-Z a-z 0-9 - _) e' gia' valido per doc2md, per i nomi di file e per gli URL."""
    d = base64.urlsafe_b64encode(str(chiave).encode("utf-8")).rstrip(b"=").decode("ascii")
    if len(d) > 255:
        raise ValueError(f"chiave troppo lunga per il doc_id ({len(d)} caratteri in base64, massimo 255)")
    return d


def chiave_da_doc_id(doc_id: str) -> str:
    return base64.urlsafe_b64decode(doc_id + "=" * (-len(doc_id) % 4)).decode("utf-8")


def converti(data: bytes, nome: str, doc_id: str | None, a) -> dict:
    head = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
    campi = {"immagini": a.immagini, "jev": str(a.jev).lower(), "testate": str(a.testate).lower(),
             "ocr": str(a.ocr).lower(), "formato": "json"}
    if doc_id:
        campi["doc_id"] = doc_id
    if getattr(a, "lingua", None):
        campi["lingua"] = a.lingua
    if getattr(a, "contesto", None):
        campi["contesto"] = a.contesto
    r = requests.post(f"{URL}/v1/converti", files={"file": (nome or "documento", data)}, data=campi,
                      headers=head, timeout=a.timeout)
    if r.status_code != 200:
        try:
            msg = r.json().get("detail", r.text)
        except ValueError:
            msg = r.text
        raise RuntimeError(f"HTTP {r.status_code}: {str(msg)[:500]}")
    return r.json()


def converti_link(link: Link, doc_id: str | None, a) -> dict:
    """Un link (pagina web, video, documento in rete) al servizio doc2md: solo lettura dalla rete."""
    head = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
    campi = {"url": link.url, "immagini": a.immagini, "jev": str(a.jev).lower(), "testate": str(a.testate).lower(),
             "ocr": str(a.ocr).lower(), "formato": "json"}
    if doc_id:
        campi["doc_id"] = doc_id
    if getattr(a, "lingua", None):
        campi["lingua"] = a.lingua
    if link.contesto or getattr(a, "contesto", None):
        campi["contesto"] = link.contesto or a.contesto
    r = requests.post(f"{URL}/v1/converti", files={k: (None, v) for k, v in campi.items()},
                      headers=head, timeout=a.timeout)
    if r.status_code != 200:
        try:
            msg = r.json().get("detail", r.text)
        except ValueError:
            msg = r.text
        raise RuntimeError(f"HTTP {r.status_code}: {str(msg)[:500]}")
    return r.json()


def completezza(data: bytes | None, md: str):
    """Quota del testo del PDF ritrovata nel Markdown -> (completezza, parole mancanti).
    None se non e' un PDF o se il PDF non ha testo (scansione: lo legge l'OCR)."""
    if not data or data[:5] != b"%PDF-" and b"%PDF-" not in data[:1024]:
        return None, None
    try:
        import pymupdf
        pdf = pymupdf.open(stream=data, filetype="pdf")
        pagine = [pg.get_text().splitlines() for pg in pdf]
    except Exception:
        return None, None
    # come fa doc2md: le righe ripetute su piu' pagine (intestazioni, pie' di pagina) contano solo
    # sulla prima pagina; i numeri di pagina ("3", "Pag. 3 di 5", "- 3 -") non contano mai
    norma = lambda r: re.sub(r"\d+", "#", " ".join(r.lower().split()))
    quante = Counter(n for righe in pagine for n in {norma(r) for r in righe if r.strip()})
    testo = []
    for i, righe in enumerate(pagine):
        for r in righe:
            n = norma(r)
            if not n or re.fullmatch(r"[-–\s]*(pag(ina|\.)?\s*)?#(\s*(di|/|of)\s*#)?[-–\s]*", n):
                continue
            if i > 0 and len(pagine) > 1 and quante[n] > 1:
                continue
            testo.append(r)
    return _copertura(Counter(_parole(" ".join(testo))), md, minimo=20)


def registra(con, chiave, res=None, errore=None, secondi=0.0, tipo=None, data=None):
    """Un documento = una transazione: o tutto (risultato + immagini) o niente."""
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with con:
        if errore is not None:
            con.execute("""INSERT OR REPLACE INTO risultati (chiave, stato, tipo, errore, secondi, convertito)
                           VALUES (?, 'errore', ?, ?, ?, ?)""", (chiave, tipo, errore, secondi, now))
            return
        rap = res["rapporto"]
        imm = res.get("immagini") or []
        r = con.execute("SELECT atteso FROM documenti WHERE chiave = ?", (chiave,)).fetchone()
        verifica, mancanti = confronto(r[0] if r else None, res["markdown"])
        compl, manc_pdf = completezza(data, res["markdown"])
        con.execute("""INSERT OR REPLACE INTO risultati
            (chiave, stato, tipo, doc_id, markdown, pagine, metodo, jev, jev_da_verificare,
             immagini, secondi, errore, rapporto, convertito, verifica, mancanti, completezza, mancanti_pdf)
            VALUES (?, 'fatto', ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)""",
                    (chiave, rap.get("tipo") or rap.get("formato"), res["doc_id"], res["markdown"],
                     rap.get("pagine"), json.dumps(rap.get("metodo"), ensure_ascii=False), rap.get("jev"),
                     _da_verificare(res["markdown"]), rap.get("immagini"), secondi,
                     json.dumps(rap, ensure_ascii=False), now, verifica, mancanti, compl, manc_pdf))
        # immagini per contenuto: il logo ripetuto in 16.000 documenti e' una riga sola
        con.executemany("INSERT OR IGNORE INTO immagini (hash, mime, dati) VALUES (?, ?, ?)",
                        [(i["hash"], i["mime"], base64.b64decode(i["dati"])) for i in imm])


def _da_verificare(md: str) -> int:
    m = re.search(r"^jev_da_verificare: (\d+)", md, re.M)
    return int(m[1]) if m else None


class Avanzamento:
    def __init__(self, totale):
        self.totale, self.fatti, self.errori, self.t0, self.pagine = totale, 0, 0, time.time(), 0

    def passo(self, ok, pagine=0):
        self.fatti += 1
        self.errori += not ok
        self.pagine += pagine or 0
        dt = time.time() - self.t0
        resto = (self.totale - self.fatti) * dt / self.fatti
        eta = time.strftime("%H:%M", time.localtime(time.time() + resto))
        return (f"[{self.fatti}/{self.totale}] {dt / self.fatti:.1f} s/doc, {self.pagine} pagine, "
                f"errori {self.errori}, fine stimata {eta} ({resto / 3600:.1f} h)")


STOP = {"chiesto": False}           # Ctrl+C (o "Ferma" nel visore): finisce il documento in corso


def _ferma(*_):
    if STOP["chiesto"]:
        raise KeyboardInterrupt
    STOP["chiesto"] = True
    print("\nInterruzione chiesta: finisco il documento in corso e mi fermo "
          "(Ctrl+C di nuovo per uscire subito).", flush=True)


def ciclo(con, elenco, leggi, a, doc_id_di=lambda k: None, av=None, fine=True):
    """elenco: chiavi da fare; leggi(chiave) -> (nome, dati). av: avanzamento condiviso tra piu'
    lotti (modo cartella con piu' radici); fine: stampa l'esito finale."""
    signal.signal(signal.SIGINT, _ferma)
    av = av or Avanzamento(len(elenco))
    for chiave in elenco:
        if STOP["chiesto"]:
            break
        nome, data = leggi(chiave)
        t0 = time.time()
        link = data if isinstance(data, Link) else None
        if not link and not data:
            registra(con, chiave, errore="nessun link nel file" if e_file_link(Path(nome)) else "contenuto vuoto")
            print(f"{chiave}: {'nessun link' if e_file_link(Path(nome)) else 'contenuto vuoto'}  {av.passo(False)}")
            continue
        if link:
            data = None             # niente BLOB per i link: completezza e anteprima non si applicano
        try:
            res = converti_link(link, doc_id_di(chiave), a) if link else converti(data, nome, doc_id_di(chiave), a)
        except requests.ConnectionError:
            print(f"\nllama-swap non raggiungibile su {URL}: mi fermo (il documento {chiave} resta da fare).")
            return 2
        except Exception as e:
            registra(con, chiave, errore=str(e), secondi=round(time.time() - t0, 2))
            print(f"{chiave}: ERRORE {str(e)[:120]}  {av.passo(False)}", flush=True)
            continue
        registra(con, chiave, res, secondi=round(time.time() - t0, 2), data=data)
        rap = res["rapporto"]
        print(f"{chiave}: {rap.get('tipo')} {rap.get('pagine')} p. {time.time() - t0:.1f} s  "
              f"{av.passo(True, rap.get('pagine'))}", flush=True)
    if fine:
        print("Interrotto: rilancia lo stesso comando per continuare." if STOP["chiesto"] else "Lotto completato.")
    return 0


def da_fare(con, sorgente_sql, riprova):
    escludi = "('fatto', 'errore')" if not riprova else "('fatto')"
    return [r[0] for r in con.execute(
        f"{sorgente_sql} WHERE chiave NOT IN (SELECT chiave FROM risultati WHERE stato IN {escludi}) ORDER BY chiave")]


def cmd_db(a):
    con = apri(a.file)
    if getattr(a, "rifai", False):           # riconverte anche i "fatti" (dopo una correzione di doc2md)
        elenco = [r[0] for r in con.execute("SELECT chiave FROM documenti ORDER BY chiave")]
    else:
        elenco = da_fare(con, "SELECT chiave FROM documenti", a.riprova_errori)
    if a.limite:
        elenco = elenco[:a.limite]
    tot = con.execute("SELECT COUNT(*) FROM documenti").fetchone()[0]
    print(f"{a.file}: {tot} documenti, {len(elenco)} da convertire")

    def leggi(chiave):
        return con.execute("SELECT nome, contenuto FROM documenti WHERE chiave = ?", (chiave,)).fetchone()
    try:
        return ciclo(con, elenco, leggi, a, doc_id_di=doc_id_da_chiave)
    except KeyboardInterrupt:           # secondo Ctrl+C
        print("\nInterrotto subito: il documento in corso si rifa' al prossimo avvio.", flush=True)
        return 130
    finally:
        chiudi(con)


# ---------------------------------------------------------------- file e cartelle
def lotto_di(p: Path, uscita: Path | None = None) -> tuple[Path, Path]:
    """(radice d'origine, radice d'uscita) del lotto di un file o di una cartella.
    Dentro input l'uscita ricalca input (vedi config.radice_lotto); fuori da input, o con --out,
    la cartella indicata (o quella del file) e' la radice e l'uscita e' <output>/<nome> o --out."""
    p = p.resolve()
    if uscita is None:
        r = config.radice_lotto(p)
        if r:
            return r[0].resolve(), r[1]
    base = p if p.is_dir() else p.parent
    return base, (Path(uscita) if uscita else OUTPUT / base.name)


def doc_id_di_file(f: Path, src: Path) -> str:
    """doc_id di un file: il percorso relativo alla cartella input (relativo alla radice del lotto
    per i file fuori da input), in base64url. lotto.py chiave lo riporta al percorso."""
    f, inp = f.resolve(), INPUT.expanduser().resolve()
    return doc_id_da_chiave((f.relative_to(inp) if f.is_relative_to(inp) else f.relative_to(src)).as_posix())


def documenti_in(cartella: Path) -> list[Path]:
    return sorted(p for p in cartella.rglob("*") if p.is_file() and not p.name.startswith(".")
                  and not p.name.endswith(NON_DOCUMENTI))


def scrivi_file(con, out: Path, chiavi) -> tuple[int, int]:
    """Scrive i .md delle chiavi indicate (sovrascrivendo) e le immagini che in img/ mancano."""
    n = 0
    for chiave in chiavi:
        r = con.execute("SELECT markdown FROM risultati WHERE chiave = ? AND stato = 'fatto'", (chiave,)).fetchone()
        if not r:
            continue
        dest = out / f"{chiave}.md"          # relazione.pdf -> relazione.pdf.md (niente collisioni con relazione.docx)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # img/ e' alla radice del lotto: dal .md in una sottocartella il percorso e' relativo
        rel = os.path.relpath(out / "img", dest.parent).replace(os.sep, "/")
        dest.write_text(re.sub(r"^(\[[^\]]+\]: )img/", rf"\g<1>{rel}/", r[0], flags=re.M), encoding="utf-8")
        n += 1
    k = 0
    for h, mime, dati in con.execute("SELECT hash, mime, dati FROM immagini"):
        f = out / "img" / f"{h}.{ESTENSIONI.get(mime, 'bin')}"
        if not f.exists():
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(dati)
            k += 1
    return n, k


def prepara_lotto(src: Path, out: Path, voci: dict, a, url: dict | None = None):
    """Apre (o crea) il lotto out/lotto.db e registra i file. voci: {chiave: indicato_da_solo}.
    Da convertire: i file indicati da soli (sempre), quelli mai fatti, gli errori con
    --riprova-errori, tutti con --rifai."""
    out.mkdir(parents=True, exist_ok=True)
    con = apri(out / "lotto.db")
    with con:     # il visore risale dal .md al file d'origine
        con.execute("INSERT OR REPLACE INTO info VALUES ('sorgente', ?)", (str(src),))
        # la cartella e' la sorgente: in "documenti" solo chiave e nome, niente BLOB
        con.executemany("INSERT OR IGNORE INTO documenti (chiave, nome) VALUES (?, ?)",
                        [(k, k.rsplit("/", 1)[-1]) for k in voci])
        # documenti letti da un link: il visore mostra il link al posto dell'originale
        con.executemany("UPDATE documenti SET url = ? WHERE chiave = ?", [(u, k) for k, u in (url or {}).items()])
    stati = dict(con.execute("SELECT chiave, stato FROM risultati").fetchall())
    elenco = [k for k in sorted(voci) if voci[k] or a.rifai or k not in stati
              or (stati[k] == "errore" and a.riprova_errori)]
    # .md mancanti di documenti gia' fatti (per esempio dopo un'interruzione immediata)
    mancanti = [k for k in voci if stati.get(k) == "fatto" and k not in elenco and not (out / f"{k}.md").is_file()]
    return con, elenco, mancanti


def cmd_cartella(a):
    """File e cartelle (anche piu' di uno, anche misti): ogni file va nel lotto a cui appartiene."""
    gruppi: dict[tuple[Path, Path], dict] = {}
    links: dict[tuple[Path, str], Link] = {}        # (radice d'origine, chiave) -> link da convertire
    doc_ids: dict[tuple[Path, str], str] = {}
    uscita = Path(a.out).expanduser() if getattr(a, "out", None) else None

    def aggiungi(f: Path, src: Path, out: Path, esplicito: bool):
        rel = f.resolve().relative_to(src).as_posix()
        voci = gruppi.setdefault((src, out), {})
        if f.suffix.lower() == LISTA_LINK:      # una lista si comporta come una cartella: solo i link nuovi
            rel_inp = (f.resolve().relative_to(INPUT.expanduser().resolve()).as_posix()
                       if f.resolve().is_relative_to(INPUT.expanduser().resolve()) else rel)
            for chiave, ln in voci_di_lista(f, rel):
                voci.setdefault(chiave, False)
                links[(src, chiave)] = ln
                doc_ids[(src, chiave)] = doc_id_link(rel_inp, ln.url)
            return
        if f.suffix.lower() in FILE_LINK:
            ln = link_di(f)
            if ln:
                links[(src, rel)] = ln[0]
        if esplicito:
            voci[rel] = True
        else:
            voci.setdefault(rel, False)
    out_tutto = OUTPUT.expanduser().resolve()
    for x in a.percorsi:
        p = trova(x)
        if not p.exists():
            raise SystemExit(f"Non trovato: {x} (ne' qui ne' in {INPUT})")
        if p.resolve().is_relative_to(out_tutto):
            raise SystemExit(f"{p} e' dentro la cartella di output: indica i documenti d'origine")
        try:
            if p.is_dir():
                radice = lotto_di(p, uscita) if uscita else None
                for f in documenti_in(p):
                    aggiungi(f, *(radice or lotto_di(f)), esplicito=False)
            else:
                aggiungi(p, *lotto_di(p, uscita), esplicito=True)
        except ValueError as e:
            raise SystemExit(str(e))
    if not gruppi:
        print("Nessun documento da convertire.")
        return 0
    lotti = []
    for (src, out), voci in sorted(gruppi.items()):
        con, elenco, mancanti = prepara_lotto(src, out, voci, a,
                                              {k: links[(s, k)].url for (s, k) in links if s == src})
        if a.limite:
            elenco = elenco[:a.limite]
        print(f"{src}: {len(voci)} documenti, {len(elenco)} da convertire -> {out}  (stato in lotto.db, immagini in img/)")
        lotti.append((src, out, con, elenco, mancanti))
    av = Avanzamento(sum(len(x[3]) for x in lotti) or 1)
    rc = 0
    for src, out, con, elenco, mancanti in lotti:
        def leggi(chiave, src=src):
            if (src, chiave) in links:
                return chiave.rsplit("/", 1)[-1], links[(src, chiave)]
            p = src / chiave
            return p.name, (b"" if e_file_link(p) else p.read_bytes() if p.is_file() else b"")

        def doc_id_di(k, src=src):
            return doc_ids.get((src, k)) or doc_id_di_file(src / k, src)
        try:
            if rc == 0 and not STOP["chiesto"]:
                rc = ciclo(con, elenco, leggi, a, doc_id_di=doc_id_di, av=av, fine=False)
        except KeyboardInterrupt:       # secondo Ctrl+C: i .md dei documenti gia' fatti si scrivono lo stesso
            print("\nInterrotto subito: il documento in corso si rifa' al prossimo avvio.", flush=True)
            rc = 130
        n, k = scrivi_file(con, out, elenco + mancanti)
        if n or k:
            print(f"{n} file .md e {k} immagini nuove scritti in {out}")
        chiudi(con)
    if rc == 0:
        print("Interrotto: rilancia lo stesso comando per continuare." if STOP["chiesto"] else "Lotto completato.")
    return rc


def registra_file(f: Path, res: dict, data: bytes, secondi: float) -> Path:
    """Per doc2md.py (conversione nello stesso processo): registra un file di input nel suo lotto
    e scrive il .md e le immagini, come lotto.py file. Restituisce il percorso del .md."""
    src, out = lotto_di(f)
    chiave = f.resolve().relative_to(src).as_posix()
    out.mkdir(parents=True, exist_ok=True)
    con = apri(out / "lotto.db")
    with con:
        con.execute("INSERT OR REPLACE INTO info VALUES ('sorgente', ?)", (str(src),))
        con.execute("INSERT OR IGNORE INTO documenti (chiave, nome) VALUES (?, ?)", (chiave, f.name))
    registra(con, chiave, res, secondi=secondi, data=data)
    scrivi_file(con, out, [chiave])
    chiudi(con)
    return out / f"{chiave}.md"


def cmd_stato(a):
    con = apri(a.file)
    tot = con.execute("SELECT COUNT(*) FROM documenti").fetchone()[0]
    st = dict(con.execute("SELECT stato, COUNT(*) FROM risultati GROUP BY stato").fetchall())
    print(f"documenti {tot}: fatti {st.get('fatto', 0)}, errori {st.get('errore', 0)}, "
          f"da fare {tot - st.get('fatto', 0) - st.get('errore', 0)}")
    r = con.execute("""SELECT SUM(secondi), SUM(pagine), AVG(secondi), AVG(jev) FROM risultati
                       WHERE stato='fatto'""").fetchone()
    if r[0]:
        print(f"tempo totale {r[0] / 3600:.1f} h, pagine {r[1]}, media {r[2]:.1f} s/doc, "
              f"{r[0] / max(1, r[1]):.1f} s/pagina" + (f", Jev medio {r[3]:.3f}" if r[3] else ""))
    print("per tipo:", dict(con.execute(
        "SELECT tipo, COUNT(*) FROM risultati WHERE stato='fatto' GROUP BY tipo ORDER BY 2 DESC").fetchall()))
    err = con.execute("""SELECT substr(errore, 1, 90), COUNT(*) FROM risultati WHERE stato='errore'
                         GROUP BY 1 ORDER BY 2 DESC LIMIT 10""").fetchall()
    for e, n in err:
        print(f"  errore x{n}: {e}")
    stato_verifica(con)
    stato_completezza(con)
    stato_immagini(con)


PAROLE_MIN = 15    # un testo atteso piu' corto e' un segnaposto (CLOB, "S REPERTI:", nome di un modello .ftl)


def atteso_valido(t) -> bool:
    return bool(t) and len(_parole(testo_atteso(t))) >= PAROLE_MIN


def _verifiche(con) -> list:
    """(chiave, tipo, verifica, metodo, mancanti, atteso valido?, inizio dell'atteso)"""
    out = []
    for chiave, tipo, ver, metodo, manc, att in con.execute(
            """SELECT r.chiave, r.tipo, r.verifica, r.metodo, r.mancanti, d.atteso FROM risultati r
               JOIN documenti d ON d.chiave = r.chiave WHERE r.stato='fatto' AND r.verifica IS NOT NULL"""):
        out.append((chiave, tipo, ver, metodo, manc, atteso_valido(att), " ".join(testo_atteso(att or "").split())[:40]))
    return out


def stato_verifica(con):
    tutte = _verifiche(con)
    if not tutte:
        return
    scarti = [x for x in tutte if not x[5]]
    if scarti:
        frequenti = Counter(x[6] for x in scarti).most_common(5)
        print(f"testo atteso non valido (meno di {PAROLE_MIN} parole), esclusi dalla verifica: {len(scarti)}  "
              f"piu' frequenti: {frequenti}")
    validi = [x for x in tutte if x[5]]
    v = [x[2] for x in validi]
    if not v:
        return
    fasce = [("almeno 95%", lambda x: x >= .95), ("80-95%", lambda x: .8 <= x < .95),
             ("50-80%", lambda x: .5 <= x < .8), ("sotto 50%", lambda x: x < .5)]
    print(f"verifica col testo atteso: {len(v)} documenti, copertura media {100 * sum(v) / len(v):.1f}%  " +
          "  ".join(f"{n}: {sum(map(f, v))}" for n, f in fasce))
    for chiave, tipo, ver, metodo, manc, _, _ in sorted(validi, key=lambda x: x[2])[:10]:
        print(f"  {ver * 100:5.1f}%  {chiave}  {tipo} {metodo}  mancano: {(manc or '')[:80]}")



def stato_completezza(con):
    righe = con.execute("""SELECT chiave, completezza, mancanti_pdf, pagine FROM risultati
                           WHERE stato='fatto' AND completezza IS NOT NULL ORDER BY completezza""").fetchall()
    if not righe:
        return
    v = [r[1] for r in righe]
    fasce = [("almeno 95%", lambda x: x >= .95), ("80-95%", lambda x: .8 <= x < .95),
             ("50-80%", lambda x: .5 <= x < .8), ("sotto 50%", lambda x: x < .5)]
    print(f"completezza rispetto al testo del PDF: {len(v)} documenti, media {100 * sum(v) / len(v):.1f}%  " +
          "  ".join(f"{n}: {sum(map(f, v))}" for n, f in fasce))
    for chiave, c, manc, pag in righe[:10]:
        print(f"  {c * 100:5.1f}%  {chiave}  {pag} p.  mancano: {(manc or '')[:80]}")


def stato_immagini(con):
    n, byte = con.execute("SELECT COUNT(*), SUM(length(dati)) FROM immagini").fetchone()
    rif = con.execute("SELECT SUM(immagini) FROM risultati WHERE stato='fatto'").fetchone()[0] or 0
    if n or rif:
        print(f"immagini: {rif} nei documenti, {n} diverse salvate ({(byte or 0) / 2**20:.1f} MB)")


def cmd_esamina(a):
    """Perche' un documento ha copertura bassa: testo atteso, Markdown prodotto e cosa c'e' nel PDF
    (testo vero, immagini, disegni, caratteri senza mappatura). Con --peggiori N i N piu' bassi."""
    con = apri(a.file)
    chiavi = a.chiavi or [x[0] for x in sorted((x for x in _verifiche(con) if x[5]),
                                                key=lambda x: x[2])[:a.peggiori]]
    for k in chiavi:
        r = con.execute("""SELECT r.verifica, r.markdown, r.rapporto, d.atteso, d.contenuto, r.mancanti,
                                  r.completezza, r.mancanti_pdf
                           FROM risultati r JOIN documenti d ON d.chiave = r.chiave WHERE r.chiave = ?""",
                        (k,)).fetchone()
        if not r:
            print(f"{k}: non trovato tra i risultati")
            continue
        ver, md, rap, att, blob, manc, compl, manc_pdf = r
        blob = blob or dati_di(con, k)
        rap = json.loads(rap or "{}")
        testo_md = " ".join(_solo_testo(md or "").split())
        testo_att = " ".join(testo_atteso(att or "").split())
        print("=" * 100)
        print(f"{k}  verifica {ver if ver is None else round(ver * 100, 1)}%  avvisi {rap.get('avvisi')}  "
              f"schemi {rap.get('schemi')}  immagini {rap.get('immagini')}")
        for i, p in enumerate(rap.get("pagine_dettaglio") or [], 1):
            print(f"  doc2md p{i}: {p}")
        print(f"  ATTESO   ({len(_parole(testo_att))} parole): {testo_att[:300]}")
        print(f"  MARKDOWN ({len(_parole(testo_md))} parole): {testo_md[:300]}")
        print(f"  mancano: {(manc or '')[:150]}")
        if compl is not None:
            print(f"  completezza rispetto al PDF {compl * 100:.1f}%  mancano: {(manc_pdf or '')[:120]}")
        try:
            import pymupdf
            pdf = pymupdf.open(stream=blob, filetype="pdf")
        except Exception as e:
            print(f"  PDF non analizzabile qui: {e}")
            continue
        for i, pg in enumerate(pdf, 1):
            t = pg.get_text()
            area = abs(pg.rect) or 1
            imm = []
            for info in pg.get_images(full=True):
                for rr in pg.get_image_rects(info[0]):
                    imm.append(f"{info[2]}x{info[3]} {100 * abs(rr & pg.rect) / area:.0f}%")
            font = sorted({f"{f[3]}({f[1]},{f[2] or '-'})" for f in pg.get_fonts()})
            print(f"  PDF p{i}: testo {len(t.strip())} caratteri, immagini {imm or 'nessuna'}, "
                  f"disegni {len(pg.get_drawings())}, font {font[:6]}")
            print(f"         testo grezzo: {' '.join(t.split())[:250]!r}")
            if a.fasi:
                fasi_doc2md(pg)
    chiudi(con)


def fasi_doc2md(pg):
    """Le fasi di doc2md che possono togliere testo da una pagina nativa, con le parole che prendono."""
    import pymupdf
    import doc2md as D
    import schema_geo
    parole = pg.get_text("words")

    def dentro(r):
        r = pymupdf.Rect(r)
        return [w[4] for w in parole if r.contains(pymupdf.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2))]
    print(f"         fasi doc2md (parole sulla pagina: {len(parole)})")
    tabelle = []
    try:
        lv = D.linee_verticali(pg)
        for t in pg.find_tables().tables:
            vera = t.row_count >= 2 and D.tabella_vera(t, pg, lv)
            w = dentro(t.bbox)
            print(f"           tabella {[round(x) for x in t.bbox]} righe {t.row_count} vera={vera} "
                  f"parole {len(w)}: {' '.join(w[:12])}")
            if vera:
                tabelle.append(pymupdf.Rect(t.bbox))
    except Exception as e:
        print(f"           tabelle: errore {e}")
    try:
        mer, rep, nodes, _ = schema_geo.convert(pg)
        info = {k: v for k, v in rep.items() if not isinstance(v, (list, dict))}
        if len(nodes) >= 3:
            area = pymupdf.Rect(nodes[0].r)
            for nd in nodes[1:]:
                area |= nd.r
            w = dentro((area + (-12, -12, 12, 12)) & pg.rect)
            print(f"           schema {info} area {[round(x) for x in area]} parole {len(w)}: {' '.join(w[:12])}")
            for nd in nodes[:8]:
                print(f"             riquadro {[round(x) for x in nd.r]} {getattr(nd, 'text', '')[:60]!r}")
        else:
            print(f"           schema: no ({info})")
    except Exception as e:
        print(f"           schema: errore {e}")
    print(f"           formule: {D.ha_formule(pg)}")
    testo = " ".join(p.md for p in D.blocchi_testo(pg, tabelle, frozenset()))
    print(f"           testo dai blocchi (senza aree escluse): {len(testo.split())} parole")


def dati_di(con, chiave) -> bytes | None:
    """Il documento originale: il BLOB (lotto da database) o il file nell'albero d'origine."""
    r = con.execute("SELECT contenuto FROM documenti WHERE chiave = ?", (chiave,)).fetchone()
    if r and r[0]:
        return r[0]
    s = con.execute("SELECT valore FROM info WHERE chiave = 'sorgente'").fetchone()
    p = Path(s[0]) / chiave if s else None
    return p.read_bytes() if p and p.is_file() else None


def cmd_completezza(a):
    """Ricalcola la completezza per i documenti gia' convertiti (per esempio un lotto fatto prima
    che esistesse il controllo). PDF dal file stesso (modo db) o dall'albero d'origine (--cartella)."""
    con = apri(a.file)
    righe = con.execute("SELECT chiave FROM risultati WHERE stato='fatto' ORDER BY chiave").fetchall()
    t0, fatti = time.time(), 0
    for i, (k,) in enumerate(righe, 1):
        md = con.execute("SELECT markdown FROM risultati WHERE chiave = ?", (k,)).fetchone()[0]
        if a.cartella:
            p = Path(a.cartella) / k
            data = p.read_bytes() if p.is_file() else None
        else:
            data = dati_di(con, k)
        c, m = completezza(data, md or "")
        con.execute("UPDATE risultati SET completezza = ?, mancanti_pdf = ? WHERE chiave = ?", (c, m, k))
        fatti += c is not None
        if i % 1000 == 0:
            con.commit()
            print(f"  {i}/{len(righe)}  {time.time() - t0:.0f} s", flush=True)
    con.commit()
    print(f"completezza calcolata per {fatti} documenti su {len(righe)} (gli altri non sono PDF con testo)")
    stato_completezza(con)
    chiudi(con)


def cmd_verifica(a):
    """Ricalcola il confronto col testo atteso (per esempio se 'atteso' e' stato caricato dopo)."""
    con = apri(a.file)
    righe = con.execute("""SELECT r.chiave, d.atteso, r.markdown FROM risultati r JOIN documenti d
                           ON d.chiave = r.chiave WHERE r.stato = 'fatto'""").fetchall()
    with con:
        for chiave, att, md in righe:
            con.execute("UPDATE risultati SET verifica = ?, mancanti = ? WHERE chiave = ?",
                        (*confronto(att, md), chiave))
    stato_verifica(con)
    chiudi(con)


def cmd_esporta(a):
    """Elenco dei risultati in CSV (senza Markdown e immagini): da aprire con Excel o da confrontare col DB."""
    con = apri(a.file)
    out = a.csv or a.file.with_suffix(".csv")
    campi = ["chiave", "stato", "tipo", "doc_id", "pagine", "metodo", "jev", "jev_da_verificare",
             "immagini", "secondi", "errore", "convertito", "verifica", "mancanti", "completezza", "mancanti_pdf"]
    with open(out, "w", newline="", encoding="utf-8-sig") as f:        # utf-8-sig: Excel legge gli accenti
        w = csv.writer(f, delimiter=";")
        w.writerow(campi)
        w.writerows(con.execute(f"SELECT {', '.join(campi)} FROM risultati ORDER BY chiave"))
    print(f"{out}: {con.execute('SELECT COUNT(*) FROM risultati').fetchone()[0]} righe")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("crea", help="crea il file di scambio vuoto")
    c.add_argument("file", type=lambda x: Path(x) if ("/" in x or x.startswith(".")) else INPUT / x,
                   help="un nome senza percorso va nella cartella input di doc2md.ini")
    for nome in ("db", "cartella", "file"):
        p = sub.add_parser(nome)
        if nome == "db":
            p.add_argument("file", type=trova)
        else:                           # "cartella" e "file" sono lo stesso comando
            p.add_argument("percorsi", nargs="+", help="cartelle e/o file (senza percorso: anche nella cartella input)")
            p.add_argument("--out", help="cartella di uscita (predefinito: ricalca input sotto output)")
        p.add_argument("--immagini", choices=["separate", "embedded", "none"],
                       default=config.valore("conversione", "immagini"))
        p.add_argument("--lingua", default=config.valore("conversione", "lingua") or None)
        p.add_argument("--jev", action="store_true")
        p.add_argument("--testate", action="store_true")
        p.add_argument("--ocr", action="store_true")
        p.add_argument("--riprova-errori", action="store_true", help="ritenta i documenti finiti in errore")
        p.add_argument("--rifai", action="store_true",
                       help="riconverte anche i documenti gia' fatti (con --limite: i primi N)")
        p.add_argument("--limite", type=int, help="converte al massimo N documenti (prove)")
        p.add_argument("--timeout", type=int, default=config.intero("conversione", "timeout"),
                       help="secondi massimi per documento (predefinito: doc2md.ini, 7200 = 2 ore)")
    s = sub.add_parser("stato")
    s.add_argument("file", type=trova)
    e = sub.add_parser("esporta", help="elenco dei risultati in CSV")
    e.add_argument("file", type=trova)
    e.add_argument("--csv", type=Path)
    cc = sub.add_parser("completezza", help="ricalcola il confronto col testo del PDF")
    cc.add_argument("file", type=trova)
    cc.add_argument("--cartella", help="modo cartella: la cartella d'origine (predefinito: quella registrata nel lotto)")
    vv = sub.add_parser("verifica", help="ricalcola il confronto col testo atteso")
    vv.add_argument("file", type=trova)
    es = sub.add_parser("esamina", help="perche' un documento ha copertura bassa")
    es.add_argument("file", type=trova)
    es.add_argument("chiavi", nargs="*", help="chiavi da esaminare (predefinito: i peggiori)")
    es.add_argument("--peggiori", type=int, default=3)
    es.add_argument("--fasi", action="store_true", help="rifa' le fasi di doc2md sulla pagina (serve il venv di doc2md)")
    k = sub.add_parser("chiave", help="decodifica un doc_id nella chiave o nel percorso d'origine")
    k.add_argument("doc_id")
    a = ap.parse_args()
    if a.cmd == "chiave":
        print(chiave_da_doc_id(a.doc_id))
        return 0
    if a.cmd == "crea":
        a.file.parent.mkdir(parents=True, exist_ok=True)
        chiudi(apri(a.file))
        print(f"Creato {a.file} con le tabelle documenti, risultati, immagini")
        return 0
    return {"db": cmd_db, "cartella": cmd_cartella, "file": cmd_cartella, "stato": cmd_stato, "esporta": cmd_esporta, "verifica": cmd_verifica, "esamina": cmd_esamina,
            "completezza": cmd_completezza}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main() or 0)
