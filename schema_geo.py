#!/usr/bin/env python3
"""schema_geo.py - prototipo: da schema vettoriale (PDF nativo) a Mermaid.

Non usa modelli: legge rettangoli, rombi, ellissi, linee e frecce dichiarati
nel PDF, associa i testi ai riquadri e ricostruisce i collegamenti.

Uso:  python schema_geo.py documento.pdf [pagina]
"""
import math
import sys
from itertools import combinations

import pymupdf

TOL_ATTACH = 18.0   # distanza massima freccia-riquadro (punti)
TOL_JOIN = 2.5      # distanza massima tra due linee che si toccano
LABELS = {"y", "n", "yes", "no", "si", "sì", "sí", "vero", "falso", "true", "false"}
ETICHETTE = {}      # (da, a) -> etichetta, riempito da wire_edges a ogni conversione


# ---------------------------------------------------------------- geometria
def pts_of(d):
    out = []
    for it in d["items"]:
        if it[0] == "re":
            r = it[1]
            out += [(r.x0, r.y0), (r.x1, r.y0), (r.x1, r.y1), (r.x0, r.y1)]
        elif it[0] == "qu":
            out += [(p.x, p.y) for p in (it[1].ul, it[1].ur, it[1].lr, it[1].ll)]
        else:
            out += [(p.x, p.y) for p in it[1:] if hasattr(p, "x")]
    return out


def seg_dist(p, a, b):
    ax, ay, bx, by = *a, *b
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0 if L2 == 0 else max(0, min(1, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2))
    return math.hypot(p[0] - ax - t * dx, p[1] - ay - t * dy)


def segseg_dist(a, b, c, d):
    def ccw(p, q, r):
        return (r[1] - p[1]) * (q[0] - p[0]) - (q[1] - p[1]) * (r[0] - p[0])
    if (ccw(a, c, d) * ccw(b, c, d) < 0) and (ccw(a, b, c) * ccw(a, b, d) < 0):
        return 0.0
    return min(seg_dist(a, c, d), seg_dist(b, c, d), seg_dist(c, a, b), seg_dist(d, a, b))


class Node:
    def __init__(self, i, kind, rect, dashed=False):
        self.i, self.kind, self.r, self.text = i, kind, rect, ""

    def poly(self):
        r = self.r
        if self.kind == "diamond":
            cx, cy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
            return [(cx, r.y0), (r.x1, cy), (cx, r.y1), (r.x0, cy)]
        return [(r.x0, r.y0), (r.x1, r.y0), (r.x1, r.y1), (r.x0, r.y1)]

    def inside(self, p):
        r = self.r
        if not (r.x0 <= p[0] <= r.x1 and r.y0 <= p[1] <= r.y1):
            return False
        if self.kind == "diamond":
            cx, cy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
            return abs(p[0] - cx) / (r.width / 2) + abs(p[1] - cy) / (r.height / 2) <= 1
        return True

    def dist(self, p):
        if self.inside(p):
            return 0.0
        q = self.poly()
        return min(seg_dist(p, q[k], q[(k + 1) % 4]) for k in range(4))

    def side_score(self, p):
        """+2 destra, +1 basso (uscita probabile); -1 alto, -2 sinistra (entrata)."""
        r = self.r
        d = {2: abs(p[0] - r.x1), -2: abs(p[0] - r.x0), 1: abs(p[1] - r.y1), -1: abs(p[1] - r.y0)}
        return min(d, key=d.get)


def chiusa(d) -> bool:
    """Il tracciato torna al punto di partenza (forma chiusa, non un connettore curvo)."""
    if d.get("closePath"):
        return True
    p = pts_of(d)
    return len(p) > 2 and math.dist(p[0], p[-1]) < 2.0


def doppioni(nodes):
    """Stessa forma disegnata due volte (riempimento + contorno): se ne tiene una."""
    keep = []
    for n in sorted(nodes, key=lambda n: -n.r.get_area()):
        if not any(abs(n.r & m.r) > 0.85 * max(abs(n.r), abs(m.r)) for m in keep):
            keep.append(n)
    return keep


# ------------------------------------------------------------------ lettura
def read_page(page):
    nodes, strokes, markers, filled = [], [], [], []
    for d in page.get_drawings():
        its, r = d["items"], d["rect"]
        kinds = [it[0] for it in its]
        stroke = d["type"] in ("s", "fs")
        big = r.width > 12 and r.height > 10
        thin = min(r.width, r.height) < 2.0 and max(r.width, r.height) > 5
        if kinds == ["re"] and d["type"] == "f" and thin:
            # linea disegnata come rettangolo sottile pieno
            if r.width >= r.height:
                y = (r.y0 + r.y1) / 2
                strokes.append(((r.x0, y), (r.x1, y), False))
            else:
                x = (r.x0 + r.x1) / 2
                strokes.append(((x, r.y0), (x, r.y1), False))
        elif big and kinds == ["re"]:
            nodes.append(Node(len(nodes), "rect", r))      # con bordo o solo riempito
        elif d["type"] in ("f", "fs") and set(kinds) == {"l"} and len(its) <= 4 \
                and 2 < max(r.width, r.height) < 14:
            p = pts_of(d)                                   # punta di freccia piena
            markers.append((sum(q[0] for q in p) / len(p), sum(q[1] for q in p) / len(p)))
        elif big and kinds == ["qu"]:
            q = its[0][1]                                   # quadrilatero: rombo o rettangolo
            nodes.append(Node(len(nodes), "rect" if q.is_rectangular else "diamond", r))
        elif stroke and big and len(its) == 4 and set(kinds) == {"c"}:
            nodes.append(Node(len(nodes), "ellipse", r))
        elif big and "c" in kinds and set(kinds) <= {"l", "c"} and len(its) <= 14 and chiusa(d):
            nodes.append(Node(len(nodes), "round", r))      # capsula, documento, angoli tondi
        elif stroke and big and len(its) in (4, 5) and set(kinds) == {"l"}:
            nodes.append(Node(len(nodes), "diamond", r))
        elif stroke and set(kinds) == {"l"}:
            if len(its) == 2 and max(r.width, r.height) < 10:
                p = pts_of(d)            # punta di freccia a "V": vertice comune
                tip = max(set(p), key=p.count)
                markers.append(tip)
            else:
                dashed = bool(d.get("dashes")) and d["dashes"] not in ("[] 0", "")
                for it in its:
                    strokes.append(((it[1].x, it[1].y), (it[2].x, it[2].y), dashed))
        elif d["type"] == "f" and max(r.width, r.height) > 12 and min(r.width, r.height) > 1.5:
            fill = d.get("fill") or (0, 0, 0)
            if sum(fill) < 2.4:            # non bianco
                filled.append(d)
    nodes = doppioni(nodes)
    W, H = page.rect.width, page.rect.height
    # barre di titolo e corsie (larghe quanto la pagina) e intestazioni verticali delle corsie
    # intestazioni delle corsie: strette e alte, oppure appoggiate al bordo sinistro con
    # il testo ruotato (piu' alte che larghe, a meno dell'8% dalla sinistra)
    corsia = lambda n: (n.r.width < 50 and n.r.height > 3 * n.r.width) or \
        (n.r.width < 80 and n.r.height > 1.5 * n.r.width and n.r.x0 < 0.08 * W)
    nodes = [n for n in nodes if n.r.width < 0.85 * W and n.r.height < 0.85 * H and not corsia(n)]
    # contenitori (cornici con dentro altri riquadri) non sono nodi
    keep = []
    for n in nodes:
        inner = sum(1 for m in nodes if m is not n and n.r.contains(m.r))
        if inner < 2:
            keep.append(n)
    nodes = keep
    # testi
    free = []
    for w in page.get_text("words"):
        c = ((w[0] + w[2]) / 2, (w[1] + w[3]) / 2)
        owner = [n for n in nodes if n.inside(c)]
        if owner:
            n = min(owner, key=lambda n: n.r.get_area())
            n.text += (" " if n.text else "") + w[4]
        else:
            free.append(w)
    nodes = [n for n in nodes if n.text.strip()]
    # "Yes"/"No" in un piccolo riquadro di testo: e' un'etichetta, non un blocco
    labels = [w for w in free if w[4].strip().lower().strip(".:") in LABELS]
    lab_nodes = [n for n in nodes if n.text.strip().lower().strip(".:") in LABELS
                 and n.r.width < 45 and n.r.height < 25]
    for n in lab_nodes:
        labels.append((n.r.x0, n.r.y0, n.r.x1, n.r.y1, n.text.strip()))
        n.etichetta = True       # resta nel cablaggio (la linea puo' attraversarlo);
                                 # se alla fine e' isolato non e' un blocco
    for k, n in enumerate(nodes):
        n.i = k
    page._etichette = labels                 # per wire_edges (senza cambiare la firma)
    return nodes, strokes, markers, filled, free


# ------------------------------------------------- frecce piene (poligoni)
def arrow_from_fill(d):
    """Asse principale del poligono; la punta e' l'estremo piu' largo."""
    P = pts_of(d)
    n = len(P)
    cx, cy = sum(p[0] for p in P) / n, sum(p[1] for p in P) / n
    sxx = sum((p[0] - cx) ** 2 for p in P)
    syy = sum((p[1] - cy) ** 2 for p in P)
    sxy = sum((p[0] - cx) * (p[1] - cy) for p in P)
    ang = 0.5 * math.atan2(2 * sxy, sxx - syy)
    ux, uy = math.cos(ang), math.sin(ang)
    T = [((p[0] - cx) * ux + (p[1] - cy) * uy, -(p[0] - cx) * uy + (p[1] - cy) * ux, p) for p in P]
    tmin, tmax = min(t[0] for t in T), max(t[0] for t in T)
    L = tmax - tmin
    if L < 10:
        return None
    zone = min(L / 2, max(L / 4, 12))
    lo = [t for t in T if t[0] <= tmin + zone]
    hi = [t for t in T if t[0] >= tmax - zone]
    spread = lambda z: max(t[1] for t in z) - min(t[1] for t in z)
    a = (cx + tmin * ux, cy + tmin * uy)
    b = (cx + tmax * ux, cy + tmax * uy)
    head_hi = spread(hi) >= spread(lo)
    tail, tip = (a, b) if head_hi else (b, a)
    return tail, tip, len(d["items"]) > 20          # molte parti = tratteggiata


def nearest(nodes, p, tol=TOL_ATTACH, skip=None):
    best = min((n for n in nodes if n is not skip), key=lambda n: n.dist(p), default=None)
    return best if best is not None and best.dist(p) <= tol else None


# ------------------------------------------------------ reti di linee sottili
def clip(a, b, n):
    """Intervallo [t0,t1] del segmento a-b dentro il riquadro del nodo (o None)."""
    r = n.r
    t0, t1 = 0.0, 1.0
    dx, dy = b[0] - a[0], b[1] - a[1]
    for p, q in ((-dx, a[0] - r.x0 + 1), (dx, r.x1 + 1 - a[0]), (-dy, a[1] - r.y0 + 1), (dy, r.y1 + 1 - a[1])):
        if p == 0:
            if q < 0:
                return None
        else:
            t = q / p
            if p < 0:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
    return (t0, t1) if t0 <= t1 else None


def etichetta_vicina(labels, segmenti, tol=15.0):
    """Etichetta Si/No piu' vicina a uno dei segmenti (entro tol punti)."""
    best, bd = None, tol
    for w in labels:
        c = ((w[0] + w[2]) / 2, (w[1] + w[3]) / 2)
        d = min(seg_dist(c, a, b) for a, b in segmenti)
        if d < bd:
            best, bd = w[4].strip(), d
    return best


def wire_edges(nodes, strokes, markers, labels=()):
    secs = []                                  # (p, q, [(nodo, punto)], tratteggio)
    for a, b, dashed in strokes:
        L = math.dist(a, b)
        if L < 1:
            continue
        at = lambda t: (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
        cuts = sorted(((c[0], c[1], n) for n in nodes if (c := clip(a, b, n))), key=lambda t: (t[0], t[1]))
        cur, prev = 0.0, None
        for t0, t1, n in cuts + [(1.0, 1.0, None)]:
            if (t0 - cur) * L > 1.0:
                att = [(m, at(t)) for m, t in ((prev, cur), (n, t0)) if m is not None]
                secs.append((at(cur), at(t0), att, dashed))
            if t1 >= cur:
                cur, prev = t1, n
    parent = list(range(len(secs)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for i, j in combinations(range(len(secs)), 2):
        if segseg_dist(secs[i][0], secs[i][1], secs[j][0], secs[j][1]) <= TOL_JOIN:
            parent[find(i)] = find(j)
    nets = {}
    for i, s in enumerate(secs):
        nets.setdefault(find(i), []).append(s)
    nets = list(nets.values())
    # ogni punta di freccia va alla rete di linee che le termina piu' vicino
    net_marks = {}
    for m in markers:
        best, bd = None, 9.0
        for k, net in enumerate(nets):
            d = min(min(math.dist(m, s[0]), math.dist(m, s[1]), seg_dist(m, s[0], s[1]) * 3) for s in net)
            if d < bd:
                best, bd = k, d
        if best is not None:
            net_marks.setdefault(best, []).append(m)
    edges, dangling = [], 0
    for k, net in enumerate(nets):
        att = {}
        for s in net:
            for n, p in s[2]:
                att.setdefault(n.i, (n, p))
        for s in net:                          # estremi che si fermano a filo del bordo
            for p in s[:2]:
                n = nearest(nodes, p, tol=6)
                if n is not None:
                    att.setdefault(n.i, (n, p))
        targets = set()
        for m in net_marks.get(k, []):         # la punta indica il riquadro di arrivo,
            n = nearest(nodes, m, tol=12)      # anche se la linea si ferma prima
            if n is not None:
                att.setdefault(n.i, (n, m))
                targets.add(n.i)
        if len(att) < 2:
            dangling += 1
            continue
        if len(targets) == len(att):           # punte su tutti i capi: nessuna sorgente
            targets = set()
        if not targets:
            score = {i: n.side_score(p) for i, (n, p) in att.items()}
            lo = min(score.values())
            if len(set(score.values())) == 1:      # nessun indizio: ordine di lettura
                first = min(att.values(), key=lambda v: (v[0].r.x0, v[0].r.y0))[0].i
                targets = set(att) - {first}
            else:
                targets = {i for i, v in score.items() if v == lo}
        dashed = any(s[3] for s in net)
        nuovi = [(s_, t_) for s_ in set(att) - targets for t_ in targets]
        for s_, t_ in nuovi:
            edges.append((s_, t_, dashed, False))
        if len(nuovi) == 1 and labels:
            best = etichetta_vicina(labels, [(s[0], s[1]) for s in net])
            if best:
                ETICHETTE[nuovi[0]] = best
    return edges, dangling


# ------------------------------------------------------------------ schema
def build(page):
    nodes, strokes, markers, filled, free = read_page(page)
    ETICHETTE.clear()
    labels = getattr(page, "_etichette", ())
    piene = []
    for d in filled:
        arr = arrow_from_fill(d)
        if not arr:
            continue
        tail, tip, dotted = arr
        s, t = nearest(nodes, tail), nearest(nodes, tip)
        if s and t and s is not t:
            piene.append((s.i, t.i, dotted, False))
            lab = etichetta_vicina(labels, [(tail, tip)])
            if lab:
                ETICHETTE[(s.i, t.i)] = lab
        elif (s is None) != (t is None):
            # freccia piena che prosegue una linea (o un'altra freccia): diventa un
            # tratto con la punta, cosi' si salda alla rete di linee
            strokes = strokes + [(tail, tip, dotted)]
            markers = markers + [tip]
    edges, dangling = wire_edges(nodes, strokes, markers, labels)
    edges += piene
    # etichetta ("Yes"/"No") con un collegamento in entrata e nessuno in uscita: la linea
    # prosegue oltre l'etichetta con un tratto troppo corto per agganciarsi; si collega
    # il blocco che sta subito oltre, nella direzione da cui arriva la linea
    outs = {e[0] for e in edges}
    for lab in [n for n in nodes if getattr(n, "etichetta", False)]:
        ins = [e for e in edges if e[1] == lab.i]
        if len(ins) != 1 or lab.i in outs:
            continue
        src = nodes[ins[0][0]]
        cx, cy = (lab.r.x0 + lab.r.x1) / 2, (lab.r.y0 + lab.r.y1) / 2
        sx, sy = (src.r.x0 + src.r.x1) / 2, (src.r.y0 + src.r.y1) / 2
        dx, dy = cx - sx, cy - sy
        L = math.hypot(dx, dy) or 1
        dx, dy = dx / L, dy / L
        best = None
        for t in range(2, 40, 2):            # al massimo 40 punti oltre l'etichetta
            q = (cx + dx * (t + max(lab.r.width, lab.r.height) / 2), cy + dy * (t + max(lab.r.width, lab.r.height) / 2))
            hit = [n for n in nodes if n is not lab and n is not src and n.inside(q)]
            if hit:
                best = hit[0]
                break
        if best is not None:
            edges.append((lab.i, best.i, ins[0][2], False))
    # etichetta con due vicini: e' un pezzo del collegamento tra i due. Il verso: dal rombo
    # (le etichette Si/No escono dalle decisioni), altrimenti quello delle frecce trovate
    for lab in [n for n in nodes if getattr(n, "etichetta", False)]:
        tocca = [e for e in edges if lab.i in e[:2]]
        vicini = {x for e in tocca for x in e[:2]} - {lab.i}
        if len(vicini) != 2:
            continue
        a, b = sorted(vicini)
        rombi = [x for x in (a, b) if nodes[x].kind == "diamond"]
        if len(rombi) == 1:
            sorg = rombi[0]
        else:
            entra = [e[0] for e in tocca if e[1] == lab.i]
            if len(entra) != 1:
                continue
            sorg = entra[0]
        dest = b if sorg == a else a
        dashed = any(e[2] for e in tocca)
        edges = [e for e in edges if lab.i not in e[:2]]
        edges.append((sorg, dest, dashed, False))
        ETICHETTE[(sorg, dest)] = lab.text.strip()
    # riquadri impilati dello stesso tipo: l'entrata di uno vale per il gruppo (dedotto)
    has_in = {e[1] for e in edges}
    for a in nodes:
        if a.i not in has_in:
            continue
        group = [b for b in nodes if b is not a and b.kind == a.kind
                 and abs(b.r.x0 - a.r.x0) < 1.5 and abs(b.r.x1 - a.r.x1) < 1.5]
        chain, grew = {a.i}, True
        while grew:
            grew = False
            for b in group:
                if b.i not in chain and any(
                        min(abs(b.r.y0 - nodes[c].r.y1), abs(nodes[c].r.y0 - b.r.y1)) < 10 for c in chain):
                    chain.add(b.i)
                    grew = True
        for b in chain - {a.i}:
            if b not in has_in:
                for e in [e for e in edges if e[1] == a.i]:
                    edges.append((e[0], b, e[2], True))
    edges = list(dict.fromkeys(edges))
    return nodes, edges, dangling, free


def to_mermaid(nodes, edges):
    ins, outs = {}, {}
    for e in edges:
        outs.setdefault(e[0], []).append(e)
        ins.setdefault(e[1], []).append(e)
    is_label = lambda n: n.text.strip().lower() in LABELS
    collapse = {n.i for n in nodes if is_label(n) and ins.get(n.i) and outs.get(n.i)
                and min(len(ins[n.i]), len(outs[n.i])) == 1}
    final = []
    for e in edges:
        if e[0] in collapse or e[1] in collapse:
            continue
        final.append((e[0], e[1], ETICHETTE.get((e[0], e[1]), ""), e[2], e[3]))
    for c in collapse:
        for i in ins[c]:
            for o in outs[c]:
                final.append((i[0], o[1], nodes[c].text.strip(), i[2] or o[2], i[3] or o[3]))
    final = list(dict.fromkeys(final))
    used = {x for e in final for x in e[:2]}
    cen = lambda k: ((nodes[k].r.x0 + nodes[k].r.x1) / 2, (nodes[k].r.y0 + nodes[k].r.y1) / 2)
    horiz = sum(abs(cen(a)[0] - cen(b)[0]) > abs(cen(a)[1] - cen(b)[1]) for a, b, *_ in final)
    lines = ["flowchart " + ("LR" if horiz > len(final) / 2 else "TD")]
    shape = {"rect": '["{}"]', "diamond": '{{"{}"}}', "ellipse": '(["{}"])', "round": '("{}")'}
    for n in nodes:
        if n.i in used or n.i not in collapse:
            lines.append(f"    N{n.i}" + shape[n.kind].format(n.text.strip().replace('"', "'")))
    for a, b, lab, dashed, inferred in sorted(final, key=lambda e: (e[0], e[1], str(e[2]))):
        arrow = "-.->" if dashed else "-->"
        if inferred:
            lines.append("    %% collegamento dedotto (riquadri impilati), non disegnato")
        lines.append(f"    N{a} {arrow}{'|' + lab + '|' if lab else ''} N{b}")
    orphans = [n for n in nodes if n.i not in used and n.i not in collapse]
    etichette_sole = {n.i for n in orphans if getattr(n, "etichetta", False)}
    lines = [l for l in lines if not any(l.startswith(f"    N{i}[") or l.startswith(f"    N{i}(")
                                          or l.startswith(f"    N{i}{{") for i in etichette_sole)]
    orphans = [n for n in orphans if n.i not in etichette_sole]
    return "\n".join(lines), final, orphans


class PaginaVista:
    """Pagina ruotata vista come appare: disegni e parole portati nelle coordinate
    visibili, cosi' "sinistra/destra" e l'ordine di lettura sono quelli veri."""
    def __init__(self, page):
        self._p, self._m = page, page.rotation_matrix
        self.rect, self.rotation, self.parent, self.number = page.rect, 0, page.parent, page.number

    def get_drawings(self):
        m, out = self._m, []
        for d in self._p.get_drawings():
            d = dict(d)
            its = []
            for it in d["items"]:
                if it[0] == "re":
                    its.append(("re", it[1] * m) + tuple(it[2:]))
                elif it[0] == "qu":
                    its.append(("qu", it[1] * m))
                else:
                    its.append((it[0],) + tuple(q * m if isinstance(q, pymupdf.Point) else q for q in it[1:]))
            d["items"], d["rect"] = its, d["rect"] * m
            out.append(d)
        return out

    def get_text(self, kind="text", **kw):
        if kind != "words":
            return self._p.get_text(kind, **kw)
        return [tuple((pymupdf.Rect(w[:4]) * self._m)) + tuple(w[4:]) for w in self._p.get_text("words", **kw)]

    def __getattr__(self, k):
        return getattr(self._p, k)


def raddrizza(page):
    return (PaginaVista(page), None) if page.rotation else (page, None)


def convert(page):
    page, _tmp = raddrizza(page)      # _tmp tiene viva la copia finche' serve
    nodes, edges, dangling, free = build(page)
    mer, final, orphans = to_mermaid(nodes, edges)
    n = len(nodes)
    ok = n >= 3 and len(orphans) <= max(1, 0.15 * n) and dangling <= 0.25 * max(1, len(edges))
    report = {"nodi": len({x for e in final for x in e[:2]}) + len(orphans), "collegamenti": len(final), "dedotti": sum(e[4] for e in final),
              "nodi_isolati": [o.text for o in orphans], "frecce_non_agganciate": dangling,
              "esito": "mermaid" if ok else "ripiego"}
    return mer, report, nodes, final


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--debug"]
    doc = pymupdf.open(args[0])
    num = int(args[1]) if len(args) > 1 else 1
    if not 1 <= num <= len(doc):
        raise SystemExit(f"Pagina {num} inesistente: il documento ha {len(doc)} pagine.")
    page = doc[num - 1]
    if "--debug" in sys.argv:
        dr = page.get_drawings()
        tipi = {}
        for d in dr:
            k = d["type"] + ":" + "".join(sorted({it[0] for it in d["items"]}))
            tipi[k] = tipi.get(k, 0) + 1
        nd, st, mk, fl, fr = read_page(page)
        print(f"parole={len(page.get_text('words'))} immagini={len(page.get_images())} "
              f"disegni={len(dr)} {tipi}")
        print(f"riquadri={len(nd)} linee={len(st)} punte={len(mk)} frecce_piene={len(fl)}\n")
    mer, report, *_ = convert(page)
    print("```mermaid\n" + mer + "\n```\n")
    print(report)
