"""Formality lint for patent drawing sheets (SVG), against 37 CFR 1.84.

Pure stdlib. Input: SVG text of one sheet. Output: a SheetReport of findings.
Batch checks across a set of sheets (same size, consecutive numbering) are in
lint_set(). Rule text read 2026-11-07 (law.cornell.edu/cfr/text/37/1.84).

What is measured, honestly:
- text height is the computed font-size (the em box), not the drawn cap height;
- text extents are estimated (0.6 em per character); text converted to outlines
  cannot be measured as text;
- curves get exact Bezier extrema; arcs, circles and ellipses are sampled.
"""

from __future__ import annotations

import hashlib
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

VERSION = "0.1.0"
DISCLAIMER = "Automated formality check only, not legal advice."

MAX_BYTES = 20 * 1024 * 1024
MAX_NODES = 200_000
MAX_USE_DEPTH = 8

PT_PER_IN = 72.0
PT_PER_MM = 72.0 / 25.4
MIN_TEXT_PT = 9.0          # 1.84(p)(3): at least .32 cm (1/8 inch)
THIN_LINE_PT = 0.5         # 1.84(l) gives no number; below this lines often drop out in scanning
SIZE_TOL_PT = 2.0          # sheet size tolerance (about 0.7 mm)
BOUNDS_TOL_PT = 0.5
NEAR_BLACK_MAX = 40        # every channel <= 40 of 255 reads as black when scanned bilevel


@dataclass(frozen=True)
class Sheet:
    name: str
    w: float
    h: float
    top: float
    left: float
    right: float
    bottom: float

    @property
    def sight(self):
        return (self.left, self.top, self.w - self.right, self.h - self.bottom)


# 1.84(f) sizes and 1.84(g) margins, in points.
SHEETS = {
    "letter": Sheet("letter", 612.0, 792.0, 72.0, 72.0, 45.0, 27.0),
    "a4": Sheet("a4", 210 * PT_PER_MM, 297 * PT_PER_MM,
                25 * PT_PER_MM, 25 * PT_PER_MM, 15 * PT_PER_MM, 10 * PT_PER_MM),
}

RULES = {
    "input": "-",
    "page-size": "37 CFR 1.84(f)",
    "margins": "37 CFR 1.84(g)",
    "frame": "37 CFR 1.84(g)",
    "ink": "37 CFR 1.84(a)(1)",
    "near-black": "37 CFR 1.84(a)(1)",
    "gradient": "37 CFR 1.84(a)(1), 1.84(m)",
    "translucent": "37 CFR 1.84(l)",
    "thin-line": "37 CFR 1.84(l)",
    "text-height": "37 CFR 1.84(p)(3)",
    "no-live-text": "37 CFR 1.84(p)(3)",
    "raster-image": "37 CFR 1.84(b)",
    "sheet-number-format": "37 CFR 1.84(t)",
    "sheet-number-missing": "37 CFR 1.84(t)",
    "sheet-number-position": "37 CFR 1.84(t)",
    "sheet-number-size": "37 CFR 1.84(t)",
    "sheet-number-sequence": "37 CFR 1.84(t)",
    "fig-label-format": "37 CFR 1.84(u)(1)",
    "fig-sequence": "37 CFR 1.84(u)(1)",
    "single-view": "37 CFR 1.84(u)(1)",
    "mixed-sizes": "37 CFR 1.84(f)",
    "numeral-missing": "37 CFR 1.84(p)(5)",
    "unsupported-css": "-",
    "unresolved-ref": "-",
    "pdf-repaired": "-",
}


class InputError(ValueError):
    """The file cannot be read as an SVG sheet."""


class TooManySheets(InputError):
    """The input holds more sheets than one run checks (HTTP 413)."""


@dataclass
class Finding:
    code: str
    severity: str  # "error" or "advisory"
    message: str
    count: int = 1
    examples: list = field(default_factory=list)

    def to_dict(self):
        return {"code": self.code, "severity": self.severity, "rule": RULES.get(self.code, "-"),
                "message": self.message, "count": self.count, "examples": self.examples[:5]}


class Findings:
    """Aggregates repeated findings by (code, message) so one bad layer is one line."""

    def __init__(self):
        self._by_key = {}

    def add(self, code, severity, message, example=None):
        key = (code, severity, message)
        f = self._by_key.get(key)
        if f is None:
            f = self._by_key[key] = Finding(code, severity, message, 0, [])
        f.count += 1
        if example and len(f.examples) < 5 and example not in f.examples:
            f.examples.append(example)

    def list(self):
        return list(self._by_key.values())


@dataclass
class SheetReport:
    name: str
    sha256: str
    page: dict
    findings: list
    texts: list            # (text, font_pt, bbox) for set-level checks
    sheet_number: tuple | None  # (n, m) if found

    @property
    def errors(self):
        return sum(1 for f in self.findings if f.severity == "error")

    @property
    def advisories(self):
        return sum(1 for f in self.findings if f.severity == "advisory")

    def to_dict(self):
        return {"file": self.name, "sha256": self.sha256, "page": self.page,
                "errors": self.errors, "advisories": self.advisories,
                "findings": [f.to_dict() for f in self.findings]}


# ---------------------------------------------------------------- geometry

IDENT = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def mul(m, n):
    """m then n applied to a point as m(n(p)): matrix product m x n."""
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return (a * a2 + c * b2, b * a2 + d * b2, a * c2 + c * d2, b * c2 + d * d2,
            a * e2 + c * f2 + e, b * e2 + d * f2 + f)


def apply(m, x, y):
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


def scale_of(m):
    return math.sqrt(abs(m[0] * m[3] - m[1] * m[2]))


_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_NUM_RE = re.compile(_NUM)


def nums(s):
    return [float(v) for v in _NUM_RE.findall(s or "")]


def parse_transform(s):
    m = IDENT
    for fn, args in re.findall(r"([a-zA-Z]+)\s*\(([^)]*)\)", s or ""):
        a = nums(args)
        fn = fn.lower()
        if fn == "matrix" and len(a) == 6:
            t = tuple(a)
        elif fn == "translate" and a:
            t = (1, 0, 0, 1, a[0], a[1] if len(a) > 1 else 0.0)
        elif fn == "scale" and a:
            t = (a[0], 0, 0, a[1] if len(a) > 1 else a[0], 0, 0)
        elif fn == "rotate" and a:
            r = math.radians(a[0])
            cs, sn = math.cos(r), math.sin(r)
            t = (cs, sn, -sn, cs, 0, 0)
            if len(a) >= 3:
                t = mul(mul((1, 0, 0, 1, a[1], a[2]), t), (1, 0, 0, 1, -a[1], -a[2]))
        elif fn == "skewx" and a:
            t = (1, 0, math.tan(math.radians(a[0])), 1, 0, 0)
        elif fn == "skewy" and a:
            t = (1, math.tan(math.radians(a[0])), 0, 1, 0, 0)
        else:
            continue
        m = mul(m, t)
    return m


def _bez_extrema(p0, p1, p2, p3):
    """Parameter values in (0,1) where a cubic's derivative is zero (one axis)."""
    a = -p0 + 3 * p1 - 3 * p2 + p3
    b = 2 * (p0 - 2 * p1 + p2)
    c = p1 - p0
    ts = []
    if abs(a) < 1e-12:
        if abs(b) > 1e-12:
            ts.append(-c / b)
    else:
        disc = b * b - 4 * a * c
        if disc >= 0:
            sq = math.sqrt(disc)
            ts += [(-b + sq) / (2 * a), (-b - sq) / (2 * a)]
    return [t for t in ts if 0 < t < 1]


def _cubic_points(P0, P1, P2, P3):
    pts = [P0, P3]
    for axis in (0, 1):
        for t in _bez_extrema(P0[axis], P1[axis], P2[axis], P3[axis]):
            mt = 1 - t
            pts.append(tuple(mt ** 3 * P0[i] + 3 * mt * mt * t * P1[i] + 3 * mt * t * t * P2[i]
                             + t ** 3 * P3[i] for i in (0, 1)))
    return pts


def _arc_points(x1, y1, rx, ry, phi_deg, large, sweep, x2, y2, n=48):
    """Sample an SVG elliptical arc (endpoint form, SVG 1.1 F.6.5)."""
    if rx == 0 or ry == 0 or (x1 == x2 and y1 == y2):
        return [(x1, y1), (x2, y2)]
    rx, ry = abs(rx), abs(ry)
    phi = math.radians(phi_deg)
    cp, sp = math.cos(phi), math.sin(phi)
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    x1p, y1p = cp * dx + sp * dy, -sp * dx + cp * dy
    lam = (x1p / rx) ** 2 + (y1p / ry) ** 2
    if lam > 1:
        rx, ry = rx * math.sqrt(lam), ry * math.sqrt(lam)
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    co = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep:
        co = -co
    cxp, cyp = co * rx * y1p / ry, -co * ry * x1p / rx
    cx = cp * cxp - sp * cyp + (x1 + x2) / 2
    cy = sp * cxp + cp * cyp + (y1 + y2) / 2

    def ang(ux, uy, vx, vy):
        a = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
        return a

    t1 = ang(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dt = ang((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and dt > 0:
        dt -= 2 * math.pi
    elif sweep and dt < 0:
        dt += 2 * math.pi
    pts = []
    for i in range(n + 1):
        t = t1 + dt * i / n
        ex, ey = rx * math.cos(t), ry * math.sin(t)
        pts.append((cx + cp * ex - sp * ey, cy + sp * ex + cp * ey))
    return pts


_ARGC = {"m": 2, "l": 2, "h": 1, "v": 1, "c": 6, "s": 4, "q": 4, "t": 2, "a": 7, "z": 0}
_NUM_AT = re.compile(_NUM)
_SEP = re.compile(r"[\s,]*")


def path_tokens(d):
    """Commands and numbers; arc flags (args 4 and 5 of A) are single characters, as SVG allows '011'."""
    d = d or ""
    toks, i, cmd, argi = [], 0, None, 0
    while True:
        i = _SEP.match(d, i).end()
        if i >= len(d):
            return toks
        ch = d[i]
        if ch in "MmZzLlHhVvCcSsQqTtAa":
            toks.append(ch)
            cmd, argi, i = ch.lower(), 0, i + 1
            continue
        if cmd == "a" and argi % 7 in (3, 4) and ch in "01":
            toks.append(float(ch))
            argi, i = argi + 1, i + 1
            continue
        m = _NUM_AT.match(d, i)
        if not m:
            return toks  # SVG renders up to the first error
        toks.append(float(m.group(0)))
        argi, i = argi + 1, m.end()


def path_segments(d):
    """Point lists in local coordinates; Beziers as ('c', P0..P3), others as ('p', pts)."""
    toks = path_tokens(d)
    out = []
    i = 0
    cx = cy = sx = sy = 0.0
    last_ctrl = None
    last_cmd = ""
    cmd = None
    while i < len(toks):
        if isinstance(toks[i], str):
            cmd = toks[i]
            i += 1
            if cmd in "Zz":
                out.append(("p", [(cx, cy), (sx, sy)]))
                cx, cy = sx, sy
                last_cmd, last_ctrl = "z", None
                continue
        if cmd is None:
            raise InputError("path data does not start with a command")
        n = _ARGC[cmd.lower()]
        args = toks[i:i + n]
        if len(args) < n or any(isinstance(a, str) for a in args):
            break  # truncated path: SVG renders up to the error
        i += n
        rel = cmd.islower()
        c = cmd.lower()
        ox, oy = (cx, cy) if rel else (0.0, 0.0)
        if c == "m":
            cx, cy = ox + args[0], oy + args[1]
            sx, sy = cx, cy
            out.append(("p", [(cx, cy)]))
            cmd = "l" if rel else "L"  # implicit lineto after moveto
            last_ctrl = None
        elif c == "l":
            nx, ny = ox + args[0], oy + args[1]
            out.append(("p", [(cx, cy), (nx, ny)]))
            cx, cy, last_ctrl = nx, ny, None
        elif c == "h":
            nx = (cx if rel else 0.0) + args[0]
            out.append(("p", [(cx, cy), (nx, cy)]))
            cx, last_ctrl = nx, None
        elif c == "v":
            ny = (cy if rel else 0.0) + args[0]
            out.append(("p", [(cx, cy), (cx, ny)]))
            cy, last_ctrl = ny, None
        elif c in "cs":
            if c == "c":
                p1 = (ox + args[0], oy + args[1])
                p2 = (ox + args[2], oy + args[3])
                p3 = (ox + args[4], oy + args[5])
            else:
                if last_cmd in "cs" and last_ctrl:
                    p1 = (2 * cx - last_ctrl[0], 2 * cy - last_ctrl[1])
                else:
                    p1 = (cx, cy)
                p2 = (ox + args[0], oy + args[1])
                p3 = (ox + args[2], oy + args[3])
            out.append(("c", [(cx, cy), p1, p2, p3]))
            last_ctrl = p2
            cx, cy = p3
        elif c in "qt":
            if c == "q":
                q1 = (ox + args[0], oy + args[1])
                p3 = (ox + args[2], oy + args[3])
            else:
                if last_cmd in "qt" and last_ctrl:
                    q1 = (2 * cx - last_ctrl[0], 2 * cy - last_ctrl[1])
                else:
                    q1 = (cx, cy)
                p3 = (ox + args[0], oy + args[1])
            p1 = (cx + 2 / 3 * (q1[0] - cx), cy + 2 / 3 * (q1[1] - cy))
            p2 = (p3[0] + 2 / 3 * (q1[0] - p3[0]), p3[1] + 2 / 3 * (q1[1] - p3[1]))
            out.append(("c", [(cx, cy), p1, p2, p3]))
            last_ctrl = q1
            cx, cy = p3
        elif c == "a":
            nx, ny = ox + args[5], oy + args[6]
            out.append(("p", _arc_points(cx, cy, args[0], args[1], args[2],
                                         bool(args[3]), bool(args[4]), nx, ny)))
            cx, cy, last_ctrl = nx, ny, None
        last_cmd = c
    return out


def _ellipse_points(cx, cy, rx, ry, n=48):
    return [(cx + rx * math.cos(2 * math.pi * i / n), cy + ry * math.sin(2 * math.pi * i / n))
            for i in range(n)]


def bbox_of(points):
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def union(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


# ---------------------------------------------------------------- style

INHERITED = {"fill", "stroke", "stroke-width", "fill-opacity", "stroke-opacity",
             "font-size", "color", "visibility", "text-anchor"}
STYLE_PROPS = INHERITED | {"opacity", "display", "stop-color", "stop-opacity"}
DEFAULTS = {"fill": "black", "stroke": "none", "stroke-width": "1", "fill-opacity": "1",
            "stroke-opacity": "1", "font-size": "16", "color": "black", "visibility": "visible",
            "text-anchor": "start"}

_LEN_RE = re.compile(r"^\s*(" + _NUM + r")\s*(px|pt|pc|mm|cm|in|em|ex|%)?\s*$")
_CSS_PX = {"": 1.0, "px": 1.0, "pt": 4 / 3, "pc": 16.0, "mm": 96 / 25.4, "cm": 960 / 25.4, "in": 96.0}


def css_len(value, em=16.0):
    """CSS length in user units (px); None if not a plain length."""
    m = _LEN_RE.match(value or "")
    if not m:
        return None
    v, u = float(m.group(1)), (m.group(2) or "")
    if u in _CSS_PX:
        return v * _CSS_PX[u]
    if u == "em":
        return v * em
    if u == "ex":
        return v * em / 2
    if u == "%":
        return v * em / 100
    return None


def parse_decls(text):
    out = {}
    for decl in (text or "").split(";"):
        if ":" in decl:
            k, v = decl.split(":", 1)
            v = v.replace("!important", "").strip()
            out[k.strip().lower()] = v
    return out


_SIMPLE_SEL = re.compile(r"^([a-zA-Z][\w-]*)?((?:[.#][\w-]+)*)$")


def parse_css(text, findings):
    """Simple selectors only (tag, .class, #id and compounds). Others are reported."""
    rules = []
    text = re.sub(r"/\*.*?\*/", "", text or "", flags=re.S)
    text = re.sub(r"@[^{;]+;", "", text)
    order = 0
    for sels, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
        decls = parse_decls(body)
        for sel in sels.split(","):
            sel = sel.strip()
            if not sel or sel.startswith("@"):
                continue
            m = _SIMPLE_SEL.match(sel)
            if not m:
                findings.add("unsupported-css", "advisory",
                             "CSS selector not understood; styles under it were not checked", sel)
                continue
            tag = m.group(1)
            parts = re.findall(r"([.#])([\w-]+)", m.group(2))
            classes = {p for k, p in parts if k == "."}
            ids = {p for k, p in parts if k == "#"}
            spec = (len(ids), len(classes), 1 if tag else 0)
            rules.append((spec, order, tag, classes, ids, decls))
            order += 1
    rules.sort(key=lambda r: (r[0], r[1]))
    return rules


def css_for(el, tag, rules):
    out = {}
    classes = set((el.attrib.get("class") or "").split())
    eid = el.attrib.get("id")
    for _spec, _o, rtag, rclasses, rids, decls in rules:
        if rtag and rtag != tag:
            continue
        if rclasses and not rclasses <= classes:
            continue
        if rids and eid not in rids:
            continue
        out.update(decls)
    return out


# ---------------------------------------------------------------- color

NAMED = {"black": (0, 0, 0), "white": (255, 255, 255)}


def parse_color(v):
    """('none'|'url'|'rgb'|'unknown', value). An unknown value keeps its original case for messages."""
    orig = (v or "").strip()
    s = orig.lower()
    if s in ("none", "transparent"):
        return ("none", None)
    if s.startswith("url("):
        m = re.match(r"url\(\s*['\"]?#([^)'\"]+)['\"]?\s*\)", s)
        return ("url", m.group(1) if m else None)
    if s in NAMED:
        return ("rgb", NAMED[s])
    m = re.fullmatch(r"#([0-9a-f]{3,8})", s)
    if m:
        h = m.group(1)
        if len(h) in (3, 4):
            rgb = tuple(int(c * 2, 16) for c in h[:3])
            alpha = int(h[3] * 2, 16) / 255 if len(h) == 4 else 1.0
        elif len(h) in (6, 8):
            rgb = tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
            alpha = int(h[6:8], 16) / 255 if len(h) == 8 else 1.0
        else:
            return ("unknown", orig)
        return ("rgb", rgb if alpha >= 0.999 else rgb + (alpha,))
    m = re.fullmatch(r"rgba?\(([^)]*)\)", s)
    if m:
        parts = [p.strip() for p in re.split(r"[,\s/]+", m.group(1).strip()) if p.strip()]
        try:
            ch = [float(p[:-1]) * 2.55 if p.endswith("%") else float(p) for p in parts[:3]]
            alpha = 1.0
            if len(parts) > 3:
                a = parts[3]
                alpha = float(a[:-1]) / 100 if a.endswith("%") else float(a)
        except ValueError:
            return ("unknown", orig)
        rgb = tuple(int(round(c)) for c in ch)
        return ("rgb", rgb if alpha >= 0.999 else rgb + (alpha,))
    return ("unknown", orig)


def classify_ink(rgb):
    """'bw', 'near-black' or 'color'."""
    if len(rgb) == 4:
        rgb = rgb[:3]
    if rgb in ((0, 0, 0), (255, 255, 255)):
        return "bw"
    if max(rgb) <= NEAR_BLACK_MAX:
        return "near-black"
    return "color"


# ---------------------------------------------------------------- page

def _parse_dim(v):
    m = _LEN_RE.match(v or "")
    if not m or m.group(2) in ("%", "em", "ex"):
        return None
    return float(m.group(1)), (m.group(2) or "")


_ABS_PT = {"pt": 1.0, "pc": 12.0, "mm": PT_PER_MM, "cm": 10 * PT_PER_MM, "in": PT_PER_IN}


def match_sheet(w, h):
    for s in SHEETS.values():
        if abs(w - s.w) <= SIZE_TOL_PT and abs(h - s.h) <= SIZE_TOL_PT:
            return s, "portrait"
        if abs(w - s.h) <= SIZE_TOL_PT and abs(h - s.w) <= SIZE_TOL_PT:
            return s, "landscape"
    return None, None


def page_geometry(root):
    """Return (w_pt, h_pt, root_matrix_user_to_pt, units_note)."""
    vb = nums(root.attrib.get("viewBox"))
    vb = vb if len(vb) == 4 and vb[2] > 0 and vb[3] > 0 else None
    wd, hd = _parse_dim(root.attrib.get("width")), _parse_dim(root.attrib.get("height"))
    if wd is None or hd is None:
        if vb is None:
            raise InputError("SVG has neither width/height nor a viewBox, so the sheet size is unknown")
        wd, hd = (vb[2], ""), (vb[3], "")
    if wd[1] in _ABS_PT and hd[1] in _ABS_PT:
        w, h = wd[0] * _ABS_PT[wd[1]], hd[0] * _ABS_PT[hd[1]]
        note = f"width/height in {wd[1]}"
    else:
        # px or unitless: CSS says 96 px per inch; Illustrator and many PDF tools write 72.
        w96, h96 = wd[0] * 0.75, hd[0] * 0.75
        w72, h72 = wd[0], hd[0]
        if match_sheet(w96, h96)[0] is None and match_sheet(w72, h72)[0] is not None:
            w, h, note = w72, h72, "px or unitless read as points (72 per inch)"
        else:
            w, h, note = w96, h96, "px or unitless read as CSS pixels (96 per inch)"
    if vb is None:
        # No viewBox: one user unit is one CSS px (0.75 pt) whatever unit width/height use;
        # only for px/unitless sheets read at 72 per inch is a user unit one point.
        s = w / wd[0] if (wd[0] and wd[1] not in _ABS_PT) else 0.75
        return w, h, (s, 0, 0, s, 0, 0), note
    sx, sy = w / vb[2], h / vb[3]
    par = (root.attrib.get("preserveAspectRatio") or "xMidYMid meet").split()
    if par and par[0] == "none":
        return w, h, (sx, 0, 0, sy, -vb[0] * sx, -vb[1] * sy), note
    s = min(sx, sy) if (len(par) < 2 or par[1] == "meet") else max(sx, sy)
    align = par[0] if par else "xMidYMid"
    fx = {"xMin": 0.0, "xMid": 0.5, "xMax": 1.0}.get(align[:4], 0.5)
    fy = {"YMin": 0.0, "YMid": 0.5, "YMax": 1.0}.get(align[4:8], 0.5)
    tx = (w - vb[2] * s) * fx - vb[0] * s
    ty = (h - vb[3] * s) * fy - vb[1] * s
    return w, h, (s, 0, 0, s, tx, ty), note


# ---------------------------------------------------------------- parse

_ENTITY_RE = re.compile(rb"<!ENTITY", re.I)


def parse_svg(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    if len(data) > MAX_BYTES:
        raise InputError(f"file is larger than {MAX_BYTES // (1024 * 1024)} MB")
    if _ENTITY_RE.search(data):
        raise InputError("XML entity declarations are not accepted")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise InputError(f"not well-formed XML: {e}")
    if local(root.tag) != "svg":
        raise InputError(f"root element is <{local(root.tag)}>, not <svg>")
    n = sum(1 for _ in root.iter())
    if n > MAX_NODES:
        raise InputError(f"more than {MAX_NODES} elements")
    return root


def local(tag):
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def href(el):
    for k, v in el.attrib.items():
        if local(k) == "href":
            return v[1:] if v.startswith("#") else None
    return None


NON_RENDERED = {"defs", "clipPath", "mask", "symbol", "marker", "pattern", "linearGradient",
                "radialGradient", "style", "title", "desc", "metadata", "script", "filter",
                "font", "font-face", "foreignObject"}
SHAPES = {"rect", "circle", "ellipse", "line", "polyline", "polygon", "path"}

_SHEETNUM_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*$")
_SHEETWORD_RE = re.compile(r"\bsheet\s*\d+\s*(?:of|/)\s*\d+\b", re.I)
_FIG_OK_RE = re.compile(r"\bFIG\.\s?(\d+)([A-Z]?)\b")
_FIG_ANY_RE = re.compile(r"\b(FIGURE|Figure|figure|FIG|Fig|fig)\b\.?\s*(\d+)")


class _Walker:
    def __init__(self, root, findings):
        self.root = root
        self.f = findings
        self.ids = {}
        for el in root.iter():
            i = el.attrib.get("id")
            if i is not None and i not in self.ids:
                self.ids[i] = el
        css = "".join("".join(el.itertext()) for el in root.iter() if local(el.tag) == "style")
        self.css = parse_css(css, findings)
        self.geom = []      # (bbox_pt, label)
        self.rects = []     # (bbox_pt, stroked, label)
        self.texts = []     # (text, font_pt, bbox_pt, label)
        self.thin = 0
        self.checked_defs = set()
        self.images = 0

    def style(self, el, tag, parent_style):
        s = {k: v for k, v in parent_style.items() if k in INHERITED}
        for k in STYLE_PROPS:
            v = el.attrib.get(k)
            if v is not None:
                s[k] = v.strip()
        s.update({k: v for k, v in css_for(el, tag, self.css).items() if k in STYLE_PROPS})
        s.update({k: v for k, v in parse_decls(el.attrib.get("style")).items() if k in STYLE_PROPS})
        for k, v in list(s.items()):
            if v == "inherit":
                s[k] = parent_style.get(k, DEFAULTS.get(k, ""))
        return s

    def label(self, el, tag):
        i = el.attrib.get("id")
        return f"<{tag} id={i}>" if i else f"<{tag}>"

    def check_paint(self, kind, value, alpha, el, tag, color):
        if value.strip().lower() == "currentcolor":
            value = color
        typ, v = parse_color(value)
        lab = self.label(el, tag)
        if typ == "none":
            return None
        if typ == "url":
            ref = self.ids.get(v) if v else None
            if ref is None:
                self.f.add("unresolved-ref", "advisory", f"{kind} refers to a missing paint server", lab)
                return "dark"
            rtag = local(ref.tag)
            if rtag in ("linearGradient", "radialGradient"):
                self.f.add("gradient", "error",
                           "gradient fill or stroke: drawings must be solid black on white, shading by lines", lab)
            elif rtag == "pattern":
                self.check_defs(ref, rtag)
            return "dark"
        if typ == "unknown":
            self.f.add("ink", "error", f"{kind} colour {v!r} is not black or white", lab)
            return "dark"
        if len(v) == 4:
            alpha *= v[3]
        cls = classify_ink(v)
        if cls == "color":
            self.f.add("ink", "error", f"{kind} uses colour or grey #{v[0]:02x}{v[1]:02x}{v[2]:02x}; "
                       "only solid black and white are allowed", lab)
        elif cls == "near-black":
            self.f.add("near-black", "advisory", f"{kind} is near-black #{v[0]:02x}{v[1]:02x}{v[2]:02x}; "
                       "set it to pure black #000000", lab)
        if 0 < alpha < 0.999 and cls != "color" and v[:3] != (255, 255, 255):
            self.f.add("translucent", "error",
                       f"{kind} is translucent (opacity {alpha:.2f}) and will print grey", lab)
        if alpha <= 0:
            return None
        return "white" if v[:3] == (255, 255, 255) else "dark"

    def check_defs(self, el, tag):
        """Ink-check a pattern or marker's content once (it is drawn wherever it is used)."""
        key = id(el)
        if key in self.checked_defs:
            return
        self.checked_defs.add(key)
        dummy = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)  # zero matrix: no geometry is recorded
        for child in el:
            self.visit(child, dummy, dict(DEFAULTS), 1.0, 0, geometry=False)

    def visit(self, el, ctm, pstyle, popacity, depth, geometry=True):
        tag = local(el.tag)
        if not tag or tag in NON_RENDERED:
            return
        s = self.style(el, tag, pstyle)
        if s.get("display") == "none":
            return
        try:
            op = popacity * float(s.get("opacity", "1"))
        except ValueError:
            op = popacity
        if op <= 0:
            return
        m = mul(ctm, parse_transform(el.attrib.get("transform")))
        if tag in ("g", "svg", "a", "switch") and el is not self.root:
            if tag == "svg":
                x, y = css_len(el.attrib.get("x", "0")) or 0.0, css_len(el.attrib.get("y", "0")) or 0.0
                m = mul(ctm, (1, 0, 0, 1, x, y))
            for c in el:
                self.visit(c, m, s, op, depth, geometry)
            return
        if tag == "use":
            ref = self.ids.get(href(el) or "")
            if ref is None:
                self.f.add("unresolved-ref", "advisory", "<use> refers to a missing element", self.label(el, tag))
                return
            if depth >= MAX_USE_DEPTH:
                raise InputError("<use> references nest too deeply (possible reference loop)")
            x, y = css_len(el.attrib.get("x", "0")) or 0.0, css_len(el.attrib.get("y", "0")) or 0.0
            m2 = mul(m, (1, 0, 0, 1, x, y))
            if local(ref.tag) == "symbol":
                for c in ref:
                    self.visit(c, m2, s, op, depth + 1, geometry)
            else:
                self.visit(ref, m2, s, op, depth + 1, geometry)
            return
        if tag == "image":
            if geometry:
                self.images += 1
                self.f.add("raster-image", "advisory",
                           "embedded image: photographs are not ordinarily permitted, and its content cannot be checked",
                           self.label(el, tag))
                a = el.attrib
                x, y = css_len(a.get("x", "0")) or 0.0, css_len(a.get("y", "0")) or 0.0
                w, h = css_len(a.get("width", "0")) or 0.0, css_len(a.get("height", "0")) or 0.0
                self.add_geom([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], m, 0.0, self.label(el, tag))
            return
        visible = s.get("visibility", "visible") not in ("hidden", "collapse")
        if tag in SHAPES or tag == "text":
            self.shape(el, tag, s, m, op, visible, geometry)
        if tag in ("text", "tspan", "textPath"):
            for c in el:
                ct = local(c.tag)
                if ct in ("tspan", "textPath", "a"):
                    self.visit(c, m, s, op, depth, geometry)

    def add_geom(self, pts, m, half_sw, lab):
        tp = [apply(m, x, y) for x, y in pts]
        b = bbox_of(tp)
        if b is None:
            return None
        b = (b[0] - half_sw, b[1] - half_sw, b[2] + half_sw, b[3] + half_sw)
        self.geom.append((b, lab))
        return b

    def shape(self, el, tag, s, m, op, visible, geometry):
        lab = self.label(el, tag)
        color = s.get("color", "black")
        try:
            fop = float(s.get("fill-opacity", "1"))
            sop = float(s.get("stroke-opacity", "1"))
        except ValueError:
            fop = sop = 1.0
        # a <line> has no interior, so its fill is never painted
        fill_shown = (visible and tag != "line"
                      and self.check_paint("fill", s.get("fill", "black"), op * fop, el, tag, color))
        stroke_shown = visible and self.check_paint("stroke", s.get("stroke", "none"), op * sop, el, tag, color)
        for mk in ("marker-start", "marker-mid", "marker-end"):
            v = el.attrib.get(mk) or parse_decls(el.attrib.get("style")).get(mk)
            if v:
                typ, ref_id = parse_color(v)
                ref = self.ids.get(ref_id) if typ == "url" and ref_id else None
                if ref is not None and local(ref.tag) == "marker":
                    self.check_defs(ref, "marker")
        # white paint on a white sheet is invisible: it neither breaks margins nor forms a frame
        if stroke_shown == "white":
            stroke_shown = None
        if fill_shown == "white":
            fill_shown = None
        if not geometry or not (fill_shown or stroke_shown):
            return
        sc = scale_of(m)
        sw_user = css_len(s.get("stroke-width", "1"))
        sw_pt = (sw_user or 0.0) * sc if stroke_shown else 0.0
        if stroke_shown and sw_user is not None and 0 < sw_pt < THIN_LINE_PT:
            self.thin += 1
            self.f.add("thin-line", "advisory",
                       f"lines thinner than {THIN_LINE_PT} pt may not reproduce", lab)
        if tag == "text":
            self.text(el, s, m, lab)
            return
        a = el.attrib
        pts = []
        if tag == "rect":
            x, y = css_len(a.get("x", "0")) or 0.0, css_len(a.get("y", "0")) or 0.0
            w, h = css_len(a.get("width", "0")) or 0.0, css_len(a.get("height", "0")) or 0.0
            if w <= 0 or h <= 0:
                return
            pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
        elif tag == "circle":
            r = css_len(a.get("r", "0")) or 0.0
            if r <= 0:
                return
            pts = _ellipse_points(css_len(a.get("cx", "0")) or 0.0, css_len(a.get("cy", "0")) or 0.0, r, r)
        elif tag == "ellipse":
            rx, ry = css_len(a.get("rx", "0")) or 0.0, css_len(a.get("ry", "0")) or 0.0
            if rx <= 0 or ry <= 0:
                return
            pts = _ellipse_points(css_len(a.get("cx", "0")) or 0.0, css_len(a.get("cy", "0")) or 0.0, rx, ry)
        elif tag == "line":
            pts = [(css_len(a.get("x1", "0")) or 0.0, css_len(a.get("y1", "0")) or 0.0),
                   (css_len(a.get("x2", "0")) or 0.0, css_len(a.get("y2", "0")) or 0.0)]
        elif tag in ("polyline", "polygon"):
            v = nums(a.get("points"))
            pts = list(zip(v[0::2], v[1::2]))
        elif tag == "path":
            tp = []
            for kind, seg in path_segments(a.get("d")):
                seg_t = [apply(m, x, y) for x, y in seg]
                tp += _cubic_points(*seg_t) if kind == "c" else seg_t
            b = bbox_of(tp)
            if b is None:
                return
            h = sw_pt / 2
            b = (b[0] - h, b[1] - h, b[2] + h, b[3] + h)
            self.geom.append((b, lab))
            return
        b = self.add_geom(pts, m, sw_pt / 2, lab)
        if tag == "rect" and b is not None:
            self.rects.append((b, stroke_shown, lab))

    def text(self, el, s, m, lab):
        sc = scale_of(m)
        anchor = s.get("text-anchor", "start")

        def seg_box(x, y, txt, fs):
            w = max(len(txt), 1) * fs * 0.6
            x0 = x - w if anchor == "end" else (x - w / 2 if anchor == "middle" else x)
            return [(x0, y - fs), (x0 + w, y - fs), (x0 + w, y + 0.25 * fs), (x0, y + 0.25 * fs)]

        def walk(node, nstyle, x, y, out):
            fs = css_len(nstyle.get("font-size", "16"), 16.0) or 16.0
            xs, ys = nums(node.attrib.get("x")), nums(node.attrib.get("y"))
            x = xs[0] if xs else x
            y = ys[0] if ys else y
            direct = (node.text or "").strip()
            if direct:
                out.append((direct, fs, x, y))
            for c in node:
                ct = local(c.tag)
                if ct in ("tspan", "textPath", "a"):
                    cs = self.style(c, ct, nstyle)
                    if cs.get("display") != "none":
                        walk(c, cs, x, y, out)
                tail = (c.tail or "").strip()
                if tail:
                    out.append((tail, fs, x, y))

        segs = []
        walk(el, s, 0.0, 0.0, segs)
        full = " ".join(t for t, *_ in segs).strip()
        if not full:
            return
        allpts = []
        for txt, fs, x, y in segs:
            pts = seg_box(x, y, txt, fs)
            allpts += pts
            fpt = fs * sc
            if fpt < MIN_TEXT_PT - 1e-6:
                self.f.add("text-height", "error",
                           f"text below 1/8 inch: font size {fpt:.1f} pt is under {MIN_TEXT_PT:.0f} pt",
                           f"{lab} {txt[:20]!r}")
        b = self.add_geom(allpts, m, 0.0, lab)
        max_fs = max(fs for _, fs, _, _ in segs) * sc
        self.texts.append((full, max_fs, b, lab))


def lint_sheet(data, name="sheet.svg", profile="auto"):
    """Lint one SVG sheet. profile: 'auto', 'letter' or 'a4'. Raises InputError."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    sha = hashlib.sha256(raw).hexdigest()
    root = parse_svg(raw)
    findings = Findings()
    w, h, root_m, note = page_geometry(root)
    sheet, orient = match_sheet(w, h)
    page = {"width_pt": round(w, 2), "height_pt": round(h, 2), "size": sheet.name if sheet else None,
            "orientation": orient, "units": note}
    if sheet is None:
        findings.add("page-size", "error",
                     f"sheet is {w / PT_PER_MM:.1f} x {h / PT_PER_MM:.1f} mm; must be A4 (210 x 297 mm) "
                     "or letter (8 1/2 x 11 in)")
    elif orient == "landscape":
        findings.add("page-size", "error",
                     "sheet is landscape; a short side must be the top (rotate the view, not the sheet)")
    if profile != "auto" and sheet is not None and sheet.name != profile:
        findings.add("page-size", "error", f"sheet is {sheet.name} but {profile} was requested")
    ref = sheet if (sheet and orient == "portrait") else (SHEETS[profile] if profile in SHEETS else None)

    wk = _Walker(root, findings)
    for c in root:
        wk.visit(c, mul(root_m, parse_transform(root.attrib.get("transform"))),
                 wk.style(root, "svg", dict(DEFAULTS)), 1.0, 0)

    if ref is not None:
        x0, y0, x1, y1 = ref.sight
        t = BOUNDS_TOL_PT
        for b, lab in wk.geom:
            if b[0] < x0 - t or b[1] < y0 - t or b[2] > x1 + t or b[3] > y1 + t:
                side = []
                if b[1] < y0 - t:
                    side.append("top")
                if b[0] < x0 - t:
                    side.append("left")
                if b[2] > x1 + t:
                    side.append("right")
                if b[3] > y1 + t:
                    side.append("bottom")
                findings.add("margins", "error",
                             f"drawing enters the {'/'.join(side)} margin (margins: top 1 in / 2.5 cm, "
                             "left 1 in / 2.5 cm, right 5/8 in / 1.5 cm, bottom 3/8 in / 1.0 cm)", lab)
        sight_area = (x1 - x0) * (y1 - y0)
        for b, stroked, lab in wk.rects:
            if not stroked:
                continue
            area = (b[2] - b[0]) * (b[3] - b[1])
            if area >= 0.9 * sight_area and b[0] <= x0 + 20 and b[1] <= y0 + 20 \
                    and b[2] >= x1 - 20 and b[3] >= y1 - 20:
                findings.add("frame", "error", "border around the usable area: frames around the sight are not allowed", lab)

    if not wk.texts:
        findings.add("no-live-text", "advisory",
                     "no live text found; if labels were converted to outlines, their height was not checked")

    sheet_number = None
    numeric_sizes = []
    for txt, fpt, b, lab in wk.texts:
        m = _SHEETNUM_RE.match(txt)
        if m:
            sheet_number = (int(m.group(1)), int(m.group(2)))
            if ref is not None and b is not None:
                x0, y0, x1, y1 = ref.sight
                if b[1] > y0 + 0.25 * (y1 - y0):
                    findings.add("sheet-number-position", "advisory",
                                 "sheet number should sit at the top middle (or top right) of the usable area", lab)
            sn_size = fpt
            continue
        if _SHEETWORD_RE.search(txt):
            findings.add("sheet-number-format", "error",
                         f"sheet number {txt!r}: use two numerals either side of a slash (for example 1/3), "
                         "with no other marking", lab)
        if re.fullmatch(r"\d+[A-Za-z']?", txt):
            numeric_sizes.append(fpt)
        for mm in _FIG_ANY_RE.finditer(txt):
            if not _FIG_OK_RE.match(txt, mm.start()):
                findings.add("fig-label-format", "advisory",
                             f"view label {mm.group(0)!r}: views are labelled with the abbreviation 'FIG.'", lab)
    if sheet_number is None and not any(f.code == "sheet-number-format" for f in findings.list()):
        findings.add("sheet-number-missing", "advisory",
                     "no sheet number (n/m) found; sheets should be numbered at the top middle")
    if sheet_number is not None:
        if sheet_number[0] < 1 or sheet_number[0] > sheet_number[1]:
            findings.add("sheet-number-sequence", "error",
                         f"sheet number {sheet_number[0]}/{sheet_number[1]} is out of range")
        if numeric_sizes and sn_size <= max(numeric_sizes):
            findings.add("sheet-number-size", "advisory",
                         "sheet number must be larger than the reference numerals")

    return SheetReport(name, sha, page, findings.list(),
                       [(t, f, b) for t, f, b, _ in wk.texts], sheet_number)


def lint_set(reports, numerals=None):
    """Checks that need the whole set of sheets. Returns a list of Finding."""
    f = Findings()
    sizes = {r.page["size"] for r in reports if r.page["size"]}
    if len(sizes) > 1:
        f.add("mixed-sizes", "error", f"sheets use different sizes ({', '.join(sorted(sizes))}); all must match")
    nums_found = [r.sheet_number for r in reports if r.sheet_number]
    if len(reports) > 1 and nums_found:
        totals = {m for _, m in nums_found}
        seen = [n for n, _ in nums_found]  # in the order the sheets were given
        if len(totals) > 1:
            f.add("sheet-number-sequence", "error", f"sheets disagree on the total ({sorted(totals)})")
        elif len(nums_found) == len(reports):
            total = totals.pop()
            if total != len(reports) or seen != list(range(1, len(reports) + 1)):
                f.add("sheet-number-sequence", "error",
                      f"sheet numbers in order {seen} are not 1..{len(reports)} of {total}")
    figs = []
    for r in reports:
        for t, _, _ in r.texts:
            for m in _FIG_OK_RE.finditer(t):
                figs.append((int(m.group(1)), m.group(2)))
    if figs:
        main = sorted({n for n, _ in figs})
        if main != list(range(1, main[-1] + 1)):
            f.add("fig-sequence", "error", f"view numbers {main} skip or do not start at 1")
        if len(figs) == 1 and len(reports) >= 1:
            f.add("single-view", "advisory",
                  "only one view found: if it is the only view in the application, it must not be numbered "
                  "and 'FIG.' must not appear")
    if numerals:
        # view labels ("FIG. 10") and sheet numbers ("1/3") are not reference numerals
        alltext = " ".join(_FIG_ANY_RE.sub(" ", t) for r in reports for t, _, _ in r.texts
                           if not _SHEETNUM_RE.match(t))
        for n in numerals:
            if not re.search(rf"(?<![\w.]){re.escape(str(n))}(?![\w])", alltext):
                f.add("numeral-missing", "error", f"reference numeral {n} does not appear on any sheet")
    return f.list()


def report(reports, set_findings):
    errors = sum(r.errors for r in reports) + sum(1 for x in set_findings if x.severity == "error")
    return {"tool": "drawlint", "version": VERSION, "rules": "USPTO 37 CFR 1.84 (drawings)",
            "disclaimer": DISCLAIMER, "pass": errors == 0, "errors": errors,
            "sheets": [r.to_dict() for r in reports],
            "set_findings": [x.to_dict() for x in set_findings]}
