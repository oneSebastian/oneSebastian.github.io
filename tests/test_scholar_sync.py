"""Tests for scripts/scholar_sync.py.

The network is never touched: every test either parses a saved page from
tests/data/ or injects the `fake_scholar` fetcher.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

import scholar_sync as ss


# --------------------------------------------------------------------------
# Profile page parsing
# --------------------------------------------------------------------------


def test_profile_parser_extracts_every_row(read_fixture):
    parser = ss.ProfileParser()
    parser.feed(read_fixture("profile_page.html"))

    assert [row["citation_id"] for row in parser.rows] == [
        "TESTUSER:u5HHmVD_uO8C",
        "TESTUSER:2osOgNQ5qMEC",
        "TESTUSER:9yKSN-GCB0IC",
    ]


def test_profile_parser_reads_authors_venue_and_counts(read_fixture):
    parser = ss.ProfileParser()
    parser.feed(read_fixture("profile_page.html"))
    first = parser.rows[0]

    assert first["authors"] == "S Pohl, N Elfaramawy, A Miling, K Cao, B Kehr, M Weidlich"
    assert first["cited_by"] == 9
    assert first["year"] == "2024"


def test_profile_parser_drops_the_year_span_from_the_venue(read_fixture):
    """The gs_oph span holds ", 2024" and must not be glued onto the venue."""
    parser = ss.ProfileParser()
    parser.feed(read_fixture("profile_page.html"))

    for row in parser.rows:
        assert not row["venue"].endswith(row["year"])
        assert ", 2024" not in row["venue"]
    assert parser.rows[2]["venue"] == "arXiv preprint arXiv:2605.07699"


def test_profile_parser_treats_an_empty_citation_link_as_zero(read_fixture):
    parser = ss.ProfileParser()
    parser.feed(read_fixture("profile_page.html"))

    assert parser.rows[1]["cited_by"] == 0


def test_profile_parser_returns_nothing_for_an_empty_profile(read_fixture):
    parser = ss.ProfileParser()
    parser.feed(read_fixture("profile_page_empty.html"))

    assert parser.rows == []


# --------------------------------------------------------------------------
# Article page parsing
# --------------------------------------------------------------------------


def test_article_parser_reads_the_labelled_fields(read_fixture):
    parser = ss.ArticleParser()
    parser.feed(read_fixture("article_conference.html"))

    assert parser.fields["Authors"].startswith("Sebastian Pohl, Nourhan Elfaramawy")
    assert parser.fields["Publication date"] == "2024/7/15"
    assert parser.fields["Conference"].startswith("Proceedings of the 36th")
    assert parser.article_url == "https://example.org/papers/snakemake.pdf"


def test_article_parser_excludes_the_pdf_badge_from_the_title(read_fixture):
    """The '[PDF] example.org' badge lives inside the title div."""
    parser = ss.ArticleParser()
    parser.feed(read_fixture("article_conference.html"))

    assert parser.title == (
        "How do users design scientific workflows? The Case of Snakemake and Nextflow"
    )
    assert "PDF" not in parser.title


def test_article_parser_handles_a_title_with_no_link(read_fixture):
    parser = ss.ArticleParser()
    parser.feed(read_fixture("article_preprint.html"))

    assert parser.title.startswith("DRIP-R: A Benchmark for Decision-Making")
    assert parser.article_url == ""


def test_article_parser_reads_a_value_containing_nested_markup(read_fixture):
    parser = ss.ArticleParser()
    parser.feed(read_fixture("article_conference.html"))

    assert parser.fields["Total citations"] == "Cited by 9"


# --------------------------------------------------------------------------
# Blocking detection
# --------------------------------------------------------------------------


def test_a_captcha_page_raises_rather_than_parsing_as_empty(read_fixture):
    with pytest.raises(ss.ScholarBlocked):
        ss._assert_not_blocked(read_fixture("blocked.html"), "https://scholar.google.com/x")


def test_a_real_profile_page_is_not_mistaken_for_a_block(read_fixture):
    ss._assert_not_blocked(read_fixture("profile_page.html"), "https://scholar.google.com/x")


def test_block_detection_sees_through_html_escaping():
    """The CAPTCHA page writes the apostrophe in "you're" as &#39;."""
    with pytest.raises(ss.ScholarBlocked):
        ss._assert_not_blocked(
            "<html><body>Please show you&#39;re not a robot</body></html>", "https://x/"
        )


@pytest.mark.parametrize("status", [403, 429])
def test_http_fetch_treats_403_and_429_as_a_block(monkeypatch, status):
    def raise_http_error(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, status, "blocked", {}, None)

    monkeypatch.setattr(ss.urllib.request, "urlopen", raise_http_error)

    with pytest.raises(ss.ScholarBlocked):
        ss.http_fetch("https://scholar.google.com/citations?user=X", attempts=1, backoff=0)


def test_http_fetch_retries_other_http_errors_then_gives_up(monkeypatch):
    calls = []

    def raise_http_error(request, timeout=None):
        calls.append(request.full_url)
        raise urllib.error.HTTPError(request.full_url, 500, "boom", {}, None)

    monkeypatch.setattr(ss.urllib.request, "urlopen", raise_http_error)

    with pytest.raises(ss.ScholarError) as excinfo:
        ss.http_fetch("https://scholar.google.com/citations?user=X", attempts=3, backoff=0)

    assert not isinstance(excinfo.value, ss.ScholarBlocked)
    assert len(calls) == 3


# --------------------------------------------------------------------------
# Normalisation helpers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, fallback, expected",
    [
        ("2024/7/15", "", "2024-07-15"),
        ("2025/8", "", "2025-08-01"),
        ("2026", "", "2026-01-01"),
        ("", "2019", "2019-01-01"),
        ("2024/2/30", "", "2024-01-01"),  # invalid day falls back to the year
    ],
)
def test_parse_date_normalises_scholars_partial_dates(raw, fallback, expected):
    assert ss.parse_date(raw, fallback) == expected


def test_parse_date_rejects_an_unusable_date():
    with pytest.raises(ss.ScholarError):
        ss.parse_date("", "")


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Towards a Principled Evaluation of Knowledge Editors",
         "towards-a-principled-evaluation-of-knowledge-editors"),
        ("DRIP-R: A Benchmark!", "drip-r-a-benchmark"),
        ("Über Fünf Dinge", "uber-funf-dinge"),
        ("!!!", "untitled"),
    ],
)
def test_slugify(title, expected):
    assert ss.slugify(title) == expected


def test_slugify_caps_the_slug_length():
    long_title = " ".join(f"word{n}" for n in range(30))
    assert ss.slugify(long_title).count("-") == 11  # 12 words


@pytest.mark.parametrize(
    "venue, label, url, expected",
    [
        ("Transactions of the ACL", "Journal", "", "manuscripts"),
        ("Proceedings of ACL 2025", "Conference", "", "conferences"),
        ("Handbook of Something", "Book", "", "books"),
        ("arXiv preprint arXiv:2605.07699", "Journal", "", "preprints"),
        ("Some Venue", "Source", "https://arxiv.org/abs/2605.07699", "preprints"),
        ("Unknown Venue", "", "", "conferences"),
        # Scholar's label is unreliable for proceedings: it files ACM conference
        # proceedings under "Book" and workshop proceedings under "Source".
        ("Proceedings of the 36th International Conference on SSDBM", "Book", "", "conferences"),
        ("Proceedings of the First Workshop on L2M2", "Source", "", "conferences"),
        ("Annual Meeting of the Association for Computational Linguistics", "Book", "", "conferences"),
        # ...but a preprint stays a preprint even if its venue says "Workshop".
        ("arXiv preprint: Workshop version", "Journal", "", "preprints"),
    ],
)
def test_classify(venue, label, url, expected):
    assert ss.classify(venue, label, url) == expected


def test_pick_venue_prefers_the_article_page_over_the_profile():
    fields = {"Conference": "Full Conference Name", "Publisher": "ACM"}
    assert ss.pick_venue(fields, "Truncated profile venue") == ("Full Conference Name", "Conference")


def test_pick_venue_falls_back_to_the_profile_row():
    assert ss.pick_venue({"Pages": "1-12"}, "Profile venue") == ("Profile venue", "")


def test_format_citation_does_not_double_a_titles_own_punctuation():
    citation = ss.format_citation("S Pohl", "2024", "How do users design workflows?", "SSDBM")
    assert '"How do users design workflows?"' in citation
    assert "??" not in citation and '?."' not in citation


def test_format_citation_shape():
    assert ss.format_citation("A Author", "2025", "A Title", "A Venue") == (
        'A Author. (2025). "A Title." <i>A Venue</i>.'
    )


def test_scholar_user_is_read_from_the_config(tmp_path):
    config = 'author:\n  googlescholar    : "https://scholar.google.com/citations?user=7gohUaEAAAAJ&hl=en"\n'
    assert ss.scholar_user_from_config(config) == "7gohUaEAAAAJ"


def test_missing_scholar_url_in_config_is_an_error():
    with pytest.raises(ss.ScholarError):
        ss.scholar_user_from_config("author:\n  googlescholar    :\n")


# --------------------------------------------------------------------------
# Record building and rendering
# --------------------------------------------------------------------------


@pytest.fixture
def records(fake_scholar):
    fetcher, _ = fake_scholar
    rows = ss.fetch_profile_rows("TESTUSER", fetcher, delay=0)
    return [ss.build_record(row, ss.fetch_article("TESTUSER", row["citation_id"], fetcher), "TESTUSER")
            for row in rows]


def test_build_record_prefers_the_untruncated_article_page_values(records):
    record = records[0]

    assert record["title"].endswith("Snakemake and Nextflow")  # profile was truncated
    assert record["authors"] == (
        "Sebastian Pohl, Nourhan Elfaramawy, Anton Miling, Kedi Cao, Birte Kehr, Matthias Weidlich"
    )
    assert record["venue"].endswith("Database Management")


def test_build_record_derives_date_slug_and_category(records):
    conference, workshop, preprint = records

    assert conference["date"] == "2024-07-15"
    assert conference["category"] == "conferences"
    assert conference["slug"].startswith("2024-07-15-how-do-users-design")

    assert workshop["date"] == "2025-08-01"
    assert preprint["category"] == "preprints"
    assert preprint["date"] == "2026-01-01"


def test_build_record_keeps_the_citation_count_from_the_profile(records):
    assert [record["cited_by"] for record in records] == [9, 0, 0]


def test_render_markdown_emits_parseable_front_matter(records):
    text = ss.render_markdown(records[0])
    head, front_matter, body = text.split("---\n", 2)

    assert head == ""
    assert 'title: "How do users design scientific workflows?' in front_matter
    assert "collection: publications" in front_matter
    assert "category: conferences" in front_matter
    assert "date: 2024-07-15" in front_matter
    assert 'paperurl: "https://example.org/papers/snakemake.pdf"' in front_matter
    assert f"authors: {ss._yaml_quote(records[0]['authors'])}" in front_matter
    assert "Scientific workflow systems are widely used" in body


def test_render_markdown_escapes_quotes_in_titles():
    record = {
        "title": 'A "Quoted" Title',
        "category": "conferences",
        "date": "2024-01-01",
        "venue": "Venue",
        "citation": "x",
        "paperurl": "",
        "scholarurl": "",
        "abstract": "",
    }
    assert 'title: "A \\"Quoted\\" Title"' in ss.render_markdown(record)


def test_render_markdown_omits_optional_fields_when_absent(records):
    text = ss.render_markdown(records[2])  # preprint has no linked paper

    assert "paperurl:" not in text
    assert "excerpt:" not in text


# --------------------------------------------------------------------------
# Overrides
# --------------------------------------------------------------------------


def test_apply_overrides_replaces_fields(records):
    overrides = {records[2]["slug"]: {"category": "manuscripts", "venue": "TACL"}}
    result = ss.apply_overrides(records, overrides)

    assert result[2]["category"] == "manuscripts"
    assert result[2]["venue"] == "TACL"


def test_apply_overrides_can_be_keyed_by_citation_id(records):
    overrides = {records[0]["citation_id"]: {"venue": "SSDBM 2024"}}
    assert ss.apply_overrides(records, overrides)[0]["venue"] == "SSDBM 2024"


def test_apply_overrides_can_drop_a_publication(records):
    result = ss.apply_overrides(records, {records[1]["slug"]: {"skip": True}})

    assert len(result) == 2
    assert all(record["slug"] != records[1]["slug"] for record in result)


def test_overriding_the_date_recomputes_the_slug(records):
    result = ss.apply_overrides(records, {records[2]["slug"]: {"date": "2026-05-08"}})

    assert result[2]["slug"].startswith("2026-05-08-drip-r")


def test_an_unknown_override_category_is_rejected(records):
    with pytest.raises(ss.ScholarError, match="category"):
        ss.apply_overrides(records, {records[0]["slug"]: {"category": "nonsense"}})


def test_missing_overrides_file_is_not_an_error(tmp_path):
    assert ss.load_overrides(tmp_path / "nope.yml") == {}


# --------------------------------------------------------------------------
# Writing to disk
# --------------------------------------------------------------------------


def test_plan_files_rejects_a_filename_collision(records):
    duplicate = dict(records[0])
    with pytest.raises(ss.ScholarError, match="same filename"):
        ss.plan_files([records[0], duplicate])


def test_write_publications_adds_updates_and_removes(tmp_path):
    out = tmp_path / "_publications"
    out.mkdir()
    (out / "stale.md").write_text("old\n", encoding="utf-8")
    (out / "keep.md").write_text("same\n", encoding="utf-8")

    changes = ss.write_publications(out, {"keep.md": "same\n", "new.md": "fresh\n"})

    assert changes == {"added": ["new.md"], "updated": [], "removed": ["stale.md"]}
    assert not (out / "stale.md").exists()
    assert (out / "new.md").read_text(encoding="utf-8") == "fresh\n"


def test_write_publications_reports_content_changes(tmp_path):
    out = tmp_path / "_publications"
    out.mkdir()
    (out / "a.md").write_text("before\n", encoding="utf-8")

    changes = ss.write_publications(out, {"a.md": "after\n"})

    assert changes["updated"] == ["a.md"]
    assert (out / "a.md").read_text(encoding="utf-8") == "after\n"


def test_write_publications_dry_run_touches_nothing(tmp_path):
    out = tmp_path / "_publications"
    out.mkdir()
    (out / "stale.md").write_text("old\n", encoding="utf-8")

    changes = ss.write_publications(out, {"new.md": "fresh\n"}, dry_run=True)

    assert changes["added"] == ["new.md"]
    assert changes["removed"] == ["stale.md"]
    assert (out / "stale.md").exists()
    assert not (out / "new.md").exists()


# --------------------------------------------------------------------------
# End-to-end sync
# --------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "_data").mkdir()
    (tmp_path / "_publications").mkdir()
    return tmp_path


def test_sync_writes_one_file_per_publication(repo, fake_scholar):
    fetcher, _ = fake_scholar
    ss.sync("TESTUSER", repo, fetcher=fetcher, delay=0)

    names = sorted(path.name for path in (repo / "_publications").glob("*.md"))
    assert names == [
        "2024-07-15-how-do-users-design-scientific-workflows-the-case-of-snakemake-and-nextflow.md",
        "2025-08-01-towards-a-principled-evaluation-of-knowledge-editors.md",
        "2026-01-01-drip-r-a-benchmark-for-decision-making-and-reasoning-under-real-world.md",
    ]


def test_sync_writes_the_data_cache_used_by_the_publications_page(repo, fake_scholar):
    fetcher, _ = fake_scholar
    ss.sync("TESTUSER", repo, fetcher=fetcher, delay=0)

    payload = json.loads((repo / "_data" / "scholar.json").read_text(encoding="utf-8"))
    assert payload["publication_count"] == 3
    assert payload["total_citations"] == 9
    assert payload["updated_at"]
    assert len(payload["publications"]) == 3


def test_sync_leaves_the_cache_alone_when_nothing_changed(repo, fake_scholar):
    """updated_at means 'the list changed then', so a no-op run must not touch it."""
    fetcher, _ = fake_scholar
    cache = repo / "_data" / "scholar.json"

    ss.sync("TESTUSER", repo, fetcher=fetcher, delay=0)
    stamped = cache.read_text(encoding="utf-8")
    cache.write_text(stamped.replace(json.loads(stamped)["updated_at"], "2020-01-01"),
                     encoding="utf-8")

    ss.sync("TESTUSER", repo, fetcher=fetcher, delay=0)

    assert json.loads(cache.read_text(encoding="utf-8"))["updated_at"] == "2020-01-01"


def test_sync_rewrites_the_cache_when_publications_change(repo, fake_scholar):
    fetcher, _ = fake_scholar
    cache = repo / "_data" / "scholar.json"
    cache.write_text('{"publication_count": 99, "updated_at": "2020-01-01"}\n', encoding="utf-8")

    ss.sync("TESTUSER", repo, fetcher=fetcher, delay=0)

    payload = json.loads(cache.read_text(encoding="utf-8"))
    assert payload["publication_count"] == 3
    assert payload["updated_at"] != "2020-01-01"


def test_sync_survives_a_corrupt_cache(repo, fake_scholar):
    fetcher, _ = fake_scholar
    cache = repo / "_data" / "scholar.json"
    cache.write_text("not json at all", encoding="utf-8")

    ss.sync("TESTUSER", repo, fetcher=fetcher, delay=0)

    assert json.loads(cache.read_text(encoding="utf-8"))["publication_count"] == 3


def test_sync_removes_publications_that_left_the_profile(repo, fake_scholar):
    fetcher, _ = fake_scholar
    stale = repo / "_publications" / "2001-01-01-gone.md"
    stale.write_text("---\ntitle: Gone\n---\n", encoding="utf-8")

    changes = ss.sync("TESTUSER", repo, fetcher=fetcher, delay=0)

    assert "2001-01-01-gone.md" in changes["removed"]
    assert not stale.exists()


def test_sync_is_idempotent(repo, fake_scholar):
    fetcher, _ = fake_scholar
    ss.sync("TESTUSER", repo, fetcher=fetcher, delay=0)
    changes = ss.sync("TESTUSER", repo, fetcher=fetcher, delay=0)

    assert changes == {"added": [], "updated": [], "removed": []}


def test_sync_refuses_to_wipe_publications_on_an_empty_profile(repo, read_fixture):
    existing = repo / "_publications" / "2024-01-01-real-paper.md"
    existing.write_text("---\ntitle: Real\n---\n", encoding="utf-8")

    def empty_fetcher(url: str) -> str:
        return read_fixture("profile_page_empty.html")

    with pytest.raises(ss.ScholarError, match="no articles"):
        ss.sync("TESTUSER", repo, fetcher=empty_fetcher, delay=0)

    assert existing.exists()


def test_sync_propagates_a_block_without_touching_the_files(repo, read_fixture):
    existing = repo / "_publications" / "2024-01-01-real-paper.md"
    existing.write_text("---\ntitle: Real\n---\n", encoding="utf-8")

    def blocked_fetcher(url: str) -> str:
        ss._assert_not_blocked(read_fixture("blocked.html"), url)
        raise AssertionError("unreachable")

    with pytest.raises(ss.ScholarBlocked):
        ss.sync("TESTUSER", repo, fetcher=blocked_fetcher, delay=0)

    assert existing.read_text(encoding="utf-8") == "---\ntitle: Real\n---\n"


def test_sync_applies_the_overrides_file(repo, fake_scholar):
    pytest.importorskip("yaml")
    fetcher, _ = fake_scholar
    (repo / "_data" / "publication_overrides.yml").write_text(
        "2026-01-01-drip-r-a-benchmark-for-decision-making-and-reasoning-under-real-world:\n"
        "  skip: true\n",
        encoding="utf-8",
    )

    ss.sync("TESTUSER", repo, fetcher=fetcher, delay=0)

    names = [path.name for path in (repo / "_publications").glob("*.md")]
    assert len(names) == 2
    assert not any("drip-r" in name for name in names)


def test_sync_pages_through_the_profile(repo, fake_scholar):
    fetcher, calls = fake_scholar
    ss.sync("TESTUSER", repo, fetcher=fetcher, delay=0)

    profile_calls = [url for url in calls if "view_op=view_citation" not in url]
    assert profile_calls[0].endswith("cstart=0&pagesize=100")
    # Only one profile request: the first page was short, so there is no page 2.
    assert len(profile_calls) == 1


def test_main_check_mode_reports_drift_without_writing(repo, fake_scholar, monkeypatch):
    fetcher, _ = fake_scholar
    monkeypatch.setattr(ss, "http_fetch", fetcher)

    exit_code = ss.main(["--user", "TESTUSER", "--repo-root", str(repo), "--delay", "0", "--check"])

    assert exit_code == 1
    assert list((repo / "_publications").glob("*.md")) == []
    assert not (repo / "_data" / "scholar.json").exists()


def test_main_returns_zero_when_nothing_changes(repo, fake_scholar, monkeypatch):
    fetcher, _ = fake_scholar
    monkeypatch.setattr(ss, "http_fetch", fetcher)
    args = ["--user", "TESTUSER", "--repo-root", str(repo), "--delay", "0"]

    assert ss.main(args) == 0
    assert ss.main(args) == 0


def test_main_reports_a_block_with_its_own_exit_code(repo, read_fixture, monkeypatch):
    """The workflow distinguishes 'rate-limited' from 'broken' by exit code."""

    def blocked_fetcher(url: str) -> str:
        ss._assert_not_blocked(read_fixture("blocked.html"), url)
        raise AssertionError("unreachable")

    monkeypatch.setattr(ss, "http_fetch", blocked_fetcher)

    exit_code = ss.main(["--user", "TESTUSER", "--repo-root", str(repo), "--delay", "0"])
    assert exit_code == ss.EXIT_BLOCKED


def test_main_reports_other_failures_as_errors(repo, read_fixture, monkeypatch):
    def empty_fetcher(url: str) -> str:
        return read_fixture("profile_page_empty.html")

    monkeypatch.setattr(ss, "http_fetch", empty_fetcher)

    exit_code = ss.main(["--user", "TESTUSER", "--repo-root", str(repo), "--delay", "0"])
    assert exit_code == ss.EXIT_ERROR
