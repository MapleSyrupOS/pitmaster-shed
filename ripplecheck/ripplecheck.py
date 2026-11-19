#!/usr/bin/env python3
"""ripplecheck: say which checks a branch's changes feed, run the cheap ones, and name the slow ones as OWED.

Usage: ripplecheck.py [--root DIR] [--map PATH] [--rows FILE] [--base REF | --files FILE] [--named ROW[,ROW...]]
                        [--dry-run] [--check-map] [--replay DIR] [--self-test]

What it does
    Lists the files a branch changed (``git diff --name-status -M <base>...HEAD``, which diffs from the merge base;
    a rename counts both its old and new path, a deletion counts its path), reads the project's map file, and prints
    every check ("row") the diff feeds, each with the file and the map line that made it fire. Then it runs the cheap
    commands and prints the rest as OWED, with the command that would run them.

The map file (default tools/ripplecheck.list)
    One rule a line; a line starting with ``#`` is a comment. Two line formats are read, and may be mixed.

    Keyword lines (fields split on spaces; `` -- `` starts a command):

        row        <ROW> cheap|slow|sweep-only [-- <command>]  declares a row and how its full run is started
        feed       <glob> <ROW> [-- <targeted command>]         a changed file matching <glob> feeds <ROW>
        always     <ROW> [-- <command>]                         fires on every diff (the project's fixed fast gate)
        strict     <prefix> [<prefix> ...]                      an unmapped changed file under these is red; a folder,
                                                                or text ending in * for every path starting with it
        sweep-only <ROW> [<ROW> ...]                            rows run on a schedule, never per diff

        A feed with a targeted command runs it; otherwise a ``cheap`` row runs its row command and a ``slow`` row is
        OWED with its row command.

    Bar lines, the format of MapleSyrupOS ADR-293 (``glob | row | action | command``):

        <glob> | <ROW> | run|owed|always | <command or ->

        ``run`` runs the command, ``owed`` prints it OWED, ``always`` fires on every diff and runs its command (OWED
        when the command is ``-``). ``run`` with ``-`` is a broken rule.

    In either format a command may hold ``{files}``: the changed paths that fired it, quoted for the shell (every
    changed path, for an always rule). Commands run through the shell from --root.

    Globs match the whole path, with ``/`` as the separator: ``*`` and ``?`` stay inside one folder, ``**`` crosses
    any number of folders, and a glob ending in ``/`` matches everything below that folder (git's ``:(glob)`` rules).

Rules about the map
    - every matching rule fires, not just the first; each (row, file, line) is printed
    - when the map has any ``row`` lines, every rule must name a declared row, and a rule may not name a sweep-only
      row
    - coverage: every row the project lists (the map's ``row`` lines, plus --rows) is fed by a rule or sweep-only

What it promises
    - Exit 0: nothing red and nothing owed (or every owed row was named with --named).
    - Exit 1: a command it ran failed, or a rule is broken (see above), or a changed file under a strict prefix
      matched no rule, or (with --replay) a fixture did not name the row it must.
    - Exit 2: nothing red, but OWED rows remain that the caller has not named. The caller names them in its landing
      record, then passes --named.
    - Exit 3: it could not run at all (no map, no git, a bad base). Nothing was run.
    - It changes no file. It runs only the commands the map names, from --root.

--rows FILE      the project's own list of rows, one name a line (``#`` comments), for the coverage check, when
                 the rows live elsewhere (a manifest) rather than as ``row`` lines in the map.
--base REF       the branch to diff against. Default: origin/main, else main.
--files FILE     read the changes from FILE ("-" for stdin) instead of git: ``git diff --name-status`` lines
                 (``M<TAB>path``, ``R100<TAB>old<TAB>new``) or bare paths, which count as modified.
--named ROWS     comma-separated OWED rows the caller has named; they no longer make the exit 2.
--dry-run        print what fires and what would run; run nothing. Exit 2 if anything would run or is owed.
--check-map      check the map alone (rules, coverage, and replays when --replay is given); no diff.
--replay DIR     run every ``*.replay`` file in DIR: a historic diff that must name the row it once missed.
                 A replay file holds ``expect <ROW>`` lines and change lines in the --files format.
--self-test      build its own maps and diffs in temporary directories, print one ok or FAIL line per case
                 (the deliberate failures included) and exit non-zero on any failure.

Python 3.8 or newer, standard library only. macOS, Linux, Windows.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, TextIO, Tuple

PROG = "ripplecheck"
DEFAULT_MAP = "tools/ripplecheck.list"
CLASSES = ("cheap", "slow", "sweep-only")
ACTIONS = ("run", "owed", "always")


# --- the map ---------------------------------------------------------------------------------------------------

@dataclass
class Row:
    name: str
    cls: str
    command: Optional[str]
    line: int


@dataclass
class Rule:
    kind: str                     # feed or always
    glob: str                     # "" for a keyword always line
    row: str
    command: Optional[str]
    line: int
    action: Optional[str] = None  # run, owed or always on a bar line; None on a keyword line
    regex: Optional["re.Pattern[str]"] = None


@dataclass
class Map:
    path: str
    rows: Dict[str, Row] = field(default_factory=dict)
    rules: List[Rule] = field(default_factory=list)
    strict: List[str] = field(default_factory=list)
    sweep: Set[str] = field(default_factory=set)
    errors: List[str] = field(default_factory=list)


def glob_regex(glob: str) -> "re.Pattern[str]":
    """Turn a map glob into a regex over the whole path."""
    if glob.endswith("/"):
        glob += "**"
    out, i = [], 0
    while i < len(glob):
        c = glob[i]
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("".join(out) + r"\Z")


def split_command(line: str) -> Tuple[str, Optional[str]]:
    if " -- " in line:
        head, cmd = line.split(" -- ", 1)
        return head, cmd.strip() or None
    return line, None


def parse_map(text: str, path: str, listed: Sequence[str] = ()) -> Map:
    m = Map(path)
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        where = f"{path}:{n}"
        if " | " in line or line.count("|") == 3:
            parts = [p.strip() for p in line.split("|")]
            if len(parts) != 4 or parts[2] not in ACTIONS or not parts[0] or not parts[1]:
                m.errors.append(f"{where}: cannot read this line (want glob | row | run|owed|always | command): "
                                f"{line}")
                continue
            glob, row, action, raw = parts
            command = None if raw in ("", "-") else raw
            if action == "run" and command is None:
                m.errors.append(f"{where}: run names no command")
            kind = "always" if action == "always" else "feed"
            m.rules.append(Rule(kind, glob, row, command, n, action, glob_regex(glob)))
            continue
        head, cmd = split_command(line)
        f = head.split()
        if f[0] == "row" and len(f) == 3 and f[2] in CLASSES:
            if f[1] in m.rows:
                m.errors.append(f"{where}: row {f[1]} declared twice (first at line {m.rows[f[1]].line})")
            elif f[2] != "sweep-only" and not cmd:
                m.errors.append(f"{where}: row {f[1]} is {f[2]} but has no command")
            m.rows[f[1]] = Row(f[1], f[2], cmd, n)
            if f[2] == "sweep-only":
                m.sweep.add(f[1])
        elif f[0] == "feed" and len(f) == 3:
            m.rules.append(Rule("feed", f[1], f[2], cmd, n, None, glob_regex(f[1])))
        elif f[0] == "always" and len(f) == 2:
            m.rules.append(Rule("always", "", f[1], cmd, n))
        elif f[0] == "strict" and len(f) >= 2:
            m.strict += [p[:-1] if p.endswith("*") else p if p.endswith("/") else p + "/" for p in f[1:]]
        elif f[0] == "sweep-only" and len(f) >= 2 and not cmd:
            m.sweep.update(f[1:])
        else:
            m.errors.append(f"{where}: cannot read this line: {line}")
    declared = bool(m.rows)
    for r in m.rules:
        rowdef = m.rows.get(r.row)
        if r.row in m.sweep:
            m.errors.append(f"{path}:{r.line}: {r.kind} names row {r.row}, which is declared sweep-only")
        elif rowdef is None and (declared or r.action is None):
            m.errors.append(f"{path}:{r.line}: {r.kind} names row {r.row}, which no row line declares")
    fed = {r.row for r in m.rules}
    wanted: Dict[str, str] = {name: f"{path}:{row.line}" for name, row in m.rows.items()}
    for name in listed:
        wanted.setdefault(name, "--rows")
    for name, where in wanted.items():
        if name not in m.sweep and name not in fed:
            m.errors.append(f"{where}: coverage: row {name} is neither fed by a rule nor sweep-only")
    return m


# --- the changes ------------------------------------------------------------------------------------------------

def parse_changes(text: str) -> List[str]:
    """Paths from name-status lines or bare paths. Renames give both paths; copies give the new one."""
    paths: List[str] = []
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) == 1:
            paths.append(parts[0].strip())
            continue
        status = parts[0].strip()
        if status[:1] == "R":
            paths += parts[1:3]
        elif status[:1] == "C":
            paths.append(parts[-1])
        else:
            paths.append(parts[1])
    seen, out = set(), []
    for p in paths:
        p = p.replace("\\", "/")
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def git(root: str, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def git_changes(root: str, base: Optional[str]) -> Tuple[List[str], str]:
    if base is None:
        for cand in ("origin/main", "main"):
            if subprocess.run(["git", "rev-parse", "--verify", "-q", cand], cwd=root,
                              capture_output=True).returncode == 0:
                base = cand
                break
        else:
            raise RuntimeError("no --base given and neither origin/main nor main exists")
    mb = git(root, "merge-base", base, "HEAD").strip()
    out = git(root, "diff", "--name-status", "-M", f"{mb}...HEAD")
    return parse_changes(out), f"{base} (merge base {mb[:10]})"


# --- the evaluation ---------------------------------------------------------------------------------------------

@dataclass
class Result:
    fired: List[Tuple[str, str, Rule]] = field(default_factory=list)   # (row, file or "(always)", rule)
    unmapped: List[str] = field(default_factory=list)
    strict_red: List[str] = field(default_factory=list)
    to_run: List[Tuple[str, str]] = field(default_factory=list)        # (row, command), in order, deduplicated
    owed: List[Tuple[str, Optional[str]]] = field(default_factory=list)

    def rows(self) -> List[str]:
        out: List[str] = []
        for row, _, _ in self.fired:
            if row not in out:
                out.append(row)
        return out


def decide(m: Map, row_name: str, r: Rule) -> Tuple[str, Optional[str]]:
    """What a fired rule does: ("run", cmd), ("owed", cmd) or ("none", None)."""
    if r.action is not None:                      # a bar line says it outright
        if r.action == "owed" or r.command is None:
            return "owed", r.command
        return "run", r.command
    row = m.rows.get(row_name)
    if row is None or row.cls == "sweep-only":    # a broken rule, already reported
        return "none", None
    if r.command:
        return "run", r.command
    if row.cls == "cheap":
        return "run", row.command
    return "owed", row.command


def evaluate(m: Map, paths: Sequence[str]) -> Result:
    res = Result()
    for r in m.rules:
        if r.kind == "always":
            res.fired.append((r.row, "(always)", r))
    for p in paths:
        hit = False
        for r in m.rules:
            if r.kind == "feed" and r.regex is not None and r.regex.match(p):
                res.fired.append((r.row, p, r))
                hit = True
        if not hit:
            res.unmapped.append(p)
            if any(p.startswith(s) for s in m.strict):
                res.strict_red.append(p)
    # Group by (what, row, command template), keeping first-seen order, so {files} gets every path that fired it.
    groups: Dict[Tuple[str, str, Optional[str]], List[str]] = {}
    for row_name, f, r in res.fired:
        what, cmd = decide(m, row_name, r)
        if what == "none":
            continue
        files = groups.setdefault((what, row_name, cmd), [])
        for p in (paths if f == "(always)" else [f]):
            if p not in files:
                files.append(p)
    seen_cmd, seen_owed = set(), set()
    for (what, row_name, cmd), files in groups.items():
        if cmd and "{files}" in cmd:
            cmd = cmd.replace("{files}", " ".join(shlex.quote(p) for p in files))
        if what == "run" and cmd is not None:
            if cmd not in seen_cmd:
                seen_cmd.add(cmd)
                res.to_run.append((row_name, cmd))
        elif (row_name, cmd) not in seen_owed:
            seen_owed.add((row_name, cmd))
            res.owed.append((row_name, cmd))
    return res


def run_cmd(root: str, cmd: str) -> Tuple[int, float, str]:
    t = time.monotonic()
    r = subprocess.run(cmd, shell=True, cwd=root, capture_output=True, text=True)
    tail = (r.stdout + r.stderr).strip().splitlines()[-5:]
    return r.returncode, time.monotonic() - t, "\n".join(tail)


def report(m: Map, res: Result, root: str, dry: bool, named: Sequence[str], out: TextIO = sys.stdout) -> int:
    red = False
    for e in m.errors:
        print(f"RED  map  {e}", file=out)
        red = True
    for row, f, r in res.fired:
        rule = ("always" if r.kind == "always" and not r.glob else
                f"{r.glob} | {r.action}" if r.action else f"feed {r.glob}")
        print(f"FED  {row}  {f}  <- {rule} ({m.path}:{r.line})", file=out)
    for p in res.strict_red:
        print(f"RED  unmapped  {p}  (under a strict prefix; add a rule for it)", file=out)
        red = True
    loose = len(res.unmapped) - len(res.strict_red)
    if loose:
        print(f"note {loose} changed file{'s' if loose != 1 else ''} unmapped (outside the strict prefixes)", file=out)
    for row, cmd in res.to_run:
        if dry:
            print(f"WOULD RUN  {row}  {cmd}", file=out)
            continue
        rc, secs, tail = run_cmd(root, cmd)
        if rc == 0:
            print(f"ok   {row}  {cmd}  ({secs:.1f} s)", file=out)
        else:
            red = True
            print(f"RED  {row}  {cmd}  (exit {rc}, {secs:.1f} s)", file=out)
            if tail:
                print("     " + tail.replace("\n", "\n     "), file=out)
    owed_left: List[str] = []
    for row, owed_cmd in res.owed:
        mark = "OWED (named)" if row in named else "OWED"
        print(f"{mark}  {row}  {owed_cmd or '(no command in the map)'}", file=out)
        if row not in named and row not in owed_left:
            owed_left.append(row)
    if red:
        print("verdict: 1 red", file=out)
        return 1
    if dry and res.to_run:
        print(f"verdict: 2 dry run; {len(res.to_run)} command(s) not run"
              + (f"; OWED: {', '.join(owed_left)}" if owed_left else ""), file=out)
        return 2
    if owed_left:
        print(f"verdict: 2 OWED: {', '.join(owed_left)} (name them in the landing record, then pass --named)",
              file=out)
        return 2
    print("verdict: 0 green", file=out)
    return 0


def replay(m: Map, folder: str, out: TextIO = sys.stdout) -> int:
    files = sorted(pathlib.Path(folder).glob("*.replay"))
    if not files:
        print(f"RED  replay  no *.replay files in {folder}", file=out)
        return 1
    bad = 0
    for f in files:
        text = f.read_text(encoding="utf-8")
        expect = [ln.split()[1] for ln in text.splitlines() if ln.startswith("expect ") and len(ln.split()) > 1]
        body = "\n".join(ln for ln in text.splitlines() if not ln.startswith("expect "))
        rows = evaluate(m, parse_changes(body)).rows()
        missing = [e for e in expect if e not in rows]
        if not expect:
            print(f"RED  replay  {f.name}: no expect line", file=out)
            bad += 1
        elif missing:
            print(f"RED  replay  {f.name}: does not name {', '.join(missing)} (named: {', '.join(rows) or 'none'})",
                  file=out)
            bad += 1
        else:
            print(f"ok   replay  {f.name}: names {', '.join(expect)}", file=out)
    return 1 if bad else 0


def read_rows(path: str) -> List[str]:
    names = []
    for ln in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            names.append(ln.split()[0])
    return names


# --- the self-test ----------------------------------------------------------------------------------------------

SAMPLE = """\
# a sample map
row FAST   cheap  -- {py} -c "print('fast')"
row UNIT   cheap  -- {py} -c "print('unit')"
row CORPUS slow   -- echo whole-corpus-run
row NIGHT  sweep-only
always FAST
feed src/**/*.py        UNIT
feed src/mm/**          CORPUS -- {py} -c "print('mm only')"
feed docs/**/*.py       CORPUS
feed tools/budget.txt   CORPUS
strict src/ tools/
"""

# The same project in ADR-293's bar format, with the rows listed apart (as a manifest would).
BARS = """\
*                 | FAST   | always | {py} -c "print('fast')"
src/**/*.py       | UNIT   | run    | {py} -c "import sys; print(sys.argv[1:])" {{files}}
src/mm/**         | CORPUS | run    | {py} -c "print('mm only')"
docs/**/*.py      | CORPUS | owed   | echo whole-corpus-run
tools/budget.txt  | CORPUS | owed   | echo whole-corpus-run
sweep-only NIGHT
strict src/ tools/
"""


def self_test() -> int:
    import contextlib
    import io

    py = f'"{sys.executable}"'
    fails = 0

    def case(name: str, cond: bool) -> None:
        nonlocal fails
        print(("ok   " if cond else "FAIL ") + name)
        if not cond:
            fails += 1

    def quiet(fn: Callable[..., int], *a: Any, **k: Any) -> Tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fn(*a, **k, out=buf)
        return rc, buf.getvalue()

    m = parse_map(SAMPLE.format(py=py), "ripplecheck.list")
    case("sample map reads clean", m.errors == [])

    # globs
    case("** crosses folders", bool(glob_regex("src/**/*.py").match("src/a/b/c.py")))
    case("**/ matches no folder too", bool(glob_regex("src/**/*.py").match("src/c.py")))
    case("* stays in one folder", not glob_regex("src/*.py").match("src/a/c.py"))
    case("trailing / is a prefix", bool(glob_regex("kernel/").match("kernel/x/y.c")))
    case("glob matches the whole path", not glob_regex("src/*.py").match("xsrc/a.py"))

    # changes
    ch = parse_changes("M\tsrc/a.py\nR087\told/x.c\tsrc/mm/x.c\nD\tgone.txt\nC100\tsrc/a.py\tsrc/b.py\nbare.txt\n")
    case("renames give both paths", "old/x.c" in ch and "src/mm/x.c" in ch)
    case("deletions count", "gone.txt" in ch)
    case("copies give the new path once", ch.count("src/a.py") == 1 and "src/b.py" in ch)
    case("bare paths count", "bare.txt" in ch)

    # every match fires
    res = evaluate(m, ["src/mm/alloc.py"])
    case("every matching rule fires (UNIT and CORPUS)", res.rows() == ["FAST", "UNIT", "CORPUS"])
    case("a targeted command runs instead of OWED", res.owed == [] and any("mm only" in c for _, c in res.to_run))

    # owed
    res = evaluate(m, ["docs/gen/build.py"])
    case("a slow row without a targeted command is OWED", [r for r, _ in res.owed] == ["CORPUS"])

    # the bar format
    b = parse_map(BARS.format(py=py), "bars.list", listed=["FAST", "UNIT", "CORPUS", "NIGHT"])
    case("bar format reads clean, rows listed apart", b.errors == [])
    res = evaluate(b, ["src/mm/a.py", "src/b.py", "README.md"])
    case("bar: always fires on every diff", "FAST" in res.rows())
    case("bar: {files} gets every path that fired, quoted",
         any("src/mm/a.py src/b.py" in c and "README" not in c for _, c in res.to_run))
    case("bar: an always rule maps no file (README.md stays unmapped)", res.unmapped == ["README.md"])
    res = evaluate(b, ["docs/x/y.py"])
    case("bar: owed prints its command", res.owed == [("CORPUS", "echo whole-corpus-run")])

    with tempfile.TemporaryDirectory() as d:
        rc, _ = quiet(report, m, evaluate(m, ["docs/gen/build.py"]), d, False, [])
        case("OWED exits 2", rc == 2)
        rc, _ = quiet(report, m, evaluate(m, ["docs/gen/build.py"]), d, False, ["CORPUS"])
        case("a named OWED row exits 0", rc == 0)

        res = evaluate(m, ["README.md"])
        rc, txt = quiet(report, m, res, d, False, [])
        case("always fires on an unrelated diff", res.rows() == ["FAST"] and rc == 0)
        case("an unmapped file outside strict is one note line", "1 changed file unmapped" in txt)

        rc, txt = quiet(report, b, evaluate(b, ["src/b.py"]), d, False, [])
        case("bar: a {files} command runs green", rc == 0 and "ok   UNIT" in txt)

        # red arms
        res = evaluate(m, ["src/new.c"])
        rc, txt = quiet(report, m, res, d, False, [])
        case("red arm: unmapped file under strict exits 1", rc == 1 and "RED  unmapped  src/new.c" in txt)
        star = parse_map(SAMPLE.format(py=py) + "strict pkg/gen*\n", "ripplecheck.list")
        res = evaluate(star, ["pkg/gen_cfg.py"])
        case("red arm: strict text* catches a file starting with it", res.strict_red == ["pkg/gen_cfg.py"])
        res = evaluate(star, ["pkg/other.py"])
        case("strict text* leaves a file that does not start with it alone", res.strict_red == [])
        plain = parse_map(SAMPLE.format(py=py) + "strict lib/gen\n", "ripplecheck.list")
        res = evaluate(plain, ["lib/gen_cfg.py", "lib/gen/x.py"])
        case("strict without * still means the folder", res.strict_red == ["lib/gen/x.py"])

        bad = parse_map(SAMPLE.format(py=py) + "row GHOST cheap -- true\n", "ripplecheck.list")
        case("red arm: coverage names an unfed row", any("GHOST" in e and "coverage" in e for e in bad.errors))
        bad = parse_map(BARS.format(py=py), "bars.list", listed=["FAST", "UNIT", "CORPUS", "NIGHT", "GHOST"])
        case("red arm: coverage names an unfed row from --rows", any("GHOST" in e for e in bad.errors))
        bad = parse_map(SAMPLE.format(py=py) + "feed x/** NIGHT\nfeed y/** NOPE\n", "ripplecheck.list")
        case("red arm: a feed naming a sweep-only row", any("NIGHT" in e and "sweep-only" in e for e in bad.errors))
        case("red arm: a feed naming an undeclared row", any("NOPE" in e for e in bad.errors))
        bad = parse_map("x/** | X | run | -\ny/** | Y | maybe | true\n", "bars.list")
        case("red arm: bar run with no command", any("run names no command" in e for e in bad.errors))
        case("red arm: bar line with an unknown action", any("cannot read" in e for e in bad.errors))
        rc, _ = quiet(report, bad, evaluate(bad, []), d, True, [])
        case("red arm: a broken map exits 1", rc == 1)

        failing = parse_map(SAMPLE.format(py=py).replace("print('unit')", "import sys; sys.exit(3)"), "m")
        rc, txt = quiet(report, failing, evaluate(failing, ["src/a.py"]), d, False, [])
        case("red arm: a failing targeted run exits 1", rc == 1 and "exit 3" in txt)

        rc, txt = quiet(report, m, evaluate(m, ["src/a.py"]), d, True, [])
        case("dry run runs nothing and exits 2", rc == 2 and "WOULD RUN" in txt)

        # replays: a docs .py swept into a corpus row
        rp = pathlib.Path(d, "replays")
        rp.mkdir()
        (rp / "docs-py.replay").write_text("# a docs .py fed the corpus\nexpect CORPUS\nA\tdocs/guide/x.py\n")
        rc, _ = quiet(replay, m, str(rp))
        case("a replay that names its row passes", rc == 0)
        rc, _ = quiet(replay, b, str(rp))
        case("the same replay passes on the bar map", rc == 0)
        gap = parse_map(SAMPLE.format(py=py).replace("feed docs/**/*.py       CORPUS\n", ""), "m")
        rc, txt = quiet(replay, gap, str(rp))
        case("red arm: a map missing the feed fails its replay", rc == 1 and "does not name CORPUS" in txt)

        # git end to end, with a rename and a deletion
        def g(*a: str) -> None:
            subprocess.run(["git", *a], cwd=d, check=True, capture_output=True)
        try:
            g("init", "-q", "-b", "main")
            g("config", "user.email", "test@example.com")
            g("config", "user.name", "t")
            g("config", "commit.gpgsign", "false")
            for p in ("src/mm/old.py", "docs/a.txt"):
                pathlib.Path(d, p).parent.mkdir(parents=True, exist_ok=True)
                pathlib.Path(d, p).write_text("x = 1\n" * 20)
            g("add", ".")
            g("commit", "-qm", "base")
            g("checkout", "-qb", "topic")
            pathlib.Path(d, "lib").mkdir()
            g("mv", "src/mm/old.py", "lib/new.py")
            g("rm", "-q", "docs/a.txt")
            g("commit", "-qm", "topic")
            paths, _ = git_changes(d, "main")
            case("git: rename counts both paths", "src/mm/old.py" in paths and "lib/new.py" in paths)
            case("git: a deletion counts", "docs/a.txt" in paths)
        except (OSError, subprocess.CalledProcessError) as e:
            case(f"git end to end ({e})", False)

    print(f"{PROG} self-test: {'PASS' if fails == 0 else f'FAIL ({fails})'}")
    return 1 if fails else 0


# --- main -------------------------------------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog=PROG, description="Which checks does this diff feed?")
    ap.add_argument("--root", default=".")
    ap.add_argument("--map", default=DEFAULT_MAP)
    ap.add_argument("--rows")
    ap.add_argument("--base")
    ap.add_argument("--files")
    ap.add_argument("--named", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check-map", action="store_true")
    ap.add_argument("--replay")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    root = os.path.abspath(a.root)
    mpath = os.path.join(root, a.map)
    if not os.path.isfile(mpath):
        print(f"{PROG}: no map at {a.map} (pass --map)", file=sys.stderr)
        return 3
    try:
        listed = read_rows(os.path.join(root, a.rows)) if a.rows else []
    except OSError as e:
        print(f"{PROG}: {e}", file=sys.stderr)
        return 3
    m = parse_map(pathlib.Path(mpath).read_text(encoding="utf-8"), a.map, listed)
    if a.check_map:
        for err in m.errors:
            print(f"RED  map  {err}")
        rc = 1 if m.errors else 0
        if a.replay:
            rc = max(rc, replay(m, os.path.join(root, a.replay)))
        print(f"verdict: {rc} {'red' if rc else 'map ok'}")
        return rc
    try:
        if a.files:
            text = sys.stdin.read() if a.files == "-" else pathlib.Path(a.files).read_text(encoding="utf-8")
            paths, against = parse_changes(text), a.files
        else:
            paths, against = git_changes(root, a.base)
    except (OSError, RuntimeError) as e:
        print(f"{PROG}: {e}", file=sys.stderr)
        return 3
    print(f"{PROG}: {len(paths)} changed file{'s' if len(paths) != 1 else ''} against {against}; map {a.map}")
    named = [x.strip() for x in a.named.split(",") if x.strip()]
    rc = report(m, evaluate(m, paths), root, a.dry_run, named)
    if a.replay and rc != 1 and replay(m, os.path.join(root, a.replay)):
        print("verdict: 1 red (replay)")
        rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
