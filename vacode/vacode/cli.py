"""Command line for the mirror: harvest it, then query it.

Every query subcommand takes --json, so the same binary serves a person reading a
terminal and a program shelling out. Agents that speak MCP get the same three
operations over stdio from `vacode mcp`; agents that speak neither can import
`vacode.search` directly. The intent is that no agent framework is privileged.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import db, embed, harvest, search, toc


def _progress(stage, done, total):
    print(f"\r{stage}: {done}/{total}    ", end="", file=sys.stderr, flush=True)
    if done >= total:
        print(file=sys.stderr)


def _connect(args, *, write=False):
    """Open the mirror. Query commands take a read-only handle so several can run at
    once against a database a harvest is still writing to."""
    return db.connect(args.db, read_only=not write)


def _emit(args, payload, render):
    if args.json:
        json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
        print()
    else:
        render(payload)


def _format_section(record, *, full=False):
    lines = [f"{search.format_citation(record)}  {record['heading']}"]
    place = search.describe_place(record)
    if place:
        lines.append(place)
    note = search.status_note(record)
    if note:
        lines.append(f"STATUS: {note}")
    lines.append("")
    body = record.get("body_text") if full else (record.get("snippet") or record.get("body_text", ""))
    if body:
        lines += [body, ""]
    if record.get("history"):
        lines += [f"History: {record['history']}", ""]
    if full and record.get("references"):
        lines += ["References: " + ", ".join(record["references"]), ""]
    when = record.get("retrieved_at") or ""
    lines.append(f"{record.get('url', '')}  ({'retrieved ' + when if when else 'text never retrieved'})")
    return "\n".join(lines)


# --- commands ---------------------------------------------------------------

def cmd_harvest(args):
    connection = _connect(args, write=True)
    stats = harvest.harvest(
        connection, args.corpus, workers=args.workers,
        skip_structure=args.skip_structure, refresh=args.refresh, limit=args.limit,
        progress=_progress if not args.quiet else harvest._noop,
    )
    _emit(args, stats, lambda s: print(
        f"{args.corpus}: {s['fetched']} fetched, {s['changed']} changed, "
        f"{s['missing']} missing, {s['errors']} errors in {s['seconds']}s"
    ))
    return 0


def cmd_get(args):
    connection = _connect(args)
    record = search.get(connection, args.citation, args.corpus)
    if not record:
        print(f"not found: {args.citation}", file=sys.stderr)
        return 1
    if not args.full:
        record.pop("body_html", None)
    _emit(args, record, lambda r: print(_format_section(r, full=True)))
    return 0


def cmd_search(args):
    connection = _connect(args)
    try:
        results = search.search(
            connection, args.query, corpus=args.corpus, title=args.title,
            status=None if args.include_inactive else "active",
            limit=args.limit, include_text=args.full, mode=args.mode,
        )
    except embed.EmbeddingError as exc:
        print(f"vacode search: {exc}", file=sys.stderr)
        print("(use --mode text for keyword search without a provider)", file=sys.stderr)
        return 2

    def render(rows):
        if not rows:
            print("no results", file=sys.stderr)
            return
        for index, row in enumerate(rows, start=1):
            print(f"{index}. " + _format_section(row, full=args.full).replace("\n", "\n   "))
            print()

    _emit(args, results, render)
    return 0 if results else 1


def cmd_toc(args):
    connection = _connect(args)
    listing = search.toc(connection, args.corpus, *args.path)
    def render(data):
        if data.get("container"):
            container = data["container"]
            print(f"{container.get('kind', 'container').title()} {container['number']}. "
                  f"{container['name']}".rstrip(". ") + "\n")
        if data["level"] == "sections":
            for item in data["items"]:
                flag = "" if item["status"] == "active" else f"  [{item['status']}]"
                print(f"  {search.format_citation(item):<18} {item['heading']}{flag}")
            return
        label = data["level"].rstrip("s").title()
        for item in data["items"]:
            print(f"  {label} {item['number']:<10} {item['name']}  ({item['sections']} sections)")
        for item in data.get("unplaced_sections") or []:
            print(f"  {search.format_citation(item):<18} {item['heading']}")
    _emit(args, listing, render)
    return 0


def cmd_neighbors(args):
    connection = _connect(args)
    rows = search.neighbors(connection, args.citation, args.corpus, args.span)
    _emit(args, rows, lambda items: [
        print(f"{search.format_citation(i):<16} {i['heading']}"
              + ("" if i["status"] == "active" else f"  [{i['status']}]"))
        for i in items
    ])
    return 0


def cmd_cited_by(args):
    connection = _connect(args)
    rows = search.cited_by(connection, args.citation, args.corpus, args.limit)
    _emit(args, rows, lambda items: [
        print(f"{search.format_citation(i):<16} {i['heading']}") for i in items])
    return 0


def cmd_status(args):
    connection = _connect(args)
    payload = search.stats(connection, args.db or db.default_path())
    def render(data):
        print(f"database: {data['database']}")
        for corpus, counts in sorted(data["corpora"].items()):
            print(f"  {corpus:<13} {counts['sections']:>7} sections "
                  f"({counts['inactive']} inactive), harvested {counts['newest_retrieved_at']}")
        for key, value in sorted((data.get("queue") or {}).items()):
            if not key.endswith(":done"):
                print(f"  queue {key}: {value}")
    _emit(args, payload, render)
    return 0


def cmd_export_context(args):
    connection = _connect(args)
    result = toc.export(connection, args.directory, args.corpus)
    _emit(args, result, lambda r: print(f"wrote {r['titles']} title files to {r['directory']}"))
    return 0


def cmd_reindex(args):
    connection = _connect(args, write=True)
    totals = harvest.reindex(connection, args.corpus, progress=_progress)
    _emit(args, totals, lambda t: print(f"reindexed {t['sections']} sections, {t['refs']} references"))
    return 0


def cmd_embed(args):
    connection = _connect(args, write=True)
    if args.rebuild:
        connection.executescript(embed.EMBEDDINGS_SCHEMA)
        connection.execute("DELETE FROM embeddings")
        connection.commit()
        embed.forget(connection)
    try:
        totals = embed.build(connection, args.corpus, limit=args.limit, progress=_progress)
    except embed.EmbeddingError as exc:
        print(f"vacode embed: {exc}", file=sys.stderr)
        return 2
    _emit(args, totals, lambda t: print(f"embedded {t['sections']} sections in {t['chunks']} chunks"))
    return 0


def cmd_mcp(args):
    from . import mcp_server
    return mcp_server.serve(args.db)


# --- argument parsing -------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="vacode",
        description="Local, searchable mirror of the Code of Virginia and its companion corpora.",
    )
    parser.add_argument("--db", help="path to the mirror (default: $VACODE_DB or ~/.local/share/vacode/vacode.db)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of formatted text")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p = subparsers.add_parser("harvest", help="crawl the LIS service into the mirror")
    p.add_argument("--corpus", default="vacode", choices=db.CORPORA)
    p.add_argument("--workers", type=int, default=harvest.DEFAULT_WORKERS)
    p.add_argument("--refresh", action="store_true", help="re-fetch every section, not just missing ones")
    p.add_argument("--skip-structure", action="store_true", help="drain the queue without re-walking the tree")
    p.add_argument("--limit", type=int, help="stop after this many section bodies")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_harvest)

    p = subparsers.add_parser("get", help="one section by citation")
    p.add_argument("citation")
    p.add_argument("--corpus", choices=db.CORPORA)
    p.add_argument("--full", action="store_true", help="include the raw HTML body")
    p.set_defaults(func=cmd_get)

    p = subparsers.add_parser("search", help="full-text search")
    p.add_argument("query")
    p.add_argument("--corpus", choices=db.CORPORA)
    p.add_argument("--title", help="restrict to one title number, e.g. 18.2")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--full", action="store_true", help="return whole sections instead of snippets")
    p.add_argument("--include-inactive", action="store_true",
                   help="also return repealed, expired and reserved sections")
    p.add_argument("--mode", default="auto", choices=["auto", "text", "semantic", "hybrid"],
                   help="ranker: text is BM25, semantic needs 'vacode embed', "
                        "auto (default) uses both when a semantic index exists")
    p.set_defaults(func=cmd_search)

    p = subparsers.add_parser(
        "toc", help="browse the structure",
        description="With no path, lists titles. Each further element walks one level down: "
                    "'toc 18.2' lists that title's chapters, 'toc 18.2 4' lists that chapter's "
                    "sections, 'toc --corpus admincode 1 20' lists that agency's chapters.")
    p.add_argument("path", nargs="*", help="container numbers, outermost first")
    p.add_argument("--corpus", default="vacode", choices=db.CORPORA)
    p.set_defaults(func=cmd_toc)

    p = subparsers.add_parser("neighbors", help="sections codified either side of one section")
    p.add_argument("citation")
    p.add_argument("--corpus", choices=db.CORPORA)
    p.add_argument("--span", type=int, default=2)
    p.set_defaults(func=cmd_neighbors)

    p = subparsers.add_parser("cited-by", help="sections whose text links to this one")
    p.add_argument("citation")
    p.add_argument("--corpus", choices=db.CORPORA)
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=cmd_cited_by)

    p = subparsers.add_parser(
        "reindex", help="recompute derived data (container keys, sort keys, the reference graph)",
        description="Everything it rebuilds is derived from text already harvested, so this "
                    "never touches the network.")
    p.add_argument("--corpus", choices=db.CORPORA)
    p.set_defaults(func=cmd_reindex)

    p = subparsers.add_parser("status", help="what the mirror holds and when it was refreshed")
    p.set_defaults(func=cmd_status)

    p = subparsers.add_parser("export-context", help="write the tiered Markdown map for agent context")
    p.add_argument("directory")
    p.add_argument("--corpus", default="vacode", choices=db.CORPORA)
    p.set_defaults(func=cmd_export_context)

    p = subparsers.add_parser(
        "embed", help="build the optional semantic index",
        description="Needs numpy and an embedding provider (VACODE_EMBED_PROVIDER, "
                    "VACODE_EMBED_MODEL, VACODE_EMBED_API_KEY). Resumable, and a re-run "
                    "after a refresh only embeds sections whose text changed.")
    p.add_argument("--corpus", choices=db.CORPORA)
    p.add_argument("--limit", type=int, help="stop after this many sections")
    p.add_argument("--rebuild", action="store_true", help="discard existing vectors first")
    p.set_defaults(func=cmd_embed)

    p = subparsers.add_parser("mcp", help="serve the mirror over MCP on stdio")
    p.set_defaults(func=cmd_mcp)

    return parser


def main(argv=None):
    # The corpus is full of section signs and em dashes; a console or pipe that
    # defaults to a narrower codepage would fail on the first result rather than the
    # first unusual one.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass

    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted; rerun to resume", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
