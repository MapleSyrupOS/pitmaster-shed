#!/usr/bin/env python3
"""modeguard: refuse a file-mode change on a tracked path unless it is on an allow list.

Usage: modeguard.py [--repo DIR] [--allow PATH | --no-allow] [--base REF] [--index-only]
                    [--plant "OLD NEW PATH"]
       modeguard.py --install-hook | --uninstall-hook [--repo DIR]
       modeguard.py --self-test

What it does
    A `cp` over an existing file writes through an inode the shell created with its own
    umask, so a tracked 100755 script comes back 100644, and `git diff` shows a real,
    committable mode change that nothing else in the tree looks at. modeguard reads three
    diffs and names each mode change in them:

        worktree  the working tree against the index        (git diff)
        index     the index against HEAD                    (git diff --cached)
        branch    HEAD against its merge base with the base (git diff BASE HEAD)

    A creation or a deletion is not a mode change (its other side is mode 000000). A path,
    a type change (file to symlink) and both modes make one finding. A rename with identical
    content and a new mode is one finding on the new path, printed with
    `(renamed from <old path>)`.

The branch base
    --base REF if given; else the branch origin/HEAD points to; else main; else master. For
    main and master the remote-tracking ref (origin/main) is preferred over the local one.
    The base used is the merge base of that ref and HEAD, and the verdict line names the ref.
    With none of them (and no --base) the branch arm cannot be judged and the exit is 2.

core.fileMode
    With core.fileMode false (git for Windows sets it by default) the working-tree diff
    cannot see modes. modeguard then skips only the working-tree arm, prints one line saying
    so, and judges the index and branch arms.

The allow list
    A deliberate mode change is listed in `.modeguard.allow` at the repository root (or the
    file named by --allow), one per line:

        <old-mode> <new-mode> <path>  # <reason>

    The path and both modes must equal the finding's; the reason is required; blank lines and
    lines starting with `#` are ignored. A row that matches no finding in a full check is
    printed as `ALLOW-UNUSED <old> <new> <path>`; it does not change the exit. An absent
    default file allows nothing; a file named with --allow must exist.

--index-only
    Judges the index arm alone, the set a commit is about to record. It reads no branch base
    and no core.fileMode, so a repository with no main or master is judged, and the verdict
    line says `index only`.

--install-hook, --uninstall-hook
    --install-hook writes a git pre-commit hook for a repository that does not use the
    pre-commit framework. The hook file is a few lines of sh (git for Windows runs hooks
    through its own sh) that call this script with this Python and `--hook`. It is written to
    the hooks directory git will actually run (core.hooksPath if set). It never overwrites a
    hook that is not its own: it says so, leaves the file alone and exits 1. --uninstall-hook
    removes only a hook it wrote.

--hook
    What the installed hook runs: --index-only, silent when clean, messages on stderr, exit 1
    for a mode change, and exit 0 with a note when the check cannot be judged (an unreadable
    allow list, for example) so a broken setup does not block every commit.

Prints one `MODE-CHANGE <arm> <old> => <new> <path>` line per finding and a verdict line.
Exit 0: no unallowed mode change. Exit 1: at least one (or, for --install-hook, a hook of
someone else's is in the way). Exit 2: a diff, the base, the allow list or the repository
cannot be read. It reports only: it never changes a file mode or a file in the work tree.

--self-test
    Builds throwaway git repositories in a temporary directory (isolated from your git
    configuration and hooks), prints one PASS or FAIL line per case and a verdict line, and
    exits non-zero on any failure.

Python 3.8 or newer, standard library only. macOS, Linux, Windows.
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Set, Tuple

PROG = "modeguard"
ALLOW_NAME = ".modeguard.allow"
ARMS = ("worktree", "index", "branch")
MODE = re.compile(r"[0-7]{6}")
ZERO_MODE = "000000"
PLANT_ARM = "planted"
HOOK_MARK = "modeguard-managed hook"
FILEMODE_OFF_NOTE = (f"{PROG}: core.fileMode is false in this repository: the working-tree arm is "
                     "skipped (git cannot see mode changes there); judging the index and branch arms")


class Change(NamedTuple):
    arm: str
    old: str
    new: str
    path: str
    source: str = ""


class Unjudgeable(Exception):
    """The check cannot be run; the message names why."""


# ---------------------------------------------------------------------------
# Reading git
# ---------------------------------------------------------------------------

def git_run(repo: Path, *args: str) -> "subprocess.CompletedProcess[bytes]":
    try:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    except OSError as exc:
        raise Unjudgeable(f"cannot run git: {exc}") from exc


def git(repo: Path, *args: str) -> bytes:
    res = git_run(repo, *args)
    if res.returncode != 0:
        detail = res.stderr.decode("utf-8", "replace").strip()
        raise Unjudgeable(f"git {' '.join(args)}: {detail}")
    return res.stdout


def toplevel(repo: Path) -> Path:
    out = git(repo, "rev-parse", "--show-toplevel").decode("utf-8", "replace").strip()
    if not out:
        raise Unjudgeable(f"{repo} is not inside a git work tree")
    return Path(out)


def parse_raw(arm: str, out: bytes) -> List[Change]:
    """The mode changes in `git diff --raw -z` output.

    Records are `:<old> <new> <old-sha> <new-sha> <status>` NUL `<path>` NUL, or for a rename
    (status `R...`) `... NUL <source> NUL <destination>` NUL.
    """
    parts = out.split(b"\0")
    changes: List[Change] = []
    i = 0
    while i + 1 < len(parts):
        meta = parts[i].decode("utf-8", "replace")
        fields = meta.lstrip(":").split(" ")
        if len(fields) < 5:
            raise Unjudgeable(f"unreadable diff record: {meta!r}")
        old, new, status = fields[0], fields[1], fields[4]
        if not (MODE.fullmatch(old) and MODE.fullmatch(new)):
            raise Unjudgeable(f"unreadable modes in diff record: {meta!r}")
        i += 1
        source = ""
        if status.startswith("R"):
            if i + 2 > len(parts):
                raise Unjudgeable(f"unreadable rename record: {meta!r}")
            source, path = (parts[i].decode("utf-8", "replace"),
                            parts[i + 1].decode("utf-8", "replace"))
            i += 2
        else:
            path = parts[i].decode("utf-8", "replace")
            i += 1
        if old != new and ZERO_MODE not in (old, new):
            changes.append(Change(arm, old, new, path, source))
    return changes


def diff_arm(repo: Path, arm: str, *rev: str) -> List[Change]:
    out = git(repo, "diff", "--raw", "-z", "--find-renames=100%", *rev)
    return parse_raw(arm, out)


def file_mode_enabled(repo: Path) -> bool:
    """git's own reading of core.fileMode (true, yes, on, 1 / false, no, off, 0); unset is true."""
    res = git_run(repo, "config", "--bool", "--get", "core.fileMode")
    if res.returncode == 0:
        return res.stdout.decode("utf-8", "replace").strip() != "false"
    if res.returncode == 1:
        return True
    raise Unjudgeable("core.fileMode is set to something git does not read as a boolean")


def ref_exists(repo: Path, rev: str) -> bool:
    return git_run(repo, "rev-parse", "--verify", "--quiet", rev).returncode == 0


def resolve_base(repo: Path, explicit: Optional[str]) -> Tuple[str, str]:
    """(merge-base hash, description of the ref used) for the branch arm."""
    if not ref_exists(repo, "HEAD"):
        raise Unjudgeable("HEAD has no commit yet, so there is no branch to judge (try --index-only)")
    candidates: List[Tuple[str, str]] = []
    if explicit:
        if explicit.startswith("-"):
            raise Unjudgeable(f"--base {explicit!r} is not a ref")
        if not ref_exists(repo, explicit + "^{commit}"):
            raise Unjudgeable(f"--base {explicit}: no such commit or ref")
        candidates.append((explicit + "^{commit}", f"{explicit} (--base)"))
    else:
        head = git_run(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
        if head.returncode == 0:
            target = head.stdout.decode("utf-8", "replace").strip()
            if target.startswith("refs/remotes/"):
                candidates.append((target, f"{target[len('refs/remotes/'):]} (origin/HEAD)"))
        for name in ("main", "master"):
            candidates.append((f"refs/remotes/origin/{name}", f"origin/{name}"))
            candidates.append((f"refs/heads/{name}", f"{name} (no origin/{name})"))
    for ref, via in candidates:
        if not ref_exists(repo, ref):
            continue
        res = git_run(repo, "merge-base", ref, "HEAD")
        if res.returncode != 0:
            raise Unjudgeable(f"{via}: no merge base with HEAD")
        return res.stdout.decode().strip(), via
    raise Unjudgeable("no origin/HEAD, main or master to take the branch's merge base from "
                      "(name one with --base REF)")


# ---------------------------------------------------------------------------
# The allow list
# ---------------------------------------------------------------------------

def read_allow(path: Path, must_exist: bool, label: str) -> Set[Tuple[str, str, str]]:
    """The (old, new, path) triples in the allow list; an absent default file allows nothing."""
    allowed: Set[Tuple[str, str, str]] = set()
    if not path.is_file():
        if must_exist:
            raise Unjudgeable(f"allow list {label} not found")
        return allowed
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise Unjudgeable(f"allow list {label} cannot be read: {exc}") from exc
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        entry, sep, reason = line.partition(" # ")
        fields = entry.split(" ", 2)
        if (not sep or not reason.strip() or len(fields) != 3
                or not MODE.fullmatch(fields[0]) or not MODE.fullmatch(fields[1])
                or not fields[2].strip()):
            raise Unjudgeable(f"{label}:{n}: not '<old-mode> <new-mode> <path>  # <reason>'")
        allowed.add((fields[0], fields[1], fields[2].strip()))
    return allowed


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------

def check(repo: Path, allow: Optional[Path] = None, use_allow: bool = True,
          plant: Optional[Tuple[str, str, str]] = None, index_only: bool = False,
          base: Optional[str] = None, bypass_hint: bool = False) -> Tuple[int, List[str]]:
    """(exit code, output lines) for the repository at `repo`."""
    lines: List[str] = []
    label = str(allow) if allow else ALLOW_NAME
    try:
        top = toplevel(repo)
        allow_path = allow if allow else top / ALLOW_NAME
        allowed = read_allow(allow_path, allow is not None, label) if use_allow else set()
        found: List[Change] = []
        if index_only:
            found += diff_arm(top, "index", "--cached")
            scope = "index (index only)"
        else:
            if file_mode_enabled(top):
                found += diff_arm(top, "worktree")
                arms = 3
            else:
                lines.append(FILEMODE_OFF_NOTE)
                arms = 2
            found += diff_arm(top, "index", "--cached")
            base_hash, via = resolve_base(top, base)
            found += diff_arm(top, "branch", base_hash, "HEAD")
            scope = f"{arms} arm(s) (base {base_hash[:8]} via {via})"
    except Unjudgeable as exc:
        return 2, [f"{PROG}: cannot judge: {exc}"]
    if plant is not None:
        found.append(Change(PLANT_ARM, *plant))
    bad = [c for c in found if (c.old, c.new, c.path) not in allowed]
    for c in bad:
        lines.append(f"MODE-CHANGE {c.arm} {c.old} => {c.new} {c.path}"
                     + (f" (renamed from {c.source})" if c.source else ""))
    if not index_only:
        seen = {(c.old, c.new, c.path) for c in found}
        for triple in sorted(allowed - seen):
            lines.append(f"ALLOW-UNUSED {' '.join(triple)}")
    if bad:
        hint = (f"{PROG}: restore the mode (chmod, or git update-index --chmod=+x / -x; a cp over the "
                f"file drops it), or list a deliberate change in {label} as "
                "'<old-mode> <new-mode> <path>  # <reason>'")
        if bypass_hint:
            hint += ", or commit once with: git commit --no-verify"
        lines.append(hint)
    lines.append(f"{PROG}: {'FAIL' if bad else 'OK'} -- {len(bad)} mode change(s) in {scope}, "
                 f"{len(found) - len(bad)} allowlisted")
    return (1 if bad else 0), lines


def run_hook(repo: Path, allow: Optional[Path], use_allow: bool) -> int:
    """What the installed hook runs: quiet when clean, stderr otherwise, fail open if unjudgeable."""
    rc, lines = check(repo, allow, use_allow, index_only=True, bypass_hint=True)
    if rc == 0:
        return 0
    print("\n".join(lines), file=sys.stderr)
    if rc == 1:
        return 1
    print(f"{PROG}: the mode check could not be judged; commit allowed", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# The hook installer
# ---------------------------------------------------------------------------

def hook_text(python: str, script: str) -> str:
    q_py, q_script = shlex.quote(python), shlex.quote(script)
    return (
        "#!/bin/sh\n"
        f"# {HOOK_MARK}: written by modeguard.py --install-hook; remove it with --uninstall-hook.\n"
        f"py={q_py}\n"
        f"script={q_script}\n"
        'if [ ! -f "$script" ] || [ ! -x "$py" ]; then\n'
        '    echo "modeguard: $script or $py is missing; mode check skipped '
        '(reinstall with --install-hook)" >&2\n'
        "    exit 0\n"
        "fi\n"
        'exec "$py" "$script" --hook\n'
    )


def hook_path(top: Path) -> Path:
    out = git(top, "rev-parse", "--git-path", "hooks").decode("utf-8", "replace").strip()
    hooks = Path(out)
    if not hooks.is_absolute():
        hooks = top / hooks
    return hooks / "pre-commit"


def is_own_hook(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        head = path.read_bytes()[:400].decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return HOOK_MARK in "\n".join(head.splitlines()[:3])


def install_hook(repo: Path) -> int:
    try:
        top = toplevel(repo)
        path = hook_path(top)
    except Unjudgeable as exc:
        print(f"{PROG}: cannot install the hook: {exc}")
        return 2
    python = sys.executable
    if not python:
        print(f"{PROG}: cannot install the hook: this Python has no known executable path")
        return 2
    script = Path(__file__).resolve()
    text = hook_text(Path(python).as_posix(), script.as_posix())
    command = f"{shlex.quote(Path(python).as_posix())} {shlex.quote(script.as_posix())} --hook"
    if (path.exists() or path.is_symlink()) and not is_own_hook(path):
        print(f"{PROG}: {path} already exists and is not modeguard's; left alone")
        print(f"{PROG}: to run both, add this line to that hook (a non-zero exit refuses the commit): "
              f"{command} || exit 1")
        return 1
    refreshed = path.exists()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.chmod(str(path), 0o755)
    except OSError as exc:
        print(f"{PROG}: cannot write {path}: {exc}")
        return 2
    print(f"{PROG}: {'refreshed' if refreshed else 'installed'} the pre-commit hook at {path}")
    print(f"{PROG}: it runs {command}; undo with: {PROG}.py --uninstall-hook")
    return 0


def uninstall_hook(repo: Path) -> int:
    try:
        path = hook_path(toplevel(repo))
    except Unjudgeable as exc:
        print(f"{PROG}: cannot remove the hook: {exc}")
        return 2
    if not (path.exists() or path.is_symlink()):
        print(f"{PROG}: no pre-commit hook at {path}; nothing to remove")
        return 0
    if not is_own_hook(path):
        print(f"{PROG}: {path} is not modeguard's; left alone")
        return 0
    try:
        path.unlink()
    except OSError as exc:
        print(f"{PROG}: cannot remove {path}: {exc}")
        return 2
    print(f"{PROG}: removed the pre-commit hook at {path}")
    return 0


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def main(argv: List[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(errors="replace")
    parser = argparse.ArgumentParser(
        prog=PROG, description="Refuse a file-mode change on a tracked path unless it is allow-listed.")
    parser.add_argument("--repo", default=".", metavar="DIR",
                        help="The repository to judge (default: the current directory).")
    allow_group = parser.add_mutually_exclusive_group()
    allow_group.add_argument("--allow", default=None, metavar="PATH",
                             help=f"The allow list (default: {ALLOW_NAME} at the repository root).")
    allow_group.add_argument("--no-allow", action="store_true", help="Ignore the allow list.")
    parser.add_argument("--base", default=None, metavar="REF",
                        help="The ref the branch arm compares HEAD with (default: origin/HEAD's "
                             "branch, else main, else master).")
    parser.add_argument("--index-only", action="store_true",
                        help="Judge only the index against HEAD (the set a commit would record).")
    parser.add_argument("--plant", default=None, metavar='"OLD NEW PATH"',
                        help="Also judge a planted change, to prove a path is or is not allowed.")
    parser.add_argument("--hook", action="store_true",
                        help="What the installed hook runs (index only, quiet when clean).")
    parser.add_argument("--install-hook", action="store_true",
                        help="Write a git pre-commit hook that runs this script.")
    parser.add_argument("--uninstall-hook", action="store_true",
                        help="Remove a pre-commit hook that --install-hook wrote.")
    parser.add_argument("--self-test", action="store_true", help="Run the built-in cases and exit.")
    args = parser.parse_args(argv)

    if args.self_test:
        return _run_self_test()
    repo = Path(args.repo)
    judging = (args.allow is not None or args.no_allow or args.base is not None
               or args.index_only or args.plant is not None or args.hook)
    if args.install_hook or args.uninstall_hook:
        if args.install_hook and args.uninstall_hook:
            parser.error("--install-hook and --uninstall-hook cannot be used together")
        if judging:
            parser.error("--install-hook and --uninstall-hook take only --repo")
        return install_hook(repo) if args.install_hook else uninstall_hook(repo)
    if args.base is not None and (args.index_only or args.hook):
        parser.error("--base has no meaning with --index-only: that judges no branch")
    plant: Optional[Tuple[str, str, str]] = None
    if args.plant is not None:
        fields = args.plant.split(" ", 2)
        if len(fields) != 3 or not all(MODE.fullmatch(f) for f in fields[:2]) or not fields[2].strip():
            parser.error("--plant wants '<old-mode> <new-mode> <path>'")
        plant = (fields[0], fields[1], fields[2])
    allow = Path(args.allow) if args.allow is not None else None
    if args.hook:
        return run_hook(repo, allow, not args.no_allow)
    rc, lines = check(repo, allow, use_allow=not args.no_allow, plant=plant,
                      index_only=args.index_only, base=args.base)
    print("\n".join(lines))
    return rc


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

SCRIPT = Path(__file__).resolve()
POSIX = os.name == "posix"
# git must read none of the caller's configuration, hooks, or in-flight hook state.
_DROP_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX", "GIT_COMMON_DIR",
             "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
             "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_TEMPLATE_DIR")


def _write(path: Path, text: str) -> None:
    """Write UTF-8 text exactly as given: no newline translation on any platform."""
    with open(str(path), "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


class _Skip(Exception):
    """A case that cannot run on this platform."""


class _T:
    """Collects the failed expectations of one case."""

    def __init__(self) -> None:
        self.problems: List[str] = []

    def ok(self, label: str, cond: bool, detail: str = "") -> None:
        if not cond:
            self.problems.append(label + (f" [{detail.strip()}]" if detail.strip() else ""))

    def run(self, label: str, res: Tuple[int, str], rc: int,
            *needles: str, absent: Tuple[str, ...] = ()) -> None:
        code, text = res
        missing = [n for n in needles if n not in text]
        present = [a for a in absent if a in text]
        if code != rc or missing or present:
            why = f"exit {code} (want {rc})"
            if missing:
                why += f", missing {missing}"
            if present:
                why += f", should be absent {present}"
            self.problems.append(f"{label}: {why} -- output: {text.strip()[:600]}")


def _git(root: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def _git_rc(root: Path, *args: str) -> Tuple[int, str]:
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.returncode, r.stdout + r.stderr


def _mg(root: Path, *args: str, script: Optional[Path] = None,
        with_repo: bool = True) -> Tuple[int, str]:
    cmd = [sys.executable, str(script or SCRIPT)]
    if with_repo:
        cmd += ["--repo", str(root)]
    r = subprocess.run(cmd + list(args), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.returncode, r.stdout + r.stderr


def _make_repo(root: Path, branch: str = "main", origin: bool = True) -> Path:
    """A clean scratch repository: three tracked scripts (two executable) on `branch`."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
    _git(root, "config", "core.fileMode", "true" if POSIX else "false")
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "config", "user.name", "modeguard test")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "tools").mkdir(exist_ok=True)
    for name, ex in (("a.sh", True), ("b.sh", False), ("sp ace.sh", True)):
        p = root / "tools" / name
        _write(p, "#!/bin/sh\necho hi\n")
        if POSIX:
            p.chmod(0o755 if ex else 0o644)
    _git(root, "add", "-A")
    _git(root, "update-index", "--chmod=+x", "tools/a.sh", "tools/sp ace.sh")
    _git(root, "commit", "-q", "-m", "base")
    if origin:
        _git(root, "update-ref", f"refs/remotes/origin/{branch}", "HEAD")
    return root


def _stage_mode(root: Path, rel: str, executable: bool) -> None:
    """Change a tracked file's mode in the index (and on disk where the platform has modes)."""
    if POSIX:
        (root / rel).chmod(0o755 if executable else 0o644)
    _git(root, "update-index", "--chmod=+x" if executable else "--chmod=-x", "--", rel)


def _worktree_chmod(root: Path, rel: str, executable: bool) -> None:
    if not POSIX:
        raise _Skip("needs POSIX file modes")
    (root / rel).chmod(0o755 if executable else 0o644)


def _commit_rc(root: Path, *args: str) -> Tuple[int, str]:
    return _git_rc(root, "commit", "-q", *args)


def _fresh(td: Path, **kw: object) -> Path:
    return _make_repo(td / "repo", **kw)  # type: ignore[arg-type]


# --- the arms -------------------------------------------------------------

def _case_clean_tree(t: _T, td: Path) -> None:
    r = _fresh(td)
    t.run("clean tree", _mg(r), 0, "0 mode change(s)", "via origin/main", "OK")


def _case_worktree_arm(t: _T, td: Path) -> None:
    # RED ARM: chmod -x in the working tree must fail by name; restoring turns it green.
    r = _fresh(td)
    _worktree_chmod(r, "tools/a.sh", False)
    t.run("chmod -x in the working tree", _mg(r), 1,
          "MODE-CHANGE worktree 100755 => 100644 tools/a.sh", "FAIL")
    t.run("--no-allow on a plain drift", _mg(r, "--no-allow"), 1)
    _worktree_chmod(r, "tools/a.sh", True)
    t.run("chmod restored is green again", _mg(r), 0, "0 mode change(s)")
    _worktree_chmod(r, "tools/b.sh", True)
    t.run("chmod +x in the working tree", _mg(r), 1,
          "MODE-CHANGE worktree 100644 => 100755 tools/b.sh")


def _case_index_arm(t: _T, td: Path) -> None:
    # RED ARM: a staged mode change must fail by name, from the index arm.
    r = _fresh(td)
    _stage_mode(r, "tools/a.sh", False)
    res = _mg(r)
    t.run("staged mode change", res, 1, "MODE-CHANGE index 100755 => 100644 tools/a.sh")
    t.ok("the working-tree arm does not repeat a staged change", " worktree " not in res[1], res[1])
    sp = _fresh_again(td, "space")
    _stage_mode(sp, "tools/sp ace.sh", False)
    t.run("a path with a space comes back whole", _mg(sp), 1,
          "100755 => 100644 tools/sp ace.sh")


def _fresh_again(td: Path, name: str, **kw: object) -> Path:
    return _make_repo(td / name, **kw)  # type: ignore[arg-type]


def _case_branch_arm(t: _T, td: Path) -> None:
    # RED ARM: a committed mode change must fail by name, from the branch arm.
    r = _fresh(td)
    _git(r, "switch", "-q", "-c", "topic")
    _stage_mode(r, "tools/a.sh", False)
    _git(r, "commit", "-q", "-m", "flip a.sh")
    t.run("committed mode change", _mg(r), 1, "MODE-CHANGE branch 100755 => 100644 tools/a.sh")


def _case_not_mode_changes(t: _T, td: Path) -> None:
    r = _fresh(td)
    _git(r, "switch", "-q", "-c", "topic")
    _write(r / "tools" / "c.sh", "#!/bin/sh\n")
    _git(r, "add", "tools/c.sh")
    _git(r, "update-index", "--chmod=+x", "tools/c.sh")
    _write(r / "tools" / "a.sh", "#!/bin/sh\necho changed\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "add c.sh, edit a.sh")
    t.run("a new executable file and a content edit are green", _mg(r), 0, "0 mode change(s)")
    _git(r, "rm", "-q", "tools/b.sh")
    t.run("a deleted file is green", _mg(r), 0, "0 mode change(s)")


def _case_renames(t: _T, td: Path) -> None:
    r = _fresh(td)
    _git(r, "mv", "tools/a.sh", "tools/c.sh")
    t.run("a plain rename is green", _mg(r), 0, "0 mode change(s)")
    _stage_mode(r, "tools/c.sh", False)
    note = "MODE-CHANGE index 100755 => 100644 tools/c.sh (renamed from tools/a.sh)"
    t.run("a staged rename plus chmod -x", _mg(r), 1, note)
    t.run("index-only sees a staged rename plus chmod -x", _mg(r, "--index-only"), 1, note)
    rb = _fresh_again(td, "rename-branch")
    _git(rb, "switch", "-q", "-c", "topic")
    _git(rb, "mv", "tools/b.sh", "tools/e.sh")
    _stage_mode(rb, "tools/e.sh", True)
    _git(rb, "commit", "-q", "-m", "rename b.sh, chmod +x")
    t.run("a committed rename plus chmod +x", _mg(rb), 1,
          "MODE-CHANGE branch 100644 => 100755 tools/e.sh (renamed from tools/b.sh)")
    _write(rb / ALLOW_NAME, "100644 100755 tools/e.sh  # e.sh runs now\n")
    res = _mg(rb)
    t.run("an allowlisted rename is green", res, 0, "1 allowlisted")
    t.ok("a used allow row is not reported as unused", "ALLOW-UNUSED" not in res[1], res[1])


def _case_type_change(t: _T, td: Path) -> None:
    r = _fresh(td)
    blob = _git(r, "rev-parse", "HEAD:tools/b.sh").strip()
    _git(r, "update-index", "--cacheinfo", f"120000,{blob},tools/b.sh")
    t.run("a file turned into a symlink is a finding", _mg(r, "--index-only"), 1,
          "MODE-CHANGE index 100644 => 120000 tools/b.sh")


# --- the allow list -------------------------------------------------------

def _drifted_branch(td: Path) -> Path:
    r = _fresh(td)
    _git(r, "switch", "-q", "-c", "topic")
    _stage_mode(r, "tools/a.sh", False)
    _git(r, "commit", "-q", "-m", "flip a.sh")
    return r


def _case_allow_list(t: _T, td: Path) -> None:
    r = _drifted_branch(td)
    allow = r / ALLOW_NAME
    _write(allow, "# a comment\n\n100755 100644 tools/a.sh  # a.sh is data now\n")
    t.run("an allowlisted change is green", _mg(r), 0, "1 allowlisted", "OK")
    t.run("--no-allow ignores the allow list", _mg(r, "--no-allow"), 1, "MODE-CHANGE branch")
    _write(allow, "100644 100755 tools/a.sh  # wrong direction\n")
    t.run("modes swapped does not clear it", _mg(r), 1, "MODE-CHANGE branch")
    _write(allow, "100755 100644 tools/b.sh  # wrong path\n")
    t.run("another path does not clear it", _mg(r), 1, "MODE-CHANGE branch",
          "ALLOW-UNUSED 100755 100644 tools/b.sh")
    _write(allow, "100755 100644 tools/a.sh  # crlf line\r\n")
    t.run("a CRLF allow list is read", _mg(r), 0, "1 allowlisted")


def _case_allow_malformed(t: _T, td: Path) -> None:
    # RED ARM: an allow row without a reason, or with one mode, is refused with exit 2.
    r = _drifted_branch(td)
    allow = r / ALLOW_NAME
    for label, row in (("no reason", "100755 100644 tools/a.sh\n"),
                       ("empty reason", "100755 100644 tools/a.sh  # \n"),
                       ("one mode", "100755 tools/a.sh  # one mode only\n"),
                       ("bad mode", "100755 10064 tools/a.sh  # five digits\n")):
        _write(allow, row)
        t.run(f"allow row with {label} is refused", _mg(r), 2, "cannot judge", f"{ALLOW_NAME}:1")


def _case_allow_override(t: _T, td: Path) -> None:
    r = _drifted_branch(td)
    other = td / "elsewhere.allow"
    _write(other, "100755 100644 tools/a.sh  # kept outside the repository\n")
    t.run("--allow PATH clears the change", _mg(r, "--allow", str(other)), 0, "1 allowlisted")
    _write(r / ALLOW_NAME, "100755 100644 tools/a.sh  # default file\n")
    _write(other, "100755 100644 tools/b.sh  # a different row\n")
    t.run("--allow replaces the default file, it does not add to it", _mg(r, "--allow", str(other)), 1,
          "MODE-CHANGE branch")
    t.run("a named allow list that is absent is refused", _mg(r, "--allow", str(td / "nope.allow")), 2,
          "cannot judge", "not found")
    t.run("--allow with --no-allow is a usage error", _mg(r, "--allow", str(other), "--no-allow"), 2)


def _case_allow_unused(t: _T, td: Path) -> None:
    r = _fresh(td)
    _write(r / ALLOW_NAME, "100755 100644 tools/gone.sh  # merged long ago\n")
    t.run("an unused allow row is reported, exit unchanged", _mg(r), 0,
          "ALLOW-UNUSED 100755 100644 tools/gone.sh", "0 mode change(s)")
    t.run("index-only does not report unused rows", _mg(r, "--index-only"), 0,
          absent=("ALLOW-UNUSED",))


# --- the branch base ------------------------------------------------------

def _flip_on_topic(r: Path) -> None:
    _git(r, "switch", "-q", "-c", "topic")
    _stage_mode(r, "tools/a.sh", False)
    _git(r, "commit", "-q", "-m", "flip a.sh")


def _case_base_fallbacks(t: _T, td: Path) -> None:
    r = _fresh(td, origin=False)
    _flip_on_topic(r)
    t.run("main without origin/main", _mg(r), 1,
          "MODE-CHANGE branch 100755 => 100644 tools/a.sh", "via main (no origin/main)")
    _git(r, "branch", "-q", "-m", "main", "trunk")
    t.run("no base ref is refused", _mg(r), 2, "cannot judge", "--base REF")
    t.run("--base names the ref", _mg(r, "--base", "trunk"), 1,
          "via trunk (--base)", "MODE-CHANGE branch")
    t.run("--base with an unknown ref is refused", _mg(r, "--base", "nope"), 2,
          "cannot judge", "no such commit or ref")
    t.run("--base with --index-only is a usage error", _mg(r, "--base", "trunk", "--index-only"), 2)


def _case_master_only(t: _T, td: Path) -> None:
    # RED ARM: a repository whose trunk is `master` is judged, and the verdict names master.
    r = _fresh(td, branch="master", origin=False)
    _flip_on_topic(r)
    t.run("master-only repository", _mg(r), 1,
          "MODE-CHANGE branch 100755 => 100644 tools/a.sh", "via master (no origin/master)")
    rr = _fresh_again(td, "master-origin", branch="master")
    _flip_on_topic(rr)
    t.run("origin/master", _mg(rr), 1, "via origin/master")
    both = _fresh_again(td, "both", origin=False)
    _git(both, "branch", "-q", "master")
    _flip_on_topic(both)
    t.run("main is preferred over master", _mg(both), 1, "via main (no origin/main)")


def _case_origin_head(t: _T, td: Path) -> None:
    r = _fresh(td, branch="trunk")
    _git(r, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    _git(r, "branch", "-q", "main")
    _flip_on_topic(r)
    t.run("origin/HEAD's branch is the base", _mg(r), 1, "via origin/trunk (origin/HEAD)",
          "MODE-CHANGE branch")


def _case_no_commit_yet(t: _T, td: Path) -> None:
    r = td / "empty"
    r.mkdir()
    _git(r, "init", "-q")
    t.run("a repository with no commit is refused", _mg(r), 2, "cannot judge", "--index-only")
    t.run("--index-only judges it", _mg(r, "--index-only"), 0, "0 mode change(s)")


# --- core.fileMode --------------------------------------------------------

def _case_filemode_off(t: _T, td: Path) -> None:
    # RED ARM: with core.fileMode off the working-tree arm is skipped and the rest are judged.
    r = _fresh(td)
    _git(r, "switch", "-q", "-c", "topic")
    for spelling in ("false", "no", "0", "off"):
        _git(r, "config", "core.fileMode", spelling)
        t.run(f"core.fileMode={spelling} is judged, with a clear line", _mg(r), 0,
              "core.fileMode is false", "working-tree arm is skipped", "2 arm(s)", "OK")
    _git(r, "config", "core.fileMode", "false")
    _stage_mode(r, "tools/b.sh", True)
    t.run("a staged change is still caught", _mg(r), 1,
          "MODE-CHANGE index 100644 => 100755 tools/b.sh", "working-tree arm is skipped")
    _git(r, "commit", "-q", "-m", "flip b.sh")
    t.run("a committed change is still caught", _mg(r), 1,
          "MODE-CHANGE branch 100644 => 100755 tools/b.sh")
    if POSIX:
        on = _fresh_again(td, "on")
        t.run("core.fileMode on judges 3 arms with no note", _mg(on), 0,
              "3 arm(s)", absent=("core.fileMode is false",))
    t.run("--index-only prints no core.fileMode line", _mg(r, "--index-only"), 0,
          "index only", absent=("core.fileMode",))


# --- index-only, plant, misc ----------------------------------------------

def _case_index_only(t: _T, td: Path) -> None:
    r = _fresh(td, origin=False)
    _git(r, "branch", "-q", "-m", "main", "trunk")
    _git(r, "config", "core.fileMode", "false")
    t.run("index-only needs no base ref and no core.fileMode", _mg(r, "--index-only"), 0,
          "0 mode change(s) in index (index only)")
    _git(r, "config", "core.fileMode", "true" if POSIX else "false")
    if POSIX:
        _worktree_chmod(r, "tools/a.sh", False)
        t.run("index-only ignores an unstaged chmod", _mg(r, "--index-only"), 0, "0 mode change(s)")
    _stage_mode(r, "tools/a.sh", False)
    t.run("index-only flags a staged chmod -x", _mg(r, "--index-only"), 1,
          "MODE-CHANGE index 100755 => 100644 tools/a.sh", "index only")
    _write(r / ALLOW_NAME, "100755 100644 tools/a.sh  # data now\n")
    t.run("index-only honours the allow list", _mg(r, "--index-only"), 0, "1 allowlisted")
    t.run("index-only --no-allow", _mg(r, "--index-only", "--no-allow"), 1)
    _write(r / ALLOW_NAME, "garbage\n")
    t.run("index-only refuses a bad allow list", _mg(r, "--index-only"), 2, "cannot judge")
    (r / ALLOW_NAME).unlink()
    _git(r, "commit", "-q", "-m", "flip a.sh")
    t.run("index-only does not judge a committed change", _mg(r, "--index-only"), 0,
          "0 mode change(s)")
    _git(r, "update-ref", "-d", "refs/heads/trunk")
    _git(r, "checkout", "-q", "--orphan", "fresh")
    _git(r, "rm", "-q", "-r", "--cached", ".")
    _git(r, "add", "-A")
    t.run("index-only on an unborn branch: all-new files are creations",
          _mg(r, "--index-only"), 0, "0 mode change(s)")


def _case_plant(t: _T, td: Path) -> None:
    r = _fresh(td)
    t.run("a planted change is judged like a finding",
          _mg(r, "--plant", "100755 100644 tools/p.sh"), 1,
          "MODE-CHANGE planted 100755 => 100644 tools/p.sh")
    _write(r / ALLOW_NAME, "100755 100644 tools/p.sh  # allowed here\n")
    t.run("a planted change can be allowed", _mg(r, "--plant", "100755 100644 tools/p.sh"), 0,
          "1 allowlisted")
    t.run("a malformed plant is a usage error", _mg(r, "--plant", "755 644 x"), 2)


def _case_not_a_repository(t: _T, td: Path) -> None:
    d = td / "plain"
    d.mkdir()
    t.run("a directory that is not a repository is refused", _mg(d), 2, "cannot judge")
    t.run("--install-hook there is refused", _mg(d, "--install-hook"), 2, "cannot install")
    t.run("a directory that does not exist is refused", _mg(td / "absent"), 2, "cannot judge")


def _case_reports_only(t: _T, td: Path) -> None:
    r = _fresh(td)
    _worktree_chmod(r, "tools/a.sh", False)
    before = (_git(r, "ls-files", "-s"), os.stat(str(r / "tools" / "a.sh")).st_mode,
              _git(r, "status", "--porcelain"))
    _mg(r)
    after = (_git(r, "ls-files", "-s"), os.stat(str(r / "tools" / "a.sh")).st_mode,
             _git(r, "status", "--porcelain"))
    t.ok("modeguard changes no mode, index entry or file", before == after, f"{before} != {after}")


# --- the installer and the installed hook ---------------------------------

def _bytes(p: Path) -> bytes:
    return p.read_bytes() if p.is_file() else b""


def _hook_file(r: Path) -> Path:
    return r / ".git" / "hooks" / "pre-commit"


def _case_install_writes_hook(t: _T, td: Path) -> None:
    r = _fresh(td)
    res = _mg(r, "--install-hook")
    t.run("install", res, 0, "installed the pre-commit hook", "--uninstall-hook")
    h = _hook_file(r)
    data = _bytes(h)
    t.ok("the hook file exists", h.is_file())
    t.ok("the hook is sh with LF endings", data.startswith(b"#!/bin/sh\n") and b"\r" not in data)
    t.ok("the hook is marked as modeguard's", HOOK_MARK.encode() in data)
    t.ok("the hook calls this script", SCRIPT.as_posix().encode() in data and b"--hook" in data)
    if POSIX:
        t.ok("the hook is executable", os.access(str(h), os.X_OK))
    t.run("install again refreshes its own hook", _mg(r, "--install-hook"), 0, "refreshed")
    t.run("--install-hook with --index-only is a usage error",
          _mg(r, "--install-hook", "--index-only"), 2)


def _case_hook_refuses_mode_change(t: _T, td: Path) -> None:
    # RED ARM: the installed hook refuses a 100755 -> 100644 commit, naming change, list, bypass.
    r = _fresh(td)
    t.run("install", _mg(r, "--install-hook"), 0)
    _write(r / "tools" / "new.txt", "x\n")
    _git(r, "add", "tools/new.txt")
    t.run("an unrelated commit passes", _commit_rc(r, "-m", "plain"), 0)
    _stage_mode(r, "tools/a.sh", False)
    rc, text = _commit_rc(r, "-m", "flip")
    t.ok("a staged chmod -x is refused", rc != 0, text)
    t.ok("the refusal names the change", "MODE-CHANGE index 100755 => 100644 tools/a.sh" in text, text)
    t.ok("the refusal names the allow list", ALLOW_NAME in text, text)
    t.ok("the refusal names the bypass", "--no-verify" in text, text)
    t.ok("nothing was committed", _git(r, "log", "--oneline").count("\n") == 2, text)
    t.run("git commit --no-verify bypasses the hook", _commit_rc(r, "--no-verify", "-m", "bypass"), 0)


def _case_hook_allows_allowed(t: _T, td: Path) -> None:
    r = _fresh(td)
    t.run("install", _mg(r, "--install-hook"), 0)
    _stage_mode(r, "tools/a.sh", False)
    _write(r / ALLOW_NAME, "100755 100644 tools/a.sh  # a.sh is data now\n")
    t.run("an allowlisted chmod -x commits", _commit_rc(r, "-m", "flip"), 0)
    _git(r, "add", ALLOW_NAME)
    t.run("the allow list itself commits", _commit_rc(r, "-m", "allow"), 0)
    _stage_mode(r, "tools/b.sh", True)
    rc, _ = _commit_rc(r, "-m", "plus")
    t.ok("an unlisted chmod +x is refused", rc != 0)
    _git(r, "reset", "-q", "--hard")
    new = r / "tools" / "new.sh"
    _write(new, "#!/bin/sh\n")
    _git(r, "add", "tools/new.sh")
    _git(r, "update-index", "--chmod=+x", "tools/new.sh")
    t.run("a new 100755 file commits", _commit_rc(r, "-m", "newexec"), 0)
    t.ok("new.sh is 100755", _git(r, "ls-files", "-s", "tools/new.sh").startswith("100755"))
    if POSIX:
        _worktree_chmod(r, "tools/new.sh", False)
        _write(r / "tools" / "other.txt", "y\n")
        _git(r, "add", "tools/other.txt")
        t.run("an unstaged chmod does not block an unrelated commit",
              _commit_rc(r, "-m", "other"), 0)


def _case_install_refuses_foreign(t: _T, td: Path) -> None:
    # RED ARM: a pre-commit hook that is not modeguard's is never overwritten.
    r = _fresh(td)
    h = _hook_file(r)
    h.parent.mkdir(parents=True, exist_ok=True)
    mine = b"#!/bin/sh\necho my own hook\n"
    h.write_bytes(mine)
    res = _mg(r, "--install-hook")
    t.run("install over a foreign hook is refused", res, 1, "not modeguard's", "left alone", "--hook")
    t.ok("the foreign hook is byte-for-byte unchanged", _bytes(h) == mine)
    t.run("uninstall leaves a foreign hook alone", _mg(r, "--uninstall-hook"), 0, "left alone")
    t.ok("the foreign hook survived uninstall", _bytes(h) == mine)
    if POSIX:
        r2 = _fresh_again(td, "link")
        target = td / "shared-hook"
        target.write_bytes(mine)
        _hook_file(r2).parent.mkdir(parents=True, exist_ok=True)
        os.symlink(str(target), str(_hook_file(r2)))
        t.run("a symlinked hook is treated as foreign", _mg(r2, "--install-hook"), 1, "left alone")
        t.ok("the symlink target is unchanged", _bytes(target) == mine)


def _case_hooks_path(t: _T, td: Path) -> None:
    r = _fresh(td)
    _git(r, "config", "core.hooksPath", "shared-hooks")
    t.run("install into core.hooksPath", _mg(r, "--install-hook"), 0, "shared-hooks")
    t.ok("the hook is in core.hooksPath, not .git/hooks",
         (r / "shared-hooks" / "pre-commit").is_file() and not _hook_file(r).exists())
    _stage_mode(r, "tools/a.sh", False)
    rc, text = _commit_rc(r, "-m", "flip")
    t.ok("that hook refuses the commit", rc != 0 and "MODE-CHANGE" in text, text)


def _case_uninstall(t: _T, td: Path) -> None:
    r = _fresh(td)
    t.run("uninstall with no hook is quiet and exits 0", _mg(r, "--uninstall-hook"), 0, "nothing to remove")
    t.run("install", _mg(r, "--install-hook"), 0)
    t.run("uninstall removes its own hook", _mg(r, "--uninstall-hook"), 0, "removed")
    t.ok("the hook file is gone", not _hook_file(r).exists())
    _stage_mode(r, "tools/a.sh", False)
    t.run("after uninstall a chmod -x commits", _commit_rc(r, "-m", "flip"), 0)


def _case_hook_skips_when_script_gone(t: _T, td: Path) -> None:
    r = _fresh(td)
    copy_dir = td / "moved"
    copy_dir.mkdir()
    moved = copy_dir / "modeguard.py"
    shutil.copy(str(SCRIPT), str(moved))
    t.run("install from a copy", _mg(r, "--install-hook", script=moved), 0)
    moved.unlink()
    _stage_mode(r, "tools/a.sh", False)
    rc, text = _commit_rc(r, "-m", "flip")
    t.ok("a missing script does not block the commit", rc == 0, text)
    t.ok("a missing script leaves a note", "mode check skipped" in text, text)


def _case_hook_fails_open_when_unjudgeable(t: _T, td: Path) -> None:
    r = _fresh(td)
    t.run("install", _mg(r, "--install-hook"), 0)
    _write(r / ALLOW_NAME, "not a row\n")
    _stage_mode(r, "tools/a.sh", False)
    rc, text = _commit_rc(r, "-m", "flip")
    t.ok("an unreadable allow list does not block the commit", rc == 0, text)
    t.ok("and the note says the check could not be judged", "could not be judged" in text, text)


def _case_hook_in_master_repo(t: _T, td: Path) -> None:
    r = _fresh(td, branch="master", origin=False)
    t.run("install", _mg(r, "--install-hook"), 0)
    _stage_mode(r, "tools/a.sh", False)
    rc, text = _commit_rc(r, "-m", "flip")
    t.ok("the hook judges a master-only repository", rc != 0 and "MODE-CHANGE" in text, text)


# --- isolation ------------------------------------------------------------

def _case_isolated_from_global_config(t: _T, td: Path) -> None:
    home = Path(os.environ["HOME"])
    trap_dir = home / "trap-hooks"
    trap_dir.mkdir(exist_ok=True)
    marker = home / "trap-ran"
    hook = trap_dir / "pre-commit"
    _write(hook, f"#!/bin/sh\necho ran > '{marker.as_posix()}'\n")
    if POSIX:
        hook.chmod(0o755)
    gc = home / ".gitconfig"
    _write(gc, f"[core]\n\thooksPath = {trap_dir.as_posix()}\n")
    try:
        r = _fresh(td)
        _write(r / "x.txt", "x\n")
        _git(r, "add", "x.txt")
        t.run("a commit", _commit_rc(r, "-m", "x"), 0)
        t.ok("a global core.hooksPath is not read in the self-test", not marker.exists())
        t.ok("git sees no global configuration",
             _git_rc(r, "config", "--global", "--list")[1].strip() == "")
    finally:
        for p in (gc, marker, hook):
            try:
                p.unlink()
            except OSError:
                pass


_CASES: List[Tuple[str, Callable[[_T, Path], None]]] = [
    ("clean-tree-is-green", _case_clean_tree),
    ("worktree-arm-fails-by-name", _case_worktree_arm),
    ("index-arm-fails-by-name", _case_index_arm),
    ("branch-arm-fails-by-name", _case_branch_arm),
    ("creations-deletions-and-edits-are-not-mode-changes", _case_not_mode_changes),
    ("renames-are-one-finding-on-the-new-path", _case_renames),
    ("file-to-symlink-is-a-finding", _case_type_change),
    ("allow-list-clears-only-an-exact-row", _case_allow_list),
    ("malformed-allow-row-exits-2", _case_allow_malformed),
    ("allow-flag-overrides-the-default-file", _case_allow_override),
    ("unused-allow-row-is-reported", _case_allow_unused),
    ("base-fallbacks-and-base-flag", _case_base_fallbacks),
    ("master-only-repository-is-judged", _case_master_only),
    ("origin-head-names-the-base", _case_origin_head),
    ("no-commit-yet", _case_no_commit_yet),
    ("file-mode-off-skips-only-the-worktree-arm", _case_filemode_off),
    ("index-only-arm", _case_index_only),
    ("planted-change", _case_plant),
    ("not-a-repository-exits-2", _case_not_a_repository),
    ("reports-only-never-repairs", _case_reports_only),
    ("install-hook-writes-a-sh-shim", _case_install_writes_hook),
    ("installed-hook-refuses-a-mode-change", _case_hook_refuses_mode_change),
    ("installed-hook-passes-allowed-new-and-unrelated", _case_hook_allows_allowed),
    ("install-hook-never-overwrites-a-foreign-hook", _case_install_refuses_foreign),
    ("install-hook-follows-core-hooks-path", _case_hooks_path),
    ("uninstall-hook", _case_uninstall),
    ("installed-hook-skips-with-a-note-when-script-gone", _case_hook_skips_when_script_gone),
    ("installed-hook-fails-open-when-unjudgeable", _case_hook_fails_open_when_unjudgeable),
    ("installed-hook-judges-a-master-only-repository", _case_hook_in_master_repo),
    ("self-test-ignores-global-git-config", _case_isolated_from_global_config),
]


def _run_self_test() -> int:
    """Run every case in its own directory inside one isolated temp tree; PASS/FAIL per case."""
    if shutil.which("git") is None:
        print(f"{PROG}: self-test needs git on PATH; none found")
        return 2
    n_pass = n_fail = n_skip = 0
    saved = dict(os.environ)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        home = root / "home"
        home.mkdir()
        empty = root / "empty.gitconfig"
        _write(empty, "")
        for k in _DROP_ENV:
            os.environ.pop(k, None)
        os.environ.update({
            "HOME": str(home), "USERPROFILE": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
            "GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_SYSTEM": str(empty),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "modeguard test", "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "modeguard test", "GIT_COMMITTER_EMAIL": "test@example.com",
        })
        try:
            for i, (name, fn) in enumerate(_CASES):
                case_dir = root / f"case{i}"
                case_dir.mkdir()
                t = _T()
                try:
                    fn(t, case_dir)
                except _Skip as exc:
                    print(f"SKIP {name} ({exc})")
                    n_skip += 1
                    continue
                except Exception as exc:  # a crashing case is a failing case
                    print(f"FAIL {name} ({type(exc).__name__}: {exc})")
                    n_fail += 1
                    continue
                if t.problems:
                    print(f"FAIL {name}")
                    for p in t.problems:
                        print(f"    {p}")
                    n_fail += 1
                else:
                    print(f"PASS {name}")
                    n_pass += 1
        finally:
            os.environ.clear()
            os.environ.update(saved)
    ran = n_pass + n_fail
    if n_fail:
        print(f"{PROG}: self-test FAIL -- {n_fail} of {ran} cases failed")
        return 1
    print(f"{PROG}: self-test PASS -- {n_pass} of {ran} cases passed"
          + (f", {n_skip} skipped" if n_skip else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
