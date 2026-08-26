#!/usr/bin/env python3
"""Three-way row merge for the timesheet DB sync flow.

`sync.sh pull --keep-local` uses this to fold the changes made on this
machine into the dump another machine pushed, instead of forcing a choice
between the two.

Three SQLite databases go in:

    base     the dump this machine last synced with (the common ancestor)
    local    the live DB, carrying this machine's unsynced changes
    target   a restore of the incoming dump, modified in place

Every row this machine changed since ``base`` is replayed onto ``target``,
keyed by primary key. A row is only a conflict when *both* machines changed
the *same* row to *different* values — two machines adding different days,
or editing different tickets, merge silently. On any conflict nothing is
applied at all, so the caller can leave both sides untouched and let the
user decide.

Exit codes: 0 merged (possibly a no-op), 1 error, 3 conflicts (nothing
applied).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import quote as urlquote

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFLICT = 3

# How much of a long text column to show in a conflict report. Allocation
# descriptions run to several paragraphs; the first line is enough to tell
# two versions apart.
VALUE_WIDTH = 60

Row = tuple[Any, ...]
Key = tuple[Any, ...]


class MergeError(Exception):
    """The databases cannot be merged row-wise (schema drift, no PK, …)."""


@dataclass(frozen=True)
class Change:
    """A local change to replay onto the target."""

    table: str
    key: Key
    kind: str  # "added" | "updated" | "deleted"
    columns: tuple[str, ...]
    pk: tuple[str, ...]
    row: Row | None  # None for a deletion


@dataclass(frozen=True)
class Conflict:
    """The same row changed on both machines, in different ways."""

    table: str
    key: Key
    columns: tuple[str, ...]
    local: Row | None
    remote: Row | None


def quote(name: str) -> str:
    """Quote an SQL identifier."""
    return '"' + name.replace('"', '""') + '"'


def connect_ro(path: str) -> sqlite3.Connection:
    """Open a database read-only, so a bug here can never touch the live DB."""
    return sqlite3.connect(f"file:{urlquote(str(Path(path)))}?mode=ro", uri=True)


def user_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [str(r[0]) for r in rows]


def table_columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    info = conn.execute(f"PRAGMA table_info({quote(table)})").fetchall()
    return tuple(str(r[1]) for r in info)


def pk_columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    info = conn.execute(f"PRAGMA table_info({quote(table)})").fetchall()
    # r[5] is the 1-based position within the primary key, 0 if not part of it.
    ranked = sorted((int(r[5]), str(r[1])) for r in info if int(r[5]) > 0)
    return tuple(name for _, name in ranked)


def read_table(
    conn: sqlite3.Connection, table: str, columns: Sequence[str], pk: Sequence[str]
) -> dict[Key, Row]:
    """Read a table into a {primary key: row} map."""
    select = ", ".join(quote(c) for c in columns)
    rows = conn.execute(f"SELECT {select} FROM {quote(table)}").fetchall()
    key_at = [columns.index(c) for c in pk]
    return {tuple(row[i] for i in key_at): tuple(row) for row in rows}


def read_all_rows(
    conn: sqlite3.Connection, table: str, columns: Sequence[str]
) -> list[Row]:
    select = ", ".join(quote(c) for c in columns)
    return [tuple(r) for r in conn.execute(f"SELECT {select} FROM {quote(table)}")]


def sort_key(key: Key) -> tuple[tuple[bool, str], ...]:
    """Order keys of mixed/NULL types without tripping over comparisons."""
    return tuple((v is None, str(v)) for v in key)


def merge_table(
    table: str,
    columns: tuple[str, ...],
    pk: tuple[str, ...],
    base: dict[Key, Row],
    local: dict[Key, Row],
    remote: dict[Key, Row],
) -> tuple[list[Change], list[Conflict]]:
    """Work out which local row changes can be replayed onto the remote's."""
    changes: list[Change] = []
    conflicts: list[Conflict] = []

    for key in sorted(set(base) | set(local) | set(remote), key=sort_key):
        was = base.get(key)
        mine = local.get(key)
        theirs = remote.get(key)

        if mine == was:
            continue  # this machine didn't touch it; whatever the remote says wins
        if mine == theirs:
            continue  # both machines made the same change
        if theirs == was:
            # Only this machine touched it — safe to replay.
            kind = "deleted" if mine is None else "added" if was is None else "updated"
            changes.append(Change(table, key, kind, columns, pk, mine))
        else:
            conflicts.append(Conflict(table, key, columns, mine, theirs))

    return changes, conflicts


def plan_merge(
    base: sqlite3.Connection, local: sqlite3.Connection, target: sqlite3.Connection
) -> tuple[list[Change], list[Conflict]]:
    """Compare all three databases and plan the replay. Nothing is written."""
    base_tables = set(user_tables(base))
    local_tables = set(user_tables(local))
    target_tables = set(user_tables(target))

    missing = sorted((local_tables - target_tables) | (local_tables - base_tables))
    if missing:
        raise MergeError(
            "schema drift: table(s) "
            + ", ".join(missing)
            + " exist locally but not on both other sides — merge by hand"
        )

    changes: list[Change] = []
    conflicts: list[Conflict] = []

    for table in sorted(local_tables):
        columns = table_columns(local, table)
        if table_columns(base, table) != columns or table_columns(target, table) != columns:
            raise MergeError(
                f"schema drift: columns of {table} differ between machines — merge by hand"
            )

        pk = pk_columns(local, table)
        if not pk:
            # No primary key means no way to tell "same row, changed" from
            # "different row". Only a problem if this machine changed it.
            if read_all_rows(local, table, columns) != read_all_rows(base, table, columns):
                raise MergeError(
                    f"{table} has no primary key and changed locally — merge by hand"
                )
            continue

        table_changes, table_conflicts = merge_table(
            table,
            columns,
            pk,
            read_table(base, table, columns, pk),
            read_table(local, table, columns, pk),
            read_table(target, table, columns, pk),
        )
        changes.extend(table_changes)
        conflicts.extend(table_conflicts)

    return changes, conflicts


def apply_changes(target: sqlite3.Connection, changes: Sequence[Change]) -> None:
    """Replay local changes onto the target. Call only when there are no conflicts."""
    for change in changes:
        if change.row is None:
            # IS rather than = so a NULL in a composite key still matches.
            where = " AND ".join(f"{quote(c)} IS ?" for c in change.pk)
            target.execute(
                f"DELETE FROM {quote(change.table)} WHERE {where}", change.key
            )
        else:
            cols = ", ".join(quote(c) for c in change.columns)
            placeholders = ", ".join("?" for _ in change.columns)
            target.execute(
                f"INSERT OR REPLACE INTO {quote(change.table)} ({cols}) "
                f"VALUES ({placeholders})",
                change.row,
            )
    target.commit()


def format_value(value: Any) -> str:
    if value is None:
        return "(none)"
    text = " ".join(str(value).split())
    if len(text) > VALUE_WIDTH:
        text = text[: VALUE_WIDTH - 1] + "…"
    return text


def format_key(key: Key) -> str:
    return "/".join(format_value(k) for k in key)


def report_changes(changes: Sequence[Change], out: Any) -> None:
    noun = "change" if len(changes) == 1 else "changes"
    print(f"Merging {len(changes)} local {noun} into the incoming dump:", file=out)
    for change in changes:
        print(
            f"  {change.table:<20} {change.kind:<8} {format_key(change.key)}",
            file=out,
        )


def report_conflicts(conflicts: Sequence[Conflict], out: Any) -> None:
    noun = "row was" if len(conflicts) == 1 else "rows were"
    print(
        f"Cannot merge: {len(conflicts)} {noun} changed on both machines.",
        file=out,
    )
    for conflict in conflicts:
        print(f"\n  {conflict.table} [{format_key(conflict.key)}]", file=out)
        if conflict.local is None:
            print("      deleted here, changed on the other machine", file=out)
            continue
        if conflict.remote is None:
            print("      changed here, deleted on the other machine", file=out)
            continue
        for i, column in enumerate(conflict.columns):
            mine, theirs = conflict.local[i], conflict.remote[i]
            if mine != theirs:
                print(
                    f"      {column:<18} here={format_value(mine)}"
                    f"   other={format_value(theirs)}",
                    file=out,
                )


def foreign_key_breaks(target: sqlite3.Connection) -> list[str]:
    """Report rows the replay left pointing at something that no longer exists."""
    target.execute("PRAGMA foreign_keys = ON")
    rows = target.execute("PRAGMA foreign_key_check").fetchall()
    return [f"{r[0]} row {r[1]} -> {r[2]}" for r in rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay this machine's DB changes onto an incoming dump.",
    )
    parser.add_argument("--base", required=True, help="dump last synced with")
    parser.add_argument("--local", required=True, help="live DB (read-only)")
    parser.add_argument("--target", required=True, help="incoming dump, edited in place")
    args = parser.parse_args(argv)

    base = connect_ro(args.base)
    local = connect_ro(args.local)
    target = sqlite3.connect(args.target)
    try:
        try:
            changes, conflicts = plan_merge(base, local, target)
        except (MergeError, sqlite3.Error) as exc:
            print(f"Merge failed: {exc}", file=sys.stderr)
            return EXIT_ERROR

        if conflicts:
            report_conflicts(conflicts, sys.stderr)
            return EXIT_CONFLICT

        if not changes:
            print("No local changes to merge — taking the incoming dump as-is.")
            return EXIT_OK

        report_changes(changes, sys.stdout)
        apply_changes(target, changes)

        breaks = foreign_key_breaks(target)
        if breaks:
            print(
                "Merge failed: replaying local rows left dangling references:",
                file=sys.stderr,
            )
            for line in breaks:
                print(f"  {line}", file=sys.stderr)
            return EXIT_ERROR

        return EXIT_OK
    finally:
        base.close()
        local.close()
        target.close()


if __name__ == "__main__":
    sys.exit(main())
