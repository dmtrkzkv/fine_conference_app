"""Regression tests for build_affiliation_map.build().

PRIMARY GUARANTEE (test_golden_map_unchanged): for every conference that already
has a frozen snapshot in tests/golden/, build() must reproduce that exact map.
This is what stops a new conference's anchor edits from silently changing an old
conference's affiliation chips.

The other tests are cheap structural guards that catch whole-pipeline breakage
(an exception, an empty map, a non-string value) before you even look at a diff.
"""

from __future__ import annotations

import contextlib
import difflib
import io

import pytest

from conftest import (
    GOLDEN_DIR,
    discover_fixtures,
    load_affiliation_sources,
    render_map,
)

FIXTURES = discover_fixtures()


def _build_quiet(builder, sources: dict) -> dict[str, str]:
    """Run build() with its chatty stdout suppressed and no .txt side-effect."""
    with contextlib.redirect_stdout(io.StringIO()):
        return builder.build(sources, out_txt=None)


# A repo with no fixtures yet shouldn't look "green" — that would hide the fact
# that nothing is actually being protected.
def test_fixtures_exist():
    assert FIXTURES, (
        "No conference fixtures found in tests/fixtures/. Add at least one "
        "<slug>.affiliation_sources.json (see tests/make_fixture.py)."
    )


@pytest.mark.parametrize("slug", FIXTURES)
def test_golden_map_unchanged(builder, slug, update_golden, promote_golden):
    """Byte-for-byte: this conference's map today == its frozen snapshot."""
    sources = load_affiliation_sources(slug)
    actual = render_map(_build_quiet(builder, sources))

    golden_path = GOLDEN_DIR / f"{slug}.expected.txt"

    if update_golden:
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        if not golden_path.exists():
            # First time for this conference: nothing to clobber, write it.
            golden_path.write_text(actual, encoding="utf-8")
            pytest.skip(f"created golden for {slug}")
        expected = golden_path.read_text(encoding="utf-8")
        if actual == expected:
            pytest.skip(f"golden for {slug} already current")
        if promote_golden:
            # --promote-golden: caller has already reviewed and wants the change
            # applied. Overwrite the golden directly, no .new file, no diff.
            golden_path.write_text(actual, encoding="utf-8")
            pytest.skip(f"promoted golden for {slug}")
        # Plain --update-golden: the golden exists AND would change. Never
        # overwrite it automatically — that would defeat the safeguard. Write the
        # new output to a sibling .new file and print the diff. Promote by hand
        # (replace .expected.txt with .expected.new), or rerun with
        # --promote-golden to overwrite directly.
        new_path = golden_path.with_suffix(".new")
        new_path.write_text(actual, encoding="utf-8")
        diff = "".join(difflib.unified_diff(
            expected.splitlines(keepends=True),
            actual.splitlines(keepends=True),
            fromfile=f"{golden_path.name} (current)",
            tofile=f"{new_path.name} (proposed)",
        ))
        pytest.skip(
            f"golden for {slug} WOULD CHANGE — not overwritten.\n"
            f"Proposed output written to {new_path.name}. Diff:\n{diff}\n"
            f"If correct, replace {golden_path.name} with {new_path.name} "
            f"(or rerun with --promote-golden to overwrite directly)."
        )

    assert golden_path.exists(), (
        f"Missing golden file {golden_path.name}. Generate it once with:\n"
        f"    pytest -k {slug} --update-golden\n"
        f"then review it before relying on it."
    )

    expected = golden_path.read_text(encoding="utf-8")
    if actual != expected:
        # Build a compact, line-level diff naming exactly which affiliations
        # changed short label (or appeared/disappeared) — so a failure tells you
        # the damage at a glance instead of dumping 4000 lines.
        _assert_with_diff(slug, expected, actual)


def _assert_with_diff(slug: str, expected: str, actual: str):
    exp = dict(_parse(expected))
    act = dict(_parse(actual))

    changed = sorted(k for k in exp.keys() & act.keys() if exp[k] != act[k])
    removed = sorted(exp.keys() - act.keys())
    added = sorted(act.keys() - exp.keys())

    msgs = [f"Affiliation map for '{slug}' changed vs golden snapshot:"]
    for k in changed[:40]:
        msgs.append(f"  CHANGED short label: {k!r}: {exp[k]!r} -> {act[k]!r}")
    for k in removed[:20]:
        msgs.append(f"  REMOVED raw key: {k!r} (was -> {exp[k]!r})")
    for k in added[:20]:
        msgs.append(f"  ADDED raw key:   {k!r} (-> {act[k]!r})")
    extra = (max(0, len(changed) - 40) + max(0, len(removed) - 20)
             + max(0, len(added) - 20))
    if extra:
        msgs.append(f"  …and {extra} more difference(s).")
    msgs.append(
        "If these changes are intentional and correct, regenerate the golden\n"
        "with:\n"
        f"    pytest -k {slug} --update-golden\n"
        f"then review the printed diff and promote {slug}.expected.new to "
        f"{slug}.expected.txt."
    )
    pytest.fail("\n".join(msgs), pytrace=False)


def _parse(text: str):
    for line in text.splitlines():
        if not line.strip():
            continue
        raw, _, short = line.partition("\t")
        yield raw, short


# ---- cheap structural guards (catch total breakage, not subtle regressions) --

@pytest.mark.parametrize("slug", FIXTURES)
def test_build_runs_and_is_wellformed(builder, slug):
    sources = load_affiliation_sources(slug)
    mapping = _build_quiet(builder, sources)
    assert isinstance(mapping, dict) and mapping, "build() returned empty/non-dict"
    assert all(isinstance(k, str) and isinstance(v, str)
               for k, v in mapping.items()), "non-string key or value in map"
    assert all(v.strip() for v in mapping.values()), "empty short label produced"


@pytest.mark.parametrize("slug", FIXTURES)
def test_build_is_deterministic(builder, slug):
    sources = load_affiliation_sources(slug)
    assert _build_quiet(builder, sources) == _build_quiet(builder, sources)
