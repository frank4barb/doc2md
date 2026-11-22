"""banco_schemi.py - prova automatica di schema_geo.py sugli schemi gia' verificati.

Converte tutte le pagine di casi.pdf (dallo zip di raccogli_casi.py) con lo schema_geo
indicato e le confronta con riferimento.json (la tua verifica) e riferimento_claude.json
(pagine ricontrollate sul disegno). Nessuna verifica a mano.

Uso (dalla cartella del progetto):
  python prove/banco_schemi.py <cartella dei casi> [schema_geo.py] [-v]
La cartella dei casi contiene casi.pdf, mappa.json, verdetti.json, riferimento.json (prodotti da
raccogli_casi.py e dalla verifica a vista) ed eventualmente riferimento_claude.json.
"""
import sys, json, importlib.util
from pathlib import Path
import pymupdf
PROGETTO = Path(__file__).resolve().parent.parent
CASI = Path(sys.argv[1])
SG = Path(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].endswith(".py") else PROGETTO / "schema_geo.py"
spec = importlib.util.spec_from_file_location("schema_geo", SG); sg = importlib.util.module_from_spec(spec)
sys.modules["schema_geo"] = sg; spec.loader.exec_module(sg)
sys.path.insert(0, str(PROGETTO)); sys.path.insert(0, str(Path(__file__).resolve().parent))
import valida_schemi as vs
vs.schema_geo = sg
R = {r["id"]: r for r in json.load(open(CASI / "riferimento.json"))}
if (CASI / "riferimento_claude.json").is_file():          # pagine ricontrollate da Claude
    R.update({r["id"]: r for r in json.load(open(CASI / "riferimento_claude.json"))})
V = json.load(open(CASI / "verdetti.json"))
M = json.load(open(CASI / "mappa.json")); doc = pymupdf.open(CASI / "casi.pdf")
iou = vs._iou
tot = dict(gold=0, ok=0, inv=0, extra=0, reali=0, trovati_zero=0, mancanti_zero=0)
gate = {"mermaid_si": 0, "mermaid_no": 0, "ripiego_si": 0, "ripiego_no": 0}
fals = 0
gate2 = {}
righe = []
for m in M:
    cid = m["id"]; v = V.get(cid, {}); r = R.get(cid)
    res = vs.converti_pagina(doc[m["pagina_casi"] - 1]) or {"nodi": [], "archi": [], "esito": "nessuno"}
    if v.get("schema") == "no":
        if res["esito"] == "mermaid": fals += 1
        righe.append((cid, "no-schema", res["esito"], "", "")); continue
    if not r: continue
    uso = v.get("utilizzabile")
    if res["esito"] in ("mermaid", "ripiego") and uso in ("si", "no"):
        gate[f"{res['esito']}_{uso}"] += 1
    gold = {tuple(e) for e in r["archi"]}
    reali = len(gold) + r["archi_mancanti"]
    tot["reali"] += reali
    if gold:
        mp = {}
        for n in res["nodi"]:
            b = max(r["nodi"], key=lambda g: iou(n["rect"], g["rect"]), default=None)
            if b and iou(n["rect"], b["rect"]) > 0.5: mp[n["i"]] = b["i"]
        new = {(mp.get(e["a"]), mp.get(e["b"])) for e in res["archi"]}
        ok = len(gold & new); inv = len({(b, a) for a, b in gold} & new - gold)
        ex = len({e for e in new if None not in e} - gold - {(b, a) for a, b in gold})
        tot["ok"] += ok; tot["inv"] += inv; tot["extra"] += ex; tot["gold"] += len(gold)
        ritrovati = min(ex, r["archi_mancanti"])      # collegamenti nuovi = mancanti segnalati
        tot["mancanti_ritrovati"] = tot.get("mancanti_ritrovati", 0) + ritrovati
        tot["extra_veri"] = tot.get("extra_veri", 0) + ex - ritrovati
        perfetto = ok + ritrovati == reali and inv == 0 and ex - ritrovati == 0
        gate2[f"{res['esito']}_{'esatto' if perfetto else 'imperfetto'}"] = gate2.get(f"{res['esito']}_{'esatto' if perfetto else 'imperfetto'}", 0) + 1
        righe.append((cid, f"{ok}/{reali}", res["esito"], inv, ex))
    else:   # nessun collegamento confermato: si conta solo quanti ne trova contro quanti ce ne sono
        tot["trovati_zero"] += len(res["archi"]); tot["mancanti_zero"] += reali
        righe.append((cid, f"~{len(res['archi'])}/{reali}", res["esito"], "", ""))
if "-v" in sys.argv:
    for x in righe: print(f"{x[0][:46]:46} {x[1]:>8} {x[2]:>8} {x[3]!s:>3} {x[4]!s:>3}")
print(f"schemi con riferimento: ritrovati {tot['ok']}/{tot['gold']} confermati, invertiti {tot['inv']}, "
      f"nuovi {tot['extra']} (di cui {tot.get('mancanti_ritrovati', 0)} al posto dei mancanti segnalati, "
      f"{tot.get('extra_veri', 0)} in piu' davvero)")
print(f"schemi senza collegamenti confermati: trovati {tot['trovati_zero']} su {tot['mancanti_zero']} reali")
print(f"esito sugli schemi con riferimento (esatto = tutti i collegamenti giusti e nessuno in piu'): {gate2}")
print(f"controllo: {gate}  | pagine senza schema date come mermaid: {fals}")
