"""Lookup and full-text search over the mirror.

The service the mirror is built from has no search endpoint at all: it can only be
navigated by number. That asymmetry is the whole reason this package exists, and it
shapes the query path here - a citation is answered by exact lookup, and everything
else goes to FTS5/BM25 over the section text.

Fields are weighted so that a section whose *heading* is about the query outranks one
that merely mentions it in passing, which matters when a chapter of definitions
mentions every term in the title.
"""

from __future__ import annotations

import re
import sqlite3

from . import citations, db, embed, normalize

# bm25 column weights, in the order the FTS table declares them: citation, heading,
# body. A heading hit is worth roughly four body hits; a citation hit dominates.
BM25_WEIGHTS = (8.0, 4.0, 1.0)

RESULT_COLUMNS = """
    id, corpus, citation, citation_key, heading, status, url, history,
    title_number, title_name, agency_number, agency_name,
    chapter_number, chapter_name, article_number, article_name,
    retrieved_at
"""

# FTS5 treats these as syntax. A user query is data, not syntax, so they are stripped
# before the query is rebuilt out of quoted phrases.
_FTS_UNSAFE = re.compile(r"[^\w\s.\-']", re.UNICODE)
_TOKENS = re.compile(r'"([^"]*)"|(\S+)')

# Words that are in nearly every section and only cost precision.
STOPWORDS = {
    "a", "an", "and", "any", "are", "as", "at", "be", "by", "for", "from", "in", "is",
    "it", "of", "on", "or", "shall", "such", "that", "the", "to", "which", "with",
}


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in row.keys()}


def fts_query(text: str, *, operator: str = "AND") -> str:
    """Build a safe FTS5 MATCH expression from free text.

    Every term becomes a quoted phrase, so punctuation in a citation ('18.2-51') and
    apostrophes in prose can never be read as FTS operators.
    """
    parts = []
    for phrase, word in _TOKENS.findall(text or ""):
        raw = _FTS_UNSAFE.sub(" ", phrase or word).strip()
        if not raw:
            continue
        if not phrase and raw.lower() in STOPWORDS:
            continue
        parts.append('"' + raw.replace('"', "") + '"')
    return f" {operator} ".join(parts)


def query_terms(text: str):
    """The bare words of a query, for snippet windowing."""
    return [t for t in re.split(r"\W+", text or "") if len(t) > 2 and t.lower() not in STOPWORDS]


def get(connection, citation: str, corpus: str | None = None, *, include_text: bool = True):
    """Look up one section by citation. Returns None if the mirror does not have it.

    Accepts anything citations.normalize_key understands, and falls back to a literal
    match so that a citation stored in an unusual form is still reachable.
    """
    key = citations.normalize_key(citation) or citations.clean(citation).lower()
    columns = RESULT_COLUMNS + (", body_text, body_html" if include_text else "")
    sql = f"SELECT {columns} FROM sections WHERE citation_key = ?"
    params = [key]
    if corpus:
        sql += " AND corpus = ?"
        params.append(corpus)
    sql += " ORDER BY corpus LIMIT 1"
    row = connection.execute(sql, params).fetchone()
    if not row:
        return None
    record = _row_to_dict(row)
    if include_text:
        record["references"] = citations.references_in_html(record.pop("body_html", ""))
    return record


# Reciprocal rank fusion: each ranker contributes 1/(RRF_K + rank), so a section both
# rankers like beats one that either loves alone. The constant damps the top of each
# list enough that a single ranker cannot dominate the merge.
RRF_K = 60


def search(connection, query: str, *, corpus=None, title=None, status="active",
           limit: int = 10, include_text: bool = False, snippet_width: int = 320,
           mode: str = "auto"):
    """Search the mirror, answering a citation query by lookup and the rest by ranking.

    status defaults to 'active' because an agent asking what the law says almost never
    wants a repealed section silently mixed in; pass status=None to include them.

    mode selects the ranker: 'text' is BM25 only, 'semantic' is embeddings only,
    'hybrid' fuses them, and 'auto' (the default) uses hybrid when this mirror has a
    semantic index and text when it does not - so the same call works with or without
    the optional embedding step.
    """
    query = (query or "").strip()
    if not query:
        return []

    # A citation is an exact question and deserves an exact answer, not a ranked list.
    if citations.looks_like_citation(query):
        hit = get(connection, query, corpus, include_text=True)
        if hit:
            hit["match"] = "citation"
            hit["score"] = 0.0
            if not include_text:
                hit["snippet"] = normalize.snippet(hit.get("body_text", ""), [], snippet_width)
                hit.pop("body_text", None)
            return [hit]

    if mode == "auto":
        mode = "hybrid" if embed.is_available(connection) else "text"

    if mode == "semantic":
        return _semantic_search(connection, query, corpus, title, status, limit,
                                include_text, snippet_width)
    if mode == "hybrid":
        return _hybrid_search(connection, query, corpus, title, status, limit,
                              include_text, snippet_width)

    results = _fts_search(connection, query, corpus, title, status, limit, include_text,
                          snippet_width, "AND")
    if not results:
        # An unmatched conjunction usually means one rare word, not an empty corpus.
        results = _fts_search(connection, query, corpus, title, status, limit, include_text,
                              snippet_width, "OR")
    return results


def _semantic_search(connection, query, corpus, title, status, limit, include_text,
                     snippet_width, *, embedder=None):
    ranked = embed.nearest(connection, query, limit=limit * 3, embedder=embedder)
    rows = _load_by_id(connection, [section_id for section_id, _ in ranked], corpus, title,
                       status, include_text, query, snippet_width)
    scores = dict(ranked)
    for row in rows:
        row["match"] = "semantic"
        row["score"] = round(scores.get(row["id"], 0.0), 4)
    rows.sort(key=lambda row: -row["score"])
    return rows[:limit]


def _hybrid_search(connection, query, corpus, title, status, limit, include_text,
                   snippet_width, *, embedder=None):
    """Fuse BM25 and embedding rankings, then return whole records for the winners."""
    lexical = _fts_search(connection, query, corpus, title, status, limit * 3, False,
                          snippet_width, "OR")
    try:
        semantic = embed.nearest(connection, query, limit=limit * 3, embedder=embedder)
    except embed.EmbeddingError:
        # A missing key or an unreachable provider must degrade to text search, not
        # fail the query: the lexical index is always there.
        return lexical[:limit]

    fused = {}
    for rank, row in enumerate(lexical):
        fused.setdefault(row["id"], 0.0)
        fused[row["id"]] += 1.0 / (RRF_K + rank + 1)
    for rank, (section_id, _score) in enumerate(semantic):
        fused.setdefault(section_id, 0.0)
        fused[section_id] += 1.0 / (RRF_K + rank + 1)

    order = sorted(fused, key=lambda section_id: -fused[section_id])[:limit]
    rows = _load_by_id(connection, order, corpus, title, status, include_text, query, snippet_width)
    for row in rows:
        row["match"] = "hybrid"
        row["score"] = round(fused.get(row["id"], 0.0), 6)
    rows.sort(key=lambda row: -row["score"])
    return rows


def _load_by_id(connection, ids, corpus, title, status, include_text, query, snippet_width):
    """Fetch full records for a ranked list of ids, applying the same filters as FTS."""
    if not ids:
        return []
    columns = ", ".join(f"s.{c.strip()}" for c in RESULT_COLUMNS.split(",") if c.strip())
    placeholders = ", ".join("?" * len(ids))
    sql = f"SELECT {columns}, s.body_text, s.body_html FROM sections s WHERE s.id IN ({placeholders})"
    params = list(ids)
    if corpus:
        sql += " AND s.corpus = ?"
        params.append(corpus)
    if title:
        sql += " AND s.title_number = ?"
        params.append(str(title))
    if status:
        sql += " AND s.status = ?"
        params.append(status)

    terms = query_terms(query)
    out = []
    for row in connection.execute(sql, params):
        record = _row_to_dict(row)
        if include_text:
            record["references"] = citations.references_in_html(record.pop("body_html", ""))
        else:
            record.pop("body_html", None)
            record["snippet"] = normalize.snippet(record.pop("body_text", ""), terms, snippet_width)
        out.append(record)
    return out


def _fts_search(connection, query, corpus, title, status, limit, include_text, snippet_width, operator):
    match = fts_query(query, operator=operator)
    if not match:
        return []

    columns = ", ".join(f"s.{c.strip()}" for c in RESULT_COLUMNS.split(",") if c.strip())
    sql = f"""
        SELECT {columns},
               bm25(sections_fts, ?, ?, ?) AS score,
               snippet(sections_fts, 2, '', '', '...', 32) AS snippet
               {", s.body_text, s.body_html" if include_text else ""}
          FROM sections_fts
          JOIN sections s ON s.id = sections_fts.rowid
         WHERE sections_fts MATCH ?
    """
    params = [*BM25_WEIGHTS, match]
    if corpus:
        sql += " AND s.corpus = ?"
        params.append(corpus)
    if title:
        sql += " AND s.title_number = ?"
        params.append(str(title))
    if status:
        sql += " AND s.status = ?"
        params.append(status)
    sql += " ORDER BY score LIMIT ?"
    params.append(limit)

    try:
        rows = connection.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        # A malformed MATCH is a bug in fts_query, not something a caller can fix.
        raise ValueError(f"bad search query {query!r}: {exc}") from exc

    terms = query_terms(query)
    out = []
    for row in rows:
        record = _row_to_dict(row)
        record["match"] = "text"
        record["score"] = round(-record["score"], 4)  # bm25 is negative; higher is better
        if include_text:
            record["references"] = citations.references_in_html(record.pop("body_html", ""))
        else:
            body = record.pop("body_text", None)
            if body is not None:
                record["snippet"] = normalize.snippet(body, terms, snippet_width)
        out.append(record)
    return out


def toc(connection, corpus="vacode", *path, include_counts=True):
    """Browse the structure by walking down a path of container numbers.

    The walk is generic on purpose, because the three corpora are not the same shape:
    the Code of Virginia nests title > chapter, the Administrative Code nests
    title > agency > chapter, and the Constitution has only articles. Rather than
    encode those, this returns whatever children the requested container has, and
    falls back to that container's sections when it has none.

        toc(c, "vacode")              -> the 76 titles
        toc(c, "vacode", "18.2")      -> that title's chapters
        toc(c, "vacode", "18.2", "4") -> that chapter's section headings
        toc(c, "admincode", "1", "20")-> that agency's chapters
    """
    path = [str(part) for part in path if part not in (None, "")]
    key = "/".join(path)

    count_column = (
        """, (SELECT COUNT(*) FROM sections s
              WHERE s.corpus = c.corpus
                AND (s.container_key = c.key OR s.container_key LIKE c.key || '/%')) AS sections"""
        if include_counts else ", 0 AS sections"
    )
    children = connection.execute(
        f"""SELECT c.kind, c.key, c.number, c.name {count_column}
              FROM containers c WHERE c.corpus = ? AND c.parent_key = ? ORDER BY c.sort_key""",
        (corpus, key),
    ).fetchall()

    result = {"corpus": corpus, "path": path, "key": key}
    if path:
        result["container"] = _container(connection, corpus, key)

    if children:
        result["level"] = children[0]["kind"] + "s"
        result["items"] = [_row_to_dict(row) for row in children]
        # A container can hold both sub-containers and sections the service never
        # placed in one - the UCC titles, which are organized into Parts. Those would
        # otherwise be invisible from every level of the walk.
        unplaced = sections_in(connection, corpus, key)
        if unplaced:
            result["unplaced_sections"] = unplaced
        return result

    result["level"] = "sections"
    result["items"] = sections_in(connection, corpus, key)
    return result


def _container(connection, corpus, key):
    row = connection.execute(
        "SELECT kind, key, number, name FROM containers WHERE corpus = ? AND key = ?",
        (corpus, key),
    ).fetchone()
    return _row_to_dict(row) if row else {"key": key, "number": key.split("/")[-1], "name": ""}


def sections_in(connection, corpus, container_key, *, descendants=False):
    """The sections filed directly under one container (or under it and everything below)."""
    sql = """SELECT citation, citation_key, heading, status, chapter_number, chapter_name,
                    article_number, article_name, part_number, part_name, url, container_key
               FROM sections WHERE corpus = ? AND """
    if descendants and container_key:
        sql += "(container_key = ? OR container_key LIKE ? || '/%')"
        params = [corpus, container_key, container_key]
    else:
        sql += "container_key = ?"
        params = [corpus, container_key]
    sql += " ORDER BY sort_key"
    return [_row_to_dict(r) for r in connection.execute(sql, params)]


def neighbors(connection, citation: str, corpus=None, span: int = 2):
    """The sections immediately before and after one section, in codified order.

    Legal text is contextual: the definition or exception that controls a provision is
    usually the section next to it, and an agent that can only fetch exact citations
    never sees it. Ordering uses the stored sort key rather than a recomputed one, so
    sections the service files outside a chapter still sit in the right place.
    """
    key = citations.normalize_key(citation) or citations.clean(citation).lower()
    sql = "SELECT corpus, container_key, sort_key FROM sections WHERE citation_key = ?"
    params = [key]
    if corpus:
        sql += " AND corpus = ?"
        params.append(corpus)
    anchor = connection.execute(sql + " LIMIT 1", params).fetchone()
    if not anchor:
        return []

    scope = (anchor["corpus"], anchor["container_key"], anchor["sort_key"], span)
    before = connection.execute(
        """SELECT citation, heading, status FROM sections
            WHERE corpus = ? AND container_key = ? AND sort_key < ?
            ORDER BY sort_key DESC LIMIT ?""", scope).fetchall()
    after = connection.execute(
        """SELECT citation, heading, status FROM sections
            WHERE corpus = ? AND container_key = ? AND sort_key > ?
            ORDER BY sort_key LIMIT ?""", scope).fetchall()
    return [_row_to_dict(r) for r in reversed(before)] + [_row_to_dict(r) for r in after]


def cited_by(connection, citation: str, corpus=None, limit: int = 25):
    """Sections whose text links to this one.

    Read from the stored cross-reference graph, which is built out of the anchors the
    service embeds in section bodies - far more reliable than parsing the prose, and an
    index lookup rather than a scan of every body in the corpus.
    """
    key = citations.normalize_key(citation) or citations.clean(citation).lower()
    if not key:
        return []
    sql = """SELECT s.citation, s.heading, s.status, s.url
               FROM refs r JOIN sections s
                 ON s.corpus = r.corpus AND s.citation_key = r.from_key
              WHERE r.to_key = ?"""
    params = [key]
    if corpus:
        sql += " AND r.corpus = ?"
        params.append(corpus)
    sql += " ORDER BY s.sort_key LIMIT ?"
    params.append(limit)
    return [_row_to_dict(r) for r in connection.execute(sql, params)]


def stats(connection, path=None):
    """What is in the mirror and when it was last refreshed."""
    out = {"database": str(path or ""), "corpora": db.counts(connection)}
    for row in connection.execute("SELECT key, value FROM meta ORDER BY key"):
        out.setdefault("meta", {})[row["key"]] = row["value"]
    pending = connection.execute(
        "SELECT corpus, state, COUNT(*) AS n FROM harvest_queue GROUP BY corpus, state"
    ).fetchall()
    if pending:
        out["queue"] = {f"{r['corpus']}:{r['state']}": r["n"] for r in pending}
    return out
