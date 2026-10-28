#!/usr/bin/env python3
"""Read what limits each neighbour and whether it is busy. Python 3.8+, standard library only.

Usage: neighbour-check.py [--root DIR] [--doc PATH] [--only NAMES] [--config FILE] [--burn show|hide] [--json]
       neighbour-check.py record --weekly PCT --weekly-reset ISO [--five PCT --five-reset ISO] [--session TEXT]
                          [--root DIR] [--doc PATH] [--config FILE]
       neighbour-check.py --self-test

The neighbours are the rows of the neighbours doc (the allow list `neighbours.py` reads). The list is found the
same way: --doc; the `neighbours:` line of pitmaster.md in --root when it names a path; then
~/.pitmaster/neighbours.md. A `neighbours:` line that names neighbours, and --only, narrow it; they never add one.

For each neighbour it prints a `## Neighbours` block line: a verdict, what limits it (a usage meter, a quota, its
reset), how fast it is being spent (burn) and what it is doing now. A neighbour gets one or more probes, chosen by
the kinds codex, agy, go, claude, ollama, openai, lanes and guard: guessed from its row, or named by a
`kind <neighbour>:` setting. A neighbour no probe matches is listed on a `Not checked:` line. Every probe reads a
file, lists processes, asks an HTTP model list, or runs a check the settings name; none writes. A probe that
raises reports its neighbour down, never skipped.

Verdicts: closed, tight, on pace, spare and unknown for a neighbour with a usage meter; free, busy, up and down for
one without. A neighbour's verdict is the worst of its facts', worst first: closed, tight, busy, down, unknown,
on pace, free, up, spare. The limits that set them (95 % used is closed; a pace that reaches 100 % by the window's
reset is tight; under 80 % projected is spare) are provisional.

Settings are `key: value` lines, read in this order, later wins: the built-in defaults; --config FILE (the first
fenced block under its `## Config` heading, else its key lines outside fences); the neighbours doc's own
`## neighbour-check` section; pitmaster.md (keys this tool does not know are ignored there); the flags. The README
lists every key. An unknown key in the first two files is a note, never a failure.

`record` appends one line to the readings file (the `meters` setting): a Claude account's weekly usage, so the
other accounts' runs can see it. It is the only write; it never edits an existing line.

NEIGHBOUR_CHECK_BREAK=1 plants the self-test's defect in the real pace function, so --self-test must exit 1 under
it: the red arm is walked.

Exit 0: the block was written and the doc is sound; 1: no doc, or findings in it (the block is still printed), or a
self-test case failed; 2: invalid usage or unreadable input.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import io
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

PROG = "neighbour-check"
KINDS = ("codex", "agy", "go", "claude", "ollama", "openai", "lanes", "guard")
RANK = ("closed", "tight", "busy", "down", "unknown", "on pace", "free", "up", "spare")
FRESH_H = 12            # an open now.tsv row older than this is a crashed session
ACTIVE_MIN = 15         # a Codex rollout written this recently is a session at work
TREE_MIN = 30           # an agy clone touched this recently is a dispatch at work
OLD_READING_H = 6       # a meter reading older than this is printed with its age
SPARE_BELOW = 80.0      # projected use at the window's reset under this is "spare"
CLOSED_AT = 95.0        # used at or over this is "closed"
HEARTBEAT_STALE_S = 60       # a lanes daemon heartbeat older than this is DOWN
METERS_HEAD = "utc\tlabel\taccount\tweekly_pct\tweekly_reset\tfive_pct\tfive_reset\tsession"
BARE_ADDRESS = re.compile(r"(?<![\w./:-])([A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)*):(\d{2,5})\b")
SETTING = re.compile(r"^\s*([A-Za-z_][\w-]*)(?:[ \t]+([^\s:]+))?\s*:\s*(.*)$")

# --- shed:shared neighbours-doc begin ---
DEFAULT_DOC = pathlib.Path("~") / ".pitmaster" / "neighbours.md"
COLUMNS = ("name", "how to reach it", "family", "cost", "streams", "live")
COSTS = ("free", "plan", "paid")
YES_NO = ("yes", "no")
DELIVERIES = ("all at once", "streamed", "hybrid")
LIVE_DEFAULTS = {"delivery": "hybrid", "filibuster": "1 sentence"}
HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
SEPARATOR = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
COMMENT = re.compile(r"<!--.*?-->", re.S)
URL = re.compile(r"\b(https?)://([A-Za-z0-9.\-]+|\[[0-9A-Fa-f:]+\])(?::(\d+))?")
PORT = re.compile(r"\bport\s+(\d{1,5})\b", re.I)


class Neighbour:
    def __init__(self, line: int, values: Dict[str, str]) -> None:
        self.line = line
        self.name = values.get("name", "")
        self.reach = values.get("how to reach it", "")
        self.family = values.get("family", "")
        self.cost = values.get("cost", "")
        self.streams = values.get("streams", "")
        self.live = values.get("live", "")
        self.seat = False
        self.now = "not probed"
        self.address: Optional[Tuple[str, int]] = None

    def as_dict(self) -> Dict[str, object]:
        return {"name": self.name, "reach": self.reach, "family": self.family, "cost": self.cost,
                "streams": self.streams, "live": self.live, "seat": self.seat, "now": self.now,
                "address": "{}:{}".format(*self.address) if self.address else None}


def cells(line: str) -> List[str]:
    """Split one table row on unescaped pipes, dropping the outer ones."""
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", body)]


def unquote(value: str) -> str:
    value = re.sub(r"\s+#.*$", "", value).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1].strip()
    return value


def names(value: str) -> List[str]:
    return [n.strip() for n in value.split(",") if n.strip()]


def is_path(value: str) -> bool:
    return value.startswith("~") or "/" in value or "\\" in value or value.lower().endswith(".md")


def project_line(root: pathlib.Path) -> Optional[str]:
    """The value of pitmaster.md's `neighbours:` line, or None."""
    config = root / "pitmaster.md"
    if not config.is_file():
        return None
    fence = ""
    for line in config.read_bytes().decode("utf-8").splitlines():
        mark = FENCE.match(line)
        if mark:
            fence = "" if fence else mark.group(1)
            continue
        found = re.match(r"^neighbours:\s*(.*)$", line)
        if found and not fence:
            return unquote(found.group(1))
    return None


def doc_lines(text: str) -> List[Tuple[int, str]]:
    """The doc's lines with their numbers, comments blanked and fenced blocks dropped."""
    text = COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    out: List[Tuple[int, str]] = []
    fence = ""
    for number, line in enumerate(text.splitlines(), start=1):
        mark = FENCE.match(line)
        if fence:
            if mark and mark.group(1)[0] == fence[0] and len(mark.group(1)) >= len(fence):
                fence = ""
            continue
        if mark:
            fence = mark.group(1)
            continue
        out.append((number, line.rstrip()))
    return out


def parse(path: pathlib.Path) -> Tuple[List[Neighbour], Optional[List[str]], Dict[str, str], List[str]]:
    """Return (neighbours, seats or None, live settings, findings) for one doc."""
    lines = doc_lines(path.read_bytes().decode("utf-8"))
    where = path.name
    found: List[str] = []
    listed: List[Neighbour] = []
    seats: Optional[List[str]] = None
    live: Dict[str, str] = {}
    table_seen = False
    section = ""
    i = 0
    while i < len(lines):
        number, line = lines[i]
        heading = HEADING.match(line)
        if heading:
            section = heading.group(2).strip().lower()
            i += 1
            continue
        seat_line = re.match(r"^\s*\**seats\**:\**\s*(.*)$", line, re.I)
        if seat_line:
            seats = names(seat_line.group(1))
            i += 1
            continue
        setting = re.match(r"^\s*(delivery|filibuster):\s*(.*)$", line, re.I)
        if setting and section == "live":
            live[setting.group(1).lower()] = unquote(setting.group(2))
            i += 1
            continue
        if not (line.lstrip().startswith("|") and i + 1 < len(lines) and SEPARATOR.match(lines[i + 1][1])):
            i += 1
            continue
        header = [c.lower() for c in cells(line)]
        i += 2
        rows: List[Tuple[int, str]] = []
        while i < len(lines) and lines[i][1].lstrip().startswith("|"):
            rows.append(lines[i])
            i += 1
        if table_seen or not header or header[0] != "name" or "how to reach it" not in header:
            continue
        table_seen = True
        for row_number, raw in rows:
            row = cells(raw)
            if len(row) != len(header):
                found.append("{}:{}: row has {} cells, the header has {}".format(
                    where, row_number, len(row), len(header)))
                continue
            values = {h: v for h, v in zip(header, row) if h in COLUMNS}
            listed.append(Neighbour(row_number, values))
    if not table_seen:
        found.append("{}: no neighbours table (a table whose header starts | name | how to reach it |)".format(where))
    seen: Dict[str, int] = {}
    for n in listed:
        label = "{}:{}: {}".format(where, n.line, n.name or "(no name)")
        if not n.name:
            found.append("{}:{}: a neighbour has no name".format(where, n.line))
        elif n.name.lower() in seen:
            found.append("{} is listed twice (first on line {})".format(label, seen[n.name.lower()]))
        else:
            seen[n.name.lower()] = n.line
        if not n.reach:
            found.append("{}: no way to reach it".format(label))
        if n.cost and n.cost.lower() not in COSTS:
            found.append("{}: cost \"{}\" is not one of {}".format(label, n.cost, ", ".join(COSTS)))
        for column, value in (("streams", n.streams), ("live", n.live)):
            if value and value.lower() not in YES_NO:
                found.append("{}: {} \"{}\" is not yes or no".format(label, column, value))
    for seat in seats or []:
        if seat.lower() not in seen:
            found.append("{}: Seats names {}, which the list does not hold".format(where, seat))
    delivery = live.get("delivery", "")
    if delivery and delivery.lower() not in DELIVERIES:
        found.append("{}: delivery \"{}\" is not one of {}".format(where, delivery, ", ".join(DELIVERIES)))
    settings = dict(LIVE_DEFAULTS)
    settings.update({k: v for k, v in live.items() if v})
    return listed, seats, settings, found


def address(reach: str) -> Optional[Tuple[str, int]]:
    """The host and port a "how to reach it" cell names, or None."""
    url = URL.search(reach)
    if url:
        host = url.group(2).strip("[]")
        port = int(url.group(3)) if url.group(3) else (443 if url.group(1) == "https" else 80)
        return host, port
    port = PORT.search(reach)
    if port:
        return "127.0.0.1", int(port.group(1))
    return None


def answers(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def read(root: pathlib.Path, doc: Optional[str], only: Optional[List[str]],
         probe: bool, timeout: float) -> Tuple[int, Dict[str, object]]:
    """Gather everything a run reports. Raises OSError, UnicodeError or ValueError on bad input."""
    if not root.is_dir():
        raise ValueError("--root must name an existing directory")
    line = project_line(root)
    narrow: Optional[List[str]] = None
    source = "default"
    if doc is not None:
        path = pathlib.Path(doc).expanduser()
        source = "--doc"
    elif line and is_path(line):
        path = pathlib.Path(line).expanduser()
        if not path.is_absolute():
            path = root / path
        source = "pitmaster.md"
    else:
        path = DEFAULT_DOC.expanduser()
    if line and not is_path(line):
        narrow = names(line)
    report: Dict[str, object] = {"doc": str(path), "source": source, "narrowed_to": None, "neighbours": [],
                                 "seats": None, "live": dict(LIVE_DEFAULTS), "notes": [], "findings": []}
    if not path.is_file():
        report["findings"] = ["{}: no neighbours doc here; a townhall finds no outside neighbour".format(path)]
        return 1, report
    listed, seats, live, found = parse(path)
    for label, wanted in (("pitmaster.md", narrow), ("--only", only)):
        if wanted is None:
            continue
        held = {n.name.lower() for n in listed if n.name}
        for name in wanted:
            if name.lower() not in held:
                found.append("{} names {}, which the list does not hold{}".format(
                    label, name, "" if label == "pitmaster.md" else " here"))
        keep = {w.lower() for w in wanted}
        listed = [n for n in listed if n.name.lower() in keep]
        narrow = [n.name for n in listed]
        report["narrowed_to"] = narrow
    notes: List[str] = []
    seat_names = {s.lower() for s in seats or []}
    for n in listed:
        n.seat = n.name.lower() in seat_names
        n.address = address(n.reach)
        if not probe:
            continue
        if n.address is None:
            n.now = "not checked (reached through an agent's tool)"
        elif answers(n.address[0], n.address[1], timeout):
            n.now = "answers"
        else:
            n.now = "no answer"
    if seats is None:
        notes.append("no Seats line yet: the first townhall asks who sits and writes it")
    if live["delivery"].lower() == "streamed":
        for n in listed:
            if n.seat and n.streams.lower() != "yes":
                notes.append("delivery is streamed, so {} (streams: {}) cannot sit live".format(
                    n.name, n.streams or "unset"))
    report.update({"neighbours": [n.as_dict() for n in listed], "seats": seats, "live": live,
                   "notes": notes, "findings": found})
    return (1 if found else 0), report
# --- shed:shared neighbours-doc end ---


# ---------------------------------------------------------------- shapes

@dataclass
class Fact:
    """One probe's finding about one neighbour. A fact with no verdict does not change the neighbour's."""
    kind: str
    verdict: Optional[str] = None
    meter: str = ""                  # what limits it, one clause
    busy: str = ""                   # what it is doing now, one clause
    burn: str = ""                   # how fast it is being spent; hidden by `burn: hide`
    ask: Optional[str] = None        # the reading the user must give when none can be read
    data: Dict[str, object] = field(default_factory=dict)


@dataclass
class Reading:
    name: str
    kinds: List[str]
    verdict: str
    facts: List[Fact]


Proc = Tuple[int, str, List[str]]


@dataclass
class Ctx:
    """Everything a probe touches outside its own arguments, so a test can stand in for each."""
    now: datetime
    env: Dict[str, str]
    home: str
    os_name: str
    root: str
    procs: Callable[[], Optional[List[Proc]]]            # None: processes cannot be listed here
    http: Callable[[str], Optional[Dict[str, object]]]   # GET a URL, parse JSON; None on any failure
    ssh: Callable[[str, str], Tuple[int, str]]           # (destination, command) -> (rc, output)
    run: Callable[[List[str], float], Tuple[int, str]]   # (argv, timeout) -> (rc, stdout)
    which: Callable[[str], Optional[str]]
    alive: Callable[[int], bool]


# ---------------------------------------------------------------- time and words

def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fmt_utc(dt: datetime) -> str:
    u = dt.astimezone(timezone.utc)
    return "{:%a} {} {:%b %H:%M} UTC".format(u, u.day, u)


def fmt_hours(h: float) -> str:
    if h >= 48:
        return "{:.1f} d".format(h / 24)
    if h >= 1:
        return "{:.0f} h".format(h)
    return "{:.0f} min".format(max(h * 60, 1))


def plural(n: int, word: str) -> str:
    return "{} {}".format(n, word) if n == 1 else "{} {}{}".format(n, word, "es" if word.endswith("s") else "s")


def expand(path: str, home: str) -> str:
    if path == "~" or path.startswith(("~/", "~\\")):
        return os.path.join(home, path[2:]) if len(path) > 1 else home
    return path


def strip_dot(text: str) -> str:
    return text.rstrip().rstrip(".")


def worst(words: Iterable[str]) -> str:
    found = [w for w in words if w in RANK]
    return min(found, key=RANK.index) if found else "unknown"


# ---------------------------------------------------------------- settings

class Settings:
    """The merged settings and where each value came from."""

    SCALARS = ("name", "burn", "probes", "meters", "now", "codex_home", "codex_trees", "agy_trees", "opencode",
               "go_limits", "lane_status", "lane_holds", "lane_switch")

    def __init__(self, env: Dict[str, str]) -> None:
        self.values: Dict[str, str] = {
            "name": "the user", "burn": "show", "probes": ", ".join(KINDS),
            "meters": env.get("METERS_TSV") or "~/.claude/meters.tsv",
            "now": env.get("NOW_TSV") or "~/.claude/now.tsv",
            "codex_home": env.get("CODEX_HOME") or "~/.codex", "codex_trees": "", "agy_trees": "",
            "opencode": "", "go_limits": "12, 30, 60", "lane_status": "lanes/queue/STATUS.json",
            "lane_holds": "", "lane_switch": ""}
        self.origin: Dict[str, str] = {k: "default" for k in self.values}
        self.kinds: Dict[str, Tuple[str, str]] = {}      # neighbour (lower case) -> (value, origin)
        self.guards: Dict[str, Tuple[str, str]] = {}
        self.notes: List[str] = []

    def apply(self, pairs: Iterable[Tuple[str, Optional[str], str]], where: str, label: str, strict: bool) -> None:
        """Merge (key, neighbour, value) settings read from `label`. `strict` notes keys this tool does not know."""
        for key, who, value in pairs:
            key = key.lower()
            if key in ("kind", "guard") and who:
                if key == "kind" and not self._kinds_ok(value, label):
                    continue
                (self.kinds if key == "kind" else self.guards)[who.lower()] = (value, where)
            elif key in self.SCALARS and not who:
                if key == "burn" and value.lower() not in ("show", "hide"):
                    self.notes.append("burn must be show or hide in {}, ignored".format(label))
                    continue
                if key == "go_limits" and self.limits(value) is None:
                    self.notes.append("go_limits must be three numbers in {}, ignored".format(label))
                    continue
                if key == "probes" and not self._kinds_ok(value, label, allow_none=False):
                    continue
                self.values[key] = value.lower() if key == "burn" else value
                self.origin[key] = where
            elif strict:
                self.notes.append("unknown setting {} in {}, ignored".format(
                    key + (" " + who if who else ""), label))

    def _kinds_ok(self, value: str, label: str, allow_none: bool = True) -> bool:
        bad = [k for k in names(value.lower()) if k not in KINDS and not (allow_none and k == "none")]
        if bad:
            self.notes.append("unknown kind {} in {}, ignored".format(", ".join(bad), label))
        return not bad

    @staticmethod
    def limits(value: str) -> Optional[Tuple[float, float, float]]:
        try:
            parts = [float(x) for x in names(value)]
        except ValueError:
            return None
        return (parts[0], parts[1], parts[2]) if len(parts) == 3 and all(p > 0 for p in parts) else None

    def probes(self) -> List[str]:
        return [k for k in names(self.values["probes"].lower()) if k in KINDS]

    def path(self, key: str, home: str) -> str:
        return expand(self.values[key], home) if self.values[key] else ""

    def as_dict(self) -> Dict[str, object]:
        out: Dict[str, object] = {k: {"value": self.values[k], "from": self.origin[k]} for k in self.SCALARS}
        for key, table in (("kind", self.kinds), ("guard", self.guards)):
            for who, (value, where) in sorted(table.items()):
                out["{} {}".format(key, who)] = {"value": value, "from": where}
        return out


def setting_lines(lines: Iterable[str]) -> List[Tuple[str, Optional[str], str]]:
    out: List[Tuple[str, Optional[str], str]] = []
    for line in lines:
        m = SETTING.match(line)
        if m:
            value = m.group(3)
            # `key:   # comment` is an empty value; unquote only drops a comment that follows a value.
            out.append((m.group(1), m.group(2), "" if value.startswith("#") else unquote(value)))
    return out


def config_pairs(text: str) -> List[Tuple[str, Optional[str], str]]:
    """The key lines of a --config file: the first fenced block under `## Config`, else every line outside fences."""
    block = re.search(r"^## Config\b[^\n]*\n.*?^(`{3,}|~{3,})[^\n]*\n(.*?)^\1", text, re.S | re.M)
    if block:
        return setting_lines(block.group(2).splitlines())
    if text.startswith("---"):
        end = re.search(r"^---[ \t]*\r?$", text[3:], re.M)
        if end:
            text = text[3 + end.end():]
    return setting_lines(line for _n, line in doc_lines(text))


def section_pairs(text: str, title: str) -> List[Tuple[str, Optional[str], str]]:
    """Key lines from the heading `title` to the next heading of the same or a higher level."""
    lines: List[str] = []
    level = 0
    for _n, line in doc_lines(text):
        heading = HEADING.match(line)
        if heading:
            if level:
                if len(heading.group(1)) <= level:
                    break
            elif heading.group(2).strip().lower() == title:
                level = len(heading.group(1))
            continue
        if level:
            lines.append(line)
    return setting_lines(lines)


def read_text(path: str) -> str:
    return pathlib.Path(path).read_bytes().decode("utf-8")


def load_settings(root: pathlib.Path, report: Dict[str, object], config: Optional[str], burn: Optional[str],
                  env: Dict[str, str]) -> Settings:
    """Raises OSError or UnicodeError when --config cannot be read."""
    s = Settings(env)
    if config is not None:
        s.apply(config_pairs(read_text(config)), "--config", os.path.basename(config), True)
    doc = pathlib.Path(str(report["doc"]))
    if doc.is_file():
        try:
            s.apply(section_pairs(read_text(str(doc)), "neighbour-check"), "doc", doc.name, True)
        except (OSError, UnicodeError):
            pass
    project = root / "pitmaster.md"
    if project.is_file():
        try:
            s.apply(setting_lines(line for _n, line in doc_lines(read_text(str(project)))), "pitmaster.md",
                    "pitmaster.md", False)
        except (OSError, UnicodeError):
            pass
    if burn:
        s.values["burn"] = burn
        s.origin["burn"] = "--burn"
    return s


# ---------------------------------------------------------------- verdicts

def verdict(used: float, reached: bool, hours_left: float, rate_per_h: Optional[float]) -> Tuple[str, Optional[float]]:
    """(word, hours until empty at that pace). The pace projects use at the window's reset."""
    if reached or used >= CLOSED_AT:
        return "closed", 0.0
    rate = rate_per_h if rate_per_h and rate_per_h > 0 else 0.0
    empty_h = (100.0 - used) / rate if rate else None
    projected = used + rate * max(hours_left, 0.0)
    if projected >= 100.0:
        return "tight", empty_h
    if projected < SPARE_BELOW:
        return "spare", empty_h
    return "on pace", empty_h


# ---------------------------------------------------------------- processes

def parse_ps(lines: Iterable[str]) -> List[Proc]:
    out: List[Proc] = []
    for line in lines:
        f = line.split(None, 2)
        if len(f) == 3 and f[0].isdigit():
            out.append((int(f[0]), f[1], f[2].split()))
    return out


def prog_name(word: str) -> str:
    base = os.path.basename(word.replace("\\", "/")).lower()
    return base[:-4] if base.endswith((".exe", ".cmd")) else base


def jobs_of(procs: Sequence[Proc], tool: str, sub: str, not_after: Sequence[str] = ()) -> List[Proc]:
    """Processes whose first or second word is `tool` and whose command carries the subcommand `sub`."""
    out = []
    for pid, et, argv in procs:
        for i, a in enumerate(argv[:2]):
            if prog_name(a) == tool:
                rest = argv[i + 1:]
                if sub in rest and not any(x in rest for x in not_after):
                    out.append((pid, et, argv))
                break
    return out


def procs_clause(procs: Optional[Sequence[Proc]], count: int, what: str) -> str:
    return plural(count, what) if procs is not None else "processes not listed here"


# ---------------------------------------------------------------- Codex

@dataclass
class Rate:
    ts: float
    used: float
    resets_at: int
    window_min: int
    plan: str
    reached: bool
    second_used: Optional[float] = None
    second_resets_at: Optional[int] = None


@dataclass
class Sess:
    path: str
    mtime: float
    originator: str
    cwd: str
    tokens: int


def scan_rollout(lines: Iterable[str], path: str = "", mtime: float = 0.0) -> Tuple[List[Rate], Optional[Sess]]:
    """One pass over a Codex rollout: its rate-limit readings and the session's total tokens."""
    rates: List[Rate] = []
    originator, cwd, tokens = "?", "", 0
    for i, line in enumerate(lines):
        if i == 0 and '"session_meta"' in line:
            try:
                p = json.loads(line)["payload"]
                originator, cwd = str(p.get("originator") or "?"), str(p.get("cwd") or "")
            except (ValueError, KeyError, TypeError):
                pass
            continue
        if '"token_count"' not in line:
            continue
        try:
            d = json.loads(line)
            pay = d["payload"]
            info = pay.get("info") or {}
            tokens = int((info.get("total_token_usage") or {}).get("total_tokens") or tokens)
            rl = pay.get("rate_limits")
            if not rl:
                continue
            prim = rl.get("primary") or {}
            if prim.get("used_percent") is None or prim.get("resets_at") is None:
                continue
            sec = rl.get("secondary") or {}
            rates.append(Rate(
                ts=parse_iso(str(d["timestamp"])).timestamp(),
                used=float(prim["used_percent"]),
                resets_at=int(prim["resets_at"]),
                window_min=int(prim.get("window_minutes") or 0),
                plan=str(rl.get("plan_type") or "?"),
                reached=bool(rl.get("rate_limit_reached_type")),
                second_used=float(sec["used_percent"]) if sec.get("used_percent") is not None else None,
                second_resets_at=int(sec["resets_at"]) if sec.get("resets_at") is not None else None,
            ))
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    sess = Sess(path, mtime, originator, cwd, tokens) if path else None
    return rates, sess


def same_window(a: int, b: int) -> bool:
    return abs(a - b) <= 60      # one window's resets_at jitters by a few seconds between processes


@dataclass
class Pace:
    used: float
    ts: float
    resets_at: int
    window_min: int
    plan: str
    reached: bool
    gains: Dict[int, Tuple[float, float]]   # lookback hours -> (points gained, hours actually spanned)
    second_used: Optional[float] = None
    second_resets_at: Optional[int] = None


def codex_pace(rates: Sequence[Rate]) -> Optional[Pace]:
    """The newest window's reading and its gain over the last 24 h and 3 h, within that window only."""
    if not rates:
        return None
    newest = max(rates, key=lambda r: r.ts)
    win = [r for r in rates if same_window(r.resets_at, newest.resets_at)]
    if os.environ.get("NEIGHBOUR_CHECK_BREAK") == "1":
        win = list(rates)        # the planted defect: an earlier window's readings leak into the pace
    cur = max(r.used for r in win if r.ts >= newest.ts - 1800)
    gains: Dict[int, Tuple[float, float]] = {}
    for hours in (24, 3):
        target = newest.ts - hours * 3600
        before = [r for r in win if r.ts <= target]
        base = max(before, key=lambda r: r.ts) if before else min(win, key=lambda r: r.ts)
        span = (newest.ts - base.ts) / 3600
        if span >= 0.25:
            gains[hours] = (cur - base.used, span)
    return Pace(cur, newest.ts, newest.resets_at, newest.window_min, newest.plan, newest.reached, gains,
                newest.second_used, newest.second_resets_at)


def project_label(cwd: str, home: str, trees: str = "") -> str:
    """A working folder's project: a worktree's parent folder, the dispatch-trees folder, scratch, or the first
    folder under Documents (else under home)."""
    c = cwd.replace("\\", "/")
    h = home.replace("\\", "/").rstrip("/")
    t = trees.replace("\\", "/").rstrip("/")
    marker = "/.claude/worktrees/"
    if marker in c:
        return os.path.basename(c.split(marker)[0]) or "?"
    if t and (c == t or c.startswith(t + "/")):
        return os.path.basename(t)
    if "/scratchpad" in c or c.startswith(("/private/", "/tmp/", "/var/")):
        return "scratch"
    rel = c[len(h) + 1:] if h and c.startswith(h + "/") else c.lstrip("/")
    parts = [p for p in rel.split("/") if p]
    if parts[:1] == ["Documents"] and len(parts) > 1:
        return parts[1]
    return parts[0] if parts else "?"


def probe_codex(s: Settings, ctx: Ctx, procs: Optional[Sequence[Proc]]) -> Fact:
    now = ctx.now
    cut = now.timestamp() - 8 * 86400
    sessions_dir = os.path.join(s.path("codex_home", ctx.home), "sessions")
    trees = s.path("codex_trees", ctx.home)
    rates: List[Rate] = []
    sessions: List[Sess] = []
    for root, _dirs, files in os.walk(sessions_dir):
        for fn in files:
            if not (fn.startswith("rollout-") and fn.endswith(".jsonl")):
                continue
            path = os.path.join(root, fn)
            try:
                mt = os.path.getmtime(path)
                if mt < cut:
                    continue
                with open(path, encoding="utf-8", errors="replace") as fh:
                    r, sess = scan_rollout(fh, path, mt)
            except OSError:
                continue
            rates.extend(r)
            if sess:
                sessions.append(sess)
    jobs = jobs_of(procs, "codex", "exec", ("app-server", "exec-server")) if procs is not None else []
    open_trees: List[str] = []
    if trees:
        try:
            for d in sorted(os.listdir(trees)):
                full = os.path.join(trees, d)
                if os.path.exists(os.path.join(full, "started")) and not os.path.exists(os.path.join(full, "finished")):
                    open_trees.append(d)
        except OSError:
            pass
    active = [x for x in sessions if x.mtime >= now.timestamp() - ACTIVE_MIN * 60]
    act_by: Dict[str, int] = {}
    for x in active:
        lab = project_label(x.cwd, ctx.home, trees)
        act_by[lab] = act_by.get(lab, 0) + 1
    busy = procs_clause(procs, len(jobs), "codex exec job") + (" running" if procs is not None else "")
    if trees:
        busy += ", {} open {} jobs".format(len(open_trees), os.path.basename(trees.rstrip("/\\")) or trees)
    busy += "; {} written in {} min".format(plural(len(active), "session"), ACTIVE_MIN)
    if act_by:
        busy += " (" + ", ".join("{} {}".format(k, v) for k, v in sorted(act_by.items(), key=lambda kv: -kv[1])) + ")"
    pace = codex_pace(rates)
    if pace is None:
        return Fact("codex", "unknown", "no rate-limit reading in its session logs for 8 days", busy,
                    ask="its weekly % (the Codex app's usage page, or /status in a Codex session)")
    reset = datetime.fromtimestamp(pace.resets_at, timezone.utc)
    hours_left = (reset - now).total_seconds() / 3600
    win_h = pace.window_min / 60 if pace.window_min else 168.0
    age_h = (now.timestamp() - pace.ts) / 3600
    by_proj: Dict[str, int] = {}
    win_start = pace.resets_at - win_h * 3600
    for x in sessions:
        if x.mtime >= win_start:
            lab = project_label(x.cwd, ctx.home, trees)
            by_proj[lab] = by_proj.get(lab, 0) + x.tokens
    top = sorted(by_proj.items(), key=lambda kv: -kv[1])[:4]
    data: Dict[str, object] = {"used": pace.used, "resets_at": reset.isoformat(), "plan": pace.plan,
                               "window_h": win_h, "reading_age_h": round(age_h, 2),
                               "gains": {str(k): [round(g, 1), round(sp, 2)] for k, (g, sp) in pace.gains.items()},
                               "tokens_by_project": dict(top), "jobs": [pid for pid, _e, _a in jobs],
                               "open_trees": open_trees}
    if hours_left <= 0:
        return Fact("codex", "unknown", "its window reset at {}, after the last reading ({:.0f} %)".format(
            fmt_utc(reset), pace.used), busy, data=data)
    meter = "{:.0f} % of its {} window ({}) as of its last call at {:%H:%M} UTC, resets {} (in {})".format(
        pace.used, fmt_hours(win_h), pace.plan, datetime.fromtimestamp(pace.ts, timezone.utc), fmt_utc(reset),
        fmt_hours(hours_left))
    if pace.second_used is not None and pace.second_resets_at:
        meter += "; second window {:.0f} %, resets {}".format(
            pace.second_used, fmt_utc(datetime.fromtimestamp(pace.second_resets_at, timezone.utc)))
    if age_h > OLD_READING_H:
        meter += "; reading {} old (it is written by each Codex call)".format(fmt_hours(age_h))
    if 24 in pace.gains:
        g, span = pace.gains[24]
        rate: Optional[float] = g / span
    else:
        elapsed = win_h - hours_left
        rate = pace.used / elapsed if elapsed > 1 else None
    word, empty_h = verdict(pace.used, pace.reached, hours_left, rate)
    burn = ", ".join("+{:.0f} points in {}".format(g, fmt_hours(sp))
                     for _h, (g, sp) in sorted(pace.gains.items(), reverse=True))
    if word == "tight" and empty_h is not None:
        burn += "; at that pace it is out in ~{}, {} before its reset".format(
            fmt_hours(empty_h), fmt_hours(hours_left - empty_h))
    if top:
        burn += "; this window's session tokens, cache reads included: " + ", ".join(
            "{} {:.1f}M".format(k, v / 1e6) for k, v in top)
    return Fact("codex", word, meter, busy, burn=burn, data=data)


# ---------------------------------------------------------------- agy

def probe_agy(s: Settings, ctx: Ctx, procs: Optional[Sequence[Proc]]) -> Fact:
    jobs = [(pid, et, a) for pid, et, a in (procs or []) if prog_name(a[0]) == "agy"
            and not (len(a) > 1 and a[1] in ("remote-control", "mic-serve"))]
    trees = s.path("agy_trees", ctx.home)
    recent: List[str] = []
    if trees:
        try:
            for d in os.listdir(trees):
                if os.path.getmtime(os.path.join(trees, d)) >= ctx.now.timestamp() - TREE_MIN * 60:
                    recent.append(d)
        except OSError:
            pass
    version = ""
    if ctx.which("agy"):
        rc, out = ctx.run(["agy", "--version"], 10)
        version = out.strip().splitlines()[0] if rc == 0 and out.strip() else ""
    busy = procs_clause(procs, len(jobs), "agy process")
    if trees:
        busy += "; {} touched in {} min ({})".format(plural(len(recent), "clone"), TREE_MIN,
                                                    os.path.basename(trees.rstrip("/\\")) or trees)
    return Fact("agy", "unknown", "agy keeps its quota to itself; it cannot be read here", busy,
                ask="its quota in the Antigravity app",
                data={"version": version, "jobs": [pid for pid, _e, _a in jobs], "recent_trees": sorted(recent)})


# ---------------------------------------------------------------- OpenCode Go

def next_monday_utc(now: datetime) -> datetime:
    u = now.astimezone(timezone.utc)
    d = (7 - u.weekday()) % 7 or 7
    return (u + timedelta(days=d)).replace(hour=0, minute=0, second=0, microsecond=0)


def probe_go(s: Settings, ctx: Ctx, procs: Optional[Sequence[Proc]]) -> Fact:
    jobs = jobs_of(procs, "opencode", "run") if procs is not None else []
    busy = procs_clause(procs, len(jobs), "opencode run job")
    limits = s.limits(s.values["go_limits"]) or (12.0, 30.0, 60.0)
    named = s.path("opencode", ctx.home)
    fallback = os.path.join(ctx.home, ".opencode", "bin", "opencode")
    binary = named or ctx.which("opencode") or (fallback if os.path.exists(fallback) else "")
    ask_bars = "the Go console's 5 h, week and month bars"
    if not binary:
        return Fact("go", "down", "opencode was not found", busy, ask=ask_bars)
    week: Dict[str, object] = {}
    today: Dict[str, object] = {}
    for days, sink in ((7, week), (0, today)):
        _rc, out = ctx.run([binary, "stats", "--days", str(days), "--models", "--json"], 45)
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                sink.update(parsed)
        except ValueError:
            pass
    if not week:
        return Fact("go", "down", "`opencode stats` did not answer", busy, ask=ask_bars)
    w = float(week.get("cost") or 0.0)      # type: ignore[arg-type]
    t = float(today.get("cost") or 0.0)     # type: ignore[arg-type]
    reset = next_monday_utc(ctx.now)
    meter = ("raw local spend ${:.2f} today, ${:.2f} in 7 days; limits ${:.0f}/5 h, ${:.0f}/week, ${:.0f}/month; "
             "raw spend understates models the pool weights above 1; week resets {}").format(
                 t, w, limits[0], limits[1], limits[2], fmt_utc(reset))
    word = "closed" if w >= limits[1] else "tight" if w >= 0.8 * limits[1] else "unknown"
    return Fact("go", word, meter, busy,
                ask=None if word == "closed" else ask_bars + " (the pool is weighted)",
                data={"raw_today": t, "raw_7d": w, "jobs": [pid for pid, _e, _a in jobs]})


# ---------------------------------------------------------------- the other Claude accounts

def account_label(env: Dict[str, str]) -> str:
    """The label a session records for its account: CLAUDE_ACCOUNT_LABEL, else the basename of
    CLAUDE_CONFIG_DIR without a leading dot, else acct- and the first four characters of
    CLAUDE_CODE_ACCOUNT_UUID, else default."""
    lab = env.get("CLAUDE_ACCOUNT_LABEL")
    if lab:
        return " ".join(lab.split())
    d = env.get("CLAUDE_CONFIG_DIR")
    if d:
        return os.path.basename(d.replace("\\", "/").rstrip("/")).lstrip(".") or "default"
    u = env.get("CLAUDE_CODE_ACCOUNT_UUID")
    return "acct-" + u[:4] if u else "default"


def account_name(reset_iso: str) -> str:
    """An account is named by the weekday its weekly limit resets."""
    return "{:%a}".format(parse_iso(reset_iso).astimezone(timezone.utc))


def read_tsv(path: str) -> List[List[str]]:
    try:
        with open(path, encoding="utf-8") as f:
            return [ln.rstrip("\r\n").split("\t") for ln in f if ln.strip()]
    except OSError:
        return []


def open_sessions(rows: Sequence[Sequence[str]], now: datetime) -> List[Sequence[str]]:
    """now.tsv rows (start, end, project, branch, account, session) with no end time that began under FRESH_H
    hours ago, one per session id: a resumed session writes a second start row under the same id."""
    out: Dict[str, Sequence[str]] = {}
    for r in rows:
        if len(r) < 6 or r[1]:
            continue
        try:
            if now - parse_iso(r[0]) <= timedelta(hours=FRESH_H):
                out[r[5]] = r            # the newest start row of each session wins
        except ValueError:
            continue
    return list(out.values())


def latest_readings(rows: Sequence[Sequence[str]]) -> Dict[str, Sequence[str]]:
    out: Dict[str, Sequence[str]] = {}
    for r in rows:
        if len(r) >= 5 and r[0] != "utc":
            if r[1] not in out or r[0] > out[r[1]][0]:
                out[r[1]] = r
    return out


def record(path: str, label: str, weekly: float, weekly_reset: str, five: Optional[float],
           five_reset: Optional[str], session: str, now: datetime) -> str:
    """Append one reading to the readings file, creating it with its header; never edits a line."""
    line = "\t".join(["{:%Y-%m-%dT%H:%MZ}".format(now.astimezone(timezone.utc)), label, account_name(weekly_reset),
                      "{:g}".format(weekly), parse_iso(weekly_reset).astimezone(timezone.utc).isoformat(),
                      "" if five is None else "{:g}".format(five),
                      parse_iso(five_reset).astimezone(timezone.utc).isoformat() if five_reset else "",
                      " ".join(session.split()) or "-"])
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", encoding="utf-8", newline="") as f:
        f.write((METERS_HEAD + "\n" if new else "") + line + "\n")
    return line


def probe_claude(s: Settings, ctx: Ctx, reach: str) -> Fact:
    now = ctx.now
    mine = account_label(ctx.env)
    meters = s.path("meters", ctx.home)
    nowtsv = s.path("now", ctx.home)
    opened = open_sessions(read_tsv(nowtsv), now)
    readings = latest_readings(read_tsv(meters))
    my_name = readings[mine][2] if mine in readings else "?"
    others = sorted({r[4] for r in opened if r[4] != mine} | {k for k in readings if k != mine})
    n_mine = sum(1 for r in opened if r[4] == mine)
    here = "this account ({}, {}): {} in {}, this one included".format(
        my_name, mine, plural(n_mine, "open session"), os.path.basename(nowtsv))
    named = [x for x in others if re.search(r"(?<![\w-])" + re.escape(x) + r"(?![\w-])", reach, re.I)]
    chosen = named or others
    if not chosen:
        return Fact("claude", "unknown",
                    "no reading from another account in {} yet".format(os.path.basename(meters)),
                    "no open sessions of another account in {}; {}".format(os.path.basename(nowtsv), here),
                    ask="the other account's weekly % (its own usage meter)",
                    data={"mine": mine, "my_name": my_name})
    lines: List[str] = []
    asks: List[str] = []
    words: List[str] = []
    data: Dict[str, object] = {"mine": mine, "my_name": my_name}
    for lab in chosen:
        sess = [r for r in opened if r[4] == lab]
        projects = sorted({os.path.basename(r[2].replace("\\", "/")) for r in sess})
        busy_l = "{} in {}".format(plural(len(sess), "open session"), os.path.basename(nowtsv))
        if sess:
            busy_l += " ({}; newest began {:%H:%M} UTC)".format(
                ", ".join(projects), max(parse_iso(r[0]) for r in sess).astimezone(timezone.utc))
        r = readings.get(lab)
        if not r:
            lines.append("{}: no reading yet; {}".format(lab, busy_l))
            asks.append("the {} account's weekly % (its own usage meter)".format(lab))
            words.append("unknown")
            continue
        try:
            read_at, name, used, reset = parse_iso(r[0]), r[2], float(r[3]), parse_iso(r[4])
        except ValueError:
            lines.append("{}: its last reading is unreadable; {}".format(lab, busy_l))
            asks.append("the {} account's weekly % (its own usage meter)".format(lab))
            words.append("unknown")
            continue
        age_h = (now - read_at).total_seconds() / 3600
        hours_left = (reset - now).total_seconds() / 3600
        if hours_left <= 0:
            lines.append("{} ({}): {:g} % at {}, but it reset at {} since; {}".format(
                name, lab, used, fmt_utc(read_at), fmt_utc(reset), busy_l))
            asks.append("the {} account's weekly % (its own usage meter)".format(name))
            words.append("unknown")
            continue
        elapsed = 168 - (reset - read_at).total_seconds() / 3600
        word, _e = verdict(used, False, hours_left, used / elapsed if elapsed > 1 else None)
        lines.append("{} ({}): {:g} % at {} ({} ago), resets {}; {}".format(
            name, lab, used, fmt_utc(read_at), fmt_hours(age_h), fmt_utc(reset), busy_l))
        words.append(word)
        data[lab] = {"name": name, "used": used, "read_at": r[0], "reset": r[4]}
    return Fact("claude", worst(words), "; ".join(lines), here, ask="; ".join(asks) if asks else None, data=data)


# ---------------------------------------------------------------- Ollama and OpenAI-wire servers

def more(items: Sequence[str], limit: int = 6) -> str:
    shown = ", ".join(items[:limit])
    return shown + (" (+{} more)".format(len(items) - limit) if len(items) > limit else "")


def base_url(reach: str, default: str) -> str:
    """The server a "how to reach it" cell names (a URL, `port N`, or host:port), else `default`."""
    url = URL.search(reach)
    if url:
        return "{}://{}{}".format(url.group(1), url.group(2), ":" + url.group(3) if url.group(3) else "")
    addr = address(reach)
    if addr is None:
        bare = BARE_ADDRESS.search(reach)
        addr = (bare.group(1), int(bare.group(2))) if bare else None
    if addr is None:
        return default
    host = "[{}]".format(addr[0]) if ":" in addr[0] else addr[0]
    return "http://{}:{}".format(host, addr[1])


def probe_ollama(ctx: Ctx, reach: str) -> Fact:
    base = base_url(reach, "http://127.0.0.1:11434")
    tags = ctx.http(base + "/api/tags")
    if tags is None:
        return Fact("ollama", "down", "no meter (local)", "Ollama is not answering at " + base)
    avail = [str(m.get("name")) for m in tags.get("models", []) if isinstance(m, dict)]  # type: ignore[union-attr]
    ps = ctx.http(base + "/api/ps") or {}
    loaded = [str(m.get("name")) for m in ps.get("models", []) if isinstance(m, dict)]  # type: ignore[union-attr]
    meter = "no meter (local); available: " + (more(avail) if avail else "no model pulled")
    word = "up"
    want = re.search(r"\bmodel\s+([\w.:/-]+)", reach, re.I)
    if want:
        model = want.group(1).rstrip(".")
        there = any(a == model or a.startswith(model + ":") for a in avail)
        meter += "; model {} is {}".format(model, "there" if there else "not pulled")
        word = "up" if there else "down"
    busy = "loaded: {} (Ollama shows no in-flight requests)".format(", ".join(loaded) if loaded else "none")
    return Fact("ollama", word, meter, busy, data={"models": avail, "loaded": loaded})


def probe_openai(ctx: Ctx, reach: str) -> Fact:
    base = base_url(reach, "http://127.0.0.1:1234")
    models = ctx.http(base + "/v1/models")
    if models is None:
        return Fact("openai", "down", "no meter (local)", "nothing answers at " + base)
    ids = [str(m.get("id")) for m in models.get("data", []) if isinstance(m, dict)]  # type: ignore[union-attr]
    meter = "serves: " + more(ids) if ids else "answers, no model resident"
    return Fact("openai", "up", meter, "", data={"models": ids})


# ---------------------------------------------------------------- guard (a read-only busy check over ssh)

def guard_word(rc: int, out: str) -> str:
    m = re.search(r"VERDICT:\s*(\S+)", out)
    if m:
        return m.group(1).upper()
    if rc == 0:
        return "FREE"
    return "UNREACHABLE" if rc == 255 else "rc {}".format(rc)


def probe_guard(ctx: Ctx, spec: Optional[str]) -> Fact:
    if not spec:
        return Fact("guard", "unknown", "no guard command set", "", ask=None)
    dest, _sep, command = spec.partition(" ")
    command = command.strip()
    if not dest or not command:
        return Fact("guard", "unknown", "the guard setting needs <ssh destination> <command>", "")
    rc, out = ctx.ssh(dest, command)
    word = guard_word(rc, out)
    verdict_word = "free" if word == "FREE" else "down" if word == "UNREACHABLE" else "busy"
    return Fact("guard", verdict_word, "guard on {}: {}".format(dest, word), "", data={"rc": rc, "word": word})


# ---------------------------------------------------------------- lanes

def hold_lines(files: Sequence[str], os_name: str, alive: Callable[[int], bool]) -> List[str]:
    """One clause per HOLD file: its first line's reason, and whether the pid it names is still alive."""
    out = []
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                first = fh.readline().strip()
        except OSError:
            first = ""
        m = re.search(r"\bpid (\d+)", first)
        tag = ""
        if m:
            pid = int(m.group(1))
            if os_name == "nt":
                tag = "; pid {} not checked here".format(pid)
            else:
                tag = "; pid {} alive".format(pid) if alive(pid) else "; pid {} GONE, a stale hold".format(pid)
        reason = re.sub(r"\s*\bpid \d+", "", first).strip()[:60] or "no reason line"
        out.append("{} ({}{})".format(os.path.basename(path), reason, tag))
    return out


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def main_checkout(root: str, ctx: Ctx) -> str:
    """The project's main checkout, also from a worktree (the parent of git's common dir); else --root."""
    rc, out = ctx.run(["git", "-C", root, "rev-parse", "--path-format=absolute", "--git-common-dir"], 5)
    common = out.strip()
    if rc == 0 and common:
        return os.path.dirname(common.rstrip("/\\")) or root
    return root


@dataclass
class LaneState:
    """What every lanes neighbour shares: the queue's status file, the switch, the daemon and the holds."""
    status: Optional[Dict[str, object]]
    switch_off: bool
    daemon_up: bool
    line: str                        # the one shared Lanes line; empty when there is no queue
    data: Dict[str, object]


def lane_state(s: Settings, ctx: Ctx) -> LaneState:
    main = main_checkout(ctx.root, ctx)
    path = os.path.join(main, s.path("lane_status", ctx.home))
    status: Optional[Dict[str, object]] = None
    try:
        parsed = json.loads(read_text(path))
        status = parsed if isinstance(parsed, dict) else None
    except (OSError, ValueError):
        status = None
    switch_line = ""
    switch_file = s.path("lane_switch", ctx.home)
    if switch_file and not os.path.isabs(switch_file):
        switch_file = os.path.join(main, switch_file)       # relative, like lane_status: from the main checkout
    if switch_file:
        try:
            for line in read_text(switch_file).splitlines():
                if line.startswith("LANES:"):
                    switch_line = line.split("(")[0].strip()[:40]
                    break
        except (OSError, UnicodeError):
            switch_line = ""
    switch_off = switch_line.upper().endswith("OFF")
    hold_files: List[str] = []
    extra = s.path("lane_holds", ctx.home)
    if extra and not os.path.isabs(extra):
        extra = os.path.join(main, extra)
    for folder in (os.path.join(main, "lanes"), extra):
        if folder and os.path.isdir(folder):
            try:
                hold_files += sorted(os.path.join(folder, n) for n in os.listdir(folder) if n.startswith("HOLD"))
            except OSError:
                pass
    holds = hold_lines(hold_files, ctx.os_name, ctx.alive)
    if status is None:
        return LaneState(None, switch_off, False, "", {"status": None, "holds": holds})
    parts: List[str] = []
    if switch_file:
        parts.append(switch_line or "switch not readable")
    daemon_up = False
    hb = str((status.get("daemon") or {}).get("heartbeat") or "")  # type: ignore[union-attr]
    try:
        stamp = datetime.fromisoformat(hb.replace("Z", "+00:00"))
        age = ctx.now.timestamp() - stamp.astimezone().timestamp()
        daemon_up = age <= HEARTBEAT_STALE_S
        parts.append("daemon {} (heartbeat {:.0f} s ago)".format("up" if daemon_up else "DOWN", age))
    except (ValueError, AttributeError):
        parts.append("daemon heartbeat unreadable")
    parts.append("owner {}".format(status.get("owner", "?")))
    queue = status.get("queue") or {}
    if isinstance(queue, dict):
        parts.append("queue {} waiting, {} running".format(queue.get("waiting", "?"), queue.get("running", "?")))
    parts.append("holds: " + (", ".join(holds) if holds else "none"))
    return LaneState(status, switch_off, daemon_up, "; ".join(parts),
                     {"status": path, "switch": switch_line, "daemon_up": daemon_up, "holds": holds})


def probe_lanes(name: str, lanes: LaneState) -> Fact:
    if lanes.status is None:
        return Fact("lanes", None, "this project has no lane queue")
    table = lanes.status.get("lanes") or {}
    entry = next((v for k, v in table.items() if str(k).lower() == name.lower()), None) \
        if isinstance(table, dict) else None
    if not isinstance(entry, dict):
        return Fact("lanes", None, "{} is not in the queue's status".format(name))
    state = str(entry.get("state") or "?")
    running = entry.get("running") or []
    jobs = len(running) if isinstance(running, list) else 0
    detail = "{}, {}".format("loaded" if entry.get("loaded") else "not loaded", plural(jobs, "job"))
    if state.lower() in ("unknown", "?") and not lanes.switch_off:
        # The queue does not know this lane's state (a remote box answers for itself): no verdict from here.
        return Fact("lanes", None, "the queue does not know lane {}'s state".format(name),
                    data={"state": state, "loaded": bool(entry.get("loaded")), "jobs": jobs})
    free = state.lower() in ("idle", "free") and not lanes.switch_off and lanes.daemon_up
    why = ""
    if lanes.switch_off:
        why += "; the lanes switch is OFF"
    if not lanes.daemon_up:
        why += "; the daemon is not up"
    return Fact("lanes", "free" if free else "busy", "no meter (local model)",
                "lane {} {} ({}){}".format(name, state.upper(), detail, why),
                data={"state": state, "loaded": bool(entry.get("loaded")), "jobs": jobs})


# ---------------------------------------------------------------- kinds

def guess_kinds(row: Dict[str, object]) -> List[str]:
    """The probe kinds a row's name, how-to-reach cell and family suggest."""
    name = str(row["name"]).lower()
    reach = str(row["reach"]).lower()
    family = str(row["family"]).lower()
    text = name + " " + reach
    found: List[str] = []
    addr = address(str(row["reach"]))
    if addr is None and "ollama" not in reach:
        bare = BARE_ADDRESS.search(str(row["reach"]))
        addr = (bare.group(1), int(bare.group(2))) if bare else None
    ollama = "ollama" in reach or (addr is not None and addr[1] == 11434)
    if "codex" in text:
        found.append("codex")
    if re.search(r"\bagy\b|antigravity", text) or name == "gemini":
        found.append("agy")
    if "opencode" in text:
        found.append("go")
    if re.search(r"\b(claude|anthropic)\b", family):
        found.append("claude")
    if ollama:
        found.append("ollama")
    if "lm studio" in reach or "openai" in reach or (addr is not None and not ollama):
        found.append("openai")
    if re.search(r"\blanes\b", reach):
        found.append("lanes")
    return found


@dataclass
class Plan:
    name: str
    row: Dict[str, object]
    kinds: List[str]              # every kind the neighbour has
    run: List[str]                # those the `probes` setting leaves on
    why: str = ""                 # why nothing runs, when nothing does


def plan_neighbours(rows: Sequence[Dict[str, object]], s: Settings) -> Tuple[List[Plan], List[str]]:
    plans: List[Plan] = []
    notes: List[str] = []
    held = {str(r["name"]).lower() for r in rows}
    for table, label in ((s.kinds, "kind"), (s.guards, "guard")):
        for who, (_value, where) in sorted(table.items()):
            if who not in held:
                notes.append("{} setting names {}, which the list does not hold; nothing is probed for it ({})".format(
                    label, who, where))
    on = s.probes()
    for row in rows:
        name = str(row["name"])
        key = name.lower()
        override = s.kinds.get(key)
        if override is not None:
            kinds = [k for k in names(override[0].lower()) if k != "none"]
            none = "none" in names(override[0].lower())
        else:
            kinds, none = guess_kinds(row), False
        if key in s.guards and not none and "guard" not in kinds:
            kinds.append("guard")
        run = [k for k in kinds if k in on]
        if not kinds:
            why = ("checks nothing: kind {}: none".format(name) if none else
                   "no probe matches its row; name one with `kind {}: <kind>`".format(name))
        elif not run:
            why = "probe off in settings: {}".format(", ".join(kinds))
        else:
            why = ""
        plans.append(Plan(name, row, kinds, run, why))
    return plans, notes


# ---------------------------------------------------------------- the readings

def run_probe(kind: str, plan: Plan, s: Settings, ctx: Ctx, procs: Optional[Sequence[Proc]],
              lanes: Optional[LaneState]) -> Fact:
    reach = str(plan.row["reach"])
    if kind == "codex":
        return probe_codex(s, ctx, procs)
    if kind == "agy":
        return probe_agy(s, ctx, procs)
    if kind == "go":
        return probe_go(s, ctx, procs)
    if kind == "claude":
        return probe_claude(s, ctx, reach)
    if kind == "ollama":
        return probe_ollama(ctx, reach)
    if kind == "openai":
        return probe_openai(ctx, reach)
    if kind == "lanes" and lanes is not None:
        return probe_lanes(plan.name, lanes)
    if kind == "guard":
        found = s.guards.get(plan.name.lower())
        return probe_guard(ctx, found[0] if found else None)
    return Fact(kind, "unknown", "no probe for kind " + kind)


@dataclass
class Output:
    code: int
    now: datetime
    doc: str
    settings: Settings
    readings: List[Reading]
    lanes_line: str
    lanes_data: Optional[Dict[str, object]]
    not_checked: List[Tuple[str, str]]
    notes: List[str]
    findings: List[str]

    @property
    def show_burn(self) -> bool:
        return self.settings.values["burn"] != "hide"

    def asks(self) -> List[str]:
        return ["{}: {}".format(r.name, f.ask) for r in self.readings for f in r.facts if f.ask]


def gather(plans: Sequence[Plan], s: Settings, ctx: Ctx) -> Tuple[List[Reading], Optional[LaneState]]:
    wanted = {k for p in plans for k in p.run}
    procs: Optional[List[Proc]] = []
    if wanted & {"codex", "agy", "go"}:
        try:
            procs = ctx.procs()
        except Exception:        # an unlistable process table reads as "not listed here"
            procs = None
    lanes = lane_state(s, ctx) if "lanes" in wanted else None
    jobs = [(p, k) for p in plans for k in p.run]
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(16, len(jobs)))) as pool:
        futures = [pool.submit(run_probe, k, p, s, ctx, procs, lanes) for p, k in jobs]
    facts: Dict[str, List[Fact]] = {p.name: [] for p in plans}
    for (p, k), future in zip(jobs, futures):
        try:
            facts[p.name].append(future.result())
        except Exception as exc:     # a probe that raises reports its neighbour down, never skipped
            facts[p.name].append(Fact(k, "down", "{} probe failed: {}: {}".format(k, type(exc).__name__, exc)))
    readings = []
    for p in plans:
        if p.run:
            readings.append(Reading(p.name, p.run, worst(f.verdict for f in facts[p.name] if f.verdict),
                                    facts[p.name]))
    return readings, lanes


def check(root: pathlib.Path, doc: Optional[str], only: Optional[List[str]], config: Optional[str],
          burn: Optional[str], ctx: Ctx) -> Output:
    """Read the doc and the settings, run the probes. Raises OSError, UnicodeError or ValueError on bad input."""
    code, report = read(root, doc, only, False, 1.0)
    s = load_settings(root, report, config, burn, ctx.env)
    rows: List[Dict[str, object]] = list(report["neighbours"])  # type: ignore[call-overload]
    plans, notes = plan_neighbours(rows, s)
    readings, lanes = gather(plans, s, ctx)
    return Output(code, ctx.now, str(report["doc"]), s, readings,
                  lanes.line if lanes is not None else "", lanes.data if lanes is not None and lanes.line else None,
                  [(p.name, p.why) for p in plans if p.why], s.notes + notes + list(report["notes"]),  # type: ignore
                  list(report["findings"]))  # type: ignore[call-overload]


# ---------------------------------------------------------------- the block

def render(out: Output) -> str:
    who = out.settings.values["name"]
    lines = ["## Neighbours",
             "Measured {:%Y-%m-%d %H:%M} UTC by {} from {} (verdicts PROVISIONAL: closed, tight, on pace, spare, "
             "unknown; free, busy, up, down). Burn: {}.".format(
                 out.now.astimezone(timezone.utc), PROG, out.doc, "shown" if out.show_burn else "hidden")]
    for r in out.readings:
        line = "{} ({}): {}.".format(r.name, ", ".join(r.kinds), r.verdict.upper())
        meters = [strip_dot(f.meter) for f in r.facts if f.meter]
        if meters:
            line += " " + "; ".join(meters) + "."
        burns = [strip_dot(f.burn) for f in r.facts if f.burn]
        if out.show_burn and burns:
            line += " Burn: " + "; ".join(burns) + "."
        busy = [strip_dot(f.busy) for f in r.facts if f.busy]
        if busy:
            line += " Busy: " + "; ".join(busy) + "."
        lines.append(line)
    if out.lanes_line:
        lines.append("Lanes (this project): " + out.lanes_line + ".")
    if out.not_checked:
        lines.append("Not checked: " + "; ".join("{} ({})".format(n, why) for n, why in out.not_checked) + ".")
    if out.notes:
        lines.append("Notes: " + "; ".join(strip_dot(n) for n in out.notes) + ".")
    if out.findings:
        lines.append("Findings: " + "; ".join(strip_dot(f) for f in out.findings) + ".")
    asks = out.asks()
    lines.append("Ask {}: {}.".format(who, "; ".join(strip_dot(a) for a in asks) if asks else "nothing"))
    return "\n".join(lines)


def as_json(out: Output) -> str:
    return json.dumps({
        "measured": out.now.isoformat(), "doc": out.doc, "settings": out.settings.as_dict(),
        "burn": "show" if out.show_burn else "hide",
        "neighbours": [{"name": r.name, "kinds": r.kinds, "verdict": r.verdict,
                        "facts": [asdict(f) for f in r.facts]} for r in out.readings],
        "lanes": out.lanes_data, "not_checked": [{"name": n, "why": w} for n, w in out.not_checked],
        "notes": out.notes, "findings": out.findings, "asks": out.asks()}, indent=2, default=str)


# ---------------------------------------------------------------- the real world

def list_processes() -> Optional[List[Proc]]:
    try:
        r = subprocess.run(["ps", "-axo", "pid=,etime=,command="], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           universal_newlines=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_ps(r.stdout.splitlines()) if r.returncode == 0 else None


def run_command(argv: List[str], timeout: float) -> Tuple[int, str]:
    try:
        r = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True,
                           timeout=timeout, stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace")
        return r.returncode, r.stdout
    except (OSError, subprocess.SubprocessError):
        return 127, ""


def ssh_argv(destination: str, command: str) -> List[str]:
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", destination, command]


def ssh_command(destination: str, command: str) -> Tuple[int, str]:
    try:
        r = subprocess.run(ssh_argv(destination, command),
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=25,
                           stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace")
        return r.returncode, r.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return 255, str(exc)


def http_json(url: str) -> Optional[Dict[str, object]]:
    try:
        with urllib.request.urlopen(url, timeout=4) as resp:
            parsed = json.loads(resp.read().decode("utf-8"))
            return parsed if isinstance(parsed, dict) else None
    except (OSError, ValueError, UnicodeError):
        return None


def real_ctx(root: pathlib.Path) -> Ctx:
    return Ctx(now=datetime.now(timezone.utc), env=dict(os.environ), home=os.path.expanduser("~"), os_name=os.name,
               root=str(root), procs=list_processes, http=http_json, ssh=ssh_command, run=run_command,
               which=shutil.which, alive=pid_alive)


# ---------------------------------------------------------------- self-test

NOW = parse_iso("2026-10-27T03:00:00Z")
NEW_W, OLD_W = 1793604416, 1793561489       # two weekly windows' resets_at
Case = Callable[[pathlib.Path], Tuple[bool, str]]

SAMPLE = (
    "# Neighbours\n\n"
    "| name | how to reach it | family | cost | streams | live |\n"
    "|---|---|---|---|---|---|\n"
    "| cod | the `outside` server, codex_ask | OpenAI | plan | no | yes |\n"
    "| oll | Ollama on this machine (port 11434), model gemma3:12b | Gemma | free | yes | yes |\n"
    "| lan | lanes (lane-a), or LM Studio on this machine (port 1234) | Qwen | free | yes | yes |\n"
    "| rem | box-a:1236 (OpenAI wire) | Qwen | free | no | no |\n"
    "| gemini | the Antigravity CLI | Google | plan | no | no |\n"
    "| goz | the opencode CLI | Mixed | plan | no | no |\n"
    "| anth | a second account | Claude | plan | no | no |\n"
    "| nothing | carrier pigeon | Bird | free | no | no |\n"
)


def _write(path: pathlib.Path, text: str) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def _no_ssh(destination: str, command: str) -> Tuple[int, str]:
    raise AssertionError("ssh must not run here")


def _ctx(td: pathlib.Path, **over: object) -> Ctx:
    (td / "proj").mkdir(exist_ok=True)
    base: Dict[str, object] = dict(
        now=NOW, env={}, home=str(td / "home"), os_name="posix", root=str(td / "proj"), procs=lambda: [],
        http=lambda url: None, ssh=_no_ssh, run=lambda argv, timeout: (127, ""), which=lambda name: None,
        alive=lambda pid: False)
    base.update(over)
    return Ctx(**base)  # type: ignore[arg-type]


def _run_check(td: pathlib.Path, doc: Optional[str], config: Optional[str] = None, only: Optional[List[str]] = None,
               burn: Optional[str] = None, project: Optional[str] = None, **over: object) -> Output:
    (td / "proj").mkdir(exist_ok=True)
    if doc is not None:
        _write(td / "neighbours.md", doc)
    if project is not None:
        _write(td / "proj" / "pitmaster.md", project)
    return check(td / "proj", str(td / "neighbours.md"), only, config, burn, _ctx(td, **over))


def _table(rows: str) -> str:
    return ("| name | how to reach it | family | cost | streams | live |\n|---|---|---|---|---|---|\n" + rows)


def _rollout_lines() -> List[str]:
    def line(ts: str, used: float, resets_at: int, reached: Optional[str] = None) -> str:
        return json.dumps({"timestamp": ts, "type": "event_msg", "payload": {
            "type": "token_count", "info": {"total_token_usage": {"total_tokens": 1000}},
            "rate_limits": {"limit_id": "codex", "primary": {"used_percent": used, "window_minutes": 10080,
                                                              "resets_at": resets_at},
                            "secondary": None, "plan_type": "prolite", "rate_limit_reached_type": reached}}})
    return [json.dumps({"timestamp": "2026-10-26T07:30:00Z", "type": "session_meta",
                        "payload": {"originator": "codex_exec",
                                    "cwd": "/Users/runner/Documents/proj-a/.claude/worktrees/w"}}),
            line("2026-10-25T20:00:00Z", 84.0, OLD_W),       # the old window: must not enter the pace
            line("2026-10-26T03:00:00Z", 3.0, NEW_W),
            line("2026-10-26T23:00:00Z", 23.0, NEW_W + 3),   # resets_at jitters by seconds
            line("2026-10-27T02:55:00Z", 50.0, NEW_W)]


def _codex_home(td: pathlib.Path, lines: Optional[List[str]] = None) -> str:
    home = td / "codex"
    path = home / "sessions" / "2026" / "10" / "26" / "rollout-a.jsonl"
    _write(path, "\n".join(_rollout_lines() if lines is None else lines) + "\n")
    os.utime(str(path), (NOW.timestamp(), NOW.timestamp()))
    return str(home)


def _pace_check(pace: Optional[Pace]) -> Tuple[bool, str]:
    """The window rule: the 24 h gain counts only the newest window, so it is 47, not the -34 a leak gives."""
    if pace is None:
        return False, "no pace"
    gain = pace.gains.get(24, (None, None))[0]
    return gain == 47.0, "24 h gain {}".format(gain)


def _settings(**kv: object) -> Settings:
    s = Settings({})
    s.values.update({k: str(v) for k, v in kv.items()})
    return s


def _status(now: datetime, lanes: Optional[Dict[str, object]] = None) -> str:
    return json.dumps({"daemon": {"heartbeat": (now - timedelta(seconds=5)).isoformat()}, "owner": "alex",
                       "queue": {"waiting": 2, "running": 1},
                       "lanes": lanes if lanes is not None else {
                           "lane-a": {"state": "idle", "loaded": True, "running": []},
                           "lane-b": {"state": "RUNNING", "loaded": True, "running": ["j1"]}}})


def _line_of(out: Output, name: str) -> str:
    return next(x for x in render(out).splitlines() if x.startswith(name + " ("))


def c_scan(td: pathlib.Path) -> Tuple[bool, str]:
    rates, sess = scan_rollout(_rollout_lines(), "/x/rollout-a.jsonl", NOW.timestamp())
    return (len(rates) == 4 and sess is not None and sess.originator == "codex_exec"
            and sess.cwd.endswith("/w")), "{} readings".format(len(rates))


def c_pace_window(td: pathlib.Path) -> Tuple[bool, str]:
    return _pace_check(codex_pace(scan_rollout(_rollout_lines())[0]))


def c_pace_3h(td: pathlib.Path) -> Tuple[bool, str]:
    pace = codex_pace(scan_rollout(_rollout_lines())[0])
    return pace is not None and pace.gains.get(3, (0.0, 0.0))[0] == 27.0, str(pace.gains if pace else None)


def c_verdicts(td: pathlib.Path) -> Tuple[bool, str]:
    got = [verdict(50, False, 148, 2.0)[0], verdict(20, False, 48, 0.5)[0], verdict(60, False, 48, 0.5)[0],
           verdict(10, True, 48, 0.1)[0], verdict(95, False, 48, 0.0)[0]]
    return got == ["tight", "spare", "on pace", "closed", "closed"], str(got)


def c_ps_codex(td: pathlib.Path) -> Tuple[bool, str]:
    procs = parse_ps(["  101 01:00 /opt/bin/codex exec -m model-x -C /w brief",
                      "  102 06:55 /Applications/X/codex -c a=b app-server --analytics",
                      "  103 05:04 /Applications/X/codex -c x=y exec-server --remote host"])
    return [x[0] for x in jobs_of(procs, "codex", "exec", ("app-server", "exec-server"))] == [101], ""


def c_ps_opencode(td: pathlib.Path) -> Tuple[bool, str]:
    procs = parse_ps(["  104 00:10 /opt/bin/opencode run -m opencode-go/model-y x",
                      "  105 00:10 /opt/bin/opencode service start"])
    return [x[0] for x in jobs_of(procs, "opencode", "run")] == [104], ""


def c_agy_procs(td: pathlib.Path) -> Tuple[bool, str]:
    procs = parse_ps(["  106 00:02 /opt/bin/agy -p hello --add-dir /t", "  107 00:02 /opt/bin/agy remote-control",
                      "  108 00:02 /opt/bin/agy mic-serve"])
    seen: List[List[str]] = []

    def run(argv: List[str], timeout: float) -> Tuple[int, str]:
        seen.append(argv)
        return 0, "agy 1.2.3\n"
    fact = probe_agy(_settings(agy_trees=""), _ctx(td, run=run, which=lambda n: "/opt/bin/agy"), procs)
    return (fact.verdict == "unknown" and fact.data["jobs"] == [106] and "1 agy process" in fact.busy
            and fact.data["version"] == "agy 1.2.3" and seen == [["agy", "--version"]]
            and "cannot be read here" in fact.meter and fact.ask == "its quota in the Antigravity app"), str(fact)


def c_agy_trees_and_no_ps(td: pathlib.Path) -> Tuple[bool, str]:
    trees = td / "trees"
    (trees / "fresh").mkdir(parents=True)
    (trees / "stale").mkdir()
    os.utime(str(trees / "fresh"), (NOW.timestamp() - 60, NOW.timestamp() - 60))
    os.utime(str(trees / "stale"), (NOW.timestamp() - 7200, NOW.timestamp() - 7200))
    fact = probe_agy(_settings(agy_trees=str(trees)), _ctx(td), None)
    return (fact.data["recent_trees"] == ["fresh"] and "processes not listed here" in fact.busy
            and fact.data["version"] == ""), fact.busy


def c_labels(td: pathlib.Path) -> Tuple[bool, str]:
    home, trees = "/Users/runner", "/Users/runner/dispatch"
    got = [project_label(c, home, trees) for c in (
        "/Users/runner/Documents/Proj A/.claude/worktrees/w", "/Users/runner/dispatch/job-1",
        "/Users/runner/Documents/pitmaster", "/private/tmp/claude-1/x/scratchpad/r3", "/tmp/x", "/var/folders/q",
        "/Users/runner/src/tool")]
    got.append(project_label("C:\\Users\\runner\\Documents\\winproj\\sub", "C:\\Users\\runner", ""))
    return got == ["Proj A", "dispatch", "pitmaster", "scratch", "scratch", "scratch", "src", "winproj"], str(got)


def c_guard_words(td: pathlib.Path) -> Tuple[bool, str]:
    got = [guard_word(0, "VERDICT: FREE"), guard_word(1, "VERDICT: in-use"), guard_word(255, "timeout"),
           guard_word(0, ""), guard_word(3, "boom")]
    return got == ["FREE", "IN-USE", "UNREACHABLE", "FREE", "rc 3"], str(got)


def c_ssh_argv(td: pathlib.Path) -> Tuple[bool, str]:
    return ssh_argv("user@box-a", "lane-check -q") == [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "user@box-a", "lane-check -q"], ""


def c_guard_probe(td: pathlib.Path) -> Tuple[bool, str]:
    calls: List[Tuple[str, str]] = []
    answers = {"a": (0, "VERDICT: FREE"), "b": (1, "VERDICT: in-use"), "c": (255, "timeout"), "d": (0, ""),
               "e": (3, "")}

    def ssh(dest: str, command: str) -> Tuple[int, str]:
        calls.append((dest, command))
        return answers[dest.split("@")[0]]
    ctx = _ctx(td, ssh=ssh)
    got = [probe_guard(ctx, "{}@box-a lane-check -q".format(k)) for k in "abcde"]
    return ([f.verdict for f in got] == ["free", "busy", "down", "free", "busy"]
            and got[1].meter == "guard on b@box-a: IN-USE" and got[4].meter.endswith("rc 3")
            and calls[0] == ("a@box-a", "lane-check -q")), str([f.verdict for f in got])


def c_now_rows(td: pathlib.Path) -> Tuple[bool, str]:
    rows = [["2026-10-27T00:00:00+00:00", "", "/r/one", "b", "acct-1413", "sid:1"],
            ["2026-10-26T10:00:00+00:00", "", "/r/old", "b", "acct-0987", "sid:2"],        # open, but 17 h old
            ["2026-10-27T01:00:00+00:00", "2026-10-27T02:00:00+00:00", "/r/x", "-", "acct-0987", "sid:3"],
            ["2026-10-27T01:30:00+00:00", "", "/r/first", "-", "acct-0987", "sid:4"],
            ["2026-10-27T02:00:00+00:00", "", "/r/second", "-", "acct-0987", "sid:4"]]   # a resumed session
    got = open_sessions(rows, NOW)
    return [r[5] for r in got] == ["sid:1", "sid:4"] and got[1][2] == "/r/second", str([r[5] for r in got])


def c_account_label(td: pathlib.Path) -> Tuple[bool, str]:
    got = [account_label({"CLAUDE_ACCOUNT_LABEL": " work  b "}), account_label({"CLAUDE_CONFIG_DIR": "/x/.claude-b/"}),
           account_label({"CLAUDE_CODE_ACCOUNT_UUID": "1413abcd"}), account_label({})]
    return got == ["work b", "claude-b", "acct-1413", "default"], str(got)


def c_account_names(td: pathlib.Path) -> Tuple[bool, str]:
    got = [account_name("2026-10-27T14:00:00Z"), account_name("2026-10-31T19:00Z"), account_name("2026-11-02T07:27:00Z")]
    return got == ["Tue", "Sat", "Mon"], str(got)


def _meters(td: pathlib.Path) -> str:
    path = str(td / "meters.tsv")
    record(path, "acct-1413", 83, "2026-10-27T14:00:00Z", 12, "2026-10-27T07:10:00Z", "s1", NOW)
    record(path, "acct-0987", 40, "2026-10-31T19:00:00Z", None, None, "s2", NOW - timedelta(hours=2))
    record(path, "acct-0987", 41, "2026-10-31T19:00:00Z", None, None, "s3", NOW - timedelta(hours=1))
    return path


def c_meters(td: pathlib.Path) -> Tuple[bool, str]:
    path = _meters(td)
    rows = read_tsv(path)
    latest = latest_readings(rows)
    with open(path, encoding="utf-8") as f:
        text = f.read()
    return (rows[0] == METERS_HEAD.split("\t") and len(rows) == 4 and text.count("utc\tlabel") == 1
            and latest["acct-0987"][3] == "41" and latest["acct-1413"][2] == "Tue"), str(rows[0])


def _claude_files(td: pathlib.Path) -> Settings:
    meters = _meters(td)
    now_rows = [["2026-10-27T00:00:00+00:00", "", "/r/one", "b", "acct-1413", "sid:1"],
                ["2026-10-27T02:00:00+00:00", "", "/r/pitmaster", "-", "acct-0987", "sid:4"]]
    _write(td / "now.tsv", "".join("\t".join(r) + "\n" for r in now_rows))
    return _settings(meters=meters, now=str(td / "now.tsv"))


def c_claude_probe(td: pathlib.Path) -> Tuple[bool, str]:
    fact = probe_claude(_claude_files(td), _ctx(td, env={"CLAUDE_CODE_ACCOUNT_UUID": "1413abcd"}), "a second account")
    return ("Sat (acct-0987): 41 %" in fact.meter and "1 open session in now.tsv (pitmaster;" in fact.meter
            and fact.ask is None and fact.verdict == "tight"
            and fact.busy.startswith("this account (Tue, acct-1413): 1 open session")), fact.meter


def c_claude_named_label(td: pathlib.Path) -> Tuple[bool, str]:
    s = _claude_files(td)
    record(str(td / "meters.tsv"), "acct-5555", 10, "2026-10-30T10:00:00Z", None, None, "s9", NOW - timedelta(hours=1))
    ctx = _ctx(td, env={"CLAUDE_CODE_ACCOUNT_UUID": "1413abcd"})
    named = probe_claude(s, ctx, "the acct-5555 account")
    every = probe_claude(s, ctx, "a second account")
    return ("acct-5555" in named.meter and "acct-0987" not in named.meter and "acct-0987" in every.meter
            and "acct-5555" in every.meter), named.meter


def c_claude_no_reading(td: pathlib.Path) -> Tuple[bool, str]:
    s = _settings(meters=str(td / "none.tsv"), now=str(td / "none-now.tsv"))
    fact = probe_claude(s, _ctx(td), "a second account")
    _write(td / "now.tsv", "2026-10-27T02:00:00+00:00\t\t/r/p\t-\tacct-0987\tsid:4\n")
    unread = probe_claude(_settings(meters=str(td / "none.tsv"), now=str(td / "now.tsv")), _ctx(td), "")
    record(str(td / "m2.tsv"), "acct-0987", 41, "2026-10-26T19:00:00Z", None, None, "s", NOW - timedelta(hours=40))
    past = probe_claude(_settings(meters=str(td / "m2.tsv"), now=str(td / "none-now.tsv")), _ctx(td), "")
    return (fact.verdict == "unknown" and fact.ask == "the other account's weekly % (its own usage meter)"
            and unread.verdict == "unknown" and unread.ask == "the acct-0987 account's weekly % (its own usage meter)"
            and past.verdict == "unknown" and "reset at" in past.meter and past.ask is not None), past.meter


def c_burn_hidden(td: pathlib.Path) -> Tuple[bool, str]:
    doc = _table("| cod | the `outside` server, codex_ask | OpenAI | plan | no | yes |\n")
    base = _write(td / "cfg.md", "codex_home: {}\n".format(_codex_home(td)))
    on = _run_check(td, doc, config=str(base))
    off = _run_check(td, doc, config=str(base), burn="hide")
    a, b = render(on), render(off)
    return ("Burn: +47 points" in a and "+47" not in b and "TIGHT" in b and "TIGHT" in a
            and "Burn: shown." in a and "Burn: hidden." in b), a


def c_burn_config_block(td: pathlib.Path) -> Tuple[bool, str]:
    md = _write(td / "SKILL.md", "---\nname: x\n---\n# x\n\n## Config\n\n```\nburn: hide   # show | hide\n"
                                 "name: Alex\nopencode:          # empty = on PATH\n```\n\n```\nburn: show\n```\n")
    s = Settings({})
    s.apply(config_pairs(read_text(str(md))), "--config", "SKILL.md", True)
    return (s.values["burn"] == "hide" and s.values["name"] == "Alex" and s.values["opencode"] == ""
            and s.notes == []), str(s.values)


def c_config_plain(td: pathlib.Path) -> Tuple[bool, str]:
    md = _write(td / "plain.md", "---\nname: not-a-setting\n---\nburn: hide\n```\nburn: show\n```\nkind a: ollama\n")
    s = Settings({})
    s.apply(config_pairs(read_text(str(md))), "--config", "plain.md", True)
    return (s.values["burn"] == "hide" and s.values["name"] == "the user" and s.kinds["a"][0] == "ollama"
            and s.notes == []), str(s.values)


def c_codex_probe(td: pathlib.Path) -> Tuple[bool, str]:
    procs = parse_ps(["  101 01:00 /opt/bin/codex exec -m model-x brief"])
    trees = td / "dispatch"
    for job, files in (("open1", ["started"]), ("done1", ["started", "finished"]), ("idle1", [])):
        (trees / job).mkdir(parents=True)
        for f in files:
            (trees / job / f).write_text("")
    s = _settings(codex_home=_codex_home(td), codex_trees=str(trees))
    fact = probe_codex(s, _ctx(td, home="/Users/runner"), procs)
    return (fact.verdict == "tight" and "50 % of its 7.0 d window" in fact.meter
            and "+47 points in 24 h" in fact.burn and "+27 points in 4 h" in fact.burn
            and "out in ~" in fact.burn and "proj-a 0.0M" in fact.burn
            and fact.busy.startswith("1 codex exec job running, 1 open dispatch jobs; 1 session written in 15 min")
            and "(proj-a 1)" in fact.busy and fact.data["open_trees"] == ["open1"]), fact.busy + " | " + fact.burn


def c_codex_none_and_reset(td: pathlib.Path) -> Tuple[bool, str]:
    none = probe_codex(_settings(codex_home=str(td / "nothing")), _ctx(td), [])
    late = probe_codex(_settings(codex_home=_codex_home(td)), _ctx(td, now=parse_iso("2026-11-03T00:00:00Z")), [])
    return (none.verdict == "unknown" and none.ask == "its weekly % (the Codex app's usage page, or /status in a Codex "
            "session)" and late.verdict == "unknown" and "reset at" in late.meter), none.meter + " | " + late.meter


def c_codex_old_files(td: pathlib.Path) -> Tuple[bool, str]:
    home = pathlib.Path(_codex_home(td))
    path = home / "sessions" / "2026" / "10" / "26" / "rollout-a.jsonl"
    old = NOW.timestamp() - 9 * 86400
    os.utime(str(path), (old, old))
    fact = probe_codex(_settings(codex_home=str(home)), _ctx(td), [])
    return fact.verdict == "unknown" and "8 days" in fact.meter, fact.meter


def _go_run(week: float, today: float, rc: int = 0) -> Callable[[List[str], float], Tuple[int, str]]:
    def run(argv: List[str], timeout: float) -> Tuple[int, str]:
        if rc:
            return rc, ""
        return 0, json.dumps({"cost": week if "7" in argv else today})
    return run


def c_go_probe(td: pathlib.Path) -> Tuple[bool, str]:
    s = _settings(opencode="/opt/box-a/opencode", go_limits="10, 30, 60")
    procs = parse_ps(["  104 00:10 /opt/bin/opencode run -m x y", "  105 00:10 /opt/bin/opencode service start"])
    got = [probe_go(s, _ctx(td, run=_go_run(w, 1.0)), procs) for w in (31.0, 25.0, 5.0)]
    down = probe_go(s, _ctx(td, run=_go_run(0, 0, rc=1)), procs)
    missing = probe_go(_settings(), _ctx(td), procs)
    return ([f.verdict for f in got] == ["closed", "tight", "unknown"] and got[0].ask is None
            and got[2].ask is not None and "(the pool is weighted)" in got[2].ask
            and "limits $10/5 h, $30/week, $60/month" in got[1].meter and "week resets Mon 2 Nov 00:00 UTC" in got[1].meter
            and "1 opencode run job" in got[1].busy and down.verdict == "down" and missing.verdict == "down"
            and "not found" in missing.meter), str([f.verdict for f in got])


def _ollama_http(td: pathlib.Path, models: Sequence[str] = ("gemma3:12b", "qwen3:8b"),
                 loaded: Sequence[str] = ("qwen3:8b",)) -> Callable[[str], Optional[Dict[str, object]]]:
    def http(url: str) -> Optional[Dict[str, object]]:
        if url == "http://127.0.0.1:11434/api/tags":
            return {"models": [{"name": m} for m in models]}
        if url == "http://127.0.0.1:11434/api/ps":
            return {"models": [{"name": m} for m in loaded]}
        return None
    return http


def c_ollama_probe(td: pathlib.Path) -> Tuple[bool, str]:
    reach = "Ollama on this machine (port 11434), model gemma3:12b"
    up = probe_ollama(_ctx(td, http=_ollama_http(td)), reach)
    absent = probe_ollama(_ctx(td, http=_ollama_http(td)), "Ollama (port 11434), model other:1b")
    many = probe_ollama(_ctx(td, http=_ollama_http(td, models=["m%d" % i for i in range(8)], loaded=[])), "Ollama")
    off = probe_ollama(_ctx(td), reach)
    return (up.verdict == "up" and "model gemma3:12b is there" in up.meter and "loaded: qwen3:8b" in up.busy
            and absent.verdict == "down" and "not pulled" in absent.meter and "(+2 more)" in many.meter
            and "loaded: none" in many.busy and off.verdict == "down"), up.meter


def c_openai_probe(td: pathlib.Path) -> Tuple[bool, str]:
    seen: List[str] = []

    def http(url: str) -> Optional[Dict[str, object]]:
        seen.append(url)
        if url == "http://box-a:1236/v1/models":
            return {"data": [{"id": "m%d" % i} for i in range(8)]}
        if url == "http://127.0.0.1:1234/v1/models":
            return {"data": []}
        return None
    ctx = _ctx(td, http=http)
    remote = probe_openai(ctx, "box-a:1236 (OpenAI wire)")
    empty = probe_openai(ctx, "LM Studio on this machine (port 1234)")
    off = probe_openai(ctx, "http://127.0.0.1:9999")
    return (remote.verdict == "up" and remote.meter == "serves: m0, m1, m2, m3, m4, m5 (+2 more)"
            and empty.verdict == "up" and empty.meter == "answers, no model resident"
            and off.verdict == "down" and "http://127.0.0.1:9999/v1/models" in seen), remote.meter


def _kinds_of(td: pathlib.Path) -> Dict[str, List[str]]:
    _write(td / "neighbours.md", SAMPLE)
    (td / "proj").mkdir(exist_ok=True)
    _code, report = read(td / "proj", str(td / "neighbours.md"), None, False, 1.0)
    return {str(r["name"]): guess_kinds(r) for r in report["neighbours"]}  # type: ignore[union-attr]


def _kind_case(name: str, expected: List[str]) -> Case:
    def case(td: pathlib.Path) -> Tuple[bool, str]:
        got = _kinds_of(td)[name]
        return got == expected, str(got)
    return case


def c_kind_addresses(td: pathlib.Path) -> Tuple[bool, str]:
    s = Settings({})
    plans, _n = plan_neighbours(
        [{"name": n, "reach": r, "family": ""} for n, r in (
            ("a", "lanes (lane-a), or LM Studio on this machine (port 1234)"), ("b", "box-a:1236 (OpenAI wire)"),
            ("c", "Ollama on this machine (port 11434), model gemma3:12b"),
            ("d", "local models on this machine (port 11434)"))], s)
    return ([p.kinds for p in plans] == [["openai", "lanes"], ["openai"], ["ollama"], ["ollama"]]
            and base_url("lanes, or LM Studio", "http://d") == "http://d"
            and base_url("box-a:1236 (OpenAI wire)", "") == "http://box-a:1236"
            and base_url("https://box-a.example.com:8443/x", "") == "https://box-a.example.com:8443"
            and base_url("port 1234", "") == "http://127.0.0.1:1234"), str([p.kinds for p in plans])


def c_not_checked(td: pathlib.Path) -> Tuple[bool, str]:
    out = _run_check(td, SAMPLE, http=lambda url: {"models": [], "data": []} if "1236" not in url else None)
    text = render(out)
    return ([n for n, _w in out.not_checked] == ["nothing"] and "Not checked: nothing (no probe matches its row; "
            "name one with `kind nothing: <kind>`)." in text), text


def c_kind_override(td: pathlib.Path) -> Tuple[bool, str]:
    cfg = _write(td / "cfg.md", "kind cod: none\nkind nothing: ollama\n")
    out = _run_check(td, SAMPLE, config=str(cfg), http=_ollama_http(td))
    names_ = [r.name for r in out.readings]
    nothing = next(r for r in out.readings if r.name == "nothing")
    return ("cod" not in names_ and dict(out.not_checked)["cod"] == "checks nothing: kind cod: none"
            and nothing.kinds == ["ollama"] and nothing.verdict == "up"), render(out)


def c_probe_off(td: pathlib.Path) -> Tuple[bool, str]:
    cfg = _write(td / "cfg.md", "probes: codex, claude, agy, go, lanes, guard, openai\n")
    out = _run_check(td, SAMPLE, config=str(cfg))
    return (dict(out.not_checked).get("oll") == "probe off in settings: ollama"
            and "oll" not in [r.name for r in out.readings]), render(out)


def c_precedence(td: pathlib.Path) -> Tuple[bool, str]:
    cfg = _write(td / "SKILL.md", "# s\n\n## Config\n\n```\nburn: hide\nname: Config\ngo_limits: 1, 2, 3\n"
                                  "probes: ollama\nbogus: 1\n```\n")
    doc = _table("| oll | Ollama (port 11434) | Gemma | free | yes | yes |\n") + (
        "\n## neighbour-check\nburn: show\nname: Doc\ngo_limits: 4, 5, 6\nzorp: 1\n```\nname: Fenced\n```\n"
        "\n## Later\nname: After\n")
    project = "# p\nburn: hide\nname: Project\nneighbours: oll\nwho: ignored\n"
    out = _run_check(td, doc, config=str(cfg), project=project)
    v, o = out.settings.values, out.settings.origin
    flagged = _run_check(td, None, config=str(cfg), project=project, burn="show")
    return (v["name"] == "Project" and o["name"] == "pitmaster.md" and v["burn"] == "hide" and o["burn"] == "pitmaster.md"
            and v["go_limits"] == "4, 5, 6" and o["go_limits"] == "doc" and v["probes"] == "ollama"
            and o["probes"] == "--config" and flagged.settings.values["burn"] == "show"
            and flagged.settings.origin["burn"] == "--burn"), str(out.settings.as_dict())


def c_unknown_setting_notes(td: pathlib.Path) -> Tuple[bool, str]:
    cfg = _write(td / "SKILL.md", "## Config\n\n```\nbogus: 1\n```\n")
    doc = _table("| oll | Ollama (port 11434) | Gemma | free | yes | yes |\n") + "\n## neighbour-check\nzorp: 1\n"
    out = _run_check(td, doc, config=str(cfg), project="neighbours: oll\nwho: ignored\n", http=_ollama_http(td))
    text = render(out)
    return (out.code == 0 and "unknown setting bogus in SKILL.md, ignored" in out.notes
            and "unknown setting zorp in neighbours.md, ignored" in out.notes
            and not any(n.startswith("unknown setting who") for n in out.notes) and "Notes: unknown setting bogus" in text), text


def c_names_not_on_list(td: pathlib.Path) -> Tuple[bool, str]:
    doc = _table("| oll | Ollama (port 11434) | Gemma | free | yes | yes |\n") + (
        "\n## neighbour-check\nkind ghost: ollama\nguard ghost: user@box-a lane-check -q\n")
    out = _run_check(td, doc, http=_ollama_http(td))
    return ([r.name for r in out.readings] == ["oll"]
            and sum("setting names ghost" in n for n in out.notes) == 2), str(out.notes)


def c_guard_setting(td: pathlib.Path) -> Tuple[bool, str]:
    doc = _table("| box | carrier pigeon | Bird | free | no | no |\n") + (
        "\n## neighbour-check\nguard box: user@box-a lane-check -q    # busy check\n")
    calls: List[Tuple[str, str]] = []

    def ssh(dest: str, command: str) -> Tuple[int, str]:
        calls.append((dest, command))
        return 0, "VERDICT: IN-USE"
    out = _run_check(td, doc, ssh=ssh)
    return (calls == [("user@box-a", "lane-check -q")] and out.readings[0].kinds == ["guard"]
            and out.readings[0].verdict == "busy" and "guard on user@box-a: IN-USE" in render(out)), render(out)


def _lanes_proj(td: pathlib.Path, status: Optional[str]) -> None:
    (td / "proj").mkdir(exist_ok=True)
    if status is not None:
        _write(td / "proj" / "lanes" / "queue" / "STATUS.json", status)


LANES_DOC = _table("| lane-a | lanes (lane-a), or LM Studio on this machine (port 1234) | Qwen | free | yes | yes |\n"
                   "| lane-b | lanes (lane-b) | Qwen | free | no | no |\n")


def _lm(url: str) -> Optional[Dict[str, object]]:
    return {"data": [{"id": "m1"}]} if url == "http://127.0.0.1:1234/v1/models" else None


def c_lanes_none(td: pathlib.Path) -> Tuple[bool, str]:
    _lanes_proj(td, None)
    out = _run_check(td, LANES_DOC, http=_lm)
    lan = next(r for r in out.readings if r.name == "lane-a")
    lbn = next(r for r in out.readings if r.name == "lane-b")
    return (lan.verdict == "up" and lbn.verdict == "unknown" and "this project has no lane queue" in render(out)
            and out.lanes_line == "" and "Lanes (this project)" not in render(out)), render(out)


def c_lanes_free_busy(td: pathlib.Path) -> Tuple[bool, str]:
    _lanes_proj(td, _status(NOW))
    out = _run_check(td, LANES_DOC, http=_lm)
    text = render(out)
    lan = next(r for r in out.readings if r.name == "lane-a")
    lbn = next(r for r in out.readings if r.name == "lane-b")
    return (lan.verdict == "free" and lbn.verdict == "busy" and text.count("Lanes (this project):") == 1
            and "daemon up (heartbeat 5 s ago)" in text and "owner alex" in text
            and "queue 2 waiting, 1 running" in text and "lane lane-b RUNNING (loaded, 1 job)" in text), text


def c_lanes_switch_off_and_daemon(td: pathlib.Path) -> Tuple[bool, str]:
    _lanes_proj(td, _status(NOW))
    _write(td / "switch.md", "# s\nLANES: OFF (ruled)\n")
    cfg = _write(td / "cfg.md", "lane_switch: {}\n".format(td / "switch.md"))
    off = _run_check(td, LANES_DOC, config=str(cfg), http=_lm)
    _lanes_proj(td, _status(NOW - timedelta(seconds=300)))
    stale = _run_check(td, LANES_DOC, http=_lm)
    return (next(r for r in off.readings if r.name == "lane-a").verdict == "busy" and "LANES: OFF" in off.lanes_line
            and next(r for r in stale.readings if r.name == "lane-a").verdict == "busy"
            and "daemon DOWN" in stale.lanes_line), off.lanes_line + " | " + stale.lanes_line


def c_lanes_relative_switch(td: pathlib.Path) -> Tuple[bool, str]:
    _lanes_proj(td, _status(NOW))
    _write(td / "proj" / "docs" / "switch.md", "# s\nLANES: OFF (ruled)\n")
    cfg = _write(td / "cfg.md", "lane_switch: docs/switch.md\n")
    out = _run_check(td, LANES_DOC, config=str(cfg), http=_lm)
    return (next(r for r in out.readings if r.name == "lane-a").verdict == "busy"
            and "LANES: OFF" in out.lanes_line), out.lanes_line


def c_lanes_unknown_state(td: pathlib.Path) -> Tuple[bool, str]:
    _lanes_proj(td, _status(NOW, {"lane-a": {"state": "unknown", "loaded": None, "remote": "unknown"}}))
    cfg = _write(td / "cfg.md", "guard lane-a: user@box-a lane-check -q\n")
    out = _run_check(td, LANES_DOC, config=str(cfg), http=_lm, ssh=lambda d, c: (0, "VERDICT: FREE"))
    lan = next(r for r in out.readings if r.name == "lane-a")
    return (lan.verdict == "free" and "the queue does not know lane lane-a's state" in render(out)), _line_of(out, "lane-a")


def c_lanes_not_in_status(td: pathlib.Path) -> Tuple[bool, str]:
    _lanes_proj(td, _status(NOW, {"lane-a": {"state": "idle", "loaded": False, "running": []}}))
    out = _run_check(td, LANES_DOC, http=lambda url: None)
    lbn = next(r for r in out.readings if r.name == "lane-b")
    return lbn.verdict == "unknown" and "lane-b is not in the queue's status" in render(out), render(out)


def c_holds_posix(td: pathlib.Path) -> Tuple[bool, str]:
    _lanes_proj(td, _status(NOW))
    _write(td / "proj" / "lanes" / "HOLD-lane-b", "maintenance window pid 4242\nmore\n")
    _write(td / "holds" / "HOLD-extra", "no pid here\n")
    cfg = _write(td / "cfg.md", "lane_holds: {}\n".format(td / "holds"))
    seen: List[int] = []

    def alive(pid: int) -> bool:
        seen.append(pid)
        return pid == 4242
    out = _run_check(td, LANES_DOC, config=str(cfg), alive=alive, http=_lm)
    return (seen == [4242] and "HOLD-lane-b (maintenance window; pid 4242 alive)" in out.lanes_line
            and "HOLD-extra (no pid here)" in out.lanes_line), out.lanes_line


def c_holds_windows(td: pathlib.Path) -> Tuple[bool, str]:
    _lanes_proj(td, _status(NOW))
    _write(td / "proj" / "lanes" / "HOLD-lane-b", "maintenance window pid 4242\n")
    killed: List[int] = []

    def alive(pid: int) -> bool:
        killed.append(pid)
        return True
    out = _run_check(td, LANES_DOC, os_name="nt", alive=alive, http=_lm)
    return killed == [] and "maintenance window; pid 4242 not checked here" in out.lanes_line, out.lanes_line


def c_main_checkout(td: pathlib.Path) -> Tuple[bool, str]:
    ctx = _ctx(td, run=lambda argv, timeout: (0, "/Users/runner/repo/.git\n") if "rev-parse" in argv else (1, ""))
    return (main_checkout("/w/tree", ctx) == "/Users/runner/repo"
            and main_checkout("/w/tree", _ctx(td)) == "/w/tree"), ""


def c_combine(td: pathlib.Path) -> Tuple[bool, str]:
    return (worst(["spare", "free", "up"]) == "free" and worst(["up", "busy"]) == "busy" and worst([]) == "unknown"
            and worst(["on pace", "unknown"]) == "unknown"
            and [worst([a, b]) for a, b in zip(RANK, reversed(RANK))] == [RANK[min(i, 8 - i)] for i in range(9)]), ""


def c_crash(td: pathlib.Path) -> Tuple[bool, str]:
    def http(url: str) -> Optional[Dict[str, object]]:
        raise RuntimeError("boom")
    out = _run_check(td, _table("| oll | Ollama (port 11434) | Gemma | free | yes | yes |\n"), http=http)
    text = render(out)
    return out.readings[0].verdict == "down" and "RuntimeError: boom" in text, text


def c_render_shape(td: pathlib.Path) -> Tuple[bool, str]:
    doc = _table("| oll | Ollama (port 11434) | Gemma | free | yes | yes |\n")
    cfg = _write(td / "cfg.md", "name: Alex\n")
    out = _run_check(td, doc, config=str(cfg), http=_ollama_http(td))
    lines = render(out).splitlines()
    return (lines[0] == "## Neighbours"
            and lines[1].startswith("Measured 2026-10-27 03:00 UTC by neighbour-check from ")
            and lines[1].endswith("(verdicts PROVISIONAL: closed, tight, on pace, spare, unknown; free, busy, up, "
                                  "down). Burn: shown.")
            and lines[2].startswith("oll (ollama): UP. no meter (local); available: gemma3:12b, qwen3:8b. Busy: loaded:")
            and lines[-1] == "Ask Alex: nothing."), "\n".join(lines)


def c_asks(td: pathlib.Path) -> Tuple[bool, str]:
    doc = _table("| cod | the `outside` server, codex_ask | OpenAI | plan | no | yes |\n"
                 "| gemini | the Antigravity CLI | Google | plan | no | no |\n")
    cfg = _write(td / "cfg.md", "codex_home: {}\n".format(td / "nothing"))
    out = _run_check(td, doc, config=str(cfg))
    return render(out).splitlines()[-1] == (
        "Ask the user: cod: its weekly % (the Codex app's usage page, or /status in a Codex session); "
        "gemini: its quota in the Antigravity app."), render(out)


def c_json(td: pathlib.Path) -> Tuple[bool, str]:
    out = _run_check(td, SAMPLE, burn="hide", http=lambda url: None)
    data = json.loads(as_json(out))
    return (set(data) == {"measured", "doc", "settings", "burn", "neighbours", "lanes", "not_checked", "notes",
                          "findings", "asks"} and data["burn"] == "hide"
            and data["settings"]["burn"] == {"value": "hide", "from": "--burn"}
            and {n["name"] for n in data["neighbours"]} >= {"oll", "rem"}
            and data["not_checked"] == [{"name": "nothing", "why": "no probe matches its row; name one with "
                                         "`kind nothing: <kind>`"}]), as_json(out)[:200]


def c_no_doc(td: pathlib.Path) -> Tuple[bool, str]:
    (td / "proj").mkdir(exist_ok=True)
    out = check(td / "proj", str(td / "absent.md"), None, None, None, _ctx(td))
    text = render(out)
    return (out.code == 1 and out.readings == [] and "Findings: " in text and "no neighbours doc here" in text
            and text.splitlines()[-1] == "Ask the user: nothing."), text


def c_only(td: pathlib.Path) -> Tuple[bool, str]:
    narrowed = _run_check(td, SAMPLE, only=["oll"], http=_ollama_http(td))
    unknown = _run_check(td, SAMPLE, only=["ghost"], http=_ollama_http(td))
    return ([r.name for r in narrowed.readings] == ["oll"] and narrowed.code == 0 and unknown.code == 1
            and "--only names ghost" in render(unknown) and unknown.readings == []), render(narrowed)


def c_project_narrows(td: pathlib.Path) -> Tuple[bool, str]:
    out = _run_check(td, SAMPLE, project="neighbours: oll, gemini\nkind oll: none\n", http=_ollama_http(td))
    return ([r.name for r in out.readings] == ["gemini"] and dict(out.not_checked) == {
        "oll": "checks nothing: kind oll: none"}), render(out)


def c_bad_config(td: pathlib.Path) -> Tuple[bool, str]:
    (td / "proj").mkdir(exist_ok=True)
    _write(td / "neighbours.md", SAMPLE)
    try:
        check(td / "proj", str(td / "neighbours.md"), None, str(td / "absent.md"), None, _ctx(td))
    except OSError:
        return True, ""
    return False, "no error"


def c_record_line(td: pathlib.Path) -> Tuple[bool, str]:
    path = str(td / "deep" / "meters.tsv")
    line = record(path, "acct-1413", 83.5, "2026-10-27T14:00:00Z", 12, "2026-10-27T07:10:00Z", "a  b\tc", NOW)
    return (line == "2026-10-27T03:00Z\tacct-1413\tTue\t83.5\t2026-10-27T14:00:00+00:00\t12\t"
            "2026-10-27T07:10:00+00:00\ta b c"), line


def _quiet_main(argv: List[str]) -> Tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main(argv)
        except SystemExit as exc:
            code = int(exc.code) if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue()


def c_record_cli(td: pathlib.Path) -> Tuple[bool, str]:
    cfg = _write(td / "cfg.md", "meters: {}\n".format(td / "m.tsv"))
    base = ["record", "--config", str(cfg), "--doc", str(td / "absent.md"), "--root", str(td)]
    ok = _quiet_main(base + ["--weekly", "83", "--weekly-reset", "2026-10-27T14:00:00Z", "--session", "s1"])
    rows = read_tsv(str(td / "m.tsv"))
    bad = [_quiet_main(base + ["--weekly", "150", "--weekly-reset", "2026-10-27T14:00:00Z"])[0],
           _quiet_main(base + ["--weekly", "5", "--weekly-reset", "tomorrow"])[0],
           _quiet_main(base + ["--weekly", "5", "--weekly-reset", "2026-10-27T14:00:00Z", "--five", "-1",
                               "--five-reset", "2026-10-27T07:00:00Z"])[0],
           _quiet_main(base + ["--weekly-reset", "2026-10-27T14:00:00Z"])[0],
           _quiet_main(["record", "--weekly", "5", "--weekly-reset", "2026-10-27T14:00:00Z",
                        "--root", str(td / "absent")])[0]]
    return (ok[0] == 0 and "neighbour-check: recorded in {}: ".format(td / "m.tsv") in ok[1]
            and len(rows) == 2 and rows[1][3] == "83" and rows[1][7] == "s1" and bad == [2, 2, 2, 2, 2]), str(bad)


def c_usage(td: pathlib.Path) -> Tuple[bool, str]:
    got = [_quiet_main(["--burn", "bogus"])[0], _quiet_main(["--root", str(td / "absent")])[0],
           _quiet_main(["--weekly", "5"])[0], _quiet_main(["--only", ","])[0],
           _quiet_main(["--config", str(td / "absent.md"), "--doc", str(td / "absent.md"), "--root", str(td)])[0]]
    return got == [2, 2, 2, 2, 2], str(got)


def c_subprocess(td: pathlib.Path) -> Tuple[bool, str]:
    """A lone copy of the script, run by path with an empty HOME: no other file of this repository is needed."""
    solo = td / "solo"
    solo.mkdir()
    script = solo / "neighbour-check.py"
    shutil.copy(str(pathlib.Path(__file__).resolve()), str(script))
    home = td / "home"
    home.mkdir()
    doc = _write(td / "nb.md", _table("| nothing | carrier pigeon | Bird | free | no | no |\n"
                                      "| quiet | carrier pigeon | Bird | free | no | no |\n")
                 + "\n## neighbour-check\nkind quiet: none\n")
    env = {k: v for k, v in os.environ.items() if k not in ("METERS_TSV", "NOW_TSV", "CODEX_HOME",
                                                           "NEIGHBOUR_CHECK_BREAK")}
    env.update(HOME=str(home), USERPROFILE=str(home))

    def call(*args: str) -> "subprocess.CompletedProcess[str]":
        return subprocess.run([sys.executable, str(script)] + list(args), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True, encoding="utf-8", env=env,
                              cwd=str(td))
    shown = call("--doc", str(doc), "--root", str(td))
    no_doc = call("--root", str(td))
    as_json = call("--doc", str(doc), "--root", str(td), "--json")
    rec = call("record", "--weekly", "9", "--weekly-reset", "2026-10-27T14:00:00Z", "--root", str(td))
    bad_root = call("--root", str(td / "absent"))
    return (shown.returncode == 0 and shown.stdout.startswith("## Neighbours\n")
            and "Not checked: nothing (no probe matches its row; name one with `kind nothing: <kind>`); "
            "quiet (checks nothing: kind quiet: none)." in shown.stdout
            and no_doc.returncode == 1 and "Findings: " in no_doc.stdout
            and json.loads(as_json.stdout)["not_checked"][0]["name"] == "nothing"
            and rec.returncode == 0 and (home / ".claude" / "meters.tsv").is_file() and bad_root.returncode == 2
            ), "{} {} {}".format(shown.returncode, shown.stdout, shown.stderr)


_CASES: List[Tuple[str, Case]] = [
    ("codex: a rollout's readings and its originator", c_scan),
    ("codex: the 24 h gain counts the newest window only (47)", c_pace_window),
    ("codex: the 3 h gain is 27 points", c_pace_3h),
    ("verdict: tight, spare, on pace, reached, 95 %", c_verdicts),
    ("ps: codex exec is a job; app-server and exec-server are not", c_ps_codex),
    ("ps: opencode run is a job; the service is not", c_ps_opencode),
    ("agy: processes, version, unknown verdict and its ask", c_agy_procs),
    ("agy: recent clones counted; no process list says so", c_agy_trees_and_no_ps),
    ("labels: worktree, dispatch folder, Documents, scratch, Windows path", c_labels),
    ("guard: FREE, IN-USE, UNREACHABLE, no word, rc N", c_guard_words),
    ("guard: the ssh command line", c_ssh_argv),
    ("guard: the probe's verdicts and meter", c_guard_probe),
    ("now.tsv: open rows under 12 h, one per session", c_now_rows),
    ("account label follows now.py's rule", c_account_label),
    ("accounts are named by reset weekday", c_account_names),
    ("meters: one header, the newest row per label", c_meters),
    ("claude: the other account, its reading and open session", c_claude_probe),
    ("claude: a reach naming a label stands for that label only", c_claude_named_label),
    ("claude: no reading, an unread label, a passed reset", c_claude_no_reading),
    ("codex: the probe end to end (tight, burn, busy)", c_codex_probe),
    ("codex: no reading in 8 days; a reset since the reading", c_codex_none_and_reset),
    ("codex: files older than 8 days are not read", c_codex_old_files),
    ("go: closed, tight, unknown, down, no binary", c_go_probe),
    ("ollama: up with its model, a missing model, +N more, down", c_ollama_probe),
    ("openai: serves, no model resident, down", c_openai_probe),
    ("burn: shown by default, hidden by the flag, the verdict kept", c_burn_hidden),
    ("settings: read from a SKILL.md Config block; `key:  # comment` is empty", c_burn_config_block),
    ("config: a plain file, front matter skipped, fences skipped", c_config_plain),
    ("kinds: a codex row is codex", _kind_case("cod", ["codex"])),
    ("kinds: Ollama on port 11434 is ollama", _kind_case("oll", ["ollama"])),
    ("kinds: lanes plus LM Studio is lanes and openai", lambda td: ((lambda g: (sorted(g) == ["lanes", "openai"], str(g)))(
        _kinds_of(td)["lan"]))),
    ("kinds: box-a:1236 (OpenAI wire) is openai", _kind_case("rem", ["openai"])),
    ("kinds: an agy row is agy", _kind_case("gemini", ["agy"])),
    ("kinds: an opencode row is go", _kind_case("goz", ["go"])),
    ("kinds: family Claude is claude", _kind_case("anth", ["claude"])),
    ("kinds: a row that matches nothing has none", _kind_case("nothing", [])),
    ("kinds: addresses and base URLs", c_kind_addresses),
    ("a neighbour with no kind is listed as Not checked", c_not_checked),
    ("kind <name>: none and kind <name>: ollama override the guess", c_kind_override),
    ("a kind left out of probes puts its neighbour on Not checked", c_probe_off),
    ("settings: config < doc section < pitmaster.md < --burn", c_precedence),
    ("settings: an unknown key is a note in config and doc, ignored in pitmaster.md", c_unknown_setting_notes),
    ("settings: kind and guard for a name not on the list run nothing", c_names_not_on_list),
    ("guard: a guard setting runs its command and sets the verdict", c_guard_setting),
    ("lanes: no queue gives a fact with no verdict", c_lanes_none),
    ("lanes: idle is free, running is busy, one shared line", c_lanes_free_busy),
    ("lanes: the switch OFF and a stale daemon make a lane busy", c_lanes_switch_off_and_daemon),
    ("lanes: a lane missing from the status has no verdict", c_lanes_not_in_status),
    ("lanes: a relative lane_switch is read from the main checkout", c_lanes_relative_switch),
    ("lanes: a lane the queue marks unknown has no verdict; its guard decides", c_lanes_unknown_state),
    ("lanes: hold files with a live pid", c_holds_posix),
    ("lanes: on Windows no pid is signalled", c_holds_windows),
    ("lanes: the main checkout from git's common dir", c_main_checkout),
    ("verdicts combine worst first", c_combine),
    ("a probe that raises reports its neighbour down", c_crash),
    ("the block's shape and the Ask line", c_render_shape),
    ("asks gather from every fact", c_asks),
    ("--json carries the same readings", c_json),
    ("red: no doc is exit 1 with a Findings line", c_no_doc),
    ("--only narrows, and an unknown name is a finding", c_only),
    ("pitmaster.md narrows the list", c_project_narrows),
    ("an unreadable --config is an error", c_bad_config),
    ("record: the line, tab-separated", c_record_line),
    ("record: the CLI writes, and bad input is exit 2", c_record_cli),
    ("usage: bad flags and folders are exit 2", c_usage),
    ("copy-one-folder: run by path, empty HOME", c_subprocess),
]


def self_test() -> int:
    passed = failed = 0

    def report(name: str, ok: bool, detail: str = "") -> None:
        nonlocal passed, failed
        print("{} {}{}".format("PASS" if ok else "FAIL", name, " ({})".format(detail) if detail and not ok else ""))
        passed += ok
        failed += not ok

    for name, case in _CASES:
        with tempfile.TemporaryDirectory(prefix="neighbour-check-") as tmp:
            try:
                ok, detail = case(pathlib.Path(tmp))
            except Exception as exc:     # a crashing case is a failing case
                ok, detail = False, "{}: {}".format(type(exc).__name__, exc)
        report(name, bool(ok), detail)
    # The red arm: the real pace function with the planted defect on (an earlier window's readings leak in) must
    # FAIL the same window rule the green case passed.
    saved = os.environ.get("NEIGHBOUR_CHECK_BREAK")
    os.environ["NEIGHBOUR_CHECK_BREAK"] = "1"
    try:
        bad_ok, bad_detail = _pace_check(codex_pace(scan_rollout(_rollout_lines())[0]))
    finally:
        if saved is None:
            del os.environ["NEIGHBOUR_CHECK_BREAK"]
        else:
            os.environ["NEIGHBOUR_CHECK_BREAK"] = saved
    print("{} red arm (planted): the leaked window's pace -> {}".format("FAIL" if not bad_ok else "PASS?", bad_detail))
    report("red arm: the planted cross-window pace was caught by the window rule", not bad_ok)
    ran = passed + failed
    if failed:
        print("{}: self-test FAIL -- {} of {} cases failed".format(PROG, failed, ran))
        return 1
    print("{}: self-test PASS -- {} of {} cases passed (the planted FAIL line above was caught)".format(
        PROG, passed, ran))
    return 0


# ---------------------------------------------------------------- main

def percent(text: str) -> float:
    value = float(text)
    if not 0 <= value <= 100:
        raise ValueError("a percentage must be from 0 to 100")
    return value


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog=PROG, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", nargs="?", choices=("record",), help="record: append a Claude account's reading")
    ap.add_argument("--root", default=".", help="project root holding pitmaster.md (default: current directory)")
    ap.add_argument("--doc", help="the neighbours doc to read; wins over pitmaster.md and the default")
    ap.add_argument("--only", help="comma-separated names to narrow the list to")
    ap.add_argument("--config", help="a settings file, or a SKILL.md whose `## Config` block holds them")
    ap.add_argument("--burn", choices=("show", "hide"), help="show or hide burn numbers; wins over every setting")
    ap.add_argument("--json", action="store_true", help="print the readings as JSON")
    ap.add_argument("--weekly", type=percent, metavar="PCT", help="record: the weekly usage, 0 to 100")
    ap.add_argument("--weekly-reset", metavar="ISO", help="record: when the weekly limit resets")
    ap.add_argument("--five", type=percent, metavar="PCT", help="record: the 5 h usage, 0 to 100")
    ap.add_argument("--five-reset", metavar="ISO", help="record: when the 5 h limit resets")
    ap.add_argument("--session", default="", help="record: free text naming the session")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if args.self_test:
        return self_test()
    recording = args.command == "record"
    if recording:
        if args.weekly is None or not args.weekly_reset:
            ap.error("record needs --weekly and --weekly-reset")
        if args.only is not None or args.json or args.burn:
            ap.error("record takes no --only, --json or --burn")
    elif any(v is not None for v in (args.weekly, args.weekly_reset, args.five, args.five_reset)) or args.session:
        ap.error("--weekly, --weekly-reset, --five, --five-reset and --session belong to `record`")
    only = names(args.only) if args.only is not None else None
    if only is not None and not only:
        ap.error("--only must name at least one neighbour")
    root = pathlib.Path(args.root)
    ctx = real_ctx(root)
    try:
        if recording:
            for label, value in (("--weekly-reset", args.weekly_reset), ("--five-reset", args.five_reset)):
                if value:
                    parse_iso(value)
            _code, found = read(root, args.doc, None, False, 1.0)
            s = load_settings(root, found, args.config, None, ctx.env)
            path = s.path("meters", ctx.home)
            line = record(path, account_label(ctx.env), args.weekly, args.weekly_reset, args.five, args.five_reset,
                          args.session, ctx.now)
            print("{}: recorded in {}: {}".format(PROG, path, line))
            return 0
        out = check(root, args.doc, only, args.config, args.burn, ctx)
    except (OSError, UnicodeError, ValueError) as exc:
        print("{}: {}".format(PROG, exc), file=sys.stderr)
        return 2
    print(as_json(out) if args.json else render(out))
    return out.code


if __name__ == "__main__":
    sys.exit(main())
