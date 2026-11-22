#!/usr/bin/env python3
"""d2m.py - client leggero di doc2md: manda i documenti al servizio attraverso llama-swap.

Non carica nessun modello e nessuna libreria pesante: si puo' lanciare quante volte si vuole.
La prima chiamata dopo una pausa aspetta che llama-swap avvii il servizio (pochi secondi);
le chiamate successive trovano tutto gia' in memoria; dopo il ttl llama-swap libera la memoria.

Uso:
  python d2m.py documento.pdf                    # un file della cartella input (senza percorso si cerca
                                                 # anche li'): .md nel suo lotto, che ricalca input sotto
                                                 # output (vedi lotto.py); altrimenti <output>/documento.pdf.md
  python d2m.py *.pdf --jev --out-dir convertiti
  python d2m.py fattura.pdf -o /tmp/fattura.md --immagini none
  python d2m.py manuale.pdf --immagini separate   # immagini in img/ accanto al .md (una per contenuto)
  python d2m.py https://www.youtube.com/watch?v=...  --out-dir trascrizioni
  python d2m.py https://sito.it/articolo -o articolo.md
  python d2m.py riunione.m4a --lingua it

Impostazioni: doc2md.ini ([servizi] doc2md_url e token, [cartelle] input e output).
"""
import argparse
import base64
import json
import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config                                   # noqa: E402  (solo libreria standard)

URL = config.valore("servizi", "doc2md_url").rstrip("/")
TOKEN = config.valore("servizi", "token")
ESTENSIONI = {"image/png": "png", "image/jpeg": "jpg"}
OUTPUT = config.cartella("output")


def e_link(s: str) -> bool:
    return bool(re.match(r"https?://", s, re.I))


def converti(sorgente: str, a) -> dict:
    head = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
    campi = {"immagini": a.immagini, "jev": str(a.jev).lower(), "testate": str(a.testate).lower(),
             "ocr": str(a.ocr).lower(), "formato": "json"}
    if a.doc_id:
        campi["doc_id"] = a.doc_id
    if a.lingua:
        campi["lingua"] = a.lingua
    if a.contesto:
        campi["contesto"] = a.contesto
    if e_link(sorgente):
        # i campi vanno come multipart (come per i file): FastAPI legge Form
        r = requests.post(f"{URL}/v1/converti", files={k: (None, v) for k, v in {**campi, "url": sorgente}.items()},
                          headers=head, timeout=a.timeout)
    else:
        path = Path(sorgente)
        with path.open("rb") as f:
            r = requests.post(f"{URL}/v1/converti", files={"file": (path.name, f)}, data=campi,
                              headers=head, timeout=a.timeout)
    if r.status_code != 200:
        try:
            msg = r.json().get("detail", r.text)
        except ValueError:
            msg = r.text
        raise RuntimeError(f"{r.status_code}: {str(msg)[:300]}")
    return r.json()


def nome_da_link(url: str) -> str:
    """Nome del .md per un link: ultimo pezzo del percorso o id del video, senza caratteri strani."""
    from urllib.parse import parse_qs, urlparse
    u = urlparse(url)
    base = parse_qs(u.query).get("v", [""])[0] or u.path.rstrip("/").rsplit("/", 1)[-1] or u.netloc
    return re.sub(r"[^\w.-]+", "_", base)[:80] or "pagina"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="+")
    ap.add_argument("-o", "--out", help="file .md (solo con un documento)")
    ap.add_argument("--out-dir", help="cartella per i .md (predefinito: output di doc2md.ini)")
    ap.add_argument("--immagini", choices=["embedded", "separate", "none"],
                    default=config.valore("conversione", "immagini"),
                    help="separate: file in img/ accanto al .md, nominati con l'impronta del contenuto")
    ap.add_argument("--jev", action="store_true", help="voto di Jev sulle regioni lette dai modelli")
    ap.add_argument("--doc-id")
    ap.add_argument("--testate", action="store_true", help="tiene intestazioni e numeri di pagina")
    ap.add_argument("--ocr", action="store_true", help="legge con i modelli anche le pagine native")
    ap.add_argument("--lingua", default=config.valore("conversione", "lingua") or None,
                    help="trascrizioni: it, en... (predefinito: doc2md.ini, altrimenti riconosciuta)")
    ap.add_argument("--contesto", help="trascrizioni: nomi e termini da scrivere giusti")
    ap.add_argument("--timeout", type=int, default=config.intero("conversione", "timeout"),
                       help="secondi massimi per documento (predefinito: doc2md.ini, 7200 = 2 ore)")
    ap.add_argument("-q", "--quiet", action="store_true", help="non stampa il rapporto")
    a = ap.parse_args()
    if a.out and len(a.file) > 1:
        raise SystemExit("-o vale per un solo documento: con piu' file usa --out-dir")
    errori = 0
    # i file della cartella input (senza -o, --out-dir, --doc-id) vanno nel loro lotto, come con
    # "lotto.py file": il .md ricalca input sotto output e si aggiornano lotto.db e img/ del lotto
    sorgenti = list(a.file)
    if not (a.out or a.out_dir or a.doc_id):
        inp = config.cartella("input").expanduser().resolve()
        nel_lotto = [str(config.trova(s)) for s in sorgenti if not e_link(s) and config.trova(s).is_file()
                     and config.trova(s).resolve().is_relative_to(inp)]
        if nel_lotto:
            import lotto
            sorgenti = [s for s in sorgenti if e_link(s) or str(config.trova(s)) not in nel_lotto]
            ns = argparse.Namespace(**{**vars(a), "percorsi": nel_lotto, "out": None, "rifai": False,
                                       "riprova_errori": False, "limite": None})
            if lotto.cmd_cartella(ns):
                errori += 1
    # file di link fuori da input (.links, .url, .webloc, .desktop): si convertono i link che contengono
    import lotto
    voci, contesto = [], a.contesto
    for s in sorgenti:
        p = None if e_link(s) else config.trova(s)
        if p is not None and p.is_file() and lotto.e_file_link(p):
            voci += [(ln.url, ln.contesto) for ln in lotto.link_di(p)] or [(s, None)]
        else:
            voci.append((s, None))
    for sorgente, desc in voci:
        a.contesto = desc or contesto       # la descrizione della riga della lista, se c'e'
        t0 = time.time()
        if not e_link(sorgente):
            sorgente = str(config.trova(sorgente))      # nome senza percorso: anche nella cartella input
        f = Path(nome_da_link(sorgente)) if e_link(sorgente) else Path(sorgente)
        if not e_link(sorgente) and not f.is_file():
            print(f"ERRORE {sorgente}: file non trovato (ne' qui ne' in {config.cartella('input')})", file=sys.stderr)
            errori += 1
            continue
        try:
            res = converti(sorgente, a)
        except requests.ConnectionError:
            raise SystemExit(f"llama-swap non raggiungibile su {URL}: e' acceso?")
        except Exception as e:
            print(f"ERRORE {f.name}: {e}", file=sys.stderr)
            errori += 1
            continue
        out = Path(a.out) if a.out else Path(a.out_dir or OUTPUT) / f"{f.name}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(res["markdown"], encoding="utf-8")
        for i in res.get("immagini") or []:
            dest = out.parent / "img" / i["file"]
            if dest.exists():                          # gia' salvata da un altro documento
                continue
            dest.parent.mkdir(exist_ok=True)
            dest.write_bytes(base64.b64decode(i["dati"]))
        if not a.quiet:
            print(json.dumps(res["rapporto"], indent=1, ensure_ascii=False))
        print(f"{sorgente if e_link(sorgente) else f.name} -> {out}  ({time.time() - t0:.1f} s)")
    sys.exit(1 if errori else 0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nInterrotto.")
