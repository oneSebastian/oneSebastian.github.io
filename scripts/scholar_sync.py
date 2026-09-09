#!/usr/bin/env python3
"""Regenerate _publications/ from a Google Scholar profile.

Google Scholar has no public API, so this walks the profile's HTML: first the
list of articles on the profile page, then each article's own page for the full
title, author list, venue and abstract (the profile page truncates all of them).

The generated Markdown is committed to the repository, so the site keeps
rendering the last good result whenever a fetch is blocked or fails. Nothing is
written unless the whole run succeeds.

Hand corrections belong in _data/publication_overrides.yml, keyed by the slug
that appears in each generated filename -- files under _publications/ are
overwritten on every run.

Usage:
    python scripts/scholar_sync.py              # read the profile from _config.yml
    python scripts/scholar_sync.py --check      # report drift, write nothing
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable

SCHOLAR_HOST = "https://scholar.google.com"
PROFILE_URL = SCHOLAR_HOST + "/citations?user={user}&hl=en&oi=ao&cstart={start}&pagesize={size}"
ARTICLE_URL = SCHOLAR_HOST + "/citations?view_op=view_citation&hl=en&user={user}&citation_for_view={cid}"
PAGE_SIZE = 100

# Scholar serves the real page to browsers only; a default urllib User-Agent is
# refused outright.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

BLOCK_MARKERS = (
    "gs_captcha",
    "recaptcha",
    "please show you're not a robot",
    "our systems have detected unusual traffic",
)

#: Status codes Scholar uses to turn away clients it thinks are bots.
BLOCK_STATUSES = frozenset({403, 429})

VALID_CATEGORIES = ("books", "manuscripts", "conferences", "preprints")

#: Exit codes. The workflow treats EXIT_BLOCKED as "try again next week" and
#: EXIT_ERROR as a genuine failure worth an email.
EXIT_OK = 0
EXIT_DRIFT = 1  # --check only: the generated files are out of date
EXIT_ERROR = 2
EXIT_BLOCKED = 3

Fetcher = Callable[[str], str]


class ScholarError(RuntimeError):
    """A fetch or parse failed in a way that should abort the whole sync."""


class ScholarBlocked(ScholarError):
    """Scholar served a CAPTCHA or rate-limit page instead of the profile."""


# --------------------------------------------------------------------------
# HTML parsing
# --------------------------------------------------------------------------


def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
    for name, value in attrs:
        if name == "class" and value:
            return set(value.split())
    return set()


def _attr(attrs: list[tuple[str, str | None]], key: str) -> str:
    for name, value in attrs:
        if name == key:
            return value or ""
    return ""


def _clean(text: str) -> str:
    """Collapse whitespace and normalise the entities Scholar emits."""
    text = html.unescape(text)
    text = text.replace(" ", " ").replace("​", "")
    return re.sub(r"\s+", " ", text).strip()


def _strip_trailing_year(venue: str) -> str:
    """Drop a trailing ', 2024' left on a profile-page venue.

    Belt and braces: the year normally lives in its own <span class="gs_oph">,
    which ProfileParser skips, but Scholar sometimes inlines it instead.
    """
    return re.sub(r",\s*(19|20)\d{2}\s*$", "", venue).strip().rstrip(",")


class _CapturingParser(HTMLParser):
    """Base parser that captures the text of an element and its descendants.

    Scholar wraps values in nested markup (links inside author lists, <span>s
    inside venues), so capture has to survive nesting rather than stop at the
    first close tag. Some of that nested markup is chrome rather than content --
    the ", 2024" appended to a profile venue, the "[PDF] example.org" badge next
    to an article title -- so subtrees whose class is listed in SKIP_CLASSES are
    dropped even while a capture is running.
    """

    #: Classes whose entire subtree is chrome and must not reach any capture.
    SKIP_CLASSES: frozenset[str] = frozenset()

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._capture_depth: int | None = None
        self._buffer: list[str] = []
        self._skip_depth = 0

    def _start_capture(self) -> None:
        self._capture_depth = 0
        self._buffer = []

    def _stop_capture(self) -> str:
        self._capture_depth = None
        return _clean("".join(self._buffer))

    @property
    def _capturing(self) -> bool:
        return self._capture_depth is not None

    def _enter(self, attrs: list[tuple[str, str | None]]) -> bool:
        """Handle skip bookkeeping for a start tag.

        Returns True when the caller should ignore this tag entirely, either
        because it opens a chrome subtree or because we are already inside one.
        """
        if self._skip_depth:
            self._skip_depth += 1
            return True
        if _classes(attrs) & self.SKIP_CLASSES:
            self._skip_depth = 1
            return True
        return False

    def _leave(self) -> bool:
        """Mirror of _enter for end tags; True means the tag was chrome."""
        if self._skip_depth:
            self._skip_depth -= 1
            return True
        return False

    def handle_data(self, data: str) -> None:
        if self._capturing and not self._skip_depth:
            self._buffer.append(data)


class ProfileParser(_CapturingParser):
    """Pull the article rows out of a Google Scholar profile page.

    Each row is a <tr class="gsc_a_tr"> holding the article link (whose
    citation_for_view parameter is the stable article id), a truncated title,
    two <div class="gs_gray"> blocks for authors and venue, the citation count
    and the year.
    """

    # The venue div ends with a <span class="gs_oph">, 2024</span>. The year has
    # its own column, so drop the span rather than glue it onto the venue name.
    SKIP_CLASSES = frozenset({"gs_oph"})

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict] = []
        self._row: dict | None = None
        self._field: str | None = None
        self._gray_seen = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._enter(attrs):
            return
        if self._capturing:
            self._capture_depth += 1  # type: ignore[operator]
            return

        classes = _classes(attrs)
        if tag == "tr" and "gsc_a_tr" in classes:
            self._row = {"citation_id": "", "title": "", "authors": "", "venue": "", "cited_by": 0, "year": ""}
            self._gray_seen = 0
        elif self._row is None:
            return
        elif tag == "a" and "gsc_a_at" in classes:
            href = _attr(attrs, "href")
            match = re.search(r"citation_for_view=([^&\"]+)", href)
            if match:
                self._row["citation_id"] = html.unescape(match.group(1))
            self._field = "title"
            self._start_capture()
        elif tag == "div" and "gs_gray" in classes:
            self._field = "authors" if self._gray_seen == 0 else "venue"
            self._gray_seen += 1
            self._start_capture()
        elif tag == "a" and "gsc_a_ac" in classes:
            self._field = "cited_by"
            self._start_capture()
        elif tag == "span" and "gsc_a_h" in classes:
            self._field = "year"
            self._start_capture()

    def handle_endtag(self, tag: str) -> None:
        if self._leave():
            return
        if self._capturing:
            if self._capture_depth:  # still inside a nested element
                self._capture_depth -= 1  # type: ignore[operator]
                return
            value = self._stop_capture()
            if self._row is not None and self._field:
                if self._field == "cited_by":
                    self._row["cited_by"] = int(value) if value.isdigit() else 0
                else:
                    self._row[self._field] = value
            self._field = None
        elif tag == "tr" and self._row is not None:
            if self._row["citation_id"]:
                self._row["venue"] = _strip_trailing_year(self._row["venue"])
                self.rows.append(self._row)
            self._row = None


class ArticleParser(_CapturingParser):
    """Pull the labelled fields out of a single Scholar article page.

    The page is a title (usually linking out to the publisher or arXiv) followed
    by <div class="gs_scl"> blocks, each pairing a gsc_oci_field label such as
    "Authors" or "Publication date" with a gsc_oci_value.
    """

    # The "[PDF] example.org" badge sits inside the title div and is not part of
    # the title.
    SKIP_CLASSES = frozenset({"gsc_oci_title_ggi"})

    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.article_url = ""
        self.fields: dict[str, str] = {}
        self._pending_label: str | None = None
        self._mode: str | None = None
        self._in_title_div = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._enter(attrs):
            return

        classes = _classes(attrs)
        # The title link sits *inside* the title div, so read its href before
        # the capture-depth bookkeeping below returns early on nested tags.
        if tag == "a" and "gsc_oci_title_link" in classes and self._in_title_div:
            self.article_url = html.unescape(_attr(attrs, "href"))

        if self._capturing:
            self._capture_depth += 1  # type: ignore[operator]
            return

        if tag == "div" and _attr(attrs, "id") == "gsc_oci_title":
            # Capture the whole div rather than the inner link: titles without a
            # publisher link have no <a> at all.
            self._in_title_div = True
            self._mode = "title"
            self._start_capture()
        elif tag == "div" and "gsc_oci_field" in classes:
            self._mode = "label"
            self._start_capture()
        elif tag == "div" and "gsc_oci_value" in classes and self._pending_label:
            self._mode = "value"
            self._start_capture()

    def handle_endtag(self, tag: str) -> None:
        if self._leave():
            return
        if not self._capturing:
            return
        if self._capture_depth:
            self._capture_depth -= 1  # type: ignore[operator]
            return

        value = self._stop_capture()
        if self._mode == "title":
            self.title = value
            self._in_title_div = False
        elif self._mode == "label":
            self._pending_label = value
        elif self._mode == "value":
            if self._pending_label:
                self.fields[self._pending_label] = value
            self._pending_label = None
        self._mode = None


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------


def _blocked(url: str) -> ScholarBlocked:
    return ScholarBlocked(
        f"Google Scholar served a CAPTCHA / rate-limit page for {url}. "
        "This is common from datacenter IPs such as GitHub Actions runners; "
        "the existing publications were left untouched."
    )


def _assert_not_blocked(body: str, url: str) -> None:
    # Unescape first: the CAPTCHA page writes the apostrophe in "you're" as
    # &#39;, which would otherwise slip past the marker.
    head = html.unescape(body[:4000]).lower()
    if any(marker in head for marker in BLOCK_MARKERS):
        raise _blocked(url)


def http_fetch(url: str, *, attempts: int = 3, backoff: float = 5.0) -> str:
    """GET a Scholar URL, retrying transient failures with a growing delay."""
    last_error: Exception | None = None
    for attempt in range(attempts):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                body = response.read().decode(charset, errors="replace")
            _assert_not_blocked(body, url)
            return body
        except ScholarBlocked:
            raise  # retrying a CAPTCHA only digs the hole deeper
        except urllib.error.HTTPError as exc:
            if exc.code in BLOCK_STATUSES:
                raise _blocked(url) from exc
            last_error = exc
            if attempt < attempts - 1:
                time.sleep(backoff * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < attempts - 1:
                time.sleep(backoff * (attempt + 1))
    raise ScholarError(f"Could not fetch {url}: {last_error}")


def fetch_profile_rows(user: str, fetcher: Fetcher, delay: float = 2.0) -> list[dict]:
    """Page through the profile until Scholar stops returning new articles."""
    rows: list[dict] = []
    seen: set[str] = set()
    start = 0
    while True:
        parser = ProfileParser()
        parser.feed(fetcher(PROFILE_URL.format(user=user, start=start, size=PAGE_SIZE)))
        new = [row for row in parser.rows if row["citation_id"] not in seen]
        if not new:
            break
        seen.update(row["citation_id"] for row in new)
        rows.extend(new)
        if len(parser.rows) < PAGE_SIZE:
            break
        start += PAGE_SIZE
        time.sleep(delay)
    return rows


def fetch_article(user: str, citation_id: str, fetcher: Fetcher) -> dict:
    parser = ArticleParser()
    # Citation ids contain ':' and '_', which stay literal in a query string.
    cid = urllib.parse.quote(citation_id, safe=":_-")
    parser.feed(fetcher(ARTICLE_URL.format(user=user, cid=cid)))
    return {"title": parser.title, "article_url": parser.article_url, "fields": parser.fields}


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------


def slugify(text: str, *, max_words: int = 12) -> str:
    """Turn a title into a stable, filename-safe slug."""
    normalised = unicodedata.normalize("NFKD", text)
    ascii_text = normalised.encode("ascii", "ignore").decode("ascii").lower()
    words = re.findall(r"[a-z0-9]+", ascii_text)
    return "-".join(words[:max_words]) or "untitled"


def parse_date(raw: str, fallback_year: str = "") -> str:
    """Normalise Scholar's '2025/7/8', '2024/5' or '2024' into an ISO date.

    Scholar omits the day (and sometimes the month) for many venues, so missing
    components default to the first of the period rather than being dropped --
    publications are sorted by this value.
    """
    parts = [part for part in re.split(r"[/-]", raw.strip()) if part.isdigit()]
    if not parts:
        parts = [fallback_year] if fallback_year.isdigit() else []
    if not parts:
        raise ScholarError(f"No usable publication date in {raw!r}")
    year = int(parts[0])
    month = int(parts[1]) if len(parts) > 1 else 1
    day = int(parts[2]) if len(parts) > 2 else 1
    try:
        return dt.date(year, month, day).isoformat()
    except ValueError:
        return dt.date(year, 1, 1).isoformat()


def pick_venue(fields: dict[str, str], profile_venue: str) -> tuple[str, str]:
    """Return (venue, source_label) preferring the article page over the profile."""
    for label in ("Journal", "Conference", "Book", "Source", "Institution", "Publisher"):
        if fields.get(label):
            return fields[label], label
    return profile_venue, ""


#: Venue names that mean "conference paper" whatever Scholar's field label says.
_PROCEEDINGS_RE = re.compile(
    r"\bproceedings\b|\bconference\b|\bworkshop\b|\bsymposium\b|\bmeeting of\b", re.IGNORECASE
)


def classify(venue: str, source_label: str, article_url: str) -> str:
    """Map a venue onto one of the site's publication_category keys.

    Scholar's field label is only a hint: it files ACM conference proceedings
    under "Book" and arXiv preprints under "Journal", so the venue text is
    checked first in both of those cases.
    """
    haystack = f"{venue} {article_url}".lower()
    if "arxiv" in haystack or "preprint" in haystack or "biorxiv" in haystack:
        return "preprints"
    if _PROCEEDINGS_RE.search(venue):
        return "conferences"
    if source_label == "Journal":
        return "manuscripts"
    if source_label == "Book":
        return "books"
    return "conferences"


def format_citation(authors: str, year: str, title: str, venue: str) -> str:
    """Build the 'Recommended citation' string the archive templates render.

    Shape: `Authors. (Year). "Title." <i>Venue</i>.` -- each segment carries its
    own terminator, because the title's is inside the closing quote and titles
    ending in '?' or '!' must not also gain a period.
    """
    segments = []
    if authors:
        segments.append(authors.rstrip(". ") + ".")
    if year:
        segments.append(f"({year}).")
    terminator = "" if title.endswith((".", "?", "!")) else "."
    segments.append(f'"{title}{terminator}"')
    if venue:
        segments.append(f"<i>{venue}</i>.")
    return " ".join(segments)


def build_record(row: dict, article: dict, user: str) -> dict:
    """Combine a profile row and its article page into one normalised record."""
    fields = article["fields"]
    title = article["title"] or row["title"]
    authors = fields.get("Authors", row["authors"])
    venue, source_label = pick_venue(fields, row["venue"])
    date = parse_date(fields.get("Publication date", ""), row["year"])
    year = date[:4]
    return {
        "citation_id": row["citation_id"],
        "slug": f"{date}-{slugify(title)}",
        "title": title,
        "authors": authors,
        "venue": venue,
        "date": date,
        "year": year,
        "category": classify(venue, source_label, article["article_url"]),
        "paperurl": article["article_url"],
        "scholarurl": ARTICLE_URL.format(user=user, cid=row["citation_id"]),
        "abstract": fields.get("Description", ""),
        "cited_by": row["cited_by"],
        "citation": format_citation(authors, year, title, venue),
    }


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def _yaml_quote(value: str) -> str:
    """Emit a double-quoted YAML scalar."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_markdown(record: dict) -> str:
    lines = [
        "---",
        "# Generated by scripts/scholar_sync.py -- edits here are overwritten.",
        "# Corrections belong in _data/publication_overrides.yml.",
        f"title: {_yaml_quote(record['title'])}",
        "collection: publications",
        f"category: {record['category']}",
        f"date: {record['date']}",
        f"venue: {_yaml_quote(record['venue'])}",
    ]
    # The publications page renders authors as their own line, so they are
    # emitted separately rather than only inside the citation string.
    if record.get("authors"):
        lines.append(f"authors: {_yaml_quote(record['authors'])}")
    if record.get("paperurl"):
        lines.append(f"paperurl: {_yaml_quote(record['paperurl'])}")
    if record.get("scholarurl"):
        lines.append(f"scholarurl: {_yaml_quote(record['scholarurl'])}")
    if record.get("excerpt"):
        lines.append(f"excerpt: {_yaml_quote(record['excerpt'])}")
    lines.append(f"citation: {_yaml_quote(record['citation'])}")
    lines.append("---")
    lines.append("")
    body = (record.get("body") or record.get("abstract") or "").strip()
    if body:
        lines.append(body)
        lines.append("")
    return "\n".join(lines)


def load_overrides(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on environment
        raise ScholarError(
            f"{path} exists but PyYAML is not installed "
            "(pip install -r scripts/requirements.txt)"
        ) from exc
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ScholarError(f"{path} must contain a mapping of slug -> overrides")
    return {str(key): (value or {}) for key, value in data.items()}


def apply_overrides(records: list[dict], overrides: dict[str, dict]) -> list[dict]:
    """Apply per-publication corrections, keyed by slug or Scholar citation id."""
    result = []
    for record in records:
        override = overrides.get(record["slug"]) or overrides.get(record["citation_id"]) or {}
        if override.get("skip"):
            continue
        merged = dict(record)
        merged.update({k: v for k, v in override.items() if k != "skip"})
        if merged["category"] not in VALID_CATEGORIES:
            raise ScholarError(
                f"Override for {record['slug']!r} sets category "
                f"{merged['category']!r}, which is not one of {VALID_CATEGORIES}"
            )
        if merged["date"] != record["date"] or merged["title"] != record["title"]:
            merged["slug"] = f"{merged['date']}-{slugify(merged['title'])}"
        result.append(merged)
    return result


def plan_files(records: Iterable[dict]) -> dict[str, str]:
    """Map filename -> contents, rejecting slug collisions rather than losing one."""
    files: dict[str, str] = {}
    for record in records:
        name = f"{record['slug']}.md"
        if name in files:
            raise ScholarError(f"Two publications produce the same filename: {name}")
        files[name] = render_markdown(record)
    return files


def write_publications(out_dir: Path, files: dict[str, str], *, dry_run: bool = False) -> dict[str, list[str]]:
    """Make out_dir match `files` exactly; returns the added/updated/removed sets."""
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = {path.name: path for path in out_dir.glob("*.md")}
    changes = {"added": [], "updated": [], "removed": []}

    for name, content in sorted(files.items()):
        path = existing.get(name)
        if path is None:
            changes["added"].append(name)
        elif path.read_text(encoding="utf-8") != content:
            changes["updated"].append(name)
        else:
            continue
        if not dry_run:
            (out_dir / name).write_text(content, encoding="utf-8", newline="\n")

    for name, path in sorted(existing.items()):
        if name not in files:
            changes["removed"].append(name)
            if not dry_run:
                path.unlink()

    return changes


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def scholar_user_from_config(config_text: str) -> str:
    """Read the Scholar user id out of _config.yml's author.googlescholar URL."""
    match = re.search(r"googlescholar\s*:\s*[\"']?[^\"'\n]*[?&]user=([A-Za-z0-9_-]+)", config_text)
    if not match:
        raise ScholarError(
            "Could not find a Google Scholar profile URL at author.googlescholar "
            "in _config.yml; pass --user instead."
        )
    return match.group(1)


def sync(
    user: str,
    repo_root: Path,
    *,
    fetcher: Fetcher | None = None,
    delay: float = 2.0,
    dry_run: bool = False,
) -> dict:
    fetcher = fetcher or http_fetch
    rows = fetch_profile_rows(user, fetcher, delay=delay)
    if not rows:
        raise ScholarError(
            "The Scholar profile returned no articles. Refusing to delete the "
            "existing publications on the strength of an empty response."
        )

    records = []
    for index, row in enumerate(rows):
        if index:
            time.sleep(delay)
        records.append(build_record(row, fetch_article(user, row["citation_id"], fetcher), user))

    records = apply_overrides(records, load_overrides(repo_root / "_data" / "publication_overrides.yml"))
    records.sort(key=lambda record: (record["date"], record["title"]))

    changes = write_publications(repo_root / "_publications", plan_files(records), dry_run=dry_run)

    if not dry_run:
        _write_cache(repo_root / "_data" / "scholar.json", user, records)

    return changes


def _write_cache(path: Path, user: str, records: list[dict]) -> bool:
    """Refresh _data/scholar.json, but only when something actually changed.

    `updated_at` is the date the publication list last changed, not the date it
    was last checked. Stamping every run would rewrite this file weekly and give
    the workflow an empty commit to make each time.

    Returns True if the file was written.
    """
    payload = {
        "profile": f"{SCHOLAR_HOST}/citations?user={user}&hl=en",
        "updated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"),
        "publication_count": len(records),
        "total_citations": sum(record["cited_by"] for record in records),
        "publications": records,
    }

    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous = {}
        unchanged = all(previous.get(key) == payload[key]
                        for key in ("profile", "publication_count", "total_citations", "publications"))
        if unchanged:
            return False

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user", help="Google Scholar user id (default: read from _config.yml)")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--delay", type=float, default=float(os.environ.get("SCHOLAR_DELAY", "2.0")),
                        help="Seconds to wait between Scholar requests (default: 2.0)")
    parser.add_argument("--check", action="store_true",
                        help="Report what would change and exit 1 if anything would; write nothing")
    args = parser.parse_args(argv)

    repo_root: Path = args.repo_root
    user = args.user or scholar_user_from_config((repo_root / "_config.yml").read_text(encoding="utf-8"))

    try:
        changes = sync(user, repo_root, delay=args.delay, dry_run=args.check)
    except ScholarBlocked as exc:
        # Its own exit code: being rate-limited is expected from shared IPs and
        # means "try later", not "the sync is broken".
        print(f"scholar_sync: {exc}", file=sys.stderr)
        return EXIT_BLOCKED
    except ScholarError as exc:
        print(f"scholar_sync: {exc}", file=sys.stderr)
        return EXIT_ERROR

    total = sum(len(names) for names in changes.values())
    labels = {"added": "add", "updated": "update", "removed": "remove"}
    for kind, label in labels.items():
        for name in changes[kind]:
            print(f"{label:>6}: {name}")
    if total == 0:
        print("Publications are already up to date.")
    elif args.check:
        print(f"{total} file(s) would change.")
        return EXIT_DRIFT
    else:
        print(f"{total} file(s) changed.")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
