#!/usr/bin/env python3
"""spend-ledger.py: what a session actually spent, read from its own transcript, as one ledger row.

Four jobs:
  measure    read session transcripts on this machine and print one ledger row per session
  report     read the ledger file and print spend beside what landed, over a window
  reading    log one reading of a usage meter (percent of its limit) to the readings file
  calibrate  pair consecutive readings of a meter with the tokens every Claude Code session on this machine
             spent between them, and fit K (tokens per point) for budget-select

Platforms it reads (any other is written by hand; see SKILL.md):
  claude   Claude Code: ~/.claude/projects/<project>/<session>.jsonl, plus <session>/subagents/*.jsonl for
           the workers it spawned. Each assistant message id counts once, at its largest output_tokens
           (streamed records repeat an id with partial usage), so the order of the records does not matter.
  codex    Codex: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl. Its token_count events carry a running
           total, so a window's spend is the last total in it less the last total before it. Their
           rate_limits carry the plan meter's reading, so the meter's start and end need no asking.

Tokens are the measurement. Two figures are derived from them, and each is labelled as derived:
  list $    a list-price proxy from prices.tsv beside this script (dated; a model with no row shows "-").
            It puts models on one scale. It is not a bill and it is not the plan's meter.
  est pts   this session's share of the meter's movement, by list-price weight, when other sessions on the
            same platform overlapped its window. Alone on the meter, the movement is the session's own.

Usage:
  spend-ledger.py measure [--platform claude|codex] [--project DIR | --any-project]
                          [--session ID | --latest | --all] [--since WHEN] [--until WHEN]
                          [--meter START END] [--lead TEXT] [--landed TEXT] [--note TEXT] [--header]
  spend-ledger.py report  [--ledger FILE] [--since DATE] [--until DATE]
  spend-ledger.py reading PERCENT [--meter NAME] [--at WHEN] [--readings FILE]
  spend-ledger.py calibrate [--meter NAME] [--since WHEN] [--models TEXT] [--readings FILE] [--write LOG]
  spend-ledger.py --self-test

WHEN is a date (YYYY-MM-DD, the day included whole) or an ISO time (2026-10-20T18:00:00Z, the instant
included); a time without an offset is UTC. --meter takes two readings of one meter in points (percent of
the limit), from the platform's usage tool or the user. The ledger holds numbers and links only, never
prompt text or file contents. The readings file ($PITMASTER_HOME/usage-readings.tsv, default
~/.pitmaster/usage-readings.tsv) is per machine and never in a repository: one line per reading, its UTC time,
the meter's name and the percent. A reading lower than the one before it is a reset, never a pair. A pair whose
meter moved under 2 points (readings are often whole percents) or whose K is under half the median (the meter
moved for spending this machine cannot see: a phone, the web) is flagged and left out of the fit. Python 3.8+, standard library only. Exit 1 on a failed self-test, 2 on a
usage error.
"""
import argparse
import collections
import datetime
import glob
import io
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
COLUMNS = ["date", "session", "platform", "lead", "window (UTC)", "tokens", "workers", "models",
           "list $", "meter", "landed", "note"]
KINDS = ("fresh", "cached", "written", "out")
TIME = re.compile(r"(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d)(?::(\d\d)(?:\.(\d+))?)?\s*(Z|[+-]\d\d:?\d\d)?$")
CLAUDE_FAMILIES = ("opus", "sonnet", "haiku", "fable")


# ---------------------------------------------------------------- time and the window

def parse_time(text):
    """An ISO timestamp as an aware UTC datetime; None when it is not one. No offset means UTC."""
    m = TIME.match(str(text).strip())
    if not m:
        return None
    y, mo, d, h, mi, sec, frac, off = m.groups()
    try:
        dt = datetime.datetime(int(y), int(mo), int(d), int(h), int(mi), int(sec or 0),
                               int(((frac or "0") + "000000")[:6]), datetime.timezone.utc)
    except ValueError:
        return None
    if off and off != "Z":
        sign = -1 if off[0] == "-" else 1
        digits = off[1:].replace(":", "")
        dt -= sign * datetime.timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
    return dt


class Window:
    """--since/--until. A bound without a time is a whole UTC day; a bound with a time is an instant."""

    def __init__(self, since="", until=""):
        self.since, self.until = since or "", until or ""
        for bound in (self.since, self.until):
            if bound and not re.fullmatch(r"\d{4}-\d\d-\d\d", bound) and self._instant(bound) is None:
                raise ValueError(f"not a date or an ISO time: {bound!r}")
        self.since_t, self.until_t = self._instant(self.since), self._instant(self.until)

    @staticmethod
    def _instant(bound):
        if not bound or not re.search(r"[T ]\d\d:", bound):
            return None
        return parse_time(bound)

    def has(self, ts):
        ts = str(ts or "")
        if not ts:
            return False
        day = ts[:10]
        if self.since and self.since_t is None and day < self.since:
            return False
        if self.until and self.until_t is None and day > self.until:
            return False
        if self.since_t or self.until_t:
            t = parse_time(ts)
            if t is None or (self.since_t and t < self.since_t) or (self.until_t and t > self.until_t):
                return False
        return True

    def before(self, ts):
        """True when ts falls before the window's start (used to take a running total's baseline)."""
        ts = str(ts or "")
        if self.since_t:
            t = parse_time(ts)
            return t is not None and t < self.since_t
        return bool(self.since) and ts[:10] < self.since

    def earliest(self):
        """The window's start as an epoch second, for skipping files last written before it."""
        if self.since_t:
            return self.since_t.timestamp()
        if self.since:
            return datetime.datetime.strptime(self.since, "%Y-%m-%d").replace(
                tzinfo=datetime.timezone.utc).timestamp()
        return 0


# ---------------------------------------------------------------- sessions

class Session:
    def __init__(self, platform, sid):
        self.platform, self.id = platform, sid
        self.name = ""
        self.cwd = ""
        self.start = self.end = ""
        self.usage = collections.defaultdict(collections.Counter)   # (model, role) -> Counter of KINDS
        self.worker_files = set()
        self.effort = ""
        self.meter = None          # (start %, end %, label) when the transcript carries it

    def see(self, ts):
        if ts and (not self.start or ts < self.start):
            self.start = ts
        if ts and (not self.end or ts > self.end):
            self.end = ts

    def add(self, model, role, counts):
        for k in KINDS:
            self.usage[(model, role)][k] += counts.get(k, 0)

    def total(self, role=None):
        c = collections.Counter()
        for (_, r), u in self.usage.items():
            if role is None or r == role:
                c.update(u)
        return c

    def lead_model(self):
        outs = collections.Counter()
        for (m, r), u in self.usage.items():
            if r == "lead":
                outs[m] += u["out"]
        return outs.most_common(1)[0][0] if outs else ""


def slug(path):
    """Claude Code's project folder name for a working directory."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(path))


def read_claude(root, win, project=None):
    """Claude Code sessions with activity in the window. A subagents/ file joins its lead's session as
    a worker. A message id counts once per session, at its largest output_tokens."""
    files = glob.glob(os.path.join(root, "*", "*.jsonl"))
    files += glob.glob(os.path.join(root, "*", "*", "subagents", "*.jsonl"))
    want = slug(project) if project else None
    floor = win.earliest()
    best, sessions = {}, {}
    for path in files:
        rel = os.path.relpath(path, root).split(os.sep)
        if want and rel[0] != want:
            continue
        try:
            if floor and os.path.getmtime(path) < floor:
                continue
        except OSError:
            continue
        worker = len(rel) == 4 and rel[2] == "subagents"
        sid = rel[1] if worker else rel[1][:-len(".jsonl")]
        key = (rel[0], sid)
        with open(path, errors="replace") as fh:
            for line in fh:
                if '"assistant"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("type") != "assistant" or not win.has(rec.get("timestamp")):
                    continue
                msg = rec.get("message") or {}
                model, mid = msg.get("model") or "", msg.get("id")
                if not mid or not model or model.startswith("<"):
                    continue
                s = sessions.get(key)
                if s is None:
                    s = sessions[key] = Session("claude", sid)
                    s.name = rel[0]
                if rec.get("cwd") and not worker and (not s.cwd or rec.get("timestamp", "") < s.start):
                    s.cwd = rec["cwd"]   # the folder the session started in, not where a shell wandered
                s.see(rec.get("timestamp"))
                role = "worker" if worker or rec.get("isSidechain") else "lead"
                if worker:
                    s.worker_files.add(rel[3])
                u = msg.get("usage") or {}
                counts = {"fresh": u.get("input_tokens", 0) or 0,
                          "written": u.get("cache_creation_input_tokens", 0) or 0,
                          "cached": u.get("cache_read_input_tokens", 0) or 0,
                          "out": u.get("output_tokens", 0) or 0}
                prev = best.get((key, mid))
                if prev is None or counts["out"] >= prev[2]["out"]:
                    best[(key, mid)] = (model, role, counts)
    for (key, _), (model, role, counts) in best.items():
        sessions[key].add(model, role, counts)
    for s in sessions.values():
        s.name = os.path.basename(s.cwd) if s.cwd else s.name
    return list(sessions.values())


def read_codex(root, win, project=None):
    """Codex sessions with activity in the window. Each token_count's running total is turned into a
    step and charged to the model of the turn it fell in; steps before the window are the baseline."""
    want = os.path.normcase(os.path.abspath(project)) if project else None
    floor = win.earliest()
    out = []
    for path in glob.glob(os.path.join(root, "*", "*", "*", "*.jsonl")):
        try:
            if floor and os.path.getmtime(path) < floor:
                continue
        except OSError:
            continue
        s = Session("codex", os.path.basename(path)[:-len(".jsonl")])
        model, prev, meter_first, meter_last, label = "", None, None, None, ""
        with open(path, errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                p = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
                kind, ts = rec.get("type"), rec.get("timestamp")
                if kind == "session_meta":
                    s.id = p.get("id") or s.id
                    s.cwd = p.get("cwd") or ""
                elif kind == "turn_context":
                    model = p.get("model") or model
                    s.effort = p.get("effort") or s.effort
                elif kind == "event_msg" and p.get("type") == "token_count":
                    tot = ((p.get("info") or {}).get("total_token_usage")) or {}
                    if not tot:
                        continue
                    cur = {"fresh": max(0, tot.get("input_tokens", 0) - tot.get("cached_input_tokens", 0)),
                           "cached": tot.get("cached_input_tokens", 0),
                           "written": tot.get("cache_write_input_tokens", 0),
                           "out": tot.get("output_tokens", 0)}
                    if win.has(ts):
                        step = {k: cur[k] - (prev or {}).get(k, 0) for k in KINDS}
                        s.add(model or "unknown", "lead", step)
                        s.see(ts)
                        prim = ((p.get("rate_limits") or {}).get("primary")) or {}
                        if "used_percent" in prim:
                            meter_first = prim["used_percent"] if meter_first is None else meter_first
                            meter_last = prim["used_percent"]
                            label = window_label(prim.get("window_minutes"))
                    if win.has(ts) or win.before(ts):
                        prev = cur
        # Both sides go through abspath and normcase, so a recorded "/w/alpha" and a --project of "/w/alpha"
        # still match on Windows, where abspath adds a drive letter.
        have = os.path.normcase(os.path.abspath(s.cwd)) if s.cwd else ""
        if want and not (have == want or have.startswith(want.rstrip(os.sep) + os.sep)):
            continue
        if not s.start:
            continue
        s.name = os.path.basename(s.cwd) or s.id[:8]
        if meter_first is not None:
            s.meter = (meter_first, meter_last, label)
        out.append(s)
    return out


def window_label(minutes):
    return {10080: "weekly", 300: "5-hour"}.get(minutes, f"{minutes}-min" if minutes else "")


# ---------------------------------------------------------------- prices and figures

def load_prices(path):
    """[(substring, in $/M, out $/M, cache-write x, cache-read x)] from prices.tsv; comment lines skipped."""
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path) as fh:
        for line in fh:
            if not line.strip() or line.startswith("#"):
                continue
            cols = line.rstrip("\n").split("\t")
            if len(cols) < 5:
                continue
            rows.append((cols[0], float(cols[1]), float(cols[2]), float(cols[3]), float(cols[4])))
    return rows


def price_of(prices, model):
    return next((r for r in prices if r[0] in (model or "")), None)


def cost(prices, model, u):
    """List-price proxy in dollars; None when the model has no row."""
    r = price_of(prices, model)
    if r is None:
        return None
    _, pi, po, cw, cr = r
    return (u["fresh"] * pi + u["written"] * pi * cw + u["cached"] * pi * cr + u["out"] * po) / 1e6


def session_cost(prices, s, role=None):
    """(dollars over the priced models, True if every model was priced)."""
    total, whole = 0.0, True
    for (m, r), u in s.usage.items():
        if role and r != role:
            continue
        c = cost(prices, m, u)
        if c is None:
            whole = False
        else:
            total += c
    return total, whole


def human(n):
    n = int(n)
    if n < 1000:
        return str(n)
    if n < 10000:
        return f"{n / 1e3:.1f}k"
    if n < 1e6:
        return f"{n / 1e3:.0f}k"
    return f"{n / 1e6:.1f}M"


def short_model(model):
    return next((f for f in CLAUDE_FAMILIES if f in model), model)


def models_cell(prices, s):
    """Each model's share: of list $ when every model is priced, else of output tokens."""
    _, whole = session_cost(prices, s)
    share = collections.Counter()
    for (m, _), u in s.usage.items():
        share[short_model(m)] += cost(prices, m, u) if whole else u["out"]
    total = sum(share.values())
    if not total:
        return "-"
    basis = "" if whole else " (of output)"
    return " · ".join(f"{m} {100 * v / total:.0f}%" for m, v in share.most_common()) + basis


def workers_cell(prices, s):
    if not s.total("worker")["out"] and not s.worker_files:
        return "none"
    allc, whole = session_cost(prices, s)
    wc, _ = session_cost(prices, s, "worker")
    if whole and allc:
        pct = 100 * wc / allc
    else:
        out = s.total()["out"]
        pct = 100 * s.total("worker")["out"] / out if out else 0
    files = len(s.worker_files)
    return f"{pct:.0f}%" + (f" ({files} file{'s' if files != 1 else ''})" if files else "")


def window_cell(s):
    a, b = s.start[:16].replace("T", " "), s.end[:16].replace("T", " ")
    if a[:10] == b[:10]:
        return f"{a[11:]}–{b[11:]}"
    return f"{a[5:]} → {b[5:]}"


def overlap(sessions_all, s):
    """Other sessions on the same platform with activity inside s's window."""
    return [o for o in sessions_all if (o.platform, o.id) != (s.platform, s.id)]


def meter_cell(prices, s, meter, others):
    """The meter's start → end, whether it was shared, and this session's estimated points."""
    if meter is None and s.meter is not None:
        meter = s.meter
    if meter is None:
        return "not read"
    start, end = meter[0], meter[1]
    label = meter[2] if len(meter) > 2 and meter[2] else ""
    delta = end - start
    text = f"{start:g} → {end:g}" + (f" {label}" if label else "")
    if delta == 0 and float(start).is_integer():
        # A whole-percent reading that did not move says "under a point", not "free".
        return text + (" · alone here" if not others else f" · shared with {len(others)}") + " · under 1 pt"
    if not others:
        return text + f" · alone here · {delta:g} pts"
    mine, whole = session_cost(prices, s)
    pool = mine
    for o in others:
        c, w = session_cost(prices, o)
        pool += c
        whole = whole and w
    if not whole or not pool:
        return text + f" · shared with {len(others)} · est n/a (unpriced)"
    return text + f" · shared with {len(others)} · est {delta * mine / pool:.1f} pts"


def row(prices, s, lead="", meter=None, others=(), landed="", note=""):
    t = s.total()
    tokens = " · ".join(f"{k} {human(t[k])}" for k in KINDS if t[k] or k in ("fresh", "out"))
    dollars, whole = session_cost(prices, s)
    money = "-" if not dollars and not whole else f"${dollars:,.2f}" + ("" if whole else "+")
    if not lead:
        lead = short_model(s.lead_model()) if s.platform == "claude" else s.lead_model()
        lead += f", {s.effort}" if s.effort else ""
    cells = [s.start[:10], f"{s.name} [{s.id[:8]}]", s.platform, lead or "-", window_cell(s), tokens,
             workers_cell(prices, s), models_cell(prices, s), money, meter_cell(prices, s, meter, others),
             landed or "not recorded", note or ""]
    return "| " + " | ".join(c.replace("|", "/") for c in cells) + " |"


def header():
    return "| " + " | ".join(COLUMNS) + " |\n|" + "---|" * len(COLUMNS)


# ---------------------------------------------------------------- report

def parse_ledger(text):
    """Ledger rows as dicts, one per session: a later row for the same session replaces an earlier one
    (that is how a correction is written)."""
    rows = collections.OrderedDict()
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != len(COLUMNS) or cells[0] == "date" or set(cells[0]) <= set("-: "):
            continue
        r = dict(zip(COLUMNS, cells))
        rows.pop(r["session"], None)
        rows[r["session"]] = r
    return list(rows.values())


def dollars_of(cell):
    m = re.match(r"\$([\d,]+(?:\.\d+)?)", cell)
    return float(m.group(1).replace(",", "")) if m else None


def points_of(cell):
    m = re.search(r"est ([\d.]+) pts", cell) or re.search(r"alone here · (-?[\d.]+) pts", cell)
    return float(m.group(1)) if m else None


LAND_KINDS = ("merged", "opened", "ratified", "committed")
LAND_PART = re.compile(r"(\d+)\s+(" + "|".join(LAND_KINDS) + r")\b")


def landed_split(cell):
    """The landed cell's split by kind, {"merged": 2, ...}; None for a bare count or a cell not recorded.

    A split is written "2 merged, 1 opened, 1 committed". An old-style count ("2: #18, #19", or just "2")
    has no split."""
    if re.match(r"\d+\s*(:|$)", cell):
        return None
    got = {}
    for n, kind in LAND_PART.findall(cell):
        got[kind] = got.get(kind, 0) + int(n)
    return got or None


def landed_of(cell):
    """The count a landed cell holds: the sum of its split, else the number it starts with; None when it was
    not recorded."""
    if cell.startswith("not recorded") or cell in ("", "?"):
        return None
    split = landed_split(cell)
    if split:
        return sum(split.values())
    m = re.match(r"(\d+)", cell)
    return int(m.group(1)) if m else None


def report(text, since="", until="", out=sys.stdout):
    rows = [r for r in parse_ledger(text)
            if (not since or r["date"] >= since) and (not until or r["date"] <= until)]
    span = f"{since or 'start'}..{until or 'now'}"
    if not rows:
        out.write(f"spend-ledger report {span}: no rows\n")
        return
    money = sum(dollars_of(r["list $"]) or 0 for r in rows)
    pts = [points_of(r["meter"]) for r in rows]
    known = [p for p in pts if p is not None]
    lands = [landed_of(r["landed"]) for r in rows]
    counted = sum(n for n in lands if n)
    out.write(f"spend-ledger report {span}: {len(rows)} sessions, ${money:,.2f} list-price proxy, "
              f"{sum(known):.1f} pts over {len(known)} metered sessions, {counted} landings\n")
    paid = [(dollars_of(r["list $"]) or 0, n) for r, n in zip(rows, lands) if n is not None]
    if counted:
        per_pts = [(p, n) for p, n in zip(pts, lands) if p is not None and n is not None]
        line = f"  per landing: ${sum(d for d, _ in paid) / counted:,.2f}"
        pl = sum(n for _, n in per_pts)
        if pl:
            line += f", {sum(p for p, _ in per_pts) / pl:.2f} pts"
        out.write(line + "\n")
    splits = [landed_split(r["landed"]) for r in rows]
    kinds = collections.Counter()
    for sp in splits:
        kinds.update(sp or {})
    if kinds:
        unsplit = sum(n for sp, n in zip(splits, lands) if sp is None and n)
        out.write("  landed by kind: " + ", ".join(f"{kinds[k]} {k}" for k in LAND_KINDS if kinds[k])
                  + (f", {unsplit} not split" if unsplit else "") + "\n")
        if kinds["merged"]:
            spent = sum(dollars_of(r["list $"]) or 0 for r, sp in zip(rows, splits) if sp)
            out.write(f"  per merged landing (rows with a split only): ${spent / kinds['merged']:,.2f}\n")
    empty = [r for r, n in zip(rows, lands) if n == 0]
    if empty:
        out.write(f"  spent with nothing landed: {len(empty)} sessions, "
                  f"${sum(dollars_of(r['list $']) or 0 for r in empty):,.2f}: "
                  + "; ".join(f"{r['session']} ({r['note'] or 'no note'})" for r in empty) + "\n")
    unknown = sum(1 for n in lands if n is None)
    if unknown:
        out.write(f"  landed not recorded: {unknown} sessions\n")
    by = collections.defaultdict(lambda: [0, 0.0, 0])
    for r, n in zip(rows, lands):
        b = by[r["lead"]]
        b[0] += 1
        b[1] += dollars_of(r["list $"]) or 0
        b[2] += n or 0
    out.write("  by lead: " + "; ".join(f"{k}: {v[0]} sessions, ${v[1]:,.2f}, {v[2]} landed"
                                       for k, v in sorted(by.items(), key=lambda kv: -kv[1][1])) + "\n")
    top = sorted(rows, key=lambda r: -(dollars_of(r["list $"]) or 0))[:3]
    out.write("  largest: " + "; ".join(f"{r['session']} {r['list $']} ({r['landed']})" for r in top) + "\n")


# ---------------------------------------------------------------- measure

def measure(a, out=sys.stdout):
    prices = load_prices(os.path.join(HERE, "prices.tsv"))
    win = Window(a.since, a.until)
    roots = {"claude": a.claude_root, "codex": a.codex_root}
    readers = {"claude": read_claude, "codex": read_codex}
    platforms = [a.platform] if a.platform else ["claude", "codex"]
    project = None if a.any_project else (a.project or os.getcwd())
    found = []
    for p in platforms:
        found += readers[p](roots[p], win, project)
    if a.session:
        found = [s for s in found if s.id.startswith(a.session) or a.session in s.id]
    elif not a.all:
        found = sorted(found, key=lambda s: s.end)[-1:]
    if not found:
        out.write("spend-ledger: no session with activity matches\n")
        return 1
    if a.header:
        out.write(header() + "\n")
    for s in sorted(found, key=lambda s: s.start):
        others = []
        if a.meter or s.meter:
            near = Window(s.start, s.end)
            others = [o for o in readers[s.platform](roots[s.platform], near, None)
                      if o.id != s.id]
        meter = (a.meter[0], a.meter[1], "") if a.meter else None
        out.write(row(prices, s, a.lead, meter, others, a.landed, a.note) + "\n")
    return 0


# ---------------------------------------------------------------- reading and calibrate

MIN_MOVE = 2.0   # points; a smaller movement of a whole-percent meter is mostly rounding


def readings_path(path=""):
    if path:
        return path
    home = os.environ.get("PITMASTER_HOME") or os.path.expanduser("~/.pitmaster")
    return os.path.join(home, "usage-readings.tsv")


def utc_iso(t):
    return t.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def add_reading(path, percent, meter="weekly", at=""):
    if not 0 <= percent <= 100:
        raise ValueError(f"a reading is a percent from 0 to 100, not {percent:g}")
    if "\t" in meter or not meter.strip():
        raise ValueError("a meter's name is one or more words with no tab")
    when = parse_time(at) if at else datetime.datetime.now(datetime.timezone.utc)
    if when is None:
        raise ValueError(f"not an ISO time: {at!r}")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    line = f"{utc_iso(when)}\t{meter.strip()}\t{percent:g}\n"
    with open(path, "a") as fh:
        fh.write(line)
    return line.rstrip("\n")


def load_readings(path, meter="weekly", since=""):
    """[(time, percent)] for one meter, oldest first; malformed lines are skipped."""
    out = []
    if not os.path.exists(path):
        return out
    floor = parse_time(since) if since and re.search(r"[T ]\d\d:", since) else None
    with open(path) as fh:
        for line in fh:
            cols = line.rstrip("\n").split("\t")
            if len(cols) != 3 or cols[1] != meter:
                continue
            t = parse_time(cols[0])
            try:
                pct = float(cols[2])
            except ValueError:
                continue
            if t is None or (floor and t < floor) or (since and not floor and cols[0][:10] < since):
                continue
            out.append((t, pct))
    return sorted(out)


def pairs_of(readings):
    """Consecutive readings that rose; a drop is a reset and starts afresh."""
    return [(a, b) for a, b in zip(readings, readings[1:]) if b[1] > a[1]]


def calibrate(readings, claude_root, models="", prices=()):
    """One dict per pair, with K and a flag, and the fit over the unflagged pairs."""
    rows = []
    for (t0, p0), (t1, p1) in pairs_of(readings):
        win = Window(utc_iso(t0), utc_iso(t1))
        tokens, dollars = 0, 0.0
        for s in read_claude(claude_root, win, None):
            for (m, _), u in s.usage.items():
                if models and models not in m:
                    continue
                tokens += sum(u[k] for k in KINDS)
                dollars += cost(list(prices), m, u) or 0.0
        move = p1 - p0
        rows.append({"from": utc_iso(t0), "to": utc_iso(t1), "points": move, "tokens": tokens,
                     "k": tokens / move, "dollars": dollars, "flag": "small move" if move < MIN_MOVE else ""})
    ks = sorted(r["k"] for r in rows if not r["flag"])
    if ks:
        median = ks[len(ks) // 2] if len(ks) % 2 else (ks[len(ks) // 2 - 1] + ks[len(ks) // 2]) / 2
        for r in rows:
            if not r["flag"] and r["k"] < median / 2:
                r["flag"] = "low: spending this machine cannot see"
    kept = [r for r in rows if not r["flag"]]
    fit = None
    if kept:
        fit = sum(r["tokens"] for r in kept) / sum(r["points"] for r in kept)
    return rows, fit, len(kept)


FIT_HEAD = "## Fitted K (spend-ledger.py calibrate)"
FIT_COLS = "| from (UTC) | to (UTC) | meter | points | tokens | K (tokens/point) | list $ per point | flag |"


def fit_line(r, meter):
    per = r["dollars"] / r["points"] if r["points"] else 0
    return (f"| {r['from']} | {r['to']} | {meter} | {r['points']:g} | {human(r['tokens'])} | "
            f"{human(int(r['k']))} | ${per:.2f} | {r['flag'] or 'kept'} |")


def write_fits(log, rows, meter):
    """Append the pairs not already in the log under its Fitted K section; returns how many were added."""
    text = open(log).read() if os.path.exists(log) else ""
    have = set(re.findall(r"^\| (\S+) \| (\S+) \| ([^|]+?) \|", text, re.M))
    new = [r for r in rows if (r["from"], r["to"], meter) not in have]
    if not new:
        return 0
    add = ""
    if FIT_HEAD not in text:
        add += ("\n" if text and not text.endswith("\n\n") else "") + FIT_HEAD + "\n\n" + FIT_COLS + "\n" + \
            "|" + "---|" * 8 + "\n"
    add += "".join(fit_line(r, meter) + "\n" for r in new)
    if FIT_HEAD in text:
        start = text.index(FIT_HEAD)
        nxt = text.find("\n## ", start + len(FIT_HEAD))
        end = len(text) if nxt < 0 else nxt + 1
        body = text[start:end].rstrip("\n") + "\n" + add
        text = text[:start] + body + ("\n" + text[end:] if nxt >= 0 else "")
    elif "\n## Field notes" in text:   # a new section goes before the log's notes
        cut = text.index("\n## Field notes") + 1
        text = text[:cut] + add.lstrip("\n") + "\n" + text[cut:]
    else:
        text += add
    with open(log, "w") as fh:
        fh.write(text)
    return len(new)


def run_calibrate(a, out=sys.stdout):
    path = readings_path(a.readings)
    readings = load_readings(path, a.meter, a.since)
    if len(readings) < 2:
        out.write(f"spend-ledger: fewer than two '{a.meter}' readings in {path}; log some with 'reading'\n")
        return 1
    rows, fit, kept = calibrate(readings, a.claude_root, a.models, load_prices(os.path.join(HERE, "prices.tsv")))
    if not rows:
        out.write("spend-ledger: no pair of readings rose (each drop is a reset); nothing to fit yet\n")
        return 1
    out.write(FIT_COLS + "\n")
    for r in rows:
        out.write(fit_line(r, a.meter) + "\n")
    if fit is None:
        out.write("fit: none (every pair is flagged)\n")
    else:
        trust = "trusted" if kept >= 3 else "not yet trusted: budget-select wants three"
        out.write(f"fit: K = {human(int(fit))} tokens per point over {kept} kept pair(s), {trust}\n")
    if a.write:
        n = write_fits(a.write, rows, a.meter)
        out.write(f"wrote {n} new pair(s) to {a.write}\n")
    return 0


# ---------------------------------------------------------------- self-test

def self_test():
    """Fixture transcripts for both platforms and a fixture ledger. Each arm prints and counts; several
    are red arms that a plausible wrong implementation fails (noted beside them)."""
    prices = [("opus", 5, 25, 1.25, 0.1), ("haiku", 1, 5, 1.25, 0.1)]

    def asst(mid, model, out, ts, cwd="/w/alpha", side=False, fresh=0, cached=0):
        return json.dumps({"type": "assistant", "timestamp": ts, "cwd": cwd, "isSidechain": side,
                           "message": {"id": mid, "model": model, "usage": {
                               "input_tokens": fresh, "cache_read_input_tokens": cached,
                               "cache_creation_input_tokens": 0, "output_tokens": out}}})

    def tc(ts, inp, cached, out, pct=None):
        p = {"type": "token_count", "info": {"total_token_usage": {
            "input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out}}}
        if pct is not None:
            p["rate_limits"] = {"primary": {"used_percent": pct, "window_minutes": 10080}}
        return json.dumps({"type": "event_msg", "timestamp": ts, "payload": p})

    def ctx(ts, model, effort):
        return json.dumps({"type": "turn_context", "timestamp": ts, "payload": {"model": model, "effort": effort}})

    arms = []
    with tempfile.TemporaryDirectory() as tmp:
        croot = os.path.join(tmp, "claude")
        alpha = os.path.join(croot, slug("/w/alpha"))
        beta = os.path.join(croot, slug("/w/beta"))
        os.makedirs(os.path.join(alpha, "s-alpha-1", "subagents"))
        os.makedirs(beta)
        with open(os.path.join(alpha, "s-alpha-1.jsonl"), "w") as f:
            f.write("\n".join([
                asst("m1", "claude-opus-5-5", 40000, "2026-10-20T10:00:01Z"),   # final usage first ...
                asst("m1", "claude-opus-5-5", 10, "2026-10-20T10:00:00Z"),      # ... a partial repeat after
                asst("m2", "claude-opus-5-5", 40000, "2026-10-19T09:00:00Z"),   # an earlier day
                asst("m3", "<synthetic>", 5, "2026-10-20T10:30:00Z"),
                asst("m4", "claude-opus-5-5", 0, "2026-10-20T10:40:00Z", cwd="/w/alpha/sub"),  # a shell cd
            ]) + "\n")
        with open(os.path.join(alpha, "s-alpha-1", "subagents", "w1.jsonl"), "w") as f:
            f.write(asst("w1", "claude-haiku-5-5", 200000, "2026-10-20T11:00:00Z") + "\n")
        with open(os.path.join(beta, "s-beta-1.jsonl"), "w") as f:
            f.write(asst("m1", "claude-opus-5-5", 40000, "2026-10-20T10:15:00Z", cwd="/w/beta") + "\n")

        _touch(croot)
        win = Window("2026-10-20", "2026-10-20")
        got = {s.id: s for s in read_claude(croot, win)}
        a, b = got.get("s-alpha-1"), got.get("s-beta-1")
        arms += [
            # A last-wins dedupe keeps the 10-token partial; a summing one gives 40010.
            ("claude: an id counts once, at its largest output", a and a.total("lead")["out"] == 40000),
            ("claude: a subagents/ file joins its lead as a worker",
             a and a.total("worker")["out"] == 200000 and a.worker_files == {"w1.jsonl"}),
            ("claude: the window drops an earlier day", a and a.start.startswith("2026-10-20")),
            # Keying the dedupe by message id alone would drop beta's m1.
            ("claude: the same id in two sessions counts in both", b and b.total()["out"] == 40000),
            ("claude: a synthetic record is skipped", a and all(m != "<synthetic>" for m, _ in a.usage)),
            ("claude: --project keeps one project's sessions",
             [s.id for s in read_claude(croot, win, "/w/beta")] == ["s-beta-1"]),
            ("claude: the session is named by its working folder", a and a.name == "alpha"),
        ]
        dollars, whole = session_cost(prices, a)
        arms.append(("price: opus 40k out + haiku 200k out = $2.00", whole and round(dollars, 6) == 2.0))
        r = row(prices, a, landed="2: #18, #19")
        cells = [c.strip() for c in r.strip().strip("|").split("|")]
        arms += [
            ("row: twelve cells in the ledger's column order", len(cells) == len(COLUMNS)
             and cells[2] == "claude" and cells[3] == "opus" and cells[8] == "$2.00"),
            ("row: workers' share is of list $", cells[6] == "50% (1 file)"),
            ("row: model shares", cells[7] == "opus 50% · haiku 50%"),
            ("row: no meter reads 'not read'", cells[9] == "not read"),
        ]
        alone = meter_cell(prices, a, (40, 43, "weekly"), [])
        shared = meter_cell(prices, a, (40, 43, ""), [b])
        arms += [
            ("meter: alone, the movement is the session's own", alone.endswith("alone here · 3 pts")),
            # alpha $2.00, beta $1.00: alpha's share of 3 points is 2.0, not 3 and not 1.5.
            ("meter: shared, the share is by list-price weight", shared.endswith("shared with 1 · est 2.0 pts")),
            # A whole-percent meter that did not move must not read as zero points.
            ("meter: an unmoved whole-percent reading is 'under 1 pt'",
             meter_cell(prices, a, (42, 42, ""), [b]).endswith("shared with 1 · under 1 pt")
             and points_of(meter_cell(prices, a, (42, 42, ""), [])) is None),
        ]
        unpriced = Session("claude", "u")
        unpriced.add("mystery-model", "lead", {"out": 1000})
        arms += [
            ("price: an unpriced model shows '-' and shares by output",
             "| - |" in row(prices, unpriced) and models_cell(prices, unpriced).endswith("(of output)")),
            ("meter: an unpriced neighbour gives no estimate",
             meter_cell(prices, a, (40, 43, ""), [unpriced]).endswith("est n/a (unpriced)")),
        ]

        xroot = os.path.join(tmp, "codex")
        day = os.path.join(xroot, "2026", "10", "20")
        os.makedirs(day)
        with open(os.path.join(day, "rollout-x.jsonl"), "w") as f:
            f.write("\n".join([
                json.dumps({"type": "session_meta", "timestamp": "2026-10-20T08:00:00Z",
                            "payload": {"id": "codex-sess-1", "cwd": "/w/alpha"}}),
                ctx("2026-10-20T08:00:00Z", "gpt-x", "high"),
                tc("2026-10-20T08:01:00Z", 1000, 400, 100, 10.0),     # before the window: the baseline
                tc("2026-10-20T09:01:00Z", 3000, 1400, 300, 11.0),
                ctx("2026-10-20T09:30:00Z", "gpt-y", "xhigh"),
                tc("2026-10-20T09:31:00Z", 6000, 3400, 700, 12.5),
            ]) + "\n")
        _touch(xroot)
        xs = read_codex(xroot, Window("2026-10-20T09:00:00Z", ""), "/w/alpha")
        x = xs[0] if xs else None
        arms += [
            # Taking the running total as the spend would count the 08:01 tokens too.
            ("codex: a window's spend is the running total less its baseline",
             x and x.total()["out"] == 600),
            ("codex: fresh input is input less cached", x and x.total()["fresh"] == 2000
             and x.total()["cached"] == 3000),
            ("codex: each step is charged to its turn's model",
             x and x.usage[("gpt-x", "lead")]["out"] == 200 and x.usage[("gpt-y", "lead")]["out"] == 400),
            ("codex: the meter's start and end come from the transcript", x and x.meter == (11.0, 12.5, "weekly")),
            ("codex: the effort is the last turn's", x and x.effort == "xhigh"),
            ("codex: --project filters by working folder", read_codex(xroot, Window(), "/w/beta") == []),
        ]

    ledger = "\n".join([
        header(),
        "| 2026-10-19 | a [11111111] | claude | Opus 5.5, high | 10:00–11:00 | fresh 1k · out 2k | none"
        " | opus 100% | $4.00 | 40 → 42 · alone here · 2 pts | 2: #1, #2 | |",
        "| 2026-10-20 | b [22222222] | claude | Sonnet 5.5, high | 10:00–11:00 | fresh 1k · out 2k | none"
        " | sonnet 100% | $3.00 | 42 → 44 · shared with 1 · est 1.0 pts | 0 | research only |",
        "| 2026-10-20 | c [33333333] | codex | gpt-x, high | 10:00–11:00 | fresh 1k · out 2k | none"
        " | gpt-x 100% | - | not read | not recorded | |",
        "| 2026-10-19 | a [11111111] | claude | Opus 5.5, high | 10:00–11:00 | fresh 1k · out 2k | none"
        " | opus 100% | $5.00 | 40 → 42 · alone here · 2 pts | 3: #1, #2, #3 | corrects the landed count |",
    ])
    rows = parse_ledger(ledger)
    buf = io.StringIO()
    report(ledger, out=buf)
    text = buf.getvalue()
    arms += [
        # Keeping both rows for session a would count 5 landings and $9.
        ("report: a later row for the same session replaces the earlier", len(rows) == 3
         and rows[-1]["list $"] == "$5.00"),
        ("report: totals", "3 sessions, $8.00 list-price proxy, 3.0 pts over 2 metered sessions, 3 landings" in text),
        ("report: per landing over sessions that recorded landings", "per landing: $2.67, 1.00 pts" in text),
        ("report: nothing landed is named with its note", "nothing landed: 1 sessions, $3.00: b [22222222]"
         " (research only)" in text),
        ("report: 'not recorded' is counted apart", "landed not recorded: 1 sessions" in text),
        ("report: a window by date", "1 sessions" in _report(ledger, "2026-10-19", "2026-10-19")),
        # Old rows (a bare number, or "N: links") carry no split, so the report adds no by-kind line for them.
        ("landed: old-style cells still read", landed_of("2: #18, #19") == 2 and landed_of("3") == 3
         and landed_of("0") == 0 and landed_of("not recorded") is None and landed_split("2: #18") is None
         and "landed by kind" not in text),
        # Reading only the number the cell starts with would count "2 merged, 1 opened, 1 committed" as 2.
        ("landed: a split counts every kind", landed_of("2 merged, 1 opened, 1 committed") == 4
         and landed_split("2 merged, 1 opened, 1 committed") == {"merged": 2, "opened": 1, "committed": 1}),
        ("landed: a split may carry links after it", landed_of("1 merged, 1 opened: #4, #5") == 2),
    ]
    split_ledger = "\n".join([
        header(),
        "| 2026-10-21 | x [44444444] | claude | Opus 5.5, high | 10:00–11:00 | fresh 1k · out 2k | none"
        " | opus 100% | $6.00 | not read | 2 merged, 1 opened | |",
        "| 2026-10-21 | y [55555555] | claude | Opus 5.5, high | 10:00–11:00 | fresh 1k · out 2k | none"
        " | opus 100% | $4.00 | not read | 1 merged, 1 committed | |",
        "| 2026-10-21 | z [66666666] | claude | Opus 5.5, high | 10:00–11:00 | fresh 1k · out 2k | none"
        " | opus 100% | $3.00 | not read | 2: #1, #2 | an old row |",
    ])
    stext = _report(split_ledger, "", "")
    arms += [
        ("report: landings add up across split and old rows", "7 landings" in stext),
        ("report: landed by kind, with old rows named as not split",
         "landed by kind: 3 merged, 1 opened, 1 committed, 2 not split" in stext),
        # $10.00 of split rows over 3 merged; folding in the old row's $3.00 would give $4.33.
        ("report: cost per merged landing counts rows with a split only",
         "per merged landing (rows with a split only): $3.33" in stext),
    ]
    arms += [
        ("window: a bad bound is rejected", _rejects("2026-10-20T25:99:00Z") and _rejects("yesterday")),
    ]
    arms += _tally_arms()
    fails = 0
    for name, ok in arms:
        print(f"  {'ok  ' if ok else 'FAIL'} {name}")
        fails += not ok
    print(f"spend-ledger self-test: {'PASS' if not fails else 'FAIL'} {len(arms) - fails}/{len(arms)}")
    return 1 if fails else 0


def _tally_arms():
    """Readings and calibrate on fixtures. The red arms are noted beside them."""
    def line(mid, model, ts, out, cached=0):
        return json.dumps({"type": "assistant", "timestamp": ts, "cwd": "/w/t", "message": {
            "id": mid, "model": model, "usage": {"input_tokens": 0, "cache_read_input_tokens": cached,
                                                  "cache_creation_input_tokens": 0, "output_tokens": out}}})
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "claude")
        os.makedirs(os.path.join(root, "p1"))
        os.makedirs(os.path.join(root, "p2"))
        with open(os.path.join(root, "p1", "a.jsonl"), "w") as fh:   # project one
            fh.write("\n".join([line("m1", "claude-opus-5-5", "2026-10-20T10:30:00Z", 3000000),
                                line("m2", "claude-opus-5-5", "2026-10-20T12:30:00Z", 4000000)]) + "\n")
        with open(os.path.join(root, "p2", "b.jsonl"), "w") as fh:   # another project, same meter
            fh.write(line("m3", "claude-sonnet-5-5", "2026-10-20T11:00:00Z", 1000000, cached=1000000) + "\n")
        _touch(root)
        path = os.path.join(tmp, "home", "usage-readings.tsv")
        add_reading(path, 10, "weekly", "2026-10-20T10:00:00Z")
        add_reading(path, 15, "weekly", "2026-10-20T12:00:00Z")
        add_reading(path, 16, "weekly", "2026-10-20T13:00:00Z")
        add_reading(path, 3, "weekly", "2026-10-21T15:00:00Z")      # after a reset
        add_reading(path, 0, "fable", "2026-10-20T10:00:00Z")      # another meter, never mixed in
        rejected = False
        try:
            add_reading(path, 120)
        except ValueError:
            rejected = True
        readings = load_readings(path, "weekly")
        rows, fit, kept = calibrate(readings, root)
        first = rows[0] if rows else {}
        log = os.path.join(tmp, "calibration.md")
        with open(log, "w") as fh:
            fh.write("# log\n\n## Rows\n\n| a |\n|---|\n\n## Field notes\n\nnone\n")
        n1 = write_fits(log, rows, "weekly")
        n2 = write_fits(log, rows, "weekly")
        text = open(log).read()
    return [
        ("reading: a percent over 100 is refused", rejected),
        ("reading: only the named meter is read", len(readings) == 4),
        # Pairing the 16 % reading with the 3 % one after the reset would give a negative K.
        ("calibrate: a drop is a reset, never a pair", len(rows) == 2 and all(r["points"] > 0 for r in rows)),
        # Counting only one project would give 3M; the meter moved for both.
        ("calibrate: every project's tokens count", first.get("tokens") == 5000000),
        ("calibrate: K is tokens per point of movement", first.get("k") == 1000000),
        # A 1-point pair kept in the fit would drag K from 1.0M to 1.33M.
        ("calibrate: a move under 2 points is flagged and left out", rows[1]["flag"] == "small move"
         and kept == 1 and fit == 1000000),
        ("calibrate --write: rows land in their own section, once", n1 == 2 and n2 == 0
         and text.count("2026-10-20T10:00:00Z | 2026-10-20T12:00:00Z") == 1
         and text.index(FIT_HEAD) < text.index("## Field notes")),
    ]


def _touch(root):
    """Date the fixture files after their records, so the skip-old-files shortcut keeps them on any clock."""
    stamp = datetime.datetime(2026, 10, 21, tzinfo=datetime.timezone.utc).timestamp()
    for path in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        os.utime(path, (stamp, stamp))


def _report(text, since, until):
    buf = io.StringIO()
    report(text, since, until, buf)
    return buf.getvalue()


def _rejects(bound):
    try:
        Window(bound, "")
    except ValueError:
        return True
    return False


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--self-test", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    m = sub.add_parser("measure", help="one ledger row per session, read from its transcript")
    m.add_argument("--platform", choices=("claude", "codex"))
    m.add_argument("--project", help="the session's working folder (default: this folder)")
    m.add_argument("--any-project", action="store_true", help="every project on this machine")
    pick = m.add_mutually_exclusive_group()
    pick.add_argument("--session", help="a session id, or its start")
    pick.add_argument("--latest", action="store_true", help="the session active most recently (the default)")
    pick.add_argument("--all", action="store_true", help="every session with activity in the window")
    m.add_argument("--since", default="")
    m.add_argument("--until", default="")
    m.add_argument("--meter", nargs=2, type=float, metavar=("START", "END"),
                   help="two readings of one meter, in points, when the transcript does not carry them")
    m.add_argument("--lead", default="", help='the lead\'s model and effort, e.g. "Opus 5.5, high"')
    m.add_argument("--landed", default="", help='what landed, split by kind: "2 merged, 1 opened, 1 committed" (the old '
                   '"2: #18, #19" still reads)')
    m.add_argument("--note", default="")
    m.add_argument("--header", action="store_true", help="print the table header first")
    m.add_argument("--claude-root", default=os.path.expanduser("~/.claude/projects"))
    m.add_argument("--codex-root", default=os.path.expanduser("~/.codex/sessions"))
    r = sub.add_parser("report", help="spend beside what landed, from the ledger")
    r.add_argument("--ledger", default="docs/spend-ledger.md")
    r.add_argument("--since", default="")
    r.add_argument("--until", default="")
    g = sub.add_parser("reading", help="log one reading of a usage meter")
    g.add_argument("percent", type=float, help="the meter's percent used, as the usage tool shows it")
    g.add_argument("--meter", default="weekly", help="the meter's name, such as 'weekly' or 'fable' (default weekly)")
    g.add_argument("--at", default="", help="when it was read, an ISO time (default now)")
    g.add_argument("--readings", default="", help="the readings file (default ~/.pitmaster/usage-readings.tsv)")
    c = sub.add_parser("calibrate", help="fit K, tokens per point, from pairs of readings")
    c.add_argument("--meter", default="weekly")
    c.add_argument("--since", default="", help="only readings from this date or ISO time")
    c.add_argument("--models", default="", help="count only models whose id contains this, for a per-model meter")
    c.add_argument("--readings", default="")
    c.add_argument("--write", default="", help="append the pairs to this calibration log, under Fitted K")
    c.add_argument("--claude-root", default=os.path.expanduser("~/.claude/projects"))
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if a.cmd == "measure":
        try:
            Window(a.since, a.until)
        except ValueError as e:
            ap.error(str(e))
        return measure(a)
    if a.cmd == "reading":
        try:
            print(add_reading(readings_path(a.readings), a.percent, a.meter, a.at))
        except ValueError as e:
            ap.error(str(e))
        return 0
    if a.cmd == "calibrate":
        return run_calibrate(a)
    if a.cmd == "report":
        if not os.path.exists(a.ledger):
            ap.error(f"no ledger at {a.ledger}")
        with open(a.ledger) as fh:
            report(fh.read(), a.since, a.until)
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
