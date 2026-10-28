#!/usr/bin/env python3
"""check-twins: fail when two copies of a shared code block differ by even a byte.

Usage: check-twins.py [--root DIR] [--self-test]

What it does
    Every tool in this repository is one script that must work alone, so code two tools share is copied into each
    of them between two marker lines, which start in the first column:

        # --- shed:shared NAME begin ---
        ... the shared code ...
        # --- shed:shared NAME end ---

    check-twins finds every such block in every tool folder (``<tool>/*.py``, one level down from the root) and
    compares the copies of each NAME. The text compared is everything between the two marker lines, byte for
    byte, so a changed line end or a trailing space counts.

What it promises
    - Exit 0: every block that appears in two or more files is identical in all of them, and every marker is
      well formed.
    - Exit 1: two copies of a block differ (the message names the block, the first file, the file that differs
      and the first differing line of the block), or a marker is malformed: a begin with no end, an end with no
      begin, a block inside a block, or a name used twice in one file.
    - Exit 2: --root is not a folder.
    - A block that appears in only one file is allowed; nothing is compared.
    - It reads only. It never edits a file.

--root DIR
    The repository root. Default: the folder above this script's folder.

--self-test
    Builds its own tool folders in temporary directories, prints one PASS or FAIL line per case and a verdict
    line, and exits non-zero on any failure. One case changes one byte in one copy of one block and requires the
    check to fail naming that block and both files.

Python 3.8 or newer, standard library only. macOS, Linux, Windows.
"""
from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

PROG = "check-twins"
MARKER = re.compile(rb"^# --- shed:shared ([A-Za-z0-9_.-]+) (begin|end) ---\r?$")
SKIP_DIRS = {"scripts", "node_modules", "__pycache__"}


def tool_files(root: Path) -> List[Path]:
    """Every ``*.py`` directly inside a top-level folder that holds a ``<folder>/<folder>.py`` tool."""
    files: List[Path] = []
    for d in sorted(root.iterdir()):
        if d.is_dir() and not d.name.startswith(".") and d.name not in SKIP_DIRS and (d / f"{d.name}.py").is_file():
            files.extend(sorted(d.glob("*.py")))
    return files


def blocks_in(path: Path, rel: str, problems: List[str]) -> Dict[str, bytes]:
    """The shared blocks of one file: name to the bytes between its marker lines. Malformed markers are added to
    `problems`."""
    found: Dict[str, bytes] = {}
    lines = path.read_bytes().split(b"\n")
    open_name: Optional[str] = None
    open_at = 0
    body: List[bytes] = []
    for no, line in enumerate(lines, start=1):
        m = MARKER.match(line)
        if m is None:
            if open_name is not None:
                body.append(line)
            continue
        name, kind = m.group(1).decode("ascii"), m.group(2).decode("ascii")
        if kind == "begin":
            if open_name is not None:
                problems.append(f"{rel}:{no}: block {name} begins inside block {open_name} (opened at line {open_at})")
                continue
            open_name, open_at, body = name, no, []
        else:
            if open_name is None:
                problems.append(f"{rel}:{no}: end of block {name} with no begin")
            elif name != open_name:
                problems.append(f"{rel}:{no}: end of block {name} closes block {open_name} (opened at line {open_at})")
                open_name = None
            else:
                if name in found:
                    problems.append(f"{rel}:{no}: block {name} appears twice in this file")
                found[name] = b"\n".join(body)
                open_name = None
    if open_name is not None:
        problems.append(f"{rel}:{open_at}: block {open_name} begins and never ends")
    return found


def first_difference(a: bytes, b: bytes) -> int:
    """The 1-based line of the block where `a` and `b` first differ."""
    la, lb = a.split(b"\n"), b.split(b"\n")
    for i in range(max(len(la), len(lb))):
        if i >= len(la) or i >= len(lb) or la[i] != lb[i]:
            return i + 1
    return 1


def check(root: Path) -> Tuple[List[str], List[str]]:
    """(problems, ok lines) for every tool file under `root`."""
    problems: List[str] = []
    copies: Dict[str, List[Tuple[str, bytes]]] = {}
    files = tool_files(root)
    for path in files:
        rel = path.relative_to(root).as_posix()
        for name, text in blocks_in(path, rel, problems).items():
            copies.setdefault(name, []).append((rel, text))
    ok: List[str] = []
    for name in sorted(copies):
        group = copies[name]
        first_rel, first_text = group[0]
        clean = True
        for rel, text in group[1:]:
            if text != first_text:
                clean = False
                problems.append(f"block {name} differs between {first_rel} and {rel} "
                                f"(first difference at line {first_difference(first_text, text)} of the block)")
        if clean:
            ok.append(f"block {name}: {len(group)} cop{'y' if len(group) == 1 else 'ies'}"
                      f"{', identical' if len(group) > 1 else ', nothing to compare'}")
    ok.append(f"{len(copies)} block(s) in {len(files)} file(s)")
    return problems, ok


def run(root: Path) -> int:
    if not root.is_dir():
        print(f"{PROG}: {root} is not a folder")
        return 2
    problems, ok = check(root)
    for line in ok[:-1]:
        print(f"{PROG}: {line}")
    for p in problems:
        print(f"{PROG}: FAIL -- {p}")
    if problems:
        print(f"{PROG}: FAIL -- {len(problems)} problem(s); {ok[-1]}")
        return 1
    print(f"{PROG}: PASS -- {ok[-1]}")
    return 0


# ----------------------------------------------------------------------------------------------- self-test

def _block(name: str, code: str) -> str:
    return f"# --- shed:shared {name} begin ---\n{code}# --- shed:shared {name} end ---\n"


def _tool(root: Path, tool: str, text: str) -> None:
    d = root / tool
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f"{tool}.py", "wb") as fh:
        fh.write(text.encode("utf-8"))


def _result(root: Path) -> Tuple[int, str]:
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run(root)
    return rc, buf.getvalue()


def _expect(cond: object, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


SHARED = "def shared():\n    return 1\n"


def _case_identical_copies_pass(td: Path) -> None:
    _tool(td, "alpha", "x = 1\n" + _block("core", SHARED) + "y = 2\n")
    _tool(td, "beta", "# other code\n" + _block("core", SHARED))
    rc, out = _result(td)
    _expect(rc == 0 and "PASS" in out and "block core: 2 copies, identical" in out, f"{rc} {out!r}")


def _case_one_byte_changed_fails_by_name(td: Path) -> None:
    # RED ARM: one byte changed in one copy of one block fails, naming the block and both files.
    _tool(td, "alpha", _block("core", SHARED))
    _tool(td, "beta", _block("core", SHARED.replace("return 1", "return 2")))
    rc, out = _result(td)
    _expect(rc == 1, f"exit {rc}: {out!r}")
    _expect("block core differs between alpha/alpha.py and beta/beta.py" in out, f"names: {out!r}")
    _expect("first difference at line 2 of the block" in out, f"line: {out!r}")


def _case_third_copy_named(td: Path) -> None:
    _tool(td, "alpha", _block("core", SHARED))
    _tool(td, "beta", _block("core", SHARED))
    _tool(td, "gamma", _block("core", SHARED + "q = 1\n"))
    rc, out = _result(td)
    _expect(rc == 1 and "alpha/alpha.py and gamma/gamma.py" in out and "beta/beta.py and" not in out,
            f"{rc} {out!r}")


def _case_line_end_change_is_a_difference(td: Path) -> None:
    # RED ARM: a block compared byte for byte; a CRLF line end is not an LF line end.
    _tool(td, "alpha", _block("core", SHARED))
    _tool(td, "beta", _block("core", SHARED).replace("\n    return", "\r\n    return"))
    rc, out = _result(td)
    _expect(rc == 1 and "differs between" in out, f"{rc} {out!r}")


def _case_only_the_differing_block_is_named(td: Path) -> None:
    _tool(td, "alpha", _block("one", SHARED) + _block("two", SHARED))
    _tool(td, "beta", _block("one", SHARED) + _block("two", "z = 1\n"))
    rc, out = _result(td)
    _expect(rc == 1 and "block two differs" in out and "block one differs" not in out, f"{rc} {out!r}")
    _expect("block one: 2 copies, identical" in out, f"{out!r}")


def _case_single_copy_is_allowed(td: Path) -> None:
    _tool(td, "alpha", _block("alone", SHARED))
    _tool(td, "beta", "x = 1\n")
    rc, out = _result(td)
    _expect(rc == 0 and "nothing to compare" in out, f"{rc} {out!r}")


def _case_begin_without_end_fails(td: Path) -> None:
    # RED ARM: a block that never ends fails, naming the file and the line.
    _tool(td, "alpha", "x = 1\n# --- shed:shared core begin ---\n" + SHARED)
    rc, out = _result(td)
    _expect(rc == 1 and "alpha/alpha.py:2: block core begins and never ends" in out, f"{rc} {out!r}")


def _case_end_without_begin_fails(td: Path) -> None:
    _tool(td, "alpha", SHARED + "# --- shed:shared core end ---\n")
    rc, out = _result(td)
    _expect(rc == 1 and "end of block core with no begin" in out, f"{rc} {out!r}")


def _case_nested_and_repeated_blocks_fail(td: Path) -> None:
    _tool(td, "alpha", "# --- shed:shared a begin ---\n" + _block("b", SHARED) + "# --- shed:shared a end ---\n")
    rc, out = _result(td)
    _expect(rc == 1 and "block b begins inside block a" in out, f"nested: {rc} {out!r}")
    _tool(td, "alpha", _block("a", SHARED) + _block("a", SHARED))
    rc, out = _result(td)
    _expect(rc == 1 and "block a appears twice in this file" in out, f"twice: {rc} {out!r}")


def _case_markers_must_start_the_line(td: Path) -> None:
    # A marker quoted inside a string or indented is prose, not a marker.
    _tool(td, "alpha", 'x = "# --- shed:shared core begin ---"\n    # --- shed:shared core begin ---\n')
    rc, out = _result(td)
    _expect(rc == 0 and "0 block(s)" in out, f"{rc} {out!r}")


def _case_only_tool_folders_are_read(td: Path) -> None:
    _tool(td, "alpha", _block("core", SHARED))
    (td / "scripts").mkdir()
    with open(td / "scripts" / "scripts.py", "w") as fh:
        fh.write(_block("core", "z = 1\n"))
    (td / "loose").mkdir()
    with open(td / "loose" / "other.py", "w") as fh:
        fh.write(_block("core", "z = 1\n"))
    rc, out = _result(td)
    _expect(rc == 0, f"a folder that is not a tool was read: {rc} {out!r}")


def _case_missing_root_exits_2(td: Path) -> None:
    rc, out = _result(td / "nowhere")
    _expect(rc == 2 and "is not a folder" in out, f"{rc} {out!r}")


def _case_never_edits(td: Path) -> None:
    _tool(td, "alpha", _block("core", SHARED))
    _tool(td, "beta", _block("core", SHARED + "q = 1\n"))
    before = sorted((p.name, p.read_bytes()) for p in td.rglob("*.py"))
    _result(td)
    _expect(before == sorted((p.name, p.read_bytes()) for p in td.rglob("*.py")), "a file changed")


_CASES: List[Tuple[str, Callable[[Path], None]]] = [
    ("identical-copies-pass", _case_identical_copies_pass),
    ("one-byte-changed-fails-by-name", _case_one_byte_changed_fails_by_name),
    ("third-copy-that-differs-is-named", _case_third_copy_named),
    ("line-end-change-is-a-difference", _case_line_end_change_is_a_difference),
    ("only-the-differing-block-is-named", _case_only_the_differing_block_is_named),
    ("single-copy-is-allowed", _case_single_copy_is_allowed),
    ("begin-without-end-fails", _case_begin_without_end_fails),
    ("end-without-begin-fails", _case_end_without_begin_fails),
    ("nested-and-repeated-blocks-fail", _case_nested_and_repeated_blocks_fail),
    ("markers-must-start-the-line", _case_markers_must_start_the_line),
    ("only-tool-folders-are-read", _case_only_tool_folders_are_read),
    ("missing-root-exits-2", _case_missing_root_exits_2),
    ("never-edits-a-file", _case_never_edits),
]


def _run_self_test() -> int:
    n_pass = n_fail = 0
    for name, fn in _CASES:
        with tempfile.TemporaryDirectory() as td:
            try:
                fn(Path(td).resolve())
            except Exception as exc:  # a crashing case is a failing case
                print(f"FAIL {name} ({type(exc).__name__}: {exc})")
                n_fail += 1
                continue
        print(f"PASS {name}")
        n_pass += 1
    ran = n_pass + n_fail
    if n_fail:
        print(f"{PROG}: self-test FAIL -- {n_fail} of {ran} cases failed")
        return 1
    print(f"{PROG}: self-test PASS -- {n_pass} of {ran} cases passed")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="check-twins.py", description="Fail when copies of a shared block differ.")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent),
                    help="the repository root (default: the folder above this script's folder)")
    ap.add_argument("--self-test", action="store_true", help="run the built-in cases")
    args = ap.parse_args(argv)
    if args.self_test:
        return _run_self_test()
    return run(Path(args.root))


if __name__ == "__main__":
    sys.exit(main())
