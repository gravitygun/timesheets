# Timesheet TUI App - Developer Notes

See [README.md](README.md) for installation and user guide.

## Key Design Decisions

### Week Structure

- **Weeks start on Saturday** (for billing purposes, not Monday)
- Billing boundary is calendar month working days
- Boundary weeks (e.g., Dec 27 - Jan 2) appear in both adjacent months' views
- Days from adjacent months are shown in parentheses, e.g., `(Dec 27)`

### Navigation Behaviour

- LEFT/RIGHT moves through weeks within current month
- Going past week 1 (left) or last week (right) switches to adjacent month
- Months only change at explicit boundaries - no auto-switching based
  on weekday majority
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

```text
max_hours = (weekdays × 7.5h) - public_holiday_adjustment_hours
```

Only `P` type adjustments reduce max hours.

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

### Language

- Use British English spellings (e.g., colour, behaviour, initialise)
- Exception: code identifiers follow library/API conventions (e.g., `color`
  in CSS)

### Checks

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

**Note:** Do not add `Co-Authored-By` or other AI credits to commit messages.
