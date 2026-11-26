#!/usr/bin/env python3
"""Check the decisions tables in a meeting record. Python 3.8+, standard library only.

Usage: record-check.py FILE [FILE ...]
       record-check.py FILE [FILE ...] --transcript TRANSCRIPT [--quote-fillers drop] [--words WORDS]
       record-check.py FILE [FILE ...] --shape minutes [--transcript TRANSCRIPT ...]
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
Without --transcript the script does not check that a citation is true, only that one is
there.

With --transcript (a transcript numbered T1, T2..., one "T<n>: text" line per turn, as
transcript-number writes it), it also checks that each citation is true:
  - every T-number the record names (T12, or a range T12-T15) exists in the transcript;
  - every decisions row's where cell holds at least one T-number;
  - every quote of three or more words, in straight or curly double quotes, is found word
    for word in the transcript (case, punctuation and spacing aside). A quote may drop
    words only with --quote-fillers drop, and then only fillers (uh, um, uhm, er, erm, ah,
    hmm, you know), each place marked [...] or [\u2026]; [sic] is ignored;
  - every agreed row has an assent word (yes, agreed, sounds good, +1 and the like) in
    the lines it cites or the three after each.
--words FILE names a project's words file (Markdown, sections "Corrections", "Names" and
"Assent", one "- " item per entry; see the README). A "heard" spelling of a correction
with no "only in" context, or a name the file substitutes, still in the record is a
finding (the name itself is never printed). Quotes are matched against the transcript as
written and again after the file's swaps. Assent items add to the built-in list.

--shape minutes checks minutes in place of decisions tables. Minutes have four sections,
in this order: Present (a heading, or a "Present:" line), Reports, Action items and
Pending; each item is one "- " bullet. The script checks that:
  - all four are there, in that order, and no Decisions, Corrections, Key points, Open
    questions or Topics section is (agreed items fold into Reports; corrections go in
    the record's check list);
  - every item in Reports, Action items and Pending cites a T-number ("- None." holds an
    empty section);
  - every action item is written "who: what";
  - no item says again what another says (two sentences sharing most of their words);
  - nothing Reports marks settled ("Agreed: ...", "Done: ...") is also in Pending.
With --transcript, the transcript checks above run too, and each "Agreed:" in Reports
needs an assent word in the lines its item cites or the three after each.

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
from typing import Callable, Dict, List, Optional, Sequence, Tuple

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


Row = Tuple[int, str, str]  # (line number, status, where cell) of a well-formed decisions row


def findings(path: pathlib.Path, collect: Optional[List[Row]] = None) -> Tuple[List[str], int, int]:
    """Return (findings, tables, rows) for one record; append well-formed rows to collect."""
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
            elif collect is not None and where_col is not None:
                collect.append((lineno, status, row[where_col]))
    close_heading()
    if tables == 0:
        out.append("{}: no decisions table (a table whose header starts | decision | status |)".format(path.name))
    return out, tables, rows


# ---------------------------------------------------------------- transcript checks (--transcript)

TLINE = re.compile(r"^\s*T(\d+):\s?(.*)$")
TREF = re.compile(r"(?<![\w-])(?<!tier )(?<!Tier )T(\d+)(?:\s*[-–—]\s*T(\d+)|[-–—](\d+))?(?![\w])")
QUOTE = re.compile(r"\"([^\"\n]+)\"|“([^”\n]+)”")
DROP = re.compile(r"\[(?:\.\.\.|…)\]")
SIC = re.compile(r"\[sic\]", re.IGNORECASE)
WORD = re.compile(r"[\w']+")
FILLERS = (("uh",), ("um",), ("uhm",), ("er",), ("erm",), ("ah",), ("hmm",), ("you", "know"))
ASSENT = ("yes", "yeah", "yep", "agreed", "agree", "sounds good", "no objection", "+1", "i will do it",
          "i'll do it", "i can do that", "let's do that", "works for me")
AFTER = 3  # an agreed row's yes may come in the cited lines or this many lines after each


def words_of(text: str) -> List[str]:
    return WORD.findall(text.replace("’", "'").replace("‘", "'").lower())


class Words:
    """A project's words file: corrections, names and extra assent words."""

    def __init__(self) -> None:
        self.flag: List[Tuple[str, ...]] = []           # heard forms to flag in a record (no "only in")
        self.names: List[Tuple[str, ...]] = []          # substituted-away names, never printed
        self.swaps: List[Tuple[Tuple[str, ...], Tuple[str, ...]]] = []
        self.assent: List[str] = []


def read_words(path: pathlib.Path) -> Words:
    w = Words()
    section = ""
    for lineno, line in enumerate(path.read_bytes().decode("utf-8").splitlines(), 1):
        heading = HEADING.match(line)
        if heading:
            title = bare(heading.group(2))
            section = next((n for n in ("corrections", "names", "assent") if title.startswith(n)), "")
            continue
        item = re.match(r"^\s*[-*]\s+(.*\S)\s*$", line)
        if not item or section not in ("corrections", "names", "assent"):
            continue
        text = item.group(1)
        if section == "assent":
            w.assent.append(text.strip().strip("\"").lower())
            continue
        context = re.search(r"\(\s*only in\b[^)]*\)\s*$", text, re.IGNORECASE)
        if context:
            text = text[:context.start()].rstrip()
        parts = re.split(r"\s*(?:→|->)\s*", text)
        if len(parts) == 1:
            if section == "names":
                raise ValueError("{}:{}: a Names item needs \"name -> name used\"".format(path.name, lineno))
            continue  # a right-spelling-only correction: advice for the writer, nothing to check
        if len(parts) != 2 or not parts[1].strip():
            raise ValueError("{}:{}: write an item as \"heard -> right\"".format(path.name, lineno))
        right = tuple(words_of(parts[1]))
        for heard in re.split(r"\s*[,/]\s*", parts[0]):
            form = tuple(words_of(heard))
            if not form:
                continue
            w.swaps.append((form, right))
            if section == "names":
                w.names.append(form)
            elif not context:
                w.flag.append(form)
    return w


def swapped(seq: Sequence[str], swaps: Sequence[Tuple[Tuple[str, ...], Tuple[str, ...]]]) -> List[str]:
    out: List[str] = []
    i = 0
    longest = sorted(swaps, key=lambda s: -len(s[0]))
    while i < len(seq):
        for heard, right in longest:
            if tuple(seq[i:i + len(heard)]) == heard:
                out.extend(right)
                i += len(heard)
                break
        else:
            out.append(seq[i])
            i += 1
    return out


def left_in(seq: Sequence[str], form: Sequence[str], rights: Sequence[Tuple[str, ...]]) -> bool:
    """True when form occurs in seq other than inside a longer right-hand form, so "Pat -> Pat Example" never
    flags "Pat Example" while "Pat Example -> Pat" still flags "Pat Example"."""
    n = len(form)
    for i in range(len(seq) - n + 1):
        if tuple(seq[i:i + n]) != tuple(form):
            continue
        inside = any(len(r) > n and tuple(seq[j:j + len(r)]) == r and j <= i and i + n <= j + len(r)
                     for r in rights for j in range(max(0, i - len(r) + n), i + 1))
        if not inside:
            return True
    return False


def contains(hay: Sequence[str], needle: Sequence[str]) -> bool:
    n = len(needle)
    return n > 0 and any(tuple(hay[i:i + n]) == tuple(needle) for i in range(len(hay) - n + 1))


def filler_at(hay: Sequence[str], j: int) -> int:
    """The length of a filler starting at j, or 0."""
    return next((len(f) for f in FILLERS if tuple(hay[j:j + len(f)]) == f), 0)


def found_with_fillers(hay: Sequence[str], parts: List[List[str]], lead: bool = False, trail: bool = False) -> bool:
    """Each part in order, with only one or more fillers between neighbours (and before the first, after the
    last, where the quote starts or ends with a [...] mark)."""
    def at(i: int, k: int) -> bool:
        part = parts[k]
        if tuple(hay[i:i + len(part)]) != tuple(part):
            return False
        j = i + len(part)
        if k == len(parts) - 1:
            return not trail or filler_at(hay, j) > 0
        skipped = False
        while True:
            for f in FILLERS:
                if tuple(hay[j:j + len(f)]) == f:
                    j += len(f)
                    skipped = True
                    break
            else:
                break
            if skipped and at(j, k + 1):
                return True
        return False
    def led(i: int) -> bool:
        return any(tuple(hay[i - len(f):i]) == f for f in FILLERS if i - len(f) >= 0)
    return bool(parts) and any(at(i, 0) for i in range(len(hay)) if not lead or led(i))


def has_assent(text: str, extra: Sequence[str]) -> bool:
    flat = text.replace("’", "'").replace("‘", "'")
    for phrase in tuple(ASSENT) + tuple(extra):
        if re.search(r"(?<![\w'])" + re.escape(phrase) + r"(?![\w'])", flat, re.IGNORECASE):
            return True
    return False


def read_transcript(path: pathlib.Path) -> Dict[int, str]:
    lines: Dict[int, str] = {}
    text = path.read_bytes().decode("utf-8")
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        m = TLINE.match(line)
        if m:
            lines[int(m.group(1))] = m.group(2)
    if not lines:
        raise ValueError("{}: no numbered lines (\"T1: ...\"); number it with transcript-number".format(path.name))
    return lines


def refs(text: str) -> List[int]:
    out: List[int] = []
    for m in TREF.finditer(text):
        a = int(m.group(1))
        if a == 0:
            continue  # T0 is a tier's name (ADR-032); a transcript starts at T1
        end = m.group(2) or m.group(3)
        b = int(end) if end else a
        out.extend(range(a, b + 1) if 0 <= b - a <= 500 else [a, b])
    return out


def transcript_findings(path: pathlib.Path, rows: List[Row], tx: Dict[int, str], fillers: bool,
                        words: Optional[Words]) -> List[str]:
    out: List[str] = []
    name = path.name
    lo, hi = min(tx), max(tx)
    text_lines = path.read_bytes().decode("utf-8").splitlines()
    hays = [words_of(tx[k]) for k in sorted(tx)]  # a quote comes from one line: one turn, one speaker
    hays += [swapped(h, words.swaps) for h in hays] if words else []
    rights = [r for _, r in words.swaps] if words else []
    extra = words.assent if words else []
    fence = ""
    for lineno, line in enumerate(text_lines, 1):
        mark = FENCE.match(line)
        if fence:
            if mark and mark.group(1)[0] == fence[0] and len(mark.group(1)) >= len(fence):
                fence = ""
            continue
        if mark:
            fence = mark.group(1)
            continue
        for t in sorted(set(refs(line))):
            if t not in tx:
                out.append("{}:{}: T{} is not in the transcript (it has T{}-T{})".format(name, lineno, t, lo, hi))
        for m in QUOTE.finditer(line):
            quote = m.group(1) or m.group(2)
            plain = SIC.sub(" ", quote)
            if not words_of(DROP.sub(" ", plain)):
                continue
            short = quote if len(quote) <= 60 else quote[:57] + "..."
            if words and any(left_in(words_of(quote), f, rights) for f in words.names):
                short = "(it holds a name the words file substitutes; not shown)"
            if DROP.search(plain):
                if not fillers:
                    out.append("{}:{}: quote drops words, and --quote-fillers is not drop: \"{}\"".format(
                        name, lineno, short))
                    continue
                pieces = [words_of(p) for p in DROP.split(plain)]
                lead, trail = not pieces[0], not pieces[-1]
                parts = [p for p in pieces if p]
                ok = any(found_with_fillers(h, parts, lead, trail) for h in hays)
            else:
                q = words_of(plain)
                ok = any(contains(h, q) for h in hays)
            if not ok:
                out.append("{}:{}: quote not found word for word in the transcript: \"{}\"".format(
                    name, lineno, short))
        if words:
            low = words_of(line)
            for form in words.flag:
                if left_in(low, form, rights):
                    out.append("{}:{}: \"{}\" is a heard spelling in the words file".format(
                        name, lineno, " ".join(form)))
            if any(left_in(low, form, rights) for form in words.names):
                out.append("{}:{}: a name the words file substitutes is still in the record".format(name, lineno))
    for lineno, status, where in rows:
        cited = [t for t in refs(where) if t in tx]
        if not refs(where):
            out.append("{}:{}: where cell cites no T-number".format(name, lineno))
            continue
        if status == "agreed" and cited:
            span = sorted({k for t in cited for k in range(t, t + AFTER + 1) if k in tx})
            if not has_assent(" ".join(tx[k] for k in span), extra):
                out.append("{}:{}: agreed, but no assent word in T{}-T{} (the cited lines and {} after)".format(
                    name, lineno, min(cited), max(cited) + AFTER, AFTER))
    return out


# ---------------------------------------------------------------- the minutes shape (--shape minutes)

SHAPES = ("decisions", "minutes")
MINUTES_SECTIONS = ("present", "reports", "action items", "pending")
NOT_IN_MINUTES = {
    "decisions": "agreed items fold into Reports",
    "corrections": "corrections go in the record's check list",
    "key points": "key points are Reports",
    "open questions": "open questions are Pending",
    "topics": "topics are Reports",
}
BULLET = re.compile(r"^ {0,3}(?:[-*+]|\d+[.)])\s+(.*)$")
PRESENT_LINE = re.compile(r"^\s*(?:\*\*|__)?present(?:\*\*|__)?\s*:", re.IGNORECASE)
WHO = re.compile(r"^\s*(?:\*\*|__)?[^:*_\s][^:*_]{0,59}?(?:\*\*|__)?\s*:(?:\*\*|__)?\s+\S")
SENTENCE = re.compile(r"(?<=[.;!?])\s+")
LABEL = re.compile(r"^\s*(?:\*\*|__)[^*_]{1,80}(?:\*\*|__)\s*")  # a bold topic label at an item's start
SETTLED = ("agreed", "done", "decided")
STOP = frozenset(
    "a an and are as at be been but by for from had has have he her his i if in into is it its me my of on or "
    "our she so than that the their them they this to us was we were what when which who will with would "
    "you your".split())
SAME = 0.75     # two sentences sharing this much of their content words say the same thing
LONG = 4        # a sentence needs this many content words before it can repeat another
Item = Tuple[int, str, str]  # (line number, section, text)


def stem(word: str) -> str:
    for end in ("ing", "ed", "s"):
        if len(word) > len(end) + 2 and word.endswith(end) and not word.endswith("ss"):
            return word[:-len(end)]
    return word


def content(sentence: str) -> frozenset:
    words = words_of(TREF.sub(" ", sentence))
    if words and words[0] in SETTLED:
        words = words[1:]
    return frozenset(stem(w) for w in words if w not in STOP and len(w) > 1)


def sentences(text: str) -> List[str]:
    return [s for s in SENTENCE.split(LABEL.sub("", text)) if s.strip()]


def settled(sentence: str) -> bool:
    words = words_of(sentence)
    return bool(words) and words[0] in SETTLED and bool(re.match(r"^\W*\w+\W*:", sentence.strip()))


def minutes_findings(path: pathlib.Path) -> Tuple[List[str], List[Item]]:
    """Return (findings, items) for one set of minutes."""
    lines = path.read_bytes().decode("utf-8").splitlines()
    name = path.name
    out: List[str] = []
    items: List[Item] = []
    seen: Dict[str, int] = {}
    section = ""
    fence = ""
    current: Optional[List] = None  # [line number, section, text] of the item being read

    def close() -> None:
        nonlocal current
        if current is not None:
            items.append((current[0], current[1], current[2].strip()))
            current = None

    for lineno, line in enumerate(lines, 1):
        mark = FENCE.match(line)
        if fence:
            if mark and mark.group(1)[0] == fence[0] and len(mark.group(1)) >= len(fence):
                fence = ""
            continue
        if mark:
            close()
            fence = mark.group(1)
            continue
        heading = HEADING.match(line)
        if heading:
            close()
            title = bare(heading.group(2)).rstrip(":")
            key = next((k for k in MINUTES_SECTIONS + tuple(NOT_IN_MINUTES)
                        if title == k or title.startswith(k + " ")), "")
            if key in NOT_IN_MINUTES:
                out.append("{}:{}: minutes have no \"{}\" section ({})".format(
                    name, lineno, heading.group(2).strip(), NOT_IN_MINUTES[key]))
            if key in MINUTES_SECTIONS:
                seen.setdefault(key, lineno)
            section = key if key in MINUTES_SECTIONS else ""
            continue
        if PRESENT_LINE.match(line):
            close()
            seen.setdefault("present", lineno)
            continue
        if not line.strip():
            close()
            continue
        bullet = BULLET.match(line)
        if bullet and section in MINUTES_SECTIONS[1:]:
            close()
            current = [lineno, section, bullet.group(1)]
        elif current is not None:
            current[2] += " " + line.strip()
    close()

    missing = [k for k in MINUTES_SECTIONS if k not in seen]
    for k in missing:
        out.append("{}: no \"{}\" section".format(name, k.capitalize()))
    order = [k for k in MINUTES_SECTIONS if k in seen]
    if [seen[k] for k in order] != sorted(seen[k] for k in order):
        out.append("{}: sections out of order: want Present, Reports, Action items, Pending".format(name))

    items = [it for it in items if bare(it[2]).rstrip(".") != "none"]  # "- None." holds an empty section
    for lineno, sec, text in items:
        if not refs(text):
            out.append("{}:{}: item cites no T-number".format(name, lineno))
        if sec == "action items" and not WHO.match(text):
            out.append("{}:{}: action item is not written \"who: what\"".format(name, lineno))

    split = [(lineno, sec, [(s, content(s), settled(s)) for s in sentences(text)]) for lineno, sec, text in items]
    for n, (lineno, _, mine) in enumerate(split):
        for other_line, _, theirs in split[:n]:
            if any(len(a) >= LONG and len(b) >= LONG and len(a & b) / len(a | b) >= SAME
                   for _, a, _ in mine for _, b, _ in theirs):
                out.append("{}:{}: says again what line {} says (say each thing once)".format(
                    name, lineno, other_line))
                break
    done = [(lineno, c) for lineno, sec, ss in split if sec == "reports" for _, c, s in ss if s and c]
    for lineno, sec, ss in split:
        if sec != "pending":
            continue
        for _, c, _ in ss:
            hit = next((d for d, s in done if len(c & s) >= 2 and len(c & s) / len(s) >= SAME), None)
            if hit is not None:
                out.append("{}:{}: pending, but line {} settles it in Reports".format(name, lineno, hit))
                break
    return out, items


def minutes_agreed(path: pathlib.Path, items: List[Item], tx: Dict[int, str],
                   words: Optional[Words]) -> List[str]:
    out: List[str] = []
    extra = words.assent if words else []
    for lineno, sec, text in items:
        if sec != "reports" or not any(words_of(s)[:1] == ["agreed"] and settled(s) for s in sentences(text)):
            continue
        cited = [t for t in refs(text) if t in tx]
        if not cited:
            continue
        span = sorted({k for t in cited for k in range(t, t + AFTER + 1) if k in tx})
        if not has_assent(" ".join(tx[k] for k in span), extra):
            out.append("{}:{}: \"Agreed\", but no assent word in T{}-T{} (the cited lines and {} after)".format(
                path.name, lineno, min(cited), max(cited) + AFTER, AFTER))
    return out


def run(paths: List[pathlib.Path], transcript: Optional[pathlib.Path] = None, fillers: bool = False,
        words_path: Optional[pathlib.Path] = None, shape: str = "decisions") -> int:
    problems: List[str] = []
    tables = rows = 0
    try:
        tx = None
        words = None
        for extra in (transcript, words_path):
            if extra is not None and not extra.is_file():
                raise ValueError("{}: no such file".format(extra))
        if transcript is not None:
            tx = read_transcript(transcript)
        if words_path is not None:
            words = read_words(words_path)
        for path in paths:
            if not path.is_file():
                raise ValueError("{}: no such file".format(path))
            if shape == "minutes":
                found, items = minutes_findings(path)
                problems += found
                rows += len(items)
                if tx is not None:
                    problems += transcript_findings(path, [], tx, fillers, words)
                    problems += minutes_agreed(path, items, tx, words)
                continue
            good_rows: List[Row] = []
            found, t, r = findings(path, good_rows)
            problems += found
            tables += t
            rows += r
            if tx is not None:
                problems += transcript_findings(path, good_rows, tx, fillers, words)
    except (OSError, UnicodeError, ValueError) as exc:
        print("{}: {}".format(PROG, exc), file=sys.stderr)
        return 2
    for problem in problems:
        print("FAIL " + problem)
    if shape == "minutes":
        print("{}: {} file(s), minutes, {} item(s), {} finding(s)".format(PROG, len(paths), rows, len(problems)))
    else:
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


TRANSCRIPT = (
    "T1: Sam: Welcome, everyone.\n\n"
    "T2: Alex: Should we move the call to Thursdays?\n\n"
    "T3: Sam: Uh, I think Thursdays work better for most of us.\n\n"
    "T4: Alex: Sounds good to me.\n\n"
    "T5: Sam: The docs are on apjay.org now.\n\n"
    "T6: Alex: Someone should write the release notes.\n\n"
    "T7: Sam: Let's look at the budget next time.\n"
)
TX_RECORD = (
    "# Record of the call\n\n"
    "Sam said \"I think Thursdays work better\" (T3).\n\n"
    "## Decisions\n\n"
    "| decision | status | where | who |\n"
    "|---|---|---|---|\n"
    "| Move the call to Thursdays | agreed | T2-T3 | Sam, Alex |\n"
    "| Write the release notes | proposed | T6 | - |\n"
    "| Look at the budget | open | T7 | - |\n"
)
WORDS = (
    "# Words\n\n## Corrections\n\n- apjay.org -> apache.org\n- tack, tax -> TAC (only in: travel/committee)\n"
    "- ComDev\n\n## Names\n\n- Samuel Q Private -> Sam\n\n## Assent\n\n- aye\n"
)


def _tx_case(record: str, expected: int, says: str = "", *, transcript: str = TRANSCRIPT, fillers: bool = False,
             words: str = "", absent: str = "") -> Callable[[pathlib.Path], bool]:
    def case(base: pathlib.Path) -> bool:
        rec = _write(base / "record.md", record)
        tx = _write(base / "record-transcript.md", transcript)
        wp = _write(base / "record-words.md", words) if words else None
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = run([rec], tx, fillers, wp)
        text = out.getvalue()
        return code == expected and says in text and (not absent or absent not in text)
    return case


_TX = [
    ("transcript: a true record", TX_RECORD, 0, "0 finding(s)", {}),
    ("transcript: a quote that keeps its filler", TX_RECORD.replace("\"I think", "\"Uh, I think"), 0, "", {}),
    ("transcript: quote_fillers drop, a filler marked [\u2026]",
     TX_RECORD.replace("\"I think", "\"[\u2026] I think").replace("(T3)", "(T3) and \"I [...] think Thursdays work\"")
     .replace("I [...] think", "Sam: [...] I think"), 0, "", {"fillers": True}),
    ("transcript: a quote with [sic] and curly quotes", TX_RECORD.replace("\"I think Thursdays work better\"",
     "\u201cI think Thursdays [sic] work better\u201d"), 0, "", {}),
    ("transcript: words file swaps apply to quotes", TX_RECORD + "\nSam: \"docs are on apache.org now\" (T5).\n",
     0, "0 finding(s)", {"words": WORDS}),
    ("transcript: a heard form with an only-in context is not flagged", TX_RECORD + "\nThe tax line, T7.\n", 0, "",
     {"words": WORDS}),
    ("transcript: an extra assent word from the words file", TX_RECORD.replace("Sounds good to me", "Aye from me"),
     0, "", {"words": WORDS, "transcript": TRANSCRIPT.replace("Sounds good to me", "Aye from me")}),
    ("transcript: tier names are not citations", TX_RECORD.replace("# Record of the call", "# Record of the call, tier T0 or tier T9"), 0, "0 finding(s)", {}),
    ("transcript: \"T6 - 9 people\" is not a range", TX_RECORD + "\nAt T6 - 9 people were there.\n", 0, "0 finding(s)", {}),
    ("transcript: a name whose right side holds its left side", TX_RECORD.replace("Sam, Alex", "Sam, Alex Example"), 0,
     "0 finding(s)", {"words": "## Names\n\n- Alex -> Alex Example\n"}),
    ("transcript: a trailing [\u2026] over a filler", TX_RECORD.replace("\"I think Thursdays work better\"",
     "\"Sam: [\u2026] I think Thursdays work better for most of us.\""), 0, "", {"fillers": True}),
    ("red: a two-word quote not in the transcript", TX_RECORD + "\nThe \"budget talk\" waits (T7).\n", 1,
     "quote not found", {}),
    ("red: a trailing [\u2026] hiding words, not fillers", TX_RECORD.replace("work better\"", "work [\u2026]\""), 1,
     "quote not found", {"fillers": True}),
    ("red: a quote spanning two lines", TX_RECORD + "\n\"to me. Sam: The docs\" (T4).\n", 1, "quote not found", {}),
    ("red: an em-dash range checks every line in it", TX_RECORD + "\nSee T6\u2014T9.\n", 1, "T8 is not in the transcript", {}),
    ("red: a heading with words after its name is still read", TX_RECORD + "\nThe docs moved to apjay.org (T5).\n", 1,
     "heard spelling", {"words": "## Corrections (heard -> right)\n\n- apjay.org -> apache.org\n"}),
    ("red: a name the right side shortens is still flagged", TX_RECORD + "\nAlex Example spoke (T2).\n", 1,
     "a name the words file substitutes", {"words": "## Names\n\n- Alex Example -> Alex\n"}),
    ("red: a failing quote holding a substituted name is not printed", TX_RECORD + "\n\"Samuel Q Private ships it\" (T3).\n",
     1, "not shown", {"words": WORDS, "absent": "Private ships"}),
    ("red: an out-of-range T-number", TX_RECORD.replace("| T7 |", "| T99 |"), 1,
     "record.md:11: T99 is not in the transcript (it has T1-T7)", {}),
    ("red: an altered quote", TX_RECORD.replace("work better", "works best"), 1,
     "record.md:3: quote not found word for word", {}),
    ("red: an agreed row with no assent word", TX_RECORD.replace("| proposed | T6 |", "| agreed | T6 |"), 1,
     "record.md:10: agreed, but no assent word in T6-T9", {}),
    ("red: a where cell with no T-number", TX_RECORD.replace("| T7 |", "| the end |"), 1,
     "record.md:11: where cell cites no T-number", {}),
    ("red: a dropped filler without --quote-fillers drop", TX_RECORD.replace("\"I think", "\"[\u2026] I think"), 1,
     "quote drops words, and --quote-fillers is not drop", {}),
    ("red: quote_fillers drop drops a word that is no filler", TX_RECORD.replace("I think Thursdays", "I [...] Thursdays"),
     1, "quote not found", {"fillers": True}),
    ("red: a heard spelling left in the record", TX_RECORD + "\nThe docs moved to apjay.org (T5).\n", 1,
     "\"apjay org\" is a heard spelling", {"words": WORDS}),
    ("red: a substituted name left in the record, never printed", TX_RECORD + "\nSamuel Q Private spoke (T3).\n", 1,
     "a name the words file substitutes is still in the record", {"words": WORDS, "absent": "Private"}),
]


def _case_tx_usage(base: pathlib.Path) -> bool:
    rec = _write(base / "record.md", TX_RECORD)
    unnumbered = _write(base / "plain.md", "Sam: hello\n")
    bad_words = _write(base / "w.md", "## Names\n\n- Only One Side\n")
    tx = _write(base / "t.md", TRANSCRIPT)
    codes = []
    for args in ((unnumbered, None), (tx, bad_words), (base / "absent.md", None)):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            codes.append(run([rec], args[0], False, args[1]))
    return codes == [2, 2, 2]


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

MINUTES = (
    "# Minutes of the call\n\n"
    "Present: Sam, Alex.\n\n"
    "## Reports\n\n"
    "- **Call day.** Sam finds Thursdays work better for most of us (T3). Agreed: move the call to\n"
    "  Thursdays (T2-T4).\n"
    "- **Docs.** The docs now live on apache.org (T5).\n\n"
    "## Action items\n\n"
    "- Alex: write the release notes (T6).\n\n"
    "## Pending\n\n"
    "- The budget, next time (T7).\n"
)


def _min_case(record: str, expected: int, says: str = "", *, transcript: bool = False,
              words: str = "") -> Callable[[pathlib.Path], bool]:
    def case(base: pathlib.Path) -> bool:
        rec = _write(base / "minutes.md", record)
        tx = _write(base / "minutes-transcript.md", TRANSCRIPT) if transcript or words else None
        wp = _write(base / "words.md", words) if words else None
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = run([rec], tx, False, wp, "minutes")
        return code == expected and says in out.getvalue()
    return case


_MIN = [
    ("minutes: a good set", MINUTES, 0, "minutes, 4 item(s), 0 finding(s)", {}),
    ("minutes: a good set, with its transcript and words file", MINUTES, 0, "0 finding(s)",
     {"words": WORDS}),
    ("minutes: Present as a heading, bold who, numbered items", MINUTES.replace("Present: Sam, Alex.",
     "## Present\n\nSam, Alex.").replace("- Alex: write", "1. **Alex:** write"), 0, "0 finding(s)", {}),
    ("minutes: an action and its report may share a topic word", MINUTES.replace("The budget, next time",
     "Whether the call on Thursdays needs an agenda"), 0, "0 finding(s)", {}),
    ("minutes: \"None.\" holds an empty section", MINUTES.replace("- Alex: write the release notes (T6).",
     "- None."), 0, "minutes, 3 item(s), 0 finding(s)", {}),
    ("red: minutes with no Pending", MINUTES[:MINUTES.index("## Pending")], 1, "no \"Pending\" section", {}),
    ("red: minutes out of order", MINUTES.replace("## Action items", "## Later").replace("## Pending",
     "## Action items").replace("## Later", "## Pending").replace("- Alex: write the release notes (T6).",
     "- Alex: X (T7).").replace("- The budget, next time (T7).", "- Y (T6)."), 1, "out of order", {}),
    ("red: a Corrections section in minutes", MINUTES + "\n## Corrections\n\n- apjay.org to apache.org (T5).\n",
     1, "minutes have no \"Corrections\" section", {}),
    ("red: a Decisions section in minutes", MINUTES.replace("## Action items", "## Decisions\n\n- X (T2).\n\n"
     "## Action items"), 1, "agreed items fold into Reports", {}),
    ("red: an item with no T-number", MINUTES.replace("release notes (T6)", "release notes"), 1,
     "minutes.md:13: item cites no T-number", {}),
    ("red: an action not written who: what", MINUTES.replace("- Alex: write", "- Write"), 1,
     "minutes.md:13: action item is not written \"who: what\"", {}),
    ("red: an agreed item restated as an action", MINUTES.replace("- **Docs.** The docs now live on apache.org (T5).",
     "- **Notes.** Agreed: Alex writes the release notes (T6)."), 1, "minutes.md:13: says again what line 9 says", {}),
    ("red: the same point in Reports and Pending", MINUTES.replace("The budget, next time",
     "Sam finds Thursdays work better for most people"), 1, "minutes.md:17: says again what line 7 says", {}),
    ("red: settled in Reports, still pending", MINUTES.replace("The budget, next time",
     "Whether to move the call to Thursdays"), 1, "minutes.md:17: pending, but line 7 settles it", {}),
    ("red: Agreed with no assent word in its lines", MINUTES.replace("The docs now live on apache.org (T5).",
     "Agreed: Sam keeps the docs page up to date (T7)."), 1, "minutes.md:9: \"Agreed\", but no assent word",
     {"transcript": True}),
    ("red: a heard spelling left in minutes", MINUTES.replace("apache.org", "apjay.org"), 1,
     "heard spelling", {"words": WORDS}),
]

_CASES: List[Tuple[str, Callable[[pathlib.Path], bool]]] = (
    [(row[0], _table_case(*row[1:])) for row in _TABLE]
    + [(row[0], _tx_case(row[1], row[2], row[3], **row[4])) for row in _TX]
    + [(row[0], _min_case(row[1], row[2], row[3], **row[4])) for row in _MIN]
    + [
        ("red: two files, the bad one named", _case_two_files_one_bad),
        ("usage: an unnumbered transcript, a bad words file, a missing transcript", _case_tx_usage),
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
    parser.add_argument("--transcript", metavar="FILE", help="the numbered transcript (T1: ...) the records cite")
    parser.add_argument("--quote-fillers", choices=("keep", "drop"), default="keep",
                        help="drop: a quote may leave out fillers, each place marked [...] (default keep)")
    parser.add_argument("--words", metavar="FILE", help="the project's words file (corrections, names, assent)")
    parser.add_argument("--shape", choices=SHAPES, default="decisions",
                        help="minutes: check minutes (Present, Reports, Action items, Pending), not decisions tables")
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
    if args.transcript is None and (args.words or args.quote_fillers == "drop"):
        print("{}: --words and --quote-fillers need --transcript".format(PROG), file=sys.stderr)
        return 2
    return run([pathlib.Path(f) for f in args.files],
               pathlib.Path(args.transcript) if args.transcript else None,
               args.quote_fillers == "drop",
               pathlib.Path(args.words) if args.words else None,
               args.shape)


if __name__ == "__main__":
    sys.exit(main())
