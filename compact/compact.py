#!/usr/bin/env python3
"""compact: move a block of text out of one file into an archive, verbatim, and prove it.

Usage:
  compact.py --source FILE --start LINE --end LINE --divider HEADING [--archive FILE] [--root DIR]
             [--bullet [--with FILE] [--drain]] [--expect-purity HEX] [--apply]
  compact.py --ordinal [--archive FILE] [--root DIR]
  compact.py --verify [BASE] --source FILE [--archive FILE] [--root DIR]
  compact.py --self-test

What it does
    Moves the lines from --start to --end (both inclusive, each named by its exact text) out of
    --source and appends them to --archive under --divider. It automates the mechanics only: what to
    move and when stays the caller's judgment. Without --apply nothing is written; the dry run prints
    the block, its purity digest and the ordinal reading.

What it promises
    - The archive is verbatim and append-only. No option deletes from it or rewrites a block in it.
    - The archive is written and proven before the source is touched. The block's purity digest
      (SHA-256 over its text, first 16 hex digits) is taken from the source, taken again from the
      archive as re-read from disk, and the source is edited only if the two agree. On a mismatch the
      archive is put back to the bytes it had before the append and the source is left as it was.
    - A refusal changes nothing. A prose stub is only ever written from a file the caller names
      (--with), never from the command line, and only with --bullet.
    - The ordinal of the divider is read from the archive and printed. It is never written into prose.
    - Each file keeps its own line endings (LF or CRLF); a file that mixes the two is refused.

The walls, in the order they fire (each is a refusal that names itself)
    1  Anchors are content, never line numbers; an anchor matching zero or several lines is refused.
    2  The block must be blank-line bounded (or, with --bullet, be exactly one top-level bullet: wall 8).
    6  --end is inclusive; an --end that opens a new entry means the block is one entry too long.
    5  A divider that carries the marker word (COMPACTION) must be readable by the ordinal pattern.
    9  --drain: the stub it leaves ends with `` body under `<divider>`. ``, the divider byte for byte.
    3  The archive is written and proven before the source is touched (above).
    4  The ordinal is read by the pattern and printed, not written.
    7  --source and --archive must be different files.
    8  --bullet: the block is one top-level bullet; nothing nested is cut off from it.

--verify [BASE]
    After the fact: proves from git that a compaction, or any number of them, moved text and changed
    nothing else. It compares --source and --archive as they are now with BASE (a git ref; default: the
    merge base of HEAD with origin/main, else with main; neither available is exit 2) and checks that
    - the archive is its base text plus an appended tail, nothing earlier changed;
    - the lines the source lost are exactly the blocks of that tail, verbatim and in order;
    - every stub the source gained that cites a divider (``body under `<divider>`.``) cites a divider
      line the archive tail holds, byte for byte;
    - putting the lost lines back over the gained ones rebuilds the base source byte for byte.
    It catches what compact cannot guarantee itself: a hand edit made after compact ran.

Exit codes
    0  proven (and, with --apply, landed); with --verify, every check passed.
    1  refused: a wall fired and nothing was left changed; with --verify, a check failed.
    2  harness error: bad arguments, an unreadable file, git missing, no base to verify against.

Python 3.8 or newer, standard library only. --verify and some self-test cases need git.
"""
from __future__ import annotations

import argparse
import contextlib
import difflib
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

PROG = "compact"
DEFAULT_ARCHIVE = "docs/STATUS-archive.md"
# The heading pattern whose first group is the ordinal word (or number). The hyphen in the class is
# needed: a hyphenated ordinal such as TWENTY-FIRST would otherwise go unmatched.
DEFAULT_PATTERN = r"^#+ [⚑⬇] ([A-Z-]+) COMPACTION"
DEFAULT_MARKER = "COMPACTION"
ORDINAL_RE = re.compile(DEFAULT_PATTERN, re.MULTILINE)

# A top-level list item opener: `- ` or `* ` at column 0. An indented one is a nested item.
ENTRY_OPEN_RE = re.compile(r"^[-*] ")

# A word this table does not carry is reported as unknown, never guessed.
_UNITS = [
    "FIRST", "SECOND", "THIRD", "FOURTH", "FIFTH", "SIXTH", "SEVENTH",
    "EIGHTH", "NINTH", "TENTH", "ELEVENTH", "TWELFTH", "THIRTEENTH",
    "FOURTEENTH", "FIFTEENTH", "SIXTEENTH", "SEVENTEENTH", "EIGHTEENTH",
    "NINETEENTH",
]
_TENS_ORD = {
    20: "TWENTIETH", 30: "THIRTIETH", 40: "FORTIETH", 50: "FIFTIETH",
    60: "SIXTIETH", 70: "SEVENTIETH", 80: "EIGHTIETH", 90: "NINETIETH",
}
_TENS_CARD = {
    20: "TWENTY", 30: "THIRTY", 40: "FORTY", 50: "FIFTY",
    60: "SIXTY", 70: "SEVENTY", 80: "EIGHTY", 90: "NINETY",
}


class Refusal(Exception):
    """A wall fired (or a check failed). Exit 1, nothing left changed."""


class Harness(Exception):
    """The tool could not do its job: bad arguments, unreadable file, no git. Exit 2."""


# --------------------------------------------------------------------------
# Ordinals
# --------------------------------------------------------------------------

def ordinal_to_int(word: str) -> Optional[int]:
    """FIFTY-THIRD -> 53, 53 -> 53. None when the table does not carry it."""
    w = word.strip().upper()
    if w.isdigit() and w.isascii():
        return int(w)
    if w in _UNITS:
        return _UNITS.index(w) + 1
    for value, name in _TENS_ORD.items():
        if w == name:
            return value
    if "-" in w:
        tens, _, unit = w.partition("-")
        for value, name in _TENS_CARD.items():
            if tens == name and unit in _UNITS[:9]:
                return value + _UNITS.index(unit) + 1
    return None


def int_to_ordinal(n: int) -> Optional[str]:
    """53 -> FIFTY-THIRD. None rather than a guess."""
    if 1 <= n <= 19:
        return _UNITS[n - 1]
    if n in _TENS_ORD:
        return _TENS_ORD[n]
    if 20 < n < 100:
        tens, unit = (n // 10) * 10, n % 10
        if unit and tens in _TENS_CARD:
            return "%s-%s" % (_TENS_CARD[tens], _UNITS[unit - 1])
    return None


def _name_of(n: int, numeric: bool) -> Optional[str]:
    return str(n) if numeric else int_to_ordinal(n)


def read_ordinals(archive_text: str, pat: "re.Pattern[str]" = ORDINAL_RE) -> Tuple[List[str], Optional[int], Optional[str]]:
    """Wall 4: run the standing pattern. Returns (words found, highest number, next name)."""
    found = pat.findall(archive_text)
    found = [f if isinstance(f, str) else f[0] for f in found]
    numbered = [(ordinal_to_int(w), w) for w in found]
    known = [(n, w) for n, w in numbered if n is not None]
    if not known:
        return found, None, None
    highest, word = max(known, key=lambda t: t[0])
    return found, highest, _name_of(highest + 1, word.isdigit())


def compile_pattern(pattern: str) -> "re.Pattern[str]":
    """The ordinal pattern: a regular expression with exactly one group, the ordinal word or number."""
    try:
        pat = re.compile(pattern, re.MULTILINE)
    except re.error as exc:
        raise Harness("--ordinal-pattern is not a regular expression: %s" % exc)
    if pat.groups != 1:
        raise Harness("--ordinal-pattern must have exactly one capture group (the ordinal), it has %d" % pat.groups)
    return pat


def do_ordinal(archive: str, pat: "re.Pattern[str]" = ORDINAL_RE) -> int:
    """Print the highest ordinal in the archive and the next one to take."""
    text = read_text(archive)
    found, highest, nxt = read_ordinals(text, pat)
    unknown = [w for w in found if ordinal_to_int(w) is None]
    print("compact: %d divider heading(s) match the ordinal pattern" % len(found))
    # A count of dividers is not the ordinal: one compaction can have two dividers, and the first has
    # none. Both numbers are printed, labelled, so the two questions cannot be confused.
    if highest is None:
        print("compact: ORDINAL UNSETTLED -- no recognised ordinal in any heading")
        return 1
    numeric = any(w.isdigit() for w in found if ordinal_to_int(w) == highest)
    print("compact: highest ordinal present  = %s (%d)" % (_name_of(highest, numeric), highest))
    print("compact: next ordinal to take     = %s (%d)" % (nxt, highest + 1))
    if unknown:
        print("compact: UNRECOGNISED ordinal word(s), not counted: %s" % unknown)
        print("compact: an ordinal this tool cannot name is reported, never guessed.")
        return 1
    return 0


# --------------------------------------------------------------------------
# Files and text
# --------------------------------------------------------------------------

def purity(block: str) -> str:
    """SHA-256 over the block's text (no trailing newline), first 16 hex digits.

    Taken over the block, not the file, so the same block hashes the same in the source and in the
    archive, which is what lets "re-taken from the archive's own bytes, identical" be settled by a machine.
    """
    return hashlib.sha256(block.encode("utf-8")).hexdigest()[:16]


def read_text(path: str) -> str:
    """Read a file as UTF-8 with no newline translation."""
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise Harness("cannot read %s: %s" % (path, exc))


def write_text(path: str, text: str) -> None:
    """In-place truncating write with no newline translation.

    Not write-to-a-temp-then-rename: some mounts refuse the unlink a rename needs. What stands in for
    atomicity is wall 3: the archive is proven before the source is opened, and the bytes of whichever
    file is being touched are held in memory for the rollback.
    """
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def split_eol(raw: str, what: str) -> Tuple[str, str]:
    """(text with LF endings, the file's own ending). A file mixing CRLF and LF is refused."""
    crlf = raw.count("\r\n")
    lf = raw.count("\n") - crlf
    if crlf and lf:
        raise Refusal(
            "%s mixes CRLF and LF line endings.\n"
            "  compact: moving lines between files with mixed endings could not be verbatim.\n"
            "  compact: make the file's endings consistent first.\n"
            "  compact: nothing was written." % what
        )
    return (raw.replace("\r\n", "\n"), "\r\n") if crlf else (raw, "\n")


def to_eol(text: str, eol: str) -> str:
    return text.replace("\n", eol) if eol != "\n" else text


def lines_of(text: str) -> List[str]:
    """Lines of a text as a diff sees them: no empty last element for a final newline."""
    parts = text.split("\n")
    if parts and parts[-1] == "":
        parts.pop()
    return parts


# --------------------------------------------------------------------------
# The walls
# --------------------------------------------------------------------------

def locate(lines: List[str], anchor: str, what: str) -> int:
    """Wall 1: the index of the ONE line equal to `anchor`; zero or several is a refusal.

    Anchors name content because a line number goes stale the moment the file changes, and a tool that
    picks one of two identical lines is the decapitation failure with initiative.
    """
    hits = [i for i, ln in enumerate(lines) if ln == anchor]
    if not hits:
        raise Refusal(
            "%s anchor matched NO line: %r\n"
            "  compact: anchors are compared as whole lines, exactly, including leading spaces.\n"
            "  compact: nothing was written." % (what, anchor)
        )
    if len(hits) > 1:
        rows = ", ".join(str(h + 1) for h in hits)
        raise Refusal(
            "%s anchor matched %d lines (rows %s): %r\n"
            "  compact: an ambiguous anchor is refused, never resolved to the first hit.\n"
            "  compact: nothing was written." % (what, len(hits), rows, anchor)
        )
    return hits[0]


def check_bounded(lines: List[str], start: int, end: int) -> None:
    """Wall 2: a block is cut at a blank-line boundary or it is not cut.

    Cutting one line off leaves a bullet sliced mid-sentence in the source and an orphan in the archive.
    This is structural and independent of --expect-purity: a caller who computed no digest still gets it.
    """
    before_ok = start == 0 or lines[start - 1].strip() == ""
    after_ok = end == len(lines) - 1 or lines[end + 1].strip() == ""
    if before_ok and after_ok:
        return
    detail = []
    if not before_ok:
        detail.append("line %d above the block is not blank: %r" % (start, lines[start - 1]))
    if not after_ok:
        detail.append("line %d below the block is not blank: %r" % (end + 2, lines[end + 1]))
    raise Refusal(
        "block is NOT blank-line bounded -- this is the decapitation class.\n"
        + "".join("  compact: %s\n" % d for d in detail)
        + "  compact: a boundary drawn one line off leaves a bullet cut mid-sentence in the source "
        "and an orphan in the archive.\n"
        "  compact: nothing was written."
    )


def check_entry_boundary(lines: List[str], start: int, end: int) -> None:
    """Wall 6: --end is inclusive, and a block that ends on a new entry's opening line is one entry too long.

    The digest cannot catch this: it proves the block moved faithfully, never that it was the right block.
    A one-line block is exempt (it is its own entry), and the opening line must follow a blank to count.
    """
    if end == start:
        return
    if not ENTRY_OPEN_RE.match(lines[end]):
        return
    if not (end == 0 or lines[end - 1].strip() == ""):
        return
    raise Refusal(
        "--end anchor (row %d) OPENS A NEW ENTRY -- the block is one entry too long.\n"
        "  compact: %s\n"
        "  compact: `--end` is INCLUSIVE, not half-open: it names the LAST line to MOVE, never the first "
        "line to keep.\n"
        "  compact: aim --end at the last line of the entry you are moving. For a single-line entry that "
        "is the SAME line as --start.\n"
        "  compact: --expect-purity cannot catch this: it proves the block moved faithfully, never that "
        "the block was the right block.\n"
        "  compact: nothing was written." % (end + 1, lines[end].strip()[:120])
    )


def check_bullet(lines: List[str], start: int, end: int) -> None:
    """Wall 8: with --bullet the block is exactly one top-level bullet and nothing nested is cut off.

    Replaces wall 2 for the call. --start must open a top-level item, no other line of the block may
    open one, and the line after must be blank, a top-level item, a heading or the end of the file; an
    indented item there belongs to this bullet and a cut in front of it is a decapitation.
    """
    if not ENTRY_OPEN_RE.match(lines[start]):
        raise Refusal(
            "--bullet: --start (row %d) does not open a top-level list item.\n"
            "  compact: %s\n"
            "  compact: a bullet starts with `- ` or `* ` at column 0; an indented `- ` is a nested item "
            "and a mid-bullet line is a cut inside a bullet.\n"
            "  compact: nothing was written." % (start + 1, lines[start].strip()[:120])
        )
    openers = [i for i in range(start + 1, end + 1) if ENTRY_OPEN_RE.match(lines[i])]
    if openers:
        first = openers[0]
        raise Refusal(
            "--bullet: the block contains a SECOND top-level bullet at row %d -- it is more than one "
            "bullet.\n"
            "  compact: %s\n"
            "  compact: `--end` is INCLUSIVE; aim it at the last line of the bullet you are moving, never "
            "at the first line of the next one.\n"
            "  compact: nothing was written." % (first + 1, lines[first].strip()[:120])
        )
    if end + 1 < len(lines):
        nxt = lines[end + 1]
        ok = nxt.strip() == "" or ENTRY_OPEN_RE.match(nxt) is not None or nxt.startswith("#")
        if not ok:
            raise Refusal(
                "--bullet: the line after the block (row %d) is not blank, a top-level bullet or a "
                "heading.\n"
                "  compact: %r\n"
                "  compact: a nested item or a continuation there belongs to the bullet being moved; "
                "cutting in front of it is the decapitation class.\n"
                "  compact: nothing was written." % (end + 2, nxt.strip()[:120])
            )


def check_divider(divider: str, ordinal_re: "re.Pattern[str]" = ORDINAL_RE, marker: str = DEFAULT_MARKER) -> None:
    """Wall 5: a divider that carries the marker word must be one the ordinal pattern can read.

    A heading the pattern misses is invisible to the one method that settles the sequence, which is how
    an ordinal gets taken twice. Either spell an ordinal the pattern reads or drop the word; no override.
    """
    if "\n" in divider or "\r" in divider or not divider.strip():
        raise Refusal(
            "--divider must be one non-blank line.\n"
            "  compact: nothing was written."
        )
    if not marker or marker.upper() not in divider.upper():
        return
    if ordinal_re.match(divider.strip()):
        return
    raise Refusal(
        "divider says %s but the ordinal pattern cannot read it:\n"
        "  compact: %s\n"
        "  compact: the pattern is `%s` -- a heading it misses is invisible to the one method that "
        "settles the sequence, which is how an ordinal gets taken twice.\n"
        "  compact: either spell an ordinal it can read, or drop the word.\n"
        "  compact: nothing was written." % (marker, divider.strip()[:120], ordinal_re.pattern)
    )


def check_distinct(source: str, archive: str) -> None:
    """Wall 7: the same file as --source and --archive is refused, before a byte is read.

    The tool would append the block, prove it, then overwrite the append with the pre-move source,
    deleting the block and reporting green. Compared by resolved path and by inode, so a symlink or a
    `./` spelling cannot slip past.
    """
    same = os.path.realpath(source) == os.path.realpath(archive)
    if not same and os.path.exists(source) and os.path.exists(archive):
        same = os.path.samefile(source, archive)
    if same:
        raise Refusal(
            "--source and --archive are the SAME FILE (%s).\n"
            "  compact: this tool would append the block, prove it, and then overwrite the append with "
            "the pre-move source -- deleting the block and reporting green.\n"
            "  compact: a same-file move is a hand move with a purity check either side; this tool does "
            "not do it.\n"
            "  compact: nothing was read, nothing was written." % source
        )


def drain_citation(divider: str) -> str:
    """Wall 9: the fixed phrase --drain makes the stub end with, quoting the divider byte for byte.

    A quoted divider resolves without guessing. A stub's own nickname for what it replaced is a
    paraphrase, and paraphrased stubs were measured missing their heading about half the time.
    """
    return "body under `%s`." % divider


def drain_replacement(replacement: str, divider: str) -> str:
    """Return the --with text with the citation as its last words; refuse a citation left mid-prose."""
    cite = drain_citation(divider)
    body = replacement.rstrip()
    if body.endswith(cite):
        return body
    if cite in body:
        raise Refusal(
            "--drain: the --with text holds the citation, but not at its end.\n"
            "  compact: the stub must END with `%s`; the prose before it is yours, the last words are "
            "the citation's.\n"
            "  compact: nothing was read, nothing was written." % cite
        )
    return (body + " " + cite) if body else cite


# --------------------------------------------------------------------------
# compact
# --------------------------------------------------------------------------

def compact(
    source: str,
    archive: str,
    start_anchor: str,
    end_anchor: str,
    divider: str,
    expect: Optional[str],
    apply: bool,
    bullet: bool = False,
    with_file: Optional[str] = None,
    drain: bool = False,
    ordinal_re: "re.Pattern[str]" = ORDINAL_RE,
    marker: str = DEFAULT_MARKER,
) -> int:
    check_distinct(source, archive)
    if with_file is not None and not bullet:
        raise Refusal(
            "--with is a --bullet option: the default mode rewrites nothing.\n"
            "  compact: nothing was read, nothing was written."
        )
    # These fire before any wall that reads the block, so a --drain call aimed at the wrong shape is
    # refused for that reason and not shadowed by whichever wall the block itself would trip first.
    if drain and not bullet:
        raise Refusal(
            "--drain requires --bullet: a stub is always a single top-level bullet.\n"
            "  compact: nothing was read, nothing was written."
        )
    if drain and with_file is None:
        raise Refusal(
            "--drain requires --with FILE: the stub's prose is still the caller's to write; --drain only "
            "guarantees the citation ends it.\n"
            "  compact: nothing was read, nothing was written."
        )
    src_raw = read_text(source)
    arc_raw = read_text(archive)
    src_text, src_eol = split_eol(src_raw, source)
    arc_text, arc_eol = split_eol(arc_raw, archive)
    # Read before any wall, so an unreadable --with FILE is a harness exit, never a half-done move.
    # It is a file and not a command-line string because the replacement may contain quotes.
    replacement: Optional[str] = None
    if with_file is not None:
        replacement = read_text(with_file).replace("\r\n", "\n")
        if replacement.endswith("\n"):
            replacement = replacement[:-1]
    src_lines = src_text.split("\n")

    start = locate(src_lines, start_anchor, "--start")
    end = locate(src_lines, end_anchor, "--end")
    if end < start:
        raise Refusal(
            "--end anchor (row %d) is ABOVE --start (row %d).\n"
            "  compact: nothing was written." % (end + 1, start + 1)
        )
    if bullet:
        # Wall 8 replaces wall 2 here: adjacent bullets are never blank-line bounded, and wall 8 covers
        # wall 6's hazard on its own terms.
        check_bullet(src_lines, start, end)
    else:
        check_bounded(src_lines, start, end)
        # Wall 6 fires after wall 2 deliberately: a block can be blank-line bounded and still be one
        # entry too long, and the structural message must not shadow the inclusive-anchor one.
        check_entry_boundary(src_lines, start, end)
    check_divider(divider, ordinal_re, marker)

    if drain:
        assert replacement is not None  # the refusals above guarantee it
        replacement = drain_replacement(replacement, divider)
        print("compact: --drain          stub ends with: `%s`" % drain_citation(divider))

    block = "\n".join(src_lines[start:end + 1])
    before = purity(block)
    nlines = end - start + 1

    # A boundary drawn one line off changes the block and so changes this digest.
    if expect is not None and expect.lower() != before:
        raise Refusal(
            "purity MISMATCH against --expect-purity.\n"
            "  compact: expected %s\n"
            "  compact: block is  %s  (%d lines, rows %d-%d)\n"
            "  compact: the block this tool selected is not the block you hashed -- check the boundary "
            "before anything else.\n"
            "  compact: nothing was written." % (expect.lower(), before, nlines, start + 1, end + 1)
        )

    _, highest, nxt = read_ordinals(arc_text, ordinal_re)

    print("compact: source          %s" % source)
    print("compact: archive         %s" % archive)
    print("compact: block           rows %d-%d, %d lines" % (start + 1, end + 1, nlines))
    print("compact: purity BEFORE   %s" % before)
    print("compact: ordinal highest %s  ->  next %s" % (
        (_name_of(highest, nxt is not None and nxt.isdigit()) if highest is not None else "NONE"),
        nxt or "UNKNOWN"))
    if bullet:
        rep_n = replacement.count("\n") + 1 if replacement is not None else 0
        print("compact: --bullet        one top-level bullet; replacement %d line(s) from %s; net %+d"
              % (rep_n, with_file or "(none)", rep_n - nlines))

    if not apply:
        print("compact: DRY RUN -- nothing written.  Re-run with --apply.")
        print("compact: record lines for the stub paragraph (yours to place, never written by this tool):")
        print("    purity `%s`, %d lines, taken before the move and re-taken from the archive's own bytes after"
              % (before, nlines))
        return 0

    # Wall 3: the archive is written and proven before the source is touched.
    arc_hold = arc_raw  # the exact pre-write bytes, for the rollback
    if arc_text == "":
        sep = ""
    else:
        sep = "" if arc_text.endswith("\n\n") else ("\n" if arc_text.endswith("\n") else "\n\n")
    prefix = arc_text + sep + divider + "\n\n"
    write_text(archive, to_eol(prefix + block + "\n", arc_eol))

    # Read back from disk, not from what was just built: the comparison must be against bytes that
    # survived the write, not against a string compared with itself.
    reread, _ = split_eol(read_text(archive), archive)
    if not reread.startswith(prefix):
        write_text(archive, arc_hold)
        raise Refusal(
            "archive re-read does NOT contain the divider just written.\n"
            "  compact: archive rolled back to its pre-write bytes; source untouched.\n"
            "  compact: nothing was moved."
        )
    archived = reread[len(prefix):]
    if archived.endswith("\n"):
        archived = archived[:-1]
    after = purity(archived)
    if after != before:
        write_text(archive, arc_hold)
        raise Refusal(
            "purity MISMATCH across the archive write.\n"
            "  compact: before %s\n"
            "  compact: after  %s\n"
            "  compact: archive rolled back to its pre-write bytes; source untouched and byte-identical.\n"
            "  compact: nothing was moved." % (before, after)
        )
    print("compact: purity AFTER    %s  -- re-taken from the archive's own bytes, IDENTICAL" % after)

    # The source is edited only after the archive write is proven.
    middle = replacement.split("\n") if replacement is not None else []
    remaining = src_lines[:start] + middle + src_lines[end + 1:]
    # Removing a blank-line-bounded block leaves its bounding blank lines adjacent. Collapse that one
    # pair, and only when both sides are blank, so no other part of the file is reflowed.
    if not middle and 0 < start < len(remaining) \
            and remaining[start - 1].strip() == "" \
            and remaining[start].strip() == "":
        del remaining[start]
    write_text(source, to_eol("\n".join(remaining), src_eol))
    print("compact: LANDED -- %d lines moved verbatim, source now %d lines" % (nlines, len(remaining)))
    print("compact: the stub paragraph and the ordinal are YOURS to write; this tool prints them and "
          "never edits prose.")
    return 0


# --------------------------------------------------------------------------
# --verify
# --------------------------------------------------------------------------

Hunk = Tuple[int, int, int, int, List[str], List[str]]  # old_start, old_n, new_start, new_n, removed, added
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_CITE_OPEN = "body under `"


def _git(root: str, *args: str) -> "subprocess.CompletedProcess[bytes]":
    try:
        return subprocess.run(["git", "-C", root] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise Harness("--verify needs git and git was not found")


def resolve_base(root: str, given: str) -> Tuple[str, str, str]:
    """(label, commit, description) for the ref the verification compares against."""
    probe = _git(root, "rev-parse", "--is-inside-work-tree")
    if probe.returncode != 0:
        raise Harness("--verify needs a git work tree and %s is not inside one" % root)
    if given:
        r = _git(root, "rev-parse", "--verify", "-q", given + "^{commit}")
        if r.returncode != 0:
            raise Harness("--verify: BASE %r is not a git ref that names a commit" % given)
        sha = r.stdout.decode("utf-8", "replace").strip()
        return given, sha, "base %s (%s)" % (given, sha[:7])
    for cand in ("origin/main", "main"):
        if _git(root, "rev-parse", "--verify", "-q", cand + "^{commit}").returncode != 0:
            continue
        mb = _git(root, "merge-base", "HEAD", cand)
        if mb.returncode != 0:
            continue
        sha = mb.stdout.decode("utf-8", "replace").strip()
        return cand, sha, "base %s (merge base %s)" % (cand, sha[:7])
    raise Harness("--verify: no BASE given and neither origin/main nor main has a merge base with HEAD; "
                  "pass a ref: --verify BASE")


def _git_show(root: str, sha: str, path: str, label: str) -> str:
    rel = os.path.relpath(path, root).replace(os.sep, "/")
    r = _git(root, "show", "%s:./%s" % (sha, rel))
    if r.returncode != 0:
        raise Harness("%s is not tracked at %s (git show failed); both files must exist at the base" % (path, label))
    try:
        return r.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Harness("%s at %s is not UTF-8: %s" % (path, label, exc))


def _hunks(base_text: str, work_text: str) -> List[Hunk]:
    """Zero-context hunks from git's own diff of the two texts."""
    with tempfile.TemporaryDirectory() as td:
        b, w = os.path.join(td, "base"), os.path.join(td, "work")
        write_text(b, base_text)
        write_text(w, work_text)
        try:
            p = subprocess.run(
                ["git", "diff", "--no-index", "--no-color", "--no-ext-diff", "--unified=0", "--minimal",
                 "--", "base", "work"],
                cwd=td, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError:
            raise Harness("--verify needs git and git was not found")
    if p.returncode not in (0, 1):
        raise Harness("git diff failed: %s" % p.stderr.decode("utf-8", "replace").strip())
    out: List[Hunk] = []
    cur: Optional[Hunk] = None
    for ln in p.stdout.decode("utf-8", "replace").split("\n"):
        m = _HUNK_RE.match(ln)
        if m:
            cur = (int(m.group(1)), int(m.group(2) or 1) if m.group(2) != "0" else 0,
                   int(m.group(3)), int(m.group(4) or 1) if m.group(4) != "0" else 0, [], [])
            out.append(cur)
        elif cur is not None and ln.startswith("-"):
            cur[4].append(ln[1:])
        elif cur is not None and ln.startswith("+"):
            cur[5].append(ln[1:])
    return out


def _walk_tail(tail: List[str], rem: List[Optional[str]]) -> Optional[Tuple[List[Tuple[str, List[str]]], int]]:
    """Parse the archive tail as repeated (divider, blank, block) and match each block to the removed lines.

    `rem` holds the lines the source lost, with None between hunks. Blocks must match consecutive removed
    lines exactly; only blank lines (the blank a removal collapses) may sit between blocks. Returns the
    (divider, block) list and how many removed blank lines were skipped, or None if the tail does not fit.
    """
    nt, nr = len(tail), len(rem)
    dead = set()  # (ti, ri) positions already known to fail

    def go(ti: int, ri: int) -> Optional[Tuple[List[Tuple[str, List[str]]], int]]:
        while ti < nt and tail[ti].strip() == "":
            ti += 1
        skipped = 0
        while ri < nr and (rem[ri] is None or (rem[ri] or "").strip() == ""):
            if rem[ri] is not None:
                skipped += 1
            ri += 1
        if (ti, ri) in dead:
            return None
        if ti == nt:
            return ([], skipped) if ri == nr else _dead(dead, ti, ri)
        if ri == nr or ti + 2 >= nt or tail[ti + 1] != "":
            return _dead(dead, ti, ri)
        divider, bs = tail[ti], ti + 2
        longest = 0
        while (bs + longest < nt and ri + longest < nr and rem[ri + longest] is not None
               and tail[bs + longest] == rem[ri + longest]):
            longest += 1
        for n in range(longest, 0, -1):
            if tail[bs + n - 1].strip() == "":
                continue  # a block never ends on a blank line
            rest = go(bs + n, ri + n)
            if rest is not None:
                return [(divider, tail[bs:bs + n])] + rest[0], rest[1] + skipped
        return _dead(dead, ti, ri)

    return go(0, 0)


def _dead(dead: set, ti: int, ri: int) -> None:
    dead.add((ti, ri))
    return None


def _diagnose(removed: List[str], tail: List[str]) -> List[str]:
    """Name what is wrong when the lost lines and the archive tail do not fit: CHANGED, MISSING, ORDER."""
    rn = [x for x in removed if x.strip()]
    tn = [x for x in tail if x.strip()]
    lost, held = Counter(rn), Counter(tn)
    missing = lost - held
    extra = held - lost
    msgs: List[str] = []
    if missing:
        for line in sorted(missing.elements()):
            close = difflib.get_close_matches(line, list(extra.elements()), n=1, cutoff=0.6)
            if close:
                msgs.append("moved line CHANGED in the archive: the source lost %r, the archive tail holds %r"
                            % (line, close[0]))
                extra[close[0]] -= 1
                extra += Counter()
            else:
                msgs.append("moved line MISSING from the archive tail: the source lost %r and the tail does "
                            "not hold it" % line)
        return msgs
    spare = Counter(lost)
    kept = []
    for line in tn:
        if spare[line] > 0:
            spare[line] -= 1
            kept.append(line)
    if kept != rn:
        for i, (a, b) in enumerate(zip(kept, rn)):
            if a != b:
                msgs.append("moved lines OUT OF ORDER: the %d%s moved line is %r in the source but %r in the "
                            "archive tail (blocks must be moved top to bottom, or verified one run at a time)"
                            % (i + 1, _suffix(i + 1), b, a))
                return msgs
    extras = sorted(extra.elements())
    msgs.append("the archive tail is not repeated (divider, blank line, block) matching the lost lines; "
                "lines it holds that the source did not lose: %r" % extras[:5])
    return msgs


def _suffix(n: int) -> str:
    return "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def _stub_cited(line: str) -> Optional[str]:
    """The divider a stub line cites, or None if the line cites none."""
    i = line.find(_CITE_OPEN)
    if i < 0 or not line.endswith("`."):
        return None
    return line[i + len(_CITE_OPEN):-2]


def verify(source: str, archive: str, root: str, base: str = "") -> int:
    """--verify: prove from git that the changes since BASE only moved text, verbatim, into the archive."""
    check_distinct(source, archive)
    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old_limit, 20000))
    try:
        return _verify_impl(source, archive, root, base)
    finally:
        sys.setrecursionlimit(old_limit)


def _verify_impl(source: str, archive: str, root: str, base: str) -> int:
    label, sha, desc = resolve_base(root, base)
    for p in (source, archive):
        if not os.path.isfile(p):
            raise Harness("cannot read %s: no such file" % p)
    nl = lambda t: t.replace("\r\n", "\n")  # noqa: E731 -- a checkout may add CR; only the text is compared
    b_src = nl(_git_show(root, sha, source, desc))
    b_arc = nl(_git_show(root, sha, archive, desc))
    w_src = nl(read_text(source))
    w_arc = nl(read_text(archive))
    print("compact: verify %s; source %s, archive %s" % (desc, source, archive))
    problems: List[str] = []

    def fail(msg: str) -> None:
        problems.append(msg)
        print("compact: verify FAIL -- %s" % msg)

    # 1. The archive is its base text plus an appended tail.
    tail_text = ""
    if w_arc.startswith(b_arc):
        tail_text = w_arc[len(b_arc):]
    else:
        fail("archive REWRITTEN: it is not its base text plus an appended tail; something earlier in the "
             "archive changed or was removed (append-only is the archive's one rule)")
    tail = tail_text.split("\n") if tail_text else []

    # 2. What the source lost and gained, by git's own diff.
    hunks = _hunks(b_src, w_src)
    rem: List[Optional[str]] = []
    added: List[str] = []
    for h in hunks:
        old_start, _, new_start, _, gone, came = h
        if not any(x.strip() for x in gone):
            fail("hunk removed NOTHING: at source row %d it only added %d line(s) or removed blank lines, "
                 "with no block moved; that is an edit made by hand, not a compaction"
                 % (new_start, len(came)))
        rem.extend(gone)
        rem.append(None)
        added.extend(came)
    removed = [x for x in rem if x is not None]

    # 3. The lost lines are the archive tail's blocks, verbatim and in order.
    blocks: List[Tuple[str, List[str]]] = []
    if w_arc.startswith(b_arc):
        walked = _walk_tail(tail, rem)
        if walked is None:
            for m in _diagnose(removed, tail):
                fail(m)
        else:
            blocks, blanks = walked
            if blanks > len(blocks):
                fail("the source lost %d blank line(s) beyond the one each removal collapses" % (blanks - len(blocks)))

    # 4. Every stub that cites a divider cites one the tail holds, byte for byte.
    dividers = set(d for d, _ in blocks) if blocks else set(x for x in tail if x.strip())
    stubs = 0
    for line in added:
        cited = _stub_cited(line)
        if cited is None or not w_arc.startswith(b_arc):
            continue
        stubs += 1
        if cited not in dividers:
            fail("stub does not CITE a divider in the archive tail: it cites `%s`, which no divider line "
                 "the archive gained matches byte for byte; stub: %.80s" % (cited, line))

    # 5. Putting the lost lines back over the gained ones rebuilds the base byte for byte.
    rebuilt = lines_of(w_src)
    for old_start, old_n, new_start, new_n, gone, came in reversed(hunks):
        idx = new_start - 1 if new_n > 0 else new_start
        rebuilt[idx:idx + new_n] = gone
    if rebuilt != lines_of(b_src) or w_src.endswith("\n") != b_src.endswith("\n"):
        fail("putting the lost lines back does not rebuild the base source byte for byte")

    nmoved = sum(len(b) for _, b in blocks)
    if problems:
        print("compact: verify FAIL -- %d problem(s) against %s" % (len(problems), desc))
        return 1
    print("compact: verify PASS -- %s: %d line(s) in %d block(s) moved to the archive verbatim and in order, "
          "%d stub(s) cite their divider, the base rebuilds byte for byte"
          % (desc, nmoved, len(blocks), stubs))
    return 0


# --------------------------------------------------------------------------
# --self-test. Every fixture is built in a temporary directory.
# --------------------------------------------------------------------------

FIXTURE_SRC = """# Fixture

Intro line that must survive.

**Updated:** the block that moves.
It has a second line, and a third that ends mid-thought so that a boundary
drawn one line off is visibly a decapitation.

Trailing line that must survive.
"""

FIXTURE_ARC = """# Archive

## ⬇ FIRST COMPACTION

old content
"""

START = "**Updated:** the block that moves."
SECOND = "It has a second line, and a third that ends mid-thought so that a boundary"
END = "drawn one line off is visibly a decapitation."
DIV = "## ⬇ SECOND COMPACTION"

LEDGER = (
    "# Ledger\n"
    "\n"
    "- **PAID** the entry that is genuinely done and may be moved.\n"
    "\n"
    "- **OPEN** the still-open entry that must NOT move.\n"
    "\n"
    "Trailing line that must survive.\n"
)
PAID = "- **PAID** the entry that is genuinely done and may be moved."
OPEN = "- **OPEN** the still-open entry that must NOT move."

BULLETS = (
    "# Rules\n"
    "\n"
    "- first rule, kept.\n"
    "- **second rule** with a long story\n"
    "  that continues on an indented line\n"
    "  - and carries a nested item\n"
    "  before it ends here.\n"
    "- third rule, kept.\n"
    "\n"
    "Trailing line that must survive.\n"
)
B_START = "- **second rule** with a long story"
B_END = "  before it ends here."
B_WANT = "\n".join(BULLETS.split("\n")[3:7])
B_STUB = "- **second rule** -- the story is in the archive.\n"
B_DIV = "## ⬇ DISTILLATION — fixture"
DRAIN_DIV = "## ⬇ DEBT DRAIN — fixture — the second rule entry"

VER_SRC = (
    "# Ledger\n"
    "\n"
    "- **alpha** first entry, spread\n"
    "  over two lines.\n"
    "- **beta** second entry.\n"
    "- **gamma** third entry stays.\n"
    "\n"
    "Closing line that stays.\n"
)
VER_ARC = "# Archive\n\n## ⬇ FIRST COMPACTION\n\nold content\n"
VER_DIV1 = "## ⬇ DRAIN 1 -- alpha"
VER_DIV2 = "## ⬇ DRAIN 2 -- beta"


class Skip(Exception):
    """Raised by a case that cannot run here; counted and named in the verdict line."""


def _fx(td: Path, src_text: str = FIXTURE_SRC, arc_text: str = FIXTURE_ARC) -> Tuple[str, str]:
    src, arc = str(td / "SRC.md"), str(td / "ARC.md")
    write_text(src, src_text)
    write_text(arc, arc_text)
    return src, arc


def _run(**kw: object) -> Tuple[int, str]:
    """Call compact() capturing its output, mapping a Refusal to 1 and a Harness error to 2."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = compact(**kw)  # type: ignore[arg-type]
        return rc, buf.getvalue()
    except Refusal as exc:
        return 1, buf.getvalue() + str(exc)
    except Harness as exc:
        return 2, buf.getvalue() + str(exc)


def _call(src: str, arc: str, **kw: object) -> Tuple[int, str]:
    args: Dict[str, object] = dict(source=src, archive=arc, start_anchor=START, end_anchor=END,
                                   divider=DIV, expect=None, apply=True)
    args.update(kw)
    return _run(**args)


def _main(argv: Sequence[str]) -> Tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(list(argv))
    return rc, out.getvalue() + err.getvalue()


def _unchanged(src: str, arc: str, src_text: str = FIXTURE_SRC, arc_text: str = FIXTURE_ARC) -> bool:
    return read_text(src) == src_text and read_text(arc) == arc_text


# -- ported cases: the walls ------------------------------------------------

def c_green_round_trip(td: Path) -> bool:
    src, arc = _fx(td)
    want = "\n".join(FIXTURE_SRC.split("\n")[4:7])
    rc, out = _call(src, arc)
    arc_now, src_now = read_text(arc), read_text(src)
    return (rc == 0 and want in arc_now and "IDENTICAL" in out and START not in src_now
            and "Intro line that must survive." in src_now and "Trailing line that must survive." in src_now)


def c_red_corrupted_archive_byte(td: Path) -> bool:
    # RED ARM (wall 3): the archived copy differs by one byte when re-read, as a mount that mangled the
    # write would show it; the move must be refused, the archive rolled back and the source untouched.
    src, arc = _fx(td)
    real_read: Callable[[str], str] = globals()["read_text"]

    def corrupt(path: str, _real: Callable[[str], str] = real_read, _arc: str = arc) -> str:
        text = _real(path)
        return text.replace("the block that moves", "the block that m0ves") if path == _arc else text

    globals()["read_text"] = corrupt
    try:
        rc, out = _call(src, arc)
    finally:
        globals()["read_text"] = real_read
    return rc == 1 and "purity MISMATCH across the archive write" in out and _unchanged(src, arc)


def c_red_boundary_one_line_off(td: Path) -> bool:
    # RED ARM (wall 2): --end one line short leaves the block ending mid-bullet.
    src, arc = _fx(td)
    rc, out = _call(src, arc, end_anchor=SECOND)
    return rc == 1 and "decapitation class" in out and _unchanged(src, arc)


def c_red_expect_purity_mismatch(td: Path) -> bool:
    # RED ARM: the caller hashed one block and the anchors select another, with the boundary still blank-line bounded.
    src, arc = _fx(td)
    rc, out = _call(src, arc, expect="0000000000000000")
    return rc == 1 and "purity MISMATCH against --expect-purity" in out and _unchanged(src, arc)


def c_expect_purity_match_passes(td: Path) -> bool:
    src, arc = _fx(td)
    want = purity("\n".join(FIXTURE_SRC.split("\n")[4:7]))
    rc, _ = _call(src, arc, expect=want.upper())
    return rc == 0 and START not in read_text(src)


def c_green_path_really_writes(td: Path) -> bool:
    # The refusals all assert "nothing changed", which a tool that did nothing would satisfy too.
    src, arc = _fx(td)
    rc, _ = _call(src, arc)
    return rc == 0 and read_text(arc) != FIXTURE_ARC and read_text(src) != FIXTURE_SRC


def c_dry_run_writes_nothing(td: Path) -> bool:
    src, arc = _fx(td)
    rc, out = _call(src, arc, apply=False)
    return rc == 0 and "DRY RUN" in out and _unchanged(src, arc)


def c_red_ambiguous_anchor(td: Path) -> bool:
    # RED ARM (wall 1): an anchor matching two lines is refused, not resolved to the first.
    src, arc = _fx(td, FIXTURE_SRC + "\nduplicate\n\nduplicate\n")
    rc, out = _call(src, arc, start_anchor="duplicate", end_anchor="duplicate")
    return rc == 1 and "matched 2 lines" in out and read_text(arc) == FIXTURE_ARC


def c_red_zero_match_anchor(td: Path) -> bool:
    # RED ARM (wall 1): an anchor matching no line is refused, never the closest match.
    src, arc = _fx(td)
    rc, out = _call(src, arc, start_anchor="**Updated:** the block that moves")
    return rc == 1 and "matched NO line" in out and _unchanged(src, arc)


def c_red_end_above_start(td: Path) -> bool:
    src, arc = _fx(td)
    rc, out = _call(src, arc, start_anchor=END, end_anchor=START)
    return rc == 1 and "ABOVE" in out and _unchanged(src, arc)


def c_wall5_unreadable_divider_refused(td: Path) -> bool:
    # RED ARM (wall 5): a divider that says COMPACTION without a readable ordinal.
    src, arc = _fx(td)
    rc, out = _call(src, arc, divider="## ⬇ DEBT DRAIN — the COMPACTION-RITUAL entry")
    return rc == 1 and "cannot read it" in out and _unchanged(src, arc)


def c_wall5_readable_and_wordfree_pass(td: Path) -> bool:
    # The wall must let through what it should: a readable ordinal, and a heading without the word.
    ok = True
    for i, div in enumerate(("## ⬇ SEVENTIETH COMPACTION -- whatever follows", "## ⬇ DEBT DRAIN — fixture")):
        sub = td / ("a%d" % i)
        sub.mkdir()
        src, arc = _fx(sub)
        rc, _ = _call(src, arc, divider=div)
        ok = ok and rc == 0
    return ok


def c_wall5_marker_flag(td: Path) -> bool:
    # --ordinal-marker moves the wall to another word; an empty marker turns it off.
    pat = compile_pattern(r"^## Pass (\d+)")
    ok = True
    try:
        check_divider("## Pass number three", pat, "Pass")
        ok = False
    except Refusal:
        pass
    check_divider("## Pass 3", pat, "Pass")
    check_divider("## Pass number three", pat, "")
    return ok


def c_red_divider_must_be_one_line(td: Path) -> bool:
    src, arc = _fx(td)
    rc1, out1 = _call(src, arc, divider="## ⬇ SECOND COMPACTION\n\nextra")
    rc2, _ = _call(src, arc, divider="   ")
    return rc1 == 1 and rc2 == 1 and "one non-blank line" in out1 and _unchanged(src, arc)


def _ledger(td: Path) -> Tuple[str, str]:
    return _fx(td, LEDGER, FIXTURE_ARC)


def c_wall6_end_opens_new_entry_refused(td: Path) -> bool:
    # RED ARM (wall 6): --end aimed at the next entry on the half-open reading.
    src, arc = _ledger(td)
    rc, out = _call(src, arc, start_anchor=PAID, end_anchor=OPEN, divider="## ⬇ DEBT DRAIN — fixture")
    return (rc == 1 and "OPENS A NEW ENTRY" in out and "the still-open entry that must NOT move" in out
            and _unchanged(src, arc, LEDGER))


def c_wall6_single_line_entry_passes(td: Path) -> bool:
    src, arc = _ledger(td)
    rc, _ = _call(src, arc, start_anchor=PAID, end_anchor=PAID, divider="## ⬇ DEBT DRAIN — fixture")
    s = read_text(src)
    return (rc == 0 and PAID in read_text(arc) and PAID not in s and OPEN in s
            and "Trailing line that must survive." in s)


def c_wall6_indented_item_opens_nothing(td: Path) -> bool:
    try:
        check_entry_boundary(["a", "", "- top", "  - nested last line"], 2, 3)
    except Refusal:
        return False
    return True


def c_wall7_same_file_refused(td: Path) -> bool:
    # RED ARM (wall 7): one file as both --source and --archive.
    queue = "# Queue\n\n- **LIVE** the entry a hand move would move.\n\n## ⬇ ARCHIVE\n\n- older\n"
    q = str(td / "Q.md")
    write_text(q, queue)
    live = "- **LIVE** the entry a hand move would move."
    rc, out = _call(q, q, start_anchor=live, end_anchor=live)
    rc2, out2 = _call(q, os.path.join(str(td), ".", "Q.md"), start_anchor=live, end_anchor=live)
    return rc == 1 and "SAME FILE" in out and rc2 == 1 and "SAME FILE" in out2 and read_text(q) == queue


def c_wall7_symlink_spelling_refused(td: Path) -> bool:
    queue = "# Queue\n\n- **LIVE** the entry a hand move would move.\n\n## ⬇ ARCHIVE\n\n- older\n"
    q = str(td / "Q.md")
    write_text(q, queue)
    link = str(td / "Q-link.md")
    try:
        os.symlink(q, link)
    except (OSError, NotImplementedError, AttributeError):
        raise Skip("no symlinks here")
    live = "- **LIVE** the entry a hand move would move."
    rc, out = _call(q, link, start_anchor=live, end_anchor=live)
    return rc == 1 and "SAME FILE" in out and read_text(q) == queue


def _bullets(td: Path) -> Tuple[str, str, str]:
    src, arc = str(td / "R.md"), str(td / "A.md")
    stub = str(td / "with.txt")
    write_text(src, BULLETS)
    write_text(arc, FIXTURE_ARC)
    write_text(stub, B_STUB)
    return src, arc, stub


def c_wall8_default_mode_refuses_adjacent_bullets(td: Path) -> bool:
    src, arc, _ = _bullets(td)
    rc, out = _call(src, arc, start_anchor=B_START, end_anchor=B_END, divider=B_DIV)
    return rc == 1 and "decapitation class" in out and read_text(src) == BULLETS


def c_wall8_bullet_moves_one_verbatim(td: Path) -> bool:
    src, arc, stub = _bullets(td)
    rc, out = _call(src, arc, start_anchor=B_START, end_anchor=B_END, divider=B_DIV,
                    expect=purity(B_WANT), bullet=True, with_file=stub)
    s, a = read_text(src), read_text(arc)
    return (rc == 0 and "IDENTICAL" in out and B_WANT in a and "and carries a nested item" not in s
            and "- first rule, kept.\n- **second rule** -- the story is in the archive.\n- third rule, kept.\n" in s
            and "Trailing line that must survive." in s)


def c_wall8_red_next_bullet_end(td: Path) -> bool:
    src, arc, _ = _bullets(td)
    rc, out = _call(src, arc, start_anchor=B_START, end_anchor="- third rule, kept.", divider=B_DIV, bullet=True)
    return rc == 1 and "SECOND top-level bullet" in out and "third rule" in out and read_text(src) == BULLETS


def c_wall8_red_orphaned_nested_item(td: Path) -> bool:
    src, arc, _ = _bullets(td)
    rc, out = _call(src, arc, start_anchor=B_START, end_anchor="  that continues on an indented line",
                    divider=B_DIV, bullet=True)
    return (rc == 1 and "not blank, a top-level bullet or a heading" in out and read_text(src) == BULLETS
            and read_text(arc) == FIXTURE_ARC)


def c_wall8_red_mid_bullet_start(td: Path) -> bool:
    src, arc, _ = _bullets(td)
    rc, out = _call(src, arc, start_anchor="  that continues on an indented line", end_anchor=B_END,
                    divider=B_DIV, bullet=True)
    return rc == 1 and "does not open a top-level list item" in out and read_text(src) == BULLETS


def c_red_with_requires_bullet(td: Path) -> bool:
    src, arc, stub = _bullets(td)
    rc, out = _call(src, arc, start_anchor=B_START, end_anchor=B_END, divider=B_DIV, bullet=False, with_file=stub)
    return rc == 1 and "--with is a --bullet option" in out and read_text(src) == BULLETS


def c_missing_with_file_exits_2(td: Path) -> bool:
    src, arc, _ = _bullets(td)
    rc, out = _call(src, arc, start_anchor=B_START, end_anchor=B_END, divider=B_DIV, bullet=True,
                    with_file=str(td / "nope.txt"))
    return rc == 2 and "cannot read" in out and read_text(src) == BULLETS and read_text(arc) == FIXTURE_ARC


# -- ported cases: --drain, and the new rule that the stub ENDS with the citation ---

def c_drain_requires_bullet(td: Path) -> bool:
    src, arc, _ = _bullets(td)
    rc, out = _call(src, arc, start_anchor=B_START, end_anchor=B_END, divider=DRAIN_DIV, drain=True)
    return rc == 1 and "requires --bullet" in out and read_text(src) == BULLETS


def c_drain_requires_with(td: Path) -> bool:
    src, arc, _ = _bullets(td)
    rc, out = _call(src, arc, start_anchor=B_START, end_anchor=B_END, divider=DRAIN_DIV, bullet=True, drain=True)
    return rc == 1 and "requires --with FILE" in out and read_text(src) == BULLETS


def c_drain_stub_ends_with_citation(td: Path) -> bool:
    src, arc, stub = _bullets(td)
    rc, out = _call(src, arc, start_anchor=B_START, end_anchor=B_END, divider=DRAIN_DIV, bullet=True,
                    with_file=stub, drain=True)
    s = read_text(src)
    cite = drain_citation(DRAIN_DIV)
    stub_line = [x for x in s.split("\n") if x.startswith("- **second rule**")]
    return (rc == 0 and len(stub_line) == 1 and stub_line[0].endswith(" " + cite)
            and stub_line[0].startswith("- **second rule** -- the story is in the archive.")
            and cite == "body under `%s`." % DRAIN_DIV and s.count(cite) == 1)


def c_drain_citation_already_at_end_not_duplicated(td: Path) -> bool:
    src, arc, _ = _bullets(td)
    stub2 = str(td / "with2.txt")
    write_text(stub2, "- **second rule** -- the story is in the archive. %s\n" % drain_citation(DRAIN_DIV))
    rc, _ = _call(src, arc, start_anchor=B_START, end_anchor=B_END, divider=DRAIN_DIV, bullet=True,
                  with_file=stub2, drain=True)
    return rc == 0 and read_text(src).count(drain_citation(DRAIN_DIV)) == 1


def c_red_drain_citation_mid_prose_refused(td: Path) -> bool:
    # RED ARM (wall 9): the citation is present but not last; the stub must END with it.
    src, arc, _ = _bullets(td)
    stub3 = str(td / "with3.txt")
    write_text(stub3, "- **second rule** %s and then more prose.\n" % drain_citation(DRAIN_DIV))
    rc, out = _call(src, arc, start_anchor=B_START, end_anchor=B_END, divider=DRAIN_DIV, bullet=True,
                    with_file=stub3, drain=True)
    return rc == 1 and "not at its end" in out and read_text(src) == BULLETS and read_text(arc) == FIXTURE_ARC


def c_drain_divider_quoted_byte_for_byte(td: Path) -> bool:
    # A divider holding backticks, a dash and non-ASCII text is quoted exactly as given.
    div = "## ⬇ DRAIN — the `tools/x.py` entry, “quoted”"
    src, arc, stub = _bullets(td)
    rc, _ = _call(src, arc, start_anchor=B_START, end_anchor=B_END, divider=div, bullet=True, with_file=stub, drain=True)
    s = read_text(src)
    return rc == 0 and s.split("\n")[3].endswith("body under `%s`." % div) and ("\n" + div + "\n") in read_text(arc)


# -- ordinals ---------------------------------------------------------------

def c_ordinal_hyphens_highest_unknown(td: Path) -> bool:
    ok_h = ordinal_to_int("TWENTY-FIRST") == 21 and int_to_ordinal(70) == "SEVENTIETH"
    _, hi, nxt = read_ordinals("## ⬇ TWENTY-FIRST COMPACTION\n## ⬇ SECOND COMPACTION\n")
    ok_g = hi == 21 and nxt == "TWENTY-SECOND"
    ok_u = ordinal_to_int("HUNDREDTH") is None and int_to_ordinal(100) is None
    return ok_h and ok_g and ok_u


def c_ordinal_prints_highest_and_next(td: Path) -> bool:
    arc = str(td / "A.md")
    write_text(arc, "## ⬇ NINTH COMPACTION\n\nx\n\n## ⬇ TENTH COMPACTION\n\ny\n\n## ⬇ SECOND COMPACTION\n")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = do_ordinal(arc)
    out = buf.getvalue()
    return rc == 0 and "= TENTH (10)" in out and "= ELEVENTH (11)" in out and "3 divider heading(s)" in out


def c_ordinal_unknown_word_reported_not_guessed(td: Path) -> bool:
    arc = str(td / "A.md")
    write_text(arc, "## ⬇ SECOND COMPACTION\n## ⬇ HUNDREDTH COMPACTION\n")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = do_ordinal(arc)
    return rc == 1 and "UNRECOGNISED" in buf.getvalue() and "HUNDREDTH" in buf.getvalue()


def c_ordinal_none_found_unsettled(td: Path) -> bool:
    arc = str(td / "A.md")
    write_text(arc, "# Archive\n\nnothing here\n")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = do_ordinal(arc)
    return rc == 1 and "ORDINAL UNSETTLED" in buf.getvalue()


def c_ordinal_custom_pattern(td: Path) -> bool:
    # --ordinal-pattern: a number in the heading, and a spelled word in a different heading.
    arc = str(td / "A.md")
    write_text(arc, "## Pass 2 -- a\n\n## Pass 11 -- b\n\n## Pass 9\n")
    rc1, out1 = _main(["--ordinal", "--archive", arc, "--ordinal-pattern", r"^## Pass (\d+)"])
    write_text(arc, "# ROUND FOURTH\n# ROUND SEVENTH\n## ⬇ TENTH COMPACTION\n")
    rc2, out2 = _main(["--ordinal", "--archive", arc, "--ordinal-pattern", r"^# ROUND ([A-Z]+)"])
    rc3, out3 = _main(["--ordinal", "--archive", arc])
    return (rc1 == 0 and "= 11 (11)" in out1 and "= 12 (12)" in out1 and "3 divider heading(s)" in out1
            and rc2 == 0 and "= SEVENTH (7)" in out2 and "= EIGHTH (8)" in out2
            and rc3 == 0 and "= TENTH (10)" in out3)


def c_ordinal_custom_pattern_reaches_compact(td: Path) -> bool:
    # The pattern drives wall 5 and the ordinal reading of a real move, not only --ordinal.
    src, arc = _fx(td, FIXTURE_SRC, "# Archive\n\n## Pass 4\n\nold\n")
    args = ["--source", src, "--archive", arc, "--start", START, "--end", END, "--divider", "## Pass 5"]
    rc, out = _main(args + ["--ordinal-pattern", r"^## Pass (\d+)", "--ordinal-marker", "Pass"])
    rc_bad, out_bad = _main(["--source", src, "--archive", arc, "--start", START, "--end", END,
                             "--divider", "## Pass five", "--ordinal-pattern", r"^## Pass (\d+)",
                             "--ordinal-marker", "Pass"])
    return rc == 0 and "highest 4  ->  next 5" in out and rc_bad == 1 and "cannot read it" in out_bad


def c_red_ordinal_pattern_needs_one_group(td: Path) -> bool:
    arc = str(td / "A.md")
    write_text(arc, "## Pass 2\n")
    rc0, out0 = _main(["--ordinal", "--archive", arc, "--ordinal-pattern", r"^## Pass \d+"])
    rc2, out2 = _main(["--ordinal", "--archive", arc, "--ordinal-pattern", r"^## (Pass) (\d+)"])
    rcx, outx = _main(["--ordinal", "--archive", arc, "--ordinal-pattern", "(["])
    return (rc0 == 2 and "exactly one capture group" in out0 and rc2 == 2 and rcx == 2
            and "not a regular expression" in outx)


# -- line endings, root, defaults, arguments -----------------------------------

def c_crlf_files_round_trip(td: Path) -> bool:
    src, arc = str(td / "S.md"), str(td / "A.md")
    write_text(src, FIXTURE_SRC.replace("\n", "\r\n"))
    write_text(arc, FIXTURE_ARC.replace("\n", "\r\n"))
    rc, out = _call(src, arc)
    s, a = read_text(src), read_text(arc)
    return (rc == 0 and "IDENTICAL" in out and "\r\n" in s and s.count("\n") == s.count("\r\n")
            and a.count("\n") == a.count("\r\n") and a.endswith("decapitation.\r\n")
            and START not in s and START in a and "Trailing line that must survive.\r\n" in s)


def c_red_mixed_line_endings_refused(td: Path) -> bool:
    mixed = FIXTURE_SRC.replace("\n", "\r\n", 3)
    src, arc = _fx(td, mixed)
    rc, out = _call(src, arc)
    return rc == 1 and "mixes CRLF and LF" in out and read_text(src) == mixed and read_text(arc) == FIXTURE_ARC


def c_default_archive_and_root(td: Path) -> bool:
    (td / "docs").mkdir()
    write_text(str(td / "STATUS.md"), FIXTURE_SRC)
    write_text(str(td / "docs" / "STATUS-archive.md"), FIXTURE_ARC)
    rc, out = _main(["--root", str(td), "--source", "STATUS.md", "--start", START, "--end", END,
                     "--divider", DIV, "--apply"])
    a = read_text(str(td / "docs" / "STATUS-archive.md"))
    # and --ordinal reads the same default
    rc2, out2 = _main(["--root", str(td), "--ordinal"])
    return (rc == 0 and START in a and START not in read_text(str(td / "STATUS.md"))
            and rc2 == 0 and "= SECOND (2)" in out2)


def c_missing_archive_exits_2(td: Path) -> bool:
    write_text(str(td / "S.md"), FIXTURE_SRC)
    rc, out = _main(["--root", str(td), "--source", "S.md", "--start", START, "--end", END, "--divider", DIV])
    return rc == 2 and "cannot read" in out


def c_missing_required_arguments_exit_2(td: Path) -> bool:
    rc, out = _main(["--source", "x.md"])
    rc2, out2 = _main(["--root", str(td / "nope"), "--ordinal"])
    return (rc == 2 and "missing required argument(s): --start --end --divider" in out
            and rc2 == 2 and "not a directory" in out2)


def c_empty_archive_gets_no_leading_blank(td: Path) -> bool:
    src, arc = _fx(td, FIXTURE_SRC, "")
    rc, _ = _call(src, arc)
    return rc == 0 and read_text(arc).startswith(DIV + "\n\n" + START)


# -- --verify ----------------------------------------------------------------

def _need_git() -> None:
    if shutil.which("git") is None:
        raise Skip("git not found")


def _g(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                    "-c", "core.autocrlf=false"] + list(args),
                   cwd=str(cwd), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _repo(td: Path, files: Dict[str, str], branch: str = "main") -> Path:
    repo = td / "repo"
    repo.mkdir()
    _g(repo, "init", "-q")
    _g(repo, "symbolic-ref", "HEAD", "refs/heads/" + branch)
    for name, text in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        write_text(str(p), text)
    _g(repo, "add", "-A")
    _g(repo, "commit", "-q", "-m", "base")
    return repo


def _vpaths(repo: Path) -> Tuple[str, str]:
    return str(repo / "STATUS.md"), str(repo / "docs" / "STATUS-archive.md")


def _verify(repo: Path, base: str = "") -> Tuple[int, str]:
    src, arc = _vpaths(repo)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = verify(src, arc, str(repo), base)
        return rc, buf.getvalue()
    except Refusal as exc:
        return 1, buf.getvalue() + str(exc)
    except Harness as exc:
        return 2, buf.getvalue() + str(exc)


def _moved_repo(td: Path, eol: str = "\n") -> Path:
    """A repo whose base holds the ledger; two stubs drained out of it, uncommitted."""
    repo = _repo(td, {"STATUS.md": VER_SRC.replace("\n", eol), "docs/STATUS-archive.md": VER_ARC.replace("\n", eol)})
    src, arc = _vpaths(repo)
    s1, s2 = str(td / "stub1.txt"), str(td / "stub2.txt")
    write_text(s1, "- **alpha** -- moved, kept short.\n")
    write_text(s2, "- **beta** -- moved too.\n")
    rc1, out1 = _run(source=src, archive=arc, start_anchor="- **alpha** first entry, spread",
                     end_anchor="  over two lines.", divider=VER_DIV1, expect=None, apply=True,
                     bullet=True, with_file=s1, drain=True)
    rc2, out2 = _run(source=src, archive=arc, start_anchor="- **beta** second entry.",
                     end_anchor="- **beta** second entry.", divider=VER_DIV2, expect=None, apply=True,
                     bullet=True, with_file=s2, drain=True)
    assert rc1 == 0 and rc2 == 0, out1 + out2
    return repo


def _edit(path: str, old: str, new: str) -> None:
    text = read_text(path)
    assert old in text, (old, text)
    write_text(path, text.replace(old, new, 1))


def c_verify_passes_after_compact(td: Path) -> bool:
    _need_git()
    repo = _repo(td, {"STATUS.md": FIXTURE_SRC, "docs/STATUS-archive.md": FIXTURE_ARC})
    src, arc = _vpaths(repo)
    rc0, _ = _run(source=src, archive=arc, start_anchor=START, end_anchor=END, divider=DIV, expect=None, apply=True)
    rc, out = _verify(repo)
    return rc0 == 0 and rc == 0 and "verify PASS" in out and "3 line(s) in 1 block(s)" in out and "base main" in out


def c_verify_passes_after_two_drains(td: Path) -> bool:
    _need_git()
    repo = _moved_repo(td)
    rc, out = _verify(repo)
    return rc == 0 and "verify PASS" in out and "2 block(s)" in out and "2 stub(s) cite their divider" in out


def c_verify_passes_with_crlf_files(td: Path) -> bool:
    _need_git()
    repo = _moved_repo(td, "\r\n")
    rc, out = _verify(repo)
    return rc == 0 and "verify PASS" in out


def c_verify_passes_after_commit_with_explicit_base(td: Path) -> bool:
    _need_git()
    repo = _moved_repo(td)
    _g(repo, "add", "-A")
    _g(repo, "commit", "-q", "-m", "moved")
    rc, out = _verify(repo, "HEAD~1")
    rc_head, out_head = _verify(repo, "HEAD")
    return (rc == 0 and "base HEAD~1" in out and "verify PASS" in out
            and rc_head == 0 and "0 line(s) in 0 block(s)" in out_head)


def c_verify_passes_with_nothing_moved(td: Path) -> bool:
    _need_git()
    repo = _repo(td, {"STATUS.md": FIXTURE_SRC, "docs/STATUS-archive.md": FIXTURE_ARC})
    rc, out = _verify(repo)
    return rc == 0 and "0 line(s) in 0 block(s)" in out


def c_red_verify_moved_line_changed_one_byte(td: Path) -> bool:
    # RED ARM: after compact, one byte of a moved line is changed in the archive by hand.
    _need_git()
    repo = _moved_repo(td)
    _edit(_vpaths(repo)[1], "- **beta** second entry.", "- **beta** second entry!")
    rc, out = _verify(repo)
    return rc == 1 and "moved line CHANGED" in out and "verify FAIL" in out


def c_red_verify_moved_line_dropped(td: Path) -> bool:
    # RED ARM: a line is dropped from the archived block.
    _need_git()
    repo = _moved_repo(td)
    _edit(_vpaths(repo)[1], "  over two lines.\n", "")
    rc, out = _verify(repo)
    return rc == 1 and "moved line MISSING" in out and "over two lines." in out


def c_red_verify_moved_lines_out_of_order(td: Path) -> bool:
    # RED ARM: two lines of a moved block are swapped in the archive.
    _need_git()
    repo = _moved_repo(td)
    _edit(_vpaths(repo)[1], "- **alpha** first entry, spread\n  over two lines.\n",
          "  over two lines.\n- **alpha** first entry, spread\n")
    rc, out = _verify(repo)
    return rc == 1 and "moved lines OUT OF ORDER" in out


def c_red_verify_stub_divider_paraphrased(td: Path) -> bool:
    # RED ARM: a stub cites a paraphrase of its divider instead of the divider.
    _need_git()
    repo = _moved_repo(td)
    _edit(_vpaths(repo)[0], "body under `%s`." % VER_DIV2, "body under `## ⬇ DRAIN 2 beta`.")
    rc, out = _verify(repo)
    return rc == 1 and "stub does not CITE a divider" in out and "DRAIN 2 beta" in out


def c_red_verify_archive_earlier_text_changed(td: Path) -> bool:
    # RED ARM: text earlier in the archive (before the appended tail) was changed.
    _need_git()
    repo = _moved_repo(td)
    _edit(_vpaths(repo)[1], "old content", "older content")
    rc, out = _verify(repo)
    return rc == 1 and "archive REWRITTEN" in out


def c_red_verify_source_line_edited_by_hand(td: Path) -> bool:
    # RED ARM: another line of the source was edited by hand after compact ran.
    _need_git()
    repo = _moved_repo(td)
    _edit(_vpaths(repo)[0], "Closing line that stays.", "Closing line, edited.")
    rc, out = _verify(repo)
    return rc == 1 and "moved line MISSING" in out and "Closing line that stays." in out


def c_red_verify_source_line_added_by_hand(td: Path) -> bool:
    # RED ARM: a line was added to the source where nothing was moved.
    _need_git()
    repo = _moved_repo(td)
    _edit(_vpaths(repo)[0], "Closing line that stays.\n", "Closing line that stays.\nA brand new line.\n")
    rc, out = _verify(repo)
    return rc == 1 and "hunk removed NOTHING" in out


def c_red_verify_extra_blank_deleted_by_hand(td: Path) -> bool:
    # RED ARM: after a compaction collapsed one blank line, another blank line next to it was deleted by hand.
    _need_git()
    repo = _repo(td, {"STATUS.md": FIXTURE_SRC, "docs/STATUS-archive.md": FIXTURE_ARC})
    src, arc = _vpaths(repo)
    rc0, _ = _run(source=src, archive=arc, start_anchor=START, end_anchor=END, divider=DIV, expect=None, apply=True)
    _edit(src, "Intro line that must survive.\n\n", "Intro line that must survive.\n")
    rc, out = _verify(repo)
    return rc0 == 0 and rc == 1 and "blank line(s) beyond the one each removal collapses" in out


def c_red_verify_extra_line_in_archive_tail(td: Path) -> bool:
    # RED ARM: a line the source did not lose was added to the archive tail.
    _need_git()
    repo = _moved_repo(td)
    _edit(_vpaths(repo)[1], "- **beta** second entry.\n", "- **beta** second entry.\nstray extra line\n")
    rc, out = _verify(repo)
    return rc == 1 and "stray extra line" in out


def c_red_verify_final_newline_dropped(td: Path) -> bool:
    # RED ARM: the source's final newline was removed by hand, so putting the lost lines back cannot
    # rebuild the base byte for byte.
    _need_git()
    repo = _moved_repo(td)
    src = _vpaths(repo)[0]
    write_text(src, read_text(src).rstrip("\n"))
    rc, out = _verify(repo)
    return rc == 1 and "does not rebuild the base source byte for byte" in out


def c_verify_default_base_is_main(td: Path) -> bool:
    _need_git()
    repo = _moved_repo(td)
    rc, out = _verify(repo)
    return rc == 0 and "base main (merge base " in out


def c_verify_default_base_prefers_origin_main(td: Path) -> bool:
    _need_git()
    repo = _moved_repo(td)
    _g(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    rc, out = _verify(repo)
    return rc == 0 and "base origin/main (merge base " in out


def c_red_verify_no_base_exits_2(td: Path) -> bool:
    # RED ARM: no BASE given and neither origin/main nor main exists.
    _need_git()
    repo = _repo(td, {"STATUS.md": VER_SRC, "docs/STATUS-archive.md": VER_ARC}, branch="trunk")
    rc, out = _verify(repo)
    return rc == 2 and "neither origin/main nor main" in out


def c_red_verify_bad_base_exits_2(td: Path) -> bool:
    _need_git()
    repo = _moved_repo(td)
    rc, out = _verify(repo, "no-such-ref")
    return rc == 2 and "not a git ref" in out


def c_red_verify_outside_git_exits_2(td: Path) -> bool:
    _need_git()
    src, arc = _fx(td)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            verify(src, arc, str(td), "")
    except Harness as exc:
        return "git work tree" in str(exc)
    return False


def c_verify_cli_exit_codes(td: Path) -> bool:
    # --verify through main(): BASE is optional and takes the next word, and the exit codes are 0, 1, 2.
    _need_git()
    repo = _moved_repo(td)
    common = ["--root", str(repo), "--source", "STATUS.md"]
    rc0, out0 = _main(["--verify"] + common)
    rc0b, out0b = _main(["--verify", "main"] + common)
    _edit(_vpaths(repo)[1], "- **beta** second entry.", "- **beta** second entry!")
    rc1, out1 = _main(["--verify"] + common)
    rc2, out2 = _main(["--verify", "no-such-ref"] + common)
    rc2b, out2b = _main(["--verify", "--root", str(repo)])
    return (rc0 == 0 and "verify PASS" in out0 and rc0b == 0 and "base main (" in out0b and "merge base" not in out0b
            and rc1 == 1 and "verify FAIL" in out1 and rc2 == 2 and rc2b == 2 and "--source" in out2b)


_CASES: List[Tuple[str, Callable[[Path], bool]]] = [
    ("green-round-trip", c_green_round_trip),
    ("green-path-really-writes", c_green_path_really_writes),
    ("dry-run-writes-nothing", c_dry_run_writes_nothing),
    ("wall3-red-corrupted-archive-byte-refused-and-rolled-back", c_red_corrupted_archive_byte),
    ("wall2-red-boundary-one-line-off-refused", c_red_boundary_one_line_off),
    ("expect-purity-red-mismatch-refused", c_red_expect_purity_mismatch),
    ("expect-purity-match-passes", c_expect_purity_match_passes),
    ("wall1-red-ambiguous-anchor-refused", c_red_ambiguous_anchor),
    ("wall1-red-zero-match-anchor-refused", c_red_zero_match_anchor),
    ("end-above-start-refused", c_red_end_above_start),
    ("wall5-red-unreadable-ordinal-divider-refused", c_wall5_unreadable_divider_refused),
    ("wall5-readable-and-word-free-dividers-pass", c_wall5_readable_and_wordfree_pass),
    ("wall5-marker-flag-moves-or-disables-the-wall", c_wall5_marker_flag),
    ("divider-red-must-be-one-nonblank-line", c_red_divider_must_be_one_line),
    ("wall6-red-end-opening-a-new-entry-refused", c_wall6_end_opens_new_entry_refused),
    ("wall6-single-line-entry-passes", c_wall6_single_line_entry_passes),
    ("wall6-indented-item-opens-nothing", c_wall6_indented_item_opens_nothing),
    ("wall7-red-same-file-refused", c_wall7_same_file_refused),
    ("wall7-red-symlink-spelling-refused", c_wall7_symlink_spelling_refused),
    ("wall8-default-mode-refuses-adjacent-bullets", c_wall8_default_mode_refuses_adjacent_bullets),
    ("wall8-bullet-moves-one-verbatim-with-replacement", c_wall8_bullet_moves_one_verbatim),
    ("wall8-red-end-at-next-bullet-refused", c_wall8_red_next_bullet_end),
    ("wall8-red-orphaned-nested-item-refused", c_wall8_red_orphaned_nested_item),
    ("wall8-red-mid-bullet-start-refused", c_wall8_red_mid_bullet_start),
    ("wall8-red-with-without-bullet-refused", c_red_with_requires_bullet),
    ("with-file-missing-exits-2", c_missing_with_file_exits_2),
    ("wall9-red-drain-without-bullet-refused", c_drain_requires_bullet),
    ("wall9-red-drain-without-with-refused", c_drain_requires_with),
    ("wall9-drain-stub-ends-with-citation", c_drain_stub_ends_with_citation),
    ("wall9-drain-citation-at-end-not-duplicated", c_drain_citation_already_at_end_not_duplicated),
    ("wall9-red-drain-citation-mid-prose-refused", c_red_drain_citation_mid_prose_refused),
    ("wall9-drain-divider-quoted-byte-for-byte", c_drain_divider_quoted_byte_for_byte),
    ("wall4-ordinal-hyphens-highest-unknown", c_ordinal_hyphens_highest_unknown),
    ("wall4-ordinal-prints-highest-and-next", c_ordinal_prints_highest_and_next),
    ("wall4-ordinal-unknown-word-reported-not-guessed", c_ordinal_unknown_word_reported_not_guessed),
    ("wall4-ordinal-none-found-is-unsettled", c_ordinal_none_found_unsettled),
    ("ordinal-custom-pattern", c_ordinal_custom_pattern),
    ("ordinal-custom-pattern-reaches-compact", c_ordinal_custom_pattern_reaches_compact),
    ("ordinal-red-pattern-needs-exactly-one-group", c_red_ordinal_pattern_needs_one_group),
    ("crlf-files-round-trip", c_crlf_files_round_trip),
    ("red-mixed-line-endings-refused", c_red_mixed_line_endings_refused),
    ("default-archive-and-root", c_default_archive_and_root),
    ("missing-archive-exits-2", c_missing_archive_exits_2),
    ("missing-arguments-exit-2", c_missing_required_arguments_exit_2),
    ("empty-archive-gets-no-leading-blank", c_empty_archive_gets_no_leading_blank),
    ("verify-passes-after-compact", c_verify_passes_after_compact),
    ("verify-passes-after-two-drains", c_verify_passes_after_two_drains),
    ("verify-passes-with-crlf-files", c_verify_passes_with_crlf_files),
    ("verify-passes-after-commit-with-explicit-base", c_verify_passes_after_commit_with_explicit_base),
    ("verify-passes-with-nothing-moved", c_verify_passes_with_nothing_moved),
    ("verify-red-moved-line-changed-by-one-byte", c_red_verify_moved_line_changed_one_byte),
    ("verify-red-moved-line-dropped", c_red_verify_moved_line_dropped),
    ("verify-red-moved-lines-out-of-order", c_red_verify_moved_lines_out_of_order),
    ("verify-red-stub-divider-paraphrased", c_red_verify_stub_divider_paraphrased),
    ("verify-red-archive-earlier-text-changed", c_red_verify_archive_earlier_text_changed),
    ("verify-red-source-line-edited-by-hand", c_red_verify_source_line_edited_by_hand),
    ("verify-red-source-line-added-by-hand", c_red_verify_source_line_added_by_hand),
    ("verify-red-extra-blank-line-deleted-by-hand", c_red_verify_extra_blank_deleted_by_hand),
    ("verify-red-extra-line-in-archive-tail", c_red_verify_extra_line_in_archive_tail),
    ("verify-red-final-newline-dropped", c_red_verify_final_newline_dropped),
    ("verify-default-base-is-main", c_verify_default_base_is_main),
    ("verify-default-base-prefers-origin-main", c_verify_default_base_prefers_origin_main),
    ("verify-red-no-base-exits-2", c_red_verify_no_base_exits_2),
    ("verify-red-bad-base-exits-2", c_red_verify_bad_base_exits_2),
    ("verify-red-outside-git-exits-2", c_red_verify_outside_git_exits_2),
    ("verify-cli-exit-codes", c_verify_cli_exit_codes),
]

_NEEDS_GIT = {name for name, _ in _CASES if name.startswith("verify-")}


def _run_self_test() -> int:
    """Run every case in its own temporary directory; print PASS, FAIL or SKIP per case."""
    n_pass = n_fail = n_skip = 0
    git_ok = shutil.which("git") is not None
    for name, fn in _CASES:
        if name in _NEEDS_GIT and not git_ok:
            print("SKIP %s (git not found)" % name)
            n_skip += 1
            continue
        with tempfile.TemporaryDirectory() as td:
            try:
                ok = fn(Path(td))
            except Skip as why:
                print("SKIP %s (%s)" % (name, why))
                n_skip += 1
                continue
            except Exception as exc:  # a crashing case is a failing case
                print("FAIL %s (%s: %s)" % (name, type(exc).__name__, exc))
                n_fail += 1
                continue
        if ok:
            print("PASS %s" % name)
            n_pass += 1
        else:
            print("FAIL %s" % name)
            n_fail += 1
    ran = n_pass + n_fail
    if n_fail:
        print("%s: self-test FAIL -- %d of %d cases failed" % (PROG, n_fail, ran))
        return 1
    print("%s: self-test PASS -- %d of %d cases passed%s"
          % (PROG, n_pass, ran, (", %d skipped" % n_skip) if n_skip else ""))
    return 0


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog=PROG,
        description="Move a block of text into an archive verbatim and prove it; verify a compaction "
                    "against git; read the archive's ordinal. Run --help on a mode for its options.")
    ap.add_argument("--root", default=".", metavar="DIR",
                    help="relative --source, --archive and --with paths resolve against this directory "
                         "(default: the current directory)")
    ap.add_argument("--source", metavar="FILE", help="the file the block is moved out of")
    ap.add_argument("--archive", default=DEFAULT_ARCHIVE, metavar="FILE",
                    help="the append-only archive (default: %s)" % DEFAULT_ARCHIVE)
    ap.add_argument("--start", dest="start_anchor", metavar="LINE", help="exact text of the first line to move")
    ap.add_argument("--end", dest="end_anchor", metavar="LINE", help="exact text of the last line to move (inclusive)")
    ap.add_argument("--divider", metavar="HEADING", help="the heading line written above the block in the archive")
    ap.add_argument("--expect-purity", dest="expect", metavar="HEX",
                    help="the digest you computed for the block; refuse if the selected block differs")
    ap.add_argument("--apply", action="store_true", help="write the move (default: a dry run that writes nothing)")
    ap.add_argument("--bullet", action="store_true",
                    help="move ONE top-level bullet (wall 8 replaces wall 2); reaches adjacent bullets")
    ap.add_argument("--with", dest="with_file", metavar="FILE",
                    help="--bullet only: FILE whose text replaces the bullet in the source (never a string)")
    ap.add_argument("--drain", action="store_true",
                    help="requires --bullet and --with; the stub ends with `body under `<divider>`.`, "
                         "the divider quoted byte for byte, appended if --with did not end with it")
    ap.add_argument("--ordinal", action="store_true", help="print the archive's highest ordinal and the next one")
    ap.add_argument("--ordinal-pattern", default=DEFAULT_PATTERN, metavar="REGEX",
                    help="heading pattern with ONE group, the ordinal word or number (default: %s)" % DEFAULT_PATTERN)
    ap.add_argument("--ordinal-marker", default=DEFAULT_MARKER, metavar="WORD",
                    help="a divider containing this word must match the pattern (wall 5); empty turns the "
                         "wall off (default: %s)" % DEFAULT_MARKER)
    ap.add_argument("--verify", nargs="?", const="", default=None, metavar="BASE",
                    help="prove from git that the changes since BASE only moved text into the archive "
                         "(default BASE: merge base of HEAD with origin/main, else main); needs --source")
    ap.add_argument("--self-test", dest="selftest", action="store_true", help="run the built-in cases and exit")
    return ap


def main(argv: Sequence[str]) -> int:
    args = _build_parser().parse_args(list(argv))
    if args.selftest:
        return _run_self_test()
    try:
        root = args.root
        if not os.path.isdir(root):
            raise Harness("--root %s is not a directory" % root)

        def rp(p: Optional[str]) -> Optional[str]:
            return p if p is None or os.path.isabs(p) else os.path.normpath(os.path.join(root, p))

        archive = rp(args.archive)
        assert archive is not None
        if args.ordinal:
            return do_ordinal(archive, compile_pattern(args.ordinal_pattern))
        if args.verify is not None:
            if not args.source:
                raise Harness("missing required argument(s): --source")
            return verify(rp(args.source) or "", archive, root, args.verify)
        missing = [n for n, v in (("--source", args.source), ("--start", args.start_anchor),
                                  ("--end", args.end_anchor), ("--divider", args.divider)) if not v]
        if missing:
            raise Harness("missing required argument(s): %s" % " ".join(missing))
        return compact(rp(args.source) or "", archive, args.start_anchor, args.end_anchor, args.divider,
                       args.expect, args.apply, bullet=args.bullet, with_file=rp(args.with_file),
                       drain=args.drain, ordinal_re=compile_pattern(args.ordinal_pattern),
                       marker=args.ordinal_marker)
    except Refusal as exc:
        print("compact: REFUSED -- %s" % exc, file=sys.stderr)
        return 1
    except Harness as exc:
        print("compact: error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    # A console that cannot print the headings' symbols must not turn a result into a crash.
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(errors="backslashreplace")
    sys.exit(main(sys.argv[1:]))
