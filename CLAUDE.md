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

### Billing & Points Model

This is the rule every billing/points figure in the UI must obey. It was
reworked on 2026-06-25 after the on-screen numbers disagreed with each
other and with the Billing view; read this before touching any points or
£ display.

**A ticket bills in the bill it is finalised into, as a whole.** Its
points come from *all* its hours, regardless of which months those hours
were booked in. When you finalise a bill the ticket is stamped
`billed_year`/`billed_month` and a `bill_lines` snapshot is written.

The billed month is chosen at finalise time (editable in `FinaliseBillScreen`)
and **defaults to the month the work was actually delivered** — the latest
allocation date across the pending bill (`get_current_bill_delivery_month`),
*not* today. This stops a bill finalised on the 1st/2nd of the next month
from being stamped into that month. The invoice's "services delivered" line
is derived the same way (`get_current_bill_delivery_month` /
`get_finalised_bill_delivery_month`), so even a mis-stamped bill invoices
against the right month.

So "what do I bill for month X?" has exactly one answer, and there is one
helper for it:

```python
storage.get_month_bill_points(year, month, hours_per_point,
                              point_rate, vat_rate, contract_start)
    -> (points: int, is_finalised: bool)
```

- **Finalised month** → its snapshot total (`get_bill_lines`, or a
  reconstruction via `get_finalised_bill_summary`). Label it **"Billed"**.
- **Unfinalised month** → the current pending bill (`get_current_bill_summary`
  = all closed-but-unbilled work). Label it **"To bill"**.

Rounding is **per deliverable** (ceiling), because the deliverable is the
invoiced unit. This figure is stable as you navigate months and always
equals the Billing view for the same period.

**Do NOT reintroduce these — they were deliberately deleted as confusing
and wrong, and they are what caused the disagreeing numbers:**

- month-scoped-by-booking-date points ("points from hours *booked* in this
  calendar month") — was `get_monthly_points_breakdown`
- contract-to-date status totals + an "inc N from previous months"
  parenthetical — was `get_points_by_status`
- a separate "Speculative" (open-ticket) figure in the allocations bar or
  the month/year earnings panels

The **per-row "Pts" column** in the allocations table is the one exception:
it intentionally shows each ticket's *lifetime* points with *per-ticket*
rounding — a "how big is this ticket" gauge, NOT a bill total, so it will
not (and should not) sum to the bill.

## Files

- `app.py` - Main TUI application using Textual framework
- `storage.py` - SQLite database layer. Default DB path is
  `~/Library/Application Support/timesheets/timesheet.db` (override with
  `TIMESHEET_DB`). The same path is hard-coded in `sync.sh` — if you
  change one, change the other. WAL mode is enabled so the HTTP API can
  read while the TUI is open.
- `models.py` - TimeEntry and Config dataclasses
- `api.py` - Thin FastAPI HTTP wrapper around `storage.py`. No business
  logic of its own; new behaviour belongs in `storage.py` and the TUI
  picks it up for free. See README for endpoint list.
- `run_api.sh` - Launcher for the API (binds `127.0.0.1:8765` by default)
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

**Always run commands inside the venv.** Either activate it first or prefix commands:

```bash
ruff check *.py          # Linting
pyright *.py             # Type checking
pytest tests/            # Unit tests
```

All checks must pass with zero errors.

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
