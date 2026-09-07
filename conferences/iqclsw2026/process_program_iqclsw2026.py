#!/usr/bin/env python3
# MIT License
#
# Copyright (c) 2026 David Burghoff <burghoff@utexas.edu>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""process_program_iqclsw2026.py — turn the program text into the
clean, source-agnostic conference_data.json that build_conference_app.py wants.

Input  (data/detailed_program.txt, written by fetch_program_iqclsw2026.py):
    The visible text of the single detailed-program page. It is organized as:

        ◊ Monday 29 June
        14:30-15:45 — <Talk Title>
        <Authors line, optionally with superscript affiliation markers>
        <Affiliation line(s): "1. Inst (Country) ; 2. Inst (Country)" or a
                              single unnumbered institution>
        ...
        LIST OF POSTERS
        • <Poster Title>
        <Authors>
        <Affiliations>
        ...

Output (conference_data.json, beside this script):
    The schema documented in build_conference_app.py:
      conference_name, sessions[], talks[], session_types[], talk_types[],
      affiliation_sources[] (one flat, de-duplicated list of raw affiliation
      strings).

Design notes for THIS conference:

  * The conference is a small single-track school/workshop: there are no parallel
    rooms, no presiders, no paper numbers, and no per-talk abstracts on the
    page. Every value the builder reads but the source doesn't carry is emitted
    as a well-typed empty (""/[]/false) so the app renders gracefully.

  * Day -> calendar date. The page gives weekday + day-of-month + month name
    (e.g. "Monday 29 June"); the year is 2026. We turn each timed entry's
    "HH:MM-HH:MM" plus its day into ISO start_ts/end_ts. The Friday gala block
    "15:30-00:00" is treated as ending at midnight of the SAME calendar day
    (00:00 < 15:30), which is fine for ordering — it sorts last that day.

  * We model each DAY as one session (a single-track day), and every academic
    talk that day becomes a talk under it. That gives the app its natural
    "tap a day, see its talks" structure without inventing a session hierarchy
    the source doesn't have. Non-academic blocks (meals, coffee breaks, the
    poster *sessions*, opening/closing, receptions, dinners) are NOT emitted as
    talks; they're house-keeping, and the app is about the science. The whole
    LIST OF POSTERS becomes its own "Posters" session, one talk per poster.

  * Color/type. Talks are classified into the standard type registry below:
      - school-phase lectures                        -> "fuchsia" (Tutorial)
      - workshop-phase invited lectures              -> "indigo"  (Invited)
      - short contributed talks                      -> "sky"     (Contributed)
      - posters                                      -> "teal"    (Poster)
    The heuristic: 30+ minute slots are invited/tutorial lectures, shorter
    slots are contributed. School sessions are Tutorial (fuchsia), Workshop
    sessions are Technical (blue); house-keeping dividers are Event (rose).

  * Affiliation sources. Every institution name we parse is pooled into the
    flat affiliation_sources list so build_affiliation_map.py can learn short
    forms. (This program has no presiders and no full-address lines.)

Run directly:  python process_program_iqclsw2026.py
(or let make_app.py run it for you).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"
# The fetch script saves only the raw page HTML (the source of record); the
# processor extracts the text it needs from these with lxml (see _html_to_text).
HTML_IN = DATA_DIR / "detailed_program.html"
OVERVIEW_HTML_IN = DATA_DIR / "program_overview.html"   # session names (optional)
# Back-compat: if a pre-extracted .txt is present (older pipeline), use it.
TEXT_IN = DATA_DIR / "detailed_program.txt"
OVERVIEW_IN = DATA_DIR / "program_overview.txt"
# OPTIONAL, manually-supplied: the organizers' book of abstracts. When
# present, the processor pulls per-talk short abstracts out of it and joins
# them by title; when absent, talks are emitted without abstracts.
ABSTRACT_BOOK_IN = DATA_DIR / "iqclsw2026-book-of-abstracts.pdf"
JSON_OUT = SCRIPT_DIR / "conference_data.json"

# Poster day + board order from the PRINTED program. The book of abstracts lists
# every poster in one alphabetical catalog and records neither which evening a
# poster is shown nor its board number — that is only on the printed sheet handed
# out on site, and no online/digital source carries it. We therefore pin the
# order here, but store only a HASH of each title (not the title text), so no
# program content lives in this source — see AGENTS.md ("Hashed ordering for
# one-offs"). Each inner list is one evening session in printed BOARD order; the
# sessions are in chronological order (earliest evening first). A hash is
# _poster_title_hash(title). When the parsed catalog doesn't match this table
# one-to-one, the processor falls back to splitting the list in half.
POSTER_BOARD_ORDER = [
    # Poster Session I — Tuesday 30 June 2026
    ["a2118ff964", "e2f871a695", "438a71ae79", "8b7ef8f97f", "d2f27abb3c", "7c019424b7",
     "80c21191bf", "02a2caabc6", "190fee77b1", "c7868ba2c0", "8881c802c6", "51f041aebf",
     "c00a485e41", "5d4ea4a13c", "9a5a09b9cf", "b327d293c7", "f136e7286b", "272b68e2d8",
     "e766fab62d", "0f6c7d5d59", "58ff5f9edd", "795eeb6cbe", "23e8478ad5", "0d534322d0"],
    # Poster Session II — Thursday 2 July 2026
    ["3a97b6c6ac", "350959498e", "c1f3e26be6", "25bd4b977f", "01c1a0f7c4", "ccb85b35b3",
     "022730f33b", "1e53ff180b", "2df555b7c5", "641a5110af", "347a27011e", "f9867065bc",
     "f5669905c3", "56bfe0b662", "0e2807c43d", "cbd9381605", "3aa890143f", "6f85223f1b",
     "4f6680fffc", "6c7c2b6ce7", "fcf115ecf8", "3f7e718036", "38ae527a08", "4335ae9427"],
]

PROGRAM_MARKER = "DETAILED PROGRAM"
POSTER_MARKER = "LIST OF POSTERS"
OVERVIEW_MARKER = "PROGRAM AT A GLANCE"

CONFERENCE_NAME = "IQCLSW 2026"
YEAR = 2026


# -----------------------------------------------------------------------------
# HTML → text extraction. The fetch script saves only the raw page HTML; we
# render it to the line-structured plain text the parsers below expect, using
# lxml. The key is to emit a newline at every BLOCK-level boundary (and <br>),
# while keeping inline runs (span/a/em/sup…) joined — this reproduces the
# logical line breaks the program/overview parsers rely on (each timed block,
# author line and affiliation line on its own line) without the inline
# fragmentation a naive get_text() would cause.
# -----------------------------------------------------------------------------
_BLOCK_TAGS = {
    "div", "p", "section", "article", "tr", "table", "thead", "tbody",
    "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6", "header", "footer",
    "figure", "figcaption", "blockquote", "br",
}

# Sentinel injected by _html_to_text immediately before an underlined text run.
# The source program marks the PRESENTING author by underlining their name
# (`<span style="text-decoration: underline">`), so this sentinel is how that
# signal survives the HTML->text flattening: the author parser uses it to set
# speaker_pos, then strips it. A C0 control char so it can never collide with
# real program text and is trivially removed everywhere a name/title is cleaned.
_SPEAKER_MARK = "\x02"


def _is_underline(el) -> bool:
    """True if an element carries an inline underline style — the source's way
    of marking the presenting author within an author list."""
    style = (el.get("style") or "").lower()
    return "underline" in style and "text-decoration" in style


def _html_to_text(html: str, marker: str, end_marker: str | None = None) -> str:
    """Extract the page's content region as line-structured text.

    `marker` anchors the region of interest (e.g. "DETAILED PROGRAM"); the
    returned text starts at the LAST line containing it (the page chrome repeats
    the title in nav menus, so the last occurrence is the real content heading).
    `end_marker`, when given, makes the region selector walk up to the smallest
    ancestor that contains BOTH markers — needed for the detailed program, whose
    schedule and "LIST OF POSTERS" catalog live in sibling containers under one
    section.
    """
    import lxml.html

    doc = lxml.html.fromstring(html)
    for bad in doc.xpath("//script|//style|//noscript|//nav|//header|//footer"):
        bad.getparent().remove(bad)

    # Pick the region: start at the heading whose own text IS the marker, then
    # walk up to the smallest ancestor that contains end_marker (if given) or is
    # comfortably larger than just the heading.
    heads = [el for el in doc.iter()
             if isinstance(el.tag, str)
             and (el.text or "").strip().lower() == marker.lower()]
    if heads:
        node = heads[-1]
        while node.getparent() is not None:
            tc = node.text_content().lower()
            if end_marker:
                if end_marker.lower() in tc:
                    break
            elif len(node.text_content().strip()) >= 400:
                break
            node = node.getparent()
    else:
        # Fallback: smallest element whose text_content contains the marker.
        cands = [el for el in doc.iter()
                 if isinstance(el.tag, str)
                 and marker.lower() in (el.text_content() or "").lower()]
        node = min(cands, key=lambda e: len(e.text_content())) if cands else doc

    parts: list[str] = []

    def _walk(el, u_depth: int = 0) -> None:
        el_underlined = _is_underline(el)
        if el.tag == "br":
            parts.append("\n")
        # Emit the speaker sentinel just before an underlined run's text, but
        # only at the OUTERMOST underline (u_depth == 0) so a nested span can't
        # double-mark the same name. The marker precedes the name; the element's
        # own tail (the rest of the author list after </span>) stays unmarked.
        if el_underlined and u_depth == 0:
            parts.append(_SPEAKER_MARK)
        if el.text and el.text.strip():
            parts.append(re.sub(r"[ \t\r\n]+", " ", el.text))
        child_depth = u_depth + (1 if el_underlined else 0)
        for ch in el:
            if not isinstance(ch.tag, str):   # comment / processing instruction
                if ch.tail and ch.tail.strip():
                    parts.append(re.sub(r"[ \t\r\n]+", " ", ch.tail))
                continue
            blk = ch.tag in _BLOCK_TAGS
            if blk:
                parts.append("\n")
            _walk(ch, child_depth)
            if blk:
                parts.append("\n")
            if ch.tail and ch.tail.strip():
                parts.append(re.sub(r"[ \t\r\n]+", " ", ch.tail))

    _walk(node)
    text = "".join(parts)
    # Ensure a space after the "HH:MM-HH:MM —" time separator (inline spans
    # sometimes butt the dash against the title), and drop NBSPs.
    text = re.sub(r"(\d{2}:\d{2}-\d{2}:\d{2})\s*—\s*", r"\1 — ", text)
    text = text.replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", l).strip() for l in text.split("\n")]
    lines = [l for l in lines if l]
    # Trim page chrome before the real content heading (last marker occurrence).
    idxs = [k for k, l in enumerate(lines) if marker.lower() in l.lower()]
    if idxs:
        lines = lines[idxs[-1]:]
    return "\n".join(lines)


def _load_program_text() -> str:
    """The detailed-program text: from the saved HTML (preferred), else a
    pre-extracted .txt for back-compat with the older pipeline."""
    if HTML_IN.exists():
        return _html_to_text(HTML_IN.read_text(encoding="utf-8"),
                             PROGRAM_MARKER, POSTER_MARKER)
    if TEXT_IN.exists():
        return TEXT_IN.read_text(encoding="utf-8")
    raise SystemExit(
        f"[process] ERROR: missing input — expected {HTML_IN.name} (or "
        f"{TEXT_IN.name}) in data/. Run fetch_program_iqclsw2026.py first "
        "(or via make_app.py).")


def _load_overview_text() -> str:
    """The overview text (optional): from saved HTML, else a .txt, else ''."""
    if OVERVIEW_HTML_IN.exists():
        return _html_to_text(OVERVIEW_HTML_IN.read_text(encoding="utf-8"),
                             OVERVIEW_MARKER)
    if OVERVIEW_IN.exists():
        return OVERVIEW_IN.read_text(encoding="utf-8")
    return ""


# -----------------------------------------------------------------------------
# Book-of-abstracts parser (OPTIONAL input)
#
# The organizers circulate a PDF "book of abstracts" that's not published on
# the website. When the file is present in data/ (it has to be supplied
# manually — see data_requirements), we extract per-talk SHORT ABSTRACTS from
# it and attach them by title to the matching talks. When the file is absent,
# every talk's abstract stays "" and the rest of the pipeline runs unchanged.
#
# Strategy: walk pages, identify first-page-of-a-talk by font size (title
# runs are visibly larger than body), confirm the page carries a real
# header block (an email / "Contact Email" / numbered affiliation line —
# front-matter pages don't), then look for an explicit "Abstract" or
# "Short Abstract" label. Take the labelled content up to the next section
# header. We intentionally require an explicit label rather than trying to
# slice off the "first paragraph before Introduction" — the latter produces
# dirty output for pages with no clean structural cue.
# -----------------------------------------------------------------------------
_ABS_TITLE_FONT_MIN = 13.0   # title-font cutoff in pt
_ABS_BODY_FONT_MIN  = 9.0    # below this is usually a superscript / footnote
_ABS_REF_FONT_MIN   = 7.5    # reference text never falls below this; smaller
                             # lines in the reference region are figure axis
                             # labels / sub-panel junk on a spill page
_ABS_LINE_TOL = 2.5          # word-Δ within which two words share a line

_ABS_LABEL_RE = re.compile(
    r"^\s*(Short\s*Abstract|Abstract)\s*[:\-–]?\s*(.*)$", re.IGNORECASE)
# Mid-line label: the separator is REQUIRED here (unlike the line-start form
# above). Without it, the ordinary English word "abstract" inside body prose
# ("... discussed in this abstract. Since ...") matches and hijacks the
# abstract start deep into the paper body. A genuine inline label is always
# "Abstract: ..." / "Short Abstract – ...".
_ABS_LABEL_INLINE_RE = re.compile(
    r"\b(Short\s*Abstract|Abstract)\s*[:\-–]\s*(.+)$", re.IGNORECASE)
_ABS_PLACEHOLDER_RE = re.compile(
    r"^Brief\s+summary\s*\(.*characters?\s+maximum\)\.?\s*$",
    re.IGNORECASE)
_ABS_SECTION_KEYWORDS = (
    # "Abstract" as a section terminator catches the case where the page
    # has BOTH a "Short Abstract" up top AND a separate longer "Abstract"
    # section below: we want only the Short Abstract, so we stop the
    # collector when the second "Abstract" header appears. (The initial
    # label match already consumed the original "Short Abstract" /
    # "Abstract:" line — the collector starts AFTER it.)
    "Abstract",
    "Introduction", "Results", "Methods", "Method", "Discussion",
    "Conclusion", "Conclusions", "Summary", "Theory", "Background",
    "Context", "Experiment", "Experiments", "Measurements", "Setup",
    "Approach", "Motivation", "Overview",
)
_ABS_SECTION_KW_RE = re.compile(
    r"^\s*\d{0,2}\.?\s*(" + "|".join(_ABS_SECTION_KEYWORDS) + r")\b",
    re.IGNORECASE)
# Same keywords but used as an INLINE marker like "Introduction: <body…>" —
# the body starts on the same line as the header word, no length cap.
_ABS_SECTION_INLINE_RE = re.compile(
    r"^\s*\d{0,2}\.?\s*(" + "|".join(_ABS_SECTION_KEYWORDS) + r")\s*[:\-–]",
    re.IGNORECASE)
# Generic numbered section header — "1. <Title>" / "2.III-V growth on …".
# We require: 1-2 digit number, period, optional space, then a Capital
# starting a short title line that does NOT end with a sentence period
# in the middle. Catches author-defined sections that aren't in the
# keyword list above.
_ABS_NUMBERED_SECTION_RE = re.compile(
    r"^\s*\d{1,2}\.\s*[A-Z][A-Za-z0-9\-–—’ '().,&/]{0,90}$"
)
# Talk-body STOP markers: References / Acknowledg(e)ments / Bibliography.
# When we hit one of these we end the abstract — they always come at the
# very end of a paper-style writeup and what follows is bibliography
# entries, not useful prose.
_ABS_STOP_RE = re.compile(
    r"^\s*(?:\d{0,2}\.?\s*)?(References?|Acknowledg(?:e?ments?)?|"
    r"Bibliograph(?:y|ie|ies)|Funding|Author\s+contributions?|"
    r"Competing\s+interests?|Data\s+availability)\b",
    re.IGNORECASE)
# Figure/table caption start: "Figure 1.", "Fig. 1:", "Fig 1A)", "Table 2-".
# Used both to drop caption paragraphs from the abstract body and to stop
# reference collection when a caption (or the figure block) follows the
# reference list on a spill page.
_ABS_CAPTION_RE = re.compile(
    r"^(?:Figure|Fig\.?|Table|Tab\.?)\s*\d+[A-Za-z]?\s*[:.\-–—)]",
    re.IGNORECASE)
# Inline "1.Introduction" / "2.Results" pattern pdfplumber sometimes merges
# with the previous sentence. Used to TRIM the final string. We require a
# 1–2 digit section number (so "2025." is never matched) AND a known
# section keyword.
_ABS_INLINE_SECTION_RE = re.compile(
    r"(?:^|(?<=[.!?]))\s*\d{1,2}\.\s*(" + "|".join(_ABS_SECTION_KEYWORDS)
    + r")\b",
    re.IGNORECASE)
# Strong "this is a talk page" signal: an email, "Contact Email", or a
# numbered affiliation line that begins with a digit followed by a clearly
# institution-like word.
_ABS_SIG_RE = re.compile(
    r"(\bContact\s*Email\b|@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"
    r"|^\s*\d+\s*[A-Za-z\xc0-\xff][a-zA-Z\xc0-\xff\s,.\-]+(University|"
    r"Institute|Laboratoire|Laboratory|CNRS|Dipartimento|Politecnico|"
    r"Department|Centre|Center|National|Research|Academy|TU\s|ETH\s|CEA|"
    r"CNR|GmbH))",
    re.IGNORECASE)


def _abs_page_lines(page) -> list[tuple[float, list[dict], float, float]]:
    """Cluster a PDF page's words into top-down lines. Returns
    [(max_font_size, words, top, x0), …] where x0 is the leftmost word
    position on the line.

    Uses x_tolerance=2.0 (default is 3.0) so consecutive characters
    separated by a 2.49 pt positional gap — what some LaTeX templates
    in this book use as a space — are recognized as a word boundary.
    With the default, those gaps fall under the threshold and pdfplumber
    glues words together, giving runs like "extendednonlinearliquid…"."""
    words = page.extract_words(extra_attrs=["size"], x_tolerance=2.0) or []
    for w in words:
        w["top"] = float(w["top"])
        w["x0"] = float(w["x0"])
        w["x1"] = float(w["x1"])
        w["size"] = float(w.get("size", 0) or 0)
    words.sort(key=lambda w: (w["top"], w["x0"]))
    lines: list[list[dict]] = []
    for w in words:
        if lines and abs(w["top"] - lines[-1][0]["top"]) <= _ABS_LINE_TOL:
            lines[-1].append(w)
        else:
            lines.append([w])
    out: list[tuple[float, list[dict], float, float]] = []
    for ln in lines:
        ln.sort(key=lambda w: w["x0"])
        out.append((max(w["size"] for w in ln), ln,
                    min(w["top"] for w in ln),
                    min(w["x0"] for w in ln)))
    return out


# Adobe Symbol-font glyph codes land in the Unicode Private Use Area
# (U+F020–U+F0FF) when a PDF embeds the Symbol font without a ToUnicode
# CMap. PDF readers map these to Greek letters and math symbols via the
# font itself, so the document looks correct on screen, but pdfplumber
# returns the raw PUA codepoints — which have no glyph in browser fonts
# and render as a missing-glyph box. The table below translates the
# common Symbol-font slots into their proper Unicode equivalents (e.g.
# U+F06D — Symbol's "m" — to μ U+03BC).
_SYMBOL_PUA_TO_UNICODE = {
    # Greek lowercase
    0xF061: "α", 0xF062: "β", 0xF063: "χ", 0xF064: "δ", 0xF065: "ε",
    0xF066: "φ", 0xF067: "γ", 0xF068: "η", 0xF069: "ι", 0xF06A: "ϕ",
    0xF06B: "κ", 0xF06C: "λ", 0xF06D: "μ", 0xF06E: "ν", 0xF06F: "ο",
    0xF070: "π", 0xF071: "θ", 0xF072: "ρ", 0xF073: "σ", 0xF074: "τ",
    0xF075: "υ", 0xF076: "ϖ", 0xF077: "ω", 0xF078: "ξ", 0xF079: "ψ",
    0xF07A: "ζ",
    # Greek uppercase
    0xF041: "Α", 0xF042: "Β", 0xF043: "Χ", 0xF044: "Δ", 0xF045: "Ε",
    0xF046: "Φ", 0xF047: "Γ", 0xF048: "Η", 0xF049: "Ι", 0xF04B: "Κ",
    0xF04C: "Λ", 0xF04D: "Μ", 0xF04E: "Ν", 0xF04F: "Ο", 0xF050: "Π",
    0xF051: "Θ", 0xF052: "Ρ", 0xF053: "Σ", 0xF054: "Τ", 0xF055: "Υ",
    0xF057: "Ω", 0xF058: "Ξ", 0xF059: "Ψ", 0xF05A: "Ζ",
    # Math and punctuation
    0xF0A3: "≤", 0xF0A5: "∞", 0xF0B0: "°", 0xF0B1: "±", 0xF0B2: "″",
    0xF0B3: "≥", 0xF0B4: "×", 0xF0B5: "∝", 0xF0B6: "∂", 0xF0B7: "·",
    0xF0B8: "÷", 0xF0B9: "≠", 0xF0BA: "≡", 0xF0BB: "≈", 0xF0BC: "…",
    0xF0D7: "·", 0xF0D6: "√", 0xF0D5: "∏", 0xF0E5: "∑", 0xF0F2: "∫",
    0xF0AE: "→", 0xF0AC: "←", 0xF0AD: "↑", 0xF0AF: "↓",
    0xF0DE: "⇒", 0xF0DC: "⇐", 0xF0DD: "⇑", 0xF0DF: "⇓", 0xF0DB: "⇔",
    0xF0CE: "∈", 0xF0CF: "∉", 0xF0C7: "∩", 0xF0C8: "∪",
    0xF020: " ",
}


def _symbol_pua_to_unicode(text: str) -> str:
    if not text:
        return text
    # Fast-path most lines (no PUA codepoints at all).
    if not any(0xF020 <= ord(c) <= 0xF0FF for c in text):
        return text
    return text.translate(_SYMBOL_PUA_TO_UNICODE)


def _abs_line_text(words: list[dict]) -> str:
    raw = re.sub(r"\s+", " ",
                 " ".join(w["text"] for w in words)).strip()
    return _symbol_pua_to_unicode(raw)


_ABS_INSTITUTION_WORDS_RE = re.compile(
    r"\b(University|Institute|Institut|Laboratoire|Laboratory|CNRS|"
    r"Dipartimento|Politecnico|Department|Centre|Center|Facult|National|"
    r"Research|Academy|Academia|Max\s+Planck|TU\s|ETH\s|CEA|CNR|GmbH|"
    r"S\.\s*A\.?|Inc\.?|Ltd\.?)",
    re.IGNORECASE)


def _abs_is_header_line(text: str) -> bool:
    """True for an author / affiliation / contact-email line — i.e. anything
    in the block between the title and the abstract. Used in the no-label
    fallback to skip the header without dropping abstract content."""
    if not text:
        return True
    if re.search(r"\bContact\s*Email\b|ContactEmail|@[A-Za-z0-9.\-]+"
                 r"\.[A-Za-z]{2,}", text, re.IGNORECASE):
        return True
    if text[:1] in "*†‡§¶":
        return True
    if re.match(r"^\s*\d", text):
        return True
    # Section labels squished into one word (e.g. "ShortAbstract" with no
    # space) — these are LABELS, not body, so treat as header.
    if re.match(r"^\s*(Short\s*Abstract|Abstract)\s*$", text, re.IGNORECASE):
        return True
    if _ABS_INSTITUTION_WORDS_RE.search(text):
        return True
    if len(text) <= 50:
        return True
    toks = text.split()
    if 1 <= len(toks) <= 6 and len(text) < 80:
        is_name = lambda t: bool(
            re.match(r"^[A-ZÀ-Ý][A-Za-zÀ-ÿ'’.\-]*$", t)
            or re.match(r"^\d+[*†‡§]?$", t)
            or t in ("and", "&"))
        if all(is_name(t) for t in toks):
            return True
    upper_tokens = sum(1 for t in toks
                       if t[:1].isupper() and len(t) > 1)
    if text.count(",") >= 2 and upper_tokens >= 3 and len(text) < 250:
        return True
    # Squished author roster (LaTeX template strips inter-word spaces):
    # the line has many CamelCase tokens (lower→upper boundary inside a
    # word, e.g. "JayaprasathElumalai") AND commas, but no spaces around
    # the commas so the upper_tokens count above misses it. Detect by
    # counting embedded camelCase boundaries.
    camel = len(re.findall(r"[a-z][A-Z]", text))
    if camel >= 3 and text.count(",") >= 2 and len(text) < 400:
        return True
    return False


_CITE_RE = re.compile(r"\[\s*(\d{1,3}(?:\s*[,\-–—]\s*\d{1,3})*)\s*\]")


def _cited_ref_numbers(text: str) -> set[int]:
    """Return the set of reference numbers cited anywhere in `text`.
    Handles [1], [1,2], [1, 2], [1-3], [1–3], [1, 3-5] — and expands
    ranges to every covered integer."""
    out: set[int] = set()
    for m in _CITE_RE.finditer(text):
        body = m.group(1)
        # Split on commas; each piece is either a single number or a
        # range "lo-hi" with optional hyphen / en-dash / em-dash.
        for piece in body.split(","):
            piece = piece.strip()
            rm = re.match(r"^(\d+)\s*[\-–—]\s*(\d+)$", piece)
            if rm:
                lo, hi = int(rm.group(1)), int(rm.group(2))
                if 0 < lo <= hi and hi - lo < 50:   # sanity cap
                    out.update(range(lo, hi + 1))
            elif piece.isdigit():
                n = int(piece)
                if 0 < n < 1000:
                    out.add(n)
    return out


def _parse_reference_entries(ref_lines: list[str]) -> dict[int, str]:
    """Parse a flat list of reference lines into {N: entry_text}. Each
    entry starts with `[N]` (sometimes `N.`) and may span multiple
    wrapped lines. Continuation lines are joined onto the current
    entry with a single space."""
    entries: dict[int, str] = {}
    cur_num: int | None = None
    cur_parts: list[str] = []
    start_re = re.compile(r"^\s*\[\s*(\d+)\s*\]\s*(.*)$")
    alt_start_re = re.compile(r"^\s*(\d{1,3})\.\s+(.+)$")

    def _flush():
        nonlocal cur_num, cur_parts
        if cur_num is not None and cur_parts:
            txt = re.sub(r"\s+", " ", " ".join(cur_parts)).strip()
            if txt:
                entries[cur_num] = txt
        cur_num = None
        cur_parts = []

    for ln in ref_lines:
        m = start_re.match(ln)
        if not m:
            m = alt_start_re.match(ln)
        if m:
            _flush()
            cur_num = int(m.group(1))
            cur_parts = [m.group(2)]
        elif cur_num is not None:
            cur_parts.append(ln.strip())
    _flush()
    return entries


def _book_paper_start_title(lines) -> tuple[str, int] | None:
    """If `lines` is the start of a paper page in the book, return
    (title, first_post_title_line_index). Otherwise None.

    Detection: the page must lead with one or more consecutive title-font
    lines, and within the next ~12 lines carry a "talk page signature" —
    a Contact Email marker, an email address, a numbered affiliation, or
    a line that contains an institution keyword. Front-matter pages
    (Welcome, Committees, Scientific Program, …) lead with the same
    large font but lack the signature."""
    if not lines:
        return None
    first_size = lines[0][0]
    if first_size < _ABS_TITLE_FONT_MIN:
        return None
    title_parts: list[str] = []
    i = 0
    while i < len(lines) and lines[i][0] >= _ABS_TITLE_FONT_MIN:
        title_parts.append(_abs_line_text(lines[i][1]))
        i += 1
    title = re.sub(r"\s+", " ", " ".join(title_parts)).strip()
    if len(title) < 8:
        return None
    sig_seen = False
    for k in range(i, min(i + 12, len(lines))):
        size, words = lines[k][0], lines[k][1]
        if size >= _ABS_TITLE_FONT_MIN:
            break
        text = _abs_line_text(words)
        if (re.search(r"\bContact\s*Email\b|ContactEmail|@[A-Za-z0-9.\-]+"
                      r"\.[A-Za-z]{2,}", text, re.IGNORECASE)
                or re.match(r"^\s*\d+\s*[A-Za-zÀ-Ý]", text)
                or _ABS_INSTITUTION_WORDS_RE.search(text)):
            sig_seen = True
            break
    if not sig_seen:
        return None
    return title, i


def _book_page_is_poster(lines) -> bool:
    """Return True when a paper-start page is actually a POSTER laid out
    as a 2-D collage of panels rather than a single-column abstract. Such
    pages have no readable linear "abstract"; the extracted text is just
    scattered slide fragments, so we emit the talk with an empty abstract
    (the poster PDF itself is still attached and viewable).

    Two independent tells, each well clear of any normal abstract page:
      - rotated / overflow text blocks, which pdfplumber reports at a
        NEGATIVE x0 (a single-column page never has any); or
      - a very wide horizontal spread of text combined with many tiny
        fragment lines (panels and captions strewn across the width)."""
    body = [(s, x0) for (s, _w, _t, x0) in lines
            if 5 <= s < _ABS_TITLE_FONT_MIN]
    if len(body) < 8:
        return False
    xs = [x0 for _s, x0 in body]
    n_neg = sum(1 for x in xs if x < 0)
    span = max(xs) - min(xs)
    n_tiny = sum(1 for s, _x in body if s < 7.5)
    if n_neg >= 2:
        return True
    if span > 500 and n_tiny >= 10:
        return True
    return False


def _parse_book_page(lines, get_more_pages) -> tuple[str, str] | None:
    """Return (title, abstract) iff this PDF page begins a talk.

    Strategy:
      1. Confirm the page IS a paper start (via _book_paper_start_title)
         and take the title.
      2. Look for an explicit "Short Abstract" / "Abstract" label and start
         collecting from after it. If no label is found, fall back to
         "first prose line after the header block" — this captures the
         pages whose authors didn't include a labelled short abstract but
         whose talk body is still useful as an abstract surrogate.
      3. Consume body lines (page-by-page via `get_more_pages`, which
         yields the next page's lines) until we hit either a References /
         Acknowledgments / Bibliography section, the next talk's title-
         font run, or the abstract hits the size cap.

    `get_more_pages` is a generator that yields the next page's `lines`
    each time it is called. We use it to spill the abstract across pages
    so the collector matches the user's "err on the side of including
    too much" preference."""
    hit = _book_paper_start_title(lines)
    if hit is None:
        return None
    title, i = hit
    # Poster pages carry no linear abstract — emit the title with an
    # empty abstract (the poster PDF is still attached for viewing).
    if _book_page_is_poster(lines):
        return title, ""
    # Find abstract start: (a) explicit Abstract label, or (b) first prose
    # line after the header block.
    abs_start_idx = None
    inline_tail = ""
    label_seen = False
    for k in range(i, len(lines)):
        size, words = lines[k][0], lines[k][1]
        if size >= _ABS_TITLE_FONT_MIN:
            return None
        text = _abs_line_text(words)
        m = _ABS_LABEL_RE.match(text)
        if m:
            label_seen = True
            tail = m.group(2).strip(" .:–—-")
            if tail and not _ABS_PLACEHOLDER_RE.match(tail):
                inline_tail = tail
            abs_start_idx = k + 1
            break
        # An Abstract label only ever sits in the HEADER region (right after
        # the authors/affiliation/contact block). Once a section heading
        # ("1. Introduction", "Results", ...) appears, the labeled-abstract
        # region is over — stop scanning so a stray "abstract" in body prose
        # can't hijack the start; the no-label fallback below then picks the
        # first prose line. Checked AFTER the line-start label match, since a
        # bare "Abstract" header line is itself a section keyword.
        if (_ABS_NUMBERED_SECTION_RE.match(text)
                or (_ABS_SECTION_KW_RE.match(text) and len(text) <= 80)):
            break
        m2 = _ABS_LABEL_INLINE_RE.search(text)
        if m2 and m2.start() > 5:
            label_seen = True
            tail = m2.group(2).strip(" .:–—-")
            if tail and not _ABS_PLACEHOLDER_RE.match(tail):
                inline_tail = tail
            abs_start_idx = k + 1
            break
    if abs_start_idx is None:
        # Fallback: scan past the header block and start at the first
        # prose-shaped line.
        for k in range(i, len(lines)):
            size, words = lines[k][0], lines[k][1]
            if size >= _ABS_TITLE_FONT_MIN:
                return None
            if size < _ABS_BODY_FONT_MIN:
                continue
            text = _abs_line_text(words)
            if _ABS_PLACEHOLDER_RE.match(text):
                continue
            if _abs_is_header_line(text):
                continue
            abs_start_idx = k
            break
        if abs_start_idx is None:
            return None

    abs_chunks: list[str] = []
    if inline_tail:
        abs_chunks.append(inline_tail)
    # In the no-label fallback, we want to skip any leftover header-shaped
    # noise (single-token vendor names, contact lines pdfplumber didn't
    # cluster cleanly with the author block) until we hit a real prose
    # sentence. When an explicit Abstract / Short Abstract label was
    # found, trust the label — everything after it is the abstract, even
    # if the opening sentence happens to LOOK author-shaped (e.g. a
    # sentence beginning with a person's name + ≥2 commas).
    skip_header_until_body = not label_seen

    # Layout-change detection state:
    #   PARA_GAP_PT — vertical gap above which we mark a paragraph break.
    #   INDENT_DELTA_PT — horizontal change in left-margin (x0) above
    #     which we treat the line as a layout shift (e.g. the body
    #     section starts at a different left margin than the indented
    #     short abstract).
    PARA_GAP_PT = 18.0
    INDENT_DELTA_PT = 12.0
    PARA_SEP = "\n\n"
    prev_top: list[float] = [None]
    base_x0: list[float] = [None]
    pending_para_break: list[bool] = [False]
    # Abstract collection runs until we hit a stop condition (section
    # header, References, Acknowledgments, layout shift). Reference
    # collection then takes over and runs until the next talk title.
    # The collectors are independent so we can keep walking past
    # section headers to find the References section that comes later.
    abstract_done: list[bool] = [False]
    refs_mode: list[bool] = [False]
    refs_lines: list[str] = []
    _REF_HDR_RE = re.compile(r"^\s*References?\s*:?\s*$", re.IGNORECASE)

    def _consume(it) -> bool:
        """Walk lines. Builds the abstract until a stop is reached, then
        keeps walking to look for a References section (whose entries
        are collected into refs_lines). Returns True only when we hit
        the next talk's title-font run (end of this talk's pages)."""
        nonlocal skip_header_until_body
        for size, words, top, x0 in it:
            if size >= _ABS_TITLE_FONT_MIN:
                return True
            text = _abs_line_text(words)
            # References mode: collect lines into refs_lines. Stop the
            # whole walk once a figure/table caption or an
            # acknowledgment/funding section appears — on a spill page the
            # reference list is often followed by the figure block and an
            # Acknowledgment paragraph, and without this they'd be glued
            # onto the last reference entry as continuation lines.
            if refs_mode[0]:
                if re.match(r"^\s*\d{1,3}\s*$", text):
                    continue
                if re.match(r"^\s*[ivxlcIVXLC]+\s*$", text):
                    continue
                if _ABS_CAPTION_RE.match(text) or _ABS_STOP_RE.match(text):
                    return True
                # Sub-body-size text in the reference region is the
                # figure block (axis labels, sub-panel letters) that a
                # spill page often places ABOVE the "Figure N" caption —
                # references themselves are always body-sized, so a tiny
                # line here means the reference list has ended.
                if size < _ABS_REF_FONT_MIN:
                    return True
                refs_lines.append(text)
                continue
            # Found References header — switch modes, keep walking.
            if _REF_HDR_RE.match(text):
                refs_mode[0] = True
                abstract_done[0] = True
                continue
            # Other stop markers (Acknowledgments / Bibliography / etc.)
            # close the abstract but DON'T stop the walk — we still
            # want to reach a possible References section after them.
            if _ABS_STOP_RE.match(text):
                abstract_done[0] = True
                continue
            if _ABS_PLACEHOLDER_RE.match(text):
                continue
            if size < _ABS_BODY_FONT_MIN:
                continue
            if _ABS_SECTION_INLINE_RE.match(text):
                abstract_done[0] = True
                continue
            if (_ABS_SECTION_KW_RE.match(text)
                    or _ABS_NUMBERED_SECTION_RE.match(text)):
                if len(text) <= 80:
                    abstract_done[0] = True
                    continue
            if re.match(r"^\s*[ivxlcIVXLC]+\s*$", text):
                continue
            if re.match(r"^\s*\d{1,3}\s*$", text):
                continue
            # Once the abstract is done, just keep walking until we
            # find References (we never add more body content).
            if abstract_done[0]:
                continue
            if skip_header_until_body:
                if _abs_is_header_line(text):
                    continue
                skip_header_until_body = False
                prev_top[0] = top
                base_x0[0] = x0
                abs_chunks.append(text)
                continue
            # Layout-shift detection: track the LEFTMOST line we have
            # collected so far (the wrap margin), and stop adding to
            # the abstract when a new line starts visibly to the LEFT
            # of that. Catches the "Thouless Pumping"-style abstract
            # that uses indentation (not an "Introduction" header) to
            # separate the abstract from the body.
            if base_x0[0] is None:
                base_x0[0] = x0
            else:
                if x0 < base_x0[0] - INDENT_DELTA_PT:
                    abstract_done[0] = True
                    continue
                if x0 < base_x0[0]:
                    base_x0[0] = x0
            # Paragraph break: a wider-than-usual vertical gap. Emit
            # the separator in-line; the joiner below renders it as
            # the literal `\n\n` paragraph delimiter.
            if prev_top[0] is not None and abs_chunks:
                gap = top - prev_top[0]
                if gap > PARA_GAP_PT:
                    pending_para_break[0] = True
            if pending_para_break[0]:
                abs_chunks.append(PARA_SEP)
                pending_para_break[0] = False
            prev_top[0] = top
            abs_chunks.append(text)
        return False

    stopped = _consume(lines[abs_start_idx:])
    while not stopped:
        next_lines = get_more_pages()
        if next_lines is None:
            break
        prev_top[0] = None
        stopped = _consume(next_lines)

    if not abs_chunks:
        return None
    # Joiner: PARA_SEP entries become literal "\n\n"; everything else is
    # joined with a single space. Collapse runs of spaces within each
    # paragraph (NOT across them) so the paragraph breaks survive.
    paragraphs: list[list[str]] = [[]]
    for chunk in abs_chunks:
        if chunk == PARA_SEP:
            if paragraphs[-1]:
                paragraphs.append([])
        else:
            paragraphs[-1].append(chunk)
    para_strs = [re.sub(r"\s+", " ", " ".join(p)).strip()
                 for p in paragraphs if p]
    # Drop figure-caption paragraphs. Captions land in the PDF text
    # between body paragraphs and have no reading value once the figure
    # itself isn't carried over. Matches Figure 1:, Fig. 1:, Fig 1.,
    # Figure 1A:, etc.
    para_strs = [p for p in para_strs if p and not _ABS_CAPTION_RE.match(p)]
    abstract = "\n\n".join(para_strs)
    # Find which references the abstract actually cites, then look up
    # those entries in the References section and append them. Citations
    # come in many shapes: [1], [1,2], [1, 2], [1-3], [1–3], [1, 3-5].
    cited = _cited_ref_numbers(abstract)
    if cited and refs_lines:
        ref_map = _parse_reference_entries(refs_lines)
        wanted = [n for n in sorted(cited) if n in ref_map]
        if wanted:
            ref_block = "\n".join(f"[{n}] {ref_map[n]}" for n in wanted)
            abstract = abstract + "\n\n" + ref_block
    # Strip leading punctuation noise (a stray ":"/"."/"-" pdfplumber
    # sometimes leaves attached to the first word) and outer whitespace,
    # but PRESERVE the trailing sentence-final period.
    abstract = abstract.lstrip(" .:-–—\t").rstrip()
    # Trim at any inline section header pdfplumber merged with the previous
    # sentence (e.g. "...lasers. 1.Introduction The mid-infrared region…").
    m_trim = _ABS_INLINE_SECTION_RE.search(abstract)
    if m_trim and m_trim.start() > 80:
        abstract = abstract[:m_trim.start()].rstrip(" ,-")
    # Repair line-break hyphenations: "ma- turity" → "maturity". Conservative:
    # only fire when the right-hand side is a real lowercase word fragment,
    # NOT a connector word (which would indicate a legitimate compound like
    # "mid- and far-infrared" or "second- and third-order").
    _CONNECTORS = {"and", "or", "the", "for", "to", "by", "in", "on", "of",
                   "from", "with", "at", "as", "but", "nor", "yet", "so",
                   "than", "into", "onto"}
    def _heal_hyphen(m):
        right = m.group(2)
        if right.lower() in _CONNECTORS:
            return m.group(0)
        return m.group(1) + right
    abstract = re.sub(r"(\w{2,})-\s+([a-z]\w+)", _heal_hyphen, abstract)
    # Try to unmangle the runs-together text some LaTeX templates produce.
    abstract = re.sub(
        r"(\S{20,})",
        lambda m: re.sub(r"(?<=[a-z])(?=[A-Z])", " ", m.group(1)),
        abstract)
    # Generous cap (only fires for genuinely runaway extractions; most real
    # abstracts land well under this). The user prefers "too much" over
    # "too little", so this is set high enough that every legitimate
    # writeup in the book fits with room to spare.
    if len(abstract) > 9000:
        abstract = abstract[:9000].rsplit(" ", 1)[0] + " …"
    if len(abstract) < 40:
        return None
    return title, abstract


def _norm_title_for_match(title: str) -> str:
    """Normalize a title for matching across sources. Strips accents,
    lowercases, collapses non-word characters to a single space, AND removes
    spaces around digits (so "4 kHz" and "4kHz" hash the same)."""
    import unicodedata
    s = unicodedata.normalize("NFKD", title.strip().lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+(?=\d)|(?<=\d)\s+", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _alt_title_fingerprint(title: str) -> str:
    """Aggressive secondary fingerprint that drops ALL whitespace and ALL
    digits — used as a fallback when titles disagree only on how chemistry
    subscripts (e.g. "Pb(1-x)SnxSe" vs "Pb Sn Se" with the subscript on a
    separate line) flowed through PDF text extraction."""
    import unicodedata
    s = unicodedata.normalize("NFKD", title.strip().lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z]", "", s)


def _title_prefix_key(title: str, n: int = 100) -> str:
    """First N characters of the normalized title — a tertiary fingerprint
    for titles that diverge in their tail (chemistry subscripts at the end,
    minor wording differences in the last few words). Real talks rarely
    share a 100-char prefix unless they're the same talk."""
    return _norm_title_for_match(title)[:n]


def _load_abstracts() -> dict[str, str]:
    """Read the optional book-of-abstracts PDF and return
    {normalized_title: short_abstract}. Returns {} (with a one-line note)
    when the file is absent or unreadable."""
    if not ABSTRACT_BOOK_IN.exists():
        print(f"[process] no book of abstracts at {ABSTRACT_BOOK_IN.name}; "
              "talks will emit without abstracts (this file is optional).",
              flush=True)
        return {}
    try:
        import pdfplumber
    except ImportError:
        print(f"[process] pdfplumber not installed — skipping the book of "
              "abstracts.", flush=True)
        return {}
    out: dict[str, str] = {}
    try:
        with pdfplumber.open(ABSTRACT_BOOK_IN) as pdf:
            page_lines = [_abs_page_lines(p) for p in pdf.pages]
    except Exception as e:                                # noqa: BLE001
        print(f"[process] could not read {ABSTRACT_BOOK_IN.name} ({e}); "
              "talks will emit without abstracts.", flush=True)
        return {}
    for pno, lines in enumerate(page_lines):
        # Build a generator the parser drives to spill onto following
        # pages. We stop after a few pages of spill — that's far more
        # than any real abstract needs but stops a misparse from
        # consuming the whole rest of the book.
        spill_idx = pno + 1
        spill_budget = 6  # conservative cap on spill pages per abstract

        def _next_page(
                _state={"i": spill_idx, "left": spill_budget}):
            if _state["left"] <= 0 or _state["i"] >= len(page_lines):
                return None
            nxt = page_lines[_state["i"]]
            _state["i"] += 1
            _state["left"] -= 1
            return nxt

        res = _parse_book_page(lines, _next_page)
        if not res:
            continue
        title, abstract = res
        # Index under three keys: the normal normalized form, the
        # aggressive letters-only fingerprint, and the first-100-chars
        # prefix. The lookup tries each in turn.
        out.setdefault(_norm_title_for_match(title), abstract)
        out.setdefault(_alt_title_fingerprint(title), abstract)
        prefix = _title_prefix_key(title)
        if len(prefix) >= 60:
            out.setdefault(prefix, abstract)
    n_unique = sum(1 for _ in {id(v) for v in out.values()})
    print(f"[process] book of abstracts : {n_unique} short abstract(s) "
          "extracted.", flush=True)
    return out


def _attach_paper_pages(talks: list[dict]) -> None:
    """Tag each matched talk with the source PDF + its page range, as
        talk["paper"] = {"file": "<book filename>", "pages": [first, last]}
    where the page numbers are 1-based and inclusive (relative to the
    file, which lives in data/). The builder does the actual slicing and
    embedding; the processor only records WHERE each paper is. Mutates
    `talks` in place. No-op when the book file is missing or unreadable.

    For this conference the abstract book and the "book of papers" are
    the same file — short abstracts and full long-form contributions live
    in the same pages — so we treat each talk's page range within the
    book as that talk's paper. Paper-start pages are detected with the
    same heuristic the abstract parser uses (title-font run + talk-page
    signature); each paper spans [its-start, next-start - 1], and the
    last paper runs to end of book."""
    if not ABSTRACT_BOOK_IN.exists():
        return
    try:
        import pdfplumber
    except ImportError as e:
        print(f"[process] book of papers   : skipping page detection "
              f"({e}).", flush=True)
        return
    try:
        with pdfplumber.open(ABSTRACT_BOOK_IN) as pdf:
            page_count = len(pdf.pages)
            page_lines = [_abs_page_lines(p) for p in pdf.pages]
    except Exception as e:                                # noqa: BLE001
        print(f"[process] book of papers   : could not read "
              f"{ABSTRACT_BOOK_IN.name} ({e}).", flush=True)
        return

    # Pre-pass over every page: each one that passes the paper-start
    # signature is a paper boundary. Collect (page_index, title).
    boundaries: list[tuple[int, str]] = []
    for pno, lines in enumerate(page_lines):
        hit = _book_paper_start_title(lines)
        if hit is None:
            continue
        boundaries.append((pno, hit[0]))
    if not boundaries:
        print("[process] book of papers   : no paper-start pages detected; "
              "no papers attached.", flush=True)
        return

    # Derive page ranges and index them by the same three title keys the
    # abstract lookup uses, so the talk-side match below is identical.
    title_to_range: dict[str, tuple[int, int]] = {}
    for idx, (pno, title) in enumerate(boundaries):
        end = (boundaries[idx + 1][0] - 1
               if idx + 1 < len(boundaries) else page_count - 1)
        for key in (_norm_title_for_match(title),
                    _alt_title_fingerprint(title),
                    _title_prefix_key(title)):
            if key and key not in title_to_range:
                title_to_range[key] = (pno, end)

    n_tagged = 0
    for talk in talks:
        title = (talk.get("title") or "").strip()
        if not title:
            continue
        rng = (title_to_range.get(_norm_title_for_match(title))
               or title_to_range.get(_alt_title_fingerprint(title))
               or title_to_range.get(_title_prefix_key(title)))
        if rng is None:
            continue
        start, end = rng                       # 0-based, inclusive
        talk["paper"] = {
            "file": ABSTRACT_BOOK_IN.name,
            "pages": [start + 1, end + 1],     # 1-based, inclusive
        }
        n_tagged += 1
    print(f"[process] book of papers   : tagged {n_tagged} talk(s) with "
          f"source pages in {ABSTRACT_BOOK_IN.name}.", flush=True)


# -----------------------------------------------------------------------------
# Book-of-abstracts program-overview parser (OPTIONAL)
#
# The same PDF that supplies the per-talk abstracts also opens with a
# Scientific Program grid (a few pages laying out every day's sessions with
# their NAMES and CHAIRS). When the book is supplied, we parse that grid
# and use it as the AUTHORITATIVE session structure — it carries finer-
# grained session boundaries than the website (each "Summer School I" /
# "Workshop I" is its own session, not merged into a per-half-day "School N"
# block) and the chair names the website omits entirely.
#
# Schema returned by _parse_book_program:
#   [{"date": "YYYY-MM-DD", "wd": "monday",
#     "items": [
#         {"kind": "session", "name": "Summer School I",
#          "chair": "Edmund Linfield",
#          "talks": [(start, end, title, speaker, aff_line), …]},
#         {"kind": "event",   "name": "Lunch & Free Time",
#          "start": "12:30", "end": "15:00"},
#         {"kind": "posters", "name": "POSTER SESSION",
#          "start": "18:30", "end": "20:00"},
#         {"kind": "break",   "name": "Coffee Break",  # only when outside
#          "start": "10:30", "end": "10:45"},          # any session
#      ]}]
# Coffee breaks INSIDE a session are folded into that session's `talks` list
# with the speaker/aff_line slot left empty and a sentinel value in place
# (see "break" in the last position) — the restructurer below picks them up
# and keeps them visible as in-session breaks.
# -----------------------------------------------------------------------------
_BP_DAY_RE = re.compile(
    r"^(?P<wd>MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY)"
    r"\s+(?P<d>\d+)(?:st|nd|rd|th)?\s+(?P<m>\w+)\b", re.IGNORECASE)
_BP_SESSION_HDR_RE = re.compile(
    r"^(?P<name>(?:Summer\s+School|Workshop|School|Symposium|Tutorial)"
    r"\s+[IVX]+(?:\s+[A-Za-z]+)?)"
    r"\s*[-–]\s*Chair\s*:\s*(?P<chair>.+?)\s*$", re.IGNORECASE)
_BP_TIME_RE = re.compile(
    r"^(\d{1,2}):(\d{2})\s*[-–]\s*(\d{1,2}):(\d{2})\s+(.+)$")
_BP_BREAK_RE = re.compile(r"^Coffee\s*Break\b", re.IGNORECASE)
_BP_LUNCH_RE = re.compile(r"^Lunch\b", re.IGNORECASE)
_BP_DINNER_RE = re.compile(r"^Dinner\b", re.IGNORECASE)
_BP_RECEPT_RE = re.compile(r"^(Welcome\s+Reception|Reception)\b",
                           re.IGNORECASE)
_BP_POSTERS_RE = re.compile(r"^POSTER\s+SESSION\b", re.IGNORECASE)
_BP_POSTER_PRIZE_RE = re.compile(r"^Poster\s+Prize\b", re.IGNORECASE)
_BP_EXCURSION_RE = re.compile(r"^(Excursion|City\s+Tour)\b", re.IGNORECASE)
_BP_GALA_RE = re.compile(r"^Gala\b", re.IGNORECASE)
_BP_FREE_RE = re.compile(r"^Free\s+Time\b", re.IGNORECASE)
_BP_OPENING_RE = re.compile(r"^OPENING\b", re.IGNORECASE)
_BP_CLOSING_RE = re.compile(r"^CLOSING\b", re.IGNORECASE)


def _bp_find_pages(pdf):
    """Return (first_idx, last_idx) of the program-overview pages — those
    whose text contains the SCIENTIFIC PROGRAM heading and any chair-tagged
    session header. Returns (None, None) when the book is some other shape."""
    first = None
    for i, page in enumerate(pdf.pages[:30]):
        text = page.extract_text() or ""
        if "SCIENTIFIC PROGRAM" in text:
            first = i
            break
    if first is None:
        return None, None
    last = first
    for i in range(first + 1, min(first + 10, len(pdf.pages))):
        text = pdf.pages[i].extract_text() or ""
        if _BP_DAY_RE.search(text) or "Chair:" in text:
            last = i
        else:
            break
    return first, last


def _bp_collect_lines(pdf, first_idx, last_idx) -> list[str]:
    """Extract the program-overview text as a flat, footer-stripped line
    list spanning every overview page."""
    raw: list[str] = []
    for i in range(first_idx, last_idx + 1):
        text = pdf.pages[i].extract_text() or ""
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        # Drop trailing bare page-number / roman-numeral footers.
        while lines and re.match(r"^[ivxlcdmIVXLCDM]+\s*$|^\d{1,3}\s*$",
                                  lines[-1]):
            lines.pop()
        raw.extend(lines)
    return raw


def _parse_book_program(pdf_path, year: int = YEAR) -> list[dict]:
    """Parse the book's Scientific Program grid into per-day structured
    items. Returns [] when the book is missing or the grid can't be found."""
    if not Path(pdf_path).exists():
        return []
    try:
        import pdfplumber
    except ImportError:
        return []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            first, last = _bp_find_pages(pdf)
            if first is None:
                return []
            lines = _bp_collect_lines(pdf, first, last)
    except Exception:                                     # noqa: BLE001
        return []

    days: list[dict] = []
    cur_day: dict | None = None
    cur_session: dict | None = None

    def _flush_session():
        nonlocal cur_session
        if cur_session is not None and cur_day is not None:
            cur_day["items"].append(cur_session)
        cur_session = None

    i = 0
    while i < len(lines):
        ln = lines[i]
        m = _BP_DAY_RE.match(ln)
        if m:
            _flush_session()
            wd = m.group("wd").lower()
            dnum = int(m.group("d"))
            mnum = MONTHS.get(m.group("m").lower())
            iso = f"{year:04d}-{mnum:02d}-{dnum:02d}" if mnum else ""
            cur_day = {"date": iso, "wd": wd, "items": []}
            days.append(cur_day)
            i += 1
            continue
        if cur_day is None:
            i += 1
            continue
        m = _BP_SESSION_HDR_RE.match(ln)
        if m:
            _flush_session()
            cur_session = {"kind": "session", "name": m.group("name").strip(),
                           "chair": m.group("chair").strip(),
                           "talks": []}
            i += 1
            continue
        m = _BP_TIME_RE.match(ln)
        if m:
            sh, sm, eh, em, rest = m.groups()
            start = f"{int(sh):02d}:{int(sm):02d}"
            end = f"{int(eh):02d}:{int(em):02d}"
            rest = rest.strip()
            # Classify by `rest` (most-specific first).
            if _BP_BREAK_RE.match(rest):
                # Coffee break → keep inside current session if any.
                if cur_session is not None:
                    cur_session["talks"].append(
                        (start, end, "Coffee Break", "", "break"))
                else:
                    cur_day["items"].append({"kind": "break",
                                             "name": "Coffee Break",
                                             "start": start, "end": end})
                i += 1
                continue
            event_kinds = (_BP_LUNCH_RE, _BP_DINNER_RE, _BP_RECEPT_RE,
                           _BP_EXCURSION_RE, _BP_GALA_RE, _BP_FREE_RE)
            if any(pat.match(rest) for pat in event_kinds) \
                    or "Welcome Reception" in rest \
                    or "Lunch & Free Time" in rest:
                _flush_session()
                cur_day["items"].append({"kind": "event", "name": rest,
                                         "start": start, "end": end})
                i += 1
                continue
            if _BP_POSTERS_RE.match(rest):
                _flush_session()
                # Some posters lines have a "& \"South\" break" continuation
                # on the next visible line — fold it into the same item.
                name = rest
                if i + 1 < len(lines) and lines[i + 1].startswith("&"):
                    name = name + " " + lines[i + 1]
                    i += 1
                cur_day["items"].append({"kind": "posters", "name": name,
                                         "start": start, "end": end})
                i += 1
                continue
            if _BP_POSTER_PRIZE_RE.match(rest):
                _flush_session()
                cur_day["items"].append({"kind": "event",
                                         "name": "Poster Prize",
                                         "start": start, "end": end})
                i += 1
                continue
            if _BP_OPENING_RE.match(rest):
                _flush_session()
                # "OPENING Session - Angela Vasanelli & Joshua Freeman"
                # → title "Opening Session", presider on the chair slot,
                # NO talks (it's an empty Event session).
                _, _, organizers = rest.partition(" - ")
                cur_day["items"].append({
                    "kind": "event", "name": "Opening Session",
                    "chair": organizers.strip(),
                    "start": start, "end": end,
                })
                i += 1
                continue
            if _BP_CLOSING_RE.match(rest):
                _flush_session()
                cur_day["items"].append({
                    "kind": "event", "name": "Closing Remarks",
                    "chair": "",
                    "start": start, "end": end,
                })
                i += 1
                continue
            # Otherwise a talk row. The title can wrap onto the next line(s);
            # the line AFTER the title is "Speaker - Affiliation (Country)".
            title = rest
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if (_BP_TIME_RE.match(nxt) or _BP_DAY_RE.match(nxt)
                        or _BP_SESSION_HDR_RE.match(nxt)):
                    break
                if (" - " in nxt and "Chair:" not in nxt
                        and not nxt.startswith("(")):
                    break  # this is the speaker-affiliation line
                title = title + " " + nxt
                j += 1
            speaker_line = lines[j] if j < len(lines) else ""
            speaker, aff = "", ""
            if speaker_line and " - " in speaker_line:
                speaker, aff = speaker_line.split(" - ", 1)
                speaker, aff = speaker.strip(), aff.strip()
                i = j + 1
            else:
                i = j  # don't consume the next line if it didn't match
            if cur_session is None:
                # Orphan talk — synthesize an unnamed session container.
                cur_session = {"kind": "session", "name": "", "chair": "",
                               "talks": []}
            cur_session["talks"].append(
                (start, end, title.strip(), speaker, aff))
            continue
        i += 1
    _flush_session()
    return days


def _restructure_sessions_from_book(
    book_days: list[dict], old_sessions: list[dict], talks: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Replace the website-derived session list with one built from the book
    program. Existing talks are PRESERVED (their author/affiliation/abstract
    data is the richest source); each talk's session_id and start/end_ts
    are reassigned to the book session it falls under, matched by title.

    Returns (new_sessions, talks) — talks is the same list (mutated) so
    callers can keep their existing reference."""
    # Build talk lookup keyed by normalized title. We DON'T collapse on
    # (date, title) — multiple Coffee Break talks share that key, and a
    # dict would only keep the last one; instead the lookup returns the
    # full candidate list and the matcher picks by date + start-time
    # proximity below.
    talks_by_title: dict[str, list[dict]] = {}
    for t in talks:
        nt = _norm_title_for_match(t["title"])
        talks_by_title.setdefault(nt, []).append(t)
    used_talk_ids: set[str] = set()
    matched_count = 0
    unmatched: list[tuple[str, str]] = []

    def _match_talk(date: str, title: str,
                    start_hint: str = "") -> dict | None:
        """Find the talk matching this book entry. `start_hint` is "HH:MM"
        from the book schedule; when several candidates share the same
        title + date (multiple Coffee Breaks on the same day), pick the
        one whose start_ts is closest in time."""
        nonlocal matched_count
        nt = _norm_title_for_match(title)
        cands: list[dict] = []
        if nt in talks_by_title:
            cands = list(talks_by_title[nt])
        else:
            alt = _alt_title_fingerprint(title)
            pref = _title_prefix_key(title)
            for cand in talks:
                cand_alt = _alt_title_fingerprint(cand["title"])
                cand_pref = _title_prefix_key(cand["title"])
                if alt == cand_alt or (len(pref) >= 60 and pref == cand_pref):
                    cands = [cand]
                    break
        if not cands:
            return None
        # Same-date candidates first; break ties by start-time proximity.
        def _hint_minutes(ts: str) -> int:
            try:
                return int(start_hint[:2]) * 60 + int(start_hint[3:5])
            except (ValueError, IndexError):
                return -1
        def _ts_minutes(ts: str) -> int:
            try:
                return int(ts[11:13]) * 60 + int(ts[14:16])
            except (ValueError, IndexError):
                return -1
        hint_min = _hint_minutes(start_hint) if start_hint else -1
        same_date = [c for c in cands
                     if c.get("start_ts", "")[:10] == date
                     and c["id"] not in used_talk_ids]
        if same_date:
            if hint_min >= 0:
                same_date.sort(
                    key=lambda c: abs(_ts_minutes(c.get("start_ts", ""))
                                      - hint_min))
            chosen = same_date[0]
            used_talk_ids.add(chosen["id"])
            matched_count += 1
            return chosen
        # Fall back to any unused candidate.
        for c in cands:
            if c["id"] not in used_talk_ids:
                used_talk_ids.add(c["id"])
                matched_count += 1
                return c
        return cands[0]

    import datetime as _dt
    sessions: list[dict] = []
    sess_seq = 0
    weekday_name = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    def _next_id() -> str:
        nonlocal sess_seq
        sess_seq += 1
        return f"S{sess_seq:03d}"

    for day in book_days:
        date_iso = day["date"]
        try:
            date_obj = _dt.date.fromisoformat(date_iso)
            date_label = date_obj.strftime(f"%d-%b-{YEAR}")
        except (ValueError, TypeError):
            date_label = ""
        for item in day["items"]:
            kind = item["kind"]
            if kind == "session":
                tids: list[str] = []
                # Map each book talk to an existing talk record; update its
                # session_id and ts.
                for (start, end, title, speaker, aff) in item["talks"]:
                    sess_start_iso = f"{date_iso}T{start}:00"
                    sess_end_iso = f"{date_iso}T{end}:00"
                    is_break = (aff == "break")
                    talk = _match_talk(date_iso, title, start_hint=start)
                    if talk is None:
                        unmatched.append((date_iso + " " + start, title))
                        continue
                    talk["start_ts"] = sess_start_iso
                    talk["end_ts"] = sess_end_iso
                    tids.append(talk["id"])
                if not tids:
                    continue
                # Session start/end = first/last talk's ts.
                first_talk = next(t for t in talks if t["id"] == tids[0])
                last_talk = next(t for t in talks if t["id"] == tids[-1])
                sess_id = _next_id()
                for tid in tids:
                    talk = next(t for t in talks if t["id"] == tid)
                    talk["session_id"] = sess_id
                # The session COLOR is a SESSION-type token picked by PHASE,
                # NOT the dominant talk color: talk-type tokens (indigo
                # "Invited", sky "Contributed") must not leak up to the session
                # level — those aren't session types. A School session is
                # Tutorial (fuchsia); a Workshop session is Technical (blue),
                # matching _flush_tech() and the SESSION_TYPES registry. The
                # talks inside keep their own Invited/Contributed coloring.
                is_school = "School" in item["name"]
                color = "fuchsia" if is_school else "blue"
                sessions.append({
                    "id": sess_id,
                    "title": item["name"],
                    "type": "School" if is_school else "Workshop",
                    "topic": "",
                    "date": date_label,
                    "location": "",
                    "presider": item["chair"],
                    "presider_aff": "",
                    "details": "",
                    "start_ts": first_talk["start_ts"],
                    "end_ts": last_talk["end_ts"],
                    "color": color,
                    "talk_ids": tids,
                })
            elif kind == "event":
                # Standalone Event-type session — always EMPTY (no talks).
                # Lunch / Dinner / Reception / Opening / Closing / Poster
                # Prize / etc. are time slots without technical content.
                sess_id = _next_id()
                start_iso = f"{date_iso}T{item['start']}:00"
                end_iso = f"{date_iso}T{item['end']}:00"
                sessions.append({
                    "id": sess_id,
                    "title": item["name"],
                    "type": "General",
                    "topic": "",
                    "date": date_label,
                    "location": "",
                    # Some events carry a chair-like attribution (the
                    # Opening Session's organizers).
                    "presider": item.get("chair", ""),
                    "presider_aff": "",
                    "details": "",
                    "start_ts": start_iso,
                    "end_ts": end_iso,
                    "color": "rose",
                    "talk_ids": [],
                })
            elif kind == "break":
                # A coffee break that wasn't inside any session (rare in the
                # IQCLSW grid) — emit as a standalone Event with the time.
                sess_id = _next_id()
                start_iso = f"{date_iso}T{item['start']}:00"
                end_iso = f"{date_iso}T{item['end']}:00"
                sessions.append({
                    "id": sess_id,
                    "title": item["name"],
                    "type": "General",
                    "topic": "",
                    "date": date_label,
                    "location": "",
                    "presider": "",
                    "presider_aff": "",
                    "details": "",
                    "start_ts": start_iso,
                    "end_ts": end_iso,
                    "color": "rose",
                    "talk_ids": [],
                })
            elif kind == "posters":
                # The original poster sessions (POSTERS1 / POSTERS2) already
                # carry the catalog of P0xx talks. We update their times to
                # match the book and keep them in the new session list.
                poster_sess = None
                for s in old_sessions:
                    if s.get("type") == "Posters":
                        ts = s.get("start_ts", "")
                        if ts.startswith(date_iso):
                            poster_sess = s
                            break
                if poster_sess is not None:
                    start_iso = f"{date_iso}T{item['start']}:00"
                    end_iso = f"{date_iso}T{item['end']}:00"
                    poster_sess["start_ts"] = start_iso
                    poster_sess["end_ts"] = end_iso
                    poster_sess["date"] = date_label
                    # Sync poster talks' ts to the new session window too.
                    for tid in poster_sess.get("talk_ids", []):
                        for t in talks:
                            if t["id"] == tid:
                                t["start_ts"] = start_iso
                                t["end_ts"] = end_iso
                                break
                    sessions.append(poster_sess)

    if unmatched:
        print(f"[process] book program     : {len(unmatched)} talk(s) "
              "could not be matched to a website talk:", flush=True)
        for ts, ti in unmatched[:10]:
            print(f"          - {ts}  {ti[:60]}", flush=True)
    # Drop website talks that no book session claimed. These are typically
    # the program's "Opening remarks" / "Closing remarks" entries, which
    # the book treats as empty Event sessions with no talk inside, and any
    # other content the website surfaced that the book program excludes.
    kept_talks = [t for t in talks if t["id"] in used_talk_ids
                  or t.get("session_id", "").startswith("POSTERS")]
    n_dropped = len(talks) - len(kept_talks)
    if n_dropped:
        print(f"[process] book program     : dropped {n_dropped} website "
              "talk(s) the book program doesn't surface as talks.",
              flush=True)
    return sessions, kept_talks


# -----------------------------------------------------------------------------
# Type / color registries (baked into the JSON; the app reads these directly).
# `id` is the color token the app filters and groups on, AND the token each
# session/talk's `color` field must use. The conference's color caption is a
# three-way split — invited-for-school / invited-for-workshop / contributed &
# posters — which we model as the TALK types below. SESSIONS use their own
# coarser taxonomy (Technical / Tutorial / Poster / Event), colored by program
# PHASE — never by their talks' type. Talk-type tokens (indigo "Invited", sky
# "Contributed") deliberately do NOT appear in SESSION_TYPES, so a session can
# never be labelled "Invited"; the per-talk Invited/Contributed split still
# shows on the talks themselves.
# -----------------------------------------------------------------------------
# Standard session/talk type taxonomy. The shared types; a conference only
# surfaces the ones its program actually uses (the app hides count-0 types).
SESSION_TYPES = [
    {"id": "blue",    "label": "Technical",
     "fg": "#2563eb", "bg_light": "#e8efff", "bg_dark": "#1a233d"},
    {"id": "fuchsia", "label": "Tutorial",
     "fg": "#c026d3", "bg_light": "#fae8ff", "bg_dark": "#3a0f3f"},
    {"id": "teal",   "label": "Poster",
     "fg": "#0d9488", "bg_light": "#d6f3ef", "bg_dark": "#102b27"},
    {"id": "rose",   "label": "Event",
     "fg": "#e11d48", "bg_light": "#ffe1e8", "bg_dark": "#38161f"},
]
TALK_TYPES = [
    {"id": "indigo", "label": "Invited",
     "fg": "#4f46e5", "bg_light": "#e6e4ff", "bg_dark": "#1d1a3d"},
    {"id": "sky",    "label": "Contributed",
     "fg": "#0284c7", "bg_light": "#e0f2fe", "bg_dark": "#0c2a3d"},
    {"id": "fuchsia", "label": "Tutorial",
     "fg": "#c026d3", "bg_light": "#fae8ff", "bg_dark": "#3a0f3f"},
    {"id": "teal",   "label": "Poster",
     "fg": "#0d9488", "bg_light": "#d6f3ef", "bg_dark": "#102b27"},
    {"id": "rose",   "label": "Event",
     "fg": "#e11d48", "bg_light": "#ffe1e8", "bg_dark": "#38161f"},
]

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

# Superscript digit -> ASCII digit, for affiliation markers on author names.
SUP = {"⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4", "⁵": "5",
       "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9"}
SUP_CLASS = "".join(re.escape(c) for c in SUP)

# Agenda blocks that DIVIDE sessions. The program's session structure (per the
# Program-at-a-Glance overview) splits the day into a MORNING and an AFTERNOON
# session at the mid-day meal/free-time, with evening events (dinner, reception,
# excursion, gala, poster session) standing apart. So only meals/free-time and
# evening events end a session — NOT coffee breaks (see below).
NON_TALK_PATTERNS = [
    r"^lunch\b", r"^dinner\b",
    r"^welcome reception", r"^reception\b", r"^poster session\b",
    r"^excursion\b", r"^gala\b", r"^free time\b", r"^poster prize\b",
    r"^registration\b", r"^lunch & ", r"^lunch and ",
]
_NON_TALK_RE = [re.compile(p, re.I) for p in NON_TALK_PATTERNS]

# Coffee breaks do NOT divide a session — the overview groups all talks in a
# morning (or afternoon) into ONE session regardless of the coffee break in the
# middle. So a coffee break is folded into the session as a (no-author) General
# talk at its time slot, exactly like Opening / Closing Remarks.
_INSESSION_BREAK_RE = re.compile(r"^(coffee break|break)\b", re.I)

# A poster-session divider is special: it both ends a session AND records a real
# scheduled poster slot whose time we attach to the poster catalog.
_POSTER_SLOT_RE = re.compile(r"^poster session\b", re.I)


def _block_kind(first_line: str) -> str:
    """Classify a block by its first (title) line:
        'talk'         — a scientific talk OR an in-session agenda item
                         (coffee break, Opening, Closing) folded into the session
        'poster_slot'  — a scheduled "Poster session" divider
        'divider'      — a meal/free-time/evening item that ENDS the session
    """
    s = _strip_emphasis(first_line)
    if _POSTER_SLOT_RE.search(s):
        return "poster_slot"
    if _INSESSION_BREAK_RE.search(s):
        return "talk"           # coffee break -> in-session General talk
    if any(rx.search(s) for rx in _NON_TALK_RE):
        return "divider"
    return "talk"


# Tidy display labels for the common divider blocks. The raw program text is
# sometimes verbose ("Lunch & free time", "Welcome Reception + Dinner"); we
# title the session by the meal/break itself. Order matters — first match wins.
_DIVIDER_LABELS = [
    (re.compile(r"coffee break", re.I), "Coffee Break"),
    (re.compile(r"\bbreak\b", re.I), "Break"),
    (re.compile(r"\blunch\b", re.I), "Lunch"),
    (re.compile(r"\bdinner\b", re.I), "Dinner"),
    (re.compile(r"reception", re.I), "Reception"),
    (re.compile(r"poster prize", re.I), "Poster Prize"),
    (re.compile(r"poster session", re.I), "Poster Session"),
    (re.compile(r"\bopening\b", re.I), "Opening"),
    (re.compile(r"closing", re.I), "Closing Remarks"),
    (re.compile(r"excursion|gala", re.I), "Excursion & Gala Dinner"),
    (re.compile(r"free time", re.I), "Free Time"),
    (re.compile(r"registration", re.I), "Registration"),
]


def _divider_label(first_line: str) -> str:
    """Return a clean session title for a meal/break/admin block."""
    s = _strip_emphasis(first_line)
    for rx, label in _DIVIDER_LABELS:
        if rx.search(s):
            return label
    # Fallback: use the raw label, trimmed of trailing decoration.
    return _clean(s).rstrip(" .–—-") or "Break"


# Day header line: "◊ Monday 29 June"
DAY_RE = re.compile(r"^\s*[◊◆♦•*]*\s*"
                    r"(?P<wd>[A-Za-z]+)\s+(?P<dom>\d{1,2})\s+(?P<mon>[A-Za-z]+)\s*$")
# Timed block start: "14:30-15:45 — rest" (any dash variant, optional trailing).
TIME_RE = re.compile(
    r"^(?P<s>\d{1,2}:\d{2})\s*[-–—]\s*(?P<e>\d{1,2}:\d{2})\s*[-–—]?\s*(?P<rest>.*)$")
# Poster bullet: "• Title"
POSTER_RE = re.compile(r"^\s*[•▪◦‣]\s*(?P<title>.+?)\s*$")


def _clean(s: str) -> str:
    """Collapse whitespace (incl. zero-width and NBSP) and trim. Also drops the
    speaker sentinel so it can never survive into an emitted name/affiliation."""
    if not s:
        return ""
    s = s.replace("\u200b", "").replace("\u200e", "").replace("\u00a0", " ")
    s = s.replace(_SPEAKER_MARK, "")
    return re.sub(r"\s+", " ", s).strip()


def _strip_emphasis(s: str) -> str:
    """Strip stray markdown/emphasis artifacts and surrounding quotes that
    occasionally survive the text extraction (e.g. '*Title coming soon*')."""
    s = s.replace(_SPEAKER_MARK, "").strip()
    s = re.sub(r"^[\*_]+|[\*_]+$", "", s).strip()
    return s


def _iso(dom: int, month: int, hhmm: str) -> str:
    h, m = hhmm.split(":")
    return f"{YEAR:04d}-{month:02d}-{dom:02d}T{int(h):02d}:{int(m):02d}:00"


def _looks_like_affiliation(line: str) -> bool:
    """True if a line is an affiliation block rather than an author list.

    Affiliation lines either start with a numbered marker ("1. ...") or carry
    a country-in-parentheses tail / strong institution keywords, and tend not
    to be a short comma list of Person Names.
    """
    s = line.strip()
    if re.match(r"^\d{1,2}[.\)]\s", s):
        return True
    if re.search(r"\([A-Za-zÀ-ÿ .'’\-]+\)\s*$", s) and any(
            kw in s for kw in (
                "University", "Universit", "Institute", "Institut",
                "Laboratory", "Laboratoire", "Lab", "CNR", "CNRS", "CEA",
                "ETH", "Technische", "School", "Centre", "Center", "GmbH",
                "Technology", "Photonics", "Physics", "Department",
                "Dipartimento", "National", "Academy", "Inc", "Labs")):
        return True
    return False


def _split_numbered_insts(line: str) -> list[tuple[int, str]]:
    """Split "1. A (X) ; 2. B (Y), 3. C (Z)" into [(1,'A (X)'), ...].

    A real institution marker is a number-dot (or number-paren) that sits at a
    DELIMITER boundary — the start of the string, or right after the ';' / ','
    that separates two numbered institutions. Requiring that boundary stops a
    number that merely lives inside an institution name from being mistaken for
    a marker — e.g. the "9)" in "Peter Gruenberg Institute 9 (PGI 9) (Germany)"
    must NOT split institution 3 into a bogus "#9". The separator before each
    marker may be ';', ',', or nothing; markers themselves are "N." or "N)".
    """
    anchors: list[tuple[int, int]] = []
    for m in re.finditer(r"(?:^|[;,])\s*(\d{1,2})[.\)]\s", line):
        # m.start(1) is where the number itself begins (after the separator/ws).
        anchors.append((m.start(1), int(m.group(1))))
    # Also accept a LEADING bare-number marker with no period, e.g.
    # "1 Laboratoire de physique …" (a source typo). Only at the very start, so
    # we never mistake a number embedded in an address (like "Building 2") for
    # an institution marker.
    lead = re.match(r"^(\d{1,2})\s+(?=[A-Za-zÀ-ÿ])", line)
    if lead and not any(pos == 0 for pos, _ in anchors):
        anchors = [(0, int(lead.group(1)))] + anchors
        anchors.sort()
    if not anchors:
        return []
    out: list[tuple[int, str]] = []
    for i, (pos, num) in enumerate(anchors):
        end = anchors[i + 1][0] if i + 1 < len(anchors) else len(line)
        seg = line[pos:end]
        # Strip the marker: "N." / "N)" / a leading bare "N ".
        seg = re.sub(r"^\d{1,2}([.\)]\s*|\s+)", "", seg).strip()
        seg = seg.rstrip(" ;,:")
        if seg:
            out.append((num, _clean(seg)))
    return out


def _parse_institutions(aff_lines: list[str]) -> list[dict]:
    """Parse one or more affiliation lines into [{n, name, alt_names}].

    Numbered form wins if present (joined across lines first). Otherwise the
    whole thing is a single unnumbered institution -> n=1.
    """
    joined = " ; ".join(l.strip() for l in aff_lines if l.strip())
    joined = _clean(joined)
    if not joined:
        return []
    # Drop a DANGLING trailing numbered marker — a "N." / "N)" with no
    # institution body after it (e.g. the source line
    # "1. … Politecnico di Milano (Italy) ; 2."). Left in place it gets
    # swallowed into the previous institution's name (defeating the shortener)
    # or yields an empty institution. Repeat to clear several (". ; 2. ; 3.").
    while True:
        trimmed = re.sub(r"[;,]?\s*\d{1,2}[.\)]\s*$", "", joined).strip()
        if trimmed == joined:
            break
        joined = trimmed
    if not joined:
        return []
    numbered = _split_numbered_insts(joined)
    insts: list[dict] = []
    if numbered:
        for n, name in numbered:
            insts.append({"n": n, "name": name, "alt_names": []})
    else:
        insts.append({"n": 1, "name": joined, "alt_names": []})
    return insts


def _parse_author_token(tok: str) -> tuple[str, list[int]]:
    """Parse one author token -> (name, [inst numbers]).

    Handles a leading 'and ', and a trailing run of superscript digits
    (optionally space-separated, e.g. 'Adam Bieganski¹ ³' -> insts [1,3]).
    """
    tok = _clean(tok)
    tok = re.sub(r"^and\s+", "", tok, flags=re.I)
    insts: list[int] = []
    m = re.search(rf"([{SUP_CLASS}\s]+)$", tok)
    if m:
        run = m.group(1)
        digits = [SUP[c] for c in run if c in SUP]
        if digits:
            insts = [int(d) for d in digits]
            tok = tok[:m.start()].strip()
    else:
        # Fallback: the source sometimes glues a PLAIN ASCII marker to the end
        # of a surname where a superscript was intended (e.g.
        # 'R. E. Dunin-Borkowski5'). Only treat a trailing 1-2 digit run as a
        # marker when it directly follows a letter (so we don't mangle a name
        # that legitimately ends in a number, which is vanishingly rare here).
        m2 = re.search(r"(?<=[A-Za-zÀ-ÿ])(\d{1,2})$", tok)
        if m2:
            insts = [int(m2.group(1))]
            tok = tok[:m2.start()].strip()
    # Strip a trailing lone comma/period left after marker removal.
    tok = tok.strip().rstrip(",")
    return tok, insts


def _parse_authors(line: str, institutions: list[dict]
                   ) -> tuple[list[dict], list[str], int | None]:
    """Parse an author line into (authors, aliases, speaker_idx).

    Each author is {name, insts}. When the talk has exactly one institution and
    NO author carried an explicit marker, every author is attributed to inst 1.
    Any author reference to an institution number that wasn't actually parsed
    is dropped, so the emitted data is always internally consistent (the builder
    rejects dangling references). Aliases collect the loose forms for search.

    `speaker_idx` is the index (into the returned authors list) of the author
    whose name carried the _SPEAKER_MARK sentinel — i.e. the presenting author,
    underlined in the source. It is None when no author was marked (the source
    occasionally omits the underline), letting the caller fall back to author 0.
    """
    valid_nums = {i["n"] for i in institutions}
    n_insts = len(institutions)
    authors: list[dict] = []
    speaker_idx: int | None = None
    for tok in line.split(","):
        tok = tok.strip()
        if not tok:
            continue
        is_speaker = _SPEAKER_MARK in tok
        tok = tok.replace(_SPEAKER_MARK, "")
        name, insts = _parse_author_token(tok)
        if name:
            if is_speaker and speaker_idx is None:
                speaker_idx = len(authors)
            authors.append({"name": name, "insts": insts})

    any_marker = any(a["insts"] for a in authors)
    if not any_marker and n_insts == 1:
        for a in authors:
            a["insts"] = [1]

    # Drop references to institutions that don't exist (source numbering gaps /
    # truncated affiliation lines), keeping order and de-duping.
    for a in authors:
        seen: set[int] = set()
        kept: list[int] = []
        for n in a["insts"]:
            if n in valid_nums and n not in seen:
                seen.add(n)
                kept.append(n)
        a["insts"] = kept

    aliases = [a["name"] for a in authors]
    return authors, aliases, speaker_idx


def _looks_like_authors(line: str) -> bool:
    """Heuristic: a line is an author list if it has comma-separated tokens that
    look like person names (capitalized words / initials), and is not itself an
    affiliation line."""
    s = line.strip()
    if not s or _looks_like_affiliation(s):
        return False
    # Author lines are usually "First Last, F. Last, ..." — short tokens, lots
    # of capitals/initials, few institution keywords.
    if re.search(r"\b(University|Institut|Laborat|CNRS|CNR|ETH|GmbH|"
                 r"Department|Dipartimento|School of)\b", s):
        return False
    # At least one capitalized name-ish token.
    return bool(re.search(r"[A-ZÀ-Þ][a-zà-ÿ]+|\b[A-Z]\.", s))


def _split_glued_name_aff(line: str) -> tuple[str, str]:
    """Some single-author blocks glue the speaker and affiliation on ONE line,
    separated by a run of >=2 spaces, e.g.
        'Benedikt Schwarz  Technische Universität Wien (Austria)'
    Return (author_part, aff_part) if such a split is detected, else (line, '').
    """
    m = re.search(r"^(.*?\S)\s{2,}(\S.*)$", line)
    if not m:
        return line, ""
    left, right = m.group(1).strip(), m.group(2).strip()
    if _looks_like_affiliation(right) and not _looks_like_affiliation(left):
        return left, right
    return line, ""


# Titles that are "General" program items regardless of slot length: the
# opening/closing housekeeping. (Coffee breaks and meals are also General — see
# _BREAK_TITLE_RE below — and a memorial lecture is treated as a
# School talk, not General, per the program's framing.)
_GENERAL_TITLE_RE = re.compile(
    r"^\s*(opening|closing\b)", re.I)

# In-session coffee breaks are folded into the session as agenda talks (not
# session splits). They are classified as Event (rose), same as the other
# housekeeping items.
_BREAK_TITLE_RE = re.compile(r"^\s*(coffee break|break\b)", re.I)

# Titles that are School talks regardless of their (short) slot length: named
# lectures (e.g. an opening named lecture) and memorial lectures that belong to
# the School program rather than being General housekeeping items. The generic
# "in memoriam" memorial marker is built in; any program-specific title patterns
# are read at runtime from an optional data file so no real talk title lives in
# tracked source (one regex per line, '#' comments and blanks ignored):
#
#     DATA_DIR / "school_title_patterns.txt"
def _load_school_title_re() -> "re.Pattern":
    pats = [r"in memoriam\b"]
    path = DATA_DIR / "school_title_patterns.txt"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                pats.append(line)
    return re.compile("|".join(pats), re.I)


_SCHOOL_TITLE_RE = _load_school_title_re()


# Some overview slots list two names for one session — a primary topic plus a
# secondary "(… session)" label that should be appended in parentheses rather
# than split off into its own session. Which names count as secondary is
# program-specific (and may include a person's name), so the matching substrings
# are read at runtime from an optional data file (case-insensitive substring
# match, one per line, '#' comments and blanks ignored; absent file -> nothing
# is treated as secondary):
#
#     DATA_DIR / "secondary_session_names.txt"
def _load_secondary_session_markers() -> list[str]:
    path = DATA_DIR / "secondary_session_names.txt"
    if not path.exists():
        return []
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line.lower())
    return out


_SECONDARY_SESSION_MARKERS = _load_secondary_session_markers()


def _is_secondary_session_name(name: str) -> bool:
    low = (name or "").lower()
    return any(m in low for m in _SECONDARY_SESSION_MARKERS)


def _classify_talk(title: str, start: str, end: str,
                   phase: str) -> tuple[str, str]:
    """Return (color_id, type_label) for a talk under the standard taxonomy,
    using both the slot length AND which program PHASE the talk is in
    ('school' for Mon–Wed, 'workshop' for Thu–Fri):

        Event        (rose)    — Opening / Closing / memorial, by title.
        Tutorial     (fuchsia) — substantive lectures during the SCHOOL phase
                                (the >= 30-min school lectures), plus the named
                                opening School talk.
        Invited      (indigo)  — the >= 30-min invited talks during the WORKSHOP
                                phase.
        Contributed  (sky)     — the short (< 30-min) talks in either phase.

    (Posters are classified separately, in the poster pass.) The School vs.
    Invited distinction follows the program's color caption — "invited talk for
    the school" vs "invited talk for the Workshop" — which maps onto the
    school/workshop phase of the program.
    """
    clean = _strip_emphasis(title or "")
    if _SCHOOL_TITLE_RE.search(clean):
        return "fuchsia", "Tutorial"
    if _BREAK_TITLE_RE.search(clean):
        return "rose", "Event"
    if _GENERAL_TITLE_RE.search(clean):
        return "rose", "Event"
    try:
        sh, sm = (int(x) for x in start.split(":"))
        eh, em = (int(x) for x in end.split(":"))
        dur = (eh * 60 + em) - (sh * 60 + sm)
        if dur < 0:
            dur += 24 * 60
    except Exception:
        dur = 30
    if dur >= 30:
        # Substantive (invited-level) lecture: its label depends on the phase.
        return ("fuchsia", "Tutorial") if phase == "school" else ("indigo", "Invited")
    return "sky", "Contributed"


# -----------------------------------------------------------------------------
# Parsing the schedule (timed blocks) and the poster list.
# -----------------------------------------------------------------------------
def _segment(text: str) -> tuple[list[dict], list[list[str]]]:
    """Split the program text into (timed_blocks, poster_blocks).

    timed_blocks: each {day_dom, day_month, start, end, lines:[...]} where lines
                  are the content lines after the time (title, authors, affs).
    poster_blocks: each a list of lines [title, authors, affs...].
    """
    lines = text.split("\n")

    # Where does the poster list begin? Everything after it is posters.
    poster_start = next(
        (i for i, l in enumerate(lines)
         if l.strip().upper().startswith("LIST OF POSTERS")),
        len(lines))

    timed: list[dict] = []
    cur_dom = cur_month = None
    cur: dict | None = None
    for i in range(poster_start):
        s = lines[i].strip()
        if not s:
            continue
        dm = DAY_RE.match(s)
        if dm and dm.group("mon").lower() in MONTHS:
            cur_dom = int(dm.group("dom"))
            cur_month = MONTHS[dm.group("mon").lower()]
            continue
        tm = TIME_RE.match(s)
        if tm and cur_dom is not None:
            if cur:
                timed.append(cur)
            rest = _clean(tm.group("rest"))
            cur = {"day_dom": cur_dom, "day_month": cur_month,
                   "start": tm.group("s"), "end": tm.group("e"),
                   "lines": [rest] if rest else []}
        elif cur is not None:
            cur["lines"].append(s)
    if cur:
        timed.append(cur)

    # Posters: bullet-delimited blocks.
    posters: list[list[str]] = []
    pcur: list[str] | None = None
    for i in range(poster_start, len(lines)):
        s = lines[i].strip()
        if not s:
            continue
        if s.upper().startswith("LIST OF POSTERS"):
            continue
        if s.lower().startswith("poster size"):
            continue
        pm = POSTER_RE.match(s)
        if pm:
            if pcur:
                posters.append(pcur)
            pcur = [_clean(pm.group("title"))]
        elif pcur is not None:
            pcur.append(s)
    if pcur:
        posters.append(pcur)

    return timed, posters


def _poster_title_hash(title: str) -> str:
    """Stable 10-hex-char hash of a poster title, folded first to lowercase
    letters/digits only (so punctuation, spacing, accents and subscripts don't
    perturb it). Used to pin the printed-program board order in POSTER_BOARD_ORDER
    without storing any title text. Must stay in sync with how that table was
    generated; if the abstract titles change, regenerate the table."""
    key = re.sub(r"[^a-z0-9]+", "", (title or "").lower())
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def _block_to_talk(block_lines: list[str]) -> dict | None:
    """Turn a block's content lines into a parsed talk dict, or None if the
    block is a non-academic agenda item (lunch, break, poster session, …).

    Returns {title, authors, author_aliases, institutions, speaker, presenter,
             speaker_pos, first_author, last_author}.
    """
    if not block_lines:
        return None
    title_line = _strip_emphasis(block_lines[0])
    if not title_line:
        return None
    if any(rx.search(title_line) for rx in _NON_TALK_RE):
        return None

    rest = list(block_lines[1:])

    # Normalize the "General" housekeeping titles for clean display.
    if re.match(r"^opening\b", title_line, re.I):
        title_line = "Opening remarks"
    elif re.match(r"^closing\b", title_line, re.I):
        title_line = "Closing remarks"
    elif re.match(r"^(coffee break|break)\b", title_line, re.I):
        title_line = "Coffee Break"

    # A single-line block (title only) with no people = agenda item; skip unless
    # it clearly names a speaker glued on. Try the glued split on the title too.
    title, glued_aff = _split_glued_name_aff(title_line)
    # The title rarely contains the affiliation; only accept a glued split when
    # what's left looks like a real (longish) title. Otherwise keep whole line.
    if glued_aff and len(title.split()) < 3:
        title, glued_aff = title_line, ""

    author_line = ""
    aff_lines: list[str] = []
    if glued_aff:
        # Unusual; treat the rest as affiliations.
        aff_lines = rest
        author_line = ""  # speaker unknown from a glued-title case
    elif rest:
        # First non-empty rest line is normally the author list; the glued
        # name+affiliation single-author case is handled here too.
        first = rest[0]
        a_part, aff_part = _split_glued_name_aff(first)
        if aff_part:
            author_line = a_part
            aff_lines = ([aff_part] + rest[1:]) if len(rest) > 1 else [aff_part]
        else:
            if _looks_like_affiliation(first) and not _looks_like_authors(first):
                # No authors given, only an affiliation (rare).
                aff_lines = rest
            else:
                author_line = first
                aff_lines = rest[1:]

    institutions = _parse_institutions(aff_lines)
    authors, aliases, speaker_idx = _parse_authors(author_line, institutions)

    # The presenting author is the one underlined in the source (carried here as
    # speaker_idx). It is NOT always the first author — e.g. a senior author can
    # be listed first while a student/postdoc presents — so honor the underline
    # and fall back to author 0 only when the source left no marker.
    spk = speaker_idx if (speaker_idx is not None and authors) else 0
    speaker = authors[spk]["name"] if authors else ""
    # Byline convention the builder expects (see legacyTalkByline in
    # build_conference_app.py): it renders "first … last". So `last_author` must
    # be EMPTY when there is only one author — otherwise the same name is shown
    # twice ("Strasser…Strasser"). Only set last_author for 2+ authors.
    # first/last_author follow AUTHOR order (not the speaker), so the byline
    # still reads "first … last" with the speaker underlined wherever it sits.
    first_author = authors[0]["name"] if authors else ""
    last_author = authors[-1]["name"] if len(authors) > 1 else ""
    return {
        "title": title,
        "authors": authors,
        "author_aliases": aliases,
        "institutions": institutions,
        "speaker": speaker,
        "presenter": speaker,
        "speaker_pos": spk if authors else None,
        "first_author": first_author,
        "last_author": last_author,
    }


def parse_overview(text: str) -> dict[tuple[int, int, str], dict]:
    """Parse the 'Program at a Glance' overview grid into a lookup:

        {(month, dom, 'AM'|'PM'): {'names': [str, ...], 'phase': 'school'|'workshop'}}

    The page is a day-by-time grid. When flattened to visible text (the
    innerText the fetch script saves) it reads, in order: the "PROGRAM AT A
    GLANCE" heading; the day headers (each day name and its DD/MM on SEPARATE
    lines); then the MORNING, MID-DAY, AFTERNOON and EVENING rows. Within
    MORNING and AFTERNOON each session is introduced by a "School" or "Workshop"
    tag line, followed by its name on one or more lines (long names wrap), until
    the next tag or the next section. Cells appear in day-column order; empty
    cells (e.g. Friday afternoon, or Monday morning's tagless "Arrival") simply
    don't carry a School/Workshop session.

    Returns {} if the text doesn't look like the overview, so the caller falls
    back to generic "School N"/"Workshop N" titles.
    """
    if not text or "PROGRAM AT A GLANCE" not in text.upper():
        return {}
    lines = [l.strip() for l in text.split("\n")]

    # ---- day columns: name and DD/MM may be on one line OR two lines ----
    DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                 "Saturday", "Sunday")
    one_line = re.compile(
        r"^(%s)\s+(\d{1,2})/(\d{1,2})\b" % "|".join(DAY_NAMES), re.I)
    date_only = re.compile(r"^(\d{1,2})/(\d{1,2})$")
    days: list[tuple[int, int]] = []   # (month, dom) in column order
    i = 0
    while i < len(lines):
        l = lines[i]
        m = one_line.match(l)
        if m:
            days.append((int(m.group(3)), int(m.group(2))))
        elif l in DAY_NAMES and i + 1 < len(lines):
            dm = date_only.match(lines[i + 1])
            if dm:
                days.append((int(dm.group(2)), int(dm.group(1))))
                i += 1
        i += 1
    if not days:
        return {}

    SECTIONS = {"MORNING", "MID-DAY", "AFTERNOON", "EVENING"}
    TAGS = {"school", "workshop"}

    def _section_slice(name: str) -> list[str]:
        """Lines strictly between section `name` and the next section header."""
        try:
            a = next(k for k, l in enumerate(lines) if l.upper() == name)
        except StopIteration:
            return []
        b = len(lines)
        for k in range(a + 1, len(lines)):
            if lines[k].upper() in SECTIONS:
                b = k
                break
        return lines[a + 1:b]

    def _cells(name: str) -> list[dict]:
        """Tag-anchored session cells in a MORNING/AFTERNOON section. Each
        "School"/"Workshop" tag opens a cell; subsequent non-tag lines are its
        (possibly wrapped) name, joined into one string, until the next tag."""
        cells: list[dict] = []
        cur: dict | None = None
        for l in _section_slice(name):
            if not l:
                continue
            if l.lower() in TAGS:
                if cur:
                    cells.append(cur)
                cur = {"phase": l.lower(), "parts": []}
            elif cur is not None:
                cur["parts"].append(l)
            # lines before the first tag (e.g. "Arrival") are ignored
        if cur:
            cells.append(cur)
        # Collapse each cell's wrapped name parts. A blank-ish part naming a
        # secondary "(… session)" label is a SECOND name in the same cell;
        # detect that the page lists it as its own emphasized heading by keeping
        # parts whole only when they read as continuation fragments. We treat
        # every part as a separate name candidate, then re-join fragments that
        # are clearly wrapped (start lowercase / are connective) into the prior.
        for c in cells:
            names: list[str] = []
            for p in c["parts"]:
                p = _clean(p)
                if not p:
                    continue
                # A fragment that begins with a lowercase word or a connective
                # ("for", "and", "of"…) is a wrapped continuation of the prior
                # name; otherwise it's a new name in the same cell.
                first = p.split()[0].lower() if p.split() else ""
                if names and (first in ("for", "and", "of", "the", "&")
                              or p[:1].islower()):
                    names[-1] = f"{names[-1]} {p}"
                else:
                    names.append(p)
            c["names"] = names
            del c["parts"]
        return cells

    out: dict[tuple[int, int, str], dict] = {}
    # MORNING cells map to the days that HAVE a tagged morning session, in
    # column order. Monday's morning is the tagless "Arrival" (no cell), so the
    # first tagged cell belongs to the first day after Monday, etc. We align
    # cells to days by skipping days whose morning is untagged: simplest correct
    # rule for this program — Monday has no morning session, every other day
    # does — so morning cells map to days[1:].
    morning = _cells("MORNING")
    morning_days = days[1:] if len(morning) == len(days) - 1 else days
    for (month, dom), cell in zip(morning_days, morning):
        out[(month, dom, "AM")] = {"names": cell["names"],
                                    "phase": cell["phase"]}

    # AFTERNOON cells map to the days that have an afternoon session, in column
    # order. Friday ends at lunch (no afternoon), so cells map to days[:-1] when
    # there's exactly one fewer cell than days.
    aft = _cells("AFTERNOON")
    aft_days = days[:-1] if len(aft) == len(days) - 1 else days
    for (month, dom), cell in zip(aft_days, aft):
        out[(month, dom, "PM")] = {"names": cell["names"],
                                    "phase": cell["phase"]}

    return out


def build_conference_data() -> dict:
    text = _load_program_text()
    timed, posters = _segment(text)
    print(f"[process] parsed {len(timed)} timed blocks, "
          f"{len(posters)} poster entries.", flush=True)

    # Optional: the overview grid gives per-session NAMES and the School/Workshop
    # PHASE for each (day, morning/afternoon). If it's absent or unparseable we
    # fall back to generic "School N"/"Workshop N" titles and a date-based phase.
    overview: dict[tuple[int, int, str], dict] = {}
    overview_text = _load_overview_text()
    if overview_text:
        try:
            overview = parse_overview(overview_text)
            print(f"[process] overview: {len(overview)} named session slots.",
                  flush=True)
        except Exception as e:
            print(f"[process] overview parse failed ({e}); using generic "
                  "session titles.", flush=True)
    else:
        print("[process] no overview page; using generic session titles.",
              flush=True)

    # Optional: short abstracts harvested from the manually-supplied book of
    # abstracts. Empty dict when the PDF is absent — talks then emit with
    # abstract: "" just like before this feature was added.
    abstracts = _load_abstracts()

    sessions: list[dict] = []
    talks: list[dict] = []
    _talk_by_id: dict[str, dict] = {}   # tid -> talk dict, for late session_id fill

    # Affiliation source pools the builder/affiliation-map learn from.
    aff_full_lines: set[str] = set()
    inst_strings: set[str] = set()

    def _record_affs(institutions: list[dict]) -> None:
        for inst in institutions:
            nm = (inst.get("name") or "").strip()
            if nm:
                aff_full_lines.add(nm)
                inst_strings.add(nm)
        if institutions:
            inst_strings.add(
                "; ".join((i.get("name") or "").strip() for i in institutions))

    # ---- group timed blocks by day -> one session per day ----
    # Preserve first-seen day order.
    day_order: list[tuple[int, int]] = []
    day_blocks: dict[tuple[int, int], list[dict]] = {}
    for b in timed:
        key = (b["day_month"], b["day_dom"])
        if key not in day_blocks:
            day_blocks[key] = []
            day_order.append(key)
        day_blocks[key].append(b)

    weekday_name = {
        0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday",
        4: "Friday", 5: "Saturday", 6: "Sunday",
    }
    import datetime as _dt
    from collections import Counter

    # Real scheduled poster-session slots, captured as we walk the program, so
    # the poster catalog can be anchored to actual times instead of a guess.
    poster_slots: list[tuple[str, str]] = []   # (start_ts, end_ts)

    # PHASE + session NAMES come from the overview grid. The overview tags each
    # morning/afternoon as School or Workshop, but the actual School→Workshop
    # transition on Wednesday happens MID-MORNING (at the 11:30 session, after
    # the coffee break) — earlier than the lunch split. So we keep an explicit
    # cutoff timestamp for the phase, and a session run is broken whenever the
    # phase changes between consecutive talks (not only at meal dividers). This
    # yields, on Wednesday: a School morning run (09:00–11:00) and a Workshop
    # pre-lunch run (11:30–12:30), then the Workshop post-lunch run (13:30–).
    WORKSHOP_START_TS = _iso(1, 7, "11:30")   # 2026-07-01T11:30:00

    def _half_of(start_ts: str | None) -> str:
        return "AM" if (start_ts or "")[11:13] < "12" else "PM"

    def _phase_of(start_ts: str | None) -> str:
        return "workshop" if (start_ts or "") >= WORKSHOP_START_TS else "school"

    # Session NAME lookup. Normally a session's name is its overview half
    # (AM->morning name, PM->afternoon name). The one wrinkle is Wednesday's
    # pre-lunch Workshop run: it sits in the AM half by the clock but belongs to
    # the Workshop phase, so it should take the AFTERNOON ("…combs") name, not
    # the morning ("Unipolar devices…") one. We detect that mismatch — a slot
    # whose clock-half is AM but whose phase is Workshop (or PM but School) — and
    # read the OTHER half's names for it.
    def _slot_names(month: int, dom: int, half: str) -> list[str]:
        info = overview.get((month, dom, half))
        return list(info["names"]) if info else []

    def _names_for(start_ts: str | None) -> list[str]:
        ts = start_ts or ""
        if not ts:
            return []
        month, dom = int(ts[5:7]), int(ts[8:10])
        half = _half_of(ts)
        # Detect exactly that window — an AM block whose START TIME-OF-DAY is at
        # or after the 11:30 cutoff (i.e. a late-morning Workshop run, not a
        # normal ~09:00 morning start) — and read the PM slot's names. A Workshop
        # morning that begins at the normal 09:00 start (e.g. Thursday
        # "Applications") starts before 11:30 and keeps its own AM name.
        if half == "AM" and _phase_of(ts) == "workshop" \
                and ts[11:16] >= WORKSHOP_START_TS[11:16]:
            half = "PM"
        return _slot_names(month, dom, half)

    talk_seq = 0
    sess_seq = 0
    school_no = 0       # continuous count of School technical sessions
    workshop_no = 0     # continuous count of Workshop technical sessions
    for (month, dom) in day_order:
        blocks = day_blocks[(month, dom)]
        d = _dt.date(YEAR, month, dom)
        wd = weekday_name[d.weekday()]
        date_label = d.strftime(f"{dom:02d}-%b-{YEAR}")  # e.g. 29-Jun-2026

        # Walk the day's blocks in order. Contiguous runs of talks accumulate
        # into one TECHNICAL session, flushed when a divider (meal/break/admin/
        # poster-slot) is hit. Technical sessions are named by phase and
        # numbered continuously ("School 1" … "Workshop 1" …). Each divider
        # becomes its OWN session titled by what it is ("Lunch", …) with no talks.
        cur_talk_ids: list[str] = []
        cur_colors: list[str] = []
        cur_start_ts: str | None = None
        cur_end_ts: str | None = None

        def _flush_tech() -> None:
            """Emit the accumulated technical-talk run as one named session."""
            nonlocal cur_talk_ids, cur_colors, cur_start_ts, cur_end_ts
            nonlocal sess_seq, school_no, workshop_no
            if not cur_talk_ids:
                cur_start_ts = cur_end_ts = None
                return
            sess_seq += 1
            sid = f"S{sess_seq:03d}"
            for tid in cur_talk_ids:
                _talk_by_id[tid]["session_id"] = sid
            phase = _phase_of(cur_start_ts)
            if phase == "school":
                school_no += 1
                phase_label = f"School {school_no}"
                phase_word = "School"
                color = "fuchsia"
            else:
                workshop_no += 1
                phase_label = f"Workshop {workshop_no}"
                phase_word = "Workshop"
                color = "blue"

            # Pick the session's display NAME from the overview, keyed by phase
            # (so a pre-lunch Workshop run gets the afternoon name, not the
            # morning name). A slot may list several names for one session — a
            # primary topic plus a secondary "(… session)" label. The secondary
            # label is appended in parentheses to the primary name, so it reads
            # "<primary topic> (<secondary> session)" — one ordinary School
            # session, not a separate one.
            names = _names_for(cur_start_ts)
            name = ""
            if names:
                secondary = next((n for n in names
                                  if _is_secondary_session_name(n)), "")
                primary = next((n for n in names
                                if not _is_secondary_session_name(n)), "")
                if primary and secondary:
                    name = f"{primary} ({secondary})"
                else:
                    name = primary or secondary or names[0]

            # Title reads "School: <name>" / "Workshop: <name>". The continuous
            # "School N"/"Workshop N" counter rides along in `topic`. With no
            # overview name, fall back to just the phase label.
            if name:
                title = f"{phase_word}: {name}"
                topic = phase_label
            else:
                title = phase_label
                topic = ""
            sessions.append({
                "id": sid,
                "title": title,
                "type": phase_word,
                "topic": topic,
                "date": date_label,
                "location": "",
                "presider": "",
                "presider_aff": "",
                "details": "",
                "start_ts": cur_start_ts,
                "end_ts": cur_end_ts,
                "color": color,
                "talk_ids": list(cur_talk_ids),
            })
            cur_talk_ids = []
            cur_colors = []
            cur_start_ts = cur_end_ts = None

        def _emit_divider(label: str, start_ts: str, end_ts: str) -> None:
            """Emit a meal/break/admin block as its own (talk-less) session,
            titled by the block label only (not numbered)."""
            nonlocal sess_seq
            sess_seq += 1
            sessions.append({
                "id": f"S{sess_seq:03d}",
                "title": label,
                "type": "General",
                "topic": "",
                "date": date_label,
                "location": "",
                "presider": "",
                "presider_aff": "",
                "details": "",
                "start_ts": start_ts,
                "end_ts": end_ts,
                "color": "rose",
                "talk_ids": [],
            })

        for b in blocks:
            start_ts = _iso(dom, month, b["start"])
            end_ts = _iso(dom, month, b["end"])
            first_line = b["lines"][0] if b["lines"] else ""
            kind = _block_kind(first_line)

            if kind in ("poster_slot", "divider"):
                # End any open technical run, THEN emit this divider as its own
                # session so the day reads in order: …#1, Coffee Break, #2, …
                _flush_tech()
                if kind == "poster_slot":
                    # Record the real slot time for the poster CATALOG session
                    # (built below); don't also emit an empty "Poster Session"
                    # divider here, which would duplicate the catalog.
                    poster_slots.append((start_ts, end_ts))
                    continue
                _emit_divider(_divider_label(first_line), start_ts, end_ts)
                continue

            parsed = _block_to_talk(b["lines"])
            if parsed is None:
                # Classifier said talk but parser disagreed; close the run so we
                # never fold a garbage block into a technical session.
                _flush_tech()
                continue

            # Break the run when the School↔Workshop phase changes between
            # consecutive talks, so the mid-morning Wednesday transition (at
            # 11:30) starts a fresh session even though no meal divides them.
            if cur_talk_ids and _phase_of(start_ts) != _phase_of(cur_start_ts):
                _flush_tech()

            talk_seq += 1
            color, _label = _classify_talk(
                parsed["title"], b["start"], b["end"], _phase_of(start_ts))
            tid = f"T{talk_seq:03d}"
            cur_talk_ids.append(tid)
            cur_colors.append(color)
            if cur_start_ts is None or start_ts < cur_start_ts:
                cur_start_ts = start_ts
            if cur_end_ts is None or end_ts > cur_end_ts:
                cur_end_ts = end_ts
            _record_affs(parsed["institutions"])
            t = {
                "id": tid,
                "session_id": "",   # filled in by _flush_tech
                "title": parsed["title"],
                "number": "",
                "start_ts": start_ts,
                "end_ts": end_ts,
                "presenter": parsed["presenter"],
                "speaker": parsed["speaker"],
                "speaker_pos": parsed["speaker_pos"],
                "authors": parsed["authors"],
                "author_aliases": parsed["author_aliases"],
                "institutions": parsed["institutions"],
                "institutions_may_dedup": False,
                "abstract": (
                    abstracts.get(_norm_title_for_match(parsed["title"]))
                    or abstracts.get(_alt_title_fingerprint(parsed["title"]))
                    or abstracts.get(_title_prefix_key(parsed["title"]))
                    or ""),
                "status": "Sessioned",
                "withdrawn": False,
                "first_author": parsed["first_author"],
                "last_author": parsed["last_author"],
                "color": color,
                "location": "",
            }
            talks.append(t)
            _talk_by_id[tid] = t

        _flush_tech()   # end-of-day: flush any trailing technical run

    # Suffix-number any session NAME shared by more than one technical session
    # (e.g. a split Workshop run on one day -> "Topic name 1" / "Topic name 2").
    # Sessions keep first-seen order; titles that are unique are left untouched.
    from collections import Counter as _Counter
    _tech = [s for s in sessions if s["type"] in ("School", "Workshop")]
    _title_counts = _Counter(s["title"] for s in _tech)
    _seen: dict[str, int] = {}
    for s in _tech:
        if _title_counts[s["title"]] > 1:
            _seen[s["title"]] = _seen.get(s["title"], 0) + 1
            s["title"] = f"{s['title']} {_seen[s['title']]}"

    # ---- posters: split the catalog into TWO sessions, one per scheduled
    #      poster slot. The program lists all posters together without saying
    #      which evening each is shown, so we split the list in half: the first
    #      half goes to the first slot, the second half to the second. (If only
    #      one slot was found, everything goes there.) ----
    if posters:
        def _slot_label(ts: tuple[str, str]) -> str:
            s, e = ts
            dd = _dt.date.fromisoformat(s[:10])
            return f"{weekday_name[dd.weekday()]} {s[11:16]}–{e[11:16]}"

        # Establish the slot times (start, end), in chronological order.
        if poster_slots:
            slots = sorted(poster_slots)
        else:
            p_dom, p_month = ((day_order[-1][1], day_order[-1][0])
                              if day_order else (3, 7))
            slots = [(_iso(p_dom, p_month, "17:00"),
                      _iso(p_dom, p_month, "19:00"))]

        # Parse all posters once (skipping any that don't yield a title).
        parsed_posters = []
        for idx, pblock in enumerate(posters, 1):
            parsed = _block_to_talk(pblock)
            if parsed and parsed["title"]:
                parsed_posters.append((idx, parsed))

        # Partition the catalog into one group per evening slot, in slot
        # (chronological) order. POSTER_BOARD_ORDER pins the printed program's
        # day + board order by title hash — authoritative because no online
        # source records it. We pair each pinned session with a slot by position
        # (both chronological: first pinned session -> earliest slot) and bucket
        # each poster by its title hash. If the pinned table doesn't cover the
        # parsed catalog one-to-one, fall back to splitting the alphabetical list
        # in half so the build still succeeds.
        n_slots = len(slots)
        groups = None
        pinned_total = sum(len(b) for b in POSTER_BOARD_ORDER)
        if len(POSTER_BOARD_ORDER) == n_slots and pinned_total == len(parsed_posters):
            pos_by_hash = {h: (si, pos)
                           for si, board in enumerate(POSTER_BOARD_ORDER)
                           for pos, h in enumerate(board)}
            buckets: list[list] = [[] for _ in slots]   # (pos, idx, parsed)
            ok = True
            seen: set[str] = set()
            for idx, parsed in parsed_posters:
                h = _poster_title_hash(parsed["title"])
                loc = pos_by_hash.get(h)
                if loc is None or h in seen:
                    ok = False
                    break
                seen.add(h)
                si, pos = loc
                buckets[si].append((pos, idx, parsed))
            if ok and len(seen) == len(parsed_posters):
                groups = [[(idx, parsed) for _pos, idx, parsed in sorted(b)]
                          for b in buckets]
            else:
                print("[process] poster hash table did not match the poster "
                      "catalog one-to-one; falling back to the half-split.",
                      flush=True)
        if groups is None:
            per = -(-len(parsed_posters) // n_slots)   # ceil division
            groups = [parsed_posters[i:i + per]
                      for i in range(0, len(parsed_posters), per)] or [[]]
            # Guard: never produce more groups than slots (rounding safety).
            while len(groups) > n_slots:
                groups[-2].extend(groups[-1])
                groups.pop()

        board_no = 0   # 1-based board number, running across sessions in order
        for gi, group in enumerate(groups):
            if not group:
                continue
            base_start, base_end = slots[gi] if gi < n_slots else slots[-1]
            sess_id = f"POSTERS{gi + 1}"
            tids: list[str] = []
            for _idx, parsed in group:
                board_no += 1
                # The id encodes board order so it sorts correctly within its
                # session (the builder orders same-time talks by id).
                tid = f"P{board_no:03d}"
                tids.append(tid)
                _record_affs(parsed["institutions"])
                talks.append({
                    "id": tid,
                    "session_id": sess_id,
                    "title": parsed["title"],
                    "number": f"P{board_no}",
                    "start_ts": base_start,
                    "end_ts": base_end,
                    "presenter": parsed["presenter"],
                    "speaker": parsed["speaker"],
                    "speaker_pos": parsed["speaker_pos"],
                    "authors": parsed["authors"],
                    "author_aliases": parsed["author_aliases"],
                    "institutions": parsed["institutions"],
                    "institutions_may_dedup": False,
                    "abstract": (
                        abstracts.get(_norm_title_for_match(parsed["title"]))
                        or abstracts.get(
                            _alt_title_fingerprint(parsed["title"]))
                        or abstracts.get(_title_prefix_key(parsed["title"]))
                        or ""),
                    "status": "Sessioned",
                    "withdrawn": False,
                    "first_author": parsed["first_author"],
                    "last_author": parsed["last_author"],
                    "color": "teal",
                    "location": "",
                })
            label = (_slot_label(slots[gi]) if gi < n_slots else "")
            title = (f"Poster Session {gi + 1}" if len(groups) > 1
                     else "Poster Session")
            sessions.append({
                "id": sess_id,
                "title": title,
                "type": "Posters",
                "topic": label,
                "date": _dt.date.fromisoformat(base_start[:10]).strftime(
                    f"%d-%b-{YEAR}"),
                "location": "",
                "presider": "",
                "presider_aff": "",
                "details": (f"{label}. " if label else "")
                           + "Poster size is A0 vertical. "
                           "Clips for hanging are provided on site.",
                "start_ts": base_start,
                "end_ts": base_end,
                "color": "teal",
                "talk_ids": tids,
            })

    # If the book of abstracts is available, REPLACE the website-derived
    # session structure with the book's Scientific Program grid. The book
    # carries finer session boundaries (each "Summer School I" / "Workshop I"
    # as its own session, not merged into a per-half-day block) and the
    # chair names the website doesn't publish.
    book_days = _parse_book_program(ABSTRACT_BOOK_IN)
    if book_days:
        sessions, talks = _restructure_sessions_from_book(
            book_days, sessions, talks)
        print(f"[process] book program     : restructured into "
              f"{len(sessions)} sessions from the abstract book.",
              flush=True)

    # Pool every affiliation source into one flat, de-duplicated, sorted list for
    # the builder's affiliation map (this program has no presiders). Full-address
    # lines are kept whole; the institution strings may be ';'-joined lists, so
    # split them here at the source.
    affiliation_pool: set[str] = set(aff_full_lines)
    for _v in inst_strings:
        for _piece in _v.split(";"):
            _p = _piece.strip()
            if _p:
                affiliation_pool.add(_p)

    # Conference-code split: this conference assigns no human-facing codes of its
    # own. The poster `number` ("P1", "P2", …) is fabricated HERE, not a real
    # program code, so we deliberately do NOT promote it to `code`; like every
    # other talk, posters get `code = ""` and the builder synthesizes a display
    # code ("<sessioncode>.<n>") in _resolve_display_codes_and_ids. Sessions are
    # likewise left empty for the builder to synthesize.
    for _s in sessions:
        _s["code"] = ""
    for _t in talks:
        _t["code"] = ""

    data = {
        "conference_name": CONFERENCE_NAME,
        "sessions": sessions,
        "talks": talks,
        "session_types": SESSION_TYPES,
        "talk_types": TALK_TYPES,
        "affiliation_sources": sorted(affiliation_pool),
    }
    return data


def _collapse_session_tags(sessions):
    """Collapse each session's legacy ``type``/``topic`` into an ordered list of
    labelled ``tags`` ({"key", "value"} pairs), shown in the app as
    "Key: Value · Key: Value". Redundant topics are dropped: empty,
    identical to the session id, or merely restating the format."""
    for s in sessions:
        fmt = (s.pop("type", None) or "").strip()
        topic = (s.pop("topic", None) or "").strip()
        tags = []
        if fmt:
            tags.append({"key": "Format", "value": fmt})
        tl, fl = topic.casefold(), fmt.casefold()
        redundant = (
            not topic
            or tl == str(s.get("id", "")).casefold()
            or (bool(fl) and (tl == fl or tl.startswith(fl)))
        )
        if not redundant:
            head = topic.split(":", 1)[0].strip()
            if ":" in topic and head and " " not in head:
                k, v = topic.split(":", 1)
                tags.append({"key": k.strip(), "value": v.strip()})
            else:
                tags.append({"key": "Track", "value": topic})
        if tags:
            s["tags"] = tags
    return sessions


def main() -> None:
    data = build_conference_data()
    # This conference carries no session tags: the legacy type ("School"/"Workshop"/
    # "General"/"Posters") restates the title, and the topic was a bare
    # ordinal. Strip both so sessions emit no tags line.
    for _s in data["sessions"]:
        _s.pop("type", None)
        _s.pop("topic", None)
        _s.pop("tags", None)
    # Tag each matched talk with its source PDF + page range
    # (`paper: {file, pages}`); the builder slices and embeds it. No-op
    # when the book is missing, pdfplumber isn't installed, or no
    # paper-start pages are detected — talks then emit without `paper`.
    _attach_paper_pages(data["talks"])
    JSON_OUT.write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    n_t = len(data["talks"])
    n_s = len(data["sessions"])
    n_auth = sum(len(t["authors"]) for t in data["talks"])
    print(f"[process] wrote {JSON_OUT.name}: {n_s} sessions, {n_t} talks, "
          f"{n_auth} author entries, "
          f"{len(data['affiliation_sources'])} "
          f"affiliation strings.", flush=True)


if __name__ == "__main__":
    main()
