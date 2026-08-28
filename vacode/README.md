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

Measured on a full harvest, 28 August 2026:

| | Code of Virginia | Administrative Code | Constitution |
| --- | ---: | ---: | ---: |
| Titles | 76 | 24 | 13 articles |
| Agencies | — | 152 | — |
| Chapters | 1,553 | 2,183 | — |
| Sections | **33,854** | **34,627** | 130 |
| Repealed / expired / reserved | 2,262 | — | 0 |
| Cross-references | 39,992 | — | — |
| Plain text | 52 MB | — | — |
| Harvest | 43 min at 12 workers, 0 errors | ~45 min | 11 s |

The whole mirror — three corpora, full text, FTS5 index and the reference graph — is
one 221 MB SQLite file. The Code of Virginia alone is roughly **13 million tokens** of
text. That is why it lives in an index rather than in a prompt.

99 of the 33,854 sections are known to the chapter listings but not to the detail
operation — a gap in the source, recorded in the harvest queue rather than papered over.

## Install

Nothing to install beyond Python 3.9+ (numpy only if you want the optional semantic
index):

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
vacode harvest                       # Code of Virginia (~45 min)
vacode harvest --corpus constitution # ~10 seconds
vacode harvest --corpus admincode    # regulations (~45 min)
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

## Optional: semantic search

BM25 is very good at what legal text is mostly made of — terms of art, defined phrases,
citations — and bad at the way people ask. *"What happens if I hurt someone with acid"*
shares no word with *"malicious bodily injury by means of any caustic substance."*

Embeddings fix that, and are opt-in because they need a dependency and a provider:

```bash
pip install numpy
export VACODE_EMBED_API_KEY=sk-...        # or OPENAI_API_KEY / VOYAGE_API_KEY
vacode embed                              # ~19M tokens for the full Code
```

Once built, `vacode search` fuses BM25 and vector rankings (reciprocal rank fusion) by
default. Force one or the other with `--mode text` / `--mode semantic` / `--mode hybrid`.

Providers: any OpenAI-compatible `/v1/embeddings` endpoint (`VACODE_EMBED_PROVIDER=openai`,
the default — also covers Together and vLLM), Voyage (`voyage`), or a local Ollama
(`ollama`). Set `VACODE_EMBED_BASE_URL` and `VACODE_EMBED_MODEL` to point anywhere else.

Without numpy or without a provider, everything still works — `search` uses BM25 and says
nothing about it. The embedding step is resumable and skips sections whose text has not
changed since the last run.

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

Measured on the real corpus:

| Tier | File | Size |
| --- | --- | ---: |
| 0 | `context/vacode/titles-map.md` — all 76 titles and their names | 4 KB, **~1,000 tokens** |
| 1 | `context/vacode/titles/18.2.md` — one title's chapters, articles and section headings | median **~5,000 tokens** (largest, Title 15.2: ~43,000) |
| 2 | the section text itself | retrieved from the index, never preloaded |

Tier 0 is small enough to keep resident and is enough to route a question to a title
before searching. Tier 1 is loaded for the one title a question is about. Titles the
service organizes into Parts rather than Chapters (the UCC) are grouped by Part, so their
outlines read the same way.

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
