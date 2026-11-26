"""config.py - impostazioni di doc2md lette da doc2md.ini (file di testo, formato INI).

Dove si cerca doc2md.ini, nell'ordine:
  1. il percorso nella variabile DOC2MD_INI, se impostata;
  2. accanto al codice (la cartella di doc2md.py);
  3. ~/.config/doc2md/doc2md.ini.
Se non c'e', valgono i valori predefiniti qui sotto (gli stessi di doc2md.ini.esempio).

Una variabile d'ambiente con lo stesso significato prevale sul file (comoda per prove e per i
servizi avviati da llama-swap): per esempio DOC2MD_URL prevale su [servizi] doc2md_url.
Solo libreria standard: lo usano anche i client leggeri (d2m.py, lotto.py).
"""
import configparser
import os
from pathlib import Path

QUI = Path(__file__).resolve().parent

# sezione, chiave -> (valore predefinito, variabile d'ambiente che lo sostituisce)
PREDEFINITI = {
    ("cartelle", "input"): ("~/ai/data/doc2md/inp", "DOC2MD_INPUT"),
    ("cartelle", "output"): ("~/ai/data/doc2md/out", "DOC2MD_OUTPUT"),
    ("servizi", "llm_url"): ("http://127.0.0.1:8080/v1", "DOC2MD_LLM_URL"),
    ("servizi", "doc2md_url"): ("http://127.0.0.1:8080/upstream/doc2md", "DOC2MD_URL"),
    ("servizi", "jev_url"): ("http://127.0.0.1:8080/upstream/jev-servizio", "DOC2MD_JEV_URL"),
    ("servizi", "token"): ("", "DOC2MD_TOKEN"),
    ("modelli", "layout_dir"): ("~/ai/modelli/phungpx__PP-DocLayoutV3-ONNX", "DOC2MD_LAYOUT_DIR"),
    ("modelli", "testo"): ("paddleocr-vl", "DOC2MD_TESTO"),
    ("modelli", "tabelle"): ("minicpm-ocr", "DOC2MD_TABELLE"),
    ("modelli", "formule"): ("minicpm-ocr", "DOC2MD_FORMULE"),
    ("modelli", "whisper"): ("whisper", "DOC2MD_WHISPER"),
    ("conversione", "immagini"): ("separate", "DOC2MD_IMMAGINI"),
    ("conversione", "lingua"): ("", "DOC2MD_LINGUA"),
    ("conversione", "profondita"): ("3", "DOC2MD_PROFONDITA"),
    ("conversione", "max_estratto_mb"): ("2048", "DOC2MD_MAX_ESTRATTO_MB"),
    ("conversione", "timeout"): ("7200", "DOC2MD_TIMEOUT"),
    ("servizio", "paralleli"): ("1", "DOC2MD_PARALLELI"),
    ("servizio", "max_mb"): ("100", "DOC2MD_MAX_MB"),
    ("servizio", "max_mb_media"): ("2048", "DOC2MD_MAX_MB_MEDIA"),
    ("servizio", "uno_python"): ("/usr/bin/python3", "DOC2MD_UNO_PYTHON"),
    ("rete", "reti_private"): ("no", "DOC2MD_RETI_PRIVATE"),
    ("visore", "host"): ("127.0.0.1", "DOC2MD_VISORE_HOST"),
    ("visore", "porta"): ("8095", "DOC2MD_VISORE_PORTA"),
}


def file_ini() -> Path | None:
    for p in (os.environ.get("DOC2MD_INI"), QUI / "doc2md.ini", Path.home() / ".config/doc2md/doc2md.ini"):
        if p and Path(p).expanduser().is_file():
            return Path(p).expanduser()
    return None


_ini = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";", "#"))
INI = file_ini()
if INI:
    _ini.read(INI, encoding="utf-8")


def valore(sezione: str, chiave: str) -> str:
    predefinito, env = PREDEFINITI.get((sezione, chiave), ("", None))
    if env and os.environ.get(env) not in (None, ""):
        return os.environ[env]
    return _ini.get(sezione, chiave, fallback=predefinito).strip()


def intero(sezione: str, chiave: str) -> int:
    return int(valore(sezione, chiave))


def vero(sezione: str, chiave: str) -> bool:
    """Valore si/no (anche si', yes, true, 1)."""
    return valore(sezione, chiave).lower() in ("si", "sì", "yes", "true", "1", "on")


def cartella(chiave: str) -> Path:
    """input oppure output. Un percorso relativo si intende dentro la home
    (o dentro "dati", se un doc2md.ini di una versione precedente ha ancora quella voce)."""
    p = Path(valore("cartelle", chiave)).expanduser()
    if not p.is_absolute():
        base = _ini.get("cartelle", "dati", fallback="").strip()
        p = (Path(base).expanduser() if base else Path.home()) / p
    return p


def trova(percorso) -> Path:
    """Un file o una cartella com'e' scritto; se non esiste ed e' un percorso relativo, si cerca
    dentro la cartella input (cosi' "python d2m.py referto.pdf" trova inp/referto.pdf)."""
    p = Path(percorso).expanduser()
    if p.exists() or p.is_absolute():
        return p
    q = cartella("input") / p
    return q if q.exists() else p


def radice_lotto(percorso) -> tuple[Path, Path] | None:
    """Il lotto a cui appartiene un file o una cartella dentro la cartella input:
    (radice d'origine, radice d'uscita). L'uscita ricalca input:
      input/referto.pdf            -> lotto di output/          (output/lotto.db, output/img/)
      input/referti/2025/rx.pdf    -> lotto di output/referti/  (output/referti/lotto.db, output/referti/img/)
    e il Markdown va in <radice d'uscita>/<percorso relativo alla radice d'origine>.md.
    None per un percorso fuori da input, o per input stessa (che contiene piu' lotti)."""
    inp = cartella("input").expanduser().resolve()
    p = Path(percorso).expanduser().resolve()
    if p == inp or not p.is_relative_to(inp):
        return None
    primo = p.relative_to(inp).parts[0]
    if len(p.relative_to(inp).parts) == 1 and not p.is_dir():
        return inp, cartella("output")
    if primo == "img":
        raise ValueError(f"{inp / primo}: una cartella di primo livello non puo' chiamarsi 'img' "
                         "(in output e' la cartella delle immagini): rinominala")
    return inp / primo, cartella("output") / primo


def cartelle_visore() -> list[Path]:
    """Le cartelle che il visore lascia esplorare: le stesse input e output."""
    out = []
    for p in (cartella("input"), cartella("output")):
        if p not in out:
            out.append(p)
    return out


def riepilogo() -> dict:
    return {"file": str(INI) if INI else "(nessuno: valori predefiniti)",
            "input": str(cartella("input")),
            "output": str(cartella("output")), "doc2md_url": valore("servizi", "doc2md_url"),
            "llm_url": valore("servizi", "llm_url")}
