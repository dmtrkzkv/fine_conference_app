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

"""process_program_ecio2026.py — PROCESS ONLY.

Reads the JSON document the fetcher saved into data/ECIO2026_program.json
(Optica's presentations API) and emits a conference_data.json matching the
schema in docs/CONFERENCE_JSON.md.

Source-data shape (one record per session or talk):
    parentId        0 for a session; the parent session's id for a talk.
    id              numeric record id.
    code            "M1A", "M1A.1", … . Empty for some metadata entries.
    track           "SC1" .. "SC7" subcommittee tag (sessions only); cosmetic.
    title           Display title. Talks starting with "(Withdrawn)" are
                    flagged withdrawn here.
    startDate       ISO local timestamp ("2026-06-15T08:30:00").
    endDate         ISO local timestamp.
    location        "Room HG F1" / "Foyer …" / etc.
    presiderName    On a SESSION: the chair's name (sometimes "Name, Affil").
                    On a TALK: the presenting author's "Name, Affil" string.
    tags            Subset of {Invited, Plenary, Special Event}.
    description     Talk body. Format:
                        "<abstract HTML><br/><br/><strong>Authors</strong>:
                         Name1, Affil1 / Name2, Affil2 / ..."

Skipped: parent records with an obviously placeholder date (year 0001) —
Optica's CMS uses these for tour-style entries that have not been scheduled
yet. They carry no useful start time and would clutter the day filter.
"""

from __future__ import annotations

import html
import json
import re
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"
INPUT_JSON = DATA_DIR / "ECIO2026_program.json"
OUTPUT_JSON = SCRIPT_DIR / "conference_data.json"


def log(msg: str) -> None:
    print(msg, flush=True)


# =============================================================================
# Conference metadata
# =============================================================================
CONFERENCE_NAME = "ECIO 2026"

CURATOR = {
    "name": "Dmitry Kazakov",
    "affiliation": "Aylight",
    "link": "https://aylight.io/",
}

# Type/color registries. Each id is a color token referenced by sessions/talks
# and surfaced in the Types panel.
SESSION_TYPES = [
    {"id": "blue",    "label": "Technical Session"},
    {"id": "violet",  "label": "Plenary"},
    {"id": "emerald", "label": "Workshop / Panel"},
    {"id": "amber",   "label": "Industry / Poster"},
    {"id": "orange",  "label": "Other"},
]
TALK_TYPES = [
    {"id": "indigo", "label": "Invited"},
    {"id": "teal",   "label": "Plenary"},
    {"id": "rose",   "label": "Industry / Workshop"},
    {"id": "pink",   "label": "Contributed"},
]


# =============================================================================
# Helpers — html/text cleanup
# =============================================================================

# Tags the builder will render literally inside abstracts. Anything else gets
# stripped before the abstract reaches the JSON.
_KEEP_TAGS = {"sup", "sub", "i", "b", "em", "strong"}

_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)\b[^>]*>")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_WS_RE = re.compile(r"[ \t]+")
_NL_COLLAPSE_RE = re.compile(r"\n{3,}")


def _clean_html_to_text(s: str) -> str:
    """Reduce a fragment of Optica abstract HTML to text the builder can
    render. Keep the small set of inline tags the schema explicitly allows;
    drop everything else."""
    if not s:
        return ""
    s = _BR_RE.sub("\n", s)

    def _sub(m: re.Match[str]) -> str:
        tag = m.group(2).lower()
        return m.group(0) if tag in _KEEP_TAGS else ""

    s = _TAG_RE.sub(_sub, s)
    s = html.unescape(s)
    # collapse runs of spaces inside each line but preserve paragraph breaks
    out_lines = [_WS_RE.sub(" ", ln).strip() for ln in s.split("\n")]
    s = "\n".join(out_lines)
    s = _NL_COLLAPSE_RE.sub("\n\n", s).strip()
    return s


def _strip_inline_to_plain(s: str) -> str:
    """Strip ALL HTML for fields that don't allow inline markup (titles, names,
    affiliations)."""
    if not s:
        return ""
    s = _BR_RE.sub(" ", s)
    s = _TAG_RE.sub("", s)
    s = html.unescape(s)
    return _WS_RE.sub(" ", s).strip()


# =============================================================================
# Description parsing — split abstract from Authors list
# =============================================================================
_AUTHORS_MARKER_RE = re.compile(
    r"<\s*strong\s*>\s*Authors?\s*</\s*strong\s*>\s*:?\s*",
    re.IGNORECASE,
)


def split_description(desc: str) -> tuple[str, str]:
    """Return (abstract_html, authors_raw). authors_raw is the slash-separated
    "Name, Affil / Name, Affil" string after the <strong>Authors</strong>
    marker, or empty if there is no such marker."""
    if not desc:
        return ("", "")
    m = _AUTHORS_MARKER_RE.search(desc)
    if not m:
        return (desc, "")
    return (desc[:m.start()], desc[m.end():])


def parse_authors_block(raw: str) -> list[tuple[str, str]]:
    """Parse the slash-separated authors string into a list of
    (name, affiliation) tuples. Each entry is "Name, Affiliation"; we split on
    the FIRST comma so affiliations containing commas (rare here but possible)
    survive intact."""
    out: list[tuple[str, str]] = []
    if not raw:
        return out
    for ent in raw.split(" / "):
        ent = _strip_inline_to_plain(ent)
        if not ent:
            continue
        name, _, aff = ent.partition(", ")
        out.append((name.strip(), aff.strip()))
    return out


def split_name_affil(s: str) -> tuple[str, str]:
    """Split a "Name, Affiliation" string used in the presiderName field."""
    s = _strip_inline_to_plain(s)
    if not s:
        return ("", "")
    name, _, aff = s.partition(", ")
    return (name.strip(), aff.strip())


# =============================================================================
# Type classification — session / talk -> color token + human type label
# =============================================================================

def classify_session(rec: dict[str, Any]) -> tuple[str, str]:
    """Return (color_token, human_type_label) for a parent record."""
    tags = set(rec.get("tags") or [])
    title = (rec.get("title") or "").lower()
    code = (rec.get("code") or "").strip()

    if "Plenary" in tags:
        return ("violet", "Plenary Session")
    if "Special Event" in tags:
        return ("orange", "Special Event")
    if "workshop" in title:
        return ("emerald", "Workshop / Panel")
    if "industry talks" in title:
        return ("amber", "Industry Talks")
    if "poster" in title:
        return ("amber", "Poster Session")
    if code:
        if "Invited" in tags:
            return ("blue", "Invited Session")
        return ("blue", "Technical Session")
    return ("orange", "Event")


def classify_talk(talk_rec: dict[str, Any],
                  session_color: str) -> tuple[str, str]:
    """Return (color_token, human_type_label) for a talk record."""
    tags = set(talk_rec.get("tags") or [])
    if "Plenary" in tags or session_color == "violet":
        return ("teal", "Plenary Lecture")
    if "Invited" in tags:
        return ("indigo", "Invited Talk")
    if session_color == "emerald":
        return ("rose", "Workshop Talk")
    if session_color == "amber":
        return ("rose", "Industry Talk")
    return ("pink", "Contributed Talk")


# =============================================================================
# Date handling
# =============================================================================

# The CMS reports yet-unscheduled events with a year-0001 sentinel date. Filter
# those out so they don't poison the day filter.
_PLACEHOLDER_DATE_RE = re.compile(r"^0001-")


def has_valid_date(rec: dict[str, Any]) -> bool:
    s = rec.get("startDate") or ""
    return bool(s) and not _PLACEHOLDER_DATE_RE.match(s)


# =============================================================================
# Withdrawn handling
# =============================================================================
_WITHDRAWN_PREFIX_RE = re.compile(r"^\s*\(\s*withdrawn\s*\)\s*", re.IGNORECASE)


def strip_withdrawn_prefix(title: str) -> tuple[str, bool]:
    """If the title begins with a "(Withdrawn)" marker, peel it off and report
    the talk as withdrawn."""
    m = _WITHDRAWN_PREFIX_RE.match(title)
    if not m:
        return (title.strip(), False)
    return (title[m.end():].strip(), True)


# =============================================================================
# Institution dedup per talk
# =============================================================================

def build_institutions(
        authors: list[tuple[str, str]]) -> tuple[list[dict[str, Any]],
                                                  list[list[int]]]:
    """Given an ordered list of (name, affil) tuples, return:

      institutions: numbered list of unique affiliations as
                    [{"n": 1, "name": "ETH Zurich"}, …] in first-seen order.
      authors_insts: parallel to authors; each entry is the list of n-numbers
                     that author belongs to (one element here — Optica gives
                     us only a single affil per author).
    """
    institutions: list[dict[str, Any]] = []
    affil_to_n: dict[str, int] = {}
    authors_insts: list[list[int]] = []
    for _name, aff in authors:
        if not aff:
            authors_insts.append([])
            continue
        if aff not in affil_to_n:
            n = len(institutions) + 1
            affil_to_n[aff] = n
            institutions.append({"n": n, "name": aff})
        authors_insts.append([affil_to_n[aff]])
    return institutions, authors_insts


# =============================================================================
# Main build
# =============================================================================

def build_session(rec: dict[str, Any]) -> dict[str, Any]:
    color, type_label = classify_session(rec)
    sid = f"S-{rec['id']}"
    title = _strip_inline_to_plain(rec.get("title") or "") or "(Untitled)"
    code = (rec.get("code") or "").strip()
    track = (rec.get("track") or "").strip()
    location = _strip_inline_to_plain(rec.get("location") or "")
    presider_name, presider_aff = split_name_affil(rec.get("presiderName") or "")

    sess: dict[str, Any] = {
        "id": sid,
        "title": title,
        "color": color,
        "type": type_label,
        "start_ts": rec["startDate"],
        "end_ts": rec["endDate"],
        "talk_ids": [],
    }

    # Surface the session code + track as the topic line.
    topic_bits = [b for b in (code, track) if b]
    if topic_bits:
        sess["topic"] = " · ".join(topic_bits)

    if location:
        sess["location"] = location
    if presider_name:
        sess["presider"] = presider_name
        if presider_aff:
            sess["presider_aff"] = presider_aff

    abstract = _clean_html_to_text(rec.get("description") or "")
    if abstract:
        sess["details"] = abstract

    return sess


def build_talk(rec: dict[str, Any],
               parent_session: dict[str, Any]) -> dict[str, Any]:
    color, _type_label = classify_talk(rec, parent_session["color"])
    tid = f"T-{rec['id']}"
    raw_title = _strip_inline_to_plain(rec.get("title") or "")
    title, withdrawn = strip_withdrawn_prefix(raw_title)
    if not title:
        title = "(Untitled)"
    code = (rec.get("code") or "").strip()
    location = _strip_inline_to_plain(rec.get("location") or "")

    abstract_html, authors_raw = split_description(rec.get("description") or "")
    abstract_text = _clean_html_to_text(abstract_html)
    authors = parse_authors_block(authors_raw)

    institutions, authors_insts = build_institutions(authors)
    authors_field = [
        {"name": name, "insts": insts}
        for (name, _aff), insts in zip(authors, authors_insts)
    ]

    # Speaker = presiderName on the record (the first author, with affil
    # baked in). Fall back to the first author when presiderName is empty.
    speaker_name, _speaker_aff = split_name_affil(rec.get("presiderName") or "")
    if not speaker_name and authors:
        speaker_name = authors[0][0]

    speaker_pos = None
    if speaker_name:
        for i, (n, _a) in enumerate(authors):
            if n.lower() == speaker_name.lower():
                speaker_pos = i
                break

    talk: dict[str, Any] = {
        "id": tid,
        "session_id": parent_session["id"],
        "title": title,
        "color": color,
        "start_ts": rec["startDate"],
        "end_ts": rec["endDate"],
    }
    if code:
        talk["number"] = code
    if location:
        talk["location"] = location
    if speaker_name:
        talk["speaker"] = speaker_name
        if speaker_pos is not None:
            talk["speaker_pos"] = speaker_pos
    if authors:
        talk["first_author"] = authors[0][0]
        talk["last_author"] = authors[-1][0]
        talk["authors"] = authors_field
    if institutions:
        talk["institutions"] = institutions
    if abstract_text:
        talk["abstract"] = abstract_text
    if withdrawn:
        talk["withdrawn"] = True
        talk["status"] = "Withdrawn"

    return talk


def main() -> None:
    log("=" * 72)
    log("[config] ECIO 2026 PROCESSOR")
    log(f"[config]   input JSON : {INPUT_JSON}")
    log(f"[config]   output     : {OUTPUT_JSON}")
    log("=" * 72)

    if not INPUT_JSON.exists():
        log(f"[fatal] missing input JSON: {INPUT_JSON}")
        log("[fatal] run fetch_program_ecio2026.py first.")
        sys.exit(1)

    doc = json.loads(INPUT_JSON.read_text(encoding="utf-8"))
    presentations: list[dict[str, Any]] = doc.get("presentations", [])
    if not presentations:
        log("[fatal] presentations array is empty.")
        sys.exit(1)

    log(f"[info] loaded {len(presentations)} presentation records.")

    # Split into parents + children, filtering placeholder-date parents.
    parents_by_id: dict[int, dict[str, Any]] = OrderedDict()
    children_by_parent: dict[int, list[dict[str, Any]]] = {}
    skipped_parents = 0
    for rec in presentations:
        if rec.get("parentId") == 0:
            if not has_valid_date(rec):
                skipped_parents += 1
                continue
            parents_by_id[rec["id"]] = rec
        else:
            pid = rec["parentId"]
            children_by_parent.setdefault(pid, []).append(rec)
    if skipped_parents:
        log(f"[info] skipped {skipped_parents} parent record(s) with "
            "placeholder dates (no real start time).")

    # Sort parents by start time, then by code for a stable ordering.
    parents_sorted = sorted(
        parents_by_id.values(),
        key=lambda r: (r["startDate"], r.get("code") or ""),
    )

    sessions: list[dict[str, Any]] = []
    talks: list[dict[str, Any]] = []

    # Build sessions, then their child talks in (startDate, code) order.
    for prec in parents_sorted:
        sess = build_session(prec)
        # Children — sort by startDate, then by the trailing number in code.
        kids = children_by_parent.get(prec["id"], [])

        def _kid_key(c: dict[str, Any]) -> tuple[str, int, str]:
            code = c.get("code") or ""
            tail = code.rsplit(".", 1)[-1]
            try:
                idx = int(tail)
            except ValueError:
                idx = 0
            return (c.get("startDate") or "", idx, code)

        kids = sorted(kids, key=_kid_key)
        for krec in kids:
            if not has_valid_date(krec):
                continue
            t = build_talk(krec, sess)
            talks.append(t)
            sess["talk_ids"].append(t["id"])

        sessions.append(sess)

    # Affiliation raw-string pools the shortener learns from.
    aff_lines: list[str] = []
    for t in talks:
        for inst in t.get("institutions") or []:
            n = inst.get("name", "")
            if n:
                aff_lines.append(n)
    presider_affs = sorted({s.get("presider_aff", "")
                            for s in sessions if s.get("presider_aff")})

    out: dict[str, Any] = {
        "conference_name": CONFERENCE_NAME,
        "sessions": sessions,
        "talks": talks,
        "session_types": SESSION_TYPES,
        "talk_types": TALK_TYPES,
        "affiliation_sources": {
            "affiliation_full_lines": sorted(set(aff_lines)),
            "presider_affiliation_strings": presider_affs,
            "institution_strings": [],
        },
    }

    if CURATOR and CURATOR.get("name"):
        cur = {"name": CURATOR["name"]}
        if CURATOR.get("affiliation"):
            cur["affiliation"] = CURATOR["affiliation"]
        if CURATOR.get("link"):
            cur["link"] = CURATOR["link"]
        out["curator"] = cur

    OUTPUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False),
                           encoding="utf-8")
    log(f"[ok] wrote {OUTPUT_JSON.name}: "
        f"{len(sessions)} sessions, {len(talks)} talks.")
    log("=" * 72)
    log("DONE.")
    log("=" * 72)


if __name__ == "__main__":
    main()
