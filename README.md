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

5. Configure the database location (recommended: use a cloud-synced folder):

   ```bash
   # Create the directory in iCloud Drive
   mkdir -p ~/Library/Mobile\ Documents/com~apple~CloudDocs/timesheets

   # Add to your shell profile (~/.zshrc)
   DB_PATH='$HOME/Library/Mobile Documents/com~apple~CloudDocs/timesheets'
   echo "export TIMESHEET_DB=\"$DB_PATH/timesheet.db\"" >> ~/.zshrc

   # Reload your shell
   source ~/.zshrc
   ```

   If `TIMESHEET_DB` is not set, the app uses `data/timesheet.db` in the
   project directory.

## Running the App

```bash
source .venv/bin/activate
python app.py
```

To check database location and sync status:

```bash
python app.py --db-info
```

## User Guide

### Views

The app has five views, accessible via keyboard shortcuts:

| Key | View        | Description                           |
| --- | ----------- | ------------------------------------- |
| `w` | Week        | Daily time entries for one week       |
| `m` | Month       | Weekly summaries for one month        |
| `y` | Year        | Monthly summaries for fiscal year     |
| `M` | Allocations | Ticket allocations matrix for a month |

Pressing `Enter` on a day in week view opens **Day view**, showing ticket
allocations for that day.

### Navigation

| Key       | Action                                                   |
| --------- | -------------------------------------------------------- |
| `Left`    | Previous week/month/year (depending on view)             |
| `Right`   | Next week/month/year (depending on view)                 |
| `Up/Down` | Navigate rows                                            |
| `Enter`   | Drill down (year → month → week → day)                   |
| `Esc`     | Return to week view (from day view)                      |
| `t`       | Jump to today                                            |

### Editing

| Key         | Action                                  |
| ----------- | --------------------------------------- |
| `e`/`Enter` | Edit selected day                       |
| `L`         | Quick add Leave (7.5h)                  |
| `S`         | Quick add Sick (7.5h)                   |
| `T`         | Quick add Training (7.5h)               |
| `h`         | Populate UK bank holidays (year view)   |

Quick adjust shortcuts (`L`/`S`/`T`) prompt for confirmation if the day
already has data.

### Ticket Tracking

Worked hours can be allocated to tickets (e.g., JIRA IDs) for billing reports.

| Key | Action (context)                          |
| --- | ----------------------------------------- |
| `K` | Open ticket management screen             |
| `M` | Open allocations report (tickets × days)  |
| `a` | Add allocation (day view)                 |
| `d` | Delete allocation (day view)              |

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

| Key | Action                  |
| --- | ----------------------- |
| `$` | Toggle earnings display |
| `?` | Show keyboard shortcuts |
| `q` | Quit                    |

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

## Multi-Machine Setup

The app supports syncing via cloud storage (iCloud, Dropbox, etc.) using the
`TIMESHEET_DB` environment variable.

### Important

- **Never run the app on both machines simultaneously** - SQLite doesn't
  handle concurrent access from different machines well
- Wait for cloud sync to complete before switching machines
  (use `--db-info` to verify timestamps)
- The app creates the database automatically if it doesn't exist

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
