#!/usr/bin/env python3
"""A small stand-in for the Jira 8 REST endpoints sync_worklogs.py uses.

Standard library only, so it runs anywhere the tool does - including the
client site, to prove an install end to end before pointing it at the real
Jira. It behaves like Jira where that matters to the tool: it hands times
back in UTC whatever offset they were sent with, insists on Jira's exact
timestamp format, wants a token, and won't let you edit someone else's
worklogs.

    python mock_jira.py --seed time-allocations-2026-10.json --scenario demo

then point a config at http://127.0.0.1:8089 with token mock-token.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import socketserver
import sys
import threading
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

DEFAULT_PORT = 8089
DEFAULT_TOKEN = "mock-token"
DEFAULT_USER = "mock.user"
OTHER_USER = "someone.else"
TIMEZONE = "Europe/London"

POINTS_FIELD = "customfield_10002"
MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December")
LINK_TYPES = [
    {"id": "10000", "name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
    {"id": "10003", "name": "Relates", "inward": "relates to", "outward": "relates to"},
    {"id": "10100", "name": "Delivers", "inward": "is delivered by",
     "outward": "delivers"},
]

# Status -> Jira status category
STATUSES = {"Open": "new", "Reopened": "new", "In Progress": "indeterminate",
            "Resolved": "done", "Closed": "done"}
RESOLUTION_FIELD = {"required": True, "name": "Resolution",
                    "allowedValues": [{"name": "Done"}, {"name": "Won't Do"}]}


def _status_json(name: str) -> dict[str, Any]:
    return {"name": name, "statusCategory": {"key": STATUSES.get(name, "new")}}


def _transitions(status: str) -> list[dict[str, Any]]:
    """A classic Jira workflow: two ways to a done status, one way back."""
    if STATUSES.get(status) == "done":
        return [{"id": "3", "name": "Reopen Issue", "to": _status_json("Reopened"),
                 "fields": {}}]
    return [
        {"id": "5", "name": "Resolve Issue", "to": _status_json("Resolved"),
         "fields": {"resolution": RESOLUTION_FIELD}},
        {"id": "2", "name": "Close Issue", "to": _status_json("Closed"),
         "fields": {"resolution": RESOLUTION_FIELD}},
    ]


STARTED_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{4}$")
JIRA_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%f%z"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def _user(name: str) -> dict[str, Any]:
    return {"name": name, "key": f"JIRAUSER-{name}", "displayName": name.title(),
            "active": True, "timeZone": TIMEZONE}


def _fmt_spent(seconds: int) -> str:
    hours, rest = divmod(seconds, 3600)
    parts = [f"{hours}h"] if hours else []
    if rest // 60:
        parts.append(f"{rest // 60}m")
    return " ".join(parts) or "0m"


def _local_day(started_utc: str) -> date:
    moment = datetime.strptime(started_utc, JIRA_TIME_FORMAT)
    try:
        from zoneinfo import ZoneInfo
        return moment.astimezone(ZoneInfo(TIMEZONE)).date()
    except Exception:
        return moment.date()


class MockJira:
    """In-memory Jira: issues and their worklogs, optionally saved to a file."""

    def __init__(self, user: str = DEFAULT_USER, token: str = DEFAULT_TOKEN,
                 state_path: Path | None = None, page_size: int = 1000) -> None:
        self.user = user
        self.token = token
        self.state_path = state_path
        self.page_size = page_size
        self.lock = threading.Lock()
        self.requests: list[tuple[str, str]] = []  # (method, path), for tests
        self.next_id = 100000
        self.issues: dict[str, dict[str, Any]] = {}
        # Links as Jira stores them: source [outward text] destination
        self.links: list[dict[str, str]] = []
        # Make links the other way round from the REST docs, as Jira 8 can
        self.reversed_links = False
        if state_path and state_path.exists():
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.next_id = saved["next_id"]
            self.issues = saved["issues"]
            self.links = saved.get("links", [])

    def save(self) -> None:
        if self.state_path:
            self.state_path.write_text(json.dumps(
                {"next_id": self.next_id, "issues": self.issues, "links": self.links},
                indent=2), encoding="utf-8")

    def _new_id(self) -> str:
        self.next_id += 1
        return str(self.next_id)

    def add_issue(self, key: str, summary: str, issuetype: str = "Task",
                  fields: dict[str, Any] | None = None, status: str = "Open") -> None:
        self.issues.setdefault(key.upper(), {
            "id": self._new_id(), "summary": summary, "worklogs": [],
            "issuetype": issuetype, "fields": dict(fields or {}),
            "status": status, "resolution": None})

    def add_link(self, type_name: str, inward_key: str, outward_key: str) -> dict[str, str]:
        """Link as the REST docs describe: outward [outward text] inward."""
        source, destination = outward_key.upper(), inward_key.upper()
        if self.reversed_links:
            source, destination = destination, source
        link = {"id": self._new_id(), "type": type_name, "source": source,
                "destination": destination}
        self.links.append(link)
        return link

    def links_of(self, key: str) -> list[dict[str, str]]:
        """Links touching key, each as (other key, the text it reads with)."""
        found = []
        for link in self.links:
            kind = next(t for t in LINK_TYPES if t["name"] == link["type"])
            if link["source"] == key:
                found.append({"other": link["destination"], "reads": kind["outward"],
                              "type": link["type"], "id": link["id"]})
            if link["destination"] == key:
                found.append({"other": link["source"], "reads": kind["inward"],
                              "type": link["type"], "id": link["id"]})
        return found

    def issue_json(self, key: str) -> dict[str, Any]:
        issue = self.issues[key]
        links = []
        for link in self.links:
            kind = next(t for t in LINK_TYPES if t["name"] == link["type"])
            for side, here, there in (("outwardIssue", "source", "destination"),
                                      ("inwardIssue", "destination", "source")):
                if link[here] == key:
                    other = link[there]
                    links.append({"id": link["id"], "type": kind, side: {
                        "key": other, "fields": {
                            "summary": self.issues.get(other, {}).get("summary", "")}}})
        return {"id": issue["id"], "key": key, "fields": {
            "summary": issue["summary"],
            "project": {"key": key.rpartition("-")[0]},
            "issuetype": {"name": issue.get("issuetype", "Task")},
            "status": _status_json(issue.get("status", "Open")),
            "resolution": ({"name": issue["resolution"]} if issue.get("resolution")
                           else None),
            "issuelinks": links, **issue.get("fields", {})}}

    def add_worklog(self, key: str, author: str, started: str, seconds: int,
                    comment: str) -> dict[str, Any]:
        """Store a worklog the way Jira does: started converted to UTC."""
        moment = datetime.strptime(started, JIRA_TIME_FORMAT)
        record = {
            "id": self._new_id(),
            "author": author,
            "updateAuthor": author,
            "started": moment.astimezone(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.000+0000"),
            "timeSpentSeconds": seconds,
            "comment": comment,
            "created": _now(),
            "updated": _now(),
        }
        self.issues[key.upper()]["worklogs"].append(record)
        return record

    def worklog_json(self, key: str, record: dict[str, Any]) -> dict[str, Any]:
        issue = self.issues[key]
        return {
            "self": f"/rest/api/2/issue/{issue['id']}/worklog/{record['id']}",
            "author": _user(record["author"]),
            "updateAuthor": _user(record["updateAuthor"]),
            "comment": record["comment"],
            "created": record["created"],
            "updated": record["updated"],
            "started": record["started"],
            "timeSpent": _fmt_spent(record["timeSpentSeconds"]),
            "timeSpentSeconds": record["timeSpentSeconds"],
            "id": record["id"],
            "issueId": issue["id"],
        }


def _key_for(ticket_id: str, prefix: str, project_map: dict[str, str] | None) -> str:
    """The tool's mapping, in brief: prefix bare numbers, rename projects."""
    if ticket_id.isdigit():
        return f"{prefix}{ticket_id}"
    project, dash, number = ticket_id.upper().rpartition("-")
    if not dash:
        return ticket_id.upper()
    return f"{(project_map or {}).get(project, project)}-{number}"


def _seed_bill(mock: MockJira, bill_path: Path, prefix: str, *, initials: str,
               billing_project: str, project_map: dict[str, str] | None = None,
               scenario: str | None = None) -> None:
    """Billing tickets for a bill export's month, as someone else makes them.

    One per deliverable on the bill, with made-up projected points; two for
    deliverables not on it; and two that must be left alone - someone else's,
    and one of yours for the month before. Plus the work tickets to link to.
    --scenario demo adds a link already in place and a stray one.
    """
    data = json.loads(bill_path.read_text(encoding="utf-8"))
    when = data.get("delivered") or data["billed"]
    year, month = int(when["year"]), int(when["month"])
    deliverables = [str(line["deliverable_id"]) for line in data["lines"]
                    if line.get("deliverable_id")]
    numbers = iter(range(501, 10000))

    def ticket(deliverable: str, points: float, who: str = initials,
               month_index: int = month) -> str:
        key = f"{billing_project}-{next(numbers)}"
        summary = f"{who}-{deliverable}-{MONTH_NAMES[month_index - 1]}-{year}"
        mock.add_issue(key, summary, fields={POINTS_FIELD: points})
        return key

    keys = [ticket(d, 10.0) for d in deliverables]
    for spare in ("WP4-D1", "WP5-D1"):
        if spare not in deliverables:
            ticket(spare, 8.0)
    first = deliverables[0] if deliverables else "WP2d-D1"
    ticket(first, 5.0, who="XY")
    last_month = ticket(first, 12.0, month_index=12 if month == 1 else month - 1)
    mock.issues[last_month]["status"] = "Closed"
    mock.issues[last_month]["resolution"] = "Done"

    for line in data["lines"]:
        for t in line.get("tickets", []):
            mock.add_issue(_key_for(str(t["ticket_id"]), prefix, project_map),
                           str(t.get("description") or ""))

    if scenario == "demo" and keys and data["lines"][0].get("tickets"):
        work = _key_for(str(data["lines"][0]["tickets"][0]["ticket_id"]), prefix,
                        project_map)
        mock.add_link("Delivers", keys[0], work)
        mock.add_issue(f"{prefix}99998", "Something not on this bill")
        mock.add_link("Delivers", keys[0], f"{prefix}99998")


def _seed(mock: MockJira, export_path: Path, prefix: str, *, entered: bool,
          scenario: str | None, missing: set[str],
          project_map: dict[str, str] | None = None) -> None:
    """Create the export's issues and, optionally, some worklogs to find.

    --seed-entered pretends allocations marked "entered on client" were
    logged by hand. --scenario demo adds one of each kind of discrepancy the
    tool has to cope with, so a plan against it shows every action.
    """
    data = json.loads(export_path.read_text(encoding="utf-8"))

    def key_for(ticket_id: str) -> str:
        return _key_for(ticket_id, prefix, project_map)

    for ticket_id, description in data.get("tickets", {}).items():
        key = key_for(ticket_id)
        if key not in missing:
            mock.add_issue(key, description)

    rows = [r for r in data.get("allocations", []) if key_for(r["ticket_id"]) in mock.issues]

    def started(day: str, hour: int = 9) -> str:
        # BST-ish offset, so the UTC conversion is exercised
        return f"{day}T{hour:02d}:00:00.000+0100"

    def log_row(row: dict[str, Any], seconds: int | None = None,
                comment: str | None = None, author: str | None = None) -> None:
        mock.add_worklog(key_for(row["ticket_id"]), author or mock.user,
                         started(row["date"]),
                         round(row["hours"] * 3600) if seconds is None else seconds,
                         row.get("description", "") if comment is None else comment)

    fresh = []
    for row in rows:
        if entered and row.get("entered_on_client"):
            log_row(row)
        else:
            fresh.append(row)

    if scenario != "demo" or len(fresh) < 5:
        return
    in_sync, wrong_time, wrong_comment, split, others = fresh[:5]
    log_row(in_sync)
    log_row(wrong_time, seconds=round(wrong_time["hours"] * 3600) + 1800)
    log_row(wrong_comment, comment="Typed in by hand, differently")
    # Two worklogs that don't add up to the allocation: a conflict
    half = round(split["hours"] * 1800)
    log_row(split, seconds=half)
    log_row(split, seconds=max(half - 900, 900), comment="second half")
    # Someone else's time on the same day must be ignored
    log_row(others, author=OTHER_USER, comment="not yours")

    # A worklog of yours on a day with no allocation for that issue
    key = key_for(in_sync["ticket_id"])
    busy = {r["date"] for r in rows if key_for(r["ticket_id"]) == key}
    day = date.fromisoformat(data["period"]["from"])
    while day.isoformat() in busy:
        day += timedelta(days=1)
    mock.add_worklog(key, mock.user, started(day.isoformat()), 3600,
                     "Logged against the wrong day")

    # Time logged on an issue the export doesn't mention at all
    stray = f"{prefix}99999"
    mock.add_issue(stray, "An issue that is not in the timesheet")
    mock.add_worklog(stray, mock.user, started(in_sync["date"], 14), 2700,
                     "Logged against the wrong issue")


class Handler(BaseHTTPRequestHandler):
    mock: MockJira  # set on the subclass made by make_server
    quiet = False

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        if not self.quiet:
            sys.stderr.write("mock-jira: " + format % args + "\n")

    def _send(self, status: int, payload: Any = None) -> None:
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "application/json;charset=UTF-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str = "",
               errors: dict[str, str] | None = None) -> None:
        self._send(status, {"errorMessages": [message] if message else [],
                            "errors": errors or {}})

    def _authorised(self) -> bool:
        header = self.headers.get("Authorization", "")
        if header == f"Bearer {self.mock.token}":
            return True
        if header.startswith("Basic "):
            user = base64.b64decode(header[6:]).decode("utf-8").split(":", 1)[0]
            return user == self.mock.user
        return False

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def _route(self, method: str) -> None:
        url = urlsplit(self.path)
        path = url.path
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        mock = self.mock
        with mock.lock:
            mock.requests.append((method, path))
            if path == "/rest/api/2/serverInfo" and method == "GET":
                return self._send(200, {"version": "8.20.7", "versionNumbers": [8, 20, 7],
                                        "deploymentType": "Server",
                                        "serverTitle": "Mock Jira"})
            if not self._authorised():
                return self._error(401, "You are not authenticated. "
                                        "Authentication required to perform this operation.")
            if path == "/rest/api/2/myself" and method == "GET":
                return self._send(200, _user(mock.user))
            if path == "/rest/api/2/search" and method == "GET":
                return self._search(query)
            if path == "/rest/api/2/field" and method == "GET":
                return self._send(200, [
                    {"id": "summary", "name": "Summary", "custom": False},
                    {"id": POINTS_FIELD, "name": "Story Points", "custom": True,
                     "schema": {"type": "number"}}])
            if path == "/rest/api/2/issueLinkType" and method == "GET":
                return self._send(200, {"issueLinkTypes": LINK_TYPES})
            if path == "/rest/api/2/issueLink" and method == "POST":
                return self._create_link()
            link_match = re.fullmatch(r"/rest/api/2/issueLink/(\d+)", path)
            if link_match:
                link = next((k for k in mock.links if k["id"] == link_match.group(1)), None)
                if link is None:
                    return self._error(404, "No issue link with id " + link_match.group(1))
                if method == "DELETE":
                    mock.links.remove(link)
                    mock.save()
                    return self._send(204)
                return self._send(200, {"id": link["id"], "type": {"name": link["type"]},
                                        "inwardIssue": {"key": link["destination"]},
                                        "outwardIssue": {"key": link["source"]}})

            transitions = re.fullmatch(r"/rest/api/2/issue/([^/]+)/transitions", path)
            if transitions:
                return self._transition(transitions.group(1).upper(), method)

            match = re.fullmatch(r"/rest/api/2/issue/([^/]+)(/worklog(?:/(\d+))?)?", path)
            if not match:
                return self._error(404, f"No mock for {method} {path}")
            key = match.group(1).upper()
            issue = mock.issues.get(key)
            if issue is None:
                return self._error(404, "Issue Does Not Exist")
            if not match.group(2):
                if method == "GET":
                    return self._send(200, mock.issue_json(key))
                if method == "PUT":
                    body_fields = self._body().get("fields", {})
                    if STATUSES.get(issue.get("status", "Open")) == "done":
                        # As a workflow that locks closed issues answers
                        return self._error(400, errors={
                            f: f"Field '{f}' cannot be set. It is not on the "
                               "appropriate screen, or unknown." for f in body_fields})
                    issue.setdefault("fields", {}).update(body_fields)
                    mock.save()
                    return self._send(204)
                return self._error(405, "Method not allowed")
            worklog_id = match.group(3)
            if worklog_id is None:
                if method == "GET":
                    return self._list_worklogs(key, query)
                if method == "POST":
                    return self._create_worklog(key)
                return self._error(405, "Method not allowed")
            record = next((w for w in issue["worklogs"] if w["id"] == worklog_id), None)
            if record is None:
                return self._error(404, "Cannot find worklog with id: " + worklog_id)
            if method == "GET":
                return self._send(200, mock.worklog_json(key, record))
            if record["author"] != mock.user:
                return self._error(403, "You do not have the permission to edit this worklog.")
            if method == "PUT":
                return self._update_worklog(key, record)
            if method == "DELETE":
                issue["worklogs"].remove(record)
                mock.save()
                return self._send(204)
            return self._error(405, "Method not allowed")

    def _list_worklogs(self, key: str, query: dict[str, str]) -> None:
        logs = self.mock.issues[key]["worklogs"]
        start = int(query.get("startAt", 0))
        page = logs[start:start + self.mock.page_size]
        self._send(200, {"startAt": start, "maxResults": len(page), "total": len(logs),
                         "worklogs": [self.mock.worklog_json(key, w) for w in page]})

    def _check_fields(self, body: dict[str, Any], creating: bool) -> dict[str, str]:
        errors: dict[str, str] = {}
        if "started" in body or creating:
            if not STARTED_RE.match(str(body.get("started", ""))):
                errors["started"] = ("Invalid date format. Please enter the date in "
                                     "the format \"yyyy-MM-dd'T'HH:mm:ss.SSSZ\".")
        if "timeSpentSeconds" in body or creating:
            seconds = body.get("timeSpentSeconds")
            if not isinstance(seconds, int) or seconds <= 0:
                errors["timeLogged"] = "You must indicate the time spent working."
        return errors

    def _create_worklog(self, key: str) -> None:
        body = self._body()
        errors = self._check_fields(body, creating=True)
        if errors:
            return self._error(400, errors=errors)
        record = self.mock.add_worklog(key, self.mock.user, body["started"],
                                       body["timeSpentSeconds"], body.get("comment") or "")
        self.mock.save()
        self._send(201, self.mock.worklog_json(key, record))

    def _update_worklog(self, key: str, record: dict[str, Any]) -> None:
        body = self._body()
        errors = self._check_fields(body, creating=False)
        if errors:
            return self._error(400, errors=errors)
        if "started" in body:
            moment = datetime.strptime(body["started"], JIRA_TIME_FORMAT)
            record["started"] = moment.astimezone(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.000+0000")
        if "timeSpentSeconds" in body:
            record["timeSpentSeconds"] = body["timeSpentSeconds"]
        if "comment" in body:
            record["comment"] = body["comment"] or ""
        record["updated"] = _now()
        record["updateAuthor"] = self.mock.user
        self.mock.save()
        self._send(200, self.mock.worklog_json(key, record))

    def _transition(self, key: str, method: str) -> None:
        issue = self.mock.issues.get(key)
        if issue is None:
            return self._error(404, "Issue Does Not Exist")
        available = _transitions(issue.get("status", "Open"))
        if method == "GET":
            return self._send(200, {"expand": "transitions", "transitions": available})
        if method != "POST":
            return self._error(405, "Method not allowed")
        body = self._body()
        wanted = str((body.get("transition") or {}).get("id", ""))
        chosen = next((t for t in available if t["id"] == wanted), None)
        if chosen is None:
            return self._error(400, f"It seems that you have tried to perform a "
                                    f"workflow operation ({wanted}) that is not valid "
                                    "for the current state of this issue.")
        resolution = ((body.get("fields") or {}).get("resolution") or {}).get("name")
        if "resolution" in chosen["fields"] and not resolution:
            return self._error(400, errors={"resolution": "Resolution is required."})
        issue["status"] = chosen["to"]["name"]
        issue["resolution"] = resolution if STATUSES[issue["status"]] == "done" else None
        self.mock.save()
        self._send(204)

    def _create_link(self) -> None:
        body = self._body()
        type_name = (body.get("type") or {}).get("name", "")
        if not any(t["name"] == type_name for t in LINK_TYPES):
            return self._error(404, f"No issue link type with name '{type_name}' found.")
        keys = [str((body.get(side) or {}).get("key", "")).upper()
                for side in ("inwardIssue", "outwardIssue")]
        for key in keys:
            if key not in self.mock.issues:
                return self._error(404, f"Issue Does Not Exist: {key}")
        self.mock.add_link(type_name, keys[0], keys[1])
        self.mock.save()
        self._send(201)

    def _search(self, query: dict[str, str]) -> None:
        """The two JQL shapes the tool sends: your worklogs between dates, and
        a project's issues by type and summary words."""
        jql = query.get("jql", "")
        if "worklogAuthor" in jql:
            low = re.search(r'worklogDate\s*>=\s*"?(\d{4}-\d{2}-\d{2})', jql)
            high = re.search(r'worklogDate\s*<=\s*"?(\d{4}-\d{2}-\d{2})', jql)
            if not low or not high:
                return self._error(400, f"Mock search can't handle: {jql}")
            first, last = date.fromisoformat(low.group(1)), date.fromisoformat(high.group(1))
            keys = [key for key, issue in sorted(self.mock.issues.items())
                    if any(w["author"] == self.mock.user
                           and first <= _local_day(w["started"]) <= last
                           for w in issue["worklogs"])]
        else:
            project = re.search(r'project\s*=\s*"?([A-Z0-9_]+)', jql)
            if not project:
                return self._error(400, f"Mock search can't handle: {jql}")
            issuetype = re.search(r'issuetype\s*=\s*"([^"]+)"', jql)
            words = [w.casefold() for w in re.findall(r'summary\s*~\s*"([^"]+)"', jql)]
            keys = [key for key, issue in sorted(self.mock.issues.items())
                    if key.startswith(project.group(1) + "-")
                    and (not issuetype
                         or issue.get("issuetype", "Task") == issuetype.group(1))
                    and all(w in issue["summary"].casefold() for w in words)]
        found = [self.mock.issue_json(key) for key in keys]
        start = int(query.get("startAt", 0))
        size = int(query.get("maxResults", 50))
        self._send(200, {"startAt": start, "maxResults": size, "total": len(found),
                         "issues": found[start:start + size]})

    def do_GET(self) -> None:  # noqa: N802
        self._route("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._route("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._route("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._route("DELETE")


class _Server(ThreadingHTTPServer):
    def server_bind(self) -> None:
        # HTTPServer's own version does a reverse DNS lookup of the host,
        # which hangs for half a minute on a machine without DNS.
        socketserver.TCPServer.server_bind(self)
        self.server_name = str(self.server_address[0])
        self.server_port = int(self.server_address[1])


def make_server(mock: MockJira, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                quiet: bool = False) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"mock": mock, "quiet": quiet})
    return _Server((host, port), handler)


def _project_pair(text: str) -> tuple[str, str]:
    source, equals, target = text.upper().partition("=")
    source, target = source.strip().rstrip("-"), target.strip().rstrip("-")
    if not (equals and source and target):
        raise argparse.ArgumentTypeError(f"{text!r} is not FROM=TO, e.g. FD=MPITFD")
    return source, target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--token", default=DEFAULT_TOKEN)
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--state", metavar="FILE",
                        help="keep state in this JSON file between runs")
    parser.add_argument("--reset", action="store_true",
                        help="ignore any saved --state and start afresh")
    parser.add_argument("--seed", metavar="EXPORT",
                        help="create an issue for each ticket in this export")
    parser.add_argument("--project-map", action="append", default=[],
                        type=_project_pair, metavar="FROM=TO",
                        help="rename a project when seeding, e.g. FD=MPITFD "
                             "(repeatable; match your config's [project_map])")
    parser.add_argument("--prefix", default="MPIT-",
                        help="turns bare ticket numbers into keys (default MPIT-)")
    parser.add_argument("--seed-entered", action="store_true",
                        help="also log allocations marked entered on client, "
                             "as if typed in by hand")
    parser.add_argument("--scenario", choices=["demo"],
                        help="demo: add one of each discrepancy the tool handles")
    parser.add_argument("--missing", action="append", default=[], metavar="KEY",
                        help="leave this issue out, to rehearse a mapping error")
    parser.add_argument("--seed-bill", metavar="BILL",
                        help="create billing tickets for this bill export's month")
    parser.add_argument("--initials", default="WB",
                        help="whose billing tickets to create (default WB)")
    parser.add_argument("--billing-project", default="PITLAI",
                        help="billing project key (default PITLAI)")
    parser.add_argument("--reversed-links", action="store_true",
                        help="make links the other way round, as Jira 8 can")
    args = parser.parse_args(argv)

    state = Path(args.state) if args.state else None
    if state and args.reset and state.exists():
        state.unlink()
    resumed = bool(state and state.exists())
    mock = MockJira(args.user, args.token, state)
    mock.reversed_links = args.reversed_links
    if args.seed and not resumed:
        _seed(mock, Path(args.seed), args.prefix.upper(),
              entered=args.seed_entered, scenario=args.scenario,
              missing={k.upper() for k in args.missing},
              project_map=dict(args.project_map))
    if args.seed_bill and not resumed:
        _seed_bill(mock, Path(args.seed_bill), args.prefix.upper(),
                   initials=args.initials, billing_project=args.billing_project.upper(),
                   project_map=dict(args.project_map), scenario=args.scenario)
    if (args.seed or args.seed_bill) and resumed:
        print(f"Resuming {state}; seeding ignored (add --reset to reseed)")
    elif args.seed or args.seed_bill:
        mock.save()

    server = make_server(mock, port=args.port)
    worklogs = sum(len(i["worklogs"]) for i in mock.issues.values())
    print(f"Mock Jira on http://127.0.0.1:{args.port}  (user {args.user}, "
          f"token {args.token}; {len(mock.issues)} issues, {worklogs} worklogs)")
    print("Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
