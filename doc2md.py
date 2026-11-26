#!/usr/bin/env python3
"""doc2md.py - qualunque documento -> Markdown (testo, tabelle, formule LaTeX, schemi Mermaid).

Percorso per ogni pagina:
  1. smistamento con PyMuPDF: pagina nativa (testo vero nel PDF) o scansione;
  2. pagina nativa: testo e tabelle dal PDF; se ci sono formule (font matematici)
     il divisore trova le formule e MiniCPM-OCR le scrive in LaTeX; gli schemi
     vettoriali passano da schema_geo.py; le immagini da schema_raster.py;
  3. scansione: PP-DocLayoutV3 divide la pagina, ogni regione va al suo lettore
     (testo PaddleOCR-VL, tabelle e formule MiniCPM-OCR, figure schema_raster.py);
  4. facoltativo: MiniCPM-OCR Jev vota le regioni lette (voto nell'intestazione);
  5. ricomposizione con doc_id nell'intestazione, in ogni pagina e nei nomi delle
     immagini; le immagini non convertibili sono incorporate in base64 in fondo.

Formati: PDF, Word/PowerPoint/Excel (anche vecchi e OpenDocument), immagini, html, epub, posta
(eml, msg con gli allegati), archivi (zip, tar, gz, 7z), notebook, json, xml, svg, audio e video
(trascritti con whisper.cpp), link a pagine web, video (YouTube...) e documenti (formati.py,
media.py, web.py).

Nessuna copia del documento va su disco: dove un programma esterno vuole un file (LibreOffice,
ffmpeg, yt-dlp) si usa una cartella in RAM (/dev/shm) cancellata prima di restituire il risultato.
Dalla rete si legge soltanto (pagine, video): i dati del chiamante non escono mai.

Uso normale: d2m.py (client leggero) -> llama-swap -> doc2md_api.py, che resta in memoria
solo mentre lavora. Uso diretto, nello stesso processo (prove, debug):
  python doc2md.py documento.pdf -o documento.md [--jev] [--immagini none]

Impostazioni: doc2md.ini (vedi doc2md.ini.esempio e config.py).
"""
import argparse
import base64
import hashlib
import io
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pymupdf
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config                                                # noqa: E402
from modelli import (DocLayout, ask, clean, dedup,  # noqa: E402
                     jev_health, jev_score, mark_nested)
import schema_geo                                             # noqa: E402
import schema_raster                                          # noqa: E402
import formati                                                # noqa: E402
import media                                                  # noqa: E402
import web                                                    # noqa: E402
for _m in (formati, media, web):        # i moduli dei formati usano le funzioni di questo
    _m.D = sys.modules[__name__]

VERSIONE = "0.2"
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp", ".gif"}
OFFICE_EXT = {".doc", ".docx", ".odt", ".rtf", ".ppt", ".pptx", ".odp", ".xls", ".ods", ".html"}
SHEET_EXT = {".xlsx", ".xlsm"}
TEXT_EXT = {".txt", ".md", ".csv"}
WORD = {"docx", "doc", "odt", "rtf"}             # letti dalla struttura (i vecchi passano da docx)
PRESENTAZIONI = {"pptx", "ppt", "odp"}
FOGLI_VECCHI = {"xls", "ods"}                    # -> xlsx con LibreOffice, poi celle esatte
ARCHIVI = {"zip", "tar", "gz", "bz2", "xz", "7z"}
POSTA = {"eml", "msg"}
AUDIO = {"mp3", "wav", "ogg", "opus", "flac", "m4a", "aac", "amr", "wma"}
VIDEO = {"mp4", "mov", "mkv", "webm", "avi", "wmv", "mpg", "ts", "flv", "3gp"}
PROMPTS = {"text": "OCR:", "table": "Table Recognition:", "formula": "Formula Recognition:"}
PROMPTS_MINICPM = {"text": "Text Recognition:", "table": "Table Recognition:",
                   "formula": "Formula Recognition:"}
MAX_TOKENS = {"text": 2048, "table": 8192, "formula": 1024}
MATH_FONTS = re.compile(r"CMMI|CMSY|CMEX|MSBM|Math|Symbol|MTExtra|STIX|Euclid|Cambria", re.I)
MATH_CHARS = set("∑∫∏√∞≤≥≠±∂∇∈∉⊂⊆∪∩→⇒⇔∀∃αβγδεθλμπσφω")
TESTATE = {"header", "footer", "number", "header_image", "footer_image"}
FIGURE = {"image", "figure", "picture", "chart", "seal", "header_image", "footer_image"}
ESTENSIONI = {"image/png": "png", "image/jpeg": "jpg"}
OUTPUT = config.cartella("output")


def impronta(dati: bytes) -> str:
    """Nome di un'immagine: SHA-256 del contenuto, primi 128 bit in esadecimale (32 caratteri).
    Immagini identiche hanno lo stesso nome (si salvano una volta sola); per due immagini diverse
    con lo stesso nome servirebbero ~10^19 immagini: in pratica impossibile."""
    return hashlib.sha256(dati).hexdigest()[:32]
JEV_SOGLIA = 0.5          # sotto: la regione e' segnalata da verificare
JEV_MAX_TABELLA = 2000    # oltre questi caratteri il voto di Jev sulle tabelle non e' affidabile
JEV_MIN_CARATTERI = 10    # le regioni piu' corte non si votano: il voto non significa niente


# ---------------------------------------------------------------- tipo del documento
def _media(data: bytes) -> str | None:
    """Audio e video dalle firme dei contenitori."""
    h = data[:16]
    if h[:3] == b"ID3" or (len(h) > 1 and h[0] == 0xFF and h[1] & 0xF6 == 0xF2):
        return "mp3"
    if len(h) > 1 and h[0] == 0xFF and h[1] & 0xF6 == 0xF0:
        return "aac"
    if h[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if h[:4] == b"RIFF" and data[8:12] == b"AVI ":
        return "avi"
    if h[:4] == b"OggS":
        return "opus" if b"OpusHead" in data[:100] else "ogg"
    if h[:4] == b"fLaC":
        return "flac"
    if h[:6] == b"#!AMR\n":
        return "amr"
    if data[4:8] == b"ftyp":
        marca = data[8:12]
        if marca in (b"heic", b"heix", b"mif1", b"msf1", b"avif"):
            return None                                   # immagini HEIC/AVIF, non video
        if marca in (b"M4A ", b"M4B ", b"M4P "):
            return "m4a"
        if marca == b"qt  ":
            return "mov"
        if marca.startswith(b"3g"):
            return "3gp"
        return "mp4"
    if h[:4] == b"\x1a\x45\xdf\xa3":
        return "webm" if b"webm" in data[:64] else "mkv"
    if h[:8] == b"\x30\x26\xb2\x75\x8e\x66\xcf\x11":
        return "wmv"
    if h[:4] in (b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3"):
        return "mpg"
    if h[:4] == b"FLV\x01":
        return "flv"
    if len(data) > 376 and data[0] == 0x47 and data[188] == 0x47 and data[376] == 0x47:
        return "ts"
    return None


EML_TESTA = re.compile(r"^(Received|Return-Path|From|Date|Subject|To|Message-ID|MIME-Version|"
                       r"Delivered-To|X-[\w-]+|Content-Type):", re.I | re.M)


def riconosci_tipo(data: bytes, nome: str = "") -> str:
    """Tipo del documento dal contenuto (non dal nome, che nei DB puo' mancare o mentire):
    pdf, docx, xlsx, pptx, doc, xls, ppt, odt, ods, odp, rtf, html, png, jpg, gif, tif, bmp,
    webp, csv, txt, md, epub, zip, tar, gz, bz2, xz, 7z, eml, msg, ipynb, json, xml, svg,
    audio (mp3, wav, ...), video (mp4, mkv, ...), sconosciuto."""
    h = data[:16]
    if h.startswith(b"%PDF-") or b"%PDF-" in data[:1024]:
        return "pdf"
    if len(data) > 262 and data[257:262] == b"ustar":
        return "tar"
    for firma, t in ((b"\x1f\x8b", "gz"), (b"BZh", "bz2"), (b"\xfd7zXZ\x00", "xz"),
                     (b"7z\xbc\xaf\x27\x1c", "7z")):
        if h.startswith(firma):
            return t
    m = _media(data)
    if m:
        return m
    if h.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if h.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if h[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if h[:4] in (b"II*\x00", b"MM\x00*"):
        return "tif"
    if h[:2] == b"BM":
        return "bmp"
    if h[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if h.startswith(b"{\\rtf"):
        return "rtf"
    if h[:4] == b"PK\x03\x04":                      # zip: Office moderno o OpenDocument
        try:
            import zipfile
            z = zipfile.ZipFile(io.BytesIO(data))
            nomi = set(z.namelist())
            if "mimetype" in nomi:
                mt = z.read("mimetype").decode(errors="ignore")
                if "epub" in mt:
                    return "epub"
                for k, t in (("text", "odt"), ("spreadsheet", "ods"), ("presentation", "odp")):
                    if k in mt:
                        return t
            if any(n.startswith("word/") for n in nomi):
                return "docx"
            if any(n.startswith("xl/") for n in nomi):
                return "xlsm" if "xl/vbaProject.bin" in nomi else "xlsx"
            if any(n.startswith("ppt/") for n in nomi):
                return "pptx"
            return "zip"
        except Exception:
            pass
        return "sconosciuto"
    if h[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":   # OLE2: Office 97-2003, posta di Outlook
        if "__substg1.0_".encode("utf-16-le") in data and \
                "__properties_version1.0".encode("utf-16-le") in data:
            return "msg"
        for nome_flusso, t in (("WordDocument", "doc"), ("Workbook", "xls"), ("Book", "xls"),
                               ("PowerPoint Document", "ppt")):
            if nome_flusso.encode("utf-16-le") in data:
                return t
        return "sconosciuto"
    testa = data[:4096]
    try:
        testo = testa.decode("utf-8")
    except UnicodeDecodeError:
        try:
            testo = testa.decode("cp1252")
        except UnicodeDecodeError:
            return "sconosciuto"
    if sum(c.isprintable() or c in "\r\n\t" for c in testo) < 0.95 * max(1, len(testo)):
        return "sconosciuto"
    inizio = testo.lstrip("﻿ \r\n\t")[:2000]
    if len(EML_TESTA.findall(re.split(r"\r?\n\r?\n", testo, maxsplit=1)[0])) >= 3:
        return "eml"                       # prima dell'HTML: un messaggio puo' contenere HTML
    if re.search(r"<(!doctype html|html|body|table|p)[\s>]", inizio, re.I):
        return "html"
    if inizio.startswith("<?xml") or inizio.startswith("<svg"):
        return "svg" if re.search(r"<svg[\s>]", inizio) else "xml"
    if inizio[:1] in "{[":
        if '"cells"' in testo and '"nbformat"' in data[-4096:].decode("utf-8", "ignore") + testo:
            return "ipynb"
        try:
            json.loads(data)
            return "json"
        except ValueError:
            pass
    est = Path(nome).suffix.lower().lstrip(".")
    if est in ("csv", "md"):
        return est
    righe = [r for r in testo.splitlines()[:20] if r.strip()]
    if len(righe) >= 2 and len({r.count(";") for r in righe}) == 1 and righe[0].count(";") >= 1:
        return "csv"
    if len(righe) >= 2 and len({r.count(",") for r in righe}) == 1 and righe[0].count(",") >= 2:
        return "csv"
    return "txt"


# ---------------------------------------------------------------- impostazioni
@dataclass
class Config:
    llm_url: str = config.valore("servizi", "llm_url")
    jev_url: str = config.valore("servizi", "jev_url")
    layout_dir: Path = Path(config.valore("modelli", "layout_dir")).expanduser()
    layout_soglia: float = 0.4
    lettori: dict = field(default_factory=lambda: {
        "text": config.valore("modelli", "testo"),
        "table": config.valore("modelli", "tabelle"),
        "formula": config.valore("modelli", "formule")})
    dpi: int = 150            # rendering delle scansioni per il divisore e i lettori
    dpi_figure: int = 220     # ritaglio delle figure per schema_raster
    timeout: int = 300
    whisper: str = config.valore("modelli", "whisper")                 # nome del modello in llama-swap
    profondita: int = config.intero("conversione", "profondita")      # archivi dentro archivi
    max_estratto_mb: int = config.intero("conversione", "max_estratto_mb")   # archivi aperti


@dataclass
class Opzioni:
    immagini: str = "embedded"   # embedded | separate | none
    jev: bool = False
    doc_id: str | None = None
    testate: bool = False        # intestazioni, piè di pagina e numeri di pagina
    ocr: bool = False            # legge con i modelli anche le pagine native (prove, PDF dubbi)
    nome: str = "documento"
    lingua: str | None = None    # trascrizioni: "it", "en"... (predefinito: riconosciuta)
    contesto: str | None = None  # trascrizioni: nomi e termini da scrivere giusti ("Marco Vannini, Eckhart")
    segmenti: bool = False       # trascrizioni: nel rapporto anche i segmenti di whisper (inizio, fine, testo)


class Errore(Exception):
    """Errore da mostrare all'utente (formato non gestito, servizio assente...)."""


# ---------------------------------------------------------------- file temporanei in RAM
# Regola: i documenti non si scrivono su disco. Dove un programma esterno vuole un file
# (ffmpeg, LibreOffice, yt-dlp) si usa una cartella in /dev/shm (memoria), cancellata dallo
# stesso processo prima di restituire il risultato. Le cartelle di un processo morto senza
# pulire (kill -9) si cancellano al successivo avvio di doc2md.
RAM = "/dev/shm" if os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK) else None


def _pulisci_orfani():
    import glob
    for p in glob.glob(f"{RAM or tempfile.gettempdir()}/doc2md-*"):
        m = re.match(r"doc2md-(\d+)-", os.path.basename(p))
        if not m:
            continue
        try:
            os.kill(int(m[1]), 0)                  # processo ancora vivo: non si tocca
        except ProcessLookupError:
            shutil.rmtree(p, ignore_errors=True)
        except PermissionError:
            pass


_pulisci_orfani()


def cartella_ram(nome: str = "lavoro"):
    """with cartella_ram() as d: ... -> cartella privata (0700) in memoria, cancellata all'uscita."""
    return tempfile.TemporaryDirectory(prefix=f"doc2md-{os.getpid()}-{nome}-", dir=RAM)


# ---------------------------------------------------------------- servizi
_layout, _layout_lock = None, threading.Lock()


class _Silenzioso:
    """Il modulo del divisore stampa avvisi innocui di overflow: qui si tacciono."""
    def __init__(self, lay):
        self.lay = lay

    def detect(self, png):
        import warnings
        import numpy as np
        with warnings.catch_warnings(), np.errstate(over="ignore", invalid="ignore"):
            warnings.simplefilter("ignore", RuntimeWarning)
            return self.lay.detect(png)


def layout(cfg: Config):
    global _layout
    with _layout_lock:
        if _layout is None:
            _layout = _Silenzioso(DocLayout(cfg.layout_dir, cfg.layout_soglia))
        return _layout


def leggi(cfg: Config, cat: str, png: bytes) -> str:
    """Una regione -> testo pulito, con il lettore scelto per la categoria."""
    model = cfg.lettori[cat]
    prompt = (PROMPTS_MINICPM if model.startswith("minicpm") else PROMPTS)[cat]
    text, *_ = ask(cfg.llm_url, model, prompt, png, "image/png", MAX_TOKENS[cat], cfg.timeout)
    return clean(text, cat)


def ocr_breve(cfg: Config):
    """Lettore per le scritte degli schemi (blocchi ed etichette)."""
    def f(png):
        try:
            return leggi(cfg, "text", png)
        except Exception:
            return ""
    return f


# ---------------------------------------------------------------- utilita'
def png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def render(page, dpi, clip=None) -> bytes:
    return page.get_pixmap(dpi=dpi, clip=clip, alpha=False).tobytes("png")


def _file_img(mime: str, b64: str) -> str:
    return f"{impronta(base64.b64decode(b64))}.{ESTENSIONI[mime]}"


def per_incorporare(png: bytes) -> tuple[str, str]:
    """Immagine -> (mime, base64): JPEG se il PNG e' pesante (foto)."""
    if len(png) > 200_000:
        img = Image.open(io.BytesIO(png)).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85)
        if buf.tell() < len(png):
            return "image/jpeg", base64.b64encode(buf.getvalue()).decode()
    return "image/png", base64.b64encode(png).decode()


def tabella_coerente(html: str) -> bool:
    """Controllo di struttura delle tabelle grandi (al posto di Jev): la maggior parte
    delle righe ha la stessa larghezza, contando colspan e le celle occupate da rowspan."""
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I)
    if len(rows) < 2:
        return True
    widths, carry = [], []          # carry: [colonne, righe ancora occupate]
    for r in rows:
        w = sum(c for c, _ in carry)
        carry = [[c, n - 1] for c, n in carry if n > 1]
        for attr in re.findall(r"<t[dh]([^>]*)>", r, re.I):
            cs = int((re.search(r'colspan="?(\d+)', attr) or [0, 1])[1])
            rs = int((re.search(r'rowspan="?(\d+)', attr) or [0, 1])[1])
            w += cs
            if rs > 1:
                carry.append([cs, rs - 1])
        widths.append(w)
    moda = statistics.mode(widths)
    return sum(w == moda for w in widths) >= 0.8 * len(widths)


@dataclass
class Pezzo:
    """Un elemento della pagina, da mettere in ordine di lettura."""
    y: float
    x: float
    md: str
    ordine: int | None = None
    r: object = None            # riquadro sulla pagina, per l'ordine di lettura a colonne


class Documento:
    def __init__(self, cfg: Config, opz: Opzioni, doc_id: str):
        self.cfg, self.opz, self.doc_id = cfg, opz, doc_id
        # nomi delle immagini: il doc_id se corto, altrimenti un'impronta (i nomi di file hanno un limite)
        self.prefisso = doc_id if len(doc_id) <= 48 else "h" + hashlib.sha1(doc_id.encode()).hexdigest()[:20]
        self.immagini = []      # (rif, mime, base64)
        self.pagine = []        # markdown per pagina
        self.info_pagine = []   # dict per pagina
        self.jev = []           # voti di tutte le regioni votate
        self.sotto_soglia = 0
        self.tabelle = {True: 0, False: 0}   # tabelle grandi: controllo di struttura
        self.schemi = {"mermaid": 0, "ripiego": 0}
        self.avvisi = []
        self.segmenti = []      # trascrizioni con opz.segmenti: [{"inizio", "fine", "testo", "verifica"}]
        self.testate_pdf = frozenset()   # intestazioni ripetute nelle pagine native
        self.profondita = 0              # archivi e allegati: livello di annidamento
        self.membri = []                 # archivi e posta: un elemento per file contenuto
        self.estratto = 0                # byte estratti dagli archivi (contro gli archivi "bomba")
        self.meta = {}                   # righe in piu' per l'intestazione (titolo, autore, durata...)

    # -- archivi e allegati: ogni file contenuto e' un sotto-documento con le sue immagini
    def figlio(self, n: int) -> "Documento":
        f = Documento(self.cfg, self.opz, f"{self.doc_id[:200]}-m{n}")
        f.prefisso = f"{self.prefisso}-m{n}"
        f.profondita = self.profondita + 1
        f.radice = getattr(self, "radice", self)      # il conteggio dei byte estratti e' unico
        return f

    def unisci(self, f: "Documento"):
        self.immagini += f.immagini
        self.jev += f.jev
        for k in self.schemi:
            self.schemi[k] += f.schemi[k]
        for k in self.tabelle:
            self.tabelle[k] += f.tabelle[k]

    # -- immagini
    def figura(self, png: bytes, pagina: int, didascalia: str = "Figura") -> str:
        k = sum(1 for r, *_ in self.immagini if f"-p{pagina}-" in r) + 1
        rif = f"img-{self.prefisso}-p{pagina}-{k}"
        if self.opz.immagini == "none":
            return f"<!-- immagine omessa: {rif} -->"
        mime, data = per_incorporare(png)
        self.immagini.append((rif, mime, data))
        return f"![{didascalia} p{pagina}-{k}][{rif}]"

    def schema_da_immagine(self, png: bytes, pagina: int) -> str:
        """Figura -> Mermaid se e' uno schema a blocchi riconosciuto, altrimenti immagine."""
        try:
            mer, rep = schema_raster.convert(png, ocr_breve(self.cfg))
        except Exception as e:      # una figura strana non deve fermare il documento
            mer, rep = "", {"esito": "ripiego", "errore": str(e)}
        if rep.get("esito") == "mermaid":
            self.schemi["mermaid"] += 1
            return f"```mermaid\n{mer}\n```"
        self.schemi["ripiego"] += 1
        return self.figura(png, pagina)

    # -- Jev
    def vota(self, regioni, pagina_png: bytes):
        """regioni: dict con categoria, box, testo, ritaglio. Aggiunge 'voto'."""
        items = []
        for i, r in enumerate(regioni):
            t = r.get("testo") or ""
            if len(t) < JEV_MIN_CARATTERI:
                continue
            if r["categoria"] == "table" and len(t) > JEV_MAX_TABELLA:
                r["coerente"] = tabella_coerente(t)
                self.tabelle[r["coerente"]] += 1
                continue
            items.append({"case_id": str(i), "image": base64.b64encode(r["ritaglio"]).decode(),
                          "ocr_text": t, "category": r["categoria"]})
        if not items:
            return
        try:
            res = jev_score(self.cfg.jev_url, items, self.cfg.timeout)
        except Exception as e:
            self.avvisi.append(f"Jev non disponibile: {e}")
            return
        for k, (verdetto, voto) in res.items():
            if verdetto == "scored" and voto is not None:
                regioni[int(k)]["voto"] = float(voto)
                self.jev.append(float(voto))


# ---------------------------------------------------------------- smistamento
def tipo_pagina(page) -> tuple[str, dict]:
    """'nativa' | 'scansione', con i dettagli che hanno deciso."""
    text = page.get_text("text")
    chars = [c for c in text if not c.isspace()]
    area = abs(page.rect)
    img_area = 0.0
    for info in page.get_images(full=True):
        try:
            for r in page.get_image_rects(info[0]):
                img_area += abs(r & page.rect)
        except Exception:
            pass
    cop = min(1.0, img_area / area) if area else 0
    det = {"caratteri": len(chars), "copertura_immagini": round(cop, 2)}
    if len(chars) < 40:
        return ("scansione" if cop > 0.3 or len(chars) == 0 else "nativa"), det
    lettere = sum(c.isalnum() for c in chars) / len(chars)
    strani = sum(c == "\ufffd" or 0xE000 <= ord(c) <= 0xF8FF for c in chars) / len(chars)
    det["lettere"], det["strani"] = round(lettere, 2), round(strani, 3)
    # testo senza mappatura Unicode (simboli a caso): meglio leggerlo come immagine
    if lettere < 0.45 or strani > 0.05:
        return "scansione", det
    # scansione con strato OCR invisibile sopra: si rilegge (l'OCR vecchio e' spesso scarso)
    if cop > 0.85 and _testo_invisibile(page):
        return "scansione", det
    return "nativa", det


def _testo_invisibile(page) -> bool:
    try:
        spans = [s for b in page.get_texttrace() for s in [b]]
        if not spans:
            return False
        inv = sum(1 for s in spans if s.get("type") == 3 or s.get("opacity", 1) == 0)
        return inv > 0.8 * len(spans)
    except Exception:
        return False


def ha_formule(page) -> bool:
    fonts = {f[3] for f in page.get_fonts(full=True)}
    if any(MATH_FONTS.search(f or "") for f in fonts):
        return True
    return sum(c in MATH_CHARS for c in page.get_text("text")) >= 5


# ---------------------------------------------------------------- pagina nativa
ELENCO = re.compile(r"^\s*(\d+(\.\d+)*\.?|[A-Z](\.\d+)+|[-•–*▪◦])\s|\s\d+\s*$")


def norma_testata(t: str) -> str:
    """Testo di intestazione/piè di pagina senza numeri: uguale su tutte le pagine."""
    return re.sub(r"[\d\s]+", " ", t.lower()).strip()


def in_margine(r, page) -> bool:
    """Fascia alta o bassa della pagina. Larga (15%): LaTeX e Word mettono intestazioni e
    piè di pagina a distanze diverse; a togliere decide la ripetizione su molte pagine."""
    H = page.rect.height
    return r.y1 < 0.15 * H or r.y0 > 0.85 * H


def testate_ripetute(pdf) -> set:
    """Righe nei margini alti e bassi che si ripetono su molte pagine (titolo corrente,
    copyright, numero di pagina): nelle pagine native si tolgono come nelle scansioni."""
    conta = {}
    for page in pdf:
        visti = set()
        for b in page.get_text("blocks"):
            r = pymupdf.Rect(b[:4])
            if b[6] == 0 and in_margine(r, page):
                k = norma_testata(b[4])
                if k and k not in visti:
                    visti.add(k)
                    conta[k] = conta.get(k, 0) + 1
    soglia = max(3, 0.2 * len(pdf))
    return {k for k, n in conta.items() if n >= soglia}


def _dividi_blocchi(blocchi, vuoto=15):
    """Un blocco del PDF puo' unire righe di colonne diverse (due firme affiancate):
    si divide dove nessuna riga attraversa uno spazio vuoto verticale di almeno `vuoto` pt."""
    out = []
    for b in blocchi:
        if b.get("type") != 0 or len(b.get("lines", [])) < 2:
            out.append(b)
            continue
        gruppi = []                       # [x0, x1, righe]
        for l in sorted(b["lines"], key=lambda l: l["bbox"][0]):
            x0, x1 = l["bbox"][0], l["bbox"][2]
            if gruppi and x0 - gruppi[-1][1] < vuoto:
                gruppi[-1][1] = max(gruppi[-1][1], x1)
                gruppi[-1][2].append(l)
            else:
                gruppi.append([x0, x1, [l]])
        if len(gruppi) == 1:
            out.append(b)
            continue
        for _, _, righe in gruppi:
            righe.sort(key=lambda l: (l["bbox"][1], l["bbox"][0]))
            r = pymupdf.Rect(righe[0]["bbox"])
            for l in righe[1:]:
                r |= l["bbox"]
            out.append({**b, "bbox": tuple(r), "lines": righe})
    return out


def blocchi_testo(page, escludi, togli=frozenset()):
    """Testo nativo -> pezzi Markdown (titoli dalla dimensione del carattere).
    togli: testi normalizzati di intestazioni/piè di pagina da saltare nei margini."""
    d = page.get_text("dict", sort=True)
    sizes = []
    for b in d["blocks"]:
        for l in b.get("lines", []):
            for s in l["spans"]:
                sizes += [round(s["size"], 1)] * len(s["text"].strip())
    corpo = statistics.median(sizes) if sizes else 10
    out = []
    for b in _dividi_blocchi(d["blocks"]):
        if b.get("type") != 0:
            continue
        r = pymupdf.Rect(b["bbox"])
        if any(abs(r & e) > 0.5 * abs(r) for e in escludi if abs(r)):
            continue
        # righe visive: pezzi di testo alla stessa altezza (indici, elenchi a colonne)
        righe, dim, gras = [], 0, True
        for l in b["lines"]:
            t = "".join(s["text"] for s in l["spans"]).strip()
            if not t:
                continue
            y0, y1 = l["bbox"][1], l["bbox"][3]
            # stessa riga visiva: altezze sovrapposte per piu' di meta' (font diversi
            # sulla stessa riga hanno basi e altezze un po' diverse)
            if righe and min(righe[-1][0][1], y1) - max(righe[-1][0][0], y0) > 0.5 * min(righe[-1][0][1] - righe[-1][0][0], y1 - y0):
                righe[-1][1].append((l["bbox"][0], t))
            else:
                righe.append([(y0, y1), [(l["bbox"][0], t)]])
            for s in l["spans"]:
                if s["text"].strip():
                    dim = max(dim, s["size"])
                    gras &= bool(s["flags"] & 16) or "Bold" in s["font"]
        # pezzi della stessa riga visiva da sinistra a destra
        righe = [PUNTINI.sub(" … ", " ".join(t for _, t in sorted(seg))).strip() for _, seg in righe]
        if not righe:
            continue
        if togli and in_margine(r, page) and norma_testata(" ".join(righe)) in togli:
            continue
        if len(righe) > 1 and sum(bool(ELENCO.search(t)) for t in righe) >= 0.6 * len(righe):
            testo = "\n".join(righe)        # elenco o indice: una voce per riga
        else:
            testo = ""
            for t in righe:          # a capo con trattino: parola spezzata
                if testo.endswith("-") and t[:1].islower():
                    testo = testo[:-1] + t
                else:
                    testo = f"{testo} {t}" if testo else t
        if dim >= 1.5 * corpo and len(testo) < 150:
            md = "# " + testo
        elif dim >= 1.2 * corpo and len(testo) < 200:
            md = "## " + testo
        elif gras and len(testo) < 120 and len(righe) <= 2:
            md = "### " + testo
        else:
            md = testo
        out.append(Pezzo(r.y0, r.x0, md, r=r))
    return out


def schema_vettoriale(doc: Documento, page, num, tabelle):
    """Schema disegnato nel PDF -> (pezzo, area da escludere) oppure (None, None)."""
    disegni = [d for d in page.get_drawings()
               if not any(abs(pymupdf.Rect(d["rect"]) & t) > 0.5 * abs(pymupdf.Rect(d["rect"]))
                          for t in tabelle if abs(pymupdf.Rect(d["rect"])))]
    if len(disegni) < 12:
        return None, None
    try:
        if tabelle:
            # le celle delle tabelle sembrano riquadri: si lavora su una copia in memoria
            # della pagina con le tabelle cancellate
            tmp = pymupdf.open()
            tmp.insert_pdf(page.parent, from_page=page.number, to_page=page.number)
            tp = tmp[0]
            for t in tabelle:
                tp.add_redact_annot(t + (-1, -1, 1, 1))
            tp.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                                graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED)
            mer, rep, nodes, final = schema_geo.convert(tp)
        else:
            mer, rep, nodes, final = schema_geo.convert(page)
    except Exception as e:
        doc.avvisi.append(f"p{num}: schema vettoriale non letto ({e})")
        return None, None
    if len(nodes) < 3:
        return None, None
    # riquadri senza nessun collegamento: cornici di un modulo (le sezioni di un referto),
    # non uno schema. Sui casi verificati: 1 schema vero su 59 senza collegamenti (e non
    # convertibile), 7 falsi schemi su 15. Il testo resta testo.
    if not rep.get("collegamenti"):
        return None, None
    area = pymupdf.Rect(nodes[0].r)
    for n in nodes[1:]:
        area |= n.r
    area = (area + (-12, -12, 12, 12)) & page.rect
    if rep.get("esito") == "mermaid":
        doc.schemi["mermaid"] += 1
        md = f"```mermaid\n{mer}\n```"
    else:
        doc.schemi["ripiego"] += 1
        md = doc.figura(render(page, doc.cfg.dpi_figure, area), num, "Schema")
    # i blocchi arrivano in coordinate visibili (il ritaglio le usa cosi'); testo e
    # ordinamento della pagina lavorano su quelle non ruotate
    if page.rotation:
        area = (area * page.derotation_matrix).normalize()
    return Pezzo(area.y0, area.x0, md, r=area), area


def immagini_nativa(doc: Documento, page, num, escludi):
    """Immagini dentro una pagina nativa: schema -> Mermaid, altrimenti incorporate."""
    out, aree = [], []
    minimo = 0.02 * abs(page.rect)
    for info in page.get_images(full=True):
        try:
            rects = page.get_image_rects(info[0])
        except Exception:
            continue
        for r in rects:
            vis = r & page.rect
            if abs(vis) < minimo or any(abs(vis & a) > 0.8 * abs(vis) for a in aree + escludi):
                continue
            aree.append(vis)
            png = None
            if abs(vis) > 0.98 * abs(r):        # immagine intera: si usa l'originale
                try:
                    x = page.parent.extract_image(info[0])
                    im = Image.open(io.BytesIO(x["image"]))
                    if im.width >= 1.5 * vis.width:   # piu' definita del rendering a 72 dpi
                        png = png_bytes(im.convert("RGB"))
                except Exception:
                    png = None
            if png is None:                      # ritagliata, ruotata o in formato strano
                png = render(page, doc.cfg.dpi_figure, vis)
            out.append(Pezzo(vis.y0, vis.x0, doc.schema_da_immagine(png, num), r=vis))
    return out, aree


def formule_native(doc: Documento, page, num, tabelle=()):
    """Pagina nativa con formule: il divisore le trova, MiniCPM-OCR le scrive in LaTeX.
    Il testo intorno resta quello del PDF."""
    png = render(page, doc.cfg.dpi)
    dets = layout(doc.cfg).detect(png)
    sx = page.rect.width / Image.open(io.BytesIO(png)).width
    out, aree, regioni = [], [], []
    parole = page.get_text("words")
    for d in dets:
        lab = d["label"].lower()
        if lab not in ("display_formula", "formula", "equation"):
            continue
        box = [v for v in d["box"]]
        r = pymupdf.Rect(box[0] * sx, box[1] * sx, box[2] * sx, box[3] * sx) + (-2, -2, 2, 2)
        if any(abs(r & t) > 0.5 * abs(r) for t in tabelle):
            continue
        # formula dentro una riga di testo: resta il testo del PDF (spostarla in un
        # blocco a parte romperebbe la frase)
        accanto = [w for w in parole if r.y0 <= (w[1] + w[3]) / 2 <= r.y1
                   and not pymupdf.Rect(w[:4]).intersects(r)]
        if len(accanto) >= 3:
            continue
        crop = render(page, 220, r)
        try:
            t = leggi(doc.cfg, "formula", crop)
        except Exception as e:
            doc.avvisi.append(f"p{num}: formula non letta ({e})")
            continue
        if not t:
            continue
        reg = {"categoria": "formula", "testo": t, "ritaglio": crop}
        regioni.append(reg)
        out.append((Pezzo(r.y0, r.x0, "", r=r), reg))
        aree.append(r)
    if doc.opz.jev:
        doc.vota(regioni, png)
    pezzi = []
    for p, reg in out:
        p.md = f"$$\n{reg['testo']}\n$$" + _nota_voto(reg)
        pezzi.append(p)
    return pezzi, aree, [r.get("voto") for r in regioni if r.get("voto") is not None]


def _nota_voto(reg) -> str:
    v = reg.get("voto")
    if v is not None and v < JEV_SOGLIA:
        return f"\n<!-- verifica: voto Jev {v:.2f} -->"
    if reg.get("coerente") is False:
        return "\n<!-- verifica: righe della tabella di lunghezza diversa -->"
    return ""


PUNTINI = re.compile(r"(\.\s?){5,}")   # puntini di guida degli indici


def tabella_vera(t, page, linee_v) -> bool:
    """find_tables vede tabelle anche negli indici e nelle pagine con righe di
    intestazione: si tengono le tabelle con righe verticali disegnate, oppure
    quelle senza righe ma piene e senza puntini di guida."""
    celle = [c for row in t.extract() for c in row]
    if not celle:
        return False
    testi = [c for c in celle if c and c.strip()]
    if sum(bool(PUNTINI.search(c)) for c in testi) >= 3:
        return False                                   # indice
    bb = pymupdf.Rect(t.bbox)
    verticali = sum(1 for r in linee_v if r.intersects(bb) and r.height > 0.3 * bb.height)
    if verticali >= 2:
        return True
    vuote = 1 - len(testi) / len(celle)
    return vuote <= 0.2 and abs(bb) < 0.6 * abs(page.rect)


def linee_verticali(page):
    out = []
    for d in page.get_drawings():
        for it in d["items"]:
            if it[0] == "l" and abs(it[1].x - it[2].x) < 1.5:
                out.append(pymupdf.Rect(it[1], it[2]).normalize() + (-1, 0, 1, 0))
            elif it[0] == "re":
                r = pymupdf.Rect(it[1])
                if r.width < 2 and r.height > 5:        # riga sottile disegnata come rettangolo
                    out.append(r)
                elif r.width > 5 and r.height > 5:      # bordo di cella: lati verticali
                    out += [pymupdf.Rect(r.x0 - 1, r.y0, r.x0 + 1, r.y1),
                            pymupdf.Rect(r.x1 - 1, r.y0, r.x1 + 1, r.y1)]
    return out


def pagina_nativa(doc: Documento, page, num):
    tabelle, pezzi = [], []
    try:
        lv = None
        for t in page.find_tables().tables:
            md = t.to_markdown().strip()
            if lv is None:
                lv = linee_verticali(page)
            if md and t.row_count >= 2 and tabella_vera(t, page, lv):
                r = pymupdf.Rect(t.bbox)
                tabelle.append(r)
                pezzi.append(Pezzo(r.y0, r.x0, md, r=r))
    except Exception as e:
        doc.avvisi.append(f"p{num}: tabelle non lette ({e})")
    escludi = list(tabelle)
    sch, area = schema_vettoriale(doc, page, num, tabelle)
    solo_immagini = []
    if sch:
        pezzi.append(sch)
        if sch.md.startswith("```mermaid"):
            escludi.append(area)          # le etichette sono nel Mermaid
        else:
            solo_immagini.append(area)    # schema come immagine: il testo dell'area resta anche come testo
    figs, aree = immagini_nativa(doc, page, num, escludi + solo_immagini)
    pezzi += figs
    escludi += aree
    voti, metodo = [], "nativa"
    if ha_formule(page):
        try:
            f, aree_f, voti = formule_native(doc, page, num, tabelle)
            pezzi += f
            escludi += aree_f
            metodo = "nativa+formule"
        except Exception as e:
            doc.avvisi.append(f"p{num}: formule lasciate come testo ({e})")
    togli = doc.testate_pdf if (num > 1 and not doc.opz.testate) else frozenset()
    for pz, area in riquadri_campi(page, escludi):
        pezzi.append(pz)
        escludi.append(area)
    pezzi += blocchi_testo(page, escludi, togli)
    pezzi = _unisci_righe(ordine_lettura(pezzi))
    return "\n\n".join(p.md for p in pezzi), metodo, voti


def riquadri_campi(page, escludi):
    """Riquadri disegnati che contengono campi di un report (etichette e valori in righe,
    ciascuno nel suo rettangolo di ritaglio: i-net Clear Reports, Crystal Reports e simili)
    -> tabella Markdown senza intestazione, una riga del riquadro per riga di tabella."""
    try:
        dis = page.get_drawings(extended=True)
    except Exception:
        return []
    clip = [pymupdf.Rect(x["scissor"]) for x in dis if x.get("type") == "clip"]
    cornici = [pymupdf.Rect(x["rect"]) for x in dis if x.get("type") == "s"
               and x["rect"].width > 0.3 * page.rect.width and x["rect"].height > 20]
    if not clip or not cornici:
        return []
    righe_testo = []
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            t = "".join(s["text"] for s in l["spans"]).strip()
            if t:
                righe_testo.append((pymupdf.Rect(l["bbox"]), t))
    out = []
    for c in cornici:
        if any(abs(c & e) > 0.3 * abs(c) for e in escludi if abs(c)):
            continue
        dentro = [(r, t) for r, t in righe_testo if c.contains(r + (1, 1, -1, -1))]
        if len(dentro) < 4 or len(dentro) > 80:
            continue
        # ogni riga di testo nel suo campo (rettangolo di ritaglio piu' piccolo che la contiene)
        campi = []
        for r, t in dentro:
            cc = [k for k in clip if k.contains(pymupdf.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)) and abs(k) < 0.5 * abs(c)]
            if not cc:
                break
            k = min(cc, key=abs)
            campi.append((k, r, t))
        else:
            if len({(round(k.x0), round(k.y0)) for k, _, _ in campi}) < 0.8 * len(campi):
                continue          # i campi non sono rettangoli separati: testo normale
            # righe: campi che si sovrappongono in altezza per piu' di meta'
            campi.sort(key=lambda z: (z[0].y0, z[0].x0))
            righe = []
            for k, r, t in campi:
                for rg in righe:
                    a = rg[0][0]
                    if min(a.y1, k.y1) - max(a.y0, k.y0) > 0.5 * min(a.height, k.height):
                        rg.append((k, r, t))
                        break
                else:
                    righe.append([(k, r, t)])
            if len(righe) < 2:
                continue
            # colonne: zone orizzontali coperte dal testo, separate da almeno 8 pt di vuoto
            # (etichetta e valore vicini finiscono nella stessa cella)
            col = []                      # [x0, x1]
            for x0, x1 in sorted((r.x0, r.x1) for _, r, _ in campi):
                if col and x0 - col[-1][1] < 8:
                    col[-1][1] = max(col[-1][1], x1)
                else:
                    col.append([x0, x1])
            tab = []
            for rg in sorted(righe, key=lambda g: min(z[0].y0 for z in g)):
                celle = [""] * len(col)
                for k, r, t in sorted(rg, key=lambda z: z[1].x0):
                    i = max(range(len(col)), key=lambda j: min(col[j][1], r.x1) - max(col[j][0], r.x0))
                    celle[i] = (celle[i] + " " + t).strip()
                tab.append(celle)
            # colonna di sole etichette ("Regime :"): si unisce al valore che segue
            for j in range(len(col) - 2, -1, -1):
                pieni = [r[j] for r in tab if r[j]]
                if pieni and all(x.rstrip().endswith(":") for x in pieni):
                    for r in tab:
                        r[j + 1] = (r[j] + " " + r[j + 1]).strip()
                        r[j] = ""
            usate = [j for j in range(len(col)) if any(r[j] for r in tab)]
            tab = [[r[j].replace("|", "\\|") for j in usate] for r in tab]
            md = "| " + " | ".join([" "] * len(usate)) + " |\n|" + "---|" * len(usate) + "\n"
            md += "\n".join("| " + " | ".join(r) + " |" for r in tab)
            out.append((Pezzo(c.y0, c.x0, md, r=c), c))
    return out


def _semplice(p):
    return p.r is not None and p.r.height < 20 and p.md and "\n" not in p.md and not p.md.startswith(("#", "!", "|", "$", "`", "<"))


def _unisci_righe(pezzi):
    """Pezzi di testo semplice consecutivi sulla stessa riga visiva ("Radiofarmaco:" e il
    suo valore) -> una riga sola, da sinistra a destra."""
    out = []
    for p in pezzi:
        q = out[-1] if out else None
        if q is not None and _semplice(p) and _semplice(q) and p.r.x0 >= q.r.x1 - 1:
            sov = min(p.r.y1, q.r.y1) - max(p.r.y0, q.r.y0)
            if sov > 0.5 * min(p.r.height, q.r.height):
                out[-1] = Pezzo(q.y, q.x, q.md + " " + p.md, r=q.r | p.r)
                continue
        out.append(p)
    return out


def _riq(p):
    return p.r if p.r is not None else pymupdf.Rect(p.x, p.y, p.x + 1, p.y + 1)


def _fasce(pezzi, asse, minimo):
    """Divide i pezzi dove c'e' uno spazio vuoto lungo l'asse (0 = in verticale -> righe,
    1 = in orizzontale -> colonne). minimo: larghezza minima dello spazio vuoto."""
    lim = sorted(((_riq(p).y0, _riq(p).y1) if asse == 0 else (_riq(p).x0, _riq(p).x1), i)
                 for i, p in enumerate(pezzi))
    gruppi, cur, fine = [], [], None
    for (a, b), i in lim:
        if cur and a - fine >= minimo:
            gruppi.append(cur)
            cur, fine = [], None
        cur.append(pezzi[i])
        fine = b if fine is None else max(fine, b)
    gruppi.append(cur)
    return gruppi


def _allineati(sx, dx):
    """Quota dei pezzi di destra che stanno alla stessa altezza di un pezzo di sinistra
    (modulo o tabella: si legge per righe; colonne di testo: si legge per colonne)."""
    if not dx:
        return 1.0
    n = 0
    for p in dx:
        a = _riq(p)
        for q in sx:
            b = _riq(q)
            sov = min(a.y1, b.y1) - max(a.y0, b.y0)
            if sov > 0.5 * min(a.height or 1, b.height or 1):
                n += 1
                break
    return n / len(dx)


def ordine_lettura(pezzi, prof=0):
    """Ordine di lettura XY-cut: si taglia la zona dove c'e' uno spazio vuoto che la
    attraversa tutta. Le colonne di testo (pezzi non allineati in altezza) si leggono una
    dopo l'altra; righe e moduli dall'alto in basso e da sinistra a destra."""
    if len(pezzi) <= 1 or prof > 30:
        return list(pezzi)
    colonne = _fasce(pezzi, 1, 8)
    if len(colonne) > 1:
        righe = _fasce(pezzi, 0, 0.5)
        sx = colonne[0]
        resto = [p for c in colonne[1:] for p in c]
        if len(righe) == 1 or (len(sx) >= 2 and len(resto) >= 2 and _allineati(sx, resto) < 0.5):
            return [q for c in colonne for q in ordine_lettura(c, prof + 1)]
    righe = _fasce(pezzi, 0, 0.5)
    if len(righe) > 1:
        return [q for r in righe for q in ordine_lettura(r, prof + 1)]
    # nessun taglio possibile: righe per sovrapposizione in altezza, poi da sinistra a destra
    righe = []
    for p in sorted(pezzi, key=lambda p: _riq(p).y0):
        a = _riq(p)
        if righe:
            b = righe[-1][0]
            if min(a.y1, b.y1) - max(a.y0, b.y0) > 0.5 * min(a.height or 1, b.height or 1):
                righe[-1][1].append(p)
                if a.height < b.height:       # si confronta con il pezzo piu' basso della riga
                    righe[-1][0] = a
                continue
        righe.append([a, [p]])
    return [p for _, g in righe for p in sorted(g, key=lambda p: _riq(p).x0)]


# ---------------------------------------------------------------- scansione
def pagina_scansione(doc: Documento, png: bytes, num: int, fonte_pdf=None):
    """Pagina come immagine: divisore + lettori. fonte_pdf=(page) per ritagliare
    le figure a risoluzione piu' alta."""
    img = Image.open(io.BytesIO(png)).convert("RGB")
    W, H = img.size
    dets = layout(doc.cfg).detect(png)
    regs = []
    for d in dets:
        lab = d["label"].lower()
        # intestazioni e piè di pagina: nella prima pagina spesso c'e' il titolo del
        # documento (nome del condominio, intestazione dello studio), quindi si tengono;
        # nelle altre si ripetono e si tolgono. I numeri di pagina si tolgono sempre.
        if not doc.opz.testate and (lab == "number" or (lab in TESTATE and num > 1)):
            continue
        x0, y0, x1, y1 = d["box"]
        box = [max(0, x0 - 4), max(0, y0 - 4), min(W, x1 + 4), min(H, y1 + 4)]
        if box[2] - box[0] < 12 or box[3] - box[1] < 8:
            continue
        if lab in FIGURE:
            cat = "figure"
        elif "table" in lab and not any(w in lab for w in ("title", "caption", "footnote", "note")):
            cat = "table"
        elif ("formula" in lab or "equation" in lab) and "number" not in lab:
            cat = "formula"
        else:
            cat = "text"
        regs.append({"tipo": lab, "categoria": cat, "box": [round(v) for v in box],
                     "score": d["score"], "ordine": d.get("order")})
    regs = mark_nested(dedup(regs))
    letti = []
    for r in regs:
        if r["contenuta"]:
            continue
        crop = png_bytes(img.crop(r["box"]))
        r["ritaglio"] = crop
        if r["categoria"] == "figure":
            if fonte_pdf is not None:      # dal PDF: ritaglio a risoluzione piu' alta
                s = fonte_pdf.rect.width / W
                b = r["box"]
                crop = render(fonte_pdf, doc.cfg.dpi_figure,
                              pymupdf.Rect(b[0] * s, b[1] * s, b[2] * s, b[3] * s))
            r["md"] = doc.schema_da_immagine(crop, num) if r["tipo"] in ("image", "figure", "chart") \
                else doc.figura(crop, num)
            continue
        try:
            r["testo"] = leggi(doc.cfg, r["categoria"], crop)
        except Exception as e:
            r["testo"] = ""
            doc.avvisi.append(f"p{num}: regione {r['tipo']} non letta ({e})")
        letti.append(r)
    if doc.opz.jev:
        doc.vota(letti, png)
    for r in regs:
        if r["contenuta"] or "md" in r:
            continue
        t = r.get("testo", "")
        if not t:
            continue
        if r["categoria"] == "formula":
            md = f"$${t}$$" if r["tipo"] == "inline_formula" else f"$$\n{t}\n$$"
        elif r["tipo"] == "doc_title":
            md = "# " + " ".join(t.split())
        elif r["tipo"] in ("paragraph_title", "title"):
            md = "## " + " ".join(t.split())
        else:
            md = t
        r["md"] = md + _nota_voto(r)
        # regione dubbia: con le immagini incorporate si aggiunge anche l'originale
        if (r.get("voto", 1) < JEV_SOGLIA or r.get("coerente") is False) and doc.opz.immagini != "none":
            r["md"] += "\n\n" + doc.figura(r["ritaglio"], num, "Originale da verificare")
    voti = [r["voto"] for r in letti if r.get("voto") is not None]
    md = "\n\n".join(r["md"] for r in regs if r.get("md"))
    return md, voti


# ---------------------------------------------------------------- formati
class Ufficio:
    """LibreOffice a richiesta, come i modelli in llama-swap: parte alla prima conversione
    Office (tramite unoserver), resta acceso finche' e' acceso doc2md, si chiude con lui.
    La conversione avviene in memoria: il documento non viene scritto su disco.
    Se unoserver non c'e', si torna a lanciare soffice per ogni documento."""

    def __init__(self):
        self.proc, self.url, self.lock, self.ram = None, None, threading.Lock(), None
        self.python = config.valore("servizio", "uno_python")

    @staticmethod
    def _porta() -> int:
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def _avvia(self):
        import atexit
        import signal
        import xmlrpc.client
        porta, uno = self._porta(), self._porta()

        def figlio():                     # se doc2md muore, muore anche LibreOffice
            try:
                import ctypes
                ctypes.CDLL("libc.so.6").prctl(1, signal.SIGTERM)   # PR_SET_PDEATHSIG
            except Exception:
                pass
        # profilo e file temporanei di LibreOffice in RAM: niente "documenti recenti",
        # copie di ripristino o file di lavoro su disco; si cancellano quando si chiude
        self.ram = tempfile.mkdtemp(prefix=f"doc2md-{os.getpid()}-uno-", dir=RAM)
        os.makedirs(f"{self.ram}/tmp")
        env = dict(os.environ, TMPDIR=f"{self.ram}/tmp", TMP=f"{self.ram}/tmp", TEMP=f"{self.ram}/tmp")
        self.proc = subprocess.Popen(
            [self.python, "-m", "unoserver.server", "--interface", "127.0.0.1",
             "--port", str(porta), "--uno-port", str(uno),
             "--user-installation", f"{self.ram}/profilo"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, preexec_fn=figlio, env=env)
        atexit.register(self.chiudi)
        self.url = f"http://127.0.0.1:{porta}"
        t0 = time.time()
        while time.time() - t0 < 60:      # LibreOffice impiega qualche secondo a partire
            if self.proc.poll() is not None:
                raise RuntimeError(f"unoserver terminato (codice {self.proc.returncode}): "
                                   f"manca 'pip install unoserver' per {self.python}?")
            try:
                xmlrpc.client.ServerProxy(self.url, allow_none=True).info()
                return
            except Exception:
                time.sleep(0.3)
        raise RuntimeError("unoserver non risponde dopo 60 s")

    def chiudi(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        if self.ram:
            shutil.rmtree(self.ram, ignore_errors=True)
            self.ram = None

    def converti(self, data: bytes, suffix: str, verso: str = "pdf") -> bytes:
        """verso: pdf, docx, pptx, xlsx, png..."""
        import xmlrpc.client
        with self.lock:
            try:
                if self.proc is None or self.proc.poll() is not None:
                    self._avvia()
                srv = xmlrpc.client.ServerProxy(self.url, allow_none=True)
                out = srv.convert(None, xmlrpc.client.Binary(data), None, verso)
                return out.data if hasattr(out, "data") else bytes(out)
            except Exception as e:
                self.ultimo_errore = str(e)
                return office_lento(data, suffix, verso)


_ufficio = Ufficio()


def pdf_da_office(data: bytes, suffix: str) -> bytes:
    return _ufficio.converti(data, suffix)


def office_lento(data: bytes, suffix: str, verso: str = "pdf") -> bytes:
    """Ripiego: un soffice nuovo per ogni documento, tutto in una cartella in RAM cancellata subito."""
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if not exe:
        raise Errore("LibreOffice non installato: serve per Word, PowerPoint ed Excel vecchi "
                     "(sudo apt install libreoffice-core libreoffice-writer libreoffice-calc "
                     "libreoffice-impress)")
    with cartella_ram("soffice") as d:
        src = Path(d) / f"documento{suffix}"
        src.write_bytes(data)
        # profilo separato: piu' conversioni in parallelo non si bloccano a vicenda
        prof = Path(d) / "profilo"
        env = dict(os.environ, TMPDIR=d, TMP=d, TEMP=d)
        r = subprocess.run([exe, f"-env:UserInstallation=file://{prof}", "--headless",
                            "--convert-to", verso, "--outdir", d, str(src)],
                           capture_output=True, timeout=300, env=env)
        out = Path(d) / f"documento.{verso}"
        if not out.is_file():
            raise Errore(f"Conversione con LibreOffice non riuscita: {r.stderr.decode(errors='replace')[:300]}")
        return out.read_bytes()


def foglio_da_excel(data: bytes) -> list[str]:
    """Excel moderno: valori delle celle direttamente in tabelle Markdown (esatti)."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    pagine = []
    for ws in wb.worksheets:
        rows = [["" if v is None else str(v).replace("|", "\\|").replace("\n", " ") for v in r]
                for r in ws.iter_rows(values_only=True)]
        rows = [r for r in rows if any(c.strip() for c in r)]
        if not rows:
            continue
        ncol = max(len(r) for r in rows)
        # colonne vuote in fondo
        while ncol > 1 and all(len(r) < ncol or not r[ncol - 1].strip() for r in rows):
            ncol -= 1
        rows = [(r + [""] * ncol)[:ncol] for r in rows]
        md = [f"## {ws.title}", "", "| " + " | ".join(rows[0]) + " |",
              "|" + "---|" * ncol]
        md += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        pagine.append("\n".join(md))
    return pagine


def frame_immagine(data: bytes):
    """Immagine (anche TIFF a piu' pagine) -> lista di PNG."""
    img = Image.open(io.BytesIO(data))
    out = []
    for k in range(getattr(img, "n_frames", 1)):
        img.seek(k)
        out.append(png_bytes(img.convert("RGB")))
    return out


# ---------------------------------------------------------------- documento
def calcola_doc_id(data: bytes) -> str:
    """Stesso contenuto, stesso doc_id: rilanciare un lotto non crea doppioni."""
    return "D2M-" + hashlib.sha256(data).hexdigest()[:16].upper()


def apri_pdf(data_pdf: bytes):
    try:
        pdf = pymupdf.open(stream=data_pdf, filetype="pdf")
    except Exception as e:
        raise Errore(f"PDF non leggibile: {e}")
    if pdf.needs_pass:
        raise Errore("PDF protetto da password")
    return pdf


def pagina_pdf(doc: Documento, page, num: int):
    """Una pagina di PDF -> (markdown, info): nativa o scansione."""
    tipo, det = tipo_pagina(page)
    if doc.opz.ocr:
        tipo, det["forzato"] = "scansione", True
    try:
        if tipo == "nativa":
            md, metodo, voti = pagina_nativa(doc, page, num)
        else:
            md, voti = pagina_scansione(doc, render(page, doc.cfg.dpi), num, page)
            metodo = "ocr"
    except Errore:
        raise
    except Exception as e:      # una pagina rotta non deve perdere tutto il documento
        doc.avvisi.append(f"p{num}: pagina non convertita ({type(e).__name__}: {e})")
        md, metodo, voti = doc.figura(render(page, doc.cfg.dpi), num, "Pagina non convertita"), "errore", []
    return md, {"metodo": metodo, "voti": voti, **det}


def pagine_pdf(doc: Documento, data_pdf: bytes):
    pdf = apri_pdf(data_pdf)
    doc.testate_pdf = frozenset(testate_ripetute(pdf))
    for num, page in enumerate(pdf, 1):
        md, info = pagina_pdf(doc, page, num)
        doc.pagine.append(md)
        doc.info_pagine.append(info)


def riempi(doc: Documento, data: bytes, nome: str) -> str:
    """Legge il documento e riempie doc.pagine / doc.info_pagine. -> formato riconosciuto."""
    tipo = riconosci_tipo(data, nome)
    # il tipo riconosciuto dal contenuto vale piu' dell'estensione del nome
    suffix = "." + tipo if tipo != "sconosciuto" else Path(nome).suffix.lower()
    formato = tipo if tipo != "sconosciuto" else (suffix.lstrip(".") or "sconosciuto")
    t = suffix.lstrip(".")

    if suffix in TEXT_EXT:
        testo = data.decode("utf-8", errors="replace")
        if suffix == ".csv":
            import csv
            sep = ";" if testo.split("\n", 1)[0].count(";") > testo.split("\n", 1)[0].count(",") else ","
            rows = list(csv.reader(io.StringIO(testo), delimiter=sep))
            if rows:
                testo = "\n".join(["| " + " | ".join(rows[0]) + " |", "|" + "---|" * len(rows[0])] +
                                  ["| " + " | ".join(r) + " |" for r in rows[1:]])
        doc.pagine, doc.info_pagine = [testo], [{"metodo": "testo"}]
    elif t in ("json", "xml", "ipynb"):
        formati.codice(doc, data, t)
    elif suffix in SHEET_EXT or t in FOGLI_VECCHI:
        xlsx = data if suffix in SHEET_EXT else _ufficio.converti(data, suffix, "xlsx")
        doc.pagine = foglio_da_excel(xlsx)
        doc.info_pagine = [{"metodo": "foglio"} for _ in doc.pagine]
    elif t in WORD:
        formati.word(doc, data, t)
    elif t in PRESENTAZIONI:
        formati.presentazione(doc, data, t)
    elif t == "html":
        formati.html(doc, data)
    elif t == "epub":
        formati.epub(doc, data)
    elif t in ARCHIVI:
        formati.archivio(doc, data, t)
    elif t in POSTA:
        formati.posta(doc, data, t)
    elif t in AUDIO or t in VIDEO:
        media.trascrivi(doc, data, t)
    elif t == "svg":            # vettoriale: come una pagina PDF nativa (testo e schemi)
        svg = pymupdf.open(stream=data, filetype="svg")
        pagine_pdf(doc, svg.convert_to_pdf())
    elif suffix in IMG_EXT:
        for k, png in enumerate(frame_immagine(data), 1):
            md, voti = pagina_scansione(doc, png, k)
            doc.pagine.append(md)
            doc.info_pagine.append({"metodo": "ocr", "voti": voti})
    elif suffix == ".pdf" or data[:5] == b"%PDF-":
        pagine_pdf(doc, data)
    else:
        raise Errore(f"Formato non gestito: {suffix or 'sconosciuto'}")
    return formato


def controlla_doc_id(doc_id: str):
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,255}", doc_id):
        raise Errore("doc_id non valido: solo lettere, cifre, punto, trattino e trattino basso (max 255)")


def converti(data: bytes, nome: str, opz: Opzioni | None = None, cfg: Config | None = None):
    """-> (markdown, rapporto)"""
    t0 = time.time()
    opz = opz or Opzioni()
    cfg = cfg or Config()
    doc_id = opz.doc_id or calcola_doc_id(data)
    controlla_doc_id(doc_id)
    doc = Documento(cfg, opz, doc_id)
    formato = riempi(doc, data, nome)
    return componi(doc, nome, formato, time.time() - t0)


def converti_url(url: str, opz: Opzioni | None = None, cfg: Config | None = None):
    """Pagina web, video (YouTube e altri siti) o documento raggiungibile da un link.
    Si legge soltanto: nessun dato del chiamante va in rete. -> (markdown, rapporto)"""
    t0 = time.time()
    opz = opz or Opzioni()
    cfg = cfg or Config()
    if not re.match(r"https?://", url, re.I):
        raise Errore("il link deve cominciare con http:// o https://")
    doc_id = opz.doc_id or web.doc_id_url(url)
    controlla_doc_id(doc_id)
    doc = Documento(cfg, opz, doc_id)
    formato = web.leggi_url(doc, url)
    return componi(doc, url, formato, time.time() - t0)


def componi(doc: Documento, nome: str, formato: str, secondi: float):
    did = doc.doc_id
    metodi = {}
    for i in doc.info_pagine:
        metodi[i["metodo"]] = metodi.get(i["metodo"], 0) + 1
    fm = [
        "---",
        f'doc_id: "{did}"',
        f"fonte: {json.dumps(nome, ensure_ascii=False)}",
        f"formato: {formato}",
        f"pagine: {len(doc.pagine)}",
        f"convertito: {datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"convertitore: doc2md {VERSIONE}",
        "metodo: {" + ", ".join(f"{k}: {v}" for k, v in metodi.items()) + "}",
        f"immagini: {doc.opz.immagini}",
    ]
    for k, v in doc.meta.items():            # titolo, autore, durata, link... (pagine web, video, posta)
        if v not in (None, ""):
            fm.append(f"{k}: {json.dumps(v, ensure_ascii=False) if isinstance(v, str) else v}")
    if doc.membri:
        fm.append(f"contenuti: {len(doc.membri)}   # file dell'archivio o allegati, uno per sezione")
    if sum(doc.schemi.values()):
        fm.append(f"schemi: {{mermaid: {doc.schemi['mermaid']}, immagine: {doc.schemi['ripiego']}}}")
    if doc.opz.jev:
        if doc.jev:
            sotto = sum(v < JEV_SOGLIA for v in doc.jev)
            fm += [f"jev: {statistics.mean(doc.jev):.3f}",
                   f"jev_minimo: {min(doc.jev):.3f}",
                   f"jev_regioni: {len(doc.jev)}",
                   f"jev_da_verificare: {sotto}"]
        else:
            fm.append("jev: null   # nessuna regione letta da modello, o servizio non raggiungibile")
        if sum(doc.tabelle.values()):
            fm.append(f"tabelle_grandi: {{coerenti: {doc.tabelle[True]}, da_verificare: {doc.tabelle[False]}}}")
    fm.append("---")
    parti = ["\n".join(fm), "", f"<!-- DOCID:{did} -->", f"**doc_id:** `{did}`", ""]
    for num, (md, info) in enumerate(zip(doc.pagine, doc.info_pagine), 1):
        tag = f"<!-- DOCID:{did} p:{num} metodo:{info['metodo']}"
        voti = info.get("voti") or []
        if voti:
            tag += f" jev:{statistics.mean(voti):.3f}"
        parti += [tag + " -->", "", md.strip(), ""]
    if doc.immagini and doc.opz.immagini == "separate":
        # immagini fuori dal testo: il Markdown punta a img/<impronta>.<est> (lo stesso file per
        # immagini identiche, anche in documenti diversi); i dati vanno al chiamante
        parti += [f"<!-- IMMAGINI DOCID:{did} -->", ""]
        parti += [f"[{rif}]: img/{_file_img(mime, b)}" for rif, mime, b in doc.immagini]
        parti.append("")
    elif doc.immagini:
        parti += [f"<!-- IMMAGINI DOCID:{did} -->", ""]
        parti += [f"[{rif}]: data:{mime};base64,{b}" for rif, mime, b in doc.immagini]
        parti.append("")
    rapporto = {"doc_id": did, "fonte": nome, "formato": formato, "tipo": formato, "pagine": len(doc.pagine),
                "metodo": metodi, "schemi": doc.schemi, "immagini": len(doc.immagini),
                "secondi": round(secondi, 1), "avvisi": doc.avvisi, **doc.meta,
                **({"membri": doc.membri} if doc.membri else {}),
                **({"segmenti": doc.segmenti} if doc.segmenti else {}),
                "pagine_dettaglio": [{k: v for k, v in i.items() if k != "voti"} |
                                     ({"jev": round(statistics.mean(i["voti"]), 3)} if i.get("voti") else {})
                                     for i in doc.info_pagine]}
    if doc.opz.immagini == "separate":
        uniche = {}
        for rif, mime, b in doc.immagini:            # una voce per immagine diversa
            f = _file_img(mime, b)
            uniche.setdefault(f, {"hash": f.split(".")[0], "file": f, "mime": mime, "dati": b})
        rapporto["_immagini"] = list(uniche.values())
        rapporto["immagini_diverse"] = len(uniche)
    if doc.opz.jev:
        rapporto["jev"] = round(statistics.mean(doc.jev), 3) if doc.jev else None
        rapporto["jev_regioni"] = len(doc.jev)
        rapporto["tabelle_grandi"] = {"coerenti": doc.tabelle[True], "da_verificare": doc.tabelle[False]}
    return "\n".join(parti), rapporto


def _jev_acceso(cfg: Config) -> bool | None:
    """jev-servizio gia' acceso in llama-swap? (GET /running non avvia nulla). None: non si sa."""
    import requests
    base = cfg.llm_url.rstrip("/").removesuffix("/v1")
    try:
        r = requests.get(base + "/running", timeout=5)
        nomi = {m.get("model") for m in r.json().get("running", [])}
    except Exception:
        return None
    nome = cfg.jev_url.rstrip("/").rsplit("/", 1)[-1]
    return nome in nomi


def stato(cfg: Config | None = None, jev: bool = False) -> dict:
    """Controllo dei servizi usati (per /v1/stato e --stato).
    jev=False: Jev si controlla solo se e' gia' acceso (accenderlo carica il modello da 2B e il
    servizio di render, e puo' richiedere un paio di minuti); jev=True: lo accende e aspetta."""
    import requests
    cfg = cfg or Config()
    out = {"configurazione": config.riepilogo()}
    try:
        r = requests.get(cfg.llm_url.rstrip("/") + "/models", timeout=5)
        ids = {m.get("id") for m in r.json().get("data", [])}
        out["llama_swap"] = {m: (m in ids) for m in set(cfg.lettori.values())}
    except Exception as e:
        out["llama_swap"] = f"non raggiungibile: {e}"
    acceso = _jev_acceso(cfg)
    if jev or acceso:
        t0 = time.time()
        ok, info = jev_health(cfg.jev_url, timeout=300 if jev else 10)
        out["jev"] = f"pronto ({time.time() - t0:.0f} s)" if ok else f"non pronto: {info[:160]}"
    else:
        out["jev"] = ("spento: si accende da solo con --jev (per provarlo: doc2md.py --stato --jev, "
                      "fino a qualche minuto la prima volta)")
    out["divisore"] = str(cfg.layout_dir) if (cfg.layout_dir / "pp_doclayoutv3.onnx").is_file() \
        else f"modello mancante in {cfg.layout_dir}"
    out["libreoffice"] = bool(shutil.which("soffice") or shutil.which("libreoffice"))
    r = subprocess.run([_ufficio.python, "-c", "import uno, unoserver"], capture_output=True)
    out["unoserver"] = "pronto (LibreOffice resta acceso con doc2md)" if r.returncode == 0 else \
        f"assente: un soffice per documento (pip install unoserver per {_ufficio.python})"
    # audio, video, link e formati strutturati
    try:
        r = requests.get(cfg.llm_url.rstrip("/") + "/models", timeout=5)
        ids = {m.get("id") for m in r.json().get("data", [])}
        simili = sorted(i for i in ids if i and "whisper" in i.lower())
        out["whisper"] = "in llama-swap" if cfg.whisper in ids else (
            f"voce '{cfg.whisper}' non tra i modelli di llama-swap: config.yaml ricaricato "
            f"(systemctl --user restart llama-swap)? 'unlisted: true' la nasconde da questo controllo"
            + (f"; voci simili: {simili} (nome diverso: DOC2MD_WHISPER=<nome>)" if simili else ""))
    except Exception:
        out["whisper"] = "llama-swap non raggiungibile"
    out["ffmpeg"] = bool(shutil.which("ffmpeg"))
    out["file_temporanei"] = f"in RAM ({RAM})" if RAM else f"su disco ({tempfile.gettempdir()}): manca /dev/shm"
    import importlib.util as iu
    for mod, pip in (("markitdown", "'markitdown[docx,pptx]==0.1.9'"), ("trafilatura", "trafilatura"),
                     ("yt_dlp", "'yt-dlp[default]'"), ("extract_msg", "extract-msg")):
        out[mod] = "pronto" if iu.find_spec(mod) else f"manca: pip install {pip}"
    js = shutil.which("deno") or shutil.which("node")
    out["youtube_js"] = js or "manca un motore JavaScript (deno o node) per YouTube"
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", help="file oppure link (pagina web, video, documento)")
    ap.add_argument("-o", "--out", help="file .md (predefinito: cartella output di doc2md.ini)")
    ap.add_argument("--immagini", choices=["embedded", "separate", "none"],
                    default=config.valore("conversione", "immagini"))
    ap.add_argument("--jev", action="store_true", help="voto di Jev sulle regioni lette dai modelli")
    ap.add_argument("--doc-id")
    ap.add_argument("--testate", action="store_true", help="tiene intestazioni e numeri di pagina")
    ap.add_argument("--ocr", action="store_true",
                    help="legge con i modelli anche le pagine native (per provare lettori e Jev)")
    ap.add_argument("--lingua", default=config.valore("conversione", "lingua") or None,
                    help="trascrizioni: it, en... (predefinito: doc2md.ini, altrimenti riconosciuta)")
    ap.add_argument("--contesto", help="trascrizioni: nomi e termini da scrivere giusti")
    ap.add_argument("--segmenti", action="store_true",
                    help="trascrizioni: scrive accanto al .md un .segmenti.json con inizio, fine e testo")
    ap.add_argument("--stato", action="store_true", help="controlla i servizi ed esce (con --jev accende anche Jev e aspetta)")
    a = ap.parse_args()
    if a.stato or not a.file:
        print(json.dumps(stato(jev=a.jev), indent=1, ensure_ascii=False))
        return
    link = re.match(r"https?://", a.file, re.I)
    src = Path(re.sub(r"[^\w.-]+", "_", a.file.rstrip("/").rsplit("/", 1)[-1])[:80]) if link \
        else config.trova(a.file)                       # senza percorso: anche nella cartella input
    if not link and not src.is_file():
        raise SystemExit(f"File non trovato: {a.file} (ne' qui ne' in {config.cartella('input')})")
    opz = Opzioni(immagini=a.immagini, jev=a.jev, doc_id=a.doc_id, testate=a.testate, ocr=a.ocr,
                  nome=a.file if link else src.name, lingua=a.lingua, contesto=a.contesto, segmenti=a.segmenti)
    # un file della cartella input (senza -o): nel suo lotto, come lotto.py file (vedi lotto.lotto_di)
    nel_lotto = not link and not a.out and not a.doc_id and \
        src.resolve().is_relative_to(config.cartella("input").expanduser().resolve())
    if nel_lotto:
        import lotto
        opz.doc_id = lotto.doc_id_di_file(src, lotto.lotto_di(src)[0])
    t0 = time.time()
    try:
        data = None if link else src.read_bytes()
        md, rap = converti_url(a.file, opz) if link else converti(data, src.name, opz)
    except Errore as e:
        raise SystemExit(f"Errore: {e}")
    if nel_lotto:
        res = {"doc_id": rap["doc_id"], "markdown": md, "rapporto": rap, "immagini": rap.pop("_immagini", [])}
        out = lotto.registra_file(src, res, data, round(time.time() - t0, 2))
        print(json.dumps(rap, indent=1, ensure_ascii=False))
        print(f"Scritto {out} (registrato in {lotto.lotto_di(src)[1] / 'lotto.db'})")
        return
    out = Path(a.out) if a.out else OUTPUT / f"{src.name}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    if rap.get("segmenti"):
        out.with_suffix(".segmenti.json").write_text(json.dumps(rap["segmenti"], ensure_ascii=False, indent=0),
                                                     encoding="utf-8")
    for im in rap.pop("_immagini", []):
        f = out.parent / "img" / im["file"]
        f.parent.mkdir(exist_ok=True)
        if not f.exists():                             # immagine gia' salvata da un altro documento
            f.write_bytes(base64.b64decode(im["dati"]))
    print(json.dumps(rap, indent=1, ensure_ascii=False))
    print(f"Scritto {out}")


if __name__ == "__main__":
    main()
