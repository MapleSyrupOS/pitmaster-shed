# ratchet

Fails when a listed file grows past its line-count limit. A limit only moves down: when a file shrinks,
you lower its number; when it grows past the number, `ratchet` fails. It reports only and never changes
a file.

Python 3.8 or newer, standard library only.

## Usage

```
python3 ratchet.py [--list FILE] [--root DIR] [--base REF] [--allow-raise PATH=REASON]
python3 ratchet.py --self-test
```

| Option | Meaning |
|--------|---------|
| `--root DIR` | Listed paths resolve against this directory. Default: the current directory. |
| `--list FILE` | The list to read. Default: `.ratchet.list` in the root. |
| `--base REF` | Also compare each limit with the same row of the list as committed at the git ref `REF`, and fail when a limit was raised. |
| `--allow-raise PATH=REASON` | Allow one named raise under `--base`; the reason is printed. May repeat. |
| `--self-test` | Run the built-in cases, print `PASS` or `FAIL` for each, then a verdict line. Exits non-zero on any failure. |

## The list

One row per file: `<path> <max_lines>`. Blank lines and lines starting with `#` are ignored. A path
resolves against `--root`. A row with other than two fields, a limit that is not a whole number, or a
negative limit is malformed.

The line count is the number of `\n` bytes in the file, the same as `wc -l`: a last line with no newline
at the end is not counted.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Every listed file is at or below its limit. |
| 1 | A listed file is over its limit, or (with `--base`) a limit was raised and not allowed. |
| 2 | The list is missing, empty or malformed, or a listed file is missing. Nothing is checked. |

## Example

`.ratchet.list`:

```
# file          max lines
src/big.py      800
docs/guide.md   120
```

```
$ python3 ratchet/ratchet.py
ratchet: src/big.py -- FAIL: 812 lines, over its ratchet of 800 by 12
ratchet: docs/guide.md -- OK: 118 lines, 2 below its ratchet of 120; lower the ratchet to 118 in this change
ratchet: FAIL -- 1 problem(s) across 2 file(s)
$ echo $?
1
```

As a pre-commit hook, the hook runs from the root of your repository, so the default list is found there:

```yaml
repos:
  - repo: <URL of this repository>
    rev: <tag or commit>
    hooks:
      - id: ratchet
```

## Tests

`python3 ratchet.py --self-test` builds its own files in a temporary directory, so there are no fixtures
to carry. Some cases are red arms, which prove the guard works: a file over its limit must exit 1 and be named,
and a missing file or malformed row must exit 2. The `--base` cases need `git`; without it they are
skipped and the verdict line says so.
