"""
Author:  Dermascan Team
Date: 2026-05-18
Licence: MIT

Description: This module contains the implementation of the logic of adding data to qutis index.


"""

import os
import shutil
from dotenv import load_dotenv
from collections import defaultdict
from typing import Any

from langchain_community.document_loaders.pdf import PyPDFLoader
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from dermascan.rules_engine.build.cir_db import (
    CIRPDFRow,
    _close,
    _connect_db,
    _fetch_cir_pdf_rows,
)
from dermascan.shared.logger import get_logger
from dermascan.shared.env_paths import resolve_env_path

# Load environment variables
load_dotenv()

BASE_PATH: str = "C:/Users/LENOVO/Documents/cir/cir-crawler/data/pdfs/00e719cf-8d74-ec11-8943-0022482f06a6.pdf"

logger = get_logger(__name__)


def _load_pdf(path: str) -> list[Document]:
    """
    Load a PDF file and return its pages.
    
    Args:
        path (str): The path to the PDF file.
        
    Returns:
        list[Document]: A list of page documents from the PDF file.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"PDF not found at path: {path}")

    loader = PyPDFLoader(path)
    pages = loader.load()

    extracted_text = "\n".join((page.page_content or "") for page in pages)
    cleaned_text = extracted_text.replace("\x0c", "").strip()
    alnum_chars = sum(1 for char in cleaned_text if char.isalnum())
    usable_text = len(cleaned_text) >= 300 and alnum_chars >= max(1, len(cleaned_text) // 10)

    if usable_text:
        for page in pages:
            page.metadata["extraction_method"] = "text"
        return pages

    try:
        import fitz  # pymupdf
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError(
            "Scanned PDF detected but OCR dependencies are missing. "
            "Install: pymupdf, pytesseract, pillow, and Tesseract OCR binary."
        ) from exc

    ocr_pages: list[Document] = []
    pdf = fitz.open(path)
    try:
        tesseract_path = resolve_env_path(os.getenv("TESSERACT_PATH", "")) or shutil.which("tesseract")
        if tesseract_path and os.path.exists(tesseract_path):
            pytesseract.pytesseract.tesseract_cmd = tesseract_path
        for page_number, page in enumerate(pdf, start=1):
            pixmap = page.get_pixmap(dpi=200)
            image = Image.frombytes("RGB", [pixmap.width, pixmap.height], pixmap.samples)
            try:
                text = pytesseract.image_to_string(image)
            except pytesseract.TesseractNotFoundError as exc:
                raise RuntimeError(
                    "Tesseract OCR executable not found. Install Tesseract and add it to PATH, "
                    "or set pytesseract.pytesseract.tesseract_cmd to the full exe path."
                ) from exc
            ocr_pages.append(
                Document(
                    page_content=text,
                    metadata={
                        "source": path,
                        "page": page_number,
                        "extraction_method": "ocr",
                    },
                )
            )
    finally:
        pdf.close()

    return ocr_pages


def _chunk_pages(
    pages: list[Document],
    *,
    chunk_size: int = 1200,
    chunk_overlap: int = 200,
) -> list[dict[str, object]]:
    """
    Split page documents into chunk templates while preserving page metadata.

    Args:
        pages (list[Document]): Source PDF pages.
        chunk_size (int): Maximum chunk character length.
        chunk_overlap (int): Overlap between adjacent chunks.

    Returns:
        list[dict[str, object]]: Chunk templates with text, page number, and chunk index.
    """
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", ". ", " ", ""],
    )

    templates: list[dict[str, object]] = []
    chunk_index = 0
    for page in pages:
        page_number = int(page.metadata.get("page", 0)) + 1
        splits = splitter.split_text(page.page_content or "")
        for text in splits:
            cleaned = text.strip()
            if not cleaned:
                continue
            templates.append(
                {
                    "text": cleaned,
                    "page": page_number,
                    "chunk_index": chunk_index,
                }
            )
            chunk_index += 1

    return templates


def _build_ingredient_chunks(
    row: CIRPDFRow,
    chunk_templates: list[dict[str, object]],
) -> list[dict[str, Any]]:
    """
    Build ingredient-specific CIR chunks from cached document chunk templates.

    Args:
        row (CIRPDFRow): CIR database row containing ingredient metadata.
        chunk_templates (list[dict[str, object]]): Cached chunk templates for one PDF.

    Returns:
        list[dict[str, Any]]: Chunk payloads with per-ingredient metadata.
    """
    return [
        {
            "text": str(template["text"]),
            "ingredient_id": row.cir_ingredient_id,
            "cir_ingredient_name": row.cir_ingredient_name,
            "function": row.function,
            "report_url": row.report_url,
            "attachment_url": row.attachment_view_url,
            "chunk_index": int(template["chunk_index"]),
            "page": int(template["page"]),
        }
        for template in chunk_templates
    ]


def _append_failed_row(
    *,
    failed_rows_path: str,
    row: CIRPDFRow,
    stage: str,
    error: Exception,
) -> None:
    """
    Persist a failed ingestion row to disk for retries and troubleshooting.

    Args:
        failed_rows_path (str): Target file path for failed row records.
        row (CIRPDFRow): Row that failed during ingestion.
        stage (str): Pipeline stage where failure happened.
        error (Exception): Raised exception.
    """
    parent_dir = os.path.dirname(failed_rows_path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)
    with open(failed_rows_path, "a", encoding="utf-8") as file_obj:
        file_obj.write(
            "|".join(
                [
                    stage,
                    row.cir_ingredient_id,
                    row.cir_ingredient_name,
                    row.pdf_path,
                    row.report_url,
                    row.attachment_view_url,
                    str(error).replace("\n", " "),
                ]
            )
            + "\n"
        )


def ingest_cir_documents(
    *,
    db_path: str | None = None,
    row_limit: int | None = None,
    upsert_batch_size: int = 100,
    embedding_batch_size: int = 64,
    chunk_size: int = 1200,
    chunk_overlap: int = 200,
    failed_rows_path: str = "failed.txt",
    dry_run: bool = False,
) -> dict[str, int]:
    """
    Ingest CIR PDFs into Pinecone using PDF-path-based chunk caching and duplicate-row skipping.

    This function reads metadata from SQLite, loads/chunks each unique PDF once, reuses cached
    chunks for all ingredients linked to that PDF, and skips duplicate ingredient/pdf rows.

    Args:
        db_path (str | None): Optional override path for SQLite DB.
        row_limit (int | None): Optional row cap for testing.
        upsert_batch_size (int): Pinecone upsert batch size.
        embedding_batch_size (int): Embedding batch size.
        chunk_size (int): Chunk size for text splitting.
        chunk_overlap (int): Chunk overlap for text splitting.
        failed_rows_path (str): File path where failed rows are appended.
        dry_run (bool): If True, build chunks but do not upsert vectors.

    Returns:
        dict[str, int]: Summary counters for ingestion diagnostics.
    """
    conn = _connect_db(db_path) if db_path else _connect_db()
    try:
        rows = _fetch_cir_pdf_rows(conn, limit=row_limit)
    finally:
        _close(conn)

    logger.info("Loaded %s ingestion rows from CIR database", len(rows))

    vector_store = None
    cir_chunk_cls = None
    if not dry_run:
        from dermascan.rules_engine.build.build_vectorstore import CIRChunk, VectorStore

        cir_chunk_cls = CIRChunk
        vector_store = VectorStore()

    cached_templates_by_pdf: dict[str, list[dict[str, object]]] = {}
    seen_pairs: set[tuple[str, str]] = set()
    chunks_by_pdf: dict[str, list[dict[str, Any]]] = defaultdict(list)

    summary = {
        "rows": len(rows),
        "unique_pdfs": 0,
        "skipped_duplicates": 0,
        "missing_pdfs": 0,
        "chunks_prepared": 0,
        "vectors_upserted": 0,
        "failed_rows": 0,
    }

    for row_number, row in enumerate(rows, start=1):
        logger.info(
            "Processing row %s/%s | ingredient_id=%s | pdf_path=%s",
            row_number,
            len(rows),
            row.cir_ingredient_id,
            row.pdf_path,
        )

        dedup_key = (row.cir_ingredient_id, row.pdf_path)
        if dedup_key in seen_pairs:
            summary["skipped_duplicates"] += 1
            logger.info(
                "Skipped duplicate row for ingredient_id=%s and pdf_path=%s",
                row.cir_ingredient_id,
                row.pdf_path,
            )
            continue
        seen_pairs.add(dedup_key)

        if not os.path.exists(row.pdf_path):
            summary["missing_pdfs"] += 1
            logger.warning("Skipping missing PDF path: %s", row.pdf_path)
            continue

        try:
            if row.pdf_path not in cached_templates_by_pdf:
                logger.info("Loading and chunking new PDF: %s", row.pdf_path)
                pages = _load_pdf(row.pdf_path)
                templates = _chunk_pages(
                    pages,
                    chunk_size=chunk_size,
                    chunk_overlap=chunk_overlap,
                )
                cached_templates_by_pdf[row.pdf_path] = templates
                summary["unique_pdfs"] += 1
                logger.info(
                    "Chunked PDF successfully | pdf_path=%s | chunks=%s",
                    row.pdf_path,
                    len(templates),
                )
            else:
                logger.info("Reusing cached chunks for PDF: %s", row.pdf_path)

            ingredient_chunks = _build_ingredient_chunks(
                row,
                cached_templates_by_pdf[row.pdf_path],
            )
            chunks_by_pdf[row.pdf_path].extend(ingredient_chunks)
            summary["chunks_prepared"] += len(ingredient_chunks)
            logger.info(
                "Prepared %s chunks for ingredient_id=%s",
                len(ingredient_chunks),
                row.cir_ingredient_id,
            )
        except Exception as exc:
            summary["failed_rows"] += 1
            logger.exception(
                "Failed processing row %s/%s | ingredient_id=%s | pdf_path=%s",
                row_number,
                len(rows),
                row.cir_ingredient_id,
                row.pdf_path,
            )
            _append_failed_row(
                failed_rows_path=failed_rows_path,
                row=row,
                stage="load_or_chunk",
                error=exc,
            )
            continue

    if vector_store is not None:
        for pdf_path, chunks in chunks_by_pdf.items():
            if not chunks:
                continue
            try:
                cir_chunks = [cir_chunk_cls(**chunk) for chunk in chunks]
                vector_ids = vector_store._upsert_texts(
                    cir_chunks,
                    upsert_batch_size=upsert_batch_size,
                    embedding_batch_size=embedding_batch_size,
                )
                summary["vectors_upserted"] += len(vector_ids)
                logger.info("Upserted %s vectors for PDF: %s", len(vector_ids), pdf_path)
            except Exception as exc:
                unique_failed_rows: dict[tuple[str, str], CIRPDFRow] = {}
                for chunk in chunks:
                    failed_key = (str(chunk["ingredient_id"]), pdf_path)
                    if failed_key not in unique_failed_rows:
                        unique_failed_rows[failed_key] = CIRPDFRow(
                            cir_ingredient_id=str(chunk["ingredient_id"]),
                            cir_ingredient_name=str(chunk["cir_ingredient_name"]),
                            function=str(chunk["function"]),
                            report_url=str(chunk["report_url"]),
                            attachment_view_url=str(chunk["attachment_url"]),
                            pdf_path=pdf_path,
                        )

                summary["failed_rows"] += len(unique_failed_rows)
                logger.exception("Failed upsert for PDF: %s", pdf_path)
                for failed_row in unique_failed_rows.values():
                    _append_failed_row(
                        failed_rows_path=failed_rows_path,
                        row=failed_row,
                        stage="upsert",
                        error=exc,
                    )

    logger.info("Ingestion completed with summary: %s", summary)

    return summary


if __name__ == "__main__":
    report = ingest_cir_documents(row_limit=5, dry_run=True)
    print(report)
    