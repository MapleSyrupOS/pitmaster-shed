# adr-check

Checks every architecture decision record (ADR) in a project: that its YAML header passes the adr-plus schema,
that each proof it names resolves, that it has a History section, and that MADR's features (a Drivers list,
Good and Bad bullets, a chosen reason) appear exactly when two or more options were weighed. It reports only and
never changes a file.

Python 3.8 or newer, standard library only. macOS, Linux, Windows. Pull-request proofs are looked up with the
GitHub CLI (`gh`) when it is installed.

## Usage

```
python3 adr-check.py [--root DIR] [--dir PATH] [--triggers-from N] [--triggers-skip N,N]
python3 adr-check.py --self-test
```

| Option | Meaning |
|--------|---------|
| `--root DIR` | The repository root; proofs resolve against it. Default: the current directory. The tool never looks in a folder above it. |
| `--dir PATH` | The decisions folder, relative to the root. Default: the first of `docs/decisions`, `docs/adr`, `docs/adrs`, `doc/adr`, `doc/decisions`, `adr`, `decisions` that exists. |
| `--triggers-from N` | The first ADR number the trigger check judges. Default: 1. |
| `--triggers-skip N,N` | ADR numbers the trigger check does not judge (ADRs written before the check existed). Anything but whole numbers separated by commas is a usage error (exit 2). |
| `--self-test` | Run the built-in cases, print `PASS` or `FAIL` for each, then a verdict line. Exits non-zero on any failure. |

## What it checks

For an ADR with a YAML header (ADR+, spec 1.3):

- **The header.** It parses, and passes the adr-plus schema: only known fields; `status` one of the seven
  lowercase words; `kind` one of architecture, design or process; `built` one of landed, partial, not started or
  n/a; `proof` present unless not started; `owed` present when partial; `supersedes` and `amends` present; a
  value YAML would retype (a date, a number, yes/no) written in double quotes. The schema is the project's own
  `adr-plus.schema.json` in its decisions folder when there is one, else the adr-plus 0.1.0 schema embedded in
  the script.
- **The proof.** A landed or partial decision names at least one proof, and each must resolve:

  | Written as | Resolves when |
  |------------|---------------|
  | `` `path/to/file` `` | the file or folder exists |
  | `` `path/to/test.py::name` `` | the file exists and contains `name` |
  | `#123` or a `.../pull/123` URL | `gh pr view 123` finds it; without `gh`, or when it cannot be looked up, a warning |

  Backticks are optional: without them the value is split at commas and semicolons and only the pieces with no
  spaces count. A commit hash is never proof: it may follow a real proof, for history, but alone it fails. A URL
  that is not a pull-request link is not proof either.
- **History.** The file has a `## History` section.
- **The triggers** (spec 1.7), for every ADR numbered `--triggers-from` or higher and not in `--triggers-skip`.
  Two or more options under `## Alternatives` need a `Drivers:` list in `## Context`, a `- Good:` and a `- Bad:`
  bullet under every option, and `(chosen: reason)` on the first option's heading (a `rejected` or `withdrawn`
  decision is exempt from the last). A `Drivers:` list with fewer than two options is padding and fails.

An ADR still written with bold `**Status:**` lines fails and names the converter, adr-plus's
`adr-yaml.py --apply`. For MADR and Nygard ADRs it checks only that a status can be found. For every format: a
title, and no two ADRs with the same number. The check confirms that a proof resolves; it does not judge whether
the proof is a good one.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | No failures. Warnings are allowed. |
| 1 | At least one failure. |
| 2 | No decisions folder was found, or an option is unusable (for example a `--triggers-skip` that is not numbers). |

## Example

```
$ python3 adr-check.py
FAIL ADR-002-cache.md: test file `tests/test_cache.py` does not exist
FAIL ADR-002-cache.md: no '## History' section
adr-check: 2 ADRs, 2 failure(s)
$ echo $?
1
```

As a pre-commit hook, the hook runs from the root of your repository (add `args: [--triggers-from, "3"]` to
set the options):

```yaml
repos:
  - repo: <URL of this repository>
    rev: <tag or commit>
    hooks:
      - id: adr-check
```

## Shared code

The script is one file. The code it shares with `adr-index` (the ADR+ header reader and the ADR parser) is
copied into both between `# --- shed:shared NAME begin ---` and `# --- shed:shared NAME end ---` lines; a check
in this repository (`scripts/check-twins.py`) fails if the two copies of a block differ by a byte. The `adrplus`
block holds the header reader and schema validator of adr-plus 0.1.0, and the schema as a string constant. The
`adr_triggers` block is used by this script alone. Edit a shared block in both files together.

## Tests

`python3 adr-check.py --self-test` builds its own ADRs and project folders in temporary directories, so there are
no fixtures to carry. Most cases are red arms, which prove a guard works and name what they expect in the output:
a missing status, a bad `built` word, an unresolved proof, a commit alone as proof, each trigger rule, and the
`--triggers-from` and `--triggers-skip` scoping must each exit 1 with the named message; a run with no decisions
folder must exit 2; a run below the root must not find the decisions folder of a folder above it. The case that
looks up a pull request that cannot be found checks that it is a warning, never a failure. The `gh` lookup and
the real commit in the commit cases need `gh` and `git`; without them the lookup case still passes (the answer is
a warning either way) and the commit cases use a made-up hash.

## Behaviour changes from the source

- The message for an ADR in bold-line form names the converter as adr-plus's `adr-yaml.py --apply`, without a
  path. It used to give a path beside the skill the script came from.
- The ADR+ header reader and schema validator (adr-plus 0.1.0) are part of the script, not imported from a
  folder beside it, and the shipped schema is a string constant used when the project has no schema of its own.
  The reader's lookup order is unchanged: an explicit setting, then the project's `adr-plus.schema.json`, then
  the embedded one.
- A `--dir` outside the root no longer stops the run with a traceback; the schema is then looked up in the
  usual folder names.
- The commit warning no longer cites a numbered rule ("(S1)"); its wording is otherwise the same.
- The self-test prints one `PASS` or `FAIL` line per case and a verdict line, in place of one "self-test passed"
  line, and has more cases: a missing status, a bad `built` word, a URL that is not a pull-request link, a PR
  that cannot be looked up, a retyped value, a title missing, a Nygard ADR with no status, the project schema
  winning, `--dir`, the default root, no search above the root, both trigger flags through the command line, a
  bad `--triggers-skip`, and that no file is edited.
