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

"""fetch_program_ecio2026.py — DOWNLOAD ONLY.

The "downloader" half of the ECIO 2026 pipeline. The full ECIO 2026 program is
published as two PDFs linked from the public programme page,

    https://www.ecio-conference.org/programme-26/

There is no planner / Excel / abstract-book export. The two PDFs are:

    ECIO26_DetailedSchedule.pdf   the wide A3 grid: every session, every time
                                  slot, every talk title + speaker.
    ECIO26_Concise.pdf            one-page program overview (session blocks
                                  only, no per-talk detail).

This script fetches both into data/. The filenames on the website carry the
re-issue date in their suffix (e.g. ECIO26_DetailedProgramSchedule_21_5.pdf),
which changes whenever the organisers publish a refresh, so we do NOT hard-code
those URLs. Instead we scrape the programme page itself and pick the most
recent matching PDF link by the date encoded in its filename. This way the
fetcher keeps working when ECIO publishes an updated version of either PDF.

Contacts the network only; launches no browser. The processor
(process_program_ecio2026.py) runs entirely offline against what we save here.
"""

from __future__ import annotations

import re
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"

PROGRAMME_URL = "https://www.ecio-conference.org/programme-26/"

# The two artifacts we save into data/. Each picks the most-recent matching link
# on the programme page by the trailing date in the filename.
ARTIFACTS = [
    {
        "name": "ECIO26_DetailedSchedule.pdf",
        # Detailed schedule: ECIO26_DetailedProgramSchedule_<dd>_<m>.pdf
        "pattern": re.compile(
            r"https?://[^\"'>\s]+/ECIO26_DetailedProgramSchedule[^\"'>\s]*\.pdf",
            re.IGNORECASE,
        ),
        "desc": "detailed program schedule",
    },
    {
        "name": "ECIO26_Concise.pdf",
        # Concise overview: ECIO_FinalProgram_Concise_<dd>_<mm>.pdf
        "pattern": re.compile(
            r"https?://[^\"'>\s]+/ECIO_FinalProgram_Concise[^\"'>\s]*\.pdf",
            re.IGNORECASE,
        ),
        "desc": "concise program overview",
    },
]

# Filename-date pattern: ..._<d>_<m>.pdf or ..._<dd>_<mm>.pdf at the very end of
# the basename (used to pick the most recent re-issue when multiple candidates
# show up). Year is assumed 2026.
DATE_RE = re.compile(r"_(\d{1,2})_(\d{1,2})\.pdf$", re.IGNORECASE)

# Polite UA — some WP installs 403 the default urllib UA.
UA = "Mozilla/5.0 (ecio2026-fetch; fine-conference-app)"


def _fetch_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="replace")


def _fetch_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def _date_key(url: str) -> tuple[int, int, str]:
    """Sort key for picking the freshest re-issue of a PDF: extract (month, day)
    from the trailing _<d>_<m>.pdf suffix; fall back to the URL itself so the
    sort is total even when no date can be parsed."""
    m = DATE_RE.search(url)
    if not m:
        return (0, 0, url)
    day, month = int(m.group(1)), int(m.group(2))
    return (month, day, url)


def _pick_latest(html: str, pat: re.Pattern[str]) -> str | None:
    candidates = sorted(set(pat.findall(html)), key=_date_key, reverse=True)
    return candidates[0] if candidates else None


def main() -> None:
    print("=" * 72)
    print("[config] ECIO 2026 DOWNLOADER starting up.")
    print(f"[config]   script dir   : {SCRIPT_DIR}")
    print(f"[config]   data dir     : {DATA_DIR}")
    print(f"[config]   programme URL: {PROGRAMME_URL}")
    print(f"[config]   run date     : {date.today().isoformat()}")
    print("=" * 72)

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[info] fetching programme page to discover PDF links …")
    try:
        html = _fetch_text(PROGRAMME_URL)
    except urllib.error.URLError as e:
        print(f"[fatal] could not fetch {PROGRAMME_URL}: {e}")
        sys.exit(1)
    print(f"[info]   fetched {len(html):,} chars of HTML.")

    saved_any = False
    failed: list[str] = []
    for art in ARTIFACTS:
        target = DATA_DIR / art["name"]
        url = _pick_latest(html, art["pattern"])
        if not url:
            print(f"[warn] no link matching {art['desc']} found on the "
                  f"programme page; cannot fetch {art['name']}.")
            failed.append(art["name"])
            continue
        print(f"[info] downloading {art['desc']} from {url}")
        try:
            body = _fetch_bytes(url)
        except urllib.error.URLError as e:
            print(f"[warn]   download failed: {e}")
            failed.append(art["name"])
            continue
        target.write_bytes(body)
        size_kb = target.stat().st_size / 1024
        print(f"[ok]   saved {target.name} ({size_kb:,.1f} KB).")
        saved_any = True

    print()
    print("=" * 72)
    if failed:
        print(f"DONE WITH WARNINGS — {len(failed)} file(s) not retrieved:")
        for n in failed:
            print(f"  - {n}")
        print("Re-check the programme page or see data_requirements_ecio2026.txt "
              "for the manual fallback.")
    else:
        print("DONE (downloaded program PDFs). Next: run process_program_ecio2026.py")
    print(f"  data dir : {DATA_DIR}")
    print("=" * 72)
    if not saved_any:
        sys.exit(1)


if __name__ == "__main__":
    main()
