# modeguard

Refuses a file-mode change on a tracked path unless the change is on an allow list. The usual
cause is a lost executable bit: a `cp` over an existing script writes through a new file with
your umask, the script comes back `100644` instead of `100755`, and `git diff` shows a real,
committable mode change that nothing else in the tree looks at. `modeguard` names every such
change, in the working tree, the index and your branch, and never repairs one.

Python 3.8 or newer, standard library only, plus `git` on your `PATH`. macOS, Linux, Windows
(on Windows the working-tree arm is skipped when git cannot see modes; see below).

## Usage

```
python3 modeguard.py [--repo DIR] [--allow PATH | --no-allow] [--base REF] [--index-only] [--plant "OLD NEW PATH"]
python3 modeguard.py --install-hook [--repo DIR]
python3 modeguard.py --uninstall-hook [--repo DIR]
python3 modeguard.py --self-test
```

| Option | Meaning |
|--------|---------|
| `--repo DIR` | The repository to judge. Default: the current directory. |
| `--allow PATH` | The allow list to read. Default: `.modeguard.allow` at the repository root. A file named here must exist. |
| `--no-allow` | Ignore the allow list. |
| `--base REF` | The ref the branch arm compares `HEAD` with (its merge base with `HEAD` is used). Default: the branch `origin/HEAD` points to, else `main`, else `master`. |
| `--index-only` | Judge only the index against `HEAD`: the set a commit is about to record. Reads no base and no `core.fileMode`. |
| `--plant "OLD NEW PATH"` | Also judge a planted change, to see whether a path would be allowed. |
| `--install-hook` | Write a git pre-commit hook that runs this script (see below). Never overwrites a hook that is not its own. |
| `--uninstall-hook` | Remove a pre-commit hook that `--install-hook` wrote, and nothing else. |
| `--hook` | What the installed hook runs: `--index-only`, silent when clean, messages on stderr. You do not normally type it. |
| `--self-test` | Run the built-in cases, print `PASS` or `FAIL` for each, then a verdict line. Exits non-zero on any failure. |

## What is judged

Three diffs, each its own arm:

| Arm | Compares |
|-----|----------|
| `worktree` | the working tree against the index (`git diff`) |
| `index` | the index against `HEAD` (`git diff --cached`) |
| `branch` | `HEAD` against its merge base with the base ref |

A creation or a deletion is not a mode change. A path, a type change (file to symlink) and both
modes make one finding. A rename with identical content and a new mode is one finding on the new
path, printed with `(renamed from <old path>)`.

The branch base is `--base`, else the branch `origin/HEAD` points to, else `main`, else `master`
(for `main` and `master` the remote-tracking `origin/` ref is preferred over the local one). The
verdict line says which ref was used, for example `(base 1a2b3c4d via origin/main)`. With no
ref at all the branch arm cannot be judged and the exit is 2.

### Windows, and `core.fileMode` off

Git for Windows sets `core.fileMode` to false, and then the working-tree diff cannot see modes.
`modeguard` skips only the working-tree arm, says so in one line, and judges the index and branch
arms (so the verdict reads `2 arm(s)`):

```
modeguard: core.fileMode is false in this repository: the working-tree arm is skipped (git cannot see mode changes there); judging the index and branch arms
```

## The allow list

A deliberate mode change is one line in `.modeguard.allow`:

```
<old-mode> <new-mode> <path>  # <reason>
```

The path (as git shows it, relative to the repository root) and both modes must equal the
finding's, and the reason is required. Blank lines and lines starting with `#` are ignored. A row
that is not in this form is a failure to judge (exit 2). A row that matches no finding in a full
check is printed as `ALLOW-UNUSED <old> <new> <path>`; it does not change the exit, and it is
not reported under `--index-only`. An absent default file allows nothing.

## Installing

### With the pre-commit framework

Add this to your `.pre-commit-config.yaml` (it judges what the commit is about to record):

```yaml
repos:
  - repo: <URL of this repository>
    rev: <tag or commit>
    hooks:
      - id: modeguard
```

### Without it

```
python3 modeguard.py --install-hook
```

writes `.git/hooks/pre-commit` (or the `pre-commit` file in the directory `core.hooksPath` names).
The file is a few lines of `sh`, which git for Windows also runs through its own `sh`, that
`exec` this Python on this script with `--hook`. The Python and script paths are the ones used at
install time; move the script and the hook prints a note and lets the commit through until you
run `--install-hook` again.

- It refuses to overwrite a `pre-commit` hook that is not its own, says so, leaves the file
  untouched and exits 1. The message gives the one line to add to your own hook to run both.
- Running it again refreshes its own hook.
- `python3 modeguard.py --uninstall-hook` removes it.
- A mode change is refused with `MODE-CHANGE` lines, a pointer to `.modeguard.allow`, and the
  bypass `git commit --no-verify`. When the check cannot be judged (a broken allow list, for
  example) the hook prints why and lets the commit through, so a broken setup never blocks every
  commit.
- The hook always reads `.modeguard.allow` at the repository root.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | No unallowed mode change. (`--install-hook` and `--uninstall-hook`: done.) |
| 1 | At least one unallowed mode change. (`--install-hook`: a hook that is not modeguard's is in the way.) |
| 2 | The repository, a diff, the base, or the allow list cannot be read, or the options are wrong. Nothing is judged. |

## Example

```
$ cp new-tool.sh tools/tool.sh        # drops the executable bit
$ python3 modeguard/modeguard.py
MODE-CHANGE worktree 100755 => 100644 tools/tool.sh
modeguard: restore the mode (chmod, or git update-index --chmod=+x / -x; a cp over the file drops it), or list a deliberate change in .modeguard.allow as '<old-mode> <new-mode> <path>  # <reason>'
modeguard: FAIL -- 1 mode change(s) in 3 arm(s) (base 1a2b3c4d via origin/main), 0 allowlisted
$ echo $?
1
```

Then either `chmod +x tools/tool.sh`, or, if the change is meant, add a row to `.modeguard.allow`:

```
100755 100644 tools/tool.sh  # tool.sh is data now
```

## Tests

`python3 modeguard.py --self-test` builds throwaway git repositories in a temporary directory, so
there are no fixtures to carry. The repositories read none of your git configuration or hooks
(the self-test points `HOME` and git's global and system config at empty files, and sets a local
identity in each repository). Some cases are red arms, which prove a guard works: each arm must
fail a mode change by name, a malformed allow row must exit 2, a `master`-only repository must be
judged, `core.fileMode` off must be judged rather than refused, the installed hook must refuse a
`100755` to `100644` commit, and the installer must leave another hook alone.

Cases that need POSIX file modes (the working-tree arm, a real `chmod`) are skipped on Windows,
by name, and counted in the verdict line.

## Behaviour changes from the source

- **Name, files and allow list.** The allow list is `.modeguard.allow` at the repository root (it
  was a file inside a `tools/` directory), and `--allow PATH` names another. A named file that is
  missing is an error rather than an empty list.
- **No bash.** The installer is Python (`--install-hook`, `--uninstall-hook`) and writes a pre-commit
  hook directly; the source's installer was a shell script that set `core.hooksPath` to a tracked
  directory of hooks, and its hook was a separate shell script found by path. The hook file now
  holds the Python and script paths from install time. `core.hooksPath` is read, not written.
- **Hook overwrite.** The installer refuses to overwrite a `pre-commit` hook that is not its own
  and says so (exit 1). The source never wrote into an existing hook file.
- **Windows.** With `core.fileMode` false the check no longer exits 2: it skips the working-tree arm
  only, prints a clear line, and judges the index and branch arms.
- **Branch base.** `--base REF`, else `origin/HEAD`'s branch, else `main`, else `master`. The source
  knew only `origin/main` and `main`.
- **Options.** An argparse command line (`--repo`, `--allow`, `--base`) replaces the source's
  hand-parsed one. The source's `--fixture` (write a scratch repository) is gone; the self-test
  builds its own. `--hook` is new.
- **Output.** The verdict is now `modeguard: OK -- ...` or `modeguard: FAIL -- ...`, and a failing run
  prints one line saying how to proceed (restore the mode or list it in the allow list).
- **Self-test.** Ported onto the shed's `PASS <case>` / `FAIL <case>` shape with a verdict line, and
  isolated from your git configuration. New cases cover the installer and the installed hook, a
  `master`-only repository, `origin/HEAD`, `--base`, `core.fileMode` off, a file turned into a
  symlink, and a CRLF allow list.
- **Allow list file.** A UTF-8 byte-order mark and CRLF line endings are accepted.
