"""An MCP server for the mirror, spoken over stdio in about three hundred lines.

This is deliberately dependency-free: MCP's stdio transport is newline-delimited
JSON-RPC, which the standard library can do, and requiring an SDK would tie the mirror
to one vendor's release cadence for no benefit. Any MCP client - Claude Code, Claude
Desktop, Cursor, Continue, an SDK agent, a homegrown loop - can run:

    vacode mcp

Results are returned as formatted text rather than raw JSON because that is what a
model reads best: each section comes back with its citation, where it sits in the Code,
its status, its official URL, and the date the text was retrieved, so a model that
quotes it can also cite it and say how fresh it is.
"""

from __future__ import annotations

import json
import sys
import traceback

from . import db, search

# Versions this server knows how to speak. A client asking for one of these gets it
# echoed back; anything else is answered with the newest we implement, which is what
# the specification asks for.
KNOWN_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
DEFAULT_PROTOCOL_VERSION = "2025-06-18"

SERVER_INFO = {"name": "vacode", "version": "1.0.0"}

INSTRUCTIONS = """Search and read Virginia law from a local mirror of the Commonwealth's
official Legislative Information System.

Covers the Code of Virginia (statutes), the Virginia Administrative Code (regulations)
and the Constitution of Virginia. Prefer search_virginia_law for questions phrased in
words, and get_virginia_law_section when you already have a citation.

The mirror is unofficial and is only as current as its last harvest: every result
carries the official URL and the retrieval date. Repealed and expired sections are kept
and are excluded from search results unless you ask for them, so a section that is
missing from a search may still exist as repealed - look it up by citation to see."""

CORPUS_ENUM = list(db.CORPORA)


TOOLS = [
    {
        "name": "search_virginia_law",
        "description": (
            "Full-text search across Virginia law. Use this for questions asked in words "
            "('what is the penalty for reckless driving'). Returns ranked sections with a "
            "snippet, the official URL and the retrieval date. Narrow with `title` when you "
            "already know the subject area (18.2 is crimes, 46.2 motor vehicles, 8.01 civil "
            "procedure); browse_virginia_law lists the titles."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Words to search for, or a citation."},
                "corpus": {"type": "string", "enum": CORPUS_ENUM,
                           "description": "Restrict to statutes (vacode), regulations (admincode) or the constitution."},
                "title": {"type": "string", "description": "Restrict to one title number, e.g. '18.2'."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                "full_text": {"type": "boolean", "default": False,
                              "description": "Return whole sections instead of snippets. Costly; use after narrowing."},
                "include_inactive": {"type": "boolean", "default": False,
                                     "description": "Also return repealed, expired and reserved sections."},
                "mode": {"type": "string", "enum": ["auto", "text", "semantic", "hybrid"],
                         "default": "auto",
                         "description": "Ranking strategy. Leave as auto unless a keyword search "
                                        "missed something you are sure is there, in which case "
                                        "'semantic' finds sections that mean the same thing in "
                                        "different words."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_virginia_law_section",
        "description": (
            "Read one section in full by citation. Accepts the forms people actually write: "
            "'18.2-51', '§ 18.2-51', '1VAC20-10-10', 'Va. Const. art. I, § 8'. Returns the "
            "operative text, the enactment history, the sections it cross-references, its "
            "status, the official URL and the retrieval date."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "citation": {"type": "string"},
                "corpus": {"type": "string", "enum": CORPUS_ENUM},
            },
            "required": ["citation"],
        },
    },
    {
        "name": "browse_virginia_law",
        "description": (
            "Walk the structure of the law. With no arguments it lists every title; add a "
            "`path` to walk down one level at a time - ['18.2'] lists that title's chapters, "
            "['18.2','4'] lists that chapter's section headings. For regulations the path is "
            "title, agency, chapter. Use it to find the right corner of the law before "
            "searching, or to see what else a chapter contains."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "corpus": {"type": "string", "enum": CORPUS_ENUM, "default": "vacode"},
                "path": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Container numbers, outermost first, e.g. ['18.2'] or ['18.2','4'].",
                },
            },
        },
    },
    {
        "name": "virginia_law_context",
        "description": (
            "The sections codified immediately around one section, and the sections whose text "
            "links to it. Definitions and exceptions usually live next door to the provision "
            "they govern, so this is how to find the parts of the law a citation depends on."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "citation": {"type": "string"},
                "span": {"type": "integer", "minimum": 1, "maximum": 10, "default": 2,
                         "description": "How many sections either side to include."},
            },
            "required": ["citation"],
        },
    },
    {
        "name": "virginia_law_status",
        "description": (
            "What the mirror contains and when it was last refreshed. Check this before relying "
            "on the corpus for anything time-sensitive: Virginia's session laws generally take "
            "effect July 1, so a mirror harvested before then will not reflect them."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


# --- rendering --------------------------------------------------------------

def render_section(record, *, body_key="body_text") -> str:
    lines = [f"{search.format_citation(record)} — {record['heading']}"]
    place = search.describe_place(record)
    if place:
        lines.append(place)
    note = search.status_note(record)
    if note:
        lines.append(f"STATUS: {note}")
    body = record.get(body_key) or record.get("snippet") or ""
    if body:
        lines += ["", body]
    if record.get("history"):
        lines += ["", f"History: {record['history']}"]
    if record.get("references"):
        lines += ["", "Cross-references: " + ", ".join(record["references"][:20])]
    when = record.get("retrieved_at") or ""
    lines += ["", f"Source: {record.get('url', '')} "
                  f"({'retrieved ' + when if when else 'text never retrieved'})"]
    return "\n".join(lines)


# --- tool implementations ---------------------------------------------------

def _tool_search(connection, arguments):
    results = search.search(
        connection,
        arguments.get("query", ""),
        corpus=arguments.get("corpus"),
        title=arguments.get("title"),
        status=None if arguments.get("include_inactive") else "active",
        limit=int(arguments.get("limit") or 10),
        include_text=bool(arguments.get("full_text")),
        mode=arguments.get("mode") or "auto",
    )
    if not results:
        return ("No matching sections. Try fewer or more common words, drop the `title` filter, "
                "or set include_inactive if the provision may have been repealed.")
    header = f"{len(results)} result(s) for {arguments.get('query')!r}:"
    blocks = [render_section(r) for r in results]
    return header + "\n\n" + "\n\n---\n\n".join(blocks)


def _tool_get(connection, arguments):
    record = search.get(connection, arguments.get("citation", ""), arguments.get("corpus"))
    if not record:
        return (f"No section {arguments.get('citation')!r} in the mirror. Check the citation, or use "
                "search_virginia_law to find it by words.")
    return render_section(record)


def _tool_browse(connection, arguments):
    corpus = arguments.get("corpus") or "vacode"
    path = arguments.get("path") or []
    if isinstance(path, str):
        path = [path]
    # Older callers, and models that pattern-match on the CLI, may send title/chapter
    # instead of a path; accepting both costs one line and avoids a useless tool error.
    if not path:
        path = [p for p in (arguments.get("title"), arguments.get("agency"),
                            arguments.get("chapter")) if p]

    listing = search.toc(connection, corpus, *path)
    header = ""
    if listing.get("container"):
        container = listing["container"]
        header = (f"{container.get('kind', 'container').title()} {container['number']}. "
                  f"{container['name']}").rstrip(". ")

    lines = [header] if header else []
    if listing["level"] == "sections":
        lines.append(f"{len(listing['items'])} sections:")
        for item in listing["items"]:
            flag = "" if item["status"] == "active" else f"  [{item['status']}]"
            lines.append(f"  {search.format_citation(item)} — {item['heading']}{flag}")
    else:
        label = listing["level"].rstrip("s")
        lines.append(f"{len(listing['items'])} {listing['level']}:")
        for item in listing["items"]:
            lines.append(f"  {label} {item['number']}: {item['name']} ({item['sections']} sections)")
        for item in listing.get("unplaced_sections") or []:
            lines.append(f"  {search.format_citation(item)} — {item['heading']}")
    if not listing["items"] and not listing.get("unplaced_sections"):
        lines.append("Nothing at that path. Call this tool with no arguments to see the titles.")
    return "\n".join(lines)


def _tool_context(connection, arguments):
    citation = arguments.get("citation", "")
    span = int(arguments.get("span") or 2)
    around = search.neighbors(connection, citation, span=span)
    inbound = search.cited_by(connection, citation)
    lines = [f"Context for {citation}:", "", "Codified nearby:"]
    lines += [f"  {search.format_citation(i)} — {i['heading']}" for i in around] or ["  (none)"]
    lines += ["", "Sections that cite it:"]
    lines += [f"  {search.format_citation(i)} — {i['heading']}" for i in inbound] or ["  (none)"]
    return "\n".join(lines)


def _tool_status(connection, _arguments):
    payload = search.stats(connection)
    lines = ["Mirror contents:"]
    for corpus, counts in sorted(payload["corpora"].items()):
        lines.append(f"  {corpus}: {counts['sections']} sections "
                     f"({counts['inactive']} repealed/expired/reserved), "
                     f"last retrieved {counts['newest_retrieved_at']}")
    if not payload["corpora"]:
        lines.append("  (empty — run 'vacode harvest')")
    lines.append("")
    lines.append("Source: https://law.lis.virginia.gov (unofficial mirror; verify anything load-bearing).")
    return "\n".join(lines)


HANDLERS = {
    "search_virginia_law": _tool_search,
    "get_virginia_law_section": _tool_get,
    "browse_virginia_law": _tool_browse,
    "virginia_law_context": _tool_context,
    "virginia_law_status": _tool_status,
}


# --- JSON-RPC plumbing ------------------------------------------------------

def _result(request_id, payload):
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle(message, connection):
    """Answer one JSON-RPC message. Returns the response, or None for notifications."""
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in KNOWN_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
        return _result(request_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        })

    # Notifications carry no id and must never be answered.
    if request_id is None:
        return None

    if method == "ping":
        return _result(request_id, {})

    if method == "tools/list":
        return _result(request_id, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        handler = HANDLERS.get(name)
        if not handler:
            return _error(request_id, -32602, f"unknown tool {name!r}")
        try:
            text = handler(connection, params.get("arguments") or {})
        except Exception as exc:  # surfaced to the model as a tool error, not a crash
            return _result(request_id, {
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                "isError": True,
            })
        return _result(request_id, {"content": [{"type": "text", "text": text}], "isError": False})

    return _error(request_id, -32601, f"method not found: {method}")


def serve(database=None, stdin=None, stdout=None) -> int:
    """Run the stdio loop until the client closes the stream."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout

    # Section signs and em dashes are everywhere in this corpus, and a Windows console
    # defaults to a codepage that cannot encode them - which would turn every response
    # into a UnicodeEncodeError rather than a tool result.
    for stream in (stdin, stdout):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass

    try:
        connection = db.connect(database, read_only=True)
    except FileNotFoundError as exc:
        print(f"vacode mcp: {exc}", file=sys.stderr)
        return 2

    def reply(payload):
        json.dump(payload, stdout, ensure_ascii=False)
        stdout.write("\n")
        stdout.flush()

    # readline rather than iteration: iterating a text stream reads ahead, which on a
    # pipe can hold a client's request until the next one arrives.
    while True:
        line = stdin.readline()
        if not line:
            return 0
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            reply(_error(None, -32700, "parse error"))
            continue
        try:
            response = handle(message, connection)
        except Exception:
            traceback.print_exc(file=sys.stderr)
            response = _error(message.get("id"), -32603, "internal error")
        if response is not None:
            reply(response)
