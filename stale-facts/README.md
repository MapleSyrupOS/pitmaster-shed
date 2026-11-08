# stale-facts

Checks the facts a project's docs state by hand (how many skills it ships, the repository it installs from) against
the project itself, and fails, naming the file and line, where a doc has gone stale. A facts file says what each
fact is, how to work it out from the files, and which docs state it. With `--fix` it rewrites only the stale
value's own words; it never edits a decision record.

Python 3.8 or newer, standard library only. Runs on macOS, Linux, Windows.

## Usage

```
python3 stale-facts.py [--root DIR] [--facts FILE] [--fix]
python3 stale-facts.py --self-test
```

| Option | Meaning |
|--------|---------|
| `--root DIR` | The project root. The facts file and every doc resolve against it. Default: the current directory. |
| `--facts FILE` | The facts file, relative to the root. Wins over `facts_file` in `pitmaster.md`. Default: `docs/facts.txt`. |
| `--fix` | Rewrite each stale value in the docs that are not ruled, then report what changed. |
| `--self-test` | Run the built-in cases, print `PASS` or `FAIL` for each, then a verdict line. Exits non-zero on any failure. |

Exit 0: every fact current (or every finding fixed); 1: findings; 2: invalid usage, facts file or unreadable input.

## The facts file

One line each; blank lines and lines starting with `#` are ignored.

```
# what a fact is, and how to work it out
fact skills = dirs skills SKILL.md
fact repo   = json .claude-plugin/plugin.json repository github\.com/(.+)$

# where docs state it
state skills README.md There are {number} [skills]
state repo README.md marketplace add {text}
state owner ruled docs/decisions/ADR-*.md#Access `{word}/pitmaster`
mention skills "AGENTS.md#Where things live"
```

| Source | The value |
|--------|-----------|
| `dirs <folder> [<marker>]` | The folders in `<folder>` (only those holding the file `<marker>`, if given): a list. |
| `files <glob>` | The files matching `<glob>`: a list. |
| `json <file> <key.path> [<regex>]` | A JSON value; with a regex, its first group. |
| `text <value>` | A fixed value. |

A **`state`** line names a doc and a pattern: literal text holding one slot.

| Slot | Matches |
|------|---------|
| `{number}` | digits, or a number word up to ninety-nine (`seventeen`, `Forty-two`) |
| `{word}` | letters, digits, `.`, `_`, `-` |
| `{text}` | a word that may also hold `/` |

A list fact compares its count with `{number}`; a value fact compares its text. Whitespace in a pattern matches any
run of whitespace, so a sentence wrapped across lines still matches. **Every `state` pattern must match at least
once**, so a sentence that was reworded is reported rather than silently skipped.

A **`mention`** line needs every item of a list fact named in the doc as a whole word.

A doc may be a glob, and may name a section: `#Section` reads only that section (the heading whose text equals it,
at any level, down to the next heading at the same or a higher level); `!Section` reads everything else. Quote the
doc in double quotes when the section name has spaces. Headings inside fenced code are not sections.

## Ruled records

A `ruled` doc is a decision record whose ruled text is never edited. A stale value there is reported, never
fixed, and it passes once the same file holds a section whose heading contains `Amendment` and whose text states
the true value. The old line stays as history.

## What it does not do

It checks only the facts the file names, in the docs the file lists: it cannot notice a fact nobody wrote down,
and it does not judge whether a plan has been overtaken. `--fix` leaves `mention` findings to be fixed by hand.

## pre-commit

```yaml
- repo: https://github.com/MapleSyrupOS/pitmaster-shed
  rev: <tag>
  hooks:
    - id: stale-facts
```
