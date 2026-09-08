"""Structural checks on the Jekyll site.

These do not build the site -- that needs Ruby -- but they catch the mistakes
that restructuring actually causes: front matter that no longer parses, nav
links to pages that were deleted, collections referenced after being removed,
and generated publications whose category is not one the theme knows how to
render.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Collections that no longer exist; nothing may reference them.
REMOVED_COLLECTIONS = ("talks", "teaching", "portfolio", "posts")

FRONT_MATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def front_matter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    match = FRONT_MATTER_RE.match(text)
    assert match, f"{path.name} has no YAML front matter"
    return yaml.safe_load(match.group(1)) or {}


@pytest.fixture(scope="module")
def config() -> dict:
    return yaml.safe_load((REPO_ROOT / "_config.yml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def navigation() -> dict:
    return yaml.safe_load((REPO_ROOT / "_data" / "navigation.yml").read_text(encoding="utf-8"))


def page_paths() -> list[Path]:
    return sorted((REPO_ROOT / "_pages").glob("*.*"))


def publication_paths() -> list[Path]:
    return sorted((REPO_ROOT / "_publications").glob("*.md"))


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def test_config_parses(config):
    assert config["title"] == "Sebastian Pohl"
    assert config["url"] == "https://oneSebastian.github.io"


def test_config_declares_only_the_collections_that_still_exist(config):
    assert set(config["collections"]) == {"publications", "writing"}
    for name in ("_publications", "_writing"):
        assert (REPO_ROOT / name).is_dir(), f"{name}/ is declared but missing"


def test_defaults_do_not_reference_removed_collections(config):
    types = {entry["scope"].get("type") for entry in config["defaults"]}
    assert not types & set(REMOVED_COLLECTIONS)


def test_scholar_profile_is_configured(config):
    assert "scholar.google.com" in config["author"]["googlescholar"]


def test_the_avatar_file_exists(config):
    """A typo here renders as a broken image on every page, with no build error."""
    avatar = config["author"]["avatar"]

    if "://" in avatar:  # remotely hosted; nothing local to check
        return
    assert (REPO_ROOT / "images" / avatar).is_file(), f"images/{avatar} is missing"


def test_publication_categories_cover_what_the_sync_can_emit(config):
    import scholar_sync as ss

    assert set(config["publication_category"]) == set(ss.VALID_CATEGORIES)


# --------------------------------------------------------------------------
# Navigation
# --------------------------------------------------------------------------


def test_navigation_has_exactly_the_intended_tabs(navigation):
    assert [item["title"] for item in navigation["main"]] == ["Publications", "Writing", "CV"]


def test_every_nav_link_resolves(navigation):
    """A nav link is either a page permalink or a file that ships with the site."""
    permalinks = {front_matter(path).get("permalink") for path in page_paths()}

    for item in navigation["main"]:
        url = item["url"]
        if url in permalinks:
            continue
        target = REPO_ROOT / url.lstrip("/")
        assert target.is_file(), f"nav item {item['title']!r} points at neither a page nor a file"


def test_the_cv_tab_points_at_the_cv_pdf(navigation):
    cv = next(item for item in navigation["main"] if item["title"] == "CV")

    assert cv["url"].endswith(".pdf")
    assert (REPO_ROOT / cv["url"].lstrip("/")).is_file()


def test_the_old_cv_url_still_redirects_to_the_same_pdf(navigation):
    """/cv/ and the nav tab must not drift apart onto different files."""
    cv_page = front_matter(REPO_ROOT / "_pages" / "cv.md")
    nav_url = next(item["url"] for item in navigation["main"] if item["title"] == "CV")

    assert cv_page["permalink"] == "/cv/"
    assert cv_page["redirect_to"] == nav_url


def test_redirect_from_plugin_is_enabled(config):
    """_pages/cv.md's redirect_to does nothing without it."""
    assert "jekyll-redirect-from" in config["plugins"]
    assert "jekyll-redirect-from" in config["whitelist"]


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------


def test_every_page_has_parseable_front_matter():
    for path in page_paths():
        front_matter(path)


def test_no_page_iterates_a_removed_collection():
    for path in page_paths():
        text = path.read_text(encoding="utf-8")
        for collection in REMOVED_COLLECTIONS:
            assert f"site.{collection}" not in text, f"{path.name} still reads site.{collection}"


def writing_paths() -> list[Path]:
    return sorted((REPO_ROOT / "_writing").glob("*.md"))


def test_the_writing_page_exists():
    assert front_matter(REPO_ROOT / "_pages" / "writing.md")["permalink"] == "/writing/"


def test_the_writing_page_still_handles_being_empty():
    """The placeholder branch has to survive edits made once entries exist."""
    page = (REPO_ROOT / "_pages" / "writing.md").read_text(encoding="utf-8")

    assert "{% else %}" in page and "Nothing here yet" in page


def test_every_writing_entry_has_the_fields_the_page_reads():
    assert writing_paths(), "no writing entries found"

    for path in writing_paths():
        meta = front_matter(path)
        assert meta["collection"] == "writing", path.name
        assert meta["title"], path.name
        assert meta["date"], path.name


def test_every_writing_pdf_exists():
    """A broken link here is invisible until someone clicks it."""
    for path in writing_paths():
        pdfurl = front_matter(path).get("pdfurl")
        if not pdfurl:
            continue
        assert (REPO_ROOT / pdfurl.lstrip("/")).is_file(), f"{path.name} links a missing {pdfurl}"


def test_writing_entries_do_not_link_a_pdf_only_in_prose():
    """If the body links a PDF, pdfurl must too, so the index shows the link."""
    for path in writing_paths():
        text = path.read_text(encoding="utf-8")
        body = text.split("---\n", 2)[2]
        if "/files/" in body:
            assert front_matter(path).get("pdfurl"), (
                f"{path.name} links a PDF in its body but sets no pdfurl, "
                "so the Writing index will not offer it"
            )


def test_removed_pages_are_gone():
    for name in ("talks.html", "teaching.html", "portfolio.html", "year-archive.html",
                 "talkmap.html", "category-archive.html", "tag-archive.html"):
        assert not (REPO_ROOT / "_pages" / name).exists(), f"_pages/{name} should have been removed"


# --------------------------------------------------------------------------
# Generated publications
# --------------------------------------------------------------------------


def test_publications_exist():
    assert publication_paths(), "no publications were generated"


def test_every_publication_has_the_fields_the_templates_read(config):
    categories = set(config["publication_category"])

    for path in publication_paths():
        meta = front_matter(path)
        assert meta["collection"] == "publications", path.name
        assert meta["category"] in categories, f"{path.name} has category {meta['category']!r}"
        assert meta["title"], path.name
        assert meta["venue"], path.name
        assert meta["citation"], path.name


def test_publication_filenames_match_their_dates():
    for path in publication_paths():
        date = str(front_matter(path)["date"])
        assert path.stem.startswith(date), f"{path.name} does not start with its date {date}"


def test_the_scholar_cache_matches_the_generated_files():
    import json

    cache = json.loads((REPO_ROOT / "_data" / "scholar.json").read_text(encoding="utf-8"))
    assert cache["publication_count"] == len(publication_paths())
    assert cache["updated_at"]


def test_the_publications_page_reads_the_field_the_cache_actually_writes():
    """A rename in scholar_sync.py must not silently blank the page's date line."""
    import json

    page = (REPO_ROOT / "_pages" / "publications.html").read_text(encoding="utf-8")
    cache = json.loads((REPO_ROOT / "_data" / "scholar.json").read_text(encoding="utf-8"))

    assert "site.data.scholar.updated_at" in page
    assert "updated_at" in cache
