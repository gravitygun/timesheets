"""Tests for the three-way row merge behind `sync.sh pull --keep-local`."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import sync_merge

SCHEMA = """
CREATE TABLE time_entries (
    date TEXT PRIMARY KEY,
    day_of_week TEXT NOT NULL,
    clock_in TEXT,
    lunch_minutes INTEGER,
    clock_out TEXT,
    comment TEXT
);
CREATE TABLE tickets (
    id TEXT PRIMARY KEY,
    description TEXT
);
CREATE TABLE bill_lines (
    year INTEGER,
    month INTEGER,
    line_no INTEGER,
    points INTEGER,
    PRIMARY KEY (year, month, line_no)
);
"""


def make_db(path: Path, entries: list[tuple] = [], tickets: list[tuple] = []) -> Path:
    """Build a database with the shared schema and the given rows."""
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.executemany("INSERT INTO time_entries VALUES (?,?,?,?,?,?)", entries)
    conn.executemany("INSERT INTO tickets VALUES (?,?)", tickets)
    conn.commit()
    conn.close()
    return path


def run_merge(tmp_path: Path, base_rows, local_rows, remote_rows) -> tuple[int, Path]:
    """Run the merge end to end, returning its exit code and the target DB."""
    base = make_db(tmp_path / "base.db", base_rows)
    local = make_db(tmp_path / "local.db", local_rows)
    target = make_db(tmp_path / "target.db", remote_rows)
    code = sync_merge.main(
        ["--base", str(base), "--local", str(local), "--target", str(target)]
    )
    return code, target


def entries_in(db: Path) -> list[tuple]:
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT * FROM time_entries ORDER BY date").fetchall()
    conn.close()
    return rows


MON = ("2026-08-10", "Mon", "09:30", 30, "18:30", None)
TUE_LOCAL = ("2026-08-11", "Tue", "09:30", 30, "17:15", None)
WED_REMOTE = ("2026-08-12", "Wed", "09:30", 30, "16:15", None)
THU_REMOTE = ("2026-08-13", "Thu", "09:45", 30, "17:30", None)


class TestDisjointChanges:
    """The common case: two machines recording different days."""

    def test_local_and_remote_additions_both_survive(self, tmp_path: Path) -> None:
        code, target = run_merge(
            tmp_path,
            base_rows=[MON],
            local_rows=[MON, TUE_LOCAL],
            remote_rows=[MON, WED_REMOTE, THU_REMOTE],
        )

        assert code == sync_merge.EXIT_OK
        assert entries_in(target) == [MON, TUE_LOCAL, WED_REMOTE, THU_REMOTE]

    def test_local_edit_of_untouched_row_is_replayed(self, tmp_path: Path) -> None:
        edited = ("2026-08-10", "Mon", "09:30", 60, "18:30", "long lunch")
        code, target = run_merge(
            tmp_path,
            base_rows=[MON],
            local_rows=[edited],
            remote_rows=[MON, WED_REMOTE],
        )

        assert code == sync_merge.EXIT_OK
        assert entries_in(target) == [edited, WED_REMOTE]

    def test_local_deletion_is_replayed(self, tmp_path: Path) -> None:
        code, target = run_merge(
            tmp_path,
            base_rows=[MON, TUE_LOCAL],
            local_rows=[MON],
            remote_rows=[MON, TUE_LOCAL, WED_REMOTE],
        )

        assert code == sync_merge.EXIT_OK
        assert entries_in(target) == [MON, WED_REMOTE]

    def test_remote_only_changes_are_left_alone(self, tmp_path: Path) -> None:
        code, target = run_merge(
            tmp_path,
            base_rows=[MON],
            local_rows=[MON],
            remote_rows=[MON, WED_REMOTE],
        )

        assert code == sync_merge.EXIT_OK
        assert entries_in(target) == [MON, WED_REMOTE]

    def test_composite_primary_key_merges(self, tmp_path: Path) -> None:
        base = make_db(tmp_path / "base.db")
        local = make_db(tmp_path / "local.db")
        target = make_db(tmp_path / "target.db")
        for db, line in ((local, (2026, 8, 1, 5)), (target, (2026, 8, 2, 3))):
            conn = sqlite3.connect(db)
            conn.execute("INSERT INTO bill_lines VALUES (?,?,?,?)", line)
            conn.commit()
            conn.close()

        code = sync_merge.main(
            ["--base", str(base), "--local", str(local), "--target", str(target)]
        )

        assert code == sync_merge.EXIT_OK
        conn = sqlite3.connect(target)
        assert conn.execute("SELECT * FROM bill_lines ORDER BY line_no").fetchall() == [
            (2026, 8, 1, 5),
            (2026, 8, 2, 3),
        ]
        conn.close()


class TestConflicts:
    """Both machines changing the same row must stop, not guess."""

    def test_same_day_different_values_conflicts(self, tmp_path: Path) -> None:
        mine = ("2026-07-20", "Mon", "09:45", 30, "16:15", None)
        theirs = ("2026-07-20", "Mon", "09:30", 30, "16:15", "house viewing")

        code, target = run_merge(
            tmp_path, base_rows=[], local_rows=[mine], remote_rows=[theirs]
        )

        assert code == sync_merge.EXIT_CONFLICT
        # Nothing applied: the target still holds exactly the remote's version.
        assert entries_in(target) == [theirs]

    def test_conflict_blocks_all_changes_not_just_the_clashing_one(
        self, tmp_path: Path
    ) -> None:
        clash_mine = ("2026-07-20", "Mon", "09:45", 30, "16:15", None)
        clash_theirs = ("2026-07-20", "Mon", "09:30", 30, "16:15", "viewing")

        code, target = run_merge(
            tmp_path,
            base_rows=[],
            local_rows=[clash_mine, TUE_LOCAL],
            remote_rows=[clash_theirs, WED_REMOTE],
        )

        assert code == sync_merge.EXIT_CONFLICT
        assert TUE_LOCAL not in entries_in(target)

    def test_identical_change_on_both_machines_is_not_a_conflict(
        self, tmp_path: Path
    ) -> None:
        code, target = run_merge(
            tmp_path,
            base_rows=[MON],
            local_rows=[MON, TUE_LOCAL],
            remote_rows=[MON, TUE_LOCAL],
        )

        assert code == sync_merge.EXIT_OK
        assert entries_in(target) == [MON, TUE_LOCAL]

    def test_local_edit_versus_remote_delete_conflicts(self, tmp_path: Path) -> None:
        edited = ("2026-08-10", "Mon", "10:00", 30, "18:30", None)
        code, _ = run_merge(
            tmp_path, base_rows=[MON], local_rows=[edited], remote_rows=[]
        )

        assert code == sync_merge.EXIT_CONFLICT

    def test_local_delete_versus_remote_edit_conflicts(self, tmp_path: Path) -> None:
        edited = ("2026-08-10", "Mon", "10:00", 30, "18:30", None)
        code, _ = run_merge(
            tmp_path, base_rows=[MON], local_rows=[], remote_rows=[edited]
        )

        assert code == sync_merge.EXIT_CONFLICT

    def test_conflict_report_names_the_differing_columns(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        mine = ("2026-07-20", "Mon", "09:45", 30, "16:15", None)
        theirs = ("2026-07-20", "Mon", "09:30", 30, "16:15", "house viewing")

        run_merge(tmp_path, base_rows=[], local_rows=[mine], remote_rows=[theirs])

        err = capsys.readouterr().err
        assert "time_entries [2026-07-20]" in err
        assert "clock_in" in err
        assert "09:45" in err and "09:30" in err
        # Columns that agree shouldn't be listed as differences.
        assert "day_of_week" not in err


class TestNoOpAndSafety:
    def test_no_local_changes_leaves_target_untouched(self, tmp_path: Path) -> None:
        code, target = run_merge(
            tmp_path,
            base_rows=[MON],
            local_rows=[MON],
            remote_rows=[MON, WED_REMOTE],
        )

        assert code == sync_merge.EXIT_OK
        assert entries_in(target) == [MON, WED_REMOTE]

    def test_live_db_is_never_written_to(self, tmp_path: Path) -> None:
        base = make_db(tmp_path / "base.db", [MON])
        local = make_db(tmp_path / "local.db", [MON, TUE_LOCAL])
        target = make_db(tmp_path / "target.db", [MON, WED_REMOTE])
        before = local.read_bytes()

        sync_merge.main(
            ["--base", str(base), "--local", str(local), "--target", str(target)]
        )

        assert local.read_bytes() == before

    def test_schema_drift_is_refused(self, tmp_path: Path) -> None:
        base = make_db(tmp_path / "base.db", [MON])
        local = make_db(tmp_path / "local.db", [MON])
        target = make_db(tmp_path / "target.db", [MON])
        conn = sqlite3.connect(local)
        conn.execute("ALTER TABLE time_entries ADD COLUMN mood TEXT")
        conn.commit()
        conn.close()

        code = sync_merge.main(
            ["--base", str(base), "--local", str(local), "--target", str(target)]
        )

        assert code == sync_merge.EXIT_ERROR

    def test_table_missing_from_remote_is_refused(self, tmp_path: Path) -> None:
        base = make_db(tmp_path / "base.db", [MON])
        local = make_db(tmp_path / "local.db", [MON])
        target = make_db(tmp_path / "target.db", [MON])
        for db in (base, local):
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE notes (id TEXT PRIMARY KEY)")
            conn.commit()
            conn.close()

        code = sync_merge.main(
            ["--base", str(base), "--local", str(local), "--target", str(target)]
        )

        assert code == sync_merge.EXIT_ERROR

    def test_table_without_primary_key_is_refused_only_when_changed(
        self, tmp_path: Path
    ) -> None:
        paths = [tmp_path / n for n in ("base.db", "local.db", "target.db")]
        for path in paths:
            make_db(path, [MON])
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE scratch (note TEXT)")
            conn.commit()
            conn.close()
        base, local, target = paths

        argv = ["--base", str(base), "--local", str(local), "--target", str(target)]
        assert sync_merge.main(argv) == sync_merge.EXIT_OK

        conn = sqlite3.connect(local)
        conn.execute("INSERT INTO scratch VALUES ('changed')")
        conn.commit()
        conn.close()

        assert sync_merge.main(argv) == sync_merge.EXIT_ERROR
