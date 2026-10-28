#!/usr/bin/env python3
"""adr-public: draft each ADR's public copy, keep it in step with the private ADR, and hold it until it is approved.

Usage: adr-public.py [--root DIR] [--dir PATH] COMMAND ...
       adr-public.py --self-test [--break pending|leak|stale|oneway|drivers]

Commands
    draft ADR...     Make the mechanical cuts of the private ADR into docs/decisions/public/pending/ (see "The
                     cuts"), mark every place the agent must rewrite prose with a REWRITE comment, record which
                     version of the private ADR the copy came from, and add a dated "Public copy ... drafted" line
                     to the private ADR's History. A superseded or withdrawn ADR gets no draft; its live copy, if
                     it has one, is queued for withdrawal instead.
    check [ADR...]   Report, never change: leaks (scripts/check-leaks.py, where present), the copy's shape, a
                     REWRITE marker left in a pending copy, a stale copy (the private ADR changed since the copy
                     was drafted), links that run the wrong way, and a publish.txt line that names a pending copy.
    list [--brief]   Every pending copy and queued withdrawal, with its diff against the live copy.
    approve ADR...   ONLY ON THE OWNER'S WORD FOR THAT COPY, never another agent's, and --word quotes it. Moves the
                     pending copy up into docs/decisions/public/, adds its publish.txt overlay line, appends the
                     dated "approved" line to the private ADR's History, and records the approval in the source
                     list. A queued withdrawal is approved the same way: the live copy moves to withdrawn/ and its
                     publish.txt line is commented out. It refuses a copy that `check` would fail.
    still-true ADR   ONLY ON THE OWNER'S WORD. The private ADR changed in a way that does not touch its public copy
                     (a typo): record the new version as the copy's source, with a History line, and no new draft.

The cuts (draft)
    Dropped: the Context (all but its Drivers list, which adr-check asks for wherever two or more options are
    weighed), Access and Open questions sections, the Owner and Consulted lines, and every Q-, A- and
    D-number (a parenthesis of only numbers goes; a number left in prose is marked). History becomes dates and one
    word ("2026-10-21: proposed."). "the owner" becomes "the user". The opening line goes under the title. Marked
    for the agent: a Q/A/D number left in prose, a private path, a PR number, an amendment section, and a header
    proof or owed that names a private path. The agent rewrites each marked line for a stranger, deletes the mark,
    and runs `check`; `approve` refuses a copy with a mark left.

Files
    docs/decisions/public/pending/ADR-NNN-*.md      the copies waiting for the release review; publish.txt never
                                                    names this folder and scripts/publish.py refuses it
    docs/decisions/public/pending/SOURCES.tsv       the source list: which version of each private ADR each copy
                                                    came from (see the README for why it lives there)
    docs/decisions/public/withdrawn/                where an approved withdrawal moves a live copy

What it promises
    - draft, approve and still-true change files; check and list never do. approve never runs without --word.
    - It never edits the ruled text of a private ADR: it appends History lines and touches nothing else there.
    - It never publishes, pushes, commits or contacts the network.
    - Exit 0: clean (or done). Exit 1: findings (or a refusal). Exit 2: a usage error or a file it cannot use.

--root DIR   The repository root. Default: the current directory.
--dir PATH   The private decisions folder, relative to the root. Default: docs/decisions.
--date D     draft, approve, still-true: the date written into History lines and the source list (YYYY-MM-DD).
             Default: today.

--self-test  Builds its own repository in a temporary folder and prints one ok or FAIL line per case. --break ARM
             switches one guard off, to watch its arm go red by name: pending (a publish.txt line that names a
             pending copy goes unreported), leak (the leak check is skipped), stale (staleness is not checked),
             oneway (a History line to a missing file, or a public copy naming a private path, goes unreported),
             drivers (draft drops a Context's Drivers list with the rest of Context).

Python 3.8 or newer, standard library only.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import difflib
import hashlib
import importlib.util
import io
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from typing import Dict, List, Optional, Sequence, Set, Tuple

PROG = "adr-public"
OPENING = "This is the published version of this decision. Some internal details have been left out."
OLD_OPENING = "This is the public copy of a decision I made. I've left out a few internal notes."
MARK = "REWRITE:"
SOURCES_NAME = "SOURCES.tsv"
SOURCES_HEADER = ("# adr-public source list: which version of each private ADR each public copy was drafted from.\n"
                  "# Never named in publish.txt; scripts/publish.py refuses everything in this folder.\n"
                  "# adr\tslot\tsource\tdrafted\tapproved\tword\n"
                  "#   slot: pending (waits for the release review), live (approved), withdraw (a queued withdrawal)\n"
                  "#   source: a hash of the private ADR without its History section\n")
REF = r"[QAD]\d{1,2}[a-z]?"
PRIVATE_PREFIXES = ("docs/bbq/", "docs/proposals/", "docs/audits/", "docs/field-reports/", "docs/STATUS", "drafts/",
                    ".claude/", "scripts/leak-allow-private", "docs/decisions/ADR-",
                    "docs/decisions/public/pending/", "docs/decisions/public/withdrawn/")
DROPPED_SECTIONS = ("context", "access", "open questions", "open question")
DRIVERS_LABEL = re.compile(r"^(?:\*\*)?Drivers(?::\*\*|\*\*:|:)\s*$")
GUARDS = {"pending": True, "leak": True, "stale": True, "oneway": True, "drivers": True}
ARMS = tuple(GUARDS)


class Fail(Exception):
    """A refusal or a file the tool cannot use; main prints it and exits with its code."""

    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------------------------------- layout
class Layout:
    def __init__(self, root: pathlib.Path, dec: str):
        self.root = root
        self.dec_rel = dec.strip("/")
        self.dec = root / self.dec_rel
        self.public = self.dec / "public"
        self.pending = self.public / "pending"
        self.withdrawn = self.public / "withdrawn"
        self.sources = self.pending / SOURCES_NAME
        self.publish_txt = root / "publish.txt"

    def rel(self, path: pathlib.Path) -> str:
        return path.relative_to(self.root).as_posix()


def adr_number(arg: str) -> int:
    m = re.fullmatch(r"(?:ADR-?)?0*(\d+)", arg.strip(), re.I)
    if not m:
        raise Fail(f"{PROG}: '{arg}' is not an ADR number (write 14, 014 or ADR-014)")
    return int(m.group(1))


def adr_files(folder: pathlib.Path, num: Optional[int] = None) -> Dict[int, List[pathlib.Path]]:
    out: Dict[int, List[pathlib.Path]] = {}
    if folder.is_dir():
        for p in sorted(folder.iterdir()):
            m = re.match(r"ADR-(\d+)-.*\.md$", p.name)
            if m and p.is_file() and (num is None or int(m.group(1)) == num):
                out.setdefault(int(m.group(1)), []).append(p)
    return out


def private_adr(lay: Layout, num: int) -> pathlib.Path:
    found = adr_files(lay.dec, num).get(num, [])
    if not found:
        raise Fail(f"{PROG}: no private ADR-{num:03d} in {lay.dec_rel}/")
    return found[0]


def today() -> str:
    return datetime.date.today().isoformat()


def valid_date(value: str) -> str:
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        raise Fail(f"{PROG}: --date '{value}' is not YYYY-MM-DD")
    return value


# ---------------------------------------------------------------------------------------------------- reading ADRs
def split_header(text: str) -> Tuple[List[str], List[str]]:
    """(header lines without the --- fences, the rest). No YAML header: ([], all lines)."""
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return lines[1:i], lines[i + 1:]
    return [], lines


def split_sections(lines: Sequence[str]) -> Tuple[List[str], List[Tuple[str, List[str]]]]:
    """(the lines before the first '## ' heading, [(heading text, body lines)]); fenced code is not scanned."""
    pre: List[str] = []
    secs: List[Tuple[str, List[str]]] = []
    fence = False
    for line in lines:
        if re.match(r"\s*(```|~~~)", line):
            fence = not fence
        m = None if fence else re.match(r"## (.*\S)\s*$", line)
        if m:
            secs.append((m.group(1), []))
        elif secs:
            secs[-1][1].append(line)
        else:
            pre.append(line)
    return pre, secs


def source_hash(text: str) -> str:
    """The version of a private ADR: everything but its History section, so a History line (a drafted or approved
    note, a 'still true') never makes a copy stale, and a ratification, an amendment or a rewrite does."""
    header, rest = split_header(text)
    pre, secs = split_sections(rest)
    kept = list(header) + pre
    for heading, body in secs:
        if heading.strip().lower() != "history":
            kept += ["## " + heading] + body
    norm = "\n".join(x.rstrip() for x in kept).strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:12]


def history_bullets(text: str) -> List[str]:
    """The History section's bullets, each joined into one line, oldest first."""
    _, rest = split_header(text)
    _, secs = split_sections(rest)
    out: List[str] = []
    for heading, body in secs:
        if heading.strip().lower() != "history":
            continue
        for line in body:
            if line.startswith("- "):
                out.append(line.strip())
            elif line.startswith((" ", "\t")) and out and line.strip():
                out[-1] += " " + line.strip()
    return out


def newest_public_copy_path(text: str) -> Optional[str]:
    """The path named by the newest History bullet that is a public-copy line with a docs/.../public/ path."""
    found = None
    for bullet in history_bullets(text):
        if re.search(r"public copy", bullet, re.I):
            m = re.search(r"`([^`\s]*/public/[^`\s]+\.md)`", bullet)
            if m:
                found = m.group(1)
    return found


def append_history(path: pathlib.Path, text: str) -> None:
    """Append one dated bullet at the end of the History section; nothing else in the file changes."""
    lines = path.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")
    start = None
    fence = False
    for i, line in enumerate(lines):
        if re.match(r"\s*(```|~~~)", line):
            fence = not fence
        if not fence and re.match(r"## History\s*$", line):
            start = i
    if start is None:
        raise Fail(f"{PROG}: {path.name} has no '## History' section to append to")
    end = len(lines)
    fence = False
    for i in range(start + 1, len(lines)):
        if re.match(r"\s*(```|~~~)", lines[i]):
            fence = not fence
        if not fence and lines[i].startswith("## "):
            end = i
            break
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    wrapped = textwrap.wrap(text, width=118, subsequent_indent="  ", break_long_words=False, break_on_hyphens=False)
    lines[end:end] = wrapped
    out = "\n".join(lines)
    path.write_text(out if out.endswith("\n") else out + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------------------------------- source list
class Row:
    def __init__(self, adr: int, slot: str, src: str, drafted: str, approved: str = "-", word: str = "-"):
        self.adr, self.slot, self.src, self.drafted, self.approved, self.word = adr, slot, src, drafted, approved, word

    def line(self) -> str:
        return "\t".join([f"ADR-{self.adr:03d}", self.slot, self.src, self.drafted, self.approved,
                          self.word.replace("\t", " ").replace("\n", " ")])


def load_sources(lay: Layout) -> List[Row]:
    rows: List[Row] = []
    if not lay.sources.is_file():
        return rows
    for n, raw in enumerate(lay.sources.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip() or raw.startswith("#"):
            continue
        parts = raw.split("\t")
        if len(parts) != 6 or not re.fullmatch(r"ADR-\d+", parts[0]) or parts[1] not in ("pending", "live", "withdraw"):
            raise Fail(f"{PROG}: {lay.rel(lay.sources)}:{n} is not a source-list row: '{raw}'")
        rows.append(Row(int(parts[0][4:]), parts[1], parts[2], parts[3], parts[4], parts[5]))
    return rows


def save_sources(lay: Layout, rows: List[Row]) -> None:
    lay.pending.mkdir(parents=True, exist_ok=True)
    rows = sorted(rows, key=lambda r: (r.adr, r.slot))
    lay.sources.write_text(SOURCES_HEADER + "".join(r.line() + "\n" for r in rows), encoding="utf-8")


def find_row(rows: List[Row], num: int, slot: str) -> Optional[Row]:
    return next((r for r in rows if r.adr == num and r.slot == slot), None)


# ---------------------------------------------------------------------------------------------------- the cuts
def unquote(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        return v[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if len(v) >= 2 and v[0] == "'" and v[-1] == "'":
        return v[1:-1].replace("''", "'")
    return v


YAML_TYPED = re.compile(r"^(?:\d{4}-\d{2}-\d{2}|[-+]?\d+(?:\.\d+)?|(?i:yes|no|true|false|on|off|null|~))$")


def yaml_scalar(value: str) -> str:
    """Quote a value YAML would otherwise read as something else: punctuation, or a date, number or yes/no."""
    if (value == "" or value[0] in "\"'[]{}&*!|>%@`#" or ": " in value or " #" in value or value != value.strip()
            or YAML_TYPED.match(value)):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def owner_to_user(s: str) -> str:
    return re.sub(r"\b([Tt])he owner\b", lambda m: m.group(1) + "he user", s)


REFTOK = re.compile(rf"^{REF}(?:\s*[–-]\s*{REF})?$|^(?:Defaulted|defaulted|P\d+)$")


def strip_ref_parens(line: str) -> str:
    """Drop a parenthesis made only of Q-, A- and D-numbers; strip the numbers from a mixed one."""
    def fix(m: "re.Match[str]") -> str:
        inner = m.group(1)
        if not re.search(rf"\b{REF}\b", inner):
            return m.group(0)
        toks = [t.strip() for t in re.split(r"\s*[,;]\s*|\s+and\s+", inner) if t.strip()]
        rest = [t for t in toks if not REFTOK.match(t)]
        return (" (" + ", ".join(rest) + ")") if rest else ""
    return re.sub(r"[ \t]*\(([^()\n]*(?:\n[^()\n]+)*)\)", fix, line)


def strip_refs_outside_fences(text: str) -> str:
    """strip_ref_parens over a block of lines (a parenthesis may wrap onto the next line), skipping fenced code."""
    parts = re.split(r"(^[ \t]*(?:```|~~~).*?^[ \t]*(?:```|~~~)[ \t]*$)", text, flags=re.M | re.S)
    return "".join(x if i % 2 else strip_ref_parens(x) for i, x in enumerate(parts))


def path_tokens(line: str) -> List[str]:
    toks = re.findall(r"`([^`]+)`", line) + re.findall(r"\]\(([^)\s]+)\)", line)
    return [t for t in toks if re.fullmatch(r"[A-Za-z0-9_.\-/]+\.[A-Za-z]{1,5}", t) and "/" in t
            and not t.startswith(("http", "#"))]


# The project root and the paths its publish.txt produces.
PubTree = Tuple[pathlib.Path, Set[str]]


def private_paths(line: str, tree: Optional[PubTree]) -> List[str]:
    """Paths in a line that a stranger could not open: a private folder, a private ADR, or (where publish.py can say)
    a file the repository has but publish.txt does not list."""
    out = []
    for tok in path_tokens(line):
        priv = tok.startswith(PRIVATE_PREFIXES) or bool(re.search(r"docs/decisions/(?!public/)ADR-\d+", tok))
        if priv or (tree is not None and tok not in tree[1] and (tree[0] / tok).exists()):
            out.append(tok)
    return out


def public_files(root: pathlib.Path) -> Optional[PubTree]:
    """The paths the project's publish.txt produces, from scripts/publish.py where it exists; None otherwise."""
    script = root / "scripts" / "publish.py"
    if not script.is_file() or not (root / "publish.txt").is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location("adr_public_publish", script)
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return root, set(mod.listed_files(root))
    except BaseException:  # a repository without git, or a publish.py of another shape: fall back to the prefixes
        return None


def history_word(rest: str) -> Optional[str]:
    low = rest.lower().lstrip(" —:-")
    if low.startswith(("public copy", "the public copy", "built line", "built updated")):
        return None
    words = {"proposed": "proposed", "drafted": "proposed", "accepted": "ratified by the owner",
             "ratified": "ratified by the owner", "superseded": "superseded", "amended": "amended",
             "amendment": "amended", "rejected": "rejected", "withdrawn": "withdrawn"}
    first = re.match(r"\w+", low)
    if first and first.group(0) in words:
        return words[first.group(0)]
    for key, word in (("ratified", "ratified by the owner"), ("superseded", "superseded"), ("amend", "amended"),
                      ("rejected", "rejected"), ("withdrawn", "withdrawn")):
        if key in low:
            return word
    return None


def public_history(secs: List[Tuple[str, List[str]]]) -> List[str]:
    bullets: List[str] = []
    for heading, body in secs:
        if heading.strip().lower() == "history":
            for line in body:
                if line.startswith("- "):
                    bullets.append(line.strip())
                elif line.startswith((" ", "\t")) and bullets and line.strip():
                    bullets[-1] += " " + line.strip()
    out: List[str] = []
    for b in bullets:
        m = re.match(r"- (\d{4}-\d{2}-\d{2})(.*)$", b)
        word = history_word(m.group(2)) if m else None
        if m and word and f"- {m.group(1)}: {word}." not in out:
            out.append(f"- {m.group(1)}: {word}.")
    return out


def cut_line(line: str, public: Optional[PubTree]) -> Tuple[str, List[str]]:
    reasons: List[str] = []
    new = owner_to_user(line)
    if re.search(rf"\b{REF}\b", new):
        reasons.append("a question number is left in the prose")
    if re.search(r"(?<![\w&/])#\d{1,5}\b", new):
        reasons.append("names a PR number of the private repository")
    paths = private_paths(new, public)
    if paths:
        reasons.append("names a path outside the public tree: " + ", ".join(paths))
    return new, reasons


def drivers_list(lines: List[str]) -> List[str]:
    """A Context section's Drivers list: its label line and the `- ` items under it, as written; [] if none.
    adr-check asks for this list wherever two or more options are weighed, so the copy keeps it (and only it)."""
    for i, line in enumerate(lines):
        if not DRIVERS_LABEL.match(line.strip()):
            continue
        out = [line]
        for later in lines[i + 1:]:
            if not later.strip():
                if len(out) > 1:
                    break
                continue
            if re.match(r"\s*- ", later) or (len(out) > 1 and later.startswith("  ")):
                out.append(later)
            else:
                break
        return out if len(out) > 1 else []
    return []


def make_copy(text: str, public: Optional[PubTree]) -> Tuple[str, List[str], List[str]]:
    """The mechanical cuts. Returns (the copy, the sections it dropped, the History bullets it dropped)."""
    header, rest = split_header(text)
    if not header:
        raise Fail(f"{PROG}: this ADR has no YAML header; convert it with adr-plus's adr-yaml.py --apply first")
    pre, secs = split_sections(rest)
    out: List[str] = ["---"]
    for line in header:
        m = re.match(r"([A-Za-z][\w-]*):\s*(.*)$", line)
        if not m:
            out.append(line)
            continue
        key, value = m.group(1), unquote(m.group(2))
        if key == "status-note":
            value = re.split(r"\s+\(", value, 1)[0].strip()
            out.append(f"{key}: {yaml_scalar(value)}")
            continue
        if key == "proof":
            value = re.sub(r",\s*#\d+|#\d+,\s*", "", value)
            value = re.sub(r"^#\d+$", "", value).strip()
        line_out = f"{key}: {yaml_scalar(value)}"
        paths = private_paths(value, public) if key in ("proof", "owed") else []
        if paths:
            line_out += f"  # {MARK} names a path outside the public tree: " + ", ".join(paths)
        out.append(line_out)
    out.append("---")
    title = next((i for i, x in enumerate(pre) if x.startswith("# ")), None)
    if title is None:
        raise Fail(f"{PROG}: this ADR has no '# ADR-NNN — title' line")
    out += [pre[title], "", OPENING, ""]
    extra = [x for x in pre[title + 1:] if not re.match(r"\*\*(Owner|Consulted|Status|Built):\*\*", x)]
    while extra and not extra[0].strip():
        extra.pop(0)
    notes_dropped: List[str] = []
    body: List[str] = []
    fence = False

    def cut(lines: List[str]) -> List[str]:
        nonlocal fence
        res = []
        for line in strip_refs_outside_fences("\n".join(lines)).split("\n"):
            if re.match(r"\s*(```|~~~)", line):
                fence = not fence
            if fence or re.match(r"\s*(```|~~~)", line):
                res.append(line)
                continue
            new, reasons = cut_line(line, public)
            res.append(new + (f" <!-- {MARK} {'; '.join(reasons)} -->" if reasons else ""))
        return res

    body += cut(extra)
    for heading, lines in secs:
        low = heading.strip().lower()
        kept = drivers_list(lines) if low == "context" and GUARDS["drivers"] else []
        if kept:
            notes_dropped.append(f"{heading} (all but its Drivers list)")
            body += ["## " + heading, ""] + cut(kept) + [""]
            continue
        if low in DROPPED_SECTIONS:
            notes_dropped.append(heading)
            continue
        if low == "history":
            body += ["## History", ""] + (public_history(secs) or [f"<!-- {MARK} the private ADR has no dated History -->"]) + [""]
            continue
        head = "## " + heading
        if low.startswith("amendment"):
            head += f" <!-- {MARK} amendment text is internal prose; rewrite it for a stranger -->"
        body += [head] + cut(lines)
    text_out = "\n".join(out + body)
    text_out = re.sub(r"\n{3,}", "\n\n", text_out).rstrip("\n") + "\n"
    dropped_hist = len([b for b in history_bullets(text) if b]) - len(public_history(secs))
    return text_out, notes_dropped, [f"{max(dropped_hist, 0)} History line(s) not carried (dates and one word only)"]


# ---------------------------------------------------------------------------------------------------- publish.txt
def publish_lines(lay: Layout) -> List[str]:
    return lay.publish_txt.read_text(encoding="utf-8").split("\n") if lay.publish_txt.is_file() else []


def write_publish_lines(lay: Layout, lines: List[str]) -> None:
    out = "\n".join(lines)
    lay.publish_txt.write_text(out if out.endswith("\n") else out + "\n", encoding="utf-8")


def overlay_line(lay: Layout, num: int, folder: str = "") -> str:
    return f"{lay.dec_rel}/public/{folder}ADR-{num:03d}-*.md -> {lay.dec_rel}/"


def add_publish_line(lay: Layout, num: int) -> str:
    """Make the copy's overlay line live: uncomment it if it is there commented, else insert it in number order."""
    lines = publish_lines(lay)
    live = overlay_line(lay, num)
    if live in [x.strip() for x in lines]:
        return "already live"
    for i, x in enumerate(lines):
        if x.strip() == "# " + live:
            lines[i] = live
            write_publish_lines(lay, lines)
            return "uncommented"
    pat = re.compile(rf"^{re.escape(lay.dec_rel)}/public/ADR-(\d+)-\*\.md\s*->")
    at = None
    for i, x in enumerate(lines):
        m = pat.match(x)
        if m and int(m.group(1)) < num:
            at = i + 1
    if at is None:
        at = next((i for i, x in enumerate(lines) if "public/INDEX.md" in x), None)
        if at is not None and at > 0 and lines[at - 1].startswith("#"):
            at -= 1
    if at is None:
        at = len(lines) - 1 if lines and lines[-1] == "" else len(lines)
    lines.insert(at, live)
    write_publish_lines(lay, lines)
    return "added"


def comment_out_publish_line(lay: Layout, num: int, why: str, date: str) -> bool:
    lines = publish_lines(lay)
    live = overlay_line(lay, num)
    for i, x in enumerate(lines):
        if x.strip() == live:
            lines[i:i + 1] = [f"# ADR-{num:03d} withdrawn {date}: {why}", "# " + overlay_line(lay, num, "withdrawn/")]
            write_publish_lines(lay, lines)
            return True
    return False


# ---------------------------------------------------------------------------------------------------- the checks
Finding = Tuple[str, str, str]  # (kind, file or ADR, message)


def glob_match(rel: str, pattern: str) -> bool:
    def seg(pat: List[str], segs: List[str]) -> bool:
        if not pat:
            return not segs
        if pat[0] == "**":
            return any(seg(pat[1:], segs[i:]) for i in range(len(segs) + 1))
        import fnmatch
        return bool(segs) and fnmatch.fnmatchcase(segs[0], pat[0]) and seg(pat[1:], segs[1:])
    segs = rel.split("/")
    pat = pattern.strip("/").split("/")
    return any(seg(pat, segs[:k]) for k in range(1, len(segs) + 1))


def check_pending_listed(lay: Layout) -> List[Finding]:
    """A publish.txt line must never name a pending copy (scripts/publish.py also refuses it)."""
    pend = [lay.rel(p) for p in sorted(lay.pending.rglob("*")) if p.is_file()] if lay.pending.is_dir() else []
    out: List[Finding] = []
    pend_rel = lay.rel(lay.pending) + "/"
    for raw in publish_lines(lay):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pattern = line.split("->")[0].strip()
        hit = [p for p in pend if glob_match(p, pattern)]
        if hit or pattern.startswith(pend_rel.rstrip("/")) or "/pending/" in pattern:
            out.append(("pending", "publish.txt", f"'{line}' names a pending copy"
                        + (f" ({hit[0]})" if hit else "") + "; pending copies are never published"))
    return out


def copy_shape(lay: Layout, path: pathlib.Path, strict: bool) -> List[Finding]:
    text = path.read_text(encoding="utf-8")
    rel = lay.rel(path)
    out: List[Finding] = []

    def add(msg: str) -> None:
        out.append(("shape", rel, msg))
    header, rest = split_header(text)
    keys = {m.group(1) for x in header for m in [re.match(r"([A-Za-z][\w-]*):", x)] if m}
    for key in ("status", "owner", "kind", "built", "supersedes", "amends"):
        if key not in keys:
            add(f"the header has no '{key}'")
    m = re.match(r"ADR-(\d+)-", path.name)
    num = int(m.group(1)) if m else -1
    title = next((x for x in rest if x.startswith("# ")), "")
    if not re.match(rf"# ADR-0*{num}\b", title):
        add(f"the title line does not name ADR-{num:03d}: '{title}'")
    pre, secs = split_sections(rest)
    heads = [h.strip().lower() for h, _ in secs]
    if "decision" not in heads:
        add("no '## Decision' section")
    if "history" not in heads:
        add("no '## History' section")
    if not strict:
        return out
    ti = rest.index(title) if title in rest else 0
    after = [x for x in rest[ti + 1:] if x.strip()]
    if not after or after[0].strip() != OPENING:
        add(f"the line under the title is not: {OPENING}")
    for heading, lines in secs:
        h = heading.strip().lower()
        kept = drivers_list(lines) if h == "context" else []
        if kept and [x for x in lines if x.strip()] == [x for x in kept if x.strip()]:
            continue
        if h in DROPPED_SECTIONS:
            more = " with more than its Drivers list" if kept else ""
            add(f"has a '## {h.title()}' section{more}, which a public copy leaves out")
    body = "\n".join(rest)
    for label in ("Owner", "Consulted"):
        if f"**{label}:**" in body:
            add(f"has a '**{label}:**' line, which a public copy leaves out")
    fence = False
    in_history = False
    for n, line in enumerate(rest, 1):
        if re.match(r"\s*(```|~~~)", line):
            fence = not fence
        if fence:
            continue
        if line.startswith("## "):
            in_history = line.strip().lower() == "## history"
        if re.search(rf"\b{REF}\b", line):
            add(f"a question number is left in the text: '{line.strip()[:70]}'")
        if not in_history and re.search(r"\b[Tt]he owner\b", line):
            add(f"'the owner' is left in the text (it reads 'the user'): '{line.strip()[:70]}'")
    for h, bodyl in secs:
        if h.strip().lower() == "history":
            for line in bodyl:
                if line.strip() and not re.fullmatch(r"- \d{4}-\d{2}-\d{2}: [a-z][a-z ,'-]*\.", line):
                    add(f"History must be dates and a word only; found '{line.strip()[:70]}'")
    return out


def check_markers(lay: Layout, path: pathlib.Path) -> List[Finding]:
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
        if MARK in line:
            out.append(("marker", lay.rel(path), f"line {n} still has a REWRITE mark: {line.strip()[:90]}"))
    return out


def check_copy_links(lay: Layout, path: pathlib.Path, public: Optional[PubTree]) -> List[Finding]:
    """No public copy names its private ADR or a path outside the public tree. Links run one way."""
    out: List[Finding] = []
    rel = lay.rel(path)
    fence = False
    for n, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
        if re.match(r"\s*(```|~~~)", line):
            fence = not fence
        if fence:
            continue
        m = re.search(rf"{re.escape(lay.dec_rel)}/(?!public/)ADR-\d+[^\s`)\]]*", line)
        if m:
            out.append(("oneway", rel, f"line {n} names a private ADR: {m.group(0)}"))
        for tok in private_paths(line, public):
            if not (m and tok == m.group(0)):
                out.append(("oneway", rel, f"line {n} names a path outside the public tree: {tok}"))
    return out


def check_history_links(lay: Layout, only: Optional[Set[int]]) -> List[Finding]:
    """Each private ADR's newest public-copy History line must name a file that exists."""
    out: List[Finding] = []
    for num, files in adr_files(lay.dec).items():
        if only is not None and num not in only:
            continue
        newest = newest_public_copy_path(files[0].read_text(encoding="utf-8"))
        if newest and not (lay.root / newest).is_file():
            out.append(("oneway", lay.rel(files[0]),
                        f"its newest public-copy History line names {newest}, which does not exist"))
    return out


def run_leaks(lay: Layout, files: List[pathlib.Path]) -> List[Finding]:
    script = lay.root / "scripts" / "check-leaks.py"
    if not files:
        return []
    if not script.is_file():
        return [("note", "scripts/check-leaks.py", "not found, so the leak check was skipped")]
    r = subprocess.run([sys.executable, "scripts/check-leaks.py", *[lay.rel(p) for p in files]], cwd=lay.root,
                       capture_output=True, text=True)
    if r.returncode == 0:
        return []
    lines = (r.stdout + r.stderr).strip().splitlines() or [f"exited {r.returncode}"]
    return [("leak", "scripts/check-leaks.py", x) for x in lines]


def stale_rows(lay: Layout, rows: List[Row], only: Optional[Set[int]]) -> List[Finding]:
    out: List[Finding] = []
    for r in rows:
        if r.slot == "withdraw" or (only is not None and r.adr not in only):
            continue
        has_pending = find_row(rows, r.adr, "pending") is not None
        if r.slot == "live" and has_pending:
            continue  # the pending revision is the update; the live copy keeps publishing as approved
        files = adr_files(lay.dec, r.adr).get(r.adr, [])
        if not files:
            out.append(("source", f"ADR-{r.adr:03d}", "the source list names an ADR with no private file"))
            continue
        now = source_hash(files[0].read_text(encoding="utf-8"))
        if now != r.src:
            where = "pending" if r.slot == "pending" else "live"
            out.append(("stale", f"ADR-{r.adr:03d}", f"the {where} copy was drafted from version {r.src}; the private "
                        f"ADR is now {now}. Re-draft it, or record `still-true` if the change does not touch the copy"))
    return out


def run_checks(lay: Layout, only: Optional[Set[int]] = None) -> List[Finding]:
    rows = load_sources(lay)
    public = public_files(lay.root)
    findings: List[Finding] = []
    pend = {n: v for n, v in adr_files(lay.pending).items() if only is None or n in only}
    live = {n: v for n, v in adr_files(lay.public).items() if only is None or n in only}
    for num, files in sorted(pend.items()):
        for path in files:
            findings += copy_shape(lay, path, strict=True) + check_markers(lay, path)
            if GUARDS["oneway"]:
                findings += check_copy_links(lay, path, public)
            if find_row(rows, num, "pending") is None:
                findings.append(("source", lay.rel(path), "no source recorded: draft it with `adr-public draft`"))
            if not adr_files(lay.dec, num).get(num):
                findings.append(("source", lay.rel(path), "there is no private ADR with this number"))
    for num, files in sorted(live.items()):
        for path in files:
            findings += copy_shape(lay, path, strict=False)
            if GUARDS["oneway"]:
                findings += check_copy_links(lay, path, public)
    for r in rows:
        if r.slot == "pending" and not pend.get(r.adr) and (only is None or r.adr in only):
            findings.append(("source", f"ADR-{r.adr:03d}", "the source list has a pending row but no pending copy"))
    if GUARDS["stale"]:
        findings += stale_rows(lay, rows, only)
    if GUARDS["oneway"]:
        findings += check_history_links(lay, only)
    if GUARDS["pending"]:
        findings += check_pending_listed(lay)
    if GUARDS["leak"]:
        findings += run_leaks(lay, [p for fs in list(pend.values()) + list(live.values()) for p in fs])
    return findings


def print_findings(findings: List[Finding]) -> int:
    bad = [f for f in findings if f[0] != "note"]
    for kind, where, msg in findings:
        print(f"{'note' if kind == 'note' else 'FAIL'} {kind} {where}: {msg}")
    return 1 if bad else 0


# ---------------------------------------------------------------------------------------------------- commands
def discard_pending(lay: Layout, rows: List[Row], num: int) -> List[Row]:
    """A superseded or withdrawn ADR gets no public copy of its own: any pending draft goes, with its source row."""
    for path in adr_files(lay.pending, num).get(num, []):
        path.unlink()
        print(f"{PROG}: the pending copy {lay.rel(path)} is discarded (a withdrawn ADR gets no new copy)")
    return [r for r in rows if not (r.adr == num and r.slot == "pending")]


def cmd_draft(lay: Layout, nums: List[int], date: str, force: bool) -> int:
    rows = load_sources(lay)
    public = public_files(lay.root)
    for num in nums:
        src = private_adr(lay, num)
        text = src.read_text(encoding="utf-8")
        header, _ = split_header(text)
        status = next((unquote(m.group(1)) for x in header for m in [re.match(r"status:\s*(.*)$", x)] if m), "")
        live = adr_files(lay.public, num).get(num, [])
        supersedes = next((unquote(m.group(1)) for x in header for m in [re.match(r"supersedes:\s*(.*)$", x)] if m), "")
        for old in sorted({int(n) for n in re.findall(r"ADR-0*(\d+)", supersedes)}):
            if adr_files(lay.public, old).get(old) and not find_row(rows, old, "withdraw"):
                rows.append(Row(old, "withdraw", "-", date))
                rows = discard_pending(lay, rows, old)
                oldp = private_adr(lay, old)
                append_history(oldp, f"- {date} — Public copy `{lay.rel(adr_files(lay.public, old)[old][0])}` queued "
                               f"for withdrawal: superseded by ADR-{num:03d}; it waits for the release review (ADR-011).")
                print(f"{PROG}: ADR-{old:03d} is superseded by ADR-{num:03d}; its live copy is queued for withdrawal")
        if status in ("superseded", "withdrawn", "rejected"):
            if live and not find_row(rows, num, "withdraw"):
                rows.append(Row(num, "withdraw", "-", date))
                append_history(src, f"- {date} — Public copy `{lay.rel(live[0])}` queued for withdrawal: "
                               f"the ADR is {status}; it waits for the release review (ADR-011).")
                print(f"{PROG}: ADR-{num:03d} is {status}; its live copy is queued for withdrawal, and no draft is made")
            else:
                print(f"{PROG}: ADR-{num:03d} is {status}; no draft is made")
            rows = discard_pending(lay, rows, num)
            continue
        now = source_hash(text)
        row = find_row(rows, num, "pending")
        dest = lay.pending / src.name
        if row and dest.is_file() and row.src == now and not force:
            print(f"{PROG}: ADR-{num:03d}: the pending copy already comes from this version; nothing to do "
                  "(--force makes the cuts again and loses any rewriting)")
            continue
        if row and dest.is_file() and row.src != now:
            print(f"{PROG}: ADR-{num:03d}: the pending copy was stale; it is replaced (any rewriting of it is lost; "
                  "git has the old one)")
        copy, dropped, notes = make_copy(text, public)
        lay.pending.mkdir(parents=True, exist_ok=True)
        dest.write_text(copy, encoding="utf-8")
        rows = [r for r in rows if not (r.adr == num and r.slot == "pending")] + [Row(num, "pending", now, date)]
        save_sources(lay, rows)
        pending_path = lay.rel(dest)
        newest = newest_public_copy_path(text)
        if newest != pending_path:
            append_history(src, f"- {date} — Public copy `{pending_path}` drafted; it waits for the release review "
                           "and its `publish.txt` line is not live (ADR-011).")
        marks = [(n, x) for n, x in enumerate(copy.split("\n"), 1) if MARK in x]
        print(f"{PROG}: ADR-{num:03d}: drafted {pending_path}")
        print(f"       dropped sections: {', '.join(dropped) or 'none'}; {notes[0]}")
        if live:
            print(f"       a live copy exists ({lay.rel(live[0])}) and keeps publishing; `list` shows the diff")
        print(f"       {len(marks)} place(s) to rewrite for a stranger, then run `check`:")
        for n, x in marks:
            print(f"         line {n}: {x.strip()[:100]}")
    save_sources(lay, rows)
    return 0


def cmd_check(lay: Layout, nums: List[int]) -> int:
    only = set(nums) or None
    findings = run_checks(lay, only)
    rc = print_findings(findings)
    if rc == 0:
        print(f"{PROG}: check clean")
    else:
        print(f"{PROG}: check found {len([f for f in findings if f[0] != 'note'])} problem(s)")
    return rc


def cmd_list(lay: Layout, brief: bool) -> int:
    rows = load_sources(lay)
    pend = adr_files(lay.pending)
    stale = {f[1] for f in stale_rows(lay, rows, None)}
    shown = 0
    for num, files in sorted(pend.items()):
        for path in files:
            shown += 1
            row = find_row(rows, num, "pending")
            marks = len(check_markers(lay, path))
            state = "STALE" if f"ADR-{num:03d}" in stale else "current"
            print(f"ADR-{num:03d}  {path.name}")
            print(f"    drafted {row.drafted if row else '?'}; source {state}; {marks} REWRITE mark(s) left")
            live = adr_files(lay.public, num).get(num, [])
            if not live:
                print("    no live copy: this would be a new public copy")
                continue
            a = live[0].read_text(encoding="utf-8").splitlines()
            b = path.read_text(encoding="utf-8").splitlines()
            diff = list(difflib.unified_diff(a, b, "live " + live[0].name, "pending " + path.name, lineterm="", n=1))
            adds = sum(1 for x in diff if x.startswith("+") and not x.startswith("+++"))
            dels = sum(1 for x in diff if x.startswith("-") and not x.startswith("---"))
            print(f"    against the live copy: +{adds} -{dels} line(s)" + ("" if diff else " (identical)"))
            if not brief:
                print("\n".join("    " + x for x in diff))
    for r in rows:
        if r.slot == "withdraw":
            shown += 1
            live = adr_files(lay.public, r.adr).get(r.adr, [])
            print(f"ADR-{r.adr:03d}  WITHDRAWAL queued {r.drafted}: live copy "
                  f"{live[0].name if live else '(already gone)'} would move to withdrawn/")
    tracked = {r.adr for r in rows if r.slot in ("live", "pending")}
    untracked = sorted(n for n in adr_files(lay.public) if n not in tracked)
    if untracked:
        print("live copies with no source recorded (drafted before the source list; not checked for staleness): "
              + ", ".join(f"{n:03d}" for n in untracked))
    if not shown:
        print("nothing pending")
    return 0


def cmd_approve(lay: Layout, nums: List[int], word: str, by: str, date: str, index: bool = True) -> int:
    if not word.strip():
        raise Fail(f"{PROG}: approve needs --word, the owner's own words approving the copy")
    rows = load_sources(lay)
    plan: List[Tuple[int, str]] = []
    problems: List[Finding] = []
    for num in nums:
        pend = adr_files(lay.pending, num).get(num, [])
        if pend and find_row(rows, num, "pending"):
            plan.append((num, "copy"))
            problems += [f for f in run_checks(lay, {num}) if f[0] != "note"]
        elif find_row(rows, num, "withdraw"):
            plan.append((num, "withdrawal"))
        else:
            raise Fail(f"{PROG}: ADR-{num:03d} has nothing pending to approve")
    if problems:
        print_findings(problems)
        print(f"{PROG}: refused: fix the above, run `check`, and approve again")
        return 1
    for num, kind in plan:
        src = private_adr(lay, num)
        if kind == "copy":
            pend = adr_files(lay.pending, num)[num][0]
            for old in adr_files(lay.public, num).get(num, []):
                old.unlink()
            lay.public.mkdir(parents=True, exist_ok=True)
            dest = lay.public / pend.name
            os.replace(str(pend), str(dest))
            how = add_publish_line(lay, num)
            row = find_row(rows, num, "pending")
            rows = [r for r in rows if r.adr != num or r.slot == "withdraw"]
            rows.append(Row(num, "live", row.src if row else "-", row.drafted if row else date, date, word))
            append_history(src, f"- {date} — Public copy `{lay.rel(dest)}` approved by {by} in chat (\"{word}\"); its "
                           "`publish.txt` line is live (ADR-011).")
            print(f"{PROG}: ADR-{num:03d}: approved; moved to {lay.rel(dest)}; publish.txt line {how}")
        else:
            live = adr_files(lay.public, num).get(num, [])
            lay.withdrawn.mkdir(parents=True, exist_ok=True)
            dest = lay.withdrawn / (live[0].name if live else src.name)
            if live:
                os.replace(str(live[0]), str(dest))
            comment_out_publish_line(lay, num, "withdrawn on the owner's approval", date)
            rows = [r for r in rows if r.adr != num]
            append_history(src, f"- {date} — Public copy `{lay.rel(dest)}` withdrawn by {by} in chat (\"{word}\"); its "
                           "`publish.txt` line is commented out (ADR-011).")
            print(f"{PROG}: ADR-{num:03d}: withdrawal approved; moved to {lay.rel(dest)}; publish.txt line commented out")
    save_sources(lay, rows)
    idx = lay.root / "skills" / "makeadr" / "scripts" / "adr-index.py"
    if index and idx.is_file():
        subprocess.run([sys.executable, str(idx), "--root", str(lay.root), "--dir", lay.rel(lay.public)], check=False)
        print(f"{PROG}: the public INDEX.md was rewritten with adr-index")
    else:
        print(f"{PROG}: now rewrite the public INDEX.md: adr-index.py --root . --dir {lay.rel(lay.public)}")
    return 0


def cmd_still_true(lay: Layout, num: int, word: str, by: str, date: str) -> int:
    if not word.strip():
        raise Fail(f"{PROG}: still-true needs --word, the owner's own words")
    rows = load_sources(lay)
    row = find_row(rows, num, "pending") or find_row(rows, num, "live")
    if row is None:
        raise Fail(f"{PROG}: ADR-{num:03d} has no recorded source to clear")
    src = private_adr(lay, num)
    row.src = source_hash(src.read_text(encoding="utf-8"))
    copies = adr_files(lay.pending if row.slot == "pending" else lay.public, num).get(num, [])
    if not copies:
        raise Fail(f"{PROG}: ADR-{num:03d} has no {row.slot} copy file")
    save_sources(lay, rows)
    append_history(src, f"- {date} — Public copy `{lay.rel(copies[0])}` checked against the changed ADR: still true, "
                   f"no new draft, by {by} in chat (\"{word}\").")
    print(f"{PROG}: ADR-{num:03d}: the {row.slot} copy is recorded as still true of the current version")
    return 0


# ---------------------------------------------------------------------------------------------------- self-test
PRIVATE_FIXTURE = '''---
status: accepted
status-note: "— ratified by the owner 2026-10-01 (in chat: \\"Ratify\\")"
owner: the owner
kind: process
built: landed
proof: "`skills/demo/SKILL.md`, #12"
supersedes: nothing
amends: nothing
---
# ADR-001 — demo: a demo decision

**Owner:** the owner
**Consulted:** this session (a model)

## Decision

The owner rules a thing (Q1, Q2). Details are in `docs/bbq/2026-10-01-demo.md` and PR #12.

## Context

Private context only the owner needs.

Drivers:
- speed (Q2)
- one source

## The rules

1. **Rule** (Q3, Defaulted): do it. Q4 chose this.

## Access

Private logistics.

## Open questions

- one

## History

- 2026-10-01 — PROPOSED from a bbq.
- 2026-10-01 — ACCEPTED: the owner ratified it in chat.
- 2026-10-02 — Built line updated.
'''
FIXTURE_LEAK_SCRIPT = ('import sys\nneedle = "/Us" + "ers/"\nbad = 0\nfor p in sys.argv[1:]:\n'
                       '    for n, line in enumerate(open(p, encoding="utf-8"), 1):\n'
                       '        if needle in line:\n            print(f"{p}:{n}: home path"); bad = 1\nsys.exit(bad)\n')


def make_fixture(base: pathlib.Path, name: str, with_publish: bool = False) -> Layout:
    root = base / name
    (root / "docs/decisions/public").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "docs/decisions/ADR-001-demo.md").write_text(PRIVATE_FIXTURE, encoding="utf-8")
    (root / "scripts/check-leaks.py").write_text(FIXTURE_LEAK_SCRIPT, encoding="utf-8")
    (root / "publish.txt").write_text("# a demo list\nREADME.md\ndocs/decisions/public/INDEX.md -> docs/decisions/\n",
                                      encoding="utf-8")
    (root / "README.md").write_text("A demo.\n", encoding="utf-8")
    beside = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "publish.py"
    if with_publish and beside.is_file():  # a lone copy of this folder (the shed's CI) has no publish.py beside it
        shutil.copy2(beside, root / "scripts" / "publish.py")
    return Layout(root, "docs/decisions")


def clean_copy(lay: Layout) -> pathlib.Path:
    """What an agent's rewrite of the fixture's draft would leave: the marks gone and the prose made plain."""
    path = lay.pending / "ADR-001-demo.md"
    text = path.read_text(encoding="utf-8")
    text = re.sub(r" <!-- REWRITE:.*?-->", "", text)
    text = re.sub(r"  # REWRITE:.*", "", text)
    text = text.replace(" Details are in `docs/bbq/2026-10-01-demo.md` and PR #12.", "").replace(" Q4 chose this.", "")
    path.write_text(text, encoding="utf-8")
    return path


def self_test(broken: Optional[str]) -> int:
    if broken is not None and broken not in ARMS:
        print(f"{PROG}: --break takes one of {', '.join(ARMS)}", file=sys.stderr)
        return 2
    if broken:
        GUARDS[broken] = False
    failed: List[str] = []

    def expect(label: str, ok: bool) -> None:
        print(("ok   " if ok else "FAIL ") + label)
        if not ok:
            failed.append(label)

    def arm(label: str, check: str, findings: List[Finding], must_name: str = "") -> None:
        """A red arm passes only when a finding of that kind came back and named must_name."""
        if any(f[0] == check and must_name in (f[1] + f[2]) for f in findings):
            print(f"ok   red arm {label}: the {check} check failed it, as it should")
        else:
            print(f"FAIL red arm {label}: the {check} check let it through")
            failed.append(label)

    def has(findings: List[Finding], kind: str) -> bool:
        return any(f[0] == kind for f in findings)

    def quiet(fn):
        def run(*a, **k):
            with contextlib.redirect_stdout(io.StringIO()):
                return fn(*a, **k)
        return run
    q_cmd_draft, q_cmd_approve, q_cmd_list, q_cmd_still_true = (quiet(f) for f in (
        cmd_draft, cmd_approve, cmd_list, cmd_still_true))
    q_print_findings = print_findings

    saved_env = os.environ.get("PITMASTER_DENYLIST")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            os.environ["PITMASTER_DENYLIST"] = str(base / "no-denylist.txt")
            D = "2026-10-23"

            # Green: the mechanical cuts.
            lay = make_fixture(base, "cuts")
            q_cmd_draft(lay, [1], D, False)
            pend = lay.pending / "ADR-001-demo.md"
            text = pend.read_text(encoding="utf-8")
            expect("green: draft writes the copy into pending/ under the private file's name", pend.is_file())
            expect("green: the opening line sits under the title",
                   re.search(r"\n# ADR-001 — [^\n]*\n\n" + re.escape(OPENING) + r"\n", text) is not None)
            expect("green: Access and Open questions are dropped, and Context but for its Drivers list",
                   not re.search(r"^## (Access|Open questions)", text, re.M) and "Private context" not in text)
            expect("(drivers) the Context's Drivers list is kept, its numbers cut, for adr-check's trigger rule",
                   "\n## Context\n\nDrivers:\n- speed\n- one source\n" in text)
            expect("green: a date, number or yes/no in the header keeps its quotes",
                   yaml_scalar("2026-10-23") == '"2026-10-23"' and yaml_scalar("12") == '"12"'
                   and yaml_scalar("yes") == '"yes"' and yaml_scalar("the owner") == "the owner")
            expect("green: the Owner and Consulted lines are dropped", "**Owner:**" not in text and "Consulted" not in text)
            expect("green: a parenthesis of only numbers goes and a mixed number is stripped",
                   "(Q1, Q2)" not in text and "(Q3, Defaulted)" not in text and "**Rule**: do it." in text)
            body_text = text.split("\n## History")[0].split("\n---\n", 1)[1]
            expect("green: in body prose 'the owner' becomes 'the user'; the header and History keep 'the owner'",
                   "The user rules a thing." in text and "the owner" not in body_text)
            expect("green: History is dates and one word",
                   "- 2026-10-01: proposed.\n- 2026-10-01: ratified by the owner.\n" in text and "Built line" not in text)
            expect("green: the header keeps its fields, drops the PR number and the chat quote",
                   "status-note: — ratified by the owner 2026-10-01\n" in text and "owner: the owner\n" in text and 'proof: "`skills/demo/SKILL.md`"' in text)
            marks = [x for x in text.split("\n") if MARK in x]
            expect("green: a number left in prose, a private path and a PR number are marked for the agent",
                   len(marks) == 2 and any("question number" in x for x in marks) and any("docs/bbq/" in x for x in marks))
            expect("green: the private ADR gains a 'drafted' History line and nothing else changes",
                   "Public copy `docs/decisions/public/pending/ADR-001-demo.md` drafted" in
                   (lay.dec / "ADR-001-demo.md").read_text(encoding="utf-8")
                   and (lay.dec / "ADR-001-demo.md").read_text(encoding="utf-8").startswith(PRIVATE_FIXTURE.rstrip("\n")))
            expect("green: a second draft of the same version changes nothing",
                   (q_cmd_draft(lay, [1], D, False), pend.read_text(encoding="utf-8"))[1] == text)
            expect("green: a draft with marks left fails the check by name", has(run_checks(lay), "marker"))

            # Green: a rewritten copy checks clean; a History line never makes it stale.
            clean_copy(lay)
            found = [f for f in run_checks(lay) if f[0] != "note"]
            expect("green: a rewritten copy checks clean", not found)
            if found:
                q_print_findings(found)
            ctx = pend.read_text(encoding="utf-8")
            pend.write_text(ctx.replace("Drivers:\n", "Private context.\n\nDrivers:\n"), encoding="utf-8")
            expect("green: a Context with more than its Drivers list fails the check by name",
                   any("more than its Drivers list" in f[2] for f in run_checks(lay)))
            pend.write_text(ctx, encoding="utf-8")
            private = lay.dec / "ADR-001-demo.md"
            append_history(private, "- 2026-10-05 — a note that changes History only.")
            expect("green: a History line on the private ADR does not make the copy stale",
                   not has(run_checks(lay), "stale"))

            # (pending) a publish.txt line that names a pending copy is reported by name.
            lay_p = make_fixture(base, "pending-arm", with_publish=True)
            q_cmd_draft(lay_p, [1], D, False)
            clean_copy(lay_p)
            (lay_p.publish_txt).write_text(lay_p.publish_txt.read_text() + overlay_line(lay_p, 1, "pending/") + "\n")
            arm("(pending) a publish.txt line that names a pending copy", "pending", run_checks(lay_p), "ADR-001")
            plan = public_files_for_test(lay_p)
            if plan is None:
                print("skip scripts/publish.py is not beside this tool, so its refusal of pending/ is not run here")
            else:
                expect("green: scripts/publish.py leaves a listed pending copy out of the tree",
                       not any("pending" in p or p.endswith("ADR-001-demo.md") for p in plan))

            # (leak) a home path in a pending copy.
            lay_l = make_fixture(base, "leak-arm")
            q_cmd_draft(lay_l, [1], D, False)
            path = clean_copy(lay_l)
            path.write_text(path.read_text() + "\nNotes live in /Us" + "ers/someone/notes.\n", encoding="utf-8")
            arm("(leak) a home path in a pending copy", "leak", run_checks(lay_l), "home path")

            # (stale) the private ADR changes after the draft.
            lay_s = make_fixture(base, "stale-arm")
            q_cmd_draft(lay_s, [1], D, False)
            clean_copy(lay_s)
            p = lay_s.dec / "ADR-001-demo.md"
            p.write_text(p.read_text().replace("The owner rules a thing", "The owner rules another thing"), encoding="utf-8")
            arm("(stale) the private ADR changed after its copy was drafted", "stale", run_checks(lay_s), "ADR-001")
            if broken is None:
                q_cmd_still_true(lay_s, 1, "still true", "the owner", D)
                expect("green: still-true clears the flag with a History line",
                       not has(run_checks(lay_s), "stale") and "still true" in p.read_text())

            # (oneway) a History line to a file that is not there; a public copy naming its private ADR.
            lay_o = make_fixture(base, "oneway-arm")
            q_cmd_draft(lay_o, [1], D, False)
            path = clean_copy(lay_o)
            private = lay_o.dec / "ADR-001-demo.md"
            append_history(private, "- 2026-10-06 — Public copy `docs/decisions/public/ADR-001-gone.md` approved by the owner.")
            arm("(oneway) a History line that names a missing file", "oneway", run_checks(lay_o), "does not exist")
            lay_o2 = make_fixture(base, "oneway-arm2")
            q_cmd_draft(lay_o2, [1], D, False)
            path = clean_copy(lay_o2)
            path.write_text(path.read_text() + "\nSee `docs/decisions/ADR-001-demo.md` for the private record.\n", encoding="utf-8")
            arm("(oneway) a public copy that names its private ADR", "oneway", run_checks(lay_o2), "private ADR")

            # Green: approve, list, and a withdrawal.
            lay_a = make_fixture(base, "approve")
            q_cmd_draft(lay_a, [1], D, False)
            word = "Approve as drafted"
            refused = q_cmd_approve(lay_a, [1], word, "the owner", D, index=False) == 1
            expect("green: approve refuses a copy with a REWRITE mark left", refused and
                   (lay_a.pending / "ADR-001-demo.md").is_file() and not adr_files(lay_a.public))
            clean_copy(lay_a)
            q_cmd_list(lay_a, True)
            expect("green: approve without --word is refused",
                   raises_fail(lambda: q_cmd_approve(lay_a, [1], "", "the owner", D, index=False)))
            rc = q_cmd_approve(lay_a, [1], word, "the owner", D, index=False)
            live = lay_a.public / "ADR-001-demo.md"
            expect("green: approve moves the copy up and leaves pending/ without it",
                   rc == 0 and live.is_file() and not (lay_a.pending / "ADR-001-demo.md").exists())
            expect("green: approve adds the publish.txt overlay line",
                   "docs/decisions/public/ADR-001-*.md -> docs/decisions/" in lay_a.publish_txt.read_text().split("\n"))
            expect("green: approve appends the dated History line to the private ADR",
                   f'approved by the owner in chat ("{word}")' in " ".join(
                       history_bullets((lay_a.dec / "ADR-001-demo.md").read_text())))
            rows = load_sources(lay_a)
            expect("green: the approval is recorded in the source list by date and word",
                   len(rows) == 1 and rows[0].slot == "live" and rows[0].approved == D and rows[0].word == word)
            expect("green: after approval the check is clean",
                   not [f for f in run_checks(lay_a) if f[0] != "note"])
            # An amendment re-drafts beside the live copy; the live copy keeps publishing.
            pa = lay_a.dec / "ADR-001-demo.md"
            pa.write_text(pa.read_text() + "\n## Amendment — 2026-10-07, the owner\n\nA narrower scope.\n", encoding="utf-8")
            if broken != "stale":
                expect("green: an amendment makes the live copy stale", has(run_checks(lay_a), "stale"))
            q_cmd_draft(lay_a, [1], "2026-10-07", False)
            expect("green: the re-draft waits in pending/ and the live copy stays",
                   (lay_a.pending / "ADR-001-demo.md").is_file() and live.is_file())
            # A supersede queues the old copy's withdrawal; approving it moves the copy to withdrawn/.
            (lay_a.dec / "ADR-002-next.md").write_text(PRIVATE_FIXTURE.replace("ADR-001", "ADR-002").replace(
                "supersedes: nothing", "supersedes: ADR-001"), encoding="utf-8")
            q_cmd_draft(lay_a, [2], "2026-10-08", False)
            expect("green: a supersede queues the old copy's withdrawal",
                   find_row(load_sources(lay_a), 1, "withdraw") is not None)
            rc = q_cmd_approve(lay_a, [1], "Withdraw it", "the owner", "2026-10-09", index=False)
            expect("green: an approved withdrawal moves the copy to withdrawn/ and comments out its line",
                   rc == 0 and (lay_a.withdrawn / "ADR-001-demo.md").is_file() and not live.exists()
                   and "# docs/decisions/public/withdrawn/ADR-001-*.md -> docs/decisions/" in lay_a.publish_txt.read_text())
    finally:
        for k in GUARDS:
            GUARDS[k] = True
        if saved_env is None:
            os.environ.pop("PITMASTER_DENYLIST", None)
        else:
            os.environ["PITMASTER_DENYLIST"] = saved_env
    if failed:
        print(f"{PROG}: self-test FAILED: {', '.join(failed)}")
        return 1
    print(f"{PROG}: self-test passed")
    return 0


def raises_fail(fn) -> bool:
    try:
        fn()
    except Fail:
        return True
    return False


def public_files_for_test(lay: Layout) -> Optional[Set[str]]:
    """The tree scripts/publish.py would build from the fixture, or None where publish.py is not beside this tool."""
    if not (lay.root / "scripts" / "publish.py").is_file():
        return None
    subprocess.run(["git", "-C", str(lay.root), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(lay.root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(lay.root), "-c", "user.name=t", "-c", "user.email=t@example.com",
                    "-c", "commit.gpgsign=false", "commit", "-q", "-m", "t"], check=True)
    tree = public_files(lay.root)
    return tree[1] if tree else None


# ---------------------------------------------------------------------------------------------------- main
def main(argv: List[str]) -> int:
    if "--self-test" in argv:
        rest = [a for a in argv if a != "--self-test"]
        if not rest:
            return self_test(None)
        if len(rest) == 2 and rest[0] == "--break":
            return self_test(rest[1])
        print(f"usage: {PROG}.py --self-test [--break {'|'.join(ARMS)}]", file=sys.stderr)
        return 2
    common = argparse.ArgumentParser(add_help=False)  # for the subcommands: a value given there wins, else the main one
    common.add_argument("--root", default=argparse.SUPPRESS)
    common.add_argument("--dir", default=argparse.SUPPRESS)
    ap = argparse.ArgumentParser(prog=PROG, formatter_class=argparse.RawDescriptionHelpFormatter,
                                 description="Draft each ADR's public copy, keep it in step, and hold it until approved.\n"
                                 "approve and still-true run ONLY on the owner's word for that copy, never another "
                                 "agent's.")
    ap.add_argument("--root", default=".")
    ap.add_argument("--dir", default="docs/decisions")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("draft", parents=[common], help="make the mechanical cuts into pending/")
    p.add_argument("adr", nargs="+")
    p.add_argument("--date", default=None)
    p.add_argument("--force", action="store_true", help="cut again even when the pending copy is current")
    p = sub.add_parser("check", parents=[common], help="leaks, shape, marks, staleness, one-way links")
    p.add_argument("adr", nargs="*")
    p = sub.add_parser("list", parents=[common], help="every pending copy with its diff against the live copy")
    p.add_argument("--brief", action="store_true", help="counts only, no diff")
    owner = ("ONLY ON THE OWNER'S WORD for that copy (never another agent's). --word quotes it; "
             "--by names who approved (default: the owner).")
    p = sub.add_parser("approve", parents=[common], help="move a pending copy up and list it. " + owner,
                       description=owner)
    p.add_argument("adr", nargs="+")
    p.add_argument("--word", required=True, help="the owner's words, e.g. \"Approve as drafted\"")
    p.add_argument("--by", default="the owner")
    p.add_argument("--date", default=None)
    p = sub.add_parser("still-true", parents=[common], help="clear a stale flag the owner calls harmless. " + owner,
                       description=owner)
    p.add_argument("adr")
    p.add_argument("--word", required=True)
    p.add_argument("--by", default="the owner")
    p.add_argument("--date", default=None)
    args = ap.parse_args(argv)
    root = pathlib.Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"{PROG}: {root} is not a folder", file=sys.stderr)
        return 2
    lay = Layout(root, args.dir)
    if not lay.dec.is_dir():
        print(f"{PROG}: no decisions folder {lay.dec_rel}/ in {root}", file=sys.stderr)
        return 2
    try:
        date = valid_date(args.date) if getattr(args, "date", None) else today()
        if args.cmd == "draft":
            return cmd_draft(lay, [adr_number(a) for a in args.adr], date, args.force)
        if args.cmd == "check":
            return cmd_check(lay, [adr_number(a) for a in args.adr])
        if args.cmd == "list":
            return cmd_list(lay, args.brief)
        if args.cmd == "approve":
            return cmd_approve(lay, [adr_number(a) for a in args.adr], args.word, args.by, date)
        return cmd_still_true(lay, adr_number(args.adr), args.word, args.by, date)
    except Fail as e:
        print(e, file=sys.stderr)
        return e.code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
