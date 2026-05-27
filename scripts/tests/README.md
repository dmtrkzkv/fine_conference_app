# Affiliation-map regression tests

These tests protect already-built conference apps from silent changes to their
canonical affiliation maps when anchors or the fallback shortener get added or
tweaked for a new conference.

## How it works

`build_affiliation_map.build(sources)` is a pure, deterministic function from a
conference's `affiliation_sources` block to a `{raw_string -> short_label}`
dict. For every conference already built, that dict is frozen to disk
(`tests/golden/<slug>.expected.txt`) and each run asserts the output reproduces
it byte-for-byte. Any unintended change to an old conference fails the test with
a readable, line-level diff (e.g. `'Würzburg' -> 'GENERIC_UNI'`).

## Layout

```
build_affiliation_map.py          # the module under test (imported by path)
tests/
  conftest.py                     # fixture discovery + helpers
  test_affiliation_map.py         # the tests
  make_fixture.py                 # extract a fixture from a full conference JSON
  fixtures/<slug>.affiliation_sources.json   # trimmed input (KBs, not MBs)
  golden/<slug>.expected.txt      # frozen expected output
```

Fixtures hold only the `affiliation_sources` block (all that `build()` reads),
so they stay small — the full multi-MB `conference_data.json` is not needed.

## Running

```
pytest tests/test_affiliation_map.py
```

This must be green before shipping a build.

## Adding a new conference

1. Create its fixture from the processor's full JSON:
   ```
   python tests/make_fixture.py path/to/conference_data_<slug>.json
   ```
2. Freeze its golden snapshot (first time writes the file directly):
   ```
   pytest -k <slug> --update-golden
   ```
3. Glance over `tests/golden/<slug>.expected.txt` to confirm the labels look
   right. That conference is now protected.

## Intentionally changing an existing conference's map

A change that is wanted — say a new anchor that correctly shortens a string that
used to fall through — will also fail the snapshot. Run the update across all
conferences at once (omit `-k` to update every out-of-date golden in one pass):

```
pytest --update-golden
```

This never overwrites an existing golden automatically. For each conference whose
map would change it writes the proposed output to
`tests/golden/<slug>.expected.new` and prints a unified diff, leaving
`<slug>.expected.txt` untouched. Conferences that are already current are skipped.
After the diffs look correct, promote by hand — for several at once:

```
cd tests/golden
for f in *.expected.new; do mv "$f" "${f%.new}.txt"; done
```

(PowerShell: `Get-ChildItem tests/golden/*.expected.new | Rename-Item -NewName { $_.Name -replace '\.new$' }`)

### Skipping the review step

Once the diffs have already been reviewed (or the change is trivially correct),
`--promote-golden` overwrites the `.expected.txt` files directly — no `.new`
files, no diff:

```
pytest --promote-golden
```

This bypasses the safeguard, so use it only when the changes are already known to
be correct. Plain `--update-golden` remains the default for anything you haven't
eyeballed yet, since it keeps the original golden intact until you promote.

