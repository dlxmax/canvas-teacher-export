"""Unit tests for the cross-course master index builder, the shared course_meta
island parser (read_course_meta), and the resumable whole-account driver's pure
planning helpers. All of these now live in canvas_archive.py (the driver and the
index builder were merged into the single main script).

Run with: pytest tests/
"""
import csv
import json

import pytest

import canvas_archive as ac

# Backwards-compatible aliases: the driver (formerly archive_all.py) and the
# index builder (formerly build_course_index.py) were merged into canvas_archive.
archive_all = ac
bci = ac


# ---------------------------------------------------------------------------
# Fixtures: synthetic archived-course folders carrying a real course_meta island
# ---------------------------------------------------------------------------

def _make_course_dir(root, folder, cid, name, *, term="", code="",
                     status="available", students=0, assignments=0):
    """Write a course folder whose index.html holds a course_meta island, using
    the real writer so the test exercises the true round-trip."""
    course_dir = root / folder
    course_dir.mkdir(parents=True, exist_ok=True)
    course = {
        "id": cid, "name": name, "course_code": code,
        "workflow_state": status,
        "term": {"name": term} if term else None,
        "start_at": None, "end_at": None,
    }
    stats = {"student_count": students, "assignment_count": assignments,
             "quiz_count": 0, "module_count": 0, "page_count": 0}
    ac.write_course_root_index(course_dir, name, course, stats)
    return course_dir


# ---------------------------------------------------------------------------
# read_course_meta
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestReadCourseMeta:
    def test_round_trip(self, tmp_path):
        d = _make_course_dir(tmp_path, "2025-Spring-Math", 42, "Algebra",
                             term="2025 Spring", code="MATH101",
                             students=12, assignments=5)
        meta = ac.read_course_meta(d)
        assert meta is not None
        assert meta["course"]["id"] == 42
        assert meta["course"]["name"] == "Algebra"
        assert meta["stats"]["student_count"] == 12

    def test_name_with_html_metacharacters_survives(self, tmp_path):
        # Ampersands/angle brackets are escaped on write; the parser must recover
        # the original, and a literal </script> in the name must not break it.
        tricky = 'Reading & Writing </script><b>x</b>'
        d = _make_course_dir(tmp_path, "tricky", 7, tricky)
        meta = ac.read_course_meta(d)
        assert meta["course"]["name"] == tricky

    def test_missing_index_returns_none(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        assert ac.read_course_meta(empty) is None

    def test_index_without_island_returns_none(self, tmp_path):
        d = tmp_path / "noisland"
        d.mkdir()
        (d / "index.html").write_text("<html><body>nope</body></html>", encoding="utf-8")
        assert ac.read_course_meta(d) is None


# ---------------------------------------------------------------------------
# build_course_index: discovery, grouping, rendering, CSV
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestBuildCourseIndex:
    def test_discovers_only_course_folders(self, tmp_path):
        _make_course_dir(tmp_path, "courseA", 1, "A")
        (tmp_path / "not-a-course").mkdir()
        (tmp_path / "_error.log").write_text("x", encoding="utf-8")
        found = bci.discover_course_dirs(tmp_path)
        assert [p.name for p in found] == ["courseA"]

    def test_collect_rows_and_group_order(self, tmp_path):
        _make_course_dir(tmp_path, "c1", 1, "Banana", term="2025 Fall")
        _make_course_dir(tmp_path, "c2", 2, "Apple", term="2025 Fall")
        _make_course_dir(tmp_path, "c3", 3, "Orphan", term="")
        rows = bci.collect_rows(tmp_path)
        assert len(rows) == 3
        grouped = bci._group_and_sort(rows)
        # "No term" bucket is pinned last; within a group, sorted by name.
        assert grouped[0][0] == "2025 Fall"
        assert [r["name"] for r in grouped[0][1]] == ["Apple", "Banana"]
        assert grouped[-1][0] == bci.NO_TERM_GROUP

    def test_manifest_only_course_listed_without_link(self, tmp_path):
        _make_course_dir(tmp_path, "done", 1, "Done Course", term="2025")
        manifest = {"courses": {
            "999": {"name": "Crashed Course", "status": "failed",
                    "folder": "", "last_attempt": "2026-01-01T00:00:00Z"},
        }}
        rows = bci.collect_rows(tmp_path, manifest)
        names = {r["name"]: r for r in rows}
        assert "Crashed Course" in names
        assert names["Crashed Course"]["href"] == ""  # no folder -> not linkable

    def test_build_index_writes_files(self, tmp_path):
        _make_course_dir(tmp_path, "c1", 1, "Alpha", term="2025 Spring",
                         students=3, assignments=2)
        index_path = bci.build_index(tmp_path)
        assert index_path.exists()
        html_text = index_path.read_text(encoding="utf-8")
        assert "Alpha" in html_text
        assert 'href="c1/index.html"' in html_text
        assert "2025 Spring" in html_text
        csv_text = (tmp_path / bci.MASTER_CSV).read_text(encoding="utf-8")
        assert "Alpha" in csv_text and "2025 Spring" in csv_text

    def test_empty_root_renders_placeholder(self, tmp_path):
        index_path = bci.build_index(tmp_path)
        assert "No archived courses found" in index_path.read_text(encoding="utf-8")

    def test_index_island_is_machine_readable(self, tmp_path):
        _make_course_dir(tmp_path, "c1", 1, "Alpha", term="2025 Spring")
        bci.build_index(tmp_path)
        html_text = (tmp_path / "index.html").read_text(encoding="utf-8")
        # extract the index island the same way read_course_meta does
        import re
        m = re.search(r'data-section="course_archive_index">(.*?)</script>',
                      html_text, re.DOTALL)
        assert m is not None
        import html as _h
        data = json.loads(_h.unescape(m.group(1)))
        assert data["courses"][0]["name"] == "Alpha"


# ---------------------------------------------------------------------------
# prune_empty_dirs (drops contentless section folders like an empty Quizzes/)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestPruneEmptyDirs:
    def test_removes_empty_folder(self, tmp_path):
        (tmp_path / "Quizzes").mkdir()
        (tmp_path / "Pages").mkdir()
        (tmp_path / "Discussions").mkdir()
        (tmp_path / "Discussions" / "Week 1.html").write_text("x", encoding="utf-8")
        removed = ac.prune_empty_dirs(tmp_path)
        assert "Quizzes" in removed and "Pages" in removed
        assert not (tmp_path / "Quizzes").exists()
        assert (tmp_path / "Discussions").exists()  # has a file -> kept

    def test_nested_empty_parent_removed(self, tmp_path):
        # Files/EmptySub/ with nothing in it -> both Files and EmptySub go.
        (tmp_path / "Files" / "EmptySub").mkdir(parents=True)
        removed = ac.prune_empty_dirs(tmp_path)
        assert not (tmp_path / "Files").exists()
        assert "Files/EmptySub" in removed

    def test_folder_with_deep_file_is_kept(self, tmp_path):
        deep = tmp_path / "Files" / "Unit 1"
        deep.mkdir(parents=True)
        (deep / "handout.pdf").write_text("x", encoding="utf-8")
        ac.prune_empty_dirs(tmp_path)
        assert (deep / "handout.pdf").exists()

    def test_course_root_never_removed(self, tmp_path):
        ac.prune_empty_dirs(tmp_path)
        assert tmp_path.exists()


# ---------------------------------------------------------------------------
# Course root index: per-section "note" counts
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestRootIndexSectionCounts:
    def test_students_note_uses_student_count_not_child_count(self, tmp_path):
        # Students/ holds only the roster page + an avatars/ subfolder (child
        # count 2), so the note must come from stats["student_count"], not the
        # folder's child count.
        (tmp_path / "Students" / "avatars").mkdir(parents=True)
        (tmp_path / "Students" / "Students.html").write_text("x", encoding="utf-8")
        (tmp_path / "Assignments" / "A1").mkdir(parents=True)
        (tmp_path / "Assignments" / "A2").mkdir(parents=True)
        course = {"id": 1, "name": "Course", "workflow_state": "available",
                  "term": None}
        ac.write_course_root_index(
            tmp_path, "Course", course,
            {"student_count": 37, "assignment_count": 2})
        html_text = (tmp_path / "index.html").read_text(encoding="utf-8")
        assert "37 students" in html_text          # real roster size, not "2 entries"
        # Other sections still report their child count.
        assert "2 entries" in html_text            # Assignments/ has 2 folders

    def test_students_note_falls_back_when_count_missing(self, tmp_path):
        (tmp_path / "Students" / "avatars").mkdir(parents=True)
        (tmp_path / "Students" / "Students.html").write_text("x", encoding="utf-8")
        course = {"id": 1, "name": "Course", "workflow_state": "available",
                  "term": None}
        ac.write_course_root_index(tmp_path, "Course", course, {})  # no student_count
        html_text = (tmp_path / "index.html").read_text(encoding="utf-8")
        assert "2 entries" in html_text            # graceful fallback to child count


# ---------------------------------------------------------------------------
# Quiz folder layout: each quiz must mirror an assignment
# (Quizzes/<stem>/<stem>.html + _grades.csv), so the folder is self-indexed
# (no stray index.html) and the Quizzes index links the quiz once, straight to
# the page that carries the student-score table.
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestQuizFolderLayout:
    def _make_quiz_folder(self, root):
        quizzes = root / "Quizzes"
        quizzes.mkdir()
        stem = "Quiz 5- Why cant we stop"
        qdir = quizzes / stem
        qdir.mkdir()
        quiz = {"id": 1, "title": stem, "points_possible": 3, "question_count": 1}
        scores = ('<section data-section="quiz_scores" data-count="1">'
                  '<h2>Student scores</h2>'
                  '<p class="csv-link"><a href="_grades.csv">_grades.csv</a></p>'
                  '<table><tbody><tr><td>Kim</td><td>3 / 3</td></tr></tbody></table>'
                  '</section>')
        (qdir / (stem + ".html")).write_text(
            ac.render_quiz_html("Course", quiz, [], {}, submissions_html=scores),
            encoding="utf-8")
        (qdir / "_grades.csv").write_text("student_id,score\n1,3\n", encoding="utf-8")
        return quizzes, qdir, stem

    def test_page_is_folder_self_index(self, tmp_path):
        _, qdir, stem = self._make_quiz_folder(tmp_path)
        assert ac._named_index_for(qdir) == stem + ".html"

    def test_walker_writes_no_stray_index(self, tmp_path):
        _, qdir, _ = self._make_quiz_folder(tmp_path)
        ac.write_folder_indexes(tmp_path, "Course", set(), set())
        assert not (qdir / "index.html").exists()

    def test_quizzes_index_links_quiz_once_to_page(self, tmp_path):
        quizzes, _, stem = self._make_quiz_folder(tmp_path)
        ac.write_folder_indexes(tmp_path, "Course", set(), set())
        import re
        hrefs = re.findall(r'href="([^"]+)"',
                           (quizzes / "index.html").read_text(encoding="utf-8"))
        quoted = stem.replace(" ", "%20")
        quiz_links = [h for h in hrefs if quoted in h]
        assert quiz_links == [f"{quoted}/{quoted}.html"]

    def test_page_has_scores_and_relative_links(self, tmp_path):
        _, qdir, stem = self._make_quiz_folder(tmp_path)
        page = (qdir / (stem + ".html")).read_text(encoding="utf-8")
        assert 'data-section="quiz_scores"' in page       # score table present
        assert 'href="../index.html"' in page             # up-link to Quizzes index
        assert 'href="_grades.csv"' in page               # CSV is a sibling


# ---------------------------------------------------------------------------
# Quiz score table: grouped by section, like the assignment roster
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestQuizScoreSectionGrouping:
    def _run(self, tmp_path, monkeypatch, user_to_section):
        # Fake the network fetch: 3 students, fixed scores, no per-question data.
        def fake_fetch(api, cid, qid):
            return ([
                {"user_id": 1, "score": 3, "quiz_points_possible": 3,
                 "kept_score": 3, "attempt": 1, "finished_at": "",
                 "workflow_state": "complete"},
                {"user_id": 2, "score": 2, "quiz_points_possible": 3,
                 "kept_score": 2, "attempt": 1, "finished_at": "",
                 "workflow_state": "complete"},
                {"user_id": 3, "score": 1, "quiz_points_possible": 3,
                 "kept_score": 1, "attempt": 1, "finished_at": "",
                 "workflow_state": "complete"},
            ], {})
        monkeypatch.setattr(ac, "fetch_classic_quiz_submissions", fake_fetch)
        students_by_id = {
            "1": {"id": 1, "name": "Alice", "sortable_name": "Alice"},
            "2": {"id": 2, "name": "Bob", "sortable_name": "Bob"},
            "3": {"id": 3, "name": "Cara", "sortable_name": "Cara"},
        }
        quizzes_dir = tmp_path / "Quizzes"
        quizzes_dir.mkdir()
        qstats = {"students_recorded": 0, "quizzes_with_submissions": 0}
        html_out = ac._archive_quiz_submissions(
            None, 1, {"id": 10, "title": "Q"}, [], students_by_id,
            user_to_section, quizzes_dir, "Q", "Course", {}, qstats)
        return html_out, quizzes_dir

    def test_table_split_by_section(self, tmp_path, monkeypatch):
        html_out, _ = self._run(
            tmp_path, monkeypatch,
            {"1": "F-1pm", "2": "W-3pm", "3": "F-1pm"})
        # One sub-table per section, with a per-section header + student count.
        assert html_out.count("<tbody>") == 2
        assert 'data-section-label="F-1pm"' in html_out
        assert 'data-section-label="W-3pm"' in html_out
        assert "F-1pm <small>(2 students)</small>" in html_out
        assert "W-3pm <small>(1 students)</small>" in html_out
        # Empty section bucket would sort first; here both rows carry a section.
        assert html_out.index("F-1pm") < html_out.index("W-3pm")

    def test_csv_has_section_column(self, tmp_path, monkeypatch):
        _, quizzes_dir = self._run(
            tmp_path, monkeypatch, {"1": "F-1pm", "2": "W-3pm", "3": "F-1pm"})
        rows = list(csv.reader((quizzes_dir / "Q" / "_grades.csv").open()))
        header = rows[0]
        assert header[2] == "section"
        sec_by_name = {r[1]: r[2] for r in rows[1:]}
        assert sec_by_name["Alice"] == "F-1pm"
        assert sec_by_name["Bob"] == "W-3pm"

    def test_no_section_info_falls_back_to_all_students(self, tmp_path, monkeypatch):
        html_out, _ = self._run(tmp_path, monkeypatch, {})  # nobody has a section
        assert html_out.count("<tbody>") == 1
        assert "All students <small>(3 students)</small>" in html_out


# ---------------------------------------------------------------------------
# archive_all: pure planning helpers
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestArchiveAllPlanning:
    def test_plan_skips_completed(self):
        manifest = {"courses": {"1": {"status": "ok"}}}
        course_list = [(1, "Done"), (2, "Todo")]
        to_run, to_skip = archive_all.plan_run(manifest, course_list, force=False, only=None)
        assert to_run == [(2, "Todo")]
        assert to_skip == [(1, "Done")]

    def test_force_reruns_completed(self):
        manifest = {"courses": {"1": {"status": "ok"}}}
        to_run, to_skip = archive_all.plan_run(manifest, [(1, "Done")], force=True, only=None)
        assert to_run == [(1, "Done")] and to_skip == []

    def test_only_filter(self):
        manifest = {"courses": {}}
        course_list = [(1, "A"), (2, "B"), (3, "C")]
        to_run, to_skip = archive_all.plan_run(manifest, course_list, force=False, only={2})
        assert to_run == [(2, "B")]
        assert sorted(to_skip) == [(1, "A"), (3, "C")]

    def test_failed_course_is_retried_on_next_run(self):
        manifest = {"courses": {"1": {"status": "failed"}}}
        to_run, _ = archive_all.plan_run(manifest, [(1, "A")], force=False, only=None)
        assert to_run == [(1, "A")]  # only 'ok' is skipped

    def test_record_result_increments_attempts(self):
        manifest = {"courses": {}}
        archive_all.record_result(manifest, 5, "X", "failed", 1, "", "boom", "t1")
        archive_all.record_result(manifest, 5, "X", "ok", 0, "X-folder", "", "t2")
        entry = manifest["courses"]["5"]
        assert entry["attempts"] == 2
        assert entry["status"] == "ok"
        assert entry["folder"] == "X-folder"

    def test_trailing_failures(self):
        assert archive_all.trailing_failures([]) == 0
        assert archive_all.trailing_failures(["ok", "failed", "failed"]) == 2
        assert archive_all.trailing_failures(["failed", "ok"]) == 0
        assert archive_all.trailing_failures(["failed", "failed", "failed"]) == 3

    def test_build_course_command_passthrough(self):
        args = archive_all.build_parser().parse_args([
            "--output-root", "/tmp/out", "--workers", "8",
            "--include-unpublished", "--skip-student-photos",
        ])
        cmd = archive_all.build_course_command(args, 321)
        assert "--course-id" in cmd and "321" in cmd
        assert "--include-unpublished" in cmd
        assert "--skip-student-photos" in cmd
        assert "--zip" not in cmd  # not enabled -> omitted
        assert "8" in cmd  # workers passed through
        # A/V submissions are skipped by default -> opt-in flag not forwarded.
        assert "--include-av-submissions" not in cmd

    def test_build_course_command_forwards_av_optin(self):
        args = archive_all.build_parser().parse_args([
            "--output-root", "/tmp/out", "--include-av-submissions",
        ])
        cmd = archive_all.build_course_command(args, 5)
        assert "--include-av-submissions" in cmd

    def test_save_and_load_manifest_round_trip(self, tmp_path):
        manifest = {"courses": {"1": {"status": "ok", "name": "A"}}}
        archive_all.save_manifest(tmp_path, manifest, "now")
        loaded = archive_all.load_manifest(tmp_path)
        assert loaded["courses"]["1"]["status"] == "ok"
        assert loaded["updated_at"] == "now"

    def test_load_manifest_missing_returns_skeleton(self, tmp_path):
        loaded = archive_all.load_manifest(tmp_path)
        assert loaded == {"courses": {}}


# ---------------------------------------------------------------------------
# interactive_setup: when a config is already saved, show it and let the user
# keep it with one keystroke instead of re-answering every prompt.
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestInteractiveSetupSavedConfig:
    def _args(self):
        a = type("A", (), {})()
        a.all = False
        a.course_id = None
        a.output_root = ac.Path("/tmp/keepme")
        a.zip = False
        a.save_announcements = True
        a.include_unpublished = True
        a.skip_student_photos = False
        a.skip_av_submissions = True
        a.skip_files = False
        a.max_file_size_mb = 100
        a.term_scheme = "korean"
        a.workers = 8
        return a

    def _drive(self, monkeypatch, cfg, feed):
        """Run interactive_setup with load_config -> cfg and stdin -> feed.
        Returns (args, list_of_prompts_shown, save_count)."""
        monkeypatch.setattr(ac, "load_config", lambda: cfg)
        saves = []
        monkeypatch.setattr(ac, "save_config", lambda args: saves.append(args))
        prompts = []
        it = iter(feed)
        monkeypatch.setattr(
            "builtins.input",
            lambda p="": (prompts.append(p), next(it))[1],
        )
        a = self._args()
        ok = ac.interactive_setup(a, None)
        assert ok is True
        return a, prompts, len(saves)

    def test_keep_saved_skips_option_prompts_and_does_not_resave(self, monkeypatch, capsys):
        # scope=1 (all), "Change any of these?" -> Enter (default No), start -> Enter.
        a, prompts, saves = self._drive(
            monkeypatch, {"zip": False, "workers": 8}, ["1", "", ""])
        joined = " ".join(prompts)
        assert "Change any of these?" in joined          # the keep/change gate is shown
        assert "Parent folder" not in joined             # no per-option prompts walked
        assert "Max file size" not in joined
        assert saves == 0                                # unchanged -> nothing re-saved
        assert str(a.output_root) == "/tmp/keepme"       # settings untouched
        # The settings block is shown once (the "saved from last time" header) and
        # NOT repeated in the Ready summary, which only confirms the scope.
        out = capsys.readouterr().out
        assert out.count("term-scheme=korean") == 1
        assert "Ready: ALL courses" in out

    def test_change_saved_walks_options_and_saves(self, monkeypatch, capsys):
        # scope=1, change? y, then Enter through every option, save? Enter (Yes), start.
        a, prompts, saves = self._drive(
            monkeypatch, {"zip": False},
            ["1", "y"] + [""] * 12)
        joined = " ".join(prompts)
        assert "Parent folder" in joined                 # options were walked
        assert saves == 1                                # changed -> saved
        # When walked, the Ready summary re-prints the full block as confirmation.
        out = capsys.readouterr().out
        assert out.count("term-scheme=korean") == 2

    def test_first_run_no_config_walks_options(self, monkeypatch, capsys):
        # No saved config: go straight to options, no "Change any?" gate.
        a, prompts, saves = self._drive(monkeypatch, {}, ["1"] + [""] * 12)
        joined = " ".join(prompts)
        assert "Change any of these?" not in joined
        assert "Parent folder" in joined
        assert "Save these settings" in joined
        # First run prints the settings only in the Ready summary (no saved-header).
        out = capsys.readouterr().out
        assert out.count("term-scheme=korean") == 1
