# vacode

A local, searchable mirror of **Virginia law** — the Code of Virginia, the Virginia
Administrative Code, and the Constitution of Virginia — built for LLM agents.

Zero dependencies. One SQLite file. Three interfaces: an **MCP server**, a **CLI with
`--json`**, and a **Python API**. Nothing here is tied to a particular agent framework.

---

## Why a mirror instead of live API calls

The Commonwealth publishes its law through a JSON service at
`https://law.lis.virginia.gov/api/`, and it is a genuinely good service — fast, open,
no key required. But **it has no search endpoint**. It can only be navigated by number:
title → chapter → section.

That means an agent holding only the upstream API can answer *"what does § 18.2-51
say?"* and cannot answer *"what does Virginia law say about malicious wounding?"* —
which is the question agents actually get asked.

So: harvest once, index locally, and give agents search, citation lookup, and structural
browse over the result.

## What's in it

| | Code of Virginia | Administrative Code | Constitution |
| --- | ---: | ---: | ---: |
| Titles | 76 | 24 | 13 articles |
| Chapters | 1,577 | 2,183 | — |
| Sections | ~33,200 | ~26,000 | 130 |
| Full text | ~75 MB | — | — |
| Harvest time | ~80 min | ~60 min | ~10 s |

The Code of Virginia is roughly **15 million tokens** of text. That is why it lives in
an index rather than in a prompt.

## Install

Nothing to install beyond Python 3.9+:

```bash
python -m vacode --help          # from this directory
```

Or put `vacode` on your PATH:

```bash
pip install ./vacode
vacode --help
```

## Harvest

```bash
vacode harvest                       # Code of Virginia (~80 min)
vacode harvest --corpus constitution # ~10 seconds
vacode harvest --corpus admincode    # regulations (~60 min)
vacode status
```

The mirror lands at `~/.local/share/vacode/vacode.db` (override with `$VACODE_DB` or
`--db`). The harvest is **resumable** — interrupt it and run it again, and it picks up
from the work queue rather than starting over. A later `vacode harvest --refresh`
re-fetches everything and rewrites only the sections whose text actually changed.

## Query

```bash
vacode search "malicious wounding"
vacode search "reckless driving" --title 46.2 --limit 5
vacode get 18.2-51
vacode get "Va. Const. art. I, § 8"
vacode toc                 # every title
vacode toc 18.2            # that title's chapters
vacode toc 18.2 4          # that chapter's section headings
vacode neighbors 18.2-51   # what is codified either side of it
vacode cited-by 18.2-51    # sections whose text links to it
```

Add `--json` to any of these for machine-readable output.

Citations are accepted in the forms people actually write: `18.2-51`, `§ 18.2-51`,
`Section 18.2-51`, `1VAC20-10-10`, `1 VAC 20-10-10`, `Va. Const. art. I, § 8`.

## Give it to an agent

### MCP (any MCP client)

```bash
vacode mcp     # speaks MCP over stdio
```

Five tools: `search_virginia_law`, `get_virginia_law_section`, `browse_virginia_law`,
`virginia_law_context`, `virginia_law_status`.

**Claude Code:**

```bash
claude mcp add vacode -- python -m vacode mcp
```

**Claude Desktop / Cursor / anything reading an MCP JSON config:**

```json
{
  "mcpServers": {
    "vacode": {
      "command": "python",
      "args": ["-m", "vacode", "mcp"],
      "env": { "VACODE_DB": "/path/to/vacode.db" }
    }
  }
}
```

The server opens the mirror **read-only**, so any number of agents can query one
database while a harvest is running.

### CLI

```bash
vacode search "adverse possession" --json | jq '.[0].citation'
```

### Python

```python
from vacode import db, search

connection = db.connect(read_only=True)
for hit in search.search(connection, "malicious wounding", limit=5):
    print(hit["citation"], hit["heading"], hit["url"])
```

## The context map

Search alone leaves an agent guessing at scope — it will search the whole Code for a
question a lawyer would answer by opening one title. The fix is not to put the corpus in
the prompt but to put the **map** there, in tiers:

```bash
vacode export-context ./context
```

- `context/vacode/titles-map.md` — all 76 titles and their names. About **1,000 tokens**.
  Small enough to keep resident; enough to route a question to a title before searching.
- `context/vacode/titles/18.2.md` — that title's chapters, articles and section headings.
  Median **6,000 tokens**, so you load the one title a question is about.
- Section text itself stays in the index and is retrieved, not preloaded.

Point any agent at those files — a Claude Code skill, a system prompt, a RAG loader, an
`AGENTS.md`. They are plain Markdown on purpose.

## Freshness

Virginia's session laws generally take effect **July 1**. Re-harvest quarterly:

```bash
vacode harvest --refresh
```

Every section carries its `retrieved_at` date and its official URL, and every tool
surfaces both, so an agent can tell a user how current the text is and where to verify
it. `vacode status` reports the last harvest per corpus.

Repealed, expired and reserved sections are **kept**, flagged, and excluded from search
results unless you ask for them — an agent that cannot see that a provision was repealed
will cite it as current law.

## Notes on the upstream service

Findings from building against it, since none of this is documented:

- The real base URL is `https://law.lis.virginia.gov/api/`. The pages at `/jsonapi/` and
  `/xmlapi/` are operation catalogs, not endpoints.
- Requests without a trailing slash answer **301**; follow redirects.
- The `...Xml` operations return **JSON** anyway. There is no reason to use them.
- A citation that does not exist answers **HTTP 200** with an empty `ChapterList`.
  Status codes are not usable for error handling.
- `SectionText` is always `null`; the text is in `Body` as HTML. The trailing paragraph
  is the enactment history, and a `<p class='sidenote'>` boilerplate paragraph is
  appended to every Code section.
- The titles list returns **79 rows for 76 titles** — 8.2, 8.2A and 8.4A each appear
  twice.
- **The Uniform Commercial Code titles (8.1A, 8.2, 8.9A, …) cannot be enumerated through
  the chapter operation.** They are organized into Parts, not Chapters, and the chapter
  listing returns placeholder rows with empty numbers. The harvester finds those sections
  by bounded enumeration against the detail operation instead — without it, the entire
  UCC is missing from the mirror.
- Cross-references inside section bodies are real anchors (`/vacode/2.2-4000/`), which
  makes the citation graph reliable to extract.

## Tests

```bash
python -m unittest discover -s tests
```

No network: the tests build their own fixture mirror in memory.

## Legal

This is an **unofficial** mirror of public law, assembled from the Commonwealth's own
public service. It is not legal advice. The official text is at
<https://law.lis.virginia.gov> — every result this tool returns carries the URL to it.
