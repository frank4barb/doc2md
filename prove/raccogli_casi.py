#!/usr/bin/env python3
"""raccogli_casi.py - prepara il pacchetto per migliorare schema_geo.py senza verifiche a mano.

Mette in un unico zip:
  casi.pdf         tutte le pagine verificate (con e senza schema), una dopo l'altra
  mappa.json       pagina di casi.pdf -> schema originale (file#pagina)
  riferimento.json, verdetti.json, conversione.json   le tue risposte e la conversione di allora
  diagnosi.json    per ogni pagina: cosa c'e' disegnato (linee, curve, rettangoli, riempimenti,
                   testo, immagini, font di simboli) e cosa ne ha capito schema_geo

Uso:
  python raccogli_casi.py out2/validazione --cartella campione/schemi_vettoriali
  -> out2/validazione/casi_schemi.zip  (da allegare)
"""
import argparse
import json
import sys
import zipfile
from collections import Counter
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # il progetto
import schema_geo  # noqa: E402


def diagnosi(page) -> dict:
    dr = page.get_drawings()
    items, fill, stroke, widths = Counter(), Counter(), Counter(), Counter()
    for d in dr:
        for it in d["items"]:
            items[it[0]] += 1
        if d.get("fill") is not None:
            fill[str(tuple(round(c, 2) for c in d["fill"]))] += 1
        if d.get("color") is not None:
            stroke[str(tuple(round(c, 2) for c in d["color"]))] += 1
        if d.get("width"):
            widths[round(d["width"], 1)] += 1
    fonts = Counter(f[3] for f in page.get_fonts(full=True))
    simboli = Counter(ch for ch in page.get_text("text") if ord(ch) > 0x2000 and not ch.isspace())
    out = {"disegni": len(dr), "elementi": dict(items), "riempimenti": dict(fill.most_common(6)),
           "contorni": dict(stroke.most_common(6)), "spessori": dict(widths.most_common(6)),
           "parole": len(page.get_text("words")), "immagini": len(page.get_images()),
           "font": dict(fonts), "simboli": dict(simboli.most_common(10)),
           "pagina": [round(page.rect.width), round(page.rect.height)]}
    try:
        nd, st, mk, fl, fr = schema_geo.read_page(page)
        out["schema_geo"] = {"riquadri": len(nd), "linee": len(st), "punte": len(mk),
                             "frecce_piene": len(fl), "testi_liberi": len(fr)}
    except Exception as e:
        out["schema_geo"] = {"errore": f"{type(e).__name__}: {e}"}
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("validazione", type=Path, help="cartella con conversione.json e verdetti.json")
    ap.add_argument("--cartella", type=Path, default=Path("campione/schemi_vettoriali"))
    a = ap.parse_args()
    v = a.validazione
    conv = json.loads((v / "conversione.json").read_text(encoding="utf-8"))
    out = pymupdf.open()
    mappa, diag, aperti = [], {}, {}
    for c in conv:
        f = a.cartella / c["file"]
        if not f.is_file():
            print(f"manca {f}, salto")
            continue
        src = aperti.setdefault(c["file"], pymupdf.open(f))
        out.insert_pdf(src, from_page=c["pagina"] - 1, to_page=c["pagina"] - 1)
        mappa.append({"pagina_casi": len(out), "id": c["id"]})
        diag[c["id"]] = diagnosi(src[c["pagina"] - 1])
    zp = v / "casi_schemi.zip"
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("casi.pdf", out.tobytes(garbage=3, deflate=True))
        z.writestr("mappa.json", json.dumps(mappa, ensure_ascii=False, indent=1))
        z.writestr("diagnosi.json", json.dumps(diag, ensure_ascii=False, indent=1))
        for n in ("riferimento.json", "verdetti.json", "conversione.json"):
            if (v / n).is_file():
                z.write(v / n, n)
    print(f"{len(mappa)} pagine raccolte in {zp} ({zp.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
