#!/usr/bin/env python3
"""ratchet: report files that have grown past their line-count limit.

Usage: ratchet.py [--list FILE] [--root DIR] [--base REF] [--allow-raise PATH=REASON] [--self-test]

What it does
    Reads a list of files, each with a maximum line count (its ratchet), and
    reports every file that is over. A ratchet only moves down: when a file
    shrinks, lower its number; when a file grows past it, the check fails.

The list
    One row per file: ``<path> <max_lines>``. Blank lines and lines starting
    with ``#`` are ignored. Paths resolve against --root (default: the current
    directory). The default list is ``<root>/.ratchet.list``.

What it promises
    - The line count is the number of ``\\n`` bytes in the file (as ``wc -l``).
    - Exit 0: every listed file is at or below its ratchet.
    - Exit 1: at least one listed file is over its ratchet, or (with --base)
      a ratchet was raised since REF and the raise was not allowed.
    - Exit 2: the list is missing, empty or malformed, or a listed file is
      missing. Nothing is checked in that case.
    - It reports only. It never edits, creates or deletes a file.

--base REF
    Compares each ratchet with the same row in the list as committed at the git
    ref REF, and fails when a ratchet was raised. --allow-raise PATH=REASON
    permits one named raise and prints the reason.

--self-test
    Runs the built-in cases in a temporary directory, prints one PASS or FAIL
    line per case and a verdict line, and exits non-zero on any failure.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, List, NoReturn, Tuple

PROG = "ratchet"
DEFAULT_LIST = ".ratchet.list"


def count_lines(path: Path) -> int:
    """Return the number of ``\\n`` bytes in *path* (wc -l semantics)."""
    return path.read_bytes().count(b"\n")


def _fail_exit(msg: str) -> NoReturn:
    print(f"{PROG}: {msg}")
    sys.exit(2)


def _parse_rows(list_path: Path, text: str) -> List[Tuple[str, int]]:
    """Parse ratchet list text, returning rows of (path, max_lines)."""
    rows: List[Tuple[str, int]] = []
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if stripped == "" or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) != 2:
            _fail_exit(f"{list_path}:{lineno}: malformed row")
        p, n = parts
        try:
            m = int(n)
        except ValueError:
            _fail_exit(f"{list_path}:{lineno}: malformed row")
        if m < 0:
            _fail_exit(f"{list_path}:{lineno}: malformed row")
        rows.append((p, m))
    return rows


def _parse_list(list_path: Path) -> List[Tuple[str, int]]:
    """Parse the ratchet list, returning rows of (path, max_lines)."""
    try:
        text = list_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        _fail_exit(f"{list_path}: malformed list (not UTF-8 text)")
    return _parse_rows(list_path, text)


def base_compare(
    root: Path,
    list_path: Path,
    rows: List[Tuple[str, int]],
    ref: str,
    allow: dict,
) -> int:
    """Compare each shared path against the ratchet list stored at ref.

    Returns the number of raised-and-not-allowed rows.
    """
    try:
        rel = list_path.resolve().relative_to(root.resolve())
    except ValueError:
        _fail_exit(f"--list is outside --root; --base cannot find it at {ref}")
    rel_str = rel.as_posix()
    proc = subprocess.run(
        ["git", "-C", str(root), "show", f"{ref}:{rel_str}"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print(f"{PROG}: base {ref} has no ratchet list; nothing to compare")
        return 0
    base_map = dict(_parse_rows(list_path, proc.stdout))
    n_fail = 0
    for path_str, new in rows:
        if path_str not in base_map:
            continue
        old = base_map[path_str]
        if new > old:
            if path_str in allow:
                print(
                    f"{PROG}: {path_str} -- raised from {old} to {new} since {ref}, "
                    f"allowed: {allow[path_str]}"
                )
            else:
                n_fail += 1
                print(f"{PROG}: {path_str} -- FAIL: the ratchet was raised from {old} to {new} since {ref}")
        elif new == old:
            print(f"{PROG}: {path_str} -- base OK: the ratchet is unchanged at {new} since {ref}")
        else:
            print(f"{PROG}: {path_str} -- base OK: the ratchet was lowered from {old} to {new} since {ref}")
    return n_fail


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Check that files do not grow past their ratchet limits.",
    )
    parser.add_argument(
        "--list",
        default=None,
        metavar="FILE",
        help=f"Path to the ratchet list (default: <root>/{DEFAULT_LIST}).",
    )
    parser.add_argument(
        "--root",
        default=".",
        metavar="DIR",
        help="Directory that listed paths resolve against (default: the current directory).",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run the built-in cases and exit.",
    )
    parser.add_argument(
        "--base",
        default=None,
        metavar="REF",
        help="Git ref of a committed ratchet list to compare against.",
    )
    parser.add_argument(
        "--allow-raise",
        action="append",
        default=None,
        metavar="PATH=REASON",
        help="Allow a single ratchet raise, given as PATH=REASON (may repeat).",
    )
    args = parser.parse_args(argv)

    allow: dict = {}
    if args.allow_raise is not None:
        for value in args.allow_raise:
            if "=" not in value:
                _fail_exit(f"malformed --allow-raise {value}")
            a, reason = value.split("=", 1)
            allow[a] = reason

    if args.self_test:
        return _run_self_test()

    root = Path(args.root)
    list_path = Path(args.list) if args.list else root / DEFAULT_LIST
    if not list_path.is_file():
        print(f"{PROG}: {list_path} -- REFUSED: no such file")
        return 2

    rows = _parse_list(list_path)
    if not rows:
        print(f"{PROG}: no rows")
        return 2

    results: List[Tuple[str, int, int]] = []
    for path_str, max_lines in rows:
        resolved = root / path_str
        if not resolved.is_file():
            print(f"{PROG}: {path_str} -- REFUSED: no such file")
            return 2
        results.append((path_str, count_lines(resolved), max_lines))

    for path_str, count, max_lines in results:
        if count > max_lines:
            d = count - max_lines
            print(f"{PROG}: {path_str} -- FAIL: {count} lines, over its ratchet of {max_lines} by {d}")
        elif count == max_lines:
            print(f"{PROG}: {path_str} -- OK: {count} lines, at its ratchet")
        else:
            d = max_lines - count
            print(
                f"{PROG}: {path_str} -- OK: {count} lines, "
                f"{d} below its ratchet of {max_lines}; lower the ratchet to {count} in this change"
            )

    n_fails = base_compare(root, list_path, rows, args.base, allow) if args.base else 0
    total = len(results)
    n_fails += sum(1 for _, c, m in results if c > m)
    if n_fails:
        print(f"{PROG}: FAIL -- {n_fails} problem(s) across {total} file(s)")
        return 1
    print(f"{PROG}: OK -- {total} file(s) at or below their ratchet")
    return 0


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _run(list_path: Path, root: Path, *extra: str) -> Tuple[str, int]:
    """Run this script with the given args; return (stdout, exit code)."""
    script = str(Path(__file__).resolve())
    result = subprocess.run(
        [sys.executable, script, "--list", str(list_path), "--root", str(root), *extra],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip(), result.returncode


def _case_at_ratchet(td: Path) -> bool:
    (td / "a.txt").write_bytes(b"line1\nline2\nline3\n")
    (td / "l.list").write_text("a.txt 3\n")
    out, rc = _run(td / "l.list", td)
    return rc == 0 and "at its ratchet" in out


def _case_over_ratchet(td: Path) -> bool:
    # RED ARM: a file over its ratchet must exit 1 and name the file.
    (td / "big.txt").write_bytes(b"line1\nline2\nline3\n")
    (td / "l.list").write_text("big.txt 2\n")
    out, rc = _run(td / "l.list", td)
    return rc == 1 and "big.txt -- FAIL: 3 lines, over its ratchet of 2 by 1" in out


def _case_one_of_two_over(td: Path) -> bool:
    # RED ARM: one over file among passing files still fails the run, by name.
    (td / "ok.txt").write_bytes(b"1\n")
    (td / "bad.txt").write_bytes(b"1\n2\n3\n")
    (td / "l.list").write_text("ok.txt 5\nbad.txt 1\n")
    out, rc = _run(td / "l.list", td)
    return rc == 1 and "bad.txt -- FAIL" in out and "ok.txt -- FAIL" not in out


def _case_below_ratchet(td: Path) -> bool:
    (td / "c.txt").write_bytes(b"line1\nline2\nline3\n")
    (td / "l.list").write_text("c.txt 5\n")
    out, rc = _run(td / "l.list", td)
    return rc == 0 and "lower the ratchet to 3" in out


def _case_counts_newline_bytes(td: Path) -> bool:
    # A last line with no newline is not counted; CR is not a line break.
    (td / "n.txt").write_bytes(b"a\nb")
    (td / "r.txt").write_bytes(b"a\rb\rc\r")
    (td / "l.list").write_text("n.txt 1\nr.txt 0\n")
    out, rc = _run(td / "l.list", td)
    return rc == 0 and "n.txt -- OK: 1 lines, at its ratchet" in out and "r.txt -- OK: 0 lines, at its ratchet" in out


def _case_comments_and_blanks(td: Path) -> bool:
    (td / "a.txt").write_bytes(b"x\n")
    (td / "l.list").write_text("# a comment\n\n   \na.txt 1\n")
    out, rc = _run(td / "l.list", td)
    return rc == 0 and "1 file(s)" in out


def _case_missing_file(td: Path) -> bool:
    # RED ARM: a listed file that does not exist must exit 2 and name it.
    (td / "l.list").write_text("missing.txt 5\n")
    out, rc = _run(td / "l.list", td)
    return rc == 2 and "missing.txt -- REFUSED" in out


def _case_malformed_row(td: Path) -> bool:
    # RED ARM: a row with the wrong number of fields must exit 2 and give its line.
    (td / "l.list").write_text("# header\na.txt b c\n")
    out, rc = _run(td / "l.list", td)
    return rc == 2 and "l.list:2: malformed row" in out


def _case_non_numeric_and_negative(td: Path) -> bool:
    # RED ARM: a non-numeric or negative limit must exit 2.
    (td / "l1.list").write_text("a.txt many\n")
    (td / "l2.list").write_text("a.txt -1\n")
    out1, rc1 = _run(td / "l1.list", td)
    out2, rc2 = _run(td / "l2.list", td)
    return rc1 == 2 and rc2 == 2 and "malformed row" in out1 and "malformed row" in out2


def _case_empty_list(td: Path) -> bool:
    (td / "l.list").write_text("# only a comment\n")
    out, rc = _run(td / "l.list", td)
    return rc == 2 and "no rows" in out


def _case_missing_list(td: Path) -> bool:
    out, rc = _run(td / "nope.list", td)
    return rc == 2 and "REFUSED" in out


def _case_default_list(td: Path) -> bool:
    # With no --list, the list is <root>/.ratchet.list.
    (td / "a.txt").write_bytes(b"1\n2\n")
    (td / DEFAULT_LIST).write_text("a.txt 1\n")
    script = str(Path(__file__).resolve())
    r = subprocess.run([sys.executable, script, "--root", str(td)], capture_output=True, text=True)
    return r.returncode == 1 and "a.txt -- FAIL" in r.stdout


def _case_never_repairs(td: Path) -> bool:
    # Report-only: neither the file nor the list changes, even when over.
    (td / "a.txt").write_bytes(b"1\n2\n3\n")
    (td / "l.list").write_text("a.txt 1\n")
    _run(td / "l.list", td)
    return (td / "a.txt").read_bytes() == b"1\n2\n3\n" and (td / "l.list").read_text() == "a.txt 1\n"


def _git_repo(td: Path) -> Path:
    """Make a one-commit git repo holding a.txt (10 lines) and a list at 10."""
    repo = td / "repo"
    repo.mkdir()
    (repo / "a.txt").write_bytes(b"1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n")
    (repo / DEFAULT_LIST).write_text("a.txt 10\n")
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
         "commit", "-q", "-m", "i"],
        cwd=str(repo),
        check=True,
    )
    return repo


def _case_base_unchanged(td: Path) -> bool:
    repo = _git_repo(td)
    out, rc = _run(repo / DEFAULT_LIST, repo, "--base", "HEAD")
    return rc == 0 and "unchanged at 10 since HEAD" in out


def _case_base_raised(td: Path) -> bool:
    # RED ARM: raising a ratchet since the base ref must exit 1 and name the file.
    repo = _git_repo(td)
    (repo / DEFAULT_LIST).write_text("a.txt 11\n")
    out, rc = _run(repo / DEFAULT_LIST, repo, "--base", "HEAD")
    return rc == 1 and "a.txt -- FAIL: the ratchet was raised from 10 to 11 since HEAD" in out


def _case_base_allowed(td: Path) -> bool:
    repo = _git_repo(td)
    (repo / DEFAULT_LIST).write_text("a.txt 11\n")
    out, rc = _run(repo / DEFAULT_LIST, repo, "--base", "HEAD", "--allow-raise", "a.txt=why")
    return rc == 0 and "allowed: why" in out


def _case_base_lowered(td: Path) -> bool:
    repo = _git_repo(td)
    (repo / DEFAULT_LIST).write_text("a.txt 9\n")
    (repo / "a.txt").write_bytes(b"1\n2\n3\n4\n5\n6\n7\n8\n9\n")
    out, rc = _run(repo / DEFAULT_LIST, repo, "--base", "HEAD")
    return rc == 0 and "lowered from 10 to 9 since HEAD" in out


_CASES: List[Tuple[str, Callable[[Path], bool], bool]] = [
    # (name, function, needs git)
    ("at-ratchet", _case_at_ratchet, False),
    ("over-ratchet-fails-by-name", _case_over_ratchet, False),
    ("one-of-two-over-fails-by-name", _case_one_of_two_over, False),
    ("below-ratchet-says-lower-it", _case_below_ratchet, False),
    ("counts-newline-bytes-only", _case_counts_newline_bytes, False),
    ("comments-and-blank-lines-ignored", _case_comments_and_blanks, False),
    ("missing-listed-file-exits-2", _case_missing_file, False),
    ("malformed-row-exits-2", _case_malformed_row, False),
    ("bad-limit-exits-2", _case_non_numeric_and_negative, False),
    ("empty-list-exits-2", _case_empty_list, False),
    ("missing-list-exits-2", _case_missing_list, False),
    ("default-list-is-dot-ratchet-list", _case_default_list, False),
    ("never-repairs", _case_never_repairs, False),
    ("base-unchanged", _case_base_unchanged, True),
    ("base-raised-fails-by-name", _case_base_raised, True),
    ("base-raise-allowed", _case_base_allowed, True),
    ("base-lowered", _case_base_lowered, True),
]


def _run_self_test() -> int:
    """Run every case in its own temporary directory; print PASS/FAIL per case."""
    git_ok = shutil.which("git") is not None
    n_pass = n_fail = n_skip = 0
    for name, fn, needs_git in _CASES:
        if needs_git and not git_ok:
            print(f"SKIP {name} (git not found)")
            n_skip += 1
            continue
        with tempfile.TemporaryDirectory() as td:
            try:
                ok = fn(Path(td))
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
    if n_fail:
        print(f"{PROG}: self-test FAIL -- {n_fail} of {ran} cases failed")
        return 1
    print(f"{PROG}: self-test PASS -- {n_pass} of {ran} cases passed" + (f", {n_skip} skipped" if n_skip else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
