#!/usr/bin/env python3
"""Read the neighbours doc and say who answers right now. Python 3.8+, standard library only.

Usage: neighbours.py [--root DIR] [--doc PATH] [--only NAMES] [--no-probe] [--timeout S] [--json]
       neighbours.py --self-test

The neighbours doc is the allow list of models other than the one you are talking to
that a skill may call. It is found in this order: --doc; the `neighbours:` line of
pitmaster.md in --root when that line names a path; then ~/.pitmaster/neighbours.md.
A `neighbours:` line that names neighbours instead (`neighbours: s35, codex`) narrows
the list to those names. --only narrows it further. Narrowing never adds a name: a
name the doc does not list is a finding. A value is a path when it starts with ~ or
holds a / or \\ or ends in .md.

The doc holds one table whose header starts with a "name" cell and has a "how to
reach it" cell; family, cost, streams and live columns are optional. When present,
cost is free, plan or paid, and streams and live are yes or no. A "Seats: a, b" line
names the default line-up; each seat must be on the list. A "## Live" section may set
`delivery:` (all at once, streamed or hybrid; hybrid when unset) and `filibuster:`
(free text; 1 sentence when unset). HTML comments and fenced code blocks are skipped.

"Who answers" is a network check, made only where the "how to reach it" cell gives an
address: a URL (http://host:port) or "port N" (this machine). The check opens a TCP
connection and closes it; it proves something listens there, not that a model is
loaded. A neighbour reached only through an agent's tool (an MCP server, a lane) is
reported as not checked. --no-probe skips every check.

Exit 0: the list was read and is sound (some neighbours may not answer); 1: no doc,
or findings in it; 2: invalid usage or unreadable input.
"""
from __future__ import annotations

import argparse
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
from typing import Callable, Dict, List, Optional, Tuple

PROG = "neighbours"
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


def show(report: Dict[str, object]) -> None:
    for problem in report["findings"]:  # type: ignore[union-attr]
        print("FAIL " + str(problem))
    rows = report["neighbours"]  # type: List[Dict[str, object]]
    narrowed = report["narrowed_to"]
    print("doc: {} (from {}){}".format(report["doc"], report["source"],
                                      ", narrowed to: " + ", ".join(narrowed) if narrowed is not None else ""))
    if rows:
        head = ("name", "family", "cost", "streams", "live", "seat", "now")
        table = [head] + [(str(r["name"]), str(r["family"]), str(r["cost"]), str(r["streams"]), str(r["live"]),
                           "yes" if r["seat"] else "", str(r["now"]) + (" ({})".format(r["address"])
                                                                         if r["address"] and r["now"] != "not probed"
                                                                         else ""))
                          for r in rows]
        widths = [max(len(t[c]) for t in table) for c in range(len(head))]
        for t in table:
            print("  " + "  ".join(v.ljust(w) for v, w in zip(t, widths)).rstrip())
    seats = report["seats"]
    print("Seats: {}".format(", ".join(seats) if seats else "(none)"))  # type: ignore[arg-type]
    live = report["live"]  # type: Dict[str, str]
    print("Live: delivery {}, filibuster {}".format(live["delivery"], live["filibuster"]))
    for note in report["notes"]:  # type: ignore[union-attr]
        print("note: " + str(note))
    count = lambda word: sum(1 for r in rows if str(r["now"]).startswith(word))
    print("{}: {} neighbour(s), {} answer, {} silent, {} not checked, {} finding(s)".format(
        PROG, len(rows), count("answers"), count("no answer"), count("not checked"),
        len(report["findings"])))  # type: ignore[arg-type]


def run(root: pathlib.Path, doc: Optional[str] = None, only: Optional[List[str]] = None,
        probe: bool = True, timeout: float = 1.0, as_json: bool = False) -> int:
    try:
        code, report = read(root, doc, only, probe, timeout)
    except (OSError, UnicodeError, ValueError) as exc:
        print("{}: {}".format(PROG, exc), file=sys.stderr)
        return 2
    if as_json:
        print(json.dumps(report, indent=2))
    else:
        show(report)
    return code


# ---------------------------------------------------------------- self-test

GOOD = (
    "# Neighbours\n\n"
    "| name | how to reach it | family | cost | streams | live |\n"
    "|---|---|---|---|---|---|\n"
    "| s35 | LM Studio on this machine (port {open}) | Qwen | free | yes | yes |\n"
    "| codex | the `outside` server, codex_ask | OpenAI | plan | no | yes |\n"
    "| gemma | Ollama at http://127.0.0.1:{closed} | Gemma | free | yes | no |\n\n"
    "Seats: s35, codex          <!-- written by the first townhall -->\n\n"
    "## Live\n"
    "delivery: hybrid           <!-- all at once | streamed | hybrid -->\n"
    "filibuster: 2 sentences\n"
)


def _write(path: pathlib.Path, text: str) -> pathlib.Path:
    """Write UTF-8 text with the newlines exactly as given, on every platform."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def _ports() -> Tuple[socket.socket, int, int]:
    """A listening socket and its port, and a port that nothing listens on."""
    gone = socket.socket()
    gone.bind(("127.0.0.1", 0))
    closed = gone.getsockname()[1]
    gone.close()
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    return server, server.getsockname()[1], closed


def _report(base: pathlib.Path, doc_text: Optional[str], config: Optional[str] = None,
            doc: Optional[str] = "doc", only: Optional[List[str]] = None,
            probe: bool = False) -> Tuple[int, Dict[str, object]]:
    if doc_text is not None:
        _write(base / "neighbours.md", doc_text)
    root = base / "project"
    root.mkdir(exist_ok=True)
    if config is not None:
        _write(root / "pitmaster.md", config)
    target = str(base / "neighbours.md") if doc == "doc" else doc
    return read(root, target, only, probe, 0.5)


def _plain(base: pathlib.Path) -> str:
    return GOOD.format(open=1234, closed=11434)


def _table_case(change: Callable[[str], str], expected: int, says: str = "") -> Callable[[pathlib.Path], bool]:
    def case(base: pathlib.Path) -> bool:
        code, report = _report(base, change(_plain(base)))
        return code == expected and says in json.dumps(report)
    return case


def _case_reads_everything(base: pathlib.Path) -> bool:
    code, report = _report(base, _plain(base))
    rows = report["neighbours"]  # type: List[Dict[str, object]]
    return (code == 0 and [r["name"] for r in rows] == ["s35", "codex", "gemma"]
            and report["seats"] == ["s35", "codex"] and [r["seat"] for r in rows] == [True, True, False]
            and report["live"] == {"delivery": "hybrid", "filibuster": "2 sentences"}
            and rows[0]["address"] == "127.0.0.1:1234" and rows[1]["address"] is None
            and rows[2]["address"] == "127.0.0.1:11434" and rows[0]["now"] == "not probed")


def _case_probe(base: pathlib.Path) -> bool:
    server, open_port, closed_port = _ports()
    try:
        code, report = _report(base, GOOD.format(open=open_port, closed=closed_port), probe=True)
    finally:
        server.close()
    now = [r["now"] for r in report["neighbours"]]  # type: ignore[union-attr]
    return code == 0 and now == ["answers", "not checked (reached through an agent's tool)", "no answer"]


def _case_live_defaults(base: pathlib.Path) -> bool:
    text = _plain(base)
    code, report = _report(base, text[:text.index("## Live")])
    return code == 0 and report["live"] == LIVE_DEFAULTS


def _case_no_seats_note(base: pathlib.Path) -> bool:
    code, report = _report(base, re.sub(r"Seats:.*\n", "", _plain(base)))
    return code == 0 and report["seats"] is None and "no Seats line yet" in str(report["notes"])


def _case_streamed_note(base: pathlib.Path) -> bool:
    code, report = _report(base, _plain(base).replace("delivery: hybrid", "delivery: streamed"))
    return code == 0 and "codex (streams: no) cannot sit live" in str(report["notes"])


def _case_narrow_by_project(base: pathlib.Path) -> bool:
    code, report = _report(base, _plain(base), config="neighbours: gemma, s35   # this project only\n")
    return code == 0 and [r["name"] for r in report["neighbours"]] == ["s35", "gemma"]  # type: ignore[union-attr]


def _case_narrow_unknown(base: pathlib.Path) -> bool:
    code, report = _report(base, _plain(base), config="neighbours: s35, gpt9\n")
    return (code == 1 and "pitmaster.md names gpt9" in str(report["findings"])
            and [r["name"] for r in report["neighbours"]] == ["s35"])  # type: ignore[union-attr]


def _case_only_narrows_further(base: pathlib.Path) -> bool:
    code, report = _report(base, _plain(base), config="neighbours: s35, codex\n", only=["codex", "gemma"])
    return (code == 1 and "--only names gemma" in str(report["findings"])
            and [r["name"] for r in report["neighbours"]] == ["codex"])  # type: ignore[union-attr]


def _case_project_points_elsewhere(base: pathlib.Path) -> bool:
    _write(base / "project" / "docs" / "neighbours.md", _plain(base).replace("| gemma |", "| llama |"))
    code, report = _report(base, None, config="neighbours: docs/neighbours.md\n", doc=None)
    return (code == 0 and report["source"] == "pitmaster.md"
            and "llama" in [r["name"] for r in report["neighbours"]])  # type: ignore[union-attr]


def _case_doc_flag_wins(base: pathlib.Path) -> bool:
    _write(base / "project" / "docs" / "neighbours.md", "nothing\n")
    code, report = _report(base, _plain(base), config="neighbours: docs/neighbours.md\n")
    return code == 0 and report["source"] == "--doc"


def _case_fenced_config_ignored(base: pathlib.Path) -> bool:
    code, report = _report(base, _plain(base), config="```\nneighbours: s35\n```\n")
    return code == 0 and report["narrowed_to"] is None


def _case_missing_doc(base: pathlib.Path) -> bool:
    code, report = _report(base, None, doc=str(base / "absent.md"))
    return code == 1 and "no neighbours doc here" in str(report["findings"])


def _case_bad_root(base: pathlib.Path) -> bool:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return run(base / "absent", probe=False) == 2


def _case_cli(base: pathlib.Path) -> bool:
    """From a lone copy of its folder: the default doc under HOME, --json, --no-probe and a usage error."""
    solo = base / "solo"
    solo.mkdir()
    script = solo / "neighbours.py"
    shutil.copy(str(pathlib.Path(__file__).resolve()), str(script))
    home = base / "home"
    _write(home / ".pitmaster" / "neighbours.md", _plain(base))
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home))

    def call(*args: str) -> "subprocess.CompletedProcess[str]":
        return subprocess.run([sys.executable, str(script)] + list(args), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True, encoding="utf-8",
                              env=env, cwd=str(base))
    plain = call("--no-probe")
    as_json = call("--no-probe", "--json")
    report = json.loads(as_json.stdout)
    return (plain.returncode == 0 and "neighbours: 3 neighbour(s)" in plain.stdout
            and "Live: delivery hybrid, filibuster 2 sentences" in plain.stdout
            and as_json.returncode == 0 and report["source"] == "default"
            and call("--timeout", "-1").returncode == 2
            and call("--doc", str(base / "absent.md")).returncode == 1)


# (name, change to the good doc, expected exit, text the report must hold)
_TABLE = [
    ("red: cost not free, plan or paid", lambda t: t.replace("| plan |", "| cheap |"), 1, "cost \\\"cheap\\\""),
    ("red: streams not yes or no", lambda t: t.replace("| Qwen | free | yes |", "| Qwen | free | sometimes |"),
     1, "streams \\\"sometimes\\\""),
    ("red: live not yes or no", lambda t: t.replace("| yes | no |", "| yes | later |"), 1, "live \\\"later\\\""),
    ("red: a name listed twice", lambda t: t.replace("| gemma |", "| S35 |"), 1, "listed twice"),
    ("red: a row with no way to reach it", lambda t: t.replace("the `outside` server, codex_ask", ""), 1,
     "no way to reach it"),
    ("red: a row short of a cell", lambda t: t.replace("| Gemma | free | yes | no |", "| Gemma | free | yes |"),
     1, "row has 5 cells"),
    ("red: a seat not on the list", lambda t: t.replace("Seats: s35, codex", "Seats: s35, gpt9"), 1,
     "Seats names gpt9"),
    ("red: delivery not one of the three", lambda t: t.replace("delivery: hybrid", "delivery: fast"), 1,
     "delivery \\\"fast\\\""),
    ("red: no table", lambda t: "# Neighbours\n\nNone yet.\n", 1, "no neighbours table"),
    ("red: the only table is fenced", lambda t: "```markdown\n" + t + "```\n", 1, "no neighbours table"),
    ("red: a table with the wrong header", lambda t: t.replace("| how to reach it |", "| server |"), 1,
     "no neighbours table"),
    ("optional columns may be left out", lambda t: "| name | how to reach it |\n|---|---|\n| s35 | port 1234 |\n",
     0, "\"s35\""),
    ("columns in another order", lambda t: "| name | family | how to reach it |\n|:-|:-|:-|\n"
     "| codex | OpenAI | outside |\n", 0, "\"OpenAI\""),
    ("CRLF line endings", lambda t: t.replace("\n", "\r\n"), 0, "2 sentences"),
    ("a bold Seats line", lambda t: t.replace("Seats:", "**Seats:**"), 0, "\"seats\": [\"s35\""),
    ("delivery outside Live is ignored", lambda t: t.replace("## Live\ndelivery: hybrid", "delivery: fast\n## Live"),
     0, "\"delivery\": \"hybrid\""),
    ("an IPv6 URL", lambda t: t.replace("http://127.0.0.1:", "http://[::1]:"), 0, "\"::1:11434\""),
]

_CASES: List[Tuple[str, Callable[[pathlib.Path], bool]]] = (
    [
        ("reads the table, Seats and Live", _case_reads_everything),
        ("probes: answers, not checked, no answer", _case_probe),
        ("Live defaults when the section is absent", _case_live_defaults),
        ("no Seats line is a note, not a finding", _case_no_seats_note),
        ("delivery streamed notes a seat that cannot sit live", _case_streamed_note),
        ("pitmaster.md narrows to names", _case_narrow_by_project),
        ("red: pitmaster.md names an unknown neighbour", _case_narrow_unknown),
        ("red: --only narrows further, never widens", _case_only_narrows_further),
        ("pitmaster.md points to another doc", _case_project_points_elsewhere),
        ("--doc wins over pitmaster.md", _case_doc_flag_wins),
        ("a fenced neighbours line in pitmaster.md is ignored", _case_fenced_config_ignored),
        ("red: no doc", _case_missing_doc),
        ("usage: a root that does not exist", _case_bad_root),
    ]
    + [(row[0], _table_case(*row[1:])) for row in _TABLE]
    + [("copy-one-folder: default doc, --json, usage", _case_cli)]
)


def self_test() -> int:
    passed = failed = 0
    for name, case in _CASES:
        with tempfile.TemporaryDirectory(prefix="neighbours-") as tmp:
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
    parser.add_argument("--root", default=".", help="project root holding pitmaster.md (default: current directory)")
    parser.add_argument("--doc", help="the neighbours doc to read; wins over pitmaster.md and the default")
    parser.add_argument("--only", help="comma-separated names to narrow the list to")
    parser.add_argument("--no-probe", action="store_true", help="do not check who answers")
    parser.add_argument("--timeout", default="1", help="seconds to wait for each answer (default: 1)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if args.self_test:
        return self_test()
    try:
        timeout = float(args.timeout)
        if not 0 < timeout <= 60:
            raise ValueError
    except ValueError:
        parser.print_usage(sys.stderr)
        print("{}: --timeout must be a number of seconds above 0 and at most 60".format(PROG), file=sys.stderr)
        return 2
    only = names(args.only) if args.only is not None else None
    if only is not None and not only:
        print("{}: --only must name at least one neighbour".format(PROG), file=sys.stderr)
        return 2
    return run(pathlib.Path(args.root), args.doc, only, not args.no_probe, timeout, args.json)


if __name__ == "__main__":
    sys.exit(main())
