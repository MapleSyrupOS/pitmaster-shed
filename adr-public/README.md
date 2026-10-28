# adr-public

Drafts the public copy of each architecture decision record (ADR), keeps it in step with the private ADR, and holds
it in a waiting folder until the owner approves it. It makes the mechanical cuts itself and marks every place where
the agent in the session must rewrite prose for a stranger. It never publishes anything.

Python 3.8 or newer, standard library only. macOS, Linux, Windows. For a project that keeps its ADRs private and
publishes a chosen copy of some (pitmaster does, by a publish list, `publish.txt`). pitmaster's makeadr skill carries
a pinned copy and runs `draft` whenever it makes, ratifies, amends or supersedes an ADR; its setfree skill runs
`list` and `approve` on release day.

## Usage

```
python3 adr-public.py [--root DIR] [--dir PATH] draft ADR...
python3 adr-public.py [--root DIR] [--dir PATH] check [ADR...]
python3 adr-public.py [--root DIR] [--dir PATH] list [--brief]
python3 adr-public.py [--root DIR] [--dir PATH] approve ADR... --word "..." [--by NAME]
python3 adr-public.py [--root DIR] [--dir PATH] still-true ADR --word "..." [--by NAME]
python3 adr-public.py --self-test [--break pending|leak|stale|oneway|drivers]
```

`ADR` is `14`, `014` or `ADR-014`. `--root` is the repository (default: the current folder); `--dir` is the private
decisions folder (default `docs/decisions`); `--date YYYY-MM-DD` sets the date written into History lines and the
source list (default: today; `draft`, `approve`, `still-true`).

| Command | What it does |
|---|---|
| `draft` | The mechanical cuts into `docs/decisions/public/pending/`, with a `<!-- REWRITE: ... -->` mark on every line the agent must rewrite; records the source version; appends a dated "Public copy ... drafted" line to the private ADR's History. A superseded, withdrawn or rejected ADR gets no draft: its live copy, if it has one, is queued for withdrawal and any pending draft is dropped. A new ADR that `supersedes` another queues the old live copy's withdrawal too. A pending copy already drafted from the current version is left alone (`--force` cuts again and loses the rewriting). A stale one is replaced. |
| `check` | Reports, never changes. See "What it checks". Exit 1 on any finding. |
| `list` | Every pending copy with its source state, its REWRITE marks left, and its unified diff against the live copy (`--brief` for counts only); every queued withdrawal; the live copies with no source recorded. |
| `approve` | **Only on the owner's word for that copy, never another agent's.** `--word` must quote it. Refuses a copy that `check` would fail. Moves the pending copy up into `docs/decisions/public/` (replacing the live copy of an amendment), adds its `publish.txt` overlay line (or makes a commented one live), appends the dated "approved by the owner in chat ("...")" line to the private ADR, records the approval (date and word) in the source list, and rewrites the public `INDEX.md` with `adr-index` where it is at `skills/makeadr/scripts/adr-index.py`. A queued withdrawal is approved the same way: the live copy moves to `withdrawn/`, its `publish.txt` line is commented out, and the private ADR gets a "withdrawn" line. |
| `still-true` | **Only on the owner's word.** The private ADR changed in a way that does not touch its copy (a typo): records the current version as the copy's source, with a History line, and no new draft. |

## The cuts

Mechanical, in `draft`:

- Dropped: the Context section but for its Drivers list (adr-check asks for that list wherever two or more options
  are weighed, so a copy that cut it would fail adr-check in the public tree); the Access and Open questions
  sections; the Owner and Consulted lines.
- Q-, A- and D-numbers: a parenthesis of only numbers goes ("(Q3, Q4)"), a mixed one keeps its words ("(Q5, the
  draft's rules)" becomes "(the draft's rules)"). A number left in prose is marked.
- History becomes dates and one word: "- 2026-10-21: proposed." and "- 2026-10-21: ratified by the owner." Built-line
  updates and public-copy lines are not carried.
- "the owner" becomes "the user" in the body prose. The header and History keep "the owner": there it names who ruled.
- The header keeps its fields. `status-note` loses its parenthesis (the chat quote), `proof` loses PR numbers.
- The opening line goes under the title: "This is the published version of this decision. Some internal
  details have been left out."

Marked for the agent (a `<!-- REWRITE: reason -->` at the end of the line, or `# REWRITE:` in the header): a question
number left in prose, a path a stranger could not open, a PR number, an amendment section, a header `proof` or `owed`
that names a private path. The agent rewrites each marked line, deletes the mark, runs `check`. `approve` refuses a
copy with a mark left.

## What it checks

- **Leaks.** `scripts/check-leaks.py`, run from the repository root over every pending and live copy. If the script is
  not there, `check` prints a note and skips the leak check.
- **Shape.** A pending copy must have the full header (status, owner, kind, built, supersedes, amends), the title for
  its own number, the opening line under the title, a Decision and a History section, History as dates and a word,
  and none of: a Context section with more than its Drivers list, an Access or Open questions section, an Owner or Consulted line, a Q/A/D number, "the owner" in the body (the header and History may say it).
  A live copy is held to the header, title, Decision and History only: live copies made before
  this tool keep their opening line and wording until each is next re-drafted.
- **Marks.** A REWRITE mark left in a pending copy.
- **Staleness.** The source list holds a hash of each private ADR as it was when its copy was drafted, taken over
  everything except the History section. The ADR changed since (ratified, amended, rewritten) means the copy is
  stale. A History line never makes a copy stale, so the drafted, approved and still-true lines do not. A live copy
  is flagged only while it has no pending revision; with one, the pending copy is the update.
- **One-way links.** Each private ADR's newest public-copy History line names a file that exists; no
  public copy names a private ADR file or a path outside the public tree. A path is outside the public tree when it
  starts with a private folder (`docs/bbq/`, `docs/proposals/`, `docs/audits/`, `docs/field-reports/`, `drafts/`,
  `.claude/`, a private ADR), or, where `scripts/publish.py` is beside, when the repository has the file and
  `publish.txt` does not list it.
- **Pending is never listed.** No `publish.txt` line names a pending copy. `scripts/publish.py` refuses the
  same, by its own `pending` check, and leaves a listed pending file out of the tree anyway.
- **Source list.** Every pending copy has a row and a private ADR; every pending row has a copy.

## The source list

`docs/decisions/public/pending/SOURCES.tsv`: tab-separated, one row per copy: ADR, slot (`pending`, `live` or
`withdraw`), the source hash, the date drafted, the date approved, and the owner's word.

Why there: it must not be named in `publish.txt`, and this folder is the one place that is already guaranteed never to
publish. `scripts/publish.py` refuses every path under `docs/decisions/public/pending/`, so even a mistaken broad line
cannot carry the list out. `adr-index` reads only a folder's own files, never a subfolder, so no index sees it. It
sits beside the drafts it describes, so the release review reads both in one place, and git tracks it, so every
change to it is in the history. The cost: it also holds the rows for approved copies, in a folder named for the ones
still waiting. A plain text file, so a diff of it is readable.

Live copies made before this tool have no rows. They are listed as "no source recorded" and not checked for
staleness; each gets a row the next time it is re-drafted.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Clean, or the command is done. |
| 1 | `check` found something, or `approve` refused a copy that does not check clean. |
| 2 | A usage error, an ADR it cannot use (no YAML header, no title, no History to append to), or nothing to approve. |

## Tests

`python3 adr-public.py --self-test` builds its own repository in a temporary folder (a private ADR with every kind of
cut in it, a `publish.txt`, a stand-in leak check, and, when `scripts/publish.py` is two folders up, a copy of it) and
prints one `ok` or `FAIL` line per case. It checks each cut, an idempotent second draft, a clean rewritten copy, that
a History line does not make a copy stale, the whole approve path (a refusal while a mark is left, a refusal without
`--word`, the move, the `publish.txt` line, the History line, the recorded word), an amendment re-draft beside the
live copy, a supersede queuing the old copy's withdrawal, and that withdrawal's approval.

Five of the cases are red arms. Each goes red, by name, when its guard is switched off with `--break`:

| Arm | Case | `--break` makes it go red because |
|---|---|---|
| `pending` | a `publish.txt` line names a pending copy | the check for a listed pending copy reports nothing |
| `leak` | a home path in a pending copy | the leak check is skipped |
| `stale` | the private ADR changes after its copy is drafted | staleness is not checked |
| `oneway` | a History line names a missing file; a public copy names its private ADR | the one-way checks report nothing |
| `drivers` | a Context section with a Drivers list | draft drops the list with the rest of Context, so the copy would fail adr-check's trigger rule |

The `pending` arm also runs the real `scripts/publish.py` (a copy of it, in the temporary repository) and checks that
a listed pending copy is not in the tree it plans. That guard has its own arm in `scripts/publish.py --self-test
--break pending`. The self-test stands in for `scripts/check-leaks.py` so it does not depend on the repository it sits
in; the real script runs in `check`.
