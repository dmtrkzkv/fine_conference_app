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

The "processor" half of the ECIO 2026 pipeline. Reads ONLY what fetch put into
data/ (no network), and emits a clean conference_data.json next to itself.

Inputs (under data/):
    ECIO26_DetailedSchedule.pdf   the wide A3 grid of every session/talk
    ECIO26_Concise.pdf            one-page program-overview (currently used only
                                  as a cross-check; the skeleton below is the
                                  authoritative session list)

ECIO publishes no abstract book and no per-talk page, so this processor cannot
recover full author lists, affiliations, or abstracts. Each talk carries only
its title and a single presenting-author name (what the schedule grid prints).

Strategy
--------
The schedule PDF is one wide page laid out as a vertical sequence of day blocks.
Each day block is a TIME x ROOM grid: the leftmost column holds the time-slot
labels (e.g. "0830-0845") and the next three columns hold the parallel-room
cells, one per session-track (HG F1 / HG E1.1 / HG E1.2). A cell is one talk:
title text on the left, speaker name right-aligned at the cell's right edge,
separated by a visible gap. We parse this geometry directly.

The day-level structure (sessions, time blocks, rooms, types) is small and
stable across re-issues of the PDF, so it lives below as a hand-curated
SKELETON. The processor's job is to populate each track session in that
skeleton with the talks the PDF actually prints under it.

For non-track items (Plenary, Workshop panels, Industry Talks, Poster sessions,
ceremonies, social events) we emit them as sessions in their own right. The
talks under Workshops and Industry Talk sessions also come straight from the
PDF — those cells don't use the wide title-vs-speaker x-gap of the tech grid;
they pack "Title. Speaker, Affiliation" into a single run of words. We parse
that run with _harvest_block_cells (see below). Plenary lectures are the one
exception: the PDF only prints the plenary speaker's name on a meta-row, with
no talk title we could extract, so those entries are still hand-listed below
(speaker name + invented "Plenary Lecture" placeholder).

Session titles also come from the PDF wherever the PDF renders one: the topic
words above each tech-track column (e.g. "Electro-Optic Modulators"), the long
"WORKSHOP 1: …" / "WORKSHOP 2: …" headers, and the "Industry Talk Session N: …"
headers all sit at a known Y in a known column and we read them off. For
sessions the PDF has no explicit header for (ceremonies, lunches, social
events) the SKELETON carries an explicit `title`.

Output:
    conference_data.json   schema documented in docs/CONFERENCE_JSON.md
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path


def log(msg: str) -> None:
    print(msg, flush=True)


SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"
INPUT_PDF = DATA_DIR / "ECIO26_DetailedSchedule.pdf"
OUTPUT_JSON = SCRIPT_DIR / "conference_data.json"


def _bootstrap_pdfplumber() -> None:
    try:
        import pdfplumber  # noqa: F401
    except ImportError:
        log("[setup] Installing pdfplumber…")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install",
             "--quiet", "pdfplumber>=0.10"])


# =============================================================================
# Conference name + day registry
# =============================================================================
CONFERENCE_NAME = "ECIO 2026"

# Curator credit shown at the bottom of the About section in the built app.
# Schema (per CONFERENCE_JSON.md): {name, affiliation?, link?}. Leave `name`
# empty (or set CURATOR = None) to omit the curator line entirely.
CURATOR = {
    "name": "Dmitry Kazakov",
    "affiliation": "AyLight AG",
    "link": "https://aylight.io/",
}

# day key -> ISO date. The key is what the SKELETON entries reference.
DAYS = {
    "sun": "2026-06-14",
    "mon": "2026-06-15",
    "tue": "2026-06-16",
    "wed": "2026-06-17",
}

# Three parallel-session rooms in the technical-grid blocks.
ROOM_COL1 = "HG F1"
ROOM_COL2 = "HG E1.1"
ROOM_COL3 = "HG E1.2"
PLENARY_ROOM = "HG F30 (Plenary Auditorium)"

# Column index (1..3) -> room. Sessions in the SKELETON refer to columns by
# integer; this is the canonical mapping the parser uses to assign x-ranges.
ROOM_BY_COL = {1: ROOM_COL1, 2: ROOM_COL2, 3: ROOM_COL3}

# Type/color tokens we emit. Kept small on purpose so the Types panel in the
# built app stays uncluttered.
SESSION_TYPES = [
    {"id": "blue",   "label": "Technical Session"},
    {"id": "violet", "label": "Plenary"},
    {"id": "emerald","label": "Workshop / Panel"},
    {"id": "amber",  "label": "Industry / Poster"},
    {"id": "orange", "label": "Other"},
]
TALK_TYPES = [
    {"id": "indigo", "label": "Invited"},
    {"id": "pink",   "label": "Contributed"},
    {"id": "rose",   "label": "Industry / Panel"},
    {"id": "teal",   "label": "Plenary"},
]


# =============================================================================
# SKELETON: hand-curated session list
#
# Each entry is one session. Fields:
#   id        : stable string id (used in talk session_id)
#   day       : key into DAYS (-> ISO date)
#   start/end : "HH:MM" local time
#   title     : OPTIONAL display title. When omitted we read it from the PDF
#               (topic header for track sessions, header row for workshops /
#               industry sessions); supplied for ceremonies, lunches, and
#               social events where the PDF has no header to scrape.
#   type      : human label shown in the session detail header
#   color     : token referenced by SESSION_TYPES above
#   room      : optional override (else ROOM_BY_COL[column])
#   track     : optional 3-char track code printed on the PDF (e.g. "M1A");
#               purely metadata, surfaces in the topic line
#   column    : 1/2/3 -> the PDF column to harvest talks from; OMIT for sessions
#               with no PDF-parsed talks (ceremonies, lunches, social events,
#               and hand-listed entries like the plenary lectures)
#   pdf_title : optional PDF-title hints. Maps to the topic-header / row-text
#               location the title text lives at. Two shapes:
#                 {"source": "topic_header", "column": 1|2|3}
#                     The size-4.56 topic words sitting just above this
#                     session's Y band in the given column. Default for any
#                     tech-track session (one with "column" + "track") if
#                     pdf_title is omitted.
#                 {"source": "row_text", "column": 1|2|3, "y": <float>}
#                     A single PDF row at the given Y (size-4.08 text) in the
#                     given column — used for workshop and industry headers
#                     which sit on a dedicated row, not above the column.
#   harvest   : optional directive to harvest non-grid talk cells from the PDF
#               (used for industry sessions + workshops, whose cells pack
#               "Title. Speaker, Affiliation" into a single run rather than
#               using the wide title-vs-speaker x-gap of the tech grid).
#                 {"column": 1|2|3, "talk_color": "rose",
#                  "slot_mode": "per_slot" | "session",
#                  "slot_minutes": <int>}      # only for slot_mode "per_slot"
#               "per_slot" walks the time-slot rows inside the band in order
#               and emits one talk per slot from the cell at that Y; "session"
#               emits one talk per non-empty cell row and inherits the
#               session's start/end for all of them.
#   talks     : optional explicit talk list (kept for the few plenary-lecture
#               entries whose talk title is invented, not from the PDF).
#               Each entry: {"title", "speaker", "speaker_aff", "color"}. The
#               processor turns these into talk objects directly.
# =============================================================================
SKELETON: list[dict] = [
    # ---- Sunday June 14 — student day ---------------------------------------
    # These three have no PDF row of their own (they're listed in the Sunday
    # banner only), so keep titles hardcoded.
    {"id": "S-sun-student-workshop", "day": "sun",
     "start": "13:00", "end": "16:30",
     "title": "Student Workshop", "type": "Student Event",
     "color": "orange", "room": PLENARY_ROOM},
    {"id": "S-sun-bench-to-business", "day": "sun",
     "start": "15:30", "end": "16:30",
     "title": "Bench to Business Symposium", "type": "Symposium",
     "color": "orange", "room": PLENARY_ROOM},
    {"id": "S-sun-pizza", "day": "sun",
     "start": "17:00", "end": "19:30",
     "title": "Networking Pizza Dinner", "type": "Social Event",
     "color": "orange", "room": "Venue to be announced"},

    # ---- Monday June 15 -----------------------------------------------------
    {"id": "S-mon-opening", "day": "mon",
     "start": "08:00", "end": "08:15",
     "title": "Opening Ceremony", "type": "Ceremony",
     "color": "orange", "room": PLENARY_ROOM},

    # Tech-track sessions. Title omitted -> read from the PDF topic-header row
    # above the column. talks parsed from the wide title-vs-speaker grid as
    # before.
    {"id": "S-mon-M1A", "day": "mon", "start": "08:30", "end": "10:15",
     "type": "Technical Session",
     "color": "blue", "track": "M1A", "column": 1},
    {"id": "S-mon-M1B", "day": "mon", "start": "08:30", "end": "10:15",
     "type": "Technical Session",
     "color": "blue", "track": "M1B", "column": 2},
    {"id": "S-mon-M1C", "day": "mon", "start": "08:30", "end": "10:15",
     "type": "Technical Session",
     "color": "blue", "track": "M1C", "column": 3},

    {"id": "S-mon-M2A", "day": "mon", "start": "10:45", "end": "12:30",
     "type": "Technical Session",
     "color": "blue", "track": "M2A", "column": 1},
    {"id": "S-mon-M2B", "day": "mon", "start": "10:45", "end": "12:30",
     "type": "Technical Session",
     "color": "blue", "track": "M2B", "column": 2},
    {"id": "S-mon-M2C", "day": "mon", "start": "10:45", "end": "12:30",
     "type": "Technical Session",
     "color": "blue", "track": "M2C", "column": 3},

    {"id": "S-mon-M3A", "day": "mon", "start": "13:30", "end": "15:15",
     "type": "Technical Session",
     "color": "blue", "track": "M3A", "column": 1},
    {"id": "S-mon-M3B", "day": "mon", "start": "13:30", "end": "15:15",
     "type": "Technical Session",
     "color": "blue", "track": "M3B", "column": 2},
    {"id": "S-mon-M3C", "day": "mon", "start": "13:30", "end": "15:15",
     "type": "Technical Session",
     "color": "blue", "track": "M3C", "column": 3},

    {"id": "S-mon-poster-blitz-1-1", "day": "mon",
     "start": "15:25", "end": "15:40",
     "title": "Poster Blitz 1.1", "type": "Poster Blitz",
     "color": "amber", "room": ROOM_COL1},
    {"id": "S-mon-poster-blitz-1-2", "day": "mon",
     "start": "15:40", "end": "15:55",
     "title": "Poster Blitz 1.2", "type": "Poster Blitz",
     "color": "amber", "room": ROOM_COL2},
    {"id": "S-mon-poster-1", "day": "mon",
     "start": "15:55", "end": "16:55",
     "title": "Coffee + Poster Session 1", "type": "Poster Session",
     "color": "amber", "room": "Foyers in front of Plenary Auditorium"},

    # All three Monday industry sessions run in parallel 16:55–17:55 with six
    # 10-min slots per column. Title text and per-slot talk cells both come
    # straight from the PDF (cells x ≈ 55-415 / 415-770 / 770-1100, header
    # row y ≈ 319.1). _harvest_block_cells parses the "Title. Speaker,
    # Affiliation" run packed inside each cell.
    {"id": "S-mon-industry-1", "day": "mon",
     "start": "16:55", "end": "17:55",
     "type": "Industry Talks", "color": "amber", "room": ROOM_COL1,
     "pdf_title": {"source": "row_text", "column": 1, "y": 319.1},
     "harvest": {"column": 1, "talk_color": "rose",
                 "slot_mode": "per_slot", "slot_minutes": 10}},
    {"id": "S-mon-industry-2", "day": "mon",
     "start": "16:55", "end": "17:55",
     "type": "Industry Talks", "color": "amber", "room": ROOM_COL2,
     "pdf_title": {"source": "row_text", "column": 2, "y": 319.1},
     "harvest": {"column": 2, "talk_color": "rose",
                 "slot_mode": "per_slot", "slot_minutes": 10}},
    {"id": "S-mon-industry-3", "day": "mon",
     "start": "16:55", "end": "17:55",
     "type": "Industry Talks", "color": "amber", "room": ROOM_COL3,
     "pdf_title": {"source": "row_text", "column": 3, "y": 319.1},
     "harvest": {"column": 3, "talk_color": "rose",
                 "slot_mode": "per_slot", "slot_minutes": 10}},

    {"id": "S-mon-plenary-1", "day": "mon",
     "start": "18:05", "end": "18:50",
     "title": "Plenary Session 1", "type": "Plenary",
     "color": "violet", "room": PLENARY_ROOM,
     "talks": [
        # Plenary "talks" are speaker-only — the PDF prints only the lecturer
        # name on a meta-row, no extractable talk title.
        {"title": "Plenary Lecture", "speaker": "Peter Seitz",
         "speaker_aff": "", "color": "teal"},
     ]},
    {"id": "S-mon-welcome", "day": "mon",
     "start": "18:50", "end": "20:30",
     "title": "Welcome Reception", "type": "Social Event",
     "color": "orange",
     "room": "Foyers in front of Session Rooms"},

    # ---- Tuesday June 16 ----------------------------------------------------
    {"id": "S-tue-T1A", "day": "tue", "start": "08:30", "end": "10:15",
     "type": "Technical Session",
     "color": "blue", "track": "T1A", "column": 1},
    {"id": "S-tue-T1B", "day": "tue", "start": "08:30", "end": "10:15",
     "type": "Technical Session",
     "color": "blue", "track": "T1B", "column": 2},
    {"id": "S-tue-T1C", "day": "tue", "start": "08:30", "end": "10:15",
     "type": "Technical Session",
     "color": "blue", "track": "T1C", "column": 3},

    {"id": "S-tue-T2A", "day": "tue", "start": "10:45", "end": "12:30",
     "type": "Technical Session",
     "color": "blue", "track": "T2A", "column": 1},
    {"id": "S-tue-T2B", "day": "tue", "start": "10:45", "end": "12:30",
     "type": "Technical Session",
     "color": "blue", "track": "T2B", "column": 2},
    {"id": "S-tue-T2C", "day": "tue", "start": "10:45", "end": "12:30",
     "type": "Technical Session",
     "color": "blue", "track": "T2C", "column": 3},

    # Workshops: title text + panellist cells both from the PDF (header row
    # y ≈ 508.6, talk cells scattered through y 508..555 inside the column).
    # slot_mode "session" because workshop cells don't line up with the
    # time-slot rows — they're free-form panellist entries.
    {"id": "S-tue-W1", "day": "tue", "start": "13:30", "end": "15:20",
     "type": "Workshop", "color": "emerald", "room": ROOM_COL1,
     "pdf_title": {"source": "row_text", "column": 1, "y": 508.6},
     "harvest": {"column": 1, "talk_color": "rose",
                 "slot_mode": "session"}},
    {"id": "S-tue-W2", "day": "tue", "start": "13:30", "end": "15:20",
     "type": "Workshop", "color": "emerald", "room": ROOM_COL2,
     "pdf_title": {"source": "row_text", "column": 2, "y": 508.6},
     "harvest": {"column": 2, "talk_color": "rose",
                 "slot_mode": "session"}},

    {"id": "S-tue-poster-blitz-2-1", "day": "tue",
     "start": "15:30", "end": "16:00",
     "title": "Poster Blitz 2.1", "type": "Poster Blitz",
     "color": "amber", "room": ROOM_COL1},
    {"id": "S-tue-poster-blitz-2-2", "day": "tue",
     "start": "15:30", "end": "16:00",
     "title": "Poster Blitz 2.2", "type": "Poster Blitz",
     "color": "amber", "room": ROOM_COL2},
    {"id": "S-tue-poster-2", "day": "tue",
     "start": "16:00", "end": "17:00",
     "title": "Coffee + Poster Session 2", "type": "Poster Session",
     "color": "amber", "room": "Foyers in front of Plenary Auditorium"},

    {"id": "S-tue-plenary-2", "day": "tue",
     "start": "17:00", "end": "17:45",
     "title": "Plenary Session 2", "type": "Plenary",
     "color": "violet", "room": PLENARY_ROOM,
     "talks": [
        {"title": "Plenary Lecture", "speaker": "Mona Jarrahi",
         "speaker_aff": "UCLA", "color": "teal"},
     ]},

    {"id": "S-tue-city-tour", "day": "tue",
     "start": "18:00", "end": "19:00",
     "title": "Zurich City Tour", "type": "Social Event",
     "color": "orange", "room": "Meet at venue"},
    {"id": "S-tue-gala", "day": "tue",
     "start": "19:00", "end": "23:00",
     "title": "Gala Dinner", "type": "Social Event",
     "color": "orange", "room": "MS Panta Rhei"},

    # ---- Wednesday June 17 --------------------------------------------------
    {"id": "S-wed-W1A", "day": "wed", "start": "08:30", "end": "10:15",
     "type": "Technical Session",
     "color": "blue", "track": "W1A", "column": 1},
    {"id": "S-wed-W1B", "day": "wed", "start": "08:30", "end": "10:15",
     "type": "Technical Session",
     "color": "blue", "track": "W1B", "column": 2},

    {"id": "S-wed-W2A", "day": "wed", "start": "10:45", "end": "12:30",
     "type": "Technical Session",
     "color": "blue", "track": "W2A", "column": 1},
    {"id": "S-wed-W2B", "day": "wed", "start": "10:45", "end": "12:30",
     "type": "Technical Session",
     "color": "blue", "track": "W2B", "column": 2},
    {"id": "S-wed-W2C", "day": "wed", "start": "10:45", "end": "12:30",
     "type": "Technical Session",
     "color": "blue", "track": "W2C", "column": 3},

    {"id": "S-wed-W3A", "day": "wed", "start": "13:30", "end": "15:15",
     "type": "Technical Session",
     "color": "blue", "track": "W3A", "column": 1},
    {"id": "S-wed-W3B", "day": "wed", "start": "13:30", "end": "15:15",
     "type": "Technical Session",
     "color": "blue", "track": "W3B", "column": 2},

    {"id": "S-wed-closing", "day": "wed",
     "start": "15:25", "end": "15:40",
     "title": "Closing Ceremony", "type": "Ceremony",
     "color": "orange", "room": ROOM_COL1},
    {"id": "S-wed-labs", "day": "wed",
     "start": "16:45", "end": "18:00",
     "title": "Lab Tours and Company Visits", "type": "Other",
     "color": "orange", "room": "ETH Zurich"},
]


# =============================================================================
# PDF parsing: row-bucket the words, locate day Y bands, harvest column cells.
# =============================================================================

# Column X-ranges in the detailed PDF. The grid has session-room cells centred
# at x≈216 / 565 / 911 (from the "Session Rooms ->" header). Speakers are
# right-aligned to ~378 / 742 / 1065. The boundaries below sit comfortably in
# the inter-column gaps so a word's midpoint deterministically picks one column,
# including the long invited speakers (e.g. "Camille Sophie Brès") whose last
# token straddles the visual seam.
COL_X_RANGES = {
    1: (55.0, 415.0),
    2: (415.0, 770.0),
    3: (770.0, 1100.0),
}
TIME_X_RANGE = (15.0, 55.0)  # left-edge time-slot column

# A row is "the same line" if its top differs by at most this. The schedule
# sometimes baselines a speaker chip 2-3pt below its title (especially for
# italic names rendered in a tighter font), so the tolerance has to clear that
# small offset without merging adjacent time-slot rows (gap >= 5pt).
ROW_TOL = 3.5
# A speaker is split from a title when the words inside a row have an x-gap
# of at least this many points between them. Cells are narrow enough that
# 13pt is a clean separator — normal word-to-word gaps inside titles are
# 2-6pt, and hyphenated compounds carry NO internal space (pdfplumber emits
# "Single-Photon" as one word). The smallest title→speaker gap we measured
# in the ECIO 2026 PDF was ~13.9pt (the "(UTC-PDs)" UTC-photodiodes row),
# so the threshold sits just under that.
SPEAKER_GAP_PT = 13.0
# The session-track topic header above each block is rendered in a slightly
# larger font (4.56pt) than talk text (4.08pt). Used to filter topic words out
# when harvesting talk content.
TOPIC_FONT_MIN = 4.4

# Patterns that mark a "row" as a non-talk break (coffee, lunch, plenary
# announcements, etc.) when they appear inside what would otherwise be a track
# session's Y band. The schedule PDF lays these out as full-width rows that
# bleed slightly into the column we're harvesting — drop them outright.
NON_TALK_PREFIXES = (
    "Coffee", "Lunch", "Welcome", "Closing", "Opening", "Plenary",
    "Industry Talks", "Industry Talk", "Workshop", "Poster Blitz",
    "Panel Discussion", "Gala", "Networking", "Bench to Business",
    "Student Workshop", "Zurich City", "Lab Tours", "Registration",
    "Exhibition", "Session Rooms",
)

TIME_RE = re.compile(r"^\d{4}-\d{4}$")
DAY_RE = re.compile(
    r"^(SUNDAY|MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY)$"
)
TRACK_LABEL_RE = re.compile(r"^[MTW][1-3][A-C]$")


def _hhmm_to_minutes(hhmm: str) -> int:
    """Convert 'HH:MM' or 'HHMM' to minutes-since-midnight."""
    s = hhmm.replace(":", "")
    return int(s[:2]) * 60 + int(s[2:])


def _extract_words(pdf_path: Path) -> list[dict]:
    import pdfplumber
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[0]
        words = page.extract_words(
            keep_blank_chars=False,
            use_text_flow=False,
            extra_attrs=["size", "fontname"],
        )
    # pdfplumber returns floats as strings sometimes; normalise.
    out: list[dict] = []
    for w in words:
        out.append({
            "text": w["text"],
            "x0": float(w["x0"]),
            "x1": float(w["x1"]),
            "top": float(w["top"]),
            "size": float(w.get("size", 0.0) or 0.0),
        })
    return out


def _cluster_rows(words: list[dict], tol: float = ROW_TOL) -> list[dict]:
    """Cluster words by `top` into baseline rows. Chaining is transitive on the
    sorted stream: each new word merges into the current row when its top is
    within `tol` of the *most recently added* word's top. This lets a title at
    y=268.7 chain together with a tracked italic name whose letters sit on
    y=266.3 (above) and y=271.3 (below) — the kind of split baseline a few of
    the longer invited-speaker chips use in the schedule grid."""
    if not words:
        return []
    sw = sorted(words, key=lambda w: (w["top"], w["x0"]))
    rows: list[dict] = []
    for w in sw:
        if rows and (w["top"] - rows[-1]["last_top"]) <= tol:
            rows[-1]["words"].append(w)
            rows[-1]["last_top"] = w["top"]
        else:
            rows.append({"last_top": w["top"], "words": [w]})
    for r in rows:
        r["words"].sort(key=lambda w: w["x0"])
        tops = sorted(w["top"] for w in r["words"])
        # `top` = the median word baseline. Using the median (not the min)
        # keeps a long row anchored to its bulk text even when a handful of
        # words sit on a slightly different baseline (e.g. a tracked italic
        # name whose letters render 5pt below the title's baseline). That bulk
        # baseline is what _talk_time_window matches against slot anchors.
        r["top"] = tops[len(tops) // 2]
    return rows


def _day_y_bands(rows: list[dict], page_h: float) -> dict[str, tuple[float, float]]:
    """Return {day_key: (y_top, y_bottom)} for each weekday header found.

    Day headers in the detailed PDF appear as a two-word run "<WEEKDAY>, JUNE",
    rendered in a noticeably larger font (~4.92pt) than talk text. We locate
    each such header's Y and treat the day's vertical band as everything from
    that Y down to the next day's Y (or the page bottom for the last day).
    """
    found: list[tuple[float, str]] = []
    for r in rows:
        # A row is a day header if it contains one of the WEEKDAY tokens at
        # the larger font size (4.6+).
        for w in r["words"]:
            t = w["text"].rstrip(",").upper()
            if DAY_RE.match(t) and w["size"] >= 4.4:
                key = {
                    "SUNDAY": "sun",
                    "MONDAY": "mon",
                    "TUESDAY": "tue",
                    "WEDNESDAY": "wed",
                }.get(t)
                if key:
                    found.append((r["top"], key))
                    break
    found.sort()
    bands: dict[str, tuple[float, float]] = {}
    for i, (y, key) in enumerate(found):
        y_end = found[i + 1][0] if i + 1 < len(found) else page_h
        bands[key] = (y, y_end)
    return bands


def _row_in_band(row: dict, band: tuple[float, float]) -> bool:
    return band[0] <= row["top"] <= band[1]


def _slot_minutes(slot: str) -> tuple[int, int]:
    """Convert 'HHMM-HHMM' to (start_min, end_min)."""
    a, b = slot.split("-")
    return _hhmm_to_minutes(a), _hhmm_to_minutes(b)


def _session_time_slots(
    words: list[dict],
    band: tuple[float, float],
    start_min: int,
    end_min: int,
) -> list[tuple[float, int, int]]:
    """Return [(top_y, start_min, end_min), …] for every "HHMM-HHMM" time-slot
    label in the left-edge column whose start falls inside [start_min, end_min).
    Sorted by Y ascending (top-of-page first).

    Scans the raw word stream rather than pre-clustered rows on purpose: row
    clustering chains transitively across columns at this density (talk lines
    in different columns sit at very similar Y), which would smear the time
    label onto neighbouring rows and mis-place the slot anchor."""
    out: list[tuple[float, int, int]] = []
    for w in words:
        if not (band[0] <= w["top"] <= band[1]):
            continue
        if w["x0"] >= TIME_X_RANGE[1]:
            continue
        if not TIME_RE.match(w["text"]):
            continue
        s, e = _slot_minutes(w["text"])
        if start_min <= s < end_min:
            out.append((w["top"], s, e))
    out.sort(key=lambda t: t[0])
    return out


def _harvest_session_y_range(
    slots: list[tuple[float, int, int]],
    band: tuple[float, float],
) -> tuple[float, float]:
    """Tight Y range for a session given its time-slot rows. A modest tail-pad
    below the last time-slot row catches invited talks that span two slots and
    sit just under the last labelled slot. Too generous and we'd absorb the
    next block's session header."""
    if not slots:
        return (band[0], band[0])
    tops = [s[0] for s in slots]
    return (min(tops) - 1.0, max(tops) + 5.0)


def _talk_time_window(
    y: float,
    slots: list[tuple[float, int, int]],
    sess_start_min: int,
    sess_end_min: int,
    is_invited: bool = False,
) -> tuple[int, int]:
    """Map a talk's row-Y to the time window it occupies.

    Strategy: a 15-minute talk's text row sits within ~2pt of one time-slot
    label's Y; a 30-minute invited talk's row sits roughly midway between two
    consecutive labels (each ~5-7pt away). So we pick the NEAREST slot by
    absolute Y distance, and extend to span the neighbouring slot only when
    the two are about equally far from the talk (i.e. it's genuinely between
    them, not just close to one).
    """
    if not slots:
        return sess_start_min, sess_end_min

    closest_idx = min(range(len(slots)),
                      key=lambda i: abs(y - slots[i][0]))
    a_top, a_start, a_end = slots[closest_idx]
    dist_a = abs(y - a_top)

    # Invited talks are 30-minute slots on the ECIO grid: extend the anchor to
    # the next slot's end (or pull in the previous slot's start, if the row is
    # actually above the closest slot). The "Invited:" tag in the title is the
    # authoritative signal — geometry alone can't tell a 15- from a 30-minute
    # row when an invited row sits flush with one of the two slots it covers.
    if is_invited:
        if closest_idx + 1 < len(slots) and y >= a_top - 1.0:
            return a_start, slots[closest_idx + 1][2]
        if closest_idx - 1 >= 0:
            return slots[closest_idx - 1][1], a_end
        return a_start, a_end

    # Non-invited (15-min): "equidistant neighbour" check catches the rare row
    # that lands midway between two slot labels.
    for nb_idx in (closest_idx - 1, closest_idx + 1):
        if not (0 <= nb_idx < len(slots)):
            continue
        nb_top, nb_start, nb_end = slots[nb_idx]
        dist_b = abs(y - nb_top)
        if abs(dist_a - dist_b) < 2.0 and dist_a > 3.0:
            lo = min(closest_idx, nb_idx)
            hi = max(closest_idx, nb_idx)
            return slots[lo][1], slots[hi][2]

    return a_start, a_end


def _split_title_speaker(
    line_words: list[dict],
    col_x: tuple[float, float],
) -> tuple[str, str]:
    """For one line of words inside a cell, split into (title, speaker) at the
    largest x-gap of at least SPEAKER_GAP_PT. The split is accepted only when
    the right-hand chunk starts in the last 40% of the column width — that's
    the right-aligned speaker region in the schedule grid. Otherwise the gap
    is between two title chunks and we keep the whole line as title."""
    if not line_words:
        return "", ""
    ws = sorted(line_words, key=lambda w: w["x0"])
    # Largest gap in the row.
    best_split: int | None = None
    best_gap = SPEAKER_GAP_PT
    for i in range(1, len(ws)):
        gap = ws[i]["x0"] - ws[i - 1]["x1"]
        if gap >= best_gap:
            best_gap = gap
            best_split = i
    if best_split is None:
        return _join_words_baseline_aware(ws), ""
    right = ws[best_split:]
    col_lo, col_hi = col_x
    right_zone_start = col_lo + 0.6 * (col_hi - col_lo)
    if right[0]["x0"] < right_zone_start:
        return _join_words_baseline_aware(ws), ""
    return (_join_words_baseline_aware(ws[:best_split]),
            _join_words_baseline_aware(right))


def _join_words(ws: list[dict]) -> str:
    """Reassemble a list of (sorted-by-x) word dicts into a string with single
    spaces. Letters that pdfplumber split into 1-2 character fragments (it does
    this for some condensed font runs) get glued back when their boxes touch."""
    if not ws:
        return ""
    parts: list[str] = []
    prev = None
    for w in ws:
        if prev is not None and (w["x0"] - prev["x1"]) <= 0.5:
            parts.append(w["text"])
        else:
            parts.append((" " if parts else "") + w["text"])
        prev = w
    return "".join(parts).strip()


def _join_words_baseline_aware(ws: list[dict]) -> str:
    """Like _join_words, but when the words occupy more than one distinct
    baseline (some italic speaker chips render across two y values per glyph),
    group by baseline first, sort each group by x, and concatenate groups in
    top-to-bottom order. This prevents interleaved characters like
    "S-e-y-e-d-m-o-h-…" on one baseline crossing with "S-e-y-e-d-i-n-n-…" on
    the next from being woven together by a flat x-sort."""
    if not ws:
        return ""
    # Group by top with a small tolerance — these are GLYPH baselines, not row
    # bands. 2pt is tight enough to keep two stacked italic-name rows (5pt
    # apart) in their own groups, but loose enough to fold a 1.7pt-offset
    # chemical subscript ("SiN-LiNbO3", "CuInP2S6") onto the base word so it
    # joins with no space rather than getting orphaned downstream.
    sw = sorted(ws, key=lambda w: w["top"])
    groups: list[list[dict]] = []
    for w in sw:
        if groups and abs(w["top"] - groups[-1][-1]["top"]) <= 2.0:
            groups[-1].append(w)
        else:
            groups.append([w])
    parts: list[str] = []
    for g in groups:
        g.sort(key=lambda w: w["x0"])
        text = _join_words(g)
        if text:
            parts.append(text)
    return " ".join(parts).strip()


def _extract_cell_lines(
    rows: list[dict],
    col_x: tuple[float, float],
    y_range: tuple[float, float],
) -> list[tuple[str, str, float]]:
    """Pull (title, speaker, y) lines out of a single column inside a session's
    Y range. Filters out the larger-font track-topic header words and the bare
    3-letter track labels (M1A, T2B, …) that get rendered next to cells.

    The result is sorted by Y (top to bottom)."""
    cell_words: list[dict] = []
    for r in rows:
        if not (y_range[0] <= r["top"] <= y_range[1]):
            continue
        for w in r["words"]:
            mid = (w["x0"] + w["x1"]) / 2
            if not (col_x[0] <= mid < col_x[1]):
                continue
            if w["size"] >= TOPIC_FONT_MIN:
                continue  # topic headers
            if TRACK_LABEL_RE.match(w["text"]):
                continue  # bare track labels
            cell_words.append(w)
    if not cell_words:
        return []
    # Re-cluster these into lines (cells often print one talk per line; long
    # titles wrap to a second line at the same x0).
    lines = _cluster_rows(cell_words, tol=ROW_TOL)
    out: list[tuple[str, str, float]] = []
    for ln in lines:
        title, speaker = _split_title_speaker(ln["words"], col_x)
        if not title and not speaker:
            continue
        # Drop rows that are obviously non-talk break content (Coffee, Lunch,
        # Plenary, Workshop, …) — these are full-width rows in the PDF that
        # bleed slightly into the column we're harvesting.
        if any(title.startswith(p) for p in NON_TALK_PREFIXES):
            continue
        out.append((title, speaker, ln["top"]))
    # Merge consecutive lines where the second line had no speaker AND
    # comes within 6pt vertically of the previous one — these are wrapped
    # titles.
    merged: list[tuple[str, str, float]] = []
    for title, speaker, top in out:
        if (merged and not speaker
                and abs(top - merged[-1][2]) < 6.0
                and not merged[-1][1]):  # previous also had no speaker
            prev_t, _, prev_y = merged[-1]
            merged[-1] = (prev_t + " " + title, "", prev_y)
        else:
            merged.append((title, speaker, top))
    return merged


# =============================================================================
# Title / speaker post-processing.
# =============================================================================

_INVITED_PREFIXES = ("Invited:", "Invited :", "Invited -")


def _clean_title(raw: str) -> tuple[str, bool]:
    """Strip an 'Invited:' marker and trailing punctuation/colons. Returns
    (clean_title, is_invited)."""
    t = raw.strip()
    is_invited = False
    for pfx in _INVITED_PREFIXES:
        if t.startswith(pfx):
            t = t[len(pfx):].strip()
            is_invited = True
            break
    # Drop a stray trailing colon that the PDF sometimes carries after a
    # right-aligned speaker box.
    t = re.sub(r"[:\s]+$", "", t)
    return t, is_invited


def _clean_speaker(raw: str) -> str:
    s = raw.strip().rstrip(":,;").strip()
    # Collapse internal multi-space runs.
    s = re.sub(r"\s+", " ", s)
    return s


def _talk_id(session_id: str, n: int) -> str:
    return f"{session_id}-T{n:02d}"


def _session_start_iso(day_key: str, hhmm: str) -> str:
    h, m = hhmm.split(":")
    return f"{DAYS[day_key]}T{h}:{m}:00"


def _build_minute_slots(start: str, end: str) -> list[tuple[str, str]]:
    """Return list of (start_iso_time, end_iso_time) 15-minute slots inside
    [start, end). Used to assign a default time to each talk when the PDF row
    didn't provide a finer one (we don't currently propagate per-row times
    through _extract_cell_lines, so all talks inherit the session times)."""
    # Currently unused — kept for future per-talk timing if we wire it in.
    return [(start, end)]


# =============================================================================
# PDF title + non-grid cell harvesting.
#
# Used for sessions whose talks (and titles) come from PDF rows that don't fit
# the wide title-vs-speaker tech grid: the industry-talk blocks and the two
# workshop blocks. Their cells render "Title. Speaker, Affiliation" as one run
# of words with normal letter spacing (no big x-gap), and their session titles
# sit on a dedicated row inside the column rather than as a size-4.56 topic
# header above it.
# =============================================================================

# Title and Speaker are separated by ". " (period + space). The PDF sometimes
# pads or omits the space; allow zero-or-more spaces on either side. Followed
# by a capital letter so we don't split a mid-sentence abbreviation.
_PERIOD_SPLIT_RE = re.compile(r"\s*\.\s+(?=[A-ZÀ-Ý])")
# Speaker, Affiliation separator: a comma with optional whitespace either side.
# The PDF occasionally renders as "Heidi Potts ,Zurich Instruments" (space
# before, none after), so we tolerate both directions.
_COMMA_SPLIT_RE = re.compile(r"\s*,\s*")

# Rows of this content inside a workshop band are panel/meta rows, not talks.
_WORKSHOP_NON_TALK_RE = re.compile(
    r"^(Panel Discussion|Q&A|Lunch|Coffee|Plenary|Poster|WORKSHOP\b)",
    re.IGNORECASE,
)


def _read_pdf_title(
    rows: list[dict],
    pdf_title_spec: dict,
) -> str:
    """Return the session title text read from a specific PDF row.

    Used by workshops and industry sessions, whose header text sits on a
    dedicated row inside the column (not as a larger-font topic banner above
    the column). The spec carries the column index and the target Y; we find
    the row clustered nearest that Y and pull its in-column words.
    """
    col = pdf_title_spec["column"]
    target_y = float(pdf_title_spec["y"])
    col_lo, col_hi = COL_X_RANGES[col]
    # Find the row whose centre is closest to target_y (tolerance: a single
    # ROW_TOL window). Rows further than ROW_TOL away don't actually contain
    # our header.
    candidates = [r for r in rows if abs(r["top"] - target_y) <= ROW_TOL + 0.5]
    if not candidates:
        return ""
    row = min(candidates, key=lambda r: abs(r["top"] - target_y))
    header_words = [
        w for w in row["words"]
        if col_lo <= (w["x0"] + w["x1"]) / 2 < col_hi
    ]
    if not header_words:
        return ""
    header_words.sort(key=lambda w: w["x0"])
    text = _join_words(header_words).strip()
    # Trim a trailing punctuation/colon the renderer sometimes leaves on.
    text = re.sub(r"[:\s]+$", "", text)
    return text


def _topic_header_title(
    rows: list[dict],
    band: tuple[float, float],
    column: int,
) -> str:
    """Return the topic-header text rendered above a tech-track session's
    column, e.g. "Electro-Optic Modulators". The PDF uses a larger 4.56pt
    font for topic headers on Mon/Tue but mysteriously falls back to the
    4.08pt talk-text font on Wed — so we cannot key purely on size.

    Strategy: identify the row immediately above the session's first slot
    that, in this column, looks like a SHORT, NON-TALK row (no large
    word-gap, no time tag, no track label, not a day banner). The
    "Registration, Foyer in front…" banner that sometimes sits just above
    the topic row is filtered out by a non-talk-prefix check.
    """
    col_lo, col_hi = COL_X_RANGES[column]
    # All candidate rows in this column above the session's first slot row
    # but no more than ~25pt above (so we don't reach into a previous block).
    candidates: list[tuple[float, str, float]] = []  # (top, text, size)
    for r in rows:
        if r["top"] > band[0] + 0.5:  # below the band's start — talks, not headers
            continue
        if r["top"] < band[0] - 25:
            continue
        cell = [
            w for w in r["words"]
            if col_lo <= (w["x0"] + w["x1"]) / 2 < col_hi
        ]
        if not cell:
            continue
        cell.sort(key=lambda w: w["x0"])
        # Day banner rows: large font, often contain "JUNE" or weekday.
        sizes = [float(w.get("size", 0)) for w in cell]
        if max(sizes, default=0) >= 4.7:
            continue
        text = _join_words(cell).strip()
        if not text:
            continue
        # Filter generic non-topic banners.
        if text.startswith(("Registration", "Coffee", "Lunch", "Welcome",
                            "Closing", "Opening", "Plenary", "Industry",
                            "Workshop", "Poster", "Panel", "Gala",
                            "Networking", "Bench", "Student", "Zurich",
                            "Lab", "Exhibition", "Session Rooms",
                            "WORKSHOP")):
            continue
        if TIME_RE.match(text.split()[0] if text.split() else ""):
            continue
        if TRACK_LABEL_RE.match(text.split()[0] if text.split() else ""):
            continue
        # If any size-4.56 word, prefer this row strongly.
        candidates.append((r["top"], text, max(sizes)))
    if not candidates:
        return ""
    # Prefer a size-4.56 row when present (Mon/Tue case). Otherwise take
    # the row closest to band[0] from above.
    larger = [c for c in candidates if c[2] >= TOPIC_FONT_MIN]
    if larger:
        chosen = max(larger, key=lambda c: c[0])  # closest from above
    else:
        chosen = max(candidates, key=lambda c: c[0])
    return chosen[1]


def _split_industry_cell(text: str) -> tuple[str, str, str]:
    """Parse one industry/workshop cell into (title, speaker, affiliation).

    The PDF packs the three fields as "Title. Speaker, Affiliation" in one
    continuous run. We split from the right:
      1. The affiliation is everything after the LAST comma.
      2. In the prefix that remains, the title is split from the speaker
         by ". " (period + space + capital letter). Where no such period
         exists, an unambiguous trailing "X Y" name pattern (1–4 words,
         each starting upper-case) is taken as the speaker; otherwise the
         whole prefix is the title and speaker is empty.

    Degenerate inputs:
      - empty / whitespace-only             -> ("", "", "")
      - one company token, no comma         -> ("", "", text)  (sponsor slot)
      - "Bert Offrein" (one name, no comma) -> ("", "Bert Offrein", "")
    """
    def _strip_trailing_punct(s: str) -> str:
        # Some title cells embed an inner comma before the speaker (e.g.
        # "ltoi300: ... PICs, Andrei Kiselev, Luxtelligence SA"). The last
        # comma correctly splits off the affiliation, but the title is then
        # left with a stray ", " or ",". Trim any trailing comma / semicolon
        # / colon / whitespace so titles don't render with that artifact.
        return re.sub(r"[\s,;:]+$", "", s).strip()

    t = text.strip()
    if not t:
        return "", "", ""

    # Strip an opening "." (the PDF sometimes leads with one when a sponsor
    # slot has no title, e.g. ". Frederic Loizeau, Lightium AG").
    t = re.sub(r"^\.\s*", "", t)

    # No commas at all: either a bare affiliation (single sponsor) or a bare
    # speaker (workshop chair). A bare affiliation tends to be a known-company
    # short string like "LIGENTEC SA"; a bare speaker is a 1-3-word personal
    # name. Use word-count + presence of digits/all-caps as a weak signal.
    if "," not in t:
        if _looks_like_person(t):
            return "", _strip_trailing_punct(t), ""
        return "", "", t

    # One or more commas: the LAST comma chunk is the affiliation.
    last_comma = t.rfind(",")
    affiliation = t[last_comma + 1:].strip()
    prefix = t[:last_comma].strip()

    # In the prefix, split title/speaker on ". <Capital>". Look at the LAST
    # such split (titles can legitimately contain period+capital, though rare;
    # the speaker always comes last). If no such split, fall back to "look at
    # the trailing word group: if it looks like a person name (<=4 short
    # capital-led words), take it as the speaker; otherwise treat the whole
    # prefix as title".
    matches = list(_PERIOD_SPLIT_RE.finditer(prefix))
    if matches:
        last = matches[-1]
        title = prefix[:last.start()].strip()
        speaker = prefix[last.end():].strip()
        return _strip_trailing_punct(title), _strip_trailing_punct(speaker), affiliation

    # No period delimiter — look for an implicit speaker tail (a short
    # capital-led name run). Walk back from the end and absorb tokens until
    # we hit one that doesn't look like a name token.
    tokens = prefix.split()
    if not tokens:
        return "", "", affiliation
    # Collect a trailing run of "name-shaped" tokens, max 4.
    tail_start = len(tokens)
    for i in range(len(tokens) - 1, max(-1, len(tokens) - 5), -1):
        if _looks_like_name_token(tokens[i]):
            tail_start = i
        else:
            break
    if tail_start < len(tokens) and tail_start > 0:
        title = " ".join(tokens[:tail_start]).strip()
        speaker = " ".join(tokens[tail_start:]).strip()
        # Sanity: if "title" is suspiciously short (1 word), probably it's
        # actually all a name and there's no title.
        if len(title.split()) <= 1 and _looks_like_person(prefix):
            return "", _strip_trailing_punct(prefix), affiliation
        return _strip_trailing_punct(title), _strip_trailing_punct(speaker), affiliation
    # The whole prefix is name-shaped -> bare-speaker entry.
    if _looks_like_person(prefix):
        return "", _strip_trailing_punct(prefix), affiliation
    # Otherwise treat the whole prefix as title and speaker as empty.
    return _strip_trailing_punct(prefix), "", affiliation


_NAME_TOKEN_RE = re.compile(r"^[A-ZÀ-Ý][A-Za-zÀ-ÿ'’\-]*[A-Za-zÀ-ÿ\-]?\.?$")


def _looks_like_name_token(tok: str) -> bool:
    """A token that could plausibly be part of a personal name."""
    return bool(_NAME_TOKEN_RE.match(tok))


def _looks_like_person(text: str) -> bool:
    """Heuristic: 1-4 words, each starting upper-case, total ≤32 chars, no
    digits, no all-caps abbreviation token at the end. Matches "Bert Offrein",
    "Ana Filipa Carvalho", "Hernán Furci" but not "LIGENTEC SA" or
    "Industry Talk Session 1: Devices"."""
    s = text.strip()
    if not s or any(ch.isdigit() for ch in s):
        return False
    toks = s.split()
    if not (1 <= len(toks) <= 4):
        return False
    if len(s) > 36:
        return False
    for tok in toks:
        if not _looks_like_name_token(tok):
            return False
        # All-caps token of length 3+ is more company-like than name-like
        # (e.g. "IHP", "UCLA"). We allow short initials like "A." but reject
        # bare all-caps words.
        if len(tok) >= 3 and tok.isupper():
            return False
    return True


def _harvest_block_cells(
    rows: list[dict],
    band: tuple[float, float],
    column: int,
) -> list[tuple[str, str, str, float]]:
    """Walk every row whose top sits in [band[0], band[1]] and pick out the
    column's cell content. Return [(title, speaker, affiliation, top_y), …]
    sorted by Y.

    Rows whose max in-column word-gap is wider than SPEAKER_GAP_PT are SKIPPED
    — those are tech-grid rows (title left + right-aligned speaker chip) that
    bleed into the band (the schedule has one such overflow row at 1330-1345
    in the workshop column). Rows matching a workshop-meta pattern (panel
    discussion, lunch, …) are also dropped.
    """
    col_lo, col_hi = COL_X_RANGES[column]
    out: list[tuple[str, str, str, float]] = []
    for r in rows:
        if not (band[0] <= r["top"] <= band[1]):
            continue
        cell = [
            w for w in r["words"]
            if col_lo <= (w["x0"] + w["x1"]) / 2 < col_hi
            and float(w.get("size", 0)) < TOPIC_FONT_MIN
        ]
        if not cell:
            continue
        cell.sort(key=lambda w: w["x0"])
        # Tech-grid rejection: a tech-grid talk has a giant gap between its
        # title block and the right-aligned speaker chip.
        max_gap = 0.0
        for i in range(1, len(cell)):
            max_gap = max(max_gap, cell[i]["x0"] - cell[i - 1]["x1"])
        if max_gap >= SPEAKER_GAP_PT:
            continue
        text = _join_words(cell)
        if not text:
            continue
        if _WORKSHOP_NON_TALK_RE.match(text):
            continue
        if TRACK_LABEL_RE.match(text.split()[0] if text.split() else ""):
            continue
        title, speaker, aff = _split_industry_cell(text)
        out.append((title, speaker, aff, r["top"]))
    out.sort(key=lambda t: t[3])
    return out


def _harvest_per_slot_talks(
    cells: list[tuple[str, str, str, float]],
    sess_start_min: int,
    sess_end_min: int,
    slot_minutes: int,
) -> list[dict]:
    """For a per-slot industry block: assign each harvested cell to a
    fixed-length time slot, in Y order. The PDF prints six 10-min slots for
    the ECIO industry blocks; this function maps the first cell to
    [start, start+slot_minutes), the second to the next slot, and so on.

    Returns a list of dicts {title, speaker, aff, start_min, end_min}.
    """
    out: list[dict] = []
    cur = sess_start_min
    for (title, speaker, aff, _y) in cells:
        nxt = min(cur + slot_minutes, sess_end_min)
        out.append({
            "title": title, "speaker": speaker, "aff": aff,
            "start_min": cur, "end_min": nxt,
        })
        cur = nxt
        if cur >= sess_end_min:
            break
    return out


def _harvest_session_talks(
    cells: list[tuple[str, str, str, float]],
) -> list[dict]:
    """For a session-wide workshop block: emit one talk per non-empty cell,
    with no per-talk time window (they inherit the session start/end)."""
    return [
        {"title": title, "speaker": speaker, "aff": aff,
         "start_min": None, "end_min": None}
        for (title, speaker, aff, _y) in cells
    ]


# =============================================================================
# Driver
# =============================================================================
def main() -> None:
    _bootstrap_pdfplumber()
    log("=" * 72)
    log(f"[config] ECIO 2026 PROCESSOR")
    log(f"[config]   input PDF : {INPUT_PDF}")
    log(f"[config]   output    : {OUTPUT_JSON}")
    log("=" * 72)

    if not INPUT_PDF.exists():
        log(f"[fatal] required input not found: {INPUT_PDF}")
        sys.exit(1)

    import pdfplumber
    log(f"[info] reading {INPUT_PDF.name} …")
    with pdfplumber.open(INPUT_PDF) as pdf:
        page = pdf.pages[0]
        page_h = float(page.height)
    words = _extract_words(INPUT_PDF)
    log(f"[info]   page height {page_h:.1f}; {len(words):,} words extracted.")

    rows = _cluster_rows(words)
    bands = _day_y_bands(rows, page_h)
    log(f"[info]   day bands:")
    for k, (a, b) in bands.items():
        log(f"          {k}: y=[{a:.1f}, {b:.1f}]")

    # ---- Build sessions + talks --------------------------------------------
    sessions_out: list[dict] = []
    talks_out: list[dict] = []
    affiliations_pool: set[str] = set()

    for sess in SKELETON:
        day_key = sess["day"]
        day_iso = DAYS[day_key]
        start_iso = f"{day_iso}T{sess['start']}:00"
        end_iso = f"{day_iso}T{sess['end']}:00"
        room = sess.get("room") or ROOM_BY_COL.get(sess.get("column", 0), "")
        day_band = bands.get(day_key)

        # ---- Resolve the session's display title --------------------------
        # Precedence: explicit `title` -> `pdf_title` directive -> topic
        # header above this session's column (default for tech tracks) ->
        # the track code as a last-resort label.
        title = sess.get("title", "").strip()
        if not title:
            spec = sess.get("pdf_title")
            if spec and spec.get("source") == "row_text":
                title = _read_pdf_title(rows, spec)
            elif (spec and spec.get("source") == "topic_header"
                  and day_band):
                s_min = _hhmm_to_minutes(sess["start"])
                e_min = _hhmm_to_minutes(sess["end"])
                slots = _session_time_slots(words, day_band, s_min, e_min)
                y_range = _harvest_session_y_range(slots, day_band)
                title = _topic_header_title(rows, y_range, spec["column"])
            elif "column" in sess and day_band:
                # Default for tech-track sessions: topic header above the
                # column at this session's Y.
                s_min = _hhmm_to_minutes(sess["start"])
                e_min = _hhmm_to_minutes(sess["end"])
                slots = _session_time_slots(words, day_band, s_min, e_min)
                y_range = _harvest_session_y_range(slots, day_band)
                title = _topic_header_title(rows, y_range, sess["column"])
        if not title:
            title = sess.get("track", "") or "(untitled session)"
            log(f"[warn] no title resolved for {sess['id']}; "
                f"falling back to {title!r}")

        topic_parts = []
        if sess.get("track"):
            topic_parts.append(sess["track"])
        topic = " · ".join(topic_parts) if topic_parts else ""

        s_obj: dict = {
            "id": sess["id"],
            "title": title,
            "color": sess["color"],
            "type": sess["type"],
            "start_ts": start_iso,
            "end_ts": end_iso,
            "talk_ids": [],
        }
        if room:
            s_obj["location"] = room
        if topic:
            s_obj["topic"] = topic
        sessions_out.append(s_obj)

        # ---- Collect this session's talks
        # Each entry is (title, speaker, aff, is_invited, color_override,
        # talk_start_min, talk_end_min). For PDF-harvested talks, color
        # follows the harvest directive ("rose" for industry/workshop) and
        # is_invited stays False. For tech-grid harvested talks, color is
        # decided downstream from is_invited.
        talks_for_session: list[
            tuple[str, str, str, bool, str | None, int | None, int | None]
        ] = []

        if "talks" in sess:
            # Hand-listed talks (plenary speakers only, in this skeleton).
            for t in sess["talks"]:
                ts = t.get("start")
                te = t.get("end")
                t_start_min = _hhmm_to_minutes(ts) if ts else None
                t_end_min = _hhmm_to_minutes(te) if te else None
                talks_for_session.append((
                    t.get("title", "").strip(),
                    t.get("speaker", "").strip(),
                    t.get("speaker_aff", "").strip(),
                    False, t.get("color"),
                    t_start_min, t_end_min,
                ))
        elif "harvest" in sess:
            # Non-grid harvest (industry talks + workshops). Walks the entire
            # band as a block, parsing "Title. Speaker, Affiliation" cells.
            if not day_band:
                log(f"[warn] no day band for {day_key}; skipping {sess['id']}")
                continue
            s_min = _hhmm_to_minutes(sess["start"])
            e_min = _hhmm_to_minutes(sess["end"])
            slots = _session_time_slots(words, day_band, s_min, e_min)
            # For "session" mode (workshops), there may be no slot rows in the
            # session's band (workshops just use the session-wide time). Fall
            # back to a Y range derived from the session's own time bounds.
            if slots:
                y_range = _harvest_session_y_range(slots, day_band)
            else:
                y_range = day_band
            harvest = sess["harvest"]
            cells = _harvest_block_cells(rows, y_range, harvest["column"])
            color_override = harvest.get("talk_color", "rose")
            if harvest.get("slot_mode") == "per_slot":
                slot_minutes = int(harvest.get("slot_minutes", 10))
                parsed = _harvest_per_slot_talks(
                    cells, s_min, e_min, slot_minutes)
            else:
                parsed = _harvest_session_talks(cells)
            for p in parsed:
                if not (p["title"] or p["speaker"] or p["aff"]):
                    continue
                talks_for_session.append((
                    p["title"], p["speaker"], p["aff"],
                    False, color_override,
                    p["start_min"], p["end_min"],
                ))
        elif "column" in sess:
            # Tech-grid harvest (title left, right-aligned speaker chip).
            if not day_band:
                log(f"[warn] no day band for {day_key}; skipping {sess['id']}")
                continue
            s_min = _hhmm_to_minutes(sess["start"])
            e_min = _hhmm_to_minutes(sess["end"])
            slots = _session_time_slots(words, day_band, s_min, e_min)
            y_range = _harvest_session_y_range(slots, day_band)
            col_x = COL_X_RANGES[sess["column"]]
            lines = _extract_cell_lines(rows, col_x, y_range)
            for title_raw, speaker_raw, y in lines:
                t_title, is_invited = _clean_title(title_raw)
                speaker = _clean_speaker(speaker_raw)
                if not t_title and not speaker:
                    continue
                t_start, t_end = _talk_time_window(
                    y, slots, s_min, e_min, is_invited=is_invited)
                talks_for_session.append(
                    (t_title, speaker, "", is_invited, None, t_start, t_end))

        # ---- Emit talks for this session
        for i, (t_title, speaker, aff, is_invited, color_override,
                t_start_min, t_end_min) in enumerate(talks_for_session, 1):
            tid = _talk_id(sess["id"], i)
            if color_override:
                color = color_override
            else:
                color = "indigo" if is_invited else "pink"

            authors: list[dict] = []
            institutions: list[dict] = []
            if speaker:
                a: dict = {"name": speaker}
                if aff:
                    a["insts"] = [1]
                    institutions = [{"n": 1, "name": aff}]
                    affiliations_pool.add(aff)
                else:
                    a["insts"] = []
                authors = [a]
            elif aff:
                # Bare-affiliation sponsor slot (e.g. "LIGENTEC SA"): record
                # the institution but emit no author.
                institutions = [{"n": 1, "name": aff}]
                affiliations_pool.add(aff)

            # Per-talk timing: PDF-harvested talks get the slot window;
            # session-mode entries inherit the session times.
            if t_start_min is not None and t_end_min is not None:
                t_start_iso = (f"{day_iso}T"
                               f"{t_start_min // 60:02d}:"
                               f"{t_start_min %  60:02d}:00")
                t_end_iso = (f"{day_iso}T"
                             f"{t_end_min // 60:02d}:"
                             f"{t_end_min %  60:02d}:00")
            else:
                t_start_iso = start_iso
                t_end_iso = end_iso

            # Pick a sensible placeholder when the PDF cell has no title text
            # (e.g. ". Frederic Loizeau, Lightium AG" or a bare-affiliation
            # sponsor slot like "LIGENTEC SA"). The placeholder uses the
            # session type, not invented title text.
            sess_type = sess.get("type", "")
            if sess_type == "Industry Talks":
                placeholder = "Industry Talk"
            elif sess_type == "Workshop":
                placeholder = "Workshop Panelist"
            else:
                placeholder = "(untitled)"

            talk_obj: dict = {
                "id": tid,
                "session_id": sess["id"],
                "title": t_title or placeholder,
                "color": color,
                "start_ts": t_start_iso,
                "end_ts": t_end_iso,
            }
            if speaker:
                talk_obj["speaker"] = speaker
                talk_obj["speaker_pos"] = 0
                talk_obj["first_author"] = speaker
                talk_obj["last_author"] = speaker
            if authors:
                talk_obj["authors"] = authors
            if institutions:
                talk_obj["institutions"] = institutions
            talks_out.append(talk_obj)
            s_obj["talk_ids"].append(tid)

    # ---- Assemble final JSON ------------------------------------------------
    data = {
        "conference_name": CONFERENCE_NAME,
        "sessions": sessions_out,
        "talks": talks_out,
        "session_types": SESSION_TYPES,
        "talk_types": TALK_TYPES,
    }
    # Optional curator credit (shown in the About section of the built app).
    # Per the schema, the block is rendered only when `name` is non-empty.
    if CURATOR and CURATOR.get("name"):
        cur = {"name": CURATOR["name"]}
        if CURATOR.get("affiliation"):
            cur["affiliation"] = CURATOR["affiliation"]
        if CURATOR.get("link"):
            cur["link"] = CURATOR["link"]
        data["curator"] = cur
    if affiliations_pool:
        data["affiliation_sources"] = {
            "affiliation_full_lines": sorted(affiliations_pool),
        }

    OUTPUT_JSON.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                           encoding="utf-8")
    log(f"[ok] wrote {OUTPUT_JSON.name}: "
        f"{len(sessions_out)} sessions, {len(talks_out)} talks.")
    log("=" * 72)
    log("DONE.")
    log("=" * 72)


if __name__ == "__main__":
    main()
