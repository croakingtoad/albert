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

from . import citations, db, normalize

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


def search(connection, query: str, *, corpus=None, title=None, status="active",
           limit: int = 10, include_text: bool = False, snippet_width: int = 320):
    """Search the mirror, answering a citation query by lookup and the rest by BM25.

    status defaults to 'active' because an agent asking what the law says almost never
    wants a repealed section silently mixed in; pass status=None to include them.
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

    results = _fts_search(connection, query, corpus, title, status, limit, include_text, snippet_width, "AND")
    if not results:
        # An unmatched conjunction usually means one rare word, not an empty corpus.
        results = _fts_search(connection, query, corpus, title, status, limit, include_text, snippet_width, "OR")
    return results


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


def toc(connection, corpus="vacode", title=None, chapter=None, *, include_sections=None):
    """Browse the structure: titles, then chapters, then section headings.

    Returns the level below whatever was specified, which is what makes this usable as
    a drill-down: no arguments lists titles, a title lists its chapters, and a title
    plus chapter lists that chapter's sections.
    """
    if include_sections is None:
        include_sections = chapter is not None

    if title is None:
        rows = connection.execute(
            """SELECT number, name, (SELECT COUNT(*) FROM sections s
                                      WHERE s.corpus = c.corpus AND s.title_number = c.number) AS sections
                 FROM containers c WHERE corpus = ? AND kind = 'title' ORDER BY sort_key""",
            (corpus,),
        ).fetchall()
        return {"corpus": corpus, "level": "titles", "items": [_row_to_dict(r) for r in rows]}

    title = str(title)
    if chapter is None:
        rows = connection.execute(
            """SELECT number, name, (SELECT COUNT(*) FROM sections s
                                      WHERE s.corpus = c.corpus AND s.title_number = ?
                                        AND s.chapter_number = c.number) AS sections
                 FROM containers c
                WHERE corpus = ? AND kind = 'chapter' AND parent_key LIKE ?
                ORDER BY sort_key""",
            (title, corpus, f"{title}%"),
        ).fetchall()
        title_row = connection.execute(
            "SELECT number, name FROM containers WHERE corpus = ? AND kind = 'title' AND number = ?",
            (corpus, title),
        ).fetchone()
        result = {
            "corpus": corpus,
            "level": "chapters",
            "title": _row_to_dict(title_row) if title_row else {"number": title, "name": ""},
            "items": [_row_to_dict(r) for r in rows],
        }
        if include_sections:
            result["sections"] = sections_in(connection, corpus, title, None)
        return result

    return {
        "corpus": corpus,
        "level": "sections",
        "title": {"number": title},
        "chapter": {"number": str(chapter)},
        "items": sections_in(connection, corpus, title, str(chapter)),
    }


def sections_in(connection, corpus, title, chapter):
    sql = """SELECT citation, heading, status, article_number, article_name, chapter_number, url
               FROM sections WHERE corpus = ? AND title_number = ?"""
    params = [corpus, title]
    if chapter is not None:
        sql += " AND chapter_number = ?"
        params.append(chapter)
    sql += " ORDER BY sort_key"
    return [_row_to_dict(r) for r in connection.execute(sql, params)]


def neighbors(connection, citation: str, corpus=None, span: int = 2):
    """The sections immediately before and after one section, in codified order.

    Legal text is contextual: the definition that controls a provision is usually the
    section next to it, and an agent that can only fetch exact citations never sees it.
    """
    row = get(connection, citation, corpus, include_text=False)
    if not row:
        return []
    before = connection.execute(
        """SELECT citation, heading, status FROM sections
            WHERE corpus = ? AND title_number = ? AND chapter_number = ? AND sort_key < ?
            ORDER BY sort_key DESC LIMIT ?""",
        (row["corpus"], row["title_number"], row["chapter_number"],
         db.sort_key(row["title_number"], row["chapter_number"], row["citation"]), span),
    ).fetchall()
    after = connection.execute(
        """SELECT citation, heading, status FROM sections
            WHERE corpus = ? AND title_number = ? AND chapter_number = ? AND sort_key > ?
            ORDER BY sort_key LIMIT ?""",
        (row["corpus"], row["title_number"], row["chapter_number"],
         db.sort_key(row["title_number"], row["chapter_number"], row["citation"]), span),
    ).fetchall()
    return [_row_to_dict(r) for r in reversed(before)] + [_row_to_dict(r) for r in after]


def cited_by(connection, citation: str, limit: int = 25):
    """Sections whose text links to this one.

    Cross-references are read from the anchors the service embeds in section bodies,
    which is far more reliable than parsing the prose around them.
    """
    key = citations.normalize_key(citation) or citations.clean(citation).lower()
    if not key:
        return []
    rows = connection.execute(
        """SELECT citation, heading, status, url FROM sections
            WHERE body_html LIKE ? ORDER BY sort_key LIMIT ?""",
        (f"%/vacode/{key}/%", limit),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


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
