"""SQLite storage for the mirrored corpora: schema, connections, and natural sorting.

One file holds everything — structure, full text, and the FTS5 index — so the whole
mirror is a single artifact you can copy to another machine, check a hash against, or
hand to a container. There is no server to run and no dependency to install.

Env overrides (all optional):
  VACODE_DB   path to the database file
              (default: $XDG_DATA_HOME/vacode/vacode.db, else ~/.local/share/vacode/vacode.db)
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 2

# Bumping this invalidates stored bodies on the next harvest even when the upstream
# text is unchanged, which is how a normalizer fix gets re-applied to the whole mirror.
NORMALIZER_VERSION = 1

CORPORA = ("vacode", "admincode", "constitution")


def default_path() -> Path:
    """Where the mirror lives unless VACODE_DB says otherwise."""
    override = os.environ.get("VACODE_DB")
    if override:
        return Path(override).expanduser()
    data_home = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(data_home) / "vacode" / "vacode.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- The structural skeleton: titles, agencies and chapters, kept separately from
-- sections so that a chapter with no sections (or one whose section crawl failed)
-- still shows up in a table of contents instead of silently vanishing.
CREATE TABLE IF NOT EXISTS containers (
    corpus     TEXT NOT NULL,
    kind       TEXT NOT NULL,          -- 'title' | 'agency' | 'chapter'
    key        TEXT NOT NULL,          -- unique within (corpus, kind), e.g. '18.2' or '18.2/4'
    number     TEXT NOT NULL,
    name       TEXT NOT NULL DEFAULT '',
    parent_key TEXT NOT NULL DEFAULT '',
    sort_key   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (corpus, kind, key)
);
CREATE INDEX IF NOT EXISTS containers_parent ON containers (corpus, kind, parent_key, sort_key);

CREATE TABLE IF NOT EXISTS sections (
    id              INTEGER PRIMARY KEY,
    corpus          TEXT NOT NULL,
    citation        TEXT NOT NULL,      -- '18.2-51', '1VAC20-10-10', 'Va. Const. art. 1, s 1'
    citation_key    TEXT NOT NULL,      -- normalized for lookup (see citations.normalize_key)
    heading         TEXT NOT NULL DEFAULT '',
    body_html       TEXT NOT NULL DEFAULT '',
    body_text       TEXT NOT NULL DEFAULT '',
    history         TEXT NOT NULL DEFAULT '',
    authority       TEXT NOT NULL DEFAULT '',
    -- 'active' | 'repealed' | 'expired' | 'reserved'. Non-active sections are kept
    -- and returned: an agent that cannot see that a provision was repealed will
    -- happily cite it as current law.
    status          TEXT NOT NULL DEFAULT 'active',
    url             TEXT NOT NULL DEFAULT '',
    title_number    TEXT NOT NULL DEFAULT '',
    title_name      TEXT NOT NULL DEFAULT '',
    agency_number   TEXT NOT NULL DEFAULT '',
    agency_name     TEXT NOT NULL DEFAULT '',
    chapter_number  TEXT NOT NULL DEFAULT '',
    chapter_name    TEXT NOT NULL DEFAULT '',
    article_number  TEXT NOT NULL DEFAULT '',
    article_name    TEXT NOT NULL DEFAULT '',
    part_number     TEXT NOT NULL DEFAULT '',
    part_name       TEXT NOT NULL DEFAULT '',
    subtitle_number TEXT NOT NULL DEFAULT '',
    subtitle_name   TEXT NOT NULL DEFAULT '',
    subpart_number  TEXT NOT NULL DEFAULT '',
    subpart_name    TEXT NOT NULL DEFAULT '',
    -- The containers row this section belongs to ('18.2/4', '1/20/10', '1'), which is
    -- what makes the table of contents an exact join instead of a prefix match. Empty
    -- for a section the service never placed in a chapter.
    container_key   TEXT NOT NULL DEFAULT '',
    sort_key        TEXT NOT NULL DEFAULT '',
    body_hash       TEXT NOT NULL DEFAULT '',
    retrieved_at    TEXT NOT NULL DEFAULT '',
    UNIQUE (corpus, citation_key)
);
CREATE INDEX IF NOT EXISTS sections_by_place ON sections (corpus, title_number, chapter_number, sort_key);
CREATE INDEX IF NOT EXISTS sections_by_key ON sections (citation_key);
CREATE INDEX IF NOT EXISTS sections_by_status ON sections (corpus, status);
CREATE INDEX IF NOT EXISTS sections_by_container ON sections (corpus, container_key, sort_key);

-- The cross-reference graph, read out of the anchors the service embeds in section
-- bodies. Stored rather than scanned so 'what cites this?' is an index lookup.
CREATE TABLE IF NOT EXISTS refs (
    corpus   TEXT NOT NULL,
    from_key TEXT NOT NULL,
    to_key   TEXT NOT NULL,
    PRIMARY KEY (corpus, from_key, to_key)
);
CREATE INDEX IF NOT EXISTS refs_inbound ON refs (to_key);

-- Work queue for the harvester: one row per section the structure crawl found, so a
-- text harvest can be interrupted and resumed without re-walking the tree.
CREATE TABLE IF NOT EXISTS harvest_queue (
    corpus       TEXT NOT NULL,
    citation_key TEXT NOT NULL,
    payload      TEXT NOT NULL DEFAULT '',   -- JSON: whatever the detail fetch needs
    state        TEXT NOT NULL DEFAULT 'pending',  -- pending | done | missing | error
    error        TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (corpus, citation_key)
);
CREATE INDEX IF NOT EXISTS harvest_queue_state ON harvest_queue (corpus, state);

-- Crawl-level failures (a chapter listing that would not load), kept apart from the
-- section queue so a harvest can be audited without polluting the work list.
CREATE TABLE IF NOT EXISTS crawl_errors (
    corpus     TEXT NOT NULL,
    what       TEXT NOT NULL,
    error      TEXT NOT NULL DEFAULT '',
    noticed_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (corpus, what)
);

CREATE VIRTUAL TABLE IF NOT EXISTS sections_fts USING fts5 (
    citation,
    heading,
    body_text,
    content='sections',
    content_rowid='id',
    tokenize="unicode61 remove_diacritics 2"
);

CREATE TRIGGER IF NOT EXISTS sections_ai AFTER INSERT ON sections BEGIN
    INSERT INTO sections_fts (rowid, citation, heading, body_text)
    VALUES (new.id, new.citation, new.heading, new.body_text);
END;
CREATE TRIGGER IF NOT EXISTS sections_ad AFTER DELETE ON sections BEGIN
    INSERT INTO sections_fts (sections_fts, rowid, citation, heading, body_text)
    VALUES ('delete', old.id, old.citation, old.heading, old.body_text);
END;
CREATE TRIGGER IF NOT EXISTS sections_au AFTER UPDATE ON sections BEGIN
    INSERT INTO sections_fts (sections_fts, rowid, citation, heading, body_text)
    VALUES ('delete', old.id, old.citation, old.heading, old.body_text);
    INSERT INTO sections_fts (rowid, citation, heading, body_text)
    VALUES (new.id, new.citation, new.heading, new.body_text);
END;
"""


def connect(path=None, *, read_only: bool = False) -> sqlite3.Connection:
    """Open the mirror, creating and migrating it if needed.

    read_only opens an existing file with the SQLite immutable-free URI mode so that
    several agents can query one mirror concurrently while a harvest is running.
    """
    path = Path(path) if path else default_path()
    if read_only:
        if not path.exists():
            raise FileNotFoundError(f"no mirror at {path} - run 'vacode harvest' first")
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        connection.row_factory = sqlite3.Row
        # A read-only handle cannot migrate, and a query against a mirror missing a
        # column added since it was built fails with a bare SQLite error. Saying so
        # here turns that into an instruction.
        stored = get_meta(connection, "schema_version", "0")
        if int(stored or 0) < SCHEMA_VERSION:
            raise RuntimeError(
                f"the mirror at {path} was built by an older version of vacode "
                f"(schema {stored}, this build expects {SCHEMA_VERSION}); "
                "run 'vacode reindex' once to migrate it - no re-crawl is needed"
            )
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    if not read_only:
        # WAL lets readers keep working during a multi-hour harvest.
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        # Migrations run first: SCHEMA creates indexes over columns added after the
        # first release, so on an older mirror the script itself would fail before any
        # migration could repair it. On a new database there is nothing to migrate.
        _migrate(connection)
        connection.executescript(SCHEMA)
        set_meta(connection, "schema_version", str(SCHEMA_VERSION))
        connection.commit()
    return connection


# Columns added after the first release. CREATE TABLE IF NOT EXISTS will not add a
# column to a table that already exists, so each one is applied explicitly against
# databases harvested by an earlier version rather than forcing a re-harvest.
_ADDED_COLUMNS = {
    "sections": {"container_key": "TEXT NOT NULL DEFAULT ''"},
}


def _migrate(connection: sqlite3.Connection) -> None:
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    for table, columns in _ADDED_COLUMNS.items():
        # A table absent altogether is a new database, not an old one: SCHEMA is about
        # to create it with every column already in place.
        if table not in tables:
            continue
        existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def get_meta(connection: sqlite3.Connection, key: str, default=None):
    row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


_SORT_PART = re.compile(r"(\d+)|(\D+)")


def sort_key(*parts: str) -> str:
    """A string that sorts legal numbering the way a lawyer reads it.

    Plain text sorting puts Title 10.1 before Title 2.2 and Chapter 10 before Chapter 2.
    Each run of digits is zero-padded to eight places so numeric runs compare
    numerically while the letters around them still compare as letters ('8.2A' after
    '8.2', '18.2-51.1' after '18.2-51').
    """
    out = []
    for part in parts:
        chunks = []
        for digits, text in _SORT_PART.findall(str(part or "")):
            chunks.append(digits.zfill(8) if digits else text.lower())
        out.append("".join(chunks))
    return "|".join(out)


def counts(connection: sqlite3.Connection) -> dict:
    """Row counts per corpus, for status output and freshness checks."""
    out = {}
    for row in connection.execute(
        """SELECT corpus,
                  COUNT(*) AS n,
                  SUM(status != 'active') AS inactive,
                  MIN(retrieved_at) AS oldest,
                  MAX(retrieved_at) AS newest
             FROM sections GROUP BY corpus"""
    ):
        out[row["corpus"]] = {
            "sections": row["n"],
            "inactive": row["inactive"] or 0,
            "oldest_retrieved_at": row["oldest"],
            "newest_retrieved_at": row["newest"],
        }
    return out
