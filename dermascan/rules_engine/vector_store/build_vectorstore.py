"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-05-18
License: MIT

Description: Pinecone vector store utilities for large-scale CIR ingestion.


## TODO:
1. Temp directory for firebase downloads
2. Cache postgress and indexed pdf reports
3. Resolve hardcoded tesseract.exe path

"""


from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from logging import Logger
from typing import Any

from dotenv import load_dotenv
from langchain_openai.embeddings import OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pinecone import Pinecone
from pypdf import PdfReader

from dermascan.rules_engine.firebase_init.firebase_utils import _download_pdf_from_url
from dermascan.rules_engine.vector_store.build_index import IndexBuilder, IndexConfig
from dermascan.shared.logger import get_logger

# Load environment variables
load_dotenv()

# Initialize logger
logger: Logger = get_logger(__name__)

PINECONE_API_KEY_QUTIS = str(os.environ.get("PINECONE_API_KEY_QUTIS", ""))
OPENAI_API_KEY_QUTIS = str(os.environ.get("OPENAI_API_KEY_QUTIS", ""))
INDEX_NAME = "qutis-index"


def _has_index(pc: Pinecone, index_name: str) -> bool:
    """
    Check if an index exists in Pinecone.
    
    Args:
        pc (Pinecone): Pinecone client
        index_name (str): Name of the index to check
        
    Returns:
        bool: True if index exists, False otherwise
    """
    return pc.indexes.exists(name=index_name)


@dataclass(slots=True)
class CIRChunk:
    """
    Canonical chunk object stored in Pinecone.
    This will be used to build the metadata and chunk id.

    Attributes:
        text: Chunk content.
        ingredient_id: Stable ingredient identifier.
        cir_ingredient_name: Human-readable ingredient name.
        normalized_name: Normalized ingredient name for cross-reference.
        function: Ingredient function/category from CIR metadata.
        report_url: CIR report URL.
        attachment_url: CIR attachment/view URL.
        pdf_name: Source PDF filename.
        storage_path: Storage URI/path for source PDF.
        chunk_index: 0-based chunk index within source PDF.
        page: Source page number (1-based), if known.
        chunk_id: Optional deterministic override ID.
    """

    text: str
    ingredient_id: str
    cir_ingredient_name: str
    normalized_name: str
    function: str
    report_url: str
    attachment_url: str
    pdf_name: str
    storage_path: str
    chunk_index: int
    page: int | None = None
    chunk_id: str | None = None


class VectorStore:
    def __init__(
        self,
        index_name: str = INDEX_NAME,
        api_key: str = PINECONE_API_KEY_QUTIS,
        openai_api_key: str = OPENAI_API_KEY_QUTIS,
        embedding_model: str = "text-embedding-3-small",
        embedding_dimension: int = 1536,
        default_namespace: str = "cir",
    ):

        """
        Vector store class for initializing a pinecone vector store..
        
        Args:
            index_name (str): Name of the index
            api_key (str): Pinecone API key
            openai_api_key (str): OpenAI API key
            embedding_model (str): Text embedding model
            embedding_dimension (int): embedding dimension default is 3072
            default_namespace (str): default namespace
        """
        if not api_key:
            raise ValueError("PINECONE_API_KEY_QUTIS is required")
        if not openai_api_key:
            raise ValueError("OPENAI_API_KEY_QUTIS is required")

        self.index_name = index_name
        self.api_key = api_key
        self.default_namespace = default_namespace

        self.pc = Pinecone(api_key=api_key)
        if not _has_index(self.pc, self.index_name):
            logger.warning("Index %s not found; creating...", self.index_name)

        # Build index if it doesn't exist
        index_builder = IndexBuilder(
            IndexConfig(
                index_name=self.index_name,
                api_key=self.api_key,
                dimension=embedding_dimension,
                metric="cosine",
            )
        )

        # Create if index doesn't exist
        index_builder.ensure_index()

        logger.info("Index %s is ready.", self.index_name)
        self.index = self.pc.Index(self.index_name)

        # Specify embeddings
        self.embeddings = OpenAIEmbeddings(
            model=embedding_model,
            dimensions=embedding_dimension,
            api_key=openai_api_key,
        )


    @staticmethod
    def _normalize_name(raw_name: str) -> str:
        """
        Normalize a name for cross-ingredient matching.
        A static method means it can be called without creating an instance of the class.
        
        Args:
            raw_name (str): Raw ingredient name to normalize
            
        Returns:
            str: Normalized name
        """
        normalized = "".join(ch.lower() for ch in raw_name if ch.isalnum())
        return normalized

    @staticmethod
    def _has_usable_text(text: str) -> bool:
        """
        Checks if text has usable characters.
        Important during PDF extraction to filter out empty or corrupted pages.
        
        Args:
            text (str): Text to check
            
        Returns:
            bool: True if text has usable characters, False otherwise
        """
        cleaned = text.replace("\x0c", "").strip()
        if len(cleaned) < 300:
            return False
        alnum_chars = sum(1 for char in cleaned if char.isalnum())
        return alnum_chars >= max(1, len(cleaned) // 10)

    def _extract_pdf_text(self, pdf_path: str) -> str:
        """
        Extract PDF text with OCR fallback for scanned files.
        Args:
            pdf_path (str): Path to the PDF file
        Returns:
            str: Extracted text
        """
        reader = PdfReader(pdf_path)
        page_texts = [(page.extract_text() or "").strip() for page in reader.pages]
        extracted_text = "\n\n".join(part for part in page_texts if part)
        if self._has_usable_text(extracted_text):
            logger.info("Using native text extraction for %s", pdf_path)
            return extracted_text

        logger.warning("Low native text quality for %s. Attempting OCR fallback.", pdf_path)
        try:
            import fitz  # pymupdf
            import pytesseract
            from PIL import Image
        except ImportError as exc:
            logger.warning(
                "OCR dependencies missing. Falling back to native extracted text for %s", pdf_path
            )
            if extracted_text:
                return extracted_text
            raise RuntimeError(
                "Scanned PDF detected but OCR dependencies are missing. Install pymupdf, pytesseract, pillow, and Tesseract OCR binary."
            ) from exc

        try:
            tesseract_path = os.getenv("TESSERACT_PATH", "")
            if not os.path.exists(tesseract_path):
                raise FileNotFoundError("Tesseract.exe path not set in the environment.")
        except FileNotFoundError:
            logger.error("Tesseract.exe path not configured in the environment")
            raise

        pytesseract.pytesseract.tesseract_cmd = tesseract_path

        ocr_texts: list[str] = []
        pdf = fitz.open(pdf_path)
        try:
            for page in pdf:
                pixmap = page.get_pixmap(dpi=200)
                image = Image.frombytes("RGB", [pixmap.width, pixmap.height], pixmap.samples)
                ocr_texts.append(pytesseract.image_to_string(image).strip())
        finally:
            pdf.close()

        ocr_text = "\n\n".join(text for text in ocr_texts if text)
        if not ocr_text.strip():
            logger.warning("OCR fallback produced no text for %s; using native text output", pdf_path)
            return extracted_text
        logger.info("Using OCR text extraction for %s", pdf_path)
        return ocr_text

    def build_chunks_from_pdf(
        self,
        *,
        pdf_path: str,
        ingredient_id: str,
        cir_ingredient_name: str,
        function: str,
        report_url: str,
        attachment_url: str,
        storage_path: str,
        pdf_name: str | None = None,
        max_chars: int = 1400,
    ) -> list[CIRChunk]:
        """
        Build chunks from one PDF file using recursive text splitting.

        Args:
            pdf_path (str): Path to the PDF file
            ingredient_id (str): Ingredient ID
            cir_ingredient_name (str): CIR ingredient name
            function (str): Function
            report_url (str): Report URL
            attachment_url (str): Attachment URL
            storage_path (str): Storage path
            pdf_name (str | None): PDF name
            max_chars (int): Maximum characters per chunk
            
        Returns:
            list[CIRChunk]: List of CIRChunk objects
        """
        # Normalize name and resolve pdf name
        normalized_name = self._normalize_name(cir_ingredient_name)
        resolved_pdf_name = pdf_name or os.path.basename(pdf_path)

        full_text = self._extract_pdf_text(pdf_path)
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=max_chars,
            chunk_overlap=min(200, max(0, max_chars // 6)),
            separators=["\n\n", "\n", ". ", " ", ""],
        )
        split_texts = splitter.split_text(full_text)

        chunks: list[CIRChunk] = []
        for idx, chunk_text in enumerate(split_texts):
            cleaned = str(chunk_text).strip()
            if not cleaned:
                continue
            chunks.append(
                CIRChunk(
                    text=cleaned,
                    ingredient_id=ingredient_id,
                    cir_ingredient_name=cir_ingredient_name,
                    normalized_name=normalized_name,
                    function=function,
                    report_url=report_url,
                    attachment_url=attachment_url,
                    pdf_name=resolved_pdf_name,
                    storage_path=storage_path,
                    chunk_index=idx,
                )
            )

        logger.info(
            "Prepared %s chunks for ingredient=%s (%s)",
            len(chunks),
            ingredient_id,
            cir_ingredient_name,
        )
        return chunks

    def index_pdf_from_firebase(
        self,
        *,
        pdf_url: str,
        ingredient_id: str,
        cir_ingredient_name: str,
        function: str,
        report_url: str,
        attachment_url: str,
        storage_path: str,
    ) -> dict[str, Any]:
        """
        Lazy-load one PDF from Firebase, chunk/classify it, then upsert to Pinecone.

        The Algorithm works as follows:
            1. Download the PDF from Firebase to a temporary directory
            2. Build chunks from the PDF
            3. Upsert the chunks to Pinecone
            4. Temporary directory is automatically cleaned up

        Args:
            pdf_url (str): URL of the PDF file
            ingredient_id (str): Ingredient ID
            cir_ingredient_name (str): CIR ingredient name
            function (str): Function
            report_url (str): Report URL
            attachment_url (str): Attachment URL
            storage_path (str): Storage path

        Returns:
            dict[str, Any]: Result dictionary
        """
        logger.info(
            "Starting lazy indexing from firebase | ingredient_id=%s | ingredient=%s",
            ingredient_id,
            cir_ingredient_name,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            local_pdf_path = _download_pdf_from_url(url=pdf_url, output_dir=temp_dir)
            logger.info("Downloaded source PDF to %s", local_pdf_path)

            chunks = self.build_chunks_from_pdf(
                pdf_path=local_pdf_path,
                ingredient_id=ingredient_id,
                cir_ingredient_name=cir_ingredient_name,
                function=function,
                report_url=report_url,
                attachment_url=attachment_url,
                storage_path=storage_path,
                pdf_name=os.path.basename(local_pdf_path),
            )
            inserted_ids = self._upsert_texts(chunks)
            logger.info("Completed lazy indexing. Inserted %s vectors", len(inserted_ids))
            return {
                "local_pdf_path": local_pdf_path,
                "chunk_count": len(chunks),
                "inserted_vector_count": len(inserted_ids),
                "inserted_ids": inserted_ids,
            }

    def _build_chunk_id(self, chunk: CIRChunk) -> str:
        """
        Build a stable chunk ID from the chunk's attributes.
        
        Args:
            chunk (CIRChunk): CIR chunk
            
        Returns:
            str: Stable chunk ID
        """
        if chunk.chunk_id:
            return chunk.chunk_id

        # Create a stable payload from the chunk's attributes
        stable_payload = "|".join(
            [
                chunk.ingredient_id,
                chunk.cir_ingredient_name,
                chunk.function,
                chunk.report_url,
                chunk.attachment_url,
                chunk.normalized_name,
                chunk.pdf_name,
                chunk.storage_path,
                str(chunk.chunk_index),
                str(chunk.page if chunk.page is not None else -1),
                chunk.text,
            ]
        )
        return hashlib.sha1(stable_payload.encode("utf-8")).hexdigest()

    def _resolve_namespace(self) -> str:
        """
        Resolve the namespace.
        
        Returns:
            str: Namespace
        """
        safe = self.default_namespace.lower().replace("_", "-").replace(" ", "-")
        return safe

    def _build_metadata(self, chunk: CIRChunk) -> dict[str, Any]:
        """
        Build metadata for a chunk.
        
        Args:
            chunk (CIRChunk): CIR chunk
            
        Returns:
            dict[str, Any]: Metadata
        """
        page_value = int(chunk.page) if chunk.page is not None else -1

        return {
            "ingredient_id": chunk.ingredient_id,
            "normalized_name": chunk.normalized_name,
            "cir_ingredient_name": chunk.cir_ingredient_name,
            "function": chunk.function,
            "report_url": chunk.report_url,
            "attachment_url": chunk.attachment_url,
            "pdf_name": chunk.pdf_name,
            "storage_path": chunk.storage_path,
            "chunk_index": chunk.chunk_index,
            "page": page_value,
            "chunk_text": chunk.text,
        }

    def _embed_texts(self, texts: list[str], batch_size: int = 64) -> list[list[float]]:
        """
        Embed texts in batches for throughput and API stability.
        
        Args:
            texts (list[str]): List of texts to embed
            batch_size (int): Batch size for embedding
            
        Returns:
            list[list[float]]: List of embeddings
        """
        vectors: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            logger.info("Embedding batch %s-%s", i, i + len(batch) - 1)
            vectors.extend(self.embeddings.embed_documents(batch))
        return vectors

    def _embedd_texts(self, texts: list[str], batch_size: int = 64) -> list[list[float]]:
        """
        Backward-compatible alias for historical typo in method name.
        """
        return self._embed_texts(texts=texts, batch_size=batch_size)


    def _upsert_texts(
        self,
        chunks: list[CIRChunk],
        *,
        upsert_batch_size: int = 100,
        embedding_batch_size: int = 64,
    ) -> list[str]:
        """
        Upsert chunked CIR texts using batched embedding and batched writes.
        
        Args:
            chunks (list[CIRChunk]): List of CIR chunks
            upsert_batch_size (int): Batch size for upserting
            embedding_batch_size (int): Batch size for embedding
            
        Returns:
            list[str]: List of inserted vector IDs
        """
        if not chunks:
            return []

        unique_texts = list(dict.fromkeys(chunk.text for chunk in chunks))
        unique_vectors = self._embed_texts(unique_texts, batch_size=embedding_batch_size)
        text_to_vector = {
            text: vector for text, vector in zip(unique_texts, unique_vectors, strict=True)
        }

        grouped: dict[str, list[dict[str, Any]]] = {}
        inserted_ids: list[str] = []
        namespace = self._resolve_namespace()

        for chunk in chunks:
            vector = text_to_vector[chunk.text]
            vector_id = self._build_chunk_id(chunk)
            inserted_ids.append(vector_id)

            payload = {
                "id": vector_id,
                "values": vector,
                "metadata": self._build_metadata(chunk),
            }
            grouped.setdefault(namespace, []).append(payload)

        for namespace, rows in grouped.items():
            for i in range(0, len(rows), upsert_batch_size):
                batch = rows[i : i + upsert_batch_size]
                logger.info(
                    "Upserting %s vectors to namespace=%s (batch %s-%s)",
                    len(batch),
                    namespace,
                    i,
                    i + len(batch) - 1,
                )
                self.index.upsert(vectors=batch, namespace=namespace)

        return inserted_ids

    def _query(
        self,
        query_text: str,
        *,
        top_k: int = 5,
        ingredient_id: str | None = None,
        ingredient_ids: list[str] | None = None,
    ) -> list[Any]:
        """
        Query the index.
        NB its important to provide the ingredient_id/ids inorder to filter the namespace for target vectors.

        Args:
            query_text (str): query
            top_k (int): Number of results to return
            ingredient_id (str | None): Optional metadata filter for ingredient
            ingredient_ids (list | None): Optional metadata filter for ingredients
        
        Returns:
            list[Any]: Query matches
        """
        namespace = self._resolve_namespace()
        metadata_filter: dict[str, Any] | None = None
        if ingredient_ids:
            metadata_filter = {"ingredient_id": {"$in": ingredient_ids}}
        elif ingredient_id:
            metadata_filter = {"ingredient_id": {"$eq": ingredient_id}}

        vector = self._embed_texts([query_text], batch_size=1)[0]
        response = self.index.query(
            vector=vector,
            top_k=top_k,
            include_metadata=True,
            namespace=namespace,
            filter=metadata_filter,
        )
        return response.matches

    def _query_with_score_threshold(
        self, 
        query_text: str, 
        top_k: int = 5,
        score_threshold: float = 0.4,
        ingredient_id: str | None = None,
        ingredient_ids: list[str] | None = None,
        candidate_multiplier: int = 3,
        max_candidate_k: int = 100,
    ) -> list[Any]:
        """
        Query the index with a score threshold.
        
        Args:
            query_text (str): Text to query
            top_k (int): Number of results to return
            score_threshold (float): Minimum score threshold
            ingredient_id (str | None): Optional metadata filter for ingredient
            ingredient_ids (list | None): Optional metadata filter for ingredients
            candidate_multiplier (int): Multiplier for candidate retrieval
            max_candidate_k (int): Maximum candidate retrieval limit
            
        Returns:
            list[Any]: Filtered query matches
        """
        threshold = max(0.0, min(float(score_threshold), 1.0))
        effective_top_k = max(1, top_k)
        if threshold > 0:
            effective_top_k = min(max_candidate_k, max(top_k, top_k * max(1, candidate_multiplier)))

        matches = self._query(
            query_text=query_text,
            top_k=effective_top_k,
            ingredient_id=ingredient_id,
            ingredient_ids=ingredient_ids,
        )
        filtered = [match for match in matches if float(getattr(match, "score", 0.0)) >= threshold]
        return filtered[:top_k]

    def query_safety_report(
        self,
        *,
        query_text: str,
        ingredient_ids: list[str],
        top_k: int = 12,
        score_threshold: float = 0.4,
    ) -> list[Any]:
        """
        Query safety-oriented chunks across multiple ingredients.
        It includes a score threshold that will later be used to calculate the multi-layer confidence score.

        Args:
            query_text (str): Text to query
            ingredient_ids (list[str]): List of ingredient IDs to query
            top_k (int): Number of results to return
            score_threshold (float): Minimum score threshold
            
        Returns:
            list[Any]: Filtered query matches
        """
        return self._query_with_score_threshold(
            query_text=query_text,
            top_k=top_k,
            score_threshold=score_threshold,
            ingredient_ids=ingredient_ids,
        )

    def query_irritation_and_dermatology(
        self,
        *,
        query_text: str,
        ingredient_ids: list[str],
        top_k: int = 12,
        score_threshold: float = 0.4,
    ) -> list[Any]:
        """
        Query irritation/dermatology evidence across multiple ingredients.
        It includes a score threshold that will later be used to calculate the multi-layer confidence score.
        
        Args:
            query_text (str): Text to query
            ingredient_ids (list[str]): List of ingredient IDs to query
            top_k (int): Number of results to return
            score_threshold (float): Minimum score threshold
            
        Returns:
            list[Any]: Filtered query matches
        """
        return self._query_with_score_threshold(
            query_text=query_text,
            top_k=top_k,
            score_threshold=score_threshold,
            ingredient_ids=ingredient_ids,
        )

    def query_cross_ingredient_interactions(
        self,
        *,
        query_text: str,
        ingredient_ids: list[str],
        top_k: int = 12,
        score_threshold: float = 0.4,
    ) -> list[Any]:
        """
        Query interaction-oriented evidence across multiple ingredients.
        It includes a score threshold that will later be used to calculate the multi-layer confidence score.
        
        Args:
            query_text (str): Text to query
            ingredient_ids (list[str]): List of ingredient IDs to query
            top_k (int): Number of results to return
            score_threshold (float): Minimum score threshold
            
        Returns:
            list[Any]: Filtered query matches
        """
        return self._query_with_score_threshold(
            query_text=query_text,
            top_k=top_k,
            score_threshold=score_threshold,
            ingredient_ids=ingredient_ids,
        )


    
if __name__ == "__main__":
    tst = os.environ.get("TESSERACT_PATH")
    print(tst)