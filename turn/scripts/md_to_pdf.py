"""Convert Turn relay integration markdown documentation to PDF."""

from __future__ import annotations

import re
import sys
from io import BytesIO
from pathlib import Path

from markdown import markdown
from xhtml2pdf import pisa


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>{title}</title>
  <style>
    @page {{
      size: A4;
      margin: 2cm 1.8cm 2.2cm 1.8cm;
      @frame footer {{
        -pdf-frame-content: footerContent;
        bottom: 0.6cm;
        margin-left: 1.8cm;
        margin-right: 1.8cm;
        height: 1cm;
      }}
    }}
    body {{
      font-family: Helvetica, Arial, sans-serif;
      font-size: 10pt;
      line-height: 1.45;
      color: #1a1a1a;
    }}
    h1 {{
      font-size: 20pt;
      border-bottom: 2px solid #4a148c;
      padding-bottom: 6px;
      margin-top: 0;
      color: #4a148c;
    }}
    h2 {{
      font-size: 14pt;
      margin-top: 18px;
      color: #311b92;
      border-bottom: 1px solid #ddd;
      padding-bottom: 4px;
    }}
    h3 {{
      font-size: 11pt;
      margin-top: 14px;
      color: #333;
    }}
    p, li {{
      margin: 6px 0;
    }}
    ul, ol {{
      margin: 6px 0 6px 18px;
      padding-left: 14px;
    }}
    code {{
      font-family: Courier, monospace;
      font-size: 8.5pt;
      background: #f4f4f4;
      padding: 1px 3px;
    }}
    pre {{
      background: #f6f8fa;
      border: 1px solid #e1e4e8;
      padding: 8px 10px;
      font-family: Courier, monospace;
      font-size: 8pt;
      line-height: 1.35;
      white-space: pre-wrap;
      word-wrap: break-word;
      margin: 10px 0;
    }}
    pre code {{
      background: transparent;
      padding: 0;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      margin: 10px 0;
      font-size: 9pt;
    }}
    th, td {{
      border: 1px solid #ccc;
      padding: 5px 7px;
      vertical-align: top;
      text-align: left;
    }}
    th {{
      background: #ede7f6;
      font-weight: bold;
    }}
    hr {{
      border: none;
      border-top: 1px solid #ddd;
      margin: 16px 0;
    }}
    blockquote {{
      border-left: 3px solid #4a148c;
      margin: 10px 0;
      padding: 4px 10px;
      color: #444;
      background: #fafafa;
    }}
    a {{
      color: #1565c0;
      text-decoration: none;
    }}
    #footerContent {{
      font-size: 8pt;
      color: #666;
      text-align: center;
    }}
  </style>
</head>
<body>
  {body}
  <div id="footerContent">
    {title}
  </div>
</body>
</html>
"""


def _ascii_safe_code_blocks(source: str) -> str:
    """Replace Unicode symbols in fenced code blocks for PDF font compatibility."""

    replacements = {
        "\u2192": "->",
        "\u2502": "|",
        "\u251c": "+",
        "\u2514": "+",
        "\u2500": "-",
        "\u2014": "-",
        "\u2013": "-",
    }

    def _replace_block(match: re.Match[str]) -> str:
        body = match.group(0)
        for src, dst in replacements.items():
            body = body.replace(src, dst)
        return body

    return re.sub(r"```.*?```", _replace_block, source, flags=re.DOTALL)


def _preprocess_mermaid(source: str) -> str:
    """Replace mermaid blocks with a plain-text diagram placeholder."""

    def _replace(match: re.Match[str]) -> str:
        body = match.group(1).strip()
        return f"\n\n**Diagram (see markdown source):**\n\n```\n{body}\n```\n\n"

    processed = re.sub(r"```mermaid\s*\n(.*?)```", _replace, source, flags=re.DOTALL)
    return _ascii_safe_code_blocks(processed)


def _extract_title(source: str) -> str:
    """Return the first H1 title from markdown, or a default."""
    for line in source.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return "Turn.io Relay Integration"


def _pdf_safe_text(source: str) -> str:
    """Replace Unicode punctuation that default PDF fonts may not render."""
    return (
        source.replace("\u2192", "->")
        .replace("\u2014", "-")
        .replace("\u2013", "-")
    )


def _markdown_to_html(source: str) -> str:
    """Render markdown to HTML body fragment."""
    processed = _pdf_safe_text(_preprocess_mermaid(source))
    return markdown(
        processed,
        extensions=["fenced_code", "tables", "sane_lists", "toc"],
    )


def convert(md_path: Path, pdf_path: Path) -> None:
    """
    Convert a markdown file to PDF.

    Args:
        md_path: Source markdown path.
        pdf_path: Destination PDF path.

    Raises:
        FileNotFoundError: If the markdown file does not exist.
        RuntimeError: If PDF generation fails.
    """
    if not md_path.is_file():
        raise FileNotFoundError(f"Markdown file not found: {md_path}")

    source = md_path.read_text(encoding="utf-8")
    title = _extract_title(source)
    body_html = _markdown_to_html(source)
    full_html = _HTML_TEMPLATE.format(title=title, body=body_html)

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    with pdf_path.open("wb") as pdf_file:
        result = pisa.CreatePDF(full_html, dest=pdf_file, encoding="utf-8")

    if result.err:
        raise RuntimeError(f"PDF generation failed with {result.err} error(s)")


def main() -> int:
    """CLI entrypoint."""
    repo_root = Path(__file__).resolve().parents[2]
    default_md = repo_root / "turn" / "relay_integration.md"
    default_pdf = repo_root / "turn" / "relay_integration.pdf"

    md_path = Path(sys.argv[1]) if len(sys.argv) > 1 else default_md
    pdf_path = Path(sys.argv[2]) if len(sys.argv) > 2 else default_pdf

    convert(md_path, pdf_path)
    print(f"Wrote {pdf_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
