# Canvas Teacher Export

> This was quickly developed for saving my own courses due to the Canvas Free for Teachers abrupt termination, and may not archive every element important to your courses, but I hope it serves you well.

Bail out of your Canvas account before it disappears.

Instructure is shutting down **Canvas Free For Teachers on May 29, 2026 1pm ET**. If you taught on a FFT account, every gradebook, discussion thread, assignment submission, file upload, page, and quiz you built lives behind that login until the lights go out. This tool gets it out as a human-readable, machine-parseable archive on your disk before that happens.

It also works against any other Canvas instance (paid institutional, self-hosted, beta, whatever) as long as you can log into it in a browser.

<!--
  GitHub topics to set on this repo (Settings -> Topics). Keep lowercase and hyphenated:
  canvas-lms, canvas, instructure, canvas-free-for-teachers, fft, course-export,
  course-archive, lms-export, lms-migration, data-export, gradebook, backup,
  edtech, teacher-tools, education, web-scraping, python, stdlib, cli
-->

*Keywords: export Canvas Free For Teachers courses before shutdown, download all my Canvas courses, Canvas FFT backup, save Canvas gradebook and submissions, Canvas LMS course archive, migrate Canvas course data, Instructure account export.*

## Why cookies and not an API token

Funny story.

Instructure revoked API token generation on Free For Teachers accounts "for security." Which would be a reasonable call if browser sessions weren't still allowed to call `/api/v1/*` with cookies. Any actual hacker would have noticed in about five minutes. The only people who got locked out were teachers trying to migrate their own course data on the way out.

So that's what this tool does. It takes the exact cookies your browser already uses, talks to the exact same JSON API Instructure left wide open, and pulls down everything you'd see if you clicked through every page by hand.

No tokens. No OAuth dance. No third-party servers. Your cookies stay on your machine. Pure Python stdlib, zero dependencies.

## Authentication and API equivalence (for the record)

This section exists because some users of this tool need the resulting archive to stand up as evidence: in court, in academic appeals, in disputes with their employer. Here is exactly what the tool does and does not do, on the technical record.

### One API, three auth methods

Canvas LMS exposes a single REST API rooted at `/api/v1/*`. Per Canvas's own published API documentation (`canvas.instructure.com/doc/api/`) and the open-source `canvas-lms` codebase on GitHub (AGPL-licensed, `github.com/instructure/canvas-lms`), that API accepts three authentication mechanisms, any of which can be used to reach the same endpoints:

1. **Bearer token**, sent as `Authorization: Bearer <token>`. This is the "API access token" that Instructure revoked on Free For Teachers accounts.
2. **OAuth2 access token**, used by third-party integrations going through Canvas's developer-key flow.
3. **Session cookie**, the cookie issued by the standard browser login flow.

All three hit the same controllers, return the same JSON schemas, and operate under the same permission scoping. A session cookie carries the same effective privileges as a personal access token belonging to the same user. That equivalence is intentional, because Canvas's own web UI heavily calls the same `/api/v1/*` endpoints to populate dashboards, gradebooks, modules, and discussion threads. Cookie authentication has to remain functional against the API for Canvas's own pages to work.

### What this tool does

This tool sends `GET` requests to documented `/api/v1/*` endpoints, carrying the user's own session cookie that Canvas issued to the user's own browser when the user logged in. The HTTP requests are functionally indistinguishable from what the browser sends when the user clicks anywhere inside Canvas. The same controllers run, the same authorization checks fire, the same JSON returns.

### What this tool does not do

- It does not exploit any vulnerability.
- It does not bypass any access control. Every authorization check is enforced by Canvas itself, not by this tool.
- It does not impersonate another user, escalate privileges, or read any data the logged-in user could not already read by clicking around in their browser.
- It does not modify any data on Canvas. Every request is `GET`.
- It does not transmit cookies, credentials, or course content to any third party. There is no telemetry, no remote dependency, no outbound traffic except to your own Canvas host.

### Why "we revoked API tokens for security" does not describe a security boundary

A security boundary, properly defined, is something that, when removed, lets new actors do new things. The Bearer-token path and the cookie path reach the same endpoints with the same permissions. Removing Bearer-token issuance while leaving cookie authentication functional therefore does not contract the attack surface in any meaningful way. An attacker who has compromised an account has the same access either way. The only added step for them is logging in through `/login` instead of clicking "New Access Token" in the settings page, which is, to be clear, not a step.

What removing Bearer-token issuance does accomplish: it makes it materially harder for the legitimate owner of an account to script their own data out. Teachers who built years of course material on Free For Teachers lose the documented, supported, auditable migration path. The undocumented path (this tool, or any equivalent script) still works precisely because it had to keep working for Canvas's own UI to keep working.

In other words, the door Instructure said it closed is still open. This tool walks through it carrying the user's own keys.

### Evidentiary summary

The archive produced by this tool is the same data that would have come out of a personal access token on the same account: same endpoints, same JSON shapes, same authorization rules. The only difference is the auth header on the request. Where an investigator needs to verify a specific record, every HTML in the archive embeds both a JSON island (`<script type="application/json" data-section="...">`, for example `data-section="raw_submission"`) containing the structured source data and `data-*` attributes on rendered elements, so the rendered view, the embedded JSON, and the original `/api/v1/*` response can be cross-checked against each other.

## What you get out

One folder per course, named like `Example Course (1) 2026/`. Inside:

```
Example Course (1) 2026/
├── index.html                   ← course home: links to everything below
├── Students.csv                 ← roster with sections
├── Students/
│   ├── Students.html            ← roster with profile photos, sections, emails
│   └── avatars/                 ← downloaded profile photos (real ones only)
├── Assignments.csv              ← one row per assignment
├── Gradebook.csv                ← student x assignment score matrix
├── Syllabus.html                ← description + grading weights
├── Modules.html                 ← Canvas module sequence, links resolved locally
├── Assignments/
│   ├── index.html               ← mirrors the Canvas Assignments page:
│   │                              grouped by assignment group, weights shown,
│   │                              every assignment listed (unpublished marked)
│   └── Persuasive Essay/
│       ├── Persuasive Essay.html    ← overview: description, rubric, roster
│       ├── Jane Doe.html            ← one student: grade, comments, rubric, body
│       ├── Jane Doe Essay.docx
│       └── _grades.csv
├── Discussions/
│   └── Week 1 Introductions.html
├── Files/                       ← original Canvas folder tree preserved exactly
├── Pages/
├── Quizzes/                     ← same layout as Assignments: one folder per quiz
│   └── Quiz 1/
│       ├── Quiz 1.html          ← questions, correct answers, and a per-student score table
│       └── _grades.csv          ← per-student, per-question results
└── Announcements/               ← only with --save-announcements
```

Sections with no content in a given course are skipped. A handful of less common
elements are captured as top-level pages **only when the course has them**:
`Groups.html`, `Conferences.html`, `Collaborations.html`, `Outcomes.html`,
`GradingSchemes.html`. New Quizzes (Quizzes.Next), when present, land in
`Quizzes/` as `<name> (New Quiz).html`.

Every HTML file ships with two layers for machine parsing later:

1. A `<script type="application/json" data-section="...">` JSON island holding the full structured record (for example `data-section="raw_submission"` on a student page, `data-section="course_meta"` on the course `index.html`).
2. `data-*` attributes on rendered elements (`data-score`, `data-author-id`, `data-criterion-id`, `data-chosen`, etc.) so DOM scrapers work too.

Course folder names that don't already include a year get one auto-derived from the earliest assignment's due date, using the convention `<base> (<term>) <YYYY>`. How months map to a term number is configurable (interactive prompt, or `--term-scheme`):

| Scheme | Term mapping |
|--------|--------------|
| `korean` (default) | `(1)` Spring = Feb-Jul, `(2)` Fall = Aug-Dec, January = prior year's `(2)` |
| `us` | `(1)` Spring = Jan-May, `(2)` Summer = Jun-Jul, `(3)` Fall = Aug-Dec |
| `trimester` | `(1)` Jan-Apr, `(2)` May-Aug, `(3)` Sep-Dec |
| `summer_winter` | `(1)` Winter = Dec-Feb, `(2)` Summer = Jun-Aug, `(3)` regular otherwise |
| `none` | no term marker added |

A course whose name already contains a `(N) YYYY` marker is left as-is.

## Setup

The tool is a single Python file with zero dependencies. There is no `pip install`, no virtualenv, no build step. You need three things: Python, the one script, and a cookie file.

### 1. Get Python (3.10 or newer)

Check whether you already have it. Open a terminal and run:

- **Windows 11** (PowerShell): `python --version`
- **macOS** (Terminal): `python3 --version`
- **Linux** (Terminal): `python3 --version`

If it prints `Python 3.10` or higher, you are set. If not:

- **Windows 11:** install from the Microsoft Store (search "Python 3.12") or [python.org/downloads](https://www.python.org/downloads/). During the python.org installer, tick **"Add python.exe to PATH"**.
- **macOS:** `brew install python` if you use Homebrew, otherwise grab the installer from [python.org/downloads](https://www.python.org/downloads/). macOS ships an older Python; installing a current one is recommended.
- **Linux:** it is almost certainly already installed. If not, `sudo apt install python3` (Debian/Ubuntu) or your distro's equivalent.

Then get the script. It is a single file, `canvas_archive.py`, with no dependencies beyond Python itself. Download it from this repo into a folder you can find, for example your Desktop, or clone the whole repo with `git clone`. That one file does everything: one course, every course, and the master index.

### 2. Export your Canvas cookies

You log into Canvas in your browser; this tool reuses that same session. Export the cookies for your Canvas host (`canvas.instructure.com`, or your institution's host) as JSON with the free Cookie-Editor extension.

**Chrome / Edge / Brave:**
1. Install [Cookie-Editor](https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm).
2. Log into Canvas in the browser.
3. While on a Canvas page, click the Cookie-Editor toolbar icon.
4. Click the **Export** button at the bottom, choose **JSON**.
5. Save the result as a file named `canvas-cookies.json`. By default the tool looks for it at `~/canvas-archive/canvas-cookies.json` (that is, a `canvas-archive` folder in your home directory), so either save it there or save it anywhere and point at it with `--cookies <path>`. If you run interactively, you can skip the file entirely and paste the JSON when prompted; the tool saves it to the default location for you.

**Firefox:** install [Cookie-Editor for Firefox](https://addons.mozilla.org/firefox/addon/cookie-editor/) and follow the same steps.

Just export everything; Cookie-Editor's "Export as JSON" grabs the whole set and the tool uses what it needs. The one cookie that actually grants access is `canvas_session` (your login session). `_csrf_token`, if present, additionally lets the tool fetch rich-text discussion and comment bodies via Canvas's GraphQL endpoint; without it those fall back to plain text and everything else still works.

> **Cookies expire.** If a run starts failing with `401` partway through, log back into Canvas, re-export the cookie file over the old one, and run again. The cookie file is the only auth surface in the whole tool, so rotating credentials is exactly that easy.

### 3. Run it

The easiest way is to run with no arguments and answer the prompts. It will ask for your Canvas URL and cookie file (you can even paste the cookie JSON directly), let you pick one course or all of them, set options, and offer to remember your settings for next time.

**Windows 11** (PowerShell, from the folder holding the script):
```powershell
python canvas_archive.py
```

**macOS / Linux** (Terminal, from the folder holding the script):
```bash
python3 canvas_archive.py
```

That is all most people need. The interactive prompt walks you through everything.

#### How long it takes

Roughly **one minute per course**, give or take, depending on how many files, submissions, and students it has. A few courses is a coffee break. A full account of 80+ courses runs over an hour, which is exactly why [`--all`](#archiving-many-courses-resumable) below is resumable: start it, walk away, and if it stops (laptop sleeps, cookie expires, network drops) just run it again and it picks up where it left off.

## What it does *not* do

- It does not upload to a new LMS for you. This is an archive tool, not a migrator. If you want to land somewhere else, the JSON islands inside each HTML are designed to make that import script straightforward to write.
- It does not download conference (BigBlueButton) recordings. Those are typically not retrievable through the API; the conference metadata is captured, the recordings are not.
- It does not modify anything on Canvas. Read-only end to end.

## Untested and best-effort features

This tool was built and verified against a real Free For Teachers course that used classic quizzes, a single Files tree, sections, rubrics, discussions, pages, and modules. Those paths are exercised on every run and are solid.

The following features are implemented but have **not** been verified against real course data, because the test course did not contain them. They are written defensively (they skip cleanly when the data is absent and isolate any error to their own section), but treat their output as best-effort until you confirm it against your own course:

- **New Quizzes (Quizzes.Next).** Detected as LTI assignments. The tool attempts to pull per-question content from the New Quizzes API and falls back to a shell page (linking the assignment view and gradebook score) when that API is not reachable with session cookies, which is the common case. Student scores are always captured via the normal gradebook path.
- **Per-student quiz results.** Always archived: each quiz page gets a per-student score table grouped by section (the same way the assignment roster is split), and a `_grades.csv` records each student's section and per-question answer, whether it was correct, and points earned. Verified for classic quizzes (multiple-choice, true/false, and similar option-based questions). Other classic question types (numerical, formula, matching, file-upload) are captured generically and the recorded answer text may be less precise.
- **Group sets / group membership, Conferences, Collaborations, Outcomes, and custom Grading schemes.** Each is written as a metadata page only when the course actually has that data. The page layouts have not been seen against real data and may need adjustment.

## Running with flags instead of prompts

Once you know what you want, you can skip the prompts. Substitute `python` on Windows for `python3` below.

```bash
# Archive one course by id (good for a first test on a small course)
python3 canvas_archive.py --course-id 123456

# Archive every course your cookie can see
python3 canvas_archive.py --all

# Cookie file saved somewhere other than the default location
python3 canvas_archive.py --all --cookies ~/Desktop/canvas-cookies.json

# Institutional or self-hosted Canvas: point at your host
python3 canvas_archive.py --all --base-url https://yourschool.instructure.com
```

Useful flags:

| Flag | Effect |
|---|---|
| `--course-id <id>` | Archive a single course. Mutually exclusive with `--all`. |
| `--all` | Archive every course the cookie can see. |
| `--cookies <path>` | Cookie-Editor JSON export. Default `~/canvas-archive/canvas-cookies.json`. |
| `--base-url <url>` | Canvas root. Default `https://canvas.instructure.com`. |
| `--output-root <dir>` | Where course folders are written. Default `./archive`. |
| `--include-unpublished` | Include unpublished assignments, pages, modules, etc. Off by default. |
| `--save-announcements` | Archive course announcements. Off by default. |
| `--skip-student-photos` | Do not download student profile photos; the roster shows initials instead. Photos download by default. |
| `--include-av-submissions` | Also download student audio/video submissions. Skipped by default because recorded media files are often very large. |
| `--skip-files` | Do not download any files, including ones attached or linked inside assignments, quizzes, discussions, and pages. Those links stay pointed at Canvas (which stops working after the shutdown). |
| `--max-file-size-mb <n>` | Skip any single file larger than this. Default 100. |
| `--zip` | Pack each course into its own `.zip` (one per course) and delete the unpacked folder. Note: the master index cannot link into zipped courses. |
| `--term-scheme <name>` | How folder names are dated: `korean` (default), `us`, `trimester`, `summer_winter`, or `none`. See [Folder naming](#what-you-get-out) above. |
| `--workers <n>` | Parallel download/render threads (default 16). Lower it (e.g. `8` or `4`) if Canvas returns 503s. |

Settings you save through the interactive prompt are written to `~/.canvas-archive/config.json` and loaded automatically on the next run. On that next run the tool shows your saved settings in one block and asks "Change any of these?" (default No), so you can keep everything with a single Enter instead of stepping through every question again. Anything you pass on the command line overrides the saved value.

Python 3.10+ is enough. No `pip install`, no venv. The whole thing is the standard library.

### Archiving many courses (resumable)

For a whole account (dozens of courses), `--all` runs a resumable driver: it archives **one course per subprocess**, so a single course that hangs, crashes, or runs out of memory cannot take down the rest of the run. It records progress to `<output-root>/_manifest.json`, so you can stop and rerun and it **skips courses already finished**. It retries transient failures with backoff and aborts early (assuming an expired cookie) if several courses fail in a row.

You get this by choosing "every course" in the interactive prompt, or by running `canvas_archive.py --all`:

```bash
# Archive everything, resumably, to ./archive
python3 canvas_archive.py --all --cookies ~/canvas-cookies.json

# Run it again later: finished courses are skipped, the rest resume
python3 canvas_archive.py --all --cookies ~/canvas-cookies.json

# See the plan without doing anything
python3 canvas_archive.py --all --cookies ~/canvas-cookies.json --list-only

# Only specific courses, or force a re-archive of finished ones
python3 canvas_archive.py --all --only 123,456
python3 canvas_archive.py --all --force
```

`--all` accepts all the per-course flags above (`--output-root`, `--workers`, `--include-unpublished`, `--skip-student-photos`, `--zip`, and so on) and passes them through to each course. Whole-account-only flags: `--retries` (default 2), `--timeout` (per-course seconds, default 3600), `--pause` (seconds between courses), `--abort-after` (consecutive failures before giving up, default 5), and `--no-index` (skip the master index below).

### Browse everything from one page

When `--all` finishes it builds a master `index.html` at the top of your output folder that lists every archived course grouped by term, with student and assignment counts and a link into each course. You can also (re)build it any time, straight from the folders already on disk:

```bash
python3 canvas_archive.py --rebuild-index --output-root ./archive
```

This is pure post-processing (no network, no cookies). It also writes a machine-readable `_courses.csv` next to the index.

## License

MIT. Use it, fork it, ship it to your colleagues before the shutdown.
