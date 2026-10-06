"""Tests for the app module."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch



class TestAppUtilityMethods:
    """Tests for utility methods in TimesheetApp."""

    def test_find_week_for_date(self):
        """Test finding which week contains a date."""
        from app import TimesheetApp

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()

            # Set up known weeks
            app.weeks = [
                (date(2026, 1, 3), date(2026, 1, 9)),
                (date(2026, 1, 10), date(2026, 1, 16)),
                (date(2026, 1, 17), date(2026, 1, 23)),
            ]

            # Test finding week for date in first week
            assert app._find_week_for_date(date(2026, 1, 5)) == 0

            # Test finding week for date in second week
            assert app._find_week_for_date(date(2026, 1, 12)) == 1

            # Test finding week for date in third week
            assert app._find_week_for_date(date(2026, 1, 20)) == 2

    def test_find_week_for_date_not_found(self):
        """Test finding week for date not in any week returns 0."""
        from app import TimesheetApp

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()
            app.weeks = [
                (date(2026, 1, 3), date(2026, 1, 9)),
            ]

            # Date outside any week should return 0
            assert app._find_week_for_date(date(2026, 2, 1)) == 0

    def test_get_week_month_majority_in_first_month(self):
        """Test determining week's month when majority is in first month."""
        from app import TimesheetApp

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()

            # Week with Mon-Fri mostly in January
            # Sat Jan 31, Sun Feb 1, Mon Feb 2, Tue Feb 3, Wed Feb 4, Thu Feb 5, Fri Feb 6
            # Only 1 weekday in Jan (none - Jan 31 is Sat), 5 weekdays in Feb
            week_start = date(2026, 1, 31)
            week_end = date(2026, 2, 6)

            year, month = app._get_week_month(week_start, week_end)

            # Should be February (more weekdays there)
            assert month == 2

    def test_get_week_month_all_same_month(self):
        """Test determining week's month when all days in same month."""
        from app import TimesheetApp

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()

            # Week entirely in January
            week_start = date(2026, 1, 10)  # Saturday
            week_end = date(2026, 1, 16)  # Friday

            year, month = app._get_week_month(week_start, week_end)

            assert year == 2026
            assert month == 1

    def test_count_weekdays_full_week(self):
        """Test counting weekdays in a full week."""
        from app import TimesheetApp

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()

            # Monday to Friday
            start = date(2026, 1, 26)  # Monday
            end = date(2026, 1, 30)  # Friday

            count = app._count_weekdays(start, end)

            assert count == 5

    def test_count_weekdays_with_weekend(self):
        """Test counting weekdays including weekend."""
        from app import TimesheetApp

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()

            # Full week Sat-Fri
            start = date(2026, 1, 24)  # Saturday
            end = date(2026, 1, 30)  # Friday

            count = app._count_weekdays(start, end)

            assert count == 5  # Only Mon-Fri

    def test_count_weekdays_filter_month(self):
        """Test counting weekdays filtered by month."""
        from app import TimesheetApp

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()

            # Week spanning Jan-Feb
            start = date(2026, 1, 31)  # Saturday
            end = date(2026, 2, 6)  # Friday

            # Only count February days
            count = app._count_weekdays(start, end, filter_month=2)

            # Feb 2-6 = Mon-Fri = 5 weekdays
            assert count == 5

    def test_entry_is_blank_true(self):
        """Test entry_is_blank returns True for empty entry."""
        from app import TimesheetApp
        from models import TimeEntry

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()

            entry = TimeEntry(
                date=date(2026, 1, 27),
                day_of_week="Mon",
            )

            assert app._entry_is_blank(entry) is True

    def test_entry_is_blank_false_clock_in(self):
        """Test entry_is_blank returns False when clock_in set."""
        from app import TimesheetApp
        from datetime import time
        from models import TimeEntry

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()

            entry = TimeEntry(
                date=date(2026, 1, 27),
                day_of_week="Mon",
                clock_in=time(9, 0),
            )

            assert app._entry_is_blank(entry) is False

    def test_entry_is_blank_false_adjustment(self):
        """Test entry_is_blank returns False when adjustment set."""
        from app import TimesheetApp
        from models import TimeEntry

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()

            entry = TimeEntry(
                date=date(2026, 1, 27),
                day_of_week="Mon",
                adjustment=timedelta(hours=7.5),
                adjust_type="L",
            )

            assert app._entry_is_blank(entry) is False


class TestGetAllocationStatus:
    """Tests for _get_allocation_status method."""

    def test_no_worked_hours(self):
        """Test status when no worked hours."""
        from app import TimesheetApp

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()

            result = app._get_allocation_status(date(2026, 1, 27), Decimal("0"))

            assert str(result) == "-"

    def test_no_allocations(self):
        """Test status when worked but no allocations."""
        from app import TimesheetApp
        import storage

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()

            with patch.object(storage, 'get_total_allocated_hours', return_value=Decimal("0")):
                result = app._get_allocation_status(date(2026, 1, 27), Decimal("7.5"))

            assert str(result) == "?"

    def test_under_allocated(self):
        """Test status when under-allocated."""
        from app import TimesheetApp
        import storage

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()

            with patch.object(storage, 'get_total_allocated_hours', return_value=Decimal("5")):
                result = app._get_allocation_status(date(2026, 1, 27), Decimal("7.5"))

            assert "↓" in str(result)

    def test_over_allocated(self):
        """Test status when over-allocated."""
        from app import TimesheetApp
        import storage

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()

            with patch.object(storage, 'get_total_allocated_hours', return_value=Decimal("10")):
                result = app._get_allocation_status(date(2026, 1, 27), Decimal("7.5"))

            assert "↑" in str(result)

    def test_exact_allocation(self):
        """Test status when exactly allocated."""
        from app import TimesheetApp
        import storage

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()

            with patch.object(storage, 'get_total_allocated_hours', return_value=Decimal("7.5")):
                result = app._get_allocation_status(date(2026, 1, 27), Decimal("7.5"))

            assert "✓" in str(result)


class TestHasAllocationMismatch:
    """Tests for _has_allocation_mismatch method."""

    def test_no_entry(self):
        """Test mismatch check when no entry exists."""
        from app import TimesheetApp

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()

            result = app._has_allocation_mismatch(date(2026, 1, 27), {})

            assert result is False

    def test_no_worked_hours(self):
        """Test mismatch check when no worked hours."""
        from app import TimesheetApp
        from models import TimeEntry

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()

            entries_dict = {
                date(2026, 1, 27): TimeEntry(
                    date=date(2026, 1, 27),
                    day_of_week="Mon",
                )
            }

            result = app._has_allocation_mismatch(date(2026, 1, 27), entries_dict)

            assert result is False

    def test_matching_allocation(self):
        """Test mismatch check when allocation matches."""
        from app import TimesheetApp
        from datetime import time
        from models import TimeEntry
        import storage

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()

            entries_dict = {
                date(2026, 1, 27): TimeEntry(
                    date=date(2026, 1, 27),
                    day_of_week="Mon",
                    clock_in=time(9, 0),
                    lunch_duration=timedelta(minutes=30),
                    clock_out=time(17, 0),  # 7.5 hours
                )
            }

            with patch.object(storage, 'get_total_allocated_hours', return_value=Decimal("7.5")):
                result = app._has_allocation_mismatch(date(2026, 1, 27), entries_dict)

            assert result is False

    def test_mismatched_allocation(self):
        """Test mismatch check when allocation doesn't match."""
        from app import TimesheetApp
        from datetime import time
        from models import TimeEntry
        import storage

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()

            entries_dict = {
                date(2026, 1, 27): TimeEntry(
                    date=date(2026, 1, 27),
                    day_of_week="Mon",
                    clock_in=time(9, 0),
                    lunch_duration=timedelta(minutes=30),
                    clock_out=time(17, 0),  # 7.5 hours
                )
            }

            with patch.object(storage, 'get_total_allocated_hours', return_value=Decimal("5")):
                result = app._has_allocation_mismatch(date(2026, 1, 27), entries_dict)

            assert result is True


class TestGitStatusProbe:
    """Tests for _check_git_status against real scratch repositories."""

    @staticmethod
    def _git(repo, *args: str) -> None:
        import subprocess

        subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        )

    @classmethod
    def _make_repo(cls, tmp_path):
        """Build a clone with an upstream, both holding one commit."""
        origin = tmp_path / "origin.git"
        work = tmp_path / "work"
        cls._git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
        cls._git(tmp_path, "clone", str(origin), str(work))
        cls._git(work, "config", "user.email", "test@example.com")
        cls._git(work, "config", "user.name", "Test")
        (work / "README.md").write_text("hello\n")
        cls._git(work, "add", "README.md")
        cls._git(work, "commit", "-m", "initial")
        cls._git(work, "push", "-u", "origin", "main")
        return work

    def test_clean_repo_is_in_sync_with_nothing_uncommitted(self, tmp_path):
        from app import _check_git_status

        work = self._make_repo(tmp_path)
        status = _check_git_status(work)

        assert status.state == "in_sync"
        assert status.branch == "main"
        assert status.uncommitted == 0

    def test_modified_file_counts_as_uncommitted(self, tmp_path):
        from app import _check_git_status

        work = self._make_repo(tmp_path)
        (work / "README.md").write_text("changed\n")

        status = _check_git_status(work)
        assert status.state == "in_sync"
        assert status.uncommitted == 1

    def test_staged_and_untracked_both_count(self, tmp_path):
        from app import _check_git_status

        work = self._make_repo(tmp_path)
        (work / "staged.txt").write_text("x\n")
        self._git(work, "add", "staged.txt")
        (work / "untracked.txt").write_text("y\n")

        status = _check_git_status(work)
        assert status.uncommitted == 2

    def test_ignored_files_do_not_count(self, tmp_path):
        from app import _check_git_status

        work = self._make_repo(tmp_path)
        (work / ".gitignore").write_text("*.log\n")
        self._git(work, "add", ".gitignore")
        self._git(work, "commit", "-m", "ignore logs")
        self._git(work, "push")
        (work / "noise.log").write_text("chatter\n")

        status = _check_git_status(work)
        assert status.uncommitted == 0

    def test_unpushed_commit_is_out_of_sync_and_ahead(self, tmp_path):
        from app import _check_git_status

        work = self._make_repo(tmp_path)
        (work / "README.md").write_text("more\n")
        self._git(work, "commit", "-am", "local work")

        status = _check_git_status(work)
        assert status.state == "out_of_sync"
        assert (status.ahead, status.behind) == (1, 0)
        assert status.uncommitted == 0

    def test_unpushed_and_uncommitted_reported_together(self, tmp_path):
        from app import _check_git_status

        work = self._make_repo(tmp_path)
        (work / "README.md").write_text("more\n")
        self._git(work, "commit", "-am", "local work")
        (work / "scratch.txt").write_text("wip\n")

        status = _check_git_status(work)
        assert status.state == "out_of_sync"
        assert status.ahead == 1
        assert status.uncommitted == 1

    def test_uncommitted_survives_missing_upstream(self, tmp_path):
        """The count must still arrive when the sync check can't run."""
        from app import _check_git_status

        work = self._make_repo(tmp_path)
        self._git(work, "checkout", "-b", "no-upstream")
        (work / "scratch.txt").write_text("wip\n")

        status = _check_git_status(work)
        assert status.state == "unavailable"
        assert status.reason == "no upstream set"
        assert status.uncommitted == 1

    def test_not_a_repo_is_unavailable(self, tmp_path):
        from app import _check_git_status

        status = _check_git_status(tmp_path)
        assert status.state == "unavailable"
        assert status.reason == "not running from a git repo"


class TestUpdateWarnings:
    """Tests for the toast/dialog wording driven by the probe."""

    @staticmethod
    def _run(status):
        """Run the background check with a canned status; capture UI calls."""
        from app import TimesheetApp

        calls = []

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()
        with (
            patch('app._check_git_status', return_value=status),
            patch.object(
                TimesheetApp, 'call_from_thread',
                lambda self, fn, *a, **kw: calls.append((fn.__name__, a, kw)),
            ),
        ):
            app._check_for_updates_bg()

        assert len(calls) == 1, f"expected one UI call, got {calls}"
        return calls[0]

    def test_in_sync_and_clean_is_a_plain_notice(self):
        from app import GitStatus

        name, args, kwargs = self._run(GitStatus("in_sync", branch="main"))
        assert name == "notify"
        assert args[0] == "App is up to date with remote branch main"
        assert "severity" not in kwargs

    def test_in_sync_but_dirty_warns(self):
        from app import GitStatus

        name, args, kwargs = self._run(
            GitStatus("in_sync", branch="main", uncommitted=3)
        )
        assert name == "notify"
        assert args[0] == (
            "Up to date with remote branch main, but you have "
            "3 uncommitted files"
        )
        assert kwargs["severity"] == "warning"

    def test_single_uncommitted_file_is_singular(self):
        from app import GitStatus

        _, args, _ = self._run(
            GitStatus("in_sync", branch="main", uncommitted=1)
        )
        assert "1 uncommitted file," not in args[0]
        assert "you have 1 uncommitted file" in args[0]

    def test_ahead_only_warns_about_unpushed_commits(self):
        from app import GitStatus

        name, args, kwargs = self._run(
            GitStatus("out_of_sync", branch="main", ahead=2)
        )
        assert name == "notify"
        assert args[0] == "Not synced to remote main: 2 unpushed commits"
        assert kwargs["severity"] == "warning"

    def test_ahead_and_dirty_warns_about_both(self):
        from app import GitStatus

        _, args, kwargs = self._run(
            GitStatus("out_of_sync", branch="main", ahead=1, uncommitted=2)
        )
        assert args[0] == (
            "Not synced to remote main: 1 unpushed commit and "
            "2 uncommitted files"
        )
        assert kwargs["severity"] == "warning"

    def test_behind_still_opens_the_dialog_carrying_the_count(self):
        from app import GitStatus

        name, args, _ = self._run(
            GitStatus(
                "out_of_sync", branch="main", ahead=0, behind=4, uncommitted=2
            )
        )
        assert name == "push_screen"
        screen = args[0]
        assert screen.behind == 4
        assert screen.uncommitted == 2

    def test_unavailable_mentions_uncommitted_work(self):
        from app import GitStatus

        _, args, kwargs = self._run(
            GitStatus(
                "unavailable", branch="main",
                reason="offline (fetch failed)", uncommitted=2,
            )
        )
        assert args[0] == (
            "Can't check if updates are available: offline (fetch failed) "
            "(you have 2 uncommitted files)"
        )
        assert kwargs["severity"] == "warning"

    def test_unavailable_and_clean_is_unchanged(self):
        from app import GitStatus

        _, args, _ = self._run(
            GitStatus("unavailable", reason="git is not installed")
        )
        assert args[0] == (
            "Can't check if updates are available: git is not installed"
        )


class TestEntryCacheFreshness:
    """The entries cache must not go stale when another process writes.

    The HTTP API writes to the same SQLite file while the TUI is open, so a
    snapshot loaded once per month change silently shows old data.
    """

    @staticmethod
    def _app(year: int, month: int):
        from app import TimesheetApp
        from utils import get_weeks_in_month

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()
        app.current_year, app.current_month = year, month
        app.weeks = get_weeks_in_month(year, month)
        app._load_month_data()
        return app

    @staticmethod
    def _save(d, clock_in, clock_out):
        import storage
        from datetime import time
        from models import TimeEntry

        storage.save_entry(
            TimeEntry(
                date=d,
                day_of_week=d.strftime("%a"),
                clock_in=time(*clock_in),
                lunch_duration=timedelta(minutes=30),
                clock_out=time(*clock_out),
            )
        )

    def test_cycling_weeks_picks_up_an_external_write(self, clean_db):
        """The reported bug, end to end.

        Moving between weeks *within* a month took a branch that only
        re-rendered, so a day the API had rewritten kept its old times until
        the month changed.
        """
        from datetime import time
        from unittest.mock import MagicMock

        from app import TimesheetApp

        day = date(2026, 1, 14)
        self._save(day, (9, 0), (17, 0))
        app = self._app(2026, 1)
        app.view_mode = "week"
        app.current_week_idx = 0
        assert app._get_or_create_entry(day).clock_in == time(9, 0)

        # Another actor (the API on a remote box) rewrites the day.
        self._save(day, (8, 0), (16, 0))

        # Cycle away and back, staying inside the month the whole time.
        with (
            patch.object(
                TimesheetApp, 'query_one',
                return_value=MagicMock(cursor_row=0),
            ),
            patch.object(TimesheetApp, '_refresh_week_display'),
            patch.object(TimesheetApp, '_update_window_title'),
        ):
            app.action_next_week()
            app.action_prev_week()

        assert app.current_week_idx == 0, "should be back on the starting week"
        assert app._get_or_create_entry(day).clock_in == time(8, 0)

    def test_reload_picks_up_an_external_delete(self, clean_db):
        import storage

        day = date(2026, 1, 14)
        self._save(day, (9, 0), (17, 0))
        app = self._app(2026, 1)

        conn = storage.get_connection()
        conn.execute("DELETE FROM time_entries WHERE date = ?", (day.isoformat(),))
        conn.commit()
        conn.close()

        app._load_month_data()
        assert app._get_or_create_entry(day).clock_in is None

    def test_refresh_display_rereads_before_rendering(self):
        """Week navigation only calls _refresh_display, so the reload lives there."""
        from app import TimesheetApp

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()
        app.view_mode = "week"

        with (
            patch.object(TimesheetApp, '_load_month_data') as load,
            patch.object(TimesheetApp, '_refresh_week_display'),
            patch.object(TimesheetApp, '_update_window_title'),
        ):
            app._refresh_display()

        load.assert_called_once()

    def test_boundary_days_from_adjacent_months_are_loaded(self, clean_db):
        """January's first week shows late-December days; they need real data."""
        from datetime import time

        boundary = date(2025, 12, 30)
        self._save(boundary, (9, 0), (17, 0))

        app = self._app(2026, 1)
        # The week grid really does span this day.
        assert app.weeks[0][0] <= boundary <= app.weeks[0][1]
        assert app._get_or_create_entry(boundary).clock_in == time(9, 0)

    def test_trailing_boundary_days_are_loaded(self, clean_db):
        """And the last week can run into the following month."""
        from datetime import time

        app = self._app(2026, 1)
        last_week_end = app.weeks[-1][1]
        assert last_week_end.month != 1

        self._save(last_week_end, (10, 0), (15, 0))
        app._load_month_data()
        assert app._get_or_create_entry(last_week_end).clock_in == time(10, 0)

    def test_cache_stays_bounded_to_the_visible_range(self, clean_db):
        """Widening the range must not pull in the whole table."""
        far_off = date(2026, 6, 1)
        self._save(far_off, (9, 0), (17, 0))

        app = self._app(2026, 1)
        assert far_off not in app.entries
        assert min(app.entries, default=date(2026, 1, 1)) >= app.weeks[0][0]


class TestAutoSync:
    """Tests for the periodic push of DB changes to the data repo."""

    @staticmethod
    def _script(tmp_path, body):
        script = tmp_path / "sync.sh"
        script.write_text(f"#!/bin/bash\n{body}\n")
        script.chmod(0o755)
        return script

    @staticmethod
    def _app():
        from app import TimesheetApp

        with patch.object(TimesheetApp, 'run'):
            return TimesheetApp()

    def test_fingerprint_moves_on_write_and_ignores_shm(self, tmp_path):
        import os

        from app import _db_fingerprint

        db = tmp_path / "t.db"
        db.write_bytes(b"x")
        before = _db_fingerprint(db)
        assert before == _db_fingerprint(db)

        (tmp_path / "t.db-shm").write_bytes(b"read")
        assert _db_fingerprint(db) == before

        wal = tmp_path / "t.db-wal"
        wal.write_bytes(b"commit")
        os.utime(wal, ns=(1, 1))
        assert _db_fingerprint(db) != before

    def test_push_reports_pushed(self, tmp_path):
        from app import _run_sync_push

        script = self._script(tmp_path, r'printf "\033[32mPushed.\033[0m\n"')
        assert _run_sync_push(script) == (True, True, "")

    def test_nothing_to_push_is_quiet_success(self, tmp_path):
        from app import _run_sync_push

        script = self._script(tmp_path, 'echo "No changes to push."')
        assert _run_sync_push(script) == (True, False, "")

    def test_passes_if_changed_and_force_flags(self, tmp_path):
        from app import _run_sync_push

        script = self._script(
            tmp_path,
            '[[ "$*" == "push --if-changed --force-with-running" ]] '
            '&& echo Pushed.',
        )
        assert _run_sync_push(script).pushed

    def test_failure_surfaces_first_stderr_line(self, tmp_path):
        from app import _run_sync_push

        script = self._script(
            tmp_path,
            r'printf "\033[31mRemote is 2 commit(s) ahead\033[0m\nmore\n" >&2;'
            ' exit 1',
        )
        result = _run_sync_push(script)
        assert not result.ok
        assert result.message == "Remote is 2 commit(s) ahead"

    def test_missing_script_is_a_failure_not_a_crash(self, tmp_path):
        from app import _run_sync_push

        result = _run_sync_push(tmp_path / "nope.sh")
        assert not result.ok
        assert "couldn't run sync.sh" in result.message

    def test_unchanged_db_skips_the_script(self):
        from app import _db_fingerprint

        import storage

        app = self._app()
        app._auto_sync_fingerprint = _db_fingerprint(storage.DB_PATH)
        with patch('app.threading.Thread') as thread:
            app._auto_sync_tick()
        thread.assert_not_called()

    def test_changed_db_runs_the_script_once(self):
        app = self._app()
        with patch('app.threading.Thread') as thread:
            app._auto_sync_tick()
            app._auto_sync_tick()  # still running: must not stack up
        thread.assert_called_once()

    def test_success_records_fingerprint(self):
        from app import SyncResult

        app = self._app()
        app._auto_sync_running = True
        with patch.object(type(app), 'notify') as notify:
            app._on_auto_sync_done(((1, 1), None), SyncResult(True, pushed=True))
        assert app._auto_sync_fingerprint == ((1, 1), None)
        assert not app._auto_sync_running
        notify.assert_called_once_with("Timesheet data synced to remote")

    def test_failure_retries_but_only_warns_once(self):
        from app import SyncResult

        app = self._app()
        failed = SyncResult(False, message="Remote is 1 commit(s) ahead")
        with patch.object(type(app), 'notify') as notify:
            app._on_auto_sync_done(((1, 1), None), failed)
            app._on_auto_sync_done(((2, 2), None), failed)
        assert app._auto_sync_fingerprint is None  # so the next tick retries
        assert notify.call_count == 1
