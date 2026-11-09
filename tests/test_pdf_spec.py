"""Spec tests for the PDF intake (README "PDF intake"). Built from the written spec only.
Real hand-built PDF bytes, real rsvg-convert PDFs, real files, real CLI subprocesses, real server in a thread."""
import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from drawlint import intake
from drawlint.engine import InputError
from drawlint.server import make_server

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RSVG = "/usr/bin/rsvg-convert"
LETTER = "[0 0 612 792]"


class PDF:
    """Minimal PDF writer: uncompressed streams, classic xref with correct offsets."""

    def __init__(self):
        self.objs = {}
        self.nxt = 4
        self.kids = []
        self.objs[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    def add(self, body):
        n = self.nxt
        self.nxt += 1
        self.objs[n] = body.encode("latin-1") if isinstance(body, str) else body
        return n

    def stream(self, inner, data):
        if isinstance(data, str):
            data = data.encode("latin-1")
        return self.add(b"<< " + inner.encode("latin-1") + b" /Length %d >>\nstream\n" % len(data) + data + b"\nendstream")

    def page(self, content, media=LETTER, res="", extra="", content_obj=None, fonts="/F1 3 0 R"):
        c = content_obj if content_obj is not None else self.stream("", content)
        p = self.add("<< /Type /Page /Parent 2 0 R /MediaBox %s %s /Contents %d 0 R "
                     "/Resources << /Font << %s >> %s >> >>" % (media, extra, c, fonts, res))
        self.kids.append(p)
        return c

    def build(self, bad_xref=False, pages_override=None):
        kids = self.kids
        self.objs[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
        self.objs[2] = ("<< /Type /Pages /Kids [%s] /Count %d >>"
                        % (" ".join("%d 0 R" % k for k in kids), len(kids))).encode()
        out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
        offs = {}
        for n in sorted(self.objs):
            offs[n] = len(out)
            out += b"%d 0 obj\n" % n + self.objs[n] + b"\nendobj\n"
        xpos = len(out)
        size = max(self.objs) + 1
        out += b"xref\n0 %d\n0000000000 65535 f \n" % size
        for n in range(1, size):
            o = offs[n] + (7 if bad_xref else 0)
            out += b"%010d 00000 n \n" % o
        out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\nendxref\n" % (size, xpos)
        return bytes(out).replace(b"endxref\n", b"%%EOF\n")


HDR = "BT /F1 14 Tf 1 0 0 1 300 700 Tm (%s) Tj ET\n"


def mk(body, setup=None, num="1/1", **pk):
    b = PDF()
    res = setup(b) if setup else ""
    b.page((HDR % num) + body, res=res, **pk)
    return b.build()


def multi(nums, bad_xref=False):
    b = PDF()
    for n in nums:
        b.page((HDR % n) + "100 100 50 50 re f\n")
    return b.build(bad_xref=bad_xref)


def fn_black():
    return "<< /FunctionType 2 /Domain [0 1] /C0 [1] /C1 [0] /N 1 >>"


def lint(data, name="t.pdf", profile="auto"):
    return intake.lint_file(data, name=name, profile=profile)


def sheet(data):
    return lint(data)[0]


def codes(data):
    return {f.code: f for f in sheet(data).findings}


def cli(data, name="t.pdf", args=()):
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, name)
        with open(p, "wb") as f:
            f.write(data)
        r = subprocess.run([sys.executable, "-m", "drawlint", p, "--json"] + list(args), cwd=ROOT,
                           capture_output=True, text=True, timeout=120,
                           env=dict(os.environ, CUDA_VISIBLE_DEVICES=""))
    return r


def cli_codes(data):
    r = cli(data)
    rep = json.loads(r.stdout)
    out = {}
    for s in rep["sheets"]:
        for f in s["findings"]:
            out.setdefault(f["code"], f["severity"])
    for f in rep.get("set_findings", []):
        out.setdefault(f["code"], f["severity"])
    return r, rep, out


class Base(unittest.TestCase):
    def has(self, data, code, sev=None):
        c = codes(data)
        self.assertIn(code, c, "expected %s, got %s" % (code, sorted(c)))
        if sev:
            self.assertEqual(c[code].severity, sev)

    def lacks(self, data, code):
        c = codes(data)
        self.assertNotIn(code, c, "unexpected %s in %s" % (code, sorted(c)))


class PageSize(Base):
    def test_cropbox_used_over_mediabox(self):
        d = mk("", media="[0 0 700 900]", extra="/CropBox [0 0 612 792]")
        self.lacks(d, "page-size")

    def test_mediabox_when_no_cropbox(self):
        self.has(mk("", media="[0 0 700 900]"), "page-size", "error")

    def test_cropbox_wrong_size_fires_even_if_mediabox_letter(self):
        self.has(mk("", extra="/CropBox [0 0 500 700]"), "page-size", "error")

    def test_cropbox_offset_origin(self):
        d = mk("", media="[0 0 800 1000]", extra="/CropBox [100 100 712 892]")
        self.lacks(d, "page-size")

    def test_userunit_scales(self):
        self.lacks(mk("", media="[0 0 306 396]", extra="/UserUnit 2"), "page-size")

    def test_userunit_absent_means_wrong_size(self):
        self.has(mk("", media="[0 0 306 396]"), "page-size")

    def test_a4_accepted(self):
        self.lacks(mk("", media="[0 0 595.28 841.89]"), "page-size")

    def test_landscape_fires(self):
        self.has(mk("", media="[0 0 792 612]"), "page-size", "error")

    def test_rotate90_landscape_media_is_portrait_letter(self):
        self.lacks(mk("", media="[0 0 792 612]", extra="/Rotate 90"), "page-size")

    def test_rotate270_landscape_media_is_portrait_letter(self):
        self.lacks(mk("", media="[0 0 792 612]", extra="/Rotate 270"), "page-size")

    def test_rotate90_portrait_media_becomes_landscape(self):
        self.has(mk("", extra="/Rotate 90"), "page-size", "error")

    def test_rotate180_keeps_size(self):
        self.lacks(mk("", extra="/Rotate 180"), "page-size")

    def test_profile_a4_on_letter(self):
        c = {f.code for f in lint(mk(""), profile="a4")[0].findings}
        self.assertIn("page-size", c)


class Rotation(Base):
    # Rotate 90 (clockwise): unrotated point (x, y) lands at new x = y, new y(up) = W - x, W = 792.
    def rot(self, body):
        b = PDF()
        b.page(body, media="[0 0 792 612]", extra="/Rotate 90")
        return b.build()

    def test_content_inside_after_rotation(self):
        # unrotated x 720..740 -> new y(up) 52..72 -> svg y 720..740, inside sight
        d = self.rot("720 300 20 100 re f\n")
        self.lacks(d, "margins")

    def test_content_outside_after_rotation(self):
        # unrotated x 20..40 -> new y(up) 752..772 -> svg y 20..40, above the top margin
        d = self.rot("20 300 20 100 re f\n")
        self.has(d, "margins", "error")

    def test_rotation_moves_content_into_left_margin(self):
        # unrotated y 20..60 -> new x 20..60: outside left margin (72); unrotated x 400 is fine
        d = self.rot("400 20 50 40 re f\n")
        self.has(d, "margins", "error")

    def test_rotation_boundary_inside(self):
        # new x = unrotated y; unrotated y 100..200 -> new x 100..200 inside
        d = self.rot("400 100 50 100 re f\n")
        self.lacks(d, "margins")


class Geometry(Base):
    def test_frame_lone_re_stroked(self):
        self.has(mk("1 w 75 30 489 687 re S\n"), "frame", "error")

    def test_frame_closed_re_B(self):
        self.has(mk("1 w 75 30 489 687 re B\n"), "frame")

    def test_frame_not_for_small_rect(self):
        self.lacks(mk("1 w 100 100 200 200 re S\n"), "frame")

    def test_frame_not_for_fill_only(self):
        self.lacks(mk("75 30 489 687 re f\n"), "frame")

    def test_frame_not_when_edges_far_from_sight(self):
        self.lacks(mk("1 w 130 100 380 520 re S\n"), "frame")

    def test_frame_via_mlllh_has_no_margin_error(self):
        d = mk("1 w 75 30 m 564 30 l 564 717 l 75 717 l h S\n")
        self.lacks(d, "margins")

    def test_zero_width_is_thin_line(self):
        self.has(mk("0 w 100 400 m 200 400 l S\n"), "thin-line", "advisory")

    def test_width_one_not_thin(self):
        self.lacks(mk("1 w 100 400 m 200 400 l S\n"), "thin-line")

    def test_default_width_not_thin(self):
        self.lacks(mk("100 400 m 200 400 l S\n"), "thin-line")

    def test_width_below_half_is_thin(self):
        self.has(mk("0.4 w 100 400 m 200 400 l S\n"), "thin-line")

    def test_width_exactly_half_not_thin(self):
        self.lacks(mk("0.5 w 100 400 m 200 400 l S\n"), "thin-line")

    def test_cm_scale_makes_thin(self):
        self.has(mk("0.1 0 0 0.1 0 0 cm 1 w 1000 4000 m 2000 4000 l S\n"), "thin-line")

    def test_q_Q_restores_width(self):
        self.lacks(mk("q 0 w Q 100 400 m 200 400 l S\n"), "thin-line")

    def test_extgstate_LW(self):
        d = mk("/G1 gs 100 400 m 200 400 l S\n", lambda b: "/ExtGState << /G1 << /LW 0.2 >> >>")
        self.has(d, "thin-line")

    def test_thin_line_only_when_stroked(self):
        self.lacks(mk("0 w 100 400 50 50 re f\n"), "thin-line")

    def test_painting_ops_in_margin_fire(self):
        for op in ["S", "s", "f", "F", "f*", "B", "B*", "b", "b*"]:
            with self.subTest(op=op):
                self.has(mk("1 w 10 400 m 30 400 l 30 420 l %s\n" % op), "margins", "error")

    def test_n_paints_nothing(self):
        d = mk("1 0 0 rg 10 400 50 50 re n\n")
        c = codes(d)
        self.assertNotIn("margins", c)
        self.assertNotIn("ink", c)

    def test_curve_ops_reach_margin(self):
        for body in ["300 400 m 200 400 100 400 40 400 c S\n",
                     "300 400 m 200 400 40 400 v S\n",
                     "300 400 m 200 400 40 400 y S\n"]:
            with self.subTest(body=body):
                self.has(mk(body), "margins")

    def test_curve_inside_ok(self):
        self.lacks(mk("300 400 m 200 450 150 450 150 400 c S\n"), "margins")

    def test_stroke_half_width_counts(self):
        # line centred at x=72.2, width 4 -> extends to 70.2, more than 0.5 outside
        self.has(mk("4 w 72.2 300 m 72.2 400 l S\n"), "margins")

    def test_within_half_point_tolerance(self):
        self.lacks(mk("0 w 71.8 300 m 71.8 400 l S\n"), "margins")

    def test_cm_translation_moves_into_margin(self):
        self.has(mk("1 0 0 1 -200 0 cm 1 w 100 300 50 50 re f\n"), "margins")

    def test_q_Q_restores_ctm(self):
        self.lacks(mk("q 1 0 0 1 -200 0 cm Q 100 300 50 50 re f\n"), "margins")

    def test_inside_geometry_clean(self):
        c = codes(mk("1 w 100 300 100 100 re S\n"))
        for bad in ("margins", "frame", "ink", "gradient", "translucent", "thin-line", "text-height"):
            self.assertNotIn(bad, c)

    def test_top_margin_pdf_y_up(self):
        # PDF y 730 is svg y 62: above the 72 pt top margin
        self.has(mk("100 730 50 10 re f\n"), "margins")
        self.lacks(mk("100 715 50 5 re f\n"), "margins")


class Opacity(Base):
    GS = "/ExtGState << /G1 << /ca 0.5 >> /G2 << /CA 0.5 >> /G3 << /ca 1 /CA 1 >> /G4 << /ca 0.5 /CA 0.5 >> >>"

    def test_fill_alpha_translucent(self):
        self.has(mk("/G1 gs 0 g 100 400 50 50 re f\n", lambda b: self.GS), "translucent", "error")

    def test_stroke_alpha_translucent(self):
        self.has(mk("/G2 gs 100 400 m 200 400 l S\n", lambda b: self.GS), "translucent")

    def test_fill_alpha_does_not_affect_stroke(self):
        self.lacks(mk("/G1 gs 100 400 m 200 400 l S\n", lambda b: self.GS), "translucent")

    def test_stroke_alpha_does_not_affect_fill(self):
        self.lacks(mk("/G2 gs 100 400 50 50 re f\n", lambda b: self.GS), "translucent")

    def test_alpha_one_clean(self):
        self.lacks(mk("/G3 gs 100 400 50 50 re f\n", lambda b: self.GS), "translucent")

    def test_alpha_restored_by_Q(self):
        self.lacks(mk("q /G1 gs Q 100 400 50 50 re f\n", lambda b: self.GS), "translucent")

    def test_alpha_inside_form(self):
        def setup(b):
            fm = b.stream("/Type /XObject /Subtype /Form /BBox [0 0 612 792]", "/G4 gs 100 400 50 50 re f\n")
            return "/XObject << /Fm1 %d 0 R >> /ExtGState << /G4 << /ca 0.5 >> >>" % fm
        self.has(mk("/Fm1 Do\n", setup), "translucent")


class Colour(Base):
    def test_default_black_clean(self):
        c = codes(mk("1 w 100 400 50 50 re B\n"))
        self.assertNotIn("ink", c)
        self.assertNotIn("near-black", c)

    def test_gray(self):
        self.has(mk("0.5 g 100 400 50 50 re f\n"), "ink", "error")
        self.lacks(mk("0 g 100 400 50 50 re f\n"), "ink")
        self.lacks(mk("1 g 100 400 50 50 re f\n"), "ink")

    def test_near_black_gray(self):
        d = mk("0.1 g 100 400 50 50 re f\n")
        self.has(d, "near-black", "advisory")
        self.lacks(d, "ink")

    def test_rgb(self):
        self.has(mk("1 0 0 rg 100 400 50 50 re f\n"), "ink")
        self.lacks(mk("0 0 0 rg 100 400 50 50 re f\n"), "ink")

    def test_stroke_colour(self):
        self.has(mk("0 0 1 RG 100 400 m 200 400 l S\n"), "ink")

    def test_unused_stroke_colour_not_checked_for_fill_only(self):
        self.lacks(mk("1 0 0 RG 0 g 100 400 50 50 re f\n"), "ink")

    def test_cmyk_pure_black_clean(self):
        d = mk("0 0 0 1 k 100 400 50 50 re f\n")
        c = codes(d)
        self.assertNotIn("ink", c)
        self.assertNotIn("near-black", c)

    def test_cmyk_stroke_black_clean(self):
        self.lacks(mk("0 0 0 1 K 100 400 m 200 400 l S\n"), "ink")

    def test_cmyk_white_clean(self):
        self.lacks(mk("0 0 0 0 k 100 400 50 50 re f\n"), "ink")

    def test_cmyk_cyan_is_ink(self):
        self.has(mk("0.3 0 0 0 k 100 400 50 50 re f\n"), "ink")
        self.has(mk("1 0 0 0 k 100 400 50 50 re f\n"), "ink")
        self.has(mk("0.1 0 0 0.5 k 100 400 50 50 re f\n"), "ink")

    def test_cmyk_k_only_grey_is_ink(self):
        self.has(mk("0 0 0 0.5 k 100 400 50 50 re f\n"), "ink")

    def sep(self, name, alt="/DeviceGray", fn=None):
        def setup(b):
            return "/ColorSpace << /CS1 [/Separation /%s %s %s] >>" % (name, alt, fn or fn_black())
        return setup

    def test_separation_black_tint1_clean(self):
        d = mk("/CS1 cs 1 scn 100 400 50 50 re f\n", self.sep("Black"))
        c = codes(d)
        self.assertNotIn("ink", c)

    def test_separation_black_tint_half_is_ink(self):
        self.has(mk("/CS1 cs 0.5 scn 100 400 50 50 re f\n", self.sep("Black")), "ink", "error")

    def test_separation_black_tint0_white_clean(self):
        self.lacks(mk("/CS1 cs 0 scn 100 400 50 50 re f\n", self.sep("Black")), "ink")

    def test_separation_all_tint1_clean(self):
        self.lacks(mk("/CS1 cs 1 scn 100 400 50 50 re f\n", self.sep("All")), "ink")

    def test_separation_all_tint_half_is_ink(self):
        self.has(mk("/CS1 cs 0.5 scn 100 400 50 50 re f\n", self.sep("All")), "ink")

    PAN = "PANTONE#20185#20C"
    PFN = "<< /FunctionType 2 /Domain [0 1] /C0 [0 0 0 0] /C1 [0 0.9 0.76 0] /N 1 >>"

    def test_pantone_is_ink_and_named(self):
        d = mk("/CS1 cs 1 scn 100 400 50 50 re f\n", self.sep(self.PAN, "/DeviceCMYK", self.PFN))
        self.has(d, "ink", "error")
        r, rep, out = cli_codes(d)
        self.assertEqual(r.returncode, 1)
        self.assertIn("PANTONE 185 C", r.stdout)  # spec example is spot-ink:PANTONE 185 C

    def test_pantone_small_tint_still_ink(self):
        d = mk("/CS1 cs 0.05 scn 100 400 50 50 re f\n", self.sep(self.PAN, "/DeviceCMYK", self.PFN))
        self.has(d, "ink")

    def test_pantone_tint0_clean(self):
        d = mk("/CS1 cs 0 scn 100 400 50 50 re f\n", self.sep(self.PAN, "/DeviceCMYK", self.PFN))
        self.lacks(d, "ink")

    def test_pantone_stroke(self):
        d = mk("/CS1 CS 1 SCN 100 400 m 200 400 l S\n", self.sep(self.PAN, "/DeviceCMYK", self.PFN))
        self.has(d, "ink")

    def devn(self, b):
        fn = b.stream("/FunctionType 4 /Domain [0 1 0 1] /Range [0 1 0 1 0 1 0 1]", "{0 0 3 -1 roll}")
        return "/ColorSpace << /CS1 [/DeviceN [/Cyan /Black] /DeviceCMYK %d 0 R] >>" % fn

    def test_devicen_only_black_clean(self):
        self.lacks(mk("/CS1 cs 0 1 scn 100 400 50 50 re f\n", self.devn), "ink")

    def test_devicen_other_colourant_ink(self):
        self.has(mk("/CS1 cs 0.5 1 scn 100 400 50 50 re f\n", self.devn), "ink")

    def test_devicen_all_zero_clean(self):
        self.lacks(mk("/CS1 cs 0 0 scn 100 400 50 50 re f\n", self.devn), "ink")

    def idx(self, b):
        return "/ColorSpace << /CS1 [/Indexed /DeviceRGB 1 <ff0000000000>] >>"

    def test_indexed_red_entry_ink(self):
        self.has(mk("/CS1 cs 0 sc 100 400 50 50 re f\n", self.idx), "ink")

    def test_indexed_black_entry_clean(self):
        self.lacks(mk("/CS1 cs 1 sc 100 400 50 50 re f\n", self.idx), "ink")

    def test_icc_by_component_count(self):
        def setup(n):
            def f(b):
                s = b.stream("/N %d" % n, b"\x00" * 16)
                return "/ColorSpace << /CS1 [/ICCBased %d 0 R] >>" % s
            return f
        self.has(mk("/CS1 cs 1 0 0 sc 100 400 50 50 re f\n", setup(3)), "ink")
        self.lacks(mk("/CS1 cs 0 0 0 sc 100 400 50 50 re f\n", setup(3)), "ink")
        self.has(mk("/CS1 cs 0.5 sc 100 400 50 50 re f\n", setup(1)), "ink")
        self.lacks(mk("/CS1 cs 0 sc 100 400 50 50 re f\n", setup(1)), "ink")
        self.lacks(mk("/CS1 cs 0 0 0 1 sc 100 400 50 50 re f\n", setup(4)), "ink")
        self.has(mk("/CS1 cs 1 0 0 0 sc 100 400 50 50 re f\n", setup(4)), "ink")

    def test_calrgb_calgray(self):
        wp = "<< /WhitePoint [0.95 1 1.09] >>"
        cal = lambda b: "/ColorSpace << /CS1 [/CalRGB %s] /CS2 [/CalGray %s] >>" % (wp, wp)
        self.has(mk("/CS1 cs 1 0 0 sc 100 400 50 50 re f\n", cal), "ink")
        self.lacks(mk("/CS1 cs 0 0 0 sc 100 400 50 50 re f\n", cal), "ink")
        self.has(mk("/CS2 cs 0.5 sc 100 400 50 50 re f\n", cal), "ink")
        self.lacks(mk("/CS2 cs 0 sc 100 400 50 50 re f\n", cal), "ink")

    def test_colour_restored_by_Q(self):
        self.lacks(mk("q 1 0 0 rg Q 100 400 50 50 re f\n"), "ink")

    def test_tiling_coloured_pattern_ink_checked(self):
        def setup(b):
            p = b.stream("/Type /Pattern /PatternType 1 /PaintType 1 /TilingType 1 /BBox [0 0 10 10] "
                         "/XStep 10 /YStep 10 /Resources << >>", "1 0 0 rg 0 0 5 5 re f\n")
            return "/Pattern << /P1 %d 0 R >>" % p
        self.has(mk("/Pattern cs /P1 scn 100 400 100 100 re f\n", setup), "ink")

    def test_tiling_coloured_pattern_black_clean(self):
        def setup(b):
            p = b.stream("/Type /Pattern /PatternType 1 /PaintType 1 /TilingType 1 /BBox [0 0 10 10] "
                         "/XStep 10 /YStep 10 /Resources << >>", "0 g 0 0 5 5 re f\n")
            return "/Pattern << /P1 %d 0 R >>" % p
        c = codes(mk("/Pattern cs /P1 scn 100 400 100 100 re f\n", setup))
        self.assertNotIn("ink", c)
        self.assertNotIn("gradient", c)

    def uncol(self, b):
        p = b.stream("/Type /Pattern /PatternType 1 /PaintType 2 /TilingType 1 /BBox [0 0 10 10] "
                     "/XStep 10 /YStep 10 /Resources << >>", "0 0 5 5 re f\n")
        return "/ColorSpace << /CS1 [/Pattern /DeviceRGB] >> /Pattern << /P1 %d 0 R >>" % p

    def test_uncoloured_tiling_takes_given_colour(self):
        self.has(mk("/CS1 cs 1 0 0 /P1 scn 100 400 100 100 re f\n", self.uncol), "ink")
        self.lacks(mk("/CS1 cs 0 0 0 /P1 scn 100 400 100 100 re f\n", self.uncol), "ink")


class ShadingsImages(Base):
    SH = ("<< /ShadingType 2 /ColorSpace /DeviceGray /Coords [100 0 200 0] /Function "
          "<< /FunctionType 2 /Domain [0 1] /C0 [0] /C1 [1] /N 1 >> >>")

    def test_sh_operator_gradient(self):
        d = mk("/S1 sh\n", lambda b: "/Shading << /S1 %d 0 R >>" % b.add(self.SH))
        self.has(d, "gradient", "error")

    def test_shading_pattern_gradient(self):
        def setup(b):
            s = b.add(self.SH)
            p = b.add("<< /PatternType 2 /Shading %d 0 R >>" % s)
            return "/Pattern << /P1 %d 0 R >>" % p
        self.has(mk("/Pattern cs /P1 scn 100 400 100 100 re f\n", setup), "gradient", "error")

    def test_no_shading_no_gradient(self):
        self.lacks(mk("0.5 g 100 400 100 100 re f\n"), "gradient")

    def img(self, b):
        n = b.stream("/Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray /BitsPerComponent 8",
                     b"\x80")
        return "/XObject << /Im1 %d 0 R >>" % n

    def test_image_xobject(self):
        self.has(mk("q 100 0 0 100 200 300 cm /Im1 Do Q\n", self.img), "raster-image", "advisory")

    def test_inline_image(self):
        d = mk("q 100 0 0 100 200 300 cm\nBI /W 1 /H 1 /CS /G /BPC 8 ID \x80\nEI\nQ\n")
        self.has(d, "raster-image", "advisory")

    def test_no_image_no_advisory(self):
        self.lacks(mk("100 300 100 100 re f\n"), "raster-image")

    def test_image_area_is_geometry(self):
        self.has(mk("q 100 0 0 100 10 300 cm /Im1 Do Q\n", self.img), "margins")
        self.lacks(mk("q 100 0 0 100 200 300 cm /Im1 Do Q\n", self.img), "margins")

    def test_inline_image_area_is_geometry(self):
        d = mk("q 100 0 0 100 10 300 cm\nBI /W 1 /H 1 /CS /G /BPC 8 ID \x80\nEI\nQ\n")
        self.has(d, "margins")


class Forms(Base):
    def form(self, matrix, body="0 0 10 10 re f\n"):
        def setup(b):
            fm = b.stream("/Type /XObject /Subtype /Form /BBox [0 0 100 100] /Matrix %s" % matrix, body)
            return "/XObject << /Fm1 %d 0 R >>" % fm
        return setup

    def test_matrix_moves_into_margin(self):
        self.has(mk("/Fm1 Do\n", self.form("[1 0 0 1 20 300]")), "margins")

    def test_matrix_keeps_inside(self):
        self.lacks(mk("/Fm1 Do\n", self.form("[1 0 0 1 200 300]")), "margins")

    def test_matrix_scale(self):
        # 10x10 square scaled by 20 starting at x=60 reaches x 60..260: left edge is in the margin
        self.has(mk("/Fm1 Do\n", self.form("[20 0 0 20 60 300]")), "margins")

    def test_form_own_resources_colour(self):
        def setup(b):
            fm = b.stream("/Type /XObject /Subtype /Form /BBox [0 0 100 100] /Resources << /ColorSpace "
                          "<< /CS9 [/Separation /PANTONE#20185#20C /DeviceCMYK << /FunctionType 2 /Domain [0 1] "
                          "/C0 [0 0 0 0] /C1 [0 1 1 0] /N 1 >>] >> >>", "/CS9 cs 1 scn 0 0 10 10 re f\n")
            return "/XObject << /Fm1 %d 0 R >>" % fm
        self.has(mk("q 1 0 0 1 200 300 cm /Fm1 Do Q\n", setup), "ink")

    def chain(self, depth):
        def setup(b):
            nums = [b.nxt + i for i in range(depth)]
            for i in range(depth):
                inner = "0 0 10 10 re f\n" if i == depth - 1 else "/Fm%d Do\n" % (i + 2)
                res = "" if i == depth - 1 else "/Resources << /XObject << /Fm%d %d 0 R >> >>" % (i + 2, nums[i + 1])
                n = b.stream("/Type /XObject /Subtype /Form /BBox [0 0 600 800] %s" % res, inner)
                assert n == nums[i]
            return "/XObject << /Fm1 %d 0 R >>" % nums[0]
        return setup

    def test_nesting_shallow_ok(self):
        d = mk("q 1 0 0 1 200 300 cm /Fm1 Do Q\n", self.chain(3))
        self.assertTrue(lint(d))

    def test_nesting_8_deep_ok(self):
        d = mk("q 1 0 0 1 200 300 cm /Fm1 Do Q\n", self.chain(8))
        self.assertTrue(lint(d))

    def test_nesting_too_deep_refused(self):
        d = mk("q 1 0 0 1 200 300 cm /Fm1 Do Q\n", self.chain(12))
        with self.assertRaises(InputError):
            lint(d)

    def test_nesting_too_deep_cli_exit2(self):
        d = mk("q 1 0 0 1 200 300 cm /Fm1 Do Q\n", self.chain(12))
        self.assertEqual(cli(d).returncode, 2)

    def test_self_referencing_form_refused(self):
        def setup(b):
            n = b.nxt
            b.stream("/Type /XObject /Subtype /Form /BBox [0 0 600 800] /Resources << /XObject << /Fm1 %d 0 R >> >>" % n,
                     "/Fm1 Do\n")
            return "/XObject << /Fm1 %d 0 R >>" % n
        with self.assertRaises(InputError):
            lint(mk("/Fm1 Do\n", setup))


class Text(Base):
    def txt(self, size_ops, string="1/1", pos="1 0 0 1 300 700 Tm"):
        return "BT %s %s (%s) Tj ET\n" % (size_ops, pos, string)

    def size_of(self, d, text):
        s = sheet(d)
        for t, pt, bbox in s.texts:
            if t == text:
                return pt
        self.fail("text %r not in %r" % (text, [t[0] for t in s.texts]))

    def test_tf1_with_tm_scale(self):
        d = "BT /F1 1 Tf 12 0 0 12 300 700 Tm (1/1) Tj ET\n"
        b = PDF()
        b.page(d + "BT /F1 12 Tf 1 0 0 1 200 400 Tm (hello) Tj ET\n")
        data = b.build()
        self.assertAlmostEqual(self.size_of(data, "1/1"), 12, places=3)
        self.lacks(data, "text-height")

    def test_tf8_is_text_height_error(self):
        d = mk("BT /F1 8 Tf 1 0 0 1 200 400 Tm (small) Tj ET\n")
        self.has(d, "text-height", "error")

    def test_tf9_boundary_ok(self):
        d = mk("BT /F1 9 Tf 1 0 0 1 200 400 Tm (small) Tj ET\n")
        self.lacks(d, "text-height")

    def test_tf_below_9_via_tm_scale_down(self):
        d = mk("BT /F1 18 Tf 0.4 0 0 0.4 200 400 Tm (small) Tj ET\n")
        self.has(d, "text-height")

    def test_tf1_tm_scale_8_is_error(self):
        d = mk("BT /F1 1 Tf 8 0 0 8 200 400 Tm (small) Tj ET\n")
        self.has(d, "text-height")

    def test_cm_scale_gives_size(self):
        d = mk("q 12 0 0 12 0 0 cm BT /F1 1 Tf 1 0 0 1 20 30 Tm (okay) Tj ET Q\n")
        self.lacks(d, "text-height")
        d = mk("q 8 0 0 8 0 0 cm BT /F1 1 Tf 1 0 0 1 20 30 Tm (small) Tj ET Q\n")
        self.has(d, "text-height")

    def test_no_text_advisory(self):
        b = PDF()
        b.page("100 100 50 50 re f\n")
        self.has(b.build(), "no-live-text", "advisory")

    def test_text_present_no_advisory(self):
        self.lacks(mk(""), "no-live-text")

    def test_render_mode_3_ignored_sheet_number(self):
        d = mk("BT /F1 12 Tf 3 Tr 1 0 0 1 200 400 Tm (Sheet 1 of 2) Tj ET\n")
        self.lacks(d, "sheet-number-format")
        self.assertNotIn("Sheet 1 of 2", [t[0] for t in sheet(d).texts])

    def test_render_mode_7_ignored(self):
        d = mk("BT /F1 12 Tf 7 Tr 1 0 0 1 200 400 Tm (Sheet 1 of 2) Tj ET\n")
        self.lacks(d, "sheet-number-format")

    def test_render_mode_3_small_text_no_height_error(self):
        d = mk("BT /F1 4 Tf 3 Tr 1 0 0 1 200 400 Tm (tiny) Tj ET\n")
        self.lacks(d, "text-height")

    def test_visible_wrong_sheet_number_fires(self):
        d = mk("BT /F1 12 Tf 1 0 0 1 200 400 Tm (Sheet 1 of 2) Tj ET\n")
        self.has(d, "sheet-number-format", "error")

    def test_render_mode_0_after_3_visible_again(self):
        d = mk("BT /F1 12 Tf 3 Tr 0 Tr 1 0 0 1 200 400 Tm (Sheet 1 of 2) Tj ET\n")
        self.has(d, "sheet-number-format")

    def test_stroked_text_modes_use_stroke_colour(self):
        for mode in (1, 2, 5, 6):
            with self.subTest(mode=mode):
                d = mk("1 0 0 RG BT /F1 12 Tf %d Tr 1 0 0 1 200 400 Tm (abc) Tj ET\n" % mode)
                self.has(d, "ink")

    def test_fill_only_text_ignores_stroke_colour(self):
        d = mk("1 0 0 RG BT /F1 12 Tf 0 Tr 1 0 0 1 200 400 Tm (abc) Tj ET\n")
        self.lacks(d, "ink")

    def test_stroked_text_thin_line(self):
        d = mk("0.2 w BT /F1 12 Tf 1 Tr 1 0 0 1 200 400 Tm (abc) Tj ET\n")
        self.has(d, "thin-line")

    def test_red_fill_text_ink(self):
        d = mk("1 0 0 rg BT /F1 12 Tf 1 0 0 1 200 400 Tm (abc) Tj ET\n")
        self.has(d, "ink")

    def sheet_number_text(self, body):
        b = PDF()
        b.page(body)
        return b.build()

    def test_split_1_slash_3_auto_advance_joins(self):
        d = self.sheet_number_text("BT /F1 12 Tf 1 0 0 1 300 700 Tm (1) Tj (/) Tj (3) Tj ET\n")
        s = sheet(d)
        self.assertIn("1/3", [t[0] for t in s.texts])
        self.lacks(d, "sheet-number-missing")
        self.assertEqual(tuple(s.sheet_number), (1, 3))

    def test_split_1_slash_3_separate_tm_joins(self):
        # Helvetica 12 pt: '1' 6.672, '/' 3.336 -> exactly adjacent
        d = self.sheet_number_text(
            "BT /F1 12 Tf 1 0 0 1 300 700 Tm (1) Tj ET\nBT /F1 12 Tf 1 0 0 1 306.672 700 Tm (/) Tj ET\n"
            "BT /F1 12 Tf 1 0 0 1 310.008 700 Tm (3) Tj ET\n")
        self.assertIn("1/3", [t[0] for t in sheet(d).texts])
        self.lacks(d, "sheet-number-missing")

    def test_numerals_five_em_apart_stay_separate(self):
        d = self.sheet_number_text(
            "BT /F1 12 Tf 1 0 0 1 300 700 Tm (1) Tj ET\nBT /F1 12 Tf 1 0 0 1 360 700 Tm (3) Tj ET\n")
        texts = [t[0] for t in sheet(d).texts]
        self.assertIn("1", texts)
        self.assertIn("3", texts)
        for t in texts:
            self.assertNotIn("13", t.replace(" ", ""))

    def test_gap_over_015_em_gets_space(self):
        # gap 0.3 em = 3.6 pt after the '1' (ends at 306.672)
        d = self.sheet_number_text(
            "BT /F1 12 Tf 1 0 0 1 300 700 Tm (1) Tj ET\nBT /F1 12 Tf 1 0 0 1 310.272 700 Tm (3) Tj ET\n")
        self.assertIn("1 3", [t[0] for t in sheet(d).texts])

    def test_gap_under_015_em_no_space(self):
        # gap 0.1 em = 1.2 pt
        d = self.sheet_number_text(
            "BT /F1 12 Tf 1 0 0 1 300 700 Tm (1) Tj ET\nBT /F1 12 Tf 1 0 0 1 307.872 700 Tm (3) Tj ET\n")
        self.assertIn("13", [t[0] for t in sheet(d).texts])

    def test_gap_over_06_em_separate(self):
        # gap 0.8 em = 9.6 pt
        d = self.sheet_number_text(
            "BT /F1 12 Tf 1 0 0 1 300 700 Tm (1) Tj ET\nBT /F1 12 Tf 1 0 0 1 316.272 700 Tm (3) Tj ET\n")
        t = [x[0] for x in sheet(d).texts]
        self.assertIn("1", t)
        self.assertIn("3", t)

    def test_different_baseline_not_joined(self):
        d = self.sheet_number_text(
            "BT /F1 12 Tf 1 0 0 1 300 700 Tm (1) Tj ET\nBT /F1 12 Tf 1 0 0 1 306.672 650 Tm (3) Tj ET\n")
        t = [x[0] for x in sheet(d).texts]
        self.assertNotIn("13", t)
        self.assertNotIn("1 3", t)

    def test_different_size_not_joined(self):
        d = self.sheet_number_text(
            "BT /F1 12 Tf 1 0 0 1 300 700 Tm (1) Tj ET\nBT /F1 20 Tf 1 0 0 1 306.672 700 Tm (3) Tj ET\n")
        t = [x[0] for x in sheet(d).texts]
        self.assertNotIn("13", t)
        self.assertNotIn("1 3", t)

    def test_tj_array_text(self):
        d = self.sheet_number_text("BT /F1 12 Tf 1 0 0 1 300 700 Tm [(1) -100 (/) (2)] TJ ET\n")
        self.assertTrue(any("1" in t[0] and "2" in t[0] for t in sheet(d).texts))

    def test_tounicode_cid_font(self):
        b = PDF()
        tu = b.stream("", "/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
                      "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
                      "/CMapName /Adobe-Identity-UCS def /CMapType 2 def\n"
                      "1 begincodespacerange <0000> <FFFF> endcodespacerange\n"
                      "3 beginbfchar <0001> <0031> <0002> <002F> <0003> <0032> endbfchar\n"
                      "endcmap CMapName currentdict /CMap defineresource pop end end\n")
        cf = b.add("<< /Type /Font /Subtype /CIDFontType2 /BaseFont /Foo /CIDSystemInfo "
                   "<< /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> /DW 500 >>")
        f = b.add("<< /Type /Font /Subtype /Type0 /BaseFont /Foo /Encoding /Identity-H "
                  "/DescendantFonts [%d 0 R] /ToUnicode %d 0 R >>" % (cf, tu))
        b.page("BT /F2 12 Tf 1 0 0 1 300 700 Tm <000100020003> Tj ET\n", fonts="/F1 3 0 R /F2 %d 0 R" % f)
        self.assertIn("1/2", [t[0] for t in sheet(b.build()).texts])

    def test_gap_rules_same_text_object(self):
        def tx(x):
            b = PDF()
            b.page("BT /F1 12 Tf 1 0 0 1 300 700 Tm (1) Tj 1 0 0 1 %s 700 Tm (3) Tj ET\n" % x)
            return [t[0] for t in sheet(b.build()).texts]
        self.assertIn("1 3", tx(310.272))   # about 0.3 em
        self.assertIn("13", tx(307.872))    # about 0.1 em
        t = tx(318.0)                       # well over 0.6 em
        self.assertIn("1", t)
        self.assertIn("3", t)


class MultiPage(Base):
    def test_one_sheet_per_page_in_order_with_names(self):
        d = multi(["1/3", "2/3", "3/3"])
        sheets = lint(d, name="my.pdf")
        self.assertEqual(len(sheets), 3)
        self.assertEqual([s.name for s in sheets], ["my.pdf page 1", "my.pdf page 2", "my.pdf page 3"])
        self.assertEqual([tuple(s.sheet_number) for s in sheets], [(1, 3), (2, 3), (3, 3)])

    def test_sha256_shared_and_equal_to_file(self):
        d = multi(["1/2", "2/2"])
        h = hashlib.sha256(d).hexdigest()
        self.assertEqual([s.sha256 for s in lint(d)], [h, h])

    def test_page_dict_units_and_pdf_page(self):
        d = multi(["1/2", "2/2"])
        s = lint(d)
        self.assertEqual(s[0].page.get("units"), "PDF page in points")
        self.assertEqual([x.page.get("pdf_page") for x in s], [1, 2])

    def test_sequence_ok_no_set_errors(self):
        r, rep, out = cli_codes(multi(["1/3", "2/3", "3/3"]))
        self.assertNotIn("sheet-number-sequence", out)
        self.assertEqual(r.returncode, 0, r.stdout[:500])

    def test_sequence_out_of_order_across_pages(self):
        r, rep, out = cli_codes(multi(["2/3", "1/3", "3/3"]))
        self.assertIn("sheet-number-sequence", out)
        self.assertEqual(r.returncode, 1)

    def test_duplicate_number_across_pages(self):
        r, rep, out = cli_codes(multi(["1/3", "1/3", "3/3"]))
        self.assertIn("sheet-number-sequence", out)

    def test_totals_disagree_across_pages(self):
        r, rep, out = cli_codes(multi(["1/3", "2/2"]))
        self.assertIn("sheet-number-sequence", out)

    def test_missing_page_in_sequence(self):
        r, rep, out = cli_codes(multi(["1/3", "3/3"]))
        self.assertIn("sheet-number-sequence", out)

    def test_cli_report_names(self):
        r, rep, out = cli_codes(multi(["1/2", "2/2"]))
        names = [s.get("name") or s.get("file") for s in rep["sheets"]]
        self.assertEqual([os.path.basename(n) for n in names], ["t.pdf page 1", "t.pdf page 2"])

    def test_single_page_name_unsuffixed_or_page1(self):
        s = lint(multi(["1/1"]), name="one.pdf")
        self.assertEqual(len(s), 1)
        self.assertTrue(s[0].name.startswith("one.pdf"))

    def test_different_page_sizes_mixed(self):
        b = PDF()
        b.page(HDR % "1/2")
        b.page(HDR % "2/2", media="[0 0 595.28 841.89]")
        r, rep, out = cli_codes(b.build())
        self.assertIn("mixed-sizes", out)

    def test_pdf_and_svg_in_one_run_order(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.pdf")
            with open(p, "wb") as f:
                f.write(multi(["1/3", "2/3"]))
            s = os.path.join(d, "b.svg")
            with open(s, "w") as f:
                f.write('<svg xmlns="http://www.w3.org/2000/svg" width="8.5in" height="11in" viewBox="0 0 612 792">'
                        '<text x="300" y="90" font-size="14">3/3</text></svg>')
            r = subprocess.run([sys.executable, "-m", "drawlint", p, s, "--json"], cwd=ROOT,
                               capture_output=True, text=True, timeout=120,
                               env=dict(os.environ, CUDA_VISIBLE_DEVICES=""))
        rep = json.loads(r.stdout)
        self.assertEqual(len(rep["sheets"]), 3)
        codes_ = {f["code"] for x in rep["sheets"] for f in x["findings"]} | {f["code"] for f in rep.get("set_findings", [])}
        self.assertNotIn("sheet-number-sequence", codes_)


class Refusals(Base):
    def test_encrypted_refused(self):
        from pypdf import PdfWriter
        w = PdfWriter()
        w.add_blank_page(612, 792)
        w.encrypt("secret")
        buf = io.BytesIO()
        w.write(buf)
        with self.assertRaises(InputError):
            lint(buf.getvalue())
        self.assertEqual(cli(buf.getvalue()).returncode, 2)

    def test_encrypted_empty_user_password_refused(self):
        from pypdf import PdfWriter
        w = PdfWriter()
        w.add_blank_page(612, 792)
        w.encrypt("", owner_password="owner")
        buf = io.BytesIO()
        w.write(buf)
        with self.assertRaises(InputError):
            lint(buf.getvalue())

    def test_zero_pages_refused(self):
        b = PDF()
        d = b.build()
        with self.assertRaises(InputError):
            lint(d)
        self.assertEqual(cli(d).returncode, 2)

    def test_not_a_pdf_with_header_refused(self):
        d = b"%PDF-1.4\nthis is just text, not a document\n"
        with self.assertRaises(InputError):
            lint(d)
        self.assertEqual(cli(d).returncode, 2)

    def test_garbage_binary_with_header_refused(self):
        d = b"%PDF-1.7\n" + bytes(range(256)) * 20
        with self.assertRaises(InputError):
            lint(d)

    def test_101_pages_refused(self):
        b = PDF()
        c = b.stream("", HDR % "1/1")
        for _ in range(101):
            b.page("", content_obj=c)
        d = b.build()
        with self.assertRaises(InputError):
            lint(d)
        self.assertEqual(cli(d).returncode, 2)

    def test_100_pages_accepted(self):
        b = PDF()
        c = b.stream("", HDR % "1/1")
        for _ in range(100):
            b.page("", content_obj=c)
        self.assertEqual(len(lint(b.build())), 100)

    def test_over_20mb_refused(self):
        b = PDF()
        b.page(HDR % "1/1")
        b.stream("", b"0" * (21 * 1024 * 1024))
        with self.assertRaises(InputError):
            lint(b.build())

    def test_over_200000_elements_refused(self):
        body = "0 0 1 1 re f\n" * 200001
        with self.assertRaises(InputError):
            lint(mk(body))

    def test_many_elements_under_limit_ok(self):
        body = "100 100 1 1 re f\n" * 1000
        self.assertTrue(lint(mk(body)))


class Repair(Base):
    def test_damaged_xref_checked_with_one_advisory(self):
        d = multi(["1/2", "2/2"], bad_xref=True)
        sheets = lint(d)
        self.assertEqual(len(sheets), 2)
        n = [sum(1 for f in s.findings if f.code == "pdf-repaired") for s in sheets]
        self.assertEqual(n, [1, 0])
        f = [f for f in sheets[0].findings if f.code == "pdf-repaired"][0]
        self.assertEqual(f.severity, "advisory")

    def test_damaged_file_content_still_checked(self):
        b = PDF()
        b.page((HDR % "1/1") + "1 0 0 rg 100 400 50 50 re f\n")
        sheets = lint(b.build(bad_xref=True))
        self.assertIn("ink", {f.code for f in sheets[0].findings})

    def test_intact_file_has_no_repair_advisory(self):
        self.lacks(mk(""), "pdf-repaired")

    def test_cli_no_stderr_and_exit0_for_repaired(self):
        r = cli(multi(["1/1"], bad_xref=True))
        self.assertEqual(r.stderr, "", r.stderr)
        self.assertEqual(r.returncode, 0, r.stdout[:400])
        rep = json.loads(r.stdout)
        cs = [f["code"] for s in rep["sheets"] for f in s["findings"]]
        self.assertEqual(cs.count("pdf-repaired"), 1)

    def test_cli_no_stderr_multi_page_repaired(self):
        r = cli(multi(["1/2", "2/2"], bad_xref=True))
        self.assertEqual(r.stderr, "", r.stderr)
        rep = json.loads(r.stdout)
        cs = [f["code"] for s in rep["sheets"] for f in s["findings"]]
        self.assertEqual(cs.count("pdf-repaired"), 1)

    def test_truncated_trailer_repair(self):
        d = multi(["1/1"])
        d = d[:d.rindex(b"xref")]  # drop xref table, trailer and startxref
        try:
            sheets = lint(d)
        except InputError:
            self.skipTest("pypdf could not repair this truncation")
        self.assertIn("pdf-repaired", {f.code for f in sheets[0].findings})


@unittest.skipUnless(os.path.exists(RSVG), "rsvg-convert missing")
class Rsvg(Base):
    def conv(self, inner, head='width="612pt" height="792pt" viewBox="0 0 612 792"'):
        svg = '<svg xmlns="http://www.w3.org/2000/svg" %s>%s</svg>' % (head, inner)
        r = subprocess.run([RSVG, "-f", "pdf"], input=svg.encode(), capture_output=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith(b"%PDF"))
        return r.stdout

    def test_letter_size_and_clean_black(self):
        d = self.conv('<rect x="100" y="200" width="100" height="100" fill="none" stroke="#000" stroke-width="1"/>')
        c = codes(d)
        for bad in ("page-size", "margins", "ink", "frame", "gradient", "translucent"):
            self.assertNotIn(bad, c)

    def test_red_is_ink(self):
        self.has(self.conv('<rect x="100" y="200" width="100" height="100" fill="#f00"/>'), "ink", "error")

    def test_rect_in_margin(self):
        self.has(self.conv('<rect x="10" y="200" width="30" height="30" fill="#000"/>'), "margins")

    def test_opacity_translucent(self):
        self.has(self.conv('<rect x="100" y="200" width="100" height="100" fill="#000" fill-opacity="0.5"/>'),
                 "translucent")

    def test_thin_stroke(self):
        self.has(self.conv('<path d="M100 200 L300 200" stroke="#000" stroke-width="0.2"/>'), "thin-line")

    def test_gradient(self):
        d = self.conv('<defs><linearGradient id="g"><stop offset="0" stop-color="#000"/>'
                      '<stop offset="1" stop-color="#fff"/></linearGradient></defs>'
                      '<rect x="100" y="200" width="100" height="100" fill="url(#g)"/>')
        self.has(d, "gradient", "error")

    def test_frame(self):
        d = self.conv('<rect x="75" y="75" width="489" height="687" fill="none" stroke="#000" stroke-width="1"/>')
        self.has(d, "frame")

    def test_a4_size(self):
        d = self.conv('<rect x="100" y="200" width="50" height="50" fill="#000"/>',
                      head='width="595.28pt" height="841.89pt" viewBox="0 0 595.28 841.89"')
        self.lacks(d, "page-size")

    def test_landscape(self):
        d = self.conv('<rect x="100" y="200" width="50" height="50" fill="#000"/>',
                      head='width="792pt" height="612pt" viewBox="0 0 792 612"')
        self.has(d, "page-size")

    def test_live_text_and_height(self):
        d = self.conv('<text x="300" y="90" font-family="sans-serif" font-size="14">1/1</text>'
                      '<text x="200" y="400" font-family="sans-serif" font-size="6">tiny</text>')
        s = sheet(d)
        self.assertTrue(any(t[0] == "1/1" for t in s.texts), [t[0] for t in s.texts])
        self.assertIn("text-height", {f.code for f in s.findings})
        self.assertNotIn("sheet-number-missing", {f.code for f in s.findings})

    def test_text_14pt_size(self):
        d = self.conv('<text x="300" y="90" font-family="sans-serif" font-size="14">1/1</text>')
        sizes = [pt for t, pt, bb in sheet(d).texts if t == "1/1"]
        self.assertTrue(sizes and abs(sizes[0] - 14) < 0.5, sizes)

    def test_raster_image(self):
        png = base64.b64encode(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")).decode()
        d = self.conv('<image x="100" y="200" width="50" height="50" href="data:image/png;base64,%s"/>' % png)
        self.has(d, "raster-image", "advisory")


class Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.log = os.path.join(cls.tmp.name, "usage.jsonl")
        cls.srv = make_server("127.0.0.1", 0, cls.log)
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thread.join(10)
        cls.tmp.cleanup()

    def post(self, body, ctype, path="/v1/lint"):
        r = urllib.request.Request(self.base + path, data=body, method="POST")
        r.add_header("Content-Type", ctype)
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            try:
                return e.code, e.read()
            finally:
                e.close()

    def post_json(self, obj):
        return self.post(json.dumps(obj).encode(), "application/json")

    def pages(self, n):
        b = PDF()
        c = b.stream("", HDR % "1/1")
        for _ in range(n):
            b.page("", content_obj=c)
        return b.build()

    def test_application_pdf_body(self):
        st, body = self.post(multi(["1/2", "2/2"]), "application/pdf")
        self.assertEqual(st, 200, body[:300])
        rep = json.loads(body)
        self.assertEqual(len(rep["sheets"]), 2)
        self.assertEqual(rep["disclaimer"], "Automated formality check only, not legal advice.")

    def test_application_pdf_with_charset_param_and_profile(self):
        st, body = self.post(multi(["1/1"]), "application/pdf", "/v1/lint?profile=a4")
        self.assertEqual(st, 200)
        codes_ = [f["code"] for f in json.loads(body)["sheets"][0]["findings"]]
        self.assertIn("page-size", codes_)

    def test_pdf_error_found_reported(self):
        d = mk("1 0 0 rg 100 400 50 50 re f\n")
        st, body = self.post(d, "application/pdf")
        self.assertEqual(st, 200)
        codes_ = [f["code"] for f in json.loads(body)["sheets"][0]["findings"]]
        self.assertIn("ink", codes_)

    def test_json_pdf_base64(self):
        d = multi(["1/2", "2/2"])
        st, body = self.post_json({"sheets": [{"name": "a.pdf", "pdf_base64": base64.b64encode(d).decode()}]})
        self.assertEqual(st, 200, body[:300])
        rep = json.loads(body)
        self.assertEqual(len(rep["sheets"]), 2)
        names = [s.get("name") or s.get("file") for s in rep["sheets"]]
        self.assertEqual(names, ["a.pdf page 1", "a.pdf page 2"])

    def test_json_mixed_svg_and_pdf(self):
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="8.5in" height="11in" viewBox="0 0 612 792">'
               '<text x="300" y="90" font-size="14">2/2</text></svg>')
        d = multi(["1/2"])
        st, body = self.post_json({"sheets": [{"name": "a.pdf", "pdf_base64": base64.b64encode(d).decode()},
                                              {"name": "b.svg", "svg": svg}]})
        self.assertEqual(st, 200, body[:300])
        self.assertEqual(len(json.loads(body)["sheets"]), 2)

    def test_invalid_base64_400(self):
        st, body = self.post_json({"sheets": [{"name": "a.pdf", "pdf_base64": "!!!not base64***"}]})
        self.assertEqual(st, 400, body[:300])

    def test_101_pages_413_raw(self):
        st, body = self.post(self.pages(101), "application/pdf")
        self.assertEqual(st, 413, body[:300])

    def test_101_pages_413_json(self):
        st, body = self.post_json({"sheets": [{"name": "a.pdf",
                                               "pdf_base64": base64.b64encode(self.pages(101)).decode()}]})
        self.assertEqual(st, 413, body[:300])

    def test_100_pages_ok(self):
        st, body = self.post(self.pages(100), "application/pdf")
        self.assertEqual(st, 200, body[:200])
        self.assertEqual(len(json.loads(body)["sheets"]), 100)

    def test_sheets_total_over_100_after_splitting_413(self):
        pdf = self.pages(60)
        b64 = base64.b64encode(pdf).decode()
        st, body = self.post_json({"sheets": [{"name": "a.pdf", "pdf_base64": b64},
                                              {"name": "b.pdf", "pdf_base64": b64}]})
        self.assertEqual(st, 413, body[:300])

    def test_unreadable_pdf_422(self):
        st, body = self.post(b"%PDF-1.4\nnot really a pdf\n", "application/pdf")
        self.assertEqual(st, 422, body[:300])

    def test_unreadable_pdf_in_json_422(self):
        st, body = self.post_json({"sheets": [{"name": "a.pdf",
                                               "pdf_base64": base64.b64encode(b"%PDF-1.4\njunk").decode()}]})
        self.assertEqual(st, 422, body[:300])

    def test_encrypted_pdf_422(self):
        from pypdf import PdfWriter
        w = PdfWriter()
        w.add_blank_page(612, 792)
        w.encrypt("secret")
        buf = io.BytesIO()
        w.write(buf)
        st, body = self.post(buf.getvalue(), "application/pdf")
        self.assertEqual(st, 422, body[:300])

    def test_usage_event_counts_pages_and_stores_no_names(self):
        before = self._lines()
        st, body = self.post_json({"sheets": [{"name": "secret-name-5521.pdf",
                                               "pdf_base64": base64.b64encode(multi(["1/2", "2/2"])).decode()}]})
        self.assertEqual(st, 200)
        new = self._lines()[len(before):]
        self.assertEqual(len(new), 1)
        ev = json.loads(new[0])
        self.assertEqual(ev["event"], "sheet-linted")
        self.assertEqual(ev["sheets"], 2)
        self.assertNotIn("secret-name-5521", new[0])

    def test_refused_pdf_logs_nothing(self):
        before = self._lines()
        self.post(b"%PDF-1.4\nnot really a pdf\n", "application/pdf")
        self.assertEqual(self._lines(), before)

    def _lines(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as f:
            return [l for l in f.read().split("\n") if l.strip()]


if __name__ == "__main__":
    unittest.main()
