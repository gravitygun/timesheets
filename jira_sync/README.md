# jira_sync: timesheets into the client's Jira

`sync_worklogs.py` does two jobs in the client's Jira (Data Center 8.x) that
used to be done by hand:

- **Worklogs:** it makes your worklogs match a month of timesheets
  allocations, replacing typing them in from the text report.
- **Bills:** for a finalised bill, it sets the story points on that month's
  deliverable tickets in the billing project and links each one to the work
  tickets behind its points. See [Bills](#bills-story-points-and-links).

It **reconciles** rather than appends: for each allocation it looks at *your*
worklogs on that issue that day, then does whatever makes the two agree.
That means running it twice is harmless, and the months you already entered
by hand are recognised, not duplicated.

What it will never do:

- write anything unless you run `apply` and type `yes` (or pass `--yes`)
- touch anyone else's worklogs
- touch issues that aren't in the export, except to move a worklog off one,
  or days outside the export's month
- delete a worklog unless you add `--delete-extra`, or it's the old half of
  a [move](#moving-allocations-between-tickets); for bills, never remove a
  link
- touch billing tickets other than your own for the bill's month
- edit a closed billing ticket, or close one that doesn't match the bill
- create a worklog for an allocation that timesheets says you already
  entered, unless you add `--create-entered`, since that usually means your
  hand-entered worklog wasn't recognised
- act on a plan that has gone stale: if Jira changes while it waits for
  your `yes`, it stops
- carry on after Jira refuses a change: it stops at the first failure

Every `apply` writes a journal of what it changed, and `undo` reverses it.
After making changes, `apply` re-reads Jira to confirm they took.

## Contents

| File                 | What it is                                           |
| -------------------- | ---------------------------------------------------- |
| `sync_worklogs.py`   | The tool. Python 3.9+, needs `requests`              |
| `mock_jira.py`       | A pretend Jira for rehearsals. Standard library only |
| `config.example.ini` | Settings template, with comments                     |
| `requirements.txt`   | `requests`                                           |
| `README.md`          | This file                                            |

`bundle.sh` packs these up for the trip, and `__init__.py` serves the
timesheets test suite. Neither goes to HS.

## On the timesheets laptop: make the export

1. Open the allocations view (`M`) and press `R`.
2. Pick the month, choose **JSON for jira_sync**, and export. The default
   is `~/time-allocations-YYYY-MM.json`.
3. Carry that file across with the tool.

The export holds raw ticket IDs (`10400`, `FD-2587`). The tool turns them
into Jira keys (`MPIT-10400`, `MPITFD-2587`) on the HS side, so a wrong
mapping is a config fix there, not a fresh export. See
[How ticket IDs become Jira keys](#how-ticket-ids-become-jira-keys).

## Carrying it across

On the laptop, pack the tool and the exports together:

```bash
jira_sync/bundle.sh ~/time-allocations-2026-09.json ~/time-allocations-2026-10.json
```

That writes two equivalent forms to `~/jira_sync-transfer/`. Send whichever
the transfer route accepts:

- `jira_sync-VERSION.zip`
- `jira_sync-VERSION-txt/`: the same files with `.txt` appended

Both include `SHA256SUMS`. The exports hold only ticket IDs, dates, hours,
descriptions and the entered flag, with no rates or amounts.

On HS, unpack and check that nothing changed on the way:

```bash
unzip jira_sync-0.1.0.zip            # or: python3 -m zipfile -e jira_sync-0.1.0.zip .
cd jira_sync-0.1.0
sha256sum -c SHA256SUMS
```

or, for the text files, drop the `.txt` first:

```bash
for f in *.txt; do mv "$f" "${f%.txt}"; done
sha256sum -c SHA256SUMS
```

If the check fails after a text transfer, line endings were probably
converted on the way. The tool still works, but note it in case anything
else misbehaves.

For later versions, replace the files in the same folder. Your config, token
and journals live in `~/.jira-sync` and are untouched.

## Setting up on HS

### 1. Python

```bash
python3 --version        # needs 3.9 or later
```

On RHEL 8 the default `python3` may be 3.6. If so, try `python3.9` or
`python3.11` instead, and use that name in the next step.

### 2. Virtual environment

From the unpacked folder:

```bash
cd jira_sync-0.1.0
python3 -m venv .venv
. .venv/bin/activate
pip config list           # is pip already pointed at the local mirror?
pip install -r requirements.txt
```

If `pip config list` shows no mirror, name it directly:

```bash
pip install --index-url https://MIRROR/simple/ -r requirements.txt
# add --trusted-host MIRROR if its certificate isn't trusted
```

On Windows, use `py -3 -m venv .venv` and `.venv\Scripts\activate`.

### 3. Personal access token

In Jira: your avatar (top right) → **Profile** → **Personal Access Tokens**
→ **Create token**. Name it `jira-sync` and give it an expiry you can live
with. Copy it at once, because Jira shows it only once.

Store it in a file only you can read. Pasting it into `cat` keeps it out of
your shell history:

```bash
mkdir -p ~/.jira-sync && chmod 700 ~/.jira-sync
( umask 077; cat > ~/.jira-sync/token )   # paste, Enter, then Ctrl+D
```

Alternatives, in the order the tool looks for them:

1. the `JIRA_TOKEN` environment variable
2. `token =` in the config file
3. `token_file =` in the config file (the default is `~/.jira-sync/token`)

If personal access tokens are switched off on this Jira, set
`username =` instead. The tool will then ask for your password on every
run, or read it from `JIRA_PASSWORD`.

### 4. Config

```bash
cp config.example.ini ~/.jira-sync/config.ini
chmod 600 ~/.jira-sync/config.ini
vi ~/.jira-sync/config.ini
```

At minimum, set `url` to the Jira base address: what you see in the
browser before `/browse/...`, including any path such as `/jira`. Every
other setting is explained in the file.

If the server uses a certificate from an internal CA, point `verify_ssl` at
the CA bundle. On RHEL that is usually
`/etc/pki/tls/certs/ca-bundle.crt`. Don't set it to `false` except as a
one-off test.

### 5. Check the connection

```bash
python sync_worklogs.py check --issue 10668
```

This is read-only. You should see something like:

```text
Jira:       https://jira.example  (Jira, Jira 8.20.7, Server)
User:       Your Name  (name you, key JIRAUSER12345)
Auth:       personal access token from /home/you/.jira-sync/token
Days in:    Europe/London
Mapping:    12345 -> MPIT-12345, FD-1 -> MPITFD-1; skip projects: none; 0 [issue_map] entries
Writes:     adjustEstimate=auto, comments set on create, then yours, new worklogs from 09:00
Journals:   /home/you/.jira-sync/journals
Tool:       sync_worklogs 0.1.0, Python 3.9.18, requests 2.31.0
Issue:      MPIT-10668  Planning and progress meetings October 2026  [In Progress]
            12 worklogs, 4 of them yours; your latest:
            ...
OK
```

Check that `Days in` is your timezone and that the issue's summary is the
one you expect.

## First runs: a staged approach

Each stage builds confidence before the next one writes more. Note the
`Tool:` version in your notes each time, because it tells us which iteration
was running.

### Stage 0: rehearse against the mock (still on HS, no Jira involved)

This proves the install works before the real Jira is involved. In one
terminal:

```bash
python mock_jira.py --seed time-allocations-2026-10.json --scenario demo --project-map FD=MPITFD
```

In a second terminal, make a config for the mock and run the whole cycle:

```bash
cat > /tmp/mock.ini <<'EOF'
[jira]
url = http://127.0.0.1:8089
token = mock-token
[sync]
journal_dir = /tmp/mock-journals
[project_map]
FD = MPITFD
EOF
python sync_worklogs.py check -c /tmp/mock.ini
python sync_worklogs.py plan -c /tmp/mock.ini time-allocations-2026-10.json
python sync_worklogs.py apply -c /tmp/mock.ini time-allocations-2026-10.json --limit 3
python sync_worklogs.py undo -c /tmp/mock.ini /tmp/mock-journals/apply-*.jsonl
```

`--scenario demo` plants one of each kind of discrepancy (see
[How matching works](#how-matching-works)), so the plan shows them all.

### Stage 1: read-only against the real Jira, on a month you entered by hand

September was entered by hand, so a `verify` of September should come back
**IN SYNC**. Export September in timesheets as well as October, then:

```bash
python sync_worklogs.py verify time-allocations-2026-09.json
```

This tests the parts most likely to be wrong without writing anything:
the ticket mapping, which worklogs count as yours, and which day each one
falls on. Look out for:

- **`missing`**: an issue the tool can't read. Is the prefix or the
  `[project_map]` rename wrong, or does this project live somewhere else? Use
  `[project_map]`, `[issue_map]` or `skip_projects`.
- **`!! the Jira summary doesn't look like the timesheet's`**: a mapping
  error is likely.
- **`update` with `comment`**: only with `update_comments = true`. It means
  what you typed by hand differs from the timesheet description; add
  `--show-comments` to see how.
- **`update` with a time change, or `create`**: either a hand-entry slip, or a
  timezone problem. Check the worklog in Jira's Work Log tab.
- **`unexpected`**: time you logged in September on an issue not in the
  timesheet.

Don't apply anything to September. This stage is only a test.

### Stage 2: one change on the real Jira

Pick one small October allocation and make just that one change:

```bash
python sync_worklogs.py plan  time-allocations-2026-10.json --issue 10668 --date 2026-10-21
python sync_worklogs.py apply time-allocations-2026-10.json --issue 10668 --date 2026-10-21 --step
```

`--step` shows the change in full before anything is sent. Press `e` to tidy
the comment in vim, then `y`, then type `yes` at the final prompt.

Then look at it in the browser: the issue's **Work Log** tab should show the
right day, time and comment. Also check whether:

- watchers got an email (this depends on the project's notification
  scheme; the API can't switch it off), and
- the remaining estimate went down (`adjust_estimate = auto` copies the Jira
  UI's default; set it to `leave` if that's unwelcome).

Now rehearse the escape hatch on that create. Undo it, check the worklog
has gone from the Work Log tab, then apply again:

```bash
ls -t ~/.jira-sync/journals/            # newest first
python sync_worklogs.py undo ~/.jira-sync/journals/apply-YYYYMMDD-HHMMSS.jsonl
python sync_worklogs.py apply time-allocations-2026-10.json --issue 10668 --date 2026-10-21
```

Then rehearse an **update**:

1. In the browser, change that worklog's time to something else.
2. Run the same `apply` again. The plan should show an `update` back to the
   timesheet's figure.
3. Check in the Work Log tab that the time is right, and that the worklog's
   start date and time haven't moved.
4. `undo` that run's journal. Your browser edit should come back.
5. `apply` once more to leave it right.

### Stage 3: one issue, then a week

```bash
python sync_worklogs.py apply time-allocations-2026-10.json --issue 10668
python sync_worklogs.py apply time-allocations-2026-10.json --from 2026-10-05 --to 2026-10-09
```

### Stage 4: the rest of the month

```bash
python sync_worklogs.py apply time-allocations-2026-10.json
python sync_worklogs.py verify time-allocations-2026-10.json    # IN SYNC, exit 0
```

Then, back in timesheets, mark the month as entered on client: `c` in the
day view, or `POST /allocations/mark-entered` on the API.

## Each month after that

```bash
python sync_worklogs.py plan   time-allocations-YYYY-MM.json --changes-only
python sync_worklogs.py apply  time-allocations-YYYY-MM.json --changes-only --step
python sync_worklogs.py verify time-allocations-YYYY-MM.json --changes-only
```

If you change an allocation's hours in timesheets after syncing, export
again and re-run `apply`; it corrects the worklog's time in place. If you
move one to another ticket, `apply` moves the worklog too; see
[moving allocations](#moving-allocations-between-tickets). Comments are
another matter: see below.

## Reviewing changes and editing comments

Timesheet descriptions are a first draft. A comment is set when its worklog
is created, and after that it's yours: edit it in Jira and later runs leave
it alone, reconciling time only. (`update_comments = true` reverses that,
putting the timesheet text back over your edits on every run.)

To fix comments *before* they go in, `apply` has two review modes:

- **`--step`** walks through the changes one at a time. Each is shown in
  full: the issue, day, hours, and the whole comment. Then you choose:

  | Key | Does                                                       |
  | --- | ---------------------------------------------------------- |
  | `y` | send this change as shown                                  |
  | `e` | edit the comment in your editor, see it again, then decide |
  | `s` | skip it this run; a later `apply` offers it again          |
  | `a` | send this one and all the rest without asking              |
  | `q` | stop reviewing; the rest are skipped this run              |

  Nothing is sent while you review. The changes you approve go in one batch
  after the usual final `yes`.

- **`--edit`** puts every comment in one file and opens it in your editor.
  Each comment sits under its own `=== [n]` line. Delete a whole block to
  leave that worklog out of the run, or quit with `:cq` to change nothing.

The editor is `$VISUAL` or `$EDITOR` if set, else `vim`, else `vi`. To review
every time, set `review = step` (or `editor`) in `[sync]`; `--review none`
skips it for one run.

Edited comments are saved in the journals folder as you go
(`edit-YYYYMMDD-HHMMSS.txt`). If a run stops before sending, say you
answered `no` at the end, the edits aren't lost. Start the next run from
them with `--edits FILE`, with or without `--step`.

## Bills: story points and links

Each month someone else creates a ticket per deliverable in the billing
project (`PITLAI`), named like `WB-WP2d-D1-October-2026`, with projected
points that are usually way off. Given a finalised bill, the tool:

- finds **your** tickets for the bill's month by that naming convention, so
  anyone else's tickets and other months' are never touched;
- sets each one's story points (`customfield_10002`) to the bill's figure
  for its deliverable, and to `0` for deliverables not on the bill, so
  nothing unbilled is billable;
- adds an "is delivered by" link from each deliverable's ticket to every
  work ticket behind its points: the same list as the Billing view's
  drill-down, mapped to Jira keys as for worklogs (`FD-2587` →
  `MPITFD-2587`);
- then **closes** every one of those tickets, the ones set to 0 included,
  because a ticket gets paid only once it's closed. Before closing, it
  re-reads Jira and closes only the tickets that now match the bill exactly.
  Any that don't are left open and reported.

It never removes a link. A link to a ticket not on the bill line is reported
as `extra-link`, and one made the wrong way round as `reversed`, for you to
look at.

It never edits a closed ticket either. One that's closed but doesn't match
the bill shows as `closed`, and you reopen it by hand if it really needs
changing. That keeps reopening something already submitted for payment a
deliberate act. `undo` will reopen tickets it closed itself.

### Set-up

In `~/.jira-sync/config.ini`, add a `[billing]` section with at least your
initials. The other settings default to the values above; see
`config.example.ini`.

```ini
[billing]
initials = WB
```

`check` should then show the billing lines. Look for `(Story Points)` after
the field, and the link type that reads "is delivered by":

```text
Billing:    PITLAI tickets named like WB-WP2d-D1-October-2026
            points in customfield_10002 (Story Points); links read 'is delivered by' (type ...)
```

### Each month

1. In timesheets, finalise the bill, then press `J` in the Billing view.
   That writes `~/bill-YYYY-MM.json`. Only finalised bills export, because
   they're the figures that were invoiced.
2. Take it across with the month's allocations export.
3. On HS, run the same commands as for worklogs; the tool recognises a bill
   export:

```bash
python sync_worklogs.py plan   bill-2026-10.json
python sync_worklogs.py apply  bill-2026-10.json --step
python sync_worklogs.py verify bill-2026-10.json        # IN SYNC, exit 0
```

`--step` walks through each change, as for worklogs: points and links
first, then the closes. `--no-close` leaves the tickets open for a run, so
you can look them over in the browser before a later `apply` closes them.
`undo` reverses a bill run, newest change first: it reopens the tickets it
closed, puts the old points back, and removes the links it made.

The tickets are named for the month the work was **delivered**, as the
invoice is. If a bill's tickets carry another month's name, `--month YYYY-MM`
says which.

### First run

One worry is specific to links. Jira 8 can make a link the other way round
from how its REST API documents it. The tool reads every new link back,
fixes it if it's reversed, and verifies the whole bill at the end. Even so,
confirm the direction in the browser the first time.

1. **Read-only.** `plan bill-2026-10.json` writes nothing. Check that it
   finds every one of your tickets for the month, that the story points
   and closes look right (`Open -> Closed via 'Closed'`), and that nothing
   is marked `missing`.
2. **One points change.** When October's bill is finalised, run
   `apply bill-2026-10.json --step --no-close --limit 1`. Check the story
   points on that ticket in the browser.
3. **One link.** Run the same command again; this time the first change is
   a link. In the browser, the deliverable ticket should show
   **is delivered by** `MPIT-…`, and the work ticket **delivers**
   `PITLAI-…`.
4. **Undo both and redo.** Run `undo` on each journal, newest first, then
   the same `apply` again.
5. **Then the rest, still open.** Run
   `apply bill-2026-10.json --step --no-close`, and look the tickets over in
   the browser.
6. **Close them.** Run `apply bill-2026-10.json --step`, which now consists
   only of the closes, and then `verify`.

### What the plan lines mean

| Plan line    | Meaning                                                                                                                                                         | `apply` does                                            |
| ------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------- |
| `points`     | Story points differ from the bill (or 0 for a deliverable not on it)                                                                                            | Sets them                                               |
| `link`       | No "is delivered by" link to a work ticket behind the points                                                                                                    | Adds it                                                 |
| `ok`         | Story points already right                                                                                                                                      | Nothing                                                 |
| `linked`     | Link already there                                                                                                                                              | Nothing                                                 |
| `extra-link` | An "is delivered by" link to a ticket that isn't on this bill line                                                                                              | Nothing: look at it                                     |
| `reversed`   | A link of that type made the wrong way round                                                                                                                    | Nothing: fix it by hand                                 |
| `close`      | Open ticket, to be closed once everything above it checks out                                                                                                   | Closes it, after re-reading Jira                        |
| `missing`    | No ticket for a deliverable, two with the same name, an unknown ticket, or no way to close it (including a close screen that insists on more than a resolution) | Refuses to run, unless `--skip-missing`                 |
| `closed`     | Closed, but doesn't match the bill                                                                                                                              | Refuses to run, unless `--skip-missing`; never edits it |

Closing uses the workflow transition to a "done" status, preferring one that
leads to "Closed". If the workflow has none, or several, name one with
`close_transition` in `[billing]`: the name as it appears in the issue's
Workflow menu. If closing asks for a resolution, it sends `Done`; change
that with `resolution`. Other fields on the close screen are left as they
are. If one of them is required, the plan marks the ticket `missing`, so
you know before anything is written.

The search pages through all of the billing project's Tasks, and the naming
convention picks out yours. It doesn't use Jira's text search, which indexes
`WB-WP5-D4-September-2026` as one single word, so that searching for
`September` finds nothing. If paging through the whole project is slow, set
`jql` in `[billing]` to narrow it, for example
`project = PITLAI AND created >= startOfMonth(-1)`.

## Moving allocations between tickets

When you move an allocation from one ticket to another in timesheets, Jira
still has the worklog on the old issue. A fresh export then shows a worklog
to create on the new issue, and a stray one on the old issue with no
allocation behind it. Rather than create a duplicate, the tool pairs them
into a single `move`:

```text
MPIT-10836  Review and test the core platform configuration migration (10394, C1)
   [13] move       2026-10-20 Tue  45m      worklog 100068 from MPIT-10394, "Picked up C1 and..."
```

`apply` then creates the worklog on the new issue, with the old one's
comment (yours, edits and all) and start time, and only after that deletes
the old one, so the time is never missing from Jira. Both halves go in the
journal, so `undo` puts it back where it was.

A pair needs the same day and the same length, and nothing else that
could match either side. If a day has several candidates of the same length,
a matching comment settles it, or they're left alone as a `create` and an
`extra` or `unexpected`, for you to sort out. Moving an allocation to another
*day* isn't paired; that shows as a `create` and an `extra`.

The move is found even when you run with `--issue` for the new ticket only,
so narrowing a run can't turn a move into a copy.

## How ticket IDs become Jira keys

In order, first match wins:

1. `[issue_map]`: a one-off for a single ticket, e.g. `10668 = MPIT-10668`
   or `ADMIN = skip`.
2. Bare numbers get `issue_prefix`: `10400` → `MPIT-10400`.
3. IDs that already have a project keep their number, with the project
   renamed by `[project_map]`: `FD-2587` → `MPITFD-2587`. A project not
   listed there is used as it is.
4. Anything else, such as `ADMIN`, is skipped and listed as `skip`.

`skip_projects` names projects as Jira knows them, after the rename:
`MPITFD`, not `FD`. `--issue` accepts either form: `--issue FD-2587` and
`--issue MPITFD-2587` mean the same issue.

## How matching works

Each allocation is paired with **your** worklogs on the same issue on the
same day. Days are judged in your Jira profile's timezone, or `[sync]
timezone` if you set it.

It also searches Jira for every worklog of yours in the month, on any issue.
So time logged against an issue the export doesn't mention shows up too, as
`unexpected`: logged by hand on the wrong ticket, say, or left behind by an
allocation you've since moved.

| Plan line    | Meaning                                                                                     | `apply` does                               |
| ------------ | ------------------------------------------------------------------------------------------- | ------------------------------------------ |
| `create`     | No worklog of yours that day                                                                | Creates one                                |
| `update`     | One worklog, but the time differs (or the comment, with `update_comments = true`)           | Edits it in place                          |
| `ok`         | Already matches, or several worklogs that add up                                            | Nothing                                    |
| `conflict`   | Several worklogs that **don't** add up                                                      | Nothing: fix by hand                       |
| `move`       | Your worklog belongs on another issue now: see [moves](#moving-allocations-between-tickets) | Creates it there, then deletes the old one |
| `extra`      | Your worklog on a day with no allocation for that issue                                     | Nothing, unless `--delete-extra`           |
| `delete`     | An `extra` when `--delete-extra` is given                                                   | Deletes it                                 |
| `unexpected` | Your time this month on an issue the export doesn't have                                    | Nothing: check by hand                     |
| `missing`    | The issue couldn't be read (not found, no permission)                                       | Refuses to run, unless `--skip-missing`    |
| `skip`       | Ticket ID isn't a Jira key, or is mapped to skip                                            | Nothing                                    |

With `update_comments = true`, comments are compared ignoring trailing
whitespace and line-ending style.
New worklogs start at `day_start` (09:00) and follow on from each other in
the day's order. That's cosmetic, since start times are never used for
matching.

Write lines are numbered `[1]`, `[2]`, … in the order `apply` will make
them, so `--limit N` makes the first N.

## Command reference

```text
sync_worklogs.py check  [--issue ID]
sync_worklogs.py plan   EXPORT [scope] [--limit N] [--month YYYY-MM] [--no-close]
sync_worklogs.py apply  EXPORT [scope] [--limit N] [--yes] [--skip-missing]
                        [--create-entered] [--step | --edit | --review MODE]
                        [--edits FILE] [--month YYYY-MM] [--no-close]
sync_worklogs.py verify EXPORT [scope] [--month YYYY-MM] [--no-close]
sync_worklogs.py undo   JOURNAL [--yes] [--force]

scope:  --issue ID          a ticket ID (10400) or key (MPIT-10400); repeatable
        --date YYYY-MM-DD   repeatable
        --from / --to YYYY-MM-DD
        --unentered-only    leave alone allocations timesheets marks as entered
        --delete-extra      delete your worklogs that have no allocation
        --changes-only      hide lines already in sync
        --show-comments     new comments in full, and comment diffs

all:    -c/--config PATH  -v/--verbose  --log-file PATH
```

Exit codes:

- `0`: fine
- `1`: `verify` found differences, or an `apply` didn't verify
- `2`: the config, export or arguments need fixing
- `3`: Jira couldn't be reached, or refused something

`undo` won't touch a worklog that was edited after the run (add `--force` to
undo it anyway). It won't recreate a deleted worklog that is already back,
so running it twice is harmless. It refuses a journal written against a
different Jira URL or by a different user.

## Troubleshooting

Add `-v` to see every request and its HTTP status, or `--log-file
run.log` to keep a full record. Neither ever records the token.

| Symptom                                     | Likely cause and fix                                                                               |
| ------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `TLS failure` / `CERTIFICATE_VERIFY_FAILED` | Internal CA: set `verify_ssl` to the CA bundle path                                                |
| `cannot reach ... timed out`                | Wrong URL, or a proxy is in the way: check `echo $https_proxy` and add the Jira host to `no_proxy` |
| `returned text/html ... not JSON`           | An SSO page or proxy answered instead of Jira; check `url`                                         |
| `redirected to ...`                         | `url` is wrong (http vs https, or a missing `/jira` path)                                          |
| `401 ... authentication failed`             | Token mistyped, expired or revoked: create a new one                                               |
| `wants a CAPTCHA`                           | Too many failed logins: log in once in a browser                                                   |
| `403` on a worklog                          | No *Work On Issues* permission in that project, or the worklog isn't yours                         |
| `missing` for a whole project               | Wrong `issue_prefix` or `[project_map]`, or add the project to `skip_projects`                     |
| Day off by one                              | Set `[sync] timezone = Europe/London` explicitly                                                   |
| `could not search for your worklogs`        | Only the `unexpected` check is skipped; everything else ran                                        |

When something goes wrong and you're bringing details back from HS, note:

- the `Tool:` version
- the command you ran
- the exit code (`echo $?`)
- the error lines, with any `Jira says:` text
