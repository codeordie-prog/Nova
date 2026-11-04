"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Render a Qutis markdown brief in this directory to PDF (fpdf2).

Usage:
    pip install fpdf2
    python dermascan/docs/render_qutis_brief_pdf.py
    python dermascan/docs/render_qutis_brief_pdf.py qutis-safety-guardrails-overview-anthropic
"""


from __future__ import annotations

import html
import re
import sys
from pathlib import Path

from fpdf import FPDF

_DOCS = Path(__file__).resolve().parent
_DEFAULT_STEM = "qutis-mydawa-executive-brief"

_HEADER_BY_STEM: dict[str, str] = {
    "qutis-mydawa-executive-brief": "Qutis Clinical Intelligence - MYDAWA Executive Brief",
    "qutis-safety-guardrails-overview-anthropic": "Qutis Safety Guardrails - Anthropic Overview",
}


class BriefPDF(FPDF):
    """PDF with header/footer for Qutis briefs."""

    def __init__(self, header_title: str) -> None:
        super().__init__()
        self._header_title = header_title

    def header(self) -> None:
        if self.page_no() == 1:
            return
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(100, 100, 100)
        self.cell(0, 8, self._header_title, align="C")
        self.ln(4)

    def footer(self) -> None:
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(120, 120, 120)
        self.cell(0, 10, f"Page {self.page_no()}", align="C")


def _ascii_safe(text: str) -> str:
    text = text.replace("\u2014", "-").replace("\u2013", "-")
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("\u2026", "...")
    return text.encode("ascii", "replace").decode("ascii")


def _url_display_lines(url: str, max_len: int = 56) -> list[str]:
    """
    Split a URL into short lines at path boundaries for PDF wrapping.

    Args:
        url: Absolute http(s) URL.
        max_len: Soft maximum characters per line.

    Returns:
        One or more display lines (full URL preserved for link targets).
    """
    url = _ascii_safe(url.strip())
    if len(url) <= max_len:
        return [url]
    segments = url.split("/")
    if len(segments) <= 1:
        return [url]
    lines: list[str] = []
    current = segments[0]
    for part in segments[1:]:
        candidate = f"{current}/{part}" if current else part
        if len(candidate) <= max_len:
            current = candidate
            continue
        if current:
            lines.append(current if current.endswith("/") else f"{current}/")
        current = part
    if current:
        lines.append(current)
    return lines or [url]


def _render_url_block(pdf: FPDF, body_w: float, url: str) -> None:
    """Render a clickable URL with path-aware line breaks."""
    pdf.ln(1)
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(0, 40, 120)
    for idx, segment in enumerate(_url_display_lines(url)):
        pdf.multi_cell(body_w, 4.2, segment, link=url)
        if idx == 0:
            pdf.ln(0.5)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(4)


def _md_inline_to_html(text: str) -> str:
    text = _ascii_safe(text)
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda m: f"<b>{html.escape(m.group(1))}</b>",
        text,
    )
    return text


def _is_table_separator(line: str) -> bool:
    return bool(re.match(r"^\|[\s\-:|]+\|$", line.strip()))


def _parse_table_row(line: str) -> list[str]:
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return cells


def render(md_path: Path, out_path: Path) -> None:
    """
    Convert markdown brief to a readable PDF.

    Args:
        md_path: Source markdown file.
        out_path: Destination PDF path.
    """
    stem = md_path.stem
    header_title = _HEADER_BY_STEM.get(stem, "Qutis Brief")
    lines = md_path.read_text(encoding="utf-8").splitlines()
    pdf = BriefPDF(header_title=header_title)
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()
    pdf.set_margins(20, 20, 20)

    body_w = pdf.w - pdf.l_margin - pdf.r_margin

    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()
        i += 1

        if line.strip() == "---":
            pdf.ln(3)
            y = pdf.get_y()
            pdf.set_draw_color(180, 180, 180)
            pdf.line(pdf.l_margin, y, pdf.l_margin + body_w, y)
            pdf.ln(5)
            continue

        if line.startswith("# "):
            pdf.ln(2)
            pdf.set_font("Helvetica", "B", 17)
            pdf.set_text_color(15, 55, 95)
            pdf.multi_cell(body_w, 8, _ascii_safe(line[2:].strip()))
            pdf.set_text_color(0, 0, 0)
            pdf.ln(2)
            continue

        if line.startswith("## "):
            pdf.ln(5)
            pdf.set_font("Helvetica", "B", 13)
            pdf.set_text_color(25, 75, 115)
            pdf.multi_cell(body_w, 7, _ascii_safe(line[3:].strip()))
            pdf.set_text_color(0, 0, 0)
            pdf.ln(1)
            continue

        if line.startswith("### "):
            pdf.ln(3)
            pdf.set_font("Helvetica", "B", 11)
            pdf.multi_cell(body_w, 6, _ascii_safe(line[4:].strip()))
            continue

        if line.startswith("|") and not _is_table_separator(line):
            cells = _parse_table_row(line)
            if len(cells) >= 2 and cells[0]:
                pdf.ln(1)
                pdf.set_font("Helvetica", "B", 10)
                pdf.multi_cell(body_w, 5, _ascii_safe(cells[0]))
                pdf.set_font("Helvetica", "", 10)
                pdf.write_html(
                    f'<p style="font-size: 10pt; line-height: 1.35; margin: 0 0 4px 0;">'
                    f"{_md_inline_to_html(cells[1])}</p>"
                )
                continue
            if len(cells) >= 2 and not cells[0] and cells[1]:
                continue

        if line.startswith("- "):
            pdf.set_font("Helvetica", "", 10)
            pdf.write_html(
                f'<p style="font-size: 10pt; line-height: 1.4; margin: 0 0 3px 0; '
                f'padding-left: 8px;">- {_md_inline_to_html(line[2:].strip())}</p>'
            )
            continue

        if re.match(r"^\d+\.\s", line):
            pdf.set_font("Helvetica", "", 10)
            pdf.write_html(
                f'<p style="font-size: 10pt; line-height: 1.4; margin: 0 0 4px 0; '
                f'padding-left: 4px;">{_md_inline_to_html(line.strip())}</p>'
            )
            continue

        if not line.strip():
            pdf.ln(2)
            continue

        label_url = re.match(r"^\*\*(.+?)\*\*:\s*(https?://\S+)\s*$", line.strip())
        if label_url:
            pdf.set_font("Helvetica", "B", 10)
            pdf.multi_cell(body_w, 5, _ascii_safe(label_url.group(1) + ":"))
            _render_url_block(pdf, body_w, label_url.group(2))
            continue

        if line.strip().startswith("http"):
            _render_url_block(pdf, body_w, line.strip())
            continue

        stripped = line.strip()
        if stripped.startswith("**") and stripped.endswith("**") and "http" not in stripped:
            pdf.set_font("Helvetica", "B", 10)
            pdf.multi_cell(body_w, 5, _ascii_safe(stripped.strip("*")))
            continue

        pdf.set_font("Helvetica", "", 10)
        pdf.write_html(
            f'<p style="font-size: 10pt; line-height: 1.45; margin: 0 0 4px 0;">'
            f"{_md_inline_to_html(line)}</p>"
        )

    pdf.output(str(out_path))


def main() -> None:
    stem = sys.argv[1] if len(sys.argv) > 1 else _DEFAULT_STEM
    md_path = _DOCS / f"{stem}.md"
    out_path = _DOCS / f"{stem}.pdf"
    if not md_path.is_file():
        raise SystemExit(f"Missing source: {md_path}")
    render(md_path, out_path)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
