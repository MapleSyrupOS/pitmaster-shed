# fence-mark

Runs a command in a directory (a "fence") and always leaves two files behind when the run ends: a one-line
completion marker (`DONE`) and the whole change the command made (`fence.diff`), untracked files included.
A person or a script that polls the fence knows a run is over when the marker appears, whatever way it
ended: success, failure or a signal.

macOS, Linux, Windows. Python 3.8 or newer, standard library only. `git` is needed for the diff only.

## Usage

```
python3 fence-mark.py [options] FENCE_DIR [--] COMMAND [ARG ...]
python3 fence-mark.py --self-test
```

`FENCE_DIR` is created if it does not exist and is the command's working directory. It is usually a git
clone one run works in; a directory that is not in a git repository gets a marker and no diff. Options go
before `FENCE_DIR`: everything after it is the command.

| Option | Meaning |
|--------|---------|
| `--marker NAME` | The marker file. Relative names are inside the fence. Default: `DONE`. |
| `--marker-format TEMPLATE` | The marker line. Fields: `{verdict}` `{rc}` `{sig}` `{signame}` `{stamp}` `{cmd}`; `{sig}` is ` sig=NAME` (with its leading space) or empty, `{signame}` is the bare name or empty. Default: `{verdict} rc={rc}{sig} at {stamp} cmd={cmd}`. |
| `--done-word WORD` | `{verdict}` when the status is 0. Default: `DONE`. |
| `--fail-word WORD` | `{verdict}` when it is not. Default: `FAIL`. |
| `--time-format STRFTIME` | `{stamp}` format, local time. Default: `%Y-%m-%dT%H:%M:%S%z`. |
| `--diff NAME` | The diff file. Relative names are inside the fence. Default: `fence.diff`. |
| `--no-diff` | Do not record a diff. |
| `--no-intent-to-add` | Record tracked changes only: skip the `git add -N` that makes untracked files visible. A diagnostic; the self-test uses it as a red arm. |
| `--log NAME` | A small log of the run (`start ...` and `marker ...` lines). Default: `job.log`. |
| `--no-log` | Do not write a log. |
| `--self-test` | Run the built-in cases, print `PASS`, `FAIL` or `SKIP` for each, then a verdict line. Exits non-zero on any failure. |

A `--marker-format` or `--time-format` that cannot be rendered is refused before the command runs (exit 2),
so a typo cannot cost a run its marker.

## The marker

One line, ending in a newline, written on every way the run can end (default format):

```
DONE rc=0 at 2026-01-31T09:15:02-0500 cmd=make test
FAIL rc=2 at 2026-01-31T09:15:02-0500 cmd=make test
FAIL rc=143 sig=TERM at 2026-01-31T09:15:02-0500 cmd=make test
```

`cmd=` is the arguments joined by single spaces; a line break inside an argument is replaced by a space
so the marker stays one line. The marker is written to a temporary file in the same directory and renamed
into place (`os.replace`), so a poller sees no marker or a complete one, never half a line. On Windows the
rename is retried for a moment if a poller has the old marker open.

A poller reads the marker like this: the file `<fence>/DONE` existing means the run ended, and its first
line says how. That is what the defaults keep exactly, and the self-test proves it with a case that
reads the default marker that way (including with a real `sh` and `head -1` where one exists).

A fence with no marker means the run is still going, or died unseen: `kill -9` of fence-mark itself
cannot be caught, so a poller has to check which, and not assume either.

## Signals

fence-mark catches HUP, INT and TERM (Windows: Ctrl-C and Ctrl-Break) so that the marker is still written.

- macOS and Linux: the signal is passed on to the command, fence-mark waits for the command to end, records
  the diff and the marker (`FAIL rc=128+N sig=NAME`), and exits with that rc. A command that is killed by a
  signal on its own (fence-mark not signalled) is recorded the same way, by name: `sig=TERM rc=143`,
  `sig=KILL rc=137`. A Ctrl-C typed at a terminal already reaches the command through the terminal's
  process group, so the command sees that one twice. A command that merely exits with 129, 130 or 143 is named HUP, INT or TERM; any other
  exit code carries no name.
- Windows: a console Ctrl-C or Ctrl-Break is delivered to every process in the console (or process group),
  the command included, so fence-mark does not send it a second time; it waits, then records
  `sig=INT rc=130` or `sig=BREAK rc=149`. SIGTERM is not delivered by Windows itself; if raised in
  fence-mark it terminates the command. There is no `sig=` for `taskkill` or `TerminateProcess` on
  fence-mark itself, which cannot be caught.

## The diff

`fence.diff` is the output of `git diff` for the whole change, including files the command created and
never committed. `git diff` does not show untracked files, so fence-mark marks them with `git add -N`
(intent-to-add), runs `git diff`, and unmarks exactly those paths. The recording is index-neutral: the
index after a run equals the index before it, so nothing the command staged on purpose is unstaged and
nothing is left staged that was not. fence-mark's own files (marker, diff, log) are not in the diff.

If the fence is a folder inside a larger repository, the diff is of that repository. Any failure while
recording the diff is swallowed: it never turns a finished run into a FAIL and never changes the status.
A path containing a newline is handled.

## Exit codes

| Code | Meaning |
|------|---------|
| the command's | Its own exit status; `128 + N` when a signal stopped it (a Windows status above 255 is kept whole). |
| 126 | The command was found but cannot be run. A marker is written. |
| 127 | The command was not found. A marker is written. |
| 2 | A usage error: no fence named, or the fence cannot be made (no marker is possible), or a bad marker option; with a fence but no command, a marker `FAIL rc=2` is written. |

## Example

```
$ python3 fence-mark.py ~/work/run-17 -- python3 agent.py --task 17
$ cat ~/work/run-17/DONE
DONE rc=0 at 2026-01-31T09:15:02-0500 cmd=python3 agent.py --task 17
$ git apply --check ~/work/run-17/fence.diff
```

A different marker, with its own words and no log:

```
$ python3 fence-mark.py --marker finished.txt --done-word OK --fail-word BAD --no-log \
    --marker-format '{verdict} {rc}{sig}' work -- make
$ cat work/finished.txt
OK 0
```

## Tests

`python3 fence-mark.py --self-test` builds its own repositories in temporary directories, so there are
no fixtures to carry. It covers: zero and non-zero exit (the rc is not clobbered by the diff recorder),
each signal by name (passed on to the command, and a command that dies of a signal), the marker written by
rename and never half-written under a polling reader, tracked and untracked files in the diff, the index
unchanged (with a file the command staged on purpose, and in a repository with no commit), a renamed
marker, the default marker read the way a poller reads it, and the tool run from a lone copy of its
folder. Red arms: `--no-intent-to-add` must hide the untracked file; the index, rc, marker and naming
cases each fail by name when their guard is broken.

Cases that cannot run on a platform are SKIPPED by name and counted in the verdict line. The POSIX signal
cases (and the newline and glob file-name cases) are skipped on Windows. The two Windows console cases,
`windows-ctrl-break-recorded` and `windows-ctrl-c-recorded`, are skipped elsewhere. On Windows each starts
fence-mark (and a waiting command) in a new console and sends a real console Ctrl-C or Ctrl-Break to it
from a helper process that attaches to that console, then checks the command saw it and the marker says
`sig=INT rc=130` or `sig=BREAK rc=149`. Before that, each probes the same delivery on a plain process
with no fence-mark in the way; if a machine cannot deliver the event at all (no console can be made or
attached), the case is skipped by name with the reason, never passed. Both ran for real on GitHub's
`windows-latest` runner.

## Behaviour changes from the source

The source was a bash script. These differences are deliberate:

- One Python script for macOS, Linux and Windows. Signal handlers and `try`/`finally` stand in for bash's
  `EXIT` trap.
- The signal is passed on to the command (macOS and Linux). In the source the trap ran only after the
  command had finished by itself, and the command saw the signal only if the terminal sent it to the
  whole process group.
- A command killed by any signal is named (`sig=KILL rc=137` too); the source named only HUP, INT, TERM,
  and only by the 129, 130 and 143 exit codes, which are still named the same way.
- The marker name, words, time format and whole line are options. The defaults write exactly what the
  source wrote: `DONE rc=0 at <time> cmd=<command>`, `FAIL rc=N ...`, `FAIL rc=143 sig=TERM ...` (the
  source's header comment spelled the last one `FAIL sig=TERM rc=143`, but the line it wrote has
  `rc=` first, and that is what is kept).
- The log and diff names are options, and each can be turned off.
- The diff leaves out fence-mark's own files (marker, diff, log); the source's diff included the log.
- The diff uses `--no-color --no-ext-diff`, so a user's git config cannot change its form, and literal
  pathspecs, so a file name with `[` or `*` in it is a name and not a pattern.
- Untracked files are listed one by one (`git status -z --untracked-files=all`), so a file name with a
  newline works (the source could not) and a new directory's files are marked file by file.
- A path list too long for one command line is split across several git calls. If marking a batch fails,
  its files are tried one at a time, and only the paths actually marked are unmarked.
- A line break inside a command argument becomes a space in `cmd=`, so the marker stays one line.
- A command that is not found gives `FAIL rc=127` and one that cannot be run `FAIL rc=126` (as a shell
  would), instead of a Python traceback.
- The source's `--self-test` printed `ok` lines and a summary; this one prints `PASS` / `FAIL` / `SKIP`
  per case and a verdict line.
