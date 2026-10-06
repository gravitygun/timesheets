"""Tests for auto-sync (autosync.py) and its wiring into the app."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from autosync import (
    SyncResult,
    SyncState,
    db_fingerprint,
    format_when,
    run_sync_push,
)

NOW = datetime(2026, 10, 6, 15, 0)


def _script(tmp_path, body):
    script = tmp_path / "sync.sh"
    script.write_text(f"#!/bin/bash\n{body}\n")
    script.chmod(0o755)
    return script


class TestFingerprint:
    def test_moves_on_write_and_ignores_shm(self, tmp_path):
        db = tmp_path / "t.db"
        db.write_bytes(b"x")
        before = db_fingerprint(db)
        assert before == db_fingerprint(db)

        (tmp_path / "t.db-shm").write_bytes(b"read")
        assert db_fingerprint(db) == before

        wal = tmp_path / "t.db-wal"
        wal.write_bytes(b"commit")
        os.utime(wal, ns=(1, 1))
        assert db_fingerprint(db) != before


class TestRunSyncPush:
    def test_reports_pushed(self, tmp_path):
        script = _script(tmp_path, r'printf "\033[32mPushed.\033[0m\n"')
        result = run_sync_push(script)
        assert result.ok and result.pushed
        assert result.output == "Pushed."

    def test_nothing_to_push_is_quiet_success(self, tmp_path):
        result = run_sync_push(_script(tmp_path, 'echo "No changes to push."'))
        assert result.ok and not result.pushed

    def test_passes_if_changed_and_force_flags(self, tmp_path):
        script = _script(
            tmp_path,
            '[[ "$*" == "push --if-changed --force-with-running" ]] '
            '&& echo Pushed.',
        )
        assert run_sync_push(script).pushed

    def test_failure_keeps_first_line_and_full_output(self, tmp_path):
        script = _script(
            tmp_path,
            r'printf "\033[31mRemote is 2 commit(s) ahead\033[0m\nmore\n" >&2;'
            ' exit 1',
        )
        result = run_sync_push(script)
        assert not result.ok
        assert result.message == "Remote is 2 commit(s) ahead"
        assert result.output == "Remote is 2 commit(s) ahead\nmore"

    def test_missing_script_is_a_failure_not_a_crash(self, tmp_path):
        result = run_sync_push(tmp_path / "nope.sh")
        assert not result.ok
        assert "couldn't run sync.sh" in result.message


class TestFormatWhen:
    @pytest.mark.parametrize("moment, expected", [
        (datetime(2026, 10, 6, 9, 5), "09:05"),
        (datetime(2026, 10, 5, 17, 30), "Mon 17:30"),
        (datetime(2026, 9, 20, 8, 0), "20 Sep 08:00"),
    ])
    def test_formats(self, moment, expected):
        assert format_when(moment, NOW) == expected


class TestSyncState:
    def test_success_records_fingerprint_and_clears_error(self):
        state = SyncState(running=True, error="old", error_at=NOW)
        assert not state.record(SyncResult(True, pushed=True), ((1, 1), None), NOW)
        assert state.fingerprint == ((1, 1), None)
        assert state.last_push == NOW
        assert not state.running and state.error is None

    def test_failure_keeps_retrying_but_is_new_only_once(self):
        state = SyncState()
        failed = SyncResult(False, message="Remote is 1 commit(s) ahead")
        assert state.record(failed, ((1, 1), None), NOW)
        assert not state.record(failed, ((2, 2), None), NOW)
        assert state.fingerprint is None  # so the next check runs again

    @pytest.mark.parametrize("state, text, level", [
        (SyncState(enabled=False), "Auto-sync off (not the default DB)", "muted"),
        (SyncState(running=True), "⟳ Syncing…", "busy"),
        (
            SyncState(error="x", error_at=NOW),
            "⚠ Sync failed 15:00 — press s for details",
            "error",
        ),
        (SyncState(next_check=NOW), "Auto-sync: first check 15:00", "muted"),
        (SyncState(last_checked=NOW), "✓ In sync · checked 15:00", "ok"),
        (
            SyncState(last_checked=NOW, last_push=NOW - timedelta(days=1)),
            "✓ In sync · last push Mon 15:00 · checked 15:00",
            "ok",
        ),
    ])
    def test_summary(self, state, text, level):
        assert state.summary(NOW) == (text, level)


class TestAppWiring:
    @staticmethod
    def _app():
        from app import TimesheetApp

        with patch.object(TimesheetApp, 'run'):
            return TimesheetApp()

    def test_unchanged_db_skips_the_script(self):
        import storage

        app = self._app()
        app.sync_state.fingerprint = db_fingerprint(storage.DB_PATH)
        with patch('app.threading.Thread') as thread:
            app._sync_check()
        thread.assert_not_called()
        assert app.sync_state.last_outcome == "no changes since last sync"
        assert app.sync_state.next_check is not None

    def test_force_runs_even_when_unchanged(self):
        import storage

        app = self._app()
        app.sync_state.fingerprint = db_fingerprint(storage.DB_PATH)
        with patch('app.threading.Thread') as thread:
            app._sync_check(force=True)
        thread.assert_called_once()

    def test_changed_db_runs_the_script_once(self):
        app = self._app()
        with patch('app.threading.Thread') as thread:
            app._sync_check()
            app._sync_check()  # still running: must not stack up
        thread.assert_called_once()

    def test_new_failure_warns_once(self):
        app = self._app()
        failed = SyncResult(False, message="Remote is 1 commit(s) ahead")
        with patch.object(type(app), 'notify') as notify:
            app._on_sync_done(((1, 1), None), failed)
            app._on_sync_done(((2, 2), None), failed)
        assert notify.call_count == 1

    def test_heartbeat_prompts_once_when_overdue(self):
        app = self._app()
        app.auto_quit_at = datetime.now() - timedelta(minutes=1)
        with patch.object(type(app), 'push_screen') as push:
            app._heartbeat()
            app._heartbeat()
        push.assert_called_once()

    def test_keep_open_extends_by_an_hour(self):
        app = self._app()
        with patch.object(type(app), 'notify'):
            app._on_auto_quit_answer(False)
        remaining = app.auto_quit_at - datetime.now()
        assert timedelta(minutes=59) < remaining <= timedelta(hours=1)

    def test_quit_runs_a_final_sync_then_exits(self):
        app = self._app()
        app.sync_state.enabled = True
        with (
            patch('app.run_sync_push', return_value=SyncResult(True)),
            patch.object(type(app), 'notify'),
            patch('app.threading.Thread') as thread,
        ):
            app._on_auto_quit_answer(True)
            app._on_auto_quit_answer(True)  # second answer is ignored
        thread.assert_called_once()

        with (
            patch('app.run_sync_push', return_value=SyncResult(
                False, message="offline",
            )),
            patch.object(
                type(app), 'call_from_thread',
                lambda self, fn, *a, **kw: fn(*a, **kw),
            ),
            patch.object(type(app), 'exit') as exit_,
        ):
            app._final_sync_bg("Quit automatically after 8 hours.")
        message = exit_.call_args.kwargs["message"]
        assert message.startswith("Quit automatically after 8 hours.")
        assert "Final sync failed: offline" in message

    def test_disabled_sync_quits_straight_away(self):
        app = self._app()
        app.sync_state.enabled = False
        with (
            patch.object(type(app), 'exit') as exit_,
            patch('app.threading.Thread') as thread,
        ):
            app._quit_with_final_sync("bye")
        exit_.assert_called_once_with(message="bye")
        thread.assert_not_called()


@pytest.mark.parametrize("minutes, expected", [
    (480, "8 hours"),
    (65, "1 hour 5 minutes"),
    (1, "1 minute"),
])
def test_describe_duration(minutes, expected):
    from app import _describe_duration

    assert _describe_duration(timedelta(minutes=minutes)) == expected
