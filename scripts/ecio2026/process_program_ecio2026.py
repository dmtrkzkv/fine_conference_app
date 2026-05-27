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

import html
import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path


def log(msg: str) -> None:
    print(msg, flush=True)


SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"
INPUT_PDF = DATA_DIR / "ECIO26_DetailedSchedule.pdf"
INPUT_INVITED_HTML = DATA_DIR / "ECIO26_InvitedSpeakers.html"
# Optional web-enrichment HTML pages (all under data/, all `required: no` in
# data_requirements_ecio2026.txt). Each adds detail the detailed-schedule PDF
# doesn't render; the processor uses what's there and falls back when any is
# missing. See `_load_web_enrichment` for how each is wired in.
INPUT_PLENARY_HTML  = DATA_DIR / "ECIO26_PlenarySpeakers.html"
INPUT_WORKSHOPS_HTML = DATA_DIR / "ECIO26_Workshops.html"
INPUT_STUDENT_HTML  = DATA_DIR / "ECIO26_StudentEvent.html"
INPUT_INDUSTRY_HTML = DATA_DIR / "ECIO26_IndustryTalks.html"
INPUT_SOCIAL_HTML   = DATA_DIR / "ECIO26_SocialEvents.html"
INPUT_LABS_HTML     = DATA_DIR / "ECIO26_LabTours.html"
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
    # The entire Sunday programme is one logical "Student Event" session that
    # runs in the Plenary Auditorium (with the pizza dinner moving to a
    # to-be-announced venue at the end). Its three components are emitted as
    # three talks under this single session, so the schedule UI shows them as
    # an expandable group:
    #   1. The workshop on scientific communication (the website is the only
    #      source for its full title; the PDF prints just "Student Workshop").
    #   2. The Bench-to-Business Symposium, which has multiple panellists and
    #      is therefore emitted as a single talk with all panellists listed as
    #      authors. The website carries the panellist roster; if the
    #      enrichment HTML is missing, this talk is emitted with no authors.
    #   3. The Networking Pizza Dinner.
    # Default start/end times here match the PDF's day-block headers; the
    # student-event web page (when present) overrides them with more precise
    # values.
    {"id": "sun-student-event", "day": "sun",
     "start": "13:30", "end": "20:00",
     "title": "Sunday Student Event", "type": "Student Event",
     "color": "orange", "room": PLENARY_ROOM,
     "talks": [
        {"title": "Student Workshop",
         "speaker": "", "speaker_aff": "", "color": "rose",
         "start": "13:30", "end": "15:20"},
        {"title": "Bench to Business Symposium",
         "speaker": "", "speaker_aff": "", "color": "rose",
         "start": "15:30", "end": "16:30"},
        {"title": "Networking Pizza Dinner",
         "speaker": "", "speaker_aff": "", "color": "rose",
         "start": "17:00", "end": "19:30"},
     ]},

    # ---- Monday June 15 -----------------------------------------------------
    {"id": "mon-opening", "day": "mon",
     "start": "08:00", "end": "08:15",
     "title": "Opening Ceremony", "type": "Ceremony",
     "color": "orange", "room": PLENARY_ROOM},

    # Tech-track sessions. Title omitted -> read from the PDF topic-header row
    # above the column. talks parsed from the wide title-vs-speaker grid as
    # before.
    {"id": "M1A", "day": "mon", "start": "08:30", "end": "10:15",
     "type": "Technical Session",
     "color": "blue", "track": "M1A", "column": 1},
    {"id": "M1B", "day": "mon", "start": "08:30", "end": "10:15",
     "type": "Technical Session",
     "color": "blue", "track": "M1B", "column": 2},
    {"id": "M1C", "day": "mon", "start": "08:30", "end": "10:15",
     "type": "Technical Session",
     "color": "blue", "track": "M1C", "column": 3},

    {"id": "M2A", "day": "mon", "start": "10:45", "end": "12:30",
     "type": "Technical Session",
     "color": "blue", "track": "M2A", "column": 1},
    {"id": "M2B", "day": "mon", "start": "10:45", "end": "12:30",
     "type": "Technical Session",
     "color": "blue", "track": "M2B", "column": 2},
    {"id": "M2C", "day": "mon", "start": "10:45", "end": "12:30",
     "type": "Technical Session",
     "color": "blue", "track": "M2C", "column": 3},

    {"id": "M3A", "day": "mon", "start": "13:30", "end": "15:15",
     "type": "Technical Session",
     "color": "blue", "track": "M3A", "column": 1},
    {"id": "M3B", "day": "mon", "start": "13:30", "end": "15:15",
     "type": "Technical Session",
     "color": "blue", "track": "M3B", "column": 2},
    {"id": "M3C", "day": "mon", "start": "13:30", "end": "15:15",
     "type": "Technical Session",
     "color": "blue", "track": "M3C", "column": 3},

    {"id": "mon-poster-blitz-1-1", "day": "mon",
     "start": "15:25", "end": "15:40",
     "title": "Poster Blitz 1.1", "type": "Poster Blitz",
     "color": "amber", "room": ROOM_COL1},
    {"id": "mon-poster-blitz-1-2", "day": "mon",
     "start": "15:40", "end": "15:55",
     "title": "Poster Blitz 1.2", "type": "Poster Blitz",
     "color": "amber", "room": ROOM_COL2},
    {"id": "mon-poster-1", "day": "mon",
     "start": "15:55", "end": "16:55",
     "title": "Coffee + Poster Session 1", "type": "Poster Session",
     "color": "amber", "room": "Foyers in front of Plenary Auditorium"},

    # All three Monday industry sessions run in parallel 16:55–17:55 with six
    # 10-min slots per column. Title text and per-slot talk cells both come
    # straight from the PDF (cells x ≈ 55-415 / 415-770 / 770-1100, header
    # row y ≈ 319.1). _harvest_block_cells parses the "Title. Speaker,
    # Affiliation" run packed inside each cell.
    {"id": "mon-industry-1", "day": "mon",
     "start": "16:55", "end": "17:55",
     "type": "Industry Talks", "color": "amber", "room": ROOM_COL1,
     "pdf_title": {"source": "row_text", "column": 1, "y": 319.1},
     "harvest": {"column": 1, "talk_color": "rose",
                 "slot_mode": "per_slot", "slot_minutes": 10}},
    {"id": "mon-industry-2", "day": "mon",
     "start": "16:55", "end": "17:55",
     "type": "Industry Talks", "color": "amber", "room": ROOM_COL2,
     "pdf_title": {"source": "row_text", "column": 2, "y": 319.1},
     "harvest": {"column": 2, "talk_color": "rose",
                 "slot_mode": "per_slot", "slot_minutes": 10}},
    {"id": "mon-industry-3", "day": "mon",
     "start": "16:55", "end": "17:55",
     "type": "Industry Talks", "color": "amber", "room": ROOM_COL3,
     "pdf_title": {"source": "row_text", "column": 3, "y": 319.1},
     "harvest": {"column": 3, "talk_color": "rose",
                 "slot_mode": "per_slot", "slot_minutes": 10}},

    {"id": "mon-plenary-1", "day": "mon",
     "start": "18:05", "end": "18:50",
     "title": "Plenary Session 1", "type": "Plenary",
     "color": "violet", "room": PLENARY_ROOM,
     "talks": [
        # Plenary "talks" are speaker-only — the PDF prints only the lecturer
        # name on a meta-row, no extractable talk title.
        {"title": "Plenary Lecture", "speaker": "Peter Seitz",
         "speaker_aff": "EPFL", "color": "teal"},
     ]},
    {"id": "mon-welcome", "day": "mon",
     "start": "18:50", "end": "20:30",
     "title": "Welcome Reception", "type": "Social Event",
     "color": "orange",
     "room": "Foyers in front of Session Rooms"},

    # ---- Tuesday June 16 ----------------------------------------------------
    {"id": "T1A", "day": "tue", "start": "08:30", "end": "10:15",
     "type": "Technical Session",
     "color": "blue", "track": "T1A", "column": 1},
    {"id": "T1B", "day": "tue", "start": "08:30", "end": "10:15",
     "type": "Technical Session",
     "color": "blue", "track": "T1B", "column": 2},
    {"id": "T1C", "day": "tue", "start": "08:30", "end": "10:15",
     "type": "Technical Session",
     "color": "blue", "track": "T1C", "column": 3},

    {"id": "T2A", "day": "tue", "start": "10:45", "end": "12:30",
     "type": "Technical Session",
     "color": "blue", "track": "T2A", "column": 1},
    {"id": "T2B", "day": "tue", "start": "10:45", "end": "12:30",
     "type": "Technical Session",
     "color": "blue", "track": "T2B", "column": 2},
    {"id": "T2C", "day": "tue", "start": "10:45", "end": "12:30",
     "type": "Technical Session",
     "color": "blue", "track": "T2C", "column": 3},

    # Workshops: title text + panellist cells both from the PDF (header row
    # y ≈ 508.6, talk cells scattered through y 508..555 inside the column).
    # slot_mode "session" because workshop cells don't line up with the
    # time-slot rows — they're free-form panellist entries.
    {"id": "tue-W1", "day": "tue", "start": "13:30", "end": "15:20",
     "type": "Workshop", "color": "emerald", "room": ROOM_COL1,
     "pdf_title": {"source": "row_text", "column": 1, "y": 508.6},
     "harvest": {"column": 1, "talk_color": "rose",
                 "slot_mode": "session"}},
    {"id": "tue-W2", "day": "tue", "start": "13:30", "end": "15:20",
     "type": "Workshop", "color": "emerald", "room": ROOM_COL2,
     "pdf_title": {"source": "row_text", "column": 2, "y": 508.6},
     "harvest": {"column": 2, "talk_color": "rose",
                 "slot_mode": "session"}},

    {"id": "tue-poster-blitz-2-1", "day": "tue",
     "start": "15:30", "end": "16:00",
     "title": "Poster Blitz 2.1", "type": "Poster Blitz",
     "color": "amber", "room": ROOM_COL1},
    {"id": "tue-poster-blitz-2-2", "day": "tue",
     "start": "15:30", "end": "16:00",
     "title": "Poster Blitz 2.2", "type": "Poster Blitz",
     "color": "amber", "room": ROOM_COL2},
    {"id": "tue-poster-2", "day": "tue",
     "start": "16:00", "end": "17:00",
     "title": "Coffee + Poster Session 2", "type": "Poster Session",
     "color": "amber", "room": "Foyers in front of Plenary Auditorium"},

    {"id": "tue-plenary-2", "day": "tue",
     "start": "17:00", "end": "17:45",
     "title": "Plenary Session 2", "type": "Plenary",
     "color": "violet", "room": PLENARY_ROOM,
     "talks": [
        {"title": "Plenary Lecture", "speaker": "Mona Jarrahi",
         "speaker_aff": "UCLA", "color": "teal"},
     ]},

    {"id": "tue-city-tour", "day": "tue",
     "start": "18:00", "end": "19:00",
     "title": "Zurich City Tour", "type": "Social Event",
     "color": "orange", "room": "Meet at venue"},
    {"id": "tue-gala", "day": "tue",
     "start": "19:00", "end": "23:00",
     "title": "Gala Dinner", "type": "Social Event",
     "color": "orange", "room": "MS Panta Rhei"},

    # ---- Wednesday June 17 --------------------------------------------------
    {"id": "W1A", "day": "wed", "start": "08:30", "end": "10:15",
     "type": "Technical Session",
     "color": "blue", "track": "W1A", "column": 1},
    {"id": "W1B", "day": "wed", "start": "08:30", "end": "10:15",
     "type": "Technical Session",
     "color": "blue", "track": "W1B", "column": 2},

    {"id": "W2A", "day": "wed", "start": "10:45", "end": "12:30",
     "type": "Technical Session",
     "color": "blue", "track": "W2A", "column": 1},
    {"id": "W2B", "day": "wed", "start": "10:45", "end": "12:30",
     "type": "Technical Session",
     "color": "blue", "track": "W2B", "column": 2},
    {"id": "W2C", "day": "wed", "start": "10:45", "end": "12:30",
     "type": "Technical Session",
     "color": "blue", "track": "W2C", "column": 3},

    {"id": "W3A", "day": "wed", "start": "13:30", "end": "15:15",
     "type": "Technical Session",
     "color": "blue", "track": "W3A", "column": 1},
    {"id": "W3B", "day": "wed", "start": "13:30", "end": "15:15",
     "type": "Technical Session",
     "color": "blue", "track": "W3B", "column": 2},

    {"id": "wed-closing", "day": "wed",
     "start": "15:25", "end": "15:40",
     "title": "Closing Ceremony", "type": "Ceremony",
     "color": "orange", "room": ROOM_COL1},
    {"id": "wed-labs", "day": "wed",
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
# Invited-speakers HTML cross-reference
#
# The detailed-schedule PDF prints only the speaker name in each talk cell.
# The conference's public Invited Speakers page is the one source that ties
# each invited speaker to an affiliation, laid out as
#
#   <p><strong>Name</strong></p>
#   <p><em>Affiliation</em></p>
#   <p><strong>"Talk Title"</strong></p>
#
# triples grouped under SC1..SC7 <h2> section headers. We parse these triples
# from the cached HTML and use them to fill `institutions` on the matching
# PDF-harvested talks during emission.
#
# A small alias map covers the cases where the website name does not match
# the PDF speaker name after normalization. These fall into two kinds:
#   (a) PDF spelling drift / typos (e.g. "Hecklemann" vs "Heckelmann");
#   (b) Deliberate substitutions — the talk was announced under one invited
#       speaker but is being presented by a different group member (e.g.
#       Stanford's "Quantum Transducers" talk, announced for Safavi-Naeini
#       on the invited-speakers page, presented at the conference by Samuel
#       Gyger). In those cases we still want the listed affiliation attached
#       to the presenter's talk.
# Keys and values are *unnormalized*; the lookup normalizes both sides.
# =============================================================================

# Curly + straight quote chars that wrap talk titles on the WP page.
_INV_QUOTE_CHARS = "\u201c\u201d\u201e\u201f\u2033\u2036\"'"
_INV_TITLE_QUOTE_RE = re.compile(f"[{_INV_QUOTE_CHARS}]")
_INV_STRIP_QUOTES_RE = re.compile(
    f"^[{_INV_QUOTE_CHARS}]+|[{_INV_QUOTE_CHARS}]+$")
_INV_SECTION_HEADER_RE = re.compile(r"^SC\d+\b")


# Maps Invited-Speakers-page-name -> PDF-schedule speaker name. The processor
# attaches the page's affiliation to a talk whose speaker is *either* the
# website name or this aliased target. Add an entry here whenever the curator
# notices a name in the JSON without an affiliation but a clearly matching
# entry on the invited-speakers page.
INVITED_NAME_ALIASES: dict[str, str] = {
    # PDF schedule prints "Hecklemann"; website spells "Heckelmann".
    "Ina Heckelmann": "Ina Hecklemann",
    # Talk announced under Safavi-Naeini on the invited-speakers page;
    # actually presented by Samuel Gyger (Stanford LINQS).
    "Amir Safavi-Naeini": "Samuel Gyger",
}


def _inv_strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _inv_is_title(text: str) -> bool:
    return bool(_INV_TITLE_QUOTE_RE.search(text))


def _inv_strip_title_quotes(s: str) -> str:
    return _INV_STRIP_QUOTES_RE.sub("", s).strip()


def _norm_name(n: str) -> str:
    """Normalize a name for matching: strip accents, lowercase, collapse
    whitespace, drop punctuation. The canonical name (with accents) stays in
    its original form everywhere else."""
    s = unicodedata.normalize("NFKD", n)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def _parse_invited_html(html_text: str) -> list[dict]:
    """Return [{"name": str, "affiliation": str, "title": str}, ...] for every
    Name/Affiliation/Title triple on the invited-speakers page. Tolerant of
    nested spans, named/numeric entities, curly vs straight quotes, and the
    SC1..SC7 section headers (which we strip before scanning so they can't
    leak in as ghost "name" tokens)."""
    # Drop heading-level wrappers so SC1.. section headers don't appear as
    # bold runs in our token stream.
    body = re.sub(r"<h[1-6]\b[^>]*>.*?</h[1-6]>", "", html_text,
                  flags=re.IGNORECASE | re.DOTALL)

    # Pull every <strong>...</strong> and <em>...</em> run, in document order.
    pat = re.compile(r"<(strong|em)\b[^>]*>(.*?)</\1>",
                     re.IGNORECASE | re.DOTALL)
    tokens: list[tuple[str, str]] = []  # kind ∈ {"name","title","aff"}
    for m in pat.finditer(body):
        tag = m.group(1).lower()
        text = _inv_strip_tags(m.group(2))
        if not text:
            continue
        if _INV_SECTION_HEADER_RE.match(text):
            continue
        if tag == "em":
            tokens.append(("aff", text))
        else:  # strong
            tokens.append(("title" if _inv_is_title(text) else "name", text))

    # Walk tokens and group into (name, aff, title) records. A new "name"
    # token starts a new record; intervening stray tokens are tolerated.
    records: list[dict] = []
    i = 0
    while i < len(tokens):
        if tokens[i][0] != "name":
            i += 1
            continue
        name = tokens[i][1]
        j = i + 1
        aff = ""
        title = ""
        while j < len(tokens) and tokens[j][0] != "name":
            if tokens[j][0] == "aff" and not aff:
                aff = tokens[j][1]
            elif tokens[j][0] == "title" and not title:
                title = _inv_strip_title_quotes(tokens[j][1])
            j += 1
        if aff or title:
            records.append({"name": name, "affiliation": aff, "title": title})
        i = j
    return records


def _load_invited_affiliations(path: Path) -> dict[str, str]:
    """Build a {normalized_speaker_name: affiliation} lookup from the cached
    invited-speakers HTML. Returns an empty dict (with a warning) if the file
    is missing — the pipeline still produces useful JSON, just without
    affiliations on the invited talks."""
    if not path.exists():
        log(f"[warn] invited-speakers HTML not found at {path}; "
            f"talks will be emitted without invited-speaker affiliations.")
        return {}
    records = _parse_invited_html(path.read_text(encoding="utf-8"))
    log(f"[info] parsed {len(records)} entries from {path.name}.")

    lookup: dict[str, str] = {}
    for r in records:
        aff = r["affiliation"]
        if not aff:
            continue
        # Index under the website name itself, plus any curated alias target
        # (so we hit the PDF-printed name too).
        keys = [r["name"]]
        if r["name"] in INVITED_NAME_ALIASES:
            keys.append(INVITED_NAME_ALIASES[r["name"]])
        for k in keys:
            lookup[_norm_name(k)] = aff
    return lookup


# =============================================================================
# Web enrichment: optional HTML pages from the ECIO website that fill in detail
# the detailed-schedule PDF doesn't render. Each parser is tolerant of small
# WordPress-block markup drift (extra spans, attribute reordering, &-entities)
# and returns a small typed struct. _load_web_enrichment() ties them together
# into a single `enrichment` dict that main() consults by session_id and
# speaker name. Every individual file is optional: when missing we just log a
# note and leave the corresponding enrichment empty.
# =============================================================================

# Shared HTML helpers ---------------------------------------------------------

# Block-level tags we replace with whitespace when flattening text. The
# explicit inclusion of <br> is what keeps phrasing like `for AI<br>datacenters`
# from collapsing into the single token `AIdatacenters` after tag-strip — the
# WP block editor sometimes wraps inside a single <strong> across a <br>, so
# adjacent text nodes that visibly appear on two lines arrive in our parser
# with no whitespace between them.
_HTML_BLOCK_TAGS = ("p", "div", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6")
_HTML_BLOCK_TAG_RE = re.compile(
    r"</?(?:" + "|".join(_HTML_BLOCK_TAGS) + r")\b[^>]*>",
    re.IGNORECASE,
)


def _html_collapse_whitespace(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _html_strip_to_text(fragment: str) -> str:
    """Return plain text for an HTML fragment: drop tags (block-level tags
    become a space first, so adjacent text nodes that visibly appeared on
    separate lines keep their word boundary), decode entities, normalise
    whitespace."""
    s = _HTML_BLOCK_TAG_RE.sub(" ", fragment)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return _html_collapse_whitespace(s)


def _html_strip_quote_chars(s: str) -> str:
    """Strip curly/straight quote chars from the ends of a title string."""
    return _INV_STRIP_QUOTES_RE.sub("", s).strip()


def _html_extract_main(html_text: str) -> str:
    """Focus parsing on the page body: when a <main>…</main> wrapper is
    present we return its inner content; otherwise we drop the obvious
    non-content chrome (scripts, nav, headers, footers, asides)."""
    m = re.search(r"<main\b[^>]*>(.*?)</main>",
                  html_text, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1)
    s = html_text
    for tag in ("script", "style", "nav", "header", "footer", "aside"):
        s = re.sub(rf"<{tag}\b[^>]*>.*?</{tag}>", "",
                   s, flags=re.IGNORECASE | re.DOTALL)
    return s


# Page parser: plenary speakers -----------------------------------------------

def _parse_plenary_html(html_text: str) -> list[dict]:
    """Return [{name, affiliation, title, abstract, bio}, …] for each plenary
    speaker on the page. The structure is one <h2> per speaker followed by a
    sequence of <p> blocks; we classify each <p> by content (a <p> opening
    with a curly/straight quote char is the talk title; the first short
    non-quoted <p> is the affiliation; the rest is prose). The first prose
    paragraph that opens with the speaker's first name marks the start of
    the bio; everything before is the abstract."""
    body = _html_extract_main(html_text)
    body = re.sub(r"<h1\b[^>]*>.*?</h1>", "",
                  body, flags=re.IGNORECASE | re.DOTALL)
    chunks = re.split(r"(<h2\b[^>]*>.*?</h2>)",
                      body, flags=re.IGNORECASE | re.DOTALL)
    out: list[dict] = []
    for i in range(1, len(chunks), 2):
        head = chunks[i]
        rest = chunks[i + 1] if i + 1 < len(chunks) else ""
        name = _html_strip_to_text(head)
        # Drop a leading "Prof. Dr." / "Dr." / "Prof." honorific so the name
        # matches what the PDF schedule prints in its speaker cells.
        name = re.sub(r"^(?:Prof\.?\s+Dr\.?|Dr\.?|Prof\.?)\s+", "", name)
        paras = re.findall(r"<p\b[^>]*>(.*?)</p>",
                           rest, re.IGNORECASE | re.DOTALL)
        aff = ""
        title = ""
        prose: list[str] = []
        for raw in paras:
            txt = _html_strip_to_text(raw)
            if not txt:
                continue
            if not aff and not _INV_TITLE_QUOTE_RE.search(txt) and len(txt) < 200:
                aff = txt
                continue
            if not title and txt.lstrip()[:1] in _INV_QUOTE_CHARS:
                title = _html_strip_quote_chars(txt)
                continue
            prose.append(txt)
        abstract = ""
        bio = ""
        if prose:
            first_name = name.split()[0] if name else ""
            bio_idx = None
            for j, p in enumerate(prose):
                if first_name and p.startswith(first_name):
                    bio_idx = j
                    break
            if bio_idx is None:
                abstract = "\n\n".join(prose)
            else:
                abstract = "\n\n".join(prose[:bio_idx])
                bio = "\n\n".join(prose[bio_idx:])
        out.append({
            "name": name, "affiliation": aff, "title": title,
            "abstract": abstract, "bio": bio,
        })
    # Defensive: drop records whose "name" doesn't look like a person name
    # (contains a colon, is implausibly long, or matches the SC\d+ section
    # header pattern used elsewhere on the ECIO site).
    out = [r for r in out
           if r["name"] and ":" not in r["name"] and len(r["name"]) <= 60
           and not _INV_SECTION_HEADER_RE.match(r["name"])]
    return out


# Page parser: workshops ------------------------------------------------------

_WORKSHOP_HEAD_RE = re.compile(r"\bworkshop\s*(\d+)?\b", re.IGNORECASE)
_WORKSHOP_PLACEHOLDER_PHRASES = (
    "workshop panelist", "coming soon", "coming soon..", "coming soon ..",
    "coming soon ...",
)


def _parse_workshops_html(html_text: str) -> list[dict]:
    """Return [{position, title, chair, panelists: [{name, aff, talk_title}]}, …]
    for each workshop on the page. The page has 2 workshops, each opening
    with a "WORKSHOP N" paragraph; the H2 immediately after carries the
    workshop topic. Inside each block, panelists are laid out as:
        <p><strong>Name</strong></p>
        <p><em>Affiliation</em></p>
        <p><strong>"Talk Title"</strong></p>     (optional)
    Each row is one <p>; we walk paragraphs in document order and classify
    them by content."""
    body = _html_extract_main(html_text)
    chunks: list[tuple[str, str, str]] = []
    for m in re.finditer(r"<(h2|p)\b[^>]*>(.*?)</\1>",
                         body, re.IGNORECASE | re.DOTALL):
        kind = m.group(1).lower()
        raw = m.group(2)
        plain = _html_strip_to_text(raw)
        if plain:
            chunks.append((kind, raw, plain))

    workshops: list[dict] = []
    cur_workshop: dict | None = None
    cur_panelist: dict | None = None

    def _flush_panelist() -> None:
        nonlocal cur_panelist
        if cur_workshop is not None and cur_panelist is not None and (
            cur_panelist["name"] or cur_panelist["aff"]
        ):
            cur_workshop["panelists"].append(cur_panelist)
        cur_panelist = None

    def _flush_workshop() -> None:
        nonlocal cur_workshop
        _flush_panelist()
        if cur_workshop is not None:
            workshops.append(cur_workshop)
            cur_workshop = None

    for kind, raw, plain in chunks:
        plain_lower = plain.lower()

        # Workshop-header paragraph: short, contains "WORKSHOP N", no chair /
        # panelist keyword.
        m_h = _WORKSHOP_HEAD_RE.search(plain)
        if (kind == "p" and m_h
                and "chair" not in plain_lower
                and "panelist" not in plain_lower
                and len(plain) < 40):
            _flush_workshop()
            pos_str = m_h.group(1)
            position = int(pos_str) if pos_str else len(workshops) + 1
            cur_workshop = {
                "position": position, "title": "", "chair": "",
                "panelists": [],
            }
            continue

        if cur_workshop is None:
            continue

        # Workshop topic from the H2 following the header.
        if kind == "h2" and not cur_workshop["title"]:
            cur_workshop["title"] = plain
            continue

        # Chair line: name follows the colon in the same paragraph.
        if "workshop chair" in plain_lower:
            after = plain.split(":", 1)[1].strip() if ":" in plain else ""
            cur_workshop["chair"] = after
            _flush_panelist()
            continue

        # Placeholder: closes the current panelist without modifying fields.
        if plain_lower in _WORKSHOP_PLACEHOLDER_PHRASES:
            _flush_panelist()
            continue

        strong_texts = [_html_strip_to_text(m.group(1)) for m in re.finditer(
            r"<strong\b[^>]*>(.*?)</strong>", raw,
            re.IGNORECASE | re.DOTALL)]
        em_texts = [_html_strip_to_text(m.group(1)) for m in re.finditer(
            r"<em\b[^>]*>(.*?)</em>", raw,
            re.IGNORECASE | re.DOTALL)]
        strong_texts = [t for t in strong_texts if t]
        em_texts = [t for t in em_texts if t]

        # Affiliation paragraph: italic-only.
        if em_texts and not strong_texts:
            if cur_panelist is not None:
                cur_panelist["aff"] = em_texts[0]
            continue

        # Talk-title paragraph: the plain text begins or ends with a quote.
        stripped = plain.strip()
        if (stripped[:1] in _INV_QUOTE_CHARS
                or stripped[-1:] in _INV_QUOTE_CHARS):
            if cur_panelist is not None:
                cur_panelist["talk_title"] = _html_strip_quote_chars(stripped)
            continue

        # Panelist name: opens a new panelist record.
        if strong_texts:
            _flush_panelist()
            if _WORKSHOP_HEAD_RE.fullmatch(strong_texts[0].strip()):
                continue
            cur_panelist = {
                "name": " ".join(strong_texts).strip(),
                "aff": "", "talk_title": "",
            }
            continue

    _flush_workshop()
    return workshops


# Page parser: Sunday student event -------------------------------------------

def _parse_student_event_html(html_text: str) -> dict:
    """Return {workshop?, bench?, pizza?} dicts for the three sub-events on
    the page. Each carries any of: title, location, start, end. The bench
    record also carries `panelists: [{name, aff}, …]` parsed from the
    <em>Name, Affiliation</em> paragraphs between the bench header and the
    pizza header."""
    body = _html_extract_main(html_text)
    paras = re.findall(r"<p\b[^>]*>(.*?)</p>",
                       body, re.IGNORECASE | re.DOTALL)
    out: dict = {"workshop": {}, "bench": {}, "pizza": {}}
    section: str | None = None
    time_re = re.compile(
        r"(\d{1,2}:\d{2})\s*[-\u2013\u2014]\s*(\d{1,2}:\d{2})")
    for raw in paras:
        plain = _html_strip_to_text(raw)
        if not plain:
            continue
        lower = plain.lower()
        if "workshop on scientific communication" in lower:
            section = "workshop"
            out[section]["title"] = "Workshop on Scientific Communication"
        elif "bench to business" in lower:
            section = "bench"
        elif "pizza dinner" in lower or "networking and pizza" in lower:
            section = "pizza"
        else:
            # Continuation: bench panelists are <em>Name, Affiliation</em>.
            if section == "bench" and "," in plain and len(plain) < 200:
                name, _, aff = plain.partition(",")
                out[section].setdefault("panelists", []).append(
                    {"name": name.strip(), "aff": aff.strip()})
            continue
        m_t = time_re.search(plain)
        if m_t:
            out[section]["start"] = m_t.group(1)
            out[section]["end"] = m_t.group(2)
        m_loc = re.search(r"<em\b[^>]*>(.*?)</em>",
                          raw, re.IGNORECASE | re.DOTALL)
        if m_loc:
            loc = _html_strip_to_text(m_loc.group(1))
            if loc and loc.lower() not in ("th",):
                out[section]["location"] = loc
    return out


# Page parser: industry talks -------------------------------------------------

_INDUSTRY_SECTION_RE = re.compile(
    r"<h3\b[^>]*>\s*Session\s*(\d+)\s*:\s*([^<]*)</h3>",
    re.IGNORECASE,
)


def _parse_industry_html(html_text: str) -> list[dict]:
    """Return [{position, label, talks: [{company, name, title}]}, …]. The
    page is laid out as three <h3>Session N: <label></h3> blocks; inside each
    block, talks are introduced by <h4>company</h4> followed by <p>s with the
    talk title (in curly quotes inside a <strong>) and a "Speaker: <name>"
    line."""
    body = _html_extract_main(html_text)
    headers = list(_INDUSTRY_SECTION_RE.finditer(body))
    if not headers:
        return []
    sessions: list[dict] = []
    for i, m in enumerate(headers):
        position = int(m.group(1))
        label = m.group(2).strip()
        block_start = m.end()
        block_end = (headers[i + 1].start()
                     if i + 1 < len(headers) else len(body))
        block = body[block_start:block_end]

        company_split = re.split(
            r"<h4\b[^>]*>(.*?)</h4>", block, flags=re.IGNORECASE | re.DOTALL)
        talks: list[dict] = []
        for j in range(1, len(company_split), 2):
            company = _html_strip_to_text(company_split[j])
            sub = company_split[j + 1] if j + 1 < len(company_split) else ""
            title = ""
            for sm in re.finditer(r"<strong\b[^>]*>(.*?)</strong>",
                                  sub, re.IGNORECASE | re.DOTALL):
                txt = _html_strip_to_text(sm.group(1))
                if not txt:
                    continue
                if _inv_is_title(txt) or txt.lower().startswith("coming soon"):
                    title = _inv_strip_title_quotes(txt)
                    break
            plain = _html_strip_to_text(sub)
            name = ""
            sp = re.search(r"Speaker\s*:\s*([^\n,;.]+)", plain)
            if sp:
                cand = sp.group(1).strip()
                if cand and not cand.lower().startswith("coming soon"):
                    name = cand
            talks.append({"company": company, "name": name, "title": title})
        sessions.append({
            "position": position, "label": label, "talks": talks,
        })
    return sessions


# Page parser: social events --------------------------------------------------

def _parse_social_html(html_text: str) -> list[dict]:
    """Return [{heading, description}, …] for each <h3>-introduced social
    event on the page."""
    body = _html_extract_main(html_text)
    body = re.sub(r"<h1\b[^>]*>.*?</h1>", "",
                  body, flags=re.IGNORECASE | re.DOTALL)
    chunks = re.split(r"(<h3\b[^>]*>.*?</h3>)",
                      body, flags=re.IGNORECASE | re.DOTALL)
    out: list[dict] = []
    for i in range(1, len(chunks), 2):
        heading = _html_strip_to_text(chunks[i])
        sub = chunks[i + 1] if i + 1 < len(chunks) else ""
        paras = re.findall(r"<p\b[^>]*>(.*?)</p>",
                           sub, re.IGNORECASE | re.DOTALL)
        text_paras = []
        for p in paras:
            t = _html_strip_to_text(p)
            if not t:
                continue
            # Skip pure photo-credit paragraphs ("(© …)").
            if t.startswith("(") and t.endswith(")") and "©" in t:
                continue
            text_paras.append(t)
        description = "\n\n".join(text_paras)
        if heading:
            out.append({"heading": heading, "description": description})
    return out


# Page parser: lab tours ------------------------------------------------------

def _parse_lab_tours_html(html_text: str) -> list[dict]:
    """Return [{heading, description}, …] — one record per <h2>-introduced
    visit on the page (ETH Lab Tour / Menhir / Lightium / …). The <p> body
    that follows is the prose description; "Find out more:" footers are
    dropped."""
    body = _html_extract_main(html_text)
    body = re.sub(r"<h1\b[^>]*>.*?</h1>", "",
                  body, flags=re.IGNORECASE | re.DOTALL)
    chunks = re.split(r"(<h2\b[^>]*>.*?</h2>)",
                      body, flags=re.IGNORECASE | re.DOTALL)
    out: list[dict] = []
    for i in range(1, len(chunks), 2):
        heading = _html_strip_to_text(chunks[i])
        sub = chunks[i + 1] if i + 1 < len(chunks) else ""
        paras = re.findall(r"<p\b[^>]*>(.*?)</p>",
                           sub, re.IGNORECASE | re.DOTALL)
        text_paras = []
        for p in paras:
            t = _html_strip_to_text(p)
            if not t:
                continue
            if t.lower().startswith("find out more"):
                continue
            text_paras.append(t)
        description = "\n\n".join(text_paras)
        if heading:
            out.append({"heading": heading, "description": description})
    # Defensive: drop SC\d+-shaped headings (used on the invited-speakers
    # page) and implausibly long headings.
    out = [r for r in out
           if not _INV_SECTION_HEADER_RE.match(r["heading"])
           and len(r["heading"]) <= 100]
    return out


# Web-enrichment loader -------------------------------------------------------

# Mapping of (workshop position) → SKELETON session_id.
_WORKSHOP_POS_TO_SID = {1: "tue-W1", 2: "tue-W2"}

# Mapping of (industry-session position) → SKELETON session_id.
_INDUSTRY_POS_TO_SID = {
    1: "mon-industry-1",
    2: "mon-industry-2",
    3: "mon-industry-3",
}

# Mapping of social-event heading prefix → SKELETON session_id. We match on a
# lowercased prefix because the heading lines also carry a date and time we
# don't want to re-parse.
_SOCIAL_HEADING_TO_SID = {
    "welcome reception": "mon-welcome",
    "zurich city tour":  "tue-city-tour",
    "gala dinner":       "tue-gala",
}


def _load_web_enrichment() -> dict:
    """Read all six optional enrichment HTML files into one structured dict
    that main() consults during emission. Any missing file leaves its branch
    empty (with a warning) — emission falls back to whatever the PDF gives
    us."""
    enrich: dict = {
        "plenary": {}, "workshops": {}, "student": {},
        "industry": {}, "social": {}, "lab_tours": [],
    }

    if INPUT_PLENARY_HTML.exists():
        recs = _parse_plenary_html(INPUT_PLENARY_HTML.read_text(encoding="utf-8"))
        log(f"[info] plenary HTML       : {len(recs)} speaker(s) parsed.")
        for r in recs:
            if r["name"]:
                enrich["plenary"][_norm_name(r["name"])] = r
    else:
        log(f"[warn] plenary HTML not found at {INPUT_PLENARY_HTML.name}; "
            f"plenary talks will use PDF placeholder titles only.")

    if INPUT_WORKSHOPS_HTML.exists():
        recs = _parse_workshops_html(
            INPUT_WORKSHOPS_HTML.read_text(encoding="utf-8"))
        log(f"[info] workshops HTML     : {len(recs)} workshop(s) parsed.")
        for r in recs:
            sid = _WORKSHOP_POS_TO_SID.get(r["position"])
            if sid:
                enrich["workshops"][sid] = r
    else:
        log(f"[warn] workshops HTML not found; workshop sessions will use "
            f"PDF-harvested titles + panellists only.")

    if INPUT_STUDENT_HTML.exists():
        rec = _parse_student_event_html(
            INPUT_STUDENT_HTML.read_text(encoding="utf-8"))
        present = [k for k, v in rec.items() if v]
        log(f"[info] student-event HTML : sub-events parsed: {present}")
        enrich["student"] = rec
    else:
        log(f"[warn] student-event HTML not found; Sunday student-event "
            f"talks will use SKELETON defaults only (no Bench-to-Business "
            f"panellists, no website-derived time overrides).")

    if INPUT_INDUSTRY_HTML.exists():
        recs = _parse_industry_html(
            INPUT_INDUSTRY_HTML.read_text(encoding="utf-8"))
        log(f"[info] industry HTML      : {len(recs)} session(s), "
            f"{sum(len(r['talks']) for r in recs)} talk(s) parsed.")
        for r in recs:
            sid = _INDUSTRY_POS_TO_SID.get(r["position"])
            if sid:
                enrich["industry"][sid] = r
    else:
        log(f"[warn] industry-talks HTML not found; industry sessions will "
            f"use the noisier PDF-harvested talk cells.")

    if INPUT_SOCIAL_HTML.exists():
        recs = _parse_social_html(
            INPUT_SOCIAL_HTML.read_text(encoding="utf-8"))
        log(f"[info] social-events HTML : {len(recs)} event(s) parsed.")
        for r in recs:
            lower = r["heading"].lower()
            for prefix, sid in _SOCIAL_HEADING_TO_SID.items():
                if lower.startswith(prefix):
                    enrich["social"][sid] = r["description"]
                    break
    else:
        log(f"[warn] social-events HTML not found; social-event sessions "
            f"will be emitted without descriptions.")

    if INPUT_LABS_HTML.exists():
        recs = _parse_lab_tours_html(
            INPUT_LABS_HTML.read_text(encoding="utf-8"))
        log(f"[info] lab-tours HTML     : {len(recs)} visit option(s) parsed.")
        enrich["lab_tours"] = recs
    else:
        log(f"[warn] lab-tours HTML not found; the lab-tours session will "
            f"be emitted as a single SKELETON entry with no talk options.")

    return enrich


# =============================================================================
# Driver
# =============================================================================
def main() -> None:
    _bootstrap_pdfplumber()
    log("=" * 72)
    log(f"[config] ECIO 2026 PROCESSOR")
    log(f"[config]   input PDF       : {INPUT_PDF}")
    log(f"[config]   invited HTML    : {INPUT_INVITED_HTML}")
    log(f"[config]   enrichment HTML : {DATA_DIR} (6 optional files)")
    log(f"[config]   output          : {OUTPUT_JSON}")
    log("=" * 72)

    if not INPUT_PDF.exists():
        log(f"[fatal] required input not found: {INPUT_PDF}")
        sys.exit(1)

    # Load the {normalized_name -> affiliation} lookup from the cached
    # invited-speakers page. Missing file is non-fatal: the pipeline still
    # produces valid JSON, just without invited-speaker affiliations.
    invited_aff = _load_invited_affiliations(INPUT_INVITED_HTML)

    # Load the optional web-enrichment HTML pages. Each is independently
    # optional; missing ones leave their branch of `enrich` empty and the
    # session/talk pipeline falls back to whatever the PDF harvested.
    log("-" * 72)
    log("[info] loading optional web-enrichment pages …")
    enrich = _load_web_enrichment()

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
    # Telemetry: how many talks had their affiliation filled from the cached
    # invited-speakers page (vs already-present from the PDF harvest).
    invited_filled_count = 0
    invited_filled_speakers: list[str] = []

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
        # Each entry is a dict with these keys (any may be empty/None):
        #   title         : the talk title
        #   speaker       : presenting-author name (becomes the first author)
        #   aff           : presenting-author affiliation
        #   is_invited    : True for "Invited:" tech-track talks
        #   color         : color override (e.g. "rose" for industry/workshop)
        #                   or None to derive from is_invited downstream
        #   start_min/end_min: per-talk timing in minutes-since-midnight, or
        #                   None to inherit the session's start/end
        #   abstract      : optional abstract/bio prose
        #   extra_authors : optional [{name, aff}, …] appended to the author
        #                   list. Used for multi-author talks such as the
        #                   Bench-to-Business symposium panellist roster.
        talks_for_session: list[dict] = []

        if "talks" in sess:
            # Hand-listed talks (plenary speakers + Sunday components).
            for t in sess["talks"]:
                ts = t.get("start")
                te = t.get("end")
                talks_for_session.append({
                    "title": t.get("title", "").strip(),
                    "speaker": t.get("speaker", "").strip(),
                    "aff": t.get("speaker_aff", "").strip(),
                    "is_invited": False,
                    "color": t.get("color"),
                    "start_min": _hhmm_to_minutes(ts) if ts else None,
                    "end_min": _hhmm_to_minutes(te) if te else None,
                    "abstract": "",
                    "extra_authors": [],
                })
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
                talks_for_session.append({
                    "title": p["title"],
                    "speaker": p["speaker"],
                    "aff": p["aff"],
                    "is_invited": False,
                    "color": color_override,
                    "start_min": p["start_min"],
                    "end_min": p["end_min"],
                    "abstract": "",
                    "extra_authors": [],
                })
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
                talks_for_session.append({
                    "title": t_title, "speaker": speaker, "aff": "",
                    "is_invited": is_invited, "color": None,
                    "start_min": t_start, "end_min": t_end,
                    "abstract": "", "extra_authors": [],
                })

        # ---- Apply web enrichment overrides (when the corresponding HTML
        # page was present and parsed). Each branch below either *augments*
        # PDF-derived talks (e.g. attaching an abstract to a plenary lecture)
        # or *replaces* them entirely (e.g. swapping in the website's clean
        # industry-talk list for the noisy PDF cell harvest). The session
        # object itself can also gain a `description` (social events) or
        # `chair` note in its topic (workshops) here.
        sid = sess["id"]

        # Plenary: augment the hand-listed lecture with the website's title,
        # affiliation, abstract, and bio.
        if sess.get("type") == "Plenary" and enrich["plenary"]:
            for tk in talks_for_session:
                rec = enrich["plenary"].get(_norm_name(tk["speaker"]))
                if not rec:
                    continue
                if rec.get("title"):
                    tk["title"] = rec["title"]
                if rec.get("affiliation") and not tk["aff"]:
                    tk["aff"] = rec["affiliation"]
                # Concatenate abstract + bio into one prose field (the talk
                # schema only renders a single abstract block).
                pieces = []
                if rec.get("abstract"):
                    pieces.append(rec["abstract"])
                if rec.get("bio"):
                    pieces.append(f"About the speaker:\n\n{rec['bio']}")
                if pieces:
                    tk["abstract"] = "\n\n".join(pieces)

        # Workshops: swap PDF-harvested panellists for the website's clean
        # (Name, Affiliation, Talk Title) triples; surface the website's
        # topic title; surface the workshop chair in the topic line.
        if sid in enrich["workshops"]:
            w = enrich["workshops"][sid]
            if w.get("title"):
                s_obj["title"] = w["title"]
            if w.get("chair"):
                chair_note = f"Chair: {w['chair']}"
                s_obj["topic"] = (
                    f"{s_obj['topic']} · {chair_note}"
                    if s_obj.get("topic") else chair_note
                )
            if w.get("panelists"):
                talks_for_session = [{
                    "title": p["talk_title"],
                    "speaker": p["name"],
                    "aff": p["aff"],
                    "is_invited": False,
                    "color": "rose",
                    "start_min": None, "end_min": None,
                    "abstract": "", "extra_authors": [],
                } for p in w["panelists"]]

        # Sunday Student Event: the session is a single hand-listed entry
        # with three talks. The enrichment fills in (a) the proper title of
        # the scientific-communication workshop, (b) the Bench-to-Business
        # panellist roster — attached as multiple authors on that single
        # talk rather than as separate talks — and (c) precise per-talk
        # times that the website carries but the PDF doesn't.
        if sid == "sun-student-event" and enrich["student"]:
            student = enrich["student"]
            # talks_for_session is in SKELETON order: workshop, bench, pizza.
            # We index by title-prefix rather than position so reordering the
            # SKELETON later doesn't silently swap content.
            by_kind: dict[str, dict] = {}
            for tk in talks_for_session:
                low = tk["title"].lower()
                if "workshop" in low or "scientific communication" in low:
                    by_kind["workshop"] = tk
                elif "bench" in low:
                    by_kind["bench"] = tk
                elif "pizza" in low or "networking" in low:
                    by_kind["pizza"] = tk

            for kind in ("workshop", "bench", "pizza"):
                rec = student.get(kind)
                tk = by_kind.get(kind)
                if not (rec and tk):
                    continue
                if rec.get("title"):
                    tk["title"] = rec["title"]
                if rec.get("start") and rec.get("end"):
                    tk["start_min"] = _hhmm_to_minutes(rec["start"])
                    tk["end_min"] = _hhmm_to_minutes(rec["end"])
                # Per-talk location override: the student-event page sometimes
                # specifies a different room for an individual sub-event
                # (e.g. "ETH HG, Audi Max" for the workshop, vs the session-
                # level room "HG F30, Plenary Auditorium"). We carry this on
                # the talk dict as `location` and the emit loop below picks
                # it up to override the session-default location.
                if rec.get("location"):
                    tk["location"] = rec["location"]
                # Bench-to-Business: the panellist list becomes co-authors
                # on this single talk rather than separate talks.
                if kind == "bench":
                    tk["extra_authors"] = [
                        {"name": p["name"], "aff": p["aff"]}
                        for p in rec.get("panelists", [])
                        if p.get("name")
                    ]

        # Industry talks: the PDF cells for these are notoriously hard to
        # parse, so when the industry-talks page is available we *replace*
        # the PDF harvest with its clean (Company, Talk Title, Speaker)
        # triples.
        if sid in enrich["industry"]:
            ind = enrich["industry"][sid]
            if ind.get("label"):
                s_obj["title"] = f"Industry Talks · {ind['label']}"
            new_talks: list[dict] = []
            s_min = _hhmm_to_minutes(sess["start"])
            e_min = _hhmm_to_minutes(sess["end"])
            n = len(ind.get("talks", []))
            slot = (e_min - s_min) // n if n else 0
            for i, t in enumerate(ind["talks"]):
                ts = s_min + i * slot
                te = ts + slot if slot else e_min
                new_talks.append({
                    "title": t["title"] or "Industry Talk",
                    "speaker": t["name"],
                    "aff": t["company"],
                    "is_invited": False,
                    "color": "rose",
                    "start_min": ts if slot else None,
                    "end_min":   te if slot else None,
                    "abstract": "", "extra_authors": [],
                })
            if new_talks:
                talks_for_session = new_talks

        # Social events: attach the website's description to the session
        # object. No talks are synthesized — social events have no
        # presenters in any meaningful sense; the description belongs on the
        # session itself.
        if sid in enrich["social"]:
            s_obj["description"] = enrich["social"][sid]

        # Lab tours: synthesize one talk per visit option, with the visit
        # name as title and the description as abstract.
        if sid == "wed-labs" and enrich["lab_tours"]:
            talks_for_session = [{
                "title": v["heading"],
                "speaker": "", "aff": "",
                "is_invited": False, "color": "rose",
                "start_min": None, "end_min": None,
                "abstract": v["description"],
                "extra_authors": [],
            } for v in enrich["lab_tours"]]

        # ---- Emit talks for this session
        for i, tk in enumerate(talks_for_session, 1):
            t_title = tk["title"]
            speaker = tk["speaker"]
            aff = tk["aff"]
            is_invited = tk["is_invited"]
            color_override = tk["color"]
            t_start_min = tk["start_min"]
            t_end_min = tk["end_min"]
            t_abstract = tk.get("abstract", "")
            extra_authors_in = tk.get("extra_authors", []) or []
            tid = _talk_id(sess["id"], i)
            if color_override:
                color = color_override
            else:
                color = "indigo" if is_invited else "pink"

            authors: list[dict] = []
            institutions: list[dict] = []
            # Build the author + institution lists. The presenting speaker (if
            # any) becomes the first author; extra_authors are appended after.
            # Affiliations are deduplicated into a single institutions list and
            # each author's `insts` field carries 1-based indices into it.
            author_inputs: list[tuple[str, str]] = []  # (name, aff)
            if speaker:
                # Fall back to the invited-speakers cross-reference when the
                # PDF cell didn't carry an affiliation. PDF-harvested talks
                # from the tech-grid never do (the grid prints only speaker
                # name + title), so this fill is what gives invited speakers
                # their institution in the final JSON.
                if not aff and invited_aff:
                    looked_up = invited_aff.get(_norm_name(speaker), "")
                    if looked_up:
                        aff = looked_up
                        invited_filled_count += 1
                        invited_filled_speakers.append(speaker)
                author_inputs.append((speaker, aff))
            for ea in extra_authors_in:
                nm = (ea.get("name") or "").strip()
                af = (ea.get("aff") or "").strip()
                if nm:
                    author_inputs.append((nm, af))

            if author_inputs:
                inst_map: dict[str, int] = {}  # affiliation -> 1-based id
                for nm, af in author_inputs:
                    a: dict = {"name": nm}
                    if af:
                        if af not in inst_map:
                            inst_map[af] = len(inst_map) + 1
                            institutions.append({"n": inst_map[af], "name": af})
                            affiliations_pool.add(af)
                        a["insts"] = [inst_map[af]]
                    else:
                        a["insts"] = []
                    authors.append(a)
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
            # Inherit the session's room as the talk's location. The schedule
            # PDF prints rooms only at the session-block level (one column per
            # room), so every talk in a session shares its parent's location.
            # An enrichment branch above (e.g. the student-event integration)
            # can override this for an individual talk by setting tk["location"];
            # in that case we use the per-talk value.
            t_location = tk.get("location") or room
            if t_location:
                talk_obj["location"] = t_location
            # Author-display fields.
            # - `speaker` / `speaker_pos` mark the presenting author (only set
            #   when there is one; multi-author talks with no presenter, like
            #   the Sunday Bench-to-Business panel, omit both).
            # - `first_author` / `last_author` are taken from the authors list
            #   and used by the legacy byline and the search indexer.
            if speaker:
                talk_obj["speaker"] = speaker
                talk_obj["speaker_pos"] = 0
            if authors:
                talk_obj["first_author"] = authors[0]["name"]
                talk_obj["last_author"] = authors[-1]["name"]
            elif speaker:
                # Defensive: a `speaker` without a populated authors list
                # shouldn't happen given the construction above, but keep the
                # legacy fields populated either way.
                talk_obj["first_author"] = speaker
                talk_obj["last_author"] = speaker
            if authors:
                talk_obj["authors"] = authors
            if institutions:
                talk_obj["institutions"] = institutions
            if t_abstract:
                talk_obj["abstract"] = t_abstract
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
    if invited_filled_count:
        log(f"[ok]   filled affiliations on {invited_filled_count} talk(s) "
            f"from cached invited-speakers page:")
        for sp in invited_filled_speakers:
            log(f"          - {sp}")
    log("=" * 72)
    log("DONE.")
    log("=" * 72)


if __name__ == "__main__":
    main()
