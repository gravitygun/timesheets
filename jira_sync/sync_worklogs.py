#!/usr/bin/env python3
"""Make your Jira worklogs match a timesheets allocations export.

The timesheets app writes the export (R in the allocations view, format
JSON). This tool compares each allocation with *your* worklogs on the
matching Jira issue and day, then creates or updates worklogs so the two
agree. Nothing is written to Jira unless you run ``apply``, and every write
is journalled so ``undo`` can reverse it.

Commands:
    check            connection, credentials, user and timezone
    plan EXPORT      show what apply would do (read-only)
    apply EXPORT     make the changes, then re-read Jira to verify them
    verify EXPORT    exit 0 if Jira matches the export, 1 if not
    undo JOURNAL     reverse the changes recorded in a journal

Standalone on purpose: it is copied to the client site on its own, so it
must not import anything from the timesheets app. Needs Python 3.9+ and
requests. See README.md alongside this file for setup and a staged first run.
"""

from __future__ import annotations

import argparse
import configparser
import difflib
import getpass
import json
import logging
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, tzinfo
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:  # pragma: no cover - only on a machine not yet set up
    sys.exit("sync_worklogs needs the 'requests' package: "
             "pip install -r requirements.txt")

__version__ = "0.2.0"

log = logging.getLogger("jira_sync")

EXIT_OK = 0
EXIT_DIFFERENCES = 1  # verify found work to do, or an apply didn't verify
EXIT_USAGE = 2        # config, export or arguments need fixing
EXIT_JIRA = 3         # Jira couldn't be reached or refused a request

EXPORT_SCHEMA = "timesheets.allocations"
EXPORT_VERSION = 1

DEFAULT_HOME = Path("~/.jira-sync")
CONFIG_ENV = "JIRA_SYNC_CONFIG"
TOKEN_ENV = "JIRA_TOKEN"
PASSWORD_ENV = "JIRA_PASSWORD"

# Jira's timestamp format, e.g. 2026-10-23T09:00:00.000+0100
JIRA_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%f%z"
ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*-[0-9]+$")
PROJECT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Plan action kinds. Only the first three write to Jira.
CREATE = "create"
UPDATE = "update"
DELETE = "delete"
OK = "ok"
CONFLICT = "conflict"
EXTRA = "extra"
UNEXPECTED = "unexpected"
MISSING = "missing"
SKIP = "skip"
WRITES = (CREATE, UPDATE, DELETE)

# How apply lets you look over its changes before sending them.
REVIEW_MODES = ("none", "step", "editor")


class ConfigError(Exception):
    """A problem with the config, the export or the arguments."""


class JiraError(Exception):
    """Jira couldn't be reached, or refused a request.

    ``status`` is the HTTP status, or None when there was no usable HTTP
    response at all (network, TLS, a proxy page instead of JSON).
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


# --- Settings ---------------------------------------------------------------


@dataclass
class Settings:
    """Everything read from the config file and the token's sources."""

    url: str
    token: str | None = None
    token_source: str = ""
    username: str | None = None
    verify: bool | str = True
    timeout: float = 30.0
    issue_prefix: str = "MPIT-"
    project_map: dict[str, str] = field(default_factory=dict)
    issue_map: dict[str, str] = field(default_factory=dict)
    skip_projects: frozenset[str] = frozenset()
    timezone: str | None = None
    day_start: dtime = dtime(9, 0)
    adjust_estimate: str = "auto"
    update_comments: bool = False
    review: str = "none"
    journal_dir: Path = DEFAULT_HOME / "journals"


class _CaseSensitiveConfig(configparser.ConfigParser):
    """Keeps [issue_map] ticket IDs exactly as written (FD-2587, not fd-2587)."""

    def optionxform(self, optionstr: str) -> str:
        return optionstr


def config_path(explicit: str | None) -> Path:
    """--config, else $JIRA_SYNC_CONFIG, else ~/.jira-sync/config.ini."""
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get(CONFIG_ENV)
    if env:
        return Path(env).expanduser()
    return (DEFAULT_HOME / "config.ini").expanduser()


def _warn_if_shared(path: Path, what: str) -> None:
    """Warn, as ssh does, when a file holding a secret is readable by others."""
    if os.name != "posix":
        return
    if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        log.warning("%s %s can be read by other users; run: chmod 600 %s",
                    what, path, path)


def _get_bool(section: configparser.SectionProxy, key: str, default: bool) -> bool:
    try:
        return section.getboolean(key, fallback=default)
    except ValueError:
        raise ConfigError(f"[{section.name}] {key} must be true or false") from None


def load_settings(path: Path) -> Settings:
    """Read the INI config; the token may come from elsewhere (see README)."""
    if not path.is_file():
        raise ConfigError(
            f"no config file at {path} - copy config.example.ini there "
            "(see README.md)"
        )
    parser = _CaseSensitiveConfig(interpolation=None)  # tokens may contain %
    try:
        parser.read(path, encoding="utf-8")
    except configparser.Error as exc:
        raise ConfigError(f"cannot parse {path}: {exc}") from None
    for name in ("jira", "sync", "project_map", "issue_map"):
        if not parser.has_section(name):
            parser.add_section(name)
    jira = parser["jira"]
    sync = parser["sync"]

    url = jira.get("url", "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ConfigError(f"[jira] url in {path} must start with https:// "
                          f"(got {url!r})")

    # Token sources, first found wins: environment, config, token file.
    token = os.environ.get(TOKEN_ENV, "").strip() or None
    token_source = f"${TOKEN_ENV}" if token else ""
    if not token and jira.get("token", "").strip():
        token = jira.get("token", "").strip()
        token_source = str(path)
        _warn_if_shared(path, "Config file")
    if not token and jira.get("token_file", "").strip():
        token_file = Path(jira.get("token_file", "").strip()).expanduser()
        try:
            token = token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"cannot read token_file {token_file}: {exc}") from None
        if not token:
            raise ConfigError(f"token_file {token_file} is empty")
        token_source = str(token_file)
        _warn_if_shared(token_file, "Token file")
    username = jira.get("username", "").strip() or None
    if not token and not username:
        raise ConfigError(
            f"no credentials - set ${TOKEN_ENV}, or token, token_file or "
            f"username in [jira] of {path}"
        )

    raw_verify = jira.get("verify_ssl", "true").strip()
    verify: bool | str
    if raw_verify.lower() in ("true", "yes", "on", "1"):
        verify = True
    elif raw_verify.lower() in ("false", "no", "off", "0"):
        verify = False
    else:
        bundle = Path(raw_verify).expanduser()
        if not bundle.exists():
            raise ConfigError(f"[jira] verify_ssl: no CA bundle at {bundle}")
        verify = str(bundle)

    try:
        timeout = float(jira.get("timeout", "30"))
    except ValueError:
        raise ConfigError("[jira] timeout must be a number of seconds") from None

    prefix = sync.get("issue_prefix", "MPIT-").strip().upper()
    if not prefix.endswith("-"):
        prefix += "-"
    # Without a project key a bare number would reach Jira as an internal
    # issue ID, which names some unrelated issue.
    if not PROJECT_KEY_RE.match(prefix[:-1]):
        raise ConfigError(f"[sync] issue_prefix {prefix!r} must be a project "
                          "key and a dash, e.g. MPIT-")

    try:
        day_start = datetime.strptime(sync.get("day_start", "09:00").strip(),
                                      "%H:%M").time()
    except ValueError:
        raise ConfigError("[sync] day_start must be HH:MM, e.g. 09:00") from None

    adjust = sync.get("adjust_estimate", "auto").strip().lower()
    if adjust not in ("auto", "leave"):
        raise ConfigError("[sync] adjust_estimate must be auto or leave")

    project_map: dict[str, str] = {}
    for source, target in parser["project_map"].items():
        src, dst = source.strip().upper().rstrip("-"), target.strip().upper().rstrip("-")
        if not (PROJECT_KEY_RE.match(src) and PROJECT_KEY_RE.match(dst)):
            raise ConfigError(f"[project_map] {source} = {target!r}: use project "
                              "keys, e.g. FD = MPITFD")
        project_map[src] = dst

    review = sync.get("review", "none").strip().lower()
    if review not in REVIEW_MODES:
        raise ConfigError(f"[sync] review must be one of {', '.join(REVIEW_MODES)}")

    issue_map: dict[str, str] = {}
    for ticket_id, target in parser["issue_map"].items():
        target = target.strip()
        if target.lower() != "skip" and not ISSUE_KEY_RE.match(target.upper()):
            raise ConfigError(f"[issue_map] {ticket_id} = {target!r}: use a Jira "
                              "key such as MPIT-123, or skip")
        issue_map[ticket_id.strip()] = target

    return Settings(
        url=url,
        token=token,
        token_source=token_source,
        username=username,
        verify=verify,
        timeout=timeout,
        issue_prefix=prefix,
        project_map=project_map,
        issue_map=issue_map,
        skip_projects=frozenset(
            p.strip().upper().rstrip("-")
            for p in sync.get("skip_projects", "").split(",") if p.strip()
        ),
        timezone=sync.get("timezone", "").strip() or None,
        day_start=day_start,
        adjust_estimate=adjust,
        update_comments=_get_bool(sync, "update_comments", False),
        review=review,
        journal_dir=Path(sync.get("journal_dir", "~/.jira-sync/journals")
                         .strip()).expanduser(),
    )


def issue_key_for(ticket_id: str, settings: Settings) -> str | None:
    """The Jira key a timesheets ticket ID logs time against, or None.

    [issue_map] wins; then bare numbers get issue_prefix (10400 ->
    MPIT-10400), and IDs that are already keys keep their number but have
    their project renamed by [project_map] (FD-2587 -> MPITFD-2587). None
    means "leave alone": mapped to skip, or not recognisable as a key at all.
    """
    tid = ticket_id.strip()
    if tid in settings.issue_map:
        target = settings.issue_map[tid]
        return None if target.lower() == "skip" else target.upper()
    return _prefixed_key(tid, settings)


def _prefixed_key(ticket_id: str, settings: Settings) -> str | None:
    """issue_key_for without [issue_map]: the prefix and project rules."""
    if ticket_id.isdigit():
        key = f"{settings.issue_prefix}{ticket_id}"
    else:
        key = ticket_id.upper()
        project, _, number = key.rpartition("-")
        if project in settings.project_map:
            key = f"{settings.project_map[project]}-{number}"
    return key if ISSUE_KEY_RE.match(key) else None


def project_of(key: str) -> str:
    return key.rpartition("-")[0]


def natural_key(key: str) -> tuple[str, int, str]:
    """Sort MPIT-9 before MPIT-10."""
    project, _, number = key.rpartition("-")
    return (project, int(number), key) if number.isdigit() else (key, 0, key)


# --- Time -------------------------------------------------------------------


class Clock:
    """Turns Jira timestamps into calendar days, and days into timestamps.

    Days are judged in one timezone - normally your Jira profile's - so a
    worklog started at 00:30 BST lands on the right day even though Jira may
    hand it back as 23:30 UTC the day before. ``tz`` None means this
    machine's local time.
    """

    def __init__(self, tz: tzinfo | None, name: str) -> None:
        self.tz = tz
        self.name = name

    def day_of(self, moment: datetime) -> date:
        return moment.astimezone(self.tz).date()

    def at(self, day: date, wall: dtime) -> datetime:
        naive = datetime.combine(day, wall)
        if self.tz is None:
            return naive.astimezone()
        return naive.replace(tzinfo=self.tz)


def make_clock(configured: str | None, profile: str | None) -> Clock:
    """The [sync] timezone if set, else the Jira profile's, else local time."""
    for name, configured_here in ((configured, True), (profile, False)):
        if not name:
            continue
        try:
            from zoneinfo import ZoneInfo
            return Clock(ZoneInfo(name), name)
        except Exception as exc:  # ImportError, ZoneInfoNotFoundError, ...
            if configured_here:
                raise ConfigError(f"[sync] timezone {name!r} unusable: {exc}") from None
            log.warning("cannot load your Jira profile timezone %r (%s); "
                        "using this machine's local time", name, exc)
    return Clock(None, "this machine's local time")


def parse_jira_time(text: str) -> datetime:
    return datetime.strptime(text, JIRA_TIME_FORMAT)


def format_jira_time(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000%z")


def fmt_duration(seconds: int) -> str:
    hours, rest = divmod(int(seconds), 3600)
    minutes = rest // 60
    if hours and minutes:
        return f"{hours}h {minutes:02d}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def fmt_day(day: date) -> str:
    return day.strftime("%Y-%m-%d %a")


def norm_comment(text: str | None) -> str:
    """Comments compare equal if they differ only in trailing whitespace."""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip()


def preview(text: str, width: int = 56) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[:width - 3] + "..."


# --- The export -------------------------------------------------------------


@dataclass(frozen=True)
class ExportedAllocation:
    """One row of the export, exactly as timesheets wrote it."""

    ticket_id: str
    day: date
    seconds: int
    comment: str
    entered: bool


@dataclass
class Export:
    path: Path
    first_day: date
    last_day: date
    generated_at: str
    tickets: dict[str, str]
    allocations: list[ExportedAllocation]


def load_export(path: Path) -> Export:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from None
    except ValueError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from None
    if not isinstance(data, dict) or data.get("schema") != EXPORT_SCHEMA:
        raise ConfigError(f"{path} is not a timesheets allocations export "
                          "(export with R, format JSON)")
    if data.get("version") != EXPORT_VERSION:
        raise ConfigError(f"{path} is export version {data.get('version')}; "
                          f"sync_worklogs {__version__} reads version "
                          f"{EXPORT_VERSION}")
    try:
        first_day = date.fromisoformat(data["period"]["from"])
        last_day = date.fromisoformat(data["period"]["to"])
        tickets = {str(k): str(v) for k, v in data.get("tickets", {}).items()}
        rows = data["allocations"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"{path}: malformed export header ({exc!r})") from None

    allocations: list[ExportedAllocation] = []
    for index, row in enumerate(rows):
        try:
            allocations.append(ExportedAllocation(
                ticket_id=str(row["ticket_id"]).strip(),
                day=date.fromisoformat(row["date"]),
                # Whole minutes: Jira's own unit, so nothing can fail to settle
                seconds=round(float(row["hours"]) * 60) * 60,
                comment=str(row.get("description") or ""),
                entered=bool(row.get("entered_on_client", False)),
            ))
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"{path}: allocation #{index + 1} is malformed "
                              f"({exc!r})") from None
    return Export(path, first_day, last_day, str(data.get("generated_at", "")),
                  tickets, allocations)


@dataclass
class Allocation:
    """One issue-day of time, as the timesheet says Jira should have it."""

    issue_key: str
    day: date
    seconds: int
    comment: str
    entered: bool
    ticket_ids: tuple[str, ...]
    start: dtime = dtime(9, 0)  # wall-clock start, only used for new worklogs


def map_allocations(
    export: Export, settings: Settings,
) -> tuple[list[Allocation], list[tuple[ExportedAllocation, str]]]:
    """Key the export's allocations by Jira issue.

    Returns the allocations to sync plus (row, reason) for rows that will
    not be. Two ticket IDs mapped to the same issue on the same day are
    merged into one worklog, since matching is by issue and day.
    """
    merged: dict[tuple[str, date], Allocation] = {}
    skipped: list[tuple[ExportedAllocation, str]] = []
    for row in export.allocations:
        if row.seconds <= 0:
            log.warning("ignoring %s on %s: no time recorded", row.ticket_id, row.day)
            continue
        key = issue_key_for(row.ticket_id, settings)
        if key is None:
            if row.ticket_id in settings.issue_map:
                skipped.append((row, "mapped to skip in [issue_map]"))
            else:
                skipped.append((row, "not a Jira key - map it in [issue_map]"))
            continue
        if project_of(key) in settings.skip_projects:
            skipped.append((row, f"project {project_of(key)} is in skip_projects"))
            continue
        existing = merged.get((key, row.day))
        if existing is None:
            merged[(key, row.day)] = Allocation(
                key, row.day, row.seconds, row.comment.strip(), row.entered,
                (row.ticket_id,),
            )
        else:
            existing.seconds += row.seconds
            existing.comment = "\n\n".join(
                c for c in (existing.comment, row.comment.strip()) if c
            )
            existing.entered = existing.entered and row.entered
            existing.ticket_ids += (row.ticket_id,)

    allocations = list(merged.values())
    _assign_start_times(allocations, settings.day_start)
    return allocations, skipped


def _assign_start_times(allocations: list[Allocation], day_start: dtime) -> None:
    """Lay each day's worklogs end to end from day_start, in export order.

    Purely cosmetic - matching is by issue and day, never by start time - but
    it stops a day's new worklogs all claiming the same minute.
    """
    used: dict[date, int] = {}
    for alloc in allocations:
        offset = used.get(alloc.day, 0)
        start = datetime.combine(alloc.day, day_start) + timedelta(seconds=offset)
        alloc.start = start.time() if start.date() == alloc.day else day_start
        used[alloc.day] = offset + alloc.seconds


# --- Jira -------------------------------------------------------------------


def _describe_failure(method: str, url: str, resp: requests.Response) -> str:
    """A one-glance explanation of a failed call, with Jira's own messages."""
    messages: list[str] = []
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        messages += [str(m) for m in payload.get("errorMessages") or []]
        errors = payload.get("errors") or {}
        if isinstance(errors, dict):
            messages += [f"{k}: {v}" for k, v in errors.items()]

    hint = {
        401: "authentication failed - check the token (expired? revoked?)",
        403: "forbidden - this account lacks the permission for it",
        404: "not found, or not visible to this account",
    }.get(resp.status_code, "")
    if "CAPTCHA" in resp.headers.get("X-Authentication-Denied-Reason", ""):
        hint = ("Jira wants a CAPTCHA after failed logins - log in once in a "
                "browser, then retry")
    if 300 <= resp.status_code < 400:
        hint = (f"redirected to {resp.headers.get('Location')} - check [jira] "
                "url (an SSO login page?)")

    lines = [f"{method} {url} -> {resp.status_code} {resp.reason}"]
    if hint:
        lines.append(hint)
    if messages:
        lines.append("Jira says: " + "; ".join(messages))
    elif not hint:
        lines.append(resp.text[:200])
    return "\n  ".join(lines)


class JiraClient:
    """The handful of Jira REST v2 calls this tool needs, over one session."""

    def __init__(self, base_url: str, session: requests.Session,
                 timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session
        self.timeout = timeout

    def _call(self, method: str, path: str, *,
              params: dict[str, Any] | None = None,
              body: dict[str, Any] | None = None,
              missing_ok: bool = False) -> Any:
        url = f"{self.base_url}/rest/api/2/{path}"
        log.debug("%s %s %s", method, url, params or "")
        try:
            resp = self.session.request(
                method, url, params=params, json=body, timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.exceptions.SSLError as exc:
            raise JiraError(
                f"TLS failure talking to {self.base_url}: {exc}\n  If Jira "
                "uses an internal CA, set verify_ssl in [jira] to its PEM file"
            ) from None
        except requests.exceptions.RequestException as exc:
            raise JiraError(f"cannot reach {url}: {exc}") from None
        log.debug("-> %s %s", resp.status_code, resp.reason)

        if missing_ok and resp.status_code == 404:
            return None
        if resp.status_code >= 300:
            log.debug("response body: %s", resp.text[:2000])
            raise JiraError(_describe_failure(method, url, resp), resp.status_code)
        if resp.status_code == 204 or not resp.content:
            return None
        content_type = resp.headers.get("Content-Type", "")
        if "json" not in content_type:
            raise JiraError(
                f"{method} {url} returned {content_type or 'no content type'}, "
                "not JSON - is an SSO page or proxy in the way?"
            )
        return resp.json()

    def server_info(self) -> dict[str, Any]:
        return self._call("GET", "serverInfo") or {}

    def myself(self) -> dict[str, Any]:
        return self._call("GET", "myself") or {}

    def issue(self, key: str) -> dict[str, Any] | None:
        return self._call("GET", f"issue/{key}",
                          params={"fields": "summary,status,project"},
                          missing_ok=True)

    def worklogs(self, key: str) -> list[dict[str, Any]]:
        """Every worklog on an issue.

        Jira 8 returns them all in one go, but page anyway in case it doesn't.
        Coming up short is an error: a worklog we can't see is one we would
        create again.
        """
        found: list[dict[str, Any]] = []
        seen: set[str] = set()
        while True:
            params = {"startAt": len(found)} if found else None
            page = self._call("GET", f"issue/{key}/worklog", params=params) or {}
            fresh = [w for w in page.get("worklogs", [])
                     if str(w.get("id")) not in seen]
            seen.update(str(w.get("id")) for w in fresh)
            found.extend(fresh)
            total = int(page.get("total", len(found)))
            if len(found) >= total:
                return found
            if not fresh:
                raise JiraError(f"Jira listed only {len(found)} of {total} "
                                f"worklogs on {key}", status=200)

    def worklog(self, key: str, worklog_id: str) -> dict[str, Any] | None:
        return self._call("GET", f"issue/{key}/worklog/{worklog_id}",
                          missing_ok=True)

    def add_worklog(self, key: str, body: dict[str, Any],
                    adjust: str) -> dict[str, Any]:
        return self._call("POST", f"issue/{key}/worklog",
                          params={"adjustEstimate": adjust}, body=body) or {}

    def update_worklog(self, key: str, worklog_id: str, body: dict[str, Any],
                       adjust: str) -> dict[str, Any]:
        return self._call("PUT", f"issue/{key}/worklog/{worklog_id}",
                          params={"adjustEstimate": adjust}, body=body) or {}

    def delete_worklog(self, key: str, worklog_id: str, adjust: str) -> None:
        self._call("DELETE", f"issue/{key}/worklog/{worklog_id}",
                   params={"adjustEstimate": adjust})

    def search(self, jql: str) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        while True:
            page = self._call("GET", "search", params={
                "jql": jql, "fields": "summary",
                "startAt": len(issues), "maxResults": 100,
            }) or {}
            batch = page.get("issues", [])
            issues.extend(batch)
            if not batch or len(issues) >= int(page.get("total", 0)):
                return issues


def make_session(settings: Settings) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "Accept": "application/json",
        "X-Atlassian-Token": "no-check",
        "User-Agent": f"jira-sync/{__version__}",
    })
    if settings.token:
        session.headers["Authorization"] = f"Bearer {settings.token}"
    else:
        username = settings.username or ""
        password = os.environ.get(PASSWORD_ENV) or getpass.getpass(
            f"Jira password for {username}: ")
        session.auth = (username, password)
    session.verify = settings.verify
    if settings.verify is False:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        log.warning("TLS certificate checks are OFF (verify_ssl = false)")
    return session


@dataclass
class Worklog:
    """One of your worklogs, as Jira reports it."""

    id: str
    issue_key: str
    day: date
    started: str
    seconds: int
    comment: str
    visibility: dict[str, Any] | None = None

    def snapshot(self) -> dict[str, Any]:
        snap: dict[str, Any] = {"started": self.started,
                                "timeSpentSeconds": self.seconds,
                                "comment": self.comment}
        if self.visibility:
            snap["visibility"] = self.visibility
        return snap


def parse_worklog(issue_key: str, data: dict[str, Any], clock: Clock) -> Worklog:
    started = str(data["started"])
    return Worklog(
        id=str(data["id"]),
        issue_key=issue_key,
        day=clock.day_of(parse_jira_time(started)),
        started=started,
        seconds=int(data.get("timeSpentSeconds") or 0),
        comment=str(data.get("comment") or ""),
        visibility=data.get("visibility") or None,
    )


def is_mine(author: dict[str, Any] | None, me: dict[str, Any]) -> bool:
    if not author:
        return False
    if author.get("key") and me.get("key"):
        return author["key"] == me["key"]
    return bool(author.get("name")) and author.get("name") == me.get("name")


# --- Planning ---------------------------------------------------------------


@dataclass
class Selection:
    """Which (issue, day) pairs a run may look at or touch."""

    first_day: date
    last_day: date
    issues: frozenset[str] = frozenset()
    days: frozenset[date] = frozenset()
    exclude: frozenset[tuple[str, date]] = frozenset()

    def wants_issue(self, key: str) -> bool:
        return not self.issues or key in self.issues

    def wants_day(self, day: date) -> bool:
        return (self.first_day <= day <= self.last_day
                and (not self.days or day in self.days))

    def wants(self, key: str, day: date) -> bool:
        return (self.wants_issue(key) and self.wants_day(day)
                and (key, day) not in self.exclude)

    def describe(self) -> str:
        issues = ", ".join(sorted(self.issues, key=natural_key)) or "all issues"
        if self.days:
            days = ", ".join(d.isoformat() for d in sorted(self.days))
        else:
            days = f"{self.first_day} to {self.last_day}"
        extra = "; not those already entered" if self.exclude else ""
        return f"{issues}; {days}{extra}"


@dataclass
class Action:
    """One line of the plan: what to do (or not) about one issue-day."""

    kind: str
    issue_key: str
    day: date
    allocation: Allocation | None = None
    worklogs: list[Worklog] = field(default_factory=list)
    changes: dict[str, Any] = field(default_factory=dict)
    note: str = ""


@dataclass
class IssueState:
    key: str
    summary: str | None = None
    error: str | None = None
    worklogs: list[Worklog] = field(default_factory=list)  # yours, in scope


@dataclass
class Plan:
    actions: list[Action]
    allocations: list[Allocation]        # in scope
    states: dict[str, IssueState]
    descriptions: dict[str, str]         # issue key -> timesheet description

    def writes(self) -> list[Action]:
        return [a for a in self.actions if a.kind in WRITES]

    def count(self, kind: str) -> int:
        return sum(1 for a in self.actions if a.kind == kind)


@dataclass
class Context:
    settings: Settings
    client: JiraClient
    me: dict[str, Any]
    clock: Clock


def reconcile(
    key: str, allocations: list[Allocation], worklogs: list[Worklog], *,
    update_comments: bool, delete_extra: bool,
) -> list[Action]:
    """What would make one issue's worklogs (yours, in scope) match the timesheet.

    Matching is by day: each allocation pairs with your worklogs on that
    issue that day. One worklog is updated in place; several that add up
    are left alone; several that don't add up are a conflict for a human,
    because there is no safe way to guess which to change.
    """
    by_day: dict[date, list[Worklog]] = {}
    for wl in sorted(worklogs, key=lambda w: (w.day, w.started, w.id)):
        by_day.setdefault(wl.day, []).append(wl)
    wanted = {a.day: a for a in allocations}

    actions: list[Action] = []
    for day in sorted(set(wanted) | set(by_day)):
        alloc = wanted.get(day)
        logs = by_day.get(day, [])
        if alloc is None:
            kind = DELETE if delete_extra else EXTRA
            actions += [Action(kind, key, day, worklogs=[wl]) for wl in logs]
        elif not logs:
            note = "timesheets says this was already entered" if alloc.entered else ""
            actions.append(Action(CREATE, key, day, allocation=alloc, note=note))
        elif len(logs) == 1:
            changes: dict[str, Any] = {}
            if logs[0].seconds != alloc.seconds:
                changes["timeSpentSeconds"] = alloc.seconds
            if update_comments and norm_comment(logs[0].comment) != norm_comment(alloc.comment):
                changes["comment"] = alloc.comment
            actions.append(Action(UPDATE if changes else OK, key, day,
                                  allocation=alloc, worklogs=logs, changes=changes))
        else:
            total = sum(wl.seconds for wl in logs)
            if total == alloc.seconds:
                actions.append(Action(OK, key, day, allocation=alloc, worklogs=logs,
                                      note=f"split over {len(logs)} worklogs"))
            else:
                actions.append(Action(CONFLICT, key, day, allocation=alloc,
                                      worklogs=logs))
    return actions


def fetch_issue(ctx: Context, key: str, selection: Selection) -> IssueState:
    """The issue's summary and your in-scope worklogs on it.

    An HTTP refusal for one issue becomes its error (so the rest of the plan
    still shows); a dead connection is raised, since every issue would fail.
    """
    try:
        issue = ctx.client.issue(key)
        if issue is None:
            return IssueState(key, error="issue not found, or not visible to you")
        answered = str(issue.get("key", key)).upper()
        if answered != key.upper():
            return IssueState(key, error=f"Jira answered with {answered} (moved?) "
                                         "- map the ticket in [issue_map]")
        raw = ctx.client.worklogs(key)
    except JiraError as exc:
        if exc.status is None:
            raise
        return IssueState(key, error=str(exc).replace("\n  ", "; "))

    mine: list[Worklog] = []
    for data in raw:
        if not is_mine(data.get("author"), ctx.me):
            continue
        try:
            wl = parse_worklog(key, data, ctx.clock)
        except (KeyError, TypeError, ValueError) as exc:
            # Guessing could duplicate this worklog, so stop the issue cold.
            return IssueState(key, error=f"cannot read worklog {data.get('id')}: {exc!r}")
        if selection.wants(key, wl.day):
            mine.append(wl)
    summary = (issue.get("fields") or {}).get("summary")
    return IssueState(key, summary=summary, worklogs=mine)


def find_unexpected(ctx: Context, selection: Selection,
                    known: set[str]) -> list[IssueState]:
    """Issues not in the export where you logged time in the period."""
    # A day of slack each side: JQL judges dates in the server's view of
    # your timezone; Selection then filters precisely.
    first = selection.first_day - timedelta(days=1)
    last = selection.last_day + timedelta(days=1)
    jql = (f'worklogAuthor = currentUser() AND worklogDate >= "{first}" '
           f'AND worklogDate <= "{last}"')
    try:
        found = ctx.client.search(jql)
    except JiraError as exc:
        if exc.status is None:
            raise
        log.warning("could not search for your worklogs on other issues, so "
                    "that check is skipped: %s", str(exc).splitlines()[0])
        return []
    states: list[IssueState] = []
    for issue in found:
        key = str(issue.get("key"))
        if key in known or project_of(key) in ctx.settings.skip_projects:
            continue
        state = fetch_issue(ctx, key, selection)
        if state.worklogs or state.error:
            states.append(state)
    return states


def build_plan(ctx: Context, export: Export, selection: Selection, *,
               unentered_only: bool, delete_extra: bool) -> Plan:
    allocations, skipped = map_allocations(export, ctx.settings)
    if unentered_only:
        selection.exclude = frozenset(
            (a.issue_key, a.day) for a in allocations if a.entered)
    wanted = [a for a in allocations if selection.wants(a.issue_key, a.day)]

    descriptions: dict[str, str] = {}
    for alloc in wanted:
        descriptions.setdefault(alloc.issue_key,
                                export.tickets.get(alloc.ticket_ids[0], ""))

    exported = {a.issue_key for a in allocations}
    keys = sorted({a.issue_key for a in wanted} | set(selection.issues),
                  key=natural_key)
    states: dict[str, IssueState] = {}
    actions: list[Action] = []
    for key in keys:
        log.info("reading %s", key)
        state = states[key] = fetch_issue(ctx, key, selection)
        mine = [a for a in wanted if a.issue_key == key]
        if state.error:
            actions += [Action(MISSING, key, a.day, allocation=a, note=state.error)
                        for a in mine]
            continue
        if key not in exported:
            # Named with --issue but absent from the export: report, never
            # touch. Only issues the timesheet knows can lose a worklog.
            actions += [Action(UNEXPECTED, key, wl.day, worklogs=[wl])
                        for wl in state.worklogs]
            continue
        actions += reconcile(key, mine, state.worklogs,
                             update_comments=ctx.settings.update_comments,
                             delete_extra=delete_extra)

    if not selection.issues:
        # Issues whose tickets were deliberately skipped aren't unexpected.
        known = set(keys) | {k for k in (_prefixed_key(row.ticket_id, ctx.settings)
                                         for row, _ in skipped) if k}
        for state in find_unexpected(ctx, selection, known):
            states[state.key] = state
            if state.error:
                log.warning("%s: %s", state.key, state.error)
            actions += [Action(UNEXPECTED, state.key, wl.day, worklogs=[wl])
                        for wl in state.worklogs]
        for row, reason in skipped:
            if selection.wants_day(row.day):
                actions.append(Action(SKIP, row.ticket_id, row.day, note=reason,
                                      allocation=Allocation(
                                          row.ticket_id, row.day, row.seconds,
                                          row.comment, row.entered,
                                          (row.ticket_id,))))
                descriptions.setdefault(row.ticket_id,
                                        export.tickets.get(row.ticket_id, ""))

    return Plan(actions, wanted, states, descriptions)


# --- Output -----------------------------------------------------------------


def _summary_differs(description: str, summary: str) -> bool:
    """True when a Jira summary looks unrelated to the timesheet description."""
    a, b = description.casefold().strip(), summary.casefold().strip()
    if not a or not b or a in b or b in a:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() < 0.6


def _comment_diff(old: str, new: str) -> list[str]:
    return list(difflib.unified_diff(
        norm_comment(old).splitlines(), norm_comment(new).splitlines(),
        "Jira", "timesheet", lineterm="", n=1,
    ))


def describe_action(action: Action) -> str:
    alloc = action.allocation
    wl = action.worklogs[0] if action.worklogs else None
    if action.kind == CREATE and alloc:
        text = f'{fmt_duration(alloc.seconds):<8} "{preview(alloc.comment)}"'
    elif action.kind == UPDATE and alloc and wl:
        parts = []
        if "timeSpentSeconds" in action.changes:
            parts.append(f"{fmt_duration(wl.seconds)} -> {fmt_duration(alloc.seconds)}")
        if "comment" in action.changes:
            parts.append("comment")
        text = f"worklog {wl.id}: " + ", ".join(parts)
    elif action.kind == OK and alloc:
        text = fmt_duration(alloc.seconds)
    elif action.kind == CONFLICT and alloc:
        ids = ", ".join(w.id for w in action.worklogs)
        total = sum(w.seconds for w in action.worklogs)
        text = (f"timesheet {fmt_duration(alloc.seconds)}, but your "
                f"{len(action.worklogs)} worklogs ({ids}) total "
                f"{fmt_duration(total)} - fix by hand in Jira")
    elif action.kind == EXTRA and wl:
        text = (f"worklog {wl.id}, {fmt_duration(wl.seconds)}: no allocation "
                "that day (--delete-extra removes it)")
    elif action.kind == DELETE and wl:
        text = f"worklog {wl.id}, {fmt_duration(wl.seconds)}: no allocation that day"
    elif action.kind == UNEXPECTED and wl:
        text = (f"worklog {wl.id}, {fmt_duration(wl.seconds)}: this issue "
                "isn't in the export - check it by hand")
    elif action.kind in (MISSING, SKIP) and alloc:
        text = f"{fmt_duration(alloc.seconds):<8} {action.note}"
        return text
    else:
        text = ""
    if action.note:
        text += f"  ({action.note})"
    return text


def render_plan(plan: Plan, *, limit: int | None, show_comments: bool,
                changes_only: bool) -> list[str]:
    lines: list[str] = []
    groups: dict[str, list[Action]] = {}
    for action in plan.actions:
        groups.setdefault(action.issue_key, []).append(action)

    number = 0
    for key, actions in groups.items():
        if changes_only and all(a.kind == OK for a in actions):
            continue
        description = plan.descriptions.get(key, "")
        lines.append(f"{key}  {description}".rstrip())
        state = plan.states.get(key)
        if state and state.summary and \
                norm_comment(state.summary).casefold() != norm_comment(description).casefold():
            lines.append(f"    Jira: {state.summary}")
            if description and _summary_differs(description, state.summary):
                lines.append("    !! the Jira summary doesn't look like the "
                             "timesheet's - check the ticket mapping")
        for action in actions:
            if changes_only and action.kind == OK:
                continue
            label = ""
            suffix = ""
            if action.kind in WRITES:
                number += 1
                label = f"[{number}]"
                if limit is not None and number > limit:
                    suffix = "  (beyond --limit)"
            lines.append(f"  {label:>5} {action.kind:<10} {fmt_day(action.day)}  "
                         f"{describe_action(action)}{suffix}")
            if show_comments and action.allocation:
                if action.kind == CREATE:
                    lines += ["          | " + ln
                              for ln in norm_comment(action.allocation.comment).splitlines()]
                elif "comment" in action.changes and action.worklogs:
                    lines += ["          " + ln for ln in _comment_diff(
                        action.worklogs[0].comment, action.allocation.comment)]
        lines.append("")

    lines.append(
        f"Summary: {plan.count(CREATE)} to create, {plan.count(UPDATE)} to update, "
        f"{plan.count(DELETE)} to delete; {plan.count(OK)} in sync; "
        f"{plan.count(CONFLICT)} conflicts, {plan.count(EXTRA)} extra, "
        f"{plan.count(UNEXPECTED)} unexpected; {plan.count(MISSING)} missing, "
        f"{plan.count(SKIP)} skipped"
    )
    return lines


def print_header(ctx: Context, export: Export, selection: Selection) -> None:
    me = ctx.me
    print(f"Export:  {export.path.name}  ({export.first_day} to {export.last_day}, "
          f"{len(export.allocations)} allocations, generated {export.generated_at})")
    print(f"Jira:    {ctx.settings.url} as {me.get('displayName')} "
          f"({me.get('name')}); days judged in {ctx.clock.name}")
    print(f"Scope:   {selection.describe()}")
    print(f"Tool:    sync_worklogs {__version__}")
    print()


# --- Writing ----------------------------------------------------------------


class Journal:
    """Append-only JSON Lines record of every write, flushed as it happens.

    The first line describes the run; each later line is one write with the
    worklog's content before and after, which is all ``undo`` needs.
    """

    def __init__(self, directory: Path, command: str,
                 header: dict[str, Any]) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = directory / f"{command}-{stamp}.jsonl"
        n = 1
        while path.exists():
            path = directory / f"{command}-{stamp}_{n}.jsonl"  # sorts after
            n += 1
        self.path = path
        self._fh = path.open("w", encoding="utf-8")
        self.write({"type": "run", "command": command, "tool": __version__,
                    "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    **header})

    def write(self, record: dict[str, Any]) -> None:
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def op(self, op: str, key: str, worklog_id: str, day: date, **detail: Any) -> None:
        self.write({"type": "op", "op": op, "issue": key, "worklog_id": worklog_id,
                    "day": day.isoformat(), **detail})

    def close(self) -> None:
        self._fh.close()


def _snapshot(data: dict[str, Any]) -> dict[str, Any]:
    snap = {"started": data.get("started"),
            "timeSpentSeconds": data.get("timeSpentSeconds"),
            "comment": data.get("comment") or ""}
    if data.get("visibility"):
        snap["visibility"] = data["visibility"]
    return snap


def _same_moment(a: Any, b: Any) -> bool:
    try:
        return parse_jira_time(str(a)) == parse_jira_time(str(b))
    except ValueError:
        return a == b


def _same_content(current: dict[str, Any], snapshot: dict[str, Any]) -> bool:
    """Same start, time and comment: what undo needs before touching a worklog."""
    return (int(current.get("timeSpentSeconds") or 0)
            == int(snapshot.get("timeSpentSeconds") or 0)
            and norm_comment(current.get("comment")) == norm_comment(snapshot.get("comment"))
            and _same_moment(current.get("started"), snapshot.get("started")))


def _update_body(changes: dict[str, Any], before: dict[str, Any]) -> dict[str, Any]:
    """A PUT body that restates start and visibility, not just the changes.

    Jira's docs say omitted fields are left alone; restating them costs
    nothing and doesn't depend on that holding for every 8.x release.
    """
    body = {"started": before["started"], **changes}
    if before.get("visibility"):
        body["visibility"] = before["visibility"]
    return body


def execute(ctx: Context, action: Action, journal: Journal) -> str:
    """Make one planned change in Jira and journal it. Returns the worklog ID."""
    client = ctx.client
    adjust = ctx.settings.adjust_estimate
    key, day = action.issue_key, action.day
    if action.kind == CREATE:
        alloc = action.allocation
        assert alloc is not None
        body = {"started": format_jira_time(ctx.clock.at(day, alloc.start)),
                "timeSpentSeconds": alloc.seconds, "comment": alloc.comment}
        created = client.add_worklog(key, body, adjust)
        worklog_id = str(created.get("id"))
        journal.op("create", key, worklog_id, day, after=_snapshot(created))
        return worklog_id
    wl = action.worklogs[0]
    if action.kind == UPDATE:
        updated = client.update_worklog(
            key, wl.id, _update_body(action.changes, wl.snapshot()), adjust)
        journal.op("update", key, wl.id, day, changed=sorted(action.changes),
                   before=wl.snapshot(), after=_snapshot(updated))
        return wl.id
    if action.kind == DELETE:
        client.delete_worklog(key, wl.id, adjust)
        journal.op("delete", key, wl.id, day, before=wl.snapshot())
        return wl.id
    raise ValueError(f"not a write: {action.kind}")


def describe_done(action: Action, worklog_id: str) -> str:
    verb = {CREATE: "created", UPDATE: "updated", DELETE: "deleted"}[action.kind]
    return (f"{verb} worklog {worklog_id} on {action.issue_key} "
            f"{fmt_day(action.day)}  {describe_action(action)}")


def _recheck(ctx: Context, plan: Plan, selection: Selection, key: str,
             delete_extra: bool) -> tuple[IssueState, list[Action]]:
    """Re-read one issue and reconcile it again against the same allocations."""
    state = fetch_issue(ctx, key, selection)
    if state.error:
        return state, []
    return state, reconcile(key, [a for a in plan.allocations if a.issue_key == key],
                            state.worklogs,
                            update_comments=ctx.settings.update_comments,
                            delete_extra=delete_extra)


def _signature(action: Action) -> tuple[Any, ...]:
    return (action.kind, action.issue_key, action.day,
            tuple(wl.id for wl in action.worklogs),
            tuple(sorted((k, str(v)) for k, v in action.changes.items())))


def changed_since_planned(ctx: Context, plan: Plan, selection: Selection,
                          todo: list[Action], delete_extra: bool) -> list[str]:
    """Issues whose worklogs moved on while the plan waited for a 'yes'."""
    stale: list[str] = []
    for key in sorted({a.issue_key for a in todo}, key=natural_key):
        state, now = _recheck(ctx, plan, selection, key, delete_extra)
        current = {_signature(a) for a in now}
        if state.error or any(_signature(a) not in current
                              for a in todo if a.issue_key == key):
            stale.append(key)
    return stale


def verify_applied(ctx: Context, plan: Plan, selection: Selection,
                   applied: list[tuple[Action, str]], delete_extra: bool) -> list[str]:
    """Re-read every issue touched; return what still disagrees (empty = good)."""
    problems: list[str] = []
    for key in sorted({a.issue_key for a, _ in applied}, key=natural_key):
        state, after = _recheck(ctx, plan, selection, key, delete_extra)
        if state.error:
            problems.append(f"{key}: could not re-read it: {state.error}")
            continue
        unsettled = {a.day for a in after if a.kind in (CREATE, UPDATE, CONFLICT)}
        present = {wl.id for wl in state.worklogs}
        for action, worklog_id in applied:
            if action.issue_key != key:
                continue
            where = f"{key} {fmt_day(action.day)}"
            if action.kind == DELETE:
                if worklog_id in present:
                    problems.append(f"{where}: worklog {worklog_id} is still there")
            elif action.day in unsettled:
                problems.append(f"{where}: Jira still differs from the timesheet")
            elif action.kind == CREATE and worklog_id not in present:
                problems.append(f"{where}: new worklog {worklog_id} not found")
    return problems


def confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise ConfigError("not running interactively - add --yes to go ahead")
    return input(f"{question} Type 'yes' to go ahead: ").strip().lower() == "yes"


# --- Editing comments -------------------------------------------------------


EDIT_HEADER_RE = re.compile(r"^=== \[\d+\] \w+ (\S+) (\d{4}-\d{2}-\d{2})\b")

EDIT_INSTRUCTIONS = """\
# Comments for this run's worklogs. Edit them, then save and quit (:wq).
#
#  - Each comment sits under its "=== [n]" line; leave those lines as they are.
#  - Delete a whole block, === line and all, to leave that worklog out.
#  - Quit with :cq to change nothing at all.
#  - Lines starting with # up here are ignored. Inside a block, every line is
#    part of the comment.
"""


def _editable(todo: list[Action]) -> list[tuple[int, Action]]:
    """The run's writes that send a comment, with their plan numbers."""
    return [(n, a) for n, a in enumerate(todo, 1)
            if a.allocation and (a.kind == CREATE or "comment" in a.changes)]


def edit_file_text(editable: list[tuple[int, Action]],
                   earlier: dict[tuple[str, date], str]) -> str:
    blocks = [EDIT_INSTRUCTIONS]
    for n, action in editable:
        alloc = action.allocation
        assert alloc is not None
        comment = earlier.get((action.issue_key, action.day), alloc.comment)
        blocks.append(f"=== [{n}] {action.kind} {action.issue_key} {fmt_day(action.day)} "
                      f"{fmt_duration(alloc.seconds)}\n{norm_comment(comment)}\n")
    return "\n".join(blocks)


def parse_edit_file(text: str) -> dict[tuple[str, date], str]:
    """The comment for each (issue, day) block. ValueError says what's wrong.

    Blocks are keyed by issue and day, not by their [n], so a file edited
    for one run can seed the next even if the numbering has moved.
    """
    blocks: dict[tuple[str, date], list[str]] = {}
    current: tuple[str, date] | None = None
    for line in text.splitlines():
        match = EDIT_HEADER_RE.match(line)
        if match:
            try:
                current = (match.group(1), date.fromisoformat(match.group(2)))
            except ValueError:
                raise ValueError(f"bad date in the line {line!r}") from None
            if current in blocks:
                raise ValueError(f"{current[0]} {current[1]} appears twice")
            blocks[current] = []
        elif line.startswith("=== ["):
            raise ValueError(f"can't read the line {line!r}")
        elif current is not None:
            blocks[current].append(line)
        elif line.strip() and not line.startswith("#"):
            raise ValueError(f"text before the first === line: {preview(line, 40)!r}")
    comments = {key: norm_comment("\n".join(lines)) for key, lines in blocks.items()}
    for (key, day), comment in comments.items():
        if not comment:
            raise ValueError(f"{key} {day} has no comment - write one, or delete "
                             "the whole block to leave that worklog out")
    return comments


def load_edits(path: Path) -> dict[tuple[str, date], str]:
    try:
        return parse_edit_file(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from None
    except ValueError as exc:
        raise ConfigError(f"{path}: {exc}") from None


def run_editor(path: Path) -> bool:
    """Open path in $VISUAL, $EDITOR, vim or vi. False if it exited with an error."""
    editor = (os.environ.get("VISUAL") or os.environ.get("EDITOR")
              or shutil.which("vim") or shutil.which("vi") or "vi")
    command = [*shlex.split(editor, posix=os.name != "nt"), str(path)]
    try:
        return subprocess.run(command).returncode == 0
    except OSError as exc:
        raise ConfigError(f"cannot start the editor {editor!r} ({exc}); "
                          "set $EDITOR") from None


def _set_comment(action: Action, comment: str) -> None:
    """Send this comment instead; verification then checks for it too."""
    alloc = action.allocation
    assert alloc is not None
    alloc.comment = comment
    if action.kind == UPDATE:
        action.changes["comment"] = comment


def edit_comments(ctx: Context, plan: Plan, todo: list[Action],
                  earlier: dict[tuple[str, date], str]) -> list[Action] | None:
    """Let the user rewrite all this run's comments in one editor session.

    Returns the writes to go ahead with (blocks deleted in the editor are
    left out), or None to stop. The edited text replaces the allocation's
    comment, so post-apply verification checks what was actually sent.
    """
    editable = _editable(todo)
    if not editable:
        print("No comments to edit in this run.")
        return todo
    if not sys.stdin.isatty():
        raise ConfigError("editing comments needs an interactive terminal")
    ctx.settings.journal_dir.mkdir(parents=True, exist_ok=True)
    path = ctx.settings.journal_dir / f"edit-{datetime.now():%Y%m%d-%H%M%S}.txt"
    path.write_text(edit_file_text(editable, earlier), encoding="utf-8")

    wanted = {(a.issue_key, a.day) for _, a in editable}
    while True:
        if not run_editor(path):
            print(f"The editor exited with an error (:cq?), so nothing was changed. "
                  f"The file is {path}")
            return None
        try:
            edited = parse_edit_file(path.read_text(encoding="utf-8"))
            strays = sorted(set(edited) - wanted)
            if strays:
                raise ValueError(f"{strays[0][0]} {strays[0][1]} isn't one of this "
                                 "run's changes")
            break
        except ValueError as exc:
            print(f"Can't use the edited comments: {exc}")
            if input("Edit again? [Y/n] ").strip().lower().startswith("n"):
                print(f"Nothing changed. Your edits are in {path}")
                return None

    print(f"Edited comments saved in {path}")
    print(f"  (a later apply can start from them with: --edits {path})")
    editable_ids = {id(a) for _, a in editable}
    keep: list[Action] = []
    dropped: list[Action] = []
    rewritten = 0
    for action in todo:
        if id(action) not in editable_ids:
            keep.append(action)
            continue
        comment = edited.get((action.issue_key, action.day))
        if comment is None:
            dropped.append(action)
            continue
        alloc = action.allocation
        assert alloc is not None
        if comment != norm_comment(alloc.comment):
            rewritten += 1
        _set_comment(action, comment)
        keep.append(action)
    print(f"Comments: {rewritten} rewritten, "
          f"{len(editable) - rewritten - len(dropped)} as exported, "
          f"{len(dropped)} worklog(s) left out")
    for action in dropped:
        print(f"  left out: {action.issue_key} {fmt_day(action.day)}")
    return keep


STEP_HELP = """\
    y  send this change as shown
    e  edit the comment in your editor, then decide
    s  skip it this run (a later apply offers it again)
    a  send this one and all the rest without asking
    q  stop reviewing; the rest are skipped this run"""


def _wrapped(text: str, indent: str = "      ") -> list[str]:
    lines: list[str] = []
    for paragraph in norm_comment(text).split("\n"):
        lines += textwrap.wrap(paragraph, width=78, initial_indent=indent,
                               subsequent_indent=indent) or [""]
    return lines


def _show_step(action: Action, index: int, total: int, plan: Plan) -> None:
    alloc = action.allocation
    wl = action.worklogs[0] if action.worklogs else None
    if action.kind == CREATE and alloc:
        what = fmt_duration(alloc.seconds)
    elif action.kind == UPDATE and alloc and wl:
        what = f"worklog {wl.id}, " + (
            f"{fmt_duration(wl.seconds)} -> {fmt_duration(alloc.seconds)}"
            if "timeSpentSeconds" in action.changes else fmt_duration(wl.seconds))
    elif wl:
        what = f"worklog {wl.id}, {fmt_duration(wl.seconds)}: no allocation that day"
    else:
        what = ""
    print()
    print(f"[{index}/{total}] {action.kind.upper()}  {action.issue_key}  "
          f"{fmt_day(action.day)}  {what}")
    description = plan.descriptions.get(action.issue_key, "")
    if description:
        print(f"      {description}")
    state = plan.states.get(action.issue_key)
    if state and state.summary and state.summary.casefold() != description.casefold():
        print(f"      Jira: {state.summary}")
    if action.note:
        print(f"      ({action.note})")
    print()
    if action.kind == CREATE and alloc:
        print("    Comment:")
        print("\n".join(_wrapped(alloc.comment)))
    elif action.kind == UPDATE and alloc and wl and "comment" in action.changes:
        print("    Comment now:")
        print("\n".join(_wrapped(wl.comment)))
        print("    Will become:")
        print("\n".join(_wrapped(alloc.comment)))
    elif action.kind == UPDATE:
        print("    (comment left as it is)")
    elif wl:
        print("    Its comment:")
        print("\n".join(_wrapped(wl.comment)))
    print()


def _edit_one(ctx: Context, comment: str) -> str | None:
    """Edit one comment in the editor. None leaves it as it was."""
    ctx.settings.journal_dir.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix="comment-", suffix=".txt",
                                    dir=ctx.settings.journal_dir)
    path = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(norm_comment(comment) + "\n")
        if not run_editor(path):
            print("    The editor exited with an error; comment unchanged.")
            return None
        edited = norm_comment(path.read_text(encoding="utf-8"))
    finally:
        path.unlink()
    if not edited:
        print("    The comment came back empty; left as it was.")
        return None
    return edited


def step_review(ctx: Context, plan: Plan, todo: list[Action],
                earlier: dict[tuple[str, date], str]) -> list[Action] | None:
    """Walk through the changes one at a time: send, edit, skip or stop.

    Nothing is sent here; the approved changes go after the usual final
    'yes'. Edited comments are saved as they're made, in the same format as
    --edit, so --edits can pick them up if this run goes no further.
    """
    if not sys.stdin.isatty():
        raise ConfigError("--step needs an interactive terminal")
    editable_ids = {id(a) for _, a in _editable(todo)}
    rewritten: list[tuple[int, Action]] = []
    for index, action in enumerate(todo, 1):
        comment = earlier.get((action.issue_key, action.day))
        if comment and id(action) in editable_ids:
            _set_comment(action, comment)
            rewritten.append((index, action))

    edits_path = ctx.settings.journal_dir / f"edit-{datetime.now():%Y%m%d-%H%M%S}.txt"
    keep: list[Action] = []
    skipped = 0
    send_rest = stop = False
    for index, action in enumerate(todo, 1):
        if send_rest:
            keep.append(action)
            continue
        editable = id(action) in editable_ids
        choices = "[y]es  " + ("[e]dit  " if editable else "") + \
            "[s]kip  [a]ll the rest  [q]uit  [?]"
        _show_step(action, index, len(todo), plan)
        while True:
            answer = input(f"  {choices} ").strip().lower()[:1]
            if answer == "y":
                keep.append(action)
            elif answer == "a":
                keep.append(action)
                send_rest = True
            elif answer == "s":
                skipped += 1
            elif answer == "q":
                stop = True
            elif answer == "e" and editable:
                assert action.allocation is not None
                comment = _edit_one(ctx, action.allocation.comment)
                if comment is not None:
                    _set_comment(action, comment)
                    if all(a is not action for _, a in rewritten):
                        rewritten.append((index, action))
                    edits_path.write_text(edit_file_text(rewritten, {}),
                                          encoding="utf-8")
                    _show_step(action, index, len(todo), plan)
                continue
            else:
                print(STEP_HELP)
                continue
            break
        if stop:
            break

    unreviewed = len(todo) - len(keep) - skipped
    print(f"\nReviewed: {len(keep)} to send, {skipped} skipped"
          + (f", {unreviewed} not reviewed" if unreviewed else ""))
    if edits_path.exists():
        print(f"Edited comments saved in {edits_path}")
        print(f"  (a later apply can start from them with: --edits {edits_path})")
    return keep


# --- Commands ---------------------------------------------------------------


def connect(args: argparse.Namespace) -> Context:
    settings = load_settings(config_path(args.config))
    client = JiraClient(settings.url, make_session(settings), settings.timeout)
    me = client.myself()
    return Context(settings, client, me, make_clock(settings.timezone,
                                                     me.get("timeZone")))


def make_selection(args: argparse.Namespace, export: Export,
                   settings: Settings) -> Selection:
    first = max(export.first_day, args.date_from or export.first_day)
    last = min(export.last_day, args.date_to or export.last_day)
    if first > last:
        raise ConfigError(f"--from/--to leave nothing of the export's period "
                          f"({export.first_day} to {export.last_day})")
    outside = [d for d in args.date if not first <= d <= last]
    if outside:
        raise ConfigError(f"--date {outside[0]} is outside {first} to {last}")
    issues: set[str] = set()
    for raw in args.issue:
        key = issue_key_for(raw, settings)
        if key is None:
            raise ConfigError(f"--issue {raw}: not a Jira key, or mapped to skip")
        if project_of(key) in settings.skip_projects:
            raise ConfigError(f"--issue {raw}: project {project_of(key)} is "
                              "in skip_projects")
        issues.add(key)
    return Selection(first, last, frozenset(issues), frozenset(args.date))


def _prepare(args: argparse.Namespace) -> tuple[Context, Export, Selection, Plan]:
    export = load_export(Path(args.export).expanduser())
    ctx = connect(args)
    selection = make_selection(args, export, ctx.settings)
    print_header(ctx, export, selection)
    plan = build_plan(ctx, export, selection, unentered_only=args.unentered_only,
                      delete_extra=args.delete_extra)
    return ctx, export, selection, plan


def cmd_check(args: argparse.Namespace) -> int:
    settings = load_settings(config_path(args.config))
    client = JiraClient(settings.url, make_session(settings), settings.timeout)
    me = client.myself()
    try:
        info = client.server_info()
        server = (f"{info.get('serverTitle', '?')}, Jira {info.get('version', '?')}, "
                  f"{info.get('deploymentType', '?')}")
    except JiraError as exc:
        server = f"server info unavailable ({str(exc).splitlines()[0]})"
    clock = make_clock(settings.timezone, me.get("timeZone"))
    auth = (f"personal access token from {settings.token_source}" if settings.token
            else f"password for {settings.username}")

    print(f"Jira:       {settings.url}  ({server})")
    print(f"User:       {me.get('displayName')}  (name {me.get('name')}, "
          f"key {me.get('key')})")
    print(f"Auth:       {auth}")
    print(f"Days in:    {clock.name}")
    renames = "".join(f", {src}-1 -> {dst}-1"
                      for src, dst in sorted(settings.project_map.items()))
    print(f"Mapping:    12345 -> {issue_key_for('12345', settings)}{renames}; "
          f"skip projects: {', '.join(sorted(settings.skip_projects)) or 'none'}; "
          f"{len(settings.issue_map)} [issue_map] entries")
    comments = ("kept in step with timesheets" if settings.update_comments
                else "set on create, then yours")
    if settings.review != "none":
        comments += f", reviewed first ({settings.review})"
    print(f"Writes:     adjustEstimate={settings.adjust_estimate}, comments "
          f"{comments}, new worklogs from {settings.day_start:%H:%M}")
    print(f"Journals:   {settings.journal_dir}")
    print(f"Tool:       sync_worklogs {__version__}, Python "
          f"{platform.python_version()}, requests {getattr(requests, '__version__', '?')}")

    if args.issue:
        key = issue_key_for(args.issue, settings) or args.issue.upper()
        issue = client.issue(key)
        if issue is None:
            print(f"Issue:      {key} not found, or not visible to you")
            return EXIT_JIRA
        fields = issue.get("fields") or {}
        status = (fields.get("status") or {}).get("name", "?")
        raw = client.worklogs(key)
        mine = sorted((parse_worklog(key, w, clock) for w in raw
                       if is_mine(w.get("author"), me)),
                      key=lambda w: (w.day, w.started))
        print(f"Issue:      {key}  {fields.get('summary')}  [{status}]")
        print(f"            {len(raw)} worklogs, {len(mine)} of them yours"
              + ("; your latest:" if mine else ""))
        for wl in mine[-5:]:
            print(f"            {fmt_day(wl.day)}  {fmt_duration(wl.seconds):<8} "
                  f"{preview(wl.comment, 40)}")
    print("OK")
    return EXIT_OK


def cmd_plan(args: argparse.Namespace) -> int:
    _, _, _, plan = _prepare(args)
    print("\n".join(render_plan(plan, limit=args.limit,
                                show_comments=args.show_comments,
                                changes_only=args.changes_only)))
    writes = len(plan.writes())
    if writes:
        print(f"Nothing has been changed. apply with the same options would make "
              f"{min(writes, args.limit or writes)} change(s).")
    return EXIT_OK


def cmd_verify(args: argparse.Namespace) -> int:
    _, _, _, plan = _prepare(args)
    print("\n".join(render_plan(plan, limit=None,
                                show_comments=args.show_comments,
                                changes_only=args.changes_only)))
    differences = sum(plan.count(k) for k in
                      (*WRITES, CONFLICT, EXTRA, UNEXPECTED, MISSING))
    if differences:
        print(f"NOT IN SYNC: {differences} difference(s) between Jira and the export.")
        return EXIT_DIFFERENCES
    print("IN SYNC: Jira matches the export for this scope.")
    return EXIT_OK


def cmd_apply(args: argparse.Namespace) -> int:
    ctx, export, selection, plan = _prepare(args)
    print("\n".join(render_plan(plan, limit=args.limit,
                                show_comments=args.show_comments,
                                changes_only=args.changes_only)))
    print()

    missing = sorted({a.issue_key for a in plan.actions if a.kind == MISSING},
                     key=natural_key)
    if missing and not args.skip_missing:
        print(f"Stopped: {len(missing)} issue(s) could not be read "
              f"({', '.join(missing)}). Fix the mapping, narrow with --issue, "
              "or add --skip-missing to sync the rest.")
        return EXIT_JIRA

    todo = plan.writes()[:args.limit] if args.limit else plan.writes()
    if not todo:
        print("Nothing to change.")
        return EXIT_OK

    # Timesheets says these were typed in by hand, yet no worklog matched:
    # far more likely a day, timezone or mapping slip than truly missing.
    entered = [a for a in todo if a.kind == CREATE and a.allocation
               and a.allocation.entered]
    if entered and not args.create_entered:
        print(f"Stopped: {len(entered)} new worklog(s) would be for allocations "
              "timesheets marks as already entered on client, so the hand-entered "
              "worklogs weren't recognised (wrong day, timezone or issue?). Check "
              "them in Jira, then leave them out with --unentered-only, or add "
              "--create-entered to create them anyway.")
        return EXIT_USAGE

    review = args.review or ctx.settings.review
    if args.edits and review == "none":
        review = "editor"
    if review != "none":
        earlier = load_edits(Path(args.edits).expanduser()) if args.edits else {}
        reviewer = step_review if review == "step" else edit_comments
        reviewed = reviewer(ctx, plan, todo, earlier)
        if reviewed is None:
            return EXIT_OK
        if not reviewed:
            print("Everything was left out, so nothing was changed.")
            return EXIT_OK
        todo = reviewed

    if not confirm(f"Make {len(todo)} change(s) in {ctx.settings.url} as "
                   f"{ctx.me.get('name')}?", args.yes):
        print("Nothing changed.")
        return EXIT_OK
    if not args.yes or review != "none":
        # Reviewing, or the prompt, may have taken a while: don't act on a
        # stale plan.
        stale = changed_since_planned(ctx, plan, selection, todo, args.delete_extra)
        if stale:
            print(f"Stopped: Jira changed on {', '.join(stale)} while waiting for "
                  "'yes'. Nothing was changed; run apply again for a fresh plan.")
            return EXIT_DIFFERENCES

    journal = Journal(ctx.settings.journal_dir, "apply", {
        "url": ctx.settings.url, "user": ctx.me.get("name"),
        "export": str(export.path), "scope": selection.describe(),
    })
    applied: list[tuple[Action, str]] = []
    failed = False
    try:
        for action in todo:
            try:
                worklog_id = execute(ctx, action, journal)
            except JiraError as exc:
                print(f"FAILED on {action.issue_key} {fmt_day(action.day)} "
                      f"({action.kind}): {exc}")
                if exc.status is None:
                    print("Jira may or may not have made that change, and it isn't "
                          "in the journal. Run plan before anything else to see "
                          "where things stand; apply won't repeat a change that "
                          "landed.")
                print("Stopping here; nothing after this was attempted.")
                failed = True
                break
            applied.append((action, worklog_id))
            print(f"  {describe_done(action, worklog_id)}")
    finally:
        journal.close()

    print(f"\n{len(applied)} change(s) made. Journal: {journal.path}")
    if applied:
        print(f"  (to reverse them: {Path(sys.argv[0]).name} undo {journal.path})")
    print("Verifying against Jira...")
    problems = verify_applied(ctx, plan, selection, applied, args.delete_extra)
    for problem in problems:
        print(f"  !! {problem}")
    if problems:
        print(f"Verification FAILED for {len(problems)} change(s).")
        return EXIT_DIFFERENCES
    print(f"Verified: all {len(applied)} change(s) are in Jira as planned.")
    left = len(plan.writes()) - len(applied)
    if left and not failed:
        print(f"{left} further change(s) not made this run.")
    return EXIT_JIRA if failed else EXIT_OK


def _read_journal(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        records = [json.loads(line) for line in
                   path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from None
    except ValueError as exc:
        raise ConfigError(f"{path} is not a journal: {exc}") from None
    if not records or records[0].get("type") != "run":
        raise ConfigError(f"{path} is not a sync_worklogs journal")
    return records[0], [r for r in records[1:] if r.get("type") == "op"]


def _find_twin(ctx: Context, key: str, snapshot: dict[str, Any]) -> str | None:
    """A worklog of yours identical to snapshot, e.g. one already recreated."""
    for data in ctx.client.worklogs(key):
        if is_mine(data.get("author"), ctx.me) and _same_content(data, snapshot):
            return str(data.get("id"))
    return None


def cmd_undo(args: argparse.Namespace) -> int:
    path = Path(args.journal).expanduser()
    header, ops = _read_journal(path)
    ctx = connect(args)
    if header.get("url") != ctx.settings.url:
        raise ConfigError(f"journal was written against {header.get('url')}, "
                          f"but the config points at {ctx.settings.url}")
    if header.get("user") != ctx.me.get("name"):
        raise ConfigError(f"journal was written as {header.get('user')}, but "
                          f"these credentials are {ctx.me.get('name')}")
    print(f"Journal: {path.name}  ({header.get('command')} at {header.get('at')}, "
          f"{len(ops)} change(s))")

    # (what to do, op, current worklog JSON) - newest first, so a worklog
    # changed twice is unwound in the right order.
    steps: list[tuple[str, dict[str, Any], dict[str, Any] | None]] = []
    for op in reversed(ops):
        key, worklog_id = op["issue"], str(op["worklog_id"])
        where = f"{key} {op['day']} worklog {worklog_id}"
        current = None if op["op"] == "delete" else ctx.client.worklog(key, worklog_id)
        if op["op"] == "delete":
            twin = _find_twin(ctx, key, op["before"])
            if twin:
                print(f"  -         {where}: already back as worklog {twin}")
                continue
            steps.append(("recreate", op, None))
            print(f"  recreate  {where}  ({fmt_duration(op['before']['timeSpentSeconds'])})")
        elif current is None:
            print(f"  -         {where}: already gone, nothing to undo")
        elif not args.force and not _same_content(current, op["after"]):
            print(f"  -         {where}: edited since, leaving it (--force to undo anyway)")
        elif op["op"] == "create":
            steps.append(("delete", op, current))
            print(f"  delete    {where}  ({fmt_duration(current.get('timeSpentSeconds') or 0)})")
        else:
            steps.append(("restore", op, current))
            print(f"  restore   {where}  ({', '.join(op['changed'])})")

    if not steps:
        print("Nothing to undo.")
        return EXIT_OK
    if not confirm(f"Undo {len(steps)} change(s)?", args.yes):
        print("Nothing changed.")
        return EXIT_OK

    adjust = ctx.settings.adjust_estimate
    journal = Journal(ctx.settings.journal_dir, "undo", {
        "url": ctx.settings.url, "user": ctx.me.get("name"), "undoes": str(path),
    })
    try:
        for step, op, current in steps:
            key, worklog_id = op["issue"], str(op["worklog_id"])
            day = date.fromisoformat(op["day"])
            try:
                if step == "delete":
                    assert current is not None
                    ctx.client.delete_worklog(key, worklog_id, adjust)
                    journal.op("delete", key, worklog_id, day, before=_snapshot(current))
                    print(f"  deleted worklog {worklog_id} on {key}")
                elif step == "restore":
                    assert current is not None
                    body = _update_body({f: op["before"][f] for f in op["changed"]},
                                        op["before"])
                    updated = ctx.client.update_worklog(key, worklog_id, body, adjust)
                    journal.op("update", key, worklog_id, day, changed=op["changed"],
                               before=_snapshot(current), after=_snapshot(updated))
                    print(f"  restored worklog {worklog_id} on {key}")
                else:
                    created = ctx.client.add_worklog(key, dict(op["before"]), adjust)
                    new_id = str(created.get("id"))
                    journal.op("create", key, new_id, day, after=_snapshot(created))
                    print(f"  recreated worklog on {key} as {new_id}")
            except JiraError as exc:
                print(f"FAILED on {key} worklog {worklog_id}: {exc}")
                print(f"Stopping. What was done is in {journal.path}")
                return EXIT_JIRA
    finally:
        journal.close()
    print(f"Undone. Journal of the undo: {journal.path}")
    return EXIT_OK


# --- Entry point ------------------------------------------------------------


def _iso_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not YYYY-MM-DD") from None


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"{text!r} is not a positive whole number")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sync_worklogs.py",
        description="Make your Jira worklogs match a timesheets allocations export.",
        epilog="See README.md for setup and a staged first run.",
    )
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", metavar="PATH",
                        help=f"config file (default ${CONFIG_ENV}, else "
                             "~/.jira-sync/config.ini)")
    common.add_argument("-v", "--verbose", action="store_true",
                        help="log each HTTP request to stderr")
    common.add_argument("--log-file", metavar="PATH",
                        help="also write a full debug log here")

    scope = argparse.ArgumentParser(add_help=False)
    scope.add_argument("export", help="JSON export from timesheets (R, format JSON)")
    scope.add_argument("--issue", action="append", default=[], metavar="ID",
                       help="only this issue: a timesheets ticket ID or a Jira "
                            "key (repeatable)")
    scope.add_argument("--date", action="append", default=[], type=_iso_date,
                       metavar="YYYY-MM-DD", help="only this day (repeatable)")
    scope.add_argument("--from", dest="date_from", type=_iso_date,
                       metavar="YYYY-MM-DD", help="only from this day")
    scope.add_argument("--to", dest="date_to", type=_iso_date,
                       metavar="YYYY-MM-DD", help="only up to this day")
    scope.add_argument("--unentered-only", action="store_true",
                       help="leave alone allocations timesheets already marks "
                            "as entered on client")
    scope.add_argument("--delete-extra", action="store_true",
                       help="delete your worklogs that have no allocation "
                            "(otherwise they are only reported)")
    scope.add_argument("--changes-only", action="store_true",
                       help="hide lines already in sync")
    scope.add_argument("--show-comments", action="store_true",
                       help="print new comments in full, and comment diffs")

    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = sub.add_parser("check", parents=[common],
                       help="test the connection, credentials and settings")
    p.add_argument("--issue", metavar="ID",
                   help="also read this issue and list your latest worklogs on it")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("plan", parents=[common, scope],
                       help="show what apply would do; changes nothing")
    p.add_argument("--limit", type=_positive_int, metavar="N",
                   help="mark which changes apply --limit N would make")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("apply", parents=[common, scope],
                       help="make the changes, then verify them")
    p.add_argument("--limit", type=_positive_int, metavar="N",
                   help="make at most N changes")
    p.add_argument("--yes", action="store_true", help="don't ask first")
    p.add_argument("--skip-missing", action="store_true",
                   help="go ahead even if some issues could not be read")
    review = p.add_mutually_exclusive_group()
    review.add_argument("--step", dest="review", action="store_const", const="step",
                        help="walk through the changes one at a time: send, edit "
                             "the comment in $EDITOR (vim), or skip each")
    review.add_argument("--edit", dest="review", action="store_const",
                        const="editor",
                        help="edit all the comments in one $EDITOR (vim) file first")
    review.add_argument("--review", choices=REVIEW_MODES,
                        help="none, step or editor; overrides review in the config")
    p.add_argument("--edits", metavar="FILE",
                   help="start from comments edited in an earlier run (implies "
                        "--edit unless --step is given)")
    p.add_argument("--create-entered", action="store_true",
                   help="create worklogs even for allocations timesheets marks "
                        "as entered on client")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("verify", parents=[common, scope],
                       help="exit 0 if Jira matches the export, 1 if not")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("undo", parents=[common],
                       help="reverse the changes recorded in a journal")
    p.add_argument("journal", help="journal file printed by apply")
    p.add_argument("--yes", action="store_true", help="don't ask first")
    p.add_argument("--force", action="store_true",
                   help="undo even worklogs edited since")
    p.set_defaults(func=cmd_undo)
    return parser


def setup_logging(verbose: bool, log_file: str | None) -> None:
    log.setLevel(logging.DEBUG)
    for handler in list(log.handlers):
        log.removeHandler(handler)
        handler.close()
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    log.addHandler(console)
    if log_file:
        to_file = logging.FileHandler(Path(log_file).expanduser(), encoding="utf-8")
        to_file.setLevel(logging.DEBUG)
        to_file.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(to_file)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose, args.log_file)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except JiraError as exc:
        print(f"Jira error: {exc}", file=sys.stderr)
        return EXIT_JIRA
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
