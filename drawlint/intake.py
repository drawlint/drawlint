"""One entry point for any input file: an SVG is one sheet, a PDF is one sheet per page."""

from __future__ import annotations

import hashlib

from . import engine, pdf


def lint_file(data, name="sheet.svg", profile="auto"):
    """Return a list of SheetReports, in page order. Raises engine.InputError."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    if not pdf.is_pdf(raw):
        return [engine.lint_sheet(raw, name=name, profile=profile)]
    sha = hashlib.sha256(raw).hexdigest()
    pages, repairs = pdf.pdf_pages(raw)
    reports = []
    for n, svg in pages:
        r = engine.lint_sheet(svg, name=f"{name} page {n}" if len(pages) > 1 else name, profile=profile)
        r.sha256 = sha
        r.page["units"] = "PDF page in points"
        r.page["pdf_page"] = n
        reports.append(r)
    if repairs:
        reports[0].findings.append(engine.Finding(
            "pdf-repaired", "advisory",
            "the PDF has structural problems that were repaired when reading it; a re-export is safer",
            len(repairs), repairs[:5]))
    return reports
