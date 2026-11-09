#!/usr/bin/env python3
"""diskscan.py: find where a machine's disk space went, inside a scope its owner has read and approved, for diskcheck.

Agents leave things behind: a clone per job, a worktree per subagent, model downloads, VM disks, scratch folders.
Each is small; together they fill a disk in days. This tool measures where the space is, what is growing, and what
no one has named yet. It only reads, and only inside its scope.

The scope is a plain text file the owner can read and edit (default: <config>/pitmaster/diskcheck/scope.txt, where
<config> is ~/.config, or %APPDATA% on Windows). One line each:

  root <path> [depth N]   a folder to measure; folder sizes are reported N levels below it (default 1)
  exclude <path>          never read, nor anything inside it
  limit <seconds>         stop reading after this long and mark the scan partial (default 900)
  big <size>              flag a folder this big that no space line names (default 5G)
  ledger <path>           where scan results are appended (default: beside the scope file)
  space <path> <kind> [note...]
                          a known space: kind is trees (git clones or worktrees), cold (keep, but could move),
                          cache (an app can rebuild it) or live (in use; leave it)
  approved <date> <digest>
                          written by `approve`; never by hand

Paths may start with ~. Lines starting with # are comments.

The access lines are root, exclude, limit and ledger. `approve` writes a digest of them, and `scan` refuses to run
when they no longer match it, so widening what the tool reads always takes the owner's fresh approval. space and big
lines can change freely: they describe, they do not grant.

Usage:
  python3 diskscan.py init [--scope FILE]          write a starting scope (refuses to overwrite one)
  python3 diskscan.py plan [--scope FILE]          print what a scan would read and write; reads nothing
  python3 diskscan.py approve [--scope FILE]       record the owner's approval of the current access lines
  python3 diskscan.py scan [--scope FILE] [--top N] [--tsv] [--alert-free SIZE] [--alert-growth SIZE]
  python3 diskscan.py trees DIR... [--hours N]     read-only verdicts on the git trees inside each DIR
  python3 diskscan.py --self-test [--break NAME]

What scan reads: the names, sizes, modification times and device numbers of files and folders (os.lstat). Never a
file's contents. It does not follow symbolic links, does not cross onto another disk below a root, does not ask
for raised privileges, and does not use the network. A folder the system refuses is counted and named, not retried.
With --alert-free or --alert-growth, scan first prints one ALERT line per disk with less free space than SIZE and per
folder that grew by SIZE or more since the previous scan, and exits 3 when there is any. This is for a scheduled,
report-only run: it changes nothing more than a plain scan does.
What scan writes: rows appended to the ledger (date, path, bytes, newest modification time) and one audit line
saying what it read. Nothing else.

What trees reads: each git tree's status, branches and alternates, through git with GIT_OPTIONAL_LOCKS=0 so the
reading changes nothing. A tree is "removable" only when it is idle, not in use, borrowed from by no other tree,
its own borrowed objects still exist, and it has nothing a remote lacks. The tool never removes anything.

The guards, each named when it fails:
  symlink   a link is never followed
  device    the walk never crosses onto another disk below a root
  exclude   an excluded folder is never read
  approve   scan refuses an unapproved or changed scope
  denied    a refused folder is counted and the scan goes on
  growth    growth is measured against the previous scan of the same path
  unnamed   a big folder no space line names is flagged
  lossless  a tree with uncommitted or unpushed work is never removable
  borrowed  a tree another tree borrows objects from is never removable
  alert     low free space and fast growth raise ALERT lines and exit 3

Exit status: 0 done, 1 scan refused or partial, 2 usage error, 3 an alert was raised. Python 3.8+, standard library only.
"""
import argparse
import datetime as dt
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time

ACCESS_KEYS = ("root", "exclude", "limit", "ledger")
KINDS = ("trees", "cold", "cache", "live")
BREAK = set()

PRIVATE_MAC = ["Library/Mail", "Library/Messages", "Library/Safari", "Library/Keychains", "Library/Cookies",
               "Library/Mobile Documents", "Library/Application Support/Google/Chrome",
               "Library/Application Support/Firefox", "Library/Containers/com.apple.mail",
               "Library/Group Containers", "Pictures/Photos Library.photoslibrary"]
PRIVATE_ALL = [".ssh", ".gnupg", ".password-store", ".mozilla", ".config/google-chrome", ".config/chromium"]
SPACES_GUESS = [("codex-trees", "trees", "Codex job clones"), ("agy-trees", "trees", "Antigravity job clones"),
                (".claude", "live", "Claude Code's sessions and settings"), (".codex", "live", "Codex sessions"),
                (".lima", "live", "Lima virtual machines"), (".ollama", "cache", "Ollama models"),
                (".lmstudio", "cache", "LM Studio models"), (".cache/huggingface", "cache", "Hugging Face downloads"),
                ("Library/Developer/CoreSimulator", "cache", "Xcode simulators"), (".Trash", "cold", "the Trash")]


# --- the scope file ---------------------------------------------------------

def config_dir():
    base = os.environ.get("APPDATA") if os.name == "nt" else None
    base = base or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "pitmaster", "diskcheck")


def expand(p):
    return os.path.abspath(os.path.expanduser(p))


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


class Scope:
    def __init__(self, path):
        self.path = path
        self.roots, self.excludes, self.spaces = [], [], []
        self.limit, self.big, self.ledger = 900, 5 << 30, None
        self.approved = None
        self.access_lines = []
        with open(path, encoding="utf-8") as f:
            for n, raw in enumerate(f, 1):
                line = raw.split("#", 1)[0].strip() if not raw.lstrip().startswith("space") else raw.strip()
                if not line or line.startswith("#"):
                    continue
                key, _, rest = line.partition(" ")
                rest = rest.strip()
                if key in ACCESS_KEYS:
                    self.access_lines.append(" ".join(line.split()))
                if key == "root":
                    parts = rest.rsplit(" depth ", 1)
                    self.roots.append((expand(parts[0].strip()), int(parts[1]) if len(parts) == 2 else 1))
                elif key == "exclude":
                    self.excludes.append(expand(rest))
                elif key == "limit":
                    self.limit = int(rest.rstrip("s"))
                elif key == "big":
                    self.big = parse_size(rest)
                elif key == "ledger":
                    self.ledger = expand(rest)
                elif key == "space":
                    bits = rest.split(" ", 2) if not rest.startswith('"') else None
                    p, kind, note = self._space(rest)
                    if kind not in KINDS:
                        raise SystemExit("%s:%d: space kind must be one of %s" % (path, n, ", ".join(KINDS)))
                    self.spaces.append((expand(p), kind, note))
                elif key == "approved":
                    self.approved = rest.split()
                else:
                    raise SystemExit("%s:%d: unknown line: %s" % (path, n, key))
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


def default_scope_text(home):
    lines = ["# diskcheck scope: what diskscan may read. Edit it, then run `diskscan.py approve` again.",
             "# Access lines (root, exclude, limit, ledger) need approval; space and big lines do not.", "",
             "root ~ depth 2", "limit 900", "big 5G", "",
             "# Private places, never read:"]
    for p in PRIVATE_ALL + (PRIVATE_MAC if sys.platform == "darwin" else []):
        if os.path.exists(os.path.join(home, p)):
            lines.append("exclude ~/" + p)
    lines += ["", "# Known spaces found in the home folder (kind: trees, cold, cache or live):"]
    for p, kind, note in SPACES_GUESS:
        if os.path.exists(os.path.join(home, p)):
            lines.append("space ~/%s %s %s" % (p, kind, note))
    return "\n".join(lines) + "\n"


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
                u = shutil.disk_usage(p)
                seen[m] = (u.total, u.used, u.free)
    return seen


# --- the walk ---------------------------------------------------------------

class Walk:
    def __init__(self, scope, deadline):
        self.scope, self.deadline = scope, deadline
        self.sizes, self.newest = {}, {}
        self.entries, self.denied, self.partial = 0, [], False
        self.inodes = set()
        self.wanted = {sp for sp, _, _ in scope.spaces}

    def excluded(self, p):
        if "exclude" in BREAK:
            return False
        return any(p == e or p.startswith(e + os.sep) for e in self.scope.excludes)

    def run(self):
        for root, depth in self.scope.roots:
            if not os.path.isdir(root) or self.excluded(root):
                continue
            self.walk(root, 0, depth, os.lstat(root).st_dev)
        return self

    def walk(self, d, level, depth, dev):
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
            return 0, 0
        for ent in it:
            p = ent.path
            if self.excluded(p):
                continue
            try:
                st = os.lstat(p) if "symlink" not in BREAK else os.stat(p)
            except OSError:
                continue
            self.entries += 1
            newest = max(newest, st.st_mtime)
            if stat.S_ISLNK(st.st_mode):
                total += 0
                continue
            if stat.S_ISDIR(st.st_mode):
                if st.st_dev != dev and "device" not in BREAK:
                    continue
                t, nw = self.walk(p, level + 1, depth, dev)
                total += t
                newest = max(newest, nw)
            else:
                key = (st.st_dev, st.st_ino)
                if st.st_nlink > 1:
                    if key in self.inodes:
                        continue
                    self.inodes.add(key)
                total += getattr(st, "st_blocks", 0) * 512 if hasattr(st, "st_blocks") else st.st_size
        if level <= depth or d in self.wanted:
            self.sizes[d] = total
            self.newest[d] = newest
        return total, newest


# --- the ledger -------------------------------------------------------------

def read_ledger(path):
    rows = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 3 and parts[0] != "date" and not parts[0].startswith("#"):
                    rows.append((parts[0], parts[1], int(parts[2])))
    return rows


def previous_sizes(rows, now):
    last = {}
    for when, path, size in rows:
        if when < now:
            last[path] = (when, size)
    return last


def growth(sizes, prev, now):
    out = []
    t_now = dt.datetime.fromisoformat(now)
    for p, size in sizes.items():
        if p in prev and "growth" not in BREAK:
            when, old = prev[p]
            days = max((t_now - dt.datetime.fromisoformat(when)).total_seconds() / 86400, 1e-6)
            out.append((size - old, (size - old) / days, p, when))
        elif "growth" in BREAK:
            out.append((size, size, p, now))
    return sorted(out, reverse=True)


# --- commands ----------------------------------------------------------------

def cmd_init(a):
    path = a.scope or os.path.join(config_dir(), "scope.txt")
    if os.path.exists(path):
        print("diskscan: a scope already exists at %s; edit it instead" % path)
        return 2
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(default_scope_text(os.path.expanduser("~")))
    print("diskscan: wrote a starting scope to %s. Read it, then run `plan`." % path)
    return 0


def cmd_plan(a):
    sc = Scope(a.scope or os.path.join(config_dir(), "scope.txt"))
    print("Scope file: %s" % sc.path)
    print("Status: %s" % ("APPROVED on %s (%s)" % (sc.approved[0], sc.digest()) if sc.is_approved()
                          else "NOT APPROVED: scan will refuse until `approve` is run (digest %s)" % sc.digest()))
    print("\nDisks the roots are on (the scan never leaves these):")
    for m, (tot, used, free) in disks_for([r for r, _ in sc.roots]).items():
        print("  %-30s %s total, %s used, %s free" % (m, human(tot), human(used), human(free)))
    print("\nWill read (sizes, dates and names only; never file contents):")
    for r, d in sc.roots:
        print("  %s  (folder sizes reported %d level%s down)" % (r, d, "" if d == 1 else "s"))
    print("\nWill never read:")
    for e in sc.excludes:
        print("  %s" % e)
    print("  anything behind a symbolic link, or on another disk below a root")
    print("\nWill write: rows appended to %s, and one audit line there" % sc.ledger)
    print("Stops after: %d s (then marks the scan partial)" % sc.limit)
    print("Never: deletes, moves, raises privileges, or uses the network.")
    return 0


def cmd_approve(a):
    path = a.scope or os.path.join(config_dir(), "scope.txt")
    sc = Scope(path)
    with open(path, encoding="utf-8") as f:
        kept = [l for l in f if not l.lstrip().startswith("approved ")]
    stamp = "approved %s %s\n" % (dt.date.today().isoformat(), sc.digest())
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(kept + ([] if kept and kept[-1].endswith("\n") else ["\n"]) + [stamp])
    print("diskscan: approved the access lines of %s (%s)" % (path, sc.digest()))
    return 0


def cmd_scan(a):
    sc = Scope(a.scope or os.path.join(config_dir(), "scope.txt"))
    if not sc.is_approved():
        print("diskscan: REFUSED: the scope's access lines are not approved, or changed since (%s). "
              "Run `plan`, show it to the owner, and run `approve` on their yes." % sc.digest())
        return 1
    now = dt.datetime.now().replace(microsecond=0).isoformat()
    t0 = time.monotonic()
    w = Walk(sc, t0 + sc.limit).run()
    took = time.monotonic() - t0
    rows = read_ledger(sc.ledger)
    prev = previous_sizes(rows, now)
    os.makedirs(os.path.dirname(sc.ledger), exist_ok=True)
    new = not os.path.exists(sc.ledger)
    with open(sc.ledger, "a", encoding="utf-8") as f:
        if new:
            f.write("date\tpath\tbytes\tnewest\n")
        for p, size in sorted(w.sizes.items()):
            f.write("%s\t%s\t%d\t%s\n" % (now, p, size,
                    dt.datetime.fromtimestamp(w.newest.get(p, 0)).date().isoformat()))
        f.write("# audit %s roots=%s entries=%d denied=%d excluded=%d seconds=%.0f%s\n" % (
            now, ",".join(r for r, _ in sc.roots), w.entries, len(w.denied), len(sc.excludes), took,
            " PARTIAL" if w.partial else ""))
    found = alerts(sc, w, prev, now, getattr(a, "alert_free", None), getattr(a, "alert_growth", None))
    for line in found:
        print(line)
    report(sc, w, prev, now, took, a.top, a.tsv)
    if found:
        return 3
    return 1 if w.partial else 0


def alerts(sc, w, prev, now, free_min, growth_min):
    """ALERT lines for low free space and fast growth; empty when neither option is given."""
    if "alert" in BREAK:
        return []
    out = []
    if free_min:
        for m, (tot, used, free) in disks_for([r for r, _ in sc.roots]).items():
            if free < parse_size(free_min):
                out.append("ALERT free space: %s has %s free, under %s" % (m, human(free), free_min))
    if growth_min:
        roots = {r for r, _ in sc.roots}
        leaf = {p: s for p, s in w.sizes.items() if p not in roots}
        for delta, rate, p, when in growth(leaf, prev, now):
            if delta >= parse_size(growth_min):
                out.append("ALERT growth: %s grew %s since %s" % (p, human(delta), when[:16]))
    return out


def named(sc, p):
    """p is a known space, or inside one."""
    return any(p == s or p.startswith(s + os.sep) for s, _, _ in sc.spaces)


def unnamed_rest(sc, sizes, p):
    """p's size less the named spaces measured inside it (the outermost ones only)."""
    inside = [s for s, _, _ in sc.spaces if s.startswith(p + os.sep) and s in sizes]
    top = [s for s in inside if not any(o != s and s.startswith(o + os.sep) for o in inside)]
    return sizes.get(p, 0) - sum(sizes[s] for s in top)


def report(sc, w, prev, now, took, top, tsv):
    roots = {r for r, _ in sc.roots}
    leaf = {p: s for p, s in w.sizes.items() if p not in roots}
    if tsv:
        for p, s in sorted(leaf.items(), key=lambda x: -x[1]):
            print("%d\t%s\t%s" % (s, p, "named" if named(sc, p) else "unnamed"))
        return
    print("diskscan %s: read %d entries in %.0f s%s" % (now, w.entries, took, "  PARTIAL (time limit)" if w.partial else ""))
    print("\nDisks:")
    for m, (tot, used, free) in disks_for(list(roots)).items():
        print("  %-30s %s free of %s (%d%% used)" % (m, human(free), human(tot), 100 * used // max(tot, 1)))
    if w.denied:
        print("\nRefused by the system, not read (%d):" % len(w.denied))
        for d, why in w.denied[:8]:
            print("  %s  (%s)" % (d, why))
    print("\nBiggest folders:")
    for p, s in sorted(leaf.items(), key=lambda x: -x[1])[:top]:
        print("  %8s  %s%s" % (human(s), p, "" if named(sc, p) else "  [unnamed]"))
    g = [x for x in growth(leaf, prev, now) if abs(x[0]) >= (1 << 30)]
    if g:
        print("\nChanged most since the last scan (1 GB or more):")
        for delta, rate, p, when in g[:top]:
            print("  %+9s  (%s/day since %s)  %s" % (human(delta), human(rate), when[:10], p))
    elif not prev:
        print("\nNo earlier scan in the ledger: growth shows from the next scan on.")
    big_unnamed = {p for p in leaf if not named(sc, p) and unnamed_rest(sc, w.sizes, p) >= sc.big}
    unnamed = [(unnamed_rest(sc, w.sizes, p), p) for p in big_unnamed
               if not any(q != p and q.startswith(p + os.sep) for q in big_unnamed)]
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
            print("  %8s  %-5s  %s%s" % (human(s) if s is not None else "-", kind, p, ("  " + note) if note else ""))


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

def self_test(broken):
    BREAK.update(broken)
    fails = []
    T = tempfile.mkdtemp(prefix="diskscan-test-")
    try:
        home = os.path.join(T, "home")
        os.makedirs(os.path.join(home, "big", "inner", "deep"))
        os.makedirs(os.path.join(home, "private"))
        os.makedirs(os.path.join(home, "denied"))
        outside = os.path.join(T, "outside")
        os.makedirs(outside)
        for f, n in (("big/inner/a.bin", 300000), ("private/secret.bin", 500000), ("denied/x.bin", 1000),
                     ("small.bin", 10)):
            with open(os.path.join(home, f), "wb") as fh:
                fh.write(b"x" * n)
        with open(os.path.join(outside, "far.bin"), "wb") as fh:
            fh.write(b"x" * 900000)
        os.symlink(outside, os.path.join(home, "link"))
        scope = os.path.join(T, "scope.txt")
        with open(scope, "w") as fh:
            fh.write("root %s depth 2\nexclude %s\nbig 100K\nspace %s live small\nspace %s cold deep\n"
                     % (home, os.path.join(home, "private"), os.path.join(home, "small.bin"),
                        os.path.join(home, "big", "inner", "deep")))

        def check(name, ok):
            print("%s %s" % ("ok  " if ok else "FAIL", name))
            if not ok:
                fails.append(name)

        ns = argparse.Namespace(scope=scope, top=10, tsv=True)
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cmd_scan(ns)
        check("approve: an unapproved scope is refused", rc == 1 and "REFUSED" in buf.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            cmd_approve(ns)
        sc = Scope(scope)
        # The refusal is planted in os.scandir, so it holds on every system and for an administrator too
        # (chmod cannot refuse anything on Windows, or to root).
        real_scandir, refused = os.scandir, os.path.join(home, "denied")

        def refusing_scandir(path="."):
            if os.path.abspath(path) == refused:
                raise PermissionError(13, "Permission denied", path)
            return real_scandir(path)
        os.scandir = refusing_scandir
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
        check("symlink: a link is never followed", w.sizes.get(home, 0) < 900000)
        check("exclude: an excluded folder is never read",
              not any(p.startswith(os.path.join(home, "private")) for p in w.sizes) and w.sizes.get(home, 0) < 500000)
        check("depth: folder sizes reach the asked depth", os.path.join(home, "big", "inner") in w.sizes)
        check("space: a named space below the depth is still measured",
              w.sizes.get(os.path.join(home, "big", "inner", "deep"), -1) == 0 or
              os.path.join(home, "big", "inner", "deep") in w.sizes)
        # device: a fake device boundary, by patching lstat for one folder.
        real_lstat = os.lstat
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
        # approve: changing an access line voids the approval
        with open(scope, "a") as fh:
            fh.write("root %s depth 1\n" % outside)
        check("approve: a widened scope needs approval again", not Scope(scope).is_approved())
        # growth: two ledger rows a day apart
        led = os.path.join(T, "ledger.tsv")
        with open(led, "w") as fh:
            fh.write("2026-01-01T00:00:00\t/x\t%d\t-\n" % (1 << 30))
        prev = previous_sizes(read_ledger(led), "2026-01-03T00:00:00")
        g = growth({"/x": 3 << 30}, prev, "2026-01-03T00:00:00")
        check("growth: measured against the previous scan of the same path",
              g and g[0][0] == 2 << 30 and abs(g[0][1] - (1 << 30)) < 2)
        # alert: an impossible free-space floor and a planted growth both raise ALERT lines
        a_prev = {os.path.join(home, "big"): ("2026-01-01T00:00:00", 0)}
        found = alerts(Scope(scope), w, a_prev, "2026-01-02T00:00:00", "1000T", "100K")
        check("alert: low free space and fast growth raise ALERT lines",
              any(x.startswith("ALERT free space") for x in found) and
              any(x.startswith("ALERT growth") and x.find(os.path.join(home, "big")) > 0 for x in found))
        check("alert: no options, no alerts", alerts(Scope(scope), w, a_prev, "2026-01-02T00:00:00", None, None) == [])
        # unnamed: big is 100K, big/ is 300K and unnamed
        sc2 = Scope(scope)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            report(sc2, w, {}, "2026-01-03T00:00:00", 0, 10, False)
        check("unnamed: a big folder no space line names is flagged",
              "no space line names" in buf.getvalue() and os.path.join(home, "big") in buf.getvalue())
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
        if name == "scan":
            s.add_argument("--top", type=int, default=15)
            s.add_argument("--tsv", action="store_true")
            s.add_argument("--alert-free", metavar="SIZE")
            s.add_argument("--alert-growth", metavar="SIZE")
    t = sub.add_parser("trees")
    t.add_argument("dirs", nargs="+")
    t.add_argument("--hours", type=int, default=24)
    a = ap.parse_args(argv)
    return {"init": cmd_init, "plan": cmd_plan, "approve": cmd_approve, "scan": cmd_scan, "trees": cmd_trees}[a.cmd](a)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
