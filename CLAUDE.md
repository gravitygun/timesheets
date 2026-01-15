# Timesheet TUI App

A Textual-based terminal UI application for tracking work hours, converted from an Excel spreadsheet.

## Running the App

```bash
source venv/bin/activate
python app.py
```

## Multi-Machine Setup

The database location can be overridden with the `TIMESHEET_DB` environment variable.
This allows syncing via cloud storage (iCloud, Dropbox, etc.).

### Personal Machine Setup

1. Move existing database to iCloud (or other sync folder):

   ```bash
   mkdir -p ~/Library/Mobile\ Documents/com~apple~CloudDocs/timesheets
   mv data/timesheet.db ~/Library/Mobile\ Documents/com~apple~CloudDocs/timesheets/
   ```

2. Add to shell profile (`~/.zshrc` or `~/.bashrc`):

   ```bash
   export TIMESHEET_DB="$HOME/Library/Mobile Documents/com~apple~CloudDocs/timesheets/timesheet.db"
   ```

### Work Laptop Setup

1. Clone the repository:

   ```bash
   git clone <repo-url> ~/Projects/timesheets
   cd ~/Projects/timesheets
   ```

2. Create virtual environment and install dependencies:

   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install textual holidays
   ```

3. Point to synced database (add to `~/.zshrc`):

   ```bash
   export TIMESHEET_DB="$HOME/Library/Mobile Documents/com~apple~CloudDocs/timesheets/timesheet.db"
   ```

4. Run the app:

   ```bash
   source venv/bin/activate
   python app.py
   ```

### Important Notes

- **Never run the app on both machines simultaneously** - SQLite doesn't handle
  concurrent access from different machines well
- Wait for cloud sync to complete before switching machines
- The app creates the database automatically if it doesn't exist

## Key Design Decisions

### Week Structure
- **Weeks start on Saturday** (for billing purposes, not Monday)
- Billing boundary is calendar month working days
- Boundary weeks (e.g., Dec 27 - Jan 2) appear in both adjacent months' views
- Days from adjacent months are shown in parentheses, e.g., `(Dec 27)`

### Navigation Behavior
- LEFT/RIGHT moves through weeks within current month
- Going past week 1 (left) or last week (right) switches to adjacent month
- Months only change at explicit boundaries - no auto-switching based on weekday majority
- This allows editing boundary week days in context of either month

### Adjust Types
- `P` = Public Holiday
- `L` = Leave
- `S` = Sick
- `T` = Training
- Adjustment hours require a type (validation enforced)

### Display
- Weekend rows (Sat/Sun) are dimmed
- Privacy mode: earnings hidden by default, toggle with `$` key
- Currency is GBP, hourly rate is £97
- Standard working day is 7.5 hours (configurable in Config)

### Max Hours Calculation
```
max_hours = (weekdays × 7.5h) - public_holiday_adjustment_hours
```
Only `P` type adjustments reduce max hours.

## Key Bindings

| Key | Action |
|-----|--------|
| `q` | Quit |
| `←/→` | Navigate weeks |
| `↑/↓` | Navigate rows |
| `m` | Month picker (TODO) |
| `t` | Jump to today |
| `$` | Toggle earnings display |
| `e` or `Enter` | Edit selected day |
| `h` | Populate UK bank holidays |
| `L` | Quick add Leave (7.5h) |
| `S` | Quick add Sick (7.5h) |
| `T` | Quick add Training (7.5h) |

Quick adjust shortcuts (L/S/T) will prompt for confirmation if the day already has data.

## Files

- `app.py` - Main TUI application using Textual framework
- `storage.py` - SQLite database layer (data in `data/timesheet.db`)
- `models.py` - TimeEntry and Config dataclasses
- `import_data.py` - One-time import from Excel JSON (already run)
- `tools/extract.py` - Excel extraction utility

## Pending Work

- Month picker modal (`action_select_month` is TODO)
- Reports feature for invoicing

## Code Quality

Run these checks before committing:

```bash
ruff check *.py          # Linting
pyright *.py             # Type checking
```

Both must pass with zero errors.

### Markdown

- Markdown files must pass `markdownlint`

## Commit Convention

Use [Conventional Commits](https://www.conventionalcommits.org/):

- `feat:` new feature
- `fix:` bug fix
- `refactor:` code change that neither fixes a bug nor adds a feature
- `docs:` documentation only
- `style:` formatting, missing semicolons, etc.
- `chore:` maintenance tasks
