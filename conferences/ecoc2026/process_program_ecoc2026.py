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

"""process_program_ecoc2026.py — OFFLINE PROCESSING ONLY.

Reads the files the downloader left in data/ and emits conference_data.json in
the schema of docs/CONFERENCE_JSON.md. No network access happens here, and no
program content is embedded in this file — everything is parsed at runtime.

Input shape (see data_requirements_ecoc2026.txt):

    programme_sessions.json    one record per session: day, time, room, track
    session_speakers.json      {session id: [chair / organizer / speaker rows]}
    abstracts.json             {abstract id: summary text + full author list}
    programme_page.html        read only for its <title> (conference name)
    programme_categories.json  the track registry
    page_*.html                prose the planner has no field for

How the two levels are recovered
--------------------------------
The planner models a session as a row in the session list and everything under
it as a "person" row carrying a role. A row's `role` is either "moderator" (a
chair or an organizer — no talk of their own) or "speaker" (one talk). So
sessions come from the session list, talks come from the speaker rows, and the
moderator rows become the session's presiders.

Types are read off each person row's role label, which is the one place the
planner records what kind of item a talk is. The labels map onto the standard
taxonomy as follows:

    Contributed      -> Contributed talk
    Invited          -> Invited talk
    Tutorial         -> Tutorial talk
    Poster           -> Poster talk
    Plenary Speaker  -> Plenary talk
    Panelist         -> Event talk-row
    Exhibition       -> Event talk-row
    Chair, Organizer -> not a talk; becomes a session presider

A session's own type is then derived from the talks it holds: a session of
posters is a Poster session, one holding the plenary lectures is a Plenary
session, anything with oral talks is the standard Technical grouping, and
whatever has no talks at all is an Event (breaks, meals, receptions, and the
discussion workshops, which the planner lists with organizers but no named
child talks).

Enrichment
----------
The planner returns every session's description field empty and leaves many
speakers with no affiliation, so the saved sub-pages fill both gaps: workshop
and symposium abstracts become session `details`, and the speaker-profile pages
supply affiliations matched by name. All of it is optional; a missing page just
means the corresponding field is left off.
"""

import html as html_mod
import json
import re
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

try:
    from lxml import html as LH
except ImportError:  # enrichment pages are optional, so this is not fatal
    LH = None

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"
JSON_OUT = SCRIPT_DIR / "conference_data.json"

CURATOR = {
    "name": "Aylight",
    "affiliation": "",
    "link": "https://github.com/Aylight-io",
}

# ---------------------------------------------------------------------------
# Type registries. Tokens and RGB come from the standard taxonomy; see
# scripts/AGENTS.md. Sessions use Technical / Plenary / Poster / Event; talks
# use Invited / Contributed / Tutorial / Poster / Plenary / Event.
# ---------------------------------------------------------------------------

TECHNICAL, PLENARY, POSTER = "blue", "orange", "teal"
TUTORIAL, EVENT, INVITED, CONTRIBUTED = "fuchsia", "rose", "indigo", "sky"

_RGB = {
    TECHNICAL:   ("#2563eb", "#e8efff", "#1a233d"),
    PLENARY:     ("#ea580c", "#ffedd5", "#3b1d0a"),
    POSTER:      ("#0d9488", "#d6f3ef", "#102b27"),
    TUTORIAL:    ("#c026d3", "#fae8ff", "#3a0f3f"),
    EVENT:       ("#e11d48", "#ffe1e8", "#38161f"),
    INVITED:     ("#4f46e5", "#e6e4ff", "#1d1a3d"),
    CONTRIBUTED: ("#0284c7", "#e0f2fe", "#0c2a3d"),
}


def _type_entry(token: str, label: str) -> dict:
    fg, bg_light, bg_dark = _RGB[token]
    return {"id": token, "label": label,
            "fg": fg, "bg_light": bg_light, "bg_dark": bg_dark}


SESSION_TYPES = [
    _type_entry(TECHNICAL, "Technical"),
    _type_entry(PLENARY, "Plenary"),
    _type_entry(POSTER, "Poster"),
    _type_entry(EVENT, "Event"),
]
TALK_TYPES = [
    _type_entry(INVITED, "Invited"),
    _type_entry(CONTRIBUTED, "Contributed"),
    _type_entry(TUTORIAL, "Tutorial"),
    _type_entry(POSTER, "Poster"),
    _type_entry(PLENARY, "Plenary"),
    _type_entry(EVENT, "Event"),
]

# Planner role label -> talk color token. Labels are generic genre names, not
# program content. Anything unrecognized falls back to Contributed.
ROLE_TO_TALK_TYPE = {
    "contributed": CONTRIBUTED,
    "invited": INVITED,
    "tutorial": TUTORIAL,
    "poster": POSTER,
    "plenary speaker": PLENARY,
    "plenary": PLENARY,
    "panelist": EVENT,
    "exhibition": EVENT,
}

# Role labels that mark a person as a presider rather than a presenter.
PRESIDER_ROLES = {"chair", "co-chair", "organizer", "organiser", "moderator"}

# Inline markup the app renders; everything else is stripped from abstracts.
KEEP_TAGS = {"sup", "sub", "i", "b", "em", "strong"}

# A status-only marker carries no content of its own (see AGENTS.md).
WITHDRAWN_RE = re.compile(
    r"\b(withdrawn|cancell?ed|no\s*show)\b", re.IGNORECASE)

MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}


# ---------------------------------------------------------------------------
# Small text helpers
# ---------------------------------------------------------------------------

def _load_json(name: str, default=None):
    path = DATA_DIR / name
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
    except (ValueError, OSError):
        return default


def _norm_space(text: str) -> str:
    """Collapse whitespace, including the non-breaking spaces the source uses."""
    if not text:
        return ""
    text = text.replace("\xa0", " ").replace("​", "")
    return re.sub(r"\s+", " ", text).strip()


def _clean_text(text: str) -> str:
    """Unescape entities and normalize whitespace and stray punctuation."""
    if not text:
        return ""
    text = html_mod.unescape(text)
    text = _norm_space(text)
    # NB: do NOT join `word- word` into `word-word` here. That repair suits a
    # PDF, where a line break can split a hyphenated word, but these sources
    # are HTML and JSON with no such splitting — the pattern only ever occurs
    # as a legitimate suspended hyphen ("intra- and inter-channel"), which
    # joining would corrupt.
    return text.strip(" ;,")


def _strip_markup(fragment: str) -> str:
    """Render an HTML fragment as text, keeping only the inline tags the app
    renders. Block tags become spaces; everything else is dropped."""
    if not fragment:
        return ""
    text = re.sub(r"(?i)<\s*br\s*/?\s*>", " ", fragment)
    text = re.sub(r"(?i)</\s*(p|div|li|tr)\s*>", " ", text)

    def _tag(match: re.Match) -> str:
        closing, name = match.group(1), match.group(2).lower()
        return f"<{closing}{name}>" if name in KEEP_TAGS else ""

    text = re.sub(r"<(/?)\s*([A-Za-z][A-Za-z0-9]*)\b[^>]*>", _tag, text)
    text = html_mod.unescape(text)
    return _norm_space(text)


def _norm_key(text: str) -> str:
    """A loose key for matching titles and names across sources."""
    text = unicodedata.normalize("NFKD", html_mod.unescape(text or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def _fix_name(name: str) -> str:
    """Tidy a personal name: collapse the doubled spaces the source inserts
    between forename and surname, and drop any trailing role annotation."""
    name = _clean_text(name)
    name = re.sub(r"\s*\((?:speaker|presenter|chair)\)\s*$", "", name,
                  flags=re.IGNORECASE)
    return name


def _parse_people_block(raw: str) -> list:
    """Split a `Last, First / Last, First` author list into display names.

    The abstract records use ` / ` between authors and `, ` inside a name, so
    the split is unambiguous — unlike the comma-joined form the session rows
    carry, where a surname cannot be told from the next forename.
    """
    people = []
    for chunk in (raw or "").split("/"):
        chunk = _clean_text(chunk)
        if not chunk:
            continue
        if "," in chunk:
            surname, _, forename = chunk.partition(",")
            name = f"{_norm_space(forename)} {_norm_space(surname)}".strip()
        else:
            name = chunk
        name = _fix_name(name)
        if name:
            people.append(name)
    return people


def _short_room(room: str) -> str:
    """Compact room label for the bubble chip.

    Rooms are published as `<code> <descriptive name>`, where the code is what
    signage and attendees actually use, so the code alone is the compact form.
    Rooms with no code fall back to their parenthetical, then to the full name.
    """
    room = _norm_space(room)
    if not room:
        return ""
    match = re.match(r"^([A-Z]{1,3}[0-9]*(?:[.\-][0-9A-Za-z]+)?)\s+\S", room)
    if match:
        return match.group(1)
    inner = re.search(r"\(([^)]+)\)", room)
    if inner:
        return _norm_space(inner.group(1))
    return room


def _split_code(title: str):
    """Split a leading program code off a session or talk title.

    Codes look like `<day><slot>-<track>` (two letters, digits, a hyphen, then
    a short track token). Titles without one return an empty code, which tells
    the builder to synthesize a friendly code from the title.
    """
    title = _norm_space(title)
    match = re.match(r"^([A-Z][a-z][0-9]+-[A-Za-z0-9]{1,4})\s+(.*)$", title)
    if match and match.group(2):
        return match.group(1), _norm_space(match.group(2))
    return "", title


def _parse_day(raw_date: str, fallback: str = "") -> str:
    """Return the YYYY-MM-DD prefix for a session's calendar day."""
    raw_date = _norm_space(raw_date)
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw_date)
    if match:
        return "-".join(match.groups())
    # `Weekday, D Month YYYY`
    match = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", fallback or raw_date)
    if match:
        day, month, year = match.groups()
        num = MONTHS.get(month.lower())
        if num:
            return f"{year}-{num:02d}-{int(day):02d}"
    return ""


def _ts(day: str, clock: str) -> str:
    """Combine a YYYY-MM-DD day and an HH:MM clock into an ISO timestamp."""
    clock = _norm_space(clock)
    if not day or not re.fullmatch(r"\d{1,2}:\d{2}", clock):
        return ""
    hour, minute = clock.split(":")
    return f"{day}T{int(hour):02d}:{minute}:00"


def _add_minutes(stamp: str, minutes: int) -> str:
    try:
        return (datetime.fromisoformat(stamp)
                + timedelta(minutes=minutes)).isoformat()
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# Enrichment pages
# ---------------------------------------------------------------------------

def _page(name: str):
    """Parse an optional sub-page into an lxml tree, or None."""
    path = DATA_DIR / f"page_{name}.html"
    if LH is None or not path.exists():
        return None
    try:
        return LH.fromstring(path.read_bytes())
    except (ValueError, OSError):
        return None


def _text_of(node) -> str:
    return _clean_text(LH.tostring(node, encoding="unicode", method="text"))


def _harvest_descriptions() -> dict:
    """Map a normalized session title -> {details, organizers[(name, aff)]}.

    Covers the pages that describe a session rather than a person: the
    workshop listing (a <details> block per workshop) and the symposium /
    special-event / demo pages (description and organizer blocks).
    """
    found = {}
    tree = _page("workshops")
    if tree is not None:
        for item in tree.find_class("workshop-item"):
            titles = item.find_class("workshop-title")
            if not titles:
                continue
            title = _text_of(titles[0])
            abstracts = item.find_class("abstract-text")
            details = _text_of(abstracts[0]) if abstracts else ""
            people = []
            for info in item.find_class("organizer-info"):
                people.extend(_split_organizer_line(info))
            if title:
                found[_norm_key(title)] = {
                    "details": details, "organizers": people, "title": title}

    for page in ("special_symposia", "special_events", "demo_zone"):
        tree = _page(page)
        if tree is None:
            continue
        for desc in tree.find_class("symposium-description"):
            title = ""
            # The heading is the nearest preceding heading element.
            for prev in desc.itersiblings(preceding=True):
                if prev.tag in ("h1", "h2", "h3", "h4", "h5", "strong", "p"):
                    candidate = _text_of(prev)
                    if candidate and len(candidate) < 200:
                        title = candidate
                        break
            body = _text_of(desc)
            if not title or not body:
                continue
            key = _norm_key(title)
            found.setdefault(key, {"details": "", "organizers": [],
                                   "title": title})
            found[key]["details"] = found[key]["details"] or body
    return found


def _split_organizer_line(node) -> list:
    """Pull (name, affiliation) pairs out of an organizer block.

    Each organizer is a bare text node followed by a span carrying their
    affiliation, with <br/> between people.
    """
    people = []
    fragment = LH.tostring(node, encoding="unicode")
    fragment = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", fragment)
    try:
        block = LH.fromstring(f"<div>{fragment}</div>")
    except ValueError:
        return people
    text = LH.tostring(block, encoding="unicode", method="text")
    affs = [_text_of(s) for s in block.find_class("organizer-affiliation")]
    lines = [ln for ln in (_clean_text(x) for x in text.split("\n")) if ln]
    for index, line in enumerate(lines):
        aff = affs[index] if index < len(affs) else ""
        name = line
        if aff and line.endswith(aff):
            name = _clean_text(line[: -len(aff)])
        if name:
            people.append((_fix_name(name), aff))
    return people


def _harvest_speaker_affiliations() -> dict:
    """Map a normalized speaker name -> affiliation, from the profile pages."""
    affiliations = {}

    def _record(name: str, aff: str) -> None:
        name, aff = _fix_name(name), _clean_text(aff)
        if name and aff:
            affiliations.setdefault(_norm_key(name), aff)

    for page in ("tutorial_speakers", "plenary_speakers"):
        tree = _page(page)
        if tree is None:
            continue
        for card in tree.find_class("speaker-card"):
            names = card.find_class("speaker-name")
            companies = card.find_class("speaker-company")
            if names and companies:
                _record(_text_of(names[0]), _text_of(companies[0]))

    tree = _page("invited_speakers")
    if tree is not None:
        for row in tree.xpath("//tr[td]"):
            names = row.find_class("speaker-name")
            affs = row.find_class("speaker-affiliation")
            if names and affs:
                _record(_text_of(names[0]), _text_of(affs[0]))
        # Some rows carry the classes on the cells rather than a wrapper.
        for name_node in tree.find_class("speaker-name"):
            parent = name_node.getparent()
            if parent is None:
                continue
            affs = parent.find_class("speaker-affiliation")
            if affs:
                _record(_text_of(name_node), _text_of(affs[0]))
    return affiliations


def _harvest_talk_titles() -> dict:
    """Map a normalized talk title -> the properly-cased form.

    Talks the planner has no abstract record for fall back to a free-text
    field that is inconsistently cased (sometimes wholly lower-case). The
    speaker-profile pages print the same titles properly cased, so they are
    used to repair the casing wherever the two refer to the same talk.
    """
    titles = {}
    for page, class_name in (("tutorial_speakers", "tutorial-title"),
                             ("invited_speakers", "talk-title"),
                             ("plenary_speakers", "talk-row")):
        tree = _page(page)
        if tree is None:
            continue
        for node in tree.find_class(class_name):
            title = re.sub(r"^Talk:\s*", "", _text_of(node),
                           flags=re.IGNORECASE)
            title = _clean_text(title)
            if title:
                titles.setdefault(_norm_key(title), title)
    return titles


def _recase_title(title: str, cased_titles: dict) -> str:
    """Repair a talk title's capitalization where the source damaged it."""
    better = cased_titles.get(_norm_key(title))
    if better:
        return better
    # Nothing in the source is capitalized at all -> sentence-case it.
    if title and not any(c.isupper() for c in title):
        return title[0].upper() + title[1:]
    return title


def _harvest_plenary_abstracts() -> dict:
    """Map a normalized talk title -> abstract, for the plenary lectures.

    Plenary talks carry no abstract id in the planner, so their profile page is
    the only source. Each card holds the talk title and an abstract panel.
    """
    abstracts = {}
    tree = _page("plenary_speakers")
    if tree is None:
        return abstracts
    for card in tree.find_class("speaker-card"):
        title = ""
        for node in card.iter():
            text = _clean_text(node.text or "")
            match = re.match(r"^Talk:\s*(.+)$", text, re.IGNORECASE)
            if match:
                title = _clean_text(match.group(1))
                break
        if not title:
            continue
        body = ""
        for panel in card.find_class("panel-body"):
            candidate = _text_of(panel)
            candidate = re.sub(r"^Abstract:?\s*", "", candidate,
                               flags=re.IGNORECASE)
            if len(candidate) > len(body):
                body = candidate
        if body:
            abstracts[_norm_key(title)] = body
    return abstracts


def _conference_name() -> str:
    """Read the display name from the saved programme page's <title>."""
    path = DATA_DIR / "programme_page.html"
    if path.exists():
        match = re.search(
            r"<title[^>]*>(.*?)</title>",
            path.read_text(encoding="utf-8", errors="replace"),
            re.IGNORECASE | re.DOTALL)
        if match:
            name = _clean_text(_strip_markup(match.group(1)))
            if name:
                return name
    return SCRIPT_DIR.name.upper()


# ---------------------------------------------------------------------------
# Abstract records
# ---------------------------------------------------------------------------

def _clean_abstract(raw: str) -> str:
    """Turn an abstract's stored HTML into the app's lightly-marked-up text."""
    text = _strip_markup(raw)
    # The stored text opens with its own bolded caption; the app supplies one.
    text = re.sub(r"^\s*<b>\s*Abstract[^<]*</b>\s*", "", text,
                  flags=re.IGNORECASE)
    text = re.sub(r"^\s*Abstract\s*(\(summary\))?\s*:?\s*", "", text,
                  flags=re.IGNORECASE)
    return _norm_space(text)


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def _session_color(talk_colors: list, has_people: bool) -> str:
    """Derive a session's type from the talks it holds."""
    if not talk_colors:
        return EVENT
    unique = set(talk_colors)
    if unique == {POSTER}:
        return POSTER
    if PLENARY in unique:
        return PLENARY
    if unique <= {EVENT}:
        return EVENT
    return TECHNICAL


def build_conference_data() -> dict:
    sessions_raw = (_load_json("programme_sessions.json", {}) or {}).get(
        "data", [])
    if not sessions_raw:
        raise SystemExit(
            "[process] ERROR: data/programme_sessions.json is missing or "
            "empty; run the downloader first.")
    speakers_raw = _load_json("session_speakers.json", {}) or {}
    abstracts_raw = _load_json("abstracts.json", {}) or {}

    descriptions = _harvest_descriptions()
    page_affiliations = _harvest_speaker_affiliations()
    plenary_abstracts = _harvest_plenary_abstracts()
    cased_titles = _harvest_talk_titles()

    sessions, talks, affiliation_sources = [], [], []

    for raw in sessions_raw:
        session_id = raw.get("sessionid")
        if session_id is None:
            continue
        day = _parse_day(raw.get("daycalendarCal", ""),
                         raw.get("daycalendar", ""))
        clock = _norm_space(raw.get("time", ""))
        start_clock, _, end_clock = clock.partition("-")
        start_ts = _ts(day, start_clock)
        end_ts = _ts(day, end_clock)

        code, title = _split_code(_clean_text(raw.get("session", "")))
        room = _norm_space(raw.get("room", ""))
        track = _clean_text(raw.get("category", ""))

        rows = speakers_raw.get(str(session_id)) or []
        rows = sorted(rows, key=lambda r: (r.get("presentationorder") or 0))

        presiders, presider_affs = [], []
        talk_rows = []
        for row in rows:
            role_label = _norm_space(row.get("rolename", "")).lower()
            is_presider = (role_label in PRESIDER_ROLES
                           or _norm_space(row.get("role", "")).lower()
                           == "moderator")
            name = _fix_name(row.get("name", ""))
            # The planner stores a person's affiliation in the same field it
            # uses for country, so treat any non-empty value as affiliation.
            aff = _clean_text(row.get("country", ""))
            if not aff:
                aff = page_affiliations.get(_norm_key(name), "")
            if is_presider:
                if name:
                    presiders.append(name)
                    presider_affs.append(aff)
                    if aff:
                        affiliation_sources.append(aff)
                continue
            if _clean_text(row.get("presentation", "")):
                talk_rows.append((row, name, aff))

        # Sessions the site describes in prose rather than as child talks.
        info = descriptions.get(_norm_key(title)) or {}
        if info.get("organizers") and not presiders:
            for name, aff in info["organizers"]:
                presiders.append(name)
                presider_affs.append(aff)
                if aff:
                    affiliation_sources.append(aff)

        session_key = f"S{session_id}"
        talk_ids, talk_colors = [], []

        for index, (row, name, aff) in enumerate(talk_rows):
            talk = _build_talk(
                row=row, index=index, speaker=name, speaker_aff=aff,
                session_key=session_key, day=day,
                session_end_ts=end_ts,
                next_row=(talk_rows[index + 1][0]
                          if index + 1 < len(talk_rows) else None),
                abstracts_raw=abstracts_raw,
                plenary_abstracts=plenary_abstracts,
                cased_titles=cased_titles,
                page_affiliations=page_affiliations,
                affiliation_sources=affiliation_sources,
            )
            if talk is None:
                continue
            talks.append(talk)
            talk_ids.append(talk["id"])
            talk_colors.append(talk["color"])

        session = {
            "id": session_key,
            "code": code,
            "title": title,
            "color": _session_color(talk_colors, bool(rows)),
            "talk_ids": talk_ids,
        }
        if start_ts:
            session["start_ts"] = start_ts
        if end_ts:
            session["end_ts"] = end_ts
        if room:
            session["location"] = room
            short = _short_room(room)
            if short and short != room:
                session["short_location"] = short
        if presiders:
            session["presider"] = "; ".join(presiders)
            if any(presider_affs):
                session["presider_aff"] = "; ".join(presider_affs)
        if info.get("details"):
            session["details"] = info["details"]
        if track:
            session["tags"] = [{"key": "Track", "value": track}]
        sessions.append(session)

    # Talk colors decide their session's color, so a Poster session's talks
    # inherit nothing further; but an Event session's stray rows should read as
    # Event rather than as oral talks.
    by_session = {s["id"]: s for s in sessions}
    for talk in talks:
        parent = by_session.get(talk["session_id"])
        if parent and parent["color"] == EVENT and talk["color"] == CONTRIBUTED:
            talk["color"] = EVENT

    affiliation_sources = sorted({a for a in affiliation_sources if a})

    return {
        "conference_name": _conference_name(),
        "curator": CURATOR,
        "sessions": sessions,
        "talks": talks,
        "session_types": SESSION_TYPES,
        "talk_types": TALK_TYPES,
        "affiliation_sources": affiliation_sources,
    }


def _build_talk(row, index, speaker, speaker_aff, session_key, day,
                session_end_ts, next_row, abstracts_raw, plenary_abstracts,
                cased_titles, page_affiliations, affiliation_sources) -> dict:
    title = _clean_text(_strip_markup(row.get("presentation", "")))
    if not title:
        return None

    # A row carrying only a status word annotates its neighbour; it is not a
    # talk of its own.
    stripped = WITHDRAWN_RE.sub("", title).strip(" ()[]-–—:;,.")
    if not stripped:
        return None
    withdrawn = bool(WITHDRAWN_RE.search(title))

    role_label = _norm_space(row.get("rolename", "")).lower()
    # An unlabelled row is a program fixture (an opening, a hand-over), not a
    # genre of talk; those read as Event.
    color = ROLE_TO_TALK_TYPE.get(role_label, CONTRIBUTED if role_label
                                  else EVENT)

    # `#<submission> / <program code>` — only the program code is user-facing.
    code = ""
    number = _norm_space(row.get("abstractnumber", ""))
    if "/" in number:
        code = _norm_space(number.split("/", 1)[1])
    elif number and not number.startswith("#"):
        code = number
    if not code:
        # Talks with no abstract record carry their code inside the title.
        code, title = _split_code(title)
    title = _recase_title(title, cased_titles)

    start_ts = _ts(day, row.get("presentationtimeststart", ""))
    end_ts = _ts(day, row.get("presentationtimeend", ""))
    if start_ts and not end_ts:
        if next_row is not None:
            end_ts = _ts(day, next_row.get("presentationtimeststart", ""))
        if not end_ts:
            end_ts = session_end_ts
        # Guard against a following item that starts before this one ends.
        if end_ts and end_ts <= start_ts:
            end_ts = _add_minutes(start_ts, 15)

    record = abstracts_raw.get(str(row.get("abstractid") or "")) or {}
    authors = _parse_people_block(record.get("abstractauthors", ""))
    if not authors:
        # The session rows carry a comma-joined `First Last` list instead.
        authors = [_fix_name(a) for a
                   in (row.get("abstractauthors") or "").split(",")
                   if _fix_name(a)]
    if not authors and speaker:
        authors = [speaker]

    abstract = _clean_abstract(record.get("abstracttext", ""))
    if not abstract:
        abstract = plenary_abstracts.get(_norm_key(title), "")

    if not speaker_aff:
        speaker_aff = page_affiliations.get(_norm_key(speaker), "")
    if speaker_aff:
        affiliation_sources.append(speaker_aff)

    speaker_pos = None
    if speaker:
        target = _norm_key(speaker)
        for position, author in enumerate(authors):
            if _norm_key(author) == target:
                speaker_pos = position
                break

    # Only the presenting author's affiliation is published, so it is the one
    # institution the talk can carry, attached to that author alone.
    institutions, author_objs = [], []
    if speaker_aff:
        institutions = [{"n": 1, "name": speaker_aff}]
    for position, author in enumerate(authors):
        insts = [1] if (institutions and position == speaker_pos) else []
        author_objs.append({"name": author, "insts": insts})
    if institutions and speaker_pos is None and speaker:
        # The speaker is not in the author list (common for a panel or an
        # invited overview); add them so the affiliation has an owner.
        author_objs.insert(0, {"name": speaker, "insts": [1]})
        authors = [speaker] + authors
        speaker_pos = 0

    talk = {
        "id": f"T{row.get('assignid') or f'{session_key}-{index}'}",
        "session_id": session_key,
        "code": code,
        "title": title,
        "color": color,
    }
    if start_ts:
        talk["start_ts"] = start_ts
    if end_ts:
        talk["end_ts"] = end_ts
    if speaker:
        talk["speaker"] = speaker
        if speaker_pos is not None:
            talk["speaker_pos"] = speaker_pos
    if authors:
        talk["first_author"] = authors[0]
        talk["last_author"] = authors[-1]
        talk["authors"] = author_objs
    if institutions:
        talk["institutions"] = institutions
    if abstract:
        talk["abstract"] = abstract
    if withdrawn:
        talk["withdrawn"] = True
    return talk


def main() -> None:
    data = build_conference_data()
    JSON_OUT.write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    n_sessions = len(data["sessions"])
    n_talks = len(data["talks"])
    n_abstracts = sum(1 for t in data["talks"] if t.get("abstract"))
    n_authors = sum(len(t.get("authors", [])) for t in data["talks"])
    n_presided = sum(1 for s in data["sessions"] if s.get("presider"))
    print(f"[process] wrote {JSON_OUT.name}: {n_sessions} sessions "
          f"({n_presided} with presiders), {n_talks} talks "
          f"({n_abstracts} with abstracts), {n_authors} author entries, "
          f"{len(data['affiliation_sources'])} affiliation strings.",
          flush=True)


if __name__ == "__main__":
    main()
