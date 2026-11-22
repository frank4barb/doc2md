"""web.py - link -> Markdown: pagine web, video (via media.py), documenti raggiungibili da un link.

Si legge soltanto (GET, niente cookie, niente dati del chiamante): il contenuto e' gia' in rete.
Pagine web: trafilatura estrae l'articolo (titolo, testo, tabelle, link) senza menu, pubblicita'
e pie' di pagina; se l'estrazione e' scarsa si converte la pagina intera. Un link a un PDF, a un
Word, a un'immagine... scarica il file in memoria e lo passa a doc2md come un documento normale.
"""
import base64
import hashlib
import re
from urllib.parse import unquote, urljoin, urlparse

import requests

D = None    # il modulo doc2md, collegato da doc2md all'avvio

AGENTE = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/130.0 Safari/537.36")
MAX_MB = 200


def doc_id_url(url: str) -> str:
    """Il link in base64url (reversibile), o un'impronta se troppo lungo."""
    d = base64.urlsafe_b64encode(url.encode()).rstrip(b"=").decode()
    return d if len(d) <= 255 else "URL-" + hashlib.sha256(url.encode()).hexdigest()[:16].upper()


def scarica(url: str):
    try:
        r = requests.get(url, headers={"User-Agent": AGENTE, "Accept-Language": "it,en;q=0.8"},
                         timeout=(15, 120), stream=True, allow_redirects=True)
    except requests.RequestException as e:
        raise D.Errore(f"link non raggiungibile: {e}")
    if r.status_code >= 400:
        raise D.Errore(f"link: HTTP {r.status_code}")
    dati = bytearray()
    for pezzo in r.iter_content(1 << 16):
        dati += pezzo
        if len(dati) > MAX_MB * 2**20:
            raise D.Errore(f"link: contenuto oltre {MAX_MB} MB")
    return r, bytes(dati)


def nome_file(r, url: str) -> str:
    cd = r.headers.get("Content-Disposition", "")
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd, re.I)
    if m:
        return unquote(m[1])
    return unquote(posix_base(urlparse(r.url or url).path)) or "documento"


def posix_base(p: str) -> str:
    return p.rstrip("/").rsplit("/", 1)[-1]


def leggi_url(doc, url: str) -> str:
    doc.meta["link"] = url
    if D.media.e_video(url):
        D.media.da_link(doc, url)
        return "youtube" if re.search(r"(youtube\.com|youtu\.be)/", url, re.I) else "video"
    r, dati = scarica(url)
    ctype = r.headers.get("Content-Type", "").lower()
    if r.url != url:
        doc.meta["link"] = r.url
    if "html" in ctype or (not ctype.startswith(("application/pdf", "image/", "audio/", "video/"))
                           and D.riconosci_tipo(dati) == "html"):
        pagina(doc, dati, r.url or url)
        return "html"
    return D.riempi(doc, dati, nome_file(r, url))


def assoluti(md: str, base: str) -> str:
    """Link e immagini relativi -> assoluti (il Markdown si legge lontano dal sito)."""
    return re.sub(r"(\]\()(?!https?:|mailto:|tel:|#|data:)([^)\s]+)",
                  lambda m: m[1] + urljoin(base, m[2]), md)


def pagina(doc, dati: bytes, url: str):
    import trafilatura
    html = D.formati.decodifica(dati)
    md = trafilatura.extract(html, url=url, output_format="markdown", include_tables=True,
                             include_links=True, include_images=doc.opz.immagini != "none",
                             include_formatting=True, favor_recall=True) or ""
    meta = trafilatura.extract_metadata(html, default_url=url)
    if len(md) < 200:                                 # pagine-indice, elenchi, siti strani
        intera = D.formati.html_md(html)
        if len(intera) > len(md) + 100:
            md = intera
            doc.avvisi.append("articolo non riconosciuto: convertita la pagina intera")
    if len(md.strip()) < 100 and re.search(r"<script", html, re.I):
        doc.avvisi.append("pagina costruita da JavaScript: il testo e' quasi assente")
    titolo = getattr(meta, "title", None)
    if titolo and not md.lstrip().startswith("#"):
        md = f"# {titolo}\n\n{md}"
    doc.meta.update({"titolo": titolo, "autore": getattr(meta, "author", None),
                     "pubblicato": getattr(meta, "date", None), "sito": getattr(meta, "sitename", None)})
    doc.pagine.append(assoluti(md, url))
    doc.info_pagine.append({"metodo": "web"})
