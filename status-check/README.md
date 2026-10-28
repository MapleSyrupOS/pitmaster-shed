# status-check

Checks a project's status file (`STATUS.md` by default) two ways: it must not grow past a line ceiling, and every
bullet under the sections you care about must cite at least one backticked file that exists. It reports only and
never changes a file.

Python 3.8 or newer, standard library only. Runs on macOS, Linux, Windows.

## Usage

```
python3 status-check.py [--root DIR] [--ceiling N] [--sections LIST]
python3 status-check.py --self-test
```

| Option | Meaning |
|--------|---------|
| `--root DIR` | The project root. The status file and every cited path resolve against it. Default: the current directory. |
| `--ceiling N` | The most lines the status file may have (a positive whole number). Wins over `status_ceiling` in `pitmaster.md`. Default: 300. |
| `--sections LIST` | Comma-separated section names whose bullets must cite a file, for example `"Goal, Landed, In progress, Waiting on owner"`. Wins over `status_sections` in `pitmaster.md`. Default: `Now, Next, Known debt`. |
| `--self-test` | Run the built-in cases, print `PASS` or `FAIL` for each, then a verdict line. Exits non-zero on any failure. |

## Config

Flags always work alone. When the root holds a `pitmaster.md`, it is read too, and a flag wins over it. Three
`key: value` lines are read; every other line is ignored, and a line starting with `#` is inactive:

| Key | Meaning |
|-----|---------|
| `status_file` | The status file, relative to the root. Default: `STATUS.md`. |
| `status_ceiling` | The line ceiling. Default: 300. |
| `status_sections` | The sections to check, comma-separated: `status_sections: Goal, Landed, In progress, Waiting on owner`. Default: `Now, Next, Known debt`. |

## What is checked

- A section is a level-2 heading (`## Now`); its name must equal an entry in the list exactly (capitals
  included). Level-3 and deeper headings stay inside the section above them. A level-1 heading ends the section.
- Every bullet in a checked section (`-`, `+`, `*` or `1.`), with its wrapped lines and indented paragraphs, must
  cite at least one backticked path that exists under the root. A path contains `/` or ends in a file extension.
  A trailing `:12` line number or `Q9` / `Q9, Q11` question reference is stripped before the check.
- Other backticked text (a commit id, a model name, a glob such as `docs/*.md`) is not a file and is not counted.
- Fenced code blocks are skipped. Bullets in sections that are not listed are not checked.
- It checks that a cited file exists, not that the file backs the claim.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Within the ceiling, and every checked bullet cites an existing file. |
| 1 | A finding: the file is over the ceiling, a checked bullet cites nothing, a bullet's cited paths all do not exist, or the status file is missing. |
| 2 | Invalid usage or config (a bad `--ceiling`, `--sections`, `status_ceiling` or `status_sections`, an unclosed quote in `pitmaster.md`, a missing `--root`), or an unreadable file. |

## Example

```
$ python3 status-check/status-check.py --root my-project --sections "Goal, Landed"
FAIL STATUS.md:7: bullet has no backticked file citation
FAIL STATUS.md:12: no cited path exists (`docs/old-plan.md`)
status-check: 41 lines, ceiling 300, 2 finding(s)
$ echo $?
1
```

## Tests

`python3 status-check.py --self-test` builds its own project folders in a temporary directory, so there are no
fixtures to carry. The red arms prove each guard: a bullet with no citation, a citation to a missing file, a
wildcard, a file over its ceiling, an uncited bullet under a configured section, and bad config each fail by
name; the same bullet under an unconfigured section passes; `--sections` beats `status_sections`; and the script
works from a lone copy of its own folder.

## Behaviour changes from the source

The source was a script inside the pitmaster plugin. Every flag, config key, output line and exit code is
unchanged, and its cases are carried over. Deliberate differences:

- New: `--sections` and the `status_sections` key choose the checked sections; the default is the old fixed set.
- The self-test verdict line is now `status-check: self-test PASS -- N of N cases passed` (it was
  `status-check: self-test passed (N cases, 0 failed)`), with the shed's `PASS <case>` / `FAIL <case>` lines; the
  per-case lines no longer print the exit codes.
- New self-test cases: custom sections, flag over config, flags with no `pitmaster.md`, a Windows-style
  (CRLF) status file, the unchanged output lines, and the lone-folder copy.
- Output is written as UTF-8 even where the console's default is narrower, so a non-ASCII path does not crash.
