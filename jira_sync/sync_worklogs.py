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
import string
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time as dtime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Callable

try:
    import requests
except ImportError:  # pragma: no cover - only on a machine not yet set up
    sys.exit("sync_worklogs needs the 'requests' package: "
             "pip install -r requirements.txt")

__version__ = "0.4.1"

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

# Plan action kinds. Only the first four write to Jira.
CREATE = "create"
UPDATE = "update"
DELETE = "delete"
MOVE = "move"
OK = "ok"
CONFLICT = "conflict"
EXTRA = "extra"
UNEXPECTED = "unexpected"
MISSING = "missing"
SKIP = "skip"
WRITES = (CREATE, UPDATE, DELETE, MOVE)

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
    billing_project: str = "PITLAI"
    billing_initials: str = ""
    billing_summary: str = "{initials}-{deliverable}-{month}-{year}"
    billing_issue_type: str = "Task"
    billing_jql: str = ""
    points_field: str = "customfield_10002"
    link_description: str = "is delivered by"
    close_transition: str = ""
    resolution: str = "Done"
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
    for name in ("jira", "sync", "billing", "project_map", "issue_map"):
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

    billing = parser["billing"]
    billing_summary = billing.get("summary_format", Settings.billing_summary).strip()
    _check_placeholders("[billing] summary_format", billing_summary,
                        require_deliverable=True)
    billing_jql = billing.get("jql", "").strip()
    _check_placeholders("[billing] jql", billing_jql, require_deliverable=False)

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
        billing_project=billing.get("project", "PITLAI").strip().upper().rstrip("-"),
        billing_initials=billing.get("initials", "").strip(),
        billing_summary=billing_summary,
        billing_issue_type=billing.get("issue_type", "Task").strip(),
        billing_jql=billing_jql,
        points_field=billing.get("points_field", "customfield_10002").strip(),
        link_description=billing.get("link_description", "is delivered by").strip(),
        close_transition=billing.get("close_transition", "").strip(),
        resolution=billing.get("resolution", "Done").strip(),
        journal_dir=Path(sync.get("journal_dir", "~/.jira-sync/journals")
                         .strip()).expanduser(),
    )


# Placeholders a billing ticket's summary_format may use.
SUMMARY_FIELDS = ("initials", "deliverable", "month", "mon", "mm", "year")


def _check_placeholders(what: str, text: str, *, require_deliverable: bool) -> None:
    try:
        names = {name for _, name, _, _ in string.Formatter().parse(text)
                 if name is not None}
    except ValueError as exc:
        raise ConfigError(f"{what}: {exc}") from None
    allowed = set(SUMMARY_FIELDS) - (set() if require_deliverable else {"deliverable"})
    unknown = names - allowed
    if unknown:
        raise ConfigError(f"{what}: unknown placeholder {{{sorted(unknown)[0]}}}; "
                          f"use {', '.join('{' + n + '}' for n in sorted(allowed))}")
    if require_deliverable and "deliverable" not in names:
        raise ConfigError(f"{what} must contain {{deliverable}}")


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


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from None
    except ValueError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from None


def read_export(path: Path) -> Export | BillExport:
    """An allocations export or a bill export, whichever the file is."""
    data = _read_json(path)
    if isinstance(data, dict) and data.get("schema") == BILL_SCHEMA:
        return _parse_bill(path, data)
    return load_export(path, data)


def load_export(path: Path, data: Any = None) -> Export:
    if data is None:
        data = _read_json(path)
    if not isinstance(data, dict) or data.get("schema") != EXPORT_SCHEMA:
        raise ConfigError(f"{path} is not a timesheets export (R in the "
                          "allocations view, or J in the Billing view)")
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

    def issue(self, key: str,
              fields: str = "summary,status,project") -> dict[str, Any] | None:
        return self._call("GET", f"issue/{key}", params={"fields": fields},
                          missing_ok=True)

    def set_field(self, key: str, field_id: str, value: Any) -> None:
        self._call("PUT", f"issue/{key}", body={"fields": {field_id: value}})

    def fields(self) -> list[dict[str, Any]]:
        return self._call("GET", "field") or []

    def link_types(self) -> list[dict[str, Any]]:
        return (self._call("GET", "issueLinkType") or {}).get("issueLinkTypes", [])

    def create_link(self, type_name: str, inward_key: str, outward_key: str) -> None:
        """Link two issues. Jira's documented reading: inward [inward] outward."""
        self._call("POST", "issueLink", body={
            "type": {"name": type_name},
            "inwardIssue": {"key": inward_key},
            "outwardIssue": {"key": outward_key},
        })

    def transitions(self, key: str) -> list[dict[str, Any]]:
        """Transitions open to you from the issue's status, with their fields."""
        return (self._call("GET", f"issue/{key}/transitions",
                           params={"expand": "transitions.fields"}) or {}
                ).get("transitions", [])

    def transition(self, key: str, body: dict[str, Any]) -> None:
        self._call("POST", f"issue/{key}/transitions", body=body)

    def link(self, link_id: str) -> dict[str, Any] | None:
        return self._call("GET", f"issueLink/{link_id}", missing_ok=True)

    def delete_link(self, link_id: str) -> None:
        self._call("DELETE", f"issueLink/{link_id}")

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

    def search(self, jql: str, fields: str = "summary") -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        while True:
            page = self._call("GET", "search", params={
                "jql": jql, "fields": fields,
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


def pair_moves(actions: list[Action], *, update_comments: bool) -> list[Action]:
    """Turn a stray worklog plus a new one elsewhere into a single move.

    Moving an allocation to another ticket in timesheets looks like this from
    Jira: a worklog to create on the new issue, and the old one left with no
    allocation behind it, on the same day and the same length. Such pairs
    become a move: create the new worklog, then delete the old. Only pairs
    with no rival on either side count; where a day has several candidates of
    the same length, matching comments must settle it, or they're left alone.
    """
    creates = [a for a in actions if a.kind == CREATE and a.allocation]
    strays = [a for a in actions if a.kind in (EXTRA, DELETE, UNEXPECTED) and a.worklogs]

    def same_slot(stray: Action, create: Action) -> bool:
        wl, alloc = stray.worklogs[0], create.allocation
        return (alloc is not None and wl.day == create.day and wl.seconds == alloc.seconds
                and wl.issue_key != create.issue_key)

    def same_comment(stray: Action, create: Action) -> bool:
        alloc = create.allocation
        return (alloc is not None
                and norm_comment(stray.worklogs[0].comment) == norm_comment(alloc.comment))

    def unrivalled(pairs: list[tuple[Action, Action]]) -> list[tuple[Action, Action]]:
        strays_seen = [id(s) for s, _ in pairs]
        creates_seen = [id(c) for _, c in pairs]
        return [(s, c) for s, c in pairs
                if strays_seen.count(id(s)) == 1 and creates_seen.count(id(c)) == 1]

    candidates = [(s, c) for s in strays for c in creates if same_slot(s, c)]
    chosen = unrivalled(candidates)
    used = {id(x) for pair in chosen for x in pair}
    chosen += unrivalled([(s, c) for s, c in candidates if same_comment(s, c)
                          and id(s) not in used and id(c) not in used])
    if not chosen:
        return actions

    moves: dict[int, Action] = {}
    gone: set[int] = set()
    for stray, create in chosen:
        wl, alloc = stray.worklogs[0], create.allocation
        assert alloc is not None
        note = f"from {wl.issue_key}"
        if not same_comment(stray, create):
            note += ", comment differs"
        if not update_comments:
            alloc.comment = wl.comment  # the comment is yours: it moves too
        moves[id(create)] = Action(MOVE, create.issue_key, create.day,
                                   allocation=alloc, worklogs=[wl], note=note)
        gone.add(id(stray))
    return [moves.get(id(a), a) for a in actions if id(a) not in gone]


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

    # Your worklogs in the period on issues outside this run: reported, and
    # the other half of any move. With --issue they're only wanted for moves,
    # but they're looked for all the same, so a move can't become a copy.
    known = set(keys) | {k for k in (_prefixed_key(row.ticket_id, ctx.settings)
                                     for row, _ in skipped) if k}
    everywhere = replace(selection, issues=frozenset())
    for state in find_unexpected(ctx, everywhere, known):
        states[state.key] = state
        if state.error:
            log.warning("%s: %s", state.key, state.error)
        actions += [Action(UNEXPECTED, state.key, wl.day, worklogs=[wl])
                    for wl in state.worklogs]
    actions = pair_moves(actions, update_comments=ctx.settings.update_comments)
    if selection.issues:
        actions = [a for a in actions
                   if a.kind != UNEXPECTED or a.issue_key in selection.issues]

    if not selection.issues:
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
    elif action.kind == MOVE and alloc and wl:
        text = (f"{fmt_duration(alloc.seconds):<8} worklog {wl.id} {action.note}, "
                f'"{preview(alloc.comment, 40)}"')
        return text
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
        f"{plan.count(MOVE)} to move, {plan.count(DELETE)} to delete; "
        f"{plan.count(OK)} in sync; "
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
        self.record(op, issue=key, worklog_id=worklog_id, day=day.isoformat(),
                    **detail)

    def record(self, op: str, **detail: Any) -> None:
        self.write({"type": "op", "op": op, **detail})

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
    if action.kind == MOVE:
        alloc = action.allocation
        assert alloc is not None
        # Create first, then delete: the time is never missing from Jira
        body: dict[str, Any] = {"started": wl.started, "timeSpentSeconds": alloc.seconds,
                                "comment": alloc.comment}
        if wl.visibility:
            body["visibility"] = wl.visibility
        created = client.add_worklog(key, body, adjust)
        worklog_id = str(created.get("id"))
        journal.op("create", key, worklog_id, day, after=_snapshot(created))
        try:
            client.delete_worklog(wl.issue_key, wl.id, adjust)
        except JiraError as exc:
            raise JiraError(
                f"made worklog {worklog_id} on {key}, but couldn't delete the old "
                f"one ({wl.id} on {wl.issue_key}) - delete that by hand, or undo "
                f"this run: {exc}", exc.status) from None
        journal.op("delete", wl.issue_key, wl.id, wl.day, before=wl.snapshot())
        return worklog_id
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
    verb = {CREATE: "created", UPDATE: "updated", DELETE: "deleted",
            MOVE: "moved"}[action.kind]
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
    def expected(action: Action) -> tuple[Any, ...]:
        # Re-checked one issue at a time, a move's new half is a create
        if action.kind == MOVE:
            return (CREATE, action.issue_key, action.day, (), ())
        return _signature(action)

    stale: list[str] = []
    for key in sorted({a.issue_key for a in todo}, key=natural_key):
        state, now = _recheck(ctx, plan, selection, key, delete_extra)
        current = {_signature(a) for a in now}
        if state.error or any(expected(a) not in current
                              for a in todo if a.issue_key == key):
            stale.append(key)
    for action in todo:
        if action.kind == MOVE:
            old = action.worklogs[0]
            now_there = ctx.client.worklog(old.issue_key, old.id)
            if now_there is None or not _same_content(now_there, old.snapshot()):
                stale.append(old.issue_key)
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
            elif action.kind in (CREATE, MOVE) and worklog_id not in present:
                problems.append(f"{where}: new worklog {worklog_id} not found")
    for action, _ in applied:
        if action.kind == MOVE:
            old = action.worklogs[0]
            if ctx.client.worklog(old.issue_key, old.id) is not None:
                problems.append(f"{old.issue_key} {fmt_day(old.day)}: moved worklog "
                                f"{old.id} is still there")
    return problems


def confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise ConfigError("not running interactively - add --yes to go ahead")
    return input(f"{question} Type 'yes' to go ahead: ").strip().lower() == "yes"


# --- Billing ----------------------------------------------------------------
#
# A bill export (Billing view, J) holds a finalised month's points per
# deliverable and the tickets behind each. Each month someone else creates a
# ticket per deliverable in the billing project, named by convention, e.g.
# WB-WP2d-D1-October-2026. For the bill's month, every one of your tickets
# gets the bill's story points (0 if its deliverable isn't on the bill, so
# nothing unbilled is billable) and an "is delivered by" link to each work
# ticket behind its points.

BILL_SCHEMA = "timesheets.bill"
BILL_VERSION = 1
MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December")

# Bill plan kinds. POINTS, LINK and CLOSE write to Jira; CLOSED marks a
# closed ticket that's wrong, which only a person should reopen.
POINTS = "points"
LINK = "link"
CLOSE = "close"
LINKED = "linked"
EXTRA_LINK = "extra-link"
REVERSED = "reversed"
CLOSED = "closed"
BILL_WRITES = (POINTS, LINK, CLOSE)
BILL_PROBLEMS = (MISSING, CLOSED)


@dataclass
class BillLine:
    deliverable: str | None
    title: str
    points: int
    tickets: list[tuple[str, str]]  # (timesheets ticket ID, description)


@dataclass
class BillExport:
    path: Path
    year: int                       # month the billing tickets are named for
    month: int
    billed: tuple[int, int]
    generated_at: str
    lines: list[BillLine]

    @property
    def points(self) -> int:
        return sum(line.points for line in self.lines)

    @property
    def month_name(self) -> str:
        return f"{MONTH_NAMES[self.month - 1]} {self.year}"


def _parse_bill(path: Path, data: dict[str, Any]) -> BillExport:
    if data.get("version") != BILL_VERSION:
        raise ConfigError(f"{path} is bill export version {data.get('version')}; "
                          f"sync_worklogs {__version__} reads version {BILL_VERSION}")
    try:
        billed = (int(data["billed"]["year"]), int(data["billed"]["month"]))
        delivered = data.get("delivered")
        # Named for the month the work was delivered, as the invoice is
        named = ((int(delivered["year"]), int(delivered["month"]))
                 if delivered else billed)
        lines = [
            BillLine(
                deliverable=str(line["deliverable_id"]).strip()
                if line.get("deliverable_id") else None,
                title=str(line.get("deliverable_title") or ""),
                points=int(line["points"]),
                tickets=[(str(t["ticket_id"]).strip(), str(t.get("description") or ""))
                         for t in line.get("tickets", [])],
            )
            for line in data["lines"]
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"{path}: malformed bill export ({exc!r})") from None
    return BillExport(path, named[0], named[1], billed,
                      str(data.get("generated_at", "")), lines)


def _summary_values(settings: Settings, year: int, month: int,
                    deliverable: str = "") -> dict[str, str]:
    name = MONTH_NAMES[month - 1]
    return {"initials": settings.billing_initials, "deliverable": deliverable,
            "month": name, "mon": name[:3], "mm": f"{month:02d}", "year": str(year)}


def billing_summary(settings: Settings, deliverable: str, year: int, month: int) -> str:
    """The summary your billing ticket for a deliverable and month should have."""
    return settings.billing_summary.format(
        **_summary_values(settings, year, month, deliverable))


def billing_pattern(settings: Settings, year: int, month: int) -> re.Pattern[str]:
    """Matches the month's billing summaries, capturing the deliverable."""
    marker = "\x00"
    before, _, after = billing_summary(settings, marker, year, month).partition(marker)
    return re.compile(re.escape(before) + "(?P<deliverable>.+?)" + re.escape(after),
                      re.IGNORECASE)


def _jql_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def billing_jql(settings: Settings, year: int, month: int) -> str:
    """JQL for the candidates; billing_pattern then picks out the month's.

    No text search: Jira indexes a hyphenated summary with digits in it,
    like WB-WP5-D4-September-2026, as a single word, so searching for its
    parts finds nothing. Paging through the project's tasks is slower but
    can't miss.
    """
    if settings.billing_jql:
        return settings.billing_jql.format(**_summary_values(settings, year, month))
    clauses = [f"project = {_jql_quote(settings.billing_project)}"]
    if settings.billing_issue_type:
        clauses.append(f"issuetype = {_jql_quote(settings.billing_issue_type)}")
    return " AND ".join(clauses) + " ORDER BY created DESC"


def find_billing_tickets(ctx: Context, year: int, month: int
                         ) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    """Your billing tickets for the month: deliverable -> [(as named, issue)].

    Keyed by the deliverable in lower case; a list, because two tickets with
    the same name is a mistake to report, not to guess around.
    """
    s = ctx.settings
    fields = f"summary,status,issuetype,issuelinks,{s.points_field}"
    found = ctx.client.search(billing_jql(s, year, month), fields=fields)
    pattern = billing_pattern(s, year, month)
    tickets: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for issue in found:
        summary = str((issue.get("fields") or {}).get("summary") or "").strip()
        match = pattern.fullmatch(summary)
        if match:
            named = match.group("deliverable")
            tickets.setdefault(named.casefold(), []).append((named, issue))
    return tickets


@dataclass
class LinkType:
    name: str
    billing_inward: bool  # billing ticket goes as inwardIssue to read right


def find_link_type(client: JiraClient, description: str) -> LinkType:
    """The link type with a side that reads description ("is delivered by")."""
    want = description.casefold()
    types = client.link_types()
    for kind in types:
        if str(kind.get("inward") or "").casefold() == want:
            return LinkType(str(kind["name"]), True)
        if str(kind.get("outward") or "").casefold() == want:
            return LinkType(str(kind["name"]), False)
    known = "; ".join(f"{k.get('name')} ({k.get('outward')} / {k.get('inward')})"
                      for k in types)
    raise ConfigError(f"no issue link type reads {description!r} - set "
                      f"link_description in [billing]. Jira has: {known}")


def read_links(issue: dict[str, Any], description: str
               ) -> tuple[dict[str, str], dict[str, str]]:
    """An issue's links as {other key: link id}, in two sets.

    The first holds links that read "issue <description> other"; the second
    those of the same type that read the other way round. From an issue's
    side, a link listing an outwardIssue reads with the type's outward text
    and one listing an inwardIssue with its inward text.
    """
    want = description.casefold()
    forward: dict[str, str] = {}
    backward: dict[str, str] = {}
    for link in (issue.get("fields") or {}).get("issuelinks") or []:
        kind = link.get("type") or {}
        if "outwardIssue" in link:
            other, reads, opposite = link["outwardIssue"], kind.get("outward"), kind.get("inward")
        elif "inwardIssue" in link:
            other, reads, opposite = link["inwardIssue"], kind.get("inward"), kind.get("outward")
        else:
            continue
        key = str(other.get("key", "")).upper()
        if str(reads or "").casefold() == want:
            forward[key] = str(link.get("id"))
        elif str(opposite or "").casefold() == want:
            backward[key] = str(link.get("id"))
    return forward, backward


@dataclass
class BillAction:
    """One line of a bill plan: a ticket's points, or one of its links."""

    kind: str
    deliverable: str
    issue: str | None = None          # the billing ticket
    summary: str = ""
    points_from: Any = None
    points_to: int | None = None
    work: str | None = None           # the work ticket a link is to
    work_title: str = ""
    note: str = ""
    closing: bool = False             # part of the closing phase


@dataclass
class BillPlan:
    bill: BillExport
    actions: list[BillAction]
    link_type: LinkType

    def writes(self) -> list[BillAction]:
        return [a for a in self.actions if a.kind in BILL_WRITES]

    def count(self, kind: str) -> int:
        return sum(1 for a in self.actions if a.kind == kind)


def _same_points(current: Any, wanted: float) -> bool:
    try:
        return current is not None and float(current) == float(wanted)
    except (TypeError, ValueError):
        return False


def fmt_points(value: Any) -> str:
    if value is None:
        return "empty"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return repr(value)
    return f"{number:g}"


def _is_done(fields: dict[str, Any]) -> bool:
    status = fields.get("status") or {}
    return (status.get("statusCategory") or {}).get("key") == "done"


def close_transition(ctx: Context, key: str) -> tuple[dict[str, Any] | None, str]:
    """The transition that closes the ticket and the body to send, or why not.

    close_transition in [billing] names it; otherwise it's the one way to a
    status in Jira's "done" category, preferring one to a status called
    Closed if there are several.
    """
    s = ctx.settings
    transitions = ctx.client.transitions(key)
    names = ", ".join(repr(str(t.get("name"))) for t in transitions) or "none"
    if s.close_transition:
        chosen = [t for t in transitions
                  if str(t.get("name", "")).casefold() == s.close_transition.casefold()]
        if not chosen:
            return None, f"no transition named {s.close_transition!r} from here ({names})"
    else:
        chosen = [t for t in transitions
                  if ((t.get("to") or {}).get("statusCategory") or {}).get("key") == "done"]
        if len(chosen) > 1:
            chosen = [t for t in chosen if str((t.get("to") or {}).get("name", "")
                                               ).casefold() == "closed"] or chosen
        if len(chosen) != 1:
            why = "no way to a done status" if not chosen else "several ways to close it"
            return None, f"{why} from here ({names}) - set close_transition in [billing]"
    transition = chosen[0]
    fields = transition.get("fields") or {}
    # The close screen may insist on more than a resolution (Fix Version/s,
    # say); better to say so in the plan than have Jira refuse mid-run.
    unfilled = sorted(str(spec.get("name") or field_id)
                      for field_id, spec in fields.items()
                      if field_id != "resolution" and spec.get("required")
                      and not spec.get("hasDefaultValue"))
    if unfilled:
        return None, (f"closing via {transition.get('name')!r} asks for "
                      f"{', '.join(unfilled)}, which the tool doesn't fill in")
    body: dict[str, Any] = {"transition": {"id": str(transition.get("id"))}}
    resolution = fields.get("resolution")
    if resolution:
        allowed = [str(v.get("name")) for v in resolution.get("allowedValues") or []]
        if allowed and s.resolution.casefold() not in (a.casefold() for a in allowed):
            return None, (f"resolution {s.resolution!r} isn't allowed here ("
                          f"{', '.join(allowed)}) - set resolution in [billing]")
        body["fields"] = {"resolution": {"name": s.resolution}}
    transition["body"] = body
    return transition, ""


def build_bill_plan(ctx: Context, bill: BillExport, *, closing: bool = True) -> BillPlan:
    """What would make your tickets for the bill's month match it.

    Points and links come first, ticket by ticket; closing comes last, so
    that apply can check each ticket before closing it.
    """
    s = ctx.settings
    if not s.billing_initials:
        raise ConfigError("set initials in [billing] - they pick out your tickets")
    link_type = find_link_type(ctx.client, s.link_description)
    found = find_billing_tickets(ctx, bill.year, bill.month)

    known: dict[str, bool] = {}

    def work_exists(key: str) -> bool:
        if key not in known:
            try:
                issue = ctx.client.issue(key, fields="summary")
            except JiraError as exc:
                if exc.status is None:
                    raise
                issue = None
            known[key] = bool(issue) and str(issue.get("key", key)).upper() == key
        return known[key]

    closes: list[BillAction] = []

    def ticket_actions(deliverable: str, issue: dict[str, Any], points: int,
                       tickets: list[tuple[str, str]], note: str) -> list[BillAction]:
        key = str(issue["key"])
        fields = issue.get("fields") or {}
        summary = str(fields.get("summary") or "")
        current = fields.get(s.points_field)
        status = str((fields.get("status") or {}).get("name") or "?")
        done = _is_done(fields)
        if _same_points(current, points):
            actions = [BillAction(OK, deliverable, key, summary, current, points, note=note)]
        elif done:
            actions = [BillAction(CLOSED, deliverable, key, summary, current, points,
                                  note=f"{status}, but its story points are "
                                       f"{fmt_points(current)}, not {points} - "
                                       "reopen it by hand, then re-run")]
        else:
            actions = [BillAction(POINTS, deliverable, key, summary, current, points,
                                  note=note)]
        if not done and closing:
            transition, problem = close_transition(ctx, key)
            if transition is None:
                closes.append(BillAction(MISSING, deliverable, key, summary,
                                         note=f"can't close it: {problem}",
                                         closing=True))
            else:
                target = (transition.get("to") or {}).get("name", "?")
                closes.append(BillAction(CLOSE, deliverable, key, summary,
                                         note=f"{status} -> {target} via "
                                              f"{transition.get('name')!r}",
                                         closing=True))
        forward, backward = read_links(issue, s.link_description)
        wanted: set[str] = set()
        for ticket_id, title in tickets:
            work = issue_key_for(ticket_id, s)
            base: dict[str, Any] = {"deliverable": deliverable, "issue": key,
                                    "summary": summary, "work_title": title}
            if work is None:
                actions.append(BillAction(MISSING, work=ticket_id, note="not a Jira key "
                                          "- map it in [issue_map]", **base))
                continue
            if work in wanted:
                continue
            wanted.add(work)
            if work in forward:
                actions.append(BillAction(LINKED, work=work, **base))
            elif work in backward:
                actions.append(BillAction(REVERSED, work=work, note="linked the "
                                          "wrong way round - fix it by hand", **base))
            elif not work_exists(work):
                actions.append(BillAction(MISSING, work=work, note="work ticket not "
                                          "found, or not visible to you", **base))
            elif done:
                actions.append(BillAction(CLOSED, work=work, note=f"{status}, but not "
                                          "linked to this - reopen it by hand, then "
                                          "re-run", **base))
            else:
                actions.append(BillAction(LINK, work=work, **base))
        actions += [BillAction(EXTRA_LINK, deliverable, key, summary, work=other,
                               note="not on this bill line, left alone")
                    for other in sorted(set(forward) - wanted, key=natural_key)]
        return actions

    actions: list[BillAction] = []
    on_bill: set[str] = set()
    for line in bill.lines:
        if not line.deliverable:
            actions.append(BillAction(
                MISSING, "(none)", points_to=line.points,
                note=f"{len(line.tickets)} ticket(s) have no deliverable, so no "
                     "ticket to bill them on - fix in timesheets and re-finalise"))
            continue
        on_bill.add(line.deliverable.casefold())
        expected = billing_summary(s, line.deliverable, bill.year, bill.month)
        matches = found.get(line.deliverable.casefold(), [])
        if len(matches) != 1:
            problem = (f"no {s.billing_project} ticket named {expected}" if not matches
                       else f"{len(matches)} tickets named {expected} ("
                            + ", ".join(str(i["key"]) for _, i in matches)
                            + ") - can't tell which to bill")
            actions.append(BillAction(MISSING, line.deliverable, summary=expected,
                                      points_to=line.points, note=problem))
            continue
        actions += ticket_actions(line.deliverable, matches[0][1], line.points,
                                  line.tickets, "")

    for folded, matches in sorted(found.items()):
        if folded in on_bill:
            continue
        for named, issue in matches:
            actions += ticket_actions(named, issue, 0, [], "not on this bill")
    return BillPlan(bill, actions + closes, link_type)


def describe_bill_action(action: BillAction, description: str) -> str:
    note = f"  ({action.note})" if action.note else ""
    if action.closing:
        return f"{action.issue}  {action.summary}  ({action.note})"
    if action.kind == POINTS:
        return (f"story points {fmt_points(action.points_from)} -> "
                f"{fmt_points(action.points_to)}{note}")
    if action.kind == OK:
        return f"story points {fmt_points(action.points_to)}{note}"
    if action.kind in (LINK, LINKED):
        return f"{description} {action.work}  {preview(action.work_title, 44)}".rstrip()
    if action.kind == EXTRA_LINK:
        return f"{description} {action.work}{note}"
    if action.kind == REVERSED:
        return f"{action.work}: {action.note}"
    if action.kind == CLOSED and not action.work:
        return action.note
    if action.work:
        return f"{action.work}: {action.note}"
    return action.note + (f"  ({fmt_points(action.points_to)} points)"
                          if action.points_to is not None else "")


def render_bill_plan(plan: BillPlan, description: str, *, limit: int | None,
                     changes_only: bool) -> list[str]:
    lines: list[str] = []
    groups: dict[str, list[BillAction]] = {}
    for action in plan.actions:
        group = "\x00closing" if action.closing else action.issue or action.deliverable
        groups.setdefault(group, []).append(action)
    number = 0
    for group, actions in groups.items():
        if changes_only and all(a.kind in (OK, LINKED) for a in actions):
            continue
        first = actions[0]
        if group == "\x00closing":
            lines.append("Then closing, for each ticket that checks out:")
        elif first.issue:
            lines.append(f"{first.issue}  {first.summary}")
        else:
            lines.append(f"{first.deliverable}  {first.summary}".rstrip())
        for action in actions:
            if changes_only and action.kind in (OK, LINKED):
                continue
            label = suffix = ""
            if action.kind in BILL_WRITES:
                number += 1
                label = f"[{number}]"
                if limit is not None and number > limit:
                    suffix = "  (beyond --limit)"
            lines.append(f"  {label:>5} {action.kind:<10} "
                         f"{describe_bill_action(action, description)}{suffix}")
        lines.append("")
    lines.append(
        f"Summary: {plan.count(POINTS)} story points to set, {plan.count(LINK)} "
        f"links to add, {plan.count(CLOSE)} to close; {plan.count(OK)} points and "
        f"{plan.count(LINKED)} links already right; {plan.count(REVERSED)} reversed, "
        f"{plan.count(EXTRA_LINK)} extra links; {plan.count(MISSING)} missing, "
        f"{plan.count(CLOSED)} closed but wrong"
    )
    return lines


def print_bill_header(ctx: Context, bill: BillExport, plan_link: LinkType | None) -> None:
    s = ctx.settings
    billed = f"{bill.billed[0]}-{bill.billed[1]:02d}"
    tickets = sum(len(line.tickets) for line in bill.lines)
    print(f"Bill:     {bill.path.name}  (billed {billed}; {len(bill.lines)} "
          f"deliverable(s), {bill.points} points, {tickets} ticket(s); generated "
          f"{bill.generated_at})")
    print(f"Jira:     {s.url} as {ctx.me.get('displayName')} ({ctx.me.get('name')})")
    example = billing_summary(s, "<deliverable>", bill.year, bill.month)
    print(f"Billing:  {s.billing_project} tickets named {example}; points in "
          f"{s.points_field}; links read {s.link_description!r}"
          + (f" (type {plan_link.name})" if plan_link else ""))
    print(f"Tool:     sync_worklogs {__version__}")
    print()


class LinkMaker:
    """Creates "<billing ticket> is delivered by <work ticket>" links.

    Jira 8 can create a link the other way round from how the REST docs
    describe it (when a link type's name matches its outward text), so every
    new link is read back. If it came out reversed, it's removed and made
    again the other way, and later links are made that way from the start.
    """

    def __init__(self, ctx: Context, link_type: LinkType) -> None:
        self.ctx = ctx
        self.link_type = link_type
        self.flipped = False

    def create(self, issue_key: str, work_key: str) -> str:
        client = self.ctx.client
        description = self.ctx.settings.link_description
        for attempt in (1, 2):
            billing_inward = self.link_type.billing_inward != self.flipped
            inward, outward = ((issue_key, work_key) if billing_inward
                               else (work_key, issue_key))
            client.create_link(self.link_type.name, inward, outward)
            issue = client.issue(issue_key, fields="issuelinks") or {}
            forward, backward = read_links(issue, description)
            if work_key in forward:
                return forward[work_key]
            if work_key in backward and attempt == 1:
                client.delete_link(backward[work_key])
                self.flipped = not self.flipped
                log.warning("Jira made the %s link the other way round; removed "
                            "it and making links the other way from now on",
                            self.link_type.name)
                continue
            break
        raise JiraError(f"linked {issue_key} and {work_key}, but no link reading "
                        f"{description!r} can be found afterwards", status=200)


def execute_bill(ctx: Context, action: BillAction, journal: Journal,
                 links: LinkMaker) -> str:
    """Make one planned bill change in Jira and journal it."""
    assert action.issue is not None
    if action.kind == POINTS:
        assert action.points_to is not None
        value = float(action.points_to)
        ctx.client.set_field(action.issue, ctx.settings.points_field, value)
        journal.record("points", issue=action.issue, field=ctx.settings.points_field,
                       before=action.points_from, after=value)
        return action.issue
    if action.kind == LINK:
        assert action.work is not None
        link_id = links.create(action.issue, action.work)
        journal.record("link", issue=action.issue, other=action.work, link_id=link_id)
        return link_id
    if action.kind == CLOSE:
        return close_ticket(ctx, action.issue, journal)
    raise ValueError(f"not a write: {action.kind}")


def _status_name(ctx: Context, key: str) -> str:
    issue = ctx.client.issue(key, fields="status") or {}
    return str(((issue.get("fields") or {}).get("status") or {}).get("name") or "")


def close_ticket(ctx: Context, key: str, journal: Journal) -> str:
    before = _status_name(ctx, key)
    transition, problem = close_transition(ctx, key)
    if transition is None:
        raise JiraError(f"can't close {key}: {problem}", status=200)
    ctx.client.transition(key, transition["body"])
    journal.record("close", issue=key, status_before=before)
    return key


def _bill_signature(action: BillAction) -> tuple[Any, ...]:
    return (action.kind, action.issue, action.work, fmt_points(action.points_from),
            action.points_to)


def verify_bill_applied(ctx: Context, bill: BillExport,
                        applied: list[BillAction]) -> list[str]:
    """Re-read the month's billing tickets; return what still disagrees."""
    after = build_bill_plan(ctx, bill)
    settled_points = {(a.issue, a.points_to) for a in after.actions if a.kind == OK}
    settled_links = {(a.issue, a.work) for a in after.actions if a.kind == LINKED}
    still_open = {a.issue for a in after.actions if a.closing}
    problems: list[str] = []
    for action in applied:
        if action.kind == CLOSE and action.issue in still_open:
            problems.append(f"{action.issue}: not closed")
        if action.kind == POINTS and (action.issue, action.points_to) not in settled_points:
            problems.append(f"{action.issue}: story points aren't "
                            f"{fmt_points(action.points_to)}")
        if action.kind == LINK and (action.issue, action.work) not in settled_links:
            problems.append(f"{action.issue}: no {ctx.settings.link_description!r} "
                            f"link to {action.work}")
    return problems


def _show_bill_step(action: BillAction, index: int, total: int, plan: BillPlan,
                    description: str) -> None:
    print()
    print(f"[{index}/{total}] {action.kind.upper()}  {action.issue}  {action.summary}")
    if action.kind == POINTS:
        line = next((bl for bl in plan.bill.lines if bl.deliverable
                     and bl.deliverable.casefold() == action.deliverable.casefold()),
                    None)
        print(f"      story points {fmt_points(action.points_from)} -> "
              f"{fmt_points(action.points_to)}"
              + (f"  ({line.title})" if line else f"  ({action.note})"))
    elif action.kind == CLOSE:
        print(f"      close it: {action.note}")
    else:
        print(f"      {action.issue} {description} {action.work}")
        if action.work_title:
            print(f"      {action.work}: {action.work_title}")
    print()


def bill_apply(args: argparse.Namespace, ctx: Context, bill: BillExport) -> int:
    description = ctx.settings.link_description
    closing = not args.no_close
    plan = build_bill_plan(ctx, bill, closing=closing)
    print_bill_header(ctx, bill, plan.link_type)
    print("\n".join(render_bill_plan(plan, description, limit=args.limit,
                                     changes_only=args.changes_only)))
    print()

    problems = sum(plan.count(k) for k in BILL_PROBLEMS)
    if problems and not args.skip_missing:
        print(f"Stopped: {problems} problem(s) marked missing or closed above. Fix "
              "them, or add --skip-missing to do the rest.")
        return EXIT_JIRA
    todo = plan.writes()[:args.limit] if args.limit else plan.writes()
    if not todo:
        print("Nothing to change.")
        return EXIT_OK

    review = args.review or ctx.settings.review
    if review != "none":
        if not sys.stdin.isatty():
            raise ConfigError("--step needs an interactive terminal")
        todo = walk_changes(todo, lambda a, i, n: _show_bill_step(a, i, n, plan,
                                                                  description))
        if not todo:
            print("Everything was left out, so nothing was changed.")
            return EXIT_OK

    if not confirm(f"Make {len(todo)} change(s) in {ctx.settings.url} as "
                   f"{ctx.me.get('name')}?", args.yes):
        print("Nothing changed.")
        return EXIT_OK
    if not args.yes or review != "none":
        fresh = {_bill_signature(a)
                 for a in build_bill_plan(ctx, bill, closing=closing).writes()}
        stale = sorted({str(a.issue) for a in todo if _bill_signature(a) not in fresh})
        if stale:
            print(f"Stopped: Jira changed on {', '.join(stale)} while waiting. "
                  "Nothing was changed; run apply again for a fresh plan.")
            return EXIT_DIFFERENCES

    journal = Journal(ctx.settings.journal_dir, "apply", {
        "url": ctx.settings.url, "user": ctx.me.get("name"),
        "export": str(bill.path), "scope": f"bill for {bill.month_name}",
    })
    links = LinkMaker(ctx, plan.link_type)
    applied: list[BillAction] = []
    held: list[BillAction] = []
    failed = False

    def run(actions: list[BillAction]) -> bool:
        for action in actions:
            try:
                execute_bill(ctx, action, journal, links)
            except JiraError as exc:
                print(f"FAILED on {action.issue} ({action.kind}): {exc}")
                print("Stopping here; nothing after this was attempted.")
                return False
            applied.append(action)
            print(f"  {action.issue}: {describe_bill_action(action, description)}")
        return True

    try:
        failed = not run([a for a in todo if a.kind != CLOSE])
        closes = [a for a in todo if a.kind == CLOSE]
        if closes and not failed:
            # Closing is what gets a ticket paid: re-read Jira first, and only
            # close tickets that now match the bill exactly.
            check = build_bill_plan(ctx, bill, closing=False)
            unsettled = {a.issue for a in check.actions if a.issue and a.kind in
                         (POINTS, LINK, REVERSED, *BILL_PROBLEMS)}
            held = [a for a in closes if a.issue in unsettled]
            for action in held:
                print(f"  {action.issue}: left open - it doesn't match the bill yet")
            failed = not run([a for a in closes if a.issue not in unsettled])
    finally:
        journal.close()

    print(f"\n{len(applied)} change(s) made. Journal: {journal.path}")
    if applied:
        print(f"  (to reverse them: {Path(sys.argv[0]).name} undo {journal.path})")
    print("Verifying against Jira...")
    problems = verify_bill_applied(ctx, bill, applied)
    for problem in problems:
        print(f"  !! {problem}")
    if problems:
        print(f"Verification FAILED for {len(problems)} change(s).")
        return EXIT_DIFFERENCES
    print(f"Verified: all {len(applied)} change(s) are in Jira as planned.")
    left = len(plan.writes()) - len(applied)
    if left and not failed:
        print(f"{left} further change(s) not made this run.")
    if failed:
        return EXIT_JIRA
    return EXIT_DIFFERENCES if held else EXIT_OK



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
            if a.allocation and (a.kind in (CREATE, MOVE) or "comment" in a.changes)]


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


STEP_HELP = (
    ("y", "send this change as shown"),
    ("e", "edit the comment in your editor, then decide"),
    ("s", "skip it this run (a later apply offers it again)"),
    ("a", "send this one and all the rest without asking"),
    ("q", "stop reviewing; the rest are skipped this run"),
)


def walk_changes(todo: list[Any], show: Callable[[Any, int, int], None],
                 edit: Callable[[Any, int], bool] | None = None,
                 can_edit: Callable[[Any], bool] = lambda _change: False,
                 ) -> list[Any]:
    """Ask about each change in turn; returns the ones to send.

    ``show`` prints a change; ``edit`` (offered where ``can_edit`` says so)
    returns True if it changed something worth showing again.
    """
    keep: list[Any] = []
    skipped = 0
    send_rest = stop = False
    for index, change in enumerate(todo, 1):
        if send_rest:
            keep.append(change)
            continue
        editable = edit is not None and can_edit(change)
        choices = "[y]es  " + ("[e]dit  " if editable else "") + \
            "[s]kip  [a]ll the rest  [q]uit  [?]"
        show(change, index, len(todo))
        while True:
            answer = input(f"  {choices} ").strip().lower()[:1]
            if answer == "y":
                keep.append(change)
            elif answer == "a":
                keep.append(change)
                send_rest = True
            elif answer == "s":
                skipped += 1
            elif answer == "q":
                stop = True
            elif answer == "e" and edit is not None and editable:
                if edit(change, index):
                    show(change, index, len(todo))
                continue
            else:
                print("\n".join(f"    {key}  {text}" for key, text in STEP_HELP
                                if key != "e" or editable))
                continue
            break
        if stop:
            break

    unreviewed = len(todo) - len(keep) - skipped
    print(f"\nReviewed: {len(keep)} to send, {skipped} skipped"
          + (f", {unreviewed} not reviewed" if unreviewed else ""))
    return keep


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
    elif action.kind == MOVE and alloc and wl:
        what = f"{fmt_duration(alloc.seconds)}, worklog {wl.id} {action.note}"
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
    if action.kind == MOVE and wl:
        print(f"    Creates it on {action.issue_key}, then deletes worklog {wl.id} "
              f"from {wl.issue_key}.")
    if action.kind in (CREATE, MOVE) and alloc:
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

    def edit(action: Action, index: int) -> bool:
        assert action.allocation is not None
        comment = _edit_one(ctx, action.allocation.comment)
        if comment is None:
            return False
        _set_comment(action, comment)
        if all(a is not action for _, a in rewritten):
            rewritten.append((index, action))
        edits_path.write_text(edit_file_text(rewritten, {}), encoding="utf-8")
        return True

    keep = walk_changes(
        todo, lambda action, index, total: _show_step(action, index, total, plan),
        edit, lambda action: id(action) in editable_ids,
    )
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


# Options that only make sense for worklogs, by argparse dest
WORKLOG_ONLY = (("issue", "--issue"), ("date", "--date"), ("date_from", "--from"),
                ("date_to", "--to"), ("unentered_only", "--unentered-only"),
                ("delete_extra", "--delete-extra"), ("show_comments", "--show-comments"),
                ("edits", "--edits"))


def load_for_command(args: argparse.Namespace) -> Export | BillExport:
    """The command's export, with options checked against its kind."""
    export = read_export(Path(args.export).expanduser())
    if isinstance(export, BillExport):
        used = [flag for dest, flag in WORKLOG_ONLY if getattr(args, dest, None)]
        if used:
            raise ConfigError(f"{', '.join(used)} only apply to worklogs, not to "
                              "a bill export")
        if getattr(args, "review", None) == "editor":
            raise ConfigError("a bill has no comments to edit; --step reviews it")
        if args.month:
            export.year, export.month = args.month
    elif args.month or args.no_close:
        raise ConfigError("--month and --no-close only apply to a bill export")
    return export


def bill_report(args: argparse.Namespace, bill: BillExport, *, verify: bool) -> int:
    """plan or verify for a bill export."""
    ctx = connect(args)
    plan = build_bill_plan(ctx, bill, closing=not args.no_close)
    print_bill_header(ctx, bill, plan.link_type)
    print("\n".join(render_bill_plan(plan, ctx.settings.link_description,
                                     limit=None if verify else args.limit,
                                     changes_only=args.changes_only)))
    if verify:
        differences = sum(plan.count(k) for k in (*BILL_WRITES, REVERSED, *BILL_PROBLEMS))
        if differences:
            print(f"NOT IN SYNC: {differences} difference(s) between Jira and the bill.")
            return EXIT_DIFFERENCES
        print(f"IN SYNC: your {bill.month_name} billing tickets match the bill.")
        return EXIT_OK
    writes = len(plan.writes())
    if writes:
        print(f"Nothing has been changed. apply with the same options would make "
              f"{min(writes, args.limit or writes)} change(s).")
    return EXIT_OK


def _prepare(args: argparse.Namespace, export: Export
             ) -> tuple[Context, Export, Selection, Plan]:
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
    if settings.billing_initials:
        today = date.today()
        example = billing_summary(settings, "WP2d-D1", today.year, today.month)
        try:
            names = {str(f.get("id")): str(f.get("name")) for f in client.fields()}
            field = names.get(settings.points_field, "NOT FOUND in Jira")
        except JiraError as exc:
            field = f"couldn't check: {str(exc).splitlines()[0]}"
        try:
            link = f"type {find_link_type(client, settings.link_description).name}"
        except (JiraError, ConfigError) as exc:
            link = str(exc).splitlines()[0]
        print(f"Billing:    {settings.billing_project} tickets named like {example}")
        print(f"            points in {settings.points_field} ({field}); links "
              f"read {settings.link_description!r} ({link})")
    else:
        print("Billing:    not set up (no initials in [billing])")
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
    export = load_for_command(args)
    if isinstance(export, BillExport):
        return bill_report(args, export, verify=False)
    _, _, _, plan = _prepare(args, export)
    print("\n".join(render_plan(plan, limit=args.limit,
                                show_comments=args.show_comments,
                                changes_only=args.changes_only)))
    writes = len(plan.writes())
    if writes:
        print(f"Nothing has been changed. apply with the same options would make "
              f"{min(writes, args.limit or writes)} change(s).")
    return EXIT_OK


def cmd_verify(args: argparse.Namespace) -> int:
    export = load_for_command(args)
    if isinstance(export, BillExport):
        return bill_report(args, export, verify=True)
    _, _, _, plan = _prepare(args, export)
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
    loaded = load_for_command(args)
    if isinstance(loaded, BillExport):
        return bill_apply(args, connect(args), loaded)
    ctx, export, selection, plan = _prepare(args, loaded)
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


@dataclass
class UndoStep:
    """What undo will do about one journalled change; run None means nothing."""

    label: str
    run: Callable[[Journal], str] | None = None


def _undo_worklog(ctx: Context, op: dict[str, Any], force: bool) -> UndoStep:
    key, worklog_id = op["issue"], str(op["worklog_id"])
    where = f"{key} {op['day']} worklog {worklog_id}"
    day = date.fromisoformat(op["day"])
    adjust = ctx.settings.adjust_estimate
    if op["op"] == "delete":
        twin = _find_twin(ctx, key, op["before"])
        if twin:
            return UndoStep(f"  -         {where}: already back as worklog {twin}")

        def recreate(journal: Journal) -> str:
            created = ctx.client.add_worklog(key, dict(op["before"]), adjust)
            new_id = str(created.get("id"))
            journal.op("create", key, new_id, day, after=_snapshot(created))
            return f"recreated worklog on {key} as {new_id}"
        return UndoStep(f"  recreate  {where}  "
                        f"({fmt_duration(op['before']['timeSpentSeconds'])})", recreate)

    current = ctx.client.worklog(key, worklog_id)
    if current is None:
        return UndoStep(f"  -         {where}: already gone, nothing to undo")
    if not force and not _same_content(current, op["after"]):
        return UndoStep(f"  -         {where}: edited since, leaving it "
                        "(--force to undo anyway)")
    if op["op"] == "create":
        def delete(journal: Journal) -> str:
            ctx.client.delete_worklog(key, worklog_id, adjust)
            journal.op("delete", key, worklog_id, day, before=_snapshot(current))
            return f"deleted worklog {worklog_id} on {key}"
        return UndoStep(f"  delete    {where}  "
                        f"({fmt_duration(current.get('timeSpentSeconds') or 0)})", delete)

    def restore(journal: Journal) -> str:
        body = _update_body({f: op["before"][f] for f in op["changed"]}, op["before"])
        updated = ctx.client.update_worklog(key, worklog_id, body, adjust)
        journal.op("update", key, worklog_id, day, changed=op["changed"],
                   before=_snapshot(current), after=_snapshot(updated))
        return f"restored worklog {worklog_id} on {key}"
    return UndoStep(f"  restore   {where}  ({', '.join(op['changed'])})", restore)


def _undo_points(ctx: Context, op: dict[str, Any], force: bool) -> UndoStep:
    key, field_id = op["issue"], op["field"]
    issue = ctx.client.issue(key, fields=field_id)
    if issue is None:
        return UndoStep(f"  -         {key}: not found, nothing to undo")
    current = (issue.get("fields") or {}).get(field_id)
    if not force and fmt_points(current) != fmt_points(op["after"]):
        return UndoStep(f"  -         {key}: story points changed since "
                        f"({fmt_points(current)}), leaving them (--force to undo anyway)")

    def restore(journal: Journal) -> str:
        ctx.client.set_field(key, field_id, op["before"])
        journal.record("points", issue=key, field=field_id, before=current,
                       after=op["before"])
        return f"story points on {key} back to {fmt_points(op['before'])}"
    return UndoStep(f"  points    {key}: {fmt_points(current)} -> "
                    f"{fmt_points(op['before'])}", restore)


def _undo_link(ctx: Context, op: dict[str, Any]) -> UndoStep:
    key, other = op["issue"], op["other"]
    description = ctx.settings.link_description
    if op["op"] == "link":
        link_id = str(op["link_id"])
        if ctx.client.link(link_id) is None:
            return UndoStep(f"  -         {key} -> {other}: link already gone")

        def unlink(journal: Journal) -> str:
            ctx.client.delete_link(link_id)
            journal.record("unlink", issue=key, other=other, link_id=link_id)
            return f"removed the link from {key} to {other}"
        return UndoStep(f"  unlink    {key} {description} {other}", unlink)

    issue = ctx.client.issue(key, fields="issuelinks") or {}
    if other in read_links(issue, description)[0]:
        return UndoStep(f"  -         {key} -> {other}: already linked again")

    def relink(journal: Journal) -> str:
        link_type = find_link_type(ctx.client, description)
        link_id = LinkMaker(ctx, link_type).create(key, other)
        journal.record("link", issue=key, other=other, link_id=link_id)
        return f"linked {key} to {other} again"
    return UndoStep(f"  relink    {key} {description} {other}", relink)


def _undo_close(ctx: Context, op: dict[str, Any]) -> UndoStep:
    key = op["issue"]
    issue = ctx.client.issue(key, fields="status")
    if issue is None:
        return UndoStep(f"  -         {key}: not found, nothing to undo")
    fields = issue.get("fields") or {}
    status = str((fields.get("status") or {}).get("name") or "")
    if op["op"] == "reopen":
        if _is_done(fields):
            return UndoStep(f"  -         {key}: already closed ({status})")

        def close(journal: Journal) -> str:
            close_ticket(ctx, key, journal)
            return f"closed {key} again"
        return UndoStep(f"  close     {key}  ({status})", close)

    if not _is_done(fields):
        return UndoStep(f"  -         {key}: already open ({status})")
    wanted = str(op.get("status_before") or "").casefold()
    transitions = ctx.client.transitions(key)
    reopen = ([t for t in transitions
               if str((t.get("to") or {}).get("name", "")).casefold() == wanted]
              or [t for t in transitions
                  if ((t.get("to") or {}).get("statusCategory") or {}).get("key") != "done"])
    if len(reopen) != 1:
        return UndoStep(f"  -         {key}: can't tell how to reopen it - do that by "
                        "hand, then run undo again for the rest")
    transition = reopen[0]

    def do_reopen(journal: Journal) -> str:
        ctx.client.transition(key, {"transition": {"id": str(transition.get("id"))}})
        journal.record("reopen", issue=key, status_before=status)
        return f"reopened {key} ({(transition.get('to') or {}).get('name')})"
    return UndoStep(f"  reopen    {key}  via {transition.get('name')!r}", do_reopen)


def undo_step(ctx: Context, op: dict[str, Any], force: bool) -> UndoStep:
    if op["op"] in ("close", "reopen"):
        return _undo_close(ctx, op)
    if op["op"] in ("create", "update", "delete"):
        return _undo_worklog(ctx, op, force)
    if op["op"] == "points":
        return _undo_points(ctx, op, force)
    if op["op"] in ("link", "unlink"):
        return _undo_link(ctx, op)
    return UndoStep(f"  ?         unknown change {op['op']!r}, skipped")


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

    # Newest first, so anything changed twice is unwound in the right order
    steps: list[UndoStep] = []
    for op in reversed(ops):
        step = undo_step(ctx, op, args.force)
        print(step.label)
        if step.run is not None:
            steps.append(step)

    if not steps:
        print("Nothing to undo.")
        return EXIT_OK
    if not confirm(f"Undo {len(steps)} change(s)?", args.yes):
        print("Nothing changed.")
        return EXIT_OK

    journal = Journal(ctx.settings.journal_dir, "undo", {
        "url": ctx.settings.url, "user": ctx.me.get("name"), "undoes": str(path),
    })
    try:
        for step in steps:
            assert step.run is not None
            try:
                print(f"  {step.run(journal)}")
            except JiraError as exc:
                print(f"FAILED: {step.label.strip()}: {exc}")
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


def _year_month(text: str) -> tuple[int, int]:
    try:
        year, month = (int(part) for part in text.split("-"))
        date(year, month, 1)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not YYYY-MM") from None
    return year, month


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
    scope.add_argument("export", help="JSON export from timesheets: allocations "
                                      "(R) or a bill (J in the Billing view)")
    scope.add_argument("--issue", action="append", default=[], metavar="ID",
                       help="only this issue: a timesheets ticket ID or a Jira "
                            "key (repeatable)")
    scope.add_argument("--date", action="append", default=[], type=_iso_date,
                       metavar="YYYY-MM-DD", help="only this day (repeatable)")
    scope.add_argument("--from", dest="date_from", type=_iso_date,
                       metavar="YYYY-MM-DD", help="only from this day")
    scope.add_argument("--to", dest="date_to", type=_iso_date,
                       metavar="YYYY-MM-DD", help="only up to this day")
    scope.add_argument("--month", type=_year_month, metavar="YYYY-MM",
                       help="bill exports: the month the billing tickets are "
                            "named for, if not the bill's own")
    scope.add_argument("--no-close", action="store_true",
                       help="bill exports: leave the tickets open this run")
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
