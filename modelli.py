"""modelli.py - chiamate ai modelli e al divisore di pagina usate da doc2md.

  ask            richiesta a un modello visivo compatibile OpenAI (llama-swap)
  clean          pulizia della risposta (marcatori, blocchi di codice, OTSL -> HTML)
  DocLayout      PP-DocLayoutV3 in ONNX (divisore della pagina in blocchi)
  jev_score      voto 0-1 di MiniCPM-OCR-Jev su una trascrizione
  dedup, mark_nested, _area, _inter   geometria dei riquadri del divisore

Estratto dagli script di confronto dei modelli usati per scegliere la pipeline: doc2md non
dipende piu' da quegli script.
"""
import base64
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

import requests
from PIL import Image

def otsl_to_html(text: str) -> str:
    """Converte le tabelle OTSL (<otsl>...</otsl>, usate da MiniCPM-OCR e
    PaddleOCR-VL) in HTML con colspan/rowspan. Il resto del testo resta invariato."""
    def one(m):
        body = m.group(1)
        rows, cur = [], []
        for tok, val in re.findall(r"<(fcel|ecel|lcel|ucel|xcel|nl)>([^<]*)", body):
            if tok == "nl":
                rows.append(cur)
                cur = []
            else:
                cur.append((tok, val.strip()))
        if cur:
            rows.append(cur)
        ncol = max((len(r) for r in rows), default=0)
        grid = [r + [("ecel", "")] * (ncol - len(r)) for r in rows]
        html = ["<table>"]
        for i, r in enumerate(grid):
            html.append("<tr>")
            for j, (tok, val) in enumerate(r):
                if tok not in ("fcel", "ecel"):
                    continue                      # cella fusa con una precedente
                cs = 1
                while j + cs < ncol and grid[i][j + cs][0] in ("lcel", "xcel"):
                    cs += 1
                rs = 1
                while i + rs < len(grid) and grid[i + rs][j][0] in ("ucel", "xcel"):
                    rs += 1
                attr = (f' colspan="{cs}"' if cs > 1 else "") + (f' rowspan="{rs}"' if rs > 1 else "")
                html.append(f"<td{attr}>{val}</td>")
            html.append("</tr>")
        html.append("</table>")
        return "".join(html)
    text = re.sub(r"<otsl>(.*?)(?:</otsl>|$)", one, text, flags=re.S)
    if "<otsl>" not in text and re.search(r"<(fcel|ecel)>", text) and "<table" not in text:
        text = one(re.match(r"(.*)", text, re.S))   # OTSL senza involucro
    return text

END_MARK = re.compile(r"<[|\uFF5C]\s*end[^>]{0,40}>\s*$")   # marcatore di fine testo


def ask(base_url, model, prompt, img, mime, max_tokens, timeout):
    content = [{"type": "image_url", "image_url": {
        "url": f"data:{mime};base64,{base64.b64encode(img).decode()}"}}]
    if prompt:
        content.append({"type": "text", "text": prompt})
    body = {"model": model, "temperature": 0, "max_tokens": max_tokens,
            "cache_prompt": False,
            "messages": [{"role": "user", "content": content}]}
    t0 = time.perf_counter()
    r = requests.post(f"{base_url}/chat/completions", json=body, timeout=timeout)
    secs = time.perf_counter() - t0
    r.raise_for_status()
    data = r.json()
    choice = data["choices"][0]
    usage = dict(data.get("usage", {}))
    tm = data.get("timings") or {}          # tempi interni di llama-server, se presenti
    usage["lettura_s"] = round(tm.get("prompt_ms", 0) / 1000, 2)
    usage["scrittura_s"] = round(tm.get("predicted_ms", 0) / 1000, 2)
    return (choice["message"].get("content") or "", secs, usage,
            choice.get("finish_reason", ""))

BLOCK_RE = re.compile(
    r"<BLOCK>\s*(\w+)\s*(?:<LEVEL\d+>)?\s*\[\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\]"
    r"\s*<FORK>(.*?)(?=<BLOCK>|<PAGE>|\Z)", re.S)


def clean(text: str, cat: str) -> str:
    """Toglie involucri: marcatori dei modelli, blocchi di codice, delimitatori di formula."""
    text = END_MARK.sub("", text or "").strip()
    if "<BLOCK>" in text:                          # Unlimited-OCR-2 su un ritaglio
        parts = [c for *_, c in BLOCK_RE.findall(text)]
        text = "\n".join(p.strip() for p in parts if p.strip())
    text = text.replace("<PAGE>", "").strip()
    text = re.sub(r"^```\w*\s*|\s*```$", "", text).strip()
    text = otsl_to_html(text)
    if cat == "formula":
        text = re.sub(r"^(\$\$|\\\[|\\\(|\$)\s*|\s*(\$\$|\\\]|\\\)|\$)$", "", text).strip()
    return text

def jev_health(url, timeout=10):
    try:
        r = requests.get(f"{url}/healthz", timeout=timeout)
        return r.ok and r.json().get("status") == "ok", r.text[:300]
    except Exception as e:
        return False, str(e)


def jev_score(url, items, timeout):
    """items: [{case_id, image(b64), ocr_text, category}] -> {case_id: (verdetto, voto)}"""
    r = requests.post(f"{url}/v1/rm/score", json={"input_form": "ternary", "items": items},
                      timeout=timeout)
    r.raise_for_status()
    return {x["case_id"]: (x.get("verdict", "?"), x.get("prob_yes")) for x in r.json()["results"]}

class DocLayout:
    """PP-DocLayoutV3 in ONNX. Usa il modulo pp_doclayout_v3_onnx.py scaricato con
    il modello; se manca o non si carica, decodifica da se' le uscite grezze."""

    def __init__(self, folder: Path, threshold: float):
        self.folder, self.thr, self.helper, self.session = folder, threshold, None, None
        onnx = folder / "pp_doclayoutv3.onnx"
        if not onnx.is_file():
            raise SystemExit(f"Modello di layout non trovato: {onnx}")
        mod = folder / "pp_doclayout_v3_onnx.py"
        try:
            spec = importlib.util.spec_from_file_location("pp_doclayout_v3_onnx", mod)
            m = importlib.util.module_from_spec(spec)
            sys.modules["pp_doclayout_v3_onnx"] = m       # serve a dataclass e simili
            spec.loader.exec_module(m)
            self.helper = m.PPDocLayoutV3ONNX(str(onnx), device="cpu")
            self.mode = "modulo del modello"
        except Exception as e:
            import onnxruntime as ort
            self.session = ort.InferenceSession(str(onnx), providers=["CPUExecutionProvider"])
            labels = json.loads((folder / "labels.json").read_text(encoding="utf-8"))
            labels = labels.get("id2label", labels)
            self.labels = {int(k): v for k, v in labels.items()}
            self.mode = f"decodifica interna ({type(e).__name__}: {e})"

    def detect(self, png: bytes):
        """-> lista di dict {label, score, box[x0,y0,x1,y1] in pixel, order}"""
        img = Image.open(io.BytesIO(png)).convert("RGB")
        W, H = img.size
        dets = self._helper(png, W, H) if self.helper else self._raw(img, W, H)
        dets = [d for d in dets if d["score"] >= self.thr]
        if all(d.get("order") is not None for d in dets):
            dets.sort(key=lambda d: d["order"])
        else:
            dets.sort(key=lambda d: (round(d["box"][1] / 20), d["box"][0]))
        return dets

    def _helper(self, png, W, H):
        # in RAM (/dev/shm): la pagina del documento non tocca il disco
        fd, path = tempfile.mkstemp(suffix=".png", dir="/dev/shm" if os.path.isdir("/dev/shm") else None)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(png)
            try:
                res = self.helper.predict(path, threshold=self.thr)
            except TypeError:
                res = self.helper.predict(path)
        finally:
            os.unlink(path)
        out = []
        for r in res:
            box = r.get("box") or r.get("bbox")
            if box and isinstance(box[0], (list, tuple)):          # poligono -> riquadro
                xs, ys = [p[0] for p in box], [p[1] for p in box]
                box = [min(xs), min(ys), max(xs), max(ys)]
            box = [float(v) for v in box[:4]]
            if max(box) <= 1.5:                                     # coordinate normalizzate
                box = [box[0] * W, box[1] * H, box[2] * W, box[3] * H]
            out.append({"label": str(r.get("label")), "score": float(r.get("score", 1)),
                        "box": box, "order": r.get("order")})
        return out

    def _raw(self, img, W, H):
        import numpy as np
        x = np.asarray(img.resize((800, 800), Image.BICUBIC), dtype=np.float32) / 255.0
        x = x.transpose(2, 0, 1)[None]
        outs = self.session.run(None, {"pixel_values": x})
        logits, boxes = outs[0][0], outs[1][0]
        prob = 1 / (1 + np.exp(-logits))
        flat = prob.reshape(-1)
        top = np.argsort(-flat)[:150]
        ncls = prob.shape[1]
        out, seen = [], set()
        for idx in top:
            score = float(flat[idx])
            if score < self.thr:
                break
            q, c = divmod(int(idx), ncls)
            if q in seen:
                continue
            seen.add(q)
            cx, cy, w, h = boxes[q]
            out.append({"label": self.labels.get(c, str(c)), "score": score, "order": None,
                        "box": [(cx - w / 2) * W, (cy - h / 2) * H, (cx + w / 2) * W, (cy + h / 2) * H]})
        return out

def _area(b):
    return max(0, b[2] - b[0]) * max(0, b[3] - b[1])


def _inter(a, b):
    return _area([max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])])


def dedup(regs, iou_max=0.6):
    """Toglie i doppioni: riquadri della stessa categoria molto sovrapposti
    (il modello non applica NMS); tiene quello con punteggio piu' alto."""
    keep = []
    for r in sorted(regs, key=lambda r: -r.get("score", 1)):
        if all(r["categoria"] != k["categoria"] or
               _inter(r["box"], k["box"]) / (_area(r["box"]) + _area(k["box"]) -
                                             _inter(r["box"], k["box"]) or 1) <= iou_max
               for k in keep):
            keep.append(r)
    return [r for r in regs if r in keep]


def mark_nested(regs, frac=0.85):
    """Segna le regioni contenute in un'altra piu' grande (es. formule dentro una
    tabella): ricevono comunque il voto di Jev, ma non entrano nella pagina
    ricomposta, che altrimenti conterrebbe due volte lo stesso testo."""
    for r in regs:
        r["contenuta"] = any(o is not r and _area(o["box"]) > _area(r["box"]) and
                             _inter(r["box"], o["box"]) >= frac * _area(r["box"])
                             for o in regs)
    return regs
