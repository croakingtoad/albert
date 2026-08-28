"""Client for the Virginia LIS law API at https://law.lis.virginia.gov/api/.

The two documentation pages the Commonwealth publishes (``/jsonapi/`` and ``/xmlapi/``)
are catalogs, not endpoints: every operation they list actually lives under ``/api/``.
Three behaviours of that service shape this module:

  * Requests without a trailing slash answer 301 to the slashed form, so redirects
    must be followed (urllib does this for GET by default).
  * A citation that does not exist answers **200** with an empty ``ChapterList``
    rather than 404, so callers must inspect the payload, never the status code.
  * The ``...Xml`` operations return JSON anyway, so there is no reason to use them.

Env overrides (all optional):
  VACODE_API_BASE      service root (default: https://law.lis.virginia.gov/api)
  VACODE_USER_AGENT    User-Agent header sent with every request
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = os.environ.get("VACODE_API_BASE", "https://law.lis.virginia.gov/api").rstrip("/")
USER_AGENT = os.environ.get(
    "VACODE_USER_AGENT",
    "vacode/1.0 (+https://github.com/croakingtoad/albert; local mirror of the Code of Virginia)",
)

# The public site is a small IIS deployment; a failed request is far more expensive
# than a slow one, so retries are generous and the backoff is jittered to keep a
# pool of harvest threads from re-colliding in lockstep.
MAX_ATTEMPTS = 5
BACKOFF_BASE = 1.5
TIMEOUT = 60


class ApiError(RuntimeError):
    """A request failed after every retry, or returned something that is not JSON."""


def _url(operation: str, *segments: str) -> str:
    """Build an operation URL, percent-encoding each path segment.

    Segments are quoted with an empty ``safe`` set: chapter numbers legitimately
    contain characters like ``/`` in a handful of titles, and letting those through
    unencoded would silently change which operation is being called.
    """
    parts = [API_BASE, operation]
    parts += [urllib.parse.quote(str(s), safe="") for s in segments]
    return "/".join(parts)


def fetch(operation: str, *segments: str):
    """GET an operation and return the decoded JSON body.

    Retries transient network failures and 5xx responses; raises ApiError once the
    attempts are exhausted. A 4xx is returned to the caller immediately as an error,
    since retrying a malformed path never helps.
    """
    url = _url(operation, *segments)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})

    last_error = None
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            time.sleep(BACKOFF_BASE ** attempt + random.random())
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                raw = response.read()
            break
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code < 500:
                raise ApiError(f"{url} -> HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
    else:
        raise ApiError(f"{url} failed after {MAX_ATTEMPTS} attempts: {last_error}")

    try:
        return json.loads(raw.decode("utf-8-sig"))
    except ValueError as exc:
        # The service answers HTML (its operations catalog) when a path does not match
        # any operation at all, which is a caller bug worth surfacing loudly.
        raise ApiError(f"{url} returned non-JSON ({len(raw)} bytes)") from exc


# --- Code of Virginia -------------------------------------------------------

def titles():
    """All Code of Virginia titles.

    The service returns a few duplicate TitleNumber rows (8.2, 8.2A and 8.4A each
    appear twice), so this dedupes by number while keeping first-seen order.
    """
    seen, out = set(), []
    for row in fetch("CoVTitlesGetListOfJson") or []:
        number = (row.get("TitleNumber") or "").strip()
        if not number or number in seen:
            continue
        seen.add(number)
        out.append({"title_number": number, "title_name": (row.get("TitleName") or "").strip()})
    return out


def chapters(title_number: str):
    """Chapters within one title."""
    payload = fetch("CoVChaptersGetListOfJson", title_number) or {}
    return [
        {
            "title_number": title_number,
            "title_name": (payload.get("TitleName") or "").strip(),
            "chapter_number": (row.get("ChapterNum") or "").strip(),
            "chapter_name": (row.get("ChapterName") or "").strip(),
        }
        for row in (payload.get("ChapterList") or [])
    ]


def sections(title_number: str, chapter_number: str):
    """Section headings within one chapter, flattened out of the article/subpart nesting.

    The payload nests SectionList inside SubPartList inside ArticleList; most chapters
    use a single unnamed article and a single unnamed subpart, so the nesting carries
    real information only sometimes. Flattening keeps the article context on each row.
    """
    payload = fetch("CoVSectionsGetListOfJson", title_number, chapter_number) or {}
    out = []
    for article in payload.get("ArticleList") or []:
        for subpart in article.get("SubPartList") or []:
            for section in subpart.get("SectionList") or []:
                citation = (section.get("SectionNumber") or "").strip()
                if not citation:
                    continue
                out.append(
                    {
                        "citation": citation,
                        "heading": (section.get("SectionTitle") or "").strip(),
                        "title_number": (payload.get("TitleNumber") or title_number).strip(),
                        "title_name": (payload.get("TitleName") or "").strip(),
                        "chapter_number": (payload.get("ChapterNum") or chapter_number).strip(),
                        "chapter_name": (payload.get("ChapterName") or "").strip(),
                        "subtitle_number": (payload.get("SubtitleNum") or "").strip(),
                        "subtitle_name": (payload.get("SubtitleName") or "").strip(),
                        "part_number": (payload.get("PartNum") or "").strip(),
                        "part_name": (payload.get("PartName") or "").strip(),
                        "article_number": (article.get("ArticleNum") or "").strip(),
                        "article_name": (article.get("ArticleName") or "").strip(),
                        "subpart_number": (subpart.get("SubPartNum") or "").strip(),
                        "subpart_name": (subpart.get("SubPartName") or "").strip(),
                    }
                )
    return out


def section_detail(citation: str):
    """Full text of one section, or None if the service does not know the citation.

    A missing citation is an empty ChapterList under a 200, which is why this checks
    the payload rather than trusting the status code.
    """
    payload = fetch("CoVSectionsGetSectionDetailsJson", citation) or {}
    rows = payload.get("ChapterList") or []
    if not rows:
        return None
    row = rows[0]
    return {
        "citation": (row.get("SectionNumber") or citation).strip(),
        "heading": (row.get("SectionTitle") or "").strip(),
        "body_html": row.get("Body") or "",
        "section_range": (row.get("SectionRange") or "").strip(),
        "title_number": (payload.get("TitleNumber") or "").strip(),
        "title_name": (payload.get("TitleName") or "").strip(),
        "chapter_number": (row.get("ChapterNum") or "").strip(),
        "chapter_name": (row.get("ChapterName") or "").strip(),
        "subtitle_number": (row.get("SubtitleNum") or "").strip(),
        "subtitle_name": (row.get("SubtitleName") or "").strip(),
        "part_number": (row.get("PartNum") or "").strip(),
        "part_name": (row.get("PartName") or "").strip(),
        "article_number": (row.get("ArticleNum") or "").strip(),
        "article_name": (row.get("ArticleName") or "").strip(),
        "subpart_number": (row.get("SubPartNum") or "").strip(),
        "subpart_name": (row.get("SubPartName") or "").strip(),
    }


# --- Administrative Code ----------------------------------------------------
#
# Every Administrative Code operation answers the same nested envelope
# (Title -> AgencyList -> ChapterList -> Sections), progressively filled in as the
# path grows more specific. These helpers dig the one level that each operation
# actually populates rather than making callers walk the envelope themselves.

def _admin_dig(payload, *keys):
    """Walk the shared envelope, returning the deepest list the payload filled in."""
    node = payload if isinstance(payload, dict) else {}
    for key in keys:
        rows = node.get(key) or []
        if not rows:
            return []
        node = rows[0] if isinstance(rows[0], dict) else {}
        last = rows
    return last


def admin_titles():
    """All Virginia Administrative Code titles."""
    return [
        {"title_number": str(row.get("TitleNumber") or "").strip(), "title_name": (row.get("TitleName") or "").strip()}
        for row in fetch("AdministrativeCodeGetTitleListOfJson") or []
        if str(row.get("TitleNumber") or "").strip()
    ]


def admin_agencies(title_number: str):
    """Agencies promulgating regulations under one VAC title."""
    payload = fetch("AdministrativeCodeGetAgencyListOfJson", title_number) or {}
    return [
        {
            "title_number": title_number,
            "title_name": (payload.get("TitleName") or "").strip(),
            "agency_number": str(row.get("AgencyNumber") or "").strip(),
            "agency_name": (row.get("AgencyName") or "").strip(),
        }
        for row in (payload.get("AgencyList") or [])
        if str(row.get("AgencyNumber") or "").strip()
    ]


def admin_chapters(title_number: str, agency_number: str):
    """Chapters within one agency's regulations."""
    payload = fetch("AdministrativeCodeChapterListOfJson", title_number, agency_number) or {}
    return [
        {
            "title_number": title_number,
            "agency_number": agency_number,
            "chapter_number": str(row.get("ChapterNumber") or "").strip(),
            "chapter_name": (row.get("ChapterName") or "").strip(),
        }
        for row in _admin_dig(payload, "AgencyList", "ChapterList")
        if str(row.get("ChapterNumber") or "").strip()
    ]


def admin_sections(title_number: str, agency_number: str, chapter_number: str):
    """Section headings within one VAC chapter.

    The list operation leaves Body null; only AdministrativeCodeGetSectionDetailsJson
    fills it. Citations are assembled into the form the Commonwealth actually prints
    (``1VAC20-10-10``) so they can be looked up the way a person would write them.
    """
    payload = fetch("AdministrativeCodeGetSectionListOfJson", title_number, agency_number, chapter_number) or {}
    out = []
    for row in _admin_dig(payload, "AgencyList", "ChapterList", "Sections"):
        number = str(row.get("SectionNumber") or "").strip()
        if not number:
            continue
        out.append(
            {
                "citation": f"{title_number}VAC{agency_number}-{chapter_number}-{number}",
                "section_number": number,
                "heading": (row.get("SectionTitle") or "").strip(),
                "title_number": title_number,
                "title_name": (payload.get("TitleName") or "").strip(),
                "agency_number": agency_number,
                "chapter_number": chapter_number,
                "part_number": (row.get("PartNumber") or "").strip(),
                "part_name": (row.get("PartName") or "").strip(),
                "article_number": (row.get("ArticleNumber") or "").strip(),
                "article_name": (row.get("ArticleName") or "").strip(),
            }
        )
    return out


def split_admin_section_number(number: str):
    """Split a VAC section number into the (number, point, colon) triple the API wants.

    AdministrativeCodeGetSectionDetailsJson takes the three components as separate path
    segments and expects a literal ``0`` for any component the section does not use, so
    ``10`` becomes ``(10, 0, 0)``, ``10.5`` becomes ``(10, 5, 0)`` and ``10:1`` becomes
    ``(10, 0, 1)``.
    """
    number = str(number).strip()
    colon = "0"
    if ":" in number:
        number, _, colon = number.partition(":")
        colon = colon or "0"
    point = "0"
    if "." in number:
        number, _, point = number.partition(".")
        point = point or "0"
    return number or "0", point, colon


def admin_section_detail(title_number: str, agency_number: str, chapter_number: str, section_number: str):
    """Full text of one VAC section, or None if the service does not know it."""
    number, point, colon = split_admin_section_number(section_number)
    payload = fetch(
        "AdministrativeCodeGetSectionDetailsJson",
        title_number, agency_number, chapter_number, number, point, colon,
    ) or {}
    rows = _admin_dig(payload, "AgencyList", "ChapterList", "Sections")
    if not rows:
        return None
    row = rows[0]
    return {
        "citation": f"{title_number}VAC{agency_number}-{chapter_number}-{section_number}",
        "heading": (row.get("SectionTitle") or "").strip(),
        "body_html": row.get("Body") or "",
        "authority": (row.get("Authority") or "").strip(),
        "history": (row.get("HistoricalNote") or "").strip(),
        "part_number": (row.get("PartNumber") or "").strip(),
        "part_name": (row.get("PartName") or "").strip(),
        "article_number": (row.get("ArticleNumber") or "").strip(),
        "article_name": (row.get("ArticleName") or "").strip(),
    }


# --- Constitution -----------------------------------------------------------
#
# Constitution operations answer one envelope per article with a Sections list, and
# are the only part of the service that carries a LastUpdate timestamp.

def constitution_articles():
    """The articles of the Constitution of Virginia."""
    return [
        {
            "article_number": str(row.get("ArticleNumber") or "").strip(),
            "article_label": (row.get("Article") or "").strip(),
            "article_name": (row.get("ArticleName") or "").strip(),
        }
        for row in fetch("ConstitutionArticlesGetListOfJson") or []
        if str(row.get("ArticleNumber") or "").strip()
    ]


def constitution_sections(article_number: str):
    """Section headings within one article."""
    payload = fetch("ConstitutionSectionsGetListOfXml", article_number) or {}
    return [
        {
            "article_number": str(payload.get("ArticleNumber") or article_number).strip(),
            "article_label": (payload.get("Article") or "").strip(),
            "article_name": (payload.get("ArticleName") or "").strip(),
            "section_number": str(row.get("SectionNumber") or "").strip(),
            "section_label": (row.get("Section") or "").strip(),
            "heading": (row.get("SectionName") or "").strip(),
            "last_update": (row.get("LastUpdate") or "").strip(),
        }
        for row in (payload.get("Sections") or [])
        if str(row.get("SectionNumber") or "").strip()
    ]


def constitution_section_detail(article_number: str, section_number: str):
    """Full text of one constitutional section, or None if it does not exist."""
    payload = fetch("ConstitutionSectionDetailsJson", article_number, section_number) or {}
    rows = payload.get("Sections") or []
    if not rows:
        return None
    row = rows[0]
    return {
        "citation": f"Va. Const. art. {article_number}, \u00a7 {section_number}",
        "article_number": str(payload.get("ArticleNumber") or article_number).strip(),
        "article_label": (payload.get("Article") or "").strip(),
        "article_name": (payload.get("ArticleName") or "").strip(),
        "section_number": str(row.get("SectionNumber") or section_number).strip(),
        "section_label": (row.get("Section") or "").strip(),
        "heading": (row.get("SectionName") or "").strip(),
        "body_html": row.get("Body") or "",
        "last_update": (row.get("LastUpdate") or "").strip(),
    }
