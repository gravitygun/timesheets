# jira_sync: timesheets allocations into Jira worklogs

`sync_worklogs.py` makes your worklogs in the client's Jira (Data Center 8.x)
match a month of timesheets allocations. It replaces typing them in by hand
from the text report.

It **reconciles** rather than appends: for each allocation it looks at *your*
worklogs on that issue that day, then does whatever makes the two agree.
That means running it twice is harmless, and the months you already entered
by hand are recognised, not duplicated.

What it will never do:

- write anything unless you run `apply` and type `yes` (or pass `--yes`)
- touch anyone else's worklogs
- touch issues that aren't in the export, or days outside the export's month
- delete anything unless you add `--delete-extra`
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
again and re-run `apply`; it corrects the worklog's time in place. Comments
are another matter: see below.

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

| Plan line    | Meaning                                                                           | `apply` does                            |
| ------------ | --------------------------------------------------------------------------------- | --------------------------------------- |
| `create`     | No worklog of yours that day                                                      | Creates one                             |
| `update`     | One worklog, but the time differs (or the comment, with `update_comments = true`) | Edits it in place                       |
| `ok`         | Already matches, or several worklogs that add up                                  | Nothing                                 |
| `conflict`   | Several worklogs that **don't** add up                                            | Nothing: fix by hand                    |
| `extra`      | Your worklog on a day with no allocation for that issue                           | Nothing, unless `--delete-extra`        |
| `delete`     | An `extra` when `--delete-extra` is given                                         | Deletes it                              |
| `unexpected` | Your time this month on an issue the export doesn't have                          | Nothing: check by hand                  |
| `missing`    | The issue couldn't be read (not found, no permission)                             | Refuses to run, unless `--skip-missing` |
| `skip`       | Ticket ID isn't a Jira key, or is mapped to skip                                  | Nothing                                 |

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
sync_worklogs.py plan   EXPORT [scope] [--limit N]
sync_worklogs.py apply  EXPORT [scope] [--limit N] [--yes] [--skip-missing]
                        [--create-entered] [--step | --edit | --review MODE]
                        [--edits FILE]
sync_worklogs.py verify EXPORT [scope]
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
