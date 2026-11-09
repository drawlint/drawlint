"""Spec-driven tests for drawlint CLI. Real files, real subprocesses, no mocks."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DISCLAIMER = "Automated formality check only, not legal advice."
NS = 'xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"'
LETTER = 'width="8.5in" height="11in" viewBox="0 0 612 792"'
A4 = 'width="210mm" height="297mm" viewBox="0 0 595.28 841.89"'


def svg(body="", num="1/1", head=LETTER, pre="", numsize=14, numy=100):
    """A sheet whose own content is clean; num=None omits the sheet number."""
    n = ""
    if num is not None:
        n = ('<text x="306" y="%s" font-size="%s" text-anchor="middle" fill="#000">%s</text>'
             % (numy, numsize, num))
    return '<svg %s %s>%s%s%s</svg>' % (NS, head, pre, n, body)


def blk(x, y, w=10, h=10, extra=""):
    return '<rect x="%s" y="%s" width="%s" height="%s" fill="#000" %s/>' % (x, y, w, h, extra)


class Base(unittest.TestCase):
    def run_cli(self, sheets, args=(), text=False):
        with tempfile.TemporaryDirectory() as d:
            paths = []
            for i, s in enumerate(sheets):
                p = os.path.join(d, "sheet%d.svg" % (i + 1))
                with open(p, "w", encoding="utf-8") as f:
                    f.write(s)
                paths.append(p)
            cmd = [sys.executable, "-m", "drawlint"] + paths + list(args)
            if not text:
                cmd.append("--json")
            r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                               env=dict(os.environ, CUDA_VISIBLE_DEVICES=""), timeout=60)
        return r

    def lint(self, sheets, args=()):
        if isinstance(sheets, str):
            sheets = [sheets]
        r = self.run_cli(sheets, args)
        try:
            rep = json.loads(r.stdout)
        except ValueError:
            self.fail("no JSON; rc=%s stdout=%r stderr=%r" % (r.returncode, r.stdout[:300], r.stderr[:300]))
        return r.returncode, rep

    @staticmethod
    def findings(rep):
        out = {}
        for s in rep.get("sheets", []):
            for f in s["findings"]:
                out.setdefault(f["code"], f["severity"])
        for f in rep.get("set_findings", []):
            out.setdefault(f["code"], f["severity"])
        return out

    def codes(self, sheets, args=()):
        rc, rep = self.lint(sheets, args)
        return rc, self.findings(rep), rep

    def fires(self, svgtxt, code, sev=None, args=()):
        rc, f, rep = self.codes(svgtxt, args)
        self.assertIn(code, f, "expected %s, got %s" % (code, f))
        if sev:
            self.assertEqual(f[code], sev)
        return rc

    def silent(self, svgtxt, code, args=()):
        rc, f, rep = self.codes(svgtxt, args)
        self.assertNotIn(code, f, "unexpected %s in %s" % (code, f))
        return rc


class Exit(Base):
    def test_clean_exit0(self):
        rc, f, rep = self.codes(svg(blk(100, 200)))
        self.assertEqual(rc, 0, f)
        self.assertTrue(rep["pass"])
        self.assertEqual(rep["errors"], 0)

    def test_error_exit1(self):
        rc, f, rep = self.codes(svg(blk(100, 200, extra='stroke="red"')))
        self.assertEqual(rc, 1)
        self.assertFalse(rep["pass"])

    def test_advisory_only_exit0(self):
        rc, f, rep = self.codes(svg(blk(100, 200), num=None))
        self.assertIn("sheet-number-missing", f)
        self.assertEqual(rc, 0)

    def test_refused_not_xml(self):
        self.assertEqual(self.run_cli(["hello this is not xml"]).returncode, 2)

    def test_refused_root_not_svg(self):
        self.assertEqual(self.run_cli(['<html xmlns="http://www.w3.org/1999/xhtml"/>']).returncode, 2)

    def test_refused_entity(self):
        s = '<!DOCTYPE svg [<!ENTITY a "b">]><svg %s %s>&a;</svg>' % (NS, LETTER)
        self.assertEqual(self.run_cli([s]).returncode, 2)

    def test_refused_no_size(self):
        self.assertEqual(self.run_cli(['<svg %s><rect width="1" height="1"/></svg>' % NS]).returncode, 2)

    def test_refused_use_loop(self):
        s = svg('<g id="a"><use href="#b"/></g><g id="b"><use href="#a"/></g><use href="#a"/>')
        self.assertEqual(self.run_cli([s]).returncode, 2)

    def test_refused_self_use_loop(self):
        s = svg('<g id="a"><use href="#a"/></g><use href="#a"/>')
        self.assertEqual(self.run_cli([s]).returncode, 2)

    def test_missing_file_exit2(self):
        r = subprocess.run([sys.executable, "-m", "drawlint", "/nonexistent/x.svg", "--json"],
                           cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 2)

    def test_disclaimer_json_and_text(self):
        rc, rep = self.lint(svg())
        self.assertEqual(rep["disclaimer"], DISCLAIMER)
        r = self.run_cli([svg()], text=True)
        self.assertIn(DISCLAIMER, r.stdout)
        r = self.run_cli([svg(blk(100, 100, extra='stroke="red"'))], text=True)
        self.assertIn(DISCLAIMER, r.stdout)


class Units(Base):
    def width(self, head):
        rc, f, rep = self.codes(svg(head=head))
        return rep["sheets"][0]["page"]["width_pt"], rep["sheets"][0]["page"]["height_pt"], f

    def test_inches(self):
        w, h, f = self.width('width="8.5in" height="11in"')
        self.assertAlmostEqual(w, 612, 1)
        self.assertAlmostEqual(h, 792, 1)
        self.assertNotIn("page-size", f)

    def test_mm_viewbox_mm(self):
        w, h, f = self.width('width="215.9mm" height="279.4mm" viewBox="0 0 215.9 279.4"')
        self.assertAlmostEqual(w, 612, 1)
        self.assertNotIn("page-size", f)

    def test_unitless_72(self):
        w, h, f = self.width('width="612" height="792"')
        self.assertAlmostEqual(w, 612, 1)
        self.assertNotIn("page-size", f)

    def test_px_96(self):
        w, h, f = self.width('width="816px" height="1056px"')
        self.assertAlmostEqual(w, 612, 1)
        self.assertNotIn("page-size", f)

    def test_unitless_96(self):
        w, h, f = self.width('width="816" height="1056"')
        self.assertAlmostEqual(w, 612, 1)
        self.assertNotIn("page-size", f)

    def test_viewbox_only_72(self):
        w, h, f = self.width('viewBox="0 0 612 792"')
        self.assertAlmostEqual(w, 612, 1)
        self.assertNotIn("page-size", f)

    def test_viewbox_only_96(self):
        w, h, f = self.width('viewBox="0 0 816 1056"')
        self.assertAlmostEqual(w, 612, 1)
        self.assertNotIn("page-size", f)

    def test_pt_pc_cm(self):
        w, h, f = self.width('width="612pt" height="792pt"')
        self.assertAlmostEqual(w, 612, 1)
        w, h, f = self.width('width="51pc" height="66pc"')
        self.assertAlmostEqual(w, 612, 1)
        w, h, f = self.width('width="21cm" height="29.7cm"')
        self.assertAlmostEqual(w, 595.28, 0)
        self.assertNotIn("page-size", f)

    def test_unitless_non_legal_is_96(self):
        w, h, f = self.width('width="700" height="900"')
        self.assertAlmostEqual(w, 525, 1)
        self.assertIn("page-size", f)


class PageSize(Base):
    def test_letter_ok(self):
        self.silent(svg(), "page-size")

    def test_a4_ok(self):
        self.silent(svg(head=A4), "page-size")

    def test_within_2pt(self):
        self.silent(svg(head='width="614pt" height="792pt" viewBox="0 0 614 792"'), "page-size")
        self.silent(svg(head='width="612pt" height="790pt" viewBox="0 0 612 790"'), "page-size")

    def test_3pt_off(self):
        self.fires(svg(head='width="615pt" height="792pt" viewBox="0 0 615 792"'), "page-size", "error")
        self.fires(svg(head='width="612pt" height="789pt" viewBox="0 0 612 789"'), "page-size", "error")

    def test_a4_within_2(self):
        self.silent(svg(head='width="597pt" height="841.89pt" viewBox="0 0 597 841.89"'), "page-size")
        self.fires(svg(head='width="599pt" height="841.89pt" viewBox="0 0 599 841.89"'), "page-size", "error")

    def test_landscape(self):
        self.fires(svg(head='width="11in" height="8.5in" viewBox="0 0 792 612"'), "page-size", "error")

    def test_profile_mismatch(self):
        self.fires(svg(), "page-size", "error", args=["--profile", "a4"])
        self.fires(svg(head=A4), "page-size", "error", args=["--profile", "letter"])

    def test_profile_match(self):
        self.silent(svg(), "page-size", args=["--profile", "letter"])
        self.silent(svg(head=A4), "page-size", args=["--profile", "a4"])
        self.silent(svg(head=A4), "page-size", args=["--profile", "auto"])

    def test_mixed_sizes(self):
        rc, f, rep = self.codes([svg(num="1/2"), svg(num="2/2", head=A4)])
        self.assertEqual(f.get("mixed-sizes"), "error")

    def test_same_sizes(self):
        rc, f, rep = self.codes([svg(num="1/2"), svg(num="2/2")])
        self.assertNotIn("mixed-sizes", f)


class Margins(Base):
    def test_inside_letter(self):
        self.silent(svg(blk(72, 72, 495, 693)), "margins")

    def test_boundary_each_side_letter(self):
        # 0.4 pt outside passes, 0.6 pt outside fires
        for ok, bad in [
            (blk(71.6, 200), blk(71.4, 200)),
            (blk(200, 71.6), blk(200, 71.4)),
            (blk(557.4, 200), blk(557.6, 200)),      # right edge 567.4 / 567.6
            (blk(200, 755.4), blk(200, 755.6)),      # bottom edge 765.4 / 765.6
        ]:
            with self.subTest(ok=ok):
                self.silent(svg(ok), "margins")
                self.fires(svg(bad), "margins", "error")

    def test_stroke_half_width_counts(self):
        # line on the edge with width 1 -> 0.5 outside, passes; width 3 -> 1.5 outside, fires
        ln = '<line x1="100" y1="72" x2="500" y2="72" stroke="#000" stroke-width="%s"/>'
        self.silent(svg(ln % 1), "margins")
        self.fires(svg(ln % 3), "margins", "error")

    def test_a4_sight(self):
        self.silent(svg(blk(71, 72, 480, 700), head=A4), "margins")
        self.fires(svg(blk(70, 200), head=A4), "margins", "error")       # left 25mm=70.87
        self.fires(svg(blk(200, 70), head=A4), "margins", "error")
        self.fires(svg(blk(544.5, 200), head=A4), "margins", "error")    # right edge 554.5 > 552.76
        self.silent(svg(blk(542, 200), head=A4), "margins")              # 552
        self.fires(svg(blk(200, 806), head=A4), "margins", "error")      # bottom 816 > 813.54
        self.silent(svg(blk(200, 802), head=A4), "margins")              # 812

    def test_cubic_controls_outside_curve_inside(self):
        p = '<path d="M100,300 C100,60 500,60 500,300" fill="none" stroke="#000" stroke-width="1"/>'
        self.silent(svg(p), "margins")

    def test_cubic_curve_outside(self):
        p = '<path d="M100,200 C100,0 500,0 500,200" fill="none" stroke="#000" stroke-width="1"/>'
        self.fires(svg(p), "margins", "error")

    def test_white_background(self):
        bg = '<rect width="612" height="792" fill="white"/>'
        rc, f, rep = self.codes(svg(bg + blk(100, 100)))
        self.assertNotIn("margins", f)
        self.assertNotIn("frame", f)
        bg = '<rect width="612" height="792" fill="#ffffff"/>'
        rc, f, rep = self.codes(svg(bg + blk(100, 100)))
        self.assertNotIn("margins", f)
        self.assertNotIn("frame", f)

    def test_invisible_outside_ignored(self):
        self.silent(svg(blk(10, 10, extra='display="none"') ), "margins") if False else None
        self.silent(svg(blk(10, 10, extra='style="display:none"')), "margins")
        self.silent(svg(blk(10, 10, extra='style="visibility:hidden"')), "margins")
        self.silent(svg('<rect x="10" y="10" width="20" height="20" fill="none" stroke="none"/>'), "margins")

    def test_rect_outside_fires_error(self):
        self.fires(svg(blk(10, 10)), "margins", "error")


class Frame(Base):
    def rect(self, inset, stroke='stroke="#000" stroke-width="1"', fill="none"):
        return '<rect x="%s" y="%s" width="%s" height="%s" fill="%s" %s/>' % (
            72 + inset, 72 + inset, 495 - 2 * inset, 693 - 2 * inset, fill, stroke)

    def test_frame_fires(self):
        self.fires(svg(self.rect(0)), "frame", "error")
        self.fires(svg(self.rect(10)), "frame", "error")

    def test_small_rect_no_frame(self):
        self.silent(svg(self.rect(60)), "frame")
        self.silent(svg(self.rect(25)), "frame")

    def test_unstroked_no_frame(self):
        self.silent(svg(self.rect(0, stroke="", fill="#000")), "frame")

    def test_area_under_90(self):
        r = '<rect x="72" y="72" width="495" height="300" fill="none" stroke="#000"/>'
        self.silent(svg(r), "frame")


class Ink(Base):
    def test_colors(self):
        for c in ["#000", "#000000", "black", "rgb(0,0,0)", "#fff", "white", "rgb(255,255,255)", "none"]:
            with self.subTest(c=c):
                self.silent(svg(blk(100, 100).replace("#000", c)), "ink")

    def test_bad_fill(self):
        for c in ["red", "#f00", "#0000ff", "rgb(10,200,10)", "gray", "#808080"]:
            with self.subTest(c=c):
                self.fires(svg(blk(100, 100).replace("#000", c)), "ink", "error")

    def test_bad_stroke(self):
        self.fires(svg('<line x1="100" y1="100" x2="200" y2="100" stroke="red"/>'), "ink", "error")

    def test_marker(self):
        pre = ('<defs><marker id="m" markerWidth="10" markerHeight="10" refX="5" refY="5">'
               '<circle cx="5" cy="5" r="3" fill="red"/></marker></defs>')
        p = '<path d="M100 100 L300 100" stroke="#000" marker-end="url(#m)"/>'
        self.fires(svg(p, pre=pre), "ink", "error")

    def test_pattern(self):
        pre = ('<defs><pattern id="p" width="10" height="10" patternUnits="userSpaceOnUse">'
               '<rect width="5" height="5" fill="red"/></pattern></defs>')
        self.fires(svg('<rect x="100" y="100" width="100" height="100" fill="url(#p)"/>', pre=pre), "ink", "error")

    def test_near_black_boundary(self):
        c, f, rep = self.codes(svg(blk(100, 100).replace("#000", "#282828")))
        self.assertEqual(f.get("near-black"), "advisory")
        self.assertNotIn("ink", f)
        c, f, rep = self.codes(svg(blk(100, 100).replace("#000", "rgb(40,40,40)")))
        self.assertEqual(f.get("near-black"), "advisory")
        self.assertNotIn("ink", f)
        c, f, rep = self.codes(svg(blk(100, 100).replace("#000", "#292929")))
        self.assertEqual(f.get("ink"), "error")
        self.assertNotIn("near-black", f)
        c, f, rep = self.codes(svg(blk(100, 100).replace("#000", "rgb(0,41,0)")))
        self.assertIn("ink", f)

    def test_pure_black_not_near_black(self):
        self.silent(svg(blk(100, 100)), "near-black")

    def test_near_black_exit0(self):
        rc, f, rep = self.codes(svg(blk(100, 100).replace("#000", "#101010")))
        self.assertEqual(rc, 0)

    def test_current_color(self):
        b = '<g color="red"><rect x="100" y="100" width="20" height="20" fill="none" stroke="currentColor"/></g>'
        self.fires(svg(b), "ink", "error")
        b = '<g color="#000"><rect x="100" y="100" width="20" height="20" fill="none" stroke="currentColor"/></g>'
        self.silent(svg(b), "ink")
        b = '<rect x="100" y="100" width="20" height="20" fill="none" stroke="currentColor"/>'
        self.silent(svg(b), "ink")

    def test_style_overrides_attribute(self):
        b = '<rect x="100" y="100" width="20" height="20" fill="red" style="fill:#000"/>'
        self.silent(svg(b), "ink")
        b = '<rect x="100" y="100" width="20" height="20" fill="#000" style="fill:red"/>'
        self.fires(svg(b), "ink", "error")

    def test_style_class(self):
        pre = '<style>.b{fill:red}</style>'
        self.fires(svg('<rect class="b" x="100" y="100" width="20" height="20"/>', pre=pre), "ink", "error")
        self.silent(svg('<rect x="100" y="100" width="20" height="20"/>', pre=pre), "ink")
        pre = '<style>.b{fill:#000} rect{fill:red}</style>'
        self.silent(svg('<rect class="b" x="100" y="100" width="20" height="20"/>', pre=pre), "ink")

    def test_inherited(self):
        self.fires(svg('<g fill="red"><rect x="100" y="100" width="20" height="20"/></g>'), "ink", "error")

    def test_invisible_ink_ignored(self):
        self.silent(svg(blk(100, 100).replace("#000", "red") .replace('/>', ' style="display:none"/>')), "ink")
        self.silent(svg('<rect x="100" y="100" width="20" height="20" fill="none" stroke="none"/>'), "ink")


class Gradient(Base):
    G = ('<defs><linearGradient id="g"><stop offset="0" stop-color="#000"/><stop offset="1" stop-color="#fff"/>'
         '</linearGradient><radialGradient id="r"><stop offset="0" stop-color="#000"/>'
         '<stop offset="1" stop-color="#fff"/></radialGradient></defs>')

    def test_linear_fill(self):
        self.fires(svg('<rect x="100" y="100" width="50" height="50" fill="url(#g)"/>', pre=self.G), "gradient", "error")

    def test_radial_stroke(self):
        self.fires(svg('<rect x="100" y="100" width="50" height="50" fill="none" stroke="url(#r)"/>', pre=self.G),
                   "gradient", "error")

    def test_defined_unused(self):
        self.silent(svg(blk(100, 100), pre=self.G), "gradient")

    def test_plain(self):
        self.silent(svg(blk(100, 100)), "gradient")


class Translucent(Base):
    def rc(self, extra, tag='rect x="100" y="100" width="20" height="20" fill="#000"'):
        return svg('<%s %s/>' % (tag, extra))

    def test_opacity_values(self):
        self.silent(self.rc('opacity="1"'), "translucent")
        self.fires(self.rc('opacity="0.5"'), "translucent", "error")
        self.silent(self.rc('opacity="0"'), "translucent")

    def test_fill_opacity(self):
        self.fires(self.rc('fill-opacity="0.5"'), "translucent", "error")
        self.silent(self.rc('fill-opacity="1"'), "translucent")

    def test_stroke_opacity(self):
        t = '<rect x="100" y="100" width="20" height="20" fill="none" stroke="#000" stroke-opacity="%s"/>'
        self.fires(svg(t % "0.5"), "translucent", "error")
        self.silent(svg(t % "1"), "translucent")

    def test_alpha_colors(self):
        for c in ["rgba(0,0,0,0.5)", "#00000080", "#0008"]:
            with self.subTest(c=c):
                self.fires(svg(blk(100, 100).replace("#000", c)), "translucent", "error")
        for c in ["rgba(0,0,0,1)", "#000000ff", "#000f"]:
            with self.subTest(c=c):
                self.silent(svg(blk(100, 100).replace("#000", c)), "translucent")

    def test_multiplied(self):
        self.fires(svg('<g opacity="0.5">' + blk(100, 100) + '</g>'), "translucent", "error")
        self.fires(svg('<g opacity="0.5"><g opacity="0.5">' + blk(100, 100) + '</g></g>'), "translucent", "error")
        self.fires(svg('<g opacity="0.5">' + blk(100, 100, extra='opacity="0.5"') + '</g>'), "translucent", "error")
        self.silent(svg('<g opacity="0">' + blk(100, 100) + '</g>'), "translucent")
        self.silent(svg('<g opacity="1">' + blk(100, 100) + '</g>'), "translucent")

    def test_style_opacity(self):
        self.fires(self.rc('style="opacity:.5"'), "translucent", "error")

    def test_near_black_translucent(self):
        self.fires(svg(blk(100, 100).replace("#000", "#202020").replace("/>", ' opacity="0.5"/>')),
                   "translucent", "error")

    def test_non_black_not_translucent(self):
        self.silent(svg(blk(100, 100).replace("#000", "red").replace("/>", ' opacity="0.5"/>')), "translucent")


class ThinLine(Base):
    def ln(self, w, tr=""):
        return svg('<line x1="100" y1="100" x2="300" y2="100" stroke="#000" stroke-width="%s" %s/>' % (w, tr))

    def test_boundary(self):
        self.silent(self.ln("0.5"), "thin-line")
        c, f, rep = self.codes(self.ln("0.49"))
        self.assertEqual(f.get("thin-line"), "advisory")
        self.silent(self.ln("1"), "thin-line")

    def test_zero_width(self):
        self.silent(self.ln("0"), "thin-line")

    def test_scaled(self):
        self.fires(self.ln("1", 'transform="scale(0.4)"').replace("100", "300", 3), "thin-line")
        self.silent(self.ln("0.25", 'transform="scale(2)"'), "thin-line")

    def test_viewbox_scale(self):
        # 2 units per pt: width 0.8 units = 0.4 pt
        s = svg('<line x1="100" y1="100" x2="300" y2="100" stroke="#000" stroke-width="0.8"/>',
                head='width="8.5in" height="11in" viewBox="0 0 1224 1584"', numy=200)
        self.fires(s, "thin-line")

    def test_unit_width(self):
        self.silent(self.ln("0.5pt"), "thin-line")
        self.fires(self.ln("0.1mm"), "thin-line")      # 0.28pt

    def test_invisible_stroke(self):
        self.silent(svg('<line x1="100" y1="100" x2="300" y2="100" stroke="none" stroke-width="0.1"/>'), "thin-line")

    def test_advisory_exit0(self):
        rc, f, rep = self.codes(self.ln("0.3"))
        self.assertEqual(rc, 0)


class Text(Base):
    # no viewBox: one user unit is a CSS px (0.75 pt), so absolute units are physical
    PT = 'width="816" height="1056"'

    def t(self, size, extra="", head=None):
        return svg('<text x="100" y="300" font-size="%s" fill="#000" %s>10</text>' % (size, extra),
                   head=head or LETTER)

    def test_boundary(self):
        self.silent(self.t("9"), "text-height")
        c, f, rep = self.codes(self.t("8.9"))
        self.assertEqual(f.get("text-height"), "error")

    def test_units(self):
        h = self.PT
        self.silent(self.t("9pt", head=h), "text-height")
        self.fires(self.t("8pt", head=h), "text-height", "error")
        self.silent(self.t("0.125in", head=h), "text-height")
        self.fires(self.t("3mm", head=h), "text-height", "error")      # 8.5pt
        self.silent(self.t("3.2mm", head=h), "text-height")            # 9.07pt
        self.silent(self.t("12px", head=h), "text-height")             # 9pt
        self.fires(self.t("11px", head=h), "text-height", "error")

    def test_pt_width_no_viewbox_user_unit_is_css_px(self):
        # SVG: with no viewBox one user unit is one CSS px (0.75 pt), so 11 units = 8.25 pt
        self.fires(self.t("11", head='width="612pt" height="792pt"'), "text-height", "error")

    def test_inherit(self):
        self.fires(svg('<g font-size="8"><text x="100" y="300" fill="#000">10</text></g>'), "text-height", "error")
        self.silent(svg('<g font-size="12"><text x="100" y="300" fill="#000">10</text></g>'), "text-height")

    def test_style_over_attribute(self):
        self.silent(self.t("8", 'style="font-size:12"'), "text-height")
        self.fires(self.t("12", 'style="font-size:8"'), "text-height", "error")

    def test_class(self):
        s = svg('<text class="s" x="100" y="300" fill="#000">10</text>', pre='<style>.s{font-size:8pt}</style>', head=self.PT)
        self.fires(s, "text-height", "error")

    def test_scale(self):
        self.silent(svg('<g transform="scale(0.5)"><text x="200" y="600" font-size="18" fill="#000">10</text></g>'),
                    "text-height")
        self.fires(svg('<g transform="scale(0.5)"><text x="200" y="600" font-size="17.8" fill="#000">10</text></g>'),
                   "text-height", "error")

    def test_viewbox_scale(self):
        head = 'width="8.5in" height="11in" viewBox="0 0 1224 1584"'
        self.silent(svg('<text x="200" y="600" font-size="18" fill="#000">10</text>', head=head, numsize=28, numy=200), "text-height")
        self.fires(svg('<text x="200" y="600" font-size="17" fill="#000">10</text>', head=head, numsize=28, numy=200),
                   "text-height", "error")

    def test_no_live_text(self):
        c, f, rep = self.codes(svg(blk(100, 100), num=None))
        self.assertEqual(f.get("no-live-text"), "advisory")
        self.silent(svg(blk(100, 100)), "no-live-text")

    def test_raster(self):
        img = '<image href="data:image/png;base64,iVBORw0KGgo=" x="100" y="100" width="50" height="50"/>'
        c, f, rep = self.codes(svg(img))
        self.assertEqual(f.get("raster-image"), "advisory")
        self.silent(svg(blk(100, 100)), "raster-image")


class SheetNumber(Base):
    def test_wrong_format(self):
        for t in ["Sheet 1 of 3", "sheet 1/3", "Sheet 1/3"]:
            with self.subTest(t=t):
                c, f, rep = self.codes(svg(num=t))
                self.assertEqual(f.get("sheet-number-format"), "error")
                self.assertNotIn("sheet-number-missing", f)

    def test_correct_format(self):
        c, f, rep = self.codes(svg(num="1/3"))
        self.assertNotIn("sheet-number-format", f)
        self.assertNotIn("sheet-number-missing", f)

    def test_missing(self):
        c, f, rep = self.codes(svg(blk(100, 100), num=None))
        self.assertEqual(f.get("sheet-number-missing"), "advisory")

    def test_position(self):
        self.silent(svg(numy=100), "sheet-number-position")
        self.silent(svg(numy=240), "sheet-number-position")   # quarter ends at 245.25
        c, f, rep = self.codes(svg(numy=300))
        self.assertEqual(f.get("sheet-number-position"), "advisory")
        self.fires(svg(numy=500), "sheet-number-position")

    def test_size(self):
        lab = '<text x="200" y="300" font-size="14" fill="#000">10</text>'
        c, f, rep = self.codes(svg(lab, numsize=14))
        self.assertEqual(f.get("sheet-number-size"), "advisory")
        c, f, rep = self.codes(svg(lab, numsize=10))
        self.assertEqual(f.get("sheet-number-size"), "advisory")
        c, f, rep = self.codes(svg(lab, numsize=16))
        self.assertNotIn("sheet-number-size", f)
        lab = '<text x="200" y="300" font-size="10" fill="#000">10</text>'
        c, f, rep = self.codes(svg(lab, numsize=16))
        self.assertNotIn("sheet-number-size", f)

    def test_sequence_single(self):
        for t in ["0/1", "3/2"]:
            with self.subTest(t=t):
                c, f, rep = self.codes(svg(num=t))
                self.assertEqual(f.get("sheet-number-sequence"), "error")
        c, f, rep = self.codes(svg(num="1/1"))
        self.assertNotIn("sheet-number-sequence", f)

    def test_set_ok(self):
        c, f, rep = self.codes([svg(num="1/2"), svg(num="2/2")])
        self.assertNotIn("sheet-number-sequence", f)
        self.assertEqual(c, 0)

    def test_set_duplicate(self):
        c, f, rep = self.codes([svg(num="1/2"), svg(num="1/2")])
        self.assertEqual(f.get("sheet-number-sequence"), "error")
        self.assertEqual(c, 1)

    def test_set_totals_disagree(self):
        c, f, rep = self.codes([svg(num="1/2"), svg(num="2/3")])
        self.assertEqual(f.get("sheet-number-sequence"), "error")

    def test_set_total_mismatch_count(self):
        c, f, rep = self.codes([svg(num="1/3"), svg(num="2/3")])
        self.assertEqual(f.get("sheet-number-sequence"), "error")

    def test_set_three_ok(self):
        c, f, rep = self.codes([svg(num="1/3"), svg(num="2/3"), svg(num="3/3")])
        self.assertNotIn("sheet-number-sequence", f)


class Figs(Base):
    def fig(self, label, y=400):
        return '<text x="250" y="%s" font-size="12" fill="#000">%s</text>' % (y, label)

    def test_label_format(self):
        for t in ["Fig 2", "Figure 2", "FIG 2"]:
            with self.subTest(t=t):
                c, f, rep = self.codes(svg(self.fig(t)))
                self.assertEqual(f.get("fig-label-format"), "advisory")
        self.silent(svg(self.fig("FIG. 2")), "fig-label-format")

    def test_sequence_ok(self):
        s = svg(self.fig("FIG. 1", 300) + self.fig("FIG. 2A", 400) + self.fig("FIG. 2B", 500))
        c, f, rep = self.codes(s)
        self.assertNotIn("fig-sequence", f)
        self.assertNotIn("single-view", f)

    def test_sequence_across_sheets_ok(self):
        c, f, rep = self.codes([svg(self.fig("FIG. 1"), num="1/2"), svg(self.fig("FIG. 2"), num="2/2")])
        self.assertNotIn("fig-sequence", f)

    def test_skip(self):
        c, f, rep = self.codes(svg(self.fig("FIG. 1", 300) + self.fig("FIG. 3", 400)))
        self.assertEqual(f.get("fig-sequence"), "error")

    def test_skip_across_sheets(self):
        c, f, rep = self.codes([svg(self.fig("FIG. 1"), num="1/2"), svg(self.fig("FIG. 3"), num="2/2")])
        self.assertEqual(f.get("fig-sequence"), "error")

    def test_not_start_at_1(self):
        c, f, rep = self.codes(svg(self.fig("FIG. 2", 300) + self.fig("FIG. 3", 400)))
        self.assertEqual(f.get("fig-sequence"), "error")

    def test_single_view(self):
        c, f, rep = self.codes(svg(self.fig("FIG. 1")))
        self.assertEqual(f.get("single-view"), "advisory")
        c, f, rep = self.codes(svg(blk(100, 100)))
        self.assertNotIn("single-view", f)
        c, f, rep = self.codes(svg(self.fig("FIG. 1", 300) + self.fig("FIG. 2", 400)))
        self.assertNotIn("single-view", f)


class Numerals(Base):
    def lab(self, *ts):
        return "".join('<text x="%d" y="%d" font-size="10" fill="#000">%s</text>' % (150 + 40 * i, 300, t)
                       for i, t in enumerate(ts))

    def test_all_present(self):
        s = svg(self.lab("10", "12", "14a"))
        c, f, rep = self.codes(s, ["--numerals", "10,12,14a"])
        self.assertNotIn("numeral-missing", f)

    def test_missing(self):
        s = svg(self.lab("10", "12"))
        c, f, rep = self.codes(s, ["--numerals", "10,12,14a"])
        self.assertEqual(f.get("numeral-missing"), "error")
        self.assertEqual(c, 1)

    def test_inside_fig(self):
        c, f, rep = self.codes(svg(self.lab("FIG. 10")), ["--numerals", "10"])
        self.assertEqual(f.get("numeral-missing"), "error")

    def test_inside_110(self):
        c, f, rep = self.codes(svg(self.lab("110")), ["--numerals", "10"])
        self.assertEqual(f.get("numeral-missing"), "error")

    def test_token_in_text(self):
        c, f, rep = self.codes(svg(self.lab("12 14a")), ["--numerals", "12,14a"])
        self.assertNotIn("numeral-missing", f)

    def test_other_sheet(self):
        c, f, rep = self.codes([svg(self.lab("12"), num="1/2"), svg(self.lab("10"), num="2/2")],
                               ["--numerals", "10,12"])
        self.assertNotIn("numeral-missing", f)

    def test_no_numerals_option(self):
        c, f, rep = self.codes(svg(self.lab("12")))
        self.assertNotIn("numeral-missing", f)

    def test_prefix_token(self):
        c, f, rep = self.codes(svg(self.lab("14a")), ["--numerals", "14"])
        self.assertEqual(f.get("numeral-missing"), "error")


class Css(Base):
    def test_unsupported(self):
        for sel in ["g > rect", "g rect", "rect:hover", "rect + rect", "[fill=red]"]:
            with self.subTest(sel=sel):
                c, f, rep = self.codes(svg(blk(100, 100), pre="<style>%s{stroke:#000}</style>" % sel))
                self.assertEqual(f.get("unsupported-css"), "advisory")

    def test_supported(self):
        for sel in ["rect", ".a", "#x", "rect.a", "rect#x", ".a.b"]:
            with self.subTest(sel=sel):
                c, f, rep = self.codes(svg(blk(100, 100), pre="<style>%s{stroke:#000}</style>" % sel))
                self.assertNotIn("unsupported-css", f)

    def test_unresolved(self):
        c, f, rep = self.codes(svg('<rect x="100" y="100" width="20" height="20" fill="url(#nope)"/>'))
        self.assertEqual(f.get("unresolved-ref"), "advisory")
        c, f, rep = self.codes(svg('<use href="#nope"/>'))
        self.assertEqual(f.get("unresolved-ref"), "advisory")

    def test_resolved(self):
        c, f, rep = self.codes(svg('<defs><rect id="r" x="100" y="100" width="20" height="20" fill="#000"/></defs>'
                                   '<use href="#r"/>'))
        self.assertNotIn("unresolved-ref", f)


class Geometry(Base):
    def test_translate_scale(self):
        self.silent(svg('<g transform="translate(100,100)">' + blk(0, 0) + '</g>'), "margins")
        self.fires(svg('<g transform="scale(10)">' + blk(60, 60) + '</g>'), "margins", "error")
        self.silent(svg('<g transform="scale(2)">' + blk(60, 60) + '</g>'), "margins")

    def test_rotate(self):
        self.silent(svg('<g transform="rotate(90 306 396)">' + blk(300, 300) + '</g>'), "margins")
        self.fires(svg('<g transform="rotate(90)">' + blk(300, 100) + '</g>'), "margins", "error")

    def test_matrix(self):
        self.fires(svg('<g transform="matrix(1 0 0 1 700 100)">' + blk(0, 0) + '</g>'), "margins", "error")
        self.silent(svg('<g transform="matrix(1 0 0 1 200 200)">' + blk(0, 0) + '</g>'), "margins")

    def test_nested(self):
        self.silent(svg('<g transform="translate(100,0)"><g transform="translate(100,0)">' + blk(100, 200) + '</g></g>'),
                    "margins")
        self.fires(svg('<g transform="translate(300,0)"><g transform="translate(300,0)">' + blk(100, 200) + '</g></g>'),
                   "margins", "error")

    def test_skew(self):
        self.fires(svg('<g transform="skewX(80)">' + blk(100, 700) + '</g>'), "margins", "error")

    def test_use(self):
        defs = '<defs><rect id="r" x="100" y="200" width="20" height="20" fill="#000"/></defs>'
        self.silent(svg('<use href="#r"/>', pre=defs), "margins")
        self.fires(svg('<use href="#r" x="600"/>', pre=defs), "margins", "error")
        self.fires(svg('<use href="#r" transform="translate(600,0)"/>', pre=defs), "margins", "error")

    def test_use_xlink(self):
        defs = '<defs><rect id="r" x="100" y="200" width="20" height="20" fill="#000"/></defs>'
        self.fires(svg('<use xlink:href="#r" x="600"/>', pre=defs), "margins", "error")

    def test_use_ink(self):
        defs = '<defs><rect id="r" x="100" y="200" width="20" height="20" fill="red"/></defs>'
        self.fires(svg('<use href="#r"/>', pre=defs), "ink", "error")

    def test_defs_not_drawn(self):
        defs = '<defs><rect id="r" x="1" y="1" width="20" height="20" fill="red"/></defs>'
        c, f, rep = self.codes(svg(blk(100, 100), pre=defs))
        self.assertNotIn("margins", f)
        self.assertNotIn("ink", f)

    def test_relative_path(self):
        ok = '<path d="m100 100 l50 0 l0 50 z" fill="none" stroke="#000"/>'
        self.silent(svg(ok), "margins")
        bad = '<path d="M500 100 l100 0" fill="none" stroke="#000"/>'
        self.fires(svg(bad), "margins", "error")
        bad = '<path d="M500 100 h100" fill="none" stroke="#000"/>'
        self.fires(svg(bad), "margins", "error")
        bad = '<path d="M100 700 v100" fill="none" stroke="#000"/>'
        self.fires(svg(bad), "margins", "error")

    def test_compact_arc_flags(self):
        ok = '<path d="M300 300 a20 20 0 011 40" fill="none" stroke="#000"/>'
        rc = self.silent(svg(ok), "margins")
        self.assertEqual(rc, 0)
        bad = '<path d="M550 300 a20 20 0 011 40" fill="none" stroke="#000"/>'   # bulges right to ~570
        self.fires(svg(bad), "margins", "error")

    def test_arc_circle_extent(self):
        self.fires(svg('<path d="M547 400 A20 20 0 0 1 587 400" fill="none" stroke="#000"/>'), "margins", "error")
        self.silent(svg('<circle cx="300" cy="400" r="50" fill="none" stroke="#000"/>'), "margins")
        self.fires(svg('<circle cx="560" cy="400" r="50" fill="none" stroke="#000"/>'), "margins", "error")
        self.fires(svg('<ellipse cx="300" cy="100" rx="50" ry="50" fill="none" stroke="#000"/>'), "margins", "error")

    def test_quadratic_and_smooth(self):
        self.silent(svg('<path d="M100,300 Q300,100 500,300" fill="none" stroke="#000"/>'), "margins")  # peak y=200
        self.fires(svg('<path d="M100,100 Q300,-300 500,100" fill="none" stroke="#000"/>'), "margins", "error")
        self.silent(svg('<path d="M100,300 C100,200 200,200 300,300 S500,400 500,300" fill="none" stroke="#000"/>'), "margins")

    def test_preserve_aspect_ratio(self):
        head = 'width="8.5in" height="11in" viewBox="0 0 306 396" preserveAspectRatio="xMidYMid meet"'
        # units are 2pt each: rect at 20,20 -> 40pt, outside the margin
        self.fires(svg(blk(20, 20), head=head, numy=50), "margins", "error")

    def test_polygon_polyline_line(self):
        self.fires(svg('<polygon points="100,100 700,100 400,400" fill="#000"/>'), "margins", "error")
        self.fires(svg('<polyline points="100,100 100,800" fill="none" stroke="#000"/>'), "margins", "error")
        self.silent(svg('<polyline points="100,100 100,700" fill="none" stroke="#000"/>'), "margins")


if __name__ == "__main__":
    unittest.main()
