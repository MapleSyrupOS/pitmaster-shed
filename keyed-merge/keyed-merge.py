#!/usr/bin/env python3
"""keyed-merge: merge the shared tables many branches add to by key, not by line; stop only on a real conflict.

Usage: keyed-merge.py driver BASE OURS THEIRS PATH [--marker-size N]      (git calls this; see install)
       keyed-merge.py merge BASE OURS THEIRS --path PATH [-o OUT] [--format F] [--key K] [--order O]
       keyed-merge.py preview REV_A REV_B [PATH ...]
       keyed-merge.py check [PATH ...]                                       (also: --check)
       keyed-merge.py regen [PATH ...]
       keyed-merge.py install [--trust-commands]
       keyed-merge.py trust [--all | COMMAND]
       keyed-merge.py lint [--fail] [PATH ...]
       keyed-merge.py --self-test
       Every command takes --json (the same report, for agents) and --root DIR (default: the repository top).

What it does
    Some checked-in files are tables that nearly every branch adds to: a census, an allowlist, a ledger, a list of
    test cases. Git merges them line by line, so two branches that each append a row conflict, and git's own
    ``merge=union`` keeps both sides' lines even when both changed the same row, writing it twice with no conflict.
    keyed-merge reads each version as rows with a key and merges by key:

        a row changed (added, edited or deleted) on one side only   that side's version
        the same change on both sides                               taken once
        changed two different ways, or added on both differently    CONFLICT
        deleted on one side and changed on the other                CONFLICT
        a duplicate key in any input                                refused (bad input)

    Row order comes from the config, never from either side: ``sorted`` by key, or ``added`` (the base's rows in
    the base's order, then our new rows, then theirs). A clean row keeps its own text, byte for byte. Before a
    merge is called clean, the result is read back in its format and must hold exactly the rows decided; if not,
    the file falls back to git's whole-file line conflict.

The config: .keyed-merge at the repository root
    One table a line. A line starting with ``#``, or ``#`` after a space before any ``regen=``, is a comment.
    Fields split on spaces; ``regen=`` takes the rest of the line, ``#`` included.

        <path or glob>  <format>  [key=FIELD[,FIELD...]]  [order=sorted|added]  [regen=<command>]
        lint fail                 (the lint exits 1 on a finding instead of reporting only)
        lint also <glob> ...      (more files for the lint to read)

    Formats: ``lines`` (one name a line; the line is the key), ``tsv`` and ``csv`` (a header row; key= names
    columns, default the first), ``md-table`` (one Markdown table, with text before and after it), ``jsonl`` (one
    JSON object a line; key= is required), ``json-array`` (a JSON list of objects; key= is required) and
    ``json-object`` (a JSON object; its top-level names are the keys). Order defaults to ``sorted``. Globs: ``*``
    and ``?`` stay in one folder, ``**`` crosses folders, a trailing ``/`` means everything below.

    Then name the same files in ``.gitattributes`` with ``merge=keyed-merge`` (or ``merge=tablemerge``, the same
    tool under its second name), and run ``install`` once in each clone.

install
    Copies this script into the clone's git folder (``<git dir>/keyed-merge/keyed-merge.py``) and registers both
    driver names there, so no branch can change the code a merge runs. Run it again to take a newer copy.

Generated tables (regen=)
    A table a script writes should be regenerated, not merged. Git calls a merge driver before the merged files
    exist, so the driver cannot run the script: when both sides changed a generated table, the driver (and the
    merge and preview commands) keep our side, stop on that file, and say to run ``regen`` once the rest of the
    merge is in place. ``regen`` runs the command from the root, but only a command this clone approved (``trust``,
    or ``install --trust-commands``), recorded word for word in this clone's own git settings as
    ``keyed-merge.trust``. A branch that changes the command in .keyed-merge cannot make it run.

Conflicts
    Every clean row is written. Each conflicted row is written between git's usual markers, ours above and theirs
    below (an empty side means that side deleted the row; for JSON the whole record sits between them), and the
    report gives one line per conflicted key. When the text outside the rows (a header, comments at the top, the
    text around a Markdown table) changed on both sides, or a header changed at all, the whole file falls back to
    ``git merge-file``'s line conflict and the report says why.

What it promises
    - Exit 0: clean. Exit 1: conflicts (or a generated table to regenerate, or a check or lint finding). Exit 2:
      bad input (a duplicate key, a file that does not parse or cannot be read, a bad config line). Exit 3:
      refused (an unapproved regen command). Exit 4: it could not run (bad arguments, no config line for the file,
      no git where git is needed).
    - It never drops or invents a row silently: a row it cannot place is a conflict.
    - It writes only the merged file (and, for install and trust, this clone's git folder and settings); never
      commits, pushes or changes branches; never reaches the network.
    - A clean merge is a prediction, not a measurement: when both sides changed a table it says so, so the checks
      that read the table can be run.
    - JSON numbers are compared and written as their exact text; NaN and Infinity are refused.

lint
    Flags bare counts (``CASES=412``, ``"total": 9``) in the configured tables, the files beside them and any
    ``lint also`` files, and suggests a keyed list: a list merges, and the reader can count it. A line holding
    ``keyed-merge: ok`` is skipped. Reports only (exit 0) unless ``--fail`` or the config's ``lint fail``.

--self-test
    Runs every arm for every format it applies to, checking whole rows (keys and values), then runs each arm again
    against a deliberately broken copy of the tool (the break that arm exists to catch) and requires every format
    it applies to to fail there. Ends with real merges, a rebase, a cherry-pick and a generated table through the
    installed driver in a scratch repository. One ok or FAIL line per case.

Python 3.8 or newer, standard library only; git on the path for driver, preview, install, trust and regen.
macOS, Linux, Windows. tablemerge.py beside this file is a launcher for the same script.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

PROG = "keyed-merge"
ALIAS = "tablemerge"
CONFIG = ".keyed-merge"
TRUST_KEY = "keyed-merge.trust"
FORMATS = ("lines", "tsv", "csv", "md-table", "jsonl", "json-array", "json-object")
JSONISH = ("json-array", "json-object")
ORDERS = ("sorted", "added")
OK, CONFLICT, BAD, REFUSED, CANNOT = 0, 1, 2, 3, 4
VERDICT = {OK: "clean", CONFLICT: "conflict", BAD: "bad-input", REFUSED: "refused", CANNOT: "could-not-run"}

# The self-test sets one deliberate break at a time, to prove each arm can fail. The driver reads it from the
# environment so the end-to-end arms can break a driver that git starts. Unset, it changes nothing.
BREAK = os.environ.get("KEYED_MERGE_SELFTEST_BREAK", "")


class BadInput(Exception):
    """A file that cannot be merged by key as it stands: a duplicate key, unreadable text, a bad config line."""


class CannotRun(Exception):
    """Missing config, missing git, bad arguments."""


# --- text ------------------------------------------------------------------------------------------------------

LINE = re.compile(r"[^\n]*\n|[^\n]+\Z")


def split_lines(text: str) -> List[str]:
    """Split on LF only (CRLF stays on its line); never on the other Unicode line separators."""
    if BREAK == "splitlines":
        return text.splitlines(keepends=True)
    return LINE.findall(text)


def detect_eol(text: str) -> str:
    if BREAK == "lf-only":
        return "\n"
    return "\r\n" if "\r\n" in text else "\n"


def ended(line: str, eol: str) -> str:
    return line if line.endswith("\n") else line + eol


def read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            return f.read()
    except (OSError, UnicodeDecodeError) as e:
        raise BadInput(f"cannot read {path}: {e}")


def write_text(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


# --- the config ------------------------------------------------------------------------------------------------

def glob_regex(glob: str) -> "re.Pattern[str]":
    """Turn a glob into a regex over the whole path (ripplecheck's rules)."""
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


@dataclass
class Spec:
    pattern: str
    fmt: str
    key: List[str] = field(default_factory=list)
    order: str = "sorted"
    regen: Optional[str] = None
    line: int = 0

    def matches(self, path: str) -> bool:
        return bool(glob_regex(self.pattern).match(path.replace("\\", "/")))


def validate_spec(spec: Spec, where: str) -> None:
    if spec.fmt not in FORMATS:
        raise BadInput(f"{where}: unknown format `{spec.fmt}` (one of {', '.join(FORMATS)})")
    if spec.order not in ORDERS:
        raise BadInput(f"{where}: order is sorted or added")
    if spec.fmt in ("jsonl", "json-array") and not spec.key:
        raise BadInput(f"{where}: {spec.fmt} needs key=FIELD")
    if spec.fmt in ("lines", "json-object") and spec.key:
        raise BadInput(f"{where}: {spec.fmt} takes no key= (the {'line' if spec.fmt == 'lines' else 'name'} is the key)")


@dataclass
class Config:
    specs: List[Spec] = field(default_factory=list)
    lint_fail: bool = False
    lint_also: List[str] = field(default_factory=list)

    def find(self, path: str) -> Optional[Spec]:
        for s in self.specs:
            if s.matches(path):
                return s
        return None


def parse_config(text: str) -> Config:
    cfg = Config()
    for n, raw in enumerate(split_lines(text), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        regen = None
        m = re.search(r"(?:^|\s)regen=(.*)$", line)
        hash_at = re.search(r"\s#", line)
        if m and hash_at and hash_at.start() < m.start() and BREAK != "comment-regen":
            m = None
        if m and BREAK != "config-no-regen":
            regen = m.group(1).strip()
            line = line[:m.start()].strip()
        line = re.sub(r"\s#.*$", "", line).strip()
        parts = line.split()
        where = f"{CONFIG}:{n}"
        if not parts:
            raise BadInput(f"{where}: a regen= with no table before it")
        if parts[0] == "lint":
            if parts[1:] == ["fail"]:
                cfg.lint_fail = True
            elif len(parts) > 2 and parts[1] == "also":
                cfg.lint_also += parts[2:]
            else:
                raise BadInput(f"{where}: a lint line is `lint fail` or `lint also <glob> ...`")
            continue
        if len(parts) < 2:
            raise BadInput(f"{where}: expected `<path> <format> ...`")
        spec = Spec(parts[0], parts[1], line=n, regen=regen or None)
        for opt in parts[2:]:
            k, _, v = opt.partition("=")
            if k == "key" and v:
                spec.key = [x for x in v.split(",") if x]
            elif k == "order" and v:
                spec.order = v
            elif BREAK != "config-lenient":
                raise BadInput(f"{where}: cannot read `{opt}` (key=A,B  order=sorted|added  regen=<command>)")
        if BREAK != "config-lenient":
            validate_spec(spec, where)
        cfg.specs.append(spec)
    return cfg


def load_config(root: str) -> Config:
    p = os.path.join(root, CONFIG)
    if not os.path.isfile(p):
        raise CannotRun(f"no {CONFIG} at {root}")
    return parse_config(read_text(p))


# --- JSON, with numbers kept as their exact text --------------------------------------------------------------

class Lit(str):
    """A JSON number, kept as the text it was written as."""


def bad_constant(name: str) -> Any:
    raise BadInput(f"{name} is not JSON (only finite numbers are)")


def no_dup_hook(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    d: Dict[str, Any] = {}
    for k, v in pairs:
        if k in d and BREAK != "no-dup-check":
            raise BadInput(f"duplicate key {k}")
        d[k] = v
    return d


DECODER = json.JSONDecoder(object_pairs_hook=no_dup_hook, parse_float=Lit, parse_int=Lit,
                           parse_constant=bad_constant)
WS = re.compile(r"[ \t\r\n]*")


def canon(v: Any) -> str:
    """A form for equality: object names sorted, numbers as written, no spacing."""
    if isinstance(v, Lit):
        return str(v)
    if isinstance(v, dict):
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + canon(x) for k, x in sorted(v.items())) + "}"
    if isinstance(v, list):
        return "[" + ",".join(canon(x) for x in v) + "]"
    return json.dumps(v, ensure_ascii=False)


def load_json(text: str, label: str) -> Any:
    try:
        i = WS.match(text, 0).end()       # type: ignore[union-attr]
        v, end = DECODER.raw_decode(text, i)
    except BadInput as e:
        raise BadInput(f"{label}: {e}")
    except ValueError as e:
        raise BadInput(f"{label}: not JSON ({e})")
    if text[end:].strip():
        raise BadInput(f"{label}: text after the JSON value")
    return v


def scan_container(text: str, opener: str, label: str) -> Tuple[List[Tuple[Optional[str], Any, str]], str]:
    """The top-level entries of a JSON object or array, each with its exact source text; and the indent."""
    closer = "}" if opener == "{" else "]"
    entries: List[Tuple[Optional[str], Any, str]] = []
    indent = "  "
    try:
        i = WS.match(text, 0).end()       # type: ignore[union-attr]
        if text[i:i + 1] != opener:
            raise BadInput(f"{label}: expected a JSON {'object' if opener == '{' else 'array'}")
        i += 1
        j = WS.match(text, i).end()       # type: ignore[union-attr]
        gap = text[i:j]
        if "\n" in gap:
            indent = gap.rsplit("\n", 1)[1]
        i = j
        if text[i:i + 1] == closer:
            i += 1
        else:
            while True:
                start = i
                name = None
                if opener == "{":
                    if text[i:i + 1] != '"' and BREAK != "json-name-lenient":
                        raise BadInput(f"{label}: an object name is not a string")
                    name, i = DECODER.raw_decode(text, i)
                    if not isinstance(name, str) or (isinstance(name, Lit) and BREAK != "json-name-lenient"):
                        raise BadInput(f"{label}: an object name is not a string")
                    i = WS.match(text, i).end()       # type: ignore[union-attr]
                    if text[i:i + 1] != ":":
                        raise BadInput(f"{label}: expected `:` after {name!r}")
                    i = WS.match(text, i + 1).end()   # type: ignore[union-attr]
                value, i = DECODER.raw_decode(text, i)
                entries.append((name, value, text[start:i]))
                i = WS.match(text, i).end()           # type: ignore[union-attr]
                if text[i:i + 1] == ",":
                    i = WS.match(text, i + 1).end()   # type: ignore[union-attr]
                    continue
                if text[i:i + 1] == closer:
                    i += 1
                    break
                raise BadInput(f"{label}: expected `,` or `{closer}` at offset {i}")
    except BadInput as e:
        raise BadInput(str(e) if str(e).startswith(label) else f"{label}: {e}")
    except ValueError as e:
        raise BadInput(f"{label}: not JSON ({e})")
    if text[i:].strip():
        raise BadInput(f"{label}: text after the JSON value")
    return entries, indent


def key_part(v: Any) -> Any:
    """A key component that keeps its type: the number 1 and the string "1" are different keys."""
    if BREAK == "untyped-keys":
        return str(v) if isinstance(v, str) else canon(v)
    if isinstance(v, Lit):
        try:
            return (0, Decimal(str(v)), str(v))
        except InvalidOperation:
            return (2, str(v))
    if isinstance(v, str):
        return (1, v)
    return (2, canon(v))


def show_part(c: Any) -> str:
    return c if isinstance(c, str) else str(c[-1])


def show_key(key: Tuple[Any, ...]) -> str:
    return ",".join(show_part(c) for c in key)


def key_of(rec: Dict[str, Any], fields: List[str], label: str) -> Tuple[Any, ...]:
    out = []
    for f in fields:
        if f not in rec:
            raise BadInput(f"{label}: a record has no key field `{f}`")
        out.append(key_part(rec[f]))
    return tuple(out)


# --- tables ----------------------------------------------------------------------------------------------------

@dataclass
class Row:
    key: Tuple[Any, ...]
    norm: str      # what equality compares: the row's content, with formatting taken out
    text: str      # what is written: the row's own text, as it stood


@dataclass
class Table:
    head: str = ""
    rows: List[Row] = field(default_factory=list)
    tail: str = ""
    header: Optional[Tuple[str, ...]] = None
    indent: str = "  "    # JSON only
    eol: str = "\n"


def check_dups(rows: List[Row], label: str) -> None:
    if BREAK == "no-dup-check":
        return
    seen = set()
    for r in rows:
        if r.key in seen:
            raise BadInput(f"{label}: duplicate key {show_key(r.key)}")
        seen.add(r.key)


def header_index(header: Tuple[str, ...], spec: Spec, label: str) -> List[int]:
    names = spec.key or [header[0]]
    idx = []
    for n in names:
        if n not in header:
            raise BadInput(f"{label}: no column `{n}` in the header")
        idx.append(header.index(n))
    return idx


def lead_comments(lines: List[str]) -> int:
    i = 0
    while i < len(lines) and (not lines[i].strip() or lines[i].lstrip().startswith("#")):
        i += 1
    return i


def parse_lines(text: str, spec: Spec, label: str) -> Table:
    t = Table(eol=detect_eol(text))
    lines = split_lines(text)
    i = lead_comments(lines)
    t.head = "".join(lines[:i])
    for ln in lines[i:]:
        s = ln.rstrip("\r\n").rstrip()
        if not s or s.lstrip().startswith("#"):
            raise BadInput(f"{label}: a blank or comment line among the rows (keep comments at the top)")
        t.rows.append(Row((s,), s, ended(ln, t.eol)))
    return t


def parse_tsv(text: str, spec: Spec, label: str) -> Table:
    t = Table(eol=detect_eol(text))
    lines = split_lines(text)
    i = lead_comments(lines)
    if i == len(lines):
        t.head = "".join(lines)
        return t
    t.header = tuple(lines[i].rstrip("\r\n").split("\t"))
    t.head = "".join(lines[:i]) + ended(lines[i], t.eol)
    idx = header_index(t.header, spec, label)
    for ln in lines[i + 1:]:
        s = ln.rstrip("\r\n")
        if not s.strip():
            raise BadInput(f"{label}: a blank line among the rows")
        cells = s.split("\t")
        if max(idx) >= len(cells):
            raise BadInput(f"{label}: a row has fewer columns than its key needs: {s!r}")
        t.rows.append(Row(tuple(cells[j] for j in idx), "\t".join(cells), ended(ln, t.eol)))
    return t


def csv_records(lines: List[str]) -> List[str]:
    """Group lines into whole CSV records: a quoted field may hold a line break."""
    if BREAK == "csv-parity-off":
        return list(lines)
    out, cur = [], ""
    for ln in lines:
        cur += ln
        if cur.count('"') % 2 == 0:
            out.append(cur)
            cur = ""
    if cur:
        out.append(cur)
    return out


def csv_rows(text: str, label: str) -> List[List[str]]:
    try:
        return list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except csv.Error as e:
        raise BadInput(f"{label}: CSV does not parse ({e})")


def parse_csv(text: str, spec: Spec, label: str) -> Table:
    t = Table(eol=detect_eol(text))
    lines = split_lines(text)
    i = lead_comments(lines)
    recs = csv_records(lines[i:])
    if not recs:
        t.head = "".join(lines)
        return t
    for rec in recs:
        if not rec.strip():
            raise BadInput(f"{label}: a blank line among the rows")
    whole = csv_rows("".join(recs), label)
    each = [csv_rows(r, label) for r in recs]
    if len(whole) != len(recs) or any(len(e) != 1 or e[0] != w for e, w in zip(each, whole)):
        raise BadInput(f"{label}: CSV records do not split cleanly (an unclosed quote?)")
    t.header = tuple(whole[0])
    t.head = "".join(lines[:i]) + ended(recs[0], t.eol)
    idx = header_index(t.header, spec, label)
    for rec, cells in zip(recs[1:], whole[1:]):
        if max(idx) >= len(cells):
            raise BadInput(f"{label}: a row has fewer columns than its key needs: {rec.strip()!r}")
        t.rows.append(Row(tuple(cells[j] for j in idx), json.dumps(cells, ensure_ascii=False), ended(rec, t.eol)))
    return t


MD_SEP = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
MD_PIPE = re.compile(r"(?<!\\)\|")


def md_cells(line: str) -> List[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip() for c in MD_PIPE.split(s)]


def parse_md(text: str, spec: Spec, label: str) -> Table:
    t = Table(eol=detect_eol(text))
    lines = split_lines(text)
    seps = [i for i, ln in enumerate(lines)
            if i > 0 and MD_SEP.match(ln.rstrip("\r\n")) and MD_PIPE.search(lines[i - 1])
            and len(md_cells(lines[i - 1])) == len(md_cells(ln))]
    if not seps:
        t.head = text
        return t
    if len(seps) > 1:
        raise BadInput(f"{label}: more than one Markdown table (keyed-merge merges one table a file)")
    s = seps[0]
    t.header = tuple(md_cells(lines[s - 1]))
    t.head = "".join(lines[:s + 1])
    idx = header_index(t.header, spec, label)
    e = s + 1
    while e < len(lines) and lines[e].strip():
        if not MD_PIPE.search(lines[e]):
            raise BadInput(f"{label}: a line inside the table has no `|`: {lines[e].strip()!r} "
                           f"(end the table with a blank line)")
        cells = md_cells(lines[e])
        if max(idx) >= len(cells):
            raise BadInput(f"{label}: a row has fewer cells than its key needs: {lines[e].strip()!r}")
        norm = "|".join(cells) if BREAK == "md-norm-joined" else json.dumps(cells, ensure_ascii=False)
        t.rows.append(Row(tuple(cells[j] for j in idx), norm, ended(lines[e], t.eol)))
        e += 1
    t.tail = "" if BREAK == "md-tail-dropped" else "".join(lines[e:])
    return t


def parse_jsonl(text: str, spec: Spec, label: str) -> Table:
    t = Table(eol=detect_eol(text))
    for ln in split_lines(text):
        if not ln.strip():
            raise BadInput(f"{label}: a blank line among the records")
        rec = load_json(ln, label)
        if not isinstance(rec, dict):
            raise BadInput(f"{label}: a line is not a JSON object")
        t.rows.append(Row(key_of(rec, spec.key, label), canon(rec), ended(ln, t.eol)))
    return t


def parse_json_array(text: str, spec: Spec, label: str) -> Table:
    t = Table(eol=detect_eol(text), head="[", tail="]")
    if not text.strip():
        return t
    entries, t.indent = scan_container(text, "[", label)
    for _, rec, raw in entries:
        if not isinstance(rec, dict):
            raise BadInput(f"{label}: json-array wants a list of objects")
        t.rows.append(Row(key_of(rec, spec.key, label), canon(rec), raw))
    return t


def parse_json_object(text: str, spec: Spec, label: str) -> Table:
    t = Table(eol=detect_eol(text), head="{", tail="}")
    if not text.strip():
        return t
    entries, t.indent = scan_container(text, "{", label)
    for name, value, raw in entries:
        t.rows.append(Row(((1, name),), canon(value), raw))
    return t


PARSERS: Dict[str, Callable[[str, Spec, str], Table]] = {
    "lines": parse_lines, "tsv": parse_tsv, "csv": parse_csv, "md-table": parse_md,
    "jsonl": parse_jsonl, "json-array": parse_json_array, "json-object": parse_json_object,
}


def parse(text: str, spec: Spec, label: str) -> Table:
    t = PARSERS[spec.fmt](text, spec, label)
    check_dups(t.rows, label)
    return t


@dataclass
class Conflict:
    key: Tuple[Any, ...]
    kind: str
    base: Optional[Row]
    ours: Optional[Row]
    theirs: Optional[Row]


Item = Any   # a Row, or a Conflict


def render(spec: Spec, t: Table, items: List[Item], marker: int = 7) -> str:
    eol = t.eol
    lt, eq, gt = "<" * marker + " ours" + eol, "=" * marker + eol, ">" * marker + " theirs" + eol
    if spec.fmt in JSONISH:
        if not items:
            return t.head + t.tail + eol
        # Conflicts next to each other form one block, each side a run of whole entries. A comma goes after an
        # entry, or after a block side's last entry, only when a clean entry (one that stays whichever side is
        # chosen) follows; a block after the last clean entry starts each non-empty side with a comma instead. So
        # choosing either side of every block, deletions included, leaves valid JSON.
        groups: List[Any] = []
        for it in items:
            if isinstance(it, Conflict) and groups and isinstance(groups[-1], list) and BREAK != "json-comma-naive":
                groups[-1].append(it)
            else:
                groups.append([it] if isinstance(it, Conflict) else it)
        clean = [not isinstance(g, list) for g in groups]
        out = [t.head + eol]
        for n, grp in enumerate(groups):
            later = any(clean[n + 1:])
            earlier = any(clean[:n])
            comma = "," if later or (BREAK == "json-comma-naive" and n < len(groups) - 1) else ""
            if isinstance(grp, list):
                lead = "," + eol if (not later and earlier and BREAK != "json-comma-naive") else ""
                sides = ([c.ours for c in grp if c.ours and BREAK != "drop-ours-in-conflict"],
                         [c.theirs for c in grp if c.theirs])
                for s, rows in enumerate(sides):
                    out.append(lt if s == 0 else eq)
                    if rows:
                        out.append(lead + ("," + eol).join(t.indent + r.text for r in rows) + comma + eol)
                out.append(gt)
            else:
                out.append(t.indent + grp.text + comma + eol)
        return "".join(out) + t.tail + eol
    out = [t.head]
    for it in items:
        if isinstance(it, Conflict):
            out.append(lt)
            if it.ours and BREAK != "drop-ours-in-conflict":
                out.append(it.ours.text)
            out.append(eq)
            if it.theirs:
                out.append(it.theirs.text)
            out.append(gt)
        else:
            out.append(it.text)
    return "".join(out) + t.tail


# --- the merge -------------------------------------------------------------------------------------------------

@dataclass
class Result:
    path: str
    code: int = OK
    text: Optional[str] = None
    conflicts: List[Conflict] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    from_ours: int = 0
    from_theirs: int = 0
    reason: str = ""


def same(a: Optional[Row], b: Optional[Row]) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return a.norm == b.norm


def merge_blob(b: str, o: str, t: str) -> Optional[str]:
    if o == t or b == t:
        return o
    if b == o:
        return t
    return None


def order_keys(spec: Spec, base: Table, ours: Table, theirs: Table, keys: List[Tuple[Any, ...]]) -> List[Tuple[Any, ...]]:
    want = set(keys)
    if BREAK == "added-sorted" or (spec.order == "sorted" and BREAK != "order-from-ours"):
        return sorted(want)
    if BREAK == "order-from-ours":
        seq = [r.key for r in ours.rows] + [r.key for r in theirs.rows]
    else:
        seq = [r.key for r in base.rows] + [r.key for r in ours.rows] + [r.key for r in theirs.rows]
    out, seen = [], set()
    for k in seq:
        if k in want and k not in seen:
            out.append(k)
            seen.add(k)
    return out


def merge_tables(spec: Spec, base: Table, ours: Table, theirs: Table, path: str, marker: int = 7) -> Result:
    res = Result(path)
    headers = {h for h in (base.header, ours.header, theirs.header) if h is not None}
    head = merge_blob(base.head, ours.head, theirs.head)
    tail = merge_blob(base.tail, ours.tail, theirs.tail)
    if (len(headers) > 1 and BREAK != "header-ignored") or head is None or tail is None:
        res.code = CONFLICT
        res.reason = ("the header changed" if len(headers) > 1 and BREAK != "header-ignored"
                      else "the text outside the rows changed on both sides")
        return res
    if spec.fmt in ("tsv", "csv", "md-table") and BREAK != "no-readback":
        hdr = ours.header if head == ours.head else theirs.header if head == theirs.head else base.header
        rows_left = any(r for r in ours.rows) or any(r for r in theirs.rows)
        if hdr is None and rows_left:
            res.code = CONFLICT
            res.reason = "one side has no header (it emptied the file?) while rows remain: merge it by hand"
            return res
    B = {r.key: r for r in base.rows}
    O = {r.key: r for r in ours.rows}
    T = {r.key: r for r in theirs.rows}
    keys = list(dict.fromkeys(list(B) + list(O) + list(T)))
    chosen: Dict[Tuple[Any, ...], Item] = {}
    for k in keys:
        b, o, t = B.get(k), O.get(k), T.get(k)
        if same(o, t):
            pick: Optional[Row] = o
            if BREAK == "twice-when-same" and o is not None and t is not None and not same(b, o):
                chosen[k + ("\x00",)] = o
        elif same(b, o):
            pick = t
            res.from_theirs += 1
            if BREAK == "drop-theirs-adds" and b is None:
                pick = None
            if BREAK == "ignore-deletes" and t is None:
                pick = b
            if BREAK == "stale-value" and b is not None and t is not None:
                pick = b
        elif same(b, t):
            pick = o
            res.from_ours += 1
            if BREAK == "ignore-deletes" and o is None:
                pick = b
        elif BREAK == "no-conflicts":
            pick = o or t
        else:
            if b is None:
                kind = "added on both sides differently"
            elif o is None or t is None:
                kind = "deleted on one side, changed on the other"
            else:
                kind = "changed two different ways"
            c = Conflict(k, kind, b, o, t)
            res.conflicts.append(c)
            chosen[k] = c
            continue
        if pick is not None:
            chosen[k] = pick
    order = order_keys(spec, base, ours, theirs, list(chosen))
    items = [chosen[k] for k in order]
    shell = Table(head=head, tail=tail, eol=ours.eol if ours.rows or ours.head else theirs.eol,
                  indent=ours.indent if ours.rows else theirs.indent)
    res.text = render(spec, shell, items, marker)
    if res.conflicts:
        res.code = CONFLICT
        return res
    # Read the result back: it must hold exactly the rows decided, in that order. Anything else (a header lost
    # with an emptied file, a row a parser would read differently) is a whole-file conflict, never a silent change.
    try:
        back = parse(res.text, spec, f"{path} (merged)")
        good = [(r.key, r.norm) for r in back.rows] == [(r.key, r.norm) for r in items]
    except BadInput:
        good = False
    if not good and BREAK != "no-readback":
        res.code = CONFLICT
        res.text = None
        res.reason = "the merged rows would not read back as decided (a side emptied the file or changed its shape)"
        return res
    changed_o = {k: r.norm for k, r in O.items()} != {k: r.norm for k, r in B.items()}
    changed_t = {k: r.norm for k, r in T.items()} != {k: r.norm for k, r in B.items()}
    if (changed_o and changed_t) if BREAK != "note-exclusive" else (res.from_ours and res.from_theirs):
        res.notes.append("both sides changed this table: a merge is a prediction, so run the checks that read it")
    return res


def merge_texts(spec: Spec, b: str, o: str, t: str, path: str, marker: int = 7) -> Result:
    try:
        tb = parse(b, spec, f"{path} (base)")
        to = parse(o, spec, f"{path} (ours)")
        tt = parse(t, spec, f"{path} (theirs)")
    except BadInput as e:
        return Result(path, BAD, reason=str(e))
    return merge_tables(spec, tb, to, tt, path, marker)


def git_merge_file(b: str, o: str, t: str, marker: int) -> Optional[str]:
    with tempfile.TemporaryDirectory() as d:
        paths = []
        for name, text in (("ours", o), ("base", b), ("theirs", t)):
            p = os.path.join(d, name)
            write_text(p, text)
            paths.append(p)
        try:      # bytes, not text: a CRLF inside a field is content, so no newline translation
            r = subprocess.run(["git", "merge-file", "-p", f"--marker-size={marker}", "-L", "ours", "-L", "base",
                                "-L", "theirs", *paths], capture_output=True, text=BREAK == "fallback-text-mode")
        except FileNotFoundError:
            return None
        out = r.stdout
        return out if isinstance(out, str) else out.decode("utf-8", errors="surrogateescape")


def generated_pick(b: str, o: str, t: str) -> Optional[str]:
    """For a generated table: the one side that changed it, or None when both changed it (regenerate)."""
    if BREAK == "merge-generated-stop-always":
        return None
    if b == o:
        return t
    if b == t or BREAK == "generated-identical-taken" and o == t:
        return o
    return None       # both sides changed it, even identically: regenerate from the merged inputs


def generated_stop(spec: Spec, path: str) -> Result:
    return Result(path, CONFLICT, reason=f"generated: both sides changed it, so it is not merged; once the rest of "
                                         f"the merge is in place, run `{PROG} regen {path}` (it runs: {spec.regen})")


# --- git -------------------------------------------------------------------------------------------------------

def run_git(root: str, *args: str, check: bool = True) -> "subprocess.CompletedProcess[str]":
    try:
        r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8",
                           errors="surrogateescape")
    except FileNotFoundError:
        raise CannotRun("git is not on the path")
    if check and r.returncode != 0:
        raise CannotRun(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r


def git_paths(root: str, *args: str) -> List[str]:
    """Paths from a git command run with -z: NUL-separated and never quoted."""
    return [p for p in run_git(root, *args, "-z").stdout.split("\0") if p]


def find_root(start: str) -> str:
    try:
        r = run_git(start, "rev-parse", "--show-toplevel", check=False)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except CannotRun:
        pass
    d = os.path.abspath(start)
    while True:
        if os.path.isfile(os.path.join(d, CONFIG)):
            return d
        up = os.path.dirname(d)
        if up == d:
            return os.path.abspath(start)
        d = up


def repo_files(root: str) -> List[str]:
    try:
        r = run_git(root, "ls-files", "-z", check=False)
        if r.returncode == 0:
            return [p for p in r.stdout.split("\0") if p]
    except CannotRun:
        pass
    out = []
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x != ".git"]
        for f in files:
            out.append(os.path.relpath(os.path.join(d, f), root).replace(os.sep, "/"))
    return out


def trusted(root: str) -> List[str]:
    """This clone's own approved commands, each whole (NUL-separated, so a value with line breaks stays one)."""
    if BREAK == "trust-lines":
        r = run_git(root, "config", "--local", "--get-all", TRUST_KEY, check=False)
        return r.stdout.splitlines()
    r = run_git(root, "config", "--local", "--null", "--get-all", TRUST_KEY, check=False)
    return [x for x in r.stdout.split("\0") if x] if r.returncode == 0 else []


# --- reporting -------------------------------------------------------------------------------------------------

def say(results: List[Result], as_json: bool, out: Any = None, extra: Optional[Dict[str, Any]] = None) -> int:
    out = out or sys.stdout
    code = max([r.code for r in results] or [OK])
    if as_json:
        doc: Dict[str, Any] = {"verdict": VERDICT[code], "exit": code, "files": [{
            "path": r.path, "verdict": VERDICT[r.code], "reason": r.reason, "notes": r.notes,
            "from_ours": r.from_ours, "from_theirs": r.from_theirs,
            "conflicts": [{"key": [show_part(c) for c in x.key], "kind": x.kind,
                           "base": x.base.norm if x.base else None, "ours": x.ours.norm if x.ours else None,
                           "theirs": x.theirs.norm if x.theirs else None} for x in r.conflicts]}
            for r in results]}
        doc.update(extra or {})
        print(json.dumps(doc, indent=2, ensure_ascii=False), file=out)
        return code
    for r in results:
        if r.code == OK:
            print(f"clean     {r.path}  ({r.from_ours} row(s) from ours, {r.from_theirs} from theirs)"
                  + (f"  {r.reason}" if r.reason else ""), file=out)
        elif r.conflicts:
            print(f"CONFLICT  {r.path}  {len(r.conflicts)} key(s)", file=out)
        else:
            print(f"{VERDICT[r.code].upper():<9} {r.path}  {r.reason}", file=out)
        for c in r.conflicts:
            print(f"  key {show_key(c.key)}: {c.kind}", file=out)
            for side, row in (("base", c.base), ("ours", c.ours), ("theirs", c.theirs)):
                print(f"    {side:<6} {row.norm if row else '(none)'}", file=out)
        for n in r.notes:
            print(f"  note: {n}", file=out)
    return code


# --- commands --------------------------------------------------------------------------------------------------

def root_of(a: argparse.Namespace) -> str:
    return find_root(getattr(a, "root", None) or os.getcwd())


def spec_for(root: str, path: str, a: argparse.Namespace) -> Spec:
    """The config's line for path, with any --format, --key and --order applied on top, then checked."""
    spec: Optional[Spec] = None
    try:
        spec = load_config(root).find(path)
    except CannotRun:
        if not getattr(a, "format", None):
            raise
    if spec is None:
        if not getattr(a, "format", None):
            raise CannotRun(f"{path}: no line in {CONFIG} names it (or give --format)")
        spec = Spec(path, a.format)
    else:
        spec = Spec(spec.pattern, spec.fmt, list(spec.key), spec.order, spec.regen, spec.line)
    if getattr(a, "format", None):
        spec.fmt = a.format
    if getattr(a, "key", None) is not None:
        spec.key = [x for x in a.key.split(",") if x]
    if getattr(a, "order", None):
        spec.order = a.order
    validate_spec(spec, "the options")
    return spec


def cmd_driver(a: argparse.Namespace) -> int:
    root = root_of(a)
    path = a.path.replace("\\", "/")
    spec = spec_for(root, path, a)
    b, o, t = read_text(a.base), read_text(a.ours), read_text(a.theirs)
    if spec.regen and BREAK != "merge-generated":
        pick = generated_pick(b, o, t)
        if pick is None:
            return say([generated_stop(spec, path)], a.json, sys.stderr)
        write_text(a.ours, pick)
        return say([Result(path, reason="generated: one side changed it; that side taken")], a.json, sys.stderr)
    r = merge_texts(spec, b, o, t, path, a.marker_size)
    if r.text is None and r.code == CONFLICT:
        r.text = git_merge_file(b, o, t, a.marker_size)
    if r.text is not None:
        write_text(a.ours, r.text)
    return say([r], a.json, sys.stderr)


def cmd_merge(a: argparse.Namespace) -> int:
    root = root_of(a)
    spec = spec_for(root, a.path, a)
    b, o, t = read_text(a.base), read_text(a.ours), read_text(a.theirs)
    if spec.regen:
        pick = generated_pick(b, o, t)
        if pick is None:
            return say([generated_stop(spec, a.path)], a.json)
        r = Result(a.path, text=pick, reason="generated: one side changed it; that side taken")
    else:
        r = merge_texts(spec, b, o, t, a.path)
    if r.text is None and r.code == CONFLICT:
        r.text = git_merge_file(b, o, t, 7)
    if r.text is not None:
        if a.output:
            write_text(a.output, r.text)
        elif not a.json:
            sys.stdout.write(r.text)
    extra = {"text": r.text} if a.json and not a.output else None
    return say([r], a.json, sys.stdout if a.output or a.json else sys.stderr, extra)


def show(root: str, rev: str, path: str) -> str:
    """A file's text at a revision, byte for byte (no newline translation), or "" where it does not exist."""
    r = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=root, capture_output=True)
    if r.returncode != 0:
        return ""
    if BREAK == "show-text-mode":
        return r.stdout.decode("utf-8").replace("\r\n", "\n")
    try:
        return r.stdout.decode("utf-8")
    except UnicodeDecodeError as e:
        raise BadInput(f"{path} at {rev}: not UTF-8 ({e})")


def cmd_preview(a: argparse.Namespace) -> int:
    root = root_of(a)
    cfg = load_config(root)
    base = run_git(root, "merge-base", a.rev_a, a.rev_b).stdout.strip()
    both = sorted(set(git_paths(root, "diff", "--name-only", f"{base}..{a.rev_a}"))
                  & set(git_paths(root, "diff", "--name-only", f"{base}..{a.rev_b}")))
    results = []
    for p in both:
        if a.paths and p not in a.paths:
            continue
        spec = cfg.find(p)
        if spec is None:
            continue
        b, o, t = show(root, base, p), show(root, a.rev_a, p), show(root, a.rev_b, p)
        if spec.regen:
            results.append(generated_stop(spec, p) if generated_pick(b, o, t) is None
                           else Result(p, reason="generated: one side changed it; that side taken"))
            continue
        results.append(merge_texts(spec, b, o, t, p))
    if not results and not a.json:
        print("no configured table was changed on both sides")
    return say(results, a.json)


def check_text(spec: Spec, text: str, path: str) -> Result:
    r = Result(path)
    try:
        t = parse(text, spec, path)
    except BadInput as e:
        return Result(path, BAD, reason=str(e))
    if BREAK == "check-lenient":
        return r
    if render(spec, t, list(t.rows)) != text:
        r.code = CONFLICT
        r.reason = "not in canonical form, so a merge would rewrite its layout"
        r.reason += (" (JSON: one entry a line at one indent, the closing bracket on its own line)"
                     if spec.fmt in JSONISH else " (every line must end with a line break)")
    elif spec.order == "sorted" and [x.key for x in t.rows] != sorted(x.key for x in t.rows):
        r.code = CONFLICT
        r.reason = "order=sorted, but the rows are not sorted by key"
    return r


def configured_files(root: str, cfg: Config) -> List[Tuple[str, Spec]]:
    out = []
    for p in repo_files(root):
        s = cfg.find(p)
        if s is not None and os.path.isfile(os.path.join(root, p)):
            out.append((p, s))
    return out


def cmd_check(a: argparse.Namespace) -> int:
    root = root_of(a)
    cfg = load_config(root)
    files = configured_files(root, cfg)
    if a.paths:
        files = [(p, s) for p, s in files if p in a.paths]
    results = []
    for p, s in files:
        try:
            results.append(check_text(s, read_text(os.path.join(root, p)), p))
        except BadInput as e:
            results.append(Result(p, BAD, reason=str(e)))
    if a.json:
        return say(results, True)
    for r in results:
        print(f"{'ok' if r.code == OK else VERDICT[r.code].upper():<9} {r.path}  {'' if r.code == OK else r.reason}")
    return max([r.code for r in results] or [OK])


def cmd_regen(a: argparse.Namespace) -> int:
    root = root_of(a)
    cfg = load_config(root)
    ok_cmds = trusted(root)
    if a.paths:
        todo = [(p, cfg.find(p)) for p in a.paths]
    else:   # only the unmerged files that are generated tables; other conflicts are not regen's business
        todo = [(p, cfg.find(p)) for p in git_paths(root, "diff", "--name-only", "--diff-filter=U")]
        todo = [(p, s) for p, s in todo if s is not None and s.regen]
    results = []
    for p, spec in todo:
        if spec is None or not spec.regen:
            results.append(Result(p, CANNOT, reason=f"no regen= command for it in {CONFIG}"))
            continue
        if spec.regen not in ok_cmds and BREAK != "trust-all":
            results.append(Result(p, REFUSED, reason=f"command not approved in this clone: {spec.regen}  "
                                                    f"(approve it with `{PROG} trust` after reading it)"))
            continue
        r = subprocess.run(spec.regen, shell=True, cwd=root, capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        if r.returncode != 0:
            results.append(Result(p, CONFLICT, reason=f"`{spec.regen}` exited {r.returncode}: "
                                                      f"{(r.stderr or r.stdout).strip()[-300:]}"))
            continue
        full = os.path.join(root, p)
        if not os.path.isfile(full):
            results.append(Result(p, BAD, reason=f"`{spec.regen}` ran but did not write {p}"))
            continue
        c = check_text(Spec(p, spec.fmt, spec.key, spec.order), read_text(full), p)
        if c.code == BAD and BREAK != "regen-unchecked":
            results.append(c)
            continue
        results.append(Result(p, OK, notes=[f"regenerated; now `git add {p}`"]))
    if not results and not a.json:
        print("nothing to regenerate: no unmerged file is a generated table")
    return say(results, a.json)


def shell_quote(s: str) -> str:
    """For the command git runs through sh: quoted for the shell, with git's own % doubled."""
    return shlex.quote(s).replace("%", "%%")


def cmd_install(a: argparse.Namespace) -> int:
    root = root_of(a)
    gd = run_git(root, "rev-parse", "--git-common-dir").stdout.strip()
    gd = gd if os.path.isabs(gd) else os.path.join(root, gd)
    home = os.path.join(gd, PROG)
    os.makedirs(home, exist_ok=True)
    dest = os.path.join(home, f"{PROG}.py")
    if BREAK == "driver-in-place":
        dest = os.path.abspath(__file__)
    else:
        shutil.copyfile(os.path.abspath(__file__), dest)
    py = sys.executable.replace("\\", "/")
    drv = f"{shell_quote(py)} {shell_quote(dest.replace(chr(92), '/'))} driver %O %A %B %P --marker-size %L"
    for name in (PROG, ALIAS):
        run_git(root, "config", "--local", f"merge.{name}.name", f"{PROG}: merge tables by key")
        run_git(root, "config", "--local", f"merge.{name}.driver", drv)
    lines = [f"installed: merge={PROG} and merge={ALIAS} in this clone, running {dest}"]
    report: Dict[str, Any] = {"installed": dest, "drivers": [PROG, ALIAS], "notes": [], "approved": [],
                              "not_approved": []}
    try:
        cfg = load_config(root)
    except CannotRun as e:
        cfg = Config()
        report["notes"].append(f"{e}; write it before the driver can merge anything")
    attrs_p = os.path.join(root, ".gitattributes")
    attrs = read_text(attrs_p) if os.path.isfile(attrs_p) else ""
    for s in cfg.specs:
        if not re.search(rf"^{re.escape(s.pattern)}\s.*merge=({PROG}|{ALIAS})\b", attrs, re.M):
            report["notes"].append(f".gitattributes has no line for {s.pattern}; add:  {s.pattern} merge={PROG}")
    have = trusted(root)
    for c in [s.regen for s in cfg.specs if s.regen]:
        if c in have:
            continue
        if a.trust_commands:
            run_git(root, "config", "--local", "--add", TRUST_KEY, c)
            report["approved"].append(c)
        else:
            report["not_approved"].append(c)
    if a.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return OK
    for line in lines:
        print(line)
    for n in report["notes"]:
        print(f"note: {n}")
    for c in report["approved"]:
        print(f"approved: {c}")
    for c in report["not_approved"]:
        print(f"not approved yet (regen refuses it until `{PROG} trust` approves it): {c}")
    return OK


def cmd_trust(a: argparse.Namespace) -> int:
    root = root_of(a)
    have = trusted(root)
    if a.command:
        wanted = [a.command]
    else:
        wanted = [s.regen for s in load_config(root).specs if s.regen]   # type: ignore[misc]
        if not a.all:
            rows = [{"command": c, "approved": c in have} for c in wanted]
            if a.json:
                print(json.dumps({"commands": rows}, indent=2, ensure_ascii=False))
            else:
                for r in rows:
                    print(f"{'approved    ' if r['approved'] else 'not approved'}  {r['command']}")
            return OK
    done = []
    for c in wanted:
        if c not in have:
            run_git(root, "config", "--local", "--add", TRUST_KEY, c)
            done.append(c)
    if a.json:
        print(json.dumps({"approved": done}, indent=2, ensure_ascii=False))
    else:
        for c in done:
            print(f"approved: {c}")
    return OK


COUNT_WORD = r"(?:count|total|num|number|cases|tally|floor|refusals|ran)"
LINT_ASSIGN = re.compile(rf"^\s*(?:export\s+)?([A-Za-z_]*{COUNT_WORD}[A-Za-z0-9_]*|[A-Za-z_][A-Za-z0-9_]*_N|N_[A-Za-z0-9_]+)"
                         r"\s*[=:]\s*(\d+)\s*(?:#.*)?$", re.I)
LINT_JSON = re.compile(rf'"([A-Za-z_]*{COUNT_WORD}[A-Za-z0-9_]*|n_[a-z0-9_]+)"\s*:\s*(\d+)', re.I)


def lint_text(text: str, path: str) -> List[str]:
    if BREAK == "lint-blind":
        return []
    out = []
    for n, ln in enumerate(split_lines(text), 1):
        if "keyed-merge: ok" in ln:
            continue
        m = LINT_ASSIGN.match(ln.rstrip("\r\n")) or LINT_JSON.search(ln)
        if m:
            out.append(f"{path}:{n}  {m.group(1)} = {m.group(2)}  a bare count; a keyed list (one row an item) "
                       f"merges, and the reader can count it")
    return out


def cmd_lint(a: argparse.Namespace) -> int:
    root = root_of(a)
    cfg = load_config(root)
    files = repo_files(root)
    if a.paths:
        chosen = list(a.paths)
    else:
        tables = [p for p in files if cfg.find(p)]
        dirs = {os.path.dirname(p) for p in tables}
        also = [p for p in files if any(glob_regex(g).match(p) for g in cfg.lint_also)]
        chosen = sorted({p for p in files if os.path.dirname(p) in dirs} | set(tables) | set(also))
    finds: List[str] = []
    for p in chosen:
        full = os.path.join(root, p)
        try:
            if os.path.getsize(full) > 2_000_000:
                continue
            finds += lint_text(read_text(full), p)
        except (OSError, BadInput):
            continue
    if a.json:
        print(json.dumps({"findings": finds, "files": len(chosen)}, indent=2, ensure_ascii=False))
    else:
        for f in finds:
            print(f"LINT  {f}")
        print(f"lint: {len(finds)} bare count(s) in {len(chosen)} file(s)")
    return CONFLICT if finds and (a.fail or cfg.lint_fail) else OK


# --- the self-test ---------------------------------------------------------------------------------------------

# Each format's test table is built from (key, value) pairs, so one set of arms runs over all seven.
def build(fmt: str, rows: List[Tuple[str, str]]) -> str:
    if fmt == "lines":
        return "# names\n" + "".join(f"{k}={v}\n" for k, v in rows)
    if fmt == "tsv":
        return "name\tvalue\n" + "".join(f"{k}\t{v}\n" for k, v in rows)
    if fmt == "csv":
        return "name,value\n" + "".join(f'{k},"{v}"\n' for k, v in rows)
    if fmt == "md-table":
        return "# Ledger\n\n| name | value |\n|---|---|\n" + "".join(f"| {k} | {v} |\n" for k, v in rows) + \
            "\nNotes after the table.\n"
    if fmt == "jsonl":
        return "".join(json.dumps({"name": k, "value": v}) + "\n" for k, v in rows)
    if fmt == "json-array":
        return json.dumps([{"name": k, "value": v} for k, v in rows], indent=2) + "\n"
    return json.dumps({k: {"value": v} for k, v in rows}, indent=2) + "\n"


def spec_of(fmt: str, order: str = "sorted") -> Spec:
    key = ["name"] if fmt in ("jsonl", "json-array", "tsv", "csv", "md-table") else []
    return Spec("t", fmt, key, order)


def pairs_in(fmt: str, text: Optional[str]) -> List[Tuple[str, str]]:
    """The (key, value) of each clean row in a merged text, conflict blocks left out."""
    if text is None:
        return []
    if "<<<<<<<" in text:
        text = re.sub(r"<<<<<<< ours\r?\n.*?>>>>>>> theirs\r?\n", "", text, flags=re.S)
        if fmt in JSONISH:
            text = re.sub(r",(\s*[\]}])", r"\1", text)
    t = parse(text, spec_of(fmt), "merged")
    out = []
    for r in t.rows:
        k = show_key(r.key)
        if fmt == "lines":
            k, _, v = k.partition("=")
        elif fmt == "tsv":
            v = r.norm.split("\t")[1]
        elif fmt == "csv":
            v = json.loads(r.norm)[1]
        elif fmt == "md-table":
            v = json.loads(r.norm)[1]
        else:
            v = json.loads(r.norm)["value"]
        out.append((k, v))
    return out


def resolve(fmt: str, text: Optional[str], side: str) -> List[Tuple[str, str]]:
    """Take one side of every conflict block, as a person would, and read the result back."""
    if text is None:
        return []
    keep = 1 if side == "ours" else 2
    text = re.sub(r"<<<<<<< ours\r?\n(.*?)=======\r?\n(.*?)>>>>>>> theirs\r?\n",
                  lambda m: m.group(keep), text, flags=re.S)
    if fmt in JSONISH:
        json.loads(text)          # a resolved JSON table must be valid JSON on its own
    return pairs_in(fmt, text)


BASE = [("a", "1"), ("b", "2")]
NOT_LINES = tuple(f for f in FORMATS if f != "lines")   # a names list has no value to change, so never conflicts


def mt(fmt: str, b: List[Tuple[str, str]], o: List[Tuple[str, str]], t: List[Tuple[str, str]],
       order: str = "sorted") -> Result:
    return merge_texts(spec_of(fmt, order), build(fmt, b), build(fmt, o), build(fmt, t), "t")


def arm_union(fmt: str) -> bool:
    r = mt(fmt, BASE, BASE + [("c", "3")], BASE + [("d", "4")])
    return r.code == OK and pairs_in(fmt, r.text) == BASE + [("c", "3"), ("d", "4")]


def arm_value_one_side(fmt: str) -> bool:
    r = mt(fmt, BASE, BASE, [("a", "1"), ("b", "7")])
    return r.code == OK and pairs_in(fmt, r.text) == [("a", "1"), ("b", "7")]


def arm_two_ways(fmt: str) -> bool:
    # git's merge=union writes both a=9 and a=5 here, with no conflict (tested on git 2.54.0)
    r = mt(fmt, BASE, [("a", "9"), ("b", "2"), ("d", "4")], [("a", "5"), ("b", "2"), ("c", "3")])
    return (r.code == CONFLICT and len(r.conflicts) == 1 and show_key(r.conflicts[0].key) == "a"
            and r.text is not None and r.text.count("<<<<<<< ours") == 1
            and pairs_in(fmt, r.text) == [("b", "2"), ("c", "3"), ("d", "4")])


def arm_names_both_kept(fmt: str) -> bool:
    r = mt(fmt, BASE, [("a", "9"), ("b", "2")], [("a", "5"), ("b", "2")])
    return r.code == OK and pairs_in(fmt, r.text) == [("a", "5"), ("a", "9"), ("b", "2")]


def arm_conflict_holds_both(fmt: str) -> bool:
    r = mt(fmt, BASE, [("a", "9"), ("b", "2"), ("d", "4")], [("a", "5"), ("b", "2"), ("c", "3")])
    return (resolve(fmt, r.text, "ours") == [("a", "9"), ("b", "2"), ("c", "3"), ("d", "4")]
            and resolve(fmt, r.text, "theirs") == [("a", "5"), ("b", "2"), ("c", "3"), ("d", "4")])


def arm_conflict_resolves(fmt: str) -> bool:
    """Delete against edit on the last row: choosing either side leaves a valid table (JSON: no stray comma)."""
    r = mt(fmt, BASE, [("a", "1")], [("a", "1"), ("b", "7")])
    r2 = mt(fmt, [("a", "1"), ("b", "2"), ("c", "3")], [("a", "1"), ("c", "3")], [("a", "1"), ("b", "7"), ("c", "3")])
    r3 = mt(fmt, [("a", "1"), ("b", "2"), ("c", "3")], [("a", "1"), ("b", "8")], [("a", "1"), ("b", "9")])
    r4 = mt(fmt, [("a", "1"), ("b", "1")], [("a", "2"), ("b", "2")], [("a", "3"), ("b", "3")])   # all conflict
    r5 = mt(fmt, [("a", "1"), ("b", "1"), ("c", "1")], [("a", "2"), ("c", "2")], [("a", "3"), ("b", "3")])
    return (resolve(fmt, r.text, "ours") == [("a", "1")] and resolve(fmt, r.text, "theirs") == [("a", "1"), ("b", "7")]
            and resolve(fmt, r2.text, "ours") == [("a", "1"), ("c", "3")]
            and resolve(fmt, r2.text, "theirs") == [("a", "1"), ("b", "7"), ("c", "3")]
            and resolve(fmt, r3.text, "ours") == [("a", "1"), ("b", "8")]
            and resolve(fmt, r3.text, "theirs") == [("a", "1"), ("b", "9")]
            and resolve(fmt, r4.text, "ours") == [("a", "2"), ("b", "2")]
            and resolve(fmt, r4.text, "theirs") == [("a", "3"), ("b", "3")]
            and resolve(fmt, r5.text, "ours") == [("a", "2"), ("c", "2")]
            and resolve(fmt, r5.text, "theirs") == [("a", "3"), ("b", "3")])


def arm_emptied_with_conflict(fmt: str) -> bool:
    """One side empties the file while another row conflicts: still no output that loses the header."""
    r = merge_texts(spec_of(fmt), build(fmt, [("a", "1")]), "", build(fmt, [("a", "2"), ("b", "3")]), "t")
    return r.code == CONFLICT and (r.text is None or not r.text.startswith("<<<<<<<"))


def arm_delete_edit(fmt: str) -> bool:
    r = mt(fmt, BASE, [("a", "1")], [("a", "1"), ("b", "7")])
    return r.code == CONFLICT and [c.kind for c in r.conflicts] == ["deleted on one side, changed on the other"]


def arm_duplicate(fmt: str) -> bool:
    if fmt == "json-object":    # build() cannot write a duplicate name through a dict; write one by hand
        r = merge_texts(spec_of(fmt), build(fmt, BASE), '{\n  "a": {"value": "1"},\n  "a": {"value": "2"}\n}\n',
                        build(fmt, BASE), "t")
    else:
        r = mt(fmt, BASE, BASE + [("a", "1")], BASE)
    return r.code == BAD and "duplicate" in r.reason


def arm_reorder(fmt: str) -> bool:
    r = mt(fmt, BASE, list(reversed(BASE)), BASE + [("c", "3")])
    return r.code == OK and pairs_in(fmt, r.text) == BASE + [("c", "3")]


def arm_same_change(fmt: str) -> bool:
    new = [("a", "1"), ("b", "2" if fmt == "lines" else "8"), ("z", "9")]
    r = mt(fmt, BASE, new, new)
    return r.code == OK and pairs_in(fmt, r.text) == new


def arm_one_side_delete(fmt: str) -> bool:
    r = mt(fmt, BASE, [("a", "1")], BASE + [("c", "3")])
    return r.code == OK and pairs_in(fmt, r.text) == [("a", "1"), ("c", "3")]


def arm_added_order(fmt: str) -> bool:
    r = mt(fmt, [("m", "1")], [("m", "1"), ("z", "2")], [("m", "1"), ("b", "3")], order="added")
    return r.code == OK and pairs_in(fmt, r.text) == [("m", "1"), ("z", "2"), ("b", "3")]


def arm_emptied(fmt: str) -> bool:
    """One side empties the file, the other adds a row: never a clean merge that loses the header."""
    r = merge_texts(spec_of(fmt), build(fmt, BASE), "", build(fmt, BASE + [("c", "3")]), "t")
    if r.code == OK:
        return pairs_in(fmt, r.text) == [("c", "3")] and check_text(spec_of(fmt), r.text or "", "t").code != BAD
    return r.code == CONFLICT


def arm_check(fmt: str) -> bool:
    good = check_text(spec_of(fmt), build(fmt, BASE), "t").code == OK
    bad = check_text(spec_of(fmt), build(fmt, list(reversed(BASE))), "t").code == CONFLICT
    return good and bad


FORMAT_ARMS: List[Tuple[str, Callable[[str], bool], str, Tuple[str, ...]]] = [
    ("both sides add different rows: a clean union", arm_union, "drop-theirs-adds", FORMATS),
    ("a value changed on one side only: that value", arm_value_one_side, "stale-value", NOT_LINES),
    ("one key changed two ways: a conflict, not union's double row", arm_two_ways, "no-conflicts", NOT_LINES),
    ("names list: a=9 and a=5 are two names, each kept once", arm_names_both_kept, "drop-theirs-adds", ("lines",)),
    ("deleted on one side, changed on the other: a conflict", arm_delete_edit, "no-conflicts", NOT_LINES),
    ("a conflict block holds both sides whole", arm_conflict_holds_both, "drop-ours-in-conflict", NOT_LINES),
    ("JSON: either side of a conflict resolves to valid JSON", arm_conflict_resolves, "json-comma-naive", JSONISH),
    ("one side empties the file while a row conflicts: the header is not lost", arm_emptied_with_conflict,
     "no-readback", ("tsv", "csv", "md-table")),
    ("a duplicate key in an input: refused", arm_duplicate, "no-dup-check", FORMATS),
    ("rows only reordered: no change, order from the config", arm_reorder, "order-from-ours", FORMATS),
    ("the same change on both sides: taken once", arm_same_change, "twice-when-same", FORMATS),
    ("deleted on one side only: deleted", arm_one_side_delete, "ignore-deletes", FORMATS),
    ("order=added: base rows, then ours, then theirs", arm_added_order, "added-sorted", FORMATS),
    ("one side empties the file: never clean with the header lost", arm_emptied, "no-readback",
     ("tsv", "csv", "md-table")),
    ("--check: canonical passes, unsorted fails", arm_check, "check-lenient", FORMATS),
]


def other_arms() -> List[Tuple[str, Callable[[], bool], str]]:
    def header_change() -> bool:
        b = build("tsv", BASE)
        r = merge_texts(spec_of("tsv"), b, b.replace("name\tvalue", "name\tvalue\tnote"),
                        build("tsv", BASE + [("c", "3")]), "t")
        return r.code == CONFLICT and r.text is None and "header" in r.reason

    def lint_finds() -> bool:
        txt = 'EVAL_RAN=726\nMAPPY_CASES = 41  # pinned\nname=x\nFLOOR: 54\nX=1  # keyed-merge: ok\n{"total": 9}\n'
        f = lint_text(txt, "m")
        return len(f) == 4 and "EVAL_RAN" in f[0] and "total" in f[3]

    def config_reads() -> bool:
        c = parse_config("# tables\ndocs/*.tsv tsv key=part,site order=added  # the census\ngen/c.json json-object "
                         "regen=python3 tools/gen.py --all # kept\nlint fail\nlint also tools/*.manifest\n")
        s, g = c.find("docs/census.tsv"), c.find("gen/c.json")
        return (s is not None and s.key == ["part", "site"] and s.order == "added" and g is not None
                and g.regen == "python3 tools/gen.py --all # kept" and c.lint_fail
                and c.lint_also == ["tools/*.manifest"] and c.find("docs/sub/x.tsv") is None)

    def config_refuses() -> bool:
        for bad in ("x.json json-array\n", "x.tsv yaml\n", "x.txt lines key=a\n", "x.tsv tsv order=random\n",
                    "regen=echo hi\n", "x.tsv tsv colour=red\n"):
            try:
                parse_config(bad)
                return False
            except BadInput:
                pass
        return True

    def crlf_kept() -> bool:
        b = "name\tvalue\r\na\t1\r\n"
        r = merge_texts(spec_of("tsv"), b, b + "c\t3\r\n", b + "d\t4", "t")
        j = '{\r\n  "a": 1\r\n}\r\n'
        rj = merge_texts(spec_of("json-object"), j, j, '{\r\n  "a": 1,\r\n  "b": 2\r\n}\r\n', "t")
        return (r.code == OK and r.text == "name\tvalue\r\na\t1\r\nc\t3\r\nd\t4\r\n"
                and rj.code == OK and rj.text == '{\r\n  "a": 1,\r\n  "b": 2\r\n}\r\n')

    def md_text_kept() -> bool:
        b = build("md-table", BASE)
        r = merge_texts(spec_of("md-table"), b, b.replace("Notes after", "Changed notes after"),
                        build("md-table", BASE + [("c", "3")]), "t")
        return r.code == OK and r.text is not None and "Changed notes after" in r.text and "| c | 3 |" in r.text

    def md_rows_without_border() -> bool:
        b = "| name | value |\n|---|---|\n| a | 1 |\n"
        r = merge_texts(spec_of("md-table"), b, b + "b | 2\n", b + "| b | 3 |\n", "t")
        return r.code == CONFLICT and [show_key(c.key) for c in r.conflicts] == ["b"]

    def csv_quotes() -> bool:
        b = 'name,value\na,"one\ntwo"\n'
        r = merge_texts(spec_of("csv"), b, b + "c,3\n", b + "d,4\n", "t")
        bad = merge_texts(spec_of("csv"), "name,value\na,1\n", 'name,value\na,"unterminated\nb,2\n',
                          "name,value\na,1\nc,3\n", "t")
        return r.code == OK and r.text == 'name,value\na,"one\ntwo"\nc,3\nd,4\n' and bad.code == BAD

    def json_numbers_exact() -> bool:
        sp = Spec("t", "json-object")
        big = merge_texts(sp, '{\n  "a": 9007199254740992.0\n}\n', '{\n  "a": 9007199254740993.0\n}\n',
                          '{\n  "a": 9007199254740994.0\n}\n', "t")
        inf = merge_texts(sp, '{\n  "a": 0\n}\n', '{\n  "a": 1e400\n}\n', '{\n  "a": 2e400\n}\n', "t")
        nan = merge_texts(sp, '{\n  "a": 0\n}\n', '{\n  "a": NaN\n}\n', '{\n  "a": 0\n}\n', "t")
        kept = merge_texts(sp, '{\n  "a": 1.50\n}\n', '{\n  "a": 1.50\n}\n', '{\n  "a": 1.50,\n  "b": 1e400\n}\n', "t")
        return (big.code == CONFLICT and inf.code == CONFLICT and nan.code == BAD and kept.code == OK
                and kept.text is not None and "1.50" in kept.text and "1e400" in kept.text)

    def typed_keys() -> bool:
        sp = Spec("t", "jsonl", ["id"])
        r = merge_texts(sp, "", '{"id": 1, "v": "n"}\n', '{"id": "1", "v": "s"}\n', "t")
        return r.code == OK and r.text is not None and r.text.count('"id"') == 2

    def md_escaped_pipes() -> bool:
        h = "| name | v | x |\n|---|---|---|\n"
        r = merge_texts(spec_of("md-table"), h + "| a | old | old |\n", h + "| a | b\\|c\\ | d |\n",
                        h + "| a | b\\ | c\\|d |\n", "t")
        return r.code == CONFLICT

    def json_names_strings() -> bool:
        return merge_texts(Spec("t", "json-object"), "{}\n", "{\n  1:2\n}\n", "{}\n", "t").code == BAD

    def generated_one_side() -> bool:
        return generated_pick("b", "b", "t") == "t" and generated_pick("b", "o", "b") == "o" and \
            generated_pick("b", "o", "t") is None

    def generated_identical() -> bool:
        return generated_pick("b", "x", "x") is None and generated_pick("b", "b", "b") == "b"

    def fallback_keeps_bytes() -> bool:
        body = 'id,v\na,"one\r\ntwo"\n'
        out = git_merge_file("#base\n" + body, "#ours\n" + body, "#theirs\n" + body, 7)
        return out is not None and '"one\r\ntwo"' in out

    def comment_before_regen() -> bool:
        c = parse_config("p lines # example regen=echo SHOULD_NOT_BE_CONFIG\n")
        return c.specs[0].regen is None

    def note_identical_change() -> bool:
        r = merge_texts(Spec("t", "json-object"), "{}\n", '{\n  "a": 1\n}\n', '{\n  "a": 1\n}\n', "t")
        return r.code == OK and bool(r.notes)

    def unicode_separators() -> bool:
        r = merge_texts(spec_of("tsv"), "name\tvalue\n", "name\tvalue\na\tone b\n", "name\tvalue\n", "t")
        return r.code == OK and len(pairs_in("tsv", r.text)) == 1

    return [
        ("tsv: a changed header falls back to a whole-file conflict", header_change, "header-ignored"),
        ("lint: finds bare counts, skips `keyed-merge: ok`", lint_finds, "lint-blind"),
        ("config: reads tables, inline comments, regen= and lint lines", config_reads, "config-no-regen"),
        ("config: refuses a bad line", config_refuses, "config-lenient"),
        ("CRLF line ends are kept, JSON too", crlf_kept, "lf-only"),
        ("md-table: text outside the table merges as text", md_text_kept, "md-tail-dropped"),
        ("md-table: a row without outer pipes is still a keyed row", md_rows_without_border, "no-conflicts"),
        ("csv: a quoted field may hold a line break; an unclosed quote is refused", csv_quotes, "csv-parity-off"),
        ("json: numbers compared and kept as written; NaN refused", json_numbers_exact, "no-conflicts"),
        ("json: the number 1 and the string \"1\" are different keys", typed_keys, "untyped-keys"),
        ("tsv: a Unicode line separator inside a field is not a row break", unicode_separators, "splitlines"),
        ("md-table: an escaped pipe never makes two different rows look the same", md_escaped_pipes,
         "md-norm-joined"),
        ("json: an object name must be a string, not a number", json_names_strings, "json-name-lenient"),
        ("generated: a table one side changed takes that side; both changed stops", generated_one_side,
         "merge-generated-stop-always"),
        ("config: a comment before regen= keeps it a comment", comment_before_regen, "comment-regen"),
        ("generated: both sides making the same change still regenerates", generated_identical,
         "generated-identical-taken"),
        ("the whole-file fallback keeps a CRLF inside a CSV field", fallback_keeps_bytes, "fallback-text-mode"),
        ("the both-sides note also fires when both made the same change", note_identical_change, "note-exclusive"),
    ]


def git_arms(tmp: str) -> List[Tuple[str, Callable[[], bool], str]]:
    """End to end: a scratch repository, the driver installed, real merges through git."""
    script = os.path.abspath(__file__)
    os.makedirs(tmp, exist_ok=True)
    empty = os.path.join(tmp, "empty.gitconfig")
    write_text(empty, "")
    gen_py = ("import sys\nrows = sys.argv[2]\nopen(sys.argv[1], 'w', newline='').write('name\\tvalue\\n' + "
              "''.join(r + '\\t1\\n' for r in rows))\n")
    py = f'"{sys.executable}"'

    def environ() -> Dict[str, str]:      # read at each call, so a break the self-test sets reaches git's driver
        return dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=empty, HOME=tmp, GIT_AUTHOR_NAME="t",
                    GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")

    def g(repo: str, *args: str, ok: bool = True) -> "subprocess.CompletedProcess[str]":
        r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, env=environ())
        if ok and r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {r.stderr}")
        return r

    def km(repo: str, *args: str) -> "subprocess.CompletedProcess[str]":
        return subprocess.run([sys.executable, script, *args], cwd=repo, capture_output=True, text=True,
                              env=environ())

    def fresh(name: str, attr: str = PROG) -> str:
        repo = os.path.join(tmp, name)
        os.makedirs(repo)
        g(repo, "init", "-q", "-b", "main")
        write_text(os.path.join(repo, CONFIG), f"census.tsv tsv key=name\nallow.json json-object\n"
                                               f"gen.tsv tsv regen={py} gen.py gen.tsv abg\n"
                                               f"bad.tsv tsv regen={py} gen.py bad.tsv aa\n")
        write_text(os.path.join(repo, ".gitattributes"),
                   "".join(f"{f} merge={attr}\n" for f in ("census.tsv", "allow.json", "gen.tsv", "bad.tsv")))
        write_text(os.path.join(repo, "gen.py"), gen_py)
        for f, fmt in (("census.tsv", "tsv"), ("allow.json", "json-object"), ("gen.tsv", "tsv"),
                       ("bad.tsv", "tsv")):
            write_text(os.path.join(repo, f), build(fmt, BASE))
        g(repo, "add", ".")
        g(repo, "commit", "-qm", "base")
        r = km(repo, "install")
        if r.returncode != 0:
            raise RuntimeError(r.stdout + r.stderr)
        return repo

    def branches(repo: str, ours: List[Tuple[str, str]], theirs: List[Tuple[str, str]], gen: bool = False) -> None:
        for branch, rows in (("side", theirs), ("main", ours)):
            if branch == "side":
                g(repo, "checkout", "-qb", "side")
            else:
                g(repo, "checkout", "-q", "main")
            write_text(os.path.join(repo, "census.tsv"), build("tsv", rows))
            write_text(os.path.join(repo, "allow.json"), build("json-object", rows))
            if gen:
                write_text(os.path.join(repo, "gen.tsv"), build("tsv", rows))
            g(repo, "commit", "-qam", branch)

    n = [0]

    def name() -> str:
        n[0] += 1
        return f"r{n[0]}"

    def unioned(repo: str) -> bool:
        want = BASE + [("c", "3"), ("d", "4")]
        return (pairs_in("tsv", read_text(os.path.join(repo, "census.tsv"))) == want
                and pairs_in("json-object", read_text(os.path.join(repo, "allow.json"))) == want)

    def merge_clean(attr: str = PROG) -> bool:
        repo = fresh(name(), attr)
        branches(repo, BASE + [("c", "3")], BASE + [("d", "4")])
        return g(repo, "merge", "-q", "side", "-m", "m", ok=False).returncode == 0 and unioned(repo)

    def merge_conflict() -> bool:
        repo = fresh(name())
        branches(repo, [("a", "9"), ("b", "2"), ("d", "4")], [("a", "5"), ("b", "2"), ("c", "3")])
        r = g(repo, "merge", "-q", "side", "-m", "m", ok=False)
        txt = read_text(os.path.join(repo, "census.tsv"))
        return r.returncode != 0 and txt.count("<<<<<<<") == 1 and "c\t3" in txt and "d\t4" in txt

    def rebase_clean() -> bool:
        repo = fresh(name())
        branches(repo, BASE + [("c", "3")], BASE + [("d", "4")])
        g(repo, "checkout", "-q", "side")
        return g(repo, "rebase", "-q", "main", ok=False).returncode == 0 and unioned(repo)

    def cherry_pick_clean() -> bool:
        repo = fresh(name())
        branches(repo, BASE + [("c", "3")], BASE + [("d", "4")])
        return g(repo, "cherry-pick", "side", ok=False).returncode == 0 and unioned(repo)

    def branch_cannot_change_driver() -> bool:
        """The driver runs the copy install put in the git folder, not a file a branch can edit."""
        repo = fresh(name())
        drv = g(repo, "config", "--local", "merge.keyed-merge.driver").stdout
        return ".git" in drv.replace("\\", "/") and os.path.isfile(os.path.join(repo, ".git", PROG, f"{PROG}.py"))

    def generated_stops() -> bool:
        repo = fresh(name())
        branches(repo, BASE + [("c", "3")], BASE + [("d", "4")], gen=True)
        r = g(repo, "merge", "-q", "side", "-m", "m", ok=False)
        kept = read_text(os.path.join(repo, "gen.tsv")) == build("tsv", BASE + [("c", "3")])
        refused = km(repo, "regen")
        steps = [("the merge stops", r.returncode != 0), ("our side is kept", kept),
                 ("the message names regen", "regen" in r.stdout + r.stderr),
                 ("regen refuses an unapproved command", refused.returncode == REFUSED),
                 ("trust --all", km(repo, "trust", "--all").returncode == 0)]
        ran = km(repo, "regen")
        steps += [("regen runs", ran.returncode == 0),
                  ("regen wrote the table", read_text(os.path.join(repo, "gen.tsv")) == "name\tvalue\na\t1\nb\t1\ng\t1\n"),
                  ("regen refuses output with a duplicate key", km(repo, "regen", "bad.tsv").returncode == BAD)]
        failed = [s for s, good in steps if not good]
        if failed:
            print(f"     (failed: {', '.join(failed)}; regen said: {(ran.stdout + ran.stderr).strip()[-300:]})")
        return not failed

    def preview_keeps_bytes() -> bool:
        """preview reads each version byte for byte: CRLF inside a quoted CSV field is content, not a line end."""
        repo = fresh(name())
        write_text(os.path.join(repo, CONFIG), read_text(os.path.join(repo, CONFIG)) + "q.csv csv key=id\n")
        write_text(os.path.join(repo, "q.csv"), 'id,v\na,"one\r\ntwo"\n')
        g(repo, "add", ".")
        g(repo, "commit", "-qm", "q")
        g(repo, "checkout", "-qb", "qside")
        write_text(os.path.join(repo, "q.csv"), 'id,v\na,"one\r\ntwo!"\n')
        g(repo, "commit", "-qam", "theirs")
        g(repo, "checkout", "-q", "main")
        write_text(os.path.join(repo, "q.csv"), 'id,v\na,"one\ntwo"\n')
        g(repo, "commit", "-qam", "ours")
        return km(repo, "preview", "main", "qside").returncode == CONFLICT

    def trust_is_whole() -> bool:
        """A command approved over several lines does not approve each of its lines alone."""
        repo = fresh(name())
        g(repo, "config", "--local", "--add", TRUST_KEY, f"if false; then\n{py} gen.py gen.tsv abg\nfi")
        return km(repo, "regen", "gen.tsv").returncode == REFUSED

    return [
        ("git: a merge of two appends through the driver is clean", merge_clean, "drop-theirs-adds"),
        ("git: merge=tablemerge runs the same tool", lambda: merge_clean(ALIAS), "drop-theirs-adds"),
        ("git: one key changed two ways stops the merge, one marked row", merge_conflict, "no-conflicts"),
        ("git: rebase goes through the driver", rebase_clean, "drop-theirs-adds"),
        ("git: cherry-pick goes through the driver", cherry_pick_clean, "drop-theirs-adds"),
        ("git: the driver runs install's copy in the git folder", branch_cannot_change_driver, "driver-in-place"),
        ("git: a generated table stops; regen refuses until trusted, writes it, refuses bad output",
         generated_stops, "merge-generated"),
        ("git: regen also checks what the command wrote", generated_stops, "regen-unchecked"),
        ("git: a multi-line approval does not approve its lines one by one", trust_is_whole, "trust-lines"),
        ("git: preview reads each version byte for byte", preview_keeps_bytes, "show-text-mode"),
    ]


def self_test() -> int:
    global BREAK
    fails = 0

    def case(label: str, cond: bool) -> None:
        nonlocal fails
        print(("ok   " if cond else "FAIL ") + label)
        if not cond:
            fails += 1

    def safe(fn: Callable[[], bool]) -> bool:
        try:
            return bool(fn())
        except Exception as e:      # an arm that raises has failed; say why
            print(f"     ({type(e).__name__}: {e})")
            return False

    def under(brk: str, fn: Callable[[], bool]) -> bool:
        global BREAK
        BREAK = brk
        os.environ["KEYED_MERGE_SELFTEST_BREAK"] = brk
        old = sys.stdout
        try:
            with open(os.devnull, "w") as dn:
                sys.stdout = dn
                return safe(fn)
        finally:
            sys.stdout = old
            BREAK = ""
            os.environ.pop("KEYED_MERGE_SELFTEST_BREAK", None)

    BREAK = ""
    os.environ.pop("KEYED_MERGE_SELFTEST_BREAK", None)
    for label, arm, brk, fmts in FORMAT_ARMS:
        for fmt in fmts:
            case(f"{fmt:<11} {label}", safe(lambda: arm(fmt)))
        missed = [fmt for fmt in fmts if under(brk, lambda: arm(fmt))]
        case(f"red arm ({brk}): breaks `{label}` in every format" + (f" (not: {', '.join(missed)})" if missed else ""),
             not missed)
    for label, fn, brk in other_arms():
        case(label, safe(fn))
        if brk:
            case(f"red arm ({brk}): breaks `{label}`", not under(brk, fn))
    if shutil.which("git") is None:
        case("git is on the path, for the end-to-end arms", False)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            arms = git_arms(os.path.join(tmp, "green"))
            for label, fn, brk in arms:
                if brk != "regen-unchecked":
                    case(label, safe(fn))
            for label, fn, brk in git_arms(os.path.join(tmp, "red")):
                if brk:
                    case(f"red arm ({brk}): breaks `{label}`", not under(brk, fn))
    print(f"{PROG} self-test: {'PASS' if fails == 0 else f'FAIL -- {fails} case(s)'}")
    return 0 if fails == 0 else 1


# --- main ------------------------------------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["--self-test"]:
        return self_test()
    if "--check" in args[:1]:
        args[0] = "check"
    p = argparse.ArgumentParser(prog=PROG, description="Merge shared tables by key, not by line.")
    p.add_argument("--json", action="store_true", help="the same report as JSON, for agents")
    p.add_argument("--root", help="the repository top (default: found from the current directory)")
    sub = p.add_subparsers(dest="cmd")

    def fmt_opts(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--format", choices=FORMATS, help="override the config: the file's format")
        sp.add_argument("--key", help="override the config: key fields, comma-separated")
        sp.add_argument("--order", choices=ORDERS, help="override the config: row order")

    d = sub.add_parser("driver", help="git calls this (see install)")
    for n in ("base", "ours", "theirs", "path"):
        d.add_argument(n)
    d.add_argument("--marker-size", type=int, default=7)
    fmt_opts(d)
    m = sub.add_parser("merge", help="merge three versions of one table")
    for n in ("base", "ours", "theirs"):
        m.add_argument(n)
    m.add_argument("--path", required=True, help="the table's path in the repository, to find its config line")
    m.add_argument("-o", "--output", help="write the merge here (default: stdout)")
    fmt_opts(m)
    pv = sub.add_parser("preview", help="merge the tables two revisions both changed, writing nothing")
    pv.add_argument("rev_a")
    pv.add_argument("rev_b")
    pv.add_argument("paths", nargs="*")
    c = sub.add_parser("check", help="each configured table parses, has no duplicate key, is canonical and ordered")
    c.add_argument("paths", nargs="*")
    rg = sub.add_parser("regen", help="run the approved regen= commands for unmerged (or named) generated tables")
    rg.add_argument("paths", nargs="*")
    ins = sub.add_parser("install", help="copy the driver into this clone's git folder and register it")
    ins.add_argument("--trust-commands", action="store_true", help="also approve every regen= command now")
    tr = sub.add_parser("trust", help="list, or approve, the regen= commands this clone may run")
    tr.add_argument("--all", action="store_true", help="approve every regen= command in the config")
    tr.add_argument("command", nargs="?", help="approve this exact command")
    li = sub.add_parser("lint", help="flag bare counts that a keyed list would replace")
    li.add_argument("--fail", action="store_true", help="exit 1 on a finding")
    li.add_argument("paths", nargs="*")
    for sp in (d, m, pv, c, rg, ins, tr, li):
        sp.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        sp.add_argument("--root", default=argparse.SUPPRESS)
    a = p.parse_args(args)
    if not a.cmd:
        p.print_help()
        return CANNOT
    handlers = {"driver": cmd_driver, "merge": cmd_merge, "preview": cmd_preview, "check": cmd_check,
                "regen": cmd_regen, "install": cmd_install, "trust": cmd_trust, "lint": cmd_lint}
    code, msg = OK, ""
    try:
        return handlers[a.cmd](a)
    except BadInput as e:
        code, msg = BAD, str(e)
    except CannotRun as e:
        code, msg = CANNOT, str(e)
    if a.json:
        print(json.dumps({"verdict": VERDICT[code], "exit": code, "reason": msg}, indent=2, ensure_ascii=False))
    else:
        print(f"{PROG}: {VERDICT[code]}: {msg}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
