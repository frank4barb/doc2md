"""Misura l'ordine di lettura delle pagine native di doc2md su un lotto SQLite (lotto.py).

Riferimento: il testo di pdftotext (ordine "naturale" di poppler) e, per l'intestazione, le prime
80 parole di `pdftotext -layout` (righe visive da sinistra a destra: riquadri anagrafici, moduli).
Per ogni PDF converte con doc2md (ed eventualmente con una versione precedente) e scrive un CSV:

  ordine      quota delle parole del riferimento ritrovate nella stessa sequenza (0-1)
  testa       come ordine, sulle prime 80 parole della prima pagina (layout)
  presenza    quota delle parole del riferimento presenti, in qualsiasi ordine
  secondi     tempo di conversione

Uso (dalla cartella di doc2md, con il suo venv):
  .venv/bin/python prove/ordine_lettura.py LOTTO.db [--n 1000] [--seme 7] [--csv out.csv]
        [--confronta doc2md.py.bak-AAAAMMGG]

Legge il lotto in sola lettura; i PDF passano a pdftotext da stdin (nessun file temporaneo).
Il CSV contiene solo chiavi e numeri, nessun testo dei documenti.
"""
import argparse
import csv
import difflib
import importlib.machinery
import importlib.util
import random
import re
import sqlite3
import statistics
import subprocess
import sys
import time
from pathlib import Path

QUI = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(QUI))
import doc2md  # noqa: E402

PAROLA = re.compile(r"[^\W\d_]{2,}|\d+", re.U)
TOGLI = re.compile(r"!\[[^\]]*\]\([^)]*\)|<!--.*?-->|</?[a-zA-Z][^<>]*>|\A---.*?^---|"
                   r"^\[img-[^\]]*\]:[^\n]*|\*\*doc_id:\*\*[^\n]*", re.S | re.M)


def parole(t):
    return [w.lower() for w in PAROLA.findall(TOGLI.sub(" ", t or ""))]


def ordine(rif, md):
    if not rif:
        return 1.0
    sm = difflib.SequenceMatcher(None, rif, md, autojunk=False)
    return sum(b.size for b in sm.get_matching_blocks()) / len(rif)


def presenza(rif, md):
    s = set(md)
    return sum(w in s for w in rif) / len(rif) if rif else 1.0


def pdftotext(blob, *opz):
    r = subprocess.run(["pdftotext", "-q", "-enc", "UTF-8", *opz, "-", "-"], input=blob,
                       capture_output=True, timeout=120)
    return r.stdout.decode("utf-8", "replace")


def carica(percorso):
    caricatore = importlib.machinery.SourceFileLoader("doc2md_confronto", str(percorso))
    spec = importlib.util.spec_from_loader("doc2md_confronto", caricatore)
    m = importlib.util.module_from_spec(spec)
    sys.modules["doc2md_confronto"] = m
    spec.loader.exec_module(m)
    return m


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("lotto")
    ap.add_argument("--n", type=int, default=1000, help="PDF da provare, scelti a caso (0 = tutti)")
    ap.add_argument("--seme", type=int, default=7)
    ap.add_argument("--csv", default="ordine_lettura.csv")
    ap.add_argument("--confronta", help="file doc2md.py di una versione precedente")
    a = ap.parse_args()
    versioni = [("nuovo", doc2md)]
    if a.confronta:
        versioni.insert(0, ("vecchio", carica(a.confronta)))
    c = sqlite3.connect(f"file:{a.lotto}?mode=ro", uri=True)
    chiavi = [r[0] for r in c.execute("select chiave from documenti order by chiave")]
    random.seed(a.seme)
    if a.n and a.n < len(chiavi):
        chiavi = random.sample(chiavi, a.n)
    campi = ["chiave"] + [f"{m}_{v}" for v, _ in versioni for m in ("ordine", "testa", "presenza", "secondi")]
    righe = []
    with open(a.csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(campi)
        for i, k in enumerate(chiavi, 1):
            blob, = c.execute("select contenuto from documenti where chiave=?", (k,)).fetchone()
            if not blob or blob[:5] != b"%PDF-":
                continue
            rif = parole(pdftotext(blob))
            testa = parole(pdftotext(blob, "-layout", "-l", "1"))[:80]
            riga = [k]
            for _, M in versioni:
                t = time.time()
                try:
                    md = parole(M.converti(blob, k + ".pdf", M.Opzioni(immagini="none"))[0])
                except Exception:
                    md = []
                riga += [round(ordine(rif, md), 4), round(ordine(testa, md[:160]), 4),
                         round(presenza(rif, md), 4), round(time.time() - t, 2)]
            w.writerow(riga)
            righe.append(riga)
            if i % 100 == 0:
                print(f"... {i}/{len(chiavi)}", file=sys.stderr, flush=True)
    print(f"PDF misurati: {len(righe)}  (CSV: {a.csv})")
    for j, nome in enumerate(campi[1:], 1):
        v = [r[j] for r in righe]
        if v:
            extra = "" if nome.startswith("secondi") else f"  sotto 0,95: {sum(x < 0.95 for x in v)}"
            print(f"{nome:18s} media {statistics.mean(v):.4f}  minimo {min(v):.3f}{extra}")
    if a.confronta:
        for m in ("ordine", "testa"):
            iv, inn = campi.index(f"{m}_vecchio"), campi.index(f"{m}_nuovo")
            d = [r[inn] - r[iv] for r in righe]
            print(f"{m}: migliorati {sum(x > 0.01 for x in d)}, peggiorati {sum(x < -0.01 for x in d)}")


if __name__ == "__main__":
    main()
