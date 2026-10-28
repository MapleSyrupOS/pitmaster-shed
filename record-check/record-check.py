#!/usr/bin/env python3
"""Check the decisions tables in a meeting record. Python 3.8+, standard library only.

Usage: record-check.py FILE [FILE ...]
       record-check.py --self-test

A record is a Markdown file written by hand: a talk-through record, a townhall record,
or any record kept in the same shape. The script checks only its decisions tables; the
prose around them is the writer's.

A decisions table is a Markdown table whose header row starts with a "decision" cell
and has a "status" cell. Every record holds at least one. Each table must:
  - have a "where" column (where the decision came up: a question, a turn, a quote;
    any header that starts with "where", such as "where it came up");
  - hold at least one row;
  - give every row the header's number of cells;
  - mark every row's status agreed, proposed or open (bold, italics or backticks
    around the word are fine; any other word is a finding);
  - cite every row: its where cell is not empty and not a placeholder (-, ?, n/a,
    tbd, todo, none).
A "Decisions" heading with no decisions table under it is a finding. Tables inside
fenced code blocks are examples and are skipped. A pipe inside a cell is written \\|.
The script does not check that a citation is true, only that one is there.

Exit 0: green; 1: findings; 2: invalid usage or unreadable input.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Callable, List, Tuple

PROG = "record-check"
STATUSES = ("agreed", "proposed", "open")
PLACEHOLDERS = {"", "-", "--", "–", "—", "?", "n/a", "na", "tbd", "todo", "none"}
HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
SEPARATOR = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
EMPHASIS = re.compile(r"^[*_`]+|[*_`]+$")


def cells(line: str) -> List[str]:
    """Split one table row on unescaped pipes, dropping the outer ones."""
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", body)]


def bare(cell: str) -> str:
    """A cell's text without emphasis markers around it, lower-cased."""
    return EMPHASIS.sub("", cell.strip()).strip().lower()


def findings(path: pathlib.Path) -> Tuple[List[str], int, int]:
    """Return (findings, tables, rows) for one record."""
    lines = path.read_bytes().decode("utf-8").splitlines()
    out: List[str] = []
    tables = rows = 0
    fence = ""
    open_heading = None  # (line number, level) of a Decisions heading still waiting for its table
    i = 0

    def close_heading() -> None:
        nonlocal open_heading
        if open_heading is not None:
            out.append("{}:{}: \"Decisions\" heading has no decisions table under it".format(
                path.name, open_heading[0]))
            open_heading = None

    while i < len(lines):
        line = lines[i]
        mark = FENCE.match(line)
        if fence:
            if mark and mark.group(1)[0] == fence[0] and len(mark.group(1)) >= len(fence):
                fence = ""
            i += 1
            continue
        if mark:
            fence = mark.group(1)
            i += 1
            continue
        heading = HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            if open_heading is not None and level <= open_heading[1]:
                close_heading()
            if bare(heading.group(2)) == "decisions":
                close_heading()
                open_heading = (i + 1, level)
            i += 1
            continue
        if not (line.lstrip().startswith("|") and i + 1 < len(lines) and SEPARATOR.match(lines[i + 1])):
            i += 1
            continue
        header = [bare(c) for c in cells(line)]
        start = i + 1
        i += 2
        body: List[Tuple[int, str]] = []
        while i < len(lines) and lines[i].lstrip().startswith("|"):
            body.append((i + 1, lines[i]))
            i += 1
        if not header or header[0] != "decision" or "status" not in header:
            continue
        tables += 1
        open_heading = None
        status_col = header.index("status")
        where_col = next((n for n, h in enumerate(header) if h.startswith("where")), None)
        if where_col is None:
            out.append("{}:{}: decisions table has no \"where\" column".format(path.name, start))
        if not body:
            out.append("{}:{}: decisions table has no rows".format(path.name, start))
        for lineno, raw in body:
            row = cells(raw)
            rows += 1
            if len(row) != len(header):
                out.append("{}:{}: row has {} cells, the header has {}".format(
                    path.name, lineno, len(row), len(header)))
                continue
            status = bare(row[status_col])
            if status not in STATUSES:
                out.append("{}:{}: status \"{}\" is not one of {}".format(
                    path.name, lineno, row[status_col], ", ".join(STATUSES)))
            if where_col is not None and bare(row[where_col]) in PLACEHOLDERS:
                out.append("{}:{}: decision is not cited (empty where)".format(path.name, lineno))
    close_heading()
    if tables == 0:
        out.append("{}: no decisions table (a table whose header starts | decision | status |)".format(path.name))
    return out, tables, rows


def run(paths: List[pathlib.Path]) -> int:
    problems: List[str] = []
    tables = rows = 0
    try:
        for path in paths:
            if not path.is_file():
                raise ValueError("{}: no such file".format(path))
            found, t, r = findings(path)
            problems += found
            tables += t
            rows += r
    except (OSError, UnicodeError, ValueError) as exc:
        print("{}: {}".format(PROG, exc), file=sys.stderr)
        return 2
    for problem in problems:
        print("FAIL " + problem)
    print("{}: {} file(s), {} decisions table(s), {} row(s), {} finding(s)".format(
        PROG, len(paths), tables, rows, len(problems)))
    return 1 if problems else 0


# ---------------------------------------------------------------- self-test

GOOD = (
    "# talk-through: a record\n\n"
    "**Status:** open: nothing here is ruled unless marked agreed.\n\n"
    "## Decisions\n\n"
    "| decision | status | where |\n"
    "|---|---|---|\n"
    "| Use SQLite | agreed | Q3: \"SQLite, for now\" |\n"
    "| Add a cache | proposed | round 2, codex |\n"
    "| Which port | open | Q7 |\n"
)


def _write(path: pathlib.Path, text: str) -> pathlib.Path:
    """Write UTF-8 text with the newlines exactly as given, on every platform."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def _quiet_run(paths: List[pathlib.Path]) -> Tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = run(paths)
    return code, out.getvalue()


def _table_case(text: str, expected: int, says: str = "") -> Callable[[pathlib.Path], bool]:
    def case(base: pathlib.Path) -> bool:
        code, out = _quiet_run([_write(base / "record.md", text)])
        return code == expected and says in out
    return case


def _case_two_files_one_bad(base: pathlib.Path) -> bool:
    good = _write(base / "good.md", GOOD)
    bad = _write(base / "bad.md", GOOD.replace("| proposed |", "| maybe |"))
    code, out = _quiet_run([good, bad])
    return code == 1 and "bad.md:10:" in out and "good.md" not in out.replace("bad.md", "")


def _case_missing_file(base: pathlib.Path) -> bool:
    return _quiet_run([base / "absent.md"])[0] == 2


def _case_not_utf8(base: pathlib.Path) -> bool:
    path = base / "latin.md"
    path.write_bytes(b"| decision | status | where |\n|---|---|---|\n| caf\xe9 | open | Q1 |\n")
    return _quiet_run([path])[0] == 2


def _case_copy_one_folder(base: pathlib.Path) -> bool:
    """The script works from a lone copy of its folder, with no repository and no other file."""
    solo = base / "solo"
    solo.mkdir()
    script = solo / "record-check.py"
    shutil.copy(str(pathlib.Path(__file__).resolve()), str(script))
    good = _write(base / "good.md", GOOD)
    bad = _write(base / "bad.md", GOOD.replace("| Q7 |", "|  |"))

    def call(*args: str) -> int:
        return subprocess.run([sys.executable, str(script)] + list(args), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE).returncode
    return call(str(good)) == 0 and call(str(bad)) == 1 and call() == 2


# (name, record text, expected exit, text the output must hold)
_TABLE = [
    ("a talk-through record", GOOD, 0, "1 decisions table(s), 3 row(s), 0 finding(s)"),
    ("an amendment's second table", GOOD + "\n## Amendment\n\n| decision | status | where |\n"
     "|---|---|---|\n| Name it stew | agreed | \"let's make the default stew bbq\" |\n", 0, "2 decisions table(s)"),
    ("extra columns, other order", "| decision | where | status | who |\n|:--|:-:|--:|---|\n"
     "| A | Q1 | open | the owner |\n", 0, ""),
    ("bold and backticked statuses", GOOD.replace("| agreed |", "| **agreed** |")
     .replace("| open |", "| `open` |"), 0, ""),
    ("a \"where it came up\" header", GOOD.replace("| where |", "| Where it came up |"), 0, ""),
    ("a capitalised header", GOOD.replace("| decision | status | where |", "| Decision | Status | Where |"), 0, ""),
    ("an escaped pipe in a cell", GOOD.replace("Use SQLite", "Use A \\| B"), 0, ""),
    ("CRLF line endings", GOOD.replace("\n", "\r\n"), 0, "0 finding(s)"),
    ("other tables are left alone", GOOD + "\n| skill | tres | really |\n|---|---|---|\n| bbq | no |  |\n", 0, ""),
    ("a fenced example is skipped", GOOD + "\n```markdown\n| decision | status | where |\n|---|---|---|\n"
     "| X | maybe |  |\n```\n", 0, "1 decisions table(s)"),
    ("Decisions heading, table under a subheading", "## Decisions\n\n### First day\n\n"
     "| decision | status | where |\n|---|---|---|\n| A | open | Q1 |\n", 0, ""),
    ("red: a status that is not one of the three", GOOD.replace("| proposed |", "| maybe |"), 1,
     "record.md:10: status \"maybe\""),
    ("red: a status with words after it", GOOD.replace("| proposed |", "| proposed, then agreed |"), 1, "status"),
    ("red: an empty where", GOOD.replace("| Q7 |", "|  |"), 1, "record.md:11: decision is not cited"),
    ("red: a placeholder where", GOOD.replace("| Q7 |", "| — |"), 1, "not cited"),
    ("red: TBD as the where", GOOD.replace("| Q7 |", "| *TBD* |"), 1, "not cited"),
    ("red: no where column", "| decision | status |\n|---|---|\n| A | open |\n", 1, "no \"where\" column"),
    ("red: a header with no rows", "## Decisions\n\n| decision | status | where |\n|---|---|---|\n", 1,
     "has no rows"),
    ("red: a row short of a cell", GOOD.replace("| Which port | open | Q7 |", "| Which port | open |"), 1,
     "row has 2 cells, the header has 3"),
    ("red: no decisions table at all", "# A record\n\nJust prose.\n", 1, "no decisions table"),
    ("red: the only table is fenced", "```\n| decision | status | where |\n|---|---|---|\n| A | open | Q1 |\n```\n",
     1, "no decisions table"),
    ("red: Decisions heading with no table", "## Decisions\n\nNone yet.\n\n## Open questions\n\n" + GOOD[GOOD.index("|"):],
     1, "record.md:1: \"Decisions\" heading has no decisions table"),
    ("red: Decisions heading with no table, at the end", GOOD + "\n## Decisions\n\nNone.\n", 1, "heading has no"),
]

_CASES: List[Tuple[str, Callable[[pathlib.Path], bool]]] = (
    [(row[0], _table_case(*row[1:])) for row in _TABLE]
    + [
        ("red: two files, the bad one named", _case_two_files_one_bad),
        ("usage: a missing file", _case_missing_file),
        ("usage: a file that is not UTF-8", _case_not_utf8),
        ("copy-one-folder", _case_copy_one_folder),
    ]
)


def self_test() -> int:
    passed = failed = 0
    for name, case in _CASES:
        with tempfile.TemporaryDirectory(prefix="record-check-") as tmp:
            try:
                ok = case(pathlib.Path(tmp))
                detail = ""
            except Exception as exc:  # a crashing case is a failing case
                ok = False
                detail = " ({}: {})".format(type(exc).__name__, exc)
        print("{} {}{}".format("PASS" if ok else "FAIL", name, detail))
        passed += ok
        failed += not ok
    ran = passed + failed
    if failed:
        print("{}: self-test FAIL -- {} of {} cases failed".format(PROG, failed, ran))
        return 1
    print("{}: self-test PASS -- {} of {} cases passed".format(PROG, passed, ran))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog=PROG, description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="*", help="record files to check")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if args.self_test:
        return self_test()
    if not args.files:
        parser.print_usage(sys.stderr)
        print("{}: name at least one record file".format(PROG), file=sys.stderr)
        return 2
    return run([pathlib.Path(f) for f in args.files])


if __name__ == "__main__":
    sys.exit(main())
