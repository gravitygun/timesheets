"""Tests for jira_sync: the worklog sync tool and its mock Jira."""

from __future__ import annotations

import json
import threading
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Generator

import pytest

from jira_sync import mock_jira
from jira_sync import sync_worklogs as sw

ME = mock_jira.DEFAULT_USER


# --- Helpers ----------------------------------------------------------------


def _settings(**overrides: Any) -> sw.Settings:
    return sw.Settings(url="https://jira.example", token="t", **overrides)


def _alloc(day: int, seconds: int = 3600, comment: str = "did things",
           key: str = "MPIT-1", entered: bool = False) -> sw.Allocation:
    return sw.Allocation(key, date(2026, 10, day), seconds, comment, entered, ("1",))


def _worklog(day: int, seconds: int = 3600, comment: str = "did things",
             wid: str = "500") -> sw.Worklog:
    return sw.Worklog(wid, "MPIT-1", date(2026, 10, day),
                      f"2026-10-{day:02d}T08:00:00.000+0000", seconds, comment)


def _reconcile(allocs: list[sw.Allocation], logs: list[sw.Worklog], *,
               update_comments: bool = True,
               delete_extra: bool = False) -> list[sw.Action]:
    return sw.reconcile("MPIT-1", allocs, logs, update_comments=update_comments,
                        delete_extra=delete_extra)


def _export_doc(rows: list[tuple[str, str, float, str]],
                tickets: dict[str, str] | None = None,
                entered: frozenset[tuple[str, str]] = frozenset()) -> dict[str, Any]:
    """An export like storage.get_allocations_export writes, for October 2026."""
    return {
        "schema": "timesheets.allocations",
        "version": 1,
        "generated_at": "2026-10-27T10:00:00+00:00",
        "period": {"from": "2026-10-01", "to": "2026-10-31"},
        "tickets": tickets or {tid: f"Ticket {tid}" for tid, *_ in rows},
        "allocations": [
            {"ticket_id": tid, "date": day, "hours": hours, "description": text,
             "entered_on_client": (tid, day) in entered}
            for tid, day, hours, text in rows
        ],
    }


# --- Mapping and settings ---------------------------------------------------


class TestIssueKeyFor:
    def test_bare_number_gets_prefix(self):
        assert sw.issue_key_for("10400", _settings()) == "MPIT-10400"

    def test_existing_key_is_kept(self):
        assert sw.issue_key_for("FD-2587", _settings()) == "FD-2587"
        assert sw.issue_key_for("fd-2587", _settings()) == "FD-2587"

    def test_issue_map_overrides_and_skips(self):
        settings = _settings(issue_map={"10668": "OTHER-5", "ADMIN": "skip"})
        assert sw.issue_key_for("10668", settings) == "OTHER-5"
        assert sw.issue_key_for("ADMIN", settings) is None

    def test_unrecognisable_id_is_none(self):
        assert sw.issue_key_for("ADMIN", _settings()) is None

    def test_prefix_is_configurable(self):
        assert sw.issue_key_for("7", _settings(issue_prefix="ABC-")) == "ABC-7"

    def test_project_map_renames_keyed_tickets(self):
        settings = _settings(project_map={"FD": "MPITFD"})
        assert sw.issue_key_for("FD-2587", settings) == "MPITFD-2587"
        assert sw.issue_key_for("fd-2587", settings) == "MPITFD-2587"
        assert sw.issue_key_for("OTHER-1", settings) == "OTHER-1"
        assert sw.issue_key_for("MPITFD-3", settings) == "MPITFD-3"
        assert sw.issue_key_for("10400", settings) == "MPIT-10400"

    def test_issue_map_wins_over_project_map(self):
        settings = _settings(project_map={"FD": "MPITFD"},
                             issue_map={"FD-1": "MPIT-5", "FD-2": "skip"})
        assert sw.issue_key_for("FD-1", settings) == "MPIT-5"
        assert sw.issue_key_for("FD-2", settings) is None


class TestLoadSettings:
    def _write(self, tmp_path: Path, body: str) -> Path:
        path = tmp_path / "config.ini"
        path.write_text(body)
        path.chmod(0o600)
        return path

    def test_token_precedence(self, tmp_path, monkeypatch):
        token_file = tmp_path / "token"
        token_file.write_text("from-file\n")
        token_file.chmod(0o600)
        path = self._write(tmp_path, f"[jira]\nurl = https://j/\ntoken_file = {token_file}\n")

        monkeypatch.delenv(sw.TOKEN_ENV, raising=False)
        assert sw.load_settings(path).token == "from-file"
        monkeypatch.setenv(sw.TOKEN_ENV, "from-env")
        assert sw.load_settings(path).token == "from-env"

    def test_inline_token_beats_token_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv(sw.TOKEN_ENV, raising=False)
        path = self._write(tmp_path, "[jira]\nurl = https://j\ntoken = inline\n"
                                     "token_file = /nonexistent\n")
        assert sw.load_settings(path).token == "inline"

    def test_defaults_and_normalisation(self, tmp_path, monkeypatch):
        monkeypatch.setenv(sw.TOKEN_ENV, "t")
        path = self._write(tmp_path, "[jira]\nurl = https://j/\n"
                                     "[sync]\nissue_prefix = mpit\nskip_projects = fd, x-\n")
        settings = sw.load_settings(path)
        assert settings.url == "https://j"
        assert settings.issue_prefix == "MPIT-"
        assert settings.skip_projects == frozenset({"FD", "X"})
        assert settings.day_start == time(9, 0)
        assert settings.adjust_estimate == "auto"
        assert settings.update_comments is False
        assert settings.review == "none"

    def test_project_map(self, tmp_path, monkeypatch):
        monkeypatch.setenv(sw.TOKEN_ENV, "t")
        path = self._write(tmp_path, "[jira]\nurl = https://j\n"
                                     "[project_map]\nfd- = mpitfd\n")
        assert sw.load_settings(path).project_map == {"FD": "MPITFD"}

    def test_issue_map_keeps_case(self, tmp_path, monkeypatch):
        monkeypatch.setenv(sw.TOKEN_ENV, "t")
        path = self._write(tmp_path, "[jira]\nurl = https://j\n"
                                     "[issue_map]\nFD-1 = MPIT-2\nAdmin = skip\n")
        assert sw.load_settings(path).issue_map == {"FD-1": "MPIT-2", "Admin": "skip"}

    @pytest.mark.parametrize("body", [
        "[jira]\nurl = jira.example\n",
        "[jira]\nurl = https://j\n[sync]\nadjust_estimate = new\n",
        "[jira]\nurl = https://j\n[sync]\nday_start = 9am\n",
        "[jira]\nurl = https://j\nverify_ssl = /no/such/bundle.pem\n",
        "[jira]\nurl = https://j\n[issue_map]\n1 = not a key\n",
        "[jira]\nurl = https://j\n[sync]\nissue_prefix =\n",
        "[jira]\nurl = https://j\n[sync]\nissue_prefix = 9X-\n",
        "[jira]\nurl = https://j\n[project_map]\nFD = MPT FD\n",
        "[jira]\nurl = https://j\n[sync]\nreview = sometimes\n",
    ])
    def test_bad_values_are_config_errors(self, tmp_path, monkeypatch, body):
        monkeypatch.setenv(sw.TOKEN_ENV, "t")
        with pytest.raises(sw.ConfigError):
            sw.load_settings(self._write(tmp_path, body))

    def test_no_credentials_is_an_error(self, tmp_path, monkeypatch):
        monkeypatch.delenv(sw.TOKEN_ENV, raising=False)
        with pytest.raises(sw.ConfigError, match="no credentials"):
            sw.load_settings(self._write(tmp_path, "[jira]\nurl = https://j\n"))

    def test_missing_file(self, tmp_path):
        with pytest.raises(sw.ConfigError, match="no config file"):
            sw.load_settings(tmp_path / "absent.ini")


# --- Export -----------------------------------------------------------------


class TestExport:
    def test_load(self, tmp_path):
        path = tmp_path / "e.json"
        path.write_text(json.dumps(_export_doc([("10400", "2026-10-05", 3.25, "x")])))
        export = sw.load_export(path)
        assert export.first_day == date(2026, 10, 1)
        assert export.allocations[0].seconds == 3 * 3600 + 900

    @pytest.mark.parametrize("doc", [
        {"schema": "something.else", "version": 1},
        {"schema": "timesheets.allocations", "version": 99},
        {"schema": "timesheets.allocations", "version": 1, "period": {}},
    ])
    def test_rejects_other_documents(self, tmp_path, doc):
        path = tmp_path / "e.json"
        path.write_text(json.dumps(doc))
        with pytest.raises(sw.ConfigError):
            sw.load_export(path)

    def test_map_allocations_merges_skips_and_stacks_start_times(self, tmp_path):
        path = tmp_path / "e.json"
        path.write_text(json.dumps(_export_doc([
            ("10400", "2026-10-05", 2, "a"),
            ("10401", "2026-10-05", 1.5, "b"),
            ("ADMIN", "2026-10-05", 1, "c"),
            ("FD-1", "2026-10-05", 1, "d"),
            ("99", "2026-10-05", 0.5, "e"),
        ])))
        settings = _settings(issue_map={"10401": "MPIT-10400"},
                             skip_projects=frozenset({"FD"}))
        allocations, skipped = sw.map_allocations(sw.load_export(path), settings)

        [merged, other] = allocations
        assert merged.issue_key == "MPIT-10400"
        assert merged.seconds == int(3.5 * 3600)
        assert merged.comment == "a\n\nb"
        assert merged.start == time(9, 0)
        assert other.issue_key == "MPIT-99"
        assert other.start == time(12, 30)  # after the merged 3.5h
        assert sorted(row.ticket_id for row, _ in skipped) == ["ADMIN", "FD-1"]


# --- Reconciliation ---------------------------------------------------------


class TestWholeMinutes:
    def test_hours_round_to_whole_minutes(self, tmp_path):
        path = tmp_path / "e.json"
        path.write_text(json.dumps(_export_doc([("1", "2026-10-05", 0.01, "x")])))
        assert sw.load_export(path).allocations[0].seconds == 60


class TestReconcile:
    def test_missing_worklog_is_created(self):
        [action] = _reconcile([_alloc(5)], [])
        assert action.kind == sw.CREATE

    def test_matching_worklog_is_ok(self):
        [action] = _reconcile([_alloc(5)], [_worklog(5)])
        assert action.kind == sw.OK

    def test_whitespace_only_comment_difference_is_ok(self):
        [action] = _reconcile([_alloc(5, comment="line one\nline two")],
                              [_worklog(5, comment="line one  \r\nline two\n")])
        assert action.kind == sw.OK

    def test_wrong_time_is_updated(self):
        [action] = _reconcile([_alloc(5, 5400)], [_worklog(5, 3600)])
        assert action.kind == sw.UPDATE
        assert action.changes == {"timeSpentSeconds": 5400}

    def test_wrong_comment_is_updated_unless_disabled(self):
        [action] = _reconcile([_alloc(5, comment="new")], [_worklog(5, comment="old")])
        assert action.changes == {"comment": "new"}
        [action] = _reconcile([_alloc(5, comment="new")], [_worklog(5, comment="old")],
                              update_comments=False)
        assert action.kind == sw.OK

    def test_split_worklogs_that_add_up_are_ok(self):
        [action] = _reconcile([_alloc(5, 3600)],
                              [_worklog(5, 1800, wid="1"), _worklog(5, 1800, wid="2")])
        assert action.kind == sw.OK

    def test_split_worklogs_that_do_not_add_up_conflict(self):
        [action] = _reconcile([_alloc(5, 3600)],
                              [_worklog(5, 1800, wid="1"), _worklog(5, 900, wid="2")])
        assert action.kind == sw.CONFLICT

    def test_worklog_without_allocation_is_extra_or_deleted(self):
        [action] = _reconcile([], [_worklog(5)])
        assert action.kind == sw.EXTRA
        [action] = _reconcile([], [_worklog(5)], delete_extra=True)
        assert action.kind == sw.DELETE

    def test_create_notes_when_timesheets_thinks_it_was_entered(self):
        [action] = _reconcile([_alloc(5, entered=True)], [])
        assert "already entered" in action.note


class TestClock:
    def test_utc_late_evening_belongs_to_next_london_day(self):
        clock = sw.make_clock("Europe/London", None)
        moment = sw.parse_jira_time("2026-10-04T23:30:00.000+0000")
        assert clock.day_of(moment) == date(2026, 10, 5)

    def test_new_worklogs_carry_the_right_offset(self):
        clock = sw.make_clock("Europe/London", None)
        assert sw.format_jira_time(clock.at(date(2026, 10, 5), time(9))) == \
            "2026-10-05T09:00:00.000+0100"
        assert sw.format_jira_time(clock.at(date(2026, 11, 5), time(9))) == \
            "2026-11-05T09:00:00.000+0000"

    def test_profile_timezone_used_when_not_configured(self):
        assert sw.make_clock(None, "Europe/Paris").name == "Europe/Paris"

    def test_bad_configured_timezone_is_an_error(self):
        with pytest.raises(sw.ConfigError):
            sw.make_clock("Mars/Olympus", None)

    def test_bad_profile_timezone_falls_back_to_local(self):
        assert sw.make_clock(None, "Mars/Olympus").tz is None


# --- Against the mock Jira --------------------------------------------------


class Rig:
    """A mock Jira on a free port, a config pointing at it, and an export."""

    def __init__(self, tmp_path: Path, mock: mock_jira.MockJira, url: str) -> None:
        self.tmp_path = tmp_path
        self.mock = mock
        self.url = url
        self.export = tmp_path / "export.json"
        self.journals = tmp_path / "journals"
        self.write_config()

    def write_config(self, extra_sync: str = "", token: str = mock_jira.DEFAULT_TOKEN) -> None:
        config = self.tmp_path / "config.ini"
        config.write_text(f"[jira]\nurl = {self.url}\ntoken = {token}\n"
                          f"[sync]\ntimezone = Europe/London\n"
                          f"journal_dir = {self.journals}\n{extra_sync}")
        config.chmod(0o600)

    def write_export(self, rows: list[tuple[str, str, float, str]], **kwargs: Any) -> None:
        self.export.write_text(json.dumps(_export_doc(rows, **kwargs)))

    def run(self, *args: str) -> int:
        command, *rest = args
        argv = [command, "--config", str(self.tmp_path / "config.ini"), *rest]
        if command in ("plan", "apply", "verify"):
            argv.insert(3, str(self.export))
        return sw.main(argv)

    def writes(self) -> list[tuple[str, str]]:
        return [r for r in self.mock.requests if r[0] != "GET"]

    def worklogs(self, key: str) -> list[dict[str, Any]]:
        return self.mock.issues[key]["worklogs"]

    def last_journal(self) -> Path:
        return sorted(self.journals.glob("apply-*.jsonl"))[-1]


@pytest.fixture
def rig(tmp_path: Path, monkeypatch) -> Generator[Rig, None, None]:
    monkeypatch.delenv(sw.TOKEN_ENV, raising=False)
    mock = mock_jira.MockJira()
    for n in (1, 2, 3):
        mock.add_issue(f"MPIT-{n}", f"Ticket {n}")
    server = mock_jira.make_server(mock, port=0, quiet=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                              daemon=True)
    thread.start()
    yield Rig(tmp_path, mock, f"http://127.0.0.1:{server.server_address[1]}")
    server.shutdown()
    server.server_close()


def _bst(day: str, hour: int = 9) -> str:
    return f"{day}T{hour:02d}:00:00.000+0100"


class TestCheck:
    def test_ok(self, rig, capsys):
        assert rig.run("check", "--issue", "1") == sw.EXIT_OK
        out = capsys.readouterr().out
        assert "Jira 8.20.7" in out
        assert "MPIT-1  Ticket 1" in out

    def test_bad_token(self, rig, capsys):
        rig.write_config(token="wrong")
        assert rig.run("check") == sw.EXIT_JIRA
        assert "authentication failed" in capsys.readouterr().err

    def test_unreachable(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv(sw.TOKEN_ENV, raising=False)
        config = tmp_path / "c.ini"
        config.write_text("[jira]\nurl = http://127.0.0.1:9\ntoken = t\ntimeout = 2\n")
        assert sw.main(["check", "--config", str(config)]) == sw.EXIT_JIRA
        assert "cannot reach" in capsys.readouterr().err


class TestPlanAndApply:
    def test_plan_writes_nothing(self, rig, capsys):
        rig.write_export([("1", "2026-10-05", 2, "work")])
        assert rig.run("plan") == sw.EXIT_OK
        assert rig.writes() == []
        assert "1 to create" in capsys.readouterr().out

    def test_apply_creates_then_is_idempotent(self, rig, capsys):
        rig.write_export([("1", "2026-10-05", 2, "work"), ("2", "2026-10-05", 1, "more")])
        assert rig.run("apply", "--yes") == sw.EXIT_OK
        assert "Verified: all 2" in capsys.readouterr().out
        [first] = rig.worklogs("MPIT-1")
        [second] = rig.worklogs("MPIT-2")
        assert first["started"] == "2026-10-05T08:00:00.000+0000"   # 09:00 BST
        assert second["started"] == "2026-10-05T10:00:00.000+0000"  # follows on
        assert first["timeSpentSeconds"] == 7200
        assert first["comment"] == "work"

        before = len(rig.writes())
        assert rig.run("apply", "--yes") == sw.EXIT_OK
        assert "Nothing to change" in capsys.readouterr().out
        assert len(rig.writes()) == before
        assert rig.run("verify") == sw.EXIT_OK

    def test_limit(self, rig):
        rig.write_export([("1", "2026-10-05", 1, "a"), ("1", "2026-10-06", 1, "b")])
        assert rig.run("apply", "--yes", "--limit", "1") == sw.EXIT_OK
        assert len(rig.worklogs("MPIT-1")) == 1
        assert rig.run("verify") == sw.EXIT_DIFFERENCES

    def test_scope_filters(self, rig):
        rig.write_export([("1", "2026-10-05", 1, "a"), ("1", "2026-10-06", 1, "b"),
                          ("2", "2026-10-05", 1, "c")])
        assert rig.run("apply", "--yes", "--issue", "MPIT-1", "--date", "2026-10-06") == 0
        assert [w["comment"] for w in rig.worklogs("MPIT-1")] == ["b"]
        assert rig.worklogs("MPIT-2") == []

    def test_updates_own_worklog_and_ignores_others(self, rig):
        rig.mock.add_worklog("MPIT-1", ME, _bst("2026-10-05"), 3600, "work")
        rig.mock.add_worklog("MPIT-1", mock_jira.OTHER_USER, _bst("2026-10-05"), 600, "x")
        rig.write_export([("1", "2026-10-05", 2, "work")])
        assert rig.run("apply", "--yes") == sw.EXIT_OK
        mine, theirs = rig.worklogs("MPIT-1")
        assert mine["timeSpentSeconds"] == 7200
        assert theirs["timeSpentSeconds"] == 600

    def test_conflicts_and_extras_are_left_alone(self, rig, capsys):
        rig.mock.add_worklog("MPIT-1", ME, _bst("2026-10-05"), 600, "part 1")
        rig.mock.add_worklog("MPIT-1", ME, _bst("2026-10-05"), 600, "part 2")
        rig.mock.add_worklog("MPIT-1", ME, _bst("2026-10-07"), 600, "stray")
        rig.write_export([("1", "2026-10-05", 2, "work")])
        assert rig.run("apply", "--yes") == sw.EXIT_OK
        out = capsys.readouterr().out
        assert "1 conflicts, 1 extra" in out
        assert "Nothing to change" in out
        assert rig.writes() == []

    def test_delete_extra(self, rig):
        rig.mock.add_worklog("MPIT-1", ME, _bst("2026-10-07"), 600, "stray")
        rig.mock.add_worklog("MPIT-1", ME, _bst("2026-11-02"), 600, "next month")
        rig.write_export([("1", "2026-10-05", 1, "work")])
        assert rig.run("apply", "--yes", "--delete-extra") == sw.EXIT_OK
        assert sorted(w["comment"] for w in rig.worklogs("MPIT-1")) == ["next month", "work"]

    def test_unexpected_issue_is_reported_not_touched(self, rig, capsys):
        rig.mock.add_worklog("MPIT-3", ME, _bst("2026-10-05"), 600, "elsewhere")
        rig.write_export([("1", "2026-10-05", 1, "work")])
        assert rig.run("verify") == sw.EXIT_DIFFERENCES
        assert "1 unexpected" in capsys.readouterr().out
        assert rig.run("apply", "--yes") == sw.EXIT_OK
        assert len(rig.worklogs("MPIT-3")) == 1

    def test_missing_issue_blocks_apply(self, rig, capsys):
        rig.write_export([("1", "2026-10-05", 1, "a"), ("404", "2026-10-05", 1, "b")])
        assert rig.run("apply", "--yes") == sw.EXIT_JIRA
        assert "MPIT-404" in capsys.readouterr().out
        assert rig.writes() == []
        assert rig.run("apply", "--yes", "--skip-missing") == sw.EXIT_OK
        assert len(rig.worklogs("MPIT-1")) == 1

    def test_unentered_only(self, rig):
        rig.write_export([("1", "2026-10-05", 1, "a"), ("1", "2026-10-06", 1, "b")],
                         entered=frozenset({("1", "2026-10-05")}))
        assert rig.run("apply", "--yes", "--unentered-only") == sw.EXIT_OK
        assert [w["comment"] for w in rig.worklogs("MPIT-1")] == ["b"]

    def test_time_on_a_skipped_ticket_is_not_unexpected(self, rig, capsys):
        rig.mock.add_worklog("MPIT-3", ME, _bst("2026-10-05"), 600, "meetings")
        rig.write_config("[issue_map]\n3 = skip\n")
        rig.write_export([("1", "2026-10-05", 1, "a"), ("3", "2026-10-05", 1, "m")])
        assert rig.run("plan") == sw.EXIT_OK
        out = capsys.readouterr().out
        assert "0 unexpected" in out
        assert "1 skipped" in out

    def test_project_map_end_to_end(self, rig, capsys):
        rig.mock.add_issue("MPITFD-2587", "RAPIDS feasibility")
        rig.write_config("[project_map]\nFD = MPITFD\n")
        rig.write_export([("FD-2587", "2026-10-02", 0.5, "a"),
                          ("FD-2587", "2026-10-05", 1, "b")])
        assert rig.run("apply", "--yes", "--issue", "FD-2587",
                       "--date", "2026-10-02") == sw.EXIT_OK
        assert [w["comment"] for w in rig.worklogs("MPITFD-2587")] == ["a"]
        assert rig.run("apply", "--yes") == sw.EXIT_OK
        assert rig.run("verify") == sw.EXIT_OK
        assert "MPITFD-2587" in capsys.readouterr().out

    def test_skipped_tickets_are_reported(self, rig, capsys):
        rig.write_export([("1", "2026-10-05", 1, "a"), ("ADMIN", "2026-10-05", 1, "b")])
        assert rig.run("plan") == sw.EXIT_OK
        assert "1 skipped" in capsys.readouterr().out

    def test_paginated_worklogs(self, rig):
        rig.mock.page_size = 2
        for day in range(5, 10):
            rig.mock.add_worklog("MPIT-1", ME, _bst(f"2026-10-{day:02d}"), 3600, "w")
        rig.write_export([("1", f"2026-10-{day:02d}", 1, "w") for day in range(5, 10)])
        assert rig.run("verify") == sw.EXIT_OK

    def test_needs_yes_when_not_interactive(self, rig, monkeypatch):
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        rig.write_export([("1", "2026-10-05", 1, "a")])
        assert rig.run("apply") == sw.EXIT_USAGE
        assert rig.writes() == []

    def test_jira_rejection_stops_the_run(self, rig, capsys, monkeypatch):
        rig.write_export([("1", "2026-10-05", 1, "a"), ("2", "2026-10-05", 1, "b")])
        real = sw.JiraClient.add_worklog
        calls = []

        def flaky(self, key, body, adjust):
            calls.append(key)
            if key == "MPIT-2":
                raise sw.JiraError("POST -> 403 Forbidden", 403)
            return real(self, key, body, adjust)

        monkeypatch.setattr(sw.JiraClient, "add_worklog", flaky)
        assert rig.run("apply", "--yes") == sw.EXIT_JIRA
        assert calls == ["MPIT-1", "MPIT-2"]
        assert "FAILED on MPIT-2" in capsys.readouterr().out
        assert len(rig.last_journal().read_text().splitlines()) == 2  # header + 1


class TestUndo:
    def test_undo_reverses_create_update_and_delete(self, rig):
        rig.mock.add_worklog("MPIT-1", ME, _bst("2026-10-05"), 3600, "old text")
        rig.mock.add_worklog("MPIT-1", ME, _bst("2026-10-07"), 600, "stray")
        rig.write_export([("1", "2026-10-05", 2, "new text"), ("2", "2026-10-06", 1, "b")])
        assert rig.run("apply", "--yes", "--delete-extra") == sw.EXIT_OK
        assert rig.worklogs("MPIT-2")

        assert rig.run("undo", str(rig.last_journal()), "--yes") == sw.EXIT_OK
        assert rig.worklogs("MPIT-2") == []
        restored = {w["comment"]: w["timeSpentSeconds"] for w in rig.worklogs("MPIT-1")}
        assert restored == {"old text": 3600, "stray": 600}
        [stray] = [w for w in rig.worklogs("MPIT-1") if w["comment"] == "stray"]
        assert stray["started"] == "2026-10-07T08:00:00.000+0000"

    def test_undo_leaves_worklogs_edited_since(self, rig, capsys):
        rig.write_export([("1", "2026-10-05", 1, "a")])
        assert rig.run("apply", "--yes") == sw.EXIT_OK
        rig.worklogs("MPIT-1")[0]["timeSpentSeconds"] = 999
        journal = str(rig.last_journal())

        assert rig.run("undo", journal, "--yes") == sw.EXIT_OK
        assert "edited since" in capsys.readouterr().out
        assert len(rig.worklogs("MPIT-1")) == 1
        assert rig.run("undo", journal, "--yes", "--force") == sw.EXIT_OK
        assert rig.worklogs("MPIT-1") == []

    def test_undo_refuses_a_different_jira(self, rig, capsys):
        rig.write_export([("1", "2026-10-05", 1, "a")])
        assert rig.run("apply", "--yes") == sw.EXIT_OK
        journal = rig.last_journal()
        lines = journal.read_text().splitlines()
        header = json.loads(lines[0])
        header["url"] = "https://elsewhere"
        journal.write_text("\n".join([json.dumps(header), *lines[1:]]) + "\n")
        assert rig.run("undo", str(journal), "--yes") == sw.EXIT_USAGE
        assert len(rig.worklogs("MPIT-1")) == 1


class TestMockJira:
    def test_rejects_badly_formatted_started(self, rig):
        import requests

        resp = requests.post(f"{rig.url}/rest/api/2/issue/MPIT-1/worklog",
                             headers={"Authorization": f"Bearer {rig.mock.token}"},
                             json={"started": "2026-10-05T09:00:00+01:00",
                                   "timeSpentSeconds": 60}, timeout=5)
        assert resp.status_code == 400
        assert "started" in resp.json()["errors"]

    def test_seed_demo_scenario(self, tmp_path):
        export = tmp_path / "e.json"
        export.write_text(json.dumps(_export_doc(
            [(str(n), f"2026-10-{n + 4:02d}", 1, f"t{n}") for n in range(1, 7)])))
        mock = mock_jira.MockJira()
        mock_jira._seed(mock, export, "MPIT-", entered=False, scenario="demo",
                        missing={"MPIT-6"})
        assert "MPIT-6" not in mock.issues
        assert "MPIT-99999" in mock.issues
        stored = mock.issues["MPIT-1"]["worklogs"][0]["started"]
        assert datetime.strptime(stored, mock_jira.JIRA_TIME_FORMAT).tzinfo == timezone.utc


class TestSafetyNets:
    """Regression tests for the independent review's findings."""

    def test_undo_twice_does_not_recreate_twice(self, rig):
        rig.mock.add_worklog("MPIT-1", ME, _bst("2026-10-07"), 600, "stray")
        rig.write_export([("1", "2026-10-05", 1, "a")])
        assert rig.run("apply", "--yes", "--delete-extra") == sw.EXIT_OK
        journal = str(rig.last_journal())
        assert rig.run("undo", journal, "--yes") == sw.EXIT_OK
        assert rig.run("undo", journal, "--yes") == sw.EXIT_OK
        assert [w["comment"] for w in rig.worklogs("MPIT-1")].count("stray") == 1

    def test_issue_outside_export_is_never_deleted_from(self, rig, capsys):
        rig.mock.add_worklog("MPIT-3", ME, _bst("2026-10-05"), 600, "hand-logged")
        rig.write_export([("1", "2026-10-05", 1, "a")])
        assert rig.run("apply", "--yes", "--issue", "3", "--delete-extra") == sw.EXIT_OK
        assert "1 unexpected" in capsys.readouterr().out
        assert len(rig.worklogs("MPIT-3")) == 1

    def test_short_worklog_page_blocks_the_issue(self, rig, monkeypatch, capsys):
        rig.mock.page_size = 2
        for day in (5, 6, 7):
            rig.mock.add_worklog("MPIT-1", ME, _bst(f"2026-10-0{day}"), 3600, "w")
        real = sw.JiraClient._call

        def ignores_start_at(self, method, path, **kwargs):
            if path.endswith("/worklog"):
                kwargs["params"] = None
            return real(self, method, path, **kwargs)

        monkeypatch.setattr(sw.JiraClient, "_call", ignores_start_at)
        rig.write_export([("1", f"2026-10-0{day}", 1, "w") for day in (5, 6, 7)])
        assert rig.run("apply", "--yes") == sw.EXIT_JIRA
        assert "only 2 of 3 worklogs" in capsys.readouterr().out
        assert rig.writes() == []

    def test_jira_answering_with_another_issue_blocks_it(self, rig, monkeypatch):
        real = sw.JiraClient.issue

        def moved(self, key):
            found = real(self, key)
            return {**found, "key": "NEW-9"} if found and key == "MPIT-1" else found

        monkeypatch.setattr(sw.JiraClient, "issue", moved)
        rig.write_export([("1", "2026-10-05", 1, "a")])
        assert rig.run("apply", "--yes") == sw.EXIT_JIRA
        assert rig.writes() == []

    def test_update_restates_start_and_visibility(self, rig, monkeypatch):
        record = rig.mock.add_worklog("MPIT-1", ME, _bst("2026-10-05"), 3600, "a")
        record["visibility"] = {"type": "group", "value": "staff"}
        real_json = rig.mock.worklog_json
        monkeypatch.setattr(rig.mock, "worklog_json", lambda key, rec: {
            **real_json(key, rec), **({"visibility": rec["visibility"]}
                                      if "visibility" in rec else {})})
        sent = []
        real = sw.JiraClient.update_worklog

        def capture(self, key, worklog_id, body, adjust):
            sent.append(body)
            return real(self, key, worklog_id, body, adjust)

        monkeypatch.setattr(sw.JiraClient, "update_worklog", capture)
        rig.write_export([("1", "2026-10-05", 2, "a")])
        assert rig.run("apply", "--yes") == sw.EXIT_OK
        assert sent == [{"started": "2026-10-05T08:00:00.000+0000",
                         "timeSpentSeconds": 7200,
                         "visibility": {"type": "group", "value": "staff"}}]

    def test_create_for_entered_allocation_needs_a_flag(self, rig, capsys):
        rig.write_export([("1", "2026-10-05", 1, "a")],
                         entered=frozenset({("1", "2026-10-05")}))
        assert rig.run("apply", "--yes") == sw.EXIT_USAGE
        assert "already entered" in capsys.readouterr().out
        assert rig.writes() == []
        assert rig.run("apply", "--yes", "--unentered-only") == sw.EXIT_OK
        assert rig.writes() == []
        assert rig.run("apply", "--yes", "--create-entered") == sw.EXIT_OK
        assert len(rig.worklogs("MPIT-1")) == 1

    def test_change_while_waiting_for_yes_stops_the_run(self, rig, monkeypatch, capsys):
        rig.write_export([("1", "2026-10-05", 1, "a")])
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        def typed_in_browser_meanwhile(prompt):
            rig.mock.add_worklog("MPIT-1", ME, _bst("2026-10-05"), 3600, "a")
            return "yes"

        monkeypatch.setattr("builtins.input", typed_in_browser_meanwhile)
        assert rig.run("apply") == sw.EXIT_DIFFERENCES
        assert "while waiting" in capsys.readouterr().out
        assert len(rig.worklogs("MPIT-1")) == 1

    def test_undo_refuses_another_users_journal(self, rig):
        rig.write_export([("1", "2026-10-05", 1, "a")])
        assert rig.run("apply", "--yes") == sw.EXIT_OK
        journal = rig.last_journal()
        lines = journal.read_text().splitlines()
        header = {**json.loads(lines[0]), "user": "someone.else"}
        journal.write_text("\n".join([json.dumps(header), *lines[1:]]) + "\n")
        assert rig.run("undo", str(journal), "--yes") == sw.EXIT_USAGE
        assert len(rig.worklogs("MPIT-1")) == 1


def test_readme_stage_two_rehearsal(rig):
    """The README's Stage 2 sequence: create, undo, redo, update, undo, redo."""
    rig.write_export([("1", "2026-10-21", 1, "meeting"), ("1", "2026-10-22", 2, "x")])
    one = ("--issue", "1", "--date", "2026-10-21", "--yes")

    assert rig.run("apply", *one) == sw.EXIT_OK
    assert rig.run("undo", str(rig.last_journal()), "--yes") == sw.EXIT_OK
    assert rig.worklogs("MPIT-1") == []

    assert rig.run("apply", *one) == sw.EXIT_OK
    [wl] = rig.worklogs("MPIT-1")
    started = wl["started"]
    wl["timeSpentSeconds"] = 5400  # edited in the browser

    assert rig.run("apply", *one) == sw.EXIT_OK
    assert (wl["timeSpentSeconds"], wl["started"]) == (3600, started)
    assert rig.run("undo", str(rig.last_journal()), "--yes") == sw.EXIT_OK
    assert wl["timeSpentSeconds"] == 5400

    assert rig.run("apply", *one) == sw.EXIT_OK
    assert [w["timeSpentSeconds"] for w in rig.worklogs("MPIT-1")] == [3600]


def test_mock_seed_renames_projects(tmp_path):
    export = tmp_path / "e.json"
    export.write_text(json.dumps(_export_doc(
        [("FD-2587", "2026-10-05", 1, "a"), ("10400", "2026-10-05", 1, "b"),
         ("ADMIN", "2026-10-05", 1, "c")])))
    mock = mock_jira.MockJira()
    mock_jira._seed(mock, export, "MPIT-", entered=False, scenario=None,
                    missing=set(), project_map={"FD": "MPITFD"})
    assert sorted(mock.issues) == ["ADMIN", "MPIT-10400", "MPITFD-2587"]


class TestCommentDefaults:
    def test_comment_differences_are_left_alone_by_default(self, rig, capsys):
        rig.mock.add_worklog("MPIT-1", ME, _bst("2026-10-05"), 3600, "tidied by hand")
        rig.write_export([("1", "2026-10-05", 1, "AI draft")])
        assert rig.run("verify") == sw.EXIT_OK
        assert rig.run("apply", "--yes") == sw.EXIT_OK
        assert rig.writes() == []

    def test_update_comments_brings_them_into_line(self, rig):
        rig.mock.add_worklog("MPIT-1", ME, _bst("2026-10-05"), 3600, "tidied by hand")
        rig.write_config("update_comments = true\n")
        rig.write_export([("1", "2026-10-05", 1, "AI draft")])
        assert rig.run("apply", "--yes") == sw.EXIT_OK
        assert rig.worklogs("MPIT-1")[0]["comment"] == "AI draft"


class TestEditFile:
    def _actions(self) -> list[sw.Action]:
        return [sw.Action(sw.CREATE, "MPIT-1", date(2026, 10, n),
                          allocation=_alloc(n, comment=f"draft {n}"))
                for n in (5, 6)]

    def test_round_trip(self):
        text = sw.edit_file_text(list(enumerate(self._actions(), 1)), {})
        assert "=== [1] create MPIT-1 2026-10-05 Mon 1h" in text
        assert sw.parse_edit_file(text) == {
            ("MPIT-1", date(2026, 10, 5)): "draft 5",
            ("MPIT-1", date(2026, 10, 6)): "draft 6",
        }

    def test_earlier_edits_seed_the_text(self):
        earlier = {("MPIT-1", date(2026, 10, 6)): "already tidied"}
        text = sw.edit_file_text(list(enumerate(self._actions(), 1)), earlier)
        assert "already tidied" in text
        assert "draft 6" not in text

    def test_multiline_comment_keeps_hash_lines_inside_a_block(self):
        text = "# ignored\n=== [1] create MPIT-1 2026-10-05 Mon 1h\n# first\n\nsecond  \n\n"
        assert sw.parse_edit_file(text) == {("MPIT-1", date(2026, 10, 5)): "# first\n\nsecond"}

    @pytest.mark.parametrize(("text", "complaint"), [
        ("=== [1] create MPIT-1 2026-10-05 Mon 1h\n\n", "no comment"),
        ("=== [1] create MPIT-1 2026-10-05 Mon 1h\na\n=== [2] create MPIT-1 2026-10-05 Mon\nb\n",
         "twice"),
        ("=== [1] create MPIT-1 2026-10-05 Mon 1h\na\n=== [2] mangled\n", "can't read"),
        ("stray words\n=== [1] create MPIT-1 2026-10-05 Mon 1h\na\n", "before the first"),
        ("=== [1] create MPIT-1 2026-13-05 Mon 1h\na\n", "bad date"),
    ])
    def test_problems_are_explained(self, text, complaint):
        with pytest.raises(ValueError, match=complaint):
            sw.parse_edit_file(text)


class TestEditingComments:
    """apply --edit, with a stand-in for vim."""

    @pytest.fixture(autouse=True)
    def interactive(self, monkeypatch):
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def _editor(self, monkeypatch, *edits, result: bool = True) -> list[str]:
        """Each call applies the next edit function to the file's text."""
        seen: list[str] = []
        queue = list(edits)

        def fake(path):
            text = path.read_text()
            seen.append(text)
            if queue:
                path.write_text(queue.pop(0)(text))
            return result

        monkeypatch.setattr(sw, "run_editor", fake)
        return seen

    def test_edited_comment_is_sent_and_deleted_block_left_out(self, rig, monkeypatch, capsys):
        rig.write_export([("1", "2026-10-05", 1, "AI draft"), ("2", "2026-10-05", 1, "other")])

        def edit(text):
            text = text.replace("AI draft", "What I actually did")
            head, _, _ = text.partition("=== [2]")
            return head
        self._editor(monkeypatch, edit)

        assert rig.run("apply", "--yes", "--edit") == sw.EXIT_OK
        out = capsys.readouterr().out
        assert "1 rewritten, 0 as exported, 1 worklog(s) left out" in out
        assert "Verified: all 1" in out
        assert [w["comment"] for w in rig.worklogs("MPIT-1")] == ["What I actually did"]
        assert rig.worklogs("MPIT-2") == []

        # A later plain run keeps the edit and picks up what was left out
        assert rig.run("apply", "--yes", "--review", "none") == sw.EXIT_OK
        assert rig.worklogs("MPIT-1")[0]["comment"] == "What I actually did"
        assert [w["comment"] for w in rig.worklogs("MPIT-2")] == ["other"]

    def test_cq_changes_nothing(self, rig, monkeypatch):
        rig.write_export([("1", "2026-10-05", 1, "draft")])
        self._editor(monkeypatch, lambda t: t.replace("draft", "x"), result=False)
        assert rig.run("apply", "--yes", "--edit") == sw.EXIT_OK
        assert rig.writes() == []

    def test_bad_edit_can_be_fixed_or_abandoned(self, rig, monkeypatch):
        rig.write_export([("1", "2026-10-05", 1, "draft")])
        seen = self._editor(monkeypatch,
                            lambda t: t.replace("draft", ""),        # empty: rejected
                            lambda t: t.replace("\n\n", "\nfixed\n", 1)
                            if "fixed" not in t else t)
        answers = iter(["", "n"])
        monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
        assert rig.run("apply", "--yes", "--edit") == sw.EXIT_OK
        assert len(seen) == 2  # reopened once after the problem
        assert rig.writes() == [] or rig.worklogs("MPIT-1")

    def test_abandoning_after_a_bad_edit(self, rig, monkeypatch):
        rig.write_export([("1", "2026-10-05", 1, "draft")])
        self._editor(monkeypatch, lambda t: t.replace("draft", ""))
        monkeypatch.setattr("builtins.input", lambda prompt: "n")
        assert rig.run("apply", "--yes", "--edit") == sw.EXIT_OK
        assert rig.writes() == []

    def test_edits_file_seeds_a_later_run(self, rig, monkeypatch):
        rig.write_export([("1", "2026-10-05", 1, "draft")])
        self._editor(monkeypatch, lambda t: t.replace("draft", "tidied"), result=False)
        assert rig.run("apply", "--yes", "--edit") == sw.EXIT_OK  # :cq after editing
        [saved] = rig.journals.glob("edit-*.txt")

        seen = self._editor(monkeypatch)  # opens, changes nothing
        assert rig.run("apply", "--yes", "--edits", str(saved)) == sw.EXIT_OK
        assert "tidied" in seen[0]
        assert rig.worklogs("MPIT-1")[0]["comment"] == "tidied"

    def test_config_turns_editing_on(self, rig, monkeypatch):
        rig.write_config("review = editor\n")
        rig.write_export([("1", "2026-10-05", 1, "draft")])
        seen = self._editor(monkeypatch)
        assert rig.run("apply", "--yes") == sw.EXIT_OK
        assert len(seen) == 1
        rig.write_export([("1", "2026-10-06", 1, "next")])
        assert rig.run("apply", "--yes", "--review", "none") == sw.EXIT_OK
        assert len(seen) == 1

    def test_comment_updates_are_editable_too(self, rig, monkeypatch):
        rig.mock.add_worklog("MPIT-1", ME, _bst("2026-10-05"), 3600, "old")
        rig.write_config("update_comments = true\n")
        rig.write_export([("1", "2026-10-05", 1, "new draft")])
        self._editor(monkeypatch, lambda t: t.replace("new draft", "new, tidied"))
        assert rig.run("apply", "--yes", "--edit") == sw.EXIT_OK
        assert rig.worklogs("MPIT-1")[0]["comment"] == "new, tidied"


class TestStepReview:
    """apply --step: one change at a time."""

    @pytest.fixture(autouse=True)
    def interactive(self, monkeypatch):
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def _answers(self, monkeypatch, *answers: str) -> list[str]:
        prompts: list[str] = []
        queue = list(answers)

        def answer(prompt):
            prompts.append(prompt)
            return queue.pop(0)

        monkeypatch.setattr("builtins.input", answer)
        return prompts

    def _export(self, rig):
        rig.write_export([("1", "2026-10-05", 1, "first draft"),
                          ("2", "2026-10-05", 2, "second draft"),
                          ("3", "2026-10-05", 1, "third draft")])

    def test_yes_skip_and_quit(self, rig, monkeypatch, capsys):
        self._export(rig)
        self._answers(monkeypatch, "y", "s", "q")
        assert rig.run("apply", "--yes", "--step") == sw.EXIT_OK
        out = capsys.readouterr().out
        assert "[1/3] CREATE  MPIT-1  2026-10-05 Mon  1h" in out
        assert "first draft" in out
        assert "Reviewed: 1 to send, 1 skipped, 1 not reviewed" in out
        assert len(rig.worklogs("MPIT-1")) == 1
        assert rig.worklogs("MPIT-2") == rig.worklogs("MPIT-3") == []

    def test_all_the_rest(self, rig, monkeypatch):
        self._export(rig)
        prompts = self._answers(monkeypatch, "s", "a")
        assert rig.run("apply", "--yes", "--step") == sw.EXIT_OK
        assert len(prompts) == 2
        assert rig.worklogs("MPIT-1") == []
        assert len(rig.worklogs("MPIT-2")) == len(rig.worklogs("MPIT-3")) == 1

    def test_edit_then_send(self, rig, monkeypatch, capsys):
        self._export(rig)
        self._answers(monkeypatch, "e", "y", "q")

        def vim(path):
            path.write_text("A better\nsecond line\n")
            return True

        monkeypatch.setattr(sw, "run_editor", vim)
        assert rig.run("apply", "--yes", "--step") == sw.EXIT_OK
        assert rig.worklogs("MPIT-1")[0]["comment"] == "A better\nsecond line"
        assert "Edited comments saved in" in capsys.readouterr().out
        assert not list(rig.journals.glob("comment-*.txt"))  # temp file tidied

    def test_cq_or_emptied_comment_leaves_it_alone(self, rig, monkeypatch):
        self._export(rig)
        results = iter([(False, "ignored"), (True, "  \n")])

        def vim(path):
            ok, text = next(results)
            path.write_text(text)
            return ok

        monkeypatch.setattr(sw, "run_editor", vim)
        self._answers(monkeypatch, "e", "e", "y", "q")
        assert rig.run("apply", "--yes", "--step") == sw.EXIT_OK
        assert rig.worklogs("MPIT-1")[0]["comment"] == "first draft"
        assert not list(rig.journals.glob("edit-*.txt"))

    def test_unknown_answer_shows_help_and_asks_again(self, rig, monkeypatch, capsys):
        self._export(rig)
        prompts = self._answers(monkeypatch, "x", "q")
        assert rig.run("apply", "--yes", "--step") == sw.EXIT_OK
        assert len(prompts) == 2
        assert "send this one and all the rest" in capsys.readouterr().out
        assert rig.writes() == []

    def test_step_edits_seed_a_later_run(self, rig, monkeypatch):
        self._export(rig)

        def vim(path):
            path.write_text("tidied\n")
            return True

        monkeypatch.setattr(sw, "run_editor", vim)
        self._answers(monkeypatch, "e", "q")  # edit the first, then stop
        assert rig.run("apply", "--yes", "--step") == sw.EXIT_OK
        assert rig.writes() == []
        [saved] = rig.journals.glob("edit-*.txt")

        self._answers(monkeypatch, "a")
        assert rig.run("apply", "--yes", "--step", "--edits", str(saved)) == sw.EXIT_OK
        assert rig.worklogs("MPIT-1")[0]["comment"] == "tidied"
        assert rig.worklogs("MPIT-2")[0]["comment"] == "second draft"

    def test_review_from_config(self, rig, monkeypatch):
        rig.write_config("review = step\n")
        self._export(rig)
        prompts = self._answers(monkeypatch, "a")
        assert rig.run("apply", "--yes") == sw.EXIT_OK
        assert len(prompts) == 1
        assert len(rig.worklogs("MPIT-3")) == 1

    def test_update_shows_both_comments(self, rig, monkeypatch, capsys):
        rig.mock.add_worklog("MPIT-1", ME, _bst("2026-10-05"), 3600, "in Jira now")
        rig.write_config("update_comments = true\n")
        rig.write_export([("1", "2026-10-05", 2, "from timesheet")])
        self._answers(monkeypatch, "y")
        assert rig.run("apply", "--yes", "--step") == sw.EXIT_OK
        out = capsys.readouterr().out
        assert "1h -> 2h" in out
        assert out.index("in Jira now") < out.index("Will become:") < out.index("from timesheet", out.index("Will become:"))
