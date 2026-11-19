# diskscan

Finds where a machine's disk space went: the biggest folders, what grew since the last scan, and big folders
nobody has named. It is built for machines where agents work: a clone per job, a worktree per subagent, model
downloads and VM disks add up to hundreds of gigabytes in days. It only reads, and only the folders its owner
allowed by name.

Python 3.8 or newer, standard library only; `git` on the path for `trees`. Runs on macOS, Linux and Windows.

## Usage

```
python3 diskscan.py init [--scope FILE] [--list [--hidden]]
                                              write a starting scope that allows nothing (never overwrites one);
                                              --list prints the names of the folders in your home folder
python3 diskscan.py plan [--scope FILE]       show what a scan would read and write; reads nothing
python3 diskscan.py approve [--scope FILE]    record the owner's approval of the scope
python3 diskscan.py scan [--scope FILE] [--top N] [--tsv] [--alert-free SIZE] [--alert-growth SIZE]
python3 diskscan.py trees DIR... [--hours N]  read-only verdicts on the git trees in each DIR
python3 diskscan.py --self-test [--break NAME]
```

The scope lives at `~/.config/pitmaster/diskcheck/scope.txt` (`%APPDATA%\pitmaster\diskcheck\scope.txt` on
Windows) unless `--scope` names another file.

## The scope: what it may read

A plain text file, one line each:

| Line | Meaning |
|------|---------|
| `allow <path> [depth N]` | A folder the scan may read. Folder sizes are reported N levels below it (default 1). Nothing outside an allow line is read. |
| `exclude <path>` | Never read, nor anything inside it: carves a folder out of an allowed one. |
| `limit <seconds>` | Stop reading after this long and mark the scan partial (default 900). |
| `ledger <path>` | Where results are appended (default: `ledger.tsv` beside the scope). |
| `big <size>` | Flag a folder this big that no `space` line names (default `5G`). |
| `space <path> <kind> [note]` | A known space. Kind: `trees` (git clones or worktrees), `cold` (keep, but could move), `cache` (an app can rebuild it), `live` (in use; leave it). A space line describes a folder; it does not allow it. |
| `approved <time> <digest>` | Written by `approve`, never by hand. |

**Starting out.** `init` writes a scope with no allow line, so nothing is read until the owner picks folders. It
adds a `space` line for each common agent space it finds. To help them choose, `init --list` prints the names of
the folders in the home folder, the way a file browser shows them: names only, no sizes, nothing inside read
(`--hidden` adds the names that start with a dot). Or they type the folders themselves.

**Old scopes.** A scope with a `root` line (diskscan 0.6 and before) is refused, with the allow lines to write
instead. The owner approves the rewritten scope.

**Approval.** The access lines are `allow`, `exclude`, `limit` and `ledger`. `approve` stores a digest of them, and
`scan` refuses to run when they no longer match it. Widening what the tool reads therefore always takes the owner's
fresh approval. `space` and `big` lines describe and grant nothing, so they can change freely.

**The boundary is the allow list,** not the system's permissions: a scan reads the same folders whether a person
or a scheduler starts it, and whatever the system would let it read.

**The consent guard.** The first scan after an approval records the folders the system refused it. Later scans
under the same approval leave those folders unread, as if excluded, and raise an `ALERT access` line naming them,
until the owner approves again. So a scan never reads more than the first approved scan did: a scheduler that
holds wider access (macOS gives a launchd job access an app's shell lacks) cannot widen it. Running `approve` again,
even unchanged, starts a new guard.

## What it reads, writes and never does

- **Reads** the names, sizes, dates and device numbers of files and folders inside the allowed folders. Never a
  file's contents.
- **Writes** rows to the ledger (date, path, bytes, newest date inside) and one audit line per scan, plus comment
  lines that older readers skip: the folders it did not read (excluded, refused or guarded), the total outside
  what you allowed per disk, and the guard of the first scan after an approval.
- **Never** follows a symbolic link (an allowed folder that is a link included), crosses onto another disk below an
  allowed folder, deletes or moves anything, asks for raised privileges, or uses the network. A folder the system
  refuses is counted and named, not retried.

## What scan reports

Each disk's free space; **outside what you allowed**: the disk's used space less what the scan measured, as one
total that is never broken down or read (it also holds system files, snapshots and other accounts' files, so it
says "something grew", not what); the folders the system refused and the folders the guard held back; the biggest
folders, marked `[unnamed]` when no `space` line covers them; what changed by 1 GB or more since the last scan,
with a rate per day; the big folders no `space` line names; and the size of each known space (`not allowed` when
no allow line covers it).

**Like with like.** Growth is compared only when the same folders went unread inside a folder both times. A folder
whose unread set changed is listed as not compared. Folders whose earlier scan recorded no unread set (any scan
before 0.7.0) are not compared either, and are counted in one line rather than listed.
The outside total is compared only with a scan under the same access lines.

**Alerts, for a scheduled run.** `--alert-free 50G` raises an `ALERT free space` line for each scanned disk with less
than 50 GB free; `--alert-growth 20G` raises an `ALERT growth` line for each folder that grew 20 GB or more since the
previous scan, and an `ALERT unread` line when the space outside what you allowed grew 20 GB or more (choose a folder
to allow to know where). `ALERT access` is raised whenever the guard held a folder back. Alert lines print before
the report, and the exit status is 3 when there is any, so a scheduler can notify on that alone. An alerting scan
reads and writes exactly what a plain scan does.

## trees

For each git tree inside each DIR, one verdict. It reads through git with `GIT_OPTIONAL_LOCKS=0`, so reading changes
nothing (a plain `git status` updates the index, which makes every tree look recently used).

| Verdict | Meaning |
|---------|---------|
| `removable` | Idle for `--hours` (default 24), not in use, nothing borrows from it, and everything in it is on a remote. |
| `in-use` | A process is working inside it. |
| `borrowed` | Another tree borrows its objects (`git clone --shared`); removing it would break that tree. |
| `source-gone` | The tree it borrowed objects from is gone; git can no longer read its history. |
| `recent` | Something outside `.git`, `build` and `node_modules` changed within `--hours`. |
| `unsaved` | It has uncommitted or untracked changes. |
| `unpushed` | Its HEAD is on no remote-tracking branch. |
| `unreadable` | git could not read it. |

It never removes anything. Removing, or saving a tree's work first, is for the person or the skill acting on their
word.

## Self-test

`--self-test` builds a throwaway home folder with a folder beside it that is never allowed, a link, an excluded
folder, a refused folder, a fake second disk and a fake disk usage, plus throwaway git trees, and checks each guard.
`--break NAME` switches one guard off to show its case go red: `allow`, `symlink`, `device`, `exclude`, `approve`,
`denied`, `guard`, `growth`, `access`, `unread`, `unnamed`, `lossless`, `borrowed`, `alert`, `first`.
