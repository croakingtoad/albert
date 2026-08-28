"""Exporting the mirror's structure as tiered Markdown an agent can hold in context.

Search alone leaves an agent guessing at scope: it will semantic-search the entire
Code for a question that a lawyer would answer by opening one title. The fix is not to
put the corpus in the prompt - the Code of Virginia is roughly 15 million tokens - but
to put the *map* there, in tiers:

  tier 0  every title, number and name. About a thousand tokens. Cheap enough to keep
          resident, and enough to route a question to a title before searching.
  tier 1  one file per title: its chapters, articles and section headings. Median
          6,000 tokens, so it is loaded for the one title a question is about.
  tier 2  the section text itself, which comes from the index rather than a file.

That is the same progressive-disclosure shape a well-built skill uses, and it works
for any agent that can read a file, not just for one framework.
"""

from __future__ import annotations

from pathlib import Path

from . import db, search

CORPUS_TITLES = {
    "vacode": "Code of Virginia",
    "admincode": "Virginia Administrative Code",
    "constitution": "Constitution of Virginia",
}

SOURCE_NOTE = (
    "Mirrored from the Virginia Legislative Information System "
    "(https://law.lis.virginia.gov). Unofficial: cite the URL on each section and "
    "verify anything load-bearing against the official site."
)


def _stamp(connection, corpus: str) -> str:
    return db.get_meta(connection, f"harvested_at:{corpus}", "never") or "never"


def tier0(connection, corpus: str = "vacode") -> str:
    """The whole-corpus map: every title, with how many sections it holds."""
    listing = search.toc(connection, corpus)
    name = CORPUS_TITLES.get(corpus, corpus)
    lines = [
        f"# {name} - title map",
        "",
        f"{len(listing['items'])} titles. Harvested {_stamp(connection, corpus)}.",
        "",
        "Route a question to a title here, then load that title's file for its chapters "
        "and section headings, then retrieve the section text itself.",
        "",
        "| Title | Name | Sections |",
        "| --- | --- | ---: |",
    ]
    for item in listing["items"]:
        lines.append(f"| {item['number']} | {item['name']} | {item['sections']} |")
    lines += ["", SOURCE_NOTE, ""]
    return "\n".join(lines)


def tier1(connection, corpus: str, title_number: str) -> str:
    """One title's full outline: every level below it, down to section headings.

    Written recursively rather than as title -> chapter -> section because the corpora
    are not the same depth: regulations add an agency level between the two.
    """
    listing = search.toc(connection, corpus, title_number)
    container = listing.get("container") or {"number": title_number, "name": ""}
    lines = [
        f"# {CORPUS_TITLES.get(corpus, corpus)} — Title {container['number']}. {container['name']}".rstrip(". "),
        "",
        f"Harvested {_stamp(connection, corpus)}.",
        "",
    ]
    lines += _outline(connection, corpus, listing, depth=2)
    lines += ["", SOURCE_NOTE, ""]
    return "\n".join(lines)


def _outline(connection, corpus, listing, depth):
    """Render one level of the walk, recursing into each child container."""
    lines = []
    if listing["level"] == "sections":
        return _section_lines(listing["items"])

    for child in listing["items"]:
        label = listing["level"].rstrip("s").title()
        lines.append(f"{'#' * depth} {label} {child['number']}. {child['name']}".rstrip(". "))
        lines.append("")
        below = search.toc(connection, corpus, *child["key"].split("/"), include_counts=False)
        lines += _outline(connection, corpus, below, depth + 1)
        lines.append("")

    unplaced = listing.get("unplaced_sections") or []
    if unplaced:
        lines += [f"{'#' * depth} Sections not assigned to a chapter", ""]
        lines += _section_lines(unplaced)
        lines.append("")
    return lines


def _section_lines(sections):
    """Section headings, grouped under their article heading where the service gives one."""
    lines, current_article = [], None
    for section in sections:
        article = (section.get("article_number", ""), section.get("article_name", ""))
        if article != current_article and any(article):
            lines += [f"### Article {article[0]}. {article[1]}".rstrip(". "), ""]
            current_article = article
        flag = "" if section["status"] == "active" else f" [{section['status']}]"
        lines.append(f"- § {section['citation']} — {section['heading']}{flag}")
    return lines


def export(connection, directory, corpus: str = "vacode") -> dict:
    """Write the tiered map to disk. Returns a summary of what was written."""
    directory = Path(directory).expanduser()
    (directory / corpus / "titles").mkdir(parents=True, exist_ok=True)

    root = directory / corpus
    (root / "titles-map.md").write_text(tier0(connection, corpus), encoding="utf-8")

    written = 0
    for item in search.toc(connection, corpus)["items"]:
        number = item["number"]
        # Title numbers contain dots but never a path separator, so they are safe as
        # file names; the slash guard is here because chapter-style keys are not.
        safe = str(number).replace("/", "_")
        (root / "titles" / f"{safe}.md").write_text(tier1(connection, corpus, number), encoding="utf-8")
        written += 1

    (root / "README.md").write_text(_readme(connection, corpus, written), encoding="utf-8")
    return {"directory": str(root), "titles": written}


def _readme(connection, corpus: str, titles: int) -> str:
    counts = db.counts(connection).get(corpus, {})
    return f"""# {CORPUS_TITLES.get(corpus, corpus)} - context map

Generated by `vacode export-context`. {titles} titles, {counts.get('sections', 0)} sections
({counts.get('inactive', 0)} repealed, expired or reserved). Harvested {_stamp(connection, corpus)}.

- `titles-map.md` — every title and its name. Small enough to keep in context; use it
  to pick a title before searching.
- `titles/<number>.md` — that title's chapters, articles and section headings. Load the
  one title a question is about.
- Section text is not exported here: retrieve it with `vacode get <citation>` or
  `vacode search <query>`, which return the official URL and retrieval date with each
  section.

{SOURCE_NOTE}
"""
