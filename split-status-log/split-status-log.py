#!/usr/bin/env python3
"""split-status-log: keep a newest-first, dated log file small by splitting it by month, verbatim.

Usage: split-status-log.py [--log FILE] [--archive-dir DIR] [--date YYYY-MM-DD] [--apply | --verify]
       split-status-log.py --self-test

What it does
    A log that grows without end costs every reader its whole size on every
    call. This tool keeps the log to the current month and moves each older
    month into its own file. The log is newest-first, sectioned by
    ``## YYYY-MM-DD ...`` headings, and every month's headings run in one
    contiguous block. The tool:

      1. Finds the preamble (everything before the first dated heading).
      2. Groups the dated sections by month.
      3. Leaves the CURRENT month (the month of the first, newest heading) in
         the log, preceded by the preamble and a pointer block that names
         every archived month.
      4. Writes every OLDER month verbatim to ``<archive-dir>/YYYY-MM.md``:
         no preamble, no pointer block, that month's sections in the order
         they already had.

    Nothing is reworded and nothing is reordered: the log with its pointer
    block stripped, followed by the archived months newest first, is the
    original file byte for byte (line endings and bytes that are not UTF-8
    included). A dry run is the default; --apply writes.

Paths
    --log FILE         the log (default: docs/STATUS-log.md, relative to the current directory)
    --archive-dir DIR  where the month files go (default: the log's path with
                       its ``.md`` removed, beside it: docs/STATUS-log.md ->
                       docs/STATUS-log/). Required when the log has no extension.

Why it is safe to re-run
    - On an unsplit log it splits as above.
    - On an already-split log with new current-month entries added at the top
      by someone else, it strips the existing pointer block, finds only the
      current month, and writes the pointer block again: a no-op on content.
    - If a month rolls over (the first heading names a new month), the month
      just completed is archived for the first time and the new month becomes
      current.
    - If entries for an already-archived month turn up in the log below the
      current month, they are put in front of that month's file, unless they
      are already a prefix of it, so a second run over the same input
      changes nothing.
    - Only files named ``YYYY-MM.md`` in the archive directory count as
      archived months; any other file there is left alone and ignored.

What it refuses (exit 1, and nothing is written)
    - no ``## YYYY-MM-DD`` heading at all;
    - a month whose headings are not one contiguous run;
    - months that are not newest-first (an oldest-first log would archive the
      wrong months);
    - a pointer block with a begin marker and no end marker, or the reverse.
    A heading is a line that starts ``## `` and a date; a line inside a fenced
    code block that looks like one is taken for a heading.

Modes
    (none)    dry run: print the plan, write nothing
    --apply   write the split, then check what was written
    --verify  read the split on disk and confirm it round-trips: the months are
              contiguous and newest-first, every month file holds only its own
              month, and no month is both in the log and archived

Exit codes
    0  clean plan, apply, verify or self-test
    1  a refusal (a structural check failed; nothing was written), or a failed
       verify or self-test
    2  a usage error or a harness error (the log cannot be read, a file cannot
       be written)

--self-test
    Builds its own logs in a temporary directory, runs the built-in cases,
    prints one PASS or FAIL line per case and a verdict line, and exits
    non-zero on any failure.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import io
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

PROG = "split-status-log"
DEFAULT_LOG = "docs/STATUS-log.md"

HEADING_RE = re.compile(r"^## (\d{4})-(\d{2})-\d{2}\b")
MONTH_FILE_RE = re.compile(r"^(\d{4}-\d{2})\.md$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

POINTER_BEGIN = "<!-- STATUS-LOG-POINTER:BEGIN -->"
POINTER_END = "<!-- STATUS-LOG-POINTER:END -->"


class Refusal(Exception):
    """A structural check failed. Exit 1, nothing written."""


class Layout(NamedTuple):
    log: Path
    archive: Path


# ---------------------------------------------------------------------------
# Text in and out: bytes round-trip exactly (UTF-8 with undecodable bytes kept,
# line endings untouched).
# ---------------------------------------------------------------------------

def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="surrogateescape")


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(text.encode("utf-8", errors="surrogateescape"))
    os.replace(str(tmp), str(path))


def _lines(text: str) -> List[str]:
    """Split on ``\\n`` only, keeping each line's ending (str.splitlines also breaks on form feeds etc.)."""
    out: List[str] = []
    start = 0
    while True:
        i = text.find("\n", start)
        if i < 0:
            if start < len(text):
                out.append(text[start:])
            return out
        out.append(text[start:i + 1])
        start = i + 1


def archive_months(layout: Layout) -> List[str]:
    """The months archived on disk (files named YYYY-MM.md), newest first."""
    if not layout.archive.is_dir():
        return []
    found = []
    for p in layout.archive.iterdir():
        m = MONTH_FILE_RE.match(p.name)
        if m and p.is_file():
            found.append(m.group(1))
    return sorted(found, reverse=True)


# ---------------------------------------------------------------------------
# The split
# ---------------------------------------------------------------------------

def strip_pointer_block(text: str) -> str:
    """Remove every pointer block. Idempotent: a text with none comes back unchanged."""
    while True:
        b = text.find(POINTER_BEGIN)
        if b < 0:
            if POINTER_END in text:
                raise Refusal("a pointer end marker has no begin marker before it; "
                              "refusing rather than guess what to remove.")
            return text
        e = text.find(POINTER_END, b)
        if e < 0:
            raise Refusal("a pointer begin marker has no end marker after it; "
                          "refusing rather than guess what to remove.")
        e += len(POINTER_END)
        m = re.compile(r"(?:\r?\n){1,2}").match(text, e)  # the END line's break, plus one blank line
        if m:
            e = m.end()
        text = text[:b] + text[e:]


def month_groups(text: str) -> Tuple[str, List[Tuple[str, str]]]:
    """(preamble, [(month, block_text), ...]): months in the order they occur (newest first),
    each block the verbatim text of its contiguous run of headings and everything under them."""
    lines = _lines(text)
    heading_idx: List[Tuple[int, str]] = []
    for i, ln in enumerate(lines):
        m = HEADING_RE.match(ln)
        if m:
            heading_idx.append((i, f"{m.group(1)}-{m.group(2)}"))

    if not heading_idx:
        raise Refusal("no `## YYYY-MM-DD` heading found -- nothing to split. "
                      "This tool only understands a log sectioned by dated headings.")

    preamble = "".join(lines[:heading_idx[0][0]])
    groups: List[Tuple[str, str]] = []
    cur_month: Optional[str] = None
    cur_start = 0
    for i, month in heading_idx:
        if month != cur_month:
            if cur_month is not None:
                groups.append((cur_month, "".join(lines[cur_start:i])))
            cur_month = month
            cur_start = i
    assert cur_month is not None  # heading_idx is non-empty (refused above)
    groups.append((cur_month, "".join(lines[cur_start:])))

    # A month appearing in two separate runs breaks the contiguity this tool relies on;
    # refuse by name rather than merge and silently reorder entries.
    seen = set()
    for month, _ in groups:
        if month in seen:
            raise Refusal(f"month {month} appears in more than one non-contiguous run of headings -- "
                          "refusing rather than silently reordering entries; "
                          "this needs a human to look at the actual break.")
        seen.add(month)
    # The first month is taken as the current one, so the log must be newest-first.
    for (newer, _), (older, _) in zip(groups, groups[1:]):
        if newer < older:
            raise Refusal(f"months are not newest-first ({newer} comes before {older}) -- "
                          "refusing, because the first month would be taken as the current one "
                          "and the wrong months would be archived.")
    return preamble, groups


def _rel_posix(target: Path, start: Path) -> str:
    try:
        return Path(os.path.relpath(str(target), str(start))).as_posix()
    except ValueError:  # another drive on Windows
        return target.as_posix()


def build_pointer_block(layout: Layout, current_month: str, archived: List[str], date: str) -> str:
    rel = _rel_posix(layout.archive, layout.log.parent)
    lines = [POINTER_BEGIN, ""]
    lines.append(
        f"> **Split {date} by `split-status-log`.** This file carries the CURRENT month "
        f"({current_month}) only; older months live under `{rel}/`. To find an entry by its date, "
        f"read the month: this file if it is the current month, `{rel}/YYYY-MM.md` otherwise; "
        "then find the dated `## ` heading inside it."
    )
    lines.append(">")
    for m in archived:  # newest archived month first
        lines.append(f"> - `{rel}/{m}.md`")
    lines.append("")
    lines.append(POINTER_END)
    lines.append("")
    return "\n".join(lines) + "\n"


def plan(layout: Layout, text: str, date: str) -> Tuple[str, Dict[str, str], List[str]]:
    """(new_log_text, {month: full_new_archive_text}, months moved this pass, newest first),
    without writing anything."""
    text = strip_pointer_block(text)
    preamble, groups = month_groups(text)
    current_month, current_block = groups[0]
    older = groups[1:]

    archive_writes: Dict[str, str] = {}
    moved: List[str] = []
    for month, block in older:
        moved.append(month)
        path = layout.archive / f"{month}.md"
        if path.is_file():
            existing = _read(path)
            if existing.startswith(block):
                archive_writes[month] = existing        # already archived: no-op
            elif block.startswith(existing):
                archive_writes[month] = block           # an earlier, partial write: take the whole
            else:
                archive_writes[month] = block + existing  # new entries for an archived month, newest first
        else:
            archive_writes[month] = block

    # The pointer names every month archived so far, not only the ones this pass moved,
    # so a re-run that finds nothing new to archive does not forget earlier months.
    named = sorted(set(archive_months(layout)) | set(moved), reverse=True)
    new_log_text = build_pointer_block(layout, current_month, named, date) + preamble + current_block

    # Checked before anything is written.
    if strip_pointer_block(new_log_text) != preamble + current_block:
        raise Refusal("internal check failed: the new log does not hold the preamble and current month "
                      "verbatim. Nothing written; report this rather than trust the split.")
    for month, block in older:
        path = layout.archive / f"{month}.md"
        existing = _read(path) if path.is_file() else ""
        new = archive_writes[month]
        if not (new.startswith(block) and (new == block or new.endswith(existing))):
            raise Refusal(f"internal check failed: the new {month} file would lose or reorder text. "
                          "Nothing written; report this rather than trust the split.")
    return new_log_text, archive_writes, moved


def _changed(path: Path, text: str) -> bool:
    return not path.is_file() or _read(path) != text


def do_plan(layout: Layout, date: str) -> int:
    text = _read(layout.log)
    new_log_text, archive_writes, moved = plan(layout, text, date)
    print(f"{PROG}: log            {layout.log}")
    print(f"{PROG}: archive dir    {layout.archive}")
    print(f"{PROG}: current month kept in place; {len(moved)} older month(s): {', '.join(moved) or '(none)'}")
    print(f"{PROG}: new log        {len(_lines(new_log_text))} line(s)"
          f"{'  (write)' if _changed(layout.log, new_log_text) else '  (unchanged)'}")
    for m, t in archive_writes.items():
        p = layout.archive / f"{m}.md"
        print(f"{PROG}:   {p}  {len(_lines(t))} line(s){'  (write)' if _changed(p, t) else '  (unchanged)'}")
    print(f"{PROG}: DRY RUN -- nothing written. Re-run with --apply.")
    return 0


def do_apply(layout: Layout, date: str) -> int:
    text = _read(layout.log)
    new_log_text, archive_writes, moved = plan(layout, text, date)
    if layout.archive.exists() and not layout.archive.is_dir():
        raise Refusal(f"{layout.archive} exists and is not a directory.")
    layout.archive.mkdir(parents=True, exist_ok=True)
    planned: List[Tuple[Path, str]] = [(layout.archive / f"{m}.md", t) for m, t in archive_writes.items()]
    planned.append((layout.log, new_log_text))  # the log last: a crash before it leaves a re-runnable state
    for path, t in planned:
        if _changed(path, t):
            _write(path, t)
    for path, t in planned:
        if _read(path) != t:
            raise Refusal(f"post-write check FAILED: {path} does not hold what was planned. "
                          "Report this rather than trust the split.")
    print(f"{PROG}: LANDED -- current month kept in {layout.log}, {len(moved)} older month(s) "
          f"archived under {layout.archive}, what was written re-read and checked.")
    return 0


def concat_for_verify(layout: Layout, log_text: str) -> str:
    """The log with its pointer block stripped, then every month file newest first: the original file."""
    out = strip_pointer_block(log_text)
    for m in archive_months(layout):
        out += _read(layout.archive / f"{m}.md")
    return out


def do_verify(layout: Layout) -> int:
    """Read the split on disk and confirm it round-trips. Needs no snapshot from before the split."""
    text = _read(layout.log)
    problems: List[str] = []
    combined = concat_for_verify(layout, text)
    try:
        month_groups(combined)
    except Refusal as exc:
        problems.append(str(exc))
    log_months = {m for _, g in [month_groups(strip_pointer_block(text))] for m, _ in g}
    for m in archive_months(layout):
        body = _read(layout.archive / f"{m}.md")
        try:
            pre, groups = month_groups(body)
        except Refusal as exc:
            problems.append(f"{m}.md: {exc}")
            continue
        if pre.strip():
            problems.append(f"{m}.md: text before its first dated heading")
        if [g for g, _ in groups] != [m]:
            problems.append(f"{m}.md: holds headings of {', '.join(g for g, _ in groups)}, not only {m}")
        if m in log_months:
            problems.append(f"month {m} is both in the log and archived")
    if problems:
        for p in problems:
            print(f"{PROG}: VERIFY FAILED -- {p}", file=sys.stderr)
        return 1
    print(f"{PROG}: VERIFY OK -- concatenation is {len(_lines(combined))} line(s), "
          "months contiguous and newest-first, every month file holds only its own month.")
    return 0


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def _layout_from_args(ap: argparse.ArgumentParser, args: argparse.Namespace) -> Layout:
    log = Path(args.log)
    if args.archive_dir is not None:
        archive = Path(args.archive_dir)
    else:
        archive = log.with_suffix("")
        if archive == log:
            ap.error("the log has no extension, so there is no default archive directory; give --archive-dir")
    return Layout(log, archive)


def main(argv: List[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")  # type: ignore[attr-defined]
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(errors="backslashreplace")  # type: ignore[attr-defined]
    ap = argparse.ArgumentParser(
        prog=PROG, description="Keep a newest-first dated log small by splitting it by month, verbatim.")
    ap.add_argument("--log", default=DEFAULT_LOG, metavar="FILE",
                    help=f"the log to split (default: {DEFAULT_LOG}, relative to the current directory)")
    ap.add_argument("--archive-dir", default=None, metavar="DIR",
                    help="where the YYYY-MM.md files go (default: the log's path without .md)")
    ap.add_argument("--date", default=None, metavar="YYYY-MM-DD",
                    help="date stamp for the pointer block (default: today, from the system clock)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="write the split (the default is a dry run)")
    mode.add_argument("--verify", action="store_true", help="check the split on disk and write nothing")
    ap.add_argument("--self-test", action="store_true", help="run the built-in cases and exit")
    args = ap.parse_args(argv)

    if args.self_test:
        return _run_self_test()
    if args.date is not None and not DATE_RE.match(args.date):
        ap.error(f"--date must look like YYYY-MM-DD, not {args.date!r}")
    layout = _layout_from_args(ap, args)
    date = args.date or datetime.date.today().isoformat()
    try:
        if args.verify:
            return do_verify(layout)
        if args.apply:
            return do_apply(layout, date)
        return do_plan(layout, date)
    except Refusal as exc:
        print(f"{PROG}: REFUSED -- {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"{PROG}: harness error -- {exc}", file=sys.stderr)
        return 2


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

FIXTURE = """# Fixture log (newest first)

**What this is.** A preamble line that must survive.

## 2026-03-15 — march entry two

march two body

## 2026-03-01 — march entry one

march one body

## 2026-02-20 — february entry

february body
"""
DATE = "2026-04-30"


def _put(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8", errors="surrogateescape"))


def _get(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="surrogateescape")


def _layout(td: Path, text: str = FIXTURE) -> Layout:
    lay = Layout(td / "docs" / "STATUS-log.md", td / "docs" / "STATUS-log")
    _put(lay.log, text)
    return lay


def _quiet(fn: Callable[..., int], *a: object) -> int:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn(*a)


def _cli(td: Path, *args: str) -> Tuple[str, str, int]:
    script = str(Path(__file__).resolve())
    r = subprocess.run([sys.executable, script, *args], cwd=str(td), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    return r.stdout, r.stderr, r.returncode


def _tree(td: Path) -> List[Tuple[str, bytes]]:
    return sorted((p.relative_to(td).as_posix(), p.read_bytes()) for p in td.rglob("*") if p.is_file())


def _case_plain_split(td: Path) -> bool:
    lay = _layout(td)
    rc = _quiet(do_apply, lay, DATE)
    new_log = _get(lay.log)
    feb = _get(lay.archive / "2026-02.md")
    return (rc == 0
            and "march entry two" in new_log and "march entry one" in new_log
            and "february entry" not in new_log and "february entry" in feb
            and sorted(p.name for p in lay.archive.iterdir()) == ["2026-02.md"]
            and "preamble line that must survive" in new_log
            and concat_for_verify(lay, new_log) == FIXTURE)


def _case_idempotent(td: Path) -> bool:
    lay = _layout(td)
    _quiet(do_apply, lay, DATE)
    first = _tree(td)
    rc = _quiet(do_apply, lay, DATE)
    return rc == 0 and _tree(td) == first


def _case_concurrent_append(td: Path) -> bool:
    lay = _layout(td)
    _quiet(do_apply, lay, DATE)
    feb_before = _get(lay.archive / "2026-02.md")
    cur = _get(lay.log)
    b = cur.find(POINTER_END) + len(POINTER_END)
    rest = cur[b:]
    at = rest.find("## ")
    injected = rest[:at] + "## 2026-03-20 — a landing added this mid-run\n\nnew body\n\n" + rest[at:]
    _put(lay.log, cur[:b] + injected)
    rc = _quiet(do_apply, lay, DATE)
    final = _get(lay.log)
    return (rc == 0 and "a landing added this mid-run" in final and "march entry two" in final
            and _get(lay.archive / "2026-02.md") == feb_before)


def _case_month_rollover(td: Path) -> bool:
    lay = _layout(td)
    _quiet(do_apply, lay, DATE)
    cur = _get(lay.log)
    b = cur.find(POINTER_END) + len(POINTER_END)
    rest = cur[b:]
    at = rest.find("## ")
    _put(lay.log, cur[:b] + rest[:at] + "## 2026-04-02 — april entry\n\napril body\n\n" + rest[at:])
    rc = _quiet(do_apply, lay, DATE)
    final = _get(lay.log)
    mar = _get(lay.archive / "2026-03.md")
    return (rc == 0 and "april entry" in final and "march entry two" not in final
            and mar.startswith("## 2026-03-15") and "march entry one" in mar
            and "`STATUS-log/2026-03.md`" in final and "`STATUS-log/2026-02.md`" in final
            and concat_for_verify(lay, final).replace(
                "## 2026-04-02 — april entry\n\napril body\n\n", "") == FIXTURE)


def _case_stray_old_entry_goes_to_its_month(td: Path) -> bool:
    lay = _layout(td)
    _quiet(do_apply, lay, DATE)
    stray = "## 2026-02-25 — a late february entry\n\nlate body\n\n"
    _put(lay.log, _get(lay.log) + "\n" + stray)
    rc = _quiet(do_apply, lay, DATE)
    feb = _get(lay.archive / "2026-02.md")
    after = _tree(td)
    rc2 = _quiet(do_apply, lay, DATE)
    return (rc == 0 and feb.startswith("## 2026-02-25") and "## 2026-02-20" in feb
            and feb.index("## 2026-02-25") < feb.index("## 2026-02-20") and "a late february entry" not in _get(lay.log)
            and rc2 == 0 and _tree(td) == after)


def _case_plan_moves_content(td: Path) -> bool:
    # Anti-vacuity: the plan moves content, checked by what moved rather than by byte count.
    lay = Layout(td / "x.md", td / "x")
    new_log, writes, moved = plan(lay, FIXTURE, DATE)
    return (moved == ["2026-02"] and "february entry" not in new_log and "march entry two" in new_log
            and "february entry" in writes["2026-02"])


def _case_interleaved_refused(td: Path) -> bool:
    # RED ARM: months that interleave are refused by name, never merged or reordered.
    try:
        month_groups(FIXTURE + "\n## 2026-03-25 — march again after february\n\nbody\n")
    except Refusal as exc:
        return "non-contiguous" in str(exc)
    return False


def _case_oldest_first_refused(td: Path) -> bool:
    # RED ARM: an oldest-first log is refused; its first month is not the current one.
    text = "# log\n\n## 2026-01-05 - a\n\nbody\n\n## 2026-02-05 - b\n\nbody\n"
    lay = _layout(td, text)
    out, err, rc = _cli(td, "--apply")
    return rc == 1 and "not newest-first" in err and _get(lay.log) == text and not lay.archive.exists()


def _case_no_heading_refused(td: Path) -> bool:
    # RED ARM: a file with no dated heading is refused and left alone.
    text = "# notes\n\nno dated headings here\n"
    lay = _layout(td, text)
    _, err, rc = _cli(td, "--apply")
    return rc == 1 and "no `## YYYY-MM-DD` heading found" in err and _get(lay.log) == text


def _case_refusal_writes_nothing(td: Path) -> bool:
    # RED ARM: a refusal leaves every file untouched.
    lay = _layout(td, FIXTURE + "\n## 2026-03-25 — march again\n\nbody\n")
    before = _tree(td)
    _, err, rc = _cli(td, "--apply")
    return rc == 1 and "REFUSED" in err and _tree(td) == before


def _case_unbalanced_pointer_refused(td: Path) -> bool:
    # RED ARM: a begin marker with no end (or the reverse) is refused, not guessed at.
    lay = _layout(td, POINTER_BEGIN + "\n\n" + FIXTURE)
    lay2 = Layout(td / "two.md", td / "two")
    _put(lay2.log, FIXTURE + POINTER_END + "\n")
    before = _tree(td)
    _, err1, rc1 = _cli(td, "--apply")
    _, err2, rc2 = _cli(td, "--log", "two.md", "--apply")
    return (rc1 == 1 and "no end marker" in err1 and rc2 == 1 and "no begin marker" in err2
            and _tree(td) == before)


def _case_dry_run_writes_nothing(td: Path) -> bool:
    _layout(td)
    before = _tree(td)
    out, _, rc = _cli(td)
    return (rc == 0 and "DRY RUN -- nothing written" in out and "1 older month(s): 2026-02" in out
            and _tree(td) == before and not (td / "docs" / "STATUS-log").exists())


def _case_cli_apply_then_verify(td: Path) -> bool:
    _layout(td)
    out1, _, rc1 = _cli(td, "--apply", "--date", DATE)
    out2, _, rc2 = _cli(td, "--verify")
    return (rc1 == 0 and "LANDED" in out1 and rc2 == 0 and "VERIFY OK" in out2
            and f"Split {DATE} by" in _get(td / "docs" / "STATUS-log.md"))


def _case_verify_catches_foreign_month(td: Path) -> bool:
    # RED ARM: a month file holding another month's heading fails verify, by name.
    lay = _layout(td)
    _quiet(do_apply, lay, DATE)
    _put(lay.archive / "2026-02.md", _get(lay.archive / "2026-02.md") + "\n## 2026-01-09 — january\n\nbody\n")
    _, err, rc = _cli(td, "--verify")
    return rc == 1 and "2026-02.md: holds headings of 2026-02, 2026-01, not only 2026-02" in err


def _case_verify_catches_month_in_both(td: Path) -> bool:
    # RED ARM: a month that is both in the log and archived fails verify.
    lay = _layout(td)
    _quiet(do_apply, lay, DATE)
    _put(lay.archive / "2026-03.md", "## 2026-03-02 — a copy\n\nbody\n")
    _, err, rc = _cli(td, "--verify")
    return rc == 1 and "month 2026-03 is both in the log and archived" in err


def _case_verify_catches_misordered_months(td: Path) -> bool:
    # RED ARM: an archive that puts a newer month after an older one fails verify.
    lay = _layout(td)
    _quiet(do_apply, lay, DATE)
    _put(lay.archive / "2026-01.md", "## 2026-01-09 — january\n\nbody\n")
    _put(lay.archive / "2026-04.md", "## 2026-04-09 — april\n\nbody\n")
    _, err, rc = _cli(td, "--verify")
    return rc == 1 and "not newest-first" in err


def _case_missing_log_exits_2(td: Path) -> bool:
    _, err, rc = _cli(td, "--apply")
    return rc == 2 and "harness error" in err


def _case_crlf_kept(td: Path) -> bool:
    crlf = FIXTURE.replace("\n", "\r\n")
    lay = _layout(td, crlf)
    rc = _quiet(do_apply, lay, DATE)
    feb = (lay.archive / "2026-02.md").read_bytes()
    return rc == 0 and b"\r\n" in feb and b"\n" not in feb.replace(b"\r\n", b"") and \
        concat_for_verify(lay, _get(lay.log)) == crlf


def _case_non_utf8_kept(td: Path) -> bool:
    raw = FIXTURE.encode("utf-8").replace(b"february body", b"caf\xe9 body")
    lay = Layout(td / "STATUS-log.md", td / "STATUS-log")
    lay.log.write_bytes(raw)
    rc = _quiet(do_apply, lay, DATE)
    feb = (lay.archive / "2026-02.md").read_bytes()
    rebuilt = concat_for_verify(lay, _get(lay.log)).encode("utf-8", errors="surrogateescape")
    return rc == 0 and b"caf\xe9 body" in feb and rebuilt == raw


def _case_only_newline_breaks_lines(td: Path) -> bool:
    # A form feed or vertical tab is not a line break: a heading-looking tail after one is not a heading.
    text = FIXTURE.replace("february body", "february body\x0c## 2026-01-01 not a heading\x0b")
    lay = _layout(td, text)
    rc = _quiet(do_apply, lay, DATE)
    return (rc == 0 and sorted(p.name for p in lay.archive.iterdir()) == ["2026-02.md"]
            and concat_for_verify(lay, _get(lay.log)) == text)


def _case_stray_archive_files_ignored(td: Path) -> bool:
    lay = _layout(td)
    _put(lay.archive / "README.md", "not a month\n")
    _put(lay.archive / "2026-1.md", "not a month either\n")
    rc = _quiet(do_apply, lay, DATE)
    new_log = _get(lay.log)
    return (rc == 0 and "README" not in new_log and "2026-1.md" not in new_log
            and concat_for_verify(lay, new_log) == FIXTURE
            and _get(lay.archive / "README.md") == "not a month\n")


def _case_flags_and_relative_pointer(td: Path) -> bool:
    _put(td / "docs" / "LOG.md", FIXTURE)
    out, _, rc = _cli(td, "--log", "docs/LOG.md", "--archive-dir", "docs/old", "--apply", "--date", DATE)
    log = _get(td / "docs" / "LOG.md")
    return (rc == 0 and (td / "docs" / "old" / "2026-02.md").is_file()
            and "`old/2026-02.md`" in log and "`old/`" in log and not (td / "docs" / "LOG").exists())


def _case_default_archive_dir_from_log_name(td: Path) -> bool:
    _put(td / "docs" / "STATUS-log.md", FIXTURE)
    _, _, rc = _cli(td, "--log", "docs/STATUS-log.md", "--apply")
    return rc == 0 and (td / "docs" / "STATUS-log" / "2026-02.md").is_file()


def _case_log_without_extension_needs_archive_dir(td: Path) -> bool:
    _put(td / "LOG", FIXTURE)
    before = _tree(td)
    _, err, rc = _cli(td, "--log", "LOG", "--apply")
    return rc == 2 and "--archive-dir" in err and _tree(td) == before


def _case_bad_date_exits_2(td: Path) -> bool:
    _layout(td)
    before = _tree(td)
    _, err, rc = _cli(td, "--apply", "--date", "yesterday")
    return rc == 2 and "--date" in err and _tree(td) == before


def _case_apply_and_verify_exclusive(td: Path) -> bool:
    _layout(td)
    _, _, rc = _cli(td, "--apply", "--verify")
    return rc == 2


def _case_verify_never_writes(td: Path) -> bool:
    _layout(td)
    before = _tree(td)
    _, _, rc = _cli(td, "--verify")
    return rc == 0 and _tree(td) == before


def _case_already_archived_block_not_duplicated(td: Path) -> bool:
    # RED ARM: a month already in its file (an earlier run died before the log was rewritten)
    # is not archived twice, even when the file has grown past it.
    lay = _layout(td)
    block = "## 2026-02-20 \u2014 february entry\n\nfebruary body\n"
    held = block + "\n## 2026-02-10 \u2014 earlier february\n\nearlier body\n"
    _put(lay.archive / "2026-02.md", held)
    rc = _quiet(do_apply, lay, DATE)
    return rc == 0 and _get(lay.archive / "2026-02.md") == held and "february entry" not in _get(lay.log)


def _case_partial_archive_completed(td: Path) -> bool:
    # RED ARM: a month file holding only the start of the month is completed, not prepended to.
    lay = _layout(td)
    block = "## 2026-02-20 \u2014 february entry\n\nfebruary body\n"
    _put(lay.archive / "2026-02.md", "## 2026-02-20 \u2014 february entry\n")
    rc = _quiet(do_apply, lay, DATE)
    return rc == 0 and _get(lay.archive / "2026-02.md") == block


def _case_verify_catches_preamble_in_month_file(td: Path) -> bool:
    # RED ARM: text before the first heading of a month file fails verify.
    lay = _layout(td)
    _quiet(do_apply, lay, DATE)
    _put(lay.archive / "2026-02.md", "stray words\n" + _get(lay.archive / "2026-02.md"))
    _, err, rc = _cli(td, "--verify")
    return rc == 1 and "2026-02.md: text before its first dated heading" in err


def _recording_writes(fn: Callable[[], int]) -> Tuple[List[str], Optional[BaseException]]:
    """Run fn with _write replaced by a recorder that still writes; return the paths in order."""
    real = globals()["_write"]
    seen: List[str] = []

    def spy(path: Path, text: str) -> None:
        seen.append(path.name)
        real(path, text)

    globals()["_write"] = spy
    try:
        fn()
        return seen, None
    except BaseException as exc:  # reported by the caller
        return seen, exc
    finally:
        globals()["_write"] = real


def _case_log_written_last(td: Path) -> bool:
    # RED ARM: the log is rewritten after the month files, so a crash leaves a re-runnable state.
    lay = _layout(td)
    seen, exc = _recording_writes(lambda: _quiet(do_apply, lay, DATE))
    return exc is None and seen == ["2026-02.md", "STATUS-log.md"]


def _case_rerun_writes_no_file(td: Path) -> bool:
    lay = _layout(td)
    _quiet(do_apply, lay, DATE)
    seen, exc = _recording_writes(lambda: _quiet(do_apply, lay, DATE))
    return exc is None and seen == []


def _case_post_write_check(td: Path) -> bool:
    # RED ARM: if what lands on disk is not what was planned, apply says so instead of reporting success.
    lay = _layout(td)
    real = globals()["_write"]
    globals()["_write"] = lambda path, text: real(path, text[:-1])  # a write that loses its last byte
    try:
        try:
            _quiet(do_apply, lay, DATE)
        except Refusal as exc:
            return "post-write check FAILED" in str(exc)
        return False
    finally:
        globals()["_write"] = real


def _case_archive_dir_is_a_file_refused(td: Path) -> bool:
    # RED ARM: an archive directory that is a file is refused, and nothing is written.
    lay = _layout(td)
    _put(td / "docs" / "STATUS-log", "i am a file\n")
    before = _tree(td)
    _, err, rc = _cli(td, "--apply")
    return rc == 1 and "not a directory" in err and _tree(td) == before


_CASES: List[Tuple[str, Callable[[Path], bool]]] = [
    ("plain-split-byte-exact", _case_plain_split),
    ("idempotent-rerun-changes-nothing", _case_idempotent),
    ("concurrent-append-folded-in-archive-untouched", _case_concurrent_append),
    ("month-rollover-archives-the-finished-month", _case_month_rollover),
    ("late-entry-for-archived-month-goes-to-its-file", _case_stray_old_entry_goes_to_its_month),
    ("plan-really-moves-content", _case_plan_moves_content),
    ("already-archived-block-not-duplicated", _case_already_archived_block_not_duplicated),
    ("partial-month-file-is-completed", _case_partial_archive_completed),
    ("log-written-after-the-month-files", _case_log_written_last),
    ("rerun-writes-no-file", _case_rerun_writes_no_file),
    ("post-write-check-catches-a-lost-write", _case_post_write_check),
    ("archive-dir-that-is-a-file-refused", _case_archive_dir_is_a_file_refused),
    ("interleaved-months-refused-by-name", _case_interleaved_refused),
    ("oldest-first-log-refused", _case_oldest_first_refused),
    ("no-dated-heading-refused", _case_no_heading_refused),
    ("refusal-writes-nothing", _case_refusal_writes_nothing),
    ("unbalanced-pointer-markers-refused", _case_unbalanced_pointer_refused),
    ("dry-run-writes-nothing", _case_dry_run_writes_nothing),
    ("apply-then-verify", _case_cli_apply_then_verify),
    ("verify-catches-a-foreign-month-in-a-month-file", _case_verify_catches_foreign_month),
    ("verify-catches-a-month-in-log-and-archive", _case_verify_catches_month_in_both),
    ("verify-catches-misordered-months", _case_verify_catches_misordered_months),
    ("verify-catches-text-before-a-month-files-first-heading", _case_verify_catches_preamble_in_month_file),
    ("verify-never-writes", _case_verify_never_writes),
    ("missing-log-exits-2", _case_missing_log_exits_2),
    ("crlf-line-endings-kept", _case_crlf_kept),
    ("non-utf8-bytes-kept", _case_non_utf8_kept),
    ("only-newline-breaks-a-line", _case_only_newline_breaks_lines),
    ("stray-files-in-archive-dir-ignored", _case_stray_archive_files_ignored),
    ("log-and-archive-dir-flags-relative-pointer", _case_flags_and_relative_pointer),
    ("default-archive-dir-from-log-name", _case_default_archive_dir_from_log_name),
    ("log-without-extension-needs-archive-dir", _case_log_without_extension_needs_archive_dir),
    ("bad-date-exits-2", _case_bad_date_exits_2),
    ("apply-and-verify-are-exclusive", _case_apply_and_verify_exclusive),
]


def _run_self_test() -> int:
    """Run every case in its own temporary directory; print PASS/FAIL per case."""
    n_pass = n_fail = 0
    for name, fn in _CASES:
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
    print(f"{PROG}: self-test PASS -- {n_pass} of {ran} cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
