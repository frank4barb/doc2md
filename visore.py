#!/usr/bin/env python3
"""visore.py - confronto visivo delle conversioni di doc2md, nel browser.

A sinistra il documento originale (pagine PDF, immagini, audio e video; Word e simili
convertiti in PDF per l'anteprima), a destra il Markdown prodotto, con immagini, formule
(KaTeX) e schemi (Mermaid) disegnati. Si naviga nelle cartelle input e output di doc2md.ini,
si interrogano i file SQLite dei lotti (sola lettura) per trovare e confrontare i documenti
convertiti male, e si avviano e fermano le conversioni (lotto.py db / cartella).

Uso:
  python visore.py                       # http://127.0.0.1:8095 (host e porta da doc2md.ini)
  python visore.py --scarica-librerie    # una volta: marked, KaTeX e Mermaid in static/vendor,
                                         # cosi' il browser non le chiede a Internet
  ./visore.sh                            # lo avvia in sottofondo e apre il browser (stop: ./visore.sh stop)
Da un altro PC: ssh -L 8095:127.0.0.1:8095 utente@<computer di doc2md>, poi http://127.0.0.1:8095

Sicurezza: ascolta solo su 127.0.0.1 (salvo diversa impostazione), mostra solo file dentro
le cartelle configurate, apre i database in sola lettura e accetta solo SELECT. Le conversioni
le fa lotto.py (l'unico che scrive nei lotti), una alla volta; i comandi di avvio e arresto
richiedono un'intestazione che solo la pagina del visore manda (niente richieste da altri siti).
Le conversioni si fermano con il visore: finiscono il documento in corso e si rilanciano dal visore
o dal terminale ripartendo da dove erano.
"""
import argparse
import contextlib
import functools
import io
import mimetypes
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tarfile
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

QUI = Path(__file__).resolve().parent
sys.path.insert(0, str(QUI))
import config  # noqa: E402

STATIC = QUI / "static"
VENDOR = STATIC / "vendor"
# versioni fissate: l'interfaccia della pagina e' scritta per queste
LIBRERIE = {
    "marked": ("12.0.2", {"package/marked.min.js": "marked.min.js"}),
    "katex": ("0.16.11", {"package/dist/katex.min.js": "katex.min.js",
                          "package/dist/katex.min.css": "katex.min.css",
                          "package/dist/fonts/": "fonts/"}),
    "mermaid": ("10.9.1", {"package/dist/mermaid.min.js": "mermaid.min.js"}),
}
CDN = {"marked.min.js": "https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js",
       "katex.min.js": "https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js",
       "katex.min.css": "https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css",
       "mermaid.min.js": "https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js"}

RADICI = [p.expanduser().resolve() for p in config.cartelle_visore()]
OUTPUT = config.cartella("output").expanduser().resolve()
INPUT = config.cartella("input").expanduser().resolve()
MAX_RIGHE = 1000
TEMPO_QUERY = 15          # secondi massimi per una query

PRESET = [
    ("Peggiori per testo atteso (verifica)",
     "SELECT r.chiave, r.tipo, r.pagine, round(r.verifica * 100, 1) AS verifica_pct, r.mancanti\n"
     "FROM risultati r JOIN documenti d ON d.chiave = r.chiave\n"
     "WHERE r.verifica IS NOT NULL AND parole(d.atteso) >= 15\nORDER BY r.verifica LIMIT 200"),
    ("Peggiori per completezza rispetto al PDF",
     "SELECT chiave, tipo, pagine, round(completezza * 100, 1) AS completezza_pct, mancanti_pdf\n"
     "FROM risultati WHERE completezza IS NOT NULL\nORDER BY completezza LIMIT 200"),
    ("Errori di conversione",
     "SELECT chiave, tipo, errore, convertito FROM risultati WHERE stato = 'errore'"),
    ("Documenti con pagine lette dai modelli (OCR)",
     "SELECT chiave, tipo, pagine, metodo FROM risultati\nWHERE metodo LIKE '%ocr%' ORDER BY chiave LIMIT 500"),
    ("Documenti con schemi Mermaid",
     "SELECT chiave, tipo, pagine FROM risultati\nWHERE markdown LIKE '%```mermaid%' LIMIT 500"),
    ("Documenti con formule",
     "SELECT chiave, tipo, pagine FROM risultati\nWHERE markdown LIKE '%$$%' LIMIT 500"),
    ("Punti da riascoltare o ricontrollare (whisper, Jev)",
     "SELECT chiave, tipo, jev, jev_da_verificare FROM risultati\n"
     "WHERE markdown LIKE '%<!-- verifica:%' LIMIT 500"),
    ("Testo atteso non valido (segnaposto)",
     "SELECT d.chiave, substr(d.atteso, 1, 80) AS atteso FROM documenti d\n"
     "WHERE d.atteso IS NOT NULL AND parole(d.atteso) < 15 LIMIT 500"),
    ("Riepilogo per tipo",
     "SELECT tipo, COUNT(*) AS documenti, SUM(pagine) AS pagine,\n"
     "       round(AVG(verifica) * 100, 1) AS verifica_media, round(AVG(completezza) * 100, 1) AS completezza_media\n"
     "FROM risultati WHERE stato = 'fatto' GROUP BY tipo ORDER BY documenti DESC"),
]


# ---------------------------------------------------------------- librerie del browser
def scarica_librerie():
    """Scarica marked, KaTeX e Mermaid dal registro npm in static/vendor (una volta)."""
    import requests
    VENDOR.mkdir(parents=True, exist_ok=True)
    for nome, (versione, file) in LIBRERIE.items():
        url = f"https://registry.npmjs.org/{nome}/-/{nome}-{versione}.tgz"
        print(f"scarico {nome} {versione} ...", flush=True)
        r = requests.get(url, timeout=120)
        r.raise_for_status()
        with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as t:
            for m in t.getmembers():
                for origine, dest in file.items():
                    if m.isfile() and (m.name == origine or (origine.endswith("/") and m.name.startswith(origine))):
                        f = VENDOR / (dest + m.name[len(origine):] if origine.endswith("/") else dest)
                        f.parent.mkdir(parents=True, exist_ok=True)
                        f.write_bytes(t.extractfile(m).read())
    print(f"librerie in {VENDOR}")


def url_libreria(nome: str) -> str:
    return f"/static/vendor/{nome}" if (VENDOR / nome).is_file() else CDN[nome]


# ---------------------------------------------------------------- percorsi
def dentro(p) -> Path:
    """Solo file dentro le cartelle configurate (niente ../ verso il resto del disco)."""
    q = Path(p).expanduser().resolve()
    for r in RADICI:
        if q == r or q.is_relative_to(r):
            return q
    from fastapi import HTTPException
    raise HTTPException(403, f"fuori dalle cartelle del visore: {q}")


def lotto_sopra(p: Path) -> Path | None:
    """Il lotto.db piu' vicino risalendo da p (modo cartella)."""
    for a in [p] + list(p.parents):
        if (a / "lotto.db").is_file():
            return a / "lotto.db"
        if a in RADICI:
            break
    return None


def link_di_file(p: Path) -> str | None:
    """Il link di una scorciatoia .url / .webloc / .desktop."""
    import lotto
    if p.suffix.lower() in lotto.FILE_LINK and p.is_file():
        ln = lotto.link_di(p)
        return ln[0].url if ln else None
    return None


def origine_di_md(md: Path) -> dict:
    """Da dove viene un .md: {"sorgente": file d'origine} oppure, per un link di una lista .links,
    {"link": url, "lista": file .links}."""
    db = lotto_sopra(md.parent)
    if db:
        try:
            with ro(db) as con:
                r = con.execute("SELECT valore FROM info WHERE chiave = 'sorgente'").fetchone()
                rel = md.relative_to(db.parent).as_posix()[:-3]
                u = None
                if "url" in {c[1] for c in con.execute("PRAGMA table_info(documenti)")}:
                    u = con.execute("SELECT url FROM documenti WHERE chiave = ?", (rel,)).fetchone()
            if r:
                cand = Path(r[0]) / rel
                if cand.is_file():
                    return {"sorgente": cand, "link": link_di_file(cand)}
                if u and u[0]:
                    lista = Path(r[0]) / rel.rsplit("/", 1)[0]
                    return {"link": u[0], "lista": lista if lista.is_file() else None}
        except (sqlite3.Error, ValueError):
            pass
    return {"sorgente": sorgente_di_md(md, senza_lotto=True)}


def lista_link(p: Path) -> list[dict]:
    """Le righe di una lista .links con il loro .md e l'esito nel lotto."""
    import lotto
    try:
        r = config.radice_lotto(p)
    except ValueError:
        r = None
    src, out = (r[0], r[1].expanduser().resolve()) if r else (p.parent, OUTPUT / p.parent.name)
    voci = lotto.voci_di_lista(p, p.relative_to(src).as_posix())
    esiti = {}
    if (out / "lotto.db").is_file():
        try:
            with ro(out / "lotto.db") as con:
                esiti = {k: (st, err) for k, st, err in con.execute(
                    "SELECT chiave, stato, errore FROM risultati WHERE chiave LIKE ? ESCAPE '\\'",
                    (p.relative_to(src).as_posix().replace("%", "\\%").replace("_", "\\_") + "/%",))}
        except sqlite3.Error:
            pass
    righe = []
    for chiave, ln in voci:
        md = out / f"{chiave}.md"
        st, err = esiti.get(chiave, (None, None))
        righe.append({"nome": chiave.rsplit("/", 1)[-1], "url": ln.url, "contesto": ln.contesto,
                      "md": str(md) if md.is_file() else None, "stato": st, "errore": err})
    return righe


def sorgente_di_md(md: Path, senza_lotto: bool = False) -> Path | None:
    if not senza_lotto:
        return origine_di_md(md).get("sorgente")
    nome = md.name[:-3]                      # documento.pdf.md -> documento.pdf (d2m.py, doc2md.py)
    for radice in RADICI:
        for i, c in enumerate(radice.rglob(nome)):
            if c.is_file() and not c.is_relative_to(OUTPUT):
                return c
            if i > 50:
                break
    return None


def md_di_sorgente(src: Path) -> Path | None:
    """Il .md di un documento: dentro input il posto e' uno solo (l'uscita ricalca input, vedi
    config.radice_lotto); per conversioni fatte prima di questa regola, i posti di allora."""
    try:
        r = config.radice_lotto(src)
    except ValueError:
        r = None
    if r:
        atteso = r[1] / f"{src.relative_to(r[0]).as_posix()}.md"
        if atteso.is_file():
            return atteso
    vecchi = [OUTPUT / a.name / f"{src.relative_to(a).as_posix()}.md" for a in src.parents
              if a.is_relative_to(INPUT) and a != INPUT] + [OUTPUT / f"{src.name}.md"]
    return next((c for c in vecchi if c.is_file()), None)


# ---------------------------------------------------------------- sorgenti (file o BLOB)
@contextlib.contextmanager
def ro(db: Path):
    """Connessione in sola lettura, chiusa all'uscita dal "with" (lo "with" di sqlite3 da solo non la
    chiude: le connessioni rimaste aperte impedirebbero a lotto.py di richiudere il file a fine lavoro)."""
    con = sqlite3.connect(f"file:{quote(str(db))}?mode=ro", uri=True, check_same_thread=False, timeout=10)
    try:
        con.execute("PRAGMA query_only = ON")
        con.create_function("parole", 1, lambda t: len(re.findall(r"\w{2,}", t)) if isinstance(t, str) else 0)
        yield con
    finally:
        con.close()


@functools.lru_cache(maxsize=16)
def _dati(file: str | None, db: str | None, chiave: str | None) -> tuple[bytes, str]:
    if file:
        p = Path(file)
        return p.read_bytes(), p.name
    with ro(Path(db)) as con:
        r = con.execute("SELECT nome, contenuto FROM documenti WHERE chiave = ?", (chiave,)).fetchone()
        if r and r[1]:
            return bytes(r[1]), r[0] or chiave
        s = con.execute("SELECT valore FROM info WHERE chiave = 'sorgente'").fetchone()
    if s and (Path(s[0]) / chiave).is_file():          # modo cartella: il file e' nell'albero d'origine
        p = Path(s[0]) / chiave
        return p.read_bytes(), p.name
    from fastapi import HTTPException
    raise HTTPException(404, "documento originale non disponibile")


def dati(file=None, db=None, chiave=None):
    if file:
        file = str(dentro(file))
    if db:
        db = str(dentro(db))
    return _dati(file, db, chiave)


@functools.lru_cache(maxsize=1)
def _d2m():
    import doc2md                                     # pesante: solo quando serve
    return doc2md


@functools.lru_cache(maxsize=16)
def _pdf_anteprima(file, db, chiave) -> tuple[str, bytes | None]:
    """(tipo, PDF per l'anteprima a pagine o None)."""
    data, nome = _dati(file, db, chiave)
    D = _d2m()
    tipo = D.riconosci_tipo(data, nome)
    if tipo == "pdf":
        return tipo, data
    if tipo in ("doc", "docx", "odt", "rtf", "ppt", "pptx", "odp", "xls", "xlsx", "xlsm", "ods", "html"):
        try:
            return tipo, D.pdf_da_office(data, "." + tipo)   # LibreOffice, in RAM
        except Exception:
            return tipo, None
    if tipo == "svg":
        import pymupdf
        return tipo, pymupdf.open(stream=data, filetype="svg").convert_to_pdf()
    return tipo, None


# ---------------------------------------------------------------- Markdown
RIF = re.compile(r"^(\[[^\]]+\]:\s*)(\S+)\s*$", re.M)


def md_per_browser(md: str, base: Path | None = None, db: Path | None = None) -> str:
    """I riferimenti alle immagini diventano indirizzi del visore."""
    def sost(m):
        dest = m.group(2)
        if dest.startswith(("data:", "http://", "https://")):
            return m.group(0)
        h = re.match(r"(?:.*/)?img/([0-9a-f]{32})\.\w+$", dest)
        if db is not None and h:
            return f"{m.group(1)}/api/db/img?db={quote(str(db))}&hash={h[1]}"
        if base is not None:
            p = (base / dest).resolve()
            return f"{m.group(1)}/api/file?percorso={quote(str(p))}"
        return m.group(0)
    return RIF.sub(sost, md)

# ---------------------------------------------------------------- conversioni (lotto.py)
LOTTO = QUI / "lotto.py"
AVANZAMENTO = re.compile(r"\[(\d+)/(\d+)\]\s*(.*)")
CODA_LOG = 400                     # righe del registro mostrate nella pagina


def piano(percorso: str) -> dict:
    """Cosa fara' la conversione (tutta con lotto.py): lotto .db, cartella o file solo.
    Restituisce modo, lotti coinvolti (file di stato e prefisso delle chiavi), registro e uscita.
    Dentro input l'uscita ricalca input (config.radice_lotto): input/referti/2025 -> output/referti/2025,
    con output/referti/lotto.db e output/referti/img; i file in input -> output, output/lotto.db."""
    from fastapi import HTTPException
    p = dentro(percorso)
    if p.is_file() and p.suffix.lower() in (".db", ".sqlite"):
        return {"modo": "db", "sorgente": p, "lotti": [(p, None)], "log": OUTPUT / f"{p.stem}.log", "uscita": p}
    if p == OUTPUT or p.is_relative_to(OUTPUT):
        raise HTTPException(422, "e' nella cartella di output: scegli i documenti d'origine")
    if not p.exists():
        raise HTTPException(404, "non trovato")
    import lotto
    modo = "cartella" if p.is_dir() else "lista" if p.suffix.lower() == lotto.LISTA_LINK else "file"
    if p == INPUT:                       # tutta input: un lotto per ogni cartella di primo livello, piu' output
        lotti = [(OUTPUT / "lotto.db", "")] + [(OUTPUT / d.name / "lotto.db", "") for d in sorted(INPUT.iterdir())
                                               if d.is_dir() and not d.name.startswith(".")]
        return {"modo": modo, "sorgente": p, "lotti": lotti, "log": OUTPUT / "lotto.log", "uscita": OUTPUT}
    try:
        r = config.radice_lotto(p)
    except ValueError as e:
        raise HTTPException(422, str(e))
    src, out = (r[0], r[1].expanduser().resolve()) if r else ((p if p.is_dir() else p.parent), OUTPUT / (p if p.is_dir() else p.parent).name)
    rel = p.relative_to(src).as_posix() if p != src else ""
    uscita = out / f"{rel}.md" if modo == "file" else (out / rel if rel else out)    # lista: <uscita>/<lista>.links/
    return {"modo": modo, "sorgente": p, "lotti": [(out / "lotto.db", rel)], "log": out / "lotto.log",
            "uscita": uscita, "lotto": out}


def situazione(lotti: list, modo: str = "cartella") -> dict | None:
    """Dal file (o dai file) di stato, in sola lettura: documenti, fatti, errori e da fare, solo per le
    chiavi sotto il prefisso (una sottocartella del lotto); per un file solo, il suo esito."""
    tot, st, file_solo = 0, {}, None
    for db, prefisso in lotti:
        if not db.is_file():
            continue
        if prefisso is None:
            dove, arg = "1", ()
        elif modo == "file":
            dove, arg = "chiave = ?", (prefisso,)
        else:
            esc = prefisso.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            dove, arg = "chiave LIKE ? ESCAPE '\\'", ((esc + "/%") if esc else "%",)
        try:
            with ro(db) as con:
                tot += con.execute(f"SELECT COUNT(*) FROM documenti WHERE {dove}", arg).fetchone()[0]
                for k, n in con.execute(f"SELECT stato, COUNT(*) FROM risultati WHERE {dove} GROUP BY stato", arg):
                    st[k] = st.get(k, 0) + n
                if modo == "file":
                    file_solo = con.execute("SELECT stato, convertito, errore FROM risultati WHERE chiave = ?",
                                            arg).fetchone()
        except sqlite3.Error as e:
            return {"errore": str(e)}
    if modo == "file":
        if not file_solo:
            return None
        try:
            quando = datetime.fromisoformat(file_solo[1]).strftime("%d/%m/%Y %H:%M")
        except (TypeError, ValueError):
            quando = file_solo[1] or "?"
        return {"file": True, "stato": file_solo[0], "convertito": quando, "errore_doc": file_solo[2]}
    if not tot:
        return None
    fatti, errori = st.get("fatto", 0), st.get("errore", 0)
    return {"documenti": tot, "fatti": fatti, "errori": errori, "da_fare": max(tot - fatti - errori, 0)}


def situazione_di(pn_modo: str, sorgente: Path, lotti: list) -> dict | None:
    """Come situazione(), ma per una lista .links conta le righe della lista (anche i link appena
    aggiunti, che nel lotto entrano solo alla conversione)."""
    if pn_modo != "lista":
        return situazione(lotti, pn_modo)
    voci = lista_link(sorgente)
    if not voci:
        return None
    fatti = sum(v["stato"] == "fatto" for v in voci)
    errori = sum(v["stato"] == "errore" for v in voci)
    return {"documenti": len(voci), "fatti": fatti, "errori": errori, "da_fare": len(voci) - fatti - errori}


def lotti_in_corso() -> list[dict]:
    """Processi "lotto.py db|cartella" in esecuzione, anche avviati dal terminale."""
    out = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            arg = [a.decode(errors="replace") for a in (d / "cmdline").read_bytes().split(b"\0") if a]
        except OSError:
            continue
        i = next((i for i, a in enumerate(arg) if a.endswith("lotto.py")), None)
        if i is not None and len(arg) > i + 2 and arg[i + 1] in ("db", "cartella", "file"):
            out.append({"pid": int(d.name), "modo": arg[i + 1], "percorso": arg[i + 2]})
    return out


def _figlio():
    """Nel processo di lotto.py: se il visore si chiude (anche Ctrl+C o terminale chiuso),
    lotto.py riceve SIGINT, cioe' finisce il documento in corso e si ferma in ordine."""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").prctl(1, signal.SIGINT)      # PR_SET_PDEATHSIG
    except Exception:
        pass


class Conversione:
    """Una conversione alla volta (il servizio doc2md ne fa comunque una per volta)."""

    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.info: dict = {}

    def attiva(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def avvia(self, percorso: str, opz: dict) -> dict:
        """Da chiamare nel thread principale (endpoint async): PR_SET_PDEATHSIG segue il thread
        che ha creato il processo, e i thread di lavoro di FastAPI si chiudono quando sono inattivi."""
        from fastapi import HTTPException
        if self.attiva():
            raise HTTPException(409, "c'e' gia' una conversione in corso: fermala prima")
        pn = piano(percorso)
        altri = lotti_in_corso() if pn["modo"] != "file" else []    # un file solo puo' passare tra un documento e l'altro del lotto
        if altri:
            raise HTTPException(409, "lotto.py e' gia' in esecuzione (pid " +
                                ", ".join(str(a["pid"]) for a in altri) + "): una conversione alla volta")
        cmd = [sys.executable, "-u", str(LOTTO), "file" if pn["modo"] == "lista" else pn["modo"], str(pn["sorgente"])]
        for chiave, opzione in (("riprova_errori", "--riprova-errori"), ("jev", "--jev"), ("ocr", "--ocr")):
            if chiave == "riprova_errori" and pn["modo"] == "file":
                continue
            if opz.get(chiave):
                cmd.append(opzione)
        if opz.get("rifai") and pn["modo"] != "file":
            cmd.append("--rifai")
        if pn["modo"] != "file" and str(opz.get("limite") or "").isdigit() and int(opz["limite"]) > 0:
            cmd += ["--limite", str(int(opz["limite"]))]
        if opz.get("immagini") in ("separate", "embedded", "none"):
            cmd += ["--immagini", opz["immagini"]]
        if re.fullmatch(r"[a-z]{2,3}", str(opz.get("lingua") or "")):
            cmd += ["--lingua", opz["lingua"]]
        log = pn["log"]
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S}  avviato dal visore: "
                    f"{Path(cmd[2]).name} {' '.join(cmd[3:])}\n")
            inizio_log = f.tell()
            self.proc = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                         cwd=QUI, start_new_session=True, preexec_fn=_figlio)
        self.info = {"modo": pn["modo"], "sorgente": str(pn["sorgente"]), "lotti": pn["lotti"],
                     "log": str(log), "uscita": str(pn["uscita"]), "comando": cmd[2:],
                     "inizio": time.time(), "inizio_log": inizio_log, "stop": 0}
        return self.stato()

    def ferma(self, subito: bool = False) -> dict:
        """Prima richiesta: finisce il documento in corso e si ferma. "subito": interrompe anche quello
        (lo rifara' al prossimo avvio; in modo cartella i .md si scrivono al prossimo avvio)."""
        bersagli = [self.proc.pid] if self.attiva() else [a["pid"] for a in lotti_in_corso()]
        if self.attiva():
            self.info["stop"] = 2 if subito else max(self.info.get("stop", 0), 1)
        for pid in bersagli:
            try:
                os.kill(pid, signal.SIGINT)
                if subito:
                    time.sleep(0.3)
                    os.kill(pid, signal.SIGINT)       # il secondo SIGINT di lotto.py: uscita immediata
            except ProcessLookupError:
                pass
        return self.stato()

    def stato(self) -> dict:
        out = {"attiva": self.attiva(), "esterni": [] if self.attiva() else lotti_in_corso()}
        if not self.info:
            return out
        i = self.info
        out.update({k: i[k] for k in ("modo", "sorgente", "log", "uscita", "comando", "stop")})
        out["pid"] = self.proc.pid
        out["codice"] = None if out["attiva"] else self.proc.returncode
        out["secondi"] = round(time.time() - i["inizio"]) if out["attiva"] else i.setdefault(
            "durata", round(time.time() - i["inizio"]))
        righe = []
        try:
            with open(i["log"], "rb") as f:
                f.seek(0, 2)
                f.seek(max(i["inizio_log"], f.tell() - 256 * 1024))
                righe = f.read().decode("utf-8", errors="replace").splitlines()[-CODA_LOG:]
        except OSError:
            pass
        out["righe"] = righe
        ultimo = next((m for r in reversed(righe) if (m := AVANZAMENTO.search(r))), None)
        out["avanzamento"] = {"fatti": int(ultimo[1]), "totale": int(ultimo[2]), "testo": ultimo[3]} if ultimo else None
        out["situazione"] = situazione_di(i["modo"], Path(i["sorgente"]), i["lotti"])
        return out


CONVERSIONE = Conversione()


# ---------------------------------------------------------------- applicazione
def crea_app():
    from fastapi import Body, FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
    from fastapi.staticfiles import StaticFiles

    app = FastAPI(title="visore doc2md")
    STATIC.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", response_class=HTMLResponse)
    def pagina():
        html = (STATIC / "visore.html").read_text(encoding="utf-8")
        for nome in CDN:
            html = html.replace("{{" + nome + "}}", url_libreria(nome))
        return html

    @app.get("/api/radici")
    def radici():
        return {"radici": [{"nome": r.name, "percorso": str(r), "esiste": r.exists()} for r in RADICI],
                "configurazione": config.riepilogo(), "preset": [{"nome": n, "sql": q} for n, q in PRESET],
                "librerie_locali": all((VENDOR / n).is_file() for n in CDN),
                "predefiniti": {"immagini": config.valore("conversione", "immagini"),
                                "lingua": config.valore("conversione", "lingua")}}

    @app.get("/api/elenco")
    def elenco(percorso: str):
        p = dentro(percorso)
        if not p.is_dir():
            raise HTTPException(404, "cartella inesistente")
        voci = []
        for c in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower()))[:5000]:
            if c.name.startswith(".") or c.name == "__pycache__":
                continue
            voci.append({"nome": c.name, "percorso": str(c), "cartella": c.is_dir(),
                         "byte": c.stat().st_size if c.is_file() else None})
        return {"percorso": str(p), "su": str(p.parent) if p not in RADICI else None, "voci": voci}

    @app.get("/api/file")
    def file(percorso: str):
        p = dentro(percorso)
        if not p.is_file():
            raise HTTPException(404, "file inesistente")
        return FileResponse(p, media_type=mimetypes.guess_type(p.name)[0] or "application/octet-stream")

    @app.get("/api/coppia")
    def coppia(percorso: str):
        import lotto
        p = dentro(percorso)
        if p.suffix.lower() == ".md":
            o = origine_di_md(p)
            return {"md": str(p), **{k: (str(v) if isinstance(v, Path) else v) for k, v in o.items()}}
        if p.suffix.lower() == lotto.LISTA_LINK:
            return {"lista": str(p), "voci": lista_link(p)}
        return {"sorgente": str(p), "md": (str(m) if (m := md_di_sorgente(p)) else None), "link": link_di_file(p)}

    @app.get("/api/md")
    def md(percorso: str):
        p = dentro(percorso)
        return {"testo": md_per_browser(p.read_text(encoding="utf-8", errors="replace"), base=p.parent)}

    def _sorg(file, db, chiave):
        return (str(dentro(file)) if file else None, str(dentro(db)) if db else None, chiave)

    @app.get("/api/sorgente/info")
    def sorgente_info(file: str | None = None, db: str | None = None, chiave: str | None = None):
        f, d, k = _sorg(file, db, chiave)
        data, nome = _dati(f, d, k)
        tipo, pdf = _pdf_anteprima(f, d, k)
        pagine = None
        if pdf:
            import pymupdf
            pagine = len(pymupdf.open(stream=pdf, filetype="pdf"))
        mime = mimetypes.guess_type(f"x.{tipo}")[0] or "application/octet-stream"
        return {"nome": nome, "tipo": tipo, "byte": len(data), "pagine": pagine, "mime": mime}

    @app.get("/api/sorgente/pagina")
    def sorgente_pagina(n: int = 1, dpi: int = 110, file: str | None = None, db: str | None = None,
                        chiave: str | None = None):
        import pymupdf
        f, d, k = _sorg(file, db, chiave)
        _, pdf = _pdf_anteprima(f, d, k)
        if not pdf:
            raise HTTPException(404, "nessuna anteprima a pagine")
        doc = pymupdf.open(stream=pdf, filetype="pdf")
        if not 1 <= n <= len(doc):
            raise HTTPException(404, "pagina inesistente")
        png = doc[n - 1].get_pixmap(dpi=max(40, min(dpi, 300))).tobytes("png")
        return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=300"})

    @app.get("/api/sorgente/raw")
    def sorgente_raw(file: str | None = None, db: str | None = None, chiave: str | None = None):
        f, d, k = _sorg(file, db, chiave)
        data, nome = _dati(f, d, k)
        tipo, _ = _pdf_anteprima(f, d, k)
        mime = mimetypes.guess_type(f"x.{tipo}")[0] or "application/octet-stream"
        return Response(data, media_type=mime,
                        headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(nome)}"})

    @app.get("/api/db/documento")
    def db_documento(db: str, chiave: str):
        p = dentro(db)
        with ro(p) as con:
            con.row_factory = sqlite3.Row
            url = ", d.url" if "url" in {c[1] for c in con.execute("PRAGMA table_info(documenti)")} else ""
            r = con.execute(f"""SELECT r.*, d.nome, d.atteso, length(d.contenuto) AS byte_originale{url}
                                FROM risultati r LEFT JOIN documenti d ON d.chiave = r.chiave
                                WHERE r.chiave = ?""", (chiave,)).fetchone()
        if not r:
            raise HTTPException(404, "documento non convertito (nessun risultato)")
        info = {k: r[k] for k in r.keys() if k not in ("markdown", "rapporto")}
        info["atteso"] = (info.get("atteso") or "")[:4000] or None
        return {"info": info, "testo": md_per_browser(r["markdown"] or "", db=p)}

    @app.get("/api/db/img")
    def db_img(db: str, hash: str):
        with ro(dentro(db)) as con:
            r = con.execute("SELECT mime, dati FROM immagini WHERE hash = ?", (hash,)).fetchone()
        if not r:
            raise HTTPException(404, "immagine non trovata")
        return Response(bytes(r[1]), media_type=r[0] or "image/png", headers={"Cache-Control": "max-age=3600"})

    @app.post("/api/db/query")
    def db_query(richiesta: dict = Body(...)):
        p = dentro(richiesta.get("db", ""))
        sql = (richiesta.get("sql") or "").strip().rstrip(";").strip()
        senza_commenti = re.sub(r"--[^\n]*|/\*.*?\*/", " ", sql, flags=re.S).strip()
        if ";" in senza_commenti or not re.match(r"(?is)^(select|with)\b", senza_commenti):
            raise HTTPException(422, "solo una query SELECT (o WITH ... SELECT)")
        t0 = time.time()
        try:
            with ro(p) as con:
                con.set_progress_handler(lambda: 1 if time.time() - t0 > TEMPO_QUERY else 0, 20000)
                cur = con.execute(sql)
                colonne = [c[0] for c in cur.description or []]
                righe = cur.fetchmany(MAX_RIGHE + 1)
        except sqlite3.Error as e:
            raise HTTPException(422, f"SQLite: {e}")

        def cella(v):
            if isinstance(v, (bytes, bytearray)):
                return f"<BLOB {len(v)} byte>"
            if isinstance(v, str) and len(v) > 400:
                return v[:400] + "…"
            return v
        return JSONResponse({"colonne": colonne, "righe": [[cella(v) for v in r] for r in righe[:MAX_RIGHE]],
                             "troncato": len(righe) > MAX_RIGHE, "secondi": round(time.time() - t0, 2)})

    # ------------------------------------------------------------ conversioni
    def dalla_pagina(req: Request):
        """Solo dalla pagina del visore: un altro sito aperto nel browser non puo' aggiungere
        questa intestazione senza il permesso del visore (che non lo da')."""
        if req.headers.get("x-visore") != "1":
            raise HTTPException(403, "richiesta non proveniente dal visore")

    @app.get("/api/conversione")
    def conversione_stato():
        return CONVERSIONE.stato()

    @app.get("/api/conversione/piano")
    def conversione_piano(percorso: str):
        pn = piano(percorso)
        return {"modo": pn["modo"], "uscita": str(pn["uscita"]), "log": str(pn["log"]),
                "lotto": str(pn["lotto"]) if pn.get("lotto") else None,
                "situazione": situazione_di(pn["modo"], pn["sorgente"], pn["lotti"])}

    @app.post("/api/conversione/avvia")
    async def conversione_avvia(req: Request, richiesta: dict = Body(...)):
        dalla_pagina(req)                  # async: il processo nasce nel thread principale (vedi avvia)
        return CONVERSIONE.avvia(str(richiesta.get("percorso") or ""), richiesta.get("opzioni") or {})

    @app.post("/api/conversione/ferma")
    async def conversione_ferma(req: Request, richiesta: dict = Body(default={})):
        dalla_pagina(req)
        return CONVERSIONE.ferma(bool(richiesta.get("subito")))

    return app


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scarica-librerie", action="store_true", help="scarica marked, KaTeX e Mermaid ed esce")
    ap.add_argument("--host", default=config.valore("visore", "host"))
    ap.add_argument("--porta", type=int, default=config.intero("visore", "porta"))
    a = ap.parse_args()
    if a.scarica_librerie:
        return scarica_librerie()
    if not all((VENDOR / n).is_file() for n in CDN):
        print("nota: librerie del browser non scaricate, le prende da cdn.jsdelivr.net "
              "(python visore.py --scarica-librerie per averle in locale)")
    print("cartelle:", ", ".join(str(r) for r in RADICI))
    print(f"apri http://{a.host}:{a.porta}")
    import uvicorn
    uvicorn.run(crea_app(), host=a.host, port=a.porta, log_level="warning")


if __name__ == "__main__":
    main()
