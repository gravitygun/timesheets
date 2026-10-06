"""Auto-sync of the DB to the data repo while the app is open, and its status.

The app does the cheap part (noticing writes, via an O(1) stat) and leaves
everything git-related to sync.sh, so there is only one sync implementation.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple

SYNC_INTERVAL = timedelta(minutes=15)
SYNC_SCRIPT = Path(__file__).resolve().parent / "sync.sh"
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")

Fingerprint = tuple[tuple[int, int] | None, ...]


def db_fingerprint(db_path: Path) -> Fingerprint:
    """Cheap O(1) marker that changes whenever the DB is written to.

    Every commit lands in the -wal file and every checkpoint rewrites the
    main file, so (mtime, size) of the pair moves on any write, from this
    process or the API. False positives (a checkpoint with no new data) are
    harmless: sync.sh's own dump-and-compare then finds nothing to push.
    The -shm file is deliberately excluded because readers touch it too.
    """
    def stat(path: Path) -> tuple[int, int] | None:
        try:
            st = path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    return (stat(db_path), stat(db_path.with_name(db_path.name + "-wal")))


class SyncResult(NamedTuple):
    """Outcome of one push; message (first error line) is set on failure."""

    ok: bool
    pushed: bool = False
    message: str = ""
    output: str = ""


def run_sync_push(script: Path = SYNC_SCRIPT, timeout: float = 120.0) -> SyncResult:
    """Run `sync.sh push` on behalf of the still-open app.

    --force-with-running because the app is, by definition, running (a dump
    is a single read transaction, so WAL gives it a consistent snapshot).
    --if-changed keeps a no-op tick offline. The script's own guards still
    apply: it refuses if another machine has pushed since our last sync.
    """
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        result = subprocess.run(
            [str(script), "push", "--if-changed", "--force-with-running"],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return SyncResult(False, message="sync.sh timed out")
    except OSError as exc:
        return SyncResult(False, message=f"couldn't run sync.sh: {exc}")
    stdout = _ANSI_ESCAPE.sub("", result.stdout)
    stderr = _ANSI_ESCAPE.sub("", result.stderr)
    output = "\n".join(part.rstrip() for part in (stdout, stderr) if part.strip())
    if result.returncode != 0:
        first = next(
            (line.strip() for line in stderr.splitlines() if line.strip()), None,
        )
        return SyncResult(
            False,
            message=first or f"sync.sh exited with {result.returncode}",
            output=output,
        )
    return SyncResult(True, pushed="Pushed." in stdout, output=output)


def format_when(moment: datetime, now: datetime) -> str:
    """Short timestamp: just the time today, the weekday this week."""
    if moment.date() == now.date():
        return moment.strftime("%H:%M")
    if now - moment < timedelta(days=6):
        return moment.strftime("%a %H:%M")
    return moment.strftime("%d %b %H:%M")


@dataclass
class SyncState:
    """Everything the status bar and sync dialog show, owned by the app."""

    enabled: bool = True
    running: bool = False
    # What the DB looked like at the last successful sync; None forces the
    # next check to run the script, catching changes from before startup.
    fingerprint: Fingerprint | None = None
    next_check: datetime | None = None
    last_checked: datetime | None = None
    last_outcome: str = ""
    last_push: datetime | None = None
    error: str | None = None
    error_at: datetime | None = None
    error_output: str = ""

    def record(self, result: SyncResult, fingerprint: Fingerprint, now: datetime) -> bool:
        """Fold a finished push into the state.

        Returns True if this is a new failure the user should be told about
        (failures keep retrying each check but only warn once per message).
        """
        self.running = False
        self.last_checked = now
        if result.ok:
            # Fingerprint was taken before the push, so a write that landed
            # mid-push still differs from it and gets picked up next check.
            self.fingerprint = fingerprint
            self.error = None
            self.error_at = None
            self.error_output = ""
            if result.pushed:
                self.last_push = now
                self.last_outcome = "pushed"
            else:
                self.last_outcome = "nothing to send"
            return False
        is_new = result.message != self.error
        self.error = result.message
        self.error_at = now
        self.error_output = result.output or result.message
        self.last_outcome = "failed"
        return is_new

    def summary(self, now: datetime) -> tuple[str, str]:
        """One-line status and a level: "ok", "busy", "error" or "muted"."""
        if not self.enabled:
            return ("Auto-sync off (not the default DB)", "muted")
        if self.running:
            return ("⟳ Syncing…", "busy")
        if self.error and self.error_at:
            return (
                f"⚠ Sync failed {format_when(self.error_at, now)} "
                "— press s for details",
                "error",
            )
        if self.last_checked is None:
            if self.next_check is None:
                return ("Auto-sync: not checked yet", "muted")
            return (
                f"Auto-sync: first check {format_when(self.next_check, now)}",
                "muted",
            )
        parts = ["✓ In sync"]
        if self.last_push:
            parts.append(f"last push {format_when(self.last_push, now)}")
        parts.append(f"checked {format_when(self.last_checked, now)}")
        return (" · ".join(parts), "ok")
