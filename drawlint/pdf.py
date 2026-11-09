"""PDF intake: turn each PDF page into an equivalent SVG sheet for the same checks.

The page's content stream is interpreted directly (pypdf parses the file and decodes
fonts; nothing is rendered or rasterised). Every painted path, text run, image,
shading and pattern becomes an SVG element in page points, with its full transform,
paint, opacity and line width, so engine.lint_sheet checks it like any SVG.

Page size: the CropBox (the visible page; MediaBox when absent), with /Rotate and
/UserUnit applied. Not interpreted: clipping paths (geometry outside a clip still
counts), optional-content visibility (hidden layers are checked), soft masks,
annotations. Text width is estimated from the font's glyph widths.
"""

from __future__ import annotations

import io
import logging
import math
import threading
from xml.sax.saxutils import escape, quoteattr

from . import engine

try:
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError
    from pypdf.generic import ContentStream, IndirectObject
    from pypdf.generic._font import Font
except ImportError:  # the SVG path needs nothing beyond the standard library
    PdfReader = None

MAX_PAGES = 100
MAX_FORM_DEPTH = 8
MAX_ELEMENTS = engine.MAX_NODES

IDENT = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


class _Repairs(logging.Handler):
    """pypdf reports problems it repaired in the customer's file through logging. Collect them
    per thread so they reach that file's report instead of the server's stderr."""

    def __init__(self):
        super().__init__(logging.WARNING)
        self.local = threading.local()

    def start(self):
        self.local.items = []

    def stop(self):
        items, self.local.items = getattr(self.local, "items", []), None
        return items

    def emit(self, record):
        items = getattr(self.local, "items", None)
        if items is None:  # pypdf used outside pdf_pages: nothing to attach it to
            logging.lastResort.handle(record)
            return
        msg = record.getMessage()
        if msg not in items:
            items.append(msg)


_REPAIRS = _Repairs()
_pypdf_log = logging.getLogger("pypdf")
_pypdf_log.addHandler(_REPAIRS)
_pypdf_log.propagate = False


def is_pdf(data: bytes) -> bool:
    return b"%PDF-" in data[:1024]


def then(a, b):
    """Matrix that applies a, then b (PDF row-vector order: a x b)."""
    return (a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3],
            a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5])


def _pt(m, x, y):
    return m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]


def _f(v):
    return f"{v:.4f}".rstrip("0").rstrip(".") or "0"


def _mat(m):
    return "matrix(" + " ".join(_f(v) for v in m) + ")"


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _obj(o):
    return o.get_object() if isinstance(o, IndirectObject) else o


# ---------------------------------------------------------------- colour

def _hex(rgb):
    r, g, b = (max(0, min(255, round(c * 255))) for c in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def _gray(g):
    return _hex((g, g, g))


def _cmyk(c, m, y, k):
    return _hex(((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k)))


class ColorSpace:
    """Resolve colour operands to an SVG paint string (hex, a named spot ink, or url())."""

    def __init__(self, cs, resources):
        self.kind, self.n, self.base, self.lookup, self.names = "DeviceGray", 1, None, None, None
        cs = _obj(cs)
        name = cs
        if isinstance(cs, list) and cs:
            name = str(_obj(cs[0]))
        elif isinstance(cs, str) and cs not in ("/DeviceGray", "/DeviceRGB", "/DeviceCMYK", "/Pattern",
                                                 "/G", "/RGB", "/CMYK"):
            named = _obj(_obj(resources.get("/ColorSpace", {})).get(cs)) if resources else None
            if named is not None:
                self.__init__(named, resources)
                return
        name = str(name)
        if name in ("/DeviceGray", "/G", "/CalGray"):
            self.kind, self.n = "gray", 1
        elif name in ("/DeviceRGB", "/RGB", "/CalRGB", "/Lab"):
            self.kind, self.n = "rgb", 3
        elif name in ("/DeviceCMYK", "/CMYK"):
            self.kind, self.n = "cmyk", 4
        elif name == "/ICCBased":
            n = int(_num(_obj(cs[1]).get("/N", 3)))
            self.kind, self.n = {1: "gray", 3: "rgb", 4: "cmyk"}.get(n, "rgb"), n
        elif name in ("/Indexed", "/I"):
            self.kind, self.n = "indexed", 1
            self.base = ColorSpace(cs[1], resources)
            look = _obj(cs[3])
            if hasattr(look, "get_data"):
                self.lookup = look.get_data()
            elif hasattr(look, "original_bytes"):
                self.lookup = look.original_bytes
            else:
                self.lookup = bytes(look)
        elif name in ("/Separation", "/DeviceN"):
            names = _obj(cs[1])
            names = [str(_obj(x)) for x in names] if isinstance(names, list) else [str(names)]
            self.kind, self.n, self.names = "spot", len(names), names
        elif name == "/Pattern":
            self.kind, self.n = "pattern", 0
            if isinstance(cs, list) and len(cs) > 1:  # uncoloured patterns: colour comes in this space
                self.base = ColorSpace(cs[1], resources)
        else:
            self.kind, self.n = "rgb", 3

    def initial(self):
        if self.kind == "cmyk":
            return _cmyk(0, 0, 0, 1)
        if self.kind == "spot":
            return self.paint([1.0] * self.n)
        if self.kind == "indexed":
            return self.paint([0])
        return "#000000"

    def paint(self, comps):
        c = [_num(x) for x in comps if not isinstance(x, str)]
        if self.kind == "gray":
            return _gray(c[0] if c else 0.0)
        if self.kind == "rgb":
            c = (c + [0.0, 0.0, 0.0])[:3]
            return _hex(c)
        if self.kind == "cmyk":
            c = (c + [0.0, 0.0, 0.0, 1.0])[:4]
            return _cmyk(*c)
        if self.kind == "indexed":
            i = int(c[0]) if c else 0
            n = self.base.n
            raw = self.lookup[i * n:(i + 1) * n]
            return self.base.paint([b / 255 for b in raw])
        if self.kind == "spot":
            tints = c or [1.0]
            if all(t <= 0 for t in tints):
                return "#ffffff"
            inked = [nm for nm, t in zip(self.names, tints) if t > 0]
            if all(nm in ("/Black", "/All") for nm in inked):
                return _gray(1.0 - max(tints))
            return "spot-ink:" + ",".join(nm.lstrip("/") for nm in inked)
        return "#000000"


# ---------------------------------------------------------------- state

class GState:
    __slots__ = ("ctm", "lw", "fill", "stroke", "fcs", "scs", "fa", "sa",
                 "tc", "tw", "th", "tl", "tf", "tfs", "tr", "trise")

    def __init__(self):
        self.ctm = IDENT
        self.lw = 1.0
        self.fill = self.stroke = "#000000"
        self.fcs = self.scs = None
        self.fa = self.sa = 1.0
        self.tc = self.tw = 0.0
        self.th = 1.0
        self.tl = 0.0
        self.tf = None
        self.tfs = 0.0
        self.tr = 0
        self.trise = 0.0

    def copy(self):
        g = GState.__new__(GState)
        for k in GState.__slots__:
            setattr(g, k, getattr(self, k))
        return g


class Page:
    """Interpret one page into a list of SVG element strings."""

    def __init__(self, reader, page, page_index):
        self.reader = reader
        self.page_index = page_index
        self.elements = []
        self.defs = []
        self.runs = []          # pending text runs, merged and flushed at ET
        self.fonts = {}
        self._ids = 0
        box = page.cropbox if "/CropBox" in page else page.mediabox
        uu = _num(page.get("/UserUnit", 1.0), 1.0) or 1.0
        x0, y0, x1, y1 = (float(v) for v in (box.left, box.bottom, box.right, box.top))
        rot = int(_num(page.get("/Rotate", 0))) % 360
        w, h = (x1 - x0) * uu, (y1 - y0) * uu
        if rot == 90:
            flip = (0, 1, 1, 0, -y0, -x0)
            w, h = h, w
        elif rot == 180:
            flip = (-1, 0, 0, 1, x1, -y0)
        elif rot == 270:
            flip = (0, -1, -1, 0, y1, x1)
            w, h = h, w
        else:
            flip = (1, 0, 0, -1, -x0, y1)
        self.base = then(flip, (uu, 0, 0, uu, 0, 0))
        self.w, self.h = w, h
        self.page = page

    # ------------------------------------------------------------ output

    def _id(self, prefix):
        self._ids += 1
        return f"{prefix}{self.page_index}_{self._ids}"

    def emit(self, s):
        if len(self.elements) + len(self.defs) >= MAX_ELEMENTS:
            raise engine.InputError(f"page {self.page_index + 1}: more than {MAX_ELEMENTS} drawing elements")
        self.elements.append(s)

    def device(self, m):
        return then(m, self.base)

    @staticmethod
    def _paint_attr(name, paint, alpha):
        a = f" {name}={quoteattr(paint)}"
        if alpha < 0.999:
            a += f" {name}-opacity=\"{_f(max(alpha, 0.0))}\""
        return a

    # ------------------------------------------------------------ painting

    def paint_path(self, gs, d, fill, stroke, evenodd=False, rect=None):
        if not d:
            return
        tf = f" transform=\"{_mat(self.device(gs.ctm))}\""
        if rect is not None:  # a lone rectangle stays a rect, so the frame check sees it
            x, y, w, h = rect
            x, w = (x + w, -w) if w < 0 else (x, w)
            y, h = (y + h, -h) if h < 0 else (y, h)
            attrs = f" x=\"{_f(x)}\" y=\"{_f(y)}\" width=\"{_f(w)}\" height=\"{_f(h)}\"" + tf
            tag = "rect"
        else:
            attrs = f" d={quoteattr(d)}" + tf
            tag = "path"
        attrs += self._paint_attr("fill", gs.fill, gs.fa) if fill else " fill=\"none\""
        if fill and evenodd:
            attrs += " fill-rule=\"evenodd\""
        if stroke:
            attrs += self._paint_attr("stroke", gs.stroke, gs.sa)
            # a zero-width PDF line is the thinnest line the device can draw
            attrs += f" stroke-width=\"{_f(gs.lw if gs.lw > 0 else 0.01)}\""
        self.emit(f"<{tag}{attrs}/>")

    def image(self, gs, label):
        m = then((1, 0, 0, -1, 0, 1), self.device(gs.ctm))
        self.emit(f"<image id={quoteattr(self._id(label))} x=\"0\" y=\"0\" width=\"1\" height=\"1\" "
                  f"transform=\"{_mat(m)}\" href=\"data:,\"/>")

    def gradient(self, gs):
        gid = self._id("shade")
        self.defs.append(f"<linearGradient id=\"{gid}\"><stop offset=\"0\" stop-color=\"#000000\"/>"
                         f"<stop offset=\"1\" stop-color=\"#ffffff\"/></linearGradient>")
        return f"url(#{gid})"

    def pattern(self, pat, gs, resources, depth, uncolored_paint):
        pat = _obj(pat)
        if pat is None:
            return "#000000"
        if int(_num(pat.get("/PatternType", 1))) == 2:
            return self.gradient(gs)
        if uncolored_paint is not None:      # PaintType 2: the pattern is a stencil in this colour
            return uncolored_paint
        pid = self._id("pat")
        sub = Page.__new__(Page)
        sub.__dict__.update(self.__dict__)
        sub.elements, sub.runs = [], []
        sub.base = IDENT
        sub.run(ContentStream(pat, self.reader), _obj(pat.get("/Resources")) or resources, GState(), depth + 1)
        sub.flush_text()
        self.defs.append(f"<pattern id=\"{pid}\">" + "".join(sub.elements) + "</pattern>")
        return f"url(#{pid})"

    # ------------------------------------------------------------ text

    def font(self, resources, name):
        fd = _obj(_obj(resources.get("/Font", {})).get(name)) if resources else None
        if fd is None:
            return None
        key = id(fd)
        if key not in self.fonts:
            self.fonts[key] = Font.from_font_resource(fd)
        return self.fonts[key]

    def show(self, gs, tm, raw):
        """Show one string; return the new text matrix."""
        font = gs.tf
        if font is None:
            return tm
        if isinstance(raw, str):  # pypdf decoded the literal; the font needs the raw codes
            raw = raw.original_bytes
        if isinstance(font.encoding, str):
            text = bytes(raw).decode(font.encoding, "replace")
            chars = text
        else:
            text = "".join(font.encoding.get(b, chr(b)) for b in bytes(raw))
            chars = "".join(chr(b) for b in bytes(raw))
        uni = "".join(font.character_map.get(c, c) for c in chars) if font.character_map else text
        adv = 0.0
        for c, u in zip(chars, uni if len(uni) == len(chars) else chars):
            w0 = font.space_width if c == font.space_char else font.get_text_width(c)
            adv += (w0 / 1000.0 * gs.tfs + gs.tc + (gs.tw if u == " " else 0.0)) * gs.th
        start = then((1, 0, 0, 1, 0, gs.trise), then(tm, gs.ctm))
        new_tm = then((1, 0, 0, 1, adv, 0), tm)
        if gs.tr in (3, 7) or not uni.strip():
            return new_tm
        dev = self.device(start)
        size = gs.tfs * math.hypot(dev[2], dev[3])
        if size <= 0:
            return new_tm
        end = _pt(self.device(then((1, 0, 0, 1, 0, gs.trise), then(new_tm, gs.ctm))), 0, 0)
        fill = gs.tr in (0, 2, 4, 6)
        stroke = gs.tr in (1, 2, 5, 6)
        self.runs.append({"text": uni, "m": dev, "start": _pt(dev, 0, 0), "end": end, "size": size,
                          "tfs": gs.tfs, "paint": (fill and gs.fill, fill and gs.fa, stroke and gs.stroke,
                                                   stroke and gs.sa,
                                                   gs.lw * engine.scale_of(self.device(gs.ctm)))})
        return new_tm

    def flush_text(self):
        merged = []
        for r in self.runs:
            p = merged[-1] if merged else None
            if p and abs(p["size"] - r["size"]) <= 0.01 * p["size"] and p["paint"] == r["paint"] \
                    and all(abs(a - b) <= 0.01 * max(abs(a), abs(b), 1e-9) for a, b in zip(p["m"][:4], r["m"][:4])):
                ux, uy = p["m"][0], p["m"][1]
                un = math.hypot(ux, uy) or 1.0
                ux, uy = ux / un, uy / un
                dx, dy = r["start"][0] - p["end"][0], r["start"][1] - p["end"][1]
                gap = (dx * ux + dy * uy) / p["size"]
                ox, oy = r["start"][0] - p["start"][0], r["start"][1] - p["start"][1]
                off = abs(-ox * uy + oy * ux) / p["size"]
                if off <= 0.2 and -0.3 <= gap <= 0.6:
                    sep = " " if gap > 0.15 and not p["text"].endswith(" ") and not r["text"].startswith(" ") else ""
                    p["text"] += sep + r["text"]
                    p["end"] = r["end"]
                    continue
            merged.append(dict(r))
        for r in merged:
            # SVG text is drawn y-down: flip the glyph space so the run reads upright on the sheet
            m = then((1, 0, 0, -1, 0, 0), r["m"])
            fill, fa, stroke, sa, lw_pt = r["paint"]
            attrs = f" x=\"0\" y=\"0\" font-size=\"{_f(r['tfs'])}\" transform=\"{_mat(m)}\""
            attrs += self._paint_attr("fill", fill, fa) if fill else " fill=\"none\""
            if stroke:
                sc = engine.scale_of(m) or 1.0
                attrs += self._paint_attr("stroke", stroke, sa) + f" stroke-width=\"{_f((lw_pt or 0.01) / sc)}\""
            self.emit(f"<text{attrs}>{escape(r['text'].strip())}</text>")
        self.runs = []

    # ------------------------------------------------------------ interpreter

    def run(self, content, resources, gs, depth):
        if depth > MAX_FORM_DEPTH:
            raise engine.InputError(f"page {self.page_index + 1}: form XObjects nest too deeply")
        resources = _obj(resources) or {}
        stack = []
        d = []
        rects = []  # the path is a lone rectangle while this holds exactly one re and nothing else
        cur = (0.0, 0.0)
        start = (0.0, 0.0)
        tm = tlm = IDENT
        for operands, op in content.operations:
            op = op.decode("latin-1") if isinstance(op, bytes) else op
            o = operands
            if op == "q":
                stack.append(gs.copy())
            elif op == "Q":
                if stack:
                    gs = stack.pop()
            elif op == "cm":
                gs.ctm = then(tuple(_num(x) for x in o[:6]), gs.ctm)
            elif op == "w":
                gs.lw = _num(o[0], 1.0)
            elif op == "gs":
                eg = _obj(_obj(resources.get("/ExtGState", {})).get(o[0]))
                if eg is not None:
                    if "/LW" in eg:
                        gs.lw = _num(eg["/LW"], gs.lw)
                    if "/CA" in eg:
                        gs.sa = _num(eg["/CA"], 1.0)
                    if "/ca" in eg:
                        gs.fa = _num(eg["/ca"], 1.0)
            # colour
            elif op in ("g", "G"):
                cs = ColorSpace("/DeviceGray", resources)
                setattr(gs, "fill" if op == "g" else "stroke", cs.paint(o))
                setattr(gs, "fcs" if op == "g" else "scs", cs)
            elif op in ("rg", "RG"):
                cs = ColorSpace("/DeviceRGB", resources)
                setattr(gs, "fill" if op == "rg" else "stroke", cs.paint(o))
                setattr(gs, "fcs" if op == "rg" else "scs", cs)
            elif op in ("k", "K"):
                cs = ColorSpace("/DeviceCMYK", resources)
                setattr(gs, "fill" if op == "k" else "stroke", cs.paint(o))
                setattr(gs, "fcs" if op == "k" else "scs", cs)
            elif op in ("cs", "CS"):
                cs = ColorSpace(o[0], resources)
                setattr(gs, "fcs" if op == "cs" else "scs", cs)
                setattr(gs, "fill" if op == "cs" else "stroke", cs.initial())
            elif op in ("sc", "scn", "SC", "SCN"):
                fill = op in ("sc", "scn")
                cs = (gs.fcs if fill else gs.scs) or ColorSpace("/DeviceGray", resources)
                if cs.kind == "pattern" and o and isinstance(o[-1], str):
                    under = (cs.base or ColorSpace("/DeviceRGB", resources)).paint(o[:-1]) if len(o) > 1 else None
                    pat = _obj(_obj(resources.get("/Pattern", {})).get(o[-1]))
                    paint = self.pattern(pat, gs, resources, depth, under)
                else:
                    paint = cs.paint(o)
                setattr(gs, "fill" if fill else "stroke", paint)
            # path construction
            elif op == "m":
                cur = start = (_num(o[0]), _num(o[1]))
                d.append(f"M{_f(cur[0])} {_f(cur[1])}")
                rects.append(None)
            elif op == "l":
                cur = (_num(o[0]), _num(o[1]))
                d.append(f"L{_f(cur[0])} {_f(cur[1])}")
            elif op == "c":
                v = [_num(x) for x in o[:6]]
                d.append("C" + " ".join(_f(x) for x in v))
                cur = (v[4], v[5])
            elif op == "v":
                v = [_num(x) for x in o[:4]]
                d.append("C" + " ".join(_f(x) for x in (cur[0], cur[1], *v)))
                cur = (v[2], v[3])
            elif op == "y":
                v = [_num(x) for x in o[:4]]
                d.append("C" + " ".join(_f(x) for x in (*v, v[2], v[3])))
                cur = (v[2], v[3])
            elif op == "h":
                d.append("Z")
                cur = start
            elif op == "re":
                x, y, w, h = (_num(v) for v in o[:4])
                d.append(f"M{_f(x)} {_f(y)}h{_f(w)}v{_f(h)}h{_f(-w)}Z")
                rects.append((x, y, w, h))
                cur = start = (x, y)
            # path painting
            elif op in ("S", "s", "f", "F", "f*", "B", "B*", "b", "b*", "n"):
                if op in ("s", "b", "b*"):
                    d.append("Z")
                if op != "n":
                    lone = rects[0] if len(rects) == 1 and len(d) == 1 else None
                    self.paint_path(gs, "".join(d), fill=op not in ("S", "s"),
                                    stroke=op in ("S", "s", "B", "B*", "b", "b*"), evenodd="*" in op, rect=lone)
                d = []
                rects = []
            elif op == "sh":
                paint = self.gradient(gs)
                self.emit(f"<rect width=\"0\" height=\"0\" fill=\"{paint}\"/>")
            # XObjects and inline images
            elif op == "Do":
                xo = _obj(_obj(resources.get("/XObject", {})).get(o[0]))
                if xo is None:
                    continue
                sub = str(xo.get("/Subtype"))
                if sub == "/Image":
                    self.image(gs, "img")
                elif sub == "/Form":
                    inner = gs.copy()
                    if "/Matrix" in xo:
                        inner.ctm = then(tuple(_num(x) for x in xo["/Matrix"]), gs.ctm)
                    self.run(ContentStream(xo, self.reader), _obj(xo.get("/Resources")) or resources,
                             inner, depth + 1)
            elif op == "INLINE IMAGE":
                self.image(gs, "inline")
            # text
            elif op == "BT":
                tm = tlm = IDENT
            elif op == "ET":
                pass  # runs merge across text objects (many writers put each glyph in its own BT/ET)
            elif op == "Tf":
                gs.tf = self.font(resources, o[0])
                gs.tfs = _num(o[1])
            elif op == "Tc":
                gs.tc = _num(o[0])
            elif op == "Tw":
                gs.tw = _num(o[0])
            elif op == "Tz":
                gs.th = _num(o[0], 100.0) / 100.0
            elif op == "TL":
                gs.tl = _num(o[0])
            elif op == "Ts":
                gs.trise = _num(o[0])
            elif op == "Tr":
                gs.tr = int(_num(o[0]))
            elif op == "Tm":
                tm = tlm = tuple(_num(x) for x in o[:6])
            elif op in ("Td", "TD"):
                tx, ty = _num(o[0]), _num(o[1])
                if op == "TD":
                    gs.tl = -ty
                tm = tlm = then((1, 0, 0, 1, tx, ty), tlm)
            elif op == "T*":
                tm = tlm = then((1, 0, 0, 1, 0, -gs.tl), tlm)
            elif op == "Tj":
                tm = self.show(gs, tm, o[0])
            elif op == "TJ":
                for item in o[0]:
                    if isinstance(item, (int, float)):
                        tm = then((1, 0, 0, 1, -_num(item) / 1000.0 * gs.tfs * gs.th, 0), tm)
                    else:
                        tm = self.show(gs, tm, item)
            elif op in ("'", '"'):
                if op == '"':
                    gs.tw, gs.tc = _num(o[0]), _num(o[1])
                tm = tlm = then((1, 0, 0, 1, 0, -gs.tl), tlm)
                tm = self.show(gs, tm, o[-1])
        if self.runs and depth == 0:
            self.flush_text()

    def svg(self):
        content = self.page.get_contents()
        self.run(content if content is not None else _Empty(), self.page.get("/Resources"), GState(), 0)
        defs = f"<defs>{''.join(self.defs)}</defs>" if self.defs else ""
        return (f"<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"{_f(self.w)}pt\" height=\"{_f(self.h)}pt\" "
                f"viewBox=\"0 0 {_f(self.w)} {_f(self.h)}\">{defs}{''.join(self.elements)}</svg>")


class _Empty:
    operations = []


def pdf_pages(data: bytes):
    """Return ([(page_number, svg_text)], repairs) where repairs lists problems pypdf
    fixed while reading the file. Raises engine.InputError."""
    if PdfReader is None:
        raise engine.InputError("PDF input needs the pypdf package (pip install pypdf==6.21.4)")
    if len(data) > engine.MAX_BYTES:
        raise engine.InputError(f"file is larger than {engine.MAX_BYTES // (1024 * 1024)} MB")
    _REPAIRS.start()
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise engine.InputError("the PDF is encrypted; save an unprotected copy")
        n = len(reader.pages)
        if n == 0:
            raise engine.InputError("the PDF has no pages")
        if n > MAX_PAGES:
            raise engine.TooManySheets(f"the PDF has {n} pages; at most {MAX_PAGES} are checked per file")
        out = []
        for i, page in enumerate(reader.pages):
            out.append((i + 1, Page(reader, page, i).svg()))
    except engine.InputError:
        raise
    except (PyPdfError, ValueError, KeyError, TypeError, IndexError, AttributeError) as e:
        raise engine.InputError(f"could not read the PDF: {type(e).__name__}: {e}")
    finally:
        repairs = _REPAIRS.stop()
    return out, repairs
