"""Citation parsing: turning what a person or an agent types into a lookup key.

Citations arrive in every shape - `18.2-51`, `Sec. 18.2-51`, `SS 18.2-51`, `1 VAC
20-10-10`, `Va. Const. art. I, Sec. 8` - and the mirror stores one canonical key per
section so all of those land on the same row. Keys are deliberately lossy: they keep
only what distinguishes one provision from another.
"""

from __future__ import annotations

import re
import unicodedata

# The section sign, its doubled form, and the dash characters that word processors and
# PDFs substitute for a plain hyphen. Normalizing these first is what makes a citation
# pasted out of a brief match one typed at a shell.
_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
_SECTION_WORDS = re.compile(r"^\s*(?:§{1,2}|sec(?:tion|s|t)?\.?|ss\.?)\s*", re.IGNORECASE)

# 'Va. Const. art. I, section 8' and every abbreviation of it people actually write.
_CONSTITUTION = re.compile(
    r"""^\s*(?:va\.?\s*|virginia\s+)?const(?:\.|itution)?\s*
        (?:of\s+virginia\s*)?[,.]?\s*
        art(?:\.|icle)?\s*(?P<article>[ivxl]+|\d+)\s*
        (?:[,.]?\s*(?:§{1,2}|sec(?:tion)?\.?)?\s*(?P<section>[\d.\-a-z]+))?\s*$""",
    re.IGNORECASE | re.VERBOSE,
)

# '1VAC20-10-10', '1 VAC 20-10-10', '1vac20-10-10'.
_ADMIN = re.compile(r"^\s*(?P<title>\d+)\s*vac\s*(?P<rest>[\d\-.:]+)\s*$", re.IGNORECASE)

# '18.2-51', '8.01-581.1', '2.2-4000'. Titles carry a dot; the section follows a hyphen.
_CODE = re.compile(r"^\s*(?P<title>\d+(?:\.\d+[a-z]?|[a-z])?)-(?P<section>[\d.:\-a-z]+)\s*$", re.IGNORECASE)

_ROMAN = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}


def _from_roman(text: str):
    """Roman numeral to int, or None if it is not one. Article numbers are written both ways."""
    text = text.lower()
    if not text or any(ch not in _ROMAN for ch in text):
        return None
    total, previous = 0, 0
    for ch in reversed(text):
        value = _ROMAN[ch]
        total = total - value if value < previous else total + value
        previous = max(previous, value)
    return total


def clean(text: str) -> str:
    """Strip section signs, normalize exotic dashes, and collapse whitespace."""
    text = unicodedata.normalize("NFKC", str(text or "")).translate(_DASHES)
    text = _SECTION_WORDS.sub("", text)
    return re.sub(r"\s+", " ", text).strip().rstrip(".,;")


def constitution_key(article: str, section: str) -> str:
    """The canonical key for a constitutional provision."""
    return f"const-art{article}-{section}" if section else f"const-art{article}"


def admin_key(citation: str) -> str:
    return clean(citation).lower().replace(" ", "")


def normalize_key(text: str):
    """Map a citation in any common form to its stored key, or None if unrecognizable.

    Returning None rather than a best guess matters: a caller that cannot parse the
    citation should fall through to full-text search instead of reporting that a
    perfectly real section does not exist.
    """
    text = clean(text)
    if not text:
        return None

    match = _CONSTITUTION.match(text)
    if match:
        article = match.group("article")
        numeric = _from_roman(article)
        article = str(numeric) if numeric else article.lstrip("0") or article
        return constitution_key(article, (match.group("section") or "").lower())

    match = _ADMIN.match(text)
    if match:
        return f"{match.group('title')}vac{match.group('rest')}".lower()

    match = _CODE.match(text)
    if match:
        return f"{match.group('title')}-{match.group('section')}".lower()

    return None


def looks_like_citation(text: str) -> bool:
    """Whether a query should be answered by exact lookup before full-text search."""
    return normalize_key(text) is not None


# Cross-references inside section bodies are hyperlinked to /vacode/<citation>/, which
# is a far more reliable source of the outbound edges than parsing the prose.
_HREF_CITATION = re.compile(r"/vacode/([^/'\"\s>]+)/?", re.IGNORECASE)


def references_in_html(html: str):
    """Citations this section links to, deduped, in first-appearance order."""
    seen, out = set(), []
    for raw in _HREF_CITATION.findall(html or ""):
        key = normalize_key(raw)
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def code_url(citation: str) -> str:
    """The official law.lis.virginia.gov page for a Code section.

    This short form is the one LIS itself emits in the cross-reference anchors inside
    section bodies (``/vacode/2.2-4000/``), which makes it the safest canonical link:
    the site is a single-page app that answers 200 for any path, so a hand-built
    longer form cannot be verified by fetching it.
    """
    return f"https://law.lis.virginia.gov/vacode/{clean(citation)}/"


def admin_url(title_number: str, agency_number: str, chapter_number: str, section_number: str) -> str:
    """The official page for an Administrative Code section."""
    return (
        "https://law.lis.virginia.gov/admincode/"
        f"title{title_number}/agency{agency_number}/chapter{chapter_number}/section{section_number}/"
    )


def constitution_url(article_number: str, section_number: str) -> str:
    """The official page for a constitutional section."""
    return f"https://law.lis.virginia.gov/constitution/article{article_number}/section{section_number}/"
