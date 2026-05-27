"""Make a trimmed test fixture from a full conference_data.json.

build_affiliation_map.build() reads ONLY the 'affiliation_sources' block, so a
fixture needs nothing else. This extracts just that block, keeping fixtures tiny
(KBs, not MBs) and the test surface honest.

Usage:
    python tests/make_fixture.py path/to/conference_data_cleo2025.json
    python tests/make_fixture.py path/to/conference_data_cleo2025.json --slug cleo2025

Then freeze its golden snapshot:
    pytest -k <slug> --update-golden
This creates tests/fixtures/<slug>.affiliation_sources.json (here) and
tests/golden/<slug>.expected.txt (on the first --update-golden run).
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

_KEYS = (
    "affiliation_full_lines",
    "presider_affiliation_strings",
    "institution_strings",
)


def slug_from_filename(path: Path) -> str:
    stem = path.stem
    m = re.match(r"conference_data_(.+)", stem)
    return (m.group(1) if m else stem)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("data_json", type=Path,
                    help="A full conference_data.json from the processor.")
    ap.add_argument("--slug", default=None,
                    help="Conference id used for fixture/golden filenames. "
                         "Defaults to the part after 'conference_data_'.")
    args = ap.parse_args()

    data = json.loads(args.data_json.read_text(encoding="utf-8"))
    src = data.get("affiliation_sources")
    if not isinstance(src, dict):
        src = data  # builder accepts the bare block too
    trimmed = {k: (src.get(k) or []) for k in _KEYS}

    slug = args.slug or slug_from_filename(args.data_json)
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    out = FIXTURES_DIR / f"{slug}.affiliation_sources.json"
    out.write_text(json.dumps(trimmed, ensure_ascii=False, indent=1,
                              sort_keys=True) + "\n", encoding="utf-8")
    counts = ", ".join(f"{k}={len(trimmed[k])}" for k in _KEYS)
    print(f"wrote {out}  ({counts})")
    print(f"now run:  pytest -k {slug} --update-golden")


if __name__ == "__main__":
    main()
