# drawlint

Formality lint for patent drawing sheets in SVG or PDF, against USPTO 37 CFR 1.84.
SVG checks use the Python 3 standard library only; PDF input needs `pypdf==6.21.4` (BSD-3-Clause, `requirements.txt`).

**Automated formality check only, not legal advice.** Every report and every listing carries this line.

It checks customers' files. It ships no drawings, filing text or other lab material.

## Use

```bash
cd products/drawlint
python3 -m drawlint sheet1.svg sheet2.svg drawings.pdf [--profile auto|letter|a4] [--numerals 10,12,14a] [--json]
python3 -m drawlint.server --port 8787 --usage-log usage.jsonl
```

Exit codes: `0` no errors, `1` formality errors found, `2` the input could not be read.

HTTP: `POST /v1/lint` with one SVG (`Content-Type: image/svg+xml`) or one PDF (`application/pdf`), `?profile=`;
or JSON `{"sheets": [{"name": "...", "svg": "..."} or {"name": "...", "pdf_base64": "..."}], "numerals": ["10"],
"profile": "auto"}` (bad base64: 400). At most 100 sheets per request after PDFs are split into pages (413). `GET /healthz`.
`GET /` serves the free checker page (`drawlint/static/index.html`): one SVG or PDF at a time, posted to
`/v1/lint`; every value from the report is HTML-escaped before display; the page loads nothing from other sites and
is sent with a Content-Security-Policy that allows only its own inline script and style and requests to the same server.
Status codes: 200 report, 400 bad request, 404, 411 no length, 413 too big (over 20 MB or over 100 sheets),
415 wrong content type, 422 unreadable SVG. Each 200 appends one usage event
`{"time", "event": "sheet-linted", "sheets", "request_id"}` to the usage log; no drawing content,
file names or client addresses are stored. Nothing charges anyone.

## Spec (what each check does)

One SVG file is one sheet; one PDF page is one sheet; a run is the whole set in order (files in the order given,
PDF pages in page order). A multi-page PDF's sheets are named `<file> page <n>`.

### PDF intake

Each page's content stream is interpreted (not rendered) into an equivalent SVG sheet in points, and that sheet
goes through exactly the checks below. Page report: `units` is `PDF page in points`, `pdf_page` is the page number,
`sha256` is the hash of the PDF file.

- Sheet size: the CropBox (MediaBox when there is none), times `/UserUnit`; `/Rotate` 90 or 270 swaps width
  and height, and all content is rotated the same way (a landscape MediaBox with `/Rotate 90` is a portrait sheet).
- Geometry: paths (`m l c v y h re`) painted with `S s f F f* B B* b b*` (`n` paints nothing), with the current
  transform (`cm`, `q`/`Q`), line width (`w`, ExtGState `/LW`) and opacity (ExtGState `/CA` stroke, `/ca` fill).
  A path that is exactly one `re` is a rectangle (so it can be a frame). Line width 0 is a 0.01 pt line (thin-line).
- Colour: DeviceGray, DeviceRGB, DeviceCMYK (converted to RGB: r = (1-c)(1-k) and so on), ICCBased (by its
  component count), CalGray/CalRGB, Indexed (looked up in its base space), Separation/DeviceN: tint 0 is white;
  a colourant named `Black` or `All` is grey 1 - tint (tint 1 is pure black); any other colourant with tint above 0 is an
  `ink` error naming the colourant as written (for example `spot-ink:PANTONE 185 C`). Default colour is black.
  CMYK with k = 1 is black whatever c, m and y are (rich black), by the formula above.
- Shadings and patterns: the `sh` operator and shading patterns (PatternType 2) are `gradient` errors. Tiling
  patterns: their content is ink-checked; an uncoloured tiling pattern takes the colour given with it.
- Images: image XObjects and inline images are `raster-image` advisories, with the image's area as geometry.
- Form XObjects: drawn with their `/Matrix` and own `/Resources`, nested up to 8 deep (deeper: refused).
- Text: fonts are decoded through pypdf (encodings, ToUnicode maps, CID fonts). Font size in points is the `Tf`
  size times the vertical scale of the text matrix and transform. Text render mode 3 and 7 (invisible) is skipped;
  modes 1, 2, 5, 6 stroke the text with the line width. Pieces of text shown separately on the same baseline,
  in the same size and paint, are joined into one text when the gap is at most 0.6 em (with a space when the gap
  is over 0.15 em), so `1`, `/`, `3` drawn separately read as the sheet number `1/3`. This holds across separate
  text objects (`BT ... ET`), since many writers put each glyph in its own.
- Not interpreted: clipping (geometry outside a clip still counts), hidden optional-content layers (checked as
  visible), soft masks, annotations and form fields.
- Damaged files that pypdf can repair are checked, with one `pdf-repaired` advisory on the first page listing
  up to five repairs. Refused (exit 2 / HTTP 422): not readable as PDF, encrypted, no pages, over 20 MB, a page
  with over 200,000 drawing elements. Over 100 pages in one PDF: exit 2 / HTTP 413.
- Form XObject nesting: the page is level 0; a form inside a form inside the page is level 2; level 9 is refused.

| Code | Severity | Rule | Fires when |
|---|---|---|---|
| `page-size` | error | 1.84(f) | sheet is not A4 (595.28 x 841.89 pt) or letter (612 x 792 pt) within 2 pt; or is landscape; or differs from `--profile` |
| `margins` | error | 1.84(g) | any visible geometry (stroke half-width included) is outside the sight by more than 0.5 pt. Letter sight: x 72..567, y 72..765 pt. A4: top/left 25 mm, right 15 mm, bottom 10 mm |
| `frame` | error | 1.84(g) | a stroked rect covers at least 90% of the sight area with each edge within 20 pt of the sight edge |
| `ink` | error | 1.84(a)(1) | a painted fill/stroke/marker/pattern colour is not black or white (near-black excepted) |
| `near-black` | advisory | 1.84(a)(1) | colour with every channel at or below 40/255, not pure black |
| `gradient` | error | 1.84(a)(1) | fill or stroke uses a linear or radial gradient |
| `translucent` | error | 1.84(l) | black or near-black ink with effective opacity strictly between 0 and 1 (opacity, fill-/stroke-opacity, alpha in the colour, multiplied down the tree) |
| `thin-line` | advisory | 1.84(l) | visible stroke with effective width above 0 and below 0.5 pt |
| `text-height` | error | 1.84(p)(3) | effective font size below 9 pt (1/8 in). Font size is the em box, not cap height |
| `no-live-text` | advisory | 1.84(p)(3) | no text elements at all |
| `raster-image` | advisory | 1.84(b) | an `<image>` is drawn |
| `sheet-number-format` | error | 1.84(t) | text like `Sheet 1 of 3` or `sheet 1/3` |
| `sheet-number-missing` | advisory | 1.84(t) | no text that is exactly `n/m`, and no wrong-format sheet number |
| `sheet-number-position` | advisory | 1.84(t) | the `n/m` text sits below the top quarter of the sight |
| `sheet-number-size` | advisory | 1.84(t) | the `n/m` text is not larger than the largest purely numeric label |
| `sheet-number-sequence` | error | 1.84(t) | n < 1 or n > m on a sheet; across a set: totals disagree, or the numbers, in the order the sheets were given, are not 1..N of N |
| `fig-label-format` | advisory | 1.84(u)(1) | `Fig 2`, `Figure 2`, `FIG 2` (anything but `FIG.` + number) |
| `fig-sequence` | error | 1.84(u)(1) | across the set, `FIG.` numbers skip or do not start at 1 (letters such as 2A are partial views of 2) |
| `single-view` | advisory | 1.84(u)(1) | exactly one `FIG.` label in the whole set |
| `mixed-sizes` | error | 1.84(f) | sheets in the set have different sizes |
| `numeral-missing` | error | 1.84(p)(5) | a `--numerals` value appears as a whole token on no sheet (view labels such as `FIG. 10` and sheet numbers such as `1/3` do not count) |
| `unsupported-css` | advisory | - | a `<style>` selector other than tag, `.class`, `#id` or their compounds |
| `unresolved-ref` | advisory | - | `url(#x)` or `<use href="#x">` points at nothing |
| `pdf-repaired` | advisory | - | the PDF had structural damage that was repaired when reading it |

Units: `in`, `mm`, `cm`, `pt`, `pc` are exact. `px` or unitless width/height are read as CSS pixels
(96 per inch) unless only the 72-per-inch reading matches a legal sheet size (Illustrator-style files).
Without width/height, the viewBox is used the same way. Without a viewBox, one user unit is one CSS px (0.75 pt), except on px/unitless sheets read at 72 per inch, where it is one point. `viewBox` and `preserveAspectRatio` are honoured.

Supported: transforms (matrix, translate, scale, rotate, skewX, skewY), nested `g`/`svg`, `use` and `symbol`
(depth limit 8), `<style>` with simple selectors, presentation attributes, inline `style`, inheritance,
`display:none`, `visibility:hidden`, `currentColor`, hex (3/4/6/8 digits), `rgb()`/`rgba()`, paths with all
commands (absolute and relative, compact arc flags), exact Bezier extrema, sampled arcs.

Input refused (exit 2 / HTTP 422): not XML, root not `<svg>`, any `<!ENTITY` declaration, over 20 MB,
over 200,000 elements, no width/height and no viewBox, `use` nesting past 8.

Not checked yet: cap height of outlined text, reference characters crossing lines (1.84(p)(3)),
scan target points, view orientation, numerals against the specification text.


## Related

Need the drawings themselves? [FigSheet](https://figsheet.com) turns a STEP CAD file into USPTO-style sheets
and checks every sheet with drawlint. Ordering is open: upload a STEP file for a free watermarked preview, then
$49 per file for the full set (vector PDF, SVG per sheet, check report).

## Licence

MIT. Automated formality check only, not legal advice.
