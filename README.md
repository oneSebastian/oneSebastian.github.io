# oneSebastian.github.io

Sebastian Pohl's personal site, served by GitHub Pages at
<https://oneSebastian.github.io>. It is a Jekyll site built on the
[Academic Pages](https://github.com/academicpages/academicpages.github.io)
template, itself a fork of
[Minimal Mistakes](https://mmistakes.github.io/minimal-mistakes/).

The site has three tabs: **Publications**, **Writing** and **CV**.

## Publications sync

`_publications/` is generated, not hand-written. `scripts/scholar_sync.py`
reads the Google Scholar profile named by `author.googlescholar` in
`_config.yml` and writes one Markdown file per publication, plus a
`_data/scholar.json` cache that the Publications page uses for its
"last synced" line.

`.github/workflows/update-publications.yml` runs the script every Monday and
commits whatever changed, which is what makes GitHub Pages rebuild the site. It
can also be run by hand from the Actions tab.

To run it locally:

```bash
python scripts/scholar_sync.py            # fetch and rewrite _publications/
python scripts/scholar_sync.py --check    # report drift, write nothing (exit 1 if stale)
```

No third-party packages are needed unless you use the overrides file below.

### When Scholar blocks the runner

Google Scholar has no API, so the script scrapes the profile pages, and Scholar
rate-limits datacenter IPs — GitHub's runners included. When it serves a CAPTCHA
instead of the profile the script writes nothing and exits 3; the workflow turns
that into a warning rather than a failure, and the committed publications stay
exactly as they were. **The site never degrades because a fetch failed.**

If the scheduled run is blocked for weeks on end, run the script from your own
machine and commit the result — a residential IP is not rate-limited the same
way. That is the intended fallback; nothing else needs to change.

### Correcting a publication

Files in `_publications/` are overwritten on every sync, so edits there are
lost. Corrections go in `_data/publication_overrides.yml`, keyed by the slug in
the generated filename. Copy `_data/publication_overrides.yml.example` to that
name to get started — it lists every field you can override, including `skip`
to hide a record and `category` to move one between the headings defined under
`publication_category` in `_config.yml`.

The overrides file is the only part that needs a dependency:
`pip install -r scripts/requirements.txt`.

## Writing

`_writing/` holds non-academic pieces, one Markdown file per piece. The
**Writing** page shows a placeholder until the first file lands. Add one like:

```markdown
---
title: "A title"
collection: writing
date: 2026-09-08
excerpt: 'One line shown on the Writing index.'
---

The piece itself.
```

The filename becomes the URL, so `_writing/on-something.md` is served at
`/writing/on-something/`.

## Local preview

Requires Docker:

```bash
docker compose up
```

then open <http://localhost:4000>.

## Tests

```bash
pip install -r scripts/requirements.txt pytest
python -m pytest tests -q
```

`tests/test_scholar_sync.py` covers the Scholar parsing and file generation
against saved pages in `tests/data/`; `tests/test_site_structure.py` checks the
site's own wiring — front matter parses, nav links resolve, no page reads a
collection that no longer exists. Neither touches the network. The same suite
runs in CI via `.github/workflows/tests.yml`.
