#!/usr/bin/env python3
"""prova_contesto.py - misura l'effetto del testo iniziale (titolo) sulle trascrizioni di whisper.

Trascrive i primi N minuti di un video due volte, senza e con il titolo come testo iniziale,
e una traccia di controllo (dove whisper aveva inventato una frase). Stampa numeri confrontabili:
  punteggiatura   segni (. , ; : ? !) ogni 100 parole
  maiuscole       % di frasi che dopo un punto cominciano con la maiuscola
  nomi            nomi propri del titolo scritti giusti / scritti in modo simile ma sbagliato
  ripetizioni     sequenze di 5 parole ripetute a breve distanza (indizio di frasi inventate)
  logprob min     punteggio peggiore di un pezzo (sotto -0.3: dubbio)
Si lancia una volta per configurazione di whisper-server (-mc 0, poi -mc 64) e si confrontano.
L'audio passa solo dalla RAM, come in doc2md.

  python prova_contesto.py "https://www.youtube.com/watch?v=..." --traccia controllo.mp3 --etichetta mc0
"""
import argparse
import sys
import difflib
import re
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # il progetto
import doc2md  # noqa: E402
import media   # noqa: E402


def misure(testo: str, segs_grezzi: list, titolo: str) -> dict:
    parole = re.findall(r"\w+", testo)
    n = max(1, len(parole))
    frasi = re.findall(r"[.?!]\s+(\S)", testo)
    nomi = [w for w in re.findall(r"\b[A-ZÀ-Ý][\wà-ÿ']+", titolo or "") if len(w) > 2]
    capitali = set(re.findall(r"\b[A-ZÀ-Ý][\wà-ÿ']+", testo))
    giusti = sum(len(re.findall(rf"\b{re.escape(w)}\b", testo)) for w in nomi)
    storpiati = sorted({c for w in nomi for c in difflib.get_close_matches(w, capitali, n=5, cutoff=0.7) if c != w})
    minuscole = [w.lower() for w in parole]
    rip = 0
    for i in range(len(minuscole) - 5):
        g = minuscole[i:i + 5]
        finestra = minuscole[i + 5:i + 80]
        if any(finestra[j:j + 5] == g for j in range(len(finestra) - 4)):
            rip += 1
    lp = [s.get("avg_logprob") for s in segs_grezzi if s.get("avg_logprob") is not None]
    dubbi = [f"{s.get('start', 0):.0f}s {s.get('avg_logprob'):.2f} {(s.get('text') or '').strip()[:70]!r}"
             for s in segs_grezzi if (s.get("avg_logprob") or 0) < -0.3]
    return {"parole": len(parole),
            "punteggiatura/100": round(100 * len(re.findall(r"[.,;:?!]", testo)) / n, 1),
            "maiuscole%": round(100 * sum(c[:1].isupper() for c in frasi) / max(1, len(frasi))),
            "nomi giusti": giusti, "nomi storpiati": storpiati, "ripetizioni": rip,
            "logprob min": round(min(lp), 3) if lp else None,
            "pezzi < -0.3": sum(x < -0.3 for x in lp), "_dubbi": dubbi}


def prova(cfg, pcm, contesto, titolo, etichetta):
    t0 = time.time()
    res = media.whisper(cfg, media.wav_bytes(pcm), "it", contesto)
    testo = media.paragrafi(media.pulisci(media.segmenti(res, 0)))
    m = misure(testo, res.get("segments") or [], titolo)
    print(f"\n== {etichetta}  ({time.time() - t0:.0f} s)")
    print("   " + "  ".join(f"{k}={v}" for k, v in m.items() if not k.startswith("_")))
    for d in m["_dubbi"]:
        print("      dubbio:", d)
    print("   " + re.sub(r"\s+", " ", testo)[:500] + "...")
    return testo


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("--minuti", type=float, default=10)
    ap.add_argument("--traccia", help="audio di controllo (es. la traccia con 'tessuto naturale')")
    ap.add_argument("--etichetta", default="", help="es. mc0 o mc64: finisce nei nomi dei file")
    a = ap.parse_args()
    cfg = doc2md.Config()
    print("scarico l'audio...")
    audio, info = media.scarica_audio(a.url)
    titolo = info.get("title") or ""
    tipo = doc2md.riconosci_tipo(audio)
    pcm = media.in_pcm(audio, tipo if tipo != "sconosciuto" else info.get("ext", "webm"))[:int(a.minuti * 60 * media.SR)]
    del audio
    print(f"titolo: {titolo!r}, primi {a.minuti:g} minuti")
    out = {}
    out["senza"] = prova(cfg, pcm, None, titolo, f"[{a.etichetta}] video SENZA testo iniziale")
    out["base"] = prova(cfg, pcm, media.contesto_di(media.BASE["it"]), titolo,
                        f"[{a.etichetta}] video con il testo iniziale di BASE")
    out["titolo"] = prova(cfg, pcm, media.contesto_di(media.BASE["it"], titolo), titolo,
                          f"[{a.etichetta}] video con BASE + TITOLO (come doc2md)")
    if a.traccia:
        p = Path(a.traccia)
        tp = doc2md.riconosci_tipo(p.read_bytes())
        pcm2 = media.in_pcm(p.read_bytes(), tp if tp != "sconosciuto" else p.suffix.lstrip("."))
        t = prova(cfg, pcm2, media.contesto_di(media.BASE["it"]), "",
                  f"[{a.etichetta}] traccia di controllo con BASE (come doc2md con --lingua it)")
        print(f"   'tessuto naturale' compare {t.lower().count('tessuto naturale')} volte (nell'audio: 1)")
        out["traccia"] = t
    for k, t in out.items():               # i testi interi, per rileggerli se serve
        Path(f"contesto-{a.etichetta or 'prova'}-{k}.md").write_text(t, encoding="utf-8")
    print(f"\ntesti interi in contesto-{a.etichetta or 'prova'}-*.md")


if __name__ == "__main__":
    main()
