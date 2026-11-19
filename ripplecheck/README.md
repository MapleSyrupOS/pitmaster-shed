# ripplecheck

Says which of a project's checks a branch's changes feed, from a map file the project keeps, with the file and the
map line that made each one fire. It runs the cheap checks, and names the slow ones **OWED** with the command that
would run them, so no change lands having fed a check nobody ran. It changes no file.

Python 3.8 or newer, standard library only, and `git` on the path (unless `--files` gives the changes). Runs on
macOS, Linux and Windows.

## Usage

```
python3 ripplecheck.py [--root DIR] [--map PATH] [--rows FILE] [--base REF | --files FILE] [--named ROW[,ROW...]]
                       [--dry-run] [--replay DIR]
python3 ripplecheck.py --check-map [--map PATH] [--rows FILE] [--replay DIR]
python3 ripplecheck.py --self-test
```

| Option | Meaning |
|--------|---------|
| `--root DIR` | The project root; commands run from here. Default: the current directory. |
| `--map PATH` | The map, relative to the root. Default: `tools/ripplecheck.list`. |
| `--rows FILE` | The project's own list of checks, one a line, for the coverage check, when they live outside the map. |
| `--base REF` | What the branch is diffed against, from the merge base. Default: `origin/main`, else `main`. |
| `--files FILE` | Read the changes from FILE (`-` for stdin) instead of git: `git diff --name-status` lines or bare paths. |
| `--named ROWS` | OWED checks the caller has named in its landing record; they no longer make the exit 2. |
| `--dry-run` | Print what fires and what would run; run nothing. |
| `--check-map` | Check the map alone: its rules, the coverage, and the replays. |
| `--replay DIR` | Run every `*.replay` file in DIR (see *Replays*). |
| `--self-test` | Run the built-in cases, deliberate failures included, print `ok` or `FAIL` for each and a verdict line. |

## The map

One rule a line; a line starting with `#` is a comment. Two formats, which may be mixed.

Keyword lines (` -- ` starts a command):

```
row        <ROW> cheap|slow|sweep-only [-- <command>]
feed       <glob> <ROW> [-- <targeted command>]
always     <ROW> [-- <command>]
strict     <prefix> [<prefix> ...]
sweep-only <ROW> [<ROW> ...]
```

Bar lines: `<glob> | <ROW> | run|owed|always | <command or ->`.

- Every matching rule fires, not just the first.
- A feed's targeted command runs; otherwise a `cheap` check runs its own command and a `slow` one is OWED. On a bar
  line the rule itself says `run` or `owed`; `always` fires on every diff.
- `{files}` in a command stands for the changed paths that fired it, quoted for the shell.
- Globs: `*` and `?` stay in one folder, `**` crosses folders, a trailing `/` means everything below.
- An unmapped changed file under a `strict` prefix is red; elsewhere the count is one line. A `strict` prefix is a
  folder (`tools/`, or `tools`); one ending in `*` (`tools/gen*`) covers every path that starts with the text before
  the `*`.
- Coverage: every check the project lists is fed by a rule or is sweep-only.

## Replays

A `*.replay` file holds a past change that slipped through and the check it should have named:

```
# the change that fed the corpus check nobody ran
expect CORPUS
A	docs/examples/walkthrough.py
```

If the map stops naming that check for that change, the replay is red.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Nothing red and nothing owed (or every OWED check was named). |
| 1 | A command failed, a rule is broken, an unmapped file sits under a strict prefix, or a replay failed. |
| 2 | Nothing red, but OWED checks nobody has named. |
| 3 | It could not run (no map, no git, a bad base). |

Decided by pitmaster ADR-045.
