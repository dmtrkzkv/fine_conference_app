"""Shared fixtures/helpers for the affiliation-map regression tests.

These tests guarantee that adding or tweaking anchors / the fallback shortener
for a new conference never silently changes the canonical affiliation map of an
already-shipped conference. The mechanism is golden-master ("snapshot") testing:
each known conference's full build() output is frozen on disk under
tests/golden/, and every run is compared byte-for-byte against it.

To intentionally accept a change (the diff has been reviewed and is correct),
regenerate the snapshots with the --update-golden flag:

    pytest tests/test_affiliation_map.py --update-golden

For a conference that has no golden yet, this writes one directly. For a
conference whose golden already exists, it never overwrites the file: it writes
the proposed output to <slug>.expected.new and prints the diff. Promote it by
replacing <slug>.expected.txt with <slug>.expected.new only after the diff looks
right.

Once the diffs have been reviewed, --promote-golden overwrites the
<slug>.expected.txt files directly (no .new step, no diff) — handy when several
conferences changed at once. It bypasses the safeguard, so use it only for
changes already known to be correct.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

# tests/ lives next to build_affiliation_map.py. Adjust if your layout differs.
TESTS_DIR = Path(__file__).resolve().parent
REPO_DIR = TESTS_DIR.parent
GOLDEN_DIR = TESTS_DIR / "golden"
FIXTURES_DIR = TESTS_DIR / "fixtures"


def pytest_addoption(parser):
    """Add flags so snapshots can be regenerated identically on any platform
    (no shell-specific environment-variable syntax)."""
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="Regenerate golden snapshots. Writes a new golden directly when "
             "none exists; for an existing golden, writes <slug>.expected.new "
             "and prints the diff instead of overwriting.",
    )
    parser.addoption(
        "--promote-golden",
        action="store_true",
        default=False,
        help="Like --update-golden, but overwrites existing <slug>.expected.txt "
             "files directly (no .new step, no diff). Use only after you've "
             "already reviewed the changes — this skips the safeguard.",
    )


@pytest.fixture(scope="session")
def update_golden(request) -> bool:
    # --promote-golden implies --update-golden (both put the test in write mode);
    # the distinction is handled by the promote_golden fixture below.
    return bool(request.config.getoption("--update-golden")
                or request.config.getoption("--promote-golden"))


@pytest.fixture(scope="session")
def promote_golden(request) -> bool:
    return bool(request.config.getoption("--promote-golden"))


def _load_builder():
    """Import build_affiliation_map.py by path (it's a standalone script, not a
    package), the same way build_conference_app.py loads it at runtime."""
    path = REPO_DIR / "build_affiliation_map.py"
    spec = importlib.util.spec_from_file_location("build_affiliation_map", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="session")
def builder():
    return _load_builder()


def discover_fixtures() -> list[str]:
    """Every conference we have a fixture for == every conference we must not
    regress. Returns the conference 'slug' (filename stem) for each fixture."""
    if not FIXTURES_DIR.exists():
        return []
    return sorted(p.stem.replace(".affiliation_sources", "")
                  for p in FIXTURES_DIR.glob("*.affiliation_sources.json"))


def load_affiliation_sources(slug: str) -> dict:
    """Load the trimmed affiliation_sources block for one conference. build()
    accepts either the whole JSON or just this block, so the fixture only needs
    to carry this block — not the multi-MB full conference_data.json."""
    path = FIXTURES_DIR / f"{slug}.affiliation_sources.json"
    return json.loads(path.read_text(encoding="utf-8"))


def render_map(mapping: dict[str, str]) -> str:
    """Deterministic, diff-friendly text rendering of the map: one
    'raw<TAB>short' line per entry, sorted by raw key. Mirrors the module's own
    write_text() format (minus the auto-generated header, which carries counts
    that would create noise in diffs)."""
    lines = []
    for k in sorted(mapping):
        kk = k.replace("\t", " ").replace("\n", " ")
        vv = mapping[k].replace("\t", " ").replace("\n", " ")
        lines.append(f"{kk}\t{vv}")
    return "\n".join(lines) + "\n"
