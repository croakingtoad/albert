"""Optional semantic search: embeddings alongside the BM25 index.

BM25 is very good at what legal text is mostly made of - terms of art, defined
phrases, citations - and quite bad at the way people actually ask. "What happens if I
hurt someone with acid" does not share a word with "malicious bodily injury by means
of any caustic substance", and no amount of ranking fixes that. Embeddings do.

Everything in this module is optional. The core mirror harvests, indexes and searches
with nothing but the standard library; this adds a second ranking signal if you want
it, and the rest of the package works unchanged when the table is empty.

It needs two things the core does not:
  * numpy, for the query-time dot product (33,000 sections is small, but not small
    enough for a Python loop);
  * an embedding provider, configured by environment.

Env:
  VACODE_EMBED_PROVIDER  'openai' (any OpenAI-compatible /v1/embeddings endpoint,
                         including Together, vLLM and Ollama's compatibility shim),
                         'voyage', or 'ollama'. Default: openai.
  VACODE_EMBED_MODEL     model name. Default depends on provider.
  VACODE_EMBED_BASE_URL  API root. Default depends on provider.
  VACODE_EMBED_API_KEY   credential; falls back to OPENAI_API_KEY / VOYAGE_API_KEY.
"""

from __future__ import annotations

import json
import os
import re
import struct
import time
import urllib.error
import urllib.request

from . import db

# Sections are short - median 1.4 KB - so most become a single chunk and keep their
# full meaning in one vector. Only the long ones are split, on paragraph boundaries,
# with the heading repeated into each piece so a chunk still knows what it is about.
CHUNK_CHARS = 1200
CHUNK_OVERLAP = 150
BATCH_SIZE = 64

PROVIDERS = {
    "openai": {"base_url": "https://api.openai.com/v1", "model": "text-embedding-3-small",
               "key_env": ("VACODE_EMBED_API_KEY", "OPENAI_API_KEY")},
    "voyage": {"base_url": "https://api.voyageai.com/v1", "model": "voyage-3",
               "key_env": ("VACODE_EMBED_API_KEY", "VOYAGE_API_KEY")},
    "ollama": {"base_url": "http://localhost:11434/v1", "model": "nomic-embed-text",
               "key_env": ("VACODE_EMBED_API_KEY",)},
}

EMBEDDINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS embeddings (
    section_id INTEGER NOT NULL,
    chunk_ix   INTEGER NOT NULL,
    body_hash  TEXT NOT NULL DEFAULT '',   -- lets a re-embed skip unchanged sections
    vector     BLOB NOT NULL,
    PRIMARY KEY (section_id, chunk_ix)
);
CREATE INDEX IF NOT EXISTS embeddings_section ON embeddings (section_id);
"""


class EmbeddingError(RuntimeError):
    """The provider could not be reached, or is not configured."""


def numpy_or_none():
    """numpy if it is installed, else None. Semantic search is skipped without it."""
    try:
        import numpy
    except ImportError:
        return None
    return numpy


def settings():
    """Resolved provider configuration, from environment with per-provider defaults."""
    name = (os.environ.get("VACODE_EMBED_PROVIDER") or "openai").lower()
    if name not in PROVIDERS:
        raise EmbeddingError(f"unknown provider {name!r}; expected one of {', '.join(PROVIDERS)}")
    defaults = PROVIDERS[name]
    key = next((os.environ[env] for env in defaults["key_env"] if os.environ.get(env)), "")
    return {
        "provider": name,
        "model": os.environ.get("VACODE_EMBED_MODEL") or defaults["model"],
        "base_url": (os.environ.get("VACODE_EMBED_BASE_URL") or defaults["base_url"]).rstrip("/"),
        "api_key": key,
    }


def chunks(heading: str, text: str):
    """Split one section into embeddable pieces, each carrying its heading.

    Splitting happens on blank lines first so a chunk boundary lands between
    subsections rather than mid-sentence; the character window is the fallback for a
    single very long paragraph.
    """
    text = (text or "").strip()
    prefix = f"{heading}\n\n" if heading else ""
    if not text:
        return [prefix.strip()] if prefix.strip() else []
    if len(text) <= CHUNK_CHARS:
        return [prefix + text]

    pieces, current = [], ""
    for paragraph in re.split(r"\n\s*\n", text):
        if current and len(current) + len(paragraph) + 2 > CHUNK_CHARS:
            pieces.append(current)
            current = current[-CHUNK_OVERLAP:] + "\n\n" + paragraph if CHUNK_OVERLAP else paragraph
        else:
            current = f"{current}\n\n{paragraph}" if current else paragraph
        while len(current) > CHUNK_CHARS * 2:
            pieces.append(current[:CHUNK_CHARS])
            current = current[CHUNK_CHARS - CHUNK_OVERLAP:]
    if current.strip():
        pieces.append(current)
    return [prefix + piece.strip() for piece in pieces]


def embed_texts(texts, config=None, *, attempts=4):
    """Embed a batch of strings, returning a list of float lists."""
    config = config or settings()
    if not config["api_key"] and config["provider"] != "ollama":
        raise EmbeddingError(
            f"no API key for provider {config['provider']!r}; set VACODE_EMBED_API_KEY"
        )
    payload = json.dumps({"model": config["model"], "input": list(texts)}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if config["api_key"]:
        headers["Authorization"] = f"Bearer {config['api_key']}"
    request = urllib.request.Request(f"{config['base_url']}/embeddings", data=payload, headers=headers)

    last_error = None
    for attempt in range(attempts):
        if attempt:
            time.sleep(2 ** attempt)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                body = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}: {exc.read()[:300].decode('utf-8', 'replace')}"
            if exc.code < 500 and exc.code != 429:
                raise EmbeddingError(last_error) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = str(exc)
    else:
        raise EmbeddingError(f"embedding request failed after {attempts} attempts: {last_error}")

    rows = body.get("data") or []
    if len(rows) != len(texts):
        raise EmbeddingError(f"provider returned {len(rows)} vectors for {len(texts)} inputs")
    return [row["embedding"] for row in sorted(rows, key=lambda r: r.get("index", 0))]


def _pack(vector):
    """Store a unit-normalized float16 vector: half the memory, no measurable recall cost."""
    total = sum(component * component for component in vector) ** 0.5 or 1.0
    return struct.pack(f"<{len(vector)}e", *[component / total for component in vector])


def build(connection, corpus=None, *, batch_size=BATCH_SIZE, limit=None, progress=None,
          embedder=None, config=None):
    """Embed every section that does not already have a current vector.

    Skipping by body_hash makes this resumable and makes a re-run after a quarterly
    refresh cost only the sections whose text actually changed.
    """
    progress = progress or (lambda *_: None)
    embedder = embedder or (lambda texts: embed_texts(texts, config))
    connection.executescript(EMBEDDINGS_SCHEMA)

    sql = """SELECT s.id, s.heading, s.body_text, s.body_hash
               FROM sections s
               LEFT JOIN embeddings e ON e.section_id = s.id AND e.chunk_ix = 0
              WHERE s.body_text != '' AND (e.section_id IS NULL OR e.body_hash != s.body_hash)"""
    params = []
    if corpus:
        sql += " AND s.corpus = ?"
        params.append(corpus)
    sql += " ORDER BY s.id"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    rows = connection.execute(sql, params).fetchall()
    if not rows:
        return {"sections": 0, "chunks": 0}

    pending, totals = [], {"sections": 0, "chunks": 0}
    for row in rows:
        for index, chunk in enumerate(chunks(row["heading"], row["body_text"])):
            pending.append((row["id"], index, row["body_hash"], chunk))
        totals["sections"] += 1
        if len(pending) >= batch_size:
            totals["chunks"] += _flush(connection, pending, embedder)
            pending = []
            progress("embed", totals["sections"], len(rows))
    if pending:
        totals["chunks"] += _flush(connection, pending, embedder)
    connection.commit()
    progress("embed", len(rows), len(rows))
    db.set_meta(connection, "embedding_model", (config or settings())["model"])
    connection.commit()
    return totals


def _flush(connection, pending, embedder):
    vectors = embedder([item[3] for item in pending])
    for (section_id, chunk_ix, body_hash, _text), vector in zip(pending, vectors):
        connection.execute(
            """INSERT INTO embeddings (section_id, chunk_ix, body_hash, vector)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(section_id, chunk_ix) DO UPDATE SET
                   body_hash = excluded.body_hash, vector = excluded.vector""",
            (section_id, chunk_ix, body_hash, _pack(vector)),
        )
    connection.commit()
    return len(pending)


def is_available(connection) -> bool:
    """Whether this mirror has a usable semantic index."""
    if numpy_or_none() is None:
        return False
    try:
        row = connection.execute("SELECT COUNT(*) AS n FROM embeddings").fetchone()
    except Exception:
        return False
    return bool(row and row["n"])


# The vector matrix is rebuilt from BLOBs on first use and then kept, keyed by the
# connection: at 1536 dimensions the full Code is ~120 MB of blobs, which is nothing to
# hold once and far too much to re-read on every query. A row count is cheap enough to
# check each time, and catches a rebuild underneath a long-lived process.
_MATRIX_CACHE = {}


def _matrix(connection, numpy):
    count = connection.execute("SELECT COUNT(*) AS n FROM embeddings").fetchone()["n"]
    cached = _MATRIX_CACHE.get(id(connection))
    if cached and cached[0] == count:
        return cached[1], cached[2]

    rows = connection.execute("SELECT section_id, vector FROM embeddings ORDER BY rowid").fetchall()
    if not rows:
        return [], None
    section_ids = [row["section_id"] for row in rows]
    matrix = numpy.frombuffer(b"".join(row["vector"] for row in rows), dtype=numpy.float16)
    matrix = matrix.reshape(len(rows), -1).astype(numpy.float32)
    _MATRIX_CACHE[id(connection)] = (count, section_ids, matrix)
    return section_ids, matrix


def nearest(connection, query: str, *, limit=20, embedder=None, config=None):
    """Section ids most similar to a query, best first, as (section_id, similarity).

    Vectors are unit-normalized at write time, so cosine similarity is a dot product
    and the whole index is one matrix multiply - fast enough at this scale that an
    approximate index would only add a dependency and a failure mode.
    """
    numpy = numpy_or_none()
    if numpy is None:
        raise EmbeddingError("semantic search needs numpy: pip install numpy")

    section_ids, matrix = _matrix(connection, numpy)
    if matrix is None:
        return []

    embedder = embedder or (lambda texts: embed_texts(texts, config))
    query_vector = numpy.frombuffer(_pack(embedder([query])[0]), dtype=numpy.float16).astype(numpy.float32)
    if matrix.shape[1] != query_vector.shape[0]:
        raise EmbeddingError(
            f"stored vectors have {matrix.shape[1]} dimensions but the query has "
            f"{query_vector.shape[0]}; the embedding model changed - rebuild with "
            "'vacode embed --rebuild'"
        )

    scores = matrix @ query_vector
    # One section can own several chunks; it should be ranked by its best one.
    best = {}
    for position in numpy.argsort(-scores)[: limit * 4]:
        section_id = section_ids[int(position)]
        score = float(scores[int(position)])
        if score > best.get(section_id, -2.0):
            best[section_id] = score
    return sorted(best.items(), key=lambda item: -item[1])[:limit]


def forget(connection=None) -> None:
    """Drop the cached matrix, for a process that rebuilds the index in place."""
    if connection is None:
        _MATRIX_CACHE.clear()
    else:
        _MATRIX_CACHE.pop(id(connection), None)
