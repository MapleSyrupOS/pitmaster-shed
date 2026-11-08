#!/usr/bin/env python3
"""Check the facts a project's living docs state against the project itself. Python 3.8+, standard library only.

Usage: stale-facts.py [--root DIR] [--facts FILE] [--fix]
       stale-facts.py --self-test

A facts file (docs/facts.txt by default; pitmaster.md's facts_file line, then --facts, win) says what each fact
is, how to work it out from the files, and which docs state it:

  fact <name> = dirs <folder> [<marker file>]     the folders in <folder> (only those holding <marker>, if given)
  fact <name> = files <glob>                      the files matching <glob>
  fact <name> = json <file> <key.path> [<regex>]  a JSON value; with a regex, its first group
  fact <name> = text <value>                      a fixed value
  state <name> [ruled] <doc>[#Section|!Section] <pattern>
  mention <name> <doc>[#Section|!Section]

A `state` pattern is literal text holding one slot: {number} (digits, or a number word up to ninety-nine),
{word} (letters, digits, '.', '_', '-') or {text} (a {word} that may also hold '/'). Whitespace in a pattern
matches any run of whitespace, line breaks included. A list fact (dirs, files) compares its count with {number};
a value fact compares its text. Every `state` pattern must match at least once across its docs, so a reworded
sentence is found rather than silently skipped. `mention` needs every item of a list fact to appear in the doc as
a whole word. <doc> may be a glob, in double quotes when its section name has spaces. `#Section` reads only that section (a heading whose text equals it, at any
level, down to the next heading at the same or a higher level); `!Section` reads everything but it.

A `ruled` doc is a decision record whose ruled text is never edited: a stale value there passes once the same
file holds a section whose heading contains "Amendment" and whose text states the true value. --fix rewrites the
slot's own text in every doc that is not ruled (a number keeps its form: digits stay digits, a word stays a word
with its capital), and never touches a ruled doc. Lines starting with '#' and blank lines in the facts file are
ignored. Checks the docs it is told to read; it cannot know a fact the file does not name.

Exit 0: green; 1: findings; 2: invalid usage, facts file or unreadable input.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Callable, Dict, List, Optional, Tuple

PROG = "stale-facts"
DEFAULT_FACTS = "docs/facts.txt"
HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
        "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
SLOTS = {
    "number": r"\d+|[A-Za-z]+(?:-[A-Za-z]+)?",
    "word": r"[A-Za-z0-9_.-]+",
    "text": r"[A-Za-z0-9_./-]+",
}


class FactsError(Exception):
    pass


def read(path: pathlib.Path) -> str:
    with open(path, encoding="utf-8", newline="") as fh:  # keeps CRLF as it is, so --fix leaves line ends alone
        return fh.read()


def write(path: pathlib.Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def number_word(n: int) -> str:
    if n < 20:
        return ONES[n]
    tens, ones = divmod(n, 10)
    return TENS[tens] + ("-" + ONES[ones] if ones else "")


WORD_VALUES = {number_word(n): n for n in range(100)}


def read_number(text: str) -> Optional[int]:
    if text.isdigit():
        return int(text)
    return WORD_VALUES.get(text.lower())


def write_number(n: int, like: str) -> str:
    if like.isdigit() or n >= 100:
        return str(n)
    word = number_word(n)
    return word[0].upper() + word[1:] if like[:1].isupper() else word


# ---- the facts file -------------------------------------------------------------------------------------------

class Fact:
    def __init__(self, name: str, kind: str, value: object) -> None:
        self.name, self.kind, self.value = name, kind, value  # kind: "list" or "value"


class Rule:
    def __init__(self, verb: str, fact: str, doc: str, ruled: bool, pattern: str, lineno: int) -> None:
        self.verb, self.fact, self.doc, self.ruled, self.pattern, self.lineno = verb, fact, doc, ruled, pattern, lineno


def work_out(root: pathlib.Path, name: str, source: str) -> Fact:
    parts = source.split()
    if not parts:
        raise FactsError("fact {}: no source".format(name))
    how, args = parts[0], parts[1:]
    if how == "dirs" and len(args) in (1, 2):
        base = root / args[0]
        if not base.is_dir():
            raise FactsError("fact {}: no folder {}".format(name, args[0]))
        names = sorted(p.name for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")
                       and (len(args) == 1 or (p / args[1]).is_file()))
        return Fact(name, "list", names)
    if how == "files" and len(args) == 1:
        return Fact(name, "list", sorted(p.name for p in root.glob(args[0]) if p.is_file()))
    if how == "json" and len(args) >= 2:
        path = root / args[0]
        try:
            value: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise FactsError("fact {}: cannot read {}: {}".format(name, args[0], exc))
        for key in args[1].split("."):
            if not isinstance(value, dict) or key not in value:
                raise FactsError("fact {}: {} has no key {}".format(name, args[0], args[1]))
            value = value[key]
        text = str(value)
        if len(args) > 2:
            match = re.search(" ".join(args[2:]), text)
            if not match or not match.groups():
                raise FactsError("fact {}: regex found no group in {!r}".format(name, text))
            text = match.group(1)
        return Fact(name, "value", text)
    if how == "text" and args:
        return Fact(name, "value", " ".join(args))
    raise FactsError("fact {}: cannot read source {!r}".format(name, source))


def parse(root: pathlib.Path, text: str) -> Tuple[Dict[str, Fact], List[Rule]]:
    facts: Dict[str, Fact] = {}
    rules: List[Rule] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        verb, _, rest = line.partition(" ")
        if verb == "fact":
            name, eq, source = rest.partition("=")
            name = name.strip()
            if not eq or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                raise FactsError("line {}: expected 'fact <name> = <source>'".format(lineno))
            facts[name] = work_out(root, name, source.strip())
        elif verb in ("state", "mention"):
            m = re.match(r'^(\S+)\s+(?:(ruled)\s+)?(?:"([^"]+)"|(\S+))(?:\s+(.*))?$', rest.strip())
            if not m or (verb == "state") != bool(m.group(5)):
                raise FactsError("line {}: expected '{} <fact> [ruled] <doc>{}'".format(
                    lineno, verb, " <pattern>" if verb == "state" else ""))
            fact, ruled, doc, pattern = m.group(1), bool(m.group(2)), m.group(3) or m.group(4), m.group(5) or ""
            if fact not in facts:
                raise FactsError("line {}: no fact named {} above this line".format(lineno, fact))
            if verb == "state" and len(re.findall(r"\{(number|word|text)\}", pattern)) != 1:
                raise FactsError("line {}: a pattern needs exactly one slot: {{number}}, {{word}} or {{text}}"
                                 .format(lineno))
            if verb == "mention" and facts[fact].kind != "list":
                raise FactsError("line {}: mention needs a list fact (dirs or files)".format(lineno))
            rules.append(Rule(verb, fact, doc, ruled, pattern, lineno))
        else:
            raise FactsError("line {}: unknown line {!r}".format(lineno, verb))
    return facts, rules


# ---- the docs -------------------------------------------------------------------------------------------------

def sections(lines: List[str]) -> List[Tuple[int, str, int, int]]:
    """Each heading as (level, text, first line index, end line index exclusive), ignoring fenced code."""
    heads: List[Tuple[int, str, int]] = []
    fence: Optional[str] = None
    for i, line in enumerate(lines):
        m = FENCE.match(line)
        if m:
            mark = m.group(1)[0]
            fence = None if fence == mark else (mark if fence is None else fence)
            continue
        if fence is None:
            h = HEADING.match(line)
            if h:
                heads.append((len(h.group(1)), h.group(2), i))
    out = []
    for n, (level, text, start) in enumerate(heads):
        end = len(lines)
        for level2, _, start2 in heads[n + 1:]:
            if level2 <= level:
                end = start2
                break
        out.append((level, text, start, end))
    return out


def spans(lines: List[str], spec: str) -> List[Tuple[int, int]]:
    """The line ranges a doc spec reads."""
    if spec.startswith("#") or spec.startswith("!"):
        name = spec[1:]
        hits = [(s, e) for _, text, s, e in sections(lines) if text == name]
        if spec.startswith("#"):
            return hits
        keep: List[Tuple[int, int]] = []
        cur = 0
        for s, e in sorted(hits):
            if s > cur:
                keep.append((cur, s))
            cur = max(cur, e)
        if cur < len(lines):
            keep.append((cur, len(lines)))
        return keep
    return [(0, len(lines))]


def split_doc(doc: str) -> Tuple[str, str]:
    for mark in ("#", "!"):
        if mark in doc:
            path, _, name = doc.partition(mark)
            return path, mark + name
    return doc, ""


def compile_pattern(pattern: str) -> Tuple["re.Pattern[str]", str]:
    slot = re.search(r"\{(number|word|text)\}", pattern).group(1)  # type: ignore[union-attr]
    before, after = pattern.split("{" + slot + "}", 1)

    def lit(s: str) -> str:
        return r"\s+".join(re.escape(part) for part in re.split(r"\s+", s)) if s.strip() else re.escape(s)
    lead = lit(before)
    tail = lit(after)
    # A slot never runs into a neighbouring word character.
    body = r"(?<![A-Za-z0-9_])(" + SLOTS[slot] + r")(?![A-Za-z0-9_])"
    return re.compile(lead + body + tail), slot


def matches_truth(fact: Fact, slot: str, found: str) -> Tuple[bool, str]:
    if fact.kind == "list":
        n = len(fact.value)  # type: ignore[arg-type]
        if slot != "number":
            raise FactsError("fact {} is a list: state it with {{number}}".format(fact.name))
        return read_number(found) == n, str(n)
    truth = str(fact.value)
    if slot == "number":
        return read_number(found) == read_number(truth), truth
    return found == truth, truth


def amended(lines: List[str], truth: str, slot: str) -> bool:
    for _, text, s, e in sections(lines):
        if "Amendment" in text:
            body = "\n".join(lines[s:e])
            if slot == "number" and read_number(truth) is not None:
                n = read_number(truth)
                if re.search(r"\b({}|{})\b".format(n, number_word(n) if n < 100 else n), body, re.I):  # type: ignore
                    return True
            elif truth in body:
                return True
    return False


Finding = Tuple[str, int, str, bool]  # doc, line, message, fixed by --fix


def check(root: pathlib.Path, facts: Dict[str, Fact], rules: List[Rule], fix: bool) -> List[Finding]:
    found: List[Finding] = []
    edits: Dict[pathlib.Path, List[Tuple[int, int, str]]] = {}
    for rule in rules:
        fact = facts[rule.fact]
        path_glob, spec = split_doc(rule.doc)
        paths = sorted(p for p in root.glob(path_glob) if p.is_file())
        if not paths:
            found.append((rule.doc, 0, "facts line {}: no file matches".format(rule.lineno), False))
            continue
        hits = 0
        for path in paths:
            rel = path.relative_to(root).as_posix()
            try:
                text = read(path)
            except (OSError, UnicodeDecodeError) as exc:
                raise FactsError("cannot read {}: {}".format(rel, exc))
            lines = text.split("\n")
            offsets = [0]
            for line in lines:
                offsets.append(offsets[-1] + len(line) + 1)
            for s, e in spans(lines, spec):
                chunk_start, chunk_end = offsets[s], offsets[e] - 1 if e > s else offsets[s]
                chunk = text[chunk_start:chunk_end]
                if rule.verb == "mention":
                    hits += 1
                    for item in fact.value:  # type: ignore[union-attr]
                        if not re.search(r"(?<![A-Za-z0-9_-]){}(?![A-Za-z0-9_-])".format(re.escape(item)), chunk):
                            found.append((rel, s + 1, "{}: does not name {}".format(rule.fact, item), False))
                    continue
                regex, slot = compile_pattern(rule.pattern)
                for m in regex.finditer(chunk):
                    hits += 1
                    ok, truth = matches_truth(fact, slot, m.group(1))
                    if ok:
                        continue
                    at = chunk_start + m.start(1)
                    lineno = next(i for i in range(len(offsets)) if offsets[i + 1] > at) + 1
                    if rule.ruled:
                        if amended(lines, truth, slot):
                            continue
                        found.append((rel, lineno, "{}: says {}, the project has {} (a ruled record: add a dated "
                                      "amendment stating it)".format(rule.fact, m.group(1), truth), False))
                        continue
                    found.append((rel, lineno, "{}: says {}, the project has {}".format(rule.fact, m.group(1), truth),
                                  fix))
                    if fix:
                        new = write_number(int(truth), m.group(1)) if slot == "number" and truth.isdigit() else truth
                        edits.setdefault(path, []).append((at, chunk_start + m.end(1), new))
        if hits == 0:
            found.append((rule.doc, 0, "facts line {}: the pattern matches nothing; was the sentence reworded?"
                          .format(rule.lineno), False))
    for path, changes in edits.items():
        text = read(path)
        for start, end, new in sorted(set(changes), reverse=True):
            text = text[:start] + new + text[end:]
        write(path, text)
    return found


def config_facts(root: pathlib.Path) -> Optional[str]:
    cfg = root / "pitmaster.md"
    if not cfg.is_file():
        return None
    for line in cfg.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^facts_file:\s*(\S+)", line)
        if m:
            return m.group(1)
    return None


def run(root: pathlib.Path, facts_file: Optional[str] = None, fix: bool = False) -> int:
    rel = facts_file or config_facts(root) or DEFAULT_FACTS
    path = root / rel
    try:
        facts, rules = parse(root, path.read_text(encoding="utf-8"))
        found = check(root, facts, rules, fix)
    except OSError as exc:
        print("{}: cannot read {}: {}".format(PROG, rel, exc))
        return 2
    except FactsError as exc:
        print("{}: {}: {}".format(PROG, rel, exc))
        return 2
    for doc, lineno, message, done in found:
        print("{}: {}{}: {}{}".format(PROG, doc, ":{}".format(lineno) if lineno else "", message,
                                      " (fixed)" if done else ""))
    fixed = sum(1 for f in found if f[3])
    if found:
        tail = "; {} fixed".format(fixed) if fix else ""
        print("{}: {} finding(s){}".format(PROG, len(found), tail))
        return 0 if fix and fixed == len(found) else 1
    print("{}: {} rule(s), all facts current".format(PROG, len(rules)))
    return 0


# ---- self-test ------------------------------------------------------------------------------------------------

def _tree(base: pathlib.Path, files: Dict[str, str]) -> pathlib.Path:
    for rel, text in files.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        write(p, text)
    return base


SKILLS = {"skills/a/SKILL.md": "", "skills/b/SKILL.md": "", "skills/c/SKILL.md": "", "skills/notes/x.txt": "",
          "plugin.json": '{"version": "1.2.3", "repository": "https://github.com/Org/proj"}'}


def _quiet(root: pathlib.Path, fix: bool = False) -> Tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = run(root, fix=fix)
    return code, out.getvalue()


def _case(files: Dict[str, str], want: int, needle: str = "", fix: bool = False,
          after: Optional[Tuple[str, str]] = None) -> Callable[[pathlib.Path], bool]:
    def case(base: pathlib.Path) -> bool:
        root = _tree(base, dict(SKILLS, **files))
        code, out = _quiet(root, fix)
        ok = code == want and needle in out
        if after:
            ok = ok and after[1] in read(root / after[0])
        return ok
    return case


FACTS = "fact skills = dirs skills SKILL.md\n"
CASES: List[Tuple[str, Callable[[pathlib.Path], bool]]] = [
    ("green: a word count that is right", _case({
        "docs/facts.txt": FACTS + "state skills README.md There are {number} skills\n",
        "README.md": "There are three skills.\n"}, 0, "all facts current")),
    ("red arm: a word count that is stale", _case({
        "docs/facts.txt": FACTS + "state skills README.md There are {number} skills\n",
        "README.md": "Intro.\nThere are two skills.\n"}, 1, "README.md:2: skills: says two, the project has 3")),
    ("red arm: a digit count that is stale", _case({
        "docs/facts.txt": FACTS + "state skills README.md ({number} skills)\n",
        "README.md": "Skills (4 skills)\n"}, 1, "says 4")),
    ("a match across a line break, with its line", _case({
        "docs/facts.txt": FACTS + "state skills README.md There are {number} skills\n",
        "README.md": "a\nb There are\nfour skills\n"}, 1, "README.md:3:")),
    ("--fix keeps the word and its capital", _case({
        "docs/facts.txt": FACTS + "state skills README.md {number} skills ship\n",
        "README.md": "Seventeen skills ship.\n"}, 0, "1 fixed", fix=True, after=("README.md", "Three skills ship."))),
    ("--fix keeps digits as digits", _case({
        "docs/facts.txt": FACTS + "state skills README.md ({number} skills)\n",
        "README.md": "(9 skills)\n"}, 0, "fixed", fix=True, after=("README.md", "(3 skills)"))),
    ("a pattern that matches nothing is a finding", _case({
        "docs/facts.txt": FACTS + "state skills README.md There are {number} skills\n",
        "README.md": "We ship a handful of skills.\n"}, 1, "matches nothing")),
    ("a json fact with a regex group", _case({
        "docs/facts.txt": "fact owner = json plugin.json repository github\\.com/([^/]+)/\n"
                          "state owner README.md add {word}/proj\n",
        "README.md": "marketplace add Old/proj\n"}, 1, "owner: says Old, the project has Org")),
    ("a ruled record is never fixed, only reported", _case({
        "docs/facts.txt": "fact owner = json plugin.json repository github\\.com/([^/]+)/\n"
                          "state owner ruled ADR-*.md#Access `{word}/proj`\n",
        "ADR-1.md": "# ADR-1\n## Access\nFrom `Old/proj`.\n"}, 1, "a ruled record", fix=True,
        after=("ADR-1.md", "`Old/proj`"))),
    ("a ruled record is cleared by an amendment stating the truth", _case({
        "docs/facts.txt": "fact owner = json plugin.json repository github\\.com/([^/]+)/\n"
                          "state owner ruled ADR-*.md#Access `{word}/proj`\n",
        "ADR-1.md": "# ADR-1\n## Access\nFrom `Old/proj`.\n## Amendment — 2026-11-05, x\nNow `Org/proj`.\n"},
        0, "all facts current")),
    ("#Section reads only that section", _case({
        "docs/facts.txt": FACTS + "state skills STATUS.md#Now {number} skills\n",
        "STATUS.md": "## Landed\n- two skills\n## Now\n- three skills\n"}, 0, "all facts current")),
    ("!Section skips that section", _case({
        "docs/facts.txt": FACTS + "state skills STATUS.md!Landed {number} skills\n",
        "STATUS.md": "## Landed\n- two skills\n### deeper\n- one skills\n## Now\n- four skills\n"},
        1, "STATUS.md:6: skills: says four")),
    ("a heading inside fenced code is not a section", _case({
        "docs/facts.txt": FACTS + "state skills R.md#Real {number} skills\n",
        "R.md": "## Real\n```\n## Fake\n```\nthree skills\n"}, 0, "all facts current")),
    ("mention: every item named", _case({
        "docs/facts.txt": FACTS + "mention skills AGENTS.md\n",
        "AGENTS.md": "skills: a, b-x, c\n"}, 1, "does not name b")),
    ("a quoted doc with a section name holding spaces", _case({
        "docs/facts.txt": FACTS + 'mention skills "AGENTS.md#Where things live"\n',
        "AGENTS.md": "## Where things live\na, b, c\n## Other\nnothing\n"}, 0, "all facts current")),
    ("a broken facts file exits 2", _case({
        "docs/facts.txt": "fact skills dirs skills\n"}, 2, "expected 'fact")),
    ("a state line on an unknown fact exits 2", _case({
        "docs/facts.txt": "state nope README.md {number} skills\n", "README.md": "x\n"}, 2, "no fact named")),
    ("pitmaster.md's facts_file names the file", _case({
        "pitmaster.md": "facts_file: facts.txt\n",
        "facts.txt": FACTS + "state skills README.md {number} skills\n", "README.md": "3 skills\n"},
        0, "all facts current")),
    ("--fix leaves CRLF line ends alone", _case({
        "docs/facts.txt": FACTS + "state skills README.md {number} skills\n",
        "README.md": "a\r\nfive skills\r\nb\r\n"}, 0, "fixed", fix=True, after=("README.md", "a\r\nthree skills\r\nb"))),
    ("number words up to ninety-nine", lambda base: read_number("Forty-two") == 42 and number_word(99) ==
     "ninety-nine" and write_number(21, "Two") == "Twenty-one"),
]


def _case_cli(base: pathlib.Path) -> bool:
    root = _tree(base, dict(SKILLS, **{"docs/facts.txt": FACTS + "state skills README.md {number} skills\n",
                                        "README.md": "two skills\n"}))
    script = pathlib.Path(__file__).resolve()
    done = subprocess.run([sys.executable, str(script), "--root", str(root)], capture_output=True, text=True)
    return done.returncode == 1 and "README.md:1:" in done.stdout


CASES.append(("the command line exits 1 on a finding", _case_cli))


def self_test() -> int:
    passed = 0
    for name, case in CASES:
        base = pathlib.Path(tempfile.mkdtemp(prefix="stale-facts-"))
        try:
            ok = bool(case(base))
        except Exception as exc:  # a case that raises has failed
            ok, name = False, "{} ({})".format(name, exc)
        finally:
            shutil.rmtree(base, ignore_errors=True)
        passed += ok
        print("{} {}".format("PASS" if ok else "FAIL", name))
    if passed != len(CASES):
        print("{}: self-test FAIL -- {} of {} cases passed".format(PROG, passed, len(CASES)))
        return 1
    print("{}: self-test PASS -- {} of {} cases passed".format(PROG, passed, len(CASES)))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog=PROG, description="Check the facts living docs state against the project.")
    parser.add_argument("--root", default=".", help="the project root (default: the current directory)")
    parser.add_argument("--facts", help="the facts file, relative to the root (default: docs/facts.txt)")
    parser.add_argument("--fix", action="store_true", help="rewrite stale values in docs that are not ruled")
    parser.add_argument("--self-test", action="store_true", help="run the built-in cases")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    root = pathlib.Path(args.root)
    if not root.is_dir():
        print("{}: no folder {}".format(PROG, args.root))
        return 2
    return run(root, args.facts, args.fix)


if __name__ == "__main__":
    sys.exit(main())
