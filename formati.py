"""formati.py - letture strutturate per doc2md.

Word e PowerPoint si leggono dalla loro struttura (titoli, tabelle, formule OMML -> LaTeX esatte,
note del relatore, dati dei grafici, SmartArt -> Mermaid) con i lettori di MarkItDown usati come
libreria: solo quelli locali, mai trascrizione audio, YouTube, Azure o descrizioni con LLM esterni.
Le immagini contenute passano dalla pipeline di doc2md (schemi -> Mermaid, pagine scansionate
-> OCR, altre -> immagini). I formati vecchi (doc, rtf, odt, ppt, odp) diventano prima docx/pptx
con LibreOffice. Un documento disegnato con frecce e connettori passa invece dal PDF, dove
schema_geo ricostruisce lo schema.

Poi: archivi (zip, tar, gz, bz2, xz, 7z), posta (eml, msg con gli allegati), epub, html,
notebook Jupyter, json e xml. Tutto in memoria.
"""
import email
import gzip
import io
import json
import posixpath
import re
import tarfile
import zipfile
from collections import defaultdict
from email import policy

from PIL import Image

D = None    # il modulo doc2md, collegato da doc2md all'avvio

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "dgm": "http://schemas.openxmlformats.org/drawingml/2006/diagram",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
}
W, R, MC = (f"{{{NS[k]}}}" for k in ("w", "r", "mc"))
CONNETTORI = re.compile(rb'cNvCnPr|<p:cxnSp[\s>]|prst="(?:straightConnector1|bentConnector\d|curvedConnector\d)"')
SEGNO = re.compile(r"D2M(?:IMG|OGG)\d+X")
SCARTI = re.compile(r"(^|/)(__MACOSX/|\.DS_Store$|Thumbs\.db$|desktop\.ini$|~\$)", re.I)


# ---------------------------------------------------------------- immagini contenute
def in_png(blob: bytes) -> bytes | None:
    """Qualunque immagine contenuta -> PNG. EMF/WMF (disegni di Word) con LibreOffice."""
    try:
        im = Image.open(io.BytesIO(blob))
        im.load()
        return D.png_bytes(im.convert("RGB"))
    except Exception:
        pass
    emf = len(blob) > 44 and blob[40:44] == b" EMF"
    wmf = blob[:4] == b"\xd7\xcd\xc6\x9a" or (blob[:2] in (b"\x01\x00", b"\x02\x00") and blob[2:4] == b"\x09\x00")
    if not (emf or wmf):
        return None
    try:
        png = D._ufficio.converti(blob, ".emf" if emf else ".wmf", "png")
        im = Image.open(io.BytesIO(png)).convert("RGB")
        from PIL import ImageChops
        bbox = ImageChops.difference(im, Image.new("RGB", im.size, "white")).getbbox()
        return D.png_bytes(im.crop(bbox)) if bbox else b""    # LibreOffice la mette su una pagina
    except Exception:
        return None


def immagine(doc, blob: bytes, pagina: int, in_tabella: bool = False) -> str:
    png = in_png(blob)
    if png == b"":                              # disegno vuoto (anteprima bianca di un oggetto)
        return ""
    if png is None:
        doc.avvisi.append(f"p{pagina}: immagine in un formato non leggibile, omessa")
        return ""
    w, h = Image.open(io.BytesIO(png)).size
    if w * h < 3000 or min(w, h) < 24:          # icone, punti elenco, righe decorative
        return ""
    if in_tabella:
        return doc.figura(png, pagina)
    if min(w, h) >= 900 and w * h >= 1_200_000:  # una pagina scansionata incollata nel documento
        md, _ = D.pagina_scansione(doc, png, pagina)
        return md
    return doc.schema_da_immagine(png, pagina)


def sostituisci(doc, md: str, immagini: dict, oggetti: dict, pagina: int) -> str:
    """I segnaposto lasciati nel testo diventano Mermaid, tabelle dei grafici o immagini."""
    righe = []
    for riga in md.split("\n"):
        for tok in SEGNO.findall(riga):
            tab = riga.lstrip().startswith("|")
            if tok in oggetti:
                rep = oggetti[tok]
                rep = "(grafico: vedi sotto la tabella)" if tab else rep
                if tab:
                    doc.pagine_extra.append(oggetti[tok])
            else:
                rep = immagine(doc, immagini.pop(tok, b""), pagina, tab)
            riga = riga.replace(tok, rep if tab else f"\n\n{rep}\n\n")
        righe.append(riga)
    md = "\n".join(righe)
    return re.sub(r"\n{3,}", "\n\n", md).strip()


# ---------------------------------------------------------------- grafici e SmartArt (OOXML)
def _x():
    from lxml import etree
    return etree


def relazioni(z: zipfile.ZipFile, parte: str) -> dict:
    d, f = posixpath.split(parte)
    rp = f"{d}/_rels/{f}.rels"
    if rp not in z.namelist():
        return {}
    out = {}
    for r in _x().fromstring(z.read(rp)):
        if r.get("TargetMode") == "External":
            continue
        t = r.get("Target", "")
        out[r.get("Id")] = t.lstrip("/") if t.startswith("/") else posixpath.normpath(posixpath.join(d, t))
    return out


def _cache(el) -> list:
    if el is None:
        return []
    liv = el.find(".//c:lvl", NS)                    # categorie su piu' livelli: il primo
    base = liv if liv is not None else el
    pts = {int(p.get("idx")): (p.findtext("c:v", namespaces=NS) or "") for p in base.iterfind(".//c:pt", NS)}
    return [pts.get(i, "") for i in range(max(pts) + 1)] if pts else []


def _num(v: str) -> str:
    try:
        f = float(v)
        return f"{f:g}" if abs(f) < 1e15 else v
    except ValueError:
        return v


def grafico_md(xml: bytes) -> str:
    """Grafico di Office -> tabella dei dati (i valori sono salvati nel file: esatti)."""
    root = _x().fromstring(xml)
    titolo = " ".join(t.text or "" for t in root.iterfind(".//c:title//a:t", NS)).strip()
    area = root.find(".//c:plotArea", NS)
    tipi = sorted({el.tag.split("}")[1].replace("Chart", "") for el in area if el.tag.endswith("Chart")}) \
        if area is not None else []
    serie, cat = [], []
    for ser in root.iterfind(".//c:ser", NS):
        nome = " ".join(v.text or "" for v in ser.iterfind("c:tx//c:v", NS)).strip() or f"Serie {len(serie) + 1}"
        val = _cache(ser.find("c:val", NS)) or _cache(ser.find("c:yVal", NS))
        cat = cat or _cache(ser.find("c:cat", NS)) or _cache(ser.find("c:xVal", NS))
        serie.append((nome, val))
    if not serie:
        return f"**Grafico{': ' + titolo if titolo else ''}** (senza dati leggibili)"
    n = max([len(cat)] + [len(v) for _, v in serie])
    cat = (cat + [str(i + 1) for i in range(len(cat), n)])[:n]
    testa = "| " + " | ".join(["Categoria"] + [s for s, _ in serie]) + " |"
    righe = [testa, "|" + "---|" * (len(serie) + 1)]
    for i in range(n):
        righe.append("| " + " | ".join([cat[i]] + [_num(v[i]) if i < len(v) else "" for _, v in serie]) + " |")
    tipo = f" ({', '.join(tipi)})" if tipi else ""
    return f"**Grafico{': ' + titolo if titolo else ''}**{tipo}\n\n" + "\n".join(righe)


def smartart_md(dati: bytes, layout: bytes | None = None) -> str:
    """SmartArt (organigrammi, processi, gerarchie) -> Mermaid, dal modello dei dati."""
    from schema_raster import mermaid_testo
    root = _x().fromstring(dati)
    nodi, radice = {}, None
    for pt in root.iterfind(".//dgm:ptLst/dgm:pt", NS):
        t = pt.get("type", "node")
        if t in ("parTrans", "sibTrans", "pres"):
            continue
        paragrafi = [" ".join((x.text or "") for x in p.iterfind(".//a:t", NS)).strip()
                     for p in pt.iterfind("dgm:t/a:p", NS)]
        nodi[pt.get("modelId")] = " / ".join(p for p in paragrafi if p)
        if t == "doc":
            radice = pt.get("modelId")
    figli = defaultdict(list)
    for cx in root.iterfind(".//dgm:cxnLst/dgm:cxn", NS):
        if cx.get("type", "parOf") == "parOf" and cx.get("srcId") in nodi and cx.get("destId") in nodi:
            figli[cx.get("srcId")].append((int(cx.get("srcOrd", "0")), cx.get("destId")))
    lay = ""
    if layout:
        try:
            lay = _x().fromstring(layout).get("uniqueId", "")
        except Exception:
            pass
    archi, ordine, visti = [], [], set()

    def visita(n, padre):                 # i nodi senza testo si saltano, i figli salgono al padre
        if n in visti:
            return
        visti.add(n)
        vivo = n != radice and nodi.get(n)
        if vivo:
            ordine.append(n)
            if padre:
                archi.append((padre, n))
        for _, f in sorted(figli.get(n, [])):
            visita(f, n if vivo else padre)
    if radice is None:
        return ""
    visita(radice, None)
    if not ordine:
        return ""
    ids = {n: f"N{i}" for i, n in enumerate(ordine)}
    if not archi:                          # elenco piatto: processo (in fila), ciclo o semplice elenco
        if re.search(r"process|chevron|arrow|cycle|flow|timeline", lay, re.I):
            archi = list(zip(ordine, ordine[1:])) + ([(ordine[-1], ordine[0])] if "cycle" in lay.lower() else [])
            verso = "LR"
        else:
            return "\n".join(f"- {nodi[n]}" for n in ordine)
    else:
        verso = "TD"
    righe = [f"flowchart {verso}"] + [f'    {ids[n]}["{mermaid_testo(nodi[n])}"]' for n in ordine]
    righe += [f"    {ids[a]} --> {ids[b]}" for a, b in archi]
    return "```mermaid\n" + "\n".join(righe) + "\n```"


def _da_struttura(z, rels, el, chiave):
    t = rels.get(el.get(R + chiave))
    return z.read(t) if t and t in z.namelist() else None


# ---------------------------------------------------------------- Word
def word(doc, data: bytes, t: str):
    if t != "docx":
        data = D._ufficio.converti(data, f".{t}", "docx")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        xml = z.read("word/document.xml")
    except Exception as e:
        raise D.Errore(f"documento Word non leggibile: {e}")
    n = len(CONNETTORI.findall(xml))
    if n >= 2:
        doc.avvisi.append(f"disegno con {n} connettori: letto dal PDF per convertire lo schema")
        return D.pagine_pdf(doc, D._ufficio.converti(data, ".docx", "pdf"))
    try:
        md = _word(doc, z, xml, data)
    except D.Errore:
        raise
    except Exception as e:
        doc.avvisi.append(f"lettura strutturata non riuscita ({type(e).__name__}: {e}): letto dal PDF")
        doc.pagine, doc.info_pagine = [], []
        return D.pagine_pdf(doc, D._ufficio.converti(data, ".docx", "pdf"))
    doc.pagine.append(md)
    doc.info_pagine.append({"metodo": "struttura"})


def _word(doc, z, xml, data: bytes) -> str:
    from markitdown import StreamInfo
    from markitdown.converters import DocxConverter
    etree = _x()
    root = etree.fromstring(xml)
    rels = relazioni(z, "word/document.xml")
    oggetti, fatti = {}, set()
    for dr in list(root.iter(W + "drawing")):
        ch, dg = dr.find(".//c:chart", NS), dr.find(".//dgm:relIds", NS)
        if ch is None and dg is None:
            continue
        el, a = dr, dr.getparent()
        while a is not None:                       # il disegno puo' stare dentro mc:AlternateContent
            if a.tag == MC + "AlternateContent":
                el = a
                break
            a = a.getparent()
        if id(el) in fatti or el.getparent() is None:
            continue
        fatti.add(id(el))
        if ch is not None:
            parte = _da_struttura(z, rels, ch, "id")
            md = grafico_md(parte) if parte else ""
        else:
            parte = _da_struttura(z, rels, dg, "dm")
            md = smartart_md(parte, _da_struttura(z, rels, dg, "lo")) if parte else ""
            if md.startswith("```mermaid"):
                doc.schemi["mermaid"] += 1
        tok = f"D2MOGG{len(oggetti)}X"
        oggetti[tok] = md
        tt = etree.Element(W + "t")
        tt.text = tok
        padre = el.getparent()
        if padre.tag == W + "r":
            padre.replace(el, tt)
        else:
            r = etree.Element(W + "r")
            r.append(tt)
            padre.replace(el, r)
    sorgente = io.BytesIO()
    if oggetti:
        nuovo = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        with zipfile.ZipFile(sorgente, "w", zipfile.ZIP_DEFLATED) as zo:
            for it in z.infolist():
                zo.writestr(it, nuovo if it.filename == "word/document.xml" else z.read(it.filename))
    else:
        sorgente = io.BytesIO(data)
    sorgente.seek(0)
    immagini = {}

    class Lettore(DocxConverter):
        def _image_to_html(self, stream, stream_info, **kw):
            tok = f"D2MIMG{len(immagini)}X"
            immagini[tok] = stream.read()
            return f"<p>{tok}</p>"
    doc.pagine_extra = []
    md = Lettore().convert(sorgente, StreamInfo(extension=".docx")).markdown
    md = sostituisci(doc, md, immagini, oggetti, 1)
    return "\n\n".join([md] + doc.pagine_extra)


# ---------------------------------------------------------------- PowerPoint
def presentazione(doc, data: bytes, t: str):
    if t != "pptx":
        data = D._ufficio.converti(data, f".{t}", "pptx")
    import pptx
    from markitdown import StreamInfo
    from markitdown.converters import PptxConverter
    etree = _x()
    try:
        prs = pptx.Presentation(io.BytesIO(data))
    except Exception as e:
        raise D.Errore(f"presentazione non leggibile: {e}")
    slides = list(prs.slides)
    disegnate = [len(CONNETTORI.findall(etree.tostring(s._element))) >= 2 for s in slides]
    immagini = {}

    class Lettore(PptxConverter):
        def _image_to_html(self, stream, stream_info, **kw):
            tok = f"D2MIMG{len(immagini)}X"
            immagini[tok] = stream.read()
            return f"<p>{tok}</p>"
    md = Lettore().convert(io.BytesIO(data), StreamInfo(extension=".pptx")).markdown
    pezzi = re.split(r"<!-- Slide number: (\d+) -->", md)
    per_numero = {int(pezzi[i]): pezzi[i + 1] for i in range(1, len(pezzi) - 1, 2)}
    pdf = D.apri_pdf(D._ufficio.converti(data, ".pptx", "pdf")) if any(disegnate) else None
    for i, s in enumerate(slides):
        num = i + 1
        testo = per_numero.get(num, "").strip()
        corpo, _, note = testo.partition("### Notes:")
        extra = []
        for gf in s._element.iter("{http://schemas.openxmlformats.org/presentationml/2006/main}graphicFrame"):
            dg = gf.find(".//dgm:relIds", NS)
            if dg is None:
                continue
            try:
                dati = s.part.related_part(dg.get(R + "dm")).blob
                lay = s.part.related_part(dg.get(R + "lo")).blob if dg.get(R + "lo") else None
                m = smartart_md(dati, lay)
                if m.startswith("```mermaid"):
                    doc.schemi["mermaid"] += 1
                extra.append(m)
            except Exception as e:
                doc.avvisi.append(f"p{num}: SmartArt non letto ({e})")
        if disegnate[i] and pdf is not None and i < len(pdf):
            corpo, info = D.pagina_pdf(doc, pdf[i], num)       # frecce e connettori: schema dal PDF
            extra = []
        else:
            doc.pagine_extra = []
            corpo = sostituisci(doc, corpo, immagini, {}, num)
            info = {"metodo": "struttura"}
        corpo = corpo.replace("### Chart", "### Grafico").replace("| Category |", "| Categoria |")
        parti = [corpo.strip()] + extra
        if note.strip():
            parti.append("### Note del relatore\n\n" + note.strip())
        doc.pagine.append("\n\n".join(p for p in parti if p))
        doc.info_pagine.append(info)


# ---------------------------------------------------------------- html, epub, notebook, dati
def decodifica(data: bytes) -> str:
    m = re.search(rb"""<meta[^>]+charset=["']?([\w-]+)""", data[:4096], re.I)
    for enc in ([m[1].decode()] if m else []) + ["utf-8"]:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            pass
    try:
        from charset_normalizer import from_bytes
        best = from_bytes(data).best()
        if best:
            return str(best)
    except Exception:
        pass
    return data.decode("cp1252", errors="replace")


def html_md(html: str) -> str:
    from markitdown.converters import HtmlConverter
    return HtmlConverter().convert_string(html).markdown


def html(doc, data: bytes):
    testo = decodifica(data)
    t = re.search(r"<title[^>]*>(.*?)</title>", testo, re.I | re.S)
    if t:
        doc.meta["titolo"] = " ".join(t[1].split())
    doc.pagine.append(html_md(testo))
    doc.info_pagine.append({"metodo": "struttura"})


def epub(doc, data: bytes):
    from markitdown import StreamInfo
    from markitdown.converters import EpubConverter
    md = EpubConverter().convert(io.BytesIO(data), StreamInfo(extension=".epub")).markdown
    md = md.replace("**Title:**", "**Titolo:**").replace("**Authors:**", "**Autori:**")
    doc.pagine.append(md)
    doc.info_pagine.append({"metodo": "struttura"})


def codice(doc, data: bytes, t: str):
    testo = decodifica(data)
    if t == "ipynb":
        return notebook(doc, json.loads(testo))
    if t == "json":
        try:
            testo = json.dumps(json.loads(testo), indent=2, ensure_ascii=False)
        except ValueError:
            pass
    doc.pagine.append(f"```{t}\n{testo.strip()}\n```")
    doc.info_pagine.append({"metodo": "testo"})


def notebook(doc, nb: dict):
    import base64
    lingua = nb.get("metadata", {}).get("language_info", {}).get("name", "python")
    parti = []
    for c in nb.get("cells", []):
        src = "".join(c.get("source", []))
        if c.get("cell_type") == "markdown":
            parti.append(src)
        elif c.get("cell_type") == "code":
            parti.append(f"```{lingua}\n{src}\n```")
            for o in c.get("outputs", []):
                dati = o.get("data", {})
                if "image/png" in dati:
                    parti.append(doc.figura(base64.b64decode("".join(dati["image/png"])), 1, "Risultato"))
                elif o.get("text") or "text/plain" in dati:
                    out = "".join(o.get("text") or dati.get("text/plain"))
                    parti.append(f"```text\n{out.rstrip()}\n```")
    doc.pagine.append("\n\n".join(parti))
    doc.info_pagine.append({"metodo": "struttura"})


# ---------------------------------------------------------------- archivi e allegati
def _limite(doc) -> int:
    radice = getattr(doc, "radice", doc)
    return doc.cfg.max_estratto_mb * 1024 * 1024 - radice.estratto


def _conta(doc, n: int):
    getattr(doc, "radice", doc).estratto += n


def elementi_archivio(doc, data: bytes, t: str, nome: str):
    """-> (nome, dati | None, motivo) uno alla volta, senza scrivere niente su disco."""
    if t == "zip":
        z = zipfile.ZipFile(io.BytesIO(data))
        for it in z.infolist():
            n = it.filename
            if not it.flag_bits & 0x800:            # nomi senza UTF-8: Windows italiano (cp850)
                try:
                    n = n.encode("cp437").decode("cp850")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    pass
            if it.is_dir() or SCARTI.search(n):
                continue
            if it.flag_bits & 0x1:
                yield n, None, "protetto da password"
                continue
            if it.file_size > _limite(doc):
                yield n, None, f"oltre il limite di estrazione ({doc.cfg.max_estratto_mb} MB)"
                continue
            if it.file_size > 100 * 2**20 and it.file_size > 1000 * max(1, it.compress_size):
                yield n, None, "compressione sospetta (archivio 'bomba')"
                continue
            dati = z.read(it)
            _conta(doc, len(dati))
            yield n, dati, None
        return
    if t == "7z":
        try:
            import py7zr
        except ImportError:
            raise D.Errore("archivio 7z: serve 'pip install py7zr'")
        with py7zr.SevenZipFile(io.BytesIO(data)) as z:
            for n, f in (z.readall() or {}).items():
                if SCARTI.search(n):
                    continue
                dati = f.read()
                _conta(doc, len(dati))
                yield n, dati, None
        return
    try:
        tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
    except tarfile.TarError:
        tf = None
    if tf is None:                                   # .gz/.bz2/.xz di un solo file
        import bz2
        import lzma
        apri = {"gz": gzip.open, "bz2": bz2.open, "xz": lzma.open}[t]
        with apri(io.BytesIO(data)) as f:
            dati = f.read(_limite(doc) + 1)
        if len(dati) > _limite(doc):
            yield nome, None, f"oltre il limite di estrazione ({doc.cfg.max_estratto_mb} MB)"
            return
        _conta(doc, len(dati))
        yield re.sub(r"\.(gz|bz2|xz)$", "", nome, flags=re.I) or "contenuto", dati, None
        return
    with tf:
        for m in tf:
            if not m.isfile() or SCARTI.search(m.name):
                continue
            if m.size > _limite(doc):
                yield m.name, None, f"oltre il limite di estrazione ({doc.cfg.max_estratto_mb} MB)"
                continue
            dati = tf.extractfile(m).read()
            _conta(doc, len(dati))
            yield m.name, dati, None


def archivio(doc, data: bytes, t: str):
    try:
        contenuti(doc, elementi_archivio(doc, data, t, doc.opz.nome), "File")
    except (zipfile.BadZipFile, tarfile.TarError, EOFError, OSError) as e:
        raise D.Errore(f"archivio {t} non leggibile: {e}")
    if not doc.pagine:
        doc.pagine.append("*Archivio vuoto.*")
        doc.info_pagine.append({"metodo": "contenuto"})


def contenuti(doc, elementi, etichetta: str):
    """Ogni file contenuto e' convertito come un documento a se' e diventa una sezione."""
    for n, (nome, dati, motivo) in enumerate(elementi, 1):
        voce = {"nome": nome}
        if dati is not None and doc.profondita >= doc.cfg.profondita:
            dati, motivo = None, f"oltre {doc.cfg.profondita} livelli di archivi annidati"
        if dati is not None and not dati:
            dati, motivo = None, "file vuoto"
        corpo = ""
        if dati is None:
            voce["errore"] = motivo
        else:
            f = doc.figlio(n)
            try:
                voce["tipo"] = D.riempi(f, dati, nome)
                voce["pagine"] = len(f.pagine)
                doc.unisci(f)
                doc.avvisi += [f"{nome}: {a}" for a in f.avvisi]
                if f.membri:
                    voce["contenuti"] = f.membri
                corpo = "\n\n".join(f"<!-- {etichetta.lower()}:{nome} p:{i} metodo:{inf['metodo']} -->\n\n{m.strip()}"
                                    for i, (m, inf) in enumerate(zip(f.pagine, f.info_pagine), 1))
            except Exception as e:
                voce["errore"] = f"{type(e).__name__}: {e}" if not isinstance(e, D.Errore) else str(e)
        if voce.get("errore"):
            doc.avvisi.append(f"{nome}: non convertito ({voce['errore']})")
            corpo = f"*Non convertito: {voce['errore']}*"
        doc.membri.append(voce)
        doc.pagine.append(f"## {etichetta}: {nome}\n\n{corpo}")
        doc.info_pagine.append({"metodo": "contenuto", "nome": nome, "tipo": voce.get("tipo")})


# ---------------------------------------------------------------- posta
def _intestazione(campi) -> str:
    righe = ["| | |", "|---|---|"]
    righe += [f"| **{k}** | {str(v).replace('|', '/').replace(chr(10), ' ')} |" for k, v in campi if v]
    return "\n".join(righe)


def posta(doc, data: bytes, t: str):
    if t == "msg":
        return _msg(doc, data)
    m = email.message_from_bytes(data, policy=policy.default)
    corpo_html = m.get_body(preferencelist=("html",))
    corpo_txt = m.get_body(preferencelist=("plain",))
    if corpo_html is not None:
        corpo = html_md(corpo_html.get_content())
    elif corpo_txt is not None:
        corpo = corpo_txt.get_content()
    else:
        corpo = ""
    allegati = []
    for p in m.iter_attachments():
        nome = p.get_filename() or f"allegato-{len(allegati) + 1}"
        if p.get_content_type() == "message/rfc822":
            interno = p.get_payload(0) if p.is_multipart() else p.get_content()
            dati = interno.as_bytes() if hasattr(interno, "as_bytes") else bytes(interno)
            nome = p.get_filename() or "messaggio-allegato.eml"
        else:
            dati = p.get_payload(decode=True) or b""
        allegati.append((nome, dati))
    _messaggio(doc, m["subject"], [("Da", m["from"]), ("A", m["to"]), ("Cc", m["cc"]),
                                   ("Data", m["date"]), ("Oggetto", m["subject"])], corpo, allegati)


def _msg(doc, data: bytes):
    try:
        import extract_msg
    except ImportError:
        raise D.Errore("posta di Outlook (.msg): serve 'pip install extract-msg'")
    m = extract_msg.openMsg(data)
    try:
        corpo = ""
        try:
            hb = m.htmlBody
            if hb:
                corpo = html_md(hb.decode("utf-8", "replace") if isinstance(hb, bytes) else hb)
        except Exception:
            corpo = ""
        corpo = corpo or (m.body or "")
        allegati = []
        for a in m.attachments:
            nome = getattr(a, "longFilename", None) or getattr(a, "shortFilename", None) or \
                f"allegato-{len(allegati) + 1}"
            dati = a.data
            if not isinstance(dati, (bytes, bytearray)):           # messaggio allegato
                try:
                    dati = dati.exportBytes()
                    nome = nome if nome.lower().endswith(".msg") else f"{nome}.msg"
                except Exception:
                    continue
            allegati.append((nome, bytes(dati or b"")))
        _messaggio(doc, m.subject, [("Da", m.sender), ("A", m.to), ("Cc", m.cc),
                                    ("Data", m.date), ("Oggetto", m.subject)], corpo, allegati)
    finally:
        m.close()


def _messaggio(doc, oggetto, campi, corpo: str, allegati):
    doc.meta["oggetto"] = str(oggetto or "")
    da = dict(campi).get("Da")
    if da:
        doc.meta["mittente"] = str(da)
    testa = f"# {oggetto or '(senza oggetto)'}\n\n{_intestazione(campi)}"
    if allegati:
        testa += "\n\n**Allegati:** " + ", ".join(n for n, _ in allegati)
    doc.pagine.append(f"{testa}\n\n{corpo.strip()}")
    doc.info_pagine.append({"metodo": "posta"})
    # i loghi delle firme (immagini piccolissime) non sono allegati veri
    allegati = [(n, d) for n, d in allegati
                if not (D.riconosci_tipo(d, n) in ("png", "jpg", "gif", "bmp") and len(d) < 5000)]
    contenuti(doc, ((n, d, None) for n, d in allegati), "Allegato")
