"""Turning the service's HTML bodies into indexable text, history, and status.

Section bodies are small fragments of HTML - in a 200-section sample of the Code the
only tags present were <p> and <a>, and the only class was 'sidenote'. Three things
have to come out of them:

  * plain text, because that is what gets indexed and quoted;
  * the credit line (the trailing 'Code 1950, s 18.1-65; 1960, c. 358; ...'), which is
    the closest thing the service offers to an amendment history; and
  * whether the section is still operative, since repealed and expired sections are
    returned by the API as ordinary rows with a body that just says so.

The boilerplate sidenote the service appends to every Code section ("The chapters of
the acts of assembly referenced in the historical citation ...") is dropped: it is
identical on 33,000 sections and would otherwise dominate any full-text ranking.
"""

from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser

BLOCK_TAGS = {
    "p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "blockquote", "section", "article",
}

# Paragraph classes whose content is service furniture rather than law.
DROPPED_CLASSES = {"sidenote"}


class _TextExtractor(HTMLParser):
    """Collect visible text, one entry per block, skipping dropped paragraph classes."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self._current = []
        self._skip_depth = 0
        self._skip_tag = None

    def _flush(self):
        text = re.sub(r"[ \t ]+", " ", "".join(self._current)).strip()
        if text:
            self.blocks.append(text)
        self._current = []

    def handle_starttag(self, tag, attrs):
        if self._skip_depth:
            if tag == self._skip_tag:
                self._skip_depth += 1
            return
        classes = set()
        for name, value in attrs:
            if name == "class" and value:
                classes.update(value.split())
        if classes & DROPPED_CLASSES:
            self._flush()
            self._skip_tag, self._skip_depth = tag, 1
            return
        if tag in BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag):
        if self._skip_depth:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if not self._skip_depth:
                    self._skip_tag = None
            return
        if tag in BLOCK_TAGS:
            self._flush()

    def handle_data(self, data):
        if not self._skip_depth:
            self._current.append(data)

    def close(self):
        super().close()
        self._flush()


def blocks(html: str):
    """The visible paragraphs of a body, in order, with furniture removed."""
    parser = _TextExtractor()
    parser.feed(html or "")
    parser.close()
    return parser.blocks


# A credit line is a run of session-law citations, not prose: 'Code 1950, s 58-32;
# 1960, c. 339; 1984, c. 675.' or '2020, cc. 1215, 1258.' Anchoring on that opening
# shape avoids mistaking a final substantive paragraph for history.
HISTORY_RE = re.compile(
    r"""^(?:Code\s+(?:18|19)\d\d
         |Acts\s+(?:19|20)\d\d
         |(?:19|20)\d\d,\s*(?:c{1,2}\.|Sp\.\s*Sess\.)
        )""",
    re.IGNORECASE | re.VERBOSE,
)

REPEALED_RE = re.compile(r"^\s*repealed\b", re.IGNORECASE)
EXPIRED_RE = re.compile(r"^\s*(?:expired|not\s+set\s+out)\b", re.IGNORECASE)
RESERVED_RE = re.compile(r"^\s*reserved\b", re.IGNORECASE)


def status_of(heading: str, text: str) -> str:
    """Classify a section as active, repealed, expired, or reserved.

    The heading is checked first because the service sets it to exactly 'Repealed'
    for repealed sections; the body is the fallback for the ones where it does not.
    """
    for probe in (heading or "", text or ""):
        if REPEALED_RE.match(probe):
            return "repealed"
        if EXPIRED_RE.match(probe):
            return "expired"
        if RESERVED_RE.match(probe):
            return "reserved"
    return "active"


def parse_body(html: str, heading: str = ""):
    """Split a body into text, credit line, and status.

    Returns a dict with `text` (the operative text, history removed), `history`
    (the credit line, or ''), `status`, and `hash` (a digest of the raw HTML, used to
    skip re-normalizing and re-indexing unchanged sections on a later harvest).
    """
    paragraphs = blocks(html)
    history = ""
    if paragraphs and HISTORY_RE.match(paragraphs[-1]):
        history = paragraphs.pop()
    text = "\n\n".join(paragraphs)
    return {
        "text": text,
        "history": history,
        "status": status_of(heading, text),
        "hash": hashlib.sha256((html or "").encode("utf-8")).hexdigest()[:32],
    }


def snippet(text: str, query_terms, width: int = 320) -> str:
    """A window of text around the first query term, for search result previews.

    Falls back to the head of the section when no term matches the body, which happens
    when the hit came from the heading or the citation.
    """
    flat = re.sub(r"\s+", " ", text or "").strip()
    if not flat:
        return ""
    lowered = flat.lower()
    position = -1
    for term in query_terms:
        term = term.strip().lower()
        if len(term) < 3:
            continue
        found = lowered.find(term)
        if found >= 0 and (position < 0 or found < position):
            position = found
    if position < 0:
        return flat[:width] + ("..." if len(flat) > width else "")
    start = max(0, position - width // 3)
    end = min(len(flat), start + width)
    return ("..." if start else "") + flat[start:end].strip() + ("..." if end < len(flat) else "")
