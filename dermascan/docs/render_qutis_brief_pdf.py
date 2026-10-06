"""
Render ``qutis-mydawa-executive-brief.md`` to PDF (fpdf2).

Usage:
    pip install fpdf2
    python dermascan/docs/render_qutis_brief_pdf.py
"""

from __future__ import annotations

import re
from pathlib import Path

from fpdf import FPDF

_DOCS = Path(__file__).resolve().parent
_MD = _DOCS / "qutis-mydawa-executive-brief.md"
_OUT = _DOCS / "qutis-mydawa-executive-brief.pdf"


class BriefPDF(FPDF):
    """PDF with header/footer for the executive brief."""

    def header(self) -> None:
        if self.page_no() == 1:
            return
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(100, 100, 100)
        self.cell(0, 8, "Qutis Clinical Intelligence - MYDAWA Executive Brief", align="C")
        self.ln(4)

    def footer(self) -> None:
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(120, 120, 120)
        self.cell(0, 10, f"Page {self.page_no()}", align="C")


def _strip_md_inline(text: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = text.replace("\u2014", "-").replace("\u2013", "-")
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("\u2026", "...")
    return text.encode("ascii", "replace").decode("ascii")


def render(md_path: Path, out_path: Path) -> None:
    """
    Convert markdown brief to a readable PDF.

    Args:
        md_path: Source markdown file.
        out_path: Destination PDF path.
    """
    lines = md_path.read_text(encoding="utf-8").splitlines()
    pdf = BriefPDF()
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    pdf.set_margins(18, 18, 18)

    body_w = pdf.w - pdf.l_margin - pdf.r_margin

    for raw in lines:
        line = raw.rstrip()
        if line.strip() == "---":
            pdf.ln(2)
            continue
        if line.startswith("# "):
            pdf.ln(4)
            pdf.set_font("Helvetica", "B", 18)
            pdf.set_text_color(20, 60, 100)
            pdf.multi_cell(body_w, 9, _strip_md_inline(line[2:].strip()))
            pdf.set_text_color(0, 0, 0)
            continue
        if line.startswith("## "):
            pdf.ln(3)
            pdf.set_font("Helvetica", "B", 14)
            pdf.set_text_color(30, 80, 120)
            pdf.multi_cell(body_w, 7, _strip_md_inline(line[3:].strip()))
            pdf.set_text_color(0, 0, 0)
            continue
        if line.startswith("### "):
            pdf.ln(2)
            pdf.set_font("Helvetica", "B", 11)
            pdf.multi_cell(body_w, 6, _strip_md_inline(line[4:].strip()))
            continue
        if line.startswith("|"):
            pdf.set_font("Helvetica", "", 9)
            pdf.multi_cell(body_w, 5, _strip_md_inline(line))
            continue
        if line.startswith("- "):
            pdf.set_font("Helvetica", "", 10)
            pdf.multi_cell(body_w, 5, "  - " + _strip_md_inline(line[2:].strip()))
            continue
        if re.match(r"^\d+\.\s", line):
            pdf.set_font("Helvetica", "", 10)
            pdf.multi_cell(body_w, 5, "  " + _strip_md_inline(line.strip()))
            continue
        if not line.strip():
            pdf.ln(2)
            continue
        pdf.set_font("Helvetica", "", 10)
        pdf.multi_cell(body_w, 5, _strip_md_inline(line))

    pdf.output(str(out_path))


def main() -> None:
    if not _MD.is_file():
        raise SystemExit(f"Missing source: {_MD}")
    render(_MD, _OUT)
    print(f"Wrote {_OUT}")


if __name__ == "__main__":
    main()
