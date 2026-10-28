#!/usr/bin/env python3
"""fence-mark: run a command in a directory and always leave a completion marker and a diff.

Usage: fence-mark.py [options] FENCE_DIR [--] COMMAND [ARG ...]
       fence-mark.py --self-test

What it does
    Runs COMMAND with FENCE_DIR (created if missing) as its working directory, waits for it, and then,
    on every way the run can end, writes two files into the fence:

      <fence>/DONE          the marker: one line saying how the run ended
      <fence>/fence.diff    the whole change the command left in the git repository the fence is in,
                            untracked files included

    A person or a script that polls the fence sees the marker appear when, and only when, the run is over.
    A fence is typically a git clone that one run works in; anything else works too (a fence that is not
    in a git repository gets a marker and no diff).

The marker (default format)
    One line, ending in a newline:
        DONE rc=0 at <time> cmd=<command>            exit status zero
        FAIL rc=N at <time> cmd=<command>            any other exit status
        FAIL rc=143 sig=TERM at <time> cmd=<command> a signal, named
    <time> is local time as YYYY-MM-DDTHH:MM:SS+ZZZZ. <command> is the arguments joined by single spaces.
    The marker is written to a temporary file in the same directory and renamed into place, so a poller
    sees either no marker or a complete one, never a half-written line. The name, the words, the time
    format and the whole line are options; the defaults above are what is written without them.

Signals
    SIGHUP, SIGINT and SIGTERM (on Windows: Ctrl-C and Ctrl-Break) are caught. On macOS and Linux the signal is
    passed on to the command, the command is waited for, and the marker records the signal by name with
    rc = 128 + the signal number; fence-mark itself then exits with that rc. On Windows a console Ctrl-C or
    Ctrl-Break already reaches every process in the console or process group, so it is not sent a second
    time. A command that is killed by a signal (not through fence-mark) is recorded the same way, by name.
    SIGKILL cannot be caught, so a kill -9 of fence-mark itself leaves no marker: a fence with no marker
    means the run is either still going or died unseen, and a poller must check which.

The diff
    Built with ``git add -N`` (intent-to-add) on the untracked files, then ``git diff``, then the same files
    unmarked again, because plain ``git diff`` does not show untracked files. Only the paths that were
    untracked beforehand are marked and unmarked, so the index after the run equals the index before it:
    nothing the command staged on purpose is unstaged, and nothing is left staged that was not. The files
    fence-mark writes itself (marker, diff, log) are not part of the diff. Any failure while recording the
    diff is swallowed: it never turns a finished run into a FAIL and never changes rc.

Exit status
    The command's own (128 + n for a signal). 127 when the command is not found and 126 when it cannot be
    run, with a marker either way. 2 for a usage error or an unusable fence, with no marker when no fence
    could be made; 2 with a marker when the fence exists but the command is missing.

--self-test
    Runs the built-in cases in temporary directories, prints one PASS, FAIL or SKIP line per case and a
    verdict line, and exits non-zero on any failure.
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

PROG = "fence-mark"
IS_WINDOWS = os.name == "nt"

DEFAULT_MARKER = "DONE"
DEFAULT_DIFF = "fence.diff"
DEFAULT_LOG = "job.log"
DEFAULT_DONE_WORD = "DONE"
DEFAULT_FAIL_WORD = "FAIL"
DEFAULT_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
DEFAULT_MARKER_FORMAT = "{verdict} rc={rc}{sig} at {stamp} cmd={cmd}"

# Exit codes a shell reports for a command that died of HUP, INT or TERM. A command that returns one of
# these is named the same way as a command killed by the signal.
CONVENTIONAL_SIGNAL_CODES = {129: "HUP", 130: "INT", 143: "TERM"}

# Longest argument list handed to one git call (Windows allows about 32k characters in all).
_ARG_CHUNK_CHARS = 12000


# ---------------------------------------------------------------------------
# Signals and exit status
# ---------------------------------------------------------------------------

def signal_label(signum: int) -> str:
    """Return the short name of a signal number: 15 -> "TERM"."""
    try:
        return signal.Signals(signum).name[3:]
    except ValueError:
        return str(signum)


def classify_exit(returncode: int, received: Optional[int]) -> Tuple[int, str]:
    """Return (rc, signal name or "") for how a run ended.

    *received* is the first signal fence-mark itself caught, if any; it wins, because the marker
    must say the run was stopped from outside. Otherwise a command killed by a signal (a negative
    return code, macOS and Linux) is named, and so are the three conventional shell codes.
    """
    if received is not None:
        return 128 + received, signal_label(received)
    if returncode < 0:
        return 128 - returncode, signal_label(-returncode)
    if returncode in CONVENTIONAL_SIGNAL_CODES:
        return returncode, CONVENTIONAL_SIGNAL_CODES[returncode]
    return returncode, ""


class RunState:
    """What the signal handler and the main flow share."""

    def __init__(self) -> None:
        self.child: Optional["subprocess.Popen[bytes]"] = None
        self.received: Optional[int] = None


def _forward(child: "Optional[subprocess.Popen[bytes]]", signum: int) -> None:
    """Pass *signum* on to the command if it is still running."""
    if child is None or child.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            # A console Ctrl-C / Ctrl-Break already reached the whole console or process group.
            if signum == getattr(signal, "SIGTERM", None):
                child.terminate()
        else:
            child.send_signal(signum)
    except OSError:
        pass


def _caught_signals() -> List[int]:
    names = ("SIGHUP", "SIGINT", "SIGTERM", "SIGBREAK")
    return [getattr(signal, n) for n in names if hasattr(signal, n)]


def _install_handlers(state: RunState) -> Dict[int, object]:
    """Install handlers that note the signal and pass it on; return the old handlers."""

    def handler(signum: int, frame: object) -> None:
        if state.received is None:
            state.received = signum
        _forward(state.child, signum)

    old: Dict[int, object] = {}
    for sig in _caught_signals():
        try:
            old[sig] = signal.signal(sig, handler)
        except (OSError, ValueError):
            pass
    return old


def _restore_handlers(old: Dict[int, object]) -> None:
    for sig, h in old.items():
        try:
            signal.signal(sig, h)  # type: ignore[arg-type]
        except (OSError, ValueError, TypeError):
            pass


def run_command(command: Sequence[str], cwd: Path, state: RunState) -> int:
    """Run *command* in *cwd* and return its return code (127 / 126 when it cannot start)."""
    if state.received is not None:
        return 1  # a signal arrived before the command started; do not start it
    try:
        child = subprocess.Popen(list(command), cwd=str(cwd))
    except FileNotFoundError:
        print(f"{PROG}: command not found: {command[0]}", file=sys.stderr)
        return 127
    except OSError as exc:
        print(f"{PROG}: cannot run {command[0]}: {exc}", file=sys.stderr)
        return 126
    state.child = child
    if state.received is not None:
        _forward(child, state.received)  # the signal landed between Popen and the line above
    while True:
        try:
            return child.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            continue


# ---------------------------------------------------------------------------
# The marker
# ---------------------------------------------------------------------------

def now_stamp(fmt: str) -> str:
    return datetime.datetime.now().astimezone().strftime(fmt)


def render_marker(fmt: str, verdict: str, rc: int, signame: str, stamp: str, cmd: str) -> str:
    """Return the marker text for *fmt*: always one line, ending in a newline."""
    sig = f" sig={signame}" if signame else ""
    line = fmt.format(verdict=verdict, rc=rc, sig=sig, signame=signame, stamp=stamp, cmd=cmd)
    return re.sub(r"[\r\n]+", " ", line).rstrip() + "\n"


def write_marker_atomic(path: Path, text: str) -> None:
    """Write *text* to *path* by temp file and rename: a reader sees nothing or all of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass
    _replace(tmp, path)


def _replace(src: Path, dst: Path) -> None:
    """os.replace, retried briefly: on Windows a reader holding the old file open blocks the rename."""
    for attempt in range(100):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if not IS_WINDOWS or attempt == 99:
                raise
            time.sleep(0.02)


# ---------------------------------------------------------------------------
# The diff
# ---------------------------------------------------------------------------

def _git(top: Path, args: Sequence[str]) -> "subprocess.CompletedProcess[bytes]":
    return subprocess.run(
        ["git", "--literal-pathspecs", "-C", str(top), *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
    )


def _chunks(paths: Sequence[str]) -> List[List[str]]:
    out: List[List[str]] = []
    cur: List[str] = []
    size = 0
    for p in paths:
        if cur and size + len(p) + 1 > _ARG_CHUNK_CHARS:
            out.append(cur)
            cur, size = [], 0
        cur.append(p)
        size += len(p) + 1
    if cur:
        out.append(cur)
    return out


def parse_untracked(porcelain_z: bytes) -> List[str]:
    """Return the untracked paths from ``git status --porcelain -z`` output."""
    records = porcelain_z.split(b"\0")
    paths: List[str] = []
    i = 0
    while i < len(records):
        rec = records[i]
        i += 1
        if len(rec) < 4:
            continue
        xy, path = rec[:2], rec[3:]
        if xy == b"??":
            paths.append(os.fsdecode(path))
        elif xy[:1] in (b"R", b"C") or xy[1:2] in (b"R", b"C"):
            i += 1  # a rename or copy record is followed by its source path
    return paths


def _same(a: str, b: str) -> bool:
    return os.path.normcase(a) == os.path.normcase(b)


def record_diff(fence: Path, diff_path: Path, own: Sequence[Path], intent_to_add: bool = True) -> str:
    """Write the fence's whole change to *diff_path*; return a note for the log, "" when all is as expected.

    Never raises for a git problem: the caller is the completion path of a run.
    """
    git = shutil.which("git")
    if git is None:
        return "diff skipped: git not found"
    probe = subprocess.run(
        ["git", "-C", str(fence), "rev-parse", "--show-toplevel"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
    )
    if probe.returncode != 0:
        return ""  # not a git repository: no diff, nothing to say
    top = Path(os.path.realpath(os.fsdecode(probe.stdout.strip())))
    own_rel = []
    for p in own:
        try:
            own_rel.append(os.path.relpath(os.path.realpath(str(p)), str(top)).replace(os.sep, "/"))
        except ValueError:
            pass  # a different drive on Windows: not inside the repository

    status = _git(top, ["status", "--porcelain", "-z", "--untracked-files=all"])
    untracked = []
    if status.returncode == 0:
        untracked = [p for p in parse_untracked(status.stdout) if not any(_same(p, o) for o in own_rel)]

    marked: List[str] = []
    note = ""
    try:
        if untracked and intent_to_add:
            for chunk in _chunks(untracked):
                if _git(top, ["add", "-N", "--", *chunk]).returncode == 0:
                    marked.extend(chunk)
                    continue
                for one in chunk:  # a file may have vanished; do not lose the rest of the chunk
                    if _git(top, ["add", "-N", "--", one]).returncode == 0:
                        marked.append(one)
        diff = _git(top, ["diff", "--no-color", "--no-ext-diff"])
        if diff.returncode == 0:
            tmp = diff_path.with_name(f"{diff_path.name}.tmp.{os.getpid()}")
            try:
                tmp.write_bytes(diff.stdout)
                _replace(tmp, diff_path)
            finally:
                if tmp.exists():
                    tmp.unlink()
        else:
            note = "diff skipped: git diff failed"
    finally:
        # Unmark exactly what was marked. A blanket `git reset` would also unstage what the
        # command staged on purpose.
        for chunk in _chunks(marked):
            _git(top, ["reset", "-q", "--", *chunk])
    return note


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=PROG,
        usage=f"{PROG}.py [options] FENCE_DIR [--] COMMAND [ARG ...]",
        description="Run a command in a directory; always leave a completion marker and a diff.",
    )
    p.add_argument("--marker", default=DEFAULT_MARKER, metavar="NAME",
                   help=f"marker file, in the fence unless absolute (default: {DEFAULT_MARKER})")
    p.add_argument("--marker-format", default=DEFAULT_MARKER_FORMAT, metavar="TEMPLATE",
                   help="marker line; fields {verdict} {rc} {sig} {signame} {stamp} {cmd}; "
                        "{sig} is ' sig=NAME' or empty (default: '%s')" % DEFAULT_MARKER_FORMAT)
    p.add_argument("--done-word", default=DEFAULT_DONE_WORD, metavar="WORD",
                   help=f"{{verdict}} when rc is 0 (default: {DEFAULT_DONE_WORD})")
    p.add_argument("--fail-word", default=DEFAULT_FAIL_WORD, metavar="WORD",
                   help=f"{{verdict}} when rc is not 0 (default: {DEFAULT_FAIL_WORD})")
    p.add_argument("--time-format", default=DEFAULT_TIME_FORMAT, metavar="STRFTIME",
                   help=f"{{stamp}} format (default: {DEFAULT_TIME_FORMAT.replace('%', '%%')})")
    p.add_argument("--diff", default=DEFAULT_DIFF, metavar="NAME",
                   help=f"diff file, in the fence unless absolute (default: {DEFAULT_DIFF})")
    p.add_argument("--no-diff", action="store_true", help="do not record a diff")
    p.add_argument("--no-intent-to-add", action="store_true",
                   help="record tracked changes only: skip the git add -N that shows untracked files "
                        "(a diagnostic)")
    p.add_argument("--log", default=DEFAULT_LOG, metavar="NAME",
                   help=f"log file, in the fence unless absolute (default: {DEFAULT_LOG})")
    p.add_argument("--no-log", action="store_true", help="do not write a log")
    p.add_argument("--self-test", action="store_true", help="run the built-in cases and exit")
    p.add_argument("fence", nargs="?", metavar="FENCE_DIR", help="the directory the command runs in")
    p.add_argument("command", nargs=argparse.REMAINDER, metavar="COMMAND", help="the command and its arguments")
    return p


def _log(path: Optional[Path], line: str) -> None:
    if path is None:
        return
    try:
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.self_test:
        return _run_self_test()
    if args.fence is None:
        print(f"usage: {PROG}.py [options] FENCE_DIR [--] COMMAND [ARG ...]", file=sys.stderr)
        return 2  # no fence named, so no marker is possible: the one markerless exit
    command: List[str] = list(args.command)
    if command and command[0] == "--":
        command = command[1:]

    # Check the marker options before anything runs: a bad template must not cost a run its marker.
    try:
        render_marker(args.marker_format, args.done_word, 0, "", now_stamp(args.time_format), "x")
    except (KeyError, IndexError, ValueError) as exc:
        print(f"{PROG}: bad --marker-format or --time-format: {exc!r}", file=sys.stderr)
        return 2

    try:
        fence = Path(os.path.abspath(args.fence))
        fence.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"{PROG}: cannot make the fence {args.fence}: {exc}", file=sys.stderr)
        return 2
    marker = fence / args.marker
    diff_path = fence / args.diff
    log_path = None if args.no_log else fence / args.log
    cmd_text = " ".join(command)

    state = RunState()
    old = _install_handlers(state)
    rc = 1
    try:
        _log(log_path, f"start {now_stamp(args.time_format)} cmd={cmd_text}")
        if not command:
            print(f"{PROG}: fence given, command missing", file=sys.stderr)
            rc = 2  # the finally block still writes the marker: FAIL rc=2
        else:
            rc = run_command(command, fence, state)
    finally:
        rc, signame = classify_exit(rc, state.received)
        if not args.no_diff:
            try:
                own = [marker, diff_path] + ([log_path] if log_path else [])
                note = record_diff(fence, diff_path, own, intent_to_add=not args.no_intent_to_add)
                if note:
                    _log(log_path, note)
            except Exception as exc:  # the recorder must never turn a finished run into a failure
                _log(log_path, f"diff skipped: {type(exc).__name__}: {exc}")
        try:
            verdict = args.done_word if rc == 0 else args.fail_word
            stamp = now_stamp(args.time_format)
            write_marker_atomic(marker, render_marker(args.marker_format, verdict, rc, signame, stamp, cmd_text))
            _log(log_path, f"marker {verdict} rc={rc}{' sig=' + signame if signame else ''} {stamp}")
        except OSError as exc:
            print(f"{PROG}: could not write the marker {marker}: {exc}", file=sys.stderr)
        _restore_handlers(old)
    return rc


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

SCRIPT = str(Path(__file__).resolve())
PY = sys.executable


class _Skip(Exception):
    """Raised by a case that cannot run here; the message says why."""


def _tool(args: Sequence[str], cwd: Optional[Path] = None, timeout: int = 90) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        [PY, SCRIPT, *args], cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )


def _git_cmd(repo: Path, *args: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
         "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def _make_repo(td: Path, name: str = "repo") -> Path:
    """A git repository with one commit holding tracked.txt, then a tracked edit and a new file."""
    repo = td / name
    repo.mkdir()
    _git_cmd(repo, "init", "-q")
    _write(repo / "tracked.txt", "original line\n")
    _git_cmd(repo, "add", "tracked.txt")
    _git_cmd(repo, "commit", "-q", "-m", "base")
    with open(repo / "tracked.txt", "a", encoding="utf-8", newline="\n") as fh:
        fh.write("worker edit\n")
    _write(repo / "new-file.txt", "THE-NEW-FILE-BODY\n")
    return repo


def _write(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _py(code: str, *args: str) -> List[str]:
    return [PY, "-c", code, *args]


_EXIT_N = "import sys; sys.exit(int(sys.argv[1]))"
_MARKER_RE = re.compile(r"^(DONE|FAIL) rc=(\d+)( sig=([A-Z]+))? at (\S+) cmd=(.*)$")


def _index_state(repo: Path) -> Tuple[str, str, str]:
    return (
        _git_cmd(repo, "ls-files", "-s").stdout,
        _git_cmd(repo, "diff", "--cached").stdout,
        "".join(ln + "\n" for ln in _git_cmd(repo, "status", "--porcelain", "--untracked-files=all").stdout.splitlines()
                if ln[3:].rsplit("/", 1)[-1] not in ("DONE", "fence.diff", "job.log")),  # fence-mark's own files
    )


def _case_exit_zero(td: Path) -> bool:
    r = _tool([str(td / "f"), "--", PY, "-c", "pass"])
    text = _read(td / "f" / "DONE")
    return r.returncode == 0 and text.startswith("DONE rc=0 at ") and text.count("\n") == 1


def _case_exit_nonzero_keeps_rc(td: Path) -> bool:
    # RED ARM: recording the diff must not clobber the command's own status. The fence is a repo
    # with changes in it, so the diff recorder really runs.
    repo = _make_repo(td)
    r = _tool([str(repo), "--", PY, "-c", _EXIT_N, "3"])
    text = _read(repo / "DONE")
    return r.returncode == 3 and text.startswith("FAIL rc=3 at ") and (repo / "fence.diff").is_file()


def _case_marker_line_format(td: Path) -> bool:
    r = _tool([str(td / "f"), "--", PY, "-c", _EXIT_N, "5"])
    raw = (td / "f" / "DONE").read_bytes()
    text = raw.decode("utf-8")
    m = _MARKER_RE.match(text.rstrip("\n"))
    if not (r.returncode == 5 and text.endswith("\n") and text.count("\n") == 1 and b"\r" not in raw and m):
        return False
    stamp_ok = re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d{4}$", m.group(5)) is not None
    cmd_ok = m.group(6) == " ".join([PY, "-c", _EXIT_N, "5"]) and m.group(1) == "FAIL" and m.group(2) == "5"
    return stamp_ok and cmd_ok and m.group(3) is None


def _case_one_line_for_multiline_command(td: Path) -> bool:
    # A command argument with a line break must not make the marker two lines.
    _tool([str(td / "f"), "--", PY, "-c", "import sys\nsys.exit(0)"])
    text = _read(td / "f" / "DONE")
    return text.count("\n") == 1 and text.endswith("\n") and "cmd=" in text


def _case_default_marker_parses_like_a_poller(td: Path) -> bool:
    # A status poller treats the existence of <fence>/DONE as "ended" and reads its first line; it
    # matches ``^DONE rc=0 `` and ``^FAIL rc=1 ``. Mirror that on the default marker of three fences.
    ok = _tool([str(td / "a"), "--", PY, "-c", "pass"])
    bad = _tool([str(td / "b"), "--", PY, "-c", _EXIT_N, "1"])
    if ok.returncode != 0 or bad.returncode != 1:
        return False

    def poll(fence: Path) -> Optional[str]:
        marker = fence / "DONE"
        if not marker.is_file():
            return None
        with open(marker, encoding="utf-8") as fh:
            return fh.readline().rstrip("\n")

    line_a, line_b = poll(td / "a"), poll(td / "b")
    if line_a is None or line_b is None:
        return False
    if not (re.match(r"^DONE rc=0 ", line_a) and re.match(r"^FAIL rc=1 ", line_b)):
        return False
    if poll(td / "never-run") is not None:  # no fence run: no marker, the poller sees "not ended"
        return False
    if not IS_WINDOWS and shutil.which("sh"):  # the same reads, done by a real shell
        shell = subprocess.run(
            ["sh", "-c", 'for d in "$@"; do [ -f "$d/DONE" ] && head -1 "$d/DONE"; done', "sh",
             str(td / "a"), str(td / "b")], capture_output=True, text=True)
        if shell.stdout.splitlines() != [line_a, line_b]:
            return False
    return True


def _case_exit_codes_named_as_signals(td: Path) -> bool:
    # A command that returns the conventional shell codes is named as the source named them; any
    # other code, 137 included, carries no name.
    for code, want in (("143", "FAIL rc=143 sig=TERM at "), ("130", "FAIL rc=130 sig=INT at "),
                       ("129", "FAIL rc=129 sig=HUP at "), ("137", "FAIL rc=137 at ")):
        fence = td / ("f" + code)
        r = _tool([str(fence), "--", PY, "-c", _EXIT_N, code])
        if r.returncode != int(code) or not _read(fence / "DONE").startswith(want):
            return False
    return True


def _case_missing_command_is_a_fail_marker(td: Path) -> bool:
    # RED ARM: a fence with no command still gets a marker (FAIL rc=2); a command that does not exist
    # gets FAIL rc=127.
    a = _tool([str(td / "a")])
    b = _tool([str(td / "b"), "--", "fence-mark-no-such-command-xyz"])
    return (a.returncode == 2 and _read(td / "a" / "DONE").startswith("FAIL rc=2 at ")
            and b.returncode == 127 and _read(td / "b" / "DONE").startswith("FAIL rc=127 at "))


def _case_no_fence_is_usage_error(td: Path) -> bool:
    r = _tool([], cwd=td)
    return r.returncode == 2 and "usage" in r.stderr and not any(td.iterdir())


def _case_bad_marker_format_refused_before_running(td: Path) -> bool:
    # RED ARM: a template naming a field that does not exist is refused with rc 2 and the command
    # never runs.
    ran = td / "ran.txt"
    code = "import sys; open(sys.argv[1], 'w').write('x')"
    r = _tool(["--marker-format", "{nope}", str(td / "f"), "--", PY, "-c", code, str(ran)])
    return r.returncode == 2 and not ran.exists() and not (td / "f" / "DONE").exists()


def _case_runs_in_the_fence(td: Path) -> bool:
    code = "import os, sys; open('cwd.txt', 'w').write(os.getcwd())"
    _tool([str(td / "f"), "--", PY, "-c", code])
    got = (td / "f" / "cwd.txt").read_text(encoding="utf-8")
    return os.path.realpath(got) == os.path.realpath(str(td / "f"))


# -- the diff ---------------------------------------------------------------

def _case_diff_has_tracked_edit(td: Path) -> bool:
    repo = _make_repo(td)
    _tool([str(repo), "--", PY, "-c", "pass"])
    return "worker edit" in _read(repo / "fence.diff")


def _case_diff_has_untracked_file(td: Path) -> bool:
    # The point of ``git add -N``: a file the command created, never committed, is in the diff. Names
    # with spaces and glob characters, and a file in a new directory, are literal paths.
    repo = _make_repo(td)
    _write(repo / "[odd] name.txt", "ODD-NAME-BODY\n")
    (repo / "newdir").mkdir()
    _write(repo / "newdir" / "deep.txt", "DEEP-BODY\n")
    _tool([str(repo), "--", PY, "-c", "pass"])
    diff = _read(repo / "fence.diff")
    return all(s in diff for s in ("THE-NEW-FILE-BODY", "ODD-NAME-BODY", "DEEP-BODY"))


def _case_newline_in_a_file_name(td: Path) -> bool:
    repo = _make_repo(td)
    _write(repo / "two\nlines.txt", "NEWLINE-NAME-BODY\n")
    _tool([str(repo), "--", PY, "-c", "pass"])
    return "NEWLINE-NAME-BODY" in _read(repo / "fence.diff")


def _case_glob_named_file_leaves_staged_files_alone(td: Path) -> bool:
    # RED ARM: an untracked file named "*.md" is a name, not a pattern. As a pattern, unmarking it
    # would also unstage the other staged .md file, and marking it would mark every .md file.
    repo = _make_repo(td)
    _write(repo / "staged.md", "STAGED-MD\n")
    _git_cmd(repo, "add", "staged.md")
    _write(repo / "*.md", "GLOB-NAME-BODY\n")
    before = _index_state(repo)
    _tool([str(repo), "--", PY, "-c", "pass"])
    return before == _index_state(repo) and "GLOB-NAME-BODY" in _read(repo / "fence.diff")


def _case_no_intent_hides_untracked(td: Path) -> bool:
    # RED ARM: with --no-intent-to-add the untracked file must vanish from the diff, or the case
    # above could pass for a reason other than the intent-to-add.
    repo = _make_repo(td)
    _tool(["--no-intent-to-add", str(repo), "--", PY, "-c", "pass"])
    diff = _read(repo / "fence.diff")
    return "worker edit" in diff and "THE-NEW-FILE-BODY" not in diff


def _case_index_unchanged(td: Path) -> bool:
    # RED ARM: the index after the run equals the index before it. The repository holds a file the
    # command staged on purpose (it must stay staged), a staged edit, an unstaged edit and
    # untracked files (they must stay untracked).
    repo = _make_repo(td)
    _write(repo / "kept.txt", "kept\n")
    _git_cmd(repo, "add", "kept.txt")
    _git_cmd(repo, "commit", "-q", "-m", "kept")
    _write(repo / "kept.txt", "kept and edited\n")
    _git_cmd(repo, "add", "kept.txt")
    _write(repo / "staged-new.txt", "STAGED-NEW\n")
    _git_cmd(repo, "add", "staged-new.txt")
    before = _index_state(repo)
    _tool([str(repo), "--", PY, "-c", "pass"])
    after = _index_state(repo)
    still_untracked = "?? new-file.txt" in after[2] and "A  staged-new.txt" in after[2]
    return before == after and still_untracked and "THE-NEW-FILE-BODY" in _read(repo / "fence.diff")


def _case_index_unchanged_unborn(td: Path) -> bool:
    # The same in a repository with no commit yet.
    repo = td / "repo"
    repo.mkdir()
    _git_cmd(repo, "init", "-q")
    _write(repo / "a.txt", "A-BODY\n")
    _write(repo / "b.txt", "B-BODY\n")
    _git_cmd(repo, "add", "b.txt")
    before = _index_state(repo)
    _tool([str(repo), "--", PY, "-c", "pass"])
    return before == _index_state(repo) and "A-BODY" in _read(repo / "fence.diff")


def _case_diff_leaves_out_its_own_files(td: Path) -> bool:
    # RED ARM: the marker, the log and the previous diff are fence-mark's own and must not be in the
    # diff, or each run's diff would contain the last one.
    repo = _make_repo(td)
    _tool([str(repo), "--", PY, "-c", "pass"])
    _tool([str(repo), "--", PY, "-c", "pass"])
    diff = _read(repo / "fence.diff")
    return "THE-NEW-FILE-BODY" in diff and "job.log" not in diff and "DONE" not in diff and "fence.diff" not in diff


def _case_non_git_fence(td: Path) -> bool:
    r = _tool([str(td / "bare"), "--", PY, "-c", "pass"])
    return (r.returncode == 0 and not (td / "bare" / "fence.diff").exists()
            and _read(td / "bare" / "DONE").startswith("DONE rc=0 "))


def _case_fence_inside_a_larger_repo(td: Path) -> bool:
    # A fence that is a folder of a repository: the diff is that repository's, untracked files in the
    # fence included, and the index is unchanged.
    repo = _make_repo(td)
    sub = repo / "sub"
    sub.mkdir()
    _write(sub / "in-sub.txt", "IN-SUB-BODY\n")
    before = _index_state(repo)
    _tool([str(sub), "--", PY, "-c", "pass"])
    diff = _read(sub / "fence.diff")
    return "IN-SUB-BODY" in diff and "THE-NEW-FILE-BODY" in diff and before == _index_state(repo)


def _case_no_diff_flag(td: Path) -> bool:
    repo = _make_repo(td)
    _tool(["--no-diff", str(repo), "--", PY, "-c", "pass"])
    return not (repo / "fence.diff").exists() and (repo / "DONE").is_file()


def _case_clean_repo_empty_diff(td: Path) -> bool:
    repo = td / "repo"
    repo.mkdir()
    _git_cmd(repo, "init", "-q")
    _write(repo / "a.txt", "a\n")
    _git_cmd(repo, "add", "a.txt")
    _git_cmd(repo, "commit", "-q", "-m", "base")
    _tool([str(repo), "--", PY, "-c", "pass"])
    return (repo / "fence.diff").read_bytes() == b""


# -- the marker file --------------------------------------------------------

def _case_marker_written_by_rename(td: Path) -> bool:
    # RED ARM: the marker reaches its name only through os.replace of a complete temp file in the
    # same directory; at the moment of the rename the marker name does not yet hold a partial line.
    marker = td / "DONE"
    text = render_marker(DEFAULT_MARKER_FORMAT, "DONE", 0, "", "stamp", "cmd")
    seen: List[Tuple[bool, bool, bool]] = []
    real = os.replace

    def spy(src: object, dst: object) -> None:
        src_p, dst_p = Path(str(src)), Path(str(dst))
        seen.append((src_p.parent == dst_p.parent, src_p.read_text(encoding="utf-8") == text, not dst_p.exists()))
        real(src, dst)  # type: ignore[arg-type]

    os.replace = spy  # type: ignore[assignment]
    try:
        write_marker_atomic(marker, text)
    finally:
        os.replace = real
    leftovers = [p.name for p in td.iterdir() if ".tmp." in p.name]
    return seen == [(True, True, True)] and _read(marker) == text and not leftovers


def _case_marker_never_half_written(td: Path) -> bool:
    # RED ARM: a poller reading the marker while it is rewritten many times (each line a megabyte)
    # sees only complete lines, and never an existing-but-empty file.
    marker = td / "DONE"
    payload = "x" * 1_000_000
    first = render_marker(DEFAULT_MARKER_FORMAT, "DONE", 0, "", "stamp", payload)
    write_marker_atomic(marker, first)
    stop = threading.Event()
    bad: List[str] = []
    reads = [0]

    def poll() -> None:
        while not stop.is_set():
            try:
                data = marker.read_bytes()
            except OSError:
                continue
            reads[0] += 1
            if not (data.startswith(b"DONE rc=0 ") and data.endswith(b"\n") and len(data) == len(first)):
                bad.append(f"{len(data)} bytes")
                return

    t = threading.Thread(target=poll)
    t.start()
    try:
        for i in range(60):
            write_marker_atomic(marker, render_marker(DEFAULT_MARKER_FORMAT, "DONE", 0, "", "stamp", payload))
    finally:
        stop.set()
        t.join()
    return not bad and reads[0] > 0


# -- flags ------------------------------------------------------------------

def _case_flag_renamed_marker(td: Path) -> bool:
    # The marker's name, words, time format and whole line are flags. The default files are not
    # written when they are renamed.
    fence = td / "f"
    year0 = str(datetime.datetime.now().year)
    args = ["--marker", "finished.txt", "--done-word", "OK", "--fail-word", "BAD", "--time-format", "%Y",
            "--marker-format", "{verdict}|{rc}|{signame}|{stamp}|{cmd}", "--diff", "my.diff", "--log", "run.log"]
    r1 = _tool([*args, str(fence), "--", PY, "-c", "pass"])
    got1 = _read(fence / "finished.txt")
    r2 = _tool([*args, str(fence), "--", PY, "-c", _EXIT_N, "4"])
    got2 = _read(fence / "finished.txt")
    year1 = str(datetime.datetime.now().year)
    cmd1, cmd2 = " ".join([PY, "-c", "pass"]), " ".join([PY, "-c", _EXIT_N, "4"])
    want1 = {f"OK|0||{y}|{cmd1}\n" for y in (year0, year1)}
    want2 = {f"BAD|4||{y}|{cmd2}\n" for y in (year0, year1)}
    names = sorted(p.name for p in fence.iterdir())
    return (r1.returncode == 0 and r2.returncode == 4 and got1 in want1 and got2 in want2
            and "DONE" not in names and "job.log" not in names and "fence.diff" not in names
            and "run.log" in names and "finished.txt" in names)


def _case_flag_renamed_diff_and_absolute_marker(td: Path) -> bool:
    repo = _make_repo(td)
    elsewhere = td / "out" / "m.txt"
    _tool(["--diff", "mine.diff", "--marker", str(elsewhere), "--no-log", str(repo), "--", PY, "-c", "pass"])
    return ("THE-NEW-FILE-BODY" in _read(repo / "mine.diff") and _read(elsewhere).startswith("DONE rc=0 ")
            and not (repo / "DONE").exists() and not (repo / "job.log").exists())


def _case_default_log(td: Path) -> bool:
    _tool([str(td / "f"), "--", PY, "-c", _EXIT_N, "2"])
    lines = _read(td / "f" / "job.log").splitlines()
    return (len(lines) == 2 and lines[0].startswith("start ") and "cmd=" in lines[0]
            and lines[-1].startswith("marker FAIL rc=2 "))


def _case_runs_from_a_lone_copy_of_its_folder(td: Path) -> bool:
    # The copy-one-folder test, from inside: the tool's folder, copied alone to an empty directory
    # with no repository around it, still fences a run. (scripts/run-self-tests.py runs the whole
    # --self-test from such a copy.)
    here = Path(__file__).resolve().parent
    lone = td / "lone" / here.name
    shutil.copytree(here, lone, ignore=shutil.ignore_patterns("__pycache__"))
    fence = td / "fence"
    r = subprocess.run([PY, str(lone / Path(__file__).name), str(fence), "--", PY, "-c", "pass"],
                       cwd=str(td / "lone"), capture_output=True, text=True)
    return r.returncode == 0 and _read(fence / "DONE").startswith("DONE rc=0 ")


# -- signals ----------------------------------------------------------------

_WAITER = (
    "import os, signal, sys, time\n"
    "ready, got, name = sys.argv[1], sys.argv[2], sys.argv[3]\n"
    "def h(n, f):\n"
    "    open(got, 'w').write(signal.Signals(n).name)\n"
    "    sys.exit(0)\n"
    "signal.signal(getattr(signal, 'SIG' + name), h)\n"
    "open(ready, 'w').write(str(os.getpid()))\n"
    "time.sleep(40)\n"
    "open(got, 'w').write('timeout')\n"
)


def _wait_for(path: Path, seconds: float = 25.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if path.exists() and path.read_text(encoding="utf-8") != "":
            return True
        time.sleep(0.05)
    return False


def _kill_leftover(ready: Path) -> None:
    """Stop the waiter this case started, by the pid it wrote, if it is still alive."""
    try:
        pid = int(ready.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    try:
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except OSError:
        pass


def _signal_the_wrapper(td: Path, name: str, rc: int) -> bool:
    fence, ready, got = td / "f", td / "ready", td / "got"
    proc = subprocess.Popen(
        [PY, SCRIPT, str(fence), "--", PY, "-c", _WAITER, str(ready), str(got), name],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        if not _wait_for(ready):
            return False
        os.kill(proc.pid, getattr(signal, "SIG" + name))
        proc.wait(timeout=25)
    except subprocess.TimeoutExpired:
        return False
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate()
        _kill_leftover(ready)
    marker = _read(fence / "DONE") if (fence / "DONE").is_file() else ""
    return (proc.returncode == rc and got.exists() and _read(got) == "SIG" + name
            and marker.startswith(f"FAIL rc={rc} sig={name} at "))


def _case_signal_term_passed_on(td: Path) -> bool:
    return _signal_the_wrapper(td, "TERM", 143)


def _case_signal_hup_passed_on(td: Path) -> bool:
    return _signal_the_wrapper(td, "HUP", 129)


def _case_signal_int_passed_on(td: Path) -> bool:
    return _signal_the_wrapper(td, "INT", 130)


def _case_command_killed_by_signal_is_named(td: Path) -> bool:
    # RED ARM: a command that dies of a signal by itself (fence-mark not signalled) is recorded by
    # name, TERM / HUP / INT / KILL.
    body = ("import os, signal, sys\n"
            "s = getattr(signal, 'SIG' + sys.argv[1])\n"
            "if sys.argv[1] != 'KILL':\n"
            "    signal.signal(s, signal.SIG_DFL)\n"
            "os.kill(os.getpid(), s)\n"
            "import time; time.sleep(30)\n")
    for name, rc in (("TERM", 143), ("HUP", 129), ("INT", 130), ("KILL", 137)):
        fence = td / name
        r = _tool([str(fence), "--", PY, "-c", body, name])
        if r.returncode != rc or not _read(fence / "DONE").startswith(f"FAIL rc={rc} sig={name} at "):
            return False
    return True


def _case_signal_before_diff_recorded(td: Path) -> bool:
    # The signal arm still records the diff: the marker is the last thing written, after the diff.
    repo = _make_repo(td)
    ready, got = td / "ready", td / "got"
    proc = subprocess.Popen(
        [PY, SCRIPT, str(repo), "--", PY, "-c", _WAITER, str(ready), str(got), "TERM"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        if not _wait_for(ready):
            return False
        os.kill(proc.pid, signal.SIGTERM)
        proc.wait(timeout=25)
    except subprocess.TimeoutExpired:
        return False
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate()
        _kill_leftover(ready)
    return (repo / "DONE").is_file() and "THE-NEW-FILE-BODY" in _read(repo / "fence.diff")


# -- Windows console events -------------------------------------------------

_WIN_WAITER = (
    "import os, signal, sys, time\n"
    "ready, got, name = sys.argv[1], sys.argv[2], sys.argv[3]\n"
    "def h(n, f):\n"
    "    open(got, 'w').write(name)\n"
    "    sys.exit(0)\n"
    "signal.signal(getattr(signal, 'SIG' + name), h)\n"
    "open(ready, 'w').write(str(os.getpid()))\n"
    "end = time.time() + 40\n"
    "while time.time() < end:\n"
    "    time.sleep(0.1)  # short sleeps: on Windows only Ctrl-C cuts a long sleep short, not Ctrl-Break\n"
    "open(got, 'w').write('timeout')\n"
)

# Sent from a helper that attaches to the target's console and generates the event for every process
# on it (Ctrl-C cannot be aimed at one process group; Ctrl-Break could, but this works for both).
_WIN_SEND_EVENT = (
    "import ctypes, sys, time\n"
    "k = ctypes.windll.kernel32\n"
    "handler = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)(lambda event: 1)  # handled: do not die of it\n"
    "k.FreeConsole()\n"
    "if not k.AttachConsole(int(sys.argv[1])):\n"
    "    sys.exit(3)\n"
    "k.SetConsoleCtrlHandler(None, 1)\n"
    "k.SetConsoleCtrlHandler(handler, 1)\n"
    "if not k.GenerateConsoleCtrlEvent(int(sys.argv[2]), 0):\n"
    "    sys.exit(4)\n"
    "time.sleep(1)\n"
)


def _win_launch(argv: List[str]) -> "subprocess.Popen[bytes]":
    return subprocess.Popen(argv, creationflags=subprocess.CREATE_NEW_CONSOLE)  # type: ignore[attr-defined]


def _win_send(proc: "subprocess.Popen[bytes]", kind: str) -> int:
    """Send the event to proc's console; return the helper's exit status (0 = sent)."""
    event = "1" if kind == "BREAK" else "0"  # CTRL_BREAK_EVENT is 1, CTRL_C_EVENT is 0
    return subprocess.run([PY, "-c", _WIN_SEND_EVENT, str(proc.pid), event]).returncode


def _win_event_case(td: Path, kind: str, rc: int) -> bool:
    """Drive a real Ctrl-C (kind "INT") or Ctrl-Break (kind "BREAK") at fence-mark on Windows."""
    # Probe first, with no fence-mark in the way: if the runner cannot deliver this event to a plain
    # process, the case is skipped by name, never passed.
    pready, pgot = td / "pready", td / "pgot"
    why = ""
    try:
        probe = _win_launch([PY, "-c", _WIN_WAITER, str(pready), str(pgot), kind])
    except OSError as exc:
        raise _Skip(f"this runner cannot start a process in a new console: {exc}")
    try:
        if not _wait_for(pready):
            why = "the probe process never became ready"
        else:
            sent = _win_send(probe, kind)
            if sent != 0:
                why = f"the console could not be attached or signalled (helper status {sent})"
            elif not _wait_for(pgot, 15):
                why = "the event was sent but never reached the probe"
    finally:
        if probe.poll() is None:
            probe.kill()
        probe.wait()
        _kill_leftover(pready)
    if why:
        raise _Skip(f"this runner cannot deliver a console {kind} event to a plain process: {why}")
    fence, ready, got = td / "f", td / "ready", td / "got"
    proc = _win_launch([PY, SCRIPT, str(fence), "--", PY, "-c", _WIN_WAITER, str(ready), str(got), kind])
    try:
        if not _wait_for(ready) or _win_send(proc, kind) != 0:
            return False
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        return False
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        _kill_leftover(ready)
    marker = _read(fence / "DONE") if (fence / "DONE").is_file() else ""
    return proc.returncode == rc and marker.startswith(f"FAIL rc={rc} sig={kind} at ") and got.exists() and _read(got) == kind


def _case_windows_ctrl_break(td: Path) -> bool:
    return _win_event_case(td, "BREAK", 128 + getattr(signal, "SIGBREAK", 21))


def _case_windows_ctrl_c(td: Path) -> bool:
    return _win_event_case(td, "INT", 130)


def _case_windows_exit_nonzero(td: Path) -> bool:
    # Windows exit statuses can exceed 255; the marker keeps the whole number.
    code = "3221225786"
    r = _tool([str(td / "f"), "--", PY, "-c", "import os; os._exit(-1073741510)"])  # 0xC000013A
    return r.returncode == int(code) and _read(td / "f" / "DONE").startswith(f"FAIL rc={code} at ")


# (name, function, needs): needs is "" (any host), "git", "posix" or "windows"
_CASES: List[Tuple[str, Callable[[Path], bool], str]] = [
    ("zero-exit-writes-done", _case_exit_zero, ""),
    ("nonzero-exit-keeps-its-rc", _case_exit_nonzero_keeps_rc, "git"),
    ("marker-is-one-line-in-the-default-format", _case_marker_line_format, ""),
    ("multiline-command-still-one-line", _case_one_line_for_multiline_command, ""),
    ("default-marker-parses-like-a-poller", _case_default_marker_parses_like_a_poller, ""),
    ("conventional-exit-codes-named-as-signals", _case_exit_codes_named_as_signals, ""),
    ("missing-command-writes-fail-marker", _case_missing_command_is_a_fail_marker, ""),
    ("no-fence-is-a-usage-error", _case_no_fence_is_usage_error, ""),
    ("bad-marker-format-refused-before-running", _case_bad_marker_format_refused_before_running, ""),
    ("command-runs-in-the-fence", _case_runs_in_the_fence, ""),
    ("diff-has-the-tracked-edit", _case_diff_has_tracked_edit, "git"),
    ("diff-has-untracked-files", _case_diff_has_untracked_file, "git"),
    ("file-name-with-a-newline-is-in-the-diff", _case_newline_in_a_file_name, "git+posix"),
    ("glob-named-file-leaves-staged-files-alone", _case_glob_named_file_leaves_staged_files_alone, "git+posix"),
    ("no-intent-to-add-hides-untracked", _case_no_intent_hides_untracked, "git"),
    ("index-unchanged-by-a-run", _case_index_unchanged, "git"),
    ("index-unchanged-in-a-repo-with-no-commit", _case_index_unchanged_unborn, "git"),
    ("diff-leaves-out-fence-marks-own-files", _case_diff_leaves_out_its_own_files, "git"),
    ("non-git-fence-gets-a-marker-and-no-diff", _case_non_git_fence, ""),
    ("fence-inside-a-larger-repo", _case_fence_inside_a_larger_repo, "git"),
    ("no-diff-flag", _case_no_diff_flag, "git"),
    ("clean-repo-gives-an-empty-diff", _case_clean_repo_empty_diff, "git"),
    ("marker-written-by-rename", _case_marker_written_by_rename, ""),
    ("marker-never-half-written", _case_marker_never_half_written, ""),
    ("flag-renamed-marker", _case_flag_renamed_marker, ""),
    ("flag-renamed-diff-and-absolute-marker", _case_flag_renamed_diff_and_absolute_marker, "git"),
    ("default-log-lines", _case_default_log, ""),
    ("runs-from-a-lone-copy-of-its-folder", _case_runs_from_a_lone_copy_of_its_folder, ""),
    ("signal-term-passed-on-and-named", _case_signal_term_passed_on, "posix"),
    ("signal-hup-passed-on-and-named", _case_signal_hup_passed_on, "posix"),
    ("signal-int-passed-on-and-named", _case_signal_int_passed_on, "posix"),
    ("command-killed-by-a-signal-is-named", _case_command_killed_by_signal_is_named, "posix"),
    ("signal-arm-still-records-the-diff", _case_signal_before_diff_recorded, "posix+git"),
    ("windows-large-exit-status-kept", _case_windows_exit_nonzero, "windows"),
    ("windows-ctrl-break-recorded", _case_windows_ctrl_break, "windows"),
    ("windows-ctrl-c-recorded", _case_windows_ctrl_c, "windows"),
]


def _skip_reason(needs: str, git_ok: bool) -> Optional[str]:
    for need in needs.split("+"):
        if need == "git" and not git_ok:
            return "git not found"
        if need == "posix" and IS_WINDOWS:
            return "needs macOS or Linux"
        if need == "windows" and not IS_WINDOWS:
            return "needs Windows"
    return None


def _run_self_test() -> int:
    """Run every case in its own temporary directory; print PASS/FAIL/SKIP per case."""
    git_ok = shutil.which("git") is not None
    n_pass = n_fail = n_skip = 0
    for name, fn, needs in _CASES:
        reason = _skip_reason(needs, git_ok)
        if reason:
            print(f"SKIP {name} ({reason})")
            n_skip += 1
            continue
        with tempfile.TemporaryDirectory() as td:
            try:
                ok = fn(Path(td))
            except _Skip as why:
                print(f"SKIP {name} ({why})")
                n_skip += 1
                continue
            except Exception as exc:  # a crashing case is a failing case
                print(f"FAIL {name} ({type(exc).__name__}: {exc})")
                n_fail += 1
                continue
        if ok:
            print(f"PASS {name}")
            n_pass += 1
        else:
            print(f"FAIL {name}")
            n_fail += 1
    ran = n_pass + n_fail
    tail = f", {n_skip} skipped" if n_skip else ""
    if n_fail:
        print(f"{PROG}: self-test FAIL -- {n_fail} of {ran} cases failed{tail}")
        return 1
    print(f"{PROG}: self-test PASS -- {n_pass} of {ran} cases passed{tail}")
    return 0


if __name__ == "__main__":
    _rc = main()
    if IS_WINDOWS and _rc > 0x7FFFFFFF:
        _rc -= 1 << 32  # sys.exit takes a C int; the same bits as the unsigned status
    sys.exit(_rc)
