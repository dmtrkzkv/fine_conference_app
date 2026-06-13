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

The "downloader" half of the ECIO 2026 pipeline. ECIO 2026 is now hosted on
Optica's conference platform, which exposes a clean JSON endpoint backing the
on-line schedule at

    https://www.optica.org/events/topical_meetings/european_conference_on_integrated_optics_(ecio)/schedule/

The endpoint itself is

    https://www.optica.org/api/presentations/?EventAcronym=ECIO&PageSize=500

It returns every session and every talk in one JSON document (sessions are
records with parentId == 0; talks reference their session via parentId). The
talk description carries the abstract followed by a `<strong>Authors</strong>:`
block with each author and their affiliation, so this one file contains
everything the processor needs.

Bot protection note: Optica's site sits behind Radware bot detection. A
request with the default urllib UA is redirected to a validation page and
returns no data. We therefore first GET the public schedule HTML page (which
sets the Radware cookies), then send the API request with those cookies plus
a real browser User-Agent and Referer. That is enough to pass the bot check.

Output:
    data/ECIO2026_program.json   the full presentations JSON document
"""

from __future__ import annotations

import http.cookiejar
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"

SCHEDULE_URL = (
    "https://www.optica.org/events/topical_meetings/"
    "european_conference_on_integrated_optics_(ecio)/schedule/"
)
API_URL = (
    "https://www.optica.org/api/presentations/"
    "?EventAcronym=ECIO&PageSize=500"
)
OUTPUT_JSON = DATA_DIR / "ECIO2026_program.json"

# Optica's bot-protection layer rejects the default urllib UA. Pose as a real
# browser; Referer must be set to the schedule page or the API still bounces.
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _opener_with_cookies() -> urllib.request.OpenerDirector:
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def main() -> None:
    print("=" * 72)
    print("[config] ECIO 2026 DOWNLOADER starting up.")
    print(f"[config]   script dir   : {SCRIPT_DIR}")
    print(f"[config]   data dir     : {DATA_DIR}")
    print(f"[config]   schedule URL : {SCHEDULE_URL}")
    print(f"[config]   API URL      : {API_URL}")
    print("=" * 72)

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    opener = _opener_with_cookies()

    print("[info] priming bot-protection cookies via schedule page …")
    try:
        req = urllib.request.Request(SCHEDULE_URL, headers={
            "User-Agent": BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        })
        with opener.open(req, timeout=60) as resp:
            _ = resp.read()
    except urllib.error.URLError as e:
        print(f"[fatal] could not load schedule page: {e}")
        sys.exit(1)
    print("[info]   cookies acquired.")

    print("[info] fetching presentations JSON …")
    try:
        req = urllib.request.Request(API_URL, headers={
            "User-Agent": BROWSER_UA,
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": SCHEDULE_URL,
        })
        with opener.open(req, timeout=120) as resp:
            body = resp.read()
    except urllib.error.URLError as e:
        print(f"[fatal] API request failed: {e}")
        sys.exit(1)

    try:
        doc = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"[fatal] API did not return valid JSON: {e}")
        sys.exit(1)

    total = doc.get("recordCount")
    pres = doc.get("presentations", [])
    if not isinstance(pres, list) or not pres:
        print("[fatal] API JSON has no presentations — Radware may have "
              "intercepted the request again. Try again later.")
        sys.exit(1)
    if total is not None and len(pres) < total:
        print(f"[warn] returned {len(pres)} of {total} records — PageSize "
              "may be too small.")

    OUTPUT_JSON.write_text(json.dumps(doc, indent=2, ensure_ascii=False),
                           encoding="utf-8")
    size_kb = OUTPUT_JSON.stat().st_size / 1024
    parents = sum(1 for p in pres if p.get("parentId") == 0)
    children = sum(1 for p in pres if p.get("parentId"))
    print(f"[ok]   saved {OUTPUT_JSON.name} ({size_kb:,.1f} KB): "
          f"{parents} sessions, {children} talks.")

    print()
    print("=" * 72)
    print("DONE (downloaded program JSON). "
          "Next: run process_program_ecio2026.py")
    print(f"  data dir : {DATA_DIR}")
    print("=" * 72)


if __name__ == "__main__":
    main()
