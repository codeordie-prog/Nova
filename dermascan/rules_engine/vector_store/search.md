## Implementation of the vector search algorithm

This is the most critical part of the rules engine.
It makes it possible to bridge between cosmetic ingredient safety reports and internal clinical workflows between the client and the Model.

## Search Policy

### Overview
All safety reports for the ingredients are hosted in a unified medical literature corpus containing over 12,000 ingredients with INCI IDs, names, functions, toxicology, and safety assessment reports in PDF form.

### Pipeline Steps

**1. Ingredient Retrieval from Database**
- Query Postgres database for ingredient records using ingredient IDs or normalized names
- Fetch metadata including: `ingredient_id`, `cir_ingredient_name`, `function`, `report_url`, `attachment_url`, `storage_path`, `download_url`, `pdf_name`
- Filter for records with non-empty Firebase download URLs

**2. PDF Download**
- Download PDF documents from Firebase storage using the `download_url`
- Store temporarily for text extraction

**3. Text Extraction with OCR Fallback**
- Extract native text from PDF using PyPDF
- For pages with insufficient usable text (<300 characters), apply OCR fallback using Tesseract
- Combine native and OCR-extracted text per page

**4. Chunking**
- Apply recursive character text splitting to extracted text
- Create chunks of appropriate size (e.g., 1000 characters with 200-character overlap)
- Preserve metadata for each chunk: `ingredient_id`, `normalized_name`, `cir_ingredient_name`, `function`, `report_url`, `attachment_url`, `pdf_name`, `storage_path`, `chunk_index`, `page`, `chunk_text`

**5. Vectorization**
- Generate embeddings for each chunk using OpenAI's text-embedding-3-small model (1536 dimensions)
- Convert chunk text into high-dimensional vector representations

**6. Upsert to Vector Store**
- Upload vectors to Pinecone index with associated metadata
- Use ingredient-based namespaces for organization
- Handle null values in metadata (e.g., convert `page=None` to `-1`)

**7. Retrieval Policy**

- Query the vector store with user questions using cosine similarity
- Filter results by ingredient IDs to limit search scope
- Apply score threshold to filter low-relevance matches
- Over-fetch candidates (multiplier × top_k) and re-rank to prioritize substantive content over references

#### Mathematical retrieval formula

Retrieval keeps only chunks whose **cosine similarity** to the query is at or above the score threshold **τ**:

> **sim(Q, C) = (Q · C) / (‖Q‖ · ‖C‖) ≥ τ**

| Symbol | Meaning |
| --- | --- |
| **Q** | Query vector (embedded user question), 1536 dimensions |
| **C** | Chunk vector (embedded safety-report chunk) |
| **Q · C** | Dot product of the two vectors |
| **‖Q‖**, **‖C‖** | L2 norms (vector magnitudes) |
| **τ** | Minimum relevance score (e.g. 0.1) |

**8. Internet Supplementation**
- Retrieved information is supplemented with real-time web search for any additional context required

**9. Summarization**
- Final step synthesizes retrieved information into a coherent summary
- Summarized output is pipelined back to the client streams