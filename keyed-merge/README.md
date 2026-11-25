# keyed-merge

Merges the checked-in tables that many branches add to (a census, an allowlist, a ledger, a list of test cases)
**by key instead of by line**. Two branches that each add a row merge cleanly; two branches that change the same
row two ways stop with a conflict on just that row. It never writes a row twice and never drops one silently, which
git's own `merge=union` does when both sides change one row. `tablemerge` is its second name: the same tool.

Python 3.8 or newer, standard library only; `git` on the path for the driver, `preview`, `install`, `trust` and
`regen`. Runs on macOS, Linux and Windows.

## Set it up in a project

1. Name each shared table in `.keyed-merge` at the repository root (see *The config*).
2. Name the same files in `.gitattributes`: `docs/census.tsv merge=keyed-merge`.
3. In each clone, once: `python3 keyed-merge/keyed-merge.py install`. A merge driver lives in a clone's own git
   settings, not in the repository, so a fresh clone needs this step (worktrees of one clone share it). `install`
   copies the script into the clone's git folder (`.git/keyed-merge/keyed-merge.py`) and points the driver there, so
   no branch can change the code a merge runs; run it again to take a newer copy.

GitHub's merge button and its "mergeable" badge never run a custom driver, so a pull request there can still show a
conflict that a local merge resolves.

## Usage

```
python3 keyed-merge.py driver BASE OURS THEIRS PATH [--marker-size N]     git calls this; install sets it up
python3 keyed-merge.py merge BASE OURS THEIRS --path PATH [-o OUT] [--format F --key K --order O]
python3 keyed-merge.py preview REV_A REV_B [PATH ...]     merge the tables both revisions changed; writes nothing
python3 keyed-merge.py check [PATH ...]                   (or --check) each table parses, no duplicate key, canonical
python3 keyed-merge.py regen [PATH ...]                   regenerate generated tables after a merge
python3 keyed-merge.py install [--trust-commands]         register merge=keyed-merge and merge=tablemerge here
python3 keyed-merge.py trust [--all | COMMAND]            list or approve regen= commands for this clone
python3 keyed-merge.py lint [--fail] [PATH ...]           flag bare counts a keyed list would replace
python3 keyed-merge.py --self-test
```

Every command takes `--json` (the same report, for agents) and `--root DIR`.

## The merge rule

For each key:

| What happened | Result |
|---|---|
| added, changed or deleted on one side only | that side's version |
| the same change on both sides | taken once |
| changed two different ways, or added on both sides differently | **conflict** |
| deleted on one side and changed on the other | **conflict** |
| a duplicate key in any of the three inputs | **refused** (bad input) |

Row order comes from the config, never from either side. A clean row keeps its own text, byte for byte. Before a
merge is called clean, the result is read back in its format and must hold exactly the rows decided; if not (say one
side emptied the file, header and all), it is a whole-file conflict instead. Every clean row is written;
each conflicted row sits between git's usual `<<<<<<< ours` / `=======` / `>>>>>>> theirs` markers (an empty side
means that side deleted it; for JSON, the whole record, with its commas placed so that keeping either side leaves
valid JSON). When the text outside the rows (a header, the comments at
the top, the prose around a Markdown table) changed on both sides, or a header changed at all, the file falls back
to git's whole-file line conflict and the report says why.

A clean merge is a prediction: two branches can each be right alone and wrong together. When both sides changed a
table the report says so, so the checks that read it can be run.

## The config

`.keyed-merge`, one table a line. A line starting with `#`, or `#` after a space before any `regen=`, is a comment.
Fields split on spaces; `regen=` takes the rest of the line, `#` included.

```
# path or glob        format       key            order          generated
docs/census.tsv       tsv          key=part,site  order=sorted
tools/allow.list      lines
data/cases.json       json-array   key=id         order=added
logs/runs.jsonl       jsonl        key=run
docs/ledger.md        md-table     key=Date       order=added
gen/sites.json        json-object                                regen=python3 tools/gen-sites.py
lint fail
lint also tools/*.manifest
```

| Format | What a row is | The key |
|---|---|---|
| `lines` | one line (comments and blank lines only at the top) | the line |
| `tsv`, `csv` | one record under a header row (a CSV field may be quoted across lines) | `key=` columns; default the first |
| `md-table` | one row of the file's one Markdown table; text before and after it is kept | `key=` columns; default the first |
| `jsonl` | one JSON object a line | `key=` fields, required |
| `json-array` | one object in a JSON list | `key=` fields, required |
| `json-object` | one top-level name and its value | the name |

`order=sorted` (the default) sorts rows by key; `order=added` keeps the base's rows in the base's order, then our new
rows, then theirs. Globs: `*` and `?` stay in one folder, `**` crosses folders, a trailing `/` means everything below.

JSON tables are written with the file's own indent, one entry a line-start, each entry's own text kept as written;
`check` says when a file is not already in that form, so the first merge does not reformat it. JSON numbers are
compared and written as their exact text (`1.50` stays `1.50`; two different large numbers never look equal), NaN and
Infinity are refused, and the number `1` and the string `"1"` are different keys. Line endings (LF or CRLF) are kept;
only LF ends a line, so a Unicode line separator inside a field stays in its field. A Markdown table row may leave
out its outer pipes; the table ends at the first blank line.

## Generated tables

A table a script writes should be regenerated, not merged. Git calls a merge driver before the merged files exist,
so the driver cannot run the script on the merged inputs. When only one side changed a table marked `regen=`, that
side is taken. When both sides changed it, the driver (and `merge` and `preview`) keep our side, stop on that file,
and say to run `regen` once the rest of the merge is in place. `regen` runs the command from the repository root, then checks the file it wrote parses.

It runs only a command this clone approved, word for word: `trust` lists and approves them (`install
--trust-commands` approves all at once), recorded as `keyed-merge.trust` in the clone's git settings. A branch that
changes the command in `.keyed-merge` cannot make it run: `regen` refuses with exit 3 and says how to approve it.

## The lint

`lint` flags bare counts (`CASES=412`, `"total": 9`) in the configured tables, the files beside them and any `lint
also` files, and suggests a keyed list: a count cannot be merged (base 726, ours 727, theirs 728 has no answer), but a
list of the things counted can, and the reader counts it. A line holding `keyed-merge: ok` is skipped. It reports
only (exit 0) unless `--fail` or the config's `lint fail`.

## Exit codes

| Exit | Meaning |
|---|---|
| 0 | clean |
| 1 | conflicts, a generated table to regenerate, or a `check` or `lint --fail` finding |
| 2 | bad input: a duplicate key, a file that does not parse, a bad config line |
| 3 | refused: a `regen` command this clone has not approved |
| 4 | could not run: bad arguments, no config line for the file, no git where git is needed |

## What it never does

It writes no file but the one being merged (and, for `install` and `trust`, this clone's git folder and settings;
a `regen` command writes what it writes). It never drops or invents a row silently, never runs an unapproved command, never
commits, pushes or changes branches, and never reaches the network.

## Self-test

`--self-test` runs every merge arm for each format it applies to, checking whole rows (keys and values): a clean
union; a value changed on one side; one key changed two ways, which git's `merge=union` writes twice; delete against
edit; a duplicate key; rows only reordered; the same change on both sides; a delete on one side; `order=added`; a
side that empties the file; `check`. Then the header, lint, config, line-ending, Markdown, CSV, JSON-number, typed-key
and Unicode arms, then real merges, a rebase, a cherry-pick, the installed copy, a generated table and multi-line
trust through the installed driver in a scratch repository. Then it runs each arm again against a deliberately
broken copy of the tool (the break that arm exists to catch) and requires it to fail, in every format it applies
to. 152 cases, 43 of them red arms.

`tablemerge.py` is a six-line launcher for `keyed-merge.py`, so `python3 tablemerge.py ...` works too. It is the
one exception to the shed's one-script-a-folder layout, kept because the second name is part of the tool's
decision; the self-test and the pins use `keyed-merge.py`.

Tested by hand on git 2.54.0: `git merge-tree --write-tree` also goes through the driver.
