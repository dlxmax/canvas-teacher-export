#!/usr/bin/env python3
"""
canvas_archive.py: a single-file Canvas course archiver.

One file, no dependencies beyond the Python standard library. Archives a single
course (--course-id), every course your cookie can see (--all, resumable, one
subprocess per course), or rebuilds the master index over already-archived
folders (--rebuild-index). Run with no arguments for an interactive prompt.

Layout per course:

    <Course Name (sem) YYYY>/
    ├── _course_meta.json
    ├── Students.csv               (with section column)
    ├── Assignments.csv
    ├── Gradebook.csv              (Canvas native format: student × assignment scores)
    ├── Syllabus.html
    ├── Modules.html
    ├── Assignments/
    │   └── <assignment>/
    │       ├── <assignment>.html        overview: description + rubric
    │       └── [<section>/]             (subfolder only when course has >1 section)
    │           ├── _grades.csv
    │           ├── <Student>.html        with JSON island + data-* attrs
    │           ├── <Student> <attachment>.ext
    │           └── _<Student>.json       raw audit copy
    ├── Discussions/
    │   └── <Topic>[ - <section>].html    suffix only when split
    ├── Quizzes/
    │   └── <Quiz Title>/                 (mirrors the Assignments layout)
    │       ├── <Quiz Title>.html         questions + per-student score table
    │       └── _grades.csv               per-student, per-question results
    ├── Pages/<Page Title>.html
    ├── Announcements/<Date> <Title>.html
    └── Files/<canvas folder tree preserved>

Pure stdlib. Cookies via Cookie-Editor JSON export.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from typing import Any, Iterable, Iterator


def _utc_now_iso() -> str:
    """ISO-8601 UTC timestamp, ending in 'Z' (Canvas-style)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

logger = logging.getLogger("canvas_archive")

SUBMISSION_BEARING_TYPES = {"online_text_entry", "online_upload", "external_tool", "discussion_topic"}
SCHEMA_SUBMISSION = "canvas-archive/submission/v1"
SCHEMA_DISCUSSION = "canvas-archive/discussion/v1"
SCHEMA_QUIZ = "canvas-archive/quiz/v1"
# Term-recognition schemes. Each maps a calendar month (1-12) to
# (term_number, year_offset): the numeric term label shown in the folder name
# and a shift applied to the labeled year (so e.g. a January course can be
# tagged as the prior year's term). A course is classified by the month of its
# earliest assignment due date. "none" disables tagging entirely. Configurable
# in the interactive settings and via --term-scheme; "korean" is the default.
TERM_SCHEMES: dict[str, dict[int, tuple[int, int]]] = {
    # Korean university calendar. (1) Spring can start late Feb but usually
    # March, classes end ~late June; July is summer break (folded into the
    # just-finished spring). (2) Fall can start late Aug but usually Sept,
    # through December; January is deep winter break, belonging to the prior
    # year's fall term.
    "korean": {
        2: (1, 0), 3: (1, 0), 4: (1, 0), 5: (1, 0), 6: (1, 0), 7: (1, 0),
        8: (2, 0), 9: (2, 0), 10: (2, 0), 11: (2, 0), 12: (2, 0),
        1: (2, -1),
    },
    # US-style: Spring (1) = Jan-May, Summer (2) = Jun-Jul, Fall (3) = Aug-Dec.
    "us": {
        1: (1, 0), 2: (1, 0), 3: (1, 0), 4: (1, 0), 5: (1, 0),
        6: (2, 0), 7: (2, 0),
        8: (3, 0), 9: (3, 0), 10: (3, 0), 11: (3, 0), 12: (3, 0),
    },
    # Trimesters: T1 (1) = Jan-Apr, T2 (2) = May-Aug, T3 (3) = Sep-Dec.
    "trimester": {
        1: (1, 0), 2: (1, 0), 3: (1, 0), 4: (1, 0),
        5: (2, 0), 6: (2, 0), 7: (2, 0), 8: (2, 0),
        9: (3, 0), 10: (3, 0), 11: (3, 0), 12: (3, 0),
    },
    # Summer/Winter sessions. Winter (1) = Dec-Feb (one session spanning the
    # year boundary, labeled by its December year). Summer (2) = Jun-Aug.
    # Everything else falls back to a generic regular term (3).
    "summer_winter": {
        12: (1, 0), 1: (1, -1), 2: (1, -1),
        6: (2, 0), 7: (2, 0), 8: (2, 0),
        3: (3, 0), 4: (3, 0), 5: (3, 0), 9: (3, 0), 10: (3, 0), 11: (3, 0),
    },
}
DEFAULT_TERM_SCHEME = "korean"
# Order shown in the interactive menu; "none" disables term tagging.
TERM_SCHEME_CHOICES = ("korean", "us", "trimester", "summer_winter", "none")

# Concurrency knobs. Sixteen threads is enough to amortize Canvas + S3 latency
# without tripping rate limits. Stages run one at a time, so peak concurrency is
# this many requests regardless of how many phases are parallelized. Override at
# runtime with --workers (lower it if Canvas starts returning 503s). GraphQL
# batching keeps the inner loop cheap. GQL_BATCH_SIZE is intentionally small:
# Canvas GraphQL silently returns empty objects when too many top-level aliases
# are requested in a single call. 20 is the largest size that returns complete
# data across the courses tested.
HTTP_WORKERS = 16
GQL_BATCH_SIZE = 20


def _run_parallel(items: list, worker, workers: int) -> list:
    """Map worker over items, preserving input order. Runs serially when there
    is nothing to gain (workers<=1 or a single item); otherwise uses a thread
    pool capped at `workers`. Worker exceptions propagate, so each worker should
    catch and log its own errors and return a sentinel."""
    if not items:
        return []
    if workers <= 1 or len(items) == 1:
        return [worker(it) for it in items]
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as pool:
        return list(pool.map(worker, items))


# ============================================================================
# Cookie + HTTP
# ============================================================================

def load_cookies(path: Path, base_url: str) -> CookieJar:
    raw = json.loads(path.read_text(encoding="utf-8"))
    jar = CookieJar()
    host = urllib.parse.urlparse(base_url).hostname or ""
    for e in raw:
        name = e.get("name")
        if not name:
            continue  # skip malformed entries that carry no cookie name
        domain = (e.get("domain") or host).lstrip(".")
        c = Cookie(
            version=0, name=name, value=e.get("value") or "",
            port=None, port_specified=False,
            domain=domain, domain_specified=True,
            domain_initial_dot=(e.get("domain") or "").startswith("."),
            path=e.get("path", "/"), path_specified=True,
            secure=bool(e.get("secure", False)),
            expires=int(e["expirationDate"]) if e.get("expirationDate") else None,
            discard=False, comment=None, comment_url=None,
            rest={"HttpOnly": ""} if e.get("httpOnly") else {},
            rfc2109=False,
        )
        jar.set_cookie(c)
    logger.info("loaded %d cookies for %s", len(jar), host)
    return jar


def csrf_from_jar(jar: CookieJar) -> str:
    """Canvas requires X-CSRF-Token on POSTs (including /api/graphql).
    The browser session writes it as the _csrf_token cookie; the value is URL-encoded."""
    for c in jar:
        if c.name == "_csrf_token":
            return urllib.parse.unquote(c.value or "")
    return ""


# Transient HTTP statuses worth retrying. 503 means Canvas is briefly
# overloaded (often because we are requesting too fast); 429 is explicit rate
# limiting; 502/504 are gateway hiccups. All clear up on a short pause.
RETRY_STATUSES = {429, 502, 503, 504}
MAX_HTTP_RETRIES = 4
RETRY_PAUSE_SECONDS = 1.0


@dataclass
class Canvas:
    base: str
    opener: urllib.request.OpenerDirector
    csrf: str = ""

    def _req(self, url: str, accept: str = "application/json+canvas-string-ids") -> urllib.request.Request:
        return urllib.request.Request(url, headers={
            "Accept": accept,
            "User-Agent": "canvas-archive/0.1",
        })

    def _open(self, req, timeout: int):
        """Open a request, retrying transient HTTP errors (503/429/502/504)
        after a pause. Honors Retry-After when Canvas supplies it. Raises the
        last error once retries are exhausted."""
        attempt = 0
        while True:
            try:
                return self.opener.open(req, timeout=timeout)
            except urllib.error.HTTPError as ex:
                if ex.code not in RETRY_STATUSES or attempt >= MAX_HTTP_RETRIES:
                    raise
                retry_after = ex.headers.get("Retry-After") if ex.headers else None
                try:
                    pause = float(retry_after) if retry_after else RETRY_PAUSE_SECONDS * (attempt + 1)
                except (TypeError, ValueError):
                    pause = RETRY_PAUSE_SECONDS * (attempt + 1)
                attempt += 1
                url = req.full_url if hasattr(req, "full_url") else str(req)
                logger.warning("  [HTTP %s, retry %d/%d after %.0fs] %s",
                               ex.code, attempt, MAX_HTTP_RETRIES, pause, url)
                time.sleep(pause)

    def get_json(self, url: str) -> tuple[Any, Any]:
        with self._open(self._req(url), timeout=120) as r:
            return json.loads(r.read().decode("utf-8")), r.headers

    def get_paginated(self, url: str) -> list[dict]:
        items: list[dict] = []
        while url:
            data, headers = self.get_json(url)
            if isinstance(data, list):
                items.extend(data)
            else:
                items.append(data)
            url = parse_next_link(headers.get("Link"))
        return items

    def download(self, url: str, dest: Path, skip_if_exists: bool = True) -> int:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if skip_if_exists and dest.exists() and dest.stat().st_size > 0:
            return dest.stat().st_size
        req = urllib.request.Request(url, headers={"User-Agent": "canvas-archive/0.1"})
        with self._open(req, timeout=300) as r:
            total = 0
            with dest.open("wb") as f:
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    total += len(chunk)
            return total

    def gql(self, query: str, variables: dict | None = None) -> dict:
        """POST a GraphQL query. Canvas REST omits rich-text formatting from
        submission_comments, but /api/graphql returns htmlComment + comment
        attachments. Requires X-CSRF-Token from the browser session."""
        body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "canvas-archive/0.1",
        }
        if self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        req = urllib.request.Request(f"{self.base}/api/graphql", data=body, headers=headers)
        with self._open(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8"))


# ============================================================================
# Global error log (shared across all course runs in a single invocation)
# ============================================================================

# Path is set once main() determines the output root; every logged error
# (per-stage failures, per-course crashes) appends here. Singular `_error.log`
# per the user's spec.
_ERROR_LOG_PATH: Path | None = None


def init_error_log(output_root: Path) -> Path:
    """Create archive/_error.log and stamp it with a run-start banner."""
    global _ERROR_LOG_PATH
    output_root.mkdir(parents=True, exist_ok=True)
    _ERROR_LOG_PATH = output_root / "_error.log"
    with _ERROR_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"\n===== run started {_utc_now_iso()} =====\n")
    return _ERROR_LOG_PATH


def log_error(course_label: str, stage_name: str, exc: BaseException) -> None:
    """Append a one-line error record + traceback to the global error log."""
    msg = f"[{_utc_now_iso()}] {course_label} :: {stage_name} :: {type(exc).__name__}: {exc}"
    logger.warning("%s", msg)
    if _ERROR_LOG_PATH is None:
        return
    try:
        with _ERROR_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")
            f.write(traceback.format_exc())
            f.write("\n")
    except Exception:
        pass


@contextmanager
def stage(course_label: str, stage_name: str) -> Iterator[None]:
    """Per-stage context manager: log exceptions to the global _error.log and
    continue so the rest of the per-course archive still runs."""
    try:
        yield
    except Exception as ex:
        log_error(course_label, stage_name, ex)


def parse_next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        seg = part.strip()
        if 'rel="next"' in seg:
            return seg.split(";")[0].strip().lstrip("<").rstrip(">")
    return None


# ============================================================================
# Naming
# ============================================================================

_SAFE_RE = re.compile(r"[\x00-\x1F\x7F/\\?%*:|\"<>]")


def safe_name(s: str, max_len: int = 120) -> str:
    s = (s or "").strip()
    s = _SAFE_RE.sub("-", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    if len(s) > max_len:
        s = s[:max_len].rstrip(" .")
    return s or "_"


def _earliest_due_date(assignments: list[dict]) -> datetime | None:
    """Earliest due_at (falling back to unlock_at) across assignments, used to
    place a course in an academic term. Unparseable / missing dates are skipped."""
    earliest = None
    for a in assignments:
        due = a.get("due_at") or a.get("unlock_at")
        if not due:
            continue
        try:
            dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if earliest is None or dt < earliest:
            earliest = dt
    return earliest


# A term embedded in the title itself as "YYYY.N" or "YYYY-N" (e.g. the Korean
# "2015학년도 1학기" often written "2015.1"). Year must be 19xx/20xx; term 1-4.
_EMBEDDED_TERM_RE = re.compile(r"\b((?:19|20)\d{2})[.\-]([1-4])\b")


def _normalize_embedded_term(name: str) -> str | None:
    """Rewrite a title that already encodes its term as 'YYYY.N'/'YYYY-N' into the
    canonical '<base> (N) YYYY' form, trusting the title's own year and term.
    Returns None when no such token is present."""
    m = _EMBEDDED_TERM_RE.search(name)
    if not m:
        return None
    year, term = m.group(1), m.group(2)
    base = (name[:m.start()] + name[m.end():])
    base = re.sub(r"\s{2,}", " ", base).strip(" .-")
    label = f"{base} ({term}) {year}" if base else f"({term}) {year}"
    return safe_name(label)


def _split_trailing_year(name: str) -> tuple[str, int] | None:
    """If the title ends with a bare 'YYYY' (19xx/20xx) and no semester, return
    (base_without_year, year). The title's year is authoritative; only the term
    is missing and gets derived from the due dates. Returns None otherwise."""
    m = re.search(r"\s+((?:19|20)\d{2})\s*$", name)
    if not m:
        return None
    base = name[:m.start()].rstrip(" .-")
    return (base or name.strip()), int(m.group(1))


def derive_course_folder_name(course: dict, assignments: list[dict],
                              scheme: str = DEFAULT_TERM_SCHEME) -> str:
    name = course.get("name") or course.get("course_code") or f"course_{course.get('id')}"
    # Already carries an explicit "(N) YYYY" marker? Trust it, do not relabel.
    if re.search(r"\(\s*\d+\s*\)\s*\d{4}", name):
        return safe_name(name)
    # Title encodes the term as "YYYY.N"/"YYYY-N"? Normalize to canonical form.
    normalized = _normalize_embedded_term(name)
    if normalized is not None:
        return normalized
    if scheme == "none":
        return safe_name(name)

    table = TERM_SCHEMES.get(scheme) or TERM_SCHEMES[DEFAULT_TERM_SCHEME]
    earliest = _earliest_due_date(assignments)
    if earliest is None:
        return safe_name(name)

    term, year_offset = table.get(earliest.month, (1, 0))
    # Title already carries a bare year? Trust it and only add the term, instead
    # of appending a second, possibly-redundant year.
    trailing = _split_trailing_year(name)
    if trailing is not None:
        base, title_year = trailing
        return safe_name(f"{base} ({term}) {title_year}")
    return safe_name(f"{name} ({term}) {earliest.year + year_offset}")


def derive_section_short(section_name: str, course_name: str) -> str:
    """Strip everything up to and including the `(N) YYYY` marker in the
    section name itself, returning the suffix as the short label.
    Works even when the section name uses an abbreviation of the course
    name (e.g. course='Example Course (1) 2026',
    section='EC (1) 2026 F-3pm' -> 'F-3pm')."""
    m = re.search(r"\([12]\)\s*\d{4}\s+(.+)$", section_name)
    if m:
        return m.group(1).strip()
    return section_name


# ============================================================================
# JSON island + small HTML helpers
# ============================================================================

def html_doc(title: str, schema: str, body: str, head_extra: str = "",
             up_href: str | None = None, up_label: str | None = None) -> str:
    css = """
    body { font-family: -apple-system,Segoe UI,sans-serif; max-width: 900px;
           margin: 24px auto; padding: 0 16px; color: #2d3b45; line-height: 1.5; }
    h1,h2,h3 { color: #1c2024; }
    h1 { border-bottom: 2px solid #e0e3e6; padding-bottom: 8px; }
    section { margin: 24px 0; padding: 12px 16px; border-left: 3px solid #e0e3e6;
              background: #fafbfc; border-radius: 4px; }
    section h2 { margin-top: 0; font-size: 1.1em; color: #5a6b78; }
    .kv { display: grid; grid-template-columns: max-content 1fr; gap: 4px 16px;
          font-family: ui-monospace,monospace; font-size: 0.9em; }
    .kv dt { color: #888; }
    .kv dd { margin: 0; }
    .entry { margin: 10px 0; padding: 10px 14px; border-left: 3px solid #e0e3e6;
             background: #fafbfc; border-radius: 4px; }
    .entry .replies { margin-left: 24px; }
    .entry-author { font-weight: 600; }
    .entry-when { color: #888; font-size: 0.9em; margin-left: 8px; }
    .entry-deleted { color: #aaa; font-style: italic; }
    table { border-collapse: collapse; width: 100%; margin: 8px 0; }
    th,td { padding: 6px 10px; border-bottom: 1px solid #e0e3e6; text-align: left;
            font-size: 0.95em; }
    th { background: #f0f3f6; }
    .question { margin: 16px 0; padding: 12px 16px; border: 1px solid #e0e3e6;
                border-radius: 4px; }
    .answer { padding: 6px 10px; margin: 4px 0; border-left: 3px solid #ccc; }
    .answer.correct { border-left-color: #2e7d32; background: #e8f5e9; }
    .correct-tag { color: #2e7d32; font-weight: 600; margin-left: 6px; }
    a { color: #1976d2; }
    img { max-width: 100%; height: auto; }
    nav.up { font-size: 0.9em; margin-bottom: 12px; color: #5a6b78; }
    nav.up a { text-decoration: none; }
    nav.up a:hover { text-decoration: underline; }
    .badge { display: inline-block; padding: 1px 8px; border-radius: 10px;
             font-size: 0.8em; margin-left: 4px; }
    .badge.graded { background: #c8e6c9; color: #1b5e20; }
    .badge.submitted { background: #bbdefb; color: #0d47a1; }
    .badge.late { background: #ffe0b2; color: #e65100; }
    .badge.missing { background: #ffcdd2; color: #b71c1c; }
    .badge.excused { background: #d1c4e9; color: #311b92; }
    .badge.unsubmitted { background: #eceff1; color: #455a64; }
    .csv-link { font-size: 0.9em; }
    .roster th, .roster td { vertical-align: top; }
    .roster td.attachments { font-size: 0.85em; }
    .roster td.attachments a { display: block; }
    """
    up_html = ""
    if up_href is not None:
        label = html.escape(up_label or "up")
        up_html = (
            f'<nav class="up" data-up-href="{attr(up_href)}">'
            f'&larr; <a href="{attr(up_href)}">Up to {label}</a></nav>'
        )
    return (
        f'<!doctype html>\n<html lang="en" data-archive-schema="{attr(schema)}">\n<head>\n'
        f'<meta charset="utf-8"><title>{html.escape(title)}</title>\n'
        f'<style>{css}</style>\n{head_extra}\n</head><body>\n{up_html}{body}\n</body></html>\n'
    )


# ============================================================================
# Generic helpers used across renderers + index pages
# ============================================================================

@dataclass(frozen=True)
class IndexEntry:
    """One row on a folder index page. href is relative to the index file."""
    label: str
    href: str
    note: str = ""
    badge: str = ""


def render_index_doc(title: str, schema: str, intro_html: str,
                     entries: list[IndexEntry],
                     up_href: str | None, up_label: str | None,
                     extra_sections: str = "") -> str:
    parts = [f'<h1>{html.escape(title)}</h1>']
    if intro_html:
        parts.append(intro_html)
    if entries:
        parts.append('<section data-section="index"><ul class="index-list">')
        for e in entries:
            badge_html = f' <span class="badge {attr(e.badge)}">{html.escape(e.badge)}</span>' if e.badge else ""
            note_html = f' <span class="note">{html.escape(e.note)}</span>' if e.note else ""
            parts.append(
                f'<li><a href="{attr(e.href)}">{html.escape(e.label)}</a>'
                f'{badge_html}{note_html}</li>'
            )
        parts.append('</ul></section>')
    if extra_sections:
        parts.append(extra_sections)
    return html_doc(title, schema, "\n".join(parts), up_href=up_href, up_label=up_label)


def status_badge(sub: dict | None) -> str:
    if not sub:
        return "unsubmitted"
    if sub.get("excused"):
        return "excused"
    if sub.get("missing"):
        return "missing"
    if sub.get("late"):
        return "late"
    ws = sub.get("workflow_state") or ""
    if ws == "graded":
        return "graded"
    if ws in {"submitted", "pending_review"}:
        return "submitted"
    return ws or "unsubmitted"


def format_grading_weights_table(assignment_groups: list[dict],
                                  apply_weights: bool) -> str:
    """Render the assignment-group weights pulled from
    /api/v1/courses/<cid>/assignment_groups. Group weights only matter when
    the course has `apply_assignment_group_weights` enabled, otherwise the
    final grade is computed by raw points."""
    if not assignment_groups:
        return ""
    rows = []
    total_weight = 0.0
    for g in assignment_groups:
        w = g.get("group_weight") or 0
        try:
            total_weight += float(w or 0)
        except (TypeError, ValueError):
            pass
        rows.append((g.get("name") or f"group_{g.get('id')}", w, len(g.get("assignments") or [])))
    rows.sort(key=lambda r: -float(r[1] or 0))
    body = [
        '<section data-section="grading_weights" '
        f'data-apply-group-weights="{str(bool(apply_weights)).lower()}">'
        '<h2>Grading weights</h2>'
    ]
    if not apply_weights:
        body.append(
            '<p><em>This course is not configured to apply group weights '
            '(final grade computed by raw points). Weights below are for reference.</em></p>'
        )
    body.append('<table><thead><tr><th>Group</th><th>Weight</th><th>Assignments</th></tr></thead><tbody>')
    for name, weight, count in rows:
        weight_disp = f"{_fmt_pct(weight)}%"
        body.append(
            f'<tr data-group-name="{attr(name)}" data-weight="{attr(weight)}">'
            f'<td>{html.escape(name)}</td>'
            f'<td>{html.escape(weight_disp)}</td>'
            f'<td>{count}</td></tr>'
        )
    if apply_weights:
        body.append(
            f'<tr><th>Total</th><th>{_fmt_pct(total_weight)}%</th><th></th></tr>'
        )
    body.append('</tbody></table></section>')
    return "\n".join(body)


def fmt_iso(s: str | None) -> str:
    if not s:
        return ""
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return s


def attr(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _fmt_pct(value: Any) -> str:
    """Render a weight as a whole number when it has no fractional part, otherwise
    strip trailing zeros. 50 -> '50', 50.0 -> '50', 12.5 -> '12.5'."""
    try:
        f = float(value or 0)
    except (TypeError, ValueError):
        return "0"
    if f.is_integer():
        return str(int(f))
    return f"{f:g}"


def _fmt_points(value: Any) -> str:
    """Render a rubric/score point value. Unlike _fmt_pct this preserves a real
    zero (0.0 -> '0') and returns '' only for a genuinely absent value (None).
    2.0 -> '2', 0.0 -> '0', 1.5 -> '1.5'."""
    if value is None:
        return ""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if f.is_integer():
        return str(int(f))
    return f"{f:g}"


def rating_points_label(rating: dict, ratings: list[dict], use_range: bool) -> str:
    """Points label for one rubric rating tier. When the criterion uses point
    ranges, Canvas shows each tier as 'high to >low' (the low bound is the next
    lower tier's points, exclusive). Otherwise it is a single number. A genuine
    zero is preserved; only a missing value renders empty."""
    p = rating.get("points")
    if p is None:
        return ""
    if not use_range:
        return _fmt_points(p)
    try:
        pf = float(p)
    except (TypeError, ValueError):
        return _fmt_points(p)
    lowers = [float(o.get("points")) for o in ratings
              if o.get("points") is not None and float(o.get("points")) < pf]
    low = max(lowers) if lowers else 0.0
    return f"{_fmt_points(p)} to >{_fmt_points(low)}"


def is_published(item: dict | None) -> bool:
    """True when the item is published (i.e., visible to students). Falls back
    to workflow_state when the published flag is absent."""
    if not item:
        return True
    if "published" in item:
        return bool(item["published"])
    ws = item.get("workflow_state") or ""
    return ws not in {"unpublished", "deleted"}


def unpublished_suffix(item: dict | None) -> str:
    """Inline ' (unpublished)' marker for index labels when the item is not published."""
    return "" if is_published(item) else " (unpublished)"


# ============================================================================
# CSV writers
# ============================================================================

def write_students_csv(path: Path, students: list[dict], user_to_section: dict[str, str]) -> None:
    cols = ["student_id", "student_name", "sortable_name", "login_id", "email", "section"]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for s in sorted(students, key=lambda x: (x.get("sortable_name") or "")):
            uid = str(s.get("id") or "")
            w.writerow([
                uid,
                s.get("name") or "",
                s.get("sortable_name") or "",
                s.get("login_id") or "",
                s.get("email") or "",
                user_to_section.get(uid, ""),
            ])


# ---------------------------------------------------------------------------
# Student roster photo page
# ---------------------------------------------------------------------------

# Content-Type -> file extension. Canvas thumbnail avatar URLs carry no
# extension, so the response header is the authoritative source.
_AVATAR_CT_EXT = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
    "image/gif": ".gif", "image/webp": ".webp",
}


def _is_default_avatar(url: str | None) -> bool:
    """True when the URL is the generic Canvas placeholder (no uploaded photo)."""
    if not url:
        return True
    return "/images/messages/avatar" in url or "/images/dotted_pic" in url


def _avatar_initials(name: str | None) -> str:
    """One or two uppercase initials for the no-photo placeholder."""
    parts = [p for p in re.split(r"\s+", (name or "").strip()) if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _download_one_avatar(api: Canvas, url: str, dest_dir: Path, student_id: str) -> str | None:
    """Download a single avatar. Returns the saved filename (within dest_dir) or None."""
    req = urllib.request.Request(url, headers={"User-Agent": "canvas-archive/0.1"})
    with api._open(req, timeout=120) as r:
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        data = r.read()
    if not data:
        return None
    ext = _AVATAR_CT_EXT.get(ctype)
    if ext is None:
        suf = Path(urllib.parse.urlparse(url).path).suffix.lower()
        ext = suf if suf in {".png", ".jpg", ".jpeg", ".gif", ".webp"} else ".jpg"
    dest = dest_dir / f"{safe_name(student_id)}{ext}"
    dest.write_bytes(data)
    return dest.name


def archive_student_avatars(api: Canvas, students: list[dict],
                            course_dir: Path, workers: int = HTTP_WORKERS) -> dict[str, str]:
    """Download non-default student profile photos into Students/avatars/.
    Returns {student_id: path relative to the Students/ folder}. Students on the
    Canvas default avatar are skipped and rendered as initials instead."""
    real = [(str(s.get("id")), s.get("avatar_url") or "")
            for s in students if not _is_default_avatar(s.get("avatar_url"))]
    if not real:
        return {}
    avatar_dir = course_dir / "Students" / "avatars"
    avatar_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, str] = {}

    def _work(item: tuple[str, str]) -> tuple[str, str | None]:
        sid, url = item
        try:
            return sid, _download_one_avatar(api, url, avatar_dir, sid)
        except (urllib.error.URLError, OSError) as ex:
            logger.warning("avatar download failed for student %s: %s", sid, ex)
            return sid, None

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(real)))) as pool:
        for sid, fname in pool.map(_work, real):
            if fname:
                result[sid] = f"avatars/{fname}"
    logger.info("  %d/%d student photos downloaded", len(result), len(real))
    return result


_STUDENTS_CSS = """
.student-grid { list-style: none; padding: 0; display: grid;
  grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 14px; }
.s-card { border: 1px solid #e0e3e6; border-radius: 8px; padding: 14px;
  background: #fff; text-align: center; }
.s-card .avatar { width: 96px; height: 96px; border-radius: 50%;
  object-fit: cover; margin: 0 auto 8px; display: block; background: #f0f3f6; }
.s-card .avatar.placeholder { display: flex; align-items: center;
  justify-content: center; font-size: 1.8em; font-weight: 600; color: #5a6b78; }
.s-name { font-weight: 600; }
.s-sec, .s-login, .s-email { font-size: 0.85em; color: #5a6b78; word-break: break-word; }
"""


def render_students_html(course_name: str, students: list[dict],
                         user_to_section: dict[str, str],
                         avatar_paths: dict[str, str]) -> str:
    """Roster page (Students/Students.html) showing each student's profile photo
    (or initials), name, section, login, and email. Includes a machine-readable
    JSON island and per-card data-* attributes."""
    rows = sorted(students, key=lambda x: (x.get("sortable_name") or x.get("name") or ""))
    cards: list[str] = []
    island_rows: list[dict] = []
    for s in rows:
        uid = str(s.get("id") or "")
        name = (s.get("name") or "").strip()
        sec = user_to_section.get(uid, "")
        email = s.get("email") or ""
        login = s.get("login_id") or ""
        rel = avatar_paths.get(uid)
        if rel:
            img = (f'<img class="avatar" src="{attr(urllib.parse.quote(rel))}" '
                   f'alt="{attr(name)}" loading="lazy">')
        else:
            img = (f'<div class="avatar placeholder" aria-hidden="true">'
                   f'{html.escape(_avatar_initials(name))}</div>')
        meta = []
        if sec:
            meta.append(f'<div class="s-sec">{html.escape(sec)}</div>')
        # Canvas login_id is usually the email; don't print it twice. Keep the
        # login line only when it differs from the (hotlinked) email below.
        if login and login.strip().lower() != email.strip().lower():
            meta.append(f'<div class="s-login">{html.escape(login)}</div>')
        if email:
            meta.append(f'<div class="s-email">'
                        f'<a href="mailto:{attr(email)}">{html.escape(email)}</a></div>')
        cards.append(
            f'<li class="s-card" data-student-id="{attr(uid)}" '
            f'data-sortable-name="{attr(s.get("sortable_name") or "")}" '
            f'data-section="{attr(sec)}" data-login-id="{attr(login)}" '
            f'data-email="{attr(email)}" data-has-photo="{str(bool(rel)).lower()}">'
            f'{img}<div class="s-name">{html.escape(name)}</div>{"".join(meta)}</li>'
        )
        island_rows.append({
            "id": uid, "name": name,
            "sortable_name": s.get("sortable_name") or "",
            "login_id": login, "email": email, "section": sec,
            "avatar_url": s.get("avatar_url") or "", "local_avatar": rel or "",
        })
    island = ('<script type="application/json" data-section="students">'
              + html.escape(json.dumps(island_rows, default=str), quote=False)
              + '</script>')
    body = (
        f'<h1 data-course-name="{attr(course_name)}">{html.escape(course_name)} - Students '
        f'<small>({len(rows)})</small></h1>\n'
        f'<p class="csv-link"><a href="../Students.csv">Students.csv</a> '
        f'(machine-readable roster)</p>\n'
        f'<ul class="student-grid">\n' + "\n".join(cards) + '\n</ul>\n' + island
    )
    return html_doc(
        f"{course_name} - Students",
        "canvas-archive/students/v1",
        body,
        head_extra=f"<style>{_STUDENTS_CSS}</style>",
        up_href="../index.html",
        up_label=course_name,
    )


def write_assignments_csv(path: Path, assignments: list[dict]) -> None:
    cols = ["assignment_id", "name", "points_possible", "due_at", "submission_types",
            "grading_type", "published", "position"]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for a in sorted(assignments, key=lambda x: (x.get("position") or 0)):
            w.writerow([
                a.get("id"), a.get("name") or "",
                a.get("points_possible"),
                a.get("due_at") or "",
                "|".join(a.get("submission_types") or []),
                a.get("grading_type") or "",
                bool(a.get("published")),
                a.get("position"),
            ])


def write_gradebook(out_dir: Path, students: list[dict], assignments: list[dict],
                    subs_by_student_assignment: dict[tuple[str, str], dict]) -> None:
    students_sorted = sorted(students, key=lambda x: (x.get("sortable_name") or ""))
    assignments_sorted = sorted(assignments, key=lambda x: (x.get("position") or 0))

    wide_path = out_dir / "Gradebook.csv"
    with wide_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["student_id", "student_name"] + [a.get("name") or "" for a in assignments_sorted])
        for s in students_sorted:
            uid = str(s.get("id"))
            row = [uid, s.get("name") or ""]
            for a in assignments_sorted:
                sub = subs_by_student_assignment.get((uid, str(a.get("id"))))
                row.append(sub.get("score") if sub else "")
            w.writerow(row)


def write_section_grades_csv(path: Path, assignment: dict, rows: list[dict]) -> None:
    cols = ["student_id", "student_name", "score", "grade", "submitted_at",
            "workflow_state", "late", "missing", "excused", "attempt"]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in sorted(rows, key=lambda x: (x.get("student_name") or "")):
            w.writerow([r.get(c, "") for c in cols])


# ============================================================================
# Submission HTML rendering
# ============================================================================

def render_submission_html(course_name: str, assignment: dict, student: dict,
                            submission: dict, attachment_filenames: list[str],
                            local_paths: dict[tuple[str, str], str],
                            depth_to_root: int,
                            rich_comments: list[dict] | None = None,
                            up_href: str = "",
                            up_label: str = "") -> str:
    rubric_assessment = submission.get("rubric_assessment") or {}
    rubric_criteria = assignment.get("rubric") or []
    rubric_total = 0.0
    for crit in rubric_criteria:
        crit_id = crit.get("id")
        if crit_id in rubric_assessment:
            pts = rubric_assessment[crit_id].get("points")
            if isinstance(pts, (int, float)):
                rubric_total += pts

    # Prefer GraphQL rich comments (preserve formatting + carry attachment
    # filenames downloaded into the same folder). Fall back to REST comments
    # when GraphQL is unavailable.
    rest_comments = submission.get("submission_comments") or []
    if rich_comments is not None:
        structured_comments = rich_comments
    else:
        structured_comments = [
            {"author": c.get("author_name") or "",
             "author_id": str(c.get("author_id")) if c.get("author_id") is not None else None,
             "created_at": c.get("created_at"),
             "comment": c.get("comment") or "",
             "html_comment": "",
             "attachments": []}
            for c in rest_comments
        ]

    structured_rubric = []
    for crit in rubric_criteria:
        crit_id = crit.get("id")
        assess = rubric_assessment.get(crit_id) or {}
        rating_id = assess.get("rating_id")
        rating_desc = ""
        rating_pts = None
        for r in (crit.get("ratings") or []):
            if r.get("id") == rating_id:
                rating_desc = r.get("description") or ""
                rating_pts = r.get("points")
                break
        structured_rubric.append({
            "criterion_id": crit_id,
            "description": crit.get("description") or "",
            "long_description": crit.get("long_description") or "",
            "points_possible": crit.get("points"),
            "points_awarded": assess.get("points"),
            "rating_id": rating_id,
            "rating_description": rating_desc,
            "rating_points": rating_pts,
            "use_range": bool(crit.get("criterion_use_range")),
            "ratings_scale": [
                {"id": r.get("id"), "description": r.get("description") or "",
                 "long_description": r.get("long_description") or "",
                 "points": r.get("points")}
                for r in (crit.get("ratings") or [])
            ],
            "comments": assess.get("comments") or "",
        })

    submission_types = assignment.get("submission_types") or []
    flags = []
    if submission.get("late"): flags.append("late")
    if submission.get("missing"): flags.append("missing")
    if submission.get("excused"): flags.append("excused")

    parts = [
        f'<h1 data-assignment-id="{attr(assignment.get("id"))}" '
        f'data-assignment-name="{attr(assignment.get("name"))}" '
        f'data-student-id="{attr(student.get("id"))}" '
        f'data-student-name="{attr(student.get("name"))}" '
        f'data-student-sortable-name="{attr(student.get("sortable_name"))}" '
        f'data-course-id="{attr(submission.get("course_id"))}" '
        f'data-course-name="{attr(course_name)}">'
        f'{html.escape(student.get("name") or "")} &mdash; {html.escape(assignment.get("name") or "")}</h1>'
    ]

    parts.append(
        f'<section data-section="meta" '
        f'data-submission-id="{attr(submission.get("id"))}" '
        f'data-submission-types="{attr(",".join(submission_types))}" '
        f'data-attempt="{attr(submission.get("attempt"))}" '
        f'data-flag-late="{str(bool(submission.get("late"))).lower()}" '
        f'data-flag-missing="{str(bool(submission.get("missing"))).lower()}" '
        f'data-flag-excused="{str(bool(submission.get("excused"))).lower()}">'
        f'\n<h2>Submission</h2>\n<dl class="kv">'
    )
    score = submission.get("score")
    points = assignment.get("points_possible")
    score_str = f"{score} / {points}" if score is not None else "(no score)"
    parts.append(f'<dt>Score</dt><dd data-score="{attr(score)}" data-points-possible="{attr(points)}">{html.escape(score_str)}</dd>')
    parts.append(f'<dt>Grade</dt><dd data-grade="{attr(submission.get("grade"))}">{html.escape(str(submission.get("grade") or ""))}</dd>')
    parts.append(f'<dt>Submitted</dt><dd data-submitted-at="{attr(submission.get("submitted_at"))}">{html.escape(fmt_iso(submission.get("submitted_at")))}</dd>')
    parts.append(f'<dt>State</dt><dd data-workflow-state="{attr(submission.get("workflow_state"))}">{html.escape(str(submission.get("workflow_state") or ""))}</dd>')
    parts.append(f'<dt>Submission types</dt><dd>{html.escape(", ".join(submission_types))}</dd>')
    if flags:
        parts.append(f'<dt>Flags</dt><dd>{", ".join(flags)}</dd>')
    parts.append('</dl></section>')

    body_html = submission.get("body")
    if body_html:
        body_html = rewrite_canvas_html(body_html, local_paths, depth_to_root)
        parts.append(f'<section data-section="body"><h2>Body</h2>\n{body_html}\n</section>')

    discussion_entries = submission.get("discussion_entries") or []
    if discussion_entries:
        parts.append('<section data-section="discussion_entries"><h2>Discussion posts</h2>')
        for de in discussion_entries:
            de_msg = rewrite_canvas_html(de.get("message") or "", local_paths, depth_to_root)
            parts.append(
                f'<div class="entry" data-entry-id="{attr(de.get("id"))}" '
                f'data-parent-id="{attr(de.get("parent_id"))}" '
                f'data-created="{attr(de.get("created_at"))}" '
                f'data-updated="{attr(de.get("updated_at"))}">'
                f'<div><span class="entry-when">{html.escape(fmt_iso(de.get("created_at")))}</span></div>'
                f'<div>{de_msg or "<em>[deleted]</em>"}</div></div>'
            )
        parts.append('</section>')

    url = submission.get("url")
    if url:
        parts.append(f'<section data-section="url_submission"><h2>URL Submission</h2>'
                     f'<p><a href="{attr(url)}" data-submitted-url="{attr(url)}">{html.escape(url)}</a></p></section>')

    if attachment_filenames:
        parts.append('<section data-section="attachments"><h2>Attachments</h2><ul>')
        for fn in attachment_filenames:
            parts.append(
                f'<li data-attachment-file="{attr(fn)}">'
                f'<a href="{attr(urllib.parse.quote(fn))}">{html.escape(fn)}</a></li>'
            )
        parts.append('</ul></section>')

    if structured_comments:
        parts.append('<section data-section="comments" '
                     f'data-comment-count="{len(structured_comments)}"><h2>Comments</h2>')
        for c in structured_comments:
            html_body = c.get("html_comment") or ""
            if not html_body and c.get("comment"):
                html_body = f"<p>{html.escape(c['comment'])}</p>"
            atts_html = ""
            cms_atts = c.get("attachments") or []
            if cms_atts:
                atts_html_parts = ['<div class="comment-attachments"><strong>Attachments:</strong><ul>']
                for ca in cms_atts:
                    fn = ca.get("local_filename") or ca.get("display_name") or "attachment"
                    atts_html_parts.append(
                        f'<li data-comment-attachment-id="{attr(ca.get("attachment_id"))}" '
                        f'data-display-name="{attr(ca.get("display_name"))}" '
                        f'data-content-type="{attr(ca.get("content_type"))}" '
                        f'data-size="{attr(ca.get("size"))}">'
                        f'<a href="{attr(urllib.parse.quote(fn))}">{html.escape(fn)}</a></li>'
                    )
                atts_html_parts.append('</ul></div>')
                atts_html = "".join(atts_html_parts)
            parts.append(
                f'<div class="entry" data-author="{attr(c.get("author"))}" '
                f'data-author-id="{attr(c.get("author_id"))}" '
                f'data-comment-id="{attr(c.get("comment_id"))}" '
                f'data-created="{attr(c.get("created_at"))}">'
                f'<div><span class="entry-author">{html.escape(c.get("author") or "")}</span>'
                f'<span class="entry-when">{html.escape(fmt_iso(c.get("created_at")))}</span></div>'
                f'<div class="comment-body">{html_body}</div>'
                f'{atts_html}</div>'
            )
        parts.append('</section>')

    if structured_rubric:
        parts.append(f'<section data-section="rubric" data-rubric-total="{attr(rubric_total)}"><h2>Rubric</h2>')
        parts.append('<table><thead><tr><th>Criterion</th><th>Points</th><th>Rating</th><th>Comments</th></tr></thead><tbody>')
        for r in structured_rubric:
            pts_aw = r["points_awarded"]
            pts_po = r["points_possible"]
            pts_cell = ""
            if pts_aw is not None or pts_po is not None:
                pts_cell = f'{_fmt_points(pts_aw) if pts_aw is not None else "-"} / {_fmt_points(pts_po) if pts_po is not None else "-"}'
            rating_cell = r["rating_description"] or ""
            if r.get("use_range") and r["rating_points"] is not None:
                range_label = rating_points_label(
                    {"points": r["rating_points"]}, r["ratings_scale"], True)
                rating_cell = f"{rating_cell} ({range_label})".strip()
            applied_id = r["rating_id"]
            alt_spans = []
            for tier in r["ratings_scale"]:
                is_applied = tier["id"] == applied_id and applied_id is not None
                alt_spans.append(
                    f'<span class="alt-rating" data-rating-id="{attr(tier["id"])}" '
                    f'data-rating-description="{attr(tier["description"])}" '
                    f'data-rating-long-description="{attr(tier["long_description"])}" '
                    f'data-rating-points="{attr(tier["points"])}" '
                    f'data-rating-applied="{str(is_applied).lower()}"></span>'
                )
            alt_block = ""
            if alt_spans:
                alt_block = f'<span class="ratings-scale" hidden>{"".join(alt_spans)}</span>'
            parts.append(
                f'<tr data-criterion-id="{attr(r["criterion_id"])}" '
                f'data-long-description="{attr(r["long_description"])}" '
                f'data-points-awarded="{attr(pts_aw)}" '
                f'data-points-possible="{attr(pts_po)}" '
                f'data-rating-id="{attr(applied_id)}" '
                f'data-rating-points="{attr(r["rating_points"])}">'
                f'<td>{html.escape(r["description"])}{alt_block}</td>'
                f'<td>{html.escape(pts_cell)}</td>'
                f'<td>{html.escape(rating_cell)}</td>'
                f'<td>{html.escape(r["comments"])}</td></tr>'
            )
        parts.append('</tbody></table></section>')

    # Audit copy: embed the raw submission JSON as a script-tag island so the
    # archive remains machine-parseable without writing a separate _<Student>.json.
    raw_island = (
        '<script type="application/json" data-section="raw_submission">'
        + html.escape(json.dumps(submission, default=str), quote=False)
        + '</script>'
    )
    parts.append(raw_island)

    return html_doc(
        f"{student.get('name') or ''} - {assignment.get('name') or ''}",
        SCHEMA_SUBMISSION,
        "\n".join(parts),
        up_href=up_href or "../" + safe_name(assignment.get("name") or "") + ".html",
        up_label=up_label or (assignment.get("name") or "Assignment"),
    )


@dataclass(frozen=True)
class StudentRow:
    """One row per student per assignment for the roster table on
    <aname>.html. Section is the short label (or '' when not split).
    attachments / comment_attachments are filenames relative to the section
    folder (or the assignment folder when not split)."""
    section: str
    student_id: str
    student_name: str
    sortable_name: str
    submission_html: str  # href relative to <aname>.html
    score: Any
    points_possible: Any
    badge: str
    submitted_at: str
    attempt: Any
    attachments: tuple[str, ...]
    comment_attachments: tuple[str, ...]


def render_assignment_overview_html(course_name: str, assignment: dict,
                                    local_paths: dict[tuple[str, str], str],
                                    student_rows: list[StudentRow] | None = None,
                                    section_grades_csv: dict[str, str] | None = None) -> str:
    """Page at Assignments/<assignment>/<assignment>.html. Depth-to-root = 2.

    section_grades_csv maps short section label (or '') -> href to that
    section's _grades.csv, relative to the overview HTML."""
    rubric_criteria = assignment.get("rubric") or []
    sub_types = assignment.get("submission_types") or []
    description = rewrite_canvas_html(assignment.get("description") or "", local_paths, 2)

    parts = [
        f'<h1 data-assignment-id="{attr(assignment.get("id"))}" '
        f'data-assignment-name="{attr(assignment.get("name"))}" '
        f'data-published="{str(bool(assignment.get("published"))).lower()}" '
        f'data-course-name="{attr(course_name)}">'
        f'{html.escape(assignment.get("name") or "")}</h1>'
    ]
    parts.append(
        f'<section data-section="meta" '
        f'data-points-possible="{attr(assignment.get("points_possible"))}" '
        f'data-due-at="{attr(assignment.get("due_at"))}" '
        f'data-unlock-at="{attr(assignment.get("unlock_at"))}" '
        f'data-lock-at="{attr(assignment.get("lock_at"))}" '
        f'data-grading-type="{attr(assignment.get("grading_type"))}" '
        f'data-submission-types="{attr(",".join(sub_types))}">'
        f'<dl class="kv">'
        f'<dt>Points possible</dt><dd>{html.escape(str(assignment.get("points_possible") or ""))}</dd>'
        f'<dt>Due</dt><dd>{html.escape(fmt_iso(assignment.get("due_at")))}</dd>'
        f'<dt>Unlock</dt><dd>{html.escape(fmt_iso(assignment.get("unlock_at")))}</dd>'
        f'<dt>Lock</dt><dd>{html.escape(fmt_iso(assignment.get("lock_at")))}</dd>'
        f'<dt>Grading type</dt><dd>{html.escape(str(assignment.get("grading_type") or ""))}</dd>'
        f'<dt>Submission types</dt><dd>{html.escape(", ".join(sub_types))}</dd>'
        f'<dt>Published</dt><dd>{str(bool(assignment.get("published"))).lower()}</dd>'
        f'</dl></section>'
    )
    if description:
        parts.append(
            f'<section data-section="description"><h2>Description</h2>{description}</section>'
        )

    if rubric_criteria:
        total_points = sum((c.get("points") or 0) for c in rubric_criteria)
        parts.append(
            f'<section data-section="rubric" data-rubric-total="{attr(total_points)}">'
            f'<h2>Rubric</h2>'
        )
        parts.append(
            '<table><thead><tr><th>Criterion</th><th>Pts</th>'
            '<th>Rating tiers (points: label)</th></tr></thead><tbody>'
        )
        for crit in rubric_criteria:
            ratings = crit.get("ratings") or []
            use_range = bool(crit.get("criterion_use_range"))
            tier_parts = []
            for r in ratings:
                pts_label = rating_points_label(r, ratings, use_range)
                tier_parts.append(
                    f'<div class="alt-rating" data-rating-id="{attr(r.get("id"))}" '
                    f'data-rating-points="{attr(r.get("points"))}" '
                    f'data-rating-use-range="{str(use_range).lower()}" '
                    f'data-rating-description="{attr(r.get("description"))}" '
                    f'data-rating-long-description="{attr(r.get("long_description"))}">'
                    f'<strong>{html.escape(pts_label)}</strong>: '
                    f'{html.escape(r.get("description") or "")}'
                    + (f'<div class="rating-long">{html.escape(r.get("long_description") or "")}</div>'
                       if r.get("long_description") else "")
                    + '</div>'
                )
            parts.append(
                f'<tr data-criterion-id="{attr(crit.get("id"))}" '
                f'data-points-possible="{attr(crit.get("points"))}" '
                f'data-use-range="{str(use_range).lower()}" '
                f'data-long-description="{attr(crit.get("long_description"))}">'
                f'<td><strong>{html.escape(crit.get("description") or "")}</strong>'
                + (f'<div class="crit-long">{html.escape(crit.get("long_description") or "")}</div>'
                   if crit.get("long_description") else "")
                + f'</td>'
                f'<td>{html.escape(_fmt_points(crit.get("points")))}</td>'
                f'<td>{"".join(tier_parts)}</td></tr>'
            )
        parts.append('</tbody></table></section>')

    if student_rows:
        # Group rows by section label (sorted: empty -> first, then alpha).
        by_section: dict[str, list[StudentRow]] = {}
        for r in student_rows:
            by_section.setdefault(r.section, []).append(r)
        for sec in by_section:
            by_section[sec].sort(key=lambda r: (r.sortable_name or r.student_name).lower())
        section_keys = sorted(by_section.keys(), key=lambda s: (s != "", s.lower()))
        points = assignment.get("points_possible")
        for sec in section_keys:
            rows = by_section[sec]
            sec_label = sec or "All students"
            csv_href = (section_grades_csv or {}).get(sec)
            parts.append(
                f'<section data-section="roster" data-section-label="{attr(sec)}" '
                f'data-row-count="{len(rows)}">'
                f'<h2>{html.escape(sec_label)} <small>({len(rows)} students)</small></h2>'
            )
            if csv_href:
                parts.append(
                    f'<p class="csv-link" data-grades-csv="{attr(csv_href)}">'
                    f'Grades: <a href="{attr(csv_href)}">_grades.csv</a></p>'
                )
            parts.append(
                '<table class="roster"><thead><tr>'
                '<th>Student</th><th>Score</th><th>Status</th>'
                '<th>Attempt</th><th>Submitted</th><th>Files</th>'
                '</tr></thead><tbody>'
            )
            for r in rows:
                score_disp = (f"{r.score}" if r.score is not None else "-")
                if points is not None:
                    score_disp += f" / {points}"
                # Build hrefs RELATIVE to the assignment overview HTML. The
                # submission_html stored on StudentRow is already URL-quoted; for
                # files we URL-quote the filename and prepend the same section
                # prefix (if any).
                sec_prefix = ""
                if "/" in r.submission_html:
                    sec_prefix = r.submission_html.rsplit("/", 1)[0] + "/"
                att_links: list[str] = []
                for fn in r.attachments:
                    att_links.append(
                        f'<a href="{attr(sec_prefix + urllib.parse.quote(fn))}" '
                        f'data-attachment="submission">{html.escape(fn)}</a>'
                    )
                for fn in r.comment_attachments:
                    att_links.append(
                        f'<a href="{attr(sec_prefix + urllib.parse.quote(fn))}" '
                        f'data-attachment="comment">{html.escape(fn)} '
                        f'<small>(comment)</small></a>'
                    )
                att_cell = "".join(att_links) or "<em>-</em>"
                parts.append(
                    f'<tr data-student-id="{attr(r.student_id)}" '
                    f'data-student-name="{attr(r.student_name)}" '
                    f'data-sortable-name="{attr(r.sortable_name)}" '
                    f'data-score="{attr(r.score)}" '
                    f'data-points-possible="{attr(points)}" '
                    f'data-status="{attr(r.badge)}" '
                    f'data-attempt="{attr(r.attempt)}" '
                    f'data-submitted-at="{attr(r.submitted_at)}">'
                    f'<td><a href="{attr(r.submission_html)}">{html.escape(r.student_name)}</a></td>'
                    f'<td>{html.escape(score_disp)}</td>'
                    f'<td><span class="badge {attr(r.badge)}">{html.escape(r.badge)}</span></td>'
                    f'<td>{html.escape(str(r.attempt) if r.attempt is not None else "")}</td>'
                    f'<td>{html.escape(fmt_iso(r.submitted_at))}</td>'
                    f'<td class="attachments">{att_cell}</td>'
                    f'</tr>'
                )
            parts.append('</tbody></table></section>')

    return html_doc(
        f"{assignment.get('name') or 'assignment'} - overview",
        "canvas-archive/assignment/v1",
        "\n".join(parts),
        up_href="../index.html",
        up_label="Assignments",
    )


def render_assignments_index_html(course_name: str,
                                  assignment_groups: list[dict],
                                  assignments: list[dict],
                                  local_paths: dict[tuple[str, str], str],
                                  apply_weights: bool,
                                  include_unpublished: bool) -> str:
    """Assignments/index.html mirroring the Canvas Assignments page: assignments
    grouped by assignment group, each group showing its weight (when the course
    applies group weights), every assignment listed in Canvas order with an
    '(unpublished)' marker, and a link to the local overview page. Lives at
    Assignments/index.html, so hrefs drop the leading 'Assignments/'."""
    by_id = {str(a.get("id")): a for a in assignments}

    def _href(aid: str) -> str | None:
        rel = local_paths.get(("Assignment", str(aid)))
        if not rel:
            return None
        # rel is course-root-relative ("Assignments/<n>/<n>.html"); this page is
        # already inside Assignments/, so strip that prefix.
        inner = rel[len("Assignments/"):] if rel.startswith("Assignments/") else rel
        return _quote_path(inner)

    groups = sorted(assignment_groups, key=lambda g: (g.get("position") or 0))
    parts: list[str] = [
        f'<h1 data-course-name="{attr(course_name)}">Assignments</h1>'
    ]
    if apply_weights:
        parts.append(
            '<p><em>Final grade is computed from weighted assignment groups. '
            'Group weights are shown below.</em></p>'
        )
    else:
        parts.append(
            '<p><em>This course is not configured to apply group weights '
            '(final grade computed by raw points). Weights below are for reference.</em></p>'
        )

    total_weight = 0.0
    for g in groups:
        gname = g.get("name") or f"group_{g.get('id')}"
        gweight = g.get("group_weight") or 0
        try:
            total_weight += float(gweight or 0)
        except (TypeError, ValueError):
            pass
        weight_disp = f"{_fmt_pct(gweight)}%"
        # Prefer the authoritative full assignment record; fall back to the
        # lighter copy embedded in the group.
        g_assignments = sorted(
            (g.get("assignments") or []),
            key=lambda a: (a.get("position") or 0),
        )
        rows: list[str] = []
        listed = 0
        for ga in g_assignments:
            aid = str(ga.get("id"))
            a = by_id.get(aid, ga)
            published = is_published(a)
            if not published and not include_unpublished:
                continue
            listed += 1
            name = a.get("name") or f"assignment_{aid}"
            suffix = "" if published else " (unpublished)"
            href = _href(aid)
            if href:
                name_cell = f'<a href="{attr(href)}">{html.escape(name)}</a>{suffix}'
            else:
                name_cell = f'{html.escape(name)}{suffix}'
            pts = a.get("points_possible")
            due = fmt_iso(a.get("due_at"))
            rows.append(
                f'<tr data-assignment-id="{attr(aid)}" '
                f'data-published="{str(bool(published)).lower()}" '
                f'data-points-possible="{attr(pts)}" '
                f'data-due-at="{attr(a.get("due_at"))}">'
                f'<td>{name_cell}</td>'
                f'<td>{html.escape(_fmt_points(pts))}</td>'
                f'<td>{html.escape(due)}</td></tr>'
            )
        header = html.escape(gname)
        if apply_weights or gweight:
            header += f' &middot; {html.escape(weight_disp)}'
        section = [
            f'<section data-section="assignment_group" '
            f'data-group-id="{attr(g.get("id"))}" '
            f'data-group-name="{attr(gname)}" '
            f'data-group-weight="{attr(gweight)}">'
            f'<h2>{header}</h2>'
        ]
        if rows:
            section.append(
                '<table><thead><tr><th>Assignment</th><th>Points</th>'
                '<th>Due</th></tr></thead><tbody>'
            )
            section.extend(rows)
            section.append('</tbody></table>')
        else:
            section.append('<p><em>No assignments.</em></p>')
        section.append('</section>')
        parts.append("\n".join(section))

    if apply_weights:
        parts.append(
            f'<section data-section="weights_total">'
            f'<p><strong>Total weight:</strong> {html.escape(_fmt_pct(total_weight))}%</p>'
            f'</section>'
        )

    island = (
        '<script type="application/json" data-section="assignments_index">'
        + html.escape(json.dumps(
            {"apply_group_weights": bool(apply_weights),
             "assignment_groups": assignment_groups},
            default=str), quote=False)
        + '</script>'
    )
    parts.append(island)

    return html_doc(
        f"{course_name} - Assignments",
        "canvas-archive/assignments-index/v1",
        "\n".join(parts),
        up_href="../index.html",
        up_label=f"{course_name} index",
    )


# ============================================================================
# Discussion HTML rendering
# ============================================================================

def collect_entries(view_entries: list[dict]) -> Iterable[dict]:
    for e in view_entries:
        yield e
        yield from collect_entries(e.get("replies") or [])


def filter_entries_by_users(view_entries: list[dict], allowed_user_ids: set[str]) -> list[dict]:
    """Return a deep-filtered copy of view_entries keeping only entries whose
    user_id is in allowed_user_ids. A reply is kept if its author is allowed
    (independent of parent). Deleted entries (no user_id) are dropped from
    section-split views."""
    out = []
    for e in view_entries:
        kids = filter_entries_by_users(e.get("replies") or [], allowed_user_ids)
        uid = str(e.get("user_id") or "")
        if uid in allowed_user_ids:
            new = dict(e)
            new["replies"] = kids
            out.append(new)
        else:
            # parent excluded; lift the kept replies up so they aren't lost
            out.extend(kids)
    return out


def render_discussion_entry(entry: dict, participants_by_id: dict,
                            local_paths: dict[tuple[str, str], str],
                            depth: int = 0) -> str:
    uid = str(entry.get("user_id") or "")
    author = participants_by_id.get(uid, {}).get("display_name") or entry.get("user_name") or f"user {uid}"
    when = fmt_iso(entry.get("created_at") or entry.get("updated_at"))
    msg = rewrite_canvas_html(entry.get("message") or "", local_paths, 1)
    parts = [f'<div class="entry" data-entry-id="{attr(entry.get("id"))}" '
             f'data-author-id="{attr(uid)}" data-parent-id="{attr(entry.get("parent_id"))}" '
             f'data-created="{attr(entry.get("created_at"))}" data-depth="{depth}">']
    parts.append(
        f'<div><span class="entry-author">{html.escape(author)}</span>'
        f'<span class="entry-when">{html.escape(when)}</span></div>'
    )
    if not msg:
        parts.append('<div class="entry-deleted">[deleted]</div>')
    else:
        parts.append(f'<div>{msg}</div>')
    rating = entry.get("rating_count")
    if rating:
        parts.append(f'<div data-rating-count="{attr(rating)}">👍 {rating}</div>')
    replies = entry.get("replies") or []
    if replies:
        parts.append('<div class="replies">')
        for r in replies:
            parts.append(render_discussion_entry(r, participants_by_id, local_paths, depth + 1))
        parts.append('</div>')
    parts.append('</div>')
    return "\n".join(parts)


def render_discussion_html(course_name: str, topic: dict, view: dict,
                           section_label: str | None,
                           filtered_entries: list[dict],
                           participants_by_id: dict,
                           local_paths: dict[tuple[str, str], str],
                           grades_csv_href: str | None = None) -> str:
    all_entries = list(collect_entries(filtered_entries))
    unique_posters = len({str(e.get("user_id") or "") for e in all_entries if e.get("user_id")})
    title_suffix = f" - {section_label}" if section_label else ""

    parts = [
        f'<h1 data-topic-id="{attr(topic.get("id"))}" '
        f'data-topic-title="{attr(topic.get("title"))}" '
        f'data-assignment-id="{attr(topic.get("assignment_id"))}" '
        f'data-course-name="{attr(course_name)}" '
        f'data-section="{attr(section_label)}">'
        f'{html.escape(topic.get("title") or "")}{html.escape(title_suffix)}</h1>'
    ]
    parts.append(
        f'<section data-section="topic_meta" '
        f'data-total-entries="{len(all_entries)}" '
        f'data-top-level-entries="{len(filtered_entries)}" '
        f'data-unique-posters="{unique_posters}">'
        f'<dl class="kv">'
        f'<dt>Author</dt><dd data-author="{attr(topic.get("user_name"))}">{html.escape(topic.get("user_name") or "")}</dd>'
        f'<dt>Posted</dt><dd data-posted-at="{attr(topic.get("posted_at"))}">{html.escape(fmt_iso(topic.get("posted_at")))}</dd>'
        f'<dt>Top-level entries</dt><dd>{len(filtered_entries)}</dd>'
        f'<dt>Total (incl. replies)</dt><dd>{len(all_entries)}</dd>'
        f'</dl></section>'
    )
    if grades_csv_href:
        parts.append(
            f'<section data-section="grades_csv" data-grades-csv="{attr(grades_csv_href)}">'
            f'<h2>Grades</h2>'
            f'<p class="csv-link"><a href="{attr(grades_csv_href)}">_grades.csv</a> '
            f'(score, status, attempt per student)</p></section>'
        )
    if topic.get("message"):
        prompt = rewrite_canvas_html(topic["message"], local_paths, 1)
        parts.append(f'<section data-section="topic_body"><h2>Prompt</h2>{prompt}</section>')
    parts.append('<section data-section="entries"><h2>Entries</h2>')
    for e in filtered_entries:
        parts.append(render_discussion_entry(e, participants_by_id, local_paths))
    parts.append('</section>')

    return html_doc(
        f"{topic.get('title') or 'discussion'}{title_suffix}",
        SCHEMA_DISCUSSION,
        "\n".join(parts),
        up_href="index.html",
        up_label="Discussions",
    )


# ============================================================================
# Quiz HTML rendering (question bank only)
# ============================================================================

def render_quiz_html(course_name: str, quiz: dict, questions: list[dict],
                     local_paths: dict[tuple[str, str], str],
                     submissions_html: str = "") -> str:
    parts = [
        f'<h1 data-quiz-id="{attr(quiz.get("id"))}" '
        f'data-quiz-title="{attr(quiz.get("title"))}" '
        f'data-quiz-type="{attr(quiz.get("quiz_type"))}" '
        f'data-points-possible="{attr(quiz.get("points_possible"))}" '
        f'data-question-count="{attr(quiz.get("question_count"))}" '
        f'data-time-limit="{attr(quiz.get("time_limit"))}" '
        f'data-due-at="{attr(quiz.get("due_at"))}" '
        f'data-course-name="{attr(course_name)}">'
        f'{html.escape(quiz.get("title") or "")}</h1>'
    ]
    parts.append(
        f'<section data-section="quiz_meta"><dl class="kv">'
        f'<dt>Questions</dt><dd>{len(questions)}</dd>'
        f'<dt>Points possible</dt><dd>{html.escape(str(quiz.get("points_possible") or ""))}</dd>'
        f'<dt>Time limit</dt><dd>{html.escape(str(quiz.get("time_limit") or "none"))} min</dd>'
        f'</dl></section>'
    )
    if quiz.get("description"):
        desc = rewrite_canvas_html(quiz["description"], local_paths, 2)
        parts.append(f'<section data-section="description"><h2>Description</h2>{desc}</section>')

    if submissions_html:
        parts.append(submissions_html)

    parts.append('<section data-section="questions"><h2>Questions</h2>')
    for i, q in enumerate(questions, 1):
        qtype = q.get("question_type") or ""
        parts.append(
            f'<div class="question" data-question-id="{attr(q.get("id"))}" '
            f'data-question-type="{attr(qtype)}" '
            f'data-question-name="{attr(q.get("question_name"))}" '
            f'data-points-possible="{attr(q.get("points_possible"))}">'
        )
        parts.append(f'<h3>Q{i}. {html.escape(q.get("question_name") or "")} '
                     f'<small>({qtype}, {q.get("points_possible") or 0} pts)</small></h3>')
        qtext = rewrite_canvas_html(q.get("question_text") or "", local_paths, 2)
        parts.append(f'<div data-section="question_text">{qtext}</div>')
        answers = q.get("answers") or []
        if answers:
            parts.append('<div class="answers">')
            for a in answers:
                correct = (a.get("weight") or 0) > 0
                cls = "answer correct" if correct else "answer"
                tag = '<span class="correct-tag">[correct]</span>' if correct else ""
                txt = a.get("text") or a.get("html") or ""
                parts.append(
                    f'<div class="{cls}" data-answer-id="{attr(a.get("id"))}" '
                    f'data-answer-weight="{attr(a.get("weight"))}" '
                    f'data-answer-correct="{str(correct).lower()}">'
                    f'{html.escape(txt)}{tag}</div>'
                )
            parts.append('</div>')
        parts.append('</div>')
    parts.append('</section>')

    return html_doc(
        quiz.get("title") or "quiz", SCHEMA_QUIZ, "\n".join(parts),
        up_href="../index.html", up_label="Quizzes",
    )


def _quiz_answer_text(question: dict, entries: list[dict]) -> str:
    """Human-readable record of what a student answered for one classic-quiz
    question: the text of each option they chose (matched against the question's
    answer list by id), or their raw free-text / fill-in response. Multiple
    selections are joined with ' | '."""
    chosen_ids = {str(e.get("answer_id")) for e in entries
                  if e.get("answer_id") is not None}
    picked: list[str] = []
    for a in (question.get("answers") or []):
        if str(a.get("id")) in chosen_ids:
            picked.append((a.get("text") or a.get("html") or "").strip())
    # Free-text / fill-in answers carry no answer_id; keep their raw text.
    for e in entries:
        if e.get("answer_id") is None and e.get("text"):
            picked.append(str(e.get("text")).strip())
    return " | ".join(p for p in picked if p)


# ============================================================================
# Page / Announcement / Module / Syllabus rendering
# ============================================================================

def render_page_html(course_name: str, page: dict, body: str,
                     local_paths: dict[tuple[str, str], str]) -> str:
    body = rewrite_canvas_html(body, local_paths, 1)
    body_html = (
        f'<h1 data-page-url="{attr(page.get("url"))}" '
        f'data-page-title="{attr(page.get("title"))}" '
        f'data-updated-at="{attr(page.get("updated_at"))}" '
        f'data-editing-roles="{attr(page.get("editing_roles"))}" '
        f'data-published="{str(bool(page.get("published"))).lower()}" '
        f'data-course-name="{attr(course_name)}">'
        f'{html.escape(page.get("title") or "")}</h1>'
        f'<section data-section="page_body">{body}</section>'
    )
    return html_doc(
        page.get("title") or "page", "canvas-archive/page/v1", body_html,
        up_href="index.html", up_label="Pages",
    )


def render_announcement_html(course_name: str, ann: dict,
                             local_paths: dict[tuple[str, str], str]) -> str:
    message = rewrite_canvas_html(ann.get("message") or "", local_paths, 1)
    body_html = (
        f'<h1 data-announcement-id="{attr(ann.get("id"))}" '
        f'data-announcement-title="{attr(ann.get("title"))}" '
        f'data-posted-at="{attr(ann.get("posted_at"))}" '
        f'data-author="{attr(ann.get("user_name"))}" '
        f'data-course-name="{attr(course_name)}">'
        f'{html.escape(ann.get("title") or "")}</h1>'
        f'<section data-section="meta"><dl class="kv">'
        f'<dt>Posted</dt><dd>{html.escape(fmt_iso(ann.get("posted_at")))}</dd>'
        f'<dt>Author</dt><dd>{html.escape(ann.get("user_name") or "")}</dd>'
        f'</dl></section>'
        f'<section data-section="body">{message}</section>'
    )
    return html_doc(
        ann.get("title") or "announcement", "canvas-archive/announcement/v1", body_html,
        up_href="index.html", up_label="Announcements",
    )


def _quote_path(rel: str) -> str:
    """Quote each path segment for use as an href, preserving '/' separators."""
    return "/".join(urllib.parse.quote(seg) for seg in rel.split("/"))


# Canvas embeds resource references as href/src in user-authored HTML. Walk the
# matched attributes once and rewrite anything that resolves to a local archive
# path. Anything we can't resolve (unpublished, external) is left alone so the
# original Canvas URL stays inspectable.
# YouTube iframes return error 153 from file:// origins because YouTube's player
# rejects null/file origins. Replace each embed with a clickable thumbnail that
# jumps to YouTube on click. Video title comes from YouTube's public oEmbed API
# (no key needed) so the page reads naturally instead of just showing an image.
_YT_EMBED_RE = re.compile(
    r'<iframe([^>]*?)\bsrc="https?://(?:www\.)?youtube(?:-nocookie)?\.com/embed/'
    r'(?P<vid>[A-Za-z0-9_-]+)[^"]*"([^>]*?)>(?:\s*</iframe>)?',
    re.IGNORECASE,
)
_YT_DIM_RE = re.compile(r'\b(width|height)="(\d+)"', re.IGNORECASE)
_YT_TITLE_CACHE: dict[str, str] = {}


def _fetch_youtube_title(vid: str) -> str:
    if vid in _YT_TITLE_CACHE:
        return _YT_TITLE_CACHE[vid]
    url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={vid}&format=json"
    req = urllib.request.Request(url, headers={"User-Agent": "canvas-archive/0.1"})
    title = ""
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
            title = data.get("title") or ""
    except Exception as ex:
        logger.debug("  [yt title %s unreachable: %s]", vid, ex)
    _YT_TITLE_CACHE[vid] = title
    return title


def _rewrite_youtube_embeds(body: str) -> str:
    def sub(m: re.Match) -> str:
        vid = m.group("vid")
        attrs = (m.group(1) or "") + " " + (m.group(3) or "")
        dims = dict(_YT_DIM_RE.findall(attrs))
        w = dims.get("width", "560")
        h = dims.get("height", "315")
        watch = f"https://www.youtube.com/watch?v={vid}"
        thumb = f"https://img.youtube.com/vi/{vid}/hqdefault.jpg"
        title = _fetch_youtube_title(vid)
        title_block = ""
        if title:
            title_block = (
                f'<div class="yt-title" data-yt-title="{attr(title)}">'
                f'{html.escape(title)}</div>'
            )
        return (
            f'<div class="yt-embed" data-youtube-id="{vid}">'
            f'{title_block}'
            f'<a class="yt-thumb" href="{watch}" target="_blank" rel="noopener" '
            f'title="Play on YouTube">'
            f'<img src="{thumb}" alt="YouTube video {vid}" '
            f'width="{w}" height="{h}" loading="lazy"></a>'
            f'</div>'
        )
    return _YT_EMBED_RE.sub(sub, body)


_CANVAS_REF_RE = re.compile(r'(?P<attr>\b(?:src|href))="(?P<url>[^"]+)"', re.IGNORECASE)

# Canvas leaves literal import placeholders such as $CANVAS_OBJECT_REFERENCE$ in
# some copied/imported content when its substitution pass fails. These resolve to
# nothing and become live-but-dead canvas.instructure.com links once FFT shuts down.
_CANVAS_PLACEHOLDER_RE = re.compile(r"\$[A-Z][A-Z0-9_-]*\$")
_PLACEHOLDER_ANCHOR_RE = re.compile(
    r'<a\b[^>]*?\bhref="[^"]*\$[A-Z][A-Z0-9_-]*\$[^"]*"[^>]*?>(?P<text>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)


def _neutralize_canvas_placeholders(body: str) -> str:
    """Render Canvas import-placeholder links (e.g. $CANVAS_OBJECT_REFERENCE$) as
    inert text instead of leaving a live link to a target that does not exist.
    Anchors collapse to a labelled span; any other element keeping a placeholder
    in href/src has that attribute defused to data-broken-* so it cannot navigate."""
    def _kill_anchor(m: re.Match) -> str:
        return (
            '<span class="canvas-archive-broken" '
            'title="Canvas import placeholder, no archived target">'
            f'{m.group("text")}</span>'
        )

    body = _PLACEHOLDER_ANCHOR_RE.sub(_kill_anchor, body)

    def _defuse_attr(m: re.Match) -> str:
        if _CANVAS_PLACEHOLDER_RE.search(m.group("url")):
            return f'data-broken-{m.group("attr").lower()}="{m.group("url")}"'
        return m.group(0)

    return _CANVAS_REF_RE.sub(_defuse_attr, body)
_RESOLVERS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'/(?:courses/\d+/)?files/(\d+)(?:/|\?|#|$)'), "File"),
    (re.compile(r'/courses/\d+/pages/([^/?#"]+)'), "Page"),
    (re.compile(r'/courses/\d+/quizzes/(\d+)'), "Quiz"),
    (re.compile(r'/courses/\d+/assignments/(\d+)'), "Assignment"),
    (re.compile(r'/courses/\d+/discussion_topics/(\d+)'), "Discussion"),
]

# Optional lazy resolver for File ids the bulk Files API didn't surface
# (Canvas keeps "hidden" copies of files attached to assignment/quiz/discussion
# descriptions, accessible only via the verifier-bearing URL in the HTML).
# Set by archive_one; called as: _UNRESOLVED_FILE_FETCHER(fid: str, url_in_html: str) -> rel | None
_UNRESOLVED_FILE_FETCHER = None

# Optional lazy resolver for Page links that use a STALE slug. Canvas keeps old
# page URLs working via redirect after a page is renamed, but the pages API only
# lists the current slug, so a body authored before the rename references a slug
# we never registered. This resolver fetches the page by the stale slug to learn
# its canonical url / page_id, then looks those up in local_paths.
# Set by archive_one; called as: _UNRESOLVED_PAGE_FETCHER(slug: str) -> rel | None
_UNRESOLVED_PAGE_FETCHER = None


def rewrite_canvas_html(body: str | None, local_paths: dict[tuple[str, str], str],
                        depth: int) -> str:
    if not body:
        return body or ""
    prefix = "../" * depth
    body = _neutralize_canvas_placeholders(body)

    def resolve(url: str, kind_filter: str | None = None) -> tuple[str, str] | None:
        for pat, kind in _RESOLVERS:
            m = pat.search(url)
            if m:
                key = (kind, urllib.parse.unquote(m.group(1)))
                rel = local_paths.get(key)
                if rel:
                    return kind, rel
                if kind == "File" and _UNRESOLVED_FILE_FETCHER is not None:
                    rel = _UNRESOLVED_FILE_FETCHER(key[1], url)
                    if rel:
                        local_paths[key] = rel
                        return kind, rel
                if kind == "Page" and _UNRESOLVED_PAGE_FETCHER is not None:
                    rel = _UNRESOLVED_PAGE_FETCHER(key[1])
                    if rel:
                        local_paths[key] = rel
                        return kind, rel
                return None
        return None

    def sub(m: re.Match) -> str:
        hit = resolve(m.group("url"))
        if not hit:
            return m.group(0)
        _, rel = hit
        return f'{m.group("attr")}="{prefix}{_quote_path(rel)}"'

    body = _CANVAS_REF_RE.sub(sub, body)
    body = _rewrite_youtube_embeds(body)
    return body


def _module_item_local_href(it: dict, local_paths: dict[tuple[str, str], str]) -> str | None:
    kind = it.get("type") or ""
    if kind == "Page":
        slug = it.get("page_url") or ""
        rel = local_paths.get(("Page", str(slug)))
    elif kind in {"Assignment", "Quiz", "Discussion", "File"}:
        cid = it.get("content_id")
        if cid is None:
            return None
        rel = local_paths.get((kind, str(cid)))
    else:
        return None
    return _quote_path(rel) if rel else None


def render_modules_html(course_name: str, modules: list[dict],
                        local_paths: dict[tuple[str, str], str],
                        include_unpublished: bool = False) -> str:
    parts = [f'<h1 data-course-name="{attr(course_name)}">{html.escape(course_name)} - Modules</h1>']
    for m in modules:
        m_published = is_published(m)
        if not m_published and not include_unpublished:
            continue
        mod_name = (m.get("name") or f"module_{m.get('id')}") + unpublished_suffix(m)
        parts.append(
            f'<section data-module-id="{attr(m.get("id"))}" '
            f'data-published="{str(m_published).lower()}">'
        )
        parts.append(f'<h2>{html.escape(mod_name)}</h2>')
        items = m.get("items") or []
        if items:
            parts.append('<ul>')
            for it in items:
                it_published = is_published(it)
                if not it_published and not include_unpublished:
                    continue
                title = (it.get("title") or "") + unpublished_suffix(it)
                kind = it.get("type") or ""
                local_href = _module_item_local_href(it, local_paths)
                if kind == "SubHeader":
                    parts.append(
                        f'<li data-module-item-type="SubHeader"><strong>{html.escape(title)}</strong></li>'
                    )
                    continue
                if kind in {"ExternalUrl", "ExternalTool"}:
                    href = it.get("external_url") or it.get("html_url") or it.get("url") or ""
                    parts.append(
                        f'<li data-module-item-type="{attr(kind)}" data-external="true" '
                        f'data-published="{str(it_published).lower()}">'
                        f'<span class="kind">[{html.escape(kind)}]</span> '
                        f'<a href="{attr(href)}">{html.escape(title)}</a></li>'
                    )
                    continue
                attrs = (
                    f'data-module-item-type="{attr(kind)}" '
                    f'data-content-id="{attr(it.get("content_id"))}" '
                    f'data-published="{str(it_published).lower()}"'
                )
                if local_href:
                    parts.append(
                        f'<li {attrs} data-link="local">'
                        f'<span class="kind">[{html.escape(kind)}]</span> '
                        f'<a href="{local_href}">{html.escape(title)}</a></li>'
                    )
                else:
                    parts.append(
                        f'<li {attrs} data-link="missing">'
                        f'<span class="kind">[{html.escape(kind)}]</span> '
                        f'{html.escape(title)} '
                        f'<span class="note">(not archived)</span></li>'
                    )
            parts.append('</ul>')
        parts.append('</section>')
    return html_doc(
        f"{course_name} - Modules", "canvas-archive/modules/v1", "\n".join(parts),
        up_href="index.html", up_label=f"{course_name} index",
    )


def render_syllabus_html(course_name: str, body: str,
                         local_paths: dict[tuple[str, str], str],
                         assignment_groups: list[dict] | None = None,
                         apply_weights: bool = False) -> str:
    body = rewrite_canvas_html(body, local_paths, 0) if body else ""
    weights_html = format_grading_weights_table(assignment_groups or [], apply_weights)
    page = (
        f'<h1 data-course-name="{attr(course_name)}">{html.escape(course_name)} - Syllabus</h1>'
        f'{weights_html}'
        f'<section data-section="syllabus_body"><h2>Course syllabus</h2>{body or "<em>(empty)</em>"}</section>'
    )
    return html_doc(
        f"{course_name} - Syllabus",
        "canvas-archive/syllabus/v1",
        page,
        up_href="index.html",
        up_label=f"{course_name} index",
    )


# ============================================================================
# Per-section helpers
# ============================================================================

def build_section_index(sections: list[dict], course_name: str) -> tuple[dict[str, str], dict[str, str]]:
    """Returns (user_id -> short section name, section_id -> short section name)."""
    user_to_section: dict[str, str] = {}
    section_short: dict[str, str] = {}
    for s in sections:
        sname = s.get("name") or f"section_{s.get('id')}"
        short = derive_section_short(sname, course_name)
        section_short[str(s.get("id"))] = short
        for st in s.get("students") or []:
            user_to_section[str(st["id"])] = short
    return user_to_section, section_short


# ============================================================================
# GraphQL: rich-text submission comments + comment attachments
# ============================================================================

_GQL_COMMENTS_NODE = """
    _id
    commentsConnection(first: 100) {
      nodes {
        _id
        comment
        htmlComment
        createdAt
        author { _id name shortName }
        attachments {
          _id
          displayName
          contentType
          url
          size
          mimeClass
        }
      }
    }
"""


_GQL_COMMENTS_QUERY = f"""
query SubmissionComments($id: ID!) {{
  submission(id: $id) {{{_GQL_COMMENTS_NODE}}}
}}
"""


def _parse_gql_submission_node(sub: dict | None) -> list[dict]:
    """Flatten a GraphQL submission node into the comment shape this module uses."""
    if not sub:
        return []
    nodes = (sub.get("commentsConnection") or {}).get("nodes") or []
    out: list[dict] = []
    for n in nodes:
        author = n.get("author") or {}
        out.append({
            "comment_id": n.get("_id"),
            "author": author.get("name") or author.get("shortName") or "",
            "author_id": author.get("_id"),
            "created_at": n.get("createdAt"),
            "comment": n.get("comment") or "",
            "html_comment": n.get("htmlComment") or "",
            "attachments": [
                {
                    "attachment_id": a.get("_id"),
                    "display_name": a.get("displayName"),
                    "content_type": a.get("contentType"),
                    "size": a.get("size"),
                    "mime_class": a.get("mimeClass"),
                    "url": a.get("url"),
                }
                for a in (n.get("attachments") or [])
            ],
        })
    return out


def fetch_classic_quiz_submissions(
        api: Canvas, cid: int, qid: Any
) -> tuple[list[dict], dict[str, list[dict]]]:
    """For a classic quiz, return (quiz_submissions, submission_data_by_user_id).

    submission_data is the authoritative per-question record (selected answer_id,
    correct flag, points) pulled from each student's submission_history. The kept
    attempt is preferred (matched on attempt number, else the last history entry
    carrying submission_data)."""
    url = (f"{api.base}/api/v1/courses/{cid}/quizzes/{qid}/submissions"
           f"?per_page=100&include[]=submission&include[]=submission_history")
    data_by_user: dict[str, list[dict]] = {}
    qsubs: list[dict] = []
    try:
        first, _ = api.get_json(url)
    except Exception as ex:
        logger.warning("  [quiz %s submissions fetch failed]: %s", qid, ex)
        return [], {}
    qsubs = first.get("quiz_submissions") or []
    assoc = first.get("submissions") or []
    qsub_attempt_by_user = {str(q.get("user_id")): q.get("attempt") for q in qsubs}
    for s in assoc:
        uid = str(s.get("user_id"))
        history = s.get("submission_history") or []
        want_attempt = qsub_attempt_by_user.get(uid)
        chosen = None
        for h in history:
            if h.get("submission_data") is None:
                continue
            if want_attempt is not None and h.get("attempt") == want_attempt:
                chosen = h
                break
            chosen = h  # fall back to the latest history entry with data
        if chosen is not None:
            data_by_user[uid] = chosen.get("submission_data") or []
    return qsubs, data_by_user


def _archive_quiz_submissions(api: Canvas, cid: int, quiz: dict,
                              questions: list[dict],
                              students_by_id: dict[str, dict],
                              user_to_section: dict[str, str],
                              quizzes_dir: Path, stem: str, course_name: str,
                              local_paths: dict[tuple[str, str], str],
                              q_stats: dict) -> str:
    """For a classic quiz, write one _grades.csv (in a per-quiz folder, mirroring
    the assignment layout) holding every student's results -- one row per answered
    question with their answer, whether it was correct, and points earned -- and
    return an HTML section with a per-student score table to embed on the quiz
    page. The on-page table is grouped by section, exactly like the assignment
    roster. No per-student HTML pages are produced (the detail lives in the CSV).
    Returns '' when the quiz has no submissions."""
    qsubs, data_by_user = fetch_classic_quiz_submissions(api, cid, quiz.get("id"))
    if not qsubs:
        return ""

    questions_by_id = {str(q.get("id")): q for q in questions}
    ordered_qids = [str(q.get("id")) for q in questions]

    # (sortable, student_name, section, score, points_possible) for the on-page table.
    summary_rows: list[tuple[str, str, str, Any, Any]] = []
    csv_rows: list[list[str]] = []
    for qsub in qsubs:
        uid = str(qsub.get("user_id"))
        student = students_by_id.get(uid) or {"id": uid, "name": f"user {uid}"}
        sname = student.get("name") or f"student_{uid}"
        sec = user_to_section.get(uid, "")
        score = qsub.get("score")
        pts = qsub.get("quiz_points_possible")
        summary_rows.append(
            (student.get("sortable_name") or sname, sname, sec, score, pts))

        # Columns repeated on every per-question row for this student.
        base = [
            str(student.get("id") or uid), sname, sec,
            _fmt_points(score), _fmt_points(qsub.get("kept_score")), _fmt_points(pts),
            str(qsub.get("attempt") or ""), qsub.get("finished_at") or "",
            qsub.get("workflow_state") or "",
        ]
        ans_by_qid: dict[str, list[dict]] = {}
        for d in data_by_user.get(uid, []):
            ans_by_qid.setdefault(str(d.get("question_id")), []).append(d)
        # Quiz order first, then any answered question not in the fetched bank
        # (question groups / randomized pools).
        qids = ordered_qids + [q for q in ans_by_qid if q not in ordered_qids]
        wrote_detail = False
        for n, qid in enumerate(qids, 1):
            entries = ans_by_qid.get(qid)
            if not entries:
                continue
            q = questions_by_id.get(qid, {})
            got = sum(float(e.get("points") or 0) for e in entries)
            correct = any(e.get("correct") for e in entries)
            csv_rows.append(base + [
                str(n), str(qid), q.get("question_name") or "",
                q.get("question_type") or "",
                _fmt_points(got), str(bool(correct)).lower(),
                _quiz_answer_text(q, entries),
            ])
            wrote_detail = True
        if not wrote_detail:
            # No per-question data captured (submission_data unavailable); still
            # record the student's overall result as a single summary row.
            csv_rows.append(base + ["", "", "", "", "", "", ""])

    # _grades.csv in a per-quiz folder, e.g. Quizzes/<Quiz Title>/_grades.csv.
    grades_dir = quizzes_dir / stem
    grades_dir.mkdir(parents=True, exist_ok=True)
    with (grades_dir / "_grades.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([
            "student_id", "student_name", "section", "score", "kept_score",
            "points_possible", "attempt", "finished_at", "workflow_state",
            "question_number", "question_id", "question_name", "question_type",
            "points_earned", "correct", "answer",
        ])
        # Sort by student name, then question number (now at index 9 after the
        # inserted section column at index 2).
        w.writerows(sorted(
            csv_rows,
            key=lambda r: (r[1].lower(), int(r[9]) if r[9].isdigit() else 0)))

    q_stats["quizzes_with_submissions"] += 1
    q_stats["students_recorded"] += len(summary_rows)

    # The quiz page now lives inside the per-quiz folder (Quizzes/<stem>/<stem>.html),
    # so _grades.csv is a sibling in the same folder.
    csv_href = "_grades.csv"
    out = [
        f'<section data-section="quiz_scores" data-count="{len(summary_rows)}">',
        '<h2>Student scores</h2>',
        f'<p class="csv-link"><a href="{attr(csv_href)}">_grades.csv</a> '
        f'(per-question results for each student)</p>',
    ]
    # Group by section, exactly like the assignment roster: one sub-table per
    # section, empty-section bucket first, then sections alphabetically; within a
    # section, students sorted by sortable name.
    by_section: dict[str, list[tuple[str, str, str, Any, Any]]] = {}
    for row in summary_rows:
        by_section.setdefault(row[2], []).append(row)
    section_keys = sorted(by_section.keys(), key=lambda s: (s != "", s.lower()))
    for sec in section_keys:
        rows = sorted(by_section[sec], key=lambda r: (r[0] or "").lower())
        sec_label = sec or "All students"
        out.append(
            f'<div data-section="quiz_scores_group" data-section-label="{attr(sec)}" '
            f'data-row-count="{len(rows)}">'
            f'<h3>{html.escape(sec_label)} <small>({len(rows)} students)</small></h3>'
            '<table><thead><tr><th>Student</th><th>Score</th></tr></thead><tbody>'
        )
        for _sortable, name, _sec, score, pts in rows:
            score_disp = (f"{_fmt_points(score)} / {_fmt_points(pts)}"
                          if score is not None else "(no score)")
            out.append(
                f'<tr data-student-name="{attr(name)}" data-score="{attr(score)}">'
                f'<td>{html.escape(name)}</td><td>{html.escape(score_disp)}</td></tr>'
            )
        out.append('</tbody></table></div>')
    out.append('</section>')
    return "\n".join(out)


def fetch_rich_comments(api: Canvas, submission_id: Any) -> list[dict] | None:
    """Single-submission fetch. Kept for callers that genuinely need one
    record; the assignment loop uses fetch_rich_comments_batched instead."""
    try:
        resp = api.gql(_GQL_COMMENTS_QUERY, {"id": str(submission_id)})
    except Exception as ex:
        logger.debug("    [gql comments fail sub=%s]: %s", submission_id, ex)
        return None
    return _parse_gql_submission_node((resp.get("data") or {}).get("submission"))


def _build_batched_gql_query(n: int) -> str:
    """Generate a GraphQL query that fetches N submissions in one round trip
    using field aliases. Each alias 's0', 's1', ... maps to the i-th id."""
    sig = ", ".join(f"$id{i}: ID!" for i in range(n))
    fields = "\n  ".join(
        f"s{i}: submission(id: $id{i}) {{{_GQL_COMMENTS_NODE}}}"
        for i in range(n)
    )
    return f"query Q({sig}) {{\n  {fields}\n}}"


def fetch_rich_comments_batched(
    api: Canvas,
    submission_ids: list[str],
    batch_size: int = GQL_BATCH_SIZE,
    workers: int = HTTP_WORKERS,
) -> dict[str, list[dict] | None]:
    """Return {submission_id -> comments list (or None on failure)} for every
    id in submission_ids, using GraphQL aliasing to do up to `batch_size`
    submissions per HTTP call, and a thread pool to run multiple batches in
    parallel."""
    result: dict[str, list[dict] | None] = {}
    if not submission_ids:
        return result
    # Deduplicate while preserving order; callers may pass the same id more
    # than once because the same submission can appear in multiple sections.
    seen: set[str] = set()
    ordered: list[str] = []
    for sid in submission_ids:
        sid_s = str(sid)
        if sid_s in seen:
            continue
        seen.add(sid_s)
        ordered.append(sid_s)

    def _one_batch(batch: list[str]) -> dict[str, list[dict] | None]:
        query = _build_batched_gql_query(len(batch))
        variables = {f"id{i}": sid for i, sid in enumerate(batch)}
        try:
            resp = api.gql(query, variables)
        except Exception as ex:
            logger.debug("    [gql batch fail size=%d]: %s", len(batch), ex)
            return {sid: None for sid in batch}
        data = resp.get("data") or {}
        return {
            batch[i]: _parse_gql_submission_node(data.get(f"s{i}"))
            for i in range(len(batch))
        }

    batches = [ordered[i:i + batch_size] for i in range(0, len(ordered), batch_size)]
    if not batches:
        return result
    if workers <= 1 or len(batches) == 1:
        for b in batches:
            result.update(_one_batch(b))
        return result
    with ThreadPoolExecutor(max_workers=min(workers, len(batches))) as ex:
        for d in ex.map(_one_batch, batches):
            result.update(d)
    return result


# ============================================================================
# Folder-index walker
# ============================================================================

# Naming rules for "is this folder already indexed by a named HTML file":
# - <aname>/<aname>.html  -> the assignment folder is indexed by <aname>.html
# - any other folder with a file matching the folder name + ".html" -> indexed
# Otherwise we write index.html.
# Section subfolders are explicitly skipped (per user spec): the assignment
# overview already lists every student in that section.
def _named_index_for(folder: Path) -> str | None:
    fname = folder.name
    candidate = folder / f"{fname}.html"
    if candidate.exists():
        return candidate.name
    return None


def _label_for_path(rel: Path) -> str:
    return rel.name


def _entries_for_dir(dirpath: Path, course_dir: Path, skip: set[str],
                     unpublished_paths: set[Path]) -> list[IndexEntry]:
    entries: list[IndexEntry] = []
    for child in sorted(dirpath.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if child.name in skip:
            continue
        if child.name.startswith(".") or child.name == "_error.log":
            continue
        rel = child.relative_to(course_dir)
        suffix = " (unpublished)" if rel in unpublished_paths else ""
        if child.is_dir():
            sub_index = _named_index_for(child) or "index.html"
            entries.append(IndexEntry(
                label=child.name + "/" + suffix,
                href=urllib.parse.quote(child.name) + "/" + urllib.parse.quote(sub_index),
            ))
        else:
            entries.append(IndexEntry(
                label=child.name + suffix,
                href=urllib.parse.quote(child.name),
            ))
    return entries


def prune_empty_dirs(course_dir: Path) -> list[str]:
    """Remove any directory under course_dir that holds no files at all (for
    example a Quizzes/ folder created for a course that turned out to have no
    quizzes). This matches the documented behaviour that empty sections are
    skipped and stops the course index from linking to dead, contentless
    folders. Deepest-first, so a parent that becomes empty once its empty
    children are gone is also removed. The course root is never touched.
    Returns the course-relative paths removed."""
    removed: list[str] = []
    dirs = [p for p in course_dir.rglob("*") if p.is_dir()]
    for current in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
        if not current.exists():
            continue
        if any(p.is_file() for p in current.rglob("*")):
            continue
        try:
            shutil.rmtree(current)
            removed.append(current.relative_to(course_dir).as_posix())
        except OSError as ex:
            logger.warning("could not prune empty dir %s: %s", current, ex)
    return removed


def write_folder_indexes(course_dir: Path, course_name: str,
                          section_dirs: set[Path],
                          unpublished_paths: set[Path] | None = None) -> None:
    """Write index.html in every directory under course_dir that doesn't
    already have a named index (e.g. Assignments/<aname>/<aname>.html), and
    that isn't a section subfolder. The course root is handled separately
    by write_course_root_index."""
    unpub = unpublished_paths or set()
    for current in sorted(course_dir.rglob("*")):
        if not current.is_dir():
            continue
        if current == course_dir:
            continue
        if current in section_dirs:
            continue
        if _named_index_for(current):
            # already has e.g. <aname>.html as the index
            continue
        # Depth from course root determines relative path back to root.
        rel = current.relative_to(course_dir)
        depth = len(rel.parts)
        up_href = "../" * depth + "index.html"
        # Up label = parent folder name (or course name for top-level).
        if depth == 1:
            up_label = f"{course_name} index"
        else:
            parent = current.parent
            up_label = _named_index_for(parent) or parent.name
            if up_label.endswith(".html"):
                up_label = up_label[:-5]
        # The "up" from an immediate child of root should point at root's
        # index.html, which is one level up.
        entries = _entries_for_dir(current, course_dir, skip={"index.html"},
                                    unpublished_paths=unpub)
        title = f"{rel.as_posix()}"
        html_out = render_index_doc(
            title=title,
            schema="canvas-archive/folder-index/v1",
            intro_html=f'<p><em>Contents of <code>{html.escape(rel.as_posix())}/</code></em></p>',
            entries=entries,
            up_href=up_href,
            up_label=up_label,
        )
        (current / "index.html").write_text(html_out, encoding="utf-8")


def write_course_root_index(course_dir: Path, course_name: str,
                             course: dict, stats: dict,
                             unpublished_paths: set[Path] | None = None,
                             extra_files: list[tuple[str, str]] | None = None) -> None:
    """Top-level index.html for the course folder. extra_files is a list of
    (filename, description) for optional top-level pages (Groups, Conferences,
    Outcomes, etc.) that exist only when the course actually has that data."""
    unpub = unpublished_paths or set()
    entries: list[IndexEntry] = []
    notable = [
        ("Syllabus.html", "Syllabus (course description + grading weights)"),
        ("Modules.html", "Modules (canvas module sequence with links)"),
        ("Gradebook.csv", "Gradebook (student x assignment scores)"),
        ("Students.csv", "Students (roster with sections)"),
        ("Assignments.csv", "Assignments (one row per assignment)"),
    ] + list(extra_files or [])
    for fname, _desc in notable:
        if (course_dir / fname).exists():
            entries.append(IndexEntry(
                label=fname,
                href=urllib.parse.quote(fname),
                note=_desc,
            ))
    # The visible note is a quick "how much is in here" count. For most sections
    # the number of immediate children is the right answer (one folder per
    # assignment, one file per discussion, etc.), but the Students folder holds
    # only the roster page plus an avatars/ subfolder, so its child count (2) is
    # meaningless; show the actual student count from stats instead.
    student_count = stats.get("student_count")
    subdirs = [d for d in sorted(course_dir.iterdir()) if d.is_dir()]
    for d in subdirs:
        sub_index = _named_index_for(d) or "index.html"
        suffix = " (unpublished)" if d.relative_to(course_dir) in unpub else ""
        if d.name == "Students" and isinstance(student_count, int):
            note = f"{student_count} students"
        else:
            count = sum(1 for _ in d.iterdir())
            note = f"{count} entries"
        entries.append(IndexEntry(
            label=f"{d.name}/{suffix}",
            href=urllib.parse.quote(d.name) + "/" + urllib.parse.quote(sub_index),
            note=note,
        ))

    course_meta_island = (
        '<script type="application/json" data-section="course_meta">'
        + html.escape(json.dumps({"course": course, "stats": stats,
                                  "exported_at": _utc_now_iso()},
                                 default=str), quote=False)
        + '</script>'
    )
    # Humanize Canvas's workflow_state for the visible label; the raw value is
    # preserved in the data-course-status attribute for machine parsing.
    raw_status = course.get("workflow_state") or ""
    status_label = {
        "available": "Active (published)",
        "completed": "Concluded",
        "unpublished": "Unpublished",
        "deleted": "Deleted",
    }.get(raw_status, raw_status or "unknown")
    # Course status and term are kept only in non-surfaced HTML (the data-*
    # attributes below and the course_meta JSON island), not in the visible list.
    intro = (
        f'<section data-section="course_meta" '
        f'data-course-id="{attr(course.get("id"))}" '
        f'data-course-name="{attr(course.get("name"))}" '
        f'data-course-status="{attr(raw_status)}" '
        f'data-course-status-label="{attr(status_label)}" '
        f'data-course-term="{attr((course.get("term") or {}).get("name") or "")}" '
        f'data-course-code="{attr(course.get("course_code"))}">'
        f'<dl class="kv">'
        f'<dt>Canvas course id</dt><dd>{html.escape(str(course.get("id") or ""))}</dd>'
        f'<dt>Course code</dt><dd>{html.escape(course.get("course_code") or "")}</dd>'
        f'<dt>Start</dt><dd>{html.escape(fmt_iso(course.get("start_at")))}</dd>'
        f'<dt>End</dt><dd>{html.escape(fmt_iso(course.get("end_at")))}</dd>'
        f'</dl></section>'
    )
    html_out = render_index_doc(
        title=f"{course_name}",
        schema="canvas-archive/course-index/v1",
        intro_html=intro,
        entries=entries,
        up_href=None,
        up_label=None,
        extra_sections=course_meta_island,
    )
    (course_dir / "index.html").write_text(html_out, encoding="utf-8")


# The course_meta JSON island written above is the machine-readable record of a
# course archive. The whole-account driver (--all) and the cross-course master
# index both read it back through read_course_meta(), so the extraction lives
# here next to the writer to keep one source of truth.
_COURSE_META_RE = re.compile(
    r'<script type="application/json" data-section="course_meta">(.*?)</script>',
    re.DOTALL,
)


def read_course_meta(course_dir: Path) -> dict | None:
    """Parse the course_meta JSON island from a course folder's index.html.

    Returns the {"course": ..., "stats": ..., "exported_at": ...} dict written by
    write_course_root_index, or None if the folder has no readable index/island.
    The island is html-escaped (quote=False) on write, so it is unescaped before
    JSON decoding. A literal '</script>' can never appear in the payload because
    write-time escaping turns '<' into '&lt;', so the non-greedy regex is safe."""
    index = course_dir / "index.html"
    if not index.is_file():
        return None
    try:
        text = index.read_text(encoding="utf-8")
    except OSError:
        return None
    match = _COURSE_META_RE.search(text)
    if not match:
        return None
    try:
        meta = json.loads(html.unescape(match.group(1)))
    except json.JSONDecodeError:
        return None
    return meta if isinstance(meta, dict) else None


# ============================================================================
# New Quizzes (Quizzes.Next) + optional course-structure metadata pages
# ============================================================================

def _archive_new_quizzes(api: Canvas, cid: int, assignments: list[dict],
                         quizzes_dir: Path, course_name: str,
                         local_paths: dict[tuple[str, str], str],
                         include_unpublished: bool, q_stats: dict,
                         workers: int = HTTP_WORKERS) -> None:
    """New Quizzes are LTI assignments and never appear in /api/v1/.../quizzes.
    Per-question content lives behind the New Quizzes items API, which usually
    is not reachable with session cookies. Best-effort: try to fetch items; if
    blocked, fall back to a shell page that links the assignment overview (which
    already carries the description and, via the gradebook, student scores)."""
    new_quizzes = [a for a in assignments
                   if a.get("is_quiz_lti_assignment")
                   and (is_published(a) or include_unpublished)]
    if not new_quizzes:
        return

    def _do_new_quiz(a: dict) -> dict | None:
        aid = str(a.get("id"))
        title = a.get("name") or f"new_quiz_{aid}"
        stem = safe_name(title)
        fname = f"{stem} (New Quiz).html"

        items_captured = 0
        items_html = ""
        items = None
        try:
            items, _ = api.get_json(
                f"{api.base}/api/quiz/v1/courses/{cid}/quizzes/{aid}/items")
        except Exception as ex:
            logger.info("  [new quiz items unavailable %s]: %s", aid, str(ex)[:80])
        if isinstance(items, list) and items:
            rendered = []
            for i, it in enumerate(items, 1):
                entry = it.get("entry") or {}
                title_i = entry.get("title") or it.get("title") or f"Item {i}"
                body_i = rewrite_canvas_html(
                    entry.get("item_body") or entry.get("stem") or "", local_paths, 1)
                rendered.append(
                    f'<div class="question" data-item-id="{attr(it.get("id"))}">'
                    f'<h3>Q{i}. {html.escape(title_i)}</h3>{body_i}</div>'
                )
            items_html = ('<section data-section="new_quiz_items">'
                          f'<h2>Questions ({len(items)})</h2>'
                          + "".join(rendered) + '</section>')
            items_captured = 1

        asg_rel = local_paths.get(("Assignment", aid))
        asg_link = ""
        if asg_rel:
            inner = asg_rel[len("Quizzes/"):] if asg_rel.startswith("Quizzes/") else "../" + asg_rel
            asg_link = (f'<p>Assignment view (description + roster): '
                        f'<a href="{attr(inner)}">{html.escape(title)}</a></p>')

        note = (
            '<section data-section="new_quiz_note"><h2>New Quiz</h2>'
            '<p>This is a Canvas <strong>New Quiz</strong> (Quizzes.Next), an '
            'external tool. Its question content is served by that tool and may '
            'not be fully archivable through the API. Student scores are in the '
            'gradebook and the assignment view.</p>'
            f'{asg_link}</section>'
        )
        body = (
            f'<h1 data-assignment-id="{attr(aid)}" data-new-quiz="true" '
            f'data-published="{str(bool(is_published(a))).lower()}" '
            f'data-course-name="{attr(course_name)}">{html.escape(title)}</h1>'
            f'<section data-section="meta"><dl class="kv">'
            f'<dt>Points possible</dt><dd>{html.escape(_fmt_points(a.get("points_possible")))}</dd>'
            f'<dt>Due</dt><dd>{html.escape(fmt_iso(a.get("due_at")))}</dd>'
            f'</dl></section>'
            f'{note}{items_html}'
        )
        (quizzes_dir / fname).write_text(
            html_doc(f"{title} (New Quiz)", "canvas-archive/new-quiz/v1", body,
                     up_href="index.html", up_label="Quizzes"),
            encoding="utf-8")
        local_paths[("Assignment", aid)] = f"Quizzes/{fname}"
        return {"new_quizzes": 1, "new_quiz_items_captured": items_captured}

    for res in _run_parallel(new_quizzes, _do_new_quiz, workers):
        if not res:
            continue
        q_stats["new_quizzes"] = q_stats.get("new_quizzes", 0) + res["new_quizzes"]
        q_stats["new_quiz_items_captured"] = (
            q_stats.get("new_quiz_items_captured", 0) + res["new_quiz_items_captured"])


def _kv_section(title: str, pairs: list[tuple[str, Any]]) -> str:
    rows = "".join(
        f'<dt>{html.escape(str(k))}</dt><dd>{html.escape(str(v) if v is not None else "")}</dd>'
        for k, v in pairs
    )
    return f'<section><h3>{html.escape(title)}</h3><dl class="kv">{rows}</dl></section>'


def _write_meta_page(course_dir: Path, fname: str, course_name: str,
                     title: str, schema: str, body: str, raw: Any) -> None:
    island = (
        '<script type="application/json" data-section="raw">'
        + html.escape(json.dumps(raw, default=str), quote=False)
        + '</script>'
    )
    (course_dir / fname).write_text(
        html_doc(f"{course_name} - {title}", schema,
                 f'<h1>{html.escape(title)}</h1>{body}{island}',
                 up_href="index.html", up_label=f"{course_name} index"),
        encoding="utf-8")


def archive_course_extras(api: Canvas, cid: int, course_dir: Path,
                          course_name: str) -> list[tuple[str, str]]:
    """Capture course-structure elements that have no dedicated section: group
    sets, conferences (metadata only, no recordings), collaborations, outcomes,
    and custom grading schemes. Each becomes a top-level HTML page only when the
    course actually has that data (skipped silently otherwise). Returns a list
    of (filename, description) for the course-root index."""
    created: list[tuple[str, str]] = []

    def _get_list(url: str, key: str | None = None) -> list[dict]:
        try:
            d, _ = api.get_json(url)
        except Exception as ex:
            logger.debug("  [extras fetch failed %s]: %s", url, ex)
            return []
        if key:
            return (d or {}).get(key) or []
        return d if isinstance(d, list) else []

    # --- Group sets / groups / members ---
    cats = _get_list(f"{api.base}/api/v1/courses/{cid}/group_categories?per_page=100")
    groups = _get_list(f"{api.base}/api/v1/courses/{cid}/groups?per_page=100")
    if groups:
        for g in groups:
            try:
                g["_members"] = _get_list(
                    f"{api.base}/api/v1/groups/{g.get('id')}/users?per_page=100")
            except Exception:
                g["_members"] = []
        cat_name = {str(c.get("id")): c.get("name") for c in cats}
        by_cat: dict[str, list[dict]] = {}
        for g in groups:
            by_cat.setdefault(str(g.get("group_category_id")), []).append(g)
        secs = []
        for cat_id, gl in by_cat.items():
            secs.append(f'<section data-section="group_set" data-category-id="{attr(cat_id)}">'
                        f'<h2>{html.escape(cat_name.get(cat_id) or "Group set")}</h2>')
            for g in gl:
                members = g.get("_members") or []
                names = ", ".join(html.escape(m.get("name") or "") for m in members) or "(no members)"
                secs.append(
                    f'<div class="entry" data-group-id="{attr(g.get("id"))}">'
                    f'<strong>{html.escape(g.get("name") or "")}</strong> '
                    f'({len(members)} members)<div>{names}</div></div>'
                )
            secs.append('</section>')
        _write_meta_page(course_dir, "Groups.html", course_name, "Groups",
                         "canvas-archive/groups/v1", "".join(secs),
                         {"group_categories": cats, "groups": groups})
        created.append(("Groups.html", f"Groups ({len(groups)} groups)"))

    # --- Conferences (metadata only; recordings are intentionally not fetched) ---
    confs = _get_list(f"{api.base}/api/v1/courses/{cid}/conferences", key="conferences")
    if confs:
        secs = []
        for c in confs:
            secs.append(_kv_section(c.get("title") or "Conference", [
                ("Type", c.get("conference_type")),
                ("Status", c.get("status")),
                ("Started", fmt_iso(c.get("started_at"))),
                ("Ended", fmt_iso(c.get("ended_at"))),
                ("Duration (min)", c.get("duration")),
                ("Description", c.get("description")),
                ("Recordings", f"{len(c.get('recordings') or [])} (not downloaded)"),
            ]))
        _write_meta_page(course_dir, "Conferences.html", course_name, "Conferences",
                         "canvas-archive/conferences/v1", "".join(secs),
                         {"conferences": confs})
        created.append(("Conferences.html", f"Conferences ({len(confs)})"))

    # --- Collaborations ---
    collabs = _get_list(f"{api.base}/api/v1/courses/{cid}/collaborations?per_page=100")
    if collabs:
        secs = []
        for c in collabs:
            secs.append(_kv_section(c.get("title") or "Collaboration", [
                ("Type", c.get("collaboration_type")),
                ("Created by", c.get("user_name")),
                ("Created", fmt_iso(c.get("created_at"))),
                ("URL", c.get("url")),
                ("Description", c.get("description")),
            ]))
        _write_meta_page(course_dir, "Collaborations.html", course_name, "Collaborations",
                         "canvas-archive/collaborations/v1", "".join(secs),
                         {"collaborations": collabs})
        created.append(("Collaborations.html", f"Collaborations ({len(collabs)})"))

    # --- Outcomes ---
    links = _get_list(f"{api.base}/api/v1/courses/{cid}/outcome_group_links?per_page=100")
    if links:
        rows = []
        for ln in links:
            o = ln.get("outcome") or {}
            rows.append(
                f'<tr data-outcome-id="{attr(o.get("id"))}">'
                f'<td>{html.escape(o.get("title") or "")}</td>'
                f'<td>{html.escape((o.get("description") or "")[:400])}</td></tr>'
            )
        body = ('<table><thead><tr><th>Outcome</th><th>Description</th></tr></thead>'
                f'<tbody>{"".join(rows)}</tbody></table>')
        _write_meta_page(course_dir, "Outcomes.html", course_name, "Outcomes",
                         "canvas-archive/outcomes/v1", body, {"outcome_group_links": links})
        created.append(("Outcomes.html", f"Outcomes ({len(links)})"))

    # --- Custom grading schemes ---
    schemes = _get_list(f"{api.base}/api/v1/courses/{cid}/grading_standards?per_page=100")
    if schemes:
        secs = []
        for sch in schemes:
            scheme = sch.get("grading_scheme") or []
            rows = "".join(
                f'<tr><td>{html.escape(str(e.get("name")))}</td>'
                f'<td>{_fmt_pct(float(e.get("value") or 0) * 100)}%</td></tr>'
                for e in scheme
            )
            secs.append(
                f'<section data-section="grading_scheme" data-scheme-id="{attr(sch.get("id"))}">'
                f'<h2>{html.escape(sch.get("title") or "Grading scheme")}</h2>'
                f'<table><thead><tr><th>Name</th><th>Min</th></tr></thead>'
                f'<tbody>{rows}</tbody></table></section>'
            )
        _write_meta_page(course_dir, "GradingSchemes.html", course_name, "Grading schemes",
                         "canvas-archive/grading-schemes/v1", "".join(secs),
                         {"grading_standards": schemes})
        created.append(("GradingSchemes.html", f"Grading schemes ({len(schemes)})"))

    return created


# Audio/video file extensions, used as a fallback when an attachment carries no
# usable content-type. Student A/V submissions (recorded video/audio) are often
# very large, so they are skipped by default.
_AV_EXTENSIONS = (
    ".mp4", ".mov", ".m4v", ".avi", ".wmv", ".flv", ".mkv", ".webm",
    ".mpg", ".mpeg", ".3gp", ".ogv",
    ".mp3", ".m4a", ".wav", ".aac", ".ogg", ".oga", ".flac", ".wma", ".aiff",
)


def _is_av_attachment(att: dict) -> bool:
    """True when a submission attachment is audio or video. Canvas tags the MIME
    type as 'content-type' (hyphenated) on file objects; we also accept
    'content_type' and fall back to the filename extension."""
    ctype = (att.get("content-type") or att.get("content_type") or "").lower()
    if ctype.startswith(("video/", "audio/")):
        return True
    name = (att.get("display_name") or att.get("filename") or "").lower()
    return name.endswith(_AV_EXTENSIONS)


# ============================================================================
# Main
# ============================================================================

def archive_one(args, api: Canvas, cid: int) -> int:
    # Reset module-level resolver in case a previous course crashed before clearing it.
    global _UNRESOLVED_FILE_FETCHER
    _UNRESOLVED_FILE_FETCHER = None

    workers = max(1, getattr(args, "workers", HTTP_WORKERS) or HTTP_WORKERS)
    course_label = f"course {cid}"
    logger.info("== fetch course meta ==")
    course, _ = api.get_json(f"{api.base}/api/v1/courses/{cid}?include[]=syllabus_body&include[]=term")
    course_label = f"{course.get('name')} ({cid})"
    logger.info("course: %s (id=%s)", course.get("name"), course.get("id"))

    logger.info("== fetch assignments ==")
    assignments = api.get_paginated(f"{api.base}/api/v1/courses/{cid}/assignments?per_page=100&include[]=rubric_assessment")
    logger.info("  %d assignments", len(assignments))

    folder_name = derive_course_folder_name(
        course, assignments, getattr(args, "term_scheme", DEFAULT_TERM_SCHEME) or DEFAULT_TERM_SCHEME
    )
    course_dir = args.output_root / folder_name
    course_dir.mkdir(parents=True, exist_ok=True)
    logger.info("output: %s", course_dir)

    # Assignment groups (for grading weights in syllabus)
    assignment_groups: list[dict] = []
    with stage(course_label, "assignment_groups"):
        assignment_groups = api.get_paginated(
            f"{api.base}/api/v1/courses/{cid}/assignment_groups?include[]=assignments&per_page=100"
        )
        logger.info("  %d assignment groups", len(assignment_groups))

    sections: list[dict] = []
    students: list[dict] = []
    user_to_section: dict[str, str] = {}
    section_short: dict[str, str] = {}
    split_mode = False
    students_by_id: dict[str, dict] = {}
    with stage(course_label, "sections+students"):
        logger.info("== fetch sections ==")
        sections = api.get_paginated(f"{api.base}/api/v1/courses/{cid}/sections?include[]=students&per_page=100")
        user_to_section, section_short = build_section_index(sections, course.get("name") or "")
        split_mode = len([s for s in sections if (s.get("students") or [])]) > 1
        logger.info("  %d section(s); split_mode=%s", len(sections), split_mode)

        logger.info("== fetch students ==")
        students = api.get_paginated(
            f"{api.base}/api/v1/courses/{cid}/users?enrollment_type[]=student"
            f"&include[]=avatar_url&include[]=email&per_page=100"
        )
        students_by_id = {str(s["id"]): s for s in students}
        write_students_csv(course_dir / "Students.csv", students, user_to_section)
        write_assignments_csv(course_dir / "Assignments.csv", assignments)
        logger.info("  %d students", len(students))

        if getattr(args, "skip_student_photos", False):
            logger.info("== student photos skipped (--skip-student-photos) ==")
            avatar_paths = {}
        else:
            logger.info("== fetch student photos ==")
            avatar_paths = archive_student_avatars(api, students, course_dir, workers)
        (course_dir / "Students").mkdir(exist_ok=True)
        (course_dir / "Students" / "Students.html").write_text(
            render_students_html(
                course.get("name") or folder_name, students, user_to_section, avatar_paths
            ),
            encoding="utf-8",
        )

    subs_by_key: dict[tuple[str, str], dict] = {}
    with stage(course_label, "submissions_matrix"):
        logger.info("== fetch gradebook (submissions matrix, published assignments only) ==")
        sub_url = (f"{api.base}/api/v1/courses/{cid}/students/submissions"
                   f"?student_ids[]=all&per_page=100"
                   f"&include[]=submission_comments&include[]=rubric_assessment"
                   f"&include[]=attachments")
        all_submissions = api.get_paginated(sub_url)
        for s in all_submissions:
            subs_by_key[(str(s.get("user_id")), str(s.get("assignment_id")))] = s
        logger.info("  %d submission records", len(all_submissions))
        write_gradebook(course_dir, students, assignments, subs_by_key)

    # local_paths maps (item_type, content_id_or_slug) -> relative path under course_dir.
    # Populated as each resource is rendered so Modules.html and rewrite_canvas_html() can
    # turn Canvas URLs in user-authored HTML into local archive paths.
    local_paths: dict[tuple[str, str], str] = {}
    # unpublished_paths holds course_dir-relative POSIX paths for archived items
    # whose Canvas publication state is "unpublished" -- index renderers append
    # a " (unpublished)" suffix to their labels.
    unpublished_paths: set[Path] = set()

    # === Files: mirror the Canvas Files folder hierarchy under course_dir/Files ===
    # Folder metadata is needed to compute the correct relative path even for
    # lazy embed fetches, so always fetch it (cheap) regardless of --skip-files.
    files_dir = course_dir / "Files"
    folder_path_by_id: dict[str, str] = {}
    include_unpublished = bool(getattr(args, "include_unpublished", False))
    try:
        folders = api.get_paginated(f"{api.base}/api/v1/courses/{cid}/folders?per_page=100")
        folder_path_by_id = {
            str(f["id"]): (f.get("full_name") or "course files") for f in folders
        }
    except Exception as ex:
        logger.warning("  [folders meta fetch failed]: %s", ex)

    def _rel_folder_for(folder_id: Any) -> str:
        """Convert a Canvas folder_id into a path relative to course_dir/Files.
        Empty string means "directly under Files/". Falls back to fetching the
        folder when not already in the bulk-listed map."""
        key = str(folder_id or "")
        full = folder_path_by_id.get(key)
        if not full and key:
            try:
                fmeta, _ = api.get_json(f"{api.base}/api/v1/folders/{key}")
                full = fmeta.get("full_name") or "course files"
                folder_path_by_id[key] = full
            except Exception:
                full = "course files"
        if not full:
            return ""
        rel = full.split("course files", 1)[-1].lstrip("/")
        # Sanitize each segment. A Canvas-supplied full_name could contain path
        # separators or "..", which would otherwise let a malicious/self-hosted
        # instance steer file writes outside Files/. safe_name() turns ".." into
        # "_" and strips slashes, so the joined path can never escape.
        segments = [safe_name(p) for p in rel.replace("\\", "/").split("/")
                    if p and p != "."]
        return "/".join(segments)

    if args.skip_files:
        logger.info("== skip files (--skip-files); files linked in assignments/quizzes/etc. are NOT downloaded ==")
    else:
        with stage(course_label, "files"):
            logger.info("== fetch + download files (max %d MB per file, %d workers) ==",
                        args.max_file_size_mb, workers)
            files_dir.mkdir(exist_ok=True)
            max_bytes = args.max_file_size_mb * 1024 * 1024
            files = api.get_paginated(f"{api.base}/api/v1/courses/{cid}/files?per_page=100")
            f_stats = {"ok": 0, "fail": 0, "skipped_size": 0, "skipped_unpublished": 0}
            f_stats_lock = threading.Lock()

            # Pre-filter and prepare each download task on the main thread, so
            # the workers only need to call api.download + bookkeep.
            file_tasks: list[tuple[dict, str, str, Path]] = []
            for fmeta in files:
                if not is_published(fmeta) and not include_unpublished:
                    f_stats["skipped_unpublished"] += 1
                    continue
                size = fmeta.get("size") or 0
                if size > max_bytes:
                    logger.info("  [skip oversize] %s (%.1f MB > %d MB)",
                                fmeta.get("display_name") or fmeta.get("filename"),
                                size / (1024 * 1024), args.max_file_size_mb)
                    f_stats["skipped_size"] += 1
                    continue
                rel_folder = _rel_folder_for(fmeta.get("folder_id"))
                dest_folder = files_dir / rel_folder if rel_folder else files_dir
                dest_folder.mkdir(parents=True, exist_ok=True)
                fn = safe_name(fmeta.get("display_name") or fmeta.get("filename")
                               or f"file_{fmeta.get('id')}")
                url = fmeta.get("url")
                if not url:
                    f_stats["fail"] += 1
                    continue
                file_tasks.append((fmeta, fn, url, dest_folder / fn))

            def _download_one(task):
                fmeta, fn, url, dest_path = task
                try:
                    api.download(url, dest_path)
                    return fmeta, fn, dest_path, None
                except Exception as ex:
                    return fmeta, fn, dest_path, ex

            if file_tasks:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    for fmeta, fn, dest_path, err in pool.map(_download_one, file_tasks):
                        if err is not None:
                            logger.warning("  [file fail %s]: %s", fn, err)
                            with f_stats_lock:
                                f_stats["fail"] += 1
                            continue
                        rel_folder = _rel_folder_for(fmeta.get("folder_id"))
                        rel = f"Files/{rel_folder}/{fn}" if rel_folder else f"Files/{fn}"
                        with f_stats_lock:
                            f_stats["ok"] += 1
                            local_paths[("File", str(fmeta.get("id")))] = rel
                            if not is_published(fmeta):
                                unpublished_paths.add(Path(rel))
            logger.info("  files: %s", f_stats)

    # Lazy resolver for files referenced in user-authored HTML but not in the
    # bulk Files listing (Canvas keeps hidden copies attached to assignment /
    # quiz / discussion descriptions; they only exist behind verifier URLs).
    # Resolved files land in their proper Canvas folder under Files/, mirroring
    # the Canvas tree exactly (no synthetic _embedded/ shelf).
    embed_stats = {"resolved": 0, "failed": 0, "cache_hits": 0}
    # fid -> resolved relative path (None when a previous attempt failed). Lets
    # multiple HTML bodies referencing the same file share one network call.
    embed_cache: dict[str, str | None] = {}
    embed_cache_lock = threading.Lock()

    def _place_path(rel_folder: str, fname: str) -> Path:
        dest_folder = files_dir / rel_folder if rel_folder else files_dir
        dest_folder.mkdir(parents=True, exist_ok=True)
        return dest_folder / fname

    def _lazy_file_fetch(fid: str, url_in_html: str) -> str | None:
        # Cache hit: if we already resolved (or failed) this file id, reuse it.
        # Cuts repeat /api/v1/files/<fid> meta calls when the same embedded
        # file is referenced from multiple bodies (announcements, syllabus,
        # multiple page bodies, etc.).
        with embed_cache_lock:
            if fid in embed_cache:
                rel = embed_cache[fid]
                if rel is not None:
                    embed_stats["cache_hits"] += 1
                return rel
        rel = _lazy_file_fetch_uncached(fid, url_in_html)
        with embed_cache_lock:
            embed_cache[fid] = rel
        return rel

    def _lazy_file_fetch_uncached(fid: str, url_in_html: str) -> str | None:
        # Try the API metadata endpoint first (works when the file is visible to
        # the authenticated user even if it wasn't in the bulk course listing).
        try:
            meta_url = f"{api.base}/api/v1/files/{fid}"
            meta, _ = api.get_json(meta_url)
            fname = safe_name(meta.get("display_name") or meta.get("filename") or f"file_{fid}")
            dl_url = meta.get("url")
            if dl_url:
                rel_folder = _rel_folder_for(meta.get("folder_id"))
                api.download(dl_url, _place_path(rel_folder, fname))
                embed_stats["resolved"] += 1
                rel = f"Files/{rel_folder}/{fname}" if rel_folder else f"Files/{fname}"
                logger.info("  [embed fetched via api] %s -> %s", fid, rel)
                if not is_published(meta):
                    unpublished_paths.add(Path(rel))
                return rel
        except Exception as ex:
            logger.debug("  [embed meta lookup failed %s]: %s", fid, ex)
        # Fall back: use the verifier-bearing URL from the HTML directly.
        # The href usually looks like /courses/<cid>/files/<fid>?verifier=... ;
        # convert it to /courses/<cid>/files/<fid>/download?verifier=...&download_frd=1
        # which forces the real binary instead of a preview page.
        try:
            actual = html.unescape(url_in_html)
            if actual.startswith("/"):
                actual = api.base.rstrip("/") + actual
            parsed = urllib.parse.urlparse(actual)
            qs = urllib.parse.parse_qs(parsed.query)
            verifier = (qs.get("verifier") or [""])[0]
            path = parsed.path
            cm = re.search(r"/courses/(\d+)/files/(\d+)", path)
            if cm:
                dl_path = f"/courses/{cm.group(1)}/files/{cm.group(2)}/download"
            else:
                dl_path = f"/files/{fid}/download"
            dl_query = "download_frd=1"
            if verifier:
                dl_query = f"verifier={urllib.parse.quote(verifier)}&{dl_query}"
            dl_url = (f"{parsed.scheme or 'https'}://{parsed.netloc or 'canvas.instructure.com'}"
                      f"{dl_path}?{dl_query}")
            files_dir.mkdir(parents=True, exist_ok=True)
            req = urllib.request.Request(dl_url, headers={
                "User-Agent": "canvas-archive/0.1",
                "Accept": "*/*",
            })
            with api._open(req, timeout=120) as r:
                ctype = (r.headers.get("Content-Type") or "").lower()
                disp = r.headers.get("Content-Disposition") or ""
                m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', disp)
                if m:
                    fname = safe_name(urllib.parse.unquote(m.group(1)))
                elif "text/html" in ctype:
                    logger.debug("  [embed verifier got HTML, not file: %s]", fid)
                    embed_stats["failed"] += 1
                    return None
                else:
                    fname = f"file_{fid}"
                # No folder context available on verifier-only fetches; drop at
                # the Files/ root so they remain visible and parseable.
                tmp_dest = files_dir / fname
                with tmp_dest.open("wb") as out:
                    while True:
                        chunk = r.read(65536)
                        if not chunk:
                            break
                        out.write(chunk)
            embed_stats["resolved"] += 1
            rel = f"Files/{fname}"
            logger.info("  [embed fetched via verifier] %s -> %s", fid, rel)
            return rel
        except Exception as ex:
            logger.debug("  [embed verifier fetch failed %s]: %s", fid, ex)
            embed_stats["failed"] += 1
            return None

    # With --skip-files, leave the embed resolver disabled so files linked from
    # assignment/quiz/discussion/page bodies are NOT fetched either; those links
    # stay as their original Canvas URLs in the archived HTML.
    _UNRESOLVED_FILE_FETCHER = None if args.skip_files else _lazy_file_fetch

    # === Pre-register internal link targets ===
    # Bodies are rendered section by section below, but Canvas HTML cross-links
    # freely between sections (an assignment description linking to a Page, a
    # Page linking to a Quiz, etc.). Registering every target path BEFORE any
    # body is rendered lets rewrite_canvas_html() resolve those links regardless
    # of section order. The lists fetched here are reused by the render loops
    # further down so nothing is fetched twice. Only items that will actually be
    # written get registered, so links never point at a missing local file.
    def _safe_fetch_list(url: str, what: str) -> list[dict]:
        try:
            return api.get_paginated(url)
        except Exception as ex:
            logger.warning("  [%s list fetch failed]: %s", what, ex)
            return []

    topics = _safe_fetch_list(
        f"{api.base}/api/v1/courses/{cid}/discussion_topics?per_page=100", "discussions")
    quizzes = _safe_fetch_list(
        f"{api.base}/api/v1/courses/{cid}/quizzes?per_page=100", "quizzes")
    pages = _safe_fetch_list(
        f"{api.base}/api/v1/courses/{cid}/pages?per_page=100", "pages")

    for t in topics:
        if not is_published(t) and not include_unpublished:
            continue
        title = safe_name(t.get("title") or f"topic_{t.get('id')}")
        # Always a single canonical page per topic. In split_mode we ALSO write
        # per-section breakdowns alongside it, but the link target must be a real
        # file (a bare "Discussions/" folder href shows a directory listing under
        # file://, not the discussion), so it always points at the combined page.
        local_paths[("Discussion", str(t.get("id")))] = f"Discussions/{title}.html"
    for q in quizzes:
        if not is_published(q) and not include_unpublished:
            continue
        qfname = safe_name(q.get("title") or f"quiz_{q.get('id')}") + ".html"
        local_paths[("Quiz", str(q.get("id")))] = f"Quizzes/{qfname}"
    for p_meta in pages:
        if not is_published(p_meta) and not include_unpublished:
            continue
        slug = p_meta.get("url")
        if not slug:
            continue
        pfname = safe_name(p_meta.get("title") or slug) + ".html"
        local_paths[("Page", str(slug))] = f"Pages/{pfname}"
        pid = p_meta.get("page_id")
        if pid is not None:
            local_paths[("Page", str(pid))] = f"Pages/{pfname}"

    # Page-alias resolver: when a body links a page by a stale slug (the page was
    # renamed after the link was authored), fetch it by that slug to learn the
    # canonical url / page_id and map back to the already-registered local path.
    global _UNRESOLVED_PAGE_FETCHER
    page_alias_cache: dict[str, str | None] = {}
    page_alias_lock = threading.Lock()

    def _lazy_page_resolve(slug: str) -> str | None:
        with page_alias_lock:
            if slug in page_alias_cache:
                return page_alias_cache[slug]
        rel: str | None = None
        try:
            d, _ = api.get_json(
                f"{api.base}/api/v1/courses/{cid}/pages/{urllib.parse.quote(slug)}")
            canon = d.get("url")
            pid = d.get("page_id")
            if canon is not None:
                rel = local_paths.get(("Page", str(canon)))
            if rel is None and pid is not None:
                rel = local_paths.get(("Page", str(pid)))
            if rel:
                logger.info("  [page alias] %s -> %s", slug, canon)
        except Exception as ex:
            logger.debug("  [page alias resolve failed %s]: %s", slug, ex)
        with page_alias_lock:
            page_alias_cache[slug] = rel
        return rel

    _UNRESOLVED_PAGE_FETCHER = _lazy_page_resolve

    # === Assignments ===
    logger.info("== render assignments (gql batch=%d, %d workers) ==",
                GQL_BATCH_SIZE, workers)
    assignments_dir = course_dir / "Assignments"
    section_dirs: set[Path] = set()
    stats = {
        "asgmt_folders": 0, "students_rendered": 0,
        "attachments": 0, "attach_failed": 0, "skipped_av": 0,
        "comment_attachments": 0, "comment_attach_failed": 0,
        "gql_comment_fetches": 0, "gql_comment_failures": 0,
    }
    skip_av = bool(getattr(args, "skip_av_submissions", True))
    stats_lock = threading.Lock()
    skipped_unpublished = 0

    # Phase 1 - Plan every assignment that will produce student pages, group
    # submissions by section, and accumulate the full list of submission ids
    # that need GraphQL comment lookups so we can batch them in one swoop.
    @dataclass
    class _AsgmtPlan:
        a: dict
        aname: str
        asgmt_dir: Path
        per_section_subs: dict[str, list[tuple[dict, dict]]]
        per_section_rows: dict[str, list[dict]]

    plans: list[_AsgmtPlan] = []
    sub_ids_needing_comments: list[str] = []
    # Every published (or, with --include-unpublished, every) assignment gets an
    # overview page so the Assignments index mirrors Canvas and every link
    # resolves. Submission-bearing assignments additionally get a per-student
    # roster + submission pages + _grades.csv when submissions exist.
    for a in assignments:
        a_published = is_published(a)
        if not a_published and not include_unpublished:
            skipped_unpublished += 1
            continue
        aname = safe_name(a.get("name") or f"assignment_{a.get('id')}")
        aid = str(a.get("id"))
        local_paths[("Assignment", aid)] = f"Assignments/{aname}/{aname}.html"
        if not a_published:
            unpublished_paths.add(Path(f"Assignments/{aname}"))
            unpublished_paths.add(Path(f"Assignments/{aname}/{aname}.html"))

        per_section_rows: dict[str, list[dict]] = {}
        per_section_subs: dict[str, list[tuple[dict, dict]]] = {}
        for s in students:
            uid = str(s["id"])
            sub = subs_by_key.get((uid, aid))
            if not sub:
                continue
            sec_short = user_to_section.get(uid, "unsectioned") if split_mode else None
            sec_key = sec_short or ""
            per_section_subs.setdefault(sec_key, []).append((s, sub))
            per_section_rows.setdefault(sec_key, []).append({
                "student_id": uid,
                "student_name": s.get("name") or "",
                "score": sub.get("score"),
                "grade": sub.get("grade"),
                "submitted_at": sub.get("submitted_at"),
                "workflow_state": sub.get("workflow_state"),
                "late": sub.get("late"),
                "missing": sub.get("missing"),
                "excused": sub.get("excused"),
                "attempt": sub.get("attempt"),
            })
            if sub.get("submission_comments") and sub.get("id") is not None:
                sub_ids_needing_comments.append(str(sub.get("id")))

        plans.append(_AsgmtPlan(
            a=a,
            aname=aname,
            asgmt_dir=assignments_dir / aname,
            per_section_subs=per_section_subs,
            per_section_rows=per_section_rows,
        ))

    # Phase 2 - one round of batched GraphQL across every assignment. Cuts
    # 300+ sequential round trips down to a handful of parallel ones.
    comments_by_sub_id: dict[str, list[dict] | None] = {}
    if sub_ids_needing_comments:
        logger.info("  gql batch: %d submissions", len(sub_ids_needing_comments))
        comments_by_sub_id = fetch_rich_comments_batched(
            api, sub_ids_needing_comments,
            batch_size=GQL_BATCH_SIZE, workers=workers,
        )

    # Phase 3 - per assignment, do the per-(student, sub) work in parallel:
    # download submission attachments, download comment attachments, render
    # the submission HTML. The thread pool is reused across assignments so
    # connections stay keep-alive across the whole stage.
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        for plan in plans:
            a = plan.a
            aname = plan.aname
            aid = str(a.get("id"))
            with stage(course_label, f"assignment[{aid}={aname}]"):
                stats["asgmt_folders"] += 1
                plan.asgmt_dir.mkdir(parents=True, exist_ok=True)
                student_rows: list[StudentRow] = []
                section_grades_csv: dict[str, str] = {}

                # Build the section-keyed leaf paths + _grades.csv first so
                # workers can write straight to them.
                section_leaf: dict[str, Path] = {}
                for sec_key, _pairs in plan.per_section_subs.items():
                    leaf = plan.asgmt_dir / sec_key if split_mode else plan.asgmt_dir
                    leaf.mkdir(parents=True, exist_ok=True)
                    if split_mode:
                        section_dirs.add(leaf)
                    write_section_grades_csv(leaf / "_grades.csv", a,
                                              plan.per_section_rows[sec_key])
                    section_leaf[sec_key] = leaf
                    section_grades_csv[sec_key] = (
                        f"{urllib.parse.quote(sec_key)}/_grades.csv" if split_mode
                        else "_grades.csv"
                    )

                # Flatten (sec_key, student, sub) for the pool.
                tasks: list[tuple[str, dict, dict, Path]] = []
                for sec_key, pairs in plan.per_section_subs.items():
                    leaf = section_leaf[sec_key]
                    for student, sub in pairs:
                        tasks.append((sec_key, student, sub, leaf))

                def _process_one(task):
                    sec_key, student, sub, leaf = task
                    sname = safe_name(student.get("name") or f"student_{student.get('id')}")

                    # Download submission attachments serially within this
                    # worker (size is small per student; the parallelism is
                    # already across students).
                    fnames: list[str] = []
                    local_stats = {
                        "attachments": 0, "attach_failed": 0, "skipped_av": 0,
                        "comment_attachments": 0, "comment_attach_failed": 0,
                        "gql_comment_fetches": 0, "gql_comment_failures": 0,
                    }
                    for att in (sub.get("attachments") or []):
                        if skip_av and _is_av_attachment(att):
                            local_stats["skipped_av"] += 1
                            continue
                        fn = safe_name(
                            f"{sname} {att.get('display_name') or att.get('filename') or 'attachment'}")
                        url = att.get("url")
                        if not url:
                            continue
                        try:
                            api.download(url, leaf / fn)
                            local_stats["attachments"] += 1
                            fnames.append(fn)
                        except Exception as ex:
                            logger.warning("    [att fail %s]: %s", fn, ex)
                            local_stats["attach_failed"] += 1

                    # Rich comments came from the batched GraphQL pass. The
                    # subset that REST said had no comments was never queued,
                    # so absence here means "REST said empty" (empty list).
                    sub_id = sub.get("id")
                    rest_has_comments = bool(sub.get("submission_comments"))
                    if not rest_has_comments:
                        rich: list[dict] | None = []
                    else:
                        rich = comments_by_sub_id.get(str(sub_id))
                        if rich is None:
                            local_stats["gql_comment_failures"] += 1
                        else:
                            local_stats["gql_comment_fetches"] += 1

                    comment_fnames: list[str] = []
                    if rich:
                        for c in rich:
                            for ca in c.get("attachments") or []:
                                ca_url = ca.get("url")
                                disp = ca.get("display_name") or f"comment_{ca.get('attachment_id')}"
                                fn = safe_name(f"{sname} (comment) {disp}")
                                if not ca_url:
                                    continue
                                try:
                                    api.download(ca_url, leaf / fn)
                                    local_stats["comment_attachments"] += 1
                                    comment_fnames.append(fn)
                                    ca["local_filename"] = fn
                                except Exception as ex:
                                    logger.warning("    [comment att fail %s]: %s", fn, ex)
                                    local_stats["comment_attach_failed"] += 1

                    depth = 3 if split_mode else 2
                    up_href = (
                        f"../{urllib.parse.quote(aname)}.html" if split_mode
                        else f"{urllib.parse.quote(aname)}.html"
                    )
                    (leaf / f"{sname}.html").write_text(
                        render_submission_html(
                            course.get("name") or "", a, student, sub, fnames,
                            local_paths, depth, rich_comments=rich,
                            up_href=up_href, up_label=a.get("name") or "Assignment",
                        ),
                        encoding="utf-8")

                    sub_html_rel = (
                        f"{urllib.parse.quote(sec_key)}/{urllib.parse.quote(sname)}.html"
                        if split_mode else f"{urllib.parse.quote(sname)}.html"
                    )
                    row = StudentRow(
                        section=sec_key,
                        student_id=str(student.get("id")),
                        student_name=student.get("name") or "",
                        sortable_name=student.get("sortable_name") or "",
                        submission_html=sub_html_rel,
                        score=sub.get("score"),
                        points_possible=a.get("points_possible"),
                        badge=status_badge(sub),
                        submitted_at=sub.get("submitted_at") or "",
                        attempt=sub.get("attempt"),
                        attachments=tuple(fnames),
                        comment_attachments=tuple(comment_fnames),
                    )
                    return row, local_stats

                for row, local_stats in pool.map(_process_one, tasks):
                    student_rows.append(row)
                    with stats_lock:
                        for k, v in local_stats.items():
                            stats[k] += v
                        stats["students_rendered"] += 1

                # Render the assignment overview HTML once the roster is complete.
                (plan.asgmt_dir / f"{aname}.html").write_text(
                    render_assignment_overview_html(
                        course.get("name") or "", a, local_paths,
                        student_rows=student_rows,
                        section_grades_csv=section_grades_csv,
                    ),
                    encoding="utf-8")
    finally:
        pool.shutdown(wait=True)

    logger.info("  %s; skipped %d unpublished assignments", stats, skipped_unpublished)

    # === Discussions ===
    logger.info("== fetch + render discussions ==")
    discussions_dir = course_dir / "Discussions"
    discussions_dir.mkdir(exist_ok=True)
    d_stats = {"topics": 0, "files_written": 0, "grades_csv_written": 0,
               "skipped_unpublished": 0}
    with stage(course_label, "discussions"):
        # topics was pre-fetched above for link pre-registration.
        pub_topics = []
        for t in topics:
            if not is_published(t) and not include_unpublished:
                d_stats["skipped_unpublished"] += 1
            else:
                pub_topics.append(t)

        def _do_topic(t: dict) -> dict | None:
            t_published = is_published(t)
            try:
                view, _ = api.get_json(
                    f"{api.base}/api/v1/courses/{cid}/discussion_topics/{t['id']}/view")
                participants_by_id = {str(p["id"]): p for p in (view.get("participants") or [])}
                view_entries = view.get("view") or []
                title = safe_name(t.get("title") or f"topic_{t.get('id')}")
                files_written = 0
                unpub: list[Path] = []

                # If the discussion is graded, write a _grades.csv beside the HTML.
                grades_csv_filename = None
                grades_csv_written = 0
                assignment_id = t.get("assignment_id")
                if assignment_id is not None:
                    asgmt = next((x for x in assignments if str(x.get("id")) == str(assignment_id)), None)
                    if asgmt is not None:
                        rows = []
                        for s in students:
                            uid = str(s["id"])
                            sub = subs_by_key.get((uid, str(assignment_id)))
                            if not sub:
                                continue
                            rows.append({
                                "student_id": uid,
                                "student_name": s.get("name") or "",
                                "score": sub.get("score"),
                                "grade": sub.get("grade"),
                                "submitted_at": sub.get("submitted_at"),
                                "workflow_state": sub.get("workflow_state"),
                                "late": sub.get("late"),
                                "missing": sub.get("missing"),
                                "excused": sub.get("excused"),
                                "attempt": sub.get("attempt"),
                            })
                        if rows:
                            grades_csv_filename = safe_name(f"{title} _grades") + ".csv"
                            write_section_grades_csv(discussions_dir / grades_csv_filename, asgmt, rows)
                            grades_csv_written = 1

                # Canonical page: the full discussion (all sections). Always
                # written so module links and inline references resolve to a real
                # file. This is the link target registered in local_paths above.
                (discussions_dir / f"{title}.html").write_text(
                    render_discussion_html(
                        course.get("name") or "", t, view, None, view_entries,
                        participants_by_id, local_paths,
                        grades_csv_href=urllib.parse.quote(grades_csv_filename) if grades_csv_filename else None,
                    ),
                    encoding="utf-8")
                files_written += 1
                if not t_published:
                    unpub.append(Path(f"Discussions/{title}.html"))

                # Multi-section courses additionally get a per-section breakdown
                # alongside the canonical page.
                if split_mode:
                    for sec in sections:
                        students_in_sec = {str(st["id"]) for st in (sec.get("students") or [])}
                        if not students_in_sec:
                            continue
                        short = section_short[str(sec.get("id"))]
                        filtered = filter_entries_by_users(view_entries, students_in_sec)
                        if not list(collect_entries(filtered)):
                            continue
                        fname = safe_name(f"{title} - {short}") + ".html"
                        (discussions_dir / fname).write_text(
                            render_discussion_html(
                                course.get("name") or "", t, view, short, filtered,
                                participants_by_id, local_paths,
                                grades_csv_href=urllib.parse.quote(grades_csv_filename) if grades_csv_filename else None,
                            ),
                            encoding="utf-8")
                        files_written += 1
                        if not t_published:
                            unpub.append(Path(f"Discussions/{fname}"))
                return {"files_written": files_written,
                        "grades_csv_written": grades_csv_written, "unpublished": unpub}
            except Exception as ex:
                log_error(course_label, f"discussion[{t.get('id')}]", ex)
                return None

        for res in _run_parallel(pub_topics, _do_topic, workers):
            if not res:
                continue
            d_stats["topics"] += 1
            d_stats["files_written"] += res["files_written"]
            d_stats["grades_csv_written"] += res["grades_csv_written"]
            for p in res["unpublished"]:
                unpublished_paths.add(p)
    logger.info("  %s", d_stats)

    # === Quizzes ===
    logger.info("== fetch + render quizzes ==")
    quizzes_dir = course_dir / "Quizzes"
    quizzes_dir.mkdir(exist_ok=True)
    q_stats = {"rendered": 0, "skipped_unpublished": 0,
               "students_recorded": 0, "quizzes_with_submissions": 0}
    with stage(course_label, "quizzes"):
        # quizzes was pre-fetched above for link pre-registration.
        pub_quizzes = []
        for q in quizzes:
            if not is_published(q) and not include_unpublished:
                q_stats["skipped_unpublished"] += 1
            else:
                pub_quizzes.append(q)

        def _do_quiz(q: dict) -> dict | None:
            q_published = is_published(q)
            try:
                questions = api.get_paginated(
                    f"{api.base}/api/v1/courses/{cid}/quizzes/{q['id']}/questions?per_page=100")
                stem = safe_name(q.get("title") or f"quiz_{q.get('id')}")
                fname = stem + ".html"
                # Mirror the assignment layout: each quiz is its own folder, with
                # the page at Quizzes/<stem>/<stem>.html and _grades.csv beside it.
                # The page name matches the folder, so the folder-index walker
                # treats it as the folder's index (no stray index.html), and the
                # Quizzes index links each quiz once, straight to the page with
                # its student-score table.
                quiz_dir = quizzes_dir / stem
                quiz_dir.mkdir(parents=True, exist_ok=True)

                # Local stats dict so the (thread-shared) q_stats is only touched
                # by the single-threaded aggregator below.
                local_qs = {"students_recorded": 0, "quizzes_with_submissions": 0}
                # Quiz results are small (a per-quiz _grades.csv, no per-student
                # pages), so they are always archived.
                submissions_html = _archive_quiz_submissions(
                    api, cid, q, questions, students_by_id, user_to_section,
                    quizzes_dir, stem, course.get("name") or "", local_paths,
                    local_qs)

                (quiz_dir / fname).write_text(
                    render_quiz_html(course.get("name") or "", q, questions,
                                     local_paths, submissions_html=submissions_html),
                    encoding="utf-8")
                local_paths[("Quiz", str(q.get("id")))] = f"Quizzes/{stem}/{fname}"
                unpublished = []
                if not q_published:
                    unpublished = [Path(f"Quizzes/{stem}"),
                                   Path(f"Quizzes/{stem}/{fname}")]
                return {"students_recorded": local_qs["students_recorded"],
                        "quizzes_with_submissions": local_qs["quizzes_with_submissions"],
                        "unpublished": unpublished}
            except Exception as ex:
                log_error(course_label, f"quiz[{q.get('id')}]", ex)
                return None

        for res in _run_parallel(pub_quizzes, _do_quiz, workers):
            if not res:
                continue
            q_stats["rendered"] += 1
            q_stats["students_recorded"] += res["students_recorded"]
            q_stats["quizzes_with_submissions"] += res["quizzes_with_submissions"]
            for p in res["unpublished"]:
                unpublished_paths.add(p)
        # New Quizzes (Quizzes.Next) live as LTI assignments, not in /quizzes.
        with stage(course_label, "new_quizzes"):
            _archive_new_quizzes(
                api, cid, assignments, quizzes_dir, course.get("name") or "",
                local_paths, include_unpublished, q_stats, workers)
    logger.info("  %s", q_stats)

    # === Pages ===
    logger.info("== fetch + render pages ==")
    pages_dir = course_dir / "Pages"
    pages_dir.mkdir(exist_ok=True)
    p_stats = {"rendered": 0, "skipped_unpublished": 0}
    with stage(course_label, "pages"):
        # pages was pre-fetched above for link pre-registration.
        pub_pages = []
        for p_meta in pages:
            if not is_published(p_meta) and not include_unpublished:
                p_stats["skipped_unpublished"] += 1
            elif p_meta.get("url"):
                pub_pages.append(p_meta)

        def _do_page(p_meta: dict) -> dict | None:
            url_slug = p_meta.get("url")
            try:
                page_full, _ = api.get_json(
                    f"{api.base}/api/v1/courses/{cid}/pages/{url_slug}")
                # The single-page endpoint returns a fuller record; prefer its
                # published flag because it can override the listing's value.
                p_published = is_published(page_full) and is_published(p_meta)
                body = page_full.get("body") or ""
                fname = safe_name(p_meta.get("title") or url_slug) + ".html"
                (pages_dir / fname).write_text(
                    render_page_html(course.get("name") or "", page_full, body, local_paths),
                    encoding="utf-8")
                local_paths[("Page", str(url_slug))] = f"Pages/{fname}"
                page_id = page_full.get("page_id") or p_meta.get("page_id")
                if page_id is not None:
                    local_paths[("Page", str(page_id))] = f"Pages/{fname}"
                return {"unpublished": [Path(f"Pages/{fname}")] if not p_published else []}
            except Exception as ex:
                log_error(course_label, f"page[{url_slug}]", ex)
                return None

        for res in _run_parallel(pub_pages, _do_page, workers):
            if not res:
                continue
            p_stats["rendered"] += 1
            for p in res["unpublished"]:
                unpublished_paths.add(p)
    logger.info("  %s", p_stats)

    # === Announcements (opt-in via --save-announcements) ===
    if args.save_announcements:
        with stage(course_label, "announcements"):
            logger.info("== fetch + render announcements ==")
            ann_dir = course_dir / "Announcements"
            ann_dir.mkdir(exist_ok=True)
            anns = api.get_paginated(
                f"{api.base}/api/v1/announcements?context_codes[]=course_{cid}"
                f"&start_date=2014-01-01&end_date=2030-01-01&per_page=100"
            )
            for ann in anns:
                with stage(course_label, f"announcement[{ann.get('id')}]"):
                    date_prefix = (ann.get("posted_at") or "")[:10] or "undated"
                    fname = safe_name(f"{date_prefix} {ann.get('title') or 'announcement'}") + ".html"
                    (ann_dir / fname).write_text(
                        render_announcement_html(course.get("name") or "", ann, local_paths),
                        encoding="utf-8")
            logger.info("  %d announcements", len(anns))
    else:
        logger.info("== skip announcements (use --save-announcements to archive) ==")

    # === Syllabus + Modules (last so all link targets are known) ===
    with stage(course_label, "syllabus"):
        syl_body = course.get("syllabus_body") or ""
        (course_dir / "Syllabus.html").write_text(
            render_syllabus_html(
                course.get("name") or "", syl_body, local_paths,
                assignment_groups=assignment_groups,
                apply_weights=bool(course.get("apply_assignment_group_weights")),
            ),
            encoding="utf-8")

    modules: list[dict] = []
    with stage(course_label, "modules"):
        logger.info("== fetch + render modules ==")
        modules = api.get_paginated(f"{api.base}/api/v1/courses/{cid}/modules?include[]=items&per_page=100")
        (course_dir / "Modules.html").write_text(
            render_modules_html(course.get("name") or "", modules, local_paths,
                                include_unpublished=include_unpublished),
            encoding="utf-8")
        logger.info("  %d modules", len(modules))

    # Clear lazy resolvers so they don't carry into the next course (--all).
    _UNRESOLVED_FILE_FETCHER = None
    _UNRESOLVED_PAGE_FETCHER = None
    if embed_stats["resolved"] or embed_stats["failed"]:
        logger.info("  embedded files: %s", embed_stats)

    # === Course-structure metadata pages (groups, conferences, collaborations,
    # outcomes, grading schemes); each written only when the course has data. ===
    extra_files: list[tuple[str, str]] = []
    with stage(course_label, "extras"):
        logger.info("== capture course extras (groups/conferences/etc.) ==")
        extra_files = archive_course_extras(
            api, cid, course_dir, course.get("name") or folder_name)
        if extra_files:
            logger.info("  extras: %s", [f for f, _ in extra_files])

    # === Browseable index pages (course root + every subfolder except sections) ===
    with stage(course_label, "indexes"):
        # Drop section folders that ended up with no content (e.g. a Quizzes/
        # folder for a course with no quizzes) before indexing, so the course
        # index never links to an empty, contentless folder.
        pruned = prune_empty_dirs(course_dir)
        if pruned:
            logger.info("  pruned %d empty folder(s): %s", len(pruned), pruned)
        logger.info("== write folder indexes ==")
        write_folder_indexes(course_dir, course.get("name") or folder_name, section_dirs,
                              unpublished_paths=unpublished_paths)
        # Overwrite the generic Assignments/ folder index with a Canvas-style
        # grouped index (groups, weights, every assignment incl. unpublished).
        assignments_dir.mkdir(parents=True, exist_ok=True)
        (assignments_dir / "index.html").write_text(
            render_assignments_index_html(
                course.get("name") or folder_name,
                assignment_groups, assignments, local_paths,
                apply_weights=bool(course.get("apply_assignment_group_weights")),
                include_unpublished=include_unpublished,
            ),
            encoding="utf-8")
        write_course_root_index(
            course_dir, course.get("name") or folder_name, course,
            {
                "assignments": stats,
                "discussions": d_stats,
                "embedded_files": embed_stats,
                "page_count": len(pages),
                "quiz_count": len(quizzes),
                "module_count": len(modules),
                "section_count": len(sections),
                "student_count": len(students),
                "assignment_count": len(assignments),
            },
            unpublished_paths=unpublished_paths,
            extra_files=extra_files,
        )

    if args.zip:
        zip_path = course_dir.with_suffix(".zip")
        logger.info("== zipping archive -> %s ==", zip_path.name)
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for p in sorted(course_dir.rglob("*")):
                if p.is_file():
                    zf.write(p, arcname=p.relative_to(course_dir.parent))
        logger.info("  zip size: %.1f MB", zip_path.stat().st_size / (1024 * 1024))
        shutil.rmtree(course_dir)
        logger.info("  removed source folder; zip is the only artifact")
        logger.info("\n== DONE ==\n  saved to: %s", zip_path)
        return 0

    logger.info("\n== DONE ==\n  saved to: %s", course_dir)
    return 0


def list_all_course_ids(api: "Canvas") -> list[tuple[int, str]]:
    """Return [(course_id, name), ...] for every course the cookie can see."""
    seen: dict[int, str] = {}
    states = ["available", "completed", "unpublished"]
    state_qs = "".join(f"&state[]={s}" for s in states)
    url = f"{api.base}/api/v1/courses?per_page=100{state_qs}"
    for c in api.get_paginated(url):
        try:
            cid = int(c["id"])
        except (KeyError, TypeError, ValueError):
            continue
        seen[cid] = c.get("name") or f"course_{cid}"
    return sorted(seen.items())


def _pick_course_interactive(api) -> int | None:
    print("\nLoading course list...")
    courses = list_all_course_ids(api)
    if not courses:
        print("No courses found. Check that your cookies are valid for this Canvas instance.")
        return None
    print(f"Found {len(courses)} courses:\n")
    for i, (cid, name) in enumerate(courses, 1):
        print(f"  [{i:>3}]  {cid:>10}  {name}")
    print()
    while True:
        choice = input("Enter a number (or 'q' to quit): ").strip().lower()
        if choice in {"q", "quit", ""}:
            return None
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(courses):
                return courses[idx - 1][0]
        print("  Please enter a number from the list, or 'q'.")


def interactive_banner() -> None:
    print()
    print("=" * 64)
    print("   Canvas Teacher Export   -   interactive mode")
    print("   archive your Canvas courses to local HTML / CSV / files")
    print("=" * 64)


# ----------------------------------------------------------------------------
# Local config persistence (~/.canvas-archive/config.json)
# ----------------------------------------------------------------------------

CONFIG_DIR = Path.home() / ".canvas-archive"
CONFIG_PATH = CONFIG_DIR / "config.json"

# All options the interactive UI lets the user set. Each key on args has a
# matching key here, so the wizard can iterate without missing flags.
PERSISTED_KEYS = (
    "base_url",
    "cookies",
    "output_root",
    "skip_files",
    "max_file_size_mb",
    "save_announcements",
    "include_unpublished",
    "skip_student_photos",
    "skip_av_submissions",
    "zip",
    "term_scheme",
    "workers",
)


def load_config() -> dict:
    """Read ~/.canvas-archive/config.json. Returns {} if missing or unreadable."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as ex:
        logger.warning("could not read config %s: %s", CONFIG_PATH, ex)
        return {}


def save_config(args) -> None:
    """Persist current args values for the keys the UI manages, so the next run
    starts from where the last one left off. Cookies are stored by path, not
    inline."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data = {}
        for k in PERSISTED_KEYS:
            v = getattr(args, k, None)
            if isinstance(v, Path):
                v = str(v)
            data[k] = v
        CONFIG_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        logger.info("config saved to %s", CONFIG_PATH)
    except OSError as ex:
        logger.warning("could not save config %s: %s", CONFIG_PATH, ex)


def apply_config_defaults(args, cfg: dict) -> None:
    """Use values in cfg as starting defaults on args. CLI args still win
    because argparse already populated args before this is called."""
    for k in PERSISTED_KEYS:
        if k not in cfg or cfg[k] is None:
            continue
        # Only override when the user did not supply a CLI value.
        # argparse defaults: paths -> Path, bools -> False, strings -> stored.
        if not getattr(args, f"_explicit_{k}", False):
            v = cfg[k]
            if k in {"cookies", "output_root"} and v:
                v = Path(v).expanduser()
            setattr(args, k, v)


COOKIE_EDITOR_URL = (
    "https://chromewebstore.google.com/detail/cookie-editor/"
    "hlkenndednhfkekhgcdicdfddnkalmdm"
)


def _print_cookie_editor_help(base_url: str) -> None:
    """Print step-by-step directions for exporting cookies with the free
    Cookie-Editor browser extension. Works in Chrome, Edge, and Firefox."""
    print()
    print("  How to get your Canvas cookies (one-time, ~30 seconds):")
    print("    1) Install the free Cookie-Editor extension:")
    print(f"       {COOKIE_EDITOR_URL}")
    print(f"    2) Log into Canvas in that same browser: {base_url}")
    print("       (make sure you can see your dashboard, then keep the tab open).")
    print("    3) On the Canvas tab, click the Cookie-Editor toolbar icon.")
    print("    4) Click the Export icon (bottom row) and choose 'Export as JSON'.")
    print("       This copies every cookie for the site to your clipboard.")
    print("    5) Come back here and paste it below (Ctrl+V, or Cmd+V on a Mac).")
    print("  Your cookies stay on this machine. Nothing is uploaded anywhere.")


# Strips ANSI / VT100 control sequences, including the bracketed-paste markers
# (ESC[200~ ... ESC[201~) that most modern terminals wrap pasted text in. Those
# markers otherwise leak into input() and corrupt a pasted JSON blob.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

# Catches bracketed-paste markers even when the ESC byte or the '[' was consumed
# on a separate read, leaving a literal fragment like '[200~' or '201~' glued to
# the JSON. The '~' makes this unambiguous: it never appears in a cookie value.
_PASTE_MARKER_RE = re.compile(r"\x1b?\[?20[01]~")


def _clean_pasted_text(buf: str) -> str:
    """Remove terminal escape sequences and bracketed-paste marker fragments
    from pasted text so the JSON underneath can be parsed."""
    return _PASTE_MARKER_RE.sub("", _ANSI_ESCAPE_RE.sub("", buf))


def _extract_cookie_array(buf: str):
    """Pull the JSON cookie array out of a pasted blob, tolerating whatever the
    terminal wrapped around it (bracketed-paste markers, stray escape codes,
    prompt echoes, leading/trailing whitespace).

    Cookie-Editor's 'Export as JSON' always produces a JSON array, so we strip
    control sequences and then slice from the first '[' to the last ']'. This
    works no matter how the terminal delivers the paste: one line, many lines,
    or the whole blob in a single read. Returns the parsed list, or None if a
    complete array is not present yet."""
    clean = _clean_pasted_text(buf)
    start = clean.find("[")
    end = clean.rfind("]")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        value = json.loads(clean[start:end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, list) else None


def _diagnose_cookie_paste(buf: str) -> str:
    """Return a one-line explanation of why a pasted blob did not parse, with a
    repr() window around the failure so hidden control characters are visible."""
    clean = _clean_pasted_text(buf)
    start = clean.find("[")
    end = clean.rfind("]")
    snippet = clean[start:end + 1] if (start != -1 and end != -1 and end > start) else clean
    try:
        json.loads(snippet)
        return "parsed cleanly on retry (transient)"
    except json.JSONDecodeError as ex:
        lo = max(0, ex.pos - 40)
        return f"{ex}; text near the error: {snippet[lo:ex.pos + 40]!r}"


def _paste_cookies_to_disk(dest: Path, base_url: str, show_help: bool = True) -> bool:
    """Read a pasted Cookie-Editor JSON blob from stdin and write it to dest.

    The paste is accepted automatically as soon as a complete JSON array is
    detected in the accumulated input. Because terminals deliver pastes in
    different and surprising ways, a blank-line Enter also submits whatever has
    been pasted so far, 'END' on its own line cancels, and the reader never
    silently hangs: it always tells the user what it is waiting for."""
    if show_help:
        _print_cookie_editor_help(base_url)
        print()
    # Pasting a multi-KB JSON blob into a terminal is unreliable over SSH and on
    # some terminals (a byte dropped inside the long session value corrupts the
    # JSON). Loading from a saved file is bulletproof, so offer any cookie JSON
    # files already sitting in the working directory as numbered choices.
    file_choices = sorted(Path.cwd().glob("*cookie*.json"))
    if file_choices:
        print("  Found these JSON files in the current folder (most reliable):")
        for i, p in enumerate(file_choices, 1):
            print(f"    {i}) {p.name}")
        print("  Enter a number to load one of those, OR")
    print("  Paste your Cookie-Editor JSON below, then press Enter on a blank line.")
    print("  (Accepted automatically once the full array is detected. You can also")
    print("   type the path to a saved .json file, or type END to cancel.)")
    print("  Tip: if pasting fails (common over SSH), save the JSON to a file and")
    print("  type its path or name here instead.")

    def _load_from_file(path: Path) -> bool:
        nonlocal parsed
        loaded = _extract_cookie_array(path.read_text(encoding="utf-8", errors="replace"))
        if loaded:
            parsed = loaded
            print(f"  Loaded {len(loaded)} cookies from {path}")
            return True
        print(f"  {path} does not contain a JSON cookie array.")
        return False

    lines: list[str] = []
    parsed = None
    while True:
        try:
            raw = input()
        except EOFError:
            break
        line = _ANSI_ESCAPE_RE.sub("", raw).replace("\r", "")
        stripped = line.strip()
        if stripped == "END":
            break
        # Numbered selection of a detected file.
        if file_choices and stripped.isdigit() and 1 <= int(stripped) <= len(file_choices):
            if _load_from_file(file_choices[int(stripped) - 1]):
                break
            continue
        # Escape hatch: a line that is just a path to an existing file (and not
        # the start of pasted JSON) is loaded directly. Bulletproof when paste
        # itself is being mangled by the terminal.
        if stripped and stripped[0] not in "[{\"":
            candidate_path = Path(stripped.strip('"').strip("'")).expanduser()
            if candidate_path.is_file():
                if _load_from_file(candidate_path):
                    break
                continue
        if stripped == "":
            # Blank Enter means "I'm done." Finish with whatever we have.
            candidate = _extract_cookie_array("\n".join(lines))
            if candidate:
                parsed = candidate
                break
            if "".join(lines).strip():
                print("  I don't see a complete JSON array yet.")
                print(f"  Reason: {_diagnose_cookie_paste(chr(10).join(lines))}")
                print("  Make sure you used Cookie-Editor's 'Export as JSON' (not")
                print("  Header String or Netscape). Paste again, type the path to a")
                print("  saved .json file, or type END to cancel.")
                lines = []
            else:
                print("  Waiting for your paste: paste the JSON, then press Enter.")
            continue
        lines.append(line)
        # Auto-accept the moment a complete array is present.
        candidate = _extract_cookie_array("\n".join(lines))
        if candidate:
            parsed = candidate
            break
    if parsed is None:
        parsed = _extract_cookie_array("\n".join(lines))
    if not isinstance(parsed, list) or not parsed:
        print("  ERROR: did not receive a non-empty JSON array of cookies.")
        return False
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(parsed, indent=2), encoding="utf-8")
    except OSError as ex:
        print(f"  ERROR: could not write to {dest}: {ex}")
        return False
    print(f"  Saved {len(parsed)} cookies to {dest}")
    return True


def interactive_connection(args) -> bool:
    """Confirm or override Canvas base URL and cookies. Mutates args. Supports
    pasting cookie JSON directly when no file is configured yet."""
    print()
    print("[Step 1/3] Connect to Canvas")
    print(f"   Canvas URL:    {args.base_url}")
    print(f"   Cookies file:  {args.cookies}")
    if input("  Use these settings? (Y/n): ").strip().lower().startswith("n"):
        url = input(f"  Canvas base URL [{args.base_url}]: ").strip()
        if url:
            args.base_url = url.rstrip("/")
        print("  Cookies source:")
        print("    1) Use a file on disk (default)")
        print("    2) Paste a Cookie-Editor JSON blob now (saved to a file)")
        src = input("  > ").strip()
        if src == "2":
            default_dest = CONFIG_DIR / "canvas-cookies.json"
            dest_in = input(f"  Save pasted cookies to [{default_dest}]: ").strip().strip('"').strip("'")
            dest = Path(dest_in).expanduser() if dest_in else default_dest
            if not _paste_cookies_to_disk(dest, args.base_url):
                return False
            args.cookies = dest
        else:
            ck = input(f"  Cookies JSON path [{args.cookies}]: ").strip().strip('"').strip("'")
            if ck:
                args.cookies = Path(ck).expanduser()
    if not args.cookies.exists():
        print(f"  No cookies file found at {args.cookies}")
        _print_cookie_editor_help(args.base_url)
        print()
        if input("  Paste your Cookie-Editor JSON now? (Y/n): ").strip().lower().startswith("n"):
            print("  Cannot connect without cookies. Re-run when you have them ready.")
            return False
        if not _paste_cookies_to_disk(args.cookies, args.base_url, show_help=False):
            return False
    return True


# Human-facing labels for the interactive term-scheme menu, in display order.
_TERM_SCHEME_MENU = (
    ("korean", "Korean school year (default): Mar-Jun = (1),\n       Sep-Dec = (2)."),
    ("us", "US semesters: Jan-May = (1) Spring, Jun-Jul = (2) Summer,\n       Aug-Dec = (3) Fall."),
    ("trimester", "Trimesters: Jan-Apr = (1), May-Aug = (2), Sep-Dec = (3)."),
    ("summer_winter", "Summer/Winter sessions: Dec-Feb = (1) Winter,\n       Jun-Aug = (2) Summer, other months = (3)."),
    ("none", "No term label: keep the course name as-is (no term or year added)."),
)


def _pick_term_scheme(current: str) -> str:
    """Menu for choosing how the academic term is recognized in folder names.
    Folders are labeled '<Course Name> (<term>) <year>' from each course's
    earliest assignment due date. Enter keeps the current value."""
    print("\n  How should course folders be labeled by term?")
    print("  Each course is sorted by the month of its earliest assignment.")
    print("  The number in parentheses is what appears in the folder name,")
    print("  e.g. \"Algebra I (1) 2026\".\n")
    for i, (key, label) in enumerate(_TERM_SCHEME_MENU, start=1):
        mark = " <- current" if key == current else ""
        print(f"    {i}) {label}{mark}")
    while True:
        ans = input(f"  Choose 1-{len(_TERM_SCHEME_MENU)} [{current}]: ").strip()
        if not ans:
            return current
        if ans.isdigit() and 1 <= int(ans) <= len(_TERM_SCHEME_MENU):
            return _TERM_SCHEME_MENU[int(ans) - 1][0]
        if ans in TERM_SCHEMES or ans == "none":
            return ans
        print(f"    Please enter a number 1-{len(_TERM_SCHEME_MENU)}.")


def interactive_setup(args, api) -> bool:
    """Prompt for archive scope + options. Mutates args. Returns False if user quits."""
    print()
    print("[Step 2/3] What do you want to archive?")
    print("  1) Every course your account can see")
    print("  2) One specific course (pick from a list)")
    print("  q) Quit")
    while True:
        choice = input("> ").strip().lower()
        if choice in {"1", "a", "all"}:
            args.all = True
            break
        if choice in {"2", "o", "one"}:
            cid = _pick_course_interactive(api)
            if cid is None:
                return False
            args.course_id = cid
            break
        if choice in {"q", "quit"}:
            return False
        print("  Please enter 1, 2, or q.")

    def _yn(prompt: str, default: bool) -> bool:
        suffix = " (Y/n): " if default else " (y/N): "
        ans = input(f"  {prompt}{suffix}").strip().lower()
        if not ans:
            return default
        return ans.startswith("y")

    def _print_settings() -> None:
        """The persisted options, one compact block. Scope (all vs one course)
        is chosen every run and is never part of this."""
        print(f"  output={args.output_root}/")
        print(f"  zip={args.zip}  announcements={args.save_announcements}  "
              f"include-unpublished={args.include_unpublished}")
        print(f"  skip-files={args.skip_files}  max-mb={args.max_file_size_mb}")
        print(f"  student-photos={not getattr(args, 'skip_student_photos', False)}  "
              f"av-submissions={not getattr(args, 'skip_av_submissions', True)}")
        print(f"  term-scheme={getattr(args, 'term_scheme', DEFAULT_TERM_SCHEME)}  "
              f"workers={getattr(args, 'workers', HTTP_WORKERS)}")

    def _ask_options() -> None:
        print("  A subfolder is created here for each course, named after the course "
              "(e.g. \"Algebra I (1) 2026\").")
        out = input(f"  Parent folder for course archives [{args.output_root}]: "
                    ).strip().strip('"').strip("'")
        if out:
            args.output_root = Path(out).expanduser()
        # Always default to No: most users want a browsable folder, and a saved
        # config should not silently flip this on.
        args.zip = _yn("Pack each course into its own .zip file?", False)
        # Default No (like zip): most teachers don't need announcements, and a
        # saved config should not silently flip this on.
        args.save_announcements = _yn("Include announcements?", False)
        args.include_unpublished = _yn(
            "Include unpublished items (assignments, pages, modules, etc.)?",
            args.include_unpublished,
        )
        args.skip_student_photos = not _yn(
            "Download student profile photos?",
            not getattr(args, "skip_student_photos", False),
        )
        args.skip_av_submissions = _yn(
            "Skip student audio/video submissions? (they are often very large)",
            bool(getattr(args, "skip_av_submissions", True)),
        )
        args.skip_files = _yn("Skip the Files section entirely?", args.skip_files)
        if args.skip_files:
            print("  WARNING: files attached or linked inside assignments, quizzes,")
            print("  discussions, and pages will NOT be downloaded. Those links will")
            print("  still point to Canvas, which stops working after the shutdown.")
        else:
            m = input(f"  Max file size in MB [{args.max_file_size_mb}]: ").strip()
            if m.isdigit():
                args.max_file_size_mb = int(m)
        args.term_scheme = _pick_term_scheme(
            getattr(args, "term_scheme", DEFAULT_TERM_SCHEME) or DEFAULT_TERM_SCHEME
        )
        cur_workers = getattr(args, "workers", HTTP_WORKERS) or HTTP_WORKERS
        print("\n  How many files to download at once? Higher is faster, but if")
        print("  Canvas starts rejecting requests, try a lower number. The default")
        print("  works well for most people.")
        w = input(f"  Downloads at once [{cur_workers}]: ").strip()
        if w.isdigit() and int(w) >= 1:
            args.workers = int(w)

    # If a config was saved on a previous run, show it and let the user keep it
    # as-is with a single keystroke instead of re-answering every prompt. Only
    # walk through the individual options on a first run or when they ask to
    # change something.
    changed = True
    if bool(load_config()):
        print("\n[Step 3/3] These settings are saved from last time:")
        _print_settings()
        print()
        if _yn("Change any of these?", False):
            print()
            _ask_options()
        else:
            changed = False
    else:
        print("\n[Step 3/3] Options (press Enter for default)")
        _ask_options()

    print()
    print("=" * 64)
    scope = "ALL courses your cookie can see" if args.all else f"course {args.course_id}"
    print(f"  Ready: {scope}")
    # The settings were just shown above when kept unchanged; only re-print the
    # full block when the user actually walked through the options.
    if changed:
        _print_settings()
    print("=" * 64)
    # Nothing to save when the user kept the existing config unchanged.
    if changed and _yn(
            "Save these settings to ~/.canvas-archive/config.json for next time?", True):
        save_config(args)
    input("Press Enter to start (or Ctrl+C to cancel)... ")
    return True


# ============================================================================
# Whole-account driver (the "--all" mode)
#
# Archiving every course your cookie can see is the unattended 80+ course run.
# Rather than loop in a single process, --all spawns one fresh subprocess of
# THIS script per course (python canvas_archive.py --course-id N), so a crash,
# hang, or out-of-memory in one course cannot take down the rest. It keeps a
# JSON manifest so you can stop and resume without redoing finished courses,
# retries transient failures with backoff, and aborts early if your session
# cookie appears to have expired. When the run finishes it builds the
# cross-course master index (see below) unless --no-index is given.
#
# State lives in <output-root>/_manifest.json. Per-course errors are also
# appended to <output-root>/_error.log by each subprocess.
# ============================================================================

# This very file; re-invoked once per course for crash isolation.
SCRIPT = Path(__file__).resolve()
MANIFEST_NAME = "_manifest.json"

# Status values stored per course in the manifest.
OK, FAILED, PENDING = "ok", "failed", "pending"


def load_manifest(output_root: Path) -> dict:
    """Read the run manifest, or return a fresh skeleton if absent/unreadable."""
    path = output_root / MANIFEST_NAME
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("courses"), dict):
                return data
        except (json.JSONDecodeError, OSError) as ex:
            logger.warning("could not read manifest %s: %s (starting fresh)", path, ex)
    return {"courses": {}}


def save_manifest(output_root: Path, manifest: dict, now: str) -> None:
    """Atomically write the manifest (temp file + os.replace) so an interrupted
    write can never corrupt a resumable run."""
    manifest["updated_at"] = now
    path = output_root / MANIFEST_NAME
    tmp = path.with_suffix(".json.tmp")
    try:
        output_root.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as ex:
        logger.warning("could not write manifest %s: %s", path, ex)


def plan_run(manifest: dict, course_list: list[tuple[int, str]],
             force: bool, only: set[int] | None) -> tuple[list[tuple[int, str]],
                                                           list[tuple[int, str]]]:
    """Split the course list into (to_run, to_skip).

    A course is skipped when it already has status 'ok' in the manifest, unless
    --force is given. --only restricts the run to the named ids (others skipped)."""
    to_run: list[tuple[int, str]] = []
    to_skip: list[tuple[int, str]] = []
    courses = manifest.get("courses", {})
    for cid, name in course_list:
        if only is not None and cid not in only:
            to_skip.append((cid, name))
            continue
        entry = courses.get(str(cid))
        already_ok = bool(entry) and entry.get("status") == OK
        if already_ok and not force:
            to_skip.append((cid, name))
        else:
            to_run.append((cid, name))
    return to_run, to_skip


def record_result(manifest: dict, cid: int, name: str, status: str,
                  rc: int | None, folder: str, error: str, now: str) -> dict:
    """Update (or create) the manifest entry for one course. Returns the entry."""
    courses = manifest.setdefault("courses", {})
    entry = courses.setdefault(str(cid), {"first_seen": now, "attempts": 0})
    entry["name"] = name
    entry["status"] = status
    entry["rc"] = rc
    entry["folder"] = folder or entry.get("folder", "")
    entry["error"] = error
    entry["attempts"] = int(entry.get("attempts", 0)) + 1
    entry["last_attempt"] = now
    return entry


def trailing_failures(statuses: list[str]) -> int:
    """How many of the most recent results were failures (run of FAILED at the
    tail). Used to abort a run whose cookie has clearly expired."""
    count = 0
    for status in reversed(statuses):
        if status == FAILED:
            count += 1
        else:
            break
    return count


def build_course_command(args, cid: int) -> list[str]:
    """Assemble the single-course command line (a subprocess of this script),
    passing through the per-course options set for the run."""
    cmd = [sys.executable, str(SCRIPT), "--course-id", str(cid),
           "--output-root", str(args.output_root),
           "--cookies", str(args.cookies),
           "--base-url", args.base_url,
           "--term-scheme", args.term_scheme,
           "--workers", str(args.workers),
           "--max-file-size-mb", str(args.max_file_size_mb)]
    for flag, enabled in (
        ("--skip-files", args.skip_files),
        ("--save-announcements", args.save_announcements),
        ("--include-unpublished", args.include_unpublished),
        ("--skip-student-photos", args.skip_student_photos),
        ("--include-av-submissions", not getattr(args, "skip_av_submissions", True)),
        ("--zip", args.zip),
        ("--verbose", getattr(args, "verbose", False)),
    ):
        if enabled:
            cmd.append(flag)
    return cmd


def _find_folder_for(output_root: Path, cid: int) -> str:
    """After a successful run, find the folder whose course_meta id matches cid.
    Returns the folder name, or '' (e.g. when --zip removed the folder)."""
    try:
        children = sorted(output_root.iterdir()) if output_root.is_dir() else []
    except OSError:
        return ""
    for child in children:
        if not child.is_dir():
            continue
        meta = read_course_meta(child)
        if meta and str((meta.get("course") or {}).get("id")) == str(cid):
            return child.name
    return ""


def run_one_course(args, cid: int, timeout: int) -> tuple[int | None, str]:
    """Run this script for one course in a fresh subprocess. Returns
    (returncode, error_text). returncode is None on timeout. Child stdout/stderr
    stream to this console so the user sees live progress."""
    cmd = build_course_command(args, cid)
    try:
        proc = subprocess.run(cmd, timeout=timeout)
        return proc.returncode, ("" if proc.returncode == 0 else f"exit code {proc.returncode}")
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout}s"
    except OSError as ex:
        return 1, f"failed to launch: {ex}"


def _connect(args) -> "Canvas":
    jar = load_cookies(args.cookies, args.base_url)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    return Canvas(args.base_url, opener, csrf=csrf_from_jar(jar))


def run_all(args) -> int:
    """Robust subprocess-per-course driver for the whole account. Enumerates
    every visible course, archives each in its own subprocess (resumable via the
    manifest, retried with backoff, aborting on a likely-expired cookie), then
    builds the cross-course master index unless --no-index."""
    output_root: Path = args.output_root
    output_root.mkdir(parents=True, exist_ok=True)
    init_error_log(output_root)

    logger.info("== enumerating courses ==")
    try:
        api = _connect(args)
        course_list = list_all_course_ids(api)
    except FileNotFoundError:
        logger.error("cookie file not found at %s -- export your Canvas cookies "
                     "with Cookie-Editor and pass --cookies <path> (or run with no "
                     "arguments to paste them in)", args.cookies)
        return 1
    except Exception as ex:  # noqa: BLE001 - top-level guard; report and stop.
        logger.error("could not list courses (cookie expired or wrong base-url?): %s", ex)
        return 1
    if not course_list:
        logger.error("no courses visible to this cookie; nothing to do "
                     "(check that the cookie is current for %s)", args.base_url)
        return 1
    logger.info("found %d courses", len(course_list))

    manifest = load_manifest(output_root)
    manifest.setdefault("base_url", args.base_url)
    manifest.setdefault("output_root", str(output_root))
    manifest.setdefault("started_at", _utc_now_iso())
    only = {int(x) for x in args.only.split(",") if x.strip().isdigit()} if args.only else None
    to_run, to_skip = plan_run(manifest, course_list, args.force, only)

    logger.info("plan: %d to archive, %d to skip (already done / filtered)",
                len(to_run), len(to_skip))
    if args.list_only:
        for cid, name in to_run:
            logger.info("  WILL ARCHIVE  %d  %s", cid, name)
        for cid, name in to_skip:
            logger.info("  skip          %d  %s", cid, name)
        return 0

    statuses: list[str] = []
    ok_count = fail_count = 0
    for idx, (cid, name) in enumerate(to_run, 1):
        logger.info("\n###### [%d/%d] %d  %s ######", idx, len(to_run), cid, name)
        rc, error = run_one_course(args, cid, args.timeout)
        attempt = 1
        while rc != 0 and attempt <= args.retries:
            wait = args.retry_wait * (2 ** (attempt - 1))
            logger.warning("  course %d failed (%s); retry %d/%d in %ds",
                           cid, error, attempt, args.retries, wait)
            time.sleep(wait)
            rc, error = run_one_course(args, cid, args.timeout)
            attempt += 1

        status = OK if rc == 0 else FAILED
        folder = _find_folder_for(output_root, cid) if status == OK else ""
        record_result(manifest, cid, name, status, rc, folder, error, _utc_now_iso())
        save_manifest(output_root, manifest, _utc_now_iso())
        statuses.append(status)
        if status == OK:
            ok_count += 1
        else:
            fail_count += 1
            logger.error("  FAILED: %d  %s  (%s)", cid, name, error)

        if trailing_failures(statuses) >= args.abort_after:
            logger.error("\n== ABORTING ==  %d courses in a row failed; your session "
                         "cookie has most likely expired. Re-export cookies and run "
                         "again (finished courses will be skipped).", args.abort_after)
            break

        if idx < len(to_run) and args.pause > 0:
            time.sleep(args.pause)

    logger.info("\n== RUN COMPLETE ==  ok: %d  failed: %d  skipped: %d",
                ok_count, fail_count, len(to_skip))
    for cid, name in to_run:
        entry = manifest["courses"].get(str(cid), {})
        if entry.get("status") == FAILED:
            logger.info("  FAILED: %d  %s  (%s)", cid, name, entry.get("error", ""))
    if _ERROR_LOG_PATH and _ERROR_LOG_PATH.exists():
        logger.info("  per-course errors: %s", _ERROR_LOG_PATH)
    logger.info("  manifest: %s", output_root / MANIFEST_NAME)

    if not args.no_index:
        logger.info("\n== building master course index ==")
        try:
            index_path = build_index(output_root, manifest)
            logger.info("  master index: %s", index_path)
        except Exception as ex:  # noqa: BLE001 - index is best-effort; never fail the run on it.
            logger.warning("  could not build master index: %s", ex)

    return 0 if fail_count == 0 else 1


# ============================================================================
# Master cross-course index
#
# Each course archive has its own browseable index.html carrying a
# machine-readable course_meta JSON island. The master index scans the output
# root, reads those islands, and writes one top-level index.html (+ a
# _courses.csv) listing every course grouped by term, with counts, status, and
# a link into each course folder. Pure post-processing: no network, no cookies.
# Built automatically at the end of an --all run, or on demand via
# --rebuild-index.
# ============================================================================

MASTER_INDEX = "index.html"
MASTER_CSV = "_courses.csv"

# Courses with no term get bucketed here; this label sorts to the end.
NO_TERM_GROUP = "No term"


def discover_course_dirs(output_root: Path) -> list[Path]:
    """Immediate subdirectories of output_root that look like a course archive
    (an index.html carrying a readable course_meta island)."""
    if not output_root.is_dir():
        return []
    try:
        children = sorted(output_root.iterdir())
    except OSError as ex:
        logger.warning("could not list %s: %s", output_root, ex)
        return []
    found: list[Path] = []
    for child in children:
        if not child.is_dir():
            continue
        if read_course_meta(child) is not None:
            found.append(child)
    return found


def _index_load_manifest(output_root: Path) -> dict:
    """Read an --all run manifest if present; {} otherwise. (Looser than
    load_manifest, which returns a {"courses": {}} skeleton for the driver.)"""
    path = output_root / MANIFEST_NAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as ex:
        logger.warning("could not read manifest %s: %s", path, ex)
        return {}
    return data if isinstance(data, dict) else {}


def _term_of(course: dict) -> str:
    term = (course.get("term") or {}).get("name") if isinstance(course.get("term"), dict) else ""
    return (term or "").strip() or NO_TERM_GROUP


def _row_from_meta(course_dir: Path, meta: dict) -> dict:
    """Normalize a parsed course_meta island into a flat row for rendering."""
    course = meta.get("course") or {}
    stats = meta.get("stats") or {}
    return {
        "id": str(course.get("id") or ""),
        "name": (course.get("name") or course_dir.name).strip(),
        "code": (course.get("course_code") or "").strip(),
        "term": _term_of(course),
        "status": (course.get("workflow_state") or "").strip(),
        "students": stats.get("student_count"),
        "assignments": stats.get("assignment_count"),
        "quizzes": stats.get("quiz_count"),
        "modules": stats.get("module_count"),
        "pages": stats.get("page_count"),
        "exported_at": meta.get("exported_at") or "",
        "folder": course_dir.name,
        "href": urllib.parse.quote(course_dir.name) + "/" + MASTER_INDEX,
    }


def _row_from_manifest(cid: str, entry: dict) -> dict:
    """Fallback row for a course present only in the manifest (e.g. failed, or
    stored as a .zip with no folder to scan)."""
    folder = entry.get("folder") or ""
    zip_name = f"{folder}.zip" if folder else ""
    href = ""
    if folder:
        href = urllib.parse.quote(folder) + "/" + MASTER_INDEX
    return {
        "id": str(cid),
        "name": (entry.get("name") or f"course {cid}").strip(),
        "code": "",
        "term": NO_TERM_GROUP,
        "status": entry.get("status") or "",
        "students": None,
        "assignments": None,
        "quizzes": None,
        "modules": None,
        "pages": None,
        "exported_at": entry.get("last_attempt") or "",
        "folder": folder,
        "href": href,
        "zip": zip_name,
        "manifest_status": entry.get("status") or "",
    }


def collect_rows(output_root: Path, manifest: dict | None = None) -> list[dict]:
    """Build the merged course list: rich rows from scanned folders, plus any
    manifest-only courses (failed / zip-only) that have no folder to scan."""
    rows: list[dict] = []
    seen_ids: set[str] = set()
    seen_folders: set[str] = set()
    for course_dir in discover_course_dirs(output_root):
        meta = read_course_meta(course_dir)
        if meta is None:
            continue
        row = _row_from_meta(course_dir, meta)
        rows.append(row)
        seen_folders.add(row["folder"])
        if row["id"]:
            seen_ids.add(row["id"])

    courses = (manifest or {}).get("courses") or {}
    for cid, entry in courses.items():
        if not isinstance(entry, dict):
            continue
        if str(cid) in seen_ids:
            continue
        if entry.get("folder") and entry["folder"] in seen_folders:
            continue
        rows.append(_row_from_manifest(str(cid), entry))
    return rows


def _group_and_sort(rows: list[dict]) -> list[tuple[str, list[dict]]]:
    """Group rows by term, sort courses by name within a group, and order groups
    alphabetically with the 'No term' bucket pinned last."""
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["term"], []).append(row)
    for items in groups.values():
        items.sort(key=lambda r: (r["name"].lower(), r["id"]))

    def group_key(name: str) -> tuple[int, str]:
        return (1, "") if name == NO_TERM_GROUP else (0, name.lower())

    return [(name, groups[name]) for name in sorted(groups, key=group_key)]


def _count_note(row: dict) -> str:
    bits: list[str] = []
    if row.get("students") is not None:
        bits.append(f"{row['students']} students")
    if row.get("assignments") is not None:
        bits.append(f"{row['assignments']} assignments")
    if row.get("code"):
        bits.append(row["code"])
    if row.get("exported_at"):
        bits.append(f"archived {fmt_iso(row['exported_at'])}")
    if not row.get("href") and row.get("zip"):
        bits.append(f"in {row['zip']}")
    return " · ".join(bits)


# Map Canvas workflow_state / manifest status to a reused .badge CSS class.
_STATUS_BADGE = {
    "available": "graded",
    "completed": "submitted",
    "unpublished": "unsubmitted",
    "ok": "graded",
    "failed": "missing",
    "deleted": "missing",
}


def render_master_index(output_root: Path, rows: list[dict]) -> str:
    """Render the master index HTML, grouping courses by term. Reuses the same
    html_doc shell/CSS as the per-course pages for a consistent look."""
    root_label = output_root.name or str(output_root)
    parts = [
        f'<h1>Course archive index <small>({len(rows)} courses)</small></h1>',
        f'<p class="csv-link"><a href="{attr(MASTER_CSV)}">{MASTER_CSV}</a> '
        f'(machine-readable list)</p>',
    ]
    if not rows:
        parts.append(
            '<section><p>No archived courses found in this folder. '
            'Run <code>canvas_archive.py</code> first.</p></section>'
        )
    for term, items in _group_and_sort(rows):
        lis = []
        for row in items:
            badge = _STATUS_BADGE.get(row.get("status") or "", "")
            badge_html = (
                f' <span class="badge {attr(badge)}">{html.escape(row["status"])}</span>'
                if row.get("status") else ""
            )
            note = _count_note(row)
            note_html = f' <span class="note">{html.escape(note)}</span>' if note else ""
            label = html.escape(row["name"])
            if row.get("href"):
                link = f'<a href="{attr(row["href"])}">{label}</a>'
            else:
                # Failed / zip-only course: no folder index to link to.
                link = f'<span class="entry-deleted">{label}</span>'
            lis.append(
                f'<li data-course-id="{attr(row["id"])}" '
                f'data-term="{attr(term)}" data-status="{attr(row.get("status"))}">'
                f'{link}{badge_html}{note_html}</li>'
            )
        parts.append(
            f'<section data-section="term" data-term="{attr(term)}">'
            f'<h2>{html.escape(term)} <small>({len(items)})</small></h2>'
            f'<ul class="index-list">\n' + "\n".join(lis) + '\n</ul></section>'
        )

    island = (
        '<script type="application/json" data-section="course_archive_index">'
        + html.escape(json.dumps({"root": root_label, "courses": rows,
                                  "generated_at": _utc_now_iso()}, default=str),
                      quote=False)
        + '</script>'
    )
    parts.append(island)
    return html_doc(
        f"Course archive index ({len(rows)})",
        "canvas-archive/course-archive-index/v1",
        "\n".join(parts),
    )


_CSV_COLS = ["id", "name", "code", "term", "status", "students", "assignments",
             "quizzes", "modules", "pages", "exported_at", "folder"]


def write_master_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(_CSV_COLS)
        for row in sorted(rows, key=lambda r: (r["term"], r["name"].lower())):
            writer.writerow([row.get(c, "") if row.get(c) is not None else "" for c in _CSV_COLS])


def build_index(output_root: Path, manifest: dict | None = None) -> Path:
    """Write index.html + _courses.csv into output_root. Returns the index path."""
    if manifest is None:
        manifest = _index_load_manifest(output_root)
    rows = collect_rows(output_root, manifest)
    output_root.mkdir(parents=True, exist_ok=True)
    index_path = output_root / MASTER_INDEX
    # Write atomically (temp + os.replace) so an interrupted write never leaves a
    # half-written index.html in place of the previous good one.
    tmp = index_path.with_suffix(".html.tmp")
    tmp.write_text(render_master_index(output_root, rows), encoding="utf-8")
    os.replace(tmp, index_path)
    write_master_csv(output_root / MASTER_CSV, rows)
    logger.info("wrote %s (%d courses) and %s", index_path, len(rows), MASTER_CSV)
    return index_path


def build_parser() -> argparse.ArgumentParser:
    """All command-line flags for every mode: a single course (--course-id), the
    whole account (--all), and index-only (--rebuild-index). One parser means a
    per-course flag is defined exactly once and can never drift between the
    single-course run and the whole-account driver."""
    p = argparse.ArgumentParser(
        description="Archive Canvas LMS courses to local HTML/CSV/files. "
                    "Run without --course-id, --all, or --rebuild-index to use "
                    "the interactive prompt."
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--course-id", type=int, help="archive a single course by id")
    g.add_argument("--all", action="store_true",
                   help="archive every course the cookie can see (resumable; one "
                        "subprocess per course; builds the master index at the end)")
    g.add_argument("--rebuild-index", action="store_true",
                   help="rebuild the master index.html + _courses.csv from the "
                        "already-archived course folders, then exit (no network)")
    p.add_argument("--cookies", type=Path,
                   default=Path.home() / "canvas-archive" / "canvas-cookies.json")
    p.add_argument("--base-url", default="https://canvas.instructure.com")
    p.add_argument("--output-root", type=Path, default=Path("./archive"))
    p.add_argument("--skip-files", action="store_true",
                   help="do not download any files, including ones attached or "
                        "linked inside assignments, quizzes, discussions, and pages")
    p.add_argument("--max-file-size-mb", type=int, default=100,
                   help="skip any individual file larger than this many MB (default 100)")
    p.add_argument("--save-announcements", action="store_true",
                   help="archive course announcements (off by default)")
    p.add_argument("--include-unpublished", action="store_true",
                   help="archive unpublished items (assignments, pages, modules, "
                        "discussions, quizzes, files); off by default")
    p.add_argument("--skip-student-photos", action="store_true",
                   help="skip downloading student profile photos; the roster "
                        "renders initials instead (photos download by default)")
    p.add_argument("--include-av-submissions", dest="skip_av_submissions",
                   action="store_false", default=True,
                   help="also download student audio/video submissions; skipped "
                        "by default because recorded media files are often very "
                        "large")
    p.add_argument("--zip", action="store_true",
                   help="pack each course into its own <course folder>.zip and "
                        "delete the unpacked folder (one .zip per course)")
    p.add_argument("--workers", type=int, default=HTTP_WORKERS,
                   help=f"number of parallel download/render threads "
                        f"(default {HTTP_WORKERS}; lower it, e.g. 8 or 4, if Canvas "
                        f"returns 503s)")
    p.add_argument("--term-scheme", choices=TERM_SCHEME_CHOICES, default=DEFAULT_TERM_SCHEME,
                   help="how to recognize the academic term in folder names from a "
                        "course's earliest due date: korean (default; Spring=1 Feb-Jul, "
                        "Fall=2 Aug-Dec, Jan=prior-year fall), us (Spring/Summer/Fall), "
                        "trimester, summer_winter, or none (no term tag)")
    p.add_argument("--verbose", "-v", action="store_true")

    # --- whole-account driver options (only take effect with --all) ---
    drv = p.add_argument_group("whole-account options (used with --all)")
    drv.add_argument("--only", default="",
                     help="comma-separated course ids to archive (others skipped)")
    drv.add_argument("--force", action="store_true",
                     help="re-archive courses already marked done in the manifest")
    drv.add_argument("--retries", type=int, default=2,
                     help="retries per course on failure (default 2)")
    drv.add_argument("--retry-wait", type=int, default=5,
                     help="base seconds between retries; doubles each retry (default 5)")
    drv.add_argument("--pause", type=int, default=1,
                     help="seconds to pause between courses (default 1)")
    drv.add_argument("--timeout", type=int, default=3600,
                     help="per-course hard timeout in seconds (default 3600)")
    drv.add_argument("--abort-after", type=int, default=5,
                     help="abort the whole run after this many consecutive course "
                          "failures (likely expired cookie); default 5")
    drv.add_argument("--no-index", action="store_true",
                     help="do not build the cross-course master index at the end")
    drv.add_argument("--list-only", action="store_true",
                     help="print the plan (what would run / skip) and exit")
    return p


def main() -> int:
    # Set ARGV-explicit markers BEFORE argparse runs so apply_config_defaults
    # can tell apart "user did not pass --foo" from "user passed the default".
    _argv = set(sys.argv[1:])
    args = build_parser().parse_args()

    # Mark which flags the user explicitly passed on the command line. These
    # win over saved config. Long-form only is checked; close enough for our
    # interactive-first audience.
    flag_to_dest = {
        "--base-url": "base_url",
        "--cookies": "cookies",
        "--output-root": "output_root",
        "--skip-files": "skip_files",
        "--max-file-size-mb": "max_file_size_mb",
        "--save-announcements": "save_announcements",
        "--include-unpublished": "include_unpublished",
        "--skip-student-photos": "skip_student_photos",
        "--include-av-submissions": "skip_av_submissions",
        "--zip": "zip",
        "--term-scheme": "term_scheme",
        "--workers": "workers",
    }
    for flag, dest in flag_to_dest.items():
        if any(a == flag or a.startswith(flag + "=") for a in _argv):
            setattr(args, f"_explicit_{dest}", True)

    # Layer saved config under CLI defaults.
    apply_config_defaults(args, load_config())

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # --rebuild-index is pure post-processing: no cookie, no network.
    if args.rebuild_index:
        if not args.output_root.is_dir():
            logger.error("output root does not exist: %s", args.output_root)
            return 1
        index_path = build_index(args.output_root)
        logger.info("open it: %s", index_path)
        return 0

    interactive_mode = (args.course_id is None and not args.all
                        and not args.rebuild_index)

    # Non-interactive --all: hand straight to the robust driver, which does its
    # own cookie loading and prints a friendly error if the file is missing.
    if args.all and not interactive_mode:
        return run_all(args)

    if interactive_mode:
        interactive_banner()
        if not interactive_connection(args):
            return 1

    try:
        jar = load_cookies(args.cookies, args.base_url)
    except (OSError, json.JSONDecodeError) as ex:
        logger.error("could not read cookie file %s: %s", args.cookies, ex)
        print(f"\nERROR: could not read your cookie file at {args.cookies}\n"
              f"Export your Canvas cookies as JSON with Cookie-Editor, then point "
              f"at the file with --cookies <path> (or run with no arguments to "
              f"paste them in).")
        return 1
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    api = Canvas(args.base_url, op, csrf=csrf_from_jar(jar))
    if not api.csrf:
        logger.warning("no _csrf_token cookie found; rich-text comments via "
                       "GraphQL will be skipped (REST plaintext fallback used)")

    if interactive_mode:
        if not interactive_setup(args, api):
            print("Cancelled.")
            return 0
        # Interactive "every course" choice: same robust driver as --all.
        if args.all:
            return run_all(args)

    # Set up the single global error log under the output root.
    init_error_log(args.output_root)

    try:
        return archive_one(args, api, args.course_id)
    except Exception as ex:
        log_error(f"course {args.course_id}", "archive_one", ex)
        return 1


if __name__ == "__main__":
    sys.exit(main())
