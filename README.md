# Timesheet TUI

A terminal-based timesheet application for tracking work hours, built with
[Textual](https://textual.textualize.io/).

## Prerequisites

- macOS (tested on macOS 14+)
- [Homebrew](https://brew.sh/)
- Python 3.12+
- Git with SSH access to GitHub

## Installation

1. Install Python 3.12 via Homebrew:

   ```bash
   brew install python@3.12
   ```

2. Create a project directory and clone the repository:

   ```bash
   mkdir -p ~/Projects
   git clone git@github.com:gravitygun/timesheets.git ~/Projects/timesheets
   cd ~/Projects/timesheets
   ```

3. Create and activate a Python virtual environment:

   ```bash
   python3.12 -m venv .venv
   source .venv/bin/activate
   ```

4. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

5. (Optional, multi-machine only) Clone the data repo for cross-machine
   sync. See [Multi-Machine Setup](#multi-machine-setup) below for what
   this is and why iCloud isn't used.

   ```bash
   git clone git@github.com:gravitygun/timesheets-data.git ~/.timesheets-data
   ```

   The app stores its database at
   `~/Library/Application Support/timesheets/timesheet.db` by default. Do
   **not** set `TIMESHEET_DB` to a cloud-synced path — iCloud has corrupted
   the SQLite WAL files in the past. The `TIMESHEET_DB` override is kept
   only for tests and edge cases.

## Running the App

**Always launch the app via `./run`, never `python app.py` directly.**
The launcher pulls the latest DB dump from the data repo before the app
starts and pushes your changes back when it exits. Bypassing it means
working against a stale DB, or losing work that never makes it to the
other machine.

```bash
./run app    # TUI only
./run api    # HTTP API only
./run both   # API in background + TUI in foreground
```

The launcher activates the venv for you. See
[Multi-Machine Setup](#multi-machine-setup) for the underlying sync flow
and recovery if a session crashes before the push.

To check database location without running the app:

```bash
source .venv/bin/activate
python app.py --db-info
```

## User Guide

### Views

The app has four top-level views, accessible via keyboard shortcuts:

| Key | View        | Description                           |
| --- | ----------- | ------------------------------------- |
| `w` | Week        | Daily time entries for one week       |
| `m` | Month       | Weekly summaries for one month        |
| `y` | Year        | Monthly summaries for fiscal year     |
| `M` | Allocations | Ticket allocations matrix for a month |

Pressing `Enter` on a day in week view opens **Day view**, showing ticket
allocations and work descriptions for that day.

### Navigation

| Key       | Action                                                      |
| --------- | ----------------------------------------------------------- |
| `Left`    | Previous week/month/year (depending on view)                |
| `Right`   | Next week/month/year (depending on view)                    |
| `[` / `]` | Previous/next month (allocations view)                      |
| `Up/Down` | Navigate rows                                               |
| `Enter`   | Drill down (year → month → week → day)                      |
| `Esc`     | Return to week view (from day view)                         |
| `t`       | Jump to today                                               |

### Editing

| Key         | Action                                            |
| ----------- | ------------------------------------------------- |
| `e`/`Enter` | Edit selected day or allocation                   |
| `L`         | Quick add Leave (7.5h)                            |
| `S`         | Quick add Sick (7.5h)                             |
| `T`         | Quick add Training (7.5h)                         |
| `h`         | Populate UK bank holidays (year view)             |
| `Ctrl+X`    | Cut day entry (week view)                         |
| `Ctrl+C`    | Copy day entry (week view)                        |
| `Ctrl+V`    | Paste day entry (week view)                       |

Quick adjust shortcuts (`L`/`S`/`T`) prompt for confirmation if the day
already has data.

### Ticket Tracking

Worked hours can be allocated to tickets (e.g., JIRA IDs) for billing reports.

| Key | Action                                                     |
| --- | ---------------------------------------------------------- |
| `K` | Open ticket management screen                              |
| `M` | Open allocations report (tickets × days)                   |
| `a` | Add allocation (day view and allocations view)             |
| `e` | Edit allocation (day view and allocations view)            |
| `d` | Delete allocation (day view and allocations view)          |
| `v` | Move allocation to a different day (day/allocations view)  |
| `c` | Toggle "entered on client" flag (day view)                 |
| `p` | Toggle "points entered" flag (allocations view)            |

**Week view indicators** (Alloc column):

| Symbol | Meaning                 | Style  |
| ------ | ----------------------- | ------ |
| `-`    | No worked hours         | dim    |
| `?`    | Worked but no alloc     | dim    |
| `↓`    | Under-allocated         | yellow |
| `↑`    | Over-allocated          | red    |
| `✓`    | Fully allocated         | green  |

**Ticket management** (`K`):

- Create tickets with ID (max 8 chars) and description
- Archive/unarchive tickets
- Delete tickets (only if no allocations exist)
- Search/filter tickets

### Other

| Key | Action                          |
| --- | ------------------------------- |
| `$` | Toggle earnings display         |
| `?` | Show keyboard shortcuts         |
| `q` | Quit                            |

### Adjustment Types

When editing a day, you can add adjustment hours with a type:

| Type | Meaning        |
| ---- | -------------- |
| `P`  | Public Holiday |
| `L`  | Leave          |
| `S`  | Sick           |
| `T`  | Training       |

Only `P` (Public Holiday) adjustments reduce the target maximum hours
for the period.

### Week Structure

- Weeks start on **Saturday** (for billing purposes)
- Boundary weeks (e.g., Dec 28 - Jan 3) appear in both adjacent months
- Days from adjacent months are shown in parentheses, e.g., `(Dec 28)`

### Display Notes

- Weekend rows (Sat/Sun) are dimmed
- Earnings are hidden by default (toggle with `$`)
- Standard working day is 7.5 hours

## HTTP API

A small read/write HTTP API is shipped alongside the TUI for automating
allocation entry from external tools (e.g. an AI assistant on a remote dev
machine, reached over an SSH `RemoteForward`).

Start it with:

```bash
./run_api.sh
```

By default it binds `127.0.0.1:8765`. Override with `TIMESHEETS_API_HOST` /
`TIMESHEETS_API_PORT`. The TUI can stay open while the API is running -
SQLite is opened in WAL mode so reads and writes don't fight.

Endpoints (auto-generated docs at <http://127.0.0.1:8765/docs>):

<!-- markdownlint-disable MD013 -->
| Method | Path                                  | Purpose                                                  |
| ------ | ------------------------------------- | -------------------------------------------------------- |
| GET    | `/health`                             | Liveness + reports the active database path              |
| GET    | `/entries/{date}`                     | Attendance + worked/allocated/gap hours for a day        |
| GET    | `/tickets?q=&include_archived=`       | List/search tickets                                      |
| GET    | `/tickets/{id}`                       | Fetch a ticket (includes `deliverable_id`)               |
| POST   | `/tickets`                            | Create a ticket (`{id, description, deliverable_id?}`)   |
| GET    | `/deliverables?active_only=`          | List deliverables (defaults to active only)              |
| POST   | `/tickets/{id}/archive`               | Close                                                    |
| POST   | `/tickets/{id}/unarchive`             | Reopen                                                   |
| GET    | `/allocations/{date}`                 | Allocations for a day                                    |
| GET    | `/allocations/month/{year}/{month}`   | Allocations for a calendar month                         |
| POST   | `/allocations`                        | Upsert `{ticket_id, date, hours, description?}`          |
| DELETE | `/allocations/{ticket_id}/{date}`     | Remove a single allocation                               |
<!-- markdownlint-enable MD013 -->

Hours are sent and received as decimal-shaped strings (e.g. `"3.50"`) to
avoid float drift. Allocation `description` may be multi-line. Deliverables
are exposed read-only and can be set on a ticket at create time so external
automation (e.g. record-my-time) can keep billing tidy. The API
deliberately does not expose config, work packages, billing, or ticket
rename/delete - the TUI keeps full control of those.

## Multi-Machine Setup

The DB is synced between machines through a private git repo (`sync.sh` +
the `run` launcher), not iCloud or Dropbox. Cloud-storage sync of live
SQLite files corrupted the WAL on at least one occasion, so this flow
treats the local DB as a working copy and the dump in the data repo
(`timesheet.sql`) as the source of truth.

### One-time setup (per machine)

Clone the data repo to the path `sync.sh` expects:

```bash
git clone git@github.com:gravitygun/timesheets-data.git ~/.timesheets-data
```

Then seed the local DB:

```bash
./sync.sh status   # sanity check
./sync.sh pull     # writes ~/Library/Application Support/timesheets/timesheet.db
```

### Daily use

Instead of running `app.py` directly, use the launcher — it pulls before
launch and pushes on clean exit:

```bash
./run app    # TUI only
./run api    # HTTP API only
./run both   # API in background + TUI in foreground
```

If the app crashes or is SIGKILL'd, the trap doesn't fire — run
`./sync.sh push` manually before switching machines.

### Important

- **Never run the app on both machines simultaneously** — SQLite doesn't
  handle concurrent access from different machines well, and the dump-based
  sync flow has no merge story.
- Always quit the app/API before pulling. `sync.sh pull` refuses to run
  while it sees them in the process list.
- `./sync.sh status` shows whether the local DB is dirty and whether the
  data repo is ahead/behind the remote.

## Development Setup

1. Install dev dependencies:

   ```bash
   pip install -r requirements-dev.txt
   ```

2. Install markdownlint:

   ```bash
   brew install markdownlint-cli
   ```

3. Run checks before committing:

   ```bash
   ruff check *.py          # Linting
   pyright *.py             # Type checking
   pytest tests/            # Unit tests
   markdownlint *.md        # Markdown linting
   ```

See [CLAUDE.md](CLAUDE.md) for coding conventions and design decisions.
