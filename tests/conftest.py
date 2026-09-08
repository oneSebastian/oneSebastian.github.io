"""Shared fixtures for the scholar_sync tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(__file__).resolve().parent / "data"

# scripts/ is not an installable package, so put it on the path directly.
sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.fixture
def data_dir() -> Path:
    return DATA_DIR


@pytest.fixture
def read_fixture():
    """Read a saved Scholar page out of tests/data/."""

    def _read(name: str) -> str:
        return (DATA_DIR / name).read_text(encoding="utf-8")

    return _read


@pytest.fixture
def fake_scholar(read_fixture):
    """Serve the saved Scholar pages in place of the network.

    Returns a (fetcher, calls) pair so tests can also assert on which URLs were
    requested -- pagination behaviour is otherwise invisible.
    """

    article_pages = {
        "TESTUSER:u5HHmVD_uO8C": "article_conference.html",
        "TESTUSER:2osOgNQ5qMEC": "article_workshop.html",
        "TESTUSER:9yKSN-GCB0IC": "article_preprint.html",
    }

    calls: list[str] = []

    def _fetch(url: str) -> str:
        calls.append(url)
        if "view_op=view_citation" in url:
            for citation_id, page in article_pages.items():
                # Citation ids are percent-encoded in the request URL.
                if citation_id.replace(":", "%3A") in url or citation_id in url:
                    return read_fixture(page)
            raise AssertionError(f"No article fixture matches {url}")
        # Only the first page of the profile has rows; anything beyond it is
        # the empty tail Scholar returns past the end of the list.
        if "cstart=0" in url:
            return read_fixture("profile_page.html")
        return read_fixture("profile_page_empty.html")

    return _fetch, calls
