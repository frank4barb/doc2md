#!/usr/bin/env python3
"""Schema a blocchi in immagine (scansione, png, webp) -> Mermaid.

Stessa idea di schema_geo.py, ma sui pixel:
  1. zone di colore uniforme  -> riquadri (rettangolo, rombo, ellisse, ...)
  2. tratti rimasti fuori     -> linee e frecce; la punta indica la direzione
  3. scritte dentro i riquadri e accanto alle linee -> lettore OCR
  4. Mermaid + controllo; se il controllo non passa: esito "ripiego"

Il lettore OCR legge solo le scritte: la struttura viene dalla geometria.

Uso:
  python schema_raster.py schema.png                      # lettore PaddleOCR-VL via llama-swap
  python schema_raster.py schema.png --ocr tesseract      # prova senza server
  python schema_raster.py schema.png --debug              # disegna cosa ha trovato
"""
import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np

SHAPES = {  # forma -> modello Mermaid
    "rect": '["{}"]', "round": '("{}")', "diamond": '{{"{}"}}', "ellipse": '(["{}"])',
    "para": '[/"{}"/]', "hexagon": '{{{{"{}"}}}}', "doc": '["{}"]',
}
MAX_NODES = 60   # oltre: quasi sempre una foto o un disegno fitto, non uno schema a blocchi
# risposte del lettore quando nel ritaglio non c'e' testo leggibile
NOT_TEXT = re.compile(r"quick brown fox|lazy dog|\bimage\b|blurry|unab[e]?l|no text|\[.*\]", re.I)
LABELS = {"si", "sì", "no", "yes", "y", "n", "vero", "falso", "true", "false", "ok", "ko"}


# ------------------------------------------------------------------ immagine
def load(path, min_side=1100):
    """path: file, oppure bytes dell'immagine (uso dentro doc2md, senza file)."""
    raw = path if isinstance(path, (bytes, bytearray)) else None
    data = np.frombuffer(raw, np.uint8) if raw is not None else np.fromfile(str(path), np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
    if img is None:  # webp/gif non gestiti da questa build di OpenCV
        import io
        from PIL import Image
        src = io.BytesIO(raw) if raw is not None else path
        img = cv2.cvtColor(np.array(Image.open(src).convert("RGB")), cv2.COLOR_RGB2BGR)
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    if img.shape[2] == 4:  # trasparenza su bianco
        a = img[:, :, 3:4].astype(np.float32) / 255
        img = (img[:, :, :3] * a + 255 * (1 - a)).astype(np.uint8)
    scale = max(1.0, min_side / max(img.shape[:2]))
    if scale > 1:
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    return img, scale


def color_regions(img, tol=14):
    """Etichetta le zone di colore uniforme (riempimento per somiglianza con i vicini)."""
    lab = cv2.cvtColor(cv2.bilateralFilter(img, 7, 30, 7), cv2.COLOR_BGR2LAB).astype(np.int16)
    # un pixel e' "piatto" se e' simile a tutti e 4 i vicini: i bordi e le scritte no
    flat = np.ones(lab.shape[:2], bool)
    for dy, dx in ((0, 1), (1, 0), (0, -1), (-1, 0)):
        sh = np.roll(lab, (dy, dx), (0, 1))
        flat &= np.abs(lab - sh).sum(2) <= tol // 2
    # quantizzazione grossolana del colore per separare zone vicine di colore diverso
    q = (lab[:, :, 0] // 12) * 10000 + ((lab[:, :, 1] + 128) // 10) * 100 + (lab[:, :, 2] + 128) // 10
    labels = np.zeros(lab.shape[:2], np.int32)
    nxt = 1
    for val in np.unique(q[flat]):
        m = (flat & (q == val)).astype(np.uint8)
        if m.sum() < 150:
            continue
        n, cc, st, _ = cv2.connectedComponentsWithStats(m, connectivity=4)
        for k in range(1, n):
            if st[k, cv2.CC_STAT_AREA] >= 150:
                labels[cc == k] = nxt
                nxt += 1
    return labels, lab, flat


def merge_regions(labels, lab, tol=12, jumps=(3, 6, 10, 15)):
    """1) unisce zone confinanti di colore quasi uguale (sfumature);
    2) elenca le coppie della stessa tinta separate solo da una scritta: diventeranno
       un riquadro unico solo se insieme formano una figura regolare (find_nodes)."""
    n = labels.max() + 1
    parent = np.arange(n)
    area = np.bincount(labels.ravel(), minlength=n).astype(np.int64)
    edge = np.zeros(n, bool)
    edge[np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]]))] = True
    big = 0.08 * labels.size

    def find(i):
        r = i
        while parent[r] != r:
            r = parent[r]
        while parent[i] != r:
            parent[i], i = r, parent[i]
        return r

    def scan(L, d):
        out = []
        for a, b, la, lb, sl in ((L[:, :-d], L[:, d:], lab[:, :-d], lab[:, d:], 1),
                                 (L[:-d], L[d:], lab[:-d], lab[d:], 0)):
            m = (a != b) & (a > 0) & (b > 0)
            if d > 1:  # in mezzo: pixel non piatti e ben diversi (una scritta)
                h = d // 2
                if sl:
                    lm, mm = lab[:, h:h + a.shape[1]], L[:, h:h + a.shape[1]]
                else:
                    lm, mm = lab[h:h + a.shape[0]], L[h:h + a.shape[0]]
                m &= (np.abs(lm - la).sum(2) > 60) & (mm == 0)
            if not m.any():
                continue
            pa, pb = a[m].astype(np.int64), b[m].astype(np.int64)
            diff = np.abs(la[m] - lb[m]).sum(1)
            key = pa * n + pb
            cnt = np.bincount(key)
            ok = np.bincount(key, weights=(diff <= tol).astype(float))
            strict = np.bincount(key, weights=(diff <= tol // 2).astype(float))
            for k in np.nonzero(cnt)[0]:
                if cnt[k] >= 6 and ok[k] / cnt[k] > 0.7:
                    out.append((k // n, k % n, cnt[k], strict[k] / cnt[k]))
        return out

    for ia, ib, _, st in scan(labels, 1):
        ra, rb = find(ia), find(ib)
        if ra == rb:
            continue
        # verso sfondo/pannelli serve un colore quasi identico: i riquadri chiari
        # (azzurro pallido su bianco) non devono sparire nello sfondo
        if (edge[ra] or edge[rb] or area[ra] > big or area[rb] > big) and st < 0.7:
            continue
        parent[ra] = rb
        area[rb] += area[ra]
        edge[rb] |= edge[ra]
    roots = np.array([find(i) for i in range(n)])
    roots[0] = 0
    merged = roots[labels]
    pairs = {}
    for d in jumps:
        for ia, ib, c, _ in scan(merged, d):
            if edge[ia] or edge[ib] or area[ia] > big or area[ib] > big:
                continue
            key = (min(ia, ib), max(ia, ib))
            pairs[key] = pairs.get(key, 0) + c
    return merged, sorted(pairs, key=pairs.get, reverse=True)


# ------------------------------------------------------------------ riquadri
class Node:
    def __init__(self, i, kind, cnt, mask):
        self.i, self.kind, self.cnt, self.mask, self.text = i, kind, cnt, mask, ""
        self.x, self.y, self.w, self.h = cv2.boundingRect(cnt)
        mo = cv2.moments(cnt)
        self.c = (mo["m10"] / mo["m00"], mo["m01"] / mo["m00"])


def classify(cnt):
    """Forma del riquadro dal suo inviluppo convesso."""
    hull = cv2.convexHull(cnt)
    x, y, w, h = cv2.boundingRect(hull)
    m = np.zeros((h + 2, w + 2), np.uint8)
    cv2.drawContours(m, [hull - (x - 1, y - 1)], -1, 1, -1)
    area = m.sum()
    ext = area / (w * h)
    peri = cv2.arcLength(hull, True)
    ap = cv2.approxPolyDP(hull, 0.03 * peri, True).reshape(-1, 2)
    nv = len(ap)
    # angoli: pieni nel rettangolo, vuoti in ellisse/rombo/capsula
    t = max(2, int(0.12 * min(w, h)))
    corners = sum(int(m[yy, xx]) for xx, yy in ((t, t), (w - t, t), (t, h - t), (w - t, h - t)))
    ell = np.zeros_like(m)
    cv2.ellipse(ell, (w // 2 + 1, h // 2 + 1), (w // 2, h // 2), 0, 0, 360, 1, -1)
    iou_ell = (m & ell).sum() / max(1, (m | ell).sum())
    cx, cy = x + w / 2, y + h / 2
    mids = sum(min(abs(px - cx) / w, abs(py - cy) / h) < 0.12 for px, py in ap)
    if nv == 4 and mids >= 3 and ext < 0.62:
        return "diamond"
    if corners == 4:
        return "rect" if ext > 0.94 else "round"
    if iou_ell > 0.9 or (corners == 0 and ext > 0.8):
        return "ellipse"
    if nv == 4 and ext > 0.6:
        return "para"
    if nv == 6 and ext > 0.65:
        return "hexagon"
    if corners >= 2 and ext > 0.8:
        return "round"
    return "other"


def has_marks(img, filled, fill_px):
    """Dentro il riquadro ci sono scritte? Si guarda lontano dal bordo, cosi' le linee
    che delimitano una zona vuota non contano; servono alcuni segni piccoli (lettere)."""
    _, _, w, h = cv2.boundingRect(filled)
    e = max(2, min(int(0.008 * max(img.shape[:2])), int(0.08 * min(w, h))))
    inner = cv2.erode(filled, np.ones((2 * e + 1, 2 * e + 1), np.uint8)) > 0
    if inner.sum() == 0:
        return False
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB).astype(np.int16)
    sel = fill_px & inner
    ref = np.median(lab[sel], 0) if sel.any() else np.median(lab[inner], 0)
    marks = ((np.abs(lab - ref).sum(2) > 60) & inner).astype(np.uint8)
    n, _, st, _ = cv2.connectedComponentsWithStats(marks, connectivity=8)
    small = sum(1 for k in range(1, n) if 4 <= st[k, cv2.CC_STAT_AREA] and
                max(st[k, cv2.CC_STAT_WIDTH], st[k, cv2.CC_STAT_HEIGHT]) < 0.06 * max(img.shape[:2]))
    return small >= 1


def shape_of(m, kclose):
    """(contorno, inviluppo, riempimento, forma) di una maschera di riempimento."""
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, kclose)
    cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    cnt = max(cnts, key=cv2.contourArea)
    hull = cv2.convexHull(cnt)
    hm = np.zeros_like(m)
    cv2.drawContours(hm, [hull], -1, 1, -1)
    fill = (m & hm).sum() / max(1, hm.sum())
    own = np.zeros_like(m)  # la zona stessa con i suoi buchi interni (le scritte)
    cv2.drawContours(own, [cnt], -1, 1, -1)
    return cnt, hull, hm, fill, classify(cnt), own


def bordered(labf, hm, ref, labels, t=3):
    """Un riquadro bianco su pagina bianca ha un bordo suo tutto intorno: una linea
    sottile (pixel non piatti), non i riempimenti di altri riquadri vicini."""
    k = np.ones((2 * t + 1, 2 * t + 1), np.uint8)
    ring = (cv2.dilate(hm, k) > 0) & ~(cv2.erode(hm, k) > 0)
    d = np.abs(labf[ring] - ref).sum(1) * (labels[ring] == 0)
    # su ogni raggio basta un pixel di bordo: si misura per settori angolari
    ys, xs = np.nonzero(ring)
    cy, cx = ys.mean(), xs.mean()
    ang = ((np.arctan2(ys - cy, xs - cx) + np.pi) / (2 * np.pi) * 36).astype(int) % 36
    hit = np.zeros(36, bool)
    hit[np.unique(ang[d > 30])] = True
    return hit.mean() >= 0.9


def find_nodes(img, labels, pairs=()):
    H, W = labels.shape
    tot = H * W
    border = set(np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]])))
    kc = max(7, int(0.012 * min(H, W)) | 1)
    kclose = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kc, kc))
    # pezzi della stessa tinta separati da scritte: si uniscono solo se insieme
    # formano una figura regolare (due rombi impilati, invece, restano due)
    labels = labels.copy()
    labf = cv2.cvtColor(img, cv2.COLOR_BGR2LAB).astype(np.float32)
    big_id = np.bincount(labels.ravel())[1:].argmax() + 1 if labels.max() else 0
    bg_ref = labf[labels == big_id].mean(0) if big_id else None
    for ia, ib in pairs:
        if ia in border or ib in border:
            continue
        ma, mb = labels == ia, labels == ib
        if not ma.any() or not mb.any():
            continue
        su = shape_of((ma | mb).astype(np.uint8), kclose)
        if su is None or su[4] == "other" or su[3] < 0.75:
            continue
        sa, sb = shape_of(ma.astype(np.uint8), kclose), shape_of(mb.astype(np.uint8), kclose)
        best = max(x[3] for x in (sa, sb) if x is not None)
        if su[3] >= best - 0.08:
            labels[mb] = ia
    cand = []
    for i in np.unique(labels):
        if not i or i in border:
            continue
        m = (labels == i).astype(np.uint8)
        if m.sum() < 0.0008 * tot:
            continue
        sh = shape_of(m, kclose)
        if sh is None:
            continue
        cnt, hull, hm, fill, kind, own = sh
        area = cv2.contourArea(cnt)
        x, y, w, h = cv2.boundingRect(cnt)
        if area < 0.0012 * tot or area > 0.2 * tot:
            continue
        if min(w, h) < 18 or max(w, h) / min(w, h) > 9:
            continue
        if fill < 0.5 or kind == "other":
            continue
        if not has_marks(img, own, labels == i):
            continue  # zona chiusa da linee, senza scritte: non e' un blocco
        if bg_ref is not None and np.abs(labf[labels == i].mean(0) - bg_ref).sum() < 15 \
                and not bordered(labf, hm, bg_ref, labels):
            continue  # stesso colore della pagina e senza bordo proprio: zona tra le linee
        cand.append((area, kind, hull, hm, fill))
    # dal piu' piccolo: un candidato che copre riquadri gia' accettati e' un contenitore
    # (pannello, legenda, zona chiusa da linee) e si scarta; se invece coincide con uno
    # solo (stessa forma con bordo o sfumatura) si tiene il piu' grande
    cand.sort(key=lambda t: t[0])
    keep = []
    for c in cand:
        hits = [k for k in keep if (k[3] & c[3]).sum() > 0.15 * k[3].sum()]
        if not hits:
            keep.append(c)
        elif len(hits) == 1 and hits[0][0] > 0.6 * c[0]:
            keep.remove(hits[0])
            keep.append(c)
    keep = [k[:4] for k in keep]
    keep.sort(key=lambda t: (cv2.boundingRect(t[2])[1] // 40, cv2.boundingRect(t[2])[0]))
    return [Node(n, k, c, f) for n, (_, k, c, f) in enumerate(keep)]


# ------------------------------------------------------------------ debug
def draw(img, nodes, edges=(), comps=(), out=None):
    vis = img.copy()
    for c in comps:
        vis[c["mask"] > 0] = (0, 0, 255) if c["kind"] == "linea" else (255, 0, 255) if c["kind"] == "freccia" else (0, 200, 255)
        for p in c.get("heads", []):
            cv2.circle(vis, (int(p[0]), int(p[1])), 7, (0, 160, 0), 2)
    for n in nodes:
        cv2.drawContours(vis, [n.cnt], -1, (255, 0, 0), 2)
        cv2.putText(vis, f"{n.i}{n.kind[0]}", (n.x, n.y + 14), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0), 2)
    for a, b, *_ in edges:
        pa, pb = nodes[a].c, nodes[b].c
        cv2.arrowedLine(vis, (int(pa[0]), int(pa[1])), (int(pb[0]), int(pb[1])), (0, 140, 0), 2, tipLength=0.04)
    cv2.imwrite(str(out), vis)


# ------------------------------------------------------------------ tratti
def ink_mask(img, nodes, pad=6):
    """Pixel che si staccano dallo sfondo locale e non stanno nei riquadri."""
    H, W = img.shape[:2]
    k = max(31, int(0.045 * max(H, W)) | 1)
    bg = cv2.medianBlur(img, k)
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB).astype(np.int16)
    lbg = cv2.cvtColor(bg, cv2.COLOR_BGR2LAB).astype(np.int16)
    diff = np.abs(lab - lbg).sum(2)
    # le ombre dei riquadri sono grigio chiaro: si tengono i pixel colorati o
    # decisamente piu' scuri dello sfondo, oppure molto diversi
    darker = (lbg[:, :, 0] - lab[:, :, 0]) > 40
    chroma = (np.abs(lab[:, :, 1] - 128) + np.abs(lab[:, :, 2] - 128)) > 14
    ink = ((diff > 45) & (darker | chroma | (diff > 90))).astype(np.uint8)
    nodes_m = np.zeros((H, W), np.uint8)
    for n in nodes:
        cv2.drawContours(nodes_m, [n.cnt], -1, 1, -1)
    nodes_d = cv2.dilate(nodes_m, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * pad + 1, 2 * pad + 1)))
    ink[nodes_d > 0] = 0
    return ink, nodes_m


def node_dist(nodes, shape, reach):
    """Per ogni riquadro: distanza (in pixel) dal suo bordo, in una finestra attorno."""
    H, W = shape
    out = []
    for n in nodes:
        x0, y0 = max(0, n.x - reach), max(0, n.y - reach)
        x1, y1 = min(W, n.x + n.w + reach), min(H, n.y + n.h + reach)
        m = np.ones((y1 - y0, x1 - x0), np.uint8)
        cv2.drawContours(m, [n.cnt - (x0, y0)], -1, 0, -1)
        out.append((x0, y0, cv2.distanceTransform(m, cv2.DIST_L2, 3)))
    return out


def dist_to(nd, i, xs, ys):
    """Distanze dei punti (xs, ys) dal riquadro i (inf fuori finestra)."""
    x0, y0, dt = nd[i]
    xx, yy = xs - x0, ys - y0
    ok = (xx >= 0) & (yy >= 0) & (xx < dt.shape[1]) & (yy < dt.shape[0])
    d = np.full(len(xs), np.inf)
    d[ok] = dt[yy[ok], xx[ok]]
    return d


def nearest_node(nd, p, reach):
    best, bd = None, reach
    for i in range(len(nd)):
        d = dist_to(nd, i, np.array([int(p[0])]), np.array([int(p[1])]))[0]
        if d < bd:
            best, bd = i, d
    return best


def components(ink, nodes, nd, tol):
    """Separa i tratti: linee (sottili, con punte), frecce piene, scritte."""
    n, cc, st, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    hs = [st[k, cv2.CC_STAT_HEIGHT] for k in range(1, n)
          if 12 <= st[k, cv2.CC_STAT_AREA] <= 600 and st[k, cv2.CC_STAT_HEIGHT] < 45]
    char_h = float(np.median(hs)) if hs else 12.0
    dt_all = cv2.distanceTransform(ink, cv2.DIST_L2, 3)
    comps = []
    for k in range(1, n):
        x, y, w, h, area = st[k]
        if area < 12:
            continue
        ys, xs = np.nonzero(cc[y:y + h, x:x + w] == k)
        ys, xs = ys + y, xs + x
        touch = [i for i in range(len(nodes)) if dist_to(nd, i, xs, ys).min() <= tol]
        thick = 2 * float(np.percentile(dt_all[ys, xs], 90))
        long_ = max(w, h) >= 2.5 * char_h
        if not long_:  # corto: e' una freccia solo se sta tra due riquadri
            near = [i for i in range(len(nodes)) if dist_to(nd, i, xs, ys).min() <= 2.5 * tol]
            long_ = len(near) >= 2 and max(w, h) >= 1.2 * char_h
        if not long_ and len(touch) < 2:
            kind = "testo"
        elif thick >= max(9, 0.0075 * max(ink.shape)):
            kind = "freccia"   # freccia piena (blocco)
        else:
            kind = "linea"
        mask = np.zeros_like(ink)
        mask[ys, xs] = 1
        comps.append({"id": k, "kind": kind, "xs": xs, "ys": ys, "box": (x, y, w, h),
                      "thick": thick, "touch": touch, "mask": mask})
    return comps, char_h


def block_arrow(c, skip=None):
    """Freccia piena: asse principale, punta = estremo dove la sagoma e' piu' larga.
    skip: pixel da ignorare nella misura (attaccati a un riquadro: ombra, bordo)."""
    pts = np.stack([c["xs"], c["ys"]], 1).astype(np.float32)
    if skip is not None:  # l'ombra attaccata storcerebbe anche l'asse
        pts = pts[~skip]
        skip = None
    mean = pts.mean(0)
    _, _, vt = np.linalg.svd(pts - mean, full_matrices=False)
    ax = vt[0]
    t = (pts - mean) @ ax
    u = (pts - mean) @ vt[1]
    lo, hi = t.min(), t.max()
    span = hi - lo
    keep = np.ones(len(t), bool) if skip is None else ~skip
    near_lo = (t < lo + 0.35 * span) & keep
    near_hi = (t > hi - 0.35 * span) & keep
    w_lo = np.ptp(u[near_lo]) if near_lo.any() else 0
    w_hi = np.ptp(u[near_hi]) if near_hi.any() else 0
    head, tail = (mean + ax * hi, mean + ax * lo) if w_hi >= w_lo else (mean + ax * lo, mean + ax * hi)
    return tail, head


def thin(m):
    """Scheletro di una maschera 0/1 (Zhang-Suen; usa OpenCV-contrib se c'e')."""
    if hasattr(cv2, "ximgproc"):
        return (cv2.ximgproc.thinning(m * 255) > 0).astype(np.uint8)
    img = np.pad(m.astype(np.uint8), 1)
    while True:
        changed = False
        for step in (0, 1):
            p = [np.roll(np.roll(img, -dy, 0), -dx, 1) for dy, dx in
                 ((-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1))]
            b = sum(p)
            a = sum(((p[k] == 0) & (p[(k + 1) % 8] == 1)).astype(np.uint8) for k in range(8))
            if step == 0:
                c = (p[0] * p[2] * p[4] == 0) & (p[2] * p[4] * p[6] == 0)
            else:
                c = (p[0] * p[2] * p[6] == 0) & (p[0] * p[4] * p[6] == 0)
            rm = (img == 1) & (b >= 2) & (b <= 6) & (a == 1) & c
            if rm.any():
                img[rm] = 0
                changed = True
        if not changed:
            return img[1:-1, 1:-1]


def sk_ends(sk):
    """Estremi dello scheletro: i vicini formano un solo arco (anche con angoli doppi)."""
    img = np.pad(sk.astype(np.uint8), 1)
    p = [np.roll(np.roll(img, -dy, 0), -dx, 1) for dy, dx in
         ((-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1))]
    b = sum(p)
    a = sum(((p[k] == 0) & (p[(k + 1) % 8] == 1)).astype(np.uint8) for k in range(8))
    return ((img == 1) & (b >= 1) & (b <= 3) & (a == 1))[1:-1, 1:-1]


def prune_ends(sk, spur):
    """Estremi veri dello scheletro: i rami corti (le alette della punta) si tolgono;
    se a un incrocio resta un solo ramo lungo, l'incrocio diventa l'estremo."""
    H, W = sk.shape
    pts = set(zip(*np.nonzero(sk)))
    nb = lambda q: [(q[0] + dy, q[1] + dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1)
                    if (dy or dx) and (q[0] + dy, q[1] + dx) in pts]
    ends = list(zip(*np.nonzero(sk_ends(sk))))
    out, short = [], []
    for e in ends:
        path, prev, cur = [e], None, e
        while len(path) <= spur:
            nx = [q for q in nb(cur) if q != prev and q not in path]
            if len(nx) != 1:
                break
            prev, cur = cur, nx[0]
            path.append(cur)
        deg = len([q for q in nb(cur) if q not in path])
        if len(path) <= spur and deg >= 2:     # ramo corto che finisce in un incrocio
            short.append((e, cur, len(path)))
        else:
            out.append(e)
    # incroci vicini = un incrocio solo
    groups = []
    for e, j, n in short:
        for g in groups:
            if abs(g[0][0] - j[0]) <= 3 and abs(g[0][1] - j[1]) <= 3:
                g[1].append((e, n))
                break
        else:
            groups.append((j, [(e, n)]))
    for j, br in groups:
        if len(br) < 2:
            continue                    # un ramo corto isolato: rumore
        if out:
            out.append(j)               # alette della punta: la punta e' l'incrocio
        else:                           # freccia corta: il ramo piu' lungo e' la coda
            br.sort(key=lambda t: -t[1])
            out += [br[0][0], j]
    if not out:
        out = ends
    return np.array([q[1] for q in out]), np.array([q[0] for q in out])


def line_ends(c):
    """Estremi di una linea (anche spezzata ad angolo) e quali hanno la punta."""
    x, y, w, h = c["box"]
    m = c["mask"][y:y + h, x:x + w]
    sk = thin(m)
    ys, xs = np.nonzero(sk_ends(sk))
    # punte: le parti che restano dopo un'apertura piu' larga del tratto
    k = int(round(c["thick"] * 1.6)) + 2
    op = cv2.morphologyEx(m, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    n, _, st, cen = cv2.connectedComponentsWithStats(op)
    heads = [cen[j] for j in range(1, n) if st[j, cv2.CC_STAT_AREA] >= max(6, 0.5 * k * k)]
    # gli estremi vicini (rami corti della punta) sono un estremo solo
    pts, grp = [(px, py) for px, py in zip(xs, ys)], []
    rad = min(max(8, 3 * c["thick"] + 4), 0.4 * max(w, h))
    for p in pts:
        for g in grp:
            if np.hypot(p[0] - g[0][0], p[1] - g[0][1]) <= rad:
                g.append(p)
                break
        else:
            grp.append([p])
    if not grp and len(pts) == 0:  # anello o macchia: estremi lungo l'asse principale
        tail, head = block_arrow(c)
        return [(tuple(tail), False), (tuple(head), False)]
    # punta: attorno all'estremo c'e' piu' "inchiostro" che lungo il resto della linea
    yy, xx = np.nonzero(m)
    dtm = cv2.distanceTransform(m, cv2.DIST_L2, 3)
    w50 = max(1.0, 2 * float(np.median(dtm[yy, xx])))
    r = 2.5 * w50 + 4
    ends = []
    for g in grp:
        p = np.mean(g, 0)
        mass = int(((xx - p[0]) ** 2 + (yy - p[1]) ** 2 <= r * r).sum())
        blob = any(np.hypot(p[0] - hx, p[1] - hy) <= rad + k for hx, hy in heads) or len(g) >= 3
        ends.append([(p[0] + x, p[1] + y), blob, mass])
    base = r * w50  # un tratto dritto che finisce: circa r pixel di lunghezza x spessore
    for e in ends:
        e[1] = e[1] or e[2] >= 1.6 * base
    if len(ends) >= 2:
        lo = min(e[2] for e in ends)
        for e in ends:
            if e[2] < 1.35 * lo and not any(np.hypot(e[0][0] - x - hx, e[0][1] - y - hy) <= rad + k for hx, hy in heads):
                e[1] = False
    if len(ends) == 2 and max(w, h) < 5 * r:
        # freccia corta: la massa attorno ai due estremi si sovrappone; si confronta
        # la larghezza della sagoma nel terzo vicino a ciascun estremo
        a, b = np.array(ends[0][0]) - (x, y), np.array(ends[1][0]) - (x, y)
        ax = b - a
        L = np.linalg.norm(ax)
        if L > 3:
            ax /= L
            t = (np.stack([xx, yy], 1) - a) @ ax
            u = (np.stack([xx, yy], 1) - a) @ np.array([-ax[1], ax[0]])
            wa = np.ptp(u[t < 0.35 * L]) if (t < 0.35 * L).any() else 0
            wb = np.ptp(u[t > 0.65 * L]) if (t > 0.65 * L).any() else 0
            ends[0][1], ends[1][1] = wa > 1.3 * wb, wb > 1.3 * wa
    ends = [(tuple(e[0]), e[1]) for e in ends]
    c["heads"] = [(hx + x, hy + y) for hx, hy in heads]
    return ends


def attach(nd, idmap, p, head, c, reach):
    """Riquadro a cui arriva un estremo: prima lungo la direzione della linea
    (le etichette Si/No spesso stanno tra la linea e il riquadro), poi il piu' vicino."""
    j = nearest_node(nd, p, reach)
    if j is not None:
        return j
    xs, ys = c["xs"], c["ys"]
    d2 = (xs - p[0]) ** 2 + (ys - p[1]) ** 2
    R = max(10.0, 3 * c["thick"] + 6)
    near = (d2 >= (0.4 * R) ** 2) & (d2 <= R * R)  # tratto locale vicino all'estremo
    if near.sum() >= 3:
        q = np.array([xs[near].mean(), ys[near].mean()], float)
        v = np.array(p, float) - q
        if np.linalg.norm(v) > 2:
            v /= np.linalg.norm(v)
            H, W = idmap.shape
            for t in range(1, int(2.5 * reach)):
                x, y = int(round(p[0] + v[0] * t)), int(round(p[1] + v[1] * t))
                if not (0 <= x < W and 0 <= y < H):
                    break
                if idmap[y, x]:
                    return int(idmap[y, x]) - 1
    return nearest_node(nd, p, 1.5 * reach) if not head else None


def dashed_links(comps, nd, reach):
    """Linee tratteggiate: trattini allineati e vicini -> collegamento non orientato."""
    dash = [c for c in comps if c["kind"] == "testo" and c["thick"] <= 7
            and max(c["box"][2:]) >= 2.2 * max(1, min(c["box"][2:]))]
    if len(dash) < 3:
        return []
    cen = np.array([[c["xs"].mean(), c["ys"].mean()] for c in dash])
    ang = []
    for c in dash:
        pts = np.stack([c["xs"], c["ys"]], 1).astype(float)
        pts -= pts.mean(0)
        ang.append(np.linalg.svd(pts, full_matrices=False)[2][0])
    ang = np.array(ang)
    size = np.array([max(c["box"][2:]) for c in dash])
    parent = list(range(len(dash)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i in range(len(dash)):
        for j in range(i + 1, len(dash)):
            v = cen[j] - cen[i]
            d = np.linalg.norm(v)
            if d == 0 or d > 2.2 * max(size[i], size[j]):
                continue
            v /= d
            # stessa direzione dei due trattini e della retta che li unisce
            if abs(ang[i] @ ang[j]) > 0.9 and abs(ang[i] @ v) > 0.9:
                parent[find(i)] = find(j)
    groups = {}
    for i in range(len(dash)):
        groups.setdefault(find(i), []).append(i)
    out = []
    for g in groups.values():
        if len(g) < 3:
            continue
        pts = np.concatenate([np.stack([dash[i]["xs"], dash[i]["ys"]], 1) for i in g]).astype(float)
        m = pts.mean(0)
        ax = np.linalg.svd(pts - m, full_matrices=False)[2][0]
        t = (pts - m) @ ax
        a, b = pts[t.argmin()], pts[t.argmax()]
        na, nb = nearest_node(nd, a, reach), nearest_node(nd, b, reach)
        if na is not None and nb is not None and na != nb:
            out.append((na, nb, False, -1))
            for i in g:
                dash[i]["kind"] = "tratteggio"
    return out


def wire(nodes, comps, nd, tol):
    """Collegamenti: (da, a, orientato, componente)."""
    reach = 4 * tol
    far_tails = []
    H, W = comps[0]["mask"].shape if comps else (1, 1)
    idmap = np.zeros((H, W), np.int32)
    for n in nodes:
        cv2.drawContours(idmap, [n.cnt], -1, n.i + 1, -1)
    edges, loose = [], []
    for c in comps:
        if c["kind"] == "testo":
            continue
        # bordi e ombre dei riquadri: tutto il tratto sta attaccato a un riquadro solo
        close = [i for i in range(len(nodes))
                 if np.percentile(dist_to(nd, i, c["xs"], c["ys"]), 95) <= 2.2 * tol]
        if len(close) == 1:
            c["kind"] = "bordo"
            continue
        if c["kind"] == "freccia":
            skip = np.zeros(len(c["xs"]), bool)
            for i in range(len(nodes)):
                skip |= dist_to(nd, i, c["xs"], c["ys"]) <= 1.5 * tol
            tail, head = block_arrow(c, skip if (~skip).sum() > 20 else None)
            ends = [(tuple(tail), False), (tuple(head), True)]
            c["heads"] = [tuple(head)]
        else:
            ends = line_ends(c)
        att = [(attach(nd, idmap, p, hd, c, reach), hd, p) for p, hd in ends]
        c["ends"] = att
        tgt = {i for i, hd, _ in att if i is not None and hd}
        src = {i for i, hd, _ in att if i is not None and not hd} - tgt
        if tgt and not src:
            # coda staccata (spesso c'e' un'etichetta Si/No in mezzo): riquadro piu'
            # vicino entro un raggio piu' ampio
            for i, hd, p in att:
                if i is None and not hd:
                    j = nearest_node(nd, p, 3 * reach)
                    if j is not None and j not in tgt:
                        src.add(j)
                        far_tails.append((j, p))
        # le linee che toccano un riquadro a meta' (non con un estremo) contano come origine
        src |= set(c["touch"]) - tgt - src if len(c["touch"]) <= 3 else set()
        if len(src) >= 2 and len(tgt) >= 2 or len(src) + len(tgt) >= 5:
            c["ambigua"] = True  # linee che si incrociano: non si sa chi va dove
        if tgt and src:
            edges += [(a, b, True, c["id"]) for a in sorted(src) for b in sorted(tgt) if a != b]
        elif not tgt and len(src) == 2:
            a, b = sorted(src)
            edges.append((a, b, False, c["id"]))
        elif len(tgt) >= 2 and not src:
            t = sorted(tgt)
            edges += [(t[i], t[j], False, c["id"]) for i in range(len(t)) for j in range(i + 1, len(t))]
        else:
            loose.append(c)
    # frecce spezzate (Word disegna una freccia per tratto): la punta di una finisce
    # vicino alla coda della successiva
    for c in loose:
        srcs = [i for i, hd, _ in c["ends"] if i is not None and not hd]
        free_heads = [p for i, hd, p in c["ends"] if hd and i is None]
        if len(srcs) != 1 or not free_heads:
            continue
        cur_head, seen = np.array(free_heads[0]), {c["id"]}
        for _ in range(5):
            nxt = None
            for d in loose:
                if d["id"] in seen:
                    continue
                for i, hd, p in d["ends"]:
                    if not hd and i is None and np.linalg.norm(np.array(p) - cur_head) <= 2 * reach:
                        nxt = d
                if nxt:
                    break
            if nxt is None:
                break
            seen.add(nxt["id"])
            done = [i for i, hd, _ in nxt["ends"] if hd and i is not None]
            if done:
                edges.append((srcs[0], done[0], True, c["id"]))
                break
            fh = [p for i, hd, p in nxt["ends"] if hd and i is None]
            if not fh:
                break
            cur_head = np.array(fh[0])
    edges += dashed_links(comps, nd, reach)
    out, seen = [], set()
    for e in edges:
        k = (e[0], e[1]) if e[2] else tuple(sorted(e[:2]))
        if k not in seen and (e[1], e[0]) not in seen:
            seen.add(k)
            out.append(e)
    return out


# ------------------------------------------------------------------ scritte
def ocr_tesseract(png):
    exe = shutil.which("tesseract")
    if not exe:
        raise SystemExit("tesseract non trovato: usa --ocr paddle")
    r = subprocess.run([exe, "stdin", "stdout", "-l", "ita+eng", "--psm", "6"], input=png,
                       capture_output=True, timeout=60)
    return r.stdout.decode("utf-8", "replace")


def make_ocr(kind, base_url, model):
    if kind == "tesseract":
        return ocr_tesseract
    if kind == "none":
        return lambda png: ""
    import requests

    def ocr(png):
        body = {"model": model, "temperature": 0, "max_tokens": 256, "cache_prompt": False,
                "messages": [{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}},
                    {"type": "text", "text": "OCR:"}]}]}
        r = requests.post(f"{base_url}/chat/completions", json=body, timeout=120)
        r.raise_for_status()
        return r.json()["choices"][0]["message"].get("content") or ""
    return ocr


def crop_png(img, x, y, w, h, pad=2, up=2):
    H, W = img.shape[:2]
    x0, y0, x1, y1 = max(0, x - pad), max(0, y - pad), min(W, x + w + pad), min(H, y + h + pad)
    c = img[y0:y1, x0:x1]
    if c.size == 0:
        return None
    c = cv2.resize(c, None, fx=up, fy=up, interpolation=cv2.INTER_CUBIC)
    # bordo bianco: il lettore non deve vedere i tratti del riquadro come lettere
    c = cv2.copyMakeBorder(c, 12, 12, 12, 12, cv2.BORDER_REPLICATE)
    return cv2.imencode(".png", c)[1].tobytes()


def clean_text(t):
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"[`*_#|]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def node_text(img, n, ocr):
    # solo l'interno: si toglie il bordo della figura
    e = max(5, int(0.06 * min(n.w, n.h)) | 1)
    mi = cv2.erode(n.mask, np.ones((e, e), np.uint8))
    ys, xs = np.nonzero(mi)
    if not len(xs):
        return ""
    x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
    # fuori dall'interno della figura si dipinge il colore di riempimento: niente bordi
    sub = img[y0:y1 + 1, x0:x1 + 1].copy()
    inside = mi[y0:y1 + 1, x0:x1 + 1] > 0
    if inside.any():
        sub[~inside] = np.median(sub[inside].reshape(-1, 3), 0).astype(np.uint8)
    png = crop_png(sub, 0, 0, sub.shape[1], sub.shape[0], pad=0)
    t = clean_text(ocr(png)) if png else ""
    return "" if NOT_TEXT.search(t) else t


def text_groups(comps, char_h, shape):
    """Raggruppa le lettere fuori dai riquadri in parole/righe."""
    m = np.zeros(shape, np.uint8)
    for c in comps:
        if c["kind"] == "testo":
            m |= c["mask"]
    k = max(3, int(char_h * 0.8))
    g = cv2.dilate(m, cv2.getStructuringElement(cv2.MORPH_RECT, (k, max(3, k // 3))))
    n, cc, st, cen = cv2.connectedComponentsWithStats(g)
    return [(tuple(st[j][:4]), tuple(cen[j])) for j in range(1, n) if st[j, cv2.CC_STAT_AREA] > 20]


def seg_dist(p, a, b):
    p, a, b = map(np.asarray, (p, a, b))
    ab = b - a
    t = 0 if not ab.any() else np.clip(((p - a) @ ab) / (ab @ ab), 0, 1)
    return float(np.linalg.norm(p - (a + t * ab)))


def attach_labels(groups, img, ocr, nodes, comps, edges, char_h):
    """Etichette brevi vicino a una linea (Si/No, Yes/No) -> etichetta del collegamento."""
    by_comp = {}
    for e in edges:
        by_comp.setdefault(e[3], []).append(e)
    pts = {c["id"]: np.stack([c["xs"], c["ys"]], 1) for c in comps if c["id"] in by_comp}
    labels, free = {}, 0
    for (x, y, w, h), cen in groups:
        # prima la geometria: si legge solo cio' che puo' essere un'etichetta (corto,
        # vicino a una linea). Il resto (titoli, note) lo legge gia' l'OCR della pagina.
        near = None
        if h <= 2.5 * char_h and w <= 10 * char_h:
            best = 4 * char_h
            for cid, p in pts.items():
                d = np.min(np.hypot(p[:, 0] - cen[0], p[:, 1] - cen[1]))
                if d < best:
                    near, best = cid, d
        if near is None:
            free += 1
            continue
        png = crop_png(img, x, y, w, h, pad=3, up=3)
        txt = clean_text(ocr(png)) if png else ""
        if not re.search(r"[A-Za-zÀ-ÿ]{2,}", txt) or len(txt) > 12 or NOT_TEXT.search(txt):
            continue  # pezzi di freccia letti come segni, o risposte inventate
        # tra i collegamenti della linea, quello che passa piu' vicino
        cand = by_comp[near]
        e = min(cand, key=lambda e: seg_dist(cen, nodes[e[0]].c, nodes[e[1]].c))
        labels[(e[0], e[1])] = txt
    return labels, free


# ------------------------------------------------------------------ Mermaid
def to_mermaid(nodes, edges, labels):
    used = {i for e in edges for i in e[:2]}
    horiz = sum(abs(nodes[a].c[0] - nodes[b].c[0]) > abs(nodes[a].c[1] - nodes[b].c[1]) for a, b, *_ in edges)
    lines = ["flowchart " + ("LR" if horiz > len(edges) / 2 else "TD")]
    esc = mermaid_testo
    for n in nodes:
        if n.i in used:
            lines.append(f"    N{n.i}" + SHAPES.get(n.kind, '["{}"]').format(esc(n.text)))
    for a, b, directed, _ in edges:
        lab = labels.get((a, b))
        arrow = "-->" if directed else "---"
        lines.append(f"    N{a} {arrow}{'|\"' + esc(lab) + '\"|' if lab else ''} N{b}")
    return "\n".join(lines)


def mermaid_testo(t: str) -> str:
    """Testo sicuro dentro le virgolette di Mermaid: niente virgolette doppie, a capo o
    caratteri di controllo. Con identificativi N0, N1... e testi sempre tra virgolette la
    sintassi e' corretta per costruzione: non serve lanciare mmdc (Chromium) per verificarla."""
    t = re.sub(r"[\x00-\x1f]+", " ", t or "").replace('"', "'").strip()
    return t.replace("#", "#35;") or " "


def check_mermaid(code):
    exe = shutil.which("mmdc")
    if not exe:
        return None
    with tempfile.TemporaryDirectory(dir="/dev/shm" if os.path.isdir("/dev/shm") else None) as d:  # in RAM
        src, out = Path(d) / "s.mmd", Path(d) / "s.svg"
        src.write_text(code, encoding="utf-8")
        cfg = Path(d) / "p.json"  # Ubuntu 24.04 blocca la sandbox di Chromium
        cfg.write_text('{"args": ["--no-sandbox"]}')
        try:
            r = subprocess.run([exe, "-p", str(cfg), "-i", str(src), "-o", str(out)],
                               capture_output=True, timeout=120)
        except subprocess.TimeoutExpired:
            return None
        if r.returncode == 0:
            return True
        err = r.stderr.decode("utf-8", "replace")
        # solo un errore di sintassi boccia lo schema; se manca il browser non si sa
        return False if re.search(r"Parse error|Syntax error|Lexical error", err, re.I) else None


def convert(path, ocr, debug_dir=None):
    with np.errstate(all="ignore"):
        return _convert(path, ocr, debug_dir)


def _convert(path, ocr, debug_dir=None):
    img, scale = load(path)
    H, W = img.shape[:2]
    l0, lab, _ = color_regions(img)
    labels, pairs = merge_regions(l0, lab)
    nodes = find_nodes(img, labels, pairs)
    tol = max(6, int(0.008 * max(H, W)))
    nd = node_dist(nodes, (H, W), 12 * tol)
    ink, _ = ink_mask(img, nodes)
    comps, char_h = components(ink, nodes, nd, tol)
    edges = wire(nodes, comps, nd, tol)
    used = {i for e in edges for i in e[:2]}
    orphans = [n for n in nodes if n.i not in used]
    linked = len(used)
    # lo schema deve essere un grafo quasi tutto connesso: pezzi staccati e molti
    # riquadri senza collegamenti vogliono dire collegamenti persi
    parent = {i: i for i in used}

    def find(i):
        while parent[i] != i:
            i = parent[i]
        return i
    for a, b, *_ in edges:
        parent[find(a)] = find(b)
    sizes = {}
    for i in used:
        sizes[find(i)] = sizes.get(find(i), 0) + 1
    main_part = max(sizes.values()) if sizes else 0
    ambig = sum(1 for c in comps if c.get("ambigua"))
    # macchie grandi e piene fuori dai riquadri: quasi sempre riquadri non riconosciuti
    blobs = sum(1 for c in comps if c["kind"] in ("freccia", "linea") and min(c["box"][2:]) >= 40
                and len(c["xs"]) >= 0.4 * c["box"][2] * c["box"][3])
    deg = {}
    for a, b, *_ in edges:
        deg[a] = deg.get(a, 0) + 1
        deg[b] = deg.get(b, 0) + 1
    hub = max(deg.values()) if deg else 0
    ok = (linked >= 3 and len(edges) >= 2 and main_part >= 0.75 * linked
          and linked >= 0.6 * len(nodes) and not ambig and not blobs and hub <= 6
          and len(nodes) <= MAX_NODES)
    # le scritte si leggono solo se la geometria regge: per foto e schemi non
    # riconosciuti non si spendono chiamate al lettore
    elabels, free = {}, 0
    if ok or debug_dir:
        for n in nodes:
            n.text = node_text(img, n, ocr)
        groups = text_groups(comps, char_h, (H, W))
        elabels, free = attach_labels(groups, img, ocr, nodes, comps, edges, char_h)
    mer = to_mermaid(nodes, edges, elabels)
    report = {"riquadri": len(nodes), "collegati": linked, "collegamenti": len(edges),
              "orientati": sum(e[2] for e in edges), "etichette": len(elabels),
              "linee_ambigue": ambig, "macchie": blobs, "max_collegamenti_riquadro": hub,
              "parte_principale": f"{main_part}/{linked}",
              "riquadri_isolati": [n.text for n in orphans],
              "scritte_fuori_non_lette": free}
    # controllo con mmdc (Chromium, 1-2 s) solo se chiesto: la sintassi e' corretta per costruzione
    syntax = check_mermaid(mer) if ok and os.environ.get("DOC2MD_MMDC") == "1" else None
    if syntax is False:
        ok = False
    report["sintassi_mermaid"] = syntax
    report["esito"] = "mermaid" if ok else "ripiego"
    if debug_dir:
        stem = "immagine" if isinstance(path, (bytes, bytearray)) else Path(path).stem
        draw(img, nodes, edges, comps, Path(debug_dir) / (stem + "_schema.png"))
    return mer, report


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("immagini", nargs="+")
    ap.add_argument("--ocr", choices=["paddle", "tesseract", "none"], default="paddle")
    ap.add_argument("--base-url", default="http://127.0.0.1:8080/v1", help="llama-swap")
    ap.add_argument("--model", default="paddleocr-vl")
    ap.add_argument("--debug", nargs="?", const="out-schemi", default=None,
                    help="cartella dove disegnare riquadri e collegamenti trovati")
    a = ap.parse_args()
    ocr = make_ocr(a.ocr, a.base_url, a.model)
    if a.debug:
        Path(a.debug).mkdir(parents=True, exist_ok=True)
    for p in a.immagini:
        mer, rep = convert(p, ocr, a.debug)
        print(f"## {Path(p).name}\n\n```mermaid\n{mer}\n```\n")
        print(json.dumps(rep, ensure_ascii=False, indent=1), "\n")


if __name__ == "__main__":
    main()
