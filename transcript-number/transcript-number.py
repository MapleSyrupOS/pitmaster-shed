#!/usr/bin/env python3
"""Number a talk's transcript T1, T2..., or write its quote-only record. Python 3.8+, standard library only.

Usage: transcript-number.py FILE [-o OUT]
       transcript-number.py FILE --quote-only [--numbered] [--source TEXT] [--by NAME] [-o OUT]
       transcript-number.py --check FILE
       transcript-number.py --self-test

FILE is a plain-text transcript ("-" reads standard input): one turn per line, or
"Speaker: text" lines, as a meeting tool exports it or as pasted. Every line that is not
blank becomes one numbered line (a line ends only at a line break: \n, \r\n or \r), "T<n>: <the line>", with its text kept exactly (only the
spaces at its end are dropped); blank lines are dropped and never numbered. The numbered
lines are written a blank line apart, so they read as separate lines in Markdown too. A
file that is already numbered is refused: use --check on it.

--quote-only writes a quote-only record instead (a record no model wrote; pitmaster
ADR-032 tier T0): every line that carries a decision word (decide, agree, vote, motion,
action, will, todo, follow up, propose, and their forms) becomes one row of a decisions
table, quoting the line, citing its T-number, with status open. Nothing in it is agreed.
With --numbered, FILE is a transcript already numbered by this tool. Its header gives the
date, the source (FILE's name, or --source TEXT, such as the original file or a link) and,
with --by NAME, who ran it. A straight double quote inside a line is written as a curly one
there, so it cannot end the row's quote. The record passes record-check, with or without
--transcript.

--check FILE checks a numbered transcript: its lines run T1, T2... with none skipped and
none doubled, and no line between them is left unnumbered.

-o OUT writes to OUT instead of the screen; it never overwrites FILE.

Exit 0: green; 1: findings (--check, or a self-test case failed); 2: invalid usage or
unreadable input.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import io
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Callable, List, Optional, Tuple

PROG = "transcript-number"
TLINE = re.compile(r"^\s*T(\d+):\s?(.*)$")
DECISION = re.compile(
    r"(?<![\w'])(decid\w*|decision\w*|agree\w*|vote[sd]?|voting|motion\w*|action\w*|will|todo|to-do|"
    r"follow[- ]?ups?|propos\w*)(?![\w'])", re.IGNORECASE)


def read_text(name: str) -> str:
    if name == "-":
        data = sys.stdin.buffer.read()
    else:
        path = pathlib.Path(name)
        if not path.is_file():
            raise ValueError("{}: no such file".format(name))
        data = path.read_bytes()
    return data.decode("utf-8-sig")


def split_lines(text: str) -> List[str]:
    """Lines broken only at \n, \r\n and \r, never at the other breaks str.splitlines() knows."""
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def number(text: str) -> List[Tuple[int, str]]:
    lines = [line.rstrip() for line in split_lines(text)]
    kept = [line for line in lines if line.strip()]
    if kept and all(TLINE.match(line) for line in kept):
        raise ValueError("the transcript is already numbered; check it with --check")
    return [(n, line) for n, line in enumerate(kept, 1)]


def numbered_lines(text: str) -> List[Tuple[int, str]]:
    out = []
    for line in split_lines(text):
        m = TLINE.match(line)
        if m:
            out.append((int(m.group(1)), m.group(2).rstrip()))
    if not out:
        raise ValueError("no numbered lines (\"T1: ...\")")
    return out


def render_numbered(rows: List[Tuple[int, str]]) -> str:
    return "\n\n".join("T{}: {}".format(n, line) for n, line in rows) + "\n"


def cell(text: str) -> str:
    return text.replace("|", "\\|").replace('"', "\u201d")  # a straight quote inside would end the quote


def render_quote_only(rows: List[Tuple[int, str]], source: str, by: str = "", day: str = "") -> str:
    hits = [(n, line) for n, line in rows if DECISION.search(line)]
    first, last = rows[0][0], rows[-1][0]
    out = [
        "# Quote-only record: {}".format(source),
        "",
        "**Quote-only record.** Code copied every line of the transcript that carries a decision word, with its",
        "T-number. No model judged it: nothing here is agreed, and every status is open.",
        "",
        "Date: {}. Source: {}, T{}-T{}.{}".format(day or datetime.date.today().isoformat(), source, first, last,
                                                " Run by: {}.".format(by) if by else ""),
        "",
        "## Decisions",
        "",
        "| decision | status | where |",
        "|---|---|---|",
    ]
    if hits:
        out += ["| \"{}\" | open | T{} |".format(cell(line), n) for n, line in hits]
    else:
        out.append("| No line carries a decision word | open | T{}-T{} |".format(first, last))
    return "\n".join(out) + "\n"


def check(rows: List[Tuple[int, str]], text: str) -> List[str]:
    out: List[str] = []
    seen = set()
    expect = 1
    for n, _ in rows:
        if n in seen:
            out.append("T{} is doubled".format(n))
            continue
        seen.add(n)
        if n != expect:
            if n > expect:
                out.append("T{} is skipped".format(expect) if n == expect + 1
                           else "T{}-T{} are skipped".format(expect, n - 1))
            else:
                out.append("T{} is out of order (after T{})".format(n, expect - 1))
        expect = max(expect, n + 1)
    started = False
    for lineno, line in enumerate(split_lines(text), 1):
        if TLINE.match(line):
            started = True
        elif started and line.strip():
            out.append("line {}: not numbered (\"{}\")".format(lineno, line.strip()[:40]))
    return out


def write(out_name: Optional[str], text: str, source: str) -> None:
    if out_name:
        path = pathlib.Path(out_name)
        if source != "-" and path.resolve() == pathlib.Path(source).resolve():
            raise ValueError("-o names the input file; write somewhere else")
        path.write_bytes(text.encode("utf-8"))
    else:
        sys.stdout.write(text)


def run(source: str, out_name: Optional[str] = None, quote_only: bool = False, numbered: bool = False,
        check_only: bool = False, label: str = "", by: str = "") -> int:
    try:
        text = read_text(source)
        if check_only:
            problems = check(numbered_lines(text), text)
            for problem in problems:
                print("FAIL {}: {}".format(source, problem))
            print("{}: {}, {} finding(s)".format(PROG, source, len(problems)))
            return 1 if problems else 0
        rows = numbered_lines(text) if numbered else number(text)
        if not rows:
            raise ValueError("{}: the transcript is empty".format(source))
        name = label or ("standard input" if source == "-" else pathlib.Path(source).name)
        write(out_name, render_quote_only(rows, name, by) if quote_only else render_numbered(rows), source)
    except (OSError, UnicodeError, ValueError) as exc:
        print("{}: {}".format(PROG, exc), file=sys.stderr)
        return 2
    return 0


# ---------------------------------------------------------------- self-test

PLAIN = (
    "Sam: Welcome, everyone.\n"
    "\n"
    "Alex: Should we move the call to Thursdays?\n"
    "Sam: I agree, Thursdays work better.   \n"
    "Alex: I will write the release notes | with a pipe.\n"
    "Sam: See you then.\n"
)


def _write(path: pathlib.Path, text: str) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def _quiet(*args, **kwargs) -> Tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = run(*args, **kwargs)
    return code, out.getvalue()


def _case_numbers(base: pathlib.Path) -> bool:
    code, out = _quiet(str(_write(base / "t.txt", PLAIN)))
    want = ("T1: Sam: Welcome, everyone.\n\nT2: Alex: Should we move the call to Thursdays?\n\n"
            "T3: Sam: I agree, Thursdays work better.\n\nT4: Alex: I will write the release notes | with a pipe.\n\n"
            "T5: Sam: See you then.\n")
    return code == 0 and out == want


def _case_crlf_bom(base: pathlib.Path) -> bool:
    path = base / "t.txt"
    path.write_bytes(b"\xef\xbb\xbf" + PLAIN.replace("\n", "\r\n").encode("utf-8"))
    code, out = _quiet(str(path))
    return code == 0 and out.startswith("T1: Sam: Welcome") and "\r" not in out and "T5:" in out


def _case_output_file(base: pathlib.Path) -> bool:
    src = _write(base / "t.txt", PLAIN)
    code, out = _quiet(str(src), str(base / "out.md"))
    return code == 0 and out == "" and (base / "out.md").read_text(encoding="utf-8").startswith("T1:") \
        and src.read_text(encoding="utf-8") == PLAIN


def _case_check_green(base: pathlib.Path) -> bool:
    src = _write(base / "t.txt", PLAIN)
    _quiet(str(src), str(base / "n.md"))
    return _quiet(str(base / "n.md"), check_only=True)[0] == 0


def _case_quote_only(base: pathlib.Path) -> bool:
    code, out = _quiet(str(_write(base / "t.txt", PLAIN)), quote_only=True)
    return (code == 0 and "| \"Sam: I agree, Thursdays work better.\" | open | T3 |" in out
            and "| \"Alex: I will write the release notes \\| with a pipe.\" | open | T4 |" in out
            and "T1 |" not in out and "agreed |" not in out and "Quote-only record" in out)


def _case_quote_only_numbered(base: pathlib.Path) -> bool:
    src = _write(base / "t.txt", PLAIN)
    _quiet(str(src), str(base / "n.md"))
    code, out = _quiet(str(base / "n.md"), quote_only=True, numbered=True)
    return code == 0 and "| open | T3 |" in out


def _case_quote_only_header(base: pathlib.Path) -> bool:
    code, out = _quiet(str(_write(base / "t.txt", PLAIN)), quote_only=True, label="call.txt", by="the owner")
    return code == 0 and "Source: call.txt, T1-T5. Run by: the owner." in out and "Date: 20" in out


def _case_other_breaks_kept(base: pathlib.Path) -> bool:
    code, out = _quiet(str(_write(base / "t.txt", "Sam: one\u2028two\x0cthree\nAlex: four\n")))
    return code == 0 and out == "T1: Sam: one\u2028two\x0cthree\n\nT2: Alex: four\n"


def _case_quote_only_none(base: pathlib.Path) -> bool:
    code, out = _quiet(str(_write(base / "t.txt", "Sam: hello\nAlex: hi\n")), quote_only=True)
    return code == 0 and "| No line carries a decision word | open | T1-T2 |" in out


def _case_red_skipped(base: pathlib.Path) -> bool:
    path = _write(base / "n.md", "T1: a\n\nT2: b\n\nT4: d\n")
    code, out = _quiet(str(path), check_only=True)
    return code == 1 and "T3 is skipped" in out


def _case_red_doubled(base: pathlib.Path) -> bool:
    path = _write(base / "n.md", "T1: a\n\nT2: b\n\nT2: c\n\nT3: d\n")
    code, out = _quiet(str(path), check_only=True)
    return code == 1 and "T2 is doubled" in out


def _case_red_unnumbered(base: pathlib.Path) -> bool:
    path = _write(base / "n.md", "T1: a\n\nstray line\n\nT2: b\n")
    code, out = _quiet(str(path), check_only=True)
    return code == 1 and "line 3: not numbered" in out


def _case_usage(base: pathlib.Path) -> bool:
    numbered = _write(base / "n.md", "T1: a\nT2: b\n")
    src = _write(base / "t.txt", PLAIN)
    return (_quiet(str(base / "absent.txt"))[0] == 2
            and _quiet(str(numbered))[0] == 2
            and _quiet(str(_write(base / "e.txt", "\n\n")))[0] == 2
            and _quiet(str(src), str(src))[0] == 2
            and src.read_text(encoding="utf-8") == PLAIN)


def _case_copy_one_folder(base: pathlib.Path) -> bool:
    solo = base / "solo"
    solo.mkdir()
    script = solo / "transcript-number.py"
    shutil.copy(str(pathlib.Path(__file__).resolve()), str(script))
    src = _write(base / "t.txt", PLAIN)

    def call(*args: str) -> int:
        return subprocess.run([sys.executable, str(script)] + list(args), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE).returncode
    return call(str(src)) == 0 and call() == 2


_CASES: List[Tuple[str, Callable[[pathlib.Path], bool]]] = [
    ("numbers each non-blank line, text kept, trailing spaces dropped", _case_numbers),
    ("a byte-order mark and CRLF line endings", _case_crlf_bom),
    ("-o writes the file and leaves the input alone", _case_output_file),
    ("--check passes its own output", _case_check_green),
    ("--quote-only: decision-word lines, open, cited, pipes escaped", _case_quote_only),
    ("--quote-only --numbered reads a numbered transcript", _case_quote_only_numbered),
    ("--quote-only with no decision word still writes one cited row", _case_quote_only_none),
    ("--quote-only header: date, --source and --by", _case_quote_only_header),
    ("a line ends only at a line break, not at U+2028 or a form feed", _case_other_breaks_kept),
    ("red: a skipped T-number", _case_red_skipped),
    ("red: a doubled T-number", _case_red_doubled),
    ("red: an unnumbered line between numbered ones", _case_red_unnumbered),
    ("usage: missing, already numbered, empty, -o onto the input", _case_usage),
    ("copy-one-folder", _case_copy_one_folder),
]


def self_test() -> int:
    passed = failed = 0
    for name, case in _CASES:
        with tempfile.TemporaryDirectory(prefix="transcript-number-") as tmp:
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
    parser.add_argument("file", nargs="?", help="the transcript (\"-\" for standard input)")
    parser.add_argument("-o", metavar="OUT", help="write here instead of the screen")
    parser.add_argument("--quote-only", action="store_true", help="write the quote-only record")
    parser.add_argument("--numbered", action="store_true", help="FILE is already numbered (with --quote-only)")
    parser.add_argument("--source", metavar="TEXT", default="", help="the source named in the quote-only header")
    parser.add_argument("--by", metavar="NAME", default="", help="who ran it, for the quote-only header")
    parser.add_argument("--check", action="store_true", help="check a numbered transcript")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if args.self_test:
        return self_test()
    if not args.file:
        parser.print_usage(sys.stderr)
        print("{}: name a transcript file".format(PROG), file=sys.stderr)
        return 2
    if args.numbered and not args.quote_only:
        print("{}: --numbered goes with --quote-only".format(PROG), file=sys.stderr)
        return 2
    if args.check and (args.quote_only or args.o):
        print("{}: --check takes only a file".format(PROG), file=sys.stderr)
        return 2
    if (args.source or args.by) and not args.quote_only:
        print("{}: --source and --by go with --quote-only".format(PROG), file=sys.stderr)
        return 2
    return run(args.file, args.o, args.quote_only, args.numbered, args.check, args.source, args.by)


if __name__ == "__main__":
    sys.exit(main())
