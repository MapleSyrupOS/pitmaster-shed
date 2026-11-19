#!/usr/bin/env python3
"""diskscan.py: find where a machine's disk space went, inside the folders its owner allowed, for diskcheck.

Agents leave things behind: a clone per job, a worktree per subagent, model downloads, VM disks, scratch folders.
Each is small; together they fill a disk in days. This tool measures where the space is, what is growing, and what
no one has named yet. It only reads, and only the folders its owner allowed by name (pitmaster ADR-035).

The scope is a plain text file the owner can read and edit (default: <config>/pitmaster/diskcheck/scope.txt, where
<config> is ~/.config, or %APPDATA% on Windows). One line each:

  allow <path> [depth N]  a folder the scan may read; folder sizes are reported N levels below it (default 1).
                          Nothing outside an allow line is read.
  exclude <path>          never read, nor anything inside it (carves a folder out of an allowed one)
  limit <seconds>         stop reading after this long and mark the scan partial (default 900)
  big <size>              flag a folder this big that no space line names (default 5G)
  ledger <path>           where scan results are appended (default: beside the scope file)
  space <path> <kind> [note...]
                          a known space: kind is trees (git clones or worktrees), cold (keep, but could move),
                          cache (an app can rebuild it) or live (in use; leave it)
  approved <time> <digest>
                          written by `approve`; never by hand

Paths may start with ~. Lines starting with # are comments. A scope with a `root` line (diskscan 0.6 and before) is
refused, with advice on rewriting it as allow lines.

The access lines are allow, exclude, limit and ledger. `approve` writes a digest of them, and `scan` refuses to run
when they no longer match it, so widening what the tool reads always takes the owner's fresh approval. space and big
lines can change freely: they describe, they do not grant.

Usage:
  python3 diskscan.py init [--scope FILE] [--list [--hidden]]
                                                   write a starting scope that allows nothing (never overwrites one);
                                                   --list also prints the names of the folders in the home folder
  python3 diskscan.py plan [--scope FILE]          print what a scan would read and write; reads nothing
  python3 diskscan.py approve [--scope FILE]       record the owner's approval of the current access lines
  python3 diskscan.py scan [--scope FILE] [--top N] [--tsv] [--alert-free SIZE] [--alert-growth SIZE]
  python3 diskscan.py trees DIR... [--hours N]     read-only verdicts on the git trees inside each DIR
  python3 diskscan.py --self-test [--break NAME]

What scan reads: the names, sizes, modification times and device numbers of files and folders (os.lstat), inside the
allowed folders only. Never a file's contents. It does not follow symbolic links, does not cross onto another disk
below an allowed folder, does not ask for raised privileges, and does not use the network. A folder the system
refuses is counted and named, not retried.

Outside what you allowed: the disk's used space less what the scan measured is reported as one total, never broken
down and never read. The consent guard: the first scan after an approval records the folders the system refused;
later scans under the same approval leave them unread, as if excluded, and raise an ALERT access line naming them,
so a scan never reads more than the first approved scan did, whoever starts it.

With --alert-free or --alert-growth, scan first prints one ALERT line per disk with less free space than SIZE, per
folder that grew by SIZE or more since the previous scan, and (ALERT unread) when the space outside what you allowed
grew by SIZE or more. A folder whose unread set (excluded, refused or guarded folders inside it) changed since the
previous scan is listed as not compared instead. Any ALERT line makes the exit status 3. This is for a scheduled,
report-only run: it changes nothing more than a plain scan does.
What scan writes: rows appended to the ledger (date, path, bytes, newest modification time), one audit line, and
comment lines recording the folders it did not read, the total outside what you allowed, and (first scan after an
approval) the guard. Nothing else.

What trees reads: each git tree's status, branches and alternates, through git with GIT_OPTIONAL_LOCKS=0 so the
reading changes nothing. A tree is "removable" only when it is idle, not in use, borrowed from by no other tree,
its own borrowed objects still exist, and it has nothing a remote lacks. The tool never removes anything.

The guards, each named when it fails:
  allow     nothing outside an allow line is read
  symlink   a link is never followed
  device    the walk never crosses onto another disk below an allowed folder
  exclude   an excluded folder is never read
  approve   scan refuses an unapproved or changed scope
  denied    a refused folder is counted and the scan goes on
  guard     a folder refused in the first scan after an approval is not read by a later one, and ALERT access names it
  growth    growth is measured against the previous scan of the same path
  access    growth is compared only when the same folders were left unread both times
  unread    space outside what you allowed that grew raises ALERT unread
  unnamed   a big folder no space line names is flagged
  lossless  a tree with uncommitted or unpushed work is never removable
  borrowed  a tree another tree borrows objects from is never removable
  alert     low free space and fast growth raise ALERT lines and exit 3

Exit status: 0 done, 1 scan refused or partial, 2 usage error, 3 an alert was raised. Python 3.8+, standard library only.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time

ACCESS_KEYS = ("allow", "exclude", "limit", "ledger")
KINDS = ("trees", "cold", "cache", "live")
BREAK = set()
DISK_USAGE = shutil.disk_usage  # the self-test swaps in a fake disk

SPACES_GUESS = [("codex-trees", "trees", "Codex job clones"), ("agy-trees", "trees", "Antigravity job clones"),
                (".claude", "live", "Claude Code's sessions and settings"), (".codex", "live", "Codex sessions"),
                (".lima", "live", "Lima virtual machines"), (".ollama", "cache", "Ollama models"),
                (".lmstudio", "cache", "LM Studio models"), (".cache/huggingface", "cache", "Hugging Face downloads"),
                ("Library/Developer/CoreSimulator", "cache", "Xcode simulators"), (".Trash", "cold", "the Trash")]

OFFER = ("To help you choose, I can list the names of the folders in your home folder: just their names, the way "
         "your file browser shows them. No sizes, and nothing inside them is read. Or you can type the folders you "
         "want checked yourself.")


# --- the scope file ---------------------------------------------------------

class ScopeError(Exception):
    """The scope cannot be used as written; the message says how to fix it."""


def config_dir():
    base = os.environ.get("APPDATA") if os.name == "nt" else None
    base = base or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "pitmaster", "diskcheck")


def expand(p):
    return os.path.abspath(os.path.expanduser(p))


def inside(p, a):
    """p is the folder a, or inside it."""
    a = a.rstrip(os.sep) or os.sep
    return p == a or p.startswith(a if a.endswith(os.sep) else a + os.sep)


def parse_size(s):
    s = s.strip().upper()
    mult = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}
    if s and s[-1] in mult:
        return int(float(s[:-1]) * mult[s[-1]])
    return int(s)


def human(n):
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024 or unit == "T":
            return ("%.0f%s" if unit in "BK" else "%.1f%s") % (n, unit)
        n /= 1024.0


def root_advice(path, roots, spaces):
    lines = ["diskscan: REFUSED: %s has a `root` line (%s)." % (path, "; ".join(roots)),
             "Since 0.7.0 diskscan reads only the folders you allow by name (pitmaster ADR-035), so a root line is",
             "no longer accepted. Rewrite the scope: delete the root line and add one line per folder you want read,",
             "for example:"]
    examples = [s for s, _, _ in spaces][:4] or ["~/Documents", "~/Projects"]
    home = os.path.expanduser("~")
    for e in examples:
        e = "~" + e[len(home):] if inside(e, home) and e != home else e
        lines.append("  allow %s depth 1" % e)
    lines += ["Keep any exclude line that carves a folder out of an allowed one. `init --list` shows the names of the",
              "folders in your home folder (names only). Then run `plan`, and `approve` on the owner's yes."]
    return "\n".join(lines)


class Scope:
    def __init__(self, path):
        self.path = path
        self.allows, self.excludes, self.spaces = [], [], []
        self.limit, self.big, self.ledger = 900, 5 << 30, None
        self.approved = None
        self.access_lines = []
        roots = []
        with open(path, encoding="utf-8") as f:
            for n, raw in enumerate(f, 1):
                line = raw.split("#", 1)[0].strip() if not raw.lstrip().startswith("space") else raw.strip()
                if not line or line.startswith("#"):
                    continue
                key, _, rest = line.partition(" ")
                rest = rest.strip()
                if key in ACCESS_KEYS:
                    self.access_lines.append(" ".join(line.split()))
                if key == "allow":
                    parts = rest.rsplit(" depth ", 1)
                    p = expand(parts[0].strip())
                    if "allow" in BREAK:
                        p = os.path.dirname(p)  # the old root behaviour: read the folder around it
                    self.allows.append((p, int(parts[1]) if len(parts) == 2 else 1))
                elif key == "root":
                    roots.append(" ".join(line.split()))
                elif key == "exclude":
                    self.excludes.append(expand(rest))
                elif key == "limit":
                    self.limit = int(rest.rstrip("s"))
                elif key == "big":
                    self.big = parse_size(rest)
                elif key == "ledger":
                    self.ledger = expand(rest)
                elif key == "space":
                    p, kind, note = self._space(rest)
                    if kind not in KINDS:
                        raise ScopeError("%s:%d: space kind must be one of %s" % (path, n, ", ".join(KINDS)))
                    self.spaces.append((expand(p), kind, note))
                elif key == "approved":
                    self.approved = rest.split()
                else:
                    raise ScopeError("%s:%d: unknown line: %s" % (path, n, key))
        if roots:
            raise ScopeError(root_advice(path, roots, self.spaces))
        self.ledger = self.ledger or os.path.join(os.path.dirname(os.path.abspath(path)), "ledger.tsv")

    @staticmethod
    def _space(rest):
        # A path may contain spaces: the kind is the first word that is a known kind.
        words = rest.split(" ")
        for i, w in enumerate(words):
            if w in KINDS and i > 0:
                return " ".join(words[:i]), w, " ".join(words[i + 1:])
        return rest, "", ""

    def digest(self):
        return "sha256:" + hashlib.sha256("\n".join(self.access_lines).encode()).hexdigest()[:16]

    def is_approved(self):
        if "approve" in BREAK:
            return True
        return bool(self.approved) and self.approved[-1] == self.digest()

    def approval_key(self):
        """One approval: its time and digest. Approving again, even unchanged, starts a new guard."""
        return " ".join(self.approved or [])

    def top_allows(self):
        """The allowed folders not inside another allowed folder: the walk starts at each of these once."""
        paths = [a for a, _ in self.allows]
        return sorted({a for a in paths if not any(o != a and inside(a, o) for o in paths)})

    def reported(self, d):
        """d's size is reported: it lies within the depth of an allow line that holds it."""
        for a, n in self.allows:
            if inside(d, a):
                rest = d[len(a.rstrip(os.sep)):]
                if rest.count(os.sep) <= n:
                    return True
        return False

    def allowed(self, p):
        return any(inside(p, a) for a, _ in self.allows)


def load_scope(a):
    return Scope(a.scope or os.path.join(config_dir(), "scope.txt"))


def default_scope_text(home):
    lines = ["# diskcheck scope: what diskscan may read. Edit it, then run `diskscan.py approve` again.",
             "# Access lines (allow, exclude, limit, ledger) need approval; space and big lines do not.",
             "#",
             "# Nothing is allowed yet. Add one line per folder you want checked, for example:",
             "#   allow ~/Documents depth 1",
             "# The scan reads only the folders you allow. Space used anywhere else is shown as one total,",
             "# \"outside what you allowed\", and never read. Use exclude to carve a folder out of an allowed one.",
             "", "limit 900", "big 5G", "",
             "# Known spaces found in the home folder (kind: trees, cold, cache or live). They describe a folder;",
             "# they do not allow it to be read."]
    for p, kind, note in SPACES_GUESS:
        if os.path.exists(os.path.join(home, p)):
            lines.append("space ~/%s %s %s" % (p, kind, note))
    return "\n".join(lines) + "\n"


def home_names(home, hidden):
    """The names of the folders in home, the way a file browser shows them: names only, nothing inside read."""
    out = []
    with os.scandir(home) as it:
        for e in it:
            if e.name.startswith(".") and not hidden:
                continue
            if e.is_dir(follow_symlinks=False):
                out.append(e.name)
    return sorted(out, key=str.lower)


# --- disks -----------------------------------------------------------------

def mount_of(path):
    path = os.path.realpath(path)
    dev = os.lstat(path).st_dev
    while True:
        parent = os.path.dirname(path)
        if parent == path:
            return path
        try:
            if os.lstat(parent).st_dev != dev:
                return path
        except OSError:
            return path
        path = parent


def disks_for(paths):
    seen = {}
    for p in paths:
        if os.path.exists(p):
            m = mount_of(p)
            if m not in seen:
                u = DISK_USAGE(p)
                seen[m] = (u.total, u.used, u.free)
    return seen


# --- the walk ---------------------------------------------------------------

class Walk:
    def __init__(self, scope, deadline, guarded=()):
        self.scope, self.deadline = scope, deadline
        self.guarded = list(guarded)
        self.sizes, self.newest, self.totals = {}, {}, {}
        self.entries, self.denied, self.partial = 0, [], False
        self.notread = []  # (path, why): excluded, refused or guarded
        self.inodes = set()
        self.wanted = {sp for sp, _, _ in scope.spaces}

    def skipped(self, p):
        """Why p is not read, or None."""
        if "exclude" not in BREAK and any(inside(p, e) for e in self.scope.excludes):
            return "excluded"
        if any(inside(p, g) for g in self.guarded):
            return "guarded"
        return None

    def run(self):
        for a in self.scope.top_allows():
            why = self.skipped(a)
            if why:
                self.notread.append((a, why))
                continue
            try:
                st = os.lstat(a)
            except OSError:
                continue
            if not stat.S_ISDIR(st.st_mode):
                continue  # a link or a file: an allowed folder that is a link is never followed
            self.totals[a] = self.walk(a, st.st_dev)[0]
        return self

    def walk(self, d, dev):
        total, newest = 0, 0
        if time.monotonic() > self.deadline:
            self.partial = True
            return 0, 0
        try:
            it = list(os.scandir(d))
        except (PermissionError, OSError) as e:
            if "denied" in BREAK:
                raise
            self.denied.append((d, e.strerror or str(e)))
            self.notread.append((d, "refused"))
            return 0, 0
        for ent in it:
            p = ent.path
            why = self.skipped(p)
            if why:
                self.notread.append((p, why))
                continue
            try:
                st = os.lstat(p) if "symlink" not in BREAK else os.stat(p)
            except OSError:
                continue
            self.entries += 1
            newest = max(newest, st.st_mtime)
            if stat.S_ISLNK(st.st_mode):
                continue
            if stat.S_ISDIR(st.st_mode):
                if st.st_dev != dev and "device" not in BREAK:
                    continue
                t, nw = self.walk(p, dev)
                total += t
                newest = max(newest, nw)
            else:
                key = (st.st_dev, st.st_ino)
                if st.st_nlink > 1:
                    if key in self.inodes:
                        continue
                    self.inodes.add(key)
                total += getattr(st, "st_blocks", 0) * 512 if hasattr(st, "st_blocks") else st.st_size
        if self.scope.reported(d) or d in self.wanted:
            self.sizes[d] = total
            self.newest[d] = newest
        return total, newest


def outside_totals(w, disks):
    """Per disk: its used space less what the scan measured there. Never broken down, never read."""
    measured = {}
    for a, t in w.totals.items():
        m = mount_of(a)
        measured[m] = measured.get(m, 0) + t
    return {m: max(used - measured.get(m, 0), 0) for m, (tot, used, free) in disks.items()}


# --- the ledger -------------------------------------------------------------

class Ledger:
    """The ledger's size rows, plus the comment lines diskscan 0.7 adds (older readers skip them):
    # notread <when>\\t<json [[path, why], ...]>    the folders a scan did not read
    # outside <when>\\t<disk>\\t<bytes>\\t<digest>    the total outside what you allowed
    # guard <when>\\t<approval>\\t<json [path, ...]>  the folders the first scan after an approval was refused
    """

    def __init__(self, path):
        self.rows, self.notread, self.outside, self.guards = [], {}, [], {}
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if line.startswith("# notread "):
                    when, _, js = line[len("# notread "):].partition("\t")
                    self.notread[when] = [p for p, _ in json.loads(js)]
                elif line.startswith("# outside "):
                    parts = line[len("# outside "):].split("\t")
                    if len(parts) == 4:
                        self.outside.append((parts[0], parts[1], int(parts[2]), parts[3]))
                elif line.startswith("# guard "):
                    parts = line[len("# guard "):].split("\t")
                    if len(parts) == 3:
                        self.guards.setdefault(parts[1], json.loads(parts[2]))
                else:
                    parts = line.split("\t")
                    if len(parts) >= 3 and not line.startswith("#") and parts[2].isdigit():
                        self.rows.append((parts[0], parts[1], int(parts[2])))

    def last_outside(self, now, digest):
        """Per disk, the latest earlier total measured under the same access lines."""
        last = {}
        for when, m, b, dg in self.outside:
            if when < now and dg == digest:
                last[m] = (when, b)
        return last


def read_ledger(path):
    return Ledger(path).rows


def previous_sizes(rows, now):
    last = {}
    for when, path, size in rows:
        if when < now:
            last[path] = (when, size)
    return last


def unread_in(paths, p):
    return frozenset(q for q in paths if inside(q, p))


def growth(sizes, prev, now, prev_notread=None, cur_notread=None):
    """(rows, not_compared). rows are (delta, per day, path, since). With prev_notread (when -> paths) given, a path
    is compared only when the same folders inside it went unread both times, and the earlier scan recorded them."""
    out, skipped = [], []
    t_now = dt.datetime.fromisoformat(now)
    for p, size in sizes.items():
        if p in prev and "growth" not in BREAK:
            when, old = prev[p]
            if prev_notread is not None and "access" not in BREAK:
                before = prev_notread.get(when)
                if before is None or unread_in(before, p) != unread_in(cur_notread or [], p):
                    skipped.append(p)
                    continue
            days = max((t_now - dt.datetime.fromisoformat(when)).total_seconds() / 86400, 1e-6)
            out.append((size - old, (size - old) / days, p, when))
        elif "growth" in BREAK:
            out.append((size, size, p, now))
    return sorted(out, reverse=True), sorted(skipped)


def split_not_compared(skipped, prev, prev_notread):
    """(changed, unrecorded): folders not compared because the folders left unread inside them changed, and folders
    whose last scan recorded no unread set at all (made before 0.7.0). The second kind is reported as one line."""
    if "first" in BREAK:
        return list(skipped), []
    changed, unrecorded = [], []
    for p in skipped:
        when = prev[p][0] if p in prev else None
        (unrecorded if prev_notread.get(when) is None else changed).append(p)
    return changed, unrecorded


# --- one scan's results ------------------------------------------------------

class Result:
    def __init__(self, sc, w, led, now, took=0.0):
        self.w, self.now, self.took = w, now, took
        self.prev = previous_sizes(led.rows, now)
        self.prev_notread = led.notread
        self.cur_notread = [p for p, _ in w.notread]
        self.disks = disks_for([a for a, _ in sc.allows])
        self.outside = outside_totals(w, self.disks)
        self.prev_outside = led.last_outside(now, sc.digest())
        self.guarded = sorted({p for p, why in w.notread if why == "guarded"})
        tops = set(sc.top_allows())
        self.leaf = {p: s for p, s in w.sizes.items() if p not in tops}

    def growth(self):
        return growth(self.leaf, self.prev, self.now, self.prev_notread, self.cur_notread)


# --- commands ----------------------------------------------------------------

def cmd_init(a):
    path = a.scope or os.path.join(config_dir(), "scope.txt")
    home = os.path.expanduser("~")
    if getattr(a, "list", False):
        print("The folders in your home folder (names only: no sizes, and nothing inside them was read):")
        for n in home_names(home, getattr(a, "hidden", False)):
            print("  ~/%s" % n)
        print("\nPick the ones to check, and add a line for each to the scope: allow ~/<name> depth 1")
    if os.path.exists(path):
        print("diskscan: a scope already exists at %s; edit it instead" % path)
        return 0 if getattr(a, "list", False) else 2
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(default_scope_text(home))
    print("diskscan: wrote a starting scope to %s. It allows no folder yet." % path)
    if not getattr(a, "list", False):
        print(OFFER)
        print("(`init --list` prints those names; `--hidden` adds the ones whose names start with a dot.)")
    return 0


def cmd_plan(a):
    sc = load_scope(a)
    print("Scope file: %s" % sc.path)
    print("Status: %s" % ("APPROVED on %s (%s)" % (sc.approved[0][:10], sc.digest()) if sc.is_approved()
                          else "NOT APPROVED: scan will refuse until `approve` is run (digest %s)" % sc.digest()))
    print("\nThe boundary is the allow list below, not the system's permissions: the scan reads only these")
    print("folders, whoever starts it (you, or a scheduler) and whatever the system would let it read.")
    if sc.allows:
        print("\nDisks the allowed folders are on (the scan never leaves these):")
        for m, (tot, used, free) in disks_for([r for r, _ in sc.allows]).items():
            print("  %-30s %s total, %s used, %s free" % (m, human(tot), human(used), human(free)))
    print("\nWill read (sizes, dates and names only; never file contents):")
    if not sc.allows:
        print("  nothing yet: the scope has no allow line, so scan will refuse")
    for r, d in sc.allows:
        print("  %s  (folder sizes reported %d level%s down)" % (r, d, "" if d == 1 else "s"))
    print("\nWill never read:")
    for e in sc.excludes:
        print("  %s" % e)
    print("  anything outside the allowed folders: it is shown as one total, \"outside what you allowed\"")
    print("  (the disk's used space less what was measured), never broken down")
    print("  anything behind a symbolic link, or on another disk below an allowed folder")
    if sc.is_approved():
        guard = Ledger(sc.ledger).guards.get(sc.approval_key())
        if guard is None:
            print("  folders the system refuses the first scan under this approval: later scans leave them unread")
        elif guard:
            print("  %d folder%s the first scan under this approval was refused (until you approve again):"
                  % (len(guard), "" if len(guard) == 1 else "s"))
            for g in guard[:8]:
                print("    %s" % g)
    print("\nWill write: rows appended to %s, and one audit line there" % sc.ledger)
    print("Stops after: %d s (then marks the scan partial)" % sc.limit)
    print("Never: deletes, moves, raises privileges, or uses the network.")
    return 0


def cmd_approve(a):
    path = a.scope or os.path.join(config_dir(), "scope.txt")
    sc = Scope(path)
    with open(path, encoding="utf-8") as f:
        kept = [l for l in f if not l.lstrip().startswith("approved ")]
    stamp = "approved %s %s\n" % (dt.datetime.now().replace(microsecond=0).isoformat(), sc.digest())
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(kept + ([] if kept and kept[-1].endswith("\n") else ["\n"]) + [stamp])
    print("diskscan: approved the access lines of %s (%s)" % (path, sc.digest()))
    if not sc.allows:
        print("diskscan: note: the scope allows no folder yet, so scan will refuse until it has an allow line")
    return 0


def cmd_scan(a):
    sc = load_scope(a)
    if not sc.allows:
        print("diskscan: REFUSED: the scope allows no folder yet. Add an `allow <path>` line for each folder to "
              "check, run `plan`, show it to the owner, and run `approve` on their yes.")
        return 1
    if not sc.is_approved():
        print("diskscan: REFUSED: the scope's access lines are not approved, or changed since (%s). "
              "Run `plan`, show it to the owner, and run `approve` on their yes." % sc.digest())
        return 1
    now = dt.datetime.now().replace(microsecond=0).isoformat()
    led = Ledger(sc.ledger)
    key = sc.approval_key()
    first = key not in led.guards
    guarded = [] if first or "guard" in BREAK else led.guards[key]
    t0 = time.monotonic()
    w = Walk(sc, t0 + sc.limit, guarded).run()
    took = time.monotonic() - t0
    res = Result(sc, w, led, now, took)
    os.makedirs(os.path.dirname(sc.ledger), exist_ok=True)
    new = not os.path.exists(sc.ledger)
    with open(sc.ledger, "a", encoding="utf-8") as f:
        if new:
            f.write("date\tpath\tbytes\tnewest\n")
        for p, size in sorted(w.sizes.items()):
            f.write("%s\t%s\t%d\t%s\n" % (now, p, size,
                    dt.datetime.fromtimestamp(w.newest.get(p, 0)).date().isoformat()))
        f.write("# audit %s allow=%s entries=%d denied=%d excluded=%d guarded=%d seconds=%.0f%s\n" % (
            now, ",".join(sc.top_allows()), w.entries, len(w.denied), len(sc.excludes), len(res.guarded), took,
            " PARTIAL" if w.partial else ""))
        f.write("# notread %s\t%s\n" % (now, json.dumps(w.notread, ensure_ascii=False)))
        for m, b in sorted(res.outside.items()):
            f.write("# outside %s\t%s\t%d\t%s\n" % (now, m, b, sc.digest()))
        if first:
            f.write("# guard %s\t%s\t%s\n" % (now, key, json.dumps([d for d, _ in w.denied], ensure_ascii=False)))
    found = alerts(sc, res, getattr(a, "alert_free", None), getattr(a, "alert_growth", None))
    for line in found:
        print(line)
    report(sc, res, a.top, a.tsv)
    if found:
        return 3
    return 1 if w.partial else 0


def alerts(sc, res, free_min, growth_min):
    """ALERT lines: access (always, when the guard held a folder back), free space, growth and unread (on request)."""
    if "alert" in BREAK:
        return []
    out = []
    if res.guarded:
        out.append("ALERT access: %d folder%s left unread: the first scan under this approval was refused %s, so "
                   "no later scan reads %s until you approve again: %s"
                   % (len(res.guarded), "" if len(res.guarded) == 1 else "s",
                      "it" if len(res.guarded) == 1 else "them", "it" if len(res.guarded) == 1 else "them",
                      ", ".join(res.guarded[:5]) + (", ..." if len(res.guarded) > 5 else "")))
    if free_min:
        for m, (tot, used, free) in res.disks.items():
            if free < parse_size(free_min):
                out.append("ALERT free space: %s has %s free, under %s" % (m, human(free), free_min))
    if growth_min:
        for delta, rate, p, when in res.growth()[0]:
            if delta >= parse_size(growth_min):
                out.append("ALERT growth: %s grew %s since %s" % (p, human(delta), when[:16]))
        if "unread" not in BREAK:
            for m, b in sorted(res.outside.items()):
                if m in res.prev_outside:
                    when, old = res.prev_outside[m]
                    if b - old >= parse_size(growth_min):
                        out.append("ALERT unread: space outside what you allowed on %s grew %s since %s; choose a "
                                   "folder to allow if you want to know where" % (m, human(b - old), when[:16]))
    return out


def named(sc, p):
    """p is a known space, or inside one."""
    return any(inside(p, s) for s, _, _ in sc.spaces)


def unnamed_rest(sc, sizes, p):
    """p's size less the named spaces measured inside it (the outermost ones only)."""
    inner = [s for s, _, _ in sc.spaces if s != p and inside(s, p) and s in sizes]
    top = [s for s in inner if not any(o != s and inside(s, o) for o in inner)]
    return sizes.get(p, 0) - sum(sizes[s] for s in top)


def report(sc, res, top, tsv):
    w, leaf = res.w, res.leaf
    if tsv:
        for p, s in sorted(leaf.items(), key=lambda x: -x[1]):
            print("%d\t%s\t%s" % (s, p, "named" if named(sc, p) else "unnamed"))
        for m, b in sorted(res.outside.items()):
            print("%d\t%s\toutside" % (b, m))
        return
    print("diskscan %s: read %d entries in %.0f s%s" % (res.now, w.entries, res.took,
                                                        "  PARTIAL (time limit)" if w.partial else ""))
    print("\nDisks:")
    for m, (tot, used, free) in res.disks.items():
        print("  %-30s %s free of %s (%d%% used)" % (m, human(free), human(tot), 100 * used // max(tot, 1)))
    print("\nOutside what you allowed (not read; system files, other folders and other accounts, as one total):")
    for m, b in sorted(res.outside.items()):
        change = ""
        if m in res.prev_outside:
            when, old = res.prev_outside[m]
            change = "  (%s%s since %s)" % ("+" if b >= old else "-", human(abs(b - old)), when[:10])
        print("  %8s  on %s%s" % (human(b), m, change))
    print("  To know where any of it is, choose a folder to allow; it is never broken down otherwise.")
    if w.denied:
        print("\nRefused by the system, not read (%d):" % len(w.denied))
        for d, why in w.denied[:8]:
            print("  %s  (%s)" % (d, why))
    if res.guarded:
        print("\nLeft unread by the consent guard (refused in the first scan under this approval) (%d):"
              % len(res.guarded))
        for d in res.guarded[:8]:
            print("  %s" % d)
    print("\nBiggest folders:")
    for p, s in sorted(leaf.items(), key=lambda x: -x[1])[:top]:
        print("  %8s  %s%s" % (human(s), p, "" if named(sc, p) else "  [unnamed]"))
    rows, skipped = res.growth()
    g = [x for x in rows if abs(x[0]) >= (1 << 30)]
    if g:
        print("\nChanged most since the last scan (1 GB or more):")
        for delta, rate, p, when in g[:top]:
            print("  %+9s  (%s/day since %s)  %s" % (human(delta), human(rate), when[:10], p))
    elif not res.prev:
        print("\nNo earlier scan in the ledger: growth shows from the next scan on.")
    changed, unrecorded = split_not_compared(skipped, res.prev, res.prev_notread or {})
    if unrecorded:
        print("\nNot compared with the last scan: %d folder(s). That scan was made before diskscan 0.7.0 and did not "
              "record what it left unread; they are compared from the next scan on." % len(unrecorded))
    if changed:
        print("\nNot compared with the last scan (%d): the folders left unread inside them changed:" % len(changed))
        for p in changed[:top]:
            print("  %s" % p)
        if len(changed) > top:
            print("  ... and %d more" % (len(changed) - top))
    big_unnamed = {p for p in leaf if not named(sc, p) and unnamed_rest(sc, w.sizes, p) >= sc.big}
    unnamed = [(unnamed_rest(sc, w.sizes, p), p) for p in big_unnamed
               if not any(q != p and inside(q, p) for q in big_unnamed)]
    if "unnamed" in BREAK:
        unnamed = []
    if unnamed:
        print("\nBig folders no space line names (%s or more):" % human(sc.big))
        for s, p in sorted(unnamed, reverse=True)[:top]:
            print("  %8s  %s" % (human(s), p))
    if sc.spaces:
        print("\nKnown spaces:")
        for p, kind, note in sc.spaces:
            s = w.sizes.get(p)
            size = human(s) if s is not None else ("-" if sc.allowed(p) else "not allowed")
            print("  %11s  %-5s  %s%s" % (size, kind, p, ("  " + note) if note else ""))


# --- trees -------------------------------------------------------------------

def git(d, *args):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    r = subprocess.run(["git", "-C", d] + list(args), capture_output=True, text=True, env=env)
    return r.returncode, r.stdout


def alternates(d):
    f = os.path.join(d, ".git", "objects", "info", "alternates")
    out = []
    if os.path.isfile(f):
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    p = line if os.path.isabs(line) else os.path.join(d, ".git", "objects", line)
                    out.append(os.path.realpath(p))
    return out


def recent(d, hours):
    cutoff = time.time() - hours * 3600
    for top, dirs, files in os.walk(d):
        dirs[:] = [x for x in dirs if x not in (".git", "build", "node_modules")]
        for n in files + dirs:
            try:
                if os.lstat(os.path.join(top, n)).st_mtime > cutoff:
                    return True
            except OSError:
                pass
    return False


def cmd_trees(a):
    cands = []
    for root in a.dirs:
        root = expand(root)
        for n in sorted(os.listdir(root)):
            p = os.path.join(root, n)
            if os.path.isdir(p) and os.path.exists(os.path.join(p, ".git")):
                cands.append(p)
    sources = set()
    for p in cands:
        sources.update(alternates(p))
    busy = cwds()
    for p in cands:
        rp = os.path.realpath(p)
        v = verdict(p, rp, a.hours, sources, busy)
        print("%-12s %s" % (v, p))
    return 0


def verdict(p, rp, hours, sources, busy):
    if any(b == rp or b.startswith(rp + os.sep) for b in busy):
        return "in-use"
    if os.path.join(rp, ".git", "objects") in sources and "borrowed" not in BREAK:
        return "borrowed"
    if any(not os.path.isdir(x) for x in alternates(p)):
        return "source-gone"
    if recent(p, hours):
        return "recent"
    rc, out = git(p, "status", "--porcelain")
    if rc != 0:
        return "unreadable"
    if out.strip() and "lossless" not in BREAK:
        return "unsaved"
    rc, out = git(p, "branch", "-r", "--contains", "HEAD")
    if (rc != 0 or not out.strip()) and "lossless" not in BREAK:
        return "unpushed"
    return "removable"


def cwds():
    out = set()
    if os.path.isdir("/proc"):
        for pid in os.listdir("/proc"):
            if pid.isdigit():
                try:
                    out.add(os.readlink("/proc/%s/cwd" % pid))
                except OSError:
                    pass
    elif shutil.which("lsof"):
        r = subprocess.run(["lsof", "-a", "-d", "cwd", "-Fn"], capture_output=True, text=True)
        out.update(l[1:] for l in r.stdout.splitlines() if l.startswith("n"))
    return out


# --- self-test -----------------------------------------------------------------

class FakeUsage:
    """A disk_usage stand-in: the disk holding `under` reports `used` bytes used of 1T."""

    def __init__(self, used):
        self.used = used

    def __call__(self, p):
        real = shutil.disk_usage(p)
        return type(real)(1 << 40, self.used, (1 << 40) - self.used)


def self_test(broken):
    global DISK_USAGE
    BREAK.update(broken)
    fails = []
    import io
    import contextlib
    T = os.path.realpath(tempfile.mkdtemp(prefix="diskscan-test-"))
    real_scandir, real_lstat, real_usage = os.scandir, os.lstat, DISK_USAGE

    def check(name, ok):
        print("%s %s" % ("ok  " if ok else "FAIL", name))
        if not ok:
            fails.append(name)

    def quiet(fn, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fn(*args)
        return rc, buf.getvalue()

    def refusing(paths, seen=None):
        def scandir(path="."):
            p = os.path.abspath(path)
            if seen is not None:
                seen.append(p)
            if p in paths:
                raise PermissionError(13, "Permission denied", path)
            return real_scandir(path)
        return scandir

    try:
        home = os.path.join(T, "home")
        os.makedirs(os.path.join(home, "big", "inner", "deep"))
        os.makedirs(os.path.join(home, "private"))
        os.makedirs(os.path.join(home, "denied"))
        os.makedirs(os.path.join(home, "Zebra Notes"))
        os.makedirs(os.path.join(home, ".hidden-place"))
        beside = os.path.join(T, "beside")  # next to the allowed folder, never allowed
        os.makedirs(beside)
        outside = os.path.join(T, "outside")
        os.makedirs(outside)
        for f, n in (("big/inner/a.bin", 300000), ("private/secret.bin", 500000), ("denied/x.bin", 1000),
                     ("small.bin", 10)):
            with open(os.path.join(home, f), "wb") as fh:
                fh.write(b"x" * n)
        with open(os.path.join(outside, "far.bin"), "wb") as fh:
            fh.write(b"x" * 900000)
        with open(os.path.join(beside, "next-door.bin"), "wb") as fh:
            fh.write(b"x" * 700000)
        os.symlink(outside, os.path.join(home, "link"))
        scope = os.path.join(T, "scope.txt")
        body = ("allow %s depth 2\nexclude %s\nbig 100K\nspace %s live small\nspace %s cold deep\n"
                % (home, os.path.join(home, "private"), os.path.join(home, "small.bin"),
                   os.path.join(home, "big", "inner", "deep")))
        with open(scope, "w") as fh:
            fh.write(body)
        ns = argparse.Namespace(scope=scope, top=10, tsv=True, alert_free=None, alert_growth=None)

        # root: an old root scope is refused, with the rewrite advice
        old = os.path.join(T, "old-scope.txt")
        with open(old, "w") as fh:
            fh.write("root %s depth 2\nspace %s trees clones\napproved 2026-11-09 sha256:0\n" % (home, beside))
        rc, out = quiet(main, ["scan", "--scope", old])
        check("root: a scope with a root line is refused, with the rewrite as allow lines",
              rc == 1 and "REFUSED" in out and ("%sbeside depth 1" % os.sep) in out and "\n  allow " in out)
        empty = os.path.join(T, "empty-scope.txt")
        with open(empty, "w") as fh:
            fh.write("limit 60\n")
        quiet(cmd_approve, argparse.Namespace(scope=empty))
        rc, out = quiet(cmd_scan, argparse.Namespace(scope=empty, top=10, tsv=True))
        check("allow: a scope that allows nothing is refused", rc == 1 and "allows no folder" in out)

        rc, out = quiet(cmd_scan, ns)
        check("approve: an unapproved scope is refused", rc == 1 and "REFUSED" in out)
        quiet(cmd_approve, ns)
        sc = Scope(scope)

        # denied: the refusal is planted in os.scandir, so it holds on every system and for an administrator too
        # (chmod cannot refuse anything on Windows, or to root).
        refused = os.path.join(home, "denied")
        os.scandir = refusing({refused})
        try:
            try:
                w = Walk(sc, time.monotonic() + 60).run()
            except PermissionError:
                w = None
        finally:
            os.scandir = real_scandir
        check("denied: a refused folder is counted and the scan goes on",
              w is not None and any(d == refused for d, _ in w.denied))
        if w is None:
            BREAK.discard("denied")
            w = Walk(sc, time.monotonic() + 60).run()
        check("allow: a folder beside an allowed one is never read or listed",
              not any(inside(p, beside) for p in w.sizes) and T not in w.sizes)
        check("symlink: a link is never followed", w.sizes.get(home, 0) < 900000)
        check("exclude: an excluded folder is never read",
              not any(p.startswith(os.path.join(home, "private")) for p in w.sizes) and w.sizes.get(home, 0) < 500000)
        check("depth: folder sizes reach the asked depth", os.path.join(home, "big", "inner") in w.sizes)
        check("space: a named space below the depth is still measured",
              os.path.join(home, "big", "inner", "deep") in w.sizes)
        # device: a fake device boundary, by patching lstat for one folder.
        far = os.path.join(home, "big")

        def fake_lstat(p, *k):
            st = real_lstat(p, *k)
            if os.path.abspath(p) == far:
                return os.stat_result((st.st_mode, st.st_ino, st.st_dev + 1) + tuple(st)[3:])
            return st
        os.lstat = fake_lstat
        try:
            w2 = Walk(sc, time.monotonic() + 60).run()
        finally:
            os.lstat = real_lstat
        check("device: the walk does not cross onto another disk", far not in w2.sizes)

        # unread: a fake disk with 50M used; the scan measured well under that, the rest is one total
        DISK_USAGE = FakeUsage(50 << 20)
        res = Result(sc, w, Ledger(os.path.join(T, "none.tsv")), "2026-01-02T00:00:00")
        m = mount_of(home)
        check("unread: space outside what you allowed is the used space less what was measured",
              res.outside.get(m) == (50 << 20) - sum(w.totals.values()))
        rc, out = quiet(report, sc, res, 10, False)
        check("unread: the report shows it as one total, never broken down",
              "Outside what you allowed" in out and beside not in out and outside not in out)
        led_u = os.path.join(T, "unread-ledger.tsv")
        with open(led_u, "w") as fh:
            fh.write("# outside 2026-01-01T00:00:00\t%s\t%d\t%s\n" % (m, 10 << 20, sc.digest()))
        res = Result(sc, w, Ledger(led_u), "2026-01-02T00:00:00")
        found = alerts(sc, res, None, "20M")
        check("unread: the total growing by SIZE raises ALERT unread",
              any(x.startswith("ALERT unread") and m in x for x in found))
        with open(led_u, "w") as fh:
            fh.write("# outside 2026-01-01T00:00:00\t%s\t%d\tsha256:other\n" % (m, 10 << 20))
        res = Result(sc, w, Ledger(led_u), "2026-01-02T00:00:00")
        check("unread: a total measured under other access lines is not compared",
              not any(x.startswith("ALERT unread") for x in alerts(sc, res, None, "20M")))
        DISK_USAGE = real_usage

        # guard: scan 1 after an approval is refused `denied`; scan 2 may read it but must not
        g_scope = os.path.join(T, "g", "scope.txt")
        os.makedirs(os.path.dirname(g_scope))
        with open(g_scope, "w") as fh:
            fh.write(body)
        gns = argparse.Namespace(scope=g_scope, top=10, tsv=True, alert_free=None, alert_growth=None)
        quiet(cmd_approve, gns)
        os.scandir = refusing({refused})
        try:
            rc1, out1 = quiet(cmd_scan, gns)
        finally:
            os.scandir = real_scandir
        seen = []
        os.scandir = refusing(set(), seen)
        try:
            rc2, out2 = quiet(cmd_scan, gns)
        finally:
            os.scandir = real_scandir
        check("guard: a folder refused in the first approved scan is not read by the next",
              rc1 in (0, 1) and refused not in seen)
        check("guard: ALERT access names it, and the scan exits 3",
              rc2 == 3 and any(l.startswith("ALERT access") and refused in l for l in out2.splitlines()))
        rc, out = quiet(cmd_plan, gns)
        check("guard: plan names the held-back folder", refused in out)
        lines, digest = open(g_scope).read().splitlines(), Scope(g_scope).digest()
        with open(g_scope, "w") as fh:
            fh.write("\n".join(l for l in lines if not l.startswith("approved ")) +
                     "\napproved 2099-01-01T00:00:00 %s\n" % digest)
        seen = []
        os.scandir = refusing(set(), seen)
        try:
            rc3, out3 = quiet(cmd_scan, gns)
        finally:
            os.scandir = real_scandir
        check("guard: approving again lets the next scan read it", refused in seen and "ALERT access" not in out3)

        # approve: changing an access line voids the approval
        with open(scope, "a") as fh:
            fh.write("allow %s depth 1\n" % outside)
        check("approve: a widened scope needs approval again", not Scope(scope).is_approved())

        # growth: two ledger rows a day apart
        led = os.path.join(T, "ledger.tsv")
        with open(led, "w") as fh:
            fh.write("2026-01-01T00:00:00\t/x\t%d\t-\n" % (1 << 30))
        prev = previous_sizes(read_ledger(led), "2026-01-03T00:00:00")
        g = growth({"/x": 3 << 30}, prev, "2026-01-03T00:00:00")[0]
        check("growth: measured against the previous scan of the same path",
              g and g[0][0] == 2 << 30 and abs(g[0][1] - (1 << 30)) < 2)

        # access: like with like. /x/a went unread last time and not now, or the last scan recorded nothing.
        x = os.sep + "x"
        same = growth({x: 3 << 30}, {x: ("2026-01-01T00:00:00", 1 << 30)}, "2026-01-03T00:00:00",
                      {"2026-01-01T00:00:00": [os.path.join(x, "a")]}, [os.path.join(x, "a")])
        changed = growth({x: 3 << 30}, {x: ("2026-01-01T00:00:00", 1 << 30)}, "2026-01-03T00:00:00",
                         {"2026-01-01T00:00:00": [os.path.join(x, "a")]}, [])
        unrecorded = growth({x: 3 << 30}, {x: ("2026-01-01T00:00:00", 1 << 30)}, "2026-01-03T00:00:00", {}, [])
        check("access: the same unread set both times is compared", same[0] and not same[1])
        check("access: a changed unread set is listed as not compared",
              not changed[0] and changed[1] == [x])
        check("access: a scan that recorded nothing is not compared", not unrecorded[0] and unrecorded[1] == [x])

        # first: folders whose last scan recorded nothing (before 0.7.0) are one line; a changed unread set is named
        y = os.sep + "y"
        prev2 = {x: ("2026-01-01T00:00:00", 1), y: ("2026-01-02T00:00:00", 1)}
        ch, un = split_not_compared([x, y], prev2, {"2026-01-02T00:00:00": []})
        check("first: a pre-0.7.0 scan's folders are counted in one line, a changed unread set is named",
              ch == [y] and un == [x])

        # alert: an impossible free-space floor and a planted growth both raise ALERT lines
        sc_a = Scope(os.path.join(T, "g", "scope.txt"))
        res = Result(sc_a, w, Ledger(os.path.join(T, "none.tsv")), "2026-01-02T00:00:00")
        res.prev = {far: ("2026-01-01T00:00:00", 0)}
        res.prev_notread = {"2026-01-01T00:00:00": [p for p, _ in w.notread]}
        found = alerts(sc_a, res, "1000T", "100K")
        check("alert: low free space and fast growth raise ALERT lines",
              any(x.startswith("ALERT free space") for x in found) and
              any(x.startswith("ALERT growth") and x.find(far) > 0 for x in found))
        check("alert: no options, no alerts", alerts(sc_a, res, None, None) == [])

        # unnamed: big is 100K, big/ is 300K and unnamed
        rc, out = quiet(report, sc_a, Result(sc_a, w, Ledger(os.path.join(T, "none.tsv")), "2026-01-03T00:00:00"),
                        10, False)
        check("unnamed: a big folder no space line names is flagged",
              "no space line names" in out and far in out)

        # init: names only when asked, no allow line; plan says where the boundary is
        saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
        os.environ["HOME"] = os.environ["USERPROFILE"] = home
        try:
            i_scope = os.path.join(T, "i", "scope.txt")
            rc, plain = quiet(cmd_init, argparse.Namespace(scope=i_scope, list=False, hidden=False))
            text = open(i_scope).read()
            rc2, listed = quiet(cmd_init, argparse.Namespace(scope=i_scope, list=True, hidden=False))
            rc3, hidden = quiet(cmd_init, argparse.Namespace(scope=i_scope, list=True, hidden=True))
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        check("init: the starting scope allows nothing",
              rc == 0 and not any(l.startswith("allow") for l in text.splitlines()) and Scope(i_scope).allows == [])
        check("init: folder names are listed only when asked, names only",
              "Zebra Notes" not in plain and "Zebra Notes" in listed and "inner" not in listed
              and "a.bin" not in listed and ".hidden-place" not in listed and ".hidden-place" in hidden)
        rc, out = quiet(cmd_plan, argparse.Namespace(scope=scope))
        check("plan: names the allow list as the boundary, not the system's permissions",
              "boundary is the allow list" in out and "whatever the system would let it read" in out)

        # trees
        if shutil.which("git"):
            trees = os.path.join(T, "trees")
            os.makedirs(trees)
            origin = os.path.join(T, "origin.git")
            run = lambda *c: subprocess.run(list(c), capture_output=True, check=True)
            run("git", "init", "-q", "--bare", origin)
            for n in ("clean", "dirty", "source"):
                run("git", "clone", "-q", origin, os.path.join(trees, n))
                run("git", "-C", os.path.join(trees, n), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "--allow-empty", "-m", "c")
                run("git", "-C", os.path.join(trees, n), "push", "-q", "origin", "HEAD:refs/heads/" + n)
            run("git", "clone", "-q", "--shared", os.path.join(trees, "source"), os.path.join(trees, "borrower"))
            with open(os.path.join(trees, "dirty", "new.txt"), "w") as fh:
                fh.write("x")
            old = time.time() - 3 * 86400
            for top, dirs, files in os.walk(trees):
                for n in dirs + files:
                    os.utime(os.path.join(top, n), (old, old))
            srcs = set()
            for n in os.listdir(trees):
                srcs.update(alternates(os.path.join(trees, n)))
            v = {n: verdict(os.path.join(trees, n), os.path.realpath(os.path.join(trees, n)), 24, srcs, set())
                 for n in os.listdir(trees)}
            check("trees: a clean, pushed, idle tree is removable", v.get("clean") == "removable")
            check("lossless: a tree with unsaved work is never removable", v.get("dirty") != "removable")
            check("borrowed: a tree another borrows from is never removable", v.get("source") != "removable")
        else:
            print("skip trees: no git on the path")
    finally:
        os.scandir, os.lstat, DISK_USAGE = real_scandir, real_lstat, real_usage
        shutil.rmtree(T, ignore_errors=True)
    print("diskscan self-test: %s -- %d failed" % ("FAIL" if fails else "PASS", len(fails)))
    return 1 if fails else 0


def main(argv):
    if "--self-test" in argv:
        broken = []
        if "--break" in argv:
            broken = [argv[argv.index("--break") + 1]]
        return self_test(broken)
    ap = argparse.ArgumentParser(prog="diskscan.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("init", "plan", "approve", "scan"):
        s = sub.add_parser(name)
        s.add_argument("--scope")
        if name == "init":
            s.add_argument("--list", action="store_true", help="print the names of the folders in the home folder")
            s.add_argument("--hidden", action="store_true", help="with --list, include names that start with a dot")
        if name == "scan":
            s.add_argument("--top", type=int, default=15)
            s.add_argument("--tsv", action="store_true")
            s.add_argument("--alert-free", metavar="SIZE")
            s.add_argument("--alert-growth", metavar="SIZE")
    t = sub.add_parser("trees")
    t.add_argument("dirs", nargs="+")
    t.add_argument("--hours", type=int, default=24)
    a = ap.parse_args(argv)
    try:
        return {"init": cmd_init, "plan": cmd_plan, "approve": cmd_approve, "scan": cmd_scan,
                "trees": cmd_trees}[a.cmd](a)
    except ScopeError as e:
        print(e if str(e).startswith("diskscan:") else "diskscan: %s" % e)
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
