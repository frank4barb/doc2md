"""media.py - audio e video parlati -> Markdown con i tempi, tutto in locale.

  1. ffmpeg estrae la traccia audio (anche dai video) in WAV 16 kHz mono;
  2. l'audio si divide in blocchi di circa 10 minuti, tagliati nel punto piu' silenzioso;
  3. ogni blocco va a whisper.cpp attraverso llama-swap (/v1/audio/transcriptions, modello
     "whisper"), che lo carica alla prima richiesta e lo libera dopo il ttl come gli altri;
  4. i segmenti diventano paragrafi con il tempo d'inizio; le frasi inventate tipiche di
     whisper sui silenzi ("Sottotitoli a cura di...") si scartano.

Video da un link (YouTube e gli altri siti di yt-dlp): si scarica solo l'audio, in una cartella
in RAM cancellata prima di restituire il risultato; titolo, canale, data e capitoli dal sito.
Nessun dato va in rete: dal sito si legge soltanto.
"""
import io
import re
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np
import requests

D = None    # il modulo doc2md, collegato da doc2md all'avvio

SR = 16000
BLOCCO = 600            # secondi per richiesta a whisper
CERCA = 20              # secondi prima del limite in cui cercare il silenzio per tagliare
ALLUCINAZIONI = re.compile(
    r"amara\.org|sottotitoli (creati|a cura|realizzati|e revisione)|qtss|"
    r"grazie (per|a tutti per) (la visione|l'ascolto|aver guardato)|iscriviti al canale|"
    r"thanks? (you )?for watching|subtitles by|transcribed by|^\W*[\[(]?\s*(musica|music|applausi|"
    r"applause|silenzio|silence|risate|laughter)\s*[\])]?\W*$", re.I)


def ore(s: float) -> str:
    s = int(round(s))
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise D.Errore("ffmpeg non installato: serve per audio e video (sudo apt install ffmpeg)")
    return exe


def in_pcm(data: bytes, tipo: str) -> np.ndarray:
    """Qualunque audio o video -> campioni 16 kHz mono. Il file passa da una cartella in RAM
    (ffmpeg ha bisogno di poter saltare nel file, per esempio negli mp4) cancellata subito."""
    with D.cartella_ram("media") as d:
        src, out = Path(d) / f"ingresso.{tipo}", Path(d) / "audio.wav"
        src.write_bytes(data)
        r = subprocess.run([_ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(src),
                            "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(SR), "-c:a", "pcm_s16le", str(out)],
                           capture_output=True, timeout=7200)
        err = r.stderr.decode(errors="replace")
        if r.returncode or not out.is_file():
            if "matches no streams" in err:
                raise D.Errore("il file non contiene una traccia audio")
            raise D.Errore(f"audio non leggibile: {err.strip()[-300:]}")
        with wave.open(str(out)) as w:
            return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).copy()


def blocchi(pcm: np.ndarray):
    """-> (inizio, fine) in campioni, tagliando nel quarto di secondo piu' silenzioso."""
    i, n = 0, len(pcm)
    while i < n:
        fine = i + BLOCCO * SR
        if fine >= n - 120 * SR:                 # un ultimo pezzo sotto i 2 minuti si unisce al precedente
            yield i, n
            return
        w = pcm[fine - CERCA * SR:fine].astype(np.float32)
        q = SR // 4
        energia = (w[:len(w) // q * q].reshape(-1, q) ** 2).mean(1)
        taglio = fine - CERCA * SR + int(energia.argmin()) * q + q // 2
        yield i, taglio
        i = taglio


def wav_bytes(pcm: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def whisper(cfg, wav: bytes, lingua: str | None, contesto: str | None = None) -> dict:
    """contesto: testo iniziale per whisper (titolo, nomi, termini): ne copia l'ortografia
    e la punteggiatura. Funziona solo se whisper-server non ha -mc 0 (vedi llama-swap-doc2md.yaml)."""
    url = cfg.llm_url.rstrip("/") + "/audio/transcriptions"
    campi = {"model": cfg.whisper, "response_format": "verbose_json",
             "temperature": "0", "language": lingua or "auto"}
    if contesto:
        campi["prompt"] = contesto
    try:
        r = requests.post(url, files={"file": ("audio.wav", wav, "audio/wav")}, data=campi,
                          timeout=max(cfg.timeout, 1800))
    except requests.ConnectionError as e:
        raise D.Errore(f"whisper non raggiungibile su {url}: {e}")
    if r.status_code != 200:
        raise D.Errore(f"whisper: HTTP {r.status_code} {r.text[:300]} "
                       f"(c'e' la voce '{cfg.whisper}' in llama-swap?)")
    try:
        return r.json()
    except ValueError:
        return {"text": r.text}


def segmenti(res: dict, scarto: float) -> list:
    out = []
    for s in res.get("segments") or []:
        a = s.get("start", s.get("t0", 0) / 100 if "t0" in s else 0)
        b = s.get("end", s.get("t1", 0) / 100 if "t1" in s else a)
        # lo spazio iniziale si conserva: whisper.cpp a volte chiude un segmento a meta' parola
        # ("sintet" + "iche"); un segmento che NON comincia con uno spazio continua la parola
        out.append((float(a) + scarto, float(b) + scarto, (s.get("text") or "").rstrip(), s.get("avg_logprob")))
    if not out and (res.get("text") or "").strip():
        out.append((scarto, scarto, res["text"].strip(), None))
    return out


# Pezzi da riascoltare. Misurato sulle prove: i tratti di audio difficile stanno sotto -0,3;
# la frase inventata ("...un po' di t" / "essuto naturale.") stava a -0,29 e -0,23 ma durava
# 0,1 s per 16 caratteri: il parlato vero sta sotto i 20 caratteri al secondo.
SOGLIA_LOGPROB = -0.25
MAX_CAR_SECONDO = 25


def dubbio(seg) -> str | None:
    a, b, t, lp = seg
    if lp is not None and lp < SOGLIA_LOGPROB:
        return f"punteggio {lp:.2f}"
    n = len(t.strip())
    if n >= 8 and b > a and n / (b - a) > MAX_CAR_SECONDO:
        return f"troppo veloce ({n / (b - a):.0f} caratteri/s)"
    if n >= 8 and b <= a:
        return "durata nulla"
    return None


def pulisci(segs: list) -> list:
    """Via le frasi inventate sui silenzi e le ripetizioni a ciclo (difetto noto di whisper)."""
    out = []
    for seg in segs:
        t = seg[2]
        if not t.strip() or ALLUCINAZIONI.search(t.strip()):
            continue
        if len(out) >= 2 and out[-1][2].strip() == t.strip() and out[-2][2].strip() == t.strip():
            continue
        out.append(seg)
    return out


def paragrafi(segs: list, capitoli=None) -> str:
    """Segmenti -> paragrafi con il tempo d'inizio; nuovo paragrafo dopo una pausa o ~45 s.
    In fondo al paragrafo, un commento per ogni pezzo da riascoltare: <!-- verifica: whisper ... -->"""
    capitoli = sorted(capitoli or [], key=lambda c: c.get("start_time", 0))
    parti, cur, inizio, fine_prec, note = [], [], None, None, []
    def chiudi():
        if cur:
            parti.append(f"**[{ore(inizio)}]** " + " ".join(cur) +
                         "".join(f" <!-- verifica: whisper {ore(x)} {m} -->" for x, m in note))
    for seg in segs:
        a, b, t = seg[:3]
        m = dubbio(seg) if len(seg) > 3 else None
        if cur and not t[:1].isspace():      # continuazione della parola precedente: si attacca
            cur[-1] += t
            fine_prec = b
            if m:
                note.append((a, m))
            continue
        t = t.strip()
        while capitoli and capitoli[0].get("start_time", 0) <= a + 0.5:
            chiudi()
            cur, inizio, note = [], None, []
            parti.append(f"### {capitoli.pop(0).get('title', '')}")
        nuovo = inizio is None or (a - fine_prec) >= 1.5 or (a - inizio) >= 45 or \
            ((a - inizio) >= 20 and cur and cur[-1][-1:] in ".?!")
        if nuovo:
            chiudi()
            cur, inizio, note = [], a, []
        cur.append(t)
        if m:
            note.append((a, m))
        fine_prec = b
    chiudi()
    return "\n\n".join(parti)


# Testo iniziale di base per lingua: con un contesto attivo (-mc 64) whisper imita lo stile del
# testo che "ha gia' scritto"; senza nessun esempio punteggiato, un parlato spontaneo esce senza
# punti ne' maiuscole e lo stile si trascina (misurato: 2,8 segni ogni 100 parole contro 15,4).
BASE = {
    "it": "Trascrizione in italiano, con la punteggiatura e le maiuscole.",
    "en": "Transcription in English, with punctuation and capital letters.",
    "fr": "Transcription en français, avec la ponctuation et les majuscules.",
    "de": "Transkription auf Deutsch, mit Satzzeichen und Großschreibung.",
    "es": "Transcripción en español, con puntuación y mayúsculas.",
}
NOMI_LINGUA = {"italian": "it", "english": "en", "french": "fr", "german": "de", "spanish": "es"}
MAX_CONTESTO = 240      # circa 64 token: whisper-server -mc 64 tiene solo gli ultimi


def contesto_di(*parti) -> str | None:
    """Testo iniziale per whisper: frasi chiuse dal punto (whisper ne imita anche la punteggiatura).
    whisper usa la FINE del testo iniziale: le parti piu' importanti vanno per ultime."""
    out = []
    for p in parti:
        p = " ".join((p or "").split())
        if p:
            out.append(p if p[-1] in ".?!" else p + ".")
    t = " ".join(out)
    if len(t) > MAX_CONTESTO:
        t = t[-MAX_CONTESTO:]
        t = t[t.find(" ") + 1:]                  # niente mezza parola all'inizio
    return t or None


def coda(segs: list, n: int = 120) -> str:
    """Ultima frase (o ultime parole) del blocco precedente: il blocco dopo prosegue nello stesso stile."""
    t = "".join(x[2] for x in segs[-6:]).strip()
    if len(t) <= n:
        return t
    t = t[-n:]
    return t[t.find(" ") + 1:]


def trascrivi(doc, data: bytes, tipo: str, capitoli=None, contesto: str | None = None):
    pcm = in_pcm(data, tipo)
    durata = len(pcm) / SR
    doc.meta["durata"] = ore(durata)
    if durata < 0.5:
        raise D.Errore("audio vuoto")
    lingue, tutti = [], []
    lingua = doc.opz.lingua                     # senza lingua: si scopre dal primo blocco
    for k, (a, b) in enumerate(blocchi(pcm), 1):
        testo_iniziale = contesto_di(BASE.get(lingua), coda(tutti) if tutti else None,
                                     getattr(doc.opz, "contesto", None), contesto)
        res = whisper(doc.cfg, wav_bytes(pcm[a:b]), doc.opz.lingua, testo_iniziale)
        if res.get("language"):
            lingue.append(res["language"])
            lingua = lingua or NOMI_LINGUA.get(res["language"].lower(), res["language"][:2].lower())
        segs = pulisci(segmenti(res, a / SR))
        tutti += segs
        doc.pagine.append(None)                 # il testo si compone alla fine (capitoli a cavallo)
        doc.info_pagine.append({"metodo": "trascrizione", "da": ore(a / SR), "a": ore(b / SR),
                                "_segmenti": segs})
    del pcm
    if lingue:
        doc.meta["lingua"] = max(set(lingue), key=lingue.count)
    caps = list(capitoli or [])
    for i, info in enumerate(doc.info_pagine):
        if info.get("metodo") != "trascrizione" or doc.pagine[i] is not None:
            continue
        segs = info.pop("_segmenti")
        fine = segs[-1][0] if segs else 0
        mie = [c for c in caps if c.get("start_time", 0) <= fine + 0.5]
        caps = [c for c in caps if c not in mie]
        doc.pagine[i] = paragrafi(segs, mie) or "*(nessun parlato riconosciuto in questo tratto)*"
    if not tutti:
        doc.avvisi.append("nessun parlato riconosciuto")
    doc.meta["whisper_da_verificare"] = sum(dubbio(x) is not None for x in tutti)


# ---------------------------------------------------------------- video da un link
def e_video(url: str) -> bool:
    """Il link e' di un sito video conosciuto da yt-dlp (YouTube, Vimeo, RAI Play...)?"""
    try:
        from yt_dlp.extractor import gen_extractor_classes
    except ImportError:
        return bool(re.search(r"(youtube\.com|youtu\.be)/", url))
    for ie in gen_extractor_classes():
        if ie.IE_NAME not in ("generic",) and ie.suitable(url):
            return True
    return False


def scarica_audio(url: str):
    """-> (audio, info) dal sito video; il file passa solo dalla RAM."""
    try:
        import yt_dlp
    except ImportError:
        raise D.Errore("video da link: serve 'pip install \"yt-dlp[default]\"'")
    with D.cartella_ram("video") as d:
        opzioni = {
            "format": "bestaudio/best", "outtmpl": f"{d}/media.%(ext)s", "paths": {"home": d, "temp": d},
            "quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
            "cachedir": False, "socket_timeout": 30, "retries": 3, "overwrites": True,
            "js_runtimes": {"deno": {}, "node": {}},        # YouTube: serve un motore JavaScript
            "max_filesize": 4 * 2**30, "logger": _Muto(),
        }
        try:
            with yt_dlp.YoutubeDL(opzioni) as y:
                info = y.extract_info(url, download=True)
        except Exception as e:
            raise D.Errore(f"video non scaricabile: {str(e)[-300:]}")
        file = [p for p in Path(d).iterdir() if p.is_file() and not p.name.endswith(".part")]
        if not file:
            raise D.Errore("video non scaricabile: nessun file ricevuto")
        dati = max(file, key=lambda p: p.stat().st_size).read_bytes()
    return dati, info


def da_link(doc, url: str):
    dati, info = scarica_audio(url)
    # da qui la cartella in RAM non esiste piu': l'audio e' solo in memoria
    data = info.get("upload_date") or ""
    doc.meta.update({
        "titolo": info.get("title"), "canale": info.get("channel") or info.get("uploader"),
        "pubblicato": f"{data[:4]}-{data[4:6]}-{data[6:]}" if len(data) == 8 else None,
        "link": info.get("webpage_url") or url, "sito": info.get("extractor_key"),
    })
    descr = (info.get("description") or "").strip()
    if info.get("title"):
        doc.pagine.append(f"# {info['title']}" + (f"\n\n## Descrizione\n\n{descr}" if descr else ""))
        doc.info_pagine.append({"metodo": "metadati"})
    tipo = D.riconosci_tipo(dati) or "webm"
    trascrivi(doc, dati, tipo if tipo != "sconosciuto" else (info.get("ext") or "webm"),
              capitoli=info.get("chapters"), contesto=info.get("title"))


class _Muto:
    def debug(self, m):
        pass

    def info(self, m):
        pass

    def warning(self, m):
        pass

    def error(self, m):
        pass
