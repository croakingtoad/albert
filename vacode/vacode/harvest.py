"""Crawling the LIS service into the local mirror.

The crawl runs in two phases, and the split is what makes it survivable:

  1. **Structure** walks the tree (titles -> chapters -> section headings) and writes
     one stub row per section plus a work-queue entry. It is cheap - a few thousand
     requests - and it produces the table of contents on its own.
  2. **Bodies** drains that queue one section at a time. This is the expensive phase
     (33,000 requests for the Code) and the reason the queue exists: an interrupted
     run resumes exactly where it stopped instead of starting over.

Only the HTTP fetches are threaded. Every write goes through the single connection on
the calling thread, which keeps SQLite's threading rules trivially satisfied and makes
the commit cadence (rather than lock contention) the thing that bounds throughput.

Re-harvesting is hash-based: a section whose upstream HTML is byte-identical to what
is stored is left alone, so a quarterly refresh only rewrites what actually changed.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import api, citations, db, normalize

# Enough parallelism to finish a full harvest in about an hour without hammering a
# service that exists for the public. Measured at ~7 sections/second.
DEFAULT_WORKERS = 8

# Rows per transaction. Small enough that an interrupted run loses almost nothing,
# large enough that fsync is not the bottleneck.
COMMIT_EVERY = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _noop(*_args, **_kwargs):
    pass


def _upsert_container(connection, corpus, kind, key, number, name, parent_key, sort_key):
    connection.execute(
        """INSERT INTO containers (corpus, kind, key, number, name, parent_key, sort_key)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(corpus, kind, key) DO UPDATE SET
               number = excluded.number, name = excluded.name,
               parent_key = excluded.parent_key, sort_key = excluded.sort_key""",
        (corpus, kind, key, number, name, parent_key, sort_key),
    )


def container_key_for(corpus: str, row) -> str:
    """The containers key a section belongs under.

    Each corpus nests differently - the Code by chapter, the Administrative Code by
    agency then chapter, the Constitution by article - and collapsing that into one
    key is what lets the table-of-contents walk stay corpus-agnostic. A Code section
    the service never placed in a chapter (the UCC titles) keys to its title.
    """
    get = row.get if hasattr(row, "get") else (lambda k, d="": row[k] if k in row.keys() else d)
    title = get("title_number", "") or ""
    if corpus == "admincode":
        return "/".join(p for p in (title, get("agency_number", ""), get("chapter_number", "")) if p)
    if corpus == "constitution":
        return title
    chapter = get("chapter_number", "") or ""
    return f"{title}/{chapter}" if chapter else title


_STUB_COLUMNS = (
    "corpus", "citation", "citation_key", "heading", "url", "sort_key", "container_key", "status",
    "title_number", "title_name", "agency_number", "agency_name",
    "chapter_number", "chapter_name", "article_number", "article_name",
    "part_number", "part_name", "subtitle_number", "subtitle_name",
    "subpart_number", "subpart_name",
)


def _upsert_stub(connection, values: dict):
    values = dict(values)
    values.setdefault("container_key", container_key_for(values["corpus"], values))
    values.setdefault("status", "active")
    """Write the structural facts about a section without touching its text.

    Structure and body are updated independently so that re-running the (cheap) tree
    crawl to pick up a reorganized chapter never discards text that is still current.
    """
    row = tuple(values.get(column, "") for column in _STUB_COLUMNS)
    assignments = ", ".join(f"{column} = excluded.{column}" for column in _STUB_COLUMNS[3:])
    connection.execute(
        f"""INSERT INTO sections ({", ".join(_STUB_COLUMNS)})
            VALUES ({", ".join("?" * len(_STUB_COLUMNS))})
            ON CONFLICT(corpus, citation_key) DO UPDATE SET {assignments}""",
        row,
    )


def _enqueue(connection, corpus, citation_key, payload=None):
    """Queue a section for its body fetch, leaving an already-done entry alone."""
    connection.execute(
        """INSERT INTO harvest_queue (corpus, citation_key, payload, state)
           VALUES (?, ?, ?, 'pending')
           ON CONFLICT(corpus, citation_key) DO UPDATE SET payload = excluded.payload""",
        (corpus, citation_key, json.dumps(payload or {})),
    )


# --- structure phase --------------------------------------------------------

def _safe(call, *args):
    """Run one crawl request, turning a transport failure into (None, error).

    A single unreachable chapter must not abort a crawl that has already made
    thousands of successful requests; the failure is recorded and the run continues.
    """
    try:
        return call(*args), None
    except api.ApiError as exc:
        return None, str(exc)


# The Uniform Commercial Code titles (8.1A, 8.2, 8.9A, ...) are organized into Parts,
# not Chapters, and the chapter operation answers placeholder rows with empty numbers
# for them - so the ordinary title -> chapter -> section walk reaches none of the UCC.
# The detail operation does know those sections, so they are found by enumeration:
# within a part, UCC numbering runs 01, 02, 03 ... with no gaps, so probing until a
# run of consecutive misses finds a part's sections exactly.
PROBE_PARTS = 12
PROBE_MISS_STREAK = 8
PROBE_MAX_SECTION = 99
PROBE_DECIMAL_MISS_STREAK = 2


def _probe_part(title_number, part, workers):
    """Every section the service knows in one Part of a title, found by enumeration."""
    found, misses, number = [], 0, 0
    while misses < PROBE_MISS_STREAK and number < PROBE_MAX_SECTION:
        batch = []
        while len(batch) < workers and number < PROBE_MAX_SECTION:
            number += 1
            batch.append(f"{title_number}-{part}{number:02d}")
        with ThreadPoolExecutor(workers) as pool:
            results = list(pool.map(lambda c: _safe(api.section_detail, c)[0], batch))
        for citation, detail in zip(batch, results):
            if detail:
                found.append(detail)
                misses = 0
            else:
                misses += 1
    return found


def _probe_decimals(base_citations, workers):
    """Sections carrying a decimal suffix (18.2-51.2 style) hanging off found sections."""
    found, live = [], list(base_citations)
    suffix = 0
    while live and suffix < 20:
        suffix += 1
        candidates = [f"{c}.{suffix}" for c in live]
        with ThreadPoolExecutor(workers) as pool:
            results = list(pool.map(lambda c: _safe(api.section_detail, c)[0], candidates))
        live = []
        for citation, detail in zip(candidates, results):
            if detail:
                found.append(detail)
                live.append(citation.rsplit(".", 1)[0])
    return found


def probe_title(title_number, workers=DEFAULT_WORKERS, progress=_noop):
    """Enumerate a title the chapter operation cannot describe.

    Stops after two consecutive empty Parts, which is what bounds the work: a title
    with seven parts costs roughly nine parts' worth of probes, not an open-ended scan.
    """
    found, empty_parts = [], 0
    for part in range(1, PROBE_PARTS + 1):
        rows = _probe_part(title_number, part, workers)
        progress("probe", part, PROBE_PARTS)
        if rows:
            found.extend(rows)
            empty_parts = 0
        else:
            empty_parts += 1
            if empty_parts >= 2 and found:
                break
    found.extend(_probe_decimals([row["citation"] for row in found], workers))
    return found


def structure_vacode(connection, workers=DEFAULT_WORKERS, progress=_noop):
    """Walk Code of Virginia titles, chapters and section headings."""
    titles = api.titles()
    for title in titles:
        _upsert_container(connection, "vacode", "title", title["title_number"], title["title_number"],
                          title["title_name"], "", db.sort_key(title["title_number"]))
    connection.commit()
    progress("titles", len(titles), len(titles))

    with ThreadPoolExecutor(workers) as pool:
        chapter_lists = list(pool.map(lambda t: _safe(api.chapters, t["title_number"])[0] or [], titles))

    pairs = []
    for chapters in chapter_lists:
        for chapter in chapters:
            # Placeholder rows with no number are how the service reports a title it
            # cannot decompose into chapters; they are skipped here and the title is
            # picked up by the probe pass below.
            if not chapter["chapter_number"]:
                continue
            key = f"{chapter['title_number']}/{chapter['chapter_number']}"
            _upsert_container(connection, "vacode", "chapter", key, chapter["chapter_number"],
                              chapter["chapter_name"], chapter["title_number"],
                              db.sort_key(chapter["title_number"], chapter["chapter_number"]))
            pairs.append((chapter["title_number"], chapter["chapter_number"]))
    connection.commit()
    progress("chapters", len(pairs), len(pairs))

    seen = 0
    covered = set()
    with ThreadPoolExecutor(workers) as pool:
        crawls = pool.map(lambda p: (p, _safe(api.sections, *p)), pairs)
        for index, (pair, (sections, error)) in enumerate(crawls, start=1):
            if error:
                _record_error(connection, "vacode", f"chapter {pair[0]}/{pair[1]}", error)
                continue
            for section in sections or []:
                _store_vacode_stub(connection, section)
                covered.add(section["title_number"])
                seen += 1
            if index % 50 == 0:
                connection.commit()
                progress("sections", index, len(pairs))
    connection.commit()
    progress("sections", len(pairs), len(pairs))

    for title in titles:
        if title["title_number"] in covered:
            continue
        for detail in probe_title(title["title_number"], workers=workers, progress=progress):
            _store_vacode_stub(connection, {
                "citation": detail["citation"],
                "heading": detail["heading"],
                "title_number": detail["title_number"] or title["title_number"],
                "title_name": detail["title_name"] or title["title_name"],
                "chapter_number": detail["chapter_number"],
                "chapter_name": detail["chapter_name"],
                "article_number": detail["article_number"],
                "article_name": detail["article_name"],
                "part_number": detail["part_number"],
                "part_name": detail["part_name"],
                "subtitle_number": detail["subtitle_number"],
                "subtitle_name": detail["subtitle_name"],
                "subpart_number": detail["subpart_number"],
                "subpart_name": detail["subpart_name"],
            })
            seen += 1
        connection.commit()
    return seen


def _store_vacode_stub(connection, section):
    key = citations.normalize_key(section["citation"]) or citations.clean(section["citation"]).lower()
    _upsert_stub(connection, {
        **section,
        "citation_key": key,
        "corpus": "vacode",
        "url": citations.code_url(section["citation"]),
        "sort_key": db.sort_key(section["title_number"],
                                section["chapter_number"] or section.get("part_number", ""),
                                section["citation"]),
    })
    _enqueue(connection, "vacode", key)


def _record_error(connection, corpus, what, error):
    """Keep crawl failures in the database so a harvest can be audited after the fact."""
    connection.execute(
        """INSERT INTO crawl_errors (corpus, what, error, noticed_at) VALUES (?, ?, ?, ?)
           ON CONFLICT(corpus, what) DO UPDATE SET error = excluded.error,
                                                   noticed_at = excluded.noticed_at""",
        (corpus, what, error[:500], _now()),
    )


def structure_admincode(connection, workers=DEFAULT_WORKERS, progress=_noop):
    """Walk Administrative Code titles, agencies, chapters and section headings."""
    titles = api.admin_titles()
    for title in titles:
        _upsert_container(connection, "admincode", "title", title["title_number"], title["title_number"],
                          title["title_name"], "", db.sort_key(title["title_number"]))
    connection.commit()
    progress("titles", len(titles), len(titles))

    with ThreadPoolExecutor(workers) as pool:
        agency_lists = list(pool.map(lambda t: api.admin_agencies(t["title_number"]), titles))

    agencies = []
    for rows in agency_lists:
        for agency in rows:
            key = f"{agency['title_number']}/{agency['agency_number']}"
            _upsert_container(connection, "admincode", "agency", key, agency["agency_number"],
                              agency["agency_name"], agency["title_number"],
                              db.sort_key(agency["title_number"], agency["agency_number"]))
            agencies.append((agency["title_number"], agency["agency_number"]))
    connection.commit()
    progress("agencies", len(agencies), len(agencies))

    with ThreadPoolExecutor(workers) as pool:
        chapter_lists = list(pool.map(lambda p: api.admin_chapters(*p), agencies))

    chapters = []
    for rows in chapter_lists:
        for chapter in rows:
            key = f"{chapter['title_number']}/{chapter['agency_number']}/{chapter['chapter_number']}"
            _upsert_container(connection, "admincode", "chapter", key, chapter["chapter_number"],
                              chapter["chapter_name"], f"{chapter['title_number']}/{chapter['agency_number']}",
                              db.sort_key(chapter["title_number"], chapter["agency_number"], chapter["chapter_number"]))
            chapters.append((chapter["title_number"], chapter["agency_number"], chapter["chapter_number"]))
    connection.commit()
    progress("chapters", len(chapters), len(chapters))

    # 'Preface' is a pseudo-chapter holding an agency summary rather than regulations;
    # it has no section list, so asking for one just wastes a request.
    crawlable = [c for c in chapters if c[2].lower() != "preface"]

    seen = 0
    with ThreadPoolExecutor(workers) as pool:
        for index, sections in enumerate(pool.map(lambda c: api.admin_sections(*c), crawlable), start=1):
            for section in sections:
                key = citations.admin_key(section["citation"])
                appendix = citations.is_admin_appendix(section["section_number"])
                _upsert_stub(connection, {
                    **section,
                    "citation_key": key,
                    "corpus": "admincode",
                    "url": citations.admin_url(section["title_number"], section["agency_number"],
                                               section["chapter_number"], section["section_number"]),
                    "sort_key": db.sort_key(section["title_number"], section["agency_number"],
                                            section["chapter_number"], section["section_number"]),
                    "status": "appendix" if appendix else "active",
                })
                if not appendix:
                    _enqueue(connection, "admincode", key, {
                        "title": section["title_number"], "agency": section["agency_number"],
                        "chapter": section["chapter_number"], "section": section["section_number"],
                    })
                seen += 1
            if index % 50 == 0:
                connection.commit()
                progress("sections", index, len(crawlable))
    connection.commit()
    progress("sections", len(crawlable), len(crawlable))
    return seen


def structure_constitution(connection, workers=DEFAULT_WORKERS, progress=_noop):
    """Walk the Constitution's articles and section headings."""
    articles = api.constitution_articles()
    for article in articles:
        _upsert_container(connection, "constitution", "title", article["article_number"],
                          article["article_number"],
                          article["article_name"] or article["article_label"], "",
                          db.sort_key(article["article_number"]))
    connection.commit()
    progress("articles", len(articles), len(articles))

    seen = 0
    with ThreadPoolExecutor(workers) as pool:
        section_lists = list(pool.map(lambda a: api.constitution_sections(a["article_number"]), articles))
    for sections in section_lists:
        for section in sections:
            article_number, section_number = section["article_number"], section["section_number"]
            key = citations.constitution_key(article_number, section_number)
            _upsert_stub(connection, {
                "corpus": "constitution",
                "citation": f"Va. Const. art. {article_number}, § {section_number}",
                "citation_key": key,
                "heading": section["heading"],
                # The Constitution's two levels map onto title/chapter so that the
                # table-of-contents and filtering code stays corpus-agnostic.
                "title_number": article_number,
                "title_name": section["article_name"],
                "chapter_number": section_number,
                "chapter_name": section["heading"],
                "url": citations.constitution_url(article_number, section_number),
                "sort_key": db.sort_key(article_number, section_number),
            })
            _enqueue(connection, "constitution", key,
                     {"article": article_number, "section": section_number})
            seen += 1
    connection.commit()
    progress("sections", seen, seen)
    return seen


STRUCTURE = {
    "vacode": structure_vacode,
    "admincode": structure_admincode,
    "constitution": structure_constitution,
}


# --- body phase -------------------------------------------------------------

def _fetch_body(corpus, citation_key, payload):
    """Fetch one section's detail. Returns (citation_key, record-or-None, error-or-None)."""
    try:
        if corpus == "vacode":
            detail = api.section_detail(citation_key)
        elif corpus == "admincode":
            detail = api.admin_section_detail(payload["title"], payload["agency"],
                                              payload["chapter"], payload["section"])
        elif corpus == "constitution":
            detail = api.constitution_section_detail(payload["article"], payload["section"])
        else:
            raise ValueError(f"unknown corpus {corpus!r}")
    except api.ApiError as exc:
        return citation_key, None, str(exc)
    return citation_key, detail, None


# How many sections one pass of the body phase pulls into memory at a time. The queue
# is drained in batches rather than submitted whole: ThreadPoolExecutor.map schedules
# every task up front, so handing it 34,000 sections means 34,000 queued tasks and a
# growing pile of fetched bodies waiting to be written. Batching caps the footprint at
# a constant regardless of how large the corpus is.
BODY_BATCH = 500


def bodies(connection, corpus, workers=DEFAULT_WORKERS, limit=None, progress=_noop):
    """Drain the pending work queue, writing section text as it arrives.

    Each pass re-queries for pending work, so the loop advances purely because rows get
    marked done - which also means an interrupted run and a resumed one take exactly
    the same code path.
    """
    total = connection.execute(
        "SELECT COUNT(*) AS n FROM harvest_queue WHERE corpus = ? AND state = 'pending'",
        (corpus,),
    ).fetchone()["n"]
    if limit:
        total = min(total, limit)
    stats = {"fetched": 0, "changed": 0, "missing": 0, "errors": 0}
    if not total:
        return stats

    stamp = _now()
    while stats["fetched"] < total:
        batch_size = min(BODY_BATCH, total - stats["fetched"])
        rows = connection.execute(
            """SELECT citation_key, payload FROM harvest_queue
                WHERE corpus = ? AND state = 'pending' LIMIT ?""",
            (corpus, batch_size),
        ).fetchall()
        if not rows:
            break

        work = [(corpus, row["citation_key"], json.loads(row["payload"] or "{}")) for row in rows]
        with ThreadPoolExecutor(workers) as pool:
            for citation_key, detail, error in pool.map(lambda item: _fetch_body(*item), work):
                stats["fetched"] += 1
                if error:
                    stats["errors"] += 1
                    _mark(connection, corpus, citation_key, "error", error[:500])
                elif detail is None:
                    # The heading appeared in a chapter listing but the detail operation
                    # does not know the citation. Recorded rather than retried: it is a
                    # gap in the source, not a transport failure.
                    stats["missing"] += 1
                    _mark(connection, corpus, citation_key, "missing")
                else:
                    if _store_body(connection, corpus, citation_key, detail, stamp):
                        stats["changed"] += 1
                    _mark(connection, corpus, citation_key, "done")
        connection.commit()
        progress("bodies", stats["fetched"], total)

    connection.commit()
    progress("bodies", stats["fetched"], total)
    return stats


def _mark(connection, corpus, citation_key, state, error=""):
    connection.execute(
        "UPDATE harvest_queue SET state = ?, error = ? WHERE corpus = ? AND citation_key = ?",
        (state, error, corpus, citation_key),
    )


def _store_body(connection, corpus, citation_key, detail, stamp) -> bool:
    """Write one section's text. Returns True when the stored text actually changed.

    An unchanged body still gets a fresh retrieved_at - the section was re-verified
    against the source even though nothing moved - but skips the FTS index churn.
    """
    parsed = normalize.parse_body(detail.get("body_html", ""), detail.get("heading", ""))
    existing = connection.execute(
        "SELECT body_hash FROM sections WHERE corpus = ? AND citation_key = ?",
        (corpus, citation_key),
    ).fetchone()

    if existing and existing["body_hash"] == parsed["hash"]:
        connection.execute(
            "UPDATE sections SET retrieved_at = ? WHERE corpus = ? AND citation_key = ?",
            (stamp, corpus, citation_key),
        )
        return False

    connection.execute(
        """UPDATE sections
              SET heading = COALESCE(NULLIF(?, ''), heading),
                  body_html = ?, body_text = ?, history = ?, authority = ?,
                  status = ?, body_hash = ?, retrieved_at = ?
            WHERE corpus = ? AND citation_key = ?""",
        (
            detail.get("heading", ""),
            detail.get("body_html", ""),
            parsed["text"],
            parsed["history"] or detail.get("history", ""),
            detail.get("authority", ""),
            parsed["status"],
            parsed["hash"],
            stamp,
            corpus,
            citation_key,
        ),
    )
    return True


# --- driver -----------------------------------------------------------------

def harvest(connection, corpus="vacode", workers=DEFAULT_WORKERS, *, skip_structure=False,
            refresh=False, limit=None, progress=_noop):
    """Run a full harvest of one corpus and return a stats dict.

    refresh re-queues every section, which is what a periodic re-crawl wants; without
    it only sections that have never been fetched (or previously errored) are visited,
    which is what resuming an interrupted run wants.
    """
    if corpus not in STRUCTURE:
        raise ValueError(f"unknown corpus {corpus!r}; expected one of {', '.join(db.CORPORA)}")

    started = time.time()
    if refresh:
        connection.execute("UPDATE harvest_queue SET state = 'pending' WHERE corpus = ?", (corpus,))
        connection.commit()
    else:
        # A previous run's transport errors are worth one more attempt; genuine gaps
        # in the source ('missing') are not.
        connection.execute(
            "UPDATE harvest_queue SET state = 'pending' WHERE corpus = ? AND state = 'error'", (corpus,)
        )
        connection.commit()

    found = 0
    if not skip_structure:
        found = STRUCTURE[corpus](connection, workers=workers, progress=progress)

    stats = bodies(connection, corpus, workers=workers, limit=limit, progress=progress)
    stats["structure_sections"] = found
    stats["seconds"] = round(time.time() - started, 1)

    reindex(connection, corpus, progress=progress)
    db.set_meta(connection, f"harvested_at:{corpus}", _now())
    db.set_meta(connection, f"harvest_source:{corpus}", api.API_BASE)
    db.set_meta(connection, "normalizer_version", str(db.NORMALIZER_VERSION))
    connection.commit()
    return stats


# Reindex works through ids in blocks for the same reason the body phase does: the
# bodies it reads are 75 MB of HTML for the Code alone, and none of it needs to be
# resident at once.
REINDEX_BATCH = 1000


def reindex(connection, corpus=None, progress=_noop) -> dict:
    """Recompute derived data - container keys, sort keys, and the reference graph.

    Everything here is derivable from what the harvest already stored, so this is safe
    to run at any time and is how a mirror built by an older version picks up a change
    to the derivation rules without re-crawling 33,000 sections.
    """
    corpora = [corpus] if corpus else list(db.CORPORA)
    totals = {"sections": 0, "refs": 0}
    for name in corpora:
        ids = [row["id"] for row in connection.execute(
            "SELECT id FROM sections WHERE corpus = ? ORDER BY id", (name,))]
        if not ids:
            continue
        connection.execute("DELETE FROM refs WHERE corpus = ?", (name,))

        for start in range(0, len(ids), REINDEX_BATCH):
            block = ids[start:start + REINDEX_BATCH]
            placeholders = ", ".join("?" * len(block))
            rows = connection.execute(
                f"""SELECT id, corpus, citation, citation_key, title_number, agency_number,
                           chapter_number, part_number, body_html
                      FROM sections WHERE id IN ({placeholders})""",
                block,
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE sections SET container_key = ?, sort_key = ? WHERE id = ?",
                    (container_key_for(name, row),
                     db.sort_key(row["title_number"],
                                 row["chapter_number"] or row["part_number"] or "",
                                 row["citation"]),
                     row["id"]),
                )
                for target in citations.references_in_html(row["body_html"]):
                    if target == row["citation_key"]:
                        continue  # a section quoting its own number is not a reference
                    connection.execute(
                        "INSERT OR IGNORE INTO refs (corpus, from_key, to_key) VALUES (?, ?, ?)",
                        (name, row["citation_key"], target),
                    )
                    totals["refs"] += 1
                totals["sections"] += 1
            connection.commit()
            progress("reindex", min(start + REINDEX_BATCH, len(ids)), len(ids))

        if name == "admincode":
            totals["appendices"] = _repair_appendices(connection)
            _fill_admin_names(connection)
    return totals


def _fill_admin_names(connection) -> None:
    """Copy agency and chapter names onto Administrative Code sections.

    The section listing names only the title, so a regulation would otherwise cite as
    "Agency 20 > Chapter 20" - true, and useless next to "State Board of Elections >
    Voter Registration". The names are already in containers; this joins them across.
    """
    connection.execute(
        """UPDATE sections SET agency_name = COALESCE((
                   SELECT c.name FROM containers c
                    WHERE c.corpus = 'admincode' AND c.kind = 'agency'
                      AND c.key = sections.title_number || '/' || sections.agency_number
               ), agency_name)
            WHERE corpus = 'admincode' AND agency_number != ''"""
    )
    connection.execute(
        """UPDATE sections SET chapter_name = COALESCE((
                   SELECT c.name FROM containers c
                    WHERE c.corpus = 'admincode' AND c.kind = 'chapter'
                      AND c.key = sections.container_key
               ), chapter_name)
            WHERE corpus = 'admincode' AND chapter_number != ''"""
    )
    connection.commit()


def _repair_appendices(connection) -> int:
    """Reclassify FORMS/DIBR pseudo-sections on a mirror harvested before they were known.

    They were recorded as failed fetches, which is misleading in two directions: it
    inflates the error count and it leaves an agent unable to tell an appendix from a
    section whose text is genuinely missing.
    """
    repaired = 0
    for row in connection.execute(
        "SELECT id, citation FROM sections WHERE corpus = 'admincode' AND body_text = ''"
    ).fetchall():
        if not citations.is_admin_appendix(row["citation"].rsplit("-", 1)[-1]):
            continue
        connection.execute("UPDATE sections SET status = 'appendix' WHERE id = ?", (row["id"],))
        connection.execute(
            "DELETE FROM harvest_queue WHERE corpus = 'admincode' AND citation_key = ?",
            (citations.admin_key(row["citation"]),),
        )
        repaired += 1
    connection.commit()
    return repaired
