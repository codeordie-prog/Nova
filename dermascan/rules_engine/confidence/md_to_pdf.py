"""
Convert decision-confidence research markdown to PDF with KaTeX-rendered math.

Preferred path: ``npm run build:pdf`` in this directory (md-to-pdf + Edge).
This module wraps the same flow for Python callers.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def convert(md_path: Path, pdf_path: Path, *, keep_html: bool = False) -> None:
    """
    Convert markdown with LaTeX math to PDF via ``npm run build:pdf``.

    Args:
        md_path: Source markdown path (must be ``decision_confidence.md`` in this folder).
        pdf_path: Destination PDF path.
        keep_html: Reserved; HTML export is not implemented in the Node builder.

    Raises:
        FileNotFoundError: If markdown is missing.
        RuntimeError: If the Node build fails.
    """
    if not md_path.is_file():
        raise FileNotFoundError(f"Markdown not found: {md_path}")

    if keep_html:
        raise NotImplementedError("Use md-to-pdf directly for HTML export.")

    confidence_dir = Path(__file__).resolve().parent
    if md_path.resolve() != (confidence_dir / "decision_confidence.md").resolve():
        raise ValueError("PDF builder expects decision_confidence.md in the confidence package.")

    result = subprocess.run(
        ["npm", "run", "build:pdf"],
        cwd=confidence_dir,
        capture_output=True,
        text=True,
        check=False,
        shell=sys.platform == "win32",
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"PDF build failed (exit {result.returncode}):\n{result.stdout}\n{result.stderr}"
        )

    built = confidence_dir / "decision_confidence.pdf"
    if pdf_path.resolve() != built.resolve():
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_path.write_bytes(built.read_bytes())


def main() -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description="Convert decision-confidence markdown to PDF.")
    parser.add_argument(
        "markdown",
        nargs="?",
        default=str(Path(__file__).with_name("decision_confidence.md")),
        help="Source markdown path",
    )
    parser.add_argument(
        "pdf",
        nargs="?",
        default=str(Path(__file__).with_name("decision_confidence.pdf")),
        help="Output PDF path",
    )
    parser.add_argument(
        "--keep-html",
        action="store_true",
        help="Also write an HTML file beside the PDF",
    )
    args = parser.parse_args()

    convert(Path(args.markdown), Path(args.pdf), keep_html=args.keep_html)
    print(f"Wrote {args.pdf}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
