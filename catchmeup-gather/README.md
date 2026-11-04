# catchmeup-gather

Lists a project's changes since a date, or since a person's last "caught up" stamp: commits on the default branch,
pull requests, dated log headings and the records those commits touched, each with its proof (a hash, a number, a
`file:line`). It gathers facts only; the explaining is someone else's job. It never fetches, commits or summarises.

Python 3.8 or newer, standard library only, and `git` on the path. `gh` is used for pull requests when present.
Runs on macOS, Linux, Windows.

## Usage

```
python3 catchmeup-gather.py gather [--root DIR] [--since DATE] [--until DATE] [--person NAME]
                                   [--store home|commit] [--log FILE]... [--branch NAME] [--no-gh] [--json]
python3 catchmeup-gather.py stamp --show [--root DIR] [--person NAME] [--store home|commit]
python3 catchmeup-gather.py stamp --set [DATE|now] [--root DIR] [--person NAME] [--store home|commit]
python3 catchmeup-gather.py --self-test [--break window|firstparent|sharedkey|logdate|logref|localdate|nostamp|store|until]
```

| Option | Meaning |
|--------|---------|
| `--root DIR` | The project. Default: the current directory; git finds its top level. |
| `--since DATE` | The start, inclusive. Wins over the stamp. |
| `--until DATE` | The end, inclusive (a bare date means the end of that local day). Default: now. |
| `--person NAME` | Whose stamp to read or write. Default: "the user". |
| `--store home\|commit` | Where stamps live (see *Stamps*). |
| `--log FILE` | A log file to read headings from, relative to the root; give it more than once for more. Default: `docs/STATUS-log.md` when it exists. |
| `--branch NAME` | The branch to list. Default: `origin/HEAD`'s target, else `main`, else `master`; `origin/<branch>` is preferred when present. |
| `--no-gh` | Skip pull requests. |
| `--json` | Print one object: `start`, `start_source`, `branch`, `commits`, `prs` (null when unread), `log_entries`, `records`. |
| `--self-test` | Run the built-in cases. `--break NAME` switches one guard off, to show its case go red. |

Dates are `YYYY-MM-DD` or ISO date-times. A bare date, or a date-time without a zone, is the local time zone, since
"since the 22nd" is the reader's own day. Stamps are always written in UTC.

## What it lists

| Section | What |
|---------|------|
| commits | first-parent commits on the branch, oldest first: short hash, committer date, subject |
| pull requests | merged, or open and updated, in the window: number, date, title, draft where it applies |
| log headings | lines starting `##` or `###` that hold a date, with `file:line`; only the first date counts, and future headings are left out |
| records | `docs/` paths the listed commits touched, once each, with A/M/D and hashes |

A log is read as the branch has it, so a stale checkout cannot hide the window's entries; a log the branch does not
have is read from the working tree. Renames show as a deletion plus an addition; merges are compared with their
first parent.

With no `--since` and no stamp, it prints `no start point: ask the user for a date` and exits 3. It never reads all
history by default.

## Stamps

| Store | Where |
|-------|-------|
| `home` | `$PITMASTER_HOME/catchmeup.json` (default `~/.pitmaster/catchmeup.json`), keyed by project, then person. The project is the normalised origin URL; with no origin, the main worktree's path, so linked worktrees share stamps. A write keeps other keys and goes through a temporary file. |
| `commit` | `docs/catchup/stamps.md`, headed `# catchmeup stamps`, one `- <person>: <UTC time>` line each. The tool writes the file; committing it is yours. |

`stamp --show` prints the stamp or `none`. `stamp --set` with no value, or `now`, writes the current time; a date
writes local midnight that day. Only `stamp --set` creates folders or writes anything.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Done (or every self-test case passed). |
| 1 | A self-test case failed. |
| 2 | Invalid usage, or a read or write error. |
| 3 | No start point: no `--since` and no stamp. |

## Limits

- No network except `gh`. Refs are as current as your last fetch. A missing or failing `gh` prints
  `gh: not read (<reason>)` and gathering carries on; each of its two queries reads at most 200 pull requests.
- A log heading has no time of day, so headings are compared by the start's local calendar day.

## Tests

`python3 catchmeup-gather.py --self-test` makes a throwaway git repository and worktree, a temporary
`PITMASTER_HOME` and its own git identity, plants dated commits and log headings, and runs with `--no-gh`, so there
are no fixtures to carry. Each guard fails by name when `--break` switches it off: `window` (a commit a day before
the start is not listed), `firstparent` (a merged side-branch commit is listed only through its merge),
`sharedkey` (worktrees with no origin share a home stamp), `logdate` (a heading before the start is left out, one
inside is kept), `logref` (a log is read from the branch, not the working tree), `localdate` (a bare date is local
midnight; checked where `time.tzset` exists), `nostamp` (no start exits 3), `store` (each store writes only its own
file) and `until` (the end bound is kept).

## Source

The source is `skills/catchmeup/scripts/catchmeup-gather.py` in the pitmaster plugin (its ADR-028), which now
carries a pinned copy of this file. It moved here unchanged.
