"""Unit tests for the pure (no network, no filesystem) helpers in canvas_archive.

These lock the behaviour of the formatting, naming, publish-state, pagination,
and link-neutralization helpers so future edits cannot silently regress them.
Run with: pytest tests/
"""
import pytest

import canvas_archive as ac


# ---------------------------------------------------------------------------
# parse_next_link
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestParseNextLink:
    def test_none_and_empty(self):
        assert ac.parse_next_link(None) is None
        assert ac.parse_next_link("") is None

    def test_extracts_next(self):
        header = (
            '<https://x/api?page=1>; rel="current", '
            '<https://x/api?page=2>; rel="next", '
            '<https://x/api?page=9>; rel="last"'
        )
        assert ac.parse_next_link(header) == "https://x/api?page=2"

    def test_no_next_returns_none(self):
        header = '<https://x/api?page=9>; rel="last"'
        assert ac.parse_next_link(header) is None

    def test_strips_angle_brackets_and_whitespace(self):
        assert ac.parse_next_link(' <https://x/api?p=2> ; rel="next" ') == "https://x/api?p=2"


# ---------------------------------------------------------------------------
# safe_name
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSafeName:
    def test_replaces_path_and_reserved_chars(self):
        assert ac.safe_name("a/b\\c:d?e") == "a-b-c-d-e"

    def test_collapses_whitespace(self):
        assert ac.safe_name("hello   world") == "hello world"

    def test_strips_trailing_dots_and_spaces(self):
        assert ac.safe_name("  name. ") == "name"

    def test_empty_falls_back_to_underscore(self):
        assert ac.safe_name("") == "_"
        assert ac.safe_name(None) == "_"
        assert ac.safe_name("   ") == "_"
        assert ac.safe_name(". .") == "_"

    def test_truncates_to_max_len(self):
        out = ac.safe_name("x" * 200, max_len=10)
        assert len(out) == 10

    def test_truncation_strips_trailing_space(self):
        out = ac.safe_name("abcdefghi " + "j" * 50, max_len=10)
        assert not out.endswith(" ")

    def test_colon_section_name_is_windows_safe(self):
        # Section names like "JRW R12: A01307202" crashed mkdir on Windows
        # (WinError 267); the colon must be neutralised for the folder.
        out = ac.safe_name("JRW R12: A01307202")
        assert out == "JRW R12- A01307202"
        assert ":" not in out


# ---------------------------------------------------------------------------
# _module_item_local_href  (module + inline links must target a real file,
# never a bare folder -- regression guard for the split_mode discussion bug
# where the link pointed at "Discussions/" and opened a directory listing)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestModuleItemLocalHref:
    def test_discussion_resolves_to_html_file(self):
        local_paths = {("Discussion", "55"): "Discussions/Five-shot sequence.html"}
        it = {"type": "Discussion", "content_id": 55}
        href = ac._module_item_local_href(it, local_paths)
        assert href is not None
        assert href.endswith(".html")
        assert not href.endswith("/")  # must not be a bare folder
        assert "%20" in href  # spaces are percent-encoded

    def test_assignment_and_quiz_resolve(self):
        local_paths = {
            ("Assignment", "1"): "Assignments/Essay/Essay.html",
            ("Quiz", "2"): "Quizzes/Quiz 1.html",
        }
        assert ac._module_item_local_href(
            {"type": "Assignment", "content_id": 1}, local_paths).endswith("Essay.html")
        assert ac._module_item_local_href(
            {"type": "Quiz", "content_id": 2}, local_paths).endswith(".html")

    def test_page_resolves_by_slug(self):
        local_paths = {("Page", "intro"): "Pages/Intro.html"}
        it = {"type": "Page", "page_url": "intro"}
        assert ac._module_item_local_href(it, local_paths) == "Pages/Intro.html"

    def test_missing_target_returns_none(self):
        assert ac._module_item_local_href(
            {"type": "Discussion", "content_id": 999}, {}) is None

    def test_unknown_kind_returns_none(self):
        assert ac._module_item_local_href({"type": "SubHeader"}, {}) is None

    def test_discussion_without_content_id_returns_none(self):
        assert ac._module_item_local_href({"type": "Discussion"}, {}) is None


# ---------------------------------------------------------------------------
# derive_course_folder_name  (Korean academic calendar)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestDeriveCourseFolderName:
    def test_keeps_existing_semester_marker(self):
        course = {"name": "Example Course (1) 2026", "id": 1}
        assert ac.derive_course_folder_name(course, []) == "Example Course (1) 2026"

    def test_keeps_existing_multi_digit_term_marker(self):
        # Marker detection accepts any term number, not just (1)/(2).
        course = {"name": "Workshop (3) 2024", "id": 1}
        assert ac.derive_course_folder_name(course, []) == "Workshop (3) 2024"

    def test_no_assignments_no_marker_uses_bare_name(self):
        course = {"name": "Sandbox", "id": 1}
        assert ac.derive_course_folder_name(course, []) == "Sandbox"

    def test_spring_month_is_semester_1(self):
        # March -> semester 1, same year
        course = {"name": "Sample Course", "id": 1}
        assignments = [{"due_at": "2026-03-10T00:00:00Z"}]
        assert ac.derive_course_folder_name(course, assignments) == "Sample Course (1) 2026"

    def test_autumn_month_is_semester_2_same_year(self):
        # September -> semester 2, same year
        course = {"name": "Sample Course", "id": 1}
        assignments = [{"due_at": "2025-09-02T00:00:00Z"}]
        assert ac.derive_course_folder_name(course, assignments) == "Sample Course (2) 2025"

    def test_january_rolls_back_to_prior_year_semester_2(self):
        # January -> semester 2 of the previous year (deep winter break)
        course = {"name": "Sample Course", "id": 1}
        assignments = [{"due_at": "2026-01-15T00:00:00Z"}]
        assert ac.derive_course_folder_name(course, assignments) == "Sample Course (2) 2025"

    def test_february_is_semester_1_same_year(self):
        # Spring can start late Feb -> semester 1, same year (corrected boundary)
        course = {"name": "Sample Course", "id": 1}
        assignments = [{"due_at": "2026-02-25T00:00:00Z"}]
        assert ac.derive_course_folder_name(course, assignments) == "Sample Course (1) 2026"

    def test_july_is_semester_1(self):
        # July (summer break after spring) folds into semester 1
        course = {"name": "Sample Course", "id": 1}
        assignments = [{"due_at": "2026-07-10T00:00:00Z"}]
        assert ac.derive_course_folder_name(course, assignments) == "Sample Course (1) 2026"

    def test_august_is_semester_2_same_year(self):
        # Fall can start late Aug -> semester 2, same year (corrected boundary)
        course = {"name": "Sample Course", "id": 1}
        assignments = [{"due_at": "2025-08-28T00:00:00Z"}]
        assert ac.derive_course_folder_name(course, assignments) == "Sample Course (2) 2025"

    def test_uses_earliest_due_date(self):
        course = {"name": "Sample Course", "id": 1}
        assignments = [
            {"due_at": "2026-07-01T00:00:00Z"},
            {"due_at": "2026-03-01T00:00:00Z"},
        ]
        assert ac.derive_course_folder_name(course, assignments) == "Sample Course (1) 2026"

    def test_falls_back_to_course_code_then_id(self):
        assert ac.derive_course_folder_name({"course_code": "CS101", "id": 7}, []) == "CS101"
        assert ac.derive_course_folder_name({"id": 7}, []) == "course_7"

    def test_ignores_unparseable_dates(self):
        course = {"name": "Sample Course", "id": 1}
        assignments = [{"due_at": "not-a-date"}]
        assert ac.derive_course_folder_name(course, assignments) == "Sample Course"


# ---------------------------------------------------------------------------
# derive_course_folder_name  (alternate term schemes)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestTermSchemes:
    @staticmethod
    def _name(month: int, scheme: str, year: int = 2026) -> str:
        course = {"name": "C", "id": 1}
        assignments = [{"due_at": f"{year}-{month:02d}-15T00:00:00Z"}]
        return ac.derive_course_folder_name(course, assignments, scheme)

    @pytest.mark.parametrize("month,expected", [
        (3, "C (1) 2026"),    # Spring
        (7, "C (2) 2026"),    # Summer
        (8, "C (3) 2026"),    # Fall
        (12, "C (3) 2026"),
    ])
    def test_us_scheme(self, month, expected):
        assert self._name(month, "us") == expected

    @pytest.mark.parametrize("month,expected", [
        (4, "C (1) 2026"),
        (5, "C (2) 2026"),
        (9, "C (3) 2026"),
    ])
    def test_trimester_scheme(self, month, expected):
        assert self._name(month, "trimester") == expected

    def test_summer_winter_scheme(self):
        assert self._name(7, "summer_winter") == "C (2) 2026"       # Summer
        assert self._name(12, "summer_winter") == "C (1) 2026"      # Winter (Dec)
        assert self._name(1, "summer_winter") == "C (1) 2025"       # Winter (Jan -> prior yr)
        assert self._name(4, "summer_winter") == "C (3) 2026"       # regular fallback

    def test_none_scheme_skips_tagging(self):
        course = {"name": "Sample Course", "id": 1}
        assignments = [{"due_at": "2026-03-10T00:00:00Z"}]
        assert ac.derive_course_folder_name(course, assignments, "none") == "Sample Course"

    def test_unknown_scheme_falls_back_to_default(self):
        course = {"name": "Sample Course", "id": 1}
        assignments = [{"due_at": "2026-03-10T00:00:00Z"}]
        assert ac.derive_course_folder_name(course, assignments, "bogus") == "Sample Course (1) 2026"

    def test_every_scheme_covers_all_twelve_months(self):
        for key, table in ac.TERM_SCHEMES.items():
            assert set(table.keys()) == set(range(1, 13)), f"{key} missing months"


# ---------------------------------------------------------------------------
# _normalize_embedded_term  ("YYYY.N" / "YYYY-N" titles -> canonical marker)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestEmbeddedTermNormalization:
    def test_dot_separator_trailing(self):
        assert ac._normalize_embedded_term(
            "Intro to Biology 2015.1"
        ) == "Intro to Biology (1) 2015"

    def test_hyphen_separator(self):
        assert ac._normalize_embedded_term("Calculus 2020-2") == "Calculus (2) 2020"

    def test_token_at_start(self):
        assert ac._normalize_embedded_term("2015.1 English") == "English (1) 2015"

    def test_no_token_returns_none(self):
        assert ac._normalize_embedded_term("Plain Course Name") is None

    def test_three_digit_number_is_not_a_year(self):
        # "101.2" must not be read as year.semester.
        assert ac._normalize_embedded_term("Math 101.2") is None

    def test_term_out_of_range_ignored(self):
        # Second component must be a plausible term (1-4).
        assert ac._normalize_embedded_term("Survey 2019.2024") is None

    def test_normalization_wins_over_date_derivation(self):
        # Title's own year/term is trusted, not the earliest due date.
        course = {"name": "Intro to Biology 2015.1", "id": 1}
        assignments = [{"due_at": "2015-09-09T00:00:00Z"}]  # Sept would derive (2)
        assert ac.derive_course_folder_name(course, assignments) == \
            "Intro to Biology (1) 2015"

    def test_canonical_marker_still_takes_precedence(self):
        course = {"name": "Course (2) 2015 v2015.1", "id": 1}
        # An existing "(N) YYYY" marker short-circuits before normalization.
        assert ac.derive_course_folder_name(course, []) == "Course (2) 2015 v2015.1"


# ---------------------------------------------------------------------------
# _split_trailing_year  (bare "YYYY" title -> trust year, derive only the term)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestTrailingYear:
    def test_splits_trailing_year(self):
        assert ac._split_trailing_year("World History 2013") == ("World History", 2013)

    def test_no_trailing_year(self):
        assert ac._split_trailing_year("World History") is None

    def test_year_in_middle_not_split(self):
        assert ac._split_trailing_year("History of 1984 and beyond") is None

    def test_bare_year_title_folds_into_marker(self):
        # A title with the year but not the semester.
        course = {"name": "World History 2013", "id": 1}
        assignments = [{"due_at": "2013-09-09T00:00:00Z"}]  # fall -> (2)
        assert ac.derive_course_folder_name(course, assignments) == "World History (2) 2013"

    def test_bare_year_trusts_title_year_over_due_year(self):
        course = {"name": "World History 2013", "id": 1}
        assignments = [{"due_at": "2014-09-09T00:00:00Z"}]  # due in 2014, title says 2013
        assert ac.derive_course_folder_name(course, assignments) == "World History (2) 2013"

    def test_bare_year_no_dates_keeps_name(self):
        course = {"name": "World History 2013", "id": 1}
        assert ac.derive_course_folder_name(course, []) == "World History 2013"

    def test_no_year_in_title_appends_derived(self):
        # Nothing in the title; fully derived and clean.
        course = {"name": "Intro to Biology", "id": 1}
        assignments = [{"due_at": "2014-09-09T00:00:00Z"}]
        assert ac.derive_course_folder_name(course, assignments) == \
            "Intro to Biology (2) 2014"


# ---------------------------------------------------------------------------
# _fmt_pct  (weights: drop a real zero, integers, trailing-zero strip)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestFmtPct:
    @pytest.mark.parametrize("value,expected", [
        (50, "50"),
        (50.0, "50"),
        (12.5, "12.5"),
        (0, "0"),
        (None, "0"),
        ("", "0"),
        ("not-a-number", "0"),
        (33.33, "33.33"),
    ])
    def test_values(self, value, expected):
        assert ac._fmt_pct(value) == expected


# ---------------------------------------------------------------------------
# _fmt_points  (scores: preserve a real zero, empty only for None)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestFmtPoints:
    @pytest.mark.parametrize("value,expected", [
        (None, ""),          # genuinely absent
        (0, "0"),            # real zero is preserved (the "Missing" tier bug)
        (0.0, "0"),
        (2, "2"),
        (2.0, "2"),
        (1.5, "1.5"),
        (3.25, "3.25"),
    ])
    def test_values(self, value, expected):
        assert ac._fmt_points(value) == expected

    def test_non_numeric_returns_str(self):
        assert ac._fmt_points("abc") == "abc"


# ---------------------------------------------------------------------------
# rating_points_label  (single value vs point-range "high to >low")
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestRatingPointsLabel:
    def test_missing_points_is_empty(self):
        assert ac.rating_points_label({}, [], use_range=False) == ""
        assert ac.rating_points_label({"points": None}, [], use_range=False) == ""

    def test_single_value_no_range(self):
        assert ac.rating_points_label({"points": 5}, [], use_range=False) == "5"

    def test_zero_preserved(self):
        assert ac.rating_points_label({"points": 0}, [], use_range=False) == "0"

    def test_range_uses_next_lower_tier_as_low_bound(self):
        ratings = [{"points": 5}, {"points": 3}, {"points": 0}]
        # tier 5: low bound is the next lower tier (3)
        assert ac.rating_points_label({"points": 5}, ratings, use_range=True) == "5 to >3"
        # tier 3: low bound is 0
        assert ac.rating_points_label({"points": 3}, ratings, use_range=True) == "3 to >0"

    def test_lowest_tier_range_floors_at_zero(self):
        ratings = [{"points": 5}, {"points": 3}, {"points": 1}]
        assert ac.rating_points_label({"points": 1}, ratings, use_range=True) == "1 to >0"

    def test_fractional_points_in_range(self):
        ratings = [{"points": 2.5}, {"points": 1.5}]
        assert ac.rating_points_label({"points": 2.5}, ratings, use_range=True) == "2.5 to >1.5"


# ---------------------------------------------------------------------------
# is_published / unpublished_suffix
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestPublishState:
    def test_none_treated_as_published(self):
        assert ac.is_published(None) is True

    def test_explicit_published_flag_wins(self):
        assert ac.is_published({"published": True}) is True
        assert ac.is_published({"published": False}) is False

    def test_falls_back_to_workflow_state(self):
        assert ac.is_published({"workflow_state": "active"}) is True
        assert ac.is_published({"workflow_state": "unpublished"}) is False
        assert ac.is_published({"workflow_state": "deleted"}) is False

    def test_published_flag_overrides_workflow_state(self):
        assert ac.is_published({"published": False, "workflow_state": "active"}) is False

    def test_suffix(self):
        assert ac.unpublished_suffix({"published": True}) == ""
        assert ac.unpublished_suffix({"published": False}) == " (unpublished)"
        assert ac.unpublished_suffix(None) == ""


# ---------------------------------------------------------------------------
# Student avatar helpers
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestAvatarHelpers:
    def test_default_avatar_detection(self):
        assert ac._is_default_avatar(None) is True
        assert ac._is_default_avatar("") is True
        assert ac._is_default_avatar(
            "https://canvas.instructure.com/images/messages/avatar-50.png") is True
        assert ac._is_default_avatar(
            "https://canvas.instructure.com/images/dotted_pic.png") is True

    def test_real_avatar_is_not_default(self):
        assert ac._is_default_avatar(
            "https://canvas.instructure.com/images/thumbnails/339609813/i9ujTuqX") is False

    @pytest.mark.parametrize("name,expected", [
        ("Grace Hopper", "GH"),
        ("Ada Lovelace ", "AL"),           # trailing space tolerated
        ("Aristotle", "AR"),               # single name -> first two letters
        ("  ", "?"),
        ("", "?"),
        (None, "?"),
        ("a b c d", "AD"),                 # first + last
    ])
    def test_initials(self, name, expected):
        assert ac._avatar_initials(name) == expected


# ---------------------------------------------------------------------------
# _neutralize_canvas_placeholders  (the $CANVAS_OBJECT_REFERENCE$ fix)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestNeutralizeCanvasPlaceholders:
    def test_anchor_collapses_to_inert_span(self):
        body = (
            '<a href="https://canvas.instructure.com/courses/123456/pages/'
            '$CANVAS_OBJECT_REFERENCE$">Guide</a>'
        )
        out = ac._neutralize_canvas_placeholders(body)
        assert "<a " not in out
        assert "href=" not in out
        assert ">Guide<" in out
        assert "canvas-archive-broken" in out
        assert "$CANVAS_OBJECT_REFERENCE$" not in out

    def test_anchor_with_extra_attrs(self):
        body = (
            '<a class="x" href="/courses/1/pages/$WIKI_REFERENCE$" data-y="z">Link text</a>'
        )
        out = ac._neutralize_canvas_placeholders(body)
        assert ">Link text<" in out
        assert "canvas-archive-broken" in out

    def test_non_anchor_src_is_defused(self):
        body = '<img src="/courses/1/files/$IMS-CC-FILEBASE$/x.png">'
        out = ac._neutralize_canvas_placeholders(body)
        assert '<img src=' not in out          # no live src remains
        assert "data-broken-src=" in out
        assert "$IMS-CC-FILEBASE$" in out       # original value preserved in the defused attr

    def test_real_links_are_untouched(self):
        body = '<a href="/courses/1/pages/intro">Intro</a><img src="logo.png">'
        assert ac._neutralize_canvas_placeholders(body) == body

    def test_lowercase_token_is_not_a_placeholder(self):
        # Placeholders are uppercase $TOKEN$; a price like $5$ must not match.
        body = '<a href="/courses/1/pages/cost-$5$">Cost</a>'
        out = ac._neutralize_canvas_placeholders(body)
        assert out == body


# ---------------------------------------------------------------------------
# rewrite_canvas_html: file-link gating (--skip-files must not fetch embeds)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestFileLinkGating:
    """When the lazy file fetcher is disabled (as --skip-files does), a File
    link that isn't already in local_paths must be left untouched, never
    fetched. When the fetcher is set, the same link resolves to a local path."""

    def _restore(self):
        ac._UNRESOLVED_FILE_FETCHER = None

    def test_disabled_fetcher_leaves_file_link_untouched(self):
        ac._UNRESOLVED_FILE_FETCHER = None
        try:
            body = '<a href="/courses/7/files/42/download?verifier=abc">handout</a>'
            out = ac.rewrite_canvas_html(body, {}, depth=1)
            assert out == body  # unchanged: no resolution, no download
        finally:
            self._restore()

    def test_disabled_fetcher_never_calls_fetcher(self):
        calls = []

        def _boom(fid, url):  # pragma: no cover - must never run
            calls.append(fid)
            return "Files/x.pdf"

        # Even if something leaves a stale fetcher around, a None gate wins.
        ac._UNRESOLVED_FILE_FETCHER = None
        try:
            ac.rewrite_canvas_html(
                '<a href="/files/99?verifier=z">f</a>', {}, depth=0)
            assert calls == []
        finally:
            self._restore()

    def test_enabled_fetcher_resolves_file_link(self):
        def _fetch(fid, url):
            return "Files/Unit 1/handout.pdf"

        ac._UNRESOLVED_FILE_FETCHER = _fetch
        try:
            body = '<a href="/courses/7/files/42/download?verifier=abc">handout</a>'
            out = ac.rewrite_canvas_html(body, {}, depth=1)
            assert "../Files/Unit%201/handout.pdf" in out
        finally:
            self._restore()

    def test_already_local_file_resolves_without_fetcher(self):
        ac._UNRESOLVED_FILE_FETCHER = None
        try:
            local_paths = {("File", "42"): "Files/handout.pdf"}
            body = '<a href="/courses/7/files/42/download?verifier=abc">h</a>'
            out = ac.rewrite_canvas_html(body, local_paths, depth=0)
            assert 'href="Files/handout.pdf"' in out
        finally:
            self._restore()


# ---------------------------------------------------------------------------
# _is_av_attachment (default-on skip of large student A/V submissions)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestIsAvAttachment:
    def test_video_content_type(self):
        assert ac._is_av_attachment({"content-type": "video/mp4"}) is True

    def test_audio_content_type(self):
        assert ac._is_av_attachment({"content-type": "audio/mpeg"}) is True

    def test_underscore_content_type_key_also_accepted(self):
        assert ac._is_av_attachment({"content_type": "VIDEO/QUICKTIME"}) is True

    def test_extension_fallback_when_no_content_type(self):
        assert ac._is_av_attachment({"display_name": "speech.MOV"}) is True
        assert ac._is_av_attachment({"filename": "podcast.m4a"}) is True

    def test_document_is_not_av(self):
        assert ac._is_av_attachment(
            {"content-type": "application/pdf", "display_name": "essay.pdf"}) is False

    def test_image_is_not_av(self):
        assert ac._is_av_attachment({"content-type": "image/png"}) is False

    def test_empty_attachment_is_not_av(self):
        assert ac._is_av_attachment({}) is False


# ---------------------------------------------------------------------------
# _quiz_answer_text (records a student's chosen answer for the _grades.csv)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestQuizAnswerText:
    def test_single_choice_resolves_option_text(self):
        q = {"answers": [{"id": 1, "text": "Paris"}, {"id": 2, "text": "Lyon"}]}
        entries = [{"answer_id": 1}]
        assert ac._quiz_answer_text(q, entries) == "Paris"

    def test_multiple_selections_joined(self):
        q = {"answers": [{"id": 1, "text": "A"}, {"id": 2, "text": "B"},
                         {"id": 3, "text": "C"}]}
        entries = [{"answer_id": 1}, {"answer_id": 3}]
        assert ac._quiz_answer_text(q, entries) == "A | C"

    def test_free_text_response_without_answer_id(self):
        q = {"answers": []}
        entries = [{"answer_id": None, "text": "my essay answer"}]
        assert ac._quiz_answer_text(q, entries) == "my essay answer"

    def test_html_answer_falls_back_when_no_text(self):
        q = {"answers": [{"id": 9, "html": "<b>x</b>"}]}
        assert ac._quiz_answer_text(q, [{"answer_id": 9}]) == "<b>x</b>"

    def test_no_match_returns_empty(self):
        q = {"answers": [{"id": 1, "text": "A"}]}
        assert ac._quiz_answer_text(q, [{"answer_id": 999}]) == ""

    def test_empty_entries_returns_empty(self):
        assert ac._quiz_answer_text({"answers": []}, []) == ""
