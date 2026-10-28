# looseends-scan

Lists a project's loose ends from the markers its own records already use, each with `file:line`: questions never
answered, promises not kept, things waiting on a ruling, and work never landed. It reports only and never changes
a file, a branch or a stash.

Python 3.8 or newer, standard library only, and `git` on the path for the git part. Runs on macOS, Linux, Windows.

## Usage

```
python3 looseends-scan.py [--root DIR] [--topic TEXT]... [--markers FILE] [--no-git] [--tsv]
python3 looseends-scan.py --print-markers
python3 looseends-scan.py --self-test [--break empty|scope|skip|closed|group|topic]
```

| Option | Meaning |
|--------|---------|
| `--root DIR` | The project root. Default: the current directory. |
| `--topic TEXT` | Keep only the items that mention TEXT (any case) or sit in a file whose path does. `ADR-12` and `ADR-012` match each other. Given more than once, an item matching any of them is kept. |
| `--markers FILE` | Read the markers from FILE instead of the built-in list (see *Markers*). |
| `--no-git` | Skip the git part: uncommitted files, stashes and branches. |
| `--tsv` | One line per item, `kind<TAB>path:line<TAB>text`, for another tool to read. |
| `--print-markers` | Print the built-in markers, to copy into your own file and edit. |
| `--self-test` | Run the built-in cases, print `ok` or `FAIL` for each, then a verdict line. Exits non-zero on any failure. `--break NAME` switches one guard off, to show its case go red. |

## What it reports

Four kinds, from the files git tracks (every file under the root, outside a git checkout):

| Kind | From the built-in markers |
|------|---------------------------|
| `question` | a record's `Owed` list, an ADR's `Asks left open` or `Open questions`, a proposal's open questions |
| `promise` | the debt list (`STATUS-debt.md`), an unticked `- [ ]` item |
| `ruling` | the status file's `Waiting on` section, lines with a `veto:` call still open, an unratified ADR (`status: proposed`), a commented entry in `publish.txt` |
| `unlanded` | uncommitted or untracked files, a stash, a branch never pushed or ahead of its upstream, a remote branch not merged into the default branch |

Calls open to veto count once per file (`14 matching lines, first: …`), so one long record does not bury the rest.
A line two markers find is listed once.

## Markers

The built-in list follows the layout of a project set up with pitmaster's conventions (`STATUS.md`,
`docs/decisions/`, `docs/proposals/`, `publish.txt`). Any other project writes its own file, one marker per line,
`<type> <kind> <files> <pattern>`, and passes it with `--markers`:

| Type | Meaning |
|------|---------|
| `section` | every top-level list item under a heading that starts with the pattern, or the section's first line when it has no list; a section whose first line says Empty, None or Nothing has no items |
| `line` | every line matching the regex |
| `group` | lines matching the regex, counted as one item per file |
| `list` | every top-level list item in the file, and each section with no list (pattern `-`) |
| `skip` | files matching the glob are never read (kind and pattern `-`) |
| `closed` | a file with a line matching the regex is settled, so none of its items count (kind `-`) |

`<files>` is a glob from the root: `*` and `?` stay inside one folder, `**` as a whole segment crosses folders. A
line starting with `#` is a comment.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Nothing found. |
| 1 | Loose ends found (or a self-test case failed). |
| 2 | Invalid usage: an unknown option, a missing value, a root that is not a folder, or a bad marker line or regex. |

## Example

```
$ python3 looseends-scan/looseends-scan.py --topic ADR-012
looseends-scan: 8 loose ends in /srv/my-project (4 question, 0 promise, 4 ruling, 0 unlanded)

question
  docs/decisions/ADR-012-tools-live-in-the-shed.md:159  When the GitHub repository is created (step 6 waits on the owner's OK).
  ...
ruling
  STATUS.md:43  ADR-012 (tools in the shed, pinned copies in skills): steps 1–4 and 6 built; owed step 5 …
$ echo $?
1
```

## Limits

- It finds candidates, not verdicts: an item may already be done (a question answered in a later record). The
  `looseends` skill checks each one before it lists it; on its own, read the list that way.
- It does not fetch and uses no network, so remote branches are as current as your last fetch, and open pull
  requests are not its to see. A branch merged by squash still shows as not merged.

## Tests

`python3 looseends-scan.py --self-test` builds its own throwaway repository in a temporary directory, with a planted
loose end of every kind and a decoy for each guard, so there are no fixtures to carry. Green cases: each marker
type finds its item, a commented `publish.txt` entry is found and a prose comment is not, a file with Windows line
endings (CRLF) reads the same, and uncommitted files, a stash and a branch never pushed are found. The red arms
prove each guard, each failing by name when `--break` switches it off: `empty` (an Owed section that says Empty),
`scope` (a section ends at the next heading), `skip` (a template is never read), `closed` (an accepted proposal's
questions do not count), `group` (calls open to veto are one item per file; `none expected`, a table header and
prose are not counted) and `topic` (`--topic ADR-1` keeps ADR-001's items and not ADR-002's).

## Behaviour changes from the source

The source is `skills/looseends/scripts/looseends-scan.py` in the pitmaster plugin (its ADR-019), which now
carries a pinned copy of this file. Changes made here, and copied back byte for byte:

- Output is written as UTF-8 even where the console's default is narrower, so a project's own text cannot crash it.
- A self-test case for a file with Windows line endings (CRLF).
- The usage lines name `looseends-scan.py` without a folder, since the same bytes live in both places.
