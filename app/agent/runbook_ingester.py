"""
Runbook ingester — chunks, embeds, and upserts runbook content into pgvector.

Chunking strategy: split on markdown section headers first; if a section
exceeds ~2048 characters, split further on paragraph boundaries with overlap.
SHA-256 hash of each chunk content provides idempotency.

Embedding: uses a LocalEmbedder (sentence-transformers, 384-dim) by default.
Any object with an async embed(text) -> list[float] method is accepted.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_MAX_CHUNK_CHARS = 2048
_OVERLAP_CHARS = 256


def _split_into_sections(text: str) -> list[tuple[str, str]]:
    pattern = re.compile(r"^(#{1,3} .+)$", re.MULTILINE)
    positions = [(m.start(), m.group(0)) for m in pattern.finditer(text)]
    sections: list[tuple[str, str]] = []
    for i, (start, header) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        content = text[start + len(header):end].strip()
        if content:
            sections.append((header.lstrip("#").strip(), content))
    if not sections:
        sections = [("Full Document", text.strip())]
    return sections


def _chunk_text(text: str, max_chars: int = _MAX_CHUNK_CHARS, overlap: int = _OVERLAP_CHARS) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for para in paragraphs:
        if len(para) > max_chars:
            if current:
                chunks.append("\n\n".join(current))
                current = []
                current_len = 0
            start = 0
            while start < len(para):
                end = start + max_chars
                chunks.append(para[start:end])
                start = end - overlap
            continue
        if current_len + len(para) + 2 > max_chars and current:
            chunks.append("\n\n".join(current))
            carry = current[-1] if len(current[-1]) <= overlap else current[-1][-overlap:]
            current = [carry, para]
            current_len = len(carry) + len(para) + 2
        else:
            current.append(para)
            current_len += len(para) + 2
    if current:
        chunks.append("\n\n".join(current))
    return chunks


class RunbookIngester:
    """
    Reads .md files from a directory, chunks them, embeds locally,
    and upserts into the chunks table. Idempotent via SHA-256 hash.
    """

    def __init__(self, db: Any, embedder: Any) -> None:
        self._db = db
        self._embedder = embedder

    async def ingest_all(self, runbooks_dir: Path) -> dict[str, int]:
        results: dict[str, int] = {}
        for md_file in sorted(runbooks_dir.glob("*.md")):
            inserted = await self._ingest_file(md_file)
            results[md_file.name] = inserted
            logger.info("runbook_ingested", file=md_file.name, chunks_inserted=inserted)
        return results

    async def _ingest_file(self, path: Path) -> int:
        text = path.read_text(encoding="utf-8")
        sections = _split_into_sections(text)
        inserted = 0
        for section_header, section_content in sections:
            for chunk in _chunk_text(section_content):
                content_hash = hashlib.sha256(chunk.encode()).hexdigest()
                existing = await self._db.fetchval(
                    "SELECT id FROM chunks WHERE content_hash = $1 LIMIT 1",
                    content_hash,
                )
                if existing is not None:
                    continue
                try:
                    embedding = await self._embedder.embed(chunk)
                except Exception as exc:
                    logger.error("embedding_failed", error=str(exc))
                    continue
                embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"
                await self._db.execute(
                    """
                    INSERT INTO chunks (runbook, section, content, embedding, content_hash)
                    VALUES ($1, $2, $3, $4::vector, $5)
                    """,
                    path.name,
                    section_header,
                    chunk,
                    embedding_str,
                    content_hash,
                )
                inserted += 1
        return inserted
