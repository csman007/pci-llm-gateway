#!/usr/bin/env python3
"""
Ingest PCI DSS v4.0.1 PDF into pgvector.

Downloads the PDF (or reads a local copy), extracts text, chunks by requirement,
generates OpenAI embeddings, and inserts into the pci_dss_chunks table.

Usage:
    # From a URL (downloads to /tmp/pci-dss.pdf):
    python scripts/ingest_pci_dss.py \\
        --url https://www.middlebury.edu/sites/default/files/2025-01/PCI-DSS-v4_0_1.pdf

    # From a local file:
    python scripts/ingest_pci_dss.py --file /path/to/PCI-DSS-v4_0_1.pdf

    # Re-ingest (clears existing data first):
    python scripts/ingest_pci_dss.py --url ... --clear

Environment variables:
    POSTGRES_DSN    — PostgreSQL connection string (required)
    OPENAI_API_KEY  — OpenAI API key for embeddings (required)
    EMBEDDING_MODEL — Override embedding model (default: text-embedding-3-small)
"""

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

# Add services/rag to path so embedder and vector_store can be imported directly.
_repo_root = Path(__file__).parent.parent
sys.path.insert(0, str(_repo_root / "services" / "rag"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_repo_root / ".env")

# ── Chunking ──────────────────────────────────────────────────────────────────

_REQ_PATTERN = re.compile(
    r"(?:^|\n)(?:Requirement\s+)?(\d{1,2}\.\d{1,2}(?:\.\d{1,2})?)\s*[:\.]",
    re.MULTILINE,
)
_SECTION_HEADERS = re.compile(
    r"\n((?:Defined Approach|Customized Approach|Applicability Notes?|"
    r"Testing Procedures?|Guidance|Purpose|Good Practice|Examples?|"
    r"Further Information)[^\n]*)\n",
    re.IGNORECASE,
)

_CHUNK_SIZE = 1_500  # target characters per chunk
_OVERLAP = 200  # overlap between sliding-window sub-chunks
_BATCH_SIZE = 20  # embeddings per API call


def _sliding_chunks(text: str) -> list[str]:
    """Split *text* into overlapping fixed-size windows."""
    chunks, start = [], 0
    while start < len(text):
        end = min(start + _CHUNK_SIZE, len(text))
        # Try to break on a newline near the boundary.
        if end < len(text):
            nl = text.rfind("\n", start, end)
            if nl > start + _CHUNK_SIZE // 2:
                end = nl
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = end - _OVERLAP
    return [c for c in chunks if len(c) > 100]


def extract_text(pdf_path: str) -> str:
    """Extract all text from the PDF in reading order."""
    import fitz  # pymupdf — imported here to keep module import-safe for unit tests

    doc = fitz.open(pdf_path)
    pages = []
    for page in doc:
        pages.append(page.get_text("text"))
    return "\n".join(pages)


def chunk_document(text: str) -> list[dict]:
    """Chunk the PCI DSS document by requirement section.

    Tries to split on requirement number headers first; falls back to
    sliding windows if fewer than 10 requirements are detected.
    """
    matches = list(_REQ_PATTERN.finditer(text))
    chunks: list[dict] = []

    if len(matches) >= 10:
        for i, match in enumerate(matches):
            req_id = match.group(1)
            start = match.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            section_text = text[start:end].strip()

            if len(section_text) <= _CHUNK_SIZE * 2:
                chunks.append(
                    {
                        "requirement_id": req_id,
                        "section_title": f"Requirement {req_id}",
                        "chunk_text": section_text,
                    }
                )
            else:
                for sub in _sliding_chunks(section_text):
                    chunks.append(
                        {
                            "requirement_id": req_id,
                            "section_title": f"Requirement {req_id}",
                            "chunk_text": sub,
                        }
                    )
    else:
        print(f"  Warning: only {len(matches)} requirement markers found — using sliding window.")
        for sub in _sliding_chunks(text):
            req_match = _REQ_PATTERN.search(sub)
            chunks.append(
                {
                    "requirement_id": req_match.group(1) if req_match else None,
                    "section_title": None,
                    "chunk_text": sub,
                }
            )

    return chunks


# ── Helpers ───────────────────────────────────────────────────────────────────


def download_pdf(url: str, dest: str) -> None:
    """Download *url* to *dest* with a progress indicator."""
    import httpx  # imported here to keep module import-safe for unit tests

    print(f"  Downloading {url}")
    with httpx.stream("GET", url, follow_redirects=True, timeout=120) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        received = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes(chunk_size=65_536):
                f.write(chunk)
                received += len(chunk)
                if total:
                    pct = received * 100 // total
                    print(f"\r  {pct}% ({received // 1024} KB)", end="", flush=True)
    print(f"\n  Saved to {dest}")


def _batched(lst: list, n: int):
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest PCI DSS v4.0.1 into pgvector")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--url", help="URL of the PCI DSS PDF")
    src.add_argument("--file", help="Local path to the PCI DSS PDF")
    parser.add_argument("--clear", action="store_true", help="Delete existing chunks before ingesting")
    parser.add_argument("--dsn", help="Override POSTGRES_DSN env var")
    args = parser.parse_args()

    dsn = args.dsn or os.environ.get("POSTGRES_DSN")
    if not dsn:
        sys.exit("ERROR: POSTGRES_DSN is not set. Pass --dsn or set the env var.")
    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("ERROR: OPENAI_API_KEY is not set.")

    # ── 1. Obtain PDF ─────────────────────────────────────────────────────────
    if args.file:
        pdf_path = args.file
        print(f"Using local file: {pdf_path}")
    else:
        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        pdf_path = tmp.name
        tmp.close()
        download_pdf(args.url, pdf_path)

    # ── 2. Extract & chunk ────────────────────────────────────────────────────
    print("Extracting text from PDF...")
    text = extract_text(pdf_path)
    print(f"  Extracted {len(text):,} characters")

    print("Chunking document...")
    chunks = chunk_document(text)
    print(f"  Created {len(chunks)} chunks")
    req_ids = {c["requirement_id"] for c in chunks if c["requirement_id"]}
    print(f"  Covering {len(req_ids)} unique requirement IDs")

    # ── 3. Initialise store ───────────────────────────────────────────────────
    from embedder import EmbeddingClient  # noqa: E402
    from vector_store import VectorStore  # noqa: E402

    store = VectorStore(dsn=dsn)
    print("Initialising pgvector table...")
    store.initialise()

    if args.clear:
        print("Clearing existing chunks...")
        store.clear()

    # ── 4. Embed & insert in batches ──────────────────────────────────────────
    embedder = EmbeddingClient()
    inserted = 0

    for i, batch in enumerate(_batched(chunks, _BATCH_SIZE)):
        texts = [c["chunk_text"] for c in batch]
        print(f"\r  Embedding batch {i + 1}/{-(-len(chunks) // _BATCH_SIZE)} …", end="", flush=True)
        embeddings = embedder.embed_batch(texts)
        for chunk, emb in zip(batch, embeddings):
            chunk["embedding"] = emb
        inserted += store.upsert_chunks(batch)

    print(f"\nDone — inserted {inserted} chunks. Total in store: {store.count()}")


if __name__ == "__main__":
    main()
