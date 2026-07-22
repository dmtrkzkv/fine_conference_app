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

"""fetch_program_ecoc2026.py — DOWNLOAD ONLY.

The "downloader" half of the conference pipeline. Nothing here parses program
content; it only pulls source files onto disk under data/.

The conference planner backs its programme page with a small read-only JSON
endpoint, which is what this script talks to. The public programme page is an
empty shell whose tables are filled in by client-side requests to

    <BASE>/site/api/programme/json_call.asp?dataType=<kind>&...

so plain HTTP requests to that endpoint return the same JSON the page itself
renders. No login, no bot wall, no browser automation is needed.

Four `dataType` kinds are harvested, in dependency order:

    categories        the sub-committee / track registry
    categories_days   every session: day, time, room, track, session id
    speakers_detail   per session: chairs, organizers and speakers, each with
                      a role, a presentation title and an abstract id
    abstract_text     per abstract: summary text and the full author list

The last two are per-id, so they are fetched concurrently and merged into one
file each, keyed by id. That keeps the processor's input to a handful of files
and makes re-runs cheap.

Alongside the API data, seven programme sub-pages are saved as HTML. These are
the only source for things the endpoint leaves empty — session descriptions,
speaker biographies, and the affiliations of speakers whose planner record
carries none. All seven are optional: the processor degrades gracefully when
any is missing.

Outputs (all under data/):

    programme_page.html        programme shell; source of the conference name
    programme_categories.json  raw `categories` response
    programme_sessions.json    raw `categories_days` response
    session_speakers.json      {session id: [speaker rows]}
    abstracts.json             {abstract id: abstract record}
    page_*.html                the seven programme sub-pages
"""

import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"

BASE = "https://ecoc2026.org"
EVENT_KEY = "ecoc2026"
API = f"{BASE}/site/api/programme/json_call.asp"
PROGRAMME_PAGE = f"{BASE}/site/programme/?a={EVENT_KEY}"

# Programme sub-pages, saved as data/page_<name>.html. Each carries prose the
# JSON endpoint has no field for.
SUB_PAGES = {
    "workshops": f"{BASE}/{EVENT_KEY}/programme/workshops",
    "plenary_speakers": f"{BASE}/{EVENT_KEY}/programme/plenary-speakers",
    "tutorial_speakers": f"{BASE}/{EVENT_KEY}/programme/tutorial-speakers",
    "invited_speakers": f"{BASE}/{EVENT_KEY}/programme/invited-speakers",
    "special_events": f"{BASE}/{EVENT_KEY}/programme/special-events",
    "special_symposia": f"{BASE}/{EVENT_KEY}/programme/specialsymposia",
    "demo_zone": f"{BASE}/{EVENT_KEY}/programme/ecoc-demo-zone",
}

# The endpoint is unauthenticated but does reject requests with no User-Agent.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "*/*",
}

WORKERS = 8
RETRIES = 3
TIMEOUT = 60


def _get(url: str) -> bytes:
    """GET a URL, retrying a few times on transient failures."""
    last = None
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"failed after {RETRIES} attempts: {url} ({last})")


def _get_json(url: str):
    """GET a URL and decode it as JSON, tolerating a UTF-8 BOM."""
    return json.loads(_get(url).decode("utf-8-sig", errors="replace"))


def _api(data_type: str, **params) -> list:
    """Call the endpoint and return its `data` list."""
    query = "".join(f"&{k}={v}" for k, v in params.items())
    url = f"{API}?dataType={data_type}&a={EVENT_KEY}{query}"
    payload = _get_json(url)
    return payload.get("data", []) if isinstance(payload, dict) else []


def _write(name: str, content) -> None:
    path = DATA_DIR / name
    if isinstance(content, (bytes, bytearray)):
        path.write_bytes(content)
    else:
        path.write_text(
            json.dumps(content, ensure_ascii=False, indent=1), encoding="utf-8")
    size = path.stat().st_size
    print(f"[fetch]   wrote {name} ({size:,} bytes)", flush=True)


def _fetch_speakers(session_id) -> tuple:
    """All speaker rows for one session, following the endpoint's paging."""
    rows, page, pages = [], 1, 1
    while page <= pages:
        batch = _api("speakers_detail", sessionid=session_id, curPage=page)
        rows.extend(batch)
        if batch:
            # Every row echoes the page count for its session.
            pages = max(int(r.get("maxPage") or 1) for r in batch)
        page += 1
    return str(session_id), rows


def _fetch_abstract(abstract_id) -> tuple:
    """One abstract record, or None when the endpoint returns nothing."""
    rows = _api("abstract_text", abstractid=abstract_id)
    return str(abstract_id), (rows[0] if rows else None)


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[fetch] downloading into {DATA_DIR}", flush=True)

    # 1. The programme shell. Only its <title> is of interest downstream, but
    #    it is cheap and keeps the conference name out of tracked source.
    _write("programme_page.html", _get(PROGRAMME_PAGE))

    # 2. Track registry and the full session list.
    categories = _api("categories", trackid=0, catId=0, roomid=0)
    _write("programme_categories.json", {"data": categories})

    sessions = _api("categories_days", trackid=0, catId=0, roomid=0)
    if not sessions:
        sys.exit("[fetch] ERROR: the session list came back empty; the "
                 "programme may not be published yet, or the endpoint's "
                 "parameters have changed.")
    _write("programme_sessions.json", {"data": sessions})
    print(f"[fetch] {len(sessions)} sessions listed", flush=True)

    # 3. Per-session people. One request per session, run concurrently.
    session_ids = [s["sessionid"] for s in sessions if s.get("sessionid")]
    with ThreadPoolExecutor(WORKERS) as pool:
        speakers = dict(pool.map(_fetch_speakers, session_ids))
    n_rows = sum(len(v) for v in speakers.values())
    _write("session_speakers.json", speakers)
    print(f"[fetch] {n_rows} speaker/chair rows across "
          f"{len(speakers)} sessions", flush=True)

    # 4. Per-abstract text. Only rows that actually reference an abstract.
    abstract_ids = sorted({
        row["abstractid"]
        for rows in speakers.values()
        for row in rows
        if row.get("abstractid")
    })
    with ThreadPoolExecutor(WORKERS) as pool:
        abstracts = dict(pool.map(_fetch_abstract, abstract_ids))
    abstracts = {k: v for k, v in abstracts.items() if v}
    _write("abstracts.json", abstracts)
    print(f"[fetch] {len(abstracts)} abstracts", flush=True)

    # 5. Optional prose pages. A failure here is not fatal — the processor
    #    treats every one of them as optional enrichment.
    for name, url in SUB_PAGES.items():
        try:
            _write(f"page_{name}.html", _get(url))
        except RuntimeError as exc:
            print(f"[fetch]   WARNING: skipping page_{name}.html ({exc})",
                  flush=True)

    print("[fetch] done.", flush=True)


if __name__ == "__main__":
    main()
