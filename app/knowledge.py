"""Knowledge base loading and retrieval.

Drop markdown files into knowledge/ (or upload them from /admin) and the agent
reads them. Small knowledge bases are injected wholesale; larger ones fall back
to BM25 retrieval so the prompt - and therefore latency - stays small.

BM25 is implemented here in ~40 lines on purpose: no embedding API call means
no extra cost, no extra network hop and no extra latency in the turn.
"""
import math
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import config

_WORD = re.compile(r"[a-z0-9']+")
_STOP = {
    "the", "a", "an", "and", "or", "is", "are", "to", "of", "in", "for", "on",
    "we", "you", "i", "it", "that", "this", "with", "as", "at", "by", "be",
    "do", "does", "can", "how", "what", "your", "our", "my",
}


def _tokens(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 1]


@dataclass
class Chunk:
    title: str
    text: str
    tokens: list[str]


class KnowledgeBase:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.chunks: list[Chunk] = []
        self.full_text: str = ""
        self.loaded_at: float = 0.0
        self.files: list[str] = []
        self._df: dict[str, int] = {}
        self._avg_len: float = 1.0

    # -- loading --
    def load(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        paths = sorted(p for p in directory.glob("*.md") if p.is_file())

        chunks: list[Chunk] = []
        parts: list[str] = []
        for path in paths:
            try:
                raw = path.read_text(encoding="utf-8").strip()
            except Exception:
                continue
            if not raw:
                continue
            parts.append(raw)
            for title, body in _split_sections(raw, path.stem):
                chunks.append(Chunk(title, body, _tokens(f"{title} {body}")))

        df: dict[str, int] = {}
        for chunk in chunks:
            for term in set(chunk.tokens):
                df[term] = df.get(term, 0) + 1

        with self._lock:
            self.chunks = chunks
            self.full_text = "\n\n".join(parts)
            self.files = [p.name for p in paths]
            self._df = df
            self._avg_len = (
                sum(len(c.tokens) for c in chunks) / len(chunks) if chunks else 1.0
            )
            self.loaded_at = time.time()

    # -- retrieval --
    def context_for(self, query: str) -> str:
        with self._lock:
            full = self.full_text
            chunks = list(self.chunks)
            df = dict(self._df)
            avg_len = self._avg_len

        if not chunks:
            return ""
        if len(full) <= config.KB_INLINE_LIMIT:
            return full

        scored = _bm25(query, chunks, df, avg_len)
        top = [c for _, c in scored[: config.KB_TOP_K]]
        return "\n\n".join(f"## {c.title}\n{c.text}" for c in top)

    def stats(self) -> dict:
        with self._lock:
            return {
                "files": list(self.files),
                "chunks": len(self.chunks),
                "characters": len(self.full_text),
                "mode": "inline" if len(self.full_text) <= config.KB_INLINE_LIMIT
                        else "retrieval",
                "loaded_at": self.loaded_at,
            }


def _split_sections(raw: str, fallback_title: str) -> list[tuple[str, str]]:
    """Split a markdown file on ## headings, keeping each section together."""
    lines = raw.splitlines()
    sections: list[tuple[str, list[str]]] = []
    title = fallback_title
    buffer: list[str] = []
    for line in lines:
        if line.startswith("## "):
            if buffer:
                sections.append((title, buffer))
            title = line[3:].strip()
            buffer = []
        elif line.startswith("# "):
            title = line[2:].strip()
        else:
            buffer.append(line)
    if buffer:
        sections.append((title, buffer))
    return [(t, "\n".join(b).strip()) for t, b in sections if "".join(b).strip()]


def _bm25(query: str, chunks: list[Chunk], df: dict[str, int],
          avg_len: float, k1: float = 1.5, b: float = 0.75) -> list[tuple[float, Chunk]]:
    q_terms = _tokens(query)
    n = len(chunks)
    scored: list[tuple[float, Chunk]] = []
    for chunk in chunks:
        length = len(chunk.tokens) or 1
        freqs: dict[str, int] = {}
        for term in chunk.tokens:
            freqs[term] = freqs.get(term, 0) + 1
        score = 0.0
        for term in q_terms:
            f = freqs.get(term, 0)
            if not f:
                continue
            idf = math.log(1 + (n - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5))
            score += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * length / avg_len))
        scored.append((score, chunk))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored


def dir_for(account_id: Optional[int]) -> Path:
    """None is the original single-tenant agent - keeps reading the same
    knowledge/ folder it always has, so nothing about the live deployment
    changes. Each sub-account gets its own folder on the data volume, so
    uploading a doc for one business can never leak into another's answers."""
    if account_id is None:
        return config.KNOWLEDGE_DIR
    return config.DATA_DIR / "accounts" / str(account_id) / "knowledge"


_registry: dict[Optional[int], KnowledgeBase] = {}
_registry_lock = threading.Lock()


def get_kb(account_id: Optional[int] = None) -> KnowledgeBase:
    """Lazily loads and caches one KnowledgeBase per account, so a quiet
    sub-account with no traffic costs nothing until its first call."""
    with _registry_lock:
        kb = _registry.get(account_id)
        if kb is not None:
            return kb
    kb = KnowledgeBase()
    kb.load(dir_for(account_id))
    with _registry_lock:
        _registry[account_id] = kb
    return kb


def reload_kb(account_id: Optional[int] = None) -> KnowledgeBase:
    """Forces a fresh read from disk - called right after an admin uploads a
    knowledge file for this account."""
    kb = _registry.get(account_id)
    if kb is None:
        return get_kb(account_id)
    kb.load(dir_for(account_id))
    return kb
