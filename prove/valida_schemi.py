#!/usr/bin/env python3
"""valida_schemi.py - verifica supervisionata della conversione degli schemi vettoriali.

1. prepara:  converte gli schemi dei PDF e crea una pagina HTML in cui, per ogni
             schema, si vedono l'originale (con i blocchi trovati numerati) e il
             Mermaid prodotto. Si segna a mano cosa e' giusto e si scaricano le
             risposte (verdetti.json).
2. calcola:  dalle risposte ricava le percentuali (blocchi, collegamenti, versi,
             mancanti, affidabilita' del controllo "mermaid/ripiego") e salva
             riferimento.json, la soluzione corretta confermata da te.
3. confronta: dopo una modifica al codice riconverte gli stessi schemi e li
             confronta con riferimento.json, senza bisogno di rifare la verifica.

Uso:
  python valida_schemi.py prepara campione/schemi_vettoriali --out out2/validazione
  (apri out2/validazione/validazione.html nel browser, compila, "Scarica verdetti")
  python valida_schemi.py calcola out2/validazione/verdetti.json
  python valida_schemi.py confronta out2/validazione/riferimento.json
"""
import argparse
import base64
import io
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # il progetto
import schema_geo                                          # noqa: E402
from doc2md import linee_verticali, tabella_vera           # noqa: E402

MIN_DISEGNI = 12     # come doc2md: sotto questa soglia la pagina non ha schemi
ZOOM = 2.0


# ---------------------------------------------------------------- conversione
def pagina_senza_tabelle(page):
    """Come in doc2md: le celle delle tabelle sembrano blocchi, quindi si cancellano
    su una copia in memoria della pagina."""
    try:
        lv = linee_verticali(page)
        tab = [pymupdf.Rect(t.bbox) for t in page.find_tables().tables if tabella_vera(t, page, lv)]
    except Exception:
        tab = []
    if not tab:
        return page, None
    tmp = pymupdf.open()
    tmp.insert_pdf(page.parent, from_page=page.number, to_page=page.number)
    tp = tmp[0]
    for t in tab:
        tp.add_redact_annot(t + (-1, -1, 1, 1))
    tp.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                        graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED)
    return tp, tmp          # tmp va tenuto vivo finche' si usa tp


def converti_pagina(page):
    """-> dict con blocchi, collegamenti, Mermaid ed esito (oppure None se non c'e' schema)."""
    if len(page.get_drawings()) < MIN_DISEGNI:
        return None
    p, keep = pagina_senza_tabelle(page)
    try:
        mer, rep, nodes, final = schema_geo.convert(p)
    except Exception as e:
        return {"errore": f"{type(e).__name__}: {e}", "nodi": [], "archi": [], "mermaid": "",
                "esito": "errore"}
    usati = {x for e in final for x in e[:2]}
    return {
        "nodi": [{"i": n.i, "testo": n.text.strip(), "forma": n.kind,
                  "rect": [round(v, 1) for v in n.r]} for n in nodes],
        "archi": [{"a": a, "b": b, "etichetta": lab, "tratteggiato": bool(d), "dedotto": bool(inf)}
                  for a, b, lab, d, inf in final],
        "usati": sorted(usati),
        "mermaid": mer, "esito": rep.get("esito"), "rapporto": rep,
    }


def area_schema(page, nodi):
    if not nodi:
        return page.rect
    r = pymupdf.Rect(nodi[0]["rect"])
    for n in nodi[1:]:
        r |= pymupdf.Rect(n["rect"])
    return (r + (-25, -25, 25, 25)) & page.rect


def immagine_con_blocchi(page, area, nodi) -> str:
    """Ritaglio dell'originale con i blocchi trovati riquadrati e numerati (N0, N1...)."""
    pix = page.get_pixmap(matrix=pymupdf.Matrix(ZOOM, ZOOM), clip=area, alpha=False)
    img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    dr = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 14)
    except Exception:
        font = ImageFont.load_default()
    for n in nodi:
        r = pymupdf.Rect(n["rect"])
        x0, y0 = (r.x0 - area.x0) * ZOOM, (r.y0 - area.y0) * ZOOM
        x1, y1 = (r.x1 - area.x0) * ZOOM, (r.y1 - area.y0) * ZOOM
        dr.rectangle([x0, y0, x1, y1], outline=(220, 40, 40), width=2)
        tag = f"N{n['i']}"
        tw = dr.textlength(tag, font=font)
        dr.rectangle([x0, y0 - 17, x0 + tw + 6, y0], fill=(220, 40, 40))
        dr.text((x0 + 3, y0 - 16), tag, fill="white", font=font)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


def svg_mermaid(code: str) -> str | None:
    exe = shutil.which("mmdc")
    if not exe or not code.strip():
        return None
    with tempfile.TemporaryDirectory() as d:
        src, out, cfg = Path(d) / "s.mmd", Path(d) / "s.svg", Path(d) / "p.json"
        src.write_text(code, encoding="utf-8")
        cfg.write_text('{"args": ["--no-sandbox"]}')
        try:
            r = subprocess.run([exe, "-p", str(cfg), "-i", str(src), "-o", str(out), "-b", "white"],
                               capture_output=True, timeout=120)
        except subprocess.TimeoutExpired:
            return None
        return out.read_text(encoding="utf-8") if r.returncode == 0 and out.is_file() else None


def prepara(cartella: Path, out: Path):
    out.mkdir(parents=True, exist_ok=True)
    casi = []
    pdfs = sorted(p for p in cartella.iterdir() if p.suffix.lower() == ".pdf")
    if not pdfs:
        raise SystemExit(f"Nessun PDF in {cartella}")
    for pdf in pdfs:
        doc = pymupdf.open(pdf)
        for page in doc:
            res = converti_pagina(page)
            if res is None:
                continue
            area = area_schema(page, res["nodi"])
            caso = {"id": f"{pdf.name}#p{page.number + 1}", "file": pdf.name, "pagina": page.number + 1,
                    **res, "immagine": immagine_con_blocchi(page, area, res["nodi"]),
                    "svg": svg_mermaid(res["mermaid"]) if res["esito"] != "errore" else None}
            casi.append(caso)
            print(f"{caso['id']:50} blocchi {len(res['nodi']):3}  collegamenti {len(res['archi']):3}  "
                  f"esito {res['esito']}")
    (out / "conversione.json").write_text(json.dumps(
        [{k: v for k, v in c.items() if k not in ("immagine", "svg")} for c in casi],
        ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "validazione.html").write_text(PAGINA.replace("__DATI__", json.dumps(casi, ensure_ascii=False)
                                                         .replace("</", "<\\/")), encoding="utf-8")
    print(f"\n{len(casi)} pagine con disegni. Apri {out / 'validazione.html'} nel browser, "
          f"compila e premi 'Scarica verdetti': il file va salvato come {out / 'verdetti.json'}")


# ---------------------------------------------------------------- calcolo
def calcola(verdetti: Path):
    V = json.loads(verdetti.read_text(encoding="utf-8"))
    conv = {c["id"]: c for c in json.loads((verdetti.parent / "conversione.json").read_text(encoding="utf-8"))}
    T = dict(schemi_veri=0, schemi_trovati=0, schemi_persi=0, falsi_schemi=0,
             blocchi=0, blocchi_ok=0, testo_errato=0, non_blocchi=0, blocchi_mancanti=0,
             archi=0, archi_ok=0, invertiti=0, inventati=0, mancanti=0,
             gate=dict(mermaid_utilizzabile=0, mermaid_non_utilizzabile=0,
                       ripiego_utilizzabile=0, ripiego_non_utilizzabile=0))
    righe, rif = [], []
    for cid, v in V.items():
        c = conv.get(cid)
        if c is None or v.get("schema") is None:
            continue
        if v["schema"] == "no":
            if c["nodi"]:
                T["falsi_schemi"] += 1
            continue
        T["schemi_veri"] += 1
        if not c["nodi"] or len(c["nodi"]) < 3:
            T["schemi_persi"] += 1
        else:
            T["schemi_trovati"] += 1
        nb = {int(k): s for k, s in v.get("nodi", {}).items()}
        ok_n = sum(s == "ok" for s in nb.values())
        T["blocchi"] += len(nb)
        T["blocchi_ok"] += ok_n
        T["testo_errato"] += sum(s == "testo" for s in nb.values())
        T["non_blocchi"] += sum(s == "no" for s in nb.values())
        T["blocchi_mancanti"] += int(v.get("blocchi_mancanti") or 0)
        ar = v.get("archi", {})
        ok_a = sum(s == "ok" for s in ar.values())
        inv = sum(s == "inv" for s in ar.values())
        bad = sum(s == "no" for s in ar.values())
        manc = int(v.get("archi_mancanti") or 0)
        T["archi"] += len(ar)
        T["archi_ok"] += ok_a
        T["invertiti"] += inv
        T["inventati"] += bad
        T["mancanti"] += manc
        uso = v.get("utilizzabile")
        if uso in ("si", "no") and c["esito"] in ("mermaid", "ripiego"):
            T["gate"][f"{c['esito']}_{'utilizzabile' if uso == 'si' else 'non_utilizzabile'}"] += 1
        veri = ok_a + inv + manc
        righe.append((cid, c["esito"], uso, len(nb), ok_n, len(ar), ok_a, inv, bad, manc,
                      ok_a / len(ar) if ar else None, (ok_a + inv) / veri if veri else None))
        # riferimento: collegamenti confermati, quelli invertiti nel verso giusto
        gold = []
        for k, s in ar.items():
            e = c["archi"][int(k)]
            if s == "ok":
                gold.append([e["a"], e["b"]])
            elif s == "inv":
                gold.append([e["b"], e["a"]])
        rif.append({"id": cid, "file": c["file"], "pagina": c["pagina"],
                    "nodi": [n for n in c["nodi"] if nb.get(n["i"]) in ("ok", "testo")],
                    "archi": gold, "archi_mancanti": manc, "blocchi_mancanti": int(v.get("blocchi_mancanti") or 0),
                    "utilizzabile": uso})
    pct = lambda a, b: f"{100 * a / b:.1f}%" if b else "n/d"
    veri = T["archi_ok"] + T["invertiti"] + T["mancanti"]
    out = [
        "# Validazione degli schemi vettoriali", "",
        f"Schemi verificati: {T['schemi_veri']} (trovati {T['schemi_trovati']}, persi {T['schemi_persi']}); "
        f"pagine senza schema scambiate per schema: {T['falsi_schemi']}.", "",
        "| Misura | Valore |", "|---|---|",
        f"| Blocchi giusti | {T['blocchi_ok']}/{T['blocchi']} ({pct(T['blocchi_ok'], T['blocchi'])}) |",
        f"| Blocchi con testo sbagliato | {T['testo_errato']} |",
        f"| Non erano blocchi | {T['non_blocchi']} |",
        f"| Blocchi mancanti | {T['blocchi_mancanti']} |",
        f"| Collegamenti giusti (precisione) | {T['archi_ok']}/{T['archi']} ({pct(T['archi_ok'], T['archi'])}) |",
        f"| Collegamenti col verso invertito | {T['invertiti']} |",
        f"| Collegamenti inventati | {T['inventati']} |",
        f"| Collegamenti mancanti | {T['mancanti']} |",
        f"| Collegamenti trovati sul totale reale (richiamo) | {T['archi_ok'] + T['invertiti']}/{veri} "
        f"({pct(T['archi_ok'] + T['invertiti'], veri)}) |", "",
        "## Il controllo automatico (mermaid / ripiego) e' affidabile?", "",
        "| Esito del programma | Per te utilizzabile | Per te non utilizzabile |", "|---|---|---|",
        f"| mermaid | {T['gate']['mermaid_utilizzabile']} | **{T['gate']['mermaid_non_utilizzabile']}** (errore grave) |",
        f"| ripiego | {T['gate']['ripiego_utilizzabile']} (occasione persa) | {T['gate']['ripiego_non_utilizzabile']} |", "",
        "## Per schema", "",
        "| Schema | Esito | Utilizzabile | Blocchi ok | Collegamenti ok | Invertiti | Inventati | Mancanti | Precisione | Richiamo |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    f = lambda x: f"{100 * x:.0f}%" if x is not None else "n/d"
    for cid, es, uso, nn, okn, na, oka, inv, bad, manc, prec, rec in righe:
        out.append(f"| {cid} | {es} | {uso or '?'} | {okn}/{nn} | {oka}/{na} | {inv} | {bad} | {manc} | {f(prec)} | {f(rec)} |")
    rep = verdetti.parent / "validazione-report.md"
    rep.write_text("\n".join(out) + "\n", encoding="utf-8")
    (verdetti.parent / "riferimento.json").write_text(json.dumps(rif, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n".join(out[:out.index("## Per schema")]))
    print(f"\nScritti {rep} e {verdetti.parent / 'riferimento.json'}")


# ---------------------------------------------------------------- confronto
def _iou(a, b):
    a, b = pymupdf.Rect(a), pymupdf.Rect(b)
    i = abs(a & b)
    u = abs(a) + abs(b) - i
    return i / u if u else 0


def confronta(riferimento: Path, cartella: Path):
    R = json.loads(riferimento.read_text(encoding="utf-8"))
    tot = dict(rif=0, trovati=0, invertiti=0, extra=0)
    print(f"{'Schema':50} {'ritrovati':>9} {'invertiti':>9} {'in piu':>7} {'esito':>8}")
    for g in R:
        if not g["archi"] and not g["archi_mancanti"]:
            continue
        doc = pymupdf.open(cartella / g["file"])
        res = converti_pagina(doc[g["pagina"] - 1]) or {"nodi": [], "archi": [], "esito": "nessuno"}
        # blocchi nuovi -> blocchi del riferimento, per posizione
        mappa = {}
        for n in res["nodi"]:
            best = max(g["nodi"], key=lambda m: _iou(n["rect"], m["rect"]), default=None)
            if best and _iou(n["rect"], best["rect"]) > 0.5:
                mappa[n["i"]] = best["i"]
        nuovi = {(mappa.get(e["a"]), mappa.get(e["b"])) for e in res["archi"]}
        gold = {tuple(e) for e in g["archi"]}
        ok = len(gold & nuovi)
        inv = len({(b, a) for a, b in gold} & nuovi - gold)
        extra = len(nuovi - gold - {(b, a) for a, b in gold})
        tot["rif"] += len(gold) + g["archi_mancanti"]
        tot["trovati"] += ok
        tot["invertiti"] += inv
        tot["extra"] += extra
        print(f"{g['id']:50} {ok:>4}/{len(gold) + g['archi_mancanti']:<4} {inv:>9} {extra:>7} {res['esito']:>8}")
    if tot["rif"]:
        print(f"\nCollegamenti ritrovati: {tot['trovati']}/{tot['rif']} "
              f"({100 * tot['trovati'] / tot['rif']:.1f}%), invertiti {tot['invertiti']}, in piu' {tot['extra']}")


# ---------------------------------------------------------------- pagina HTML
PAGINA = r"""<!doctype html>
<html lang="it"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Validazione schemi</title>
<style>
:root{--bg:#f6f6f4;--card:#fff;--ink:#1d1d1b;--mute:#6b6b66;--line:#ddddd8;--ok:#1f7a3a;--bad:#b3261e;--warn:#9a6700;--acc:#2456a6}
@media (prefers-color-scheme:dark){:root{--bg:#18181a;--card:#222225;--ink:#ececea;--mute:#a3a39e;--line:#3a3a3e;--ok:#5cc27a;--bad:#ff8a80;--warn:#e3b341;--acc:#8ab4ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--card);border-bottom:1px solid var(--line);padding:10px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
header b{font-size:16px}header .sp{flex:1}button{font:inherit;padding:6px 12px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--ink);cursor:pointer}
button.pri{background:var(--acc);color:#fff;border-color:var(--acc)}main{max-width:1500px;margin:0 auto;padding:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:0 0 18px;padding:14px}
.card h2{margin:0 0 4px;font-size:16px}.meta{color:var(--mute);font-size:13px;margin-bottom:10px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}@media (max-width:900px){.grid{grid-template-columns:1fr}}
.pane{border:1px solid var(--line);border-radius:8px;overflow:auto;max-height:620px;background:#fff}.pane img,.pane svg{display:block;max-width:100%;height:auto}
pre{margin:0;padding:8px;font-size:12px;white-space:pre-wrap;color:#333}
table{border-collapse:collapse;width:100%;margin-top:10px;font-size:14px}td,th{border-bottom:1px solid var(--line);padding:4px 6px;text-align:left;vertical-align:middle}
th{color:var(--mute);font-weight:600}select,input{font:inherit;padding:3px 6px;border:1px solid var(--line);border-radius:5px;background:var(--card);color:var(--ink)}
.q{display:flex;gap:18px;flex-wrap:wrap;margin:10px 0}.q label{display:flex;gap:6px;align-items:center}
.tag{display:inline-block;padding:1px 7px;border-radius:10px;font-size:12px;border:1px solid var(--line)}
.mermaid-t{color:var(--ok);border-color:var(--ok)}.ripiego-t{color:var(--warn);border-color:var(--warn)}.errore-t{color:var(--bad);border-color:var(--bad)}
.done{border-left:4px solid var(--ok)}.todo{border-left:4px solid var(--line)}details{margin-top:8px}summary{cursor:pointer;color:var(--mute)}
.hint{color:var(--mute);font-size:13px}
</style></head><body>
<header><b>Validazione schemi vettoriali</b><span id="prog" class="hint"></span><span class="sp"></span>
<button onclick="tuttiOk()">Segna come giusto ciò che non hai toccato</button>
<button class="pri" onclick="scarica()">Scarica verdetti</button></header>
<main>
<p class="hint">Per ogni pagina: a sinistra l'originale, con i blocchi trovati riquadrati in rosso e numerati (N0, N1…); a destra il Mermaid prodotto.
Indica se nella pagina c'è davvero uno schema a blocchi, poi controlla blocchi e collegamenti. I collegamenti hanno il verso del Mermaid (da → a).
Le risposte restano salvate in questo browser; alla fine premi <b>Scarica verdetti</b> e salva il file come <code>verdetti.json</code> accanto a questa pagina.</p>
<div id="lista"></div></main>
<script>
const CASI = __DATI__;
const KEY = "valida-schemi:" + CASI.map(c => c.id).join("|").length + ":" + (CASI[0]||{}).id;
let V = {};
try { V = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) { V = {}; }
const salva = () => { try { localStorage.setItem(KEY, JSON.stringify(V)); } catch (e) {} aggiorna(); };
const esc = s => String(s ?? "").replace(/[&<>"]/g, m => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[m]));
function v(id){ return V[id] ??= {schema:null, nodi:{}, archi:{}, archi_mancanti:0, blocchi_mancanti:0, utilizzabile:null, note:""}; }
function sel(name, val, opts, on){ return `<select data-k="${name}" onchange="${on}">` + opts.map(([k,t]) => `<option value="${k}" ${k===val?"selected":""}>${t}</option>`).join("") + `</select>`; }
function card(c, idx){
  const r = v(c.id), nome = i => { const n = c.nodi.find(n => n.i === i); return n ? `N${i} “${esc(n.testo || "—")}”` : `N${i}`; };
  const usati = new Set(c.usati || []);
  const nodi = c.nodi.filter(n => usati.has(n.i) || c.nodi.length <= 40);
  return `<section class="card ${completo(c)?"done":"todo"}" id="c${idx}">
  <h2>${esc(c.id)} <span class="tag ${c.esito}-t">${esc(c.esito)}</span></h2>
  <div class="meta">${c.nodi.length} blocchi trovati · ${c.archi.length} collegamenti${c.errore ? " · errore: " + esc(c.errore) : ""}</div>
  <div class="grid"><div class="pane"><img src="data:image/png;base64,${c.immagine}" alt="originale"></div>
  <div class="pane">${c.svg || `<pre>${esc(c.mermaid || "(nessun Mermaid)")}</pre>`}</div></div>
  <div class="q"><span>In questa pagina c'è uno schema a blocchi?</span>
   <label><input type="radio" name="s${idx}" ${r.schema==="si"?"checked":""} onclick="set('${c.id}','schema','si')"> sì</label>
   <label><input type="radio" name="s${idx}" ${r.schema==="no"?"checked":""} onclick="set('${c.id}','schema','no')"> no (grafico, tabella, decorazione…)</label></div>
  ${r.schema === "si" ? `
  <table><tr><th>Blocco</th><th>Forma</th><th>Giudizio</th></tr>
  ${nodi.map(n => `<tr><td>${nome(n.i)}</td><td>${n.forma}</td><td>${sel("", r.nodi[n.i] ?? "", [["","—"],["ok","giusto"],["testo","blocco giusto, testo sbagliato"],["no","non è un blocco"]], `setN('${c.id}','nodi',${n.i},this.value)`)}</td></tr>`).join("")}
  </table>
  <table><tr><th>Collegamento</th><th>Etichetta</th><th>Giudizio</th></tr>
  ${c.archi.map((e,k) => `<tr><td>${nome(e.a)} → ${nome(e.b)}${e.dedotto?" <span class=hint>(dedotto)</span>":""}</td><td>${esc(e.etichetta)}</td><td>${sel("", r.archi[k] ?? "", [["","—"],["ok","giusto"],["inv","esiste ma col verso opposto"],["no","non esiste"]], `setN('${c.id}','archi',${k},this.value)`)}</td></tr>`).join("") || `<tr><td colspan=3 class=hint>nessun collegamento trovato</td></tr>`}
  </table>
  <div class="q"><label>Blocchi mancanti <input type="number" min="0" value="${r.blocchi_mancanti}" style="width:70px" onchange="set('${c.id}','blocchi_mancanti',+this.value)"></label>
   <label>Collegamenti mancanti <input type="number" min="0" value="${r.archi_mancanti}" style="width:70px" onchange="set('${c.id}','archi_mancanti',+this.value)"></label></div>
  <div class="q"><span>Il Mermaid, così com'è, si può usare al posto dell'immagine?</span>
   <label><input type="radio" name="u${idx}" ${r.utilizzabile==="si"?"checked":""} onclick="set('${c.id}','utilizzabile','si')"> sì</label>
   <label><input type="radio" name="u${idx}" ${r.utilizzabile==="no"?"checked":""} onclick="set('${c.id}','utilizzabile','no')"> no</label></div>
  <details><summary>Nota</summary><input style="width:100%" value="${esc(r.note)}" onchange="set('${c.id}','note',this.value)"></details>
  <details><summary>Testo Mermaid</summary><pre>${esc(c.mermaid)}</pre></details>` : ""}
  </section>`;
}
function completo(c){ const r = V[c.id]; if (!r || !r.schema) return false; if (r.schema === "no") return true;
  return r.utilizzabile && c.archi.every((e,k) => r.archi[k]) ; }
function set(id, k, val){ v(id)[k] = val; salva(); if (k === "schema") rendi(); }
function setN(id, k, i, val){ v(id)[k][i] = val; salva(); }
function tuttiOk(){ for (const c of CASI){ const r = v(c.id); if (r.schema !== "si") continue;
  c.nodi.forEach(n => { if (!r.nodi[n.i] && ((c.usati||[]).includes(n.i) || c.nodi.length <= 40)) r.nodi[n.i] = "ok"; });
  c.archi.forEach((e,k) => { if (!r.archi[k]) r.archi[k] = "ok"; }); } salva(); rendi(); }
function aggiorna(){ const f = CASI.filter(completo).length; document.getElementById("prog").textContent = `${f} di ${CASI.length} pagine completate`; }
function rendi(){ const y = scrollY; document.getElementById("lista").innerHTML = CASI.map(card).join(""); scrollTo(0, y); aggiorna(); }
function scarica(){ const b = new Blob([JSON.stringify(V, null, 1)], {type: "application/json"});
  const a = document.createElement("a"); a.href = URL.createObjectURL(b); a.download = "verdetti.json"; a.click(); }
rendi();
</script></body></html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepara")
    p.add_argument("cartella", type=Path)
    p.add_argument("--out", type=Path, default=Path("out2/validazione"))
    c = sub.add_parser("calcola")
    c.add_argument("verdetti", type=Path)
    k = sub.add_parser("confronta")
    k.add_argument("riferimento", type=Path)
    k.add_argument("--cartella", type=Path, default=Path("campione/schemi_vettoriali"))
    a = ap.parse_args()
    if a.cmd == "prepara":
        prepara(a.cartella, a.out)
    elif a.cmd == "calcola":
        calcola(a.verdetti)
    else:
        confronta(a.riferimento, a.cartella)


if __name__ == "__main__":
    main()
